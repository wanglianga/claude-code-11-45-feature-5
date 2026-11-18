"""全局配置：数据库连接、演示用 API Key、重采样参数默认值。"""
from __future__ import annotations

import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
)

# 演示环境 API Key（仅用于隔离开发/验收，不作为生产配置）
ADMIN_KEY = os.getenv("ADMIN_KEY", "admin-key")
OPERATOR_KEY = os.getenv("OPERATOR_KEY", "operator-key")
CONSUMER_KEY = os.getenv("CONSUMER_KEY", "consumer-key")

# 重采样默认参数
DEFAULT_MAX_REP_SEC = int(os.getenv("MAX_REP_SEC", "1200"))          # 单样本最大代表时长 20min
DEFAULT_MIN_COVERAGE = float(os.getenv("MIN_COVERAGE", "0.5"))       # 有效覆盖阈值
DEFAULT_MAX_INTERP_GAP_SEC = int(os.getenv("MAX_INTERP_GAP_SEC", "1800"))  # 允许插值的最大断档 30min

# 交付重试
WORKER_MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "8"))
WORKER_RETRY_BACKOFF_SEC = int(os.getenv("WORKER_RETRY_BACKOFF_SEC", "5"))

# 消费端模拟故障标志
FLAG_DROP_ACK = "drop_ack"
FLAG_OFFLINE = "offline"

# 受限产品文件暂存/可领取目录（暂存内容与已允许领取内容物理分开）
EXTRACT_DIR = os.getenv("EXTRACT_DIR", "/tmp/tide_extractions")
