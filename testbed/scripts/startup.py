"""Every world's start: roles, radios, each nodeset's own setup, then the radios up.

Included by a script (`script_include("scripts/startup.py")`) among its
declarations: all of this is said to a station at its first boot, in the
order written, by its firmware's driver. A script's own first-boot
rules after the include come after the radio is up.
"""
from sim_mesh import *
from globals import FREQ_MHZ, SF, BW_KHZ, CR, SYNC

RADIOS = ~nodes(tag="no-radio")

# A node tagged with a role is told it; nothing to a firmware of another category.
# A node tagged repeater forwards whatever it runs: Reticulum's transport,
# Meshtastic's router; MeshCore's repeater is a firmware of its own.
nodes(tag="transport").on_first_boot(Node.reticulum.role("transport"))
nodes(tag="router").on_first_boot(Node.meshtastic.role("router"))
REPEATERS = nodes(tag="repeater")
REPEATERS.on_first_boot(Node.reticulum.role("transport"), Node.meshtastic.role("router"))

# Every node's radio, bar those tagged no-radio, at the node's own maximum power.
# Reticulum's sync word is said only to Reticulum nodes: other firmware has
# its own, fixed.
RETICULUM = nodes(category="reticulum")
(RADIOS & RETICULUM).on_first_boot(
    Node.radio(freq_mhz=FREQ_MHZ, sf=SF, bw_khz=BW_KHZ, cr=CR, sync=SYNC, tx_dbm="max"))
(RADIOS - RETICULUM).on_first_boot(
    Node.radio(freq_mhz=FREQ_MHZ, sf=SF, bw_khz=BW_KHZ, cr=CR, tx_dbm="max"))

# What only one nodeset's nodes need, from its own nodesets/<name>.py.
for nodeset in sim_nodesets():
    script_include("nodesets/%s.py" % nodeset, missing_ok=True)

# Last, because a radio reads its settings, SUPE and the rest when it starts.
RADIOS.on_first_boot(Node.radio_up())
