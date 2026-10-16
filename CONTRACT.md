# The firmware contract

This document is everything needed to make your own firmware for sim-mesh.
It describes the entire interface between sim-mesh and firmware adapted for
it: the zip file format and everything that is provided to a station: time,
the driver, the ether and the virtual radio. sim-mesh doesn't know anything
about any specific firmware: it only talks to its driver.

sim-mesh has an ether process that takes transmissions from all the stations
and decides what, if anything, each of the others receives, and when.
Firmware can speak the ether's protocol directly, but most use a virtual
radio sim-mesh provides instead. One is a model of the SX1262 LoRa
transceiver, linked into your firmware in place of the real chip's SPI bus.
Your driver talks to it frame by frame as it would to the hardware: it reads
and writes registers, and when a packet arrives the virtual DIO1 line rises
and your interrupt handler runs. Meanwhile, a small library loaded into every
station, the time shim, answers the C library's clocks, sleeps and timeouts
with simulated time, and random numbers come from the run's seed, so a run
can be reproduced.

A firmware that keeps to this contract runs on sim-mesh with nothing of it in
sim-mesh, and sim-mesh runs any such firmware. **Must**, **must not** and
**may** are requirements; everything else describes.

```
project ── its own script ──► <base>_<arch>_<version>.zip ──► sim firmware add ──► firmware/<name>/
script  ── nodes().firmware("<base>_latest") ──► simd: resolve, import the zip's driver.py, DRIVER(firmware)
simd    ── start the executable: SIM_MESH_* env, LD_LIBRARY_PATH (the radio), LD_PRELOAD (the time shim)
station ── libsimradio-sx1262.so ── UDP, JSON ──► the ether          hello, state, tx, idle …
simd    ── driver.wait_up / verbs / flush ──► the station          over its console, a tool, a file …
station ── console lines ──► driver.console_line ──► station.report(event) ──► run/events.jsonl
```

## 1. The firmware zip

A firmware is one station build for one architecture: a zip archive named

```
<base>_<arch>_<version>.zip
```

- **base**: lower-case letters `a`–`z`, digits, `-` and `.`, starting with
  a letter or digit; never `_`. Two builds that differ in anything sim-mesh
  does not read, the virtual radio they drive among it, are two bases; by
  custom the base ends with the radio (`relay-sx1262`).
- **arch**: the architecture it runs on, as `uname -m` spells it on Linux:
  `aarch64`, `x86_64`.
- **version**: either a build stamp, the UTC time of the build as
  `YYYYMMDDhhmmss` (fourteen digits), or a semantic version
  `MAJOR.MINOR.PATCH` with an optional `-pre-release`. A base **must** keep
  to one of the two.

The name splits at its first and its last `_`. Installed, the firmware is the
directory `firmware/<base>_<arch>_<version>/`, the zip unpacked, so a zip and
its installed directory have one name. `<base>_latest` means the newest
installed firmware of exactly that base for the machine: by stamp, or by
semantic version (a pre-release before its release).

Members sit at the archive's root, with forward-slash paths that **must**
stay inside it (no absolute path, no `..`). It holds `node.yaml`, the
executable, the driver, and whatever else the station runs. A member's Unix
permission bits, where the archive records them, are the file's; the
executable is made executable whatever the archive says.

A zip is refused when it is built for another architecture than the
machine's, when its base already holds firmware versioned the other way,
when a firmware of its name is installed already, and when its `node.yaml`,
or anything it names, is missing.

## 2. node.yaml

A YAML mapping at the archive's root.

| Key | Required | Value |
|---|---|---|
| `base` | yes | as in the name |
| `arch` | yes | as in the name |
| `version` | yes | as in the name, a string |
| `category` | yes | the driver interface its driver implements, and so the verbs it answers: `reticulum` (§7), `meshcore` (§7a), `meshtastic` (§7b) |
| `exec` | yes | the executable's path in the archive |
| `driver` | yes | the driver's path in the archive, a Python file (§6) |
| `radio` | no | the virtual radio it is linked with, by the library's name: `sx1262` is `libsimradio-sx1262.so` (§9). Absent: sim-mesh provides none, and the station speaks the ether's protocol itself (§8) or has no radio |
| `fixed` | no | the path of a read-only data tree in the archive, which its driver may hand the station |
| `env` | no | a mapping of environment variable to value, given to the station after sim-mesh's own (§3); a value starting `./` or `../` is a path in the installed firmware. `LD_LIBRARY_PATH` here is put before sim-mesh's, not instead of it |
| `title` | no | what the page calls it (`Relay 2.1 (dev)`) |
| `hardware` | no | the hardware it plays (`ESP32-S3`), shown as “virtual ESP32-S3” |

Other keys are kept and not read.

## 3. What a station is given

