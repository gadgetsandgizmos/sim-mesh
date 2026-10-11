"""Geodata: the ground nodes stand on.

    testbed/geodata/<name>/geodata.yaml
    testbed/geodata/<name>/manifest.json, …     a pack's own files, beside it
    testbed/geodata/<name>/.origin.yaml         the index it came from, when it did (indexes.py)

One directory per geodata, holding everything it is: deleting or renaming
the directory is deleting or renaming the geodata. One of two kinds:

    pack: .                             # the pack is this directory

    synthetic:                          # ground made up here, at 0°, 0°
      terrain: flat
      exponent: 2.7                     # log-distance path loss exponent
      extent_m: 20000                   # the square it covers, centred on 0°, 0°

A pack says nothing its pack does not already say: its CRS, extent and
layers are the pack's `manifest.json`. `pack:` is a directory relative to
the file: `.` in a geodata's own file, and the geodata's directory in a
run's or a snapshot's copy (`write_copy`), which is why geodata a run or a
snapshot stands on is neither deleted nor renamed.
Geodata names no node, and a nodeset names no geodata: a nodeset is offered
on every geodata whose extent holds one of its nodes. Noise figure and the
interference figures belong to the medium, not the ground, and are simd's.

**Synthetic ground lies at 0°, 0°**, and its degrees are metres by one fixed
rule on both axes, a nautical mile to the minute of arc:

    x = lon · 60 · 1852 m          y = lat · 60 · 1852 m

So a position is still latitude and longitude, the page can show it as
either, and a nodeset made on one synthetic ground stands on any other.
`terrain: flat` is the only terrain so far: a pair's loss is log-distance
with the ground's exponent. A node's height is above the ground under it.

**Shadowing**, on either kind, is two keys beside `pack:` or `synthetic:`:

    shadowing_db: 7                     # the spread of each pair's draw, dB
    shadowing_seed: 3                   # which draws; 0 when absent

Every pair of nodes gets a static draw of its own on top of its loss: one
standard normal per unordered pair, hashed from the seed and the two node
names, times `shadowing_db`, the same both ways, in every band and for the
whole run (`losses.with_shadowing`). Two pairs at one distance then need
not hear each other alike, which is what makes a hidden node or a lucky
long link. It is a layer over the tables, never in them, so neither key is
in `content_hash` and a new spread or seed recomputes nothing. Absent, or
at 0, there is none, and a file without the keys reads, hashes and is
written back exactly as it did before they existed.

**`loc_pct`**, on a pack only, is the percentage of locations its tables'
P.1812 figures are not exceeded at, 1 to 99, and 90, the planner's own, when
absent:

    pack: ../../packs/berlin-city
    loc_pct: 50

A figure at 90 % of locations already holds the spread of losses between
locations, and shadowing laid over it counts that spread twice, so a pack
with shadowing wants its tables at the median. One with shadowing at any
other percentage, 90 included, is warned about, not refused
(`losses.shadowing_warning`). Unlike shadowing it is the table's own
(`losses.header_for` records it as `p_loc_pct`), so it is in `content_hash`,
and a table at one percentage never stands in for another. The page's
coverage rasters are the planner's own sweep, at 90 % whatever this says.

Coordinates on a pack are its own CRS, absolute easting and northing in
metres. Packs are UTM on WGS84 (EPSG 326zz north, 327zz south) or on ETRS89
(258zz), a source's tiles may be on NAD83 (269zz), and the transverse Mercator here is Krüger's series to sixth order
in n (Karney 2011), good to well under a millimetre inside a zone, so what
sim-mesh sends the planner is the point the planner itself would compute. No
projection library is needed.

**A sim-mesh geodata pack** is how geodata moves between machines: a zip
holding `geodata.yaml` at the top and, for a pack, the pack under `pack/`,
which the yaml's `pack:` names. Importing takes that, or a bare planner pack
(its `manifest.json` at the zip's root, or inside one top-level directory),
expands it into `testbed/geodata/<name>/`, whole or not at all.

**Nodes are never part of the ground.** A planner pack may carry a `Nodes`
layer, a deployed network baked in; it is left out of what the page is told,
of an export, and of an import. Nodes belong to nodesets.
"""

import contextlib
import copy
import difflib
import hashlib
import json
import math
import os
import shutil
import zipfile

import yaml

import nodeset as nodeset_module
import store

