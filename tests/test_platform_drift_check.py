"""平台漂移检查脚本：拿合成的 HAR / 交互记录对照 Cursor 的漂移面。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import platform_drift_check as drift  # noqa: E402
from platforms.cursor import drift as cursor_surface  # noqa: E402

KNOWN_EMAIL_ACTION = cursor_surface.known_actions()["submit_email"]
NEW_PASSWORD_ACTION = "f" * 40


def _entry(method: str, url: str, *, headers=None, body: str = "", status: int = 200) -> dict:
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": [{"name": k, "value": v} for k, v in (headers or {}).items()],
            "postData": {"text": body} if body else {},
        },
        "response": {"status": status},
    }


def _har(entries: list[dict]) -> dict:
    return {"log": {"version": "1.2", "entries": entries}}


@pytest.fixture
def capture(tmp_path, monkeypatch):
    # 磁盘缓存指到空文件，避免机器上残留的 confirmed 干扰判定
    monkeypatch.setattr(cursor_surface, "action_cache_path", lambda: tmp_path / "no-cache.json")
    har = tmp_path / "capture.har"
    har.write_text(
        json.dumps(
            _har(
                [
                    _entry("GET", "https://authenticator.cursor.sh/sign-up?client_id=x"),
                    _entry(
                        "POST",
                        "https://authenticator.cursor.sh/sign-up",
                        headers={"next-action": KNOWN_EMAIL_ACTION},
                        body='------x\r\nContent-Disposition: form-data; name="_1_email"\r\n\r\na@b.c\r\n------x--',
                        status=303,
                    ),
                    _entry(
                        "POST",
                        "https://authenticator.cursor.sh/sign-up",
                        headers={"next-action": NEW_PASSWORD_ACTION},
                        body='------x\r\nContent-Disposition: form-data; name="_1_password"\r\n\r\nSecret1!\r\n------x--',
                        status=303,
                    ),
                    _entry("POST", "https://authenticator.cursor.sh/brand-new-step"),
                    _entry("GET", "https://authenticator.cursor.sh/_next/static/chunks/app.js"),
                    _entry("GET", "https://cursor.com/api/auth/callback?code=abc"),
                ]
            )
        ),
        encoding="utf-8",
    )
    interactions = tmp_path / "interactions.json"
    interactions.write_text(
        json.dumps(
            [
                {"type": "input", "tag_name": "input", "url": "https://authenticator.cursor.sh/sign-up",
                 "attributes": {"name": "email", "type": "email"}},
                {"type": "input", "tag_name": "input", "url": "https://authenticator.cursor.sh/sign-up",
                 "attributes": {"name": "firstName", "type": "text"}},
                {"type": "input", "tag_name": "input", "url": "https://authenticator.cursor.sh/sign-up/password",
                 "attributes": {"name": "password", "type": "password"}},
                # 站点新加的字段：代码里没有任何选择器覆盖
                {"type": "input", "tag_name": "input", "url": "https://authenticator.cursor.sh/sign-up",
                 "attributes": json.dumps({"name": "company", "type": "text", "placeholder": "Company"})},
                {"type": "click", "tag_name": "button", "url": "https://authenticator.cursor.sh/sign-up",
                 "attributes": {"type": "submit"}},
            ]
        ),
        encoding="utf-8",
    )
    return har, interactions


class TestActionIds:
    def test_known_changed_and_uncovered_steps(self, capture):
        har, _ = capture
        report = drift.run("cursor", har, None)
        by_id = {item["action_id"]: item for item in report["actions"]["observed"]}

        assert by_id[KNOWN_EMAIL_ACTION]["verdict"] == "known"
        assert by_id[KNOWN_EMAIL_ACTION]["guessed_step"] == "submit_email"
        # 密码步骤的 id 变了：按表单字段能猜出它属于 submit_password，判为"已变更"而不是"未知"
        assert by_id[NEW_PASSWORD_ACTION]["verdict"] == "changed"
        assert by_id[NEW_PASSWORD_ACTION]["guessed_step"] == "submit_password"
        assert report["actions"]["steps_not_seen"] == ["magic_code"]


class TestEndpoints:
    def test_missing_optional_and_unknown(self, capture):
        har, _ = capture
        report = drift.run("cursor", har, None)
        endpoints = report["endpoints"]

        assert endpoints["missing_expected"] == []
        assert endpoints["optional_seen"] == []
        # 静态资源不算；brand-new-step 算已知主机上的新端点
        assert endpoints["unknown_on_known_hosts"] == ["POST authenticator.cursor.sh/brand-new-step"]


class TestSelectors:
    def test_matches_recorded_inputs_and_flags_uncovered(self, capture):
        har, interactions = capture
        report = drift.run("cursor", har, interactions)
        stages = report["selectors"]["stages"]

        signup = {item["selector"]: item["ok"] for item in stages["signup"]}
        assert signup['input[name="email"]'] is True
        assert signup['input[name="firstName"], input[name="first_name"]'] is True
        # 录制里没填 lastName → 选择器"可能失效"（也可能只是这次没录到，报告让人去确认）
        assert signup['input[name="lastName"], input[name="last_name"]'] is False
        assert all(item["ok"] for item in stages["password"])
        # 没有手机页 → phone 阶段全部未匹配
        assert not any(item["ok"] for item in stages["phone"])
        uncovered = report["selectors"]["uncovered_inputs"]
        assert len(uncovered) == 1 and uncovered[0]["name"] == "company"


class TestCli:
    def test_exit_code_reflects_drift_and_json_output_is_parseable(self, capture, capsys):
        har, interactions = capture
        code = drift.main(["--platform", "cursor", "--har", str(har), "--interactions", str(interactions), "--json"])
        out = capsys.readouterr().out
        payload = json.loads(out)

        assert code == 1  # 有"已变更"的 action id
        assert payload["platform"] == "cursor"

    def test_text_report_mentions_changed_action(self, capture, capsys):
        har, _ = capture
        drift.main(["--platform", "cursor", "--har", str(har)])
        out = capsys.readouterr().out

        assert "[已变更] submit_password" in out
        assert "_KNOWN_ACTIONS" in out

    def test_unknown_platform_is_a_clear_error(self, capture):
        har, _ = capture
        with pytest.raises(SystemExit, match="drift 声明"):
            drift.run("nonexistent_platform", har, None)


class TestChatGPTSurface:
    def test_loads_without_a_server_action_layer(self):
        """ChatGPT 不是 Next.js server-action 站点，没有 action id 这一层。

        没有并不代表不能做漂移检查：端点与选择器两项照常跑。
        """
        import importlib

        surface = importlib.import_module("platforms.chatgpt.drift")
        assert surface.known_actions() == {}
        assert surface.action_cache_path() is None
        assert drift.check_action_ids([], surface)["observed"] == []
        assert surface.EXPECTED_ENDPOINTS and surface.SELECTORS_BY_STAGE

    def test_selectors_come_from_the_live_modules(self):
        """声明必须是 import 来的，不能是手抄的副本。

        手抄的话，检查通过只能说明两份抄写一致。
        """
        import importlib

        surface = importlib.import_module("platforms.chatgpt.drift")
        dom = importlib.import_module("platforms.chatgpt.dom")
        assert surface.SELECTORS_BY_STAGE["otp"] == tuple(dom.OTP_INPUT_SELECTORS[:4])
        assert surface.SELECTORS_BY_STAGE["email"] == tuple(dom.EMAIL_INPUT_SELECTORS[:4])


#: 所有走 Step 引擎的平台都必须有一份能被脚本读懂的漂移面
STEP_PLATFORMS = ["cursor", "chatgpt", "grok", "trae", "kiro", "tavily", "openblocklabs", "windsurf"]
#: 选择器按列表声明（另外几个按站点结构分组，字段名不统一）
LIST_SELECTOR_PLATFORMS = ["grok", "trae", "kiro", "openblocklabs"]


class TestStepPlatformSurfaces:
    """迁到 Step 引擎的平台：漂移面要真的接得上脚本，而不是只存在。"""

    @pytest.mark.parametrize("platform", STEP_PLATFORMS)
    def test_surface_loads_and_the_script_can_read_it(self, platform):
        import importlib

        surface = importlib.import_module(f"platforms.{platform}.drift")
        assert drift.check_action_ids([], surface)["observed"] == []
        assert surface.EXPECTED_ENDPOINTS, platform
        assert surface.SELECTORS_BY_STAGE, platform
        # 端点三元组的形状：(method, host, path)
        for method, host, path in surface.EXPECTED_ENDPOINTS + surface.OPTIONAL_ENDPOINTS:
            assert method.isupper() and "://" not in host and path.startswith("/")

    @pytest.mark.parametrize("platform", LIST_SELECTOR_PLATFORMS)
    def test_selectors_come_from_the_live_modules(self, platform):
        """声明必须是 import 来的，不能是手抄的副本。"""
        import importlib

        surface = importlib.import_module(f"platforms.{platform}.drift")
        dom = importlib.import_module(f"platforms.{platform}.dom")
        assert surface.SELECTORS_BY_STAGE["otp"] == tuple(dom.OTP_INPUT_SELECTORS)
        assert surface.SELECTORS_BY_STAGE["password"] == tuple(dom.PASSWORD_INPUT_SELECTORS)

    def test_kiro_pins_the_aws_spa_step_names(self):
        """AWS 改了 hash 里的步骤名，steps.py 的守卫会全部失效且**不报错**。"""
        from platforms.kiro import drift as surface, steps

        assert set(surface.SIGNUP_HASH_STEPS) == {
            steps.dom.STEP_ENTER_EMAIL,
            steps.dom.STEP_ENTER_NAME,
            steps.dom.STEP_VERIFY_EMAIL,
            steps.dom.STEP_CREATE_PASSWORD,
        }

    def test_windsurf_pins_the_two_response_paths_the_result_comes_from(self):
        """注册结果不在 DOM 上；这两个路径改了会"看上去成功"但拿不到 token。"""
        from platforms.windsurf import dom, drift as surface

        assert surface.RESULT_RESPONSE_PATHS == (dom.EMAIL_COMPLETE_PATH, dom.POST_AUTH_PATH)

    def test_tavily_pins_the_regex_that_finds_the_signup_entry(self):
        """入口 URL 带一次性 state，只能从 sign-in 页 HTML 里抠；正则漂了就卡在第一步。"""
        import inspect

        from platforms.tavily import dom, drift as surface

        assert surface.SIGNUP_LINK_PATTERN in inspect.getsource(dom.extract_signup_url)

    def test_openblocklabs_pins_the_hardcoded_workos_client_id(self):
        from platforms.openblocklabs import dom, drift as surface

        assert surface.KNOWN_CLIENT_ID == dom.CLIENT_ID

    def test_grok_keeps_the_protocol_action_id_visible(self):
        """浏览器路径不打 server action，但它漂了协议路径就废，所以一并盯着。"""
        from platforms.grok import core, drift as surface

        assert surface.known_actions() == {"signup": core.NEXT_ACTION}

    def test_trae_hosts_agree_between_the_two_paths(self):
        """协议路径与浏览器路径写死的域名必须是同一批——import 时就断言了，这里固定住。"""
        from platforms.trae import core, dom

        assert core.BASE_URL == f"https://{dom.TRAE_PASSPORT_DOMAIN}"
        assert core.API_SG == dom.TRAE_API_SG
