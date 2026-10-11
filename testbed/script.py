"""Scripts: plain Python against the sim-mesh library, run from top to end.

    testbed/scripts/<name>.py

    '''What this script is for, in its first line.'''
    from sim_mesh import *

    firmware = script_input("firmware", type=Firmware, category="reticulum",
                            label="Firmware for nodes not otherwise configured")
    sim_speed("real")
    nodes().firmware(firmware)
    nodes(tag="tcp-peer").on_first_boot("tcp peer add {addr:internet}:4965")

    nodes().up()
    nodes(tag="lora").reticulum.lxmf.announce(spread=300)

A script says what its simulation runs and what is done to it, in order
(sim_mesh.library is the whole of it): declarations first, then whatever it
does, the first of which starts its simulation. It is run by the runner
(sim_mesh.runner), a process of its own, never inside simd: what a station
is given at its first boot travels to simd as data, `.on_first_boot()`'s
rules, not as the script's code. A script may define `report(run_dir)`,
which returns the run's report as Markdown once the script has run to its
end.

A simulation started by a script keeps a copy of it in its run, and a
snapshot keeps that copy.

Listing a script reads it without running it: its docstring, whether it has
a report, its inputs (each `script_input(…)` at its top level, its arguments
literals and its type one of INPUT_TYPES), and the files it imports or
includes (`references`), from its syntax tree, and theirs in turn.

Two files beside the scripts are everyone's:

    testbed/scripts/globals.py        settings every script shares; the names
                                      in SHARED are also read by the page and
                                      the analysis, without running anything
    testbed/nodesets/<name>.py        a nodeset's own setup, which a script
                                      includes for each nodeset of its world
"""

import ast
import importlib.util
import os
import sys

import store

REPORT = "report"
INPUTS = "inputs"
INPUT_CALL = "script_input"
INCLUDE_CALL = "script_include"
# script_input's types as a script writes them, and as the page knows them.
INPUT_TYPES = {"int": "int", "float": "float", "str": "str", "bool": "bool",
               "Firmware": "firmware", "Run": "run"}
INPUT_ARGS = ("name", "type", "label", "default", "category")
DEFAULT_TEXT = '''"""A new script."""
from sim_mesh import *

firmware = script_input("firmware", type=Firmware,
                        label="Firmware for nodes not otherwise configured")
sim_speed("real")
nodes().firmware(firmware)
script_include("scripts/startup.py")

nodes().up()
'''
GLOBALS = "globals"
# The scripts sim-mesh ships: never written over, kept changed under a name of
# one's own (Save as).
EXAMPLES = ("lxmf-traffic", "msg-traffic", "meshtastic-check", "realtime", "startup")
# The names of globals.py read outside a script, and what each must be: the
# radio every station without the `no-radio` tag is set to, which the page's
# coverage and links, the loss tables' band and the analysis take as given.
SHARED = (("FREQ_MHZ", float), ("SF", int), ("BW_KHZ", float), ("CR", int))
NODESET_SETUP_TEXT = '''"""{name}'s own setup: what only this nodeset's nodes need."""
from sim_mesh import *
'''


def script_path(name):
    return os.path.join(store.SCRIPTS_DIR, store.check_name(name, "script") + ".py")


def globals_path(directory=None):
    """globals.py: the store's, or a run's own copy in `directory`."""
    return os.path.join(directory or store.SCRIPTS_DIR, GLOBALS + ".py")


def shared(path=None):
    """globals.py's SHARED names, read without running it: {name: value}.
    Each must be assigned a literal at the file's top level."""
    path = path or globals_path()
    try:
        with open(path, encoding="utf-8") as handle:
            tree = parse(handle.read(), os.path.basename(path))
    except OSError as err:
        raise store.StoreError("%s: %s" % (path, err)) from err
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    out = {}
    for name, kind in SHARED:
        if name not in found:
            raise store.StoreError("%s: %s is not set to a number at its top level" % (path, name))
        try:
            out[name] = kind(found[name])
        except (TypeError, ValueError) as err:
            raise store.StoreError("%s: %s is a number, not %r" % (path, name, found[name])) from err
    return out


def shared_radio(path=None):
    """The radio globals.py sets every station to, in the intent's words."""
    got = shared(path)
    return {"freq_mhz": got["FREQ_MHZ"], "sf": got["SF"], "bw_khz": got["BW_KHZ"],
            "cr": got["CR"]}


