"""Prebuilt outputs: the key, freshness, a fetch from a registry of the
test's own (token, manifest, a blob that redirects to a host that refuses an
Authorization header), what a fetch refuses, the C library check, and a pack
fetched back byte for byte."""

import hashlib
import http.server
import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prebuilt  # noqa: E402

NAME = "owner/sim-mesh-prebuilt"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A tree with every component's sources and built outputs."""
    monkeypatch.setattr(prebuilt, "ROOT", str(tmp_path))
    monkeypatch.setattr(prebuilt, "arch", lambda: "x86_64")
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.39")
    files = {
        "Dockerfile": "FROM ubuntu:24.04\n",
        "Dockerfile.dev": "ARG BASE\nFROM ${BASE}\n",
        "radio/CMakeLists.txt": "project(simradio)\n",
        "radio/src/model.cpp": "int model;\n",
        "ether/core/Cargo.toml": "[package]\n",
        "ether/core/src/lib.rs": "fn x() {}\n",
        "planner/Cargo.toml": "[workspace]\n",
        "planner/crates/planner-web/src/main.rs": "fn main() {}\n",
        "testbed/ui/package.json": "{}\n",
        "testbed/ui/src/App.vue": "<template/>\n",
    }
    for rel, text in files.items():
        write(tmp_path, rel, text)
    old = time.time() - 1000
    for rel in files:
        os.utime(tmp_path / rel, (old, old))
    build(tmp_path)
    return tmp_path


def write(root, rel, data):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    target.write_bytes(data)
    return target


def build(root, tag=b"local"):
    """Every component's outputs, as a local build leaves them."""
    for component in prebuilt.COMPONENTS.values():
        for rel in component.outputs:
            write(root, rel, tag + b" " + rel.encode()).chmod(0o755)
    write(root, "testbed/ui/dist/spa/assets/index.js", tag + b" js")


def component(name):
    return prebuilt.COMPONENTS[name]


# ---- the key -----------------------------------------------------------------------

def test_the_key_is_the_same_whatever_the_mtimes_and_listing_order(tree, monkeypatch):
    before = {name: prebuilt.key(c) for name, c in prebuilt.COMPONENTS.items()}
    for rel in ("radio/src/model.cpp", "planner/Cargo.toml", "testbed/ui/src/App.vue"):
        os.utime(tree / rel)
    real_walk = os.walk

    def reversed_walk(top):
        for dirpath, dirnames, filenames in real_walk(top):
            dirnames.reverse()
            yield dirpath, dirnames, list(reversed(filenames))
    monkeypatch.setattr(os, "walk", reversed_walk)
    assert {name: prebuilt.key(c) for name, c in prebuilt.COMPONENTS.items()} == before
    assert all(len(k) == 16 for k in before.values())


def test_the_key_changes_with_a_byte_or_a_name(tree):
    before = prebuilt.key(component("radio"))
    write(tree, "radio/src/model.cpp", "int modem;\n")
    changed = prebuilt.key(component("radio"))
    assert changed != before
    os.rename(tree / "radio/src/model.cpp", tree / "radio/src/model2.cpp")
    assert prebuilt.key(component("radio")) not in (before, changed)
    write(tree, "radio/src/new.h", "")
    assert prebuilt.key(component("radio")) != changed


def test_the_key_ignores_what_is_skipped(tree):
    before = {name: prebuilt.key(c) for name, c in prebuilt.COMPONENTS.items()}
    for rel in ("radio/build/CMakeCache.txt", "radio/build.linux-aarch64/x.o", "radio/tests/t.py",
                "radio/portduino/p.cpp", "radio/.vscode/settings.json", "radio/src/.swp",
                "radio/src/__pycache__/m.pyc", "ether/core/target/release/x",
                "planner/target/release/planner-web.d", "testbed/ui/node_modules/q/index.js",
                "testbed/ui/dist/spa/other.js", "testbed/ui/.quasar/x.js"):
        write(tree, rel, "noise")
    assert {name: prebuilt.key(c) for name, c in prebuilt.COMPONENTS.items()} == before
    # A skipped name deeper down is a source like any other.
    write(tree, "radio/src/build/x.cpp", "")
    assert prebuilt.key(component("radio")) != before["radio"]


