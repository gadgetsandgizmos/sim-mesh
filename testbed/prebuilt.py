#!/usr/bin/env python3
"""Prebuilt outputs: what sim-mesh compiles, fetched for the sources in the tree.

```
sim ── prebuilt.py fresh <c> ──► stamp + mtimes, else the key against the stamp
sim ── prebuilt.py fetch <c> ──► GET <host>/token?scope=repository:<name>:pull ──► {"token": T}
                             ──► GET <host>/v2/<name>/manifests/<c>-<key>-<arch>, Bearer T
                                                                  ──► 200 manifest | 404
                             ──► GET <host>/v2/<name>/blobs/<layers[0].digest>, Bearer T
                                                                  ──► 307 Location | 200 blob
                             ──► GET <Location>, no Authorization ──► the tar.gz
CI  ── prebuilt.py tag <c> / pack <c> --out DIR ──► oras push <repository>:<tag> DIR/<tag>.tar.gz
```

Four components, each built from one directory of the tree into a few files
that land in the tree where a local build leaves them (COMPONENTS). Each has
a **key**: a hash of every file under its source directory (outside the
directories it skips, hidden entries and `__pycache__`), by path, size and
bytes; a per-architecture component's key also holds the image's (`sim`
runs and builds everything in that image, and its outputs are linked
against the image's C library and C++ runtime). The key does not depend on
mtimes, on the order a directory lists files in, or on git: the same sources
are the same key on every checkout.

**Stored** as OCI artifacts in a container registry,
`SIM_MESH_PREBUILT` (default `ghcr.io/sim-mesh/sim-mesh-prebuilt`), tagged
`<component>-<key>-<arch>` (`any` for the page): a manifest of one layer, a
gzip tarball of the outputs by their paths in the tree. The layer's digest
is its sha256, which a fetch checks. Anonymous pulls, so the repository is
public; a registry on 127.0.0.1 or localhost is spoken to over plain HTTP.

**Fetched** into the tree: every member must be one of the component's
outputs; each output is unlinked before it is written (cargo hard-links its
binaries to copies under deps/, which writing through the link would
change), and dated now, so a source checked out before the fetch is not
newer than it. Then the **stamp**, `.prebuilt` in the output directory,
holding the key: a stamp means the outputs are exactly the prebuilt ones for
that key. A local build removes it first (`sim`'s build steps).

**Fresh** is: the outputs there, and either a stamp that no source is newer
than or whose key is the sources' (a checkout that touched files without
changing them), or no stamp and no source newer than the outputs (a local
build, as `sim` has always judged it).

Natively, a per-architecture output needs this machine's C library at least
as new as the image's (glibc 2.39, Ubuntu 24.04); an older one is said, and
`sim install --dev` builds the outputs here instead.

Exit statuses of `fresh`: 0 fresh, 1 there but of other sources, 3 not
there. Of the others: 0 done; 2 not available (not published, no
network, the C library too old, an architecture with no outputs: one line
each on stderr saying which); 1 an error worth showing (a digest that does
not match, a member outside the outputs, a registry answering wrongly).
"""

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOSITORY = "ghcr.io/sim-mesh/sim-mesh-prebuilt"
IMAGE_REPOSITORY = "ghcr.io/sim-mesh/sim-mesh"
# The key's scheme: a change to how keys are made changes this, so no key of
# the old scheme is ever taken for one of the new.
FORMAT = b"sim-mesh prebuilt 1\n"
ARCHES = ("x86_64", "aarch64")
GLIBC = (2, 39)                         # the image's: Ubuntu 24.04
STAMP = ".prebuilt"
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json, "
                  "application/vnd.docker.distribution.manifest.v2+json")
TIMEOUT = 10                            # seconds, for connecting and for each read
CHUNK = 1 << 16


class Component:
    """One thing sim-mesh compiles. `source` and `out` are relative to ROOT;
    `skip` are paths (globs) relative to `source`; `outputs` are the files
    that must be there, relative to ROOT; `tree`, when set, is a directory
    of outputs taken whole (the page's)."""

    def __init__(self, name, title, source, skip, out, outputs, tree=None, per_arch=True):
        self.name, self.title, self.source, self.skip = name, title, source, skip
        self.out, self.outputs, self.tree, self.per_arch = out, outputs, tree, per_arch