PACK, SYNTHETIC = "pack", "synthetic"
MANIFEST = "manifest.json"
TERRAINS = ("flat",)
DEFAULT_EXPONENT = 2.7
DEFAULT_EXTENT_M = 20000.0
M_PER_DEGREE = 60 * 1852.0          # a nautical mile to the minute, on both axes
SIM_MESH_ROOT = os.path.dirname(store.SIM_DIR)
PLANNER_DIR = os.path.join(SIM_MESH_ROOT, "planner")
GEODATA_FILE = "geodata.yaml"       # in a geodata's directory
OWN_PACK = "."                      # a geodata file's `pack:` for its own directory
PART_PREFIX = ".part-"
CHUNK = 1 << 16
GEODATA_MEMBER = "geodata.yaml"     # at the top of a sim-mesh geodata pack
PACK_MEMBER = "pack"                # where an exported pack goes inside it
EXPORT_LEVEL = 1                    # deflate's fastest: berlin-city, 450 MB, is 124 MB in 2 s
NODES_LAYER = "Nodes"
NODES_NOTICE = "Deployed mesh nodes"   # how the Nodes layer's notice names its source
LOC_PCT, SHADOWING_DB, SHADOWING_SEED = "loc_pct", "shadowing_db", "shadowing_seed"
LOC_PCT_RANGE = (1.0, 99.0)         # P.1812's own (planner-propag p1812::lb_from_arrays)
# The keys a file may add, in the order a file is written in; a file
# carries one only when it states it.
STATED = (LOC_PCT, SHADOWING_DB, SHADOWING_SEED)
# The keys that are laid over a table rather than computed into it: not in
# `content_hash`, so a cached table outlives any change to them.
LAYER_KEYS = (SHADOWING_DB, SHADOWING_SEED)


def planner_repo():
    """sim-mesh's planner: the Rust workspace in `planner/`, whose planner-web
    serves a pack."""
    return PLANNER_DIR


# ---- transverse Mercator -------------------------------------------------

# Semi-major axis and flattening of the two ellipsoids UTM packs are on.
WGS84 = (6378137.0, 1 / 298.257223563)
GRS80 = (6378137.0, 1 / 298.257222101)
UTM_K0 = 0.9996
UTM_FALSE_EASTING = 500000.0
UTM_FALSE_NORTHING_SOUTH = 10000000.0


# National systems that are a UTM zone on GRS80 under a code of their own:
# Italy's RDN2008 (EPSG:7791/7792, 6707/6708), Sweden's SWEREF99 TM, Finland's
# ETRS-TM35FIN.
UTM_ALIASES = {7791: 32, 7792: 33, 6707: 32, 6708: 33, 3006: 33, 3067: 35}


def utm_zone_of(epsg):
    """(zone, north, ellipsoid) for an EPSG code this module can project to."""
    epsg = int(epsg)
    if epsg in UTM_ALIASES:
        return UTM_ALIASES[epsg], True, GRS80
    for base, north, ellipsoid, last in ((32600, True, WGS84, 60), (32700, False, WGS84, 60),
                                         (25800, True, GRS80, 60), (26900, True, GRS80, 23)):
        zone = epsg - base
        if 1 <= zone <= last:
            return zone, north, ellipsoid
    raise store.StoreError("EPSG %d is not a UTM zone (326zz, 327zz, 258zz or 269zz)" % epsg)


