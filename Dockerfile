# sim-mesh's image, which everything runs in, on every OS, in Docker or Podman:
# `sim` pulls it on first use (or builds it from this file when it cannot be
# pulled) and runs itself inside it, with the directory holding sim-mesh
# mounted at its own path; nothing of sim-mesh is copied in or built here.
#
# It is the system a firmware may count on (the firmware contract), the same on
# every machine, and nothing more: Ubuntu 24.04's C library and C++ runtime and
# its CPython 3.12; everything else a firmware brings in its zip. A firmware
# runs only on the architecture it was built for: the image is pulled for the
# host's own.
#
# Holds:
#   - python3 (3.12) with aiohttp and pyyaml (the front, simd, the ether,
#     firmware.py, every firmware's driver, and a firmware written in Python)
#   - the station runtime the contract promises: libc, libstdc++
#   - ca-certificates, for HTTPS: the prebuilt outputs, the indexes, firmware
#   - procps (ps)
# What sim-mesh compiles (the radio libraries and the time shim, the ether's
# conductor, the planner, the page) is not built here: `sim` fetches it
# prebuilt for the sources in the tree. The toolchains that build it are in
# the development image (Dockerfile.dev), made from this one.
# By its full name: podman will not guess a registry for a short one without a
# terminal to ask on, and Docker reads it the same.
FROM docker.io/library/ubuntu:24.04
LABEL org.opencontainers.image.source=https://github.com/sim-mesh/sim-mesh

ENV DEBIAN_FRONTEND=noninteractive
# A UTF-8 locale for everything run here, not only what `sim` starts.
# C.UTF-8 is built into the C library; nothing to install.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 python3-aiohttp python3-yaml \
        libstdc++6 \
        procps; \
    rm -rf /var/lib/apt/lists/*

# The home `sim` mounts a named volume on, for caches between runs; any uid
# may write it.
RUN mkdir -p /home/sim-mesh && chmod 1777 /home/sim-mesh

EXPOSE 8800/tcp 8800/udp
