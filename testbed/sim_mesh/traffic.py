"""The traffic driver: messages between stations for a whole run, on the
run's own clock, on firmware of one category: LXMF over `reticulum`
(sim_mesh.reticulum.driver), direct messages over `meshcore` or
`meshtastic` (sim_mesh.meshcore.driver, sim_mesh.meshtastic.driver).

    from sim_mesh import *
    from sim_mesh import traffic

    firmware = script_input("firmware", type=Firmware, category="reticulum")
    sim_speed("max")
    nodes().firmware(firmware)
    nodes().on_first_boot(Node.reticulum.lxmf.create())
    traffic.run(OPTIONS)                    # into the run directory as traffic.json
    sim_pause()

`category` says which; "auto" takes the one every station's firmware is of,
and refuses a mix, since a run compares one protocol at a time. The
stations that send and receive are every one, or with `skip_tag` those
not tagged so (the repeaters), which still forward. What differs between
the categories is sim_mesh.messaging's: each category's verbs for making a
node known, what a node knows of the others, its route to one, and a
direct message.

Phases, each skipped when its option says so:

  up        wait until every station is up.
  identity  each sending station's destination: for Reticulum its LXMF
            delivery address (the first the `lxmf.identities` verb lists),
            asked until every station has one; for the others its name.
  warm      rounds of announces across the senders (warm_rounds, each
            spread over warm_spread seconds with warm_gap between rounds),
            then, on the senders whose firmware can list what it knows of
            the others, a sample every settle_every seconds until their
            total does not rise between two samples (or settle_max has
            passed). With no such station, two settle_every waits.
            warm_rounds 0 skips the phase.
  snapshot  a snapshot, warm_snapshot, when given.
  traffic   one message every `every` seconds for `traffic` seconds, the
            sender, recipient, size class and text all drawn from `seed`,
            so two runs with one seed and one set of station names send the
            same messages at the same instants after the traffic starts,
            whatever the protocol. `arrivals` "poisson", `pairs` and `hub`
            vary when the messages go and between whom, keeping the
            messages themselves (`schedule`). Each send is preceded by the
            route at the sender, its length at send time where the firmware
            can say it. Size classes are `sizes` ({name: (fewest characters,
            most, weight)}), SIZES by default: short 20-55 characters,
            two-frame 115-300, and 420-650 (over 500 B on the wire: a link
            and a resource), weighted 167 : 76 : 39. Every text starts with
            its marker, `marker` and a four-digit number.
  drain     `drain` seconds after the last send slot.
  gather    with `gather`, every station's `diagnostics`.
  snapshot  end_snapshot, when given.

Once every station is up the driver says the run's phases (warm-up,
traffic and drain, each with the T it ends at), and again whenever warm-up
runs longer, so the page can say which phase the run is in and when it will
be done. The result holds the phases (wall and T at each boundary), every
clock message, the warm-up samples, every send with its route, message id
and reply, and the gathered output. How much of it was delivered is counted
afterwards from the run's events, what each sender's driver reported of
each message (sim_mesh.reticulum.delivery, `report`).
"""

import asyncio
import json
import os
import random
import time

from sim_mesh import library
from sim_mesh import messaging
from sim_mesh.sim import SimError

OPTIONS = {"warm_rounds": 3, "warm_spread": 300.0, "warm_gap": 120.0,
           "settle_every": 180.0, "settle_max": 3600.0, "warm_snapshot": None,
           "traffic": 3600.0, "every": 5.0, "seed": 17, "marker": "G", "drain": 600.0,
           "gather": True, "end_snapshot": None,
           "arrivals": "even", "hub": None, "hub_share": 0.5, "pairs": 0,
           "sizes": None, "category": "reticulum", "skip_tag": None}
ARRIVALS = ("even", "poisson")

WORDS = ("mesh relay gateway lora packet announce proof link path hop station "
         "field city river bridge north south east west signal").split()
# Size classes, {name: (fewest characters, most, weight)}: LXMF's, short, two
# frames, and over 500 B on the wire (a link and a resource).
SIZES = {"short": (20, 55, 167), "two": (115, 300, 76), "big": (420, 650, 39)}
RESULT_FILE = "traffic.json"


