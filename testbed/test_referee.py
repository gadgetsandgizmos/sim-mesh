"""The referee against records written by hand, the way the ether writes them,
and against one the ether itself wrote, in-process and in virtual time; on the
tests' four stations (testdata/four.yaml) on plain-27. At SF8, n01 and n02
hear each other, as do n01 and n04, and n02 and n04; n01 and n03 do not, nor
do n03 and n04. No stations."""

import asyncio
import collections
import datetime
import json
import os
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ether"))

import ether as ether_module  # noqa: E402
import geodata  # noqa: E402
import losses  # noqa: E402
import nodeset  # noqa: E402
import referee  # noqa: E402
import runs  # noqa: E402
from sim_mesh.view import RunView  # noqa: E402

CALLING = 869_525_000
GLOBALS = "FREQ_MHZ = 869.525\nSF = 8\nBW_KHZ = 125\nCR = 5\n"
FOUR = os.path.join(HERE, "testdata", "four.yaml")
PLAIN = os.path.join(HERE, "testdata", "plain-27.yaml")
# A real-time run: its stamps are the wall clock, here across a midnight;
# the ether's rx messages carry its event loop's clock, and each station's
# `tx` its own.
MIDNIGHT = datetime.datetime(2026, 9, 29, 23, 59, 59, tzinfo=datetime.timezone.utc)
ETHER_CLOCK_US = 7_000_000_000

Sent = collections.namedtuple("Sent", "eid us span_us sf")


class Record:
    """A record written line by line the way the ether writes one: stamped
    with T (a virtual-time run) or the wall clock (`wall`), in the order of
    the stamps, and the order written at one stamp."""

    def __init__(self, wall=False):
        self.wall = wall
        self.lines = []
        self.eid = 0

    def ether_us(self, us):
        return us + (ETHER_CLOCK_US if self.wall else 0)

    def add(self, us, direction, sid, msg):
        stamp = ((MIDNIGHT + datetime.timedelta(microseconds=us)).isoformat(
            timespec="microseconds") if self.wall else "%.6f" % (us / 1e6))
        self.lines.append((us, len(self.lines), "%s\t%s\t%d\t%s" % (
            stamp, direction, sid, json.dumps(msg, separators=(",", ":"), sort_keys=True))))

    def state(self, t, sid, mode, sf=8):
        self.add(round(t * 1e6), "in", sid, {"type": "state", "sid": sid, "slot": 0, "mode": mode,
                                             "freq": CALLING, "bw": 125000, "sf": sf, "sync": 18})

    def tx(self, t, sid, span, freq=CALLING, sf=8, sync=18, slot=0, eid=None):
        """A transmission, and the number the ether gives it: the next one,
        unless a virtual run's barrier took it in another order."""
        us, span_us = round(t * 1e6), round(span * 1e6)
        own = us + (1_000_000 * sid if self.wall else 0)
        sym_us = (1 << sf) * 1e6 / 125_000
        self.add(us, "in", sid, {
            "type": "tx", "sid": sid, "slot": slot, "id": 1, "freq": freq, "bw": 125000, "sf": sf,
            "sync": sync, "power_dbm": 14, "payload": "AAAA", "t0": own,
            "t_pre": own + round(12.25 * sym_us), "t_hdr": own + round(20.25 * sym_us),
            "t_end": own + span_us})
        if eid is None:
            self.eid += 1
            eid = self.eid
        return Sent(eid, us, span_us, sf)

    def begin(self, t, rsid, sent, level, cad=False):
        """The ether telling a slot of a frame: at its start, or when the slot
        came to RX or CAD mid-frame, with the T it was told as its t0."""
        start, sym_us = self.ether_us(sent.us), (1 << sent.sf) * 1e6 / 125_000
        msg = {"type": "rx_begin", "slot": 0, "id": sent.eid, "t0": self.ether_us(round(t * 1e6)),
               "t_pre": start + round(12.25 * sym_us), "t_hdr": start + round(20.25 * sym_us),
               "t_end": start + sent.span_us, "level": level}
        if cad:
            msg["cad"] = True
        self.add(round(t * 1e6), "out", rsid, msg)

    def end(self, rsid, sent, verdict, level):
        us = sent.us + sent.span_us
        self.add(us, "out", rsid, {"type": "rx_end", "slot": 0, "id": sent.eid,
                                   "t": self.ether_us(us), "verdict": verdict, "payload": "AAAA",
                                   "rssi": level, "snr": level + 117})

    def write(self, run):
        with open(os.path.join(run.dir, "record.tsv"), "w", encoding="utf-8") as handle:
            handle.write("# 2026-09-29T00:00:00+00:00\tether record: stamp\tdir\tsid\tjson\n")
            handle.write("".join(text + "\n" for _, _, text in sorted(self.lines)))


