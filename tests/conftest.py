"""集成测试夹具：独立的 ccdev_test 数据库，每会话重建。"""
from __future__ import annotations

import os

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker

os.environ["DATABASE_URL"] = "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev_test"

from app import models  # noqa: E402
from app.db import Base, engine  # noqa: E402


def _ensure_test_db():
    admin = sqlalchemy.create_engine(
        "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as conn:
        exists = conn.execute(
            sqlalchemy.text("select 1 from pg_database where datname='ccdev_test'")
        ).scalar()
        if not exists:
            conn.execute(sqlalchemy.text("create database ccdev_test"))
    admin.dispose()


@pytest.fixture(scope="session")
def db_engine():
    _ensure_test_db()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture()
def session(db_engine):
    # 每个用例清空全部表，保证相互独立
    with db_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(sqlalchemy.text(f"truncate table {table.name} cascade"))
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()
