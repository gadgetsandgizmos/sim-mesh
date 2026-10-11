"""Runs and snapshots: one simulation's output, and a moment of one kept.

A **run** is a directory, `testbed/runs/<name>/`:

    run.yaml            the geodata, nodeset and script names, time mode,
                        the firmware rules its scripts declared, each node's
                        firmware and where each firmware's build was, when
                        it started, and the snapshot it started from, if any
    geodata.yaml        the geodata file as it was, its pack path re-based
    nodeset.yaml        the run's own copy of the nodeset: edits during the run
                        go here, never to `nodesets/`
    script.py           the script it was started with, as it was, when one was
    globals.py          scripts/globals.py as it was: the radio the run's
                        coverage, tables and analysis take its stations to have
    losses/<band>.bin   the loss tables the ether reads, the run's own copies
    nodes/<name>/state/ each station's store, as it writes it
    nodes/<name>/...    each station's log, and whatever else it keeps
    nodeset-edits.jsonl every nodeset edit made during the run, with its T
    record, logs        the ether's record and log, simd's own
    report.md           the report a script's `report` wrote, when it has one
    paused/             the run as it ended, in a snapshot's layout, when it
                        was paused rather than stopped

A **paused** run is one whose stations were stopped with their state kept:
`run.yaml` says `paused: {simulation, t, at, by?}`, and `paused/` holds what
a snapshot would. `by: script` is a pause its script asked for, which is the
script done; the page and `sim list` call that one **done** and any other
paused. Resuming either loads that into a new run directory, as a snapshot
is loaded, and the old run's `run.yaml` says `resumed: <new run>`.

A **snapshot** is a directory, `testbed/snapshots/<name>/`:

    snapshot.yaml       taken at which T, from which run, each node's firmware
                        and its rules, which builds
    geodata.yaml        the geodata reference, as it was
    nodeset.yaml        the nodeset, as the run had it
    script.py           the run's script, when it had one
    globals.py          the run's globals.py
    losses/<band>.bin   the run's table copies
    nodes/<node>/state/

Loading a snapshot brings the nodeset, the script and the tables back exactly
as they were, into a new run directory, without recomputing anything and
without the planner. A snapshot keeps its script because setup runs only on
a station with no state: a factory reset after the load sets a station up as
the first run did. Logs and the record are an account of one run and are
never copied into a snapshot.
"""

import datetime
import json
import os
import shutil

import yaml

import geodata as geodata_module
import nodeset as nodeset_module
import store

RUN_FILE = "run.yaml"
SNAPSHOT_FILE = "snapshot.yaml"
GEODATA_FILE = "geodata.yaml"
NODESET_FILE = "nodeset.yaml"
SCRIPT_FILE = "script.py"
GLOBALS_FILE = "globals.py"
LOSSES_DIR = "losses"
NODES_DIR = "nodes"
EDITS_FILE = "nodeset-edits.jsonl"
REPORT_FILE = "report.md"
RECORD_FILE = "record.tsv"
PAUSED = "paused"


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def iso_epoch(text):
    """An ISO time as wall seconds, or None."""
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def run_path(name, runs_dir=None):
    return os.path.join(runs_dir or store.RUNS_DIR, store.check_name(name, "run"))


def snapshot_path(name, snapshots_dir=None):
    return os.path.join(snapshots_dir or store.SNAPSHOTS_DIR, store.check_name(name, "snapshot"))


def runs(runs_dir=None):
    """Every run on disk, by name."""
    base = runs_dir or store.RUNS_DIR
    if not os.path.isdir(base):
        return []
    return sorted(e for e in os.listdir(base) if os.path.isfile(os.path.join(base, e, RUN_FILE)))


def snapshots(snapshots_dir=None):
    """Every snapshot on disk, by name."""
    base = snapshots_dir or store.SNAPSHOTS_DIR
    if not os.path.isdir(base):
        return []
    return sorted(e for e in os.listdir(base)
                  if os.path.isfile(os.path.join(base, e, SNAPSHOT_FILE)))


def _read_yaml(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return store.load_yaml(handle) or {}
    except (OSError, yaml.YAMLError) as err:
        raise store.StoreError("%s: %s" % (path, err)) from err


def _write_yaml(path, data):
    store.write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))


# ---- state, and moving it about ------------------------------------------

