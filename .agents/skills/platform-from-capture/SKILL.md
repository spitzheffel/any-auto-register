---
name: platform-from-capture
description: "Scaffold a new registration platform (browser Step flow and/or protocol worker) from an Anything Analyzer capture, reading the capture's deterministic MCP tools (filter_requests, get_session_enrichment, get_interactions, get_storage) instead of its LLM-generated report or ProtocolSpec. Use when adding a platform under platforms/<x>/ or when migrating a legacy platform onto the Step engine. Produces steps/obstacles/plugin/drift modules plus engine-level tests; does not replace real-site verification."
---

# 从抓包会话搭一个平台

先读 `step-engine-conventions`。这份 skill 只管"把录到的东西翻译成我们的抽象"，约定本身不在这里重复。

---

## 前置

- Anything Analyzer 的 MCP Server 在线，且有一个**完整走通**注册流程的 session（能触发的分支——密码页、手机验证、验证码——尽量都走一遍；走不到的分支之后靠 `when()` 守卫兜住）。
- 知道目标平台要支持哪几种执行器（`protocol` / `headless` / `headed`）与身份模式（`mailbox` / `oauth_browser`）。
- 录制用的内核（Electron 或 Cloak，都是 Chromium 系）与我们实际会用的内核可能不同（Cursor 用 Camoufox）。录到的是流程与协议，不是最终指纹表现；最后必须在我们的内核上实跑一次。

## 0. 只用确定性工具，不要读 AI 报告

Analyzer 的 MCP 工具分两类。**这份 skill 只用第一类。**

| 确定性（直接查 SQLite，零 LLM） | 给什么 |
|---|---|
| `get_session_enrichment` | 规则派生的 `sceneHints` / `authChain` / `storageDiff` / `streamingSeqs`；工具描述里明确写了 *without calling an LLM* |
| `filter_requests` | 按 method / domain / statusCode / statusRange / contentType / urlPattern / limit 过滤，请求体与响应体自动截到 2000 字符 |
| `get_request_by_seq` | 按 `#seq` 取**未截断**的完整请求，一次最多 20 条 |
| `get_request_detail` | 按 UUID 取单条完整请求 |
| `get_interactions` | 交互事件，可按 `type` 过滤；**`limit` 默认只有 100** |
| `get_interaction_summary` | 交互的人话步骤列表（纯字符串拼接，最多读 500 条事件） |
| `get_storage` | cookie / localStorage / sessionStorage 快照 |
| `get_hooks` | JS Hook 记录（加密调用、XHR / fetch 拦截） |

| 过 LLM（不要用） | 为什么 |
|---|---|
| `run_analysis` / `get_report` / `get_reports` | 让另一个模型替我们读请求，再把结论写成散文 |
| `get_protocol_spec` / `get_openapi` | ProtocolSpec 是**第二遍 LLM 读第一遍 LLM 写的报告**抽出来的，而且报告喂进抽取器之前先截断到 20K token。端点参数、鉴权细节、失败分支在这四层转述里最容易失真 |
| `get_session_brief` | 有 spec 时直接引用 spec，等于间接吃上面那条 |
| `chat_followup` | 同上 |

自己读原始请求既更准也更省，前提是**先过滤**。实测两份真实抓包（7 个站点、2545 条请求）：一个会话的全量是 **1.7–45 MB**，其中 **91–96% 的字节是响应体**（绝大部分又是 JS bundle）；而 POST / PUT / PATCH 只有 **7–225 条、24–792 KB**——那才是注册协议的骨架，比全量小两三个数量级。所以永远从 `filter_requests` 进，不要从 `get_requests` 进。

## 步骤

### 1. 取材料（只取，不急着写）

按顺序调，每一步都记下 session id：

1. `get_session_enrichment` —— 先看全貌。`sceneHints[].scene` 里应当出现 `registration`；`authChain[]` 每条是 `{source, credentialType, credential, consumers}`，也就是"凭据从哪来 → 什么类型 → 被哪些后续请求消费"，协议路径的鉴权链照它写即可（`credential` 已脱敏，只留前后各 8 字符）。`storageDiff.cookies.added` 里新增的那个通常就是登录态 cookie。