COMPONENTS = {c.name: c for c in (
    Component("radio", "the chip library and the time shim", "radio",
              ("build", "build.linux-*", "tests", "portduino"), "radio/build",
              ("radio/build/libsimclock.so", "radio/build/libsimradio-sx1262.so",
               "radio/build/libsimradio-lr2021.so")),
    Component("ether", "the ether's conductor", "ether/core", ("target",), "ether/build",
              ("ether/build/ether_core.abi3.so",)),
    Component("planner", "the planner", "planner", ("target",), "planner/target/release",
              ("planner/target/release/planner-web", "planner/target/release/planner-job")),
    Component("page", "the page", "testbed/ui", ("node_modules", "dist"), "testbed/ui/dist",
              ("testbed/ui/dist/spa/index.html",), tree="testbed/ui/dist/spa", per_arch=False),
)}


class Unavailable(Exception):
    """No prebuilt outputs to be had; the message says why."""


class Failed(Exception):
    """Something wrong that is worth showing."""


def say(message):
    print("sim: %s" % message, file=sys.stderr, flush=True)


def path(rel):
    return os.path.join(ROOT, *rel.split("/"))


# ---- keys -----------------------------------------------------------------------

def sources(component):
    """Every file the component is built from, as (relative path, full path),
    sorted by relative path."""
    top = path(component.source)
    found = []
    for dirpath, dirnames, filenames in os.walk(top):
        rel_dir = os.path.relpath(dirpath, top).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"

        def kept(name):
            # .cargo holds a component's own build settings (rustflags,
            # target): they change its outputs, so they belong to its key.
            hidden = name.startswith(".") and name != ".cargo"
            return not (hidden or name == "__pycache__"
                        or any(fnmatch.fnmatchcase(rel_dir + name, s) for s in component.skip))
        dirnames[:] = [d for d in dirnames if kept(d)]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if kept(name) and os.path.isfile(full):
                found.append((rel_dir + name, full))
    found.sort()
    return found


def sha256_file(name, digest=None):
    digest = digest or hashlib.sha256()
    with open(name, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest


def image_key(dev=False):
    """The image's key (or the development image's): the first 12 hex digits
    of the sha256 of the Dockerfile (followed by Dockerfile.dev)."""
    digest = sha256_file(path("Dockerfile"))
    if dev:
        sha256_file(path("Dockerfile.dev"), digest)
    return digest.hexdigest()[:12]


def key(component):
    digest = hashlib.sha256(FORMAT)
    if component.per_arch:
        digest.update(b"image " + image_key().encode() + b"\n")
    for rel, full in sources(component):
        digest.update(rel.encode() + b"\0" + str(os.path.getsize(full)).encode() + b"\0")
        sha256_file(full, digest)
    return digest.hexdigest()[:16]


def arch():
    machine = platform.machine()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)


def tag(component):
    return "%s-%s-%s" % (component.name, key(component), arch() if component.per_arch else "any")


# ---- freshness ------------------------------------------------------------------

def newer_source(component, than):
    """Whether a source file is newer than the mtime `than`."""
    return any(os.path.getmtime(full) > than for _, full in sources(component))


def built(component):
    """Whether its outputs are there, of whichever sources."""
    return all(os.path.isfile(path(o)) for o in component.outputs)


def fresh(component):
    outputs = [path(o) for o in component.outputs]
    if not built(component):
        return False
    stamp = os.path.join(path(component.out), STAMP)
    if not os.path.isfile(stamp):
        return not newer_source(component, min(os.path.getmtime(o) for o in outputs))
    # Always the key, not the mtimes: a source restored with an older mtime
    # (an archive, rsync -t) is newer in content than the stamp, never in time.
    with open(stamp, encoding="utf-8") as handle:
        return handle.read().strip() == key(component)


# ---- the registry ---------------------------------------------------------------

def repository():
    """(base URL, repository name) of SIM_MESH_PREBUILT."""
    spec = os.environ.get("SIM_MESH_PREBUILT") or REPOSITORY
    if spec == "off":
        raise Unavailable("SIM_MESH_PREBUILT is off: prebuilt outputs are never fetched")
    host, _, name = spec.partition("/")
    if not name or not host:
        raise Failed("SIM_MESH_PREBUILT is host/owner/name, not %s" % spec)
    local = host.rsplit(":", 1)[0] in ("127.0.0.1", "localhost")
    return ("http://" if local else "https://") + host, name


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is followed by hand, without the Authorization header,
    which urllib would carry to the new host."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def get(url, headers=None):
    """The response to GET url, or a redirect's HTTPError, raised."""
    return OPENER.open(urllib.request.Request(url, headers=headers or {}), timeout=TIMEOUT)


