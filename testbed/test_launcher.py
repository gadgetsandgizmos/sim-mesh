"""The launcher's container engine, with a stand-in that records what it is
asked: the flags a rootless Podman needs and Docker does not, the port floor a
station's web UI needs under either, the verbs run by the launcher's own path
whatever directory they are typed in, the image pulled (or built when it
cannot be) by the Dockerfile's sha256, the development image `sim dev` and
`sim build` build and leave the checkout on, the old images removed and no
others, and `sim install`'s .native choosing this machine."""

import hashlib
import os
import shutil
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "sim")

# Images it has not are named in <bin>/missing, those it cannot pull in
# <bin>/unpullable, one per line; `images` lists <bin>/images.
STANDIN = """#!/bin/sh
echo "$@" >> "%(log)s"
here=$(dirname "$0")
case "$1" in
    info) exit 0 ;;
    image) ! grep -qxF "$3" "$here/missing" 2>/dev/null ;;
    pull) ! grep -qxF "$2" "$here/unpullable" 2>/dev/null ;;
    images) cat "$here/images" 2>/dev/null; exit 0 ;;
    ps) echo abc123 ;;
esac
"""


# What the launcher runs besides an engine, linked into the stand-in's own
# directory: the launcher sees that directory and nothing else, so it can never
# find a real docker or podman on the machine.
TOOLS = ("bash", "sh", "env", "cksum", "cut", "grep", "id", "uname", "dirname", "cat", "sed",
         "tr", "head", "readlink", "basename", "sha256sum", "rm")


def standin(tmp_path, name):
    """An engine called `name` that logs every call, one per line."""
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (bin_dir / tool).symlink_to(found)
    log = tmp_path / ("%s.log" % name)
    path = bin_dir / name
    path.write_text(STANDIN.replace("#!/bin/sh", "#!%s" % shutil.which("sh")) % {"log": log})
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def launch(args, bin_dir, cwd, launcher=LAUNCHER, **env):
    environ = dict(os.environ, PATH=str(bin_dir))
    for key in ("SIM_MESH_ENGINE", "SIM_MESH_NATIVE", "SIM_MESH_IN_CONTAINER", "SIM_MESH_FRONT",
                "SIM_MESH_IMAGE_REPO", "SIM_MESH_PREBUILT"):
        environ.pop(key, None)
    # This checkout's launcher in the container, whether `sim install` has
    # been run here (.native) or not.
    if launcher == LAUNCHER:
        environ["SIM_MESH_NATIVE"] = "0"
    environ.update(env)
    return subprocess.run([launcher, *args], cwd=cwd, env=environ, capture_output=True,
                          text=True, timeout=60)


def checkout(tmp_path):
    """The launcher and its Dockerfiles in a directory of their own, where
    what it leaves (.dev, .native) lands, not in this checkout."""
    where = tmp_path / "mesh" / "sim-mesh"
    where.mkdir(parents=True)
    for name in ("sim", "Dockerfile", "Dockerfile.dev"):
        shutil.copy2(os.path.join(ROOT, name), where / name)
    return where


def image_names(where, repo="ghcr.io/sim-mesh/sim-mesh"):
    """The image's and the development image's names, by the Dockerfiles."""
    runtime = (where / "Dockerfile").read_bytes()
    dev = runtime + (where / "Dockerfile.dev").read_bytes()
    return ("%s:%s" % (repo, hashlib.sha256(runtime).hexdigest()[:12]),
            "sim-mesh-dev:%s" % hashlib.sha256(dev).hexdigest()[:12])


def calls(log, verb):
    return [line.split() for line in log.read_text().splitlines() if line.split()[:1] == [verb]]


def test_podman_keeps_the_callers_id_and_lets_a_station_have_port_80(tmp_path):
    bin_dir, log = standin(tmp_path, "podman")
    done = launch(["firmware", "list"], bin_dir, tmp_path, SIM_MESH_ENGINE="podman")
    assert done.returncode == 0, done.stderr
    [run] = calls(log, "run")
    assert "--sysctl" in run and "net.ipv4.ip_unprivileged_port_start=0" in run
    assert ("--userns=keep-id" in run) == (os.getuid() != 0)
    assert "--user" not in run
    assert run[-3:] == [LAUNCHER, "firmware", "list"]


