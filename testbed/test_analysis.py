"""The analysis tools against a small run laid out here: the tests' four
stations (testdata/four.yaml) on plain-27, its loss table computed there, and a
hand-written record and station logs; and the traffic driver against a
stand-in simulation."""

import argparse
import asyncio
import base64
import calendar
import contextlib
import datetime
import io
import json
import os
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ether"))

import airtime  # noqa: E402
import antennas  # noqa: E402
import compare  # noqa: E402
import compliance  # noqa: E402
import delivery  # noqa: E402
import ledger  # noqa: E402
import geodata  # noqa: E402
import links  # noqa: E402
import losses  # noqa: E402
import nodeset  # noqa: E402
import runs  # noqa: E402
import seq  # noqa: E402
import slt  # noqa: E402

import sim_mesh  # noqa: E402
from sim_mesh import reticulum  # noqa: E402
from sim_mesh.reticulum import frames  # noqa: E402
from sim_mesh import traffic as rtraffic  # noqa: E402
from sim_mesh.view import RunView  # noqa: E402

CALLING = 869_525_000
GLOBALS = "FREQ_MHZ = 869.525\nSF = 8\nBW_KHZ = 125\nCR = 5\n"
FOUR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "four.yaml")
PLAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "plain-27.yaml")
TRAFFIC_CH = 869_100_000
EPOCH = calendar.timegm((2026, 9, 25, 10, 0, 0))        # T = 0, wall clock
DEST = bytes(range(16))


def announce(hops):
    """An RNode-framed Reticulum announce of an lxmf.delivery destination."""
    data = b"\x11" * 64 + frames.name_hash("lxmf.delivery") + b"\x22" * 20
    return b"\x00" + bytes([0x01, hops]) + DEST + b"\x00" + data


def data_packet():
    return b"\x00" + bytes([0x00, 0]) + bytes(16) + b"\x00" + b"payload" * 4


class Record:
    """A record written line by line, the way the ether writes one."""

    def __init__(self):
        self.lines = ["# 2026-09-25T10:00:00+00:00\tether record: stamp\tdir\tsid\tjson"]
        self.eid = 0

    def add(self, t, direction, sid, msg):
        self.lines.append("%.6f\t%s\t%d\t%s" % (t, direction, sid,
                                                 json.dumps(msg, separators=(",", ":"))))

    def tx(self, t, sid, payload, heard, freq=CALLING, power=14, span=0.2):
        t0, t_end = int(round(t * 1e6)), int(round((t + span) * 1e6))
        b64 = base64.b64encode(payload).decode()
        self.add(t, "in", sid, {"type": "tx", "t0": t0, "t_end": t_end, "freq": freq,
                                "power_dbm": power, "sf": 8, "bw": 125000, "payload": b64})
        self.eid += 1               # the ether numbers the frame, once, heard or not
        for rsid, verdict in heard.items():
            self.add(t, "out", rsid, {"type": "rx_begin", "id": self.eid, "t0": t0,
                                      "t_end": t_end})
            self.add(t + span, "out", rsid, {"type": "rx_end", "id": self.eid,
                                             "verdict": verdict, "payload": b64})

    def write(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(self.lines) + "\n")


class WallRecord(Record):
    """A real-time run's record: stamped with the wall clock, `t` seconds
    after a second before a midnight."""

    MIDNIGHT = datetime.datetime(2026, 9, 29, 23, 59, 59, tzinfo=datetime.timezone.utc)

    def add(self, t, direction, sid, msg):
        stamp = (self.MIDNIGHT + datetime.timedelta(seconds=t)).isoformat(timespec="microseconds")
        self.lines.append("%s\t%s\t%d\t%s" % (stamp, direction, sid,
                                                 json.dumps(msg, separators=(",", ":"))))


def lay_out(tmp_path, category="reticulum", bare=False, name="run"):
    gd, ns = geodata.read(PLAIN, "plain-27"), nodeset.open_path(FOUR, "four")
    if bare:
        for each, node in ns.nodes.items():
            ns.set_node(each, tags=[t for t in node["tags"] if t != "transport"])
    table = tmp_path / "868.bin"
    if not table.exists():
        losses.synthetic_table(gd, ns, "868").write(str(table))
    run = runs.create_run(str(tmp_path / name), gd, ns, None, "max", {"868": str(table)},
                          builds={"alpha_latest": {"firmware": "alpha_x86_64_1.0.0",
                                                   "category": category}})
    # The tests' own radio, whatever the store's globals.py says today.
    with open(os.path.join(run.dir, runs.GLOBALS_FILE), "w", encoding="utf-8") as handle:
        handle.write(GLOBALS)
    run.set(firmware={n: "alpha_latest" for n in ns.nodes})

    rec = Record()
    rec.add(0, "in", 1, {"type": "hello", "sid": 1, "slots": [0], "t": 0})
    rec.add(0, "out", 1, {"type": "welcome", "epoch": EPOCH * 1_000_000, "mode": "virtual",
                          "t": 0})
    for sid in (2, 3, 4):
        rec.add(0.1 * sid, "in", sid, {"type": "hello", "sid": sid, "slots": [0], "t": 0})
    for sid in (1, 2, 3, 4):
        rec.add(1.0, "in", sid, {"type": "state", "mode": "RX", "freq": CALLING, "sf": 8})
    rec.tx(2.0, 1, announce(0), {2: "clean", 3: "clean", 4: "crc"})
    rec.tx(3.0, 2, announce(1), {1: "clean", 3: "clean"})
    rec.tx(4.0, 2, bytes([0xC2, 1, 2, 3]), {1: "clean"})
    rec.tx(5.0, 2, data_packet(), {1: "clean"}, freq=TRAFFIC_CH, power=8, span=0.1)
    rec.tx(5.2, 1, data_packet(), {2: "clean"}, freq=TRAFFIC_CH, power=14, span=0.1)
    rec.tx(6.0, 4, announce(0)[:-1] + b"\x33", {})
    rec.write(os.path.join(run.dir, "record.tsv"))

    # What the senders' drivers reported, as simd writes it.
    with open(os.path.join(run.dir, "events.jsonl"), "w") as handle:
        for event in ({"t": 6_000_000, "node": "n01", "event": "lxmf.message.status",
                       "mid": "o_1_ab", "status": "sent"},
                      {"t": 7_000_000, "node": "n01", "event": "lxmf.message.status",
                       "mid": "o_1_ab", "status": "delivered"},
                      {"t": 8_000_000, "node": "n02", "event": "lxmf.message.status",
                       "mid": "o_2_cd", "status": "failed", "why": "no_path"}):
            handle.write(json.dumps(event) + "\n")
    return run


