"""Runs inside Unreal Editor:
    set GTA4_UE_JOB=job.json
    UnrealEditor-Cmd.exe <project> -run=pythonscript -script="unreal_runner.py"

Imports what `gta4_map_importer.unreal_export` wrote: textures, master
materials + instances, static meshes, and a level with every placement.
Reports to the GUI with log lines starting with '@@'.
"""

import json
import math
import os
import sys
import time
import traceback

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unreal_lights  # noqa: E402  (bundled next to this script)
import unreal_peds  # noqa: E402

ROOT = "/Game/GTAIV"
LIGHTS_BP = ROOT + "/Blueprints/BP_GTAIV_Lights"
NIGHT_MPC = ROOT + "/Materials/MPC_GTAIV"  # scalar "Night": 1 = night-only glow on
LIGHT_DRAW_DISTANCE = 20000.0  # cm: lights fade out beyond 200 m
LIGHTS_PER_ACTOR = 800


def say(msg):
    unreal.log("@@" + msg)


def progress(f):
    say("PROGRESS %.4f" % f)


def load_job():
    # the GUI passes the job through an env var: Unreal's -script= argument
    # doesn't cope well with extra arguments or paths containing spaces
    env = os.environ.get("GTA4_UE_JOB")
    if env and os.path.isfile(env):
        return json.load(open(env, encoding="utf-8"))
    for a in sys.argv[1:]:
        if a.lower().endswith(".json") and os.path.isfile(a):
            return json.load(open(a, encoding="utf-8"))
    raise RuntimeError("no job file given")


ASSET_TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary


# ------------------------------------------------------------------ textures
def import_textures(export_dir, names, normal_maps, p0, p1):
    dest = ROOT + "/Textures"
    todo = [n for n in names if not EAL.does_asset_exist("%s/%s" % (dest, n))]
    for i in range(0, len(todo), 200):
        tasks = []
        for n in todo[i:i + 200]:
            t = unreal.AssetImportTask()
            t.filename = os.path.join(export_dir, "textures", n + ".png")
            t.destination_path = dest
            t.automated = True
            t.replace_existing = True
            t.save = False
            tasks.append(t)
        ASSET_TOOLS.import_asset_tasks(tasks)
        progress(p0 + (p1 - p0) * min(1.0, (i + 200) / max(1, len(todo))))
    for n in normal_maps:
        tex = unreal.load_asset("%s/%s" % (dest, n))
        if tex:
            tex.set_editor_property("srgb", False)
            tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
    EAL.save_directory(dest, only_if_is_dirty=True)
    say("LOG Imported %d textures" % len(todo))


# ------------------------------------------------------------------ materials
def night_collection():
    """Material Parameter Collection with one scalar, Night (0 = day, 1 = night)."""
    if EAL.does_asset_exist(NIGHT_MPC):
        return unreal.load_asset(NIGHT_MPC)
    mpc = ASSET_TOOLS.create_asset("MPC_GTAIV", ROOT + "/Materials", unreal.MaterialParameterCollection,
                                   unreal.MaterialParameterCollectionFactoryNew())
    p = unreal.CollectionScalarParameter()
    p.set_editor_property("parameter_name", "Night")
    p.set_editor_property("default_value", 0.0)
    mpc.set_editor_property("scalar_parameters", [p])
    EAL.save_loaded_asset(mpc)
    return mpc


def _add_glow(mat, diff):
    """Emissive = Diffuse * EmissiveStrength * lerp(1, Night, NightOnly).
    Instances of GTA's glowing shaders set EmissiveStrength (and NightOnly for bulbs/windows)."""
    es = MEL.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -500, 500)
    es.set_editor_property("parameter_name", "EmissiveStrength")
    es.set_editor_property("default_value", 0.0)
    no = MEL.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -500, 600)
    no.set_editor_property("parameter_name", "NightOnly")
    no.set_editor_property("default_value", 0.0)
    night = MEL.create_material_expression(mat, unreal.MaterialExpressionCollectionParameter, -500, 700)
    night.set_editor_property("collection", night_collection())
    night.set_editor_property("parameter_name", "Night")
    lerp = MEL.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -250, 650)
    lerp.set_editor_property("const_a", 1.0)
    MEL.connect_material_expressions(night, "", lerp, "B")
    MEL.connect_material_expressions(no, "", lerp, "Alpha")
    m1 = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -250, 500)
    MEL.connect_material_expressions(diff, "RGB", m1, "A")
    MEL.connect_material_expressions(es, "", m1, "B")
    m2 = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -100, 550)
    MEL.connect_material_expressions(m1, "", m2, "A")
    MEL.connect_material_expressions(lerp, "", m2, "B")
    _add_day_dim(mat, m2)


