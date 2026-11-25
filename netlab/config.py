"""Configuration and WinDivert filter helpers for NetLab.

The module deliberately keeps the public data model made of plain dictionaries so it
can be used by the desktop UI and by the command line tools without a dependency.
"""

from __future__ import annotations

import ipaddress
import math
import queue
import re
import socket
import threading
from urllib.parse import urlparse
from copy import deepcopy
from typing import Any, Mapping


_BASE_DEFAULTS: dict[str, Any] = {
    "name": "自定义场景",
    "scope": "all",
    "protocol": "all",
    "direction": "both",
    "host": "",
    "ports": "",
    # A web target keeps the hostname for display and resolves it immediately
    # before a packet session starts.  Resolved addresses are never accepted
    # from imported files; they are refreshed by resolve_web_config().
    "web_ips": [],
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
    # Existing presets/files remain packet-based unless the operator opts in.
    "blackout_mode": "packet",
    "adapter_ids": [],
    # For blackout scenarios, optionally wait before dropping traffic and then
    # restore it automatically after this many seconds.
    "start_delay_s": 0,
    "blackout_duration_s": 10,
    "blackout_loop": False,
}


def _q(value: str) -> str:
    """Quote an address for WinDivert without introducing shell syntax."""
    return value


def normalize_adapter_ids(value: Any) -> list[str]:
    """Accept only literal Windows interface GUIDs, never shell/name fragments."""
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("网卡列表必须是最多16项的 GUID 数组")
    result: list[str] = []
    pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    for item in value:
        if not isinstance(item, str):
            raise ValueError("网卡标识必须是 GUID 文本")
        literal = item[1:-1] if item.startswith("{") and item.endswith("}") else item
        if not re.fullmatch(pattern, literal):
            raise ValueError("网卡标识必须是完整 GUID，不能使用网卡名称或命令")
        normalized = literal.upper()
        if normalized in result:
            raise ValueError("网卡列表不能包含重复 GUID")
        result.append(normalized)
    return result


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


def _web_addresses(values: Any) -> list[str]:
    """Accept only IP text, never integers, scoped addresses or filter fragments."""
    if not isinstance(values, list):
        raise ValueError("网页解析地址必须是数组")
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "%" in value:
            raise ValueError("网页解析地址必须是纯 IPv4 或 IPv6 文本")
        try:
            text = str(ipaddress.ip_address(value))
        except ValueError as exc:
            raise ValueError("网页解析地址必须是纯 IPv4 或 IPv6 文本") from exc
        if text not in result:
            result.append(text)
    if len(result) > 64:
        raise ValueError("网页解析地址超过64个，请缩小测试目标，不能只测试部分解析结果")
    return result


def _web_host(value: str) -> tuple[str, int]:
    """Normalize a URL without retaining paths, credentials or arbitrary code."""
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value) or "\\" in value:
        raise ValueError("网页地址必须是域名或 http(s)/ws(s) URL，不能包含空白字符")
    # A previously normalized IPv6 host has no URL brackets or scheme.
    if "://" not in value:
        try:
            literal = ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            if "%" in value:
                raise ValueError("网页地址不能包含 IPv6 区域标识")
            return str(literal), 443
    try:
        parsed = urlparse(value if "://" in value else "https://" + value)
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            raise ValueError("网页地址只支持 http、https、ws 或 wss")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("网页地址不能包含用户名或密码，请只填写域名和端口")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("网页地址或端口无效：" + str(exc)) from exc
    if not hostname or "%" in hostname or len(hostname) > 253:
        raise ValueError("网页地址中的域名或 IP 格式无效")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        hostname = hostname[:-1] if hostname.endswith(".") else hostname
        labels = hostname.split(".")
        if any(not label or len(label) > 63 or
               not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
               for label in labels):
            raise ValueError("网页地址中的域名格式无效")
        hostname = hostname.lower()
    else:
        hostname = str(address)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("网页端口必须为1至65535")
    return hostname, port if port is not None else (80 if parsed.scheme in ("http", "ws") else 443)


def _web_filter(data: Mapping[str, Any], protocol: str, direction: str, ports: list[int]) -> str:
    addresses = _web_addresses(data.get("web_ips", []))
    if not addresses:
        # Importing/saving drafts is offline.  This contradiction can never
        # accidentally capture packets before the startup resolver succeeds.
        return "not impostor and ip.DstAddr == 0.0.0.0 and ip.DstAddr != 0.0.0.0"
    groups = {True: [], False: []}
    for value in addresses:
        address = ipaddress.ip_address(value)
        groups[address.is_loopback].append(("ip" if address.version == 4 else "ipv6", value))

    # Match the server endpoint as a pair.  A separate IP clause and a
    # separate port clause creates a cross-product that can catch another
    # service's port on the same resolved address.  For outbound traffic the
    # server is the destination; for inbound traffic it is the source.  Windows
    # reports loopback packets as outbound in WinDivert, so loopback keeps the
    # outbound guard while still using Src/Dst to distinguish the two halves.
    if not ports:
        raise ValueError("网页目标需要服务器端口，留空时请使用 URL 默认端口")
    protocols = [protocol] if protocol in ("tcp", "udp") else ["tcp", "udp"]
    branches = []
    for local, targets in groups.items():
        if not targets:
            continue
        for selected_direction, side in (("outbound", "Dst"), ("inbound", "Src")):
            if direction not in ("both", selected_direction):
                continue
            address_terms = [f"{field}.{side}Addr == {address}" for field, address in targets]
            port_terms = [f"{proto}.{side}Port == {port}" for proto in protocols for port in ports]
            clauses = (["loopback", "outbound"] if local else ["not loopback", selected_direction])
            clauses += ["(" + " or ".join(address_terms) + ")", "(" + " or ".join(port_terms) + ")"]
            branches.append("(" + " and ".join(clauses) + ")")
    if not branches:
        raise ValueError("网页目标没有可用的方向匹配条件")
    return "not impostor and (" + " or ".join(branches) + ")"