@pytest.fixture
def run(tmp_path):
    return lay_out(tmp_path)


def call(main, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


# ---- the run, as the tools see it ----------------------------------------

def test_a_run_view_takes_everything_from_the_run(run):
    view = RunView(run.dir)
    assert view.names == {1: "n01", 2: "n02", 3: "n03", 4: "n04"}
    assert view.calling_hz() == CALLING
    assert view.radio("n01") == {"freq_hz": CALLING, "sf": 8, "bw_hz": 125000, "power_dbm": 14.0}
    assert view.roles() == {1: "transport", 2: "transport", 3: "transport", 4: "transport"}
    # Levels are the table's with the antennas on it: n01's frame at n02 is
    # its power plus each antenna's gain toward the other less the loss the
    # run's table holds. On flat ground at one height that is each pattern
    # on the horizon.
    table = slt.Table.read(run.table_path("868"))
    e = view.medium()
    horizon = sum(antennas.gain(view.nodes[n]["antenna"], 0.0, 0.0) for n in ("n01", "n02"))
    assert e.level(1, 2, CALLING, 14) == pytest.approx(
        14 + horizon - table.at("n01", "n02", CALLING), abs=0.01)
    need = e.noise(125000) + e.sensitivity(8)
    assert view.audible(1, 2) == (e.level(1, 2, CALLING, 14) >= need)
    n1, n2 = view.nodes["n01"], view.nodes["n02"]
    assert view.distance(1, 2) == pytest.approx(geodata.read(PLAIN, "plain-27").distance_m(
        (n1["lat"], n1["lon"]), (n2["lat"], n2["lon"])))
    assert view.protocols() == [reticulum] and view.category("n01") == "reticulum"


def test_offsets_reach_the_medium_the_tools_read(tmp_path):
    run = lay_out(tmp_path)
    ns = run.nodeset()
    before = RunView(run.dir).medium().level(1, 2, CALLING, 14)
    ns.set_offset("n01", "n02", 20)
    ns.save()
    assert RunView(run.dir).medium().level(1, 2, CALLING, 14) == pytest.approx(before - 20)


def test_links_reach_the_medium_the_tools_read(tmp_path):
    run = lay_out(tmp_path)
    ns = run.nodeset()
    before = RunView(run.dir).medium().level(1, 2, CALLING, 14)
    model = slt.Table.read(run.table_path("868")).get("n01", "n02")
    ns.data["links"] = [{"between": ["n01", "n02"], "loss_db": 100.0}]
    ns.save()
    after = RunView(run.dir).medium().level(1, 2, CALLING, 14)
    assert after == pytest.approx(before + model - 100, abs=0.01)


def test_shadowing_reaches_the_medium_the_tools_read(tmp_path):
    run = lay_out(tmp_path)
    before = RunView(run.dir).medium().level(1, 2, CALLING, 14)
    geodata.write(run.geodata_path, dict(run.geodata().data, shadowing_db=7, shadowing_seed=3))
    after = RunView(run.dir).medium().level(1, 2, CALLING, 14)
    assert after == pytest.approx(before - 7 * losses.shadowing_unit(3, "n01", "n02"), abs=0.01)


def test_a_run_with_no_reticulum_station_reads_no_protocol(tmp_path):
    other = lay_out(tmp_path, category="meshcore", bare=True, name="mc")
    view = RunView(other.dir)
    assert set(view.roles().values()) == {"client"} and view.forwarders() == set()
    assert view.protocols() == []
    assert view.radio("n01")["sf"] == 8                  # the run's globals.py
    assert sim_mesh.protocol_for("meshcore") is None
    code, text = call(airtime.main, [other.dir, "--roles"])
    out = json.loads(text)
    assert out["roles"]["client"]["stations"] == 4
    assert set(out["by_kind"]) == {"frame"}
    code, text = call(seq.main, [other.dir, "--tail", "1"])
    assert code == 0 and "MHz" in text and "ANNOUNCE" not in text


# ---- each tool's main path ----------------------------------------------

def test_airtime(run):
    code, text = call(airtime.main, [run.dir, "--busy", "--roles"])
    out = json.loads(text)
    assert code == 0 and out["frames"] == 6 and out["calling_hz"] == CALLING
    assert out["by_kind"]["announce lxmf.delivery"]["frames"] == 3
    assert out["by_kind"]["SUPE HAIL"]["frames"] == 1
    assert out["airtime"]["traffic_channels_s"] == pytest.approx(0.2)
    assert out["by_carrier_mhz"] == {"869.1": pytest.approx(0.2), "869.5": pytest.approx(0.8)}
    assert out["losses"]["receptions_crc"] == 1 and out["losses"]["frames_nobody_received"] == 1
    assert out["exchanges"] == {"count": 1, "reduced_power": 1, "frames_mean": 2.0}
    assert out["roles"]["transport"]["stations"] == 4
    assert out["roles"]["traffic_channels"] == 1
    assert out["busy_calling"]["top"][0][0] in ("n01", "n02", "n03", "n04")


def test_links(run):
    code, text = call(links.main, [run.dir, "--min-clean", "1", "--power"])
    out = json.loads(text)
    assert code == 0 and out["usable_links_one_way"] == 4     # the calling channel's
    assert out["both_ways"] == 1                     # n01 <-> n02
    assert out["stations_hearing_nobody"] == ["n04"]
    assert out["diameter_both_ways_through_forwarders"]["hops"] == 1
    model = out["model"]
    view = RunView(run.dir)
    expected = sum(1 for a in view.names for b in view.names if a != b and view.audible(a, b))
    assert model["links_one_way"] == expected and model["sf"] == 8 and model["power_dbm"] == 14
    assert out["power_traffic_channels"]["frames"] == 2
    assert out["power_traffic_channels"]["frames_with_peer"] == 2


def test_compliance_within(run):
    got = compliance.analyse(run.dir)
    bands = {name: [r["band"] for r in node["rows"]] for name, node in got.items()}
    assert bands["n01"] == ["868.7–869.2 MHz", "869.4–869.65 MHz"]
    assert got["n01"]["rows"][1]["worst_hour_s"] == pytest.approx(0.2)
    assert got["n01"]["rows"][1]["allowed_s"] == pytest.approx(360)
    assert got["n01"]["rows"][1]["erp_dbm"] == pytest.approx(14 - 2.15)
    text = compliance.section(run.dir)
    assert text.startswith("## ETSI compliance")
    assert "Every node stayed within its time and power budgets." in text


def test_compliance_over(tmp_path):
    run = lay_out(tmp_path)
    rec = Record()
    for k in range(925):                    # 0.5 s every 4 s: 450 s in an hour
        rec.tx(4.0 * k, 1, data_packet(), {}, span=0.5)
    for k in range(370):                    # 0.5 s every 10 s at 869.1: 180 s
        rec.tx(10.0 * k + 1, 2, data_packet(), {}, freq=TRAFFIC_CH, span=0.5)
    for k in range(93):                     # 0.5 s every 40 s at 869.1: 45 s
        rec.tx(40.0 * k + 2, 3, data_packet(), {}, freq=TRAFFIC_CH, span=0.5)
    rec.tx(3.0, 4, data_packet(), {}, freq=868_650_000, span=0.2)
    rec.tx(5.0, 4, data_packet(), {}, freq=CALLING, power=30, span=0.2)
    rec.tx(7.0, 4, data_packet(), {}, freq=915_000_000, span=0.2)
    rec.write(os.path.join(run.dir, "record.tsv"))
    got = compliance.analyse(run.dir)
    one = got["n01"]["rows"][0]
    assert one["time"] == "over" and one["worst_hour_s"] == pytest.approx(450, abs=0.5)
    two = got["n02"]["rows"][0]
    assert two["time"] == "over" and two["psa"]["worst_hour_s"] == pytest.approx(180, abs=0.5)
    three = got["n03"]["rows"][0]
    assert three["time"] == "PSA" and three["psa"]["longest_frame_s"] == pytest.approx(0.5)
    four = got["n04"]
    assert four["no_entry"]["frames"] == 1 and four["outside"]["frames"] == 1
    assert not four["rows"][0]["power_ok"]
    text = compliance.section(run.dir)
    assert "**Over budget:** n01 (time in 869.4–869.65 MHz); n02 (time in 868.7–869.2 MHz); " \
           "n04 (power in 869.4–869.65 MHz, spectrum no entry allows)." in text
    assert "n04 sent 1 frame (0.2 s)" in text and "within PSA" in text


def test_compliance_says_when_the_worst_hour_began_on_the_runs_clock(tmp_path):
    """A real-time run: a station's `tx` states its own clock, here 1000 s
    ahead of the record's, and the hour's start is told on the record's."""
    run = lay_out(tmp_path)
    rec = WallRecord()
    rec.add(0.0, "in", 1, {"type": "hello", "sid": 1, "slots": [0], "t": 0})
    sent(rec, 5.0, 1, data_packet(), 0.5, own=1000.0)
    sent(rec, 65.0, 1, data_packet(), 0.5, own=1000.0)
    rec.write(os.path.join(run.dir, "record.tsv"))
    row = compliance.analyse(run.dir)["n01"]["rows"][0]
    assert row["worst_hour_s"] == pytest.approx(1.0) and row["worst_hour_from"] == 5.0
    assert "1.0 s (0.03 %) from T 00:00:05" in compliance.section(run.dir)


def test_seq(run):
    code, text = call(seq.main, [run.dir])
    assert code == 0
    assert "n01" in text.splitlines()[0] and "n04" in text.splitlines()[0]
    assert "ANNOUNCE    single/00010203  of lxmf.delivery" in text
    assert "SUPE HAIL" in text and "→ nobody" in text
    code, text = call(seq.main, ["--record", os.path.join(run.dir, "record.tsv"),
                                 "--names", "1=alpha", "--only", "supe"])
    assert code == 0 and "alpha" in text and len(text.splitlines()) == 4


def test_compare(run, tmp_path):
    code, text = call(compare.main, [run.dir, run.dir, "--logs"])
    assert code == 0
    assert "every station announced" in text
    assert "first 1-hop path" in text and "first 2-hop path" in text
    assert "n01" in text and "LXMF messages proven delivered" in text
    rows = [line.split() for line in text.splitlines() if line.startswith("n01 ")]
    assert rows[0] == ["n01", "7", "7"]            # delivered 7 s after the first hello


def told(rec, t, rsid, eid, t0, t_end, cad=False):
    """An rx_begin, as the ether records one."""
    msg = {"type": "rx_begin", "slot": 0, "id": eid, "t0": t0, "t_end": t_end}
    if cad:
        msg["cad"] = True
    rec.add(t, "out", rsid, msg)


def ended(rec, t, rsid, eid, verdict, payload):
    rec.add(t, "out", rsid, {"type": "rx_end", "slot": 0, "id": eid, "verdict": verdict,
                             "payload": base64.b64encode(payload).decode()})


def sent(rec, t, sid, payload, span, own=0.0):
    """A `tx`, its timeline on the station's clock: T, or in a real-time run
    its own, `own` seconds ahead of the record's."""
    t0 = int(round((t + own) * 1e6))
    rec.add(t, "in", sid, {"type": "tx", "sid": sid, "t0": t0, "t_end": t0 + int(span * 1e6),
                           "freq": CALLING, "sf": 8, "bw": 125000, "power_dbm": 14,
                           "payload": base64.b64encode(payload).decode()})


def test_a_reception_is_tied_to_its_frame_by_the_ethers_number(tmp_path):
    """At 5 s n04's tx reaches the ether first, but the barrier numbers n01's
    frame 1 and n04's 2, and tells their receivers in that order. At 7 s n03
    comes to RX during n02's frame 3, after n04's frame 4 has gone out, and
    is told of frame 3 then."""
    run = lay_out(tmp_path)
    rec = Record()
    sent(rec, 5.0, 4, b"four", 0.3)
    sent(rec, 5.0, 1, b"one", 0.2)
    told(rec, 5.0, 2, 1, 5_000_000, 5_200_000)
    told(rec, 5.0, 3, 2, 5_000_000, 5_300_000)
    ended(rec, 5.2, 2, 1, "clean", b"one")
    ended(rec, 5.3, 3, 2, "crc", b"four")
    sent(rec, 7.0, 2, b"two", 0.5)
    told(rec, 7.0, 1, 3, 7_000_000, 7_500_000)
    sent(rec, 7.1, 4, b"quick", 0.1)
    rec.add(7.15, "in", 3, {"type": "state", "slot": 0, "mode": "RX", "freq": CALLING, "sf": 8})
    told(rec, 7.15, 3, 3, 7_150_000, 7_500_000, cad=True)
    ended(rec, 7.5, 1, 3, "clean", b"two")
    path = os.path.join(run.dir, "record.tsv")
    rec.write(path)

    heard = {f.payload: f.heard for f in seq.read_record(path, seq.read_bytes)}
    assert heard == {b"four": {3: "crc"}, b"one": {2: "clean"},
                     b"two": {1: "clean", 3: "cad"}, b"quick": {}}
    assert compare.rx_senders(path) == {1: 1, 2: 4, 3: 2}


def test_airtime_and_links_read_a_real_time_record_across_midnight(tmp_path):
    """Stamps are the wall clock with its date; a `tx` states its station's
    own clock and an rx_begin the ether's, neither of them the record's."""
    run = lay_out(tmp_path)
    rec = WallRecord()
    ether_us = lambda t: 7_000_000_000 + int(round(t * 1e6))       # noqa: E731
    sent(rec, 0.5, 1, announce(0), 0.2, own=1000.0)
    told(rec, 0.5, 2, 1, ether_us(0.5), ether_us(0.7))
    ended(rec, 0.7, 2, 1, "clean", announce(0))
    sent(rec, 1.5, 2, announce(1), 0.2, own=2000.0)                  # after the midnight
    told(rec, 1.5, 1, 2, ether_us(1.5), ether_us(1.7))
    ended(rec, 1.7, 1, 2, "crc", announce(1))
    rec.write(os.path.join(run.dir, "record.tsv"))
    code, text = call(airtime.main, [run.dir, "--busy"])
    out = json.loads(text)
    assert code == 0 and out["frames"] == 2 and out["window_s"] == pytest.approx(1.2)
    assert out["losses"]["receptions_clean"] == 1 and out["losses"]["receptions_crc"] == 1
    assert dict(out["busy_calling"]["top"]) == {"n01": pytest.approx(0.4 / 1.2),
                                                "n02": pytest.approx(0.4 / 1.2)}
    # --from counts from the record's first line.
    assert json.loads(call(airtime.main, [run.dir, "--from", "1"])[1])["frames"] == 1
    code, text = call(links.main, [run.dir, "--min-clean", "1"])
    assert code == 0 and json.loads(text)["usable_links_one_way"] == 1


def test_seq_reads_a_real_time_record_across_midnight(tmp_path):
    rec = WallRecord()
    sent(rec, 0.5, 1, announce(0), 0.2, own=1000.0)
    sent(rec, 1.5, 2, announce(1), 0.2, own=2000.0)                  # after the midnight
    path = str(tmp_path / "record.tsv")
    rec.write(path)
    frames = seq.read_record(path)
    assert frames[1].at - frames[0].at == pytest.approx(1.0)
    code, text = call(seq.main, ["--record", path])
    assert code == 0 and [line.split()[0] for line in text.splitlines()[1:3]] == ["0.000", "1.000"]


def test_compare_reads_a_real_time_record_longer_than_a_day(tmp_path):
    """compare read a stamp's time of day and undid one midnight, and that
    only after the zero: a station that joined after the midnight joined a
    day before the first, and a frame a day on was seconds in."""
    run = lay_out(tmp_path)
    path = os.path.join(run.dir, "record.tsv")

    def record(joins):
        rec = WallRecord()
        for sid, t in joins:
            rec.add(t, "in", sid, {"type": "hello", "sid": sid, "slots": [0], "t": 0})
        sent(rec, 86_403.5, 3, announce(0), 0.2, own=1000.0)
        rec.write(path)

    def last_joined(*argv):
        code, text = call(compare.main, [run.dir, *argv])
        assert code == 0
        return next(line.split()[-1] for line in text.splitlines()
                    if line.startswith("last station joined"))

    record([(1, 0.5), (2, 1.5), (3, 86_402.5)])         # after the midnight, and a day on
    got = compare.Run(path, {})
    assert got.joined == {1: 0.0, 2: pytest.approx(1.0, abs=1e-3),
                          3: pytest.approx(86_402.0, abs=1e-3)}
    assert got.first_announce == {3: pytest.approx(86_403.0, abs=1e-3)}
    assert got.end == pytest.approx(86_403.0, abs=1e-3)
    # --starts is a real run's time of day: 23:59:59, the second before the midnight,
    # also for a record whose first line comes after it.
    assert last_joined("--starts", "86399") == "86402.5"
    record([(2, 1.5), (3, 86_402.5)])
    assert last_joined("--starts", "86399") == "86402.5"


def test_frames_alike_in_their_bytes_at_one_instant_keep_their_own_receptions(tmp_path):
    """n01 and n02 send the same bytes at the same T: n04 receives n01's,
    and n03 loses n02's."""
    run = lay_out(tmp_path)
    rec = Record()
    sent(rec, 5.0, 1, data_packet(), 0.2)
    sent(rec, 5.0, 2, data_packet(), 0.2)
    told(rec, 5.0, 4, 1, 5_000_000, 5_200_000)
    told(rec, 5.0, 3, 2, 5_000_000, 5_200_000)
    ended(rec, 5.2, 4, 1, "clean", data_packet())
    ended(rec, 5.2, 3, 2, "crc", data_packet())
    path = os.path.join(run.dir, "record.tsv")
    rec.write(path)
    losses_now = json.loads(call(airtime.main, [run.dir])[1])["losses"]
    assert losses_now["frames_received_somewhere"] == 2
    assert losses_now["frames_lost_at_every_receiver"] == 1
    assert losses_now["frames_nobody_received"] == 0
    got = {f["sid"]: (f["clean"], f["crc"])
           for f in links.read(argparse.Namespace(record=path, frm=None, to=None))}
    assert got == {1: ({4}, 0), 2: (set(), 1)}


def test_delivery(run, tmp_path):
    drive = {"sends": [
        {"marker": "G0001", "src": "n01", "dst": "n02", "cls": "short", "hops": 1,
         "mid": "o_1_ab", "t_sent": 5_000_000},
        {"marker": "G0002", "src": "n02", "dst": "n01", "cls": "two", "hops": None,
         "mid": "o_2_cd", "t_sent": 6_000_000},
        {"marker": "G0003", "src": "n03", "dst": "n04", "cls": "big", "mid": None}]}
    path = tmp_path / "traffic.json"
    path.write_text(json.dumps(drive))
    out_json = tmp_path / "delivery.json"
    code, text = call(delivery.main, [str(path), run.dir, "--json", str(out_json)])
    out = json.loads(text)
    assert code == 0 and out["sent"] == 3 and out["delivered"] == 1 and out["no_mid"] == 1
    assert out["by_route_hops"] == {"1": "1/1 (100.0%)", "no path": "0/2 (0.0%)"}
    assert out["latency_s"]["median"] == pytest.approx(2.0)
    words = dict(out["undelivered_last_word"])
    assert "no mid" in words and any("failed" in w for w in words)
    rows = json.loads(out_json.read_text())["messages"]
    # On plain-27 n01 and n02 (2.8 km) hear each other; n03 and n04 (19 km)
    # do not, and meet through n02, a transport.
    graph = RunView(run.dir).radio_graph()
    assert 2 in graph[1] and 4 not in graph[3] and {3, 4} <= graph[2]
    assert [r["radio_hops"] for r in rows] == [1, 1, 2]


def test_a_record_read_for_some_types_is_those_lines_of_it_read_whole(tmp_path):
    """`lines(path, types)` passes over the lines it cannot want unparsed:
    what it gives is exactly what reading every line gives, of those types,
    whatever the spacing, with a nested `type` of theirs not enough, and
    the malformed lines skipped alike."""
    from sim_mesh import record as record_module
    path = tmp_path / "record.tsv"
    path.write_text("\n".join([
        "# 2026-09-29T00:00:00+00:00\tether record: stamp\tdir\tsid\tjson",
        '0.001000\tin\t1\t{"sid":1,"slots":[0],"t":0,"type":"hello"}',
        '0.002000\tin\t1\t{"mode":"RX","nested":{"type":"tx"},"slot":0,"type":"state"}',
        '0.003000\tin\t1\t{"t0": 3000, "t_end": 9000, "type": "tx"}',
        '0.004000\tin\t2\t{"t0":4000,"t_end":9000,"type":"tx"',          # cut short
        '0.005000\tin\t-\t{"t0":5000,"t_end":9000,"type":"tx"}',         # no station
        '0.006000\tout\t2\t{"id":1,"type":"rx_begin"}',
        '0.007000\tin\t2\t{"t0":7000,"t_end":9000,"type":"tx"}',
        '0.008000\tin\t2\t["type", "tx"]',
    ]) + "\n")
    everything = list(record_module.lines(str(path)))
    for types in (("tx",), ("hello",), ("tx", "rx_begin"), ("state",), ("nothing",)):
        want = [item for item in everything
                if isinstance(item[3], dict) and item[3].get("type") in types]
        assert list(record_module.lines(str(path), types=types)) == want, types
    assert [m["t0"] for _s, _d, _i, m in record_module.lines(str(path), ("tx",))] == [3000, 7000]
    assert record_module.first_stamp(str(path)) == "0.001000"
    empty = tmp_path / "empty.tsv"
    empty.write_text("# nothing yet\n")
    assert record_module.first_stamp(str(empty)) is None


def rns_packet(ptype, dest, data, hops=0, via=None, ctx=0):
    """A Reticulum packet's bytes: flags, hops, [transport address,]
    destination, context, data."""
    flags = (0x40 if via else 0) | ptype
    return bytes([flags, hops]) + (via or b"") + dest + bytes([ctx]) + data


def tx_line(t_s, sid, frame, span_us=100_000):
    t = int(t_s * 1e6)
    return "%.6f\tin\t%d\t%s" % (t_s, sid, json.dumps(
        {"type": "tx", "sid": sid, "t0": t, "t_end": t + span_us, "freq": 869525000,
         "payload": base64.b64encode(frame).decode()}, separators=(",", ":"), sort_keys=True))


def test_the_ledger_ties_every_copy_of_a_packet_to_it(tmp_path):
    """A relayed copy (another hop count, a transport address) is the same
    packet; a split packet is one; a path request's answers are its
    responders', a station answering twice a repeat; a proof belongs to the
    packet it proves."""
    dest, other = bytes(range(16)), bytes(range(16, 32))
    via1, via2 = b"\xaa" * 16, b"\xbb" * 16
    data = rns_packet(0, dest, b"hello", hops=0)
    relayed = rns_packet(0, dest, b"hello", hops=1, via=via1)
    big = rns_packet(0, other, bytes(range(256)) * 2)
    path_request = next(d for d, n in frames.PLAIN_DESTS.items()
                        if n == "rnstransport.path.request")
    request = rns_packet(0, path_request, other + b"\x01" * 16 + b"\x02" * 16)
    answer = rns_packet(1, other, b"\x00" * 148, hops=1, via=via2, ctx=0x0B)
    proof = rns_packet(3, ledger.packet_hash(data)[:16], b"\x03" * 64)
    lines = ["# 2026-09-30T00:00:00+00:00\tether record: stamp\tdir\tsid\tjson",
             tx_line(1.0, 1, b"\x00" + data),
             tx_line(1.5, 2, b"\x00" + relayed),
             tx_line(2.0, 3, b"\x01" + big[:250]),        # split: first half
             tx_line(2.2, 3, b"\x01" + big[250:]),        # second half
             tx_line(3.0, 4, b"\x00" + request),
             tx_line(3.5, 5, b"\x00" + answer),
             tx_line(3.9, 6, b"\x00" + rns_packet(1, other, b"\x00" * 148, hops=2, via=via1,
                                                   ctx=0x0B)),
             tx_line(4.3, 6, b"\x00" + rns_packet(1, other, b"\x00" * 148, hops=2, via=via1,
                                                   ctx=0x0B)),
             tx_line(5.0, 7, b"\x00" + proof),
             tx_line(5.4, 8, b"\x00" + rns_packet(3, ledger.packet_hash(data)[:16],
                                                   b"\x03" * 64, hops=1, via=via2)),
             tx_line(6.0, 9, b"\xc2" + b"\x00" * 10)]  # SUPE HAIL: no Reticulum packet
    path = tmp_path / "record.tsv"
    path.write_text("\n".join(lines) + "\n")

    out = ledger.analyse(str(path))
    by = out["by_kind"]
    assert by["data"]["packets"] == 2 and by["data"]["transmissions"] == 3
    assert (by["data"]["first"], by["data"]["other_station"], by["data"]["repeat"]) == (2, 1, 0)
    assert by["data"]["airtime_s"] == pytest.approx(0.4)     # the split packet is two frames' air
    pr = out["path_requests"]
    assert pr["requests"] == 1 and pr["answered"] == 1
    assert pr["per_request"][0]["responders"] == [5, 6] and pr["per_request"][0]["responses"] == 3
    assert by["path response"]["packets"] == 1
    assert (by["path response"]["other_station"], by["path response"]["repeat"]) == (1, 1)
    assert out["proofs"] == {"proven_packets": 1, "proofs": 1, "transmissions": 2,
                             "proven_more_than_once": 0}
    assert out["not_reticulum"] == {"SUPE HAIL": {"frames": 1, "airtime_s": pytest.approx(0.1)}}
    assert out["airtime_s"] == pytest.approx(1.1)
    later = ledger.analyse(str(path), lo=3_000_000)
    assert "data" not in later["by_kind"] and later["path_requests"]["responses"] == 3


# ---- the traffic driver, against a stand-in simd ------------------------

class FakeSim:
    """Just enough of a simulation's control socket for a driver: its
    snapshot with the stations up, and commands and intents answered by the
    stations chosen, each answer carrying the asker's id."""

    def __init__(self, names, categories=None, failing_sequences=0):
        self.names = names
        self.categories = categories or {}      # name -> category, reticulum unless said
        self.got = []
        self.turns = []                 # drive and yield, as they came
        # The first this many sequences (a message's route and send) fail on
        # the way, answered as simd answers a request it could not do.
        self.failing_sequences = failing_sequences
        self.sequences = 0

    @staticmethod
    def reply(m, i):
        """A line's printed answer, or what a verb returns."""
        if m["type"] == "command":
            return "did %s" % m["line"]
        table = [{"dest": ("%02d" % k) * 16, "next_hop": ("%02d" % k) * 16, "iface": "lora/0",
                  "hops": 1} for k in range(3)]
        return {"lxmf.identities": [["n%02d" % i, ("%02d" % i) * 16]], "path": table,
                "lxmf.announce": None, "lxmf.send": "mid-%d" % i,
                "diagnostics": {"paths": "3"}}.get(m["verb"])

    async def handle(self, request):
        from aiohttp import web
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "snapshot", "clock": {"t": 1_000_000},
                            "run": {"dir": "/runs/fake"},
                            "nodes": [{"name": n, "id": i + 1, "status": "up", "base": "alpha",
                                       "category": self.categories.get(n, "reticulum"),
                                       "firmware": "alpha_latest",
                                       "max_dbm": 22, "tags": ["even"] if i % 2 else []}
                                      for i, n in enumerate(self.names)]})
        async for msg in ws:
            m = json.loads(msg.data)
            if m["type"] in ("drive", "yield"):
                self.turns.append(m["type"])
                continue
            self.got.append(m)
            if m["type"] == "wait":
                await ws.send_json({"type": "command_result", "id": m.get("id"), "results": {},
                                    "t": max(2_000_000, int(m.get("until") or 0))})
            if m["type"] in ("firmware", "first_boot"):
                await ws.send_json({"type": "command_result", "id": m.get("id"), "results": {},
                                    "t": 2_000_000})
            if m["type"] == "sequence":
                self.sequences += 1
                who = m.get("names") or self.names
                if self.sequences <= self.failing_sequences:
                    await ws.send_json({"type": "command_result", "id": m.get("id"), "results": {},
                                        "error": "rncfg gave no answer in 10s", "t": 2_000_000})
                else:
                    await ws.send_json({"type": "command_result", "id": m.get("id"), "t": 2_000_000,
                                        "results": [{n: [] for n in who},
                                                    {n: "%032x" % 7 for n in who}]})
            if m["type"] in ("command", "meta"):
                who = [m["name"]] if m.get("name") else m.get("names") or self.names
                results = {n: self.reply(m, i) for i, n in enumerate(who)}
                await ws.send_json({"type": "command_result", "id": m.get("id"),
                                    "name": m.get("name"), "t": 2_000_000, "results": results})
        return ws


