# The ether — internals

How the medium decides who hears what, and the choices behind it.
[README.md](README.md) is what it does and the wire it speaks.

## Shape

One `asyncio.DatagramProtocol` on one UDP (User Datagram Protocol) socket,
single-threaded. Everything
is driven from arriving datagrams and from timers — the event loop's in a
real-time run, the barrier's heap in a virtual one (below):

- the **loss tables**, one per band, with the node name each station id
  runs as and each station's antenna gain — held whether or not that station
  has ever been heard from;
- a **station** is an address, per slot the last `state` message it sent and
  the reception its demodulator is locked on to, and the instant its own
  transmission stops occupying it;
- a **frame** is a transmission in flight, on the ether's clock, with the
  level it arrives at each station, the receivers decoding it and every other
  transmission it shared air and band with;
- a **reception** is one receiver slot decoding one frame, from its
  `rx_begin` to its `rx_end`, and whether it has lost the lock.

A station is created the first time it is heard from, whatever the message, and
its address is updated on every one — so a station that restarts on a new
ephemeral port is simply followed. Its name and gain are not created that way:
they come from whatever is driving the run, and a station with no name is not
in the medium at all.

The ether runs **inside** the testbed's process rather than beside it, so
losses arrive by method call. That is why there is no wire for them and
nothing to keep in step: dragging a station on the map is one
`update_node()` between two frames, once its row is recomputed.

## Clocks

Three clocks meet here and only one of them is authoritative.

A station stamps its messages with its own clock, which starts at zero when
its process first reads it. Those numbers mean nothing between stations. So on every
`tx` the ether reads only the **offsets** — `t_pre − t0`, `t_hdr − t0`,
`t_end − t0` — and rebases them onto its own monotonic clock, zero when the
ether starts, so a real-time run's T counts from 00:00:00, at the instant the
datagram arrived. The `rx_begin` it sends carries ether microseconds; the
receiver, in turn, cares only about the gaps between them and schedules from its
own clock. Each hop keeps what it can trust and discards what it cannot.

The offsets are clamped: negative is zero, anything beyond the frame's own span
is the span, and a stated timeline longer than a minute is junk and is cut. A
station cannot make the ether schedule something absurd by stating it.