def test_the_per_arch_keys_hold_the_image(tree):
    before = {name: prebuilt.key(c) for name, c in prebuilt.COMPONENTS.items()}
    image = prebuilt.image_key()
    write(tree, "Dockerfile", "FROM ubuntu:24.04\n# changed\n")
    assert prebuilt.image_key() != image
    after = {name: prebuilt.key(c) for name, c in prebuilt.COMPONENTS.items()}
    for name in ("radio", "ether", "planner"):
        assert after[name] != before[name]
    assert after["page"] == before["page"]


def test_the_image_keys(tree):
    rkey = hashlib.sha256(b"FROM ubuntu:24.04\n").hexdigest()[:12]
    dkey = hashlib.sha256(b"FROM ubuntu:24.04\nARG BASE\nFROM ${BASE}\n").hexdigest()[:12]
    assert (prebuilt.image_key(), prebuilt.image_key(dev=True)) == (rkey, dkey)
    assert prebuilt.tag(component("planner")) == "planner-%s-x86_64" % prebuilt.key(component("planner"))
    assert prebuilt.tag(component("page")).endswith("-any")


# ---- freshness ---------------------------------------------------------------------

def test_a_local_build_is_fresh_until_a_source_is_newer(tree):
    assert all(prebuilt.fresh(c) for c in prebuilt.COMPONENTS.values())
    later = time.time() + 10
    os.utime(tree / "ether/core/src/lib.rs", (later, later))
    assert not prebuilt.fresh(component("ether"))
    assert prebuilt.fresh(component("planner"))


def test_a_missing_output_is_not_fresh(tree):
    os.unlink(tree / "radio/build/libsimradio-lr2021.so")
    assert not prebuilt.fresh(component("radio"))


def test_a_stamp_is_fresh_while_the_key_holds(tree):
    planner = component("planner")
    stamp = tree / "planner/target/release/.prebuilt"
    stamp.write_text(prebuilt.key(planner) + "\n")
    later = time.time() + 10
    os.utime(tree / "planner/Cargo.toml", (later, later))   # touched, not changed
    assert prebuilt.fresh(planner)
    assert os.path.getmtime(stamp) >= time.time() - 5
    write(tree, "planner/Cargo.toml", "[workspace]\n# edited\n")
    os.utime(tree / "planner/Cargo.toml", (later + 10, later + 10))
    assert not prebuilt.fresh(planner)


# ---- a registry of the test's own ----------------------------------------------------

class Registry:
    """/token, /v2/<name>/manifests/<tag>, /v2/<name>/blobs/<digest> (307 to
    /signed/<digest>, which refuses a request carrying Authorization)."""

    def __init__(self):
        self.blobs, self.manifests, self.requests = {}, {}, []
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                registry.requests.append((self.path, self.headers.get("Authorization")))
                prefix = "/v2/%s/" % NAME
                if self.path.startswith("/token?"):
                    assert "scope=repository%3A" + NAME.replace("/", "%2F") + "%3Apull" in self.path
                    return self.reply(200, json.dumps({"token": "T"}).encode())
                if self.path.startswith("/signed/"):
                    if self.headers.get("Authorization"):
                        return self.reply(400, b"no Authorization here")
                    return self.reply(200, registry.blobs[self.path[len("/signed/"):]])
                if self.headers.get("Authorization") != "Bearer T":
                    return self.reply(401, b"")
                if self.path.startswith(prefix + "manifests/"):
                    found = registry.manifests.get(self.path[len(prefix + "manifests/"):])
                    return self.reply(200, found) if found else self.reply(404, b"")
                if self.path.startswith(prefix + "blobs/"):
                    self.send_response(307)
                    self.send_header("Location", "/signed/" + self.path[len(prefix + "blobs/"):])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self.reply(404, b"")

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, tag, blob, digest=None):
        digest = digest or "sha256:" + hashlib.sha256(blob).hexdigest()
        self.blobs[digest] = blob
        self.manifests[tag] = json.dumps({
            "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "artifactType": "application/vnd.sim-mesh.prebuilt.v1",
            "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": digest, "size": len(blob)}]}).encode()


@pytest.fixture
def registry(monkeypatch):
    found = Registry()
    monkeypatch.setenv("SIM_MESH_PREBUILT", "127.0.0.1:%d/%s" % (found.server.server_port, NAME))
    yield found
    found.server.shutdown()


