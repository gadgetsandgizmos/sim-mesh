"""The time shim, in a stand-in station, against a fake conductor.

standin.c links the chip library and runs a thread that sleeps in 25 ms steps,
one in 40 ms timed condition waits, and an interval timer at 10 ms; it is started with the shim preloaded, in a
virtual-time run with the thread census on. The conductor here grants T only
up to what the station last asked for, as the ether does, and every event the
station prints must land at its own instant in node time. retuner.c switches
channel as reticulum's SX1262 driver does, waiting BUSY out in 10 µs sleeps.
"""

import json
import os
from errno import ETIMEDOUT
import select
import shutil
import socket
import subprocess
import time

import pytest

from test_conductor import STOCK, XOSC
from test_model import BUILD, LIBRARY, RADIO, load_library

SHIM = os.path.join(BUILD, "libsimclock.so")
STANDIN = os.path.join(BUILD, "standin")
RETUNER = os.path.join(BUILD, "retuner")
EPOCH = 1_790_000_000_000_000


def build_standin(program=STANDIN):
    load_library()      # builds the library, and the shim with it, if they are missing
    if not os.path.exists(SHIM):
        subprocess.run(["cmake", "--build", BUILD], check=True, stdout=subprocess.DEVNULL)
    src = os.path.join(RADIO, "tests", os.path.basename(program) + ".c")
    if (not os.path.exists(program)
            or os.path.getmtime(program) < os.path.getmtime(src)
            or os.path.getmtime(program) < os.path.getmtime(LIBRARY)):
        # Linked as a firmware is: against the radio's shared library, which
        # the process finds at run time.
        cc = shutil.which("gcc") or pytest.fail("no gcc to build the stand-in")
        subprocess.run([cc, "-O1", "-I", os.path.join(RADIO, "include"), src,
                        "-L", BUILD, "-lsimradio-sx1262", "-Wl,-rpath," + BUILD,
                        "-lpthread", "-o", program], check=True)


class Station:
    """The stand-in, started with the shim; `args` follow the ether's address
    on its command line, `extra` adds to its environment, and a None there
    takes a variable out."""

    def __init__(self, port, *args, program=STANDIN, **extra):
        env = dict(os.environ, SIM_MESH_TIME="virtual", SIM_MESH_IDLE="threads",
                   SIM_MESH_EPOCH_US=str(EPOCH), LD_PRELOAD=SHIM)
        for key in ("SIM_MESH_SEED", "SIM_MESH_NODE_ID"):
            env.pop(key, None)
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self.proc = subprocess.Popen([program, "127.0.0.1:%d" % port, *args], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.lines = []
        self.buf = b""

    def pump(self, wait=0.0):
        fd = self.proc.stdout.fileno()
        while True:
            r, _, _ = select.select([fd], [], [], wait)
            if not r:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            self.buf += chunk
            wait = 0.05
        *whole, self.buf = self.buf.split(b"\n")
        self.lines += [line.decode().split() for line in whole if line]

    def close(self):
        self.proc.kill()
        self.proc.wait()


@pytest.fixture
def conductor():
    build_standin()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3.0)
    station = Station(sock.getsockname()[1])
    yield sock, station
    station.close()
    sock.close()


