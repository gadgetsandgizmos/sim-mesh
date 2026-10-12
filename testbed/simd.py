#!/usr/bin/env python3
"""The simulated testbed as one process: ether, stations, proxy and control.

```
page ── ws /ws ─────────────────────────────────────► simd   control, one JSON object a frame
simd ── snapshot {run, geodata, nodeset, script, nodes, …} ─► page   on connect and on every load
page ── nodeset_move {name, lat, lon, settle: true} ──► simd
simd: the run's nodeset.yaml written, the move logged with T (nodeset-edits.jsonl)
simd ── node {name, …, stale: true}, nodeset {…} ────► page
simd ── GET <sidecar>/link.json × (n-1) ─────────────► planner-web   (synthetic: computed here)
simd: the node's row and column into runs/<run>/losses/<band>.bin, then into the ether
simd ── node {name, …, stale: false} ───────────────► page
script ── meta {verb: announce, tag: lora, stagger: 300, id} ► simd
simd: each station's driver does the verb its own way, spread over 300 s of T
simd ── command_result {id, verb, results: {name: reply}, t} ► every page
page ── ws /ws/console/<name> ◄────────────────────► the station's pty
browser ── http <name>.sim.localhost:<port> ────────► the station's own web UI
```

One command starts this, and this is the whole testbed. It holds

- the `Ether` and its UDP endpoint, in this process's own event loop, so the
  loss tables reach the medium by direct call and never over a wire;
- the stations, one firmware process on a pty each, with a supervisor apiece;
- one listener on the bind port, which routes by `Host`: a
  `<name|id>.sim.localhost` request is proxied to that station's own web UI,
  and everything else is the control page and its websockets;
- the loaded **run** (runs.py): the geodata, the run's own copy of the
  nodeset, the script's copy when it has one, the loss tables the ether
  reads, and every station's directory. A run is made from geodata, a
  nodeset and optionally a script (`sim_load`) or from a snapshot
  (`snapshot_load`), or is handed over made already (`--run`, which is how
  the front starts a simulation).

Several of these run at once as the children of front.py, each on its own
ports, network and run directory, with the front in front of all of them.

**Firmware.** What each node runs is said by firmware rules, `{which,
firmware}`: a selection of nodes (sim_mesh.select) and an installed
firmware's name or `<base>_latest` (firmware.py). A run is made with its
script's rules (`sim_load`'s `firmware_rules`, which the front has from the
script's runner) and takes more from a `firmware` message; they are kept in
the run (`firmware_rules`), and each node runs what the last rule matching
it names (`firmware`), resolved once and kept in the run (`builds`). A node
no rule matches runs nothing and is not started; a node placed later is
matched as it lands. A node whose firmware a new rule changes is restarted
on it, its state kept. A simulation started with a build (`build`, and
`build_tag` to limit it to the nodes carrying one tag) runs that firmware on
every node the rules give one, whatever they give. The firmware's driver
(drivers.py) is how simd starts and talks to its stations.

**First boot.** A station that boots with no state is set up: its name
(the `name` verb), then what its first-boot rules give it (`{which,
lines}`, the script's `.on_first_boot(…)`, kept in the run as
`first_boot_rules` and added to by a `first_boot` message), in order: a
line as written, typed in the firmware's own language, a verb (`{verb, args,
category?}`: its radio, its role) as the driver's verb of that name, and
nothing on a station of another category than the verb's, a transmit power
of "max" or above the node's maximum held to that maximum; then the
driver's flush. Nothing else is said: the nodeset declares no settings, and
a radio starts when a rule says so. No script code runs here: the rules are
data. A station with state is left alone. Lines have `{max_dbm}`, the
node's maximum power, among their macros.

Loss tables. The run holds one table per band its nodes' carriers use
(`losses.bands_of`), and the ether rules on every frame from them, with the
nodeset's links, the geodata's shadowing, the nodes' antennas and the
nodeset's offsets on them (`losses.medium_tables`);
the antennas need the ground under each node, which is asked of the sidecar
on a pack (`losses.grounds`) and kept in the run as `grounds`. A nodeset edit that changes
the geometry (a move, a height, a node added) recomputes the touched nodes'
rows and columns into the run's copy, as a task: through the planner sidecar
named by `--sidecar` or by the load that brought the geodata in, or on
synthetic ground here. Until a row lands the ether keeps the old one, and the
node's `node` message says `stale: true`. The cache in `losses/` is never
written by an edit.

Messages, page → simd (anything else is ignored, as on the wire). Any of
them may carry `after`, seconds on the run's clock before it is acted on:

```
sim_load {geodata, nodeset, script?, build?, build_tag?, sidecar?, firmware_rules?,
          first_boot_rules?}
                                                    a new run from these, factory fresh
sim_load {run, sidecar?}                            adopt a run directory made already
snapshot_load {name, build?, build_tag?, sidecar?}  a new run from a snapshot, state and all
snapshot_save_as {name}                             this moment, flushed, as a snapshot
nodeset_add {name, lat, lon, id?, height_m?, height_from?, max_dbm?, antenna?, tags?}
nodeset_move {name, lat, lon, height_m?, settle?}   settle false: a drag still moving
nodeset_remove {name}
nodeset_set {name, id?, lat?, lon?, height_m?, height_from?, max_dbm?, antenna?, tags?}
                                                    max_dbm null: the node states none (22 dBm)
firmware {rules: [{which, firmware}], id?}          more firmware rules, from a script
first_boot {rules: [{which, lines: [line | {verb, args, category?}]}], id?}
                                                    more first-boot rules, from a script
nodeset_offset {between: [a, b], db, note?}         db 0 removes it
levels {name, freq?}                                what the others would hear from it
command {line, name? | names? | tag?, base?, stagger?, after?, id?}
                                                    one line on the stations chosen, all of one base
meta {verb, args?, category?, name? | names? | tag?, base?, stagger?, after?, id?}
                                                    a verb, by each driver; with a category, only on
                                                    those stations of it
sequence {steps: [command | meta], name? | names? | tag?, base?, after?, id?}
                                                    those one after another at one T, answered
                                                    once with a list of results
node_reset {name} · node_factory_reset {name} · reset_all · factory_reset_all
node_stop {name, after?, id?} · node_start {name, after?, id?}
                                                    power off (stopped until started, state kept,
                                                    nothing flushed) and on
start_all · stop_all
plan {phases: [{name, until}]}                      a driver's phases, T in µs
script_loglevel {level}                             how much the scripts on it log, unless
                                                    one says otherwise: output, commands, debug
```

Messages, simd → page:

```
snapshot {run, geodata, nodeset, script, bands, nodes: [node…], port, clock,
          geodata_names, nodesets, scripts, snapshots, medium, antennas, script_loglevel}
node {name, id, base, category, firmware, device_name, web, lat, lon, height_m,
      height_from, max_dbm, antenna, tags, role, status, stale, mode?, freq?, sf?, bw?}
node_gone {name}
nodeset {name, dirty, geometry_hash, nodes, offsets, links?}
store {geodata_names, nodesets, scripts, snapshots}   after a snapshot is saved
losses_progress {band, done, total}                 while a sim_load computes
levels {name, freq, heard: {name: dBm}}
command_result {id?, line | verb, name, results: {name: reply} | [{name: reply}], t, error?}
                                                    a list for a sequence, one per step; a reply
                                                    is what the line printed or the verb returned;
                                                    `error` when a meta could not be begun
clock {mode, rate, t, observed, barriers, slow_idles, plan}
script_loglevel {level}                             the scripts' log level, changed
tx {name, eid, freq, t_start, t_end} · rx {name, from, eid, verdict, level}
radio {name, mode, freq, sf, bw}
notice {text}                                       something done that a person should know
error {text}
```

**Choosing stations.** `command` and `meta` go to the stations that are up
(or in setup) among the ones named: `name`, a list of `names`, every one
carrying `tag`, or all of them. A line is in one firmware's language, so
the chosen stations must all run one base, or `base` must narrow them to
one; a verb goes to every station, each through its own driver. `meta`'s
verbs are every firmware's (`sim_mesh.driver`) and a category's
(`sim_mesh.reticulum.driver`, `sim_mesh.meshcore.driver`,
`sim_mesh.meshtastic.driver`); with `category`,
a meta is only for the chosen stations of that category, and one that leaves
none of them is an error. `lxmf.send` takes `to`, a node or an LXMF
identity, whose address is asked of its station first, and `path` may (or
`dest_hash` and `iface`, or nothing for the whole table); `lxmf.send` may
take `from`, its sender the same way, in place of a choice of stations, and
is answered with each message's id, which simd gives it; `peer_tcp` takes
`to` and `port`. With `category: meshcore`, `msg`, `path` and `reset_path`
take `to`, a contact's name (a node's own, once it has advertised it),
passed as it is, and `msg` and `chan` are answered with each message's id.
With `category: meshtastic`, `sendtext` and `traceroute` take `to`, a node's
name, passed as it is (a `sendtext` without it is a channel message), and
`sendtext` is answered with each message's id.

`role` is what the station's driver reads from it (`current_role`), polled;
None when it cannot say. `?quiet=1` on the websocket leaves out tx, rx, radio and levels,
which is most of the traffic on a busy network and nothing a driver or the
front's registry reads. See INTERNALS.md for why it is shaped this way.

Nothing here blocks. Every wait is an awaitable, the stations' ptys are read
on a thread of their own (stations.Ptys) that hands this loop what it acts
on, and a framed-RPC query is a future that a reply handed over completes.
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import sys

from aiohttp import WSMsgType, web

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SIM_DIR)
sys.path.insert(0, os.path.join(SIM_DIR, "..", "ether"))

import antennas as antennas_module  # noqa: E402 - the paths are set just above
import boards as boards_module     # noqa: E402
import ether as ether_module       # noqa: E402
import slt                         # noqa: E402
import geodata as geodata_module   # noqa: E402
import drivers as drivers_module   # noqa: E402
import losses as losses_module     # noqa: E402
import nodeset as nodeset_module   # noqa: E402
import proxy                       # noqa: E402
import runs as runs_module         # noqa: E402
import script as script_module     # noqa: E402
import stations as stations_module  # noqa: E402
import store                       # noqa: E402
import webrtc as webrtc_module     # noqa: E402
from sim_mesh import select as select_module  # noqa: E402

UI_DIST = os.path.join(SIM_DIR, "ui", "dist", "spa")
DEFAULT_RUN = os.path.join(store.RUNS_DIR, "simd")

ROLE_POLL_S = 6.0           # how often a station is asked what it does for the mesh
SETUP_TIMEOUT_S = 90.0      # how long a fresh station has to say it is up
SHUTDOWN_TIMEOUT_S = 5.0    # how long a connection may hold up a stop
SETTLE_S = 15.0             # how long a first boot gets to finish landing what setup asked for
CLOCK_REPORT_S = 1.0        # how often the page hears how fast T is going
WEBRTC_PORT = 4433          # the station's own DataChannel port (s.net.webrtc_port)
SIGNAL_PATH = "/webrtc"     # the one station route simd keeps for itself
LOUD = ("tx", "rx", "radio", "levels")  # what a `quiet` control socket is not sent
NODE_FIELDS = ("id", "lat", "lon", "height_m", "height_from", "max_dbm", "antenna", "tags")
# The fields whose null a node message means: clear it.
CLEARABLE_FIELDS = ("max_dbm",)
EVENTS_FILE = "events.jsonl"  # what drivers report, a JSON object a line
NAMED_VERBS = ("name", "lxmf.create")  # verbs whose `name` is the node's own unless given
MESHCORE_TO_VERBS = ("msg", "path", "reset_path")   # `to`, a contact's name, is their `dest`
MESHCORE_MID_VERBS = ("msg", "chan")                # answered with the message's id
MESHTASTIC_TO_VERBS = ("sendtext", "traceroute")    # `to`, a node's name, is their `dest`
MESHTASTIC_MID_VERBS = ("sendtext",)                # answered with the message's id
SCRIPT_LOGLEVELS = ("output", "commands", "debug")  # what a script's scripts.log holds

log = stations_module.log


# ---------------------------------------------------------------------------
# Where a run goes, and what a page can pick from
# ---------------------------------------------------------------------------

def free_run_dir(path):
    """`path`, or `path-2`, `path-3`…: the first that holds no run yet. A run
    is an output and is never written over."""
    path = os.path.abspath(path)
    candidate, n = path, 2
    while os.path.exists(os.path.join(candidate, runs_module.RUN_FILE)):
        candidate = "%s-%d" % (path, n)
        n += 1
    return candidate


def socket_drops(sock):
    """The datagrams the kernel has dropped on `sock`'s full receive buffer:
    the last field of its row in /proc/net/udp (or udp6), found by its inode.
    None when there is no such row to read."""
    try:
        inode = str(os.fstat(sock.fileno()).st_ino)
    except (AttributeError, OSError, ValueError):
        return None
    for table in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(table, encoding="ascii") as handle:
                for line in handle:
                    fields = line.split()
                    if len(fields) >= 13 and fields[9] == inode:
                        return int(fields[12])
        except (OSError, ValueError):
            continue
    return None


def store_lists():
    """Everything a page can pick from, by name, and globals.py's shared
    settings (script.SHARED), or why they cannot be read."""
    try:
        shared, problem = script_module.shared(), None
    except store.StoreError as err:
        shared, problem = None, str(err)
    return {"geodata_names": geodata_module.names(), "nodesets": nodeset_module.names(),
            "scripts": script_module.names(), "snapshots": runs_module.snapshots(),
            "globals": shared, "globals_error": problem}


def page_build():
    """Which build of the page is served: its entry script's path, which the
    bundler names by its content. A page that finds itself another build
    reloads. None when the page has not been built."""
    try:
        with open(os.path.join(UI_DIST, "index.html"), encoding="utf-8") as handle:
            found = re.search(r'<script[^>]*type="module"[^>]*src="([^"]+)"', handle.read())
    except OSError:
        return None
    return found.group(1) if found else None


async def serve_page(request):
    """The built page, with every unknown path falling back to it.

    One page and no router beyond it, so a deep link is the same document;
    a request for a file that is really there gets that file.
    """
    path = request.match_info.get("tail", "")
    if path and not path.startswith("."):
        candidate = os.path.normpath(os.path.join(UI_DIST, path))
        if candidate.startswith(UI_DIST) and os.path.isfile(candidate):
            return web.FileResponse(candidate)
    page = os.path.join(UI_DIST, "index.html")
    if os.path.isfile(page):
        return web.FileResponse(page)
    return web.Response(
        text="The control page has not been built.\n\n"
             "In sim-mesh/testbed/ui run `npm install && npx quasar build`.\n",
        content_type="text/plain")


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------

class Simd:
    """The testbed: the medium, the stations, the run and the page."""

    def __init__(self, args):
        self.args = args
        self.run_base = args.run            # where new runs go: this, or this-2, …
        self.ether_addr = args.ether
        self.ether = None
        self.ether_transport = None
        self.run = None                     # a runs.Run, or None until one is loaded
        self.geodata = None
        self.nodeset = None                 # the run's own copy, as edited
        self.drivers = {}                   # firmware name -> its driver
        self.builds = {}                    # firmware name -> what it resolved to
        self.rules = []                     # firmware rules, [{which, firmware}], in order
        self.first_boot = []                # first-boot rules, [{which, lines}], in order
        self.firmware = {}                  # node name -> the firmware it runs
        self.grounds = {}                   # node name -> the ground under it, m above sea
        self.tables = {}                    # band -> slt.Table, the model's own
        self.sidecar = args.sidecar         # planner-web base URL, for a pack
        self.stale = set()                  # nodes whose rows are not in the tables yet
        self.pending_rows = set()           # of those, the ones still to compute
        self.rows_task = None
        self.stations = {}                  # node name -> Station
        self.identities = {}                # LXMF identity name -> the node holding it
        self.mids = {}                      # message id -> how many share its stem
        self.pages = {}                     # the control websockets -> quiet
        self.consoles = {}                  # node name -> set of websockets
        self.poller = None
        self.role_wake = asyncio.Event()    # set when a page or a station comes or goes
        # The driver (a script's socket that said `drive`) takes turns with T:
        # T stands while it has the floor, from each answer simd gives it
        # until it says `yield`, having nothing left to do but wait on simd.
        self.driver = None
        self.driver_floor = False
        self.floor_turn = 0                 # moves each time the driver is given the floor
        self.driver_ids = set()             # its requests not yet answered
        self.status_changed = asyncio.Event()   # a station's status moved (`wait` for up)
        self.stopping = False
        self.control_port = None            # where the aiohttp app really listens
        self.front = None
        self.runner = None
        self.relay = None                   # the WebRTC UDP relay
        self.starter = None                 # the staggered start, while it runs
        self.observed_rate = None           # seconds of T per second of wall, lately
        self.clock_watch = None
        self.plan = None                    # a driver's phases, {t, phases: [{name, until}]}

    # ---- lookups ---------------------------------------------------------

    def name_of(self, sid):
        """The node name behind a station id, or None if the nodeset has none."""
        return self.ether.names.get(sid) if self.ether is not None else None

    def node_for_label(self, label):
        """The node behind a `<label>.sim.localhost`, by name or by id.

        Behind the front the label is `<station>.<simulation>`; the front has
        already chosen this simulation by the second part, so only the first
        is this process's business.
        """
        if self.nodeset is None or label is None:
            return None
        label = label.split(".", 1)[0]
        if label in self.nodeset.nodes:
            return label
        if label.isdigit():
            return self.nodeset.by_id(int(label))
        return None

    def driver_of(self, name):
        ref = self.firmware.get(name)
        return self.drivers.get(ref) if ref else None

    @property
    def build_override(self):
        """The firmware the simulation was started with, and the tag it is
        limited to (None: every node): the run's, else this process's
        --build and --build-tag."""
        if self.run and self.run.meta.get("build"):
            return self.run.meta["build"], self.run.meta.get("build_tag") or None
        return self.args.build or None, self.args.build_tag or None

    def ids(self):
        return {name: node["id"] for name, node in self.nodeset.nodes.items()}

    def resolve_host(self, label, path=""):
        """Where a request with this `Host` label goes: a station, or simd.

        A label is a name or an id out of `<label>.sim.localhost`; anything
        else — `localhost`, an IP, no Host at all — is the control page, which
        is why one port serves both.

        One station route is simd's own: `/webrtc`, the DataChannel's
        signalling. simd stands in the middle of it so it can point the SDP
        answer at the relay, because the address the station would offer is
        inside the container and the browser is outside it.
        """
        if label is None:
            return ("127.0.0.1", self.control_port)
        name = self.node_for_label(label)
        if name is None:
            return None
        driver = self.driver_of(name)
        port = driver.web_port() if driver else None
        if port is None:
            return proxy.Refusal(
                "404 Not Found", "%s runs %s, which has no web UI.\n"
                % (name, driver.firmware["firmware"] if driver else "nothing"))
        if path.split("?", 1)[0] == SIGNAL_PATH:
            return ("127.0.0.1", self.control_port)
        return (stations_module.bind_addr(self.nodeset.nodes[name]["id"]), port)

    # ---- the medium ------------------------------------------------------

    async def start_ether(self, record_dir=None):
        """A fresh ether on the bind, recording into `record_dir`.

        One per run: T starts at zero with the run, and the record is the
        run's. Every station has been stopped before it is replaced.
        """
        if self.ether is not None:
            self.ether.close()
            # A transport lets go of its socket on the loop's next turn.
            for _ in range(3):
                await asyncio.sleep(0)
        record = None
        if record_dir is not None:
            os.makedirs(record_dir, exist_ok=True)
            record = os.path.join(record_dir, "record.tsv")
        bind = ether_module.parse_bind(self.ether_addr)
        physics = ether_module.Physics(self.args.noise_figure,
                                       interference=not self.args.no_interference,
                                       fading_db=self.args.fading_db,
                                       coherence_s=self.args.coherence_s,
                                       rician_k=self.args.rician_k)
        self.ether_transport, self.ether = await ether_module.open_ether(
            bind, record, physics=physics, time_mode=self.args.time,
            pairwise=self.args.pairwise, seed=self.args.seed, epoch=self.args.epoch,
            bench_capture=self.args.bench_capture)
        self.ether.on_tx = self.ether_tx
        self.ether.on_rx = self.ether_rx
        self.ether.on_station = self.ether_station
        self.ether.on_drain = self.drain_consoles
        self.ether.drains_watched = True        # the stations' readers are watched there
        if record is not None:
            log("ether on %s, recording to %s" % (self.ether_addr, record))
            log("time: %s; %s; %s rule" % (
                ether_module.describe_time(self.ether.mode, self.ether.rate),
                physics.describe(),
                ether_module.rule_name(self.args.pairwise, self.args.bench_capture)))
        if self.ether.clock.virtual and not os.path.exists(drivers_module.SHIM):
            log("error: no time shim at %s: a virtual-time run needs it "
                "(see sim-mesh/README.md)" % drivers_module.SHIM)

    @property
    def virtual(self):
        return self.ether is not None and self.ether.clock.virtual

    async def sleep(self, seconds):
        """Wait on the run's clock: conductor time in a virtual run."""
        await self.ether.sleep(seconds)

    def clock_message(self):
        """What the page is told about time: the mode, T, and how fast it goes."""
        if self.ether is None:
            return None
        return {"type": "clock", "mode": self.ether.mode, "rate": self.ether.rate,
                "t": self.ether.now(), "observed": self.observed_rate,
                "barriers": self.ether.barriers, "runs": self.ether.runs,
                "slow_idles": sum(st.slow_idles for st in self.ether.stations.values()),
                "ether_drops": self.ether_drops(),
                "plan": self.plan}

    def ether_drops(self):
        """What the kernel has dropped on the ether's socket, or None: as
        the core counts it while it reads (SO_RXQ_OVFL), else from its row in
        /proc/net/udp, a file that grows with every station's sockets."""
        if self.ether is None:
            return None
        core = getattr(self.ether, "core", None)
        if core is not None:
            return core.drops
        transport = self.ether.transport
        if transport is None:
            return None
        return socket_drops(transport.get_extra_info("socket"))

    async def watch_clock(self):
        """Tell the page how fast T is going, once a second of wall."""
        loop = asyncio.get_running_loop()
        last_wall, last_t = loop.time(), self.ether.now()
        slow_seen = {}
        dropped = self.ether_drops()
        while not self.stopping:
            await asyncio.sleep(CLOCK_REPORT_S)
            wall, t = loop.time(), self.ether.now()
            now_dropped = self.ether_drops()
            if dropped is not None and now_dropped is not None and now_dropped > dropped:
                # A drop between two stations and the ether is resent (the
                # ether's `resend`); anything else lost is lost, so say so.
                log("the kernel dropped %d datagram(s) on the ether's full receive buffer "
                    "by T %.3f s: what a station said may be lost, and the run with it "
                    "(net.core.rmem_max is the ceiling)" % (now_dropped - dropped, t / 1e6))
            dropped = now_dropped
            if t < last_t:                  # a new run, a new ether
                last_t, slow_seen = t, {}
            if wall > last_wall:
                self.observed_rate = (t - last_t) / 1_000_000.0 / (wall - last_wall)
            if self.virtual and t == last_t and self.ether.busy():
                log("T stands at %.3f s, waiting on station(s) %s" % (
                    t / 1e6, ", ".join(str(s) for s in self.ether.waiting_on())))
            if self.virtual:
                slow = {sid: st.slow_idles - slow_seen.get(sid, 0)
                        for sid, st in self.ether.stations.items()}
                slow = {sid: n for sid, n in slow.items() if n > 0}
                if slow and self.observed_rate is not None and self.observed_rate < 2:
                    log("T at %.3f s held by busy station(s): %s" % (t / 1e6, ", ".join(
                        "%s %d" % (self.name_of(sid) or sid, n)
                        for sid, n in sorted(slow.items(), key=lambda x: -x[1])[:5])))
                slow_seen = {sid: st.slow_idles for sid, st in self.ether.stations.items()}
            last_wall, last_t = wall, t
            self.broadcast(self.clock_message())

    def apply_to_ether(self):
        """Give the medium the tables with the links, the shadowing, the
        antennas and the offsets on them, and which station id is which node,
        wholesale.

        Cheap enough to redo outright on every change of ids, antennas,
        offsets or the node set; a node the tables have no row for yet is in
        the medium but hears nothing and is heard by nothing until its row
        lands.
        """
        if self.nodeset is None:
            self.ether.clear()
            return
        nodes = self.nodeset.nodes
        self.ether.set_losses(losses_module.medium_tables(self.tables, self.geodata,
                                                          self.nodeset, self.grounds),
                              {name: node["id"] for name, node in nodes.items()})

    async def find_grounds(self, names=None):
        """The ground under these nodes (all, by default), into `grounds` and
        the run: 0 on synthetic ground, the pack's terrain through the sidecar."""
        got = await losses_module.grounds(self.geodata, self.nodeset, self.sidecar, names)
        if got:
            self.grounds = dict(self.grounds, **got)
            if self.run is not None:
                self.run.set(grounds=self.grounds)

    def drain_consoles(self, sids, done):
        """The ether is about to move T past stations `sids`, which have run
        since it last did: read everything they printed first (their replies,
        the marker), so what it makes the testbed do happens at this T. False,
        and `done` not called, when none of them printed anything
        (stations.printed), which is most of the time. A station whose kind
        is not acted on by what it prints (console_acted_on) is not waited for:
        its console is read as it comes."""
        wanted = set(sids)
        drains = stations_module.printed([s.drain for s in self.stations.values()
                                          if wanted.intersection(s.driver.sids(s))
                                          and s.drain is not None
                                          and s.driver.console_acted_on])
        if not drains:
            return False
        stations_module.ptys().call(stations_module.catch_up, drains,
                                    asyncio.get_running_loop(), done)
        return True

    def ether_tx(self, sid, eid, freq, t_start, t_end):
        if not self.heard_loud():
            return
        name = self.name_of(sid)
        if name is not None:
            self.broadcast({"type": "tx", "name": name, "eid": eid, "freq": freq,
                            "t_start": t_start, "t_end": t_end})

    def ether_rx(self, sid, from_sid, eid, verdict, level, cause=None):
        if not self.heard_loud():
            return
        name, sender = self.name_of(sid), self.name_of(from_sid)
        if name is not None and sender is not None:
            self.broadcast({"type": "rx", "name": name, "from": sender,
                            "eid": eid, "verdict": verdict, "level": round(level, 1),
                            "cause": cause})

    def ether_station(self, sid, state):
        """A station said what its radio is doing; the map draws only the carrier."""
        if not self.heard_loud():
            return
        name = self.name_of(sid)
        if name is None:
            return
        self.broadcast({"type": "radio", "name": name, "mode": state.get("mode"),
                        "freq": state.get("freq"), "sf": state.get("sf"),
                        "bw": state.get("bw")})

    # ---- the loss rows ---------------------------------------------------

    def need_rows(self, names):
        """These nodes' rows and columns are out of date: mark them stale
        and have them recomputed, in the background, into the run's tables."""
        names = set(names) & set(self.nodeset.nodes)
        if not names:
            return
        self.stale |= names
        self.pending_rows |= names
        for name in names:
            self.broadcast(self.node_message(name))
        if self.rows_task is None or self.rows_task.done():
            self.rows_task = asyncio.ensure_future(self.recompute_rows())

    async def recompute_rows(self):
        """Recompute the pending rows until none are left.

        Each pass works on the nodeset as it is when the pass begins; a node
        edited again meanwhile is pending again and gets another pass. The
        new tables are written to the run's `losses/` and handed to the ether
        whole, so no frame is ruled on half a row.
        """
        run = self.run
        while self.pending_rows and run is self.run:
            names, self.pending_rows = set(self.pending_rows), set()
            ns = self.nodeset.copy()
            await self.find_grounds(names & set(ns.nodes))
            try:
                fresh = {}
                for band, table in list(self.tables.items()):
                    fresh[band] = await losses_module.update_nodes(
                        table, self.geodata, ns, names, self.sidecar, notice=log)
            except (store.StoreError, OSError) as err:
                self.error("recomputing losses for %s: %s" % (", ".join(sorted(names)), err))
                continue
            except Exception as err:        # noqa: BLE001 - aiohttp's, among others
                self.error("recomputing losses for %s: %r" % (", ".join(sorted(names)), err))
                continue
            if run is not self.run:
                return
            for band, table in fresh.items():
                losses_module.write_table(table, run.table_path(band))
            self.tables = fresh
            self.apply_to_ether()
            done = names - self.pending_rows
            self.stale -= done
            for name in sorted(done):
                if name in self.nodeset.nodes:
                    self.broadcast(self.node_message(name))
            log("losses recomputed for %s" % ", ".join(sorted(names)))

    # ---- stations --------------------------------------------------------

    def make_station(self, name):
        node = self.nodeset.node(name)
        station = stations_module.Station(
            name, node["id"], self.run.node_dir(name), self.driver_of(name), self.ether_addr,
            on_status=self.station_status, on_output=self.station_output,
            clock=self.ether if self.virtual else None, on_event=self.station_event)
        station.watchers = len(self.consoles.get(name, ()))
        station.board = boards_module.environment(node.get("max_dbm"))
        station.clock_profile = self.clock_profile(name)
        return station

    def clock_profile(self, name):
        """A station's crystal, off by a draw uniform within ±`--clock-ppm`
        parts per million, from the seed and its name: its node time as a
        function of T (`SIM_MESH_CLOCK_PROFILE`, INTERNALS.md's *Time*), or
        None for a true clock. A firmware's own profile in its node.yaml
        `env` still wins."""
        if not self.args.clock_ppm or not self.virtual:
            return None
        draw = ether_module.seeded_draw(self.ether.seed, name, "clock")
        return drift_profile(self.args.clock_ppm * (2.0 * draw - 1.0))

    def station_event(self, station, event, fields):
        """An event a station's driver reported, into the run's events.jsonl
        at this T: what a category's analysis counts (a message delivered)."""
        if self.run is None or self.stations.get(station.name) is not station:
            return
        line = dict(fields, t=self.ether.now(), node=station.name, event=event)
        try:
            with open(os.path.join(self.run.dir, EVENTS_FILE), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError as err:
            log("events.jsonl: %s" % err)

    def watched(self, name):
        """The console windows open on a station, as the pty thread sees it:
        it hands console bytes over only while there is one."""
        station = self.stations.get(name)
        if station is not None:
            station.watchers = len(self.consoles.get(name, ()))

    def station_status(self, station, status):
        if self.stations.get(station.name) is station:
            self.broadcast(self.node_message(station.name))
        self.role_wake.set()
        self.status_changed.set()

    def station_output(self, station, data):
        """Console bytes go to whoever has that station's terminal open."""
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        for socket in list(self.consoles.get(station.name, ())):
            asyncio.ensure_future(self.send_console(socket, text))

    async def send_console(self, socket, text):
        try:
            await socket.send_str(text)
        except (ConnectionError, RuntimeError):
            pass

    async def after_start(self, station):
        """Wait for a station to be up, and set it up if it has never been.

        A station with state is left alone: its state is the run's, and
        setting it up again would be re-answering questions it has already
        answered. A station without is a node somebody has just put on the
        map, and it should be a working station rather than a dot waiting for
        someone to type.
        """
        if not await station.driver.wait_up(station, SETUP_TIMEOUT_S):
            log("station %s never came up (%s)" % (station.name,
                                                   station.driver.firmware["firmware"]))
            return
        if not station.was_configured:
            station.set_status(stations_module.SETUP)
            try:
                await self.send_setup(station)
            except drivers_module.CommandError as err:
                self.error("setting up %s: %s" % (station.name, err))
        else:
            await self.restate_role(station)
        station.set_status(stations_module.UP)
        log("station %s is up at t %.3f s" % (station.name, self.ether.now() / 1e6))
        await self.read_role(station)
        if not station.was_configured:
            # Not everything setup asks for lands at once: an LXMF identity
            # reaches the store some seconds after the command that created it
            # has returned. One more flush once that has settled, so a station
            # reset moments after its first boot keeps what it was given
            # rather than coming back half-configured.
            await self.sleep(SETTLE_S)
            await self.flush_station(station)

    def expanded(self, name, lines):
        return drivers_module.expand_all(lines, name, self.nodeset.nodes[name]["id"],
                                         self.ids(), {"max_dbm": "%g" % self.max_dbm(name)})

    async def restate_role(self, station):
        """A station whose firmware forgets its role when it restarts comes
        back up with its state but not its role: the role verbs its
        first-boot rules gave it are said again, so a reset does not turn a
        transport into a client."""
        if not station.driver.role_volatile:
            return
        entries = self.first_boot_entries(station.name, verbs=("role",))
        if not entries:
            return
        try:
            await self.setup_entries(station, entries)
        except drivers_module.CommandError as err:
            self.error("telling %s its role again: %s" % (station.name, err))

    def max_dbm(self, name):
        """A node's maximum power at the connector (boards.max_dbm)."""
        return boards_module.max_dbm(self.nodeset.nodes[name].get("max_dbm"))

    def entry_args(self, name, entry):
        """One first-boot verb's arguments for one node. A transmit power of
        "max", or above the node's maximum, is the node's maximum."""
        args = dict(entry.get("args") or {})
        if "tx_dbm" in args:
            top = self.max_dbm(name)
            args["tx_dbm"] = top if args["tx_dbm"] == "max" else min(float(args["tx_dbm"]), top)
        if entry["verb"] in NAMED_VERBS and "name" not in args:
            args["name"] = name
        return args

    def first_boot_entries(self, name, verbs=None):
        """What the first-boot rules give a station, in the rules' order: a
        line (a string) or a verb ({verb, args, category?}). `verbs` keeps
        only those verbs, and no plain lines."""
        facts = self.facts(name)
        out = []
        for rule in self.first_boot:
            if not select_module.Nodes.from_json(rule["which"]).matches(facts):
                continue
            for entry in rule["lines"]:
                if isinstance(entry, dict):
                    if verbs is None or entry.get("verb") in verbs:
                        out.append(entry)
                elif verbs is None:
                    out.append(entry)
        return out

    async def setup_entries(self, station, entries):
        """Lines and verbs for a station, in order, then its driver's flush.
        A verb of another category than the station's is nothing to it. Every
        one is tried; the ones that failed are reported together."""
        driver = station.driver
        failed = []
        for entry in entries:
            try:
                if isinstance(entry, dict):
                    if entry.get("category") not in (None, driver.category):
                        continue
                    await self.do_verb(station, entry["verb"],
                                       self.entry_args(station.name, entry))
                else:
                    for line in self.expanded(station.name, [entry]):
                        await driver.run(station, line)
            except drivers_module.CommandError as err:
                failed.append("%s: %s" % (entry.get("verb") if isinstance(entry, dict)
                                          else entry, err))
        with contextlib.suppress(drivers_module.CommandError):
            await driver.flush(station)
        if failed:
            raise drivers_module.CommandError("; ".join(failed))

    async def send_setup(self, station):
        """The station's name, then what its first-boot rules give it, in
        their order, then a flush.

        The rules are the whole of what a station is told beyond its name:
        simd adds no settings of its own, and a radio is started by the rule
        that says so. Its driver flushes once they are in. A store that
        coalesces writes would otherwise come back from a Reset pressed the
        moment a node came up with none of it, and a testbed that lost its
        own setup that way would be lying about what it had configured.
        """
        entries = [{"verb": "name", "args": {"name": station.name}}]
        try:
            await self.setup_entries(station, entries + self.first_boot_entries(station.name))
        except drivers_module.CommandError as err:
            raise drivers_module.CommandError("first-boot lines: %s" % err) from err

    async def start_station(self, name):
        if self.run is None or name not in self.nodeset.nodes or name in self.stations:
            return
        if not self.firmware.get(name):
            return                          # it runs nothing until a rule says what
        driver = self.driver_of(name)
        if driver is None:
            self.error("%s: its firmware %s was not resolved" % (name, self.firmware[name]))
            return
        if not drivers_module.build_present(driver.firmware):
            self.error("%s: firmware %s is not installed" % (name, driver.firmware["firmware"]))
            return
        station = self.make_station(name)
        self.stations[name] = station
        station.run(self.watch_after_start)
        self.broadcast(self.node_message(name))

    async def watch_after_start(self, station):
        """after_start, with anything it raises said rather than lost.

        It runs as a task nobody awaits, so an exception in it would vanish
        and leave the station showing whatever status it had reached.
        """
        try:
            await self.after_start(station)
        except asyncio.CancelledError:
            raise
        except Exception as err:                # noqa: BLE001 - see docstring
            self.error("%s: %r" % (station.name, err))

    async def flush_station(self, station):
        """Ask a station to commit its store before we take it away from it.

        A store may coalesce writes (a firmware may hold them for a minute),
        and some of what a station records — an LXMF identity among
        them — lands there a little after the command that asked for it. So
        anything that stops or resets a station flushes it first, and so does
        taking a snapshot: a snapshot copied out of a store with a minute of
        writes still in RAM would be a picture of a moment that never quite
        existed. What a flush is, and whether there is one, is the driver's.

        Best effort. A station that will not answer is one whose store we
        cannot flush, and refusing to stop it over that would be worse.
        """
        if station.status not in (stations_module.UP, stations_module.SETUP):
            return
        with contextlib.suppress(drivers_module.CommandError):
            await station.driver.flush(station)

    async def flush_all(self):
        await asyncio.gather(*(self.flush_station(s) for s in self.stations.values()),
                             return_exceptions=True)

    async def stop_station(self, name, flush=True):
        station = self.stations.pop(name, None)
        if station is None:
            return
        if flush:
            await self.flush_station(station)
        await station.stop()

    async def start_all(self):
        """Start every station that is not running, spread over a minute.

        Not all at once. Two dozen firmware processes forking in the same
        instant is a thundering herd against one host, and the network it
        produces is worse than the load: stations that boot together finish
        booting together, so their first announces land on top of each other
        and the opening minute is a collision storm no real network powered up
        by hand would ever have. Spreading the starts costs nothing and makes
        the first minute of the air look like a first minute.
        """
        if self.nodeset is None:
            return
        pending = [name for name, _ in sorted(self.nodeset.nodes.items(),
                                              key=lambda item: item[1]["id"])
                   if name not in self.stations and self.firmware.get(name)]
        if not pending:
            return
        gap = self.args.stagger / len(pending)
        for index, name in enumerate(pending):
            if index:
                await self.sleep(gap)
            # A minute is long enough for the nodeset to have moved on.
            if self.stopping or self.nodeset is None:
                return
            await self.start_station(name)

    def begin_start_all(self):
        """Run the staggered start in the background.

        The caller is handling a message from the page, and a start that took
        a minute to return would hold every other message behind it for that
        minute. Held as a task so the next load or stop can cancel it rather
        than race it.
        """
        self.cancel_start()
        self.starter = asyncio.ensure_future(self.start_all())

    def cancel_start(self):
        if self.starter is not None:
            self.starter.cancel()
            self.starter = None

    async def stop_all(self, flush=True):
        """Every station stopped, flushed first. In a virtual run they leave T
        together (leave_together) before any process is touched."""
        self.cancel_start()
        if flush:
            await self.flush_all()
        await self.leave_together()
        await asyncio.gather(*(self.stop_station(name, flush=False)
                               for name in list(self.stations)))

    async def leave_together(self, stations=None):
        """In a virtual run, every station (or those given) leaves T at its
        next barrier: the instant they are all idle and what they said has
        been taken, before T moves on and before any is sent its next instant.
        Stopped one by one, on the host's time, the others ran on meanwhile,
        and how far into the record they got was the host's to say: two runs
        of one cell parted there. Each is marked stopping, so none that exits
        before its own stop is started again. Bounded, as every wait of a stop
        is: a run that reaches no barrier (a driver holding T, a station that
        never idles) is stopped as before."""
        leaving = list(self.stations.values() if stations is None else stations)
        if not self.virtual or not leaving:
            return
        reached = asyncio.get_running_loop().create_future()

        def barrier():
            if reached.done():
                return
            for station in leaving:
                station.stopping = True
                station.leave()
            reached.set_result(None)
        self.ether.call_at(self.ether.now(), barrier, key=-1)
        self.ether.kick()
        try:
            await asyncio.wait_for(asyncio.shield(reached), SHUTDOWN_TIMEOUT_S)
        except asyncio.TimeoutError:
            reached.cancel()
            log("the stations met no barrier in %.0f s: stopped where each was"
                % SHUTDOWN_TIMEOUT_S)

    # ---- the role poll ---------------------------------------------------

    async def read_role(self, station):
        """Ask a station what it does for the mesh, and say so.

        Read live rather than taken from the nodeset, because the setting is
        live: a person can flip it on the station itself, and the map should
        show it without the nodeset knowing. A driver that cannot ask answers
        None, and the map shows the node's role tag.
        """
        reader = getattr(station.driver, "current_role", None)
        if reader is None:
            return
        try:
            role = await reader(station)
        except drivers_module.CommandError:
            return
        if role != station.role:
            station.role = role
            if self.stations.get(station.name) is station:
                self.broadcast(self.node_message(station.name))

    def roles_wanted(self):
        """Whether a live role has anyone to go to: a page that draws the map
        is open, and a station is up to be asked. A quiet socket is a
        driver's, and a script goes by a node's role tag until a role is
        reported (sim_mesh.sim)."""
        return (any(not quiet for quiet in self.pages.values())
                and any(s.status == stations_module.UP for s in self.stations.values()))

    async def poll_roles(self):
        """Every ROLE_POLL_S of the run's clock while roles are wanted: of T
        in a virtual run, so the questions reach the stations at the same
        instants in every run.

        Only then. A question is a tool run against the station's console,
        which wakes the station and holds T while it answers: a Berlin run
        asked 170 stations every six seconds for a map nobody had open. And
        a sleep on T while nothing runs moves T as fast as the host goes, so
        a virtual run began wherever that had taken T by the time its
        stations started, which is a matter of the host's speed."""
        while not self.stopping:
            if not self.roles_wanted():
                self.role_wake.clear()
                await self.role_wake.wait()
                continue
            await self.sleep(ROLE_POLL_S)
            if not self.roles_wanted():
                continue
            running = [s for s in self.stations.values() if s.status == stations_module.UP]
            await asyncio.gather(*(self.read_role(s) for s in running),
                                 return_exceptions=True)

    # ---- talking to the page ---------------------------------------------

    def radio_of(self, sid):
        """The station's last stated radio, so a page that connects late has it.

        A station states its radio when it changes and not otherwise, so a page
        opened on a quiet network would wait for the next change to learn what
        anything is tuned to. The ether already holds the last one.
        """
        station = self.ether.stations.get(sid) if self.ether else None
        state = station.state(0) if station else None
        if not state:
            return {}
        return {"mode": state.get("mode"), "freq": state.get("freq"),
                "sf": state.get("sf"), "bw": state.get("bw")}

    def node_message(self, name):
        node = self.nodeset.nodes.get(name) if self.nodeset else None
        if node is None:
            return {"type": "node_gone", "name": name}
        station = self.stations.get(name)
        driver = self.driver_of(name)
        build = driver.firmware if driver else {}
        return {"type": "node", "name": name, "id": node["id"],
                "base": build.get("base"), "category": build.get("category"),
                "firmware": self.firmware.get(name),
                "device_name": drivers_module.label(build) if driver else None,
                "web": driver is not None and driver.web_port() is not None,
                "lat": node["lat"], "lon": node["lon"], "height_m": node["height_m"],
                "height_from": node["height_from"], "antenna": dict(node["antenna"]),
                "max_dbm": self.max_dbm(name), "tags": list(node["tags"]),
                "status": station.status if station else stations_module.STOPPED,
                "role": station.role if station else None,
                "stale": name in self.stale,
                # The T it is said at: a station's `up` comes at the instant it
                # came up, which is the run's, whenever a driver hears it.
                "t": self.ether.now() if self.ether is not None else None,
                **self.radio_of(node["id"])}

    def nodeset_message(self):
        return {"type": "nodeset", **self.nodeset.as_dict()} if self.nodeset else \
            {"type": "nodeset", "name": None}

    def script_info(self):
        if self.run is None or self.run.script_path is None:
            return None
        return script_module.describe(self.run.script_path, self.run.meta.get("script"))

    def snapshot(self):
        loaded = self.run is not None
        return {"type": "snapshot",
                "run": self.run.as_dict() if loaded else None,
                "geodata": self.geodata.as_dict() if loaded else None,
                "nodeset": self.nodeset.as_dict() if loaded else None,
                "script": self.script_info(),
                "bands": sorted(self.tables),
                "nodes": [self.node_message(name)
                          for name in (self.nodeset.nodes if loaded else ())],
                "port": self.args.public_port,
                "clock": self.clock_message(),
                "medium": {"noise_figure_db": self.args.noise_figure,
                           "pairwise": self.args.pairwise},
                "antennas": antennas_module.listing(),
                "script_loglevel": self.script_loglevel(),
                **store_lists()}

    def script_loglevel(self):
        """How much the scripts on this simulation log unless one says
        otherwise (its run's `script_loglevel`)."""
        return (self.run.meta.get("script_loglevel") if self.run else None) or SCRIPT_LOGLEVELS[0]

    async def do_script_loglevel(self, msg):
        """The scripts' log level for this simulation, kept in its run; every
        script on it is told."""
        level = msg.get("level")
        if level not in SCRIPT_LOGLEVELS:
            raise ValueError("a script log level is one of %s" % ", ".join(SCRIPT_LOGLEVELS))
        self.need_run().set(script_loglevel=level)
        self.broadcast({"type": "script_loglevel", "level": level})

    def broadcast(self, message):
        if message.get("type") == "command_result" and message.get("id") in self.driver_ids:
            # An answer to the driver: it has the floor from this T until it
            # yields, so what it does about the answer it does at this T.
            self.driver_ids.discard(message["id"])
            self.take_floor()
        loud = message.get("type") in LOUD
        pages = [socket for socket, quiet in self.pages.items() if not (quiet and loud)]
        if not pages:
            return
        text = json.dumps(message)
        for socket in pages:
            asyncio.ensure_future(self.send_page(socket, text))

    def heard_loud(self):
        """Whether a page takes what the medium does, frame by frame (LOUD):
        a run driven by a quiet socket alone need not be told it."""
        return not all(self.pages.values())

    async def send_page(self, socket, text):
        try:
            await socket.send_str(text)
        except (ConnectionError, RuntimeError):
            self.pages.pop(socket, None)

    def error(self, text):
        log("error: %s" % text)
        self.broadcast({"type": "error", "text": text})

    def notice(self, text):
        log(text)
        self.broadcast({"type": "notice", "text": text})

    def progress(self, band):
        """A losses progress callback that tells the page about a hundred times."""
        def show(done, total):
            step = max(1, total // 100)
            if done == total or done % step == 0:
                self.broadcast({"type": "losses_progress", "band": band,
                                "done": done, "total": total})
        return show

    # ---- what the page asks for ------------------------------------------

    async def handle(self, msg):
        """One message from the page. Anything unknown is ignored, as on the wire."""
        kind = msg.get("type")
        handler = getattr(self, "do_" + kind, None) if isinstance(kind, str) else None
        if handler is None:
            return
        try:
            if self.run is not None and self.later(msg):
                return
            await handler(msg)
        except (store.StoreError, drivers_module.CommandError) as err:
            self.refused(msg, str(err))
        except OSError as err:
            self.refused(msg, "%s: %s" % (kind, err))
        except (KeyError, TypeError, ValueError) as err:
            self.refused(msg, "%s: malformed message (%s)" % (kind, err))

    def refused(self, msg, text):
        """What `msg` asked for could not be done: the page is told, and a
        request (one with an `id`, which a script waits on) is answered with
        the error. Unanswered, a script waited on it for ever, and a
        virtual-time run went on without its driver as fast as it goes."""
        self.error(text)
        if msg.get("id") is not None:
            self.broadcast({"type": "command_result", "id": msg["id"], "name": msg.get("name"),
                            "results": {}, "error": text,
                            "t": self.ether.now() if self.ether is not None else None})

    # ---- the driver's turns -------------------------------------------------

    def take_floor(self):
        """The driver has the floor: T stands until it yields."""
        self.floor_turn += 1
        if self.driver is not None and not self.driver_floor and self.virtual:
            self.driver_floor = True
            self.ether.holds += 1

    def driver_yields(self):
        """The driver has nothing left to do but wait on us: it lets go once
        everything it sent before has been set going (settle), so what it
        asked for is put at the T it asked at; not if it has been answered
        again meanwhile, which gives it the floor for that answer."""
        turn = self.floor_turn

        def let_go():
            if self.floor_turn == turn:
                self.give_floor()
        self.ether.settle(let_go)

    def give_floor(self):
        """The driver yields, or has gone: T runs again."""
        if self.driver_floor:
            self.driver_floor = False
            self.ether.release()

    async def do_wait(self, msg):
        """A driver's wait, answered at the instant it ends, so the driver
        goes on at an instant of the run and not at whatever instant a look
        at a clock report happened to catch: `until`, T in µs; or `for: up`,
        until every station (or `names`) is up. A task of its own, as a
        message put off with `after` is."""
        async def wait():
            if msg.get("until") is not None:
                left = int(msg["until"]) - self.ether.now()
                if left > 0:
                    await self.sleep(left / 1e6)
            elif msg.get("for") == "up":
                names = msg.get("names")
                while True:
                    chosen = [s for n, s in self.stations.items()
                              if names is None or n in names]
                    wanted = set(names) if names is not None else set(self.firmware)
                    if chosen and {s.name for s in chosen} >= wanted and all(
                            s.status == stations_module.UP for s in chosen):
                        break
                    self.status_changed.clear()
                    await self.status_changed.wait()
            self.answered(msg, {})
        asyncio.ensure_future(wait())

    def need_run(self):
        if self.run is None:
            raise store.StoreError("nothing is loaded: load geodata and a nodeset, "
                                   "or a snapshot")
        return self.run

    def edited(self, what, **fields):
        """After a nodeset edit: the run's nodeset.yaml written, the edit
        logged with T, and the page told."""
        run = self.need_run()
        nodeset_module.write(run.nodeset_path, self.nodeset.data)
        run.log_edit(self.ether.now(), what, **fields)
        self.broadcast(self.nodeset_message())

    @staticmethod
    def node_fields(msg, without=()):
        return {key: msg[key] for key in NODE_FIELDS
                if key in msg and (msg[key] is not None or key in CLEARABLE_FIELDS)
                and key not in without}

    # ---- firmware --------------------------------------------------------

    def facts(self, name):
        """What a firmware rule's selection (sim_mesh.select) asks of a node."""
        node = self.nodeset.nodes[name]
        station = self.stations.get(name)
        driver = self.driver_of(name)
        build = driver.firmware if driver else {}
        return {"name": name, "id": node["id"], "tags": list(node["tags"]),
                "firmware": self.firmware.get(name), "base": build.get("base"),
                "category": build.get("category"),
                "role": (station.role if station else None) or nodeset_module.tag_role(node["tags"]),
                "antenna": node["antenna"]["type"], "max_dbm": self.max_dbm(name), "lat": node["lat"], "lon": node["lon"],
                "height_m": node["height_m"],
                "status": station.status if station else stations_module.STOPPED}

    @staticmethod
    def check_rule(rule):
        """A firmware rule as it came, checked: {which, firmware}."""
        if not isinstance(rule, dict) or not str(rule.get("firmware") or "").strip():
            raise ValueError("a firmware rule is {which, firmware}")
        return {"which": select_module.Nodes.from_json(rule.get("which")).to_json(),
                "firmware": str(rule["firmware"]).strip()}

    def assigned(self):
        """{node: firmware} as the rules give it: the last matching rule's.
        A build the simulation was started with then replaces the firmware of
        every node the rules gave one, or of those carrying its tag."""
        rules = [(select_module.Nodes.from_json(r["which"]), r["firmware"]) for r in self.rules]
        out = {}
        for name in self.nodeset.nodes:
            facts = self.facts(name)
            for chosen, ref in rules:
                if chosen.matches(facts):
                    out[name] = ref
        build, tag = self.build_override
        if build:
            for name in out:
                if tag is None or tag in self.nodeset.nodes[name]["tags"]:
                    out[name] = build
        return out

    async def settle_firmware(self):
        """Each node's firmware as the rules say, every name resolved to an
        installed firmware once and its driver loaded; one that cannot be is
        said, and its nodes run nothing. Returns the nodes whose firmware
        changed."""
        wanted = self.assigned()
        failed = set()
        for ref in sorted(set(wanted.values())):
            if drivers_module.build_present(self.builds.get(ref)) and ref in self.drivers:
                continue
            try:
                builds = drivers_module.resolve_builds([ref])
                self.drivers.update(drivers_module.load_all(builds))
            except drivers_module.CommandError as err:
                self.error("firmware %s: %s" % (ref, err))
                failed.add(ref)
                continue
            self.builds.update(builds)
            log("firmware %s: %s" % (ref, drivers_module.label(builds[ref])))
        wanted = {n: ref for n, ref in wanted.items() if ref not in failed}
        changed = [n for n in self.nodeset.nodes if wanted.get(n) != self.firmware.get(n)]
        self.firmware = wanted
        self.run.set(firmware=dict(self.firmware), firmware_rules=list(self.rules),
                     builds=dict(self.builds))
        return changed

    async def restart_on_firmware(self, changed):
        """Stations whose firmware changed, on it: a running one restarted
        with its state, the rest started with the staggered start."""
        idle = False
        for name in changed:
            station = self.stations.get(name)
            if station is None:
                idle = idle or bool(self.firmware.get(name))
                self.broadcast(self.node_message(name))
                continue
            had_state = station.configured
            await self.stop_station(name)
            await self.start_station(name)
            driver = self.driver_of(name)
            self.notice("%s now runs %s; its station was restarted%s" % (
                name, drivers_module.label(driver.firmware) if driver else "nothing",
                ", keeping its state" if had_state and driver else ""))
        if idle:
            self.begin_start_all()

    async def do_firmware(self, msg):
        """More firmware rules, from a driver: kept with the run, and every
        node whose firmware they change started or restarted on it. Answered
        as a `command_result` whose results are {node: firmware} for those."""
        self.need_run()
        try:
            for rule in (self.check_rule(r) for r in msg.get("rules") or ()):
                if rule not in self.rules:
                    self.rules.append(rule)
            changed = await self.settle_firmware()
            await self.restart_on_firmware(changed)
        except (ValueError, TypeError, store.StoreError, drivers_module.CommandError) as err:
            self.answered(msg, {}, verb="firmware", error=str(err))
            return
        self.answered(msg, {n: self.firmware.get(n) for n in changed}, verb="firmware")
        log("firmware rules: %d; %d node(s) changed" % (len(self.rules), len(changed)))

    @staticmethod
    def check_first_boot(rule):
        """A first-boot rule as it came, checked: {which, lines}, each line
        a string or a verb {verb, args, category?}: one of that category's,
        or without one, one every firmware has."""
        if not isinstance(rule, dict) or not isinstance(rule.get("lines"), list):
            raise ValueError("a first-boot rule is {which, lines}")
        lines = []
        for line in rule["lines"]:
            if isinstance(line, dict):
                category = line.get("category")
                if category is not None and category not in drivers_module.CATEGORY_CLASSES:
                    raise ValueError("no firmware category %r" % category)
                if line.get("verb") not in drivers_module.verbs(category):
                    raise ValueError("no such verb %r%s" % (
                        line.get("verb"), " for %s firmware" % category if category else ""))
                entry = {"verb": line["verb"], "args": dict(line.get("args") or {})}
                if category is not None:
                    entry["category"] = category
                lines.append(entry)
            else:
                lines.append(str(line))
        return {"which": select_module.Nodes.from_json(rule.get("which")).to_json(),
                "lines": lines}

    async def do_first_boot(self, msg):
        """More first-boot rules, from a script: kept with the run, for every
        station that boots with no state from now on. Answered as a
        `command_result`."""
        self.need_run()
        try:
            for rule in (self.check_first_boot(r) for r in msg.get("rules") or ()):
                self.first_boot.append(rule)
            self.run.set(first_boot_rules=list(self.first_boot))
        except (ValueError, TypeError) as err:
            self.answered(msg, {}, verb="first_boot", error=str(err))
            return
        self.answered(msg, {}, verb="first_boot")

    async def do_nodeset_add(self, msg):
        self.need_run()
        name = msg["name"]
        fields = self.node_fields(msg, without=("lat", "lon"))
        node = self.nodeset.add_node(name, float(msg["lat"]), float(msg["lon"]), **fields)
        self.edited("add", node=name, **{k: node[k] for k in ("id", "lat", "lon", "height_m")})
        await self.find_grounds([name])
        self.apply_to_ether()
        self.need_rows([name])
        await self.settle_firmware()
        await self.start_station(name)
        self.broadcast(self.node_message(name))
        log("node %s added as station %d, running %s"
            % (name, node["id"], self.firmware.get(name) or "nothing"))

    async def do_nodeset_move(self, msg):
        """A node moved. A drag sends these at a few Hz with `settle`
        false; the file, the log and the recompute wait for the one that
        settles, and the map shows the node stale meanwhile."""
        self.need_run()
        name = msg["name"]
        self.nodeset.move_node(name, float(msg["lat"]), float(msg["lon"]))
        if msg.get("height_m") is not None:
            self.nodeset.set_node(name, height_m=float(msg["height_m"]))
        if not msg.get("settle", True):
            self.stale.add(name)
            self.broadcast(self.node_message(name))
            return
        node = self.nodeset.node(name)
        nodeset_module.write(self.run.nodeset_path, self.nodeset.data)
        self.run.log_move(self.ether.now(), name, node["lat"], node["lon"], node["height_m"])
        self.broadcast(self.nodeset_message())
        self.need_rows([name])

    async def do_nodeset_remove(self, msg):
        self.need_run()
        name = msg["name"]
        self.nodeset.node(name)
        await self.stop_station(name)
        self.nodeset.remove_node(name)
        self.firmware.pop(name, None)
        self.grounds.pop(name, None)
        self.stale.discard(name)
        self.pending_rows.discard(name)
        self.edited("remove", node=name)
        self.apply_to_ether()
        self.broadcast({"type": "node_gone", "name": name})

    async def do_nodeset_set(self, msg):
        """Change a node's facts. A new id is a new address: the station is
        restarted, and one with state keeps it, whatever it set up with its
        old id or address then stale, which the page is told. New tags may
        match another firmware rule, which restarts it on that firmware; what
        they mean to its first-boot rules takes effect at its next first
        boot, a factory reset."""
        self.need_run()
        name = msg["name"]
        before = dict(self.nodeset.node(name))
        fields = self.node_fields(msg)
        changed_id = self.nodeset.set_node(name, **fields)
        node = self.nodeset.node(name)
        self.edited("set", node=name, **fields)
        if any(node[k] != before[k] for k in ("lat", "lon", "height_m")):
            self.need_rows([name])
        changed = await self.settle_firmware()
        if changed:
            await self.restart_on_firmware(changed)
            self.apply_to_ether()
            if name in changed:
                return
        station = self.stations.get(name)
        if changed_id and station is not None:
            had_state = station.configured
            await self.stop_station(name)
            self.apply_to_ether()
            await self.start_station(name)
            self.notice("%s is now id %d at %s; its station was restarted%s" % (
                name, node["id"], stations_module.bind_addr(node["id"]),
                ", keeping its state: anything it set up with its old id or "
                "address is stale until it is factory reset" if had_state else ""))
            return
        self.apply_to_ether()
        self.broadcast(self.node_message(name))

    async def do_nodeset_offset(self, msg):
        self.need_run()
        a, b = msg["between"][:2]
        db = float(msg.get("db") or 0)
        self.nodeset.set_offset(a, b, db, msg.get("note"))
        self.edited("offset", between=[a, b], db=db)
        self.apply_to_ether()

    async def do_node_reset(self, msg):
        """Press reset: the process goes, the supervisor brings it back.

        Its state store is untouched, so it comes back as the station it was —
        which is what a reset button does and the whole of why this is not
        called anything else.
        """
        station = self.stations.get(msg["name"])
        if station is None:
            await self.start_station(msg["name"])
        else:
            await self.flush_station(station)
            await station.restart()

    async def do_node_stop(self, msg):
        """Power off: the station stops and stays stopped until `node_start`.
        The node stays in the nodeset and the medium, which hears nothing from
        it meanwhile. Nothing is flushed, as nothing is when the power goes:
        it comes back with what its store holds. In a virtual run it leaves T
        at the next barrier (leave_together), at the instant the message is
        acted on (`after`), so the cut is the same in every run."""
        self.need_run()
        name = msg["name"]
        self.nodeset.node(name)
        station = self.stations.get(name)
        if station is not None:
            self.station_event(station, "power", {"on": False})
            log("%s powered off at t %.6f s" % (name, self.ether.now() / 1e6))
            await self.leave_together([station])
            await self.stop_station(name, flush=False)
            self.broadcast(self.node_message(name))
        if msg.get("id") is not None:
            self.answered(msg, {name: "off"})

    async def do_node_start(self, msg):
        """Power on a node whose station is stopped: it boots from its state,
        as after a power cut (or set up afresh where it has none), and T waits
        for it from this instant (Station.run). A running station is left as
        it is: pressing reset is `node_reset`."""
        self.need_run()
        name = msg["name"]
        self.nodeset.node(name)
        if name not in self.stations:
            log("%s powered on at t %.6f s" % (name, self.ether.now() / 1e6))
            await self.start_station(name)
            station = self.stations.get(name)
            if station is not None:
                self.station_event(station, "power", {"on": True})
        if msg.get("id") is not None:
            self.answered(msg, {name: "on" if name in self.stations else "nothing to run"})

    async def do_node_factory_reset(self, msg):
        """Throw away a station's state and start it again, set up afresh.

        Stopped rather than reset, because the state has to go while nothing is
        holding it; on the way back up the directory is empty, which is exactly
        the station a node placed on the map for the first time is, so setup
        runs again by the ordinary path and not by a special case.
        """
        run = self.need_run()
        name = msg["name"]
        self.nodeset.node(name)
        await self.stop_station(name, flush=False)
        runs_module.wipe_state(run.dir, name)
        await self.start_station(name)
        log("factory reset %s" % name)

    async def do_factory_reset_all(self, msg):
        run = self.need_run()
        await self.stop_all(flush=False)
        runs_module.wipe_state(run.dir)
        self.begin_start_all()
        log("factory reset the whole testbed")

    async def do_reset_all(self, msg):
        """Press reset on every station, spread over the start window.

        Pressing them all at once is the same collision storm `start_all`
        spreads out, and for the same reason: every station comes up and
        announces itself into the same air, so the mesh spends its first
        minutes talking over itself and half the network learns nothing. The
        stagger is what a network of real boards has for free.
        """
        await self.flush_all()
        self.cancel_start()
        self.starter = asyncio.ensure_future(self.restart_all())

    async def restart_all(self):
        stations = list(self.stations.values())
        if not stations:
            return
        gap = self.args.stagger / len(stations)
        for index, station in enumerate(stations):
            if index:
                await self.sleep(gap)
            if self.stopping:
                return
            await station.restart()

    def chosen(self, msg):
        """The stations a `command` or `meta` goes to: up or in setup, among
        `name`, `names`, those carrying `tag`, or all; of firmware `base` when given.
        A list of (name, station) in id order."""
        wanted = None
        if msg.get("name"):
            wanted = {msg["name"]}
        elif msg.get("names") is not None:
            wanted = set(msg["names"])
        tag = msg.get("tag")
        out = []
        for name, node in sorted(self.nodeset.nodes.items(), key=lambda kv: kv[1]["id"]):
            station = self.stations.get(name)
            if station is None or station.status not in (stations_module.UP,
                                                         stations_module.SETUP):
                continue
            if wanted is not None and name not in wanted:
                continue
            if tag and tag not in node["tags"]:
                continue
            if msg.get("base") and station.driver.firmware.get("base") != msg["base"]:
                continue
            out.append((name, station))
        return out

    async def spread(self, msg, targets, one):
        """Run `one(name, station)` on every target, spread over `stagger`
        seconds of the run's clock; {name: reply}, a failure as `! why`.

        A command that puts something on the air — an announce above all —
        fired at two dozen stations in the same instant is a collision storm
        rather than a measurement, and the answers are about the storm. Zero
        keeps them simultaneous, which is what a question nobody transmits to
        answer wants.
        """
        spread = max(0.0, float(msg.get("stagger") or 0))
        gap = spread / len(targets) if spread and targets else 0.0

        async def run(index, name, station):
            if gap:
                await self.sleep(index * gap)
            try:
                reply = await one(name, station)
            except (drivers_module.CommandError, store.StoreError, KeyError) as err:
                return name, "! %s" % err
            return name, reply.rstrip("\n") if isinstance(reply, str) else reply

        return dict(await asyncio.gather(
            *(run(i, n, s) for i, (n, s) in enumerate(targets))))

    def later(self, msg):
        """`after`: seconds on the run's clock before the message is acted on,
        so a script can put it at an instant of a virtual-time run. The wait
        is a task of its own: the page's next message is not held behind it.
        True when the message has been put off."""
        after = max(0.0, float(msg.get("after") or 0))
        if not after:
            return False

        async def go():
            await self.sleep(after)
            await self.handle(dict(msg, after=0))
        asyncio.ensure_future(go())
        return True

    def answered(self, msg, results, **what):
        self.broadcast({"type": "command_result", "id": msg.get("id"), "name": msg.get("name"),
                        "results": results, "t": self.ether.now(), **what})

    async def do_command(self, msg):
        """Run one line on the stations chosen and report what each said.

        The macros are expanded per station, so a line with `{name}` or
        `{addr}` in it does the right thing across the whole testbed in one
        go. A line is in one firmware's language, so it goes only to stations
        of one base: the chosen ones must run one, or `base` must say which.
        """
        self.need_run()
        line = (msg.get("line") or "").strip()
        if not line or self.later(msg):
            return
        self.answered(msg, await self.command_results(msg), line=line)

    async def command_results(self, msg):
        """`command`'s work, at once: {name: what it said}."""
        line = (msg.get("line") or "").strip()
        targets = self.chosen(msg)
        types = sorted({station.driver.firmware.get("base") for _, station in targets})
        if len(types) > 1:
            raise drivers_module.CommandError(
                "%r would go to %s stations, and a line is one firmware's language: say "
                "which base" % (line, " and ".join(types)))

        async def one(name, station):
            return await station.driver.run(station, drivers_module.expand(
                line, name, self.nodeset.nodes[name]["id"], self.ids()))

        results = await self.spread(msg, targets, one)
        log("ran %r on %d %s station(s)%s" % (line, len(results), types[0] if types else "",
            " over %.0fs" % float(msg.get("stagger") or 0) if msg.get("stagger") else ""))
        return results

    async def do_verb(self, station, verb, args):
        """One verb through the station's driver. `lxmf.create` names an
        identity, which no other node's identity and no other node may be
        named: the name is how a message's sender and recipient are found."""
        if verb != "lxmf.create":
            return await station.driver.do(station, verb, **args)
        name = args.get("name")
        holder = self.identities.get(name)
        if (holder not in (None, station.name)
                or (name in self.nodeset.nodes and name != station.name)):
            raise drivers_module.CommandError("an LXMF identity named %s would not be unique: "
                                              "%s has that name" % (name, holder or "a node"))
        found = await station.driver.do(station, verb, **args)
        if found:
            # Not known until it has an address, nor when the firmware made
            # none: lxmf_holder looks for it among the stations' identities.
            self.identities[name] = station.name
        return found

    async def lxmf_holder(self, name):
        """The node holding the LXMF identity `name`, and that identity's name
        there (None for a node named, whose identity is the one it sends
        from). An identity named in this run is known; one a station had
        before (a resumed run) is looked for among the stations up, once."""
        if name in self.identities:
            return self.identities[name], name
        if name not in self.nodeset.nodes:
            for node, station in self.chosen({}):
                if "lxmf.identities" not in station.driver.VERBS:
                    continue
                with contextlib.suppress(drivers_module.CommandError):
                    held = await station.driver.do(station, "lxmf.identities")
                    if any(ident == name for ident, _ in held):
                        self.identities[name] = node
                        return node, name
            raise drivers_module.CommandError("no node and no LXMF identity is named %s" % name)
        return name, None

    async def address_of(self, name):
        """The LXMF delivery address of the identity or node `name`, asked of
        the station holding it."""
        node, ident = await self.lxmf_holder(name)
        station = self.stations.get(node)
        if station is None or station.status != stations_module.UP:
            raise drivers_module.CommandError("%s is not up" % node)
        held = await station.driver.do(station, "lxmf.identities")
        found = held[0][1] if held and ident is None else \
            next((addr for each, addr in held if each == ident), None)
        if not found:
            raise drivers_module.CommandError("%s has no address yet" % name)
        return found

    def new_mid(self, node):
        """A message's id, sim-mesh's: the sender and the T it was sent at,
        and a count when more than one is sent at that T."""
        stem = "%s.%d" % (node, self.ether.now())
        n = self.mids.get(stem, 0)
        self.mids[stem] = n + 1
        return stem if n == 0 else "%s.%d" % (stem, n)

    async def do_meta(self, msg):
        """A verb on the stations chosen, each through its own driver.

        `lxmf.send` and `path` name the other end (`to`) by node or LXMF
        identity, whose address is asked of its station once (`path` may
        name none, for the whole table); `lxmf.send`
        may name its sender (`from`) the same way, which then chooses the
        station, and answers with the message's id, given here. `peer_tcp`
        names its other end by node, for its address in this run's network.
        Any other verb's reply is what the driver returned.
        """
        self.need_run()
        verb = msg.get("verb")
        if not verb or self.later(msg):
            return
        try:
            results = await self.meta_results(msg)
        except (drivers_module.CommandError, store.StoreError, KeyError) as err:
            # Answered, not only said: whoever asked is waiting on its id.
            self.refused(msg, str(err))
            return
        self.answered(msg, results, verb=verb)

    async def meta_results(self, msg):
        """`meta`'s work, at once: {name: what it said}."""
        verb = msg.get("verb")
        args = dict(msg.get("args") or {})
        to = args.pop("to", None)
        sender = args.pop("from", None)
        if "dest_hash" in args:
            args["dest"] = args.pop("dest_hash")
        category = msg.get("category")
        meshcore = category == "meshcore"
        meshtastic = category == "meshtastic"
        mid_verb = (verb == "lxmf.send" or (meshcore and verb in MESHCORE_MID_VERBS)
                    or (meshtastic and verb in MESHTASTIC_MID_VERBS))
        if verb == "lxmf.send" and not to:
            raise drivers_module.CommandError("lxmf.send needs `to`, a node or an LXMF identity")
        if meshcore and verb in MESHCORE_TO_VERBS:
            if not to:
                raise drivers_module.CommandError("%s needs `to`, a contact's name" % verb)
            args["dest"] = str(to)
        elif meshtastic and verb in MESHTASTIC_TO_VERBS:
            # A sendtext without `to` is a channel message.
            if to:
                args["dest"] = str(to)
            elif verb != "sendtext":
                raise drivers_module.CommandError("%s needs `to`, a node's name" % verb)
        elif verb in ("lxmf.send", "path") and to:
            args["dest"] = await self.address_of(to)
        elif verb == "peer_tcp":
            args["addr"] = stations_module.bind_addr(self.nodeset.node(to)["id"])
        if verb == "lxmf.send" and sender:
            node, args["sender"] = await self.lxmf_holder(sender)
            targets = [(n, s) for n, s in self.chosen({}) if n == node]
            if not targets:
                raise drivers_module.CommandError("%s is not up" % node)
        else:
            targets = self.chosen(msg)
        if category is not None:
            # A category's verb is nothing to a station of another; one that
            # leaves none of those chosen is a mistake, said.
            mine = [(n, s) for n, s in targets if s.driver.category == category]
            if targets and not mine:
                raise drivers_module.CommandError("none of the %d station(s) chosen runs %s "
                                                  "firmware" % (len(targets), category))
            targets = mine

        async def one(name, station):
            mine = dict(args)
            for key in ("tx_dbm", "dbm"):
                if mine.get(key) == "max":
                    mine[key] = self.max_dbm(name)
            if verb in NAMED_VERBS and "name" not in mine:
                mine["name"] = name
            if mid_verb:
                mine["mid"] = self.new_mid(name)
                await self.do_verb(station, verb, mine)
                return mine["mid"]
            return await self.do_verb(station, verb, mine)

        results = await self.spread(msg, targets, one)
        log("%s on %d station(s)" % (verb, len(results)))
        return results

    async def do_sequence(self, msg):
        """Several commands and verbs, each a `command` or `meta` without
        its own choice of stations or time, one after the other on the
        stations `msg` chooses, answered once: `results` is a list, one
        {name: reply} per step.

        One message, so the steps follow each other inside simd at the T the
        first begins, as a single `command` does: a driver that sent them one
        by one would wait on each answer across its websocket, and in a
        virtual-time run T moves on in that gap whenever every station is
        idle, which puts the second step minutes after the first.
        """
        self.need_run()
        steps = list(msg.get("steps") or ())
        if not steps or self.later(msg):
            return
        chosen = {k: msg[k] for k in ("name", "names", "tag", "base", "stagger") if k in msg}
        out = []
        for step in steps:
            step = dict(step, **chosen)
            if step.get("type") == "command":
                out.append(await self.command_results(step))
            elif step.get("type") == "meta":
                try:
                    out.append(await self.meta_results(step))
                except (drivers_module.CommandError, store.StoreError, KeyError) as err:
                    out.append({n: "! %s" % err for n, _ in self.chosen(step)})
            else:
                raise ValueError("a sequence's step is a command or a meta")
        self.answered(msg, out)

    async def do_plan(self, msg):
        """A driver says what the run is for: its phases and the T each ends at.

        simd knows T and the pace but not what the run means to do; the plan
        is what turns those into "traffic, 23 of 60 minutes" and a finish
        time. It goes out in every `clock`, and a driver sends it again when
        its recipe changes. No phases clears it.
        """
        phases = []
        for phase in msg.get("phases") or ():
            phases.append({"name": str(phase["name"]), "until": int(phase["until"])})
        self.plan = {"t": self.ether.now(), "phases": phases} if phases else None
        self.broadcast(self.clock_message())

    async def do_start_all(self, msg):
        self.begin_start_all()

    async def do_stop_all(self, msg):
        await self.stop_all()
        for name in (self.nodeset.nodes if self.nodeset else ()):
            self.broadcast(self.node_message(name))
        if msg.get("id") is not None:
            # A driver's (Sim.stop_all): it goes on once every station has stopped.
            self.answered(msg, {})

    async def do_levels(self, msg):
        """What the others would hear from one station, for the hover card.

        At the carrier asked about, else the one the station last stated,
        else the run's globals.py's: the table is at the band's centre and
        the ether corrects it to the carrier, so the answer is for the
        frequency the station is really on.
        """
        run = self.need_run()
        name = msg["name"]
        node = self.nodeset.node(name)
        radio = self.radio_of(node["id"])
        shared = run.radio()
        freq = msg.get("freq") or radio.get("freq") or shared["freq_mhz"] * 1e6
        bw = radio.get("bw") or shared["bw_khz"] * 1e3
        heard = self.ether.levels(node["id"], float(freq), bw_hz=bw)
        self.broadcast({"type": "levels", "name": name, "freq": freq,
                        "heard": {self.name_of(sid): round(level, 1)
                                  for sid, level in heard.items()
                                  if self.name_of(sid) is not None}})

    # ---- loading ---------------------------------------------------------

    async def adopt(self, run, sidecar=None):
        """Make this the loaded run: stop what was running, start what is.

        The geodata, the nodeset, the script and the tables are all the run's
        own copies, and so are its firmware and first-boot rules, which the
        script's runner gave it; each firmware name runs what the run kept
        for it while that is still installed, and is resolved again when it
        is not.
        """
        gd, ns = run.geodata(), run.nodeset()
        builds = {ref: b for ref, b in (run.meta.get("builds") or {}).items()
                  if drivers_module.build_present(b)}
        drivers = drivers_module.load_all(builds)
        rules = run.meta.get("firmware_rules") or []
        first_boot = run.meta.get("first_boot_rules") or []
        tables = {band: slt.Table.read(run.table_path(band)) for band in run.bands()}
        await self.stop_all()
        self.stale, self.pending_rows = set(), set()
        if self.rows_task is not None:
            self.rows_task.cancel()
            self.rows_task = None
        self.run, self.geodata, self.nodeset = run, gd, ns
        self.drivers, self.builds, self.tables = drivers, builds, tables
        self.rules = [self.check_rule(r) for r in rules]
        self.first_boot = [self.check_first_boot(r) for r in first_boot]
        self.firmware = {}
        self.grounds = {n: g for n, g in (run.meta.get("grounds") or {}).items() if n in ns.nodes}
        self.sidecar = sidecar or self.args.sidecar
        self.plan = None                    # a plan is for the run it was sent in
        await self.start_ether(run.dir)
        # The medium the run is started on, for whatever reads it later: the
        # analysis tools' levels take their noise figure from it, and a seed
        # drawn at random is otherwise nowhere to be found again.
        medium = dict(self.ether.physics.as_dict(), pairwise=self.ether.pairwise)
        if self.ether.bench_capture:
            medium["bench_capture"] = True
        if self.args.clock_ppm:
            medium["clock_ppm"] = self.args.clock_ppm
        run.set(physics=medium, seed=self.ether.seed)
        await self.find_grounds([n for n in ns.nodes if n not in self.grounds])
        self.apply_to_ether()
        log("run %s: geodata %s (%s), nodeset %s (%d nodes), script %s, tables %s"
            % (run.dir, gd.name, gd.kind, ns.name, len(ns.nodes),
               run.meta.get("script") or "none", ", ".join(sorted(tables)) or "none"))
        warning = losses_module.shadowing_warning(gd)
        if warning:
            log("warning: %s" % warning)
        await self.settle_firmware()
        bare = [n for n in ns.nodes if not self.firmware.get(n)]
        if bare:
            log("%d node(s) run nothing until a firmware rule names them: %s"
                % (len(bare), ", ".join(bare[:8]) + (" …" if len(bare) > 8 else "")))
        self.broadcast(self.snapshot())     # a new run is a fresh page
        self.begin_start_all()

    async def do_sim_load(self, msg):
        """A new run from geodata, a nodeset and optionally a script, factory
        fresh; or a run directory the front has made already.

        The tables come from the cache, computed into it on a miss: here on
        synthetic ground, through the sidecar for a pack. The front computes
        them before it passes this on, so behind the front this is a cache hit.
        """
        sidecar = msg.get("sidecar") or self.sidecar
        if msg.get("run"):
            await self.adopt(runs_module.open_run(msg["run"]), sidecar)
            return
        gd = geodata_module.load(msg["geodata"])
        ns = nodeset_module.load(msg["nodeset"])
        script_name = msg.get("script") or None
        if script_name:
            script_module.read(script_name)
        tables = {}
        for band in losses_module.bands_of(ns, gd):
            tables[band], _ = await losses_module.compute(gd, ns, band, sidecar,
                                                          self.progress(band), notice=log)
        backbone = losses_module.backbone_table(gd, ns, tables)
        run = runs_module.create_run(free_run_dir(self.run_base), gd, ns, script_name,
                                     self.args.time, tables)
        if backbone is not None:
            # Into the run's own copy, which `adopt` reads: never the cache,
            # which holds what the model computed.
            losses_module.write_table(backbone, run.table_path(backbone.band))
            log("backbone: %s at %d Hz" % (", ".join(backbone.names), backbone.f0_hz))
        run.set(build=msg.get("build") or None, build_tag=msg.get("build_tag") or None,
                firmware_rules=list(msg.get("firmware_rules") or []),
                first_boot_rules=list(msg.get("first_boot_rules") or []))
        await self.adopt(run, sidecar)
        log("loaded geodata %s, nodeset %s, script %s"
            % (gd.name, ns.name, script_name or "none"))

    async def do_snapshot_load(self, msg):
        """A snapshot is a moment, so loading one brings its state back too:
        its nodeset, script and tables as they were, nothing recomputed."""
        run = runs_module.load_snapshot(msg["name"], free_run_dir(self.run_base),
                                        self.args.time)
        if msg.get("build"):
            run.set(build=msg["build"], build_tag=msg.get("build_tag") or None)
        await self.adopt(run, msg.get("sidecar") or self.sidecar)
        log("loaded snapshot %s" % msg["name"])

    async def do_snapshot_save_as(self, msg):
        run = self.need_run()
        await self.flush_all()   # the stations keep running; the copy is of now
        runs_module.save_snapshot(run, msg["name"], self.ether.now())
        self.broadcast({"type": "store", **store_lists()})
        log("saved snapshot %s" % msg["name"])

    # ---- the HTTP side ---------------------------------------------------

    async def ws_page(self, request):
        """One control websocket. `?quiet=1` leaves out what only a map draws
        (LOUD), which is most of the traffic on a busy network and nothing a
        driver or the front's registry reads."""
        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await socket.prepare(request)
        self.pages[socket] = request.query.get("quiet", "") not in ("", "0")
        self.role_wake.set()
        await socket.send_str(json.dumps(self.snapshot()))
        # A socket's messages are handled in the order they came, by a worker
        # of its own, so that its `yield` is taken at once even while
        # something it asked for is still being done (which may need T).
        work = asyncio.Queue()

        async def worker():
            while True:
                msg = await work.get()
                if msg is None:
                    return
                await self.handle(msg)
        handling = asyncio.ensure_future(worker())
        try:
            async for message in socket:
                if message.type is WSMsgType.TEXT:
                    try:
                        msg = json.loads(message.data)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    kind = msg.get("type")
                    if kind == "drive":
                        if self.driver is not None and self.driver is not socket:
                            self.give_floor()
                        self.driver = socket
                        self.driver_ids.clear()
                        self.floor_turn = 0         # counted from here, as the driver counts
                        self.take_floor()
                        continue
                    if kind == "yield":
                        # A yield sent before the driver saw an answer since
                        # given lets go of nothing: that answer's floor is
                        # its own, until it yields again.
                        if socket is self.driver and msg.get("turn", self.floor_turn) == self.floor_turn:
                            self.driver_yields()
                        continue
                    if socket is self.driver:
                        # Each of the driver's requests is set going at
                        # once, in order, so its `yield` finds them all under
                        # way (and one waiting on T does not hold up the rest).
                        if msg.get("id") is not None:
                            self.driver_ids.add(msg["id"])
                        asyncio.ensure_future(self.handle(msg))
                        continue
                    work.put_nowait(msg)
        finally:
            self.pages.pop(socket, None)
            if socket is self.driver:
                self.give_floor()
                self.driver = None
                self.driver_ids.clear()
            work.put_nowait(None)
            try:
                await handling
            except Exception:           # noqa: BLE001 - a handler's own error, logged there
                pass
        return socket

    async def ws_console(self, request):
        """The station's pty over a websocket.

        A **binary** frame is keystrokes and goes to the pty as it stands; a
        **text** frame is a control message, of which there is one — `resize`,
        carrying the terminal's size. Splitting them by frame type rather than
        by an escape in the stream means no byte a person can type is special.
        """
        name = request.match_info["name"]
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        self.consoles.setdefault(name, set()).add(socket)
        self.watched(name)
        try:
            async for message in socket:
                station = self.stations.get(name)
                if station is None:
                    continue
                if message.type is WSMsgType.BINARY:
                    station.write(message.data)
                elif message.type is WSMsgType.TEXT:
                    try:
                        control = json.loads(message.data)
                    except ValueError:
                        continue
                    if control.get("type") == "resize":
                        station.resize(int(control.get("cols", 80)),
                                       int(control.get("rows", 24)))
        finally:
            self.consoles.get(name, set()).discard(socket)
            self.watched(name)
        return socket

    async def ws_signalling(self, request):
        """Stand in the middle of a station's WebRTC signalling, so the SDP
        answer can be pointed at the relay (webrtc.bridge)."""
        label = proxy.label_of(request.headers.get("Host", "").encode("latin-1"))
        name = self.node_for_label(label)
        if name is None:
            return web.Response(status=404, text="no such station\n")
        driver = self.driver_of(name)
        if driver is None or driver.web_port() is None:
            return web.Response(status=404, text="no web UI on this station\n")
        addr = stations_module.bind_addr(self.nodeset.nodes[name]["id"])
        return await webrtc_module.bridge(
            request, "http://%s:%d%s" % (addr, driver.web_port(), SIGNAL_PATH),
            self.relay, addr, WEBRTC_PORT, label)

    async def api_store(self, request):
        return web.json_response({**store_lists(),
                                  "run": self.run.as_dict() if self.run else None})

    def app(self):
        app = web.Application()
        app.router.add_get(SIGNAL_PATH, self.ws_signalling)
        app.router.add_get("/ws", self.ws_page)
        app.router.add_get("/ws/console/{name}", self.ws_console)
        app.router.add_get("/api/store", self.api_store)
        app.router.add_get("/{tail:.*}", serve_page)
        return app

    async def start_http(self):
        """The control app on a loopback port, and the front listener on the bind.

        The app is never bound to the outside: the front listener is what the
        world reaches, and it dials the app for anything that is not a station.
        One port, and `Host` decides.
        """
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.control_port = self.runner.addresses[0][1]

        host, _, port = self.args.bind.rpartition(":")
        self.front = await proxy.serve(host or "0.0.0.0", int(port),
                                       self.resolve_host)
        webrtc_module.log = log
        self.relay = await webrtc_module.serve(
            host or "0.0.0.0", self.args.relay_bind_port,
            self.args.relay_host, self.args.relay_port)
        log("webrtc relay on udp/%d, offered to browsers as %s:%d"
            % (self.args.relay_bind_port, self.args.relay_host,
               self.args.relay_port))
        log("control page on http://localhost:%s/" % self.args.public_port)
        log("stations at http://<name>.sim.localhost:%s/" % self.args.public_port)

    # ---- the run ---------------------------------------------------------

    async def run_forever(self):
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(
                    sig, lambda: done.done() or done.set_result(None))

        await self.start_ether()
        if os.path.isfile(os.path.join(self.args.run, runs_module.RUN_FILE)):
            try:
                await self.adopt(runs_module.open_run(self.args.run))
            except (store.StoreError, drivers_module.CommandError, OSError) as err:
                log("error: cannot load the run in %s: %s" % (self.args.run, err))
        await self.start_http()
        self.poller = asyncio.ensure_future(self.poll_roles())
        self.clock_watch = asyncio.ensure_future(self.watch_clock())
        try:
            await done
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Stop everything, in the order that lets each step finish.

        The aiohttp side goes first because it owns the websockets, and a
        proxied websocket is a front-listener handler that will not return
        until its connection does — closing the front first would wait on a
        page that is still open, for as long as it stayed open. Every wait
        here is bounded for the same reason: a testbed told to stop stops, and
        the sockets go with the process regardless.
        """
        self.stopping = True
        log("stopping")
        for task in (self.poller, self.clock_watch, self.rows_task):
            if task is not None:
                task.cancel()
        await self.stop_all()
        if self.runner is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.runner.cleanup(), SHUTDOWN_TIMEOUT_S)
        if self.front is not None:
            self.front.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.front.wait_closed(), SHUTDOWN_TIMEOUT_S)
        if self.relay is not None:
            self.relay.close()
        if self.ether is not None:
            self.ether.close()



# How far ahead of T 0 a drifting clock's profile reaches: thirty days, past
# any run, since past its last point node time runs at T's own rate again.
CLOCK_HORIZON_US = 30 * 86_400 * 1_000_000


def drift_profile(ppm, horizon_us=CLOCK_HORIZON_US):
    """A `SIM_MESH_CLOCK_PROFILE` for a crystal `ppm` parts per million fast (or
    slow, below zero): node time running that much ahead of T from T 0."""
    return "0:0,%d:%d" % (horizon_us, horizon_us + round(horizon_us * ppm * 1e-6))


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="the simulated testbed: ether, stations, proxy and control page")
    ap.add_argument("--bind", default="0.0.0.0:9011",
                    help="host:port for the page and the stations (default 0.0.0.0:9011)")
    ap.add_argument("--ether", default="127.0.0.1:7000",
                    help="host:port for the ether's UDP endpoint")
    ap.add_argument("--run", default=DEFAULT_RUN,
                    help="the run directory: loaded at start when it holds a run "
                         "(run.yaml), and where a run loaded later goes, or beside "
                         "it as <dir>-2, -3… (default %s)" % os.path.relpath(DEFAULT_RUN))
    ap.add_argument("--build",
                    help="an installed firmware (<name> or <base>_latest) that every "
                         "node the script gives a firmware runs instead")
    ap.add_argument("--build-tag", metavar="TAG",
                    help="with --build: only the nodes carrying this tag run it")
    ap.add_argument("--sidecar",
                    help="the planner-web base URL a pack's rows are recomputed "
                         "through after a move")
    ap.add_argument("--noise-figure", type=float, default=ether_module.DEFAULT_NOISE_FIGURE_DB,
                    help="the receivers' noise figure in dB (default %g)"
                         % ether_module.DEFAULT_NOISE_FIGURE_DB)
    ap.add_argument("--pairwise", action="store_true",
                    help="rule on collisions pairwise, per interferer by the capture "
                         "margin, instead of on the summed interference")
    ap.add_argument("--clock-ppm", type=float, default=0.0, metavar="PPM",
                    help="in a virtual-time run, each station's crystal off by a draw "
                         "uniform within this many parts per million, from the seed "
                         "and its name (default 0: every clock true)")
    ap.add_argument("--bench-capture", action="store_true",
                    help="rule on two frames of one spreading factor as a bench saw them "
                         "meet, instead of by the same-SF figure")
    ap.add_argument("--fading-db", type=float, default=ether_module.DEFAULT_FADING_DB,
                    help="fading: the spread in dB of every link's level over time "
                         "(default %g: none)" % ether_module.DEFAULT_FADING_DB)
    ap.add_argument("--coherence-s", type=float,
                    default=ether_module.DEFAULT_COHERENCE_S,
                    help="fading: how long, in seconds, a link's level stays alike "
                         "(default %g)" % ether_module.DEFAULT_COHERENCE_S)
    ap.add_argument("--rician-k", type=float, default=ether_module.DEFAULT_RICIAN_K,
                    metavar="K",
                    help="fast fading: one Rician draw per frame at each receiver, of "
                         "this K factor, 0 for Rayleigh (default: none)")
    ap.add_argument("--no-interference", action="store_true",
                    help="an oracle: the ether judges every frame against noise alone "
                         "and never takes a receiver off the frame it follows; what "
                         "overlapping frames cost a run is its delivery with this less "
                         "without")
    ap.add_argument("--seed", type=int,
                    help="the ether's seed, which its welcome hands every station "
                         "(default: drawn at random)")
    ap.add_argument("--epoch", type=int, metavar="US",
                    help="in a virtual-time run, the wall-clock microseconds T 0 "
                         "stands for (default: the wall clock when the ether "
                         "starts); two runs given the same one give their "
                         "stations the same wall clock")
    ap.add_argument("--relay-port", type=int, default=0,
                    help="the UDP port a browser sends the DataChannel to, as "
                         "the browser sees it (default: the bind port)")
    ap.add_argument("--stagger", type=float, default=60.0,
                    help="seconds to spread a whole nodeset's start over, so the "
                         "stations do not boot and announce in lockstep "
                         "(default 60)")
    ap.add_argument("--relay-host", default="127.0.0.1",
                    help="the address a browser sends the DataChannel to "
                         "(default 127.0.0.1)")
    ap.add_argument("--time", default="real",
                    help="real (default): the stations and the medium run on the "
                         "wall clock; max: virtual time, as fast as the stations "
                         "allow; <k>x: virtual time paced at k times the wall")
    ap.add_argument("--net", default=stations_module.NET,
                    help="the network the stations' addresses come from, one "
                         "/24 at a time with hosts 5 to 254 (default %s: 1000 "
                         "stations); a second testbed on the same host needs "
                         "its own, 127.0.4.0/22 say" % stations_module.NET)
    args = ap.parse_args(argv)
    try:
        stations_module.set_net(args.net)
        ether_module.parse_time_mode(args.time)
    except ValueError as err:
        ap.error(str(err))
    if args.bench_capture and args.pairwise:
        ap.error("--bench-capture is a variant of the receiver-centred rule, not --pairwise's")
    try:
        ether_module.Physics(args.noise_figure, fading_db=args.fading_db,
                             coherence_s=args.coherence_s, rician_k=args.rician_k)
    except ValueError as err:
        ap.error(str(err))
    if not 0 <= args.clock_ppm <= 1000:
        ap.error("--clock-ppm is parts per million, 0 to 1000")
    if args.clock_ppm and args.time == "real":
        ap.error("--clock-ppm needs virtual time: in real time a station's clock is the host's")
    args.run = os.path.abspath(args.run)
    args.public_port = args.bind.rpartition(":")[2]
    # The relay binds the same number as the page, on UDP — one number to
    # publish and one to remember. What the browser is told may differ, since
    # the container's mapping is the host's business, not simd's.
    args.relay_bind_port = int(args.public_port)
    if not args.relay_port:
        args.relay_port = args.relay_bind_port
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(Simd(args).run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
