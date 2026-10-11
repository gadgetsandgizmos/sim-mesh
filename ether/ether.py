#!/usr/bin/env python3
"""The virtual ether: one UDP endpoint that carries frames between stations.

Stations announce themselves with `hello`, describe their radio with `state`
and hand over a transmission with `tx`; the ether answers `welcome`,
`rx_begin` and `rx_end`.

The level a frame arrives at comes from the loss table: every ordered pair of
nodes has a path loss per band (slt.py), and

    L = P_tx + G_tx + G_rx − loss(tx → rx) − 20·log10(f / f0)

with P_tx the power the frame itself went out at, the gains the nodes'
antennas and the last term the within-band correction from the table's centre
to the frame's carrier. A pair the table does not have, or has as +inf, is
never heard. The table arrives by direct call — `set_losses()` and its
relatives — from whatever is driving the run; the UDP wire to the stations
never mentions it, and a station never learns where it is.

Reception is decided at each receiver, from everything arriving there
(README.md, "What it models"; INTERNALS.md, "Reception"). Every transmission
whose carrier overlaps a receiver's bandwidth is interference there, whatever
its spreading factor or sync word; only a frame whose bandwidth, spreading
factor, sync word and IQ polarity match the receiver's state, on its
carrier, can be decoded — or, on a radio with side detectors, match one of
those. Against thermal noise a frame goes through three stages, each a
seeded draw, keyed on the channel and not on the order of events, against
odds from one symbol error curve per spreading factor
anchored at the datasheet's sensitivity: its preamble and sync word found
(the lock), its header read (a failure ends the reception at `t_hdr`), and
every block of its payload decoded (a failure is a CRC error), so a longer
frame is more fragile at the same signal-to-noise ratio. With fading on,
every link's level wanders over time around the table's, per pair of nodes.
The receiver locks on to a decodable frame at its preamble unless it is
already demodulating another that the new one does not lead by the same-SF
figure, and a frame it is locked on survives interference when, over every
stretch of its air, it clears each class of interference, summed within the
class, by that class's rejection figure. A station in `CAD` is told a frame is
arriving, and nothing more, when that frame is decodable there or the energy
in its band crosses the sense threshold. A slot that starts listening while a
frame is on the air is judged the same way at that instant: it can still lock
on while enough of the preamble is to come, and is told the energy otherwise.
A receiver that leaves RX for anything but TX mid-frame is not told how the
frame it was following ended.

With `pairwise` set the pairwise rule decides interference instead, on the
same levels and through the same three stages: a frame is delivered where it
locks, the receiver takes a later frame only
when it leads the one in progress by the capture margin, and a frame survives
only by leading each audible same-carrier interferer by that margin, one at a
time and without summing.

Clocks. In a real-time run a station's `t` fields are its own clock and are
meaningful only relative to each other inside one message; the ether rebases
every frame onto its own monotonic clock and schedules from that, so the `t`
fields it sends back are ether microseconds. The offsets inside a frame
(`t_pre - t0`, `t_hdr - t0`, `t_end - t0`) are the transmitter's and are
carried through unchanged, which is what a receiver actually needs. In a
virtual-time run (`--time max` or `<k>x`) the ether is the conductor: its
clock is T, which it moves only when every station has answered its last
message with `idle`, and every message it sends carries T and the station's
`seq` (README.md, "Virtual time").

Every datagram in and out, except `idle` and `run`, is one tab-separated
line of the record: stamp (the wall clock in a real-time run, T in seconds in
a virtual one), direction, station id, JSON.
"""

import argparse
import asyncio
import base64
import binascii
import contextlib
import functools
import hashlib
import heapq
import json
import math
import os
import random
import socket
import statistics
import sys
import time
from array import array
from datetime import datetime, timezone

import slt

# What a receiver's state must share with a frame for the frame to be decoded.
# `iq` is the chirps' direction, "normal" or "inverted": a demodulator set for
# one finds no preamble in the other, only its energy.
MATCH_KEYS = ("mod", "bw", "sf", "sync", "iq")
# What a side detector (a state's `side` list, an LR2021's beside its main
# one) states of its own; the carrier, bandwidth and modulation are the main
# detector's, since they share the receive chain.
SIDE_KEYS = ("sf", "sync", "iq")
# The modulations this ether models: a state or a frame saying another is
# refused, since nothing here knows what it would take to hear it.
MODULATIONS = ("lora",)

DEFAULT_POWER_DBM = 14      # a `tx` that did not say what it was sent at

# ---- The figures ----------------------------------------------------------
#
# Every number the reception model decides with, and where it comes from.
#
# Thermal noise: kTB at 290 K is −174 dBm in one hertz; a receiver's floor is
# that plus 10·log10(bandwidth) plus its noise figure. 6 dB is the SX1262's
# order of magnitude and a setting of the medium (`Physics`).
THERMAL_DBM_PER_HZ = -174.0
DEFAULT_NOISE_FIGURE_DB = 6

# Demodulation threshold: the SNR over thermal noise a spreading factor needs,
# from the SX1261/2 datasheet: −7.5 dB at SF7 and 2.5 dB lower per step. It is
# where the error curve below is anchored, and where `audible` says a link is
# reachable. SF12 buys 17.5 dB of reach over SF7 and pays for it in air time;
# a flat threshold would make the two identical to the medium.
SENSITIVITY_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0,
                  9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
SLOWEST_SENSITIVITY_DB = -20.0      # a frame that named no spreading factor
SLOWEST_SF = 12

# The error curve's anchor. A symbol is demodulated by picking the largest of
# 2^SF bins, non-coherent detection of orthogonal signals in white noise, whose
# symbol error rate is exact (`symbol_error`); a frame's chance of arriving
# follows from it stage by stage (`frame_blocks`, `block_survives`). The real
# chip sits some way off that ideal curve, by an offset per spreading factor
# (`implementation_loss`), fixed so that the reference frame below fails
# ANCHOR_PER of the time at SENSITIVITY_DB. Both come from the conditions the
# SX1261/2 datasheet (DS.SX1261-2.W.APP, rev. 1.2, June 2019, section 3.5)
# states its sensitivities under: "LoRa PER = 1%, packet 64 bytes, preamble 8
# symbols, CR = 4/5, CRC on payload enabled, explicit header mode". The fitted
# offset runs from −1.3 dB at SF5 to +1.1 dB at SF12, about 0.35 dB a step,
# negative up to SF8: the datasheet's figures are a 2.5 dB ladder, and the
# ideal curve's steps are some 2.15 dB, so below SF9 the datasheet is better
# than the ideal receiver. A bench sweep through the threshold is what would
# replace the anchor.
REFERENCE_FRAME = {"payload": 64, "cr": 5, "implicit": False, "crc": True,
                   "preamble": 8, "bw": 125_000}
ANCHOR_PER = 0.01

# The resolution the symbol error curve is computed at and kept in, in dB.
SNR_STEP_DB = 0.05

# Fading: a level varying over time around the table's, per pair of nodes, as
# a Gaussian in dB of this spread, correlated over the coherence time (`Fading`).
# Off unless asked for. The coherence time is an hour because runs of Sergey's
# fork (sergeyculum) with per-link fading gave their most believable verdicts
# at hour-scale periods, shorter ones letting retries through too easily; it
# stands until deployed nodes' frame-to-frame RSSI gives one.
DEFAULT_FADING_DB = 0.0
DEFAULT_COHERENCE_S = 3600.0

# Fast fading, per frame at each receiver: Rician, of this K factor (the
# power of the steady path over that of the scattered ones; 0 is Rayleigh).
# Off (None) unless asked for.
DEFAULT_RICIAN_K = None

# Side detectors (an LR2021's, beside its main one) miss a preamble of this
# many symbols this often, at 14–15 dB of SNR where noise has nothing to do
# with it: Sergey's bench of LR2021 side detectors (sergeyculum). A straight line
# between the points, the shortest one's figure held below it, nothing from
# the longest on.
SIDE_DETECTOR_PREAMBLE_MISS = {12: 0.019, 14: 0.004, 16: 0.0}

# Same-SF rejection: how far a frame must lead the summed power of every
# same-SF transmission in its band to be demodulated through it. The 6 dB of
# Semtech's specification, the figure Croce et al. (below) quote as "6 dB
# specified" against the 0–1 dB they measure; the specification's is used
# because it is the chip's own and because the medium takes a receiver's
# worst stretch, not its average. The same figure is the lead a later frame
# needs to take a receiver off the one it is demodulating.
SAME_SF_REJECTION_DB = 6.0

# Inter-SF rejection: the signal-to-interference ratio, in dB, a frame at
# spreading factor `ref` needs over the summed power of every transmission in
# its band at spreading factor `int`. Measured on the SX1272: D. Croce,
# M. Gucciardo, S. Mangione, G. Santaromita, I. Tinnirello, "Impact of LoRa
# Imperfect Orthogonality: Analysis of Link-Level Performance", IEEE
# Communications Letters 22(4), 2018, Table II ("SIR thresholds with SX1272
# transceiver"), rows SF_ref, columns SF_int. Its diagonal (1 dB) is replaced
# by the same-SF figure above. SF5 and SF6, which the SX1262 has and the
# SX1272 measurement does not, take the SF7 row and column.
INTER_SF_REJECTION_DB = {
    #      int:  7      8      9     10     11     12
    7:  {7: 1, 8: -8, 9: -9, 10: -9, 11: -9, 12: -9},
    8:  {7: -11, 8: 1, 9: -11, 10: -12, 11: -13, 12: -13},
    9:  {7: -15, 8: -13, 9: 1, 10: -13, 11: -14, 12: -15},
    10: {7: -19, 8: -18, 9: -17, 10: 1, 11: -17, 12: -18},
    11: {7: -22, 8: -22, 9: -21, 10: -20, 11: 1, 12: -20},
    12: {7: -25, 8: -25, 9: -25, 10: -24, 11: -23, 12: 1},
}

# Sense threshold: the summed in-band level at which carrier sense calls the
# channel busy although nothing on it is decodable. ETSI EN 300 220-1
# V3.1.1 (2017-02) clause 5.21.2, Table 45: the clear-channel-assessment
# threshold for a device under 100 mW e.r.p. is 15 dB above the receiver
# sensitivity limit of Table 32, 10·log10(bandwidth in kHz) − 117 dBm, so
# −81 dBm at 125 kHz. An SX1262 CAD correlates for chirps of its own
# spreading factor and bandwidth, which is the "decodable" half of the busy
# test; this is the other half, the energy an RSSI-based listen-before-talk
# acts on.
SENSE_ABOVE_SENSITIVITY_DB = 15.0
SENSITIVITY_LIMIT_DBM_AT_1KHZ = -117.0

# Pairwise rule only: how far a frame must lead each audible interferer.
PAIRWISE_CAPTURE_DB = 6.0

# How many symbols of a preamble a receiver needs to find it. Firmware that
# senses the channel by asking the demodulator measured a blind window of
# about 4 ms at SF7 and 125 kHz, three boards over 150 trials (the reticulum
# project's bench; the chip model's kPreambleFoundSymbols is the same figure).
# So a slot that starts listening while a frame is on the air can still lock
# on to it if at least this much of the preamble is still to come, and has
# only its energy otherwise. The same symbols, and the sync word's two, are
# what must be demodulated right for the receiver to lock at all.
PREAMBLE_FOUND_SYMBOLS = 4.0
SYNC_WORD_SYMBOLS = 2

# The first block after the preamble: 8 symbols at coding rate 4/8, whatever
# the payload's coding rate (AN1200.13). It carries the header when there is one.
FIRST_BLOCK_SYMBOLS = 8

# Capture as a bench measured it (`--bench-capture`, off unless given): the
# reticulum project's tools/rncapture of 2026-09-17 (its README, the table of
# 289 collisions): an SX1262 receiver, SX1262 and LR2021 senders, SF7 at 125
# kHz, 121-byte frames, two frames at a time whose starts were within about
# 8 ms (listen-before-talk stayed on). Within 1.2 dB the two are equals: both
# were lost 9 times in 39, and otherwise one of them survived, either one.
# From there to 2.7 dB the stronger survived 119 times in 136, and from 6.1 dB
# every time; the straight line between is an assumption. The weaker never
# survived. A frame that started after the receiver had passed the first
# one's preamble was never received, and it spoiled the first unless the
# first was the stronger (six times in six, at about 2 dB); at equal power
# the first is assumed to survive as often as equals do not both die. Other
# spreading factors, bandwidths and start offsets are assumed to behave alike.
BENCH_EQUAL_DB = 1.2
BENCH_BOTH_LOST = 9 / 39
BENCH_STRONGER = 119 / 136
BENCH_STRONGER_DB = 2.7
BENCH_CERTAIN_DB = 6.1

# What follows the preamble before `t_pre`: two sync-word symbols and 2.25 of
# the start-of-frame delimiter (AN1200.13), so a frame's preamble proper ends
# this many symbols before the `t_pre` its transmitter states.
SYNC_SYMBOLS = 4.25

# How far apart two carriers may be and still be one carrier, as a fraction
# of the bandwidth. The synthesizer steps in 32 MHz / 2^25, so two drivers
# asked for the same frequency round it to register values tens of hertz
# apart; an exact match would make them deaf to each other, which no receiver
# is. A LoRa demodulator tolerates an offset of a quarter of its bandwidth.
CARRIER_TOLERANCE = 0.25

# ---------------------------------------------------------------------------

SPEED_OF_LIGHT = 299_792_458.0

MAX_FRAME_US = 60 * 1000 * 1000   # a stated timeline longer than this is junk

# A station that asks, this many times running, to run at the T it already
# has is given this much T instead: one FreeRTOS tick.
STANDING_LIMIT = 64
STANDING_QUANTUM_US = 10_000

# An idle that arrives this long, in wall time, after the message it answers
# came from a station that was busy: the station's own watchdog reports after
# 20 ms. Counted per station, so a run that crawls can say who holds it.
SLOW_IDLE_S = 0.018