sim-mesh starts the executable with its argument list from the driver (§6),
its working directory its own (below), its stdin and stdout one pty (§4),
and this environment: the host's, then these, then node.yaml's `env`, then
the driver's `env`.

| Variable | Meaning |
|---|---|
| `SIM_MESH_NODE_ID` | a small integer, unique on the host: the station's id in the ether (its `sid`), and the last bytes of any MAC address it makes |
| `SIM_MESH_NODE_DIR` | its directory and working directory; its state lives under `state/` |
| `SIM_MESH_BIND_ADDR` | its own loopback address, fixed by its id; every socket it opens **must** bind here |
| `SIM_MESH_ETHER` | `host:port` of the ether |
| `SIM_MESH_RADIO_LIB` | the path of the virtual radio its node.yaml names; its directory is first on `LD_LIBRARY_PATH` |
| `SIM_MESH_BOARD` | the board its node is, one flat JSON object: `chip`, `max_dbm` (the most at the antenna connector), and above 22 dBm a GC1109 front end's `fem_part`, `fem_tx_cal` (chip register → connector dBm), `fem_gain_db` and `fem_rx_gain_db`. The virtual radio applies the front end from it; a firmware that drives a front end takes its figures from here |
| `SIM_MESH_TIME` | `virtual` in a virtual-time run, absent in a real-time one |
| `SIM_MESH_EPOCH_US` | virtual time: the wall-clock microseconds T 0 stands for |
| `SIM_MESH_SEED` | virtual time: the run's seed; the time shim keys the station's `getentropy`/`getrandom` by it and the node id |
| `LD_PRELOAD` | virtual time: the time shim, `libsimclock.so` (§5) |
| `SIM_MESH_IDLE` | set by a driver whose station does not call `simradio_idle()` itself: `threads`, and the shim says the station is idle when every thread is blocked |
| `SIM_MESH_CLOCK_PROFILE` | optional, from node.yaml's `env`, or from simd's `--clock-ppm` (a crystal off by a draw within that many parts per million, per station), node.yaml's winning: node time as a function of T, `T:node,T:node,…` in microseconds, both increasing, slope 1 outside the points; absent, node time is T |
| `SIM_MESH_STRICT_TIME` | optional: `1` in the environment sim-mesh runs in, which its ether reads and its stations inherit, for strict time (§5) |

A station reads its identity from these and from nowhere else, so two
stations on one host never collide. **A station never learns where it
stands**: its position is the ether's and the loss table's alone.

**What a station may count on.** sim-mesh runs everything in its own image,
the same on every machine: Ubuntu 24.04 of the host's own architecture. An
executable **may** count on its C library and C++ runtime (`libc`, `libm`,
`libstdc++`, `libgcc_s`), on the radio library node.yaml names, on the time
shim, and on the image's `python3`, CPython 3.12 with its standard library
(its driver runs in it, with `aiohttp` and `pyyaml` beside `sim_mesh`).
Anything else it loads **must** be in its zip: a shared library under `lib/`
with `env: {LD_LIBRARY_PATH: ./lib}`, a Python package under a directory it
puts on `PYTHONPATH`, built for CPython 3.12; a firmware written in Python
may use the image's interpreter and need bring none. A zip **must not**
carry a radio library.

## 4. The process

- **stdin and stdout are the console**: a pty, text, shown in the map's
  console window and appended to `log` in its directory, and handed to its
  driver line by line (`console_line`). The one binary thing that may cross
  it is a framed RPC frame, which sim-mesh takes out of the stream before
  anything else sees it (§6). A driver may ask for a pipe each way instead
  (`console_tty`).
- **Exit to reboot.** sim-mesh starts the executable again, on the same
  directory and address, half a second later (of T, in a virtual-time run).
- **State is its directory.** A new simulation starts it with an empty
  `state/`, or with one a snapshot kept; a factory reset empties it; a reset
  leaves it.
- **Ports are its own**, on its own address. A web UI is on the port its
  driver names (`web_port`), reached through sim-mesh's proxy as
  `<node>.<simulation>.sim.localhost:8800`.
- **It may be several processes.** A process it starts that waits on time
  joins a virtual-time run as a station of its own, under the id its driver
  names for it (`sids`), with the same address, the time shim and
  `SIM_MESH_IDLE=threads`: it opens its link (`simradio_station_open`) and,
  with no radio, no chip. Which of them reads the console is the driver's to
  name (`console_sid`).

## 5. Time

In simulated time, the clock belongs to sim-mesh, not your computer. It
stands still while any station is still busy, then jumps straight to the next
moment something is due to happen. Most firmware works this way without
changes, as long as it keeps the four promises below.

In a real-time run a station keeps the host's time. In a virtual-time run the
ether owns conductor time T and moves it only when every station is idle
(§8), and a station keeps four promises:

