"""The front's routing, its estimate, the WebRTC relay one level up, the
loss tables before a child starts, the editors and imports over its own
socket and HTTP, coverage through a planner of the test's own, and the
planner sidecar behind /planner."""

import asyncio
import hashlib
import io
import json
import os
import pathlib
import shutil
import socket
import struct
import sys
import time
import zipfile

import aiohttp
import pytest
import yaml
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import coverage  # noqa: E402
import firmware  # noqa: E402
import front  # noqa: E402
import geodata  # noqa: E402
import nodeset  # noqa: E402
import proxy  # noqa: E402
import runs  # noqa: E402
import sources  # noqa: E402
import store  # noqa: E402
import webrtc  # noqa: E402

BERLIN_PACK = os.path.join(store.GEODATA_DIR, "berlin-city")
FOUR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "four.yaml")
PLAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "plain-27.yaml")


class Args:
    public_port = "8800"
    child_args = []
    bind = "127.0.0.1:0"
    relay_host = "127.0.0.1"
    relay_port = 0
    page_dev = None


def make_front():
    return front.Front(Args())


def make_child(f, name, state="running", port=9100):
    child = front.Child(f, name, port, 7100, "127.16.0.0/22", "max", None)
    child.state = state
    f.children[name] = child
    return child


# ---- hostnames -------------------------------------------------------------

def test_a_label_is_a_station_or_a_station_of_a_simulation():
    assert proxy.label_of(b"alpha.sim.localhost:8800") == "alpha"
    assert proxy.label_of(b"Alpha.Lora.sim.localhost") == "alpha.lora"
    assert proxy.label_of(b"a.b.c.sim.localhost") is None
    assert proxy.label_of(b"localhost:8800") is None
    assert proxy.resolve_by_id("3.lora")[1] == proxy.STATION_PORT


def test_the_suffix_away_from_localhost_is_not_the_proxys_business():
    # A remote browser's own "localhost" is not the server's, so the page
    # names a wildcard-DNS suffix for the server's real address instead
    # (nip.io, say); the proxy only cares that `.sim.` sits right after the
    # label, whatever comes after that.
    assert proxy.label_of(b"alpha.sim.192.0.2.10.nip.io:8812") == "alpha"
    assert proxy.label_of(b"Alpha.Lora.sim.203.0.113.9.nip.io") == "alpha.lora"
    assert proxy.label_of(b"a.b.c.sim.192.0.2.10.nip.io") is None
    assert proxy.label_of(b"192.0.2.10:8812") is None


def test_the_second_label_picks_the_simulation():
    async def go():
        f = make_front()
        f.control_port = 1234
        make_child(f, "lora", port=9100)
        make_child(f, "supe", port=9101)
        assert f.resolve_host("alpha.supe", "/") == ("127.0.0.1", 9101)
        assert f.resolve_host("alpha.lora", "/x") == ("127.0.0.1", 9100)
        # Signalling stays with the front, which points the answer at itself.
        assert f.resolve_host("alpha.lora", "/webrtc?x=1") == ("127.0.0.1", 1234)
        assert f.resolve_host(None, "/") == ("127.0.0.1", 1234)
        refused = f.resolve_host("alpha.nope", "/")
        assert isinstance(refused, proxy.Refusal) and "nope" in refused.text
        # Bare names are ambiguous with two running, and say which exist.
        refused = f.resolve_host("alpha", "/")
        assert isinstance(refused, proxy.Refusal) and "lora, supe" in refused.text
        f.children["supe"].state = "exited"
        assert f.resolve_host("alpha", "/") == ("127.0.0.1", 9100)
        assert isinstance(f.resolve_host("alpha.supe", "/"), proxy.Refusal)
    asyncio.run(go())


def test_allocation_steps_around_live_children_and_reuses_exited_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(front, "NET_LOCK_DIR", str(tmp_path / "nets"))

    async def go():
        f = make_front()
        first = f.allocate()
        child = front.Child(f, "a", first[0], first[1], first[2], "real", None)
        f.children["a"] = child
        second = f.allocate()
        assert second[0] != first[0] and second[1] != first[1] and second[2] != first[2]
        child.state = "exited"
        assert f.allocate()[2] == first[2]
        assert f.free_name("City99 LoRa") == "city99-lora"
        assert f.free_name("a") == "a-2"
    asyncio.run(go())


def test_two_fronts_on_one_host_never_give_out_the_same_network(tmp_path, monkeypatch):
    """Neither front's stations have bound anything yet, so only the
    host-wide lock keeps them apart; the block comes back when the
    simulation holding it ends."""
    monkeypatch.setattr(front, "NET_LOCK_DIR", str(tmp_path / "nets"))
    monkeypatch.setattr(front, "bound_addresses", lambda: set())

    async def go():
        one, two = make_front(), make_front()
        first = one.allocate()
        child = front.Child(one, "a", first[0], first[1], first[2], "real", None)
        one.children["a"] = child
        second = two.allocate()
        assert second[2] != first[2]
        child.state = "exited"
        one.children.pop("a")
        one.child_gone(child)
        two.release_net(second[2])
        assert two.allocate()[2] == first[2]
    asyncio.run(go())


def test_a_network_with_a_bound_address_is_stepped_around(tmp_path, monkeypatch):
    monkeypatch.setattr(front, "NET_LOCK_DIR", str(tmp_path / "nets"))
    monkeypatch.setattr(front, "bound_addresses", lambda: {"127.16.0.1"})

    async def go():
        assert make_front().allocate()[2] != "127.16.0.0/22"
    asyncio.run(go())


