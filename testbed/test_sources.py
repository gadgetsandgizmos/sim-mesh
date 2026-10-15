"""Building a pack from its sources: the rectangle's grid and what it is
refused for, which tiles and which extract a rectangle needs, the cache's
fetches (resumed, once, a missing file remembered), and a build end to end
against a host and a compiler of the test's own."""

import asyncio
import json
import os
import re
import shutil
import sys
import zipfile

import aiohttp
import pytest
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import crs  # noqa: E402
import geodata  # noqa: E402
import packbuild  # noqa: E402
import sourcefile  # noqa: E402
import sources  # noqa: E402
import store  # noqa: E402

SHIPPED = sourcefile.load((sourcefile.SHIPPED,))

MITTE = [13.38, 52.51, 13.42, 52.53]


def square(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]


def index():
    """A Geofabrik index with Germany, Brandenburg round Berlin, and Berlin."""
    def feature(id_, ring):
        return {"type": "Feature", "properties": {
            "id": id_, "name": id_.title(),
            "urls": {"pbf": "/geofabrik/%s-latest.osm.pbf" % id_}},
            "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}}
    return {"features": [feature("germany", square(5.8, 47.2, 15.1, 55.1)),
                         feature("brandenburg", square(11.2, 51.3, 14.8, 53.6)),
                         feature("berlin", square(13.08, 52.33, 13.77, 52.68))]}


FEED = "\n".join('<link href="https://gdi.berlin.de/data/a_lod2/atom/LoD2_%d_%d.zip"/>' % (e, n)
                 for e in range(388, 394) for n in range(5818, 5823))


def test_a_rectangle_is_a_grid_in_the_zone_of_its_centre():
    got = sources.check(MITTE, 30)
    assert got["zone"] == 33 and got["epsg"] == 32633
    nx, ny = got["cells"]
    assert 85 < nx < 95 and 70 < ny < 80
    with pytest.raises(store.StoreError, match="wider than its UTM zone"):
        sources.check([10, 50, 17, 51], 30)
    with pytest.raises(store.StoreError, match="million"):
        sources.check([12, 50, 17, 54], 10)
    with pytest.raises(store.StoreError, match="30 m or 10 m"):
        sources.check(MITTE, 20)
    with pytest.raises(store.StoreError, match="west to east"):
        sources.check([13.42, 52.51, 13.38, 52.53], 30)


def test_tiles_are_named_by_their_south_west_corner_over_the_grids_reach():
    tm = geodata.TransverseMercator(33, True, geodata.WGS84)
    hull = sources.degree_hull([12.96, 52.25, 13.85, 52.79], tm)
    glo30, worldcover = SHIPPED["glo30"], SHIPPED["worldcover"]
    assert [f.name for f in sources.template_files(glo30, hull)] == [
        "Copernicus_DSM_COG_10_N52_00_E012_00_DEM.tif",
        "Copernicus_DSM_COG_10_N52_00_E013_00_DEM.tif"]
    assert [f.name for f in sources.template_files(glo30, [-58.5, -34.7, -58.3, -34.5])] == [
        "Copernicus_DSM_COG_10_S35_00_W059_00_DEM.tif"]
    assert [f.name for f in sources.template_files(worldcover, hull)] == [
        "ESA_WorldCover_10m_2021_v200_N51E012_Map.tif"]
    assert sources.template_files(glo30, hull)[0].url.endswith(
        "/Copernicus_DSM_COG_10_N52_00_E012_00_DEM/Copernicus_DSM_COG_10_N52_00_E012_00_DEM.tif")
    # A cached name is read back as the tile it is.
    m = sources.template_pattern(glo30).match("Copernicus_DSM_COG_10_S35_00_W059_00_DEM.tif")
    assert (m.group("ns"), m.group("lat"), m.group("ew"), m.group("lon")) == ("S", "35", "W", "059")
    assert sources.template_corner(glo30, m) == (-35, -59)


def test_3deps_tiles_are_named_by_their_north_west_corner_in_lower_case_and_read_as_windows():
    usgs = SHIPPED["usgs-3dep-13"]
    tm = geodata.TransverseMercator(10, True, geodata.WGS84)
    # San Francisco: one tile, 37° to 38° N, 123° to 122° W, named n38w123.
    hull = sources.degree_hull([-122.52, 37.70, -122.35, 37.82], tm)
    [tile] = sources.template_files(usgs, hull, 10)
    assert tile.name == "USGS_13_n38w123.tif"
    assert tile.url == ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
                        "current/n38w123/USGS_13_n38w123.tif")
    # Its window is the grid's reach in degrees, a margin of 4 cells round
    # it, read at a quarter of a cell: in degrees, not metres.
    x0, y0, x1, y1 = tile.window["box"]
    margin = 40 / 111320.0
    assert x0 == pytest.approx(hull[0] - margin, abs=1e-6) and y1 == pytest.approx(hull[3] + margin, abs=1e-6)
    assert tile.window["want"] == pytest.approx(2.5 / 111320.0)
    # Across the equator and the prime meridian the letters follow the corner.
    names = sorted(f.name for f in sources.template_files(usgs, [-0.5, -0.5, 0.5, 0.5]))
    assert names == ["USGS_13_n00e000.tif", "USGS_13_n00w001.tif", "USGS_13_n01e000.tif",
                     "USGS_13_n01w001.tif"]
    # A cached name is read back as the tile whose south-west corner it has.
    m = sources.template_pattern(usgs).match("USGS_13_n38w123.tif")
    assert sources.template_corner(usgs, m) == (37, -123)
    assert sources.template_pattern(usgs).match("USGS_13_N38W123.tif") is None


def test_laea_europe_is_snyders_ellipsoidal_projection_both_ways():
    import crs
    laea = crs.plane("EPSG:3035")
    # EPSG Guidance Note 7-2's worked example: 50° N 5° E.
    assert laea.forward(50.0, 5.0) == pytest.approx((3962799.45, 2999718.85), abs=0.01)
    for lat, lon in ((47.26, 11.39), (35.0, -9.0), (70.5, 31.0)):
        assert laea.inverse(*laea.forward(lat, lon)) == pytest.approx((lat, lon), abs=1e-8)


