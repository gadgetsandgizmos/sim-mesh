#!/usr/bin/env python3
"""Firmware: the station builds a node can run.

```
sim firmware add <zip|URL> ──► unzip to firmware/.part-<name>, check node.yaml,
                                    rename to firmware/<name>/
page "add from zip"  ── POST /api/firmware/add?name=<zip name> (the zip) ──► the same
page "add from pre-built" ── firmware_prebuilt ──► GET <index>/index.html ──► site
                          ── firmware_add {url} ──► GET <index>/<name>.zip ──► site
script nodes().firmware("<base>_latest") ──► resolve() ──► firmware/<newest of base>/
```

A firmware is installed as a directory `firmware/<name>/`, unpacked from a
zip of the same basename (`<name>.zip`), the form it is handed around in. The
zip, its `node.yaml` and its driver are specified by the firmware contract
(CONTRACT.md); this module is the reader.

**Names.** `<base>_<arch>_<version>`: `<base>` is lower-case letters, digits,
`-` and `.`, never `_`; `<arch>` is the architecture as `uname -m` spells it
on Linux (`aarch64`, `x86_64`); `<version>` is a build stamp, UTC
`YYYYMMDDhhmmss`, or a semantic version `1.2.3` (with an optional `-pre`
part). The name is split at its first and last `_`. Two firmwares that differ
in anything sim-mesh does not parse, the radio they drive included, are two
bases.

A base may end in the upstream release it packages, `<base>-<semver>`
(`meshtastic-sx1262-2.7.26_aarch64_20261007201200`: our build of 20261007
of Meshtastic 2.7.26), the version after it then being our own build's.

**`<base>_latest`** is shorthand for the newest installed firmware of that
base, for this machine's architecture: of `<base>` itself and every
`<base>-<semver>`, the highest upstream release (none ranks below any), then
within it the newest build — by stamp, or by semantic version, never both,
since a base holds builds of one scheme only. So
`meshtastic-sx1262-2.7.26_latest` is our newest build of 2.7.26, and
`meshtastic-sx1262_latest` our newest build of the newest release installed.

**Adding** checks a zip before it lands: its `node.yaml` must say the name's
three parts, its category and its executable and driver must be in it, its
architecture must be this machine's, and a base keeps one versioning scheme.
A firmware already installed under that name is refused, not replaced.

**Deleting** is refused for a firmware a paused run or a snapshot uses: their
state can only be resumed on the firmware that wrote it. The page's delete
(the front's `firmware_delete` with `stop_paused`) stops such paused runs for
good first, after warning; a snapshot's hold is always refused.

Every directory is whole or absent: a zip is unpacked under a `.part-` name,
checked, and renamed into place. Downloads are aiohttp in the caller's loop and
unzipping runs in a worker thread, so nothing here blocks an event loop. Run as
a script it is the CLI behind `sim firmware`.
"""

import argparse
import asyncio
import datetime
import html.parser
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import urllib.parse
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIRMWARE_DIR = os.path.join(ROOT, "firmware")
RUNS_DIR = os.path.join(HERE, "runs")
SNAPSHOTS_DIR = os.path.join(HERE, "snapshots")
PREBUILT_INDEX = os.environ.get("SIM_MESH_FIRMWARE_INDEX", "https://sim-mesh.net/firmware/")
LATEST = "latest"
NODE_YAML = "node.yaml"
ORIGIN_YAML = "origin.yaml"
PART_PREFIX = ".part-"
CHUNK = 1 << 16
FETCH_TIMEOUT_S = 600
LIST_TIMEOUT_S = 20
CATEGORIES = ("reticulum", "meshcore", "meshtastic")

BASE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
ARCH_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
STAMP_RE = re.compile(r"^\d{14}$")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
ARCH_ALIASES = {"arm64": "aarch64", "amd64": "x86_64", "x64": "x86_64"}


class FirmwareError(Exception):
    """A firmware that cannot be used, or a name that names none."""


def machine_arch():
    """This machine's architecture, spelled as `uname -m` spells it on Linux."""
    arch = platform.machine().lower()
    return ARCH_ALIASES.get(arch, arch)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- names -------------------------------------------------------------------

