"""Delivery of a traffic run's messages, from what the stations' drivers reported.

Two things are counted for every message, from the run's `events.jsonl`:

- **delivered**: it arrived at its recipient, as the recipient's driver
  reports. `msg.received` for a MeshCore or Meshtastic message, carrying
  sim-mesh's id (sim_mesh.driver); `lxmf.message.received` for an LXMF one,
  carrying the LXMF message id, which the sender's driver ties to sim-mesh's
  id with `lxmf.message.id` (sim_mesh.reticulum.driver). An arrival the
  recipient did not take (`refused`, a full mailbox) is not a delivery and
  is counted apart.
- **proven**: the sender heard it arrived, a proof or an acknowledgement
  back across the mesh, as the sender's driver reports with `status:
  delivered`: `lxmf.message.status` for LXMF, `msg.status` for the others.

A proof is a packet as easily lost as the message, so proven is at most
delivered when both are reported. A firmware that reports no arrivals has
its messages counted by proof alone (`arrivals_reported` false).
`analyse` counts a traffic driver's sends by:

- route hops at send time: what the sender's `path` verb answered just
  before the send (`no path` where it held none);
- radio hops: the shortest path in the run's radio graph (the pairs the
  ether delivers at each sender's declared carrier, SF, bandwidth and
  power, by the run's loss tables) whose intermediate stations forward;
- size class;
- latency: from the instant the send was answered to its arrival, and to
  its proof.

An event's `t` is the run's T, in microseconds, when simd took it in.
Undelivered messages are tallied by the last status the sender's driver
reported for them.
"""

import collections
import json
import os
import re

EVENTS_FILE = "events.jsonl"
STATUS_EVENT = "lxmf.message.status"
ID_EVENT = "lxmf.message.id"
LXMF_RECEIVED = "lxmf.message.received"
MSG_RECEIVED = "msg.received"
ID_MATCH = 32           # hex digits of an LXMF message id two drivers are matched on