def token(base, name):
    host = urllib.parse.urlsplit(base).netloc
    url = "%s/token?%s" % (base, urllib.parse.urlencode(
        {"scope": "repository:%s:pull" % name, "service": host}))
    try:
        with get(url) as response:
            return "Bearer " + json.load(response)["token"]
    except urllib.error.HTTPError as e:
        if e.code == 404:               # a registry that asks for no token
            return None
        raise


def manifest(base, name, auth, ref):
    try:
        with get("%s/v2/%s/manifests/%s" % (base, name, ref),
                 dict(auth, Accept=MANIFEST_TYPES)) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise Unavailable("%s is not published" % ref) from None
        raise


def download(base, name, auth, digest, into):
    """The blob `digest` into the open file `into`, its sha256 checked."""
    url = "%s/v2/%s/blobs/%s" % (base, name, digest)
    try:
        response = get(url, auth)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308) or not e.headers.get("Location"):
            raise
        response = get(urllib.parse.urljoin(url, e.headers["Location"]))
    sha = hashlib.sha256()
    with response:
        for chunk in iter(lambda: response.read(CHUNK), b""):
            sha.update(chunk)
            into.write(chunk)
    if "sha256:" + sha.hexdigest() != digest:
        raise Failed("the download does not match its digest %s" % digest)


# ---- fetching -------------------------------------------------------------------

def check_glibc():
    try:
        name, version = os.confstr("CS_GNU_LIBC_VERSION").split()
    except (ValueError, OSError, AttributeError):
        raise Unavailable("this system's C library is not glibc, which the prebuilt binaries "
                          "need") from None
    have = tuple(int(part) for part in version.split(".")[:2])
    if name != "glibc" or have < GLIBC:
        raise Unavailable("this system's C library (glibc %s) is older than the prebuilt "
                          "binaries' (%d.%d)" % (version, *GLIBC))


def allowed(component, member):
    name = member.name
    if not (member.isfile() and not member.mode & 0o7000):
        return False
    if os.path.isabs(name) or ".." in name.split("/") or "\\" in name:
        return False
    return name in component.outputs or bool(component.tree and name.startswith(component.tree + "/"))


