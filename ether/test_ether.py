"""Fake stations on real UDP sockets, against the ether as a child process,
and the reception model's arithmetic against an ether held in-process.

Stations are linked rather than placed: every test states the path loss of
each pair it needs, in dB, into a loss table written to the test's directory
(slt.py), and the levels asserted below are what those losses give at 14 dBm.
A pair a test does not link is never heard, which is how a hidden terminal is
made here.
"""

import asyncio
import base64
import json
import math
import os
import queue
import re
import select
import socket
import statistics
import subprocess
import sys
import threading
import time
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ETHER = os.path.join(HERE, "ether.py")
sys.path.insert(0, HERE)

import ether as ether_module     # noqa: E402 - the path is set just above
import slt                       # noqa: E402

FREQ = 868_100_000
BW = 125_000
SF = 9
SYNC = 0x34
POWER_DBM = 14

# A frame long enough that begin and end are plainly separate events, short
# enough that the tests stay quick.
FRAME_US = 300_000

# Losses that keep the arithmetic legible: at 14 dBm a 100 dB pair arrives at
# −86 dBm and a 127 dB one at −113.
NEAR_DB = 100.0
FAR_DB = 127.0

# The receiver's floor at 125 kHz and the default 6 dB noise figure.
NOISE_DBM = -174.0 + 10.0 * math.log10(BW) + 6.0       # −117.03


def level_for(loss_db, gain_db=0.0, power_dbm=POWER_DBM, freq=FREQ, f0=868_000_000):
    """The level the ether will compute for this loss, in dBm."""
    return power_dbm + gain_db - loss_db - 20.0 * math.log10(freq / f0)


def expected_snr(level):
    """What the ether reports as SNR: the level above the receiver's noise."""
    return round(level - NOISE_DBM)


def radio(mode="RX", **over):
    """The radio fields shared by a state and a transmission."""
    fields = {"mode": mode, "ready_at": 0, "mod": "lora", "freq": FREQ, "bw": BW, "sf": SF,
              "cr": 5, "sync": SYNC, "hdr": "explicit", "crc": True, "pre": 8,
              "iq": "normal"}
    fields.update(over)
    return fields


def name(sid):
    return "n%d" % sid


def make_table(band, sids, losses):
    """A loss table for one band over these stations; `losses` maps an ordered
    pair of station ids to dB, and every other pair is never heard."""
    header = {"geodata": "test", "band": band, "f0_hz": slt.f0_of(band),
              "model": "log-distance",
              "nodes": [{"name": name(s), "id": s} for s in sorted(sids)]}
    table = slt.Table(header)
    for (a, b), db in losses.items():
        table.put(name(a), name(b), db)
    return table


class FakeStation:
    """One station's UDP socket, its clock and its inbox.

    The ether's address is resolved on the first datagram rather than at
    construction, because the ether does not exist until every link in the
    test has been stated — see `Bench`.
    """

    def __init__(self, sid, bench):
        self.sid = sid
        self.bench = bench
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.t = sid * 1_000_000      # each station's own clock, its own origin

    @property
    def ether(self):
        return ("127.0.0.1", self.bench.ensure_started())

    def close(self):
        self.sock.close()

    def send(self, msg):
        msg = dict(msg, sid=self.sid, t=self.t)
        self.sock.sendto(json.dumps(msg).encode(), self.ether)

    def hello(self):
        self.send({"type": "hello", "slots": [0]})
        return self.expect("welcome")

    def state(self, mode="RX", **over):
        self.send(dict({"type": "state", "slot": 0}, **radio(mode, **over)))

    def tx(self, fid, payload=b"hello", span_us=FRAME_US, pre_us=None, hdr_us=None, **over):
        t0 = self.t
        self.send(dict({"type": "tx", "slot": 0, "id": fid, "t0": t0,
                        "t_pre": t0 + (span_us // 10 if pre_us is None else pre_us),
                        "t_hdr": t0 + (span_us // 5 if hdr_us is None else hdr_us),
                        "t_end": t0 + span_us, "power_dbm": POWER_DBM,
                        "payload": base64.b64encode(payload).decode()},
                       **radio("TX", **over)))

    def recv(self, timeout=2.0):
        self.sock.settimeout(timeout)
        try:
            data, _ = self.sock.recvfrom(65535)
        except socket.timeout:
            return None
        return json.loads(data.decode())

    def expect(self, kind, timeout=2.0):
        """The next message of this type, ignoring anything else."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.recv(max(0.05, deadline - time.monotonic()))
            if msg is None:
                break
            if msg.get("type") == kind:
                return msg
        raise AssertionError("station %d never saw %s" % (self.sid, kind))

    def expect_nothing(self, timeout=0.5):
        assert self.recv(timeout) is None

    def ends(self, count, timeout=2.0):
        """The next `count` receptions to close, keyed by the bytes they carry.

        A frame's id belongs to the ether, not to the station that sent it, so
        a test that has two frames in the air tells them apart by what is in
        them.
        """
        closed = {}
        for _ in range(count):
            end = self.expect("rx_end", timeout=timeout)
            closed[base64.b64decode(end["payload"])] = end
        return closed


class Bench:
    """The ether as a child process, its nodeset and loss table, and the
    stations that talk to it.

    Calling it makes a station: `ether(1)` is station 1, node `n1`, in the
    nodeset. `link(a, b, db)` states a pair's loss, both ways. The ether starts
    on the first datagram any station sends, so every link a test wants is
    stated before then — the table is written once, as a run's is.
    """

    def __init__(self, tmp_path, time_mode="real", pairwise=False, *flags):
        self.tmp_path = tmp_path
        self.time_mode = time_mode
        self.pairwise = pairwise
        self.flags = list(flags)    # more of the ether's own, as its command line takes them
        self.record = tmp_path / "record.tsv"
        self.proc = None
        self.port = None
        self.stations = []
        self.gains = {}             # sid -> dBi, for every node in the nodeset
        self.losses = {}            # (sid, sid) -> dB at the 868 band's centre

    def link(self, a, b, db, back=None):
        assert self.proc is None, "the ether is already running"
        self.gains.setdefault(a, 0.0)
        self.gains.setdefault(b, 0.0)
        self.losses[(a, b)] = float(db)
        self.losses[(b, a)] = float(db if back is None else back)

    def write_files(self):
        """The nodeset file and the 868 MHz table, which is how the ether reads them."""
        nodes = ["nodes:"]
        for sid, gain in sorted(self.gains.items()):
            nodes.append("  %s: { id: %d, antenna: { gain_dbi: %g } }" % (name(sid), sid, gain))
        nodeset_path = self.tmp_path / "nodeset.yaml"
        nodeset_path.write_text("\n".join(nodes) + "\n")
        losses = self.tmp_path / "losses"
        losses.mkdir(exist_ok=True)
        make_table("868", self.gains, self.losses).write(str(losses / "868.bin"))
        return nodeset_path, losses

    def ensure_started(self):
        """The ether's port, starting it on the first station to speak."""
        if self.proc is None:
            self.start()
        return self.port

    def start(self):
        argv = [sys.executable, ETHER, "--bind", "127.0.0.1:0",
                "--record", str(self.record), "--time", self.time_mode]
        if self.gains:
            nodes, losses = self.write_files()
            argv += ["--nodeset", str(nodes), "--losses", str(losses)]
        if self.pairwise:
            argv.append("--pairwise")
        argv += self.flags
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()

        def drain():
            for line in self.proc.stderr:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=drain, daemon=True).start()

        deadline = time.monotonic() + 10
        while self.port is None and time.monotonic() < deadline:
            line = lines.get(timeout=10)
            if line is None:
                break
            found = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
            if found:
                self.port = int(found.group(1))
        assert self.port, "the ether never reported a listening port"

    def __call__(self, sid, gain_db=0.0):
        assert self.proc is None or sid in self.gains
        self.gains[sid] = float(gain_db)
        station = FakeStation(sid, self)
        self.stations.append(station)
        return station

    def close(self):
        for station in self.stations:
            station.close()
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=5)


@pytest.fixture
def ether(tmp_path):
    """The ether on an ephemeral port, its record in the test's directory."""
    bed = Bench(tmp_path)
    try:
        yield bed
    finally:
        bed.close()


@pytest.fixture
def pairwise(tmp_path):
    """The ether under the pairwise rule."""
    bed = Bench(tmp_path, pairwise=True)
    try:
        yield bed
    finally:
        bed.close()


# A virtual-time run's conductor is ether_core (`sim-mesh build ether`), and
# every virtual-time test needs it: without it they fail saying so, rather than
# skip and look like a suite that passed.
CORE_BUILT = os.path.exists(ether_module.CORE_PATH)


def need_core():
    if not CORE_BUILT:
        pytest.fail("no ether core built (sim-mesh build ether)")


@pytest.fixture
def conductor(tmp_path):
    """The ether in virtual time, as fast as its stations let it go."""
    need_core()
    bed = Bench(tmp_path, "max")
    try:
        yield bed
    finally:
        bed.close()


def listen(*stations, **over):
    for station in stations:
        station.hello()
    for station in stations:
        station.state("RX", **over)
    time.sleep(0.1)


def test_hello_gets_a_welcome(ether):
    welcome = ether(1).hello()
    assert welcome["mode"] == "real"
    assert welcome["rate"] == 1
    assert isinstance(welcome["t"], int)
    assert isinstance(welcome["epoch"], int)
    assert isinstance(welcome["seed"], int)
    assert "seq" not in welcome


def test_frame_reaches_a_listening_station(ether):
    ether.link(1, 2, NEAR_DB)
    sender, receiver = ether(1), ether(2)
    sender.hello()
    listen(receiver)

    level = level_for(NEAR_DB)
    sent_at = time.monotonic()
    sender.tx(7, payload=b"over the air")

    begin = receiver.expect("rx_begin")
    assert time.monotonic() - sent_at < 0.2, "rx_begin must arrive at once"
    assert begin["slot"] == 0
    assert "cad" not in begin
    assert begin["level"] == round(level)
    assert begin["t_end"] - begin["t0"] == FRAME_US
    assert begin["t_pre"] - begin["t0"] == FRAME_US // 10
    assert begin["t_hdr"] - begin["t0"] == FRAME_US // 5

    end = receiver.expect("rx_end")
    elapsed = time.monotonic() - sent_at
    assert FRAME_US / 1e6 - 0.05 < elapsed < FRAME_US / 1e6 + 0.3, elapsed
    assert end["id"] == begin["id"], "one reception, one id from beginning to end"
    assert end["verdict"] == "clean"
    assert base64.b64decode(end["payload"]) == b"over the air"
    assert end["rssi"] == round(level)
    assert end["snr"] == expected_snr(level)

    sender.expect_nothing()      # a transmitter never hears itself


def test_the_level_is_the_tables_loss(ether):
    ether.link(1, 2, NEAR_DB)
    ether.link(1, 3, FAR_DB)
    sender, near, far = ether(1), ether(2), ether(3)
    sender.hello()
    listen(near, far)

    sender.tx(1)
    assert near.expect("rx_begin")["level"] == round(level_for(NEAR_DB))
    assert far.expect("rx_begin")["level"] == round(level_for(FAR_DB))


def test_a_table_is_read_per_direction(ether):
    """A measured table need not be symmetric, and each frame reads its own
    direction."""
    ether.link(1, 2, NEAR_DB, back=NEAR_DB + 10)
    a, b = ether(1), ether(2)
    listen(a, b)
    a.tx(1)
    assert b.expect("rx_begin")["level"] == round(level_for(NEAR_DB))
    b.expect("rx_end")
    b.tx(2)
    assert a.expect("rx_begin")["level"] == round(level_for(NEAR_DB + 10))


def test_antenna_gain_lifts_both_ends(ether):
    """Gain is the receiver's as much as the transmitter's: the link has both."""
    ether.link(1, 2, FAR_DB)
    ether.link(1, 3, FAR_DB)
    sender, plain, tall = ether(1, gain_db=2), ether(2), ether(3, gain_db=9)
    sender.hello()
    listen(plain, tall)

    sender.tx(1)
    assert plain.expect("rx_begin")["level"] == round(level_for(FAR_DB, gain_db=2))
    assert tall.expect("rx_begin")["level"] == round(level_for(FAR_DB, gain_db=11))


def test_a_pair_the_table_does_not_have_is_never_heard(ether):
    ether.link(1, 2, NEAR_DB)
    ether.link(1, 3, math.inf)
    sender, heard, never, absent = ether(1), ether(2), ether(3), ether(4)
    sender.hello()
    listen(heard, never, absent)

    sender.tx(1)
    heard.expect("rx_begin")
    never.expect_nothing()
    absent.expect_nothing()


