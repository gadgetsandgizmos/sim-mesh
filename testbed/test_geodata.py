"""Geodata: the file, the projections between degrees and metres, the
extent, and importing a pack."""

import hashlib
import io
import json
import os
import pathlib
import shutil
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import geodata  # noqa: E402
import nodeset as nodeset_module  # noqa: E402
import store  # noqa: E402

# Corners of the berlin-city pack as planner-web's /api/pack states them:
# its extent in EPSG:32633 and the same points in WGS84, by the planner's
# own proj4rs.
BERLIN_CORNERS = [
    ((377598.949424815, 5808681.7827512985), (52.414648212657944, 13.20029149508144)),
    ((405178.949424815, 5829291.7827512985), (52.60535686371495, 13.599784967616484)),
]


def test_utm_matches_the_planner_both_ways():
    tm = geodata.TransverseMercator(*geodata.utm_zone_of(32633))
    for (x, y), (lat, lon) in BERLIN_CORNERS:
        got_lat, got_lon = tm.inverse(x, y)
        assert got_lat == pytest.approx(lat, abs=1e-9)
        assert got_lon == pytest.approx(lon, abs=1e-9)
        got_x, got_y = tm.forward(lat, lon)
        assert got_x == pytest.approx(x, abs=1e-3)
        assert got_y == pytest.approx(y, abs=1e-3)


def test_utm_south_and_etrs89_zones_round_trip():
    for epsg, point in ((32733, (-33.9249, 18.4241)), (25833, (52.52, 13.405)),
                        (32601, (10.0, -177.5))):
        tm = geodata.TransverseMercator(*geodata.utm_zone_of(epsg))
        lat, lon = tm.inverse(*tm.forward(*point))
        assert (lat, lon) == pytest.approx(point, abs=1e-10)
    with pytest.raises(store.StoreError):
        geodata.utm_zone_of(4326)


def manifest(name="tiny", epsg=32633):
    return {"name": name, "region": {"bbox": [13.3, 52.4, 13.5, 52.6], "crs_epsg": epsg},
            "layers": ["TerrainDtm", {"kind": "Roads"}]}


def write_pack(name="tiny", epsg=32633, comment=None):
    """Pack geodata in the store: its directory, the manifest and the
    geodata file that says the pack is the directory."""
    pack = geodata.geodata_dir(name)
    os.makedirs(pack)
    with open(os.path.join(pack, "manifest.json"), "w") as handle:
        handle.write(json.dumps(manifest(epsg=epsg)))
    geodata.write(geodata.geodata_path(name), {"pack": "."}, comment)
    return pathlib.Path(pack)


def test_a_pack_is_the_geodata_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    pack = write_pack()
    assert geodata.names() == ["tiny"]
    gd = geodata.load("tiny")
    assert gd.is_pack and gd.crs_epsg == 32633
    assert gd.pack_dir == str(pack)
    assert len(gd.pack_manifest_hash) == 64
    x, y = gd.to_xy(52.52, 13.405)
    assert gd.to_latlon(x, y) == pytest.approx((52.52, 13.405), abs=1e-10)
    assert gd.as_dict()["layers"] == ["TerrainDtm", "Roads"]
    assert gd.bbox == [13.3, 52.4, 13.5, 52.6]
    assert gd.holds(52.5, 13.4) and not gd.holds(0, 0)
    before = gd.content_hash
    (pack / "manifest.json").write_text((pack / "manifest.json").read_text() + " ")
    assert geodata.load("tiny").content_hash != before


def test_a_pack_without_its_pack_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path))
    geodata.write(geodata.geodata_path("gone"), {"pack": "../nowhere"})
    with pytest.raises(store.StoreError, match="manifest.json"):
        geodata.load("gone")