class TransverseMercator:
    """One UTM zone, forward and back, in Krüger's series.

    The series coefficients depend only on the ellipsoid's third flattening
    n, so they are worked out once per zone object. The inverse's last step,
    from the conformal latitude back to the geographic one, is Newton on
    tan(latitude), which converges to machine precision in three or four
    steps from the conformal value; five are taken unconditionally.
    """

    def __init__(self, zone, north=True, ellipsoid=WGS84):
        a, f = ellipsoid
        self.zone, self.north = zone, north
        self.lon0 = math.radians((zone - 1) * 6 - 180 + 3)
        self.fn = 0.0 if north else UTM_FALSE_NORTHING_SOUTH
        self.e = math.sqrt(f * (2 - f))
        n = f / (2 - f)
        n2, n3, n4, n5, n6 = n ** 2, n ** 3, n ** 4, n ** 5, n ** 6
        self.k0a = UTM_K0 * a / (1 + n) * (1 + n2 / 4 + n4 / 64 + n6 / 256)
        self.alpha = (
            n / 2 - 2 * n2 / 3 + 5 * n3 / 16 + 41 * n4 / 180 - 127 * n5 / 288 + 7891 * n6 / 37800,
            13 * n2 / 48 - 3 * n3 / 5 + 557 * n4 / 1440 + 281 * n5 / 630 - 1983433 * n6 / 1935360,
            61 * n3 / 240 - 103 * n4 / 140 + 15061 * n5 / 26880 + 167603 * n6 / 181440,
            49561 * n4 / 161280 - 179 * n5 / 168 + 6601661 * n6 / 7257600,
            34729 * n5 / 80640 - 3418889 * n6 / 1995840,
            212378941 * n6 / 319334400)
        self.beta = (
            n / 2 - 2 * n2 / 3 + 37 * n3 / 96 - n4 / 360 - 81 * n5 / 512 + 96199 * n6 / 604800,
            n2 / 48 + n3 / 15 - 437 * n4 / 1440 + 46 * n5 / 105 - 1118711 * n6 / 3870720,
            17 * n3 / 480 - 37 * n4 / 840 - 209 * n5 / 4480 + 5569 * n6 / 90720,
            4397 * n4 / 161280 - 11 * n5 / 504 - 830251 * n6 / 7257600,
            4583 * n5 / 161280 - 108847 * n6 / 3991680,
            20648693 * n6 / 638668800)

    def forward(self, lat, lon):
        """Degrees to (easting, northing) in metres."""
        phi, lam = math.radians(lat), math.radians(lon) - self.lon0
        e = self.e
        s = math.sin(phi)
        t = math.sinh(math.atanh(s) - e * math.atanh(e * s))
        xi_p = math.atan2(t, math.cos(lam))
        eta_p = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
        xi, eta = xi_p, eta_p
        for j, a_j in enumerate(self.alpha, 1):
            xi += a_j * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
            eta += a_j * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
        return UTM_FALSE_EASTING + self.k0a * eta, self.fn + self.k0a * xi

    def inverse(self, x, y):
        """(easting, northing) in metres to degrees (lat, lon)."""
        xi = (y - self.fn) / self.k0a
        eta = (x - UTM_FALSE_EASTING) / self.k0a
        xi_p, eta_p = xi, eta
        for j, b_j in enumerate(self.beta, 1):
            xi_p -= b_j * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
            eta_p -= b_j * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
        tau_p = math.sin(xi_p) / math.sqrt(math.sinh(eta_p) ** 2 + math.cos(xi_p) ** 2)
        lam = math.atan2(math.sinh(eta_p), math.cos(xi_p))
        e, e2 = self.e, self.e ** 2
        tau = tau_p
        for _ in range(5):
            sigma = math.sinh(e * math.atanh(e * tau / math.sqrt(1 + tau * tau)))
            tau_i = tau * math.sqrt(1 + sigma * sigma) - sigma * math.sqrt(1 + tau * tau)
            tau += ((tau_p - tau_i) / math.sqrt(1 + tau_i * tau_i)
                    * (1 + (1 - e2) * tau * tau) / ((1 - e2) * math.sqrt(1 + tau * tau)))
        return math.degrees(math.atan(tau)), math.degrees(lam + self.lon0)


# ---- synthetic ground ----------------------------------------------------

def synthetic_xy(lat, lon):
    """Degrees to metres on synthetic ground: a nautical mile to the minute."""
    return lon * M_PER_DEGREE, lat * M_PER_DEGREE


def synthetic_latlon(x, y):
    return y / M_PER_DEGREE, x / M_PER_DEGREE


# ---- the geodata ---------------------------------------------------------

def geodata_dir(name):
    return os.path.join(store.GEODATA_DIR, store.check_name(name, "geodata"))


def geodata_path(name):
    return os.path.join(geodata_dir(name), GEODATA_FILE)


def names():
    """Every geodata on disk, by name: each directory with a geodata file."""
    if not os.path.isdir(store.GEODATA_DIR):
        return []
    return sorted(entry for entry in os.listdir(store.GEODATA_DIR)
                  if store.NAME_RE.match(entry)
                  and os.path.isfile(os.path.join(store.GEODATA_DIR, entry, GEODATA_FILE)))


