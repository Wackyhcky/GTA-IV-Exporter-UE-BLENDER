"""Exports the GTA IV map for Unreal Engine (no Blender needed).

Writes into <out_dir>:
    meshes/batch_NNN.gltf + .bin   many models per file, one mesh node each
    textures/<name>.png    (Unreal can't import compressed DDS)
    gta4_unreal.json               materials, models and placements
`unreal_runner.py` then imports all of it inside the editor.
"""

import base64
import fnmatch
import json
import os
import re
import struct
import time

import numpy as np

from .mapdata import SURFACE_PRIORITY, GameData, emissive_kind, load_distant_lights, model_size, surface_kind, placed_lights, select_detail, split_interiors

ALPHA_SHADERS = ("alpha", "cutout", "decal", "glass", "window", "billboard", "trees", "grass", "wire")
BATCH_MODELS = 150


def ue_name(s):
    """Unreal asset names: letters, digits and underscores only."""
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _quat(r):
    """WPL quaternion -> rotation quaternion (w, x, y, z) in GTA space."""
    x, y, z, w = r
    return (w, -x, -y, -z)


# GTA (right-handed, Z up) -> glTF (right-handed, Y up): (x, y, z) -> (x, z, -y)
_C4 = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], np.float64)


def _quat_from_matrix(m):
    """3x3 rotation -> (x, y, z, w)."""
    t = m[0, 0] + m[1, 1] + m[2, 2]
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        q = ((m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s)
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = (0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s)
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = ((m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s)
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = ((m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s)
    q = np.array(q)
    return (q / np.linalg.norm(q)).tolist()


class _GltfBatch:
    def __init__(self):
        self.roots = []
        self.skins = []
        self.bin = bytearray()
        self.accessors = []
        self.views = []
        self.meshes = []
        self.nodes = []
        self.materials = []
        self._mat_index = {}

    def _add(self, arr, target, comp, typ, minmax=False):
        data = np.ascontiguousarray(arr).tobytes()
        while len(self.bin) % 4:
            self.bin.append(0)
        view = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        self.views.append(view)
        self.bin += data
        acc = {"bufferView": len(self.views) - 1, "componentType": comp, "count": len(arr), "type": typ}
        if minmax:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def material(self, name):
        if name not in self._mat_index:
            self._mat_index[name] = len(self.materials)
            self.materials.append({"name": name, "pbrMetallicRoughness": {"metallicFactor": 0.0}})
        return self._mat_index[name]

    def add_model(self, name, mesh, slot_names):
        # GTA (right-handed, Z up) -> glTF (right-handed, Y up): (x, y, z) -> (x, z, -y)
        p = mesh.positions
        pos = np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1).astype(np.float32)
        n = mesh.normals
        nrm = np.stack([n[:, 0], n[:, 2], -n[:, 1]], axis=1).astype(np.float32)
        ln = np.linalg.norm(nrm, axis=1)
        bad = ln < 1e-6
        nrm[bad] = (0.0, 1.0, 0.0)
        ln[bad] = 1.0
        nrm /= ln[:, None]
        uv = mesh.uvs.astype(np.float32)  # GTA UVs are already top-left origin like glTF
        a_pos = self._add(pos, 34962, 5126, "VEC3", minmax=True)
        a_nrm = self._add(nrm, 34962, 5126, "VEC3")
        a_uv = self._add(uv, 34962, 5126, "VEC2")
        prims = []
        for mi in np.unique(mesh.face_mats):
            idx = mesh.faces[mesh.face_mats == mi].astype(np.uint32).ravel()
            a_idx = self._add(idx, 34963, 5125, "SCALAR")
            prims.append({"attributes": {"POSITION": a_pos, "NORMAL": a_nrm, "TEXCOORD_0": a_uv},
                          "indices": a_idx, "material": self.material(slot_names[mi]), "mode": 4})
        self.meshes.append({"name": name, "primitives": prims})
        self.nodes.append({"name": name, "mesh": len(self.meshes) - 1})
        self.roots.append(len(self.nodes) - 1)

    def add_skinned_model(self, name, mesh, slot_names, skeleton):
        """A skinned mesh plus its joint hierarchy (one skin per call)."""
        p = mesh.positions
        pos = np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1).astype(np.float32)
        n = mesh.normals
        nrm = np.stack([n[:, 0], n[:, 2], -n[:, 1]], axis=1).astype(np.float32)
        ln = np.linalg.norm(nrm, axis=1)
        nrm[ln < 1e-6] = (0.0, 1.0, 0.0)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-6)[:, None]
        uv = mesh.uvs.astype(np.float32)
        joints = np.clip(mesh.joints, 0, len(skeleton.names) - 1).astype(np.uint16)
        weights = mesh.weights.astype(np.float32)
        a_pos = self._add(pos, 34962, 5126, "VEC3", minmax=True)
        a_nrm = self._add(nrm, 34962, 5126, "VEC3")
        a_uv = self._add(uv, 34962, 5126, "VEC2")
        a_j = self._add(joints, 34962, 5123, "VEC4")
        a_w = self._add(weights, 34962, 5126, "VEC4")
        prims = []
        for mi in np.unique(mesh.face_mats):
            idx = mesh.faces[mesh.face_mats == mi].astype(np.uint32).ravel()
            prims.append({"attributes": {"POSITION": a_pos, "NORMAL": a_nrm, "TEXCOORD_0": a_uv,
                                         "JOINTS_0": a_j, "WEIGHTS_0": a_w},
                          "indices": self._add(idx, 34963, 5125, "SCALAR"),
                          "material": self.material(slot_names[mi]), "mode": 4})
        # joints: rest pose converted to glTF space
        first = len(self.nodes)
        ibm = []
        for i, bname in enumerate(skeleton.names):
            lg = _C4 @ skeleton.local[i] @ _C4.T
            wg = _C4 @ skeleton.world[i] @ _C4.T
            self.nodes.append({"name": bname, "translation": lg[:3, 3].tolist(),
                               "rotation": _quat_from_matrix(lg[:3, :3])})
            ibm.append(np.linalg.inv(wg).T.ravel())  # glTF matrices are column-major
        for i, parent in enumerate(skeleton.parents):
            if parent >= 0:
                self.nodes[first + parent].setdefault("children", []).append(first + i)
            else:
                self.roots.append(first + i)
        a_ibm = self._add(np.array(ibm, np.float32), None, 5126, "MAT4")
        self.skins.append({"name": name + "_skin", "joints": list(range(first, first + len(skeleton.names))),
                           "inverseBindMatrices": a_ibm,
                           "skeleton": first + next(i for i, p in enumerate(skeleton.parents) if p < 0)})
        self.meshes.append({"name": name, "primitives": prims})
        self.nodes.append({"name": name, "mesh": len(self.meshes) - 1, "skin": len(self.skins) - 1})
        self.roots.append(len(self.nodes) - 1)

    def write(self, path):
        bin_name = os.path.splitext(os.path.basename(path))[0] + ".bin"
        with open(os.path.join(os.path.dirname(path), bin_name), "wb") as f:
            f.write(self.bin)
        doc = {
            "asset": {"version": "2.0", "generator": "GTA IV Exporter"},
            "scene": 0,
            "scenes": [{"nodes": self.roots}],
            "nodes": self.nodes,
            **({"skins": self.skins} if self.skins else {}),
            "meshes": self.meshes,
            "materials": self.materials,
            "accessors": self.accessors,
            "bufferViews": self.views,
            "buffers": [{"uri": bin_name, "byteLength": len(self.bin)}],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))


