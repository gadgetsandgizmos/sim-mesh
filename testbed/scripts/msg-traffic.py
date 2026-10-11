"""Message traffic, any protocol: warm-up, an hour of direct messages between the nodes that are not repeaters, a drain.

For comparing mesh protocols on one nodeset. The nodes tagged `repeater`
run the repeater firmware chosen above the script and only forward; every
other node runs the firmware for the rest, and each message goes from one
of those to another, both drawn from the seed. One seed sends the same
messages between the same pairs at the same instants whatever the protocol,
so two runs differ only in what carries them.

Both firmware must be of one category, `reticulum`, `meshcore` or
`meshtastic`: the driver (sim_mesh.traffic) takes the protocol from what
the nodes run, and stops the simulation if they run a mix. For MeshCore,
the repeaters' firmware is a repeater build and the others' a companion;
for Reticulum and Meshtastic the two may be one firmware, the repeaters
told to forward by the startup script (transport, router). Under Reticulum
the repeaters need no LXMF of their own (only the others are given an
identity, announce and send), so a firmware that only forwards can be the
repeaters'.

Texts are 20-55 characters, short enough for every protocol to carry in one
packet (MeshCore at most 160 bytes of text, Meshtastic about 200, an LXMF
opportunistic packet about 290), and Reticulous is told to send such a
message as one packet rather than over a Link, and to retry it so.
Warm-up announces the senders (an LXMF announce, a flood advert, a NodeInfo)
until what they know of each other stops growing. The result lands in the
run directory as `traffic.json`, the simulation is paused, and the report
is the delivery each sender's driver reported, counted from the run's
events.
"""
from sim_mesh import *
from sim_mesh import traffic

firmware = script_input("firmware", type=Firmware,
                        label="Firmware for the nodes that are not repeaters")
repeater_firmware = script_input("repeater_firmware", type=Firmware,
                                 label="Firmware for the repeaters")
reticulous_settings = script_input(
    "reticulous_settings", type=str, default="none",
    label="Reticulous settings for every node, before its radio starts: "
          "lines separated by ; (none for none)")

OPTIONS = {
    "category": "auto",
    "skip_tag": "repeater",
    "sizes": {"short": (20, 55, 1)},
    "warm_rounds": 3,
    "traffic": 3600.0,
    "every": 5.0,
    "seed": 17,
    "drain": 600.0,
}

sim_speed("max")
nodes().firmware(firmware)
nodes(tag="repeater").firmware(repeater_firmware)
# Said in Reticulous's own language, so only to its own bases.
RETICULOUS = nodes(base="reticulous-dev-sx1262") | nodes(base="reticulous-stable-sx1262")
# Settings a radio reads when it starts (s.lora.0.community_radius) come
# before the startup script starts it.
if reticulous_settings.strip() not in ("", "none"):
    RETICULOUS.on_first_boot(*[line.strip() for line in reticulous_settings.split(";")
                               if line.strip()])
script_include("scripts/startup.py")
# A Reticulum sender and recipient each need an identity, which the
# station's name gives it; nothing to the other categories.
(~nodes(tag="repeater")).on_first_boot(Node.reticulum.lxmf.create())
# Reticulous sends a message that fits one packet as that packet, a Link only
# for one that does not (its default is a Link always), and retries it as the
# packet again rather than over a Link.
RETICULOUS.on_first_boot("set s.lxmf.default_method link-if-big",
                         "set s.lxmf.small_retry opportunistic")

traffic.run(OPTIONS)
sim_pause()


def report(run_dir):
    return traffic.report(run_dir)