def test_bevs_tiles_are_50_km_squares_of_laea_named_by_their_corner_in_metres():
    dtm = SHIPPED["bev-als-dtm"]
    tm = geodata.TransverseMercator(32, True, geodata.WGS84)
    hull = sources.degree_hull([11.30, 47.22, 11.48, 47.31], tm)    # Innsbruck
    [tile] = sources.template_files(dtm, hull, 10)
    assert tile.url == ("https://data.bev.gv.at/download/ALS/DTM/20250915/"
                        "ALS_DTM_CRS3035RES50000mN2650000E4400000.tif")
    # Its window is the grid's reach in metres of LAEA, 4 cells round it,
    # read at a quarter of a cell.
    x0, y0, x1, y1 = tile.window["box"]
    assert 4419000 < x0 < 4419100 and 4433500 < x1 < 4433600 and 2690000 < y1 < 2690100
    assert tile.window["want"] == 2.5
    # A rectangle across a tile edge takes both tiles, west to east.
    across = sources.degree_hull([11.62, 47.12, 11.78, 47.22], tm)      # the Zillertal
    assert [f.name[len("ALS_DTM_"):-4] for f in sources.template_files(dtm, across)] == [
        "CRS3035RES50000mN2650000E4400000", "CRS3035RES50000mN2650000E4450000"]
    # A cached name is read back as its square, drawn in degrees.
    m = sources.template_pattern(dtm).match("ALS_DTM_CRS3035RES50000mN2650000E4400000.tif")
    corner = sources.template_corner(dtm, m)
    assert corner == (4400000.0, 2650000.0)
    [ring] = sources.template_square(dtm, corner)
    assert ring[0] == ring[-1] and len(ring) == 33
    assert sources.holds({"type": "Polygon", "coordinates": [ring]}, [11.35, 47.25, 11.4, 47.3])


def test_an_inspire_population_grid_becomes_the_compilers_csv(tmp_path):
    cache = sources.Cache(None, str(tmp_path / "cache"))
    f = sources.File("pop", "https://example.org/pop.zip", "pop.zip", "*.gml")
    os.makedirs(os.path.dirname(f.path(cache.root)))

    def value(v, cell):
        return ('<pd:value><pd:StatisticalValue><pd:value>%s</pd:value><pd:dimensions>'
                '<pd:Dimensions><pd:spatial xlink:href="https://data.inspire.gv.at/x/'
                'su.StatisticalGridCell/AT_%s"/></pd:Dimensions></pd:dimensions>'
                '</pd:StatisticalValue></pd:value>' % (v, cell))
    gml = ('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
           'xmlns:pd="http://inspire.ec.europa.eu/schemas/pd/4.0" '
           'xmlns:xlink="http://www.w3.org/1999/xlink"><wfs:member><pd:StatisticalDistribution>'
           + value(7, "CRS3035RES100mN2599300E4671800") + value(12, "CRS3035RES100mN2603000E4647800")
           + '</pd:StatisticalDistribution></wfs:member></wfs:FeatureCollection>')
    with zipfile.ZipFile(f.path(cache.root), "w") as zf:
        zf.writestr("pd_popreg_100m.gml", gml)
    path, system, cell = cache.inspire_grid_csv(f)
    assert (system, cell) == ("EPSG:3035", 100.0)
    assert open(path).read() == "x,y,value\n4671850.0,2599350.0,7\n4647850.0,2603050.0,12\n"
    # Once written, it is read back without parsing the GML again.
    os.remove(os.path.join(os.path.dirname(path), "pd_popreg_100m.gml"))
    assert cache.inspire_grid_csv(f) == (path, "EPSG:3035", 100.0)

    mixed = sources.File("mixed", "https://example.org/mixed.zip", "mixed.zip", "*.gml")
    os.makedirs(os.path.dirname(mixed.path(cache.root)))
    with zipfile.ZipFile(mixed.path(cache.root), "w") as zf:
        zf.writestr("m.gml", gml.replace("RES100mN2603000", "RES1000mN2603000"))
    with pytest.raises(store.StoreError, match="mixes grids"):
        cache.inspire_grid_csv(mixed)


def test_an_austrian_rectangle_hands_the_compiler_bevs_pair_and_the_census_grid(tmp_path):
    cache = sources.Cache(None, str(tmp_path / "cache"))
    files = {}

    def cached(source_id, name, members=None, content=b"tif"):
        f = sources.File(source_id, "https://example.org/" + name, name, members)
        path = f.path(cache.root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(content)
        files.setdefault(source_id, []).append(f)
    cached("glo30", "Copernicus_DSM_COG_10_N47_00_E011_00_DEM.tif")
    cached("bev-als-dtm", "ALS_DTM_CRS3035RES50000mN2650000E4400000.tif")
    cached("bev-als-dsm", "ALS_DSM_CRS3035RES50000mN2650000E4400000.tif")
    pop = tmp_path / "pop.zip"
    with zipfile.ZipFile(pop, "w") as zf:
        zf.writestr("pd.gml", '<c xmlns:pd="p" xmlns:xlink="http://www.w3.org/1999/xlink">'
                    '<pd:StatisticalValue><pd:value>3</pd:value><pd:spatial xlink:href='
                    '"x/AT_CRS3035RES100mN2650000E4400000"/></pd:StatisticalValue></c>')
    cached("statistik-austria-population", "pd_popreg_100m.zip", "*.gml", pop.read_bytes())
    build = packbuild.Build(cache, {"name": "innsbruck", "bbox": [11.30, 47.22, 11.48, 47.31],
                                    "res_m": 30}, "planner-job", lambda row: None,
                            sources_=SHIPPED)
    build.inputs = str(tmp_path / "inputs")
    params = build.params({"files": files, "grid": {"zone": 32}}, SHIPPED)
    [pair] = params["elevation"]
    assert [os.path.basename(p) for p in pair["terrain"] + pair["surface"]] == [
        "ALS_DTM_CRS3035RES50000mN2650000E4400000.tif",
        "ALS_DSM_CRS3035RES50000mN2650000E4400000.tif"]
    assert pair["proj"].startswith("+proj=laea") and pair["pixel_m"] == 7.5
    assert pair["nodata"] == [-9999.0]
    population = params["population"]
    assert population["proj"].startswith("+proj=laea") and population["cell_m"] == 100.0
    assert open(population["csv"]).read() == "x,y,value\n4400050.0,2650050.0,3\n"


def test_nad83_and_conus_albers_are_systems_sim_mesh_knows():
    import crs
    assert crs.known("EPSG:4269") and crs.known("EPSG:5070") and crs.known("EPSG:26910")
    assert not crs.known("EPSG:26924")
    assert crs.proj("EPSG:26910") == ("+proj=utm +zone=10 +ellps=GRS80 "
                                      "+towgs84=0,0,0,0,0,0,0 +units=m +no_defs")
    assert crs.per_metre("EPSG:4269") == pytest.approx(1 / 111320.0)
    assert crs.per_metre("EPSG:26910") == 1.0
    lat, lon = crs.plane("EPSG:26910").inverse(*crs.plane("EPSG:26910").forward(37.77, -122.42))
    assert (lat, lon) == pytest.approx((37.77, -122.42), abs=1e-9)
    assert crs.plane("EPSG:4269").forward(37.77, -122.42) == (-122.42, 37.77)


def test_the_smallest_extract_holding_the_rectangle_is_the_one():
    assert sources.smallest_extract(index(), MITTE)["properties"]["id"] == "berlin"
    assert sources.smallest_extract(index(), [13.0, 52.5, 13.2, 52.6])["properties"]["id"] \
        == "brandenburg"
    assert sources.smallest_extract(index(), [4.0, 50, 6.0, 51]) is None
    germany = sources.outline(index(), "germany")
    assert sources.meets(germany, [4.0, 50, 6.0, 51]) and not sources.meets(germany, [0, 0, 1, 1])
    # A rectangle astride a concave outline's notch is not held by it.
    notched = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [3, 4], [3, 1],
                                                   [1, 1], [1, 4], [0, 4], [0, 0]]]}
    assert not sources.holds(notched, [0.5, 0.5, 3.5, 3.5])
    assert sources.holds(notched, [0.2, 0.2, 3.8, 0.8])


