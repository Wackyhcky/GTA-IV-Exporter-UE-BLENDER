"""Hooks GTA IV peds into the Game Animation Sample (GASP) inside Unreal Editor
(imported by unreal_runner.py). Nothing of GASP's own is changed; everything is new under
/Game/GTAIV/Characters:

    Rig/IK_GTAIV_Ped          IK Rig for the shared GTA ped skeleton, with chains named
                              like GASP's IK_UEFN_Mannequin so they map automatically
    Rig/RTG_UEFN_to_GTAIV     retargeter: GASP (UEFN mannequin) animation -> GTA peds
    ABP_GTAIV_Retarget        copy of GASP's ABP_GenericRetarget that also knows the GTA
                              retargeter
    BP_GTAIV_Ped              copy of GASP's BP_UE4_Mannequin showing a GTA ped; picks a
                              random ped from PedMeshes each time it's spawned. Use it as a
                              GASP visual override (it copies the pose of the character
                              it's attached to).
"""

import unreal

BEL = unreal.BlueprintEditorLibrary
GE = unreal.BlueprintGraphEditor
PL = unreal.BlueprintGraphPinLibrary
EAL = unreal.EditorAssetLibrary
AT = unreal.AssetToolsHelpers.get_asset_tools()
IN = unreal.EdGraphPinDirection.EGPD_INPUT
OUT = unreal.EdGraphPinDirection.EGPD_OUTPUT

UEFN_RIG = "/Game/Characters/UEFN_Mannequin/Rigs/IK_UEFN_Mannequin"
UEFN_MESH = "/Game/Characters/UEFN_Mannequin/Meshes/SKM_UEFN_Mannequin"
GASP_ABP = "/Game/Blueprints/RetargetedCharacters/ABP_GenericRetarget"
GASP_BP = "/Game/Blueprints/RetargetedCharacters/BP_UE4_Mannequin"
GASP_RTG = "/Game/Characters/UE4_Mannequin/Rigs/RTG_UEFN_to_UE4_Mannequin"  # template op stack
RTG_NAME = "RTG_UEFN_to_GTAIV"
BUILD_VERSION = 1

CHAINS = [  # (UEFN chain name, start bone, end bone, IK goal)
    ("Root", "Root", "Root", None),
    ("Spine", "Char_Spine", "Char_Spine3", None),
    ("Neck", "Char_Neck", "Char_Neck", None),
    ("Head", "Char_Head", "Char_Head", None),
]
for _side, _s in (("Left", "L"), ("Right", "R")):
    CHAINS += [
        (_side + "Leg", "Char_%s_Thigh" % _s, "Char_%s_Foot" % _s, _side + "FootIK"),
        (_side + "Toe", "Char_%s_Toe" % _s, "Char_%s_Toe" % _s, None),
        (_side + "Clavicle", "Char_%s_Clavicle" % _s, "Char_%s_Clavicle" % _s, None),
        (_side + "Arm", "Char_%s_UpperArm" % _s, "Char_%s_Hand" % _s, _side + "HandIK"),
        (_side + "Thumb", "Char_%s_Finger0" % _s, "Char_%s_Finger02" % _s, None),
        (_side + "Index", "Char_%s_Finger1" % _s, "Char_%s_Finger12" % _s, None),
        (_side + "Middle", "Char_%s_Finger2" % _s, "Char_%s_Finger22" % _s, None),
        (_side + "Ring", "Char_%s_Finger3" % _s, "Char_%s_Finger32" % _s, None),
    ]


def gasp_present():
    return all(EAL.does_asset_exist(p) for p in (UEFN_RIG, UEFN_MESH, GASP_ABP, GASP_BP))


