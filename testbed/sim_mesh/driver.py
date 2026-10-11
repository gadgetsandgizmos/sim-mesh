"""The driver: what sim-mesh knows of one firmware, shipped inside its zip.

```
firmware zip ── node.yaml driver: driver.py ──► sim-mesh imports it, takes DRIVER
sim-mesh ── DRIVER(firmware) ──► one driver per installed firmware a run uses
sim-mesh ── env/argv/wait_up/configured/flush ──► driver        start and keep a station
sim-mesh ── <category's verbs> ──► driver ──► the station           what a script means
station console ── each line ──► driver.console_line ──► station.report(event, …)
```

A firmware is an executable sim-mesh starts once per node, and a Python
module beside it, its **driver**, which sim-mesh imports. The module defines
`DRIVER`, a subclass of its category's driver class (`ReticulumDriver` in
`sim_mesh.reticulum.driver` for category `reticulum`), which itself
subclasses `Driver` here. How the driver gets a station to do something —
lines typed at its console, framed RPC, a settings file and a restart, a
tool run against it — is its own business.

A driver imports from sim-mesh only this module and its category's driver
module. Everything sim-mesh hands it is named below and in those two
modules; anything else in sim-mesh may change under it.

**What a driver is given.** `self.firmware` is the installed firmware:

    dir       the firmware's directory (its zip, unpacked)
    exec      the executable's path
    fixed     the read-only data tree's path, or None
    env       node.yaml's `env`, `./` paths made absolute
    firmware  its name, <base>_<arch>_<version>
    base, arch, version, category, radio, title, hardware

and every call names a `station`, which offers:

    name, node_id         the node's name and its id in the ether
    dir                   the station's directory (its cwd), state under `state/`
    addr                  its loopback address, where its sockets bind
    ether_addr            host:port of the ether
    board                 its board, JSON (front end, max_dbm), or None
    log_path              the file its console output is appended to
    status                stopped, starting, setup, up, restarting
    starts                how many times its process has been started
    virtual               True in a virtual-time run
    rpc                   framed RPC on its console (rpc_query, below), or None
                          before its process has started
    await restart()       its process stopped, for the supervisor to start again
    report(event, **f)    an event for the run's record (events.jsonl), at the
                          run's T: what a category's analysis counts

**Callbacks.** `console_line(station, line)` is called with every line the
station prints, in order, as sim-mesh reads it; a driver that tells sim-mesh
what happened (a message delivered) does it from there with `report`.

**Messages.** A category whose messages carry sim-mesh's id in their text
(`meshcore`, `meshtastic`) reports them with `msg_status(station, mid,
status, why=None)`, the event `msg.status` (`STATUSES`), and
`msg_received(station, mid, text, sender=None, chan=None)`, the event
`msg.received`; `tagged(text, mid)` puts the id in a text and `untagged`
reads it back out.

**Errors.** Anything that could not be done raises `CommandError`, whose
text is shown as it is.
"""

import asyncio
import contextlib
import os
import re

import boards as _boards
import rpc as _rpc


class CommandError(Exception):
    """A station could not be asked, did not answer in time, or its firmware
    has no way to do what was meant."""


RpcError = _rpc.RpcError
PROBE = _rpc.PROBE
QUERY_TIMEOUT_S = _rpc.QUERY_TIMEOUT_S
EXEC_BOUND_S = _rpc.EXEC_BOUND_S
UP = "up"

STATUSES = ("sent", "delivered", "failed")
STATUS_EVENT = "msg.status"
RECEIVED_EVENT = "msg.received"
MID_MARK = " #"
UNTAG = re.compile(r"^(.*?) #([A-Za-z0-9_.-]+)\s*$", re.S)


def tagged(text, mid):
    """A message's text with sim-mesh's id at its end."""
    return "%s%s%s" % (text, MID_MARK, mid)


def untagged(text):
    """(text, mid) out of a tagged text; (text, None) when it carries none."""
    found = UNTAG.match(text or "")
    if not found:
        return text, None
    return found.group(1), found.group(2)


