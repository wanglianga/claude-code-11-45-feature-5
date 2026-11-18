"""空间授权治理路由：资料导入、范围解释、受限产品、分页、文件领取、交付追查。

权限分层：
- admin/operator：许可与位置资料导入、成员关系重建、内部审计（跨用户）；
- 授权用户（AuthUser Key）：仅能访问自己的求值/会话/任务/交付，错误信息不确认
  其他港区的站点名称、记录数或文件地址。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from . import access
from .db import SessionLocal
from .models import (
    AccessLicense,
    AuthUser,
    ClaimRecord,
    DeliveryRecord,
    ExtractionTask,
    LicenseMembership,
    RangeEvaluation,
    RangeEvaluationItem,
    SpatialCrs,
    SpatialZone,
    Station,
    StationInstallation,
)
from .schemas import (
    AuthUserIn,
    CrsIn,
    ExtractionIn,
    InstallationIn,
    LicenseIn,
    RevokeIn,
    ScopeIn,
    SessionIn,
    ZoneIn,
)
from .security import require_auth_user, require_role

router = APIRouter(prefix="/access", tags=["spatial-access-governance"])


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except access.ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _scope_dict(body: ScopeIn) -> tuple[dict, str]:
    data = body.model_dump()
    mode = data.pop("mode")
    return data, mode


# ---------------- 资料导入（admin/operator） ----------------

@router.post("/users", status_code=201)
def create_user(body: AuthUserIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        if s.execute(select(AuthUser).where(AuthUser.code == body.code)).scalar_one_or_none():
            raise HTTPException(409, "user code exists")
        import secrets
        user = AuthUser(code=body.code, name=body.name,
                        api_key=body.api_key or f"user-{secrets.token_urlsafe(16)}")
        s.add(user)
        s.commit()
        return {"id": user.id, "code": user.code, "api_key": user.api_key}
    finally:
        s.close()


@router.get("/users")
def list_users(_role: str = Depends(require_role("admin", "operator"))):
    s = SessionLocal()
    try:
        return [{"id": u.id, "code": u.code, "name": u.name, "active": u.active}
                for u in s.execute(select(AuthUser).order_by(AuthUser.id)).scalars()]
    finally:
        s.close()


@router.post("/crs", status_code=201)
def create_crs(body: CrsIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        crs = _handle(access.create_crs, s, body.code, body.name, body.length_unit)
        return {"id": crs.id, "code": crs.code, "length_unit": crs.length_unit}
    finally:
        s.close()


@router.get("/crs")
def get_crs(_role: str = Depends(require_role("admin", "operator"))):
    s = SessionLocal()
    try:
        rows = s.execute(select(SpatialCrs).order_by(SpatialCrs.id)).scalars()
        return [{"id": c.id, "code": c.code, "name": c.name, "length_unit": c.length_unit}
                for c in rows]
    finally:
        s.close()


@router.post("/zones", status_code=201)
def create_zone(body: ZoneIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        zone = _handle(
            access.create_zone, s, body.code, body.name,
            [p.model_dump() for p in body.polygons],
            body.valid_from, body.valid_to, body.boundary_rule)
        return {"id": zone.id, "code": zone.code, "version_no": zone.version_no,
                "boundary_rule": zone.boundary_rule}
    finally:
        s.close()


@router.get("/zones")
def list_zones(_role: str = Depends(require_role("admin", "operator"))):
    s = SessionLocal()
    try:
        return [
            {"id": z.id, "code": z.code, "version_no": z.version_no, "name": z.name,
             "boundary_rule": z.boundary_rule,
             "valid_from": z.valid_from.isoformat(),
             "valid_to": z.valid_to.isoformat() if z.valid_to else None,
             "polygon_count": len(z.polygons)}
            for z in s.execute(select(SpatialZone).order_by(SpatialZone.code, SpatialZone.version_no)).scalars()
        ]
    finally:
        s.close()


@router.post("/installations", status_code=201)
def create_installation(body: InstallationIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        inst = _handle(access.create_installation, s, body.station, body.x, body.y,
                       body.valid_from, body.valid_to, body.uncertainty_radius, body.note)
        return {"id": inst.id, "station_id": inst.station_id,
                "valid_from": inst.valid_from.isoformat(),
                "valid_to": inst.valid_to.isoformat() if inst.valid_to else None}
    finally:
        s.close()


@router.get("/installations")
def list_installations(station: str | None = None,
                       _role: str = Depends(require_role("admin", "operator"))):
    s = SessionLocal()
    try:
        q = select(StationInstallation).order_by(
            StationInstallation.station_id, StationInstallation.valid_from)
        if station:
            st = s.execute(select(Station).where(Station.code == station)).scalar_one_or_none()
            if st is None:
                raise HTTPException(404, "unknown station")
            q = q.where(StationInstallation.station_id == st.id)
        codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
        return [
            {"id": i.id, "station": codes.get(i.station_id), "x": i.x, "y": i.y,
             "uncertainty_radius": i.uncertainty_radius,
             "valid_from": i.valid_from.isoformat(),
             "valid_to": i.valid_to.isoformat() if i.valid_to else None, "note": i.note}
            for i in s.execute(q).scalars()
        ]
    finally:
        s.close()


@router.post("/licenses", status_code=201)
def create_license(body: LicenseIn, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        lic = _handle(access.create_license, s, body.model_dump())
        return {"id": lic.id, "code": lic.code, "effect": lic.effect,
                "zone_code": lic.zone_code, "min_grid_seconds": lic.min_grid_seconds,
                "revoked_at": lic.revoked_at.isoformat() if lic.revoked_at else None}
    finally:
        s.close()


@router.get("/licenses")
def list_licenses(_role: str = Depends(require_role("admin", "operator"))):
    s = SessionLocal()
    try:
        users = {u.id: u.code for u in s.execute(select(AuthUser)).scalars()}
        return [
            {"id": l.id, "code": l.code, "user": users.get(l.user_id), "effect": l.effect,
             "zone_code": l.zone_code,
             "obs_valid_from": l.obs_valid_from.isoformat(),
             "obs_valid_to": l.obs_valid_to.isoformat() if l.obs_valid_to else None,
             "stages": l.stages, "allowed_fields": l.allowed_fields,
             "min_grid_seconds": l.min_grid_seconds,
             "valid_from": l.valid_from.isoformat(),
             "valid_to": l.valid_to.isoformat() if l.valid_to else None,
             "revoked_at": l.revoked_at.isoformat() if l.revoked_at else None,
             "revoke_reason": l.revoke_reason}
            for l in s.execute(select(AccessLicense).order_by(AccessLicense.id)).scalars()
        ]
    finally:
        s.close()


@router.post("/licenses/{license_id}/revoke")
def revoke_license(license_id: int, body: RevokeIn,
                   _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        lic = _handle(access.revoke_license, s, license_id, body.reason)
        return {"id": lic.id, "revoked_at": lic.revoked_at.isoformat(),
                "revoke_reason": lic.revoke_reason}
    finally:
        s.close()


@router.post("/memberships/rebuild")
def rebuild_memberships(license_id: int | None = None,
                        _role: str = Depends(require_role("admin", "operator"))):
    """预计算成员关系（方案二）。重建期 ACTIVE 行缺失时 membership 求值直接 503。"""
    s = SessionLocal()
    try:
        return _handle(access.rebuild_memberships, s, license_id)
    finally:
        s.close()


@router.get("/memberships")
def list_memberships(license_id: int | None = None,
                     _role: str = Depends(require_role("admin", "operator"))):
    s = SessionLocal()
    try:
        q = select(LicenseMembership).order_by(LicenseMembership.id)
        if license_id:
            q = q.where(LicenseMembership.license_id == license_id)
        codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
        lics = {l.id: l.code for l in s.execute(select(AccessLicense)).scalars()}
        return [
            {"id": m.id, "license": lics.get(m.license_id),
             "station": codes.get(m.station_id),
             "valid_from": m.valid_from.isoformat(),
             "valid_to": m.valid_to.isoformat() if m.valid_to else None,
             "geometry_verdict": m.geometry_verdict, "status": m.status}
            for m in s.execute(q).scalars()
        ]
    finally:
        s.close()


# ---------------- 授权用户：范围解释与受限产品 ----------------

@router.post("/evaluate")
def evaluate(body: ScopeIn, user: AuthUser = Depends(require_auth_user)):
    """联合范围求值：逐条观测绑定位置与许可，返回放行/排除/不可确定明细。"""
    s = SessionLocal()
    try:
        scope, mode = _scope_dict(body)
        ev = _handle(access.evaluate_scope, s, user, scope, mode=mode)
        counts: dict[str, int] = {}
        items = s.execute(
            select(RangeEvaluationItem).where(RangeEvaluationItem.evaluation_id == ev.id)
        ).scalars()
        preview = []
        codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
        for it in items:
            counts[it.verdict] = counts.get(it.verdict, 0) + 1
            if it.verdict != "ALLOWED" and len(preview) < 50:
                preview.append({"station": codes.get(it.station_id),
                                "obs_id": it.obs_id,
                                "observed_at": it.observed_at.isoformat(),
                                "verdict": it.verdict, "reasons": it.reasons})
        return {"evaluation_id": ev.id, "mode": ev.mode, "scope_hash": ev.scope_hash,
                "cutoff_obs_id": ev.cutoff_obs_id, "counts": counts,
                "excluded_or_uncertain_preview": preview}
    finally:
        s.close()


@router.post("/recompute")
def recompute(body: ScopeIn, user: AuthUser = Depends(require_auth_user)):
    """授权约束下的重新聚合：后端按获准窗口重算值与有效覆盖，不先返回高频再降采样。"""
    s = SessionLocal()
    try:
        scope, mode = _scope_dict(body)
        ev = _handle(access.evaluate_scope, s, user, scope, mode=mode)
        result = _handle(access.recompute_restricted, s, ev)
        codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
        licenses = {l.id: l for l in s.execute(select(AccessLicense)).scalars()}
        out_stations = []
        for sid in sorted(result["stations"]):
            out_stations.append({
                "station": codes.get(sid),
                # 字段投影与文件清单同一套规则：未获准字段（含 aux）不输出，
                # API 层不得自行附加极值等辅助字段
                "cells": [
                    access.project_cell(c, scope["fields"], licenses)
                    for c in result["stations"][sid]
                ],
            })
        return {"evaluation_id": ev.id, "datum_version_id": result["datum_version_id"],
                "stations": out_stations,
                "uncertain_count": result["uncertain_count"],
                "excluded_count": result["excluded_count"],
                "aggregate": result.get("aggregate")}
    finally:
        s.close()


@router.post("/sessions", status_code=201)
def create_session(body: SessionIn, user: AuthUser = Depends(require_auth_user)):
    s = SessionLocal()
    try:
        scope, mode = _scope_dict(body.scope)
        ev = _handle(access.evaluate_scope, s, user, scope, mode=mode)
        sess = _handle(access.create_session, s, user, ev, body.page_size)
        return {"session_token": sess.token, "evaluation_id": ev.id,
                "scope_hash": ev.scope_hash, "page_size": sess.page_size}
    finally:
        s.close()


@router.get("/sessions/{token}/pages")
def get_page(token: str, cursor: str | None = None,
             user: AuthUser = Depends(require_auth_user)):
    s = SessionLocal()
    try:
        return _handle(access.get_page, s, user, token, cursor)
    finally:
        s.close()


# ---------------- 授权用户：文件生成与受控领取 ----------------

@router.post("/extractions", status_code=201)
def create_extraction(body: ExtractionIn, user: AuthUser = Depends(require_auth_user)):
    s = SessionLocal()
    try:
        scope, mode = _scope_dict(body.scope)
        task = _handle(access.create_extraction_task, s, user, scope, body.task_key, mode=mode)
        return {"task_id": task.id, "task_key": task.task_key, "status": task.status,
                "cell_count": task.cell_count, "station_codes": task.station_codes,
                "content_hash": task.content_hash}
    finally:
        s.close()


@router.get("/extractions")
def list_extractions(user: AuthUser = Depends(require_auth_user)):
    """普通用户只能看到自己的任务；不暴露暂存/文件路径。"""
    s = SessionLocal()
    try:
        rows = s.execute(
            select(ExtractionTask).where(ExtractionTask.user_id == user.id)
            .order_by(ExtractionTask.id)
        ).scalars()
        return [
            {"task_id": t.id, "task_key": t.task_key, "status": t.status,
             "cell_count": t.cell_count, "station_codes": t.station_codes,
             "content_hash": t.content_hash,
             "revoked_at": t.revoked_at.isoformat() if t.revoked_at else None,
             "ready_at": t.ready_at.isoformat() if t.ready_at else None}
            for t in rows
        ]
    finally:
        s.close()


@router.post("/extractions/{task_id}/promote")
def promote(task_id: int, user: AuthUser = Depends(require_auth_user)):
    s = SessionLocal()
    try:
        task = _handle(access.promote_task, s, user, task_id)
        # 只回受控领取入口，不回长期公开链接
        return {"task_id": task.id, "status": task.status,
                "claim_endpoint": f"/access/extractions/{task.id}/claim"}
    finally:
        s.close()


@router.post("/extractions/{task_id}/claim")
def claim(task_id: int, user: AuthUser = Depends(require_auth_user)):
    s = SessionLocal()
    try:
        return _handle(access.claim_task, s, user, task_id)
    finally:
        s.close()


@router.get("/my/deliveries")
def my_deliveries(user: AuthUser = Depends(require_auth_user)):
    s = SessionLocal()
    try:
        rows = s.execute(
            select(DeliveryRecord).where(DeliveryRecord.user_id == user.id)
            .order_by(DeliveryRecord.id)
        ).scalars()
        out = []
        for rec in rows:
            claims = s.execute(
                select(ClaimRecord).where(ClaimRecord.task_id == rec.task_id)
                .order_by(ClaimRecord.id)
            ).scalars()
            out.append({
                "delivery_id": rec.id, "task_id": rec.task_id,
                "content_hash": rec.content_hash, "delivered_scope": rec.delivered_scope,
                "license_ids": rec.license_ids, "station_codes": rec.station_codes,
                "cell_count": rec.cell_count, "created_at": rec.created_at.isoformat(),
                "claims": [{"result": c.result, "detail": c.detail,
                            "at": c.created_at.isoformat()} for c in claims],
            })
        return out
    finally:
        s.close()


@router.get("/my/evaluations/{evaluation_id}/audit")
def my_evaluation_audit(evaluation_id: int, user: AuthUser = Depends(require_auth_user)):
    s = SessionLocal()
    try:
        ev = s.get(RangeEvaluation, evaluation_id)
        if ev is None or ev.user_id != user.id:
            # 不确认他人求值是否存在
            raise HTTPException(404, "evaluation not found")
        return access.evaluation_audit(s, evaluation_id)
    finally:
        s.close()


# ---------------- 内部审计（admin 专有，跨用户） ----------------

@router.get("/audit/evaluations/{evaluation_id}")
def audit_evaluation(evaluation_id: int, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        return _handle(access.evaluation_audit, s, evaluation_id)
    finally:
        s.close()


@router.get("/audit/deliveries")
def audit_deliveries(user: str | None = None, _role: str = Depends(require_role("admin"))):
    s = SessionLocal()
    try:
        return access.delivery_audit(s, user)
    finally:
        s.close()


@router.get("/field-catalog")
def field_catalog(_role: str = Depends(require_role("admin", "operator"))):
    return {"fields": [{"name": k, "kind": v} for k, v in access.FIELD_CATALOG.items()]}