def _add_day_dim(mat, glow):
    """Emissive = glow * lerp(DayGlow, 1, Night): always-on glow (neon, signs) is 10% by day.
    Night-only glow is already 0 by day, so it's unaffected."""
    dg = MEL.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -500, 800)
    dg.set_editor_property("parameter_name", "DayGlow")
    dg.set_editor_property("default_value", 0.1)
    night = MEL.create_material_expression(mat, unreal.MaterialExpressionCollectionParameter, -500, 900)
    night.set_editor_property("collection", night_collection())
    night.set_editor_property("parameter_name", "Night")
    lerp = MEL.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -250, 850)
    lerp.set_editor_property("const_b", 1.0)
    MEL.connect_material_expressions(dg, "", lerp, "A")
    MEL.connect_material_expressions(night, "", lerp, "Alpha")
    out = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, 50, 700)
    MEL.connect_material_expressions(glow, "", out, "A")
    MEL.connect_material_expressions(lerp, "", out, "B")
    MEL.connect_material_property(out, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)


MASTERS = {"opaque": "M_GTA_Opaque", "masked": "M_GTA_Masked", "glass": "M_GTA_Glass", "decal": "M_GTA_Decal"}


def _add_paint(mat, diff):
    """BaseColor = Diffuse * PaintColor (white by default). Traffic cars set PaintColor on
    their paint materials (MI_VEH_*_P<slot>) from GTA's carcols combos."""
    pc = MEL.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -500, -300)
    pc.set_editor_property("parameter_name", "PaintColor")
    pc.set_editor_property("default_value", unreal.LinearColor(1, 1, 1, 1))
    mul = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -250, -200)
    MEL.connect_material_expressions(diff, "RGB", mul, "A")
    MEL.connect_material_expressions(pc, "", mul, "B")
    MEL.connect_material_property(mul, "", unreal.MaterialProperty.MP_BASE_COLOR)


