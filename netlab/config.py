"""Configuration and WinDivert filter helpers for NetLab.

The module deliberately keeps the public data model made of plain dictionaries so it
can be used by the desktop UI and by the command line tools without a dependency.
"""

from __future__ import annotations

import ipaddress
import math
import re
from copy import deepcopy
from typing import Any, Mapping


_BASE_DEFAULTS: dict[str, Any] = {
    "name": "自定义场景",
    "scope": "all",
    "protocol": "all",
    "direction": "both",
    "host": "",
    "ports": "",
    "advanced_filter": "",
    "delay_ms": 150.0,
    "jitter_ms": 30.0,
    "loss_pct": 2.0,
    "bandwidth_kbps": 0.0,
    "duplicate_pct": 0.0,
    "reorder_pct": 0.0,
    "reorder_ms": 100.0,
    "duration_s": 60,
    "blackout": False,
    # For blackout scenarios, optionally wait before dropping traffic and then
    # restore it automatically after this many seconds.
    "start_delay_s": 0,
    "blackout_duration_s": 10,
    "blackout_loop": False,
}


def _q(value: str) -> str:
    """Quote an address for WinDivert without introducing shell syntax."""
    return value


def _port_values(value: Any) -> list[int]:
    if not isinstance(value, str):
        raise ValueError("端口必须是逗号分隔的字符串")
    value = value.strip()
    if not value:
        return []
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not re.fullmatch(r"[0-9]+", item):
            raise ValueError(f"端口格式无效：{item!r}")
        port = int(item)
        if not 1 <= port <= 65535:
            raise ValueError(f"端口超出范围：{port}")
        if port not in result:
            result.append(port)
    if len(result) > 32:
        raise ValueError("端口数量不能超过32个")
    return result


_FIELD_NAMES = {"delay_ms": "延迟", "jitter_ms": "抖动", "loss_pct": "丢包率",
                "bandwidth_kbps": "带宽", "duplicate_pct": "重复率",
                "reorder_pct": "乱序率", "reorder_ms": "乱序窗口", "duration_s": "持续时间",
                "start_delay_s": "断网前等待时间", "blackout_duration_s": "断网持续时间"}


