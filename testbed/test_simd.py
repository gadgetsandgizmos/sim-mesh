"""simd on synthetic ground with stand-in firmware: loading, first boot (the
first-boot rules in order, the radio last), the tables and offsets, a move
recomputed into the run's copy, an id change, levels, commands and verbs on
chosen stations, events, snapshots."""

import asyncio
import copy
import json
import os
import socket
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drivers  # noqa: E402
import firmware  # noqa: E402
import simd  # noqa: E402
import slt  # noqa: E402
import store  # noqa: E402
import stub_firmware  # noqa: E402
from sim_mesh.reticulum.driver import ReticulumDriver  # noqa: E402


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


STUB = "stub_latest"
ALL_STUB = {"which": {"all": True}, "firmware": STUB}
# What a script's runner hands a new simulation: its firmware and first-boot rules.
FAR = {"script": "far", "firmware_rules": [ALL_STUB], "first_boot_rules": [
    {"which": {"where": {"tag": "transport"}}, "lines": [
        {"verb": "role", "args": {"role": "transport"}, "category": "reticulum"}]},
    {"which": {"not": {"where": {"tag": "no-radio"}}},
     "lines": [{"verb": "radio", "args": {"freq_mhz": 869.525, "sf": 8, "tx_dbm": "max"}}]},
    {"which": {"where": {"tag": "far"}}, "lines": ["far {addr}", "role transport"]},
    {"which": {"names": ["c"]}, "lines": ["txp {max_dbm}"]},
    {"which": {"not": {"where": {"tag": "no-radio"}}}, "lines": [{"verb": "radio_up",
                                                                  "args": {}}]}]}
STUBS = {"firmware_rules": [ALL_STUB, {"which": {"where": {"tag": "odd"}},
                                       "firmware": "other_latest"}]}


def horizon_pair_gain():
    """What two default antennas at one height on flat ground add to a pair."""
    import antennas
    return 2 * antennas.gain({"type": antennas.DEFAULT_TYPE}, 0.0, 0.0)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    for attr in ("GEODATA_DIR", "NODESETS_DIR", "SCRIPTS_DIR", "LOSSES_DIR", "RUNS_DIR",
                 "SNAPSHOTS_DIR"):
        path = tmp_path / attr.lower()
        path.mkdir()
        monkeypatch.setattr(store, attr, str(path))
    fw = tmp_path / "firmware"
    fw.mkdir()
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", str(fw))
    stub_firmware.install(fw, "stub", "20260101000000", title="Stub", hardware="ESP32")
    stub_firmware.install(fw, "stub", "20260102000000", title="Stub two", hardware="ESP32")
    stub_firmware.install(fw, "other", "1.0.0", title="Other")
    (tmp_path / "geodata_dir" / "flat").mkdir()
    (tmp_path / "geodata_dir" / "flat" / "geodata.yaml").write_text(
        "synthetic:\n  exponent: 3.0\n")
    (tmp_path / "nodesets_dir" / "three.yaml").write_text(
        "nodes:\n"
        "  a: { id: 1, lat: 0, lon: 0, tags: [transport] }\n"
        "  b: { id: 2, lat: 0, lon: 0.006, tags: [no-radio] }\n"
        "  c: { id: 3, lat: 0.006, lon: 0, tags: [far] }\n")
    (tmp_path / "scripts_dir" / "far.py").write_text('"""far"""\nfrom sim_mesh import *\n')
    (tmp_path / "scripts_dir" / "globals.py").write_text(
        "FREQ_MHZ = 869.525\nSF = 8\nBW_KHZ = 125\nCR = 5\n")
    return tmp_path


def make_simd(tmp_path, *extra):
    args = simd.parse_args([
        "--bind", "127.0.0.1:%d" % free_port(), "--ether", "127.0.0.1:%d"
        % free_port(socket.SOCK_DGRAM), "--run", str(tmp_path / "runs_dir" / "t"),
        "--stagger", "0", "--net", "127.60.0.0/22", *extra])
    daemon = simd.Simd(args)
    daemon.said = []
    daemon.broadcast = lambda message: daemon.said.append(json.loads(json.dumps(message)))
    return daemon