def _master(name, kind):
    """kind: 'opaque', 'masked' (hard cut-out: fences, wires, foliage), 'glass' (translucent)
    or 'decal' (mesh decal: blends onto the surface below, like GTA's road markings/grime)."""
    path = "%s/Materials/%s" % (ROOT, name)
    if EAL.does_asset_exist(path):
        mat = unreal.load_asset(path)
        params = [str(n) for n in MEL.get_scalar_parameter_names(mat)]
        changed = False
        # made by an older version: add what's missing in place, so existing instances keep working
        if kind != "decal" and "EmissiveStrength" not in params:
            diff = MEL.get_material_property_input_node(mat, unreal.MaterialProperty.MP_BASE_COLOR)
            if diff is not None:
                _add_glow(mat, diff)
                changed = True
        elif kind != "decal" and "DayGlow" not in params:
            glow = MEL.get_material_property_input_node(mat, unreal.MaterialProperty.MP_EMISSIVE_COLOR)
            if glow is not None:
                _add_day_dim(mat, glow)
                changed = True
        vparams = [str(n) for n in MEL.get_vector_parameter_names(mat)]
        if kind in ("opaque", "glass") and "PaintColor" not in vparams:
            diff = MEL.get_material_property_input_node(mat, unreal.MaterialProperty.MP_BASE_COLOR)
            if isinstance(diff, unreal.MaterialExpressionTextureSampleParameter2D):
                _add_paint(mat, diff)
                changed = True
        try:
            if not mat.get_editor_property("used_with_skeletal_mesh"):
                mat.set_editor_property("used_with_skeletal_mesh", True)  # GTA characters use these too
                changed = True
        except Exception:
            pass
        if changed:
            MEL.recompile_material(mat)
            EAL.save_loaded_asset(mat)
        return mat
    mat = ASSET_TOOLS.create_asset(name, ROOT + "/Materials", unreal.Material, unreal.MaterialFactoryNew())
    # set usage up front, otherwise every instance recompiles the first time the map loads
    for flag in ("used_with_instanced_static_meshes", "used_with_nanite", "used_with_skeletal_mesh"):
        try:
            mat.set_editor_property(flag, True)
        except Exception:
            pass
    diff = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, -100)
    diff.set_editor_property("parameter_name", "Diffuse")
    diff.set_editor_property("texture", unreal.load_asset("/Engine/EngineResources/WhiteSquareTexture"))
    if kind in ("opaque", "glass"):
        _add_paint(mat, diff)
    else:
        MEL.connect_material_property(diff, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
    nrm = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, 250)
    nrm.set_editor_property("parameter_name", "Normal")
    nrm.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    nrm.set_editor_property("texture", unreal.load_asset("/Engine/EngineMaterials/DefaultNormal"))
    MEL.connect_material_property(nrm, "RGB", unreal.MaterialProperty.MP_NORMAL)
    rough = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -250, 100)
    rough.set_editor_property("r", 0.1 if kind == "glass" else 0.85)
    MEL.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    if kind != "decal":
        spec = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -250, 180)
        spec.set_editor_property("r", 0.5 if kind == "glass" else 0.3)
        MEL.connect_material_property(spec, "", unreal.MaterialProperty.MP_SPECULAR)
        _add_glow(mat, diff)
    if kind == "masked":
        mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
        mat.set_editor_property("two_sided", True)
        mat.set_editor_property("opacity_mask_clip_value", 0.4)
        MEL.connect_material_property(diff, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
    elif kind == "glass":
        # see-through, lit per pixel so it still shows reflections/highlights;
        # GTA's glass alpha is the opacity (kept above 0.15 so panes stay visible)
        mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
        mat.set_editor_property("translucency_lighting_mode", unreal.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)
        mx = MEL.create_material_expression(mat, unreal.MaterialExpressionMax, -250, 300)
        mx.set_editor_property("const_b", 0.15)
        MEL.connect_material_expressions(diff, "A", mx, "A")
        MEL.connect_material_property(mx, "", unreal.MaterialProperty.MP_OPACITY)
    elif kind == "decal":
        # mesh decal: drawn onto whatever is behind it, using the texture's alpha
        mat.set_editor_property("material_domain", unreal.MaterialDomain.MD_DEFERRED_DECAL)
        mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
        MEL.connect_material_property(diff, "A", unreal.MaterialProperty.MP_OPACITY)
    MEL.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return mat


def create_materials(materials, p0, p1):
    masters = {kind: _master(name, kind) for kind, name in MASTERS.items()}
    dest = ROOT + "/Materials"
    factory = unreal.MaterialInstanceConstantFactoryNew()
    items = sorted(materials.items())
    glowing = 0
    for i, (name, m) in enumerate(items):
        path = "%s/%s" % (dest, name)
        glow = m.get("emissive")
        kind = m.get("kind") or ("masked" if m["masked"] else "opaque")
        if EAL.does_asset_exist(path):
            if not glow and kind not in ("glass", "decal"):
                continue
            # made earlier, possibly before glow/glass/decal support: update it in place
            mi = unreal.load_asset(path)
            if mi.get_editor_property("parent") != masters[kind]:
                MEL.set_material_instance_parent(mi, masters[kind])
        else:
            mi = ASSET_TOOLS.create_asset(name, dest, unreal.MaterialInstanceConstant, factory)
            MEL.set_material_instance_parent(mi, masters[kind])
            for param, key in (("Diffuse", "diffuse"), ("Normal", "bump")):
                if m.get(key):
                    tex = unreal.load_asset("%s/Textures/%s" % (ROOT, m[key]))
                    if tex:
                        MEL.set_material_instance_texture_parameter_value(mi, param, tex)
        if glow and kind != "decal":
            night_only, strength = glow
            MEL.set_material_instance_scalar_parameter_value(mi, "EmissiveStrength", float(strength))
            MEL.set_material_instance_scalar_parameter_value(mi, "NightOnly", 1.0 if night_only else 0.0)
            glowing += 1
        if i % 200 == 0:
            progress(p0 + (p1 - p0) * i / max(1, len(items)))
    EAL.save_directory(dest, only_if_is_dirty=True)
    say("LOG Created %d materials (%d glowing)" % (len(items), glowing))


# ------------------------------------------------------------------ meshes
def _pipeline(nanite):
    pipe = unreal.InterchangeGenericAssetsPipeline()
    pipe.material_pipeline.import_materials = False
    pipe.material_pipeline.texture_pipeline.import_textures = False
    pipe.mesh_pipeline.combine_static_meshes_behavior = unreal.InterchangeCombineStaticMeshesBehavior.DO_NOT_COMBINE
    pipe.mesh_pipeline.import_skeletal_meshes = False
    pipe.mesh_pipeline.build_nanite = bool(nanite)
    pipe.mesh_pipeline.collision = False  # we use the render mesh as collision instead
    return pipe


def _batch_done(names):
    return bool(names) and all(EAL.does_asset_exist("%s/Meshes/%s" % (ROOT, n)) for n in names)


def import_meshes(export_dir, batches, batch_meshes, nanite, collision, max_batches, p0, p1):
    """Import up to `max_batches` batches that aren't in the project yet.

    Returns the number of batches still left. Unreal's memory grows with every
    import and is never fully released, so the GUI runs this in several
    short-lived editor processes instead of one long one.
    """
    dest = ROOT + "/Meshes"
    mat_cache = {}
    todo = [b for b in batches if not _batch_done(batch_meshes.get(b))]
    if len(todo) < len(batches):
        say("LOG  %d of %d mesh batches already imported" % (len(batches) - len(todo), len(batches)))
    for i, b in enumerate(todo[:max_batches]):
        # InterchangeManager.import_asset only completes its first call in commandlet mode;
        # AssetImportTask with a pipeline stack override works for every batch.
        task = unreal.AssetImportTask()
        task.filename = os.path.join(export_dir, "meshes", b)
        task.destination_path = dest
        task.automated = True
        task.replace_existing = True
        task.save = False
        stack = unreal.InterchangePipelineStackOverride()
        stack.add_pipeline(_pipeline(nanite))
        task.options = stack
        ASSET_TOOLS.import_asset_tasks([task])
        result = list(task.imported_object_paths)
        if not result:
            say("LOG  warning: nothing imported from %s" % b)
        for path in result:
            sm = unreal.load_asset(path)
            if not isinstance(sm, unreal.StaticMesh):
                continue
            for si, slot in enumerate(sm.static_materials):
                name = str(slot.material_slot_name)
                if name not in mat_cache:
                    mat_cache[name] = unreal.load_asset("%s/Materials/%s" % (ROOT, name))
                if mat_cache[name]:
                    sm.set_material(si, mat_cache[name])
            if collision:
                bs = sm.get_editor_property("body_setup")
                if bs:
                    bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
        EAL.save_directory(dest, only_if_is_dirty=True)
        del task, stack, result
        sm = None
        unreal.SystemLibrary.collect_garbage()
        done = len(batches) - len(todo) + i + 1
        progress(p0 + (p1 - p0) * done / len(batches))
        say("LOG  mesh batch %d / %d" % (done, len(batches)))
    return max(0, len(todo) - max_batches)


# ------------------------------------------------------------------ characters
PED_SKELETON = ROOT + "/Characters/SKEL_GTAIV_Ped"


def import_characters(export_dir, chars, p0, p1):
    """Each GTA IV ped as a skeletal mesh; all share one skeleton (the first import creates it)."""
    if not chars or not chars.get("peds"):
        return
    dest = ROOT + "/Characters"
    skel = unreal.load_asset(PED_SKELETON) if EAL.does_asset_exist(PED_SKELETON) else None
    peds = chars["peds"]
    done = 0
    for i, ped in enumerate(peds):
        path = "%s/%s" % (dest, ped["mesh"])
        if EAL.does_asset_exist(path):
            continue
        pipe = unreal.InterchangeGenericAssetsPipeline()
        pipe.material_pipeline.import_materials = False
        pipe.material_pipeline.texture_pipeline.import_textures = False
        pipe.mesh_pipeline.import_static_meshes = False
        pipe.mesh_pipeline.import_skeletal_meshes = True
        pipe.mesh_pipeline.create_physics_asset = False
        pipe.animation_pipeline.import_animations = False
        if skel is not None:
            pipe.common_skeletal_meshes_and_animations_properties.skeleton = skel
        task = unreal.AssetImportTask()
        task.filename = os.path.join(export_dir, "meshes", ped["gltf"])
        task.destination_path = dest
        task.automated = True
        task.replace_existing = True
        task.save = False
        stack = unreal.InterchangePipelineStackOverride()
        stack.add_pipeline(pipe)
        task.options = stack
        ASSET_TOOLS.import_asset_tasks([task])
        for obj_path in list(task.imported_object_paths):
            obj = unreal.load_asset(obj_path)
            if isinstance(obj, unreal.SkeletalMesh):
                # array elements come back as copies: build a new list and assign it
                slots = []
                for m in obj.get_editor_property("materials"):
                    mi = unreal.load_asset("%s/Materials/%s" % (ROOT, m.get_editor_property("material_slot_name")))
                    if mi:
                        m.set_editor_property("material_interface", mi)
                    slots.append(m)
                obj.set_editor_property("materials", slots)
                if skel is None:
                    # keep the first ped's skeleton as the shared one, under a stable name.
                    # Rename it before the mesh: the other way round the mesh loses its
                    # skeleton reference.
                    sk = obj.get_editor_property("skeleton")
                    if sk is not None and sk.get_path_name().split(".")[0] != PED_SKELETON:
                        if EAL.rename_asset(sk.get_path_name().split(".")[0], PED_SKELETON):
                            sk = unreal.load_asset(PED_SKELETON)
                    skel = sk
                cur = obj.get_path_name().split(".")[0]
                if cur != path:
                    EAL.rename_asset(cur, path)  # Interchange names it after the file
            elif isinstance(obj, unreal.PhysicsAsset):
                EAL.delete_loaded_asset(obj)
        done += 1
        if done % 10 == 0:
            EAL.save_directory(dest, only_if_is_dirty=True)
            unreal.SystemLibrary.collect_garbage()
        progress(p0 + (p1 - p0) * (i + 1) / len(peds))
    EAL.save_directory(dest, only_if_is_dirty=True)
    say("LOG Imported %d characters (skeleton %s)" % (done, PED_SKELETON))
    setup_gasp_peds(dest, [p["mesh"] for p in peds if not p.get("player")])  # Niko isn't in the crowd


def setup_gasp_peds(dest, mesh_names):
    """If the project is based on the Game Animation Sample, make the GTA peds usable with
    its animation: IK rig + retargeter, ABP_GTAIV_Retarget and BP_GTAIV_Ped."""
    if not unreal_peds.gasp_present():
        say("LOG Game Animation Sample not found in this project: skipping the ped animation setup")
        return
    meshes = ["%s/%s" % (dest, n) for n in mesh_names if EAL.does_asset_exist("%s/%s" % (dest, n))]
    if not meshes:
        return
    try:
        rtg = unreal_peds.build_retargeter(dest, meshes[0], say)
        ped_cls = unreal_peds.build_ped_blueprint(dest, rtg, meshes, say)
        unreal_peds.build_game_mode(dest, ped_cls, say)
    except Exception:
        unreal.log_error(traceback.format_exc())
        say("LOG Couldn't set up the GASP ped animation (see the Unreal log)")


# ------------------------------------------------------------------ level
def _transform(row):
    """GTA (metres, right-handed) -> Unreal (cm, left-handed: Y is mirrored)."""
    _, x, y, z, qw, qx, qy, qz = row
    q = unreal.Quat(-qx, qy, -qz, qw)
    return unreal.Transform(unreal.Vector(x * 100.0, -y * 100.0, z * 100.0), q.rotator(), unreal.Vector(1, 1, 1))


def _add_lighting(eas):
    """New levels are empty; add a basic daylight setup so the city is visible."""
    sun = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 50000),
                                     unreal.Rotator(roll=0, pitch=-40, yaw=-60))
    sun.set_actor_label("Sun")
    lc = sun.get_component_by_class(unreal.DirectionalLightComponent)
    lc.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)  # can be rotated for day/night
    lc.set_editor_property("intensity", 8.0)
    lc.set_editor_property("atmosphere_sun_light", True)
    lc.set_editor_property("dynamic_shadow_distance_movable_light", 30000.0)
    sky = eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0))
    sky.set_actor_label("SkyAtmosphere")
    sl = eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 50000))
    sl.set_actor_label("SkyLight")
    slc = sl.get_component_by_class(unreal.SkyLightComponent)
    slc.set_editor_property("real_time_capture", True)
    slc.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    fog = eas.spawn_actor_from_class(unreal.ExponentialHeightFog, unreal.Vector(0, 0, 0))
    fog.set_actor_label("HeightFog")
    fog.get_component_by_class(unreal.ExponentialHeightFogComponent).set_editor_property("fog_density", 0.005)
    for a in (sun, sky, sl, fog):
        a.set_folder_path("Lighting")


