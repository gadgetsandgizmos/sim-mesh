"""What a script says to a simulation and its nodes, whatever firmware they run.

    '''smoke: an LXMF identity everywhere, then announces and a message.'''
    from sim_mesh import *

    firmware = script_input("firmware", type=Firmware, category="reticulum",
                            label="Firmware for nodes not otherwise configured")
    sim_speed("real")
    nodes().firmware(firmware)
    script_include("scripts/startup.py")
    nodes().on_first_boot(Node.reticulum.lxmf.create())

    nodes().up()
    nodes(tag="lora").reticulum.lxmf.announce(spread=60)
    sim_wait(600)
    node("gw02").reticulum.lxmf.send("internet", "hello")

A script is plain Python, run from its top to its end (sim_mesh.runner):
every call below does what it says and returns when it has, so a script is
read as it runs. Time is the run's own clock, so a real-time and a
virtual-time run act at the same instants of the run.

**The script**:

- `script_input(name, type=str, label=None, default=None, category=None)`:
  something the script asks for before it runs, shown above it on its page
  and given from a shell as `--set name=value`; returns its value. `type` is
  `int`, `float`, `str`, `bool`, `Firmware` (an installed firmware's name,
  or `<base>_latest`, offered from those of `category`) or `Run` (a run's
  name). Written with literal arguments, at the script's top level, so the
  page can read it without running the script; the run keeps the values.
- `script_include(path, missing_ok=False)`: another file's code run here, in
  a namespace of its own, every time it is included; `path` is under
  testbed/ (`scripts/startup.py`, `nodesets/mitte7.py`).
- `script_results(futures)`: what commands given `wait=False` came to.
- `script_loglevel(level)`: how much this script writes to the run's
  `scripts.log`, over the simulation's level: `"output"` (what it prints,
  its errors), `"commands"` (and every command with its answers) or
  `"debug"` (and what goes to and from the simulation).

**The simulation**:

- `sim_speed(speed)`: `"real"`, `"max"` (virtual time as fast as the
  stations allow) or a number, virtual time paced at that many times the
  wall. Real by default. Said before anything is done; it is the
  simulation's for its whole life.
- `sim_nodesets()`: the names of the nodesets the world is made of, for
  including each one's own setup (`nodesets/<name>.py`).
- `sim_now()`, `sim_wait(seconds)`, `sim_until(seconds)`: the run's clock,
  in seconds since the script's simulation began.
- `sim_phase((name, until), …)`: what the run is doing, for the page's
  estimate.
- `sim_snapshot(name)`, `sim_pause()`, `sim_stop()`.
- `sim_script_loglevel(level)`: the scripts' log level on this simulation,
  for every script on it that says none of its own.

**Nodes**: `nodes(field=value, …)` is a selection (sim_mesh.select),
combined with `&`, `|`, `-`, `~`; `node(name)` is a selection of one. What
is done to a selection is done to each of its nodes (each waits for them
to be up first; a node with no firmware refuses):

- `.facts()`: each node's facts, {name: {id, tags, firmware, base,
  category, role, status, max_dbm, …}}.
- `.firmware(name)`: what they run, an installed firmware by name
  (`<base>_<arch>_<version>`) or the newest of a base (`<base>_latest`).
  Each node runs what the last firmware said for it names; a node none is
  said for runs nothing.
- `.on_first_boot(*rules)`: what each is given the first time it boots with
  no state (every station of a new simulation, a node placed later, a
  station after a factory reset), after its name, in order: lines in its
  own language (a string of one or more lines), and commands said on the
  class `Node` instead of a selection, which are rules rather than done
  (`Node.radio(sf=9)`, `Node.radio_up()`, `Node.reticulum.role("transport")`).
- `.up()`: until they are all up; their names.
- `.exec(lines, pause=0)`: lines in each node's own language, macros filled
  in (`{name}`, `{id}`, `{addr}`, `{addr:<node>}`, `{max_dbm}`), the nodes
  together or one after another `pause` seconds apart: {node: what it
  printed}.
- `.reset()`: reset pressed, its state kept; `.factory_reset()`: its state
  wiped, and set up again as on its first boot.
- `.move(lat, lon, height_m=None)`: one node to another place; its links are
  recomputed.
- `.radio(freq_mhz, sf, bw_khz, cr, tx_dbm, sync, preamble)`: slot 0's
  settings, those given (`tx_dbm="max"` is each node's own maximum);
  `.radio_up()`: the radio started, after whatever it reads when it starts.
- `.reticulum.role("transport" | "client")`
- `.reticulum.path(to=None, dest_hash=None, iface=None)`: each node's path
  table, [{dest, next_hop, iface, hops}]: to a node or LXMF identity (`to`),
  a destination (`dest_hash`), on an interface (`iface`), or all of it.
- `.reticulum.lxmf.create(name=None)`: an LXMF identity, named after the node
  unless `name` says otherwise, unless the node has one by that name. The
  name is the identity's in the run, which no other identity and no other
  node may have.
- `.reticulum.lxmf.identities()`: each node's [(name, address)], the one it
  sends from first.
- `.reticulum.lxmf.announce(name=None)`: an announce of that identity's
  delivery destination (none named: the first).
- `.reticulum.lxmf.send(to, text, sender=None)`: an LXMF message to a node or
  an LXMF identity, from the node's identity `sender` (none named: the
  first): {node: the message's id}.
- `.meshcore.repeat(on)`: forwarding others' packets on or off.
- `.meshcore.advert()`, `.meshcore.floodadv()`: a zero-hop or a flooded
  advert.
- `.meshcore.contacts()`: each node's [(name, public-key prefix, path length
  or None for flood)].
- `.meshcore.msg(to, text)`: a direct message to the contact `to`, a node's
  name once it has advertised it: {node: the message's id}.
- `.meshcore.chan(nb, text)`: a message on channel `nb`: {node: the
  message's id}.
- `.meshcore.path(to)`: each node's path to the contact `to`, its hops, or
  None for flood; `.meshcore.reset_path(to)`: back to flood.
- `.meshtastic.role(role)`: its device role, Meshtastic's in lower case
  (`client`, `router`, `router_late`, …).
- `.meshtastic.hop_limit(n)`: the hops a packet it originates may take, 0–7.
- `.meshtastic.sendtext(text, to=None, ch_index=0, want_ack=True)`: a text
  message to the node `to` (its name), or on channel `ch_index` when `to` is
  None: {node: the message's id}.
- `.meshtastic.traceroute(to)`: each node's route to `to` and back, {route,
  snr_towards, route_back, snr_back}.
- `.meshtastic.nodes()`: each node's [(name, id, hops away, snr, last
  heard)]; `.meshtastic.nodeinfo()`: a NodeInfo broadcast now.

A command under `.reticulum`, `.meshcore` or `.meshtastic` is for a selection's nodes of
that category and nothing to the others; a selection with none of them
refuses it. Every
command that acts takes `after=` (seconds on the run's clock before it is
done) and `wait=False`, which returns at once with a future whose `result()`
is the answer: how a script puts many things on the clock at once. A
firmware's own commands (`.radio`, `.reticulum.…`) take `spread=` too, the
nodes spread over that many seconds.

Nothing about a station is said to it but what a script says: the radio,
the role and the rest come from first-boot rules, most of them in
`scripts/startup.py`, which a script includes. The first thing a script
does (or its end) starts the simulation: on the Nodes tab's geodata and
nodesets when the page ran it, or on `--geodata`/`--nodeset` from a shell.
A script run on a running simulation (`--sim`) says its firmware and
first-boot rules to that one instead, and cannot change its speed. One run
on a paused simulation (`--resume`) resumes it as it ended, at the script's
speed (real unless it says otherwise), and says its rules to it the same
way. Said later, `.firmware()` and `.on_first_boot()` apply to the running
simulation at once: a node whose firmware changes is restarted on it, its
state kept.
"""