def test_a_frame_well_under_its_spreading_factors_threshold_is_not_delivered(ether):
    """At SF9 the threshold is −12.5 dB over the −117.03 dBm floor: −129.53
    dBm, a loss of 143.53 dB at 14 dBm. 3 dB inside it (140.5) a short frame
    is certain to arrive; 10 dB outside it (153.5) its preamble is found
    about once in 120 000 frames."""
    ether.link(1, 2, 140.5)
    ether.link(1, 3, 153.5)
    sender, inside, outside = ether(1), ether(2), ether(3)
    sender.hello()
    listen(inside, outside)

    sender.tx(1)
    assert inside.expect("rx_end")["verdict"] == "clean"
    outside.expect_nothing()


def test_a_station_the_nodeset_does_not_name_hears_nothing(ether):
    ether.link(1, 2, NEAR_DB)
    sender = ether(1)
    ether(2)
    stray = FakeStation(9, ether)        # an id no nodeset names
    ether.stations.append(stray)
    sender.hello()
    listen(stray)

    sender.tx(121)
    stray.expect_nothing()


def test_the_sender_is_not_a_receiver_and_others_must_match(ether):
    for sid in (2, 3, 4):
        ether.link(1, sid, NEAR_DB)
    sender, same, other_freq, sleeping = ether(1), ether(2), ether(3), ether(4)
    for station in (sender, same, other_freq, sleeping):
        station.hello()
    same.state("RX")
    other_freq.state("RX", freq=869_500_000)
    sleeping.state("SLEEP")
    time.sleep(0.1)

    sender.tx(11, payload=b"x")
    assert base64.b64decode(same.expect("rx_end")["payload"]) == b"x"
    other_freq.expect_nothing()
    sleeping.expect_nothing()


def test_wrong_sync_word_is_not_decoded(ether):
    """A weak frame at another sync word is neither decoded nor loud enough
    to be energy."""
    ether.link(1, 2, FAR_DB)
    sender, receiver = ether(1), ether(2)
    sender.hello()
    listen(receiver, sync=0x12)

    sender.tx(12)
    receiver.expect_nothing()


def test_inverted_iq_is_energy_to_a_receiver_set_for_normal(ether):
    """The chirps run the other way: no preamble found, only the energy of a
    frame loud enough to cross the sense threshold."""
    ether.link(1, 2, 90.0)
    ether.link(1, 3, FAR_DB)
    sender, near, far = ether(1), ether(2), ether(3)
    sender.hello()
    listen(near, far)

    sender.tx(13, iq="inverted")
    assert near.expect("rx_begin")["cad"] is True
    near.expect_nothing(timeout=FRAME_US / 1e6 + 0.2)
    far.expect_nothing()


def test_a_modulation_the_ether_does_not_model_is_refused(ether):
    """A frame or a state saying a modulation other than LoRa reaches nobody."""
    ether.link(1, 2, NEAR_DB)
    sender, receiver = ether(1), ether(2)
    sender.hello()
    listen(receiver)

    sender.tx(13, mod="flrc")
    receiver.expect_nothing()
    sender.tx(14)
    assert receiver.expect("rx_begin")


def test_a_carrier_a_few_register_steps_off_is_the_same_carrier(ether):
    """Two drivers round one frequency to registers tens of hertz apart; a
    receiver decodes within a quarter of its bandwidth and not beyond."""
    ether.link(1, 2, FAR_DB)
    ether.link(1, 3, FAR_DB)
    sender, near_miss, other = ether(1), ether(2), ether(3)
    sender.hello()
    near_miss.hello()
    other.hello()
    near_miss.state("RX", freq=FREQ + 36)
    other.state("RX", freq=FREQ + BW // 4 + 1000)
    time.sleep(0.1)

    sender.tx(13)
    near_miss.expect("rx_begin")
    assert near_miss.expect("rx_end")["verdict"] == "clean"
    other.expect_nothing()


def test_overlapping_frames_of_equal_level_both_end_as_crc(ether):
    ether.link(1, 3, FAR_DB - 10)
    ether.link(2, 3, FAR_DB - 10)
    first, second, receiver = ether(1), ether(2), ether(3)
    first.hello()
    second.hello()
    listen(receiver)

    first.tx(21, payload=b"first")
    assert "cad" not in receiver.expect("rx_begin")
    time.sleep(FRAME_US / 2e6)          # squarely inside the first frame
    second.tx(22, payload=b"second")
    assert receiver.expect("rx_begin")["cad"] is True, "the receiver is busy"

    ends = receiver.ends(2)
    assert set(ends) == {b"first", b"second"}
    assert ends[b"first"]["verdict"] == "crc"
    assert ends[b"second"]["verdict"] == "crc"


def test_frames_that_do_not_overlap_stay_clean(ether):
    ether.link(1, 2, NEAR_DB)
    sender, receiver = ether(1), ether(2)
    sender.hello()
    listen(receiver)

    sender.tx(31, span_us=100_000)
    receiver.expect("rx_begin")
    assert receiver.expect("rx_end")["verdict"] == "clean"
    sender.tx(32, span_us=100_000)
    receiver.expect("rx_begin")
    assert receiver.expect("rx_end")["verdict"] == "clean"


def test_leaving_rx_before_a_frame_means_it_is_not_heard(ether):
    ether.link(1, 2, NEAR_DB)
    sender, receiver = ether(1), ether(2)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.05)
    receiver.state("STDBY_RC")
    time.sleep(0.05)

    sender.tx(41)
    receiver.expect_nothing()


def test_a_station_in_cad_senses_a_frame_but_is_never_told_how_it_ended(ether):
    """CAD is energy, not reception: an rx_begin marked `cad`, and no rx_end,
    so nothing is ruled on and nothing is recorded as received."""
    ether.link(1, 2, FAR_DB)
    sender, sensing = ether(1), ether(2)
    sender.hello()
    sensing.hello()
    sensing.state("CAD")
    time.sleep(0.05)

    sender.tx(43, payload=b"is anyone there")
    begin = sensing.expect("rx_begin")
    assert begin["cad"] is True
    assert begin["level"] == round(level_for(FAR_DB))
    assert begin["t_end"] - begin["t0"] == FRAME_US
    sensing.expect_nothing(timeout=FRAME_US / 1e6 + 0.3)

    time.sleep(0.2)             # the record is written as the ether goes
    ends = [line for line in ether.record.read_text().splitlines()
            if '"type":"rx_end"' in line]
    assert ends == []


def test_carrier_sense_hears_energy_over_the_threshold_at_any_sf(ether):
    """At 125 kHz the sense threshold is −81 dBm. An SF7 frame at −76 dBm is
    energy to a CAD and to a receiver at SF9; one at −96 dBm is nothing to
    either, and neither is ever decoded."""
    ether.link(1, 3, 90.0)
    ether.link(1, 4, 90.0)
    ether.link(2, 3, 110.0)
    ether.link(2, 4, 110.0)
    loud, quiet, sensing, receiving = ether(1), ether(2), ether(3), ether(4)
    loud.hello()
    quiet.hello()
    sensing.hello()
    receiving.hello()
    sensing.state("CAD")
    receiving.state("RX")
    time.sleep(0.1)

    loud.tx(1, sf=7)
    for station in (sensing, receiving):
        begin = station.expect("rx_begin")
        assert begin["cad"] is True
        assert begin["level"] == round(level_for(90.0))
    time.sleep(FRAME_US / 1e6 + 0.1)
    quiet.tx(2, sf=7)
    sensing.expect_nothing()
    receiving.expect_nothing()


# A frame long enough to start listening in the middle of. Its stated sync
# word is a tenth of the way in, 40 ms, so its preamble proper ends 17 ms
# before that at SF9 and a slot starting 100 ms in has long missed it.
LONG_US = 400_000


@pytest.mark.parametrize("mode", ["RX", "CAD"])
def test_a_station_that_starts_listening_mid_frame_is_told_its_energy_only(ether, mode):
    """A frame reaches the slots listening when it starts. One that starts
    listening later, here out of standby, has missed the preamble and cannot
    demodulate the frame; its RSSI still reads it and a CAD still finds it.
    It is told the energy, from the instant it was told to the frame's end,
    and nothing else."""
    ether.link(1, 2, NEAR_DB)
    sender, late = ether(1), ether(2)
    sender.hello()
    late.hello()
    late.state("STDBY_RC")
    time.sleep(0.05)

    sender.tx(44, payload=b"already on the air", span_us=LONG_US)
    time.sleep(0.1)
    late.state(mode)
    begin = late.expect("rx_begin")
    assert begin["cad"] is True
    assert begin["level"] == round(level_for(NEAR_DB))
    assert 0 < begin["t_end"] - begin["t0"] < LONG_US - 50_000     # what is left
    late.expect_nothing(timeout=LONG_US / 1e6)                     # and no rx_end


def test_a_station_that_starts_listening_early_in_a_long_preamble_still_locks_on(ether):
    """With four symbols or more of the preamble still to come, a slot that
    starts listening finds it as one listening all along would, and receives
    the frame."""
    ether.link(1, 2, NEAR_DB)
    sender, late = ether(1), ether(2)
    sender.hello()
    late.hello()
    late.state("STDBY_RC")
    time.sleep(0.05)

    sender.tx(45, payload=b"a long preamble", span_us=LONG_US, pre_us=300_000,
              hdr_us=320_000)
    time.sleep(0.05)
    late.state("RX")
    begin = late.expect("rx_begin")
    assert "cad" not in begin
    assert begin["t_pre"] - begin["t0"] < 300_000       # measured from when it was told
    end = late.expect("rx_end")
    assert end["verdict"] == "clean"
    assert base64.b64decode(end["payload"]) == b"a long preamble"


def test_a_late_listener_is_told_only_of_frames_it_could_have_been_told_of(ether):
    """By the rules a frame's start would have applied: not a frame already
    over, not one at another spreading factor under the sense threshold, but
    one at another spreading factor over it, as energy."""
    ether.link(1, 3, 90.0)          # −76 dBm, over the −81 dBm threshold
    ether.link(2, 3, 110.0)         # −96 dBm, under it
    loud, quiet, late = ether(1), ether(2), ether(3)
    for station in (loud, quiet, late):
        station.hello()
    late.state("STDBY_RC")
    time.sleep(0.05)

    quiet.tx(46, span_us=100_000)
    time.sleep(0.2)
    late.state("RX")                # it ended before anyone listened
    late.expect_nothing()

    late.state("STDBY_RC")
    quiet.tx(47, span_us=LONG_US, sf=7)
    time.sleep(0.1)
    late.state("RX")                # another SF, and under the threshold
    late.expect_nothing(timeout=LONG_US / 1e6)

    late.state("STDBY_RC")
    loud.tx(48, span_us=LONG_US, sf=7)
    time.sleep(0.1)
    late.state("RX")                # another SF, over the threshold
    begin = late.expect("rx_begin")
    assert begin["cad"] is True
    assert begin["level"] == round(level_for(90.0))
    late.expect_nothing(timeout=LONG_US / 1e6)


def test_a_station_back_from_its_own_frame_is_told_of_one_that_began_meanwhile(ether):
    """Half duplex hides a frame that begins while a station is sending, and
    it is the frame carrier sense most needs: the station, done sending,
    wants the channel again. Back in RX, it is told the frame's energy."""
    ether.link(1, 2, NEAR_DB)
    a, b = ether(1), ether(2)
    listen(a, b)

    a.tx(51, payload=b"a first", span_us=100_000)
    b.expect("rx_begin")
    time.sleep(0.02)
    b.tx(52, payload=b"b over it", span_us=LONG_US)
    a.expect_nothing(timeout=0.15)  # sending, and then not told: not listening
    a.state("STDBY_RC")
    a.state("RX")
    begin = a.expect("rx_begin")
    assert begin["cad"] is True


@pytest.mark.parametrize("pairwise", [False, True])
def test_a_slot_is_deaf_to_a_frame_that_starts_before_its_radio_is_ready(tmp_path, pairwise):
    """A retune's BUSY, which the chip model charges when its board says so,
    leaves the radio deaf until the `ready_at` its state gives: a frame that
    starts before then is energy to it, and lost; one after it is received."""
    bed = Bench(tmp_path, "real", pairwise)
    try:
        bed.link(1, 2, NEAR_DB)
        a, b = bed(1), bed(2)
        a.hello()
        b.hello()
        b.state("RX", ready_at=b.t + 300_000)
        time.sleep(0.05)
        a.tx(41, payload=b"while it retunes", span_us=100_000)
        assert b.expect("rx_begin").get("cad") is True
        b.expect_nothing(timeout=0.3)
        time.sleep(0.1)
        a.tx(42, payload=b"once it is ready", span_us=100_000)
        assert "cad" not in b.expect("rx_begin")
        end = b.expect("rx_end")
        assert end["verdict"] == "clean"
        assert base64.b64decode(end["payload"]) == b"once it is ready"
    finally:
        bed.close()


