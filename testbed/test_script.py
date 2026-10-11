"""Scripts, and what a station is told: the listing, inputs, the library's
declarations, selections, macros and first-boot rules."""

import os
import re
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import drivers  # noqa: E402
import nodeset  # noqa: E402
import script  # noqa: E402
import stations  # noqa: E402
import store  # noqa: E402
import stub_firmware  # noqa: E402
from sim_mesh import library  # noqa: E402


@pytest.fixture
def scripts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SCRIPTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def runtime(monkeypatch):
    """A fresh library runtime, with no simulation to reach."""
    fresh = library.Runtime()
    monkeypatch.setattr(library, "runtime", fresh)
    monkeypatch.delenv("SIM_MESH_SIM", raising=False)
    return fresh


def test_a_script_is_described_without_being_run(scripts_dir):
    (scripts_dir / "told.py").write_text(
        '"""Sets up, then reports.\n\nMore."""\nfrom sim_mesh import *\nfrom sim_mesh import traffic\n'
        'raise SystemExit("never run")\n'
        "def report(run_dir):\n    return '# done'\n")
    (scripts_dir / "broken.py").write_text("nodes().firmware(\n")
    assert script.names() == ["broken", "told"]
    got = script.describe(script.script_path("told"), "told")
    assert got["doc"] == "Sets up, then reports." and got["report"] is True
    assert [r["path"] for r in got["references"]] == ["sim_mesh/library.py", "sim_mesh/traffic.py"]
    assert all(r["library"] for r in got["references"])
    assert "line 1" in script.describe(script.script_path("broken"))["error"]
    assert script.read_reference("sim_mesh/traffic.py").startswith('"""The traffic driver')
    with pytest.raises(store.StoreError):
        script.read_reference("../front.py")


def test_a_script_is_written_only_when_it_parses(scripts_dir):
    script.write("new", script.DEFAULT_TEXT, new=True)
    with pytest.raises(store.StoreError, match="already"):
        script.write("new", script.DEFAULT_TEXT, new=True)
    with pytest.raises(store.StoreError, match="line"):
        script.write("new", "def (:\n")
    assert script.read("new") == script.DEFAULT_TEXT
    with pytest.raises(store.StoreError, match="no script"):
        script.write("absent", "x = 1\n")


def test_an_example_is_kept_only_under_another_name(scripts_dir):
    (scripts_dir / "realtime.py").write_text("x = 1\n")
    with pytest.raises(store.StoreError, match="example"):
        script.write("realtime", "x = 2\n")
    script.write("mine", "x = 2\n", new=True)
    rows = {row["name"]: row for row in script.listing()}
    assert rows["realtime"]["example"] and not rows["mine"]["example"]


def test_a_new_script_never_hides_a_module(scripts_dir):
    for name in ("json", "random", "pytest"):
        with pytest.raises(store.StoreError, match="module"):
            script.write(name, "x = 1\n", new=True)
    script.write("my-json", "x = 1\n", new=True)


def test_a_scripts_definitions_are_read_without_running_it(scripts_dir, runtime):
    (scripts_dir / "one.py").write_text(
        "from sim_mesh import *\nWHO = 'one'\nnodes().firmware('x_y_latest')\nnodes().up()\n"
        "def report(run_dir):\n    return WHO + run_dir\n")
    module = script.module_of(script.script_path("one"), "one")
    assert module.report("/r") == "one/r"
    assert runtime.firmware_rules == [] and runtime.sim is None


def test_declarations_are_collected_until_the_script_does_something(scripts_dir, runtime):
    (scripts_dir / "fw.py").write_text(
        "from sim_mesh import *\n"
        "sim_speed(10)\n"
        "nodes().firmware('alpha-sx1262_latest')\n"
        "nodes(tag='beta').firmware('beta-sx1262_aarch64_1.2.0')\n"
        "nodes(base='alpha-sx1262').on_first_boot('''\n"
        "    lora 0 txp {max_dbm}\n"
        "    # a comment\n"
        "    lxmf create {name}\n"
        "''', Node.reticulum.role('transport'))\n")
    script.run_file(script.script_path("fw"), "fw")
    assert runtime.speed == "10x"
    assert runtime.firmware_rules == [
        {"which": {"all": True}, "firmware": "alpha-sx1262_latest"},
        {"which": {"where": {"tag": "beta"}}, "firmware": "beta-sx1262_aarch64_1.2.0"}]
    assert runtime.first_boot_rules == [
        {"which": {"where": {"base": "alpha-sx1262"}},
         "lines": ["lora 0 txp {max_dbm}", "lxmf create {name}",
                   {"verb": "role", "args": {"role": "transport"}, "category": "reticulum"}]}]
    # Doing something needs a simulation, which this runtime has no way to.
    with pytest.raises(library.ScriptError, match="no simulation"):
        library.nodes().up()
    assert library.speed_of("max") == "max" and library.speed_of("0.5") == "0.5x"
    with pytest.raises(library.ScriptError):
        library.speed_of("fast")


