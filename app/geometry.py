"""空间几何引擎：共同坐标系下的多边形（含孔洞）校验与点面归属。

设计约束（对应数据接口治理要求）：
- 所有几何使用资料声明的共同坐标系（CRS 由 SpatialCrs 登记，坐标为该 CRS 下的平面 x/y）；
  无法解释的坐标系直接抛 GeometryError（范围配置问题），不静默按全区域读取。
- 支持多个不相连区域（多个 SpatialZone）与内部孔洞（exterior + holes）。
- 粗略区域名称、包围盒命中、离港口最近都不能替代实际几何判断：bbox 仅作预过滤。
- 边界归属由 zone.boundary_rule 显式声明（INCLUDE/EXCLUDE），不靠默认猜测。
- 定位误差范围横跨授权边界且不能证明完全位于许可内时返回 UNCERTAIN，
  调用方必须单列“不可确定”，不得用中心点擅自放行。
- 无效多边形（自相交、重复点、零长线段、孔洞越界/重叠）直接抛 GeometryError。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

EPS = 1e-9
CIRCLE_SEGMENTS = 64  # 误差圆的近似多边形边数


class GeometryError(ValueError):
    """范围配置问题：无效多边形或无法解释的坐标系。"""


Point = tuple[float, float]
Ring = list[Point]


def _close_ring(ring: Ring) -> Ring:
    """逻辑闭合：调用方可传首尾相同或不闭合的环，内部统一补闭合点。"""
    if len(ring) < 3:
        raise GeometryError("ring must have at least 3 distinct points")
    pts = [(float(x), float(y)) for x, y in ring]
    if all(math.isfinite(v) for p in pts for v in p) is False:
        raise GeometryError("ring contains non-finite coordinates")
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    return pts


def _on_segment(p: Point, a: Point, b: Point, tol: float = EPS) -> bool:
    """p 是否落在段 ab 上（含共线重叠与端点）。"""
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > tol:
        return False
    return (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol)


def _segments_intersect(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """两段是否相交（含端点接触与共线重叠）。"""
    if _on_segment(a1, b1, b2) or _on_segment(a2, b1, b2) \
            or _on_segment(b1, a1, a2) or _on_segment(b2, a1, a2):
        return True

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2 = orient(a1, a2, b1), orient(a1, a2, b2)
    o3, o4 = orient(b1, b2, a1), orient(b1, b2, a2)
    return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0))


def ring_area(ring: Ring) -> float:
    pts = _close_ring(ring)
    area = 0.0
    for i in range(len(pts) - 1):
        area += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(area) / 2.0


def validate_ring(ring: Ring, name: str = "ring") -> Ring:
    """校验环：点数足够、无重复点、无零长线段、无自相交。返回逻辑闭合的环。"""
    pts = _close_ring(ring)
    if ring_area(pts) <= EPS:
        raise GeometryError(f"{name}: zero-area ring")
    unclosed = pts[:-1]
    # 重复点（除首尾闭合外）
    seen = set()
    for p in unclosed:
        key = (round(p[0], 12), round(p[1], 12))
        if key in seen:
            raise GeometryError(f"{name}: repeated vertex {p}")
        seen.add(key)
    n = len(unclosed)
    for i in range(n):
        a, b = unclosed[i], unclosed[(i + 1) % n]
        if abs(a[0] - b[0]) <= EPS and abs(a[1] - b[1]) <= EPS:
            raise GeometryError(f"{name}: zero-length segment at {a}")
    # 自相交：非相邻边不得相交（相邻边共享端点属合法）
    for i in range(n):
        a1, a2 = unclosed[i], unclosed[(i + 1) % n]
        for j in range(i + 1, n):
            b1, b2 = unclosed[j], unclosed[(j + 1) % n]
            adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            if adjacent:
                # 相邻段只允许在共享端点接触；共线重叠仍是自相交
                shared = a2 if j == i + 1 else a1
                other_a = a1 if j == i + 1 else a2
                other_b = b2 if j == i + 1 else b1
                if _on_segment(other_a, b1, b2) and other_a != shared:
                    raise GeometryError(f"{name}: adjacent edges overlap near {shared}")
                if _on_segment(other_b, a1, a2) and other_b != shared:
                    raise GeometryError(f"{name}: adjacent edges overlap near {shared}")
                continue
            if _segments_intersect(a1, a2, b1, b2):
                raise GeometryError(f"{name}: self-intersection between edges {i} and {j}")
    return pts


@dataclass(frozen=True)
class Polygon:
    exterior: Ring          # 已闭合
    holes: tuple[Ring, ...] # 均已闭合

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.exterior]
        ys = [p[1] for p in self.exterior]
        return min(xs), min(ys), max(xs), max(ys)


def validate_polygon(exterior: Ring, holes: list[Ring] | None = None) -> Polygon:
    """校验多边形：外环合法、孔洞合法且位于外环内、孔洞两两不重叠。"""
    ext = validate_ring(exterior, "exterior")
    hole_rings: list[Ring] = []
    for idx, h in enumerate(holes or []):
        hr = validate_ring(h, f"hole[{idx}]")
        # 孔洞每个顶点必须位于外环内部或边界上
        for p in hr[:-1]:
            if point_in_ring(p, ext) == "OUT":
                raise GeometryError(f"hole[{idx}] vertex {p} lies outside exterior")
        hole_rings.append(hr)
    # 孔洞之间不得相交/重叠（允许边界相接以外的任何相交都判无效，保守处理）
    for i in range(len(hole_rings)):
        for j in range(i + 1, len(hole_rings)):
            r1, r2 = hole_rings[i], hole_rings[j]
            for k in range(len(r1) - 1):
                a1, a2 = r1[k], r1[k + 1]
                for m in range(len(r2) - 1):
                    b1, b2 = r2[m], r2[m + 1]
                    if _segments_intersect(a1, a2, b1, b2):
                        raise GeometryError(f"holes {i} and {j} intersect")
            # 一个孔洞整体落在另一个孔洞内也算重叠
            if point_in_ring(r2[0], r1) != "OUT":
                raise GeometryError(f"holes {i} and {j} overlap")
    return Polygon(exterior=ext, holes=tuple(hole_rings))


def point_in_ring(p: Point, ring: Ring) -> str:
    """射线法。返回 IN / OUT / BOUNDARY。ring 需已闭合。"""
    x, y = p
    for i in range(len(ring) - 1):
        if _on_segment(p, ring[i], ring[i + 1]):
            return "BOUNDARY"
    inside = False
    n = len(ring) - 1
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[i + 1]
        if ((ay > y) != (by > y)):
            x_cross = ax + (bx - ax) * (y - ay) / (by - ay)
            if x_cross > x:
                inside = not inside
    return "IN" if inside else "OUT"


def classify_point(p: Point, polygon: Polygon, boundary_rule: str = "INCLUDE") -> str:
    """点对单个多边形的归属：IN / OUT / BOUNDARY（边界按规则归属，由调用方折算）。"""
    minx, miny, maxx, maxy = polygon.bbox()
    # 包围盒仅作预过滤：落在 bbox 外可快速排除，命中 bbox 绝不等于命中几何
    if not (minx - EPS <= p[0] <= maxx + EPS and miny - EPS <= p[1] <= maxy + EPS):
        return "OUT"
    status = point_in_ring(p, polygon.exterior)
    if status == "OUT":
        return "OUT"
    for h in polygon.holes:
        hs = point_in_ring(p, h)
        if hs == "IN":
            return "OUT"          # 孔洞内部 = 区外
        if hs == "BOUNDARY":
            return "BOUNDARY"     # 孔洞边界同样按边界规则处理
    return status  # IN 或 BOUNDARY


def resolve_boundary(status: str, boundary_rule: str) -> str:
    """把 BOUNDARY 按声明规则折算为 IN/OUT；非边界状态原样返回。"""
    if status != "BOUNDARY":
        return status
    if boundary_rule == "INCLUDE":
        return "IN"
    if boundary_rule == "EXCLUDE":
        return "OUT"
    raise GeometryError(f"unknown boundary_rule {boundary_rule!r}")


def _circle_polygon(center: Point, radius: float) -> Ring:
    if radius <= 0:
        raise GeometryError("uncertainty radius must be positive")
    pts = [
        (center[0] + radius * math.cos(2 * math.pi * k / CIRCLE_SEGMENTS),
         center[1] + radius * math.sin(2 * math.pi * k / CIRCLE_SEGMENTS))
        for k in range(CIRCLE_SEGMENTS)
    ]
    return pts + [pts[0]]


def classify_uncertainty(center: Point, radius: float, polygon: Polygon,
                         boundary_rule: str = "INCLUDE") -> str:
    """定位误差范围（圆形，CRS 长度单位）对单个多边形的归属。

    返回 IN（整个误差范围可证明位于多边形内）/ OUT（整个位于区外）/
    UNCERTAIN（横跨边界或孔洞，不能证明完全位于许可内）。
    采用 {CIRCLE_SEGMENTS} 边近似圆：所有采样顶点都在区内才判 IN，
    都在区外才判 OUT，其余一律 UNCERTAIN——不以中心点擅自放行。
    """
    if radius <= 0:
        return resolve_boundary(classify_point(center, polygon, boundary_rule), boundary_rule)
    ring = _circle_polygon(center, radius)
    verdicts = set()
    for p in ring[:-1]:
        raw = classify_point(p, polygon, boundary_rule)
        verdicts.add(resolve_boundary(raw, boundary_rule))
    center_raw = resolve_boundary(classify_point(center, polygon, boundary_rule), boundary_rule)
    verdicts.add(center_raw)
    if verdicts == {"IN"}:
        return "IN"
    if verdicts == {"OUT"}:
        return "OUT"
    return "UNCERTAIN"


def bbox_hit(p: Point, polygon: Polygon) -> bool:
    """仅用于预过滤统计/快速排除，禁止作为授权命中依据。"""
    minx, miny, maxx, maxy = polygon.bbox()
    return minx <= p[0] <= maxx and miny <= p[1] <= maxy