- **It reads time only through the C library or the radio library.** The
  time shim answers `clock_gettime`, `gettimeofday`, `time`, the sleeps,
  `setitimer` and the timeouts of `poll`, `select`, `epoll_wait`,
  `pthread_cond_timedwait` and `sem_timedwait`/`sem_clockwait` in node time.
  A raw `rdtsc`, a `clock_gettime` made by system call, or a wait none of
  those is does not move with the run.
- **It opens its link early**, before anything in it waits on time:
  `simradio_station_open` is where its clock starts and the shim attaches;
  until the ether's welcome the monotonic clocks read 0 and the wall clocks
  the run's epoch.
- **It says when it is idle, and until when.** Idle is every thread blocked;
  the `until` it reports is the earliest wake anything in it holds
  (`simradio_wake_at`). Either the host calls `simradio_idle()` itself, a
  scheduler's idle with a wake at its next due tick and timer, or its driver
  sets `SIM_MESH_IDLE=threads` and the shim keeps a census of its threads. A
  station that says neither is reported idle by the radio library's
  watchdog, 20 ms of wall time after every message once none of its threads
  is on the CPU: it runs, but crawls.
- **It reads its console and talks TCP to other stations through the C
  library**: `read` on descriptor 0, and `read`/`recv`/`send`/`write` on its
  sockets. The shim counts those bytes for the ether, which holds T until a
  station has read what it was sent; a station that has read its input owes
  an idle for the work it does on it. Input taken another way holds T a
  second of wall time each time, or, under strict time, for good.

**Strict time.** Ordinarily the host's speed can still leak into a run: a
station that stays busy past the watchdog has T moved under it by however
long this host took, and two runs part there. With `SIM_MESH_STRICT_TIME=1`
nothing on the wall clock decides where T goes, and two runs given the same
seed and epoch match line for line:

- the watchdog answers for a station only once none of its threads is
  running; one still at work holds T however long it takes, and one that
  never stops stops the run, saying so every 10 s of wall time;
- a thread that waits for time by reading the clock in a loop sees T move:
  its 100 000th read of one instant first sleeps until the next whole
  millisecond of node time (or the chips' next event, with
  `SIM_MESH_SHORT_WAITS=chip`), and while it goes on waiting each next
  instant takes 1 000 reads;
- input a station has not read holds T however long its host takes.

In return, nothing the ether says reaches the host piecemeal. The radio
library applies a datagram whole, the chip's own timers running as T moves,
before the host is told of any of it: the host's waits that fall due,
`simradio_on_advance` and DIO1 come after, so a thread woken at T finds all
of T.

## 6. The driver

The driver is a translator. When a script says "send a message", the driver
turns that into something your firmware understands: a command on its
console, an RPC call, a tool it runs. It also reads what your firmware
prints, so sim-mesh can tell whether the message arrived. This is the only
firmware-specific code sim-mesh ever runs.

The driver is a Python module in the zip, imported by sim-mesh from the
installed firmware's directory under a module name of its own. It **must**
define `DRIVER`, a subclass of its category's driver class (§7), which
subclasses `sim_mesh.driver.Driver`. It **must** import from sim-mesh only
`sim_mesh.driver` and its category's module; anything else in sim-mesh may
change under it. sim-mesh makes one `DRIVER(firmware)` per installed
firmware a run uses, shared by its stations.

**What a driver is given.** `self.firmware`: `dir`, `exec`, `fixed`, `env`
(paths made absolute), `driver`, `firmware` (its name), `base`, `arch`,
`version`, `category`, `radio`, `title`, `hardware`. Every call names a
`station`, which offers:

| | |
|---|---|
| `name`, `node_id` | the node's name and its id |
| `dir`, `addr`, `ether_addr` | its directory, loopback address, the ether |
| `board` | `SIM_MESH_BOARD`'s JSON, or None |
| `log_path` | the file its console output is appended to |
| `status` | `stopped`, `starting`, `setup`, `up`, `restarting` |
| `starts` | how many times its process has been started |
| `virtual` | True in a virtual-time run |
| `rpc` | framed RPC on its console, or None before its process starts |
| `await sleep(s)` | a wait on the run's clock (T in virtual time) |
| `await restart()` | its process stopped, for the supervisor to start again |
| `report(event, **fields)` | an event into the run's `events.jsonl` at the run's T |