import asyncio
import builtins
import datetime
import functools
import json
import os
import textwrap
import threading

from sim_mesh import select as select_module
from sim_mesh import sim as sim_module

SPEEDS = ("real", "max")
LOGLEVELS = ("output", "commands", "debug")
LOG_FILE = "scripts.log"
REPLY_CHARS = 400               # of a command's answers, in the scripts' log
TESTBED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ScriptError(Exception):
    """A script asked for something that cannot be done."""


# ---- inputs ----------------------------------------------------------------------

class Firmware(str):
    """A `script_input` type: an installed firmware's name, or `<base>_latest`."""


class Run(str):
    """A `script_input` type: a run's name."""


INPUT_TYPES = {int: "int", float: "float", str: "str", bool: "bool", Firmware: "firmware",
               Run: "run"}
TRUE, FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def input_value(name, kind, given):
    """A value as given (`--set`, the page: text, or the default as written),
    as the input's type."""
    if kind is bool and not isinstance(given, bool):
        text = str(given).strip().lower()
        if text not in TRUE + FALSE:
            raise ScriptError("input %s is yes or no, not %r" % (name, given))
        return text in TRUE
    try:
        return kind(given)
    except (TypeError, ValueError):
        raise ScriptError("input %s is a %s, not %r" % (name, INPUT_TYPES[kind], given)) from None