def build_retargeter(folder, ped_mesh, log=print):
    """IK_GTAIV_Ped + RTG_UEFN_to_GTAIV in folder/Rig (kept if they already exist)."""
    rig_dir = folder + "/Rig"
    rig_path, rtg_path = rig_dir + "/IK_GTAIV_Ped", rig_dir + "/" + RTG_NAME
    if EAL.does_asset_exist(rig_path) and EAL.does_asset_exist(rtg_path):
        return unreal.load_asset(rtg_path)
    mesh = unreal.load_asset(ped_mesh)
    rig = unreal.load_asset(rig_path) if EAL.does_asset_exist(rig_path) else \
        AT.create_asset("IK_GTAIV_Ped", rig_dir, unreal.IKRigDefinition, unreal.IKRigDefinitionFactory())
    c = unreal.IKRigController.get_controller(rig)
    if not c.get_retarget_chains():
        c.set_skeletal_mesh(mesh)
        # Char (not the pelvis) is the parent of both the legs and the spine
        c.set_retarget_root("Char")
        si = c.add_solver("/Script/IKRig.IKRigFullBodyIKSolver")
        if si >= 0:
            c.set_start_bone("Char_Pelvis", si)
        for name, a, b, goal in CHAINS:
            if goal:
                c.add_new_goal(goal, b)
                if si >= 0:
                    c.connect_goal_to_solver(goal, si)
            c.add_retarget_chain(name, a, b, goal or "")
        EAL.save_loaded_asset(rig)

    # Start from a copy of GASP's own retargeter so ours has the same op stack and tuning
    # (Blend to Source / Offset Goals / Speed Plant, which ABP_GenericRetarget drives).
    if not EAL.does_asset_exist(rtg_path):
        if EAL.does_asset_exist(GASP_RTG):
            EAL.duplicate_asset(GASP_RTG, rtg_path)
        else:
            AT.create_asset(RTG_NAME, rig_dir, unreal.IKRetargeter, unreal.IKRetargetFactory())
    rtg = unreal.load_asset(rtg_path)
    rc = unreal.IKRetargeterController.get_controller(rtg)
    src, tgt = unreal.RetargetSourceOrTarget.SOURCE, unreal.RetargetSourceOrTarget.TARGET
    rc.set_ik_rig(src, unreal.load_asset(UEFN_RIG))
    rc.set_ik_rig(tgt, rig)
    if rc.get_num_retarget_ops() == 0:
        rc.add_default_ops()
    try:
        rc.assign_ik_rig_to_all_ops(tgt, rig)
    except Exception as e:
        log("LOG assign_ik_rig_to_all_ops: %s" % e)
    rc.set_preview_mesh(src, unreal.load_asset(UEFN_MESH))
    rc.set_preview_mesh(tgt, mesh)
    try:
        rc.reset_chain_settings_in_all_ops()
    except Exception:
        pass
    rc.auto_map_chains(unreal.AutoMapChainType.EXACT, True)
    try:  # drop the template's UE4-mannequin pose offsets
        rc.reset_retarget_pose(rc.get_current_retarget_pose_name(tgt), [], tgt)
    except Exception as e:
        log("LOG reset_retarget_pose: %s" % e)
    rc.auto_align_all_bones(tgt)  # GTA's bind pose is a T-pose, UEFN's an A-pose
    i = rc.get_index_of_op_by_name("Root Motion")
    if i >= 0:
        rm = rc.get_op_controller(i)
        rm.set_source_root_bone("root")
        rm.set_target_root_bone("Root")
        rm.set_target_pelvis_bone("Char")
    for i in range(rc.get_num_retarget_ops()):
        try:
            rc.run_op_initial_setup(i)
        except Exception:
            pass
    EAL.save_loaded_asset(rtg)
    log("LOG Built %s" % rtg_path)
    return rtg


def _pin(node, name, direction):
    for p in BEL.list_all_pins(node, direction):
        if str(PL.get_pin_name(p)).lower() == name.lower():
            return p
    names = [str(PL.get_pin_name(p)) for p in BEL.list_all_pins(node, direction)]
    raise RuntimeError("pin %s not found on %s (has %s)" % (name, node.get_name(), names))