# The receive buffer asked of the kernel for the ether's socket, in bytes
# (it gives no more than net.core.rmem_max). The default, some 200 KB, is less
# than a hundred stations' radio settings arriving at one instant, and a
# datagram the kernel drops is a message the ether never hears.
RECV_BUFFER_BYTES = 4 * 1024 * 1024

# The least wall time between two resends to one station of what it missed.
RESEND_GAP_S = 0.1

# Bytes written into a channel of the run (a station's console, a TCP
# connection between two stations) hold T until their reader has taken them.
# A reader that has not, this long in wall time after they were written, is
# not waiting for them; the channel lets go of T and says so.
UNREAD_GRACE_S = 1.0

# How many turns of the event loop `settle()` waits, at most, for the loop to
# have nothing else ready.
SETTLE_TURNS = 1000


def same_carrier(freq_a, freq_b, bw_hz):
    """True when two stated frequencies are one carrier at this bandwidth."""
    if freq_a is None or freq_b is None:
        return freq_a == freq_b
    return abs(freq_a - freq_b) <= CARRIER_TOLERANCE * float(bw_hz or 125_000)


def in_band(freq_a, bw_a, freq_b, bw_b):
    """True when two transmissions' channels overlap in frequency at all."""
    if freq_a is None or freq_b is None:
        return freq_a == freq_b
    half = (float(bw_a or 125_000) + float(bw_b or 125_000)) / 2.0
    return abs(freq_a - freq_b) < half


def rejection_db(sf_signal, sf_interferer):
    """The lead a frame at `sf_signal` needs over the summed interference at
    `sf_interferer`: the same-SF figure, or the inter-SF matrix's entry."""
    if sf_signal is None or sf_interferer is None or sf_signal == sf_interferer:
        return SAME_SF_REJECTION_DB
    ref = min(12, max(7, int(sf_signal)))
    other = min(12, max(7, int(sf_interferer)))
    if ref == other:
        return SAME_SF_REJECTION_DB
    return float(INTER_SF_REJECTION_DB[ref][other])


def sense_threshold_dbm(bw_hz):
    """The summed in-band level above which carrier sense calls the channel busy."""
    return (10.0 * math.log10(max(float(bw_hz or 125_000), 1000.0) / 1000.0)
            + SENSITIVITY_LIMIT_DBM_AT_1KHZ + SENSE_ABOVE_SENSITIVITY_DB)


def dbm_to_mw(dbm):
    return 10.0 ** (dbm / 10.0)


def mw_to_dbm(mw):
    return 10.0 * math.log10(mw) if mw > 0 else -math.inf


def wall_stamp():
    """The wall-clock timestamp that heads a record line."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class RealClock:
    """The event loop's own clock, in microseconds from the ether's start."""

    virtual = False

    def __init__(self, loop):
        self.loop = loop
        self.origin = loop.time()

    def now(self):
        return int((self.loop.time() - self.origin) * 1_000_000)

    def call_at(self, t_us, callback, *args, key=0):
        return self.loop.call_at(self.origin + t_us / 1_000_000.0, callback, *args)


class VirtualClock:
    """Conductor time T, in microseconds: moved by the barrier, not the wall.

    Events are kept in one heap and run when the barrier reaches their
    instant, in order of instant, then key — a station's id, so events at one
    instant run in station order — then the order they were scheduled in.
    """

    virtual = True

    def __init__(self):
        self.t = 0
        self.heap = []
        self.count = 0

    def now(self):
        return self.t

    def call_at(self, t_us, callback, *args, key=0):
        self.count += 1
        heapq.heappush(self.heap, (int(t_us), key, self.count, callback, args))

    def pop_due(self):
        """The next event at or before T, or None."""
        if self.heap and self.heap[0][0] <= self.t:
            _, _, _, callback, args = heapq.heappop(self.heap)
            return callback, args
        return None


def describe_time(mode, rate):
    if mode == "real":
        return "real"
    return "virtual, as fast as it goes" if rate is None else "virtual, paced at %gx" % rate


def parse_time_mode(text):
    """`real`, `max` or `<k>x` into (mode, rate): rate None is as fast as it goes."""
    text = (text or "real").strip().lower()
    if text == "real":
        return "real", 1.0
    if text == "max":
        return "virtual", None
    if text.endswith("x"):
        try:
            rate = float(text[:-1])
        except ValueError:
            rate = 0.0
        if rate > 0:
            return "virtual", rate
    raise ValueError("--time takes real, max or <k>x, not %r" % text)


def log(msg):
    """One line of human-readable running commentary."""
    sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg))
    sys.stderr.flush()


def seeded_draw(seed, *parts):
    """A uniform draw in [0, 1) that is a function of the seed and `parts`
    alone: the same however often, and in whatever order, it is asked for."""
    key = ":".join(str(part) for part in (seed,) + parts)
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / 2.0 ** 64


def curve_sf(sf):
    """The spreading factor the error curve is taken at: a frame that named
    none, or one outside the datasheet's, at the slowest."""
    try:
        sf = int(sf)
    except (TypeError, ValueError):
        return SLOWEST_SF
    return sf if sf in SENSITIVITY_DB else SLOWEST_SF


def i0e(z):
    """The modified Bessel function I0(z) scaled by e^−z, for z ≥ 0."""
    if z < 15.0:
        total = term = 1.0
        quarter = z * z / 4.0
        k = 1
        while term > 1e-17 * total:
            term *= quarter / (k * k)
            total += term
            k += 1
        return total * math.exp(-z)
    return (1.0 + 1.0 / (8.0 * z) + 9.0 / (128.0 * z * z)) / math.sqrt(2.0 * math.pi * z)


@functools.lru_cache(maxsize=None)
def _symbol_error(sf, steps):
    m = 1 << sf
    a = math.sqrt(2.0 * m * 10.0 ** (steps * SNR_STEP_DB / 10.0))
    lo = max(0.0, a - 10.0)
    hi = a + math.sqrt(2.0 * math.log(m)) + 15.0
    n = 600
    h = (hi - lo) / n

    def correct_at(x):
        if x <= 0.0:
            return 0.0
        miss = math.exp(-x * x / 2.0)
        if miss >= 1.0:
            return 0.0
        return (x * math.exp(-(x - a) ** 2 / 2.0) * i0e(a * x)
                * math.exp((m - 1) * math.log1p(-miss)))

    total = correct_at(lo) + correct_at(hi)
    for i in range(1, n):
        total += (4.0 if i % 2 else 2.0) * correct_at(lo + i * h)
    return min(1.0, max(0.0, 1.0 - total * h / 3.0))


def symbol_error(sf, snr_db):
    """The chance an ideal receiver demodulates one symbol at spreading factor
    `sf` wrong, at `snr_db` over the noise in its bandwidth.

    The right bin is Rician and each of the 2^SF − 1 others Rayleigh:
    Pc = ∫ x·exp(−(x² + a²)/2)·I0(a·x)·(1 − exp(−x²/2))^(M−1) dx with
    a = √(2·2^SF·SNR), integrated by Simpson's rule. Kept per SNR_STEP_DB.
    """
    return _symbol_error(curve_sf(sf), round(snr_db / SNR_STEP_DB))


def frame_blocks(sf, bw, cr, payload_len, implicit, crc):
    """How a frame's symbols after the preamble fall into blocks (AN1200.13,
    the arithmetic of radio/src/toa.h): (the first block's symbols, the number
    of payload blocks after it, the symbols in each, whether the payload's
    coding rate corrects an error). The first block is always at 4/8, which
    corrects."""
    sf = curve_sf(sf)
    bw = float(bw or 125_000)
    cr = min(8, max(5, int(cr or 5))) - 4
    de = 1 if (1 << sf) / bw > 0.016 else 0
    bits = 8 * int(payload_len) - 4 * sf + 28 + (16 if crc else 0) - (20 if implicit else 0)
    blocks = max(math.ceil(bits / (4 * (sf - 2 * de))), 0)
    return FIRST_BLOCK_SYMBOLS, blocks, cr + 4, cr >= 3


def block_survives(p, n, correcting):
    """The chance a block of `n` symbols, each wrong with chance `p`, decodes.

    Interleaving gives each codeword one bit of each symbol, so one wrong
    symbol is at most one bit error per codeword: 4/7 and 4/8 correct it, 4/5
    and 4/6 only detect it. Two approximations, both pessimistic: the first
    block's reduced rate, which tolerates a symbol landing in the next bin, is
    taken as any other; and two wrong symbols are taken to defeat a correcting
    block, which they do only when both flip one bit position.
    """
    clean = (1.0 - p) ** n
    if correcting:
        clean += n * p * (1.0 - p) ** (n - 1)
    return clean


def lock_odds(p):
    """The chance a receiver finds a preamble and its sync word, each symbol
    wrong with chance `p`."""
    return (1.0 - p) ** (PREAMBLE_FOUND_SYMBOLS + SYNC_WORD_SYMBOLS)


def frame_odds(sf, snr_db, payload, cr, implicit, crc, bw):
    """The chance a frame arrives whole at a steady SNR, its curve taken as
    it is (no implementation loss): locked, its header read, every block of
    its payload decoded."""
    p = symbol_error(sf, snr_db)
    first, blocks, per_block, correcting = frame_blocks(sf, bw, cr, payload, implicit, crc)
    return (lock_odds(p) * block_survives(p, first, True)
            * block_survives(p, per_block, correcting) ** blocks)


@functools.lru_cache(maxsize=None)
def implementation_loss(sf):
    """How far the chip sits from the ideal curve at spreading factor `sf`, in
    dB: what puts the reference frame's packet error rate at ANCHOR_PER at the
    datasheet's threshold. The curve is read at SNR − this."""
    sf = curve_sf(sf)
    ref = REFERENCE_FRAME
    lo, hi = -10.0, 10.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        per = 1.0 - frame_odds(sf, SENSITIVITY_DB[sf] - mid, ref["payload"], ref["cr"],
                               ref["implicit"], ref["crc"], ref["bw"])
        if per > ANCHOR_PER:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def frame_key(sender, start_us, payload):
    """What a frame is to its draws: the channel's, not the run's numbering —
    its sender's node name, its start on the ether's clock and a hash of its
    bytes. Two runs that put the same frame on the air at the same instant
    draw the same for it, whatever else differs between them."""
    digest = hashlib.sha256((payload or "").encode()).hexdigest()[:16]
    return "%s@%d#%s" % (sender, int(start_us), digest)


def side_preamble_miss(preamble_symbols):
    """How often a side detector misses a preamble this many symbols long
    (SIDE_DETECTOR_PREAMBLE_MISS)."""
    points = sorted(SIDE_DETECTOR_PREAMBLE_MISS.items())
    try:
        n = float(preamble_symbols)
    except (TypeError, ValueError):
        return points[0][1]
    if n <= points[0][0]:
        return points[0][1]
    for (lo, p_lo), (hi, p_hi) in zip(points, points[1:]):
        if n <= hi:
            return p_lo + (p_hi - p_lo) * (n - lo) / (hi - lo)
    return points[-1][1]


def rician_db(k, draw):
    """One Rician power gain in dB of K factor `k`, unit mean, from two
    uniform draws `draw("i")` and `draw("q")` for the scattered part."""
    normal = statistics.NormalDist()
    x = normal.inv_cdf(min(max(draw("i"), 1e-12), 1.0 - 1e-12))
    y = normal.inv_cdf(min(max(draw("q"), 1e-12), 1.0 - 1e-12))
    steady = math.sqrt(k / (k + 1.0))
    scatter = math.sqrt(1.0 / (k + 1.0) / 2.0)
    gain = (steady + scatter * x) ** 2 + (scatter * y) ** 2
    return 10.0 * math.log10(max(gain, 1e-12))


def fade_knot(seed, name_lo, name_hi, k):
    """One knot of a pair's fading: a unit Gaussian drawn from the seed, the
    pair and the knot's number alone."""
    u = seeded_draw(seed, "fade", name_lo, name_hi, k)
    return statistics.NormalDist().inv_cdf(min(max(u, 1e-12), 1.0 - 1e-12))


def fade_unit(knot, t_s, coherence_s):
    """A unit-variance Gaussian process at `t_s` seconds from the knots
    `knot(k)` one coherence time apart: cos(π·u/2)·z_k + sin(π·u/2)·z_{k+1},
    continuous, the same whatever order it is asked in, uncorrelated from two
    coherence times apart."""
    pos = t_s / coherence_s
    k = math.floor(pos)
    u = pos - k
    return (math.cos(math.pi * u / 2.0) * knot(k)
            + math.sin(math.pi * u / 2.0) * knot(k + 1))


def same_class(sf_a, sf_b):
    """True when two spreading factors meet as one class, by the same-SF
    figure rather than the inter-SF matrix (`rejection_db`)."""
    return rejection_db(sf_a, sf_b) == SAME_SF_REJECTION_DB


def bench_stronger_odds(lead):
    """How often the stronger of two frames that met survives, by its lead."""
    if lead >= BENCH_CERTAIN_DB:
        return 1.0
    if lead <= BENCH_STRONGER_DB:
        return BENCH_STRONGER
    return BENCH_STRONGER + (1.0 - BENCH_STRONGER) * (
        (lead - BENCH_STRONGER_DB) / (BENCH_CERTAIN_DB - BENCH_STRONGER_DB))