def test_a_new_simulation_is_started_empty_and_given_its_rules_once_driven(runtime,
                                                                           monkeypatch):
    """A script's new simulation runs nothing, so T stands, until the script's
    driver has the floor: it is started with no rules, and its firmware and
    first-boot rules then come in their order, as to a simulation attached to.
    Started with them, its stations ran for as long as attaching took on the
    host, and the script began at a T the host decided."""
    import asyncio
    from sim_mesh import sim as sim_module

    calls = []

    class Driven:
        async def firmware(self, rules):
            calls.append(("firmware", rules))

        async def first_boot(self, rules):
            calls.append(("first_boot", rules))

    async def start(geodata, nodesets, script=None, time="real", name=None, build=None,
                    firmware_rules=None, first_boot_rules=None, port=None, session=None,
                    inputs=None, build_tag=None):
        calls.append(("start", firmware_rules, first_boot_rules))
        return Driven()
    monkeypatch.setattr(sim_module, "start", start)
    runtime.configure(geodata="g", nodesets=["n"])
    runtime.firmware_rules = [{"which": {"all": True}, "firmware": "ours"}]
    runtime.first_boot_rules = [{"which": {"all": True}, "lines": ["hello"]}]
    asyncio.run(runtime._begin())
    assert calls == [("start", None, None),
                     ("firmware", runtime.firmware_rules),
                     ("first_boot", runtime.first_boot_rules)]


def test_the_repositorys_scripts_all_parse_and_declare_what_they_run(runtime, monkeypatch,
                                                                    tmp_path):
    monkeypatch.syspath_prepend(store.SCRIPTS_DIR)
    # script_include() reads under the testbed: the repository's scripts,
    # beside a nodeset `gw` whose own setup adds a TCP peer.
    shutil.copytree(store.SCRIPTS_DIR, str(tmp_path / "scripts"))
    (tmp_path / "nodesets").mkdir()
    (tmp_path / "nodesets" / "gw.py").write_text(
        "from sim_mesh import *\n"
        "nodes(tag=\"tcp-peer\").on_first_boot(\"tcp peer add {addr:internet}:4965\")\n")
    monkeypatch.setattr(library, "TESTBED_DIR", str(tmp_path))
    names = script.names()
    helpers = {os.path.splitext(os.path.basename(ref["path"]))[0]
               for name in names for ref in script.describe(script.script_path(name))["references"]
               if not ref["library"]}
    assert {"startup", "globals"} <= helpers
    for name in names:
        info = script.describe(script.script_path(name), name)
        assert "error" not in info, info
        if name in helpers:
            continue                    # included or imported by the others
        fresh = library.Runtime()
        fresh.configure(geodata="berlin-city", nodesets=["gw"])
        # Every input a value, as the page or --set gives them: its default,
        # else a firmware's name.
        fresh.given = {row["name"]: row.get("default") or "some_latest" for row in info["inputs"]}
        library.runtime = fresh
        path = script.script_path(name)
        # Only the declarations: everything before the first thing done.
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        head = re.split(r"\n(?:traffic\.run\(|(?:\w+ = )?nodes\(\)\.up\()", text)[0]
        exec(compile(head, path, "exec"), {"__name__": "check_" + name})   # noqa: S102
        assert fresh.firmware_rules, "%s says no firmware" % name
        assert fresh.speed in ("real", "max"), name
        # The startup script's rules came with it, in order: roles, radios,
        # gw's own, the radios up, and the script's own after.
        said = [line["verb"] if isinstance(line, dict) else line
                for rule in fresh.first_boot_rules for line in rule["lines"]]
        order = ["role", "radio", "tcp peer add {addr:internet}:4965", "radio_up"]
        seen = [s for s in said if s in order]
        assert [s for i, s in enumerate(seen) if i == 0 or seen[i - 1] != s] == order, name