def _link(a, a_pin, b, b_pin):
    if not PL.try_create_connection(_pin(a, a_pin, OUT), _pin(b, b_pin, IN)):
        raise RuntimeError("couldn't connect %s.%s -> %s.%s" % (a.get_name(), a_pin, b.get_name(), b_pin))


def _skeletal_component(bp):
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    lib = unreal.SubobjectDataBlueprintFunctionLibrary
    for h in sds.k2_gather_subobject_data_for_blueprint(bp):
        o = lib.get_object(lib.get_data(h))
        if isinstance(o, unreal.SkeletalMeshComponent):
            return o
    return None


def _build_construction_script(bp, comp_var):
    """Construction script: if PedMeshes isn't empty, show a random one of them."""
    cs = BEL.find_graph(bp, "UserConstructionScript")
    ed = GE.get_graph_editor(cs)
    old = [n for n in ed.list_all_nodes() if not isinstance(n, unreal.K2Node_FunctionEntry)]
    if old:
        ed.remove_nodes(old)
    entry = next(n for n in ed.list_all_nodes() if isinstance(n, unreal.K2Node_FunctionEntry))
    V = unreal.Vector2D
    get_arr = ed.add_get_member_variable_node("PedMeshes", "")
    arr_out = _pin(get_arr, "PedMeshes", OUT)
    length = ed.create_node_from_name("Utilities|Array|IsNotEmpty", V(500, 300), [arr_out])
    rand = ed.create_node_from_name("Utilities|Array|RandomArrayItem", V(500, 120), [arr_out])
    for node in (length, rand):
        if node is None:
            raise RuntimeError("couldn't create array nodes")
        PL.try_create_connection(arr_out, _pin(node, "TargetArray", IN))
    br = ed.add_branch_node()
    _link(entry, "then", br, "execute")
    _link(length, "ReturnValue", br, "Condition")
    comp = ed.add_get_member_variable_node(comp_var, "")
    setm = ed.add_call_function_node("/Script/Engine.SkeletalMeshComponent:SetSkeletalMeshAsset")
    _link(br, "then", setm, "execute")
    _link(comp, comp_var, setm, "self")
    _link(rand, "OutItem", setm, "NewMesh")
    for n, (x, y) in ((get_arr, (250, 200)), (length, (500, 300)), (rand, (500, 120)),
                      (br, (900, 0)), (comp, (900, 200)), (setm, (1150, 0))):
        try:
            n.set_node_pos(unreal.IntPoint(x, y))
        except Exception:
            pass


