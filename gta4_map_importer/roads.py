"""GTA IV vehicle road network (pc/data/cdimages/paths.img, nodes0..63.nod).

Each .nod file covers one 750 m cell of an 8x8 grid:
    header: u32 nodes, u32 vehicle nodes (stored first), u32 ped nodes, u32 links
    node (32 bytes): +0x08 u16 area, +0x0A u16 node id, +0x12 u16 first link,
                     +0x14 int16 x/8, y/8, z/64 (metres), +0x1E low 4 bits = link count
    link (8 bytes):  u16 target area, u16 target node, u8 ?, u8 lanes, u8, u8
                     lanes: bits 3-5 = lanes from this node to the target,
                            bits 0-2 = lanes from the target back to this node
(Direction checked against the game's distant-traffic data; heights against map geometry.)
"""

import os
import struct

from . import img


class RoadNetwork:
    """nodes: [(x, y, z)] metres; links: [[(target index, lanes out)]] per node."""

    def __init__(self):
        self.nodes = []
        self.links = []

    def __len__(self):
        return len(self.nodes)


def load_road_network(game_dir, key, bbox=None):
    """Vehicle nodes (optionally only inside bbox = (x0, y0, x1, y1) in metres) and the
    links between them that can be driven (at least one lane in that direction)."""
    p = os.path.join(game_dir, "pc", "data", "cdimages", "paths.img")
    upd = os.path.join(game_dir, "update", "pc", "data", "cdimages", "paths.img")
    if os.path.isfile(upd):
        p = upd
    if not os.path.isfile(p):
        return RoadNetwork()
    arc = img.ImgArchive(p, key)
    raw = {}      # (area, id) -> (x, y, z, [(target key, lanes out)])
    try:
        for name in arc.names():
            if not name.endswith(".nod"):
                continue
            d = arc.read(name)
            n_nodes, n_veh = struct.unpack_from("<II", d, 0)
            l0 = 16 + 32 * n_nodes
            for i in range(n_veh):
                o = 16 + 32 * i
                area, nid = struct.unpack_from("<HH", d, o + 8)
                first = struct.unpack_from("<H", d, o + 0x12)[0]
                x, y, z = struct.unpack_from("<3h", d, o + 0x14)
                count = d[o + 0x1E] & 0xF
                out = []
                for k in range(count):
                    lo = l0 + 8 * (first + k)
                    if lo + 8 > len(d):
                        break
                    ta, tn, _b4, lanes = struct.unpack_from("<HHBB", d, lo)
                    out.append(((ta, tn), (lanes >> 3) & 7))
                raw[(area, nid)] = (x / 8.0, y / 8.0, z / 64.0, out)
    finally:
        arc.close()

    net = RoadNetwork()
    index = {}
    for k, (x, y, z, _out) in raw.items():
        if bbox is None or (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]):
            index[k] = len(net.nodes)
            net.nodes.append((x, y, z))
    for k, (_x, _y, _z, out) in raw.items():
        if k not in index:
            continue
        net.links.append([(index[t], lanes) for t, lanes in out if t in index and lanes > 0])
    return net
