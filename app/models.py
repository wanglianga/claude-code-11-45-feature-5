"""数据模型：站点目录、基准与版本化转换、原始观测、产品、订阅与交付。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- 基准目录 ----------------

class Datum(Base):
    """垂直基准（如 1985 国家高程、某港区当地零点）。"""

    __tablename__ = "datums"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)


class DatumVersion(Base):
    """基准配置版本。任何转换边/站点基准变更都生成新版本，作业按版本快照取数。"""

    __tablename__ = "datum_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatumConversion(Base):
    """基准转换有向边：height(to) = height(from) + offset_m，带生效区间与版本。

    读数单位固定为米；正方向统一为“水位向上为正”。边只增不改：
    变更 = 新版本下新增边并将旧边 retired_in_version 置为该版本。
    """

    __tablename__ = "datum_conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"), index=True)
    to_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"), index=True)
    offset_m: Mapped[float] = mapped_column(Float)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_in_version: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    retired_in_version: Mapped[int | None] = mapped_column(nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")


# ---------------- 站点目录 ----------------

class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(100), index=True)  # 港区/海湾
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8), default="m")     # m / cm / mm
    positive: Mapped[str] = mapped_column(String(8), default="up")  # up=水位向上为正 down=距水面距离向下为正
    nominal_interval_sec: Mapped[int] = mapped_column(Integer, default=600)
    device_status: Mapped[str] = mapped_column(String(32), default="ONLINE")
    datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))  # 当前站点零点基准
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StationDatumHistory(Base):
    """站点零点基准指派历史（版本化，供作业按快照取）。"""

    __tablename__ = "station_datum_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))
    created_in_version: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    retired_in_version: Mapped[int | None] = mapped_column(nullable=True)


# ---------------- 原始观测 ----------------

class RawObservation(Base):
    """原始观测版本链。同一 (station, sample_seq) 仅一条 is_current；
    内容相同的重复报文直接去重，内容不同视为更正并生成新版本。"""

    __tablename__ = "raw_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    sample_seq: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8), default="m")
    quality_flag: Mapped[str] = mapped_column(String(16), default="GOOD")  # GOOD/SUSPECT/PENDING_JUMP/BAD
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[int | None] = mapped_column(nullable=True)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)  # 到达时对应窗口已有产品
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index(
            "uq_current_obs",
            "station_id",
            "sample_seq",
            unique=True,
            postgresql_where=(is_current.is_(True)),
        ),
    )


# ---------------- 产品 ----------------

class ProductJob(Base):
    """产品生成作业。创建时固定 datum_version_id，运行全程使用该快照。"""

    __tablename__ = "product_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_key: Mapped[str] = mapped_column(String(200), unique=True)  # 幂等键
    region: Mapped[str] = mapped_column(String(100))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_seconds: Mapped[int] = mapped_column(Integer)
    target_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))
    stage: Mapped[str] = mapped_column(String(16))  # PRELIMINARY / CONFIRMED
    datum_version_id: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    source_product_id: Mapped[int | None] = mapped_column(nullable=True)  # 确认产品来源初报
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING/RUNNING/COMMITTED/FAILED
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# 全局产品发布序号（订阅游标固定在该序号上）
publish_seq = Sequence("publish_seq", start=1, metadata=Base.metadata)


class Product(Base):
    """潮位产品。系列 = 区域+窗口+粒度+目标基准；每次生成为系列内一个新版本。"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("product_jobs.id"))
    series_key: Mapped[str] = mapped_column(String(300), index=True)
    region: Mapped[str] = mapped_column(String(100), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_seconds: Mapped[int] = mapped_column(Integer)
    target_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))
    stage: Mapped[str] = mapped_column(String(16))  # PRELIMINARY / CONFIRMED，服务端赋值，不可伪造
    version_no: Mapped[int] = mapped_column(Integer)
    publish_seq: Mapped[int] = mapped_column(BigInteger, unique=True)  # 发布序号（全局递增）
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    datum_version_id: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    units: Mapped[list["ProductUnit"]] = relationship(back_populates="product")

    __table_args__ = (UniqueConstraint("series_key", "version_no", name="uq_product_version"),)