# ---- first-boot rules ------------------------------------------------------------

class Rule:
    """A firmware's command said on the class (`Node.radio(sf=9)`): what
    `.on_first_boot()` gives a station, as data, {verb, args, category?}."""

    def __init__(self, verb, args, category=None):
        self.verb = verb
        self.args = {k: v for k, v in args.items() if v is not None}
        self.category = category

    def to_json(self):
        out = {"verb": self.verb, "args": dict(self.args)}
        if self.category is not None:
            out["category"] = self.category
        return out

    def __repr__(self):
        return "Node.%s%s(%s)" % (self.category + "." if self.category else "", self.verb,
                                  ", ".join("%s=%r" % kv for kv in self.args.items()))


async def _awaited(call, *args):
    """`call(*args)` on the simulation's loop, and what it hands back awaited:
    for a method that returns a task to await rather than a coroutine."""
    return await call(*args)


def lines_of(rules):
    """What rules are, in order: strings of one or more lines (kept as their
    text, line by line), Rules (as their data), or lists of either."""
    out = []
    for rule in rules:
        if isinstance(rule, Rule):
            out.append(rule.to_json())
        elif isinstance(rule, (list, tuple)):
            out += lines_of(rule)
        elif isinstance(rule, str):
            for line in textwrap.dedent(rule).splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
        else:
            raise ScriptError("a first-boot rule is lines or a command said on Node "
                              "(Node.radio(…)), not %r" % (rule,))
    return out


def speed_of(speed):
    """A sim_speed() argument as simd spells it: real, max or `<k>x`."""
    if speed in SPEEDS:
        return speed
    try:
        rate = float(str(speed).rstrip("x"))
    except ValueError:
        raise ScriptError("sim_speed() is \"real\", \"max\" or a pace such as 10, not %r"
                          % (speed,)) from None
    if rate <= 0:
        raise ScriptError("a pace is more than 0")
    return "%gx" % rate


# ---- the hold on the simulation --------------------------------------------------