# ---- the estimate ----------------------------------------------------------

def test_the_estimate_follows_the_plan_at_the_pace_of_the_window():
    async def go():
        f = make_front()
        child = make_child(f, "lora")
        plan = {"t": 0, "phases": [{"name": "warm-up", "until": 600_000_000},
                                   {"name": "traffic", "until": 4_200_000_000}]}
        # Ten seconds of wall at 10x: T went 100 s.
        wall = time.monotonic()
        child.samples.extend([(wall - 10.0, 900_000_000)])
        child.clocked({"mode": "virtual", "rate": 10, "t": 1_000_000_000, "plan": plan})
        child.samples[-1] = (wall, 1_000_000_000)
        assert abs(child.pace() - 10.0) < 1e-6
        est = child.estimate()
        assert est["phase"]["name"] == "traffic"
        assert est["phase"]["from"] == 600_000_000
        # 3200 s of T left at 10x is 320 s of wall.
        assert abs(est["eta"] - (time.time() + 320.0)) < 2.0
        assert not est["done"]
        child.clocked({"mode": "virtual", "rate": 10, "t": 5_000_000_000, "plan": plan})
        est = child.estimate()
        assert est["phase"] is None and est["done"] and est["eta"] is None
    asyncio.run(go())


def test_no_plan_is_no_estimate_and_real_time_is_pace_one():
    async def go():
        child = make_child(make_front(), "lora")
        child.clocked({"mode": "real", "rate": 1, "t": 5})
        assert child.pace() == 1.0
        assert child.estimate() == {"plan": None, "phase": None, "eta": None}
        child.heard({"type": "snapshot", "run": {"geodata": "g", "nodeset": "n", "script": "s",
                                                 "dir": "/r"}, "nodeset": {"dirty": True}})
        row = child.summary()
        assert (row["geodata"], row["nodeset"], row["script"], row["dirty"]) == ("g", "n", "s", True)
    asyncio.run(go())


# ---- WebRTC through two relays ---------------------------------------------

def binding_request(username):
    attr = username.encode()
    body = struct.pack("!HH", webrtc.STUN_USERNAME, len(attr)) + attr
    body += b"\0" * (-len(attr) % 4)
    return struct.pack("!HHI", 0x0001, len(body), webrtc.STUN_MAGIC) + b"\1" * 12 + body


ANSWER = ('{"type":"answer","sdp":"v=0\\r\\na=ice-lite\\r\\n'
          'm=application 4433 UDP/DTLS/SCTP webrtc-datachannel\\r\\n'
          'c=IN IP4 127.20.0.5\\r\\na=ice-ufrag:725D\\r\\n'
          'a=candidate:1 1 UDP 2130706431 127.20.0.5 4433 typ host\\r\\n"}')


def test_a_flow_crosses_the_front_and_the_child_to_the_station():
    """The answer is claimed twice, as it passes each relay on its way out,
    and the browser's first packet then finds the station through both."""
    async def go():
        loop = asyncio.get_running_loop()
        station = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        station.bind(("127.0.0.1", 0))
        station.setblocking(False)
        station_port = station.getsockname()[1]

        child = await webrtc.serve("127.0.0.1", 0, "127.0.0.1", 0)
        child_port = child.transport.get_extra_info("sockname")[1]
        child.advertise_port = child_port
        outer = await webrtc.serve("127.0.0.1", 0, "127.0.0.1", 0)
        outer_port = outer.transport.get_extra_info("sockname")[1]
        outer.advertise_port = outer_port

        learned = []
        seen_by_front = child.claim(ANSWER, "127.0.0.1", station_port, learned)
        seen_by_browser = outer.claim(seen_by_front, "127.0.0.1", child_port, learned)
        assert "127.0.0.1 %d typ host" % outer_port in seen_by_browser
        assert "127.20.0.5" not in seen_by_browser
        assert learned == ["725D", "725D"]

        browser = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        browser.bind(("127.0.0.1", 0))
        browser.setblocking(False)
        request = binding_request("725D:abcd")
        await loop.sock_sendto(browser, request, ("127.0.0.1", outer_port))
        data, via = await asyncio.wait_for(loop.sock_recvfrom(station, 2048), 2)
        assert data == request
        await loop.sock_sendto(station, b"reply", via)
        data, sender = await asyncio.wait_for(loop.sock_recvfrom(browser, 2048), 2)
        assert data == b"reply" and sender == ("127.0.0.1", outer_port)

        for relay in (child, outer):
            relay.close()
        station.close()
        browser.close()
    asyncio.run(go())


# ---- loss tables before a child starts -------------------------------------

