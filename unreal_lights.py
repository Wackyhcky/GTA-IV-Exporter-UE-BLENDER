"""Builds BP_GTAIV_Lights inside Unreal Editor (imported by unreal_runner.py).

One instance per district holds that district's lights as components under its
root. The Blueprint switches them all at once by changing the root's visibility:

    Variables (editable per instance in the Details panel)
        LightsOn      bool   lights visible (also applied in the editor)
        AutoDayNight  bool   follow the level's directional light (the sun)
        NightPitch    float  sun pitch above which it counts as night (default -3:
                             the sun is within 3 degrees of the horizon or below)
    Events (callable from the Level Blueprint or any other Blueprint)
        TurnLightsOn, TurnLightsOff, ApplyLights
Switching also sets the "Night" value of MPC_GTAIV, which makes GTA's night-only
glowing materials (lamp bulbs, lit windows) glow.
"""

import unreal

BEL = unreal.BlueprintEditorLibrary
GE = unreal.BlueprintGraphEditor
PL = unreal.BlueprintGraphPinLibrary
EAL = unreal.EditorAssetLibrary

IN = unreal.EdGraphPinDirection.EGPD_INPUT
OUT = unreal.EdGraphPinDirection.EGPD_OUTPUT

F_ROOT = "/Script/Engine.Actor:K2_GetRootComponent"
F_VIS = "/Script/Engine.SceneComponent:SetVisibility"
F_FIND = "/Script/Engine.GameplayStatics:GetActorOfClass"
F_ROT = "/Script/Engine.Actor:K2_GetActorRotation"
F_BREAK = "/Script/Engine.KismetMathLibrary:BreakRotator"
F_GREATER = "/Script/Engine.KismetMathLibrary:Greater_DoubleDouble"
F_NOTEQ = "/Script/Engine.KismetMathLibrary:NotEqual_BoolBool"
F_VALID = "/Script/Engine.KismetSystemLibrary:IsValid"
F_TICKINT = "/Script/Engine.Actor:SetActorTickInterval"
F_MPC_SET = "/Script/Engine.KismetMaterialLibrary:SetScalarParameterValue"
F_BOOL2F = "/Script/Engine.KismetMathLibrary:Conv_BoolToDouble"
BUILD_VERSION = 2  # bump when the graph changes; older Blueprints are rebuilt in place


def _pin(node, name, direction):
    for p in BEL.list_all_pins(node, direction):
        if str(PL.get_pin_name(p)).lower() == name.lower():
            return p
    names = [str(PL.get_pin_name(p)) for p in BEL.list_all_pins(node, direction)]
    raise RuntimeError("pin %s not found on %s (has %s)" % (name, node.get_name(), names))


def _link(a, a_pin, b, b_pin):
    """Connect output pin of a to input pin of b."""
    if not PL.try_create_connection(_pin(a, a_pin, OUT), _pin(b, b_pin, IN)):
        raise RuntimeError("couldn't connect %s.%s -> %s.%s" % (a.get_name(), a_pin, b.get_name(), b_pin))


def _value(node, pin, value):
    PL.set_pin_value(_pin(node, pin, IN), value)


def _at(node, x, y):
    try:
        node.set_node_pos(unreal.Vector2D(x, y))
    except TypeError:
        node.set_node_pos(unreal.IntPoint(x, y))
    return node


class _Graph:
    def __init__(self, graph, mpc_path=None):
        self.ed = GE.get_graph_editor(graph)
        self.mpc_path = mpc_path

    def call(self, path, x, y):
        return _at(self.ed.add_call_function_node(path), x, y)

    def get(self, var, x, y):
        return _at(self.ed.add_get_member_variable_node(var, ""), x, y)

    def set(self, var, x, y):
        return _at(self.ed.add_set_member_variable_node(var, ""), x, y)

    def branch(self, x, y):
        return _at(self.ed.add_branch_node(), x, y)

    def event(self, name, x, y):
        return _at(self.ed.add_custom_event_node(name), x, y)

    def apply_visibility(self, after, after_pin, value_node, value_pin, x, y):
        """after -> SetVisibility(GetRootComponent(), value, propagate=true)."""
        root = self.call(F_ROOT, x, y + 140)
        vis = self.call(F_VIS, x + 300, y)
        _link(root, "ReturnValue", vis, "self")
        _link(after, after_pin, vis, "execute")
        if value_node is None:
            _value(vis, "bNewVisibility", value_pin)
        else:
            _link(value_node, value_pin, vis, "bNewVisibility")
        _value(vis, "bPropagateToChildren", "true")
        if not self.mpc_path:
            return vis
        # ...then MPC_GTAIV.Night = value (night-only glowing materials)
        mpc = self.call(F_MPC_SET, x + 620, y)
        _link(vis, "then", mpc, "execute")
        _value(mpc, "Collection", self.mpc_path)
        _value(mpc, "ParameterName", "Night")
        if value_node is None:
            _value(mpc, "ParameterValue", "1.0" if value_pin == "true" else "0.0")
        else:
            conv = self.call(F_BOOL2F, x + 400, y + 220)
            _link(value_node, value_pin, conv, "InBool")
            _link(conv, "ReturnValue", mpc, "ParameterValue")
        return mpc


