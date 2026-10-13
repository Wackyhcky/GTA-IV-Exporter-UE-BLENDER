"""GTA IV characters (componentpeds.img) and weapons (weapons.img).

A ped is <name>.wft (skeleton: GTA IV's 80-bone "Char_*" rig), <name>.wdd (body-part
drawables named <slot>_<variant>_<letter>, e.g. head_000_r, uppr_001_u, lowr_000_m) and
<name>.wtd (textures). Niko is separate: playerped.rpf holds player.wft and one .wdr per
body part (head_000_r.wdr, ...) with its textures in .wtd files named after them. Body-part vertices are skinned: 4 weights + 4 indices into a per-geometry
bone table (see drawable._read_geometry)."""

import os
import string

import numpy as np

from . import drawable, img, rpf, texture
from .mapdata import joaat
from .rsc import Resource

SLOTS = ("head", "uppr", "lowr", "suse", "hand", "feet", "jack", "hair", "sus2", "teef", "face")
_NAMES = {}


def component_name(h):
    """joaat hash of a body-part drawable -> its name (e.g. 'uppr_000_u')."""
    if not _NAMES:
        for sl in SLOTS:
            for i in range(64):
                for v in string.ascii_lowercase:
                    n = "%s_%03d_%s" % (sl, i, v)
                    _NAMES[joaat(n)] = n
    return _NAMES.get(h)


class Skeleton:
    """names, parents (-1 = root), local (4x4) and world (4x4) rest matrices, GTA space, metres."""

    def __init__(self):
        self.names = []
        self.parents = []
        self.local = []
        self.world = []


def read_skeleton(res, skel):
    bones = res.ptr(skel)
    count = res.u16(skel + 0x14)
    sk = Skeleton()
    if bones is None or not count:
        return sk
    addr = {bones + i * 0xE0: i for i in range(count)}
    for i in range(count):
        b = bones + i * 0xE0
        sk.names.append(res.cstr(res.ptr(b)))
        sk.parents.append(addr.get(res.ptr(b + 0x10), -1))
        m = np.eye(4)
        m[:3, :3] = drawable._quat_matrix(*res.f32(b + 0x40, 4))
        m[:3, 3] = res.f32(b + 0x20, 3)
        sk.local.append(m)
    # Make names consistent across ped types so they all share one Unreal skeleton:
    # some peds call the toes Toe0, and the Extra_## helpers hang off different parents.
    for i, n in enumerate(sk.names):
        n = n.replace("_Toe0", "_Toe")
        if n.startswith("Extra_") and sk.parents[i] >= 0:
            n += "_" + sk.names[sk.parents[i]].replace("Char_", "").replace("_", "")
        sk.names[i] = n
    for i in range(count):
        p = sk.parents[i]
        sk.world.append(sk.world[p] @ sk.local[i] if 0 <= p < i else sk.local[i])
    return sk


GROUND = 1.0  # GTA peds have their origin at hip height, about a metre above the soles
TO_UNREAL = np.array([[-1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 1, GROUND], [0, 0, 0, 1]], float)


def add_ground_root(sk):
    """Put the origin at the feet and face the other way, like Unreal/GASP characters: a new
    'Root' bone on the ground becomes the parent of 'Char', and the body is turned 180 degrees
    about the vertical. Root is appended so existing skin indices stay valid."""
    r = len(sk.names)
    sk.names.append("Root")
    sk.parents.append(-1)
    sk.local.append(np.eye(4))
    for i, p in enumerate(sk.parents[:r]):
        if p < 0:
            sk.parents[i] = r
            sk.local[i] = TO_UNREAL @ sk.local[i]
    sk.world = [TO_UNREAL @ w for w in sk.world] + [np.eye(4)]


def _merge(meshes):
    """Concatenate finished drawable.Mesh objects (materials appended per mesh)."""
    out = drawable.Mesh()
    base = 0
    mats = []
    for m in meshes:
        out.positions.append(m.positions)
        out.normals.append(m.normals)
        out.uvs.append(m.uvs)
        out.colors.append(m.colors)
        out.faces.append(m.faces + base)
        out.face_mats.append(m.face_mats + len(mats))
        if m.skinned:
            out.joints.append(m.joints)
            out.weights.append(m.weights)
        else:
            out.joints.append(np.zeros((len(m.positions), 4), np.int32))
            out.weights.append(np.tile(np.array([1, 0, 0, 0], np.float32), (len(m.positions), 1)))
        mats.extend(m.materials)
        out.textures.update(m.textures)
        base += len(m.positions)
    out.materials = mats
    out.skinned = True
    return out if out.finish() else None


def _finish(p, meshes):
    """Merge the body parts and convert to Unreal character conventions."""
    p.mesh = _merge(meshes) if meshes else None
    if p.mesh is None:
        return None
    add_ground_root(p.skeleton)
    m = p.mesh
    m.positions = m.positions @ TO_UNREAL[:3, :3].T + TO_UNREAL[:3, 3]
    m.normals = m.normals @ TO_UNREAL[:3, :3].T
    return p


class Ped:
    def __init__(self, name):
        self.name = name
        self.skeleton = None
        self.mesh = None          # skinned drawable.Mesh of the default outfit
        self.parts = []           # component names used
        self.txd = name


