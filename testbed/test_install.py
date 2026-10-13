"""`sim install` puts on a Linux machine what sim-mesh's image holds, and
`sim install --dev` what the development image holds too: the same system
packages and the same Node major. The two are written separately, the
launcher's lists and the Dockerfiles' RUN lines, so this keeps them from
drifting apart."""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def launcher_list(name):
    """A bash array from the launcher, `NAME=(a b …)`, over several lines."""
    got = re.search(r"^%s=\(([^)]*)\)" % name, read("sim"), re.M)
    assert got, "no %s in sim" % name
    return got.group(1).split()


def dockerfile_apt(name):
    """Every package a Dockerfile's apt-get installs, nodejs aside (it comes
    from NodeSource, which the launcher adds when the system's is too old)."""
    packages = set()
    for block in re.findall(r"apt-get install -y --no-install-recommends(.*?);", read(name), re.S):
        packages |= {word for word in block.replace("\\", " ").split() if not word.startswith("-")}
    return packages - {"nodejs"}


def test_the_launcher_installs_every_package_the_image_has():
    runtime = set(launcher_list("APT_PACKAGES"))
    assert dockerfile_apt("Dockerfile") <= runtime, dockerfile_apt("Dockerfile") - runtime


def test_the_development_install_has_every_package_both_images_have():
    both = set(launcher_list("APT_PACKAGES")) | set(launcher_list("APT_DEV_PACKAGES"))
    images = dockerfile_apt("Dockerfile") | dockerfile_apt("Dockerfile.dev")
    assert images <= both, images - both
    # And a venv, where Fedora's later Node gets its plain names.
    assert "python3-venv" in launcher_list("APT_DEV_PACKAGES")


def test_the_image_holds_no_toolchain():
    # What builds sim-mesh is the development image's: the image is only what
    # runs it, as the firmware contract promises a station.
    for tool in ("build-essential", "cmake", "git", "curl", "ruby-dev", "python3-pytest"):
        assert tool not in dockerfile_apt("Dockerfile"), tool
        assert tool not in launcher_list("APT_PACKAGES"), tool
    assert "nodesource" not in read("Dockerfile") and "rustup" not in read("Dockerfile")


def test_the_launcher_takes_node_from_where_the_development_image_does():
    major = re.search(r"deb\.nodesource\.com/node_(\d+)\.x", read("Dockerfile.dev")).group(1)
    assert re.search(r"^NODE_MAJOR=%s\b" % major, read("sim"), re.M)


def test_a_fedora_install_names_the_same_things():
    # Its own names, one for each of the images' (build-essential is gcc, g++
    # and make; gnupg is only for NodeSource's apt key), in the same list.
    renamed = {"python3-yaml": ["python3-pyyaml"],
               "build-essential": ["gcc", "gcc-c++", "make"],
               "pkg-config": ["pkgconf-pkg-config"],
               "libstdc++6": ["libstdc++"], "procps": ["procps-ng"],
               "ruby-dev": ["ruby-devel"], "ruby-bundler": ["rubygem-bundler"],
               "zlib1g-dev": ["zlib-devel"]}
    same = ("ca-certificates", "curl", "git", "python3", "python3-aiohttp", "python3-pytest",
            "cmake")
    for apt_list, dnf_list in (("APT_PACKAGES", "DNF_PACKAGES"),
                               ("APT_DEV_PACKAGES", "DNF_DEV_PACKAGES")):
        apt, dnf = set(launcher_list(apt_list)), set(launcher_list(dnf_list))
        for apt_name in apt:
            if apt_name in renamed:
                assert set(renamed[apt_name]) <= dnf, apt_name
            elif apt_name in same:
                assert apt_name in dnf, apt_name
            else:
                assert apt_name in ("gnupg", "python3-venv"), apt_name
    images = dockerfile_apt("Dockerfile") | dockerfile_apt("Dockerfile.dev")
    assert set(renamed) <= images
