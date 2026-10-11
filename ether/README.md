# The ether — the medium between simulated stations

One UDP (User Datagram Protocol) endpoint that carries LoRa frames between
stations. A station tells it
what its radio is doing; when one transmits, the ether works out who hears it
and how loudly, starts their reception and closes it out at the right instant.

It is the medium and nothing else. It holds no firmware, speaks no Reticulum,
and knows a frame only as a carrier, a duration and a payload it never opens.

```sh
python3 ether.py --bind 127.0.0.1:7000 --record record.tsv \
    --geodata <geodata.yaml> --nodeset <nodeset.yaml> --losses <dir> \
    [--noise-figure 6] [--pairwise | --bench-capture] [--fading-db 0] [--coherence-s 3600]
    [--rician-k K] [--seed N] [--time real|max|<k>x]
```

Stations reach it through the testbed, which holds it in its own event loop and
hands it the loss tables by direct call — see [`../README.md`](../README.md).
[INTERNALS.md](INTERNALS.md) is how it works inside.

## What it models

**The loss table is the whole of who can hear whom.** Every ordered pair of
nodes has a path loss per band, computed at one frequency `f0` in it (the
format is [`../LOSSTABLE.md`](../LOSSTABLE.md), read by [`slt.py`](slt.py));
every node has an antenna gain; every
frame states the power it went out at. The level a frame arrives at is

```
L = P_tx + G_tx + G_rx − loss(tx → rx) − 20·log10(f / f0)
```

- The table is read in the frame's own direction: a measured table need not
  be symmetric.
- The last term moves the loss from the table's centre to the frame's carrier.
  Only the free-space part of a loss scales that way, so it is used within a
  band and a band with no table is silence.
- A pair the table does not have, or has as +inf, is **never heard**, and a
  station id the nodeset does not name is not in the medium at all.
- A frame keeps the levels it had when it went on the air: a table replaced
  mid-frame changes the next frame, not this one.

A station's **noise floor** is `−174 + 10·log10(BW) + NF`, with BW its
bandwidth in Hz and NF the noise figure — about −117 dBm at 125 kHz with the
default 6 dB.

**Who is affected and who can decode are two questions.** Every transmission
whose channel overlaps a receiver's is interference there, whatever its
spreading factor (SF) or sync word. A receiver can decode only a frame whose
**bandwidth, spreading factor, sync word and IQ polarity** match its last
stated radio, on its **carrier** — within a quarter of the bandwidth, which
is what a LoRa demodulator tolerates and what lets two drivers that round one
frequency differently hear each other — and only one whose preamble it finds
over the noise (below). IQ polarity (`iq`, `normal` or `inverted`) is the
chirps' direction: a receiver set for one finds no preamble in the other.

**Side detectors.** A radio that has them (an LR2021 has up to three) lists
them in its `state` as `side`, each with its own spreading factor, sync word
and IQ polarity, on the main detector's carrier and bandwidth. A frame that
matches one of them is decodable as it would be by the main detector, and the
`rx_begin` names the one (`det`, from 1). A side detector also misses a short
preamble whatever the SNR — 1.9% of 12-symbol preambles, 0.4% of 14, none
from 16, as Sergey's bench of LR2021 side detectors measured — and a CAD uses
the main detector only.

**Noise: one error curve, three stages.** The chance that one symbol is
demodulated wrong is exact for an ideal LoRa receiver at a given
signal-to-noise ratio (SNR), one curve per spreading factor. The chip sits a
little off that ideal, by an offset per spreading factor fitted so that the
datasheet's own test frame (64 bytes, coding rate 4/5, CRC on, explicit
header) fails 1% of the time at the datasheet's **demodulation threshold**:
−7.5 dB at SF7, 2.5 dB lower per step to −20 dB at SF12. So SF12 reaches 17.5
dB further than SF7 and pays for it in air time. From the symbol error, a
frame at a receiver goes through three stages, each one seeded draw:

1. **the lock**: four preamble symbols and the two of the sync word found. A
   frame that fails it is not delivered at all, only its energy: that is what
   "out of range" means here;