def test_the_shim_keeps_the_station_on_node_time(conductor):
    sock, station = conductor
    data, addr = sock.recvfrom(65535)
    assert json.loads(data)["type"] == "hello"
    # T at join is 0 here, so what the station read before the welcome
    # reached it and what it reads after agree.
    t, seq = 0, 1
    sock.sendto(json.dumps({"type": "welcome", "t": t, "mode": "virtual", "rate": None,
                            "epoch": EPOCH, "seq": seq}).encode(), addr)
    grants = 0
    untils = []
    started = time.monotonic()
    while t < 200_000:
        data, _ = sock.recvfrom(65535)
        msg = json.loads(data)
        if msg.get("type") != "idle" or msg.get("seq") != seq:
            continue
        assert msg["until"] is not None and msg["until"] > t
        untils.append(msg["until"])
        t = msg["until"]
        seq += 1
        grants += 1
        sock.sendto(json.dumps({"type": "run", "t": t, "seq": seq}).encode(), addr)
    took = time.monotonic() - started
    station.pump(0.2)

    clock = next(l for l in station.lines if l[0] == "clock")
    assert int(clock[1]) == 0                           # node time is T
    assert int(clock[2]) == EPOCH // 1_000_000          # time() is the epoch plus it

    # The sleeper's 25 ms and the timer's 10 ms, each at its own instant.
    sleeps = [int(l[1]) for l in station.lines if l[0] == "sleeper"]
    assert sleeps[:7] == [25_000, 50_000, 75_000, 100_000, 125_000, 150_000, 175_000]
    alarms = [l for l in station.lines if l[0] == "alarm"]
    assert len(alarms) >= 19
    # A timed wait ends at its deadline in node time, and says it timed out.
    waits = [(int(l[1]), int(l[2])) for l in station.lines if l[0] == "waiter"]
    assert waits[:4] == [(40_000, ETIMEDOUT), (80_000, ETIMEDOUT), (120_000, ETIMEDOUT),
                         (160_000, ETIMEDOUT)]
    # So does one on a condition that keeps the monotonic clock, and a timed
    # semaphore wait; a post wakes a sem_wait at the instant it is made.
    monos = [(int(l[1]), int(l[2])) for l in station.lines if l[0] == "monowaiter"]
    assert monos[:5] == [(30_000 * i, ETIMEDOUT) for i in range(1, 6)]
    sems = [(int(l[1]), int(l[2])) for l in station.lines if l[0] == "semwaiter"]
    assert sems[:5] == [(30_000 * i, ETIMEDOUT) for i in range(1, 6)]
    posts = [int(l[1]) for l in station.lines if l[0] == "posted"]
    assert posts[:1] == [120_000]
    # Every instant the station asked for is a tick, a sleep ending or a
    # 30 ms wait ending.
    ticks = {10_000 * i for i in range(1, 25)}
    thirties = {30_000 * i for i in range(1, 8)}
    assert set(untils) <= ticks | thirties | set(sleeps) | {s + 25_000 for s in sleeps}
    # Twenty grants of T went by without the busy watchdog: well under its
    # 20 ms of wall each.
    assert took < grants * 0.02


# ---- BUSY waited out, as reticulum's driver waits it ----------------------
#
# Under the BUSY a RAK board's SX1262 spent (test_conductor.py's STOCK and
# XOSC), a switch took the board 12.36 ms from SetStandby until it listened
# again on the stock path and 0.55 ms on the crystal's, 12.10 and 0.34 ms of it
# BUSY. The driver's nine commands of a stock switch keep BUSY 12 090 µs, the
# eight of the other 331 µs.

def retune(path, switches, board, **extra):
    """Run the retuner for `switches` switches on `path` with `board` as its
    SIM_MESH_BOARD, granting T as the ether does, and return each switch's time
    by the station's clock, from SetStandby until it saw BUSY low after SetRx,
    and by the chip's, from the SetStandby state to the SetRx state's
    `ready_at`."""
    build_standin(RETUNER)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3.0)
    station = Station(sock.getsockname()[1], path, str(switches), program=RETUNER,
                      SIM_MESH_BOARD=json.dumps(board), **extra)
    try:
        _, addr = sock.recvfrom(65535)
        t, seq = 0, 1
        sock.sendto(json.dumps({"type": "welcome", "t": t, "mode": "virtual", "rate": None,
                                "epoch": EPOCH, "seq": seq}).encode(), addr)
        chip, start, listening = [], None, False
        while True:
            msgs = [json.loads(line) for line in sock.recvfrom(65535)[0].split(b"\n") if line]
            for msg in msgs:
                if msg["type"] == "state" and msg["mode"].startswith("STDBY") and listening:
                    start, listening = msg["t"], False
                if msg["type"] == "state" and msg["mode"] == "RX":
                    if start is not None:
                        chip.append(msg["ready_at"] - start)
                    listening = True
            idles = [m for m in msgs if m["type"] == "idle" and m["seq"] == seq]
            if not idles:
                continue
            if idles[0]["until"] is None:
                break
            t, seq = idles[0]["until"], seq + 1
            sock.sendto(json.dumps({"type": "run", "t": t, "seq": seq}).encode(), addr)
        station.pump(0.2)
        assert ["done"] in station.lines
        switches = [line for line in station.lines if line[0] == "switch"]
        return [int(b) - int(a) for _, a, b in switches], chip
    finally:
        station.close()
        sock.close()


def figures(busy_us, **keys):
    return dict({"chip": "sx1262", "max_dbm": 22, "busy_us": busy_us, "busy_tcxo": 1}, **keys)