class Runtime:
    """One script's hold on its simulation: the declarations until it is
    started, then the simulation, its websocket on an event loop of its own
    in a thread beside the script's; and its lines in the run's scripts.log."""

    def __init__(self):
        self.world = {}             # geodata, nodesets, name, build, build_tag, port, script,
                                    # sim, resume
        self.script_name = None     # what its lines in scripts.log are under
        self.speed = None
        self.firmware_rules = []
        self.first_boot_rules = []
        self.given = {}             # input values as the runner was given them (--set)
        self.inputs = {}            # the declared inputs' values, once script_input() has run
        self.loglevel = None        # the script's own, over the simulation's
        self.log_path = None
        self.log_pending = []       # lines said before there is a run to write them in
        self.log_lock = threading.Lock()
        self.sim = None
        self.on_held = None         # called with the simulation once it is held
        self.loop = None
        self.thread = None
        self.lock = threading.Lock()
        self.blocked = 0            # the script's thread is waiting on this many calls
        self.count_lock = threading.Lock()

    def configure(self, script_name=None, **world):
        self.world = {k: v for k, v in world.items() if v is not None}
        self.script_name = script_name

    # ---- the loop ---------------------------------------------------------

    def call(self, coro, wait=True):
        """Run `coro` on the loop: its result, or with `wait` false a future."""
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self.loop.run_forever, name="sim-mesh-loop",
                                           daemon=True)
            self.thread.start()
        if not wait:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        # While the script's thread waits here, its simulation may yield the
        # floor; between two calls the script is deciding what to do next, at
        # the T of the answer it got, and T waits for it. The call stops
        # counting on the loop, as its coroutine ends: counted until the
        # script's thread took the result, a look at the floor in between
        # would yield it with the script about to act, and T would run on for
        # however long the host took to wake the thread.
        with self.count_lock:
            self.blocked += 1

        async def counted():
            try:
                return await coro
            finally:
                with self.count_lock:
                    self.blocked -= 1

        future = asyncio.run_coroutine_threadsafe(counted(), self.loop)
        if self.sim is not None:
            self.loop.call_soon_threadsafe(self.sim.poke)
        return future.result()

    def held(self):
        """The simulation, started (or attached to) on first need."""
        with self.lock:
            if self.sim is None:
                self.sim = self.call(self._begin())
                self.sim.may_yield = lambda: self.blocked > 0
                self.sim.trace = self.log
                self.open_log(self.sim.run_dir)
                if self.on_held is not None:
                    self.on_held(self.sim)
            return self.sim

    async def _begin(self):
        world = self.world
        port = world.get("port")
        attach_to = world.get("sim") or (os.environ.get("SIM_MESH_SIM") if not world else None)
        if attach_to or world.get("resume"):
            if world.get("resume"):
                # A paused one goes on as it ended, at this script's speed.
                sim = await sim_module.resume(world["resume"], self.speed or "real", port)
            elif self.speed is not None:
                raise ScriptError("sim_speed() is a new simulation's: %s runs as it was started"
                                  % attach_to)
            else:
                sim = await sim_module.attach(attach_to, port)
            if self.firmware_rules:
                await sim.firmware(self.firmware_rules)
            if self.first_boot_rules:
                await sim.first_boot(self.first_boot_rules)
            return sim
        if not world.get("geodata") or not world.get("nodesets"):
            raise ScriptError("this script has no simulation: run it from the Scripts tab, or "
                              "with sim run SCRIPT --geodata G --nodeset N (or --sim S, or "
                              "--resume S)")
        if not self.firmware_rules:
            raise ScriptError("this script says no .firmware(…), so no node would run anything: "
                              "a script that is included by others (startup.py) is run "
                              "through one of them")
        # Started with no rules, a new simulation runs nothing, and T stands,
        # until this script's driver has the floor; the rules then come as an
        # attached script's do. Started with them, its stations would run for
        # however long attaching took on the host, and the script would begin
        # at a T the host had decided.
        sim = await sim_module.start(
            world["geodata"], world["nodesets"], world.get("script"), self.speed or "real",
            world.get("name"), world.get("build"), None, None, port,
            inputs=dict(self.inputs) or None, build_tag=world.get("build_tag"))
        await sim.firmware(self.firmware_rules)
        if self.first_boot_rules:
            await sim.first_boot(self.first_boot_rules)
        return sim

    def close(self):
        if self.loop is None:
            return
        if self.sim is not None:
            self.call(self._close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop = self.sim = None

    async def _close(self):
        await self.sim.close()
        await self.sim.session.close()

    # ---- scripts.log ------------------------------------------------------

    def level(self):
        """How much this script logs: its own level, else its simulation's."""
        return self.loglevel or (self.sim.script_loglevel if self.sim else LOGLEVELS[0])

    def log(self, level, text):
        """A line in the run's scripts.log, at `level`: the run's T, the wall
        clock, the script and what it says."""
        if LOGLEVELS.index(level) > LOGLEVELS.index(self.level()):
            return
        t = (self.sim.t if self.sim else 0) or 0
        s, us = divmod(int(t), 1_000_000)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        wall = datetime.datetime.now().isoformat(timespec="milliseconds")
        line = "%02d:%02d:%02d.%03d %s %s %s\n" % (h, m, s, us // 1000, wall,
                                                  self.script_name or "-", text)
        with self.log_lock:
            if self.log_path is None:
                self.log_pending.append(line)
                return
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line)

    def open_log(self, run_dir):
        """scripts.log in `run_dir`, with what was said before it was there."""
        if not run_dir or not os.path.isdir(run_dir):
            return
        with self.log_lock:
            self.log_path = os.path.join(run_dir, LOG_FILE)
            pending, self.log_pending = self.log_pending, []
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.writelines(pending)


