"""基准转换图测试：多段连接、循环不重复加、冲突保留、无路径不补齐、生效区间。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.datum import Edge, find_conversion

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
OPEN = None


def e(i, a, b, off, vf=T0, vt=OPEN):
    return Edge(id=i, from_datum_id=a, to_datum_id=b, offset_m=off, valid_from=vf, valid_to=vt)


def test_multi_segment_path_sums_offsets():
    edges = [e(1, 1, 2, 0.5), e(2, 2, 3, -0.2)]
    r = find_conversion(edges, 1, 3, T0)
    assert r.status == "OK"
    assert abs(r.total_offset - 0.3) < 1e-9
    assert [x.id for x in r.path] == [1, 2]


def test_cycle_does_not_double_add():
    # 1->2->3 与 3->2 形成环；简单路径约束下偏移不得重复相加
    edges = [e(1, 1, 2, 0.5), e(2, 2, 3, 0.4), e(3, 3, 2, -0.4), e(4, 1, 3, 0.9)]
    r = find_conversion(edges, 1, 3, T0)
    assert r.status == "OK"
    assert abs(r.total_offset - 0.9) < 1e-9  # 两条路径结果一致，取边数最少者


def test_conflicting_paths_are_kept():
    edges = [e(1, 1, 2, 0.5), e(2, 2, 3, 0.4), e(3, 1, 3, 0.9), e(4, 1, 3, 0.88)]
    r = find_conversion(edges, 1, 3, T0)
    assert r.status == "CONFLICT"
    totals = sorted(c["total_offset"] for c in r.candidates)
    assert len(r.candidates) == 3
    assert abs(totals[0] - 0.88) < 1e-9 and abs(totals[-1] - 0.9) < 1e-9


def test_no_path_not_filled_by_neighbor():
    edges = [e(1, 1, 2, 0.5)]  # 站点基准 9 没有任何到目标 2 的路径
    r = find_conversion(edges, 9, 2, T0)
    assert r.status == "NO_PATH"
    assert r.total_offset is None


def test_validity_interval_selects_edge():
    edges = [
        e(1, 1, 2, 0.832, vf=T0, vt=T1),   # 重测前
        e(2, 1, 2, 0.815, vf=T1, vt=OPEN),  # 重测后
    ]
    before = find_conversion(edges, 1, 2, datetime(2026, 5, 31, 23, 50, tzinfo=timezone.utc))
    after = find_conversion(edges, 1, 2, datetime(2026, 6, 1, 0, 10, tzinfo=timezone.utc))
    assert abs(before.total_offset - 0.832) < 1e-9
    assert abs(after.total_offset - 0.815) < 1e-9


def test_same_datum_zero_offset():
    assert find_conversion([], 5, 5, T0).total_offset == 0.0