2. `filter_requests(sessionId, method="POST")` —— 注册流程的骨架，再按需补 `PUT` / `PATCH`。**4xx 要单独拿出来看**，那是站点的拒绝信号，决定了哪些失败该分类成 `ResourceRejectedError`。

3. `filter_requests(sessionId, domain="<目标站主域>", contentType="json")` —— 补齐 GET 里有用的那些（取配置、查可用性、拉 flow state）。

4. 对前两步挑出的关键 seq，用 `get_request_by_seq(sessionId, seqs=[...])` 取完整请求。`filter_requests` 把 body 截到 2000 字符，**签名字段和长 token 正好会被切掉**，凡是要照抄进代码的请求都得走这一步。一次最多 20 条。

5. `get_interactions(sessionId, type="input", limit=500)`，再来一次 `type="click"` —— 浏览器路径靠这两个。**必须显式传 `limit`**：默认 100，而一个完整注册流程的交互事件常有几百条。`type="hover"` 是采样的鼠标轨迹，纯噪音，不要取。

6. `get_storage` —— 成功后哪个 cookie / localStorage 键代表登录态，就是 `ExtractSessionToken` 一类步骤要取的东西；和第 1 步的 `storageDiff` 对照着看。

7. 只有在有客户端加密迹象时（请求体是密文、出现 `X-Signature` 一类头）才调 `get_hooks`。

把上面整理成一页"阶段表"：`阶段名 → 进入该阶段的 URL 特征 → 需要填的字段 → 提交动作 → 成功后去哪`。这张表就是 Step 划分。交互事件按 `url` 变化切页面阶段最省事；`get_interaction_summary` 可以先拿来对一眼顺序对不对。

### 2. 决定路径

- 站点是普通表单 + API（第 1 步取到的 POST 链能原样复现）→ 协议路径，参考 `platforms/cursor/core.py` 与 `platforms/cursor/protocol_mailbox.py`。
- 有 Turnstile / 强指纹校验 / 客户端加密 → 浏览器 Step 路径，参考 `platforms/cursor/steps.py`。
- 两条都做时先做浏览器路径（更稳），协议路径作为快速通道。

### 3. 浏览器 Step 路径要写的文件

- `platforms/<x>/dom.py`：选择器常量与 DOM 辅助函数（点 Continue、找 token、Turnstile 判定）。选择器怎么从交互记录里挑，见下面「交互记录里的选择器」。
- `platforms/<x>/steps.py`：每个阶段一个 `Step`。导航步骤 `anchor = True`；可选阶段实现 `when()`；占资源的步骤实现 `compensate()`；`writes` 要包含最终结果键（至少 `token` / `password` / `resolved_email` 之类由 `result_mapper` 消费的）。`build_flow()` 返回 `Flow(name, steps, initial_keys=("email", "password"), result_keys=(...))`。
- `platforms/<x>/obstacles.py`：`build_obstacles()`。有 Turnstile 就装配 `TurnstileObstacleHandler`，`detect_before_steps` / `poll_before_steps` 只列提交动作之后的步骤，`settled(page, step)` 按步骤给出"已进入下一阶段"的选择器。
- `platforms/<x>/plugin.py`：`@register` 的平台类，`build_browser_registration_adapter()` 返回 `BrowserRegistrationAdapter(result_mapper=..., step_flow_builder=..., obstacle_builder=..., engine_preference=..., otp_spec=...)`。
- `platforms/<x>/drift.py`：照 `platforms/cursor/drift.py`，**import** 上面文件里的常量而不是复制。

#### 交互记录里的选择器

录制器给每个事件两样东西，别混用：

