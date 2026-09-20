---
name: failure-triage
description: "Triage a failed or stuck registration task using its structured events, failure artifact bundles (screenshot / HTML / same-origin network / console) and checkpoints. Use when a task shows failed, interrupted or waiting_human, when a step keeps retrying, or when numbers/mailboxes seem to leak. Routes site_changed findings to platform-drift-check."
---

# 注册任务失败排查

输入：任务 id（形如 `task_<ms>_<hex>`）。所有材料都是我们自己的产物，不依赖外部工具。

先读 `step-engine-conventions` 里的错误分类一节。

---

## 1. 先看时间线，不要先看截图

```bash
curl -s http://127.0.0.1:8000/api/tasks/<task_id>/events?limit=500
```

（端口与鉴权以本地 `main.py` / `.env` 为准；有 `APP_PASSWORD` 时带 Bearer。）

事件的 `type` 就是引擎的结构化事件，`detail.step` 与 `detail.attempt_id` 标出属于哪次尝试的哪一步：

- 步骤：`step_started` / `step_ok` / `step_failed` / `step_retry` / `step_rewind` / `step_skipped`
- 干扰：`obstacle_detected` / `obstacle_resolved` / `obstacle_unresolved` / `obstacle_failed` / `obstacle_detect_failed`
- 辅助浏览器：`aux_browser_opened` / `aux_browser_closed`（某步开了第二个会话，现场包里会多出 `screenshot-<name>.png`）
- 其它：`compensate_failed` / `checkpoint_failed` / `waiting_human`

这份清单会随引擎增补，**以代码为准**：

```bash
grep -rn 'emit(' core/registration/steps.py core/registration/obstacles.py
```

看到清单外的 type 先去对一遍代码再怀疑接线。

如果时间线里**只有纯文本、没有这些 type**，先怀疑日志接线（`StepContext._log_fn` 没拿到 `runtime.log`），这是比业务失败更优先的问题。

## 2. 按 `step_failed.detail.category` 分流

- `transient`（等码超时、临时无号、验证码过期）：看任务层有没有补投（时间线里"该失败可补投"）。没有补投多半是预算用尽，看 `_execute_register_task` 的预算日志"可重试类失败最多补投 N 次"。连续同一处 transient → 渠道侧问题，转第 4 步。
- `resource_rejected` / `risk_control`：号码被拒、Turnstile 过不去。看 `obstacle_*` 事件序列：`detected` 后没有 `resolved` 且 headless → 没有可用 solver 或 solver 失败；看代理是否被封（同一代理连续失败）。
- `provider`：渠道接口错误（余额、限流、HTTP 5xx）。看 provider 设置与余额，不是代码问题。
- `site_changed`：选择器 / 接口对不上。**转 `platform-drift-check`**，不要在这里修。
- `config`：缺 provider、缺 key、内核未安装。任务已停批，按错误文案改配置即可。
- `unknown`：某处抛了未分类的 `RuntimeError`。这本身是缺陷——找到抛出点换成 `core/errors.py` 的分类，否则它既不重试也不补投。

## 3. 打开现场包

`step_failed.detail.artifacts` 是目录：`data/attempts/<task_id>/<attempt_id>/<Step>-<attempt>/`，也可经接口下载：

```bash
curl -s http://127.0.0.1:8000/api/tasks/<task_id>/attempts/<attempt_id>/artifacts
curl -s -o page.html.gz http://127.0.0.1:8000/api/tasks/<task_id>/attempts/<attempt_id>/artifacts/<bundle>/page.html.gz
```

- `meta.json`：`category` / `error` / `page_url` / 脱敏后的 `state` / `channels_used`。先确认 `page_url` 是不是预期页面——不是的话是流程时序问题（提前进了下一步或回撤后没回到 anchor）。
- `screenshot.png`、`page.html.gz`：看页面到底停在哪、有没有 Turnstile / 全页拦截 / 报错文案。
- `network.json`：只含同源 XHR/fetch，看 4xx / `error` 字段 / 重定向；这是判断"站点拒绝"还是"我们没等到"的最直接证据。
- `console.json`：前端报错。

现场包按任务保留最近 20 个任务目录（`prune_attempt_artifacts`），老任务的现场可能已被清。

## 4. 渠道与资源

- 号码：时间线里找"已成功租到号码" / "已释放未使用号码" / "短信验证成功"。租了没释放 → 看该步骤的 `compensate` 有没有跑（`compensate_failed` 事件）。多个任务连续在租号前卡住不动 → 验证窗口锁泄漏，日志里会有"排队等待"与 1500s 后的 `ProviderError`；重启进程可解，但要找出没释放的那次。
- 邮箱：等码超时看 provider 侧有没有收到信（`get_current_ids` 日志）、`before_ids` 是否把新邮件当旧邮件过滤了。
- 人工介入：`waiting_human` 事件后任务状态应为 `waiting_human`；`GET /api/tasks/<id>/interactions` 看 pending 项。重启后残留的 pending 会被标 cancelled。

## 5. 检查点（重启回收相关）

检查点只在服务端 `result_json.attempts` 里，接口不下发。用 Python 读：

```bash
.venv/Scripts/python.exe -c "from sqlmodel import Session; from core.db import TaskModel, engine; import json; s=Session(engine); t=s.get(TaskModel,'<task_id>'); print(json.dumps(t.get_result().get('attempts'), ensure_ascii=False, indent=2))"
```

- `phase: running` 且 `resource_refs.phone` 仍在、`done` 不为 true → 这次尝试在等码时被杀，重启时 `reclaim_interrupted_attempts` 会退号；日志里找"已回收中断尝试占用的手机号"。
- `done: true` 的尝试不会被回收——它在进程内已收尾（成功、失败或取消都算）。
- `state` 里不应出现 `password` / `token` / `phone_number`；出现了就是 `sanitize_state` 被绕过。

## 6. 复现与修复

- 步骤级问题：在 `tests/test_<x>_steps.py` 用假页面复现该分支（先写红测试）。
- 流程级时序问题（提前放行、回撤循环）：用 `TestXFlowThroughEngine` 的 `ScriptedPage` 复现阶段切换。
- 改完跑 `tests/test_step_flow_wiring.py` 确认接线未被破坏，再交 `trellis-check`。