def bench_outcome(seed, first, second, rsid, lead, locked):
    """Which of two frames that met survive at one receiver, as the bench saw.

    `first` and `second` are the frames' keys (`frame_key`) in the order they
    started, `rsid` the receiver's node name, `lead` the first's level over
    the second's there in dB, and the answer
    (the first survives, the second survives). A receiver `locked` on the
    first — following it when the second started after its preamble — never
    receives the second, however strong, and loses the first too unless the
    first is the stronger. Frames that started within a preamble of each
    other are the bench's table. The draws are the pair's and the
    receiver's, so both frames' verdicts and the lock read one outcome.
    """
    lo, hi = min(first, second), max(first, second)

    def draw(what):
        return seeded_draw(seed, lo, hi, rsid, what)

    if locked:
        if lead > BENCH_EQUAL_DB:
            return draw("stronger") < bench_stronger_odds(lead), False
        if lead < -BENCH_EQUAL_DB:
            return False, False
        return draw("equal") >= BENCH_BOTH_LOST, False
    if abs(lead) <= BENCH_EQUAL_DB:
        if draw("equal") < BENCH_BOTH_LOST:
            return False, False
        first_wins = draw("coin") < 0.5
        return first_wins, not first_wins
    if lead > 0:
        return draw("stronger") < bench_stronger_odds(lead), False
    return False, draw("stronger") < bench_stronger_odds(-lead)


def fspl_1m_db(freq_hz):
    """Free-space path loss over the first metre at this carrier, in dB: the
    log-distance model's anchor, for whatever computes synthetic ground's table."""
    return 20.0 * math.log10(4.0 * math.pi * max(freq_hz, 1.0) / SPEED_OF_LIGHT)


class Physics:
    """The medium's own settings beyond path loss: the receivers' noise
    figure, the slow fading of every link over time (its spread in dB and its
    coherence time; off unless given), fast fading per frame (a Rician K
    factor; off unless given), and whether frames on the air interfere with
    one another at all. The loss on a multi-SF receiver's side detectors is
    not a setting here: it is fixed, at the lock stage (`side_preamble_miss`).

    Without interference (`--no-interference`, an oracle, never so unless
    asked) a frame is judged against noise alone, as though nothing else were
    on the air, and a receiver is never taken off the frame it follows; a
    receiver still follows one frame at a time, cannot hear while it
    transmits, and senses the channel as before. What a run delivers with it
    less what it delivers without is what overlapping frames cost it. A
    setting at its default is left out of `as_dict`, so what a run records
    says only what was asked for."""

    def __init__(self, noise_figure_db=DEFAULT_NOISE_FIGURE_DB, interference=True,
                 fading_db=DEFAULT_FADING_DB, coherence_s=DEFAULT_COHERENCE_S,
                 rician_k=DEFAULT_RICIAN_K):
        self.noise_figure_db = float(noise_figure_db)
        self.interference = bool(interference)
        self.fading_db = float(fading_db)
        self.coherence_s = float(coherence_s)
        self.rician_k = None if rician_k is None else float(rician_k)
        if not self.fading_db >= 0:
            raise ValueError("fading is a spread in dB, not %g" % self.fading_db)
        if not self.coherence_s > 0:
            raise ValueError("the coherence time is a time in seconds, not %g"
                             % self.coherence_s)
        if self.rician_k is not None and not self.rician_k >= 0:
            raise ValueError("the Rician K factor is a power ratio, 0 or more, not %g"
                             % self.rician_k)

    @property
    def fading(self):
        """True when frames' levels fade."""
        return bool(self.fading_db) or self.rician_k is not None

    # Sergey's fork (sergeyculum) named this same spread/coherence/Rician
    # setting by an older model's terms (slow + fast terms, two draws per
    # link). Read-only aliases, so a script of its that reads a Physics back
    # (the sweep's fw_sim_mesh_oracle.py, _reach_graph_for.py) still finds
    # them; --slow-fading-db etc. alias the CLI flags themselves (`main`).
    slow_fading_db = property(lambda self: self.fading_db)
    slow_fading_s = property(lambda self: self.coherence_s)
    fast_fading_k = property(lambda self: self.rician_k)

    def describe(self):
        text = "noise figure %.1f dB" % self.noise_figure_db
        if self.fading_db:
            text += ", fading %.1f dB over %g s" % (self.fading_db, self.coherence_s)
        if self.rician_k is not None:
            text += ", Rician K %g per frame" % self.rician_k
        if not self.interference:
            text += ", no interference (an oracle)"
        return text

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(data.get("noise_figure_db", DEFAULT_NOISE_FIGURE_DB),
                   data.get("interference", True),
                   data.get("fading_db", data.get("slow_fading_db", DEFAULT_FADING_DB)),
                   data.get("coherence_s", data.get("slow_fading_s", DEFAULT_COHERENCE_S)),
                   data.get("rician_k", data.get("fast_fading_k", DEFAULT_RICIAN_K)))

    def as_dict(self):
        out = {"noise_figure_db": self.noise_figure_db}
        if self.fading_db != DEFAULT_FADING_DB:
            out["fading_db"] = self.fading_db
        if self.coherence_s != DEFAULT_COHERENCE_S:
            out["coherence_s"] = self.coherence_s
        if self.rician_k is not None:
            out["rician_k"] = self.rician_k
        if not self.interference:
            out["interference"] = False
        return out


class Station:
    """A station the ether has heard from, and the radio state it last stated."""

    def __init__(self, sid, addr, slots):
        self.sid = sid
        self.slots = slots
        self.states = {}        # slot -> the last `state` message for it
        self.ready = {}         # slot -> when its radio is ready in that state, on our clock
        self.locks = {}         # slot -> the Reception its demodulator follows
        self.tx_until = 0       # while its own frame is on the air, it is deaf
        self.on_idle = []       # callbacks for its next idle
        # The power its last frame went out at. A station states this per
        # transmission rather than in its `state`, so it is learned by
        # listening — and it is what `levels()` must answer with, or the map
        # would draw a reach the station does not have.
        self.power_dbm = DEFAULT_POWER_DBM
        self.init_conductor(addr)

    def init_conductor(self, addr):
        """Where it last wrote from, and the conductor's view of it that the
        ether reads: the last sequence number sent to it, whether it has said
        it is idle since, and the conductor time it next needs to run at
        (None: never, on its own). In a virtual-time run every station is a
        CoreStation, whose view is the core's; a real-time run has no
        conductor, and these stay as they start."""
        self.addr = addr
        self.seq = 0
        self.idle = False
        self.until = None
        self.told = 0           # the T it was last sent, which is the T it has
        self.asking = 0         # TCP writes it is waiting to be let make
        self.asked_at = 0       # its seq when it last asked
        self.slow_idles = 0     # idles that took the busy watchdog's time or more
        self.lines = False      # takes several messages to a datagram (its hello)

    def state(self, slot):
        return self.states.get(slot)

    def listening(self, slot):
        """True when this station's slot last said it was receiving or sensing."""
        st = self.states.get(slot)
        return bool(st) and st.get("mode") in ("RX", "CAD")

    def sensing(self, slot):
        """True when this station's slot last said it was in CAD."""
        st = self.states.get(slot)
        return bool(st) and st.get("mode") == "CAD"


class Channel:
    """Bytes on their way from one part of a virtual run to another.

    What the testbed types at a station's console (`tty/<sid>`), and a TCP
    connection between two stations, one way (`tcp/<a>><b>`, the writer's
    address and port, then the reader's). `writer` and `reader` are station
    ids, None for the testbed. `written` and `taken` count the bytes each end
    has said it put in and took out; while `written` is ahead, the reader has
    input it has not seen, and T waits.
    """

    def __init__(self, key, writer, reader):
        self.key = key
        self.writer = writer
        self.reader = reader
        self.written = 0
        self.taken = 0
        self.timer = None       # the unread grace, while it holds T

    def holding(self):
        return self.written > self.taken


class Frame:
    """A transmission in flight, on the ether's clock.

    A frame carries every other transmission it shared air and band with,
    whatever their spreading factors, and the level it arrives at each
    station, computed once on first asking so a table replaced mid-frame
    does not change a frame already on the air. The verdict is not one of
    its properties: each receiver rules at its own rx_end.
    """

    def __init__(self, eid, fid, sid, msg, start_us, end_us, pre_us, hdr_us):
        self.eid = eid          # the ether's own number, unique across stations
        self.fid = fid          # the transmitter's, unique only to it
        self.sid = sid
        self.freq = msg.get("freq")
        self.bw = msg.get("bw")
        self.sf = msg.get("sf")
        self.sync = msg.get("sync")
        self.power_dbm = msg.get("power_dbm", DEFAULT_POWER_DBM)
        self.preamble = msg.get("pre")      # its preamble, in symbols, as the sender set it
        self.payload = msg.get("payload", "")
        # What the error curve needs of the frame's shape: the payload is
        # passed through as it came and never read, only its length taken.
        self.cr = msg.get("cr", 5)
        self.implicit = msg.get("hdr") == "implicit"
        self.crc = msg.get("crc", True)
        self.pre_symbols = msg.get("pre", 8)
        try:
            self.payload_len = len(base64.b64decode(self.payload or ""))
        except (binascii.Error, TypeError, ValueError):
            self.payload_len = 0
        # What a receiver's stated radio is matched against, kept for the
        # slots that start listening while this frame is on the air.
        self.radio = {key: msg.get(key) for key in ("freq",) + MATCH_KEYS}
        self.start_us = start_us
        self.end_us = end_us
        self.pre_us = pre_us
        self.hdr_us = hdr_us
        self.interferers = []   # frames that shared this one's band and air
        self.receivers = []     # (sid, slot, level) for each decoding receiver
        self.receptions = {}    # (sid, slot) -> its Reception there
        self.levels = {}        # rsid -> dBm there, or None: never heard
        self.rice = {}          # rsid -> its fast fading there, dB (`Ether.rice_db`)
        self.key = None         # what its draws are keyed on (`Ether.key_of`)

    def preamble_left(self, now):
        """True when a receiver that starts listening at `now` still has
        PREAMBLE_FOUND_SYMBOLS of this frame's preamble to find it by."""
        try:
            symbol_us = (1 << int(self.sf)) / float(self.bw) * 1e6
        except (TypeError, ValueError, ZeroDivisionError):
            return False
        preamble_end = self.pre_us - SYNC_SYMBOLS * symbol_us
        return preamble_end - now >= PREAMBLE_FOUND_SYMBOLS * symbol_us

    def shares_air(self, other):
        """True when the two frames overlap in band and in time."""
        return (in_band(self.freq, self.bw, other.freq, other.bw)
                and self.start_us < other.end_us
                and other.start_us < self.end_us)


class Reception:
    """One receiver slot decoding one frame, from rx_begin to rx_end.

    `lost` is set when a louder frame took the receiver off this one at its
    own preamble, or when this one arrived while the receiver was following
    another it did not lead: the rx_end still goes out, as `crc`, and the
    chip, which follows only the last frame it was begun on, drops it.
    `abandoned` is set when the receiver left RX, for anything but its own
    transmission, while following this frame: nothing was received, so no
    rx_end goes out and nothing is recorded as received or lost.
    `hdr_ok` is the header's fate, decided at the lock; `ended` is set when
    the reception closed at the header for it, and the receiver followed the
    frame no further.
    """

    def __init__(self, frame, rsid, slot, level, lost=False, taken_at=None, det=0):
        self.frame = frame
        self.rsid = rsid
        self.slot = slot
        self.level = level
        self.det = det          # the detector that found it: 0 the main one, k side k
        self.lost = lost
        self.abandoned = False
        self.hdr_ok = True
        self.ended = False
        # When the receiver took this frame, and when a later one took it off
        # it: the bench rule asks whether it was following the frame when a
        # second one started.
        self.taken_at = None if lost else taken_at
        self.lost_at = None

    def followed_at(self, t):
        """True when the receiver was following this frame at instant `t`."""
        return (self.taken_at is not None and self.taken_at <= t
                and (self.lost_at is None or self.lost_at > t)
                and not (self.ended and t >= self.frame.hdr_us))