- `selector` 是它**按优先级现算**的：稳定 `id` → `[data-testid|data-cy|data-test|aria-label|data-id]` → 唯一的 `tag.class` → `nth-child` 路径兜底。
  所以**拿到 `nth-child` 路径，等于录制器已经找不到任何稳定标识**——那是"自己从 `attributes` 拼一个"的信号，不是"照抄"的信号。
  它判定动态 id 的正则是 `[0-9a-f]{8,}` / `_\d+$` / `^:r\d+:` / `^ember\d+` / `^react-` / `^mui-`，能滤掉大部分构建期生成的名字，但站点自定义的随机前缀它认不出来，自己再看一眼。
- `attributes` 是**固定白名单**采集的：`id` `class` `name` `type` `href` `src` `placeholder` `role` `aria-label` `data-testid` `data-id` `data-action` `value` `title` `alt`。
  **`autocomplete` 不在白名单里**，抓包数据里永远没有它——不要写 `input[autocomplete="…"]` 然后指望从录制里验证。

各属性的出现率**站点之间波动极大**。下面是两份真实抓包（共 105 条 input 事件、7 个站点）的区间：

| 属性 | 出现率区间 | 能不能当选择器 |
|---|---|---|
| `class` | 100% | 不行，构建期会变 |
| `type` | 91–92% | 能，但单独用不唯一，要配合别的 |
| `id` | 57–100% | 能，前提是没被动态 id 正则判掉 |
| `value` | 51–85% | 不是选择器，是当时填进去的值（密码框是 `[MASKED]`） |
| `name` | 39–59% | 最稳，**但只有一半左右的输入框有** |
| `placeholder` | 20–69% | 能，但站点改文案就失效，多语言站点尤其危险 |
| `data-testid` | 19–39% | 有就最稳 |
| `aria-label` | 0–26% | 有就能用 |
| `autocomplete` | **恒为 0** | 采集不到，见上 |

**除了 `autocomplete` 恒缺、`class` 恒有，其余每一项在两份抓包之间都能差出两三倍。**所以这张表只能用来提醒"别假设某个属性一定在"，不能当固定优先级用——手头这个站到底有什么，`get_interactions(sessionId, type="input", limit=500)` 取回来自己数一遍再定。

定下来之后写成 `platforms/<x>/dom.py` 里的常量，并在 `drift.py` 里 import 它——站点改版由 `platform-drift-check` 抓。

### 4. 协议路径要写的文件

- `platforms/<x>/core.py`：HTTP 客户端。第 1 步挑出的每个端点一个方法；鉴权链按 `get_session_enrichment` 的 `authChain` 传递；失败按 `core/errors.py` 分类（站点拒资源 → `ResourceRejectedError`，等码/过期 → `TransientError`，接口结构对不上 → `SiteChangedError`，缺配置 → `ConfigError`）。会轮换的 id 走类似 `CursorActionIds` 的"已知表 + 磁盘缓存 + 现场发现"，不要只写死。
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

- `filter_requests` 把请求体和响应体截到 2000 字符，**截断是静悄悄的**。凡是要照抄进代码的请求（签名字段、长 token、完整的错误响应），一律用 `get_request_by_seq` 再取一遍完整的。
- 交互记录看不到跨域 iframe 内的操作（Turnstile 里的点击），只能从网络侧看到挑战出现的时机——按"挑战出现"处理即可。
- 交互与请求的顺序只是**这一次**录制的顺序，可选分支（手机验证）不一定出现过；步骤要用 `when()` 表达"可能出现"——但 `when()` 会把渠道从 required 降成 conditional，什么时候该改用 `StepResult.skip()` 见 `step-engine-conventions`。
- 密码框的 `input_value` 和 `attributes.value` 都是 `[MASKED]`，录制里看不到真实密码——这是对的，密码由我们自己按站点策略生成。
- 点击事件记的元素是 `closest('button, a[href], input, textarea, select, [role=button], …')` 往上找到的**祖先**，不一定是鼠标真正落在的那个节点（点按钮里的 `<span>` 记的是按钮）。

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