def test_the_tables_are_computed_before_the_run_is_laid_out(tmp_path, monkeypatch):
    """losses.py runs as a subprocess on the tests' four stations, its
    progress goes out tagged with the simulation, and the run holds the
    table, the script, the build and the rules it was given."""
    monkeypatch.setattr(store, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(store, "NODESETS_DIR", str(tmp_path / "nodesets"))
    monkeypatch.setattr(store, "SCRIPTS_DIR", str(tmp_path / "scripts"))
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    (tmp_path / "nodesets").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "geodata").mkdir()
    shutil.copy(FOUR, str(tmp_path / "nodesets" / "four.yaml"))
    (tmp_path / "geodata" / "plain-27").mkdir()
    shutil.copy(PLAIN, str(tmp_path / "geodata" / "plain-27" / "geodata.yaml"))
    (tmp_path / "scripts" / "four.py").write_text("from sim_mesh import *\n")
    (tmp_path / "scripts" / "globals.py").write_text("FREQ_MHZ = 869.525\nSF = 8\n"
                                                     "BW_KHZ = 125\nCR = 5\n")
    build = "stub_x86_64_1.0.0"
    rules = [{"which": {"all": True}, "firmware": "stub_latest"}]

    async def go():
        f = make_front()
        f.sidecars = front.Sidecars(None)
        said = []
        f.broadcast = said.append
        tables = await f.compute_losses(PLAIN, FOUR, ["868"], None, {"sim": "t"})
        path, cached = tables["868"]
        assert os.path.isfile(path)
        progress = [m for m in said if m["type"] == "losses_progress"]
        assert cached or (progress[-1]["sim"] == "t"
                          and progress[-1]["done"] == progress[-1]["total"] > 0)
        spec = {"geodata": "plain-27", "nodeset": "four", "script": "four",
                "build": build, "firmware_rules": rules, "inputs": {"firmware": "stub_latest"}}
        run, sidecar = await f.prepare_run("t", spec)
        assert sidecar is None and run.bands() == ["868"]
        assert run.dir == str(tmp_path / "runs" / "t")
        assert run.meta["build"] == build and run.meta["builds"] == {}
        assert run.meta["inputs"] == {"firmware": "stub_latest"}
        assert run.meta["nodeset"] == "four" and run.meta["script"] == "four"
        assert run.meta["firmware_rules"] == rules and run.meta["first_boot_rules"] == []
        assert run.script_path
        again, _ = await f.prepare_run("t", spec)
        assert again.dir == run.dir + "-2"
        with pytest.raises(store.StoreError, match="no script"):
            await f.prepare_run("u", dict(spec, script="nothere"))
    asyncio.run(go())


def test_a_pack_without_a_planner_is_refused_with_the_sentence(monkeypatch):
    class Pack:
        is_pack, pack_dir, name = True, "/nowhere/berlin-city", "berlin"

    monkeypatch.setattr(front, "planner_web", lambda: None)

    async def go():
        cars = front.Sidecars(None)
        with pytest.raises(store.StoreError) as err:
            await cars.hold("conn:1", Pack())
        assert str(err.value) == front.NO_PLANNER % "berlin"
        assert not cars.by_pack
    asyncio.run(go())


def test_a_sidecar_nobody_holds_is_kept_for_a_while_for_a_page_coming_back(monkeypatch):
    """A page reloading lets go of its geodata's sidecar and holds it again a
    moment later: the same sidecar, still up. One nobody holds again within
    SIDECAR_GRACE_S is stopped."""
    class Pack:
        is_pack, pack_dir, name = True, "/packs/berlin", "berlin"

    monkeypatch.setattr(front, "SIDECAR_GRACE_S", 0.05)

    async def go():
        cars = front.Sidecars(None)
        car = front.Sidecar(Pack.pack_dir, 1)
        car.ready.set_result(car.url)
        cars.by_pack[car.pack] = car
        assert await cars.hold("conn:1", Pack()) == car.url
        cars.release("conn:1")
        assert cars.by_pack[car.pack] is car and car.idle is not None
        assert await cars.hold("conn:2", Pack()) == car.url
        assert car.idle is None
        await asyncio.sleep(0.1)
        assert cars.by_pack[car.pack] is car and car.holders == {"conn:2"}
        cars.release("conn:2")
        await asyncio.sleep(0.1)
        assert car.pack not in cars.by_pack
    asyncio.run(go())


def test_a_pack_is_refused_for_no_planner_before_its_manifest_is_read(tmp_path, monkeypatch):
    """With no planner the pack is typically gone with it: the reason given
    is the planner, not the manifest. With a planner, an unreadable manifest
    is said as such, and synthetic ground works either way."""
    ground = tmp_path / "geodata"
    monkeypatch.setattr(store, "GEODATA_DIR", str(ground))
    geodata.write(geodata.geodata_path("berlin"), {"pack": str(tmp_path / "gone")})
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.0}})
    monkeypatch.setattr(front, "planner_web", lambda: None)
    with pytest.raises(store.StoreError) as err:
        front.load_geodata("berlin")
    assert str(err.value) == front.NO_PLANNER % "berlin"
    with pytest.raises(store.StoreError) as err:
        front.read_geodata(geodata.geodata_path("berlin"))
    assert str(err.value) == front.NO_PLANNER % "berlin"
    assert front.load_geodata("flat").kind == "synthetic"
    monkeypatch.setattr(front, "planner_web", lambda: "/usr/bin/true")
    with pytest.raises(store.StoreError, match="no readable manifest.json"):
        front.load_geodata("berlin")


# ---- the editors, over the front's own socket and HTTP ------------------------

async def next_of(ws, kind, timeout=90):
    while True:
        msg = await ws.receive_json(timeout=timeout)
        if msg.get("type") == kind:
            return msg