2. **the header**, the first block of 8 symbols at coding rate 4/8, decided
   at the lock. A header that fails is told in the `rx_begin` (`"hdr_ok":
   false`); the reception ends at `t_hdr` with an `rx_end` of verdict `hdr`,
   and the receiver is free to lock on to another frame from then;
3. **the payload**, every block of it decoded, at the frame's end. Coding
   rates 4/7 and 4/8 correct one wrong symbol per block, 4/5 and 4/6 none. A
   payload that fails is a cyclic redundancy check (CRC) failure.

So the waterfall from 90% lost to 90% received is under 2 dB wide, a longer
frame is more fragile at the same SNR (from 50 to 200 bytes moves the
waterfall by about 0.7 dB), and a frame failing inside the waterfall usually
fails its CRC rather than its lock. The datasheet threshold is where a
64-byte frame almost always arrives; half of them do about 1.8 dB under it.
A frame is energy only after the first stage fails, and silent to the
firmware after the first two.

**Fading** (`--fading-db σ`, `--coherence-s Tc`, off unless given) moves
every link's level over time around the table's: a Gaussian in dB of spread
σ, one process per pair of nodes, the same in both directions, alike over the
coherence time (an hour unless given) and unrelated two coherence times
apart. Every level the ether uses for a frame is taken at the instant it
matters — the lock, the header, each payload block, each stretch of
interference — so a marginal link wanders in and out of reach instead of
being good or dead for the whole run. It is slow variation of the kind
things moving in the path give a fixed station. Fast fading
(`--rician-k K`, off unless given) adds, per frame at each receiver, one
Rician draw of K factor K (0 is Rayleigh), unit mean power, held for the
whole frame. Per-frame fading lets retries through more easily than a real
channel does, so the slow kind, with a long coherence time, is the one to
start from.

**A multi-SF receiver** hears more than one spreading factor: its `state`
lists them, `"sfs": [5, 6, 7]`, its own among them, and it can decode a frame
at any of them. The LR2021's model says so when its firmware sets the chip's
side detectors, which listen for the faster SFs below the main one on its
bandwidth; the SX1262's when its station's environment has
`SIM_MESH_MULTI_SF`, which makes it that receiver by the LR2021's rule. It still has
one demodulator, so a frame at any of those SFs locks it as one at its own SF
would, and everything else is as for any receiver. No state lists `sfs`
unless a station asks, so a run without it is as it was.

**The receiver locks on at the preamble.** A decodable frame takes the
receiver when it is not demodulating another, or when it leads the one in
progress by the same-SF figure, 6 dB — and then the earlier one is lost
there. A frame that does not take the receiver is sent as energy: an
`rx_begin` marked `"cad": true`, and later an `rx_end` with verdict `crc` that
the chip, following another frame, drops.

**A locked frame survives interference over its whole air, the worst
stretch deciding.** Its air is cut wherever the set of overlapping
transmissions changes, and in every piece its level over each **class** of
interference — every overlapping transmission at one spreading factor, their
powers summed — is at or above that class's rejection figure: 6 dB for its
own spreading factor, and for another the inter-SF figure Croce et al.
measured on the SX1272 (from −8 dB for SF7 against SF8 to −25 dB for SF12
against SF7).

A frame that fails is a CRC failure: the receiver is handed it with its CRC
failed, as a spoiled frame reaches a real one. So a station beside one of two
transmitters keeps its neighbour's frame, a station that hears both equally
keeps neither, two interferers each well under a frame can spoil it together,
and a strong frame at another spreading factor spoils a weak one only when it
is past the orthogonality the two SFs have. Every figure is in one table at
the top of `ether.py`, with its source.

**Carrier sense answers from the same air.** A station whose radio last said
it was in **CAD** (channel activity detection) is told a frame is arriving —
an `rx_begin` marked `"cad": true`, never an `rx_end`, and no reception
recorded — when that frame is decodable there or the summed energy in its
band crosses the **sense threshold**: 15 dB over the receiver sensitivity
limit of the European Telecommunications Standards Institute's (ETSI) EN 300
220-1, −81 dBm at 125 kHz. A station in RX is told of in-band energy over the
same threshold the same way, which is what its instantaneous RSSI (received
signal strength indication) reads.