def _clear_default_events(graph):
    """New actor Blueprints come with placeholder BeginPlay/Overlap/Tick nodes; remove them
    so the events added below are the only ones."""
    ed = GE.get_graph_editor(graph)
    ghosts = [n for n in ed.list_all_nodes() if isinstance(n, unreal.K2Node_Event)]
    if ghosts:
        ed.remove_nodes(ghosts)


def _clear_graphs(bp):
    """Remove everything this module generated, ready for a rebuild."""
    eg = BEL.find_event_graph(bp)
    if eg is not None:
        ed = GE.get_graph_editor(eg)
        nodes = list(ed.list_all_nodes())
        if nodes:
            ed.remove_nodes(nodes)
    cs = BEL.find_graph(bp, "UserConstructionScript")
    if cs is not None:
        ed = GE.get_graph_editor(cs)
        nodes = [n for n in ed.list_all_nodes() if not isinstance(n, unreal.K2Node_FunctionEntry)]
        if nodes:
            ed.remove_nodes(nodes)


def _event_node(bp, graph, name, pos):
    return BEL.add_event_override(bp, name, unreal.IntPoint(*pos))


def build(path, mpc_path=None):
    """Create (or upgrade in place) the Blueprint at `path`; returns its generated class.
    Upgrading keeps the asset, so lights actors already in levels keep working."""
    fresh = not EAL.does_asset_exist(path)
    bp = BEL.create_blueprint_asset_with_parent(path, unreal.Actor) if fresh else unreal.load_asset(path)
    if not fresh:
        cdo = unreal.get_default_object(bp.generated_class())
        try:
            if cdo.get_editor_property("BuildVersion") >= BUILD_VERSION:
                return bp.generated_class()  # current; keeps any edits made to it
        except Exception:
            pass
        _clear_graphs(bp)
    names = {str(n) for n in BEL.list_member_variable_names(bp)}
    for name, kind in (("LightsOn", "bool"), ("AutoDayNight", "bool"), ("NightPitch", "real"),
                       ("BuildVersion", "int")):
        if name not in names:
            BEL.add_member_variable(bp, name, BEL.get_basic_type_by_name(kind))
        if name != "BuildVersion":
            BEL.set_blueprint_variable_instance_editable(bp, name, True)
            BEL.set_blueprint_variable_category(bp, name, "GTA IV Lights")
    BEL.compile_blueprint(bp)

    eg = BEL.find_event_graph(bp)
    _clear_default_events(eg)
    g = _Graph(eg, mpc_path)

    # TurnLightsOn / TurnLightsOff / ApplyLights
    for i, (name, value) in enumerate((("TurnLightsOn", "true"), ("TurnLightsOff", "false"))):
        y = 600 + i * 260
        ev = g.event(name, 0, y)
        s = g.set("LightsOn", 260, y)
        _value(s, "LightsOn", value)
        _link(ev, "then", s, "execute")
        g.apply_visibility(s, "then", None, value, 560, y)
    ev = g.event("ApplyLights", 0, 1120)
    g.apply_visibility(ev, "then", g.get("LightsOn", 260, 1240), "LightsOn", 560, 1120)

    # BeginPlay: check twice a second instead of every frame, then apply
    bp_ev = _event_node(bp, eg, "ReceiveBeginPlay", (0, 0))
    _at(bp_ev, 0, 0)
    ti = g.call(F_TICKINT, 260, 0)
    _value(ti, "TickInterval", "0.5")
    _link(bp_ev, "then", ti, "execute")
    g.apply_visibility(ti, "then", g.get("LightsOn", 300, 160), "LightsOn", 600, 0)

    # Tick: if AutoDayNight and a sun exists, switch when night/day changes
    tk = _event_node(bp, eg, "ReceiveTick", (0, 1500))
    _at(tk, 0, 1500)
    b_auto = g.branch(260, 1500)
    _link(tk, "then", b_auto, "execute")
    _link(g.get("AutoDayNight", 60, 1640), "AutoDayNight", b_auto, "Condition")
    find = g.call(F_FIND, 260, 1700)
    _value(find, "ActorClass", "/Script/Engine.DirectionalLight")
    valid = g.call(F_VALID, 560, 1700)
    _link(find, "ReturnValue", valid, "Object")
    b_sun = g.branch(560, 1500)
    _link(b_auto, "then", find, "execute")  # GetActorOfClass isn't pure: it needs the exec wire
    _link(find, "then", b_sun, "execute")
    _link(valid, "ReturnValue", b_sun, "Condition")
    rot = g.call(F_ROT, 560, 1860)
    _link(find, "ReturnValue", rot, "self")
    brk = g.call(F_BREAK, 820, 1860)
    _link(rot, "ReturnValue", brk, "InRot")
    night = g.call(F_GREATER, 1080, 1860)
    _link(brk, "Pitch", night, "A")
    _link(g.get("NightPitch", 820, 2020), "NightPitch", night, "B")
    changed = g.call(F_NOTEQ, 1320, 1700)
    _link(night, "ReturnValue", changed, "A")
    _link(g.get("LightsOn", 1080, 1700), "LightsOn", changed, "B")
    b_chg = g.branch(1560, 1500)
    _link(b_sun, "then", b_chg, "execute")
    _link(changed, "ReturnValue", b_chg, "Condition")
    s = g.set("LightsOn", 1800, 1500)
    _link(b_chg, "then", s, "execute")
    _link(night, "ReturnValue", s, "LightsOn")
    g.apply_visibility(s, "then", g.get("LightsOn", 1840, 1660), "LightsOn", 2100, 1500)

    # Construction script: the LightsOn checkbox also works in the editor
    cs_graph = BEL.find_graph(bp, "UserConstructionScript")
    if cs_graph is not None:
        cs = _Graph(cs_graph, mpc_path)
        entry = [n for n in cs.ed.list_all_nodes() if isinstance(n, unreal.K2Node_FunctionEntry)]
        if entry:
            cs.apply_visibility(entry[0], "then", cs.get("LightsOn", 200, 160), "LightsOn", 300, 0)

    if not BEL.compile_blueprint(bp):
        bad = [str(n.get_node_title()) + ": " + str(n.error_msg) for n in g.ed.list_nodes_with_errors()]
        raise RuntimeError("BP_GTAIV_Lights failed to compile: %s" % bad)
    # defaults go on the class default object, after the last compile
    cdo = unreal.get_default_object(bp.generated_class())
    if fresh:
        cdo.set_editor_property("LightsOn", True)
        cdo.set_editor_property("AutoDayNight", True)
        cdo.set_editor_property("NightPitch", -3.0)
    cdo.set_editor_property("BuildVersion", BUILD_VERSION)
    bp.modify()
    EAL.save_loaded_asset(bp, only_if_is_dirty=False)
    return bp.generated_class()


