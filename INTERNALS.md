# sim-mesh — internals

Why the testbed is shaped the way it is, and the rules anything added to it
has to obey. [README.md](README.md) is how to run it and where every file
lives; this is the reasoning underneath.

## The idea in one paragraph

A station is a whole firmware, compiled for Linux and run as an ordinary
process. The cut between the real code and the simulated part is the **SPI
(serial peripheral interface) bus**: everything above it — the LoRa driver,
its carrier sense and airtime accounting, the mesh stack above that — is the
same source that runs on a board, and what sits below it is a model of an
SX1262 that hands its transmissions to a medium instead of to an antenna. The
medium rules on every frame at every receiver from a table of every pair's
path loss, which follows from where the nodes stand on their geodata. Any
number of firmwares meet on one medium this way, each above its own driver:

```
firmware A (ESP-IDF host target)            firmware B (Rust, std)
its stack, its web UI                       its stack
        │                                           │
   its LoRa driver (RadioLib)                  its LoRa driver
        │  a HAL over a GPIO shim                   │  embedded-hal
        │                                           │
        └──── the virtual radio, libsimradio-sx1262.so, provided by sim-mesh
                            │  UDP, JSON: the ether's protocol
                        the ether                   who hears what, and when
```

sim-mesh knows each firmware only through its **driver**, a Python module in
the firmware's own zip, so nothing in sim-mesh is any one project's.

## Why a host port and not an emulator

An emulator runs the image on a modelled CPU: faithful to the silicon, one
core per station, and slow. A host port compiles the same sources for the
machine you are on: fast enough that two dozen stations are nothing, and
faithful to nothing below the C. The trade is deliberate. What the testbed is
for is protocol behaviour across several nodes over time — announces, paths,
carrier sense, retries, messages — and none of that lives in the instruction
set. What it cannot catch is anything that depends on the chip being a chip:
timing at the microsecond, memory layout, cache behaviour, a peripheral's
errata.

The other ways there are were weighed and are not used. Wokwi runs in the
cloud on a budget of minutes, sandboxes custom chips with no sockets, and has
no diagram of several microcontrollers. A QEMU (Quick Emulator) fork with a
register-level SX1262 inherits the emulator's ceiling and adds a fork to keep.
Mininet-WiFi and wmediumd model 802.11 only, with no LoRa physics and no
virtual time.

## Its own launcher, and its own container

sim-mesh simulates whatever firmware brings a driver: Reticulum, MeshCore and
Meshtastic firmware are all its business. So it
is no verb of the tool that builds any one of them: it has its own launcher
(`sim`), its own image and its own site of pre-built firmware, and a
clone runs with no firmware tree beside it.

**A firmware is a zip, installed, never a build tree.** What was last
compiled in a tree is anyone's guess, so a station only ever runs installed
firmware, each under a name that says its version. The zip is the whole
hand-over between a project and sim-mesh: the project's own script makes it,
`sim firmware add` or the page installs it, and nothing in sim-mesh
reaches into a project's tree or a project into sim-mesh's.

**The driver travels with the firmware.** What sim-mesh must know of a
firmware to run it — how it is started, when it is up, how a script's verb
is done on it, what its console says about a message — changes with the
firmware, so it lives in the firmware's zip, as a Python module implementing
its category's interface (`sim_mesh.reticulum.driver` for `reticulum`). A
driver imports from sim-mesh only `sim_mesh.driver` and its category's
module; everything else in sim-mesh may change under it. Cross-protocol
layers (a `messages` layer comparing delivery across Reticulum, MeshCore and
Meshtastic) belong to sim-mesh's scripting library, above the categories.

**The radio is provided at run time.** A firmware links the virtual radio's
shared library by name and never carries it; sim-mesh puts its own on the
station's `LD_LIBRARY_PATH`. The chip model, the conductor and the ether's
protocol then change without a firmware being rebuilt: only a change to the
C interface itself (`simradio.h`) does that. A firmware compiled with the
model inside it carried whatever version it was built against, and a
renamed environment variable left such a build waiting forever at T 0. The
host's glue (its timers, its lock, its reader thread) is the firmware's
code, handed to the library with `simradio_set_services`.

**Everything runs in sim-mesh's image.** Stations need Linux: each binds a
`127.x.y.z` address of its own and the virtual-time shim is an `LD_PRELOAD`
library, and neither exists on another kernel. And a firmware needs a
system it can count on, the same on every machine, or a zip that works on
one fails on the next for a library or a Python it found there. So `sim`
runs itself inside its own image on every OS, in Docker or Podman, and the
image is that system: Ubuntu 24.04 (its C library and C++ runtime; a zip
brings everything else); the host's own architecture, because a firmware
runs only on the one it was built for; CPython 3.12 with aiohttp and
pyyaml, for sim-mesh, every driver, and a firmware written in Python that
brings its packages but not an interpreter; Node for the page, a C
toolchain and cmake for the virtual radios, cargo for the planner. Running
natively (`SIM_MESH_NATIVE=1`) is for working on sim-mesh itself. The image
is small on purpose and is not a firmware build container.
It copies nothing from the tree. `sim` mounts the directory holding
sim-mesh at its own path, and runs as the host's user, so what it writes is
theirs. The image is tagged by the Dockerfile's checksum and rebuilt when it
changes.

**A firmware's name is its base, its architecture and its version**, and
nothing else is read from it. `<base>_latest` is the newest of exactly that
base, so a project's loop is add, run, and a run started tomorrow gets
today's build without a script changing; a base keeps to one versioning
scheme, since a build stamp and a semantic version cannot be ordered against
each other. A run keeps what each name resolved to, and a paused run or a
snapshot holds its firmware against deletion: their state was written by it
and can only be resumed on it.

**Pre-built firmware is a release, served by the site.** A zip is too big for
the site's own history, so it is an asset of sim-mesh's `firmware` release,
which the site's deploy unpacks under `/firmware/` beside a generated
`index.html` whose links carry each zip's `node.yaml` facts: the page lists
what is offered without opening a zip.

**The port is 8800.** Round, and clear of everything else that runs beside
it: spangap's 9000–9011, the planner's 8787, the front's per-simulation
control and ether ports counting up from 9100 and 7100, a station's CLI
(command line) on 8081 and Reticulum's TCP (Transmission Control Protocol)
interface on 4242. It lives in
`sim`, `front.py`, `proxy.py`, `test_front.py` and the docs, and nowhere
else.

## One process per simulation

`simd.py` is the ether, the stations, the proxy and the control server in one
asyncio loop, and the stations' ptys (pseudo-terminals, their consoles) on a second loop in a thread of their
own (below). It could have been four processes talking over sockets. One is
better for one reason that matters:

> **Losses reach the medium by method call.**

Dragging a station on the map recomputes its row of the run's loss table,
and the new tables go into the ether whole between two frames. There is no
wire to define, nothing to serialize, no version to keep in step, and no
window in which the map and the medium disagree about which row is current.
The ether keeps a `main()` that reads a nodeset and a
directory of tables, so it still runs alone for its own tests, but the
testbed never uses that path.

The price is that one crash takes the lot down. That is the right price here:
a testbed is a person's session, and a medium that outlived its stations, or
stations that outlived their medium, would be a worse thing to debug than a
process that stopped.

## Several simulations: a front and its children

```
browser/driver ── localhost:8800 ──► front.py ──► simd (lora)   127.0.0.1:9100, ether :7100, 127.16.0.0/22, runs/lora/
                                              └─► simd (supe)   127.0.0.1:9101, ether :7101, 127.20.0.0/22, runs/supe/
```

One simulation is one simd, and several are several simds, each a child
process of `front.py`, which owns the one port the container publishes.
Nothing is folded into one process, for two reasons:

- **The loop is the pace limit.** At a hundred stations one simd's event loop
  is what a virtual-time run waits on. Two simulations in one loop would each
  run at half the pace, and a slow one would slow the other. As processes they
  share only the host's cores.
- **A crash is one simulation's.** The paragraph above holds per simulation:
  a simd that falls over takes its medium and its stations with it, and
  nothing else. The front keeps the registry row, exited, with the child's
  last lines, until somebody stops it.

A child is a simd with the flags a person would give it by hand — its own
`--bind` on loopback, `--ether`, `--net` and `--run` — so everything that is
true of one simd is true of each child, and a child can be driven directly on
its own port. The front allocates those four so that two children never
share any of them, and checks that a port is free on the host before it hands
it out, so a simd started by hand on one is stepped around. A network is
given out only when no socket on the host is bound to any of its addresses
(read from `/proc/net/tcp` and `/proc/net/udp`) and the front wins a
non-blocking `flock` on `$TMPDIR/sim-mesh-nets/<net>.lock`, held on an open
descriptor until the simulation ends and dropped by the kernel if the front
dies. The lock is what keeps two fronts apart in the window between giving a
block out and its stations binding it (the loss table and the child's start
come between); the bound check is what steps around a simd started by hand,
which takes no lock. The children's networks start at `127.16.0.0/22`, and
everything below is left to simds started by hand.

Each child has a process group of its own, which its stations inherit. A
child that dies without stopping its stations leaves them in that group, and
the front kills the group when it sees the child exit: stations left behind
would hold their addresses and their ether port against the next simulation
given them. Each child also asks the kernel to send it SIGTERM when the front
dies (`PR_SET_PDEATHSIG`), so a front killed outright does not leave
simulations running with nothing in front of them.

The front reads each child's output line by line, as it comes, into
`runs/<name>/simd.log` and a tail kept for the page. The line in which simd
says its control page is listening is what makes a child ready; there is no
polling of its port.

**The registry** is fed by one extra control socket per child, the front's
own, opened with `?quiet=1`: simd leaves `tx`, `rx`, `radio` and `levels` off
a quiet socket, and those are nearly all the traffic on a busy network and
nothing the registry reads. From the `snapshot`, `node`, `node_gone`,
`nodeset`, `clock` and `error` messages on it the front keeps what each
simulation loaded, its stations' statuses, its clock and its plan, and sends
the whole registry to every socket on the front once a wall second.

**A child starts on a whole run.** Before it starts, the front loads and
checks the geodata, the nodeset and the script, takes the
loss tables from the cache or computes them, and lays the run directory out;
the child then loads that directory as it would one it was handed by hand,
and settles each node's firmware from the run's rules (the script's, handed
over by its runner), fetching what they name, before a station of it
starts: a fetch is a wait the child's own loop can take.
All of that is work a person waits on with a page open, so it is the
front's, with its progress on every page, and none of it can stall a
running simulation's loop. The table is a subprocess (`losses.py`) whose
progress is read line by line, never a computation on the front's loop.

**Control through the front.** A page or a driver holds one websocket to the
front, and the front holds one websocket per child that socket uses. What a
child sends goes back with `"sim": <name>` spliced into the text rather than
parsed and re-encoded, because on a busy map that is thousands of messages a
second. A page selects one simulation and its socket to any other is closed,
so a page is never sent a map it is not showing; a driver names the
simulation in each message, or once in `?sim=`.

**Answers go to everyone, so an id is the asker's own.** simd broadcasts
each `command_result` to every page and driver on it, as it does everything
else. A driver matches an answer to its question by the `id` it sent, and
that id carries a random prefix of the driver's own: two drivers counting
from one would each take the other's answers.

**`sim` at the top of a message means a child said it.** The front splices
`sim` into everything a child sends, and a page and a driver tell a child's
messages from the front's own answers by it: an answer carrying `sim` is
never matched to the question it answers, and whoever asked waits forever.
So a front answer that names a simulation calls the field anything else
(`script_run` answers `simulation`).

**A script that is done pauses its run, it does not stop it.** When a
script is finished, the network it built (its identities, paths, message
history) is the thing worth keeping, so it ends with `sim_pause()`: the
library asks the front to pause the simulation `by: script`; the front
stops the child, which flushes every station, and only then copies the run
into its own `paused/`, as a snapshot would be, since state copied from a
running station can be half-written. Resuming is a snapshot load into a new
run directory, so the paused run and its report stay as they were written.
The script, not the front, pauses and then reports, so a script run from a
shell ends the same way as one run from the page. A script that says
nothing leaves its simulation running.

**Done and paused are one state with who asked.** Both are a run waiting
in `paused/`, resumed, stopped and deleted alike; `paused.by: script` in
`run.yaml` is the script's own pause, which the page and `sim list` call
done, so a run the script finished is told apart from one a person stopped
halfway. A second state would have doubled every path that resumes, stops
or lists a pause for a difference only the page shows.

**Scripts run beside the front and the simulation, never in them.** A script
is a process of its own (`sim_mesh.runner`) that starts its simulation, or
attaches to one, over the front's port like any other driver; its output is
read line by line and sent to every page. A script that loops, blocks or
dies takes nothing else with it. What a station must be given at its first
boot, which happens inside simd whenever a station comes up with no state,
travels there as data (`.on_first_boot()`'s rules: a selection, lines and
verbs), not as the script's code, so no script code ever runs in simd's loop.

