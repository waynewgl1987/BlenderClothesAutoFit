# -*- coding: utf-8 -*-
"""
Garment Fit - 衣服一键匹配人物（贴合 / 骨骼姿态校正 / 蒙皮 / 导出 UE5）
Blender 4.2+ / 5.x      面板: 3D 视图右侧栏 (N) -> "Garment Fit"

一键匹配:
  ① 权重传递   逐顶点取身体最近面重心插值 -> 衣服顶点组
  ② 手臂姿态校正 把衣服手臂按骨骼(锁骨/上臂/小臂)对齐到人物手臂:
                每根骨骼算出"衣服簇 -> 身体簇"的旋转+位移, 用衣服自身权重驱动,
                权重是平滑的, 所以跨碎块也不会撕; 可调最大角度/位移/强度
  ③ 防穿模     把插进身体的点沿法线推出去(平滑体素场)
  ④ 绑定        Armature 修改器 + 父级人物骨架(每顶点 4 骨)
  ⑤ 导出        FBX(烘焙缩放, 米制, 共用人物 Skeleton)

尺寸匹配(两个按钮, 统一缩放, 只改尺寸不改比例):
  人物 -> 衣服  把人物(含骨架)缩放到衣服尺寸
  衣服 -> 人物  把衣服几何缩放到人物尺寸
  护手: 缩放前先测"袖口在手臂上的覆盖比例", 夹紧/抬高比例,
        保证袖口停在手腕以内, 不包住手掌(按高度/对角线匹配时尤其重要)

进度: 状态栏进度条 + 面板百分比/阶段。
"""

bl_info = {
    "name": "Garment Fit (衣服一键匹配人物)",
    "author": "OpenCode",
    "version": (1, 7, 7),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Garment Fit",
    "description": "一键把衣服贴合人物、校正手臂姿态、蒙皮、导出 UE FBX；含双向尺寸匹配(护手)",
    "category": "Rigging",
}

import bpy
import bmesh
import math
import numpy as np
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree
from mathutils.interpolate import poly_3d_calc


# ================================================================== 进度
class GF_Progress:
    def __init__(self, total=100, title="处理"):
        self.total = total
        self.title = title
        self.pct = 0.0
        self.wm = bpy.context.window_manager
        try:
            self.wm.progress_begin(0, total)
        except Exception:
            self.wm = None
        self.set(0, "准备")

    def set(self, pct, text=""):
        if self.wm:
            try:
                self.wm.progress_update(pct)
            except Exception:
                pass
        self.pct = pct / max(1, self.total)
        st = getattr(bpy.context.scene, "garment_fit", None)
        if st is not None:
            st.progress = self.pct
            st.stage = text
        overlay_progress(self.pct, ("%s · %s" % (self.title, text)) if text else self.title)
        if text:
            print("[GarmentFit %3d%%] %s" % (pct, text))

    def step(self, pct, text=""):
        self.set(pct, text)
        bpy.context.view_layer.update()

    def end(self, text="完成"):
        self.set(self.total, text)
        overlay_progress(1.0, "%s · %s" % (self.title, text), hide_after=1.6)
        if self.wm:
            try:
                self.wm.progress_end()
            except Exception:
                pass


# ================================================================== 属性
class GF_Settings(bpy.types.PropertyGroup):
    body: bpy.props.PointerProperty(name="人物身体", type=bpy.types.Object)
    garment: bpy.props.PointerProperty(name="衣服", type=bpy.types.Object)
    armature: bpy.props.PointerProperty(name="骨架(可留空)", type=bpy.types.Object)

    auto_scale: bpy.props.BoolProperty(name="自动缩放衣服到人物尺寸", default=True)
    match_mode: bpy.props.EnumProperty(
        name="匹配方向",
        items=[("GARMENT", "衣服匹配人物", "改衣服的几何, 让人物穿得进去"),
               ("BODY", "人物匹配衣服", "改人物的尺寸/手臂姿势, 让身体落进衣服里")],
        default="GARMENT")
    bake_body: bpy.props.BoolProperty(name="把人物姿势烘焙成静止姿势", default=True)
    do_weights: bpy.props.BoolProperty(name="传递蒙皮权重", default=True)
    limit_weights: bpy.props.IntProperty(name="每顶点骨骼数", default=4, min=1, max=8)

    scale_metric: bpy.props.EnumProperty(
        name="缩放基准",
        items=[("DIAGONAL", "整体尺寸(对角线)", "用包围盒对角线比；整体体积对得上，默认"),
               ("HEIGHT", "按高度", "用世界包围盒高度比；只保证高度一致"),
               ("ARM", "按手臂(肩→腕)", "用肩到手腕的长度比；袖口正好落在手腕，不盖手掌")],
        default="DIAGONAL")
    guard_cuff: bpy.props.BoolProperty(
        name="护手(袖口不盖手掌)", default=True,
        description="缩放时限制比例，让袖口停在手腕以内，不包住手掌")
    align_positions: bpy.props.BoolProperty(
        name="同时对齐位置(包围盒中心)", default=True,
        description="缩放后把两个物体的包围盒中心对到同一点；关闭则只改尺寸，位置不动")
    scale_info: bpy.props.StringProperty(name="上次尺寸匹配", default="")

    trim_hands: bpy.props.BoolProperty(name="手(手腕/手指)", default=True)
    trim_feet: bpy.props.BoolProperty(name="脚(脚踝/脚趾)", default=True)
    trim_head: bpy.props.BoolProperty(name="头/脖子", default=False)
    trim_margin: bpy.props.FloatProperty(name="判定距离(mm)", default=10.0, min=1.0, max=100.0, precision=0)
    trim_cut: bpy.props.BoolProperty(name="切断腕/踝以外(袖口/裤脚)", default=True)
    trim_cut_radius: bpy.props.FloatProperty(name="切断半径(cm)", default=15.0, min=3.0, max=30.0, precision=1)
    trim_info: bpy.props.StringProperty(name="上次清理", default="")

    do_arm_pose: bpy.props.BoolProperty(name="校正手臂姿态(POSE不匹配)", default=True)
    weight_smooth: bpy.props.IntProperty(name="权重场平滑(防拉裂)", default=2, min=0, max=6)
    arm_max_rot: bpy.props.FloatProperty(name="最大旋转角度", default=12.0, min=0.0, max=45.0)
    arm_strength: bpy.props.FloatProperty(name="总强度", default=1.0, min=0.0, max=1.5)
    arm_str_cl: bpy.props.FloatProperty(name="肩部强度(护肩保护)", default=0.3, min=0.0, max=1.5)
    arm_str_up: bpy.props.FloatProperty(name="上臂强度", default=0.95, min=0.0, max=1.5)
    arm_str_lo: bpy.props.FloatProperty(name="小臂强度(护腕保护)", default=0.7, min=0.0, max=1.5)
    arm_max_move: bpy.props.FloatProperty(name="最大位移(cm)", default=5.0, min=0.0, max=15.0)
    arm_iterations: bpy.props.IntProperty(name="迭代次数", default=2, min=1, max=5)
    arm_rigid_chunks: bpy.props.BoolProperty(name="按碎块刚性（无裂缝但会拉开薄片）", default=False)
    cuff_tighten: bpy.props.FloatProperty(
        name="袖口收紧(大袖)", default=0.0, min=0.0, max=1.0, precision=2,
        description="默认 0 = 完全不改袖口形状；只有当前臂从宽袖口里露出来时才往上调（会改变袖口形状）")

    do_fit: bpy.props.BoolProperty(name="防穿模(推出穿插)", default=True)
    fit_gap: bpy.props.FloatProperty(name="布料间隙", default=0.008, min=0.0, max=0.05,
                                     precision=3, subtype="DISTANCE", unit="LENGTH")
    fit_max_push: bpy.props.FloatProperty(name="最大推出距离", default=0.05, min=0.005,
                                          max=0.15, precision=3, subtype="DISTANCE", unit="LENGTH")

    do_cloth: bpy.props.BoolProperty(name="布料预处理(焊接+低模+遮罩)", default=True)
    weld_mm: bpy.props.FloatProperty(name="焊接距离(mm)", default=0.5, min=0.0, max=5.0, precision=2)
    sim_tris: bpy.props.IntProperty(name="Sim网格面数", default=12000, min=500, max=200000, step=500)
    make_sim_mesh: bpy.props.BoolProperty(name="生成低模Sim网格", default=True)
    pin_top: bpy.props.FloatProperty(name="领口固定高度(cm)", default=8.0, min=0.0, max=30.0, precision=1)
    pin_waist_ratio: bpy.props.FloatProperty(name="腰带高度比例", default=0.72, min=0.0, max=1.0, precision=2)
    pin_shoulder: bpy.props.FloatProperty(name="肩部固定半径(cm)", default=12.0, min=0.0, max=40.0, precision=1)
    mask_smooth: bpy.props.IntProperty(name="遮罩平滑", default=2, min=0, max=6)

    export_sim: bpy.props.BoolProperty(name="带上 Sim 网格(布料用)", default=True)
    export_split_sim: bpy.props.BoolProperty(
        name="Sim 单独一个 FBX (UE 必须)", default=True)
    export_scope: bpy.props.EnumProperty(
        name="导出内容",
        items=[("GARMENT", "只衣服", "衣服 + 骨架(蒙皮用)"),
               ("BODY", "只人物", "人物身体 + 骨架"),
               ("BOTH", "衣服 + 人物", "两个 mesh 一起, 共用一套骨架")],
        default="GARMENT")
    export_path: bpy.props.StringProperty(name="导出路径", default="//SKM_Garment_Fitted.fbx",
                                          subtype="FILE_PATH")

    progress: bpy.props.FloatProperty(name="进度", default=0.0, min=0.0, max=1.0)
    stage: bpy.props.StringProperty(name="阶段", default="")


# ================================================================== 工具
def _wm(o):
    return np.array(o.matrix_world)


def _wverts(o):
    mw = _wm(o)
    v = np.empty(len(o.data.vertices) * 3, np.float32)
    o.data.vertices.foreach_get("co", v)
    v = v.reshape(-1, 3)
    return v @ mw[:3, :3].T + mw[:3, 3]


def _set_wverts(o, W):
    mw = _wm(o)
    inv = np.linalg.inv(mw[:3, :3])
    loc = (W - mw[:3, 3]) @ inv.T
    o.data.vertices.foreach_set("co", loc.astype(np.float32).ravel())
    o.data.update()


def _save_orig(garment):
    """把当前顶点坐标存进 mesh 属性, 用来一键还原(只存一次)"""
    me = garment.data
    if "gf_orig" in me.attributes:
        return
    n = len(me.vertices)
    v = np.empty(n * 3, np.float32)
    me.vertices.foreach_get("co", v)
    attr = me.attributes.new("gf_orig", "FLOAT_VECTOR", "POINT")
    attr.data.foreach_set("vector", v)


def _restore_orig(garment):
    me = garment.data
    if "gf_orig" not in me.attributes:
        return False
    n = len(me.vertices)
    v = np.empty(n * 3, np.float32)
    me.attributes["gf_orig"].data.foreach_get("vector", v)
    me.vertices.foreach_set("co", v)
    me.update()
    if "gf_armpose_sig" in garment:
        del garment["gf_armpose_sig"]
    return True


def _blur3(a, it):
    for _ in range(it):
        s = a.copy()
        for ax in range(3):
            s += np.roll(a, 1, ax) + np.roll(a, -1, ax)
        a = s / 7.0
    return a


def _sample3(v, p, mn, cell):
    g = (p - mn) / cell - 0.5
    g0 = np.floor(g).astype(int)
    f = g - g0
    out = (np.zeros(p.shape[0], np.float32) if v.ndim == 3
           else np.zeros((p.shape[0], 3), np.float32))
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                ix = np.clip(g0[:, 0] + dx, 0, v.shape[0] - 1)
                iy = np.clip(g0[:, 1] + dy, 0, v.shape[1] - 1)
                iz = np.clip(g0[:, 2] + dz, 0, v.shape[2] - 1)
                w = ((f[:, 0] if dx else 1 - f[:, 0]) *
                     (f[:, 1] if dy else 1 - f[:, 1]) *
                     (f[:, 2] if dz else 1 - f[:, 2])).astype(np.float32)
                out += (w * v[ix, iy, iz]) if v.ndim == 3 else (w[:, None] * v[ix, iy, iz])
    return out