def test_synthetic_ground_is_a_nautical_mile_to_the_minute_at_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path))
    geodata.write(geodata.geodata_path("flat"),
                  {"synthetic": {"exponent": 3.5, "extent_m": 10000}})
    gd = geodata.load("flat")
    assert not gd.is_pack and gd.exponent == 3.5 and gd.terrain == "flat"
    assert gd.to_xy(1 / 60, 2 / 60) == pytest.approx((2 * 1852.0, 1852.0))
    assert gd.to_latlon(*gd.to_xy(0.01, -0.02)) == pytest.approx((0.01, -0.02))
    half = 5000 / 1852.0 / 60
    assert gd.bbox == pytest.approx([-half, -half, half, half])
    assert gd.origin == pytest.approx((0, 0))
    assert gd.holds(0.04, -0.04) and not gd.holds(0.05, 0)
    assert geodata.read(geodata.geodata_path("flat")).data == gd.data


def test_a_geodata_file_must_say_what_it_is(tmp_path):
    path = tmp_path / "odd.yaml"
    path.write_text("colour: blue\n")
    with pytest.raises(store.StoreError):
        geodata.read(str(path))
    path.write_text("synthetic: { terrain: mountains }\n")
    with pytest.raises(store.StoreError, match="terrain"):
        geodata.read(str(path))
    with pytest.raises(store.StoreError):
        geodata.geodata_path("Not A Name")


def test_a_ground_figure_no_file_could_hold_is_refused(tmp_path):
    # NaN and infinity read as numbers, and store.scalar cannot write them;
    # a NaN extent also passed the check that it is above 0.
    path = tmp_path / "g.yaml"
    for ground, match in (("{ exponent: .nan }", "exponent is a number, not nan"),
                          ("{ exponent: -.inf }", "exponent is a number, not -inf"),
                          ("{ extent_m: .nan }", "extent_m is a number, not nan"),
                          ("{ extent_m: .inf }", "extent_m is a number, not inf")):
        path.write_text("synthetic: %s\n" % ground)
        with pytest.raises(store.StoreError, match=match):
            geodata.read(str(path))


def test_a_ground_figure_that_is_no_number_is_refused_by_its_key(tmp_path):
    # float() of a word raised a ValueError, and of nothing a TypeError,
    # where every other bad value is a StoreError naming its key.
    path = tmp_path / "g.yaml"
    for ground, match in (("{ extent_m: wide }", "extent_m is a number, not 'wide'"),
                          ("{ exponent: steep }", "exponent is a number, not 'steep'"),
                          ("{ extent_m: null }", "extent_m is a number, not None"),
                          ("{ exponent: [2, 3] }", r"exponent is a number, not \[2, 3\]")):
        path.write_text("synthetic: %s\n" % ground)
        with pytest.raises(store.StoreError, match=match):
            geodata.read(str(path))


def test_a_copied_pack_still_names_its_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    pack = write_pack()
    target = tmp_path / "runs" / "a" / "geodata.yaml"
    geodata.write_copy(geodata.load("tiny"), str(target))
    assert geodata.read(str(target), "tiny").pack_dir == str(pack)
    text = geodata.rebase_text(target.read_text(), str(target.parent), str(tmp_path / "elsewhere"))
    assert text == 'pack: "../geodata/tiny"\n'


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)