def test_a_receiver_that_leaves_rx_mid_frame_is_not_told_how_it_ended(ether):
    """Out of RX into standby, the chip lets go of the frame it was following,
    and the medium rules on nothing it did not receive: no rx_end, and none in
    the record."""
    ether.link(1, 2, NEAR_DB)
    a, b = ether(1), ether(2)
    listen(a, b)

    a.tx(53, payload=b"left behind", span_us=LONG_US)
    assert "cad" not in b.expect("rx_begin")
    time.sleep(0.05)
    b.state("STDBY_RC")
    b.expect_nothing(timeout=LONG_US / 1e6 + 0.2)
    time.sleep(0.1)
    ends = [line for line in ether.record.read_text().splitlines()
            if '"type":"rx_end"' in line]
    assert ends == []


def test_a_receiver_retuned_mid_frame_is_not_told_how_it_ended(ether):
    """Retuned while in RX, the demodulator cannot follow a frame on the
    channel it left, so that reception is abandoned as if it had left RX."""
    ether.link(1, 2, NEAR_DB)
    a, b = ether(1), ether(2)
    listen(a, b)

    a.tx(54, payload=b"on the old channel", span_us=LONG_US)
    assert "cad" not in b.expect("rx_begin")
    time.sleep(0.05)
    b.state("RX", freq=FREQ + 600_000)
    b.expect_nothing(timeout=LONG_US / 1e6 + 0.2)


# ---- noise: the error curve and its stages ----------------------------------

def test_the_symbol_error_curve_is_the_ideal_receivers():
    """Non-coherent detection of 2^SF orthogonal signals, no implementation
    loss: reference values at each datasheet threshold, and monotonic."""
    assert ether_module.symbol_error(7, -7.5) == pytest.approx(5.2e-4, rel=0.03)
    assert ether_module.symbol_error(9, -12.5) == pytest.approx(1.0e-4, rel=0.05)
    assert ether_module.symbol_error(12, -20.0) == pytest.approx(2.2e-6, rel=0.05)
    for sf in (7, 9, 12):
        start = ether_module.SENSITIVITY_DB[sf] - 15.0
        curve = [ether_module.symbol_error(sf, start + 0.5 * i) for i in range(40)]
        assert all(a >= b for a, b in zip(curve, curve[1:])), sf
        assert curve[0] > 0.9 and curve[-1] < 1e-6


def test_the_reference_frame_fails_at_the_anchor_rate_at_each_threshold():
    """The implementation loss puts the datasheet's own test frame at its
    stated packet error rate exactly at its stated sensitivity."""
    ref = ether_module.REFERENCE_FRAME
    for sf, threshold in ether_module.SENSITIVITY_DB.items():
        snr = threshold - ether_module.implementation_loss(sf)
        per = 1.0 - ether_module.frame_odds(sf, snr, ref["payload"], ref["cr"],
                                            ref["implicit"], ref["crc"], ref["bw"])
        assert per == pytest.approx(ether_module.ANCHOR_PER, rel=0.25), sf


def test_frame_blocks_follow_the_time_on_air_arithmetic():
    """SF7, 4/5, 64 bytes with CRC and a header: (512 − 28 + 28 + 16) / 28
    rounds up to 19 blocks of 5; at SF12 and 125 kHz a symbol lasts 32.8 ms,
    so low-data-rate optimisation divides by 4·(12 − 2)."""
    assert ether_module.frame_blocks(7, 125_000, 5, 64, False, True) == (8, 19, 5, False)
    assert ether_module.frame_blocks(12, 125_000, 8, 64, False, True) == (8, 13, 8, True)
    assert ether_module.frame_blocks(7, 125_000, 5, 0, True, False)[1] == 0


def margin_loss(margin_db, sf=SF):
    """The loss that puts a link `margin_db` over its spreading factor's
    threshold, at 14 dBm on FREQ."""
    return (POWER_DBM - 20.0 * math.log10(FREQ / 868_000_000)
            - (NOISE_DBM + ether_module.SENSITIVITY_DB[sf] + margin_db))


def shaped_frame(eid, payload_len, cr=5, sf=SF, hdr="explicit", sid=1, **over):
    """A frame from `sid` with a payload this long, frame `eid` on the air
    for 1000 µs from eid ms, its preamble over the first 100 and its header
    the next 100."""
    msg = dict(radio("TX", sf=sf, cr=cr, hdr=hdr, **over), power_dbm=POWER_DBM,
               payload=base64.b64encode(bytes(payload_len)).decode())
    t0 = eid * 1000
    return ether_module.Frame(eid, eid, sid, msg, t0, t0 + 1000, t0 + 100, t0 + 200)


def arrives(medium, frame, rsid, slot=0):
    """The three stages for one frame at one receiver, in-process."""
    if not medium.locks_on(frame, rsid, slot, frame.start_us):
        return False
    if not medium.header_ok(frame, rsid, slot):
        return False
    reception = ether_module.Reception(frame, rsid, slot, medium.level_of(frame, rsid))
    return medium.payload_survives(reception, medium.payload_blocks(frame, rsid))


def noisy_medium(medium, margin_db, seed=5, receivers=(9,)):
    links = {(1, r): margin_loss(margin_db) for r in receivers}
    table = make_table("868", [1] + list(receivers), links)
    medium.set_losses({"868": table}, {name(s): s for s in [1] + list(receivers)})
    medium.seed = seed
    return medium


def failures(medium, frames, rsid=9):
    return sum(not arrives(medium, f, rsid) for f in frames) / len(frames)


def test_a_longer_frame_is_more_fragile_at_the_same_snr(medium):
    noisy_medium(medium, -1.0)
    short = failures(medium, [shaped_frame(n, 20) for n in range(2000)])
    long = failures(medium, [shaped_frame(n, 200) for n in range(2000, 4000)])
    expected = [1.0 - ether_module.frame_odds(SF, -13.5 - ether_module.implementation_loss(SF),
                                              n, 5, False, True, BW) for n in (20, 200)]
    assert short == pytest.approx(expected[0], abs=0.025)
    assert long == pytest.approx(expected[1], abs=0.04)
    assert long > short + 0.2


def test_a_correcting_coding_rate_survives_where_4_5_fails(medium):
    noisy_medium(medium, -1.0)
    plain = failures(medium, [shaped_frame(n, 200, cr=5) for n in range(2000)])
    correcting = failures(medium, [shaped_frame(n, 200, cr=8) for n in range(2000, 4000)])
    assert correcting < plain / 2


def test_the_stages_read_the_same_whatever_order_receivers_are_ruled_in():
    """Every draw is the seed's, the frame's and the receiver's, so ruling on
    two receivers in either order — fading on — gives each the same."""
    def rule(order):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            m = ether_module.Ether(None, physics=ether_module.Physics(
                fading_db=4.0, coherence_s=0.0005))
            noisy_medium(m, -1.0, receivers=(2, 3))
            got = {}
            for f in [shaped_frame(n, 60) for n in range(300)]:
                for rsid in order:
                    got[(f.eid, rsid)] = arrives(m, f, rsid)
            return got
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    one, other = rule((2, 3)), rule((3, 2))
    assert one == other
    assert len(set(one.values())) == 2


def stage_seed(key, receiver, snr, payload_len, want, sf=SF):
    """A seed under which the stages of the frame keyed `key` (`frame_key`)
    at node `receiver`, slot 0, go as `want` says ({"lock": True, "hdr":
    False, ...}), with room on each side of every draw for rounding."""
    loss = ether_module.implementation_loss(sf)
    first, blocks, per_block, correcting = ether_module.frame_blocks(
        sf, BW, 5, payload_len, False, True)

    def odds(stage, at):
        p = ether_module.symbol_error(sf, at - loss)
        if stage == "lock":
            return ether_module.lock_odds(p)
        if stage == "hdr":
            return ether_module.block_survives(p, first, True)
        return ether_module.block_survives(p, per_block, correcting) ** blocks

    for seed in range(20_000):
        ok = True
        for stage, passes in want.items():
            draw = ether_module.seeded_draw(seed, key, receiver, 0, stage)
            if passes and not draw < odds(stage, snr - 0.2):
                ok = False
            if not passes and not draw >= odds(stage, snr + 0.2):
                ok = False
        if ok:
            return seed
    raise AssertionError("no seed gives %r" % (want,))


class Air:
    """An ether in-process on a clock the test moves, every station in RX and
    what it is sent kept: a frame's start, and so its draws, are known before
    it goes out. `links` maps a pair of station ids to dB, both ways;
    `states` a station id to what its RX state says beside `radio()`'s."""

    ADDR = ("127.0.0.1", 0)

    def __init__(self, medium, links, seed=0, states=None):
        state = states or {}
        self.medium = medium
        medium.clock = ether_module.VirtualClock()
        medium.seed = seed
        self.sent = []
        medium.send = lambda sid, msg: self.sent.append((sid, msg))
        sids = sorted({s for pair in links for s in pair})
        both = dict(links)
        both.update({(b, a): db for (a, b), db in links.items()})
        medium.set_losses({"868": make_table("868", sids, both)},
                          {name(s): s for s in sids})
        for sid in sids:
            over = dict(state.get(sid, {}))
            mode = over.pop("mode", "RX")
            medium.recv_state(sid, self.ADDR, dict({"type": "state", "slot": 0},
                                                   **radio(mode, **over)))

    def tx(self, sid, payload, span_us=300_000, **over):
        t0 = self.medium.clock.t
        self.medium.recv_tx(sid, self.ADDR, dict(
            {"type": "tx", "slot": 0, "id": 1, "t0": t0, "t_pre": t0 + span_us // 10,
             "t_hdr": t0 + span_us // 5, "t_end": t0 + span_us, "power_dbm": POWER_DBM,
             "payload": base64.b64encode(payload).decode()}, **radio("TX", **over)))

    def run_to(self, t):
        self.medium.clock.t = t
        due = self.medium.clock.pop_due()
        while due is not None:
            callback, args = due
            callback(*args)
            due = self.medium.clock.pop_due()

    def told(self, sid, kind):
        return [m for s, m in self.sent if s == sid and m["type"] == kind]


def seeded_air(medium, margin_db, want, payload, extra=None, at=0):
    """Station 1 `margin_db` over its threshold at station 2, the seed one
    under which station 1's frame of `payload`, sent at `at`, meets the
    stages there as `want` says; `extra` more links."""
    loss = margin_loss(margin_db)
    key = ether_module.frame_key(name(1), at, base64.b64encode(payload).decode())
    seed = stage_seed(key, name(2), level_for(loss) - NOISE_DBM, len(payload), want)
    links = {(1, 2): loss}
    links.update(extra or {})
    air = Air(medium, links, seed)
    air.medium.clock.t = at
    return air


def test_a_header_error_ends_the_reception_at_t_hdr_and_frees_the_slot(medium):
    air = seeded_air(medium, -3.0, {"lock": True, "hdr": False}, b"weak!",
                     {(3, 2): NEAR_DB})
    air.tx(1, b"weak!")
    begin, = air.told(2, "rx_begin")
    assert begin["hdr_ok"] is False and "cad" not in begin
    air.run_to(begin["t_hdr"])
    end, = air.told(2, "rx_end")
    assert (end["verdict"], end["cause"], end["t"]) == ("hdr", "noise", begin["t_hdr"])

    air.run_to(begin["t_hdr"] + 1000)
    air.tx(3, b"after the header")
    later = air.told(2, "rx_begin")[-1]
    assert "cad" not in later and "hdr_ok" not in later, "the slot locks again"
    air.run_to(begin["t_end"] + 400_000)
    ends = air.told(2, "rx_end")
    assert [(e["id"], e["verdict"]) for e in ends] == [
        (begin["id"], "hdr"), (later["id"], "clean")], "the failed frame ends once"


def test_a_header_error_does_not_reach_a_receiver_that_transmitted_first(medium):
    air = seeded_air(medium, -3.0, {"lock": True, "hdr": False}, b"weak!")
    air.tx(1, b"weak!")
    begin, = air.told(2, "rx_begin")
    assert begin["hdr_ok"] is False
    air.run_to(1000)
    air.tx(2, b"b speaks", span_us=10_000)
    air.run_to(begin["t_end"])
    end, = [e for e in air.told(2, "rx_end") if e["id"] == begin["id"]]
    assert (end["verdict"], end["cause"], end["t"]) == ("crc", "talked_over", begin["t_end"])