def _arm(body, armature=None):
    if armature is not None and armature.type == "ARMATURE":
        return armature
    for m in body.modifiers:
        if m.type == "ARMATURE" and m.object:
            return m.object
    p = body.parent
    while p:
        if p.type == "ARMATURE":
            return p
        p = p.parent
    return None


def _body_tree(body):
    dg = bpy.context.evaluated_depsgraph_get()
    bd = body.evaluated_get(dg)
    bm = bd.to_mesh()
    mw = _wm(body)
    v = np.empty(len(bm.vertices) * 3, np.float32)
    bm.vertices.foreach_get("co", v)
    v = v.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    polys = [tuple(p.vertices) for p in bm.polygons]
    bd.to_mesh_clear()
    return v, polys, BVHTree.FromPolygons([tuple(x) for x in v], polys, all_triangles=False)


def _auto_scale_to_body(body, garment):
    """衣服和人物体型/单位差太多时, 以高度比为基准整体缩放到人物尺寸"""
    Wb = _wverts(body)
    Wg = _wverts(garment)
    hb = float(Wb[:, 2].max() - Wb[:, 2].min())
    hg = float(Wg[:, 2].max() - Wg[:, 2].min())
    if hg < 1e-6 or hb < 1e-6:
        return 1.0
    k = hb / hg
    if 0.75 <= k <= 1.35:      # 体型接近就不缩放, 避免破坏原有比例
        return 1.0
    cb = body.matrix_world @ Vector(((0.0, 0.0, 0.0)))   # 占位, 下面用世界包围盒中心
    center = (Wb.min(0) + Wb.max(0)) * 0.5
    W2 = center + (Wg - center) * k
    _set_wverts(garment, W2)
    return k


def _check_scale(body, garment):
    """衣服和身体的世界尺寸必须同量级, 否则说明缩放不一致, 直接报错比毁模好"""
    def size(o):
        W = _wverts(o)
        return float(np.linalg.norm(W.max(0) - W.min(0)))
    sb, sg = size(body), size(garment)
    if sb < 1e-6 or sg < 1e-6:
        return "尺寸为 0，物体可能被缩放到不可见"
    r = sg / sb
    if r > 5.0 or r < 0.2:
        return ("衣服与身体尺寸不一致（衣服 %.2fm / 身体 %.2fm，比例 %.1f）。"
                "请检查物体的 scale / 父级缩放是否一致，或先把缩放 Apply 掉。" % (sg, sb, r))
    return None


# ================================================================== 尺寸匹配
# 两个按钮: 人物 -> 衣服, 衣服 -> 人物。都是统一缩放(比例不变), 只改尺寸。
# 可选"对齐位置": 让两个物体的包围盒中心重合(同一套坐标)。
# 护手: 用"侧向最远点"比较衣服袖口和人物手腕(取 95 分位, 避开毛边/裙摆外扩),
#       缩放后要求 袖口 <= 手腕, 否则夹紧(衣服)或抬高(人物)比例 —— 不会包住手掌。
# 手腕/肩肘用骨架的静止姿势关节位置, 不受当前 pose 影响(衣服和人物姿势不同也能测)。

def _bbox_stats(W):
    lo = W.min(0)
    hi = W.max(0)
    return lo, hi, (hi - lo), (lo + hi) * 0.5


def _cuff_sides(body, armature):
    """每侧: 水平侧向单位向量 d、肩关节 sh、手臂链长(肩→肘→腕)。
    用静止姿势的关节(bone.head_local), 所以人物当前摆什么姿势都不影响测量。"""
    names = _find_arm_bones(armature)
    mw = armature.matrix_world
    Wb = _wverts(body).astype(np.float64)
    cb = _bbox_stats(Wb)[3]
    out = {}
    for side in ("l", "r"):
        up = names.get("upperarm_" + side)
        low = names.get("lowerarm_" + side)
        if not up or not low:
            continue
        bu = armature.data.bones.get(up)
        bl = armature.data.bones.get(low)
        bh = armature.data.bones.get(names.get("hand_" + side) or "")
        if bu is None or bl is None:
            continue
        sh = np.array(mw @ bu.head_local, float)
        el = np.array(mw @ bl.head_local, float)
        wr = np.array(mw @ bh.head_local, float) if bh is not None \
            else np.array(mw @ bl.tail_local, float)
        d = wr - cb
        d[2] = 0.0
        n = float(np.linalg.norm(d))
        if n < 1e-6:
            continue
        chain = float(np.linalg.norm(el - sh) + np.linalg.norm(wr - el))
        out[side] = (d / n, sh, chain)
    return out, cb


def _lateral_reach(W, origin, d, pct=95.0):
    """点云在侧向 d 上离 origin 的最远距离(取 pct 分位, 抗毛边/外扩)"""
    t = (W - origin) @ d
    t = t[t > 0.02]
    if t.size < 50:
        return None
    return float(np.percentile(t, pct))


def _cuff_limits(sides, Wg, reach_origin, wrist_origin):
    """每侧 (衣服袖口距离, 人物手腕距离)。
    人物侧用"肩关节侧向距离 + 手臂链长", 等价于把手臂摆平后的腕点距离,
    这样衣服和人物姿势不同(比如衣服 T-pose、人物 A-pose)也能比。"""
    out = {}
    for side, (d, sh, chain) in sides.items():
        r = _lateral_reach(Wg, reach_origin, d)
        wref = float((sh - wrist_origin) @ d) + chain
        if r is None or wref < 1e-6:
            continue
        out[side] = (r, wref)
    return out


def _is_under(obj, root):
    p = obj.parent
    while p:
        if p == root:
            return True
        p = p.parent
    return False


def _write_world(o, W):
    """写世界坐标顶点(带形态键时写 Basis, 否则写 mesh)"""
    me = o.data
    if getattr(me, "shape_keys", None) and len(me.shape_keys.key_blocks):
        mw = _wm(o)
        inv = np.linalg.inv(mw[:3, :3])
        loc = (W - mw[:3, 3]) @ inv.T
        me.shape_keys.key_blocks[0].data.foreach_set("co", loc.astype(np.float32).ravel())
        return True
    _set_wverts(o, W)
    return False


def _nominal_k(who, metric, stats):
    """按基准算原始比例: who = 'GARMENT'(缩衣服) / 'BODY'(缩人物)"""
    sb, db = stats["body"]
    sg, dg = stats["garment"]
    if metric == "DIAGONAL":
        k = (db / dg) if who == "GARMENT" else (dg / db)
    elif metric == "ARM":
        return None
    else:
        k = (sb / sg) if who == "GARMENT" else (sg / sb)
    return k


def _scale_stats(body, garment):
    Wb = _wverts(body).astype(np.float64)
    Wg = _wverts(garment).astype(np.float64)
    _, _, extb, cb = _bbox_stats(Wb)
    _, _, extg, cg = _bbox_stats(Wg)
    bstat = (float(extb[2]), float(np.linalg.norm(extb)), Wb, cb)
    gstat = (float(extg[2]), float(np.linalg.norm(extg)), Wg, cg)
    if min(bstat[0], gstat[0], bstat[1], gstat[1]) < 1e-6:
        raise ValueError("尺寸为 0，物体可能被缩放到不可见")
    return bstat, gstat


def scale_garment_to_body(body, garment, armature=None, metric="HEIGHT",
                          guard_cuff=True, align=True):
    """把衣服整体统一缩放到人物尺寸(可同时让包围盒中心重合)。返回信息 dict"""
    bstat, gstat = _scale_stats(body, garment)
    hb, db, Wb, cb = bstat
    hg, dg, Wg, cg = gstat
    stats = {"body": (hb, db), "garment": (hg, dg)}
    pivot = cb                                     # 缩放/对齐的落点是人物包围盒中心

    sides = _cuff_sides(body, armature)[0] if armature else {}
    want = bool(sides) and (guard_cuff or metric == "ARM")
    # 对齐时衣服绕自身中心缩放后整体搬到人物中心; 不对齐时直接绕人物中心缩放
    r_origin = cg if align else cb
    limits = _cuff_limits(sides, Wg, r_origin, cb) if want else {}
    caps = [(w / r) for r, w in limits.values() if r > 1e-9]

    if metric == "ARM":
        if not caps:
            raise ValueError("没检测到袖子，不能按手臂匹配；请改用 按高度 / 按包围盒对角线")
        k = min(caps)
    else:
        k = _nominal_k("GARMENT", metric, stats)

    k_nom = k
    capped = False
    if guard_cuff and caps and k > min(caps) + 1e-6:
        k, capped = min(caps), True
    if k < 0.05 or k > 20.0:
        raise ValueError("缩放比例 x%.2f 太离谱，请检查单位 / 父子缩放" % k)

    if align:
        W2 = pivot + (Wg - cg) * k                 # 中心重合
    else:
        W2 = pivot + (Wg - pivot) * k
    shape_keys = _write_world(garment, W2)
    garment.update_tag()
    return {"k": k, "k_nominal": k_nom, "capped": capped, "metric": metric,
            "sleeves": len(limits), "shape_keys": shape_keys, "align": align}


def scale_body_to_garment(body, garment, armature, metric="HEIGHT",
                          guard_cuff=True, align=True):
    """把人物(骨架+身体)统一缩放到衣服尺寸(可同时让包围盒中心重合)。返回信息 dict"""
    bstat, gstat = _scale_stats(body, garment)
    hb, db, Wb, cb = bstat
    hg, dg, Wg, cg = gstat
    stats = {"body": (hb, db), "garment": (hg, dg)}
    pivot = cg                                     # 落点是衣服包围盒中心

    sides = _cuff_sides(body, armature)[0]
    want = bool(sides) and (guard_cuff or metric == "ARM")
    w_origin = cb if align else cg                 # 对齐时人物绕自身中心缩放
    limits = _cuff_limits(sides, Wg, cg, w_origin) if want else {}
    needs = [(r / w) for r, w in limits.values() if w > 1e-9]

    if metric == "ARM":
        if not needs:
            raise ValueError("没检测到袖子，不能按手臂匹配；请改用 按高度 / 按包围盒对角线")
        k = max(needs)
    else:
        k = _nominal_k("BODY", metric, stats)

    k_nom = k
    capped = False
    if guard_cuff and needs and k < max(needs) - 1e-6:
        k, capped = max(needs), True
    if k < 0.05 or k > 20.0:
        raise ValueError("缩放比例 x%.2f 太离谱，请检查单位 / 父子缩放" % k)

    # 衣服是"标准件", 不能被人物带着一起缩放: 解除绑定并保住世界变换
    detached = False
    g_world = garment.matrix_world.copy()
    if _is_under(garment, armature):
        garment.parent = None
        garment.matrix_world = g_world
        detached = True
    for m in list(garment.modifiers):
        if m.type == "ARMATURE" and m.object is armature:
            garment.modifiers.remove(m)
            detached = True

    body_follows = (_is_under(body, armature) or
                    any(m.type == "ARMATURE" and m.object is armature for m in body.modifiers))
    origin = cb if align else cg
    S = (Matrix.Translation(Vector(pivot.tolist())) @ Matrix.Scale(k, 4) @
         Matrix.Translation(Vector((-origin).tolist())))
    armature.matrix_world = S @ armature.matrix_world
    if not body_follows:            # 身体完全跟骨架无关时, 自己也跟着缩, 否则会脱节
        body.matrix_world = S @ body.matrix_world
    bpy.context.view_layer.update()
    garment.matrix_world = g_world   # 衣服世界位置还原(不跟人物缩放)
    return {"k": k, "k_nominal": k_nom, "capped": capped, "metric": metric,
            "sleeves": len(limits), "detached": detached, "align": align}


# ================================================================== 清理多余人体部位
# 衣服里常常"焊着"原作者的人体零件(手、脚、头)。这里用人物自身的顶点组
# (hand_l / foot_r / toe_* ...) 取人体部位的点云, 把衣服上离这些点 <= 容差
# 的面删掉 —— 只删重合的那一层, 袖子/裤腿本体不动。

_TRIM_KEYS = {
    "hands": ("hand", "wrist", "palm", "finger", "thumb", "手指", "掌"),
    "feet": ("foot", "ankle", "toe", "heel", "脚", "踝"),
    "head": ("head", "skull", "jaw", "头"),
}


