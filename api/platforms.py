from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from application.platforms import PlatformsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platforms", tags=["platforms"])
service = PlatformsService()


@router.get("")
def list_platforms():
    return service.list_platforms()


@router.get("/{platform}/desktop-state")
def get_desktop_state(platform: str):
    return service.get_desktop_state(platform)


@router.get("/{platform}/requirements")
def get_platform_requirements(
    platform: str,
    executor_type: str = "protocol",
    identity_provider: str = "mailbox",
    mail_provider: str = "",
    sms_provider: str = "",
    captcha_solver: str = "",
):
    # 用户在表单里的当前选择随请求带上，"配没配"才能按他实际选的那家算
    channel_bindings: dict[str, dict[str, str]] = {}
    for kind, key in (("mailbox", mail_provider), ("sms", sms_provider), ("captcha", captcha_solver)):
        if str(key or "").strip():
            channel_bindings[kind] = {"key": key.strip()}

    try:
        return service.describe_requirements(
            platform,
            executor_type=executor_type,
            identity_provider=identity_provider,
            channel_bindings=channel_bindings,
        )
    except KeyError as exc:
        raise HTTPException(404, f"平台不存在: {exc}")
    except NotImplementedError as exc:
        raise HTTPException(404, f"该平台不支持此执行方式: {exc}")
    except Exception:
        # DB 等基础设施异常不能伪装成"平台不存在"，否则前端会把它当成正常的空需求
        logger.exception("推导平台需求失败: platform=%s executor=%s", platform, executor_type)
        raise HTTPException(500, "推导平台需求失败，请查看服务日志")
