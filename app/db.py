"""数据库引擎与会话。"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL


def _json_default(value):
    """JSONB 统一序列化：datetime 等类型落 ISO 字符串，避免静默失败。"""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False, default=_json_default),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(engine)