def _body_region_points(body, keys, min_w=0.25):
    """人物身上属于这些部位的顶点(世界坐标)。靠顶点组名匹配, 没有就返回 None"""
    idx = set()
    for g in body.vertex_groups:
        low = g.name.lower()
        if any(k in low for k in keys):
            idx.add(g.index)
    if not idx:
        return None
    W = _wverts(body).astype(np.float64)
    out = []
    for v in body.data.vertices:
        for ge in v.groups:
            if ge.group in idx and ge.weight > min_w:
                out.append(W[v.index])
                break
    if not out:
        return None
    return np.array(out, float)


def _find_part_bone(armature, keys, side):
    """按名字找某侧的主骨(最短的那个), 排除脚趾/校正骨"""
    best = None
    for b in armature.data.bones:
        low = b.name.lower()
        if _side_of(low) != side:
            continue
        if any(x in low for x in ("toe", "ball", "corrective", "bck", "fwd", "twist")):
            continue
        if any(k in low for k in keys):
            if best is None or len(b.name) < len(best.name):
                best = b
    return best


def _limb_gates(armature, hands=True, feet=True):
    """腕/踝关节: (名字, 关节世界坐标, 肢体向外方向的单位向量, 半径倍数)。
    方向 = 父骨.head -> 本骨.head, 也就是 小臂->腕 / 小腿->踝。"""
    mw = armature.matrix_world
    gates = []
    for side in ("l", "r"):
        if hands:
            b = _find_part_bone(armature, ("hand", "wrist"), side)
            if b is not None and b.parent is not None:
                o = np.array(mw @ b.head_local, float)
                p = np.array(mw @ b.parent.head_local, float)
                d = o - p
                n = float(np.linalg.norm(d))
                if n > 1e-6:
                    gates.append(("hand_" + side, o, d / n, 1.0))
        if feet:
            b = _find_part_bone(armature, ("foot", "ankle"), side)
            if b is not None and b.parent is not None:
                o = np.array(mw @ b.head_local, float)
                p = np.array(mw @ b.parent.head_local, float)
                d = o - p
                n = float(np.linalg.norm(d))
                if n > 1e-6:
                    gates.append(("foot_" + side, o, d / n, 1.5))
    return gates