def with_fake_sim(fake, drive):
    from aiohttp import web

    async def go():
        app = web.Application()
        app.router.add_get("/ws", fake.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        sim = await sim_mesh.attach("fake", port)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return await drive(sim)
        finally:
            await sim.close()
            await sim.session.close()
            await runner.cleanup()
    return asyncio.run(go())


def test_a_driver_chooses_stations_and_asks_them(tmp_path):
    fake = FakeSim(["n01", "n02", "n03"])

    async def drive(sim):
        assert sim.run_dir == "/runs/fake" and sim.run_s == 0
        await sim.all_up()
        evens = sim.nodes(tag="even")
        assert evens.names == ["n02"] and len(sim.nodes(category="reticulum")) == 3
        assert await evens.verb("lxmf.announce", "reticulum", spread=30) == {"n02": None}
        assert await sim.node("n01").run("x", after=2) == {"n01": "did x"}
        assert sim.pairs(sample=2, seed=1) == sim.pairs(sample=2, seed=1)
        assert len(sim.pairs()) == 6
        with pytest.raises(sim_mesh.SimError):
            sim.node("nobody")
        await sim.plan(("warm", 10))

    with_fake_sim(fake, drive)
    assert fake.turns[0] == "drive"
    meta = [m for m in fake.got if m["type"] == "meta"][0]
    assert meta["verb"] == "lxmf.announce" and meta["names"] == ["n02"] and meta["stagger"] == 30
    assert meta["category"] == "reticulum"
    command = [m for m in fake.got if m["type"] == "command"][0]
    assert command["names"] == ["n01"] and command["after"] == 2
    assert [m for m in fake.got if m["type"] == "plan"][0]["phases"] == [
        {"name": "warm", "until": 11_000_000}]


class GoneSim(FakeSim):
    """A simulation the front stops under a driver: the registry lists it
    running, then, when the driver asks anything, no longer running, while
    the socket to the front stays open and nothing is answered."""

    async def handle(self, request):
        from aiohttp import web
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "snapshot", "clock": {"t": 1_000_000},
                            "run": {"dir": "/runs/fake"},
                            "nodes": [{"name": n, "status": "up"} for n in self.names]})
        await ws.send_json({"type": "sims", "sims": [{"name": "fake", "state": "running"}]})
        async for msg in ws:
            m = json.loads(msg.data)
            if m["type"] in ("drive", "yield"):
                continue
            self.got.append(m)
            await ws.send_json({"type": "sims", "sims": [{"name": "fake", "state": "exited"},
                                                         {"name": "other", "state": "running"}]})
        return ws