def body(rng, cls, marker, sizes=None):
    """A text of class `cls`. A length is drawn for every class but the last,
    and for the last only when it is the one: the draws traffic has always
    made, so one seed keeps its texts."""
    sizes = sizes or SIZES
    names = list(sizes)
    drawn = {k: rng.randint(sizes[k][0], sizes[k][1]) for k in names[:-1]}
    n = drawn.get(cls) or rng.randint(sizes[names[-1]][0], sizes[names[-1]][1])
    text = marker
    while len(text) < n:
        text += "-" + rng.choice(WORDS)
    return text[:n]


def schedule(names, seed, duration, every, marker, arrivals="even", hub=None,
             hub_share=0.5, pairs=0, sizes=None):
    """The traffic: (n, at, src, dst, cls, text), at in seconds after its start.

    By default one message every `every` seconds, its sender, recipient, size
    class (`sizes`, {name: (fewest, most, weight)}; SIZES by default) and
    text drawn from `seed`, in that order, from one stream. The
    variants draw from streams of their own, so the messages stay the ones
    the default draws, the same texts of the same classes in the same order,
    and only when they go and between whom changes:

    - arrivals "poisson": the gaps between sends drawn exponential with mean
      `every`, the first at the start, as many as fall within `duration`;
    - pairs > 0: every message between one of `pairs` sender-recipient pairs
      drawn once, taken in turn, like that many conversations at once;
    - hub: a share `hub_share` of the messages whose sender is not `hub` go to
      it instead, as to a gateway or a dispatcher.
    """
    if arrivals not in ARRIVALS:
        raise ValueError("arrivals are %s, not %r" % (" or ".join(ARRIVALS), arrivals))
    names = sorted(names)
    if hub is not None and hub not in names:
        raise ValueError("the hub %r is not one of the senders" % hub)
    if not 0.0 <= float(hub_share) <= 1.0:
        raise ValueError("a hub share is between 0 and 1, not %r" % hub_share)
    if int(pairs) < 0:
        raise ValueError("pairs is a count, not %r" % pairs)
    sizes = sizes or SIZES
    classes = [name for name, (_, _, weight) in sizes.items() for _ in range(weight)]
    rng = random.Random(seed)
    times = random.Random("%s:arrivals" % seed)
    ends = random.Random("%s:endpoints" % seed)
    fixed = []
    for _ in range(int(pairs)):
        src = ends.choice(names)
        fixed.append((src, ends.choice([s for s in names if s != src])))
    out = []
    n = 0
    at = 0.0
    while True:
        if arrivals == "even":
            at = n * every
        elif n:
            at += times.expovariate(1.0 / every)
        if at >= duration:
            break
        src = rng.choice(names)
        dst = rng.choice([s for s in names if s != src])
        cls = rng.choice(classes)
        text = body(rng, cls, "%s%04d" % (marker, n + 1), sizes)
        if fixed:
            src, dst = fixed[n % len(fixed)]
        if hub is not None and src != hub and ends.random() < float(hub_share):
            dst = hub
        out.append((n + 1, at, src, dst, cls, text))
        n += 1
    return out


class Options:
    """The phase options, `OPTIONS` with whatever a script gives over them."""

    def __init__(self, **given):
        unknown = set(given) - set(OPTIONS)
        if unknown:
            raise ValueError("no traffic option %s" % ", ".join(sorted(unknown)))
        self.__dict__.update(OPTIONS, **given)


def run(opts=None, out_path=None):
    """The whole run on the script's simulation (sim_mesh.library): the
    result, written to `out_path` (the run directory's traffic.json by
    default) as it grows."""
    sim = library.runtime.held()
    out_path = out_path or os.path.join(sim.run_dir, RESULT_FILE)
    return library.runtime.call(run_on(sim, opts or {}, out_path))


async def run_on(sim, opts, out_path):
    """The whole run on a held `sim_mesh.Sim`. `opts` is an Options (or a
    mapping of them); the result is written to `out_path` as it grows, and
    returned."""
    if isinstance(opts, dict):
        opts = Options(**opts)
    result = sim.result
    result.update({"args": dict(vars(opts)), "phases": [], "wall_start": time.time()})

    def phase(name):
        result["phases"].append([name, round(time.time(), 3), sim.t])
        print("%s: run %.1f s, wall %.1f s" % (name, sim.run_s, time.time() - result["wall_start"]),
              flush=True)

    def dump():
        with open(out_path, "w") as f:
            json.dump(result, f, indent=1)

    await sim.all_up()
    result["up"] = sim.up
    phase("all_up")
    await run_phases(opts, sim, result, phase, dump)
    result["wall_end"] = time.time()
    result["t_end"] = sim.t
    dump()
    print("done", flush=True)
    return result