class ProductUnit(Base):
    """产品单元：一站 × 一个共同时间格。记录样本来源、覆盖、换算路径与质量原因。"""

    __tablename__ = "product_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    grid_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_seconds: Mapped[int] = mapped_column(Integer)
    value_m: Mapped[float | None] = mapped_column(Float, nullable=True)  # 目标基准水位（米）
    quality_grade: Mapped[str] = mapped_column(String(16))  # MEASURED/INTERPOLATED/PENDING/MISSING/CONFLICT/NO_PATH
    coverage_sec: Mapped[float] = mapped_column(Float, default=0.0)
    coverage_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    source_sample_ids: Mapped[list] = mapped_column(JSONB, default=list)  # 参与的原始样本 id
    conversion_path: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)  # 边序列与偏移
    conversion_status: Mapped[str] = mapped_column(String(16), default="OK")  # OK/NO_PATH/CONFLICT
    quality_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    datum_version_id: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))

    product: Mapped[Product] = relationship(back_populates="units")

    __table_args__ = (
        UniqueConstraint("product_id", "station_id", "grid_start", name="uq_unit_cell"),
    )


# ---------------- 订阅与交付 ----------------

class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    region: Mapped[str] = mapped_column(String(100), index=True)
    stages: Mapped[list] = mapped_column(JSONB, default=list)  # ["PRELIMINARY","CONFIRMED"]
    endpoint_url: Mapped[str] = mapped_column(String(500))
    cursor_seq: Mapped[int] = mapped_column(BigInteger, default=0)  # 已确认的最大发布序号
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    """待交付任务。与产品同事务写入；delivery_key 全局唯一，重送保留同一标识。"""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_key: Mapped[str] = mapped_column(String(120), unique=True)  # sub:{id}-prod:{id}
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    stage: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING/SENT/ACKED/FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 模拟消费端状态（独立进程使用） ----------------

class ConsumerInbox(Base):
    __tablename__ = "consumer_inbox"

    delivery_key: Mapped[str] = mapped_column(String(120), primary_key=True)  # 按标识去重
    subscription_name: Mapped[str] = mapped_column(String(100), index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    stage: Mapped[str] = mapped_column(String(16))
    payload_hash: Mapped[str] = mapped_column(String(64))
    receive_count: Mapped[int] = mapped_column(Integer, default=0)  # 重复投递次数（去重证据）
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsumerState(Base):
    """消费端当前水位视图：每 (系列, 站点, 时间格) 只保留 publish_seq 最大的记录。"""

    __tablename__ = "consumer_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    series_key: Mapped[str] = mapped_column(String(300))
    station_id: Mapped[int] = mapped_column(Integer)
    grid_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_grade: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(16))
    product_version: Mapped[int] = mapped_column(Integer)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("series_key", "station_id", "grid_start", name="uq_consumer_cell"),
    )


class ConsumerFlag(Base):
    __tablename__ = "consumer_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), default="")


# ---------------- 空间授权治理 ----------------

class AuthUser(Base):
    """虚构授权用户（数据接口治理用，不接真实账号/外部权限服务）。"""

    __tablename__ = "auth_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    api_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SpatialCrs(Base):
    """共同坐标系声明。所有授权几何必须落在同一共同 CRS 下，无法解释即配置问题。"""

    __tablename__ = "spatial_crs"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    length_unit: Mapped[str] = mapped_column(String(16), default="m")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SpatialZone(Base):
    """多边形港区（带版本的生效区间）。

    polygons: [{"exterior": [[x,y],...], "holes": [[[x,y],...]]}, ...]
    支持多个不相连区域与内部孔洞；边界归属由 boundary_rule 显式声明。
    边界更正 = 同 code 新增版本（旧版本置 valid_to），求值按观测时刻取适用版本。
    """

    __tablename__ = "spatial_zones"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200))
    crs_id: Mapped[int] = mapped_column(ForeignKey("spatial_crs.id"), index=True)
    polygons: Mapped[list] = mapped_column(JSONB)
    boundary_rule: Mapped[str] = mapped_column(String(8), default="INCLUDE")  # INCLUDE/EXCLUDE
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("code", "version_no", name="uq_zone_version"),
    )