def _add_coronas(sds, lib, root_handle, rows, plane, mat):
    """GTA's coronas: one glowing camera-facing sprite per light (instanced, so cheap),
    under the lights actor's root so they switch with the lights."""
    handle, _fail = sds.add_new_subobject(unreal.AddNewSubobjectParams(
        parent_handle=root_handle, new_class=unreal.InstancedStaticMeshComponent))
    comp = lib.get_object(lib.get_data(handle))
    if comp is None:
        return
    sds.rename_subobject(handle, unreal.Text("Coronas"))
    comp.set_static_mesh(plane)
    comp.set_material(0, mat)
    comp.set_editor_property("cast_shadow", False)
    comp.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
    comp.set_num_custom_data_floats(4)
    xf = [unreal.Transform(unreal.Vector(x * 100.0, -y * 100.0, z * 100.0), unreal.Rotator(0, 0, 0), unreal.Vector(1, 1, 1))
          for _s, x, y, z, *_rest in rows]
    comp.add_instances(xf, False, True)
    for i, (_s, _x, _y, _z, _dx, _dy, _dz, r, g, b, rng, _in, _out) in enumerate(rows):
        size_cm = min(250.0, max(40.0, rng * 6.0))  # 20 m street lamp: 1.2 m glow
        for k, v in enumerate(((r / 255.0) ** 2.2, (g / 255.0) ** 2.2, (b / 255.0) ** 2.2, size_cm)):
            comp.set_custom_data_value(i, k, v, False)