async def ask(ws, verb, **fields):
    await ws.send_json({"type": verb, **fields})
    return await next_of(ws, verb)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    for attr in ("GEODATA_DIR", "NODESETS_DIR", "SCRIPTS_DIR", "LOSSES_DIR", "RUNS_DIR",
                 "SNAPSHOTS_DIR", "COVERAGE_DIR"):
        path = tmp_path / attr.lower()
        path.mkdir()
        monkeypatch.setattr(store, attr, str(path))
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", str(tmp_path / "firmware"))
    monkeypatch.setattr(firmware, "RUNS_DIR", str(tmp_path / "runs_dir"))
    monkeypatch.setattr(firmware, "SNAPSHOTS_DIR", str(tmp_path / "snapshots_dir"))
    monkeypatch.setattr(front, "planner_web", lambda: None)
    monkeypatch.setattr(geodata, "PLANNER_DIR", str(tmp_path / "planner"))
    monkeypatch.setattr(sources, "CACHE_DIR", str(tmp_path / "geodata_dir" / ".cache"))
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.0}})
    (tmp_path / "nodesets_dir" / "here.yaml").write_text(
        "nodes:\n  a: { id: 1, lat: 0, lon: 0, tags: [x] }\n")
    (tmp_path / "nodesets_dir" / "there.yaml").write_text(
        "nodes:\n  a: { id: 1, lat: 52.5, lon: 13.4 }\n")
    return tmp_path


def running_front(check):
    async def go():
        f = make_front()
        await f.open_sessions()
        await f.start_http()
        base = "http://127.0.0.1:%d" % f.control_port
        try:
            async with aiohttp.ClientSession() as session:
                ws = await session.ws_connect(base + "/ws")
                await check(f, session, base, ws)
                await ws.close()
        finally:
            await f.shutdown()
    asyncio.run(go())


def test_the_editors_list_open_and_save(stores):
    async def check(f, session, base, ws):
        reply = await ask(ws, "geodata_list")
        assert [g["name"] for g in reply["geodata"]] == ["flat"]
        assert reply["geodata"][0]["kind"] == "synthetic"
        reply = await ask(ws, "nodeset_list")
        rows = {r["name"]: r for r in reply["nodesets"]}
        assert rows["here"]["bbox"] == [0, 0, 0, 0] and rows["here"]["tags"] == {"x": 1}
        reply = await ask(ws, "nodeset_list", geodata="flat")
        rows = {r["name"]: r for r in reply["nodesets"]}
        assert rows["here"]["inside"] and not rows["there"]["inside"]
        reply = await ask(ws, "nodeset_save_as", name="copy",
                          data={"nodes": {"a": {"id": 1, "lat": 0, "lon": 0}, "b": {"id": 2,
                                "lat": 0.01, "lon": 0}}, "offsets": [
                                    {"between": ["a", "b"], "db": 12, "note": "hill"}]})
        assert reply["ok"] and reply["nodeset"]["offsets"][0]["note"] == "hill"
        reply = await ask(ws, "nodeset_save_as", name="copy", data={"nodes": {}})
        assert not reply["ok"] and "already" in reply["error"]
        path = os.path.join(store.NODESETS_DIR, "here.yaml")
        with open(path, "w") as handle:
            handle.write("# the prose\n# kept\nnodes:\n  a: { id: 1, lat: 0, lon: 0 }\n")
        reply = await ask(ws, "nodeset_save", name="here",
                          data={"nodes": {"a": {"id": 1, "lat": 0, "lon": 0}}})
        assert reply["ok"] and open(path).read().startswith("# the prose\n# kept\nnodes:\n")
        reply = await ask(ws, "nodeset_save", name="here",
                          data={"nodes": {"a": {"id": 1, "lat": 0, "lon": 0, "device": "dev"}}})
        assert not reply["ok"] and ".firmware(…)" in reply["error"]
        reply = await ask(ws, "script_new", name="drive")
        assert reply["ok"] and "nodes().firmware(firmware)" in reply["text"]
        assert reply["script"]["references"][0]["path"] == "sim_mesh/library.py"
        reply = await ask(ws, "module_open", path="sim_mesh/library.py")
        assert reply["ok"] and "def on_first_boot" in reply["text"]
        reply = await ask(ws, "script_save", name="drive", text="def (:\n")
        assert not reply["ok"] and "line 1" in reply["error"]
        reply = await ask(ws, "script_list")
        assert [s["name"] for s in reply["scripts"]] == ["drive"]
        reply = await ask(ws, "script_run", name="drive", sim="nothing")
        assert not reply["ok"] and "not running" in reply["error"]
        reply = await ask(ws, "geodata_new", name="hills",
                          synthetic={"exponent": 3.3, "extent_m": 5000})
        assert reply["ok"] and reply["geodata"]["extent_m"] == 5000
        reply = await ask(ws, "coverage", geodata="flat", nodes=[])
        assert not reply["ok"] and "synthetic" in reply["error"]
        reply = await ask(ws, "firmware_list")
        assert reply["ok"] and reply["firmware"] == [] and reply["arch"]
        reply = await ask(ws, "antenna_list")
        kinds = {a["type"]: a for a in reply["antennas"]}
        assert kinds["yagi_directional"]["kind"] == "directional"
        assert kinds["wire_quarter_wave"]["description"].startswith("Bare wire a quarter wave long")
        assert all(a["svg"].startswith("<svg") for a in reply["antennas"])
        sims = await ask(ws, "sims")
        assert "script_runs" in sims and sims["nodesets"] == ["copy", "here", "there"]
    running_front(check)


def test_verb_answers_even_on_an_exception_it_has_no_specific_case_for(stores, monkeypatch):
    """A feed cut mid-download left `aiohttp.ClientPayloadError` escaping
    the editor: verb() must still answer, not leave the page's request
    hanging on a task nobody awaits."""
    async def cut_short(cache, lon, lat):
        raise aiohttp.ClientPayloadError("Response payload is not completed")
    monkeypatch.setattr(sources, "sources_at", cut_short)

    async def check(f, session, base, ws):
        reply = await ask(ws, "geodata_sources_at", lon=13.4, lat=52.5)
        assert not reply["ok"] and "not completed" in reply["error"]
    running_front(check)


