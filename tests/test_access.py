"""空间授权治理服务层测试。

覆盖：多许可维度串用、内部孔洞、边界规则、误差跨边界、迁站前后、
DENY 优先、撤回/未生效、低频后端重算、授权日期跨界格元、部分站点区域汇总、
辅助字段约束、同观测多许可只纳一次、分页撤回终止、文件生成后取消与受控领取、
成员关系预计算与失效保护、无效几何配置问题、跨用户不泄漏、审计可追溯。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import access, config, services
from app.geometry import GeometryError, classify_uncertainty, validate_polygon
from app.models import (
    AccessLicense,
    AuthUser,
    ClaimRecord,
    Datum,
    DatumConversion,
    DatumVersion,
    ExtractionTask,
    LicenseMembership,
    RangeEvaluation,
    RangeEvaluationItem,
    SpatialCrs,
    SpatialZone,
    Station,
    StationDatumHistory,
    StationInstallation,
)

W0 = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
W6 = W0 + timedelta(hours=6)
T_MOVE = W0 + timedelta(hours=3)
PAST = datetime(2025, 1, 1, tzinfo=timezone.utc)

NORTH = "north-bay"
SOUTH = "south-bay"

NORTH_POLY = {
    "exterior": [(0, 0), (100, 0), (100, 100), (0, 100)],
    "holes": [[(40, 40), (60, 40), (60, 60), (40, 60)]],
}
SOUTH_POLY = {
    "exterior": [(0, 100), (100, 100), (100, 200), (0, 200)],
    "holes": [],
}
BASE_FIELDS = [f for f in access.DEFAULT_FIELDS]


# ----------------------------- 世界构造 -----------------------------

def build_world(s):
    """CRS + 两个多边形港区 + 7 个站点（含孔洞/边界/误差/迁站）+ 基准。"""
    crs = SpatialCrs(code="CRS-DEMO", name="演示共同坐标系", length_unit="m")
    s.add(crs)
    s.flush()
    s.add(SpatialZone(code=NORTH, version_no=1, name="北湾", crs_id=crs.id,
                      polygons=[NORTH_POLY], boundary_rule="INCLUDE",
                      valid_from=PAST))
    s.add(SpatialZone(code=SOUTH, version_no=1, name="南湾", crs_id=crs.id,
                      polygons=[SOUTH_POLY], boundary_rule="EXCLUDE",
                      valid_from=PAST))

    nd = Datum(code="ND85", name="目标高程")
    zero = Datum(code="Z0", name="站点零点")
    s.add_all([nd, zero])
    s.flush()
    v1 = DatumVersion(note="bootstrap")
    s.add(v1)
    s.flush()
    s.add(DatumConversion(from_datum_id=zero.id, to_datum_id=nd.id, offset_m=0.5,
                          valid_from=PAST, valid_to=None, created_in_version=v1.id))

    def station(code, region, x, y, r=0.0):
        st = Station(code=code, name=code, region=region, unit="m", positive="up",
                     nominal_interval_sec=600, datum_id=zero.id)
        s.add(st)
        s.flush()
        s.add(StationDatumHistory(station_id=st.id, datum_id=zero.id,
                                  created_in_version=v1.id))
        s.add(StationInstallation(station_id=st.id, x=x, y=y, uncertainty_radius=r,
                                  valid_from=PAST))
        return st

    station("ST-N1", "nb", 10, 10)            # 北湾内部
    station("ST-N2", "nb", 50, 50)            # 北湾孔洞内部
    station("ST-N3", "nb", 25, 75, r=30.0)    # 误差圆横跨边界/孔洞
    station("ST-EDGE", "nb", 0, 50)           # 北湾边界（INCLUDE）
    station("ST-B1", "sb", 10, 110)           # 南湾内部
    station("ST-EDGE2", "sb", 100, 150)       # 南湾边界（EXCLUDE）
    st_move = station("ST-MOVE", "nb", 10, 10)
    # 迁站：03:00 起搬到南湾；旧安装闭区间，新安装半开
    s.flush()
    old = s.execute(select(StationInstallation).where(
        StationInstallation.station_id == st_move.id)).scalar_one()
    old.valid_to = T_MOVE
    s.add(StationInstallation(station_id=st_move.id, x=10, y=190, uncertainty_radius=0.0,
                              valid_from=T_MOVE, note="搬迁至南湾"))

    u_port = AuthUser(code="u_port", name="港口用户", api_key="key-port")
    u_deny = AuthUser(code="u_deny", name="拒绝测试用户", api_key="key-deny")
    u_dual = AuthUser(code="u_dual", name="双许可用户", api_key="key-dual")
    s.add_all([u_port, u_deny, u_dual])
    s.flush()

    # 题面场景：北湾低频确认产品 + 南湾高频初报
    s.add(AccessLicense(
        code="L1", user_id=u_port.id, effect="ALLOW", zone_code=NORTH,
        obs_valid_from=W0, obs_valid_to=W6, stages=["CONFIRMED"],
        allowed_fields=BASE_FIELDS, min_grid_seconds=3600,
        valid_from=PAST))
    s.add(AccessLicense(
        code="L2", user_id=u_port.id, effect="ALLOW", zone_code=SOUTH,
        obs_valid_from=W0, obs_valid_to=W6, stages=["PRELIMINARY"],
        allowed_fields=BASE_FIELDS + ["max_m", "min_m", "extreme_max_at", "extreme_min_at"],
        min_grid_seconds=600, valid_from=PAST))
    # DENY 用户：北湾全窗 ALLOW，02:00-04:00 DENY
    s.add(AccessLicense(
        code="LD1", user_id=u_deny.id, effect="ALLOW", zone_code=NORTH,
        obs_valid_from=W0, obs_valid_to=W6, stages=["PRELIMINARY"],
        allowed_fields=BASE_FIELDS, min_grid_seconds=600, valid_from=PAST))
    s.add(AccessLicense(
        code="LD2", user_id=u_deny.id, effect="DENY", zone_code=NORTH,
        obs_valid_from=W0 + timedelta(hours=2), obs_valid_to=W0 + timedelta(hours=4),
        stages=["PRELIMINARY"], allowed_fields=BASE_FIELDS,
        min_grid_seconds=600, valid_from=PAST))
    # 双许可用户：两条 ALLOW 同时覆盖北湾
    for code in ("LU1", "LU2"):
        s.add(AccessLicense(
            code=code, user_id=u_dual.id, effect="ALLOW", zone_code=NORTH,
            obs_valid_from=W0, obs_valid_to=W6, stages=["CONFIRMED"],
            allowed_fields=BASE_FIELDS, min_grid_seconds=3600, valid_from=PAST))
    s.commit()
    return {"crs": crs, "users": {"port": u_port, "deny": u_deny, "dual": u_dual}}


def feed_hourly(s, station_code, hours=6, start=W0, value=1.0, step_min=60):
    items = [{
        "station": station_code, "sample_seq": i + 1,
        "observed_at": start + timedelta(minutes=step_min * i),
        "value": value, "unit": "m", "quality_flag": "GOOD",
    } for i in range(hours)]
    return services.ingest_batch(s, items)


def scope(**over):
    base = {
        "window_start": W0, "window_end": W6, "grid_seconds": 3600,
        "stage": "CONFIRMED", "fields": list(BASE_FIELDS), "target_datum": "ND85",
        "region": "nb", "product_kind": "STATION", "boundary_cells": "MISSING",
    }
    base.update(over)
    return base


def verdicts(s, ev_id):
    rows = s.execute(select(RangeEvaluationItem).where(
        RangeEvaluationItem.evaluation_id == ev_id)).scalars()
    return {(r.station_id, r.observed_at): r for r in rows}


def user(s, code="u_port"):
    return s.execute(select(AuthUser).where(AuthUser.code == code)).scalar_one()


def lic(s, code):
    return s.execute(select(AccessLicense).where(AccessLicense.code == code)).scalar_one()


def station(s, code):
    return s.execute(select(Station).where(Station.code == code)).scalar_one()


# ----------------------------- 几何引擎单测 -----------------------------

def test_invalid_self_intersecting_polygon_rejected():
    bowtie = [(0, 0), (10, 10), (0, 10), (10, 0)]
    with pytest.raises(GeometryError):
        validate_polygon(bowtie)


def test_hole_and_boundary_membership():
    poly = validate_polygon(NORTH_POLY["exterior"], NORTH_POLY["holes"])
    # 孔洞内部算区外，尽管被外环包围且 bbox 命中
    assert classify_uncertainty((50, 50), 0.0, poly, "INCLUDE") == "OUT"
    # 外环内部、孔洞外
    assert classify_uncertainty((10, 10), 0.0, poly, "INCLUDE") == "IN"
    # 边界：INCLUDE 收，EXCLUDE 拒
    assert classify_uncertainty((0, 50), 0.0, poly, "INCLUDE") == "IN"
    assert classify_uncertainty((0, 50), 0.0, poly, "EXCLUDE") == "OUT"
    # 误差圆横跨孔洞/外边界 → 不可确定，不以中心点放行
    assert classify_uncertainty((25, 75), 30.0, poly, "INCLUDE") == "UNCERTAIN"


def test_second_crs_is_config_problem(session):
    build_world(session)
    with pytest.raises(services.ServiceError) as ei:
        access.create_crs(session, "CRS-OTHER", "另一坐标系", "m")
    assert ei.value.status == 422


def test_invalid_zone_polygon_rejected(session):
    build_world(session)
    with pytest.raises(services.ServiceError) as ei:
        access.create_zone(session, "bad-zone", "坏多边形",
                           [{"exterior": [(0, 0), (1, 1), (0, 1), (1, 0)], "holes": []}],
                           PAST, None)
    assert ei.value.status == 422 and "invalid polygon" in ei.value.detail


# ----------------------------- 联合范围求值 -----------------------------

def test_multi_license_dimensions_never_cross_product(session):
    """北湾低频确认 + 南湾高频初报：拼不出北湾高频序列。"""
    build_world(session)
    for code in ("ST-N1", "ST-B1"):
        feed_hourly(session, code)
    u = user(session)

    # 高频初报请求：北湾 ST-N1 两条许可都不满足（L1 阶段/粒度不符，L2 几何不符）
    ev = access.evaluate_scope(session, u, scope(stage="PRELIMINARY", grid_seconds=600,
                                                 region=None,
                                                 stations=["ST-N1", "ST-B1"]))
    v = verdicts(session, ev.id)
    n1 = [r for (sid, _), r in v.items() if sid == station(session, "ST-N1").id]
    b1 = [r for (sid, _), r in v.items() if sid == station(session, "ST-B1").id]
    assert all(r.verdict == "NO_LICENSE" for r in n1)
    assert any("forbid this fine grid" in x or "stage" in x
               for r in n1 for x in r.reasons)
    # 南湾高频初报正常放行
    assert all(r.verdict == "ALLOWED" and r.license_id == lic(session, "L2").id for r in b1)

    # 低频确认请求：北湾走 L1，南湾 L2 阶段不符 → 南湾不放行
    ev2 = access.evaluate_scope(session, u, scope(stage="CONFIRMED", grid_seconds=3600,
                                                  region=None,
                                                  stations=["ST-N1", "ST-B1"]))
    v2 = verdicts(session, ev2.id)
    n1b = [r for (sid, _), r in v2.items() if sid == station(session, "ST-N1").id]
    b1b = [r for (sid, _), r in v2.items() if sid == station(session, "ST-B1").id]
    assert all(r.verdict == "ALLOWED" and r.license_id == lic(session, "L1").id for r in n1b)
    assert all(r.verdict == "NO_LICENSE" for r in b1b)


def test_hole_excludes_and_uncertain_not_released(session):
    build_world(session)
    for code in ("ST-N1", "ST-N2", "ST-N3"):
        feed_hourly(session, code)
    ev = access.evaluate_scope(session, user(session), scope())
    v = list(verdicts(session, ev.id).values())
    by_station = {}
    for r in v:
        by_station.setdefault(r.station_id, []).append(r)
    # 孔洞内站点：bbox 命中但几何在外 → 不放行
    assert all(r.verdict == "NO_LICENSE" for r in by_station[station(session, "ST-N2").id])
    # 误差跨边界：单列 UNCERTAIN，不用中心点放行
    n3 = by_station[station(session, "ST-N3").id]
    assert all(r.verdict == "UNCERTAIN" for r in n3)
    assert all("cannot prove fully inside" in x for r in n3 for x in r.reasons)
    # 正常内部站点放行
    assert all(r.verdict == "ALLOWED" for r in by_station[station(session, "ST-N1").id])

    # 重算结果只含放行站点，UNCERTAIN 单列计数，绝不进入可领取内容
    result = access.recompute_restricted(session, ev)
    assert station(session, "ST-N3").id not in result["stations"]
    assert station(session, "ST-N2").id not in result["stations"]
    assert result["uncertain_count"] == len(n3)


def test_boundary_rules_include_vs_exclude(session):
    build_world(session)
    feed_hourly(session, "ST-EDGE")
    feed_hourly(session, "ST-EDGE2")
    # u_dual 只有北湾 CONFIRMED 许可，无 DENY 干扰；再补一条南湾 CONFIRMED 许可，
    # 使 ST-EDGE2 的南湾边界 EXCLUDE 规则成为决定性因素
    u = user(session, "u_dual")
    session.add(AccessLicense(
        code="LUS", user_id=u.id, effect="ALLOW", zone_code=SOUTH,
        obs_valid_from=W0, obs_valid_to=W6, stages=["CONFIRMED"],
        allowed_fields=BASE_FIELDS, min_grid_seconds=3600, valid_from=PAST))
    session.commit()
    ev = access.evaluate_scope(session, u,
                               scope(stage="CONFIRMED", grid_seconds=3600, region=None,
                                     stations=["ST-EDGE", "ST-EDGE2"]))
    rows = list(verdicts(session, ev.id).values())
    edge = [r for r in rows if r.station_id == station(session, "ST-EDGE").id]
    edge2 = [r for r in rows if r.station_id == station(session, "ST-EDGE2").id]
    assert all(r.verdict == "ALLOWED" for r in edge)     # 北湾 INCLUDE
    assert all(r.verdict == "NO_LICENSE" for r in edge2)  # 南湾 EXCLUDE


def test_relocation_keeps_old_observation_eligibility(session):
    """搬迁不改变旧观测的位置资格：旧观测按旧安装点解释。"""
    build_world(session)
    feed_hourly(session, "ST-MOVE")  # 00:00..05:00，03:00 迁站
    ev = access.evaluate_scope(session, user(session), scope(
        region=None, stations=["ST-MOVE"]))
    rows = list(verdicts(session, ev.id).values())
    before = [r for r in rows if r.observed_at < T_MOVE]
    after = [r for r in rows if r.observed_at >= T_MOVE]
    # 旧观测在北湾 → L1 放行；新观测在南湾，L1 几何不符、L2 阶段不符 → 不放行
    assert all(r.verdict == "ALLOWED" for r in before)
    assert all(r.verdict == "NO_LICENSE" for r in after)
    # 求值明细绑定的是观测时刻适用的安装记录
    inst_ids = {r.installation_id for r in rows}
    assert len(inst_ids) == 2

    # 南湾高频初报视角：只有搬迁后的观测合格
    ev2 = access.evaluate_scope(session, user(session), scope(
        stage="PRELIMINARY", grid_seconds=600, region=None, stations=["ST-MOVE"]))
    rows2 = list(verdicts(session, ev2.id).values())
    assert all(r.verdict == "NO_LICENSE" for r in rows2 if r.observed_at < T_MOVE)
    assert all(r.verdict == "ALLOWED" for r in rows2 if r.observed_at >= T_MOVE)


def test_deny_takes_priority_in_its_scope(session):
    build_world(session)
    feed_hourly(session, "ST-N1")
    ev = access.evaluate_scope(session, user(session, "u_deny"),
                               scope(stage="PRELIMINARY", grid_seconds=600))
    rows = list(verdicts(session, ev.id).values())
    denied = [r for r in rows if W0 + timedelta(hours=2) <= r.observed_at < W0 + timedelta(hours=4)]
    allowed = [r for r in rows if r not in denied]
    assert all(r.verdict == "DENIED" and r.deny_license_id == lic(session, "LD2").id
               for r in denied)
    assert all(r.verdict == "ALLOWED" for r in allowed)


def test_revoked_and_future_licenses_never_admit(session):
    build_world(session)
    feed_hourly(session, "ST-B1")
    u = user(session)
    # 撤回 L2：南湾观测立即失去放行
    access.revoke_license(session, lic(session, "L2").id, "窗口中途撤回")
    ev = access.evaluate_scope(session, u, scope(stage="PRELIMINARY", grid_seconds=600,
                                                 region=None, stations=["ST-B1"]))
    assert all(r.verdict == "NO_LICENSE"
               for r in verdicts(session, ev.id).values())

    # 尚未生效的许可不参与（显式固定求值时刻为 W0，使 W0+30d 确实是未来许可）
    session.add(AccessLicense(
        code="LFUTURE", user_id=u.id, effect="ALLOW", zone_code=SOUTH,
        obs_valid_from=W0, obs_valid_to=W6, stages=["PRELIMINARY"],
        allowed_fields=BASE_FIELDS, min_grid_seconds=600,
        valid_from=W0 + timedelta(days=30)))
    session.commit()
    ev2 = access.evaluate_scope(session, u, scope(stage="PRELIMINARY", grid_seconds=600,
                                                  region=None, stations=["ST-B1"]), now=W0)
    assert all(r.verdict == "NO_LICENSE"
               for r in verdicts(session, ev2.id).values())


# ----------------------------- 授权约束下的重算 -----------------------------

def test_low_frequency_is_recomputed_backend_not_downsampled(session):
    build_world(session)
    # 10 分钟高频原始样本，许可最细粒度 1 小时
    feed_hourly(session, "ST-N1", hours=36, step_min=10)
    u = user(session)
    # 请求比许可更细的粒度：拒绝，不会先吐高频行再让前端降采样
    with pytest.raises(services.ServiceError) as ei:
        ev = access.evaluate_scope(session, u, scope(grid_seconds=600))
        access.recompute_restricted(session, ev)
    assert ei.value.status == 409
    ev = access.evaluate_scope(session, u, scope(grid_seconds=3600))
    result = access.recompute_restricted(session, ev)
    cells = result["stations"][station(session, "ST-N1").id]
    assert len(cells) == 6
    # 每格由 6 个以上合法样本在后端重算（1.0 + 0.5 偏移），覆盖来自实际合法输入；
    # Voronoi 代表时段使相邻边界样本各贡献部分权重，故边界格样本数可为 7
    assert all(c.quality_grade == "MEASURED" and abs(c.value_m - 1.5) < 1e-9 for c in cells)
    assert all(c.sample_count >= 6 for c in cells)


def test_cell_crossing_license_date_boundary(session):
    build_world(session)
    feed_hourly(session, "ST-N1")
    # 把 L1 的观测适用日期截到 02:15，02:00-03:00 格元跨界
    l1 = lic(session, "L1")
    l1.obs_valid_to = W0 + timedelta(hours=2, minutes=15)
    session.commit()
    ev = access.evaluate_scope(session, user(session), scope(boundary_cells="MISSING"))
    result = access.recompute_restricted(session, ev)
    cells = result["stations"][station(session, "ST-N1").id]
    cross = [c for c in cells if c.grid_start == W0 + timedelta(hours=2)][0]
    assert cross.boundary_cross is True
    assert cross.quality_grade == "MISSING" and cross.value_m is None
    assert any("license date boundary" in x for x in cross.quality_reasons)
    # 03:00 之后的格元全部缺测，未获准时段没有被拿来平均
    later = [c for c in cells if c.grid_start >= W0 + timedelta(hours=3)]
    assert all(c.quality_grade == "MISSING" for c in later)

    # 契约另一选项：跨界格元直接拒绝
    ev2 = access.evaluate_scope(session, user(session), scope(boundary_cells="DECLINE"))
    result2 = access.recompute_restricted(session, ev2)
    cross2 = [c for c in result2["stations"][station(session, "ST-N1").id]
              if c.grid_start == W0 + timedelta(hours=2)][0]
    assert cross2.quality_grade == "DECLINED"


def test_partial_region_aggregate_uses_legal_denominator(session):
    build_world(session)
    for code in ("ST-N1", "ST-N2", "ST-N3"):
        feed_hourly(session, code)
    ev = access.evaluate_scope(session, user(session),
                               scope(region="nb", stations=["ST-N1", "ST-N2", "ST-N3"],
                                     product_kind="REGION_AGG"))
    result = access.recompute_restricted(session, ev)
    agg = result["aggregate"]
    # 明确命名的受限范围产品，不伪装成全区域汇总
    assert "restricted" in agg["aggregate_name"]
    assert agg["authorized_station_codes"] == ["ST-N1"]
    assert set(agg["excluded_station_codes"]) == {"ST-N2", "ST-N3"}
    measured = [r for r in agg["rows"] if r["quality_grade"] == "MEASURED"]
    # 分母是合法且有值的站点数 1，未授权站点没有被当零水位
    assert all(r["station_count"] == 1 and abs(r["value_m"] - 1.5) < 1e-9
               for r in measured)


def test_aux_fields_cannot_bypass_grid_or_field_limits(session):
    build_world(session)
    feed_hourly(session, "ST-N1", hours=36, step_min=10)
    feed_hourly(session, "ST-B1", hours=36, step_min=10)
    u = user(session)
    # 南湾 L2 允许 aux：PRELIMINARY/600 下 ST-B1 放行并带极值；
    # 北湾 L1 粒度 3600 > 600，不参与，北湾数据根本不进入该产品
    ev = access.evaluate_scope(session, u, scope(
        stage="PRELIMINARY", grid_seconds=600, region=None, stations=["ST-B1"],
        fields=BASE_FIELDS + ["max_m", "extreme_max_at"]))
    manifest = access._build_manifest(session, ev, access.recompute_restricted(session, ev))
    assert [s["station_id"] for s in manifest["stations"]] == [station(session, "ST-B1").id]
    south_cells = [c for c in manifest["stations"][0]["cells"] if c["value_m"] is not None]
    assert south_cells and all("max_m" in c and "extreme_max_at" in c for c in south_cells)

    # 北湾 L1 不含 aux：即使请求 aux 字段，CONFIRMED/3600 产品的格元也不出现极值，
    # 无法用最大值/极值时刻等辅助字段绕过粒度限制
    ev2 = access.evaluate_scope(session, u, scope(
        region=None, stations=["ST-N1"],
        fields=BASE_FIELDS + ["max_m", "extreme_max_at"]))
    manifest2 = access._build_manifest(session, ev2, access.recompute_restricted(session, ev2))
    north_cells = manifest2["stations"][0]["cells"]
    assert all("max_m" not in c and "extreme_max_at" not in c for c in north_cells)


def test_same_observation_under_multiple_licenses_counted_once(session):
    build_world(session)
    feed_hourly(session, "ST-N1", hours=36, step_min=10)  # 密采样保证 1h 格覆盖达标
    ev = access.evaluate_scope(session, user(session, "u_dual"), scope())
    rows = list(verdicts(session, ev.id).values())
    assert all(r.verdict == "ALLOWED" and len(r.matched_license_ids) == 2 for r in rows)
    result = access.recompute_restricted(session, ev)
    cells_dual = result["stations"][station(session, "ST-N1").id]
    # 与仅持单条许可的用户在同一数据上对比：样本数一致（没有因两条许可翻倍），
    # 权重不翻倍 -> 有值格元值仍为 1.5，且格元记录两条许可（尾部缺测格元保留缺测）
    ev_single = access.evaluate_scope(session, user(session, "u_port"), scope())
    cells_single = access.recompute_restricted(
        session, ev_single)["stations"][station(session, "ST-N1").id]
    assert [c.sample_count for c in cells_dual] == [c.sample_count for c in cells_single]
    measured = [c for c in cells_dual if c.value_m is not None]
    assert measured and all(abs(c.value_m - 1.5) < 1e-9 for c in measured)
    assert all(set(c.license_ids) == {lic(session, "LU1").id, lic(session, "LU2").id}
               for c in measured)


# ----------------------------- 分页与撤回控制 -----------------------------

def test_pagination_each_page_checks_current_license(session):
    build_world(session)
    feed_hourly(session, "ST-N1")
    u = user(session)
    ev = access.evaluate_scope(session, u, scope())
    sess = access.create_session(session, u, ev, page_size=2)
    p1 = access.get_page(session, u, sess.token, None)
    assert len(p1["page"]) == 2 and p1["next_cursor"]
    p2 = access.get_page(session, u, sess.token, p1["next_cursor"])
    assert len(p2["page"]) == 2
    # 窗口中途撤回：旧游标不能把已取消的访问权延长到整段历史
    access.revoke_license(session, lic(session, "L1").id, "分页途中撤回")
    with pytest.raises(services.ServiceError) as ei:
        access.get_page(session, u, sess.token, p2["next_cursor"])
    assert ei.value.status == 409 and "terminated" in ei.value.detail
    session.refresh(sess)
    assert sess.status == "TERMINATED"
    # 终止后任何旧游标都不可再用
    with pytest.raises(services.ServiceError):
        access.get_page(session, u, sess.token, p1["next_cursor"])


# ----------------------------- 文件生成与受控领取 -----------------------------

def test_extraction_staging_promote_claim_and_revocation(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXTRACT_DIR", str(tmp_path))
    build_world(session)
    feed_hourly(session, "ST-N1")
    u = user(session)
    sc = scope()

    # 暂存内容不可直接领取（领取尝试被拒并留痕，不产生交付登记）
    task = access.create_extraction_task(session, u, sc, "T1")
    assert task.status == "STAGED" and task.ready_path is None
    with pytest.raises(services.ServiceError) as ei:
        access.claim_task(session, u, task.id)
    assert ei.value.status == 409
    pre_claims = session.execute(select(ClaimRecord).where(
        ClaimRecord.task_id == task.id)).scalars().all()
    assert [c.result for c in pre_claims] == ["DENIED_NOT_READY"]

    ready = access.promote_task(session, u, task.id)
    assert ready.status == "READY" and ready.claim_token
    claimed = access.claim_task(session, u, task.id)
    assert claimed["content_hash"] == task.content_hash
    # 文件内容只含放行站点与获准字段
    assert claimed["content"]["scope_hash"] == ev_scope_hash(session, task.evaluation_id)
    assert {s["station_id"] for s in claimed["content"]["stations"]} == {
        station(session, "ST-N1").id}

    # 一次领取完成后不能重复领取，只保留已交付确切范围的记录
    with pytest.raises(services.ServiceError) as ei2:
        access.claim_task(session, u, task.id)
    assert ei2.value.status == 409
    claims = session.execute(select(ClaimRecord).where(
        ClaimRecord.task_id == task.id)).scalars().all()
    assert [c.result for c in claims] == ["DENIED_NOT_READY", "CLAIMED", "ALREADY_CLAIMED"]

    # 生成后、领取前撤回：受控领取接口拦截，不发文件
    task2 = access.create_extraction_task(session, u, sc, "T2")
    access.promote_task(session, u, task2.id)
    access.revoke_license(session, lic(session, "L1").id, "领取前撤回")
    with pytest.raises(services.ServiceError) as ei3:
        access.claim_task(session, u, task2.id)
    assert ei3.value.status == 409
    session.refresh(task2)
    assert task2.status == "REVOKED"
    denied = session.execute(select(ClaimRecord).where(
        ClaimRecord.task_id == task2.id, ClaimRecord.result == "DENIED_REVOKED")).scalars().all()
    assert len(denied) == 1

    # 相同请求重试：当前范围已变，旧暂存文件不得复用
    with pytest.raises(services.ServiceError) as ei4:
        access.create_extraction_task(session, u, sc, "T2")
    assert ei4.value.status == 409


def ev_scope_hash(s, ev_id):
    return s.get(RangeEvaluation, ev_id).scope_hash


def test_promotion_rechecks_range_and_discards_staged_file(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXTRACT_DIR", str(tmp_path))
    build_world(session)
    feed_hourly(session, "ST-N1")
    u = user(session)
    task = access.create_extraction_task(session, u, scope(), "T3")
    access.revoke_license(session, lic(session, "L1").id, "晋升前撤回")
    with pytest.raises(services.ServiceError) as ei:
        access.promote_task(session, u, task.id)
    assert ei.value.status == 409
    session.refresh(task)
    assert task.status == "FAILED" and task.ready_path is None


# ----------------------------- 成员关系预计算 -----------------------------

def test_membership_mode_requires_prior_rebuild(session):
    build_world(session)
    feed_hourly(session, "ST-N1")
    # 未重建成员关系时不静默按全区外处理
    with pytest.raises(services.ServiceError) as ei:
        access.evaluate_scope(session, user(session), scope(), mode="membership")
    assert ei.value.status == 503 and "not built" in ei.value.detail


def test_membership_mode_matches_online_and_guards_stale(session):
    build_world(session)
    feed_hourly(session, "ST-N1")
    feed_hourly(session, "ST-N2")
    u = user(session)
    stats = access.rebuild_memberships(session)
    assert stats["membership_rows"] >= 1
    # 孔洞站点以 OUT 结论落 ACTIVE 行（区别于重建中“无行可用”）；内部站点为 IN。
    # 按北湾许可 L1 过滤，避免与南湾许可下的同站 OUT 行混淆
    l1_id = lic(session, "L1").id
    rows = session.execute(select(LicenseMembership).where(
        LicenseMembership.status == "ACTIVE",
        LicenseMembership.license_id == l1_id)).scalars().all()
    n1_rows = [m for m in rows if m.station_id == station(session, "ST-N1").id]
    n2_rows = [m for m in rows if m.station_id == station(session, "ST-N2").id]
    assert n1_rows and all(m.geometry_verdict == "IN" for m in n1_rows)
    assert n2_rows and all(m.geometry_verdict == "OUT" for m in n2_rows)

    ev_online = access.evaluate_scope(session, u, scope(), mode="online")
    ev_member = access.evaluate_scope(session, u, scope(), mode="membership")
    on = sorted((r.obs_id, r.verdict) for r in verdicts(session, ev_online.id).values())
    mb = sorted((r.obs_id, r.verdict) for r in verdicts(session, ev_member.id).values())
    assert on == mb

    # 边界更正：旧成员置 STALE，membership 求值拒绝使用失效关系
    old_zone = session.execute(select(SpatialZone).where(
        SpatialZone.code == NORTH, SpatialZone.version_no == 1)).scalar_one()
    old_zone.valid_to = W0 + timedelta(days=1)
    session.commit()
    access.create_zone(session, NORTH, "北湾更正", [NORTH_POLY], W0 + timedelta(days=1), None)
    with pytest.raises(services.ServiceError) as ei:
        access.evaluate_scope(session, u, scope(), mode="membership")
    assert ei.value.status == 503
    # online 模式不受影响（实时几何求值）
    ev3 = access.evaluate_scope(session, u, scope(), mode="online")
    assert ev3.id > 0
    # 重建后恢复：ST-N1 重新放行，孔洞站 ST-N2 仍按 OUT 排除
    access.rebuild_memberships(session)
    ev4 = access.evaluate_scope(session, u, scope(), mode="membership")
    v4 = list(verdicts(session, ev4.id).values())
    n1_after = [r for r in v4 if r.station_id == station(session, "ST-N1").id]
    n2_after = [r for r in v4 if r.station_id == station(session, "ST-N2").id]
    assert n1_after and all(r.verdict == "ALLOWED" for r in n1_after)
    assert n2_after and all(r.verdict == "NO_LICENSE" for r in n2_after)


# ----------------------------- 审计与隔离 -----------------------------

def test_audit_traces_every_release_to_conditions(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXTRACT_DIR", str(tmp_path))
    build_world(session)
    feed_hourly(session, "ST-N1")
    u = user(session)
    task = access.create_extraction_task(session, u, scope(), "TAUD")
    access.promote_task(session, u, task.id)
    access.claim_task(session, u, task.id)

    audit = access.evaluation_audit(session, task.evaluation_id)
    assert audit["license_snapshot"] and audit["scope_hash"]
    allowed = [i for i in audit["items"] if i["verdict"] == "ALLOWED"]
    assert all(i["license"] == "L1" and i["installation_id"] and i["x"] is not None
               for i in allowed)
    # 从交付登记可查回内容哈希、确切范围、接收主体与领取记录
    deliveries = access.delivery_audit(session, user_code="u_port")
    assert len(deliveries) == 1
    rec = deliveries[0]
    assert rec["content_hash"] == task.content_hash
    assert rec["cell_count"] == task.cell_count
    assert any(c["result"] == "CLAIMED" for c in rec["claims"])


def test_user_cannot_see_other_users_resources(session):
    build_world(session)
    feed_hourly(session, "ST-N1")
    ev = access.evaluate_scope(session, user(session, "u_port"), scope())
    # 服务层约定：路由按属主过滤；此处验证求值本身按用户隔离
    ev_other = access.evaluate_scope(session, user(session, "u_deny"),
                                     scope(stage="PRELIMINARY", grid_seconds=600))
    assert ev.user_id != ev_other.user_id
    own = access.evaluation_audit(session, ev.id)
    other = access.evaluation_audit(session, ev_other.id)
    # 同一批观测，两个用户的结论集合必须不同（u_port 全放行；u_deny 含 DENIED 窗口）
    own_verdicts = sorted(i["verdict"] for i in own["items"])
    other_verdicts = sorted(i["verdict"] for i in other["items"])
    assert own_verdicts != other_verdicts
    assert set(own_verdicts) == {"ALLOWED"}
    assert "DENIED" in other_verdicts and "ALLOWED" in other_verdicts
    # 任务按用户隔离查询
    rows = session.execute(select(ExtractionTask).where(
        ExtractionTask.user_id == user(session, "u_deny").id)).scalars().all()
    assert rows == []
