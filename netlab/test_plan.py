"""Business-scene test plans assembled from reusable network conditions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .config import validate_config
from .test_templates import TEST_TIME_BUDGET_MINUTES, generic_system_template


PLAN_SCHEMA_VERSION = 1
LEVEL_LABELS = {"smoke": "冒烟", "standard": "标准", "full": "完整", "custom": "自定义"}
_SCENE_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_LEVEL_KEYS = ("smoke", "standard", "full")
_NETWORK_DEFAULTS = {
    "delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 0.0,
    "bandwidth_kbps": 0.0, "duplicate_pct": 0.0, "reorder_pct": 0.0,
    "reorder_ms": 1.0, "duration_s": 60, "blackout": False,
    "start_delay_s": 0, "blackout_duration_s": 10, "blackout_loop": False,
    "blackout_mode": "packet", "adapter_ids": [],
}


def _text(value: Any, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"业务场景 {field} 必须是非空文本")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"业务场景 {field} 不能超过 {maximum} 个字符")
    return value


def network_conditions() -> list[dict[str, Any]]:
    """Return fresh copies of the existing generic 15-condition catalog."""
    return generic_system_template()["cases"]


def normalize_business_scene(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a scene's business instructions and applicability matrix."""
    if not isinstance(value, Mapping):
        raise ValueError("业务场景必须是对象")
    if value.get("schema_version", PLAN_SCHEMA_VERSION) != PLAN_SCHEMA_VERSION:
        raise ValueError("不支持的业务场景 schema_version")
    allowed = {"schema_version", "id", "name", "preconditions", "steps", "observations",
               "expected", "network_case_ids"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"业务场景存在未知字段：{', '.join(sorted(unknown))}")
    scene_id = _text(value.get("id"), "id", 64)
    if not _SCENE_ID_RE.fullmatch(scene_id):
        raise ValueError("业务场景 id 只能使用小写字母、数字、点、下划线和短横线，且以字母开头")
    matrix = value.get("network_case_ids")
    if not isinstance(matrix, Mapping) or set(matrix) != set(_LEVEL_KEYS):
        raise ValueError("network_case_ids 必须包含 smoke、standard、full 三个级别")
    known_ids = {case["id"] for case in network_conditions()}
    normalized_matrix = {}
    for level in _LEVEL_KEYS:
        ids = matrix[level]
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"业务场景 {scene_id} 的 {level} 条件列表不能为空")
        if any(not isinstance(item, str) for item in ids):
            raise ValueError(f"业务场景 {scene_id} 的 {level} 条件编号必须是文本")
        if len(ids) != len(set(ids)):
            raise ValueError(f"业务场景 {scene_id} 的 {level} 条件编号重复")
        unexpected = set(ids) - known_ids
        if unexpected:
            raise ValueError(f"业务场景 {scene_id} 引用了未知网络条件：{', '.join(sorted(unexpected))}")
        normalized_matrix[level] = list(ids)
    if not set(normalized_matrix["smoke"]).issubset(normalized_matrix["standard"]):
        raise ValueError(f"业务场景 {scene_id} 的标准级别必须包含全部冒烟条件")
    if not set(normalized_matrix["standard"]).issubset(normalized_matrix["full"]):
        raise ValueError(f"业务场景 {scene_id} 的完整级别必须包含全部标准条件")
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "id": scene_id,
        "name": _text(value.get("name"), "name", 80),
        "preconditions": _text(value.get("preconditions"), "preconditions"),
        "steps": _text(value.get("steps"), "steps"),
        "observations": _text(value.get("observations"), "observations"),
        "expected": _text(value.get("expected"), "expected"),
        "network_case_ids": normalized_matrix,
    }


def _scene(scene_id: str, name: str, preconditions: str, steps: str,
           observations: str, expected: str, smoke: Sequence[str],
           standard_extra: Sequence[str], full_extra: Sequence[str]) -> dict[str, Any]:
    standard = list(dict.fromkeys([*smoke, *standard_extra]))
    full = list(dict.fromkeys([*standard, *full_extra]))
    return normalize_business_scene({
        "id": scene_id, "name": name, "preconditions": preconditions,
        "steps": steps, "observations": observations, "expected": expected,
        "network_case_ids": {"smoke": list(smoke), "standard": standard, "full": full},
    })