def test_without_short_waits_each_busy_wait_ends_on_the_millisecond():
    """As ever: every wait ends on a whole millisecond of node time, so each
    command whose BUSY the driver waits out costs it up to one more, and the
    radio listens again 18.14 ms after SetStandby on the stock path and 6.07
    ms after on the crystal's."""
    assert retune("stock", 3, figures(STOCK)) == ([19_000] * 3, [18_140] * 3)
    assert retune("xosc", 3, figures(XOSC)) == ([7_000] * 3, [6_065] * 3)


def test_with_short_waits_the_driver_sees_busy_fall_when_it_falls():
    """SIM_MESH_SHORT_WAITS=chip: each 10 µs poll ends as BUSY falls, so a switch
    costs its commands' BUSY, and the one poll that outlasts its command's
    (SetTxParams' 1 µs) its own 10: the board's 12.10 and 0.34 ms of BUSY."""
    station, chip = retune("stock", 3, figures(STOCK), SIM_MESH_SHORT_WAITS="chip")
    assert station == chip == [12_090 + 9] * 3
    station, chip = retune("xosc", 3, figures(XOSC), SIM_MESH_SHORT_WAITS="chip")
    assert station == chip == [331 + 9] * 3


def test_with_the_hosts_time_a_switch_takes_what_it_took_on_the_board():
    """And with the board's own 29 µs a transaction (`host_us`), a switch is
    within 0.1 ms of the board's: 12.351 ms against 12.36, 0.563 against 0.55."""
    station, chip = retune("stock", 3, figures(STOCK, host_us=29), SIM_MESH_SHORT_WAITS="chip")
    assert station == chip == [12_090 + 9 * 29] * 3
    assert abs(station[0] - 12_360) <= 100
    station, chip = retune("xosc", 3, figures(XOSC, host_us=29), SIM_MESH_SHORT_WAITS="chip")
    assert station == chip == [331 + 8 * 29] * 3
    assert abs(station[0] - 550) <= 100


def entropy_of(**extra):
    """The stand-in's `entropy` line, its first, from a station started with
    `extra`. A real-time stand-in prints without end after it, so only that
    line is read."""
    build_standin()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    station = Station(sock.getsockname()[1], **extra)
    try:
        r, _, _ = select.select([station.proc.stdout], [], [], 5.0)
        assert r, "the stand-in printed nothing"
        line = station.proc.stdout.readline().decode().split()
        assert line and line[0] == "entropy"
        return tuple(line[1:])
    finally:
        station.close()
        sock.close()


def test_a_seeded_virtual_run_gives_each_station_its_own_repeatable_bytes():
    a = entropy_of(SIM_MESH_SEED="17", SIM_MESH_NODE_ID="3")
    assert entropy_of(SIM_MESH_SEED="17", SIM_MESH_NODE_ID="3") == a
    assert len(a) == 3 and len(set(a)) == 3        # three calls, three draws
    assert entropy_of(SIM_MESH_SEED="17", SIM_MESH_NODE_ID="4") != a
    assert entropy_of(SIM_MESH_SEED="18", SIM_MESH_NODE_ID="3") != a


def test_without_a_seed_or_virtual_time_the_bytes_are_the_hosts():
    unseeded = entropy_of(SIM_MESH_NODE_ID="3")
    assert entropy_of(SIM_MESH_NODE_ID="3") != unseeded
    real = entropy_of(SIM_MESH_TIME=None, SIM_MESH_SEED="17", SIM_MESH_NODE_ID="3")
    assert entropy_of(SIM_MESH_TIME=None, SIM_MESH_SEED="17", SIM_MESH_NODE_ID="3") != real


