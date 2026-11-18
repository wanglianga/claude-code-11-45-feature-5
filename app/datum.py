"""基准转换图：多段转换路径搜索。

规则：
- 边有向，height(to) = height(from) + offset_m，按观测时刻的生效区间过滤；
- 只走简单路径（节点不重复），循环路径不会把偏移重复相加；
- 多条有效路径得到互不相容的结果时保留冲突（CONFLICT），不擅自择一；
- 无路径时返回 NO_PATH，调用方不得用邻站偏移补齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

TOLERANCE_M = 1e-6  # 两条路径总偏移差超过该值视为不相容
MAX_DEPTH = 8       # 简单路径最大深度，防止超大图退化


@dataclass(frozen=True)
class Edge:
    id: int
    from_datum_id: int
    to_datum_id: int
    offset_m: float
    valid_from: datetime
    valid_to: datetime | None

    def valid_at(self, t: datetime) -> bool:
        if t < self.valid_from:
            return False
        if self.valid_to is not None and t >= self.valid_to:
            return False
        return True


@dataclass
class ConversionResult:
    status: str                      # OK / NO_PATH / CONFLICT
    total_offset: float | None       # OK 时的总偏移
    path: list[Edge] = field(default_factory=list)          # OK 时采用的路径
    candidates: list[dict] = field(default_factory=list)    # CONFLICT 时保留的全部候选


def _simple_paths(
    adj: dict[int, list[Edge]],
    src: int,
    dst: int,
) -> list[list[Edge]]:
    """DFS 枚举 src->dst 的全部简单路径（节点不重复，环不会重复计入）。"""
    results: list[list[Edge]] = []
    stack: list[tuple[int, list[Edge], frozenset[int]]] = [(src, [], frozenset({src}))]
    while stack:
        node, path, seen = stack.pop()
        if len(path) >= MAX_DEPTH:
            continue
        for e in adj.get(node, []):
            if e.to_datum_id in seen:
                continue  # 简单路径约束：不重复访问节点
            new_path = path + [e]
            if e.to_datum_id == dst:
                results.append(new_path)
            else:
                stack.append((e.to_datum_id, new_path, seen | {e.to_datum_id}))
    return results


def find_conversion(edges: list[Edge], src: int, dst: int, t: datetime) -> ConversionResult:
    """在 t 时刻有效的边上求 src->dst 的换算。"""
    if src == dst:
        return ConversionResult(status="OK", total_offset=0.0, path=[])

    valid = [e for e in edges if e.valid_at(t)]
    adj: dict[int, list[Edge]] = {}
    for e in valid:
        adj.setdefault(e.from_datum_id, []).append(e)

    paths = _simple_paths(adj, src, dst)
    if not paths:
        return ConversionResult(status="NO_PATH", total_offset=None)

    totals = [sum(e.offset_m for e in p) for p in paths]
    distinct: list[float] = []
    for tot in totals:
        if all(abs(tot - d) > TOLERANCE_M for d in distinct):
            distinct.append(tot)

    if len(distinct) == 1:
        # 所有路径一致：取边数最少的一条作为记录路径
        best = min(paths, key=len)
        return ConversionResult(status="OK", total_offset=sum(e.offset_m for e in best), path=best)

    # 互不相容：保留全部候选，不擅自选择
    candidates = [
        {
            "total_offset": tot,
            "path": [
                {
                    "edge_id": e.id,
                    "from_datum_id": e.from_datum_id,
                    "to_datum_id": e.to_datum_id,
                    "offset_m": e.offset_m,
                }
                for e in p
            ],
        }
        for p, tot in zip(paths, totals)
    ]
    return ConversionResult(status="CONFLICT", total_offset=None, candidates=candidates)
