---
name: platform-from-capture
description: "Scaffold a new registration platform (browser Step flow and/or protocol worker) from an Anything Analyzer capture: session brief, ProtocolSpec, interaction log. Use when adding a platform under platforms/<x>/ or when migrating a legacy platform onto the Step engine. Produces steps/obstacles/plugin/drift modules plus engine-level tests; does not replace real-site verification."
---

# 从抓包会话搭一个平台

先读 `step-engine-conventions`。这份 skill 只管"把录到的东西翻译成我们的抽象"，约定本身不在这里重复。

---

## 前置

- Anything Analyzer 的 MCP Server 在线，且有一个**完整走通**注册流程的 session（能触发的分支——密码页、手机验证、验证码——尽量都走一遍；走不到的分支之后靠 `when()` 守卫兜住）。
- 知道目标平台要支持哪几种执行器（`protocol` / `headless` / `headed`）与身份模式（`mailbox` / `oauth_browser`）。
- 录制用的内核（Electron 或 Cloak，都是 Chromium 系）与我们实际会用的内核可能不同（Cursor 用 Camoufox）。录到的是流程与协议，不是最终指纹表现；最后必须在我们的内核上实跑一次。

## 步骤

### 1. 取材料（只取，不急着写）

按顺序调 MCP 工具，每一步都记下 session id：

1. `get_session_brief` —— 场景、鉴权链、端点清单。
2. `get_protocol_spec` —— `endpoints`（URL 模板、方法、参数、`requestExample`）、`authChain`（token/cookie 从哪来、放哪里）、`flows`（端点顺序）、`storage`（哪个 cookie 是会话）。协议路径以它为主。
3. `get_interactions` —— 按 `url` 变化切成页面阶段；每个阶段里的 `input` 事件给出字段（看 `attributes.name / type / autocomplete / placeholder`），`click` 事件给出提交按钮。浏览器路径以它为主。
4. `get_storage` —— 成功后哪个 cookie / localStorage 键代表登录态，就是 `ExtractSessionToken` 一类步骤要取的东西。
5. 对报告里引用的 `[#seq]`，用 `get_request_by_seq` 看原始请求，尤其是失败信号（4xx、`error` 字段、重定向到哪）。

把上面整理成一页"阶段表"：`阶段名 → 进入该阶段的 URL 特征 → 需要填的字段 → 提交动作 → 成功后去哪`。这张表就是 Step 划分。

### 2. 决定路径

- 站点是普通表单 + API（能从 ProtocolSpec 复现整条链）→ 协议路径，参考 `platforms/cursor/core.py` 与 `platforms/cursor/protocol_mailbox.py`。
- 有 Turnstile / 强指纹校验 / 客户端加密 → 浏览器 Step 路径，参考 `platforms/cursor/steps.py`。
- 两条都做时先做浏览器路径（更稳），协议路径作为快速通道。

### 3. 浏览器 Step 路径要写的文件

- `platforms/<x>/dom.py`：选择器常量与 DOM 辅助函数（点 Continue、找 token、Turnstile 判定）。选择器优先 `input[name="…"]` / `input[type="…"]` / `input[autocomplete="…"]`，**不要**抄交互记录里的 `nth-of-type` 路径或动态 class。
- `platforms/<x>/steps.py`：每个阶段一个 `Step`。导航步骤 `anchor = True`；可选阶段实现 `when()`；占资源的步骤实现 `compensate()`；`writes` 要包含最终结果键（至少 `token` / `password` / `resolved_email` 之类由 `result_mapper` 消费的）。`build_flow()` 返回 `Flow(name, steps, initial_keys=("email", "password"), result_keys=(...))`。
- `platforms/<x>/obstacles.py`：`build_obstacles()`。有 Turnstile 就装配 `TurnstileObstacleHandler`，`detect_before_steps` / `poll_before_steps` 只列提交动作之后的步骤，`settled(page, step)` 按步骤给出"已进入下一阶段"的选择器。
- `platforms/<x>/plugin.py`：`@register` 的平台类，`build_browser_registration_adapter()` 返回 `BrowserRegistrationAdapter(result_mapper=..., step_flow_builder=..., obstacle_builder=..., engine_preference=..., otp_spec=...)`。
- `platforms/<x>/drift.py`：照 `platforms/cursor/drift.py`，**import** 上面文件里的常量而不是复制。

### 4. 协议路径要写的文件

- `platforms/<x>/core.py`：HTTP 客户端。每个 `ProtocolSpec.endpoints` 条目一个方法；鉴权链按 `authChain` 传递；失败按 `core/errors.py` 分类（站点拒资源 → `ResourceRejectedError`，等码/过期 → `TransientError`，接口结构对不上 → `SiteChangedError`，缺配置 → `ConfigError`）。会轮换的 id 走类似 `CursorActionIds` 的"已知表 + 磁盘缓存 + 现场发现"，不要只写死。
- `platforms/<x>/protocol_mailbox.py`：worker，`run(..., otp_callback, captcha_solver, phone, should_stop)`；等码、等挑战都要透传 `should_stop`。
- `plugin.py` 里 `build_protocol_mailbox_adapter()`，`use_captcha` / `use_phone` 按实际声明（它们驱动 `describe_requirements` 的渠道推导）。

### 5. 渠道与 provider