def test_a_geodata_delete_says_and_takes_what_stands_on_it(stores):
    pack = pathlib.Path(geodata.geodata_dir("berlin"))
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps(
        {"region": {"crs_epsg": 32633, "bbox": [13.3, 52.4, 13.5, 52.6]}}))
    geodata.write(geodata.geodata_path("berlin"), {"pack": "."})
    berlin = geodata.load("berlin")
    for base, name, marker in ((store.RUNS_DIR, "r1", runs.RUN_FILE),
                               (store.SNAPSHOTS_DIR, "s1", runs.SNAPSHOT_FILE)):
        geodata.write_copy(berlin, os.path.join(base, name, "geodata.yaml"))
        pathlib.Path(base, name, marker).write_text("{}\n")

    async def check(f, session, base, ws):
        reply = await ask(ws, "geodata_rename", name="berlin", to="b2")
        assert not reply["ok"] and "run r1, snapshot s1 stand on berlin's pack" in reply["error"]
        reply = await ask(ws, "geodata_delete_plan", names=["berlin"])
        assert {k: reply[k] for k in ("geodata", "runs", "snapshots", "nodesets", "running")} \
            == {"geodata": [], "runs": ["r1"], "snapshots": ["s1"], "nodesets": ["there"],
                "running": []}
        reply = await ask(ws, "geodata_delete", names=["berlin"])
        assert reply["ok"] and reply["deleted"]["geodata"] == ["berlin"]
        assert geodata.names() == ["flat"] and not pack.exists()
        assert os.listdir(store.RUNS_DIR) == [] and os.listdir(store.SNAPSHOTS_DIR) == []
        # `here` stands on flat, which is still there.
        assert nodeset.names() == ["here"]
    running_front(check)


def test_sizes_sources_indexes_and_files_of_points(stores, monkeypatch):
    import indexes
    site = stores / "site"
    site.mkdir()
    text = "nodes:\n  a: { id: 1, lat: 0.001, lon: 0.001 }\n"
    (site / "pair.yaml").write_text(text)
    (site / "index.yaml").write_text(
        "index: demos\nnodesets:\n  - { name: pair, url: pair.yaml, sha256: %s, bytes: %d }\n"
        % (hashlib.sha256(text.encode()).hexdigest(), len(text)))
    monkeypatch.setattr(indexes, "OWN_ADDRESS", str(site) + "/")
    monkeypatch.setattr(indexes, "OWN_NAME", "demos")
    monkeypatch.setattr(indexes, "ADDED_FILE", str(stores / "indexes.yaml"))
    glo30 = os.path.join(sources.CACHE_DIR, "glo30")
    os.makedirs(glo30)
    with open(os.path.join(glo30, "tile.tif"), "wb") as handle:
        handle.write(b"\0" * 1000)

    async def check(f, session, base, ws):
        rows = {g["name"]: g for g in (await ask(ws, "geodata_list"))["geodata"]}
        assert rows["flat"]["bytes"] > 0 and rows["flat"]["from_index"] is None
        assert rows["flat"]["origin"] == [0, 0]
        rows = {r["name"]: r for r in (await ask(ws, "nodeset_list"))["nodesets"]}
        assert rows["here"]["bytes"] == os.path.getsize(os.path.join(store.NODESETS_DIR, "here.yaml"))

        reply = await ask(ws, "geodata_sources")
        got = {r["source"]: r for r in reply["sources"]}
        assert got["glo30"]["bytes"] == 1000 and got["worldcover"]["bytes"] == 0
        reply = await ask(ws, "geodata_source_clear", source="glo30")
        assert reply["ok"] and not os.path.exists(glo30)
        reply = await ask(ws, "geodata_source_clear", source="nothing")
        assert not reply["ok"]

        reply = await ask(ws, "index_list")
        offered = reply["indexes"][0]["nodesets"]
        assert [(e["name"], e["installed"], e["bytes"]) for e in offered] == [("pair", False, len(text))]
        await ws.send_json({"type": "index_install", "index": "demos", "kind": "nodesets",
                            "name": "pair"})
        progress = await next_of(ws, "index_progress")
        assert (progress["name"], progress["state"]) == ("pair", "fetching")
        reply = await next_of(ws, "index_install")
        assert reply["ok"] and reply["installed"] == [{"kind": "nodesets", "name": "pair"}]
        rows = {r["name"]: r for r in (await ask(ws, "nodeset_list"))["nodesets"]}
        came = rows["pair"]["from_index"]
        assert came["index"] == "demos" and not came["changed"]
        reply = await ask(ws, "index_delete", name="demos")
        assert not reply["ok"] and "always listed" in reply["error"]

        reply = await ask(ws, "nodeset_import", name="pts", source="csv", geodata="flat",
                          text="y,x,who\n0.002,0.002,One\n52,13,Far\n",
                          columns={"lat": "y", "lon": "x", "name": "who"})
        assert reply["ok"] and list(reply["nodeset"]["nodes"]) == ["one"]
    running_front(check)


