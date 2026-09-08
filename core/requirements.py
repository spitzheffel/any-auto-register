"""按流程需求推导要哪些渠道。

今天注册页把邮箱/短信下拉框全都摆出来，不管当前平台+执行器+身份模式到底用不用；
缺配置也要跑到几分钟后的那一步才炸。这里从**代码本身**推导需求：
Step 平台看步骤的 requires 与 when 守卫，未迁移的平台看 adapter 上的声明。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.channel_bindings import CAPTCHA, MAILBOX, SMS

LEVEL_REQUIRED = "required"
LEVEL_CONDITIONAL = "conditional"

# 能力 → 渠道类别
CAPABILITY_CHANNEL = {
    "otp": MAILBOX,
    "link": MAILBOX,
    "mailbox": MAILBOX,
    "phone": SMS,
    "captcha": CAPTCHA,
}


@dataclass(slots=True)
class Requirement:
    capability: str
    level: str = LEVEL_REQUIRED
    reason: str = ""

    @property
    def channel(self) -> str:
        return CAPABILITY_CHANNEL.get(self.capability, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "channel": self.channel,
            "level": self.level,
            "reason": self.reason,
        }


def merge_requirements(items: list[Requirement]) -> list[Requirement]:
    """同一能力出现多次时取更强的级别（required > conditional）。"""
    merged: dict[str, Requirement] = {}
    for item in items:
        existing = merged.get(item.capability)
        if existing is None:
            merged[item.capability] = item
            continue
        if existing.level == LEVEL_CONDITIONAL and item.level == LEVEL_REQUIRED:
            merged[item.capability] = item
    return list(merged.values())


def from_step_flow(flow, obstacles=()) -> list[Requirement]:
    """Step 平台：步骤的 requires 就是答案。

    带 when 守卫的步骤是可选阶段 → conditional；
    ObstacleHandler 的需求也是 conditional（干扰不一定出现）。
    """
    items: list[Requirement] = []
    for step in getattr(flow, "steps", []):
        # 覆写过 when 的步骤才算"可选阶段"
        conditional = type(step).when is not _base_when()
        level = LEVEL_CONDITIONAL if conditional else LEVEL_REQUIRED
        for capability in getattr(step, "requires", ()):
            items.append(
                Requirement(capability, level, reason=f"步骤 {step.name}" + ("（按需触发）" if conditional else ""))
            )

    for handler in obstacles or ():
        for capability in getattr(handler, "requires", ()) or _obstacle_capabilities(handler):
            items.append(
                Requirement(capability, LEVEL_CONDITIONAL, reason=f"干扰处理 {getattr(handler, 'name', '')}")
            )
    return merge_requirements(items)


def _base_when():
    from core.registration.steps import Step

    return Step.when


def _obstacle_capabilities(handler) -> tuple[str, ...]:
    # Turnstile 类 handler 会向 CapabilityResolver 要 captcha
    name = str(getattr(handler, "name", "")).lower()
    return ("captcha",) if "turnstile" in name or "captcha" in name else ()


def from_adapter(adapter, *, identity_provider: str = "mailbox", browser_mode: bool = False) -> list[Requirement]:
    """未迁移到 Step 的平台：从 adapter 的声明推导。"""
    items: list[Requirement] = []
    if adapter is None:
        return items

    if identity_provider == "oauth_browser":
        # OAuth 走已登录的浏览器会话，不需要邮箱/短信渠道
        return items

    if getattr(adapter, "otp_spec", None) is not None:
        items.append(Requirement("otp", LEVEL_REQUIRED, reason="流程需要邮箱验证码"))
    if getattr(adapter, "link_spec", None) is not None:
        items.append(Requirement("link", LEVEL_REQUIRED, reason="流程需要邮件验证链接"))
    if getattr(adapter, "use_phone", False):
        items.append(Requirement("phone", LEVEL_CONDITIONAL, reason="流程可能要求手机号验证"))
    elif browser_mode:
        # 浏览器流程随时可能被风控要求加手机号
        items.append(Requirement("phone", LEVEL_CONDITIONAL, reason="浏览器流程可能触发手机号验证"))
    if getattr(adapter, "use_captcha", False) or getattr(adapter, "use_captcha_for_mailbox", False):
        items.append(Requirement("captcha", LEVEL_REQUIRED, reason="流程需要验证码解决器"))
    if not items or identity_provider == "mailbox":
        items.append(Requirement("mailbox", LEVEL_REQUIRED, reason="身份来自邮箱 provider"))
    return merge_requirements(items)