class Geodata:
    """One geodata, loaded: its kind, its extent, and the projection between
    a nodeset's degrees and the metres the loss model works in."""

    def __init__(self, name, data, path=None):
        self.name = name
        self.data = data
        self.path = path
        self.manifest = None
        self.pack_manifest_hash = None
        self.tm = None
        self.pack_dir = None
        if PACK in data:
            base = os.path.dirname(os.path.abspath(path)) if path else store.GEODATA_DIR
            self.pack_dir = os.path.normpath(os.path.join(base, os.path.expanduser(str(data[PACK]))))
            manifest_path = os.path.join(self.pack_dir, MANIFEST)
            try:
                with open(manifest_path, "rb") as handle:
                    raw = handle.read()
                self.manifest = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError) as err:
                raise store.StoreError("geodata %s: pack %s has no readable %s (%s)"
                                       % (name, self.pack_dir, MANIFEST, err)) from err
            self.pack_manifest_hash = hashlib.sha256(raw).hexdigest()
            epsg = (self.manifest.get("region") or {}).get("crs_epsg")
            if epsg is None:
                raise store.StoreError("geodata %s: the pack's manifest names no crs_epsg" % name)
            self.tm = TransverseMercator(*utm_zone_of(epsg))
        elif SYNTHETIC not in data:
            raise store.StoreError("geodata %s: neither `pack:` nor `synthetic:`" % name)

    @property
    def kind(self):
        return PACK if self.manifest is not None else SYNTHETIC

    @property
    def is_pack(self):
        return self.kind == PACK

    @property
    def content_hash(self):
        """What a loss table on this ground depends on, hashed: the geodata
        file's content, and for a pack its manifest. A cached table or
        coverage raster is good while this is unchanged. The layer keys
        (the shadowing) are left out: they are put on a table, never
        computed into it, and without them the hash is what it always was."""
        data = {key: value for key, value in self.data.items() if key not in LAYER_KEYS}
        text = json.dumps([data, self.pack_manifest_hash], sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @property
    def crs_epsg(self):
        return self.manifest["region"]["crs_epsg"] if self.is_pack else None

    @property
    def exponent(self):
        return self.data[SYNTHETIC]["exponent"] if not self.is_pack else None

    @property
    def terrain(self):
        return self.data[SYNTHETIC]["terrain"] if not self.is_pack else None

    @property
    def extent_m(self):
        return self.data[SYNTHETIC]["extent_m"] if not self.is_pack else None

    @property
    def loc_pct(self):
        """The percentage of locations a pack's tables are asked at when the
        file states one; None: the planner's own."""
        return self.data.get(LOC_PCT)

    @property
    def shadowing_db(self):
        """The spread of each pair's shadowing draw, in dB; 0 is none."""
        return self.data.get(SHADOWING_DB, 0.0)

    @property
    def shadowing_seed(self):
        """Which draws: the same seed and names are the same ground."""
        return self.data.get(SHADOWING_SEED, 0)

    @property
    def bbox(self):
        """The extent in degrees, [lon0, lat0, lon1, lat1]: a pack's region,
        synthetic ground's square around 0°, 0°."""
        if self.is_pack:
            return [float(v) for v in self.manifest["region"]["bbox"]]
        half = self.extent_m / 2.0 / M_PER_DEGREE
        return [-half, -half, half, half]

    @property
    def origin(self):
        """The centre (lat, lon): a pack's region's, synthetic ground's 0°, 0°."""
        lon0, lat0, lon1, lat1 = self.bbox
        return ((lat0 + lat1) / 2, (lon0 + lon1) / 2)

    def holds(self, lat, lon):
        lon0, lat0, lon1, lat1 = self.bbox
        return lat0 <= lat <= lat1 and lon0 <= lon <= lon1

    def to_xy(self, lat, lon):
        """Degrees to the ground's metres: a pack's easting and northing, or
        synthetic ground's nautical-mile metres."""
        if self.is_pack:
            return self.tm.forward(lat, lon)
        return synthetic_xy(lat, lon)

    def to_latlon(self, x, y):
        if self.is_pack:
            return self.tm.inverse(x, y)
        return synthetic_latlon(x, y)

    def distance_m(self, a, b):
        """Metres between two (lat, lon), measured in the ground's own plane."""
        ax, ay = self.to_xy(*a)
        bx, by = self.to_xy(*b)
        return math.hypot(bx - ax, by - ay)

    def as_dict(self):
        """What the page is told about the geodata."""
        out = {"name": self.name, "kind": self.kind, "origin": list(self.origin),
               "bbox": self.bbox}
        if self.is_pack:
            manifest, _ = without_nodes(self.manifest)
            out.update(pack=self.pack_dir, crs_epsg=self.crs_epsg,
                       pack_manifest_hash=self.pack_manifest_hash,
                       layers=[layer_kind(layer) for layer in manifest["layers"]],
                       licences=[{"source": str(n.get("source", "")),
                                  "notice": str(n.get("notice", ""))}
                                 for n in manifest.get("licenses") or () if isinstance(n, dict)])
        else:
            out.update(exponent=self.exponent, terrain=self.terrain, extent_m=self.extent_m)
        out.update((key, self.data[key]) for key in STATED if key in self.data)
        return out


def parse(data, where):
    """A geodata file's mapping, checked and filled out."""
    if not isinstance(data, dict):
        raise store.StoreError("%s: not geodata" % where)
    if PACK in data:
        if not data[PACK]:
            raise store.StoreError("%s: `pack:` names no directory" % where)
        return {PACK: str(data[PACK]), **_stated(data, where)}
    ground = data.get(SYNTHETIC)
    if not isinstance(ground, dict):
        raise store.StoreError("%s: geodata is `pack: <dir>` or "
                               "`synthetic: {terrain, exponent, extent_m}`" % where)
    terrain = str(ground.get("terrain") or "flat")
    if terrain not in TERRAINS:
        raise store.StoreError("%s: terrain is one of %s, not %r"
                               % (where, ", ".join(TERRAINS), terrain))
    figures = {}
    for key, default in (("exponent", DEFAULT_EXPONENT), ("extent_m", DEFAULT_EXTENT_M)):
        value = ground.get(key, default)
        try:
            figures[key] = float(value)
        except (TypeError, ValueError):
            figures[key] = math.nan
        # A word is no number; a NaN or an infinity is no ground, and no
        # file could be written with one (store.scalar).
        if not math.isfinite(figures[key]):
            raise store.StoreError("%s: %s is a number, not %r" % (where, key, value))
    exponent, extent = figures["exponent"], figures["extent_m"]
    if extent <= 0:
        raise store.StoreError("%s: extent_m must be above 0" % where)
    return {SYNTHETIC: {"terrain": terrain,
                        "exponent": exponent,
                        "extent_m": extent},
            **_stated(data, where)}


def _stated(data, where):
    """The keys beyond `pack:` or `synthetic:` a file states, checked. One
    it does not state is not there at all, so a file without it reads,
    hashes and is written back as it was before the key existed.

    A key close to one of these but not it (`shadowing_dB`, `shadow_db`) is
    refused: passed over, it would leave the model silently off, which reads
    as a run like any other. Any other key is passed over, as before."""
    for key in data:
        if key in (PACK, SYNTHETIC) or key in STATED:
            continue
        near = difflib.get_close_matches(str(key).lower(), STATED, n=1, cutoff=0.8)
        if near:
            raise store.StoreError("%s: geodata has no key %s: did you mean %s?"
                                   % (where, key, near[0]))
    out = {}
    if LOC_PCT in data:
        if PACK not in data:
            raise store.StoreError("%s: loc_pct is a pack's: synthetic ground's log-distance "
                                   "has no percentage of locations" % where)
        out[LOC_PCT] = _number(data[LOC_PCT], LOC_PCT, where)
        low, high = LOC_PCT_RANGE
        if not low <= out[LOC_PCT] <= high:
            raise store.StoreError("%s: loc_pct is %g to %g, not %g"
                                   % (where, low, high, out[LOC_PCT]))
    if SHADOWING_DB in data:
        out[SHADOWING_DB] = _number(data[SHADOWING_DB], SHADOWING_DB, where)
        if out[SHADOWING_DB] < 0:
            raise store.StoreError("%s: shadowing_db is a spread in dB, 0 or more, not %g"
                                   % (where, out[SHADOWING_DB]))
    if SHADOWING_SEED in data:
        seed = data[SHADOWING_SEED]
        whole = isinstance(seed, int) or (isinstance(seed, float) and seed.is_integer())
        if isinstance(seed, bool) or not whole:
            raise store.StoreError("%s: shadowing_seed is a whole number, not %r" % (where, seed))
        out[SHADOWING_SEED] = int(seed)
    return out


def _number(value, key, where):
    """A finite number, or a StoreError naming the key that is not one. It
    is held to the nine decimals a file keeps (`store.scalar`), so a copy
    written into a run reads back as the very number this one holds."""
    try:
        out = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError):
        out = math.nan
    if not math.isfinite(out):
        raise store.StoreError("%s: %s is a number, not %r" % (where, key, value))
    return round(out, 9)