runtime = Runtime()


def _sim():
    return runtime.held()


def _reply(results):
    text = json.dumps(results, default=str)
    return text if len(text) <= REPLY_CHARS else text[:REPLY_CHARS] + "…"


def _done(sel, what, results):
    runtime.log("commands", "%r.%s → %s" % (sel, what, _reply(results)))
    return results


# ---- the script --------------------------------------------------------------------

def script_input(name, type=str, label=None, default=None, category=None):  # noqa: A002
    """One of the script's inputs, and its value: from the page or `--set
    name=value`, else its default; one with neither stops the script, naming
    it. See the module's docstring."""
    if type not in INPUT_TYPES:
        raise ScriptError("script_input %s: a type is one of %s" % (
            name, ", ".join(t.__name__ for t in INPUT_TYPES)))
    given = runtime.given.get(name, default)
    if given in (None, ""):
        raise ScriptError("input %s (%s) has no value: choose one above the script, or "
                          "give --set %s=…" % (name, label or name, name))
    value = input_value(name, type, given)
    runtime.inputs[name] = value
    return value


def script_include(path, missing_ok=False):
    """Run another file's code here: `path` is under testbed/. With
    `missing_ok`, a file that is not there is nothing."""
    full = os.path.join(TESTBED_DIR, path)
    if not os.path.isfile(full):
        if missing_ok:
            return
        raise ScriptError("script_include: no file %s" % path)
    with open(full, encoding="utf-8") as handle:
        code = compile(handle.read(), full, "exec")
    builtins.exec(code, {"__name__": "include_%s" % os.path.splitext(os.path.basename(path))[0],
                         "__file__": full})


def script_results(futures):
    """Every future's result, in order: what `wait=False` commands came to."""
    return [f.result() for f in futures]


def script_loglevel(level):
    """This script's log level, over its simulation's: output, commands or debug."""
    if level not in LOGLEVELS:
        raise ScriptError("a log level is one of %s" % ", ".join(LOGLEVELS))
    runtime.loglevel = level


# ---- the simulation ----------------------------------------------------------------

def sim_speed(speed):
    """How the new simulation keeps time: "real", "max", or a pace."""
    if runtime.sim is not None:
        raise ScriptError("sim_speed() comes before anything is done: the simulation keeps "
                          "the speed it was started with")
    runtime.speed = speed_of(speed)


def sim_nodesets():
    """The names of the nodesets this script's world is made of, in order."""
    if runtime.world.get("nodesets"):
        return list(runtime.world["nodesets"])
    merged = _sim().run.get("nodeset") or ""
    return [name for name in merged.split("+") if name]


def sim_now():
    """Seconds of the run's clock since this script's simulation began."""
    return _sim().run_s


def sim_wait(seconds):
    """`seconds` of the run's clock."""
    sim = _sim()
    runtime.call(sim.sleep(float(seconds)))


def sim_until(seconds):
    """Until the run's clock reads `seconds`."""
    sim = _sim()
    runtime.call(sim.until(float(seconds)))


def sim_phase(*phases):
    """What the run is doing: (name, until) pairs, until in seconds of `sim_now()`."""
    sim = _sim()
    runtime.call(sim.plan(*phases))


def sim_snapshot(name, after=0.0, wait=True):
    """This moment, flushed, as a snapshot."""
    sim = _sim()
    return runtime.call(sim.snapshot(name, after), wait)


