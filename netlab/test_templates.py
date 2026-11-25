"""Reusable weak-network test templates and their local persistence.

Templates describe the operator workflow around a network condition. They do not
contain project-specific endpoints or packet filters; those are supplied by the
selected Motu Net target configuration at run time.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import uuid
from typing import Any, Mapping


TEMPLATE_SCHEMA_VERSION = 1
TEST_TIME_BUDGET_MINUTES = 60
_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_NETWORK_KEYS = {
    "delay_ms", "jitter_ms", "loss_pct", "bandwidth_kbps", "duplicate_pct",
    "reorder_pct", "reorder_ms", "duration_s", "blackout", "start_delay_s",
    "blackout_duration_s", "blackout_loop",
}
_DEFAULT_CASE_MINUTES = {
    # Standard short regression: 50 minutes including observation and result entry.
    "WN-01": 3, "WN-02": 5, "WN-03": 5, "WN-04": 6, "WN-05": 6,
    "WN-06": 6, "WN-07": 5, "WN-08": 6, "WN-09": 8,
    # Generic system regression: exactly 60 minutes at the full 15-case level.
    "CONN-01": 3, "CONN-02": 4, "CONN-03": 4, "CONN-04": 4,
    "WEAK-01": 4, "WEAK-02": 5, "WEAK-03": 5, "WEAK-04": 5,
    "PROTO-01": 4, "PROTO-02": 4,
    "EXTRA-01": 3, "EXTRA-02": 4, "EXTRA-03": 3, "EXTRA-04": 4, "EXTRA-05": 4,
}


def _text(value: Any, field: str, required: bool = True, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        raise ValueError(f"模板字段 {field} 必须是文本")
    value = value.strip()
    if required and not value:
        raise ValueError(f"模板字段 {field} 不能为空")
    if len(value) > maximum:
        raise ValueError(f"模板字段 {field} 不能超过{maximum}个字符")
    return value


def _template_id(value: Any) -> str:
    value = _text(value, "id", maximum=64).lower()
    if not _TEMPLATE_ID_RE.fullmatch(value):
        raise ValueError("模板 id 只能包含小写字母、数字、点、下划线和短横线")
    return value


def normalize_template(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a template envelope and return a fresh normalized dictionary."""
    if not isinstance(value, Mapping):
        raise ValueError("模板必须是对象")
    if value.get("schema_version") == TEMPLATE_SCHEMA_VERSION and isinstance(value.get("template"), Mapping):
        value = value["template"]
    if not isinstance(value, Mapping):
        raise ValueError("模板缺少 template 对象")
    template = {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "id": _template_id(value.get("id")),
        "name": _text(value.get("name"), "name", maximum=80),
        "description": _text(value.get("description", ""), "description", required=False),
        "source": _text(value.get("source", ""), "source", required=False, maximum=300),
        "cases": [],
    }
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("模板至少需要一条测试项")
    seen: set[str] = set()
    for index, raw in enumerate(cases, 1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"第 {index} 条测试项必须是对象")
        case_id = _text(raw.get("id"), f"第 {index} 条测试项 id", maximum=40)
        if case_id in seen:
            raise ValueError(f"测试项 id 重复：{case_id}")
        seen.add(case_id)
        network = raw.get("network", {})
        if not isinstance(network, Mapping):
            raise ValueError(f"测试项 {case_id} 的 network 必须是对象")
        unknown = set(network) - _NETWORK_KEYS
        if unknown:
            raise ValueError(f"测试项 {case_id} 存在未知网络字段：{', '.join(sorted(unknown))}")
        normalized_network = deepcopy(dict(network))
        for key in normalized_network:
            if key in {"blackout", "blackout_loop"}:
                if not isinstance(normalized_network[key], bool):
                    raise ValueError(f"测试项 {case_id} 的 {key} 必须是布尔值")
            elif key in {"duration_s", "start_delay_s", "blackout_duration_s"}:
                if isinstance(normalized_network[key], bool) or not isinstance(normalized_network[key], int):
                    raise ValueError(f"测试项 {case_id} 的 {key} 必须是整数秒")
            elif not isinstance(normalized_network[key], (int, float)) or isinstance(normalized_network[key], bool):
                raise ValueError(f"测试项 {case_id} 的 {key} 必须是数字")
        estimated_minutes = raw.get("estimated_minutes", _DEFAULT_CASE_MINUTES.get(case_id, 4))
        if (isinstance(estimated_minutes, bool) or not isinstance(estimated_minutes, int)
                or not 1 <= estimated_minutes <= 30):
            raise ValueError(f"测试项 {case_id} 的 estimated_minutes 必须是 1 到 30 的整数")
        template["cases"].append({
            "id": case_id,
            "group": _text(raw.get("group", "通用弱网"), f"测试项 {case_id} group", maximum=80),
            "title": _text(raw.get("title"), f"测试项 {case_id} title", maximum=160),
            "steps": _text(raw.get("steps"), f"测试项 {case_id} steps", maximum=4000),
            "expected": _text(raw.get("expected"), f"测试项 {case_id} expected", maximum=4000),
            "network": normalized_network,
            "estimated_minutes": estimated_minutes,
            "human_verdict": bool(raw.get("human_verdict", True)),
            "enabled": bool(raw.get("enabled", True)),
        })
    template["cases"] = [case for case in template["cases"] if case["enabled"]]
    if not template["cases"]:
        raise ValueError("模板不能全部禁用测试项")
    return template


