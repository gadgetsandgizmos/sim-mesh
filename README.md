# sim-mesh

A LoRa testbed, run in real time or in virtual time. Stations are real
firmware, built as Linux processes, each on its own loopback address; below
their radio driver sits a model of an SX1262 on a virtual SPI (serial
peripheral interface) bus, and between the models sits one medium, the
ether, that decides who hears what from a table of every pair's path loss.
A map in the browser places the nodes on real ground or on synthetic ground
and shows every frame on the air.

```
browser ── localhost:8800 ──► front.py ─┬─► simd (lora) ─┬─ ether        (UDP, in-process)
                                        │                ├─ stations     (firmware processes, one pty each)
                                        │                └─ proxy        <station>.lora.sim.localhost ─► the station's :80
                                        ├─► simd (supe)  …
                                        ├─► sim_mesh.runner  a script, top to end, driving one simulation
                                        ├─► planner-web  one per ground-data pack in use
                                        └─► planner-job  a pack built from its sources, a node map imported
```

One simulation is one simd; the front runs several behind port 8800, keeps
their registry, computes their loss tables, runs scripts against them,
starts the planner that supplies real ground and builds that ground from
public sources. Around them are the things
sim-mesh keeps, each on its own because each changes on its own:

| | What it is | Where |
|---|---|---|
| a **firmware** | one station build, installed from its zip: the executable, whatever it needs beside it, and its driver | `firmware/<base>_<arch>_<version>/` |
| an **antenna** | a kind of antenna and its radiation pattern | `testbed/antennas/` |
| **geodata** | the ground: a pack fetched pre-built from an index or built from public sources, or synthetic ground at 0°, 0° | `testbed/geodata/<name>/`: `geodata.yaml`, and the pack's files beside it |
| an **index** | a YAML file listing geodata packs and nodesets to fetch, each by its sha256, so standard ground is the same bytes everywhere | at its own address; the ones added here in `testbed/indexes.yaml` |
| a **nodeset** | which nodes stand where with what maximum power and antenna, their tags, the offsets and any stated links; its own setup script beside it | `testbed/nodesets/<name>.yaml`, `<name>.py` |
| a **loss table** | every ordered pair's path loss, derived from geodata and a nodeset | `testbed/losses/…`, a cache |
| a **script** | plain Python against the sim-mesh library, top to end: the time, what each node runs and is given at first boot, and what is done | `testbed/scripts/<name>.py` |
| a **run** | one simulation's output: its record, logs and state | `testbed/runs/<name>/` |
| a **snapshot** | a moment of a run, state and all, to start another from | `testbed/snapshots/<name>/` |

| Piece | Where |
|---|---|
| the launcher | `sim`, and its image, `Dockerfile` |
| the medium | [`ether/`](ether/README.md) |
| the virtual radios, shared libraries sim-mesh provides a station, and the time shim | `radio/` |
| the front, simd, the stores, the library, the page, the analysis tools | `testbed/` |
| the firmware contract: the zip, its node.yaml, its driver, the environment, the ether's protocol and the virtual radio | [The firmware contract](CONTRACT.md) |
| the loss table's file format | [LOSSTABLE.md](LOSSTABLE.md) |

It is the firmware under test, built for a different target — not an emulator.
sim-mesh itself knows no firmware project: each firmware arrives as a zip
that carries its own driver, the Python that tells sim-mesh how to talk to
it. [INTERNALS.md](INTERNALS.md) says how it works and why it is built this
way.

## Getting started

sim-mesh needs no firmware tree: stations are firmware zips, added from a
file or from the pre-built ones on [sim-mesh.net](https://sim-mesh.net/firmware/).

It needs **Docker or Podman** and nothing else, on Linux, a Mac or Windows:
`sim` builds its own image on first use (a few minutes, once) and runs
everything inside it, with port 8800 published. The image is the system a
firmware runs on, the same everywhere. `SIM_MESH_NATIVE=1` runs it on the
machine instead, for working on sim-mesh itself on Linux: that needs
`python3` with `aiohttp` and `pyyaml`, `node` and `npm`, `cmake` with a C
and C++ compiler, and `cargo` for real ground and the ether's conductor.

On a fresh Debian, Ubuntu or Fedora, `sim-mesh/sim install` puts that in
place as the image holds it: the system's packages through apt or dnf (with
sudo); a Node the page's build takes (22.22 or later) where the system's is
older, NodeSource's 22 on Debian and Ubuntu, which ship 18, and on Fedora,
whose default is 22.21, its own nodejs24, given its plain names in a Python
environment beside the clone, `sim-mesh/.venv`, which `sim` puts first on
the path; and Rust through rustup. A step whose result is there already is
left out; `--dry-run` says what it would do.

**1. Clone it.** The directory you clone into is the one `sim` mounts
into its image, so a firmware tree put beside sim-mesh later can build
against its radio:

```sh
mkdir mesh && cd mesh
git clone https://github.com/sim-mesh/sim-mesh.git
```

**2. Start it:**

```sh
sim-mesh/sim
```

On Windows without WSL, `powershell -ExecutionPolicy Bypass -File
sim-mesh\sim.ps1` does the same with Docker Desktop or Podman: it starts the
front, and every other verb runs inside it,
`docker exec -it sim-mesh-front ./sim <verb>`.

It builds whichever of the page, the virtual radios, the ether's conductor
and the planner (sim-mesh's own, in `planner/`; without cargo the last two
are left out, sim says so, synthetic ground works, and a virtual-time run is
refused until the conductor is built; real time needs none) is not
built yet or is older than a file of its sources (hidden directories and
`__pycache__` aside), so a fresh clone or a pull needs nothing more, then
starts the front and opens
`http://localhost:8800/`. The terminal is the
testbed's: Ctrl-C there stops every simulation and everything they started.

**3. Firmware.** A station runs an installed firmware. On the **Firmware**
tab, **Download pre-built firmware** lists what sim-mesh.net offers for this
machine's architecture (`aarch64` or `x86_64`) and **Add** installs one;
**Import zip** installs a zip of your own. From a shell,
`sim firmware add <zip or URL>` does the same.

**4. A first simulation.** Geodata and nodesets are kept in
`testbed/geodata/` and `testbed/nodesets/` and never committed: they are
fetched from an index, or made here. On the **Geodata** tab, **Download
pre-built geodata packs** lists what sim-mesh's own index, `sim-mesh-examples`, and
any index you add offer, and a click on one installs it; or make ground,
**Build** over a rectangle of the map or **New synthetic**. Click it to
choose it. The **Nodes** tab lists the nodesets with a node on it, beside a
map of the ones checked: **New** one and place nodes on it, **Import** them
from a public node map or a file, or fetch a nodeset from an index under
**Download pre-built nodesets**; a click on a nodeset opens it to edit. On
the **Scripts** tab open
`lxmf-traffic`, choose the firmware its nodes run in **Firmware for nodes
not otherwise configured** above the script, and **Run** it: a new
simulation of that nodeset on that ground. The page goes over to the
simulation's live map, framed on its nodes, and its stations come up over
the next minute; rings on the map are frames on the air. For a simulation
to work with by hand, run `realtime`: every node on the chosen firmware,
on the wall clock, set up by the startup script and left running.

**5. Play with it.**

- **Click a station** for the editor: how it is doing, its **Console** (its
  serial console) and **Web UI** (its own web interface, for a firmware that
  serves one), each in a window over the map with **−** and **+** for its
  zoom (the console's font; the web UI's page, as the browser's zoom, 75 %
  to start), and every setting it has.
- **Right-click the map ▸ New node here** puts down a station. It comes up
  and is set up on its own.
- **Drag a station** and its links change once its row of the loss table is
  recomputed.
- **Right-click a station ▸ Run command…** types one line at the selected
  stations, in their firmware's own language.
- **⋯ ▸ Save snapshot as…** on the simulation's row of the Simulations tab
  keeps the network and everything its stations have become, and **⋯ ▸ Load
  snapshot into it…** brings one back.

