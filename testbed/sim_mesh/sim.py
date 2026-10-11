"""The hold on one running simulation that the script library runs on
(sim_mesh.library, which a script uses; this is async, on the library's own
loop): the stations, the clock, and what to do with them.

```
library ── ws /ws?sim=<name>&quiet=1 ────────────────────► front ─► that simulation's simd
simd ── snapshot {nodes, clock, …} ─────────────────────► library   the stations, T
library ── meta {verb: announce, names: [..], stagger: 300, id: 7} ─► simd
simd ── command_result {id: 7, results: {name: reply}, t} ─► library
library ── firmware | first_boot {rules, id} ─────────────► simd
```

**Time** is the run's own: `sim.run_s` is seconds since the hold began
(or since the load it asked for), and `until`, `after` and `spread` are
seconds of it, so a real-time and a virtual-time run act at the same
instants of the run.

**Turns.** In a virtual-time run a driver and the run take turns with T:
the driver says `drive` when it attaches, and simd holds T while the driver
has the floor, from each answer it gives the driver until the driver says
`yield`, which it does once it has nothing left to do but wait on simd (its
loop has nothing to run, and `may_yield()` agrees: a script's thread is
waiting on a call). So what a driver does about an answer, it does at the
answer's T, and its waits (`until`, `sleep`, `all_up`, `ready`) are simd's,
ending at an instant of the run. `sim.t` is the T of the last answer.

```
driver ── wait {id} ──► simd                         turn n: the driver yields
driver ── yield {turn: n} ──► simd                   (answer n+1 already on its way)
simd   ── command_result {id} ──► driver             the floor again: turn n+1
simd   ignores yield {turn: n}: it is behind n+1     T stands for the driver
driver ── yield {turn: n+1} ──► simd                 once it has acted on the answer
```

A yield names the turn it gives back, the floors counted from `drive`, one
per answer; simd lets go only of the turn it is on. A yield that crossed an
answer on the wire would otherwise give back the floor that answer handed
over, and T would run while the driver acted on it, for however long the
host took.

**A selection** is some of the stations: `sim.nodes(tag=, firmware=, base=,
category=, role=, names=)`, or `sim.node(name)` for one. What is done to a
selection is done to each member that is up, spread over `spread` seconds and
held back `after` seconds, and answers {name: reply}:

- `run(line, base=)`: a line in the stations' own language, macros filled in;
  a selection running more than one base refuses it unless `base` narrows it;
- `verb(verb, category=None, **args)`: a driver's verb, which each station's
  driver does its own way (`sim_mesh.driver`, `sim_mesh.reticulum.driver`);
  with a category, only on its stations of that category. A firmware that
  cannot do one answers `! …` for that station;
- `reset()`, `factory_reset()`: pressed on each.
- `power(on)`: each one's power off (its station stopped until it is
  powered on again, its state kept) or on (a stopped one boots from it).

`attach(name)` holds a running simulation; `start(geodata, nodesets, …)`
asks the front for a new one, with its time and its firmware and first-boot
rules, and holds it; `resume(name)` starts a paused one again and holds it.
A script uses all of this through `sim_mesh.library`.
"""

import asyncio
import collections
import itertools
import json
import os
import random
import time

import aiohttp


def front_address(environ=os.environ):
    """The front's host and port: `SIM_MESH_FRONT`'s `host:port`, a front
    running elsewhere (from a container, `host.docker.internal:8800`), else
    this machine's on `SIM_MESH_PORT` (which the front gives the scripts it
    runs) or 8800."""
    given = environ.get("SIM_MESH_FRONT")
    if given:
        host, _, port = given.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("SIM_MESH_FRONT is host:port, not %r" % given)
        return host, int(port)
    return "127.0.0.1", int(environ.get("SIM_MESH_PORT") or 8800)


FRONT_HOST, DEFAULT_PORT = front_address()


def front_ws(port, query=""):
    """The front's control websocket at `port`."""
    return "http://%s:%d/ws%s" % (FRONT_HOST, port, query)


class SimError(Exception):
    """The front or the simulation refused, or the simulation went away."""