@pytest.fixture
def run(tmp_path):
    gd, ns = geodata.read(PLAIN, "plain-27"), nodeset.open_path(FOUR, "four")
    table = tmp_path / "868.bin"
    losses.synthetic_table(gd, ns, "868").write(str(table))
    made = runs.create_run(str(tmp_path / "run"), gd, ns, None, "max", {"868": str(table)})
    with open(os.path.join(made.dir, runs.GLOBALS_FILE), "w", encoding="utf-8") as handle:
        handle.write(GLOBALS)
    return made


def judged(run, capsys):
    assert referee.main(["--run", run.dir, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def told_over(run, capsys):
    """n01 sends half a second, n02 is told at its start and ends it clean,
    and sends into its payload anyway; n03 sends on another channel, out of
    everyone's reach, and nobody is told."""
    rec = Record()
    one = rec.tx(1.0, 1, 0.5)
    rec.begin(1.0, 2, one, -110)
    rec.end(2, one, "clean", -110)
    rec.tx(1.2, 2, 0.3)
    rec.tx(5.0, 3, 0.3, freq=869_475_000, sf=7)
    rec.write(run)
    return judged(run, capsys)


def test_the_referee_names_a_sender_that_talked_over_a_frame_it_was_told_of(run, capsys):
    report = told_over(run, capsys)
    events = report["carrier_sense"]
    assert len(events) == 1
    event = events[0]
    assert (event["sid"], event["over"], event["told"], event["told_late"]) == (2, 1, True, False)
    assert event["lock"] and event["window"] == "payload"
    assert event["on_air_ms"] == 200.0 and event["level_dbm"] == pytest.approx(-110.4, abs=0.1)
    assert report["carrier_sense_counts"]["told"]["payload"] == 1 and report["untied"] == 0
    assert referee.main(["--run", run.dir, "--detail"]) == 0
    lines = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
    assert "told 0 0 1" in lines and "n02 1 over a frame in its payload told" in lines
    assert "1.200000 s n02 over n01's frame, 200.0 ms into it, at -110.4 dBm: told (a lock); " \
           "the slot had stated no mode yet" in lines


def test_a_sender_that_began_listening_mid_frame_is_told_late_not_never(run, capsys):
    rec = Record()
    rec.state(0.5, 2, "STDBY_RC")
    rec.state(0.5, 4, "CAD")
    one = rec.tx(1.0, 1, 0.5)
    rec.begin(1.0, 4, one, -121, cad=True)      # a CAD at the frame's start: energy
    # n02 comes back to RX 100 ms in and is told the frame's energy then, and
    # sends 100 ms after that; n04 sends having sensed it from the start
    rec.state(1.1, 2, "RX")
    rec.begin(1.1, 2, one, -110, cad=True)
    rec.tx(1.2, 2, 0.3)
    rec.tx(1.3, 4, 0.1)
    rec.write(run)
    events = {(e["sid"], e["over"]): e for e in judged(run, capsys)["carrier_sense"]}
    late = events[(2, 1)]
    assert (late["told"], late["told_late"], late["lock"]) == (False, True, False)
    assert late["window"] == "payload" and late["mode"] == "STDBY_RC"
    sensed = events[(4, 1)]
    assert (sensed["told"], sensed["told_late"], sensed["lock"]) == (True, False, False)
    assert sensed["mode"] == "CAD"


def test_the_blind_window_is_four_of_the_frames_own_symbols(run, capsys):
    rec = Record()
    # 6 ms into an SF8 frame is under four of its symbols (8.2 ms): blind;
    # 6 ms into an SF7 one is past its four (4.1 ms), in its preamble
    rec.tx(1.0, 1, 0.5, sf=8)
    rec.tx(1.006, 2, 0.3, sf=8)
    rec.tx(3.0, 1, 0.5, sf=7)
    rec.tx(3.006, 2, 0.3, sf=7)
    rec.write(run)
    events = judged(run, capsys)["carrier_sense"]
    assert [(e["on_air_ms"], e["window"]) for e in events] == [(6.0, "blind"), (6.0, "preamble")]
    assert not any(e["told"] or e["told_late"] for e in events)


def test_the_referee_lists_frames_nobody_was_told_of_by_where_they_were_sent(run, capsys):
    report = told_over(run, capsys)
    unheard = {(u["sid"], u["freq"], u["sf"]): u["frames"] for u in report["unheard"]}
    assert unheard == {(2, CALLING, 8): 1, (3, 869_475_000, 7): 1}


def test_a_spoiled_reception_is_split_by_whether_the_two_senders_could_hear_each_other(run):
    rec = Record()
    # n03's frame at n02, taken off it by n01's, which n03 cannot hear
    three = rec.tx(10.0, 3, 0.4)
    rec.begin(10.0, 2, three, -127)
    one = rec.tx(10.1, 1, 0.3)
    rec.begin(10.1, 2, one, -110)
    rec.end(2, three, "crc", -127)
    rec.end(2, one, "clean", -110)
    # n04's frame at n02, taken off it by n01's, which n04 can hear
    four = rec.tx(20.0, 4, 0.4)
    rec.begin(20.0, 2, four, -123)
    rec.begin(20.0, 1, four, -121)
    one = rec.tx(20.05, 1, 0.3)
    rec.begin(20.05, 2, one, -110)
    rec.end(2, one, "clean", -110)
    rec.end(2, four, "crc", -123)
    # n02 sends while it receives n01: half duplex, not a collision
    one = rec.tx(30.0, 1, 0.4)
    rec.begin(30.0, 2, one, -110)
    rec.tx(30.1, 2, 0.2)
    rec.end(2, one, "crc", -110)
    # n04 sends from a second slot over its own frame: neither
    four = rec.tx(40.0, 4, 0.3)
    rec.begin(40.0, 2, four, -123)
    rec.tx(40.1, 4, 0.1, slot=1)
    rec.end(2, four, "crc", -123)
    rec.write(run)
    col = referee.analyse(run.dir)["collisions"]
    assert col["spoiled"] == 4 and col["by_receiver"] == {2: 4}
    assert col["overlaps"] == {"hidden": 1, "in earshot": 1, "same": 1}
    assert col["receiver_sending"] == 1 and col["no_overlap"] == 0
    got = [(r["sid"], r["receiver_sending"], r["overlaps"]) for r in col["receptions"]]
    assert got == [(3, False, [{"sid": 1, "senders": "hidden"}]),
                   (4, False, [{"sid": 1, "senders": "in earshot"}]),
                   (1, True, []),
                   (4, False, [{"sid": 4, "senders": "same"}])]


def test_a_real_time_record_is_read_on_the_wall_clock_across_midnight(run, capsys):
    rec = Record(wall=True)
    rec.state(0.0, 4, "STDBY_RC")
    one = rec.tx(0.5, 1, 0.8)
    rec.begin(0.5, 2, one, -110)
    rec.tx(0.9, 2, 0.3)
    rec.state(1.1, 4, "RX")
    rec.begin(1.1, 4, one, -121, cad=True)
    rec.tx(1.2, 4, 0.1)
    rec.write(run)
    report = judged(run, capsys)
    assert not report["virtual"] and report["untied"] == 0
    events = {(e["sid"], e["over"]): e for e in report["carrier_sense"]}
    assert set(events) == {(2, 1), (4, 1)}
    told = events[(2, 1)]
    assert (told["at"], told["told"], told["lock"]) == (0.9, True, True)
    told_late = events[(4, 1)]
    assert (told_late["at"], told_late["on_air_ms"], told_late["told_late"]) == (1.2, 700.0, True)


def test_frames_alike_at_one_instant_take_the_numbers_the_barrier_gave_them(run, capsys):
    rec = Record()
    # n04's tx reaches the ether first, but the barrier takes stations in
    # order and numbers n01's first; n04's does not lead it at n02, so it is
    # energy there, and lost
    four = rec.tx(5.0, 4, 0.2, eid=2)
    one = rec.tx(5.0, 1, 0.2, eid=1)
    rec.begin(5.0, 2, one, -110)
    rec.begin(5.0, 2, four, -123, cad=True)
    rec.end(2, one, "clean", -110)
    rec.end(2, four, "crc", -123)
    # again, with n01 on another sync word, so that n02 is told of n04's
    # alone: the level it was told at is n04's
    four = rec.tx(8.0, 4, 0.2, eid=4)
    rec.tx(8.0, 1, 0.2, sync=0x34, eid=3)
    rec.begin(8.0, 2, four, -123)
    rec.end(2, four, "clean", -123)
    rec.write(run)
    report = judged(run, capsys)
    assert report["untied"] == 0
    assert [(r["sid"], r["overlaps"]) for r in report["collisions"]["receptions"]] == \
        [(4, [{"sid": 1, "senders": "in earshot"}])]
    assert [(u["sid"], u["frames"]) for u in report["unheard"]] == [(1, 1)]


def written_by_the_ether(path, medium):
    """A record the ether itself writes, in virtual time, on the run's tables:
    all four send at 1 s, alike; three at 2 s, two of them alike, and n03
    over them; n01 and n04 alike at 3 s, and n02, told of n01's frame,
    answers at once, a second batch at the same T. The ether's own
    numbering comes back: number -> (sender, start).

    The ether is a run's, its conductor the core, and the four stations are
    sockets here that answer as the plan says. The run is over when every
    station is idle and nothing is due: nothing then can move T."""
    if not os.path.exists(ether_module.CORE_PATH):
        pytest.fail("no ether core built (sim build ether)")
    numbered = {}
    plan = {1: [(1000, 200), (2000, 200), (3000, 100)], 2: [(1000, 200), (2000, 150)],
            3: [(1000, 200), (2050, 200)], 4: [(1000, 200), (2000, 200), (3000, 100)]}

    async def go():
        loop = asyncio.get_running_loop()
        _, ether = await ether_module.open_ether(("127.0.0.1", 0), path, time_mode="max")
        at = ether.transport.get_extra_info("sockname")
        socks = {}
        try:
            ether.set_losses(medium.tables, {name: sid for sid, name in medium.names.items()})
            ether.on_tx = lambda sid, eid, freq, start, end: numbered.setdefault(eid, (sid, start))
            for sid in plan:
                socks[sid] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks[sid].bind(("127.0.0.1", 0))
                socks[sid].setblocking(False)

            def say(sid, msg):
                socks[sid].sendto(json.dumps(dict(msg, sid=sid)).encode(), at)

            def send(sid, t, span_ms):
                say(sid, {"type": "tx", "slot": 0, "id": 1, "mod": "lora", "freq": CALLING,
                          "bw": 125000, "sf": 8, "sync": 18, "power_dbm": 14, "payload": "AAAA", "t0": t,
                          "t_pre": t + 25088, "t_hdr": t + 41472, "t_end": t + span_ms * 1000})

            for sid in plan:
                ether.expect(sid)
            for sid in plan:
                say(sid, {"type": "hello", "slots": [0]})
            answered = False
            give_up = loop.time() + 20.0
            while True:
                heard = False
                for sid, sock in socks.items():
                    try:
                        msg = json.loads(sock.recv(65535))
                    except BlockingIOError:
                        continue
                    heard = True
                    t = msg["t"]
                    if msg["type"] == "welcome":
                        say(sid, {"type": "state", "slot": 0, "mode": "RX", "mod": "lora",
                                  "freq": CALLING, "bw": 125000, "sf": 8, "sync": 18})
                    while plan[sid] and plan[sid][0][0] * 1000 <= t:
                        send(sid, t, plan[sid].pop(0)[1])
                    if (sid == 2 and t == 3_000_000 and msg["type"] == "rx_begin"
                            and not msg.get("cad") and not answered):
                        answered = True
                        send(sid, t, 100)
                    say(sid, {"type": "idle", "seq": msg["seq"],
                              "until": plan[sid][0][0] * 1000 if plan[sid] else None})
                if heard:
                    continue
                if not ether.busy() and ether.core.next_instant() is None:
                    break
                assert loop.time() < give_up, "the run did not come to rest"
                await asyncio.sleep(0.002)
        finally:
            for sock in socks.values():
                sock.close()
            ether.close()

    asyncio.run(go())
    return numbered


def test_every_number_the_ether_gives_is_tied_to_its_own_frame(run):
    medium = RunView(run.dir).medium()
    path = os.path.join(run.dir, "record.tsv")
    numbered = written_by_the_ether(path, medium)
    assert len(numbered) == 11
    record = referee.Record(path, referee.Air(medium).level)
    assert record.untied == 0
    assert sum(1 for f in record.frames if f.begins and f.start in (1_000_000, 3_000_000)) == 5
    assert {eid: (f.sid, f.start) for eid, f in record.by_eid.items()} == \
        {eid: numbered[eid] for eid in record.by_eid}


def test_two_frames_begun_at_one_instant_are_counted_apart(run, capsys):
    """Neither was on the air when the other began, so neither began over the
    other: carrier sense had nothing to find. n01 and n02 hear each other and
    start together, and so do n01 and n03, which do not; only the first pair
    counts."""
    rec = Record()
    rec.tx(1.0, 1, 0.3)
    rec.tx(1.0, 2, 0.3)
    rec.tx(3.0, 1, 0.3)
    rec.tx(3.0, 3, 0.3)
    rec.write(run)
    report = judged(run, capsys)
    assert report["carrier_sense"] == []
    assert [(e["sid"], e["other"], e["at"]) for e in report["began_together"]] == [(1, 2, 1.0)]
    assert referee.main(["--run", run.dir]) == 0
    assert "and 1 time two transmissions began at one instant" in capsys.readouterr().out
