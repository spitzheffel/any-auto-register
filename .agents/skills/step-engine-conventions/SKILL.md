---
name: step-engine-conventions
description: "Conventions for the registration Step engine, PhoneLease, channel bindings, error taxonomy, checkpoints and their tests. Read before writing or reviewing any platforms/<x>/steps.py, obstacles.py, plugin.py, or anything under core/registration/. Points to source-of-truth files instead of restating facts that drift."
---

# Step 引擎约定

这份 skill 只回答"规则在哪、怎么验证"。凡是能从代码推出来的事实（当前哪些渠道是 required、Cursor 现在的 action id、重试次数）一律**去读代码或跑命令**，不要凭记忆写进新代码或新 skill。

---

## 源头文件（先读，再动手）

- 引擎：`core/registration/steps.py` —— `Step` / `Flow` / `StepRunner` / `StepContext` / `CapabilityResolver` / `ObstacleHandler` / `RetryPolicy` 与默认重试表 `DEFAULT_RETRY_BY_CATEGORY`
- 运行时契约：`core/registration/runtime.py`（`StepRuntime`，日志、取消令牌、人工通道、检查点、渠道绑定都从它取）
- 装配：`core/registration/flows.py::run_step_flow`，`core/base_platform.py::register`
- 错误分类：`core/errors.py`（docstring 就是处置表：谁原地重试、谁补投、谁停批）
- 号码租约：`core/phone_verification.py`（`PhoneVerification.acquire` / `PhoneLease.wait_code|report_*|release|swap`）
- 检查点与脱敏：`core/checkpoints.py`，`SENSITIVE_KEYS` 在 `core/registration/steps.py`
- 能力名 ↔ 渠道名：`core/requirements.py::CAPABILITY_CHANNEL`（唯一一份映射）
- 干扰处理：`core/registration/obstacles.py`（通用 Turnstile / 全页拦截），平台装配见 `platforms/cursor/obstacles.py`
- 公共页面辅助：`core/registration/dom.py`（选择器轮询、cookie 等待、CF 全页拦截、Turnstile 点击与注入）——平台的 `dom.py` 只留站点特有常量
- 可取消等待：`core/cancellation.py`（`cancellable_sleep` / `poll_until` / `accepts_should_stop`）

## 按模式找参考（8 个平台都在引擎上，挑最像你手头那个的读）

| 要解决的问题 | 读哪个 |
|---|---|
| 完整的一份（手机验证 + 两个 ObstacleHandler + 协议路径对照） | `platforms/cursor/` |
| 最小的一份（一个 dom.py + 一个 steps.py，五六步） | `platforms/grok/`、`platforms/trae/` |
| 页面顺序由服务端决定（同一个 Step 挂两次，靠 `when()` 认领） | `platforms/chatgpt/steps.py` |
| 需要第二个浏览器会话（注册完的上下文做 OAuth 会被断） | `platforms/chatgpt/steps.py` 的 `CodexOAuth` |
| 结果只在网络响应里、DOM 上读不到 | `platforms/windsurf/steps.py`、`platforms/trae/steps.py` |
| 站点是 hash 驱动的多步 SPA | `platforms/kiro/steps.py` |
| captcha 是表单的必填字段，不是偶发干扰 | `platforms/tavily/steps.py` |
| Cloudflare 全页拦截 + Turnstile 的 handler 装配 | `platforms/openblocklabs/obstacles.py`、`platforms/cursor/obstacles.py` |
| 同时要 `otp` 和 `link` 两个邮件能力 | `platforms/tavily/steps.py` |
| 允许内核回退到 Chromium（少见，只有这个） | `platforms/windsurf/plugin.py` |

## 运行时原语（步骤里一律用这几个）

- 等待：`sc.sleep` / `sc.poll` / `sc.wait_for_selector` / `sc.wait_for_url`。引擎只在**步骤之间**查取消令牌，步骤内部裸写 `time.sleep` / `while True` 等于把取消延迟拉到整步长度。
- 回调：`sc.call_with_cancel(fn)`——按签名判断后才注入 `should_stop`，不接的实现照旧跑满自己的 timeout。
- 辅助浏览器：`sc.open_aux_browser(name)` / `sc.close_aux_browser(name)`。按主会话的引擎 / 代理 / 无头设置另开一个隔离会话（典型是"注册完的上下文做 OAuth 会被断"）。**成功时自己关，失败时交给 `compensate()` 关**：引擎是先采集现场再补偿的，提前关掉现场包里就只剩一张空白截图。

## 步骤契约