def test_only_the_feed_tiles_meeting_the_rectangle_are_chosen():
    lod2 = SHIPPED["berlin-lod2"]
    chosen = sources.atom_files(lod2, FEED, MITTE)
    names = sorted(f.name for f in chosen)
    assert names and len(names) < 30 and chosen[0].members == "*.xml"
    x0, y0, x1, y1 = sources.atom_box(lod2, MITTE)
    for f in chosen:
        e, n = (int(v) for v in f.name[5:-4].split("_"))
        assert e < x1 and e + 1 > x0 and n < y1 and n + 1 > y0
    assert sources.atom_files(lod2, FEED, [12.0, 52.5, 12.1, 52.6]) == []


def test_a_listing_and_a_download_service_give_whole_addresses_and_names():
    # Brandenburg's tiles are a directory listing's relative links.
    listing = '<a href="?C=N;O=D">Name</a><a href="dgm_33367-5806.zip">dgm_33367-5806.zip</a>'
    got = sources.atom_files(SHIPPED["brandenburg-dgm1"], listing, [13.0492, 52.3904, 13.0563, 52.3950])
    assert [(f.url, f.name) for f in got] == [
        ("https://data.geobasis-bb.de/geobasis/daten/dgm/tif/dgm_33367-5806.zip",
         "dgm_33367-5806.zip")]
    assert got[0].members == "*.tif"
    # M-V's are a download service's, XML-escaped, the name in the query.
    feed = ('<link href="https://www.geodaten-mv.de/dienste/dgm_download?index=1&amp;dataset=x'
            '&amp;file=dgm1_33_262_5946_2_xyz.zip"/>')
    got = sources.atom_files(SHIPPED["mv-dgm1"], feed, [11.40, 53.62, 11.42, 53.63])
    assert [(f.url, f.name) for f in got] == [
        ("https://www.geodaten-mv.de/dienste/dgm_download?index=1&dataset=x"
         "&file=dgm1_33_262_5946_2_xyz.zip", "dgm1_33_262_5946_2_xyz.zip")]
    assert sources.file_name("https://gdi.berlin.de/x/DGM1_390_5818.zip") == "DGM1_390_5818.zip"


# ---- a host of the test's own --------------------------------------------------

def host_app(root, calls):
    async def any_file(request):
        calls.append((request.method, request.path, request.headers.get("Range"),
                      request.headers.get("User-Agent")))
        path = os.path.join(root, request.path.lstrip("/"))
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", any_file)
    return app


def running_host(root, check):
    calls = []

    async def go():
        runner = web.AppRunner(host_app(str(root), calls))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = "http://127.0.0.1:%d" % runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                await check(base, session, calls)
        finally:
            await runner.cleanup()
    asyncio.run(go())


def test_a_file_is_fetched_once_resumed_and_a_missing_one_remembered(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    (served / "big.bin").write_bytes(bytes(range(256)) * 1000)

    async def check(base, session, calls):
        cache = sources.Cache(session, str(tmp_path / "cache"))
        f = sources.File("glo30", base + "/big.bin", "big.bin")
        path = f.path(cache.root)
        os.makedirs(os.path.dirname(path))
        with open(path + ".part", "wb") as out:
            out.write((bytes(range(256)) * 1000)[:1000])
        assert await cache.size(f) == 256000
        assert (await cache.sizes_of([f]))[f] == 255000
        heard = []
        assert await cache.fetch(f, heard.append) == path
        assert open(path, "rb").read() == bytes(range(256)) * 1000
        assert calls[-1][2] == "bytes=1000-" and heard[-1] == 256000
        assert calls[-1][3] == sources.USER_AGENT
        before = len(calls)
        assert await cache.fetch(f) == path and len(calls) == before
        gone = sources.File("glo30", base + "/sea.tif", "sea.tif")
        assert await cache.fetch(gone) is None
        assert await cache.fetch(gone) is None and len(calls) == before + 1
        assert cache.have(gone) is False
    running_host(tmp_path / "served", check)


def test_a_zips_wanted_members_are_extracted_once(tmp_path):
    cache = sources.Cache(None, str(tmp_path))
    f = sources.single_file(SHIPPED["itu"])
    assert f.name == "itu.zip" and f.members == ("DN50.TXT", "N050.TXT")
    os.makedirs(tmp_path / "itu")
    with zipfile.ZipFile(tmp_path / "itu" / "itu.zip", "w") as zf:
        for name in ("ReadMe.doc", "DN50.TXT", "N050.TXT", "LAT.TXT"):
            zf.writestr(name, name)
    got = cache.extracted(f)
    assert sorted(os.path.basename(p) for p in got) == ["DN50.TXT", "N050.TXT"]
    assert sorted(os.listdir(tmp_path / "itu" / "x")) == ["DN50.TXT", "N050.TXT"]
    os.remove(tmp_path / "itu" / "itu.zip")
    assert cache.extracted(f) == got


# ---- a build, end to end ---------------------------------------------------------

FAKE_JOB = r'''#!/usr/bin/env python3
import json, os, sys
params = json.load(sys.stdin)
with open(os.environ["FAKE_JOB_SAW"], "w") as out:
    json.dump(params, out)
if params["name"] == "broken":
    print("pack build: something went wrong", file=sys.stderr)
    print(json.dumps({"error": "the DSM tiles leave holes"}))
    sys.exit(1)
os.makedirs(params["out_dir"], exist_ok=True)
for i, step in enumerate(["terrain", "clutter", "roads"]):
    print(json.dumps({"step": step, "done": i, "total": 3}), flush=True)
manifest = {"name": params["name"], "region": {"bbox": params["bbox"],
            "crs_epsg": 32600 + params["utm_zone"]}, "layers": [], "licenses": []}
path = os.path.join(params["out_dir"], "manifest.json")
with open(path, "w") as out:
    json.dump(manifest, out)
print(json.dumps({"manifest": path}))
'''


def serve_sources(root):
    """The public hosts as a directory: the index, Berlin's feeds, and a file
    for every URL a Mitte build asks for, except the GLO-30 tile east of it
    and Berlin's 1 m tiles."""
    (root / "geofabrik").mkdir(parents=True)
    (root / "index.json").write_text(json.dumps(index()))
    (root / "geofabrik" / "berlin-latest.osm.pbf").write_bytes(b"pbf" * 100)
    (root / "lod2").mkdir()
    with zipfile.ZipFile(root / "lod2" / "LoD2_390_5820.zip", "w") as zf:
        zf.writestr("LoD2_33_390_5820_1_BE.xml", "<CityModel/>")
    with zipfile.ZipFile(root / "zensus.zip", "w") as zf:
        zf.writestr(SHIPPED["zensus"].members(),
                    "GITTER_ID_100m;x_mp_100m;y_mp_100m;Einwohner\n")


def served_sources(tmp_path, base):
    """The shipped source file with every address on the test's host, and
    its outlines beside it: the sources a build is planned from, only those
    the host serves (no test reaches out to a real one)."""
    text = open(sourcefile.SHIPPED, encoding="utf-8").read()
    for old, new in (
            ("https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif", "/glo30/{tile}.tif"),
            ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
             "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif", "/wc/{tile}.tif"),
            ("https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.1812-8-202509-I!!ZIP-E.zip",
             "/itu.zip"),
            ("https://download.geofabrik.de/index-v1.json", "/index.json"),
            ("https://gdi.berlin.de/data/dgm1/atom/0.atom", "/berlin-dgm1.atom"),
            ("https://gdi.berlin.de/data/bdom/atom/0.atom", "/berlin-bdom.atom"),
            ("https://gdi.berlin.de/data/a_lod2/atom/0.atom", "/berlin-lod2.atom"),
            ("https://www.destatis.de/static/DE/zensus/gitterdaten/"
             "Zensus2022_Bevoelkerungszahl.zip", "/zensus.zip")):
        assert old in text
        text = text.replace(old, base + new)
    here = tmp_path / "sources"
    here.mkdir(exist_ok=True)
    (here / "sources.yaml").write_text(text)
    shutil.copytree(os.path.join(os.path.dirname(sourcefile.SHIPPED), "outlines"),
                    here / "outlines", dirs_exist_ok=True)
    served = ("glo30", "worldcover", "itu", "geofabrik", "berlin-dgm1", "berlin-bdom",
              "berlin-lod2", "zensus")
    return {k: v for k, v in sourcefile.load((str(here / "sources.yaml"),)).items() if k in served}


