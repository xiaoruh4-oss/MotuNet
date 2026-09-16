"""Small, dependency-free persistence layer for NetLab."""

from __future__ import annotations

import getpass
import json
import os
import platform
import threading
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .config import DEFAULT_CONFIG, PRESETS, validate_config

SCHEMA_VERSION = 1
_SCENARIO_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_SCENARIO_NAME = 80


def data_dir() -> Path:
    """Return the per-user NetLab data directory."""
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "MotuNet"
    return Path.home() / "AppData" / "Local" / "MotuNet"


def _validated_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("configuration file must contain an object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing schema_version")
    config = value.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("configuration envelope has no config object")
    try:
        return validate_config(config)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid configuration: {exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read configuration {path}: {exc}") from exc
    return _validated_envelope(value)


def _envelope(config: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "config": validate_config(config)}


def _write_json(path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    payload = _envelope(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return payload["config"]


class ConfigStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root) if root is not None else data_dir()
        self.path = self.root / "config.json"
        self.scenarios_path = self.root / "scenarios"
        self.scenario_errors: list[str] = []

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return dict(DEFAULT_CONFIG)
        return _read_json(self.path)

    def save(self, config: Mapping[str, Any]) -> dict[str, Any]:
        return _write_json(self.path, config)

    def export(self, path: str | os.PathLike[str], config: Mapping[str, Any]) -> dict[str, Any]:
        return _write_json(Path(path), config)

    def import_config(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        return _read_json(Path(path))

    @staticmethod
    def _scenario_id(value: str) -> str:
        if not isinstance(value, str) or not _SCENARIO_ID_RE.fullmatch(value):
            raise ValueError("场景ID必须是32位小写十六进制字符串")
        return value

    @staticmethod
    def _validate_scenario_config(config: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_config(config)
        name = normalized["name"].strip()
        if not name:
            raise ValueError("场景名称不能为空")
        if len(name) > _MAX_SCENARIO_NAME:
            raise ValueError(f"场景名称不能超过{_MAX_SCENARIO_NAME}个字符")
        normalized["name"] = name
        return normalized

    def _read_scenario_file(self, path: Path, expected_id: str | None = None) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取场景文件：{exc}") from exc
        if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("场景文件版本无效")
        scenario_id = value.get("id")
        self._scenario_id(scenario_id)
        if expected_id is not None and scenario_id != expected_id:
            raise ValueError("场景ID与文件名不一致")
        config = value.get("config")
        if not isinstance(config, Mapping):
            raise ValueError("场景文件缺少配置对象")
        return {"id": scenario_id, "config": self._validate_scenario_config(config)}

    def list_scenarios(self) -> list[dict[str, Any]]:
        """Load user scenarios, skipping individual bad files and recording errors."""
        self.scenario_errors = []
        if not self.scenarios_path.exists():
            return []
        result: list[dict[str, Any]] = []
        try:
            paths = sorted(self.scenarios_path.glob("*.json"), key=lambda p: p.name.casefold())
        except OSError as exc:
            self.scenario_errors.append(f"无法列出场景文件：{exc}")
            return []
        for path in paths:
            if not path.is_file():
                continue
            scenario_id = path.stem
            if not _SCENARIO_ID_RE.fullmatch(scenario_id):
                self.scenario_errors.append(f"已跳过不安全的场景文件名：{path.name}")
                continue
            try:
                result.append(self._read_scenario_file(path, scenario_id))
            except ValueError as exc:
                self.scenario_errors.append(f"已跳过损坏场景 {path.name}：{exc}")
        result.sort(key=lambda item: (item["config"]["name"].casefold(), item["id"]))
        return result

    def delete_scenario(self, scenario_id: str) -> None:
        """Delete exactly one saved user scenario; names are never file paths."""
        scenario_id = self._scenario_id(scenario_id)
        path = self.scenarios_path / f"{scenario_id}.json"
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ValueError("需要删除的自定义场景不存在") from exc
        except OSError as exc:
            raise ValueError(f"删除自定义场景失败：{exc}") from exc

    def save_scenario(self, config: Mapping[str, Any], scenario_id: str | None = None) -> dict[str, Any]:
        """Create or explicitly overwrite a user scenario using an atomic write."""
        normalized = self._validate_scenario_config(config)
        builtin_names = {str(item["name"]).casefold() for item in PRESETS}
        if normalized["name"].casefold() in builtin_names:
            raise ValueError("自定义场景名称不能与内置场景重名")
        if scenario_id is None:
            scenario_id = uuid.uuid4().hex
        else:
            scenario_id = self._scenario_id(scenario_id)
            if not (self.scenarios_path / f"{scenario_id}.json").is_file():
                raise ValueError("需要覆盖的自定义场景不存在，请另存为新场景")
        # A duplicate name is allowed only when replacing that same scenario.
        for item in self.list_scenarios():
            if item["id"] != scenario_id and item["config"]["name"].casefold() == normalized["name"].casefold():
                raise ValueError("自定义场景名称已存在")
        path = self.scenarios_path / f"{scenario_id}.json"
        payload = {"schema_version": SCHEMA_VERSION, "id": scenario_id, "config": normalized}
        temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            self.scenarios_path.mkdir(parents=True, exist_ok=True)
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp, path)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ValueError(f"保存自定义场景失败：{exc}") from exc
        return {"id": scenario_id, "config": normalized}


def export(path: str | os.PathLike[str], config: Mapping[str, Any]) -> dict[str, Any]:
    """Export a versioned configuration envelope."""
    return _write_json(Path(path), config)


def import_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Import and validate a versioned configuration envelope."""
    return _read_json(Path(path))


class AuditLog:
    """Thread-safe daily JSONL audit log; packet contents are never persisted."""

    _sensitive = {"payload", "packet", "packet_payload", "raw_packet", "data"}

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root) if root is not None else data_dir()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.root / "audit" / f"{datetime.now().date().isoformat()}.jsonl"

    def write(self, event: str, **details: Any) -> dict[str, Any]:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event must be a non-empty string")
        safe_details = {key: value for key, value in details.items() if key.lower() not in self._sensitive}
        now = datetime.now().astimezone()
        record: dict[str, Any] = {
            "timestamp": now.isoformat(timespec="seconds"),
            "event": event,
            "user": getpass.getuser(),
            "host": platform.node(),
            **safe_details,
        }
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        return record