async def until(check, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


def lines_of(run, name):
    with open(os.path.join(run.node_dir(name), "lines")) as handle:
        return handle.read().splitlines()


def test_a_run_never_lands_on_another(tmp_path):
    base = tmp_path / "lora"
    assert simd.free_run_dir(str(base)) == str(base)
    base.mkdir()
    (base / "run.yaml").write_text("{}\n")
    assert simd.free_run_dir(str(base)) == str(base) + "-2"


def test_builds_resolve_each_name_once(stores):
    got = drivers.resolve_builds([STUB, "other_latest", STUB])
    assert got[STUB]["title"] == "Stub two" and got[STUB]["version"] == "20260102000000"
    assert got["other_latest"]["base"] == "other"
    with pytest.raises(drivers.CommandError, match="nothere"):
        drivers.resolve_builds(["nothere_latest"])


@pytest.mark.parametrize("tag, want", [
    (None, {"a": "other_latest", "c": "other_latest"}),
    ("far", {"a": STUB, "c": "other_latest"})])
def test_a_build_replaces_every_nodes_firmware_or_its_tags(stores, tag, want):
    """Whatever the rules give: every node given a firmware runs the build, or
    every one carrying its tag; a node given none still runs nothing."""
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", "build": "other_latest",
                                  "build_tag": tag, "firmware_rules": [
                                      {"which": {"names": ["a", "c"]}, "firmware": STUB}]})
        assert daemon.run.meta["build"] == "other_latest"
        assert daemon.run.meta.get("build_tag") == tag
        assert await until(lambda: daemon.firmware == want)
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_setup_is_the_name_then_the_first_boot_rules_in_order(stores):
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **FAR})
        run = daemon.run
        assert run is not None and run.bands() == ["868"] and run.script_path
        assert sorted(daemon.ether.names.values()) == ["a", "b", "c"]
        assert await until(lambda: all(
            daemon.stations.get(n) and daemon.stations[n].status == "up" for n in "abc"))
        # The name, then the rules in order: verbs through the driver, lines
        # typed as written, a transmit power of "max" and {max_dbm} the
        # node's maximum.
        radio = "radio freq_mhz=869.525 sf=8 tx_dbm=22.0"
        assert lines_of(run, "a")[:4] == ["name a 1", "role transport", radio, "radio up"]
        assert lines_of(run, "b") == ["name b 2"]                 # no-radio, no role
        assert lines_of(run, "c")[:6] == ["name c 3", radio, "far 127.60.0.7",
                                          "role transport", "txp 22", "radio up"]
        assert run.meta["first_boot_rules"] == FAR["first_boot_rules"]
        node = [m for m in daemon.said if m["type"] == "node" and m["name"] == "c"][-1]
        assert node["firmware"] == STUB and node["device_name"] == "Stub two (virtual ESP32)"
        assert node["base"] == "stub" and node["category"] == "reticulum"
        assert node["antenna"] == {"type": "whip_sma_quarter_wave"}
        assert run.meta["builds"][STUB]["firmware"] == \
            "stub_%s_20260102000000" % firmware.machine_arch()
        assert daemon.run.meta["firmware"] == {n: STUB for n in "abc"}
        for key in ("lat", "lon", "height_m", "height_from", "antenna", "tags", "role", "stale"):
            assert key in node
        assert daemon.facts("a")["role"] in ("transport", "client")   # live, else its tag
        snap = daemon.snapshot()
        assert snap["script"]["name"] == "far" and snap["geodata"]["kind"] == "synthetic"
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_a_run_records_the_medium_it_was_started_on(stores):
    """The noise figure, the rule and the seed go into the run, and the
    analysis tools' medium takes its noise figure from there."""
    async def go():
        daemon = make_simd(stores, "--noise-figure", "4.5", "--seed", "77")
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **FAR})
        run = daemon.run
        assert run.meta["physics"] == {"noise_figure_db": 4.5, "pairwise": False}
        assert run.meta["seed"] == 77
        from sim_mesh import view
        assert view.RunView(run.dir).medium().physics.noise_figure_db == 4.5
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_each_station_keeps_its_own_clock_within_the_ppm_given(stores):
    """--clock-ppm: a station's crystal is off by a draw within the bound,
    its own, the same for the same seed and name; the station's environment
    carries it in a virtual run, and a firmware's own profile still wins."""
    import types
    daemon = make_simd(stores, "--time", "max", "--clock-ppm", "20")
    daemon.ether = types.SimpleNamespace(seed=5, clock=types.SimpleNamespace(virtual=True),
                                         epoch=0)
    slopes = {}
    for name in "abc":
        text = daemon.clock_profile(name)
        (t0, n0), (t1, n1) = [tuple(map(int, point.split(":"))) for point in text.split(",")]
        assert (t0, n0) == (0, 0) and t1 == simd.CLOCK_HORIZON_US
        slopes[name] = (n1 - t1) / t1 * 1e6
        assert abs(slopes[name]) <= 20
        assert daemon.clock_profile(name) == text
    assert len({round(ppm, 3) for ppm in slopes.values()}) == 3
    assert simd.drift_profile(-20) == "0:0,%d:%d" % (simd.CLOCK_HORIZON_US,
                                                      simd.CLOCK_HORIZON_US - 51_840_000)

    station = types.SimpleNamespace(node_id=1, dir="d", addr="a", ether_addr="e",
                                    clock=daemon.ether, board=None,
                                    clock_profile=daemon.clock_profile("a"))
    assert drivers.env(station, {})["SIM_MESH_CLOCK_PROFILE"] == daemon.clock_profile("a")
    own = drivers.env(station, {"env": {"SIM_MESH_CLOCK_PROFILE": "0:0,1:2"}})
    assert own["SIM_MESH_CLOCK_PROFILE"] == "0:0,1:2"
    daemon.args.clock_ppm = 0
    assert daemon.clock_profile("a") is None
    station.clock_profile = None
    assert "SIM_MESH_CLOCK_PROFILE" not in drivers.env(station, {})


def test_a_drifting_clock_needs_virtual_time():
    with pytest.raises(SystemExit):
        simd.parse_args(["--clock-ppm", "20"])
    with pytest.raises(SystemExit):
        simd.parse_args(["--time", "max", "--clock-ppm", "-1"])