def test_a_payload_that_fails_is_a_crc_error_at_t_end(medium):
    payload = bytes(200)
    air = seeded_air(medium, -1.0, {"lock": True, "hdr": True, "payload": False}, payload)
    air.tx(1, payload)
    begin, = air.told(2, "rx_begin")
    assert "hdr_ok" not in begin
    air.run_to(begin["t_end"])
    end, = air.told(2, "rx_end")
    assert (end["verdict"], end["cause"], end["t"]) == ("crc", "noise", begin["t_end"])


def test_a_frames_draws_are_its_channels_not_the_runs_numbering():
    """Two arms differing only in a frame sent before, which renumbers every
    frame after it, rule the same on every frame they share."""
    def arm(before):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            air = Air(ether_module.Ether(None), {(1, 2): margin_loss(-1.5),
                                                 (3, 4): NEAR_DB}, seed=9)
            if before:
                air.tx(3, b"only in this arm", span_us=1000)
            got = []
            for n in range(60):
                at = 10_000 + n * 400_000
                air.run_to(at)
                air.tx(1, b"frame %d" % n)
                air.run_to(at + 300_000)
            for msg in air.told(2, "rx_begin") + air.told(2, "rx_end"):
                got.append((msg["type"], msg["t0"] if "t0" in msg else msg["t"],
                            msg.get("hdr_ok"), msg.get("verdict")))
            return got
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    one, other = arm(False), arm(True)
    assert one == other
    assert {g[3] for g in one if g[0] == "rx_end"} >= {"clean", "crc"}


# ---- fading ----------------------------------------------------------------

def unit_process(seed=3, coherence=1.0):
    def knot(k):
        return ether_module.fade_knot(seed, "n1", "n2", k)
    return lambda t: ether_module.fade_unit(knot, t, coherence)


def test_without_fading_a_level_is_the_tables(medium):
    noisy_medium(medium, 3.0)
    f = shaped_frame(1, 20)
    assert all(medium.level_at(f, 9, t) == medium.level_of(f, 9)
               for t in (0, 1e6, 7.3e6, 1e9))


def test_fading_is_a_unit_gaussian_continuous_at_its_knots():
    g = unit_process()
    samples = [g(i + 0.37) for i in range(20_000)]
    mean = sum(samples) / len(samples)
    var = sum((s - mean) ** 2 for s in samples) / len(samples)
    assert abs(mean) < 0.05
    assert var == pytest.approx(1.0, abs=0.06)
    for k in range(1, 50):
        assert g(k - 1e-9) == pytest.approx(g(k), abs=1e-6)


def test_fading_is_the_pairs_whichever_way_it_is_asked(medium):
    medium.physics = ether_module.Physics(fading_db=5.0)
    noisy_medium(medium, 3.0, receivers=(2,))
    for t in (0, 3.3e6, 123.4e6):
        assert medium.fade_db(1, 2, t) == medium.fade_db(2, 1, t)
    assert medium.fade_db(1, 2, 3.3e6) != 0.0


def test_fading_is_correlated_over_its_coherence_time_and_not_beyond():
    g = unit_process()
    rng = __import__("random").Random(1)
    starts = [rng.uniform(0, 40_000) for _ in range(20_000)]

    def corr(lag):
        return sum(g(t) * g(t + lag) for t in starts) / len(starts)

    assert corr(0.1) > 0.9
    assert corr(1.0) == pytest.approx(1.0 / math.pi, abs=0.04)
    assert abs(corr(2.0)) < 0.04


def rician_gains(k, n=20_000):
    return [10.0 ** (ether_module.rician_db(
        k, lambda part, i=i: ether_module.seeded_draw(4, i, part)) / 10.0) for i in range(n)]


def test_rician_fading_has_unit_mean_power_and_shrinks_with_k():
    for k in (0.0, 3.0, 20.0):
        gains = rician_gains(k)
        assert sum(gains) / len(gains) == pytest.approx(1.0, abs=0.03), k
    rayleigh = rician_gains(0.0, 4000)
    assert sum(g < 0.1 for g in rayleigh) / len(rayleigh) == pytest.approx(
        1.0 - math.exp(-0.1), abs=0.02), "Rayleigh power is exponential"
    assert all(abs(10 * math.log10(g)) < 1.0 for g in rician_gains(1000.0, 1000))


def test_rician_fading_is_one_draw_per_frame_and_receiver(medium):
    medium.physics = ether_module.Physics(rician_k=2.0)
    noisy_medium(medium, 3.0, receivers=(2, 3))
    f = shaped_frame(1, 20)
    at = [medium.level_at(f, 2, t) - medium.level_of(f, 2) for t in (1000, 1500, 1999)]
    assert at[0] == at[1] == at[2] != 0.0
    assert medium.rice_db(f, 3) != medium.rice_db(f, 2)
    assert medium.rice_db(shaped_frame(2, 20), 2) != at[0]


def test_a_physics_says_only_what_was_asked_and_round_trips():
    assert ether_module.Physics().as_dict() == {"noise_figure_db": 6.0}
    assert ether_module.Physics().coherence_s == 3600.0
    physics = ether_module.Physics(5.0, fading_db=4.0, coherence_s=3.0, rician_k=6.0)
    assert physics.as_dict() == {"noise_figure_db": 5.0, "fading_db": 4.0,
                                 "coherence_s": 3.0, "rician_k": 6.0}
    again = ether_module.Physics.from_dict(physics.as_dict())
    assert (again.fading_db, again.coherence_s, again.rician_k) == (4.0, 3.0, 6.0)
    assert "fading 4.0 dB over 3 s" in physics.describe()
    assert "Rician K 6" in physics.describe()
    for bad in ({"fading_db": -1.0}, {"coherence_s": 0.0}, {"rician_k": -0.5}):
        with pytest.raises(ValueError):
            ether_module.Physics(**bad)


# ---- side detectors ----------------------------------------------------------

def test_a_side_detector_decodes_its_own_sf_and_sync_word_on_the_main_carrier():
    detector = ether_module.Ether.detector
    state = radio("RX", side=[{"sf": 10, "sync": 0x12, "iq": "normal"},
                              {"sf": 11, "sync": SYNC, "iq": "normal"}])
    assert detector(state, radio("TX")) == 0
    assert detector(state, radio("TX", sf=10, sync=0x12)) == 1
    assert detector(state, radio("TX", sf=11)) == 2
    assert detector(state, radio("TX", sf=10)) is None, "side 1 has its own sync word"
    assert detector(state, radio("TX", sf=10, sync=0x12, iq="inverted")) is None
    assert detector(state, radio("TX", sf=10, sync=0x12, bw=250_000)) is None
    assert detector(state, radio("TX", sf=10, sync=0x12, freq=FREQ + 200_000)) is None
    assert detector(radio("RX"), radio("TX", sf=10, sync=0x12)) is None


def test_a_side_detectors_preamble_miss_follows_the_bench():
    miss = ether_module.side_preamble_miss
    assert [miss(n) for n in (8, 12, 13, 14, 15, 16, 32)] == pytest.approx(
        [0.019, 0.019, 0.0115, 0.004, 0.002, 0.0, 0.0])


def test_a_side_detector_misses_short_preambles_where_noise_does_not(medium):
    noisy_medium(medium, 25.0)
    for pre, rate in ((12, 0.019), (16, 0.0)):
        frames = [shaped_frame(n, 20, pre=pre) for n in range(6000)]
        main = sum(not medium.locks_on(f, 9, 0, f.start_us) for f in frames)
        side = sum(not medium.locks_on(f, 9, 0, f.start_us, det=1) for f in frames)
        assert main == 0
        assert side / len(frames) == pytest.approx(rate, abs=0.005), pre


def test_a_side_detector_lock_says_which_and_cad_is_the_main_detectors(medium):
    side = [{"sf": 10, "sync": 0x12, "iq": "normal"}]
    air = Air(medium, {(1, 2): NEAR_DB, (1, 3): NEAR_DB},
              states={2: {"side": side}, 3: {"mode": "CAD", "side": side}})
    air.tx(1, b"to the side", sf=10, sync=0x12, pre=16)
    begin, = air.told(2, "rx_begin")
    assert begin["det"] == 1 and "cad" not in begin
    air.run_to(begin["t_end"])
    assert air.told(2, "rx_end")[0]["verdict"] == "clean"
    assert air.told(3, "rx_begin") == [], "CAD finds the main detector's frames only"
    air.run_to(begin["t_end"] + 1000)
    air.tx(1, b"to the main one", pre=16)
    assert "det" not in air.told(2, "rx_begin")[-1]


# ---- the side detectors' loss --------------------------------------------------

def test_without_a_side_detector_table_a_multi_sf_receiver_loses_nothing(ether):
    """Off by default: frames of 12 symbols that side detectors catch arrive."""
    ether.link(1, 2, NEAR_DB)
    sender, multi = ether(1), ether(2)
    sender.hello()
    multi.hello()
    multi.state("RX", sf=7, sfs=[5, 6, 7])
    time.sleep(0.1)
    for n in range(4):
        sender.tx(90 + n, span_us=100_000, sf=7, pre=12)
        assert multi.expect("rx_end")["verdict"] == "clean"


def test_a_frame_arrives_faded_at_its_start_and_unfaded_without_fading(medium):
    """The level a frame arrives at moves by the link's slow fade at its
    start, alike both ways (`fade_db`); without fading it is the table's,
    as ever (`level_of`, which `level_at` adds the fade to)."""
    table = make_table("868", [1, 2], {(1, 2): 110.0, (2, 1): 110.0})
    medium.set_losses({"868": table}, {"n1": 1, "n2": 2})
    assert not medium.physics.fading
    plain = frame(medium, 1, 2_000_000, 2_300_000)
    assert medium.level_of(plain, 2) == medium.level(1, 2, FREQ, POWER_DBM)

    medium.physics = ether_module.Physics(fading_db=4.0)
    there = frame(medium, 1, 2_000_000, 2_300_000)
    expected = medium.fade_db(1, 2, 2_000_000)
    assert expected != 0.0
    assert expected == medium.fade_db(2, 1, 2_000_000)
    assert medium.level_at(there, 2, 2_000_000) == pytest.approx(
        medium.level(1, 2, FREQ, POWER_DBM) + expected)


def outcomes(lead, locked=False, pairs=3000):
    """bench_outcome over many pairs of frames at one receiver."""
    return [ether_module.bench_outcome(11, 2 * n + 1, 2 * n + 2, 5, lead, locked)
            for n in range(pairs)]


def test_bench_equals_are_both_lost_one_time_in_four_and_never_both_kept():
    seen = outcomes(0.5)
    assert (True, True) not in seen
    both_lost = seen.count((False, False)) / len(seen)
    assert both_lost == pytest.approx(9 / 39, abs=0.03)
    first = seen.count((True, False)) / (len(seen) - seen.count((False, False)))
    assert first == pytest.approx(0.5, abs=0.04)


def test_bench_keeps_the_stronger_nine_times_in_ten_at_2_db_and_never_the_weaker():
    seen = outcomes(2.0)
    assert all(second is False for _, second in seen)
    kept = sum(first for first, _ in seen) / len(seen)
    assert kept == pytest.approx(119 / 136, abs=0.025)
    assert set(outcomes(-2.0)) <= {(False, True), (False, False)}


def test_bench_keeps_the_stronger_every_time_from_6_1_db():
    assert set(outcomes(6.1, pairs=500)) == {(True, False)}
    assert set(outcomes(-7.0, pairs=500)) == {(False, True)}


def test_bench_a_late_frame_is_never_received_and_a_stronger_one_spoils_both():
    assert set(outcomes(-2.0, locked=True, pairs=500)) == {(False, False)}
    assert set(outcomes(7.0, locked=True, pairs=500)) == {(True, False)}
    assert all(second is False for _, second in outcomes(0.0, locked=True))


@pytest.fixture
def bench(tmp_path):
    """The ether with bench capture, its seed pinned."""
    bed = Bench(tmp_path, "real", False, "--bench-capture", "--seed", "3")
    try:
        yield bed
    finally:
        bed.close()


def test_bench_capture_keeps_a_frame_8_db_up_as_the_same_sf_figure_does(bench):
    """The hidden terminal, judged as the bench saw it: 8 dB is past 6.1."""
    bench.link(1, 2, 110.0)
    bench.link(3, 2, 118.0)
    a, b, c = bench(1), bench(2), bench(3)
    listen(a, b, c)

    a.tx(151, payload=b"from a")
    c.tx(152, payload=b"from c")
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "clean"
    assert ends[b"from c"]["verdict"] == "crc"