**A script is synchronous.** It reads as it runs: `.firmware(...)`, then
`.exec(...)`, then `sim_wait(60)`, each returning when it is done, with no
`await` to forget (a forgotten one silently does nothing) and no `async
def main` to wrap it in. The library holds the simulation on an event loop
of its own in a thread beside the script's and hands each call to it. What
a script loses is easy concurrency; what a driver needs of it, many sends
at their own instants, is `after=` and `wait=False`, and the traffic driver
keeps its own async core on the library's loop.

**What must happen at one instant is one message.** A driver's asks cross
a websocket to simd and its answers come back the same way, and in a
virtual-time run T does not wait for that: while every station is idle the
ether moves T straight to the next thing any of them needs, which can be
minutes on. A driver that asks a sender for its route, waits for the
answer, then asks it to send, sends minutes late, and its sends bunch into
bursts. So steps that belong together travel as one `sequence`
(`Sim.sequence`), which simd runs back to back at the T the first is due:
the traffic driver's route and send are one.

**Declarations start the simulation.** `sim_speed()`, `.firmware()` and
`.on_first_boot()` are collected until the script first does something, and
that starts its simulation with them, so a simulation is always started
knowing what its nodes run and in what time; `sim_speed()` after that is
refused, because the ether keeps one clock for its whole run.

**What is done is a method of what it is done to.** A script's commands to
nodes are methods of a selection (`nodes(tag="gw").reset()`), written once
and done to every node of it, with `after=` and `wait=False` handled once
for all of them; a single node is a selection of one, not a type of its
own. A firmware category's commands are a layer reached from a selection
(`.reticulum`, and `.reticulum.lxmf` within it), the pandas way of grouping
methods, so a verb's place in the script mirrors its name in the driver
(`lxmf.send` in category `reticulum`), and a layer is for its category's
nodes only: one command serves a mixed network. Said on the class, `Node`,
a firmware's command is the same command as data, a first-boot rule, so
there is one spelling for doing a thing now and for doing it at every first
boot.

**The estimate.** simd knows T and the pace but not what the run is for; only
the driver knows when its traffic hour ends. So a driver sends `plan`, its
phases and the T each ends at, and simd puts it in every `clock`. The front
keeps each child's T against the wall for the last two minutes and divides
what is left of the plan by that pace. Two minutes, because the pace of a
virtual-time run moves with what the stations are doing — a warm-up is slower
than a quiet drain — and a whole-run average would still be answering for
the first minute an hour later. A real-time run's pace is one.

## Nothing blocks

Every wait is an awaitable. A framed RPC (remote procedure call) query on a
station's console is a future the drain
completes, with a timeout, and the websocket handlers are coroutines. The one
rule that follows: **a subscriber must not be able to stop the medium.** The
ether calls its `on_tx`/`on_rx`/`on_station` subscribers inline and swallows
what they raise, because a page that has gone away is not the air's problem.

## The ptys have a thread of their own

In virtual time the main loop runs every barrier, and a barrier waits for the
slowest thing on that loop. So what the stations print is not read there:
every station's pty is a reader on a second event loop, in a thread of
simd's own (`stations.Ptys`, started with the first station), which reads
it, takes the framed-RPC replies out
(`rpc.FrameDemux`), appends the rest to the station's log and watches for the
capability marker. The main loop is handed only what it acts on, with
`call_soon_threadsafe` and in the order it was read: a reply for the station's
RPC client, the marker, the console bytes while somebody has the station's
console window open, and the end of the stream. The log file is the pty
thread's alone from a station's first start, so a restart's banner and the
last bytes of the process before it land in the order they happened; when a
start ends, the thread reads what is left on the pty before it closes it.

In a virtual run the ether asks, before T moves past stations that have run,
for their ptys to be read to the end (`stations.catch_up`, on this thread);
the main loop hears it is done behind everything those reads handed it, so a
reply is acted on at the T it was printed at.

Both loops keep the rule above. The pty thread blocks in nothing but its
loop's own wait, and the main loop writes to a pty the way it always did,
non-blocking, with what the pty will not take held for its writer, so a frame
is never cut; in a virtual run the bytes wait there, too, until the ether says
the station is in step (`Ether.sync`).

## One port, and `Host` decides

The control page and every station's web UI are on the same published port.
The front listener reads just enough of each request to find `Host`:
`<name|id>.sim.localhost` is proxied to port 80 on that station's own address,
and everything else is proxied to the aiohttp app, which is bound to a
loopback port nothing outside can reach.

That shape — a proxy in front of the control app, rather than the control app
routing to stations — is what keeps a station's URL space its own. The page's
websocket, the station's websockets and the station's absolute links all work
because after the head is read the connection is a raw byte pump and the
station is answering on its own origin.

A name resolves through the run's nodeset, an id through arithmetic. Both
because a nodeset's nodes get renamed and a bookmark should survive it, and
because the proxy alone — for a station set somebody started another way — has
no nodeset to ask.

The front is the same proxy with a different resolver. `alpha.lora.sim.localhost`
has two labels: the front routes on the second to that simulation's port and
forwards the request untouched, and the child's own listener routes on the
first to the station. A bare `alpha.sim.localhost` goes to the one running
simulation when there is exactly one, and otherwise is refused with the names
to choose from.

## Firmware, geodata, nodeset, script: split along what changes on its own

```
geodata ──┐
          ├──► loss table (derived, per band, cached) ── + links + shadowing + antennas + offsets ──┐
nodeset ──┘  (positions, heights)                                                                   │
nodeset: antenna, role, radio, tags ───────────────────────────────────────────────────────────────┤
script: inputs, firmware() rules, setup ───────────────────────────────────────────────────────────┼──► simd ──► ether + stations ──► run
installed firmware (by the rules' names) and its drivers ──────────────────────────────────────────┘
snapshot = geodata + nodeset + script + tables + rules + every station's store
```

A simulated network is several things that change for different reasons,
and each is its own file so that changing one leaves the others alone:

- a **firmware** is a station build and its driver. It changes when somebody
  builds one, and never because of where it runs;
- the **geodata** is the ground. It changes when the ground data does, and
  never because of a node. It names no node, and a nodeset names no geodata:
  a nodeset stands on any geodata whose extent holds one of its nodes, and
  synthetic ground lies at 0°, 0° so that a nodeset made on one synthetic
  ground stands on every other;
- the **nodeset** is which nodes stand where, with their maximum powers,
  antennas and tags, and nothing that is said to a station. What the page must know
  without running anything comes from tags and one shared file: a role tag
  (`transport`) is the transport's second ring, a `no-radio` tag a node
  without a radio, and `scripts/globals.py`'s radio the coverage, the links
  and the bands for every other node;
- the **script** is everything else the software is told, as Python,
  starting with what each node runs (`firmware()`): which firmware a
  network runs is the experiment, not the network, so the same nodeset run
  on one firmware, on another and on a mix is one script whose inputs are
  chosen three ways, not three copies of every node. Its rules name nodes
  through tags and, as an escape hatch, by name, so one script fits any
  nodeset, and one nodeset serves scripts that differ by a few lines;
- the **loss table** follows from the geodata and the nodeset's geometry and
  from nothing else, so it is derived and cached under a hash of exactly
  those: which nodes, by name, where and how high. Changing a node's
  antenna, role, radio, firmware, offsets or links, the geodata's
  shadowing, or the script never recomputes it. A table finds a node by its
  name, so relabelling one does: its row and column are computed again, and
  every other pair comes from the table cached before.

**Standard ground and nodesets come from an index, by their sha256.** A
test that many people run means the same thing only on the same bytes, and
geodata built from sources is not that: the sources change, and a build
next month differs from this one. So a pack or a nodeset meant to be shared
is published once, as a file, and an index lists it with its sha256; the
installing machine checks the hash and refuses anything else, and a changed
one is a new name, never an update, since an update would quietly change
what every earlier result on that name meant. The front fetches, not the
page, so `sim geodata add` and the page take one path and an index needs no
cross-origin permission; an address may be a local path, so an index on a
stick works offline. One file lists both kinds because whoever publishes
ground usually publishes nodesets for it, and a nodeset entry names its
geodata so installing it can bring that along. What was installed from where
is a dot file beside it (`.origin.yaml`), which export, the store's
listings and the geodata file all pass over. An entry counts as installed
when the origin beside it names the entry's sha256; a geodata or nodeset of
that name with no origin, or another one, is taken, and installing over it
is refused rather than compared byte for byte.

**Every row says what it takes on disk.** Sizes are walked on a worker
thread: a running simulation's run directory every ten seconds, any other
once and again when it changes state, so the registry sent every second
reads a number and never walks a tree.

**A script names no firmware of its own.** Which firmware is the experiment,
so a system script asks for it as an input (`script_input("firmware", type=Firmware)`)
and the person running it chooses, from those installed, filtered by the
category the script needs; the run keeps the choice. A script written for a
firmware's own lines can still name one, but the scripts sim-mesh carries
name none, which is what keeps sim-mesh free of any one project.

**A firmware rule is a condition, kept.** `firmware(which, name)` holds
its selection as a condition over each node's facts (`sim_mesh.select`), not
as the list of names it matches today, and the run keeps its rules. So a
node placed later or retagged runs what the rules say of it, and a rule
written at a script's top, before there is a simulation, means the same
thing whenever it is applied. The selection is Python's set algebra (`&`,
`|`, `-`, `~` over `nodes(field=value)`) because that is what a Python
reader already knows for "these and those", and a rule sent to a simulation
is that condition as plain data, which is why a function in it is refused.

**There is one board, and a node's maximum power says which way it is
built.** What sits between the firmware and the antenna connector is an
SX1262, and it is a node's property and not a build's: one firmware serves
every node. A node's `max_dbm` is its maximum power at the connector, 22 dBm
when it states none. At 22 dBm or below the node is a bare SX1262, whose
chip puts out that maximum itself; above it, up to 27 dBm, the node is an
SX1262 behind a GC1109 front-end module, as a Heltec V4 is, since no bare
SX1262 reaches past 22 dBm and the GC1109 is the front end the testbed has
measured. A figure above 27 dBm describes no node and is refused. A node is
sent at its maximum: the coverage draws it, and the startup script's radio
sets it (`tx_dbm="max"`) — said by the script, since a station is told
nothing behind a script's back. A lower power for one node is a lower
`max_dbm`, which the map then draws too. The station is told its board
(`SIM_MESH_BOARD`) by `testbed/boards.py`, whose front-end figures are the
Heltec V4 board straddle's own.

**A front end is modelled twice, with one curve.** The chip model
(`radio/src/model.cpp`) puts the board's transmit curve between what the
chip radiates and what the medium is handed, and adds its receive gain to
every level the chip reads, so the medium only ever deals in connector
power. A firmware that drives a front end takes the same figures
(`SIM_MESH_BOARD`) in place of its build's own and converts the other way,
antenna dBm to chip drive and chip RSSI to connector RSSI, as it does on the
board; rounded the same way, a station asked for 27 dBm behind the GC1109
drives the chip at 18 and the medium carries 27, and a station told no front
end is a bare SX1262, 22 dBm at most. A driver whose firmware takes the
chip's own power converts with `chip_dbm`, the same curve. The node's
maximum is the firmware's ceiling too, so a station left at its default
power sends at its node's maximum.

**Antennas are a layer, like offsets.** A pattern's gain depends on the
direction to the other end, so a pair's gains are not one figure per node
but one per pair and direction, from the two antennas' tips in three
dimensions. They are taken off the model's loss when the tables are handed
to the medium (`losses.with_antennas`), as the offsets are added, so a new
antenna or a new aim never recomputes the model, and the medium still reads
one figure a pair. The tips need the ground under each node, which on a
pack is the terrain there, asked of the sidecar once per node and kept in
the run.

**Offsets are a layer, never baked in.** A nodeset's offsets (dB added to
one pair's computed loss, both ways) are where measurements correct the
model, and they are added when the tables are handed to the ether
(`losses.with_offsets`), in simd and in the analysis tools alike. The cached
table stays the model's own, so an offset is changed without a recompute,
and the model's error is the offsets themselves, to be driven towards zero.