- `reads` / `writes` 只到键名。`Flow` 构建期校验"读的键要有来源"，运行期校验"声明写的键真的写了"。`initial_keys` 默认是 `email` / `password`（见 `run_step_flow` 的初始 state）。
- 只有能"从零建立页面状态"的步骤才 `anchor = True`（Cursor 里只有 `OpenSignupPage`）。回撤目标必须是 anchor，否则是契约错误。
- 可选阶段用 `when()` 守卫返回 False 跳过；`core/requirements.py::from_step_flow` 按"**有没有重写 `when()`**"判定可选阶段，重写了就把该步骤的 `requires` 从 required 降成 conditional。
- **这条推导很容易被误用**：只要给步骤加了守卫，它依赖的渠道在前端就不再是必配项。如果那个渠道其实每次都要（新账号注册的邮箱验证码就是），把"这一次可能不跑"写成 `run()` 里的 `StepResult.skip(...)`，不要写成 `when()`——两者对引擎等价，对渠道推导不等价。`platforms/kiro/steps.py::AwsVerifyEmail` 是这个写法并在 docstring 里写明了原因。改完用本文末尾的 `describe_requirements` 命令核对，不要靠看代码推。
- `requires` 写能力名（`phone` / `otp` / `link` / `captcha`），不是渠道名；步骤里用 `sc.require("phone")` 取实例。
- 失败一律抛 `core/errors.py` 里的分类异常。未分类的 `RuntimeError` = `unknown`，既不原地重试也不补投，只会占掉一个名额——这几乎总是错的。
- 步骤自己 `retry` 只在默认表不合适时声明（例：`MaybePhoneVerify` 只对 `resource_rejected` 原地重试，因为等不到码时页面已经不在原处）。
- 占了外部资源的步骤必须实现 `compensate()`，并且要幂等：引擎在失败、取消、回撤发起时都会调它。

## 运行时契约（不要绕过）

- 日志经 `sc.log()` / `sc.emit()`，它们走 `runtime.log`；不要 `print`，不要直接 `ctx.log_fn`。
- 需要人工时用 `sc.ask(InteractionRequest(...))`；不要往 `config.extra` 塞活对象。
- 中途占用外部资源后立刻 `sc.resource_refs[...] = ref` 并 `sc.checkpoint_now()`；成功释放后 `pop` 再 `checkpoint_now()`。`resource_refs["account"]` 只放 `email` / `submitted`。
- 长等待循环必须看 `sc.cancel`（它是可调用的 `CancellationToken`），并把它作为 `should_stop` 传给 provider。

## 号码租约

- 只经 `PhoneLease`：`acquire(should_stop=sc.cancel)` → `wait_code(should_stop=sc.cancel)` → `report_send_succeeded|report_send_failed|report_code_failed` → `release("success"|"rejected"|"unused")`。换号用 `swap(should_stop=sc.cancel)`。
- 一次尝试只有一个 `PhoneVerification` 实例（`CapabilityResolver` 按能力 + 解析出的 key 缓存），不要自己 new。
- 独占验证窗口锁由 `PhoneVerification` 管，步骤不碰 `_SMS_VERIFY_WINDOW_LOCK`。

## 干扰处理

- Turnstile / 全页拦截不是步骤，是 `ObstacleHandler`。`TurnstileObstacleHandler` 的 `detect_before_steps` / `poll_before_steps` / `settled(page, step)` 决定在哪些步骤前探测、等多久、什么算"已进入下一阶段"——`settled` 要按步骤区分（上一页残留的输入框不算下一阶段）。
- headless 下没有 solver 且页内点击失败会抛 `RiskControlError`，由任务层换代理补投；不要在 handler 里自己等人。

## 平台装配

- `platforms/<x>/plugin.py`：`@register` 的 `BasePlatform` 子类，类属性 `supported_executors` / `supported_identity_modes` 会自动种入平台能力表，不需要手写 DB 种子。
- 浏览器路径：`build_browser_registration_adapter()` 返回带 `step_flow_builder` / `obstacle_builder` / `engine_preference` / `result_mapper` 的 `BrowserRegistrationAdapter`；`otp_spec` 在 Step 路径也会被消费。
- 协议路径：`build_protocol_mailbox_adapter()`，worker 的 `run(..., should_stop=...)` 要接取消令牌。
- 新平台若引入新渠道 provider，走 `providers/<kind>/` 的 `@register_provider` + `infrastructure/provider_definitions_repository.py` 的内置 definition；registry 支持 `aliases=`。
- 站点会变的常量（action id、端点、选择器）集中到 `platforms/<x>/drift.py`，供 `scripts/platform_drift_check.py` 对照。

## 测试约定（这轮评审踩过的坑）

- fake 的签名必须和真实对象一致：日志 fake 若接受 `**kwargs` 而真实只收一个参数，测试就是假阳性。真实链路见 `tests/test_step_flow_wiring.py`。
- `_SMS_VERIFY_WINDOW_LOCK` 是 RLock，"锁已释放"只能从**另一个线程**探测（`tests/test_step_flow_wiring.py::lock_is_free_from_another_thread`）。
- fake 的 `PhoneVerification.acquire()` 不能把已释放的 lease 再交出去（见 `tests/test_cursor_steps.py::FakePhone`）。
- 每个新平台至少要有一个**走引擎**的用例（真实 `build_flow()` + `build_obstacles()` + 按阶段切换元素的假页面），参考 `tests/test_cursor_steps.py::TestCursorFlowThroughEngine`；只测 `Step.run()` 抓不到步骤之间的接线问题。
- 测试不出网：`tests/conftest.py` 拦截 socket 与带代理的 `requests`，不要绕它。

## 验证命令

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_step_engine.py tests/test_cursor_steps.py tests/test_step_flow_wiring.py
.venv/Scripts/python.exe -m pyflakes core/registration platforms/<x>
.venv/Scripts/python.exe -c "from application.platforms import PlatformsService; import json; print(json.dumps(PlatformsService().describe_requirements('<x>', executor_type='headless', identity_provider='mailbox'), ensure_ascii=False, indent=2))"
```

最后一条会打印平台推导出的渠道需求（required / conditional）与当前解析到的 provider——用它核对 `requires` 与 `when` 写得对不对，不要手写这份清单。