def read(path, name=None, refuse_packs=None):
    """A geodata file, loaded.

    With `refuse_packs`, a sentence with one `%s` for the geodata's name, a
    pack is refused with that sentence before its manifest is looked for:
    the front passes it when there is no planner, because the reason a pack
    is unusable then is the missing planner, whatever state the pack
    directory is in.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as err:
        raise store.StoreError("%s: %s" % (path, err)) from err
    if not name:
        base = os.path.basename(path)
        name = (os.path.basename(os.path.dirname(os.path.abspath(path))) if base == GEODATA_FILE
                else os.path.splitext(base)[0])
    data = parse(data, path)
    if refuse_packs and PACK in data:
        raise store.StoreError(refuse_packs % name)
    return Geodata(name, data, path)


def load(name, refuse_packs=None):
    path = geodata_path(name)
    if not os.path.isfile(path):
        raise store.StoreError("no geodata called %r" % name)
    return read(path, name, refuse_packs)


def dump(data):
    if PACK in data:
        text = "pack: %s\n" % store.scalar(data[PACK])
    else:
        ground = data[SYNTHETIC]
        text = ("synthetic:\n  terrain: %s\n  exponent: %s\n  extent_m: %s\n"
                % (ground["terrain"], store.scalar(ground["exponent"]),
                   store.scalar(ground["extent_m"])))
    return text + "".join("%s: %s\n" % (key, store.scalar(data[key]))
                          for key in STATED if key in data)


def write(path, data, comment=None):
    """Write a geodata file; `comment` lines go first, each behind a `#`."""
    head = "".join("# %s\n" % line for line in (comment or "").splitlines())
    store.write_text(path, head + dump(parse(data, path)))


# ---- renaming and deleting ------------------------------------------------

def pack_of(path):
    """The pack directory a geodata file names, read without loading the
    pack; None for synthetic ground or a file that cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not data.get(PACK):
        return None
    base = os.path.dirname(os.path.abspath(path))
    return os.path.normpath(os.path.join(base, os.path.expanduser(str(data[PACK]))))