def _later(after, coro_fn):
    """`coro_fn()` once `after` seconds of the run's clock have passed."""
    sim = _sim()

    async def go():
        if after:
            await sim.sleep(float(after))
        return await coro_fn()
    return go()


def sim_pause(after=0.0, wait=True):
    """Stop the simulation with its state kept, to be resumed as it ended."""
    sim = _sim()
    return runtime.call(_later(after, sim.pause), wait)


def sim_stop(after=0.0, wait=True):
    """Stop the simulation for good."""
    sim = _sim()
    return runtime.call(_later(after, sim.stop), wait)


def sim_script_loglevel(level):
    """The scripts' log level on this simulation: output, commands or debug."""
    if level not in LOGLEVELS:
        raise ScriptError("a log level is one of %s" % ", ".join(LOGLEVELS))
    sim = _sim()
    sim.script_loglevel = level
    runtime.call(sim.send({"type": "script_loglevel", "level": level}))


# ---- nodes ---------------------------------------------------------------------------

class _Verb:
    """A firmware's command, done by each node's driver (sim_mesh.driver): on
    a selection, done; on the class `Node`, a first-boot rule. Its method
    gives the verb's arguments."""

    def __init__(self, verb, fn):
        self.verb = verb
        self.fn = fn
        functools.update_wrapper(self, fn)

    def __set_name__(self, owner, attr):
        self.category = owner.category

    def __get__(self, obj, owner=None):
        sel = obj if isinstance(obj, Nodes) else getattr(obj, "selection", None)

        @functools.wraps(self.fn)
        def call(*args, spread=0.0, after=0.0, wait=True, **kwargs):
            given = self.fn(None, *args, **kwargs)
            if sel is None:
                if spread or after or not wait:
                    raise ScriptError("a first-boot rule takes no spread, after or wait")
                return Rule(self.verb, given, self.category)
            return sel._verb(self.verb, given, self.category, spread, after, wait)
        return call


def verb(name):
    """A method of a selection or a layer that is the driver's verb `name`."""
    return lambda fn: _Verb(name, fn)


def _given(**args):
    return {k: v for k, v in args.items() if v is not None}


class _Layer:
    """A category's commands on a selection, or on the class `Node` (none):
    `.reticulum`, and within it `.lxmf`."""

    category = None

    def __init__(self, selection):
        self.selection = selection


class _Accessor:
    """A layer, reached from a selection, a layer, or the class `Node`."""

    def __init__(self, layer):
        self.layer = layer

    def __get__(self, obj, owner=None):
        sel = obj if isinstance(obj, Nodes) else getattr(obj, "selection", None)
        return self.layer(sel)


class _Lxmf(_Layer):
    """LXMF, on a selection's Reticulum nodes: `.reticulum.lxmf`."""

    category = "reticulum"

    @verb("lxmf.create")
    def create(self, name=None):
        """An LXMF identity, named after the node unless `name` says otherwise,
        unless the node has one by that name: {node: its address}."""
        return _given(name=name)

    @verb("lxmf.identities")
    def identities(self):
        """Each node's [(name, address)], the one it sends from first."""
        return {}

    @verb("lxmf.announce")
    def announce(self, name=None):
        """An announce of that identity's delivery destination (none named: the first)."""
        return _given(name=name)

    @verb("lxmf.send")
    def send(self, to, text, sender=None):
        """An LXMF message to a node or an LXMF identity, from the node's
        identity `sender` (none named: the first): {node: the message's id}."""
        return _given(to=str(to), text=str(text), sender=sender)


class _Reticulum(_Layer):
    """Reticulum, on a selection's Reticulum nodes: `.reticulum`."""

    category = "reticulum"
    lxmf = _Accessor(_Lxmf)

    @verb("role")
    def role(self, role):
        """Its role: "transport" (forwards for others) or "client"."""
        return {"role": str(role)}

    @verb("path")
    def path(self, to=None, dest_hash=None, iface=None):
        """Each node's path table, [{dest, next_hop, iface, hops}]: to a node or
        LXMF identity, a destination, on an interface, or all of it."""
        return _given(to=to, dest_hash=dest_hash, iface=iface)