**A station that starts listening mid-frame** — back from its own
transmission, out of standby, out of a CAD into RX — is judged by the same
rules at that instant. While at least four symbols of a frame's preamble are
still to come it can lock on to the frame, as a receiver listening all along
would; after that it has missed the preamble and is told the frame's energy
until its end, stamped with the instant it was told. A receiver that leaves
RX for anything but its own transmission, or is retuned, while following a
frame is told nothing of how that frame ended, and the record holds no end
for it.

The ether does not match on preamble length: two radios whose preambles
differ hear each other here, and may not on a bench.

A station is deaf while its own frame is going out — the radio is half duplex —
which is what makes a hidden terminal behave like one: two stations that cannot
hear each other do not defer to each other either. A frame a station was
receiving when it started transmitting is lost to it.

**The pairwise rule** (`--pairwise`, or `Ether(pairwise=True)`) decides on the
same levels the way a simpler medium does: a frame is delivered where it
matches and locks, a later frame takes the receiver only when it leads
the one in progress by 6 dB in the whole dB the station is shown, and a frame
survives only by leading each audible interferer on its carrier by 6 dB, one
at a time, whatever their spreading factors, with nothing summed. It is there
to compare against.

**Bench capture** (`--bench-capture`, or `Ether(bench_capture=True)`) rules
on two frames of one spreading factor the way a bench saw them meet (289
collisions of an SX1262 receiver, SF7 at 125 kHz), instead of by the same-SF
figure. Within 1.2 dB the two are equals: both are lost about one time in
four, and otherwise one of them survives. From there the stronger survives
seven times in eight, rising to always at 6.1 dB, and the weaker never does.
A frame that starts after the receiver has passed the first one's preamble
never takes it, and spoils the first unless the first is the stronger. Each
outcome is a draw from the seed, the pair and the receiver, so the lock and
both verdicts read the same one. Against three or more frames of its class a
frame's lead is over their sum. Inter-SF rejection and the three stages
against noise are as without it.

**Every draw is the seed's and the channel's.** Each stage, each bench
outcome, each Rician draw and each knot of fading is a hash of the seed
(`--seed`) and what it is about on the air: a frame as its sender's node
name, its start and its bytes; a receiver as its node name and slot; a pair
of nodes by their names. Never a count of events. So a verdict does not
depend on the order receptions end in, and two runs that differ — in their
routing, say — draw the same for every frame they both put on the air at
the same instant: the same channel, so what differs between them is theirs.
**Why a reception failed** is in its `rx_end` as `cause`, for the record and
the testbed's tools; a real chip cannot know it, and the station ignores it:
`noise` (a stage failed), `interference` (a rejection figure, the pairwise
capture margin or bench capture), `talked_over` (the receiver transmitted
during it) or `lost` (another frame took the receiver off it, or it arrived
while the receiver followed another).

Absent at this depth: a referee.

**The side detectors' loss** (`--side-detector-loss`, none unless given) is
what a multi-SF receiver misses of the frames its side detectors catch: those
at any SF it states but its smallest, which is on its main detector. A side
detector loses a frame of a short preamble now and then, at a chance by its
length in symbols as the sender set it (`pre`), given as `n:p` entries:
`12:0.019,14:0.004` is the reticulum project's bench, an LR2021 with SF5 on
its main detector hearing SF7 at 14-15 dB SNR, which lost 9 of 481 frames of
12 symbols, 1 of 240 of 14, and none of 16 or 24. A length takes the figure
of the longest in the table not over it; one shorter than them all, the
shortest's, a lower bound, since nothing shorter was measured; one past the
longest, none. A frame lost so ends `crc`. Frames on the main detector, and
every frame at a receiver that states no `sfs`, are never lost so. Each frame
at each receiver slot draws once, from a hash of the seed, the receiver's
name, its slot and the frame's start.

## Losses

Whatever drives a run hands the ether its tables:

```python
ether.set_losses({"868": table, ...}, {"gw-alex": 1, ...}, {1: 5.0, ...})
                                  # tables by band, station id by node name,
                                  # antenna gain in dBi by station id
ether.update_node("gw-alex", {"868": (from_db, to_db)})
                                  # one node's row and column, recomputed
ether.replace_losses("868", table)   # one band's whole table
ether.set_gain(sid, gain_db)
ether.physics = Physics(noise_figure_db=6, fading_db=0, coherence_s=3600, rician_k=None)
```

Each call swaps what it changes in whole, between two events, so no frame is
ruled on half a table; until it is made the old figures stand. The tables
handed in are never written to.

The gains are one figure per station, the same in every direction. sim-mesh's
testbed gives none: its antennas have patterns, so it takes each pair's gains
off that pair's loss before it hands the tables over (`losses.with_antennas`).

Run alone, `--nodeset` names the nodes, their ids and `antenna.gain_dbi` (a
file of the ether's own: a sim-mesh nodeset's antenna is a type, and gives 0),
and `--losses` is the directory holding `<band>.bin` for each band computed.
`--geodata` is named in the log and read for nothing else: the tables already
belong to it. The tables are handed over as they are: whatever links and
offsets a nodeset holds, and whatever shadowing a geodata asks for, are the
caller's to put on first.

## Watching the air

Whatever holds the ether can subscribe to three callbacks, which is how the
testbed's map is fed:

| Callback | Raised when |
|---|---|
| `on_tx(sid, eid, freq, t_start, t_end)` | a frame goes on the air |
| `on_rx(sid, rsid, eid, verdict, level, cause)` | one station's reception of it closes; `cause` None when clean |
| `on_station(sid, state)` | a station states what its radio is doing |

`levels(sid, freq, bw, power, sf)` answers the other question — every
station that could decode one station on that carrier, from the table, and
at what level.