def pack_users(pack_dir, but=None):
    """What stands on a pack, as (kind, name): other geodata that name it as
    their pack (`geodata`; `but` is left out), and runs and snapshots (`run`,
    `snapshot`)."""
    out = [("geodata", other) for other in names()
           if other != but and pack_of(geodata_path(other)) == pack_dir]
    for what, base in (("run", store.RUNS_DIR), ("snapshot", store.SNAPSHOTS_DIR)):
        if os.path.isdir(base):
            out += [(what, entry) for entry in sorted(os.listdir(base))
                    if pack_of(os.path.join(base, entry, GEODATA_FILE)) == pack_dir]
    return out


def own_pack(name):
    """The pack in the geodata's own directory, or None: a geodata whose pack
    is another's, or that is synthetic, holds none."""
    src = geodata_path(name)
    pack = pack_of(src)
    return pack if pack is not None and pack == os.path.dirname(os.path.abspath(src)) else None


def own_pack_users(name):
    """What stands on the geodata's own pack, as `pack_users` says it: a run
    or a snapshot, whose copies name the directory, or another geodata that
    names it as its pack (the same pack at another loc_pct or shadowing, not
    copied)."""
    pack = own_pack(name)
    return pack_users(pack, but=name) if pack else []


def taken_with(gone):
    """What deleting the geodata `gone` takes with it, so that nothing is left
    standing on a pack that is no more or on no geodata at all: whatever
    stands on their own packs (`own_pack_users`), and the nodesets with a node
    on one of them and on no geodata left after. {"geodata", "runs",
    "snapshots", "nodesets"}, each a sorted list of names."""
    out = {"geodata": set(), "run": set(), "snapshot": set()}
    for name in gone:
        for what, user in own_pack_users(name):
            out[what].add(user)
    leaving = set(gone) | out["geodata"]

    def extent(name):
        try:
            return load(name).bbox
        except store.StoreError:
            return None
    going = [b for b in map(extent, sorted(leaving)) if b]
    kept = [b for b in map(extent, sorted(set(names()) - leaving)) if b]
    nodesets = []
    for each in nodeset_module.names():
        try:
            ns = nodeset_module.load(each)
        except store.StoreError:
            continue
        if any(ns.inside(b) for b in going) and not any(ns.inside(b) for b in kept):
            nodesets.append(each)
    return {"geodata": sorted(out["geodata"] - set(gone)), "runs": sorted(out["run"]),
            "snapshots": sorted(out["snapshot"]), "nodesets": nodesets}


def _unheld(name, doing):
    """The geodata's directory, refused while anything stands on its own pack
    (`own_pack_users`)."""
    src = geodata_path(name)
    if not os.path.isfile(src):
        raise store.StoreError("no geodata called %r" % name)
    held = own_pack_users(name)
    if held:
        raise store.StoreError("%s stand%s on %s's pack: delete %s before %s %s"
                               % (", ".join("%s %s" % h for h in held),
                                  "" if len(held) > 1 else "s", name,
                                  "them" if len(held) > 1 else "it", doing, name))
    return os.path.dirname(src)