def test_a_driver_hears_its_simulation_go_while_the_front_stays():
    fake = GoneSim(["n01"])

    async def drive(sim):
        await asyncio.sleep(0.2)
        sim.check()                 # listed running: still there
        with pytest.raises(sim_mesh.SimError, match="went away"):
            await asyncio.wait_for(sim.node("n01").run("x"), 5)
        with pytest.raises(sim_mesh.SimError, match="went away"):
            await asyncio.wait_for(sim.until(3600), 5)
        with pytest.raises(sim_mesh.SimError, match="went away"):
            await sim.node("n01").run("y")

    with_fake_sim(fake, drive)
    assert [m.get("line") for m in fake.got] == ["x"]


def test_the_traffic_driver_runs_on_a_sim(tmp_path):
    fake = FakeSim(["n01", "n02"])
    out = tmp_path / "out.json"
    opts = {"warm_rounds": 1, "warm_spread": 0, "settle_every": 1, "traffic": 0, "drain": 0}
    result = with_fake_sim(fake, lambda sim: rtraffic.run_on(sim, opts, str(out)))
    metas = [m["verb"] for m in fake.got if m["type"] == "meta"]
    assert metas[:3] == ["lxmf.identities", "lxmf.announce", "path"]
    assert sorted(result["dests"]) == ["n01", "n02"]
    assert result["warm"]["samples"][-1]["total"] == 6
    assert result["gathered"]["results"]["n01"] == {"paths": "3"}
    assert json.loads(out.read_text())["phases"][-1][0] == "gathered"
    with pytest.raises(ValueError, match="no traffic option"):
        rtraffic.Options(colour="blue")