def test_firmware_and_a_pack_are_added_over_http(stores):
    import stub_firmware

    arch = firmware.machine_arch()
    name = "stub-sx1262_%s_20260925035045" % arch
    held = "stub-sx1262_%s_20260101000000" % arch

    async def check(f, session, base, ws):
        data = stub_firmware.zip_bytes("stub-sx1262", "20260925035045", hardware="ESP32",
                                       radio="sx1262")
        async with session.post(base + "/api/firmware/add", params={"name": "mine.zip"},
                                data=data) as resp:
            got = await resp.json()
        assert got["ok"] and got["name"] == name
        async with session.post(base + "/api/firmware/add", params={"name": "x.zip"},
                                data=data) as resp:
            got = await resp.json()
        assert not got["ok"] and "installed already" in got["error"]
        stub_firmware.install(stores / "firmware", "stub-sx1262", "20260101000000")
        paused = stores / "runs_dir" / "p"
        (paused / "paused").mkdir(parents=True)
        (paused / "paused" / "snapshot.yaml").write_text("{}\n")
        (paused / "run.yaml").write_text(yaml.safe_dump({
            "paused": {"t": 1}, "builds": {"x": {"firmware": held}}}))
        reply = await ask(ws, "firmware_list")
        rows = {r["name"]: r for r in reply["firmware"]}
        assert rows[name]["hardware"] == "ESP32" and rows[name]["radio"] == "sx1262"
        assert rows[held]["users"] == ["run p"]
        reply = await ask(ws, "firmware_delete", names=[held])
        assert not reply["ok"] and "held by run p" in reply["error"]
        reply = await ask(ws, "firmware_delete", names=[name])
        assert reply["ok"] and reply["deleted"] == [name]
        assert [r["name"] for r in (await ask(ws, "firmware_list"))["firmware"]] == [held]
        # A paused simulation the front lists holds it: refused without
        # stop_paused, and with it that simulation is stopped for good first.
        f.paused["lora"] = runs.Run(str(paused))
        rows = {r["name"]: r for r in (await ask(ws, "firmware_list"))["firmware"]}
        assert rows[held]["paused"] == ["lora"]
        reply = await ask(ws, "firmware_delete", names=[held])
        assert not reply["ok"] and "paused simulation lora holds it" in reply["error"]
        reply = await ask(ws, "firmware_delete", names=[held], stop_paused=True)
        assert reply["ok"] and reply["deleted"] == [held] and reply["stopped"] == ["lora"]
        assert "lora" not in f.paused and not (paused / "paused").exists()

        pack = io.BytesIO()
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("manifest.json", json.dumps(
                {"region": {"crs_epsg": 32633, "bbox": [13.3, 52.4, 13.5, 52.6]}}))
        async with session.post(base + "/api/geodata/import", params={"name": "tiny"},
                                data=pack.getvalue()) as resp:
            got = await resp.json()
        assert got["ok"] and got["geodata"]["kind"] == "pack"
        assert os.path.isfile(os.path.join(store.GEODATA_DIR, "tiny", "manifest.json"))
        assert not [n for n in os.listdir(front.SIM_DIR) if n.startswith(".upload-")]

        async with session.get(base + "/api/geodata/export", params={"name": "tiny"}) as resp:
            assert resp.headers["Content-Disposition"] == 'attachment; filename="tiny.zip"'
            exported = await resp.read()
        with zipfile.ZipFile(io.BytesIO(exported)) as zf:
            assert sorted(zf.namelist()) == ["geodata.yaml", "pack/manifest.json"]
        async with session.post(base + "/api/geodata/import", params={"name": "tiny-again"},
                                data=exported) as resp:
            got = await resp.json()
        assert got["ok"] and got["geodata"]["bbox"] == [13.3, 52.4, 13.5, 52.6]
        async with session.get(base + "/api/geodata/export", params={"name": "none"}) as resp:
            assert resp.status == 404
    running_front(check)


# ---- nodesets: a public node map imported, several saved as one ---------------

FAKE_NODES_JOB = r'''#!/usr/bin/env python3
import json, sys
job = json.load(sys.stdin)
assert sys.argv[1] == "nodes-import"
rows = json.load(open(job["file"]))
lon0, lat0, lon1, lat1 = job["bbox"]
inside = [r for r in rows if lon0 <= r["lon"] <= lon1 and lat0 <= r["lat"] <= lat1]
print(json.dumps({"nodes": [dict(r, position="gps") for r in inside
                            if job["companions"] or r["kind"] != "companion"],
                  "report": {"kept": len(inside)}}))
'''


def test_a_node_map_becomes_a_nodeset_and_several_save_as_one(stores, monkeypatch):
    job = stores / "planner-job"
    job.write_text(FAKE_NODES_JOB)
    job.chmod(0o755)
    monkeypatch.setattr(front, "planner_job", lambda: str(job))
    meta = stores / "geodata_dir" / ".cache" / "meta"
    meta.mkdir(parents=True)
    (meta / front.MESHCORE_META).write_text(json.dumps([
        {"label": "Alex Repeater 🗼", "lat": 0.01, "lon": 0.01, "kind": "repeater"},
        {"label": "", "lat": 0.02, "lon": 0.02, "kind": "room-server"},
        {"label": "phone", "lat": 0.03, "lon": 0.03, "kind": "companion"},
        {"label": "far away", "lat": 52.5, "lon": 13.4, "kind": "repeater"}]))

    async def check(f, session, base, ws):
        async with session.get(base + "/api/nodes/sources") as resp:
            assert (await resp.json())["meshcore"]["fetched"] is not None
        reply = await ask(ws, "nodeset_import", name="mc", source="meshcore", geodata="flat",
                          height_m=12)
        assert reply["ok"], reply
        nodes = reply["nodeset"]["nodes"]
        assert sorted(nodes) == ["alex-repeater", "meshcore-2"]
        assert nodes["alex-repeater"]["tags"] == ["repeater"]
        assert nodes["alex-repeater"]["height_m"] == 12
        assert nodes["alex-repeater"]["height_from"] == "assumed"
        reply = await ask(ws, "nodeset_import", name="mc", source="meshcore", geodata="flat")
        assert not reply["ok"] and "already" in reply["error"]

        reply = await ask(ws, "nodeset_merge", name="both", layers=[
            {"name": "here", "data": {"nodes": {"a": {"id": 1, "lat": 0, "lon": 0}}}},
            {"name": "mc", "data": {"nodes": nodes}}])
        assert reply["ok"], reply
        merged = reply["nodeset"]["nodes"]
        assert sorted(merged) == ["a", "alex-repeater", "meshcore-2"]
        assert sorted(n["id"] for n in merged.values()) == [1, 2, 3]
        assert "mc" in merged["alex-repeater"]["tags"] and "here" in merged["a"]["tags"]
        assert open(os.path.join(store.NODESETS_DIR, "both.yaml")).read().startswith(
            "# from here, mc\n")
    running_front(check)