def rename(name, to):
    """Geodata by another name: its directory renamed."""
    src = _unheld(name, "renaming")
    dst = geodata_dir(to)
    if os.path.exists(dst):
        raise store.StoreError("there is already a geodata called %r" % to)
    os.rename(src, dst)


def delete(name):
    """Geodata gone: its directory, the pack in it included."""
    shutil.rmtree(_unheld(name, "deleting"))


def write_copy(gd, path):
    """Write loaded geodata to another place (a run's or a snapshot's
    `geodata.yaml`), its pack path re-based so it still names the same pack."""
    data = copy.deepcopy(gd.data)
    if gd.is_pack:
        data[PACK] = os.path.relpath(gd.pack_dir, os.path.dirname(os.path.abspath(path)))
    write(path, data, "geodata %s" % gd.name)


def rebase_text(text, src_dir, dst_dir):
    """A geodata file's text as it reads from another directory: a pack path
    re-based, synthetic ground unchanged. Works on the file without loading
    the pack."""
    data = yaml.safe_load(text) or {}
    if PACK in data:
        pack = os.path.normpath(os.path.join(src_dir, str(data[PACK])))
        data[PACK] = os.path.relpath(pack, dst_dir)
    return dump(parse(data, "geodata"))


# ---- nodes are never ground ----------------------------------------------

def layer_kind(layer):
    return layer.get("kind") if isinstance(layer, dict) else str(layer)


def without_nodes(manifest):
    """A manifest with no `Nodes` layer and no notice for one, and the pack
    paths of the files those layers were. Nodes belong to nodesets, so a
    pack sim-mesh keeps, draws or exports never carries a planner's baked-in
    deployed network."""
    out = copy.deepcopy(manifest)
    layers = out.get("layers") or []
    dropped = [layer.get("path") for layer in layers
               if layer_kind(layer) == NODES_LAYER and isinstance(layer, dict)]
    out["layers"] = [layer for layer in layers if layer_kind(layer) != NODES_LAYER]
    if isinstance(out.get("licenses"), list):
        out["licenses"] = [notice for notice in out["licenses"]
                           if not str((notice or {}).get("source", "")).startswith(NODES_NOTICE)]
    return out, [os.path.normpath(p) for p in dropped if p]


# ---- a sim-mesh geodata pack ---------------------------------------------

def export_zip(gd, out):
    """Geodata as a sim-mesh geodata pack, written to `out`, a binary stream
    that need not seek (a response being sent).

    The zip holds `geodata.yaml` at the top, its first line `# geodata
    <name>`; for a pack, the pack itself under `pack/`, which the yaml's
    `pack:` names. The pack goes without its `Nodes` layer, whose file and
    manifest entry and notice are left out, and without dot files.
    """
    head = "# geodata %s\n" % gd.name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=EXPORT_LEVEL) as zf:
        if not gd.is_pack:
            zf.writestr(GEODATA_MEMBER, head + dump(gd.data))
            return
        zf.writestr(GEODATA_MEMBER, head + dump({**gd.data, PACK: PACK_MEMBER}))
        manifest, dropped = without_nodes(gd.manifest)
        zf.writestr("%s/%s" % (PACK_MEMBER, MANIFEST), json.dumps(manifest, indent=1))
        skip = set(dropped) | {MANIFEST, GEODATA_FILE}
        for top, dirs, files in os.walk(gd.pack_dir):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for each in sorted(files):
                full = os.path.join(top, each)
                inner = os.path.relpath(full, gd.pack_dir)
                if each.startswith(".") or inner in skip:
                    continue
                zf.write(full, "%s/%s" % (PACK_MEMBER, inner.replace(os.sep, "/")))


def _manifest_root(members):
    """The directory inside a zip that holds `manifest.json`: '' for the
    root, or the one top-level directory that has it."""
    if MANIFEST in members:
        return ""
    tops = {m.split("/", 1)[0] for m in members if "/" in m}
    found = [t for t in tops if "%s/%s" % (t, MANIFEST) in members]
    if len(found) == 1:
        return found[0] + "/"
    raise store.StoreError("the zip holds neither %s nor a %s at its root or inside one "
                           "top-level directory" % (GEODATA_MEMBER, MANIFEST))


def _inside(inner, what):
    """A path inside a zip, normalised, refused when it would leave it."""
    inner = os.path.normpath(inner)
    if os.path.isabs(inner) or inner == ".." or inner.startswith("../"):
        raise store.StoreError("%s leaves the zip" % what)
    return inner