class Station:
    """What the driver knows of one station, from simd's `node` messages."""

    def __init__(self, msg):
        self.update(msg)

    def update(self, msg):
        self.name = msg["name"]
        self.id = msg.get("id")
        self.tags = list(msg.get("tags") or ())
        self.firmware = msg.get("firmware")
        self.base = msg.get("base")
        self.category = msg.get("category")
        # What it reports, else its role tag (nodeset.tag_role).
        self.role = msg.get("role") or next(
            (t for t in self.tags if t in ("transport", "router", "repeater")), None)
        self.lat, self.lon = msg.get("lat"), msg.get("lon")
        self.height_m = msg.get("height_m")
        self.antenna = (msg.get("antenna") or {}).get("type")
        self.max_dbm = msg.get("max_dbm")
        self.status = msg.get("status")

    def facts(self):
        """What a selection (sim_mesh.select) can ask of it."""
        return {"name": self.name, "id": self.id, "tags": self.tags, "firmware": self.firmware,
                "base": self.base, "category": self.category, "role": self.role,
                "antenna": self.antenna, "max_dbm": self.max_dbm, "lat": self.lat,
                "lon": self.lon, "height_m": self.height_m, "status": self.status}

    def __repr__(self):
        return "<station %s #%s %s %s>" % (self.name, self.id, self.base, self.status)


class Selection:
    """Some of the stations, by name, and what can be done to all of them."""

    def __init__(self, sim, names):
        self.sim = sim
        self.names = list(names)

    def __iter__(self):
        return (self.sim.stations[n] for n in self.names if n in self.sim.stations)

    def __len__(self):
        return len(self.names)

    def __repr__(self):
        return "<selection of %d: %s>" % (len(self.names), " ".join(self.names[:8]))

    async def run(self, line, spread=0.0, after=0.0, base=None):
        return await self.sim.ask({"type": "command", "line": line}, self.names,
                                  spread, after, base)

    async def verb(self, verb, category=None, spread=0.0, after=0.0, **args):
        msg = {"type": "meta", "verb": verb, "args": args}
        if category is not None:
            msg["category"] = category
        return await self.sim.ask(msg, self.names, spread, after)

    async def reset(self, after=0.0):
        for name in self.names:
            await self.sim.send(_later({"type": "node_reset", "name": name}, after))

    async def factory_reset(self, after=0.0):
        for name in self.names:
            await self.sim.send(_later({"type": "node_factory_reset", "name": name}, after))

    async def power(self, on, after=0.0):
        for name in self.names:
            await self.sim.send(_later({"type": "node_start" if on else "node_stop",
                                        "name": name}, after))


def _later(msg, after):
    """`msg`, to be acted on `after` seconds of the run's clock from now."""
    return dict(msg, after=float(after)) if after else msg


