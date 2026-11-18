"""重采样：把不规则/不同频率的换算后样本放到共同时间格上。

规则：
- 窗口边界明确：格元为 [window_start + k*grid, window_start + (k+1)*grid)；
- 每个样本的代表时段为相邻采样时刻中点界定的区间，并向两侧最多延伸 max_rep/2，
  长时间断档因此表现为未覆盖，而不会被两侧样本“拉平”；
- 重复序号在上游已去重，这里按样本 id 再次去重，重复不增加权重；
- 覆盖率 = 代表时段与格元交集总长 / 格元时长；达到阈值记 MEASURED；
- 覆盖不足时：仅当断档两侧为 GOOD 样本且间隔 <= max_interp_gap 才线性插值（INTERPOLATED）；
  长断档记 MISSING；含待核（SUSPECT/PENDING_JUMP）样本的格元记 PENDING，且不跨待核段插值；
- BAD 样本不参与权重，只记入质量原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

GOOD = "GOOD"
SUSPECT = "SUSPECT"
PENDING_JUMP = "PENDING_JUMP"
BAD = "BAD"
PENDING_FLAGS = {SUSPECT, PENDING_JUMP}


@dataclass(frozen=True)
class Sample:
    t: datetime
    value_m: float
    quality_flag: str
    obs_id: int


@dataclass
class CellResult:
    grid_start: datetime
    grid_seconds: int
    value_m: float | None
    grade: str                 # MEASURED/INTERPOLATED/PENDING/MISSING
    coverage_sec: float
    coverage_ratio: float
    sample_ids: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _rep_intervals(samples: list[Sample], max_rep_sec: float) -> list[tuple[datetime, datetime]]:
    """每个样本的代表时段：Voronoi 中点区间，并向两侧最多延伸 max_rep/2。"""
    half = timedelta(seconds=max_rep_sec / 2)
    n = len(samples)
    out = []
    for i, s in enumerate(samples):
        left = s.t - half
        right = s.t + half
        if i > 0:
            mid = samples[i - 1].t + (s.t - samples[i - 1].t) / 2
            left = max(left, mid)
        if i < n - 1:
            mid = s.t + (samples[i + 1].t - s.t) / 2
            right = min(right, mid)
        out.append((left, right))
    return out


def _overlap_sec(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds())


def resample_window(
    samples: list[Sample],
    window_start: datetime,
    window_end: datetime,
    grid_seconds: int,
    max_rep_sec: float,
    min_coverage: float,
    max_interp_gap_sec: float,
) -> list[CellResult]:
    # 防御性去重：同一 obs_id 只计一次（重复序号不增加权重）
    seen: set[int] = set()
    uniq: list[Sample] = []
    for s in sorted(samples, key=lambda x: x.t):
        if s.obs_id in seen:
            continue
        seen.add(s.obs_id)
        uniq.append(s)

    effective = [s for s in uniq if s.quality_flag != BAD]
    bad_ids = [s.obs_id for s in uniq if s.quality_flag == BAD]
    reps = dict()
    if effective:
        for s, (l, r) in zip(effective, _rep_intervals(effective, max_rep_sec)):
            reps[s.obs_id] = (l, r)

    cells: list[CellResult] = []
    t = window_start
    while t < window_end:
        cells.append(_build_cell(
            t, grid_seconds, effective, reps, bad_ids,
            min_coverage, max_interp_gap_sec, max_rep_sec,
        ))
        t += timedelta(seconds=grid_seconds)
    return cells


def _build_cell(
    grid_start: datetime,
    grid_seconds: int,
    effective: list[Sample],
    reps: dict[int, tuple[datetime, datetime]],
    bad_ids: list[int],
    min_coverage: float,
    max_interp_gap_sec: float,
    max_rep_sec: float,
) -> CellResult:
    grid_end = grid_start + timedelta(seconds=grid_seconds)
    center = grid_start + timedelta(seconds=grid_seconds / 2)

    weights: list[tuple[Sample, float]] = []
    coverage = 0.0
    for s in effective:
        l, r = reps[s.obs_id]
        w = _overlap_sec(l, r, grid_start, grid_end)
        if w > 0:
            weights.append((s, w))
            coverage += w
    coverage = min(coverage, float(grid_seconds))
    ratio = coverage / grid_seconds

    reasons: list[str] = []
    if bad_ids:
        reasons.append(f"excluded BAD samples: {bad_ids}")

    pending_in_cell = [s for s, _ in weights if s.quality_flag in PENDING_FLAGS]

    if ratio >= min_coverage:
        value = sum(s.value_m * w for s, w in weights) / sum(w for _, w in weights)
        if pending_in_cell:
            reasons.append("contains unverified (SUSPECT/PENDING_JUMP) samples; not smoothed")
            grade = "PENDING"
        else:
            grade = "MEASURED"
        return CellResult(grid_start, grid_seconds, round(value, 6), grade,
                          round(coverage, 3), round(ratio, 4),
                          [s.obs_id for s, _ in weights], reasons)

    # 覆盖不足：尝试短断档插值（仅限两侧均为 GOOD 且间隔不超阈值，且不跨待核段）
    before = [s for s in effective if s.quality_flag == GOOD and s.t <= center]
    after = [s for s in effective if s.quality_flag == GOOD and s.t >= center]
    if before and after:
        b, a = before[-1], after[0]
        gap = (a.t - b.t).total_seconds()
        between_pending = [
            s for s in effective
            if b.t < s.t < a.t and s.quality_flag in PENDING_FLAGS
        ]
        if 0 < gap <= max_interp_gap_sec and not between_pending:
            frac = (center - b.t).total_seconds() / gap
            value = b.value_m + (a.value_m - b.value_m) * frac
            reasons.append(
                f"short gap {int(gap)}s interpolated between GOOD samples {b.obs_id} and {a.obs_id}"
            )
            ids = sorted({*[s.obs_id for s, _ in weights], b.obs_id, a.obs_id})
            return CellResult(grid_start, grid_seconds, round(value, 6), "INTERPOLATED",
                              round(coverage, 3), round(ratio, 4), ids, reasons)

    # 待核段：不插成平滑实测曲线。仅当待核样本的代表时段处于本格元邻近范围
    # （间距不超过 max_rep/2，即该样本“本应”覆盖此格元）时才标记 PENDING，
    # 长断档之外的待核段不把远处的缺测格元染成待核。
    half_rep = max_rep_sec / 2

    def _rep_distance(s: Sample) -> float:
        l, r = reps[s.obs_id]
        if r < grid_start:
            return (grid_start - r).total_seconds()
        if l > grid_end:
            return (l - grid_end).total_seconds()
        return 0.0

    nearby_pending = [
        s for s in effective
        if s.quality_flag in PENDING_FLAGS and _rep_distance(s) < half_rep
    ]
    if nearby_pending or pending_in_cell:
        ids = sorted({*[s.obs_id for s, _ in weights], *[s.obs_id for s in nearby_pending]})
        reasons.append("unverified segment nearby; interpolation suppressed")
        return CellResult(grid_start, grid_seconds, None, "PENDING",
                          round(coverage, 3), round(ratio, 4), ids, reasons)

    reasons.append("insufficient coverage; gap too long to interpolate")
    return CellResult(grid_start, grid_seconds, None, "MISSING",
                      round(coverage, 3), round(ratio, 4),
                      [s.obs_id for s, _ in weights], reasons)
