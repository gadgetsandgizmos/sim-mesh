#!/usr/bin/env python3
"""The front: several simulations, the editors, the scripts and the planner behind one port.

```
browser ── ws   localhost:8800/ws ──────────────────► front ── ws ──► child simd (lora)
browser ── http alpha.lora.sim.localhost:8800 ──────► front ───────► child (lora) ─► alpha :80
browser ── ws   alpha.lora.sim.localhost:8800/webrtc ► front ── ws ─► child ─────────► alpha
browser ── udp  localhost:8800 ─────────────────────► front ── udp ► child ── udp ──► alpha
browser ── http localhost:8800/planner/<geodata>/… ─► front ── http ► planner-web (that pack)
browser ── http localhost:8800/api/table?path=… ────► front          a loss table file (links layer)
browser ── http localhost:8800/api/coverage?… ──────► front          a node's coverage raster
browser ── POST localhost:8800/api/firmware/add ────► front          a firmware zip, installed
front ── GET sim-mesh.net/firmware/index.html ─────► site            what is pre-built
front ── GET sim-mesh.net/firmware/<name>.zip ─────► site            one, installed
browser ── POST localhost:8800/api/geodata/import ──► front          a geodata pack zip, expanded
browser ── GET  localhost:8800/api/geodata/export ──► front          a geodata pack zip, made as it goes
browser ── GET  localhost:8800/osm/<z>/<x>/<y>.png ► front ── (cache miss) ─► tile.openstreetmap.org
browser ── GET  localhost:8800/api/nominatim?q= ───► front ─────────────────► nominatim.openstreetmap.org
front ── GET /api/v1/nodes (weekly) ───────────────► map.meshcore.io  for nodeset_import
front ── planner-job nodes-import, JSON on stdin ──► planner-job      the nodes inside the geodata
front ── GET <index address> ───────────────────────► its host       index_list (indexes.py)
front ── GET <entry url> ───────────────────────────► its host       index_install, checked by sha256
browser ── GET  localhost:8800/api/geodata/sources ► front          what a rectangle's build takes
browser ── POST localhost:8800/api/geodata/build ──► front          a build started (DELETE cancels)
front ── fetch into geodata/.cache/<source>/ ──────► source hosts     (sources.py)
front ── planner-job pack-build, JSON on stdin ────► planner-job      (packbuild.py)
front ── geodata_progress {build} ─► every page     as it fetches and compiles
driver  ── ws   localhost:8800/ws?sim=lora  {type: "plan", ...} ─────► child

page ── sim_new {geodata, nodeset, script?, time} ──► front
front ── planner-web --pack P --port <free> ───────► sidecar          a pack, first user of P
front ── losses.py --geodata --nodeset --band B ───► subprocess ── GET /link.json × pairs ─► sidecar
front ── losses_progress {sim, band, done, total} ─► every page     as the subprocess reports
front: runs.create_run → runs/<sim>/ (geodata, nodeset, script, losses/<band>.bin, builds)
front ── simd.py --run runs/<sim> --sidecar URL ───► child
front ── sim_new {ok, name, control, run, …} ─► asker

page ── script_run {name, geodata, nodesets} ──► front: a name for its simulation, <sim>
front ── python -m sim_mesh.runner <script> --geodata G --nodeset N… --name <sim> ──► runner
front ── script_run {run, simulation: <sim>} ──► page          which goes over to <sim>
runner: the script from its top; sim_speed(), .firmware(), .on_first_boot() collected
runner ── sim_new {name: <sim>, …, time, firmware_rules, first_boot_rules} ──► front
runner ── ws localhost:8800/ws?sim=<sim> ─► front ─► child     what the script does
runner ── sim_pause {name, by: script} ─► front               when the script says sim_pause()
front: stop the child, runs.pause_run → runs/<sim>/paused/    the registry keeps it, done
runner: report(run_dir) → runs/<sim>/report.md                when the script has a report
front ── script_output {run, line} … script_exit {run, code} ─► every page
page ── sim_resume {name} ──► front: runs.resume_run → runs/<sim>-N/, a child on it
```

One front process owns the port the container publishes. It keeps the
registry of simulations, gives each one a control port, an ether port, a
loopback /22 and a run directory, and starts one **child** per simulation:
an ordinary simd, as a subprocess with the flags a person would give it by
hand. A child keeps its own event loop and its own pace, and a crash in one
simulation ends that one; the registry keeps it, exited, with the last lines
it wrote, until it is stopped.

**Before a child starts** its run is made whole here: the geodata and the
nodeset are loaded and checked, the loss table of every band the nodes'
carriers use is taken from the cache or computed into it by `losses.py` as a
subprocess, whose progress goes to every page, and the run directory is laid
out by `runs.create_run` (or `runs.load_snapshot`). The child is then
started on that directory and loads it itself, settling each node's firmware
from its script's `.firmware(…)` declarations.

**The planner sidecar.** One `planner-web` per pack in use, on
`127.0.0.1:<free>`: started when the first simulation or page socket opens
geodata on that pack, stopped SIDECAR_GRACE_S after the last one lets go
unless one holds it again by then, as a reloaded page does. It is sim-mesh's own,
built from `planner/` (by `sim` as it starts) to
`planner/target/release/planner-web`; not built, a pack is refused with
NO_PLANNER and synthetic ground works. The page reaches it as `/planner/<geodata>/…`, passed through with
the prefix stripped; a child is given its URL directly, for recomputing a
moved node's row. It is given the coverage cache (`--coverage-dir`), whose
rasters it combines into the bands of the view the page shows
(`/coverage/bands.bin`).

**Script runs.** A script runs as a process of its own (`sim_mesh.runner`),
its output kept (the last `SCRIPT_LINES` lines) and sent to every page as it
comes. On a new simulation it starts that itself, through `sim_new` on this
same port, with its time and its firmware and first-boot rules, under the
name the front chose when it was asked to run it, so the page can go to it
at once; or it runs on a simulation already running. Either way the
simulation runs on when the script ends, unless the script paused or
stopped it; a paused one stays in the registry until it is resumed (in a
new run directory, from its state) or removed. A script's
`report(run_dir)` then writes the run's `report.md`, which `/api/report`
serves.

The front's control websocket speaks the child's messages (simd.py lists
them) and these of its own, each answered to the asking socket as
`{type: <verb>, ok: true, …}` or `{type: <verb>, ok: false, error}`:

```
sims                                                      the registry, now
sim_new {name?, geodata, nodeset | nodesets, script?, time?, stagger?, build?, build_tag?,
         pairwise?, firmware_rules?, first_boot_rules?, inputs?}
sim_new {name?, snapshot, time?, stagger?, build?, build_tag?, pairwise?}
      → {ok, name, control, ether, net, run, time, geodata, nodeset, script, snapshot}
sim_stop {name}                   for good; a paused one's state deleted, ended → {ok, name}
sim_pause {name, by?}             stopped, its state kept in its run; `by: script` is its
                                  script's pause, which lists it as done  → {ok, name}
sim_resume {name, time?}          a paused or done one, from that state, real time unless
                                  `time` → as sim_new
run_delete {run, stop?}           a run's directory, gone; with `stop`, a simulation still on it
                                  is stopped first, else that is refused  → {ok, run}
select {sim}                                              which simulation the socket is on

firmware_list                     → {firmware: [row…], arch}      firmware.listing, each row with
                                    the paused runs and snapshots that hold it (`users`) and
                                    its size on disk (`bytes`), and the paused simulations among
                                    them by simulation name (`paused`)
firmware_prebuilt                 → {firmware: [row…], index}     what sim-mesh.net offers this
                                    machine, each row saying whether it is installed
firmware_add {url}                → {firmware: row}               a pre-built zip, installed
firmware_delete {names, stop_paused?}
                                  → {deleted, stopped}            refused for any one a snapshot
                                    holds, and for any one a paused simulation holds unless
                                    `stop_paused`, which stops those simulations for good first
antenna_list                      → {antennas: [antenna…]}       antennas.catalogue
geodata_list                      → {geodata: [{name, kind, bbox, licences, bytes, from_index, …}
                                     | {name, error, bytes}], build: the running or failed
                                     build's row, or null}   from_index: the index it came
                                     from (indexes.geodata_origin), or null
geodata_sources                   → {sources: [{source, what, licence, holds, layers, where,
                                     worldwide, own, map, bytes}],
                                     building}   the build's cache, one row per source, and the
                                    map's tiles, the sources as the source files say them
                                    (sourcefile.py); `holds` what its files hold, `layers`
                                    {layer: priority}, `where` global or continent › country,
                                    `own` from testbed/sources.yaml, `map` whether it has an
                                    area to draw
geodata_source_map {source}       → {source, worldwide, covers, covers_from, cached,
                                     cached_files, error}   where it has data and what of it
                                    is cached, GeoJSON in degrees (sources.source_map)
geodata_sources_at {lon, lat}     → {lon, lat, sources: {source: {has, cached, error}}}
                                    which sources have data at a point, and whether that
                                    part is cached (sources.sources_at)
geodata_source_clear {source}     → {source}   that source's cache emptied; refused while a
                                    build runs
index_list                        → {indexes: [{name, address, own, title, description,
                                     error?, geodata, nodesets}], fetching: [row…]}
                                     every listed index, its description its text about
                                     the collection, each
                                     entry with `installed`, `taken`, and for a nodeset `changed`
index_new {address}               → {index}    another index listed (indexes.add_index)
index_delete {name}               → {names}    an added index forgotten
index_install {index, kind, name} → {kind, name, installed: [{kind, name}…]}   when it is
                                    in; its progress goes to every page as index_progress
index_cancel {index, kind, name}  → {}         a fetch under way stopped
geodata_open {name}               → {geodata, planner}   holds the sidecar for this socket
geodata_close                                            lets it go
geodata_new {name, pack | synthetic: {terrain, exponent, extent_m}}   → {geodata}
geodata_save {name, data} · geodata_save_as {name, data}              → {geodata}
geodata_rename {name, to}         → {name}     refused while anything stands on its pack
geodata_delete_plan {names}       → {geodata, runs, snapshots, nodesets, running}   what a
                                    delete takes with it (geodata.taken_with, and the runs of
                                    the simulations on it), said before it is done
geodata_delete {names}            → {deleted}  the geodata and all its plan names gone,
                                    simulations on those runs stopped first
nodeset_list {geodata?}           → {nodesets: [{name, nodes, bbox, tags, bytes, from_index,
                                     inside?} | {name, error, bytes}]}
                                    inside: whether a node stands in that geodata's extent;
                                    from_index: the index it came from, `changed` when edited
                                    since
nodeset_open {name}               → {nodeset}
nodeset_new {name}                → {nodeset}
nodeset_save {name, data} · nodeset_save_as {name, data}              → {nodeset}
nodeset_import {name, source: sites|nodes, text | path, height_m?}  → {nodeset}
nodeset_import {name, source: csv|geojson|kml|gpx|meshtastic, text, columns?, geodata?,
                height_m?}   → {nodeset}   a file of points (nodeset.import_points); a CSV's
                                    `columns` name which of its columns is lat, lon, name,
                                    height, power and tags
nodeset_import {name, source: meshcore|potatomesh, geodata, url?, companions?, max_age_days?,
                height_m?}   → {nodeset, report}   a public node map inside the
                                    geodata's extent, through planner-job nodes-import
nodeset_heights {name, geodata}   → {nodeset, changed, estimates}   on a pack, every node
                                    whose height is assumed given planner's estimate
                                    (the sidecar's /height.json): `roof`, `raster`, or
                                    kept where it found nothing
nodeset_merge {name, layers: [{name, data}]}  → {nodeset}   the nodesets checked on the Nodes
                                    tab, earlier in its list first, merged into a new nodeset
                                    (nodeset.merge)
nodeset_setup_open {name}         → {name, text, exists}   its own setup, nodesets/<name>.py;
                                    one it has not got yet reads as a fresh one's text
nodeset_setup_save {name, text}   → {name, text, exists}   checked to parse, then written
script_list                      → {scripts: [{name, doc, report, inputs, references,
                                    included_by, example} …]}   inputs: what the script asks for
                                    ([{name, type, label, category?, default?}]);
                                    included_by: the scripts that include or import it;
                                    one some script includes is not run on its own;
                                    example: one sim-mesh ships, which script_save refuses
module_open {path}                → {path, text}   a file a script imports (the library's,
                                    or another script), by its path under testbed/
script_open {name}                → {script, text}
script_new {name, text?} · script_save {name, text} · script_save_as {name, text}
                                  → {script, text}
script_run {name, sim | resume | geodata, nodeset | nodesets, build?, build_tag?, inputs?}
                                  → {run, simulation}
                                  a new simulation is the script's own, started by
                                  it; several nodesets are merged as nodeset_merge
                                  merges them; `inputs` {name: value} for the
                                  script's inputs
script_stop {run}                 → {}
script_log {run}                  → {run, lines}
snapshot_list                     → {snapshots: [{name, t, run, geodata, nodeset, script, bytes,
                                     …}]}
losses_compute {geodata, nodeset, bands?}  → {tables: {band: {path, cached}}}
links {geodata, node, nodes: {name: record}, band?}  → {node, band, f0_hz, cells: {other:
                                    {to, from, flags}}}   one node's row and column from the
                                    nodes as the page has them, saved or not; null never heard
coverage {geodata, nodes: [{name, lat, lon, height_m}]}  → {tiles: [{node, key, cached}]}
```

and these unasked:

```
front → socket        hello {front: true, port}                    on connect
front → all sockets   sims {port, sims: [...], script_runs: [...], geodata_names,
                            nodesets, scripts, snapshots, globals, globals_error}
                                                                   on change and once a second
                      `globals`: scripts/globals.py's shared settings (script.SHARED),
                      null with `globals_error` saying why when it cannot be read
                      a sim's state: starting, running, stopping, exited, paused, or
                      ended (a run on disk that is neither; named by its run directory);
                      a paused one's `paused_by` is `script` when its script paused it
                      (done); `bytes`: its run directory's size, measured on a worker
                      thread, a running one's every SIZE_EVERY_S;
                      `report` on a sim or a script run: its run has a report.md;
                      `started`, `ended`: wall seconds (`ended` null while it runs),
                      `t`: its T now, or where it stopped, in microseconds
front → all sockets   losses_progress {sim | nodeset, band, done, total}
front → all sockets   geodata_progress {build: {name, state, step, done, total, fetched, of,
                                                error, spec} | null}
front → all sockets   script_output {run, line} · script_exit {run, code}
front → all sockets   firmware_changed {}                          firmware added or deleted
front → all sockets   index_progress {index, kind, name, state, now, fetched, of, error}
                                                                   an entry being installed: state
                                                                   fetching, done, failed or
                                                                   cancelled; `now` the kind and
                                                                   name being fetched (a
                                                                   nodeset's geodata first)
front → asker         coverage_tile {geodata, node, key} · coverage_error {geodata, node, error}
                      · coverage_band {geodata, node, key, km}: the edited node's sweep, out to km
anything else         → the child named by `sim`, or the selected one
child → socket        the child's own message, with `sim` added
```

`pairwise: true` in `sim_new` starts that simulation's ether on the pairwise
rule (simd's `--pairwise`); the front's own `-- --pairwise` does it for every
simulation.

`sim_load {geodata, nodeset, script?}` and `snapshot_load {name}` sent to a
running simulation pass through the front on their way: it computes the
tables and holds the sidecar first, with progress as for `sim_new`, and adds
`sidecar` before handing the message on.

`?sim=<name>` on the websocket selects that simulation from the start, and
`?quiet=1` asks every child for its quiet stream (no tx, rx, radio or levels).

Station hostnames carry the simulation: `<station>.<sim>.sim.localhost`. The
front routes on the second label and the child on the first. A bare
`<station>.sim.localhost` reaches the one simulation while exactly one runs. The
WebRTC relay is the child's own mechanism, one level up: the front terminates
the signalling, points the answer at itself, and forwards the UDP flow to the
child's relay.

Nothing here blocks: the children's, the sidecars', the scripts' and the loss
computation's output is read as it comes, files are laid out and zips
expanded on a worker thread, and every wait is an awaitable.
"""