def test_bench_capture_frames_a_decibel_apart_end_as_their_pairs_draw(bench):
    """1 dB apart, the two are equals, and which survives, if either, is the
    pair's draw from the seed: the lock and both verdicts read the same one."""
    bench.link(1, 2, 110.0)
    bench.link(3, 2, 111.0)
    a, b, c = bench(1), bench(2), bench(3)
    seed = a.hello()["seed"]
    listen(b, c)
    lead = level_for(110.0) - level_for(111.0)
    for n in range(5):
        a.tx(160 + n, payload=b"a %d" % n, pre_us=100_000, hdr_us=120_000)
        c.tx(170 + n, payload=b"c %d" % n, pre_us=100_000, hdr_us=120_000)
        first, second = b.expect("rx_begin"), b.expect("rx_begin")
        ends = b.ends(2)

        def key(sender, begin, payload):
            return ether_module.frame_key(sender, begin["t0"],
                                          base64.b64encode(payload).decode())

        want = ether_module.bench_outcome(seed, key("n1", first, b"a %d" % n),
                                          key("n3", second, b"c %d" % n), "n2", lead, False)
        assert ends[b"a %d" % n]["verdict"] == ("clean" if want[0] else "crc")
        assert ends[b"c %d" % n]["verdict"] == ("clean" if want[1] else "crc")
        assert ("cad" in second) == (not want[1]), "the second takes b only if it survives"


def test_bench_capture_a_stronger_frame_after_the_preamble_spoils_both(bench):
    """b follows a's frame; c's lands after its preamble, 8 dB louder: it
    never takes b, and a is lost as well."""
    bench.link(1, 2, 118.0)
    bench.link(3, 2, 110.0)
    a, b, c = bench(1), bench(2), bench(3)
    listen(a, b, c)

    a.tx(181, payload=b"from a")
    assert "cad" not in b.expect("rx_begin")
    time.sleep(FRAME_US / 4e6)          # past a's preamble, a tenth of the frame
    c.tx(182, payload=b"from c")
    assert b.expect("rx_begin")["cad"] is True
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "crc"
    assert ends[b"from c"]["verdict"] == "crc"


def test_bench_capture_a_weaker_frame_after_the_preamble_leaves_the_first(bench):
    """b follows a's frame; c's lands after its preamble, 8 dB quieter."""
    bench.link(1, 2, 110.0)
    bench.link(3, 2, 118.0)
    a, b, c = bench(1), bench(2), bench(3)
    listen(a, b, c)

    a.tx(191, payload=b"from a")
    b.expect("rx_begin")
    time.sleep(FRAME_US / 4e6)
    c.tx(192, payload=b"from c")
    assert b.expect("rx_begin")["cad"] is True
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "clean"
    assert ends[b"from c"]["verdict"] == "crc"


def test_bench_capture_keeps_each_frame_at_the_listener_nearer_its_sender(bench):
    """Two frames that meet are each kept where their sender is 10 dB nearer."""
    bench.link(1, 2, 105.0)
    bench.link(3, 2, 115.0)
    bench.link(1, 4, 115.0)
    bench.link(3, 4, 105.0)
    a, near_a, c, near_c = bench(1), bench(2), bench(3), bench(4)
    listen(a, near_a, c, near_c)

    a.tx(201, payload=b"from a")
    c.tx(202, payload=b"from c")
    at_a, at_c = near_a.ends(2), near_c.ends(2)
    assert at_a[b"from a"]["verdict"] == "clean" and at_a[b"from c"]["verdict"] == "crc"
    assert at_c[b"from c"]["verdict"] == "clean" and at_c[b"from a"]["verdict"] == "crc"


