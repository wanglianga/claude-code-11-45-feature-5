"""基于 API Key 的鉴权（演示环境）。

两类主体：
- 角色 Key（admin/operator/consumer）：管理目录、作业、订阅与内部审计；
- 授权用户 Key（AuthUser 表，虚构用户）：空间授权数据接口的实际访问主体，
  每个请求联查其当前许可，不接外部权限服务。
普通用户的错误信息不确认其他港区站点/文件是否存在。
"""
from __future__ import annotations

from fastapi import Header, HTTPException
from sqlalchemy import select

from .config import ADMIN_KEY, CONSUMER_KEY, OPERATOR_KEY
from .db import SessionLocal
from .models import AuthUser

ROLES = {
    ADMIN_KEY: "admin",
    OPERATOR_KEY: "operator",
    CONSUMER_KEY: "consumer",
}


def require_role(*allowed: str):
    def dep(x_api_key: str = Header(default="")) -> str:
        role = ROLES.get(x_api_key)
        if role is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        if role not in allowed:
            raise HTTPException(status_code=403, detail=f"role {role} not allowed")
        return role

    return dep


def require_auth_user(x_api_key: str = Header(default="")) -> AuthUser:
    """授权用户身份：按 AuthUser.api_key 联查，停用即拒。"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="invalid API key")
    s = SessionLocal()
    try:
        user = s.execute(
            select(AuthUser).where(AuthUser.api_key == x_api_key)
        ).scalar_one_or_none()
    finally:
        s.close()
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="invalid or inactive API key")
    return user


def require_admin_or_user(x_api_key: str = Header(default="")):
    """审计/资料查询：admin 角色或本人授权用户均可，返回 (kind, principal)。"""
    role = ROLES.get(x_api_key)
    if role == "admin":
        return ("role", role)
    s = SessionLocal()
    try:
        user = s.execute(
            select(AuthUser).where(AuthUser.api_key == x_api_key)
        ).scalar_one_or_none()
    finally:
        s.close()
    if user is not None and user.active:
        return ("user", user)
    raise HTTPException(status_code=401, detail="invalid API key")
