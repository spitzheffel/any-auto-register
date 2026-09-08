"""Cursor 协议邮箱注册 worker。"""
from __future__ import annotations

from typing import Callable, Optional

from core.errors import ConfigError, TransientError
from platforms.cursor.core import CursorRegister, _rand_password


class CursorProtocolMailboxWorker:
    def __init__(self, *, proxy: str | None = None, log_fn: Callable[[str], None] = print):
        self.client = CursorRegister(proxy=proxy, log_fn=log_fn)
        self.log = log_fn

    def _handle_phone_challenge(self, phone, *, should_stop=None) -> tuple[bool, str]:
        """检测最近一次提交是否被重定向到 radar-challenge，是则走协议通道完成手机验证。

        返回 (是否真的执行了手机验证, 验证后响应里可直接提取到的 auth code)。
        调用方据第一项决定要不要重提 OTP：没做过手机验证就重提同一个 OTP 只是白耗一次。
        """
        redirect = getattr(self.client, "last_action_redirect", "") or ""
        if not self.client.is_radar_challenge_redirect(redirect):
            return False, ""
        if phone is None:
            raise RuntimeError(
                "Cursor 注册触发手机号验证 (radar-challenge)，但协议模式未配置接码服务："
                "请在任务参数或全局默认里配置 SMS provider，或改用浏览器模式手动完成验证"
            )
        self.log("检测到手机号验证挑战 (radar-challenge)，走协议通道提交...")
        resp = self.client.run_radar_challenge(phone, redirect, should_stop=should_stop)
        return True, self.client.extract_auth_code(resp)

    def run(
        self,
        *,
        email: str,
        password: str | None = None,
        otp_callback: Optional[Callable[[], str]] = None,
        captcha_solver=None,
        phone=None,
        should_stop=None,
    ) -> dict:
        use_password = password or _rand_password()
        self.log(f"邮箱: {email}")
        self.log("Step1: 获取 session...")
        state_encoded, _ = self.client.step1_get_session()
        self.log("Step2: 提交邮箱...")
        self.client.step2_submit_email(email, state_encoded)
        self.log("Step3: 提交密码 + Turnstile...")
        self.client.step3_submit_password(use_password, email, state_encoded, captcha_solver)
        # 密码提交后可能被要求手机号验证（浏览器版里该页面就出现在密码页之后）
        _, auth_code = self._handle_phone_challenge(phone, should_stop=should_stop)
        if not auth_code:
            if otp_callback is None:
                # 服务线程里没有 stdin，读 input() 只会挂住或 EOFError；没邮箱通道就是配置问题
                raise ConfigError("Cursor 协议注册需要邮箱验证码，但未配置 mailbox provider")
            self.log("等待 OTP 邮件...")
            otp = otp_callback()
            if not otp:
                raise TransientError("未获取到邮箱验证码")
            self.log(f"验证码: {otp}")
            self.log("Step4: 提交 OTP...")
            auth_code = self.client.step4_submit_otp(otp, email, state_encoded)
            if not auth_code:
                # OTP 之后也可能触发手机号验证，通过后响应一般会直接带 code；
                # 没有触发手机验证就不再重提同一个 OTP——它不会突然变有效
                verified, auth_code = self._handle_phone_challenge(phone, should_stop=should_stop)
                if verified and not auth_code:
                    auth_code = self.client.step4_submit_otp(otp, email, state_encoded)
        if not auth_code:
            # 验证码过期 / 串号属于换一次尝试就好的问题，按 transient 让任务层补投
            raise TransientError("Cursor 未返回 auth code（验证码可能已过期、不属于本次注册，或手机验证未通过）")
        self.log("Step5: 获取 Token...")
        token = self.client.step5_get_token(auth_code, state_encoded)
        if not token:
            raise RuntimeError("注册流程已通过但未取到 WorkosCursorSessionToken，账号无法使用")
        return {"email": email, "password": use_password, "token": token}