def test_a_verb_runs_the_launcher_by_its_path_from_the_directory_above(tmp_path):
    bin_dir, log = standin(tmp_path, "podman")
    above = os.path.dirname(ROOT)
    done = launch(["list"], bin_dir, above, SIM_MESH_ENGINE="podman")
    assert done.returncode == 0, done.stderr
    [run] = calls(log, "exec")
    assert run[run.index("-w") + 1] == above
    assert run[-2:] == [LAUNCHER, "list"]


def test_podman_is_the_default_and_docker_is_left_alone(tmp_path):
    bin_dir, log = standin(tmp_path, "podman")
    standin(tmp_path, "docker")
    done = launch(["firmware", "list"], bin_dir, tmp_path)
    assert done.returncode == 0, done.stderr
    assert calls(log, "run")
    assert not (tmp_path / "docker.log").exists()


def test_docker_is_taken_where_there_is_no_podman_and_gets_no_podman_flags(tmp_path):
    bin_dir, log = standin(tmp_path, "docker")
    done = launch(["firmware", "list"], bin_dir, tmp_path)
    assert done.returncode == 0, done.stderr
    [run] = calls(log, "run")
    assert "--userns=keep-id" not in run and "net.ipv4.ip_unprivileged_port_start=0" in run
    assert run[run.index("--user") + 1] == "%d:%d" % (os.getuid(), os.getgid())


def test_with_neither_engine_it_says_so(tmp_path):
    bin_dir, _ = standin(tmp_path, "unused")
    (bin_dir / "unused").unlink()
    done = launch(["firmware", "list"], bin_dir, tmp_path)
    assert done.returncode != 0 and "neither podman nor docker" in done.stderr


def test_a_front_elsewhere_starts_nothing_here_and_needs_no_engine(tmp_path):
    bin_dir, log = standin(tmp_path, "podman")
    done = launch([], bin_dir, tmp_path, SIM_MESH_FRONT="127.0.0.1:1")
    assert done.returncode != 0 and "start it there" in done.stderr
    done = launch(["new", "x", "--geodata", "g", "--nodeset", "n"], bin_dir, tmp_path,
                  SIM_MESH_FRONT="127.0.0.1:1")
    assert done.returncode != 0 and "nothing answers at 127.0.0.1:1" in done.stderr
    assert not log.exists()


def test_a_front_elsewhere_is_host_and_port(tmp_path):
    bin_dir, log = standin(tmp_path, "podman")
    done = launch(["list"], bin_dir, tmp_path, SIM_MESH_FRONT="nowhere")
    assert done.returncode != 0 and "host:port" in done.stderr
    assert not log.exists()


def test_the_library_finds_the_front_where_it_is_said_to_be():
    from sim_mesh import sim
    assert sim.front_address({}) == ("127.0.0.1", 8800)
    assert sim.front_address({"SIM_MESH_PORT": "8801"}) == ("127.0.0.1", 8801)
    assert sim.front_address({"SIM_MESH_FRONT": "host.docker.internal:8802",
                              "SIM_MESH_PORT": "8801"}) == ("host.docker.internal", 8802)
    try:
        sim.front_address({"SIM_MESH_FRONT": "nowhere"})
    except ValueError:
        pass
    else:
        raise AssertionError("a front without a port was taken")


def test_the_image_is_named_by_the_dockerfiles_sha256_and_pulled_when_missing(tmp_path):
    where = checkout(tmp_path)
    bin_dir, log = standin(tmp_path, "docker")
    runtime, _ = image_names(where)
    (bin_dir / "missing").write_text(runtime + "\n")
    done = launch(["firmware", "list"], bin_dir, tmp_path, where / "sim")
    assert done.returncode == 0, done.stderr
    assert calls(log, "pull") == [["pull", runtime]]
    assert not calls(log, "build")
    [run] = calls(log, "run")
    assert run[-4:] == [runtime, str(where / "sim"), "firmware", "list"]
    assert "pulling the sim-mesh image (%s" % runtime in done.stderr


def test_an_image_that_cannot_be_pulled_is_built_under_the_same_name(tmp_path):
    where = checkout(tmp_path)
    bin_dir, log = standin(tmp_path, "docker")
    runtime, _ = image_names(where, "ghcr.io/fork/sim-mesh")
    (bin_dir / "missing").write_text(runtime + "\n")
    (bin_dir / "unpullable").write_text(runtime + "\n")
    done = launch(["firmware", "list"], bin_dir, tmp_path, where / "sim",
                  SIM_MESH_IMAGE_REPO="ghcr.io/fork/sim-mesh",
                  SIM_MESH_PREBUILT="ghcr.io/fork/sim-mesh-prebuilt")
    assert done.returncode == 0, done.stderr
    [build] = calls(log, "build")
    assert build == ["build", "-t", runtime, "-f", str(where / "Dockerfile"), str(where)]
    [run] = calls(log, "run")
    assert run[-4] == runtime
    # Both names go in, for the image's own `sim` and its fetches.
    assert "SIM_MESH_IMAGE_REPO=ghcr.io/fork/sim-mesh" in run
    assert "SIM_MESH_PREBUILT=ghcr.io/fork/sim-mesh-prebuilt" in run