def _place_distant_traffic(rows, bp_class, mpc, plane, eas, sds, lib):
    """GTA IV's distant traffic: moving white/red dots along the roads, seen from afar."""
    mat = unreal_lights.build_corona_material(ROOT + "/Materials/M_GTA_DistantTraffic", mpc, moving=True)
    actor = eas.spawn_actor_from_class(bp_class, unreal.Vector(0, 0, 0))
    actor.set_actor_label("GTAIV_DistantTraffic")
    actor.set_folder_path("GTAIV/Lights")
    root = actor.get_editor_property("root_component")
    handles = sds.k2_gather_subobject_data_for_instance(actor)
    root_handle = next((h for h in handles if lib.get_object(lib.get_data(h)) == root), handles[0])
    handle, _fail = sds.add_new_subobject(unreal.AddNewSubobjectParams(
        parent_handle=root_handle, new_class=unreal.InstancedStaticMeshComponent))
    comp = lib.get_object(lib.get_data(handle))
    sds.rename_subobject(handle, unreal.Text("DistantTraffic"))
    comp.set_static_mesh(plane)
    comp.set_material(0, mat)
    comp.set_editor_property("cast_shadow", False)
    comp.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
    comp.set_editor_property("bounds_scale", 60.0)  # dots travel up to a whole road segment away
    comp.set_num_custom_data_floats(8)
    comp.add_instances([unreal.Transform(unreal.Vector(x * 100.0, -y * 100.0, z * 100.0), unreal.Rotator(0, 0, 0),
                                         unreal.Vector(1, 1, 1)) for x, y, z, *_r in rows], False, True)
    white, red = (1.0, 0.85, 0.6), (1.0, 0.03, 0.01)
    for i, (_x, _y, _z, dx, dy, dz, is_red, phase) in enumerate(rows):
        c = red if is_red else white
        for k, v in enumerate((c[0], c[1], c[2], 60.0, dx * 100.0, -dy * 100.0, dz * 100.0, phase)):
            comp.set_custom_data_value(i, k, v, False)
    say("LOG  distant traffic (%d moving lights)" % len(rows))