def test_each_source_says_what_it_is_used_for():
    chosen = [SHIPPED[s] for s in ("glo30", "geofabrik", "berlin-bdom", "berlin-lod2", "zensus",
                                   "itu")]
    inside = sources.used_for(chosen, {s.id: True for s in chosen})
    assert inside["berlin-lod2"] == "buildings" and inside["zensus"] == "population"
    assert inside["glo30"] == "surface heights where Berlin bDOM, 1 m surface has none"
    assert inside["geofabrik"] == \
        "roads; places; buildings where Berlin LoD2 building models has none"
    assert inside["itu"] == "the radio climate (ΔN and N0); kept here"
    across = sources.used_for(chosen, {s.id: s.worldwide for s in chosen})
    assert across["berlin-lod2"] == "buildings inside its outline"
    assert across["glo30"] == "surface heights outside Berlin bDOM, 1 m surface"
    assert across["geofabrik"] == "roads; places; buildings outside Berlin LoD2 building models"
    assert across["zensus"] == "population inside its outline"


def test_the_shipped_sources_hold_together():
    assert list(SHIPPED) == ["glo30", "worldcover", "itu", "geofabrik", "berlin-dgm1",
                             "berlin-bdom", "berlin-lod2", "zensus", "brandenburg-dgm1",
                             "brandenburg-bdom", "brandenburg-lod2", "mv-dgm1", "mv-dom1",
                             "mv-lod2", "by-dgm1", "by-dom20", "by-lod2", "bw-dgm1", "bw-dom1",
                             "bw-lod2", "hb-dgm1", "hb-dom1", "hb-lod2", "hh-dgm1", "hh-bdom",
                             "hh-lod2", "he-dgm1", "he-dom1", "ni-dgm1", "ni-dom1", "ni-lod2",
                             "nrw-dgm1", "nrw-dom1", "nrw-lod2", "rp-dgm1", "rp-dom1", "rp-lod2",
                             "sl-dgm1", "sl-dom1", "sl-lod2", "sn-dgm1", "sn-dom1", "sn-lod2",
                             "st-dgm1", "st-dom1", "st-lod2", "sh-dgm1", "sh-bdom", "sh-lod2",
                             "th-dgm1", "th-dom1", "th-lod2",
                             "ahn-dtm", "ahn-dsm", "3dbag", "cbs-population",
                             "bev-als-dtm", "bev-als-dsm", "statistik-austria-population",
                             "flanders-dtm", "flanders-dsm", "fr-lidarhd-mnt-30",
                             "fr-lidarhd-mns-30", "fr-lidarhd-mnt-31", "fr-lidarhd-mns-31",
                             "fr-lidarhd-mnt-32", "fr-lidarhd-mns-32", "no-nhm-dtm", "no-nhm-dom",
                             "ee-dtm", "ee-dsm", "pl-nmt", "cz-dmr5g", "cz-dmp1g", "andalucia-mdt",
                             "andalucia-mds", "catalunya-met", "catalunya-ms", "navarra-mdt",
                             "navarra-mds", "galicia-mdt", "bz-dtm", "bz-dsm", "emilia-romagna-dtm",
                             "piemonte-dtm", "lombardia-dtm", "campania-dtm", "sicilia-mdt",
                             "mt-dtm", "mt-dsm",
                             "usgs-3dep-13", "nlcd", "worldpop-us"]
    for source in SHIPPED.values():
        assert source.worldwide == (source.continent == sourcefile.GLOBAL)
        assert source.worldwide or source.outline["type"] in ("Polygon", "MultiPolygon")
        assert source.redistributable == (source.id != "itu")
    assert SHIPPED["zensus"].where == "Europe › Germany"
    assert SHIPPED["3dbag"].where == "Europe › Netherlands"
    assert SHIPPED["no-nhm-dtm"].where == "Europe › Norway"            # "NO", quoted
    # Italy's RDN2008 / UTM 32 is UTM 32 on GRS80, as ETRS89's is.
    assert sourcefile.proj_of(SHIPPED["emilia-romagna-dtm"]) == crs.proj("EPSG:25832")
    assert SHIPPED["nlcd"].where == "North America › United States"
    assert sourcefile.proj_of(SHIPPED["ahn-dtm"]).startswith("+proj=sterea")
    assert sourcefile.proj_of(SHIPPED["nlcd"]).startswith("+proj=aea +lat_0=23 +lon_0=-96")
    assert sourcefile.proj_of(SHIPPED["usgs-3dep-13"]).startswith("+proj=longlat +ellps=GRS80")
    # The US sources cover the US, Alaska's Aleutians past 180° among it, and
    # NLCD only the conterminous states.
    for source in ("usgs-3dep-13", "worldpop-us"):
        outline = SHIPPED[source].outline
        assert sources.meets(outline, [-122.5, 37.6, -122.3, 37.8])
        assert sources.meets(outline, [-150.0, 61.1, -149.8, 61.3])
        assert sources.meets(outline, [178.0, 51.8, 178.2, 52.0])
        assert not sources.meets(outline, [13.38, 52.51, 13.42, 52.53])
    assert sources.meets(SHIPPED["nlcd"].outline, [-74.1, 40.6, -73.9, 40.8])
    assert not sources.meets(SHIPPED["nlcd"].outline, [-150.0, 61.1, -149.8, 61.3])
    # Every German state has its terrain, its surface and its buildings,
    # Hessen's buildings OpenStreetMap's.
    capitals = {"by": (11.57, 48.13), "bw": (9.18, 48.77), "hb": (8.80, 53.07),
                "hh": (9.99, 53.55), "he": (8.24, 50.08), "ni": (9.73, 52.37),
                "nrw": (6.78, 51.22), "rp": (8.27, 50.00), "sl": (7.00, 49.23),
                "sn": (13.74, 51.05), "st": (11.63, 52.13), "sh": (10.13, 54.32),
                "th": (11.03, 50.98), "berlin": (13.40, 52.52), "brandenburg": (13.06, 52.40),
                "mv": (11.41, 53.63)}
    for state, (lon, lat) in capitals.items():
        here = {layer for s in SHIPPED.values() if s.id.startswith(state + "-")
                and sources.meets(s.outline, [lon, lat, lon + 0.01, lat + 0.01])
                for layer in s.layers}
        assert here == ({"terrain", "surface"} if state == "he"
                        else {"terrain", "surface", "buildings"}), state


