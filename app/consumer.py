"""模拟消费端：独立进程运行的 FastAPI 应用（默认 8001 端口）。

行为契约：
- 按 delivery_key 去重：同一交付标识重复投递只应用一次，再次确认（ack）；
- 应用时按 publish_seq 排序保护：只接受比当前状态更新的产品，
  慢重试的旧版本不会把接收方恢复成旧值；
- 故障标志：offline -> 503；drop_ack -> 应用并落库后返回 500（模拟确认包丢失）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from . import config
from .db import SessionLocal, init_db
from .models import ConsumerFlag, ConsumerInbox, ConsumerState
from .security import require_role

app = FastAPI(title="Tide Product Simulated Consumer", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _flag(session, key: str) -> bool:
    row = session.get(ConsumerFlag, key)
    return row is not None and row.value.lower() == "true"


@app.post("/receive/{subscription_name}")
def receive(subscription_name: str, payload: dict,
            _role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        if _flag(session, config.FLAG_OFFLINE):
            raise HTTPException(status_code=503, detail="consumer offline (simulated)")

        key = payload.get("delivery_key")
        if not key:
            raise HTTPException(status_code=400, detail="missing delivery_key")

        inbox = session.get(ConsumerInbox, key)
        if inbox is not None:
            # 重复投递：不重复应用，直接再次确认
            inbox.receive_count += 1
            session.commit()
            if _flag(session, config.FLAG_DROP_ACK):
                raise HTTPException(status_code=500, detail="ack lost (simulated)")
            return {"ack": True, "delivery_key": key, "deduplicated": True}

        product = payload["product"]
        applied_cells = 0
        skipped_stale = 0
        for u in payload.get("units", []):
            grid_start = datetime.fromisoformat(u["grid_start"])
            if grid_start.tzinfo is None:
                grid_start = grid_start.replace(tzinfo=timezone.utc)
            row = session.execute(
                select(ConsumerState).where(
                    ConsumerState.series_key == product["series_key"],
                    ConsumerState.station_id == u["station_id"],
                    ConsumerState.grid_start == grid_start,
                )
            ).scalar_one_or_none()
            if row is not None and row.publish_seq >= product["publish_seq"]:
                skipped_stale += 1  # 慢重试/乱序到达的旧版本：不覆盖
                continue
            if row is None:
                row = ConsumerState(
                    series_key=product["series_key"],
                    station_id=u["station_id"],
                    grid_start=grid_start,
                )
                session.add(row)
            row.value_m = u["value_m"]
            row.quality_grade = u["quality_grade"]
            row.stage = product["stage"]
            row.product_version = product["version_no"]
            row.publish_seq = product["publish_seq"]
            row.updated_at = datetime.now(timezone.utc)
            applied_cells += 1

        session.add(ConsumerInbox(
            delivery_key=key,
            subscription_name=subscription_name,
            product_id=product["id"],
            publish_seq=product["publish_seq"],
            stage=product["stage"],
            payload_hash=product.get("payload_hash", ""),
            receive_count=1,
            applied=True,
        ))
        session.commit()

        if _flag(session, config.FLAG_DROP_ACK):
            # 确认包丢失：已应用并落库，但应答失败，等待重送（重送会被去重）
            raise HTTPException(status_code=500, detail="ack lost (simulated)")
        return {
            "ack": True,
            "delivery_key": key,
            "deduplicated": False,
            "applied_cells": applied_cells,
            "skipped_stale_cells": skipped_stale,
        }
    finally:
        session.close()


@app.get("/state")
def get_state(series_key: str | None = None,
              _role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        q = select(ConsumerState).order_by(
            ConsumerState.series_key, ConsumerState.station_id, ConsumerState.grid_start)
        if series_key:
            q = q.where(ConsumerState.series_key == series_key)
        rows = session.execute(q).scalars().all()
        return [
            {
                "series_key": r.series_key,
                "station_id": r.station_id,
                "grid_start": r.grid_start.isoformat(),
                "value_m": r.value_m,
                "quality_grade": r.quality_grade,
                "stage": r.stage,
                "product_version": r.product_version,
                "publish_seq": r.publish_seq,
            }
            for r in rows
        ]
    finally:
        session.close()


@app.get("/inbox")
def get_inbox(_role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        rows = session.execute(
            select(ConsumerInbox).order_by(ConsumerInbox.publish_seq)
        ).scalars().all()
        return [
            {
                "delivery_key": r.delivery_key,
                "subscription_name": r.subscription_name,
                "product_id": r.product_id,
                "publish_seq": r.publish_seq,
                "stage": r.stage,
                "receive_count": r.receive_count,
                "applied": r.applied,
                "received_at": r.received_at.isoformat(),
            }
            for r in rows
        ]
    finally:
        session.close()


class FlagIn(BaseModel):
    value: bool


@app.put("/flags/{key}")
def put_flag(key: str, body: FlagIn,
             _role: str = Depends(require_role("operator", "admin"))):
    session = SessionLocal()
    try:
        row = session.get(ConsumerFlag, key)
        if row is None:
            row = ConsumerFlag(key=key)
            session.add(row)
        row.value = "true" if body.value else "false"
        session.commit()
        return {"key": key, "value": row.value}
    finally:
        session.close()


@app.get("/flags")
def get_flags(_role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        return {r.key: r.value for r in session.execute(select(ConsumerFlag)).scalars()}
    finally:
        session.close()


@app.post("/reset")
def reset(_role: str = Depends(require_role("operator", "admin"))):
    session = SessionLocal()
    try:
        for model in (ConsumerState, ConsumerInbox, ConsumerFlag):
            session.query(model).delete()
        session.commit()
        return {"reset": True}
    finally:
        session.close()