def nodeset_setup_path(name):
    return os.path.join(store.NODESETS_DIR, store.check_name(name, "nodeset") + ".py")


def read_nodeset_setup(name):
    """A nodeset's setup script: (text, whether it has one yet). One it does
    not have yet reads as a fresh one's text."""
    path = nodeset_setup_path(name)
    if not os.path.isfile(path):
        return NODESET_SETUP_TEXT.format(name=name), False
    with open(path, encoding="utf-8") as handle:
        return handle.read(), True


def write_nodeset_setup(name, text):
    """Check a nodeset's setup parses, then put it in place."""
    parse(text, name + ".py")
    store.write_text(nodeset_setup_path(name), text)


def names():
    """Every script on disk, by name."""
    return store.listing(store.SCRIPTS_DIR, ".py")


def parse(text, where):
    """A script's syntax tree, or StoreError saying where it does not parse."""
    try:
        return ast.parse(text, filename=where)
    except SyntaxError as err:
        raise store.StoreError("%s, line %s: %s" % (where, err.lineno, err.msg)) from err


def has_report(tree):
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == REPORT
               for node in tree.body)


def _input_call(node):
    """The `script_input(…)` call a top-level statement is, or None."""
    value = node.value if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
            and value.func.id == INPUT_CALL:
        return value
    return None


def declared_inputs(tree):
    """A script's inputs, read from its top-level `script_input(…)` calls:
    [{name, type, label, category?, default?}], in order. One whose
    arguments are not literals, or whose type is not one of INPUT_TYPES, is
    refused, naming its line."""
    out = []
    for node in tree.body:
        call = _input_call(node)
        if call is None:
            continue
        given = dict(zip(INPUT_ARGS, call.args))
        given.update({k.arg: k.value for k in call.keywords if k.arg})
        if "name" not in given or set(given) - set(INPUT_ARGS):
            raise store.StoreError("line %d: script_input takes a name, and %s"
                                   % (node.lineno, ", ".join(INPUT_ARGS[1:])))
        kind = given.pop("type", None)
        kind = kind.id if isinstance(kind, ast.Name) else ("str" if kind is None else None)
        if kind not in INPUT_TYPES:
            raise store.StoreError("line %d: script_input's type is one of %s"
                                   % (node.lineno, ", ".join(INPUT_TYPES)))
        try:
            values = {k: ast.literal_eval(v) for k, v in given.items()}
        except ValueError as err:
            raise store.StoreError("line %d: script_input's arguments are literals"
                                   % node.lineno) from err
        row = {"name": str(values["name"]), "type": INPUT_TYPES[kind],
               "label": str(values.get("label") or values["name"])}
        for key in ("category", "default"):
            if values.get(key) is not None:
                row[key] = values[key]
        out.append(row)
    return out