class StationInstallation(Base):
    """站点安装记录（迁站历史）。半开区间 [valid_from, valid_to)。

    观测位置按观测发生时适用的安装记录解释；搬迁不改变旧观测的位置资格。
    x/y 为共同 CRS 下平面坐标，uncertainty_radius 为定位误差范围（同 CRS 长度单位）。
    """

    __tablename__ = "station_installations"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    uncertainty_radius: Mapped[float] = mapped_column(Float, default=0.0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessLicense(Base):
    """一条完整授权条件（不可拆拼）：区域 × 观测适用日期 × 产品阶段 × 允许字段 × 最细粒度。

    多条许可只取各自完整条件成立的数据并集；DENY 在其适用范围优先；
    revoked_at 非空或尚未到 valid_from 的许可不参与放行。
    min_grid_seconds：最细时间粒度（格距秒数），仅当请求格距 >= 该值时其观测才可参与。
    """

    __tablename__ = "access_licenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id"), index=True)
    effect: Mapped[str] = mapped_column(String(8))  # ALLOW / DENY
    zone_code: Mapped[str] = mapped_column(String(64), index=True)  # 按观测时刻解析 zone 版本
    obs_valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    obs_valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stages: Mapped[list] = mapped_column(JSONB, default=list)
    allowed_fields: Mapped[list] = mapped_column(JSONB, default=list)
    min_grid_seconds: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 许可生效时刻
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RangeEvaluation(Base):
    """一次联合范围求值：绑定请求范围、数据截止点与逐条观测的判定明细。"""

    __tablename__ = "range_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id"), index=True)
    scope: Mapped[dict] = mapped_column(JSONB)          # 窗口/格距/阶段/字段/站点/目标基准
    cutoff_obs_id: Mapped[int] = mapped_column(BigInteger)
    license_snapshot: Mapped[list] = mapped_column(JSONB, default=list)  # 参与求值的许可条件快照
    scope_hash: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="online")  # online / membership
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RangeEvaluationItem(Base):
    """求值明细：每条观测绑定具体位置、适用安装、命中许可与放行/排除结论。"""

    __tablename__ = "range_evaluation_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("range_evaluations.id"), index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    obs_id: Mapped[int] = mapped_column(ForeignKey("raw_observations.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    installation_id: Mapped[int | None] = mapped_column(ForeignKey("station_installations.id"),
                                                        nullable=True)
    x: Mapped[float | None] = mapped_column(Float, nullable=True)
    y: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty_radius: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(24), index=True)
    # ALLOWED / DENIED / UNCERTAIN / NO_LICENSE / NO_INSTALLATION / LICENSE_TOO_FINE
    license_id: Mapped[int | None] = mapped_column(ForeignKey("access_licenses.id"), nullable=True)
    matched_license_ids: Mapped[list] = mapped_column(JSONB, default=list)
    deny_license_id: Mapped[int | None] = mapped_column(nullable=True)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)


class AccessSession(Base):
    """分页会话：固定产品范围与数据截止点；每页仍联查当前许可状态。"""

    __tablename__ = "access_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id"), index=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("range_evaluations.id"))
    scope_hash: Mapped[str] = mapped_column(String(64))
    page_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE/TERMINATED
    terminate_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_page_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExtractionTask(Base):
    """受限产品文件生成任务。暂存内容与可领取内容分开，撤回后不得重新挂为可下载。"""

    __tablename__ = "extraction_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id"), index=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("range_evaluations.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("access_sessions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="STAGED")
    # STAGED / READY / CLAIMED / REVOKED / FAILED
    staging_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ready_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope: Mapped[dict] = mapped_column(JSONB)
    allowed_fields: Mapped[list] = mapped_column(JSONB, default=list)
    cell_count: Mapped[int] = mapped_column(Integer, default=0)
    station_codes: Mapped[list] = mapped_column(JSONB, default=list)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryRecord(Base):
    """交付登记：生成内容（哈希/确切范围）+ 接收主体；领取记录另表追加。"""

    __tablename__ = "delivery_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("extraction_tasks.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    delivered_scope: Mapped[dict] = mapped_column(JSONB)
    license_ids: Mapped[list] = mapped_column(JSONB, default=list)
    station_codes: Mapped[list] = mapped_column(JSONB, default=list)
    cell_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClaimRecord(Base):
    """领取记录：每次尝试都留痕（成功/拒绝），可从文件反查实际使用的条件。"""

    __tablename__ = "claim_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_records.id"), nullable=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("extraction_tasks.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id"), index=True)
    result: Mapped[str] = mapped_column(String(24))  # CLAIMED / DENIED_REVOKED / ALREADY_CLAIMED
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LicenseMembership(Base):
    """预计算成员关系：站点适用区间 × 许可。重建期间旧行置 STALE，求值不得使用 STALE 行。"""

    __tablename__ = "license_memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    license_id: Mapped[int] = mapped_column(ForeignKey("access_licenses.id"), index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    geometry_verdict: Mapped[str] = mapped_column(String(16))  # IN / UNCERTAIN
    status: Mapped[str] = mapped_column(String(8), default="ACTIVE", index=True)  # ACTIVE/STALE
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