def builtin_business_scenes() -> list[dict[str, Any]]:
    """Examples cover common risk types; each project can import its own scenes."""
    return [
        _scene(
            "login", "登录与进入系统", "准备可用测试账号，客户端处于未登录状态。",
            "发起登录，等待结果；网络恢复后重新查询登录状态并继续进入系统。",
            "等待或重连提示、超时处理、登录结果、恢复后的页面和账号状态。",
            "登录结果清晰；成功只生效一次，失败可重试；恢复后客户端与服务端状态一致。",
            ("CONN-01", "WEAK-01", "CONN-03"),
            ("CONN-02", "WEAK-03", "EXTRA-04"),
            ("WEAK-02", "CONN-04", "PROTO-02", "EXTRA-05"),
        ),
        _scene(
            "lobby", "大厅与页面加载", "已登录并停留在可进入目标页面的位置。",
            "进入目标页面，加载列表或资源，切换页面后重新进入并检查数据。",
            "加载状态、资源失败提示、页面可用性、返回后数据是否刷新。",
            "页面最终可用；资源失败有可理解的反馈，恢复后显示最新有效数据。",
            ("CONN-01", "WEAK-01"),
            ("WEAK-02", "WEAK-03", "EXTRA-03"),
            ("CONN-03", "CONN-04", "PROTO-02", "EXTRA-04"),
        ),
        _scene(
            "battle", "战斗与实时交互", "已进入可重复测试的实时场景，确认服务端状态可查询。",
            "持续进行一次实时操作，在网络异常和恢复后检查战斗进度及最终结果。",
            "实时反馈、断线与重连状态、输入是否重复、双方状态和最终结算。",
            "可感知网络异常；恢复后能继续交互，双方状态及最终结算一致。",
            ("CONN-01", "CONN-03", "WEAK-04"),
            ("CONN-02", "WEAK-03", "EXTRA-04"),
            ("WEAK-02", "CONN-04", "PROTO-02", "EXTRA-02"),
        ),
        _scene(
            "transaction", "支付或奖励领取", "准备可重复的订单或奖励测试数据，并能核对服务端结果。",
            "仅发起一次支付或领取；网络恢复后查询订单、余额或奖励状态。",
            "按钮处理中状态、超时与重试、重复点击、扣款或发奖次数。",
            "结果最多生效一次；客户端与服务端记录一致，未知结果可查询而非重复提交。",
            ("CONN-01", "WEAK-01", "CONN-03"),
            ("CONN-02", "WEAK-03", "PROTO-01", "EXTRA-05"),
            ("WEAK-02", "CONN-04", "PROTO-02", "EXTRA-04"),
        ),
    ]


def import_business_scenes(path: str | Path) -> list[dict[str, Any]]:
    """Read one scene, a list, or a versioned {scenes: [...]} JSON document."""
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, Mapping) and "scenes" in payload:
        if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise ValueError("不支持的业务场景文件 schema_version")
        if set(payload) != {"schema_version", "scenes"}:
            raise ValueError("业务场景文件只能包含 schema_version 和 scenes")
        payload = payload["scenes"]
    if isinstance(payload, Mapping):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise ValueError("业务场景文件至少需要一个场景")
    scenes = [normalize_business_scene(item) for item in payload]
    ids = [scene["id"] for scene in scenes]
    if len(ids) != len(set(ids)):
        raise ValueError("业务场景文件中的 id 重复")
    return scenes


