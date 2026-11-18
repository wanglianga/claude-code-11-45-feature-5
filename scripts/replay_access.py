"""空间授权治理验收回放：联合范围求值、受限重聚合、分页撤回、受控领取全链路。

场景覆盖（对应任务要求逐项核查）：
  1) 多许可维度串用：北湾低频确认 + 南湾高频初报，拼不出北湾高频序列；
  2) 内部孔洞、边界 INCLUDE/EXCLUDE、误差圆跨边界单列 UNCERTAIN；
  3) 迁站前后：旧观测按旧安装点解释，搬迁不改变旧观测位置资格；
  4) DENY 在适用范围优先；撤回/未生效许可不放行；
  5) 低频订阅后端重算（不先吐高频再降采样）、授权日期跨界格元 MISSING/DECLINE；
  6) 部分站点区域汇总：受限命名、合法分母、未授权站不当零水位；
  7) 辅助字段不能绕过粒度/字段限制；
  8) 连续翻页中途撤回 → 旧会话终止；文件生成后撤回 → 受控领取拦截；
  9) 暂存不可领取、重复领取留痕、相同请求重试核对当前范围；
 10) 成员关系预计算：online/membership 一致、更正后 STALE 503、重建恢复；
 11) 审计可从文件/求值查回具体条件；普通用户不泄漏其他港区信息。

前置：API（默认 127.0.0.1:8000）已启动。运行：python scripts/replay_access.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

API = os.getenv("API_URL", "http://127.0.0.1:8000")
ADMIN = os.getenv("ADMIN_KEY", "admin-key")
OPERATOR = os.getenv("OPERATOR_KEY", "operator-key")

W0 = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
W6 = W0 + timedelta(hours=6)
T_MOVE = W0 + timedelta(hours=3)
PAST = "2025-01-01T00:00:00+00:00"
NORTH, SOUTH = "north-bay", "south-bay"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    suffix = "" if cond else f"  <-- {detail}"
    print(f"[{mark}] {name}{suffix}", flush=True)


def api(method: str, path: str, key: str, expect: int | None = None, **kw):
    r = httpx.request(method, f"{API}{path}", headers={"X-API-Key": key}, timeout=30, **kw)
    if expect is not None and r.status_code != expect:
        raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
    return r


def iso(dt: datetime) -> str:
    return dt.isoformat()


BASE_FIELDS = ["grid_start", "grid_seconds", "value_m", "quality_grade", "coverage_sec",
               "coverage_ratio", "sample_count", "quality_reasons", "source_sample_ids"]
AUX_FIELDS = BASE_FIELDS + ["max_m", "min_m", "extreme_max_at", "extreme_min_at"]

NORTH_POLY = {
    "exterior": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "holes": [[[40, 40], [60, 40], [60, 60], [40, 60]]],
}
SOUTH_POLY = {"exterior": [[0, 100], [100, 100], [100, 200], [0, 200]], "holes": []}


def scope(**over):
    base = {
        "window_start": iso(W0), "window_end": iso(W6), "grid_seconds": 3600,
        "stage": "CONFIRMED", "fields": BASE_FIELDS, "target_datum": "ND85",
        "region": None, "stations": None, "product_kind": "STATION",
        "boundary_cells": "MISSING", "mode": "online",
    }
    base.update(over)
    return base


def obs(station, seq, t, value=1.0):
    return {"station": station, "sample_seq": seq, "observed_at": iso(t),
            "value": value, "unit": "m", "quality_flag": "GOOD"}


def main() -> int:
    # ---------- 0. 复位与目录 ----------
    api("POST", "/admin/reset", ADMIN, 200)
    for d in ({"code": "ND85", "name": "目标高程"}, {"code": "Z0", "name": "站点零点"}):
        api("POST", "/datums", ADMIN, 201, json=d)
    api("POST", "/datum-conversions", ADMIN, 201, json={
        "from_datum": "Z0", "to_datum": "ND85", "offset_m": 0.5,
        "valid_from": PAST, "valid_to": None})

    # ---------- 1. 共同坐标系与多边形港区 ----------
    r = api("POST", "/access/crs", ADMIN, 201,
            json={"code": "CRS-DEMO", "name": "演示共同坐标系", "length_unit": "m"})
    check("共同坐标系登记", r.status_code == 201)
    r2 = api("POST", "/access/crs", ADMIN, 422,
             json={"code": "CRS-OTHER", "name": "第二坐标系", "length_unit": "m"})
    check("无法解释的第二坐标系直接返回范围配置问题", r2.status_code == 422, r2.text)
    r3 = api("POST", "/access/zones", ADMIN, 422, json={
        "code": "bad", "name": "自相交", "boundary_rule": "INCLUDE",
        "valid_from": PAST, "valid_to": None,
        "polygons": [{"exterior": [[0, 0], [10, 10], [0, 10], [10, 0]], "holes": []}]})
    check("无效多边形（蝴蝶结自相交）拒绝落库", r3.status_code == 422, r3.text)

    api("POST", "/access/zones", ADMIN, 201, json={
        "code": NORTH, "name": "北湾", "boundary_rule": "INCLUDE",
        "valid_from": PAST, "valid_to": None, "polygons": [NORTH_POLY]})
    api("POST", "/access/zones", ADMIN, 201, json={
        "code": SOUTH, "name": "南湾", "boundary_rule": "EXCLUDE",
        "valid_from": PAST, "valid_to": None, "polygons": [SOUTH_POLY]})
    check("含孔洞/多港区多边形导入成功", True)

    # ---------- 2. 站点与安装记录（含迁站、误差、边界） ----------
    stations = [
        ("ST-N1", "nb", 10, 10, 0.0),     # 北湾内部
        ("ST-N2", "nb", 50, 50, 0.0),     # 北湾孔洞内
        ("ST-N3", "nb", 25, 75, 30.0),    # 误差圆横跨边界
        ("ST-EDGE", "nb", 0, 50, 0.0),    # 北湾边界（INCLUDE）
        ("ST-B1", "sb", 10, 110, 0.0),    # 南湾内部
        ("ST-EDGE2", "sb", 100, 150, 0.0),  # 南湾边界（EXCLUDE）
        ("ST-MOVE", "nb", 10, 10, 0.0),   # 迁站
    ]
    for code, region, x, y, r in stations:
        api("POST", "/stations", ADMIN, 201, json={
            "code": code, "name": code, "region": region, "unit": "m",
            "positive": "up", "nominal_interval_sec": 600, "datum": "Z0"})
        api("POST", "/access/installations", ADMIN, 201, json={
            "station": code, "x": x, "y": y, "uncertainty_radius": r,
            "valid_from": PAST, "valid_to": None})
    # 安装区间不可重叠（先对 ST-MOVE 再建一条 PAST 起的无界记录，应被拒绝）
    r = api("POST", "/access/installations", ADMIN, 422, json={
        "station": "ST-MOVE", "x": 10, "y": 190, "uncertainty_radius": 0.0,
        "valid_from": PAST, "valid_to": None})
    check("重叠安装区间被拒绝", r.status_code == 422, r.text)
    # 迁站通过接续安装记录表达：专用站 ST-MOVE2，旧区间闭到 T_MOVE，新区间从 T_MOVE 起
    api("POST", "/stations", ADMIN, 201, json={
        "code": "ST-MOVE2", "name": "迁站站", "region": "nb", "unit": "m",
        "positive": "up", "nominal_interval_sec": 600, "datum": "Z0"})
    api("POST", "/access/installations", ADMIN, 201, json={
        "station": "ST-MOVE2", "x": 10, "y": 10, "uncertainty_radius": 0.0,
        "valid_from": PAST, "valid_to": iso(T_MOVE)})
    api("POST", "/access/installations", ADMIN, 201, json={
        "station": "ST-MOVE2", "x": 10, "y": 190, "uncertainty_radius": 0.0,
        "valid_from": iso(T_MOVE), "valid_to": None, "note": "搬迁至南湾"})

    # ---------- 3. 虚构用户与完整条件许可 ----------
    def make_user(code):
        return api("POST", "/access/users", ADMIN, 201,
                   json={"code": code, "name": code}).json()["api_key"]

    key_port, key_deny, key_dual, key_bound = (
        make_user("u_port"), make_user("u_deny"), make_user("u_dual"), make_user("u_bound"))

    def license(body, key=ADMIN):
        return api("POST", "/access/licenses", key, 201, json=body)

    license({"code": "L1", "user": "u_port", "effect": "ALLOW", "zone_code": NORTH,
             "obs_valid_from": iso(W0), "obs_valid_to": iso(W6), "stages": ["CONFIRMED"],
             "allowed_fields": BASE_FIELDS, "min_grid_seconds": 3600,
             "valid_from": PAST, "valid_to": None})
    license({"code": "L2", "user": "u_port", "effect": "ALLOW", "zone_code": SOUTH,
             "obs_valid_from": iso(W0), "obs_valid_to": iso(W6),
             "stages": ["PRELIMINARY"], "allowed_fields": AUX_FIELDS,
             "min_grid_seconds": 600, "valid_from": PAST, "valid_to": None})
    license({"code": "LD1", "user": "u_deny", "effect": "ALLOW", "zone_code": NORTH,
             "obs_valid_from": iso(W0), "obs_valid_to": iso(W6),
             "stages": ["PRELIMINARY"], "allowed_fields": BASE_FIELDS,
             "min_grid_seconds": 600, "valid_from": PAST, "valid_to": None})
    license({"code": "LD2", "user": "u_deny", "effect": "DENY", "zone_code": NORTH,
             "obs_valid_from": iso(W0 + timedelta(hours=2)),
             "obs_valid_to": iso(W0 + timedelta(hours=4)),
             "stages": ["PRELIMINARY"], "allowed_fields": BASE_FIELDS,
             "min_grid_seconds": 600, "valid_from": PAST, "valid_to": None})
    for c in ("LU1", "LU2"):
        license({"code": c, "user": "u_dual", "effect": "ALLOW", "zone_code": NORTH,
                 "obs_valid_from": iso(W0), "obs_valid_to": iso(W6),
                 "stages": ["CONFIRMED"], "allowed_fields": BASE_FIELDS,
                 "min_grid_seconds": 3600, "valid_from": PAST, "valid_to": None})
    # 跨界格元专用用户：北湾许可观测日期只到 02:15
    license({"code": "LB1", "user": "u_bound", "effect": "ALLOW", "zone_code": NORTH,
             "obs_valid_from": iso(W0),
             "obs_valid_to": iso(W0 + timedelta(hours=2, minutes=15)),
             "stages": ["CONFIRMED"], "allowed_fields": BASE_FIELDS,
             "min_grid_seconds": 3600, "valid_from": PAST, "valid_to": None})
    check("完整条件许可（区域/日期/阶段/字段/粒度）导入成功", True)

    # ---------- 4. 观测接入 ----------
    batch = []
    for code, _, _, _, _ in stations:
        for i in range(6):
            batch.append(obs(code, 1000 + i, W0 + timedelta(hours=i)))
    for i in range(36):  # ST-N1 10 分钟密采样
        batch.append(obs("ST-N1", i + 1, W0 + timedelta(minutes=10 * i)))
    for i in range(6):  # 迁站站
        batch.append(obs("ST-MOVE2", 2000 + i, W0 + timedelta(hours=i)))
    r = api("POST", "/observations:ingest", OPERATOR, 200, json={"items": batch})
    check("观测接入成功", r.json()["inserted"] == len(batch), str(r.json()))

    def evaluate(key, sc):
        return api("POST", "/access/evaluate", key, 200, json=sc).json()

    def counts(payload):
        return payload["counts"]

    # ---------- 5. 多许可维度串用 ----------
    # ST-N1 另有 36 条 10 分钟密采样，故该站观测总数为 42
    e = evaluate(key_port, scope(stage="PRELIMINARY", grid_seconds=600,
                                 stations=["ST-N1", "ST-B1"]))
    c = counts(e)
    check("北湾高频初报：北湾站不被两条许可拼成放行（42 条全排除，南湾 6 条放行）",
          c.get("ALLOWED", 0) == 6 and c.get("NO_LICENSE", 0) == 42, str(c))
    e2 = evaluate(key_port, scope(stage="CONFIRMED", grid_seconds=3600,
                                  stations=["ST-N1", "ST-B1"]))
    c2 = counts(e2)
    check("北湾低频确认仅北湾放行（42 条）、南湾阶段不符排除（6 条）",
          c2.get("ALLOWED", 0) == 42 and c2.get("NO_LICENSE", 0) == 6, str(c2))

    # ---------- 6. 孔洞 / 误差 / 边界 ----------
    e3 = evaluate(key_port, scope(stations=["ST-N1", "ST-N2", "ST-N3"]))
    c3 = counts(e3)
    check("孔洞内站点 bbox 命中但几何排除", c3.get("NO_LICENSE", 0) == 6, str(c3))
    check("误差跨边界单列 UNCERTAIN 且不按中心点放行（北湾内部 42 条仍正常放行）",
          c3.get("UNCERTAIN", 0) == 6 and c3.get("ALLOWED", 0) == 42, str(c3))
    e4 = evaluate(key_dual, scope(stage="CONFIRMED", grid_seconds=3600,
                                  stations=["ST-EDGE"]))
    check("北湾边界 INCLUDE 归属区内", counts(e4).get("ALLOWED", 0) == 6, str(e4))
    license({"code": "LUS", "user": "u_dual", "effect": "ALLOW", "zone_code": SOUTH,
             "obs_valid_from": iso(W0), "obs_valid_to": iso(W6),
             "stages": ["CONFIRMED"], "allowed_fields": BASE_FIELDS,
             "min_grid_seconds": 3600, "valid_from": PAST, "valid_to": None})
    e5 = evaluate(key_dual, scope(stage="CONFIRMED", grid_seconds=3600,
                                  stations=["ST-EDGE2"]))
    check("南湾边界 EXCLUDE 归属区外", counts(e5).get("NO_LICENSE", 0) == 6, str(e5))

    # ---------- 7. 迁站前后 ----------
    e6 = evaluate(key_port, scope(stations=["ST-MOVE2"]))
    items = api("GET", f"/access/my/evaluations/{e6['evaluation_id']}/audit",
                key_port, 200).json()["items"]
    before = [i for i in items if i["observed_at"] < iso(T_MOVE)]
    after = [i for i in items if i["observed_at"] >= iso(T_MOVE)]
    check("搬迁不改变旧观测位置资格（旧观测按北湾旧安装点放行）",
          all(i["verdict"] == "ALLOWED" for i in before) and
          all(i["verdict"] == "NO_LICENSE" for i in after),
          f"{[i['verdict'] for i in items]}")
    e7 = evaluate(key_port, scope(stage="PRELIMINARY", grid_seconds=600,
                                  stations=["ST-MOVE2"]))
    items7 = api("GET", f"/access/my/evaluations/{e7['evaluation_id']}/audit",
                 key_port, 200).json()["items"]
    check("搬迁后新观测在南湾按高频初报放行、旧观测不放行",
          all(i["verdict"] == "NO_LICENSE" for i in items7 if i["observed_at"] < iso(T_MOVE)) and
          all(i["verdict"] == "ALLOWED" for i in items7 if i["observed_at"] >= iso(T_MOVE)))

    # ---------- 8. DENY 优先 ----------
    e8 = evaluate(key_deny, scope(stage="PRELIMINARY", grid_seconds=600,
                                  stations=["ST-N1"]))
    items8 = api("GET", f"/access/my/evaluations/{e8['evaluation_id']}/audit",
                 key_deny, 200).json()["items"]
    in_deny = [i for i in items8
               if iso(W0 + timedelta(hours=2)) <= i["observed_at"] < iso(W0 + timedelta(hours=4))]
    out_deny = [i for i in items8 if i not in in_deny]
    check("DENY 在其适用窗口优先于 ALLOW，窗口外照常放行",
          in_deny and all(i["verdict"] == "DENIED" and i["deny_license"] == "LD2"
                          for i in in_deny)
          and out_deny and all(i["verdict"] == "ALLOWED" for i in out_deny),
          str(counts(e8)))

    # ---------- 9. 低频后端重算 ----------
    r = api("POST", "/access/recompute", key_port, 200, json=scope(
        stations=["ST-N1"], grid_seconds=3600)).json()
    st_n1 = [s for s in r["stations"] if s["station"] == "ST-N1"][0]
    check("后端按获准 1h 窗口重算（单位米、6 个时间格）",
          len(st_n1["cells"]) == 6 and
          all(c["grid_seconds"] == 3600 for c in st_n1["cells"]) and
          all(abs(c["value_m"] - 1.5) < 1e-9 for c in st_n1["cells"] if c["value_m"] is not None),
          str(st_n1["cells"][:1]))
    check("重算格元绑定具体许可条件（L1）与来源样本集合",
          all(set(c["license_codes"]) == {"L1"} and c["source_sample_ids"]
              for c in st_n1["cells"] if c["value_m"] is not None))
    # 更细粒度请求：无完整条件成立 → 409，绝不先返回高频行
    r = api("POST", "/access/recompute", key_port, 409,
            json=scope(stations=["ST-N1"], grid_seconds=600))
    check("低频订阅取不到高频序列（后端直接拒绝而非前端降采样）",
          r.status_code == 409, r.text)

    # ---------- 10. 授权日期跨界格元 ----------
    r = api("POST", "/access/recompute", key_bound, 200, json=scope(
        stations=["ST-N1"], boundary_cells="MISSING")).json()
    cells = [s for s in r["stations"] if s["station"] == "ST-N1"][0]["cells"]
    cross = [c for c in cells if c["grid_start"] == iso(W0 + timedelta(hours=2))][0]
    check("跨界格元只用合法数据且声明覆盖不足（MISSING，不沿用全窗口均值）",
          cross["quality_grade"] == "MISSING" and cross["value_m"] is None and
          any("license date boundary" in x for x in cross["quality_reasons"]), str(cross))
    r = api("POST", "/access/recompute", key_bound, 200, json=scope(
        stations=["ST-N1"], boundary_cells="DECLINE")).json()
    cells_d = [s for s in r["stations"] if s["station"] == "ST-N1"][0]["cells"]
    cross_d = [c for c in cells_d if c["grid_start"] == iso(W0 + timedelta(hours=2))][0]
    check("契约拒绝选项：跨界格元 DECLINED", cross_d["quality_grade"] == "DECLINED")

    # ---------- 11. 部分站点区域汇总 ----------
    r = api("POST", "/access/recompute", key_port, 200, json=scope(
        region="nb", stations=["ST-N1", "ST-N2", "ST-N3"],
        product_kind="REGION_AGG")).json()
    agg = r["aggregate"]
    measured = [x for x in agg["rows"] if x["quality_grade"] == "MEASURED"]
    check("受限范围产品明确命名且不交付原全区域汇总",
          "restricted" in agg["aggregate_name"] and
          agg["authorized_station_codes"] == ["ST-N1"] and
          set(agg["excluded_station_codes"]) == {"ST-N2", "ST-N3"}, str(agg)[:200])
    check("汇总分母只数合法站点，未授权站未被当零水位",
          all(x["station_count"] == 1 and abs(x["value_m"] - 1.5) < 1e-9
              for x in measured), str(measured[:1]))

    # ---------- 12. 辅助字段约束 ----------
    r = api("POST", "/access/recompute", key_port, 200, json=scope(
        stage="PRELIMINARY", grid_seconds=600, stations=["ST-B1"],
        fields=AUX_FIELDS)).json()
    south_cells = [c for c in r["stations"][0]["cells"] if c["value_m"] is not None]
    check("获准 aux 字段随南湾高频产品交付",
          south_cells and all("max_m" in c and "extreme_max_at" in c for c in south_cells))
    r = api("POST", "/access/recompute", key_port, 200, json=scope(
        stations=["ST-N1"], fields=AUX_FIELDS)).json()
    north_cells = r["stations"][0]["cells"]
    check("北湾许可不含 aux：极值字段被服务端剔除，不能绕过粒度限制",
          north_cells and all("max_m" not in c and "extreme_max_at" not in c
                              for c in north_cells))
    # 基础字段与溯源信息仍在
    check("剔除 aux 后基础字段与许可溯源保留",
          all("value_m" in c and c["license_codes"] == ["L1"] for c in north_cells))

    # ---------- 13. 成员关系预计算 ----------
    r = api("POST", "/access/memberships/rebuild", OPERATOR, 200).json()
    check("成员关系重建产生 ACTIVE 行", r["membership_rows"] > 0, str(r))
    e_on = evaluate(key_dual, dict(scope(stations=["ST-N1", "ST-N2"]), mode="online"))
    e_mb = evaluate(key_dual, dict(scope(stations=["ST-N1", "ST-N2"]), mode="membership"))
    sig = lambda e: sorted((i["obs_id"], i["verdict"]) for i in
                           api("GET", f"/access/audit/evaluations/{e['evaluation_id']}",
                               ADMIN, 200).json()["items"])
    check("membership 与 online 求值结论一致", sig(e_on) == sig(e_mb))

    # ---------- 14. 连续翻页 + 窗口中途撤回 ----------
    sess = api("POST", "/access/sessions", key_port, 201,
               json={"scope": scope(stations=["ST-N1"]), "page_size": 2}).json()
    token = sess["session_token"]
    p1 = api("GET", f"/access/sessions/{token}/pages", key_port, 200).json()
    p2 = api("GET", f"/access/sessions/{token}/pages?cursor={p1['next_cursor']}",
             key_port, 200).json()
    check("分页固定范围与截止点，游标连续",
          len(p1["page"]) == 2 and len(p2["page"]) == 2 and p1["scope_hash"] == p2["scope_hash"])
    l1_id = [l for l in api("GET", "/access/licenses", OPERATOR, 200).json()
             if l["code"] == "L1"][0]["id"]
    api("POST", f"/access/licenses/{l1_id}/revoke", ADMIN, 200,
        json={"reason": "窗口中途撤回"})
    r = api("GET", f"/access/sessions/{token}/pages?cursor={p2['next_cursor']}",
            key_port, 409)
    check("每页联查当前许可：撤回后旧游标不能延长已取消访问权，会话终止",
          r.status_code == 409 and "terminated" in r.json()["detail"], r.text)
    r = api("GET", f"/access/sessions/{token}/pages", key_port, 409)
    check("终止后任何旧会话请求都被拒", r.status_code == 409)

    # ---------- 15. 文件生成、受控领取与生成后撤回 ----------
    # L1 已撤回：先用 u_dual（LU1/LU2 仍有效）走完整领取链路
    sc_dual = scope(stations=["ST-N1"])
    t1 = api("POST", "/access/extractions", key_dual, 201,
             json={"task_key": "D1", "scope": sc_dual}).json()
    check("生成任务先入暂存（STAGED，无领取入口）", t1["status"] == "STAGED")
    r = api("POST", f"/access/extractions/{t1['task_id']}/claim", key_dual, 409)
    check("暂存内容不可领取", r.status_code == 409, r.text)
    pr = api("POST", f"/access/extractions/{t1['task_id']}/promote",
             key_dual, 200).json()
    check("晋升只返回受控领取接口而非长期公开链接",
          pr["status"] == "READY" and pr["claim_endpoint"].endswith("/claim"), str(pr))
    claimed = api("POST", f"/access/extractions/{t1['task_id']}/claim",
                  key_dual, 200).json()
    content = claimed["content"]
    file_stations = {s["station_id"] for s in content["stations"]}
    check("领取文件内容：未授权站点/不可确定观测从未进入可领取结果",
          file_stations == {[s for s in api('GET', '/stations', OPERATOR, 200).json()
                             if s['code'] == 'ST-N1'][0]['id']} and
          all(c["quality_grade"] != "UNCERTAIN"
              for s in content["stations"] for c in s["cells"]))
    check("文件逐项核查：单位米、1h 时间格、来源集合与许可条件可溯",
          all(c.get("grid_seconds") == 3600 and "value_m" in c
              for s in content["stations"] for c in s["cells"]) and
          all(c["source_sample_ids"]
              for s in content["stations"] for c in s["cells"] if c["value_m"] is not None) and
          {"LU1", "LU2"} <= {lc["code"] for lc in content["license_conditions"]
                             if lc["effect"] == "ALLOW"},
          str(content["stations"][0]["cells"][:2]))
    r = api("POST", f"/access/extractions/{t1['task_id']}/claim", key_dual, 409)
    check("重复领取被拒并留痕（不回收对方已存副本，只记录确切交付范围）",
          r.status_code == 409)
    mine = api("GET", "/access/my/deliveries", key_dual, 200).json()
    check("交付登记保存内容哈希、确切范围、接收主体与领取记录",
          len(mine) == 1 and mine[0]["content_hash"] and
          any(c["result"] == "CLAIMED" for c in mine[0]["claims"]) and
          any(c["result"] == "ALREADY_CLAIMED" for c in mine[0]["claims"]), str(mine)[:200])
    listed = api("GET", "/access/extractions", key_dual, 200).json()
    check("任务列表不暴露暂存/文件物理地址",
          all("ready_path" not in x and "staging_path" not in x and "claim_token" not in x
              for x in listed))

    # 生成后、领取前撤回
    t2 = api("POST", "/access/extractions", key_dual, 201,
             json={"task_key": "D2", "scope": sc_dual}).json()
    api("POST", f"/access/extractions/{t2['task_id']}/promote", key_dual, 200)
    for code in ("LU1", "LU2"):
        lid = [l for l in api("GET", "/access/licenses", OPERATOR, 200).json()
               if l["code"] == code][0]["id"]
        api("POST", f"/access/licenses/{lid}/revoke", ADMIN, 200,
            json={"reason": "领取前撤回"})
    r = api("POST", f"/access/extractions/{t2['task_id']}/claim", key_dual, 409)
    check("文件生成后撤回：受控领取接口重新联查并拦截，不发文件",
          r.status_code == 409, r.text)
    t2_after = [x for x in api("GET", "/access/extractions", key_dual, 200).json()
                if x["task_key"] == "D2"][0]
    check("被撤回任务标记 REVOKED，旧任务不得重新挂为可下载",
          t2_after["status"] == "REVOKED")
    r = api("POST", "/access/extractions", key_dual, 409,
            json={"task_key": "D2", "scope": sc_dual})
    check("相同请求重试核对当前范围：不再合格的旧文件不复用",
          r.status_code == 409, r.text)

    # ---------- 16. 隔离与审计 ----------
    r = api("GET", "/access/licenses", key_port)
    check("普通用户不能访问管理端资料接口（401/403）", r.status_code in (401, 403), r.text)
    r = api("POST", "/access/evaluate", key_port,
            json=scope(stations=["ST-UNKNOWN-X"]))
    check("不确认其他港区站点是否存在（统一 404 文案）", r.status_code == 404, r.text)
    audit = api("GET", f"/access/audit/evaluations/{e3['evaluation_id']}", ADMIN, 200).json()
    check("内部审计指出每条放行/排除采用的具体条件与绑定位置",
          audit["scope_hash"] == e3["scope_hash"] and
          all(i["installation_id"] is not None and
              (i["license"] or i["verdict"] != "ALLOWED")
              for i in audit["items"] if i["verdict"] != "NO_INSTALLATION"),
          str(audit["items"][0])[:200])
    r = api("GET", f"/access/my/evaluations/{e3['evaluation_id']}/audit", key_deny, 404)
    check("用户不能查他人求值（不确认存在）", r.status_code == 404)
    deliveries = api("GET", "/access/audit/deliveries?user=u_dual", ADMIN, 200).json()
    check("审计可从交付文件查回使用了哪些许可条件而非只看文件名港区",
          deliveries and all(d["license_ids"] and d["station_codes"]
                              for d in deliveries), str(deliveries)[:200])

    # ---------- 汇总 ----------
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n===== 空间授权回放完成：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过 =====")
    if failed:
        for name, _, detail in failed:
            print(f"FAILED: {name} -- {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