import argparse
import asyncio
import collections
import contextlib
import ctypes
import fcntl
import ipaddress
import itertools
import json
import math
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
import urllib.parse

import aiohttp
from aiohttp import WSMsgType, web

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SIM_DIR)

import antennas as antennas_module  # noqa: E402 - the path is set just above
import coverage as coverage_module  # noqa: E402
import drivers as drivers_module    # noqa: E402
import firmware as firmware_module  # noqa: E402
import geodata as geodata_module    # noqa: E402
import indexes as indexes_module    # noqa: E402
import losses as losses_module      # noqa: E402
import nodeset as nodeset_module    # noqa: E402
import packbuild as packbuild_module   # noqa: E402
import proxy                        # noqa: E402
import runs as runs_module          # noqa: E402
import script as script_module      # noqa: E402
import simd as simd_module          # noqa: E402
import sources as sources_module    # noqa: E402
import store                        # noqa: E402
import webrtc as webrtc_module      # noqa: E402

SIGNAL_PATH = simd_module.SIGNAL_PATH
LOSSES_PY = os.path.join(SIM_DIR, "losses.py")
PLANNER_WEB = os.path.join("target", "release", "planner-web")
PLANNER_JOB = os.path.join("target", "release", "planner-job")
NO_PLANNER = ("geodata %s is a pack, and planner-web is not built: `sim` builds it as it "
              "starts, and said why it could not")
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_TILES_DIR = os.path.join(SIM_DIR, "osmtiles")
OSM_TILE_MAX_AGE_S = 7 * 86400      # the tile usage policy's floor for keeping one
OSM_MAX_ZOOM = 19
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_GAP_S = 1.0               # its usage policy: one request a second at most
MESHCORE_NODES = "https://map.meshcore.io/api/v1/nodes"
MESHCORE_META = "meshcore-nodes.json"
MESHCORE_MAX_AGE_DAYS = 365         # an advert older than this is a clock never set, or a node gone
POTATOMESH_NODES = "/api/nodes?limit=1000"
# The imports that are a file the page hands over: the planner's CSVs, and
# files of points (nodeset.import_points).
FILE_IMPORTS = ("sites", "nodes") + nodeset_module.POINT_FORMATS

CONTROL_PORTS = range(9100, 9200)   # a child's page, proxy and relay (TCP and UDP)
ETHER_PORTS = range(7100, 7200)     # a child's ether
NETS = range(4, 64)                 # 127.<4k>.0.0/22; below 127.16 is left for simd by hand
NET_LOCK_DIR = os.path.join(tempfile.gettempdir(), "sim-mesh-nets")   # one lock per /22, host-wide
READY_TIMEOUT_S = 30.0              # how long a child has to open its port
STOP_TIMEOUT_S = 15.0               # how long a child has to stop before it is killed
SIDECAR_READY_S = 60.0              # how long a planner-web has to answer /api/pack
SIDECAR_GRACE_S = 30.0              # how long one nobody holds is kept: a page reloading
                                    # lets go of it and opens it again a moment later
SIDECAR_POLL_S = 0.2
REPORT_S = 1.0                      # how often every socket hears the registry
PACE_WINDOW_S = 120.0               # the wall the estimate's pace is taken over
TAIL_LINES = 40                     # a child's last lines, kept for the page
LINK_CELLS_KEPT = 200_000           # pairs the links verb keeps, oldest dropped first
SCRIPT_LINES = 2000                 # a script run's last lines, kept for the page
ERRORS_KEPT = 5
SIZE_EVERY_S = 10.0                 # how often a running simulation's run directory is measured
PROGRESS_EVERY_S = 0.25             # how often an index download's progress is told
MAX_UPLOAD = 4 << 30                # a pack zip is hundreds of megabytes
EXPORT_CHUNK = 1 << 20              # an export's zip goes out a megabyte at a time,
EXPORT_QUEUE = 8                    # at most this many ahead of the reader
# What `--page-dev` answers while the page's development server is not there yet.
PAGE_DEV_WAITING = ("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=1>"
                    "<title>sim-mesh</title><body style='background:#121417;color:#9ca3af;"
                    "font:14px system-ui;padding:24px'>The page's development server is "
                    "starting (sim dev)…</body>")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

SIM_VERBS = ("sim_new", "sim_stop", "sim_pause", "sim_resume", "run_delete")
EDITOR_VERBS = (
    "firmware_list", "firmware_prebuilt", "firmware_add", "firmware_delete", "antenna_list",
    "module_open",
    "geodata_list", "geodata_open", "geodata_close", "geodata_new", "geodata_save",
    "geodata_save_as", "geodata_rename", "geodata_delete", "geodata_delete_plan",
    "geodata_sources",
    "geodata_source_clear", "geodata_source_map", "geodata_sources_at",
    "index_list", "index_new", "index_delete", "index_install", "index_cancel",
    "nodeset_list", "nodeset_open", "nodeset_new", "nodeset_save", "nodeset_save_as",
    "nodeset_delete",
    "nodeset_import", "nodeset_merge", "nodeset_setup_open", "nodeset_setup_save",
    "nodeset_heights",
    "script_list", "script_open", "script_new", "script_save", "script_save_as",
    "script_run", "script_stop", "script_log",
    "snapshot_list", "losses_compute", "links", "coverage")
QUIET_VERBS = ("firmware_list", "firmware_prebuilt", "antenna_list", "module_open",
               "geodata_list", "geodata_sources", "geodata_source_map", "geodata_sources_at",
               "index_list",
               "nodeset_list", "script_list", "snapshot_list", "geodata_open", "nodeset_open",
               "nodeset_setup_open", "script_open", "geodata_close", "script_log", "links", "coverage")

PR_SET_PDEATHSIG = 1


def log(msg):
    sys.stderr.write("front: %s\n" % msg)
    sys.stderr.flush()


def die_with_parent():
    """In a child, before exec: go when the front goes, however it goes.

    A front killed outright would otherwise leave its simulations and
    sidecars running with nothing in front of them and their ports taken.
    """
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def port_free(port):
    """Whether both TCP and UDP `port` can be bound on loopback just now."""
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        with socket.socket(socket.AF_INET, kind) as probe:
            if kind == socket.SOCK_STREAM:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
    return True


def bound_addresses():
    """Every IPv4 address a TCP or UDP socket on this host is bound to just
    now, from /proc/net: what another front's stations, or a simd started by
    hand, already stand on. Empty where /proc/net is not there to read."""
    found = set()
    for table in ("/proc/net/tcp", "/proc/net/udp"):
        try:
            with open(table, encoding="ascii") as handle:
                next(handle, None)
                for line in handle:
                    local = line.split()[1].partition(":")[0]
                    found.add(socket.inet_ntoa(int(local, 16).to_bytes(4, "little")))
        except (OSError, ValueError, IndexError):
            continue
    return found


def net_free(net, bound):
    """Whether no address of this network is among `bound`."""
    network = ipaddress.ip_network(net)
    return not any(ipaddress.ip_address(a) in network for a in bound)