def build_ped_blueprint(folder, rtg, mesh_paths, log=print):
    """ABP_GTAIV_Retarget + BP_GTAIV_Ped in folder; returns the BP's generated class.
    PedMeshes is refreshed every run, so newly imported peds join the random pool."""
    abp_path, bp_path = folder + "/ABP_GTAIV_Retarget", folder + "/BP_GTAIV_Ped"
    if not EAL.does_asset_exist(abp_path):
        EAL.duplicate_asset(GASP_ABP, abp_path)
    abp = unreal.load_asset(abp_path)
    acdo = unreal.get_default_object(abp.generated_class())
    rmap = acdo.get_editor_property("IKRetargeter_Map")
    if RTG_NAME not in [str(k) for k in rmap.keys()]:
        rmap[RTG_NAME] = rtg
        acdo.set_editor_property("IKRetargeter_Map", rmap)
        BEL.compile_blueprint(abp)
        acdo = unreal.get_default_object(abp.generated_class())
        rmap = acdo.get_editor_property("IKRetargeter_Map")
        if RTG_NAME not in [str(k) for k in rmap.keys()]:  # compile reset the default
            rmap[RTG_NAME] = rtg
            acdo.set_editor_property("IKRetargeter_Map", rmap)
    EAL.save_loaded_asset(abp, only_if_is_dirty=False)

    fresh = not EAL.does_asset_exist(bp_path)
    if fresh:
        EAL.duplicate_asset(GASP_BP, bp_path)
    bp = unreal.load_asset(bp_path)
    meshes = [unreal.load_asset(p) for p in mesh_paths]
    meshes = [m for m in meshes if m is not None]
    names = {str(n) for n in BEL.list_member_variable_names(bp)}
    for name, t in (("PedMeshes", BEL.get_array_type(BEL.get_object_reference_type(unreal.SkeletalMesh))),
                    ("BuildVersion", BEL.get_basic_type_by_name("int"))):
        if name not in names:
            BEL.add_member_variable(bp, name, t)
    BEL.set_blueprint_variable_instance_editable(bp, "PedMeshes", True)
    BEL.set_blueprint_variable_category(bp, "PedMeshes", "GTA IV")
    comp = _skeletal_component(bp)
    comp_var = comp.get_name().replace("_GEN_VARIABLE", "")
    if meshes:
        comp.set_editor_property("skeletal_mesh_asset", meshes[0])
    comp.set_editor_property("anim_class", abp.generated_class())
    comp.set_editor_property("component_tags", [RTG_NAME])
    BEL.compile_blueprint(bp)
    cdo = unreal.get_default_object(bp.generated_class())
    if fresh or cdo.get_editor_property("BuildVersion") < BUILD_VERSION:
        _build_construction_script(bp, comp_var)
        BEL.compile_blueprint(bp)
        cdo = unreal.get_default_object(bp.generated_class())
        cdo.set_editor_property("BuildVersion", BUILD_VERSION)
    cdo.set_editor_property("PedMeshes", meshes)
    EAL.save_loaded_asset(bp, only_if_is_dirty=False)
    log("LOG Built %s with %d peds" % (bp_path, len(meshes)))
    return bp.generated_class()


GASP_GM = "/Game/Blueprints/GM_Sandbox"


def build_game_mode(folder, ped_class, log=print):
    """GM_GTAIV: a child of GASP's GM_Sandbox whose visual-override list starts with
    BP_GTAIV_Ped and which selects it on BeginPlay (DDCvar.VisualOverride 0), so the player
    and every NPC appear as random GTA IV peds. Use it as the level's GameMode Override;
    GASP's cycle-visual-override key still works (index 0 = GTA peds)."""
    if not EAL.does_asset_exist(GASP_GM):
        return None
    path = folder + "/GM_GTAIV"
    fresh = not EAL.does_asset_exist(path)
    bp = BEL.create_blueprint_asset_with_parent(path, unreal.load_asset(GASP_GM).generated_class()) if fresh \
        else unreal.load_asset(path)
    if fresh:
        ed = GE.get_graph_editor(BEL.find_event_graph(bp))
        nodes = list(ed.list_all_nodes())
        parent_call = next((n for n in nodes if isinstance(n, unreal.K2Node_CallParentFunction)
                            and "BeginPlay" in str(n.get_node_title())), None)
        if parent_call is None:
            raise RuntimeError("GM_GTAIV: no Parent: BeginPlay node")
        cmd = ed.add_call_function_node("/Script/Engine.KismetSystemLibrary:ExecuteConsoleCommand")
        _link(parent_call, "then", cmd, "execute")
        PL.set_pin_value(_pin(cmd, "Command", IN), "DDCvar.VisualOverride 0")
        try:
            cmd.set_node_pos(unreal.IntPoint(600, 0))
        except Exception:
            pass
        # (wiring to the default BeginPlay -> Parent: BeginPlay placeholders turns them on)
        BEL.compile_blueprint(bp)
    cdo = unreal.get_default_object(bp.generated_class())
    lst = list(cdo.get_editor_property("VisualOverrides_Soft"))
    ped_path = ped_class.get_path_name()
    lst = [c for c in lst if ped_path not in str(c)]
    cdo.set_editor_property("VisualOverrides_Soft", [ped_class] + lst)
    EAL.save_loaded_asset(bp, only_if_is_dirty=False)
    log("LOG Built %s (GTA peds as GASP visual override)" % path)
    return bp.generated_class()