@pytest.mark.parametrize("change, why", [
    (lambda e: e.update(id="Bad Id"), "usable source id"),
    (lambda e: e.update(layers={"sky": 1}), "no layer"),
    (lambda e: e.update(coverage="worldwide"), "worldwide source stands under `global`"),
    (lambda e: e["find"].update(method="ftp"), "find.method"),
    (lambda e: e["find"].pop("feed"), "needs feed"),
    (lambda e: e["find"].update(name="(\\d+)"), "(?P<x>"),
    (lambda e: e.update(read="stream"), "read is whole or window"),
    (lambda e: e.update(read="window"), "a window is read of a regional source's cloud-optimised"),
    (lambda e: e["format"].update(crs="EPSG:4326"), "the compiler reads xyz"),
    (lambda e: e["format"].update(nodata=-9999), "format.nodata is the number a GeoTIFF"),
    (lambda e: e.update(notice=None), "gives the notice"),
])
def test_a_source_that_does_not_hold_together_is_refused_saying_why(tmp_path, change, why):
    import copy
    import yaml
    entry = copy.deepcopy(SHIPPED["berlin-dgm1"].entry)
    change(entry)
    (tmp_path / "outlines").mkdir()
    shutil.copy(os.path.join(os.path.dirname(sourcefile.SHIPPED), "outlines",
                             "berlin-dgm1.geojson"), tmp_path / "outlines" / "berlin-dgm1.geojson")
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump(
        {"europe": {"DE": {"name": "Germany", "sources": [entry]}}}, allow_unicode=True))
    with pytest.raises(store.StoreError, match=re.escape(why)):
        sourcefile.read_file(str(tmp_path / "sources.yaml"))


@pytest.mark.parametrize("source, change, why", [
    ("nlcd", lambda e: e["format"]["classes"].update({"x": "open"}), "a land cover code is a whole"),
    ("nlcd", lambda e: e["format"]["classes"].update({11: "sea"}), "'sea' is no clutter class"),
    ("nlcd", lambda e: e.update(layers={"landcover": 1, "population": 1}), "a GeoTIFF feeds one layer"),
    ("nlcd", lambda e: e["format"].update(crs="EPSG:2263"), "format.crs EPSG:2263 is not a system"),
    ("worldpop-us", lambda e: e.update(read="window"), "a window is read of a terrain or a surface"),
    ("usgs-3dep-13", lambda e: e["find"].update(size_deg=0.5), "whole number of them (size_deg)"),
    ("usgs-3dep-13", lambda e: e["find"].update(crs="EPSG:26910"),
     "tiles in metres are size_m wide"),
    ("usgs-3dep-13", lambda e: e["find"].update(corner="north-east"), "a template's corner is"),
    ("usgs-3dep-13", lambda e: e["find"].update(letters="title"), "a template's letters are"),
])
def test_a_us_source_that_does_not_hold_together_is_refused_saying_why(tmp_path, source, change,
                                                                        why):
    import copy
    import yaml
    entry = copy.deepcopy(SHIPPED[source].entry)
    change(entry)
    (tmp_path / "outlines").mkdir()
    shutil.copy(os.path.join(os.path.dirname(sourcefile.SHIPPED), "outlines", source + ".geojson"),
                tmp_path / "outlines" / (source + ".geojson"))
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump(
        {"north-america": {"US": {"name": "United States", "sources": [entry]}}},
        allow_unicode=True))
    with pytest.raises(store.StoreError, match=re.escape(why)):
        sourcefile.read_file(str(tmp_path / "sources.yaml"))


@pytest.mark.parametrize("source, change, why", [
    ("nrw-lod2", lambda e: e["format"].update(crs="EPSG:4326"), "the compiler reads citygml in a UTM"),
    ("bw-dgm1", lambda e: e["find"].update(origin_m=[1000]), "origin_m is [x, y]"),
    ("he-dgm1", lambda e: e["find"].update(tile="{x2}_{y2}"), "names its corner with {x} and {y}"),
    ("he-dgm1", lambda e: e["find"].update(file="a/{x}_{y}.tif"), "the name a tile is kept under"),
    ("hh-lod2", lambda e: e["find"].update(archives="https://h/a.zip"), "find.archives is a list"),
    ("hh-lod2", lambda e: e["find"].update(name="(?P<x>\\d+)"), "(?P<x>…) and (?P<y>…)"),
    ("ni-dgm1", lambda e: e["find"].pop("tile_property"), "both tile_property and newest_property"),
    ("ni-dgm1", lambda e: e.update(pairs_with="ni-dom1"), "pairs_with is a GeoTIFF surface's"),
])
def test_a_german_source_that_does_not_hold_together_is_refused_saying_why(tmp_path, source,
                                                                            change, why):
    import copy
    import yaml
    entry = copy.deepcopy(SHIPPED[source].entry)
    change(entry)
    (tmp_path / "outlines").mkdir()
    shutil.copy(os.path.join(os.path.dirname(sourcefile.SHIPPED), "outlines", source + ".geojson"),
                tmp_path / "outlines" / (source + ".geojson"))
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump(
        {"europe": {"DE": {"name": "Germany", "sources": [entry]}}}, allow_unicode=True))
    with pytest.raises(store.StoreError, match=re.escape(why)):
        sourcefile.read_file(str(tmp_path / "sources.yaml"))