def meta(verb, category=None, **args):
    """A verb as simd takes it: a category's (only on its stations), or with
    none, every firmware's."""
    out = {"type": "meta", "verb": verb, "args": args}
    if category is not None:
        out["category"] = category
    return out


# ---- which protocol ----------------------------------------------------------

def protocol_of(sim, stations, category):
    """The protocol the stations speak (sim_mesh.messaging): `category`, or
    with "auto" the one category every station's firmware is of, a mix
    refused."""
    if category != "auto":
        return messaging.protocol(category)
    by = {}
    for name in stations:
        by.setdefault(sim.stations[name].category, []).append(name)
    if len(by) != 1:
        raise ValueError("the nodes run firmware of more than one category, and traffic "
                         "compares one at a time: %s" % "; ".join(
                             "%s %s" % (cat, ", ".join(names)) for cat, names in sorted(
                                 by.items(), key=lambda kv: str(kv[0]))))
    return messaging.protocol(next(iter(by)))


def ask_of(proto, step):
    """A protocol's (verb, args) as the meta message simd takes."""
    verb, args = step
    return meta(verb, proto.category, **args)


async def run_phases(opts, sim, result, phase, dump):
    stations = sorted(n for n, s in sim.stations.items() if s.firmware)
    try:
        proto = protocol_of(sim, stations, opts.category)
    except ValueError:
        # Nothing to compare: the simulation stops rather than run on unwatched.
        await sim.stop()
        raise
    result["category"] = proto.category
    # The stations that send and receive: every one, or those without skip_tag.
    ends = [n for n in stations if opts.skip_tag not in sim.stations[n].tags] \
        if opts.skip_tag else stations
    if len(ends) < 2:
        raise ValueError("traffic needs two stations to send between, and has %d" % len(ends))
    result["endpoints"] = ends
    dests, answers = await proto.destinations(sim, ends)
    result["dests"] = dests
    # What a station with no destination last said, and what it runs: what
    # the report tells the reader of why.
    result["no_dest"] = {n: {"firmware": sim.stations[n].firmware,
                             "answer": str(answers.get(n, "nothing"))[:200]}
                         for n in ends if n not in dests}
    # Every endpoint, whether or not it has shown its destination: the
    # schedule is drawn over them, so one seed sends the same messages.
    senders = ends
    missing = sorted(set(senders) - set(dests))
    if missing:
        print("no destination from: %s" % " ".join(missing), flush=True)
    phase("identities")

    async def send_plan(warm_end):
        """The phases left, from where warm-up is expected to end: sent at
        the start and again whenever that moves."""
        phases = [("warm-up", warm_end)] if opts.warm_rounds > 0 else []
        if opts.traffic > 0:
            phases += [("traffic", warm_end + 1.0 + opts.traffic),
                       ("drain", warm_end + 1.0 + opts.traffic + opts.drain)]
        if phases:
            await sim.plan(*phases)

    rounds = opts.warm_rounds
    await send_plan(sim.run_s + rounds * opts.warm_spread
                    + max(0, rounds - 1) * opts.warm_gap + 2 * opts.settle_every
                    if rounds > 0 else sim.run_s)

    if opts.warm_rounds > 0:
        result["warm"] = {"rounds": [], "samples": []}
        for r in range(opts.warm_rounds):
            await sim.ask(ask_of(proto, proto.announce()), senders, spread=opts.warm_spread,
                          after=opts.warm_gap if r else 0.0)
            result["warm"]["rounds"].append([time.time(), sim.t])
            phase("warm_round_%d" % (r + 1))
        counted = list(senders)
        settle_from = sim.run_s
        prev = None
        while True:
            if not counted:
                await sim.sleep(2 * opts.settle_every)
                break
            reply = await sim.ask(ask_of(proto, proto.knowing()), counted, after=opts.settle_every)
            per = {n: len(v) for n, v in reply.items() if isinstance(v, list)}
            # Only the stations whose firmware can list what it knows are asked again.
            counted = sorted(per)
            if not counted:
                continue
            total = sum(per.values())
            result["warm"]["samples"].append({"t": sim.t, "wall": time.time(),
                                              "total": total, "per": per})
            print("  %s %d at run %.0f s" % (proto.known, total, sim.run_s), flush=True)
            dump()
            if prev is not None and total <= prev:
                break
            if sim.run_s - settle_from >= opts.settle_max:
                print("  settle-max reached, still rising", flush=True)
                break
            prev = total
            await send_plan(sim.run_s + opts.settle_every)   # one more sample at least
        phase("warm_settled")
    if opts.warm_snapshot:
        await sim.snapshot(opts.warm_snapshot)
        phase("warm_snapshot")

    if opts.traffic > 0:
        plan = schedule(senders, opts.seed, opts.traffic, opts.every, opts.marker,
                        opts.arrivals, opts.hub, opts.hub_share, opts.pairs, opts.sizes)
        start = sim.run_s + 1.0
        result["traffic_start_run_s"] = start
        await send_plan(start - 1.0)
        sends = result["sends"] = []

        async def one(n, at, src, dst, cls, text):
            rec = {"n": n, "marker": "%s%04d" % (opts.marker, n), "src": src,
                   "dst": dst, "cls": cls, "len": len(text), "at": at}
            sends.append(rec)
            if dst not in dests:
                rec["error"] = "recipient has no destination"
                return
            # The route and the send as one sequence, so both happen at the
            # T the message is due, with no round trip to here between them.
            # The `after` (never less than a millisecond) makes it a task of
            # simd's own: a message without one is handled inside the
            # socket's reader, which holds every later message behind it.
            try:
                results, t = await sim.sequence([ask_of(proto, proto.route(dst)),
                                                 ask_of(proto, proto.send(dst, text))], [src],
                                                after=max(0.001, start + at - sim.run_s))
            except SimError as err:
                # This message, not the run: the rest go on at their instants.
                rec["error"] = str(err)[:300]
                return
            path_out, out = (r.get(src) for r in results)
            rec["t_sent"] = rec["t_path"] = t if t is not None else sim.t
            rec["hops"] = proto.hops(path_out, dst)
            if path_out is not None and not isinstance(path_out, list):
                rec["path_reply"] = str(path_out)[:200]
            good = isinstance(out, str) and out and not out.startswith("! ")
            rec["mid"] = out if good else None
            if not good:
                rec["reply"] = str(out)[:300]

        phase("traffic_start")
        jobs = [asyncio.ensure_future(one(*p)) for p in plan]
        print("scheduled %d messages over %.0f s" % (len(plan), opts.traffic), flush=True)
        t_end = start + opts.traffic

        async def progress():
            # What has been sent so far, on the wall clock's beat: it asks
            # simd nothing, so it is no turn of ours.
            while True:
                await asyncio.sleep(5)
                dump()
        keeping = asyncio.ensure_future(progress())
        try:
            await sim.until(t_end)
            await asyncio.gather(*jobs)
        finally:
            keeping.cancel()
        phase("traffic_end")
        await sim.until(t_end + opts.drain)
        phase("drain_end")

    if opts.gather:
        reply = await sim.ask(meta("diagnostics"), stations)
        result["gathered"] = {"t": sim.t, "results": reply}
    phase("gathered")
    if opts.end_snapshot:
        await sim.snapshot(opts.end_snapshot)
        phase("end_snapshot")