class Ether(asyncio.DatagramProtocol):
    """The UDP endpoint: parses station messages and delivers frames.

    Whatever drives a run gives it the loss tables with `set_losses()` and
    subscribes to `on_tx`, `on_rx` and `on_station` to watch the air. Each
    callback takes the arguments named beside it below and returns nothing;
    exceptions raised in one are logged and swallowed, because a page that has
    gone away must not be able to stop the medium.
    """

    def __init__(self, record_path, physics=None, seed=None, time_mode="real",
                 pairwise=False, epoch=None, bench_capture=False):
        self.transport = None
        self.loop = asyncio.get_event_loop()
        self.mode, self.rate = parse_time_mode(time_mode)
        if self.mode == "virtual" and type(self) is Ether:
            raise ValueError("a virtual-time run is conducted by ether_core: open_ether() "
                             "makes its ether (sim-mesh build ether)")
        self.clock = VirtualClock() if self.mode == "virtual" else RealClock(self.loop)
        # The wall-clock microseconds T = 0 stands for, so every station's
        # time() agrees with every other's. A virtual run may be given one,
        # so that two runs of the same network put the same wall clock, and
        # so the same timestamps, into their stations.
        self.epoch = int(time.time() * 1_000_000) - self.clock.now()
        if epoch is not None and self.mode == "virtual":
            self.epoch = int(epoch)
        self.expected = set()       # stations started and not yet heard from
        self.holds = 0              # the testbed's own work in hand at the T it has
        self.floors = {}            # sid -> "tool" | "station": a testbed tool's session with it
        self.joins = {}             # sid -> future, done at its hello (expect)
        self.join_waited = set()    # sids whose join someone is waiting on (joined)
        self.settling = []          # callbacks for when the loop is next quiet
        self.settle_turns = 0
        self.channels = {}          # key -> Channel with bytes in it, or owed some
        self.unread = 0             # channels holding T
        self.endpoints = {}         # "addr:port" -> the station a TCP endpoint is
        self.asks = []              # (sid, arrival, channel, n, reply, addr): writes waiting
        self.on_drain = None        # (sids, done): read what they printed, then done();
                                    # or False, done not called: none of them printed
        self.drains_watched = False     # on_drain is stations.printed over the watched consoles
        self.arrivals = 0
        self.pace_timer = None
        self.pace_origin = None     # (wall seconds, T) a paced run is measured from
        self.barriers = 0           # times T has moved
        self.runs = 0               # `run`s sent: a station woken, once each
        self.resends = 0            # messages sent again to a station that missed them
        self.standing = 0           # steps in a row that left T where it was
        self.physics = physics or Physics()
        self.pairwise = bool(pairwise)
        if bench_capture and self.pairwise:
            raise ValueError("bench capture is a variant of the receiver-centred rule, "
                             "not of the pairwise one")
        self.bench_capture = bool(bench_capture)
        self.stations = {}          # sid -> Station
        self.tables = {}            # band name -> slt.Table
        self.names = {}             # sid -> node name, the table's index
        self.gains = {}             # sid -> antenna gain in dBi
        self.frames = []            # frames still in flight or just ended
        self.knots = {}             # (name, name, k) -> a fading knot (`fade_db`)
        self.next_eid = 0           # the ether's own frame numbering
        self.seed = seed if seed is not None else random.randrange(1 << 31)
        self.on_tx = None           # (sid, eid, freq, t_start, t_end)
        self.on_rx = None           # (sid, rsid, eid, verdict, level, cause)
        self.on_station = None      # (sid, state)
        self.record = None
        if record_path is not None:
            self.record = open(record_path, "a", encoding="utf-8", buffering=1)
            self.record.write("# %s\tether record: stamp\tdir\tsid\tjson\n"
                              % wall_stamp())

    # ---- clock ---------------------------------------------------------

    def now(self):
        """The ether's clock in microseconds: the loop's in a real-time run,
        conductor time T in a virtual one."""
        return self.clock.now()

    def call_at(self, t_us, callback, *args, key=0):
        """Run `callback(*args)` at `t_us` on the ether's clock."""
        return self.clock.call_at(t_us, callback, *args, key=key)

    async def sleep(self, seconds):
        """Wait `seconds` on the ether's clock: conductor time in a virtual run.

        In a virtual run the wait ends at its instant of T and T stays there
        until the caller has done what it woke up to do: the instant holds T
        from when it falls due until the loop has run everything the caller
        and whatever it started made ready (`settle()`). So a station started,
        or a line typed, after a sleep is started or typed at the sleep's T,
        whatever the pace.
        """
        if not self.clock.virtual:
            await asyncio.sleep(seconds)
            return
        done = self.loop.create_future()
        held = []

        def due():
            if not done.done():
                self.holds += 1
                held.append(True)
                done.set_result(None)

        self.clock.call_at(self.clock.t + int(seconds * 1_000_000), due, key=-1)
        self.kick()
        try:
            await done
        finally:
            if held:
                self.settle(self.release)

    def release(self):
        """One hold on T is let go."""
        self.holds -= 1
        self.kick()

    # ---- a testbed tool talking to a station ------------------------------

    def tool_session(self, sid):
        """A tool of the testbed's is about to talk to station `sid` on the
        wall clock, through the station's host door: T stays where it is while
        the tool has the floor, and runs while the station has it (from when
        it has read what the tool wrote until it has answered, `floor`), so
        each line is read and answered at a T the run decides, however long
        the tool takes on the host. Returns the function that ends the
        session, which lets T go once what that set going has run."""
        if not self.clock.virtual:
            return lambda: None
        if sid in self.floors:
            raise RuntimeError("station %d is already in a tool session" % sid)
        self.floors[sid] = "tool"
        self.holds += 1

        def end():
            floor = self.floors.pop(sid, None)
            if floor == "tool":
                self.settle(self.release)
        return end

    def station_idle(self, sid):
        """Whether station `sid` has said it is idle since it was last told
        anything: done with the instant it is at. True in a real-time run, and
        for a station not in the run."""
        if not self.clock.virtual:
            return True
        station = self.stations.get(sid)
        return station is None or bool(station.idle)

    def recv_floor(self, sid, addr, msg):
        """The station's host door changed hands. It said something, so it is
        not idle until it says so; a floor outside a tool session is no
        testbed's and holds nothing."""
        station = self.stations.get(sid)
        if station is None:
            return
        station.addr = addr
        self.mark(station, False)
        floor, to = self.floors.get(sid), msg.get("to")
        if to == "station":
            # It has read a host's bytes and waits to be brought to the run's
            # T before it takes them: an idle station has the T it was last
            # told, which the run may have long left behind.
            self.send(sid, {"type": "run", "floor": 1})
        if floor == "tool" and to == "station":
            self.floors[sid] = "station"
            self.holds -= 1
        elif floor == "station" and to == "tool":
            self.floors[sid] = "tool"
            self.holds += 1

    def settle(self, callback):
        """Run `callback` once the loop has nothing else ready to run.

        A coroutine woken by a future runs a loop turn later, and what it
        starts (a task, a gather) a turn after that; checking the loop's ready
        queue each turn follows the whole chain, for up to SETTLE_TURNS turns.
        One check serves every callback waiting, so they do not keep each
        other's checks from ever finding the loop quiet.
        """
        self.settling.append(callback)
        if len(self.settling) == 1:
            self.settle_turns = 0
            self.loop.call_soon(self.settle_check)

    def settle_check(self):
        ready = getattr(self.loop, "_ready", None)
        if ready and self.settle_turns < SETTLE_TURNS:
            self.settle_turns += 1
            self.loop.call_soon(self.settle_check)
            return
        batch, self.settling = self.settling, []
        for callback in batch:
            callback()

    # ---- the conductor --------------------------------------------------
    #
    # In a virtual-time run the barrier is ether_core's (CoreEther below):
    # T, the stations' numbering and idles, the resends, and when T may move.
    # What stays here is the ether's side of a station starting, joining and
    # leaving, and what the core calls back into at the barrier: `paced`,
    # `run_due` and `take_held`.

    def expect(self, sid):
        """A station is starting: T waits for it to say hello and then idle.
        `joined(sid)` is done at that hello."""
        if self.clock.virtual:
            self.expected.add(sid)
            old = self.joins.get(sid)
            if old is None or old.done():
                self.joins[sid] = self.loop.create_future()

    def joined(self, sid):
        """A future done once station `sid`, expected, has said hello. At that
        hello T is held until what the future woke has run, as after a sleep,
        so what the testbed does next to the station it does at the hello's T
        however soon it gets there on the host. Done at once in a real-time
        run, or for a station not expected."""
        future = self.joins.get(sid)
        if future is None:
            future = self.loop.create_future()
            future.set_result(None)
        elif not future.done():
            self.join_waited.add(sid)
        return future

    def leave(self, sid):
        """A station has stopped: T no longer waits for it."""
        self.expected.discard(sid)
        if self.forget(sid):
            log("station %d left" % sid)
        self.kick()

    def forget(self, sid):
        """Drop a station and everything it said; True if there was one."""
        station = self.stations.pop(sid, None)
        if station is None:
            return False
        self.asks = [a for a in self.asks if a[0] != sid]
        for key, channel in list(self.channels.items()):
            if sid in (channel.writer, channel.reader):
                self.drop_channel(channel)
        self.endpoints = {e: s for e, s in self.endpoints.items() if s != sid}
        return True

    def waiting_on(self):
        """The stations T is waiting for: started and unheard, not idle, or
        with input they have not read (the testbed, when it is the reader, is
        not named)."""
        unread = {c.reader for c in self.channels.values()
                  if c.holding() and c.reader is not None}
        return sorted(self.expected) + sorted(
            sid for sid, st in self.stations.items() if not st.idle or sid in unread)

    def kick(self):
        """Move T as far as the barrier lets it, now: CoreEther's. A real-time
        run has no T to move."""

    def paced(self, t):
        """True when a paced run may move to `t` now; else a timer comes back."""
        if self.rate is None:
            return True
        wall = self.loop.time()
        if self.pace_origin is None:
            self.pace_origin = (wall, self.clock.t)
        origin_wall, origin_t = self.pace_origin
        target = origin_wall + (t - origin_t) / 1_000_000.0 / self.rate
        if target <= wall:
            if wall - target > 0.25:
                # Behind by more than a beat: carry on from here rather than
                # racing to catch up.
                self.pace_origin = (wall, self.clock.t)
            return True
        if self.pace_timer is None:
            def resume():
                self.pace_timer = None
                self.kick()
            self.pace_timer = self.loop.call_at(target, resume)
        return False

    def run_due(self):
        """Everything on the ether's clock due at T, in its order."""
        while True:
            due = self.clock.pop_due()
            if due is None:
                break
            callback, args = due
            callback(*args)

    def take_held(self, batch):
        """The medium takes what stations said while T stood still: `batch`,
        (sid, addr, msg) in station order.

        A message that cannot be taken is logged and dropped on its own, as
        a datagram is in a real-time run: the batch is every station's at
        this instant, and one malformed `tx` must not silence the rest."""
        for sid, addr, msg in batch:
            self.write_record("in", sid, msg)
            if sid not in self.stations:
                continue
            kind = msg.get("type")
            try:
                if kind == "state":
                    self.recv_state(sid, addr, msg)
                elif kind == "tx":
                    self.recv_tx(sid, addr, msg)
            except Exception as err:            # noqa: BLE001 - see docstring
                log("station %d's %s could not be taken, dropped: %r" % (sid, kind, err))

    # ---- channels: input that does not come over the air -----------------

    def console(self, sid):
        """What the testbed types at station `sid`'s console."""
        key = "tty/%d" % sid
        channel = self.channels.get(key)
        if channel is None:
            channel = self.channels[key] = Channel(key, None, sid)
        return channel

    def typed(self, sid, total):
        """The testbed is writing station `sid`'s console: `total` bytes, in
        all, since it last started it. T waits until the station has read
        them."""
        if self.clock.virtual:
            self.account(self.console(sid), wrote=total, totals=True)

    def sync(self, sid, done):
        """Run `done` once station `sid` has the T the run has and nothing in
        hand.

        A station is only told T when something happens to it, so one that
        has been idle while T moved still has the T it was last told; and one
        that has been told something has not necessarily taken it yet. A line
        typed at its console goes out once `done` runs, so the station reads
        it at this T, with no message of the ether's landing halfway through.
        That is now in a real-time run; CoreEther's waits in a virtual one.
        """
        done()

    def drained(self):
        """The testbed has read what the stations printed (`on_drain`): T may
        move once what that set going has run."""
        self.settle(self.release)

    def endpoint_sid(self, endpoint):
        """The station a TCP endpoint ("addr:port") belongs to, or None: one it
        has reported from, else the station at that address."""
        sid = self.endpoints.get(endpoint)
        if sid is not None:
            return sid
        addr = endpoint.rsplit(":", 1)[0]
        for sid, station in self.stations.items():
            if station.addr and station.addr[0] == addr:
                return sid
        return None

    def recv_listen(self, sid, msg):
        """A station listens at a TCP endpoint: a connection to it is that
        station's, though another station of the run shares its address."""
        at = msg.get("at")
        if sid in self.stations and isinstance(at, str):
            self.endpoints[at] = sid

    def recv_io(self, sid, addr, msg):
        """A station's count of bytes it put into or took out of a channel.

        A write that asks (`go`) is answered, at the address it asked from,
        once its reader is in step (`sync()`); every such ask is answered,
        counted or not, since the writer waits for it.
        """
        go = msg.get("go")
        channel = self.io(sid, msg)
        if go is None:
            return
        reply = json.dumps({"type": "go", "go": go}, separators=(",", ":")).encode("utf-8")
        if channel is None:
            if self.transport is not None:
                self.transport.sendto(reply, addr)
            return
        station = self.stations[sid]
        station.asking += 1
        station.asked_at = station.seq
        self.arrivals += 1
        self.asks.append((sid, self.arrivals, channel, msg["n"], reply, addr))
        self.kick()

    def quiet(self):
        """True when nothing in the run is at work: every station idle, or
        waiting for a go-ahead it has asked for since it was last told
        anything, and no input unread but by a station that is waiting so (a
        station's thread held up in a write can keep its others from
        reading)."""
        if self.expected or self.holds:
            return False

        def waiting(st):
            return bool(st.asking) and st.seq == st.asked_at

        if not all(st.idle or waiting(st) for st in self.stations.values()):
            return False
        for channel in self.channels.values():
            if channel.holding():
                reader = self.stations.get(channel.reader)
                if reader is None or not waiting(reader):
                    return False
        return True

    def answer_ask(self):
        """Let the first write asked for, in station order, go ahead.

        Asks are answered only when the run is quiet, one at a time, and in
        station order, as the barrier takes what stations say: two stations
        writing to each other at one instant then go in the same order every
        run. A reader that has not been told the run's T is told it first.
        """
        ask = min(self.asks, key=lambda a: (a[0], a[1]))
        sid, _, channel, n, reply, addr = ask
        reader = self.stations.get(channel.reader)
        if reader is not None and reader.idle and reader.told < self.clock.t:
            self.send(channel.reader, {"type": "run"})
            if not reader.idle:
                return
        self.asks.remove(ask)
        writer = self.stations.get(sid)
        if writer is not None:
            writer.asking -= 1
        if self.channels.get(channel.key) is not channel:
            self.channels[channel.key] = channel
        self.account(channel, wrote=n)
        if self.transport is not None:
            self.transport.sendto(reply, addr)

    def io(self, sid, msg):
        """Count what a station reports; the reader of a TCP write that
        counts, else None.

        The console's counts are running totals since the process started;
        a TCP connection's are the bytes of one call. A TCP connection counts
        only when both of its ends are stations of the run; the testbed's own
        connections to a station (its web proxy) are outside it.
        """
        if sid not in self.stations:
            return None
        took = msg.get("type") == "read"
        ch = msg.get("ch")
        if ch == "tty":
            total = msg.get("total")
            if took and isinstance(total, int):
                self.account(self.console(sid), took=total, totals=True)
            return
        n = msg.get("n")
        if not isinstance(ch, str) or not ch.startswith("tcp/") or not isinstance(n, int):
            return
        src, _, dst = ch[4:].partition(">")
        if not src or not dst:
            return
        if took:
            self.endpoints[dst] = sid
            writer, reader = self.endpoint_sid(src), sid
        else:
            self.endpoints[src] = sid
            writer, reader = sid, self.endpoint_sid(dst)
        if writer is None or reader is None:
            return
        channel = self.channels.get(ch)
        if channel is None:
            channel = self.channels[ch] = Channel(ch, writer, reader)
        if took:
            self.account(channel, took=n)
            return None
        if msg.get("go") is None:
            self.account(channel, wrote=n)
        return channel

    def account(self, channel, wrote=0, took=0, totals=False):
        """Bytes into or out of a channel; T waits while its reader is behind.

        With `totals` the counts are running totals, and only ever move
        forward; otherwise they are what one call moved (a writer's may be
        negative: what it announced and the call did not take). A station
        that has just caught up with its input is at work on it, so it is
        sent the T it already has (its clock does not move under that work)
        and owes an idle for it, as for any message.
        """
        was = channel.holding()
        if totals:
            channel.written = max(channel.written, wrote)
            channel.taken = max(channel.taken, took)
        else:
            channel.written += wrote
            channel.taken += took
        now = channel.holding()
        if now and not was:
            self.unread += 1
            channel.timer = self.loop.call_later(UNREAD_GRACE_S, self.unread_grace, channel)
        elif was and not now:
            self.unread -= 1
            if channel.timer is not None:
                channel.timer.cancel()
                channel.timer = None
        if not totals and channel.written == channel.taken:
            self.channels.pop(channel.key, None)
        if took and was and not now:
            reader = self.stations.get(channel.reader)
            if reader is not None:
                self.send(channel.reader, {"type": "run", "t": reader.told})
        self.kick()

    def unread_grace(self, channel):
        """A reader that has not taken its input in UNREAD_GRACE_S of wall is
        not waiting for it: T goes on without it."""
        channel.timer = None
        if self.channels.get(channel.key) is not channel or not channel.holding():
            return
        log("%s: %d bytes unread after %.1f s; T goes on" % (
            channel.key, channel.written - channel.taken, UNREAD_GRACE_S))
        channel.taken = channel.written
        self.unread -= 1
        if not channel.key.startswith("tty/"):
            self.channels.pop(channel.key, None)
        self.kick()

    def drop_channel(self, channel):
        if channel.holding():
            self.unread -= 1
        if channel.timer is not None:
            channel.timer.cancel()
            channel.timer = None
        self.channels.pop(channel.key, None)

    # ---- the testbed's console readers --------------------------------------

    def console_marks(self, fd):
        """The marks a reader of the console on `fd` keeps for
        stations.printed, or None for the testbed's own: a real-time run has
        no barrier to ask on_drain at, and looks at none of them."""
        return None

    def watch(self, sid, marks):
        """Station `sid`'s console reader, from this start of it on."""

    def unwatch(self, sid, marks):
        """Station `sid`'s console reader `marks` has been let go."""

    # ---- the loss table -------------------------------------------------

    def set_losses(self, tables_by_band, sid_by_name, gains_db_by_sid=None):
        """Replace every loss, name and gain at once.

        `tables_by_band` maps a band name ("433", "868", "915") to its
        slt.Table; `sid_by_name` maps each node name in the tables to the
        station id it runs as; `gains_db_by_sid` maps a station id to its
        antenna gain in dBi, 0 where absent. Everything is swapped in one
        assignment each, between two events, so no frame is ruled on half an
        old table and half a new one. A station id with no name is not in
        the medium: it hears nothing and nothing hears it.
        """
        tables = dict(tables_by_band or {})
        names = {int(sid): name for name, sid in (sid_by_name or {}).items()}
        gains = {int(sid): float(g) for sid, g in (gains_db_by_sid or {}).items()}
        self.tables, self.names, self.gains = tables, names, gains

    def replace_losses(self, band, table):
        """Put in one band's whole table, as it stands, in place of the old one."""
        tables = dict(self.tables)
        if table is None:
            tables.pop(band, None)
        else:
            tables[band] = table
        self.tables = tables

    def update_node(self, name, rows_by_band):
        """One node's row and column recomputed: the old ones stand until now.

        `rows_by_band` maps a band to `(from_db, to_db)`, each a dict of other
        node name to loss in dB at the band's centre: from this node to
        that one, and from that one to this. The band's table is copied, the
        cells written into the copy and the copy put in place, so a reader
        never sees the row half written. A band not named keeps its table.
        """
        tables = dict(self.tables)
        for band, (from_db, to_db) in (rows_by_band or {}).items():
            old = tables.get(band)
            if old is None or name not in old.index:
                continue
            new = slt.Table(old.header, array("f", old.loss), array("B", old.flags),
                            array("H", old.samples))
            for other, loss in (from_db or {}).items():
                if other in new.index and other != name:
                    new.loss[new.cell(name, other)] = loss
            for other, loss in (to_db or {}).items():
                if other in new.index and other != name:
                    new.loss[new.cell(other, name)] = loss
            tables[band] = new
        self.tables = tables

    def set_gain(self, sid, gain_db):
        """One station's antenna gain, in dBi."""
        gains = dict(self.gains)
        gains[int(sid)] = float(gain_db)
        self.gains = gains

    def clear(self):
        """Forget every table, name and gain: nothing hears anything."""
        self.set_losses({}, {}, {})

    def path_loss(self, a, b, freq_hz):
        """The dB from station a to station b at this carrier, or None: never heard."""
        name_a, name_b = self.names.get(a), self.names.get(b)
        if name_a is None or name_b is None or a == b:
            return None
        table = self.tables.get(slt.band_for(freq_hz))
        if table is None or name_a not in table.index or name_b not in table.index:
            return None
        loss = table.at(name_a, name_b, freq_hz)
        return loss if math.isfinite(loss) else None

    def level(self, tx_sid, rx_sid, freq_hz, power_dbm=DEFAULT_POWER_DBM):
        """The level in dBm a frame from `tx_sid` arrives at `rx_sid`, or None."""
        loss = self.path_loss(tx_sid, rx_sid, freq_hz)
        if loss is None:
            return None
        return (float(power_dbm) + self.gains.get(tx_sid, 0.0)
                + self.gains.get(rx_sid, 0.0) - loss)

    def level_of(self, frame, rsid):
        """The level `frame` arrives at `rsid`, fixed on first asking: the
        table's, unfaded (`level_at` adds the pair's slow fade and the
        frame's own fast one)."""
        if rsid not in frame.levels:
            frame.levels[rsid] = self.level(frame.sid, rsid, frame.freq, frame.power_dbm)
        return frame.levels[rsid]

    def noise(self, bw_hz):
        """A receiver's noise floor in dBm for this bandwidth: kTB plus the figure."""
        return (THERMAL_DBM_PER_HZ + 10.0 * math.log10(max(float(bw_hz or 125_000), 1.0))
                + self.physics.noise_figure_db)

    def sensitivity(self, sf):
        """The datasheet's SNR for this spreading factor, in dB: where the
        reference frame arrives all but ANCHOR_PER of the time."""
        return SENSITIVITY_DB.get(sf, SLOWEST_SENSITIVITY_DB)

    def audible(self, level, bw_hz, sf=None):
        """True when a frame at this level, unfaded, clears its modem's
        datasheet threshold over noise: reachable, as the map shows it."""
        if level is None:
            return False
        return level >= self.noise(bw_hz) + self.sensitivity(sf)

    def symbol_error_at(self, frame, level):
        """The chance one symbol of `frame` is demodulated wrong at `level`:
        the curve at its SNR less the chip's implementation loss."""
        return symbol_error(frame.sf, level - self.noise(frame.bw)
                            - implementation_loss(frame.sf))

    def fade_db(self, a, b, t_us):
        """How far the link between stations a and b sits from the table's
        level at `t_us`, in dB: σ times a unit Gaussian process over the
        ether's clock, one per unordered pair of node names (`fade_unit`).
        Nothing without fading."""
        sigma = self.physics.fading_db
        if not sigma:
            return 0.0
        name_a, name_b = self.names.get(a), self.names.get(b)
        if name_a is None or name_b is None:
            return 0.0
        lo, hi = sorted((str(name_a), str(name_b)))

        def knot(k):
            key = (lo, hi, k)
            z = self.knots.get(key)
            if z is None:
                z = self.knots[key] = fade_knot(self.seed, lo, hi, k)
            return z

        return sigma * fade_unit(knot, t_us / 1e6, self.physics.coherence_s)

    def node(self, sid):
        """A station's node name, which outlives its restarts; its id where
        the nodeset names none."""
        return self.names.get(sid, sid)

    def key_of(self, frame):
        """What `frame`'s draws are keyed on (`frame_key`), made on first asking."""
        if frame.key is None:
            frame.key = frame_key(self.node(frame.sid), frame.start_us, frame.payload)
        return frame.key

    def draw(self, frame, rsid, slot, what):
        """One of a frame's draws at a receiving slot: from the seed and the
        channel — frame, receiver, slot — alone."""
        return seeded_draw(self.seed, self.key_of(frame), self.node(rsid), slot, what)

    def rice_db(self, frame, rsid):
        """The frame's fast fading at `rsid`, in dB: one Rician draw for the
        whole frame at that antenna. Nothing without a K factor."""
        k = self.physics.rician_k
        if k is None:
            return 0.0
        if rsid not in frame.rice:
            key, node = self.key_of(frame), self.node(rsid)
            frame.rice[rsid] = rician_db(
                k, lambda part: seeded_draw(self.seed, key, node, "rice", part))
        return frame.rice[rsid]

    def level_at(self, frame, rsid, t_us):
        """The level `frame` arrives at `rsid` at instant `t_us`: the table's,
        faded slowly by the pair's process and fast by the frame's own draw.
        None stays None."""
        level = self.level_of(frame, rsid)
        if level is None:
            return None
        return level + self.fade_db(frame.sid, rsid, t_us) + self.rice_db(frame, rsid)

    def knot_cuts(self, lo_us, hi_us):
        """The fading's knots strictly inside (lo, hi): where a piece of air
        is cut so it never spans two. None without fading, where the level
        does not move."""
        if not self.physics.fading_db:
            return []
        step = self.physics.coherence_s * 1e6
        k = math.floor(lo_us / step) + 1
        cuts = []
        while k * step < hi_us:
            if k * step > lo_us:
                cuts.append(k * step)
            k += 1
        return cuts

    def locks_on(self, frame, rsid, slot, now, det=0):
        """The first stage: whether a receiver finds the frame's preamble and
        sync word, at the frame's faded level at `now`; a side detector (`det`
        1 and up) also misses a short preamble at its own rate
        (`side_preamble_miss`). One draw per frame and slot, so a lock on
        time, a late one and a CAD read the same."""
        level = self.level_at(frame, rsid, now)
        if level is None:
            return False
        odds = lock_odds(self.symbol_error_at(frame, level))
        if det:
            odds *= 1.0 - side_preamble_miss(frame.pre_symbols)
        return self.draw(frame, rsid, slot, "lock") < odds

    def header_ok(self, frame, rsid, slot):
        """The second stage, decided at the lock: whether the first block,
        the explicit header, decodes at its faded level. A frame without a
        header has none to fail; its first block is the payload's."""
        if frame.implicit:
            return True
        level = self.level_at(frame, rsid, (frame.pre_us + frame.hdr_us) / 2.0)
        p = self.symbol_error_at(frame, level)
        return (self.draw(frame, rsid, slot, "hdr")
                < block_survives(p, FIRST_BLOCK_SYMBOLS, True))

    def payload_blocks(self, frame, rsid):
        """The payload's blocks at one receiver, as (faded level, symbols,
        correcting): spread evenly from the header's end to the frame's, and
        the first block before them when there is no header."""
        first, count, per_block, correcting = frame_blocks(
            frame.sf, frame.bw, frame.cr, frame.payload_len, frame.implicit, frame.crc)
        out = []
        if frame.implicit:
            out.append((self.level_at(frame, rsid, (frame.pre_us + frame.hdr_us) / 2.0),
                        first, True))
        span = (frame.end_us - frame.hdr_us) / count if count else 0.0
        for i in range(count):
            out.append((self.level_at(frame, rsid, frame.hdr_us + (i + 0.5) * span),
                        per_block, correcting))
        return out

    def payload_survives(self, reception, blocks):
        """The third stage, at the frame's end: whether every block decodes.
        One draw per frame and slot."""
        frame = reception.frame
        odds = 1.0
        for level, n, correcting in blocks:
            odds *= block_survives(self.symbol_error_at(frame, level), n, correcting)
        return self.draw(frame, reception.rsid, reception.slot, "payload") < odds

    def levels(self, sid, freq_hz, bw_hz=125_000, power_dbm=None, sf=None):
        """What every other station in the table would hear from `sid` on this
        carrier, as sid -> dBm: only those that could decode it.

        The power is the one that station last transmitted at, so the answer
        describes the station as it is rather than as a constant assumed it
        would be. A station that has never transmitted has no such figure and
        is asked about at the default.
        """
        station = self.stations.get(sid)
        if power_dbm is None:
            power_dbm = station.power_dbm if station else DEFAULT_POWER_DBM
        if sf is None and station is not None:
            state = station.state(0)
            sf = state.get("sf") if state else None
        heard = {}
        for other in self.names:
            if other == sid:
                continue
            level = self.level(sid, other, freq_hz, power_dbm)
            if self.audible(level, bw_hz, sf):
                heard[other] = level
        return heard

    # ---- events ---------------------------------------------------------

    def raise_event(self, callback, *args):
        """Hand one event to a subscriber; a broken subscriber is not the medium's
        problem, so it is logged and the frame carries on."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as err:                # noqa: BLE001 - see docstring
            log("subscriber raised %r" % (err,))

    # ---- plumbing ------------------------------------------------------

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.write_record("in", None, {"raw": repr(data[:200])})
            return
        if not isinstance(msg, dict):
            return
        sid = msg.get("sid")
        kind = msg.get("type")
        # What is held for the barrier (a virtual run's state and tx) goes into
        # the record when it is taken, in station order (take_held): two
        # stations saying something at one instant arrive in whichever order
        # the host ran them.
        held = self.clock.virtual and kind in ("state", "tx")
        if kind not in ("idle", "wrote", "read", "floor") and not held:
            self.write_record("in", sid, msg)
        if not isinstance(sid, int):
            return
        if kind == "hello":
            self.recv_hello(sid, addr, msg)
        elif kind == "listen":
            self.recv_listen(sid, msg)
        elif not self.clock.virtual:
            if kind == "state":
                self.recv_state(sid, addr, msg)
            elif kind == "tx":
                self.recv_tx(sid, addr, msg)
        elif kind in ("wrote", "read"):
            self.recv_io(sid, addr, msg)
        elif kind == "floor":
            self.recv_floor(sid, addr, msg)
        # A virtual run's idle, state and tx never come here: the core takes
        # them (held for the barrier, and taken in station order there, so two
        # stations acting at one instant are ruled on the same way every time).
        # Anything else is not ours to understand.

    def send(self, sid, msg):
        """Send one message to a station, at the address it last wrote from.

        In a virtual-time run every message is an instant, which CoreEther
        sends: it carries T and the station's next sequence number, and the
        station owes an idle for that number before T can move again.
        """
        station = self.stations.get(sid)
        if station is None or self.transport is None:
            return
        if msg.get("type") != "run":
            self.write_record("out", sid, msg)
        else:
            self.runs += 1
        self.transport.sendto(json.dumps(msg).encode("utf-8"), station.addr)

    def stamp(self):
        """What heads a record line: the wall clock, or T in a virtual run."""
        if self.clock.virtual:
            return "%.6f" % (self.clock.t / 1_000_000.0)
        return wall_stamp()

    def write_record(self, direction, sid, msg):
        if self.record is None:
            return
        self.record.write("%s\t%s\t%s\t%s\n" % (
            self.stamp(), direction, "-" if sid is None else sid,
            json.dumps(msg, separators=(",", ":"), sort_keys=True)))

    # ---- station messages ----------------------------------------------

    def station_for(self, sid, addr, slots=None):
        """The station record for `sid`, created on first sight."""
        station = self.stations.get(sid)
        if station is None:
            station = Station(sid, addr, slots or [0])
            self.stations[sid] = station
            log("station %d joined from %s:%d" % (sid, addr[0], addr[1]))
        else:
            station.addr = addr
            if slots:
                station.slots = slots
        return station

    def recv_hello(self, sid, addr, msg):
        slots = msg.get("slots") or [0]
        if self.clock.virtual:
            # A station saying hello again has restarted: whatever it had
            # scheduled and said went with its old process.
            self.forget(sid)
        station = self.station_for(sid, addr, slots)
        self.expected.discard(sid)
        join = self.joins.get(sid)
        if join is not None and not join.done():
            join.set_result(None)
            if sid in self.join_waited:
                self.join_waited.discard(sid)
                self.holds += 1
                self.settle(self.release)
        station.until = None
        station.lines = bool(msg.get("lines"))
        self.send(sid, {"type": "welcome", "t": self.now(), "mode": self.mode,
                        "rate": self.rate, "epoch": self.epoch, "seed": self.seed})

    def modelled(self, sid, msg):
        """True when the message states a modulation this ether models."""
        if msg.get("mod") in MODULATIONS:
            return True
        log("station %d: %s refused: modulation %r is not one this ether models (%s)" % (
            sid, msg.get("type"), msg.get("mod"), ", ".join(MODULATIONS)))
        return False

    def recv_state(self, sid, addr, msg):
        if not self.modelled(sid, msg):
            return
        station = self.station_for(sid, addr)
        slot = msg.get("slot", 0)
        before = station.state(slot) or {}
        station.states[slot] = msg
        # Its radio is deaf until `ready_at`, after a retune whose BUSY the
        # chip model charges (`busy_us` in SIM_MESH_BOARD); with none, never.
        busy = int(msg.get("ready_at") or 0) - int(msg.get("t") or 0)
        station.ready[slot] = self.now() + busy if busy > 0 else 0
        mode = msg.get("mode")
        retuned = any(before.get(key) != msg.get(key)
                      for key in ("freq", "side") + MATCH_KEYS)
        if mode != "RX" or retuned:
            # The chip lets go of what it was demodulating on leaving RX, and
            # cannot follow it onto another channel. Its own transmission is
            # half duplex, ruled on at the frame's end as talked over; anything
            # else abandons the frame, and nothing was received that the
            # record could count.
            held = station.locks.pop(slot, None)
            if held is not None and mode != "TX":
                held.abandoned = True
        log("station %d slot %s %s freq=%s bw=%s sf=%s sync=%s" % (
            sid, slot, mode, msg.get("freq"), msg.get("bw"),
            msg.get("sf"), msg.get("sync")))
        self.raise_event(self.on_station, sid, msg)
        if (mode != before.get("mode") or retuned) and station.listening(slot):
            self.tell_late(station, slot)

    def tell_late(self, rstation, slot):
        """The frames already on the air when a slot starts listening.

        A frame reaches the slots listening when it starts. A slot that starts
        later — back from its own transmission, out of standby, out of a CAD
        into RX — is judged by the same rules at that instant: while enough of
        a frame's preamble is still to come it can lock on to it, and
        otherwise it has missed the preamble and cannot demodulate the frame,
        which is still on the air for all that: an instantaneous RSSI reads it
        and a CAD finds it. Without this, carrier sense was blind to every
        frame that began while a station was not listening, which is every
        frame that began during its own transmission.
        """
        now = self.now()
        if rstation.tx_until > now:
            return
        for frame in list(self.frames):
            if frame.sid == rstation.sid or not frame.start_us <= now < frame.end_us:
                continue
            level = self.level_of(frame, rstation.sid)
            if level is None:
                continue
            if self.pairwise:
                self.arrive_pairwise(frame, frame.radio, rstation, slot, level, now)
            else:
                self.arrive(frame, frame.radio, rstation, slot, level, now)

    def recv_tx(self, sid, addr, msg):
        if not self.modelled(sid, msg):
            return
        station = self.station_for(sid, addr)
        start = self.now()
        t0 = msg.get("t0", 0)
        if self.clock.virtual and int(t0) < start:
            # The station acted on something from outside the run after it
            # last said it was idle, at the T it had then: the frame goes on
            # the air now.
            log("frame from %d stated t0 %d, %d us before T; sent at T" % (
                sid, int(t0), start - int(t0)))
        span = max(0, min(int(msg.get("t_end", t0)) - int(t0), MAX_FRAME_US))
        pre = max(0, min(int(msg.get("t_pre", t0)) - int(t0), span))
        hdr = max(pre, min(int(msg.get("t_hdr", t0)) - int(t0), span))
        self.next_eid += 1
        frame = Frame(self.next_eid, msg.get("id"), sid, msg, start, start + span,
                      start + pre, start + hdr)

        self.prune(start)
        for other in [f for f in self.frames if f.shares_air(frame)]:
            frame.interferers.append(other)
            other.interferers.append(frame)
            log("frame %d from %d shares air with frame %d from %d (%s/%s Hz)" % (
                frame.eid, sid, other.eid, other.sid, frame.freq, other.freq))
        self.frames.append(frame)
        station.tx_until = frame.end_us
        station.power_dbm = frame.power_dbm
        station.locks.clear()           # half duplex: whatever it followed is gone
        self.raise_event(self.on_tx, sid, frame.eid, frame.freq,
                         frame.start_us, frame.end_us)

        for rsid, rstation in self.stations.items():
            if rsid == sid:
                continue
            if rstation.tx_until > start:
                continue        # half duplex: its own frame is still going out
            level = self.level_of(frame, rsid)
            if level is None:
                continue        # the table says this pair never hears
            for slot in rstation.slots:
                if not rstation.listening(slot):
                    continue
                if self.pairwise:
                    self.arrive_pairwise(frame, msg, rstation, slot, level)
                else:
                    self.arrive(frame, msg, rstation, slot, level)

        log("frame %d (station's %s) from %d: %d us on %s Hz sf%s -> %s" % (
            frame.eid, frame.fid, sid, span, frame.freq, frame.sf,
            ["%d@%.1fdBm" % (r[0], r[2]) for r in frame.receivers] or "nobody"))

    @staticmethod
    def detector(state, tx):
        """Which of a receiver's detectors can decode this transmission: 0 its
        main one, k the k-th of its `side` list, None none."""
        if not same_carrier(state.get("freq"), tx.get("freq"), state.get("bw")):
            return None
        if all(state.get(k) == tx.get(k) for k in MATCH_KEYS):
            return 0
        if state.get("mod") != tx.get("mod") or state.get("bw") != tx.get("bw"):
            return None
        for k, side in enumerate(state.get("side") or (), start=1):
            if isinstance(side, dict) and all(side.get(key) == tx.get(key)
                                              for key in SIDE_KEYS):
                return k
        return None

    # ---- arrival: the lock ----------------------------------------------

    def begin_message(self, frame, slot, level, energy=False, t0=None, hdr_ok=True, det=0):
        """An rx_begin for one slot. `t0` is the instant the slot is told,
        the frame's own start unless the slot started listening later: the
        chip measures what is left of the frame from it. `hdr_ok` false says
        the header will fail at `t_hdr`; `det` names the side detector that
        found it, left out for the main one."""
        begin = {"type": "rx_begin", "slot": slot,
                 "id": frame.eid, "t0": frame.start_us if t0 is None else t0,
                 "t_pre": frame.pre_us, "t_hdr": frame.hdr_us,
                 "t_end": frame.end_us, "level": round(level)}
        if energy:
            begin["cad"] = True
        if not hdr_ok:
            begin["hdr_ok"] = False
        if det:
            begin["det"] = det
        return begin

    def current_lock(self, rstation, slot, now):
        """The reception this slot's demodulator is following at `now`, or None."""
        held = rstation.locks.get(slot)
        if held is not None and held.frame.end_us <= now:
            rstation.locks.pop(slot, None)
            held = None
        return held

    def open_reception(self, frame, rstation, slot, level, lost=False, now=None):
        """Schedule the rx_end for one receiver of a decodable frame."""
        reception = Reception(frame, rstation.sid, slot, level, lost,
                              taken_at=frame.start_us if now is None else now)
        frame.receivers.append((rstation.sid, slot, level))
        frame.receptions[(rstation.sid, slot)] = reception
        self.call_at(frame.end_us, self.deliver_end, reception, key=rstation.sid)
        return reception

    def in_band_energy(self, rsid, state, now):
        """The summed level, in dBm, of every transmission on the air at `now`
        whose channel overlaps this receiver's."""
        total = 0.0
        for other in self.frames:
            if not other.start_us <= now < other.end_us or other.sid == rsid:
                continue
            if not in_band(other.freq, other.bw, state.get("freq"), state.get("bw")):
                continue
            level = self.level_at(other, rsid, now)
            if level is not None:
                total += dbm_to_mw(level)
        return mw_to_dbm(total)

    def arrive(self, frame, msg, rstation, slot, level, now=None):
        """A frame reaches one listening slot: the receiver-centred rule.

        Off its band, nothing. Decodable — matching its state, and its
        preamble and sync word found (`locks_on`) — a CAD slot is told it is
        there, and an RX slot locks on to it unless it is following a frame
        this one does not lead by the same-SF figure; a frame that takes the
        lock loses the earlier one. Anything else in band is energy: an
        rx_begin marked `cad`, and no end, when the summed energy there
        crosses the sense threshold.

        `now` is when a slot that started listening after the frame began is
        told of it (`tell_late`): it can lock on only while enough of the
        preamble is still to come, and is otherwise told the energy. A slot
        whose radio is not ready yet (its state's `ready_at`) is deaf: a frame
        starting before then is energy to it, and lost.
        """
        state = rstation.state(slot)
        if not in_band(frame.freq, frame.bw, state.get("freq"), state.get("bw")):
            return
        late = now is not None and now > frame.start_us
        now = frame.start_us if now is None else now
        faded = self.level_at(frame, rstation.sid, now)
        det = self.detector(state, msg)
        if det and rstation.sensing(slot):
            det = None          # a CAD is the main detector's alone
        decodable = det is not None and self.locks_on(frame, rstation.sid, slot, now, det)
        if not decodable:
            if self.in_band_energy(rstation.sid, state, now) >= \
                    sense_threshold_dbm(state.get("bw")):
                self.send(rstation.sid, self.begin_message(frame, slot, faded, energy=True, t0=now))
            return
        if (rstation.sensing(slot) or (late and not frame.preamble_left(now))
                or now < rstation.ready.get(slot, 0)):
            self.send(rstation.sid, self.begin_message(frame, slot, faded, energy=True, t0=now))
            return
        held = self.current_lock(rstation, slot, now)
        if held is not None and not self.takes(held, frame, rstation.sid, now):
            # The demodulator is busy with a frame this one does not take it
            # off: it is energy to this receiver, and lost to it.
            self.send(rstation.sid, self.begin_message(frame, slot, faded, energy=True, t0=now))
            self.open_reception(frame, rstation, slot, level, lost=True)
            return
        if held is not None:
            held.lost = True
            held.lost_at = now
            log("frame %d takes station %d off frame %d" % (
                frame.eid, rstation.sid, held.frame.eid))
        self.take_lock(frame, rstation, slot, level, faded, now, det)

    def take_lock(self, frame, rstation, slot, level, faded, now, det=0):
        """A slot locks on to a frame, by detector `det`: its header's fate is
        decided now, and a header that will fail closes the reception at
        `t_hdr`."""
        reception = self.open_reception(frame, rstation, slot, level, now=now)
        reception.det = det
        reception.hdr_ok = self.header_ok(frame, rstation.sid, slot)
        rstation.locks[slot] = reception
        if not reception.hdr_ok:
            self.call_at(frame.hdr_us, self.deliver_header_error, reception, key=rstation.sid)
        self.send(rstation.sid, self.begin_message(frame, slot, faded, t0=now,
                                                   hdr_ok=reception.hdr_ok, det=det))

    def takes(self, held, frame, rsid, now):
        """Whether a frame arriving now takes a receiver off the one it is
        following, on the two frames' faded levels there now: by leading it by
        the same-SF figure, or with bench capture as the bench saw two frames
        meet — never once the receiver is past the first one's preamble, and
        otherwise when the pair's outcome says the new one survives, the same
        outcome its verdict will read. Never without interference (`Physics`)."""
        if not self.physics.interference:
            return False
        lead = self.level_at(frame, rsid, now) - self.level_at(held.frame, rsid, now)
        if not self.bench_capture:
            return lead >= SAME_SF_REJECTION_DB
        if now >= held.frame.pre_us:
            return False
        return bench_outcome(self.seed, self.key_of(held.frame), self.key_of(frame),
                             self.node(rsid), -lead, locked=False)[1]

    def arrive_pairwise(self, frame, msg, rstation, slot, level, now=None):
        """A frame reaches one listening slot: the pairwise rule.

        Delivered where it matches and its preamble is found (`locks_on`). A
        later frame takes the receiver only when it leads, in the whole dB the
        station is shown, the one in progress by the capture margin; otherwise
        it is energy and lost to this receiver. A CAD slot is told of every
        frame it could decode, and so is a slot that started listening too
        late in the preamble, or whose radio is not ready yet (`arrive`).
        """
        state = rstation.state(slot)
        late = now is not None and now > frame.start_us
        now = frame.start_us if now is None else now
        det = self.detector(state, msg)
        if det and rstation.sensing(slot):
            det = None          # a CAD is the main detector's alone
        if det is None or not self.locks_on(frame, rstation.sid, slot, now, det):
            return
        faded = self.level_at(frame, rstation.sid, now)
        if (rstation.sensing(slot) or (late and not frame.preamble_left(now))
                or now < rstation.ready.get(slot, 0)):
            self.send(rstation.sid, self.begin_message(frame, slot, faded, energy=True, t0=now))
            return
        held = self.current_lock(rstation, slot, now)
        if held is not None and (
                not self.physics.interference
                or round(faded) < round(self.level_at(held.frame, rstation.sid, now))
                + PAIRWISE_CAPTURE_DB):
            self.send(rstation.sid, self.begin_message(frame, slot, faded, energy=True, t0=now))
            self.open_reception(frame, rstation, slot, level, lost=True)
            return
        if held is not None:
            held.lost = True
            held.lost_at = now
        self.take_lock(frame, rstation, slot, level, faded, now, det)

    # ---- delivery: the verdict ------------------------------------------

    def segments(self, frame, others, cuts=()):
        """The frame's air cut where the set of overlapping transmissions
        changes, and at any further instants in `cuts`: a list of (start,
        end, the transmissions on the air) for each piece."""
        edges = {frame.start_us, frame.end_us}
        edges.update(t for t in cuts if frame.start_us < t < frame.end_us)
        for other in others:
            edges.update(t for t in (other.start_us, other.end_us)
                         if frame.start_us < t < frame.end_us)
        edges = sorted(edges)
        pieces = []
        for lo, hi in zip(edges, edges[1:]):
            pieces.append((lo, hi, [o for o in others if o.start_us < hi and o.end_us > lo]))
        return pieces

    @staticmethod
    def talked_over(reception):
        """True when the receiver itself transmitted during the frame: half
        duplex, so the frame is lost to it whatever else was on the air."""
        return any(other.sid == reception.rsid for other in reception.frame.interferers)

    @classmethod
    def unfollowed(cls, reception):
        """Why the receiver did not follow the frame to its end, as an rx_end's
        cause, or None: it did."""
        if reception.lost:
            return "lost"
        if cls.talked_over(reception):
            return "talked_over"
        return None

    def verdict_for(self, reception):
        """How interference leaves this frame at one receiver, the
        receiver-centred rule, as (verdict, cause).

        Over every piece of the frame's air, the worst deciding: its level
        over each class of interference — the transmissions in its band at
        one spreading factor, their powers summed — at or above that class's
        rejection figure. With fading the levels are taken at each piece's
        middle, and a piece never spans a knot of it.
        """
        frame, rsid = reception.frame, reception.rsid
        cause = self.unfollowed(reception)
        if cause:
            return "crc", cause
        if not self.physics.interference:
            return "clean", None
        others = [o for o in frame.interferers if self.level_of(o, rsid) is not None]
        if not others:
            return "clean", None
        worst_lead = None
        for lo, hi, piece in self.segments(frame, others,
                                           self.knot_cuts(frame.start_us, frame.end_us)):
            if not piece:
                continue
            mid = (lo + hi) / 2.0
            level = self.level_at(frame, rsid, mid)
            classes = {}
            same_mw = 0.0
            for other in piece:
                mw = dbm_to_mw(self.level_at(other, rsid, mid))
                if self.bench_capture and same_class(frame.sf, other.sf):
                    same_mw += mw
                    continue
                classes[other.sf] = classes.get(other.sf, 0.0) + mw
            if same_mw > 0.0:
                lead = level - mw_to_dbm(same_mw)
                worst_lead = lead if worst_lead is None else min(worst_lead, lead)
            for sf, mw in classes.items():
                if level - mw_to_dbm(mw) < rejection_db(frame.sf, sf):
                    return "crc", "interference"
        if worst_lead is not None and not self.bench_survives(reception, others, worst_lead):
            return "crc", "interference"
        return "clean", None

    def bench_survives(self, reception, others, lead):
        """Bench capture's word on a frame against its own class: the
        pair's outcome (`bench_outcome`) with its strongest same-class
        interferer, at the frame's lead over the summed class in its worst
        stretch of air. With one interferer that is the bench's pair exactly;
        with more, the sum stands in for the second frame. Whether the
        receiver was locked is read off the first frame's reception: past its
        preamble, and following it, when the second started."""
        frame, rsid, slot = reception.frame, reception.rsid, reception.slot
        same = [o for o in others if same_class(frame.sf, o.sf)]
        partner = max(same, key=lambda o: (self.level_of(o, rsid), self.key_of(o)))
        first, second = sorted((frame, partner), key=lambda f: (f.start_us, self.key_of(f)))
        was = first.receptions.get((rsid, slot))
        locked = (second.start_us >= first.pre_us
                  and was is not None and was.followed_at(second.start_us))
        outcome = bench_outcome(self.seed, self.key_of(first), self.key_of(second),
                                self.node(rsid), lead if first is frame else -lead, locked)
        return outcome[0] if first is frame else outcome[1]

    def verdict_pairwise(self, reception):
        """How interference leaves this frame at one receiver, the pairwise
        rule, as (verdict, cause).

        Each same-carrier interferer this station could hear is taken on its
        own, and the frame survives only by leading every one of them by the
        capture margin, the two levels taken in the middle of their overlap.
        """
        frame, rsid = reception.frame, reception.rsid
        cause = self.unfollowed(reception)
        if cause:
            return "crc", cause
        if not self.physics.interference:
            return "clean", None
        for other in frame.interferers:
            if not same_carrier(frame.freq, other.freq, max(frame.bw or 0, other.bw or 0)):
                continue
            if self.level_of(other, rsid) is None:
                continue
            mid = (max(frame.start_us, other.start_us) + min(frame.end_us, other.end_us)) / 2.0
            against = self.level_at(other, rsid, mid)
            if not self.audible(against, other.bw, other.sf):
                continue        # this receiver never heard the other frame
            if self.level_at(frame, rsid, mid) - against < PAIRWISE_CAPTURE_DB:
                return "crc", "interference"
        return "clean", None

    def end_message(self, reception, verdict, cause, level):
        """An rx_end for one slot, at the ether's instant now."""
        frame = reception.frame
        end = {"type": "rx_end", "slot": reception.slot, "id": frame.eid,
               "t": self.now(), "verdict": verdict,
               "payload": frame.payload, "rssi": round(level),
               "snr": round(level - self.noise(frame.bw))}
        if cause:
            end["cause"] = cause
        return end

    def deliver_header_error(self, reception):
        """At `t_hdr`, a reception whose header failed: closed, and the slot
        free to lock on to another frame. Nothing when the receiver stopped
        following the frame before then — taken off it, gone out of RX, or
        transmitting — and the frame's end rules on it as it would any other."""
        frame, rsid, slot = reception.frame, reception.rsid, reception.slot
        station = self.stations.get(rsid)
        if station is None or station.locks.get(slot) is not reception:
            return
        station.locks.pop(slot, None)
        reception.ended = True
        level = self.level_at(frame, rsid, frame.hdr_us)
        self.send(rsid, self.end_message(reception, "hdr", "noise", level))
        log("frame %d from %d -> station %d slot %s: hdr at %.1f dBm" % (
            frame.eid, frame.sid, rsid, slot, level))
        self.raise_event(self.on_rx, rsid, frame.sid, frame.eid, "hdr", level, "noise")

    def deliver_end(self, reception):
        """Close out one receiver's reception of a frame, at its stated end:
        interference first, by the rule in force, then noise on the payload."""
        frame, rsid, slot = reception.frame, reception.rsid, reception.slot
        if reception.ended:
            return
        station = self.stations.get(rsid)
        if station is not None and station.locks.get(slot) is reception:
            station.locks.pop(slot, None)
        if reception.abandoned:
            log("frame %d from %d -> station %d slot %s: abandoned, it left RX" % (
                frame.eid, frame.sid, rsid, slot))
            return
        verdict, cause = (self.verdict_pairwise(reception) if self.pairwise
                          else self.verdict_for(reception))
        blocks = self.payload_blocks(frame, rsid)
        if verdict == "clean" and (not reception.hdr_ok
                                   or not self.payload_survives(reception, blocks)):
            verdict, cause = "crc", "noise"
        if blocks:
            level = mw_to_dbm(sum(dbm_to_mw(b[0]) for b in blocks) / len(blocks))
        else:
            level = self.level_at(frame, rsid, frame.hdr_us)
        self.send(rsid, self.end_message(reception, verdict, cause, level))
        log("frame %d from %d -> station %d slot %s: %s%s at %.1f dBm" % (
            frame.eid, frame.sid, rsid, slot, verdict,
            " (%s)" % cause if cause else "", level))
        self.raise_event(self.on_rx, rsid, frame.sid, frame.eid, verdict, level, cause)

    def prune(self, now):
        """Drop frames whose air is long gone; collisions can no longer touch
        them. And the fading's knots before any frame still on the air: they
        are the same whenever drawn again."""
        self.frames = [f for f in self.frames if f.end_us > now]
        if self.knots:
            earliest = min([f.start_us for f in self.frames] + [now])
            k = math.floor(earliest / (self.physics.coherence_s * 1e6)) - 1
            self.knots = {key: z for key, z in self.knots.items() if key[2] >= k}

    def close(self):
        if self.pace_timer is not None:
            self.pace_timer.cancel()
        if self.transport is not None:
            self.transport.close()
        if self.record is not None:
            self.record.close()