def events(run_dir):
    """Every event the run's drivers reported, in order."""
    path = os.path.join(run_dir, EVENTS_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    got = json.loads(line)
                except ValueError:
                    continue
                if isinstance(got, dict):
                    yield got
    except FileNotFoundError:
        return


def read_messages(run_dir, status_event=STATUS_EVENT):
    """(sender, mid) -> {'delivered': T in seconds or None, 'last': its
    last reported status, with why when there is one}."""
    out = {}
    for ev in events(run_dir):
        if ev.get("event") != status_event or not ev.get("mid"):
            continue
        rec = out.setdefault((ev.get("node"), str(ev["mid"])), {"delivered": None, "last": None})
        status = str(ev.get("status") or "")
        rec["last"] = status + (": %s" % ev["why"] if ev.get("why") else "")
        if status == "delivered" and rec["delivered"] is None:
            rec["delivered"] = (ev.get("t") or 0) / 1e6
    return out


def read_arrivals(run_dir):
    """What reached its recipient: (sender, mid) -> {'t': T in seconds of
    the first arrival it took, or None; 'refused': why it was turned away,
    when it was and never taken}, or None when no driver of the run
    reported an arrival at all."""
    ids, by_id, by_mid, any_arrival = {}, {}, {}, False
    for ev in events(run_dir):
        kind = ev.get("event")
        t = (ev.get("t") or 0) / 1e6
        if kind == ID_EVENT and ev.get("mid") and ev.get("message_id"):
            ids[(ev.get("node"), str(ev["mid"]))] = str(ev["message_id"])[:ID_MATCH].lower()
        elif kind == LXMF_RECEIVED and ev.get("message_id"):
            any_arrival = True
            key = (ev.get("node"), str(ev["message_id"])[:ID_MATCH].lower())
            _arrive(by_id.setdefault(key, {"t": None, "refused": None}), t, ev.get("refused"))
        elif kind == MSG_RECEIVED and ev.get("mid"):
            any_arrival = True
            _arrive(by_mid.setdefault((ev.get("node"), str(ev["mid"])), {"t": None, "refused": None}),
                    t, ev.get("refused"))
    if not any_arrival:
        return None
    return {"ids": ids, "by_id": by_id, "by_mid": by_mid}


def _arrive(rec, t, refused):
    if refused:
        rec["refused"] = rec["refused"] or str(refused)
    elif rec["t"] is None:
        rec["t"] = t


def arrival_of(arrivals, send):
    """A send's arrival record at its recipient, or None."""
    if arrivals is None:
        return None
    got = arrivals["by_mid"].get((send["dst"], str(send.get("mid"))))
    if got is None:
        message_id = arrivals["ids"].get((send["src"], str(send.get("mid"))))
        if message_id:
            got = arrivals["by_id"].get((send["dst"], message_id))
    return got


def event_deliveries(run_dir):
    """Each sender's delivered instants, in seconds of T: name -> [s, ...]."""
    out = {}
    for (name, _), rec in read_messages(run_dir).items():
        if rec["delivered"] is not None:
            out.setdefault(name, []).append(rec["delivered"])
    return {name: sorted(times) for name, times in out.items()}


def radio_hops(adj, forwarders, src, dst):
    """Fewest hops from src to dst over `adj` (name -> names it reaches),
    passing only through `forwarders`; None when there is no such path."""
    frontier, seen, d = {src}, {src}, 0
    while frontier:
        d += 1
        nxt = set()
        for n in frontier:
            for m in adj.get(n, ()):
                if m == dst:
                    return d
                if m not in seen and m in forwarders:
                    seen.add(m)
                    nxt.add(m)
        frontier = nxt
    return None


def pct(b):
    return "%d/%d (%.1f%%)" % (b[0], b[1], 100.0 * b[0] / b[1]) if b[1] else "0/0"


def quantiles(v):
    v = sorted(v)
    if not v:
        return None
    q = lambda f: v[min(len(v) - 1, int(f * (len(v) - 1) + 0.5))]
    return {"n": len(v), "min": v[0], "p25": q(.25), "median": q(.5), "p75": q(.75),
            "p90": q(.9), "max": v[-1], "mean": sum(v) / len(v)}


def analyse(drive, logs, adj, forwarders, arrivals=None):
    """The figures, and one row per message.

    `drive` is the traffic driver's result, `logs` what `read_messages`
    found, `adj` the radio graph by name, `forwarders` the names that
    forward, `arrivals` what `read_arrivals` found (None: no arrival was
    reported, and delivered is counted by proof).
    """
    # Each bucket: [delivered, proven, sent].
    bucket = lambda: [0, 0, 0]
    total = bucket()
    by_route = collections.defaultdict(bucket)
    by_radio = collections.defaultdict(bucket)
    by_cls = collections.defaultdict(bucket)
    lat_arrive, lat_proof = [], []
    last_words = collections.Counter()
    no_mid = refused = proven_unseen = both_ways = 0
    rows = []
    for s in drive.get("sends", []):
        if not s.get("mid"):
            no_mid += 1
        rec = logs.get((s["src"], s.get("mid")), {}) if s.get("mid") else {}
        t_sent = (s.get("t_sent") or 0) / 1e6
        proven = rec.get("delivered") is not None
        got = arrival_of(arrivals, s) if s.get("mid") else None
        if arrivals is None:
            delivered = proven
        else:
            delivered = bool(got and got["t"] is not None)
            if got and got["t"] is None and got["refused"]:
                refused += 1
            if proven and not delivered:
                proven_unseen += 1
        both_ways += delivered and proven
        rh = s.get("hops")
        route = "no path" if rh is None else (str(rh) if rh <= 6 else "7+")
        gh = radio_hops(adj, forwarders, s["src"], s["dst"])
        radio = "none" if gh is None else (str(gh) if gh <= 6 else "7+")
        for b in (total, by_route[route], by_radio[radio], by_cls[s["cls"]]):
            b[0] += delivered
            b[1] += proven
            b[2] += 1
        if delivered and got:
            lat_arrive.append(got["t"] - t_sent)
        if proven:
            lat_proof.append(rec["delivered"] - t_sent)
        if not delivered:
            w = rec.get("last") or ("no mid" if not s.get("mid") else "nothing reported")
            if got and got["refused"]:
                w = "arrived, refused: %s" % got["refused"]
            w = re.sub(r"\b(o_\S+|[0-9a-f]{8,}|lxmf\.id\d\.\S+)", "…", w)
            w = re.sub(r"\d+", "N", w)
            last_words[w] += 1
        rows.append({"marker": s["marker"], "src": s["src"], "dst": s["dst"], "cls": s["cls"],
                     "route_hops": rh, "radio_hops": gh, "delivered": delivered, "proven": proven,
                     "latency_s": (got["t"] - t_sent) if delivered and got else None,
                     "proof_latency_s": (rec["delivered"] - t_sent) if proven else None})
    order = lambda k: (k in ("no path", "none"), k == "7+", k)

    def both(b):
        return {"delivered": pct((b[0], b[2])), "proven": pct((b[1], b[2]))}
    sizes = (drive.get("args") or {}).get("sizes") or ("short", "two", "big")
    out = {"sent": total[2], "delivered": total[0], "proven": total[1], "no_mid": no_mid,
           "arrivals_reported": arrivals is not None,
           "refused": refused, "proven_not_seen_arriving": proven_unseen,
           "overall": pct((total[0], total[2])),
           "overall_proven": pct((total[1], total[2])),
           "proven_of_delivered": pct((both_ways, total[0])),
           "by_route_hops": {k: both(by_route[k]) for k in sorted(by_route, key=order)},
           "by_radio_hops": {k: both(by_radio[k]) for k in sorted(by_radio, key=order)},
           "by_class": {k: both(by_cls[k]) for k in sizes if k in by_cls},
           "latency_s": quantiles(lat_arrive),
           "proof_latency_s": quantiles(lat_proof),
           "undelivered_last_word": last_words.most_common(25)}
    return out, rows


def analyse_run(traffic_path, run_dir, status_event=STATUS_EVENT):
    """A traffic run's figures: (the driver's result, figures, rows), counted
    against the run's own events (`status_event`, the sender's driver's word
    on each message; the recipients' word on what arrived) and radio graph."""
    from sim_mesh.view import RunView

    with open(traffic_path, encoding="utf-8") as handle:
        drive = json.load(handle)
    view = RunView(run_dir)
    logs = read_messages(view.dir, status_event)
    arrivals = read_arrivals(view.dir)
    graph = view.radio_graph(view.calling_hz())
    adj = {view.names[a]: {view.names[b] for b in bs} for a, bs in graph.items()}
    forwarders = {view.names[s] for s in view.forwarders()}
    out, rows = analyse(drive, logs, adj, forwarders, arrivals)
    return drive, out, rows