def test_the_startup_script_sets_every_radio_but_the_no_radio_ones(runtime, monkeypatch):
    """Reticulum's sync word goes to Reticulum nodes alone."""
    monkeypatch.syspath_prepend(store.SCRIPTS_DIR)
    runtime.configure(geodata="berlin-city", nodesets=["fachhochschule"])
    library.script_include("scripts/startup.py")   # fachhochschule has no setup of its own
    shared = script.shared()
    radio = [rule for rule in runtime.first_boot_rules
             if any(isinstance(l, dict) and l["verb"] == "radio" for l in rule["lines"])]
    figures = {"freq_mhz": shared["FREQ_MHZ"], "sf": shared["SF"], "bw_khz": shared["BW_KHZ"],
               "cr": shared["CR"], "tx_dbm": "max"}
    radios = {"not": {"where": {"tag": "no-radio"}}}
    reticulum = {"where": {"category": "reticulum"}}
    assert radio == [
        {"which": {"and": [radios, reticulum]},
         "lines": [{"verb": "radio", "args": dict(figures, sync=0x12)}]},
        {"which": {"and": [radios, {"not": reticulum}]},
         "lines": [{"verb": "radio", "args": figures}]}]
    with pytest.raises(library.ScriptError, match="no file"):
        library.script_include("nodesets/nowhere.py")
    library.script_include("nodesets/nowhere.py", missing_ok=True)


def test_globals_shared_names_are_read_without_running_it(scripts_dir):
    (scripts_dir / "globals.py").write_text("FREQ_MHZ = 433.92\nSF = 9\nBW_KHZ = 250\nCR = 5\n"
                                            "import os\nOTHER = os.getcwd()\n")
    assert script.shared_radio() == {"freq_mhz": 433.92, "sf": 9, "bw_khz": 250.0, "cr": 5}
    (scripts_dir / "globals.py").write_text("FREQ_MHZ = 433.92\nSF = 9\n")
    with pytest.raises(store.StoreError, match="BW_KHZ"):
        script.shared()


def test_macros_fill_name_id_and_addresses_and_leave_the_rest(monkeypatch):
    monkeypatch.setattr(stations, "NET", "127.16.0.0/22")
    ids = {"gw": 1, "leaf": 2}
    assert drivers.expand("hostname {name}", "gw", 1) == "hostname gw"
    assert drivers.expand("addr {addr} id {id} {unknown}", "gw", 1, ids) == \
        "addr 127.16.0.5 id 1 {unknown}"
    assert drivers.expand("tcp peer add {addr:leaf}:4965", "gw", 1, ids) == \
        "tcp peer add 127.16.0.6:4965"
    assert drivers.expand("x {addr:ghost} {name:leaf}", "gw", 1, ids) == \
        "x {addr:ghost} {name:leaf}"
    assert drivers.expand("lora 0 txp {max_dbm}", "gw", 1, ids, {"max_dbm": "27"}) == \
        "lora 0 txp 27"
    assert drivers.expand_all(["a {name}", "# c", "  ", "b"], "n", 3) == ["a n", "b"]


def test_commands_said_on_node_are_data_for_the_drivers_verbs():
    # What a script writes: data simd hands each station's driver as a verb,
    # a category's carrying it.
    Node = library.Node
    assert library.lines_of([Node.reticulum.role("transport"), "log rnsd debug",
                             [Node.radio(sf=9, tx_dbm="max"), Node.reticulum.lxmf.create()]]) == [
        {"verb": "role", "args": {"role": "transport"}, "category": "reticulum"},
        "log rnsd debug",
        {"verb": "radio", "args": {"sf": 9, "tx_dbm": "max"}},
        {"verb": "lxmf.create", "args": {}, "category": "reticulum"}]
    assert library.lines_of([Node.reticulum.lxmf.create("carol")]) == [
        {"verb": "lxmf.create", "args": {"name": "carol"}, "category": "reticulum"}]
    assert repr(Node.radio(sf=9)) == "Node.radio(sf=9)"
    with pytest.raises(library.ScriptError, match="first-boot rule is"):
        library.lines_of([3])
    assert set(drivers.verbs()) == {"name", "radio", "radio_up", "tx_power", "diagnostics"}
    assert {"role", "path", "lxmf.create", "lxmf.identities", "lxmf.announce",
            "lxmf.send"} <= set(drivers.verbs("reticulum"))
    assert set(drivers.verbs()) <= set(drivers.verbs("reticulum"))
    assert library.lines_of([Node.meshcore.repeat(True), Node.meshcore.floodadv()]) == [
        {"verb": "repeat", "args": {"on": True}, "category": "meshcore"},
        {"verb": "floodadv", "args": {}, "category": "meshcore"}]
    assert set(drivers.verbs()) | {"repeat", "advert", "floodadv", "contacts", "msg", "chan",
                                   "path", "reset_path"} == set(drivers.verbs("meshcore"))
    assert library.lines_of([Node.meshtastic.role("router"), Node.meshtastic.hop_limit(5),
                             Node.meshtastic.sendtext("hi"),
                             Node.meshtastic.sendtext("yo", to="b", want_ack=False),
                             Node.meshtastic.traceroute("c")]) == [
        {"verb": "role", "args": {"role": "router"}, "category": "meshtastic"},
        {"verb": "hop_limit", "args": {"n": 5}, "category": "meshtastic"},
        {"verb": "sendtext", "args": {"text": "hi", "ch_index": 0, "want_ack": True},
         "category": "meshtastic"},
        {"verb": "sendtext", "args": {"text": "yo", "to": "b", "ch_index": 0, "want_ack": False},
         "category": "meshtastic"},
        {"verb": "traceroute", "args": {"to": "c"}, "category": "meshtastic"}]
    assert set(drivers.verbs()) | {"role", "hop_limit", "sendtext", "traceroute", "nodes",
                                   "nodeinfo"} == set(drivers.verbs("meshtastic"))


