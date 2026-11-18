"""业务服务：基准版本化、观测接入（去重/更正）、产品生成、确认签发、交付任务。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .datum import Edge, find_conversion
from .grid import Sample, resample_window
from .models import (
    Datum,
    DatumConversion,
    DatumVersion,
    Delivery,
    Product,
    ProductJob,
    ProductUnit,
    RawObservation,
    Station,
    StationDatumHistory,
    Subscription,
    publish_seq,
)

UNIT_TO_M = {"m": 1.0, "cm": 0.01, "mm": 0.001}


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


# ---------------- 基准版本 ----------------

def current_datum_version(s: Session) -> DatumVersion:
    v = s.execute(select(DatumVersion).order_by(DatumVersion.id.desc()).limit(1)).scalar_one_or_none()
    if v is None:
        v = DatumVersion(note="bootstrap")
        s.add(v)
        s.flush()
    return v


def new_datum_version(s: Session, note: str) -> DatumVersion:
    v = DatumVersion(note=note)
    s.add(v)
    s.flush()
    return v


def edges_at_version(s: Session, version_id: int) -> list[Edge]:
    """版本快照：created_in_version <= V 且未在 V 或之前退役的边。"""
    rows = s.execute(
        select(DatumConversion).where(
            DatumConversion.created_in_version <= version_id,
            (DatumConversion.retired_in_version.is_(None))
            | (DatumConversion.retired_in_version > version_id),
        )
    ).scalars().all()
    return [
        Edge(
            id=r.id,
            from_datum_id=r.from_datum_id,
            to_datum_id=r.to_datum_id,
            offset_m=r.offset_m,
            valid_from=_aware(r.valid_from),
            valid_to=_aware(r.valid_to) if r.valid_to else None,
        )
        for r in rows
    ]


def station_datum_at_version(s: Session, station_id: int, version_id: int) -> int | None:
    row = s.execute(
        select(StationDatumHistory)
        .where(
            StationDatumHistory.station_id == station_id,
            StationDatumHistory.created_in_version <= version_id,
            (StationDatumHistory.retired_in_version.is_(None))
            | (StationDatumHistory.retired_in_version > version_id),
        )
        .order_by(StationDatumHistory.created_in_version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.datum_id if row else None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------- 观测接入 ----------------

def _obs_hash(station_id: int, seq: int, observed_at: datetime, value: float, unit: str, flag: str) -> str:
    raw = f"{station_id}|{seq}|{_aware(observed_at).isoformat()}|{value!r}|{unit}|{flag}"
    return hashlib.sha256(raw.encode()).hexdigest()


def ingest_batch(s: Session, items: list[dict]) -> dict:
    """幂等接入：内容相同的重复报文去重；内容不同视为更正生成新观测版本。"""
    stations = {st.code: st for st in s.execute(select(Station)).scalars()}
    inserted, duplicated, corrected = 0, 0, 0
    late_windows: list[dict] = []
    results = []

    for it in items:
        st = stations.get(it["station"])
        if st is None:
            raise ServiceError(404, f"unknown station {it['station']}")
        observed_at = _aware(it["observed_at"])
        ch = _obs_hash(st.id, it["sample_seq"], observed_at, it["value"], it["unit"], it["quality_flag"])
        cur = s.execute(
            select(RawObservation).where(
                RawObservation.station_id == st.id,
                RawObservation.sample_seq == it["sample_seq"],
                RawObservation.is_current.is_(True),
            )
        ).scalar_one_or_none()

        if cur is not None and cur.content_hash == ch:
            duplicated += 1
            results.append({"station": st.code, "sample_seq": it["sample_seq"], "action": "duplicate", "obs_id": cur.id})
            continue

        # 到达时对应窗口已有提交的产品 => 迟到
        is_late = s.execute(
            select(func.count(Product.id)).where(
                Product.region == st.region,
                Product.window_start <= observed_at,
                Product.window_end > observed_at,
            )
        ).scalar() > 0

        version_no = 1
        action = "inserted"
        if cur is not None:
            version_no = cur.version_no + 1
            action = "corrected"
            corrected += 1
            # 先退役旧版本（部分唯一索引要求任一时刻仅一条 is_current）
            cur.is_current = False
            s.flush()
        else:
            inserted += 1

        obs = RawObservation(
            station_id=st.id,
            sample_seq=it["sample_seq"],
            observed_at=observed_at,
            value=it["value"],
            unit=it["unit"],
            quality_flag=it["quality_flag"],
            version_no=version_no,
            content_hash=ch,
            is_current=True,
            is_late=is_late,
        )
        s.add(obs)
        s.flush()
        if cur is not None:
            cur.superseded_by = obs.id
        if is_late:
            late_windows.append({"station": st.code, "observed_at": observed_at.isoformat()})
        results.append({"station": st.code, "sample_seq": it["sample_seq"], "action": action,
                        "obs_id": obs.id, "is_late": is_late})

    s.commit()
    return {
        "inserted": inserted,
        "duplicated": duplicated,
        "corrected": corrected,
        "late": late_windows,
        "results": results,
    }


# ---------------- 产品生成 ----------------

def series_key_of(region: str, ws: datetime, we: datetime, grid: int, target_datum_id: int) -> str:
    return f"{region}|{ws.isoformat()}|{we.isoformat()}|{grid}|datum:{target_datum_id}"


def create_job(s: Session, spec, stage: str, source_product_id: int | None = None) -> ProductJob:
    """创建作业并固定创建时刻的基准版本。job_key 幂等。"""
    existing = s.execute(select(ProductJob).where(ProductJob.job_key == spec.job_key)).scalar_one_or_none()
    if existing is not None:
        return existing

    target = s.execute(select(Datum).where(Datum.code == spec.target_datum)).scalar_one_or_none()
    if target is None:
        raise ServiceError(404, f"unknown target datum {spec.target_datum}")
    version = current_datum_version(s)
    job = ProductJob(
        job_key=spec.job_key,
        region=spec.region,
        window_start=_aware(spec.window_start),
        window_end=_aware(spec.window_end),
        grid_seconds=spec.grid_seconds,
        target_datum_id=target.id,
        stage=stage,
        datum_version_id=version.id,
        source_product_id=source_product_id,
        status="PENDING",
    )
    s.add(job)
    s.commit()
    s.refresh(job)
    return job


def run_job(s: Session, job_id: int, params: dict | None = None) -> Product:
    """执行作业：换算→重采样→产品+单元+待交付任务在同一事务提交。

    全程使用作业创建时固定的 datum_version_id 快照；重复执行已提交作业返回原产品。
    """
    job = s.get(ProductJob, job_id)
    if job is None:
        raise ServiceError(404, "job not found")
    if job.status == "COMMITTED":
        return s.execute(select(Product).where(Product.job_id == job.id)).scalar_one()
    if job.status == "RUNNING":
        raise ServiceError(409, "job is running")

    params = params or {}
    max_rep = params.get("max_rep_sec") or config.DEFAULT_MAX_REP_SEC
    min_cov = params.get("min_coverage") or config.DEFAULT_MIN_COVERAGE
    max_interp = params.get("max_interp_gap_sec") or config.DEFAULT_MAX_INTERP_GAP_SEC

    job.status = "RUNNING"
    s.flush()
    try:
        product = _compute_and_publish(s, job, max_rep, min_cov, max_interp)
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        job = s.get(ProductJob, job_id)
        job.status = "FAILED"
        job.error = str(exc)
        s.commit()
        raise

    job.status = "COMMITTED"
    job.finished_at = datetime.now(timezone.utc)
    s.commit()
    s.refresh(product)
    return product


def _compute_and_publish(s: Session, job: ProductJob, max_rep: float, min_cov: float, max_interp: float) -> Product:
    ws, we = _aware(job.window_start), _aware(job.window_end)
    edges = edges_at_version(s, job.datum_version_id)
    stations = s.execute(
        select(Station).where(Station.region == job.region).order_by(Station.id)
    ).scalars().all()

    series_key = series_key_of(job.region, ws, we, job.grid_seconds, job.target_datum_id)
    prev = s.execute(
        select(Product).where(Product.series_key == series_key).order_by(Product.version_no.desc()).limit(1)
    ).scalar_one_or_none()
    version_no = (prev.version_no + 1) if prev else 1

    product = Product(
        job_id=job.id,
        series_key=series_key,
        region=job.region,
        window_start=ws,
        window_end=we,
        grid_seconds=job.grid_seconds,
        target_datum_id=job.target_datum_id,
        stage=job.stage,
        version_no=version_no,
        publish_seq=s.execute(select(publish_seq.next_value())).scalar(),
        supersedes_id=prev.id if prev else None,
        datum_version_id=job.datum_version_id,
        payload_hash="",
    )
    s.add(product)
    s.flush()

    pad = timedelta(seconds=max_rep)
    all_units: list[ProductUnit] = []
    for st in stations:
        obs_rows = s.execute(
            select(RawObservation)
            .where(
                RawObservation.station_id == st.id,
                RawObservation.is_current.is_(True),
                RawObservation.observed_at >= ws - pad,
                RawObservation.observed_at < we + pad,
            )
            .order_by(RawObservation.observed_at)
        ).scalars().all()

        station_datum = station_datum_at_version(s, st.id, job.datum_version_id)
        samples: list[Sample] = []
        obs_conv: dict[int, dict] = {}        # obs_id -> 该样本的换算记录
        bad_conv_cells: dict[datetime, tuple[str, dict]] = {}

        for obs in obs_rows:
            t = _aware(obs.observed_at)
            base = obs.value * UNIT_TO_M[obs.unit]
            if st.positive == "down":
                base = -base  # 向下为正的读数翻转为水位向上为正
            if station_datum is None:
                bad_conv_cells[t] = ("NO_PATH", {
                    "reason": "station has no datum assignment at pinned version"})
                continue
            res = find_conversion(edges, station_datum, job.target_datum_id, t)
            if res.status == "OK":
                samples.append(Sample(t=t, value_m=base + res.total_offset,
                                      quality_flag=obs.quality_flag, obs_id=obs.id))
                obs_conv[obs.id] = {
                    "offset_m": res.total_offset,
                    "path": [
                        {"edge_id": e.id, "from_datum_id": e.from_datum_id, "to_datum_id": e.to_datum_id,
                         "offset_m": e.offset_m,
                         "valid_from": e.valid_from.isoformat(),
                         "valid_to": e.valid_to.isoformat() if e.valid_to else None}
                        for e in res.path
                    ],
                }
            else:
                info = {"candidates": res.candidates} if res.status == "CONFLICT" else {
                    "reason": "no conversion path from station datum to target datum"}
                bad_conv_cells[t] = (res.status, info)

        units = _grid_station(s, job, product.id, st, samples, obs_conv, bad_conv_cells,
                              edges, station_datum, max_rep, min_cov, max_interp, ws, we)
        all_units.extend(units)

    for u in all_units:
        s.add(u)
    s.flush()

    product.payload_hash = hashlib.sha256(
        json.dumps(
            [[u.station_id, u.grid_start.isoformat(), u.value_m, u.quality_grade] for u in all_units],
            sort_keys=True,
        ).encode()
    ).hexdigest()

    # 待交付任务与产品同事务写入
    subs = s.execute(
        select(Subscription).where(Subscription.region == job.region, Subscription.active.is_(True))
    ).scalars().all()
    for sub in subs:
        if job.stage not in (sub.stages or []):
            continue
        key = f"sub:{sub.id}-prod:{product.id}"
        exists = s.execute(select(Delivery).where(Delivery.delivery_key == key)).scalar_one_or_none()
        if exists is None:
            s.add(Delivery(
                delivery_key=key,
                subscription_id=sub.id,
                product_id=product.id,
                publish_seq=product.publish_seq,
                stage=product.stage,
            ))
    s.flush()
    return product


def _merge_conv_infos(infos: list[dict]) -> dict:
    """聚合格元内各样本的换算记录：一致时给单一偏移，跨界时保留全部。"""
    distinct: list[dict] = []
    for info in infos:
        if all(abs(info["offset_m"] - d["offset_m"]) > 1e-9 for d in distinct):
            distinct.append(info)
    if len(distinct) == 1:
        return distinct[0]
    return {
        "offset_m": None,
        "offsets": [d["offset_m"] for d in distinct],
        "paths": [d["path"] for d in distinct],
        "note": "cell mixes samples converted with different validity-interval offsets",
    }


def _grid_station(
    s: Session,
    job: ProductJob,
    product_id: int,
    st: Station,
    samples: list[Sample],
    obs_conv: dict[int, dict],
    bad_conv_cells: dict[datetime, tuple[str, dict]],
    edges: list,
    station_datum: int | None,
    max_rep: float,
    min_cov: float,
    max_interp: float,
    ws: datetime,
    we: datetime,
) -> list[ProductUnit]:
    cells = resample_window(samples, ws, we, job.grid_seconds, max_rep, min_cov, max_interp)
    units: list[ProductUnit] = []
    half_cell = timedelta(seconds=job.grid_seconds / 2)
    for c in cells:
        grade = c.grade
        conv_s = "OK"
        reasons = list(c.reasons)
        value = c.value_m

        # 格元换算路径 = 参与样本的换算记录聚合
        infos = [obs_conv[i] for i in c.sample_ids if i in obs_conv]
        path = _merge_conv_infos(infos) if infos else None

        if not samples:
            # 全站无成功换算样本：按格元中心时刻求值（无路径/冲突逐格保留）
            center = c.grid_start + half_cell
            if station_datum is None:
                conv_s, info = "NO_PATH", {"reason": "station has no datum assignment at pinned version"}
            else:
                res = find_conversion(edges, station_datum, job.target_datum_id, center)
                if res.status == "OK":
                    conv_s, info = "OK", {"offset_m": res.total_offset, "path": [
                        {"edge_id": e.id, "from_datum_id": e.from_datum_id, "to_datum_id": e.to_datum_id,
                         "offset_m": e.offset_m,
                         "valid_from": e.valid_from.isoformat(),
                         "valid_to": e.valid_to.isoformat() if e.valid_to else None}
                        for e in res.path]}
                else:
                    conv_s = res.status
                    info = {"candidates": res.candidates} if res.status == "CONFLICT" else {
                        "reason": "no conversion path from station datum to target datum"}
            path = info
            if conv_s != "OK":
                grade = conv_s
                value = None
                reasons.append(f"conversion {conv_s}: {json.dumps(info, ensure_ascii=False)[:400]}")
        else:
            # 窗口内个别时刻换算失败（如跨重测日后路径变化）：对应格元标记
            for t_bad, (status, info) in bad_conv_cells.items():
                if c.grid_start <= t_bad < c.grid_start + timedelta(seconds=c.grid_seconds):
                    conv_s = status
                    grade = status
                    value = None
                    path = info
                    reasons.append(f"conversion {status} at {t_bad.isoformat()}")

        if st.device_status != "ONLINE":
            reasons.append(f"device_status={st.device_status}")

        units.append(ProductUnit(
            product_id=product_id,
            station_id=st.id,
            grid_start=c.grid_start,
            grid_seconds=c.grid_seconds,
            value_m=value,
            quality_grade=grade,
            coverage_sec=c.coverage_sec,
            coverage_ratio=c.coverage_ratio,
            sample_count=len(c.sample_ids),
            source_sample_ids=c.sample_ids,
            conversion_path=path,
            conversion_status=conv_s,
            quality_reasons=reasons,
            datum_version_id=job.datum_version_id,
        ))
    return units


def confirm_product(s: Session, product_id: int, job_key: str) -> Product:
    """把初报签发为确认产品：以当前数据重新计算，阶段为 CONFIRMED 的新版本。"""
    src = s.get(Product, product_id)
    if src is None:
        raise ServiceError(404, "product not found")
    if src.stage != "PRELIMINARY":
        raise ServiceError(409, "only PRELIMINARY products can be confirmed")

    class _Spec:
        pass

    spec = _Spec()
    spec.job_key = job_key
    spec.region = src.region
    spec.window_start = src.window_start
    spec.window_end = src.window_end
    spec.grid_seconds = src.grid_seconds
    spec.target_datum = s.get(Datum, src.target_datum_id).code

    job = create_job(s, spec, stage="CONFIRMED", source_product_id=src.id)
    if job.status == "COMMITTED":
        return s.execute(select(Product).where(Product.job_id == job.id)).scalar_one()
    return run_job(s, job.id)


def superseded_deliveries(s: Session, product_id: int) -> dict:
    """列出新版本所取代的旧版本及其订阅交付记录。"""
    product = s.get(Product, product_id)
    if product is None:
        raise ServiceError(404, "product not found")
    chain: list[Product] = []
    cur = product.supersedes_id
    while cur is not None:
        p = s.get(Product, cur)
        if p is None:
            break
        chain.append(p)
        cur = p.supersedes_id
    out = []
    for p in chain:
        rows = s.execute(select(Delivery).where(Delivery.product_id == p.id)).scalars().all()
        out.append({
            "product_id": p.id,
            "version_no": p.version_no,
            "stage": p.stage,
            "publish_seq": p.publish_seq,
            "deliveries": [
                {
                    "delivery_key": d.delivery_key,
                    "subscription_id": d.subscription_id,
                    "status": d.status,
                    "attempts": d.attempts,
                    "acked_at": d.acked_at.isoformat() if d.acked_at else None,
                }
                for d in rows
            ],
        })
    return {"product_id": product.id, "version_no": product.version_no, "superseded": out}