def trim_body_parts(garment, body, armature=None, hands=True, feet=True, head=False,
                    margin=0.01, cut_limbs=True, cut_radius=0.15, cut_len=0.5,
                    progress=None, p0=5, p1=95):
    """删掉衣服上多余的人体部位。两种判定取并集:
    ① 与人物手/脚/头表面重合的顶点(<= margin);
    ② 腕/踝以外的整段(沿肢体方向 t>0, 且离肢体轴 < cut_radius) —— 连"长出来的手"一起切掉。
    返回统计 dict"""
    parts = [p for p, on in (("hands", hands), ("feet", feet), ("head", head)) if on]
    if not parts:
        raise ValueError("没有勾选任何部位（手 / 脚 / 头）")
    me = garment.data
    if getattr(me, "shape_keys", None) and len(me.shape_keys.key_blocks):
        raise ValueError("衣服有形态键，请先删除形态键再做清理")

    if progress:
        progress.step(p0, "查找人体部位")

    pts = []
    used, missing = [], []
    for p in parts:
        P = _body_region_points(body, _TRIM_KEYS[p])
        if P is None:
            missing.append(p)
            continue
        pts.append(P)
        used.append(p)

    gates = _limb_gates(armature, hands, feet) if (cut_limbs and armature) else []
    if not pts and not gates:
        raise ValueError("人物身上找不到手/脚/头对应的顶点组，也没有可用的腕/踝关节")

    if progress:
        progress.step(p0 + (p1 - p0) // 5, "读取衣服顶点")

    n = len(me.vertices)
    co = np.empty(n * 3, np.float32)
    me.vertices.foreach_get("co", co)
    mw = _wm(garment)
    W = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    mask = np.zeros(n, bool)

    if pts:
        P = np.vstack(pts)
        kd = KDTree(len(P))
        for i in range(len(P)):
            kd.insert(Vector(P[i].tolist()), i)
        kd.balance()
        for i in range(n):
            _, _, dist = kd.find(Vector(W[i].astype(float).tolist()))
            mask[i] = dist < margin

    for nm, o, d, rk in gates:
        rel = W - o
        t = rel @ d
        radial = np.linalg.norm(rel - np.outer(t, d), axis=1)
        mask |= (t > 0.0) & (t < cut_len) & (radial < cut_radius * rk)
        # 脚: 踝点附近再兜一圈(脚是往前长的, 不在小腿轴线上)
        if nm.startswith("foot"):
            mask |= np.linalg.norm(rel, axis=1) < cut_radius * rk

    if progress:
        progress.step(p0 + (p1 - p0) * 2 // 3, "筛选中…（%d 个顶点）" % int(mask.sum()))

    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    doomed = [f for f in bm.faces if any(mask[v.index] for v in f.verts)]
    nf, nv = len(doomed), int(mask.sum())
    if nf:
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
        loose = [v for v in bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        bm.to_mesh(me)
        me.update()
    bm.free()
    return {"faces": nf, "verts": nv, "parts": used, "missing": missing,
            "gates": len(gates), "margin": margin, "cut_radius": cut_radius,
            "cut_len": cut_len, "left_faces": len(me.polygons)}


# 骨骼命名归一化: 支持 UE / Mixamo / Maya / Unity / 3ds Max 等常见写法
_FAM_KEYS = (
    # 顺序很重要: lowerarm 先判, 否则 "lowerarm_l" 会被 "arm" 误判成上臂
    ("lowerarm", ("lowerarm", "lower_arm", "forearm", "loarm", "forearmtwist",
                  "elbow", "小臂", "前臂")),
    ("clavicle", ("clavicle", "shoulder", "collar", "锁骨")),
    ("upperarm", ("upperarm", "upper_arm", "uparm", "arm_upper", "humerus",
                  "arm", "大臂", "上臂")),
)


def _side_of(low):
    """从骨骼名判断左右: _l / .l / -l / left / l_xxx / _left"""
    n = low.replace(".", "_").replace("-", "_").replace(" ", "_")
    parts = [p for p in n.split("_") if p]
    if not parts:
        return None
    if parts[0] in ("l", "left"):
        return "l"
    if parts[0] in ("r", "right"):
        return "r"
    if parts[-1] in ("l", "left") or n.endswith("left"):
        return "l"
    if parts[-1] in ("r", "right") or n.endswith("right"):
        return "r"
    if "left" in n:
        return "l"
    if "right" in n:
        return "r"
    return None


def _fam_of_name(nm):
    """顶点组 -> 骨骼族。注意: UE/这一套骨架的手臂权重全在 corrective 组里
    (upperarm_bck/fwd/in/out/bicep/tricep/twist...), 所以这里绝不能排除它们,
    否则权重场会是空的, 姿态校正等于没做。"""
    low = nm.lower()
    side = _side_of(low)
    if side is None:
        return None
    for fam, keys in _FAM_KEYS:
        for k in keys:
            if k in low:
                return fam + "_" + side
    return None


def _fam_of_bone(nm):
    """骨骼名 -> 骨骼族。这里要排除 twist/corrective 等辅助骨, 只取主骨。"""
    low = nm.lower()
    side = _side_of(low)
    if side is None:
        return None
    if any(k in low for k in ("twist", "corrective", "bck", "fwd", "in_", "out_",
                              "bicep", "tricep", "pec", "scap", "latissimus",
                              "helper", "ik", "fk")):
        return None
    for fam, keys in _FAM_KEYS:
        for k in keys:
            if k in low:
                return fam + "_" + side
    return None


def _find_arm_bones(armature):
    """在任意命名的骨架上定位 锁骨/上臂/小臂 的实际骨骼名"""
    found = {}
    bones = list(armature.data.bones.keys())
    for b in bones:
        fam = _fam_of_bone(b)
        if not fam:
            continue
        # 同一族只保留最短的那个名字(主骨往往最短)
        if fam not in found or len(b) < len(found[fam]):
            found[fam] = b
    # 补手/腕, 用于小臂朝向
    for b in bones:
        low = b.lower()
        side = _side_of(low)
        if side and any(k in low for k in ("hand", "wrist", "palm")):
            key = "hand_" + side
            if key not in found or len(b) < len(found[key]):
                found[key] = b
    return found


def _fam_weights(garment):
    n = len(garment.data.vertices)
    fam_idx = {}
    for g in garment.vertex_groups:
        k = _fam_of_name(g.name)
        if k:
            fam_idx[g.index] = k
    out = {}
    for k in ("clavicle_l", "upperarm_l", "lowerarm_l",
              "clavicle_r", "upperarm_r", "lowerarm_r"):
        out[k] = np.zeros(n, np.float32)
    for v in garment.data.vertices:
        for g in v.groups:
            k = fam_idx.get(g.group)
            if k:
                out[k][v.index] += g.weight
    return out


def _components(obj):
    me = obj.data
    n = len(me.vertices)
    ed = np.empty(len(me.edges) * 2, np.int32)
    me.edges.foreach_get("vertices", ed)
    ed = ed.reshape(-1, 2)
    parent = np.arange(n, dtype=np.int32)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in ed:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    return np.array([find(i) for i in range(n)])


def _rigid_per_chunk(W, disp):
    """每个连通碎块做一次刚性拟合(Kabsch)，块内形状不变 -> 薄片/绑带不会被拉成梳子"""
    n = len(W)
    roots = _components(bpy.context.scene.garment_fit.garment)
    out = np.zeros_like(disp)
    uniq = np.unique(roots)
    for u in uniq:
        m = roots == u
        if m.sum() < 20:
            out[m] = disp[m].mean(0)
            continue
        P = W[m]
        Q = P + disp[m]
        c0 = P.mean(0)
        c1 = Q.mean(0)
        H = (P - c0).T @ (Q - c1)
        try:
            U, S, Vt = np.linalg.svd(H)
        except Exception:
            out[m] = disp[m].mean(0)
            continue
        d = np.sign(np.linalg.det(Vt.T @ U.T))
        R = Vt.T @ np.diag([1, 1, d]) @ U.T
        out[m] = ((R @ (P - c0).T).T + c1) - P
    return out


def _smooth_fam(FW, W, cell=0.02, it=2):
    """把 6 个手臂权重场做体素平滑并归一化 -> 变形连续, 不会拉裂薄片"""
    mn = W.min(0) - 0.05
    mx = W.max(0) + 0.05
    dims = np.ceil((mx - mn) / cell).astype(int)
    idx = np.floor((W - mn) / cell).astype(int)
    accs = {}
    for k, v in FW.items():
        a = np.zeros(dims, np.float32)
        np.add.at(a, (idx[:, 0], idx[:, 1], idx[:, 2]), v)
        accs[k] = _blur3(a, it)
    tot = sum(accs.values())
    tw = np.maximum(1e-6, _sample3(tot, W, mn, cell))
    out = {}
    for k, a in accs.items():
        out[k] = np.minimum(_sample3(a, W, mn, cell) / tw, 1.0)
    return out


def _pca_axis(P):
    Q = P - P.mean(0)
    _, _, vt = np.linalg.svd(Q, full_matrices=False)
    return vt[0]


def _rodrigues(axis, rad):
    ax = axis / max(np.linalg.norm(axis), 1e-9)
    K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
    return np.eye(3) + math.sin(rad) * K + (1 - math.cos(rad)) * (K @ K)



# ================================================================== 布料预处理
def _shell_count(obj):
    me = obj.data
    n = len(me.vertices)
    ed = np.empty(len(me.edges) * 2, np.int32)
    me.edges.foreach_get("vertices", ed)
    ed = ed.reshape(-1, 2)
    parent = np.arange(n, dtype=np.int32)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in ed:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    return len(np.unique(np.array([find(i) for i in range(n)])))


def _cloth_mask(garment, armature, pin_top=0.08, pin_waist_ratio=0.72,
                pin_shoulder=0.12, smooth=2):
    """生成布料遮罩: 1 = 可模拟, 0 = 固定(领口/肩/腰)。
    结果写进顶点色 ClothMask(R=sim, G=pin) 和两个顶点组 CLOTH_SIM / CLOTH_PIN。"""
    me = garment.data
    W = _wverts(garment)
    n = len(W)
    zmin, zmax = float(W[:, 2].min()), float(W[:, 2].max())
    h = max(zmax - zmin, 1e-6)
    pin = np.zeros(n, np.float32)

    # 领口
    pin[W[:, 2] > zmax - pin_top] = 1.0
    # 腰部(腰带位置, 按高度比例)
    zw = zmin + pin_waist_ratio * h
    pin[np.abs(W[:, 2] - zw) < 0.045] = 1.0
    # 肩部(离肩关节一定半径内)
    if armature is not None:
        for s in ("l", "r"):
            pb = armature.pose.bones.get("upperarm_" + s)
            if pb is None:
                continue
            c = np.array(armature.matrix_world @ pb.head, float)
            d = np.linalg.norm(W - c[None, :], axis=1)
            pin[d < pin_shoulder] = 1.0
    # 平滑遮罩
    if smooth > 0:
        idx = np.floor((W - (W.min(0) - 0.05)) / 0.02).astype(int)
        dims = idx.max(0) + 1
        a = np.zeros(dims, np.float32)
        np.add.at(a, (idx[:, 0], idx[:, 1], idx[:, 2]), pin)
        c = np.zeros(dims, np.float32)
        np.add.at(c, (idx[:, 0], idx[:, 1], idx[:, 2]), 1.0)
        for _ in range(smooth):
            a = _blur3(a, 1)
            c = _blur3(c, 1)
        pin = _sample3(a, W, W.min(0) - 0.05, 0.02) / np.maximum(1e-6, _sample3(c, W, W.min(0) - 0.05, 0.02))
    pin = np.clip(pin, 0, 1)
    sim = 1.0 - pin

    col = np.zeros((n, 4), np.float32)
    col[:, 0] = sim
    col[:, 1] = pin
    col[:, 3] = 1.0
    attr = me.color_attributes.get("ClothMask")
    if attr is None:
        attr = me.color_attributes.new(name="ClothMask", type="FLOAT_COLOR", domain="POINT")
    attr.data.foreach_set("color", col.ravel())
    try:
        me.color_attributes.active_color = attr
        me.color_attributes.render_color_index = me.color_attributes.find(attr.name)
    except Exception:
        pass

    for gname, vals in (("CLOTH_SIM", sim), ("CLOTH_PIN", pin)):
        vg = garment.vertex_groups.get(gname) or garment.vertex_groups.new(name=gname)
        ids = np.nonzero(vals > 0.02)[0]
        step = 0.05
        q = np.round(vals[ids] / step).astype(int)
        for level in np.unique(q):
            sel = ids[q == level]
            if len(sel):
                vg.add([int(x) for x in sel], float(level * step), "REPLACE")
    return float(sim.mean())


def cloth_prep(garment, armature, weld_mm=0.5, sim_tris=12000, make_sim_mesh=True,
               pin_top=0.08, pin_waist_ratio=0.72, pin_shoulder=0.12, mask_smooth=2,
               progress=None, p0=5, p1=95):
    """布料预处理: 焊接碎块 -> 校正法线 -> 生成遮罩 -> 生成低模 Sim 网格"""
    res = {}
    bpy.context.view_layer.objects.active = garment
    bpy.ops.object.select_all(action="DESELECT")
    garment.select_set(True)

    shells0 = _shell_count(garment)
    v0 = len(garment.data.vertices)
    t0 = len(garment.data.polygons)

    if progress:
        progress.step(p0, "焊接碎块 (%.1fmm)" % weld_mm)
    # 1) 焊接: 把 595 个独立 shell 缝成连续面, 布料解算才不会散架
    if weld_mm > 0:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.remove_doubles(threshold=weld_mm / 1000.0)
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.mesh.delete_loose()
        bpy.ops.object.mode_set(mode="OBJECT")
    shells1 = _shell_count(garment)
    res["shells"] = (shells0, shells1)
    res["verts"] = (v0, len(garment.data.vertices))
    res["tris"] = (t0, len(garment.data.polygons))

    if progress:
        progress.step(p0 + (p1 - p0) // 3, "生成布料遮罩")
    res["sim_ratio"] = _cloth_mask(garment, armature, pin_top, pin_waist_ratio,
                                   pin_shoulder, mask_smooth)

    if make_sim_mesh:
        if progress:
            progress.step(p0 + (p1 - p0) * 2 // 3, "生成低模 Sim 网格")
        old = bpy.data.objects.get(garment.name + "_Sim")
        if old:
            d = old.data
            bpy.data.objects.remove(old, do_unlink=True)
            if d.users == 0:
                bpy.data.meshes.remove(d)
        sim = garment.copy()
        sim.data = garment.data.copy()
        sim.name = garment.name + "_Sim"
        sim.data.name = sim.name
        for c in garment.users_collection:
            c.objects.link(sim)
        sim.parent = None
        sim.matrix_world = garment.matrix_world.copy()
        for m in list(sim.modifiers):
            sim.modifiers.remove(m)
        cur = len(sim.data.polygons)
        ratio = max(0.01, min(1.0, float(sim_tris) / max(1, cur)))
        dec = sim.modifiers.new("GF_Decimate", "DECIMATE")
        dec.decimate_type = "COLLAPSE"
        dec.ratio = ratio
        bpy.context.view_layer.objects.active = sim
        bpy.ops.object.select_all(action="DESELECT")
        sim.select_set(True)
        bpy.ops.object.modifier_apply(modifier=dec.name)
        # Sim 网格也保留遮罩(顶点色)
        res["sim_verts"] = len(sim.data.vertices)
        res["sim_tris"] = len(sim.data.polygons)
        # 绑定到骨架(UE 里 Sim Mesh 也要跟着动)
        if armature is not None:
            sim.parent = armature
            sim.parent_type = "OBJECT"
            sim.matrix_parent_inverse = armature.matrix_world.inverted()
            am = sim.modifiers.new("Armature", "ARMATURE")
            am.object = armature
        bpy.context.view_layer.objects.active = garment
        bpy.ops.object.select_all(action="DESELECT")
        garment.select_set(True)

    if progress:
        progress.step(p1, "布料预处理完成")
    return res


# ================================================================== 人物匹配衣服
def _auto_scale_body(body, garment, armature, clamp=(0.5, 2.0)):
    """把人物(身体+骨架)整体缩放到衣服的尺寸"""
    Wb = _wverts(body)
    Wg = _wverts(garment)
    hb = float(Wb[:, 2].max() - Wb[:, 2].min())
    hg = float(Wg[:, 2].max() - Wg[:, 2].min())
    if hb < 1e-6 or hg < 1e-6:
        return 1.0
    k = hg / hb
    if 0.97 < k < 1.03:
        return 1.0
    k = max(clamp[0], min(clamp[1], k))
    # 以身体外接盒中心为原点缩放骨架(身体是骨架的子级, 会跟着走)
    c = (Wb.min(0) + Wb.max(0)) * 0.5
    arm_mw = armature.matrix_world.copy()
    S = Matrix.Translation(Vector(c.tolist())) @ Matrix.Scale(k, 4) @ Matrix.Translation(Vector((-c).tolist()))
    armature.matrix_world = S @ arm_mw
    bpy.context.view_layer.update()
    return k


def _arm_bone_names(armature):
    return _find_arm_bones(armature)


def pose_body_to_garment(body, garment, armature, max_rot=18.0, iterations=2,
                         progress=None, p0=10, p1=45):
    """旋转人物手臂骨骼, 让身体手臂落进衣服袖子里(直接改姿势, 之后可烘焙成静止姿势)"""
    names = _arm_bone_names(armature)
    if not names:
        return 0
    FW = _fam_weights(garment)
    done = 0
    for it in range(max(1, iterations)):
        # 身体(带姿势)世界顶点
        bv, polys, tree = _body_tree(body)
        Wg = _wverts(garment)
        for side in ("l", "r"):
            for part, bone_key in (("clavicle", "clavicle_" + side),
                                   ("upperarm", "upperarm_" + side),
                                   ("lowerarm", "lowerarm_" + side)):
                bname = names.get(bone_key)
                if not bname:
                    continue
                pb = armature.pose.bones.get(bname)
                if pb is None:
                    continue
                # 身体簇: 取落在该骨骼影响范围内的身体顶点(用世界坐标筛选)
                bone_h = np.array(armature.matrix_world @ pb.head, float)
                bone_t = np.array(armature.matrix_world @ pb.tail, float)
                d = bone_t - bone_h
                L = np.linalg.norm(d)
                if L < 1e-6:
                    continue
                d = d / L
                rel = bv - bone_h
                t = rel @ d
                off = rel - np.outer(t, d)
                r = np.linalg.norm(off, axis=1)
                A = bv[(t > 0.01) & (t < max(L, 0.05)) & (r < 0.09)]
                B = Wg[FW[bone_key] > 0.35]
                if len(A) < 100 or len(B) < 100:
                    continue
                axa = _pca_axis(A)
                axb = _pca_axis(B)
                if axa @ d < 0:
                    axa = -axa
                if axb @ d < 0:
                    axb = -axb
                u = axa / np.linalg.norm(axa)
                v = axb / np.linalg.norm(axb)
                ang = math.degrees(math.acos(float(np.clip(u @ v, -1, 1))))
                axis = np.cross(u, v)
                if np.linalg.norm(axis) < 1e-6:
                    continue
                ang = min(ang, max_rot)
                R3 = _rodrigues(axis, math.radians(ang))
                # 在"骨架空间"里绕骨骼头旋转(pb.matrix 是骨架空间, 支点必须用 pb.head,
                # 不能用世界坐标, 否则 0.01 缩放的骨架会把手臂甩飞)
                R = Matrix.Rotation(math.radians(ang), 4, Vector(axis.tolist()))
                T1 = Matrix.Translation(pb.head.copy())
                pb.matrix = T1 @ R @ T1.inverted() @ pb.matrix
                bpy.context.view_layer.update()
                done += 1
        if progress:
            progress.step(p0 + (p1 - p0) * (it + 1) // max(1, iterations),
                          "人物手臂贴合衣服 %d/%d" % (it + 1, iterations))
    return done


def bake_body_pose(body, armature):
    """把当前姿势烘焙成身体网格 + 骨架的静止姿势"""
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    # 1) 应用 Armature 修改器 -> 网格变成"当前姿势"的形状
    bpy.ops.object.select_all(action="DESELECT")
    body.select_set(True)
    bpy.context.view_layer.objects.active = body
    for m in list(body.modifiers):
        if m.type == "ARMATURE":
            try:
                bpy.ops.object.modifier_apply(modifier=m.name)
            except Exception as e:
                print("[GarmentFit] 应用修改器失败:", e)
            break
    # 2) 把当前姿势设为静止姿势
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.select_all(action="SELECT")
    try:
        bpy.ops.pose.armature_apply(selected=False)
    except Exception as e:
        print("[GarmentFit] armature_apply 失败:", e)
    bpy.ops.object.mode_set(mode="OBJECT")
    # 3) 重新挂上 Armature 修改器
    am = body.modifiers.new("Armature", "ARMATURE")
    am.object = armature


# ================================================================== ① 权重
def transfer_weights(garment, body, limit=4, progress=None, p0=2, p1=30):
    bv, polys, tree = _body_tree(body)
    ng = len(body.vertex_groups)
    R = np.zeros((len(bv), ng), np.float32)
    for v in body.data.vertices:
        for g in v.groups:
            if g.group < ng:
                R[v.index, g.group] = g.weight

    me = garment.data
    n = len(me.vertices)
    W = _wverts(garment)
    face = np.full(n, -1, np.int32)
    bary = np.zeros((n, 4), np.float32)
    chunk = max(1, n // 25)
    for i in range(n):
        loc, nor, fi, dist = tree.find_nearest(Vector(W[i]), 0.30)
        if loc is not None:
            poly = polys[fi]
            b = poly_3d_calc([Vector(bv[j]) for j in poly], loc)
            face[i] = fi
            for k, bb in enumerate(b[:4]):
                bary[i, k] = bb
        if progress and i % chunk == 0:
            progress.step(p0 + (p1 - p0) * i // n, "最近面 %d%%" % (i * 100 // n))

    fv = np.full((n, 4), -1, np.int32)
    for i in range(n):
        if face[i] >= 0:
            for k, j in enumerate(polys[face[i]][:4]):
                fv[i, k] = j

    K = max(1, limit)
    tops = np.zeros((n, K), np.int32)
    tvals = np.zeros((n, K), np.float32)
    CH = 40000
    for s0 in range(0, n, CH):
        s1 = min(n, s0 + CH)
        Wt = np.zeros((s1 - s0, ng), np.float32)
        for k in range(4):
            col = fv[s0:s1, k]
            ok = col >= 0
            if ok.any():
                Wt[ok] += bary[s0:s1, k][ok][:, None] * R[col[ok]]
        part = np.argpartition(-Wt, K, axis=1)[:, :K]
        rows = np.arange(s1 - s0)[:, None]
        vals = Wt[rows, part]
        vals[vals < 1e-4] = 0
        sm = vals.sum(1, keepdims=True)
        sm[sm < 1e-6] = 1
        vals /= sm
        tops[s0:s1] = part
        tvals[s0:s1] = vals
        if progress:
            progress.step(p0 + (p1 - p0) * s1 // n, "求解权重 %d%%" % (s1 * 100 // n))

    for g in list(garment.vertex_groups):
        garment.vertex_groups.remove(g)
    vgs = [garment.vertex_groups.new(name=g.name) for g in body.vertex_groups]
    for gi in range(len(vgs)):
        m = (tops == gi) & (tvals > 0.0005)
        if not m.any():
            continue
        vi, ki = np.nonzero(m)
        q = np.round(tvals[vi, ki] / 0.005).astype(np.int32)
        order = np.argsort(q, kind="stable")
        vi, q = vi[order], q[order]
        s = 0
        while s < len(vi):
            e = s
            while e < len(vi) and q[e] == q[s]:
                e += 1
            vgs[gi].add([int(x) for x in vi[s:e]], float(q[s] * 0.005), "REPLACE")
            s = e
        if progress and gi % 40 == 0:
            progress.step(p0 + (p1 - p0) * gi // len(vgs), "写权重 %d/%d" % (gi, len(vgs)))
    return len(garment.vertex_groups)


# ================================================================== ② 手臂姿态校正
def fix_arm_pose(garment, body, armature, max_rot=10.0, strength=1.0,
                 max_move=0.05, iterations=2, rigid_chunks=False,
                 str_up=0.95, str_lo=0.7, smooth_it=2, str_cl=0.3,
                 progress=None, p0=30, p1=50):
    """把衣服手臂按骨骼对齐到人物手臂(权重场平滑驱动, 不撕裂)"""
    bv, polys, tree = _body_tree(body)
    gn = {g.index: g.name for g in body.vertex_groups}
    dom = np.array([gn[max(v.groups, key=lambda x: x.weight).group] if v.groups else ""
                    for v in body.data.vertices])
    STR = {"clavicle": str_cl * strength, "upperarm": str_up * strength,
           "lowerarm": str_lo * strength}

    def head(b):
        pb = armature.pose.bones.get(b)
        return np.array(armature.matrix_world @ pb.head, float) if pb else None

    bones = _find_arm_bones(armature)
    piv, dref = {}, {}
    for s in ("l", "r"):
        sh = head(bones.get("upperarm_" + s, ""))
        el = head(bones.get("lowerarm_" + s, ""))
        wr = head(bones.get("hand_" + s, ""))
        if sh is None or el is None:
            continue
        piv["clavicle_" + s] = sh
        piv["upperarm_" + s] = sh
        piv["lowerarm_" + s] = el
        dref["clavicle_" + s] = np.array([1.0 if s == "l" else -1.0, 0.0, 0.0])
        dref["upperarm_" + s] = el - sh
        dref["lowerarm_" + s] = (wr - el) if wr is not None else (el - sh)

    sig = "%s|%s|%s|%s|%s|%s|%s" % (max_rot, strength, max_move, iterations,
                                     str_cl, str_up, str_lo)
    if garment.get("gf_armpose_sig") == sig:
        if progress:
            progress.step(p1, "已做过手臂姿态校正，跳过(重复执行会叠加变形)")
        print("[GarmentFit] 已处理过(签名相同)，跳过手臂姿态校正")
        return 0

    for it in range(max(1, iterations)):
        FW = _fam_weights(garment)
        W = _wverts(garment)
        FS = _smooth_fam(FW, W, 0.02, max(1, smooth_it))
        disp = np.zeros((len(W), 3), np.float32)
        for k in list(piv.keys()):
            part = k.split("_")[0]
            w = FS[k] * STR[part]
            gm = FW[k] > 0.35
            bmask = np.array([_fam_of_name(d) == k for d in dom])
            if gm.sum() < 200 or bmask.sum() < 80:
                continue
            A, B = W[gm], bv[bmask]
            axg, axb = _pca_axis(A), _pca_axis(B)
            d0 = dref[k] / max(np.linalg.norm(dref[k]), 1e-9)
            if axg @ d0 < 0:
                axg = -axg
            if axb @ d0 < 0:
                axb = -axb
            u = axg / np.linalg.norm(axg)
            v = axb / np.linalg.norm(axb)
            ang = math.degrees(math.acos(float(np.clip(u @ v, -1, 1))))
            axis = np.cross(u, v)
            if part == "clavicle":
                # 锁骨簇里混着领口/前胸, 一旦旋转就会把领口往下拽 -> 只平移不旋转
                R = np.eye(3)
            else:
                R = np.eye(3) if np.linalg.norm(axis) < 1e-6 else _rodrigues(axis, math.radians(min(ang, max_rot)))
            h = piv[k]
            t = (np.median(B, axis=0) - np.median(A, axis=0)) * STR[part]
            L = float(np.linalg.norm(t))
            if L > max_move:
                t = t * (max_move / L)
            P = (R @ (W - h).T).T + h + t
            disp += w[:, None] * (P - W).astype(np.float32)
        _set_wverts(garment, W + disp)
        if progress:
            progress.step(p0 + (p1 - p0) * (it + 1) // max(1, iterations),
                          "手臂姿态 %d/%d" % (it + 1, iterations))
    garment["gf_armpose_sig"] = sig
    return max(1, iterations)


# ================================================================== 大袖: 袖口吸附
def align_sleeves_rigid(garment, armature, strength=1.0, max_deg=55.0,
                        progress=None, p0=32, p1=46):
    """大袖/垂袖: 把整只袖子当作刚体绕肩关节转到人物手臂方向上。

    刚体旋转不会改变任何局部形状(袖口、褶子、垂坠都原样), 只在肩部做权重过渡,
    所以既能让袖子跟着手臂走, 又不会把袖口揉乱。
    """
    FW = _fam_weights(garment)
    W = _wverts(garment).astype(np.float64)
    FS = _smooth_fam(FW, W, 0.02, 2)
    bones = _find_arm_bones(armature)
    mw = armature.matrix_world
    disp = np.zeros_like(W)
    done = 0
    for side in ("l", "r"):
        bu = bones.get("upperarm_" + side)
        bl = bones.get("lowerarm_" + side)
        bh = bones.get("hand_" + side)
        if not bu or not bl:
            continue
        sh = np.array(mw @ armature.pose.bones[bu].head, float)
        el = np.array(mw @ armature.pose.bones[bl].head, float)
        wr = np.array(mw @ armature.pose.bones[bh].head, float) if bh \
            else np.array(mw @ armature.pose.bones[bl].tail, float)
        w = np.clip(FS["upperarm_" + side] + FS["lowerarm_" + side], 0.0, 1.0)
        sel = w > 0.25
        if int(sel.sum()) < 300:
            continue
        A = W[sel]
        # 衣服袖子自己的轴向: 离肩最近的 15% -> 最远的 15%
        d_body = wr - sh
        Lb = float(np.linalg.norm(d_body))
        if Lb < 1e-6:
            continue
        d_body /= Lb
        t = (A - sh) @ d_body
        near = A[t <= np.percentile(t, 15)].mean(0)
        far = A[t >= np.percentile(t, 85)].mean(0)
        ax_g = far - near
        lg = float(np.linalg.norm(ax_g))
        if lg < 1e-5:
            continue
        ax_g /= lg
        ang = math.degrees(math.acos(float(np.clip(ax_g @ d_body, -1.0, 1.0))))
        axis = np.cross(ax_g, d_body)
        if np.linalg.norm(axis) < 1e-6 or ang < 1.0:
            continue
        ang = min(ang * float(strength), max_deg)
        R = _rodrigues(axis / np.linalg.norm(axis), math.radians(ang))
        f = np.clip((w[sel] - 0.2) / 0.35, 0.0, 1.0)
        f = f * f * (3.0 - 2.0 * f)
        rotated = (R @ (A - sh).T).T + sh
        disp[sel] += f[:, None] * (rotated - A)
        done += 1
        if progress:
            progress.step(p0 + (p1 - p0) * done // 2, "袖子整体转向手臂 %s" % side)
    if done:
        _set_wverts(garment, W + disp)
    return done


def snap_cuffs_to_arms(garment, armature, strength=1.0, max_move=0.15, tighten=0.0,
                       progress=None, p0=30, p1=45):
    """大袖/垂袖专用: 只把"袖口那一圈"往人物手腕拉过去。

    不做旋转(旋转会把垂下来的大袖摆折成硬褶), 而是按"沿手臂由内到外的距离"给一个
    平滑权重, 越靠袖口跟随越多, 肩部完全不动 —— 袖口能扣到手腕上, 形状不会被扭。
    """
    FW = _fam_weights(garment)
    W = _wverts(garment).astype(np.float64)
    FS = _smooth_fam(FW, W, 0.02, 2)
    bones = _find_arm_bones(armature)
    mw = armature.matrix_world
    disp = np.zeros_like(W)
    done, info = 0, {}
    for side in ("l", "r"):
        hb = bones.get("lowerarm_" + side)
        hd = bones.get("hand_" + side)
        if not hb or not hd:
            continue
        el = np.array(mw @ armature.pose.bones[hb].head, float)
        wr = np.array(mw @ armature.pose.bones[hd].head, float)
        d = wr - el
        L = float(np.linalg.norm(d))
        if L < 1e-6:
            continue
        d /= L
        sel = FW["lowerarm_" + side] > 0.4
        if int(sel.sum()) < 300:
            continue
        A = W[sel]
        t = (A - el) @ d
        t0 = float(np.percentile(t, 45))
        t1 = float(t.max())
        if t1 - t0 < 1e-4:
            continue
        cuff = A[t >= np.percentile(t, 90)]
        C = cuff.mean(0)
        delta = (wr - C) * float(strength)
        dl = float(np.linalg.norm(delta))
        if dl < 0.002:
            continue
        if dl > max_move:
            delta *= max_move / dl
            dl = max_move
        f = np.clip((t - t0) / (t1 - t0), 0.0, 1.0)
        f = f * f * (3.0 - 2.0 * f)                       # smoothstep
        f *= np.clip((FS["lowerarm_" + side][sel] - 0.15) / 0.5, 0.0, 1.0)
        # 远端整段按"刚体平移"处理: 只在小臂近端做过渡, 袖口形状一点不变
        f = np.maximum(f, np.clip((t - (t0 + 0.35 * (t1 - t0))) / 0.05, 0.0, 1.0))
        disp[sel] += f[:, None] * delta

        if tighten > 1e-4:
            # 可选: 把靠手臂的那圈布料往手臂轴上收(前臂露出来时才需要)
            rel = A - el
            radial = rel - np.outer(t, d)
            r = np.linalg.norm(radial, axis=1)
            b0 = np.percentile(t, 40)
            band = np.clip((t - b0) / max(1e-6, t1 - b0), 0.0, 1.0)
            band = band * band * (3.0 - 2.0 * band)
            near = np.clip((0.18 - r) / 0.18, 0.0, 1.0)
            shrink = np.clip((r - 0.055) / np.maximum(r, 1e-6), 0.0, 1.0)
            shrink = shrink * band * near * float(tighten)
            disp[sel] -= radial * shrink[:, None]
        info[side] = (float(np.linalg.norm(wr - C)), dl)
        done += 1
        if progress:
            progress.step(p0 + (p1 - p0) * done // 2, "袖口吸附 %s" % side)
    if done:
        _set_wverts(garment, W + disp)
    return {"sides": done, "info": info}


# ================================================================== ③ 防穿模
def fit_penetration(garment, body, gap=0.008, max_push=0.05, progress=None, p0=50, p1=60):
    bv, polys, tree = _body_tree(body)
    me = garment.data
    n = len(me.vertices)
    W = _wverts(garment)
    disp = np.zeros((n, 3), np.float32)
    mask = np.zeros(n, np.float32)
    chunk = max(1, n // 20)
    for i in range(n):
        p = Vector(W[i])
        loc, nor, fi, dist = tree.find_nearest(p, 0.12)
        if loc is not None and dist <= 0.08 and (p - loc).dot(nor) < -0.001:
            d = np.array(loc + nor * gap, np.float32) - W[i]
            L = np.linalg.norm(d)
            if L > max_push:
                d = d * (max_push / L)
            disp[i] = d
            mask[i] = 1.0
        if progress and i % chunk == 0:
            progress.step(p0 + (p1 - p0) * i // n, "防穿模 %d%%" % (i * 100 // n))

    moved = int(mask.sum())
    if moved:
        cell = 0.01
        mn = W.min(0) - 0.05
        mx = W.max(0) + 0.05
        dims = np.ceil((mx - mn) / cell).astype(int)
        sel = mask > 0
        i0 = np.floor((W[sel] - mn) / cell).astype(int)
        acc = np.zeros((*dims, 3), np.float32)
        cnt = np.zeros(dims, np.float32)
        np.add.at(acc, (i0[:, 0], i0[:, 1], i0[:, 2]), disp[sel])
        np.add.at(cnt, (i0[:, 0], i0[:, 1], i0[:, 2]), 1.0)
        accb = _blur3(acc, 2)
        cntb = _blur3(cnt, 2)
        dens = cntb > 1e-4
        field = np.zeros_like(accb)
        field[dens] = accb[dens] / cntb[dens, None]
        fld = _sample3(field, W, mn, cell)
        mg = np.clip(_blur3(cnt, 2) * 3.0, 0, 1)
        mf = np.clip(_sample3(mg, W, mn, cell), 0, 1)
        _set_wverts(garment, W + fld * mf[:, None])
    return moved


# ================================================================== ④ 绑定
def bind(garment, armature):
    """绑定骨架。注意：必须保住物体原来的世界变换，否则物品会因为父级缩放(0.01/100)而飞走或缩小。"""
    world = garment.matrix_world.copy()
    for m in list(garment.modifiers):
        if m.type in {"ARMATURE", "DATA_TRANSFER"}:
            garment.modifiers.remove(m)
    garment.parent = armature
    garment.parent_type = "OBJECT"
    garment.matrix_parent_inverse = Matrix.Identity(4)
    garment.matrix_world = world          # 关键: 重新指定世界矩阵, 抵消父级缩放差
    am = garment.modifiers.new("Armature", "ARMATURE")
    am.object = armature
    am.use_deform_preserve_volume = False
    return garment.matrix_world.copy()


# ================================================================== ⑤ 导出
def export_fbx(meshes, armature, path, progress=None, p0=90, p1=100):
    """导出 FBX。
    meshes: 要导出的 mesh 列表(衣服 / 人物 / 两个都行), 骨架始终一起导出(蒙皮需要)。
    做法: 复制一份 -> 把 0.01 之类的世界缩放烘进顶点和骨骼 -> 导出 -> 删掉副本。
    """
    meshes = [m for m in meshes if m is not None and m.type == "MESH"]
    if not meshes:
        return 0

    # 统一缩放: 以骨架的世界缩放为基准; 各 mesh 若与骨架差太多就各自烘焙
    sc_arm = armature.matrix_world.to_scale()
    k = (sc_arm.x + sc_arm.y + sc_arm.z) / 3.0

    arm2 = armature.copy()
    arm2.data = armature.data.copy()
    arm2.name = "GF_TMP_ARM"
    bpy.context.scene.collection.objects.link(arm2)
    arm2.matrix_world = Matrix.Identity(4)
    arm2.data.transform(Matrix.Scale(k, 4))

    copies = []
    for src in meshes:
        o = src.copy()
        o.data = src.data.copy()
        o.name = "GF_TMP_" + src.name[:28]
        bpy.context.scene.collection.objects.link(o)
        for m in list(o.modifiers):
            o.modifiers.remove(m)
        o.parent = None
        o.matrix_world = Matrix.Identity(4)
        s = src.matrix_world.to_scale()
        km = (s.x + s.y + s.z) / 3.0
        o.data.transform(Matrix.Scale(km if abs(km - k) > 1e-6 else k, 4))
        o.parent = arm2
        o.parent_type = "OBJECT"
        o.matrix_parent_inverse = Matrix.Identity(4)
        am = o.modifiers.new("Armature", "ARMATURE")
        am.object = arm2
        copies.append(o)

    if progress:
        progress.step(p0 + (p1 - p0) // 2, "导出 FBX (%d 个网格)" % len(copies))
    bpy.ops.object.select_all(action="DESELECT")
    for o in copies:
        o.select_set(True)
    arm2.select_set(True)
    bpy.context.view_layer.objects.active = arm2
    bpy.ops.export_scene.fbx(
        filepath=bpy.path.abspath(path), use_selection=True,
        object_types={"ARMATURE", "MESH"}, apply_scale_options="FBX_SCALE_NONE",
        add_leaf_bones=False, bake_anim=False, use_mesh_modifiers=False,
        mesh_smooth_type="FACE", axis_forward="-Z", axis_up="Y")

    datas = [(o.data, o) for o in copies]
    for o in copies:
        bpy.data.objects.remove(o, do_unlink=True)
    ad = arm2.data
    bpy.data.objects.remove(arm2, do_unlink=True)
    for d, _ in datas:
        if d.users == 0:
            try:
                bpy.data.meshes.remove(d)
            except Exception:
                pass
    if ad.users == 0:
        bpy.data.armatures.remove(ad)
    return len(copies)


# ================================================================== 覆盖层(toast / 进度条)
# 3D 视图底部画一条覆盖层: 普通提示(toast)或执行中的进度条, 不打断操作。
_ov = {"handler": None, "text": "", "pct": None, "last": 0.0}


def _redraw_3d(force=False):
    try:
        for wm in bpy.data.window_managers:
            for win in wm.windows:
                scr = win.screen
                if scr:
                    for area in scr.areas:
                        if area.type == "VIEW_3D":
                            area.tag_redraw()
    except Exception:
        pass
    if force and not bpy.app.background:
        try:
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
        except Exception:
            pass


def _ov_rect(x, y, w, h, color):
    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRI_FAN",
                             {"pos": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]})
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def _ov_draw():
    import blf
    try:
        ctx = bpy.context
        region = getattr(ctx, "region", None)
        if region is None:
            return
        try:
            scale = float(ctx.preferences.system.ui_scale)
        except Exception:
            scale = 1.0
        f = 0
        pct = _ov["pct"]
        text = _ov["text"]

        if pct is None:                      # 普通提示
            size = max(12, int(16 * scale))
            try:
                blf.size(f, size)
            except TypeError:
                blf.size(f, size, 72)
            tw, _ = blf.dimensions(f, text)
            x = max(10.0, (region.width - tw) * 0.5)
            y = 44.0 * scale
            blf.color(f, 0.0, 0.0, 0.0, 0.9)
            blf.position(f, x + 2, y - 2, 0)
            blf.draw(f, text)
            blf.color(f, 0.55, 1.0, 0.60, 1.0)
            blf.position(f, x, y, 0)
            blf.draw(f, text)
            return

        w = min(560.0 * scale, region.width * 0.6)     # 进度条
        h = 12.0 * scale
        x = (region.width - w) * 0.5
        y = 64.0 * scale
        label = "%s   %d%%" % (text, int(round(pct * 100)))
        size = max(12, int(15 * scale))
        try:
            blf.size(f, size)
        except TypeError:
            blf.size(f, size, 72)
        tw, _ = blf.dimensions(f, label)
        tx = max(10.0, (region.width - tw) * 0.5)
        ty = y + h + 8 * scale
        blf.color(f, 0.0, 0.0, 0.0, 0.9)
        blf.position(f, tx + 2, ty - 2, 0)
        blf.draw(f, label)
        blf.color(f, 0.85, 0.95, 1.0, 1.0)
        blf.position(f, tx, ty, 0)
        blf.draw(f, label)
        _ov_rect(x - 3 * scale, y - 3 * scale, w + 6 * scale, h + 6 * scale, (0.05, 0.06, 0.07, 0.85))
        _ov_rect(x, y, w, h, (0.16, 0.18, 0.20, 0.95))
        if pct > 0:
            _ov_rect(x, y, max(2 * scale, w * pct), h, (0.30, 0.75, 0.40, 0.95))
    except Exception:
        pass


def _ov_set(text, pct=None):
    _ov["text"] = text
    _ov["pct"] = pct
    if _ov["handler"] is None:
        try:
            _ov["handler"] = bpy.types.SpaceView3D.draw_handler_add(
                _ov_draw, (), "WINDOW", "POST_PIXEL")
        except Exception as e:
            print("[GarmentFit] overlay:", e)
    _redraw_3d()


def _ov_hide():
    h = _ov["handler"]
    if h is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, "WINDOW")
        except Exception as e:
            print("[GarmentFit] overlay remove:", e)
        _ov["handler"] = None
    _ov["pct"] = None
    _redraw_3d()
    return None


def toast(text, seconds=5.0):
    """在 3D 视图底部弹一条提示(不打断操作, seconds 秒后自动消失)"""
    _ov_set(text, None)
    try:
        bpy.app.timers.register(_ov_hide, first_interval=max(0.5, float(seconds)))
    except Exception as e:
        print("[GarmentFit] toast:", e)


def overlay_progress(pct, text="", hide_after=None):
    """在 3D 视图底部画执行中的进度条(节流刷新, 不拖慢主任务)"""
    import time
    _ov_set(text, max(0.0, min(1.0, float(pct))))
    now = time.time()
    if now - _ov["last"] > 0.25:
        _ov["last"] = now
        _redraw_3d(force=True)
    if hide_after:
        try:
            bpy.app.timers.register(_ov_hide, first_interval=float(hide_after))
        except Exception:
            pass


# ================================================================== 操作符
def run_match(context, op, skip_arm=False):
    """一键匹配的公共实现。skip_arm=True 时跳过手臂姿态校正(大袖/垂袖)。"""
    st = context.scene.garment_fit
    if not st.garment or not st.body:
        op.report({"ERROR"}, "请先指定 人物身体 和 衣服")
        return {"CANCELLED"}
    arm = _arm(st.body, st.armature)
    if arm is None:
        op.report({"ERROR"}, "找不到人物骨架")
        return {"CANCELLED"}
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    if st.auto_scale:
        k = _auto_scale_to_body(st.body, st.garment)
        if abs(k - 1.0) > 0.03:
            print("[GarmentFit] 自动缩放衣服 x%.3f" % k)
    err = _check_scale(st.body, st.garment)
    if err:
        op.report({"ERROR"}, err)
        print("[GarmentFit] ERROR:", err)
        return {"CANCELLED"}

    # ============ 模式二: 人物匹配衣服 ============
    if st.match_mode == "BODY":
        pr = GF_Progress(100, "人物匹配衣服")
        try:
            # 先把衣服从骨架上摘下来并保住世界变换, 否则缩放人物时衣服会一起缩,
            # 两边同比缩放等于没匹配
            if st.garment.parent == arm:
                world = st.garment.matrix_world.copy()
                st.garment.parent = None
                st.garment.matrix_world = world
                for m in list(st.garment.modifiers):
                    if m.type == "ARMATURE":
                        st.garment.modifiers.remove(m)
            if st.auto_scale:
                k = _auto_scale_body(st.body, st.garment, arm)
                if abs(k - 1.0) > 0.03:
                    print("[GarmentFit] 人物缩放 x%.3f" % k)
                    pr.step(10, "人物缩放 x%.3f" % k)
            pr.step(12, "人物手臂贴合衣服")
            n = pose_body_to_garment(st.body, st.garment, arm, st.arm_max_rot,
                                     max(1, st.arm_iterations), pr, 12, 45)
            if st.bake_body:
                pr.step(50, "烘焙人物姿势")
                bake_body_pose(st.body, arm)
            if st.do_weights:
                pr.step(60, "传递权重")
                transfer_weights(st.garment, st.body, st.limit_weights, pr, 60, 92)
            pr.step(94, "绑定")
            bind(st.garment, arm)
            pr.end("完成(人物匹配衣服)")
            op.report({"INFO"}, "人物匹配衣服: 缩放+手臂调整 %d 次, 顶点组 %d" %
                      (n, len(st.garment.vertex_groups)))
        finally:
            if pr.wm:
                try:
                    pr.wm.progress_end()
                except Exception:
                    pass
            if pr.pct < 0.999:
                _ov_hide()
        return {"FINISHED"}

    # ============ 模式一(默认): 衣服匹配人物 ============
    _save_orig(st.garment)
    pr = GF_Progress(100, "一键匹配" if not skip_arm else "一键匹配(大袖/垂袖)")
    try:
        if st.do_weights:
            pr.step(2, "传递权重")
            transfer_weights(st.garment, st.body, st.limit_weights, pr, 2, 30)
        if st.do_arm_pose:
            pr.step(32, "手臂姿态校正")
            fix_arm_pose(st.garment, st.body, arm, st.arm_max_rot, st.arm_strength,
                         st.arm_max_move / 100.0, st.arm_iterations,
                         st.arm_rigid_chunks, st.arm_str_up, st.arm_str_lo,
                         st.weight_smooth, st.arm_str_cl, pr, 30, 50)
        moved = 0
        if st.do_fit:
            pr.step(50, "防穿模")
            moved = fit_penetration(st.garment, st.body, st.fit_gap, st.fit_max_push, pr, 50, 60)
        pr.step(85, "绑定")
        bind(st.garment, arm)
        pr.end("完成")
        op.report({"INFO"}, "%s: 顶点组 %d, 推出 %d 点" %
                  (st.garment.name, len(st.garment.vertex_groups), moved))
    finally:
        if pr.wm:
            try:
                pr.wm.progress_end()
            except Exception:
                pass
        if pr.pct < 0.999:
            _ov_hide()
    return {"FINISHED"}


class GF_OT_match(bpy.types.Operator):
    """一键: 权重 + 手臂姿态校正 + 防穿模 + 绑定"""
    bl_idname = "garment_fit.match"
    bl_label = "一键匹配 (姿态+蒙皮)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return run_match(context, self, skip_arm=False)


class GF_OT_scale_match(bpy.types.Operator):
    """把人物或衣服整体统一缩放到对方尺寸(只改大小, 不改比例)"""
    bl_idname = "garment_fit.scale_match"
    bl_label = "尺寸匹配"
    bl_description = "统一缩放：人物缩放到衣服尺寸，或衣服缩放到人物尺寸（护手: 袖口不盖手掌）"
    bl_options = {"REGISTER", "UNDO"}

    direction: bpy.props.EnumProperty(
        name="方向",
        items=[("BODY", "人物 → 衣服", "把人物(含骨架)缩放到衣服尺寸"),
               ("GARMENT", "衣服 → 人物", "把衣服几何缩放到人物尺寸")],
        default="BODY")

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.body or not st.garment:
            self.report({"ERROR"}, "请先指定 人物身体 和 衣服")
            return {"CANCELLED"}
        if st.body == st.garment:
            self.report({"ERROR"}, "身体和衣服不能是同一个物体")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        arm = _arm(st.body, st.armature)
        pr = GF_Progress(100, "尺寸匹配")
        try:
            try:
                pr.step(10, "测量尺寸")
                if self.direction == "GARMENT":
                    res = scale_garment_to_body(st.body, st.garment, arm,
                                                st.scale_metric, st.guard_cuff,
                                                st.align_positions)
                    who = "衣服 → 人物"
                else:
                    if arm is None:
                        self.report({"ERROR"}, "找不到人物骨架，无法缩放人物")
                        return {"CANCELLED"}
                    res = scale_body_to_garment(st.body, st.garment, arm,
                                                st.scale_metric, st.guard_cuff,
                                                st.align_positions)
                    who = "人物 → 衣服"
                pr.step(90, "写入缩放")
            except ValueError as e:
                self.report({"ERROR"}, str(e))
                print("[GarmentFit] 尺寸匹配失败:", e)
                return {"CANCELLED"}
            pr.end("完成")
        finally:
            if pr.wm:
                try:
                    pr.wm.progress_end()
                except Exception:
                    pass
            if pr.pct < 0.999:
                _ov_hide()

        msg = "%s x%.3f" % (who, res["k"])
        if res.get("capped"):
            msg += "（按基准应为 x%.3f，已限制为袖口不盖手掌）" % res["k_nominal"]
        if res.get("align"):
            msg += " · 中心已对齐"
        if res.get("detached"):
            msg += " · 已解除衣服绑定"
        if res.get("shape_keys"):
            msg += " · 含形态键只改了 Basis"
        st.scale_info = msg
        st.stage = "尺寸匹配: " + msg
        self.report({"INFO"}, msg)
        print("[GarmentFit] 尺寸匹配:", msg)
        toast("尺寸匹配完成: " + msg, 5.0)
        return {"FINISHED"}


class GF_OT_trim_body(bpy.types.Operator):
    """删除衣服上与人物重合的多余人体部位（手 / 脚 / 头）"""
    bl_idname = "garment_fit.trim_body"
    bl_label = "删除多余人体部位"
    bl_description = "按人物的顶点组找出衣服上焊着的手/脚/头，把重合的那层面删掉"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.body or not st.garment:
            self.report({"ERROR"}, "请先指定 人物身体 和 衣服")
            return {"CANCELLED"}
        if st.body == st.garment:
            self.report({"ERROR"}, "身体和衣服不能是同一个物体")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        arm = _arm(st.body, st.armature)
        pr = GF_Progress(100, "清理人体部位")
        try:
            try:
                res = trim_body_parts(st.garment, st.body, arm, st.trim_hands,
                                      st.trim_feet, st.trim_head,
                                      st.trim_margin / 1000.0,
                                      st.trim_cut, st.trim_cut_radius / 100.0,
                                      progress=pr, p0=5, p1=90)
            except ValueError as e:
                self.report({"ERROR"}, str(e))
                print("[GarmentFit] 清理失败:", e)
                return {"CANCELLED"}
        finally:
            if pr.wm:
                try:
                    pr.wm.progress_end()
                except Exception:
                    pass
            if pr.pct < 0.999:
                _ov_hide()

        if res["faces"] == 0:
            msg = "没找到与人物重合的部位（容差 %.0f mm）" % (res["margin"] * 1000.0)
            if res["gates"] == 0:
                msg += " · 没找到骨架的腕/踝关节"
        else:
            msg = "删除 %d 面，剩 %d 面（容差 %.0f mm，切断半径 %.0f cm）" % (
                res["faces"], res["left_faces"], res["margin"] * 1000.0,
                res["cut_radius"] * 100.0)
        if res["missing"]:
            msg += " · 没找到: " + "/".join(res["missing"])
        st.trim_info = msg
        st.stage = "清理人体部位: " + msg
        self.report({"INFO"}, msg)
        print("[GarmentFit] 清理人体部位:", msg)
        toast("清理完成: " + msg, 5.0)
        return {"FINISHED"}


class GF_OT_pose_only(bpy.types.Operator):
    """只校正手臂姿态(需要已有权重)"""
    bl_idname = "garment_fit.arm_pose"
    bl_label = "仅校正手臂姿态"

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.garment or not st.body:
            self.report({"ERROR"}, "请先指定身体和衣服")
            return {"CANCELLED"}
        arm = _arm(st.body, st.armature)
        if arm is None:
            self.report({"ERROR"}, "找不到骨架")
            return {"CANCELLED"}
        if not len(st.garment.vertex_groups):
            self.report({"ERROR"}, "衣服还没有顶点组，请先执行一键匹配或仅权重")
            return {"CANCELLED"}
        pr = GF_Progress(100, "手臂姿态校正")
        try:
            fix_arm_pose(st.garment, st.body, arm, st.arm_max_rot, st.arm_strength,
                         st.arm_max_move / 100.0, st.arm_iterations,
                         st.arm_rigid_chunks, st.arm_str_up, st.arm_str_lo,
                         st.weight_smooth, st.arm_str_cl, pr, 0, 100)
            pr.end("姿态校正完成")
        finally:
            if pr.wm:
                pr.wm.progress_end()
            if pr.pct < 0.999:
                _ov_hide()
        toast("手臂姿态校正完成", 4.0)
        return {"FINISHED"}


class GF_OT_weights_only(bpy.types.Operator):
    """只传权重 + 绑定"""
    bl_idname = "garment_fit.weights_only"
    bl_label = "仅权重 + 绑定"

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.garment or not st.body:
            self.report({"ERROR"}, "请先指定身体和衣服")
            return {"CANCELLED"}
        arm = _arm(st.body, st.armature)
        if arm is None:
            self.report({"ERROR"}, "找不到骨架")
            return {"CANCELLED"}
        pr = GF_Progress(100, "传递权重")
        try:
            transfer_weights(st.garment, st.body, st.limit_weights, pr, 0, 90)
            bind(st.garment, arm)
            pr.end("权重完成")
        finally:
            if pr.wm:
                pr.wm.progress_end()
            if pr.pct < 0.999:
                _ov_hide()
        toast("权重传递 + 绑定完成（%d 个顶点组）" % len(st.garment.vertex_groups), 4.0)
        return {"FINISHED"}


class GF_OT_pick_body(bpy.types.Operator):
    """取选中物体为身体"""
    bl_idname = "garment_fit.pick_body"
    bl_label = "取选中为身体"

    def execute(self, context):
        ob = context.active_object
        if not ob or ob.type != "MESH":
            self.report({"ERROR"}, "请选中人物身体 Mesh")
            return {"CANCELLED"}
        context.scene.garment_fit.body = ob
        a = _arm(ob)
        if a:
            context.scene.garment_fit.armature = a
        return {"FINISHED"}


class GF_OT_pick_garment(bpy.types.Operator):
    """取选中物体为衣服"""
    bl_idname = "garment_fit.pick_garment"
    bl_label = "取选中为衣服"

    def execute(self, context):
        ob = context.active_object
        if not ob or ob.type != "MESH":
            self.report({"ERROR"}, "请选中衣服 Mesh")
            return {"CANCELLED"}
        context.scene.garment_fit.garment = ob
        return {"FINISHED"}


class GF_OT_export(bpy.types.Operator):
    """导出 FBX 给 UE5"""
    bl_idname = "garment_fit.export_fbx"
    bl_label = "导出 FBX (给 UE5)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.body and not st.garment:
            self.report({"ERROR"}, "请先指定身体或衣服")
            return {"CANCELLED"}
        arm = _arm(st.body, st.armature) if st.body else (st.armature if st.armature else None)
        if arm is None or arm.type != "ARMATURE":
            self.report({"ERROR"}, "找不到骨架")
            return {"CANCELLED"}

        scope = st.export_scope
        meshes = []
        if scope in ("GARMENT", "BOTH") and st.garment:
            meshes.append(st.garment)
            if st.export_sim:
                sim = bpy.data.objects.get(st.garment.name + "_Sim")
                if sim is not None:
                    meshes.append(sim)
        if scope in ("BODY", "BOTH") and st.body:
            meshes.append(st.body)
        if not meshes:
            self.report({"ERROR"}, "当前导出范围里没有网格")
            return {"CANCELLED"}

        pr = GF_Progress(100, "导出 FBX")
        exported = []
        try:
            sim = None
            if st.garment:
                sim = bpy.data.objects.get(st.garment.name + "_Sim")
                if sim is None:
                    # 容忍 .001/.002 这类重名后缀
                    base = st.garment.name.split(".")[0]
                    for o in bpy.data.objects:
                        if o.type == "MESH" and o.name.endswith("_Sim") and o.name.startswith(base):
                            sim = o
                            break
            if st.export_sim and st.export_split_sim and sim is not None and sim in meshes:
                # UE 的骨骼网格 FBX 导入只吃一个 mesh -> Sim 必须单独导一份
                meshes = [m for m in meshes if m != sim]
                n1 = export_fbx(meshes, arm, st.export_path, pr, 10, 55)
                exported.append((n1, st.export_path))
                base = st.export_path
                if base.lower().endswith(".fbx"):
                    base = base[:-4]
                sim_path = base + "_Sim.fbx"
                n2 = export_fbx([sim], arm, sim_path, pr, 60, 95)
                exported.append((n2, sim_path))
            else:
                n = export_fbx(meshes, arm, st.export_path, pr, 10, 100)
                exported.append((n, st.export_path))
            pr.end("导出完成")
        finally:
            if pr.wm:
                pr.wm.progress_end()
        for count, path in exported:
            self.report({"INFO"}, "已导出 %d 个网格 -> %s" % (count, bpy.path.abspath(path)))
            print("[GarmentFit] exported", count, "meshes ->", bpy.path.abspath(path))

        try:
            names = ", ".join(bpy.path.basename(bpy.path.abspath(p)) for _, p in exported)
            total = sum(n for n, _ in exported)
            toast("导出成功: %s (%d 个网格)" % (names, total), 6.0)
        except Exception as e:
            print("[GarmentFit] toast export:", e)
        return {"FINISHED"}


class GF_OT_cloth(bpy.types.Operator):
    """布料预处理: 焊接碎块 + 生成布料遮罩 + 生成低模 Sim 网格"""
    bl_idname = "garment_fit.cloth_prep"
    bl_label = "布料预处理"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.garment:
            self.report({"ERROR"}, "请先指定衣服")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        arm = _arm(st.body, st.armature) if st.body else st.armature
        pr = GF_Progress(100, "布料优化")
        try:
            res = cloth_prep(st.garment, arm, st.weld_mm / 1000.0, st.sim_tris,
                             st.make_sim_mesh, st.pin_top / 100.0,
                             st.pin_waist_ratio, st.pin_shoulder / 100.0,
                             st.mask_smooth, pr, 5, 95)
            pr.end("布料预处理完成")
        finally:
            if pr.wm:
                try:
                    pr.wm.progress_end()
                except Exception:
                    pass
            if pr.pct < 0.999:
                _ov_hide()
        msg = ("碎块 %d→%d | 顶点 %d→%d | 面 %d→%d | 可模拟占比 %.0f%%"
               % (res["shells"][0], res["shells"][1], res["verts"][0], res["verts"][1],
                  res["tris"][0], res["tris"][1], res["sim_ratio"] * 100))
        if "sim_tris" in res:
            msg += " | Sim %d 顶点/%d 面" % (res["sim_verts"], res["sim_tris"])
        self.report({"INFO"}, msg)
        print("[GarmentFit]", msg)
        return {"FINISHED"}


class GF_OT_restore(bpy.types.Operator):
    """把衣服还原到第一次执行前的原始几何"""
    bl_idname = "garment_fit.restore_orig"
    bl_label = "还原衣服几何"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.garment:
            self.report({"ERROR"}, "请先指定衣服")
            return {"CANCELLED"}
        if _restore_orig(st.garment):
            self.report({"INFO"}, "已还原到原始几何")
            toast("已还原到原始几何", 4.0)
        else:
            self.report({"WARNING"}, "没有找到原始几何快照")
            toast("没有找到原始几何快照", 4.0)
        return {"FINISHED"}


class GF_OT_report(bpy.types.Operator):
    """检查绑定状态"""
    bl_idname = "garment_fit.report"
    bl_label = "检查绑定状态"

    def execute(self, context):
        g = context.scene.garment_fit.garment
        if not g:
            return {"CANCELLED"}
        has_arm = any(m.type == "ARMATURE" for m in g.modifiers)
        weighted = sum(1 for v in g.data.vertices if v.groups)
        msg = "顶点 %d | 组 %d | Armature %s | 有权重 %d" % (
            len(g.data.vertices), len(g.vertex_groups), "有" if has_arm else "无", weighted)
        self.report({"INFO"}, msg)
        print("[GarmentFit]", g.name, msg)
        toast(msg, 6.0)
        return {"FINISHED"}


class GF_OT_recenter(bpy.types.Operator):
    """快速把衣服的位置重置到人物中心（只改位置，不改尺寸/旋转）"""
    bl_idname = "garment_fit.recenter"
    bl_label = "对齐到人物中心"
    bl_description = "把衣服的包围盒中心平移到人物包围盒中心（只移动位置，不改大小）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        st = context.scene.garment_fit
        if not st.body or not st.garment:
            self.report({"ERROR"}, "请先指定 人物身体 和 衣服")
            return {"CANCELLED"}
        if st.body == st.garment:
            self.report({"ERROR"}, "身体和衣服不能是同一个物体")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        dist = 0.0
        pr = GF_Progress(100, "对齐到人物中心")
        try:
            pr.step(10, "测量包围盒中心")
            Wb = _wverts(st.body).astype(np.float64)
            Wg = _wverts(st.garment).astype(np.float64)
            cb = _bbox_stats(Wb)[3]
            cg = _bbox_stats(Wg)[3]
            delta = cb - cg
            dist = float(np.linalg.norm(delta))
            pr.step(60, "移动 %.1f cm" % (dist * 100))
            if dist > 1e-6:
                _write_world(st.garment, Wg + delta)
                st.garment.update_tag()
            pr.end("完成")
        finally:
            if pr.wm:
                try:
                    pr.wm.progress_end()
                except Exception:
                    pass
            if pr.pct < 0.999:
                _ov_hide()

        msg = ("已对齐到人物中心（移动 %.1f cm）" % (dist * 100)) if dist > 0.0005 \
            else "衣服已经在人物中心（0 cm）"
        st.stage = msg
        self.report({"INFO"}, msg)
        print("[GarmentFit]", msg)
        toast(msg, 4.0)
        return {"FINISHED"}


# ================================================================== UI
class GF_PT_panel(bpy.types.Panel):
    bl_label = "Garment Fit"
    bl_idname = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout

        # ---- 对象(必须先选) ----
        box = lay.box()
        box.label(text="对象", icon="OBJECT_DATA")
        row = box.row(align=True)
        row.prop(st, "body", text="身体")
        row.operator("garment_fit.pick_body", text="", icon="EYEDROPPER")
        row = box.row(align=True)
        row.prop(st, "garment", text="衣服")
        row.operator("garment_fit.pick_garment", text="", icon="EYEDROPPER")
        box.prop(st, "armature", text="骨架")

        # ---- 方向 + 主按钮 ----
        box = lay.box()
        box.label(text="匹配方向", icon="ARROW_LEFTRIGHT")
        box.row(align=True).prop(st, "match_mode", expand=True)
        lay.scale_y = 1.5
        lay.operator("garment_fit.match", icon="MOD_ARMATURE")
        lay.scale_y = 1.0
        row = lay.row(align=True)
        row.operator("garment_fit.arm_pose", icon="FILE_REFRESH")
        row.operator("garment_fit.weights_only", icon="GROUP_VERTEX")
        row = lay.row(align=True)
        row.operator("garment_fit.restore_orig", icon="LOOP_BACK")
        row.operator("garment_fit.report", icon="INFO")

        # ---- 尺寸匹配 ----
        box = lay.box()
        box.label(text="尺寸匹配 (统一缩放)", icon="FULLSCREEN_ENTER")
        row = box.row(align=True)
        op = row.operator("garment_fit.scale_match", text="人物 → 衣服")
        op.direction = "BODY"
        op = row.operator("garment_fit.scale_match", text="衣服 → 人物")
        op.direction = "GARMENT"
        box.operator("garment_fit.recenter", icon="SNAP_ON",
                     text="对齐到人物中心 (只移位置)")
        if st.scale_info:
            box.label(text=st.scale_info, icon="CHECKMARK")

        # ---- 清理 / 布料 ----
        box = lay.box()
        box.label(text="清理 / 布料", icon="TRASH")
        box.operator("garment_fit.trim_body", icon="TRASH",
                     text="删除多余人体部位 (手/脚/头)")
        box.operator("garment_fit.cloth_prep", icon="MOD_CLOTH",
                     text="布料优化 (焊接+遮罩+低模)")
        if st.trim_info:
            box.label(text=st.trim_info, icon="INFO")

        # ---- 进度(只在执行中出现, 跑完自动隐藏) ----
        if st.stage and st.progress < 0.999:
            box = lay.box()
            box.prop(st, "progress", text="%.0f%%" % (st.progress * 100), slider=True)
            box.label(text=st.stage, icon="TIME")

        # ---- 导出 ----
        lay.separator()
        lay.scale_y = 1.3
        lay.operator("garment_fit.export_fbx", icon="FILE_TICK", text="导出 FBX (给 UE5)")
        lay.scale_y = 1.0
        lay.label(text="展开下方折叠项可调参数", icon="TRIA_DOWN")


class GF_PT_match_opts(bpy.types.Panel):
    bl_label = "匹配 / 蒙皮选项"
    bl_idname = "GF_PT_match_opts"
    bl_parent_id = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 0

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout
        lay.prop(st, "auto_scale")
        if st.match_mode == "BODY":
            lay.prop(st, "bake_body")
        lay.prop(st, "do_weights")
        col = lay.column(align=True)
        col.enabled = st.do_weights
        col.prop(st, "limit_weights")


class GF_PT_arm(bpy.types.Panel):
    bl_label = "手臂姿态校正参数"
    bl_idname = "GF_PT_arm"
    bl_parent_id = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 1

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout
        lay.prop(st, "do_arm_pose")
        col = lay.column(align=True)
        col.enabled = st.do_arm_pose
        col.prop(st, "weight_smooth")
        col.prop(st, "arm_max_rot")
        col.prop(st, "arm_max_move")
        col.prop(st, "arm_strength")
        col.prop(st, "arm_str_cl")
        col.prop(st, "arm_str_up")
        col.prop(st, "arm_str_lo")
        col.prop(st, "arm_iterations")
        col.prop(st, "arm_rigid_chunks")


class GF_PT_fit(bpy.types.Panel):
    bl_label = "防穿模参数"
    bl_idname = "GF_PT_fit"
    bl_parent_id = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 2

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout
        lay.prop(st, "do_fit")
        col = lay.column(align=True)
        col.enabled = st.do_fit
        col.prop(st, "fit_gap")
        col.prop(st, "fit_max_push")


class GF_PT_scale(bpy.types.Panel):
    bl_label = "尺寸匹配设置"
    bl_idname = "GF_PT_scale"
    bl_parent_id = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 3

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout
        lay.prop(st, "scale_metric", text="基准")
        lay.prop(st, "guard_cuff")
        lay.prop(st, "align_positions")


class GF_PT_trim(bpy.types.Panel):
    bl_label = "清理设置"
    bl_idname = "GF_PT_trim"
    bl_parent_id = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 4

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout
        row = lay.row(align=True)
        row.prop(st, "trim_hands")
        row.prop(st, "trim_feet")
        row.prop(st, "trim_head")
        lay.prop(st, "trim_margin")
        lay.prop(st, "trim_cut")
        col = lay.column(align=True)
        col.enabled = st.trim_cut
        col.prop(st, "trim_cut_radius")


class GF_PT_cloth(bpy.types.Panel):
    bl_label = "布料预处理设置"
    bl_idname = "GF_PT_cloth"
    bl_parent_id = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 5

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout
        lay.prop(st, "do_cloth")
        col = lay.column(align=True)
        col.enabled = st.do_cloth
        col.prop(st, "weld_mm")
        col.prop(st, "make_sim_mesh")
        col.prop(st, "sim_tris")
        col.prop(st, "pin_top")
        col.prop(st, "pin_waist_ratio")
        col.prop(st, "pin_shoulder")
        col.prop(st, "mask_smooth")


class GF_PT_export(bpy.types.Panel):
    bl_label = "导出设置"
    bl_idname = "GF_PT_export"
    bl_parent_id = "GF_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Garment Fit"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 6

    def draw(self, context):
        st = context.scene.garment_fit
        lay = self.layout
        lay.row(align=True).prop(st, "export_scope", expand=True)
        if st.export_scope in ("GARMENT", "BOTH"):
            lay.prop(st, "export_sim")
            if st.export_sim:
                lay.prop(st, "export_split_sim")
        lay.prop(st, "export_path", text="")


CLASSES = (GF_Settings, GF_OT_match, GF_OT_scale_match, GF_OT_trim_body,
           GF_OT_pose_only, GF_OT_weights_only,
           GF_OT_pick_body, GF_OT_pick_garment, GF_OT_export,
           GF_OT_cloth, GF_OT_restore, GF_OT_report, GF_OT_recenter,
           GF_PT_panel, GF_PT_match_opts, GF_PT_arm, GF_PT_fit,
           GF_PT_scale, GF_PT_trim, GF_PT_cloth, GF_PT_export)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.garment_fit = bpy.props.PointerProperty(type=GF_Settings)


def unregister():
    del bpy.types.Scene.garment_fit
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