def test_a_message_carries_its_id_in_its_text():
    from sim_mesh.driver import tagged, untagged
    from sim_mesh.meshcore import driver as meshcore
    from sim_mesh.meshtastic import driver as meshtastic
    assert tagged("hello there", "alpha.1200") == "hello there #alpha.1200"
    assert untagged("hello there #alpha.1200") == ("hello there", "alpha.1200")
    assert untagged("hello #2 there #bravo.5.1") == ("hello #2 there", "bravo.5.1")
    assert untagged("no id here") == ("no id here", None)
    assert meshcore.tagged is meshtastic.tagged is tagged
    assert meshcore.untagged is untagged


def test_an_unknown_category_is_named():
    with pytest.raises(drivers.CommandError, match="category 'nosuch'"):
        drivers.load({"firmware": "x", "category": "nosuch", "driver": "/nowhere.py"})


def test_a_meshtastic_firmware_loads(tmp_path):
    (tmp_path / "driver.py").write_text(stub_firmware.MESHTASTIC_DRIVER)
    driver = drivers.load({"firmware": "mt", "category": "meshtastic",
                           "driver": str(tmp_path / "driver.py")})
    assert driver.category == "meshtastic" and "sendtext" in driver.VERBS


def test_inputs_are_read_without_running_and_given_when_run(scripts_dir, runtime):
    (scripts_dir / "asks.py").write_text(
        '"""asks"""\n'
        "from sim_mesh import *\n"
        "firmware = script_input('firmware', type=Firmware, category='reticulum',\n"
        "                        label='Firmware for the rest')\n"
        "count = script_input('count', type=int, default=3)\n"
        "loud = script_input('loud', bool, 'Say it all', False)\n"
        "run = script_input('run', type=Run)\n"
        "nodes().firmware(firmware)\n"
        "def report(run_dir):\n"
        "    return 'r'\n")
    row = script.describe(script.script_path("asks"), "asks")
    assert row["inputs"] == [
        {"name": "firmware", "type": "firmware", "label": "Firmware for the rest",
         "category": "reticulum"},
        {"name": "count", "type": "int", "label": "count", "default": 3},
        {"name": "loud", "type": "bool", "label": "Say it all", "default": False},
        {"name": "run", "type": "run", "label": "run"}]
    # Its report is written without asking for its inputs.
    assert script.module_of(script.script_path("asks"), "asks").report("/x") == "r"
    with pytest.raises(library.ScriptError, match="input firmware .* has no value"):
        script.run_file(script.script_path("asks"), "asks")
    runtime.given.update({"firmware": "alpha-sx1262_latest", "loud": "yes", "run": "r7"})
    script.run_file(script.script_path("asks"), "asks2")
    assert runtime.firmware_rules[-1]["firmware"] == "alpha-sx1262_latest"
    assert runtime.inputs == {"firmware": "alpha-sx1262_latest", "count": 3, "loud": True,
                              "run": "r7"}
    assert isinstance(runtime.inputs["firmware"], library.Firmware)
    runtime.given["count"] = "many"
    with pytest.raises(library.ScriptError, match="input count is a int"):
        script.run_file(script.script_path("asks"), "asks3")
    (scripts_dir / "bad.py").write_text("from sim_mesh import *\nscript_input('x', type=list)\n")
    assert "type is one of" in script.describe(script.script_path("bad"), "bad")["error"]
    (scripts_dir / "worse.py").write_text("from sim_mesh import *\nscript_input('x', default=X)\n")
    assert "literals" in script.describe(script.script_path("worse"), "worse")["error"]