# ---- coverage, through a planner of the test's own ---------------------------

def fake_planner(calls, loss_options=None):
    """/loss/start, /loss/status and /loss.bin as planner-web answers them:
    one sweep, which is running for the first status and whole after. With
    `loss_options`, /api/pack lists them, as a sidecar that takes them does;
    without, there is no /api/pack, as for a sidecar that predates it."""
    state = {"polls": 0, "radius": None}

    async def pack(request):
        calls.append(("pack", {}))
        return web.json_response({"loss_options": loss_options})

    async def start(request):
        calls.append(("start", dict(request.query)))
        state["radius"], state["polls"] = float(request.query["radius_km"]), 0
        return web.json_response({"state": "running", "done": 0, "bands": 6})

    async def status(request):
        state["polls"] += 1
        if state["polls"] < 2:
            return web.json_response({"state": "running", "done": 3, "bands": 6})
        return web.json_response({"state": "idle", "done": 6, "bands": 6,
                                  "band_km": state["radius"]})

    async def loss(request):
        calls.append(("loss", dict(request.query)))
        head = b"PLS2" + struct.pack("<II", 2, 1) + struct.pack("<4d", 0, 0, 1, 1)
        return web.Response(body=head + struct.pack("<2H", 10000, 65535))

    app = web.Application()
    app.router.add_get("/loss/start", start)
    app.router.add_get("/loss/status", status)
    app.router.add_get("/loss.bin", loss)
    if loss_options is not None:
        app.router.add_get("/api/pack", pack)
    return app


def test_a_node_raster_is_swept_once_and_then_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "COVERAGE_DIR", str(tmp_path / "coverage"))
    monkeypatch.setattr(coverage, "POLL_S", 0.01)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps(
        {"region": {"crs_epsg": 32633, "bbox": [13.3, 52.4, 13.5, 52.6]}}))
    gd = geodata.Geodata("berlin", {"pack": str(pack)})
    node = {"name": "alex", "lat": 52.52, "lon": 13.41, "height_m": 20}
    calls = []

    async def go():
        runner = web.AppRunner(fake_planner(calls))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                sweeps = coverage.Sweeps(session)
                path = await sweeps.raster(url, gd, node)
                assert open(path, "rb").read(4) == b"PLS2"
                assert await sweeps.raster(url, gd, node) == path
                moved = dict(node, height_m=25)
                assert await sweeps.raster(url, gd, moved) != path
        finally:
            await runner.cleanup()
    asyncio.run(go())
    starts = [q for what, q in calls if what == "start"]
    assert len(starts) == 2 and starts[0]["tx_h"] == "20" and starts[0]["rx_h"] == "2"
    assert "whole" not in starts[0]         # not offered, so not sent
    x, y = gd.to_xy(52.52, 13.41)
    loss = [q for what, q in calls if what == "loss"][0]
    assert float(loss["minx"]) == pytest.approx(x - 10000, abs=1e-3)
    assert loss["w"] == str(coverage.CELLS)
    assert coverage.key(gd, node) == coverage.key(gd, dict(node, tx_dbm=30))
    with pytest.raises(store.StoreError):
        coverage.cache_path("berlin", "../../etc/passwd")


def test_a_sidecar_that_offers_whole_sweeps_is_asked_for_them(tmp_path, monkeypatch):
    """Only the whole radius is kept, so a sidecar that lists `whole` is asked
    for it rather than the ladder of bands, and is asked what it takes once."""
    monkeypatch.setattr(store, "COVERAGE_DIR", str(tmp_path / "coverage"))
    monkeypatch.setattr(coverage, "POLL_S", 0.01)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps(
        {"region": {"crs_epsg": 32633, "bbox": [13.3, 52.4, 13.5, 52.6]}}))
    gd = geodata.Geodata("berlin", {"pack": str(pack)})
    node = {"name": "alex", "lat": 52.52, "lon": 13.41, "height_m": 20}
    calls = []

    async def go():
        runner = web.AppRunner(fake_planner(calls, loss_options=["whole"]))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                sweeps = coverage.Sweeps(session)
                await sweeps.raster(url, gd, node)
                await sweeps.raster(url, gd, dict(node, height_m=25))
        finally:
            await runner.cleanup()
    asyncio.run(go())
    starts = [q for what, q in calls if what == "start"]
    assert [q.get("whole") for q in starts] == ["true", "true"]
    assert [what for what, _ in calls].count("pack") == 1


# ---- heights from a pack's evidence -------------------------------------------