def test_a_surface_pairs_with_the_xyz_terrain_it_names_and_only_that(tmp_path):
    import yaml
    given = {"xyz": ["bw-dgm1"], "elevation_terrain": ["by-dgm1"],
             "elevation_surface": ["by-dom20", "bw-dom1"]}
    packbuild.Build.surfaces_to_xyz(given, SHIPPED)
    assert given == {"xyz": ["bw-dgm1", "bw-dom1"], "elevation_terrain": ["by-dgm1"],
                     "elevation_surface": ["by-dom20"]}
    # Its partner absent from the rectangle, it stays a GeoTIFF surface.
    given = {"elevation_surface": ["bw-dom1"]}
    packbuild.Build.surfaces_to_xyz(given, SHIPPED)
    assert given == {"elevation_surface": ["bw-dom1"]}
    # A partner that is no XYZ terrain is refused when the files are read.
    (tmp_path / "outlines").mkdir()
    for source in ("bw-dom1", "by-dgm1"):
        shutil.copy(os.path.join(os.path.dirname(sourcefile.SHIPPED), "outlines",
                                 source + ".geojson"), tmp_path / "outlines" / (source + ".geojson"))
    dom = dict(SHIPPED["bw-dom1"].entry, id="bw-dom1", pairs_with="by-dgm1")
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump(
        {"europe": {"DE": {"name": "Germany", "sources": [dom, SHIPPED["by-dgm1"].entry]}}},
        allow_unicode=True))
    with pytest.raises(store.StoreError, match="pairs_with names the XYZ terrain"):
        sourcefile.load((str(tmp_path / "sources.yaml"),))