# ---- selections ------------------------------------------------------------------

FACTS = {
    "gw": {"id": 1, "tags": ["lora", "tcp-peer"], "role": "transport", "height_m": 30,
           "firmware": "alpha_latest", "base": "alpha", "category": "reticulum"},
    "n2": {"id": 2, "tags": ["lora"], "role": "client", "height_m": 2,
           "firmware": "alpha_latest", "base": "alpha", "category": "reticulum"},
    "sg": {"id": 3, "tags": ["beta"], "role": "client", "height_m": 2,
           "firmware": "beta_latest", "base": "beta", "category": "meshcore"},
}


def test_a_selection_is_set_algebra_over_the_nodes_facts():
    from sim_mesh import node, nodes
    from sim_mesh.select import Nodes, which

    assert nodes().pick(FACTS) == ["gw", "n2", "sg"]
    assert node("n2").pick(FACTS) == ["n2"] and repr(node("n2")) == "node('n2')"
    # Combined, a script's selection keeps what can be done to it.
    assert type(nodes(tag="lora") - node("gw")) is library.Nodes
    assert hasattr(~nodes(), "reticulum") and hasattr(nodes() & "gw", "radio")
    assert nodes(tag="lora").pick(FACTS) == ["gw", "n2"]
    assert nodes(tag="lora", role="transport").pick(FACTS) == ["gw"]
    assert nodes(tag=("tcp-peer", "beta")).pick(FACTS) == ["gw", "sg"]
    assert (nodes(tag="lora") | nodes(base="beta")).pick(FACTS) == ["gw", "n2", "sg"]
    assert (nodes(tag="lora") - nodes(role="client")).pick(FACTS) == ["gw"]
    assert (~nodes(category="reticulum")).pick(FACTS) == ["sg"]
    assert (nodes(tag="lora") ^ nodes(role="client")).pick(FACTS) == ["gw", "sg"]
    assert nodes(height_m=lambda h: h > 20).pick(FACTS) == ["gw"]
    assert which("all").pick(FACTS) == ["gw", "n2", "sg"]
    assert which("sg").pick(FACTS) == ["sg"] and which(["n2", "gw"]).pick(FACTS) == ["gw", "n2"]
    assert (nodes(tag="lora") & "n2").pick(FACTS) == ["n2"]
    tree = (nodes(tag="lora") - nodes(name="n2")).to_json()
    assert Nodes.from_json(tree).pick(FACTS) == ["gw"]
    with pytest.raises(ValueError, match="cannot travel"):
        nodes(height_m=lambda h: h > 20).to_json()
    with pytest.raises(ValueError):
        Nodes.from_json({"where": {"colour": "red"}})
    with pytest.raises(TypeError, match="no field"):
        nodes(colour="red")
    assert library.lines_of(["a", "b\nc"]) == ["a", "b", "c"]


def test_sim_run_says_the_page_for_what_a_script_lacks(scripts_dir, runtime, monkeypatch,
                                                       capsys):
    from sim_mesh import runner

    (scripts_dir / "asks.py").write_text(
        "from sim_mesh import *\n"
        "fw = script_input('firmware', type=Firmware)\n"
        "n = script_input('n', type=int, default=2)\n"
        "nodes().firmware(fw)\n")
    monkeypatch.setenv(runner.SAY_PAGES, "1")
    assert runner.main(["asks", "--geodata", "g", "--nodeset", "a", "--nodeset", "b"]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "page: http://localhost:8800/?script=asks&geodata=g&nodeset=a&nodeset=b"
    assert out[1] == "asks needs input firmware: choose it on the Scripts tab"
    # Given its input but no simulation: the page carries what was given.
    assert runner.main(["asks", "--set", "firmware=x_latest"]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "page: http://localhost:8800/?script=asks&set.firmware=x_latest"
    assert "needs a simulation" in out[1]
    # Without `sim run`, nothing is said of pages: what it lacks is an error.
    monkeypatch.delenv(runner.SAY_PAGES)
    runtime.given.clear()
    assert runner.main(["asks", "--sim", "s"]) == 2
    assert capsys.readouterr().out == "! asks needs input firmware\n"