def packed(tree, name, tmp_path_factory, outputs=b"ci"):
    """The component packed from CI-built outputs, the tree's own then put back."""
    c = component(name)
    saved = {rel: (tree / rel).read_bytes() for rel, _ in prebuilt.members(c)}
    build(tree, outputs)
    blob = open(prebuilt.pack(c, str(tmp_path_factory.mktemp("out"))), "rb").read()
    for rel in [rel for rel, _ in prebuilt.members(c)]:
        os.unlink(tree / rel)
    for rel, data in saved.items():
        write(tree, rel, data)
    return blob


def snapshot(tree):
    found = {}
    for dirpath, _, filenames in os.walk(tree):
        for f in filenames:
            full = os.path.join(dirpath, f)
            found[os.path.relpath(full, tree)] = (open(full, "rb").read(), os.path.getmtime(full))
    return found


def test_a_fetch_unpacks_dates_and_stamps_the_outputs(tree, registry, tmp_path_factory):
    blob = packed(tree, "planner", tmp_path_factory)
    planner = component("planner")
    registry.publish(prebuilt.tag(planner), blob)
    # cargo's hard link: writing through it would change the copy under deps/.
    deps = tree / "planner/target/release/deps/planner_web-1234"
    deps.parent.mkdir()
    os.link(tree / "planner/target/release/planner-web", deps)
    started = time.time()
    assert prebuilt.main(["fetch", "planner"]) == 0
    for rel in planner.outputs:
        assert (tree / rel).read_bytes() == b"ci " + rel.encode()
        assert os.path.getmtime(tree / rel) >= started - 1
        assert os.access(tree / rel, os.X_OK)
    assert deps.read_bytes() == b"local planner/target/release/planner-web"
    assert (tree / "planner/target/release/.prebuilt").read_text().strip() == prebuilt.key(planner)
    assert not [f for f in os.listdir(tree / "planner/target/release") if f.startswith(".fetch-")]
    assert prebuilt.fresh(planner)
    # The blob was fetched from where it was redirected, without the token.
    assert ("/signed/" + "sha256:" + hashlib.sha256(blob).hexdigest(), None) in registry.requests


def test_a_fetched_page_replaces_the_old_one_whole(tree, registry, tmp_path_factory):
    blob = packed(tree, "page", tmp_path_factory)
    write(tree, "testbed/ui/dist/spa/assets/stale-1234.js", "old")
    registry.publish(prebuilt.tag(component("page")), blob)
    assert prebuilt.main(["fetch", "page"]) == 0
    assert (tree / "testbed/ui/dist/spa/assets/index.js").read_bytes() == b"ci js"
    assert not (tree / "testbed/ui/dist/spa/assets/stale-1234.js").exists()
    assert (tree / "testbed/ui/dist/.prebuilt").exists()


def test_pack_then_fetch_round_trips_the_bytes(tree, registry, tmp_path_factory):
    radio = component("radio")
    data = {rel: os.urandom(5000) for rel in radio.outputs}
    for rel, raw in data.items():
        write(tree, rel, raw)
    out = str(tmp_path_factory.mktemp("out"))
    blob = open(prebuilt.pack(radio, out), "rb").read()
    # The same outputs pack to the same bytes.
    assert open(prebuilt.pack(radio, str(tmp_path_factory.mktemp("again"))), "rb").read() == blob
    for rel in radio.outputs:
        write(tree, rel, "something else")
    registry.publish(prebuilt.tag(radio), blob)
    assert prebuilt.main(["fetch", "radio"]) == 0
    assert {rel: (tree / rel).read_bytes() for rel in radio.outputs} == data


def test_a_digest_that_does_not_match_leaves_the_tree(tree, registry, tmp_path_factory, capsys):
    blob = packed(tree, "ether", tmp_path_factory)
    registry.publish(prebuilt.tag(component("ether")), blob, digest="sha256:" + "0" * 64)
    before = snapshot(tree)
    assert prebuilt.main(["fetch", "ether"]) == 1
    assert snapshot(tree) == before
    assert "does not match its digest" in capsys.readouterr().err


def test_a_member_outside_the_outputs_leaves_the_tree(tree, registry, tmp_path_factory, capsys):
    # The planner's archive published under the ether's tag: none of it is the ether's.
    blob = packed(tree, "planner", tmp_path_factory)
    registry.publish(prebuilt.tag(component("ether")), blob)
    before = snapshot(tree)
    assert prebuilt.main(["fetch", "ether"]) == 1
    assert snapshot(tree) == before
    assert "planner/target/release/planner-web, which is not one of its outputs" \
        in capsys.readouterr().err