def test_a_us_rectangle_hands_the_compiler_3dep_alone_nlcd_over_worldcover_and_worldpop(tmp_path):
    """The compiler's inputs from what the cache holds: 3DEP's terrain with no
    surface beside it, read in degrees; land cover worldwide source first,
    each with its own table as clutter class codes; WorldPop's raster."""
    cache = sources.Cache(None, str(tmp_path / "cache"))
    files = {}

    def cached(source_id, name, members=None):
        f = sources.File(source_id, "https://example.org/" + name, name, members)
        path = f.path(cache.root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if members:
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr(name[:-4] + ".tif", b"tif")
                zf.writestr(name[:-4] + ".tif.aux.xml", b"aux")
        else:
            open(path, "wb").write(b"tif")
        files.setdefault(source_id, []).append(f)
    cached("glo30", "Copernicus_DSM_COG_10_N37_00_W123_00_DEM.tif")
    cached("usgs-3dep-13", "USGS_13_n38w123.tif")
    cached("nlcd", "Annual_NLCD_LndCov_2025_CU_C1V2.zip", "*.tif")
    cached("worldcover", "ESA_WorldCover_10m_2021_v200_N36W123_Map.tif")
    cached("worldpop-us", "usa_pop_2025_CN_100m_R2025A_v1.tif")
    build = packbuild.Build(cache, {"name": "sf", "bbox": [-122.52, 37.70, -122.35, 37.82],
                                    "res_m": 30}, "planner-job", lambda row: None,
                            sources_=SHIPPED)
    build.inputs = str(tmp_path / "inputs")
    params = build.params({"files": files, "grid": {"zone": 10}}, SHIPPED)
    [terrain] = params["elevation"]
    assert [os.path.basename(p) for p in terrain["terrain"]] == ["USGS_13_n38w123.tif"]
    assert terrain["surface"] == [] and terrain["nodata"] == [-999999.0]
    assert terrain["proj"].startswith("+proj=longlat +ellps=GRS80")
    assert terrain["pixel_m"] == pytest.approx(7.5 / 111320.0)
    assert [c["source"] for c in params["landcover"]] == [
        "ESA WorldCover 2021 land cover", "Annual NLCD 2025 land cover"]
    worldcover, nlcd = params["landcover"]
    assert [10, 3] in worldcover["classes"] and [80, 1] in worldcover["classes"]
    assert [24, 5] in nlcd["classes"] and [11, 1] in nlcd["classes"]
    assert [os.path.basename(p) for p in nlcd["tiles"]] == ["Annual_NLCD_LndCov_2025_CU_C1V2.tif"]
    assert nlcd["proj"].startswith("+proj=aea")
    population = params["population"]
    assert os.path.basename(population["raster"]) == "usa_pop_2025_CN_100m_R2025A_v1.tif"
    assert population["nodata"] == -99999.0 and population["proj"].startswith("+proj=longlat")
    assert "worldcover_tiles" not in params


@pytest.mark.parametrize("env, says", [
    ({"SIM_MESH_IN_CONTAINER": "1", "SIM_MESH_ENGINE": "podman", "SIM_MESH_HOST_OS": "Darwin"},
     "podman machine set --memory 4096"),
    ({"SIM_MESH_IN_CONTAINER": "1", "SIM_MESH_ENGINE": "docker", "SIM_MESH_HOST_OS": "Darwin"},
     "Docker Desktop: Settings › Resources › Memory"),
    ({"SIM_MESH_IN_CONTAINER": "1", "SIM_MESH_ENGINE": "podman", "SIM_MESH_HOST_OS": "Linux"},
     "no limit of its own under podman on Linux"),
    ({}, "this machine has too little free memory"),
])
def test_a_compiler_killed_for_memory_says_which_limit_to_raise(tmp_path, monkeypatch, env, says):
    for key in ("SIM_MESH_IN_CONTAINER", "SIM_MESH_ENGINE", "SIM_MESH_HOST_OS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    job = tmp_path / "planner-job"
    job.write_text("#!/usr/bin/env python3\nimport json, os, signal, sys\nsys.stdin.read()\n"
                   "print(json.dumps({'step': 'osm', 'done': 1, 'total': 8}), flush=True)\n"
                   "os.kill(os.getpid(), signal.SIGKILL)\n")
    job.chmod(0o755)
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    cache = sources.Cache(None, str(tmp_path / "cache"))
    build = packbuild.Build(cache, {"name": "boston", "bbox": [-71.1, 42.3, -71.0, 42.4],
                                    "res_m": 10}, str(job), lambda row: None)
    with pytest.raises(store.StoreError) as err:
        asyncio.run(build.compile({"name": "boston"}))
    assert str(err.value).startswith("the compiler was killed during osm, most likely for want "
                                     "of memory: ")
    assert says in str(err.value)


def test_a_surface_with_no_terrain_beside_it_is_refused(tmp_path):
    cache = sources.Cache(None, str(tmp_path / "cache"))
    f = sources.File("ahn-dsm", "https://example.org/a.tif", "a.tif")
    os.makedirs(os.path.dirname(f.path(cache.root)))
    open(f.path(cache.root), "wb").write(b"tif")
    glo = sources.File("glo30", "https://example.org/g.tif", "g.tif")
    os.makedirs(os.path.dirname(glo.path(cache.root)))
    open(glo.path(cache.root), "wb").write(b"tif")
    build = packbuild.Build(cache, {"name": "x", "bbox": [4.8, 52.3, 4.9, 52.4], "res_m": 30},
                            "planner-job", lambda row: None, sources_=SHIPPED)
    build.inputs = str(tmp_path / "inputs")
    with pytest.raises(store.StoreError, match="AHN DSM 0.5 m surface has no terrain to pair"):
        build.params({"files": {"glo30": [glo], "ahn-dsm": [f]}, "grid": {"zone": 31}}, SHIPPED)


def test_a_persons_source_may_not_take_a_shipped_id_nor_lack_its_outline(tmp_path):
    import yaml
    own = tmp_path / "sources.yaml"
    own.write_text(yaml.safe_dump({"europe": {"DE": {"name": "Germany", "sources": [
        SHIPPED["zensus"].entry]}}}, allow_unicode=True))
    with pytest.raises(store.StoreError, match="outline"):
        sourcefile.load((sourcefile.SHIPPED, str(own)))
    (tmp_path / "outlines").mkdir()
    shutil.copy(os.path.join(os.path.dirname(sourcefile.SHIPPED), "outlines", "zensus.geojson"),
                tmp_path / "outlines" / "zensus.geojson")
    with pytest.raises(store.StoreError, match="sim-mesh's"):
        sourcefile.load((sourcefile.SHIPPED, str(own)))


def test_a_build_fetches_what_it_needs_and_hands_the_compiler_its_inputs(tmp_path, monkeypatch):
    served = tmp_path / "served"
    serve_sources(served)
    monkeypatch.setattr(store, "GEODATA_DIR", str(tmp_path / "geodata"))
    ground = tmp_path / "geodata"
    job = tmp_path / "planner-job"
    job.write_text(FAKE_JOB)
    job.chmod(0o755)
    saw = tmp_path / "saw.json"
    monkeypatch.setenv("FAKE_JOB_SAW", str(saw))

    async def check(base, session, calls):
        reg = served_sources(tmp_path, base)
        (served / "glo30").mkdir()
        (served / "glo30" / "Copernicus_DSM_COG_10_N52_00_E013_00_DEM.tif").write_bytes(
            b"II*\0dem")
        (served / "wc").mkdir()
        (served / "wc" / "N51E012.tif").write_bytes(b"II*\0wc")
        with zipfile.ZipFile(served / "itu.zip", "w") as zf:
            zf.writestr("DN50.TXT", "dn")
            zf.writestr("N050.TXT", "n0")
        idx = index()
        for feature in idx["features"]:
            feature["properties"]["urls"]["pbf"] = base + feature["properties"]["urls"]["pbf"]
        (served / "index.json").write_text(json.dumps(idx))
        for source in ("berlin-dgm1", "berlin-bdom", "berlin-lod2"):
            (served / (source + ".atom")).write_text(
                FEED.replace("https://gdi.berlin.de/data/a_lod2/atom", base + "/" + source[7:]))
        cache = sources.Cache(session, str(tmp_path / "cache"))

        planned = await sources.plan(cache, {"bbox": MITTE, "res_m": 30}, sources=reg)
        rows = {r["source"]: r for r in planned["sources"]}
        assert rows["geofabrik"]["to_fetch"] == 300 and rows["glo30"]["to_fetch"] == 7
        assert planned["extract"]["id"] == "berlin"
        assert rows["berlin-lod2"]["used_for"] == "buildings"
        assert rows["geofabrik"]["used_for"] == \
            "roads; places; buildings where Berlin LoD2 building models has none"
        assert rows["zensus"]["used_for"] == "population"
        potsdam = await sources.plan(cache, {"bbox": [12.9, 52.35, 13.0, 52.42], "res_m": 30},
                                     sources=reg)
        rows = {r["source"]: r for r in potsdam["sources"]}
        assert potsdam["extract"]["id"] == "brandenburg"
        assert sorted(rows) == ["geofabrik", "glo30", "itu", "worldcover", "zensus"]
        assert rows["geofabrik"]["used_for"] == "roads; places; buildings"
        paris = {"bbox": [2.0, 48.8, 2.1, 48.9], "res_m": 30}
        with pytest.raises(store.StoreError, match="no region of"):
            await sources.plan(cache, paris, sources=reg)

        said = []
        spec = {"name": "mitte", "bbox": MITTE, "res_m": 30}
        packbuild.refuse(spec)
        build = packbuild.Build(cache, spec, str(job), said.append, sources_=reg)
        await build.start()
        assert build.row["state"] == "done", build.row["error"]
        params = json.loads(saw.read_text())
        assert params["osm_buildings"] and params["utm_zone"] == 33
        [lod2] = params["lod2"]
        assert os.path.basename(lod2["dir"]) == "lod2-berlin-lod2" and lod2["zone"] == 33
        assert lod2["source"] == "Berlin LoD2 building models"
        assert lod2["notice"].startswith("Geoportal Berlin: 3D-Gebäudemodelle LoD2")
        [xyz] = params["xyz"]
        assert xyz["zone"] == 33 and "terrain" in xyz and "surface" in xyz
        assert xyz["source"] == "Berlin DGM1, 1 m terrain and Berlin bDOM, 1 m surface"
        assert params["elevation"] == [] and params["cityjson"] is None
        # Zensus's grid as a population input, its layout and system from its source.
        population = params["population"]
        assert population["csv"].endswith("Zensus2022_Bevoelkerungszahl_100m-Gitter.csv")
        assert (population["delimiter"], population["x"], population["value"], population["cell_m"]) \
            == (";", "x_mp_100m", "Einwohner", 100.0)
        assert population["proj"].startswith("+proj=laea +lat_0=52 +lon_0=10")
        assert params["osm_pbf"].endswith("geofabrik/berlin.osm.pbf")
        assert os.path.basename(params["itu_maps_dir"]) == "x"
        assert [os.path.basename(p) for p in params["dsm_tiles"]] == [
            "Copernicus_DSM_COG_10_N52_00_E013_00_DEM.tif"]
        states = [r["state"] for r in said]
        assert states[0] == "fetching" and "compiling" in states and states[-1] == "done"
        assert [r["step"] for r in said if r["state"] == "compiling"][1:4] == \
            ["terrain", "clutter", "roads"]
        gd = geodata.load("mitte")
        assert gd.is_pack and gd.pack_dir == str(ground / "mitte")
        assert (ground / "mitte" / "geodata.yaml").read_text().startswith("# built from sources")
        assert os.listdir(ground) == ["mitte"]
        with pytest.raises(store.StoreError, match="already"):
            packbuild.refuse(spec)

        fetched = len(calls)
        build = packbuild.Build(cache, dict(spec, name="broken"), str(job), said.append,
                                sources_=reg)
        await build.start()
        assert build.row["state"] == "failed"
        assert build.row["error"] == "the DSM tiles leave holes"
        assert "something went wrong" in open(build.log_path).read()
        assert not os.path.exists(geodata.geodata_path("broken"))
        # The second build over the same ground fetched nothing new.
        assert not [c for c in calls[fetched:] if c[0] == "GET" and "glo30" in c[1]]

        build = packbuild.Build(cache, dict(spec, name="gone"), str(job), said.append,
                                sources_=reg)
        task = build.start()
        await asyncio.sleep(0)
        build.cancel()
        await task
        assert build.row["state"] == "cancelled"
        assert os.listdir(ground) == ["mitte"]
    running_host(served, check)


class StandInCache:
    """What source_map asks of the cache, from fixtures: its root, the index
    and a Berlin feed of two tiles."""

    def __init__(self, root):
        self.root = str(root)

    async def regions_index(self, source):
        return index()

    async def feed(self, source):
        return ('<a href="https://gdi.berlin.de/x/DGM1_390_5818.zip"/>'
                '<a href="https://gdi.berlin.de/x/DGM1_392_5818.zip"/>')

    async def index_features(self, source):
        raise store.StoreError("no index in this test")

    async def zip_listing(self, source):
        raise store.StoreError("no archives in this test")


def test_a_sources_area_and_its_cache_come_from_names_feeds_and_outlines(tmp_path):
    for source, name in (("glo30", "Copernicus_DSM_COG_10_N52_00_E013_00_DEM.tif"),
                         ("glo30", "Copernicus_DSM_COG_10_S34_00_W071_00_DEM.tif.part"),
                         ("worldcover", "ESA_WorldCover_10m_2021_v200_N51E012_Map.tif"),
                         ("berlin-dgm1", "DGM1_390_5818.zip"), ("berlin-dgm1", "DGM1_390_5818.zip.x"),
                         ("geofabrik", "berlin.osm.pbf")):
        (tmp_path / source).mkdir(exist_ok=True)
        (tmp_path / source / name).write_bytes(b"x")
    cache = StandInCache(tmp_path)

    glo = asyncio.run(sources.source_map(cache, "glo30", SHIPPED))
    assert glo["worldwide"] and glo["cached_files"] == 1
    assert glo["cached"]["coordinates"] == [sources._box(13, 52, 14, 53)]
    cover = asyncio.run(sources.source_map(cache, "worldcover", SHIPPED))
    assert cover["cached"]["coordinates"] == [sources._box(12, 51, 15, 54)]
    dgm = asyncio.run(sources.source_map(cache, "berlin-dgm1", SHIPPED))
    # Its data is where its feed has a tile, not the whole of its outline.
    assert not dgm["worldwide"] and len(dgm["covers"]["coordinates"]) == 2
    assert dgm["covers_from"] == "the 2 tiles its feed lists"
    assert len(dgm["cached"]["coordinates"]) == 1 and dgm["cached_files"] == 1
    osm = asyncio.run(sources.source_map(cache, "geofabrik", SHIPPED))
    assert osm["cached"]["coordinates"] == [[square(13.08, 52.33, 13.77, 52.68)]]

    # A point in the cached Berlin tile, and one in France.
    lat, lon = sources.zone_of("EPSG:25833").inverse(390500, 5818500)
    here = asyncio.run(sources.sources_at(cache, lon, lat, SHIPPED))
    assert here["berlin-dgm1"] == {"has": True, "cached": True, "error": None}
    assert here["berlin-lod2"]["has"] and not here["berlin-lod2"]["cached"]
    assert here["glo30"]["cached"] and here["zensus"]["has"]
    # Inside Berlin but on no tile its feeds list: no Berlin data there.
    lat, lon = sources.zone_of("EPSG:25833").inverse(386500, 5818500)
    beside = asyncio.run(sources.sources_at(cache, lon, lat, SHIPPED))
    assert not beside["berlin-dgm1"]["has"] and beside["zensus"]["has"]
    away = asyncio.run(sources.sources_at(cache, 31.24, 30.04, SHIPPED))      # Cairo
    assert [s for s, v in away.items() if v["has"]] == ["glo30", "worldcover", "itu", "geofabrik"]
    assert not any(v["cached"] for v in away.values())


def test_a_file_comes_from_a_mirror_when_the_first_address_fails(tmp_path):
    served = tmp_path / "served"
    (served / "second").mkdir(parents=True)
    (served / "second" / "tile.tif").write_bytes(b"tile")

    async def check(base, session, calls):
        cache = sources.Cache(session, str(tmp_path / "cache"))
        f = sources.File("glo30", [base + "/first/tile.tif", base + "/second/tile.tif"],
                         "tile.tif")
        assert await cache.fetch(f) == f.path(cache.root)
        assert [c[1] for c in calls] == ["/first/tile.tif", "/second/tile.tif"]
        gone = sources.File("glo30", [base + "/a/x.tif", base + "/b/x.tif"], "x.tif",
                            missing="error")
        with pytest.raises(store.StoreError, match="404"):
            await cache.fetch(gone)
    running_host(served, check)


def test_a_meta_file_retries_a_payload_cut_short_then_succeeds(tmp_path, monkeypatch):
    """A feed cut mid-download (`ClientPayloadError`) is a hiccup, not a
    failure: a few more tries, politely, before meta_file gives up."""
    monkeypatch.setattr(sources, "BACKOFF_S", 0.01)
    calls = []

    async def flaky(request):
        calls.append(request.path)
        if len(calls) <= 2:
            resp = web.StreamResponse(headers={"Content-Length": "40"})
            await resp.prepare(request)
            await resp.write(b"not all of it")
            request.transport.close()
            return resp
        return web.Response(text="the feed, eventually")

    app = web.Application()
    app.router.add_get("/feed.atom", flaky)

    async def go():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = "http://127.0.0.1:%d" % runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                cache = sources.Cache(session, str(tmp_path / "cache"))
                path = await cache.meta_file("feed", base + "/feed.atom")
                assert open(path, encoding="utf-8").read() == "the feed, eventually"
        finally:
            await runner.cleanup()
    asyncio.run(go())
    assert len(calls) == 3