def unpack(component, archive, the_key):
    """The outputs from the tarball `archive` into the tree, then the stamp.
    Every member is checked before anything in the tree is touched."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        bad = [m.name for m in members if not allowed(component, m)]
        if bad:
            raise Failed("the archive holds %s, which is not one of its outputs" % bad[0])
        missing = set(component.outputs) - {m.name for m in members}
        if missing:
            raise Failed("the archive lacks %s" % sorted(missing)[0])
        stamp = os.path.join(path(component.out), STAMP)
        if os.path.exists(stamp):
            os.unlink(stamp)
        if component.tree and os.path.isdir(path(component.tree)):
            shutil.rmtree(path(component.tree))
        now = time.time()
        for member in members:
            target = path(member.name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.lexists(target):
                os.unlink(target)
            with tar.extractfile(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            os.chmod(target, member.mode & 0o777)
            os.utime(target, (now, now))
    part = stamp + ".part"
    with open(part, "w", encoding="utf-8") as handle:
        handle.write(the_key + "\n")
    os.replace(part, stamp)


def fetch(component, session):
    """Fetch one component's outputs. `session` caches the registry's token
    between components."""
    if component.per_arch:
        if arch() not in ARCHES:
            raise Unavailable("there are no prebuilt binaries for %s" % arch())
        check_glibc()
    the_key = key(component)
    ref = "%s-%s-%s" % (component.name, the_key, arch() if component.per_arch else "any")
    base, name = repository()
    if "unreachable" in session:
        raise session["unreachable"]
    try:
        if "auth" not in session:
            bearer = token(base, name)
            session["auth"] = {"Authorization": bearer} if bearer else {}
        found = manifest(base, name, session["auth"], ref)
        layers = found.get("layers") or []
        if not layers or not str(layers[0].get("digest", "")).startswith("sha256:"):
            raise Failed("%s's manifest names no layer" % ref)
        digest, size = layers[0]["digest"], layers[0].get("size")
        out = path(component.out)
        os.makedirs(out, exist_ok=True)
        say("%s: fetching the prebuilt outputs%s" % (
            component.title, " (%.1f MB)" % (size / 1e6) if isinstance(size, int) else ""))
        handle = tempfile.NamedTemporaryFile(dir=out, prefix=".fetch-", suffix=".tar.gz",
                                             delete=False)
        try:
            with handle:
                download(base, name, session["auth"], digest, handle)
            unpack(component, handle.name, the_key)
        finally:
            os.unlink(handle.name)
    except urllib.error.HTTPError as e:
        raise Failed("%s answered %d %s" % (base, e.code, e.reason)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # Said once, and the other components not tried: one wait offline.
        session["unreachable"] = Unavailable("%s cannot be reached (%s)"
                                             % (base, getattr(e, "reason", e)))
        raise session["unreachable"] from None
    except (ValueError, KeyError, tarfile.TarError, EOFError, OSError) as e:
        raise Failed("%s: %s" % (ref, e)) from None


def fetch_all(names):
    status, session = 0, {}
    for name in names:
        component = COMPONENTS[name]
        try:
            fetch(component, session)
        except Unavailable as e:
            say("%s: none prebuilt: %s" % (component.title, e))
            status = status or 2
        except Failed as e:
            say("%s: the prebuilt outputs could not be fetched: %s" % (component.title, e))
            status = 1
    return status


# ---- packing (CI) -----------------------------------------------------------------

def members(component):
    files = [path(o) for o in component.outputs]
    if component.tree:
        files = []
        for dirpath, dirnames, filenames in os.walk(path(component.tree)):
            dirnames.sort()
            files += [os.path.join(dirpath, f) for f in sorted(filenames)]
    return [(os.path.relpath(f, ROOT).replace(os.sep, "/"), f) for f in files]


def pack(component, out_dir):
    """<out_dir>/<tag>.tar.gz of the outputs in the tree, the same bytes for
    the same outputs."""
    missing = [o for o in component.outputs if not os.path.isfile(path(o))]
    if missing:
        raise Failed("%s is not built" % missing[0])
    os.makedirs(out_dir, exist_ok=True)
    name = os.path.join(out_dir, tag(component) + ".tar.gz")
    with open(name, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, full in members(component):
            info = tar.gettarinfo(full, arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode &= 0o755
            with open(full, "rb") as handle:
                tar.addfile(info, handle)
    return name


# ---- the command line -----------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="prebuilt.py", description=__doc__.split("\n")[0])
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb, help_text in (("key", "print the component's key"),
                            ("tag", "print its tag, <component>-<key>-<arch>"),
                            ("fresh", "exit 0 when its outputs are fresh, 1 when they are of "
                                      "other sources, 3 when they are not there")):
        verbs.add_parser(verb, help=help_text).add_argument("component", choices=COMPONENTS)
    verbs.add_parser("image-key", help="print the image's key, or the development image's") \
        .add_argument("dev", nargs="?", choices=["dev"])
    verbs.add_parser("fetch", help="fetch the components' prebuilt outputs") \
        .add_argument("components", nargs="+", choices=COMPONENTS)
    pack_args = verbs.add_parser("pack", help="write DIR/<tag>.tar.gz of the outputs in the tree")
    pack_args.add_argument("component", choices=COMPONENTS)
    pack_args.add_argument("--out", required=True, metavar="DIR")
    verbs.add_parser("components", help="print the components' names")
    args = parser.parse_args(argv)

    try:
        if args.verb == "key":
            print(key(COMPONENTS[args.component]))
        elif args.verb == "tag":
            print(tag(COMPONENTS[args.component]))
        elif args.verb == "fresh":
            component = COMPONENTS[args.component]
            return 0 if fresh(component) else 1 if built(component) else 3
        elif args.verb == "image-key":
            print(image_key(dev=bool(args.dev)))
        elif args.verb == "fetch":
            return fetch_all(args.components)
        elif args.verb == "pack":
            print(pack(COMPONENTS[args.component], args.out))
        elif args.verb == "components":
            print(" ".join(COMPONENTS))
    except Failed as e:
        say(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
