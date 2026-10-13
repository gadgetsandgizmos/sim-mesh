"""Loss tables: synthetic ground's model, offsets, the cache, one node's row,
the command, and a pack through a planner-web of the test's own."""

import asyncio
import json
import math
import os
import socket
import subprocess
import sys

import pytest
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "ether"))

import ether  # noqa: E402
import geodata  # noqa: E402
import losses  # noqa: E402
import nodeset  # noqa: E402
import slt  # noqa: E402
import store  # noqa: E402

PLANNER_WEB = os.path.join(HERE, "..", "planner", "target", "release", "planner-web")
BERLIN_PACK = os.path.join(HERE, "geodata", "berlin-city")


@pytest.fixture
def stores(tmp_path, monkeypatch):
    for key, sub in (("GEODATA_DIR", "geodata"), ("NODESETS_DIR", "nodesets"),
                     ("LOSSES_DIR", "losses")):
        monkeypatch.setattr(store, key, str(tmp_path / sub))
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.1}})
    ns = nodeset.create("three")
    ns.add_node("a", 0.0, 0.0)
    ns.add_node("b", 0.006, 0.0)
    ns.add_node("c", 0.0, 0.0203)
    ns.add_node("d", 0.0, 0.0)                    # on top of a: a metre apart
    ns.set_offset("a", "c", 25)
    ns.save()
    return tmp_path


def reference_loss(f_hz, exponent, a, b):
    """The log-distance formula on synthetic ground, spelled out here independently."""
    ax, ay = a[1] * 111120, a[0] * 111120
    bx, by = b[1] * 111120, b[0] * 111120
    d = max(1.0, math.hypot(ax - bx, ay - by))
    return 20 * math.log10(4 * math.pi * f_hz / 299_792_458.0) + 10 * exponent * math.log10(d)


