# sim-mesh's image, which everything runs in, on every OS, in Docker or Podman:
# `sim` builds it on first use and runs itself inside it, with the directory
# holding sim-mesh mounted at its own path; nothing of sim-mesh is copied in or
# built here.
#
# It is the system a firmware may count on (the firmware contract), the same on
# every machine: Ubuntu 24.04's C library and C++ runtime and its CPython 3.12;
# everything else a firmware brings in its zip. A firmware runs only on the
# architecture it was built for: the image is pulled for the host's own.
#
# Holds:
#   - python3 (3.12) with aiohttp and pyyaml (the front, simd, the ether,
#     firmware.py, every firmware's driver, and a firmware written in Python)
#     and pytest
#   - Node 22 and npm (the page's Quasar/Vite build; Vite needs a newer Node
#     than Ubuntu ships)
#   - gcc, g++, make and cmake (the radio libraries and the time shim)
#   - cargo via rustup, with the wasm32 target (the planner, the ether's
#     conductor)
#   - Ruby and bundler (`sim dev`'s Jekyll preview of the site, sim-mesh.net)
#   - the station runtime the contract promises: libc, libstdc++
# By its full name: podman will not guess a registry for a short one without a
# terminal to ask on, and Docker reads it the same.
FROM docker.io/library/ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# A UTF-8 locale for everything run here, not only what `sim` starts: without
# one Ruby reads sources as US-ASCII, and Sass refuses the site theme's
# stylesheet. C.UTF-8 is built into the C library; nothing to install.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
        python3 python3-aiohttp python3-yaml python3-pytest \
        build-essential cmake pkg-config \
        libstdc++6 \
        procps \
        ruby-dev ruby-bundler zlib1g-dev; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

# Rust in a shared location, writable by whatever uid `sim` runs the
# container as (the host user's, so what it builds in the mounted tree is
# theirs), so cargo can fetch crates at build time.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN set -eux; \
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal; \
    rustup target add wasm32-unknown-unknown; \
    chmod -R a+rwX "$RUSTUP_HOME" "$CARGO_HOME"

# The home `sim` mounts a named volume on, for npm's and cargo's caches
# between runs; any uid may write it.
RUN mkdir -p /home/sim-mesh && chmod 1777 /home/sim-mesh

EXPOSE 8800/tcp 8800/udp
