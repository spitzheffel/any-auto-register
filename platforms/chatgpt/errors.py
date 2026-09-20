"""ChatGPT 注册的错误类型与站点文案判定。

站点返回的文字是判断"该换号还是该停批"的唯一依据，集中在这里，
免得判定散落在各步骤里各写一遍正则。
"""

from core.errors import RegistrationFailure, ResourceRejectedError, RiskControlError, TransientError


class PhoneRejectedError(ResourceRejectedError):
    """目标站点拒收当前手机号，重试同一号码没有意义，必须换号。"""


# 号码级拒绝：OpenAI 明确拒绝这个号码本身（已被占用、近期用过、运营商不支持等）
PHONE_REJECTED_PATTERNS = (
    "recently used",
    "already in use",
    "already been used",
    "phone_number_in_use",
    "another account",
    "different phone",
    "not supported",
    "unsupported",
    "blocked",
)


def _is_phone_rejected_error(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    if any(pattern in lowered for pattern in PHONE_REJECTED_PATTERNS):
        return True
    # "invalid phone number" 之类：必须同时提到号码，避免把验证码错误误判成号码问题
    if "phone" not in lowered and "number" not in lowered:
        return False
    return any(keyword in lowered for keyword in ("invalid", "not valid", "cannot", "can't", "unable"))


class PhoneCodeTimeout(TransientError):
    """这个号码等不到短信。

    与 cursor 不同：ChatGPT 的 /add-phone 页可以原地换号重进，所以步骤接住它
    之后 swap 一个新号再重试，而不是让整次尝试失败。
    """


# 风控级文案：换号 / 重试都没用，是这个 IP + 指纹组合被判了
RISK_CONTROL_TEXT_PATTERNS = (
    "sorry, we cannot create your account",
    "we cannot create your account",
    "failed to create account",
    "unusual activity",
    "suspicious activity",
    "not available in your country",
    "is not available in your",
    "access denied",
)


def error_from_auth_text(text: str, *, default=TransientError, prefix: str = "") -> RegistrationFailure:
    """把站点给出的错误文案翻译成分类错误。

    引擎按分类决定重试 / 换号 / 停批，所以这里的判定直接决定一次失败要花几个
    号、几次尝试。拿不准时退到 default（通常是 transient），让引擎自己重试。
    """
    raw = str(text or "").strip()
    lowered = raw.lower()
    message = f"{prefix}{raw}"[:300] if raw else (prefix.rstrip(": ") or "站点未给出错误文案")
    if any(pattern in lowered for pattern in RISK_CONTROL_TEXT_PATTERNS):
        return RiskControlError(message)
    return default(message)