class UnrealExporter:
    def __init__(self, game_dir, out_dir, areas="*", lods=False, textures=True, key_cache=None, log=print,
                 detail=None, interiors=True, lights=True, traffic=False, characters=False, ped_filter=None):
        self.game_dir = game_dir
        self.out_dir = out_dir
        self.area_patterns = [a.strip().lower() for a in areas.replace(";", ",").split(",") if a.strip()] or ["*"]
        self.detail = detail or ("LOW" if lods else "FULL")
        self.textures = textures
        self.interiors = interiors
        self.lights = lights
        self.traffic = traffic
        self.characters = characters
        self.ped_filter = ped_filter  # None = every ped, else a list of names
        self.key_cache = key_cache or os.path.join(out_dir, "gta4_key.bin")
        self.log = log
        self._txd_cache = {}
        self._wdd_cache = {}
        self.materials = {}   # material asset name -> {diffuse, bump, masked}
        self.tex_names = {}   # GTA texture name -> written file stem (or None)

    def _find_texture(self, name, mesh, txd):
        tex = mesh.textures.get(name)
        if tex is not None:
            return tex
        for t in self.gd.txd_chain(txd):
            d = self._txd_cache.get(t)
            if d is None:
                d = self._txd_cache[t] = self.gd.load_txd(t)
            tex = d.get(name)
            if tex is not None:
                return tex
        return None

    def _texture(self, name, mesh, txd):
        if not name or not self.textures:
            return None
        if name in self.tex_names:
            return self.tex_names[name]
        stem = None
        tex = self._find_texture(name, mesh, txd)
        if tex is not None and tex.data:
            stem = "T_" + ue_name(name)
            path = os.path.join(self.out_dir, "textures", stem + ".png")
            if not os.path.isfile(path):
                try:
                    png = tex.to_png()
                except Exception as e:
                    self.log("Couldn't decode texture %s: %s" % (name, e))
                    self.tex_names[name] = None
                    return None
                with open(path, "wb") as f:
                    f.write(png)
        self.tex_names[name] = stem
        return stem

    def _material_name(self, m, mesh, txd):
        masked = any(s in m.shader.lower() for s in ALPHA_SHADERS)
        diffuse = self._texture(m.diffuse, mesh, txd)
        bump = self._texture(m.bump, mesh, txd)
        name = "MI_" + (ue_name(m.diffuse) if m.diffuse else "untextured")
        if masked:
            name += "_M"
        if bump:
            name += "_N"
        if name not in self.materials:
            self.materials[name] = {"diffuse": diffuse, "bump": bump, "masked": masked, "kind": "opaque"}
        kind = surface_kind(m.shader)
        if SURFACE_PRIORITY[kind] > SURFACE_PRIORITY[self.materials[name]["kind"]]:
            self.materials[name]["kind"] = kind
        glow = emissive_kind(m.shader)
        if glow is not None and diffuse:
            old = self.materials[name].get("emissive")
            # a texture used both ways: "always glows" wins over "night only"
            if old is None or (old[0] and not glow[0]) or (old[0] == glow[0] and glow[1] > old[1]):
                self.materials[name]["emissive"] = [glow[0], glow[1]]
        return name

    def _distant_traffic(self, placements, spacing=40.0, margin=150.0):
        """GTA IV's distant traffic lights for roads inside the exported districts:
        one row per car (segment start, segment vector, red?, phase along the segment)."""
        xy = [(r[1], r[2]) for rows in placements.values() for r in rows]
        if not xy:
            return []
        x0, x1 = min(p[0] for p in xy) - margin, max(p[0] for p in xy) + margin
        y0, y1 = min(p[1] for p in xy) - margin, max(p[1] for p in xy) + margin
        rows = []
        for pts, red in load_distant_lights(self.game_dir):
            for a, b in zip(pts, pts[1:]):
                if not (x0 <= a[0] <= x1 and y0 <= a[1] <= y1):
                    continue
                d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
                length = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
                if length < 1.0:
                    continue
                n = max(1, int(round(length / spacing)))
                for j in range(n):
                    rows.append([a[0], a[1], a[2] + 1.0, d[0], d[1], d[2], int(red), round(j / n, 4)])
        self.log("Distant traffic: %d moving lights" % len(rows))
        return rows

    # ------------------------------------------------------------ traffic
    def _veh_texture(self, name, mesh, chain, va):
        if not name or not self.textures:
            return None
        key = "veh:" + name
        if key in self.tex_names:
            return self.tex_names[key]
        tex = mesh.textures.get(name)
        for t in chain:
            if tex is not None:
                break
            tex = va.textures(t).get(name)
        stem = None
        if tex is not None and tex.data:
            stem = "T_VEH_" + ue_name(name)
            path = os.path.join(self.out_dir, "textures", stem + ".png")
            if not os.path.isfile(path):
                try:
                    with open(path, "wb") as f:
                        f.write(tex.to_png())
                except Exception as e:
                    self.log("Couldn't decode texture %s: %s" % (name, e))
                    stem = None
        self.tex_names[key] = stem
        return stem

    def _veh_material(self, m, mesh, chain, va):
        from .vehicles import paint_slot
        sh = m.shader.lower()
        diffuse = self._veh_texture(m.diffuse, mesh, chain, va)
        bump = self._veh_texture(m.bump, mesh, chain, va)
        slot = paint_slot(sh)
        glass = "glass" in sh
        name = "MI_VEH_" + (ue_name(m.diffuse) if m.diffuse else "untextured")
        name += ("_P%d" % slot if slot else "") + ("_G" if glass else "") + ("_N" if bump else "")
        if "lightsemissive" in sh:
            name += "_L"
        if name not in self.materials:
            mat = {"diffuse": diffuse, "bump": bump, "masked": False, "kind": "glass" if glass else "opaque"}
            if slot:
                mat["paint"] = slot
            if "lightsemissive" in sh and diffuse:
                mat["emissive"] = [True, 3.0]  # head/tail light glass glows at night
            self.materials[name] = mat
        return name

    def _road_network(self, placements, margin=200.0):
        """The drivable road network around the exported districts (Unreal units), used by
        the GTAIVTraffic plugin for cars and, along its curbs, pedestrians."""
        from .roads import load_road_network
        xy = [(r[1], r[2]) for rows in placements.values() for r in rows]
        if not xy:
            return None
        bbox = (min(p[0] for p in xy) - margin, min(p[1] for p in xy) - margin,
                max(p[0] for p in xy) + margin, max(p[1] for p in xy) + margin)
        net = load_road_network(self.game_dir, self.gd.key, bbox)
        nodes, first, count, target, lanes = [], [], [], [], []
        for (x, y, z), out in zip(net.nodes, net.links):
            nodes.append([round(x * 100, 1), round(-y * 100, 1), round(z * 100, 1)])
            first.append(len(target))
            count.append(len(out))
            for t, la in out:
                target.append(t)
                lanes.append(la)
        self.log("Roads: %d nodes, %d drivable links" % (len(nodes), len(target)))
        return {"nodes": nodes, "first": first, "count": count, "target": target, "lanes": lanes, "vehicles": []}

    def _export_traffic(self, placements, batches, batch_meshes):
        """GTA IV's traffic cars (bodies + wheels, paint slots) plus the road network, for the
        GTAIVTraffic Unreal plugin."""
        from .vehicles import VehicleArchive, load_vehicle_defs
        net = self._road_network(placements)
        if net is None:
            return None
        defs, txd_parent = load_vehicle_defs(self.game_dir)
        va = VehicleArchive(self.game_dir, self.gd.key)
        cars = []
        state = {"batch": _GltfBatch(), "no": len(batches)}

        def flush():
            b = state["batch"]
            if b.nodes:
                fn = "veh_%03d.gltf" % state["no"]
                b.write(os.path.join(self.out_dir, "meshes", fn))
                batches.append(fn)
                batch_meshes[fn] = [n["name"] for n in b.nodes]
                state["no"] += 1
                state["batch"] = _GltfBatch()

        try:
            for name in sorted(n for n, d in defs.items() if d.in_traffic):
                d = defs[name]
                try:
                    vm = va.model(name)
                except Exception as e:
                    self.log("Failed to read vehicle %s: %s" % (name, e))
                    continue
                if vm is None or vm.body is None:
                    continue
                chain, t = [], d.txd
                while t and t not in chain:
                    chain.append(t)
                    t = txd_parent.get(t)
                chain.append("vehshare")
                body_name = "SM_VEH_" + ue_name(name)
                state["batch"].add_model(body_name, vm.body,
                                         [self._veh_material(m, vm.body, chain, va) for m in vm.body.materials])
                wheel_names = {}
                for wname, wm in vm.wheels.items():
                    wn = "%s_%s" % (body_name, ue_name(wname))
                    state["batch"].add_model(wn, wm, [self._veh_material(m, wm, chain, va) for m in wm.materials])
                    wheel_names[wname] = (wn, float((wm.positions[:, 2].max() - wm.positions[:, 2].min()) * 50.0))
                wheels, lowest = [], 0.0
                for bone, (x, y, z), mesh, right in vm.wheel_slots:
                    wn, radius = wheel_names[mesh]
                    wheels.append({"mesh": wn, "offset": [x * 100, -y * 100, z * 100], "right": right,
                                   "steers": bone in ("wheel_lf", "wheel_rf"), "radius": radius})
                    lowest = min(lowest, z * 100 - radius)
                ext = vm.body.positions.max(0) - vm.body.positions.min(0)
                paints = [[[round((c / 255.0) ** 2.2, 4) for c in rgb] for rgb in combo] for combo in d.colors]
                cars.append({"name": name, "body": body_name, "wheels": wheels, "paints": paints,
                             "frequency": d.frequency, "length": float(ext[1] * 100), "width": float(ext[0] * 100),
                             "ride_height": float(-lowest if wheels else 60.0)})
                if len(state["batch"].nodes) >= 60:
                    flush()
            flush()
        finally:
            va.close()
        self.log("Traffic: %d vehicles" % len(cars))
        net["vehicles"] = cars
        return net

    # ------------------------------------------------------------ characters & weapons
    def _tex_from(self, prefix, name, mesh, dicts):
        """Write texture `name` (embedded in mesh or in one of dicts) as <prefix><name>.png."""
        if not name or not self.textures:
            return None
        key = prefix + name
        if key in self.tex_names:
            return self.tex_names[key]
        tex = mesh.textures.get(name)
        for d in dicts:
            if tex is not None:
                break
            tex = d.get(name)
        stem = None
        if tex is not None and tex.data:
            stem = "T_" + ue_name(prefix + name)
            path = os.path.join(self.out_dir, "textures", stem + ".png")
            if not os.path.isfile(path):
                try:
                    with open(path, "wb") as f:
                        f.write(tex.to_png())
                except Exception as e:
                    self.log("Couldn't decode texture %s: %s" % (name, e))
                    stem = None
        self.tex_names[key] = stem
        return stem

    def _char_material(self, prefix, m, mesh, dicts):
        diffuse = self._tex_from(prefix, m.diffuse, mesh, dicts)
        bump = self._tex_from(prefix, m.bump, mesh, dicts)
        kind = surface_kind(m.shader)
        if kind == "decal":
            kind = "masked"  # decals on a character (tattoos, dirt) can't be mesh decals
        name = "MI_" + ue_name(prefix + (m.diffuse or "untextured")) + ("_M" if kind == "masked" else "") + \
            ("_G" if kind == "glass" else "") + ("_N" if bump else "")
        if name not in self.materials:
            self.materials[name] = {"diffuse": diffuse, "bump": bump, "masked": kind == "masked", "kind": kind,
                                    "skinned": True}
        return name

    def _export_characters(self, batches, batch_meshes, progress=None):
        """Every GTA IV ped (default outfit) as a skinned glTF on the shared 80-bone skeleton,
        plus the weapons/held items as static meshes."""
        from .peds import PedArchive
        pa = PedArchive(self.game_dir, self.gd.key)
        peds = []
        try:
            names = pa.ped_names()
            if self.ped_filter:
                names = [n for n in names if n in set(self.ped_filter)]
            for k, name in enumerate(names):
                try:
                    p = pa.ped(name)
                except Exception as e:
                    self.log("Failed to read ped %s: %s" % (name, e))
                    continue
                if p is None:
                    continue
                prefix = "PED_%s_" % name
                dicts = [pa.textures(name)]
                slots = [self._char_material(prefix, m, p.mesh, dicts) for m in p.mesh.materials]
                b = _GltfBatch()
                mesh_name = "SK_PED_" + ue_name(name)
                b.add_skinned_model(mesh_name, p.mesh, slots, p.skeleton)
                fn = "ped_%s.gltf" % ue_name(name)
                b.write(os.path.join(self.out_dir, "meshes", fn))
                peds.append({"name": name, "mesh": mesh_name, "gltf": fn, "parts": p.parts,
                             "height": float(p.mesh.positions[:, 2].max() - p.mesh.positions[:, 2].min())})
                if k % 25 == 0:
                    self.log("  characters %d / %d" % (k, len(names)))
            player = None
            if not self.ped_filter or "player" in self.ped_filter:
                try:
                    p = pa.player()
                except Exception as e:
                    self.log("Failed to read Niko (playerped.rpf): %s" % e)
                    p = None
                if p is not None:
                    dicts = [pa.player_textures(p)]
                    slots = [self._char_material("PED_niko_", m, p.mesh, dicts) for m in p.mesh.materials]
                    b = _GltfBatch()
                    b.add_skinned_model("SK_PED_Niko", p.mesh, slots, p.skeleton)
                    b.write(os.path.join(self.out_dir, "meshes", "ped_niko.gltf"))
                    player = {"name": "niko", "mesh": "SK_PED_Niko", "gltf": "ped_niko.gltf", "parts": p.parts,
                              "player": True}
                    peds.append(player)
            weapons = []
            b = _GltfBatch()
            for name in pa.weapon_names():
                try:
                    w = pa.weapon(name)
                except Exception:
                    w = None
                if w is None:
                    continue
                prefix = "WPN_%s_" % name
                slots = [self._char_material(prefix, m, w, [pa.textures(name)]) for m in w.materials]
                for s_ in slots:
                    self.materials[s_].pop("skinned", None)
                b.add_model("SM_WPN_" + ue_name(name), w, slots)
                weapons.append("SM_WPN_" + ue_name(name))
            if b.nodes:
                fn = "wpn_%03d.gltf" % len(batches)
                b.write(os.path.join(self.out_dir, "meshes", fn))
                batches.append(fn)
                batch_meshes[fn] = [n["name"] for n in b.nodes if "mesh" in n]
        finally:
            pa.close()
        self.log("Characters: %d peds%s, %d weapons/items" % (len(peds) - (player is not None),
                                                             " + Niko" if player else "", len(weapons)))
        return {"peds": peds, "weapons": weapons}

    def _wanted_area(self, area):
        return any(fnmatch.fnmatch(area, p) for p in self.area_patterns)

    def run(self, progress=None):
        t0 = time.time()
        for sub in ("meshes", "textures"):
            os.makedirs(os.path.join(self.out_dir, sub), exist_ok=True)
        self.gd = gd = GameData(self.game_dir, log=self.log, key_cache=self.key_cache)
        gd.load_definitions()
        gd.open_archives()
        areas = gd.load_instances(interiors=self.interiors)

        selected = {}
        for area, insts in areas.items():
            if self._wanted_area(area):
                keep = select_detail(gd, insts, area, self.detail)
                if keep:
                    selected[area] = keep
        if not selected:
            raise RuntimeError("No placements matched areas '%s'" % ",".join(self.area_patterns))
        selected = split_interiors(selected)
        need = {}  # model -> smallest size any of its placements accepts
        for v in selected.values():
            for i, min_size in v:
                need[i.hash] = min(need.get(i.hash, min_size), min_size)
        hashes = sorted(need)
        sizes = {}
        self.log("Detail level %s: %d candidate placements of %d models from %d areas"
                 % (self.detail, sum(len(v) for v in selected.values()), len(hashes), len(selected)))

        models = {}  # hash -> {"name", "batch"}
        batch, batch_no, batches = _GltfBatch(), 0, []
        batch_meshes = {}  # gltf file -> mesh names in it (lets the importer resume)

        def flush():
            nonlocal batch, batch_no
            if batch.nodes:
                fn = "batch_%03d.gltf" % batch_no
                batch.write(os.path.join(self.out_dir, "meshes", fn))
                batches.append(fn)
                batch_meshes[fn] = [n["name"] for n in batch.nodes]
                batch_no += 1
                batch = _GltfBatch()

        for n, h in enumerate(hashes):
            mdef = gd.models[h]
            try:
                mesh = gd.load_model(mdef, self._wdd_cache)
            except Exception as e:
                self.log("Failed to read %s: %s" % (mdef.name, e))
                mesh = None
            if mesh is not None and model_size(mesh) < need[h]:
                mesh = None  # too small for this detail level
            if mesh is not None:
                sizes[h] = model_size(mesh)
                slots = [self._material_name(m, mesh, mdef.txd) for m in mesh.materials]
                sm = "SM_" + ue_name(mdef.name)
                batch.add_model(sm, mesh, slots)
                models[h] = {"name": sm, "batch": "batch_%03d" % batch_no}
                if len(batch.nodes) >= BATCH_MODELS:
                    flush()
            if progress:
                progress(n / len(hashes))
            if n % 1000 == 0:
                self.log("  models %d / %d  (%.0fs)" % (n, len(hashes), time.time() - t0))
        flush()

        placements = {}
        lights = {}
        for area, insts in sorted(selected.items()):
            rows = []
            kept = []
            for i, min_size in insts:
                m = models.get(i.hash)
                if m is None or sizes[i.hash] < min_size:
                    continue
                kept.append(i)
                rows.append([m["name"]] + [round(v, 4) for v in i.pos] + [round(v, 6) for v in _quat(i.rot)])
            placements[area] = rows
            if self.lights:
                # [spot, x, y, z, dx, dy, dz, r, g, b, range, inner, outer] in GTA space
                lr = [[int(L.spot)] + [round(v, 3) for v in pos] + [round(v, 4) for v in d]
                      + list(L.color) + [L.range, L.inner, L.outer]
                      for pos, d, L in placed_lights(self.gd, kept)]
                if lr:
                    lights[area] = lr
        distant = self._distant_traffic(placements) if self.lights else []
        if self.traffic:
            traffic = self._export_traffic(placements, batches, batch_meshes)
        elif self.characters:
            traffic = self._road_network(placements)  # sidewalks for pedestrians, no cars
        else:
            traffic = None
        characters = self._export_characters(batches, batch_meshes, progress) if self.characters else None
        data = {
            "version": 1,
            "batches": batches,
            "batch_meshes": batch_meshes,
            "textures": sorted({v for v in self.tex_names.values() if v}),
            "materials": self.materials,
            "placements": placements,  # [mesh, x, y, z, qw, qx, qy, qz] in GTA space (metres, right-handed)
            "lights": lights,
            "distant": distant,
            "traffic": traffic,
            "characters": characters,  # [x, y, z, dx, dy, dz, red, phase]: one moving car light per row
        }
        with open(os.path.join(self.out_dir, "gta4_unreal.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        for _, a in gd.archives:
            a.close()
        self.log("Wrote %d mesh batches, %d textures, %d materials in %.0fs"
                 % (len(batches), len(data["textures"]), len(self.materials), time.time() - t0))
        return sum(len(v) for v in placements.values())