# ---- the report ----------------------------------------------------------

def t_text(us):
    """T as `hh:mm:ss`, or `Nd + hh:mm` from a day on."""
    s = int((us or 0) // 1_000_000)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return "%dd + %02d:%02d" % (d, h, m) if d else "%02d:%02d:%02d" % (h, m, s)


def report(run_dir, traffic_path=None):
    """A traffic run's report, as Markdown: what ran, the phases, and the
    delivery counted from the run's own logs (`delivery.analyse_run`)."""
    from sim_mesh.reticulum import delivery

    traffic_path = traffic_path or os.path.join(run_dir, RESULT_FILE)
    name = os.path.basename(os.path.normpath(run_dir))
    drive = {}
    if os.path.isfile(traffic_path):
        with open(traffic_path, encoding="utf-8") as handle:
            drive = json.load(handle)
    proto = messaging.protocol(drive.get("category") or "reticulum")
    lines = ["# %s: %s" % (proto.title, name), ""]
    if not drive:
        return "\n".join(lines + ["No %s: the driver did not get as far as writing it."
                                  % os.path.basename(traffic_path), ""])
    try:
        drive, out, _ = delivery.analyse_run(traffic_path, run_dir, proto.status_event)
    except ValueError as err:
        out = None
        problem = str(err)
    dests = drive.get("dests") or {}
    ends = drive.get("endpoints") or sorted(set(drive.get("up") or {}) | set(dests))
    args = drive.get("args") or {}
    lines += ["| | |", "|---|---|",
              "| ended at | T %s |" % t_text(drive.get("t_end")),
              "| wall | %.0f s |" % ((drive.get("wall_end") or 0) - (drive.get("wall_start") or 0)),
              "| sends | one every %s s for %s s, seed %s |"
              % (args.get("every"), args.get("traffic"), args.get("seed")),
              "| between | %d stations%s |" % (len(ends), ", not those tagged %s" % args["skip_tag"]
                                               if args.get("skip_tag") else "")]
    if proto.category == "reticulum":
        lines.append("| LXMF identities | %d of %d |" % (len(dests), len(ends)))
    lines.append("")
    if not dests:
        lines += ["**No station showed an LXMF identity**, so nothing could be sent.", ""]
    no_dest = drive.get("no_dest") or {}
    if no_dest:
        lines += ["Stations with no LXMF identity send nothing and receive nothing. An "
                  "identity is made at first boot (`Node.reticulum.lxmf.create()`); a "
                  "firmware with no LXMF destination of its own, a transport-only node "
                  "such as microReticulum, has none to show, and takes part only by "
                  "forwarding among stations that do.", "",
                  "| station | firmware | its answer |", "|---|---|---|"]
        lines += ["| %s | %s | `%s` |" % (n, row.get("firmware") or "?", row.get("answer"))
                  for n, row in sorted(no_dest.items())]
        lines.append("")
    lines += ["## Phases", "", "| phase | T |", "|---|---|"]
    lines += ["| %s | %s |" % (p[0], t_text(p[2])) for p in drive.get("phases") or []]
    lines.append("")
    if out is None:
        lines += ["## Delivery", "", "Not counted: %s." % problem, ""]
        return "\n".join(lines)

    def table(title, figures):
        rows = ["| %s | delivered | proven to the sender |" % title, "|---|---|---|"]
        return rows + ["| %s | %s | %s |" % (k, v["delivered"], v["proven"])
                       for k, v in figures.items()] + [""]

    def latency(what, lat):
        return ["%s: median %.1f s, 90%% within %.1f s, longest %.1f s (%d messages)."
                % (what, lat["median"], lat["p90"], lat["max"], lat["n"]), ""] if lat else []

    lines += ["## Delivery", ""]
    if out["arrivals_reported"]:
        lines += ["**%s** delivered: arrived at the recipient, as its driver reported." % out["overall"],
                  "", "**%s** of those proven back to the sender, **%s** of all sent."
                  % (out["proven_of_delivered"], out["overall_proven"]), ""]
        if out["refused"]:
            lines += ["%d more arrived and were refused by the recipient (a full mailbox): "
                      "not delivered." % out["refused"], ""]
        if out["proven_not_seen_arriving"]:
            lines += ["%d proven to the sender were never reported arriving: the recipient's "
                      "driver missed them." % out["proven_not_seen_arriving"], ""]
    else:
        lines += ["**%s** proven to the sender. This firmware reports no arrivals, so what "
                  "arrived unproven is not known." % out["overall_proven"], ""]
    lines += table("route hops at send", out["by_route_hops"])
    lines += table("radio hops", out["by_radio_hops"])
    lines += table("size", out["by_class"])
    lines += latency("To arrival", out.get("latency_s"))
    lines += latency("To proof", out.get("proof_latency_s"))
    if out.get("undelivered_last_word"):
        lines += ["## Undelivered, by the last word on it", "", "| last line | messages |",
                  "|---|---|"]
        lines += ["| `%s` | %d |" % (w.replace("|", "\\|"), n)
                  for w, n in out["undelivered_last_word"][:10]]
        lines.append("")
    return "\n".join(lines)