# ---------------------------------------------------------------- coronas
MAT = unreal.MaterialEditingLibrary


def _expr(mat, cls, x, y, **props):
    e = MAT.create_material_expression(mat, cls, x, y)
    for k, v in props.items():
        e.set_editor_property(k, v)
    return e


def _wire(a, a_out, b, b_in):
    MAT.connect_material_expressions(a, a_out, b, b_in)


def build_corona_material(path, mpc, moving=False):
    """GTA-style corona: a soft glowing dot at every light, camera-facing, with a minimum
    on-screen size so distant lights still read as points, only at night.

    Instances (unit plane, one per light) carry per-instance custom data:
    [0..2] = colour (linear), [3] = corona size in cm.
    moving=True makes GTA's distant traffic: [4..6] = road segment vector (cm), [7] = phase;
    the dot slides along the segment at Speed and only shows beyond NearFade."""
    if EAL.does_asset_exist(path):
        return unreal.load_asset(path)
    folder, name = path.rsplit("/", 1)
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, folder, unreal.Material,
                                                                  unreal.MaterialFactoryNew())
    mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
    mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_ADDITIVE)
    mat.set_editor_property("two_sided", True)
    mat.set_editor_property("used_with_instanced_static_meshes", True)

    # quad corner offset from the UVs: (-0.5..0.5, -0.5..0.5)
    uv = _expr(mat, unreal.MaterialExpressionTextureCoordinate, -1800, 0)
    half = _expr(mat, unreal.MaterialExpressionConstant2Vector, -1800, 100, r=0.5, g=0.5)
    c = _expr(mat, unreal.MaterialExpressionSubtract, -1600, 0)
    _wire(uv, "", c, "A")
    _wire(half, "", c, "B")
    cx = _expr(mat, unreal.MaterialExpressionComponentMask, -1400, -60, r=True, g=False, b=False, a=False)
    cy = _expr(mat, unreal.MaterialExpressionComponentMask, -1400, 40, r=False, g=True, b=False, a=False)
    _wire(c, "", cx, "")
    _wire(c, "", cy, "")

    # size: max(corona size, distance * MinScreenSize)
    pivot = _expr(mat, unreal.MaterialExpressionObjectPositionWS, -1800, 300)
    if moving:
        seg = _expr(mat, unreal.MaterialExpressionPerInstanceCustomData3Vector, -2600, 300, data_index=4)
        phase = _expr(mat, unreal.MaterialExpressionPerInstanceCustomData, -2600, 420, data_index=7)
        zero3 = _expr(mat, unreal.MaterialExpressionConstant3Vector, -2600, 520, constant=unreal.LinearColor(0, 0, 0, 0))
        seg_len = _expr(mat, unreal.MaterialExpressionDistance, -2400, 450)
        _wire(seg, "", seg_len, "A")
        _wire(zero3, "", seg_len, "B")
        speed = _expr(mat, unreal.MaterialExpressionScalarParameter, -2600, 620, parameter_name="Speed",
                      default_value=1400.0)  # cm/s (about 50 km/h)
        t = _expr(mat, unreal.MaterialExpressionTime, -2600, 720)
        travelled = _expr(mat, unreal.MaterialExpressionMultiply, -2400, 650)
        _wire(t, "", travelled, "A")
        _wire(speed, "", travelled, "B")
        along = _expr(mat, unreal.MaterialExpressionDivide, -2250, 550)
        _wire(travelled, "", along, "A")
        _wire(seg_len, "", along, "B")
        along_p = _expr(mat, unreal.MaterialExpressionAdd, -2100, 500)
        _wire(along, "", along_p, "A")
        _wire(phase, "", along_p, "B")
        frac = _expr(mat, unreal.MaterialExpressionFrac, -1950, 500)
        _wire(along_p, "", frac, "")
        offset = _expr(mat, unreal.MaterialExpressionMultiply, -1950, 380)
        _wire(seg, "", offset, "A")
        _wire(frac, "", offset, "B")
        moved = _expr(mat, unreal.MaterialExpressionAdd, -1800, 380)
        _wire(pivot, "", moved, "A")
        _wire(offset, "", moved, "B")
        pivot = moved
    cam = _expr(mat, unreal.MaterialExpressionCameraPositionWS, -1800, 400)
    dist = _expr(mat, unreal.MaterialExpressionDistance, -1600, 350)
    _wire(cam, "", dist, "A")
    _wire(pivot, "", dist, "B")
    base = _expr(mat, unreal.MaterialExpressionPerInstanceCustomData, -1600, 500, data_index=3,
                 const_default_value=60.0)
    screen = _expr(mat, unreal.MaterialExpressionScalarParameter, -1600, 600, parameter_name="MinScreenSize",
                   default_value=0.005)
    far = _expr(mat, unreal.MaterialExpressionMultiply, -1400, 450)
    _wire(dist, "", far, "A")
    _wire(screen, "", far, "B")
    size = _expr(mat, unreal.MaterialExpressionMax, -1200, 500)
    _wire(base, "", size, "A")
    _wire(far, "", size, "B")

    # camera-facing quad: offset = (right * cx - up * cy) * size, pulled toward the camera so
    # the dot isn't hidden inside the lamp's own glass
    right_v = _expr(mat, unreal.MaterialExpressionConstant3Vector, -1600, 800, constant=unreal.LinearColor(1, 0, 0, 0))
    up_v = _expr(mat, unreal.MaterialExpressionConstant3Vector, -1600, 900, constant=unreal.LinearColor(0, -1, 0, 0))
    right = _expr(mat, unreal.MaterialExpressionTransform, -1400, 800,
                  transform_source_type=unreal.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_VIEW,
                  transform_type=unreal.MaterialVectorCoordTransform.TRANSFORM_WORLD)
    up = _expr(mat, unreal.MaterialExpressionTransform, -1400, 900,
               transform_source_type=unreal.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_VIEW,
               transform_type=unreal.MaterialVectorCoordTransform.TRANSFORM_WORLD)
    _wire(right_v, "", right, "")
    _wire(up_v, "", up, "")
    rx = _expr(mat, unreal.MaterialExpressionMultiply, -1200, 800)
    _wire(right, "", rx, "A")
    _wire(cx, "", rx, "B")
    uy = _expr(mat, unreal.MaterialExpressionMultiply, -1200, 900)
    _wire(up, "", uy, "A")
    _wire(cy, "", uy, "B")
    quad = _expr(mat, unreal.MaterialExpressionAdd, -1000, 850)
    _wire(rx, "", quad, "A")
    _wire(uy, "", quad, "B")
    quad_s = _expr(mat, unreal.MaterialExpressionMultiply, -850, 850)
    _wire(quad, "", quad_s, "A")
    _wire(size, "", quad_s, "B")
    to_cam = _expr(mat, unreal.MaterialExpressionSubtract, -1400, 1050)
    _wire(cam, "", to_cam, "A")
    _wire(pivot, "", to_cam, "B")
    to_cam_n = _expr(mat, unreal.MaterialExpressionNormalize, -1200, 1050)
    _wire(to_cam, "", to_cam_n, "")
    pull = _expr(mat, unreal.MaterialExpressionMultiply, -1000, 1050, const_b=40.0)
    _wire(to_cam_n, "", pull, "A")
    wpos = _expr(mat, unreal.MaterialExpressionWorldPosition, -1000, 1200)
    rel = _expr(mat, unreal.MaterialExpressionSubtract, -850, 1150)
    _wire(pivot, "", rel, "A")
    _wire(wpos, "", rel, "B")
    wpo1 = _expr(mat, unreal.MaterialExpressionAdd, -700, 900)
    _wire(quad_s, "", wpo1, "A")
    _wire(pull, "", wpo1, "B")
    wpo = _expr(mat, unreal.MaterialExpressionAdd, -550, 1000)
    _wire(wpo1, "", wpo, "A")
    _wire(rel, "", wpo, "B")
    MAT.connect_material_property(wpo, "", unreal.MaterialProperty.MP_WORLD_POSITION_OFFSET)

    # glow: colour * soft radial falloff * Intensity * Night
    zero = _expr(mat, unreal.MaterialExpressionConstant2Vector, -1400, -300, r=0.0, g=0.0)
    r = _expr(mat, unreal.MaterialExpressionDistance, -1200, -250)
    _wire(c, "", r, "A")
    _wire(zero, "", r, "B")
    r2 = _expr(mat, unreal.MaterialExpressionMultiply, -1050, -250, const_b=2.0)
    _wire(r, "", r2, "A")
    falloff = _expr(mat, unreal.MaterialExpressionOneMinus, -900, -250)
    _wire(r2, "", falloff, "")
    sat = _expr(mat, unreal.MaterialExpressionSaturate, -750, -250)
    _wire(falloff, "", sat, "")
    soft = _expr(mat, unreal.MaterialExpressionPower, -600, -250, const_exponent=1.6)
    _wire(sat, "", soft, "Base")
    color = _expr(mat, unreal.MaterialExpressionPerInstanceCustomData3Vector, -900, -450, data_index=0)
    inten = _expr(mat, unreal.MaterialExpressionScalarParameter, -900, -100, parameter_name="Intensity",
                  default_value=8.0)
    night = _expr(mat, unreal.MaterialExpressionCollectionParameter, -900, 0, collection=mpc, parameter_name="Night")
    e1 = _expr(mat, unreal.MaterialExpressionMultiply, -450, -400)
    _wire(color, "", e1, "A")
    _wire(soft, "", e1, "B")
    e2 = _expr(mat, unreal.MaterialExpressionMultiply, -300, -300)
    _wire(e1, "", e2, "A")
    _wire(inten, "", e2, "B")
    e3 = _expr(mat, unreal.MaterialExpressionMultiply, -150, -200)
    _wire(e2, "", e3, "A")
    _wire(night, "", e3, "B")
    out = e3
    if moving:
        # only in the distance, like GTA (there's no real traffic up close)
        near = _expr(mat, unreal.MaterialExpressionScalarParameter, -600, 100, parameter_name="NearFade",
                     default_value=12000.0)
        span = _expr(mat, unreal.MaterialExpressionScalarParameter, -600, 200, parameter_name="NearFadeRange",
                     default_value=8000.0)
        dn = _expr(mat, unreal.MaterialExpressionSubtract, -450, 100)
        _wire(dist, "", dn, "A")
        _wire(near, "", dn, "B")
        dd = _expr(mat, unreal.MaterialExpressionDivide, -300, 100)
        _wire(dn, "", dd, "A")
        _wire(span, "", dd, "B")
        fade = _expr(mat, unreal.MaterialExpressionSaturate, -150, 100)
        _wire(dd, "", fade, "")
        out = _expr(mat, unreal.MaterialExpressionMultiply, 0, -150)
        _wire(e3, "", out, "A")
        _wire(fade, "", out, "B")
    MAT.connect_material_property(out, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    MAT.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return mat