def test_moves_offsets_ids_and_levels(stores):
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **STUBS})
        run = daemon.run
        assert await until(lambda: len(daemon.stations) == 3)
        before = daemon.tables["868"].get("a", "b")
        gain = horizon_pair_gain()
        await daemon.do_nodeset_move({"name": "b", "lat": 0.0, "lon": 0.03})
        assert "b" in daemon.stale
        assert [m for m in daemon.said if m["type"] == "node" and m["name"] == "b"][-1]["stale"]
        assert await until(lambda: not daemon.stale)
        after = daemon.tables["868"].get("a", "b")
        assert after > before + 10
        assert slt.Table.read(run.table_path("868")).get("a", "b") == pytest.approx(after)
        # The medium's table has the antennas on it, the run's is the model's own.
        assert daemon.ether.tables["868"].get("b", "a") == pytest.approx(after - gain, abs=0.01)
        moves = [e for e in run.edits() if e["what"] == "move"]
        assert moves and moves[-1]["node"] == "b" and "t" in moves[-1]
        assert run.nodeset().node("b")["lon"] == 0.03

        await daemon.do_nodeset_offset({"between": ["a", "b"], "db": 30, "note": "wall"})
        assert daemon.tables["868"].get("a", "b") == pytest.approx(after)
        assert daemon.ether.tables["868"].get("a", "b") == pytest.approx(after + 30 - gain,
                                                                         abs=0.01)

        # A yagi aimed away from b takes its back's floor off the pair; aimed
        # at it (b is due east of a), its peak.
        await daemon.do_nodeset_set({"name": "a", "antenna": {"type": "yagi_directional",
                                                              "azimuth_deg": 270}})
        away = daemon.ether.tables["868"].get("a", "b")
        await daemon.do_nodeset_set({"name": "a", "antenna": {"type": "yagi_directional",
                                                              "azimuth_deg": 90}})
        toward = daemon.ether.tables["868"].get("a", "b")
        assert away - toward == pytest.approx(18.0, abs=0.1)
        assert daemon.tables["868"].get("a", "b") == pytest.approx(after)
        await daemon.do_nodeset_set({"name": "a", "antenna": {"type": "whip_sma_quarter_wave"}})
        assert run.nodeset().offsets == [{"between": ["a", "b"], "db": 30.0, "note": "wall"}]

        await daemon.do_levels({"name": "a"})
        levels = [m for m in daemon.said if m["type"] == "levels"][-1]
        assert levels["freq"] == pytest.approx(869.525e6) and "c" in levels["heard"]

        await daemon.do_nodeset_set({"name": "c", "id": 9})
        assert daemon.ether.names.get(9) == "c" and 3 not in daemon.ether.names
        assert any(m["type"] == "notice" and "id 9" in m["text"] for m in daemon.said)
        assert await until(lambda: daemon.stations["c"].node_id == 9)
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_an_edit_with_a_number_no_file_could_hold_is_refused(stores):
    """JSON reads NaN, Infinity and 1e999 as numbers, and store.scalar cannot
    write them. An edit carrying one went into the run's nodeset, which could
    then not be saved, and an infinite id or height took the page's socket
    down with an OverflowError. Each is refused as a bad edit is, with the
    key it names, and leaves the nodeset as it was."""
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three"})    # nothing runs
        before = copy.deepcopy(daemon.nodeset.data)
        for text, match in (
                ('{"type": "nodeset_move", "name": "b", "lat": NaN, "lon": 0.03}',
                 "node b: lat is a finite number, not nan"),
                ('{"type": "nodeset_move", "name": "b", "lat": 0, "lon": -Infinity, '
                 '"settle": false}', "node b: lon is a finite number, not -inf"),
                ('{"type": "nodeset_move", "name": "b", "lat": 0, "lon": 0.006, '
                 '"height_m": 1e999}', "node b: height_m is a finite number, not inf"),
                ('{"type": "nodeset_add", "name": "d", "lat": NaN, "lon": 0.001}',
                 "node d: lat is a finite number, not nan"),
                ('{"type": "nodeset_add", "name": "d", "lat": 0.001, "lon": 0.001, '
                 '"height_m": Infinity}', "node d: height_m is a finite number, not inf"),
                ('{"type": "nodeset_add", "name": "d", "lat": 0.001, "lon": 0.001, '
                 '"id": Infinity}', "node d: id is a finite number, not inf"),
                ('{"type": "nodeset_set", "name": "a", "lat": NaN}',
                 "node a: lat is a finite number, not nan"),
                ('{"type": "nodeset_set", "name": "a", "id": -Infinity}',
                 "node a: id is a finite number, not -inf"),
                ('{"type": "nodeset_set", "name": "a", "antenna": {"type": "yagi_directional", '
                 '"elevation_deg": NaN}}', "node a: an antenna's elevation_deg is a finite"),
                ('{"type": "nodeset_set", "name": "c", "max_dbm": Infinity}',
                 "node c: max_dbm is -9 to 27 dBm, not inf"),
                ('{"type": "nodeset_offset", "between": ["a", "b"], "db": NaN}',
                 "the offset between a and b: db is a finite number, not nan")):
            said = len(daemon.said)
            await daemon.handle(json.loads(text))       # as the page's socket reads it
            errors = [m["text"] for m in daemon.said[said:] if m["type"] == "error"]
            assert len(errors) == 1 and match in errors[0], (text, errors)
            assert daemon.nodeset.data == before, text
        # Nothing no file could hold went in: the next edit is written.
        await daemon.handle({"type": "nodeset_offset", "between": ["a", "b"], "db": 3})
        assert daemon.run.nodeset().offsets == [{"between": ["a", "b"], "db": 3.0}]
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_a_request_that_fails_is_answered_with_its_error(stores):
    """A script waits on each request's answer, by its id. One that cannot be
    done is answered with the error, and the page told as before; left
    unanswered, the script waited for ever, and T went on without it."""
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **STUBS})
        assert await until(lambda: len(daemon.stations) == 3 and all(
            s.status == "up" for s in daemon.stations.values()))
        for msg in ({"type": "meta", "verb": "lxmf.send", "name": "a",
                     "args": {"to": "nobody", "text": "yo"}, "id": "f1"},
                    # Put off to an instant, it fails on a task of its own.
                    {"type": "meta", "verb": "lxmf.send", "name": "a", "after": 0.001,
                     "args": {"to": "nobody", "text": "yo"}, "id": "f3"}):
            said = len(daemon.said)
            await daemon.handle(msg)
            assert await until(lambda: any(m.get("id") == msg["id"] for m in daemon.said[said:]))
            answers = [m for m in daemon.said[said:] if m["type"] == "command_result"]
            errors = [m["text"] for m in daemon.said[said:] if m["type"] == "error"]
            assert [a["id"] for a in answers] == [msg["id"]], msg
            assert answers[0]["error"] == errors[0] and answers[0]["results"] == {}, msg
            assert "named nobody" in errors[0], msg
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_commands_and_verbs_go_to_the_stations_chosen(stores):
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **STUBS})
        assert await until(lambda: len(daemon.stations) == 3 and all(
            s.status == "up" for s in daemon.stations.values()))

        await daemon.do_command({"line": "hello {name}", "tag": "far", "id": "x1"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["id"] == "x1" and got["results"] == {"c": "did hello c"}

        await daemon.do_command({"line": "hi", "names": ["a", "b"], "id": "x2"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert sorted(got["results"]) == ["a", "b"]

        await daemon.do_meta({"verb": "lxmf.announce", "id": "x3"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["verb"] == "lxmf.announce" and got["results"] == {
            n: "did announce" for n in "abc"}

        await daemon.do_meta({"verb": "lxmf.send", "name": "a",
                              "args": {"to": "b", "text": "yo"}, "id": "x4"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        mid = got["results"]["a"]
        assert mid.startswith("a.")         # sim-mesh's id: the sender and T
        # What the station said under its own id, before it was coupled to
        # sim-mesh's, is in the run's events under sim-mesh's, at T.
        with open(os.path.join(daemon.run.dir, "events.jsonl")) as handle:
            events = [json.loads(line) for line in handle]
        assert events[-1]["node"] == "a" and events[-1]["mid"] == mid
        assert events[-1]["event"] == "lxmf.message.status"
        assert events[-1]["status"] == "delivered"
        assert isinstance(events[-1]["t"], int)

        await daemon.do_meta({"verb": "path", "name": "a", "args": {"to": "b"}, "id": "x5"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["results"]["a"] == "! Stub two has no way to path"

        await daemon.do_meta({"verb": "lxmf.identities", "id": "x6"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["results"]["c"] == [["c", "%032x" % 3]]

        # An identity named: a sender and a recipient by that name, and a
        # name no other identity or node may take.
        await daemon.do_meta({"verb": "lxmf.create", "name": "c", "args": {"name": "carol"},
                              "id": "x6a"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        carol = got["results"]["c"]
        await daemon.do_meta({"verb": "lxmf.send", "args": {"from": "carol", "to": "a",
                                                           "text": "hey"}, "id": "x6b"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert list(got["results"]) == ["c"] and got["results"]["c"].startswith("c.")
        assert lines_of(daemon.run, "c")[-1] == "send carol %032x hey" % 1
        await daemon.do_meta({"verb": "lxmf.send", "name": "a",
                              "args": {"to": "carol", "text": "ho"}, "id": "x6c"})
        assert lines_of(daemon.run, "a")[-1] == "send - %s ho" % carol
        for who, name in (("b", "carol"), ("a", "b")):
            await daemon.do_meta({"verb": "lxmf.create", "name": who, "args": {"name": name},
                                  "id": "x6d"})
            got = [m for m in daemon.said if m["type"] == "command_result"][-1]
            assert "would not be unique" in got["results"][who]

        # A sequence: its steps one after the other at one T, answered once.
        await daemon.do_sequence({"steps": [{"type": "command", "line": "first {name}"},
                                            {"type": "meta", "verb": "lxmf.send",
                                             "args": {"to": "b", "text": "then"}}],
                                  "names": ["a"], "id": "x8"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["id"] == "x8" and got["results"][0] == {"a": "did first a"}
        assert got["results"][1]["a"].startswith("a.")
        assert lines_of(daemon.run, "a")[-2:] == ["first a", "send - %032x then" % 2]

        # A category's verb is for its stations; one that leaves none of those
        # chosen is answered as an error, so whoever asked does not wait on it.
        await daemon.do_meta({"verb": "lxmf.announce", "category": "reticulum",
                              "names": ["a"], "id": "x9"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["results"] == {"a": "did announce"}
        await daemon.do_meta({"verb": "lxmf.announce", "category": "meshcore",
                              "names": ["a"], "id": "x10"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["id"] == "x10" and "runs meshcore firmware" in got["error"]
        with pytest.raises(ValueError, match="no such verb 'lxmf.send'"):
            daemon.check_first_boot({"which": {"all": True}, "lines": [
                {"verb": "lxmf.send", "args": {}}]})
        with pytest.raises(ValueError, match="no firmware category"):
            daemon.check_first_boot({"which": {"all": True}, "lines": [
                {"verb": "role", "args": {}, "category": "nosuch"}]})
        assert daemon.check_first_boot({"which": {"all": True}, "lines": [
            {"verb": "role", "args": {"role": "router"}, "category": "meshtastic"}]})["lines"] == [
            {"verb": "role", "args": {"role": "router"}, "category": "meshtastic"}]
        with pytest.raises(ValueError, match="no such verb 'role' for meshcore firmware"):
            daemon.check_first_boot({"which": {"all": True}, "lines": [
                {"verb": "role", "args": {}, "category": "meshcore"}]})

        # The scripts' log level: kept in the run, in the snapshot, said to all.
        assert daemon.snapshot()["script_loglevel"] == "output"
        await daemon.do_script_loglevel({"level": "commands"})
        assert daemon.run.meta["script_loglevel"] == "commands"
        assert daemon.said[-1] == {"type": "script_loglevel", "level": "commands"}
        with pytest.raises(ValueError, match="one of output"):
            await daemon.do_script_loglevel({"level": "loud"})

        # A line is one firmware's language: a mixed choice is refused unless narrowed.
        await daemon.do_nodeset_add({"name": "d", "lat": 0.001, "lon": 0.001, "tags": ["odd"]})
        assert await until(lambda: daemon.stations.get("d") and
                           daemon.stations["d"].status == "up")
        with pytest.raises(drivers.CommandError, match="say which base"):
            await daemon.do_command({"line": "x"})
        await daemon.do_command({"line": "only", "base": "other", "id": "x7"})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["results"] == {"d": "did only"}
        assert daemon.run.meta["builds"]["other_latest"]["base"] == "other"
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_meshcore_verbs_name_a_contact_and_mint_a_message_id(stores):
    """`msg` and `chan` of the meshcore category are answered with
    sim-mesh's id, which the driver puts in the text; `to` is the contact's
    name, passed as it is; a reticulum station is no target of them."""
    stub_firmware.install(stores / "firmware", "mc", "20260101000000",
                          driver=stub_firmware.MESHCORE_DRIVER, category="meshcore")

    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", "firmware_rules": [
            ALL_STUB, {"which": {"names": ["a", "c"]}, "firmware": "mc_latest"}]})
        assert await until(lambda: len(daemon.stations) == 3 and all(
            s.status == "up" for s in daemon.stations.values()))

        async def meta(verb, **msg):
            await daemon.do_meta(dict(msg, verb=verb, category="meshcore", id=verb))
            return [m for m in daemon.said if m["type"] == "command_result"][-1]

        got = await meta("msg", name="a", args={"to": "c", "text": "yo"})
        mid = got["results"]["a"]
        assert mid.startswith("a.")
        assert lines_of(daemon.run, "a")[-1] == "msg c yo #%s" % mid
        got = await meta("chan", name="c", args={"nb": 0, "text": "all"})
        assert lines_of(daemon.run, "c")[-1] == "chan 0 all #%s" % got["results"]["c"]
        assert (await meta("path", name="c", args={"to": "a"}))["results"] == {"c": ["ab"]}
        got = await meta("msg", name="a", args={"text": "nobody"})
        assert "msg needs `to`" in got["error"]
        got = await meta("repeat", args={"on": True})
        assert sorted(got["results"]) == ["a", "c"]           # b runs reticulum firmware
        with open(os.path.join(daemon.run.dir, "events.jsonl")) as handle:
            events = [json.loads(line) for line in handle]
        assert [(e["event"], e["mid"], e["status"]) for e in events if e["node"] == "a"] == [
            ("msg.status", mid, "sent")]
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_meshtastic_verbs_name_a_node_and_mint_a_message_id(stores):
    """`sendtext` of the meshtastic category is answered with sim-mesh's id,
    which the driver puts in the text; `to` is a node's name, passed as it
    is, and without it the text goes to the channel; `traceroute` needs it."""
    stub_firmware.install(stores / "firmware", "mt", "20260101000000",
                          driver=stub_firmware.MESHTASTIC_DRIVER, category="meshtastic")

    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", "firmware_rules": [
            ALL_STUB, {"which": {"names": ["a", "c"]}, "firmware": "mt_latest"}]})
        assert await until(lambda: len(daemon.stations) == 3 and all(
            s.status == "up" for s in daemon.stations.values()))

        async def meta(verb, **msg):
            await daemon.do_meta(dict(msg, verb=verb, category="meshtastic", id=verb))
            return [m for m in daemon.said if m["type"] == "command_result"][-1]

        got = await meta("sendtext", name="a", args={"to": "c", "text": "yo"})
        mid = got["results"]["a"]
        assert mid.startswith("a.")
        assert lines_of(daemon.run, "a")[-1] == "sendtext c 0 yo #%s" % mid
        got = await meta("sendtext", name="c", args={"text": "all", "ch_index": 1})
        assert lines_of(daemon.run, "c")[-1] == "sendtext ^all 1 all #%s" % got["results"]["c"]
        got = await meta("traceroute", name="c", args={"to": "a"})
        assert got["results"]["c"]["route"] == ["a"]
        got = await meta("traceroute", name="a", args={})
        assert "traceroute needs `to`" in got["error"]
        got = await meta("role", args={"role": "router"})
        assert sorted(got["results"]) == ["a", "c"]           # b runs reticulum firmware
        assert lines_of(daemon.run, "a")[-1] == "role router"
        with open(os.path.join(daemon.run.dir, "events.jsonl")) as handle:
            events = [json.loads(line) for line in handle]
        assert [(e["event"], e["mid"], e["status"]) for e in events if e["node"] == "a"] == [
            ("msg.status", mid, "sent")]
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_firmware_rules_start_restart_and_leave_idle(stores):
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three"})
        # No script, no rule: nothing runs.
        await asyncio.sleep(0.2)
        assert daemon.stations == {} and daemon.firmware == {}
        node = next(n for n in daemon.snapshot()["nodes"] if n["name"] == "a")
        assert node["firmware"] is None and node["status"] == "stopped"

        await daemon.do_firmware({"id": "f1", "rules": [
            {"which": {"where": {"tag": ["far"]}}, "firmware": STUB}]})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["id"] == "f1" and got["results"] == {"c": STUB}
        assert await until(lambda: daemon.stations.get("c") and
                           daemon.stations["c"].status == "up")
        assert set(daemon.stations) == {"c"}

        await daemon.do_firmware({"id": "f2", "rules": [
            {"which": {"all": True}, "firmware": STUB},
            {"which": {"names": ["c"]}, "firmware": "other_latest"}]})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["results"] == {"a": STUB, "b": STUB, "c": "other_latest"}
        assert await until(lambda: len(daemon.stations) == 3 and all(
            s.status == "up" for s in daemon.stations.values()))
        assert daemon.driver_of("c").firmware["base"] == "other"
        assert any(m["type"] == "notice" and "c now runs" in m["text"] for m in daemon.said)
        assert daemon.run.meta["firmware_rules"][0]["firmware"] == STUB

        await daemon.do_firmware({"id": "f3", "rules": [{"which": {"bogus": 1},
                                                         "firmware": STUB}]})
        got = [m for m in daemon.said if m["type"] == "command_result"][-1]
        assert got["id"] == "f3" and "selection" in got["error"]
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_roles_are_asked_only_for_a_page_that_draws_them(stores, monkeypatch):
    """A role question runs a tool against the station's console: it is asked
    only while a page that draws the map is open and a station is up. Until
    then T does not move for it, so a virtual run's T stays where the load
    left it until its stations start."""
    asked = []

    async def read_role(self, station):
        asked.append(station.name)
    monkeypatch.setattr(simd.Simd, "read_role", read_role)
    monkeypatch.setattr(simd, "ROLE_POLL_S", 0.05)

    async def unwatched_virtual():
        daemon = make_simd(stores, "--time", "max")
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three"})
        daemon.pages["page"] = False
        daemon.poller = asyncio.ensure_future(daemon.poll_roles())
        await asyncio.sleep(0.3)
        assert daemon.ether.now() == 0
        daemon.poller.cancel()
        daemon.ether.close()
    asyncio.run(unwatched_virtual())

    async def watched_or_not():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **FAR})
        assert await until(lambda: len(daemon.stations) == 3 and all(
            s.status == "up" for s in daemon.stations.values()))
        daemon.pages["driver"] = True
        daemon.poller = asyncio.ensure_future(daemon.poll_roles())
        await asyncio.sleep(0.3)
        asked.clear()
        await asyncio.sleep(0.3)
        assert asked == []
        daemon.pages["page"] = False
        daemon.role_wake.set()
        assert await until(lambda: set(asked) == {"a", "b", "c"})
        del daemon.pages["page"]
        await asyncio.sleep(0.2)
        asked.clear()
        await asyncio.sleep(0.3)
        assert asked == []
        daemon.poller.cancel()
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(watched_or_not())


def test_a_console_nothing_acts_on_is_not_waited_for(tmp_path, monkeypatch):
    """The drain before T moves reads only the consoles of firmwares whose
    drivers act on what their stations print (console_acted_on, by
    default): one whose console is log lines alone is read as it comes, and
    a barrier where only it printed waits for nothing."""
    import stations
    import types

    daemon = make_simd(tmp_path)
    assert ReticulumDriver.console_acted_on

    pipes = []

    def printing(node_id, acted_on):
        reader, writer = os.pipe()
        pipes.extend((reader, writer))
        marks = stations.Marks(reader)
        marks.reading, marks.taken = True, 1         # handed on reads not caught up with
        drain = types.SimpleNamespace(master=reader, marks=marks)
        driver = types.SimpleNamespace(console_acted_on=acted_on,
                                       sids=lambda station: (station.node_id,))
        return types.SimpleNamespace(node_id=node_id, drain=drain, driver=driver)
    daemon.stations = {"ours": printing(1, False), "theirs": printing(2, True)}
    waited = []
    monkeypatch.setattr(stations.ptys(), "call",
                        lambda fn, drains, loop, done: waited.extend(drains))

    async def go():
        assert daemon.drain_consoles([1], lambda: None) is False
        assert daemon.drain_consoles([1, 2], lambda: None) is True
    try:
        asyncio.run(go())
    finally:
        for fd in pipes:
            os.close(fd)
    assert waited == [daemon.stations["theirs"].drain]


@pytest.mark.parametrize("marks", ["testbed", "core"])
def test_only_a_station_that_printed_is_read_through_the_pty_thread(marks):
    """stations.printed polls the ptys on the main thread: a station that
    printed is found, one that did not is not, and bytes the pty thread has
    taken and handed on count until a catch_up has seen them through. The
    same with the marks an ether core keeps."""
    import stations
    import types

    module = None
    if marks == "core":
        module = simd.ether_module.core_module()
        if module is None:
            pytest.skip("no ether core built (sim builds it when it starts)")

    async def go():
        main = asyncio.get_running_loop()
        master, slave = os.openpty()
        os.set_blocking(master, False)
        got = []
        station = types.SimpleNamespace(log_file=open(os.devnull, "wb"), watchers=0,
                                        hears_lines=False,
                                        pty_closed=lambda drain: None,
                                        console_out=lambda text: None)
        rpc = types.SimpleNamespace(on_marker=lambda: None,
                                    on_frame=lambda fid, payload: got.append(payload))
        drain = stations.Drain(station, master, rpc, main,
                               module.Marks(master) if module is not None else None)
        drain.marks.reading = True              # read only when asked, below
        try:
            assert stations.printed([drain]) == []
            os.write(slave, b"hello\n")
            assert stations.printed([drain]) == [drain]
            # The pty thread takes it on its own; until a catch_up has seen
            # it through, it still counts.
            done = main.create_future()
            stations.ptys().call(lambda: (drain.read_once(),
                                          main.call_soon_threadsafe(done.set_result, None)))
            await done
            assert drain.marks.taken == 1 and drain.marks.caught == 0
            assert stations.printed([drain]) == [drain]
            finished = main.create_future()
            stations.ptys().call(stations.catch_up, [drain], main,
                                 lambda: finished.set_result(None))
            await finished
            assert stations.printed([drain]) == []
        finally:
            os.close(slave)
            os.close(master)
            station.log_file.close()
    asyncio.run(go())


def test_a_console_that_need_not_be_a_terminal_is_a_pipe_each_way(tmp_path):
    """A driver with console_tty False: the station's stdin and stdout are
    pipes, not a pty; what it prints reaches its log, what is typed at it
    reaches it."""
    import stat as stat_module
    import stations

    script = tmp_path / "echo.sh"
    script.write_text("#!/bin/sh\necho hello\nwhile read line; do echo \"got $line\"; done\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    class Piped(ReticulumDriver):
        console_tty = False

        def configured(self, station):
            return False

    driver = Piped({"firmware": "piped", "exec": str(script), "env": {}})

    async def go():
        station = stations.Station("p", 7, str(tmp_path / "p"), driver, "127.0.0.1:9")
        os.makedirs(station.dir, exist_ok=True)
        await station.start()
        try:
            assert stat_module.S_ISFIFO(os.fstat(station.master).st_mode)
            assert stat_module.S_ISFIFO(os.fstat(station.drain.master).st_mode)
            station.write(b"ping\n")
            text = lambda: open(station.log_path, "rb").read()
            assert await until(lambda: b"got ping" in text())
            assert b"hello" in text()
        finally:
            await station.stop()

    asyncio.run(go())


def test_a_driver_takes_turns_with_t(stores):
    """A socket that says `drive` holds T while it has the floor: from then,
    and from each answer to it, until it yields. Its waits are answered at
    the instant they end, with the floor."""
    import aiohttp
    from aiohttp import web

    async def go():
        daemon = make_simd(stores, "--time", "max")
        daemon.broadcast = simd.Simd.broadcast.__get__(daemon)     # the sockets hear it
        await daemon.start_ether()
        runner = web.AppRunner(daemon.app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three"})
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect("http://127.0.0.1:%d/ws?quiet=1" % port) as ws:
                await ws.receive()                          # the snapshot
                daemon.ether.call_at(5_000_000, lambda: None)
                await ws.send_json({"type": "drive"})
                await asyncio.sleep(0.2)
                assert daemon.ether.now() == 0              # the driver has the floor
                await ws.send_json({"type": "wait", "until": 3_000_000, "id": "w1"})
                await asyncio.sleep(0.2)
                assert daemon.ether.now() == 0              # still: it has not yielded
                await ws.send_json({"type": "yield"})
                while True:
                    msg = json.loads((await ws.receive()).data)
                    if msg.get("type") == "command_result" and msg.get("id") == "w1":
                        break
                assert msg["t"] == 3_000_000
                await asyncio.sleep(0.2)
                assert daemon.ether.now() == 3_000_000      # answered: the floor is back
                await ws.send_json({"type": "yield"})
                await asyncio.sleep(0.2)
                assert daemon.ether.now() == 5_000_000
        await asyncio.sleep(0.1)
        assert daemon.driver is None and not daemon.driver_floor
        await runner.cleanup()
        daemon.ether.close()
    asyncio.run(go())


def test_a_virtual_runs_stations_leave_t_together_before_any_is_stopped(stores, monkeypatch):
    """stop_all in a virtual run: one callback at T's next barrier (now, before
    the stations' own events of that T) has every station leave T, marked
    stopping so none is started again, before any process is stopped. With no
    barrier in time they are stopped anyway, and a late one does nothing."""
    import types
    monkeypatch.setattr(simd, "SHUTDOWN_TIMEOUT_S", 0.05)

    async def go(barrier_comes):
        daemon = make_simd(stores, "--time", "max")
        due, left, stopped = [], [], []
        daemon.ether = types.SimpleNamespace(
            clock=types.SimpleNamespace(virtual=True), now=lambda: 7_000_000, kick=lambda: None,
            call_at=lambda t, callback, key=0: due.append((t, key, callback)))

        def station(name):
            one = types.SimpleNamespace(name=name, stopping=False)
            one.leave = lambda: left.append((name, one.stopping, list(stopped)))
            return one
        daemon.stations = {name: station(name) for name in "ab"}

        async def stop_station(name, flush=True):
            stopped.append(name)
            daemon.stations.pop(name)
        daemon.stop_station = stop_station
        stopping = asyncio.ensure_future(daemon.stop_all(flush=False))
        await asyncio.sleep(0)
        assert [(t, key) for t, key, _ in due] == [(7_000_000, -1)] and not left
        if barrier_comes:
            due[0][2]()
        await stopping
        assert stopped == ["a", "b"]
        due[0][2]()                                 # late, or again: nothing
        return left
    assert asyncio.run(go(True)) == [("a", True, []), ("b", True, [])]
    assert asyncio.run(go(False)) == []


def test_a_node_powered_off_stays_off_and_boots_from_its_state_when_powered_on(stores):
    """node_stop: the station stops and is not started again, the node stays
    in the nodeset and the medium, and the run's events say so; node_start:
    it boots with the state it had, so it is not set up again. A start of a
    running one and a stop of a stopped one do nothing."""
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **FAR})
        run = daemon.run
        assert await until(lambda: all(
            daemon.stations.get(n) and daemon.stations[n].status == "up" for n in "abc"))
        setup = lines_of(run, "a")
        await daemon.do_node_stop({"name": "a", "id": "off"})
        assert "a" not in daemon.stations and "a" in daemon.nodeset.nodes
        assert "a" in daemon.ether.names.values()
        said = [m for m in daemon.said if m.get("name") == "a"]
        assert said[-2]["type"] == "node" and said[-2]["status"] == "stopped"
        assert said[-1]["type"] == "command_result" and said[-1]["results"] == {"a": "off"}
        await asyncio.sleep(0.3)
        assert "a" not in daemon.stations                  # no supervisor brings it back
        await daemon.do_node_stop({"name": "a"})            # off already: nothing
        await daemon.do_node_start({"name": "a", "id": "on"})
        assert await until(lambda: daemon.stations.get("a") and
                           daemon.stations["a"].status == "up")
        assert lines_of(run, "a") == setup                  # its state kept: not set up again
        await daemon.do_node_start({"name": "a"})           # running: left as it is
        assert daemon.stations["a"].starts == 1
        with open(os.path.join(run.dir, simd.EVENTS_FILE)) as handle:
            power = [json.loads(line) for line in handle if '"power"' in line]
        assert [(e["node"], e["on"]) for e in power] == [("a", False), ("a", True)]
        with pytest.raises(store.StoreError):                # refused, as any unknown node
            await daemon.do_node_stop({"name": "nobody"})
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_a_node_powered_off_in_a_virtual_run_leaves_t_at_the_next_barrier(stores, monkeypatch):
    """node_stop in a virtual run: the station leaves T in one callback at T's
    next barrier (now, before the stations' own events of that T), marked
    stopping, and only then is its process stopped; the others stay."""
    import types

    async def go():
        daemon = make_simd(stores, "--time", "max")
        due, left, stopped = [], [], []
        daemon.ether = types.SimpleNamespace(
            clock=types.SimpleNamespace(virtual=True), now=lambda: 7_000_000, kick=lambda: None,
            call_at=lambda t, callback, key=0: due.append((t, key, callback)))
        daemon.nodeset = types.SimpleNamespace(node=lambda name: {}, nodes={})
        daemon.run = object()
        daemon.station_event = lambda station, event, fields: None
        daemon.node_message = lambda name: {"type": "node", "name": name}

        def station(name):
            one = types.SimpleNamespace(name=name, stopping=False)
            one.leave = lambda: left.append((name, one.stopping, list(stopped)))
            return one
        daemon.stations = {name: station(name) for name in "ab"}

        async def stop_station(name, flush=True):
            stopped.append((name, flush))
            daemon.stations.pop(name)
        daemon.stop_station = stop_station
        stopping = asyncio.ensure_future(daemon.do_node_stop({"name": "a"}))
        await asyncio.sleep(0)
        assert [(t, key) for t, key, _ in due] == [(7_000_000, -1)] and not left
        due[0][2]()
        await stopping
        return left, stopped, sorted(daemon.stations)
    assert asyncio.run(go()) == ([("a", True, [])], [("a", False)], ["b"])


def test_a_role_the_station_forgets_is_said_again_after_a_reset(stores, monkeypatch):
    monkeypatch.setattr(ReticulumDriver, "role_volatile", True)

    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **FAR})
        assert await until(lambda: daemon.stations.get("a") and daemon.stations["a"].status == "up")
        run = daemon.run
        assert lines_of(run, "a").count("role transport") == 1
        await daemon.do_node_reset({"name": "a"})
        # The role verb of its first-boot rules again, and nothing else of them.
        assert await until(lambda: lines_of(run, "a").count("role transport") == 2)
        assert lines_of(run, "a").count("name a 1") == 1          # not set up again
        assert lines_of(run, "a").count("radio up") == 1
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_a_snapshot_reloads_what_the_run_had(stores):
    async def go():
        daemon = make_simd(stores)
        await daemon.start_ether()
        await daemon.do_sim_load({"geodata": "flat", "nodeset": "three", **FAR})
        assert await until(lambda: len(daemon.stations) == 3)
        await daemon.do_nodeset_move({"name": "a", "lat": 0.02, "lon": 0.0})
        assert await until(lambda: not daemon.stale)
        await daemon.do_snapshot_save_as({"name": "moved"})
        first = daemon.run.dir
        await daemon.do_snapshot_load({"name": "moved"})
        assert daemon.run.dir == first + "-2"
        assert daemon.nodeset.node("a")["lat"] == 0.02
        assert daemon.first_boot == FAR["first_boot_rules"] and daemon.rules == [ALL_STUB]
        assert daemon.tables["868"].get("a", "b") == pytest.approx(
            slt.Table.read(os.path.join(first, "losses", "868.bin")).get("a", "b"))
        await daemon.stop_all(flush=False)
        daemon.ether.close()
    asyncio.run(go())


def test_a_tool_that_ends_as_its_wait_times_out_is_a_tool_that_gave_no_answer(monkeypatch):
    """A tool's answer is waited for on the wall clock; when the wait times out
    in the instant the tool itself ends, there is no process left to kill, and
    that is the same failure as any other late answer, not another one."""
    class Late:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            raise ProcessLookupError()

        async def wait(self):
            self.returncode = 0
            return 0

    async def spawn(*argv, **kw):
        return Late()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    from sim_mesh import driver
    with pytest.raises(driver.CommandError, match="gave no answer"):
        asyncio.run(driver.run_tool(["/bin/true"], 0.05))