def test_a_pack_zip_is_imported_into_its_own_directory_and_named(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    zipped = tmp_path / "up.zip"
    make_zip(zipped, {"berlin/manifest.json": json.dumps(manifest("berlin")),
                      "berlin/terrain/a.bin": "x"})
    gd = geodata.import_zip(str(zipped), "berlin")
    here = tmp_path / "geodata" / "berlin"
    assert gd.is_pack and gd.pack_dir == str(here)
    assert (here / "terrain" / "a.bin").read_text() == "x"
    assert (here / "geodata.yaml").read_text() == '# imported pack berlin\npack: "."\n'
    with pytest.raises(store.StoreError, match="already"):
        geodata.import_zip(str(zipped), "berlin")
    # With no name given, the manifest's.
    make_zip(zipped, {"manifest.json": json.dumps(manifest("Other Place"))})
    assert geodata.import_zip(str(zipped)).name == "other-place"


def test_a_pack_zip_without_a_usable_manifest_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    zipped = tmp_path / "up.zip"
    make_zip(zipped, {"a/b.bin": "x"})
    with pytest.raises(store.StoreError, match="manifest"):
        geodata.import_zip(str(zipped), "nothing")
    make_zip(zipped, {"manifest.json": json.dumps({"region": {"crs_epsg": 4326, "bbox": [0, 0, 1, 1]}})})
    with pytest.raises(store.StoreError, match="not usable"):
        geodata.import_zip(str(zipped), "nothing")
    make_zip(zipped, {"manifest.json": "{}", "../escape": "x"})
    with pytest.raises(store.StoreError):
        geodata.import_zip(str(zipped), "nothing")
    make_zip(zipped, {"geodata.yaml": "pack: ../out\n", "manifest.json": "{}"})
    with pytest.raises(store.StoreError, match="leaves"):
        geodata.import_zip(str(zipped), "nothing")
    make_zip(zipped, {"manifest.json": "{}"})
    with pytest.raises(store.StoreError, match="usable geodata name"):
        geodata.import_zip(str(zipped))
    assert os.listdir(tmp_path / "geodata") == []


def nodes_manifest(name="city"):
    out = manifest(name)
    out["layers"] = [{"kind": "TerrainDtm", "path": "dtm.tif"},
                     {"kind": "Nodes", "path": "nodes.bin"}]
    out["licenses"] = [{"source": "Copernicus GLO-30 DSM", "notice": "(c) DLR"},
                       {"source": "Deployed mesh nodes (community adverts)", "notice": "adverts"}]
    return out


def test_a_bare_planner_pack_loses_its_nodes_on_the_way_in(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    zipped = tmp_path / "up.zip"
    make_zip(zipped, {"city/manifest.json": json.dumps(nodes_manifest()),
                      "city/dtm.tif": "t", "city/nodes.bin": "n"})
    gd = geodata.import_zip(str(zipped))
    assert gd.name == "city"
    assert sorted(os.listdir(tmp_path / "geodata" / "city")) == ["dtm.tif", "geodata.yaml",
                                                                 "manifest.json"]
    assert [layer["kind"] for layer in gd.manifest["layers"]] == ["TerrainDtm"]
    assert [n["source"] for n in gd.manifest["licenses"]] == ["Copernicus GLO-30 DSM"]


def test_geodata_goes_out_and_comes_back_as_a_sim_mesh_geodata_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    pack = tmp_path / "geodata" / "city"
    (pack / "sub").mkdir(parents=True)
    (pack / "manifest.json").write_text(json.dumps(nodes_manifest()))
    (pack / "dtm.tif").write_text("t")
    (pack / "nodes.bin").write_text("n")
    (pack / "sub" / "x.bin").write_text("x")
    (pack / ".hidden").write_text("h")
    geodata.write(geodata.geodata_path("city"), {"pack": "."})
    gd = geodata.load("city")
    assert gd.as_dict()["layers"] == ["TerrainDtm"]
    assert gd.as_dict()["licences"] == [{"source": "Copernicus GLO-30 DSM", "notice": "(c) DLR"}]

    class Unseekable:
        def __init__(self):
            self.data = bytearray()

        def write(self, b):
            self.data += b
            return len(b)

        def flush(self):
            pass

    out = Unseekable()
    geodata.export_zip(gd, out)
    zipped = tmp_path / "city.zip"
    zipped.write_bytes(bytes(out.data))
    with zipfile.ZipFile(zipped) as zf:
        assert sorted(zf.namelist()) == ["geodata.yaml", "pack/dtm.tif", "pack/manifest.json",
                                        "pack/sub/x.bin"]
        assert zf.read("geodata.yaml").decode() == "# geodata city\npack: pack\n"
        assert "Nodes" not in zf.read("pack/manifest.json").decode()

    with pytest.raises(store.StoreError, match="already"):
        geodata.import_zip(str(zipped))
    back = geodata.import_zip(str(zipped), "city-two")
    assert back.is_pack and back.pack_dir == str(tmp_path / "geodata" / "city-two")
    assert (tmp_path / "geodata" / "city-two" / "sub" / "x.bin").read_text() == "x"
    assert back.bbox == gd.bbox and back.crs_epsg == gd.crs_epsg

    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.5}})
    out = Unseekable()
    geodata.export_zip(geodata.load("flat"), out)
    zipped.write_bytes(bytes(out.data))
    with zipfile.ZipFile(zipped) as zf:
        assert zf.namelist() == ["geodata.yaml"]
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "other"))
    flat = geodata.import_zip(str(zipped))
    assert flat.name == "flat" and flat.exponent == 3.5