And from `sim_mesh.driver`: `CommandError` (anything that could not be done;
its text is shown as it is), `run_tool(argv, timeout, env=None)` (a helper
program to completion, `env` added to sim-mesh's environment),
`chip_dbm(board, connector_dbm)` (the chip power that puts that much at the
connector through the board's front end), `parse_setting`, `UP`, and the
framed RPC constants.

**What a driver implements.**

| Method | |
|---|---|
| `configured(station)` | **required**: True when the station's directory shows it has been set up. sim-mesh samples it at the fork; a station not set up is set up once it is up |
| `async wait_up(station, timeout)` | **required**: True once the station has booted and every service is up; False after `timeout` seconds of the run's clock |
| `argv(station)` | the command line; `[exec]` |
| `env(station)` | what it adds to the environment; nothing |
| `sids(station)`, `console_sid(station)` | the ether ids of its processes, and of the one reading the console; the node's own |
| `async run(station, line)` | a line in the firmware's own language (a script's `exec`, **Run command**); what it said back |
| `async flush(station)` | make what it was told durable, or apply it; asked after setup, before a stop, a reset or a snapshot |
| `web_port()` | its web UI's port; None |
| `console_line(station, line)` | a callback: each line the station prints, in order |
| `role_volatile` | True for a firmware that forgets its role at a restart: the role its first-boot rules gave it is said again at every boot |
| `console_tty` | False for a firmware whose console need not be a terminal: it gets a pipe each way instead of a pty, and its output must reach the pipe line by line; True |
| `console_acted_on` | False for a firmware whose console is log lines alone, nothing sim-mesh acts on (no framed RPC): its console is read as it comes rather than before T moves on, which a large run is much faster for. `console_line` is called either way; True |

Its helpers: `await self.pause(station, s)`, a wait on the run's clock;
`await self.joined(station, timeout)`, True once the station has joined the
ether, at the T of its hello (at once in a real-time run), so what the
driver does next it does at that T; `with self.tool_turn(station):` around a
tool run against the station's host door, during which T stands while the
tool has the floor and runs while the station works on what it read;
`await self.tool_settle(station, timeout=5.0)`, inside that turn before the
tool writes, waits on the wall clock until the station has said it is idle,
so its bytes land at the turn's T rather than in the middle of the station's
work there, and never moves T;
`await self.rpc_ready(station, timeout, marker_wait_s)` and
`await self.rpc_query(station, line, timeout=None)`, framed RPC.

**Framed RPC on the console.**

```
station → sim-mesh   "… framed rpc v1"                        once, early in boot, as log text
sim-mesh → station   F5 53 47 01 <id> <len:2> <a command line>
station → sim-mesh   F5 53 47 01 <id> <len:2> <what it printed>
```

A firmware **may** multiplex a framed side channel onto its console: a frame
is never echoed and never enters its line editor, and sim-mesh takes each
reply frame out of the stream, so the log and the console window never see
one. One frame is in flight per station; a command line is at most 4096
bytes; the id is a hash of the command, so a retry carries the one it had
([Framed RPC on the console](README.md#framed-rpc-on-the-console)).

## 7. The `reticulum` category

Every firmware does things its own way, but a script shouldn't have to care.
A category is a shared vocabulary: every MeshCore firmware answers `msg` and
`advert`, every Meshtastic one answers `sendtext`, so one script can drive any
firmware of that kind and compare them. This section and the next two give
each category's vocabulary.

A firmware of category `reticulum` is a Reticulum node. Its `DRIVER`
subclasses `sim_mesh.reticulum.driver.ReticulumDriver` and implements these
verbs, each taking the station first: every firmware's (`name`, `radio`,
`radio_up`, `tx_power`, `diagnostics`, from `sim_mesh.driver.Driver`), then
the category's. A verb it cannot do raises `CommandError`
(`self.cannot(verb)`), which the default does. A script reaches the
category's as `<selection>.reticulum.<verb>`, and they are nothing to a
station of another category.

| Verb | Means | Returns |
|---|---|---|
| `name(name)` | the node's name | |
| `role(role)` | `transport` (forwards others' traffic) or `client` | |
| `radio(freq_mhz=, sf=, bw_khz=, cr=, tx_dbm=, sync=, preamble=)` | slot 0's LoRa settings, only those given; `tx_dbm` at the antenna connector | |
| `radio_up()` | the radio started, for a firmware whose radio waits for it; nothing otherwise (default) | |
| `tx_power(dbm)` | transmit power at the connector | |
| `path(dest=None, iface=None)` | | its path table, `[{dest, next_hop, iface, hops}]`: the entries for `dest` (32 hex digits) and on `iface` (as the firmware names it) when given, all without |
| `peer_tcp(addr, port)` | a TCP link to another station | |
| `current_role()` | | `transport`, `client`, or None (default) |
| `diagnostics()` | | {label: text}, what a traffic run keeps of it; {} (default) |
| `lxmf.create(name)` | one more LXMF identity, `name` its display name, unless the station has one by that name. **Must not** refuse: a firmware with one identity, named after the node from its start, gives it `name`, and once it has another name does nothing | its delivery address, or None while it has none |
| `lxmf.identities()` | | `[(name, delivery address)]`, the one it sends from unless told otherwise first; `[]` while it has none |
| `lxmf.announce(name=None)` | an announce of that identity's delivery destination (None: the first) | |
| `lxmf.send(dest, text, mid, sender=None)` | an LXMF message to `dest` (32 hex digits) from its identity `sender` (None: the first); `mid` is sim-mesh's id for it | |

A verb with a dot is the method with an underscore: `lxmf.send` is
`lxmf_send`.

**Events.** For every message `lxmf.send` was given, the driver **must**
report how it ended, under `mid`, from `console_line` or however else it
learns it:

```
self.lxmf_status(station, mid, "delivered")
self.lxmf_status(station, mid, "failed", why=<text>)
```

and **may** report the statuses between (`pending`, `sent`); the last one
reported is what a traffic report shows for a message that never arrived.
Each is the event `lxmf.message.status` with `mid`, `status` and `why`. A
firmware's own id for a message is coupled to `mid` with
`self.lxmf_couple(station, mid, its_id)` once the driver learns it, and what
the station says under its own id is given to
`self.lxmf_native(station, its_id, status, why)`, which reports it under
`mid`, holding it until the coupling is made; what goes wrong before the
firmware has an id is reported under `mid` directly. sim-mesh writes each
event as a line of the run's `events.jsonl`:
`{"t": <T in µs>, "node": …, "event": …, …fields}`.

**What arrived.** `delivered` above is the sender's word: a proof came
back across the mesh, a packet as easily lost as the message. Whether the
message reached its recipient is the recipient's word, and a traffic report
counts the two apart. A driver whose firmware says them **should** report,
for every message `lxmf.send` was given, its LXMF message id, and for every
LXMF message its station receives, that id:

```
self.lxmf_message_id(station, its_id, message_id)    # lxmf.message.id {mid, message_id}
self.lxmf_received(station, message_id, sender=<hash>, refused=<why>)
                                                     # lxmf.message.received {message_id, sender, refused}
```

The message id is the hash sender and recipient both compute, in hex; a
firmware that logs only its first sixteen bytes gives those, and ids are
matched on them. The first is given under the firmware's own id and held
until the coupling, as `lxmf_native` is. `refused` says why a message that
arrived was not taken (a full mailbox): it is not delivered. A firmware that
reports no arrivals has its messages counted by proof alone.

## 7a. The `meshcore` category

```
script ── node(n).meshcore.msg(to, text) ──► simd: mid ──► msg(dest, text, mid) ──► station
sender ── sent, acknowledged ──► its driver ──► msg.status {mid, status}
receiver ── the message, mid in its text ──► its driver ──► msg.received {mid, text, sender | chan}
```

A firmware of category `meshcore` is a MeshCore node: a companion, a
repeater or a room server. Its
`DRIVER` subclasses `sim_mesh.meshcore.driver.MeshcoreDriver` and implements
every firmware's verbs and the category's, each taking the station first; a
verb it cannot do raises `CommandError` (`self.cannot(verb)`), which the
default does. A script reaches them as `<selection>.meshcore.<verb>`. They
carry meshcore-cli's command names and mean what those commands mean; they
are not shaped like the `reticulum` verbs, and a comparison across
protocols is a layer above both.

| Verb | Means | Returns |
|---|---|---|
| `repeat(on)` | forwarding others' packets on or off | |
| `advert()` | a zero-hop advert | |
| `floodadv()` | a flooded advert | |
| `contacts()` | | `[(name, public-key prefix, path length or None)]`, None for a contact reached by flood |
| `msg(dest, text, mid)` | a direct message to the contact `dest`, by its name; `mid` is sim-mesh's id | |
| `chan(nb, text, mid)` | a message on channel `nb` | |
| `path(dest)` | | the contact's path, its hops' hash prefixes (`[]` for a neighbour), or None for flood |
| `reset_path(dest)` | back to flood for that contact | |

A script names the other end of `msg`, `path` and `reset_path` with `to`, a
contact's name, which is a node's own once it has advertised it; simd passes
it as it is. `msg` and `chan` are answered with the message's id.

**Events.** The sender's driver reports what became of every message under
its `mid`, as the event `msg.status`: `sent`, then for `msg` `delivered`
(its acknowledgement came back) or `failed` (with `why`); a channel message
has no acknowledgement and ends at `sent`.

```
self.msg_status(station, mid, "delivered")
self.msg_received(station, mid, text, sender=<public-key prefix>)   # or chan=<nb>
```

The receiving station's driver reports every message the station received
as the event `msg.received`, with `mid`, the text, and the sender's
public-key prefix or the channel. `mid` travels in the message's text,
put there with `sim_mesh.driver.tagged(text, mid)` (`<text> #<mid>`) and
read back with `untagged`, so a receiver knows it with nothing of the
sender's. `msg_status`, `msg_received`, `tagged` and `untagged` are every
driver's (`sim_mesh.driver`), shared with the `meshtastic` category, and
`sim_mesh.meshcore.driver` offers them under the same names.

## 7b. The `meshtastic` category

```
script ── node(n).meshtastic.sendtext(text, to) ──► simd: mid ──► sendtext(text, mid, dest) ──► station
sender ── queued, then its routing answer ──► its driver ──► msg.status {mid, status}
receiver ── the text, mid in it ──► its driver ──► msg.received {mid, text, sender | chan}
```

A firmware of category `meshtastic` is a Meshtastic node. Its `DRIVER`
subclasses `sim_mesh.meshtastic.driver.MeshtasticDriver` and implements
every firmware's verbs and the category's, each taking the station first; a
verb it cannot do raises `CommandError` (`self.cannot(verb)`), which the
default does. A script reaches them as `<selection>.meshtastic.<verb>`.
They carry the Meshtastic CLI's option names (`--sendtext`,
`--traceroute`, `--nodes`) and mean what those mean.

| Verb | Means | Returns |
|---|---|---|
| `role(role)` | its device role, Meshtastic's in lower case: `client`, `client_mute`, `client_hidden`, `client_base`, `router`, `router_late`, `tracker`, `sensor`, `tak`, `tak_tracker`, `lost_and_found` | |
| `hop_limit(n)` | the hops a packet it originates may take, 0–7 | |
| `sendtext(text, mid, dest=None, ch_index=0, want_ack=True)` | a text message to the node named `dest`, or on channel `ch_index` when `dest` is None; `mid` is sim-mesh's id | |
| `traceroute(dest)` | | `{route, snr_towards, route_back, snr_back}`, hops as node names where known |
| `nodes()` | | `[(name, id, hops_away, snr, last_heard)]` |
| `nodeinfo()` | a NodeInfo broadcast now | |

`current_role(station)` says `router` for `router` and `router_late`,
`client` otherwise. A script names the other end of `sendtext` and
`traceroute` with `to`, a node's name (its Meshtastic long name, which its
driver sets to the node's own); simd passes it as it is. A `sendtext`
without `to` is a channel message; a `traceroute` without it is refused.
`sendtext` is answered with the message's id.

**Events**, as for `meshcore` (§7a): the sender's driver reports
`msg.status` `sent` once the firmware has queued the message, or `failed`
with `why` when it refused it; a direct message then ends `delivered` (its
acknowledgement came back from its destination) or `failed` (the routing
error, `MAX_RETRANSMIT` among them); a channel message ends at `sent`,
unless the firmware refused it after all (a rate limit). A message still
open when its station restarts is reported `failed`, `why: "station
restarted"`. The receiver's driver reports `msg.received` with the sender's
`!id` or the channel, `mid` read back out of the text.

## 8. The ether's protocol

You only need this section if you're writing your own radio model or
simulating something that isn't an SX1262. If your firmware uses the virtual
radio, the radio speaks this protocol for you.

The ether is the medium between stations: it decides who hears each frame,
from the run's loss tables, and in a virtual-time run it owns time. A
station speaks to it over UDP, one JSON object per datagram, from a socket
bound to its own address; payloads are base64, times in microseconds. The
ether takes a station's `idle`, `state`, `tx` and `floor` only from the
address it said `hello` from, and drops any other naming it. The
virtual radio (§9) speaks it for a firmware; a firmware **may** speak it
itself ([`ether/README.md`](ether/README.md) has the whole of it).

A real-time run:

```
station → ether   hello {sid, slots}
ether → station   welcome {t, mode: "real", rate: 1, epoch, seed}
station → ether   state {slot, mode, mod, freq, bw, sf, cr, sync, hdr, crc, pre, iq[, side]}   on every change
station → ether   tx {slot, id, t0, t_pre, t_hdr, t_end, power_dbm, mod, freq, bw, sf, …, payload}
ether → station   rx_begin {slot, id, t0, t_pre, t_hdr, t_end, level[, cad][, hdr_ok][, det]}   each receiver it reaches
ether → station   rx_end {slot, id, t, verdict, [cause,] payload, rssi, snr}   at the frame's end, or at t_hdr for a header that failed
```

A virtual-time run is the same conversation with the ether as conductor:

```
station → ether   hello {sid, slots}
ether → station   welcome {t, mode: "virtual", rate, epoch, seed, seq: 1}
station → ether   idle {seq: 1, until: 25000}            nothing to do before T 25 000
ether → station   run {t: 25000, seq: 2}                 every station idle; T moves to 25 000
station → ether   state {…}  tx {t0: 25000, …}           what it did at 25 000
station → ether   idle {seq: 2, until: 30000}
ether → station   rx_begin {t: 25000, seq: 7, …}         to each receiver, at the same T
receiver → ether  idle {seq: 7, until: …}
```

| Station → ether | Says |
|---|---|
| `hello` | this station exists, and which radio slots it has; `"lines": 1`, it takes several messages to a datagram: in a virtual-time run it is sent every message the barrier has for it at one go |
| `state` | a slot's mode (`RX`, `TX`, `CAD`, `STDBY_RC`, `SLEEP` …), its modulation `mod` and its carrier: the ether matches receivers on these |
| `tx` | a transmission: its modulation and carrier, its power at the connector, its instants (start, end of preamble, of header, end), its payload |
| `idle` | virtual time: done with everything message `seq` gave it; next needs to run at T `until` (null: not on its own); `n`, the station's own count of its idles |
| `read`, `wrote`, `listen` | virtual time: bytes taken from its console or from another station over TCP, about to be written to one (with `go`: waits for the ether's `go`), and a TCP endpoint it listens on; the time shim sends these. A `read` counts as the station speaking: it owes an idle for the work it does on what it read |

| Ether → station | Says |
|---|---|
| `welcome` | joined: `t`, `mode` (`real` or `virtual`), `rate`, `epoch`, `seed` |
| `rx_begin` | a frame is arriving: its instants and its level; `cad: true` when it is energy to this station, not a frame to demodulate; `hdr_ok: false` when its header will fail at `t_hdr`, where the chip raises a header error and lets go; `det`, from 1, the side detector that found it |
| `rx_end` | that frame is over: the verdict (`clean`, `crc`, or `hdr` at `t_hdr`), why when not clean (`cause`: `noise`, `interference`, `talked_over`, `lost`; for the record, ignored by the station), the payload, RSSI and SNR |
| `run` | virtual time: T has reached what this station asked for, or it is told the run's T before input reaches it |
| `go` | virtual time: a TCP write asked for may go ahead |

- **`mod`** is the modulation; a receiver hears only a frame in its own. The
  ether models `lora` (with `bw`, `sf`, `cr`, `sync`, `hdr`, `crc`, `pre`,
  and `iq`, the IQ polarity, `normal` or `inverted`, matched like the sync
  word); a `state` or `tx` naming another is refused. A `state` may list
  side detectors (`side`: `{sf, sync, iq}` each, an LR2021's), which decode
  beside the main one on its carrier. It matches on preamble length nowhere,
  and only a side detector's lock reads it.
- The `id` in a `tx` is the transmitter's own count; in `rx_begin` and
  `rx_end` it is the ether's, unique across stations.
- **Virtual time.** Every message the ether sends carries `t` and `seq`; the
  station applies it at that T and answers with an `idle` for that `seq`,
  and messages are applied strictly in `seq` order, never twice. T moves
  only when every station is idle, to the earliest of their `until`s and the
  air's next instant. An idle is said again every 250 ms of wall time until
  something comes back, keeping its `n`, and the ether takes a copy of the
  idle it already has for the current message for nothing, even after the
  station has said something since; the ether answers an idle for an older
  `seq` said twice by sending what came after it again. A station that says
  `hello` again has restarted.
- Unknown message types are ignored, on both sides.

## 9. The virtual radio

A virtual radio is a model of one radio chip on a virtual SPI bus, and the
station's link to the ether, as a shared library sim-mesh provides:
`libsimradio-sx1262.so` is the SX1262. A firmware is compiled against its
header, [`radio/include/simradio.h`](radio/include/simradio.h), and linked
with it **by name** (`-lsimradio-sx1262`), and **must not** carry it:
sim-mesh puts its own on the station's `LD_LIBRARY_PATH`, so a firmware
keeps working when the model or the ether's protocol changes, and only a
change to `simradio.h` itself needs it rebuilt. It makes the simulation and
the hardware do the same thing: the firmware's own driver talks SPI to it
frame by frame, exactly as to the chip on a board.

```c
int  simradio_station_open(int sid, const char *bind_addr, const char *ether_addr);  /* once, early */
simradio_t *simradio_open(int slot, void (*on_pin)(void *ctx, int pin, int level), void *ctx);
void simradio_transfer(simradio_t *, const uint8_t *out, size_t len, uint8_t *in);    /* one NSS cycle */
void simradio_reset(simradio_t *);                                                    /* RST's rising edge */
int  simradio_pin(simradio_t *, int pin);                    /* SIMRADIO_PIN_DIO1, SIMRADIO_PIN_BUSY */
void simradio_set_services(const struct simradio_services *); /* a host's own, before anything else */

int     simradio_virtual(void);          int64_t simradio_node_us(void);
int     simradio_joined(void);           int64_t simradio_node_at_join(void);
int64_t simradio_epoch_us(void);         int64_t simradio_node_to_conductor(int64_t node_us);
int     simradio_wake_create(void (*due)(void *), void *arg);
void    simradio_wake_at(int wake, int64_t node_us);           /* INT64_MAX clears it */
void    simradio_idle(void);
void    simradio_on_advance(void (*moved)(void));
```

- **Open the link once, early**: `simradio_station_open(SIM_MESH_NODE_ID,
  SIM_MESH_BIND_ADDR, SIM_MESH_ETHER)`, before anything waits on time (§5);
  then `simradio_open(slot, …)` per radio.
- **One whole frame per NSS cycle.** A bus adapter hands the model
  everything the driver put on the bus between NSS going low and going high
  as one frame: writes inside a frame are appended, never passed on one by
  one; a frame containing a read is complete at the read; the adapter never
  adds a NOP of its own. A reassembly one byte off makes `GetIrqStatus` read
  the status byte as the IRQ word's high byte, so every flag looks set.
- **Interrupts.** `on_pin` runs on whatever thread moved the line. A pin
  shim whose interrupt is level-triggered **must** fire at once when the
  interrupt is enabled while the line is asserted: a driver that disables
  its interrupt, drains, and re-enables relies on a line still high
  re-firing.
- **The host's services.** The library needs a clock, one-shot timers, a
  recursive lock, a UDP socket and a thread to read it, and a log
  (`struct simradio_services` in the header). Its own are a plain process's
  threads; a host with a scheduler of its own (FreeRTOS on ESP-IDF's Linux
  target) **must** hand it its own with `simradio_set_services` before any
  other call (a constructor is the place), so model callbacks run as that
  host's tasks.
- **The front end** is the library's: what the chip radiates goes through
  `SIM_MESH_BOARD`'s transmit curve before the ether is told its power, and
  every level the chip reads is the front end's receive gain above the
  connector's. A firmware that drives a front end converts with the same
  figures.

## 10. Building against it

Start sim-mesh once first (`sim`, which fetches or builds its radio and
leaves `radio/build/libsimradio-sx1262.so`), then build the station for
Linux:

```sh
# C or C++
cc station.c -I sim-mesh/radio/include -L sim-mesh/radio/build -lsimradio-sx1262 -lpthread -o station

# Rust: link it by name from a build script
println!("cargo:rustc-link-search=native={}/build", radio_dir);
println!("cargo:rustc-link-lib=dylib=simradio-sx1262");

# PlatformIO on Portduino: sim-mesh's radio/portduino library, in lib_deps as
#   symlink://<path to sim-mesh>/radio/portduino
# links the radio by name and stands in for spidev and libgpiod
```

Then make the zip: the executable, the driver, anything else it runs, the
shared libraries beyond the C library and C++ runtime under `lib/`, and
`node.yaml`. Check what the executable loads with `ldd`: everything but
`libc`, `libm`, `libstdc++`, `libgcc_s`, the loader and `libsimradio-*`
belongs in `lib/`. Then `sim firmware add` it, and run a script with it.

## 11. A whole example

`node.yaml`:

```yaml
base: relay-sx1262
arch: aarch64
version: '20261001120000'
category: reticulum
radio: sx1262
title: Relay (dev)
hardware: ESP32-S3
exec: relay
driver: driver.py
fixed: fixed
env:
  LD_LIBRARY_PATH: ./lib
```

`driver.py`, for a firmware that answers framed RPC on its console:

```python
import os
import re

from sim_mesh.reticulum.driver import ReticulumDriver

QUEUED = re.compile(r"queued (\S+)")
MID = re.compile(r"mid=(\S+)")


class Relay(ReticulumDriver):
    def configured(self, station):
        return os.path.exists(os.path.join(station.dir, "state", "boot"))

    async def wait_up(self, station, timeout):
        return await self.rpc_ready(station, timeout, 20.0)

    async def run(self, station, line, timeout=None):
        return await self.rpc_query(station, line, timeout)

    async def name(self, station, name):
        await self.run(station, "hostname %s" % name)

    async def lxmf_create(self, station, name):
        return None                 # one identity, there from its start

    async def lxmf_identities(self, station):
        address = (await self.run(station, "address")).strip()
        return [(station.name, address)] if address else []

    async def lxmf_announce(self, station, name=None):
        await self.run(station, "announce")

    async def lxmf_send(self, station, dest, text, mid, sender=None):
        found = QUEUED.search(await self.run(station, "send %s %s" % (dest, text)))
        if not found:
            self.lxmf_status(station, mid, "failed", "not queued")
            return
        self.lxmf_couple(station, mid, found.group(1))

    def console_line(self, station, line):
        found = MID.search(line)
        if found and "delivered" in line:
            self.lxmf_native(station, found.group(1), "delivered")
        elif found and "failed" in line:
            self.lxmf_native(station, found.group(1), "failed", line)


DRIVER = Relay
```