class Sim:
    """One simulation, over one quiet websocket through the front."""

    def __init__(self, ws, session, name, port):
        self.ws = ws
        self.session = session
        self.name = name
        self.port = port
        self.stations = {}
        self.run = {}                   # the run as simd describes it: dir, geodata, nodeset, …
        self.t = 0
        self.t_zero = None
        self.up = {}
        self.loaded = asyncio.get_running_loop().create_future()
        self.waiting = {}
        # Every page and driver hears every answer, so an id is this driver's own.
        prefix = os.urandom(4).hex()
        self.ids = ("%s-%d" % (prefix, n) for n in itertools.count(1))
        self.rng = random.Random()
        self.result = {"clock": []}      # what a driver that keeps a record reads
        self.errors = collections.deque(maxlen=50)
        self.reader = None
        self.firmware_tasks = []         # firmware said and not yet awaited
        self.has_floor = False           # simd holds T for us until we yield
        self.turn = 0                    # the floors simd has given us: `drive`, and each answer
        self.may_yield = lambda: True    # a script's runtime: its thread is waiting on a call
        self._settling = False
        self._turns = 0
        self.gone = False                # the simulation stopped, or the socket closed
        self.script_loglevel = "output"  # the simulation's, unless a script says its own
        self.trace = None                # a script's log of what is sent and answered

    # ---- the stream ------------------------------------------------------

    async def read(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            kind = m.get("type")
            if kind == "sims" and "sim" not in m:
                # The front's registry, its own and not the simulation's: the
                # socket to the front outlives a simulation stopped or paused
                # under it, and this row is how the driver learns it has gone.
                if not any(r.get("name") == self.name and r.get("state") == "running"
                           for r in m.get("sims") or ()):
                    break
            elif kind == "script_loglevel":
                self.script_loglevel = m.get("level") or self.script_loglevel
            elif kind == "snapshot":
                self.run = m.get("run") or {}
                self.script_loglevel = m.get("script_loglevel") or self.script_loglevel
                self.stations = {n["name"]: Station(n) for n in m.get("nodes") or ()}
                if m.get("clock"):
                    self.t = m["clock"]["t"]
                if m.get("run") and not self.loaded.done():
                    self.t_zero = self.t
                    self.loaded.set_result(m)
            elif kind == "clock":
                # A report on the wall clock's beat: kept, not taken for now.
                self.result["clock"].append([round(time.time(), 3), m["t"], m.get("barriers"),
                                             m.get("observed"), m.get("slow_idles")])
            elif kind == "node":
                if m["name"] in self.stations:
                    self.stations[m["name"]].update(m)
                else:
                    self.stations[m["name"]] = Station(m)
                if m.get("status") == "up" and self.t_zero is not None:
                    # The T the station came up at, as the message says it;
                    # this socket's own `t` is only its last answer's.
                    t = m.get("t")
                    self.up.setdefault(m["name"], [time.time(), self.t if t is None else t])
            elif kind == "node_gone":
                self.stations.pop(m.get("name"), None)
            elif kind == "command_result":
                if self.trace is not None and m.get("id") in self.waiting:
                    self.trace("debug", "answered %s" % msg.data)
                waiter = self.waiting.pop(m.get("id"), None)
                if waiter is not None:
                    # Ours: simd gave us the floor with it, at its T.
                    if isinstance(m.get("t"), int):
                        self.t = max(self.t, m["t"])
                    self.turn += 1
                    self.has_floor = True
                    self.poke()
                    if not waiter.done():
                        if m.get("error"):
                            # Refused, or failed on the way: the request fails.
                            waiter.set_exception(SimError(m["error"]))
                        else:
                            waiter.set_result(m)
            elif kind == "error":
                self.errors.append(m.get("text"))
                print("simd: %s" % m.get("text"), flush=True)
        self.gone = True
        for waiter in self.waiting.values():
            if not waiter.done():
                waiter.set_exception(self.went_away())

    def went_away(self):
        return SimError("simulation %s went away" % self.name)

    def check(self):
        """A SimError once the simulation has gone: what every wait on it
        asks before it waits again, so a driver never waits on a run that
        will not move."""
        if self.gone:
            raise self.went_away()

    def expect(self):
        """A fresh id and the future its answer lands in."""
        self.check()
        ident = next(self.ids)
        waiter = asyncio.get_running_loop().create_future()
        self.waiting[ident] = waiter
        return ident, waiter

    async def send(self, msg):
        text = json.dumps(msg)
        if self.trace is not None:
            self.trace("debug", "sent %s" % text)
        await self.ws.send_str(text)

    # ---- turns -------------------------------------------------------------

    async def drive(self):
        """Take turns with T (see *Turns*): we have the floor from here."""
        await self.send({"type": "drive"})
        self.turn += 1
        self.has_floor = True
        self.poke()

    def poke(self):
        """Look, once the loop has settled, whether to yield."""
        if not self._settling:
            self._settling = True
            self._turns = 0
            asyncio.get_running_loop().call_soon(self._settle)

    def _settle(self):
        loop = asyncio.get_running_loop()
        ready = getattr(loop, "_ready", None)
        if ready and self._turns < 1000:
            self._turns += 1
            loop.call_soon(self._settle)
            return
        self._settling = False
        if self.has_floor and self.may_yield():
            self.has_floor = False
            asyncio.ensure_future(self.send({"type": "yield", "turn": self.turn}))

    async def _wait(self, msg):
        """One of simd's waits: done at the instant it ends."""
        ident, waiter = self.expect()
        await self.send(dict(msg, id=ident))
        return await waiter

    @property
    def run_dir(self):
        """The run's directory, where a driver's own output belongs."""
        return self.run.get("dir")

    # ---- time ------------------------------------------------------------

    @property
    def run_s(self):
        """Seconds of the run's clock since this driver attached."""
        return (self.t - (self.t_zero or 0)) / 1e6

    async def until(self, run_s):
        """Until the run's clock reaches `run_s` seconds after attaching."""
        target = (self.t_zero or 0) + int(round(run_s * 1e6))
        if self.t < target:
            await self._wait({"type": "wait", "until": target})

    async def sleep(self, seconds):
        await self.until(self.run_s + seconds)

    async def all_up(self):
        """Until every station that has firmware is up."""
        await self._wait({"type": "wait", "for": "up"})

    async def plan(self, *phases):
        """What is left of the run: (name, until) pairs, until in seconds of
        run time after attaching. The page turns it into a phase, its
        progress, and a finish time."""
        await self.send({"type": "plan", "phases": [
            {"name": name, "until": (self.t_zero or 0) + int(until * 1e6)}
            for name, until in phases]})

    # ---- choosing --------------------------------------------------------

    def nodes(self, tag=None, firmware=None, base=None, category=None, role=None, names=None):
        """The stations with every one of these that is given, in id order."""
        chosen = []
        for s in sorted(self.stations.values(), key=lambda s: s.id or 0):
            if tag is not None and tag not in s.tags:
                continue
            if firmware is not None and s.firmware != firmware:
                continue
            if base is not None and s.base != base:
                continue
            if category is not None and s.category != category:
                continue
            if role is not None and s.role != role:
                continue
            if names is not None and s.name not in names:
                continue
            chosen.append(s.name)
        return Selection(self, chosen)

    def node(self, name):
        if name not in self.stations:
            raise SimError("no station called %s" % name)
        return Selection(self, [name])

    def facts(self):
        """Every station's facts, for a selection (sim_mesh.select): {name: facts}."""
        return {name: s.facts() for name, s in self.stations.items()}

    # ---- firmware --------------------------------------------------------

    def firmware(self, rules):
        """Firmware rules said to the simulation, [{which, firmware}]: a task
        to await, which everything that needs a station up awaits first."""
        task = asyncio.ensure_future(self._firmware(rules))
        self.firmware_tasks.append(task)
        return task

    async def _firmware(self, rules):
        return await self._rules("firmware", rules)

    async def first_boot(self, rules):
        """First-boot rules said to the simulation, [{which, lines}]: kept
        with the run, for every station that boots with no state from now on."""
        return await self._rules("first_boot", rules)

    async def _rules(self, verb, rules):
        ident, waiter = self.expect()
        await self.send({"type": verb, "rules": list(rules), "id": ident})
        reply = await waiter
        if reply.get("error"):
            raise SimError(reply["error"])
        return reply.get("results") or {}

    async def settled(self):
        """Once any firmware said is in."""
        if self.firmware_tasks:
            tasks, self.firmware_tasks = self.firmware_tasks, []
            await asyncio.gather(*tasks)

    async def ready(self, selection, timeout=None):
        """The names of a selection's stations once every one is up, after
        any firmware said is in. A station that runs nothing is refused: it
        needs firmware first."""
        await self.settled()
        names = selection.pick(self.facts())
        bare = [n for n in names if not self.stations[n].firmware]
        if bare:
            raise SimError("%s run%s no firmware: say .firmware(…) for %s first"
                           % (", ".join(bare), "s" if len(bare) == 1 else "",
                              "it" if len(bare) == 1 else "them"))
        try:
            await asyncio.wait_for(self._wait({"type": "wait", "for": "up",
                                               "names": [n for n in names if n in self.stations]}),
                                   timeout)
        except asyncio.TimeoutError:
            raise SimError("not up after %.0f s: %s" % (timeout, ", ".join(
                n for n in names if self.stations[n].status != "up"))) from None
        return [n for n in names if n in self.stations]

    def pairs(self, selection=None, sample=None, seed=None):
        """Ordered pairs of distinct stations (of `selection`, else all), each
        once; `sample` of them drawn with `seed`, in draw order."""
        names = list((selection or self.nodes()).names)
        every = [(a, b) for a in names for b in names if a != b]
        if sample is None or sample >= len(every):
            return every
        return random.Random(seed).sample(every, sample)

    # ---- asking ----------------------------------------------------------

    async def ask(self, msg, names, spread=0.0, after=0.0, base=None):
        """One command or verb on the named stations: {name: reply}."""
        ident, waiter = self.expect()
        msg = dict(msg, id=ident, names=list(names))
        if spread:
            msg["stagger"] = float(spread)
        if after:
            msg["after"] = float(after)
        if base:
            msg["base"] = base
        await self.send(msg)
        reply = await waiter
        if reply.get("error"):
            raise SimError(reply["error"])
        return reply["results"]

    async def sequence(self, steps, names, after=0.0):
        """Several commands and intents on the named stations, one after the
        other at one T inside simd: ([{name: reply}], one per step; the T in
        µs they were done at). Each step is a `command` ({type, line}) or a
        `meta` ({type, verb, args})."""
        ident, waiter = self.expect()
        msg = {"type": "sequence", "steps": list(steps), "id": ident, "names": list(names)}
        if after:
            msg["after"] = float(after)
        await self.send(msg)
        reply = await waiter
        return reply["results"], reply.get("t")

    async def command(self, line, name=None, after=0.0, stagger=0.0, base=None):
        """One line on one station, or on every station running `base`: the
        whole `command_result`, with its `t`."""
        ident, waiter = self.expect()
        msg = {"type": "command", "line": line, "id": ident}
        for key, value in (("name", name), ("base", base)):
            if value:
                msg[key] = value
        if after > 0:
            msg["after"] = after
        if stagger > 0:
            msg["stagger"] = stagger
        await self.send(msg)
        return await waiter

    async def snapshot(self, name, after=0.0):
        """This moment, flushed, as a snapshot."""
        await self.send(_later({"type": "snapshot_save_as", "name": name}, after))

    save_snapshot = snapshot

    async def move(self, name, lat, lon, height_m=None, after=0.0):
        msg = {"type": "nodeset_move", "name": name, "lat": lat, "lon": lon, "settle": True}
        if height_m is not None:
            msg["height_m"] = height_m
        await self.send(_later(msg, after))

    def wait_run(self, at):
        return self.until(at)

    async def stop_all(self):
        """Every station stopped, the simulation kept (simd's stop_all). In a
        virtual run they leave T together at the next barrier, so the run's
        record ends at this T, the same in every run."""
        await self._wait({"type": "stop_all"})

    async def stop(self):
        """Stop the simulation itself, through the front."""
        await front_verb(self.session, self.port, {"type": "sim_stop", "name": self.name})

    async def pause(self):
        """Stop the simulation with its state kept, through the front: it
        stays listed as done, the script's pause, to be resumed as it ended."""
        await front_verb(self.session, self.port,
                         {"type": "sim_pause", "name": self.name, "by": "script"})

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
        await self.ws.close()


async def front_verb(session, port, msg, timeout=None):
    """One of the front's own verbs on a socket of its own: its answer."""
    async with session.ws_connect(front_ws(port), max_msg_size=0) as ws:
        await ws.send_str(json.dumps(msg))
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            reply = json.loads(raw.data)
            if reply.get("type") == msg["type"] and "ok" in reply and reply.get("sim") is None:
                if not reply["ok"]:
                    raise SimError(reply.get("error") or "%s refused" % msg["type"])
                return reply
    raise SimError("the front went away")


async def attach(name=None, port=None, session=None):
    """Hold a running simulation: `name`, else `$SIM_MESH_SIM`."""
    name = name or os.environ.get("SIM_MESH_SIM")
    if not name:
        raise SimError("attach to which simulation? name one, or run under the front")
    port = port or DEFAULT_PORT
    session = session or aiohttp.ClientSession()
    ws = await session.ws_connect(front_ws(port, "?quiet=1&sim=%s" % name),
                                  max_msg_size=0)
    sim = Sim(ws, session, name, port)
    sim.reader = asyncio.ensure_future(sim.read())
    await asyncio.wait_for(asyncio.shield(sim.loaded), 60)
    await sim.drive()
    return sim


async def resume(name, time="real", port=None, session=None):
    """A paused simulation started again as it ended, in `time`, and held."""
    port = port or DEFAULT_PORT
    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        reply = await front_verb(session, port, {"type": "sim_resume", "name": name,
                                                 "time": time})
        return await attach(reply["name"], port, session)
    except BaseException:
        if own:
            await session.close()
        raise


async def start(geodata, nodesets, script=None, time="real", name=None, build=None,
                firmware_rules=None, first_boot_rules=None, port=None, session=None,
                inputs=None, build_tag=None):
    """A new simulation from geodata and one or more nodesets (several are
    merged), its firmware and first-boot rules given from the start, started
    by the front and held. `script` is the name of the script it keeps a
    copy of, `inputs` the values its inputs were given, which the run keeps.
    `build` is a firmware every node given one runs instead, or with
    `build_tag` every such node carrying that tag."""
    port = port or DEFAULT_PORT
    own = session is None
    session = session or aiohttp.ClientSession()
    layers = [nodesets] if isinstance(nodesets, str) else list(nodesets)
    msg = {"type": "sim_new", "geodata": geodata, "nodesets": layers, "time": time,
           "firmware_rules": list(firmware_rules or []),
           "first_boot_rules": list(first_boot_rules or [])}
    for key, value in (("script", script), ("name", name), ("build", build),
                       ("build_tag", build_tag), ("inputs", inputs)):
        if value:
            msg[key] = value
    try:
        reply = await front_verb(session, port, msg)
        return await attach(reply["name"], port, session)
    except BaseException:
        if own:
            await session.close()
        raise