# ---- renaming and deleting --------------------------------------------------

def own_store(tmp_path, monkeypatch):
    for attr, sub in (("GEODATA_DIR", "geodata"), ("RUNS_DIR", "runs"),
                      ("SNAPSHOTS_DIR", "snapshots")):
        monkeypatch.setattr(store, attr, str(tmp_path / sub))


def test_a_rename_moves_the_directory_pack_and_comment_with_it(tmp_path, monkeypatch):
    own_store(tmp_path, monkeypatch)
    write_pack(comment="built from sources")
    geodata.rename("tiny", "small")
    assert geodata.names() == ["small"]
    assert not (tmp_path / "geodata" / "tiny").exists()
    assert geodata.load("small").pack_dir == str(tmp_path / "geodata" / "small")
    assert (tmp_path / "geodata" / "small" / "geodata.yaml").read_text() \
        .startswith("# built from sources\n")
    with pytest.raises(store.StoreError, match="no geodata"):
        geodata.rename("tiny", "other")
    write_pack("other")
    with pytest.raises(store.StoreError, match="already"):
        geodata.rename("small", "other")


def test_geodata_a_snapshot_stands_on_is_neither_renamed_nor_deleted(tmp_path, monkeypatch):
    own_store(tmp_path, monkeypatch)
    pack = write_pack()
    geodata.write_copy(geodata.load("tiny"), str(tmp_path / "snapshots" / "s1" / "geodata.yaml"))
    with pytest.raises(store.StoreError,
                       match="snapshot s1 stands on tiny's pack: delete it before renaming tiny"):
        geodata.rename("tiny", "small")
    with pytest.raises(store.StoreError,
                       match="snapshot s1 stands on tiny's pack: delete it before deleting tiny"):
        geodata.delete("tiny")
    assert geodata.names() == ["tiny"] and pack.is_dir()
    shutil.rmtree(str(tmp_path / "snapshots" / "s1"))
    geodata.delete("tiny")
    assert not pack.exists() and geodata.names() == []


# ---- the keys beyond pack: and synthetic: ----------------------------------

PLAIN = "synthetic:\n  terrain: flat\n  exponent: 2.7\n  extent_m: 150000\n"
ROUGH = PLAIN + "shadowing_db: 6.5\nshadowing_seed: 3\n"