class PedArchive:
    def __init__(self, game_dir, key):
        self.game_dir, self.key = game_dir, key
        self.archives = []
        for rel in (os.path.join("pc", "models", "cdimages", "componentpeds.img"),
                    os.path.join("pc", "models", "cdimages", "weapons.img"),
                    os.path.join("pc", "models", "cdimages", "pedprops.img")):
            for base in (os.path.join(game_dir, "update"), game_dir):
                p = os.path.join(base, rel)
                if os.path.isfile(p):
                    self.archives.append(img.ImgArchive(p, key))
                    break
        self._tex = {}

    def read(self, name):
        for a in self.archives:
            d = a.read(name)
            if d is not None:
                return d
        return None

    def ped_names(self):
        return sorted({n[:-4] for a in self.archives for n in a.names() if n.endswith(".wdd")
                       and any(x.endswith(n[:-4] + ".wft") for x in a.names())})

    def weapon_names(self):
        out = set()
        for a in self.archives:
            names = a.names()
            if any(n.endswith(".wdd") for n in names):
                continue  # ped/prop archives
            out |= {n[:-4] for n in names if n.endswith((".wdr", ".wft"))}
        return sorted(out)

    def textures(self, txd):
        if txd not in self._tex:
            d = self.read(txd + ".wtd")
            try:
                self._tex[txd] = texture.load_wtd(d) if d is not None else {}
            except Exception:
                self._tex[txd] = {}
        return self._tex[txd]

    def ped(self, name, variant=0):
        """The ped's skeleton and its outfit: for each body-part slot, the given variant number
        (or the lowest one the ped has)."""
        wft, wdd = self.read(name + ".wft"), self.read(name + ".wdd")
        if wft is None or wdd is None:
            return None
        p = Ped(name)
        wres = Resource(wft)
        main = wres.ptr(0xB4)
        skel = wres.ptr(main + 0x0C) if main is not None else None
        if skel is None:
            return None
        p.skeleton = read_skeleton(wres, skel)
        res = Resource(wdd)
        hp, count = res.collection(0x10)
        ptrs = res.ptr_array(0x18)
        comps = {}
        for i, d in enumerate(ptrs):
            if d is None or hp is None:
                continue
            n = component_name(res.u32(hp + 4 * i))
            if n:
                comps[n] = d
        chosen = []
        for sl in SLOTS:
            have = sorted(n for n in comps if n.startswith(sl + "_"))
            if not have:
                continue
            want = [n for n in have if int(n.split("_")[1]) == variant] or have
            chosen.append(want[0])
        meshes = []
        for n in chosen:
            try:
                m = drawable.read_drawable(res, comps[n])
            except Exception:
                m = None
            if m is not None:
                meshes.append(m)
                p.parts.append(n)
        return _finish(p, meshes)

    PLAYER_SLOTS = ("head", "uppr", "lowr", "hand", "feet", "hair", "teef")  # no braces/accessories

    def _player_archive(self):
        if not hasattr(self, "_rpf"):
            self._rpf = None
            for base in (os.path.join(self.game_dir, "update"), self.game_dir):
                path = os.path.join(base, "pc", "models", "cdimages", "playerped.rpf")
                if os.path.isfile(path):
                    self._rpf = rpf.RpfArchive(path, self.key)
                    break
        return self._rpf

    def player(self, variant=0):
        """Niko Bellic in his default outfit (from playerped.rpf), on the same skeleton as
        the other peds. Its textures: player_textures(ped)."""
        a = self._player_archive()
        wft = a.read("player.wft") if a else None
        if wft is None:
            return None
        p = Ped("player")
        wres = Resource(wft)
        main = wres.ptr(0xB4)
        skel = wres.ptr(main + 0x0C) if main is not None else None
        if skel is None:
            return None
        p.skeleton = read_skeleton(wres, skel)
        files = [n for n in a.names() if n.endswith(".wdr")]
        meshes = []
        for sl in self.PLAYER_SLOTS:
            have = sorted(n for n in files if n.startswith(sl + "_"))
            if not have:
                continue
            want = [n for n in have if int(n.split("_")[1]) == variant] or have
            try:
                m = drawable.load_wdr(a.read(want[0]))
            except Exception:
                m = None
            if m is not None:
                meshes.append(m)
                p.parts.append(want[0][:-4])
        return _finish(p, meshes)

    def player_textures(self, ped):
        """Textures used by Niko's mesh, each from the .wtd of the same name."""
        a = self._player_archive()
        out = {}
        names = {n for m in ped.mesh.materials for n in (m.diffuse, m.bump) if n}
        for n in names:
            d = a.read(n.lower() + ".wtd") if a else None
            if d is None:
                continue
            try:
                out.update(texture.load_wtd(d))
            except Exception:
                pass
        return out

    def weapon(self, name):
        d = self.read(name + ".wdr")
        if d is not None:
            return drawable.load_wdr(d)
        d = self.read(name + ".wft")
        return drawable.load_wft(d) if d is not None else None

    def close(self):
        for a in self.archives:
            a.close()
        if getattr(self, "_rpf", None):
            self._rpf.close()