In a virtual-time run, whose clock is conductor time T ([Virtual
time](#virtual-time)), whatever holds the ether also tells it what it does to
the stations from outside the air, so that lands at an instant of T:

| Call | Does |
|---|---|
| `sleep(s)` | waits `s` of T; T stays at the end until what the wait woke has run |
| `expect(sid)` / `leave(sid)` | a station is starting (T waits for its hello and idle) / has gone |
| `typed(sid, total)` | the console of `sid` is being written, `total` bytes since it started; T waits until the station has read them |
| `sync(sid, done)` | `done()` once `sid` has the run's T and nothing in hand; console bytes go out then |
| `on_drain(sids, done)` | set by the holder: before T moves past stations that ran, read what they printed, then `done()` |

## The wire

A real-time run:

```
station → ether   hello {sid, slots}
ether → station   welcome {t, mode: "real", rate: 1, epoch, seed}
station → ether   state {slot, mode, mod, freq, bw, sf, cr, sync, hdr, crc, pre, iq[, side]}   on every change
station → ether   tx {slot, id, t0, t_pre, t_hdr, t_end, …, payload}
ether → station   rx_begin {slot, id, t0, t_pre, t_hdr, t_end, level[, cad][, hdr_ok][, det]}   each receiver it reaches
ether → station   rx_end {slot, id, t, verdict, [cause,] payload, rssi, snr}   at the frame's end
```

A header that fails:

```
ether → station   rx_begin {…, hdr_ok: false}                  the chip raises a header error at t_hdr and lets go
ether → station   rx_end {…, verdict: "hdr", cause: "noise"}    at t_hdr, for the record; the chip ignores it
```

A virtual-time run is the same conversation with the ether as conductor: it
owns conductor time T and moves it only when every station has said it is
idle.

```
station → ether   hello {sid, slots}
ether → station   welcome {t, mode: "virtual", rate, epoch, seed, seq: 1}
station → ether   idle {seq: 1, until: 25000}            nothing to do before T 25 000
ether → station   run {t: 25000, seq: 2}                 every station idle; T moves to 25 000
station → ether   state {…}  tx {t0: 25000, …}           what it did at 25 000
station → ether   idle {seq: 2, until: 30000}
ether → station   rx_begin {t: 25000, seq: 7, …}         to each receiver, at the same T
receiver → ether  idle {seq: 7, until: …}
ether → station   rx_end {t: 266000, seq: 9, …}          when T reaches the frame's end
```

Input that does not come over the air, in a virtual-time run:

```
writer → ether    wrote {ch: "tcp/A>B", n: 207, go: 4}   from the writing thread's own socket
ether → reader    run {t: T, seq: 12}                    the reader had an older T
reader → ether    idle {seq: 12, …}
ether → writer    go {go: 4}                             the run is quiet; lowest station first
reader → ether    read {ch: "tcp/A>B", n: 207}           T has waited for this
ether → reader    run {t: T, seq: 13}                    at work on it, at the T it has
reader → ether    idle {seq: 13, …}
station → ether   read {ch: "tty", total: 36}            the console, typed by the testbed
```

JSON, payloads base64, times in microseconds. A station sends one message
per datagram. The ether sends one too, except to a station whose `hello`
says `"lines": 1`: that station gets, in a virtual-time run, everything the
barrier tells it at one go in one datagram, a message a line. It applies
the lines as one, telling its host of nothing until all are in (below).
Losses and positions are not on it in either direction: a station never
learns where it is.

**Station → ether**

| Message | Says |
|---|---|
| `hello` | this station exists, and which radio slots it has; `"lines": 1`, it takes several messages to a datagram |
| `state` | a slot's mode, modulation and carrier — the ether matches on these |
| `tx` | a transmission: its modulation, carrier and power, its three instants, and its payload |
| `idle` | virtual time: the station has done everything the message numbered `seq` gave it to do, and next needs to run at T `until` (`null`: not on its own) |
| `read` | virtual time: bytes it took in from outside the air — `"ch": "tty", "total": N`, its console, a running total; `"ch": "tcp/A>B", "n": N`, a TCP connection from another station |
| `wrote` | virtual time: `"ch": "tcp/A>B", "n": N`, bytes it is about to write to another station; with `"go": k`, sent from the writing thread's own socket, which waits for the `go` |
| `listen` | virtual time: `"at": "addr:port"`, a TCP endpoint it listens on, whose connections are its own |

`mod` is the modulation a `state` or a `tx` is in, and a receiver hears only
a frame in its own. The ether models `lora` (with `bw`, `sf`, `cr`, `sync`,
`hdr`, `crc`, `pre` and `iq`); a `state` or `tx` naming another is refused and
logged, since nothing here knows what it would take to hear it. A `state`'s
`side`, when the radio has side detectors, is a list of `{sf, sync, iq}`, one
per detector, in the radio's own order; a radio without them leaves it out.

**Ether → station**

| Message | Says |
|---|---|
| `welcome` | joined: `t`, the ether's clock now; `mode`, `real` or `virtual`; `rate`, how many times the wall clock a virtual run is paced at (`null`: as fast as it goes); `epoch`, the wall-clock microseconds T 0 stands for; and `seed` |
| `rx_begin` | a frame is arriving: when its preamble, header and end fall, and how strongly; `"cad": true` when it is energy to this station, not a frame to demodulate — always so in CAD, and in RX when the receiver does not lock on to it; `"hdr_ok": false` when its header will fail at `t_hdr`; `det`, from 1, the side detector that found it, absent for the main one |
| `rx_end` | that frame is over: the verdict (`clean`, `crc`, or `hdr` at `t_hdr` for a header that failed), why when not clean (`cause`), the payload, RSSI and SNR |
| `run` | virtual time: T has reached the instant this station asked for; or, at the T it has, it has input to work on and owes an idle for it |
| `go` | virtual time, to the socket a `wrote` with `go` came from: that write may go ahead |

A station's `t0`, `t_pre`, `t_hdr` and `t_end` are its own clock in real time
and mean something only against each other within one message; in virtual
time they are T. Unknown message types are ignored, on both sides.

The `id` in a `tx` is the transmitter's own count of its frames; the `id` in an
`rx_begin` and `rx_end` is the **ether's**, and no two frames share it. A
receiver has to be able to tell two frames apart while both are in the air, and
the number the transmitter gave each of them cannot do that.

### Virtual time

`--time max` runs T as fast as the stations allow, `--time <k>x` paces it at
k times the wall clock (falling behind rather than racing to catch up), and
`--time real`, the default, is the event loop's clock and no conductor.

In virtual time every message the ether sends is an instant: it carries `t`,
the T it happens at, and `seq`, the station's own count of messages from the
ether. The station applies it at that T and answers with an `idle` for that
`seq`; an idle for an older one is stale and ignored. Anything else a station
sends — a `state`, a `tx`, a second `hello` — means it is not idle.

- **T moves only when every station is idle**, and every station the testbed
  has started and not yet heard from counts as busy. It moves to the earliest
  of every station's `until` and the ether's own timers (a frame's end), and
  every station whose `until` is reached is sent a `run`.
- **What stations say while T stands is held** and taken when every station
  is idle again, in station-id order, so two stations acting at one instant
  are ruled on the same way every run.
- **A station takes an instant whole.** What the barrier tells a station at
  one go, such as a frame that ends at T and another that begins there,
  reaches a station that said `lines` as one datagram. The station's chip
  library applies every line, its own timers running as T moves, before it
  tells its host anything: the host's waits that fall due, and DIO1. A host
  thread woken at T then finds all of T. Told message by message, it had
  been woken as T first moved. Whether it looked at the chip before or after
  the reader applied the rest depended on how the host scheduled two threads,
  and so, now and then, did a run.
- **Input from outside the air lands at an instant.** A station with bytes
  it has not read — typed at its console, written to it over TCP by another
  station — holds T until its `read` says it has them, and is then sent a
  `run` at the T it has. A station about to write to another over TCP asks
  first (`wrote` with `go`); asks are answered when nothing else in the run
  is at work, in station order, once the reader has been told T.
- **A `tx` stamped before T** came from a station that acted on something from
  outside the run after its last idle. It goes on the air at T, and the ether
  logs it.
- **A station that asks for the T it already has**, 64 times running, is
  working in no time at all; it is given 10 ms of T instead, one FreeRTOS tick.
- **A station that restarts** says `hello` again, and everything it had
  scheduled or said goes with its old process.

The conductor is the barrier above, and most of what the ether does in a
virtual-time run: a few microseconds of work at every one of hundreds of
thousands of instants. [`core/`](core/) does it in Rust (built by `sim`
when it starts, into `build/`), in the event loop's thread, on the ether's socket:
T, the stations' numbering and idles, the resend buffer, the barrier itself,
and whether the stations that just ran printed anything. Everything else —
the medium, the ether's timers, hello, the channels, the testbed's holds —
stays in `ether.py`, which the core calls at the barrier's points. The core
is the only conductor: `Ether` had one of its own in Python, and the core
gave its record line for line until that one was retired. A virtual-time
run without the core built is refused, saying to build it; a real-time run
needs no conductor.

A station that stays busy cannot stop T for good: its own side reports idle
after 20 ms of wall time with nothing to show for it (the busy watchdog in
[`../radio/src/conductor.cpp`](../radio/src/conductor.cpp)), once none of its
threads is computing (the time shim, [`../radio/shim/simclock.c`](../radio/shim/simclock.c)).
Bytes a reader has not taken a second of wall time after they were written let
go of T too, and the ether logs the channel.

## The record

Every datagram in and out, except `idle` and `run`, is one tab-separated line
of `record.tsv`:

```
<stamp>  <in|out>  <station id>  <json>
```

The stamp is the wall clock (ISO 8601, UTC) in a real-time run and T in
seconds in a virtual one.

It is the account of what actually happened on the air, against which a
station's own view can be checked — which frames went out, who was told about
them, and how each reception ended.
[`../testbed/seq.py`](../testbed/seq.py) draws it as a sequence diagram.

## Tests

No firmware needed. Most speak the wire over real UDP sockets to the ether as
a child process, each writing the nodeset and the loss table it needs; the rest
hold an ether in-process to check the reception arithmetic piece by piece.
The hand-built case — two hidden senders and one receiver, on one spreading
factor and on two — has its calculation written out beside it, against the
figures table.

```sh
python3 -m pytest test_ether.py
```