def _named(text):
    """The name in a `# geodata <name>` first line, or None."""
    first = text.split("\n", 1)[0].strip()
    if first.startswith("#"):
        words = first[1:].split()
        if len(words) == 2 and words[0] == "geodata" and store.NAME_RE.match(words[1]):
            return words[1]
    return None


def zip_name(zf):
    """The name a zip gives its geodata: a sim-mesh geodata pack's `# geodata`
    line, or a bare planner pack's manifest `name` made usable; None when it
    gives none."""
    members = [info.filename for info in zf.infolist() if not info.is_dir()]
    if GEODATA_MEMBER in members:
        return _named(zf.read(GEODATA_MEMBER).decode("utf-8", "replace"))
    try:
        manifest = json.loads(zf.read(_manifest_root(members) + MANIFEST).decode("utf-8"))
    except (store.StoreError, ValueError, KeyError):
        return None
    got = store.slug(manifest.get("name") if isinstance(manifest, dict) else None, "")
    return got or None


def import_zip(zip_path, name=None):
    """A zip as new geodata: a sim-mesh geodata pack (`geodata.yaml` at the
    top, and for a pack the pack directory its `pack:` names inside the zip),
    or a bare planner pack (a `manifest.json` at the top or inside one
    directory), which becomes pack geodata. `name` is the new geodata's; by
    default the one the zip gives. Returns the loaded geodata.

    A pack is expanded into the geodata's directory. Its members go under a
    `.part-` sibling first, the manifest is read and checked there, a `Nodes`
    layer's files and manifest entry are dropped, the geodata file is
    written, and only then is it renamed into place, so geodata that exists
    is geodata that was checked. A member that would land outside the pack
    is refused.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            name = store.check_name(name or zip_name(zf) or "", "geodata")
            if os.path.exists(geodata_dir(name)):
                raise store.StoreError("there is already geodata called %r" % name)
            members = [info.filename for info in zf.infolist() if not info.is_dir()]
            if GEODATA_MEMBER not in members:
                _expand_pack(zf, _manifest_root(members), name)
                return load(name)
            data = parse(yaml.safe_load(zf.read(GEODATA_MEMBER).decode("utf-8")) or {},
                         GEODATA_MEMBER)
            if SYNTHETIC in data:
                write(geodata_path(name), data, "imported geodata %s" % name)
                return load(name)
            root = _inside(data[PACK], "the geodata's pack")
            root = "" if root == "." else root + "/"
            if root + MANIFEST not in members:
                raise store.StoreError("the geodata names pack %s, and the zip has no %s there"
                                       % (data[PACK], MANIFEST))
            _expand_pack(zf, root, name, data)
            return load(name)
    except zipfile.BadZipFile as err:
        raise store.StoreError("not a zip: %s" % err) from err
    except yaml.YAMLError as err:
        raise store.StoreError("%s: %s" % (GEODATA_MEMBER, err)) from err


def _expand_pack(zf, root, name, data=None):
    """The pack under `root` in an open zip, with its geodata file, into the
    geodata's directory; the file keeps whatever else `data`, the zip's own
    geodata, states (its shadowing, its percentage of locations)."""
    dest = geodata_dir(name)
    os.makedirs(store.GEODATA_DIR, exist_ok=True)
    part = os.path.join(store.GEODATA_DIR, PART_PREFIX + name)
    shutil.rmtree(part, ignore_errors=True)
    try:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(root):
                continue
            inner = _inside(info.filename[len(root):], "member %s" % info.filename)
            target = os.path.join(part, inner)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
        manifest_path = os.path.join(part, MANIFEST)
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            region = manifest["region"]
            utm_zone_of(region["crs_epsg"])
            if len(region["bbox"]) != 4:
                raise ValueError("bbox is not four numbers")
        except (OSError, ValueError, KeyError, TypeError, store.StoreError) as err:
            raise store.StoreError("the pack's %s is not usable: %s" % (MANIFEST, err)) from err
        kept, dropped = without_nodes(manifest)
        if kept != manifest:
            for inner in dropped:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(part, _inside(inner, "the Nodes layer")))
            store.write_text(manifest_path, json.dumps(kept, indent=1))
        write(os.path.join(part, GEODATA_FILE), {**(data or {}), PACK: OWN_PACK},
              "imported pack %s" % manifest.get("name", name))
        os.rename(part, dest)
    finally:
        shutil.rmtree(part, ignore_errors=True)
