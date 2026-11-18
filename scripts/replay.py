"""验收回放脚本：潮位样例 + 模拟消费端全链路核对。

场景覆盖：
  1) 多基准站点（PORT_A/BAY_B/LOCAL_SG/ISOLATED）与不同采样频率、单位、正方向；
  2) 重复报文去重（同站同序号同内容只进入一次产品计算）；
  3) 跨重测日窗口：先按适用区间逐样本换算，再形成共同时间格；
  4) 发布中途基准配置变更：当前作业固定使用启动时的基准版本；
  5) 转换路径冲突保留、无路径站点不用邻站偏移补齐、循环路径不重复加偏移；
  6) 长断档缺测、短断档插值、突跳待核段不插成平滑曲线；
  7) 迟到补传与质量标记更正 -> 新产品版本，列出引用旧版本的订阅交付；
  8) 分阶段发布：初报供值守研判，确认产品才进入正式模型（初报不能伪装成确认）；
  9) 回执丢失重送去重、消费者离线恢复、慢重试不把接收方恢复成旧值；
 10) 订阅者分别确认、失败者独立重试，未送达者不记为成功。

前置：API (默认 127.0.0.1:8000) 与模拟消费端 (默认 127.0.0.1:8001) 已启动。
运行：python scripts/replay.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

API = os.getenv("API_URL", "http://127.0.0.1:8000")
CONSUMER = os.getenv("CONSUMER_URL", "http://127.0.0.1:8001")
ADMIN = os.getenv("ADMIN_KEY", "admin-key")
OPERATOR = os.getenv("OPERATOR_KEY", "operator-key")
CONSUMER_KEY = os.getenv("CONSUMER_KEY", "consumer-key")

W0 = datetime(2026, 5, 31, 22, 0, tzinfo=timezone.utc)
W1 = W0 + timedelta(hours=4)
REMEASURE = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    suffix = "" if cond else f"  <-- {detail}"
    print(f"[{mark}] {name}{suffix}", flush=True)


def api(method: str, path: str, key: str = OPERATOR, expect: int | None = None, **kw):
    r = httpx.request(method, f"{API}{path}", headers={"X-API-Key": key}, timeout=30, **kw)
    if expect is not None and r.status_code != expect:
        raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
    return r


def cons(method: str, path: str, key: str = OPERATOR, **kw):
    r = httpx.request(method, f"{CONSUMER}{path}", headers={"X-API-Key": key}, timeout=30, **kw)
    r.raise_for_status()
    return r


def iso(dt: datetime) -> str:
    return dt.isoformat()


def obs(station: str, seq: int, t: datetime, value: float, unit: str = "m", flag: str = "GOOD"):
    return {"station": station, "sample_seq": seq, "observed_at": iso(t),
            "value": value, "unit": unit, "quality_flag": flag}


def units_of(product_id: int, station: str) -> dict[str, dict]:
    rows = api("GET", f"/products/{product_id}/units?station={station}", CONSUMER_KEY, 200).json()
    return {u["grid_start"]: u for u in rows}


def consumer_cell(series_key: str, station_id: int, grid_start: datetime) -> dict | None:
    state = cons("GET", "/state", CONSUMER_KEY, params={"series_key": series_key}).json()
    for row in state:
        if row["station_id"] == station_id and row["grid_start"] == iso(grid_start):
            return row
    return None


def main() -> int:
    # ---------- 0. 复位 ----------
    api("POST", "/admin/reset", ADMIN, 200)
    cons("POST", "/reset")

    # ---------- 1. 基准与站点登记 ----------
    # 基准样例：读数单位/正方向在站点上登记；站点零点->目标高程的转换带生效区间。
    for d in [{"code": "ND85", "name": "1985国家高程基准", "description": "统一目标高程"},
              {"code": "PORT_A", "name": "港区A验潮零点"},
              {"code": "BAY_B", "name": "海湾B验潮零点"},
              {"code": "LOCAL_SG", "name": "沈家湾当地零点"},
              {"code": "ISOLATED", "name": "孤立基准（无转换路径）"}]:
        api("POST", "/datums", ADMIN, 201, json=d)

    # 转换边：height(to) = height(from) + offset_m，单位米，水位向上为正
    conversions = [
        # 港区A零点左右跨重测日的两段转换（2026-06-01 00:00Z 重测）
        {"from_datum": "PORT_A", "to_datum": "ND85", "offset_m": 0.832,
         "valid_from": "2025-01-01T00:00:00Z", "valid_to": iso(REMEASURE), "note": "重测前"},
        {"from_datum": "PORT_A", "to_datum": "ND85", "offset_m": 0.815,
         "valid_from": iso(REMEASURE), "valid_to": None, "note": "重测后"},
        {"from_datum": "BAY_B", "to_datum": "ND85", "offset_m": 1.204,
         "valid_from": "2025-01-01T00:00:00Z", "valid_to": None},
        # 多段路径：LOCAL_SG -> PORT_A -> ND85
        {"from_datum": "LOCAL_SG", "to_datum": "PORT_A", "offset_m": -0.310,
         "valid_from": "2025-01-01T00:00:00Z", "valid_to": None},
        # 直达边与多段路径不相容（0.520 vs -0.310+0.832=0.522）-> 保留冲突
        {"from_datum": "LOCAL_SG", "to_datum": "ND85", "offset_m": 0.520,
         "valid_from": "2025-01-01T00:00:00Z", "valid_to": None},
        # 反向边构成环 PORT_A<->ND85：简单路径约束下不得重复加偏移
        {"from_datum": "ND85", "to_datum": "PORT_A", "offset_m": -0.832,
         "valid_from": "2025-01-01T00:00:00Z", "valid_to": iso(REMEASURE)},
    ]
    for c in conversions:
        api("POST", "/datum-conversions", ADMIN, 201, json=c)

    stations = [
        # ST-A1：港区A，米，水位向上为正，10 分钟规则采样
        {"code": "ST-A1", "name": "港区A-1", "region": "gangqu-a", "unit": "m",
         "positive": "up", "nominal_interval_sec": 600, "datum": "PORT_A"},
        # ST-A2：港区A，厘米，不规则采样
        {"code": "ST-A2", "name": "港区A-2", "region": "gangqu-a", "unit": "cm",
         "positive": "up", "nominal_interval_sec": 900, "datum": "PORT_A"},
        # ST-C1：沈家湾当地零点 -> 存在两条互不相容的换算路径
        {"code": "ST-C1", "name": "沈家湾-1", "region": "gangqu-a", "unit": "m",
         "positive": "up", "nominal_interval_sec": 1800, "datum": "LOCAL_SG"},
        # ST-NP：孤立基准，无目标基准路径（不得用邻站偏移补齐）
        {"code": "ST-NP", "name": "孤立站", "region": "gangqu-a", "unit": "m",
         "positive": "up", "nominal_interval_sec": 600, "datum": "ISOLATED"},
        # ST-B1：海湾B，含断档/待核段/疑似样本
        {"code": "ST-B1", "name": "海湾B-1", "region": "haiwan-b", "unit": "m",
         "positive": "up", "nominal_interval_sec": 600, "datum": "BAY_B"},
        # ST-D1：海湾B，毫米、读数向下为正（距水面距离）
        {"code": "ST-D1", "name": "海湾B-2", "region": "haiwan-b", "unit": "mm",
         "positive": "down", "nominal_interval_sec": 600, "datum": "BAY_B"},
    ]
    for st in stations:
        api("POST", "/stations", ADMIN, 201, json=st)
    station_ids = {s["code"]: s["id"] for s in api("GET", "/stations", CONSUMER_KEY, 200).json()}

    # ---------- 2. 观测接入（含重复报文） ----------
    batch: list[dict] = []
    # ST-A1：10min 规则，1.000 m
    for i in range(25):
        batch.append(obs("ST-A1", i + 1, W0 + timedelta(minutes=10 * i), 1.0))
    # ST-A2：不规则采样，100.0 cm
    a2_times = [0, 7, 21, 36, 55, 80, 91, 119, 132, 160, 185, 210, 235]
    for i, m in enumerate(a2_times):
        batch.append(obs("ST-A2", i + 1, W0 + timedelta(minutes=m), 100.0, "cm"))
    # ST-C1：每小时一个样本
    for i, m in enumerate([0, 60, 120, 180]):
        batch.append(obs("ST-C1", i + 1, W0 + timedelta(minutes=m), 2.0))
    # ST-NP：孤立基准站
    for i, m in enumerate([0, 10, 20]):
        batch.append(obs("ST-NP", i + 1, W0 + timedelta(minutes=m), 0.8))
    # ST-B1：22:00-23:00 正常（22:30 SUSPECT）；23:10-23:50 长断档；
    #        00:20/00:30 突跳待核；01:10/01:20 短断档
    b1 = []
    seq = 1
    for m in [0, 10, 20, 30, 40, 50, 60]:
        flag = "SUSPECT" if m == 30 else "GOOD"
        b1.append(obs("ST-B1", seq, W0 + timedelta(minutes=m), 2.0, "m", flag)); seq += 1
    b1.append(obs("ST-B1", seq, W0 + timedelta(minutes=120), 2.0)); seq += 1
    for m in [140, 150]:
        b1.append(obs("ST-B1", seq, W0 + timedelta(minutes=m), 9.9, "m", "PENDING_JUMP")); seq += 1
    for m in [160, 170, 180, 210, 220, 230]:
        b1.append(obs("ST-B1", seq, W0 + timedelta(minutes=m), 2.0)); seq += 1
    batch.extend(b1)
    # ST-D1：毫米、向下为正
    for i in range(25):
        batch.append(obs("ST-D1", i + 1, W0 + timedelta(minutes=10 * i), 500.0, "mm"))

    r = api("POST", "/observations:ingest", OPERATOR, 200, json={"items": batch}).json()
    check("观测接入：全部样本入库", r["inserted"] == len(batch), str(r))

    # 同一批重复报文：全部去重，只进入一次产品计算
    r2 = api("POST", "/observations:ingest", OPERATOR, 200, json={"items": batch}).json()
    check("重复报文去重：同站同序号同内容不重复入库",
          r2["duplicated"] == len(batch) and r2["inserted"] == 0, str(r2))

    # ---------- 3. 订阅登记 ----------
    subs = [
        {"name": "duty", "region": "gangqu-a", "stages": ["PRELIMINARY", "CONFIRMED"],
         "endpoint_url": f"{CONSUMER}/receive/duty"},
        {"name": "model", "region": "gangqu-a", "stages": ["CONFIRMED"],
         "endpoint_url": f"{CONSUMER}/receive/model"},
        {"name": "baywatch", "region": "haiwan-b", "stages": ["PRELIMINARY", "CONFIRMED"],
         "endpoint_url": f"{CONSUMER}/receive/baywatch"},
        # 永远不可达的订阅者：验证失败者独立重试、不被记为成功
        {"name": "duty-b", "region": "gangqu-a", "stages": ["PRELIMINARY", "CONFIRMED"],
         "endpoint_url": "http://127.0.0.1:9999/receive/duty-b"},
    ]
    for s in subs:
        api("POST", "/subscriptions", ADMIN, 201, json=s)
    sub_ids = {s["name"]: s["id"]
               for s in api("GET", "/subscriptions", CONSUMER_KEY, 200).json()}

    # ---------- 4. 初报作业 J1（创建后发生基准配置变更） ----------
    j1 = api("POST", "/jobs", OPERATOR, 201, json={
        "job_key": "J1-gangqu-a", "region": "gangqu-a",
        "window_start": iso(W0), "window_end": iso(W1),
        "grid_seconds": 600, "target_datum": "ND85"}).json()
    pinned_version = j1["datum_version_id"]

    # 发布中途配置变更：ST-A2 零点基准 PORT_A -> BAY_B（产生新基准版本）
    api("PUT", f"/stations/{station_ids['ST-A2']}/datum", ADMIN, 200,
        json={"datum": "BAY_B", "note": "中途改测"})

    p1 = api("POST", f"/jobs/{j1['id']}/run", OPERATOR, 201).json()
    check("作业固定启动时基准版本", p1["datum_version_id"] == pinned_version,
          f"pinned={pinned_version} used={p1['datum_version_id']}")

    u = units_of(p1["id"], "ST-A1")
    check("跨重测日：重测前格元按旧偏移换算",
          abs(u[iso(W0)]["value_m"] - 1.832) < 1e-6, str(u[iso(W0)]))
    check("跨重测日：重测后格元按新偏移换算",
          abs(u[iso(REMEASURE)]["value_m"] - 1.815) < 1e-6, str(u[iso(REMEASURE)]))
    boundary = u[iso(REMEASURE - timedelta(minutes=10))]
    check("跨重测日：边界格元为先换算后上格的加权混合",
          1.815 < boundary["value_m"] < 1.832, str(boundary))
    check("产品单元可溯源：原始样本与换算路径",
          u[iso(W0)]["source_sample_ids"] and
          abs(u[iso(W0)]["conversion_path"]["offset_m"] - 0.832) < 1e-9,
          str(u[iso(W0)]))

    u2 = units_of(p1["id"], "ST-A2")
    check("中途配置变更：当前作业仍按启动时基准（PORT_A）计算 ST-A2",
          abs(u2[iso(W0)]["value_m"] - 1.832) < 1e-6, str(u2[iso(W0)]))
    check("不规则采样：按代表时段加权（ST-A2 边界格）",
          abs(u2[iso(REMEASURE)]["value_m"] - 1.82435) < 1e-4, str(u2[iso(REMEASURE)]))

    uc = units_of(p1["id"], "ST-C1")
    cand_totals = sorted(c["total_offset"] for c in uc[iso(W0)]["conversion_path"]["candidates"])
    check("路径冲突保留：ST-C1 标记 CONFLICT 且不产出水位",
          uc[iso(W0)]["quality_grade"] == "CONFLICT" and uc[iso(W0)]["value_m"] is None,
          str(uc[iso(W0)]))
    check("路径冲突保留：两条互不相容路径都列出（含循环图简单路径约束）",
          cand_totals == sorted([0.520, -0.310 + 0.832]), str(cand_totals))

    un = units_of(p1["id"], "ST-NP")
    check("无路径站点：NO_PATH 且不用邻站偏移补齐",
          all(x["quality_grade"] == "NO_PATH" and x["value_m"] is None for x in un.values()),
          str(list(un.values())[0]))

    # 下一个作业才使用新配置
    j2 = api("POST", "/jobs", OPERATOR, 201, json={
        "job_key": "J2-gangqu-a", "region": "gangqu-a",
        "window_start": iso(W0), "window_end": iso(W1),
        "grid_seconds": 600, "target_datum": "ND85"}).json()
    p2 = api("POST", f"/jobs/{j2['id']}/run", OPERATOR, 201).json()
    u2new = units_of(p2["id"], "ST-A2")
    check("新作业使用变更后基准（ST-A2 -> BAY_B）",
          abs(u2new[iso(W0)]["value_m"] - (1.0 + 1.204)) < 1e-6, str(u2new[iso(W0)]))
    check("产品版本递增且取代旧版本", p2["version_no"] == 2 and p2["supersedes_id"] == p1["id"],
          f"v={p2['version_no']} sup={p2['supersedes_id']}")

    # ---------- 5. 海湾B：断档/插值/待核/单位与正方向 ----------
    h1 = api("POST", "/jobs", OPERATOR, 201, json={
        "job_key": "H1-haiwan-b", "region": "haiwan-b",
        "window_start": iso(W0), "window_end": iso(W1),
        "grid_seconds": 600, "target_datum": "ND85"}).json()
    hp1 = api("POST", f"/jobs/{h1['id']}/run", OPERATOR, 201).json()
    ub = units_of(hp1["id"], "ST-B1")
    g = lambda m: ub[iso(W0 + timedelta(minutes=m))]["quality_grade"]
    check("实测格元换算值正确（2.0 + 1.204）",
          abs(ub[iso(W0)]["value_m"] - 3.204) < 1e-6, str(ub[iso(W0)]))
    check("疑似样本所在格元标记 PENDING", g(20) == "PENDING" and g(30) == "PENDING",
          f"{g(20)},{g(30)}")
    check("长断档标记缺测且不插值",
          all(g(m) == "MISSING" and ub[iso(W0 + timedelta(minutes=m))]["value_m"] is None
              for m in [70, 80, 90, 100]))
    check("断档边缘格元由相邻样本代表时段覆盖（23:50 MEASURED）",
          g(110) == "MEASURED", g(110))
    check("突跳待核段不插成平滑实测曲线",
          g(130) == "PENDING" and g(140) == "PENDING" and g(150) == "PENDING",
          f"{g(130)},{g(140)},{g(150)}")
    check("短断档允许插值", g(190) == "INTERPOLATED" and
          abs(ub[iso(W0 + timedelta(minutes=190))]["value_m"] - 3.204) < 1e-6,
          str(ub[iso(W0 + timedelta(minutes=190))]))
    ud = units_of(hp1["id"], "ST-D1")
    check("单位与正方向换算（500mm 向下为正 -> -0.5 + 1.204）",
          abs(ud[iso(W0)]["value_m"] - 0.704) < 1e-6, str(ud[iso(W0)]))

    # ---------- 6. 交付：分别确认、失败独立重试 ----------
    deliveries = api("GET", "/deliveries", CONSUMER_KEY, 200).json()
    check("产品与待交付任务同事务可见（PENDING）",
          any(d["product_id"] == p1["id"] and d["status"] == "PENDING" for d in deliveries),
          str(deliveries))
    check("初报不给 CONFIRMED 订阅者建交付",
          not any(d["product_id"] == p1["id"] and d["subscription_id"] == sub_ids["model"] for d in deliveries))

    api("POST", "/worker/run-once", OPERATOR, 200)
    deliveries = api("GET", "/deliveries", CONSUMER_KEY, 200).json()
    duty_p1 = [d for d in deliveries if d["product_id"] == p1["id"] and d["subscription_id"] == sub_ids["duty"]]
    dutyb_p1 = [d for d in deliveries if d["product_id"] == p1["id"] and d["subscription_id"] == sub_ids["duty-b"]]
    check("订阅者分别确认：可达订阅者 ACKED",
          duty_p1 and duty_p1[0]["status"] == "ACKED", str(duty_p1))
    check("失败者独立重试：不可达订阅者 FAILED 且不影响他人",
          dutyb_p1 and dutyb_p1[0]["status"] == "FAILED" and dutyb_p1[0]["attempts"] >= 1,
          str(dutyb_p1))

    series_a = p1["series_key"]
    cell = consumer_cell(series_a, station_ids["ST-A1"], W0)
    check("消费端收到初报（ST-A1 首格 1.832）",
          cell and abs(cell["value_m"] - 1.832) < 1e-6, str(cell))

    # ---------- 7. 迟到补传 -> 新版本；列出引用旧版本的交付 ----------
    late = api("POST", "/observations:ingest", OPERATOR, 200, json={"items": [
        obs("ST-A1", 100, W0 + timedelta(minutes=5), 1.5)]}).json()
    check("迟到补传被标记", late["late"] != [], str(late))
    j3 = api("POST", "/jobs", OPERATOR, 201, json={
        "job_key": "J3-gangqu-a-late", "region": "gangqu-a",
        "window_start": iso(W0), "window_end": iso(W1),
        "grid_seconds": 600, "target_datum": "ND85"}).json()
    p3 = api("POST", f"/jobs/{j3['id']}/run", OPERATOR, 201).json()
    check("迟到数据生成新产品版本", p3["version_no"] == 3 and p3["supersedes_id"] == p2["id"])
    sup = api("GET", f"/products/{p3['id']}/superseded-deliveries", CONSUMER_KEY, 200).json()
    check("列出引用旧版本的订阅交付",
          len(sup["superseded"]) == 2 and
          all(len(x["deliveries"]) >= 1 for x in sup["superseded"]), str(sup)[:300])
    u3 = units_of(p3["id"], "ST-A1")
    check("迟到样本参与重算（首格 2.5*1.832+5*2.332+2.5*1.832)/10=2.082",
          abs(u3[iso(W0)]["value_m"] - 2.082) < 1e-3, str(u3[iso(W0)]))

    # ---------- 8. 质量标记更正 -> 新版本；回执丢失重送去重 ----------
    cons("PUT", "/flags/drop_ack", json={"value": True})
    corr = api("POST", "/observations:ingest", OPERATOR, 200, json={"items": [
        obs("ST-B1", 4, W0 + timedelta(minutes=30), 2.0, "m", "GOOD")]}).json()
    check("质量标记更正生成新观测版本", corr["corrected"] == 1, str(corr))
    h2 = api("POST", "/jobs", OPERATOR, 201, json={
        "job_key": "H2-haiwan-b-corr", "region": "haiwan-b",
        "window_start": iso(W0), "window_end": iso(W1),
        "grid_seconds": 600, "target_datum": "ND85"}).json()
    hp2 = api("POST", f"/jobs/{h2['id']}/run", OPERATOR, 201).json()
    ub2 = units_of(hp2["id"], "ST-B1")
    check("更正后疑似格元恢复实测",
          ub2[iso(W0 + timedelta(minutes=30))]["quality_grade"] == "MEASURED",
          str(ub2[iso(W0 + timedelta(minutes=30))]))

    api("POST", "/worker/run-once", OPERATOR, 200)  # 回执丢失：消费端已应用但交付记 FAILED
    d = api("GET", f"/deliveries?subscription_id={sub_ids['baywatch']}", CONSUMER_KEY, 200).json()
    bay_h2 = [x for x in d if x["product_id"] == hp2["id"]][0]
    check("确认包丢失：交付记为 FAILED 而非成功", bay_h2["status"] == "FAILED", str(bay_h2))
    cell = consumer_cell(hp1["series_key"], station_ids["ST-B1"], W0 + timedelta(minutes=30))
    check("确认包丢失：消费端实际已应用更正（MEASURED）",
          cell and cell["quality_grade"] == "MEASURED", str(cell))

    cons("PUT", "/flags/drop_ack", json={"value": False})
    api("POST", "/worker/run-once", OPERATOR, 200)  # 重送：同一交付标识，消费端去重后再确认
    d = api("GET", f"/deliveries?subscription_id={sub_ids['baywatch']}", CONSUMER_KEY, 200).json()
    bay_h2 = [x for x in d if x["product_id"] == hp2["id"]][0]
    inbox = cons("GET", "/inbox", CONSUMER_KEY).json()
    inb = [x for x in inbox if x["delivery_key"] == bay_h2["delivery_key"]][0]
    check("重送保留同一交付标识并按标识去重",
          bay_h2["status"] == "ACKED" and inb["receive_count"] >= 2, str(inb))

    # ---------- 9. 消费者离线 -> 恢复后补投 ----------
    # 先置离线再产出产品，避免与后台 worker 竞争造成不确定结果
    cons("PUT", "/flags/offline", json={"value": True})
    late2 = api("POST", "/observations:ingest", OPERATOR, 200, json={"items": [
        obs("ST-A1", 101, W0 + timedelta(minutes=15), 1.6)]}).json()
    j4 = api("POST", "/jobs", OPERATOR, 201, json={
        "job_key": "J4-gangqu-a-late2", "region": "gangqu-a",
        "window_start": iso(W0), "window_end": iso(W1),
        "grid_seconds": 600, "target_datum": "ND85"}).json()
    p4 = api("POST", f"/jobs/{j4['id']}/run", OPERATOR, 201).json()
    api("POST", "/worker/run-once", OPERATOR, 200)
    d = api("GET", f"/deliveries?subscription_id={sub_ids['duty']}", CONSUMER_KEY, 200).json()
    duty_p4 = [x for x in d if x["product_id"] == p4["id"]][0]
    check("消费者离线：交付 FAILED 等待重试", duty_p4["status"] == "FAILED", str(duty_p4))
    cons("PUT", "/flags/offline", json={"value": False})
    api("POST", "/worker/run-once", OPERATOR, 200)
    d = api("GET", f"/deliveries?subscription_id={sub_ids['duty']}", CONSUMER_KEY, 200).json()
    duty_p4 = [x for x in d if x["product_id"] == p4["id"]][0]
    cell = consumer_cell(series_a, station_ids["ST-A1"], W0 + timedelta(minutes=10))
    check("消费者恢复后补投成功且按序应用（第二格 2.132）",
          duty_p4["status"] == "ACKED" and cell and abs(cell["value_m"] - 2.132) < 1e-3,
          f"{duty_p4['status']} {cell}")

    # ---------- 10. 确认产品签发 ----------
    r = api("POST", "/jobs", OPERATOR, json={
        "job_key": "Jx-bad", "region": "gangqu-a", "window_start": iso(W0),
        "window_end": iso(W1), "grid_seconds": 600, "target_datum": "ND85",
        "stage": "CONFIRMED"})
    check("初报不能伪装成确认：作业接口拒绝直接生成 CONFIRMED", r.status_code == 422,
          str(r.status_code))

    pc = api("POST", f"/products/{p1['id']}/confirm", OPERATOR, 201,
             json={"job_key": "C1-gangqu-a"}).json()
    check("确认产品签发为新版本且阶段正确",
          pc["stage"] == "CONFIRMED" and pc["version_no"] == 5, str(pc))
    api("POST", "/worker/run-once", OPERATOR, 200)
    d = api("GET", f"/deliveries?subscription_id={sub_ids['model']}", CONSUMER_KEY, 200).json()
    model_c = [x for x in d if x["product_id"] == pc["id"]]
    check("确认产品进入正式模型订阅",
          model_c and model_c[0]["status"] == "ACKED" and model_c[0]["stage"] == "CONFIRMED",
          str(model_c))
    d = api("GET", f"/deliveries?subscription_id={sub_ids['duty-b']}", CONSUMER_KEY, 200).json()
    check("未送达消费者不记为成功（duty-b 始终 FAILED）",
          all(x["status"] == "FAILED" for x in d), str(d)[:200])
    subs_state = api("GET", "/subscriptions", CONSUMER_KEY, 200).json()
    duty_sub = [s for s in subs_state if s["name"] == "duty"][0]
    check("订阅游标固定在已确认的最大发布序号",
          duty_sub["cursor_seq"] == pc["publish_seq"],
          f"cursor={duty_sub['cursor_seq']} seq={pc['publish_seq']}")

    # ---------- 11. 慢重试不恢复旧值 ----------
    stale_units = api("GET", f"/products/{p1['id']}/units", CONSUMER_KEY, 200).json()
    stale_payload = {
        "delivery_key": "manual-stale-replay",
        "subscription": "duty",
        "product": {"id": p1["id"], "series_key": series_a, "region": "gangqu-a",
                    "stage": "PRELIMINARY", "version_no": 1, "publish_seq": p1["publish_seq"],
                    "window_start": iso(W0), "window_end": iso(W1), "grid_seconds": 600,
                    "target_datum_id": p1["target_datum_id"],
                    "datum_version_id": p1["datum_version_id"],
                    "payload_hash": p1["payload_hash"], "supersedes_id": None},
        "units": stale_units,
    }
    r = cons("POST", "/receive/duty", CONSUMER_KEY, json=stale_payload).json()
    cell = consumer_cell(series_a, station_ids["ST-A1"], W0)
    check("慢重试旧版本被按 publish_seq 拒绝，接收方保持新值",
          r["skipped_stale_cells"] == len(stale_units) and abs(cell["value_m"] - 2.082) < 1e-3,
          f"{r} {cell}")
    r = cons("POST", "/receive/duty", CONSUMER_KEY, json=stale_payload).json()
    check("同一交付标识重复投递去重", r.get("deduplicated") is True, str(r))

    # ---------- 汇总 ----------
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n===== 回放完成：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过 =====")
    if failed:
        for name, _, detail in failed:
            print(f"FAILED: {name} -- {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