def copy_state(src_root, dst_root, wanted=None):
    """Every station's state store from one tree to another, and nothing else.

    A run directory also holds logs and the ether's record; those are an
    account of one run rather than part of a snapshot, so they are left where
    they fall on the way out and left alone on the way in. A station the
    source has nothing for is a station this copy is clearing.
    """
    src_nodes = os.path.join(src_root, NODES_DIR)
    dst_nodes = os.path.join(dst_root, NODES_DIR)
    have = set(os.listdir(src_nodes)) if os.path.isdir(src_nodes) else set()
    keep = have if wanted is None else (have & set(wanted))
    for name in sorted(keep):
        state = os.path.join(src_nodes, name, "state")
        if not os.path.isdir(state):
            continue
        target = os.path.join(dst_nodes, name, "state")
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copytree(state, target)
    if os.path.isdir(dst_nodes):
        for name in os.listdir(dst_nodes):
            if name not in keep:
                shutil.rmtree(os.path.join(dst_nodes, name, "state"), ignore_errors=True)


def wipe_state(root, name=None):
    """Throw away one station's state store, or every one of them.

    This is the whole of a factory reset: the station comes back to a
    directory with nothing in it, which is the same station a node newly
    placed on the map is, and it is set up again for the same reason.
    """
    nodes = os.path.join(root, NODES_DIR)
    if not os.path.isdir(nodes):
        return
    for entry in ([name] if name else os.listdir(nodes)):
        shutil.rmtree(os.path.join(nodes, entry, "state"), ignore_errors=True)


def _copy_tables(src_root, dst_root):
    src = os.path.join(src_root, LOSSES_DIR)
    dst = os.path.join(dst_root, LOSSES_DIR)
    os.makedirs(dst, exist_ok=True)
    if os.path.isdir(src):
        for entry in sorted(os.listdir(src)):
            if entry.endswith(".bin"):
                shutil.copyfile(os.path.join(src, entry), os.path.join(dst, entry))


def _copy_script(src_root, dst_root):
    for name in (SCRIPT_FILE, GLOBALS_FILE):
        src = os.path.join(src_root, name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(dst_root, name))


# ---- a run ---------------------------------------------------------------

class Run:
    """One run directory: what it was started from, and its files."""

    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        self.meta = _read_yaml(os.path.join(self.dir, RUN_FILE))

    @property
    def name(self):
        return os.path.basename(self.dir)

    @property
    def geodata_name(self):
        return self.meta.get("geodata")

    @property
    def nodeset_path(self):
        return os.path.join(self.dir, NODESET_FILE)

    @property
    def geodata_path(self):
        return os.path.join(self.dir, GEODATA_FILE)

    @property
    def script_path(self):
        """The run's copy of its script, or None when it was started without one."""
        path = os.path.join(self.dir, SCRIPT_FILE)
        return path if os.path.isfile(path) else None

    def geodata(self):
        """The geodata, from the run's own copy. A pack reads its manifest,
        so it needs the pack; nothing else about a run does."""
        return geodata_module.read(self.geodata_path, self.geodata_name)

    def nodeset(self):
        """The run's own nodeset, which edits during the run change and save."""
        return nodeset_module.open_path(self.nodeset_path, self.meta.get("nodeset"))

    def radio(self):
        """The radio the run's globals.py sets its stations to (script.shared_radio)."""
        import script as script_module
        return script_module.shared_radio(script_module.globals_path(self.dir))

    def node_count(self):
        """How many stations the run has, from its nodeset file's node list
        alone: a listing reads any run's, whatever else that file says."""
        return len((_read_yaml(self.nodeset_path).get("nodes") or {}))

    def table_path(self, band):
        return os.path.join(self.dir, LOSSES_DIR, "%s.bin" % band)

    def bands(self):
        """The bands the run has a table for."""
        base = os.path.join(self.dir, LOSSES_DIR)
        if not os.path.isdir(base):
            return []
        return sorted(e[:-4] for e in os.listdir(base) if e.endswith(".bin"))

    def node_dir(self, name):
        return os.path.join(self.dir, NODES_DIR, name)

    def set(self, **fields):
        """Record more about the run in run.yaml (build stamps once known)."""
        self.meta.update(fields)
        _write_yaml(os.path.join(self.dir, RUN_FILE), self.meta)

    def log_edit(self, t, what, **fields):
        """One nodeset edit made during the run, at simulated time `t`, as a
        JSON line: a move is `log_edit(t, "move", node=..., lat=..., lon=...)`."""
        line = json.dumps({"t": t, "what": what, **fields}, sort_keys=False)
        with open(os.path.join(self.dir, EDITS_FILE), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def log_move(self, t, name, lat, lon, height_m=None):
        fields = {"node": name, "lat": lat, "lon": lon}
        if height_m is not None:
            fields["height_m"] = height_m
        self.log_edit(t, "move", **fields)

    def edits(self):
        path = os.path.join(self.dir, EDITS_FILE)
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    @property
    def report_path(self):
        return os.path.join(self.dir, REPORT_FILE)

    def has_report(self):
        return os.path.isfile(self.report_path)

    @property
    def paused(self):
        """The pause this run waits in, {simulation, t, at}, or None: set,
        and not resumed."""
        paused = self.meta.get(PAUSED)
        if not isinstance(paused, dict) or self.meta.get("resumed"):
            return None
        if not os.path.isfile(os.path.join(self.dir, PAUSED, SNAPSHOT_FILE)):
            return None
        return paused

    def last_t(self):
        """The run's T when it stopped, in microseconds, from the last line
        of the ether's record (every line carries its message's `t`); None
        when the record has none. Only the file's tail is read."""
        path = os.path.join(self.dir, RECORD_FILE)
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8192))
                tail = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for line in reversed(tail[1:] if len(tail) > 1 else tail):
            fields = line.split("\t", 3)
            if line.startswith("#") or len(fields) != 4:
                continue
            try:
                t = json.loads(fields[3]).get("t")
            except ValueError:
                continue
            if isinstance(t, (int, float)):
                return int(t)
        return None

    def ended_at(self):
        """When the run stopped, wall seconds: its pause, else the last
        write to simd's log."""
        at = (self.meta.get(PAUSED) or {}).get("at")
        if at:
            return iso_epoch(at)
        try:
            return os.stat(os.path.join(self.dir, "simd.log")).st_mtime
        except OSError:
            return None

    def started_at(self):
        return iso_epoch(self.meta.get("started"))

    def as_dict(self):
        return {"name": self.name, "dir": self.dir, **self.meta}