def envelope(template: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": TEMPLATE_SCHEMA_VERSION, "template": normalize_template(template)}


def _case(case_id: str, group: str, title: str, network: Mapping[str, Any], steps: str,
          expected: str, estimated_minutes: int | None = None) -> dict[str, Any]:
    item = {"id": case_id, "group": group, "title": title, "network": dict(network),
            "steps": steps, "expected": expected, "human_verdict": True, "enabled": True}
    if estimated_minutes is not None:
        item["estimated_minutes"] = estimated_minutes
    return item


def standard_template() -> dict[str, Any]:
    """The short smoke/regression flow shipped with Motu Net."""
    cases = [
        _case("WN-01", "基线", "正常网络对照", {}, "登录并进入核心界面；完成一次查询或交互，记录正常耗时。", "连接与数据正常；操作只执行一次，作为后续弱网对照。"),
        _case("WN-02", "弱网络", "轻度弱网", {"delay_ms": 80.0, "jitter_ms": 15.0, "loss_pct": 1.0}, "重复登录、页面切换及一次提交操作。", "允许适度变慢；界面可用，提交不重复，数据最终一致。"),
        _case("WN-03", "弱网络", "高延迟", {"delay_ms": 600.0, "jitter_ms": 80.0}, "快速连续触发查询或交互，观察等待和返回顺序。", "有明确等待反馈；不会卡死、显示过期结果或重复提交。"),
        _case("WN-04", "弱网络", "波动与抖动", {"delay_ms": 220.0, "jitter_ms": 120.0, "loss_pct": 8.0, "reorder_pct": 2.0}, "持续进行核心操作并切换界面，观察超时与重试。", "短暂波动后可继续使用；失败提示明确，不丢失已确认状态。"),
        _case("WN-05", "弱网络", "高丢包", {"loss_pct": 20.0}, "执行一次重要操作并观察超时、重试及最终状态。", "不会无提示丢失操作；重试可控，服务端结果和界面一致。"),
        _case("WN-06", "弱网络", "低带宽", {"bandwidth_kbps": 256.0}, "打开资源较多的页面并执行核心操作。", "有合理加载反馈；不出现无限加载或重复请求，完成后内容正确。"),
        _case("WN-07", "断网恢复", "短时断网", {"blackout": True, "blackout_duration_s": 10, "loss_pct": 100.0}, "正在操作时断网，观察提示；恢复后再次查询状态。", "断网有明确提示；恢复后可重连，数据不重复、不丢失。"),
        _case("WN-08", "断网恢复", "延迟断网", {"blackout": True, "start_delay_s": 10, "blackout_duration_s": 10}, "启动后先在正常网络操作；约 10 秒后断网，观察恢复。", "断网阶段提示与状态正确；恢复后能继续操作，无错误状态残留。"),
        _case("WN-09", "断网恢复", "较长断网恢复", {"blackout": True, "blackout_duration_s": 30, "duration_s": 30, "loss_pct": 100.0}, "进行核心操作后保持断网约 30 秒，观察超时；恢复后重新进入功能。", "超时或重连提示明确；恢复后状态一致，不会重复提交。"),
    ]
    return normalize_template({"id": "standard-v1", "name": "标准弱网回归（9项）", "description": "Motu Net 内置的短流程回归模板。", "source": "Motu Net", "cases": cases})


def generic_system_template() -> dict[str, Any]:
    """A generic 15-item template aligned with the team's previous case set."""
    group_conn = "连接保持"
    group_weak = "弱网络操作"
    group_protocol = "协议安全"
    group_extra = "补充场景"
    cases = [
        _case("CONN-01", group_conn, "正常网络基线", {}, "执行系统核心链路一次，记录进入、查询、提交和返回耗时。", "核心链路可完成，数据正确，作为后续对照。"),
        _case("CONN-02", group_conn, "连接中途受扰后继续操作", {"delay_ms": 300.0, "jitter_ms": 80.0, "loss_pct": 10.0}, "在核心操作进行中施加延迟和丢包，继续完成当前操作。", "有加载或重试反馈；不重复提交，最终状态与服务端一致。"),
        _case("CONN-03", group_conn, "短时断网自动重连", {"blackout": True, "blackout_duration_s": 5, "loss_pct": 100.0}, "操作中断网约 5 秒，观察客户端提示和自动重连。", "出现明确断网/重连状态；恢复后可继续操作，状态不丢失。"),
        _case("CONN-04", group_conn, "手动切换网络后的恢复", {"blackout": True, "blackout_duration_s": 8, "loss_pct": 100.0}, "在系统操作中切换网络或暂时断开，再恢复网络并重新进入功能。", "切换后能恢复连接；无卡死、错误状态残留或重复请求。"),
        _case("WEAK-01", group_weak, "弱网加载与发起操作", {"delay_ms": 150.0, "jitter_ms": 30.0, "loss_pct": 5.0}, "打开资源较多的页面并发起一次核心操作。", "加载状态清晰；操作可完成或可重试，结果与服务端一致。"),
        _case("WEAK-02", group_weak, "低带宽高延迟边界", {"delay_ms": 500.0, "jitter_ms": 100.0, "bandwidth_kbps": 128.0}, "在低带宽高延迟条件下连续浏览并完成一次提交。", "不会无限加载；超时提示明确，提交不会重复。"),
        _case("WEAK-03", group_weak, "高丢包重试与恢复", {"loss_pct": 30.0, "delay_ms": 100.0}, "刷新页面并执行一次可重试操作，观察失败和恢复。", "失败可感知、重试可控；成功后数据不重复、不丢失。"),
        _case("WEAK-04", group_weak, "长连接场景短时中断", {"blackout": True, "start_delay_s": 3, "blackout_duration_s": 5}, "保持实时或战斗类操作，等待自动断网并观察重连。", "断网期间状态提示正确；恢复后长连接重新建立，业务状态一致。"),
        _case("PROTO-01", group_protocol, "重复请求幂等", {"duplicate_pct": 100.0}, "对提交、领取或购买等状态变更操作只执行一次，观察重复包影响。", "服务端只产生一次业务结果；客户端不会重复扣除或重复发放。"),
        _case("PROTO-02", group_protocol, "乱序响应一致性", {"reorder_pct": 100.0, "reorder_ms": 200.0}, "快速连续触发查询或状态变化，观察返回顺序。", "界面以最新有效状态为准；不会被旧响应覆盖。"),
        _case("EXTRA-01", group_extra, "移动网络切换", {"delay_ms": 220.0, "jitter_ms": 100.0, "loss_pct": 12.0}, "模拟 Wi-Fi/移动网络切换期间进入功能并恢复操作。", "切换期间提示清晰；恢复后可继续操作，数据一致。"),
        _case("EXTRA-02", group_extra, "后台挂起后重连", {"blackout": True, "start_delay_s": 2, "blackout_duration_s": 15}, "进入后台或锁屏后等待网络中断，再回到前台。", "回前台后能重新连接；页面不会显示过期或半完成状态。"),
        _case("EXTRA-03", group_extra, "静态资源加载降级", {"bandwidth_kbps": 64.0, "loss_pct": 20.0}, "打开含图片、配置或 CDN 资源的页面，观察加载失败和重试。", "资源失败有可理解提示或降级内容；不会阻塞核心操作。"),
        _case("EXTRA-04", group_extra, "接口超时与重试", {"delay_ms": 1000.0, "loss_pct": 20.0}, "触发一个耗时接口并观察超时、重试和最终结果。", "超时边界符合产品口径；重试不会产生重复副作用。"),
        _case("EXTRA-05", group_extra, "快速重复点击防护", {"delay_ms": 300.0, "loss_pct": 10.0, "duplicate_pct": 20.0}, "在弱网下连续点击提交、领取或确认按钮。", "按钮有处理中状态或幂等保护；业务结果只生效一次。"),
    ]
    return normalize_template({"id": "generic-system-v1", "name": "通用系统弱网回归（15项）", "description": "按连接保持、弱网络操作、协议安全和补充场景执行一轮通用弱网检查。", "source": "飞书弱网用例通用化", "cases": cases})


class TemplateStore:
    """Persist user templates outside the install directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.errors: list[str] = []

    def list(self) -> list[dict[str, Any]]:
        self.errors = []
        result = [standard_template(), generic_system_template()]
        if not self.root.exists():
            return result
        try:
            paths = sorted(self.root.glob("*.json"), key=lambda p: p.name.casefold())
        except OSError as exc:
            self.errors.append(f"无法列出测试模板：{exc}")
            return result
        builtin_ids = {item["id"] for item in result}
        for path in paths:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    item = normalize_template(json.load(handle))
                if item["id"] not in builtin_ids:
                    result.append(item)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.errors.append(f"已跳过损坏模板 {path.name}：{exc}")
        return result

    def save(self, template: Mapping[str, Any]) -> dict[str, Any]:
        item = normalize_template(template)
        builtin_ids = {"standard-v1", "generic-system-v1"}
        if item["id"] in builtin_ids:
            item["id"] = f"custom-{uuid.uuid4().hex[:12]}"
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{item['id']}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(envelope(item), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return item

    def import_file(self, path: str | Path) -> dict[str, Any]:
        with Path(path).open("r", encoding="utf-8") as handle:
            return self.save(json.load(handle))

    def export(self, path: str | Path, template: Mapping[str, Any]) -> None:
        Path(path).write_text(json.dumps(envelope(template), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