def test_heights_are_estimated_only_on_a_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.0}})
    monkeypatch.setattr(store, "NODESETS_DIR", str(tmp_path / "nodesets"))
    ns = nodeset.create("few")
    ns.add_node("a", 0.001, 0.001, height_m=15)
    ns.save()

    async def check(f, session, base, ws):
        reply = await ask(ws, "nodeset_heights", name="few", geodata="flat")
        assert not reply["ok"] and "synthetic" in reply["error"]
        assert nodeset.load("few").nodes["a"]["height_m"] == 15
    running_front(check)


def test_a_nodesets_assumed_heights_are_estimated_on_a_pack(tmp_path, monkeypatch):
    if front.planner_web() is None or not os.path.isdir(BERLIN_PACK):
        pytest.skip("no planner-web build in planner/ or no berlin-city geodata")
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    geodata.write(geodata.geodata_path("berlin"), {"pack": BERLIN_PACK})
    monkeypatch.setattr(store, "NODESETS_DIR", str(tmp_path / "nodesets"))
    monkeypatch.setattr(store, "RUNS_DIR", str(tmp_path / "runs"))
    ns = nodeset.create("kiez")
    # A perimeter block in Kreuzberg, and the same place surveyed.
    ns.add_node("guess", 52.4930, 13.4190, height_m=15)
    ns.add_node("known", 52.4931, 13.4191, height_m=11, height_from="measured")
    ns.save()

    async def check(f, session, base, ws):
        reply = await ask(ws, "nodeset_heights", name="kiez", geodata="berlin")
        assert reply["ok"], reply
        assert reply["changed"] == ["guess"]
        guess = nodeset.load("kiez").nodes["guess"]
        got = reply["estimates"]["guess"]
        assert guess["height_from"] == nodeset.HEIGHT_FROM_BASIS[got["basis"]]
        assert got["low_m"] <= guess["height_m"] <= got["high_m"]
        assert got["buildings_index"] in ("ready", "absent")
        known = nodeset.load("kiez").nodes["known"]
        assert (known["height_m"], known["height_from"]) == (11, "measured")
    running_front(check)


# ---- the planner behind /planner/<geodata>/ ----------------------------------

def test_the_planner_is_started_for_a_pack_and_passed_through(tmp_path, monkeypatch):
    if front.planner_web() is None or not os.path.isdir(BERLIN_PACK):
        pytest.skip("no planner-web build in planner/ or no berlin-city geodata")
    ground = tmp_path / "geodata"
    monkeypatch.setattr(store, "GEODATA_DIR", str(ground))
    geodata.write(geodata.geodata_path("berlin"), {"pack": BERLIN_PACK})
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.0}})
    monkeypatch.setattr(store, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(front, "SIDECAR_GRACE_S", 0.1)

    async def check(f, session, base, ws):
        async with session.get(base + "/planner/berlin/api/pack") as resp:
            assert resp.status == 404       # nobody holds the geodata yet
        reply = await ask(ws, "geodata_open", name="flat")
        assert reply["ok"] and reply["planner"] is None and not f.sidecars.by_pack
        reply = await ask(ws, "geodata_open", name="berlin")
        assert reply["ok"], reply
        assert reply["planner"] == "/planner/berlin/"
        assert reply["geodata"]["crs_epsg"] == 32633
        async with session.get(base + "/planner/berlin/api/pack") as resp:
            assert resp.status == 200
            info = json.loads(await resp.text())
        assert info["crs_epsg"] == 32633 and "extent" in info
        car = next(iter(f.sidecars.by_pack.values()))
        assert car.holders == {"conn:%d" % id(next(iter(f.conns)))}
        await ws.close()
        for _ in range(200):
            if car.process.returncode is not None:
                break
            await asyncio.sleep(0.05)
        assert car.process.returncode is not None and not f.sidecars.by_pack
    running_front(check)


def test_a_browser_gone_during_the_planner_proxy_ends_quietly(stores, monkeypatch, capsys):
    """Panning the map drops the request before the proxy answers: the
    write back to the browser then raises ConnectionResetError. A client
    that has gone is normal and must not become a traceback."""
    monkeypatch.setattr(front, "planner_web", lambda: "/usr/bin/true")
    pack = stores / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(json.dumps(
        {"region": {"crs_epsg": 32633, "bbox": [13.3, 52.4, 13.5, 52.6]}}))
    geodata.write(geodata.geodata_path("tile-src"), {"pack": str(pack)})

    async def slow_tile(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        try:
            for _ in range(60):
                await resp.write(b"x" * 2048)
                await asyncio.sleep(0.03)
            await resp.write_eof()
        except ConnectionError:
            pass          # the proxy let go of its own side first: fine
        return resp

    app = web.Application()
    app.router.add_get("/{tail:.*}", slow_tile)

    async def check(f, session, base, ws):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        gd = front.load_geodata("tile-src")
        car = front.Sidecar(gd.pack_dir, port)
        car.ready.set_result(car.url)
        f.sidecars.by_pack[gd.pack_dir] = car
        try:
            async with aiohttp.ClientSession() as probe:
                with pytest.raises(asyncio.TimeoutError):
                    async with probe.get(base + "/planner/tile-src/x.png",
                                         timeout=aiohttp.ClientTimeout(total=0.1)) as resp:
                        await resp.read()
            await asyncio.sleep(0.3)        # the proxy's next write meets the drop
        finally:
            await runner.cleanup()
        err = capsys.readouterr().err
        assert "the browser is gone" in err
        assert "Traceback" not in err
        reply = await ask(ws, "geodata_list")
        assert "tile-src" in [g["name"] for g in reply["geodata"]]
    running_front(check)