def references(tree, seen=None):
    """The files a script imports or includes that are sim-mesh's own, and
    those they import or include in turn: other scripts of the store, and
    the sim-mesh library's modules, as [{name, path, library}], `name` as the
    script spells it. An include whose path is not written out (one per
    nodeset, say) is not followed: nothing but running it says which."""
    wanted = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            wanted += [(a.name, module_file(a.name)) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            wanted.append((node.module, module_file(node.module)))
            wanted += [("%s.%s" % (node.module, a.name), module_file("%s.%s" % (node.module, a.name)))
                       for a in node.names]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == INCLUDE_CALL and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            path = os.path.abspath(os.path.join(store.SIM_DIR, node.args[0].value))
            wanted.append((node.args[0].value, path if os.path.isfile(path) else None))
    seen = set() if seen is None else seen
    out = []
    scripts = os.path.abspath(store.SCRIPTS_DIR) + os.sep
    for name, path in wanted:
        if not path or path in seen:
            continue
        seen.add(path)
        library = not path.startswith(scripts)
        out.append({"name": name, "path": os.path.relpath(path, store.SIM_DIR), "library": library})
        if not library:
            with open(path, encoding="utf-8") as handle:
                try:
                    out += references(parse(handle.read(), os.path.basename(path)), seen)
                except store.StoreError:
                    continue
    return out


def module_file(name):
    """The file of a module a script imports, when it is one of sim-mesh's
    own (the library or another script), else None."""
    parts = name.split(".")
    if parts == ["sim_mesh"]:
        parts = ["sim_mesh", "library"]       # what `from sim_mesh import *` is
    if parts[0] == "sim_mesh":
        base = os.path.join(store.SIM_DIR, *parts)
    elif len(parts) == 1:
        base = os.path.join(os.path.abspath(store.SCRIPTS_DIR), parts[0])
    else:
        return None
    for path in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def describe(path, name=None):
    """What a listing says of a script: its name, the first line of its
    docstring, whether it has a report, and what it imports."""
    name = name or os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        tree = parse(text, os.path.basename(path))
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        return {"name": name, "doc": doc[0] if doc else "", REPORT: has_report(tree),
                INPUTS: declared_inputs(tree), "references": references(tree)}
    except store.StoreError as err:
        return {"name": name, "doc": "", REPORT: False, INPUTS: [], "references": [],
                "error": str(err)}


def listing():
    """Every script's `describe`, with `included_by`: the scripts that
    include or import it, and `example`: whether it is one of EXAMPLES. A
    script some other one includes (startup.py, globals.py) is a part of
    those, not a thing to run on its own."""
    rows = [describe(script_path(each), each) for each in names()]
    by_path = {os.path.relpath(script_path(row["name"]), store.SIM_DIR): row for row in rows}
    for row in rows:
        row["included_by"] = []
        row["example"] = row["name"] in EXAMPLES
    for row in rows:
        for ref in row["references"]:
            other = by_path.get(ref["path"])
            if other is not None and other is not row:
                other["included_by"].append(row["name"])
    return rows


def read(name):
    path = script_path(name)
    if not os.path.isfile(path):
        raise store.StoreError("no script called %r" % name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def read_reference(relpath):
    """A file a script refers to, by its path under testbed/: its text."""
    path = os.path.abspath(os.path.join(store.SIM_DIR, relpath))
    lib = os.path.join(os.path.abspath(store.SIM_DIR), "sim_mesh") + os.sep
    scripts = os.path.abspath(store.SCRIPTS_DIR) + os.sep
    if not path.endswith(".py") or not (path.startswith(lib) or path.startswith(scripts)):
        raise store.StoreError("%s is not the library's or a script" % relpath)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def shadows_module(name):
    """Whether a script called `name` would hide a module of the standard
    library or one installed: a script runs with scripts/ first on its path."""
    if not name.isidentifier():
        return False
    if name in sys.stdlib_module_names or name in sys.builtin_module_names:
        return True
    scripts = os.path.abspath(store.SCRIPTS_DIR)
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False
    origin = spec and spec.origin
    return spec is not None and not (origin and os.path.abspath(origin).startswith(scripts + os.sep))


def write(name, text, new=False):
    """Check a script parses, then put it in place. An example is never
    written over."""
    path = script_path(name)
    if not new and name in EXAMPLES:
        raise store.StoreError("%s is an example: Save as to keep the changes under a name of your own"
                               % name)
    if new and os.path.exists(path):
        raise store.StoreError("there is already a script called %r" % name)
    if new and shadows_module(name):
        raise store.StoreError("%r is a Python module's name, which a script of that name would hide"
                               % name)
    if not new and not os.path.isfile(path):
        raise store.StoreError("no script called %r" % name)
    parse(text, name + ".py")
    store.write_text(path, text)
    return describe(path, name)


def _module(path, name):
    for where in (store.SIM_DIR, os.path.abspath(store.SCRIPTS_DIR)):
        if where not in sys.path:
            sys.path.insert(0, where)
    spec = importlib.util.spec_from_file_location(
        "sim_mesh_script_%s" % name.replace("-", "_"), path)
    return spec, importlib.util.module_from_spec(spec)


def run_file(path, name=None):
    """Run a script, its top to its end, as a module of its own: the module."""
    name = name or os.path.splitext(os.path.basename(path))[0]
    spec, module = _module(path, name)
    spec.loader.exec_module(module)
    return module


def module_of(path, name=None):
    """A script's definitions without running it: its imports, its functions
    and classes and its plain assignments, what `report` needs, and none of
    what it does."""
    name = name or os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as handle:
        tree = parse(handle.read(), os.path.basename(path))
    kept = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
            ast.Assign, ast.AnnAssign)
    # Its inputs are asked for when it runs, not when its report is written.
    tree.body = [node for node in tree.body
                 if isinstance(node, kept) and _input_call(node) is None]
    _, module = _module(path, name)
    exec(compile(tree, path, "exec"), module.__dict__)   # noqa: S102 - the script's own definitions
    return module