class BusinessSceneStore:
    """Keep imported scenes in the application's existing writable data root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.errors: list[str] = []

    def list(self) -> list[dict[str, Any]]:
        scenes = builtin_business_scenes()
        self.errors = []
        if not self.root.exists():
            return scenes
        used = {scene["id"] for scene in scenes}
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.name.casefold()):
            try:
                imported = import_business_scenes(path)
                colliding = used.intersection(scene["id"] for scene in imported)
                if colliding:
                    raise ValueError(f"业务场景 id 重复：{', '.join(sorted(colliding))}")
                scenes.extend(imported)
                used.update(scene["id"] for scene in imported)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.errors.append(f"已跳过损坏业务场景 {path.name}：{exc}")
        return scenes

    def save(self, scene: Mapping[str, Any]) -> dict[str, Any]:
        item = normalize_business_scene(scene)
        if item["id"] in {builtin["id"] for builtin in builtin_business_scenes()}:
            raise ValueError("内置业务场景不能覆盖，请使用新的 id")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{item['id']}.json"
        temp = path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        return item

    def import_file(self, path: str | Path) -> list[dict[str, Any]]:
        imported = import_business_scenes(path)
        existing_ids = {scene["id"] for scene in self.list()}
        colliding = existing_ids.intersection(scene["id"] for scene in imported)
        if colliding:
            raise ValueError(f"业务场景 id 已存在：{', '.join(sorted(colliding))}")
        return [self.save(scene) for scene in imported]


def build_test_plan(base_config: Mapping[str, Any], scenes: Sequence[Mapping[str, Any]],
                    level: str = "smoke", selected_condition_ids: Sequence[str] | None = None,
                    system_name: str = "") -> dict[str, Any]:
    """Build a run compatible with the existing case workflow and report writer."""
    if level not in LEVEL_LABELS:
        raise ValueError("测试级别必须是 smoke、standard、full 或 custom")
    if not isinstance(scenes, (list, tuple)) or not scenes:
        raise ValueError("至少选择一个业务场景")
    selected = [normalize_business_scene(scene) for scene in scenes]
    scene_ids = [scene["id"] for scene in selected]
    if len(scene_ids) != len(set(scene_ids)):
        raise ValueError("测试计划中存在重复的业务场景")
    if not isinstance(system_name, str) or len(system_name.strip()) > 80:
        raise ValueError("系统名称必须是 80 字以内的文本")
    catalog = {case["id"]: case for case in network_conditions()}
    if level == "custom":
        if not isinstance(selected_condition_ids, (list, tuple)) or not selected_condition_ids:
            raise ValueError("自定义级别必须选择至少一个网络条件")
        if any(not isinstance(item, str) for item in selected_condition_ids):
            raise ValueError("网络条件编号必须是文本")
        if len(selected_condition_ids) != len(set(selected_condition_ids)):
            raise ValueError("自定义网络条件编号重复")
        unknown = set(selected_condition_ids) - set(catalog)
        if unknown:
            raise ValueError(f"未知网络条件：{', '.join(sorted(unknown))}")
    elif selected_condition_ids is not None:
        raise ValueError("只有自定义级别可以指定网络条件")
    base = validate_config(base_config)
    rows = []
    for scene in selected:
        condition_ids = selected_condition_ids if level == "custom" else scene["network_case_ids"][level]
        for condition_id in condition_ids:
            condition = catalog[condition_id]
            config = dict(base)
            config.update(_NETWORK_DEFAULTS)
            config.update(condition["network"])
            config["name"] = f"{scene['name']} · {condition['title']}"
            rows.append({
                "id": f"{scene['id']}-{condition_id}",
                "group": scene["name"],
                "title": condition["title"],
                "steps": (f"前置条件：{scene['preconditions']}\n业务操作：{scene['steps']}\n"
                          f"网络场景操作：{condition['steps']}\n重点观察：{scene['observations']}"),
                "expected": f"业务通过标准：{scene['expected']}\n网络场景预期：{condition['expected']}",
                "estimated_minutes": condition.get("estimated_minutes", 4),
                "human_verdict": True,
                "business_scene_id": scene["id"],
                "business_scene_name": scene["name"],
                "network_case_id": condition_id,
                "config": validate_config(config),
                "result": "待测", "actual": "", "stats": None,
                "started_at": None, "finished_at": None, "elapsed_seconds": None,
            })
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    planned = sum(row["estimated_minutes"] for row in rows)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "template_id": f"business-plan-{level}",
        "template_name": f"{LEVEL_LABELS[level]}弱网测试计划",
        "template_source": "Motu Net",
        "system_name": system_name.strip(),
        "plan_level": level,
        "plan_level_name": LEVEL_LABELS[level],
        "business_scenes": deepcopy(selected),
        "started_at": started_at, "finished_at": None,
        "status": "进行中", "overall": "未完成",
        "target": base["filter"], "planned_minutes": planned,
        "time_budget_minutes": TEST_TIME_BUDGET_MINUTES,
        "within_time_budget": planned <= TEST_TIME_BUDGET_MINUTES,
        "cases": rows,
    }