def scheme(version):
    """`stamp`, `semver`, or None for a version that is neither."""
    if STAMP_RE.match(version):
        return "stamp"
    if SEMVER_RE.match(version):
        return "semver"
    return None


def parse_name(name):
    """A firmware name as (base, arch, version), or None."""
    base, sep, rest = str(name).partition("_")
    arch, sep2, version = rest.rpartition("_")
    if not sep or not sep2 or not BASE_RE.match(base) or not ARCH_RE.match(arch):
        return None
    if scheme(version) is None:
        return None
    return base, arch, version


def make_name(base, arch, version):
    name = "%s_%s_%s" % (base, arch, version)
    if parse_name(name) != (base, arch, version):
        raise FirmwareError("%s: not a firmware name (base: lower-case letters, digits, - "
                            "and .; version: YYYYMMDDhhmmss or 1.2.3)" % name)
    return name


def version_key(version):
    """What versions of one scheme are ordered by."""
    if scheme(version) == "stamp":
        return (int(version),)
    major, minor, patch, pre = SEMVER_RE.match(version).groups()
    # A pre-release sorts before its release.
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def superseded(entries, adding):
    """The listed names a publish of `adding` takes the place of: the older
    versions, in the same scheme, of each base and arch being added. The list
    offers what is current; a project that publishes often would otherwise
    keep every version it ever put up."""
    older = []
    for name, f in entries.items():
        if name in adding:
            continue
        for new in adding.values():
            kind = scheme(new["version"])
            if (f.get("base"), f.get("arch")) == (new["base"], new["arch"]) \
                    and kind is not None and scheme(f["version"]) == kind \
                    and version_key(f["version"]) < version_key(new["version"]):
                older.append(name)
                break
    return sorted(older)


def latest_base(ref):
    """The base a `<base>_latest` names, or None for any other name."""
    base, sep, last = str(ref).rpartition("_")
    if sep and last == LATEST and BASE_RE.match(base):
        return base
    return None


# ---- node.yaml ---------------------------------------------------------------

def _inside(path, key, value):
    inner = os.path.normpath(str(value))
    if os.path.isabs(inner) or inner == ".." or inner.startswith("../"):
        raise FirmwareError("%s: `%s` leaves the firmware" % (path, key))
    return inner


def read_node_yaml(directory):
    """`node.yaml` of an unpacked firmware, checked: every required key,
    `exec`, `driver` and `fixed` inside it and present, `env` a mapping."""
    path = os.path.join(directory, NODE_YAML)
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except OSError as err:
        raise FirmwareError("%s: %s" % (path, err.strerror)) from err
    except yaml.YAMLError as err:
        raise FirmwareError("%s: %s" % (path, err)) from err
    if not isinstance(doc, dict):
        raise FirmwareError("%s: not a mapping" % path)
    for key in ("base", "arch", "version", "category", "exec", "driver"):
        if doc.get(key) in (None, ""):
            raise FirmwareError("%s: no `%s`" % (path, key))
        doc[key] = str(doc[key])
    make_name(doc["base"], doc["arch"], doc["version"])
    if doc["category"] not in CATEGORIES:
        raise FirmwareError("%s: category %r is not one of %s"
                            % (path, doc["category"], ", ".join(CATEGORIES)))
    env = doc.get("env")
    if env is not None and not isinstance(env, dict):
        raise FirmwareError("%s: `env` is a mapping" % path)
    doc["env"] = {str(k): str(v) for k, v in (env or {}).items()}
    for key in ("exec", "driver", "fixed"):
        if doc.get(key) not in (None, ""):
            doc[key] = _inside(path, key, doc[key])
    for key in ("exec", "driver"):
        if not os.path.isfile(os.path.join(directory, doc[key])):
            raise FirmwareError("%s: %s %s is not in the firmware" % (path, key, doc[key]))
    if doc.get("fixed") and not os.path.isdir(os.path.join(directory, doc["fixed"])):
        raise FirmwareError("%s: fixed %s is not in the firmware" % (path, doc["fixed"]))
    return doc