def test_bench_capture_is_not_a_variant_of_the_pairwise_rule(tmp_path):
    done = subprocess.run([sys.executable, ETHER, "--bench-capture", "--pairwise",
                           "--record", str(tmp_path / "r.tsv")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 2 and "--bench-capture" in done.stderr


def test_a_station_is_deaf_while_its_own_frame_is_going_out(ether):
    """Half duplex: a radio transmitting hears nothing, however loud."""
    ether.link(1, 2, NEAR_DB)
    a, b = ether(1), ether(2)
    listen(a, b)

    b.tx(61, payload=b"b is talking")
    a.expect("rx_begin")
    a.tx(62, payload=b"and so is a")
    b.expect_nothing(timeout=0.2)
    end = a.expect("rx_end")
    assert end["verdict"] == "crc", "a talked over the frame it heard"
    assert end["cause"] == "talked_over"


def test_frames_that_share_a_station_id_are_still_told_apart(ether):
    """Each station numbers its own frames, so a receiver cannot use that
    number: the id in a reception is the ether's, and no two frames share it."""
    ether.link(1, 2, NEAR_DB)
    ether.link(3, 2, FAR_DB)
    a, b, c = ether(1), ether(2), ether(3)
    listen(a, b, c)

    a.tx(1, payload=b"from a")
    c.tx(1, payload=b"from c")
    first, second = b.expect("rx_begin"), b.expect("rx_begin")
    assert first["id"] != second["id"]


def test_the_louder_of_two_hidden_frames_keeps_the_receiver(ether):
    """The hidden terminal: a and c cannot hear each other, so both transmit.
    a arrives 8 dB over c and first: b keeps a's frame, and c's is energy."""
    ether.link(1, 2, 110.0)
    ether.link(3, 2, 118.0)
    a, b, c = ether(1), ether(2), ether(3)
    listen(a, b, c)

    a.tx(91, payload=b"from a")
    assert "cad" not in b.expect("rx_begin")
    c.tx(92, payload=b"from c")
    assert b.expect("rx_begin")["cad"] is True

    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "clean", "a leads c by more than 6 dB"
    assert ends[b"from c"]["verdict"] == "crc"
    a.expect_nothing()
    c.expect_nothing()


def test_a_louder_frame_takes_the_receiver_at_its_preamble(ether):
    """The quieter frame starts first and has the receiver; the louder one
    arrives 8 dB up, takes it, and the first is lost there."""
    ether.link(1, 2, 118.0)
    ether.link(3, 2, 110.0)
    quiet, b, loud = ether(1), ether(2), ether(3)
    listen(quiet, b, loud)

    quiet.tx(1, payload=b"quiet")
    assert "cad" not in b.expect("rx_begin")
    time.sleep(0.03)
    loud.tx(2, payload=b"loud")
    assert "cad" not in b.expect("rx_begin"), "the louder frame takes the lock"

    ends = b.ends(2)
    assert ends[b"quiet"]["verdict"] == "crc"
    assert ends[b"quiet"]["cause"] == "lost"
    assert ends[b"loud"]["verdict"] == "clean"
    assert "cause" not in ends[b"loud"]


def test_two_frames_within_the_same_sf_figure_spoil_each_other(ether):
    ether.link(1, 2, 110.0)
    ether.link(3, 2, 114.0)
    a, b, c = ether(1), ether(2), ether(3)
    listen(a, b, c)

    a.tx(101, payload=b"from a")
    c.tx(102, payload=b"from c")
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "crc"
    assert ends[b"from c"]["verdict"] == "crc"


def test_a_receiver_that_hears_only_one_of_two_colliding_frames_keeps_it(ether):
    """The collision is at b; the table gives d no path from a."""
    ether.link(1, 2, 110.0)
    ether.link(3, 2, 110.0)
    ether.link(3, 4, 110.0)
    a, b, c, d = ether(1), ether(2), ether(3), ether(4)
    listen(a, b, c, d)

    a.tx(111, payload=b"from a")
    c.tx(112, payload=b"from c")

    ends = b.ends(2)             # equal from both, so it keeps neither
    assert ends[b"from a"]["verdict"] == "crc"
    assert ends[b"from c"]["verdict"] == "crc"

    end = d.expect("rx_end")     # d heard only c, and cleanly
    assert base64.b64decode(end["payload"]) == b"from c"
    assert end["verdict"] == "clean"


def summed_interference(bench):
    """A frame at −96 dBm and two same-SF interferers at −104 dBm each,
    hidden from one another. Each alone is 8 dB under it; summed they are
    −100.99 dBm, 4.99 dB under it, which is short of the 6 dB figure."""
    bench.link(1, 4, 110.0)
    bench.link(2, 4, 118.0)
    bench.link(3, 4, 118.0)
    s, i1, i2, rx = bench(1), bench(2), bench(3), bench(4)
    listen(s, i1, i2, rx)
    s.tx(1, payload=b"signal")
    time.sleep(0.02)
    i1.tx(2, payload=b"one")
    i2.tx(3, payload=b"two")
    return rx.ends(3)


def test_interference_is_summed_within_a_class(ether):
    ends = summed_interference(ether)
    assert ends[b"signal"]["verdict"] == "crc"
    assert ends[b"signal"]["cause"] == "interference"


def test_the_pairwise_rule_takes_each_interferer_alone(pairwise):
    ends = summed_interference(pairwise)
    assert ends[b"signal"]["verdict"] == "clean"


@pytest.fixture
def oracle(tmp_path):
    """The ether without interference (`--no-interference`)."""
    bed = Bench(tmp_path, "real", False, "--no-interference")
    try:
        yield bed
    finally:
        bed.close()


def test_without_interference_a_frame_is_judged_against_noise_alone(oracle):
    ends = summed_interference(oracle)
    assert ends[b"signal"]["verdict"] == "clean"


def test_without_interference_a_louder_frame_does_not_take_the_receiver(oracle):
    """The louder frame arriving 8 dB up would take the receiver; without
    interference the receiver keeps the frame it follows, which ends clean,
    and the louder one is energy to it."""
    oracle.link(1, 2, 118.0)
    oracle.link(3, 2, 110.0)
    quiet, b, loud = oracle(1), oracle(2), oracle(3)
    listen(quiet, b, loud)

    quiet.tx(1, payload=b"quiet")
    assert "cad" not in b.expect("rx_begin")
    time.sleep(0.03)
    loud.tx(2, payload=b"loud")
    assert b.expect("rx_begin")["cad"] is True, "one frame at a time, the first kept"

    ends = b.ends(2)
    assert ends[b"quiet"]["verdict"] == "clean"
    assert ends[b"loud"]["verdict"] == "crc"


def test_without_interference_a_receiver_still_cannot_hear_while_it_sends(oracle):
    oracle.link(1, 2, 110.0)
    a, b = oracle(1), oracle(2)
    listen(a, b)
    a.tx(1, payload=b"from a")
    assert "cad" not in b.expect("rx_begin")
    b.tx(2, payload=b"from b")
    end = b.expect("rx_end")
    assert end["verdict"] == "crc", "half duplex is the radio's, not interference"
    assert end["cause"] == "talked_over"


def test_without_interference_is_said_and_kept_with_the_run():
    physics = ether_module.Physics(interference=False)
    assert "no interference" in physics.describe()
    assert physics.as_dict()["interference"] is False
    assert ether_module.Physics.from_dict(physics.as_dict()).interference is False
    assert "interference" not in ether_module.Physics().as_dict()
    assert ether_module.Physics.from_dict({}).interference is True


# ---------------------------------------------------------------------------
# The hand-built case: two hidden senders, one receiver
# ---------------------------------------------------------------------------
#
# A (station 1) and C (station 3) cannot hear each other; B (station 2)
# listens at SF9, 125 kHz. Both transmit at 14 dBm; C goes first and A 30 ms
# later, so their air overlaps for all but the ends. With the figures table:
#
#   noise at B     N = −174 + 10·log10(125 000) + 6          = −117.03 dBm
#   SF9 threshold      SENSITIVITY_DB[9]                      = −12.5 dB
#   same-SF figure     SAME_SF_REJECTION_DB                   = 6 dB
#   SF9 over SF7       INTER_SF_REJECTION_DB[9][7]            = −15 dB
#   sense threshold    10·log10(125) − 117 + 15               = −81.03 dBm
#
# Every frame below is more than 15 dB over its threshold, where the three
# stages against noise pass with certainty: only interference decides.
#
# Same SF (both at SF9). Loss A→B 120 dB, C→B 127 dB:
#   S_A = 14 − 120 = −106 dBm, SNR 11.03 ≥ −12.5: decodable.
#   S_C = 14 − 127 = −113 dBm, SNR 4.03 ≥ −12.5: decodable, and B locks on.
#   A's preamble: S_A − S_C = 7 ≥ 6, so A takes B off C: C is crc.
#   A's air, first piece (C on the air): S_A − S_C = 7 ≥ 6; second piece
#   (C gone): no interference. A is clean.
#
# Different SF (A at SF9, C at SF7). Loss A→B 120 dB, C→B 108 dB:
#   S_C = −94 dBm: not decodable at B (SF7 ≠ SF9), and under −81.03 dBm, so
#   B is told nothing of it.
#   S_A = −106 dBm, SNR 11.03 ≥ −12.5; against the SF7 class
#   S_A − S_C = −12 ≥ −15. A is clean.
#   With C→B 100 dB instead, S_C = −86: −20 < −15, and A is crc.
#   Under the pairwise rule the 108 dB case is crc too: C is audible at its
#   own SF (−94 ≥ −117.03 − 7.5) and A leads it by −12 < 6.

def test_the_figures_the_hand_calculation_uses():
    assert ether_module.SENSITIVITY_DB[9] == -12.5
    assert ether_module.SAME_SF_REJECTION_DB == 6.0
    assert ether_module.INTER_SF_REJECTION_DB[9][7] == -15
    assert ether_module.rejection_db(9, 7) == -15.0
    assert ether_module.rejection_db(9, 9) == 6.0
    assert ether_module.sense_threshold_dbm(BW) == pytest.approx(-81.03, abs=0.01)
    assert NOISE_DBM == pytest.approx(-117.03, abs=0.01)


def hidden_pair(bench, loss_c, sf_c):
    bench.link(1, 2, 120.0)
    bench.link(3, 2, loss_c)
    a, b, c = bench(1), bench(2), bench(3)
    listen(a, b, c)
    c.tx(1, payload=b"C", sf=sf_c)
    time.sleep(0.03)
    a.tx(2, payload=b"A")
    return b


def test_hand_case_same_sf(ether):
    b = hidden_pair(ether, 127.0, SF)
    begins = [b.expect("rx_begin"), b.expect("rx_begin")]
    assert [m.get("cad") for m in begins] == [None, None]
    assert [m["level"] for m in begins] == [-113, -106]
    ends = b.ends(2)
    assert ends[b"C"]["verdict"] == "crc"
    assert ends[b"A"]["verdict"] == "clean"


@pytest.mark.parametrize("loss_c, verdict", [(108.0, "clean"), (100.0, "crc")])
def test_hand_case_different_sf(ether, loss_c, verdict):
    b = hidden_pair(ether, loss_c, 7)
    begin = b.expect("rx_begin")
    assert (begin["level"], begin.get("cad")) == (-106, None), "C is nothing to B"
    end = b.expect("rx_end")
    assert base64.b64decode(end["payload"]) == b"A"
    assert end["verdict"] == verdict
    b.expect_nothing(timeout=0.2)


def test_hand_case_different_sf_under_the_pairwise_rule(pairwise):
    b = hidden_pair(pairwise, 108.0, 7)
    end = b.expect("rx_end")
    assert base64.b64decode(end["payload"]) == b"A"
    assert end["verdict"] == "crc"


# ---------------------------------------------------------------------------
# The medium in-process: tables, and the verdict's pieces
# ---------------------------------------------------------------------------

@pytest.fixture
def medium():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield ether_module.Ether(None)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_level_reads_the_band_table_with_the_within_band_correction(medium):
    t433 = make_table("433", [1, 2], {(1, 2): 100.0})
    t868 = make_table("868", [1, 2], {(1, 2): 110.0})
    medium.set_losses({"433": t433, "868": t868}, {"n1": 1, "n2": 2}, {1: 3.0})
    assert medium.level(1, 2, 433_920_000, 14) == pytest.approx(-83.0)
    assert medium.level(1, 2, 520_000_000, 14) == pytest.approx(
        -83.0 - 20 * math.log10(520e6 / 433.92e6))
    assert medium.level(1, 2, 868_000_000, 14) == pytest.approx(-93.0)
    assert medium.level(1, 2, 915_000_000, 14) is None, "no table for the band"
    assert medium.level(2, 1, 868_000_000, 14) is None, "the pair is one way"
    assert medium.level(1, 9, 868_000_000, 14) is None, "no such node"


def test_a_node_row_update_takes_effect_whole(medium):
    table = make_table("868", [1, 2, 3], {(1, 2): 100.0, (2, 1): 100.0, (1, 3): 120.0})
    medium.set_losses({"868": table}, {"n1": 1, "n2": 2, "n3": 3})
    medium.update_node("n1", {"868": ({"n2": 90.0, "n3": math.inf}, {"n2": 95.0})})
    assert medium.level(1, 2, 868e6, 14) == pytest.approx(-76.0)
    assert medium.level(2, 1, 868e6, 14) == pytest.approx(-81.0)
    assert medium.level(1, 3, 868e6, 14) is None
    assert table.get("n1", "n2") == 100.0, "the table handed in is never written"


def test_levels_lists_only_the_stations_that_could_decode(medium):
    table = make_table("868", [1, 2, 3], {(1, 2): 100.0, (1, 3): 150.0})
    medium.set_losses({"868": table}, {"n1": 1, "n2": 2, "n3": 3})
    heard = medium.levels(1, 868e6, sf=9)
    assert list(heard) == [2]
    assert heard[2] == pytest.approx(-86.0)


def frame(medium, sid, start, end, sf=SF, eid=[0]):
    eid[0] += 1
    msg = dict(radio("TX", sf=sf), power_dbm=POWER_DBM)
    return ether_module.Frame(eid[0], eid[0], sid, msg, start, end, start, start)


def verdict(medium, signal, others, rsid=9):
    for other in others:
        signal.interferers.append(other)
    level = medium.level_of(signal, rsid)
    return medium.verdict_for(ether_module.Reception(signal, rsid, 0, level))[0]


def test_the_worst_piece_decides_and_only_overlap_is_summed(medium):
    """Two interferers 8 dB under the frame: one in each half of it, they are
    never on the air together and it survives; overlapping, their sum is
    4.99 dB under it and it does not."""
    table = make_table("868", [1, 2, 3, 9],
                       {(1, 9): 110.0, (2, 9): 118.0, (3, 9): 118.0})
    medium.set_losses({"868": table}, {"n1": 1, "n2": 2, "n3": 3, "n9": 9})
    apart = verdict(medium, frame(medium, 1, 0, 1000),
                    [frame(medium, 2, 100, 400), frame(medium, 3, 600, 900)])
    assert apart == "clean"
    together = verdict(medium, frame(medium, 1, 0, 1000),
                       [frame(medium, 2, 100, 600), frame(medium, 3, 500, 900)])
    assert together == "crc"


def test_each_sf_is_its_own_class(medium):
    """An SF7 and an SF8 interferer, each within its own figure of an SF9
    frame, do not add to each other."""
    table = make_table("868", [1, 2, 3, 9],
                       {(1, 9): 120.0, (2, 9): 107.0, (3, 9): 109.0})
    medium.set_losses({"868": table}, {"n1": 1, "n2": 2, "n3": 3, "n9": 9})
    # SF7 at −93: −106 − (−93) = −13 ≥ −15. SF8 at −95: −11 ≥ −13.
    got = verdict(medium, frame(medium, 1, 0, 1000),
                  [frame(medium, 2, 0, 1000, sf=7), frame(medium, 3, 0, 1000, sf=8)])
    assert got == "clean"


def test_an_off_band_transmission_is_no_interference(medium):
    table = make_table("868", [1, 2, 9], {(1, 9): 120.0, (2, 9): 80.0})
    medium.set_losses({"868": table}, {"n1": 1, "n2": 2, "n9": 9})
    signal = frame(medium, 1, 0, 1000)
    other = frame(medium, 2, 0, 1000)
    other.freq = FREQ + BW
    assert not signal.shares_air(other)
    assert verdict(medium, signal, []) == "clean"


def test_the_unknown_is_ignored_and_the_record_holds_every_message(ether):
    ether.link(1, 2, NEAR_DB)
    sender, receiver = ether(1), ether(2)
    sender.hello()
    listen(receiver)
    sender.send({"type": "wait", "until": 5})
    sender.sock.sendto(b"not json at all", sender.ether)
    time.sleep(0.1)
    sender.tx(61, payload=b"recorded")
    receiver.expect("rx_begin")
    receiver.expect("rx_end")
    time.sleep(0.2)

    lines = [l for l in ether.record.read_text().splitlines() if not l.startswith("#")]
    rows = [l.split("\t") for l in lines]
    assert all(len(r) == 4 for r in rows)
    kinds = [(r[1], r[2], json.loads(r[3]).get("type")) for r in rows]
    assert ("in", "-", None) in kinds, "what is not JSON is recorded raw"
    assert ("in", "1", "hello") in kinds
    assert ("out", "1", "welcome") in kinds
    assert ("in", "2", "state") in kinds
    assert ("in", "1", "tx") in kinds
    assert ("out", "2", "rx_begin") in kinds
    assert ("out", "2", "rx_end") in kinds


# ---------------------------------------------------------------------------
# Virtual time: the ether as the conductor
# ---------------------------------------------------------------------------

def idle(station, seq, until):
    station.send({"type": "idle", "seq": seq, "until": until})


def join_virtual(station):
    welcome = station.hello()
    assert welcome["mode"] == "virtual"
    assert welcome["rate"] is None
    return welcome


def test_virtual_welcome_states_the_mode_and_t(conductor):
    welcome = join_virtual(conductor(1))
    assert welcome["t"] == 0
    assert welcome["seq"] == 1
    assert isinstance(welcome["epoch"], int)


def test_the_barrier_holds_until_every_station_is_idle(conductor):
    a, b = conductor(1), conductor(2)
    join_virtual(a)
    join_virtual(b)
    idle(a, 1, 10_000)
    a.expect_nothing(0.3)                       # b has not said it is idle
    idle(b, 1, 20_000)
    run = a.expect("run")
    assert (run["t"], run["seq"]) == (10_000, 2)
    b.expect_nothing(0.2)                       # b has nothing before 20 000
    idle(a, 2, 20_000)
    assert a.expect("run")["t"] == 20_000
    assert b.expect("run")["t"] == 20_000


def test_a_stale_idle_does_not_count(conductor):
    a, b = conductor(1), conductor(2)
    join_virtual(a)
    join_virtual(b)
    idle(b, 1, None)
    idle(a, 1, 5_000)
    assert a.expect("run")["seq"] == 2
    # An answer to the last grant, not this one: it does not count. Once is
    # an idle crossing the run on the wire; said again, the station missed
    # what came after it, which is sent again as it was.
    idle(a, 1, 6_000)
    a.expect_nothing(0.3)
    idle(a, 1, 6_000)
    assert a.expect("run") == {"type": "run", "seq": 2, "t": 5_000}
    idle(a, 2, 6_000)
    assert a.expect("run")["t"] == 6_000


def test_an_idle_said_twice_counts_once(conductor):
    a, b = conductor(1), conductor(2)
    join_virtual(a)
    join_virtual(b)
    idle(b, 1, None)
    idle(a, 1, 5_000)
    assert a.expect("run")["seq"] == 2
    idle(a, 2, 6_000)
    run = a.expect("run")
    assert run == {"type": "run", "seq": 3, "t": 6_000}
    # Said once more after the run went out: that run crossed it, nothing
    # is sent; said yet again, the run was lost, and goes once more, and
    # never a new grant.
    idle(a, 2, 6_000)
    a.expect_nothing(0.2)
    idle(a, 2, 6_000)
    assert a.expect("run") == run
    idle(a, 2, 6_000)                           # within the resend gap: nothing
    a.expect_nothing(0.3)


def test_anything_a_station_says_retracts_its_idle(conductor):
    a, b = conductor(1), conductor(2)
    join_virtual(a)
    join_virtual(b)
    idle(b, 1, None)
    a.state("RX")
    idle(a, 1, 7_000)
    assert a.expect("run")["t"] == 7_000
    a.state("RX")                               # before it has answered the run
    idle(a, 2, 9_000)
    assert a.expect("run")["t"] == 9_000


def test_rx_begin_and_rx_end_arrive_at_their_instants_in_t(conductor):
    conductor.link(1, 2, NEAR_DB)
    tx, rx = conductor(1), conductor(2)
    join_virtual(tx)
    join_virtual(rx)
    rx.state("RX")
    idle(rx, 1, None)
    idle(tx, 1, 50_000)
    assert tx.expect("run")["t"] == 50_000
    tx.send(dict({"type": "tx", "slot": 0, "id": 1, "t0": 50_000,
                  "t_pre": 50_000 + FRAME_US // 10, "t_hdr": 50_000 + FRAME_US // 5,
                  "t_end": 50_000 + FRAME_US, "power_dbm": POWER_DBM,
                  "payload": base64.b64encode(b"on time").decode()}, **radio("TX")))
    idle(tx, 2, None)
    begin = rx.expect("rx_begin")
    assert (begin["t"], begin["t0"], begin["t_end"]) == (50_000, 50_000, 50_000 + FRAME_US)
    assert begin["t_pre"] == 50_000 + FRAME_US // 10
    rx.expect_nothing(0.2)                      # T waits on rx, which has not answered
    idle(rx, begin["seq"], begin["t_pre"])
    assert rx.expect("run")["t"] == begin["t_pre"]
    idle(rx, begin["seq"] + 1, None)
    end = rx.expect("rx_end")
    assert end["t"] == 50_000 + FRAME_US
    assert base64.b64decode(end["payload"]) == b"on time"
    lines = [l for l in conductor.record.read_text().splitlines() if not l.startswith("#")]
    stamps = {json.loads(l.split("\t")[3])["type"]: l.split("\t")[0] for l in lines}
    assert stamps["rx_end"] == "%.6f" % ((50_000 + FRAME_US) / 1e6)
    assert "idle" not in stamps and "run" not in stamps


def test_a_late_tx_is_clamped_to_t(conductor):
    conductor.link(1, 2, NEAR_DB)
    tx, rx = conductor(1), conductor(2)
    join_virtual(tx)
    join_virtual(rx)
    rx.state("RX")
    idle(rx, 1, None)
    idle(tx, 1, 80_000)
    tx.expect("run")
    idle(tx, 2, None)
    time.sleep(0.1)
    # Something from outside the run moves the station after it said it was
    # idle; the frame it stamps with the T it last had goes on the air at T.
    tx.send(dict({"type": "tx", "slot": 0, "id": 1, "t0": 30_000, "t_pre": 31_000,
                  "t_hdr": 32_000, "t_end": 40_000, "power_dbm": POWER_DBM,
                  "payload": base64.b64encode(b"late").decode()}, **radio("TX")))
    idle(tx, 2, None)
    begin = rx.expect("rx_begin")
    assert (begin["t0"], begin["t_pre"], begin["t_end"]) == (80_000, 81_000, 90_000)


def test_one_instant_is_ruled_in_station_order(conductor):
    conductor.link(1, 3, NEAR_DB)
    conductor.link(2, 3, NEAR_DB)
    early, late, rx = conductor(1), conductor(2), conductor(3)
    for st in (early, late, rx):
        join_virtual(st)
    rx.state("RX")
    idle(rx, 1, None)
    idle(early, 1, None)
    idle(late, 1, None)

    def send_tx(st, payload):
        st.send(dict({"type": "tx", "slot": 0, "id": 1, "t0": 0,
                      "t_pre": FRAME_US // 10, "t_hdr": FRAME_US // 5,
                      "t_end": FRAME_US, "power_dbm": POWER_DBM,
                      "payload": base64.b64encode(payload).decode()}, **radio("TX")))

    # Station 2 speaks first on the wire; station 1's frame still gets the
    # lower number, and the receiver, because one instant is taken in station
    # order.
    send_tx(late, b"two")
    time.sleep(0.1)
    send_tx(early, b"one")
    idle(late, 1, None)
    idle(early, 1, None)
    first = rx.expect("rx_begin")
    idle(rx, first["seq"], None)
    second = rx.expect("rx_begin")
    assert first["id"] < second["id"]
    assert "cad" not in first and second["cad"] is True
    idle(rx, second["seq"], None)
    ends = {}
    for _ in range(2):
        end = rx.expect("rx_end")
        ends[end["id"]] = end
        idle(rx, end["seq"], None)
    assert ends[first["id"]]["verdict"] == ends[second["id"]]["verdict"] == "crc"
    lines = [l for l in conductor.record.read_text().splitlines() if not l.startswith("#")]
    order = [json.loads(l.split("\t")[3]) for l in lines if "\tout\t3\t" in l]
    begins = [m for m in order if m["type"] == "rx_begin"]
    assert [m["id"] for m in begins] == sorted(m["id"] for m in begins)


def test_a_message_the_barrier_cannot_take_drops_only_itself(conductor):
    conductor.link(1, 3, NEAR_DB)
    conductor.link(2, 3, NEAR_DB)
    bad, good, rx = conductor(1), conductor(2), conductor(3)
    for st in (bad, good, rx):
        join_virtual(st)
    rx.state("RX")
    for st in (rx, bad, good):
        idle(st, 1, None)
    # Station 1's frame states a start that is no number. It is taken first,
    # in station order, and must not take station 2's at the same T with it.
    for st, t0, payload in ((bad, "soon", b"bad"), (good, 0, b"good")):
        st.send(dict({"type": "tx", "slot": 0, "id": 1, "t0": t0,
                      "t_pre": FRAME_US // 10, "t_hdr": FRAME_US // 5,
                      "t_end": FRAME_US, "power_dbm": POWER_DBM,
                      "payload": base64.b64encode(payload).decode()}, **radio("TX")))
    idle(bad, 1, None)
    idle(good, 1, None)
    begin = rx.expect("rx_begin")
    assert "cad" not in begin
    idle(rx, begin["seq"], None)
    end = rx.expect("rx_end")
    assert end["id"] == begin["id"] and base64.b64decode(end["payload"]) == b"good"
    assert end["verdict"] == "clean"


def test_a_station_that_leaves_restarts_with_a_hello(conductor):
    a, b = conductor(1), conductor(2)
    join_virtual(a)
    join_virtual(b)
    idle(a, 1, 10_000)
    idle(b, 1, 10_000)
    assert a.expect("run")["t"] == 10_000
    b.expect("run")
    # b restarts: its hello replaces everything the ether knew of it.
    welcome = b.hello()
    assert (welcome["t"], welcome["seq"]) == (10_000, 1)
    idle(a, 2, 20_000)
    a.expect_nothing(0.2)
    idle(b, 1, 30_000)
    assert a.expect("run")["t"] == 20_000


def test_paced_time_follows_the_wall(tmp_path):
    bed = Bench(tmp_path, "10x")
    try:
        a = bed(1)
        a.hello()
        idle(a, 1, 500_000)                     # half a second of T: 50 ms of wall at 10x
        start = time.monotonic()
        a.expect("run")
        idle(a, 2, 1_500_000)                   # a whole second of T: 100 ms
        a.expect("run")
        took = time.monotonic() - start
        assert 0.08 <= took <= 0.4
    finally:
        bed.close()


# ---------------------------------------------------------------------------
# Virtual time: input that does not come over the air
# ---------------------------------------------------------------------------

def at_address(station, host):
    """Move a fake station's socket to its own loopback address, as a
    station of a run has, so the ends of a TCP connection name it."""
    station.sock.close()
    station.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    station.sock.bind((host, 0))
    return station


class Asker:
    """A station thread's own socket for asking to write over TCP."""

    def __init__(self, station):
        self.station = station
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.req = 0

    def ask(self, ch, n):
        self.req += 1
        self.sock.sendto(json.dumps({"type": "wrote", "sid": self.station.sid, "ch": ch,
                                     "n": n, "go": self.req}).encode(),
                         self.station.ether)
        return self.req

    def go(self, timeout=2.0):
        self.sock.settimeout(timeout)
        try:
            return json.loads(self.sock.recvfrom(1024)[0])["go"]
        except socket.timeout:
            return None

    def close(self):
        self.sock.close()


A_TO_B = "tcp/127.0.0.11:40000>127.0.0.12:4965"
B_TO_A = "tcp/127.0.0.12:4965>127.0.0.11:40000"


def test_a_tcp_write_waits_for_its_reader_and_holds_t_until_it_is_read(conductor):
    a = at_address(conductor(1), "127.0.0.11")
    b = at_address(conductor(2), "127.0.0.12")
    join_virtual(a)
    join_virtual(b)
    idle(b, 1, None)
    idle(a, 1, 10_000)
    assert a.expect("run")["t"] == 10_000
    asker = Asker(a)
    try:
        req = asker.ask(A_TO_B, 10)
        # b was last told T 0: it is told this T before a may write.
        run = b.expect("run")
        assert run["t"] == 10_000
        assert asker.go(0.3) is None
        idle(b, run["seq"], None)
        assert asker.go() == req
        # The bytes are on their way and b has not read them: T stays.
        idle(a, 2, 20_000)
        a.expect_nothing(0.3)
        b.send({"type": "read", "ch": A_TO_B, "n": 10})
        # b is at work on them, at the T it has.
        run = b.expect("run")
        assert run["t"] == 10_000
        idle(b, run["seq"], None)
        assert a.expect("run")["t"] == 20_000
    finally:
        asker.close()


def test_a_listener_owns_its_endpoint_on_an_address_two_stations_share(conductor):
    """Two processes of one station, each a station of the run on one
    address: a connection to the one that listens is its, not the other's,
    before it has read anything from it."""
    a = at_address(conductor(1), "127.0.0.11")
    b = at_address(conductor(2), "127.0.0.11")
    join_virtual(a)
    join_virtual(b)
    b.send({"type": "listen", "at": "127.0.0.11:7633"})
    idle(b, 1, None)
    idle(a, 1, 10_000)
    assert a.expect("run")["t"] == 10_000
    asker = Asker(a)
    try:
        req = asker.ask("tcp/127.0.0.11:40000>127.0.0.11:7633", 4)
        # b, the reader, is told this T before a may write.
        run = b.expect("run")
        assert run["t"] == 10_000
        idle(b, run["seq"], None)
        assert asker.go() == req
    finally:
        asker.close()


def test_two_stations_writing_to_each_other_go_in_station_order(conductor):
    a = at_address(conductor(1), "127.0.0.11")
    b = at_address(conductor(2), "127.0.0.12")
    join_virtual(a)
    join_virtual(b)
    idle(a, 1, 10_000)
    idle(b, 1, 10_000)
    a.expect("run")
    b.expect("run")
    ask_a, ask_b = Asker(a), Asker(b)
    try:
        req_b = ask_b.ask(B_TO_A, 5)            # b asks first, while a is still at work
        assert ask_b.go(0.3) is None
        req_a = ask_a.ask(A_TO_B, 7)
        assert ask_a.go() == req_a              # the lower station id goes first
        assert ask_b.go(0.3) is None            # and b waits until a is done
        idle(a, 2, None)
        assert ask_b.go() == req_b
    finally:
        ask_a.close()
        ask_b.close()


def test_a_write_to_something_outside_the_run_is_let_go_at_once(conductor):
    a = at_address(conductor(1), "127.0.0.11")
    join_virtual(a)
    asker = Asker(a)
    try:
        req = asker.ask("tcp/127.0.0.11:40000>127.0.0.99:80", 3)
        assert asker.go() == req
    finally:
        asker.close()


class InProcess:
    """An ether in this process's loop, in virtual time, with one station
    on a UDP socket, so its Python side can be driven directly."""

    def __init__(self, loop):
        self.loop = loop
        self.ether = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)

    async def start(self):
        _, self.ether = await ether_module.open_ether(("127.0.0.1", 0), None, time_mode="max")
        self.addr = self.ether.transport.get_extra_info("sockname")

    def send(self, msg):
        self.sock.sendto(json.dumps(dict(msg, sid=1)).encode(), self.addr)

    async def recv(self, timeout=1.0):
        raw = await self.recv_raw(timeout)
        return None if raw is None else json.loads(raw)

    async def recv_raw(self, timeout=1.0):
        end = self.loop.time() + timeout
        while self.loop.time() < end:
            try:
                return self.sock.recv(65535)
            except BlockingIOError:
                await asyncio.sleep(0.005)
        return None

    async def join(self):
        self.send({"type": "hello", "slots": [0], "t": 0})
        welcome = await self.recv()
        self.send({"type": "idle", "seq": welcome["seq"], "until": None})
        await asyncio.sleep(0.05)

    def close(self):
        self.sock.close()
        self.ether.close()


def in_process(test):
    """Run `test` on an in-process ether in virtual time."""
    need_core()
    loop = asyncio.new_event_loop()
    try:
        bed = InProcess(loop)
        loop.run_until_complete(bed.start())
        assert isinstance(bed.ether, ether_module.CoreEther)
        try:
            loop.run_until_complete(test(bed))
        finally:
            bed.close()
    finally:
        loop.close()


def test_a_station_that_takes_lines_is_told_an_instant_in_one_datagram():
    """What the barrier tells a station at one instant in one go (here two
    things that fell due there) goes as one datagram, a line each, to a
    station that said `lines` in its hello: it applies them as one, so none
    of its threads sees part of the instant. One that did not say so gets
    one a datagram. Its run comes, as ever, once it has said it is idle."""
    def check(lines):
        async def test(bed):
            ether = bed.ether
            bed.send({"type": "hello", "slots": [0], "t": 0, **({"lines": 1} if lines else {})})
            welcome = await bed.recv()
            for n in (1, 2):
                ether.call_at(1_000_000, lambda n=n: ether.send(1, {"type": "note", "n": n}))
            bed.send({"type": "idle", "seq": welcome["seq"], "until": 1_000_000})
            got = [await bed.recv_raw()]
            if not lines:
                got.append(await bed.recv_raw())
            assert await bed.recv_raw(0.1) is None
            said = [json.loads(line) for raw in got for line in raw.split(b"\n")]
            assert [(m["type"], m["n"]) for m in said] == [("note", 1), ("note", 2)]
            assert [m["seq"] for m in said] == [welcome["seq"] + 1, welcome["seq"] + 2]
            assert all(m["t"] == 1_000_000 for m in said)
            bed.send({"type": "idle", "seq": said[-1]["seq"], "until": 1_000_000})
            run = await bed.recv()
            assert run["type"] == "run" and run["t"] == 1_000_000
        in_process(test)
    check(lines=True)
    check(lines=False)


def test_t_stays_at_a_sleeps_end_until_what_it_woke_has_run():
    async def test(bed):
        ether = bed.ether
        await bed.join()
        # Something else would move T on at once: a heap entry at 2 s.
        ether.call_at(2_000_000, lambda: None)
        seen = []

        async def after():
            await ether.sleep(1.0)
            await asyncio.sleep(0)              # a turn of the loop later
            seen.append(ether.now())
            ether.expect(9)                     # a station starting at this T

        await asyncio.wait_for(after(), 2)
        await asyncio.sleep(0.05)
        assert seen == [1_000_000]
        assert ether.now() == 1_000_000         # waiting for station 9

    in_process(test)


def test_a_line_typed_at_a_station_waits_for_it_to_have_t_and_holds_t_until_read():
    async def test(bed):
        ether = bed.ether
        await bed.join()
        await ether.sleep(1.0)
        await asyncio.sleep(0.05)
        synced = []
        ether.typed(1, 12)
        ether.sync(1, lambda: synced.append(ether.now()))
        run = await bed.recv()
        assert run["type"] == "run" and run["t"] == 1_000_000
        assert synced == []
        bed.send({"type": "idle", "seq": run["seq"], "until": None})
        await asyncio.sleep(0.05)
        assert synced == [1_000_000]
        assert ether.busy()                     # twelve bytes it has not read
        bed.send({"type": "read", "ch": "tty", "total": 12})
        run = await bed.recv()
        assert run["type"] == "run" and run["t"] == 1_000_000
        bed.send({"type": "idle", "seq": run["seq"], "until": None})
        await asyncio.sleep(0.05)
        assert not ether.busy()

    in_process(test)


def test_t_stands_at_a_hello_until_what_waited_for_it_has_run():
    """joined(): done at the expected station's hello, with T held there until
    what it woke has run; nothing waiting, nothing held."""
    async def test(bed):
        ether = bed.ether
        ether.expect(1)
        ether.call_at(1_000_000, lambda: None)
        seen = []

        async def waiter():
            await ether.joined(1)
            await asyncio.sleep(0)              # a turn of the loop later
            seen.append(ether.now())
            ether.tool_session(1)               # T stays for this from here

        task = asyncio.ensure_future(waiter())
        await asyncio.sleep(0.01)
        bed.send({"type": "hello", "slots": [0], "t": 0})
        welcome = await bed.recv()
        bed.send({"type": "idle", "seq": welcome["seq"], "until": None})
        await asyncio.sleep(0.05)
        await task
        assert seen == [0]
        assert ether.now() == 0                 # held: the session has the floor
        assert ether.joined(1).done()

    in_process(test)


def test_what_stations_say_at_one_instant_is_recorded_in_station_order(conductor):
    """A virtual run's state and tx are taken at the barrier in station
    order, and recorded then: the record does not depend on which of two
    stations the host ran first."""
    one, two = conductor(1), conductor(2)
    seqs = {}
    for station in (one, two):
        seqs[station.sid] = station.hello()["seq"]
        station.send({"type": "idle", "seq": seqs[station.sid], "until": None})
    time.sleep(0.1)
    two.state("RX")
    one.state("RX")
    time.sleep(0.1)
    for station in (two, one):
        station.send({"type": "idle", "seq": seqs[station.sid], "until": None})
    time.sleep(0.2)
    conductor.close()
    lines = [line.split("\t") for line in conductor.record.read_text().splitlines()
             if not line.startswith("#")]
    states = [int(sid) for _, direction, sid, text in lines
              if direction == "in" and '"type":"state"' in text]
    assert states == [1, 2]


def test_a_tool_session_holds_t_while_the_tool_has_the_floor():
    """tool_session(): T stands from the start until the station says it has
    read what the tool wrote (`floor` to the station), runs while the
    station works, stands again once it has answered (`floor` to the tool),
    and goes when the session ends. A floor outside a session holds nothing."""
    async def test(bed):
        ether = bed.ether
        await bed.join()
        ether.call_at(1_000_000, lambda: None)
        end = ether.tool_session(1)
        ether.kick()
        await asyncio.sleep(0.05)
        assert ether.now() == 0                 # the tool has the floor
        bed.send({"type": "floor", "to": "station"})
        brought = await bed.recv()              # brought to the run's T first
        assert (brought["type"], brought["t"], brought["floor"]) == ("run", 0, 1)
        await asyncio.sleep(0.05)
        assert ether.now() == 0                 # it owes an idle for that
        bed.send({"type": "idle", "seq": brought["seq"], "until": 2_000_000})
        await asyncio.sleep(0.05)
        run = await bed.recv()                  # the station's floor: T runs, to its wake
        assert (run["type"], run["t"]) == ("run", 2_000_000)
        assert ether.now() == 2_000_000
        bed.send({"type": "floor", "to": "tool"})
        await asyncio.sleep(0.05)
        assert ether.holds == 1
        end()
        await asyncio.sleep(0.05)
        assert ether.holds == 0 and 1 not in ether.floors
        # No session: a floor is only the station speaking.
        bed.send({"type": "floor", "to": "tool"})
        await asyncio.sleep(0.05)
        assert ether.holds == 0

    in_process(test)


def test_t_does_not_wait_on_a_drain_that_found_nothing_printed():
    """A drain that says False has read nothing and set nothing going, and
    is not waited for: T moves on at once, with no turn of the loop taken."""
    async def test(bed):
        ether = bed.ether
        asked = []

        def on_drain(sids, done):
            asked.append((sids, ether.now()))
            return False
        ether.on_drain = on_drain
        await bed.join()
        ether.call_at(3_000_000, lambda: None)
        ether.kick()
        assert asked == [([1], 0)]
        assert ether.now() == 3_000_000         # in the same kick, no hold left
        assert ether.holds == 0

    in_process(test)


def test_a_watched_console_holds_t_only_once_it_has_printed():
    """With drains_watched, on_drain answers exactly whether a watched
    console shows something: T goes on at once past a station that printed
    nothing, and waits for the read of one that did. The core looks at the
    console itself and asks only then."""
    async def test(bed):
        ether = bed.ether
        r, w = os.pipe()
        os.set_blocking(r, False)
        marks = ether.console_marks(r)
        if marks is None:
            marks = types.SimpleNamespace(master=r, reading=False, taking=False,
                                          taken=0, caught=0)
        marks.reading = True
        asked = []

        def on_drain(sids, done):
            asked.append((sids, ether.now()))
            if not select.select([r], [], [], 0)[0]:
                return False
            os.read(r, 100)
            bed.loop.call_later(0.05, done)
            return True
        ether.watch(1, marks)
        ether.on_drain = on_drain
        ether.drains_watched = True
        ether.call_at(1_000_000, lambda: None)
        try:
            bed.send({"type": "hello", "slots": [0], "t": 0})
            welcome = await bed.recv()
            bed.send({"type": "idle", "seq": welcome["seq"], "until": 500_000})
            run = await bed.recv()                  # nothing printed: T went on
            assert (run["type"], run["t"]) == ("run", 500_000)
            os.write(w, b"hello\n")                 # it prints, and is idle
            bed.send({"type": "idle", "seq": run["seq"], "until": None})
            await asyncio.sleep(0.02)
            assert ether.now() == 500_000           # held for the read
            assert asked[-1] == ([1], 500_000)
            await asyncio.sleep(0.1)
            assert ether.now() == 1_000_000         # read, and T went on
            assert asked == [([1], 500_000)]        # never asked about nothing
        finally:
            ether.unwatch(1, marks)
            os.close(r)
            os.close(w)

    in_process(test)


def test_a_burst_the_kernel_could_not_hold_is_heard_whole_by_the_core():
    """The core's reader thread takes datagrams off the socket as they come,
    whatever the loop's thread is doing: a burst far past the kernel's receive
    buffer, sent while the loop is busy, arrives whole. The kernel dropping
    one would be a station's `state` or `tx` the medium never heard."""
    async def test(bed):
        ether = bed.ether
        socks = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(4)]
        try:
            for s in socks:
                s.bind(("127.0.0.1", 0))
            # 1500 hellos, the loop blocked the while: past 200 KB of buffer.
            for sid in range(2, 1502):
                hello = {"type": "hello", "sid": sid, "slots": [0], "t": 0}
                socks[sid % 4].sendto(json.dumps(hello).encode(), bed.addr)
            time.sleep(0.2)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(ether.stations) == 1500:
                    break
            assert len(ether.stations) == 1500
            assert ether.core.drops == 0
        finally:
            for s in socks:
                s.close()

    in_process(test)