def test_build_runs_in_the_development_image_and_plain_sim_stays_there(tmp_path):
    where = checkout(tmp_path)
    bin_dir, log = standin(tmp_path, "docker")
    runtime, dev = image_names(where)
    (bin_dir / "missing").write_text(dev + "\n")
    done = launch(["build", "planner"], bin_dir, tmp_path, where / "sim")
    assert done.returncode == 0, done.stderr
    [build] = calls(log, "build")
    assert build == ["build", "--build-arg", "BASE=" + runtime, "-t", dev,
                     "-f", str(where / "Dockerfile.dev"), str(where)]
    [run] = calls(log, "run")
    assert run[-4:] == [dev, str(where / "sim"), "build", "planner"]
    assert (where / ".dev").exists()
    log.unlink()
    (bin_dir / "missing").unlink()
    done = launch(["firmware", "list"], bin_dir, tmp_path, where / "sim")
    assert done.returncode == 0, done.stderr
    assert calls(log, "run")[0][-4] == dev and not calls(log, "build")
    # Without .dev, the image again.
    (where / ".dev").unlink()
    log.unlink()
    done = launch(["firmware", "list"], bin_dir, tmp_path, where / "sim")
    assert done.returncode == 0, done.stderr
    assert calls(log, "run")[0][-4] == runtime


def test_old_images_are_removed_and_no_others(tmp_path):
    where = checkout(tmp_path)
    bin_dir, log = standin(tmp_path, "podman")
    runtime, dev = image_names(where)
    old = ["ghcr.io/sim-mesh/sim-mesh:0123456789ab", "localhost/sim-mesh-dev:0123456789ab",
           "localhost/sim-mesh:2117203344", "sim-mesh:win-abcdef012345"]
    kept = [runtime, "localhost/" + dev, "docker.io/library/ubuntu:24.04", "<none>:<none>",
            "ghcr.io/someone/sim-mesh-other:1"]
    (bin_dir / "images").write_text("\n".join(old + kept) + "\n")
    done = launch(["firmware", "list"], bin_dir, tmp_path, where / "sim", SIM_MESH_ENGINE="podman")
    assert done.returncode == 0, done.stderr
    assert sorted(call[1] for call in calls(log, "rmi")) == sorted(old)
    assert "removed 4 old sim-mesh images" in done.stderr
    assert not [call for call in calls(log, "image") + calls(log, "system")
                if "prune" in call]


def test_after_install_it_runs_here_unless_told_otherwise(tmp_path):
    where = checkout(tmp_path)
    bin_dir, log = standin(tmp_path, "docker")
    (where / ".native").touch()
    done = launch(["build", "nothing"], bin_dir, tmp_path, where / "sim")
    assert done.returncode != 0 and "no component 'nothing'" in done.stderr
    assert not log.exists()
    done = launch(["build", "nothing"], bin_dir, tmp_path, where / "sim", SIM_MESH_NATIVE="0")
    assert calls(log, "run")
    (where / ".native").unlink()
    log.unlink()
    done = launch(["build", "nothing"], bin_dir, tmp_path, where / "sim", SIM_MESH_NATIVE="1")
    assert done.returncode != 0 and "no component 'nothing'" in done.stderr
    assert not log.exists()


def test_natively_dev_wants_its_tools_first(tmp_path):
    where = checkout(tmp_path)
    bin_dir, log = standin(tmp_path, "docker")
    (where / ".native").touch()
    done = launch(["dev"], bin_dir, tmp_path, where / "sim")
    assert done.returncode != 0
    assert "`sim dev` needs cmake, cargo, Node 22 here: run `sim install --dev` first" \
        in done.stderr
    done = launch(["build", "radio"], bin_dir, tmp_path, where / "sim")
    assert done.returncode != 0
    assert "`sim build` needs cmake here: run `sim install --dev` first" in done.stderr
