"""Messages between nodes, whatever mesh they run.

A script says "a message from this node to that one", or "announce", once,
and each node does it in its own protocol, with its category's verb:

    category    a node is known by   announce         what it knows   route to one   a message
    reticulum   its LXMF identity    lxmf.announce    path            path           lxmf.send
    meshcore    its contact name     floodadv         contacts        path           msg
    meshtastic  its long name        nodeinfo         nodes           nodes          sendtext

From a script, on a selection (sim_mesh.library):

    node("fred").message("sylvia", "hello")     # {node: the message's id}
    nodes(tag="repeater").announce()

Every message is a direct one, to one node, which acknowledges or proves it
where its protocol does; what became of it is the sender's driver's
`lxmf.message.status` or `msg.status`, and its arrival the recipient's
`lxmf.message.received` or `msg.received` (sim_mesh.reticulum.delivery
counts both). `PROTOCOLS` holds a `Protocol` per category, which the traffic
driver (sim_mesh.traffic) uses for its warm-up and its sends.
"""

import re


class Protocol:
    """What one category's verbs are for messaging. Each verb is given as
    (verb, args), the driver's verb and what it is given."""

    category = None
    title = None            # what a traffic report calls a run of it
    status_event = None     # the sender's word on a message (`status: delivered` is a proof)
    known = None            # what `knowing()` lists, by name

    async def destinations(self, sim, names):
        """{name: what a message to it is sent to} for the stations that have
        one, and what each one said when asked, for a report."""
        return {n: n for n in names}, {}

    def announce(self):
        """Making a node known to the others."""
        raise NotImplementedError

    def knowing(self):
        """What a node knows of the others, a list to count."""
        raise NotImplementedError

    def route(self, to):
        """What a node holds as its route to `to`, read just before a send;
        `hops` reads its answer."""
        raise NotImplementedError

    def hops(self, answer, to):
        """Route hops to `to` from what `route` answered, or None."""
        return None

    def send(self, to, text):
        """A direct message to the node `to`: answered with its id."""
        raise NotImplementedError


class Reticulum(Protocol):
    """LXMF over Reticulum: an identity each, announced; a message to a node
    is to its identity's delivery destination."""

    category = "reticulum"
    title = "LXMF traffic"
    status_event = "lxmf.message.status"
    known = "paths"

    async def destinations(self, sim, names):
        """Each station's LXMF delivery address, the identity it sends from.
        A station is up before its first-boot lines have all landed, so its
        identity may not exist yet: asked until every station has one."""
        dests, answers = {}, {}
        for attempt in range(60):
            got = await sim.ask({"type": "meta", "verb": "lxmf.identities", "args": {},
                                 "category": self.category},
                                [n for n in names if n not in dests],
                                after=2.0 if attempt else 0.0)
            for name, held in got.items():
                answers[name] = held
                addr = held[0][1] if isinstance(held, list) and held else None
                if isinstance(addr, str) and re.fullmatch(r"[0-9a-f]{32}", addr):
                    dests[name] = addr
            if len(dests) >= len(names):
                break
        return dests, answers

    def announce(self):
        return "lxmf.announce", {}

    def knowing(self):
        return "path", {}

    def route(self, to):
        return "path", {"to": str(to)}

    def hops(self, answer, to):
        return answer[0].get("hops") if isinstance(answer, list) and answer else None

    def send(self, to, text):
        return "lxmf.send", {"to": str(to), "text": str(text)}


class Meshcore(Protocol):
    """MeshCore: a node is its contact name once it has advertised; a direct
    message waits for its acknowledgement."""

    category = "meshcore"
    title = "MeshCore traffic"
    status_event = "msg.status"
    known = "contacts"

    def announce(self):
        return "floodadv", {}

    def knowing(self):
        return "contacts", {}

    def route(self, to):
        return "path", {"to": str(to)}

    def hops(self, answer, to):
        # Its hops' hash prefixes, [] for a neighbour; None is flood.
        return len(answer) if isinstance(answer, list) else None

    def send(self, to, text):
        return "msg", {"to": str(to), "text": str(text)}


class Meshtastic(Protocol):
    """Meshtastic: a node is its long name; NodeInfo makes it known; a direct
    message asks for an acknowledgement."""

    category = "meshtastic"
    title = "Meshtastic traffic"
    status_event = "msg.status"
    known = "nodes"

    def announce(self):
        return "nodeinfo", {}

    def knowing(self):
        return "nodes", {}

    def route(self, to):
        return "nodes", {}

    def hops(self, answer, to):
        # The node list's hops away for the recipient.
        for row in answer if isinstance(answer, list) else ():
            if isinstance(row, list) and row and row[0] == to:
                return row[2]
        return None

    def send(self, to, text):
        return "sendtext", {"text": str(text), "to": str(to)}


PROTOCOLS = {p.category: p for p in (Reticulum, Meshcore, Meshtastic)}


def protocol(category):
    """The category's Protocol; a ValueError for one with no messaging."""
    if category not in PROTOCOLS:
        raise ValueError("no messaging for category %r: %s" % (category, ", ".join(PROTOCOLS)))
    return PROTOCOLS[category]()