def test_datagrams_that_come_while_the_core_is_handling_others_are_all_heard():
    """Datagrams keep coming from another thread while the loop handles the
    ones before them: every one is heard, none left queued with nothing to
    wake the loop for it."""
    async def test(bed):
        ether = bed.ether

        def send_all():
            socks = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(4)]
            try:
                for sid in range(2, 3002):
                    hello = {"type": "hello", "sid": sid, "slots": [0], "t": 0}
                    socks[sid % 4].sendto(json.dumps(hello).encode(), bed.addr)
                    if sid % 50 == 0:
                        time.sleep(0.001)
            finally:
                for s in socks:
                    s.close()
        sender = threading.Thread(target=send_all)
        sender.start()
        for _ in range(600):
            await asyncio.sleep(0.01)
            if len(ether.stations) == 3000 and not sender.is_alive():
                break
        sender.join()
        assert len(ether.stations) == 3000

    in_process(test)


def test_the_core_counts_what_the_kernel_dropped_on_its_socket():
    """Datagrams the kernel could not hold before anyone read them are
    counted, from what it says with the next one (SO_RXQ_OVFL), and simd
    reports them as what may void the run."""
    async def test():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.bind(("127.0.0.1", 0))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in range(500):            # far past a 4 KB buffer, nobody reading
                sender.sendto(b'{"type": "hello"}', sock.getsockname())
            ether = ether_module.CoreEther(sock, None, time_mode="max",
                                           module=ether_module.core_module())
            try:
                # The kernel stamps its count on a datagram as it queues it,
                # from when the core has asked for it: the next one says.
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    sender.sendto(b'{"type": "hello"}', sock.getsockname())
                    if ether.core.drops:
                        break
                assert ether.core.drops > 0
            finally:
                ether.close()
        finally:
            sender.close()

    need_core()
    asyncio.run(test())


