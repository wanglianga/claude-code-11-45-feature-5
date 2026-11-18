"""空间授权治理：联合范围求值、授权约束下的重新聚合、分页撤回控制与受控领取。

核心语义（任何一条都不得违反）：
- 许可是“一条完整条件”：区域 × 观测适用日期 × 产品阶段 × 允许字段 × 最细时间粒度。
  多许可只取各自完整条件成立的数据并集，绝不先并区域再套最宽粒度。
- DENY 在其适用范围优先；已撤回或尚未生效的许可不参与放行。
- 站点位置按观测发生时适用的安装记录解释；搬迁不改变旧观测的位置资格。
- 误差范围横跨授权边界且不能证明完全位于区内 -> UNCERTAIN，单列，不以中心点放行。
- 低频订阅在后端按获准窗口重算；跨界格元只用合法数据并声明覆盖不足或拒绝；
  辅助字段（极值/极值时刻等）同样受 allowed_fields 与粒度约束。
- 部分站点授权：重算为明确命名的受限范围产品，分母只数合法输入，缺测保留，不当零水位。
- 分页每页联查当前许可；范围收窄即终止旧会话；文件领取前再次联查，不发长期公开链接。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, geometry
from .datum import find_conversion
from .grid import Sample, _overlap_sec, _rep_intervals
from .models import (
    AccessLicense,
    AccessSession,
    AuthUser,
    ClaimRecord,
    DeliveryRecord,
    ExtractionTask,
    LicenseMembership,
    RangeEvaluation,
    RangeEvaluationItem,
    RawObservation,
    SpatialCrs,
    SpatialZone,
    Station,
    StationInstallation,
)
from .services import (
    UNIT_TO_M,
    ServiceError,
    _aware,
    current_datum_version,
    edges_at_version,
    station_datum_at_version,
)

# 可交付字段目录：aux 类字段可能暴露细粒度信息，必须显式获准，禁止用来绕过粒度限制。
FIELD_CATALOG: dict[str, str] = {
    "grid_start": "base",
    "grid_seconds": "base",
    "value_m": "base",
    "quality_grade": "base",
    "coverage_sec": "base",
    "coverage_ratio": "base",
    "sample_count": "base",
    "quality_reasons": "base",
    "source_sample_ids": "base",
    "max_m": "aux",
    "min_m": "aux",
    "extreme_max_at": "aux",
    "extreme_min_at": "aux",
}
DEFAULT_FIELDS = [f for f, kind in FIELD_CATALOG.items() if kind == "base"]
STAGES = {"PRELIMINARY", "CONFIRMED"}


# ============================== 基础工具 ==============================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dump_scope(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True, ensure_ascii=False, default=str)


_SCOPE_DT_KEYS = ("window_start", "window_end")


def _load_scope(scope: dict) -> dict:
    """JSONB 回读后 ISO 字符串还原为 datetime（求值时与读取时口径一致）。"""
    out = dict(scope)
    for k in _SCOPE_DT_KEYS:
        v = out.get(k)
        if isinstance(v, str):
            out[k] = datetime.fromisoformat(v)
    return out


def scope_hash_of(scope: dict, cutoff_obs_id: int) -> str:
    return hashlib.sha256(f"{_dump_scope(scope)}|cutoff:{cutoff_obs_id}".encode()).hexdigest()


def get_user(s: Session, user_id: int) -> AuthUser:
    user = s.get(AuthUser, user_id)
    if user is None or not user.active:
        raise ServiceError(403, "user inactive or not found")
    return user


def common_crs(s: Session) -> SpatialCrs:
    crs = s.execute(select(SpatialCrs).order_by(SpatialCrs.id).limit(1)).scalar_one_or_none()
    if crs is None:
        raise ServiceError(422, "range configuration problem: common CRS not declared")
    return crs


# ============================== 资料导入校验 ==============================

def create_crs(s: Session, code: str, name: str, length_unit: str) -> SpatialCrs:
    if s.execute(select(SpatialCrs).where(SpatialCrs.code == code)).scalar_one_or_none():
        raise ServiceError(409, "crs code exists")
    existing = s.execute(select(SpatialCrs).limit(1)).scalar_one_or_none()
    if existing is not None:
        # 共同坐标系只能有一个；第二个 CRS 属于无法解释的范围配置
        raise ServiceError(422, "range configuration problem: a common CRS is already declared; "
                                "all spatial data must share it")
    crs = SpatialCrs(code=code, name=name, length_unit=length_unit)
    s.add(crs)
    s.commit()
    s.refresh(crs)
    return crs


def create_zone(s: Session, code: str, name: str, polygons: list[dict],
                valid_from: datetime, valid_to: datetime | None,
                boundary_rule: str = "INCLUDE") -> SpatialZone:
    """导入多边形港区。无效几何直接 422（范围配置问题），不静默落库。"""
    crs = common_crs(s)
    if boundary_rule not in ("INCLUDE", "EXCLUDE"):
        raise ServiceError(422, "range configuration problem: boundary_rule must be INCLUDE/EXCLUDE")
    if not polygons:
        raise ServiceError(422, "range configuration problem: zone requires at least one polygon")
    try:
        for p in polygons:
            geometry.validate_polygon(p["exterior"], p.get("holes") or [])
    except geometry.GeometryError as exc:
        raise ServiceError(422, f"range configuration problem: invalid polygon for zone {code}: {exc}") from exc
    if valid_to is not None and _aware(valid_to) <= _aware(valid_from):
        raise ServiceError(422, "range configuration problem: valid_to must be after valid_from")

    prev = s.execute(
        select(SpatialZone).where(SpatialZone.code == code)
        .order_by(SpatialZone.version_no.desc()).limit(1)
    ).scalar_one_or_none()
    version_no = (prev.version_no + 1) if prev else 1
    zone = SpatialZone(
        code=code, version_no=version_no, name=name, crs_id=crs.id,
        polygons=polygons, boundary_rule=boundary_rule,
        valid_from=_aware(valid_from),
        valid_to=_aware(valid_to) if valid_to else None,
    )
    s.add(zone)
    s.commit()
    s.refresh(zone)
    # 几何资料更正：相关成员关系作废，等待重建
    _stale_memberships_for_zones(s, [code])
    return zone


def create_installation(s: Session, station_code: str, x: float, y: float,
                        valid_from: datetime, valid_to: datetime | None,
                        uncertainty_radius: float = 0.0, note: str = "") -> StationInstallation:
    st = s.execute(select(Station).where(Station.code == station_code)).scalar_one_or_none()
    if st is None:
        raise ServiceError(404, f"unknown station {station_code}")
    if uncertainty_radius < 0:
        raise ServiceError(422, "uncertainty_radius must be >= 0")
    vf, vt = _aware(valid_from), _aware(valid_to) if valid_to else None
    if vt is not None and vt <= vf:
        raise ServiceError(422, "installation valid_to must be after valid_from")
    rows = s.execute(
        select(StationInstallation).where(StationInstallation.station_id == st.id)
    ).scalars().all()
    for r in rows:
        rvf, rvt = _aware(r.valid_from), _aware(r.valid_to) if r.valid_to else None
        overlap = vf < (rvt or datetime.max.replace(tzinfo=timezone.utc)) and \
            rvf < (vt or datetime.max.replace(tzinfo=timezone.utc))
        if overlap:
            raise ServiceError(422, f"installation intervals overlap for station {station_code}")
    inst = StationInstallation(
        station_id=st.id, x=float(x), y=float(y), uncertainty_radius=float(uncertainty_radius),
        valid_from=vf, valid_to=vt, note=note,
    )
    s.add(inst)
    s.commit()
    s.refresh(inst)
    _stale_memberships_for_stations(s, [st.id])
    return inst


def create_license(s: Session, body: dict) -> AccessLicense:
    user = s.execute(select(AuthUser).where(AuthUser.code == body["user"])).scalar_one_or_none()
    if user is None:
        raise ServiceError(404, f"unknown user {body['user']}")
    if s.execute(select(AccessLicense).where(AccessLicense.code == body["code"])).scalar_one_or_none():
        raise ServiceError(409, "license code exists")
    if body["effect"] not in ("ALLOW", "DENY"):
        raise ServiceError(422, "effect must be ALLOW or DENY")
    for stage in body["stages"]:
        if stage not in STAGES:
            raise ServiceError(422, f"unknown stage {stage}")
    for f in body["allowed_fields"]:
        if f not in FIELD_CATALOG:
            raise ServiceError(422, f"unknown deliverable field {f}")
    if int(body["min_grid_seconds"]) <= 0:
        raise ServiceError(422, "min_grid_seconds must be positive")
    # zone 必须存在（任一版本），否则许可引用了无法解释的区域
    zone = s.execute(
        select(SpatialZone).where(SpatialZone.code == body["zone_code"]).limit(1)
    ).scalar_one_or_none()
    if zone is None:
        raise ServiceError(422, f"range configuration problem: unknown zone {body['zone_code']}")
    lic = AccessLicense(
        code=body["code"], user_id=user.id, effect=body["effect"],
        zone_code=body["zone_code"],
        obs_valid_from=_aware(body["obs_valid_from"]),
        obs_valid_to=_aware(body["obs_valid_to"]) if body.get("obs_valid_to") else None,
        stages=list(body["stages"]), allowed_fields=list(body["allowed_fields"]),
        min_grid_seconds=int(body["min_grid_seconds"]),
        valid_from=_aware(body["valid_from"]),
        valid_to=_aware(body["valid_to"]) if body.get("valid_to") else None,
        note=body.get("note", ""),
    )
    s.add(lic)
    s.commit()
    s.refresh(lic)
    _stale_memberships_for_licenses(s, [lic.id])
    return lic


def revoke_license(s: Session, license_id: int, reason: str, now: datetime | None = None) -> AccessLicense:
    lic = s.get(AccessLicense, license_id)
    if lic is None:
        raise ServiceError(404, "license not found")
    now = now or utcnow()
    if lic.revoked_at is None:
        lic.revoked_at = now
        lic.revoke_reason = reason
    _stale_memberships_for_licenses(s, [lic.id])
    s.commit()
    s.refresh(lic)
    return lic


def license_active(lic: AccessLicense, now: datetime) -> bool:
    """保留生效与撤回事实：撤回或未到生效时刻/已过期的许可不参与放行。"""
    if lic.revoked_at is not None:
        return False
    if now < _aware(lic.valid_from):
        return False
    if lic.valid_to is not None and now >= _aware(lic.valid_to):
        return False
    return True


# ============================== 几何解析 ==============================

@dataclass
class ZoneAt:
    zone: SpatialZone
    polygons: list[geometry.Polygon]


def zones_at(s: Session, zone_code: str, t: datetime) -> list[ZoneAt]:
    """取 zone_code 在观测时刻 t 适用的全部版本（通常一条；边界更正后按时刻区分）。"""
    t = _aware(t)
    rows = s.execute(
        select(SpatialZone).where(SpatialZone.code == zone_code).order_by(SpatialZone.version_no)
    ).scalars().all()
    out: list[ZoneAt] = []
    for z in rows:
        if _aware(z.valid_from) <= t and (z.valid_to is None or t < _aware(z.valid_to)):
            polys = [geometry.validate_polygon(p["exterior"], p.get("holes") or [])
                     for p in z.polygons]
            out.append(ZoneAt(zone=z, polygons=polys))
    return out


def installation_at(s: Session, station_id: int, t: datetime) -> StationInstallation | None:
    t = _aware(t)
    return s.execute(
        select(StationInstallation).where(
            StationInstallation.station_id == station_id,
            StationInstallation.valid_from <= t,
            (StationInstallation.valid_to.is_(None)) | (StationInstallation.valid_to > t),
        ).order_by(StationInstallation.valid_from.desc()).limit(1)
    ).scalar_one_or_none()


def classify_against_zones(point: tuple[float, float], radius: float,
                           zone_ats: list[ZoneAt]) -> str:
    """对一组同 code 的适用 zone 版本求归属：任一多边形 IN 即 IN，否则 UNCERTAIN，否则 OUT。"""
    verdict = "OUT"
    for zat in zone_ats:
        rule = zat.zone.boundary_rule
        for poly in zat.polygons:
            v = geometry.classify_uncertainty(point, radius, poly, rule)
            if v == "IN":
                return "IN"
            if v == "UNCERTAIN":
                verdict = "UNCERTAIN"
    return verdict


# ============================== 联合范围求值 ==============================

def _license_snapshot(licenses: list[AccessLicense], now: datetime) -> list[dict]:
    return [
        {
            "license_id": l.id, "code": l.code, "effect": l.effect, "zone_code": l.zone_code,
            "obs_valid_from": _aware(l.obs_valid_from).isoformat(),
            "obs_valid_to": _aware(l.obs_valid_to).isoformat() if l.obs_valid_to else None,
            "stages": l.stages, "allowed_fields": l.allowed_fields,
            "min_grid_seconds": l.min_grid_seconds,
            "valid_from": _aware(l.valid_from).isoformat(),
            "valid_to": _aware(l.valid_to).isoformat() if l.valid_to else None,
            "revoked_at": _aware(l.revoked_at).isoformat() if l.revoked_at else None,
            "active_at_eval": license_active(l, now),
        }
        for l in licenses
    ]


def _scope_stations(s: Session, scope: dict) -> list[Station]:
    q = select(Station).order_by(Station.id)
    if scope.get("stations"):
        q = q.where(Station.code.in_(scope["stations"]))
    elif scope.get("region"):
        q = q.where(Station.region == scope["region"])
    return list(s.execute(q).scalars().all())


def evaluate_scope(s: Session, user: AuthUser, scope: dict,
                   now: datetime | None = None, mode: str = "online") -> RangeEvaluation:
    """联合范围求值：对窗口内每条当前观测逐条绑定位置与许可，输出放行/排除明细。

    求值在单次请求内联查站点有效位置与空间条件（online 模式），位置与授权变动即时生效；
    mode='membership' 时只连接已核对的 ACTIVE 成员关系，缺失即报重建中，不使用 STALE 关系。
    """
    now = now or utcnow()
    scope = _load_scope(scope)
    _validate_scope(scope)
    ws, we = _aware(scope["window_start"]), _aware(scope["window_end"])
    stations = _scope_stations(s, scope)
    known_codes = {st.code for st in stations}
    if scope.get("stations"):
        unknown = set(scope["stations"]) - known_codes
        if unknown:
            # 不向普通用户确认其他港区站点是否存在
            raise ServiceError(404, "station not found in your authorized scope")

    licenses = list(s.execute(
        select(AccessLicense).where(AccessLicense.user_id == user.id)
        .order_by(AccessLicense.id)
    ).scalars().all())
    active_licenses = [l for l in licenses if license_active(l, now)]

    if mode == "membership" and active_licenses:
        # 从未重建过成员关系时，membership 模式没有任何“已核对成员”可连接：
        # 显式 503，而不是静默把全部观测当作区外放行/排除
        built = s.execute(
            select(LicenseMembership.id).where(
                LicenseMembership.license_id.in_([l.id for l in active_licenses])
            ).limit(1)
        ).first()
        if built is None:
            raise ServiceError(503, "license membership not built yet; run rebuild or use online mode")

    obs_rows = list(s.execute(
        select(RawObservation).where(
            RawObservation.is_current.is_(True),
            RawObservation.station_id.in_([st.id for st in stations]),
            RawObservation.observed_at >= ws,
            RawObservation.observed_at < we,
        ).order_by(RawObservation.observed_at, RawObservation.id)
    ).scalars()) if stations else []
    cutoff = max((o.id for o in obs_rows), default=0)

    eval_row = RangeEvaluation(
        user_id=user.id, scope=scope, cutoff_obs_id=cutoff,
        license_snapshot=_license_snapshot(licenses, now), mode=mode,
        scope_hash="",
    )
    s.add(eval_row)
    s.flush()

    # zone 版本缓存：(zone_code, t_bucket) 仅按精确时刻缓存即可
    zone_cache: dict[tuple[str, datetime], list[ZoneAt]] = {}

    def get_zones(code: str, t: datetime) -> list[ZoneAt]:
        key = (code, _aware(t))
        if key not in zone_cache:
            zone_cache[key] = zones_at(s, code, t)
        return zone_cache[key]

    membership_cache: dict[tuple[int, int], list[LicenseMembership]] = {}

    def membership_rows(lic_id: int, station_id: int) -> list[LicenseMembership]:
        key = (lic_id, station_id)
        if key not in membership_cache:
            membership_cache[key] = list(s.execute(
                select(LicenseMembership).where(
                    LicenseMembership.license_id == lic_id,
                    LicenseMembership.station_id == station_id,
                ).order_by(LicenseMembership.id)).scalars())
        return membership_cache[key]

    def membership_verdict(lic_id: int, station_id: int, t: datetime) -> str:
        """membership 模式几何结论。

        覆盖 t 的行中存在 ACTIVE -> 用最新 ACTIVE 结论（IN/OUT/UNCERTAIN）；
        仅有 STALE 行覆盖 -> 重建中，503（不得使用已失效的放行关系）；
        无任何行覆盖 t -> 该许可在此时刻不覆盖该站，按 OUT 处理，不误报重建。
        """
        covering = []
        for m in membership_rows(lic_id, station_id):
            mvf = _aware(m.valid_from)
            mvt = _aware(m.valid_to) if m.valid_to else None
            if mvf <= t and (mvt is None or t < mvt):
                covering.append(m)
        active = [m for m in covering if m.status == "ACTIVE"]
        if active:
            return active[-1].geometry_verdict
        if covering:
            raise ServiceError(503, "license membership rebuild in progress; retry later")
        return "OUT"

    for obs in obs_rows:
        t = _aware(obs.observed_at)
        inst = installation_at(s, obs.station_id, t)
        reasons: list[str] = []
        if inst is None:
            s.add(RangeEvaluationItem(
                evaluation_id=eval_row.id, station_id=obs.station_id, obs_id=obs.id,
                observed_at=t, installation_id=None, x=None, y=None,
                uncertainty_radius=None, verdict="NO_INSTALLATION",
                reasons=["no installation record applicable at observation time; "
                         "relocation history cannot locate this observation"]))
            continue

        matched_allow: list[int] = []
        uncertain_licenses: list[int] = []
        deny_hit: AccessLicense | None = None
        too_fine: list[int] = []
        date_rejected: list[int] = []
        stage_rejected: list[int] = []
        out_licenses: list[int] = []

        for lic in active_licenses:
            # 几何判定（membership 模式走预计算成员，online 模式实时几何求值）
            if mode == "membership":
                geo = membership_verdict(lic.id, obs.station_id, t)
            else:
                zat = get_zones(lic.zone_code, t)
                if not zat:
                    raise ServiceError(
                        422,
                        f"range configuration problem: zone {lic.zone_code} has no geometry "
                        f"version applicable at {t.isoformat()}")
                _check_common_crs(s, zat)
                geo = classify_against_zones((inst.x, inst.y), inst.uncertainty_radius, zat)

            if geo == "OUT":
                out_licenses.append(lic.id)
                continue
            # 几何 UNCERTAIN：不能证明完全位于许可内，先记录，不据此放行
            if geo == "UNCERTAIN":
                uncertain_licenses.append(lic.id)
                continue

            # 几何 IN 之后才检查完整条件的其余维度
            if not (_aware(lic.obs_valid_from) <= t and
                    (lic.obs_valid_to is None or t < _aware(lic.obs_valid_to))):
                date_rejected.append(lic.id)
                continue
            if scope["stage"] not in lic.stages:
                stage_rejected.append(lic.id)
                continue
            if lic.min_grid_seconds > int(scope["grid_seconds"]):
                # 该许可最细粒度比请求格距粗：此观测不能凭它取高频
                too_fine.append(lic.id)
                continue

            if lic.effect == "DENY":
                deny_hit = lic  # 明确拒绝在其适用范围优先
            else:
                matched_allow.append(lic.id)

        if deny_hit is not None:
            verdict, license_id = "DENIED", deny_hit.id
            reasons.append(f"DENY license {deny_hit.code} applies at this location/time/stage/grid")
        elif matched_allow:
            verdict, license_id = "ALLOWED", matched_allow[0]
            if len(matched_allow) > 1:
                reasons.append(f"covered by {len(matched_allow)} licenses; observation counted once")
            if too_fine:
                reasons.append(f"licenses {too_fine} require coarser grid and did not admit this cell")
        elif uncertain_licenses:
            verdict, license_id = "UNCERTAIN", None
            reasons.append(f"position uncertainty spans authorization boundary of licenses "
                           f"{uncertain_licenses}; cannot prove fully inside, not released by center point")
        else:
            verdict, license_id = "NO_LICENSE", None
            if too_fine:
                reasons.append(f"only matching licenses {too_fine} forbid this fine grid "
                               f"(min_grid_seconds coarser than requested {scope['grid_seconds']}s)")
            if date_rejected:
                reasons.append(f"outside observation-validity dates of licenses {date_rejected}")
            if stage_rejected:
                reasons.append(f"stage {scope['stage']} not covered by licenses {stage_rejected}")
            if out_licenses:
                reasons.append(f"outside geometry of licenses {out_licenses} (bbox/name proximity is not used)")
            if not reasons:
                reasons.append("no active license covers this observation")

        s.add(RangeEvaluationItem(
            evaluation_id=eval_row.id, station_id=obs.station_id, obs_id=obs.id,
            observed_at=t, installation_id=inst.id, x=inst.x, y=inst.y,
            uncertainty_radius=inst.uncertainty_radius, verdict=verdict,
            license_id=license_id, matched_license_ids=matched_allow,
            deny_license_id=deny_hit.id if deny_hit else None, reasons=reasons))

    eval_row.scope_hash = scope_hash_of(scope, cutoff)
    s.commit()
    s.refresh(eval_row)
    return eval_row


def _check_common_crs(s: Session, zone_ats: list[ZoneAt]) -> None:
    crs = common_crs(s)
    for zat in zone_ats:
        if zat.zone.crs_id != crs.id:
            raise ServiceError(422, "range configuration problem: zone uses an unexplained CRS")


def _validate_scope(scope: dict) -> None:
    required = ("window_start", "window_end", "grid_seconds", "stage", "fields")
    missing = [k for k in required if k not in scope]
    if missing:
        raise ServiceError(422, f"scope missing keys: {missing}")
    if scope["stage"] not in STAGES:
        raise ServiceError(422, f"unknown stage {scope['stage']}")
    if int(scope["grid_seconds"]) <= 0:
        raise ServiceError(422, "grid_seconds must be positive")
    for f in scope["fields"]:
        if f not in FIELD_CATALOG:
            raise ServiceError(422, f"unknown requested field {f}")
    if _aware(scope["window_end"]) <= _aware(scope["window_start"]):
        raise ServiceError(422, "window_end must be after window_start")
    if scope.get("boundary_cells", "MISSING") not in ("MISSING", "DECLINE"):
        raise ServiceError(422, "boundary_cells must be MISSING or DECLINE")
    if scope.get("product_kind", "STATION") not in ("STATION", "REGION_AGG"):
        raise ServiceError(422, "product_kind must be STATION or REGION_AGG")


# ============================== 授权约束下的重新聚合 ==============================

@dataclass
class RestrictedCell:
    station_id: int
    grid_start: datetime
    grid_seconds: int
    value_m: float | None
    quality_grade: str
    coverage_sec: float
    coverage_ratio: float
    sample_count: int
    source_sample_ids: list[int]
    quality_reasons: list[str]
    license_ids: list[int]
    boundary_cross: bool = False
    max_m: float | None = None
    min_m: float | None = None
    extreme_max_at: datetime | None = None
    extreme_min_at: datetime | None = None


def _legal_intervals(items: list[RangeEvaluationItem], licenses: dict[int, AccessLicense],
                     ws: datetime, we: datetime) -> list[tuple[datetime, datetime]]:
    """按放行观测所依据许可的观测适用日期，合并出该站的合法时间区间。"""
    intervals: list[tuple[datetime, datetime]] = []
    for it in items:
        for lic_id in it.matched_license_ids:
            lic = licenses[lic_id]
            lo = max(ws, _aware(lic.obs_valid_from))
            hi = min(we, _aware(lic.obs_valid_to) if lic.obs_valid_to else we)
            if hi > lo:
                intervals.append((lo, hi))
    if not intervals:
        return []
    intervals.sort()
    merged = [intervals[0]]
    for lo, hi in intervals[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _within(t: datetime, intervals: list[tuple[datetime, datetime]]) -> bool:
    return any(lo <= t < hi for lo, hi in intervals)


def restricted_resample(samples: list[Sample], grid_seconds: int, ws: datetime, we: datetime,
                        legal_intervals: list[tuple[datetime, datetime]],
                        max_rep: float, min_cov: float,
                        boundary_policy: str = "MISSING") -> list[RestrictedCell]:
    """只使用合法样本重算共同时间格。

    与 grid.resample_window 的区别：样本代表时段在合法区间边界处裁断；
    跨授权日期边界的格元不做插值（不用未获准时段把格元“补平”），
    覆盖不足即 MISSING 并声明原因（boundary_policy=DECLINE 时整格拒绝）——
    绝不沿用全窗口平均值，也不把未授权站点/样本当零水位。
    """
    seen: set[int] = set()
    uniq: list[Sample] = []
    for smp in sorted(samples, key=lambda x: x.t):
        if smp.obs_id in seen:
            continue
        seen.add(smp.obs_id)
        uniq.append(smp)
    effective = [smp for smp in uniq if smp.quality_flag != "BAD"]
    reps_raw = dict(zip([smp.obs_id for smp in effective],
                        _rep_intervals(effective, max_rep))) if effective else {}
    # 代表时段裁到 [ws, we) 与合法区间的交集（一个样本可能对应多段）
    reps: dict[int, list[tuple[datetime, datetime]]] = {}
    for oid, (l, r) in reps_raw.items():
        l, r = max(l, ws), min(r, we)
        clipped = []
        for lo, hi in legal_intervals:
            il, ir = max(l, lo), min(r, hi)
            if ir > il:
                clipped.append((il, ir))
        reps[oid] = clipped
    cell_dt = timedelta(seconds=grid_seconds)
    cells: list[RestrictedCell] = []
    t = ws
    while t < we:
        ge = t + cell_dt
        weights: list[tuple[Sample, float]] = []
        coverage = 0.0
        for smp in effective:
            for l, r in reps[smp.obs_id]:
                w = _overlap_sec(l, r, t, ge)
                if w > 0:
                    weights.append((smp, w))
                    coverage += w
        coverage = min(coverage, float(grid_seconds))
        ratio = coverage / grid_seconds
        sample_ids = sorted({smp.obs_id for smp, _ in weights})
        reasons: list[str] = []
        probe = ge - timedelta(microseconds=1)
        boundary_cross = not all(_within(tt, legal_intervals) for tt in (t, probe))
        if boundary_cross:
            reasons.append("cell crosses license date boundary; only legal samples used, "
                           "interpolation across unauthorized time suppressed")
        pending = [smp for smp, _ in weights if smp.quality_flag in ("SUSPECT", "PENDING_JUMP")]
        value = None
        if boundary_cross and boundary_policy == "DECLINE":
            # 契约选项：跨界格元直接拒绝，不交付该格
            grade = "DECLINED"
            reasons.append("boundary_cells=DECLINE: cell refused per contract instead of partial delivery")
        elif ratio >= min_cov and weights:
            value = round(sum(smp.value_m * w for smp, w in weights)
                          / sum(w for _, w in weights), 6)
            if pending:
                grade = "PENDING"
                reasons.append("contains unverified samples; not smoothed")
            else:
                grade = "MEASURED"
        else:
            grade = "MISSING"
            reasons.append(f"insufficient authorized coverage ratio={round(ratio, 4)} "
                           f"threshold={min_cov}; missing retained, unauthorized data never zero-filled")
        cells.append(RestrictedCell(
            station_id=-1, grid_start=t, grid_seconds=grid_seconds,
            value_m=value, quality_grade=grade,
            coverage_sec=round(coverage, 3), coverage_ratio=round(ratio, 4),
            sample_count=len(sample_ids), source_sample_ids=sample_ids,
            quality_reasons=reasons, license_ids=[], boundary_cross=boundary_cross))
        t = ge
    return cells


def recompute_restricted(s: Session, evaluation: RangeEvaluation,
                         max_rep: float | None = None,
                         min_cov: float | None = None) -> dict:
    """按求值结果重算受限产品（站点序列或部分站点区域汇总）。"""
    scope = _load_scope(evaluation.scope)
    ws, we = _aware(scope["window_start"]), _aware(scope["window_end"])
    grid = int(scope["grid_seconds"])
    max_rep = max_rep or config.DEFAULT_MAX_REP_SEC
    min_cov = min_cov or config.DEFAULT_MIN_COVERAGE
    datum_version = current_datum_version(s)
    edges = edges_at_version(s, datum_version.id)
    from .models import Datum
    target_datum = s.execute(
        select(Datum).where(Datum.code == scope["target_datum"])
    ).scalar_one_or_none()
    if target_datum is None:
        raise ServiceError(404, f"unknown target datum {scope['target_datum']}")

    items = list(s.execute(
        select(RangeEvaluationItem).where(
            RangeEvaluationItem.evaluation_id == evaluation.id)
    ).scalars())
    licenses = {l.id: l for l in s.execute(select(AccessLicense)).scalars()}
    allowed_items = [it for it in items if it.verdict == "ALLOWED"]
    uncertain_items = [it for it in items if it.verdict == "UNCERTAIN"]
    excluded = [it for it in items if it.verdict not in ("ALLOWED", "UNCERTAIN")]
    if not allowed_items:
        raise ServiceError(409, "no observations satisfy the complete license conditions; "
                                "restricted product cannot be generated")

    obs_ids = [it.obs_id for it in allowed_items]
    obs_rows = {o.id: o for o in s.execute(
        select(RawObservation).where(RawObservation.id.in_(obs_ids))).scalars()}
    stations = {st.id: st for st in s.execute(select(Station)).scalars()}

    by_station: dict[int, list[RangeEvaluationItem]] = {}
    for it in allowed_items:
        by_station.setdefault(it.station_id, []).append(it)

    boundary_policy = scope.get("boundary_cells", "MISSING")
    station_cells: dict[int, list[RestrictedCell]] = {}
    for station_id, sitems in by_station.items():
        st = stations[station_id]
        station_datum = station_datum_at_version(s, st.id, datum_version.id)
        samples: list[Sample] = []
        for it in sitems:
            obs = obs_rows[it.obs_id]
            t = _aware(obs.observed_at)
            if station_datum is None:
                continue  # 无基准指派：该样本不参与，格元保留缺测，不用邻站补齐
            res = find_conversion(edges, station_datum, target_datum.id, t)
            if res.status != "OK":
                continue  # NO_PATH/CONFLICT 样本不参与
            base = obs.value * UNIT_TO_M[obs.unit]
            if st.positive == "down":
                base = -base
            samples.append(Sample(t=t, value_m=base + res.total_offset,
                                  quality_flag=obs.quality_flag, obs_id=obs.id))
        legal = _legal_intervals(sitems, licenses, ws, we)
        cells = restricted_resample(samples, grid, ws, we, legal, max_rep, min_cov,
                                    boundary_policy=boundary_policy)
        # 回填站点、许可与辅助字段
        sample_licenses: dict[int, set[int]] = {}
        # 站点级许可兜底：覆盖不足的 MISSING/DECLINED 格没有样本来源，
        # 仍需按该站获准的许可输出字段（值为 None/缺测），不能整格空白
        station_lic_ids: set[int] = set()
        for it in sitems:
            ids = set(it.matched_license_ids)
            sample_licenses[it.obs_id] = ids
            station_lic_ids |= ids
        for c in cells:
            c.station_id = station_id
            lic_set: set[int] = set()
            for oid in c.source_sample_ids:
                lic_set |= sample_licenses.get(oid, set())
            c.license_ids = sorted(lic_set or station_lic_ids)
            cell_samples = [smp for smp in samples if smp.obs_id in c.source_sample_ids]
            if cell_samples:
                top = max(cell_samples, key=lambda x: x.value_m)
                bot = min(cell_samples, key=lambda x: x.value_m)
                c.max_m, c.extreme_max_at = round(top.value_m, 6), top.t
                c.min_m, c.extreme_min_at = round(bot.value_m, 6), bot.t
        station_cells[station_id] = cells

    result = {
        "datum_version_id": datum_version.id,
        "stations": station_cells,
        "station_codes": [stations[i].code for i in sorted(station_cells)],
        "uncertain_count": len(uncertain_items),
        "excluded_count": len(excluded),
        "excluded_preview": [
            {"station": stations[it.station_id].code, "observed_at": it.observed_at.isoformat(),
             "verdict": it.verdict, "reasons": it.reasons}
            for it in excluded[:20]
        ],
        "uncertain_preview": [
            {"station": stations[it.station_id].code, "observed_at": it.observed_at.isoformat(),
             "reasons": it.reasons}
            for it in uncertain_items[:20]
        ],
    }

    if scope.get("product_kind") == "REGION_AGG":
        result["aggregate"] = _regional_aggregate(s, scope, station_cells, stations)
    return result


def _regional_aggregate(s: Session, scope: dict,
                        station_cells: dict[int, list[RestrictedCell]],
                        stations: dict[int, Station]) -> dict:
    """部分站点区域汇总：只用合法站点重算，明确命名；分母只数实际合法输入，不当零水位。"""
    region = scope["region"]
    # 全集 = 本次 scope 内的站点（显式 stations 时不把范围外站点计入排除清单），
    # 未声明 stations 时回退为区域内全部站点
    if scope.get("stations"):
        all_region = _scope_stations(s, scope)
    else:
        all_region = list(s.execute(select(Station).where(Station.region == region)).scalars())
    authorized_ids = set(station_cells)
    if not authorized_ids:
        raise ServiceError(409, "no authorized stations in region; region aggregate declined")
    grid_starts = sorted({c.grid_start for cells in station_cells.values() for c in cells})
    rows = []
    for gs in grid_starts:
        contributing = []
        coverages = []
        source_ids = []
        license_ids: set[int] = set()
        for sid, cells in station_cells.items():
            cell = next((c for c in cells if c.grid_start == gs), None)
            # DECLINED（按契约拒绝的跨界格元）与无值格元都不得进入汇总
            if cell is not None and cell.quality_grade != "DECLINED" and cell.value_m is not None:
                contributing.append(cell.value_m)
                coverages.append(cell.coverage_ratio)
                source_ids.append(sid)
                license_ids.update(cell.license_ids)
        if contributing:
            rows.append({
                "grid_start": gs.isoformat(),
                "value_m": round(sum(contributing) / len(contributing), 6),
                "quality_grade": "MEASURED",
                "coverage_ratio": round(sum(coverages) / len(coverages), 4),
                "station_count": len(contributing),  # 分母=合法且有值的站点，非全区域站点数
                "source_station_ids": sorted(source_ids),
                "license_ids": sorted(license_ids),
            })
        else:
            rows.append({
                "grid_start": gs.isoformat(), "value_m": None,
                "quality_grade": "MISSING", "coverage_ratio": 0.0,
                "station_count": 0, "source_station_ids": [],
                "license_ids": sorted(license_ids),
                "reasons": ["no authorized station produced a value in this cell; missing retained"],
            })
    excluded_station_codes = sorted(st.code for st in all_region if st.id not in authorized_ids)
    return {
        # 明确命名的受限范围产品，不伪装成原区域全量汇总
        "aggregate_name": f"{region}-restricted:{ '+'.join(sorted(st.code for st in all_region if st.id in authorized_ids))}",
        "region": region,
        "rows": rows,
        "authorized_station_codes": [stations[i].code for i in sorted(authorized_ids)],
        "excluded_station_codes": excluded_station_codes,
        "note": "recomputed from authorized stations only; excluded stations are never zero-filled "
                "and the original full-region aggregate is not delivered",
    }


def project_fields(cell: RestrictedCell, requested_fields: list[str],
                   licenses: dict[int, AccessLicense]) -> dict:
    """字段级约束：某字段只有在该格元全部放行许可都允许时才输出，aux 字段不得绕过粒度限制。"""
    admitting = [licenses[i] for i in cell.license_ids if i in licenses]
    out: dict = {}
    for f in requested_fields:
        if admitting and all(f in (lic.allowed_fields or []) for lic in admitting):
            if f == "grid_start":
                out[f] = cell.grid_start.isoformat()
            elif f == "extreme_max_at":
                out[f] = cell.extreme_max_at.isoformat() if cell.extreme_max_at else None
            elif f == "extreme_min_at":
                out[f] = cell.extreme_min_at.isoformat() if cell.extreme_min_at else None
            else:
                out[f] = getattr(cell, f)
        # 未获准字段直接不出现，不报错泄露其他信息；调用方在 manifest 记录剔除
    return out


def project_cell(cell: RestrictedCell, requested_fields: list[str],
                 licenses: dict[int, AccessLicense]) -> dict:
    """API 与文件清单共用的格元投影：获准数据字段 + 始终保留的溯源元数据。"""
    out = project_fields(cell, requested_fields, licenses)
    out["license_codes"] = [licenses[i].code for i in cell.license_ids if i in licenses]
    out["boundary_cross"] = cell.boundary_cross
    return out


# ============================== 分页会话 ==============================

def create_session(s: Session, user: AuthUser, evaluation: RangeEvaluation,
                   page_size: int) -> AccessSession:
    if page_size <= 0:
        raise ServiceError(422, "page_size must be positive")
    sess = AccessSession(
        token=secrets.token_urlsafe(32), user_id=user.id, evaluation_id=evaluation.id,
        scope_hash=evaluation.scope_hash, page_size=page_size, status="ACTIVE",
    )
    s.add(sess)
    s.commit()
    s.refresh(sess)
    return sess


def _encode_cursor(last_station_id: int, last_grid_start: datetime, last_obs_id: int,
                   evaluation_id: int, scope_hash: str) -> str:
    raw = json.dumps([last_station_id, last_grid_start.isoformat(), last_obs_id,
                      evaluation_id, scope_hash])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, datetime, int, int, str]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return int(raw[0]), datetime.fromisoformat(raw[1]), int(raw[2]), int(raw[3]), raw[4]
    except Exception as exc:  # noqa: BLE001
        raise ServiceError(422, "invalid pagination cursor") from exc


def _ensure_licenses_current(s: Session, evaluation: RangeEvaluation, user: AuthUser,
                             now: datetime) -> None:
    """每次取页联查当前许可：任一参与求值的许可被撤回/失效即终止会话。"""
    snapshot_ids = [ snap["license_id"] for snap in evaluation.license_snapshot]
    current = {l.id: l for l in s.execute(
        select(AccessLicense).where(AccessLicense.id.in_(snapshot_ids or [0]))).scalars()}
    for snap in evaluation.license_snapshot:
        lic = current.get(snap["license_id"])
        if lic is None or not license_active(lic, now):
            _terminate(s, evaluation, user,
                       f"license {snap['code']} revoked/expired; range narrowed, please re-request")
            raise ServiceError(409, "access range narrowed; session terminated, please re-request")
        # 条件内容发生更正（快照与当前不一致）同样终止
        now_snap = {
            "effect": lic.effect,
            "zone_code": lic.zone_code, "stages": lic.stages,
            "allowed_fields": lic.allowed_fields,
            "min_grid_seconds": lic.min_grid_seconds,
            "obs_valid_from": _aware(lic.obs_valid_from).isoformat(),
            "obs_valid_to": _aware(lic.obs_valid_to).isoformat() if lic.obs_valid_to else None,
            "valid_from": _aware(lic.valid_from).isoformat(),
            "valid_to": _aware(lic.valid_to).isoformat() if lic.valid_to else None,
        }
        old = {k: snap[k] for k in now_snap}
        if old != now_snap:
            _terminate(s, evaluation, user,
                       f"license {snap['code']} conditions corrected; please re-request")
            raise ServiceError(409, "license conditions corrected; session terminated, please re-request")


def _terminate(s: Session, evaluation: RangeEvaluation, user: AuthUser, reason: str) -> None:
    for sess in s.execute(select(AccessSession).where(
            AccessSession.evaluation_id == evaluation.id,
            AccessSession.user_id == user.id)).scalars():
        sess.status = "TERMINATED"
        sess.terminate_reason = reason
    s.commit()


def get_page(s: Session, user: AuthUser, token: str, cursor: str | None,
             now: datetime | None = None) -> dict:
    now = now or utcnow()
    sess = s.execute(select(AccessSession).where(
        AccessSession.token == token, AccessSession.user_id == user.id)
    ).scalar_one_or_none()
    if sess is None:
        raise ServiceError(404, "session not found")
    if sess.status != "ACTIVE":
        raise ServiceError(409, f"session {sess.status}: {sess.terminate_reason or 'please re-request'}")
    evaluation = s.get(RangeEvaluation, sess.evaluation_id)
    if evaluation.user_id != user.id:
        raise ServiceError(404, "session not found")
    _ensure_licenses_current(s, evaluation, user, now)

    q = select(RangeEvaluationItem).where(
        RangeEvaluationItem.evaluation_id == evaluation.id,
        RangeEvaluationItem.verdict == "ALLOWED",
    ).order_by(RangeEvaluationItem.station_id, RangeEvaluationItem.observed_at,
               RangeEvaluationItem.obs_id)
    if cursor:
        sid, gs, oid, eid, sh = _decode_cursor(cursor)
        if eid != evaluation.id or sh != evaluation.scope_hash:
            raise ServiceError(422, "cursor does not match this session scope")
        q = q.where(
            (RangeEvaluationItem.station_id > sid)
            | ((RangeEvaluationItem.station_id == sid)
               & (RangeEvaluationItem.observed_at > gs))
            | ((RangeEvaluationItem.station_id == sid)
               & (RangeEvaluationItem.observed_at == gs)
               & (RangeEvaluationItem.obs_id > oid))
        )
    rows = list(s.execute(q.limit(sess.page_size + 1)).scalars())
    has_next = len(rows) > sess.page_size
    rows = rows[:sess.page_size]
    station_codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
    next_cursor = None
    if rows and has_next:
        last = rows[-1]
        next_cursor = _encode_cursor(last.station_id, last.observed_at, last.obs_id,
                                     evaluation.id, evaluation.scope_hash)
    sess.last_page_at = now
    s.commit()
    return {
        "session_token": token,
        "evaluation_id": evaluation.id,
        "scope_hash": evaluation.scope_hash,
        "cutoff_obs_id": evaluation.cutoff_obs_id,
        "page": [
            {"station": station_codes.get(it.station_id), "obs_id": it.obs_id,
             "observed_at": it.observed_at.isoformat(),
             "matched_license_ids": it.matched_license_ids}
            for it in rows
        ],
        "next_cursor": next_cursor,
        "terminated": False,
    }


# ============================== 文件生成与受控领取 ==============================

def _ensure_dirs() -> tuple[str, str]:
    staging = os.path.join(config.EXTRACT_DIR, "staging")
    ready = os.path.join(config.EXTRACT_DIR, "ready")
    os.makedirs(staging, exist_ok=True)
    os.makedirs(ready, exist_ok=True)
    return staging, ready


def _build_manifest(s: Session, evaluation: RangeEvaluation, recomputed: dict) -> dict:
    scope = evaluation.scope
    licenses = {l.id: l for l in s.execute(select(AccessLicense)).scalars()}
    payload_stations = []
    for sid in sorted(recomputed["stations"]):
        cells = []
        for c in recomputed["stations"][sid]:
            projected = project_cell(c, scope["fields"], licenses)
            cells.append(projected)
        payload_stations.append({"station_id": sid, "cells": cells})
    manifest = {
        "manifest_version": 1,
        "evaluation_id": evaluation.id,
        "scope_hash": evaluation.scope_hash,
        "scope": scope,
        "datum_version_id": recomputed["datum_version_id"],
        "license_conditions": evaluation.license_snapshot,
        "stations": payload_stations,
        "uncertain_count": recomputed["uncertain_count"],
        "excluded_count": recomputed["excluded_count"],
    }
    if "aggregate" in recomputed:
        manifest["aggregate"] = recomputed["aggregate"]
    return manifest


def _hash_manifest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def create_extraction_task(s: Session, user: AuthUser, scope: dict, task_key: str,
                           now: datetime | None = None, mode: str = "online") -> ExtractionTask:
    """生成任务：先求值再重算，内容只落暂存区；相同 task_key 重试时重新核对当前范围。"""
    now = now or utcnow()
    existing = s.execute(
        select(ExtractionTask).where(ExtractionTask.task_key == task_key)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.user_id != user.id:
            raise ServiceError(404, "task not found")
        if existing.status in ("REVOKED", "FAILED"):
            # 权限改变后的旧任务不得重新挂为可下载：须用新 task_key 重新请求
            raise ServiceError(409, f"previous task {task_key} is {existing.status}; "
                                    "start a new request instead of reusing it")
        # 重试：核对当前范围，不复用不再合格的旧文件
        fresh = evaluate_scope(s, user, scope, now=now, mode=mode)
        prev_eval = s.get(RangeEvaluation, existing.evaluation_id)
        if fresh.scope_hash != prev_eval.scope_hash or fresh.id != existing.evaluation_id:
            raise ServiceError(409, "current scope no longer matches the previous generation; "
                                    "start a new task (old staged file is not reusable)")
        return existing

    evaluation = evaluate_scope(s, user, scope, now=now, mode=mode)
    recomputed = recompute_restricted(s, evaluation)
    manifest = _build_manifest(s, evaluation, recomputed)
    raw = json.dumps(manifest, ensure_ascii=False, indent=2, default=str).encode()
    content_hash = _hash_manifest(raw)
    staging_dir, _ = _ensure_dirs()
    staging_path = os.path.join(staging_dir, f"task_{task_key}_{secrets.token_hex(8)}.json")
    with open(staging_path, "wb") as fh:
        fh.write(raw)

    task = ExtractionTask(
        task_key=task_key, user_id=user.id, evaluation_id=evaluation.id,
        status="STAGED", staging_path=staging_path, ready_path=None,
        content_hash=content_hash, scope=scope,
        allowed_fields=list(scope["fields"]),
        cell_count=sum(len(cells) for cells in recomputed["stations"].values()),
        station_codes=recomputed["station_codes"],
    )
    s.add(task)
    s.commit()
    s.refresh(task)
    return task


def promote_task(s: Session, user: AuthUser, task_id: int,
                 now: datetime | None = None) -> ExtractionTask:
    """暂存 -> 可领取：晋升前重新求值当前范围，撤回/收窄则拒绝晋升。"""
    now = now or utcnow()
    task = s.get(ExtractionTask, task_id)
    if task is None or task.user_id != user.id:
        raise ServiceError(404, "task not found")
    if task.status in ("REVOKED", "FAILED"):
        raise ServiceError(409, f"task {task.status}; a new request is required")
    if task.status in ("READY", "CLAIMED"):
        return task

    evaluation = s.get(RangeEvaluation, task.evaluation_id)
    # 重新核对：当前许可下同一范围的放行观测集合必须与生成时一致
    fresh = evaluate_scope(s, user, task.scope, now=now, mode=evaluation.mode)
    fresh_allowed = _allowed_signature(s, fresh.id)
    old_allowed = _allowed_signature(s, evaluation.id)
    if fresh_allowed != old_allowed:
        task.status = "FAILED"
        s.commit()
        raise ServiceError(409, "access range changed before promotion; staged file discarded, "
                                "please re-request")
    # 用原求值（同一 evaluation_id）重算并重建清单，保证哈希可比、内容可溯
    recomputed = recompute_restricted(s, evaluation)
    manifest = _build_manifest(s, evaluation, recomputed)
    raw = json.dumps(manifest, ensure_ascii=False, indent=2, default=str).encode()
    new_hash = _hash_manifest(raw)
    if new_hash != task.content_hash:
        task.status = "FAILED"
        s.commit()
        raise ServiceError(409, "recomputed content differs from staged generation; please re-request")

    _, ready_dir = _ensure_dirs()
    claim_token = secrets.token_urlsafe(32)
    ready_path = os.path.join(ready_dir, f"{claim_token}.json")
    with open(ready_path, "wb") as fh:
        fh.write(raw)
    task.ready_path = ready_path
    task.claim_token = claim_token
    task.status = "READY"
    task.ready_at = now
    # 交付登记：生成内容（哈希/确切范围）+ 接收主体
    record = DeliveryRecord(
        task_id=task.id, user_id=user.id, content_hash=task.content_hash,
        delivered_scope=task.scope, license_ids=sorted({
            lid for cells in recomputed["stations"].values() for c in cells for lid in c.license_ids}),
        station_codes=task.station_codes, cell_count=task.cell_count,
    )
    s.add(record)
    s.commit()
    s.refresh(task)
    return task


def _allowed_signature(s: Session, evaluation_id: int) -> list[tuple[int, int, tuple]]:
    rows = s.execute(
        select(RangeEvaluationItem).where(
            RangeEvaluationItem.evaluation_id == evaluation_id,
            RangeEvaluationItem.verdict == "ALLOWED")
        .order_by(RangeEvaluationItem.obs_id)
    ).scalars()
    return [(r.obs_id, r.station_id, tuple(sorted(r.matched_license_ids))) for r in rows]


def claim_task(s: Session, user: AuthUser, task_id: int,
               now: datetime | None = None) -> dict:
    """受控领取：领取瞬间再次联查许可；不提供长期公开链接。"""
    now = now or utcnow()
    task = s.get(ExtractionTask, task_id)
    if task is None or task.user_id != user.id:
        raise ServiceError(404, "task not found")

    if task.status == "CLAIMED":
        rec = _claim_record(s, task, user, "ALREADY_CLAIMED",
                            "file already delivered; only the exact delivered scope is recorded")
        s.commit()
        raise ServiceError(409, f"already claimed at {task.ready_at.isoformat() if task.ready_at else ''}; "
                                f"claim_id={rec.id}")

    # 领取前最后一次联查：文件生成后、领取前撤回也必须拦住
    evaluation = s.get(RangeEvaluation, task.evaluation_id)
    try:
        _ensure_licenses_current(s, evaluation, user, now)
    except ServiceError:
        task.status = "REVOKED"
        task.revoked_at = now
        task.revoke_reason = "license revoked/changed between generation and claim"
        s.add(ClaimRecord(task_id=task.id, user_id=user.id, result="DENIED_REVOKED",
                          detail=task.revoke_reason))
        s.commit()
        raise

    if task.status != "READY" or not task.ready_path or not os.path.exists(task.ready_path):
        s.add(ClaimRecord(task_id=task.id, user_id=user.id, result="DENIED_NOT_READY",
                          detail="no ready content (staging is never downloadable)"))
        s.commit()
        raise ServiceError(409, "file is not in claimable state; staging content is never served")

    with open(task.ready_path, "rb") as fh:
        raw = fh.read()
    if _hash_manifest(raw) != task.content_hash:
        task.status = "FAILED"
        s.commit()
        raise ServiceError(409, "content integrity check failed")

    task.status = "CLAIMED"
    delivery = s.execute(
        select(DeliveryRecord).where(DeliveryRecord.task_id == task.id)
    ).scalar_one_or_none()
    s.add(ClaimRecord(task_id=task.id, user_id=user.id,
                      delivery_id=delivery.id if delivery else None,
                      result="CLAIMED", detail=f"content_hash={task.content_hash}"))
    s.commit()
    return {
        "task_id": task.id, "claim_token": task.claim_token,
        "content_hash": task.content_hash,
        "filename": f"restricted_{task.task_key}.json",
        "content": json.loads(raw.decode()),
        "note": "this is a controlled one-time claim endpoint, not a persistent public link; "
                "already-saved copies cannot be recalled — the exact delivered scope is recorded",
    }


def _claim_record(s: Session, task: ExtractionTask, user: AuthUser,
                  result: str, detail: str) -> ClaimRecord:
    rec = ClaimRecord(task_id=task.id, user_id=user.id, result=result, detail=detail)
    s.add(rec)
    s.flush()
    return rec


# ============================== 审计 ==============================

def evaluation_audit(s: Session, evaluation_id: int) -> dict:
    ev = s.get(RangeEvaluation, evaluation_id)
    if ev is None:
        raise ServiceError(404, "evaluation not found")
    station_codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
    lic_codes = {l.id: l.code for l in s.execute(select(AccessLicense)).scalars()}
    items = s.execute(
        select(RangeEvaluationItem).where(
            RangeEvaluationItem.evaluation_id == ev.id)
        .order_by(RangeEvaluationItem.station_id, RangeEvaluationItem.observed_at)
    ).scalars()
    return {
        "evaluation_id": ev.id, "mode": ev.mode, "scope_hash": ev.scope_hash,
        "cutoff_obs_id": ev.cutoff_obs_id, "created_at": ev.created_at.isoformat(),
        "scope": ev.scope,
        "license_snapshot": ev.license_snapshot,
        "items": [
            {
                "station": station_codes.get(it.station_id), "obs_id": it.obs_id,
                "observed_at": it.observed_at.isoformat(),
                "installation_id": it.installation_id, "x": it.x, "y": it.y,
                "uncertainty_radius": it.uncertainty_radius,
                "verdict": it.verdict,
                "license": lic_codes.get(it.license_id) if it.license_id else None,
                "matched_licenses": [lic_codes.get(i) for i in it.matched_license_ids],
                "deny_license": lic_codes.get(it.deny_license_id) if it.deny_license_id else None,
                "reasons": it.reasons,
            }
            for it in items
        ],
    }


def delivery_audit(s: Session, user_code: str | None = None) -> list[dict]:
    q = select(DeliveryRecord).order_by(DeliveryRecord.id)
    user_map = {u.id: u.code for u in s.execute(select(AuthUser)).scalars()}
    if user_code:
        u = s.execute(select(AuthUser).where(AuthUser.code == user_code)).scalar_one_or_none()
        if u is None:
            return []
        q = q.where(DeliveryRecord.user_id == u.id)
    out = []
    for rec in s.execute(q).scalars():
        claims = s.execute(
            select(ClaimRecord).where(ClaimRecord.task_id == rec.task_id)
            .order_by(ClaimRecord.id)
        ).scalars()
        out.append({
            "delivery_id": rec.id, "task_id": rec.task_id,
            "user": user_map.get(rec.user_id), "content_hash": rec.content_hash,
            "delivered_scope": rec.delivered_scope, "license_ids": rec.license_ids,
            "station_codes": rec.station_codes, "cell_count": rec.cell_count,
            "created_at": rec.created_at.isoformat(),
            "claims": [
                {"result": c.result, "detail": c.detail, "at": c.created_at.isoformat()}
                for c in claims
            ],
        })
    return out


# ============================== 预计算成员关系 ==============================

def _stale_memberships_for_licenses(s: Session, license_ids: list[int]) -> None:
    if not license_ids:
        return
    rows = s.execute(
        select(LicenseMembership).where(
            LicenseMembership.license_id.in_(license_ids),
            LicenseMembership.status == "ACTIVE")
    ).scalars()
    for r in rows:
        r.status = "STALE"


def _stale_memberships_for_zones(s: Session, zone_codes: list[str]) -> None:
    lic_ids = [l.id for l in s.execute(
        select(AccessLicense).where(AccessLicense.zone_code.in_(zone_codes))).scalars()]
    _stale_memberships_for_licenses(s, lic_ids)


def _stale_memberships_for_stations(s: Session, station_ids: list[int]) -> None:
    rows = s.execute(
        select(LicenseMembership).where(
            LicenseMembership.station_id.in_(station_ids),
            LicenseMembership.status == "ACTIVE")
    ).scalars()
    for r in rows:
        r.status = "STALE"


def rebuild_memberships(s: Session, license_id: int | None = None,
                        now: datetime | None = None) -> dict:
    """预生成 站点安装区间 × 许可 的成员关系。

    迁站、边界或授权日期更正后必须重建相关成员；重建期间 ACTIVE 行缺失，
    membership 模式求值直接 503，绝不回退使用 STALE 放行关系。
    """
    now = now or utcnow()
    q = select(AccessLicense)
    if license_id is not None:
        q = q.where(AccessLicense.id == license_id)
    licenses = list(s.execute(q.order_by(AccessLicense.id)).scalars())
    created = 0
    for lic in licenses:
        _stale_memberships_for_licenses(s, [lic.id])
        installations = list(s.execute(select(StationInstallation)).scalars())
        for inst in installations:
            # 安装区间与许可观测适用日期的交集
            lo = max(_aware(inst.valid_from), _aware(lic.obs_valid_from))
            hi_candidates = [inst.valid_to, lic.obs_valid_to]
            hi_candidates = [_aware(x) for x in hi_candidates if x is not None]
            hi = min(hi_candidates) if hi_candidates else None
            if hi is not None and hi <= lo:
                continue
            zat = zones_at(s, lic.zone_code, lo)
            if not zat:
                continue
            _check_common_crs(s, zat)
            verdict = classify_against_zones((inst.x, inst.y), inst.uncertainty_radius, zat)
            # OUT 也落 ACTIVE 行：membership 模式据此区分“已核对在区外”与
            # “重建中行缺失（503，不得放行）”
            s.add(LicenseMembership(
                license_id=lic.id, station_id=inst.station_id,
                valid_from=lo, valid_to=hi, geometry_verdict=verdict,
                status="ACTIVE", rebuilt_at=now,
                detail={"installation_id": inst.id, "zone_code": lic.zone_code},
            ))
            created += 1
    s.commit()
    return {"rebuilt_licenses": len(licenses), "membership_rows": created}