def parse_setting(reply, key):
    """The value a `<key> = <value>` line in `reply` gives `key`, or None."""
    return _rpc.parse_setting(reply, key)


def chip_dbm(board, connector_dbm):
    """The chip's own power that puts `connector_dbm` at the antenna
    connector through `board`'s front end, when it has one."""
    return _boards.chip_dbm(board, connector_dbm)


async def run_tool(argv, timeout, env=None):
    """A helper program run to completion: (exit code, all it printed).
    `env` adds to sim-mesh's environment."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=dict(os.environ, **env) if env else None)
    except OSError as err:
        raise CommandError("%s: %s" % (argv[0], err)) from err
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as err:
        # It may have ended in the same instant the wait did: there is then
        # nothing to kill, and it is still a tool that gave no answer in time.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise CommandError("%s gave no answer in %.0fs" % (argv[0], timeout)) from err
    return proc.returncode, out.decode("utf-8", "replace")


class Driver:
    """One firmware, as sim-mesh drives it. A category's driver class adds
    its own verbs to these, every firmware's; a firmware's DRIVER implements
    them.

        name(station, name)             the node's name
        radio(station, freq_mhz=, sf=, bw_khz=, cr=, tx_dbm=, sync=, preamble=)
                                        LoRa settings, only those given; tx_dbm
                                        is at the antenna connector (chip_dbm
                                        converts for a board's front end)
        radio_up(station)               the radio started, for a firmware whose
                                        radio needs it; nothing otherwise
        tx_power(station, dbm)          transmit power at the connector
        diagnostics(station)            {label: text}: what a run keeps of it at
                                        its end; {} by default

    A verb a firmware cannot do raises `CommandError` (`self.cannot(verb)`
    makes one), which the defaults here do where nothing is a sound answer.
    """

    category = None
    # The verbs a script may ask of a station of this category, each a method
    # taking (station, **args), named as `method_of` says.
    VERBS = ("name", "radio", "radio_up", "tx_power", "diagnostics")
    # True for a firmware whose station forgets its role when it restarts:
    # the role its first-boot rules gave it is said again at every boot.
    role_volatile = False
    # Whether its console must be a terminal: a firmware that sets the line
    # up, reads keys, or buffers its output unless it is on a tty. False
    # gives the station a pipe each way instead, which holds none of the
    # host's ptys; its output must then reach the pipe line by line.
    console_tty = True
    # Whether sim-mesh acts on what the station prints: framed-RPC replies,
    # the capability marker. Then what a station printed at an instant is read
    # before T moves on. False, for a firmware whose console carries log lines
    # alone, and its consoles are read as they come, off the barrier: nothing
    # waits on them, and holding T for them at every instant is much of what a
    # large run costs. `console_line` is called either way.
    console_acted_on = True

    def __init__(self, firmware):
        self.firmware = dict(firmware)

    # ---- starting a station ----------------------------------------------

    def argv(self, station):
        """The command line a station is started with."""
        return [self.firmware["exec"]]

    def env(self, station):
        """What this firmware adds to a station's environment, over the
        contract's own `SIM_MESH_*` (and node.yaml's `env`, already there)."""
        return {}

    def sids(self, station):
        """The ether ids of the station's processes that join the run: its
        own, and any it starts that join as stations without a radio."""
        return (station.node_id,)

    def console_sid(self, station):
        """The ether id of the process that reads the station's console."""
        return station.node_id

    def configured(self, station):
        """True when the station's directory shows it has been set up."""
        raise NotImplementedError

    async def wait_up(self, station, timeout):
        """True once the station answers; False when it has not in `timeout`
        seconds of the run's clock."""
        raise NotImplementedError

    async def flush(self, station):
        """Make what the station has been told durable, or apply it; best
        effort."""

    def web_port(self):
        """The port the station's web UI answers on, or None."""
        return None

    # ---- lines -----------------------------------------------------------

    async def run(self, station, line, timeout=None):
        """One line in the firmware's own language (a script's `exec`); what
        the station said back."""
        raise CommandError("%s takes no lines" % self.firmware.get("title", "this firmware"))

    # ---- every firmware's verbs ------------------------------------------

    async def name(self, station, name):
        raise self.cannot("name")

    async def radio(self, station, **figures):
        raise self.cannot("radio")

    async def radio_up(self, station):
        return None

    async def tx_power(self, station, dbm):
        raise self.cannot("tx_power")

    async def diagnostics(self, station):
        return {}

    # ---- callbacks -------------------------------------------------------

    def console_line(self, station, line):
        """One line the station printed."""

    # ---- helpers ---------------------------------------------------------

    async def pause(self, station, seconds):
        """Wait on the run's clock: T in a virtual-time run."""
        await station.sleep(seconds)

    async def joined(self, station, timeout):
        """True once the station has joined the ether, at the T of its hello,
        so what the driver does next it does at that T; False when it has not
        in `timeout` seconds. True at once in a real-time run."""
        if station.clock is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(station.clock.joined(station.node_id)),
                                   timeout)
        except asyncio.TimeoutError:
            return False
        return True

    @contextlib.contextmanager
    def tool_turn(self, station):
        """Around a tool run against the station's host door on the wall
        clock: in a virtual-time run T stands while the tool has the floor and
        runs while the station works on what it read, so each line is read and
        answered at a T the run decides."""
        end = station.clock.tool_session(station.node_id) if station.clock is not None else None
        try:
            yield
        finally:
            if end is not None:
                end()

    async def tool_settle(self, station, timeout=5.0):
        """Inside a tool turn, before the tool writes: until the station is
        idle at the turn's T. T stands while the tool has the floor, but a
        station due at that instant may still be at its work when the tool
        starts, and bytes landing then were taken at whichever of its looks
        the host ran first. Landing on an idle station they are taken at the
        floor's run, a T the run decides. Polled on the wall clock: nothing
        here moves T."""
        clock = station.clock
        if clock is None or not hasattr(clock, "station_idle"):
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not clock.station_idle(station.node_id) and loop.time() < deadline:
            await asyncio.sleep(0.0005)

    async def rpc_query(self, station, line, timeout=None):
        """One framed-RPC query on the station's console: what it answered."""
        if station.rpc is None:
            raise CommandError("%s is not running" % station.name)
        try:
            return await station.rpc.query(line, timeout=timeout)
        except RpcError as err:
            raise CommandError(str(err)) from err

    async def rpc_ready(self, station, timeout, marker_wait_s):
        """True once the station's console speaks framed RPC."""
        if station.rpc is None:
            return False
        return await station.rpc.wait_ready(timeout, marker_wait_s,
                                            lambda s: self.pause(station, s))

    async def do(self, station, verb, **args):
        """A verb of this category, by name: `lxmf.send` is the method
        `lxmf_send`."""
        if verb not in self.VERBS:
            raise CommandError("no such verb %r for %s firmware (there are %s)"
                               % (verb, self.category, ", ".join(self.VERBS)))
        return await getattr(self, method_of(verb))(station, **args)

    def cannot(self, verb):
        return CommandError("%s has no way to %s" % (self.firmware.get("title", "this firmware"),
                                                     verb))

    # ---- what became of a message ----------------------------------------

    def msg_status(self, station, mid, status, why=None):
        """Report a message's status under sim-mesh's id."""
        fields = {"mid": mid, "status": status}
        if why:
            fields["why"] = why
        station.report(STATUS_EVENT, **fields)

    def msg_received(self, station, mid, text, sender=None, chan=None):
        """Report a message the station received: from a node (as its
        category names one) or on a channel."""
        fields = {"mid": mid, "text": text}
        if sender is not None:
            fields["sender"] = sender
        if chan is not None:
            fields["chan"] = chan
        station.report(RECEIVED_EVENT, **fields)


def method_of(verb):
    """The driver method a verb is: its dots underscores (`lxmf.send`,
    `lxmf_send`). A verb's name is a string a script and the page say, and
    groups with dots; a method's is an identifier, which cannot."""
    return verb.replace(".", "_")