def test_a_message_whose_send_fails_is_recorded_so_and_the_rest_go_on(tmp_path):
    """simd answers a request it could not do with the error (a tool that
    gave no answer, say): that message is recorded as failed, and the
    traffic goes on. The driver had waited on the answer for ever."""
    fake = FakeSim(["n01", "n02"], failing_sequences=1)
    opts = {"warm_rounds": 0, "settle_every": 1, "traffic": 10, "every": 5, "drain": 0}
    result = with_fake_sim(fake, lambda sim: rtraffic.run_on(sim, opts, str(tmp_path / "out.json")))
    sends = result["sends"]
    assert len(sends) == fake.sequences >= 2
    assert sends[0]["error"] == "rncfg gave no answer in 10s" and "t_sent" not in sends[0]
    assert all("error" not in s and s["mid"] == "%032x" % 7 for s in sends[1:])


def test_a_script_says_it_synchronously(monkeypatch, tmp_path):
    """The library as a script uses it: plain calls, on a simulation held
    on a loop of its own, its rules said once it is attached, its commands
    in the run's scripts.log at that level."""
    from aiohttp import web

    from sim_mesh import library

    fake = FakeSim(["n01", "n02", "n03", "m04"], {"m04": "meshcore"})
    loop = asyncio.new_event_loop()
    started = threading.Event()
    port = []

    async def serve():
        app = web.Application()
        app.router.add_get("/ws", fake.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port.append(site._server.sockets[0].getsockname()[1])
        started.set()

    server = threading.Thread(target=lambda: (loop.run_until_complete(serve()),
                                              loop.run_forever()), daemon=True)
    server.start()
    started.wait(10)
    fresh = library.Runtime()
    monkeypatch.setattr(library, "runtime", fresh)
    fresh.configure(script_name="smoke", sim="fake", port=port[0])
    Node, nodes, node = library.Node, library.nodes, library.node
    reticulum = ["n01", "n02", "n03"]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            nodes().firmware("alpha_latest")
            nodes(tag="even").on_first_boot(Node.radio(sf=9), "log rnsd debug",
                                            Node.reticulum.lxmf.create())
            assert nodes().up() == reticulum + ["m04"]
            # Said to the running simulation, as a nodeset's own setup says it
            # when a script includes it after the simulation is there.
            assert nodes(tag="even").firmware("beta_latest") == {}
            assert fake.got[-1]["type"] == "firmware"
            assert fake.got[-1]["rules"] == [{"which": {"where": {"tag": "even"}},
                                              "firmware": "beta_latest"}]
            fresh.open_log(str(tmp_path))
            library.script_loglevel("commands")
            assert nodes(tag="even").exec("one\ntwo") == {"n02": "did one\ndid two",
                                                          "m04": "did one\ndid two"}
            # A Reticulum command is nothing to the meshcore node.
            assert nodes().reticulum.lxmf.announce(spread=30) == {n: None for n in reticulum}
            later = node("n01").reticulum.lxmf.send("n03", "hi", after=5, wait=False)
            assert library.script_results([later]) == [{"n01": "mid-0"}]
            assert node("n02").reticulum.path(to="n03")["n02"][0]["hops"] == 1
            nodes().radio(tx_dbm="max")
            assert node("n02").facts()["n02"]["max_dbm"] == 22
            with pytest.raises(library.ScriptError, match="no reticulum node"):
                node("m04").reticulum.role("client")
            with pytest.raises(library.ScriptError, match="no spread, after or wait"):
                Node.radio_up(after=3)
            with pytest.raises(library.ScriptError, match="comes before"):
                library.sim_speed("max")
    finally:
        fresh.close()
        loop.call_soon_threadsafe(loop.stop)
    kinds = [m["type"] for m in fake.got]
    assert kinds[:2] == ["firmware", "first_boot"]
    assert fake.got[1]["rules"][0]["lines"] == [
        {"verb": "radio", "args": {"sf": 9}}, "log rnsd debug",
        {"verb": "lxmf.create", "args": {}, "category": "reticulum"}]
    metas = [m for m in fake.got if m["type"] == "meta"]
    announce = [m for m in metas if m["verb"] == "lxmf.announce"][0]
    assert announce["names"] == reticulum and announce["category"] == "reticulum"
    sent = [m for m in metas if m["verb"] == "lxmf.send"][0]
    assert sent["args"] == {"to": "n03", "text": "hi"} and sent["after"] == 5
    assert sent["names"] == ["n01"]
    radio = [m for m in metas if m["verb"] == "radio"][0]
    assert radio["args"] == {"tx_dbm": "max"} and "category" not in radio
    logged = (tmp_path / "scripts.log").read_text()
    assert " smoke node('n01').reticulum.lxmf.send(to='n03', text='hi') → " in logged
    assert "exec(['one', 'two'])" in logged


def test_the_schedule_is_the_seed_s():
    one = rtraffic.schedule(["a", "b", "c"], 17, 30, 5, "G")
    assert one == rtraffic.schedule(["c", "b", "a"], 17, 30, 5, "G")
    assert [s[0] for s in one] == [1, 2, 3, 4, 5, 6] and all(s[2] != s[3] for s in one)


def the_schedule_as_it_was(names, seed, duration, every, marker):
    """traffic.schedule before its variants, word for word."""
    import random
    rng = random.Random(seed)
    names = sorted(names)
    out = []
    n = 0
    while n * every < duration:
        src = rng.choice(names)
        dst = rng.choice([s for s in names if s != src])
        cls = rng.choice(rtraffic.CLASSES)
        out.append((n + 1, n * every, src, dst, cls,
                    rtraffic.body(rng, cls, "%s%04d" % (marker, n + 1))))
        n += 1
    return out


def test_the_default_schedule_is_the_one_it_always_was():
    names = ["n%02d" % i for i in range(1, 28)]
    for seed in (17, 101, 102, 103):
        assert rtraffic.schedule(names, seed, 1800, 5.0, "G") == \
            the_schedule_as_it_was(names, seed, 1800, 5.0, "G")


def test_the_variants_keep_the_messages_and_change_when_and_between_whom():
    names = ["n%02d" % i for i in range(1, 11)]
    even = rtraffic.schedule(names, 7, 3600, 5.0, "G")
    texts = [(s[4], s[5]) for s in even]

    poisson = rtraffic.schedule(names, 7, 3600, 5.0, "G", arrivals="poisson")
    ats = [s[1] for s in poisson]
    assert ats[0] == 0 and ats == sorted(ats) and ats[-1] < 3600
    assert 600 < len(poisson) < 840                 # 720 expected, a rate of one per 5 s
    k = min(len(poisson), len(texts))
    assert [(s[4], s[5]) for s in poisson][:k] == texts[:k]
    assert poisson == rtraffic.schedule(names, 7, 3600, 5.0, "G", arrivals="poisson")

    paired = rtraffic.schedule(names, 7, 3600, 5.0, "G", pairs=3)
    assert len({(s[2], s[3]) for s in paired}) <= 3 and all(s[2] != s[3] for s in paired)
    assert [(s[4], s[5]) for s in paired] == texts and [s[1] for s in paired] == [s[1] for s in even]

    hubbed = rtraffic.schedule(names, 7, 3600, 5.0, "G", hub="n01", hub_share=0.5)
    others = [s for s in hubbed if s[2] != "n01"]
    share = sum(1 for s in others if s[3] == "n01") / len(others)
    assert 0.45 < share < 0.62 and all(s[2] != s[3] for s in hubbed)
    assert [(s[4], s[5]) for s in hubbed] == texts

    for bad in ({"arrivals": "bursty"}, {"hub": "nobody"}, {"hub_share": 2}, {"pairs": -1}):
        with pytest.raises(ValueError):
            rtraffic.schedule(names, 7, 60, 5.0, "G", **bad)