def test_what_stations_printed_is_read_before_t_moves():
    async def test(bed):
        ether = bed.ether
        drains = []
        ether.on_drain = lambda sids, done: drains.append((sids, done, ether.now()))
        await bed.join()
        ether.call_at(3_000_000, lambda: None)
        ether.kick()
        await asyncio.sleep(0.05)
        assert [(sids, t) for sids, _, t in drains] == [([1], 0)]
        assert ether.now() == 0                 # nothing moves until it is read
        drains[0][1]()
        await asyncio.sleep(0.05)
        assert ether.now() == 3_000_000

    in_process(test)


def test_an_idle_from_another_address_is_not_the_stations(conductor):
    """A station speaks from the address it said hello from. Another run's
    station, its ether gone and this port taken by a new one, still writing
    idles under the same id is not taken for it: T does not move for it."""
    a, b = conductor(1), conductor(2)
    join_virtual(a)
    join_virtual(b)
    idle(b, 1, None)
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        other.bind(("127.0.0.1", 0))
        other.sendto(json.dumps({"type": "idle", "sid": 1, "seq": 1,
                                 "until": 5_000}).encode(), a.ether)
        a.expect_nothing(0.3)
    finally:
        other.close()
    idle(a, 1, 5_000)
    assert a.expect("run")["seq"] == 2