def create_run(directory, gd, ns, script_name, time_mode, tables, builds=None, snapshot=None):
    """Lay out a new run directory, factory fresh.

    `gd` and `ns` are loaded geodata and a nodeset; `script_name` names the
    script whose copy the run keeps, or is None; `tables` maps a band to a
    table file (usually the cache's), copied in as the run's own. No station
    has state: a run from geodata and a nodeset is a network that has not
    happened yet.
    """
    directory = os.path.abspath(directory)
    if os.path.exists(os.path.join(directory, RUN_FILE)):
        raise store.StoreError("%s already holds a run" % directory)
    os.makedirs(os.path.join(directory, LOSSES_DIR), exist_ok=True)
    wipe_state(directory)
    geodata_module.write_copy(gd, os.path.join(directory, GEODATA_FILE))
    nodeset_module.write(os.path.join(directory, NODESET_FILE), ns.data)
    import script as script_module
    if script_name:
        shutil.copyfile(script_module.script_path(script_name),
                        os.path.join(directory, SCRIPT_FILE))
    shutil.copyfile(script_module.globals_path(), os.path.join(directory, GLOBALS_FILE))
    for band, path in (tables or {}).items():
        shutil.copyfile(path, os.path.join(directory, LOSSES_DIR, "%s.bin" % band))
    meta = {"geodata": gd.name, "nodeset": ns.name, "script": script_name, "time": time_mode,
            "builds": dict(builds or {}), "started": now_iso(), "snapshot": snapshot}
    _write_yaml(os.path.join(directory, RUN_FILE), meta)
    return Run(directory)


def open_run(directory):
    if not os.path.isfile(os.path.join(directory, RUN_FILE)):
        raise store.StoreError("%s is not a run" % directory)
    return Run(directory)


# ---- snapshots -----------------------------------------------------------

