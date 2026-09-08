---
name: platform-drift-check
description: "Detect and fix site drift for an existing registration platform (rotated server action ids, changed endpoints, renamed form fields) by diffing a freshly captured HAR / interaction log against the platform's declared drift surface. Use when a platform that used to work starts failing with site_changed / unknown errors, or after the target site redeploys."
---

# 平台漂移检查

目标：把"站点又变了"从翻 DevTools 变成一次可重复的对照。输入是一份**新录**的流量，输出是哪些常量需要改。

先读 `step-engine-conventions`。

---

## 前置

- 平台有 `platforms/<x>/drift.py`（声明 action id、预期端点、按阶段的选择器）。没有就先照 `platforms/cursor/drift.py` 补一份，再继续。
- 一份新录的 HAR 1.2。来源二选一：
  - Anything Analyzer：新建 session → 在内嵌浏览器里**完整走一遍注册**（含提交表单、密码、验证码；能触发手机验证就走到手机页）→ Inspector 底栏「导出 HAR」。同一 session 用 MCP 工具 `get_interactions` 取交互记录另存为 JSON（可选，但选择器检查依赖它）。
  - 或用它的 MITM 代理抓我们自己 headed 运行的流量。
- HAR 里有明文 token 与请求体，`.gitignore` 已排除 `*.har`；放在仓库外或 `data/` 下，用完删。

## 步骤

### 1. 跑对照

```bash
.venv/Scripts/python.exe scripts/platform_drift_check.py --platform <x> --har <capture.har> --interactions <interactions.json>
```

退出码 1 表示有漂移。报告分三段：

- **server action id**：`[已知]` 不用动；`[已变更]` 是按表单字段猜出属于哪一步、但 id 换了；`[未知]` 是猜不出步骤的新 id（多半是录制里多点了别的东西，先看它的状态码与请求体再决定）。`steps_not_seen` 列出这次录制没覆盖的步骤——不是漂移，是录得不全。
- **端点**：`预期但未出现` 要么流程变了、要么录得不全；`已知主机上的未知端点` 是站点新加的调用，判断它是否进了注册链路（看 HAR 里它的时序与响应）。
- **选择器**：`可能失效` 表示交互记录里没有任何元素能匹配该选择器；`没有任何选择器覆盖的输入框` 是站点新加的字段。注意本工具只认 `tag[attr="v"]` / `tag[attr*="v"]` 写法，其它写法列在"无法判定"里，要人工看。

### 2. 改代码

- action id：改 `platforms/<x>/core.py` 的 `_KNOWN_ACTIONS`；删掉 `data/.<x>_action_ids.json`（磁盘缓存里的 `confirmed` 会被优先使用）。运行期本来就有 `discover()` 自愈，但已知表更新后首轮就不必轮询。
- 端点：改协议 worker 里对应的 URL 常量，并同步 `drift.py` 的 `EXPECTED_ENDPOINTS` / `OPTIONAL_ENDPOINTS`。
- 选择器：改 `platforms/<x>/steps.py` / `obstacles.py` / `dom.py` 里的常量。优先 `input[name="…"]` / `input[type="…"]` / `input[autocomplete="…"]` 这类按语义属性的选择器；不要抄交互记录给出的 `nth-of-type` 路径。改完同步 `drift.py` 的 `SELECTORS_BY_STAGE`（它应当直接 import 代码里的常量，不要复制字符串）。
- 新增字段：若站点多了一个必填输入框，是新增/修改一个 Step 的问题，不是改选择器——转 `platform-from-capture` 的"步骤划分"一节。

### 3. 验证

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_<x>_*.py tests/test_platform_drift_check.py
.venv/Scripts/python.exe scripts/platform_drift_check.py --platform <x> --har <capture.har> --interactions <interactions.json>
```

第二条应当退出码 0（`steps_not_seen` 之外没有红项）。协议路径的 action id 变更还要在 `tests/test_<x>_protocol_actions.py` 里更新对应的假响应；浏览器路径的选择器变更要让 `tests/test_<x>_steps.py` 的假页面元素跟着改。

### 4. 收尾

- 删除 HAR 与交互记录导出；在 Analyzer 里删除该 session。
- 把这次漂移写进提交信息（哪一步、旧 id → 新 id / 旧选择器 → 新选择器），方便下次对比。
- 交给 `trellis-check` 做常规质量检查。

## 什么时候不该用它

- 失败分类是 `transient` / `resource_rejected` / `provider`：那是渠道或网络问题，先走 `failure-triage`。
- 平台还没有 Step 化、也没有 `drift.py`：先按 `platform-from-capture` 把常量集中出来，再谈检查。