def test_a_synthetic_table_is_the_formula_without_offsets(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    table = losses.synthetic_table(gd, ns, "868")
    assert table.header["model"] == "log-distance"
    assert table.header["geodata"] == "flat" and table.header["nodes"][0]["name"] == "a"
    assert table.f0_hz == slt.f0_of("868")
    pos = {n: (d["lat"], d["lon"]) for n, d in ns.nodes.items()}
    for a in pos:
        for b in pos:
            if a == b:
                continue
            want = reference_loss(table.f0_hz, 3.1, pos[a], pos[b])
            assert table.get(a, b) == pytest.approx(want, abs=1e-4)
            assert table.get(a, b) == table.get(b, a)
            assert table.flag(a, b) == 0
    # Within the band a frame's own carrier is the free-space correction.
    f = 869_525_000
    assert table.at("a", "b", f) == pytest.approx(
        reference_loss(f, 3.1, pos["a"], pos["b"]), abs=1e-4)


def test_offsets_are_a_layer_added_both_ways(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    table = losses.synthetic_table(gd, ns, "868")
    layered = losses.with_offsets({"868": table}, ns)["868"]
    assert layered is not table
    assert layered.get("a", "c") == pytest.approx(table.get("a", "c") + 25)
    assert layered.get("c", "a") == pytest.approx(table.get("c", "a") + 25)
    assert layered.get("a", "b") == table.get("a", "b")
    ns.set_offset("a", "c", 0)
    assert losses.with_offsets({"868": table}, ns)["868"] is table


def test_a_link_states_a_pairs_loss_whatever_the_distance(stores):
    """Ten kilometres apart and heard as the stated 110 dB, not the 150-odd
    the distance would cost: in every band's table, and in a cell the model
    never heard."""
    gd, ns = geodata.load("flat"), nodeset.load("three")
    ns.add_node("far", 0.09, 0.0)
    tables = {band: losses.synthetic_table(gd, ns, band) for band in ("433", "868")}
    assert tables["868"].get("a", "far") > 150
    for a, b in (("b", "far"), ("far", "b")):
        tables["433"].put(a, b, slt.NEVER, slt.FLAG_BEYOND_RADIUS)
    ns.data["links"] = [{"between": ["a", "far"], "loss_db": 110.0},
                        {"between": ["far", "b"], "loss_db": 120.0}]
    got = losses.with_links(tables, ns)
    for band in ("433", "868"):
        assert got[band] is not tables[band]
        assert got[band].get("a", "far") == 110 == got[band].get("far", "a")
        assert got[band].get("b", "far") == 120 == got[band].get("far", "b")
        assert got[band].get("a", "b") == tables[band].get("a", "b")
    assert tables["868"].get("a", "far") > 150                  # the model's table left alone
    del ns.data["links"]
    assert losses.with_links(tables, ns)["868"] is tables["868"]


def test_a_link_is_the_same_both_ways_unless_it_says_otherwise_and_an_offset_still_adds(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")      # 25 dB offset between a and c
    tables = {"868": losses.synthetic_table(gd, ns, "868")}
    model = tables["868"]
    gains = losses.with_antennas(tables, gd, ns)["868"]

    def gain(x, y):
        """Both ends' gains toward each other, as the antenna layer takes them off."""
        return model.get(x, y) - gains.get(x, y)
    ns.data["links"] = [{"between": ["a", "c"], "loss_db": 100.0},
                        {"between": ["a", "b"], "loss_db": 90.0, "back_db": 95.5}]
    medium = losses.medium_tables(tables, gd, ns)["868"]
    # The link stands in for the model's loss alone: the gains and the offset go on top.
    assert medium.get("a", "c") == pytest.approx(100 - gain("a", "c") + 25, abs=1e-3)
    assert medium.get("c", "a") == pytest.approx(100 - gain("c", "a") + 25, abs=1e-3)
    assert medium.get("a", "b") == pytest.approx(90 - gain("a", "b"), abs=1e-3)
    assert medium.get("b", "a") == pytest.approx(95.5 - gain("b", "a"), abs=1e-3)
    assert medium.get("b", "c") == pytest.approx(model.get("b", "c") - gain("b", "c"), abs=1e-3)


def test_without_links_the_medium_is_given_what_it_was_before_them(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    tables = {"868": losses.synthetic_table(gd, ns, "868")}
    got = losses.medium_tables(tables, gd, ns)["868"]
    was = losses.with_offsets(losses.with_antennas(tables, gd, ns), ns)["868"]
    assert got.loss.tobytes() == was.loss.tobytes()
    assert got.flags.tobytes() == was.flags.tobytes()
    # Nor does a spread of 0, or a seed alone, change a cell.
    for keys in ({"shadowing_db": 0}, {"shadowing_seed": 5}):
        got = losses.medium_tables(tables, rough(**keys), ns)["868"]
        assert got.loss.tobytes() == was.loss.tobytes()


# ---- shadowing -----------------------------------------------------------

def rough(exponent=3.1, **keys):
    """The fixture's flat ground again, with shadowing keys."""
    geodata.write(geodata.geodata_path("rough"), {"synthetic": {"exponent": exponent}, **keys})
    return geodata.load("rough")


def test_shadowing_is_one_draw_per_pair_the_same_both_ways_and_every_time():
    draw = losses.shadowing_unit
    assert draw(3, "a", "b") == draw(3, "b", "a") == draw(3, "a", "b")
    assert draw(3, "a", "b") != draw(3, "a", "c")
    assert draw(3, "a", "b") != draw(4, "a", "b")
    # The same from a cold start, and the same as on the day it was written:
    # SHA-256 of "0:a:b", Box–Muller. A change here moves every run's ground.
    draw.cache_clear()
    assert draw(0, "b", "a") == pytest.approx(-0.9460136480786182, abs=1e-12)


def test_shadowing_draws_spread_like_a_standard_normal():
    draws = [losses.shadowing_unit(7, "n%02d" % a, "n%02d" % b)
             for a in range(40) for b in range(a + 1, 40)]
    mean = sum(draws) / len(draws)
    spread = math.sqrt(sum((d - mean) ** 2 for d in draws) / (len(draws) - 1))
    assert abs(mean) < 0.12
    assert spread == pytest.approx(1.0, abs=0.08)
    assert sum(abs(d) < 1 for d in draws) / len(draws) == pytest.approx(0.683, abs=0.05)


def test_shadowing_moves_a_pair_by_its_draw_times_the_spread_both_ways_in_every_band(stores):
    gd, ns = rough(shadowing_db=7, shadowing_seed=3), nodeset.load("three")
    tables = {band: losses.synthetic_table(gd, ns, band) for band in ("433", "868")}
    got = losses.with_shadowing(tables, gd, ns)
    for band, table in tables.items():
        assert got[band] is not table
        for a in table.names:
            for b in table.names:
                if a != b:
                    want = table.get(a, b) + 7 * losses.shadowing_unit(3, a, b)
                    assert got[band].get(a, b) == pytest.approx(want, abs=1e-4)
    shift = {band: got[band].get("a", "c") - tables[band].get("a", "c") for band in tables}
    assert abs(shift["868"]) > 0.1 and shift["433"] == pytest.approx(shift["868"], abs=1e-4)
    # The draw is the pair's alone: in another nodeset, with another node
    # and in another order, the pair moves by as much.
    other = nodeset.create("other")
    for name in ("e", "c", "a"):
        node = ns.nodes.get(name) or {"lat": 0.003, "lon": 0.003}
        other.add_node(name, node["lat"], node["lon"])
    table = losses.synthetic_table(gd, other, "868")
    moved = losses.with_shadowing({"868": table}, gd, other)["868"]
    assert moved.get("c", "a") - table.get("c", "a") == pytest.approx(shift["868"], abs=1e-4)
    # No spread, no layer: the tables themselves.
    assert losses.with_shadowing(tables, rough(shadowing_seed=3), ns)["868"] is tables["868"]


def test_shadowing_leaves_never_heard_measured_and_stated_cells_as_they_are(stores):
    gd, ns = rough(shadowing_db=7), nodeset.load("three")
    table = losses.synthetic_table(gd, ns, "868")
    table.put("a", "b", slt.NEVER, slt.FLAG_BEYOND_RADIUS)
    table.put("b", "c", 120.0, slt.FLAG_MEASURED, 12)
    ns.data["links"] = [{"between": ["c", "d"], "loss_db": 100.0}]
    got = losses.with_shadowing({"868": table}, gd, ns)["868"]

    def drawn(a, b):
        return table.get(a, b) + 7 * losses.shadowing_unit(0, a, b)
    assert got.get("a", "b") == slt.NEVER                   # never heard stays never heard
    assert got.get("b", "a") == pytest.approx(drawn("b", "a"), abs=1e-4)
    assert got.get("b", "c") == 120.0                       # the measurement holds its own
    assert got.get("c", "b") == pytest.approx(drawn("c", "b"), abs=1e-4)
    assert (got.get("c", "d"), got.get("d", "c")) == (table.get("c", "d"), table.get("d", "c"))
    # In the medium the stated pair is the link, with no draw on it.
    medium = losses.medium_tables({"868": table}, gd, ns)["868"]
    unshadowed = losses.medium_tables({"868": table}, geodata.load("flat"), ns)["868"]
    assert medium.get("c", "d") == unshadowed.get("c", "d")
    assert medium.get("d", "c") == unshadowed.get("d", "c")


def test_shadowing_is_a_layer_and_never_recomputes_a_table(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    path, _ = asyncio.run(losses.compute(gd, ns, "868"))
    geodata.write(geodata.geodata_path("flat"), dict(gd.data, shadowing_db=7, shadowing_seed=3))
    assert asyncio.run(losses.compute(geodata.load("flat"), ns, "868")) == (path, True)


def test_the_band_is_the_one_globals_carrier_falls_in(stores):
    ns = nodeset.load("three")
    assert losses.bands_of(ns) == ["868"]                # the store's globals.py
    radio = {"freq_mhz": 433.92, "sf": 8, "bw_khz": 125.0, "cr": 5}
    assert losses.bands_of(ns, radio=radio) == ["433"]
    assert losses.bands_of(ns, radio=dict(radio, freq_mhz=915.0)) == ["915"]


def test_a_backbone_is_a_band_of_its_own_among_its_members_only(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    a, b, c = list(ns.nodes)[:3]
    env = {"SIM_MESH_BACKBONE": "%s,%s,nobody" % (a, b)}
    table = losses.backbone_table(gd, ns, ["868"], env)
    assert table.band == "433" and table.names == [a, b]
    assert table.get(a, b) == table.get(b, a) == losses.BACKBONE_LOSS_DB
    assert c not in table.index
    # One member, or none, is no backbone; a carrier in a band the run has is refused.
    assert losses.backbone_table(gd, ns, ["868"], {"SIM_MESH_BACKBONE": a}) is None
    assert losses.backbone_table(gd, ns, ["868"], {}) is None
    with pytest.raises(losses.LossError):
        losses.backbone_table(gd, ns, ["868"], dict(env, SIM_MESH_BACKBONE_RADIO="869000000,5,500"))


def test_a_table_round_trips_through_slt1(stores, tmp_path):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    table = losses.synthetic_table(gd, ns, "433")
    table.put("a", "b", slt.NEVER, slt.FLAG_OFF_PACK | slt.FLAG_BEYOND_RADIUS, 3)
    path = str(tmp_path / "t.bin")
    losses.write_table(table, path)
    back = slt.Table.read(path)
    assert back.header == table.header
    assert back.names == ["a", "b", "c", "d"]
    assert list(back.loss) == list(table.loss)
    assert list(back.flags) == list(table.flags)
    assert list(back.samples) == list(table.samples)
    assert back.get("a", "b") == math.inf and back.samples[back.cell("a", "b")] == 3


def test_the_cache_is_keyed_by_geodata_and_geometry(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    path, hit = asyncio.run(losses.compute(gd, ns, "868"))
    assert not hit
    assert path == os.path.join(store.LOSSES_DIR, "flat", ns.geometry_hash(), "868.bin")
    assert asyncio.run(losses.compute(gd, ns, "868")) == (path, True)
    ns.set_node("a", tags=["x"], antenna={"type": "yagi_directional"},
                max_dbm=27)                                      # not geometry: still a hit
    ns.set_offset("a", "b", 12)
    assert asyncio.run(losses.compute(gd, ns, "868"))[1]
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.2}})
    gd = geodata.load("flat")
    assert losses.cached(gd, ns, "868") is None            # the ground changed
    assert slt.Table.read(asyncio.run(losses.compute(gd, ns, "868"))[0]).header["exponent"] == 3.2


def test_a_changed_nodeset_computes_only_the_pairs_the_cache_has_not(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    asyncio.run(losses.compute(gd, ns, "868"))
    ns.move_node("c", 0.001, 0.02)
    ns.add_node("e", 0.003, 0.003)
    said, totals = [], []
    path, hit = asyncio.run(losses.compute(gd, ns, "868", progress=lambda d, t: totals.append(t),
                                           notice=said.append))
    assert not hit and path == losses.cache_path("flat", ns.geometry_hash(), "868")
    # a, b and d are unchanged: of the ten pairs only the seven touching c or e are asked.
    assert set(totals) == {7}
    assert said == ["3 of 5 nodes' pairs are from a cached table; computing those of the other 2"]
    got, full = slt.Table.read(path), losses.synthetic_table(gd, ns, "868")
    assert got.names == full.names and list(got.loss) == pytest.approx(list(full.loss))
    # Another geodata, or another ground under this one, gives nothing to reuse.
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.3}})
    assert losses.nearest_cached(geodata.load("flat"), ns, "868") == (None, None)


def test_a_renamed_nodes_row_and_column_are_computed_again(stores):
    """A table finds a node by its name, so the name is part of the geometry:
    a rename is a new key, and the renamed node is one the cached table does
    not have, whatever else it shares with a node there."""
    gd, ns = geodata.load("flat"), nodeset.load("three")
    asyncio.run(losses.compute(gd, ns, "868"))
    ns.rename_node("c", "charlie")
    said, totals = [], []
    path, hit = asyncio.run(losses.compute(gd, ns, "868", progress=lambda d, t: totals.append(t),
                                           notice=said.append))
    assert not hit and path == losses.cache_path("flat", ns.geometry_hash(), "868")
    assert said == ["3 of 4 nodes' pairs are from a cached table; computing those of the other 1"]
    assert set(totals) == {3}                              # the pairs touching charlie
    assert "charlie" in slt.Table.read(path).names


def test_one_nodes_row_and_column_are_recomputed_into_a_copy(stores):
    gd, ns = geodata.load("flat"), nodeset.load("three")
    table = losses.synthetic_table(gd, ns, "868")
    table.put("b", "c", 1.0)                               # a marker that must survive
    table.put("c", "b", 1.0)
    ns.move_node("a", 0.016, 0.0)
    ns.remove_node("d")
    ns.add_node("e", 0.002, 0.0)
    done = []
    new = asyncio.run(losses.update_nodes(table, gd, ns, ["a"],
                                          progress=lambda d, t: done.append((d, t))))
    assert new.names == ["a", "b", "c", "e"]
    assert new.get("b", "c") == 1.0                        # untouched pair carried over
    assert new.get("a", "c") == pytest.approx(
        reference_loss(new.f0_hz, 3.1, (0.016, 0.0), (0.0, 0.0203)), abs=1e-4)
    assert new.get("e", "b") == pytest.approx(
        reference_loss(new.f0_hz, 3.1, (0.002, 0.0), (0.006, 0.0)), abs=1e-4)
    assert table.get("a", "b") != new.get("a", "b")        # the original is left alone
    assert done[-1] == (5, 5)                              # a-b a-c a-e b-e c-e
    assert new.header["nodes"][0]["lat"] == 0.016


def test_the_command_prints_progress_as_json_lines(stores, tmp_path):
    env = dict(os.environ)
    code = ("import sys, store; store.GEODATA_DIR, store.NODESETS_DIR, store.LOSSES_DIR = "
            "sys.argv[1:4]; import losses; sys.exit(losses.main(sys.argv[4:]))")
    out = str(tmp_path / "run" / "losses" / "868.bin")
    proc = subprocess.run(
        [sys.executable, "-c", code, store.GEODATA_DIR, store.NODESETS_DIR, store.LOSSES_DIR,
         "--geodata", "flat", "--nodeset", "three", "--band", "868", "--out", out],
        cwd=HERE, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    assert events[-2] == {"event": "progress", "done": 6, "total": 6}
    assert events[-1]["event"] == "done" and events[-1]["path"] == out
    assert slt.Table.read(out).n == 4
    bad = subprocess.run(
        [sys.executable, "-c", code, store.GEODATA_DIR, store.NODESETS_DIR, store.LOSSES_DIR,
         "--geodata", "nothere", "--nodeset", "three"],
        cwd=HERE, capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["event"] == "error"


def test_link_json_replies_become_cells():
    near = {"lb_db": 99.5, "fresnel": {"verdict": "clear"},
            "profile_evidence": {"model": "free space + P.526 diffraction (inside ...)"}}
    assert losses.cell_from_reply(near, 869.525e6, 100) == (
        99.5, slt.FLAG_NEAR_FIELD | slt.FLAG_LOS_CLEAR)
    far = {"lb_db": 140.0, "fresnel": {"verdict": "obstructed"},
           "profile_evidence": {"model": "ITU-R P.1812-8"}}
    assert losses.cell_from_reply(far, 869.525e6, 1000) == (140.0, 0)
    assert losses.cell_from_reply((400, "path leaves the pack"), 1, 1) == (
        slt.NEVER, slt.FLAG_OFF_PACK)
    assert losses.cell_from_reply((400, "path longer than the pack window cap"), 1, 1) == (
        slt.NEVER, slt.FLAG_BEYOND_RADIUS)
    loss, flags = losses.cell_from_reply((400, "the two ends are within 20 m"), 869.525e6, 10)
    assert flags == slt.FLAG_NEAR_FIELD
    assert loss == pytest.approx(ether.fspl_1m_db(869.525e6) + 20)
    with pytest.raises(losses.LossError):
        losses.cell_from_reply((500, "boom"), 1, 1)


# ---- a pack's percentage of locations, through a sidecar of the test's own --

LINK_FIELDS = {"ax", "ay", "bx", "by", "tx_h", "rx_h", "budget_db", "tx_gain_dbi", "rx_gain_dbi",
               "loc_pct"}
SEA = {"name": "sea", "region": {"crs_epsg": 32631, "bbox": [2.9, 0.1, 3.1, 0.3]}, "layers": []}


def fake_sidecar(asked):
    """/api/pack and /link.json as planner-web answers them, for a pack out
    at sea: every pair 120 dB at 90 % of locations and 113 dB at the median,
    and a field link.json does not know refused, as its `deny_unknown_fields`
    refuses one. Each link.json query is kept in `asked`."""
    async def pack(request):
        return web.json_response({"extent": {"minx": 300000, "miny": 0,
                                             "maxx": 700000, "maxy": 100000}})

    async def link(request):
        query = dict(request.query)
        asked.append(query)
        loc = float(query.get("loc_pct", 90))
        if set(query) - LINK_FIELDS or not 1 <= loc <= 99:
            return web.Response(status=400, text="Failed to deserialize query string")
        return web.json_response({"lb_db": 113 + 7 * (loc - 50) / 40,
                                  "fresnel": {"verdict": "grazing"},
                                  "profile_evidence": {"model": losses.P1812_MODEL,
                                                       "buildings_index": "ready"}})

    app = web.Application()
    app.router.add_get("/api/pack", pack)
    app.router.add_get("/link.json", link)
    return app


def serving(app, go):
    """`go(url)` with `app` answering at url, on one loop."""
    async def main():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            await go("http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1])
        finally:
            await runner.cleanup()
    asyncio.run(main())


def test_a_packs_loc_pct_goes_to_every_request_the_header_and_the_cache_key(stores, tmp_path):
    pack = tmp_path / "packs" / "sea"
    pack.mkdir(parents=True)
    (pack / "manifest.json").write_text(json.dumps(SEA))
    geodata.write(geodata.geodata_path("sea"), {"pack": str(pack)})
    ns = nodeset.create("buoys")
    for i in range(3):
        ns.add_node("b%d" % i, 0.2, 3.0 + 0.01 * i, height_m=10)
    moved = ns.copy()
    moved.move_node("b2", 0.21, 3.02)
    asked = []

    async def go(url):
        at90 = geodata.load("sea")
        path90, _ = await losses.compute(at90, ns, "868", url)
        # Without the key, every request is what it was before the key existed.
        assert asked and all(set(q) == {"ax", "ay", "bx", "by", "tx_h", "rx_h"} for q in asked)
        assert path90 == losses.cache_path("sea", ns.geometry_hash(), "868")
        table = slt.Table.read(path90)
        assert table.header["p_loc_pct"] == 90.0 and table.get("b0", "b1") == 120
        # At the median every request says so, and neither the cache nor a
        # donor offers the 90 % table, which would otherwise serve.
        geodata.write(geodata.geodata_path("sea"), {"pack": str(pack), "loc_pct": 50})
        median = geodata.load("sea")
        assert losses.cached(median, ns, "868") is None
        assert losses.nearest_cached(at90, moved, "868")[0] is not None
        assert losses.nearest_cached(median, moved, "868") == (None, None)
        del asked[:]
        path50, hit = await losses.compute(median, ns, "868", url)
        assert not hit and path50 != path90
        assert asked and all(q["loc_pct"] == "50.0" for q in asked)
        table = slt.Table.read(path50)
        assert table.header["p_loc_pct"] == 50.0 and table.get("b0", "b1") == 113
        # Side by side: back at 90 %, the cache still has its table, and a
        # moved node's donor is the table at its own percentage.
        assert losses.cached(at90, ns, "868") == path90
        assert losses.cached(median, ns, "868") == path50
        for gd, pct in ((at90, 90.0), (median, 50.0)):
            donor, fresh = losses.nearest_cached(gd, moved, "868")
            assert donor.header["p_loc_pct"] == pct and fresh == {"b2"}
        # One node's row, as the page's links ask for it, is at the median too.
        del asked[:]
        row = await losses.row(median, ns, "b0", "868", url)
        assert row["b1"][:2] == (113, 113) and all(q["loc_pct"] == "50.0" for q in asked)
        # A table at one percentage is never carried into another.
        with pytest.raises(losses.LossError, match="of locations"):
            await losses.update_nodes(table, at90, moved, ["b2"], url)
    serving(fake_sidecar(asked), go)


def test_a_packs_heights_are_asked_of_its_sidecar_once_its_buildings_are_in(stores, tmp_path):
    pack = tmp_path / "packs" / "sea"
    pack.mkdir(parents=True)
    (pack / "manifest.json").write_text(json.dumps(SEA))
    geodata.write(geodata.geodata_path("sea"), {"pack": str(pack)})
    gd = geodata.load("sea")
    asked, heights = [], []
    app = fake_sidecar(asked)

    async def height(request):
        heights.append(dict(request.query))
        return web.json_response({"h_agl_m": 24.0, "low_m": 22.0, "high_m": 26.0,
                                  "basis": "lod2-building", "detail": {}, "clamped_from_m": None,
                                  "buildings_index": "ready"})
    app.router.add_get("/height.json", height)

    async def go(url):
        got = await losses.estimated_heights(gd, {"b0": (0.2, 3.0), "far": (0.2, 9.0)}, url)
        # The index was asked about before any height, and a point off the
        # pack is not asked at all.
        assert asked and set(got) == {"b0"} and got["b0"]["basis"] == "lod2-building"
        x, y = gd.to_xy(0.2, 3.0)
        assert heights == [{"x": "%.3f" % x, "y": "%.3f" % y}]
    serving(app, go)

    async def old(url):
        with pytest.raises(losses.LossError, match="no height estimate"):
            await losses.estimated_heights(gd, {"b0": (0.2, 3.0)}, url)
    serving(fake_sidecar([]), old)


def test_a_sidecar_with_links_json_is_asked_each_pair_once_its_row_serving_both_ways(
        stores, tmp_path):
    """Where the sidecar lists /links.json, a table's pairs go there, each
    once, from its first end, the ends as the per-pair query gives them (to
    the millimetre, heights as %g), and each row fills both cells: its flags
    as a reply's verdicts, a refusal as /link.json's own."""
    pack = tmp_path / "packs" / "sea"
    pack.mkdir(parents=True)
    (pack / "manifest.json").write_text(json.dumps(SEA))
    geodata.write(geodata.geodata_path("sea"), {"pack": str(pack), "loc_pct": 50})
    gd = geodata.load("sea")
    ns = nodeset.create("buoys")
    for i in range(3):
        ns.add_node("b%d" % i, 0.2, 3.0 + 0.01 * i, height_m=10 + i / 3)
    asked, batches = [], []
    app = web.Application()

    async def pack(request):
        return web.json_response({"extent": {"minx": 300000, "miny": 0,
                                             "maxx": 700000, "maxy": 100000},
                                  "link_batch": "/links.json"})

    async def links(request):
        body = await request.json()
        batches.append(body)
        n = len(body["pairs"])
        # The last pair refused, the first near field and clear.
        return web.json_response({"buildings_index": "ready", "read": "once", "compute_ms": 1,
                                  "lb_db": [100.0 + k for k in range(n - 1)] + [None],
                                  "flags": [3] + [0] * (n - 1),
                                  "refused": [[n - 1, 400, "path leaves the pack"]]})

    async def link(request):
        asked.append(dict(request.query))
        return web.Response(status=400, text="the probe")
    app.router.add_get("/api/pack", pack)
    app.router.add_post("/links.json", links)
    app.router.add_get("/link.json", link)

    async def go(url):
        path, _ = await losses.compute(gd, ns, "868", url)
        # /link.json is asked the probe across the pack, for the index, and no pair.
        assert len(asked) <= 1 and len(batches) == 1
        body = batches[0]
        assert body["loc_pct"] == 50.0
        names = sorted(ns.nodes)
        for name, (x, y, h) in zip(names, body["nodes"]):
            nx, ny = gd.to_xy(ns.nodes[name]["lat"], ns.nodes[name]["lon"])
            assert (x, y, h) == (float("%.3f" % nx), float("%.3f" % ny),
                                 float("%g" % ns.nodes[name]["height_m"]))
        pairs = [(names[i], names[j]) for i, j in body["pairs"]]
        assert sorted(tuple(sorted(p)) for p in pairs) == [("b0", "b1"), ("b0", "b2"), ("b1", "b2")]
        table = slt.Table.read(path)
        for k, (a, b) in enumerate(pairs):
            for x, y in ((a, b), (b, a)):
                cell = table.cell(x, y)
                if k == len(pairs) - 1:
                    assert (table.loss[cell], table.flags[cell]) == (slt.NEVER, slt.FLAG_OFF_PACK)
                else:
                    assert table.loss[cell] == 100.0 + k
                    assert table.flags[cell] == (slt.FLAG_NEAR_FIELD | slt.FLAG_LOS_CLEAR
                                                 if k == 0 else 0)
    serving(app, go)


def test_shadowing_over_a_pack_that_is_no_median_is_warned_about_not_refused(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(SEA))

    def sea(**keys):
        return geodata.Geodata("sea", geodata.parse({"pack": str(tmp_path), **keys}, "sea"))
    assert losses.shadowing_warning(sea()) is None
    assert losses.shadowing_warning(sea(loc_pct=50)) is None
    assert losses.shadowing_warning(sea(shadowing_db=7, loc_pct=50)) is None
    said = losses.shadowing_warning(sea(shadowing_db=7))
    assert "at 90 % of locations" in said and "loc_pct: 50" in said
    assert "at 70 % of locations" in losses.shadowing_warning(sea(shadowing_db=7, loc_pct=70))
    flat = geodata.Geodata("flat", geodata.parse({"synthetic": {}, "shadowing_db": 7}, "flat"))
    assert losses.shadowing_warning(flat) is None


# ---- a pack, through a real sidecar --------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_sidecar(ready):
    if not (os.path.isfile(PLANNER_WEB) and os.path.isfile(os.path.join(BERLIN_PACK, "manifest.json"))):
        pytest.skip("no planner-web build in planner/ or no berlin-city geodata")
    port = free_port()
    proc = subprocess.Popen([PLANNER_WEB, "--pack", BERLIN_PACK, "--host", "127.0.0.1",
                             "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    try:
        asyncio.run(ready(base))
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def sidecar():
    yield from run_sidecar(wait_ready)


@pytest.fixture
def fresh_sidecar():
    """A sidecar that answers, its building index still loading."""
    yield from run_sidecar(wait_answering)


async def wait_answering(base):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for _ in range(600):
            try:
                async with session.get(base + "/api/pack") as resp:
                    if resp.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("planner-web at %s never answered" % base)


async def wait_ready(base):
    """Until a link reply rests on the building index, not the raster alone:
    before that, the same pair can come back with a different number."""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for _ in range(600):
            try:
                async with session.get(base + "/link.json", params={
                        "ax": 392000, "ay": 5820000, "bx": 392500, "by": 5820200}) as resp:
                    if resp.status == 200:
                        reply = await resp.json(content_type=None)
                        if reply["profile_evidence"].get("buildings_index") == "ready":
                            return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("planner-web at %s never became ready" % base)


async def link(base, params):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(base + "/link.json", params=params) as resp:
            return resp.status, await resp.json(content_type=None)


def test_a_pack_table_cell_is_what_link_json_says(stores, sidecar):
    geodata.write(geodata.geodata_path("berlin"), {"pack": BERLIN_PACK})
    gd = geodata.load("berlin")
    ns = nodeset.create("mitte")
    ns.add_node("alex", 52.5219, 13.4132, height_m=12)
    ns.add_node("hack", 52.5245, 13.4020, height_m=8)
    ns.add_node("jann", 52.5170, 13.4190, height_m=4)
    ns.add_node("near", 52.52195, 13.41325, height_m=3)  # a few metres from alex
    steps = []
    table = asyncio.run(losses.pack_table(gd, ns, "868", sidecar,
                                          progress=lambda d, t: steps.append((d, t))))
    assert steps[-1] == (6, 6)
    assert table.header["model"] == "P.1812-8" and table.f0_hz == losses.LINK_F0_HZ
    assert table.header["pack_manifest_hash"] == gd.pack_manifest_hash

    (ax, ay), (bx, by) = (gd.to_xy(ns.nodes[n]["lat"], ns.nodes[n]["lon"])
                          for n in ("alex", "hack"))
    status, reply = asyncio.run(link(sidecar, {
        "ax": "%.3f" % ax, "ay": "%.3f" % ay, "bx": "%.3f" % bx, "by": "%.3f" % by,
        "tx_h": 12, "rx_h": 8}))
    assert status == 200
    assert table.get("alex", "hack") == pytest.approx(reply["lb_db"], abs=1e-3)
    status, reply = asyncio.run(link(sidecar, {
        "ax": "%.3f" % bx, "ay": "%.3f" % by, "bx": "%.3f" % ax, "by": "%.3f" % ay,
        "tx_h": 8, "rx_h": 12}))
    assert status == 200
    assert table.get("hack", "alex") == pytest.approx(reply["lb_db"], abs=1e-3)
    for a in table.names:
        for b in table.names:
            if a != b:
                assert math.isfinite(table.get(a, b))
    d = gd.distance_m((52.5219, 13.4132), (52.52195, 13.41325))
    assert d < 20
    assert table.flag("alex", "near") & slt.FLAG_NEAR_FIELD
    assert table.get("alex", "near") == pytest.approx(losses.free_space_db(losses.LINK_F0_HZ, d),
                                                      abs=1e-3)

    # Off the pack, and beyond the radius, are never heard, flagged, and not asked.
    ns.add_node("potsdam", 52.3906, 13.0645)
    moved = asyncio.run(losses.update_nodes(table, gd, ns, [], sidecar, radius_m=2000))
    assert moved.get("alex", "hack") == table.get("alex", "hack")
    assert moved.get("potsdam", "alex") == slt.NEVER
    assert moved.flag("potsdam", "alex") == slt.FLAG_OFF_PACK


def test_a_table_begun_while_the_sidecar_indexes_waits_for_the_index(stores, fresh_sidecar):
    """A sidecar just started answers from the clutter raster until its
    building index is in; a table begun then waits, says why, and its cells
    are the numbers link.json gives once the index is ready."""
    geodata.write(geodata.geodata_path("berlin"), {"pack": BERLIN_PACK})
    gd = geodata.load("berlin")
    ns = nodeset.create("mitte")
    ns.add_node("alex", 52.5219, 13.4132, height_m=12)
    ns.add_node("hack", 52.5245, 13.4020, height_m=8)
    ns.add_node("jann", 52.5170, 13.4190, height_m=4)
    notices = []
    table = asyncio.run(losses.pack_table(gd, ns, "868", fresh_sidecar,
                                          notice=notices.append))
    assert notices and "indexing" in notices[0]
    xy = {n: gd.to_xy(ns.nodes[n]["lat"], ns.nodes[n]["lon"]) for n in ns.nodes}
    for a in ns.nodes:
        for b in ns.nodes:
            if a == b:
                continue
            status, reply = asyncio.run(link(fresh_sidecar, {
                "ax": "%.3f" % xy[a][0], "ay": "%.3f" % xy[a][1],
                "bx": "%.3f" % xy[b][0], "by": "%.3f" % xy[b][1],
                "tx_h": "%g" % ns.nodes[a]["height_m"],
                "rx_h": "%g" % ns.nodes[b]["height_m"]}))
            assert status == 200 and reply["profile_evidence"]["buildings_index"] == "ready"
            assert table.get(a, b) == pytest.approx(reply["lb_db"], abs=1e-3)


def test_a_pack_has_only_the_868_table(stores):
    if not os.path.isfile(os.path.join(BERLIN_PACK, "manifest.json")):
        pytest.skip("no berlin-city geodata")
    geodata.write(geodata.geodata_path("berlin"), {"pack": BERLIN_PACK})
    gd = geodata.load("berlin")
    ns = nodeset.create("empty")
    assert losses.bands_of(ns, gd) == ["868"]
    with pytest.raises(losses.LossError, match="869.525 MHz only"):
        asyncio.run(losses.pack_table(gd, ns, "433", "http://127.0.0.1:1"))
    with pytest.raises(losses.LossError, match="sidecar"):
        asyncio.run(losses.pack_table(gd, ns, "868", None))


def test_two_processes_writing_one_table_do_not_trip_over_each_other(tmp_path):
    """Two runs of one checkout may write the same cached table at once: each
    through a temporary of its own."""
    import subprocess
    import sys
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import losses, slt\n"
            "class T:\n"
            "    def write(self, p):\n"
            "        open(p, 'wb').write(b'x' * 4096)\n"
            "for _ in range(200):\n"
            "    losses.write_table(T(), %r)\n") % (os.path.dirname(os.path.abspath(__file__)),
                                               str(tmp_path / "t.bin"))
    procs = [subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE)
             for _ in range(2)]
    errors = [p.communicate()[1].decode() for p in procs]
    assert [p.returncode for p in procs] == [0, 0], errors
    assert (tmp_path / "t.bin").read_bytes() == b"x" * 4096