def _data_asset(name, folder, cls):
    path = "%s/%s" % (folder, name)
    if EAL.does_asset_exist(path):
        return unreal.load_asset(path)
    f = unreal.DataAssetFactory()
    f.set_editor_property("data_asset_class", cls)
    return ASSET_TOOLS.create_asset(name, folder, cls, f)


def place_traffic(traffic, eas):
    """GTA IV traffic (GTAIVTraffic plugin): road network + vehicle set assets and a manager."""
    if not traffic or not hasattr(unreal, "GTAIVTrafficManager"):
        if traffic:
            say("LOG  traffic skipped: the GTAIVTraffic plugin isn't enabled in this project")
        return
    folder = ROOT + "/Traffic"
    net = _data_asset("DA_GTAIV_RoadNetwork", folder, unreal.GTAIVRoadNetwork)
    net.set_editor_property("nodes", [unreal.Vector(*n) for n in traffic["nodes"]])
    net.set_editor_property("first_link", traffic["first"])
    net.set_editor_property("link_count", traffic["count"])
    net.set_editor_property("link_target", traffic["target"])
    net.set_editor_property("link_lanes", traffic["lanes"])
    EAL.save_loaded_asset(net)
    place_peds(net, eas)
    if not traffic.get("vehicles"):
        return  # roads exported for pedestrians only

    cars = []
    for v in traffic["vehicles"]:
        body = unreal.load_asset("%s/Meshes/%s" % (ROOT, v["body"]))
        if body is None:
            continue
        wheels = []
        for w in v["wheels"]:
            wheels.append(unreal.GTAIVWheel(mesh=unreal.load_asset("%s/Meshes/%s" % (ROOT, w["mesh"])),
                                            offset=unreal.Vector(*w["offset"]), right_side=w["right"],
                                            steers=w["steers"], radius=w["radius"]))
        paints = [unreal.GTAIVPaint(primary=unreal.LinearColor(*c[0], 1), secondary=unreal.LinearColor(*c[1], 1),
                                    tertiary=unreal.LinearColor(*c[2], 1)) for c in v["paints"]]
        cars.append(unreal.GTAIVVehicleDef(name=v["name"], body=body, wheels=wheels, paints=paints,
                                           frequency=v["frequency"], length=v["length"], width=v["width"],
                                           ride_height=v["ride_height"]))
    vs = _data_asset("DA_GTAIV_Vehicles", folder, unreal.GTAIVVehicleSet)
    vs.set_editor_property("vehicles", cars)
    EAL.save_loaded_asset(vs)

    mgr = eas.spawn_actor_from_class(unreal.GTAIVTrafficManager, unreal.Vector(0, 0, 0))
    mgr.set_actor_label("GTAIV_Traffic")
    mgr.set_folder_path("GTAIV")
    mgr.set_editor_property("network", net)
    mgr.set_editor_property("vehicle_set", vs)
    say("LOG  traffic: %d road nodes, %d cars" % (len(traffic["nodes"]), len(cars)))


GASP_NPC = "/Game/Blueprints/SandboxCharacter_CMC"