def hash_before_the_keys(gd):
    """The content hash as it was before a geodata could say more than its
    ground: what every table cached until then is keyed by."""
    text = json.dumps([gd.data, gd.pack_manifest_hash], sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def hand_written(name, text):
    """A geodata file written by hand, in the geodata's own directory."""
    path = pathlib.Path(geodata.geodata_path(name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_geodata_without_the_keys_is_what_it_was_and_is_written_back_byte_for_byte(
        tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    write_pack()
    for name, text in (("flat", PLAIN), ("tiny", 'pack: "."\n')):
        path = hand_written(name, text)
        gd = geodata.load(name)
        assert not set(geodata.STATED) & (set(gd.data) | set(gd.as_dict()))
        assert gd.content_hash == hash_before_the_keys(gd)
        assert (gd.shadowing_db, gd.shadowing_seed) == (0, 0)
        geodata.write(str(path), gd.data)
        assert path.read_text() == text


def test_shadowing_round_trips_and_is_no_part_of_the_ground(tmp_path, monkeypatch):
    own_store(tmp_path, monkeypatch)
    rough_path = hand_written("rough", ROUGH)
    hand_written("flat", PLAIN)
    rough, flat = geodata.load("rough"), geodata.load("flat")
    assert (rough.shadowing_db, rough.shadowing_seed) == (6.5, 3)
    assert (rough.as_dict()["shadowing_db"], rough.as_dict()["shadowing_seed"]) == (6.5, 3)
    # A layer over the tables, not the ground: a table cached for the one
    # serves the other.
    assert rough.content_hash == flat.content_hash
    geodata.write(geodata.geodata_path("rough"), rough.data)
    assert rough_path.read_text() == ROUGH
    # And through everything else that writes a geodata file anew.
    copy_path = tmp_path / "runs" / "r" / "geodata.yaml"
    geodata.write_copy(rough, str(copy_path))
    assert geodata.read(str(copy_path)).data == rough.data
    assert geodata.rebase_text(ROUGH, str(tmp_path), str(tmp_path / "runs")) == ROUGH
    geodata.rename("rough", "rougher")
    assert geodata.load("rougher").data == rough.data


def test_a_packs_keys_go_where_its_pack_goes(tmp_path, monkeypatch):
    own_store(tmp_path, monkeypatch)
    write_pack()
    geodata.write(geodata.geodata_path("tiny"), {"pack": ".", "shadowing_db": 7})
    geodata.rename("tiny", "small")                 # the directory, its pack and keys with it
    small = geodata.load("small")
    assert small.pack_dir == str(tmp_path / "geodata" / "small") and small.shadowing_db == 7
    out = io.BytesIO()
    geodata.export_zip(small, out)
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as zf:
        assert zf.read("geodata.yaml").decode() == "# geodata small\npack: pack\nshadowing_db: 7\n"
    zipped = tmp_path / "small.zip"
    zipped.write_bytes(out.getvalue())
    back = geodata.import_zip(str(zipped), "again")
    assert back.is_pack and back.shadowing_db == 7


def test_shadowing_that_is_no_spread_or_no_seed_is_refused(tmp_path):
    path = tmp_path / "g.yaml"
    for extra, match in (("shadowing_db: -1\n", "0 or more"),
                         ("shadowing_db: loud\n", "is a number"),
                         ("shadowing_db: .nan\n", "is a number"),
                         ("shadowing_seed: 1.5\n", "whole number"),
                         ("shadowing_seed: yes\n", "whole number"),
                         # Misspelt, the model would be silently off.
                         ("shadowing_dB: 7\n", "did you mean shadowing_db"),
                         ("shadow_db: 7\n", "did you mean shadowing_db"),
                         ("Shadowing_Seed: 3\n", "did you mean shadowing_seed")):
        path.write_text(PLAIN + extra)
        with pytest.raises(store.StoreError, match=match):
            geodata.read(str(path))
    # A key unlike any of them is passed over, as it always was.
    path.write_text(PLAIN + "colour: blue\n")
    assert geodata.dump(geodata.read(str(path)).data) == PLAIN


def test_a_spread_is_the_number_a_copy_of_the_file_reads_back(tmp_path):
    path, copy_path = tmp_path / "g.yaml", tmp_path / "copy.yaml"
    path.write_text(PLAIN + "shadowing_db: 6.1234567891234\n")
    gd = geodata.read(str(path))
    geodata.write_copy(gd, str(copy_path))
    assert geodata.read(str(copy_path)).shadowing_db == gd.shadowing_db == 6.123456789


def test_loc_pct_is_a_packs_round_trips_and_is_part_of_the_ground(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    # Two geodata on one pack's contents, at 90 % and at the median.
    write_pack("tiny")
    write_pack("median")
    geodata.write(geodata.geodata_path("median"), {"pack": ".", "loc_pct": 50, "shadowing_db": 7})
    assert (tmp_path / "geodata" / "median" / "geodata.yaml").read_text() == \
        'pack: "."\nloc_pct: 50\nshadowing_db: 7\n'
    at90, median = geodata.load("tiny"), geodata.load("median")
    assert (at90.loc_pct, median.loc_pct) == (None, 50)
    assert median.as_dict()["loc_pct"] == 50 and "loc_pct" not in at90.as_dict()
    # The table's own, unlike the shadowing: another percentage, other tables.
    assert median.content_hash != at90.content_hash
    # It goes where the pack goes.
    out = io.BytesIO()
    geodata.export_zip(median, out)
    zipped = tmp_path / "median.zip"
    zipped.write_bytes(out.getvalue())
    back = geodata.import_zip(str(zipped), "again")
    assert (back.loc_pct, back.shadowing_db) == (50, 7)
    path = tmp_path / "g.yaml"
    for text, match in (("pack: x\nloc_pct: 0\n", "1 to 99"),
                        ("pack: x\nloc_pct: 99.5\n", "1 to 99"),
                        ("pack: x\nloc_pct: most\n", "is a number"),
                        ("pack: x\nloc_pc: 50\n", "did you mean loc_pct"),
                        (PLAIN + "loc_pct: 50\n", "a pack's")):
        path.write_text(text)
        with pytest.raises(store.StoreError, match=match):
            geodata.read(str(path))


def test_synthetic_ground_goes_whatever_stands_on_it(tmp_path, monkeypatch):
    own_store(tmp_path, monkeypatch)
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.0}})
    geodata.write_copy(geodata.load("flat"), str(tmp_path / "runs" / "r1" / "geodata.yaml"))
    geodata.delete("flat")
    assert geodata.names() == [] and not (tmp_path / "geodata" / "flat").exists()


def test_a_pack_another_geodata_stands_on_is_not_moved_from_under_it(tmp_path, monkeypatch):
    own_store(tmp_path, monkeypatch)
    pack = write_pack("berlin")
    # The same pack at the median, without a second copy of it.
    geodata.write(geodata.geodata_path("median"), {"pack": "../berlin", "loc_pct": 50})
    median = geodata.load("median")
    assert median.pack_dir == str(pack) and median.loc_pct == 50
    for go, doing in ((lambda: geodata.delete("berlin"), "deleting"),
                      (lambda: geodata.rename("berlin", "b2"), "renaming")):
        with pytest.raises(store.StoreError,
                           match="geodata median stands on berlin's pack: delete it before %s berlin"
                           % doing):
            go()
    # The one that only names the pack goes without it, a run on the pack or
    # not: nothing the run needs is in its directory.
    geodata.write_copy(median, str(tmp_path / "runs" / "r1" / "geodata.yaml"))
    geodata.delete("median")
    assert pack.is_dir() and geodata.names() == ["berlin"]
    with pytest.raises(store.StoreError, match="run r1 stands on berlin's pack"):
        geodata.delete("berlin")


def test_a_delete_takes_what_stands_on_the_pack_and_nodesets_left_on_nothing(tmp_path, monkeypatch):
    own_store(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "NODESETS_DIR", str(tmp_path / "nodesets"))
    write_pack("berlin")                                  # 13.3–13.5 E, 52.4–52.6 N
    geodata.write(geodata.geodata_path("median"), {"pack": "../berlin", "loc_pct": 50})
    geodata.write(geodata.geodata_path("flat"), {"synthetic": {"exponent": 3.0}})
    berlin = geodata.load("berlin")
    geodata.write_copy(berlin, str(tmp_path / "runs" / "r1" / "geodata.yaml"))
    geodata.write_copy(berlin, str(tmp_path / "snapshots" / "s1" / "geodata.yaml"))
    geodata.write_copy(geodata.load("flat"), str(tmp_path / "runs" / "r2" / "geodata.yaml"))
    for name, places in (("mitte", [(52.5, 13.4)]),               # on berlin alone
                         ("both", [(52.5, 13.4), (0.0, 0.0)]),    # on berlin and on flat
                         ("elsewhere", [(48.1, 11.6)])):          # on neither
        ns = nodeset_module.create(name)
        for i, (lat, lon) in enumerate(places):
            ns.add_node("n%d" % i, lat, lon)
        ns.save()
    assert geodata.taken_with(["berlin"]) == {
        "geodata": ["median"], "runs": ["r1"], "snapshots": ["s1"], "nodesets": ["mitte"]}
    # Geodata that only names a pack takes nothing with it but what is left on no geodata.
    assert geodata.taken_with(["median"]) == {
        "geodata": [], "runs": [], "snapshots": [], "nodesets": []}
    assert geodata.taken_with(["berlin", "flat"])["nodesets"] == ["both", "mitte"]

