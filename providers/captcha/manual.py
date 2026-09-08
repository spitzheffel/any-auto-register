"""人工打码 — 把题面推给操作员，等待回答。"""
from core.base_captcha import BaseCaptcha, CaptchaChallenge, CaptchaSolution, ChallengeKind
from core.errors import ConfigError
from providers.registry import register_provider


@register_provider("captcha", "manual")
class ManualCaptcha(BaseCaptcha):
    """人工打码。

    人也是一种 provider：题面经 HumanChannel 推给操作员，阻塞等待回答。
    没有通道就是配置错误——服务线程里没有 stdin，读 input() 只会挂住或 EOFError。
    """

    SUPPORTED_KINDS = (ChallengeKind.TURNSTILE, ChallengeKind.IMAGE)

    def __init__(self, human_channel=None):
        self.human_channel = human_channel

    @classmethod
    def from_config(cls, config: dict) -> 'ManualCaptcha':
        return cls(human_channel=(config or {}).get("human_channel"))

    def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        if challenge.kind == ChallengeKind.TURNSTILE:
            prompt = f"请手动获取 Turnstile token ({challenge.page_url})"
        else:
            prompt = "请输入图片验证码"

        answer = self._ask(prompt, challenge)
        if challenge.kind == ChallengeKind.TURNSTILE:
            return CaptchaSolution(token=answer)
        return CaptchaSolution(text=answer)

    def _ask(self, prompt: str, challenge: CaptchaChallenge) -> str:
        if self.human_channel is None:
            raise ConfigError("人工打码需要可用的人工通道：请在 Web 界面发起任务（终端模式下不可用）")
        return str(self.human_channel.ask_captcha(prompt, challenge) or "").strip()