class _Meshcore(_Layer):
    """MeshCore, on a selection's MeshCore nodes: `.meshcore`. Each verb is
    meshcore-cli's command of that name."""

    category = "meshcore"

    @verb("repeat")
    def repeat(self, on):
        """Forwarding others' packets on or off."""
        return {"on": bool(on)}

    @verb("advert")
    def advert(self):
        """A zero-hop advert."""
        return {}

    @verb("floodadv")
    def floodadv(self):
        """A flooded advert."""
        return {}

    @verb("contacts")
    def contacts(self):
        """Each node's [(name, public-key prefix, path length or None)]."""
        return {}

    @verb("msg")
    def msg(self, to, text):
        """A direct message to the contact `to`: {node: the message's id}."""
        return {"to": str(to), "text": str(text)}

    @verb("chan")
    def chan(self, nb, text):
        """A message on channel `nb`: {node: the message's id}."""
        return {"nb": int(nb), "text": str(text)}

    @verb("path")
    def path(self, to):
        """Each node's path to the contact `to`: its hops, or None for flood."""
        return {"to": str(to)}

    @verb("reset_path")
    def reset_path(self, to):
        """Back to flood for the contact `to`."""
        return {"to": str(to)}


class _Meshtastic(_Layer):
    """Meshtastic, on a selection's Meshtastic nodes: `.meshtastic`. Each verb
    is named as the Meshtastic CLI's option of that name."""

    category = "meshtastic"

    @verb("role")
    def role(self, role):
        """Its device role: "client", "router", "router_late", "client_mute"
        and the rest of Meshtastic's roles, in lower case."""
        return {"role": str(role)}

    @verb("hop_limit")
    def hop_limit(self, n):
        """The hops a packet it originates may take, 0 to 7."""
        return {"n": int(n)}

    @verb("sendtext")
    def sendtext(self, text, to=None, ch_index=0, want_ack=True):
        """A text message to the node `to`, or on channel `ch_index` when `to`
        is None: {node: the message's id}."""
        return _given(text=str(text), to=None if to is None else str(to),
                      ch_index=int(ch_index), want_ack=bool(want_ack))

    @verb("traceroute")
    def traceroute(self, to):
        """The route to the node `to` and back: {route, snr_towards,
        route_back, snr_back}."""
        return {"to": str(to)}

    @verb("nodes")
    def nodes(self):
        """Each node's [(name, id, hops away, snr, last heard)]."""
        return {}

    @verb("nodeinfo")
    def nodeinfo(self):
        """A NodeInfo broadcast now."""
        return {}