def read_origin(directory):
    try:
        with open(os.path.join(directory, ORIGIN_YAML), encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _env(base, env):
    """An `env` mapping, a value starting `./` or `../` taken as a path from `base`."""
    return {k: (os.path.normpath(os.path.join(base, v)) if v.startswith(("./", "../")) else v)
            for k, v in (env or {}).items()}


def result(directory):
    """An installed firmware as a run uses it."""
    node = read_node_yaml(directory)
    name = os.path.basename(directory)
    return {
        "name": name,
        "base": node["base"],
        "arch": node["arch"],
        "version": node["version"],
        "category": node["category"],
        "radio": node.get("radio"),
        "title": str(node.get("title") or name),
        "hardware": node.get("hardware"),
        "exec": os.path.join(directory, node["exec"]),
        "driver": os.path.join(directory, node["driver"]),
        "fixed": os.path.join(directory, node["fixed"]) if node.get("fixed") else None,
        "env": _env(directory, node["env"]),
        "dir": directory,
        "source": read_origin(directory).get("source"),
    }


# ---- what is installed ---------------------------------------------------------

def installed(firmware_dir=None):
    """Every installed firmware, {name: directory}."""
    base = firmware_dir or FIRMWARE_DIR
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return {}
    return {n: os.path.join(base, n) for n in sorted(names)
            if parse_name(n) and os.path.isfile(os.path.join(base, n, NODE_YAML))}


def upstream_of(base, family):
    """The upstream version a base of `family` names, as its sort key: `()`
    for the family's own base, the key of `1.2.3` for `<family>-1.2.3`, None
    for a base that is neither."""
    if base == family:
        return ()
    if not base.startswith(family + "-"):
        return None
    version = base[len(family) + 1:]
    return version_key(version) if scheme(version) == "semver" else None


def newest(base, firmware_dir=None, arch=None):
    """The name of the newest installed firmware of `base` for `arch`, or None:
    of the base itself and every `<base>-<semver>` beside it, the highest
    upstream version (a base without one below every one with one), then our
    newest build of it."""
    arch = arch or machine_arch()
    mine = []
    for parsed in map(parse_name, installed(firmware_dir)):
        if parsed and parsed[1] == arch:
            upstream = upstream_of(parsed[0], base)
            if upstream is not None:
                mine.append((upstream, version_key(parsed[2]), parsed))
    if not mine:
        return None
    return "%s_%s_%s" % max(mine)[2]


def resolve(ref, firmware_dir=None, arch=None):
    """A firmware name, or a `<base>_latest`, as what a run uses of it."""
    arch = arch or machine_arch()
    if not isinstance(ref, str) or not ref.strip():
        raise FirmwareError("firmware: empty")
    ref = ref.strip()
    base = latest_base(ref)
    if base is not None:
        name = newest(base, firmware_dir, arch)
        if name is None:
            raise FirmwareError("firmware %s: no %s firmware for %s is installed "
                                "(sim firmware add, or the Firmware page)" % (ref, base, arch))
    else:
        if parse_name(ref) is None:
            raise FirmwareError("firmware %s: a firmware is <base>_<arch>_<version> "
                                "or <base>_latest" % ref)
        name = ref
    path = os.path.join(firmware_dir or FIRMWARE_DIR, name)
    if not os.path.isfile(os.path.join(path, NODE_YAML)):
        raise FirmwareError("firmware %s: not installed" % name)
    got = result(path)
    if got["arch"] != arch:
        raise FirmwareError("firmware %s: built for %s, this machine is %s"
                            % (name, got["arch"], arch))
    return got


def row(name, directory, arch=None):
    """What the Firmware page and `firmware list` show of one firmware."""
    arch = arch or machine_arch()
    base, farch, version = parse_name(name)
    out = {"name": name, "base": base, "arch": farch, "version": version}
    try:
        got = result(directory)
        out.update({k: got[k] for k in ("category", "radio", "title", "hardware", "source")})
        if farch != arch:
            out["error"] = "built for %s, this machine is %s" % (farch, arch)
    except FirmwareError as err:
        out["error"] = str(err)
    return out


def listing(substring="", firmware_dir=None, runs_dir=None, snapshots_dir=None):
    """Every installed firmware whose name holds `substring`, newest first
    within a base, each with what holds it (`users`)."""
    held = holders(runs_dir, snapshots_dir)
    rows = [dict(row(n, d), users=held.get(n, []))
            for n, d in installed(firmware_dir).items() if substring in n]
    return newest_first(rows)


def newest_first(rows):
    """Rows by base and architecture, newest version first within each."""
    rows = sorted(rows, key=lambda r: version_key(r["version"]), reverse=True)
    return sorted(rows, key=lambda r: (r["base"], r["arch"]))


# ---- who holds what ----------------------------------------------------------

def _yaml(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def firmware_of(builds):
    """The firmware names a run.yaml's or a snapshot.yaml's `builds` holds."""
    return {str(b.get("firmware")) for b in (builds or {}).values()
            if isinstance(b, dict) and b.get("firmware")}


def holders(runs_dir=None, snapshots_dir=None):
    """{firmware name: ["run <name>" | "snapshot <name>", …]}: the paused runs
    and the snapshots whose state was written by that firmware."""
    out = {}
    runs_dir = runs_dir or RUNS_DIR
    snapshots_dir = snapshots_dir or SNAPSHOTS_DIR
    for base, what, file in ((runs_dir, "run", "run.yaml"),
                             (snapshots_dir, "snapshot", "snapshot.yaml")):
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            doc = _yaml(os.path.join(base, entry, file))
            if what == "run":
                paused = doc.get("paused")
                if not isinstance(paused, dict) or doc.get("resumed") or \
                        not os.path.isfile(os.path.join(base, entry, "paused", "snapshot.yaml")):
                    continue
            for name in firmware_of(doc.get("builds")):
                out.setdefault(name, []).append("%s %s" % (what, entry))
    return out


# ---- adding ------------------------------------------------------------------

def _schemes_of(base, firmware_dir):
    return {scheme(p[2]) for p in map(parse_name, installed(firmware_dir)) if p and p[0] == base}


def unpack(zip_path, shown, origin, firmware_dir=None, arch=None):
    """A firmware zip installed as `firmware/<name>/`, whole or not at all.

    Members are unpacked under a `.part-` sibling, `node.yaml` is read and
    checked there, and only then is the directory renamed into place. Members
    that would land outside the firmware are refused. Permission bits the zip
    carries are kept, and the executable is made executable either way.
    """
    arch = arch or machine_arch()
    firmware_dir = firmware_dir or FIRMWARE_DIR
    os.makedirs(firmware_dir, exist_ok=True)
    part = tempfile.mkdtemp(prefix=PART_PREFIX, dir=firmware_dir)
    try:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    inner = os.path.normpath(info.filename)
                    if os.path.isabs(info.filename) or inner == ".." or inner.startswith("../"):
                        raise FirmwareError("%s: member %s leaves the firmware"
                                            % (shown, info.filename))
                    target = os.path.join(part, inner)
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, CHUNK)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode:
                        os.chmod(target, mode)
        except zipfile.BadZipFile as err:
            raise FirmwareError("%s: %s" % (shown, err)) from err
        if not os.path.isfile(os.path.join(part, NODE_YAML)):
            raise FirmwareError("%s holds no %s at its top" % (shown, NODE_YAML))
        node = read_node_yaml(part)
        name = make_name(node["base"], node["arch"], node["version"])
        if node["arch"] != arch:
            raise FirmwareError("%s is built for %s, and this machine is %s"
                                % (shown, node["arch"], arch))
        others = _schemes_of(node["base"], firmware_dir) - {scheme(node["version"])}
        if others:
            raise FirmwareError("%s: %s is versioned by %s, and the %s firmware installed "
                                "already by %s" % (shown, name, scheme(node["version"]),
                                                   node["base"], others.pop()))
        dest = os.path.join(firmware_dir, name)
        if os.path.exists(dest):
            raise FirmwareError("%s is installed already" % name)
        path = os.path.join(part, node["exec"])
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with open(os.path.join(part, ORIGIN_YAML), "w", encoding="utf-8") as f:
            yaml.safe_dump(dict(origin, added=now_utc()), f, sort_keys=False)
        os.rename(part, dest)
    finally:
        shutil.rmtree(part, ignore_errors=True)
    return result(dest)


async def _download(url, path):
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise FirmwareError("%s: HTTP %d" % (url, resp.status))
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        f.write(chunk)
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise FirmwareError("%s: %s" % (url, str(err) or type(err).__name__)) from err


async def add(source, firmware_dir=None, arch=None, shown=None):
    """A firmware zip installed, from a path or an http(s) URL."""
    if "://" not in source:
        if not os.path.isfile(source):
            raise FirmwareError("%s: no such file" % source)
        return await asyncio.to_thread(unpack, source, shown or os.path.basename(source),
                                       {"source": os.path.abspath(source)}, firmware_dir, arch)
    firmware_dir = firmware_dir or FIRMWARE_DIR
    os.makedirs(firmware_dir, exist_ok=True)
    handle, tmp = tempfile.mkstemp(prefix=PART_PREFIX, suffix=".zip", dir=firmware_dir)
    os.close(handle)
    try:
        await _download(source, tmp)
        shown = shown or urllib.parse.unquote(source.rstrip("/").rsplit("/", 1)[-1])
        return await asyncio.to_thread(unpack, tmp, shown, {"source": source},
                                       firmware_dir, arch)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ---- deleting ----------------------------------------------------------------

def to_delete(substring, firmware_dir=None, runs_dir=None, snapshots_dir=None):
    """What `delete substring` would do: (deletable names, {held name: users})."""
    if not substring:
        raise FirmwareError("delete: name some part of the firmware to delete")
    held = holders(runs_dir, snapshots_dir)
    names = [n for n in installed(firmware_dir) if substring in n]
    return [n for n in names if n not in held], {n: held[n] for n in names if n in held}


def delete(names, firmware_dir=None, runs_dir=None, snapshots_dir=None):
    """Remove installed firmware by exact name, refusing any one a paused run
    or a snapshot holds."""
    held = holders(runs_dir, snapshots_dir)
    refused = {n: held[n] for n in names if n in held}
    if refused:
        raise FirmwareError("; ".join("%s is held by %s" % (n, ", ".join(u))
                                      for n, u in sorted(refused.items())))
    have = installed(firmware_dir)
    for name in names:
        if name not in have:
            raise FirmwareError("no firmware called %s" % name)
    for name in names:
        shutil.rmtree(have[name])
    return list(names)


# ---- pre-built ---------------------------------------------------------------

class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            got = {k: (v or "") for k, v in attrs}
            if got.get("href"):
                self.links.append(got)


def parse_index(text, base_url, arch=None):
    """The firmware zips a pre-built index lists for `arch`: [{name, url,
    category?, radio?, title?, hardware?}], newest first within a base. Each
    link's `data-*` attributes carry its `node.yaml` facts."""
    arch = arch or machine_arch()
    parser = _Links()
    parser.feed(text)
    parser.close()
    rows = []
    for attrs in parser.links:
        href = attrs["href"]
        name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
        if not name.endswith(".zip") or parse_name(name[:-4]) is None:
            continue
        name = name[:-4]
        base, farch, version = parse_name(name)
        if farch != arch:
            continue
        row = {"name": name, "base": base, "arch": farch, "version": version,
               "url": urllib.parse.urljoin(base_url, href)}
        for key in ("category", "radio", "title", "hardware"):
            if attrs.get("data-" + key):
                row[key] = attrs["data-" + key]
        rows.append(row)
    return newest_first(rows)


async def prebuilt(index_url=None, firmware_dir=None, arch=None):
    """What sim-mesh.net offers for this machine, each row saying whether it
    is installed here."""
    import aiohttp

    url = index_url or PREBUILT_INDEX
    url = url if url.endswith("/") else url + "/"
    timeout = aiohttp.ClientTimeout(total=LIST_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url + "index.html") as resp:
                if resp.status != 200:
                    raise FirmwareError("%sindex.html: HTTP %d" % (url, resp.status))
                text = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise FirmwareError("%sindex.html: %s" % (url, str(err) or type(err).__name__)) from err
    have = installed(firmware_dir)
    return [dict(r, installed=r["name"] in have) for r in parse_index(text, url, arch)]


# ---- the CLI -----------------------------------------------------------------

# ---- publishing --------------------------------------------------------------

PUBLISH_REPO = "sim-mesh/sim-mesh"
PUBLISH_SITE = "sim-mesh/sim-mesh.net"
PUBLISH_TAG = "firmware"
PUBLISHED_FACTS = ("base", "arch", "version", "category", "radio", "title", "hardware")
GITHUB_API = os.environ.get("SIM_MESH_GITHUB_API", "https://api.github.com")
UPLOAD_TIMEOUT_S = 3600


def zip_facts(path):
    """(name, facts) of a firmware zip about to be published: the name its
    node.yaml gives it, whatever the file is called, and its node.yaml's facts
    and its size."""
    name = os.path.basename(path)
    try:
        with zipfile.ZipFile(path) as zf:
            node = yaml.safe_load(zf.read(NODE_YAML))
    except (OSError, KeyError, zipfile.BadZipFile, yaml.YAMLError) as err:
        raise FirmwareError("%s: %s" % (path, err)) from err
    if not isinstance(node, dict):
        raise FirmwareError("%s: its node.yaml is no mapping" % name)
    try:
        want = make_name(str(node.get("base")), str(node.get("arch")), str(node.get("version")))
    except FirmwareError as err:
        raise FirmwareError("%s: its node.yaml: %s" % (name, err)) from err
    facts = {k: str(node[k]) for k in PUBLISHED_FACTS if node.get(k) not in (None, "")}
    facts["size"] = os.path.getsize(path)
    return want, facts


def index_html(entries):
    """The pre-built listing: one link a zip, its facts on it as data-*
    attributes (what parse_index reads), by base, then arch, then name."""
    rows = []
    order = sorted(entries.items(), key=lambda kv: (kv[1]["base"], kv[1]["arch"], kv[0]))
    for name, f in order:
        attrs = "".join(' data-%s="%s"' % (k, html.escape(f[k]))
                        for k in ("category", "radio", "title", "hardware") if f.get(k))
        rows.append('<tr><td><a href="%s.zip"%s>%s</a></td><td>%s</td><td>%s</td>'
                    '<td>%s</td><td>%s</td><td>%.1f MB</td></tr>'
                    % (html.escape(name), attrs, html.escape(name),
                       html.escape(f.get("title", "")), html.escape(f.get("category", "")),
                       html.escape(f.get("radio", "")), html.escape(f.get("arch", "")),
                       f.get("size", 0) / 1e6))
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pre-built firmware — sim-mesh</title>
<style>
body { font: 15px/1.5 Lato, system-ui, sans-serif; margin: 2em auto; max-width: 960px;
       padding: 0 16px; color: #1f2933; background: #fff; }
table { border-collapse: collapse; width: 100%%; }
td, th { text-align: left; padding: 6px 10px; border-bottom: 1px solid #e4e7eb; }
td:first-child a { font-family: ui-monospace, monospace; font-size: 13px; }
@media (prefers-color-scheme: dark) { body { background: #111418; color: #e4e7eb; }
  td, th { border-color: #2a2f36; } a { color: #8ab4f8; } }
</style>
</head>
<body>
<h1>Pre-built firmware</h1>
<p>Firmware zips for <a href="../">sim-mesh</a>: add one with
<code>sim firmware add &lt;its URL&gt;</code>, or from the Firmware tab's
<b>Download pre-built firmware</b>. What a zip is: <a href="../contract/">the firmware contract</a>.</p>
<table>
<thead><tr><th>Firmware</th><th>What it is</th><th>Category</th><th>Radio</th><th>Arch</th><th>Size</th></tr></thead>
<tbody>
%s
</tbody>
</table>
</body>
</html>
""" % "\n".join(rows)


class _GitHub:
    """The few calls of GitHub's REST API publishing makes, with the token
    from GH_TOKEN (or GITHUB_TOKEN), which `sim` takes from the gh logged in
    where it runs."""

    def __init__(self, session, need_token=True):
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token and need_token:
            raise FirmwareError("publishing needs a GitHub token: log in with gh where sim "
                                "runs, or set GH_TOKEN")
        self.session = session
        self.api = GITHUB_API.rstrip("/")
        self.headers = {"Accept": "application/vnd.github+json",
                        "User-Agent": "sim-mesh (+https://github.com/sim-mesh/sim-mesh)"}
        if token:
            self.headers["Authorization"] = "Bearer " + token

    async def _call(self, method, url, ok, what, **kwargs):
        headers = dict(self.headers, **kwargs.pop("headers", {}))
        async with self.session.request(method, url, headers=headers, **kwargs) as resp:
            if resp.status not in ok:
                raise FirmwareError("%s: HTTP %d %s" % (what, resp.status,
                                                        (await resp.text())[:200]))
            if resp.status == 404:
                return None
            if "json" in (resp.headers.get("Content-Type") or ""):
                return await resp.json()
            return await resp.read()

    async def release(self, repo, tag):
        return await self._call("GET", "%s/repos/%s/releases/tags/%s" % (self.api, repo, tag),
                                (200, 404), "%s: release %s" % (repo, tag))

    async def make_release(self, repo, tag):
        return await self._call("POST", "%s/repos/%s/releases" % (self.api, repo), (201,),
                                "%s: making release %s" % (repo, tag), json={
                                    "tag_name": tag, "name": "Pre-built firmware",
                                    "body": "The firmware zips sim-mesh.net/firmware/ offers. "
                                            "Published with `sim firmware publish`."})

    async def asset(self, repo, asset):
        return await self._call("GET", "%s/repos/%s/releases/assets/%d" % (
            self.api, repo, asset["id"]), (200,), "%s: %s" % (repo, asset["name"]),
            headers={"Accept": "application/octet-stream"})

    async def delete_asset(self, repo, asset):
        await self._call("DELETE", "%s/repos/%s/releases/assets/%d" % (
            self.api, repo, asset["id"]), (204,), "%s: deleting %s" % (repo, asset["name"]))

    async def upload(self, rel, path, name):
        import aiohttp

        with open(path, "rb") as body:
            await self._call(
                "POST", rel["upload_url"].split("{", 1)[0], (201,), "uploading %s" % name,
                params={"name": name}, data=body,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(os.path.getsize(path))},
                timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT_S))

    async def dispatch(self, repo, event):
        await self._call("POST", "%s/repos/%s/dispatches" % (self.api, repo), (204,),
                         "%s: %s" % (repo, event), json={"event_type": event})


async def publish(zips, delete=(), dry_run=False, repo=PUBLISH_REPO, site=PUBLISH_SITE,
                  say=print, keep_older=False):
    """Firmware zips put on the pre-built list, and named ones taken off it,
    with the older versions of what is added unless `keep_older`.

    The list is the `firmware` release of `repo`: every zip, `firmware.yaml`
    (each zip's node.yaml facts, by name) and the `index.html` made from it.
    A zip of a name already there is replaced, and, unless `keep_older`, the
    listed versions of its base and arch older than it come off. The zips go up first and the
    listing last, so the listing never names a zip that is not there; then
    `site` is told to redeploy (`firmware-published`), since a browser cannot
    fetch a release's assets across origins and the site serves a copy."""
    import aiohttp

    adding = {}
    paths = {}
    for path in zips:
        name, facts = zip_facts(path)
        adding[name] = facts
        paths[name] = path
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)) \
            as session:
        hub = _GitHub(session, need_token=not dry_run)
        rel = await hub.release(repo, PUBLISH_TAG)
        assets = {a["name"]: a for a in (rel or {}).get("assets") or ()}
        entries = {}
        if "firmware.yaml" in assets:
            listed = yaml.safe_load(await hub.asset(repo, assets["firmware.yaml"])) or {}
            entries = listed.get("firmware") or {}
        delete = list(delete)
        for name in delete:
            if name not in entries:
                raise FirmwareError("%s is not on the list" % name)
            del entries[name]
        older = [] if keep_older else superseded(entries, adding)
        for name in older:
            del entries[name]
        delete = list(delete) + older
        entries.update(adding)
        for name in sorted(adding):
            say("add     %s" % name)
        for name in delete:
            say("delete  %s%s" % (name, " (an older version)" if name in older else ""))
        if dry_run:
            return entries
        if rel is None:
            rel = await hub.make_release(repo, PUBLISH_TAG)
        with tempfile.TemporaryDirectory() as tmp:
            listing_files = []
            for name, text in (("firmware.yaml", yaml.safe_dump({"firmware": entries},
                                                                  sort_keys=True)),
                               ("index.html", index_html(entries))):
                listing_files.append((name, os.path.join(tmp, name)))
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write(text)
            for name, path in [(n + ".zip", paths[n]) for n in sorted(adding)] + listing_files:
                if name in assets:
                    await hub.delete_asset(repo, assets[name])
                say("uploading %s" % name)
                await hub.upload(rel, path, name)
        for name in delete:
            if name + ".zip" in assets:
                await hub.delete_asset(repo, assets[name + ".zip"])
        await hub.dispatch(site, "firmware-published")
        say("published; %s redeploys" % site)
    return entries


def _print_rows(rows):
    for r in rows:
        held = "   held by %s" % ", ".join(r["users"]) if r.get("users") else ""
        mark = "   ! %s" % r["error"] if r.get("error") else ""
        print("%-48s %-10s %-8s %s%s%s" % (r["name"], r.get("category") or "",
                                           r.get("radio") or "", r.get("title") or "", held, mark))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sim firmware",
                                 description="the firmware a node can run")
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("add", help="install a firmware zip, from a file or a URL")
    p.add_argument("zip", nargs="+")
    p = sub.add_parser("list", help="the installed firmware whose name holds SUBSTRING")
    p.add_argument("substring", nargs="?", default="")
    p = sub.add_parser("delete", help="remove the installed firmware whose name holds "
                                      "SUBSTRING, after listing it for confirmation")
    p.add_argument("-f", "--force", action="store_true", help="no confirmation")
    p.add_argument("substring")
    p = sub.add_parser("prebuilt", help="what %s offers for this machine" % PREBUILT_INDEX)
    p.add_argument("--index", default=None)
    p = sub.add_parser("resolve", help="what a firmware name runs, as JSON")
    p.add_argument("firmware")
    p = sub.add_parser("publish", help="put firmware zips on the pre-built list "
                                       "(sim-mesh.net/firmware/), or take them off")
    p.add_argument("zip", nargs="*")
    p.add_argument("--delete", nargs="+", default=[], metavar="NAME",
                   help="take these off the list")
    p.add_argument("--keep-older", action="store_true",
                   help="keep the older versions of what is added on the list")
    p.add_argument("-n", "--dry-run", action="store_true", help="say what it would do")
    p.add_argument("--repo", default=PUBLISH_REPO, help="whose `firmware` release is the list")
    p.add_argument("--site", default=PUBLISH_SITE, help="the site told to redeploy")
    args = ap.parse_args(argv)

    # A relative path is read from where `sim` was run, which it passes into
    # its container.
    base = os.environ.get("SIM_MESH_CALLER_DIR")

    def caller(path):
        return os.path.join(base, path) if base and not os.path.isabs(path) else path

    try:
        if args.verb == "add":
            for source in args.zip:
                if "://" not in source:
                    source = caller(source)
                got = asyncio.run(add(source))
                print("added %s" % got["name"])
        elif args.verb == "publish":
            if not args.zip and not args.delete:
                ap.error("publish: name zips to add, or --delete names")
            asyncio.run(publish([caller(z) for z in args.zip], args.delete, args.dry_run,
                                args.repo, args.site, keep_older=args.keep_older))
        elif args.verb == "list":
            _print_rows(listing(args.substring))
        elif args.verb == "prebuilt":
            for r in asyncio.run(prebuilt(args.index)):
                print("%-48s %-10s %-8s %s%s" % (r["name"], r.get("category") or "",
                                                 r.get("radio") or "", r.get("title") or "",
                                                 "   installed" if r["installed"] else ""))
        elif args.verb == "delete":
            names, held = to_delete(args.substring)
            for name, users in sorted(held.items()):
                print("kept: %s, held by %s" % (name, ", ".join(users)))
            if not names:
                print("nothing to delete" + (" that is not held" if held else ""))
                return 1 if held else 0
            if not args.force:
                print("to delete:")
                for name in names:
                    print("  " + name)
                if not sys.stdin.isatty():
                    print("not deleting without -f when nothing can confirm", file=sys.stderr)
                    return 1
                if input("delete %d firmware? [y/N] " % len(names)).strip().lower() not in ("y", "yes"):
                    return 1
            for name in delete(names):
                print("deleted %s" % name)
        else:
            print(json.dumps(resolve(args.firmware), indent=2))
    except FirmwareError as err:
        print("sim firmware: %s" % err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