**Links are a layer too, and the first.** A nodeset's link states one
pair's loss outright, a figure better than the model's (a measurement, or
another model's), and it replaces the model's cell before anything else
goes on (`losses.with_links`), so the antennas and the offsets still add
to it. The figure is the pair's, not a frequency's, so it goes into every
band's table as stated; within a band the ether moves it to the frame's
own carrier as it does every cell, a few hundredths of a dB across the
EU868 channels.

**Shadowing is a layer, one draw per pair.** Log-distance gives every pair
at one distance the same loss, and P.1812 sees only the ground it is given;
real links differ by what else stands between them, and that difference is
what makes a hidden node or a lucky long link.
A geodata's `shadowing_db` adds to each pair's loss, both ways and in every
band, that spread times a standard normal drawn from SHA-256 of
`shadowing_seed` and the pair's two node names (`losses.with_shadowing`).
The draw is fixed for the run and depends on nothing that happens in it, so
two runs that differ only in their traffic or their firmware stand on the
same ground: the common random numbers a paired comparison needs. Names,
not station ids, because a node keeps its name from one run to the next. A
loss that varies over time is fading, a different thing that lets every
retry through in the end; that is the ether's (`--fading-db`), and this is
not that. A pair never heard stays
so, a measured cell already holds its path's own shadowing, and a stated
link is the figure as stated, so none of them is drawn on. On a pack the
table it is laid over must be a median: P.1812 at 90 % of locations already
adds up to 1.28 σ_L of location spread (about 2.5 dB, with σ_L ≈ 1.96 dB at
868 MHz), and the draw on top would count that spread twice. So a pack
geodata with shadowing states `loc_pct: 50`, and simd warns of one that does
not.

A node is referred to **by name** everywhere, and its **id** is stored and
editable. The name is what a person means; the id is the station's network
identity, which its loopback address and MAC (media access control) address
follow from, so it has to be stable across saves and loads and unique across
firmwares — two processes answering under one id are one station to the medium
and two sockets on one address. Changing it moves the station, which is why
the editor restarts a station with state when its id changes.

A network also has a history, and conflating it with the design makes both
useless: a saved thing that always carried the stations' state could not
describe a network that has not happened yet, and one that never carried it
could not bring one back. So the files are the design, and a **snapshot** is
a moment of a run: the geodata, the nodeset and the script as the run had
them, its loss tables, and every station's store. Starting from geodata,
nodeset and script is a factory reset of the whole network, reproducible
from files you can read; starting from a snapshot restores a moment, without
recomputing anything and without the planner.

A snapshot keeps its script's copy. Setup runs only on a station with no
state, so a script changed after the snapshot would never reach its
stations, and a station factory reset after the snapshot is loaded is set up
by the copy, the way the first run set it up; a changed script is a new
simulation from scratch.

Half of a network is its identities, keys, paths and message history, which is
why snapshots exist at all and why they are a directory rather than a file.
A snapshot is the stores, so what it brings back is what each firmware
reloads from its store at boot, as of its last write to it
([README.md](README.md#runs-and-snapshots)).

## A run is an output, and a snapshot is taken live

The stations run in a run directory, `runs/<name>/`, never in a nodeset or a
snapshot. Without a working copy there would be no moment at which the thing
on disk was not already changed, since the stations are always writing. The
run holds its own copies of everything the simulation reads — the geodata,
the nodeset, the script, the loss tables, the firmware rules and which build
each firmware resolved to — so that an edit made during the
run changes the run and not the files it started from, and so that the
analysis tools read the network as it was run and not as the files stand
now. A run is never written over: a new one goes beside it.

A snapshot is taken **while the stations run**. Waiting for quiescence would
mean stopping a network to photograph it, and a testbed is watched rather than
batched. What makes that honest is the flush below; what makes it safe is that
the firmware's store commits whole files, which is a guarantee it already has
to meet for a power cut.

Logs and the record stay in the run and are never copied into a snapshot:
they are an account of one run, and a snapshot is something to run.

**Nothing a run writes goes on a bind mount.** Stations' stores, the record
and the logs are written constantly, and a bind mount on Docker Desktop is
virtiofs: slow, with file semantics of its own that a store on flash never
meets.

**Two runs are compared by one version of the tools.** Every column of a
comparison is recomputed from both runs' records by the current analysis
code, never taken from an older report, so a difference between the columns
is a difference between the runs.

## Real ground: the planner as a sidecar

The ground data and the propagation model are sim-mesh's own planner, the Rust
workspace in `planner/`: packs compiled from public terrain, clutter,
building and road data, and ITU-R (International Telecommunication Union,
radio sector) Recommendation P.1812-8 over a real profile. The crates came
from Sergey's planner and keep their names; sim-mesh carries the ones it runs
(core, terrain, propag, opt, coverage, pack, buildings, import, render, web,
wasm, and its own job) and changes them as it needs, since sim-mesh takes over
planning and simulation from the tools it grew out of, building the packs
included. The front runs the workspace's web server, `planner-web`, as a
**sidecar**, and asks it.

```
page  ── GET /planner/<geodata>/tile.bin ──► front ── GET /tile.bin ──► planner-web (that pack)
front ── losses.py ── GET /link.json?ax&ay&bx&by&tx_h&rx_h × pairs ──► planner-web
page  ── GET /planner/<geodata>/link.json ─► front ──────────────────► planner-web   the pair inspector
front ── GET /loss/start, /loss/status, /loss.bin, one node at a time ──► planner-web   coverage
```

- **A process, not a library**, because the planner is Rust and the front is
  Python, and what it serves over HTTP is exactly what sim-mesh needs: tiles
  for the map, roads, buildings, places, and one pair's loss with its
  evidence.
- **One per pack in use**, because `planner-web` serves one pack. The front
  starts it on a free loopback port when the first simulation or page
  opens geodata on that pack and stops it when the last one lets go, so a
  front with no pack open runs none.
- **Behind the front's port**, as `/planner/<geodata>/…` with the prefix
  stripped: the page has one origin, `planner-web` needs no CORS
  (cross-origin resource sharing) handling it does not have, and only
  sim-mesh's page calls it, so the planner's own page and its absolute paths
  never matter. The page renders the ground with the planner's own renderer,
  `planner-wasm`, from a copy vendored into the page's tree, so the page
  builds with no planner beside it.
- **Every cell is one `link.json`**, the same request the pair inspector
  makes, so a cell of the table is what the inspector shows for the same two
  nodes, near-field pairs included. The table asks it `lean`, of a sidecar
  that lists the option: the same reply without the profile the inspector
  draws. `link.json` already composes
  everything a cell needs: the profile, the P.1812 call, a near-field model
  where the two are too close for P.1812, which model it used, and the
  Fresnel verdict. The planner's own pairwise sweep is not used: it drops
  every pair beyond a link budget and every pair under 250 m, and both are
  pairs the medium needs (below).
- **Pairs are asked in parallel**, as many at once as the sidecar has
  render slots, and the sidecar computes them in parallel: it reads the
  layers a pair needs without their locks wherever the pack's layout allows
  (an uncompressed strip TIFF, as the pack builder writes them), with the
  same rows and the same decoder as through the lock, so the numbers are the
  same. Both sides size themselves by the process's CPU affinity mask, as
  simd places stations, so `taskset` bounds a table build too.
- **What the sidecar decides, and what it does not.** `link.json` takes no
  carrier and judges at the planner's EU868 parameters, 869.525 MHz, 50 % of
  time and 90 % of locations; a pack therefore has an 868 table only,
  and its header says so. The percentage of locations is the one parameter
  a request may change (`loc_pct`, from the geodata), for both ways of the
  both-way mean. sim-mesh's copy of the planner holds it to P.1812's range,
  1 to 99, itself: a failed P.1812 call is answered with the near-field
  model, which would turn a bad value into a confident number. A pair with
  an end off the pack is never heard here rather than asked, because the
  sidecar would clamp the point onto the pack's edge and answer for a place
  the node is not. A sidecar answers from the clutter raster alone until it
  has indexed the pack's buildings, a different number by tens of dB, so a
  table waits for the index before its first pair and throws away a reply
  given before it.
- **Coverage is the planner's own sweep, one node at a time.** A node's
  coverage raster is `planner-coverage`'s point-to-area sweep from its
  antenna, cut to a square around it by `loss.bin` and cached by the
  pack, the node's position and its height: path loss only, so a change of
  power, antenna or radio reuses it, and the page adds those, the antenna's
  gain toward each cell over the terrain under it, which it fetches on the
  raster's own grid. The sidecar holds
  one sweep and a new one cancels the last, so the front asks for one node
  at a time per sidecar; nothing is swept until a page asks with the layer on.
  The cache is keyed by what the sweep is asked, not by the code that
  answered, so a change to how the sweep computes empties
  `testbed/coverage/` by hand.
- **The sweep and `link.json` compose the terminal the same way.** For a
  terminal in the open below the clutter around it, P.2108 §3.1's
  correction A_h is to a loss computed from the clutter height, so both
  raise the terminal to that height on each bearing, run P.1812, then add
  A_h (`tx_model_h_m` beside `tx_terminal_db` in the sweep's parameters).
  Adding A_h to a run from the antenna itself charges the same obstruction
  twice.
- **An antenna inside a building is indoors, where it is.** One inside a
  footprint, below its roof, is not raised onto the roof and has no P.2108
  surroundings: both calculations take its own building out of its paths
  (the path starts inside it) and charge P.2109's median entry loss for a
  traditional building instead (`Indoor` in planner-web). Read as P.2108
  surroundings, a node on the ground inside the Fernsehturm's footprint was
  modelled from the tower's 253 m. `link.json` reports the antennas where
  they are (`tx_h`, `rx_h`), the heights the model ran at beside them
  (`model_tx_h`, `model_rx_h`), and each indoor end's entry loss, and its
  Fresnel check traces the real ray between the real antennas.
- **A snapshot carries its tables**, so it reloads without the planner, and
  a simulation on synthetic ground never needs one.

## Building a pack, and the node maps

```
front ── fetch into geodata/.cache/<source>/ ───────────► the sources' hosts (sources.py)
front ── planner-job pack-build, one JSON object on stdin ─► planner-job   (packbuild.py)
planner-job ── {"step","done","total"[,"part","parts"]} per line ─► front ── geodata_progress ─► pages
planner-job ── {"manifest": path} | {"error": sentence}, last ─► front: geodata/<name>/, its geodata.yaml
front ── planner-job nodes-import {source, file, bbox, companions, max_age_days, now_unix} ─► planner-job
planner-job ── {"nodes": [...], "report": {...}} ─► front ── nodeset.from_imported ─► nodesets/<name>.yaml
```

`planner-job` is one binary crate with two verbs, each one JSON object in on
standard input and JSON lines out on standard output, diagnostics on
standard error, exit 0 or non-zero with a last `{"error"}` line. It holds no
fetching: **the front fetches, the compiler compiles.** That keeps the
compiler testable from files, keeps every request to a public host in one
place (sources.py: one user agent, polite retries, a cache shared by every
build), and lets the front show a download's bytes and a compile's steps in
one row.

pack-build's steps, each only when it applies: `terrain`, `osm`,
`buildings`, `lidar`, `landcover`, `clutter`, `population`, `manifest`. Its
input names every file (the GLO-30 tiles, each land cover source's tiles
with their projection and class table, the ITU maps' directory, the PBF
extract, a directory of LoD2 CityGML per source, one of CityJSON, the 1 m
XYZ terrain and surface tiles of each UTM zone, terrain and surface
GeoTIFFs with their projection, a population CSV with its layout or a
population GeoTIFF), each source's name and notice with its files, so the
manifest credits each source for what it gave; it reads only the LoD2
tiles and lidar pairs that meet its grid, and the front hands it a directory of links
to just the tiles this rectangle needs, since the cache holds every tile any
build fetched.

**What the compiler holds grows with the rectangle, never with a file.**
An OpenStreetMap extract is the smallest region holding the rectangle, a
state or a country, so the reader first finds the nodes within 3 km of the
rectangle and keeps only the ways through them (`osm.rs`), and their nodes;
the ways of a postal area the rectangle may lie inside are the one
exception, kept wherever they run. A city takes about half a gigabyte from
Austria's 812 MB extract, where holding the country's ways took 4 GB, more
than a Podman machine has by default.

**A source is parameters, never code.** What a pack is built from changes far
more often than how a kind of data is read: a new region's lidar comes in a
feed standard, a tile scheme and a file format sim-mesh reads already. So an
entry picks one finding method, one way of reading and one format, and fills
in their parameters; the methods and the readers are sim-mesh's. A source
file can come from anyone, and an entry that could carry code would make
taking one running a stranger's code. A dataset no method fits is a new
method or reader in sim-mesh, which every source can then use.

**The front chooses the sources; the compiler takes what it is given.**
`sources.plan` takes every source of the source files whose coverage meets
the rectangle (with the shipped ones: the state surveys' terrain, surface
and LoD2 where it touches a German state, Hessen's LoD2 excepted,
the Zensus grid where it touches Germany, AHN, 3DBAG and CBS where it
touches the Netherlands, BEV's terrain and surface and Statistik
Austria's grid where it touches Austria, the lidar terrain and surface
models of France, Flanders, Norway, Estonia, Czechia, Malta and four
Spanish and Italian regions, and terrain models in Poland and six more
regions, 3DEP, NLCD and WorldPop where it
touches the United States, GLO-30 and OpenStreetMap everywhere) and says what
each is used for, so the page offers no choice and the dialog after
**Build** reads the same list. `packbuild` hands each chosen source's files
to the compiler input its format and layer go to (`packbuild.INPUTS`), never
by the source's name, and refuses two sources for an input that takes one.
The 1 m pairs, LoD2, the GeoTIFF terrain and surface and land cover take
several, so a rectangle across Berlin and Potsdam, or across a state border,
takes both states'. GeoTIFF terrains and surfaces pair by projection, each
sample taking whichever tile covers it, so two states in one zone make one
pair; XYZ tiles pair tile by tile, by the corner in their names, so a
GeoTIFF surface measured against an XYZ terrain says so (`pairs_with`)
rather than being guessed into a pair by its zone, which on a border would
take a neighbour's surface away from its own terrain.

**Sources are data; some of the compiler's readers are still fixed.** A
GeoTIFF terrain, surface, land cover or population raster comes with its
proj string (land cover with its class table as well), a population grid
with its delimiter, columns, projection and cell, CityJSON with its
projection and height attributes, so those are any source's. XYZ and
CityGML are read as the German state surveys deliver them, in an ETRS89
UTM zone (used as the grid in a pack of that zone, projected point by point
in another: Hessen's zone-32 tiles in a pack centred east of 12° E), XYZ at
1 m, a worldwide surface as EPSG:4326 (`sourcefile.COMPILER_READS` and the
GeoTIFF rules), and `sourcefile.py` refuses a source that asks otherwise
when the file is read: an entry the compiler would misread fails in
`sim source check` and in the tests, not halfway through a build. A
GeoPackage grid and an INSPIRE population grid reach the compiler as the
CSV the front writes from them beside the download (x, y of each cell's
centre, value); the INSPIRE one's projection and cell come from its cells'
EU grid codes, said in a `.json` beside the CSV, and a file that mixes
grids is refused.

**A tile scheme is computed, not listed, wherever it can be.** A template
names each tile from its corner, so finding a rectangle's tiles asks no
host anything; a tile no host has is no data. A coverage service (Hessen's
WCS) is a template too, its request naming a square's two corners. Where
the name carries something no rule gives (a survey year per tile), the
listing is read: a feed, a directory listing, an index of footprints, an
ArcGIS layer (dumped a page at a time into one GeoJSON kept as a meta file,
the newest survey of each tile kept), or the central directories of the
zips a state publishes whole. A zip is never fetched whole for a tile:
its directory is read by range (its last 64 KB, the zip64 end record where
there is one, then the directory), and each tile's member fetched alone and
inflated, so Sachsen-Anhalt's 10 GB archives cost a district's megabytes. A
zip stored uncompressed inside another (Bremen's LoD2) is read in place,
its members' offsets the outer archive's. A host's quirks are met where
they are general, never per source: one that refuses HEAD is asked for two
bytes (GeoSN's shares answer `bytes=0-0` with the whole file), one that
omits its intermediate certificate is reached through the intermediates
shipped beside the source file, and a CityGML or XYZ file with a page
appended after it (Schleswig-Holstein's) is read up to its end. A GeoTIFF
a template asks of a service is checked for what it is: an exception page
in its place (Norway's 200, Estonia's 400, Galicia's JSON labelled
image/tiff) is no data there, and so is a chunk the file stores as no
bytes (Norway's and Czechia's empty boxes), which read would decode the
file's header as heights.

**A service that reprojects is asked for UTM.** Most national surveys keep
their models in a national projection (Lambert-93, Lambert 72, L-EST97,
CS92, S-JTSK), and many of their coverage and image services reproject on
request. Asking those for a 1 km square of a UTM zone keeps sim-mesh to
the projections it already reads, at the cost of one resampling on the
server; a projection is added only for data no service reprojects. A
national system that is a UTM zone under its own code (RDN2008,
SWEREF99 TM, ETRS-TM35FIN) is that zone. Tiles in metres are found in
the source's own system: the rectangle's reach projected there
(`crs.plane`, LAEA Europe by Snyder's ellipsoidal formulas for BEV's 50 km
squares), the squares meeting it, and a cached square drawn back in
degrees along its edges, which a projection bends.

**Measured ground from GeoTIFF is sampled, not resampled.** Each cell takes
up to 8 × 8 samples, about a pixel apart, each transformed from the pack's
UTM to the source's projection and read nearest-pixel: terrain is their
mean, clutter the representative height of the surface above it. A cell
whose samples are three quarters missing keeps GLO-30's. The reader opens
the coarsest level whose pixel is no larger than a quarter of the cell, the
level a window fetched (below), so a sparse copy is only ever read where it
holds data. A source's tiles are each opened once for their grid; a worker
then opens a tile when a sample first lands on it and keeps at most eight
open, the last one used tried first: a source of 1 km tiles is a hundred
of them under a 10 km pack, past the 256 open files macOS allows a
process if every worker opened all.

**Clutter comes from one survey.** A regional terrain source with a surface
source in the same projection and with the same no-data value is a pair,
and a cell is measured only when both halves are: the terrain and the
clutter above it come from the same survey. A terrain with no surface
beside it (3DEP's) replaces only the ground and leaves each cell the
clutter it had, GLO-30's split or its buildings', since GLO-30's surface
less another survey's terrain is no height of anything: the two differ in
resolution and in vertical datum (EGM2008 against NAVD88, about a metre).
Such a cell's quality is what its clutter's is. A surface with no terrain
beside it is refused ("… has no terrain to pair with in its system").

**A raster is read in its own units.** A geographic source (WorldCover,
3DEP, WorldPop) counts in degrees, which proj4rs takes and gives in
radians: every point into or out of a source passes `system::transform`,
which speaks each system's own units. A pixel, a window's margin and the
level a window and the reader choose are said in the source's units too, a
degree taken as 111.32 km.

**Land cover is laid source over source.** Each land cover source's codes
go through its own table to clutter classes; the worldwide source is read
first and a regional one over it, a cell taking the class of the last
source with one there. A code a table does not name (NLCD's no-data 250)
is no class, and leaves the cell to the source before.

**Priority is the plan's; the compiler's order is the cell's.** A source's
priority in a layer decides what `sources.plan` says it is used for
("buildings outside Berlin LoD2 building models"), and nothing else: the
compiler is never given it. In the compiler, measured pairs overwrite
GLO-30's split on every cell they cover enough, the 1 m XYZ pairs first and
then the GeoTIFF pairs in the order given, so where two measured sources
cover one cell the later stands. LoD2 and CityJSON buildings displace
OpenStreetMap's on their tiles. An input that takes one source (population,
the surface tiles, the PBF) refuses a rectangle that two sources meet.

**A window is a sparse copy of the whole file.** `read: window` fetches a
cloud-optimised GeoTIFF's first 256 KB, walks its directories (TIFF and
BigTIFF, tiled only), and fetches the chunks of one level that the
rectangle meets, ranges closer than 16 KB merged, written at their own
offsets into a file of the remote's length. `<file>.ranges` lists what the
copy holds; a later rectangle fetches only what it lacks, and the copy
counts on disk as the blocks it holds. Any GeoTIFF reader opens it, and
reads zeros where it holds nothing.

**An outline is a source's own.** A regional source ships its coverage as a
GeoJSON file beside the source file, so no source's coverage depends on
another source's index being fetched; Berlin's and Germany's are Geofabrik's
outlines of them, made once with `sim source outline … --geofabrik`, the
United States' sources' Geofabrik's `us` (the conterminous states, for
NLCD, its four regions together).

**A pack carries only what may be passed on.** `redistributable: true` is a
claim about the licence, and an entry that makes it gives the `notice` a
pack carries; `sourcefile` refuses one without. A source that is not
redistributable (the ITU maps) stays in the cache, and only values taken
from it enter a pack. A pull request that adds a source is reviewed for its
licence before anything else.

Given both `lod2` and `osm_buildings`, the compiler takes
LoD2's buildings on the tiles of its sources that meet the grid (a tile's
extent read off its name, `LoD2_<zone>_<E>_<N>_<size>_…`, else the extent of
its buildings) and OpenStreetMap's everywhere else: an OpenStreetMap
building whose centroid lies on one of those tiles is left out, so no
building is counted twice.
The tile is the unit of LoD2's coverage, not the city boundary, so on a
tile that Berlin's border crosses the part outside Berlin has no buildings.
CityJSON's coverage is each file's extent, an OpenStreetMap building on it
left out the same way. All write to one `buildings.jsonl`, LoD2's and
CityJSON's lines first, each line's
`source` saying which, and the manifest carries both notices. A cell's
DataQuality code follows most of its built area: LoD2, else OSM tagged,
else OSM default.

**OpenStreetMap is one Geofabrik PBF extract, not Overpass.** One file per
region serves roads, places, peaks and masts, and buildings alike, and
Geofabrik's index gives every extract's outline, so the smallest one holding
the rectangle is chosen. Overpass would be four queries per region, is
rate-limited, and times out on a city's buildings. The selection is
`sources.smallest_extract`, the `regions` method's. The same holds for every
source: a source is files, fetched whole or by range into the cache, never a
query service (OGC API, WCS, Overpass), whose limits and timeouts make a
city's build unreliable.

**The build's map tiles come through the front.** The page asks
`/osm/<z>/<x>/<y>.png` and the front serves it from `testbed/osmtiles/`,
asking tile.openstreetmap.org only on a miss. One origin keeps one cache, as
the tile usage policy asks, whatever browsers have the page open. Place
search goes the same way (`/api/nominatim`), one request per search.

**Nodes are never ground.** A planner pack could carry a `Nodes` layer, the
deployed network baked in at build time. sim-mesh's compiler never writes one,
and export, import and what the page is told all leave one out: nodes are
nodesets, which stand on any ground that holds them, and which the Nodes
tab lists, shows, merges and edits. The public node maps come in as
nodesets through `nodes-import`, whose report counts every row into one
bucket (kept, or dropped and why).

**The DataQuality layer's codes are wire values; `rank()` orders them.**
0 GLO-30 synthesized, 1 LoD2, 2 lidar 1 m, 3 OSM default heights, 4 OSM
tagged heights, 5 lidar raster (a measured GeoTIFF pair). A cell takes the
best-ranked evidence it has: GLO-30 < OSM default < OSM tagged < LoD2 <
lidar, the two lidar codes ranking alike.

## A table of every pair

The medium reads every level from a table: every ordered pair of nodes,
per band, its path loss at one frequency in the band
([LOSSTABLE.md](LOSSTABLE.md)). Three choices make it that shape.

**Every pair, not the pairs that can carry a frame.** A receiver's verdict
on a frame sums the interference of everything else on the air at it, and a
transmitter far too weak to be decoded there still adds to that sum; ten of
them can spoil a frame that none of them alone would touch. A table cut at a
link budget would drop exactly those contributions, and the medium would
report as clean a frame a real receiver loses. So nothing is cut for being
weak. A radius (30 km) bounds the work, and a pair beyond it, or off the
ground data, is never heard, flagged so a reader can tell it was not
computed.

**A table, not a model in the medium.** Synthetic ground's log-distance loss
and a pack's P.1812 are written into the same format, so the ether has
one code path and no propagation model of its own, and a table computed
once serves every run of that nodeset on that geodata. It serves the
nodeset's next edit too: a pair's loss depends on its two ends and the
model alone, so a nodeset with a few nodes moved, added or taken away takes
every pair whose ends are unchanged from the nearest cached table
(`losses.nearest_cached`: the most nodes at the same name, position and
height, under the same ground and model) and computes the rest
(`update_nodes`). A run works on its own
copy: a node moved during a run has its row and column recomputed into the
copy, never into the cache, and the ether keeps the old row until the new
one is in.

**A full matrix, per direction.** The two directions of a pair are separate
cells, because a loss measured from a running network is one direction at a
time and an offset or a measurement may differ by direction. The computed
loss does not: a path loses the same both ways, and `link.json` runs P.1812
(or the near-field model) both ways and gives the pair the mean. Run one way
only, P.1812 is not reciprocal as used here: its location variability is
the receiver's alone, and the profile is decimated from the transmitter, so
the two directions of one Mitte pair came out 14 dB apart. For 200 nodes the
matrix is 40,000 cells, about 280 KB.

**Per band, with a correction within it.** A table is computed at one
frequency `f0`, and a frame on carrier `f` in the same band sees
`loss + 20·log10(f/f0)`. Only the free-space term of a loss scales that way;
diffraction, troposcatter, the location spread and the terminal clutter
correction all carry the frequency differently, so the correction is good
across a band's few megahertz and wrong across bands. Each band (433, 868,
915 MHz) has its own table, and 2.4 GHz has none: the SX1262 is the only chip
here, and the planner's clutter correction stops at 3 GHz.

## Reception at the receiver

A collision is not a property of a transmission; it is what happened at one
antenna. So the ether rules per receiver, per frame, from everything arriving
there: who is affected and who can decode, then noise in stages and
interference stretch by stretch. [`ether/INTERNALS.md`](ether/INTERNALS.md#reception-interference-the-worst-piece-deciding)
is the whole of it; this is its shape.

**The received power** of a transmission at a receiver is the transmit power
the frame states, plus each antenna's gain toward the other end, minus the
table's loss for that direction and the within-band correction; the gains
are on the table the medium is handed, as the offsets are.

**Who is affected, and who can decode, are two questions.** Every
transmission whose channel overlaps the receiver's counts towards its
interference, whatever its spreading factor (SF) or sync word, because a LoRa
demodulator hears every chirp in its band. Only a frame whose bandwidth,
spreading factor and sync word match the receiver's state, on its carrier,
can be decoded. An off-band transmission contributes nothing.

**Against noise, three stages.** One symbol error curve per spreading
factor, exact for an ideal receiver and offset so the datasheet's test frame
(64 bytes) is lost 1% of the time at the datasheet's threshold, decides by
seeded draws whether the receiver finds the preamble (the lock: failing it,
the frame is energy only), reads the header (failing it, the reception ends
at `t_hdr` as a header error) and decodes every block of the payload
(failing it, a CRC failure). Longer frames have more blocks, so fail more
often at the same level.

**Against interference, the worst stretch deciding.** The frame's air is cut
wherever the set of overlapping transmissions changes, and in every piece the
signal over each **class** of interference is at or above that class's
rejection figure, where a class is every overlapping transmission at one
spreading factor, summed in milliwatts before the test.

Noise is not added to the interference: the stages are against noise, the
same-SF figure is against a chirp, and adding noise to a chirp's power would
make neither mean what its source measured. Nor are the classes summed into
each other, because each figure was measured against one interfering
spreading factor.

**Fading**, when asked for (`--fading-db`, `--coherence-s`), moves every
link's level over time around the table's, per pair of nodes, and every
stage and every stretch reads the level at its own instant; `--rician-k`
adds a fast fade drawn per frame at each receiver. Every draw — stages,
fades, bench outcomes — is keyed on the channel (the frame's sender, start
and bytes; the receiver's name), not on the order of events, so arms of a
comparison share their channel.

**What can decode** is the receiver's main detector — bandwidth, spreading
factor, sync word and IQ polarity — or, on a radio that has them, any of its
side detectors (an LR2021's, each its own spreading factor, sync word and
IQ polarity), which also miss short preambles at a measured rate.

**The figures**, all in one table at the top of `ether/ether.py` with their
sources ([`ether/INTERNALS.md`](ether/INTERNALS.md#the-figures)):

| Figure | Value | Source |
|---|---|---|
| thermal noise | −174 dBm/Hz, plus the noise figure (6 dB) | kTB at 290 K; the SX1262's order of magnitude |
| demodulation threshold | −7.5 dB at SF7, 2.5 dB lower per step | SX1261/2 datasheet |
| the error curve's anchor | 1% of a 64-byte frame lost at the threshold | SX1261/2 datasheet, the conditions of its sensitivity table |
| same-SF rejection | 6 dB | Semtech's specification |
| inter-SF rejection | −8 … −25 dB | Croce et al., "Impact of LoRa Imperfect Orthogonality", IEEE Communications Letters 22(4), 2018, measured on the SX1272 |
| sense threshold | 15 dB over the ETSI (European Telecommunications Standards Institute) EN 300 220-1 sensitivity limit, −81 dBm at 125 kHz | EN 300 220-1 V3.1.1, 5.21.2 |

Carrier sense and CAD (channel activity detection) answer from the same air:
busy when a decodable frame, or summed in-band energy over the sense
threshold, is there.

**The pairwise rule** (`--pairwise`) rules on the same levels the simpler
way, one interferer at a time against a 6 dB margin with nothing summed, so a
run can be compared frame for frame with one ruled that way.

**Bench capture** (`--bench-capture`) replaces the same-SF figure with what
a bench measured of two frames meeting: equals within 1.2 dB (both lost about
one time in four, otherwise one survives), the stronger surviving seven times
in eight from there and always from 6.1 dB, and a frame arriving after the
receiver has passed the first one's preamble never taking it
([`ether/INTERNALS.md`](ether/INTERNALS.md#bench-capture)).

**No interference** (`--no-interference`) is an oracle, not a model: every
frame is judged against noise alone and no receiver is taken off the frame it
follows, while a receiver still follows one frame at a time, cannot hear while
it sends, and senses the channel as before. A run's delivery with it, less its
delivery without, is what overlapping frames cost that run
([`docs/AIRTIME_2026-09-30.md`](docs/AIRTIME_2026-09-30.md)).

## Why the ether owns the lock

A receiver follows one frame at a time, and which one is decided at the
preamble: a decodable frame takes the receiver when it is not demodulating
another, or when it leads the one in progress by the same-SF figure. That
decision is the ether's, and the chip model has no lock rule of its own.

The lock needs what only the ether sees: everything arriving at the antenna,
summed. A chip told of frames one by one could compare each new one only to
the one it has, and would lock differently from the verdict the ether then
gives it — the medium saying one frame survived while the chip had already
let go of it. So the chip follows whichever frame the ether last began on
it; a frame the receiver does not lock on to is sent as energy (an
`rx_begin` marked `"cad": true`), which raises the level the chip's
instantaneous RSSI (received signal strength indication) and its CAD read
and is never demodulated. [`ether/INTERNALS.md`](ether/INTERNALS.md#why-the-ether-owns-the-lock)
has when the lock is taken and released.

## Reset, factory reset, and why neither is called "apply"

Two verbs, and the difference is exactly the difference a person expects:

- **Reset** presses reset. The process exits, the supervisor brings it back,
  and the state store is untouched.
- **Factory reset** stops the station, deletes its `state/`, and starts it
  again. On the way back up the directory is empty, which is precisely what a
  node clicked onto the map for the first time is — so setup runs again by
  the ordinary path and not by a special case.

There is deliberately no verb that re-runs setup on a running station. "Re-apply" reads like a repair and behaves like one sometimes and not
others: a line that is a setting takes effect, a line that is an action happens
twice. What replaces it is **Run command**, which types one line at the
stations chosen and shows what each said, and a script's verbs, which mean
one thing to every firmware of a category, each driver doing them its own
way. That is more versatile — retune
the whole testbed, survey it, create something on all of it — and it is
honest about being a thing you did rather than a state you restored. A
node's tags changed on a running simulation are no exception: what they
mean to the first-boot rules takes effect at its next first boot, a
factory reset.

## Why the store is flushed before anything is taken away

A firmware's store may hold writes in RAM before it commits them — for a
minute, say. On a board that is a power-cut window and entirely fair. Here
it would make two things lie. A firmware whose store writes through has
nothing to flush, and its driver's `flush` does nothing.

A station **reset moments after its setup ran** would come back with none
of it, and the testbed would be claiming a configuration it never made
durable. So simd has the driver flush at the end of setup, and again a few
seconds later — not everything setup asks for lands at once, and an LXMF
(Lightweight Extensible Message Format) identity may reach the store some
seconds after the verb that created it has returned.

A **snapshot** copied out of a store with a minute of writes still in RAM would
be a picture of a moment that never quite existed. So every running station is
flushed before the copy, and before any stop or reset.

All of it is best effort with a short timeout. A station that will not answer
is one whose store cannot be flushed, and refusing to stop it over that would
be worse than losing the last minute.

The flush is simd's to ask for rather than the script's because it is not a
setting. Everything a script says describes what a station *is*; a flush is
about making that description stick.

## First boot: the name, then the first-boot rules in order

```
simd ── the driver's wait_up: booted, every service up ──────────────► station
simd ── its name (the name verb) ───────────────────────────────────────► station
simd ── what the first-boot rules give it, in the rules' order ────────► station
        startup.py's: role(…), radio(…), each nodeset's own lines, radio_up()
        then the script's own lines and verbs
simd ── the driver's flush, and again once what setup asked for has landed ► station
```

A station is set up by its driver, verb by verb, and by lines typed as
written. The alternative — a schema of settings the testbed knows the names
of — would have to grow every time a firmware grew one, and would be a
second place for a setting's name to live. So nothing is declared in the
nodeset: every setting is a script's first-boot rule, a line typed as
written or a **verb** (`Node.radio(…)`, `Node.radio_up()`,
`Node.reticulum.role(…)`, `Node.reticulum.lxmf.create()`), which each
driver does its own way and a station of another category than the verb's
skips, so one rule serves every firmware. What the page must know without a station running is read from
the same places the rules are written from: the tags the rules select on,
and `scripts/globals.py`, whose radio the startup script sets. Per-node
differences are selections (`nodes(tag=...)`) and macros, never code, which
is what lets the rules travel to simd as data.

**Every world starts from one script.** `scripts/startup.py` says the
roles, the radio from `globals.py`, then includes each nodeset's own
`nodesets/<name>.py` (`for nodeset in sim_nodesets(): script_include(...)`), then
starts the radios. A script includes it among its declarations, so what a
traffic study runs on any world is readable in three files the editor opens
together, and a world's peculiarities (a TCP gateway, a sync word) live
with the world, not in the study.

**Up means booted, not answering.** A firmware may answer its console from
early in boot, before its services have initialised, and a setting made then
can be undone by that init: a radio's frequency, set at the first instant,
gone by the time the radio started. So a driver's `wait_up` counts a station
up only once the firmware shows that every service is up, whatever sign it
gives of that.

**The radio is started last.** A radio may read its settings when it starts,
so a setting made after the radio is up waits for the next start. That is
why starting it is a verb of its own (`radio_up`, nothing for a firmware
whose radio needs no start), and why the startup script says it after each
nodeset's own setup. A script's own first-boot lines come after the include,
so what the radio reads belongs in a nodeset's setup or before the include.

**A role the firmware forgets is said at every boot.** Setup runs once, on
a station with no state; a firmware that keeps its role in RAM only would
come back a client after a reset while its node is tagged transport. A
driver whose firmware does not keep its role across a restart says so
(`role_volatile`), and simd says the role verbs of its first-boot rules
again whenever such a station comes up with state.

Lines are expanded per station: `{name}`, `{id}` and `{addr}`,
`{addr:<node>}` for another node's address, and `{max_dbm}`, the node's
maximum power. That is what lets one shared
line say node-specific things, and it is why simd adds no settings of its
own. An address is always a macro and never written down, because it follows
from the id and from the network the front gave that simulation, which
differs between two simulations of one nodeset.

The name is the node's identity in three places at once —
the map label, the proxy hostname and the station's own name — and must not
drift; simd gives the name itself, before any rule, with the `name` verb,
so it cannot disagree per node.

A macro the list does not define is left exactly as written. A line is
somebody's text and may legitimately contain braces, and silently emptying
something that only looked like a macro is worse than passing it through for
the station to complain about.

Setup runs only on an empty store, which is what makes it safe for it to
make an LXMF identity — something emphatically not idempotent. Nothing
re-runs it against a configured station; a factory reset empties the store
first.

Whether a station has been set up is sampled **at the fork**, not when it
answers: a firmware marks its state as set up moments after it starts, and
the answer the setup step needs is the one from before it ran.

## The WebRTC relay, and why it is a relay rather than a second transport

```
browser ──ws  alpha.lora.sim.localhost:8800/webrtc──► front ──ws──► simd ──ws──► alpha   signalling
browser ──udp localhost:8800─────────────────────────► front ──udp─► simd ──udp─► alpha   the channel
```

A station's web UI reads every `s.*` value over a `storage:1` WebRTC
DataChannel. There is no HTTP path for it, so without a DataChannel the page
loads and then knows nothing: no hostname, no settings, no Activity.

A host-only WebSocket transport carrying the same merge-patches would be the
obvious alternative, and the wrong one. A transport that exists only in the
testbed is a code path a board never runs, so the thing being tested stops
being the thing that ships — which is the one promise this testbed makes.

So the station speaks real WebRTC here, from the same source, and what is
sim-only is the plumbing that makes it reachable:

- **Signalling.** simd keeps `/webrtc` for itself — the one station route the
  front listener does not forward — and terminates the WebSocket on both
  sides, so the SDP (session description protocol) answer arrives as a
  parsed message. It rewrites the connection line and candidate to the
  relay's own address and drops the station's, which point at an address
  the browser cannot reach and would only cost it timeouts. The browser's
  cookie goes up with it, because signalling is behind the station's own
  login and a relay that dropped it would be introducing a stranger.
- **Media.** One UDP port in front of every station. The first packet of an
  ICE (interactive connectivity establishment) session is a STUN (session
  traversal utilities for NAT) binding request whose USERNAME begins with
  the answerer's ufrag — the same ufrag simd read out of that station's
  answer — so the packet says which station it belongs to without simd
  having to understand anything else about it. After that the browser's
  address is pinned to a flow with its own socket, and both directions are
  forwarded bytes-for-bytes.

Nothing is decrypted or inspected past the STUN username: DTLS (datagram
transport layer security) and SCTP (stream control transmission protocol)
are end-to-end between the browser and the station exactly as on a board.

Behind the front the relay is relayed, by the same code one level up. The
front keeps `/webrtc` for itself as simd does and opens the child's with
the browser's `Host`, so the child knows which station is meant. The child
points the answer at its own relay, the front points it again at the
published port and ties the answer's ufrag to the child's relay; the first
STUN packet then finds its flow at each relay by the same ufrag. The child
sees the front as a browser, and the station sees the child's relay as it
always does. `webrtc.bridge` is the signalling half for both, and `Relay` the
media half.

What this cost the firmware is three functions —
`webrtc_port.{h,cpp}`: the local addresses to advertise, the address the socket
binds, and a CRC32 (the 32-bit cyclic redundancy check) the chip has in ROM. The rest of ICE, DTLS and SCTP built
for the host unchanged. The bind is the one that matters and is easy to miss:
a chip has a network stack to itself and binds the wildcard, while here every
station is a process on one stack, so each binds its own loopback address or
the second one to start finds the port taken.

## Status, and what `up` means

`stopped` → `starting` → `setup` → `up`, with `restarting` for the gap after an
exit nobody asked for. `up` is **the station booted and answering the door
its driver talks through**, as the driver's `wait_up` says — not the process
existing: a firmware process that has forked but not finished booting is not
a station you can do anything with, and the map should not claim otherwise.

A station's **role** — `transport`, `router`, `repeater` or `client` — is not
status. Its node's role tag names one, which the startup script says to it
and the page draws before anything runs; once it runs, simd asks each
station every few seconds, through its driver (`current_role`), because the
setting is live and a person can flip it on the station itself — the map
should show what the station thinks, not what it was told at its first
boot. A driver that cannot ask leaves the tag's on show. Roles are the
category's words for what a station does, so the map draws a forwarding
ring for any firmware without knowing its protocol.

## Drivers, and the rules that come with more than one firmware

Everything the testbed knows about one firmware lives in its driver, which
comes in the firmware's zip; simd, the supervisor and the page know only the
driver's methods. The firmware contract is what every firmware shares, and
it is small on purpose: an identity, a directory, an address, the ether and
the radio, in `SIM_MESH_*`, a console on stdin/stdout, and the driver
interface of its category.

**A driver imports two modules of sim-mesh's and nothing else.**
`sim_mesh.driver` (the base class, the station surface it is handed, framed
RPC, `chip_dbm`, `run_tool`) and its category's (`sim_mesh.reticulum.driver`)
are the contract; everything else in sim-mesh may change, and a driver that
reached past them would break with it. A driver is imported from its
firmware's own directory under a module name of its own, so two firmwares'
drivers never meet.

**Verbs return what they mean, not what a station printed.** `path`
returns a hop count and `lxmf.identities` names and addresses, so the
traffic driver and the delivery analysis read every firmware the same way,
and no parser for any firmware's output lives in sim-mesh. What happens
later — a message delivered — the driver hears on the console
(`console_line`) and reports as an event at the run's T (`station.report`),
which simd writes to the run's `events.jsonl`.

**A message's id is sim-mesh's, given before the send.** simd names every
`lxmf.send` (`<sender>.<T in µs>`) and the driver reports its status under
that name. A firmware's own id cannot be the one: many give a message one
only once it is built, and a message held while a path is asked for, then
dropped, never gets one, yet what became of it is exactly what a run wants
to know. So a driver couples the firmware's id to sim-mesh's when it learns
it, holds what the station said under an id not coupled yet until it is, and
reports what goes wrong before there is an id under sim-mesh's directly
(`sim_mesh.reticulum.driver`).

**Creating an identity is never refused.** `lxmf.create` with a name the
node already has does nothing and answers its address, and a firmware with
one identity that already has another name does nothing at all, so a script
says it at every first boot without knowing what the node holds. The one
refusal is simd's: a name another node or another node's identity has, since
the name is how a sender and a recipient are found.

**One chip model for every firmware, below the driver.** Every firmware
links the same virtual radio, whatever its language. A rewrite per language
would drift on exactly the details the testbed exists to hold constant, and
a seam above the driver, at a `LoRaRadio`-style level, would skip BUSY, CAD,
the sync-word register write and a CAD cutting off a reception, which are
what break on boards.

**Protocol parts sit behind their protocol.** The ether, the record, the
map, airtime per carrier and link geometry know no protocol; LXMF traffic,
delivery analysis and SUPE frame classes live under `sim_mesh.reticulum`, so a
firmware of another protocol gets everything generic and nothing that
misreads it.

**A driver supplies what its firmware reads.** A firmware that reads other
names for the contract's values gets them from its driver's `env`, beside the
contract's own; the contract does not grow to fit one firmware.

**A line is in one dialect; a verb is in every one.** A line goes only to
stations of one base: **Run command** and a script's `run` refuse a choice
of stations that spans bases unless `base` narrows it. The same text typed
at another firmware means something else or nothing, and a testbed that sent
it anyway would be reporting an answer to a question it never asked. A verb
(`lxmf.announce`, `lxmf.send`, `role`, …) is what is meant rather than what is
typed, so it goes to every station and each driver does it its own way; a
firmware that cannot answers that it has none, for its stations alone.

**Whether a station is set up is the driver's to say, and it is sampled at
the fork.** Each firmware leaves its own mark in `state/` on a first boot,
moments after it starts; the answer the setup step needs is the one from
before it ran. Get it wrong one way and setup runs on every restart, the
other way and it never runs.

**One conversation at a time on a station's door.** A door that two callers
can interleave on — a tool that opens a pty per command, frames whose ids
could collide — is held by its driver with a lock per station around every
call, its own polls included.

**A category's rule is inert elsewhere; a category's command that reaches no
node of it is refused.** A rule and a verb carry their category (`{verb,
args, category}`), and simd hands them only to stations of that category. A
rule is a standing condition over a network that may be mixed:
`Node.reticulum.role("transport")` on a selection that also holds another
firmware's nodes means its Reticulum nodes, and is nothing to the rest, now
or when a node is placed later. A command is given once, now:
`<selection>.reticulum.…` on a selection with no Reticulum node would do
nothing anywhere and answer as if it had, which is a script naming the
wrong nodes, so the library refuses it before it is sent, and simd refuses a
meta that leaves no station of its category.

## Framed RPC, and why a console and not a TCP command line

A firmware that speaks framed RPC is asked things on its console pty, the
channel a flasher uses on a board's USB console. A command line over TCP
would work, but on a board it is closed until someone opens it, and a
testbed that needed it open would need the firmware to behave differently
here; the console is there from the first instant on both.

The pty drain is a state machine rather than a search, because a read can
end anywhere: in the middle of the magic, of a header or of a payload. Bytes
that might be the start of a frame are held until the next read settles them;
a false start gives them back to the text, first byte first, and reads the
rest again, because a magic can start inside a false one. A frame that is not
one — an id outside the range a query uses, a magic inside a payload, or a
remainder that has not arrived within a second — is given back the same way.
The station writes a whole reply under the console's write lock, so none of
that happens in a healthy run; it is there so that a line of noise costs one
answer and not the stream.

The station bounds a command at five seconds and cuts a reply that outgrows its
buffer at the last complete line, without saying so. A reply is therefore not
proof that a command finished: a line that came back at the bound may still
be running, and the next frame would find the command line busy. So a slow
line is followed by a question that confirms it landed, and a caller asks for
one key at a time rather than a subtree whose tail could be cut.

**A query is retried only after a whole timeout with no answer at all**, the
five-second bound plus margin (eight seconds), and at most twice. A slow
command that must not run twice, such as one that makes an identity, is then
never run again by a retry.

**A driver takes T from a `command_result`, not from `clock`.** `clock`
comes once per wall second, so at a high pace it is tens of seconds of T
stale, and a driver that scheduled from it would send every message that
late.

**Ids are unique across firmwares.** The ether keys stations by id; two processes
answering under one id are one station to the medium, and two sockets on one
address. A nodeset refuses a file that repeats one, and its editor an id
another node has.

## The page

Pinia stores hold the page's state, split as the data is: `catalog` (what
the store holds — firmware, geodata, nodesets, scripts, snapshots — and the
script runs with their output), `geodata` (which ground is on show, and for
a pack its manifest and the sidecar's base path), `display` (how the map is
shown, per tab), `nodes` (the Nodes tab's list of nodesets, the ones
checked and, as their files stand, loaded for its map; the nodeset open,
its selection and its dirty state), `coverage` (the nodes' rasters) and `sim` (the running simulations
and the attached one's live state); one socket store owns the websocket
they all speak through, and `lib/front` matches each editor verb's answer to
its request. Every component reads the stores; every action is one store
method that sends one message. A reconnect replays the `snapshot`, so the
page holds no state simd cannot restate — which is the whole of what makes
simd restartable under a page that is open. The nodeset open on the Nodes
tab is the one exception, deliberately: it is the page's own until it is
saved, and leaving it asks first about unsaved edits. Save selection as
sends the checked nodesets' nodes on the geodata, as the page loaded them,
to the front to be merged there (`nodeset.merge`), so what is written is
what the map showed, nodes off the geodata left out. A merged node keeps
its tags and gains its nodeset's name as a tag, so a script can still tell
them apart after the merge.

**A reply the sidecar cut short is never drawn as if it were whole.** The
sidecar caps a footprint reply's vertices and fills it in the pack's order,
not the requested box's, so a capped reply covers some of the box, in no
useful shape. Footprints are asked for in fixed squares of the ground, each
kept once fetched, and a square whose reply says it was cut is asked for
again as four. Coverage rasters are held the same way, per node and per
position: an answer names the node and the place it was asked for, answers
add to what is held rather than replace it, and one that lands late, or
for a node since moved, is never read as the node's; and the map's repaint
key includes which raster each node was drawn from, so a raster arriving is
a repaint.

**One map, three uses.** The map page (`NodesPage`) is one component, and
its map one `GroundMap` whatever it is doing, so changing between them never
fetches the ground again. On the Nodes tab's list it only shows the checked
nodesets, hollow in their colours, with nothing to select, move or cover;
with a nodeset open it edits it; opened from a
running simulation's row it stands on the Simulations tab in place of the
list and shows that run's stations live, and the same edits (move, set,
tag, offset, remove) go to the run's own copy as messages to its simd. The
map, the editor and the tags panel read one list of nodes (`nodes.list`) and
edit through one set of actions, so none of them knows which mode it is in.
Going to the Nodes tab closes an open simulation, so that tab only ever
shows nodesets.

**Coverage is combined once per set of nodes.** On a pack each node's raster
is merged into one grid of the best margin for the nodes on show, kept for
the last few sets (the whole network, and each selection lately shown); a
raster that lands later is merged into the grids that want it rather than
rebuilding them, and a repaint is one lookup a cell whatever the count.

The same page is served by the front and by a simd on its own, and tells them
apart by the front's `hello`. Behind the front the store keeps the registry
and the attached simulation beside the one simulation's state it always kept;
a reconnect re-sends the selection, which the front forgot with the socket,
and the child's `snapshot` restates the rest. A message whose `sim` is not the
attached one is dropped: it is from a socket the front was still closing when
the selection changed. Served by a simd on its own there is only the Nodes
tab, attached to it.

The map is a **canvas**. Ground tiles, thousands of building outlines,
hundreds of pulses a minute and a drag at 60 Hz are all much cheaper drawn
than laid out, and none of them wants to be an element. It is drawn in the
geodata's own metres — a pack's UTM (Universal Transverse Mercator) zone, the
same transverse Mercator the nodes' positions are projected with for the
planner, or synthetic ground's nautical mile to the minute from 0°, 0°, as
`geodata.py` projects it — so a pixel and a metre agree by construction
rather than by two implementations staying in step.

**Coverage is a margin, worked out on the page.** The layer is the best
decoding margin at each point over the nodes shown: each node's transmit
power and its antenna's gain toward a receiver 2 m over the ground there,
less its path loss there, less its own threshold (its SF and
bandwidth over the noise floor). On a pack the loss is the node's raster
from the front; on synthetic ground it is the log-distance formula. It is
painted once per settled view into a surface of its own, a cell every few
pixels, so panning costs nothing, and asked for only while the Nodes tab is
on show with the layer on. It is drawn in bands of what the margin is good
for rather than a ramp (`COVERAGE_BANDS`: indoors too from 21 dB, outdoors
only from 6, the edge from 0), because the question a map is asked is
whether a place is served, and a ramp makes the eye judge dB.

**Under a heatmap only what is picked keeps its colour.** Population and
coverage are one at a time, and while one is on the ground is drawn grey:
the base, roads and buildings from grey copies of their images (made once
per paint, since `ctx.filter` is not in every browser), offsets and rings
through one colour function that greys them. A red road or a yellow ring
over a coverage map would read as coverage. The nodes and the selected
node's links keep their colours: they are what the map is being looked at
for, and a link's colour is a verdict of its own.

A drag on a simulation's map sends `nodeset_move` at a few Hz with
`settle: false`, and once more on release with `settle: true`. Only the
settled move is written into the run's nodeset, logged, and has its row of the
loss table recomputed, so a drag is one edit and one row rather than fifty;
the station is drawn stale until its row is in.

Frames arrive as `tx` and `rx` and are drawn on the **browser's** clock: the
ether's microseconds are its own, and the only thing in a `tx` that means
anything here is how long the frame occupies the air. In a virtual-time run
that span is divided by the run's pace — the pace asked for, or for `max` the
pace the last `clock` message observed — so a ring lasts the frame's time on
the air as the run experiences it.

## A station's console is a pty

A station's stdin and stdout **are** its serial console, so the supervisor
holds the master end and the console window is that pty over a websocket.
Keystrokes go as binary frames and the terminal size as a JSON text frame, so
no byte a person can type is special to the transport.

## The seam: a bus, not a chip class

The model implements the **wire**, not the driver's idea of a radio. A
transmission arrives at it as a byte frame with an opcode, exactly as the
driver would put it on a bus, and the reply comes back as the status byte and
the data the datasheet describes. That placement is what gives the testbed its
value: the driver's own command sequences, its IRQ masks, its read-modify-write
of the sensitivity register and its interrupt handling all execute, unchanged
and unaware.

The model is deliberately shallow where depth would buy nothing: mode
transitions are instantaneous, BUSY is never busy, and the GFSK and LR-FHSS
modems and duty-cycled receive are refused. A `ready_at` field rides on
the wire from the start so the datasheet's timing table can be added later
without moving anything else.

Three things it is **not** shallow about, because everything above the bus
reads them:

- **A frame takes its time on the air.** The transmit timeline is the
  AN1200.13 time-on-air for the modem as configured, so a 250-byte frame at
  SF8/BW125 occupies the medium for two thirds of a second, carrier sense has
  something to sense, and two stations can be talking at once. A model that
  finished a transmission the instant it started would make every collision
  in the testbed impossible, and it would do it silently.
- **A receiver follows one frame at a time.** The chip follows the frame
  the medium last began on it, and takes an `rx_end` only for that one. The
  medium decides which that is, because only it sees everything arriving at
  the antenna summed: a frame the receiver does not lock on to comes as an
  `rx_begin` marked `"cad": true`, which raises the air's level for RSSI and
  CAD and is not demodulated. Without one frame at a time the driver would be
  handed whichever frame ended last, and the medium's verdict — which says
  one of the two survived — would mean nothing above the bus.
- **Channel activity detection answers.** `SetCad` runs for the symbols
  `SetCadParams` named, then raises `CAD_DONE`, with `CAD_DETECTED` when a
  frame this antenna has been told of is still on the air. A driver whose
  carrier sense is CAD waits for that answer and treats silence as a busy
  channel, so a model that accepted `SetCad` and never answered would make
  every transmission of such a driver fail seconds late, and it would look
  like a dead radio.

## The chip library, and the rules it keeps

The model and its ether link are one C++ library behind a C ABI (application
binary interface, `radio/include/simradio.h`), reaching the host only through a table of
services (`radio/src/services.h`). A station of any language links it. Each
rule below is a way the model breaks when a backend or a caller gets it wrong.

**The lock is recursive.** A timer callback takes the lock, and what it calls
can take it again; a plain mutex deadlocks on the first received frame.

**Pin callbacks and timer callbacks run with the lock released.** A host's
DIO1 callback may run a driver's interrupt handler on the spot, and that
handler issues SPI commands, each of which takes the lock. So the model
decides the line's level under the lock and calls the host after letting go,
and a backend's timer thread holds nothing when it calls in.

**Starting a timer that is running restarts it.** The receive timers are
re-armed for every frame; a start that was refused because the timer was
already armed would fire on the previous frame's schedule.

**The air is the antenna's, not the mode's.** What a CAD detects is any frame
this antenna was told of that has not yet left the air, whatever the chip did
in between: a driver goes RX, then standby, then CAD, and the frame it was
hearing is still there when the CAD looks. What the demodulator and the
instantaneous RSSI read is cleared on leaving RX, as a chip clears it.

**The medium tells a station in CAD about a frame, never how it ended.** A
CAD needs to learn of frames that start inside its window, or carrier sense
is blind exactly when two stations contend; a CAD demodulates nothing, so an
`rx_end` for it would be a reception that never happened.

**Close detaches, it does not free.** A slot's chip lives for the process,
because a timer may be about to fire on it; `simradio_close` stops its timers
and drops the host's callback, and opening the slot again powers it up fresh.

## Time

A run keeps **real time** or **virtual time** (`simd --time real|max|<k>x`),
for every station alike.

A virtual-time run is nearly serial: T moves only when every station is idle,
so more cores buy more runs side by side (other seeds, other scripts), not a
faster run. A real-time run never shares a machine with a build, because the
tick drift a loaded host adds makes it unreproducible for reasons that have
nothing to do with the protocol.

In real time `esp_timer` is `CLOCK_MONOTONIC` in microseconds from the first
reading, and every timed event in the model — the instant a preamble is found,
a sync word ends, a header lands, a frame finishes — is a one-shot on the
backend's timer. The
FreeRTOS tick is 100 Hz while a task runs and stops while every task is
blocked, so nothing is accurate below ten milliseconds; the
frames the driver sends take tens to hundreds of milliseconds, which is why
that is survivable. A station's `t` fields are its own clock, meaningful only
against each other inside one message, and the ether rebases every frame onto
its own clock before scheduling.

In virtual time the ether is the **conductor**: it owns conductor time T and
moves it only when every station has said it is idle
([`ether/INTERNALS.md`](ether/INTERNALS.md#the-barrier)). A run is then
limited by the work the stations do, not by the air: a quiet stretch of an
hour costs what the stations' timers cost to run, and a busy host slows the
run down instead of changing what happens in it. `max` goes as fast as that
allows; `<k>x` paces T at k times the wall clock, so a person can watch.

```
ether        welcome {t, mode: virtual, …}   the station's clock starts at T
station      runs until every thread is blocked, then  idle {seq, until}
ether        every station idle: T → min(until, the air's next instant)
ether        run {t} / rx_begin {t} / rx_end {t}      to each station due
station      conductor moves T, runs its timers and wakes due at T, owes an idle
```

**The station's side is `radio/src/conductor.cpp`**, in the virtual radio, so
every firmware has it by linking the radio. It keeps the last T granted, runs the
model's timers and the host's **wakes** when a grant reaches them, works out
the next instant the station needs (`until`) and sends the idle. The model
reads T; the host reads **node time**, f(T), which is where a node's own
crystal — drift, an offset — goes. f is the identity unless the station's
environment has `SIM_MESH_CLOCK_PROFILE`, a piecewise-linear map given as
`T:node` pairs in microseconds, both increasing, slope 1 outside them (the
[firmware contract's](README.md#3-what-a-station-is-given) environment); `nodeOf` / `conductorOf` are the only place
it is defined. `simd --clock-ppm P` gives every station one: a
straight line from T 0 whose slope is off by a draw uniform within ±P parts
per million, hashed from the seed and the node's name, so each station keeps
its own time and keeps it again in a run with the same seed. A crystal is
typically within ±20 ppm, 72 ms an hour. The radio's timers stay on T.

**The C library's time is answered by a preloaded shim**,
`radio/build/libsimclock.so` (built from `radio/shim/simclock.c`), which every
station of a virtual run is started with (`LD_PRELOAD`, `SIM_MESH_TIME=virtual`,
`SIM_MESH_EPOCH_US`). The chip
library finds it by name when the station opens its link and hands it the
clock (`include/simclock.h`); from then on `clock_gettime`, `gettimeofday`
and `time` read node time (plus the run's epoch for the wall clocks), and
every sleep, `setitimer`, `poll`/`select`/`epoll_wait` timeout and
`pthread_cond_timedwait` ends when node time reaches it. A waiting thread
blocks on an eventfd of its own, which a wake writes, so a signal still ends
its wait exactly as it ends a real one. Two rules keep it honest:

- **Every wait's end is rounded up to a whole millisecond of node time.** A
  driver that spins on microsecond `nanosleep`s makes each one a barrier;
  rounded, they share one, and the ends of many threads' waits on many
  stations fall on the same instants.
- **Every C library function the shim wraps is resolved in its constructor**,
  before `main()`. A thread switched out by a signal inside a lazy `dlsym`
  holds the dynamic linker's lock, and the next thread to resolve a symbol
  waits on it for good.

**The shim is also the station's randomness** when the environment carries
`SIM_MESH_SEED` (simd sets it from the ether's seed): `getentropy`,
`getrandom` and `syscall(SYS_getrandom)` — what a firmware's random number
generator and its crypto library's entropy come down to — draw from a
splitmix64 counter keyed
by the seed and `SIM_MESH_NODE_ID`. It needs no welcome, so it holds from the
first draw. A call reserves all its words in one atomic step and takes no
lock, so a thread switched out mid-call neither blocks another nor changes
its bytes. With the same seed and epoch, a station draws the same bytes in the
same order in every run, and what is left to differ between two runs is what
comes from outside them (below). The chip model's random-number register
(`RandomNumberGen`, 0x0819 to 0x081C) answers from the same `getrandom` while
the chip receives, as the chip samples its own receiver's noise, and holds
still outside receive: a firmware that seeds its generator from the radio
(RadioLib's `random()`, which MeshCore seeds its retransmit jitter with) gets
a seed of its own on every station. A register that never moved gave every
station one seed, and repeaters hearing one flood retransmitted it at the
same instant.

**Idle is the station saying every thread is blocked**, and a station has
one of two ways to know it:

- a host with a scheduler of its own (FreeRTOS on ESP-IDF's Linux target)
  says so itself, `simradio_idle()` from its idle, which runs only when every
  task is blocked: its wakes (`simradio_wake_at`) are the tick its first task
  is due at and its timers' next expiry, so `until` is the earliest of them.
  Its tick is best not a timer but a count stepped to node time every time T
  moves (`simradio_on_advance`), before anything due at the new T runs: a
  station whose tasks sleep for a second then wakes the run once in that
  second, not a hundred times. It opens its link before anything in it can
  wait on time, since only the ether says what T is;
- a station whose threads are plain pthreads: the shim's thread census
  (`SIM_MESH_IDLE=threads`, set by its driver). A thread counts
  as blocked while it is in one of the shim's waits, an untimed
  `pthread_cond_wait`, or a read on a blocking descriptor that is not a file;
  when the last one blocks, the station is idle. A read on a regular file
  does not count, however long it takes: nothing outside the process ends
  it, and counting it let a station reading its stored tables at boot look
  idle mid-work, with no wake held, and T run seconds ahead of it. A thread
  counts as running again the moment it is woken, not when it next gets the
  CPU: its wake firing, or a `pthread_cond_signal`/`pthread_cond_broadcast`
  on the condition it waits on. Otherwise a thread that signals another and
  then blocks itself would leave every thread counted blocked while the one
  it woke has work to do. A timed wait's deadline is broadcast holding the
  waiter's mutex, tried for 10 ms of wall time, since the waiter has joined
  the census a moment before its wait gives the mutex up, and a broadcast in
  between would be lost.

Neither can be told apart from a thread that is waiting where nothing can
see it, so the conductor also has a **busy watchdog**: a station that has not
said idle 20 ms of wall time after it was last told anything, or last sent
the ether anything, says so anyway, with the `until` it has — for a station
whose `until` is the next tick while a task runs, a tick at a time. That is
what keeps a thread spinning until T moves, or a host
descriptor nobody is watching, from stopping T. It must not fire on honest
work — key generation at first boot, a PBKDF2 of a community passphrase, a
signature check under load — or T would move on while the station is still
computing, by however many ticks the host's speed makes the work span. So the
shim holds the watchdog's timer back while another of the station's threads
is on the CPU: it reads the timerfd for the watchdog, and while a thread is
running and the process is spending user time or reading and writing, it
sets the timer again and waits on. A spin — a task yielding in a loop, which
on this host is signal-mask calls in the kernel and nothing read or written —
is let through after 50 ms of looking at it, and anything after 10 s. Work
then takes no T, the same in every run; the `clock` message counts idles that
took the watchdog's time as `slow_idles`, and simd logs the stations
concerned when the run's pace drops below 2x.

**Strict time** (`SIM_MESH_STRICT_TIME=1`) takes the wall clock out of every
one of those answers, so that how fast this host is never decides where T
goes. A station busy past the watchdog for a while (a proof of work, an LXMF
stamp) had T moved under it by however long the host took: in a mixed city
run one stamp took 0 ms of T in one run and 10 ms in the other, and the runs
parted from that frame on. Under strict time:

- **The watchdog answers only once none of the station's threads is
  running** (no task of it in `/proc`'s state R), looking again every grace
  and saying so every 10 s of wall time. A station whose threads all sleep,
  in waits the census cannot see, is idle whenever the host gets to it, at
  the same T, so answering for it changes nothing. One that never stops
  running stops the run.
- **A thread that waits for time by reading the clock sees T move.** It
  never stops running, so T would wait for it while it waited for T. Its
  100 000th read of one instant first sleeps the shortest sleep there is,
  which the shim ends at the next whole millisecond of node time (or the
  chips' next event, with `SIM_MESH_SHORT_WAITS=chip`), and the read answers
  after it; while it goes on waiting there, each next instant takes 1 000
  reads. Work that reads one instant fewer times takes no T, and work that
  never reads the clock holds T until it is done. A hundred thousand is
  above what a working loop of reticulum's stations reads in a city run (a
  thousand was reached), and a loop waiting on the clock passes it in a
  fraction of a second; 1 000 reads a step keeps each step's barrier cheap,
  where 100 000 made a tool waiting on the wall clock give up mid-turn. The
  rule is the conductor's `readNowUs`, behind `simradio_node_us` and behind
  the shim's clocks (`read_us` in `simclock_ops`); the thread that applies
  the ether's grants never sleeps there, since only it moves T.
- **Unread input holds T however long the reader's host takes**: the ether's
  second of grace for a channel nobody reads is off.

**What comes from outside the run lands at an instant of T.** The ether
turns everything that can wake a station other than its own messages into an
instant ([`ether/INTERNALS.md`](ether/INTERNALS.md#what-does-not-come-over-the-air)):

```
testbed      sleep ends at T; T holds until what it woke has run
testbed      typed(sid, n) — T waits;  sync(sid) — station told T, idle
testbed      writes the pty;  station reads, shim → ether  read {tty, total}
ether        the read is the station speaking — it owes an idle for that work
station      prints a reply, idles;  testbed reads every pty that ran, then T moves
writer       shim → ether  wrote {tcp/A>B, n, go}, waits
ether        quiet: reader told T, then  go  (lowest station first)
reader       reads, shim → ether  read {tcp/A>B, n};  it owes an idle
```

A read is the station speaking, the same as a `state` or a `tx`: the shim's
report marks it busy at the conductor (`spoke`) and at the ether, and the
station says its idle once the work is done. A `run` sent to it for the
purpose would land at the host's moment, in the middle of that work: a
reticulous station took it as an interrupt at a host-paced point, and the
round robin among its tasks of one priority moved.

The shim does the station's side of it, with no help from the firmware: it
counts what `read` takes from descriptor 0 and reports the running total once
a read leaves nothing waiting; it asks before a `write`/`send` on a TCP
connection to another station, on a socket of the writing thread's own
(`SIM_MESH_ETHER`), and reports what `read`/`recv` take from one; and it makes
a station's TCP connection to a loopback address leave from the station's own
address (`SIM_MESH_BIND_ADDR`), so both ends of it say which station they are.
Its reports go on the chip library's own socket to the ether, found from the
`hello` sent on it, so each is ahead of the idle that follows it. The
testbed's waits are on T as well: `Driver.pause`, `simd`'s `sleep`, `after`
and the role poll, and a framed-RPC query's timeout. With the same seed and
epoch, two runs of the same network put the same frames on the air at the
same instants.

A script takes turns with T the same way: simd holds T from each answer it
gives the script until the script yields, and a yield names the turn it
gives back (`sim_mesh/sim.py`, *Turns*). One that crossed an answer on the
wire, sent before the script saw it, would hand back the floor that answer
gave, and the script's next command would land at whatever T the host had
reached meanwhile. For the same reason a script's call counts as waiting
only until its coroutine ends on the loop, not until its thread has taken
the result.

**A station whose input is a socket idles on it.** A firmware that polls
its sockets on timers and idles in a plain sleep is never woken by what is
written to it: the ether holds T while the bytes are unread, and the sleep
waits for T. A Portduino firmware's idle is therefore a `poll()` on its
watched sockets (`radio/portduino`), which ends when one is readable. And no
wait the firmware does between its idles may take T while input could be
pending: a radio driver's sub-millisecond settling wait after each SPI
transfer (RadioLib's `delayMicroseconds(1)`) is a sleep on T as well, and on
a pass of the main loop with a command just written it stands the station
until the ether's one-second grace lets T go, at a wall-clock instant. A
chip that is never busy needs no settling, so in the simulation such a wait
takes none.

What is still outside: a TCP connection from something that is not a station
(the page's proxy to a station's web UI), a station's UDP to another, the
files it shares with the testbed (a pty a tool its driver runs talks on),
and anything a person does, which lands at whatever T the run has
reached; and a firmware that reads its console other than by `read` on
descriptor 0 holds T a second each time it is typed at.

## What this cannot tell you

- Anything timed below the tick, and anything that depends on the chip's own
  timing — BUSY after a wake, a peripheral's ramp, an errata.
- Memory: the heap ignores capabilities and wraps libc, so pressure on the
  external PSRAM (pseudo-static RAM), DMA-capable (direct memory access)
  allocation and internal-RAM exhaustion are all invisible.
- The radio's physics below the path-loss model, beyond a few figures. The
  error curve around the sensitivity threshold is an ideal receiver offset
  to the datasheet's one point, not a measured waterfall; fading, when asked
  for, is slow and Gaussian with a spread and a coherence time not yet
  fitted to anything, and fast fading one Rician draw per frame; there is no
  multipath within a frame, no antenna pattern and no noise floor but the
  thermal one. On real ground a pair's loss is P.1812's
  statistical figure at 50 % of time and 90 % of locations (or the
  geodata's `loc_pct`), not a measurement of that path. See
  [`ether/INTERNALS.md`](ether/INTERNALS.md) for what the medium does
  and does not decide.
- Anything below the C: the compiler, the ABI and the word size are the
  host's. Code that assumes a 32-bit `long` or pointer fails here and not
  on the chip, which makes the host build a free audit of width assumptions,
  and a clean run here is not a clean run on a board.
- Races between cores: the port runs one core. Scheduling faults look
  different too, since preemption runs through signals, so starvation and
  priority inversion are not the chip's.
- The boot chain, partitions, OTA (over-the-air) updates, safe mode,
  watchdogs, panics, core dumps and reset reasons (`esp_restart` is a process
  exit); growing the state partition at runtime, a factory reset by
  partition, and the updater.
- WiFi, BLE (Bluetooth Low Energy) and ESP-NOW; sleep and power management,
  GPIO wake included; and every I2C and SPI peripheral but the radio (GPS,
  IMU, RTC, SD card, battery ADC).

## What to model next, and what not

A simulator that is subtly wrong is worse than one that leaves a thing out:
it gives confidence rather than information. So every physical figure the
ether uses is to be held against a measurement on real boards, kept as a
regression check, and what gets modelled next goes in this order: capture,
wrong sync word, deafness while retuning, occupancy, the noise sum, the
error curve's waterfall and fading's spread and coherence time measured.
Multipath, antenna patterns and clock drift come after all of those, if at
all; past that point the cost grows and the answers do not change.

## Still to build

**The medium and the record**

- **Mode changes take the datasheet's time, and the ether honours
  `ready_at`.** The model charges each transition: from `STDBY_RC` to
  XOSC, FS, RX or TX 150 µs or the TCXO (temperature-compensated crystal
  oscillator) delay `SetDIO3AsTCXOCtrl` programmed; XOSC to FS 40 µs; FS to
  RX 40 µs; FS to TX the power-amplifier ramp from `SetTxParams`; a wake from
  SLEEP 150 µs plus 500 µs; `Calibrate` 3.5 ms. `ether_link.cpp` already
  sends `ready_at`; the ether treats a frame whose `t0` falls before a
  receiver's `ready_at` as energy with no lock. That makes a wrong retune or
  turnaround constant a frame loss that reproduces.
- **A loss cause for every frame at every station in range**, in the record.
  An `rx_end` says why a reception failed (`noise`, `interference`,
  `talked_over`, `lost`); still to come are the frames that never reached a
  reception — `no_rx`, `tx`, `settling`, `off_channel`, `wrong_rate`,
  `wrong_sync`, `no_lock`, `left_rx` — and a summary per station and per
  pair. A protocol claim ("`settling` cannot
  happen here") and a departure policy are judged by these.
- **A referee over `record.tsv`**, in part. After a run, `compliance.py`
  holds each node to EN 300 220's duty cycle, polite spectrum access and
  e.r.p., and `referee.py` audits the air: every `tx` that began over a frame
  on its carrier its sender could decode, whether and when the ether had told
  the slot of that frame (a slot told by a lock sent from inside a
  reception) and in which window of it; the frames nobody was told of; and
  every `crc`, by whether the overlapping senders could hear each other.
  Still to build: holding a station to any of it during a run, and
  listen-before-talk itself. That a slot was in RX or CAD before a `tx` is
  in the record; whether its firmware read the channel, and what it found
  there, is not: an RSSI read or a CAD's result never reaches the ether.
- **Calibrating the error curve and fading**: three numbers stand on
  assumptions (ether/INTERNALS.md, "The noise" and "Fading"). The curve's
  position: the datasheet's 1% at its threshold for a 64-byte frame, to be
  replaced by a step-attenuator sweep on the tools/rncapture bench through
  the threshold at SF7/125 kHz, 0.5 dB steps, ~200 frames a step, 20- and
  200-byte frames, counting clean, CRC error and nothing received; fit the
  offset to the 20-byte curve and check the 200-byte one lands where the
  model puts it unfitted (that is the test of the length effect), at SF12
  too if time allows. σ and the coherence time: from deployed nodes' frame
  to frame RSSI per link (the firmware's per-frame telemetry carries one for
  every frame, CRC failures included), the spread around a link's long-run
  mean and its autocorrelation's decay; the share of CRC errors among
  receptions is the check, and a sim run of the same nodeset with the fitted
  figures should give the same order of magnitude. Far more CRC errors in
  the sim points first at too short a coherence time, then at a lock stage
  too lenient beside the payload stage.
- **`next_instant()` from a heap**: the pending `until`s kept in a heap keyed
  by instant and station, updated on idle, retraction and leave, stale
  entries dropped when popped, instead of a scan of every station per
  barrier.
- **Runs off the bind mount**: a run's directory on the container's own
  filesystem or a tmpfs, with the page and the analysis tools reading it
  there.
- **Hardware checks**: RSSI (received signal strength) at two known
  distances and the channel-switch success rate on real boards, kept as
  regression checks on the ether and the loss model. Time on air is checked
  against a board, not only against the formula.

**Stations and firmware**

- **A snapshot keeps the scripts' log level.** `script_loglevel` is kept in
  `run.yaml` but not in a snapshot (`runs.take_snapshot` writes the builds,
  firmware and rules only), so a simulation loaded from one logs at the
  default level.

- **A `messages` layer** in the scripting library that compares delivery
  from one node to another across the `reticulum`, `meshcore` and
  `meshtastic` categories.
- **The mixed-firmware walkthrough** in the README: announces crossing both
  ways, a path through another firmware's transports, a two-frame split both
  ways, carrier sense under contention, and the hidden terminal, each with
  its commands and what `seq.py` shows.

**Running and analysing**

- **Freeze and step**: T held at the barrier while the map, the consoles and
  every station's web UI stay live, and a step that moves T on by a given
  amount and holds it again. Pause stops simd; this does not.
- **Single stations off and on** with their state kept (drawn grey), as
  `sel.stop()` and `sel.start()` in the library and on the node menu.
- **The run analyses as tools** in `testbed/sim_mesh/reticulum/` beside
  `delivery.py`, reading a run's events and record: peers withdrawn and the
  routes they dropped; directory entries, full pools and evictions;
  neighbour-table rows and evictions; paths a reloaded snapshot kept;
  calling-channel power and rate steps per target — each a `reticulum`
  event a driver reports. `callkind` classes a short HEADER_2 frame
  correctly.
- **Traffic scripts for SUPE's departure policy**, the one pure function
  `should_channel switch(peer_state, queue_state, channel_state) -> no | now |
  wait_until(t)`, each reporting losses by cause: an interactive exchange that
  waits on every delivery proof (where a naive hold timer is strictly
  harmful), a receiver-driven resource transfer, many stations on one
  transport, a peer that is not there, and SUPE and plain-LoRa nodes sharing a
  channel (whether SUPE is a good neighbour). The same runs settle SUPE's
  stated timing constants: turnaround, retune gap, burst gap, guard, seed gap
  and the schedule's spacings, jitters and lifetimes. No policy is committed
  in code before these have measured it.
- **Run options on the page**: a paced `<k>x`, a firmware override, and a
  new simulation started from a snapshot.
- **Which firmware a simulation runs** on its row, and on the Firmware tab
  the running simulations that use each one beside the paused runs and
  snapshots that hold it.
- **An LR2021 virtual radio**, `libsimradio-lr2021.so` beside the SX1262's,
  and the ether's modulations it needs beside LoRa.
- **Firmware changed mid-run as an experiment**: `.firmware()` after a
  script's simulation has started restarts the nodes it changes with their
  state kept; an upgrade test
  across firmwares whose stores differ needs a rule for what becomes of it.
- **An editable node id** in the editor, refusing one another node has and
  saying that the station restarts.
- **The snapshots in `testbed/snapshots/`**, still in the scenario format,
  retaken from converted runs or removed.
- **A live-reload mode** for working on the page: the Quasar dev server beside
  the front, with `/ws` and `/api` proxied.
- **Fewer costs per barrier in simd**: the UDP send per station and the JSON
  encoding are its largest.

**Ground and losses**

- **433 and 915 MHz tables on a pack**: `link.json` takes a carrier, so a
  pack gives all three bands.
- **A batched pair request** in `planner-web`, parallel over pairs, instead
  of one request per cell.
- **sim-mesh-examples' packs**: a few small real regions built once and
  published as release assets, each in the index with its sha256 and a
  nodeset made for it (a dense flat city, a mountain valley, a coast,
  hilly farmland), as the standard ground tests are run on.
- **A geodata editor** on the Geodata tab's map, and synthetic terrains other
  than `flat`.
- **Losses from a running network**: measured cells (the table's flag bit 4
  and `samples`), a lower bound for a pair that does not hear, and the
  residuals as calibration. Below the noise floor RSSI reads the noise, so
  received power there is SNR (signal-to-noise ratio) plus the floor; the
  median is taken per direction over time; a pair that does not hear gets a
  bound from the sensitivity, not a value; the sender's power is needed per
  frame, because SUPE varies it.
- **x86_64 pre-built firmware** beside the aarch64, from a builder of that
  architecture.