def build_filter(data: Mapping[str, Any]) -> str:
    """Compile the safe subset of a scenario into a WinDivert expression."""
    scope = data.get("scope", "all")
    protocol = data.get("protocol", "all")
    direction = data.get("direction", "both")
    host = data.get("host", "")
    ports = _port_values(data.get("ports", ""))

    if scope == "web":
        return _web_filter(data, protocol, direction, ports)

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
    if cfg["blackout_mode"] not in ("packet", "adapter"):
        raise ValueError("断网模式必须是 packet 或 adapter")
    cfg["adapter_ids"] = normalize_adapter_ids(cfg["adapter_ids"])
    # Before blackout got its own duration field, duration_s controlled the
    # outage.  Migrate that value (including 0 for manual stop) when importing
    # an older scenario, instead of silently replacing it with the new default.
    if data.get("blackout") is True and "blackout_duration_s" not in data and "duration_s" in data:
        cfg["blackout_duration_s"] = data["duration_s"]

    if not isinstance(cfg["name"], str) or not cfg["name"].strip():
        raise ValueError("场景名称不能为空，且必须是文本")
    if len(cfg["name"]) > 80:
        raise ValueError("场景名称不能超过80个字符")
    for key, values in (("scope", {"all", "endpoint", "web", "loopback", "advanced"}),
                        ("protocol", {"all", "tcp", "udp"}),
                        ("direction", {"both", "inbound", "outbound"})):
        if not isinstance(cfg[key], str) or cfg[key] not in values:
            raise ValueError(f"{key}配置值无效")
    if not isinstance(cfg["host"], str):
        raise ValueError("目标地址必须是IPv4或IPv6字面量")
    cfg["host"] = cfg["host"].strip()
    cfg["web_ips"] = _web_addresses(cfg["web_ips"])
    if cfg["host"]:
        if cfg["scope"] == "web":
            cfg["host"], default_port = _web_host(cfg["host"])
            if not isinstance(cfg["ports"], str):
                raise ValueError("端口必须是逗号分隔的字符串")
            cfg["ports"] = cfg["ports"].strip()
            if not cfg["ports"]:
                cfg["ports"] = str(default_port)
        else:
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
    if cfg["scope"] == "web" and not cfg["host"]:
        raise ValueError("网页作用域必须填写域名或网页地址")
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
    if cfg["blackout"] and cfg["blackout_mode"] == "adapter":
        if (cfg["scope"] != "all" or cfg["protocol"] != "all" or cfg["direction"] != "both"
                or cfg["host"] or cfg["ports"] or cfg["advanced_filter"]):
            raise ValueError("网卡断网影响该网卡全部流量，不能限定 IP、端口、协议或方向")
        if cfg["blackout_loop"]:
            raise ValueError("网卡断网暂不支持循环执行")
        if not 1 <= cfg["blackout_duration_s"] <= 300:
            raise ValueError("网卡断网持续时间必须为1至300秒，并自动恢复")
        # Saving an empty list is useful for portable drafts; the adapter
        # engine additionally requires an explicit non-empty local selection.
        cfg["duration_s"] = cfg["blackout_duration_s"]
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


def _resolve_web_dns(host: str, *, timeout: float = 8.0):
    """Bound the caller's wait; a stuck OS resolver cannot hold the UI job forever."""
    completed = queue.Queue(maxsize=1)

    def resolve():
        try:
            completed.put((socket.getaddrinfo(host, None, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            completed.put((None, exc))

    threading.Thread(target=resolve, name="Motu-web-dns", daemon=True).start()
    try:
        infos, error = completed.get(timeout=timeout)
    except queue.Empty as exc:
        raise ValueError(f"解析网页域名 {host} 超过8秒，请检查域名和网络后重试") from exc
    if error:
        raise ValueError(f"无法解析网页域名 {host}：{error}") from error
    return infos


def resolve_web_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a web hostname immediately before starting packet capture."""
    cfg = validate_config(data)
    if cfg.get("scope") != "web":
        return cfg
    try:
        literal = ipaddress.ip_address(cfg["host"])
    except ValueError:
        infos = _resolve_web_dns(cfg["host"])
    else:
        cfg["web_ips"] = [str(literal)]
        return validate_config(cfg)
    addresses = []
    for item in infos:
        try:
            value = str(ipaddress.ip_address(item[4][0]))
        except (ValueError, IndexError, TypeError):
            continue
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise ValueError(f"网页域名 {cfg['host']} 没有解析到 IP")
    cfg["web_ips"] = _web_addresses(addresses)
    return validate_config(cfg)


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
