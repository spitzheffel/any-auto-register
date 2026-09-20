"""平台漂移检查：拿一份新录的 HAR（和可选的交互记录）对照平台代码里写死的东西。

用法::

    python scripts/platform_drift_check.py --platform cursor --har capture.har
    python scripts/platform_drift_check.py --platform cursor --har capture.har --interactions interactions.json --json

HAR 由 Anything Analyzer 的 Inspector「导出 HAR」得到；交互记录是它 MCP 工具
``get_interactions`` 的返回（JSON 数组，每项含 ``attributes``），或同等结构。
平台需要提供 ``platforms/<name>/drift.py``（见 ``platforms/cursor/drift.py``）。

输出三类结论：
- action id：HAR 里出现的 server action id 与代码 / 磁盘缓存里已知的是否一致；
- 端点：预期端点有没有出现、有没有出现代码不认识的新端点；
- 选择器：代码依赖的输入框选择器在交互记录里能否匹配到元素。
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# HAR / 交互记录读取
# ---------------------------------------------------------------------------

def load_har_entries(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = (data.get("log") or {}).get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise SystemExit(f"不是 HAR 1.2 文件（缺少 log.entries）: {path}")
    return [entry for entry in entries if isinstance(entry, dict)]


def load_interactions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("interactions", "events", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise SystemExit(f"交互记录应是 JSON 数组（或含 interactions/events/items 键的对象）: {path}")
    return [item for item in data if isinstance(item, dict)]


def _header(entry_request: dict, name: str) -> str:
    for header in entry_request.get("headers") or []:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value") or "")
    return ""


def _post_text(entry_request: dict) -> str:
    post = entry_request.get("postData") or {}
    text = str(post.get("text") or "")
    if not text and isinstance(post.get("params"), list):
        text = "&".join(f"{p.get('name')}={p.get('value')}" for p in post["params"])
    return text


# ---------------------------------------------------------------------------
# 三项检查
# ---------------------------------------------------------------------------

def check_action_ids(entries: list[dict], surface) -> dict[str, Any]:
    """HAR 里的 next-action 与已知 id 对照，按表单字段猜每个 id 属于哪一步。"""
    observed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        request = entry.get("request") or {}
        url = urlsplit(str(request.get("url") or ""))
        if request.get("method", "").upper() != "POST" or url.netloc != surface.ACTION_REQUEST_HOST:
            continue
        if not url.path.startswith(surface.ACTION_REQUEST_PATH):
            continue
        action_id = _header(request, surface.ACTION_HEADER)
        if not action_id:
            continue
        body = _post_text(request).lower()
        step = ""
        for candidate, fields in surface.ACTION_FIELD_HINTS.items():
            if any(re.search(rf'name="(?:_\d+_)?{re.escape(field)}"', body) or f"{field}=" in body for field in fields):
                step = candidate
                break
        status = int(((entry.get("response") or {}).get("status")) or 0)
        item = observed.setdefault(action_id, {"steps": set(), "statuses": set(), "count": 0})
        item["count"] += 1
        item["statuses"].add(status)
        if step:
            item["steps"].add(step)

    known = surface.known_actions()
    cached: dict[str, str] = {}
    # 没有 server action 层的平台（如 ChatGPT）返回 None，不必去猜缓存路径
    cache_path = surface.action_cache_path()
    if not cache_path:
        return {"observed": [], "steps_not_seen": [], "known": {}, "cached": {}}
    try:
        cached = dict((json.loads(Path(cache_path).read_text(encoding="utf-8")) or {}).get("confirmed") or {})
    except Exception:
        cached = {}

    findings: list[dict[str, Any]] = []
    for action_id, item in observed.items():
        step = next(iter(item["steps"]), "")
        in_code = next((k for k, v in known.items() if v == action_id), "")
        in_cache = next((k for k, v in cached.items() if v == action_id), "")
        if in_code or in_cache:
            verdict = "known"
        elif step and (known.get(step) or cached.get(step)):
            verdict = "changed"
        else:
            verdict = "new"
        findings.append(
            {
                "action_id": action_id,
                "guessed_step": step,
                "verdict": verdict,
                "known_as": in_code or in_cache,
                "statuses": sorted(item["statuses"]),
                "count": item["count"],
            }
        )
    missing = [step for step in known if not any(f["guessed_step"] == step for f in findings)]
    return {"observed": findings, "steps_not_seen": missing, "known": known, "cached": cached}


def check_endpoints(entries: list[dict], surface) -> dict[str, Any]:
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        request = entry.get("request") or {}
        url = urlsplit(str(request.get("url") or ""))
        if not url.netloc:
            continue
        seen.add((str(request.get("method") or "GET").upper(), url.netloc, url.path))

    def matched(expected: tuple[str, str, str]) -> bool:
        method, host, prefix = expected
        return any(m == method and h == host and p.startswith(prefix) for m, h, p in seen)

    interesting_hosts = {host for _, host, _ in surface.EXPECTED_ENDPOINTS} | {
        host for _, host, _ in surface.OPTIONAL_ENDPOINTS
    }
    known_prefixes = [(m, h, p) for m, h, p in (*surface.EXPECTED_ENDPOINTS, *surface.OPTIONAL_ENDPOINTS)]
    unknown = sorted(
        f"{m} {h}{p}"
        for m, h, p in seen
        if h in interesting_hosts
        and not any(m == km and h == kh and p.startswith(kp) for km, kh, kp in known_prefixes)
        and not _looks_static(p)
    )
    return {
        "missing_expected": [f"{m} {h}{p}" for m, h, p in surface.EXPECTED_ENDPOINTS if not matched((m, h, p))],
        "optional_seen": [f"{m} {h}{p}" for m, h, p in surface.OPTIONAL_ENDPOINTS if matched((m, h, p))],
        "unknown_on_known_hosts": unknown,
    }


def _looks_static(path: str) -> bool:
    return bool(re.search(r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff2?|ttf|map|webp)$", path)) or "/_next/static/" in path


# ---- 选择器匹配：只支持我们代码里实际用到的写法 ---------------------------------

_SIMPLE = re.compile(r'^\s*(?P<tag>[a-zA-Z][\w-]*)?(?P<attrs>(?:\[[^\]]+\])*)\s*$')
_ATTR = re.compile(r'\[(?P<name>[\w:-]+)(?:(?P<op>\*?=)"?(?P<value>[^"\]]*)"?)?\]')


def _element_matches(selector: str, element: dict) -> bool | None:
    """True/False 表示可判定；None 表示这个选择器写法本工具不认识。"""
    match = _SIMPLE.match(selector)
    if not match:
        return None
    tag = (match.group("tag") or "").lower()
    if tag and str(element.get("tag") or "").lower() != tag:
        return False
    attrs = {str(k).lower(): str(v) for k, v in (element.get("attributes") or {}).items()}
    for attr in _ATTR.finditer(match.group("attrs") or ""):
        name = attr.group("name").lower()
        if name not in attrs:
            return False
        if attr.group("op") is None:
            continue
        value = attr.group("value") or ""
        if attr.group("op") == "*=":
            if value not in attrs[name]:
                return False
        elif attrs[name] != value:
            return False
    return True


def _interaction_elements(interactions: list[dict]) -> list[dict]:
    elements: list[dict] = []
    for item in interactions:
        attributes = item.get("attributes")
        if isinstance(attributes, str):
            try:
                attributes = json.loads(attributes)
            except Exception:
                attributes = {}
        tag = str(item.get("tag_name") or item.get("tagName") or "").lower()
        if not tag and isinstance(attributes, dict):
            tag = "input" if attributes.get("type") or attributes.get("name") else ""
        elements.append({"tag": tag, "attributes": attributes or {}, "url": item.get("url", "")})
    return elements


def check_selectors(interactions: list[dict], surface) -> dict[str, Any]:
    elements = _interaction_elements(interactions)
    result: dict[str, Any] = {"stages": {}, "unsupported": [], "uncovered_inputs": []}
    all_selectors: list[str] = []
    for stage, selector_groups in surface.SELECTORS_BY_STAGE.items():
        stage_result: list[dict[str, Any]] = []
        for group in selector_groups:
            alternatives = [part.strip() for part in group.split(",") if part.strip()]
            all_selectors.extend(alternatives)
            hits: list[str] = []
            undecidable: list[str] = []
            for alt in alternatives:
                for element in elements:
                    verdict = _element_matches(alt, element)
                    if verdict is None:
                        undecidable.append(alt)
                        break
                    if verdict:
                        hits.append(alt)
                        break
            if undecidable:
                result["unsupported"].extend(undecidable)
            stage_result.append({"selector": group, "matched_by": sorted(set(hits)), "ok": bool(hits)})
        result["stages"][stage] = stage_result

    # 录到了输入框，却没有任何代码里的选择器能匹配 → 站点新加的字段
    for element in elements:
        if element["tag"] != "input":
            continue
        attrs = element["attributes"]
        if str(attrs.get("type", "")).lower() in {"hidden", "submit", "button", "checkbox"}:
            continue
        if not any(_element_matches(sel, element) for sel in all_selectors):
            result["uncovered_inputs"].append(
                {k: attrs.get(k) for k in ("name", "type", "placeholder", "autocomplete", "id") if attrs.get(k)}
                | {"url": element["url"]}
            )
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render(platform: str, actions: dict, endpoints: dict, selectors: dict | None) -> str:
    lines = [f"# {platform} 漂移检查", ""]

    lines.append("## server action id")
    if not actions["observed"]:
        lines.append("- HAR 里没有带 next-action 的 POST；确认录制时真的提交过表单。")
    for item in actions["observed"]:
        tag = {"known": "已知", "changed": "已变更", "new": "未知"}[item["verdict"]]
        step = item["guessed_step"] or "?"
        extra = f"（代码里对应 {item['known_as']}）" if item["known_as"] else ""
        lines.append(f"- [{tag}] {step}: {item['action_id']} 状态 {item['statuses']} ×{item['count']}{extra}")
    if actions["steps_not_seen"]:
        lines.append(f"- 本次录制没有覆盖的步骤: {', '.join(actions['steps_not_seen'])}")
    changed = [i for i in actions["observed"] if i["verdict"] == "changed"]
    if changed:
        lines.append("- 需要更新 `platforms/<platform>/core.py` 的 `_KNOWN_ACTIONS`，并删除磁盘缓存让 discover() 重来。")
    lines.append("")

    lines.append("## 端点")
    lines.append(
        "- 预期但未出现: " + (", ".join(endpoints["missing_expected"]) or "无")
    )
    lines.append("- 可选端点出现: " + (", ".join(endpoints["optional_seen"]) or "无"))
    lines.append(
        "- 已知主机上的未知端点: " + (", ".join(endpoints["unknown_on_known_hosts"]) or "无")
    )
    lines.append("")

    if selectors is not None:
        lines.append("## 选择器（对照交互记录）")
        for stage, items in selectors["stages"].items():
            for item in items:
                mark = "OK" if item["ok"] else "可能失效"
                lines.append(f"- [{mark}] {stage}: {item['selector']}")
        if selectors["uncovered_inputs"]:
            lines.append("- 录到了但没有任何选择器覆盖的输入框:")
            for attrs in selectors["uncovered_inputs"]:
                lines.append(f"    - {attrs}")
        if selectors["unsupported"]:
            lines.append(f"- 本工具无法判定的选择器写法: {sorted(set(selectors['unsupported']))}")
    return "\n".join(lines)


def run(platform: str, har: Path, interactions: Path | None) -> dict[str, Any]:
    try:
        surface = importlib.import_module(f"platforms.{platform}.drift")
    except ModuleNotFoundError as exc:
        raise SystemExit(f"平台 {platform} 没有 drift 声明（platforms/{platform}/drift.py）: {exc}") from exc

    entries = load_har_entries(har)
    actions = check_action_ids(entries, surface)
    endpoints = check_endpoints(entries, surface)
    selectors = check_selectors(load_interactions(interactions), surface) if interactions else None
    return {"platform": platform, "actions": actions, "endpoints": endpoints, "selectors": selectors}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--har", required=True, type=Path)
    parser.add_argument("--interactions", type=Path)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 而不是文本报告")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run(args.platform, args.har, args.interactions)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=sorted))
    else:
        print(render(report["platform"], report["actions"], report["endpoints"], report["selectors"]))

    drifted = (
        any(i["verdict"] != "known" for i in report["actions"]["observed"])
        or report["endpoints"]["missing_expected"]
        or (report["selectors"] and any(not i["ok"] for items in report["selectors"]["stages"].values() for i in items))
    )
    return 1 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