# ---- the conductor in Rust ----------------------------------------------------
#
# In a virtual-time run most of the ether's work is the barrier: an idle in, T
# moved, the stations due sent a `run`. ether_core, built from ether/core
# (built by `sim` when it starts), does that in Rust on the ether's socket, in the
# event loop's thread; CoreEther is Ether with that conductor and everything
# else as Ether has it. It is the only conductor: the one Ether had in Python,
# which the core was proved against record for record, is gone, and a
# virtual-time run without the core built is refused saying so.

CORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "build", "ether_core.abi3.so")
_core_module = None


def core_module():
    """ether_core, which conducts every virtual-time run."""
    global _core_module
    if _core_module is None:
        if not os.path.exists(CORE_PATH):
            raise RuntimeError("a virtual-time run is conducted by ether_core, and there is "
                               "no %s (sim builds it when it starts)" % CORE_PATH)
        import importlib.util
        spec = importlib.util.spec_from_file_location("ether_core", CORE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _core_module = module
    return _core_module


class CoreClock(VirtualClock):
    """Conductor time as the core holds it. The events stay on this heap, in
    Python; the core is told whenever the earliest of them changes."""

    def __init__(self, core):
        self.core = core
        self.heap = []
        self.count = 0

    @property
    def t(self):
        return self.core.t

    def call_at(self, t_us, callback, *args, key=0):
        VirtualClock.call_at(self, t_us, callback, *args, key=key)
        self.core.due = self.heap[0][0]

    def pop_due(self):
        due = VirtualClock.pop_due(self)
        if due is not None:
            self.core.due = self.heap[0][0] if self.heap else None
        return due


class CoreStation(Station):
    """A station whose conductor half the core keeps: what Python still asks
    of it is read from there, and the rest is not here to be asked."""

    def __init__(self, sid, addr, slots, core):
        self.core = core
        Station.__init__(self, sid, addr, slots)

    def init_conductor(self, addr):
        pass                    # the core's, from when it was first heard from

    addr = property(lambda self: self.core.addr(self.sid),
                    lambda self, addr: self.core.set_addr(self.sid, addr))
    seq = property(lambda self: self.core.seq(self.sid))
    idle = property(lambda self: self.core.idle(self.sid))
    until = property(lambda self: self.core.until(self.sid),
                     lambda self, until: self.core.set_until(self.sid, until))
    told = property(lambda self: self.core.told(self.sid))
    asking = property(lambda self: self.core.asking(self.sid),
                      lambda self, n: self.core.set_asking(self.sid, n))
    asked_at = property(lambda self: self.core.asked_at(self.sid),
                        lambda self, seq: self.core.set_asked_at(self.sid, seq))
    slow_idles = property(lambda self: self.core.slow_idles(self.sid))
    lines = property(lambda self: self.core.lines(self.sid),
                     lambda self, on: self.core.set_lines(self.sid, on))


class _Expected:
    """The stations started and not yet heard from, which the core keeps, as
    Ether uses its set of them."""

    def __init__(self, core):
        self.core = core

    def add(self, sid):
        self.core.expect(sid)

    def discard(self, sid):
        self.core.unexpect(sid)

    def __iter__(self):
        return iter(self.core.expected())

    def __len__(self):
        return len(self.core.expected())


class _Asks(list):
    """The writes waiting for a go-ahead, which the core counts."""

    def __init__(self, core, asks=()):
        list.__init__(self, asks)
        self.core = core
        core.asks = len(self)

    def append(self, ask):
        list.append(self, ask)
        self.core.asks = len(self)

    def remove(self, ask):
        list.remove(self, ask)
        self.core.asks = len(self)


class _CoreTransport:
    """What Ether asks of its transport, on the core's socket."""

    def __init__(self, ether):
        self.ether = ether

    def sendto(self, data, addr):
        self.ether.core.sendto(data, addr)

    def get_extra_info(self, name, default=None):
        if name == "socket":
            return self.ether.sock
        if name == "sockname":
            return self.ether.sock.getsockname()
        return default

    def close(self):
        self.ether.sock.close()


def _core_count(name):
    """One of the conductor's counts, which the core keeps; Ether's start
    sets it to the zero it already is."""
    def get(self):
        return getattr(self.core, name)

    def set_(self, n):
        if n != getattr(self.core, name):
            raise AttributeError("%s is counted by the core" % name)
    return property(get, set_)


class CoreEther(Ether):
    """Ether with its conductor in ether_core. `sock` is the ether's bound
    UDP socket, which the core reads and writes in the loop's thread; the
    core calls back into the methods named `_core_*` below at the points of
    the barrier where the ether does its part."""

    def __init__(self, sock, record_path, physics=None, seed=None, time_mode="max",
                 pairwise=False, epoch=None, bench_capture=False, module=None):
        mode, rate = parse_time_mode(time_mode)
        if mode != "virtual":
            raise ValueError("the core conducts virtual-time runs; this one is %s" % mode)
        module = module or core_module()
        sock.setblocking(False)
        self.sock = sock
        self.core_module = module
        self.core = module.Core(sock.fileno(), self, rate is not None, STANDING_LIMIT,
                                STANDING_QUANTUM_US, SLOW_IDLE_S, RESEND_GAP_S)
        self._on_drain = None
        Ether.__init__(self, record_path, physics, seed=seed, time_mode=time_mode,
                       pairwise=pairwise, epoch=epoch, bench_capture=bench_capture)
        self.clock = CoreClock(self.core)
        self.expected = _Expected(self.core)
        self.transport = _CoreTransport(self)
        self.loop.add_reader(self.core.wake_fd, self.core.pump)

    holds = property(lambda self: self.core.holds,
                     lambda self, n: setattr(self.core, "holds", n))
    unread = property(lambda self: self.core.unread,
                      lambda self, n: setattr(self.core, "unread", n))
    barriers = _core_count("barriers")
    runs = _core_count("runs")
    resends = _core_count("resends")
    standing = _core_count("standing")

    @property
    def on_drain(self):
        return self._on_drain

    @on_drain.setter
    def on_drain(self, handler):
        self._on_drain = handler
        self.core.drain = handler is not None

    @property
    def drains_watched(self):
        return self._drains_watched

    @drains_watched.setter
    def drains_watched(self, on):
        """on_drain answers exactly stations.printed over the consoles watched
        here, so the core looks at them itself and asks it only when one
        shows something printed."""
        self._drains_watched = bool(on)
        self.core.watching = bool(on)

    @property
    def asks(self):
        return self._asks

    @asks.setter
    def asks(self, asks):
        self._asks = _Asks(self.core, asks)

    # ---- the conductor, which the core does -------------------------------

    def kick(self):
        """Move T as far as the barrier lets it, now."""
        self.core.kick()

    def busy(self):
        """True while T must wait: a station started and not yet idle, input
        a reader has not taken, or the testbed's own work at this instant."""
        return self.core.busy()

    def mark(self, station, idle):
        """Whether a station has said it is idle since it was last told anything."""
        self.core.mark(station.sid, idle)

    def send(self, sid, msg):
        granted = self.core.grant(sid, msg.get("t"))
        if granted is None:
            return
        seq, t = granted
        msg = dict(msg, seq=seq)
        msg.setdefault("t", t)
        run = msg.get("type") == "run"
        # One encoding for the record and the wire: a station reads the
        # members of a message by name, in whatever order and spacing.
        text = json.dumps(msg, separators=(",", ":"), sort_keys=True)
        if not run and self.record is not None:
            self.record.write("%s\t%s\t%s\t%s\n" % (self.stamp(), "out", sid, text))
        self.core.post(sid, seq, text.encode("utf-8"), run)

    def station_for(self, sid, addr, slots=None):
        station = self.stations.get(sid)
        if station is None:
            self.core.add(sid, addr)            # not idle until it says so
            station = CoreStation(sid, addr, slots or [0], self.core)
            self.stations[sid] = station
            log("station %d joined from %s:%d" % (sid, addr[0], addr[1]))
        else:
            station.addr = addr
            if slots:
                station.slots = slots
        return station

    def forget(self, sid):
        station = self.stations.pop(sid, None)
        if station is None:
            return False
        self.core.forget(sid)
        self.asks = [a for a in self.asks if a[0] != sid]
        for key, channel in list(self.channels.items()):
            if sid in (channel.writer, channel.reader):
                self.drop_channel(channel)
        self.endpoints = {e: s for e, s in self.endpoints.items() if s != sid}
        return True

    def sync(self, sid, done):
        station = self.stations.get(sid)
        if station is None or (station.idle and station.told >= self.clock.t):
            done()
            return
        station.on_idle.append(done)
        self.core.notify(sid)
        if station.told < self.clock.t:
            self.send(sid, {"type": "run"})

    def console_marks(self, fd):
        return self.core_module.Marks(fd)

    def watch(self, sid, marks):
        if isinstance(marks, self.core_module.Marks):
            self.core.watch(sid, marks)

    def unwatch(self, sid, marks):
        if isinstance(marks, self.core_module.Marks):
            self.core.unwatch(sid, marks)

    def close(self):
        self.loop.remove_reader(self.core.wake_fd)
        self.core.close()
        Ether.close(self)

    # ---- what the core calls out to ---------------------------------------

    def _core_datagram(self, data, addr):
        """A datagram that is not the conductor's: Ether takes it."""
        Ether.datagram_received(self, data, addr)

    def _core_flush(self, batch):
        """What stations said while T stood still, (sid, addr, datagram) in
        station order, as the core held them for the barrier."""
        self.take_held((sid, addr, json.loads(data.decode("utf-8")))
                       for sid, addr, data in batch)

    def _core_due(self):
        self.run_due()

    def _core_drain(self, sids):
        """True when none of `sids` printed anything, which on_drain says
        with False: nothing to read, nothing set going."""
        return self.on_drain(sids, self.drained) is False

    def _core_ask(self):
        """True when the run was quiet enough to let the first write asked
        for go ahead (kick)."""
        if self.asks and self.quiet():
            self.answer_ask()
            return True
        return False

    def _core_paced(self, t):
        return self.paced(t)

    def _core_idle(self, sid):
        """A station that `sync` waits on said its idle: what waited runs."""
        station = self.stations.get(sid)
        if station is not None and station.on_idle:
            waiting, station.on_idle = station.on_idle, []
            for callback in waiting:
                callback()

    def _core_log(self, msg):
        log(msg)


async def open_ether(bind, record_path, physics=None, time_mode="real", **kw):
    """The ether on `bind`, as (transport, ether): with its conductor in the
    core when the run is in virtual time."""
    loop = asyncio.get_running_loop()
    mode, _ = parse_time_mode(time_mode)
    if mode != "virtual":
        return await loop.create_datagram_endpoint(
            lambda: Ether(record_path, physics, time_mode=time_mode, **kw), local_addr=bind)
    module = core_module()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        sock.bind(bind)
        ether = CoreEther(sock, record_path, physics, time_mode=time_mode, module=module, **kw)
    except BaseException:
        sock.close()
        raise
    log("the conductor is ether_core's")
    return ether.transport, ether


def rule_name(pairwise, bench_capture=False):
    """The collision rule, as the logs name it."""
    if pairwise:
        return "pairwise"
    return "receiver-centred, bench capture" if bench_capture else "receiver-centred"


def parse_bind(text):
    """`host:port` into a tuple, with a bare port allowed."""
    if ":" in text:
        host, _, port = text.rpartition(":")
        return (host or "127.0.0.1", int(port))
    return ("127.0.0.1", int(text))


def read_nodeset(path):
    """A nodeset file's node names, station ids and antenna gains, for a run alone.

    Only what the medium needs: `nodes: {name: {id, antenna: {gain_dbi}}}`.
    Returns (sid_by_name, gains_db_by_sid).
    """
    import yaml                 # only a standalone run reads a file

    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    sids, gains = {}, {}
    for name, node in (data.get("nodes") or {}).items():
        sid = int(node["id"])
        sids[str(name)] = sid
        gains[sid] = float((node.get("antenna") or {}).get("gain_dbi", 0.0))
    return sids, gains


def read_losses(directory):
    """Every band's table found in `directory`, as `<band>.bin`."""
    tables = {}
    for band in slt.BANDS:
        path = os.path.join(directory, "%s.bin" % band)
        if os.path.exists(path):
            tables[band] = slt.Table.read(path)
    return tables


async def serve(bind, record_path, physics, losses, time_mode="real", pairwise=False,
                seed=None, bench_capture=False):
    loop = asyncio.get_running_loop()
    transport, ether = await open_ether(bind, record_path, physics, time_mode=time_mode,
                                        pairwise=pairwise, seed=seed,
                                        bench_capture=bench_capture)
    tables, sids, gains = losses
    ether.set_losses(tables, sids, gains)
    host, port = transport.get_extra_info("sockname")[:2]
    log("ether listening on %s:%d" % (host, port))
    log("recording to %s" % record_path)
    log("time: %s" % describe_time(ether.mode, ether.rate))
    log("physics: %s; %s rule" % (physics.describe(), rule_name(pairwise, bench_capture)))
    log("loss tables: %s; nodes: %s" % (
        ", ".join("%s MHz (%d)" % (b, t.n) for b, t in sorted(tables.items())) or "none",
        ", ".join("%s=%d" % (n, s) for n, s in sorted(sids.items(), key=lambda i: i[1]))
        or "none — nothing can hear anything"))
    stop = loop.create_future()
    try:
        await stop
    finally:
        ether.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="the virtual ether")
    ap.add_argument("--bind", default="127.0.0.1:7000",
                    help="host:port to listen on (default 127.0.0.1:7000)")
    ap.add_argument("--record", default="record.tsv",
                    help="record file (default record.tsv in the current directory)")
    ap.add_argument("--geodata", metavar="PATH",
                    help="the geodata file the nodeset and tables belong to (named in the log)")
    ap.add_argument("--nodeset", metavar="PATH",
                    help="a nodeset file: node names, station ids and antenna gains")
    ap.add_argument("--losses", metavar="DIR",
                    help="the directory holding the nodeset's <band>.bin loss tables "
                         "(default: none, so nothing is heard)")
    ap.add_argument("--noise-figure", type=float, default=DEFAULT_NOISE_FIGURE_DB,
                    help="receiver noise figure in dB (default %g)" % DEFAULT_NOISE_FIGURE_DB)
    ap.add_argument("--pairwise", action="store_true",
                    help="rule on collisions pairwise, per interferer by the capture "
                         "margin, instead of on the summed interference")
    ap.add_argument("--bench-capture", action="store_true",
                    help="rule on two frames of one spreading factor as a bench saw them "
                         "meet, instead of by the same-SF figure")
    ap.add_argument("--fading-db", "--slow-fading-db", dest="fading_db",
                    type=float, default=DEFAULT_FADING_DB,
                    help="fading: the spread in dB of every link's level over time "
                         "(default %g: none; --slow-fading-db is Sergey's fork's name "
                         "for this same setting)" % DEFAULT_FADING_DB)
    ap.add_argument("--coherence-s", "--slow-fading-s", dest="coherence_s",
                    type=float, default=DEFAULT_COHERENCE_S,
                    help="fading: how long, in seconds, a link's level stays alike "
                         "(default %g; --slow-fading-s aliases this)" % DEFAULT_COHERENCE_S)
    ap.add_argument("--rician-k", "--fast-fading-k", dest="rician_k",
                    type=float, default=DEFAULT_RICIAN_K, metavar="K",
                    help="fast fading: one Rician draw per frame at each receiver, of "
                         "this K factor, 0 for Rayleigh (default: none; --fast-fading-k "
                         "aliases this)")
    ap.add_argument("--no-interference", action="store_true",
                    help="an oracle: judge every frame against noise alone and never take "
                         "a receiver off the frame it follows; what overlapping frames "
                         "cost a run is its delivery with this less without")
    ap.add_argument("--seed", type=int,
                    help="the seed of the medium's draws, handed every station in its "
                         "welcome (default: drawn at random)")
    ap.add_argument("--time", default="real",
                    help="real (default), max, or <k>x: virtual time as fast as the "
                         "stations allow, or paced at k times the wall clock")
    args = ap.parse_args(argv)
    try:
        parse_time_mode(args.time)
    except ValueError as err:
        ap.error(str(err))
    if bool(args.nodeset) != bool(args.losses):
        ap.error("--nodeset and --losses go together")
    if args.bench_capture and args.pairwise:
        ap.error("--bench-capture is a variant of the receiver-centred rule, not --pairwise's")
    try:
        physics = Physics(args.noise_figure, not args.no_interference,
                          args.fading_db, args.coherence_s, args.rician_k)
    except ValueError as err:
        ap.error(str(err))
    sids, gains, tables = {}, {}, {}
    if args.nodeset:
        sids, gains = read_nodeset(args.nodeset)
        tables = read_losses(args.losses)
        if args.geodata:
            log("geodata: %s" % args.geodata)
    try:
        asyncio.run(serve(parse_bind(args.bind), args.record, physics,
                          (tables, sids, gains),
                          args.time, args.pairwise, args.seed, args.bench_capture))
    except KeyboardInterrupt:
        log("ether stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