class Nodes(select_module.Nodes):
    """Some nodes (sim_mesh.select), and what can be done to them; see the
    module's docstring. On the class, `Node`, a firmware's command is a
    first-boot rule."""

    category = None
    reticulum = _Accessor(_Reticulum)
    meshcore = _Accessor(_Meshcore)
    meshtastic = _Accessor(_Meshtastic)

    def _names(self):
        return self.pick(_sim().facts())

    def _verb(self, verb_name, args, category, spread, after, wait):
        sim = _sim()

        async def go():
            await sim.settled()
            if category is not None:
                chosen = self.pick(sim.facts())
                mine = [n for n in chosen if sim.stations[n].category == category]
                if not mine:
                    raise ScriptError("%r has no %s node, so %s.%s is nothing to it"
                                      % (self, category, category, verb_name))
                names = await sim.ready(select_module.Nodes({"names": mine}))
            else:
                names = await sim.ready(self)
            results = await sim.ask(
                dict({"type": "meta", "verb": verb_name, "args": args},
                     **({"category": category} if category else {})),
                names, spread, after)
            said = ", ".join("%s=%r" % kv for kv in args.items())
            return _done(self, "%s%s(%s)" % (category + "." if category else "", verb_name, said),
                         results)
        return runtime.call(go(), wait)

    # ---- what they are -----------------------------------------------------

    def facts(self):
        """Each node's facts: {name: {id, tags, firmware, base, category, role,
        status, max_dbm, …}}."""
        every = _sim().facts()
        return {name: every[name] for name in self.pick(every)}

    def firmware(self, name):
        """What they run; see the module's docstring."""
        rule = {"which": self.to_json(), "firmware": str(name)}
        if runtime.sim is None:
            runtime.firmware_rules.append(rule)
            return {}
        # The simulation hands back a task of its loop's (`Sim.firmware`), made
        # and awaited there.
        return runtime.call(_awaited(runtime.sim.firmware, [rule]))

    def on_first_boot(self, *rules):
        """Lines and first-boot rules each is given the first time it boots with
        no state; see the module's docstring."""
        rule = {"which": self.to_json(), "lines": lines_of(rules)}
        if runtime.sim is None:
            runtime.first_boot_rules.append(rule)
            return None
        return runtime.call(runtime.sim.first_boot([rule]))

    def up(self, timeout=None):
        """Until every one of them is up: their names."""
        sim = _sim()
        return runtime.call(sim.ready(self, timeout))

    # ---- what is done to them ---------------------------------------------

    def exec(self, lines, pause=0.0, after=0.0, wait=True):   # noqa: A003
        """Lines in each node's own language; see the module's docstring."""
        lines = lines_of([lines])
        sim = _sim()

        async def go():
            names = await sim.ready(self)

            async def one(name):
                replies = []
                for line in lines:
                    reply = await sim.ask({"type": "command", "line": line}, [name], after=after)
                    replies.append(str(reply.get(name, "")).rstrip("\n"))
                return "\n".join(replies)

            if not pause:
                out = dict(zip(names, await asyncio.gather(*(one(n) for n in names))))
            else:
                out = {}
                for index, name in enumerate(names):
                    if index:
                        await sim.sleep(float(pause))
                    out[name] = await one(name)
            return _done(self, "exec(%r)" % lines, out)
        return runtime.call(go(), wait)

    def reset(self, after=0.0, wait=True):
        """Reset pressed on each: the process restarts, its state kept."""
        sim = _sim()

        async def go():
            names = self._names()
            await sim.nodes(names=names).reset(after)
            return _done(self, "reset()", names)
        return runtime.call(go(), wait)

    def factory_reset(self, after=0.0, wait=True):
        """Each one's state wiped, and set up again as on its first boot."""
        sim = _sim()

        async def go():
            names = self._names()
            await sim.nodes(names=names).factory_reset(after)
            return _done(self, "factory_reset()", names)
        return runtime.call(go(), wait)

    def move(self, lat, lon, height_m=None, after=0.0, wait=True):
        """The one node to another place; its links are recomputed."""
        names = self._names()
        if len(names) != 1:
            raise ScriptError("move is for one node, and %r is %d" % (self, len(names)))
        sim = _sim()

        async def go():
            await sim.move(names[0], lat, lon, height_m, after)
            return _done(self, "move(%s, %s)" % (lat, lon), names)
        return runtime.call(go(), wait)

    @verb("radio")
    def radio(self, freq_mhz=None, sf=None, bw_khz=None, cr=None, tx_dbm=None, sync=None,
              preamble=None):
        """Slot 0's settings, those given; `tx_dbm="max"` is each node's own maximum."""
        return _given(freq_mhz=freq_mhz, sf=sf, bw_khz=bw_khz, cr=cr, tx_dbm=tx_dbm,
                      sync=sync, preamble=preamble)

    @verb("radio_up")
    def radio_up(self):
        """The radio started, after whatever it reads when it starts (its
        settings, SUPE, the community radius)."""
        return {}


Node = Nodes


def nodes(**where):
    """The nodes whose facts are all as given; see sim_mesh.select."""
    return Nodes(select_module.nodes(**where).tree)


def node(name):
    """One node, by name."""
    return Nodes({"names": [str(name)]})


__all__ = ["script_input", "script_include", "script_results", "script_loglevel",
           "sim_speed", "sim_nodesets", "sim_now", "sim_wait", "sim_until", "sim_phase",
           "sim_snapshot", "sim_pause", "sim_stop", "sim_script_loglevel",
           "nodes", "node", "Node", "Firmware", "Run", "ScriptError"]
