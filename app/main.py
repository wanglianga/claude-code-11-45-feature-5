"""潮位观测数据产品发布与订阅服务 API。

角色：admin（目录/订阅管理）、operator（观测接入/作业/确认/交付）、consumer（读取产品）。
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select, text

from .db import SessionLocal, init_db
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
)
from .schemas import (
    ConfirmIn,
    ConversionIn,
    DatumIn,
    IngestBatch,
    JobIn,
    StationDatumUpdate,
    StationIn,
    SubscriptionIn,
)
from .security import require_role
from .services import (
    ServiceError,
    confirm_product,
    create_job,
    current_datum_version,
    ingest_batch,
    new_datum_version,
    run_job,
    superseded_deliveries,
)
from .worker import run_once as worker_run_once
from .access_api import router as access_router

app = FastAPI(
    title="潮位观测数据产品发布与订阅服务",
    version="1.1.0",
    description=(
        "站点与基准登记、产品生成（初报/确认分阶段发布）、订阅管理与交付记录；"
        "空间授权治理（/access）：联合范围求值、授权约束下重新聚合、分页撤回控制与受控领取。"
        "基准转换带版本快照与生效区间；产品、清单与待交付任务同事务提交。"
    ),
)
app.include_router(access_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/admin/reset")
def admin_reset(_role: str = Depends(require_role("admin"))):
    """清空全部业务表并重置发布序号（仅演示/验收环境，ALLOW_RESET=1 时可用）。"""
    if os.getenv("ALLOW_RESET", "1") != "1":
        raise HTTPException(403, "reset disabled")
    from .db import Base, engine
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"truncate table {table.name} restart identity cascade"))
        conn.execute(text("alter sequence publish_seq restart with 1"))
    return {"reset": True}


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


# ---------------- 基准与站点目录 ----------------

@app.post("/datums", status_code=201)
def create_datum(body: DatumIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        if s.execute(select(Datum).where(Datum.code == body.code)).scalar_one_or_none():
            raise HTTPException(409, "datum code exists")
        d = Datum(code=body.code, name=body.name, description=body.description)
        s.add(d)
        s.commit()
        return {"id": d.id, "code": d.code}
    finally:
        s.close()


@app.get("/datums")
def list_datums(_role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        return [
            {"id": d.id, "code": d.code, "name": d.name, "description": d.description}
            for d in s.execute(select(Datum).order_by(Datum.id)).scalars()
        ]
    finally:
        s.close()


@app.post("/datum-conversions", status_code=201)
def create_conversion(body: ConversionIn, _role: str = Depends(require_role("admin"))):
    """新增转换边。每次变更生成新的基准版本；已有作业仍使用其固定版本。"""
    s = SessionLocal()
    try:
        def datum_id(code: str) -> int:
            d = s.execute(select(Datum).where(Datum.code == code)).scalar_one_or_none()
            if d is None:
                raise HTTPException(404, f"unknown datum {code}")
            return d.id

        version = new_datum_version(s, note=f"add conversion {body.from_datum}->{body.to_datum}")
        conv = DatumConversion(
            from_datum_id=datum_id(body.from_datum),
            to_datum_id=datum_id(body.to_datum),
            offset_m=body.offset_m,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            created_in_version=version.id,
            note=body.note,
        )
        s.add(conv)
        s.commit()
        return {"id": conv.id, "datum_version_id": version.id}
    finally:
        s.close()


@app.get("/datum-conversions")
def list_conversions(at_version: int | None = None,
                     _role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        version = at_version or current_datum_version(s).id
        rows = s.execute(
            select(DatumConversion).where(
                DatumConversion.created_in_version <= version,
                (DatumConversion.retired_in_version.is_(None))
                | (DatumConversion.retired_in_version > version),
            ).order_by(DatumConversion.id)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "from_datum_id": r.from_datum_id,
                "to_datum_id": r.to_datum_id,
                "offset_m": r.offset_m,
                "valid_from": r.valid_from.isoformat(),
                "valid_to": r.valid_to.isoformat() if r.valid_to else None,
                "created_in_version": r.created_in_version,
            }
            for r in rows
        ]
    finally:
        s.close()


@app.get("/datum-versions")
def list_datum_versions(_role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        return [
            {"id": v.id, "note": v.note, "created_at": v.created_at.isoformat()}
            for v in s.execute(select(DatumVersion).order_by(DatumVersion.id)).scalars()
        ]
    finally:
        s.close()


@app.post("/stations", status_code=201)
def create_station(body: StationIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        if s.execute(select(Station).where(Station.code == body.code)).scalar_one_or_none():
            raise HTTPException(409, "station code exists")
        datum = s.execute(select(Datum).where(Datum.code == body.datum)).scalar_one_or_none()
        if datum is None:
            raise HTTPException(404, f"unknown datum {body.datum}")
        version = new_datum_version(s, note=f"register station {body.code}")
        st = Station(
            code=body.code, name=body.name, region=body.region, lat=body.lat, lon=body.lon,
            unit=body.unit, positive=body.positive,
            nominal_interval_sec=body.nominal_interval_sec,
            device_status=body.device_status, datum_id=datum.id,
        )
        s.add(st)
        s.flush()
        s.add(StationDatumHistory(station_id=st.id, datum_id=datum.id, created_in_version=version.id))
        s.commit()
        return {"id": st.id, "code": st.code}
    finally:
        s.close()


@app.get("/stations")
def list_stations(region: str | None = None,
                  _role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        q = select(Station).order_by(Station.id)
        if region:
            q = q.where(Station.region == region)
        return [
            {
                "id": st.id, "code": st.code, "name": st.name, "region": st.region,
                "lat": st.lat, "lon": st.lon, "unit": st.unit, "positive": st.positive,
                "nominal_interval_sec": st.nominal_interval_sec,
                "device_status": st.device_status, "datum_id": st.datum_id,
            }
            for st in s.execute(q).scalars()
        ]
    finally:
        s.close()


@app.put("/stations/{station_id}/datum")
def update_station_datum(station_id: int, body: StationDatumUpdate,
                         _role: str = Depends(require_role("admin"))):
    """变更站点零点基准：写入历史并生成新基准版本；进行中的作业不受影响。"""
    s = SessionLocal()
    try:
        st = s.get(Station, station_id)
        if st is None:
            raise HTTPException(404, "station not found")
        datum = s.execute(select(Datum).where(Datum.code == body.datum)).scalar_one_or_none()
        if datum is None:
            raise HTTPException(404, f"unknown datum {body.datum}")
        version = new_datum_version(s, note=body.note or f"station {st.code} datum -> {body.datum}")
        cur = s.execute(
            select(StationDatumHistory).where(
                StationDatumHistory.station_id == station_id,
                StationDatumHistory.retired_in_version.is_(None),
            )
        ).scalar_one_or_none()
        if cur is not None:
            cur.retired_in_version = version.id
        st.datum_id = datum.id
        s.add(StationDatumHistory(station_id=station_id, datum_id=datum.id,
                                  created_in_version=version.id))
        s.commit()
        return {"station_id": station_id, "datum_id": datum.id, "datum_version_id": version.id}
    finally:
        s.close()


# ---------------- 观测接入 ----------------

@app.post("/observations:ingest")
def ingest(body: IngestBatch, _role: str = Depends(require_role("operator", "admin"))):
    """批量接入。重复报文（同站同序号同内容）去重；内容不同视为更正生成新版本。"""
    s = SessionLocal()
    try:
        return _handle(ingest_batch, s, [it.model_dump() for it in body.items])
    finally:
        s.close()


@app.get("/observations")
def list_observations(station: str, current_only: bool = True,
                      _role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        st = s.execute(select(Station).where(Station.code == station)).scalar_one_or_none()
        if st is None:
            raise HTTPException(404, "unknown station")
        q = select(RawObservation).where(RawObservation.station_id == st.id)
        if current_only:
            q = q.where(RawObservation.is_current.is_(True))
        q = q.order_by(RawObservation.observed_at)
        return [
            {
                "id": o.id, "sample_seq": o.sample_seq, "observed_at": o.observed_at.isoformat(),
                "value": o.value, "unit": o.unit, "quality_flag": o.quality_flag,
                "version_no": o.version_no, "is_current": o.is_current, "is_late": o.is_late,
                "received_at": o.received_at.isoformat(),
            }
            for o in s.execute(q).scalars()
        ]
    finally:
        s.close()


# ---------------- 产品作业与产品 ----------------

@app.post("/jobs", status_code=201)
def create_product_job(body: JobIn, _role: str = Depends(require_role("operator", "admin"))):
    """创建初报作业（固定创建时刻的基准版本）。job_key 幂等。"""
    s = SessionLocal()
    try:
        job = _handle(create_job, s, body, "PRELIMINARY")
        return {"id": job.id, "job_key": job.job_key, "status": job.status,
                "datum_version_id": job.datum_version_id}
    finally:
        s.close()


@app.post("/jobs/{job_id}/run", status_code=201)
def run_product_job(job_id: int, _role: str = Depends(require_role("operator", "admin"))):
    """执行作业：换算+重采样+发布（产品/单元/待交付任务同事务提交）。"""
    s = SessionLocal()
    try:
        product = _handle(run_job, s, job_id)
        return _product_out(s, product)
    finally:
        s.close()


@app.get("/jobs/{job_id}")
def get_job(job_id: int, _role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        job = s.get(ProductJob, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return {
            "id": job.id, "job_key": job.job_key, "region": job.region,
            "window_start": job.window_start.isoformat(), "window_end": job.window_end.isoformat(),
            "grid_seconds": job.grid_seconds, "stage": job.stage, "status": job.status,
            "datum_version_id": job.datum_version_id, "error": job.error,
        }
    finally:
        s.close()


def _product_out(s, p: Product) -> dict:
    return {
        "id": p.id, "series_key": p.series_key, "region": p.region,
        "stage": p.stage, "version_no": p.version_no, "publish_seq": p.publish_seq,
        "window_start": p.window_start.isoformat(), "window_end": p.window_end.isoformat(),
        "grid_seconds": p.grid_seconds, "target_datum_id": p.target_datum_id,
        "datum_version_id": p.datum_version_id, "supersedes_id": p.supersedes_id,
        "payload_hash": p.payload_hash, "created_at": p.created_at.isoformat(),
    }


@app.post("/products/{product_id}/confirm", status_code=201)
def confirm(product_id: int, body: ConfirmIn,
            _role: str = Depends(require_role("operator", "admin"))):
    """把初报签发为确认产品（新版本，阶段 CONFIRMED）。"""
    s = SessionLocal()
    try:
        product = _handle(confirm_product, s, product_id, body.job_key)
        return _product_out(s, product)
    finally:
        s.close()


@app.get("/products")
def list_products(region: str | None = None, stage: str | None = None,
                  _role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        q = select(Product).order_by(Product.publish_seq)
        if region:
            q = q.where(Product.region == region)
        if stage:
            q = q.where(Product.stage == stage)
        return [_product_out(s, p) for p in s.execute(q).scalars()]
    finally:
        s.close()


@app.get("/products/{product_id}")
def get_product(product_id: int,
                _role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        p = s.get(Product, product_id)
        if p is None:
            raise HTTPException(404, "product not found")
        return _product_out(s, p)
    finally:
        s.close()


@app.get("/products/{product_id}/units")
def get_units(product_id: int, station: str | None = None,
              _role: str = Depends(require_role("admin", "operator", "consumer"))):
    """产品单元：站点、原始样本、换算路径、质量状态与版本全部可溯。"""
    s = SessionLocal()
    try:
        q = select(ProductUnit).where(ProductUnit.product_id == product_id)
        if station:
            st = s.execute(select(Station).where(Station.code == station)).scalar_one_or_none()
            if st is None:
                raise HTTPException(404, "unknown station")
            q = q.where(ProductUnit.station_id == st.id)
        q = q.order_by(ProductUnit.station_id, ProductUnit.grid_start)
        codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
        return [
            {
                "station": codes.get(u.station_id), "station_id": u.station_id,
                "grid_start": u.grid_start.isoformat(), "grid_seconds": u.grid_seconds,
                "value_m": u.value_m, "quality_grade": u.quality_grade,
                "coverage_sec": u.coverage_sec, "coverage_ratio": u.coverage_ratio,
                "sample_count": u.sample_count, "source_sample_ids": u.source_sample_ids,
                "conversion_path": u.conversion_path, "conversion_status": u.conversion_status,
                "quality_reasons": u.quality_reasons, "datum_version_id": u.datum_version_id,
            }
            for u in s.execute(q).scalars()
        ]
    finally:
        s.close()


@app.get("/products/{product_id}/superseded-deliveries")
def get_superseded_deliveries(product_id: int,
                              _role: str = Depends(require_role("admin", "operator", "consumer"))):
    """列出该产品版本所取代的旧版本及其订阅交付记录。"""
    s = SessionLocal()
    try:
        return _handle(superseded_deliveries, s, product_id)
    finally:
        s.close()


# ---------------- 订阅与交付 ----------------

@app.post("/subscriptions", status_code=201)
def create_subscription(body: SubscriptionIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        if s.execute(select(Subscription).where(Subscription.name == body.name)).scalar_one_or_none():
            raise HTTPException(409, "subscription name exists")
        sub = Subscription(
            name=body.name, region=body.region, stages=body.stages,
            endpoint_url=body.endpoint_url, active=body.active,
        )
        s.add(sub)
        s.commit()
        return {"id": sub.id, "name": sub.name}
    finally:
        s.close()


@app.get("/subscriptions")
def list_subscriptions(_role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        return [
            {
                "id": sub.id, "name": sub.name, "region": sub.region, "stages": sub.stages,
                "endpoint_url": sub.endpoint_url, "cursor_seq": sub.cursor_seq,
                "active": sub.active,
            }
            for sub in s.execute(select(Subscription).order_by(Subscription.id)).scalars()
        ]
    finally:
        s.close()


@app.get("/deliveries")
def list_deliveries(status: str | None = None, subscription_id: int | None = None,
                    _role: str = Depends(require_role("admin", "operator", "consumer"))):
    s = SessionLocal()
    try:
        q = select(Delivery).order_by(Delivery.publish_seq, Delivery.id)
        if status:
            q = q.where(Delivery.status == status)
        if subscription_id:
            q = q.where(Delivery.subscription_id == subscription_id)
        return [
            {
                "id": d.id, "delivery_key": d.delivery_key,
                "subscription_id": d.subscription_id, "product_id": d.product_id,
                "publish_seq": d.publish_seq, "stage": d.stage, "status": d.status,
                "attempts": d.attempts,
                "acked_at": d.acked_at.isoformat() if d.acked_at else None,
                "last_error": d.last_error,
            }
            for d in s.execute(q).scalars()
        ]
    finally:
        s.close()


@app.post("/deliveries/{delivery_id}/retry")
def retry_delivery(delivery_id: int, _role: str = Depends(require_role("operator", "admin"))):
    """手动重试单条交付（重送保留同一 delivery_key）。"""
    s = SessionLocal()
    try:
        d = s.get(Delivery, delivery_id)
        if d is None:
            raise HTTPException(404, "delivery not found")
        if d.status == "ACKED":
            return {"delivery_key": d.delivery_key, "status": "ACKED", "note": "already confirmed"}
        d.status = "PENDING"
        s.commit()
        return {"delivery_key": d.delivery_key, "status": "PENDING"}
    finally:
        s.close()


@app.post("/worker/run-once")
def run_worker_once(_role: str = Depends(require_role("operator", "admin"))):
    """同步执行一轮交付（用于验收回放；生产由独立 worker 进程循环执行）。"""
    return worker_run_once()
