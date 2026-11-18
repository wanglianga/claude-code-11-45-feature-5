"""重采样测试：权重、覆盖、缺测、插值、待核段、重复序号。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.grid import Sample, resample_window

W0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
GRID = 600  # 10min
MAX_REP = 1200
MIN_COV = 0.5
MAX_INTERP = 1800


def s(minute, value, flag="GOOD", obs_id=None):
    return Sample(t=W0 + timedelta(minutes=minute), value_m=value,
                  quality_flag=flag, obs_id=obs_id if obs_id is not None else minute + 1)


def run(samples, cells=6):
    return resample_window(samples, W0, W0 + timedelta(minutes=10 * cells), GRID,
                           MAX_REP, MIN_COV, MAX_INTERP)


def test_regular_sampling_full_coverage():
    cells = run([s(m, 1.0) for m in range(0, 70, 10)])
    assert all(c.grade == "MEASURED" for c in cells)
    assert all(abs(c.value_m - 1.0) < 1e-9 for c in cells)
    assert all(c.coverage_ratio == 1.0 for c in cells)


def test_irregular_sampling_weighted_by_representation():
    # 样本 t=0 (v=0) 与 t=9 (v=1)：格元 [0,10) 内 0 覆盖 [0,4.5)，1 覆盖 [4.5,10)
    cells = run([s(0, 0.0), s(9, 1.0), s(20, 1.0), s(30, 1.0), s(40, 1.0), s(50, 1.0)], cells=1)
    c = cells[0]
    assert c.grade == "MEASURED"
    assert abs(c.value_m - (0.0 * 4.5 + 1.0 * 5.5) / 10.0) < 1e-6


def test_duplicate_sequence_does_not_add_weight():
    dup = [s(0, 1.0, obs_id=100), s(0, 1.0, obs_id=100)]  # 同一样本重复
    cells = run(dup + [s(m, 1.0) for m in range(10, 70, 10)])
    assert all(abs(c.value_m - 1.0) < 1e-9 for c in cells)
    # 若重复计入权重，首格覆盖会超过 100%
    assert cells[0].coverage_ratio <= 1.0


def test_long_gap_is_missing_not_interpolated():
    samples = [s(0, 1.0), s(10, 1.0)] + [s(120, 2.0), s(130, 2.0), s(140, 2.0)]
    cells = run(samples, cells=14)
    gap_cells = cells[3:10]  # 30..100 分钟为长断档
    assert all(c.grade == "MISSING" for c in gap_cells)
    assert all(c.value_m is None for c in gap_cells)


def test_short_gap_interpolated():
    # 0,10 GOOD；20 格无覆盖；30 起由 t=40 样本代表时段覆盖。断档 30min <= max_interp
    samples = [s(0, 1.0), s(10, 1.0), s(40, 2.0), s(50, 2.0), s(60, 2.0)]
    cells = run(samples, cells=6)
    assert cells[2].grade == "INTERPOLATED"
    # 中心 25min，在 t=10(v=1.0) 与 t=40(v=2.0) 之间线性插值
    assert abs(cells[2].value_m - (1.0 + (2.0 - 1.0) * (15 / 30))) < 1e-6
    assert cells[3].grade == "MEASURED"  # t=40 样本代表时段延伸覆盖


def test_pending_segment_not_smoothed():
    samples = [s(0, 1.0), s(10, 1.0), s(20, 9.9, "PENDING_JUMP"), s(30, 9.9, "PENDING_JUMP"),
               s(40, 1.0), s(50, 1.0)]
    cells = run(samples, cells=6)
    assert cells[2].grade == "PENDING"
    assert cells[3].grade == "PENDING"
    # 待核段保留原始读数供研判，但不标记为实测、不跨段插值
    assert abs(cells[2].value_m - 9.9) < 1e-6
    assert cells[0].grade == "MEASURED"
    assert cells[4].grade == "MEASURED"


def test_sparse_pending_segment_has_no_value():
    # 孤立待核样本：其代表时段覆盖所在格元（PENDING 带原始值），
    # 相邻无覆盖格元不跨待核段插值 -> PENDING 且无值
    samples = [s(0, 1.0), s(10, 1.0), s(25, 9.9, "PENDING_JUMP"), s(120, 1.0), s(130, 1.0)]
    cells = run(samples, cells=13)
    assert cells[2].grade == "PENDING"
    assert abs(cells[2].value_m - 9.9) < 1e-6
    assert cells[4].grade == "PENDING"
    assert cells[4].value_m is None
    assert cells[6].grade == "MISSING"  # 距待核样本超过插值阈值后按缺测处理


def test_bad_samples_excluded_from_weights():
    samples = [s(0, 1.0), s(10, 99.0, "BAD"), s(20, 1.0), s(30, 1.0), s(40, 1.0), s(50, 1.0)]
    cells = run(samples, cells=6)
    assert cells[1].grade in ("MEASURED", "INTERPOLATED")
    assert cells[1].value_m is None or abs(cells[1].value_m - 1.0) < 0.5
    assert any("BAD" in r for c in cells for r in c.reasons)