def test_the_shim_counts_console_and_tcp_bytes_and_waits_to_write():
    """A console line read a byte at a time is reported once, as a running
    total; a TCP write to another address of the run is asked for, waited on,
    and leaves from the station's own address; what it reads back is
    reported; a socket it listens on is reported as its own."""
    build_standin()
    ether = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ether.bind(("127.0.0.1", 0))
    ether.settimeout(3.0)
    peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    peer.bind(("127.0.0.2", 0))
    peer.listen(1)
    host, port = peer.getsockname()
    station = Station(ether.getsockname()[1], "%s:%d" % (host, port),
                      SIM_MESH_NODE_ID="3", SIM_MESH_BIND_ADDR="127.0.0.3",
                      SIM_MESH_ETHER="127.0.0.1:%d" % ether.getsockname()[1])
    conn = None
    try:
        data, addr = ether.recvfrom(65535)
        assert json.loads(data)["type"] == "hello"
        ether.sendto(json.dumps({"type": "welcome", "t": 0, "mode": "virtual", "rate": None,
                                 "epoch": EPOCH, "seq": 1}).encode(), addr)
        peer.settimeout(3.0)
        conn, (from_host, _) = peer.accept()
        assert from_host == "127.0.0.3"         # its own address, not 127.0.0.1
        station.proc.stdin.write(b"ping\n")
        station.proc.stdin.flush()
        reports = []
        asked = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not asked:
            data, sender = ether.recvfrom(65535)
            msg = json.loads(data)
            if msg["type"] in ("read", "wrote", "listen"):
                reports.append(msg)
            if msg["type"] == "wrote" and "go" in msg:
                asked = msg
                # Nothing has been written until it may go.
                conn.settimeout(0.2)
                with pytest.raises(socket.timeout):
                    conn.recv(64)
                ether.sendto(json.dumps({"type": "go", "go": msg["go"]},
                                        separators=(",", ":")).encode(), sender)
        assert {"type": "read", "sid": 3, "ch": "tty", "total": 5} in reports
        assert asked["ch"].startswith("tcp/127.0.0.3:") and asked["ch"].endswith(">%s:%d" % (host, port))
        assert asked["n"] == 5
        conn.settimeout(3.0)
        assert conn.recv(64) == b"ping\n"
        conn.sendall(b"pong\n")
        while True:
            msg = json.loads(ether.recvfrom(65535)[0])
            if msg["type"] == "read" and msg["ch"].startswith("tcp/"):
                break
        a, b = asked["ch"][4:].split(">")
        assert msg == {"type": "read", "sid": 3, "ch": "tcp/%s>%s" % (b, a), "n": 5}
        station.pump(1.0)
        assert ["tcp", "pong"] in station.lines
        # Its listening socket was said to be its own.
        port = next(l[1] for l in station.lines if l[0] == "listening")
        assert {"type": "listen", "sid": 3, "at": "127.0.0.3:%s" % port} in reports
    finally:
        if conn is not None:
            conn.close()
        station.close()
        peer.close()
        ether.close()


# ---- A busy-wait on the clock, under strict time --------------------------
#
# With SIM_MESH_STRICT_TIME=1, T moves only when the station is idle, and a
# thread that waits for time by reading the clock in a loop never is. Its
# 100 000th read of one instant sleeps the shortest sleep there is, which ends
# on the next whole millisecond of node time, and while it goes on waiting so
# its 1 000th read of each next instant does: the loop sees T move a
# millisecond at a time, at a cost in reads that is the loop's own, never the
# host's pace.

SPINNER = os.path.join(BUILD, "spinner")


def spin(**extra):
    """Run the spinner, granting T as the ether does, and return the instants
    it asked for and its `done` line."""
    build_standin(SPINNER)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3.0)
    station = Station(sock.getsockname()[1], program=SPINNER, **extra)
    try:
        _, addr = sock.recvfrom(65535)
        t, seq, untils = 0, 1, []
        sock.sendto(json.dumps({"type": "welcome", "t": t, "mode": "virtual", "rate": None,
                                "epoch": EPOCH, "seq": seq}).encode(), addr)
        while True:
            msgs = [json.loads(line) for line in sock.recvfrom(65535)[0].split(b"\n") if line]
            idles = [m for m in msgs if m["type"] == "idle" and m["seq"] == seq]
            if not idles:
                continue
            if idles[0]["until"] is None:
                break
            t, seq = idles[0]["until"], seq + 1
            untils.append(t)
            sock.sendto(json.dumps({"type": "run", "t": t, "seq": seq}).encode(), addr)
        station.pump(0.2)
        return untils, [line for line in station.lines if line[0] == "done"]
    finally:
        station.close()
        sock.close()


def test_under_strict_time_a_busy_wait_on_the_clock_moves_t_by_the_millisecond():
    untils, done = spin(SIM_MESH_STRICT_TIME="1")
    assert untils == [1000 * i for i in range(1, 7)]
    # The read that slept from 5 ms returned 6 ms, the first past 5.3 ms: one
    # read at the welcome's instant, 100 000 to the first millisecond, then
    # 1 000 per millisecond.
    assert done == [["done", "6000", str(1 + 100_000 + 5 * 1_000)]]
    assert spin(SIM_MESH_STRICT_TIME="1") == (untils, done)
