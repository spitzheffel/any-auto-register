"""检查点：把每步的游标、可序列化状态与资源引用写进任务 result。

用途不是"从这里续跑"——浏览器流程做不到，页面状态随进程消失；而是让服务重启后
能把还在计费的号码退掉、把邮箱释放掉。协议流程状态就是 cookie/token，可以真续跑，
引擎的 start_at 入口为此保留。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterator

from core.registration.steps import SENSITIVE_KEYS

ATTEMPTS_KEY = "attempts"


def _serializable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict, tuple))


def sanitize_state(state: dict) -> dict:
    """只保留可序列化且非敏感的键。

    检查点进的是任务 result_json，会经任务接口下发；密码 / 会话 token / 手机号
    一律不落。重启回收只需要 resource_refs 里的 activation_id 与邮箱地址。
    """
    return {
        key: value
        for key, value in (state or {}).items()
        if key not in SENSITIVE_KEYS and _serializable(value)
    }


def sanitize_refs(refs: dict) -> dict:
    """资源引用同样脱敏：account 只留 email / submitted，其它引用去掉敏感键。"""
    result: dict[str, Any] = {}
    for name, ref in dict(refs or {}).items():
        if not isinstance(ref, dict):
            if _serializable(ref):
                result[name] = ref
            continue
        if name == "account":
            result[name] = {
                key: ref[key] for key in ("email", "submitted") if key in ref and _serializable(ref[key])
            }
            continue
        result[name] = {
            key: value
            for key, value in ref.items()
            if key not in SENSITIVE_KEYS and _serializable(value)
        }
    return result


class TaskCheckpointSink:
    """写进任务 result_json 的 attempts[attempt_id]。

    ``mutate(task_id, fn)`` 由任务层注入（application.tasks._mutate_task），
    core 不反向依赖 application。
    """

    def __init__(self, task_id: str, *, mutate: Callable[[str, Callable[[Any], None]], Any]):
        self.task_id = task_id
        self._mutate = mutate

    def write(
        self,
        *,
        attempt_id: str,
        cursor: int,
        step: str,
        state: dict,
        resource_refs: dict,
        channels_used: dict,
        phase: str = "done",
    ) -> None:
        payload = {
            "cursor": cursor,
            "step": step,
            "phase": phase,
            "state": sanitize_state(state),
            "resource_refs": sanitize_refs(resource_refs),
            "channels_used": dict(channels_used or {}),
            "updated_at": time.time(),
        }

        def _update(task) -> None:
            result = task.get_result()
            attempts = dict(result.get(ATTEMPTS_KEY) or {})
            attempts[attempt_id or "default"] = payload
            result[ATTEMPTS_KEY] = attempts
            task.set_result(result)

        self._mutate(self.task_id, _update)

    def mark_done(self, attempt_id: str) -> None:
        """尝试已在进程内收尾（成功 / 失败 / 取消都算）：外部资源已按正常路径释放，重启回收要跳过它。

        号码 / 邮箱引用清掉；``account``（半成品账号标记）留着供人工排查。
        """

        def _update(task) -> None:
            result = task.get_result()
            attempts = dict(result.get(ATTEMPTS_KEY) or {})
            checkpoint = dict(attempts.get(attempt_id or "default") or {})
            if not checkpoint:
                return
            refs = dict(checkpoint.get("resource_refs") or {})
            checkpoint["done"] = True
            checkpoint["resource_refs"] = {"account": refs["account"]} if refs.get("account") else {}
            checkpoint["updated_at"] = time.time()
            attempts[attempt_id or "default"] = checkpoint
            result[ATTEMPTS_KEY] = attempts
            task.set_result(result)

        self._mutate(self.task_id, _update)


def iter_attempt_checkpoints(result: dict) -> Iterator[tuple[str, dict]]:
    for attempt_id, checkpoint in dict((result or {}).get(ATTEMPTS_KEY) or {}).items():
        if isinstance(checkpoint, dict):
            yield str(attempt_id), checkpoint


def release_attempt_resources(checkpoint: dict, *, platform: str = "", extra: dict | None = None) -> list[str]:
    """退还一次中断尝试占用的资源，返回给任务事件的说明。

    ``extra`` 是任务 payload 里的 extra：只填在任务参数里、没存设置页的凭据也要能用上。
    """
    messages: list[str] = []
    refs = dict((checkpoint or {}).get("resource_refs") or {})
    extra = dict(extra or {})

    phone_ref = refs.get("phone")
    if isinstance(phone_ref, dict) and phone_ref.get("activation_id"):
        messages.append(_release_phone(phone_ref, extra=extra))

    mailbox_ref = refs.get("mailbox")
    if isinstance(mailbox_ref, dict) and mailbox_ref.get("email"):
        messages.append(_release_mailbox(mailbox_ref, platform=platform, extra=extra))

    return [message for message in messages if message]


def _release_phone(ref: dict, *, extra: dict | None = None) -> str:
    from core.phone_verification import OUTCOME_UNUSED, PhoneVerification, resolve_sms_settings

    activation_id = str(ref.get("activation_id") or "")
    provider_key = str(ref.get("provider_key") or "")
    try:
        config = resolve_sms_settings(provider_key, dict(extra or {})) if provider_key else None
        lease = PhoneVerification.reclaim(ref, config=config)
        if lease is None:
            return ""
        lease.release(OUTCOME_UNUSED)
        return f"已回收中断尝试占用的手机号: activation_id={activation_id}"
    except Exception as exc:
        return f"回收手机号失败 (activation_id={activation_id}): {exc}"


def _release_mailbox(ref: dict, *, platform: str = "", extra: dict | None = None) -> str:
    """按引用里记的 provider 单独建一个实例来释放。

    不走 ``create_mailbox``：它会把所有启用的 provider 组成 FallbackMailbox，
    而 FallbackMailbox 需要账号上带 ``mailbox_provider_key`` 才知道该找谁释放。
    """
    from core.base_mailbox import METADATA_CONFIG_KEY, PROXY_CONFIG_KEY, MailboxAccount
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository
    from providers.registry import create_provider

    email = str(ref.get("email") or "")
    provider_key = str(ref.get("provider_key") or "")
    if not provider_key:
        return ""
    try:
        definition = ProviderDefinitionsRepository().get_by_key("mailbox", provider_key)
        if definition is None:
            return f"释放邮箱失败 ({email}): 邮箱 provider 定义不存在 {provider_key}"
        settings = ProviderSettingsRepository().resolve_runtime_settings("mailbox", provider_key, dict(extra or {}))
        settings[PROXY_CONFIG_KEY] = None
        settings[METADATA_CONFIG_KEY] = definition.get_metadata() or {}
        mailbox = create_provider("mailbox", str(definition.driver_type or provider_key), settings)
        account_extra = dict(ref.get("extra") or {})
        account_extra.setdefault("mailbox_provider_key", provider_key)
        mailbox.release(
            MailboxAccount(email=email, account_id=str(ref.get("account_id") or ""), extra=account_extra),
            reason="interrupted",
            platform=platform,
        )
        return f"已释放中断尝试占用的邮箱: {email}"
    except Exception as exc:
        return f"释放邮箱失败 ({email}): {exc}"