def test_an_unpublished_key_leaves_the_tree(tree, registry, capsys):
    before = snapshot(tree)
    assert prebuilt.main(["fetch", "radio", "page"]) == 2
    assert snapshot(tree) == before
    err = capsys.readouterr().err
    assert "the chip library and the time shim: none prebuilt: %s is not published" \
        % prebuilt.tag(component("radio")) in err
    assert "the page: none prebuilt" in err


def test_no_registry_is_not_available(tree, monkeypatch, capsys):
    monkeypatch.setattr(prebuilt, "TIMEOUT", 2)
    monkeypatch.setenv("SIM_MESH_PREBUILT", "127.0.0.1:9/%s" % NAME)
    assert prebuilt.main(["fetch", "page"]) == 2
    assert "cannot be reached" in capsys.readouterr().err


def test_a_registry_that_never_answers_is_waited_for_once(tree, monkeypatch, capsys):
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)                    # connections complete, and nothing is ever said
    monkeypatch.setattr(prebuilt, "TIMEOUT", 1)
    monkeypatch.setenv("SIM_MESH_PREBUILT", "127.0.0.1:%d/%s" % (silent.getsockname()[1], NAME))
    started = time.monotonic()
    try:
        assert prebuilt.main(["fetch", "radio", "ether", "planner", "page"]) == 2
    finally:
        silent.close()
    assert time.monotonic() - started < 1.9
    assert capsys.readouterr().err.count("cannot be reached") == 4


def test_an_older_c_library_is_not_available(tree, registry, tmp_path_factory, monkeypatch, capsys):
    registry.publish(prebuilt.tag(component("planner")), packed(tree, "planner", tmp_path_factory))
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.36")
    assert prebuilt.main(["fetch", "planner"]) == 2
    assert "glibc 2.36) is older than the prebuilt binaries' (2.39)" in capsys.readouterr().err
    assert registry.requests == []
    # The page is the same everywhere.
    registry.publish(prebuilt.tag(component("page")), packed(tree, "page", tmp_path_factory))
    assert prebuilt.main(["fetch", "page"]) == 0
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.41")
    assert prebuilt.main(["fetch", "planner"]) == 0


def test_an_architecture_without_outputs_is_not_available(tree, monkeypatch, capsys):
    monkeypatch.setattr(prebuilt, "arch", lambda: "riscv64")
    assert prebuilt.main(["fetch", "radio"]) == 2
    assert "no prebuilt binaries for riscv64" in capsys.readouterr().err


def test_the_command_line(tree, capsys):
    assert prebuilt.main(["components"]) == 0
    assert capsys.readouterr().out.split() == ["radio", "ether", "planner", "page"]
    assert prebuilt.main(["fresh", "radio"]) == 0
    later = time.time() + 10
    os.utime(tree / "radio/src/model.cpp", (later, later))
    assert prebuilt.main(["fresh", "radio"]) == 1      # there, of other sources
    os.unlink(tree / "radio/build/libsimclock.so")
    assert prebuilt.main(["fresh", "radio"]) == 3      # not there
    assert prebuilt.main(["pack", "radio", "--out", str(tree / "out")]) == 1
    assert "radio/build/libsimclock.so is not built" in capsys.readouterr().err


def test_the_key_holds_a_components_cargo_settings(tree):
    c = prebuilt.COMPONENTS["ether"]
    write(tree, "ether/core/.cargo/config.toml", "[build]\nrustflags = []\n")
    before = prebuilt.key(c)
    write(tree, "ether/core/.cargo/config.toml", "[build]\nrustflags = [\"-C\", \"target-cpu=native\"]\n")
    assert prebuilt.key(c) != before


def test_a_stamp_is_not_fresh_after_an_edit_with_an_older_mtime(tree):
    c = prebuilt.COMPONENTS["ether"]
    stamp = tree / c.out / prebuilt.STAMP
    stamp.write_text(prebuilt.key(c) + "\n")
    assert prebuilt.fresh(c)
    src = tree / "ether" / "core" / "src" / "lib.rs"
    old = os.path.getmtime(src)
    src.write_text("fn x() { /* restored from an archive */ }\n")
    os.utime(src, (old - 3600, old - 3600))
    assert not prebuilt.fresh(c)


def test_off_never_fetches(tree, monkeypatch):
    monkeypatch.setenv("SIM_MESH_PREBUILT", "off")
    with pytest.raises(prebuilt.Unavailable, match="off"):
        prebuilt.repository()