What to read next: [Firmware](#firmware), [Antennas](#antennas),
[Geodata](#geodata), [Nodesets](#nodesets) and [Scripts](#scripts) for
making your own network,
[The page](#the-page) for doing it on the map, [Running
simulations](#running-simulations) for the time modes.

## Firmware

A station runs a **firmware**: an executable built for Linux, whatever it
needs beside it, and a Python module, its **driver**, that tells sim-mesh how
to talk to it. A firmware arrives as a zip and is installed under
`firmware/`, unpacked into a directory of the zip's own name. sim-mesh holds
nothing of any one firmware project: what it knows of one is its driver.
[The firmware contract](CONTRACT.md) specifies the zip,
its `node.yaml`, the driver interface, what a station is given when it
starts, the ether's protocol and the virtual radio a firmware is built
against.

**A firmware is called** `<base>_<arch>_<version>`
(`relay-sx1262_aarch64_20260930163128`): its base, lower-case
letters, digits, `-` and `.`; the architecture it runs on, as `uname -m`
spells it; and a build stamp (`YYYYMMDDhhmmss`, UTC) or a semantic version
(`1.2.3`). The zip has the same name, `.zip` after it. Two builds that
differ in anything sim-mesh does not read — the radio they drive among it —
are two bases, and by custom the base names the radio. A build of someone
else's release ends its base in that release, its version our own build's
stamp: `meshtastic-sx1262-2.7.26_aarch64_20261007201200`.
**`<base>_latest`** names the newest installed firmware of that base for
this machine: of `<base>` and every `<base>-<semver>`, the highest release,
then the newest build of it, by stamp or by semantic version (a base keeps
to one of the two). So `meshtastic-sx1262-2.7.26_latest` stays on 2.7.26,
and `meshtastic-sx1262_latest` follows the newest release installed.

**A category** says what kind of mesh a firmware's stations make, and which
driver interface its driver implements: `reticulum`, `meshcore`
([§7a](CONTRACT.md#7a-the-meshcore-category)) and `meshtastic`
([§7b](CONTRACT.md#7b-the-meshtastic-category)). A script's
verbs are its category's (below), and the traffic and delivery analyses are
the `reticulum` category's own.

**Adding** one: **Import zip**, or **Add** in **Download pre-built
firmware**, on the Firmware tab, or

```sh
sim firmware add relay-sx1262_aarch64_20260930163128.zip
sim firmware add https://sim-mesh.net/firmware/<name>.zip
sim firmware prebuilt                     # what sim-mesh.net offers this machine
sim firmware list [SUBSTRING]             # what is installed, and what holds it
sim firmware delete [-f] SUBSTRING        # lists what it would delete, then asks
sim firmware resolve relay-sx1262_latest
```

A zip is refused when it is built for another architecture, when its base
already holds firmware versioned the other way, when a firmware of its name
is installed already, and when its `node.yaml` or anything it names is
missing. A paused run or a snapshot **holds** the firmware that wrote its
state, since it can only be resumed on it. `sim firmware delete` refuses a
held firmware, so the run or snapshot goes first. On the Firmware tab each
row says its size on disk and has a checkbox; **Delete selection** above the
list (shown while any are chosen) and each row's bin delete after one
confirmation. A firmware a paused simulation holds is deleted too: the
confirmation names the simulations, and they are stopped for good first. A
firmware a snapshot holds is kept, and the confirmation says so.

**Pre-built firmware** is listed on [sim-mesh.net/firmware](https://sim-mesh.net/firmware/)
(`SIM_MESH_FIRMWARE_INDEX` names another list),
whose `index.html` carries each zip's facts, so the page can say what each
is without fetching it. Projects put theirs there with `sim firmware publish`
([Publishing firmware](#publishing-firmware)).

### Drivers and the `reticulum` verbs

A script means the same thing on any firmware of a category: it asks for a
verb, and each station's driver does it its own way — a line typed at its
console, a framed RPC (remote procedure call) frame, a settings file and a
restart, a tool run against it. Every firmware's verbs (`sim_mesh.driver`),
then the `reticulum` category's (`sim_mesh.reticulum.driver`), which a
script reaches as `<selection>.reticulum.…`:

| Verb | Means | Returns |
|---|---|---|
| `name(name)` | the node's name | |
| `radio(freq_mhz, sf, bw_khz, cr, tx_dbm, sync, preamble)` | slot 0's LoRa settings, those given; `tx_dbm` is at the antenna connector | |
| `radio_up()` | the radio started, for a firmware whose radio waits for it | |
| `tx_power(dbm)` | transmit power at the connector | |
| `diagnostics()` | | what a run keeps of it at the end |
| `role(role)` | `transport`, which forwards others' traffic, or `client` | |
| `path(dest, iface)` | | its path table, `[{dest, next_hop, iface, hops}]`, for `dest` and on `iface` when given |
| `peer_tcp(addr, port)` | a TCP link to another station | |
| `current_role()` | | what it does now, `transport` or `client` |
| `lxmf.create(name)` | one more LXMF identity, `name` its display name, unless the node has one by that name; never refused | its delivery address |
| `lxmf.identities()` | | `[(name, address)]`, the one it sends from first |
| `lxmf.announce(name)` | an announce of that identity's delivery destination (none named: the first) | |
| `lxmf.send(dest, text, mid, sender)` | an LXMF message from its identity `sender` (none named: the first), `mid` sim-mesh's id for it | |

A verb with a dot is grouped, and its driver method has an underscore
(`lxmf.send` is `lxmf_send`). An LXMF identity's name is how a script names
a message's sender or recipient, so a run keeps it unique: no two
identities, and no identity and another node, share one. A node's own name
stands for the identity it sends from.

A verb a firmware cannot do is refused, naming the firmware and the verb, and
the page shows it as `! …` for that station. A driver reads the station's
console line by line and **reports** what happened, an event at the run's T:
for every message sent, `lxmf.message.status` under sim-mesh's id (`pending`,
`sent`, `delivered`, `failed`, with why), into the run's `events.jsonl`,
which is what a traffic report counts. A firmware's own ids for a message
are coupled to sim-mesh's as the driver learns them; what goes wrong before
the firmware has one is reported under sim-mesh's id directly.

A station's **role** is read live through `current_role`; one the driver
cannot read shows the node's role tag.

### What a firmware may count on

A firmware's executable is native code; it may count on the C library and
the C++ runtime of sim-mesh's image (Ubuntu 24.04, of the host's own
architecture) and brings any other shared library in its zip, found through
its node.yaml's `env` (`LD_LIBRARY_PATH: ./lib`). sim-mesh provides the rest
when it starts a station: the **virtual radio** its node.yaml names
(`radio: sx1262`), a shared library the firmware is linked with by name and
never carries, so it keeps working when the chip model or the ether's
protocol changes; the **time shim** in a virtual-time run; and a Python for
its driver. Anything else a station runs — a Python with Reticulum and LXMF
for a station that is a Python program — is in its zip.

A firmware given to a simulation (`sim new --build`, `sim run --build`,
`simd --build`) runs on every node the script gives a firmware, whatever it
gives, or with `--build-tag T` on every such node carrying the tag T. A node
the script gives no firmware still runs nothing. A simulation records the
firmware each name resolved to, and so does a snapshot; a run resumed after
that firmware was deleted resolves the name again.

## Antennas

A node carries one antenna, a type from the catalogue in
`testbed/antennas/catalogue.yaml`: generic representatives of what a
sub-GHz node (433, 868 or 915 MHz) carries, each pattern the same on all
three bands, from a bare quarter-wave wire and a spring helical to a
fibreglass collinear, a panel and a yagi, each with its description and a
picture (`testbed/antennas/<type>.svg`). The **Antennas** tab shows them and
draws each one's radiation pattern in the horizontal and the vertical plane.

A pattern is five figures: the peak gain (the kind's typical efficiency in
it), the vertical beamwidth, how far the main lobe is tilted above the
horizon (a small or drooping ground plane lifts it), the horizontal
beamwidth of a directional one, and the floor, the most it falls below its
peak. The loss below the peak is `12·((el − tilt)/vbw)²`, plus
`12·(az/hbw)²` for a directional one, never more than the floor: 3 dB at
half a beamwidth, as a half-power beamwidth says. A directional antenna is
aimed by its node, azimuth clockwise from grid north and elevation above the
horizon; an omni is the same all round.

**Between two nodes** the direction is the line between their antennas'
tips, in three dimensions: the azimuth from one to the other, and the
elevation from one tip (the ground under the node plus its height) to the
other, with the earth's curvature at the effective radius (k = 4/3) taking
its drop off. Each end's gain toward the other is added to the pair, so a
low node close under a high collinear's narrow beam hears less of it than
one on the horizon, and a yagi hears what it faces.

## Geodata

Geodata is the ground nodes stand on, one directory each,
`testbed/geodata/<name>/`, holding everything it is: renaming or deleting
the directory renames or deletes the geodata. Its `geodata.yaml` says which
of two kinds it is:

```yaml
# testbed/geodata/plain-27/geodata.yaml: synthetic ground, a flat plane at 0°, 0°
synthetic:
  terrain: flat
  exponent: 2.7                     # log-distance path-loss exponent
  extent_m: 150000                  # the square it covers, centred on 0°, 0°
```

```yaml
# testbed/geodata/berlin/geodata.yaml: a pack, whose files are this directory's
pack: .
```

**Synthetic ground** lies at 0°, 0°, and its degrees are metres by one fixed
rule on both axes, a nautical mile to the minute of arc: `x = lon · 60 · 1852
m`, `y = lat · 60 · 1852 m`. So a node's position is still latitude and
longitude, the map shows either, and a nodeset made on one synthetic ground
stands on any other. A pair's loss on flat terrain is log-distance,
`FSPL(1 m, f) + 10·n·log10(d)` (FSPL: free-space path loss); `n` is 2 for free
space, 2.7 suburban, and higher numbers bring the neighbourhoods in closer.
**New synthetic** on the Geodata tab makes one.

**A pack** is ground data compiled from public sources: terrain, clutter,
buildings, roads and places in a UTM (Universal Transverse Mercator) zone.
A pack's files sit beside its `geodata.yaml` (not committed), which says
nothing the pack does not: its extent, projection and layers come from the
pack's `manifest.json`. On a pack, a pair's loss is ITU-R (International
Telecommunication Union, radio sector) Recommendation P.1812-8 over the real
profile, as sim-mesh's planner computes it, and the map draws the pack's
ground, roads and buildings, with the notices of the sources it was built
from in its bottom corner, as OpenStreetMap's Open Database Licence (ODbL)
and Copernicus's terms ask.

**Shadowing** is two keys beside either kind, both absent by default:

```yaml
# testbed/geodata/plain-27-rough.yaml: the same ground, every pair shadowed
synthetic:
  terrain: flat
  exponent: 2.7
  extent_m: 150000
shadowing_db: 7                     # the spread of each pair's draw, dB
shadowing_seed: 3                   # which draws; 0 when absent
```

Every pair of nodes then gets a static draw of its own on top of its loss:
one standard normal per unordered pair, hashed from the seed and the two
node names, times the spread, the same both ways, in every band and for the
whole run. Two pairs at one distance need not hear each other alike, which
is what makes a hidden node or a lucky long link. It is a layer over the
tables, like a nodeset's offsets, so a new spread or seed recomputes
nothing; a pair never heard, a measured cell and a pair a nodeset states a
link for are left as they are.

**`loc_pct`**, on a pack only, is the percentage of locations its tables
are asked for, 1 to 99, and the planner's own 90 when absent. P.1812's
figure at 90 % of locations already holds the spread of losses between
locations, and shadowing over it counts that spread twice, so a pack with
shadowing wants `loc_pct: 50`, the median; the simulation's log warns of
one left at 90, or at anything but 50. The percentage is the table's own:
it is sent with every request and kept in the table's header, and a table
at one percentage is cached apart from, and never used for, another. The
coverage rasters stay the planner's own sweep at 90 %.

There are four ways of getting ground, each on the Geodata tab: a click on
an entry under **Download pre-built geodata packs** ([Indexes](#indexes)), **New
synthetic**, **Build** ([Build from sources](#build-from-sources)) and
**Import zip**. Nothing else makes or moves geodata.

The tab has three sections. **Installed geodata packs** is every geodata here,
each row with its kind, extent, the nodesets standing on it, its size on
disk, the index it came from when it did, rename and a trash can; a
checkbox begins each row, and **All**, **None**, **Invert** and **Delete
selection** above the list act on the chosen ones; a click on a row opens
it. **Download pre-built geodata packs** is what the listed indexes offer,
its sizes and icons lined up with the installed ones'. **Geodata sources**
is the build's cache, one row per source with its licence and what it holds
here, and a trash can that empties it (refused while a build runs; cached
data can be deleted freely once a pack is built, and costs only the fetch
again).
Hovering a source's name says what kind of data its files hold; under it,
where it stands in the source files and the layers it feeds with its
priority in each. Beside the list is a map: clicking a source shows where
it has data, tinted (the world for a worldwide source; for a source with a
feed, the tiles its feed lists, inside its outline; for any other its
outline), and what of it is in the cache, filled (read off the cached files'
names as its method names them: a template's tile the corner it is named by, a
feed's tile its corner, a region's file its region). Clicking either line of
the key under the map fits the map to all of that area. Clicking
the map pins a point and lists only the sources with data there, each
saying whether that spot is cached; **All sources** lists them all again.

### A sim-mesh geodata pack

How geodata goes from one machine to another: a zip holding `geodata.yaml`
at the top, its first line `# geodata <name>`, and for a pack the pack itself
under `pack/`, which the yaml's `pack:` names. Synthetic ground is the yaml
alone. **Export zip** on an open geodata's toolbar writes one; **Import
zip** takes one, or a bare planner pack (a `manifest.json` at the top or
inside one directory), which becomes pack geodata, by a name the dialog asks
(the zip's own by default).

**Nodes are never part of the ground.** A planner pack may carry a `Nodes`
layer, a deployed network baked in; the compiler never makes one, export and
import leave out its file, its manifest entry and its notice, and the page is
never told of it. Nodes belong to nodesets, which stand on any ground that
holds them.

A pack holds only data that may be passed on. The ITU-R P.1812 maps stay in
the cache: the manifest carries the two numbers taken from them (ΔN and N0),
and each source's notice.

### Indexes

```
page ── index_list ────────────────────────────► front ── GET <address> ───► the index's host
page ── index_install {index, kind, name} ─────► front ── GET <entry url> ──► the file's host
front ── index_progress {index, kind, name, fetched, of, …} ─► every page
front: size and sha256 checked; a pack expanded as Import zip expands one, a nodeset written
```

An **index** is one YAML file listing geodata packs and nodesets that can be
fetched. It is how standard ground and standard nodesets reach every
machine as the same bytes, so a test on them means the same thing on each:

```yaml
index: sim-mesh-examples              # a usable name
title: sim-mesh examples
description: |                        # what the collection is
  These examples will soon include some varied geographies and nodesets.
  …
geodata:
  - name: berlin-mitte                # the name it is installed under
    title: Berlin Mitte, dense flat city
    url: berlin-mitte.zip             # a sim-mesh geodata pack, relative to the index
    sha256: 3f1c…                     # 64 hex digits: what the file is
    bytes: 9400000                    # its size, which the page shows before fetching
    bbox: [13.36, 52.50, 13.44, 52.54]
    licences: ODbL 1.0; dl-de/zero-2.0; CC BY 4.0; Copernicus
    tags: [standard, urban, flat]
    description: …
nodesets:
  - name: mitte-40
    title: 40 rooftop nodes in Mitte
    url: mitte-40.yaml                # a nodeset file
    sha256: 9ab0…
    bytes: 5210
    geodata: berlin-mitte             # the ground it is made for
    nodes: 40
```

`name`, `url` and `sha256` are an entry's own; the rest is what the page
shows. The index's own `description` is text about the whole collection,
printed by `sim index list` with its line breaks kept, and shown under its
name on the page reflowed, a blank line parting paragraphs. **An entry is immutable**: its sha256 is what it is, and a pack or
nodeset that changes is published under a new name. **An address** is an
http(s) URL, a `file://` URL or a path on this machine; one ending in `/`
means the `index.yaml` in it, and every entry's `url` is relative to it, so
anyone can publish a directory holding an index and its files: a web site,
a release, a USB stick.

sim-mesh's own index, `sim-mesh-examples`, is always listed, from
`https://sim-mesh.net/examples/index.yaml` (`SIM_MESH_INDEX` names another):
varied geographies and nodesets that show sim-mesh and serve as reference
environments for comparing mesh protocols and firmware versions. **Add
index** lists another by its address, under the name it gives; its trash
can forgets it again, and what came from it stays installed. The list is
kept in `testbed/indexes.yaml`, one list for geodata and nodesets alike.

**Installed, an entry remembers where it came from**, in
`geodata/<name>/.origin.yaml` or `nodesets/.origin/<name>.yaml`, so its row
says the index and the page can say it is installed. A name taken here by
other ground or another nodeset is refused, and nothing is replaced: rename
or delete what is there first. A nodeset edited since it came says so. A
nodeset whose geodata the same index offers, and which is not here, brings
that geodata with it, and geodata installed brings every nodeset the same
index makes for it whose name is free here. From a shell:

```sh
sim index list                         # every listed index and what it offers
sim index add https://example.org/mesh/index.yaml
sim index delete someone-elses
sim geodata offered [SUBSTRING]        # what the indexes offer, and what is here
sim geodata add berlin-mitte           # by name from a listed index, or a pack zip
sim geodata list                       # installed, with sizes and origins
sim geodata delete [-f] NAME…
sim nodeset offered | add NAME… | list | delete [-f] NAME…
```

**Publishing** puts something installed here into an index you have
checked out, its entry written for you:

```
sim geodata publish berlin --as berlin-centre --index ../sim-mesh.net/examples
  ─► the pack exported as a zip ─► its sha256, size, extent and licences
  ─► uploaded to the GitHub release the index names (`release: owner/repo:tag`),
     made when the repository has none by that tag
  ─► the entry appended to index.yaml, which you commit and push
```

```sh
sim geodata publish NAME --index PATH [--as ENTRY] [--title T] [--description D] [--tags A,B]
sim nodeset publish NAME --index PATH [--as ENTRY] [--geodata ENTRY] [--title T] …
```

An index without `release:` gets the file beside it, its `url` relative,
for one served as a directory or carried on a stick. An entry the index has
already, or a file of that name in the release, is refused: an entry never
changes. The upload's token is `GH_TOKEN` or `GITHUB_TOKEN`; with neither
set, `sim` takes it from `gh` where it runs. sim-mesh-examples names
`sim-mesh/sim-mesh:examples`, so publishing to it takes the right to upload
there.

### Build from sources

```
page ── GET /osm/<z>/<x>/<y>.png ─────────────────► front ── (cache miss) ──► tile.openstreetmap.org
page ── GET /api/geodata/sources?bbox=&res_m= ─────► front         the grid, the sources chosen, the downloads
page ── POST /api/geodata/build {name, bbox, res_m} ► front
front ── fetch into geodata/.cache/<source>/ ──────► the sources' hosts (below)
front ── planner-job pack-build, JSON on stdin ────► planner-job
planner-job ── one JSON line per step ─────────────► front ── geodata_progress ──► every page
planner-job ── geodata/.part-<name>/ ──────────────► front: geodata/<name>/, with its geodata.yaml
```

**Build** on the Geodata tab opens a view of its own, with **← Geodata**. The map is OpenStreetMap's standard
tiles, fetched through the front and kept under `testbed/osmtiles/` a week
at least, as the tile usage policy asks; the packs there are outlined
with their names, and the outlines of the sources that do not cover the
world (the German states' own data, Germany's census grid) are tinted. A drag pans, the
wheel zooms, Ctrl or Cmd and a drag draws the rectangle, and the place
search asks Nominatim, OpenStreetMap's geocoder, once per search (on Enter),
as its policy allows.

The side panel under the rectangle has its name and resolution (30 m or
10 m), the grid's size in cells and its UTM zone, the zone of the
rectangle's centre; a rectangle wider than its zone, or a grid of more than
25 million cells, is refused with the sentence saying why.

The sources are not a choice: the build takes every source whose coverage
meets the rectangle ([Sources](#sources)), and measured data takes the
place of the worldwide data wherever it covers; a source's priority says
which one the build is said to use where several meet. With the sources
sim-mesh ships:

- **terrain and clutter**: the state surveys' 1 m terrain and their surface
  models where the rectangle touches Germany (every state), AHN's (0.5 m)
  where it touches the Netherlands,
  BEV's 1 m terrain and surface where it touches Austria, and the national
  or regional lidar models of France, Flanders, Norway, Estonia, Czechia,
  Malta, Andalucía, Catalonia, Navarra and South Tyrol (a terrain alone,
  each cell keeping its clutter, in Poland, Galicia and five more Italian
  regions), Copernicus GLO-30 everywhere else; in the United States, USGS 3DEP's
  10 m terrain under GLO-30's clutter;
- **buildings**: the same states' LoD2 models (all but Hessen's, whose
  LoD2 comes only per municipality), 3DBAG's in the Netherlands,
  OpenStreetMap's everywhere else (an OpenStreetMap building on a LoD2 or
  3DBAG tile is left out);
- **population**: the Zensus 2022 grid where the rectangle touches Germany,
  CBS's 2023 grid where it touches the Netherlands, Statistik Austria's
  2026 grid where it touches Austria, WorldPop's 2025 grid in the United
  States, none elsewhere;
- **land cover**: ESA WorldCover, with NLCD's classes over it in the
  conterminous United States;

and always OpenStreetMap roads and places, and the ITU maps. The panel lists the sources chosen, each with what it is used
for ("buildings inside its outline", "buildings outside Berlin LoD2 building
models"), what is still to fetch (what is in the cache costs nothing) and
its licence. Build is ready as soon as the sources and their files are
known; the download sizes come after, since they mean asking every file's
host (Amsterdam's 167 3DBAG tiles and 24 AHN sheets take seconds to
minutes). **Build**
says the same in a dialog, which sources go into the pack and for which
part, and goes back to the list, where the build's row shows its step and
progress and has **Cancel**; when it ends the row is geodata like any
other, or says why it failed. One
build runs at a time, as a child process the front never waits on; the
compiler's diagnostics go to `testbed/geodata/.cache/logs/<name>.log`. A
compiler killed for want of memory says so, and which limit to raise: the
Podman machine's or Docker Desktop's, where the front runs in one. What
the compiler holds grows with the rectangle, not with the OpenStreetMap
extract it lies in: a city of 15 km takes about half a gigabyte, from
Massachusetts's 310 MB extract or Austria's 812 MB alike.

**The sources** are fetched into `testbed/geodata/.cache/<source>/`, shared by every
build, so a second region beside the first fetches only what is new; a file
is fetched once, resumed where it stopped, and one its host does not have
(GLO-30 over open sea) is not asked for again. Those sim-mesh ships:

| Source | Covers | From |
|---|---|---|
| GLO-30 surface model, 1° tiles | the world | `copernicus-dem-30m.s3.amazonaws.com` |
| WorldCover 2021, 3° tiles | the world | `esa-worldcover.s3.eu-central-1.amazonaws.com` |
| OpenStreetMap extract (roads, places, sites, buildings) | the world | Geofabrik: the smallest extract whose outline holds the rectangle |
| P.1812-8 ΔN and N0 maps | the world | `itu.int`, kept here, never packed |
| DGM1, bDOM, LoD2 | Berlin | `gdi.berlin.de`'s ATOM feeds: only the tiles meeting the rectangle |
| DGM, bDOM (0.2 m), LoD2 | Brandenburg | `data.geobasis-bb.de`'s directory listings: only the tiles meeting the rectangle |
| DGM1, DOM1, LoD2 | Mecklenburg-Vorpommern | `geodaten-mv.de`'s ATOM feeds: only the tiles meeting the rectangle; the host does not resume a broken download |
| DGM1, DOM20 (0.2 m), LoD2 | Bayern | `download1.bayernwolke.de` (and `download2`), tiles named by their corner: only those meeting the rectangle, the DOM20's windows |
| DGM1 (XYZ), DOM1, LoD2 | Baden-Württemberg | `opengeodata.lgl-bw.de`, 2 km zips named by their corner: only those meeting the rectangle |
| DGM1, DOM1 (XYZ, 2017 and 2015), LoD2 | Bremen | `gdi2.geo.bremen.de`'s city zips: only the tiles meeting the rectangle, read out of them by range |
| DGM1, bDOM, LoD2 | Hamburg | `daten-hamburg.de`'s city zips: only the tiles meeting the rectangle, read out of them by range |
| DGM1, DOM1 | Hessen | `inspire-hessen.de`'s coverage service (WCS), asked for each 1 km square meeting the rectangle |
| DGM1, DOM1, LoD2 | Niedersachsen | LGLN's ArcGIS tile layers (the newest survey of each tile) and its object store: only the windows of the tiles meeting the rectangle; LoD2 tiles named by their corner |
| DGM1, DOM1, LoD2 | Nordrhein-Westfalen | `opengeodata.nrw.de`'s directory listings: only the tiles meeting the rectangle |
| DGM1, DOM1, LoD2 | Rheinland-Pfalz | `geobasis-rlp.de`'s directory listings (slow, about 9 MB each): only the tiles meeting the rectangle |
| DGM1, DOM1, LoD2 | Saarland | LVGL's district zips: only the tiles meeting the rectangle, read out of them by range |
| DGM1, DOM1, LoD2 | Sachsen | GeoSN's file shares, 2 km zips named by their corner: only those meeting the rectangle |
| DGM1, DOM1, LoD2 | Sachsen-Anhalt | LVermGeo's four state zips of each: only the tiles meeting the rectangle, read out of them by range |
| DGM1 (XYZ), bDOM (0.2 m), LoD2 | Schleswig-Holstein | `geodaten.schleswig-holstein.de`'s tile indexes: only the tiles meeting the rectangle; the host neither resumes nor says a size, and its bDOM is about 105 MB a square kilometre |
| DGM1, DOM1, LoD2 | Thüringen | `geoportal-th.de`'s ATOM feeds: only the tiles meeting the rectangle |
| Zensus 2022 100 m grid | Germany | `destatis.de` |
| AHN DTM and DSM, 0.5 m | the Netherlands | PDOK's sheet index (`service.pdok.nl`): only the windows of the sheets meeting the rectangle |
| 3DBAG buildings | the Netherlands | `data.3dbag.nl`'s tile index: only the tiles meeting the rectangle |
| CBS 2023 100 m grid | the Netherlands | `download.cbs.nl` |
| BEV ALS DTM and DSM, 1 m, 50 km squares | Austria | `data.bev.gv.at`: only the windows of the squares meeting the rectangle |
| Statistik Austria 2026 100 m grid | Austria | `statistik.at`'s INSPIRE download |
| DHMV II DTM and DSM, 1 m | Flanders | Digitaal Vlaanderen's coverage service (WCS), each 1 km square in UTM 31 |
| LiDAR HD MNT and MNS, 1 m | metropolitan France | IGN's raster map service (`data.geopf.fr/wms-r`), each 1 km square in the UTM zone (30, 31 or 32) of its part of France |
| NHM DTM and DOM, 1 m | Norway | Kartverket's coverage service (WCS), each 1 km square in UTM 33 |
| DTM 1 m, DSM 5 m | Estonia | Maa- ja Ruumiamet's coverage service (WCS), each 1 km square in UTM 35 |
| NMT 1 m (terrain) | Poland | GUGiK's coverage service (WCS), each 1 km square in UTM 34 |
| DMR 5G and DMP 1G, 2 m | Czechia | ČÚZK's image services, each 1 km square in UTM 33 |
| MDT and MDS, 1 m | Andalucía | REDIAM's file share, 2 km cloud-optimised tiles named by their north-west corner: only the windows meeting the rectangle |
| MET and MS, 1 m | Catalonia | ICGC: one cloud-optimised GeoTIFF of about 100 GB each, only its window |
| MDT and MDS 2024, 2 m | Navarra | IDENA's coverage service (WCS), each 1 km square in UTM 30 |
| MDT 2 m (terrain) | Galicia | the Xunta's image service, each 1 km square in UTM 29 |
| DTM and DSM, 2.5 m | South Tyrol | the province's coverage service (WCS), each 1 km square in UTM 32 |
| DTM (terrain): RER 2023-24 1 m, 5 m, 5 m, 5 m, 2 m | Emilia-Romagna (its 2023-24 survey), Piemonte, Lombardia, Campania, Sicilia | the regions' coverage and image services, each 1 km square in UTM 32 or 33 |
| DTM and DSM 2012, 1 m | Malta | the Planning Authority's coverage service (WCS), each 1 km square in UTM 33 |
| 3DEP 1/3 arc-second terrain, 1° tiles | the United States | `prd-tnm.s3.amazonaws.com`: only the windows of the tiles meeting the rectangle |
| Annual NLCD 2025 land cover, 30 m | the conterminous United States | `mrlc.gov`: one 1.5 GB zip |
| WorldPop 2025 population, 3 arc-seconds | the United States | `data.worldpop.org`: one 1.5 GB GeoTIFF; the host does not resume a broken download |

Geofabrik's index, Berlin's feeds and the MeshCore node list are kept in
`testbed/geodata/.cache/meta/` and asked again when a week old, or after the
`refresh_days` a source's `find` gives. A Berlin tile's name
is its south-west corner in kilometres of EPSG:25833, so a district costs
megabytes rather than the city's gigabytes. OpenStreetMap is read from one
protocol buffer file (PBF) extract, not from Overpass: one file serves roads,
places, peaks and masts and buildings alike.

### Sources

```
sim-mesh/sources/sources.yaml ─┐   global, then continent ▸ country ▸ sources
testbed/sources.yaml ──────────┴─► every source here, by id (sourcefile.py)
page ── GET /api/geodata/sources?bbox=&res_m= ─► front: for each source whose coverage
      meets the rectangle, its `find` method: the files the rectangle needs
front ── GET <file>, the next mirror when one fails ─► into geodata/.cache/<id>/
front ── planner-job pack-build: each source's files as the compiler input its
      format and layer go to ─► the pack
```

Where a pack's ground comes from is data: `sources/sources.yaml` in
sim-mesh's repository, which grows by issues and pull requests, and a
person's own beside it in `testbed/sources.yaml`, the same shape. A source
picks one of sim-mesh's methods and fills in their parameters, and never
carries code:

```yaml
global:                             # sources with data everywhere
  - id: glo30
    title: Copernicus GLO-30 surface model
    holds: Surface heights at 30 m …    # the page's tooltip
    licence: Copernicus DEM licence, attribution required
    notice: "© DLR e.V. …"              # what a pack carries
    redistributable: true               # false: only values from it enter a pack
    layers: { surface: 10 }             # each layer it feeds, with its priority there
    resolution_m: 30
    coverage: worldwide
    find:
      method: template
      url: "https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif"
      tile: "Copernicus_DSM_COG_10_{ns}{lat:02}_00_{ew}{lon:03}_00_DEM"
      size_deg: 1
      crs: EPSG:4326
    read: whole
    format: { type: geotiff, band: 1 }
europe:                             # a continent
  DE:                               # a country, by ISO 3166-1 alpha-2
    name: Germany
    sources:
      - id: berlin-lod2
        coverage: outline           # sources/outlines/berlin-lod2.geojson
        find:
          method: atom
          feed: "https://gdi.berlin.de/data/a_lod2/atom/0.atom"
          name: "(?P<x>\\d{3})_(?P<y>\\d{4})\\.zip$"   # the tile's corner in its name
          unit_m: 1000
          size_m: 1000
          crs: EPSG:25833
        …
```

- **Coverage** is `worldwide`, only under `global`, or `outline`, a GeoJSON
  polygon in `outlines/<id>.geojson` beside the file. `sim source outline ID`
  writes one from the source's own feed, or `--geofabrik REGION …` from one
  or more Geofabrik regions' together.
- **Finding**: `template` (tiles of whole degrees, EPSG:4326 or 4269, named
  by their south-west corner, or by their north-west one with
  `corner: north-west`: `{ns}`, `{lat}`, `{ew}`, `{lon}`, `:0n` padding,
  the letters upper case unless `letters: lower`; or squares of `size_m`
  in a projected `crs`, their corner named by `{x}` and `{y}` in `unit_m`,
  as BEV names its 50 km squares of EPSG:3035; the squares start at
  `origin_m: [x, y]` where their grid is offset (Baden-Württemberg's 2 km
  tiles start on odd kilometres), `{x2}` and `{y2}` name the far corner (a
  coverage service's box, Hessen's WCS) and `file` the name a tile is kept
  under when its address's last part is none), `atom` (an
  INSPIRE download feed, each tile's corner read off its file name),
  `index` (a file of footprints, GeoJSON or FlatGeobuf, or an ArcGIS
  feature layer with `index_format: arcgis`, read a page at a time, in the
  `crs` it names: each footprint meeting the rectangle is a file, its
  address the `url_property`, its checksum the `sha256_property` when it
  has one; with `tile_property` and `newest_property`, an index listing a
  tile once per survey gives only the newest of each), `zip` (the
  `archives` holding a state's or a city's tiles together, each a part of
  the whole: every archive's central directory is read by range, a tile's
  corner read off its member's name, and each tile meeting the rectangle
  fetched alone; `<archive>!<name>` is a zip stored in another),
  `regions` (Geofabrik's index, the smallest region holding the rectangle)
  or `file`. A feed may also be a web server's directory listing, its
  links relative (Brandenburg's); a link whose file name is in its query
  (`…?file=<name>`, M-V's download service) is kept under that name. Any
  address may be a list: mirrors, tried in order. A file no host has is no
  data there (`missing: error` makes it a failed build). A host that
  refuses HEAD is asked for a file's first two bytes to learn its size,
  and one that does not send its intermediate certificate is reached
  through the intermediates in `sources/intermediates.pem`. A GeoTIFF a
  template asks for that comes back as an exception page (XML, HTML or
  JSON, 200 or 400) or as anything but a TIFF is no data there: that is
  how coverage services answer outside their extent.
- **Reading** is `whole`, the file, resumed when a fetch breaks off, or
  `window`, for a regional terrain or surface as cloud-optimised GeoTIFF
  that an index, a template or a `file` finds: its directories and only
  the chunks the rectangle meets, at the coarsest level whose pixel is no
  larger than a quarter of the pack's cell, fetched by HTTP range into a
  sparse copy of the file (its `.ranges` beside it says what it holds). A
  Delft-sized pack takes about 10 MB of an AHN sheet's 330 MB, a
  Providence-sized one 8 MB of a 3DEP tile's 500 MB, an Innsbruck-sized
  one about 40 MB of each of BEV's 7 GB squares.
- **Formats**, with their parameters:
  - `geotiff` (`band`; `crs` when it is not EPSG:4326; `classes` for land
    cover, each code the file writes and the clutter class it is, a code
    not named being no class there; `nodata`, the value a file writes where
    it has none; `members` when the tiles come zipped). It feeds one
    layer. A worldwide surface is in EPSG:4326; anything else is in any
    projection sim-mesh knows: a regional terrain or surface, land cover,
    or population as people per pixel. A terrain with a surface in its
    projection is a pair, both halves measured; a terrain alone replaces
    only the ground, each cell keeping its clutter. A file may place its
    image by pixel scale and tiepoint or by a model transformation (north-up,
    unrotated, as GeoServer writes it), and a chunk it stores as no bytes is
    no data.
  - `xyz` (1 m) and `citygml`, in an ETRS89 UTM zone (`crs`, EPSG:25832
    or 25833) as the German state surveys deliver them; a pack in another
    zone takes them projected into its own. XYZ terrain and surface tiles
    of one zone are paired by the corner in their names (`dgm1_32_E_N`,
    `dom1_32_E_N`, or `dgm1_32E_N` with the zone in front), and a GeoTIFF
    surface whose source `pairs_with` an XYZ terrain is paired with it the
    same way (Baden-Württemberg's DOM1 with its DGM1). A CityGML tile's
    extent is read off its name (`LoD2_32_E_N_2_…`, the last number its
    size in kilometres), else from its buildings; a page a host appends
    after the document is not read.
  - `cityjson` (`crs`, and the attributes that hold the `ground` and `roof`
    heights): each Building's LoD0 footprint, as tall as roof less ground.
  - `csv-grid` (`delimiter`, the `x`, `y` and `value` columns, `crs`,
    `cell_m`), `gpkg-grid` (a GeoPackage of square cells: `value`, `crs`,
    `cell_m`; a negative value is withheld) and `inspire-pd-grid` (an
    INSPIRE population distribution in GML, each value naming its cell by
    the EU grid code, `CRS3035RES100mN<y>E<x>`, which gives its projection
    and size: `members`).
  - `osm-pbf` and `itu-p1812-maps`.

  A file that does not fit its format is refused when it is read, with the
  sentence saying which.
- **Projections** a source may name are EPSG:4326, NAD83 (EPSG:4269, taken
  as WGS 84's degrees), EPSG:3035, Conus Albers (EPSG:5070), UTM (WGS 84,
  ETRS89 and NAD83 zones, and the national systems that are a UTM zone
  under a code of their own: Italy's RDN2008, EPSG:7791, 7792, 6707 and
  6708, SWEREF99 TM, EPSG:3006, and ETRS-TM35FIN, EPSG:3067) and RD New
  (EPSG:28992, and EPSG:7415 for its heights). Many services reproject on
  request, so a source asks them for a UTM zone: France's, Flanders',
  Estonia's, Poland's and Czechia's models are served in UTM though
  surveyed in national systems.
- **Layers** are `surface`, `terrain`, `landcover`, `buildings`,
  `population`, `roads`, `places` and `radio-climate`, each source with its
  priority in each. Every national and regional terrain, surface and
  buildings source is 100 to GLO-30's and OpenStreetMap's
  10; Brandenburg's are 90, since its outline holds Berlin, whose own come
  first there. NLCD's land cover is 100 to WorldCover's 10.

Shipped beyond the worldwide set: Germany (every state's 1 m terrain and
surface, every state's LoD2 but Hessen's, and the Zensus 2022 grid), the
Netherlands (AHN's 0.5 m terrain and surface, the 3DBAG
buildings and CBS's 100 m population grid), Austria (BEV's 1 m terrain and
surface, and Statistik Austria's 100 m population grid), lidar terrain and
surface models in Belgium (Flanders), France, Norway, Estonia, Czechia,
Spain (Andalucía, Catalonia, Navarra) and Malta, terrain models in Poland,
Spain (Galicia) and Italy (South Tyrol with its surface, and five regions),
and the United States (3DEP's 1/3 arc-second terrain, NLCD land cover and
WorldPop's population grid).

An id is one source: a person's file may not take one sim-mesh ships (a
second address for the same data is a mirror, in the shipped entry). Every
file is read whole and checked, and the tests read sim-mesh's own the same
way, so a pull request that breaks it fails.

```sh
sim source check                     # read every source file, say what is wrong
sim source list                      # every source, where it stands, what it feeds
sim source outline zensus --geofabrik germany
sim source outline nlcd --geofabrik us-west us-midwest us-northeast us-south
```

**Buildings from OpenStreetMap** are closed `building` ways and building
multipolygons, courtyards kept as holes, written to the pack's
`buildings.jsonl` as LoD2's are, and merged into the clutter height,
`building_top` and `built_fraction` as LoD2's are. A building's height is its
`height` tag; else `building:levels` × 3 m, plus 2 m of roof unless
`roof:shape=flat`; else a default by its `building` value (3 m for a shed
or garage, 8 m for a house, 15 m for apartments, 20 m for a church, 9 m
otherwise). Each line says where its height came from (`source`: `lod2`,
`osm_height`, `osm_levels`, `default`), and the pack's `DataQuality` layer
says, per cell, whether its clutter rests on lidar, LoD2, tagged OSM heights,
OSM defaults, or GLO-30 alone.

A node's height is always above the ground under it, so the same nodes on
other ground rise and fall with it.

**The planner.** A pack needs `planner-web`, sim-mesh's own ground and
propagation server, built from the Rust workspace in `sim-mesh/planner/` (the
crates came from Sergey's planner, and keep their names). sim-mesh runs it as
a **sidecar**: one per pack in use, started by the front on a free loopback
port when the first simulation or page opens geodata on that pack, and
stopped when the last one lets go. The page reaches it as
`/planner/<geodata>/…` on port 8800. Beside it, `planner-job` is the
compiler and the node-map importer, run once per job. `sim` builds both
with cargo as it starts (in sim-mesh's image on a machine that is not Linux).
Not built, a pack is refused with the sentence saying so, and synthetic
ground works.

Geodata names no node, and a nodeset names no geodata. The medium's own
physics (the noise figure, the interference figures) belongs to the ether,
not to the ground.

## Nodesets

A nodeset is which nodes stand where, with what maximum power and antenna,
and tagged how:

```yaml
# testbed/nodesets/mitte7.yaml, abridged
nodes:
  internet: { id: 1, lat: 52.5219, lon: 13.4132, height_m: 38, height_from: assumed,
              antenna: { type: fiberglass_collinear }, tags: [transport, no-radio] }
  gw02:     { id: 2, lat: 52.5208, lon: 13.4094, height_m: 30, height_from: assumed,
              max_dbm: 27,
              antenna: { type: panel_directional, azimuth_deg: 250, elevation_deg: -3 },
              tags: [transport, tcp-peer, lxmf] }
offsets:                              # dB added to one pair's computed loss, both ways
  - { between: [internet, gw02], db: 40, note: "wall, measured 2026-09-20" }
```

A node is its position in degrees, its antenna's height above the ground and
where that figure came from (`measured`, `roof`, `raster`, `assumed`), its
**maximum power** (`max_dbm`, below), its
antenna ([Antennas](#antennas): a type, and a directional one's
`azimuth_deg` and `elevation_deg`; a node that names none carries a
quarter-wave SMA whip), and its tags. Nothing in a nodeset is said to a
station: scripts say everything, keyed off the tags ([Scripts](#scripts)).
Two tags also mean something to the page:

- a role's name, `transport` (or `router`, `repeater`): the startup script
  makes the node one, and the map draws its second ring;
- `no-radio`: the node has no radio. Every other node's radio is the one
  in `testbed/scripts/globals.py`, which the startup script sets and the
  page reads for the coverage, the links and the bands a simulation
  computes tables for.

**A node's maximum power** is its `max_dbm`, in dBm at the antenna
connector: 22 when it states none, at most 27. Every node is one board, an
SX1262. At 22 dBm or below it is a bare SX1262, whose chip puts out the
maximum itself. Above 22 dBm it is an SX1262 behind a GC1109 front-end
module, as a Heltec WiFi LoRa 32 V4 is: the chip drives the front end
through its measured transmit curve, which reaches 27 dBm at the connector,
and the front end adds 20 dB of receive gain ahead of the chip. A node
names no board: a nodeset whose node has a `board:` key is refused.
It is what a node sends at: coverage and links draw it there, and the
startup script's radio sets it (`tx_dbm="max"`); a script can still
change it with `.radio(tx_dbm=…)` or `{max_dbm}` in its lines. A station
is told its board at start (`SIM_MESH_BOARD` in its environment,
[the firmware contract](CONTRACT.md#3-what-a-station-is-given)).

**A nodeset's own setup**, `testbed/nodesets/<name>.py` beside its YAML,
is a script of declarations for what only its nodes need: mitte7's makes
`internet` a TCP gateway and connects the `tcp-peer` nodes to it. The
startup script includes it for every nodeset of the world; **Setup script**
on the Nodes tab opens it.

**What a node runs is not the nodeset's**: a script says it
(`firmware()`, [Scripts](#scripts)), so one nodeset is run on any firmware,
or on a mix of them by tag.

**A node's name is how everything refers to it**: scripts, offsets, links,
snapshots, the map, and the proxy's hostnames. **Its id is its network
identity**: it fixes the station's loopback address in the simulation's
network (node 1 is `127.16.0.5` in the front's first network) and the MAC
(media access control) address the station derives, so two nodes never
share one. A new node takes the lowest free id. Changing an id moves the
station: anything it was set up with from its old `{id}` or `{addr}` is
stale, so a station that has state is restarted, and the page says so.

**Offsets** are dB added to one pair's computed loss, both ways, on any
geodata: where a measurement says the model is wrong, and by how much, with a
note of where the figure came from. They are a layer over the loss table,
applied when the medium is given it, so the table stays the model's own and
an offset never forces a recompute.

**Links** state one pair's loss outright, where a better figure than the
model's is known:

```yaml
links:                                # optional
  - { between: [a, b], loss_db: 131.5, back_db: 133, note: measured }
```

`loss_db` is from the first node to the second, `back_db` the other way
(`loss_db` again when absent), the same in every band. A link stands in for
the model's loss and the geodata's shadowing; the antennas and any offset
still go on top, so a pair with both has the offset added to the stated
figure. It is a layer like the offsets, and a nodeset without links has no
`links:` key and is written back without one.

A nodeset is listed on the Nodes tab of every geodata whose extent holds one
of its nodes ([The Nodes tab](#the-nodes-tab)).

**Importing** makes a new nodeset of the nodes inside the geodata's extent,
from one of these sources:

- **the MeshCore map**, the node list at `map.meshcore.io/api/v1/nodes`,
  fetched by the front at most once a week into the cache under a user agent
  naming sim-mesh; repeaters and room servers, and companions when asked. An
  advert older than a year, or dated more than a week ahead, is left out:
  both are clocks never set;
- **a PotatoMesh instance**'s `/api/nodes`, by its address: Meshtastic and
  MeshCore nodes both, a position whose precision was cut on purpose left out;
- **planner sites** (`sites.csv`) and **a deployed-network CSV**;
- **any CSV**: the dialog reads its header and asks which column is the
  latitude, the longitude, the name, the height above the ground, the
  maximum power and the tags (latitude and longitude needed, each guessed
  from its header to start); comma, semicolon or tab, whichever the header
  has most of; tags split at commas or semicolons;
- **GeoJSON** Point and MultiPoint features: `name`, `title` or `label`
  names a node, `height_m` or `height_agl_m` is its height, `tags` and
  `role` are tags;
- **KML** placemarks with a point, and **GPX** waypoints (a waypoint's
  `type` is a tag);
- **a Meshtastic node list**, what `meshtastic --info` prints (its "Nodes in
  mesh:" part is read) or that JSON alone: each node with a position, named
  by its long name and tagged with its role.

A height a file gives above sea level — GeoJSON's third coordinate, a KML
altitude not relative to the ground, GPX's `ele`, a Meshtastic altitude — is
not a height above the ground, and is passed over; only a KML altitude
relative to the ground is taken. A nodeset can also be fetched from an
index ([Indexes](#indexes)).

The public Meshtastic maps are not a source: their positions are truncated on
purpose, and PotatoMesh carries the Meshtastic nodes there are. The two node
maps are read by `planner-job nodes-import`, `planner-import`'s parsers. Every
imported node stands at the height the dialog asks
(marked assumed) where the source gives none, has the default antenna and the default radio (at a
CSV's transmit power where it states one). A node from a node map is tagged
with its kind alone (`repeater`, `room-server`, `companion`, `router`,
`client`, …); one from a planner CSV with its kind, and one from a file of
points with the format and the point's own tags. Measurements (range tests, neighbour reports) are not
nodes, and are not imported.

## Scripts

A script is plain Python against sim-mesh's own library, run from its top to
its end, each call doing what it says and returning when it has:

```python
"""A study: one firmware everywhere, another where tagged."""
from sim_mesh import *

firmware = script_input("firmware", type=Firmware, category="reticulum",
                        label="Firmware for nodes not otherwise configured")
other = script_input("other", type=Firmware, category="reticulum",
                     label="Firmware for the nodes tagged other")
sim_speed("max")                                          # declarations first
nodes().firmware(firmware)
nodes(tag="other").firmware(other)
script_include("scripts/startup.py")                      # roles, radios, each nodeset's own
nodes().on_first_boot(Node.reticulum.lxmf.create())

nodes().up()                                              # the first thing done starts it
nodes(tag="transport").reticulum.lxmf.announce(spread=60)
sim_wait(600)
node("gw02").reticulum.lxmf.send("internet", "hello")
sim_pause()
```

The library's names come in three kinds: `script_…`, about the script
itself; `sim_…`, about its simulation; and **selections** of nodes,
`nodes(…)` and `node(name)`, with what is done to them as their methods.

**Inputs** are what a script asks for before it runs: `script_input(name,
type=, label=, default=, category=)` at its top level, its arguments
literals so the page can read them without running the script. Its `type`
is `int`, `float`, `str` (the default), `bool`, `Firmware`, an installed
firmware offered on the Scripts tab as a dropdown above the script of
those of its `category` (`<base>_latest` for the newest of a base, or one
by name), or `Run`, a run's name. From a shell it is `--set
firmware=<name>`. `script_input` returns the value, as its type; an input
with no value stops the script, naming it. The run keeps the values.

**Declarations** come first and are collected until the script first does
something, which starts its simulation with them:

- `sim_speed(speed)`: `"real"` (the default), `"max"`, virtual time as fast
  as the stations allow, or a number, virtual time paced at that many times
  the wall. It is the simulation's for its whole life.
- `<selection>.firmware(name)`: what those nodes run, an installed firmware
  by name or the newest of a base, `<base>_latest` ([Firmware](#firmware));
  usually an input's value. Each node runs the firmware of the last one
  said for it; a node none is said for runs nothing, grey on the map, until
  one is.
- `<selection>.on_first_boot(*rules)`: what each station is given the first
  time it boots with no state (every station of a new simulation, a node
  placed later, a station after a factory reset), after its name, the rules
  in order. A rule is lines in the station's own language, or a firmware's
  command said on the class `Node` instead of a selection, which then is a
  rule rather than done: `Node.radio(freq_mhz, sf, bw_khz, cr, tx_dbm,
  sync, preamble)` slot 0's settings (`tx_dbm="max"`, each node's own
  maximum), `Node.radio_up()` the radio started,
  `Node.reticulum.role("transport")` its role,
  `Node.reticulum.lxmf.create(name=None)` an LXMF identity named after the
  node unless `name` says otherwise, nothing when it has one by that name.
  A rule under `Node.reticulum` is nothing to a station of another category.
- `script_include(path, missing_ok=False)`: another file under `testbed/`
  run here, in a namespace of its own, every time it is included;
  `sim_nodesets()`, the names of the nodesets the world is made of.

**Every script starts from the startup script.** `script_include("scripts/startup.py")`
says, in this order: `Node.reticulum.role("transport")` to the nodes tagged
transport; `globals.py`'s radio at each node's maximum power to every node
not tagged `no-radio`; each nodeset's own setup (`for nodeset in
sim_nodesets(): script_include("nodesets/%s.py" % nodeset,
missing_ok=True)`); then `Node.radio_up()`, last because a radio reads its
settings, SUPE among them, when it starts.
A script's own first-boot lines after the include come after that. The
Scripts tab opens `startup.py`, `globals.py` and the world's nodeset setups
as tabs beside the script, so everything a station is told is on screen.

`testbed/scripts/globals.py` holds what every script shares: the channel
Berlin's Reticulum LoRa nodes announce on [rmap.world](https://rmap.world/)
(869.475 MHz, 125 kHz, SF7, CR 4/5), and `SYNC`, 0x12, the sync word every
RNode-based Reticulum firmware has fixed, so every station hears every
other. Its
`FREQ_MHZ`, `SF`, `BW_KHZ` and `CR` are also read, as written and without
running it, by the page (coverage, links) and the loss tables' band, and a
run keeps a copy of it for its analysis; they stay plain numbers.

Both kinds of rule are kept with the run, so a node placed later, or
retagged, gets what they give it. Said after the simulation has started,
they apply to it at once: a node whose firmware changes is restarted on it,
its state kept. `sim_speed()` cannot change once the simulation runs. No script
code runs inside the simulation: the rules travel to it as data.

**Selections.** `nodes(field=value, …)` selects over each node's `name`,
`id`, `tag`, `firmware`, `base`, `category`, `role`, `antenna`, `max_dbm`,
`lat`, `lon`, `height_m` and `status`, and `node(name)` is one node.
Selections are Python's own set algebra, and a combined one keeps its
methods:

| | |
|---|---|
| `nodes(tag="lora", role="transport")` | both: keywords in one call are AND |
| `nodes(tag=("lora", "tcp-peer"))` | either: a tuple, list or set is any of |
| `nodes(tag="lora") \| nodes(name="internet")` | OR |
| `nodes(tag="lora") & nodes(category="reticulum")` | AND |
| `nodes(tag="lora") - nodes(role="client")` | AND NOT |
| `~nodes(base="relay-sx1262")`, `a ^ b` | NOT, either but not both |
| `nodes(height_m=lambda h: h > 20)` | a function of the value, not in a rule |
| `nodes()` | every node |

A selection is a condition, not a list: written at a script's top, before
there is a simulation, it picks whatever matches when it is used. A rule
travels to the simulation, so its selection must be plain values, never a
function.

**What is done to a selection**, each to every node of it, waiting for them
to be up first (a node with no firmware refuses), and answering
`{node: reply}`:

- `.exec(lines, pause=0)` types lines at each node in its own language,
  macros filled in: one line, several in one string (indented as the code
  around it, blank lines and `#` comments left out), or a list. Each node
  gets its lines in order; the nodes go together, or one after another
  `pause` seconds of the run's clock apart; each answers what it printed.
- `.reset()`, `.factory_reset()`; `.up()`; `.move(lat, lon, height_m=None)`
  (one node); `.facts()`, each node's facts.
- A firmware's own commands, which each driver does its own way: `.radio(…)`
  and `.radio_up()`, every firmware's; and under `.reticulum`, for a
  selection's Reticulum nodes and nothing to the others (a selection with
  none of them refuses): `.reticulum.role(role)`,
  `.reticulum.path(to=None, dest_hash=None, iface=None)` each node's path
  table, `[{dest, next_hop, iface, hops}]`, to a node or LXMF identity, a
  destination, on an interface, or all of it; and LXMF's,
  `.reticulum.lxmf.create(name=None)`, `.identities()` each node's
  `[(name, address)]`, `.announce(name=None)`, and `.send(to, text,
  sender=None)`, a message to a node or an LXMF identity by name, answering
  its id; and under `.meshcore`, for a selection's MeshCore nodes, the
  `meshcore` verbs ([§7a](CONTRACT.md#7a-the-meshcore-category)): `.meshcore.repeat(on)`,
  `.advert()`, `.floodadv()`, `.contacts()`, `.msg(to, text)` and
  `.chan(nb, text)` answering each message's id, `.path(to)` and
  `.reset_path(to)`, `to` a contact's name; and under `.meshtastic`, for a
  selection's Meshtastic nodes, the `meshtastic` verbs
  ([§7b](CONTRACT.md#7b-the-meshtastic-category)): `.meshtastic.role(role)`,
  `.hop_limit(n)`, `.sendtext(text, to=None, ch_index=0, want_ack=True)`
  answering each message's id (`to` a node's name; none, the channel),
  `.traceroute(to)`, `.nodes()` and `.nodeinfo()`. These take `spread=`, the
  nodes spread over that many seconds.

**The simulation**: `sim_wait(seconds)`, `sim_until(seconds)`, `sim_now()`,
the run's clock in seconds since the script's simulation began;
`sim_phase((name, until), …)` for the page's estimate; `sim_snapshot(name)`,
`sim_pause()`, `sim_stop()`.

Every command that acts takes `after=` (seconds of the run's clock) and
`wait=False`, which returns at once with a future; `script_results(futures)`
is what they came to: how a script puts many things on the clock at once.
Every time is the run's, so a real-time and a virtual-time run act at the
same instants of the run.

**The scripts' log.** What a script prints and its errors go to the run's
`scripts.log` as well as to the page, each line with the run's T, the wall
clock and the script's name. `script_loglevel("commands")` adds every
command the script gives with each node's answer, and `"debug"` everything
that goes to and from the simulation; `sim_script_loglevel(level)` sets it
for every script on the simulation that says none of its own.

Lines keep these macros until a station is given them:

| Macro | Becomes |
|---|---|
| `{name}` | the node's name — `alpha` |
| `{id}` | its station id — `1` |
| `{addr}` | its loopback address in the simulation's network — `127.16.0.5` |
| `{addr:<node>}` | another node's loopback address, by name — `tcp peer add {addr:internet}:4965` |
| `{max_dbm}` | the node's maximum power at the connector ([Nodesets](#nodesets)) — `lora 0 txp {max_dbm}` |

Anything else in braces is left exactly as written. Its name and the
first-boot rules are the whole of what a station is told at its first boot:
nothing is added behind your back.

**Running one.** The Scripts tab's **Run** starts a new simulation of the
script's own on the Nodes tab's geodata and the nodeset open there, or else
the nodesets checked in its list (several merged as **Save selection as**
merges them), and goes over to its live map; or
runs it on a simulation already running, which then takes its rules, or on a
paused one, which it resumes. The script is a process of its own (`python3
-m sim_mesh.runner`) whose output is shown beside the editor and kept in the
run's `scripts.log`; from a shell:

```sh
sim run lxmf-traffic --geodata berlin-city --nodeset mitte7 --set firmware=relay-sx1262_latest
sim run ./my-study.py --geodata stralsund --nodeset fachhochschule --nodeset more \
    --set firmware=relay-sx1262_latest --set other=relay-big-sx1262_latest
sim run lxmf-traffic --sim mitte7 --set firmware=relay-sx1262_latest
sim run lxmf-traffic --resume mitte7 --set firmware=relay-sx1262_latest
```

A simulation keeps a copy of a script of `scripts/` (a file from elsewhere
only drives it), and a snapshot keeps that copy. The simulation runs on when
the script ends; one the script paused (`sim_pause()`) stays on the Simulations
tab as done, to be resumed ([Pausing](#pausing-and-resuming)). The other way round,
a script whose simulation is stopped or paused under it, or ends on its
own, stops with "simulation … went away" the next time it waits on it or
asks it anything, and exits 1. A script that stops or pauses its simulation
itself and then waits on nothing more is not stopped by it.

**`report(run_dir)`**, `def` or `async def`, runs after the script has run
to its end, and returns the run's report as Markdown. The runner writes it
to the run as `report.md` with an **ETSI compliance** section after it
(`compliance.py`, [Reading a run](#reading-a-run)), and the page's
**Report** buttons, on the script run and on the simulation's row, show it.
`sim run SCRIPT --report RUN_DIR` writes it again for a run that has
ended, reading the script's definitions without running it.

**The editor** has a tab for the script and one for every file it imports:
another script, edited as the script is, or the library's own module, to
read.

`scripts/lxmf-traffic.py` is a whole LXMF (Lightweight Extensible Message
Format) run, on virtual time, through `sim_mesh.traffic`: announce warm-up
until paths stop growing, a seeded hour of messages, a drain, a gather, with
its record written to the run as `traffic.json`, and the simulation paused.
Its messages are the `lxmf.send` verb, its identities the `lxmf.identities`
one and its warm-up `lxmf.announce`, so it runs on any firmware of category
`reticulum`, the one its input asks for. Its first-boot lines give each
station what taking part needs, an LXMF identity. Its report is the delivery
`delivery.py` counts from what the senders' drivers reported of each message
(the run's `events.jsonl`): overall, by route and radio hops and by size,
the latency, and the undelivered by the last state reported; it says so
plainly when no station had an LXMF identity to send from.

## Loss tables

A loss table is every ordered pair's path loss for one nodeset on one
geodata, in one band (433, 868 or 915 MHz), computed at one frequency in the
band; the ether adds `20·log10(f/f0)` per frame to move it to the frame's own
carrier. It is derived, never edited, and cached under
`testbed/losses/<geodata>/<nodeset geometry>/<band>.bin` (`<band>-loc50.bin`
for a pack at `loc_pct: 50`), keyed by what it depends on: the geodata's
content and the nodeset's nodes by name, their positions and heights. A
table finds a node by its name, so a renamed node's row and column are
computed again, every other pair coming from the table cached before. Ids,
antennas, firmware, radios, tags, offsets, links and a geodata's shadowing
leave it alone: they are layers put on it when the medium is given it.
[LOSSTABLE.md](LOSSTABLE.md) is the file format. A simulation computes a
table for the band `globals.py`'s carrier falls in.

- **Synthetic ground's table** is computed in-process, log-distance, in
  moments.
- **A pack's table** is computed through the sidecar's `/link.json`, one
  request per ordered pair: P.1812 where the path allows, a near-field
  model where the two are too close for it, with the model used and whether
  the first Fresnel zone is clear kept per cell. The planner judges at
  869.525 MHz, 50 % of time and 90 % of locations (or the geodata's
  `loc_pct`), so a pack has an 868 table only. The sidecar answers one
  request at a time, a few milliseconds each: 169 nodes is a few minutes.
- **Every pair is computed**, not only pairs strong enough to carry a frame,
  because a pair far too weak to decode still adds to a receiver's
  interference. Pairs more than 30 km apart, or with an end off the pack,
  are never heard.

The front computes a simulation's tables before its stations start, with
progress on the page, and copies them into the run. A nodeset the cache
has no table for starts from the cached table of the same geodata and band
that has the most of its nodes where they are now, and only the pairs
touching the others are computed: moving one of 169 nodes costs its 168
pairs, not all 14,196. A node moved during a
run has its row and column recomputed into the run's copy, never into the
cache; until it lands the ether keeps the old row. The antenna layer needs
the ground under each node, 0 on synthetic ground and the pack's terrain
there, which the simulation asks of the sidecar once per node and keeps in
the run. From a shell:

```sh
python3 testbed/losses.py --geodata berlin-city --nodeset mitte7 --band 868 --sidecar http://127.0.0.1:<port>
```

prints progress as JSON lines and the cached table's path.

## Who can hear whom

Where a node stands, on its geodata, is the whole of it unless the nodeset
states a pair's loss as a link, which then stands in for the table's: the
level a frame arrives at is

```
L = P_tx + G_tx + G_rx − loss(tx → rx) − offset − 20·log10(f / f0)
```

with `P_tx` the power the frame went out at, `G_tx` and `G_rx` each antenna's
gain toward the other end in three dimensions ([Antennas](#antennas)), the
loss the table's (or the link's), with the geodata's shadowing draw on it
when there is one, and the offset the nodeset's. Against noise a frame goes
through the chip's own stages — its preamble found, its header read, every
block of its payload decoded — each a seeded draw on one symbol error curve
per spreading factor, anchored so that the datasheet's test frame arrives 99
times in 100 at the datasheet's threshold: −7.5 dB of signal-to-noise ratio
at SF7, down to −20 dB at SF12. A frame whose preamble is not found is not
delivered at all, and that is what "out of range" means here; one that
locks but fails later is a header error or a CRC failure, and a longer frame
fails more often at the same level. So a link is in range only if it is one
the modem could actually hold, moving to a slower spreading factor really
does reach further, and a link within a dB or two of its threshold is
neither good nor dead. With `--fading-db`, every link's level also wanders
over time around the table's, and with `--rician-k` each frame takes a fast
fade of its own. Every one of those draws is keyed on the channel — the
seed, the frame's sender, start and bytes, the receiver — never on the order
of events, so two runs that differ in their routing see the same channel
for every frame they share.

Every transmission whose channel overlaps a receiver's is interference
there, whatever its spreading factor, and each receiver rules for itself: a
frame survives where it clears thermal noise by its spreading factor's
threshold and each spreading factor's summed interference by that class's
rejection figure, over every stretch of its air.
[`ether/README.md`](ether/README.md) has the whole of what the medium
decides, and [INTERNALS.md](INTERNALS.md#reception-at-the-receiver) why.

## The page

Its tabs are **Firmware**, **Antennas**, **Geodata**, **Nodes**, **Scripts**
and **Simulations**, and a status line under them all says how many
simulations and scripts are running (a simulation's name there puts the
Nodes tab on it). Right of the tabs, `geo:<geodata>` names the geodata
chosen, followed by `nodes:<nodeset>` while a nodeset is open on the Nodes
tab. Served by a simd on its own, the page is that one simulation's Nodes
tab. Served by the front, it opens on the Geodata tab, and a page that
finds the front serving another build of it (the front's `hello` names its
entry script) reloads itself.

**Everything that can be clicked is blue**: buttons are plain text, and a
row that opens something is clicked anywhere, its name in blue and its
tooltip saying what the click does. A value and its unit never part at a
line break. On a narrow page (a phone) a table's columns become one column
per row, and side-by-side panes stack, a map above its list.

**Firmware** lists the installed firmware ([Firmware](#firmware)), each by
its name with its title, its category, the hardware and radio it plays and
its version, the paused runs and snapshots that hold it, its size on disk,
and a trash can (off for one a snapshot holds); a checkbox begins each row,
and **Delete selection** deletes the chosen ones. Deleting a firmware a
paused simulation holds warns that it stops that simulation for good, and
does so. **Import zip** uploads a zip; **Download pre-built firmware**,
below the list, is what sim-mesh.net offers this machine, each added with
one click.

**Every list of things on disk works alike**: each row says how much it
takes, a checkbox begins it, and **All**, **None** and **Invert** above the
list choose rows for the list's own actions there, shown while rows are
chosen, each taking only the chosen rows it applies to after one
confirmation that says what happens.

**Antennas** lists the antenna catalogue, each with its picture and what it
is; clicking one shows its figures and its radiation pattern in the
horizontal plane (azimuth, at the horizon) and in the vertical plane
(elevation) ([Antennas](#antennas)).

**Geodata** lists the geodata with its kind, its extent, how many nodesets
have a node inside it, its size and the index it came from, and a pack
being built with its step, its progress and **Cancel** (or why it failed);
below it, what the indexes offer and the build's sources ([Geodata](#geodata)).
Nothing is chosen when the page
starts, and the world is empty. Clicking a row **chooses** that geodata:
it is shown on its own, with no nodes, scrollable and zoomable, with
**Export zip** on its toolbar, and it is the ground the Nodes tab works on,
the view shared between the two; **← Geodata** returns to the list, where the
chosen row is marked. Choosing another asks first when the nodeset being
edited has unsaved changes: save them, discard them, or stay. Each row
renames its geodata or deletes it, its pack included. A rename is refused
while a run, a snapshot or another geodata stands on its pack, whose copy
of the geodata names the pack's directory, and the page says which. A
delete takes those with it, and the nodesets that would stand on no
geodata left: the one confirmation names every run (stopping a simulation
still on one), snapshot, geodata and nodeset that goes. **New synthetic**
makes flat synthetic ground at an exponent and an extent, **Build** opens
the build view, and **Import zip** takes a sim-mesh geodata pack or a bare
planner pack ([Geodata](#geodata)).

**Scripts** lists the scripts with their first docstring line: first the
**Examples** sim-mesh ships, which are never saved over (only **Save as**
keeps changes to one, under a name of your own), then **Local scripts**,
yours, with **New**, **Save** and **Save as**. Examples and your scripts
share one set of names, and a new one may not take the name of a Python
module (`json`, `random`, an installed package), which it would hide from
every script; a script others include
(`startup.py`, `globals.py`) carries a *library* badge and is not run on
its own. It edits one (saved through the front, which checks it parses), and **Run** starts a
new simulation of its own on the Nodes tab's geodata and its nodeset (the
one open there, else the ones checked in its list, merged)
(which the dialog shows, not changes) and goes over to its live map, framed
on its nodes, or runs on a running simulation, or on a paused or done one,
which it resumes. Its output is beside the editor as it comes, with
**Stop** while it runs.

**Simulations** is the registry of **simulation runs** ([Running
simulations](#running-simulations)): every simulation, running, paused, done
or ended (every run directory under `testbed/runs/` that is none of those,
by its name). **Done** is a simulation its script paused (`sim_pause()`,
which a script says when it is finished), drawn in its own colour with a
tick; **paused** is one a person paused. Both are kept to be resumed.
Clicking a running simulation's row opens it: its live map, in place of the
list, with **← Simulations** (or the tab itself) back to the list; it stays
open while other tabs are on show, and going to the Nodes tab closes it.

```
[All] [None] [Invert]  2 chosen   [■ Stop] [❚❚ Pause] [🗑 Delete]
☐ lora     running  …  12/12   48 MB      ❚❚  ■  ⋯     🗑
☑ town     done     …  40      1.2 GB  ▶      ■     📄 🗑
☑ old-3    ended    …  40      880 MB                📄 🗑
```

Each row's actions are icons, each in a narrow column of its own so they
line up, the trash can last: **▶** resumes a paused or done one, in real
time, in a new run directory; **❚❚** pauses a running one; **■** stops one
for good (a paused or done one's kept state goes, and it is an ended run,
ended where it paused); **⋯**, on a running one, resets or factory-resets
all its stations, saves a snapshot of it, or loads one into it; the report
icon opens its report, as its script's `report` wrote it; the trash can
deletes its run directory, and a running one's warning says it is stopped
first. **Stop**, **Pause** and **Delete** above the list do the same to the
chosen rows. Each row says its run directory's size, measured every ten
seconds while it runs.
Each row shows its simulated time T and the real time it has been running
(for a stopped one, from its start to its end; T from its pause, or from the
last line of its record), and how fast T runs; a real-time run's T is its
real time, so it shows only that. An open simulation shows the same in
large figures over the top of its map. Both read as a clock,
`01:33:24`, and from a day on as `2d + 03:12`.

### The Nodes tab

It works on the geodata chosen on the Geodata tab, and lists the installed
geodata to choose from until one is. Then it is the **list** of every
nodeset with a node inside that geodata, beside a map that only shows (no
coverage, nothing to move); a nodeset with no node there is listed apart,
under **Nodesets on other geodata**:

```
Nodesets on town              New   Import
All  None  Invert  2 chosen   Save selection as   Delete selection
☑ ◯ town-core          42        6 kB  🗑
☑ ◯ meshcore-2026-09   318/402  88 kB  🗑
☐ ◯ potatomesh         77       21 kB  🗑
```

The ring is the colour the nodeset's nodes are drawn in on the map, hollow,
named on hover. **The checked nodesets are on the map together**, and when
a geodata is chosen every nodeset is checked; the map frames them as the
checks change. The size is the nodeset's file and its own setup script on
disk. The count is its nodes inside the geodata, and where some are
outside, of how many (amber): those are neither drawn nor loaded, and a
Save writes them back as they were. The bin deletes the nodeset, with its
own setup script, after a warning that this cannot be undone; with two or
more checked, **Delete selection** deletes them all.

- **New** asks a name and opens an empty nodeset.
- **Import** makes a new nodeset from a source ([Nodesets](#nodesets)), and
  opens it.
- **Save selection as**, with two or more checked, asks a name and writes
  one new nodeset of the checked ones as their files stand, earlier in the
  list first: nodes keep their tags and gain their nodeset's name as a tag;
  a name an earlier one took gets the nodeset's name appended; an id taken
  gets the lowest free one; two nodes within 5 m of each other are one
  node, the earlier one's; offsets and links come along where both ends do.
  The new nodeset is then the one checked.
- **Download pre-built nodesets**, below the list, is what the indexes
  offer ([Indexes](#indexes)), each nodeset with the geodata it is made for,
  which installing it brings along when it is not here; a click on one
  installs it, or opens it when it is installed.

**A click on a nodeset opens it**: the map then edits it, with the
selection, the tags, the editor, the links and the coverage, and its
toolbar has **← Nodesets** (or a click on the Nodes tab) back to the list,
**Save** and **Setup script**. Whatever leaves the nodeset being edited —
the list, **New**, **Import**, another geodata, another tab — asks first
when it has unsaved changes: save them (a new one is asked a name), discard
them, or stay. **Save** writes it back to its own file (amber while there
is something to save; it keeps the file's leading comment). The Nodes tab
edits nodesets and nothing else. The same map, opened from a running
simulation's row on the Simulations tab, is that run's live map: the same
edits go to the run's own copy of its nodeset (never to `nodesets/`), and
**Save nodes as nodeset** keeps them.

**The map** is one canvas, drawn in the geodata's own metres. From the
ground up:

- **ground**: for a pack, the planner's base map shaded by terrain or by
  clutter height, composed by planner-wasm from the sidecar's `tile.bin` and its
  server-side bake; on synthetic ground, a grid in metres or degrees at 1, 2
  or 5 times a power of ten, the brighter cross at 0°, 0°, and the extent's
  square;
- **roads and railways** from the sidecar's `roads.bin`, drawn as lines;
- **buildings** from `buildings.bin`: outlines under 9 km across (wider,
  the map says to zoom in to see them), filled by height above the ground
  under 4 km (slate low, sand about 20 m, red 60 m and up); a pack with no
  footprints in its `buildings.jsonl` has none. They are fetched in 1 km squares
  (smaller where one reply would be cut short by the sidecar's vertex cap),
  nearest the middle first, and kept, so every part of the view fills in
  and panning back costs nothing;
- **population**, a pack's residents per cell from its population layer, as
  a heatmap from a faint violet trace to a dense orange-yellow;
- **coverage**: the best margin over the decoding threshold at each point
  from any node on show but those tagged `no-radio`, at each node's maximum
  power, `globals.py`'s spreading factor and bandwidth, and its antenna's gain toward the point in three dimensions, in
  bands of what it is good for: **green** 21 dB and
  more (reception indoors too: 15 dB of walls on top of the edge's 6),
  **yellow** 6 to 21 dB (outdoors only), **red** 0 to 6 dB (the edge, where
  fading decides), nothing below 0 (no chance);
- **offsets** as dashed lines with their dB;
- **links**, for the one selected node and no other: every other node it
  reaches, coloured by the level it would be heard at, green to amber where
  it decodes, red where it only interferes, dashed where the first Fresnel
  zone is not clear, by the table's own figures without the nodeset's
  offsets and links or the geodata's shadowing (attached, the levels the
  ether reports for the nodes that decode stand in for those); always drawn
  while one node is selected: from the run's table, or standalone from that
  node's row and column, which the front computes from the nodes as they
  stand, saved or not, keeping each pair both ways by where its ends stand,
  so selecting the next node or moving one asks only for the pairs that are
  new (`links`);
- **the checked nodesets**' nodes, on the list's map, hollow rings in each
  nodeset's colour, named on hover;
- **the nodes**: a dot each with its name, its antenna's height above the
  ground (`20 m up`) and, smaller, its tags; a second, black ring round a
  node whose role carries others' traffic;
- **links** and **the pair inspector** take each antenna's gain toward the
  other end, as the medium does;
- **live**, attached: a ring from a transmitter for as long as the frame
  occupies the air, a flash at each receiver (green clean, red CRC (cyclic
  redundancy check) failure), the dot's colour its status (grey stopped,
  amber starting or in setup, white up, red restarting), and a dashed amber
  ring round a moved station whose row has not landed.

In its bottom corner, the sources of the ground on show, each once; a click
opens their notices in full.

**Selecting.** A click picks a node; Shift-click toggles one in or out.
Ctrl or Cmd and a drag draws a rectangle that picks what is in it, with Shift
added to the selection, with Alt taken out of it. A plain drag on a node moves
it, and every other selected node with it; any other drag pans, and the
wheel zooms about the cursor. The **tags panel** lists every tag with how many
nodes carry it, and adds the nodes carrying one to the selection, or takes
them out.

**The editor** is on the selection, one node or many. Every field shows the
value the nodes share, or `<multiple values>` where they differ, and a field
changed there changes on every selected node and no other field with it:
antenna, position (one node), height, and tags. The **antenna** is picked
from the catalogue, each with its picture and description; a directional one
has its **azimuth** (clockwise from north) and **elevation** (above the
horizon) beneath it. What a node runs is not set here: a script's
`firmware()` says it, and attached, the editor shows it, or that no rule
names the node. A node's Reticulum
role is not set here: the double ring on the map says a node carries
others' traffic, and attached, the editor shows the role the station
reports; a role tag declares it, and a script's first-boot lines can set
it. The **max power** is the node's maximum power in dBm at the antenna
connector, what it sends at when nothing declares otherwise: empty is
22 dBm, a bare SX1262; above 22 dBm the node has a GC1109 front end as a
Heltec V4 does, up to 27 ([Nodesets](#nodesets)). The **height** is the antenna's, in metres above
sea level, with its height above the ground under it beneath the box; to
its right, in small print, the **terrain** there and, when the node stands
inside a building's footprint, that building's **roof**, both above sea
level, and a click on either puts the antenna there. (The nodeset keeps the
height above the ground; on synthetic ground the terrain is 0 m.) The trash can in
its header removes the selected nodes, after asking. A node's radio is not edited here: a script's
first-boot lines set it, from `testbed/scripts/globals.py`. A tag on all of
them is filled and on some is outlined with how many; clicking it puts it on
every one, its × takes it off every one, and **add a tag** puts a new one on
all, the other tags untouched. For one node it also lists the **extra
losses** (offsets) it has, each opening its pair and removable with its ×.
**Clicking a link line** opens the pair's inspector: the planner's own
`link.json` for the two, with the antennas' heights (and an indoor end's
entry loss), the terrain profile, the table's cell beside it, and the pair's
**extra loss**, set there with a note of why. Attached,
one node's editor shows its status, live role and radio and who hears it at
what level, with **Console**, **Web UI**, **Reset** and **Factory reset**.

**Right-click** on a node: attached, **Console**, **Web UI**, **Reset**,
**Factory reset**, **Announce** and **Run command…**; on a pack, **Estimate
heights from the pack**; and **Remove this node…** (or these). The estimate
is the planner's for a node whose height nobody measured, such as an
imported map's: a roof within reach of an imprecise position with a mast on
it (`roof`), else the clutter or land class around it (`raster`); it
replaces only an `assumed` height, and a node it finds nothing for keeps its
own. On the ground: **New node here**, **New node on this roof**,
select all or none, and fit the view to the nodes. A new node takes the
lowest free id, the default antenna (a quarter-wave SMA whip) and the
default radio (869.525 MHz, SF8, 125 kHz, 14 dBm); attached, it runs what
the simulation's firmware rules give it.

**Coverage** is a heatmap, on by default: the selected nodes' coverage while
any are selected, the whole network's while none are, and the key at the
map's foot says whose. It is computed only while the tab is on show and the
heatmap is coverage. On synthetic
ground it is the log-distance formula, worked out on the page. On a pack each
node's raster is the planner's point-to-area sweep to a receiver 2 m above
the ground within 10 km, which the front has the sidecar compute one node at a
time and caches by the node's position and height (`testbed/coverage/`), so
changing power, radio or antenna redraws without asking the planner and moving a node sweeps
only that one; rasters in the cache draw at once and the rest as they land.
A redraw is worked out a slice at a time between the page's other work, so
the aim dial, the map and the editor answer while it goes on: the coverage
drawn last stays on show with **redrawing coverage…** in the map's corner,
and the new one replaces it when it is whole. Each new aim, power or view
drops the redraw before it, so turning the dial redraws once, where it stops.
Either way the antenna's gain is taken toward each point from its tip to a
receiver 2 m over the ground there, the pack's terrain under each cell
fetched with its raster, so a high collinear's narrow beam passes over the
ground close under it and an aimed panel covers what it faces.

**The display menu** (☰) says how the map is shown: what the ground is
shaded by and its roads and buildings, or the grid's units; the heatmap,
none, population or (on the Nodes tab) coverage, one at a time, with its
key at the map's foot, while everything under it but the nodes and the
selected node's links is drawn grey; and on the Nodes tab the offsets layer and whether nodes
show their names, heights and tags. The Geodata tab has the same menu without
the node entries. Each tab
keeps its own choices, per browser, and the view is remembered per geodata,
the same on the Geodata and the Nodes tab: going from one to the other keeps
the place and the zoom.

**Attached**, a panel over the map shows the simulated time T and the real
time in large figures (a real-time run only the one), with the pace and the
phase under them, and the toolbar how many stations are up; what is done to stations is on the right-click menu of the
selection (Console, Web UI and Reset for one; Factory reset, Announce and Run
command… for all selected), and what is done to the whole simulation is on
its row of the Simulations tab. Every edit is logged in the run with its T.

## Running simulations

A simulation is always a script's: **Run** on the Scripts tab starts the
Nodes tab's open nodeset, or else the ones checked in its list, on its
geodata, the script's `firmware()` rules
saying what the nodes run, so even working with the stations by hand starts
from a script, `realtime`, whose one input is the firmware. A simulation
runs the nodeset's file, so the nodeset the Nodes tab is editing has its
unsaved changes saved first. `sim new` from a shell picks geodata, a
nodeset and a script, or a snapshot, then a time mode and a firmware. The
front checks them, computes or reuses the loss tables, lays out the run
directory and starts a simd on it, which resolves each node's firmware to
an installed one and loads its driver before its stations start.

**The time** is how the run keeps it, for the ether and every station alike:

| | |
|---|---|
| `real` | the wall clock; a person is in the loop at the pace of a real network |
| `max` | virtual time as fast as the stations allow |
| `<k>x` (paced) | virtual time paced at k times the wall clock: `10x` is ten minutes of the network per wall minute, `0.5x` half speed |

In virtual time the ether owns the run's clock, T, and moves it only when
every station has nothing left to do at the T it has, so what a network does
in an hour takes the stations' own work to run and not an hour, and a loaded
host slows a run down instead of changing it. A frame occupies T for its
time on the air, a station's timers and sleeps run on T, the record and
`seq.py` are stamped with T, and simd logs the T at which a run loaded and
each station came up. How is in [INTERNALS.md](INTERNALS.md#time) and the
wire in [`ether/README.md`](ether/README.md#virtual-time). The Nodes tab,
attached, shows the mode, the pace (for `max`, the pace lately reached) and
T, and a transmission's ring lasts its time on the air at the run's pace.

In a virtual-time run what reaches a station from outside the air lands at
an instant of T, and T waits while it is dealt with: a staggered start, a
line or framed RPC query typed at a console (and the next one, typed when
the reply to it is read), a TCP (Transmission Control Protocol) write from
one station to another, and work a station spends more than a tick of the
host's time on. So with the same `--seed` and `--epoch`, two runs of the same
geodata, nodeset and script put the same frames on the air at the same T,
as long as no station works past the busy watchdog or busy-waits on the
clock; with `SIM_MESH_STRICT_TIME=1` in the environment sim-mesh runs in,
those too stop depending on the host's speed ([the firmware
contract](CONTRACT.md#5-time), *Strict time*). A station's TCP CLI or web UI reached from outside the run, and whatever a
person does, still arrives at whatever T the run has reached; a driver that
means to act at an instant of the run asks simd to wait on T first (`after`,
[below](#the-control-websocket)).

**Stations start spread over a minute**, not all at once. Two dozen firmware
processes forking in the same instant is a thundering herd against one host,
and the network it makes is worse than the load: stations that boot together
announce together, so the opening minute is a collision storm no network
powered up by hand would ever have. `--stagger <seconds>` changes the
spread; `0` starts them together. The map fills in as they come up.

### The front, and the simulation verbs

```
browser ── localhost:8800 ─────────────────────► front.py ─┬─► simd (lora)  127.0.0.1:9100
browser ── alpha.lora.sim.localhost:8800 ──────►           ├─► simd (supe)  127.0.0.1:9101
driver  ── localhost:8800/ws?sim=lora ─────────►           └─► …
```

`sim` runs the front, `testbed/front.py`, on the published port 8800. It
starts one simd per simulation behind it, each with its own loopback control
port (from 9100), ether port (from 7100), station network (a /22 from
`127.16.0.0` that no socket on the host is bound in and no other front holds
the lock of, so two fronts on one host never share one) and run directory
(`testbed/runs/<name>/`, with the simd's own log in `simd.log`). Arguments
after `--` go to every simd as they stand: `sim -- --pairwise --stagger
30`.

**A front elsewhere.** With `SIM_MESH_FRONT=host:port`, `sim` uses a front
that is already running at that address. The usual case is a build container
on the same machine, where it is `host.docker.internal:8800`. In that mode:
- the simulation verbs (`new`, `stop`, `pause`, `resume`, `list`, `plan`,
  `run`) talk to it;
- no container is started, and no front is ever started here (`sim` and `sim
  dev` refuse);
- the file verbs (`firmware`, `geodata`, …) work on the tree here, which is
  the front's own when the workspace is shared;
- a page URL the runner prints still says `localhost`, for the browser beside
  the front.

Unset, nothing changes: the variable is not passed into sim-mesh's image, and
inside it the front is always its own `127.0.0.1`.

**Simulations** lists each one: its nodeset, geodata and script, its pace
and T, the phase its driver says it is in with a bar of the plan, when it
should be done, and how many of its stations are up; a simulation that exited
says so with its last lines, and a paused or done one where T stood when it
stopped. Clicking a running one's row opens its live map.

### Pausing and resuming

```
runner ── sim_pause {name, by: script} ──► front     the script says sim_pause(): it is done
page   ── sim_pause {name} ──────────────► front     a person pauses it (the page, sim pause)
front: stop its simd (stations flushed), runs/<run>/paused/ ← the run as a snapshot
front ── sims {…, {name, state: paused, paused_by, t}} ──► every page
page ── sim_resume {name} ──► front: runs/<run>/paused/ → runs/<name>-N/, a simd on it
```

A **paused** simulation is stopped with everything a snapshot keeps (its
nodeset, script, tables and every station's state) kept inside its own run
directory, as `paused/`, instead of among the snapshots. It stays in the
registry, across a restart of the front too, until it is resumed or deleted.
**Resume** (▶) loads that into a new run directory under the same name, as a
snapshot load would, and starts it in real time, so a person can be at its
nodes: the same network as it ended, booting again, with T from 0 on a new
ether; the paused run is then listed as ended. A script run **on a paused
one** (the Scripts tab's choice, or `sim run SCRIPT --resume <name>`)
resumes it in the script's own time instead, real unless its `time(…)` says
otherwise, and its firmware and first-boot lines apply to it as to a running
one.
**Stop** (or `sim stop <name>`) deletes `paused/` and lists the run as
ended where it paused, so it no longer holds its firmware; its trash can
deletes the run directory, and with it the pause.

**Done** is the same pause, asked for by the simulation's script: a script
says `sim_pause()` when it is finished (`lxmf-traffic` does, at its end),
and the run's `run.yaml` records `paused: {…, by: script}`. The page draws
a done one apart from one a person paused (❚❚ on its row, `sim pause`) and
`sim list` says `done`; it resumes, stops and deletes as a paused one does.

From a shell, `sim`'s simulation verbs do the same (`sim help` lists
every verb); they run inside the front's container, `sim-mesh-front`.
`run` and `new` start the front in the background when nothing answers on
the port (natively, its log is `testbed/runs/front.log`). `sim run` then
opens the page on the run's live map; and a script that lacks an input
with no default, or a simulation to run on, does not run: the page opens on
the Scripts tab with the script open on what was given, its inputs filled
in, to choose the rest and Run. `--no-browser` opens nothing, and only says
the page (`page: <url>`).

```sh
sim run lxmf-traffic --geodata berlin-city --nodeset mitte7 --name lora \
    --set firmware=relay-sx1262_latest         # a script's own simulation (Scripts)
sim new pw --geodata berlin-city --nodeset mitte7 --time max --pairwise
                                               # a bare one, its ether on the pairwise rule;
                                               # prints its control address, ether, network and run as JSON
sim new --snapshot mitte7-warm --time 2x --build relay-sx1262_latest
                                               # named after what it loads: mitte7-warm
sim list                                       # each with its state (done: its script
                                               # paused it) and its run directory's size
sim plan lora warm-up=+600 traffic=+4200       # T each phase ends at; +N is N s from now
sim pause lora                                 # stopped, its state kept; listed as paused
sim resume lora                                # started again as it ended, in a new run, real time
sim resume lora --time max                     # or as fast as it goes
sim run my-study --resume lora                 # or a script on it, in the script's time
sim stop lora                                  # a paused one: its state deleted, it ended
sim run lxmf-traffic --sim lora --set firmware=relay-sx1262_latest
                                               # a script, one of scripts/ by name or a path,
                                               # on a running simulation
```

A driver never picks a port or a network for a simulation: it asks the front
for one and drives it through `ws://127.0.0.1:8800/ws?sim=<name>`.

### One simd by hand

On Linux or in sim-mesh's image, one simulation is one process:

```sh
cd sim-mesh/testbed && python3 simd.py
```

That starts the ether, the stations, the proxy and the control page, on
`http://localhost:9011/`, and loads the run its run directory holds, if it
holds one; otherwise it waits for a driver to send `sim_load`. It runs in the
foreground: Ctrl-C stops it, and everything it started. It takes `--bind`
(default `0.0.0.0:9011`), `--ether` (default `127.0.0.1:7000`), `--run` (the
run directory, default `testbed/runs/simd/`; a run loaded later that would
land on one there goes beside it as `-2`, `-3`…), `--build` and `--build-tag`, `--sidecar` (the
planner-web a pack's moved rows are recomputed through), `--stagger`,
`--net`, `--time`, `--noise-figure`, `--pairwise` or `--bench-capture`,
`--fading-db`, `--coherence-s` and `--rician-k` (the ether's receivers, its
rule, how far and how fast every link's level wanders over time, and the K
factor of a fast fade per frame; no fading of either kind unless given), and
`--no-interference` (an oracle: every frame judged against noise alone),
`--clock-ppm` (in virtual time, each station's crystal off by a draw within
that many parts per million), and `--seed` and `--epoch` (the seed the ether's welcome
carries, which in a virtual-time run also keys every station's randomness,
and the wall clock T 0 stands for; two runs of one network given both draw the
same random bytes and the same timestamps).

simd places each station on one CPU, in turn over the CPUs it may use, and
every station talks to the ether hundreds of thousands of times a run. On a
machine whose cores do not all share one last-level cache (an AMD EPYC or a
Ryzen with several core complexes), starting simd under `taskset` to CPUs that
share one keeps that talk inside the cache: a 173-station run took a fifth
less time and a seventh less CPU so, record for record the same. Whether a
run's load fits in those CPUs is the operator's to judge, so simd does not
choose them.

A simd started beside the front, or beside another, needs its own port,
ether, station addresses and run directory, because every station binds its
own address, two stations on one address are one port taken twice, and two
testbeds in one run directory would write over each other:

```sh
python3 simd.py --bind 0.0.0.0:9012 --ether 127.0.0.1:7001 --net 127.0.4.0/22 --run /tmp/run2
```

takes its addresses from `127.0.4.0/22` instead of `127.0.0.0/22`. A network
is filled one /24 at a time with hosts 5 to 254: node 1 is the network's
first `.5`, node 250 its `.254`, node 251 the next /24's `.5`. The default
/22 holds 1000 stations; a wider network holds more. The front's children
take their networks from `127.16.0.0/22` up and their ports from 9100 and
7100 up, stepping around any port already taken, so a network below
`127.16.0.0` is one no child will be given.

## Runs and snapshots

A **run** is one simulation's output, `testbed/runs/<name>/`, and the
directory its stations run in:

| Path | What it is |
|---|---|
| `run.yaml` | the geodata, nodeset and script names, the time mode, the firmware rules and what each node runs, where each firmware's build was, the ground under each node, when it started, and the snapshot it started from |
| `geodata.yaml`, `nodeset.yaml` | the geodata as it was, and the run's own copy of the nodeset: edits during the run go here, never to `nodesets/` |
| `script.py` | the script it was started with, as it was, when it had one |
| `globals.py` | `scripts/globals.py` as it was: the radio the run's analysis takes its stations to have |
| `losses/<band>.bin` | the loss tables the ether reads, the run's own copies |
| `nodeset-edits.jsonl` | every nodeset edit during the run, with its T |
| `nodes/<name>/state/` | the station's state store |
| `nodes/<name>/log` | everything the station wrote to its console, across restarts |
| `record.tsv` | every message in and out of the ether |
| `simd.log` | the simd's own log |

A **snapshot** is a moment of a run, taken with **⋯ ▸ Save snapshot as…** on
the simulation's row (or `snapshot_save_as` on the control websocket, or
`sim.snapshot`),
`testbed/snapshots/<name>/`:

| Path | What it is |
|---|---|
| `snapshot.yaml` | taken at which T, from which run, which builds |
| `geodata.yaml`, `nodeset.yaml` | the geodata and the nodeset, as the run had them |
| `script.py` | the run's script, when it had one |
| `losses/<band>.bin` | the run's tables |
| `nodes/<name>/state/` | every station's store |

A simulation given a snapshot (**⋯ ▸ Load snapshot into it…** on its row, or
started from one with `sim new --snapshot`) gets its nodeset, script and tables
back exactly as they were, without a recompute and without the planner, and
every station its store: identities, keys, paths and message history, as the
firmware keeps them. The snapshot keeps its script and its firmware and
first-boot rules because first boot comes only to a station with no state:
a factory reset after the load sets a station up as the first run did.

What comes back is what the firmware reloads at boot: a snapshot is a copy
of each station's store, so whatever a firmware keeps only in memory, or
writes to its store only now and then (a path table, say), comes back as of
its last write.

Stations keep running across a snapshot: they are flushed first and the copy
is taken while they run, which for a store that commits whole files is the
same guarantee a power cut gives a board. Logs and the record are an account
of one run and are never copied into a snapshot. A snapshot holds the
firmware that wrote its state, which cannot be deleted while it does. Runs,
snapshots, the loss and coverage caches and the installed firmware are not
committed.

## The verbs

**On one station** (the editor, or the right-click menu) **and on the whole
simulation** (the ⋯ menu on its Simulations row):

| | |
|---|---|
| **Reset** | presses reset. The process exits and comes straight back; its state is untouched, so it is the same station it was. |
| **Factory reset** | wipes its state, restarts it, and it is set up again on the empty store. Identities, keys, paths and message history go; the nodeset and the script do not. |

Across the whole simulation both are spread over the same `--stagger`
window the start uses, and for the same reason.

**Right-click ▸ Run command…** on the Nodes tab types one line at the
selected stations, and lists what each one said. A line is one firmware's
language, so the stations must all run one base; the dialog asks which when
the simulation has more than one. The macros are expanded per station.
**Right-click ▸ Announce** is the `lxmf.announce` verb, each driver doing it its
own way.

Beside the line is **spread**, in seconds. Left at 0 every station is asked at
once, which is what a question wants. Anything that *transmits* wants a
spread: two dozen stations announcing in the same instant is a collision
storm rather than an announcement, and what comes back describes the storm.
Thirty or sixty seconds across the network is enough.

### The control websocket

```
page/driver → simd   command {line, name? | names? | tag?, base?, stagger?, after?, id?}
page/driver → simd   meta {verb, args?, name? | names? | tag?, base?, stagger?, after?, id?}
simd → all pages     command_result {id, line | verb, name, results: {<station>: <reply>}, t}
driver → simd        plan {phases: [{name, until}]}
simd → all pages     clock {mode, rate, t, observed, barriers, slow_idles, plan}   once a wall second
```

Everything the page does is one JSON message on `ws://<simd>/ws`, and a
driver speaks the same messages; `testbed/simd.py`'s docstring lists them all
(`sim_load`, `snapshot_load`, `snapshot_save_as`, the `nodeset_*` edits by
node name, `levels`, the resets). `command` goes to the stations that are up
(or in setup) among those named by `name`, `names` or `tag`, or to all; they
must all run one firmware base or `base` must narrow them. `meta` is a verb
of the stations' category ([the `reticulum` verbs](#drivers-and-the-reticulum-verbs)),
each chosen station's driver doing it its own way, its reply what the verb
returned; `lxmf.send` and `path` take `to`, a node or an LXMF identity whose
address is asked of its station first, `lxmf.send` may take `from`, its
sender the same way, in place of a choice of stations, and answers each
message's id, which simd gives it; `peer_tcp` takes `to` and `port`. `stagger` spreads
the stations over that many seconds; `after` holds the whole back that many
seconds on the run's clock, so in a virtual-time run it lands at an instant
of T. Its answer is one `command_result`, broadcast to every page: the
asker's `id`, each station's reply by name (a failure as `! why`), and `t`,
the run's clock when the last reply came. `clock` says how the run keeps
time — `rate` the pace asked for (null: as fast as it goes), `observed` the
pace of the last wall second, `t` the run's clock in microseconds,
`barriers` how often T has moved and `slow_idles` how many idles have arrived
at the busy watchdog's pace ([INTERNALS.md](INTERNALS.md#time)); the
`snapshot` a page gets on connecting carries one too.

`plan` is a driver saying what the run is for: its phases in order, each
with the T it ends at in microseconds. simd keeps it until the next `plan` or
the next load, and puts it in every `clock` as `{t, phases}`, `t` being when
it was given; `phases: []` clears it. From it the page shows the phase, how
far into it the run is, and when it will be done at the pace of the last two
minutes. The LXMF traffic driver sends one once every station is up and again
whenever its warm-up runs on.

`ws://<simd>/ws?quiet=1` is a socket without `tx`, `rx`, `radio` and `levels`
— everything only a map draws, and nearly all the traffic on a busy network.
A driver wants it.

Through the front the same messages pass both ways, plus the front's own:

```
page/driver → front   sims                                       the registry, as it stands
page/driver → front   sim_new {name?, geodata, nodeset, script? | snapshot, time?, stagger?, build?, pairwise?}
front → asker         sim_new {ok, name, control, ether, net, run, time, geodata, nodeset, script, snapshot, builds}
                              or {ok: false, error}
front → every socket  losses_progress {sim, band, done, total}  while its tables are computed
page/driver → front   sim_stop {name}                            stop it, or forget one that exited
front → asker         sim_stop {ok, name} or {ok: false, error}
page → front          select {sim}                               the simulation this socket is on
front → all sockets   hello {front: true, port}                  on connecting
front → all sockets   sims {port, sims: [...], script_runs: [...], geodata_names, nodesets, scripts, snapshots}
                                                                 on a change and once a wall second
front → all sockets   script_output {run, line} · script_exit {run, code}
anything else         → the simulation named by `sim`, or the one selected
simd → socket         the simulation's own message, with `sim` added
```

`ws://<front>/ws?sim=<name>` selects a simulation from the start and
`&quiet=1` asks for its quiet stream. A row of `sims` carries the
simulation's `state` (`starting`, `running`, `stopping`, `exited` with its
`code` and last lines in `tail`), the `geodata`, `nodeset`, `script` or
`snapshot` it loaded, `time`, `mode`, `rate`, `t`, `pace` (T per wall second
over the last two minutes), station `counts` by status, its `plan`, the
current `phase` `{name, from, until, eta}` and `eta`, the wall time the last
phase should end, and where it is: `port`, `ether`, `net`, `run`; a paused
one's `paused_by` (`script`: done); and `bytes`, its run directory's size.
`GET /api/sims` is the same as JSON.

The simulation verbs: `sim_new`, `sim_stop {name}`, `sim_pause {name, by?}`
(`by: script` is its script's pause, done), `sim_resume {name, time?}`, and
`run_delete {run, stop?}`, a run's directory gone, refused while a
simulation is on it unless `stop` asks for that to be stopped first.

The front's editor verbs are answered to the asking socket as `{type, ok,
…}`, and need no simulation running:

| Verbs | |
|---|---|
| `firmware_list` | the installed firmware, each with what holds it (the paused simulations among them by name) and its size |
| `firmware_prebuilt`, `firmware_add {url}` | what sim-mesh.net offers this machine, and one of those installed |
| `firmware_delete {names, stop_paused?}` | firmware removed, `{deleted, stopped}`; refused for any a snapshot holds, and for any a paused simulation holds unless `stop_paused`, which stops those simulations for good first |
| `antenna_list` | the antenna catalogue, each with its picture |
| `geodata_list`, `geodata_open`, `geodata_close`, `geodata_new`, `geodata_save`, `geodata_save_as` | geodata, each listed with how many nodesets have a node on it, its size (`bytes`) and the index it came from (`from_index`); opening a pack holds its sidecar for the socket |
| `geodata_rename {name, to}` | another name, with its own pack; refused while a running simulation, a run, a snapshot or another geodata stands on its pack |
| `geodata_delete_plan {names}`, `geodata_delete {names}` | what deleting the geodata takes with it, `{geodata, runs, snapshots, nodesets, running}`: what stands on their own packs, the runs of the simulations on any of it (`running` those still running, which are stopped), and the nodesets with a node on it and on no geodata left; and the delete, all of that and the geodata, packs included, `{deleted}` |
| `geodata_sources`, `geodata_source_clear {source}` | the build's cache, one row per source with its licence and size, and one source's emptied (refused while a build runs) |
| `index_list`, `index_new {address}`, `index_delete {name}` | the listed indexes, each entry saying whether it is here; another listed; an added one forgotten |
| `index_install {index, kind, name}`, `index_cancel {index, kind, name}` | an entry installed, answered when it is in, its progress to every page as `index_progress`; a fetch stopped |
| `nodeset_list {geodata?}`, `nodeset_open`, `nodeset_new`, `nodeset_save`, `nodeset_save_as`, `nodeset_delete` | nodesets, each with its size and the index it came from; with `geodata`, each row says how many of its nodes stand on it (`inside`); a delete takes the nodeset's own setup script too |
| `nodeset_import {name, source, …}` | a new nodeset from the MeshCore map, a PotatoMesh instance, `sites.csv`, a deployed-network CSV, or a file of points (`csv` with its `columns`, `geojson`, `kml`, `gpx`, `meshtastic`) |
| `nodeset_merge {name, layers}` | Save selection as: the nodesets checked on the Nodes tab, earlier in its list first, as one new nodeset |
| `script_list`, `script_open`, `script_new`, `script_save`, `script_save_as` | scripts, checked to parse, each with its inputs |
| `script_run {name, sim \| resume \| geodata, nodeset, inputs?}`, `script_stop {run}`, `script_log {run}` | a script as a process, its inputs given, and its output |
| `snapshot_list`, `losses_compute` | the snapshots, each with its size, and a nodeset's tables |
| `coverage {geodata, nodes}` | each node's pack raster: cached ones at once, the rest as `coverage_tile` messages as they land |

and its HTTP side: `POST /api/firmware/add?name=<zip name>` and `POST
/api/geodata/import?name=<geodata>` take a zip as the body, and `GET
/api/geodata/export?name=` sends one; `GET /api/geodata/sources?bbox=&res_m=`
says what a build takes and which sources it chose, `POST /api/geodata/build` starts one and `DELETE` on it
cancels it, its progress going to every page as `geodata_progress`; `GET
/osm/<z>/<x>/<y>.png` and `/api/nominatim?q=` are OpenStreetMap's tiles and
geocoder through the front; `GET /api/table?path=` serves a loss table and `GET
/api/coverage?geodata=&key=` a coverage raster; `/planner/<geodata>/…` is the
pack's sidecar. `front.py`'s docstring has every field.

## Reading a run

Every analysis tool reads a run directory, and takes names, positions, radio
settings and levels from it — the run's nodeset (its tags: a role tag the
node's role, none a `client`, and `no-radio`), the run's copy of
`globals.py` (every other node's radio, at its maximum power), its geodata, the firmware each
node ran and its own loss tables with the links, shadowing, antennas and offsets on them —
never the files as they stand now. What a frame means is a protocol's, under
`testbed/sim_mesh/<protocol>/`, found by each node's firmware category; the roles are
what `airtime.py --roles` and the hop counts through forwarding stations use.

| Tool | Says |
|---|---|
| `seq.py RUN` | the record as a sequence diagram (below) |
| `compare.py RUN_A RUN_B` | two runs side by side: milestones, per-station counts, paths; one run's figures alone |
| `airtime.py RUN` | airtime per station, per carrier and per frame kind; transmit power; exchanges at reduced power; CRC losses; with `--busy` the calling channel's occupancy where each station stands, with `--roles` airtime per role |
| `links.py RUN` | link geometry: distance of every usable one-way link, neighbours, hop diameter, beside what the run's loss table says would decode; with `--power`, traffic-channel power against distance |
| `delivery.py TRAFFIC.json RUN` | an LXMF traffic run's delivery (the `traffic.json` `scripts/lxmf-traffic.py` writes) from what the senders' drivers reported (`events.jsonl`): by route hops, radio hops (by the run's loss table), size, latency |
| `compliance.py RUN` | which nodes went over their EN 300 220 budgets: per node and band entry, seconds on the air in the worst sliding hour against the entry's duty cycle (or polite spectrum access where the entry permits it: 100 s an hour per 200 kHz, frames up to 1 s, 100 ms apart on one carrier), and highest e.r.p. (power + antenna gain − 2.15 dB) against its limit; frames on spectrum no entry allows. The section every report ends with; `--json` for the figures |
| `referee.py --run RUN` | how the stations behaved on the air: every transmission that began over a frame on its carrier its sender could decode, whether the ether had told the sender of that frame (at its start, mid-frame, never) and in which window of it (its first four symbols, its preamble, its payload); the frames nobody was told of, by sender and channel; every reception that ended `crc`, each overlap split by whether the two senders could hear each other. Run by hand, not part of `report.md`; `--detail` for every event, `--json` for all of it |

`seq.py` draws a run's `record.tsv` — one lifeline per station, one arrow
per station that heard a frame, the verdict at each arrow head, and the
Reticulum packet read out on the right:

```
$ python3 seq.py runs/lora --tail 4
   t (s)   delta    india    kilo     mike     papa    sierra
   0.118     │        ◀────────┼────────┼────────┤        │  ANNOUNCE  single/4e3874cc  of rnstransport.probe  hops=0  167B
             │        │        │        │        ├────────▶
   0.250     ✗────────┼────────┼────────┼────────┼────────┤  ANNOUNCE  single/edec275b  of rnstransport.probe  hops=0  167B
             │        │        │        │        ✗────────┤
```

One transmission heard by two stations is two arrows on two rows, sharing the
timestamp and the reading: every arrow has one end at the station that
transmitted, so nothing in the picture can be read as a frame travelling
between two stations that cannot hear each other. `--only <words>` keeps the
rows whose reading matches, and `--record <file>` reads a record on its own
(its lifelines are then ids, or `--names 1=alpha,2=bravo`). In a run with no
Reticulum station, a frame reads as its length and carrier.

## A station's own doors

A station whose firmware serves a web UI serves it on the port its driver
names (`web_port`), on its own loopback address, which is invisible outside
the machine or image sim-mesh runs in; the proxy on port 8800 routes by
hostname, the simulation being the second label (a station whose firmware
has no web UI is refused with a sentence saying so, and has no **Web UI**
button):

    http://alpha.lora.sim.localhost:8800/    by name
    http://1.lora.sim.localhost:8800/        by id

A bare `alpha.sim.localhost` reaches the one running simulation while there
is exactly one; with more it is refused with their names. Chrome and Firefox
resolve any `.localhost` name to loopback with no configuration. Safari does
not, and needs entries in `/etc/hosts` on the Mac:

    127.0.0.1  alpha.lora.sim.localhost bravo.lora.sim.localhost

Port 8800 is the testbed's own: where `sim` runs sim-mesh in its image, the
container publishes it at the same number on the host. It is clear of the
ports spangap holds (9000–9011), the planner's 8787, and the per-simulation
control and ether ports the front counts up from 9100 and 7100, so a testbed,
a flashmon and a dev server can all run at once.

A station's web UI is the **whole** UI, not a static shell: it speaks the same
WebRTC DataChannel to the browser that a board does, from the same firmware
source, so the live panes — settings, the log, the CLI, Activity — all work.

```
browser ──ws  alpha.lora.sim.localhost:8800/webrtc──► front ──ws──► simd ──ws──► alpha   signalling
browser ──udp localhost:8800─────────────────────────► front ──udp─► simd ──udp─► alpha   the channel
```

The DataChannel is UDP and the station's own address is out of the
browser's reach, so the signalling passes through the front and the simd,
each of which points the station's SDP (session description) answer at
itself, and each relays the UDP behind it, picking the station out of each
packet by the ICE (interactive connectivity establishment) ufrag it saw in
that answer. Port 8800 is published on **UDP as well as TCP** for it
([INTERNALS.md](INTERNALS.md#the-webrtc-relay-and-why-it-is-a-relay-rather-than-a-second-transport)).
Neither end knows. The station is answering ICE from a peer that happens to
be a relay, and the browser is talking to a station that happens to be
simulated — which is the point: the code under test is the shipping code,
on both sides.

Besides the map, a station is reachable three other ways:

- **Console** — its serial console, in a terminal window over a websocket:
  whatever the firmware prints, and whatever a person types at it, as on a
  board on a cable.
- Its firmware's own door, which its driver uses: a framed RPC channel on
  the console (below), a tool run against a port the station makes, a
  settings file and a restart — whatever the firmware has.
- `tail -f runs/<sim>/nodes/<name>/log` — everything it has printed, across
  restarts.

### Framed RPC on the console

```
station → simd   "… serial] framed rpc v1"                 once, early in boot, as log text
simd → station   F5 53 47 01 <id> <len:2> show s.net.hostname
station → simd   F5 53 47 01 <id> <len:2> s.net.hostname = alpha
simd → station   F5 53 47 01 <id'> <len:2> <a command line>    one frame per question
station → simd   F5 53 47 01 <id'> <len:2> <what it printed>
```

A firmware may multiplex a framed side channel onto its serial console
([`spangap-core/docs/framed-rpc.md`](../spangap-core/docs/framed-rpc.md)), and
a station's console here is its pty. A frame is never echoed, never enters the
line editor and never turns the log into a CLI session, so a driver asks a
station things while a person types at its **Console**. The pty drain takes
each reply frame out of the stream and passes every other byte on unchanged,
so the log and the console window never see one. A driver speaks it through
`rpc_ready` and `rpc_query` (`sim_mesh.driver`).

A station answers framed RPC once it has printed the marker since it last
started and answered the probe with something. A station that prints no
marker within the driver's wait is sent the probe once, blind, and a Ctrl-C
after it if it does not answer; that undoes it on firmware that does not
speak frames.

The firmware runs one frame at a time, bounds each at five seconds, and cuts a
reply that outgrows its buffer at the last complete line without saying so.
So one frame is in flight per station. The id is a hash of the command, so a
retry carries the one it had, and a reply that arrives after its query gave
up answers the retry. A command line of up to 4096 bytes runs over a frame;
a longer one is refused with `rpc: command over 4096 bytes`.

A station that exits is started again, because a restart on this target is a
process exit: a station rebooting itself comes back on the same address with
the same directory.

## Building a firmware zip

A firmware project makes its own zip: its station built for Linux, against
the virtual radio's header (`radio/include/simradio.h`) and linked with the
radio's shared library by name (`-lsimradio-sx1262`, in `radio/build/` once
`sim` has started, or a copy built from `radio/` with the firmware's own
compiler when it is built for another architecture), never carrying the
library itself; its driver, a Python module
subclassing its category's driver class; anything else it runs, and the
shared libraries beyond the C library and C++ runtime, under `lib/`; and a
`node.yaml` naming them. [The firmware contract](CONTRACT.md)
is the whole of what a zip must be and what a station is given, and how to
build against the virtual radio. A firmware built on Portduino links
[`radio/portduino/`](radio/portduino/README.md), which puts the virtual radio
under it in place of spidev and libgpiod.

`sim firmware add` installs the zip, and `<base>_latest` runs the newest
one added, so a project's loop is: build, make the zip, add it, run.

### Publishing firmware

Pre-built firmware is the `firmware` release of sim-mesh's own repository,
which sim-mesh.net's site serves under `/firmware/` with an `index.html` that
lists every zip with its `node.yaml`'s facts:

```sh
sim firmware publish ZIP…                   # add or replace these, drop their older versions,
                                            # then redeploy the site
sim firmware publish --keep-older ZIP…      # the same, leaving the older versions listed
sim firmware publish --delete NAME…         # take these off it
sim firmware publish --dry-run ZIP…         # say what it would do
```

```
sim ── GitHub's REST API, the token of the gh logged in here ──► sim-mesh/sim-mesh, release `firmware`
        the zips, then firmware.yaml and index.html               (each zip's node.yaml facts)
sim ── repository_dispatch firmware-published ──► sim-mesh/sim-mesh.net, which redeploys
```

It runs where every `sim` verb does, in sim-mesh's image, so it needs
nothing installed beside `sim`; the token is the one of the `gh` logged in
where `sim` runs (or `GH_TOKEN`), with the right to upload to
sim-mesh/sim-mesh and to dispatch to the site's repository. A zip goes up
under the name its node.yaml gives it, whatever its file is called, so a
build catalogue's zip publishes as it is. A zip of a
name already listed is replaced, and every listed firmware of the same base
and arch with an older version of the same scheme comes off the list with it,
unless `--keep-older`. `--repo` and `--site` name others.

## The firmware contract

What passes between sim-mesh and a firmware is specified in
[CONTRACT.md](CONTRACT.md): the zip and its `node.yaml`, what a station is
given when it starts, time, the driver and the categories' verbs, the
ether's protocol, the virtual radio, and how to build against it. A firmware
that keeps to it runs on sim-mesh with nothing of it in sim-mesh.
sim-mesh.net publishes the same file as *Building firmware for sim-mesh*.

## After a restart

Stations are processes, not a service: stopping `sim` stops them. Their
state is not in the process though — `runs/`, `snapshots/` and `firmware/`
are all in the directory sim-mesh was cloned into, which the image mounts
from the host. A simulation's stations keep their state in its
run directory, but a new simulation starts factory-fresh; to carry a network
across a restart, save a snapshot before stopping, then after `sim` load
it into a new simulation (⋯ ▸ Load snapshot into it…), and it comes back with its names, radio
settings, identities and message history as they were.

## The pieces on their own

The medium is its own program with its own docs:
[`ether/README.md`](ether/README.md) for what it does and the wire it
speaks, [`ether/INTERNALS.md`](ether/INTERNALS.md) for how. It runs alone
against hand-written stations, taking its nodes from a nodeset and its
losses from a directory of tables:

```sh
python3 ether/ether.py --bind 127.0.0.1:7000 --record record.tsv \
        --geodata testbed/geodata/<geodata>.yaml --nodeset testbed/nodesets/<nodeset>.yaml \
        --losses <dir holding 868.bin>
```

The proxy also runs on its own, for a station set started some other way; alone
it routes by station id only, since names are the nodeset's:

```sh
python3 testbed/proxy.py --bind 0.0.0.0:8800
```

## The virtual radios

`radio/` is the virtual SX1262 and the station's link to the ether, the
shared library `libsimradio-sx1262.so` behind a C ABI (application binary
interface, `radio/include/simradio.h`): open the link, open a chip per radio
slot, hand it SPI frames, pulse its reset, read its lines, and, in a
virtual-time run, read node time, set wakes, learn every move of T and say
the station is idle. A station of any language links it by name in place of
a radio, below an unchanged driver, and sim-mesh provides it when it starts
the station. The radio is one way of speaking the ether's protocol, which a
firmware may also speak itself; more radios (an LR2021) are more libraries,
each named after its chip. Beside it,
`radio/shim/simclock.c` builds `libsimclock.so`, the preloaded library that
answers the C library's clocks and waits in node time, draws the station's
randomness from the run's seed, counts the console and TCP bytes the ether
makes into instants of T, and keeps the busy watchdog off a station that is
computing ([INTERNALS.md](INTERNALS.md#time)).

The model reaches its host through services — a clock, one-shot timers, a
recursive lock, a UDP socket, a reader, a log. The library's own are a plain
process's (`radio/backend/posix/`: `std::thread`, `CLOCK_MONOTONIC`, a
`std::recursive_mutex`); a host with a scheduler of its own (FreeRTOS on
ESP-IDF's Linux host target) hands it its own with `simradio_set_services`
before anything else, from its own code.

`sim` builds them as it starts (`radio/build/`: `libsimradio-sx1262.so`,
`libsimclock.so`); a station is given the radio from there, and a virtual
run preloads the shim from there.

## Working on the page

The page is built when `sim` starts, so an edit to it shows only after a
restart. `sim dev` instead runs the page's development server (`quasar dev`)
inside, beside the front, and the front passes the page through from it on
the same port, 8800, live-reload included: each edit is in the browser at
once. The front's own Python is read when it starts, so a change to it still
wants a restart. In a container the server looks for edits itself, since
edits made outside it arrive as no file events.

With the site, sim-mesh.net, checked out beside sim-mesh, `sim dev` also
serves it from Jekyll at `http://localhost:4000/`, through the gem GitHub
Pages builds it with, each edit reloaded in the browser as it is saved. Its
gems go into `~/.cache/sim-mesh/site-gems`, the first time it starts.

## Tests

None needs firmware, a planner or a network:

```sh
cd sim-mesh/testbed && python3 -m pytest -q      # the stores, firmware, the front, simd, the drivers, the library, the tools
cd sim-mesh/ether   && python3 -m pytest -q      # the medium and the conductor (built first), over real UDP and in-process
cd sim-mesh/radio   && python3 -m pytest -q tests  # the chip model, the conductor, the time shim
cd sim-mesh/testbed/ui && npx vue-tsc --noEmit && npx quasar build
```

No testbed test reaches a host outside this machine: one that fetches
serves the files itself on loopback, and `testbed/conftest.py` fails any
lookup of another host at once, naming it.

The testbed's tests run stand-in firmware (`testbed/stub_firmware.py`): a
shell script for a station and a driver that writes down what it is asked.
The model's tests load `libsimradio-sx1262.so` with ctypes, drive it frame by
frame the way a driver does, and play the ether on a UDP socket of their
own; the conductor's tests do the same in virtual time, and the shim's run a
small C stand-in station (`radio/tests/standin.c`), linked with the radio by
name as a firmware is, under `libsimclock.so`; the Portduino idle's
(`radio/tests/idle_standin.cpp`) is compiled with `radio/portduino/idle.cpp`
by the host's g++, with the link wraps and without, and run on the wall
clock. The
testbed's pack tests run against a real `planner-web` when it is built in
`planner/` and there is `berlin-city` geodata, and are skipped otherwise.
The builds from sources and the node-map imports are tested against a host and
a `planner-job` of the tests' own; the planner's tests of real data read the
download cache, `testbed/geodata/.cache/`, and skip what is not there, and two real builds
of a few square kilometres of Berlin run through the binary with

```sh
cd sim-mesh/planner && cargo test --release -p planner-job -- --ignored
```

`SIM_MESH_MESHCORE_SNAPSHOT` names a saved MeshCore node list for the importer's
tests, in place of `testbed/geodata/.cache/meshcore/nodes.json`.

## Where the code lives

| Where | What |
|---|---|
| `sim` | the one command: the front natively or in sim-mesh's image (building what is stale first), `new`, `stop`, `pause`, `resume`, `list`, `plan`, `run`, `firmware` |
| `Dockerfile` | sim-mesh's image, which everything runs in: the system a firmware may count on |
| `radio/` | the virtual SX1262 and the station's UDP link to the ether, as a shared library |
| `radio/src/conductor.cpp` | the station's side of virtual time: T, node time, wakes, the idle |
| `radio/shim/simclock.c` | `libsimclock.so`, the C library's time in node time (its sleeps, descriptor waits, condition and semaphore waits), the seeded randomness, the console and TCP counts, the listening sockets and the watchdog's hold; `radio/include/simclock.h` is what it is handed |
| [`radio/portduino/`](radio/portduino/README.md) | the virtual radio for a Portduino firmware: a PlatformIO library standing in for spidev and libgpiod, the firmware's idle wait and the sockets that end it |
| [`ether/`](ether/README.md) | the medium: the loss tables, who hears a frame and how it comes out; `slt.py` reads and writes a table |
| `testbed/front.py` | several simulations behind one port: the registry, the editors' verbs, the imports, the planner sidecars, the loss tables before a start, one simd per simulation, script runs, coverage, station hostnames by simulation, the WebRTC relay one level up, the finish estimate |
| `testbed/simd.py` | one simulation: the ether, the stations and their setup, the proxy, the control server, commands and verbs on chosen stations, a moved node's row |
| `testbed/simctl.py` | behind `sim new`, `stop`, `pause`, `resume`, `list` and `plan`: the front from a shell; starts the front when none answers; done for a script's pause |
| `testbed/store.py` | where geodata, nodesets, scripts, tables, coverage, runs and snapshots live, what a name may be, and what a row takes on disk |
| `testbed/indexes.py` | indexes: reading one, the listed ones, installing an entry by its sha256 and remembering where it came from; `sim index`, `sim geodata`, `sim nodeset` |
| `testbed/firmware.py` | installed firmware: names and `_latest`, adding a zip, what holds one, deleting, the pre-built index, publishing onto it (the `firmware` release, and the site redeployed); `sim firmware` |
| `testbed/drivers.py` | a run's firmware resolved, each one's driver imported from its own directory, and the contract's environment |
| `testbed/antennas.py`, `testbed/antennas/` | the antenna catalogue and pictures, a pattern's gain by direction, a pair's gain in three dimensions |
| `testbed/geodata.py` | geodata: packs and synthetic ground, the projections, the extent, a sim-mesh geodata pack's export and import |
| `sources/sources.yaml`, `sources/outlines/` | the sources sim-mesh builds packs from, as data, and their outlines |
| `testbed/sourcefile.py` | the source files read and checked; `sim source` |
| `testbed/sources.py` | a build's sources: the finding methods, what a rectangle needs of each, the download cache and its fetches (mirrors included), each source's area and cache for the map |
| `testbed/crs.py` | the projections a source may name: their proj strings for the compiler, and RD New both ways for planning |
| `testbed/fgb.py` | a FlatGeobuf file's features: properties and polygon rings |
| `testbed/cogwindow.py` | a window of a cloud-optimised GeoTIFF: its directories, the level and chunks a rectangle needs, the sparse copy and its `.ranges` |
| `testbed/packbuild.py` | one pack built from its sources: fetch, each source's files to the compiler input its format and layer go to, `planner-job pack-build`, the pack into place |
| `testbed/nodeset.py` | nodesets: nodes, their maximum powers, antennas and tags (a role tag, `no-radio`), offsets, links, edits, the geometry hash, the merge of several nodesets, the imports (the planner's CSVs, any CSV, GeoJSON, KML, GPX, a Meshtastic node list) |
| `planner/` | the Rust workspace: `planner-web` (the sidecar), `planner-job` (a pack's build, a node map's import), `planner-pack` (the compiler, OpenStreetMap from a PBF extract), `planner-buildings`, `planner-import`, and the ground, propagation and coverage crates |
| `testbed/script.py` | scripts: listing, checking, loading, a script's inputs read without running it |
| `testbed/sim_mesh/library.py`, `testbed/sim_mesh/select.py` | the script library: `script_…`, `sim_…`, and `nodes()`/`node()` selections with what is done to them (`.firmware`, `.on_first_boot`, `.exec`, `.radio`, `.reticulum…`, `.meshcore…`, `.meshtastic…`), `Node` for first-boot rules, and `scripts.log` |
| `testbed/losses.py` | a loss table, on synthetic ground or through the sidecar; the cache; links, shadowing, antennas and offsets as layers, the ground under each node; one node's row |
| `testbed/coverage.py` | a node's coverage raster on a pack, through the sidecar, cached |
| `testbed/runs.py` | a run directory, and snapshots taken from and loaded into one |
| `testbed/stations.py` | one firmware process, its pty, its log, its supervisor; the thread every station's pty is read on; its console's lines to its driver |
| `testbed/rpc.py` | framed RPC on a station's console pty: the demultiplexer in the drain, and the client a driver speaks |
| `testbed/proxy.py` | the hostname proxy |
| `testbed/webrtc.py` | the WebRTC relay: the signalling rewritten, and one UDP port in front of every station's DataChannel |
| `testbed/ui/` | the page (Quasar 2 on Vue 3; Pinia stores `catalog`, `geodata`, `nodes`, `sim`, `coverage`, `display`, `socket`); `vendor/planner-wasm` is the planner's built planner-wasm, copied in by `vendor/update-planner-wasm.mjs` so the page builds with no planner beside it |
| `testbed/seq.py`, `compare.py`, `airtime.py`, `links.py`, `delivery.py`, `compliance.py`, `referee.py` | the analysis tools ([Reading a run](#reading-a-run)) |
| `testbed/sim_mesh/` | the library: `library` (what a script says, synchronously), `select` (`nodes()`), `driver` (what a firmware's driver is, and what sim-mesh hands it), `traffic` (the LXMF traffic driver), `sim` (the async hold on a simulation the library runs on), `runner` (a script run, its simulation started, its report), `view` (a run opened for analysis), `record`; `sim_mesh/reticulum/` holds Reticulum's parts: the category's driver interface, frame reading (Reticulum packets, SUPE), delivery analysis; `sim_mesh/meshcore/` and `sim_mesh/meshtastic/` the `meshcore` and `meshtastic` categories' driver interfaces |
| `testbed/boards.py` | the one board, an SX1262 with a GC1109 front end above 22 dBm; a node's maximum power; what a station is told of it |
| `testbed/scripts/` | the scripts: the examples (`script.EXAMPLES`) `realtime.py`, `lxmf-traffic.py`, `meshtastic-check.py` and `startup.py`, which every script includes; `globals.py`, the settings they share and the page reads; and your own beside them |
| `testbed/geodata/`, `testbed/nodesets/` | your geodata and nodesets (not committed) |
| `testbed/testdata/` | what the tests stand on: the four stations `four.yaml` on the synthetic ground `plain-27.yaml` |

Nothing above the bus is aware of any of it: a firmware's LoRa driver, its
channel access and airtime accounting, its protocol stack and its web UI are
the same code that runs on a board, over a bus that ends in the virtual
radio.

## License

sim-mesh is released under the Apache License, Version 2.0; see [LICENSE](LICENSE).
