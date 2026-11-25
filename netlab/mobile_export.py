"""Convert desktop scenes and templates to the portable mobile JSON formats."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .config import build_filter, validate_config
from .test_templates import envelope, generic_system_template, standard_template


MOBILE_SCENE_SCHEMA_VERSION = 2
_IMPAIRMENT_FIELDS = (
    "delay_ms", "jitter_ms", "loss_pct", "bandwidth_kbps",
    "duplicate_pct", "reorder_pct", "reorder_ms", "duration_s",
    "blackout", "start_delay_s", "blackout_duration_s", "blackout_loop",
)


def convert_desktop_scene(payload: Mapping[str, Any], *, estimated_minutes: int = 5) -> dict[str, Any]:
    """Preserve network values while refusing Windows-only traffic filters."""
    if not isinstance(payload, Mapping):
        raise ValueError("场景必须是 JSON 对象")
    raw = payload.get("config", payload)
    if not isinstance(raw, Mapping):
        raise ValueError("场景缺少 config 对象")
    if raw.get("scope", "all") not in {"all", "endpoint"}:
        raise ValueError("移动端不支持 Windows 专用的高级过滤或回环作用域")
    if isinstance(raw.get("filter"), str) and raw["filter"] != build_filter(raw):
        raise ValueError("自定义 WinDivert filter 无法安全转换为移动端目标")
    if isinstance(estimated_minutes, bool) or not isinstance(estimated_minutes, int) or not 1 <= estimated_minutes <= 30:
        raise ValueError("estimated_minutes 必须是 1 到 30 的整数")

    config = validate_config(raw)
    if config["blackout"] and config["blackout_mode"] == "adapter":
        raise ValueError("移动端不支持 Windows 网卡断网，不能转换为丢包模拟")
    ports = [int(port) for port in config["ports"].split(",") if port]
    impairment = {}
    for key in _IMPAIRMENT_FIELDS:
        value = config[key]
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"移动端暂不支持小数参数 {key}：{value}")
            value = int(value)
        impairment[key] = deepcopy(value)
    if config["blackout"] and not config["blackout_loop"] and config["blackout_duration_s"] > 0:
        # Desktop duration starts when the delayed outage begins; mobile duration starts with the VPN.
        total_duration = config["start_delay_s"] + config["blackout_duration_s"]
        if total_duration > 86_400:
            raise ValueError("移动端断网总时长不能超过 86400 秒")
        impairment["duration_s"] = total_duration
    return {
        "schema_version": MOBILE_SCENE_SCHEMA_VERSION,
        "name": config["name"],
        "target": {
            "scope": config["scope"],
            "host": config["host"] if config["scope"] == "endpoint" else "",
            "ports": ports if config["scope"] == "endpoint" else [],
            "protocol": "both" if config["protocol"] == "all" else config["protocol"],
            "direction": config["direction"],
            "app_ids": [],
        },
        "impairment": impairment,
        "estimated_minutes": estimated_minutes,
    }


def scene_catalog(scenes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not scenes:
        raise ValueError("至少需要一个场景")
    converted = [convert_desktop_scene(scene) for scene in scenes]
    return {"schema_version": MOBILE_SCENE_SCHEMA_VERSION, "scenarios": converted}


def export_mobile_assets(scenario_dir: str | Path, output_dir: str | Path) -> list[Path]:
    """Write portable scenes plus the existing template envelopes."""
    source = Path(scenario_dir)
    paths = sorted(source.glob("*.json"), key=lambda path: path.name.casefold())
    if not paths:
        raise ValueError(f"未找到桌面场景：{source}")
    scenes = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "catalog.json": scene_catalog(scenes),
        "standard-v1.json": envelope(standard_template()),
        "generic-system-v1.json": envelope(generic_system_template()),
    }
    written = []
    for filename, payload in files.items():
        path = output / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