def _number(value: Any, key: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{_FIELD_NAMES[key]}必须是数字")
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        bound = f" >= {minimum}" if maximum is None else f" in [{minimum}, {maximum}]"
        raise ValueError(f"{_FIELD_NAMES[key]}必须是有限数值，范围为{minimum}至{maximum if maximum is not None else '无上限'}")
    return number


def _direction_clause(direction: str) -> str:
    return {"inbound": "inbound", "outbound": "outbound", "both": ""}[direction]


def build_filter(data: Mapping[str, Any]) -> str:
    """Compile the safe subset of a scenario into a WinDivert expression."""
    scope = data.get("scope", "all")
    protocol = data.get("protocol", "all")
    direction = data.get("direction", "both")
    host = data.get("host", "")
    ports = _port_values(data.get("ports", ""))

    if scope == "advanced":
        advanced = str(data.get("advanced_filter", "")).strip()
        if not advanced:
            raise ValueError("高级过滤作用域必须填写过滤表达式")
        # The guard is intentionally appended even when the user supplied one.
        return f"({advanced}) and not impostor"

    clauses: list[str] = ["not impostor"]
    if scope == "loopback":
        clauses.append("loopback")
        clauses.append("outbound")
    else:
        # `all` and `endpoint` are intentionally kept off loopback.  Loopback has
        # its own explicit scope because inbound loopback filtering is ambiguous.
        clauses.append("not loopback")
        direction_clause = _direction_clause(direction)
        if direction_clause:
            clauses.append(direction_clause)

    if protocol != "all":
        clauses.append(protocol)

    if scope == "endpoint" and host:
        address = ipaddress.ip_address(host)
        field = "ip.SrcAddr" if address.version == 4 else "ipv6.SrcAddr"
        dst_field = "ip.DstAddr" if address.version == 4 else "ipv6.DstAddr"
        clauses.append(f"({field} == {_q(host)} or {dst_field} == {_q(host)})")

    if scope == "endpoint" and ports:
        proto_names = [protocol] if protocol in ("tcp", "udp") else ["tcp", "udp"]
        port_terms = [f"{proto}.SrcPort == {port} or {proto}.DstPort == {port}" for proto in proto_names for port in ports]
        clauses.append("(" + " or ".join(port_terms) + ")")

    return " and ".join(clauses)


def validate_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a scenario, returning a complete fresh dictionary."""
    if not isinstance(data, Mapping):
        raise ValueError("配置必须是对象")
    allowed = set(_BASE_DEFAULTS) | {"filter"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"存在未知配置字段：{', '.join(sorted(unknown))}")

    cfg = deepcopy(_BASE_DEFAULTS)
    cfg.update({key: data[key] for key in _BASE_DEFAULTS if key in data})
    # Before blackout got its own duration field, duration_s controlled the
    # outage.  Migrate that value (including 0 for manual stop) when importing
    # an older scenario, instead of silently replacing it with the new default.
    if data.get("blackout") is True and "blackout_duration_s" not in data and "duration_s" in data:
        cfg["blackout_duration_s"] = data["duration_s"]

    if not isinstance(cfg["name"], str) or not cfg["name"].strip():
        raise ValueError("场景名称不能为空，且必须是文本")
    if len(cfg["name"]) > 80:
        raise ValueError("场景名称不能超过80个字符")
    for key, values in (("scope", {"all", "endpoint", "loopback", "advanced"}),
                        ("protocol", {"all", "tcp", "udp"}),
                        ("direction", {"both", "inbound", "outbound"})):
        if not isinstance(cfg[key], str) or cfg[key] not in values:
            raise ValueError(f"{key}配置值无效")
    if not isinstance(cfg["host"], str):
        raise ValueError("目标地址必须是IPv4或IPv6字面量")
    cfg["host"] = cfg["host"].strip()
    if cfg["host"]:
        try:
            ipaddress.ip_address(cfg["host"])
        except ValueError as exc:
            raise ValueError("目标地址必须是IPv4或IPv6字面量") from exc
        if "%" in cfg["host"]:
            raise ValueError("目标地址不能包含IPv6区域标识")
    cfg["ports"] = ",".join(str(p) for p in _port_values(cfg["ports"]))
    if not isinstance(cfg["advanced_filter"], str):
        raise ValueError("高级过滤表达式必须是文本")
    cfg["advanced_filter"] = cfg["advanced_filter"].strip()
    if cfg["scope"] == "endpoint" and not cfg["host"] and not cfg["ports"]:
        raise ValueError("端点作用域至少需要填写目标地址或端口")
    if cfg["scope"] == "advanced" and not cfg["advanced_filter"]:
        raise ValueError("高级作用域必须填写过滤表达式")
    if cfg["scope"] == "loopback" and cfg["direction"] != "outbound":
        raise ValueError("本机回环作用域只支持出站方向")

    ranges = {
        "delay_ms": (0.0, 15000.0),
        "jitter_ms": (0.0, 15000.0),
        "loss_pct": (0.0, 100.0),
        "bandwidth_kbps": (0.0, 1000000.0),
        "duplicate_pct": (0.0, 100.0),
        "reorder_pct": (0.0, 100.0),
        "reorder_ms": (1.0, 15000.0),
        "duration_s": (0.0, 86400.0),
        "start_delay_s": (0.0, 86400.0),
        "blackout_duration_s": (0.0, 86400.0),
    }
    for key, (minimum, maximum) in ranges.items():
        if key in {"duration_s", "start_delay_s", "blackout_duration_s"} and (isinstance(cfg[key], bool) or not isinstance(cfg[key], int)):
            labels = {"duration_s": "持续时间", "start_delay_s": "断网前等待时间", "blackout_duration_s": "断网持续时间"}
            raise ValueError(f"{labels[key]}必须是整数秒")
        value = _number(cfg[key], key, minimum, maximum)
        cfg[key] = int(value) if key in {"duration_s", "start_delay_s", "blackout_duration_s"} else value
    if not isinstance(cfg["blackout"], bool):
        raise ValueError("断网标记必须是布尔值")
    if not isinstance(cfg["blackout_loop"], bool):
        raise ValueError("循环断网标记必须是布尔值")
    if cfg["blackout_loop"]:
        if not cfg["blackout"]:
            raise ValueError("循环断网必须先启用完全断网")
        if cfg["start_delay_s"] < 1 or cfg["blackout_duration_s"] < 1:
            raise ValueError("启用循环断网时，断网前等待时间和断网持续时间必须至少为1秒")
    if cfg["blackout"] and cfg["start_delay_s"] > 0 and cfg["blackout_duration_s"] < 1:
        raise ValueError("启用延迟断网时，断网持续时间必须至少为1秒")
    cfg["filter"] = build_filter(cfg)
    if len(cfg["filter"]) > 4096:
        raise ValueError("过滤表达式不能超过4096个字符")
    try:
        cfg["filter"].encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("过滤表达式只能使用ASCII字符") from exc
    if "\x00" in cfg["filter"]:
        raise ValueError("过滤表达式不能包含空字符")
    return cfg


DEFAULT_CONFIG: dict[str, Any] = validate_config(_BASE_DEFAULTS)


PRESETS: list[dict[str, Any]] = [
    {"name": "轻度弱网", "description": "轻微延迟和丢包", "values": {"delay_ms": 80.0, "jitter_ms": 15.0, "loss_pct": 1.0, "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 0.0, "reorder_ms": 1.0}},
    {"name": "地铁电梯", "description": "延迟抖动和丢包", "values": {"delay_ms": 220.0, "jitter_ms": 120.0, "loss_pct": 8.0, "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 2.0, "reorder_ms": 100.0}},
    {"name": "高延迟", "description": "高延迟交互验证", "values": {"delay_ms": 600.0, "jitter_ms": 80.0, "loss_pct": 0.0, "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 0.0, "reorder_ms": 1.0}},
    {"name": "丢包专项", "description": "专项验证丢包恢复", "values": {"delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 20.0, "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 0.0, "reorder_ms": 1.0}},
    {"name": "低带宽", "description": "专项验证低带宽", "values": {"delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 0.0, "bandwidth_kbps": 256.0, "duplicate_pct": 0.0, "reorder_pct": 0.0, "reorder_ms": 1.0}},
    {"name": "短时断网", "description": "短时中断连接", "values": {"delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 100.0, "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 0.0, "reorder_ms": 1.0, "duration_s": 10, "blackout": True, "start_delay_s": 0, "blackout_duration_s": 10}},
    {"name": "延迟断网", "description": "等待后自动断网并恢复", "values": {"delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 0.0, "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 0.0, "reorder_ms": 1.0, "duration_s": 10, "blackout": True, "start_delay_s": 10, "blackout_duration_s": 10}},
    {"name": "正常对照", "description": "正常网络对照", "values": {"delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 0.0, "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 0.0, "reorder_ms": 1.0}},
]

# Keep preset payloads self-contained so they can be exported directly, while
# accepting older callers that only override the network impairment fields.
for _preset in PRESETS:
    _preset["values"].setdefault("start_delay_s", 0)
    _preset["values"].setdefault("blackout_duration_s", 10 if _preset["values"].get("blackout") else 0)
    _preset["values"].setdefault("blackout_loop", False)
