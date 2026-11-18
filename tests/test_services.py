"""服务层集成测试：接入去重/更正、版本快照、迟到新版本、同事务交付。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import services
from app.models import (
    Datum,
    DatumConversion,
    Delivery,
    Product,
    ProductUnit,
    RawObservation,
    Station,
    StationDatumHistory,
    Subscription,
)

W0 = datetime(2026, 5, 31, 22, 0, tzinfo=timezone.utc)
REMEASURE = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def make_world(session, offset_before=0.832, offset_after=0.815):
    """一个目标基准 ND85、一个港区基准 PORT_A（跨重测日两段偏移）、一个站。"""
    nd85 = Datum(code="ND85", name="1985国家高程")
    port = Datum(code="PORT_A", name="港区A零点")
    session.add_all([nd85, port])
    session.flush()
    v1 = services.new_datum_version(session, "bootstrap")
    st = Station(code="ST-A1", name="A1", region="gangqu-a", unit="m", positive="up",
                 nominal_interval_sec=600, datum_id=port.id)
    session.add(st)
    session.flush()
    session.add(StationDatumHistory(station_id=st.id, datum_id=port.id,
                                    created_in_version=v1.id))
    session.add_all([
        DatumConversion(from_datum_id=port.id, to_datum_id=nd85.id, offset_m=offset_before,
                        valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                        valid_to=REMEASURE, created_in_version=v1.id),
        DatumConversion(from_datum_id=port.id, to_datum_id=nd85.id, offset_m=offset_after,
                        valid_from=REMEASURE, valid_to=None, created_in_version=v1.id),
    ])
    session.commit()
    return nd85, port, st


class Spec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def job_spec(key="j1"):
    return Spec(job_key=key, region="gangqu-a",
                window_start=W0, window_end=W0 + timedelta(hours=4),
                grid_seconds=600, target_datum="ND85")


def feed(session, station_code, base_value=1.0, start=W0, count=25, step_min=10):
    items = []
    for i in range(count):
        items.append({
            "station": station_code,
            "sample_seq": i + 1,
            "observed_at": start + timedelta(minutes=step_min * i),
            "value": base_value,
            "unit": "m",
            "quality_flag": "GOOD",
        })
    return services.ingest_batch(session, items)


def test_ingest_dedupe_and_correction(session):
    make_world(session)
    r1 = feed(session, "ST-A1")
    assert r1["inserted"] == 25 and r1["duplicated"] == 0

    # 同一批重复报文：全部去重，不进入二次计算
    r2 = feed(session, "ST-A1")
    assert r2["inserted"] == 0 and r2["duplicated"] == 25

    # 质量标记更正：同序号不同内容 -> 新观测版本
    r3 = services.ingest_batch(session, [{
        "station": "ST-A1", "sample_seq": 5, "observed_at": W0 + timedelta(minutes=40),
        "value": 1.0, "unit": "m", "quality_flag": "SUSPECT",
    }])
    assert r3["corrected"] == 1
    obs = session.execute(
        select(RawObservation).where(RawObservation.sample_seq == 5)
    ).scalars().all()
    assert len(obs) == 2
    current = [o for o in obs if o.is_current][0]
    assert current.quality_flag == "SUSPECT" and current.version_no == 2


def test_window_crossing_remeasure_uses_per_sample_offsets(session):
    """跨重测日的窗口：先按适用区间逐样本换算，再形成共同时间格。"""
    make_world(session)
    feed(session, "ST-A1", base_value=1.0)
    job = services.create_job(session, job_spec(), "PRELIMINARY")
    product = services.run_job(session, job.id)
    units = session.execute(
        select(ProductUnit).where(ProductUnit.product_id == product.id)
        .order_by(ProductUnit.grid_start)
    ).scalars().all()
    assert len(units) == 24
    cell = timedelta(seconds=600)
    fully_before = [u for u in units if u.grid_start + cell < REMEASURE]
    fully_after = [u for u in units if u.grid_start >= REMEASURE]
    boundary = [u for u in units
                if u not in fully_before and u not in fully_after]
    # 完全位于重测日前/后的格元分别使用各自适用区间的偏移
    assert all(abs(u.value_m - (1.0 + 0.832)) < 1e-6 for u in fully_before)
    assert all(abs(u.value_m - (1.0 + 0.815)) < 1e-6 for u in fully_after)
    # 边界格元 [23:50,00:00) 包含 00:00 新偏移样本的代表时段，
    # 是“先逐样本换算、再上格”的加权混合，而非先平均再套单一偏移
    assert len(boundary) == 1
    assert 1.0 + 0.815 < boundary[0].value_m < 1.0 + 0.832


def test_job_pins_datum_version_at_creation(session):
    """作业创建后基准配置变更，运行仍使用启动时快照。"""
    nd85, port, st = make_world(session)
    feed(session, "ST-A1", base_value=1.0)
    job = services.create_job(session, job_spec(), "PRELIMINARY")
    pinned = job.datum_version_id

    # 发布中途配置变更：退役旧边，新增偏移 +9.9 的边（新基准版本）
    v2 = services.new_datum_version(session, "config change mid-publish")
    old_edges = session.execute(
        select(DatumConversion).where(DatumConversion.retired_in_version.is_(None))
    ).scalars().all()
    for e in old_edges:
        e.retired_in_version = v2.id
    session.add(DatumConversion(
        from_datum_id=port.id, to_datum_id=nd85.id, offset_m=9.9,
        valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc), valid_to=None,
        created_in_version=v2.id))
    session.commit()

    product = services.run_job(session, job.id)
    assert product.datum_version_id == pinned
    units = session.execute(
        select(ProductUnit).where(ProductUnit.product_id == product.id)
    ).scalars().all()
    # 全部站点按旧基准计算，没有一半旧一半新：值都落在旧配置区间内
    assert all((1.0 + 0.815) - 1e-6 <= u.value_m <= (1.0 + 0.832) + 1e-6 for u in units)
    assert not any(abs(u.value_m - (1.0 + 9.9)) < 1e-3 for u in units)

    # 下一个作业才使用新配置
    job2 = services.create_job(session, job_spec("j2"), "PRELIMINARY")
    product2 = services.run_job(session, job2.id)
    units2 = session.execute(
        select(ProductUnit).where(ProductUnit.product_id == product2.id)
    ).scalars().all()
    assert all(abs(u.value_m - (1.0 + 9.9)) < 1e-6 for u in units2)


def test_late_data_new_version_and_superseded_deliveries(session):
    make_world(session)
    feed(session, "ST-A1", base_value=1.0)
    session.add(Subscription(name="duty", region="gangqu-a",
                             stages=["PRELIMINARY", "CONFIRMED"],
                             endpoint_url="http://consumer:8001/receive/duty"))
    session.commit()

    p1 = services.run_job(session, services.create_job(session, job_spec(), "PRELIMINARY").id)
    d1 = session.execute(select(Delivery).where(Delivery.product_id == p1.id)).scalars().all()
    assert len(d1) == 1  # 产品与待交付任务同事务可见

    # 迟到补传：窗口已有产品 -> is_late
    r = services.ingest_batch(session, [{
        "station": "ST-A1", "sample_seq": 100,
        "observed_at": W0 + timedelta(minutes=5),
        "value": 1.5, "unit": "m", "quality_flag": "GOOD",
    }])
    assert r["late"], "到达时窗口已有产品，应标记迟到"

    p2 = services.run_job(session, services.create_job(session, job_spec("j-late"), "PRELIMINARY").id)
    assert p2.version_no == 2 and p2.supersedes_id == p1.id
    assert p2.publish_seq > p1.publish_seq

    sup = services.superseded_deliveries(session, p2.id)
    assert sup["superseded"][0]["product_id"] == p1.id
    assert sup["superseded"][0]["deliveries"][0]["delivery_key"] == d1[0].delivery_key


def test_confirm_creates_confirmed_version(session):
    make_world(session)
    feed(session, "ST-A1")
    session.add(Subscription(name="model", region="gangqu-a", stages=["CONFIRMED"],
                             endpoint_url="http://consumer:8001/receive/model"))
    session.commit()
    p1 = services.run_job(session, services.create_job(session, job_spec(), "PRELIMINARY").id)
    # 初报阶段不给 CONFIRMED 订阅者建交付
    assert session.execute(select(Delivery)).scalars().all() == []

    pc = services.confirm_product(session, p1.id, "confirm-1")
    assert pc.stage == "CONFIRMED" and pc.version_no == 2
    deliveries = session.execute(
        select(Delivery).where(Delivery.product_id == pc.id)).scalars().all()
    assert len(deliveries) == 1 and deliveries[0].stage == "CONFIRMED"


def test_run_job_is_idempotent(session):
    make_world(session)
    feed(session, "ST-A1")
    job = services.create_job(session, job_spec(), "PRELIMINARY")
    p1 = services.run_job(session, job.id)
    p2 = services.run_job(session, job.id)
    assert p1.id == p2.id
    assert session.execute(select(Product)).scalars().all().__len__() == 1