def claim_net(net):
    """This front's claim on a /22, host-wide: an open descriptor holding an
    exclusive `flock` on `NET_LOCK_DIR/<net>.lock`, or None when another
    front (or another simulation of this one) holds it or the lock cannot be
    taken.

    A block is given out well before its stations bind it: the loss table
    and the child's start come between. Two fronts asking in that window
    would both find it unbound, so the claim is a lock rather than a look.
    It never waits, and it goes with the descriptor: closed when the
    simulation ends, and by the kernel when the front dies however it dies,
    so a stale file claims nothing.
    """
    try:
        os.makedirs(NET_LOCK_DIR, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(NET_LOCK_DIR, 0o1777)
        fd = os.open(os.path.join(NET_LOCK_DIR, net.replace("/", "_") + ".lock"),
                     os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
    except OSError as err:
        log("network %s: no lock in %s (%s)" % (net, NET_LOCK_DIR, err))
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def any_free_port():
    """A TCP port on loopback nothing holds just now, for a sidecar."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def planner_web():
    """The planner-web executable, or None when there is no planner."""
    path = os.path.join(geodata_module.planner_repo(), PLANNER_WEB)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def planner_job():
    """The planner-job executable, which compiles a pack, or None."""
    path = os.path.join(geodata_module.planner_repo(), PLANNER_JOB)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def load_geodata(name):
    """Geodata by name, a pack refused with NO_PLANNER when there is no
    planner, before its manifest is read: the missing planner is the reason,
    and a pack left unreadable with it is not. With a planner, an unreadable
    manifest is reported as such."""
    return geodata_module.load(name, refuse_packs=None if planner_web() else NO_PLANNER)


def read_geodata(path, name=None):
    """A geodata file (a run's or a snapshot's copy), as `load_geodata`."""
    return geodata_module.read(path, name, refuse_packs=None if planner_web() else NO_PLANNER)


# ---------------------------------------------------------------------------
# The planner sidecars
# ---------------------------------------------------------------------------

class Sidecar:
    """One planner-web on one pack, and who is holding it."""

    def __init__(self, pack, port):
        self.pack = pack
        self.port = port
        self.url = "http://127.0.0.1:%d" % port
        self.process = None
        self.holders = set()
        # Its stop, while nobody holds it: called off when somebody does again.
        self.idle = None
        self.ready = asyncio.get_running_loop().create_future()
        self.log_path = os.path.join(store.RUNS_DIR, "planner-%s.log"
                                     % store.slug(os.path.basename(pack), "pack"))

    @property
    def up(self):
        return self.ready.done() and not self.ready.cancelled() \
            and self.ready.exception() is None


class Sidecars:
    """Every sidecar, by pack directory. A holder is a simulation or a
    control socket, and holds one pack at a time."""

    def __init__(self, session):
        self.session = session
        self.by_pack = {}

    def running(self, pack):
        car = self.by_pack.get(pack)
        return car if car is not None and car.up else None

    def url_for(self, gd):
        car = self.running(gd.pack_dir) if gd.is_pack else None
        return car.url if car else None

    async def hold(self, holder, gd):
        """The sidecar URL for this geodata, started if nobody runs one on its
        pack; None for synthetic ground. Whatever else the holder held it
        lets go."""
        if not gd.is_pack:
            self.release(holder)
            return None
        self.release(holder, keep=gd.pack_dir)
        car = self.by_pack.get(gd.pack_dir)
        if car is not None and car.idle is not None:
            car.idle.cancel()
            car.idle = None
        if car is None:
            binary = planner_web()
            if binary is None:
                raise store.StoreError(NO_PLANNER % gd.name)
            car = Sidecar(gd.pack_dir, any_free_port())
            self.by_pack[gd.pack_dir] = car
            asyncio.ensure_future(self.start(car, binary))
        car.holders.add(holder)
        try:
            await asyncio.shield(car.ready)
        except store.StoreError:
            car.holders.discard(holder)
            raise
        return car.url

    async def start(self, car, binary):
        os.makedirs(os.path.dirname(car.log_path), exist_ok=True)
        try:
            with open(car.log_path, "ab") as out:
                car.process = await asyncio.create_subprocess_exec(
                    binary, "--pack", car.pack, "--host", "127.0.0.1", "--port", str(car.port),
                    "--coverage-dir", store.COVERAGE_DIR,
                    stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out,
                    start_new_session=True, preexec_fn=die_with_parent)
        except OSError as err:
            self.failed(car, "planner-web: %s" % err)
            return
        log("planner-web pid %d on %s for %s (log %s)"
            % (car.process.pid, car.url, car.pack, os.path.relpath(car.log_path)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SIDECAR_READY_S
        while loop.time() < deadline:
            if car.process.returncode is not None:
                self.failed(car, "planner-web on %s exited with %s: see %s"
                            % (car.pack, car.process.returncode, car.log_path))
                return
            try:
                async with self.session.get(car.url + "/api/pack",
                                            timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        if not car.ready.done():
                            car.ready.set_result(car.url)
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass
            await asyncio.sleep(SIDECAR_POLL_S)
        self.failed(car, "planner-web on %s did not answer in %.0f s: see %s"
                    % (car.pack, SIDECAR_READY_S, car.log_path))
        await self.stop(car)

    def failed(self, car, why):
        log(why)
        if self.by_pack.get(car.pack) is car:
            del self.by_pack[car.pack]
        if not car.ready.done():
            car.ready.set_exception(store.StoreError(why))
            car.ready.exception()           # retrieved: a failure nobody awaited is not news

    def release(self, holder, keep=None):
        """`holder` lets go of every pack but `keep`; a sidecar nobody holds
        stops SIDECAR_GRACE_S later, unless somebody holds it again first: a
        page reloaded opens its geodata again before then, and finds the
        sidecar up, its building index loaded."""
        for pack, car in list(self.by_pack.items()):
            if pack == keep or holder not in car.holders:
                continue
            car.holders.discard(holder)
            if not car.holders and car.idle is None:
                car.idle = asyncio.get_running_loop().call_later(SIDECAR_GRACE_S, self.expire, car)

    def expire(self, car):
        """A sidecar nobody held again in its grace, stopped."""
        car.idle = None
        if car.holders or self.by_pack.get(car.pack) is not car:
            return
        del self.by_pack[car.pack]
        asyncio.ensure_future(self.stop(car))

    async def stop(self, car):
        if not car.ready.done():
            car.ready.set_exception(store.StoreError("the sidecar on %s was stopped" % car.pack))
            car.ready.exception()
        process = car.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        log("planner-web on %s stopped" % car.pack)

    async def close(self):
        cars = list(self.by_pack.values())
        self.by_pack.clear()
        for car in cars:
            if car.idle is not None:
                car.idle.cancel()
        await asyncio.gather(*(self.stop(car) for car in cars), return_exceptions=True)


# ---------------------------------------------------------------------------
# One simulation
# ---------------------------------------------------------------------------

class Child:
    """One simulation: its simd process, and what the front knows about it."""

    def __init__(self, front, name, port, ether_port, net, time_mode, stagger,
                 run_dir=None, sidecar=None, pairwise=False):
        self.front = front
        self.name = name
        self.port = port
        self.ether = "127.0.0.1:%d" % ether_port
        self.net = net
        self.time_mode = time_mode
        self.stagger = stagger
        self.pairwise = bool(pairwise)      # the ether's pairwise rule, for this one
        self.run_dir = run_dir or os.path.join(store.RUNS_DIR, name)
        self.sidecar = sidecar
        self.process = None
        self.state = "starting"             # starting, running, stopping, exited
        self.code = None
        self.started = time.time()
        self.ended = None                   # wall seconds, once it has exited
        self.ready = asyncio.get_running_loop().create_future()
        self.tail = collections.deque(maxlen=TAIL_LINES)
        self.errors = collections.deque(maxlen=ERRORS_KEPT)
        self.monitor = None                 # the front's own quiet socket to the child
        self.monitor_task = None
        self.reader = None
        # What the monitor has heard.
        self.loaded = {}                    # the run: geodata, nodeset, script, snapshot
        self.dirty = False
        self.clock = None
        self.nodes = {}                     # station name -> status
        self.samples = collections.deque()  # (wall, T) over the pace window

    @property
    def holder(self):
        return "sim:%s" % self.name

    # ---- the process -----------------------------------------------------

    async def start(self):
        os.makedirs(self.run_dir, exist_ok=True)
        argv = [sys.executable, "-u", os.path.join(SIM_DIR, "simd.py"),
                "--bind", "127.0.0.1:%d" % self.port,
                "--ether", self.ether,
                "--net", self.net,
                "--run", self.run_dir,
                "--time", self.time_mode]
        if self.stagger is not None:
            argv += ["--stagger", str(self.stagger)]
        if self.sidecar:
            argv += ["--sidecar", self.sidecar]
        if self.pairwise:
            argv += ["--pairwise"]
        argv += [a for a in self.front.child_args
                 if not (self.pairwise and a == "--pairwise")]
        # A process group of its own, which its stations inherit: a child that
        # dies without stopping them leaves them in it, and exited() sweeps it.
        self.process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, preexec_fn=die_with_parent)
        log("%s: simd pid %d on 127.0.0.1:%d, ether %s, net %s, run %s"
            % (self.name, self.process.pid, self.port, self.ether, self.net,
               os.path.relpath(self.run_dir)))
        self.reader = asyncio.ensure_future(self.read_output())

    async def read_output(self):
        """The child's log, line by line: to its file, to the tail, and the
        line that says its port is open is what makes it ready."""
        path = os.path.join(self.run_dir, "simd.log")
        with open(path, "a", buffering=1) as out:
            out.write("---- %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\n")
                out.write(line + "\n")
                self.tail.append(line)
                if not self.ready.done() and "control page on" in line:
                    self.ready.set_result(True)
        code = await self.process.wait()
        self.exited(code)

    def exited(self, code):
        was = self.state
        self.state = "exited"
        self.code = code
        self.ended = time.time()
        # Stations a crashed simd left behind would hold its addresses and its
        # ether port against the next simulation given them.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)
        if not self.ready.done():
            self.ready.set_result(False)
        if self.monitor_task is not None:
            self.monitor_task.cancel()
        if was != "stopping":
            log("%s: simd exited with %s" % (self.name, code))
        self.front.child_gone(self)

    async def stop(self):
        """SIGTERM, which simd takes as Ctrl-C; SIGKILL if that is not enough."""
        if self.process is None or self.process.returncode is not None:
            return
        self.state = "stopping"
        self.process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(self.reader), STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            log("%s: simd did not stop in %.0f s, killing it" % (self.name, STOP_TIMEOUT_S))
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            await self.reader

    # ---- the monitor -----------------------------------------------------

    def control_url(self, quiet=False):
        return "http://127.0.0.1:%d/ws%s" % (self.port, "?quiet=1" if quiet else "")

    async def watch(self):
        """The front's own quiet socket to the child: what the registry shows."""
        try:
            async with self.front.session.ws_connect(self.control_url(quiet=True),
                                                     max_msg_size=0) as ws:
                self.monitor = ws
                async for message in ws:
                    if message.type is WSMsgType.TEXT:
                        with contextlib.suppress(ValueError):
                            self.heard(json.loads(message.data))
        except (aiohttp.ClientError, OSError) as err:
            if self.state == "running":
                log("%s: lost the control socket (%s)" % (self.name, err))
        finally:
            self.monitor = None

    def heard(self, msg):
        kind = msg.get("type")
        if kind == "snapshot":
            run = msg.get("run") or {}
            self.loaded = {key: run.get(key) for key in ("geodata", "nodeset", "script",
                                                         "snapshot")}
            if run.get("dir"):
                self.run_dir = run["dir"]
            self.dirty = bool((msg.get("nodeset") or {}).get("dirty"))
            self.nodes = {n["name"]: n["status"] for n in msg.get("nodes") or ()}
            if msg.get("clock"):
                self.clocked(msg["clock"])
        elif kind == "node":
            self.nodes[msg["name"]] = msg.get("status")
        elif kind == "node_gone":
            self.nodes.pop(msg.get("name"), None)
        elif kind == "nodeset":
            self.dirty = bool(msg.get("dirty"))
        elif kind == "clock":
            self.clocked(msg)
        elif kind == "error":
            self.errors.append([time.time(), msg.get("text")])

    def clocked(self, clock):
        """Keep T against the wall for the last two minutes: the estimate's pace."""
        self.clock = clock
        now = time.monotonic()
        if self.samples and clock["t"] < self.samples[-1][1]:
            self.samples.clear()            # a new ether: T started again
        self.samples.append((now, clock["t"]))
        while len(self.samples) > 2 and now - self.samples[1][0] >= PACE_WINDOW_S:
            self.samples.popleft()

    def pace(self):
        """Seconds of T per second of wall over the window, or None."""
        if self.clock and self.clock.get("mode") != "virtual":
            return 1.0
        if len(self.samples) < 2:
            return None
        (w0, t0), (w1, t1) = self.samples[0], self.samples[-1]
        if w1 - w0 < 5.0 or t1 <= t0:
            return None
        return (t1 - t0) / 1e6 / (w1 - w0)

    # ---- the registry's row ---------------------------------------------

    def summary(self):
        counts = collections.Counter(self.nodes.values())
        row = {"name": self.name, "state": self.state, "code": self.code,
               "port": self.port, "ether": self.ether, "net": self.net,
               "run": os.path.relpath(self.run_dir, SIM_DIR), "time": self.time_mode,
               "started": self.started, "ended": self.ended, "geodata": self.loaded.get("geodata"),
               "nodeset": self.loaded.get("nodeset"), "script": self.loaded.get("script"),
               "snapshot": self.loaded.get("snapshot"), "dirty": self.dirty,
               "stations": len(self.nodes), "counts": dict(counts),
               "errors": list(self.errors), "pace": self.pace(),
               "report": os.path.isfile(os.path.join(self.run_dir, runs_module.REPORT_FILE)),
               "tail": list(self.tail)[-12:] if self.state == "exited" else []}
        clock = self.clock or {}
        row.update({"mode": clock.get("mode"), "rate": clock.get("rate"),
                    "observed": clock.get("observed"), "t": clock.get("t")})
        row.update(self.estimate())
        return row

    def estimate(self):
        """Where the run is in its plan, and when each part should end.

        The phase is the first whose end T has not been reached; it began where
        the one before it ended, or where the plan was given. The finish is the
        T left over the pace of the last two minutes of wall, from now.
        """
        clock = self.clock or {}
        plan, t = clock.get("plan"), clock.get("t")
        if not plan or t is None or not plan.get("phases"):
            return {"plan": None, "phase": None, "eta": None}
        phases = plan["phases"]
        begin, current = plan.get("t", t), None
        for index, phase in enumerate(phases):
            if t < phase["until"]:
                current = {"index": index, "name": phase["name"],
                           "from": begin, "until": phase["until"]}
                break
            begin = phase["until"]
        pace, now = self.pace(), time.time()
        end = phases[-1]["until"]
        eta = now + (end - t) / 1e6 / pace if pace and t < end else None
        if current is not None:
            current["eta"] = (now + (current["until"] - t) / 1e6 / pace) if pace else None
        return {"plan": phases, "plan_from": plan.get("t", t), "phase": current,
                "eta": eta, "done": t >= end}


# ---------------------------------------------------------------------------
# One script run
# ---------------------------------------------------------------------------

class ScriptRun:
    """A script, running as `sim_mesh.runner` (and then its `report`, when it
    has one, on its simulation's run). It starts its own simulation, under
    the name the front chose for it, on `world` (geodata, nodesets, build),
    or runs on the one named, `world` then None, or resumes the paused one
    named and runs on that, `world` then {"resume": True}."""

    def __init__(self, front, ident, name, sim, world=None, inputs=None):
        self.front = front
        self.id = ident
        self.name = name
        self.sim = sim
        self.world = world
        self.inputs = dict(inputs or {})    # the script's inputs, as the page gave them
        self.process = None
        self.state = "running"              # running, stopping, exited
        self.code = None
        self.started = time.time()
        self.lines = collections.deque(maxlen=SCRIPT_LINES)
        self.reader = None

    @property
    def run_dir(self):
        """Its simulation's run directory, once there is one."""
        child = self.front.children.get(self.sim)
        return child.run_dir if child is not None else None

    async def start(self, port):
        argv = [sys.executable, "-u", "-m", "sim_mesh.runner", script_module.script_path(self.name),
                "--port", str(port)]
        if self.world is None:
            argv += ["--sim", self.sim]
        elif self.world.get("resume"):
            argv += ["--resume", self.sim]
        else:
            argv += ["--geodata", self.world["geodata"], "--name", self.sim]
            for layer in self.world["nodesets"]:
                argv += ["--nodeset", layer]
            if self.world.get("build"):
                argv += ["--build", self.world["build"]]
            if self.world.get("build_tag"):
                argv += ["--build-tag", self.world["build_tag"]]
        for key, value in sorted(self.inputs.items()):
            argv += ["--set", "%s=%s" % (key, value)]
        env = dict(os.environ, SIM_MESH_PORT=str(port),
                   PYTHONPATH=os.pathsep.join(p for p in (SIM_DIR, os.environ.get("PYTHONPATH"))
                                              if p))
        env.pop("SIM_MESH_SIM", None)
        self.process = await asyncio.create_subprocess_exec(
            *argv, cwd=SIM_DIR, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, preexec_fn=die_with_parent)
        log("script %s (%s) on %s: pid %d" % (self.name, self.id, self.sim, self.process.pid))
        self.reader = asyncio.ensure_future(self.read_output())

    def said(self, line):
        self.lines.append(line)
        self.front.broadcast({"type": "script_output", "run": self.id, "line": line})

    async def read_output(self):
        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                break
            self.said(raw.decode("utf-8", "replace").rstrip("\n"))
        self.code = await self.process.wait()
        log("script %s (%s) exited with %s" % (self.name, self.id, self.code))
        # Its simulation runs on: a script that wants it paused says pause().
        self.state = "exited"
        self.front.broadcast({"type": "script_exit", "run": self.id, "code": self.code})
        self.front.changed = True

    async def stop(self):
        if self.process is None or self.process.returncode is not None:
            return
        self.state = "stopping"
        with contextlib.suppress(ProcessLookupError):
            self.process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(asyncio.shield(self.reader), STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.reader

    def summary(self):
        run_dir = self.run_dir
        return {"run": self.id, "name": self.name, "sim": self.sim, "state": self.state,
                "code": self.code, "started": self.started,
                "run_dir": os.path.relpath(run_dir, SIM_DIR) if run_dir else None,
                "report": bool(run_dir) and os.path.isfile(
                    os.path.join(run_dir, runs_module.REPORT_FILE))}


# ---------------------------------------------------------------------------
# One socket from a page or a driver
# ---------------------------------------------------------------------------

class Conn:
    """A control websocket on the front, and its sockets to children."""

    def __init__(self, ws, selected, quiet):
        self.ws = ws
        self.selected = selected
        self.quiet = quiet
        self.upstreams = {}                 # sim name -> (child websocket, pump task)
        self.opening = {}                   # sim name -> future, while one is opened

    @property
    def holder(self):
        return "conn:%d" % id(self)

    async def send(self, message):
        text = message if isinstance(message, str) else json.dumps(message)
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self.ws.send_str(text)

    def close_upstream(self, name):
        entry = self.upstreams.pop(name, None)
        if entry is not None:
            entry[1].cancel()

    def close(self):
        for name in list(self.upstreams):
            self.close_upstream(name)


# ---------------------------------------------------------------------------
# The front
# ---------------------------------------------------------------------------

class Front:

    def __init__(self, args):
        self.args = args
        self.child_args = args.child_args
        self.children = {}                  # name -> Child
        self.paused = {}                    # name -> runs.Run waiting in its pause
        self.ended_cache = {}               # run name -> ((run.yaml mtime, has report), row)
        self.script_runs = {}               # run id -> ScriptRun
        self.run_ids = itertools.count(1)
        self.net_locks = {}                 # net -> descriptor holding its claim_net lock
        self.preparing = set()              # names whose run is being laid out
        self.conns = set()
        self.session = None
        self.proxy_session = None
        self.sidecars = None
        self.sweeps = None
        self.cells = collections.OrderedDict()   # a pair's (to, from, flags), by where both stand
        self.runner = None
        self.control_port = None
        self.listener = None
        self.relay = None
        self.reporter = None
        self.changed = False
        self.cache = None                   # sources.Cache: geodata/.cache and its fetches
        self.build = None                   # packbuild.Build: the one running, or the last failed
        self.tiles = {}                     # OSM tile path -> the fetch of it under way
        self.nominatim_lock = asyncio.Lock()
        self.nominatim_at = 0.0
        self.run_bytes = {}                 # a run directory's real path -> its size on disk
        self.measurer = None
        self.fetching = {}                  # "index/kind/name" -> (index_progress row, task)
        self.cancel_asked = set()           # the fetches the page's Cancel stopped

    @property
    def bind_port(self):
        return int(self.args.bind.rpartition(":")[2])

    # ---- allocation ------------------------------------------------------

    def allocate(self):
        """A control port, an ether port and a /22 no live child has.

        Each is also checked free on the host, so a simd started by hand or
        another front's simulations are stepped around rather than collided
        with: two stations on one address fight over its ports, and the
        loser's web server and TCP interface never open. A network must have
        no socket bound to any of its addresses, and this front must win its
        host-wide lock (`claim_net`), which it holds until the simulation
        ends; that covers another front that has given the block out but
        whose stations have not bound it yet.
        """
        live = [c for c in self.children.values() if c.state != "exited"]
        ports = {c.port for c in live}
        ethers = {c.ether for c in live}
        nets = {c.net for c in live}
        for held in [n for n in self.net_locks if n not in nets]:
            self.release_net(held)
        port = next((p for p in CONTROL_PORTS if p not in ports and port_free(p)), None)
        ether = next((p for p in ETHER_PORTS
                      if "127.0.0.1:%d" % p not in ethers and port_free(p)), None)
        net = None
        if port is not None and ether is not None:
            bound = bound_addresses()
            for k in NETS:
                candidate = "127.%d.0.0/22" % (4 * k)
                if candidate in nets or not net_free(candidate, bound):
                    continue
                lock = claim_net(candidate)
                if lock is not None:
                    self.net_locks[candidate] = lock
                    net = candidate
                    break
        if net is None:
            raise ValueError("no free port or network left for another simulation")
        return port, ether, net

    def release_net(self, net):
        """Let go of this front's host-wide claim on a network."""
        lock = self.net_locks.pop(net, None)
        if lock is not None:
            os.close(lock)

    def free_name(self, base):
        base = re.sub(r"[^a-z0-9-]", "-", (base or "sim").lower()).strip("-")[:28] or "sim"
        taken = set(self.children) | self.preparing | set(self.paused) | {
            r.sim for r in self.script_runs.values() if r.world and r.state != "exited"}
        if base not in taken:
            return base
        n = 2
        while "%s-%d" % (base, n) in taken:
            n += 1
        return "%s-%d" % (base, n)

    # ---- loss tables -----------------------------------------------------

    def broadcast(self, message):
        text = json.dumps(message)
        for conn in list(self.conns):
            asyncio.ensure_future(conn.send(text))

    async def compute_losses(self, geodata_name, nodeset_name, bands, sidecar, tag):
        """Each band's table from the cache, or computed into it by
        losses.py as a subprocess: {band: (cache path, whether it was cached)}.

        Its JSON progress lines go to every page as `losses_progress` with
        `tag` ({sim} or {nodeset}) in them; its other output is kept for the
        error, should there be one.
        """
        out = {}
        for band in bands:
            argv = [sys.executable, "-u", LOSSES_PY, "--geodata", geodata_name,
                    "--nodeset", nodeset_name, "--band", band]
            if sidecar:
                argv += ["--sidecar", sidecar]
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, cwd=SIM_DIR)
            done, error, other = None, None, collections.deque(maxlen=5)
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if not isinstance(event, dict):
                    if line:
                        other.append(line)
                    continue
                if event.get("event") == "progress":
                    self.broadcast({"type": "losses_progress", **tag, "band": band,
                                    "done": event.get("done"), "total": event.get("total")})
                elif event.get("event") == "notice":
                    log("losses %s/%s band %s: %s" % (geodata_name, nodeset_name, band,
                                                      event.get("message")))
                elif event.get("event") == "done":
                    done = event
                elif event.get("event") == "error":
                    error = event.get("message")
            await proc.wait()
            if done is None:
                raise store.StoreError("losses for %s on %s, band %s: %s" % (
                    nodeset_name, geodata_name, band,
                    error or " | ".join(other) or "losses.py exited %s" % proc.returncode))
            out[band] = (done["path"], bool(done.get("cached")))
            log("losses %s/%s band %s: %s%s" % (geodata_name, nodeset_name, band,
                                                os.path.relpath(done["path"], SIM_DIR),
                                                " (cached)" if done.get("cached") else ""))
        return out

    # ---- the simulation verbs --------------------------------------------

    async def prepare_run(self, name, msg):
        """Lay out a simulation's run directory: (run, sidecar URL or None).

        Everything that can be refused is checked before the loss tables,
        which may take minutes: the names and the script. The firmware is
        the child's to settle, from the script's declarations, and `build`
        goes to it in the run. The sidecar is held for the simulation from
        here on.
        """
        snapshot = msg.get("snapshot")
        time_mode = str(msg.get("time") or "real")
        base = os.path.join(store.RUNS_DIR, name)
        holder = "sim:%s" % name
        if snapshot or msg.get("resume"):
            if snapshot:
                run = await asyncio.to_thread(runs_module.load_snapshot, snapshot,
                                              simd_module.free_run_dir(base), time_mode)
            else:
                run = await asyncio.to_thread(runs_module.resume_run, msg["resume"],
                                              simd_module.free_run_dir(base), time_mode)
            gd = read_geodata(run.geodata_path, run.geodata_name)
            run.set(build=msg.get("build") or None, build_tag=msg.get("build_tag") or None)
            sidecar = None
            if gd.is_pack:
                try:
                    sidecar = await self.sidecars.hold(holder, gd)
                except store.StoreError as err:
                    log("%s: %s; the snapshot runs, and a moved node's row waits"
                        % (name, err))
            return run, sidecar
        gd = load_geodata(msg["geodata"])
        script_name = msg.get("script") or None
        if script_name:
            script_module.read(script_name)
        layers = msg.get("nodesets") or [msg["nodeset"]]
        merged = None
        if len(layers) > 1:
            # Several nodesets are run as one, merged as Save selection as merges
            # them; losses.py reads the merge from a file of its own.
            data = nodeset_module.merge([(n, nodeset_module.load(n).data) for n in layers])
            merged = os.path.join(store.RUNS_DIR, ".merge-%s.yaml" % name)
            os.makedirs(store.RUNS_DIR, exist_ok=True)
            nodeset_module.write(merged, data)
            ns = nodeset_module.open_path(merged, "+".join(layers))
        else:
            ns = nodeset_module.load(layers[0])
        try:
            sidecar = await self.sidecars.hold(holder, gd)
            tables = await self.compute_losses(gd.path or gd.name, merged or ns.path,
                                               losses_module.bands_of(ns, gd), sidecar,
                                               {"sim": name})
            run = await asyncio.to_thread(
                runs_module.create_run, simd_module.free_run_dir(base), gd, ns, script_name,
                time_mode, {band: path for band, (path, _) in tables.items()})
        finally:
            if merged:
                with contextlib.suppress(OSError):
                    os.unlink(merged)
        run.set(build=msg.get("build") or None, build_tag=msg.get("build_tag") or None,
                firmware_rules=list(msg.get("firmware_rules") or []),
                first_boot_rules=list(msg.get("first_boot_rules") or []),
                inputs=dict(msg.get("inputs") or {}))
        return run, sidecar

    async def sim_new(self, msg):
        """Make the run, start a child on it, and say where it is."""
        snapshot = msg.get("snapshot")
        if snapshot and any(msg.get(k) for k in ("geodata", "nodeset", "script")):
            raise ValueError("a simulation is geodata, a nodeset and a script, or a "
                             "snapshot, not both")
        resume = msg.get("resume")
        if resume is not None and not isinstance(resume, runs_module.Run):
            raise ValueError("a simulation is resumed with sim_resume")
        if msg.get("nodesets"):
            layers = [str(n) for n in msg["nodesets"]]
            msg = dict(msg, nodeset=layers[0], nodesets=layers if len(layers) > 1 else None)
        if not snapshot and not resume and not all(msg.get(k) for k in ("geodata", "nodeset")):
            raise ValueError("a simulation needs geodata and a nodeset, or a snapshot")
        name = store.check_name(
            msg.get("name") or self.free_name(snapshot or msg.get("nodeset")), "simulation")
        old = self.children.get(name)
        if name in self.preparing or (old is not None and old.state != "exited"):
            raise ValueError("a simulation named %s is already running" % name)
        if name in self.paused:
            raise ValueError("a simulation named %s is paused: resume it, or remove it" % name)
        time_mode = str(msg.get("time") or "real")
        simd_module.ether_module.parse_time_mode(time_mode)
        stagger = msg.get("stagger")
        stagger = float(stagger) if stagger not in (None, "") else None

        self.preparing.add(name)
        try:
            run, sidecar = await self.prepare_run(name, msg)
        except BaseException:
            self.sidecars.release("sim:%s" % name)
            raise
        finally:
            self.preparing.discard(name)
        try:
            port, ether_port, net = self.allocate()
        except ValueError:
            self.sidecars.release("sim:%s" % name)
            raise
        child = Child(self, name, port, ether_port, net, time_mode, stagger, run.dir, sidecar,
                      pairwise=bool(msg.get("pairwise")))
        self.children[name] = child
        self.changed = True
        await child.start()
        try:
            ok = await asyncio.wait_for(asyncio.shield(child.ready), READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            ok = False
        if not ok:
            await child.stop()
            raise ValueError("simulation %s did not start: %s" % (
                name, " | ".join(list(child.tail)[-3:]) or "no output"))
        child.state = "running"
        child.monitor_task = asyncio.ensure_future(child.watch())
        self.changed = True
        for conn in list(self.conns):
            if conn.selected == name and name not in conn.upstreams:
                asyncio.ensure_future(self.open_upstream(conn, name))
        what = snapshot or "%s / %s / %s" % (run.meta.get("geodata"), run.meta.get("nodeset"),
                                            run.meta.get("script") or "no script")
        log("%s: running %s" % (name, what))
        return {"type": "sim_new", "ok": True, "name": name,
                "control": "ws://127.0.0.1:%d/ws" % port, "ether": child.ether,
                "net": net, "run": run.dir, "time": time_mode,
                "geodata": run.meta.get("geodata"), "nodeset": run.meta.get("nodeset"),
                "script": run.meta.get("script"), "snapshot": run.meta.get("snapshot"),
                "builds": run.meta.get("builds")}

    async def sim_pause(self, msg):
        """Stop a simulation with its state kept in its run, to be resumed as
        it ended; it stays in the registry as paused."""
        name = msg.get("name")
        by = msg.get("by") or None
        if by not in (None, "script"):
            raise ValueError("a pause is by its script, or by nobody named: not %s" % by)
        child = self.children.get(name)
        if child is None or child.state != "running":
            raise ValueError("no running simulation named %s" % name)
        t = (child.clock or {}).get("t") or 0
        run_dir = child.run_dir
        await self.sim_stop({"name": name})
        run = runs_module.open_run(run_dir)
        await asyncio.to_thread(runs_module.pause_run, run, name, t, by)
        self.paused[name] = run
        self.forget_size(run_dir)
        self.changed = True
        log("%s: %s at T %.0f s, in %s" % (name, "done" if by == "script" else "paused",
                                           t / 1e6, os.path.relpath(run_dir)))
        return {"type": "sim_pause", "ok": True, "name": name}

    async def sim_resume(self, msg):
        """A paused simulation, started again from where it ended, in a new
        run directory under its own name: in real time unless `time` says
        otherwise, since what a person resumes one for is to be at its nodes."""
        name = msg.get("name")
        old = self.paused.pop(name, None)
        if old is None:
            raise ValueError("no paused simulation named %s" % name)
        try:
            reply = await self.sim_new({"name": name, "resume": old,
                                        "time": msg.get("time") or "real"})
        except BaseException:
            if old.meta.pop("resumed", None) is not None:
                old.set()
            self.paused[name] = old
            self.changed = True
            raise
        return dict(reply, type="sim_resume")

    async def run_delete(self, msg):
        """A run deleted, directory and all: an ended one, or a paused one,
        which can then no longer be resumed. One a simulation is still on,
        running or exited, is refused, unless `stop` asks for that to be
        stopped first."""
        name = msg.get("run")
        path = runs_module.run_path(name)
        if not os.path.isfile(os.path.join(path, runs_module.RUN_FILE)):
            raise ValueError("no run called %s" % name)
        real = os.path.realpath(path)
        on = [c for c in self.children.values() if os.path.realpath(c.run_dir) == real]
        if any(c.state != "exited" for c in on) and not msg.get("stop"):
            raise ValueError("run %s is running: stop it first" % name)
        for child in on:
            await self.sim_stop({"name": child.name})
        for sim, run in list(self.paused.items()):
            if os.path.realpath(run.dir) == real:
                del self.paused[sim]
        await asyncio.to_thread(runs_module.delete_run, runs_module.open_run(path))
        self.ended_cache.pop(name, None)
        self.forget_size(path)
        self.changed = True
        log("run %s deleted" % name)
        return {"type": "run_delete", "ok": True, "run": name}

    @staticmethod
    def geodata_names(msg):
        """The geodata a delete names, `names` or one `name`, each one that is here."""
        names = [str(n) for n in (msg.get("names") or [msg.get("name")]) if n]
        for name in names:
            if not os.path.isfile(geodata_module.geodata_path(name)):
                raise store.StoreError("no geodata called %r" % name)
        return names

    def geodata_delete_plan(self, names):
        """What deleting the geodata `names` takes with it, to be said before
        it is done: `geodata.taken_with`, and the runs of the simulations still
        on any of that geodata, running or exited, which are stopped
        (`running`, by simulation name)."""
        plan = geodata_module.taken_with(names)
        leaving = set(names) | set(plan["geodata"])
        on = [c for c in self.children.values() if c.loaded.get("geodata") in leaving]
        plan["runs"] = sorted(set(plan["runs"]) | {os.path.basename(c.run_dir) for c in on})
        plan["running"] = sorted(c.name for c in on if c.state != "exited")
        return plan

    async def geodata_delete(self, names):
        """The geodata `names` gone, packs included, and everything their plan
        says goes with them: simulations stopped, runs and snapshots deleted,
        the geodata that named their packs, and the nodesets left on none."""
        plan = await asyncio.to_thread(self.geodata_delete_plan, names)
        for run in plan["runs"]:
            await self.run_delete({"run": run, "stop": True})
        for snapshot in plan["snapshots"]:
            await asyncio.to_thread(runs_module.delete_snapshot, snapshot)
        # What only names a pack goes before the geodata whose pack it is.
        order = plan["geodata"] + sorted(names, key=lambda n: geodata_module.own_pack(n) is not None)
        for name in order:
            await self.stop_sidecar(name)
            await asyncio.to_thread(geodata_module.delete, name)
        for nodeset in plan["nodesets"]:
            nodeset_module.delete(nodeset)
        self.changed = True
        log("deleted geodata %s%s" % (", ".join(order), "".join(
            "; %s %s" % (what, ", ".join(plan[what]))
            for what in ("runs", "snapshots", "nodesets") if plan[what])))
        return {"deleted": dict(plan, geodata=order)}

    async def stop_sidecar(self, name):
        """The planner sidecar on the geodata's pack stopped, when one runs."""
        pack = geodata_module.pack_of(geodata_module.geodata_path(name))
        car = self.sidecars.by_pack.pop(pack, None) if pack else None
        if car is not None:
            await self.sidecars.stop(car)

    def ended_rows(self):
        """Every run that is neither running nor paused: what it ran, when,
        and whether it has a report. Each is read again only when its
        run.yaml or report changes."""
        busy = {os.path.realpath(c.run_dir) for c in self.children.values()}
        busy |= {os.path.realpath(r.dir) for r in self.paused.values()}
        rows = []
        for name in runs_module.runs():
            path = runs_module.run_path(name)
            if os.path.realpath(path) in busy:
                continue
            try:
                stamp = (os.stat(os.path.join(path, runs_module.RUN_FILE)).st_mtime,
                         os.path.isfile(os.path.join(path, runs_module.REPORT_FILE)))
            except OSError:
                continue
            cached = self.ended_cache.get(name)
            if cached is None or cached[0] != stamp:
                cached = (stamp, self.ended_summary(runs_module.open_run(path)))
                self.ended_cache[name] = cached
            rows.append(cached[1])
        return rows

    def ended_summary(self, run):
        """An ended run's registry row."""
        try:
            stations = run.node_count()
        except store.StoreError:
            stations = None             # a run older than nodesets: not known
        return {"name": run.name, "state": "ended", "code": None,
                "run": os.path.relpath(run.dir, SIM_DIR), "time": run.meta.get("time"),
                "started": run.started_at(), "ended": run.ended_at(),
                "started_at": run.meta.get("started"),
                "geodata": run.meta.get("geodata"), "nodeset": run.meta.get("nodeset"),
                "script": run.meta.get("script"), "snapshot": run.meta.get("snapshot"),
                "dirty": False, "stations": stations, "counts": {}, "errors": [], "tail": [],
                "pace": None, "mode": None, "rate": None, "observed": None,
                "t": (run.meta.get(runs_module.PAUSED) or {}).get("t") or run.last_t(),
                "plan": None, "phase": None, "eta": None, "report": run.has_report()}

    def paused_summary(self, name, run):
        """A paused simulation's registry row: what it ran, and where it ended."""
        paused = run.paused or {}
        try:
            stations = run.node_count()
        except store.StoreError:
            stations = None
        return {"name": name, "state": "paused", "code": None, "run": os.path.relpath(run.dir, SIM_DIR),
                "time": run.meta.get("time"), "started": run.started_at(),
                "ended": run.ended_at(), "paused_at": paused.get("at"),
                "paused_by": paused.get("by"),
                "geodata": run.meta.get("geodata"), "nodeset": run.meta.get("nodeset"),
                "script": run.meta.get("script"), "snapshot": run.meta.get("snapshot"),
                "dirty": False, "stations": stations, "counts": {}, "errors": [], "tail": [],
                "pace": None, "mode": None, "rate": None, "observed": None, "t": paused.get("t"),
                "plan": None, "phase": None, "eta": None, "report": run.has_report()}

    def paused_holding(self):
        """{firmware name: [paused simulation…]}: the firmware each paused
        simulation's state was written by, which it can only resume on."""
        out = {}
        for sim, run in sorted(self.paused.items()):
            for name in firmware_module.firmware_of(run.meta.get("builds")):
                out.setdefault(name, []).append(sim)
        return out

    async def sim_stop(self, msg):
        name = msg.get("name")
        child = self.children.get(name)
        if child is None and name in self.paused:
            # Stopped for good: its saved state goes, and it is an ended run.
            run = self.paused.pop(name)
            await asyncio.to_thread(runs_module.stop_paused, run)
            self.ended_cache.pop(run.name, None)
            self.forget_size(run.dir)
            self.changed = True
            log("%s: stopped, its pause's state deleted" % name)
            return {"type": "sim_stop", "ok": True, "name": name}
        if child is None:
            raise ValueError("no simulation named %s" % name)
        await child.stop()
        self.children.pop(name, None)
        for conn in list(self.conns):
            conn.close_upstream(name)
        self.sidecars.release(child.holder)
        self.forget_size(child.run_dir)
        self.changed = True
        log("%s: stopped" % name)
        return {"type": "sim_stop", "ok": True, "name": name}

    def child_gone(self, child):
        for conn in list(self.conns):
            conn.close_upstream(child.name)
        if self.children.get(child.name) is child:
            self.sidecars.release(child.holder)
        if not any(c.net == child.net and c.state != "exited" for c in self.children.values()):
            self.release_net(child.net)
        self.changed = True

    async def forward_load(self, conn, name, msg):
        """A `sim_load` or `snapshot_load` on its way to a running child: the
        tables computed and the sidecar held first, then the message passed
        on with `sidecar` added."""
        child = self.children.get(name)
        try:
            if child is None or child.state != "running":
                raise ValueError("simulation %s is not running" % name)
            if msg["type"] == "sim_load" and not msg.get("run"):
                gd = load_geodata(msg["geodata"])
                ns = nodeset_module.load(msg["nodeset"])
                sidecar = await self.sidecars.hold(child.holder, gd)
                await self.compute_losses(gd.name, ns.name, losses_module.bands_of(ns, gd),
                                          sidecar, {"sim": name})
            else:
                path = os.path.join(runs_module.snapshot_path(msg["name"]),
                                    runs_module.GEODATA_FILE)
                gd = read_geodata(path)
                sidecar = None
                with contextlib.suppress(store.StoreError):
                    sidecar = await self.sidecars.hold(child.holder, gd)
            child.sidecar = sidecar
            msg = dict(msg, sidecar=sidecar)
        except (store.StoreError, ValueError, KeyError, OSError) as err:
            await conn.send({"type": "error", "sim": name, "text": str(err)})
            return
        upstream = await self.open_upstream(conn, name)
        if upstream is not None:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await upstream.send_str(json.dumps(msg))

    # ---- the editors -----------------------------------------------------

    def planner_path(self, gd):
        return "/planner/%s/" % gd.name if gd.is_pack else None

    async def open_geodata(self, conn, gd):
        """Hold the geodata's sidecar for this socket: {geodata, planner}."""
        await self.sidecars.hold(conn.holder, gd)
        return {"geodata": gd.as_dict(), "planner": self.planner_path(gd)}

    @staticmethod
    def new_path(path, what, name):
        if os.path.exists(path):
            raise store.StoreError("there is already a %s called %r" % (what, name))
        return path

    @staticmethod
    def old_path(path, what, name):
        if not os.path.isfile(path):
            raise store.StoreError("no %s called %r" % (what, name))
        return path

    def write_nodeset(self, name, data, new):
        path = nodeset_module.nodeset_path(name)
        (self.new_path if new else self.old_path)(path, "nodeset", name)
        nodeset_module.write(path, nodeset_module.parse(data, "nodeset %s" % name),
                             None if new else nodeset_module.comment_of(path))
        return {"nodeset": nodeset_module.load(name).as_dict()}

    async def estimate_heights(self, conn, name, msg):
        """A nodeset's assumed heights given planner's estimate on a pack
        (`losses.estimated_heights`), saved: a roof under a node with a mast
        on it, else the clutter or land class around it, each marked `roof` or
        `raster`. A measured, roof or raster height is kept, and so is a node
        the estimator found no evidence for. Answers the nodeset, the nodes
        changed and each one's estimate, its band and what it rested on."""
        gd = load_geodata(msg.get("geodata") or "")
        if not gd.is_pack:
            raise store.StoreError("%s is synthetic ground: a height is estimated from a pack's "
                                   "buildings and rasters" % gd.name)
        path = self.old_path(nodeset_module.nodeset_path(name), "nodeset", name)
        data = nodeset_module.read(path)
        points = {n: (node["lat"], node["lon"]) for n, node in data["nodes"].items()
                  if node.get("height_from") == "assumed"}
        sidecar = await self.sidecars.hold(conn.holder, gd)
        estimates = await losses_module.estimated_heights(gd, points, sidecar)
        changed = nodeset_module.with_estimated_heights(data, estimates)
        if changed:
            nodeset_module.write(path, data, nodeset_module.comment_of(path))
            log("estimated %d height(s) in nodeset %s on %s" % (len(changed), name, gd.name))
        return {"nodeset": nodeset_module.load(name).as_dict(), "changed": changed,
                "estimates": {n: estimates[n] for n in changed}}

    def write_geodata(self, name, data, new):
        path = geodata_module.geodata_path(name)
        (self.new_path if new else self.old_path)(path, "geodata", name)
        if geodata_module.PACK in geodata_module.parse(data, path) \
                or (not new and geodata_module.pack_of(path)):
            raise store.StoreError("geodata on a pack is built or imported, not written")
        geodata_module.write(path, data)
        return {"geodata": load_geodata(name).as_dict()}

    def script_reply(self, name):
        path = self.old_path(script_module.script_path(name), "script", name)
        return {"script": script_module.describe(path, name), "text": script_module.read(name)}

    async def editor(self, conn, verb, msg):
        """One editor verb, answered to the asking socket."""
        name = msg.get("name")
        if verb == "firmware_list":
            held = self.paused_holding()

            def listing():
                dirs = firmware_module.installed()
                return [dict(r, bytes=store.disk_bytes(dirs[r["name"]]) if r["name"] in dirs else None,
                             paused=held.get(r["name"], []))
                        for r in firmware_module.listing()]
            rows = await asyncio.to_thread(listing)
            return {"firmware": rows, "arch": firmware_module.machine_arch()}
        if verb == "firmware_prebuilt":
            rows = await firmware_module.prebuilt()
            return {"firmware": rows, "index": firmware_module.PREBUILT_INDEX}
        if verb == "firmware_add":
            got = await firmware_module.add(str(msg["url"]))
            log("added firmware %s from %s" % (got["name"], msg["url"]))
            self.broadcast({"type": "firmware_changed"})
            return {"firmware": firmware_module.row(got["name"], got["dir"])}
        if verb == "firmware_delete":
            names = [str(n) for n in msg.get("names") or ()]
            held = self.paused_holding()
            stopping = sorted({s for n in names for s in held.get(n, ())})
            if stopping and not msg.get("stop_paused"):
                raise ValueError("paused simulation%s %s hold%s it: deleting stops %s"
                                 % ("s" if len(stopping) > 1 else "", ", ".join(stopping),
                                    "" if len(stopping) > 1 else "s",
                                    "them" if len(stopping) > 1 else "it"))
            # A snapshot's hold refuses before any simulation is stopped.
            snapshots = await asyncio.to_thread(firmware_module.holders)
            kept = {n: [u for u in snapshots.get(n, ()) if u.startswith("snapshot ")] for n in names}
            if any(kept.values()):
                raise ValueError("; ".join("%s is held by %s" % (n, ", ".join(u))
                                           for n, u in sorted(kept.items()) if u))
            for sim in stopping:
                await self.sim_stop({"name": sim})
            deleted = await asyncio.to_thread(firmware_module.delete, names)
            log("deleted firmware %s" % ", ".join(deleted))
            self.broadcast({"type": "firmware_changed"})
            return {"deleted": deleted, "stopped": stopping}
        if verb == "antenna_list":
            return {"antennas": await asyncio.to_thread(antennas_module.listing)}
        if verb == "geodata_list":
            # Each geodata with how many nodesets have a node on it, its
            # size and the index it came from.
            def listing():
                sets = []
                for each in nodeset_module.names():
                    with contextlib.suppress(store.StoreError):
                        sets.append(nodeset_module.load(each))
                rows = []
                for each in geodata_module.names():
                    extra = {"bytes": store.disk_bytes(geodata_module.geodata_dir(each)),
                             "from_index": indexes_module.geodata_origin(each)}
                    try:
                        gd = load_geodata(each)
                        rows.append({**gd.as_dict(), **extra,
                                     "nodesets": sum(1 for ns in sets if ns.inside(gd.bbox))})
                    except store.StoreError as err:
                        rows.append({"name": each, "error": str(err), **extra})
                return rows
            return {"geodata": await asyncio.to_thread(listing),
                    "build": self.build.row if self.build else None}
        if verb == "geodata_sources":
            return {"sources": await asyncio.to_thread(self.source_rows),
                    "building": self.build is not None and self.build.running}
        if verb == "geodata_source_clear":
            return await self.clear_source(str(msg.get("source") or ""))
        if verb == "geodata_source_map":
            source = str(msg.get("source") or "")
            if source not in sources_module.registry():
                raise ValueError("no source called %s with an area" % source)
            return await sources_module.source_map(self.cache, source)
        if verb == "geodata_sources_at":
            lon, lat = float(msg["lon"]), float(msg["lat"])
            return {"lon": lon, "lat": lat,
                    "sources": await sources_module.sources_at(self.cache, lon, lat)}
        if verb == "index_list":
            return {"indexes": await indexes_module.listing(self.session),
                    "fetching": [row for row, _task in self.fetching.values()]}
        if verb == "index_new":
            row = await indexes_module.add_index(str(msg.get("address") or ""), self.session)
            log("listed index %s at %s" % (row["name"], row["address"]))
            return {"index": row}
        if verb == "index_delete":
            names = indexes_module.remove_index(str(msg.get("name") or ""))
            log("forgot index %s" % ", ".join(names))
            return {"names": names}
        if verb == "index_install":
            return await self.index_install(msg)
        if verb == "index_cancel":
            got = self.fetching.get(self.fetch_key(msg))
            if got is None:
                raise ValueError("nothing of that is being fetched")
            self.cancel_asked.add(self.fetch_key(msg))
            got[1].cancel()
            return {}
        if verb == "geodata_delete_plan":
            return await asyncio.to_thread(self.geodata_delete_plan, self.geodata_names(msg))
        if verb == "geodata_delete":
            return await self.geodata_delete(self.geodata_names(msg))
        if verb == "geodata_rename":
            live = [c.name for c in self.children.values()
                    if c.state != "exited" and c.loaded.get("geodata") == name]
            if live:
                raise store.StoreError("simulation %s stands on %s and is running"
                                       % (", ".join(live), name))
            await self.stop_sidecar(name)
            to = store.check_name(msg.get("to"), "geodata")
            geodata_module.rename(name, to)
            return {"name": to}
        if verb == "geodata_open":
            return await self.open_geodata(conn, load_geodata(name))
        if verb == "geodata_close":
            self.sidecars.release(conn.holder)
            return {}
        if verb == "geodata_new":
            data = {k: msg[k] for k in (geodata_module.PACK, geodata_module.SYNTHETIC)
                    if msg.get(k)}
            return self.write_geodata(store.check_name(name, "geodata"), data, new=True)
        if verb in ("geodata_save", "geodata_save_as"):
            return self.write_geodata(store.check_name(name, "geodata"), msg["data"],
                                      new=verb == "geodata_save_as")
        if verb == "nodeset_list":
            # The extent is the manifest's: listing needs no planner.
            bbox = geodata_module.load(msg["geodata"]).bbox if msg.get("geodata") else None

            def listing():
                rows = []
                for each in nodeset_module.names():
                    extra = {"bytes": nodeset_module.disk_bytes(each),
                             "from_index": indexes_module.nodeset_origin(each)}
                    try:
                        row = {**nodeset_module.summary(each), **extra}
                        if bbox is not None:
                            row["inside"] = nodeset_module.load(each).inside(bbox)
                        rows.append(row)
                    except store.StoreError as err:
                        rows.append({"name": each, "error": str(err), **extra})
                return rows
            return {"nodesets": await asyncio.to_thread(listing)}
        if verb == "nodeset_open":
            return {"nodeset": nodeset_module.load(name).as_dict()}
        if verb == "nodeset_delete":
            nodeset_module.delete(store.check_name(name, "nodeset"))
            return {"name": name}
        if verb == "nodeset_new":
            return {"nodeset": nodeset_module.create(store.check_name(name, "nodeset")).as_dict()}
        if verb in ("nodeset_save", "nodeset_save_as"):
            return self.write_nodeset(store.check_name(name, "nodeset"), msg["data"],
                                      new=verb == "nodeset_save_as")
        if verb == "nodeset_import":
            if (msg.get("source") or msg.get("format") or "sites") in FILE_IMPORTS:
                return await asyncio.to_thread(self.import_nodeset, msg)
            return await self.import_node_map(msg)
        if verb == "nodeset_heights":
            return await self.estimate_heights(conn, store.check_name(name, "nodeset"), msg)
        if verb == "nodeset_merge":
            name = store.check_name(name, "nodeset")
            path = self.new_path(nodeset_module.nodeset_path(name), "nodeset", name)
            layers = [(store.check_name(str(layer.get("name")), "layer"), layer.get("data") or {})
                      for layer in msg.get("layers") or ()]
            if not layers:
                raise store.StoreError("no nodeset is chosen to save")
            nodeset_module.write(path, nodeset_module.merge(layers),
                                 "from %s" % ", ".join(layer for layer, _ in layers))
            return {"nodeset": nodeset_module.load(name).as_dict()}
        if verb == "nodeset_setup_open":
            text, exists = script_module.read_nodeset_setup(store.check_name(name, "nodeset"))
            return {"name": name, "text": text, "exists": exists}
        if verb == "nodeset_setup_save":
            script_module.write_nodeset_setup(store.check_name(name, "nodeset"), msg["text"])
            return {"name": name, "text": msg["text"], "exists": True}
        if verb == "script_list":
            return {"scripts": await asyncio.to_thread(script_module.listing)}
        if verb == "script_open":
            return self.script_reply(name)
        if verb == "module_open":
            return {"path": msg["path"],
                    "text": await asyncio.to_thread(script_module.read_reference, msg["path"])}
        if verb == "script_new":
            script_module.write(store.check_name(name, "script"),
                                msg.get("text") or script_module.DEFAULT_TEXT, new=True)
            return self.script_reply(name)
        if verb in ("script_save", "script_save_as"):
            script_module.write(store.check_name(name, "script"), msg["text"],
                                new=verb == "script_save_as")
            return self.script_reply(name)
        if verb == "script_run":
            return await self.script_run(msg)
        if verb == "script_stop":
            run = self.script_runs.get(msg.get("run"))
            if run is None:
                raise ValueError("no script run %s" % msg.get("run"))
            await run.stop()
            return {"run": run.id}
        if verb == "script_log":
            run = self.script_runs.get(msg.get("run"))
            if run is None:
                raise ValueError("no script run %s" % msg.get("run"))
            return {"run": run.id, "lines": list(run.lines)}
        if verb == "snapshot_list":
            def listing():
                rows = []
                for each in runs_module.snapshots():
                    with contextlib.suppress(store.StoreError):
                        info = runs_module.snapshot_info(each)
                        rows.append({"name": each, **{k: v for k, v in info.items()
                                                      if k != "builds"},
                                     "bytes": store.disk_bytes(runs_module.snapshot_path(each))})
                return rows
            return {"snapshots": await asyncio.to_thread(listing)}
        if verb == "losses_compute":
            gd = load_geodata(msg["geodata"])
            ns = nodeset_module.load(msg["nodeset"])
            bands = [str(b) for b in msg["bands"]] if msg.get("bands") \
                else losses_module.bands_of(ns, gd)
            sidecar = await self.sidecars.hold(conn.holder, gd)
            tables = await self.compute_losses(gd.name, ns.name, bands, sidecar,
                                               {"nodeset": ns.name})
            return {"geodata": gd.name, "nodeset": ns.name,
                    "tables": {band: {"path": path, "cached": hit}
                               for band, (path, hit) in tables.items()}}
        if verb == "links":
            return await self.links_row(conn, msg)
        if verb == "coverage":
            return await self.coverage(conn, msg)
        raise ValueError("unknown verb %s" % verb)

    async def import_node_map(self, msg):
        """A public node map's nodes inside the geodata's extent, as a new
        nodeset: the MeshCore map's list (fetched at most weekly into the
        cache), or a PotatoMesh instance's /api/nodes (fetched for this
        import), read by `planner-job nodes-import`."""
        source = msg.get("source")
        name = store.check_name(msg.get("name"), "nodeset")
        path = self.new_path(nodeset_module.nodeset_path(name), "nodeset", name)
        gd = geodata_module.load(msg.get("geodata") or "")
        if planner_job() is None:
            raise store.StoreError("planner-job is not built: `sim` builds it as it starts, "
                                       "and said why it could not")
        if source == "meshcore":
            fetched = await self.cache.meta_file(MESHCORE_META, MESHCORE_NODES)
        elif source == "potatomesh":
            base = str(msg.get("url") or "").strip().rstrip("/")
            if not re.match(r"^https?://[^/\s]+", base):
                raise store.StoreError("a PotatoMesh instance is its address, https://…")
            fetched = await self.cache.meta_file(
                "potatomesh-%s.json" % store.slug(urllib.parse.urlsplit(base).netloc, "instance"),
                base + POTATOMESH_NODES, max_age_s=0)
        else:
            raise ValueError("an import's source is meshcore, potatomesh, sites or nodes")
        job = {"source": source, "file": fetched, "bbox": gd.bbox,
               "companions": bool(msg.get("companions")),
               "max_age_days": int(msg.get("max_age_days") or MESHCORE_MAX_AGE_DAYS),
               "now_unix": int(time.time())}
        got = await self.run_job("nodes-import", job)
        rows = got.get("nodes") or []
        if not rows:
            raise store.StoreError("%s has no node inside %s's extent (%s)"
                                   % (source, gd.name, json.dumps(got.get("report") or {})))
        data = nodeset_module.from_imported(rows, source, float(msg.get("height_m") or 15.0))
        nodeset_module.write(path, data, "imported from %s inside %s: %s" % (
            source, gd.name, json.dumps(got.get("report") or {})))
        log("imported %d nodes from %s into nodeset %s" % (len(data["nodes"]), source, name))
        return {"nodeset": nodeset_module.load(name).as_dict(), "report": got.get("report")}

    async def run_job(self, verb, job):
        """One `planner-job <verb>` run: the JSON object on its standard input,
        its last JSON line back; its standard error goes to the front's log."""
        process = await asyncio.create_subprocess_exec(
            planner_job(), verb, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, preexec_fn=die_with_parent)
        out, err = await process.communicate(json.dumps(job).encode("utf-8"))
        for line in err.decode("utf-8", "replace").splitlines()[-TAIL_LINES:]:
            log("planner-job %s: %s" % (verb, line))
        last = {}
        for line in out.decode("utf-8", "replace").splitlines():
            with contextlib.suppress(ValueError):
                last = json.loads(line)
        if process.returncode != 0 or "error" in last:
            raise store.StoreError(last.get("error") or "planner-job %s ended with %d"
                                   % (verb, process.returncode))
        return last

    async def api_nodes_sources(self, request):
        """GET: what the import sources hold here: when the MeshCore map's
        list was fetched (Unix seconds), or null."""
        return web.json_response({"meshcore": {"fetched": self.cache.meta_age(MESHCORE_META)}})

    def import_nodeset(self, msg):
        """A file as a new nodeset: a planner CSV, its transmit powers in the
        nodes' radios, or a file of points (any CSV, GeoJSON, KML, GPX, a
        Meshtastic node list); with `geodata`, only its nodes inside that
        geodata's extent."""
        name = store.check_name(msg["name"], "nodeset")
        path = nodeset_module.nodeset_path(name)
        self.new_path(path, "nodeset", name)
        fmt = msg.get("source") or msg.get("format") or "sites"
        if fmt not in FILE_IMPORTS:
            raise ValueError("a file's import is one of %s" % ", ".join(FILE_IMPORTS))
        if fmt in nodeset_module.POINT_FORMATS:
            text = msg.get("text")
            if text is None:
                with open(msg["path"], encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            data = nodeset_module.import_points(fmt, text, float(msg.get("height_m") or 15.0),
                                                msg.get("columns"))
            return self.write_imported(name, path, data, msg, fmt)
        source, tmp = msg.get("path"), None
        if not source:
            fd, tmp = tempfile.mkstemp(suffix=".csv")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(msg["text"])
            source = tmp
        extra = {}
        if msg.get("height_m"):
            extra["height_m"] = float(msg["height_m"])
        try:
            if fmt == "sites":
                data = nodeset_module.import_sites_csv(source, **extra)
            else:
                data = nodeset_module.import_nodes_csv(source, **extra)
        finally:
            if tmp:
                os.unlink(tmp)
        return self.write_imported(name, path, data, msg, fmt)

    @staticmethod
    def write_imported(name, path, data, msg, fmt):
        """An imported nodeset written, with `geodata` only its nodes inside
        that geodata's extent."""
        if msg.get("geodata"):
            gd = geodata_module.load(msg["geodata"])
            data["nodes"] = {n: node for n, node in data["nodes"].items()
                             if gd.holds(node["lat"], node["lon"])}
            if not data["nodes"]:
                raise store.StoreError("the file has no node inside %s's extent" % gd.name)
        nodeset_module.write(path, data, "imported from %s (%s)" % (
            os.path.basename(msg.get("file") or msg.get("path") or "an upload"), fmt))
        log("imported %d nodes into nodeset %s" % (len(data["nodes"]), name))
        return {"nodeset": nodeset_module.load(name).as_dict()}

    # ---- indexes, and the build's cache ----------------------------------

    @staticmethod
    def fetch_key(msg):
        return "%s/%s/%s" % (msg.get("index"), msg.get("kind"), msg.get("name"))

    async def index_install(self, msg):
        """One entry of a listed index installed, answered when it is in.
        Its progress goes to every page as `index_progress`, at most every
        PROGRESS_EVERY_S; `index_cancel` stops it."""
        key = self.fetch_key(msg)
        if key in self.fetching:
            raise ValueError("%s %s is being fetched already" % (msg.get("kind"), msg.get("name")))
        row = {"index": str(msg.get("index")), "kind": str(msg.get("kind")),
               "name": str(msg.get("name")), "state": "fetching", "now": None,
               "fetched": 0, "of": None, "error": None}
        told = [0.0]

        def progress(kind, name, fetched, of):
            row.update(now={"kind": kind, "name": name}, fetched=fetched, of=of)
            if time.monotonic() - told[0] >= PROGRESS_EVERY_S:
                told[0] = time.monotonic()
                self.broadcast({"type": "index_progress", **row})

        self.fetching[key] = (row, asyncio.current_task())
        self.broadcast({"type": "index_progress", **row})
        try:
            got = await indexes_module.install(row["index"], row["kind"], row["name"],
                                               self.session, progress)
            row["state"] = "done"
        except asyncio.CancelledError:
            row["state"] = "cancelled"
            raise ValueError("cancelled: %s %s from %s, by %s" % (
                row["kind"], row["name"], row["index"],
                "the page's Cancel" if key in self.cancel_asked
                else "the front stopping")) from None
        except store.StoreError as err:
            row.update(state="failed", error=str(err))
            raise
        finally:
            self.fetching.pop(key, None)
            self.cancel_asked.discard(key)
            self.broadcast({"type": "index_progress", **row})
            self.changed = True
        log("installed %s from index %s" % (", ".join(
            "%s %s" % ("geodata" if each["kind"] == "geodata" else "nodeset", each["name"])
            for each in got["installed"]), row["index"]))
        return got

    def source_rows(self):
        """The build's cache, one row per source, and the map's tiles: what
        each is, its licence, and what it holds here."""
        rows = [dict(source.as_dict(), map=True, bytes=store.disk_bytes(
                    os.path.join(sources_module.CACHE_DIR, source.id)))
                for source in sources_module.registry().values()]
        rows.append({"source": "meta", "what": "the sources' own lists: Geofabrik's index, "
                     "Berlin's feeds, the MeshCore map's node list", "licence": "", "map": False,
                     "holds": sources_module.CACHE_HOLDS["meta"], "where": "",
                     "bytes": store.disk_bytes(os.path.join(sources_module.CACHE_DIR, "meta"))})
        rows.append({"source": "osmtiles", "what": "OpenStreetMap map tiles, for the build's map",
                     "licence": "ODbL 1.0, © OpenStreetMap contributors", "map": False,
                     "holds": sources_module.CACHE_HOLDS["osmtiles"], "where": "",
                     "bytes": store.disk_bytes(OSM_TILES_DIR)})
        return rows

    async def clear_source(self, source):
        """One source's cache emptied, refused while a build runs: it would
        be reading it."""
        if source not in sources_module.registry() and source not in ("meta", "osmtiles"):
            raise ValueError("no source called %s" % source)
        if source != "osmtiles" and self.build is not None and self.build.running:
            raise store.StoreError("%s is being built from the cache: cancel it first"
                                   % self.build.name)
        path = OSM_TILES_DIR if source == "osmtiles" else os.path.join(sources_module.CACHE_DIR,
                                                                        source)
        await asyncio.to_thread(shutil.rmtree, path, True)
        log("emptied the cache of %s" % source)
        return {"source": source}

    async def script_run(self, msg):
        """A script, run: on a new simulation of its own from geodata and
        nodesets, which it starts itself with its time, firmware and
        first-boot declarations, under the name chosen here so the page can
        go to it; or on the running one named (`sim`); or on the paused one
        named (`resume`), which it resumes with its own time, real unless it
        says otherwise."""
        name = msg.get("name")
        info = script_module.describe(
            self.old_path(script_module.script_path(name), "script", name), name)
        if info.get("error"):
            raise store.StoreError(info["error"])
        including = next((row["included_by"] for row in script_module.listing()
                          if row["name"] == name), [])
        if including:
            raise store.StoreError("%s is part of %s, which include it: run one of those"
                                   % (name, ", ".join(including)))
        sim, world = msg.get("sim"), None
        if sim:
            child = self.children.get(sim)
            if child is None or child.state != "running":
                raise ValueError("simulation %s is not running" % sim)
        elif msg.get("resume"):
            sim, world = str(msg["resume"]), {"resume": True}
            if sim not in self.paused:
                raise ValueError("no paused simulation named %s" % sim)
        else:
            layers = [str(n) for n in (msg.get("nodesets") or [msg.get("nodeset")]) if n]
            if not msg.get("geodata") or not layers:
                raise ValueError("a script's simulation needs geodata and a nodeset")
            world = {"geodata": str(msg["geodata"]), "nodesets": layers,
                     "build": msg.get("build"), "build_tag": msg.get("build_tag")}
            sim = store.check_name(msg.get("sim_name") or self.free_name(layers[0]),
                                   "simulation")
        given = {str(k): str(v) for k, v in (msg.get("inputs") or {}).items()
                 if v not in (None, "")}
        run = ScriptRun(self, "r%d" % next(self.run_ids), name, sim, world, given)
        self.script_runs[run.id] = run
        await run.start(self.bind_port)
        self.changed = True
        # Not `sim`: a message carrying `sim` is that simulation's own to a page.
        return {"run": run.id, "simulation": sim}

    async def links_row(self, conn, msg):
        """One node's losses to and from every other, from the nodes as the
        page has them, saved or not: what its links are drawn from before
        there is a table. Each pair is kept by where both ends stand, so
        selecting the next node asks only for the pairs that are new."""
        gd = load_geodata(msg["geodata"])
        ns = nodeset_module.Nodeset(None, nodeset_module.parse(
            {"nodes": msg["nodes"], "offsets": []}, "links"))
        name = str(msg["node"])
        me = ns.node(name)
        band = str(msg.get("band") or (losses_module.bands_of(ns, gd) or ["868"])[0])

        def where(node):
            return (round(node["lat"], 7), round(node["lon"], 7), round(node["height_m"], 2))

        def key(other, back=False):
            ends = (where(me), where(ns.nodes[other]))
            return (gd.content_hash, band) + (ends[::-1] if back else ends)
        missing = [o for o in ns.nodes if o != name and key(o) not in self.cells]
        if missing:
            sidecar = await self.sidecars.hold(conn.holder, gd) if gd.is_pack else None
            got = await losses_module.row(gd, ns, name, band, sidecar, missing, self.session)
            # Kept both ways, so the other end's row finds the pair.
            for other, (to, back, flags) in got.items():
                self.cells[key(other)] = (to, back, flags)
                self.cells[key(other, back=True)] = (back, to, flags)
            while len(self.cells) > LINK_CELLS_KEPT:
                self.cells.popitem(last=False)
        out = {}
        for other in ns.nodes:
            if other == name:
                continue
            to, back, flags = self.cells[key(other)]
            out[other] = {"to": to if math.isfinite(to) else None,
                          "from": back if math.isfinite(back) else None, "flags": flags}
        f0 = losses_module.LINK_F0_HZ if gd.is_pack else losses_module.slt.f0_of(band)
        return {"node": name, "band": band, "f0_hz": f0, "cells": out}

    async def coverage(self, conn, msg):
        """Each node's coverage raster: those in the cache said at once, the
        rest computed one at a time through the sidecar and sent to this
        socket as `coverage_tile` as each lands. The one node a request
        lacks a raster for is the node being edited: it is swept band by
        band, each band said as `coverage_band` as it lands, so the page
        shows its coverage growing rather than nothing until it is whole."""
        gd = load_geodata(msg["geodata"])
        if not gd.is_pack:
            raise ValueError("geodata %s is synthetic: its coverage is worked out on the page"
                             % gd.name)
        tiles, todo = [], []
        for node in msg.get("nodes") or ():
            raster_key = coverage_module.key(gd, node)
            hit = coverage_module.cached(gd.name, raster_key) is not None
            tiles.append({"node": node["name"], "key": raster_key, "cached": hit})
            if not hit and (gd.name, raster_key) not in self.sweeps.busy:
                todo.append((node, raster_key))
        if todo:
            sidecar = await self.sidecars.hold(conn.holder, gd)

            def growing(node, raster_key):
                async def said(km):
                    await conn.send({"type": "coverage_band", "geodata": gd.name,
                                     "node": node["name"], "key": raster_key, "km": km})
                return said if len(todo) == 1 else None

            async def work():
                for node, raster_key in todo:
                    self.sweeps.busy.add((gd.name, raster_key))
                    try:
                        await self.sweeps.raster(sidecar, gd, node,
                                                 on_band=growing(node, raster_key))
                        await conn.send({"type": "coverage_tile", "geodata": gd.name,
                                         "node": node["name"], "key": raster_key})
                    except store.StoreError as err:
                        await conn.send({"type": "coverage_error", "geodata": gd.name,
                                         "node": node["name"], "error": str(err)})
                    finally:
                        self.sweeps.busy.discard((gd.name, raster_key))
            asyncio.ensure_future(work())
        return {"geodata": gd.name, "tiles": tiles}

    # ---- the registry, to everyone ---------------------------------------

    def sims_message(self):
        """The registry: every simulation, running, paused or ended, newest
        started first."""
        rows = ([c.summary() for c in self.children.values()]
                + [self.paused_summary(n, r) for n, r in self.paused.items()]
                + self.ended_rows())
        rows = [dict(r, bytes=self.run_bytes.get(os.path.realpath(os.path.join(SIM_DIR, r["run"]))))
                for r in rows]
        rows.sort(key=lambda r: r.get("started") or 0, reverse=True)
        return {"type": "sims", "port": self.args.public_port,
                "sims": rows,
                "script_runs": [r.summary() for r in self.script_runs.values()],
                **simd_module.store_lists()}

    def forget_size(self, run_dir):
        """A run directory measured again at the next pass: it changed."""
        self.run_bytes.pop(os.path.realpath(run_dir), None)

    async def measure(self):
        """Every run directory's size, on a worker thread: a running
        simulation's every SIZE_EVERY_S, any other once, and again after it
        changes (forget_size)."""
        while True:
            live = [c.run_dir for c in self.children.values()]
            known = set(self.run_bytes)

            def walk():
                todo = list(live) + [p for p in (runs_module.run_path(n) for n in runs_module.runs())
                                     if os.path.realpath(p) not in known]
                return {os.path.realpath(p): store.disk_bytes(p) for p in todo}
            got = await asyncio.to_thread(walk)
            if any(self.run_bytes.get(p) != b for p, b in got.items()):
                self.run_bytes.update(got)
                self.changed = True
            await asyncio.sleep(SIZE_EVERY_S)

    async def report(self):
        """The registry to every socket once a wall second, and on a change
        as soon as the next tick. Pace and estimate move every second anyway."""
        while True:
            await asyncio.sleep(REPORT_S if not self.changed else 0.1)
            self.changed = False
            if self.conns:
                text = json.dumps(self.sims_message())
                await asyncio.gather(*(c.send(text) for c in list(self.conns)))

    # ---- the control websocket -------------------------------------------

    async def ws_control(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await ws.prepare(request)
        conn = Conn(ws, request.query.get("sim") or None,
                    request.query.get("quiet", "") not in ("", "0"))
        self.conns.add(conn)
        await conn.send({"type": "hello", "front": True, "port": self.args.public_port,
                         "page": simd_module.page_build()})
        await conn.send(self.sims_message())
        if conn.selected and await self.open_upstream(conn, conn.selected) is None:
            await conn.send({"type": "error", "text": "simulation %s is not running"
                             % conn.selected})
        try:
            async for message in ws:
                if message.type is not WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(message.data)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await self.from_conn(conn, msg)
        finally:
            self.conns.discard(conn)
            self.sidecars.release(conn.holder)
            conn.close()
        return ws

    async def from_conn(self, conn, msg):
        kind = msg.get("type")
        if kind == "sims":
            await conn.send(self.sims_message())
        elif kind in SIM_VERBS or kind in EDITOR_VERBS:
            # A task, so a child that takes seconds to come up, or a table
            # that takes minutes, does not hold this socket's next message.
            asyncio.ensure_future(self.verb(conn, kind, msg))
        elif kind == "select":
            name = msg.get("sim") or None
            for other in list(conn.upstreams):
                if other != name:
                    conn.close_upstream(other)
            conn.selected = name
            if name is not None:
                await self.open_upstream(conn, name)
        else:
            name = msg.pop("sim", None) or conn.selected
            if name is None:
                await conn.send({"type": "error",
                                 "text": "the page asked for %s, which this sim-mesh does not "
                                         "know" % kind})
                return
            if kind in ("sim_load", "snapshot_load") and not msg.get("run"):
                asyncio.ensure_future(self.forward_load(conn, name, msg))
                return
            upstream = await self.open_upstream(conn, name)
            if upstream is None:
                await conn.send({"type": "error", "sim": name,
                                 "text": "simulation %s is not running" % name})
                return
            with contextlib.suppress(ConnectionError, RuntimeError):
                await upstream.send_str(json.dumps(msg))

    async def verb(self, conn, kind, msg):
        try:
            if kind in SIM_VERBS:
                reply = await getattr(self, kind)(msg)
            else:
                reply = {"type": kind, "ok": True, **await self.editor(conn, kind, msg)}
            if kind not in QUIET_VERBS:
                self.changed = True         # the registry's lists may have moved
        except (ValueError, OSError, KeyError, TypeError, store.StoreError,
                drivers_module.CommandError, firmware_module.FirmwareError) as err:
            text = str(err) if not isinstance(err, KeyError) else "missing %s" % err
            reply = {"type": kind, "ok": False, "error": text}
            log("%s: %s" % (kind, text))
        await conn.send(reply)

    async def open_upstream(self, conn, name):
        """This socket's own websocket to one child, opened once and kept.

        Everything the child says comes back with `sim` spliced in, as text:
        a busy map is thousands of messages a second, and parsing each one to
        add a field would be the front's whole cost.
        """
        entry = conn.upstreams.get(name)
        if entry is not None:
            return entry[0]
        if name in conn.opening:
            return await conn.opening[name]
        child = self.children.get(name)
        if child is None or child.state != "running":
            return None
        waiter = asyncio.get_running_loop().create_future()
        conn.opening[name] = waiter
        try:
            upstream = await self.session.ws_connect(child.control_url(conn.quiet),
                                                     max_msg_size=0)
        except (aiohttp.ClientError, OSError):
            upstream = None
        del conn.opening[name]
        waiter.set_result(upstream)
        if upstream is None:
            return None
        prefix = '{"sim": %s, ' % json.dumps(name)

        async def pump():
            try:
                async for message in upstream:
                    if message.type is WSMsgType.TEXT and message.data.startswith("{"):
                        await conn.send(prefix + message.data[1:])
            finally:
                await upstream.close()
                if conn.upstreams.get(name, (None,))[0] is upstream:
                    del conn.upstreams[name]

        conn.upstreams[name] = (upstream, asyncio.ensure_future(pump()))
        return upstream

    # ---- consoles --------------------------------------------------------

    async def ws_console(self, request):
        """A station's console, pumped to the child's own console socket."""
        child = self.children.get(request.match_info["sim"])
        if child is None or child.state != "running":
            return web.Response(status=404, text="no such simulation\n")
        browser = web.WebSocketResponse(heartbeat=30)
        await browser.prepare(request)
        url = "http://127.0.0.1:%d/ws/console/%s" % (child.port, request.match_info["name"])
        try:
            async with self.session.ws_connect(url) as upstream:

                async def up():
                    async for message in browser:
                        if message.type is WSMsgType.BINARY:
                            await upstream.send_bytes(message.data)
                        elif message.type is WSMsgType.TEXT:
                            await upstream.send_str(message.data)

                async def down():
                    async for message in upstream:
                        if message.type is WSMsgType.BINARY:
                            await browser.send_bytes(message.data)
                        elif message.type is WSMsgType.TEXT:
                            await browser.send_str(message.data)

                halves = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
                _, pending = await asyncio.wait(halves, return_when=asyncio.FIRST_COMPLETED)
                for half in pending:
                    half.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await half
        except (aiohttp.ClientError, OSError) as err:
            log("console %s/%s: %s" % (child.name, request.match_info["name"], err))
        return browser

    # ---- the planner, passed through -------------------------------------

    async def planner_proxy(self, request):
        """`/planner/<geodata>/<rest>` to that pack's sidecar as `/<rest>`.

        Only geodata somebody holds has a sidecar: the page opens it
        (`geodata_open`) before it asks for tiles. The body is streamed both
        ways and left encoded as the sidecar sent it.
        """
        name = request.match_info["geodata"]
        try:
            gd = load_geodata(name)
        except store.StoreError as err:
            return web.Response(status=404, text="%s\n" % err)
        if not gd.is_pack:
            return web.Response(status=404, text="geodata %s is synthetic: it has no planner\n"
                                % name)
        url = self.sidecars.url_for(gd)
        if url is None:
            return web.Response(status=404, text="no planner runs for geodata %s: open it "
                                "first\n" % name)
        target = "%s/%s" % (url, request.match_info["tail"])
        if request.query_string:
            target += "?" + request.query_string
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        body = await request.read() if request.can_read_body else None
        try:
            async with self.proxy_session.request(request.method, target, headers=headers,
                                                  data=body, allow_redirects=False) as up:
                resp = web.StreamResponse(status=up.status, reason=up.reason)
                for key, value in up.headers.items():
                    if key.lower() not in HOP_HEADERS:
                        resp.headers.add(key, value)
                if up.content_length is not None:
                    resp.content_length = up.content_length
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(1 << 16):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            return web.Response(status=502, text="the planner for %s: %s\n" % (name, err))

    # ---- the page, live (`sim dev`) --------------------------------------

    async def page_dev(self, request):
        """The page from its development server (`--page-dev`) instead of its
        build: every request the built page would answer, and the server's
        live-reload websocket, passed through, so each edit to the page's
        sources is in the browser at once, on this port as ever."""
        target = self.args.page_dev.rstrip("/") + request.path_qs
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.page_dev_socket(request, target)
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_HEADERS and k.lower() != "host"}
        try:
            async with self.proxy_session.request(request.method, target, headers=headers,
                                                  allow_redirects=False) as up:
                resp = web.StreamResponse(status=up.status, reason=up.reason)
                for key, value in up.headers.items():
                    if key.lower() not in HOP_HEADERS:
                        resp.headers.add(key, value)
                if up.content_length is not None:
                    resp.content_length = up.content_length
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(1 << 16):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            # The server takes seconds to start, and restarts when its config
            # changes: until it answers, the page waits for it by itself.
            log("page development server: %s" % err)
            return web.Response(status=503, text=PAGE_DEV_WAITING, content_type="text/html",
                                headers={"Cache-Control": "no-store"})

    async def page_dev_socket(self, request, target):
        """The development server's websocket, pumped both ways, its
        subprotocol (Vite's `vite-hmr`) kept."""
        offered = tuple(p.strip() for p in
                        request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip())
        browser = web.WebSocketResponse(protocols=offered)
        await browser.prepare(request)
        url = "ws" + target[len("http"):]
        try:
            async with self.session.ws_connect(url, protocols=offered) as upstream:

                async def pump(source, sink):
                    async for message in source:
                        if message.type is WSMsgType.TEXT:
                            await sink.send_str(message.data)
                        elif message.type is WSMsgType.BINARY:
                            await sink.send_bytes(message.data)

                halves = [asyncio.ensure_future(pump(browser, upstream)),
                          asyncio.ensure_future(pump(upstream, browser))]
                _, pending = await asyncio.wait(halves, return_when=asyncio.FIRST_COMPLETED)
                for half in pending:
                    half.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await half
        except (aiohttp.ClientError, OSError) as err:
            log("page development server: %s" % err)
        return browser

    # ---- stations --------------------------------------------------------

    def route(self, label):
        """The child and the station name behind a `<label>.sim.localhost`.

        `alpha.lora` is station alpha of simulation lora; a bare `alpha` is
        allowed while one simulation runs, so an address from before there
        were several still reaches the same station.
        """
        station, _, sim = label.partition(".")
        running = [c for c in self.children.values() if c.state == "running"]
        if sim:
            child = self.children.get(sim)
            if child is None or child.state != "running":
                return None, "No simulation named %s is running.\n" % sim
            return child, None
        if len(running) == 1:
            return running[0], None
        if not running:
            return None, "No simulation is running.\n"
        return None, ("%d simulations are running; name one: "
                      "http://%s.<simulation>.sim.localhost:%s/ (%s)\n"
                      % (len(running), station, self.args.public_port,
                         ", ".join(sorted(c.name for c in running))))

    def resolve_host(self, label, path=""):
        """Where a request goes: the front's own app, or a child's port.

        A station request goes to its child's port as it stands, `Host` and
        all, and the child's own proxy picks the station off the first label.
        `/webrtc` stays here, because the answer has to be pointed at the
        front's relay before it reaches a browser outside the container.
        """
        if label is None:
            return ("127.0.0.1", self.control_port)
        child, why = self.route(label)
        if child is None:
            return proxy.Refusal("404 Not Found", why)
        if path.split("?", 1)[0] == SIGNAL_PATH:
            return ("127.0.0.1", self.control_port)
        return ("127.0.0.1", child.port)

    async def ws_signalling(self, request):
        """The child's signalling, with the answer pointed at the front's relay,
        which forwards the flow to the child's relay (webrtc.bridge)."""
        label = proxy.label_of(request.headers.get("Host", "").encode("latin-1"))
        child, why = self.route(label) if label else (None, "no station\n")
        if child is None:
            return web.Response(status=404, text=why)
        return await webrtc_module.bridge(
            request, "http://127.0.0.1:%d%s" % (child.port, SIGNAL_PATH),
            self.relay, "127.0.0.1", child.port, label, pass_host=True)

    # ---- the HTTP side ---------------------------------------------------

    async def api_sims(self, request):
        return web.json_response(self.sims_message())

    async def api_store(self, request):
        return web.json_response(simd_module.store_lists())

    async def api_table(self, request):
        """A loss table file, for the page's links layer: `?path=` as
        `losses_compute` or a run's `losses/<band>.bin` names it, absolute or
        relative to testbed/. Only a .bin under the cache or the runs is served."""
        path = os.path.normpath(os.path.join(SIM_DIR, request.query.get("path", "")))
        roots = [os.path.join(os.path.realpath(d), "") for d in (store.LOSSES_DIR, store.RUNS_DIR)]
        real = os.path.realpath(path)
        if not (real.endswith(".bin") and any(real.startswith(r) for r in roots)
                and os.path.isfile(real)):
            raise web.HTTPNotFound(text="no such loss table")
        return web.FileResponse(real, headers={"Cache-Control": "no-store",
                                               "Content-Type": "application/octet-stream"})

    async def api_report(self, request):
        """A run's report.md, as its script's report wrote it: `?run=` as the
        registry names the run, relative to testbed/."""
        run_dir = os.path.realpath(os.path.join(SIM_DIR, request.query.get("run", "")))
        runs_root = os.path.join(os.path.realpath(store.RUNS_DIR), "")
        path = os.path.join(run_dir, runs_module.REPORT_FILE)
        if not (run_dir + os.sep).startswith(runs_root) or not os.path.isfile(path):
            raise web.HTTPNotFound(text="no report for that run")
        return web.FileResponse(path, headers={"Cache-Control": "no-store",
                                               "Content-Type": "text/markdown; charset=utf-8"})

    async def api_coverage(self, request):
        """One node's coverage raster, by geodata and key, from the cache. A
        key names what its raster is made of (coverage.key), so the raster
        at a key never changes and a browser may keep it for good."""
        try:
            path = coverage_module.cached(request.query.get("geodata", ""),
                                          request.query.get("key", ""))
        except store.StoreError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        if path is None:
            raise web.HTTPNotFound(text="no such coverage raster")
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Cache-Control": "private, max-age=31536000, immutable"})

    async def upload(self, request, suffix=".zip"):
        """A request's body into a temporary file beside the store, streamed:
        its path, for the caller to remove."""
        fd, tmp = tempfile.mkstemp(prefix=".upload-", suffix=suffix, dir=SIM_DIR)
        size = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in request.content.iter_chunked(1 << 16):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD,
                                                            actual_size=size)
                    handle.write(chunk)
        except BaseException:
            os.unlink(tmp)
            raise
        return tmp

    async def api_firmware_add(self, request):
        """POST a firmware zip, `?name=` the file it was: installed under the
        name its node.yaml gives."""
        tmp = await self.upload(request)
        shown = request.query.get("name") or "the upload"
        try:
            got = await asyncio.to_thread(firmware_module.unpack, tmp, shown,
                                          {"source": "upload of %s" % shown})
        except firmware_module.FirmwareError as err:
            return web.json_response({"ok": False, "error": str(err)})
        finally:
            os.unlink(tmp)
        log("added firmware %s from %s" % (got["name"], shown))
        self.broadcast({"type": "firmware_changed"})
        return web.json_response({"ok": True, "name": got["name"]})

    async def api_geodata_import(self, request):
        """POST a sim-mesh geodata pack or a bare planner pack, `?name=` the
        geodata it becomes (empty: the one the zip gives), expanded into its
        own directory."""
        tmp = await self.upload(request)
        try:
            gd = await asyncio.to_thread(geodata_module.import_zip, tmp,
                                         request.query.get("name") or None)
        except store.StoreError as err:
            return web.json_response({"ok": False, "error": str(err)})
        finally:
            os.unlink(tmp)
        self.changed = True
        log("imported geodata %s%s" % (gd.name, " (pack %s)" % gd.pack_dir if gd.is_pack else ""))
        return web.json_response({"ok": True, "geodata": gd.as_dict()})

    async def api_geodata_export(self, request):
        """GET `?name=`: that geodata as a sim-mesh geodata pack, a zip made
        while it is sent, never whole on disk or in memory: it is written on a
        worker thread into a short queue this handler drains into the
        response, and a reader that goes away stops the writer at its next
        chunk."""
        try:
            gd = geodata_module.load(request.query.get("name", ""))
        except store.StoreError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        loop = asyncio.get_running_loop()
        chunks = asyncio.Queue(EXPORT_QUEUE)
        gone = False

        class Sink:
            """A write-only stream: each full buffer onto the queue, waited for."""
            def __init__(self):
                self.buf = bytearray()

            def write(self, data):
                if gone:
                    raise OSError("the reader went away")
                self.buf += data
                if len(self.buf) >= EXPORT_CHUNK:
                    self.flush()
                return len(data)

            def flush(self):
                if self.buf:
                    data, self.buf = bytes(self.buf), bytearray()
                    asyncio.run_coroutine_threadsafe(chunks.put(data), loop).result()

        def make():
            sink = Sink()
            try:
                geodata_module.export_zip(gd, sink)
                sink.flush()
            finally:
                asyncio.run_coroutine_threadsafe(chunks.put(None), loop).result()

        response = web.StreamResponse(headers={
            "Content-Type": "application/zip", "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="%s.zip"' % gd.name})
        await response.prepare(request)
        maker = asyncio.ensure_future(asyncio.to_thread(make))
        try:
            while (chunk := await chunks.get()) is not None:
                await response.write(chunk)
        except (ConnectionError, asyncio.CancelledError):
            gone = True

            async def drain():
                while await chunks.get() is not None:
                    pass
            asyncio.ensure_future(drain())
            raise
        await maker
        await response.write_eof()
        log("exported geodata %s" % gd.name)
        return response

    # ---- building a pack from its sources --------------------------------

    @staticmethod
    def build_spec(query):
        """A build's name, rectangle and resolution from a query or a JSON body."""
        bbox = query.get("bbox")
        if isinstance(bbox, str):
            bbox = [float(v) for v in bbox.split(",")]
        return {"name": query.get("name"), "bbox": bbox, "res_m": float(query.get("res_m") or 30)}

    async def api_geodata_sources(self, request):
        """GET `?bbox=w,s,e,n&res_m=[&sizes=0]`: what a build of that
        rectangle takes, for the side panel: the grid, the sources chosen and
        what each is used for, their files and what is still to fetch; or why
        it is refused. `sizes=0` leaves out asking the hosts how big their
        files are, which for a city of tiles takes seconds to minutes; a
        `to_fetch` is then null wherever something is still to fetch."""
        try:
            got = await sources_module.plan(self.cache, self.build_spec(request.query),
                                            sizes=request.query.get("sizes") != "0")
        except (store.StoreError, ValueError, TypeError) as err:
            return web.json_response({"ok": False, "error": str(err)})
        got.pop("files")
        return web.json_response({"ok": True, **got})

    async def api_geodata_areas(self, request):
        """GET: the outlines of the sources that do not cover the world,
        {areas: [{source, title, outline}]}."""
        try:
            return web.json_response({"ok": True, "areas": sources_module.areas()})
        except store.StoreError as err:
            return web.json_response({"ok": False, "error": str(err)})

    async def api_geodata_build(self, request):
        """POST {name, bbox, res_m}: a build of the sources sources.plan
        chooses, started; its progress goes to every page as `geodata_progress`. One
        runs at a time."""
        try:
            spec = self.build_spec(await request.json())
            if planner_job() is None:
                raise store.StoreError("planner-job is not built: `sim` builds it as it starts, "
                                           "and said why it could not")
            if self.build is not None and self.build.running:
                raise store.StoreError("%s is being built: one build at a time" % self.build.name)
            packbuild_module.refuse(spec)
        except (store.StoreError, ValueError, TypeError) as err:
            return web.json_response({"ok": False, "error": str(err)})
        self.build = packbuild_module.Build(self.cache, spec, planner_job(), self.build_said,
                                            preexec_fn=die_with_parent)
        self.build.start()
        log("building geodata %s from sources (%s)" % (spec["name"], json.dumps(self.build.spec)))
        return web.json_response({"ok": True, "build": self.build.row})

    async def api_geodata_build_cancel(self, request):
        """DELETE: the running build cancelled, or the ended one's row gone."""
        if self.build is not None and self.build.running:
            self.build.cancel()
        else:
            self.build = None
            self.broadcast({"type": "geodata_progress", "build": None})
        return web.json_response({"ok": True})

    def build_said(self, row):
        if row["state"] in ("done", "cancelled"):
            self.build = None
        if row["state"] == "done":
            self.changed = True
        if row["state"] in ("done", "failed", "cancelled"):
            log("geodata %s: build %s%s" % (row["name"], row["state"],
                                             ": " + row["error"] if row.get("error") else ""))
        self.broadcast({"type": "geodata_progress", "build": row})

    async def osm_tile(self, request):
        """GET /osm/<z>/<x>/<y>.png: an OpenStreetMap tile for the build
        view's map, from `osmtiles/` or fetched into it. A tile is kept a week
        (the tile usage policy's floor), fetched once however many ask at
        once, and a stale one is served when the fetch fails."""
        try:
            z, x, y = (int(request.match_info[k]) for k in ("z", "x", "y"))
        except ValueError as err:
            raise web.HTTPNotFound() from err
        if not (0 <= z <= OSM_MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise web.HTTPNotFound()
        path = os.path.join(OSM_TILES_DIR, str(z), str(x), "%d.png" % y)
        fresh = os.path.isfile(path) and time.time() - os.path.getmtime(path) < OSM_TILE_MAX_AGE_S
        if not fresh:
            pending = self.tiles.get(path)
            if pending is None:
                pending = asyncio.ensure_future(self.fetch_tile(z, x, y, path))
                self.tiles[path] = pending
                pending.add_done_callback(lambda _f: self.tiles.pop(path, None))
            with contextlib.suppress(store.StoreError, OSError):
                await asyncio.shield(pending)
        if not os.path.isfile(path):
            raise web.HTTPNotFound(text="no such tile")
        return web.FileResponse(path, headers={"Content-Type": "image/png",
                                               "Cache-Control": "max-age=86400"})

    async def fetch_tile(self, z, x, y, path):
        resp = await self.cache.request("GET", OSM_TILES.format(z=z, x=x, y=y))
        try:
            if resp.status != 200:
                raise store.StoreError("tile %d/%d/%d: %d" % (z, x, y, resp.status))
            body = await resp.read()
        finally:
            resp.release()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as out:
            out.write(body)
        os.replace(path + ".part", path)

    async def api_nominatim(self, request):
        """GET `?q=`: places by that name from Nominatim, OpenStreetMap's
        geocoder, as [{name, lat, lon, bbox}]: one request per search, and
        never two within a second."""
        q = request.query.get("q", "").strip()
        if not q:
            return web.json_response({"ok": True, "places": []})
        async with self.nominatim_lock:
            wait = self.nominatim_at + NOMINATIM_GAP_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.nominatim_at = time.monotonic()
            try:
                resp = await self.cache.request("GET", "%s?%s" % (NOMINATIM, urllib.parse.urlencode(
                    {"q": q, "format": "jsonv2", "limit": 8})))
                try:
                    found = await resp.json(content_type=None) if resp.status == 200 else None
                finally:
                    resp.release()
            except (store.StoreError, ValueError) as err:
                return web.json_response({"ok": False, "error": str(err)})
        if not isinstance(found, list):
            return web.json_response({"ok": False, "error": "Nominatim did not answer"})
        places = []
        for each in found:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                s, n, w, e = (float(v) for v in each["boundingbox"])
                places.append({"name": each.get("display_name") or q, "lat": float(each["lat"]),
                               "lon": float(each["lon"]), "bbox": [w, s, e, n]})
        return web.json_response({"ok": True, "places": places})

    def app(self):
        app = web.Application(client_max_size=64 << 20)
        app.router.add_get(SIGNAL_PATH, self.ws_signalling)
        app.router.add_get("/ws", self.ws_control)
        app.router.add_get("/ws/console/{sim}/{name}", self.ws_console)
        app.router.add_get("/api/sims", self.api_sims)
        app.router.add_get("/api/store", self.api_store)
        app.router.add_get("/api/table", self.api_table)
        app.router.add_get("/api/coverage", self.api_coverage)
        app.router.add_get("/api/report", self.api_report)
        app.router.add_post("/api/firmware/add", self.api_firmware_add)
        app.router.add_post("/api/geodata/import", self.api_geodata_import)
        app.router.add_get("/api/geodata/export", self.api_geodata_export)
        app.router.add_get("/api/geodata/sources", self.api_geodata_sources)
        app.router.add_get("/api/geodata/areas", self.api_geodata_areas)
        app.router.add_post("/api/geodata/build", self.api_geodata_build)
        app.router.add_delete("/api/geodata/build", self.api_geodata_build_cancel)
        app.router.add_get("/api/nominatim", self.api_nominatim)
        app.router.add_get("/api/nodes/sources", self.api_nodes_sources)
        app.router.add_get("/osm/{z}/{x}/{y}.png", self.osm_tile)
        app.router.add_route("*", "/planner/{geodata}/{tail:.*}", self.planner_proxy)
        app.router.add_get("/{tail:.*}", self.page_dev if self.args.page_dev
                           else simd_module.serve_page)
        return app

    async def start_http(self):
        """The app on a loopback port, the listener and the relay on the bind,
        as simd does: one port, and `Host` decides."""
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.control_port = self.runner.addresses[0][1]
        host, _, port = self.args.bind.rpartition(":")
        self.listener = await proxy.serve(
            host or "0.0.0.0", int(port), self.resolve_host,
            routes="<station>.<simulation>.sim.localhost to that simulation's simd")
        webrtc_module.log = log
        self.relay = await webrtc_module.serve(host or "0.0.0.0", int(port),
                                               self.args.relay_host, self.args.relay_port)
        log("page on http://localhost:%s/, stations at "
            "http://<station>.<simulation>.sim.localhost:%s/"
            % (self.args.public_port, self.args.public_port))
        if planner_web() is None:
            log("planner-web is not built in %s (`sim` builds it as it starts): packs are refused, "
                "synthetic ground works" % geodata_module.planner_repo())

    # ---- the run ---------------------------------------------------------

    async def open_sessions(self):
        self.session = aiohttp.ClientSession()
        self.proxy_session = aiohttp.ClientSession(
            auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
        self.sidecars = Sidecars(self.session)
        self.sweeps = coverage_module.Sweeps(self.session)
        self.cache = sources_module.Cache(self.session)

    async def run(self):
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: done.done() or done.set_result(None))
        os.makedirs(store.RUNS_DIR, exist_ok=True)
        for run in runs_module.paused_runs():
            self.paused[run.paused.get("simulation") or run.name] = run
        await self.open_sessions()
        await self.start_http()
        self.reporter = asyncio.ensure_future(self.report())
        self.measurer = asyncio.ensure_future(self.measure())
        try:
            await done
        finally:
            await self.shutdown()

    async def shutdown(self):
        log("stopping")
        for task in (self.reporter, self.measurer):
            if task is not None:
                task.cancel()
        for _row, task in list(self.fetching.values()):
            task.cancel()
        if self.build is not None and self.build.running:
            self.build.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.build.task
        await asyncio.gather(*(r.stop() for r in self.script_runs.values()),
                             return_exceptions=True)
        await asyncio.gather(*(c.stop() for c in self.children.values()),
                             return_exceptions=True)
        for conn in list(self.conns):
            conn.close()
        if self.sidecars is not None:
            await self.sidecars.close()
        if self.runner is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.runner.cleanup(), simd_module.SHUTDOWN_TIMEOUT_S)
        if self.listener is not None:
            self.listener.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.listener.wait_closed(),
                                       simd_module.SHUTDOWN_TIMEOUT_S)
        if self.relay is not None:
            self.relay.close()
        for session in (self.session, self.proxy_session):
            if session is not None:
                await session.close()


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="several simulations behind one port: the registry, the "
                    "children, the editors, the scripts, the planner sidecars, the page",
        epilog="Anything after `--` is given to every child simd as it stands: "
               "--noise-figure and --pairwise, say.")
    ap.add_argument("--bind", default="0.0.0.0:8800",
                    help="host:port for the page, the stations and the WebRTC "
                         "relay (default 0.0.0.0:8800)")
    ap.add_argument("--relay-host", default="127.0.0.1",
                    help="the address a browser sends the DataChannel to "
                         "(default 127.0.0.1)")
    ap.add_argument("--relay-port", type=int, default=0,
                    help="the UDP port a browser sends the DataChannel to, as "
                         "the browser sees it (default: the bind port)")
    ap.add_argument("--page-dev", metavar="URL",
                    help="the page from this development server (`quasar dev`) "
                         "instead of its build, for working on it (`sim dev`)")
    ap.add_argument("child_args", nargs=argparse.REMAINDER,
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child_args[:1] == ["--"]:
        args.child_args = args.child_args[1:]
    args.public_port = args.bind.rpartition(":")[2]
    if not args.relay_port:
        args.relay_port = int(args.public_port)
    return args


def main(argv=None):
    args = parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Front(args).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