That is a real-time run. In a virtual one the ether's clock is **conductor
time T**, and it is the only clock: a station's timers, its sleeps and its
radio all run on T (or on node time, the station's own function of T), so a
`tx` states instants on T and the offsets are read the same way. The event
loop still carries the datagrams, but nothing the ether schedules is on the
loop's clock: `rx_end`s and the testbed's own waits (`Ether.sleep`) go into
one heap ordered by instant, then station id, then the order they were
scheduled in, and run when T reaches them.

## The barrier

In virtual time T moves only when nothing in the run could still act at the
T it has. A station is busy from the moment the ether sends it anything —
every message carries its `seq` — until it answers with an `idle` for that
`seq`; a station started and not yet heard from is busy too
(`expect()`, which the testbed calls before it starts the process). A count of
busy stations is kept, not a scan, because a barrier is taken hundreds of
times per second of T.

T also waits on two things that are not stations: **holds**, the testbed's
own work at the T it has, and **unread channels**, input a station has been
sent from outside the air and not yet read (below).

When nothing is busy, `kick()`:

1. takes whatever stations said while T stood — `state` and `tx`, held in
   arrival order — in station-id order, which is what makes two frames
   started at one instant collide the same way every run; taking them makes
   those stations busy again, so the loop ends there until they answer;
2. otherwise, when stations have run since their consoles were last read,
   has the testbed read them (`on_drain`) and holds T until it has, and until
   what that set going in the testbed has run;
3. otherwise moves T to the earliest of every station's `until` and the
   heap's first instant, runs what the heap has due, and sends `run` to every
   idle station whose `until` has been reached.

Holding a station's messages until the barrier rather than acting on them as
they arrive is the whole of determinism here. Two stations answering one
`run` race each other to the socket; taken as they arrived, the ruling on a
collision would depend on the host's scheduler.

A paced run (`--time <k>x`) measures T against the wall from where it
started, and a barrier that would get ahead waits on a loop timer. A run that
has fallen more than a quarter of a second behind carries on from where it is
rather than racing to catch up.

Two things keep a station from holding T for ever:

- **A station that asks for the T it has**, 64 times running, is given 10 ms
  instead. Work takes time; a station whose every idle says "run me now" is
  spinning, and a FreeRTOS tick of T lets it through.
- **A station that does not answer** answers anyway: its own side reports
  idle 20 ms of wall time after it was last told anything, unless a thread
  of it is still computing (the time shim holds the watchdog back then, so
  long work takes no T). An idle that
  arrives 18 ms or more after the message it answers was sent is counted per
  station (`slow_idles`), so a run that crawls can say which station is
  holding it.

A station that leaves (`leave()`) or says `hello` again is forgotten: its
held messages, its busy mark, its `until`, its channels and its asks go with
its old process.

## What does not come over the air

A station is only told T when something happens to it, and it acts on
whatever wakes it at the T it last had. Everything that can wake it other
than the ether's own messages is made into an instant of T, so that the same
run does the same thing whatever the host's scheduler does:

- **The testbed's waits.** `sleep()` ends at its instant of T with a hold,
  which is let go by `settle()`: once the event loop has nothing else ready,
  followed turn by turn, so the coroutine the wait woke and whatever it
  started (a station's start, a line typed) have run at that T. A station
  about to start is busy from `expect()`, before its process exists.
- **What the testbed types at a console** (`tty/<sid>`). `typed()` counts the
  bytes as written at once, so T waits; `sync()` holds them back until the
  station has the run's T and has taken every message sent to it, telling it
  T with a `run` first when it is behind; then the pty gets them. The
  station's time shim reports the running total it has read (`read`), and
  when that catches up the station is sent a `run` at the T it already has:
  it is at work on the input, and owes an idle for it like any message.
- **What a station prints**, which the testbed acts on (a framed-RPC reply,
  the capability marker). A station that has sent an idle has written
  everything it wrote before it; before T moves past it the testbed reads its
  pty until empty (a read of an empty pty master first waits for the kernel
  to carry the station's writes across) and T waits until what that set going
  has run, as for a sleep. The testbed's next line to a station is therefore
  typed at the T of the reply that prompted it.
- **TCP between two stations** (`tcp/<a>><b>`, one per direction). The
  writer's shim asks before the bytes exist and waits; the ether queues the
  ask and answers it only when the run is **quiet** — every station idle, or
  waiting on an ask made since it was last told anything, and no input unread
  but by such a waiting station — one ask at a time, lowest station first, and
  tells the reader T first if it is behind. Two stations that write to each
  other at one instant therefore go in the same order every run, and the
  reader reads at a T nothing else of the ether's can land in the middle of.
  The bytes count as written from the answer until the reader's `read`, and
  the reader then gets its `run`, as for the console. Both ends of a
  connection name a station, since the shim makes a station's connections to
  a loopback address leave from its own; a connection with an end that is not
  a station of the run (the testbed's web proxy) is not counted, and its asks
  are answered at once. An end is the station that has reported from it, else
  the one that said it listens there (`listen`), else the station at its
  address: two processes of one station share an address, and the one that
  listens owns the connections made to it.

Counts are running totals for the console and per call for TCP (a write the
kernel took less of is followed by the difference, negative). A channel whose
reader has not caught up in `UNREAD_GRACE_S` (a second) of wall time is not
being waited for by anything in the station — input it reads some other way,
or not at all — and lets go of T, with a line in the log.

## The level, and why it is a table

```
L = P_tx + G_tx + G_rx − loss(tx → rx) − 20·log10(f / f0)
```

The loss is read, not computed: every ordered pair of nodes has one per
band, computed at one frequency `f0` in the band by whatever made the table
([`../LOSSTABLE.md`](../LOSSTABLE.md) is the format) — the
planner's P.1812 over real ground for a pack, log-distance for synthetic
ground, with the nodeset's links and offsets and the geodata's shadowing
put on by whoever hands the tables over —
and the medium has one code path for all of them.
Every pair is in it, not only the pairs strong enough to carry a frame,
because a pair far too weak to be decoded still adds to a receiver's
interference; a pair beyond the compute radius or off the pack is +inf, and
+inf is silence.

The last term moves the loss from `f0` to the **frame's own carrier**. Only
free-space loss scales as 20·log10(f), so the correction is good within a
band and nowhere else, which is why each band has its own table and a
carrier outside every band's table is heard by nobody.

The table is read in the frame's direction, because a measured loss is per
direction. Gains are the nodeset's and the power is the frame's own, so the
same table serves a nodeset whose antennas or transmit powers change.

A frame's table level at each station is fixed the first time it is asked
for — at the frame's start for its receivers, at a verdict for an interferer
— and kept on the frame. A node moved mid-run has its row recomputed and put
in with `update_node()`, which copies the band's table, writes the row and
column into the copy and swaps it in: until then the old row stands, and a
frame already on the air keeps the levels it started with.

With fading on, every use of a level adds the pair's fade at the instant in
question to that fixed figure (`level_at`); `levels()`, the map's answer to
who can decode whom, does not.

## The noise: one error curve, three stages

`N = −174 + 10·log10(BW) + NF` (BW the bandwidth in Hz, NF the noise
figure). Against it, what decides is the **symbol error rate** of the
spreading factor (SF) at the frame's signal-to-noise ratio (SNR). Demodulation
— dechirp, a Fourier transform, the largest bin — is non-coherent detection
of M = 2^SF orthogonal signals in white noise, and its rate is exact: the
right bin is Rician and each of the others Rayleigh, so with Es/N0 = M·SNR
and a = √(2·Es/N0)

```
Pc = ∫₀^∞ x · exp(−(x² + a²)/2) · I0(a·x) · (1 − exp(−x²/2))^(M−1) dx,   SER = 1 − Pc
```

`symbol_error` integrates it by Simpson's rule with an exponentially scaled
I0, in pure Python (the ether imports nothing outside the standard
library), and keeps each result per 0.05 dB of SNR: only the SNRs a run
meets are ever computed, at about a quarter of a millisecond each.

The chip is not the ideal receiver. `implementation_loss(sf)` is the offset
the curve is read at (SNR − L), found by bisection on first use, such that
the SX1261/2 datasheet's own test frame — `REFERENCE_FRAME`: 64 bytes, coding
rate 4/5, CRC on, explicit header, 8-symbol preamble — fails `ANCHOR_PER`, 1%,
of the time at the datasheet's sensitivity, the conditions its section 3.5
states that table under. So the datasheet's threshold, −7.5 dB at SF7 and
2.5 dB lower per step to −20 dB at SF12, keeps its meaning, and `audible()`
still answers "reachable" there; what is new is the shape around it. The
offset comes out between −1.3 dB (SF5) and +1.1 dB (SF12): the datasheet's
2.5 dB steps are wider than the ideal curve's, so it is better than ideal
below SF9. A bench sweep through the threshold is what would fix it instead.

The threshold is **per spreading factor** because that is the whole point of
one: SF12 buys 17.5 dB of reach over SF7 and pays for it in air time, and a
flat cut would make the two identical to the medium.

With p the symbol error at a block's level, a frame goes through three
stages, each one draw of `seeded_draw(seed, frame, receiver, slot, stage)`
(`Ether.draw`; what the frame and the receiver are to a draw is under
"Draws", below):

1. **The lock** (`locks_on`), at `arrive` and `arrive_pairwise`, at the
   faded level of the lock instant: `(1 − p)^(4 + 2)`, four preamble symbols
   (`PREAMBLE_FOUND_SYMBOLS`) and the two of the sync word. It is the
   "decodable" test, for RX and CAD slots alike, and one draw per frame and
   slot, so a lock on time, a late one and a CAD agree. A frame that fails it
   is energy only.
2. **The header** (`header_ok`), decided at the lock, explicit headers only:
   the first block, 8 symbols at coding rate 4/8, at the faded level in the
   middle of [`t_pre`, `t_hdr`]. A header that fails is said in the
   `rx_begin` (`"hdr_ok": false`), and a timer at `t_hdr`
   (`deliver_header_error`) closes the reception there: `rx_end` verdict
   `hdr`, cause `noise`, the slot's lock released so it can take another
   frame, as a chip in continuous RX hunts again after a header error.
   `Reception.ended` makes the frame's own `t_end` timer a no-op (no timer
   can be cancelled), and `followed_at` says no from `t_hdr` on. When the
   receiver had already stopped following the frame by `t_hdr` — taken off
   it, out of RX, or transmitting — the timer does nothing, and the frame's
   end rules on it as on any other.
3. **The payload** (`payload_survives`), at `t_end`, for a reception that
   came through interference clean: every block decoded, the blocks spread
   evenly over [`t_hdr`, `t_end`], each at its own faded level (and, without
   a header, the first block before them at 4/8). A failure is `crc`, cause
   `noise`.

The blocks are AN1200.13's, the arithmetic of `radio/src/toa.h`
(`frame_blocks`): after the preamble a first block of 8 symbols at 4/8, then
`max(⌈(8·P − 4·SF + 28 + 16·crc − 20·implicit) / (4·(SF − 2·DE))⌉, 0)`
blocks of the coding rate's denominator in symbols. Interleaving gives each
codeword one bit of every symbol, so a wrong symbol is at most one bit error
per codeword: a block survives `(1 − p)^n` at 4/5 and 4/6, which only
detect, and `(1 − p)^n + n·p·(1 − p)^(n−1)` at 4/7 and 4/8, which correct
one. Two simplifications, both pessimistic: the first block's reduced rate,
which shrugs off a symbol landing in the next bin, is treated as any other;
and two wrong symbols are taken to defeat a correcting block, which they do
only when both flip the same bit position.

All three stages apply under both rules and without interference: they are
the medium's noise, and the rules differ only in what interference does. A
frame failing inside the waterfall — about 2 dB from nine in ten lost to nine
in ten received — fails its CRC far more often than its lock, because the
payload is many more symbols than the six of the lock. So the rarity of CRC
errors on real hardware is not the chain's shape but how rarely a link sits
in that window, and time variation decides how often it passes through: that
is what fading is for.

## Fading

`Physics.fading_db` (σ, `--fading-db`) and `Physics.coherence_s` (Tc,
`--coherence-s`, an hour unless given); off at σ = 0, the default. The hour
is from runs of Sergey's fork (sergeyculum) with per-link fading: shorter
periods let retries through too easily, and hour-scale ones gave the most
believable verdicts. Per unordered pair of node
names — the channel is reciprocal even where the table is not, and a name
outlives a station's restart where an id does not — a unit-variance Gaussian
process on the ether's clock:

```
k = ⌊t / Tc⌋,  u = t/Tc − k
z_k = Φ⁻¹(seeded_draw(seed, "fade", name_lo, name_hi, k))
g(t) = cos(π·u/2)·z_k + sin(π·u/2)·z_{k+1},   fade = σ·g(t)
```

cos² + sin² = 1 keeps the variance 1 everywhere; it is continuous, needs no
state, and is the same in whatever order it is asked. Its correlation at
lag Tc is 1/π averaged over the phase, and nothing from 2·Tc. The knots are
kept per pair in `Ether.knots` and pruned with the frames.

It is applied to every level after the table's: the lock, the header and
each payload block, every piece of interference (taken at the piece's
middle, the air cut at the knots too so no piece spans one), the energy
carrier sense reads, the lead a frame needs to take a receiver, and the
levels in `rx_begin` (at the lock instant) and `rx_end` (the mean over the
payload blocks, what the chip's packet status reports). Gaussian in dB is
slow variation, things moving in the path of fixed stations, not multipath.
The testbed's shadowing stays as well: that is spread over places, this over
time.

**Fast fading** is `Physics.rician_k` (`--rician-k`), off (None) unless
given: per frame at each receiver, one Rician power gain of that K factor,
unit mean, `|√(K/(K+1)) + √(1/(2(K+1)))·(x + i·y)|²` with x and y standard
normals drawn from the frame's and the receiver's key (`rician_db`,
`Ether.rice_db`), held for the whole frame and added in dB wherever the
slow fade is. Each interferer has its own draw at the receiver. K = 0 is
Rayleigh. It is per frame and not within one, so it stands for a frame that
is short beside the channel's own coherence; and a draw per frame lets a
retry through more easily than the channel would, which is why the slow
kind is the one to start from.

## Draws

Every random thing the ether decides — the three stages, bench capture's
outcomes, the Rician draws and fading's knots — is `seeded_draw`, a SHA-256
of the seed and what the draw is about, never a generator's next number and
never a count of events. What it is about is the channel:

- a frame is `frame_key`: its sender's node name, its start on the ether's
  clock and a hash of its bytes (`Ether.key_of`);
- a receiver is its node name (`Ether.node`) and the slot;
- a pair of nodes is their two names, in order.

Node names, not station ids, because a name outlives a restart; and not the
ether's own frame number, which counts every transmission before it. So a
verdict is the same whatever order receptions end in, and two arms of a
comparison that differ in their routing — and so in which frames go out and
when — draw the same for every frame they both send at the same instant.
What differs between them is then theirs, not the channel's. A frame sent
again unchanged has another start, so it is another draw: a retry is not
doomed to repeat its first fate.

## Who is affected, and who can decode

These are two questions with two answers.

**Affected** is every transmission whose channel overlaps the receiver's —
their centres closer than half the sum of their bandwidths — whatever its
spreading factor or sync word. A LoRa demodulator hears every chirp in its
band; what it can reject depends on the chirp, which is the verdict's
business, not the delivery's. Off the band a transmission contributes
nothing.

**Can decode** is narrower. For each other station, for each of its slots: it
must have a finite loss in the table, it must not be transmitting itself, the
slot must have last said `RX` or `CAD` (channel activity detection), its stated `bw`, `sf`, `sync` and `iq`
must equal the transmission's — or `bw` its, and `sf`, `sync` and `iq` one of
its side detectors' — its `freq` must be within a quarter of its bandwidth of
the transmission's, and the frame must pass the lock stage there. The
matching keys are one constant, `MATCH_KEYS`, and a side detector's
`SIDE_KEYS` — the things to extend when the medium learns to care about
coding rate, header type or preamble length.

`iq` is the IQ polarity, `normal` or `inverted`: swapping the in-phase and
quadrature halves of the baseband mirrors the spectrum, so the chirps sweep
down instead of up, and a demodulator dechirping with the other polarity
smears the energy across its bins and finds no preamble. LoRaWAN uses it to
keep uplinks and downlinks apart. A frame of the other polarity is energy to
a receiver, nothing more.

**Side detectors** are listening hardware beside the main one — an LR2021
has up to three, each set with its own spreading factor, low-data-rate
optimisation and IQ polarity (`SetLoraSideDetConfig`) and its own sync word
(`SetLoraSideDetSyncword`, 0x24 for each unless set), on the main detector's
carrier and bandwidth. A station states them in its `state` as `side`, a list
of `{sf, sync, iq}` (low-data-rate optimisation follows from the spreading
factor and bandwidth, and the ether does not match on it for the main
detector either). `Ether.detector` returns which detector matches: 0 the
main one, k the k-th side one, None none. The chip still demodulates one
frame at a time, so the lock, `takes()` and the verdicts are as for the main
detector; the `rx_begin` carries `det` so the chip can report which one.
Two differences: a side detector misses a short preamble at a rate of its
own, whatever the SNR (`SIDE_DETECTOR_PREAMBLE_MISS`, from Sergey's bench of
LR2021 side detectors: 1.9% at 12 symbols, 0.4% at 14, none from 16, a
straight line between and the 12-symbol figure held below), folded into the
lock's odds; and a CAD uses the main detector alone, so a side detector's
frame is energy to a slot in CAD.

The carrier is matched within a tolerance, not exactly, because the
synthesizer steps in 32 MHz / 2^25: two drivers asked for 869.525 MHz round it
to register values tens of hertz apart (RadioLib lands on 869 524 963 Hz,
another driver on 869 524 999), and an exact match makes two stations on
one channel deaf to each other. A quarter of the bandwidth is what a LoRa
demodulator tolerates.

A slot in `CAD` is sensing, not receiving. It must be told a frame is
arriving, or a channel activity detection is blind to every frame that starts
inside its window and carrier sense says the wrong thing exactly when two
stations contend. It must not be told how the frame ended: it demodulated
nothing, so there is no verdict to rule, and a reception recorded for it would
draw a green flash for a station that only listened for energy. So its
`rx_begin` carries `"cad": true` and no `rx_end` is scheduled; whether the
slot was sensing is decided at the frame's start, from its last `state`.

## Carrier sense

A CAD slot is told of a frame when that frame is decodable there — an SX1262
CAD correlates for chirps of its own spreading factor and bandwidth — or
when the summed level of everything on the air in its band at that instant
crosses the **sense threshold**. That threshold is the one the European
Telecommunications Standards Institute's (ETSI) EN 300 220-1 V3.1.1 sets for
clear-channel assessment (clause 5.21.2, Table 45): 15 dB above the receiver
sensitivity limit of Table 32, 10·log10(BW in kHz) − 117 dBm, which is −81
dBm at 125 kHz. It is what a listen-before-talk on the received signal
strength indication (RSSI) acts on, and it lets a strong frame at another spreading
factor make the channel busy, as it does on a bench.

An RX slot gets the same energy begins for the frames it is not decoding, so
the chip's instantaneous RSSI and a CAD it starts straight after RX read the
same air.

A slot that starts listening after a frame began is judged when it does
(`tell_late`), so that carrier sense is not blind to every frame that began
while a station was sending, in standby or in a CAD. It can still lock on to
a decodable frame while `PREAMBLE_FOUND_SYMBOLS` of its preamble are to come:
a receiver needs about four symbols to find a preamble, the bench's blind
window, and the chip model raises PreambleDetected at the same four.
Otherwise the frame is energy to it, in an `rx_begin` marked `cad` whose `t0`
is the instant it was told, so the chip measures only what is left of the
frame. "Starts listening" is a `state` into RX or CAD from another mode, or
one that retunes it; a chip reports standby at the end of its own
transmission, so a station back from sending is always one.

## Reception: interference, the worst piece deciding

Per receiver, per frame it is locked on to, at that frame's `rx_end`, before
the payload stage. The frame's air is cut at every instant an overlapping
transmission starts or ends (and, with fading, at every knot of it), so the
set of transmissions on the air is constant within each piece, and in every
piece the frame's **signal over each class of interference** is at or above
that class's rejection figure. A class is every overlapping transmission at
one spreading factor, and its powers are summed, in milliwatts, before the
test. The frame's own spreading factor needs 6 dB; another needs the
inter-SF figure, which is negative: a frame at SF12 survives an SF7
interferer 25 dB louder than itself.

A single failing piece makes the frame `crc`, cause `interference`. Noise
is not added in: the stages are against noise and the rejection figures
against a chirp, and adding noise to a chirp's power would make neither mean
what its source measured. Nor are the classes summed into each other: each
figure was measured against one interfering spreading factor, and
orthogonality between two others says nothing about their sum.

The verdict functions answer (verdict, cause): `lost` for a frame another
took the receiver off, or that arrived while it followed another;
`talked_over` when the receiver transmitted during it; `interference` for a
rejection figure, the pairwise margin or bench capture. A real chip cannot
know the cause; the `rx_end` carries it for the record and the testbed's
tools, and `on_rx` passes it on.

This is the difference between a medium and a referee. A collision is not a
property of a transmission — it is what happened at one antenna. Two stations
that cannot hear each other transmit over one another constantly; the station
between them keeps whichever frame is loud enough to be worth keeping, a
station out of one transmitter's reach never notices the collision at all,
and a receiver that hears three weak neighbours at once can lose a frame
that any one of them alone would have left it.

The interference list is held on the frame rather than recomputed from the
frames still in flight, so a reception that ends long after its interferer has
been pruned still knows what spoiled it. Scheduling an `rx_end` does not settle
it: a frame still in the air when a second one starts is spoiled retroactively
for receivers already told it was arriving, which is exactly what a radio does.

A receiver that starts transmitting while a frame is arriving loses it,
whatever else is on the air: the radio is half duplex.

## The figures

Every figure the verdict uses is in one table at the top of `ether.py`, with
its source beside it:

| Figure | Value | Source |
|---|---|---|
| thermal noise | −174 dBm/Hz | kTB at 290 K |
| noise figure | 6 dB (setting) | the SX1262's order of magnitude |
| demodulation threshold | −7.5 dB at SF7, 2.5 dB lower per step | SX1261/2 datasheet |
| the curve's anchor | 1% of a 64-byte frame (4/5, CRC, explicit header, 8-symbol preamble) lost at the threshold | SX1261/2 datasheet rev. 1.2, section 3.5, the conditions of its sensitivity table |
| symbols to lock | 4 of the preamble, 2 of the sync word | the bench's blind window; AN1200.13 |
| fading | σ 0 dB (setting), coherence 3600 s (setting) | the hour from sergeyculum's runs; both to be fitted to deployed nodes' frame-to-frame RSSI |
| fast fading | Rician K, off (setting) | — |
| side detector preamble miss | 1.9% at 12 symbols, 0.4% at 14, 0 from 16 | Sergey's bench of LR2021 side detectors |
| same-SF rejection | 6 dB | Semtech's specification, as Croce et al. quote it |
| inter-SF rejection | −8 … −25 dB | Croce et al., IEEE Comm. Letters 22(4), 2018, Table II (SX1272) |
| sense threshold | 10·log10(BW/1 kHz) − 102 dBm | ETSI EN 300 220-1 V3.1.1, 5.21.2 |

Croce et al. measure the same-SF threshold at about 1 dB; the medium uses
Semtech's 6 dB, because it is the chip's own figure and because the verdict
takes the worst piece of a frame, not its average. SF5 and SF6, which the
SX1262 has and the SX1272 measurement does not, take the SF7 row and column.

## Why the ether owns the lock

A receiver follows one frame at a time, and which one is decided at the
preamble: a decodable frame takes the receiver when it is not demodulating
another, or when it leads the one in progress by the same-SF figure, and
then the earlier frame is lost there. The chip model has no lock rule of its
own. It follows the frame the ether last began on it; a frame the receiver
does not lock on to is sent as energy, an `rx_begin` marked `"cad": true`,
and its `rx_end` — `crc`, so the map still shows the collision — is one the
chip drops, because it is not the frame it follows.

The lock is here because only the ether sees the sum. A chip told of frames
one by one would compare each new one to the one it has and nothing else,
and would decide differently from the verdict the ether then gives it.

The lock is released when the frame ends, when its `rx_end` goes out, when
its header fails at `t_hdr`, when the slot states any mode but `RX`, and when
the station transmits. The chip model lets go at a failed header on its own,
from the `hdr_ok` it was given at the lock.

## The pairwise rule

`Ether(pairwise=True)`, or `--pairwise`, rules the way a simpler medium does,
on the same levels from the same table: a frame is delivered where it matches
and locks; a later frame takes the receiver only when its level, rounded to
the whole dB the station is shown, leads the one in progress by 6 dB; and a
frame survives only by leading by 6 dB each interferer on its carrier that
reaches this receiver over its own datasheet threshold, one at a time, the
two levels taken in the middle of their overlap, with nothing summed and
no spreading factor spared. CAD is told only of frames it could decode. The
header and payload stages are as under the receiver-centred rule. It is
there so a run can be compared, frame for frame, with one ruled that way.

## Bench capture

`Ether(bench_capture=True)`, or `--bench-capture`, replaces the same-SF
figure, and nothing else, with what a bench measured: the reticulum
project's `tools/rncapture` of 2026-09-17, an SX1262 receiver with an SX1262
and an LR2021 sending, SF7 at 125 kHz, 121-byte frames, 289 collisions of two
frames whose starts were within about 8 ms. The figures and what is assumed
beyond them are in `ether.py`'s table (`BENCH_*`); `bench_outcome` is the
table as a function of the two frames, the receiver, the first frame's lead
and whether the receiver was locked on it.

It decides in two places, which must agree. At the lock, a frame arriving
while the receiver follows another takes it only when the receiver is still
inside the first one's preamble and the pair's outcome has the new one
surviving; past the preamble it never does. At the verdict, a frame that is
not lost must survive its class: its lead is over the summed power of its
own spreading factor's class in its worst stretch of air, its partner in the
outcome is the strongest of them, and "locked" is read off the first frame's
reception at that receiver, whether it was being followed when the second
started. With two frames that is the bench's pair exactly, and the outcome
drawn at the lock is the one the verdicts read, because every draw is a hash
of the seed, the pair and the receiver (`seeded_draw`); with fading on, the
lead at the lock and the lead in the worst stretch differ, and the two can
read different rows of the table. With three or more,
the sum stands in for the second frame, which is an assumption, as are other
spreading factors, bandwidths and starts further apart than the bench's.
Inter-SF classes are judged by the matrix as without it.

Matching is on the **last stated** values, not on anything the ether infers.
This is why a station publishes a `state` on every command that changes its mode
or carrier, and why a model that forgot to would go deaf silently. The one thing
the ether does infer is a transmitter's own deafness: a `tx` is not a `state`,
so a station that never said it had left `RX` would otherwise be told about
frames arriving during its own, and a hidden terminal would look like a station
ignoring what it could hear.

## A frame's two names

A station numbers its own transmissions and knows nothing of anyone else's, so
two stations can have frames in the air under the same number — which happens
the moment a testbed is restarted, since every station starts counting again. So
the ether gives each frame a number of its own and uses that one in `rx_begin`
and `rx_end`. A receiver following one frame while a second arrives, and a
reader matching an end back to its beginning, both need a name that is unique
across the air, and only the medium can issue one.

## Delivery

`rx_begin` goes out immediately, so the receiver can arm its preamble, sync and
header interrupts on the offsets. `rx_end` is a timer at the frame's stated
span, or at `t_hdr` for a header that failed. Nothing re-reads the frame in
between — a receiver that leaves `RX` mid-frame, or is retuned, discards the
reception itself, and the ether sends it no `rx_end` for it either: nothing
was received that the record, and every tool reading it, could count. Its own
transmission is the exception, ruled on at the frame's end as talked over,
`crc`, because that loss is the medium's.

Both carry the link's level: `rx_begin` as `level`, the faded level at the
lock instant, which is what an instantaneous RSSI reads and what carrier
sense acts on, and `rx_end` as `rssi`, the mean over the payload's blocks,
with `snr` the same figure above the noise floor. Both are rounded to whole dB
on the wire, because the station reads them as integers; the lock and the
verdict are decided on the unrounded figures, so a frame 6.4 dB up takes the
receiver and one 5.6 dB up does not, whatever the numbers the receiver is
shown. The pairwise rule's lock is the exception: it compares the rounded
figures, as a chip comparing the integers it was given would.

The payload is passed through as the base64 string it arrived as. The ether
never decodes it, which is what keeps it honest: it cannot accidentally know
anything about Reticulum.

## Events, and the rule about subscribers

`on_tx`, `on_rx` and `on_station` exist so something can watch the air without
reading the record — the map, at sixty frames a second. They are plain
callables, and an exception from one is logged and swallowed. A page that has
gone away, or a subscriber with a bug in it, must not be able to stop the
medium: the ether's job is the frames, and everything watching is optional.

## What is deliberately not here

- **Fading within a frame.** Fast fading, when asked for, is one Rician
  draw per frame and receiver; multipath that varies within a frame, at
  walking pace, is not modelled. No antenna pattern beyond what the tables
  are handed with, no rain.
- **Soft interference.** Interference is ruled by rejection figures, not fed
  into the symbol error curve as a signal-to-interference-plus-noise ratio.
- **Interference in the header.** A header spoiled by interference is not a
  header error: the frame runs to `t_end` and ends `crc` there, through the
  interference rule. Only noise and fading fail a header at `t_hdr`.
- **A payload with its CRC off.** A frame sent without a CRC whose payload
  fails is still reported `crc`; a real chip would hand over the corrupted
  bytes as good, but the ether never opens a payload. Reticulum and the
  reticulous firmware always run the CRC.
- **The reduced rate of the first block.** It tolerates a symbol landing in
  the next bin; the curve treats it as any other block, which is pessimistic.
- **Preamble length at the main detector.** It is not matched, and it does
  not change the main detector's lock: four of its symbols are what is
  needed, however many there are. Only a side detector's lock reads it.
- **Bandwidth and offset in the rejection figures.** The inter-SF figures
  were measured at one bandwidth on one carrier; a transmission at another
  bandwidth, or partly overlapping the band, is classed by its spreading
  factor and counted at its whole power.
- **A referee.** The ether does not judge a station's behaviour — it does not
  check that a transmission was preceded by carrier sense, or that a duty cycle
  was respected. The record is there so something else can:
  `testbed/compliance.py` and `testbed/referee.py` do, after a run.