def place_peds(net, eas):
    """Pedestrians on the sidewalks (GTAIVTraffic plugin): Game Animation Sample characters,
    shown as random GTA IV peds by the GM_GTAIV game mode."""
    if not hasattr(unreal, "GTAIVPedManager"):
        return
    if not (EAL.does_asset_exist(GASP_NPC) and EAL.does_asset_exist(ROOT + "/Characters/GM_GTAIV")):
        return
    mgr = eas.spawn_actor_from_class(unreal.GTAIVPedManager, unreal.Vector(0, 0, 0))
    mgr.set_actor_label("GTAIV_Peds")
    mgr.set_folder_path("GTAIV")
    mgr.set_editor_property("network", net)
    mgr.set_editor_property("ped_class", unreal.load_asset(GASP_NPC).generated_class())
    niko = ROOT + "/Characters/SK_PED_Niko"
    if EAL.does_asset_exist(niko):
        mgr.set_editor_property("player_mesh", unreal.load_asset(niko))  # the player is Niko
    say("LOG  pedestrians: GASP characters on the sidewalks (GTAIV_Peds)")


def place_lights(lights, p0, p1, distant=None):
    """One BP_GTAIV_Lights actor per district; its lights are components under its root,
    so the Blueprint can switch the whole district on/off (day/night)."""
    if not lights:
        return
    mpc = night_collection()
    bp_class = unreal_lights.build(LIGHTS_BP, mpc.get_path_name())
    corona_mat = unreal_lights.build_corona_material(ROOT + "/Materials/M_GTA_Corona", mpc)
    plane = unreal.load_asset("/Engine/BasicShapes/Plane")
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    lib = unreal.SubobjectDataBlueprintFunctionLibrary
    total = sum(len(v) for v in lights.values())
    done = 0
    # big districts are split over several actors: adding components gets slower the more an
    # actor already has, and each actor switches its own lights the same way
    groups = []
    for area, rows in sorted(lights.items()):
        for k in range(0, len(rows), LIGHTS_PER_ACTOR):
            label = "GTAIV_%s_lights" % area + ("_%d" % (k // LIGHTS_PER_ACTOR + 1) if k else "")
            groups.append((label, rows[k:k + LIGHTS_PER_ACTOR]))
    if distant:
        _place_distant_traffic(distant, bp_class, mpc, plane, eas, sds, lib)
    for label, rows in groups:
        actor = eas.spawn_actor_from_class(bp_class, unreal.Vector(0, 0, 0))
        actor.set_actor_label(label)
        actor.set_folder_path("GTAIV/Lights")
        root = actor.get_editor_property("root_component")
        handles = sds.k2_gather_subobject_data_for_instance(actor)
        root_handle = handles[0]
        for h in handles:
            if lib.get_object(lib.get_data(h)) == root:
                root_handle = h
                break
        for spot, x, y, z, dx, dy, dz, r, g, b, rng, inner, outer in rows:
            cls = unreal.SpotLightComponent if spot else unreal.PointLightComponent
            handle, _fail = sds.add_new_subobject(unreal.AddNewSubobjectParams(parent_handle=root_handle, new_class=cls))
            comp = lib.get_object(lib.get_data(handle))
            if comp is None:
                continue
            # setters, not set_editor_property: each editor-property change reruns the Blueprint's
            # construction script over every light added so far (minutes per district)
            comp.set_mobility(unreal.ComponentMobility.MOVABLE)
            comp.set_intensity_units(unreal.LightUnits.CANDELAS)
            comp.set_intensity(1.5 * rng * rng)  # 20 m street lamp: 600 cd
            comp.set_light_color(unreal.LinearColor(r / 255.0, g / 255.0, b / 255.0, 1.0), True)
            comp.set_attenuation_radius(rng * 100.0)
            comp.set_source_radius(5.0)
            comp.set_cast_shadows(False)
            props = {"max_draw_distance": LIGHT_DRAW_DISTANCE, "max_distance_fade_range": 2000.0}
            # GTA (metres, right-handed) -> Unreal (cm, Y mirrored); lights point along +X
            loc = unreal.Vector(x * 100.0, -y * 100.0, z * 100.0)
            rot = unreal.MathLibrary.make_rot_from_x(unreal.Vector(dx, -dy, dz))
            if spot:
                comp.set_outer_cone_angle(min(80.0, max(1.0, outer)))
                comp.set_inner_cone_angle(min(outer, max(0.0, inner)))
            comp.set_editor_properties(props)  # one change notification for the rest
            comp.set_relative_location_and_rotation(loc, rot, False, False)
            done += 1
        _add_coronas(sds, lib, root_handle, rows, plane, corona_mat)
        progress(p0 + (p1 - p0) * done / total)
        say("LOG  lights %s (%d)" % (label, len(rows)))


def build_level(placements, map_path, mode, p0, p1, lights=None, distant=None, traffic=None):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if EAL.does_asset_exist(map_path):
        # re-export: reuse the map, replacing everything in it
        if not les.load_level(map_path):
            raise RuntimeError("Couldn't open existing level %s" % map_path)
        eas.destroy_actors([a for a in eas.get_all_level_actors()
                            if not isinstance(a, (unreal.WorldSettings, unreal.Brush))])
    elif not les.new_level(map_path):
        raise RuntimeError("Couldn't create level %s" % map_path)
    _add_lighting(eas)
    gm_path = ROOT + "/Characters/GM_GTAIV"
    if EAL.does_asset_exist(gm_path):
        # Game Animation Sample project: play as/among GTA IV peds
        ws = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world().get_world_settings()
        ws.set_editor_property("default_game_mode", unreal.load_asset(gm_path).generated_class())
        say("LOG Level game mode: GM_GTAIV")
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    lib = unreal.SubobjectDataBlueprintFunctionLibrary
    meshes = {}
    total = sum(len(v) for v in placements.values())
    done = 0
    for area, rows in sorted(placements.items()):
        if mode == "ACTORS":
            folder = "GTAIV/" + area
            for row in rows:
                sm = meshes.get(row[0]) or meshes.setdefault(row[0], unreal.load_asset("%s/Meshes/%s" % (ROOT, row[0])))
                if not sm:
                    continue
                t = _transform(row)
                # spawn_actor_from_object needs actor factories, which aren't available headless
                a = eas.spawn_actor_from_class(unreal.StaticMeshActor, t.translation, t.rotation.rotator())
                if a:
                    a.static_mesh_component.set_static_mesh(sm)
                    a.set_actor_label(row[0][3:])
                    a.set_folder_path(folder)
                done += 1
                if done % 500 == 0:
                    progress(p0 + (p1 - p0) * done / total)
        else:
            actor = eas.spawn_actor_from_class(unreal.Actor, unreal.Vector(0, 0, 0))
            actor.set_actor_label("GTAIV_" + area)
            handles = sds.k2_gather_subobject_data_for_instance(actor)
            root_handle = handles[0]
            by_mesh = {}
            for row in rows:
                by_mesh.setdefault(row[0], []).append(row)
            for mesh_name, mrows in sorted(by_mesh.items()):
                sm = meshes.get(mesh_name) or meshes.setdefault(mesh_name, unreal.load_asset("%s/Meshes/%s" % (ROOT, mesh_name)))
                if not sm:
                    continue
                params = unreal.AddNewSubobjectParams(parent_handle=root_handle,
                                                      new_class=unreal.HierarchicalInstancedStaticMeshComponent)
                handle, fail = sds.add_new_subobject(params)
                comp = lib.get_object(lib.get_data(handle))
                sds.rename_subobject(handle, unreal.Text(mesh_name[3:]))
                comp.set_static_mesh(sm)
                comp.add_instances([_transform(r) for r in mrows], False, True)
                done += len(mrows)
            progress(p0 + (p1 - p0) * done / total)
        say("LOG  placed %s (%d)" % (area, len(rows)))
    place_lights(lights, p0 + (p1 - p0) * 0.7, p1, distant)
    place_traffic(traffic, eas)
    les.save_current_level()


def main():
    t0 = time.time()
    job = load_job()
    export_dir = job["export_dir"]
    data = json.load(open(os.path.join(export_dir, "gta4_unreal.json"), encoding="utf-8"))
    stage = job.get("stage", "all")
    # progress: textures 0-5%, materials 5-10%, meshes 10-85%, level 85-100%
    if stage in ("all", "assets"):
        if job.get("first_pass", True):
            normal_maps = sorted({m["bump"] for m in data["materials"].values() if m.get("bump")})
            say("LOG Importing into %s" % ROOT)
            import_textures(export_dir, data["textures"], normal_maps, 0.0, 0.05)
            create_materials(data["materials"], 0.05, 0.10)
        left = import_meshes(export_dir, data["batches"], data.get("batch_meshes", {}),
                             job.get("nanite", False), job.get("collision", True),
                             job.get("max_batches", 10 ** 9), 0.10, 0.85)
        if left:
            say("MORE %d" % left)
            return
        if stage == "assets":
            say("ASSETS_DONE")
            return
    import_characters(export_dir, data.get("characters"), 0.85, 0.87)
    map_path = "%s/Maps/%s" % (ROOT, job.get("map_name", "LibertyCity"))
    say("LOG Building level %s" % map_path)
    build_level(data["placements"], map_path, job.get("mode", "INSTANCES"), 0.85, 1.0,
                lights=data.get("lights") if job.get("lights", True) else None,
                distant=data.get("distant") if job.get("lights", True) else None,
                traffic=data.get("traffic"))
    say("DONE %d" % sum(len(v) for v in data["placements"].values()))
    say("LOG Finished in %.0fs. Open %s in the editor." % (time.time() - t0, map_path))


try:
    main()
except Exception as e:
    unreal.log_error(traceback.format_exc())
    say("FAILED %s" % e)