def save_snapshot(run, name, t, builds=None, snapshots_dir=None):
    """Keep the moment: the run's geodata, nodeset, script, tables and every
    station's state, at simulated time `t`. Returns the directory."""
    target = snapshot_path(name, snapshots_dir)
    if os.path.exists(target):
        raise store.StoreError("there is already a snapshot called %r" % name)
    os.makedirs(target)
    try:
        with open(run.geodata_path, encoding="utf-8") as handle:
            text = handle.read()
        store.write_text(os.path.join(target, GEODATA_FILE),
                         geodata_module.rebase_text(text, run.dir, target))
        ns = run.nodeset()
        nodeset_module.write(os.path.join(target, NODESET_FILE), ns.data)
        _copy_script(run.dir, target)
        _copy_tables(run.dir, target)
        copy_state(run.dir, target, wanted=ns.nodes)
        _write_yaml(os.path.join(target, SNAPSHOT_FILE), {
            "t": t, "run": run.name, "geodata": run.geodata_name,
            "nodeset": run.meta.get("nodeset"), "script": run.meta.get("script"),
            "builds": dict(builds if builds is not None else run.meta.get("builds") or {}),
            "firmware": dict(run.meta.get("firmware") or {}),
            "firmware_rules": list(run.meta.get("firmware_rules") or []),
            "first_boot_rules": list(run.meta.get("first_boot_rules") or []),
            "taken": now_iso()})
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def load_snapshot(name, directory, time_mode, snapshots_dir=None):
    """Put a snapshot into a new run directory: its geodata, nodeset, script,
    tables and state, nothing recomputed and no planner asked."""
    source = snapshot_path(name, snapshots_dir)
    if not os.path.isfile(os.path.join(source, SNAPSHOT_FILE)):
        raise store.StoreError("no snapshot called %r" % name)
    directory = os.path.abspath(directory)
    if os.path.exists(os.path.join(directory, RUN_FILE)):
        raise store.StoreError("%s already holds a run" % directory)
    snap = _read_yaml(os.path.join(source, SNAPSHOT_FILE))
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(source, GEODATA_FILE), encoding="utf-8") as handle:
        store.write_text(os.path.join(directory, GEODATA_FILE),
                         geodata_module.rebase_text(handle.read(), source, directory))
    ns = nodeset_module.open_path(os.path.join(source, NODESET_FILE), snap.get("nodeset"))
    nodeset_module.write(os.path.join(directory, NODESET_FILE), ns.data)
    _copy_script(source, directory)
    _copy_tables(source, directory)
    wipe_state(directory)
    copy_state(source, directory, wanted=ns.nodes)
    _write_yaml(os.path.join(directory, RUN_FILE), {
        "geodata": snap.get("geodata"), "nodeset": snap.get("nodeset"),
        "script": snap.get("script"), "time": time_mode,
        "builds": dict(snap.get("builds") or {}), "firmware": dict(snap.get("firmware") or {}),
        "firmware_rules": list(snap.get("firmware_rules") or []),
        "first_boot_rules": list(snap.get("first_boot_rules") or []), "started": now_iso(),
        "snapshot": name, "snapshot_t": snap.get("t")})
    return Run(directory)


# ---- pausing -------------------------------------------------------------

def pause_run(run, simulation, t, by=None):
    """Keep a stopped run as it ended, to be resumed: its state and the rest
    of a snapshot into `paused/`, and the pause in `run.yaml`. `by` is
    `script` when its script paused it, which is the script done; a pause a
    person asked for has none."""
    shutil.rmtree(os.path.join(run.dir, PAUSED), ignore_errors=True)
    save_snapshot(run, PAUSED, t, snapshots_dir=run.dir)
    paused = {"simulation": simulation, "t": t, "at": now_iso()}
    if by:
        paused["by"] = str(by)
    run.set(paused=paused)


def stop_paused(run):
    """A paused run stopped for good: the state it was paused with gone, so
    it can no longer be resumed and holds no firmware, and the run ended
    where it paused (its pause's T and time stay in `run.yaml`)."""
    shutil.rmtree(os.path.join(run.dir, PAUSED), ignore_errors=True)
    run.set(stopped=now_iso())


def paused_runs(runs_dir=None):
    """Every run waiting in a pause, oldest first."""
    base = runs_dir or store.RUNS_DIR
    found = []
    for name in runs(base):
        run = Run(os.path.join(base, name))
        if run.paused is not None:
            found.append(run)
    found.sort(key=lambda r: str(r.paused.get("at")))
    return found


def resume_run(old, directory, time_mode):
    """A paused run's `paused/` loaded into a new run directory, as a
    snapshot is; the old run then says where it went on."""
    run = load_snapshot(PAUSED, directory, time_mode, snapshots_dir=old.dir)
    run.set(snapshot=None, snapshot_t=None, resumed_from=old.name,
            resumed_t=(old.paused or {}).get("t"))
    old.set(resumed=run.name)
    return run


def delete_run(run):
    """A run directory gone, with everything in it: its logs, record, report
    and any pause."""
    shutil.rmtree(run.dir)


def delete_snapshot(name, snapshots_dir=None):
    """A snapshot gone, with everything in it. A run started from it holds
    its own copy, so nothing else needs it."""
    path = snapshot_path(name, snapshots_dir)
    if not os.path.isfile(os.path.join(path, SNAPSHOT_FILE)):
        raise store.StoreError("no snapshot called %r" % name)
    shutil.rmtree(path)


def snapshot_info(name, snapshots_dir=None):
    """A snapshot's snapshot.yaml, for a listing."""
    return _read_yaml(os.path.join(snapshot_path(name, snapshots_dir), SNAPSHOT_FILE))