- 需要新的短信 / 邮箱 / 打码 provider：`providers/<kind>/<name>.py` 用 `@register_provider(kind, driver_type)` 并实现 `from_config`；在 `infrastructure/provider_definitions_repository.py` 的内置 definition 列表加一条（字段、auth 类别、默认 enabled）。
- HeroSMS 系服务码映射在 `providers/sms/herosms.py::HERO_SMS_SERVICES`，新平台若用它接码要加一行。

### 6. 测试

- `tests/test_<x>_steps.py`：每个 Step 的关键分支直接调 `run()`；再加一个 `TestXFlowThroughEngine`，用按阶段切换元素的假页面 + 真实 `build_flow()` / `build_obstacles()` 跑 `StepRunner`（照 `tests/test_cursor_steps.py::ScriptedPage`）。至少覆盖：成功路径、等不到验证码、可选阶段被跳过。
- 协议路径：`tests/test_<x>_protocol_actions.py`，网络全部 mock（`tests/conftest.py` 会拦出网）。
- 跑 `describe_requirements`（命令见 `step-engine-conventions`）核对渠道推导：required / conditional 要与阶段表一致。

### 7. 实跑与收尾

- 用我们自己的内核 headed 跑一次 count=1；看任务时间线是否出现 `step_started` / `step_ok`，失败时现场包是否可下载。
- 在 Analyzer 里删除该 session（里面有明文 token）。
- 交给 `trellis-check`。

## 常见误判

- ProtocolSpec 的 `reproduction.code` 是通用 requests 脚本，只能当参考，不要直接放进仓库。
- 交互记录看不到跨域 iframe 内的操作（Turnstile 里的点击），只能从网络侧看到挑战出现的时机——按"挑战出现"处理即可。
- `flows` 里的顺序是这次录制的顺序，可选分支（手机验证）不一定出现；步骤要用 `when()` 表达"可能出现"。

---

## 迁移一个旧平台（整段式 → Step）

仓库里 8 个有浏览器路径的平台已全部迁完，这一节留给外部插件和以后新接的老代码。
`platforms/chatgpt/` 是最大的一例（3936 行的 `browser_register.py`），按这个顺序做，
每一步都能单独跑绿、单独提交。平台只有一两百行时（`platforms/grok/`、`platforms/trae/`
就是这一档）第 1、2 步并成一步做即可，直接照着 `dom.py` + `steps.py` 两件套写：

1. **删死代码**。先用可达性分析算出哪些顶层定义从入口类不可达（`ast` 扫一遍建调用图即可），
   别凭印象删。注意“活函数里的死块”（算了却没人读的局部变量）分析看不到，删掉它们后要再跑一遍取不动点。
2. **纯搬运拆模块**：`dom.py`（选择器 + 逐页动作）、大块页面逻辑各自成模块。
   这一步**不改任何测试**——测试一行不改还全绿，就是“零行为变化”的证明。
   旧 worker 暂时保留，并再导出测试打补丁用的名字。
3. **类型化错误 + 取消穿透**。这是增量的：`RegistrationFailure` 继承 `RuntimeError`，
   所以旧路径的 `except RuntimeError` 仍然接得住，两边同时可跑。
4. **写 `steps.py` / `obstacles.py`，切 `plugin.py`**。切换点就一个字段，回滚 = 改回去。
5. **删旧文件，重写钉它内部名字的测试，补 `drift.py` 与文档**。
   旧文件里如果还混着别的功能（`platforms/windsurf/browser_register.py` 里有 800 行
   Stripe / 支付宝结账），只搬注册那一段，文件留着。

迁移途中几条反复踩到的：

- **`requires` 的渠道级别由"有没有重写 `when()`"决定**（`core/requirements.py:60`）。
  按页面阶段做守卫的步骤会把渠道从 required 降成 conditional——如果那个渠道其实每次
  都要（新账号注册的邮箱验证码就是），把"这次可能不跑"写成 `run()` 里的
  `StepResult.skip()`，别写成 `when()`。`platforms/kiro/steps.py::AwsVerifyEmail` 是这个写法。
- **OTP 步骤用 `RetryPolicy(max_attempts=1)`**。原地重试会再向邮箱要一封**新**邮件，
  站点却只发过一封，结果是白等满一个 OTP 超时。让这次尝试结束，由任务层换邮箱补投。
- **手写的"重试 N 次"循环就是 `RetryPolicy`**，"睡 5 秒再来一次"就是 `backoff_seconds`。
  要在最后一次给出不同的错误分类（比如"这个邮箱域名被拒了"），把次数记在黑板上
  （只声明 `writes`，`Flow.validate` 不允许 `reads` 里出现本步骤自己才写的键）。
- **`compensate()` 里抛的异常会被引擎吞掉**（只 emit 一条 warning），别指望它能改变失败分类。
- **`page.on("response")` 这类监听挂在锚点步骤**，用 `sc.handles` 里的键做幂等标记，
  回撤重跑时清空已捕获的内容而不是重复挂。`platforms/trae/steps.py`、
  `platforms/windsurf/steps.py` 是这个写法。


常见坑：旧实现的重试循环、“卡住”计数器、`except Exception: return None`
都要**删掉**而不是搬进 Step：它们分别对应 `RetryPolicy`、回撤预算、错误分类，
搬过去就是两套机制打架。
