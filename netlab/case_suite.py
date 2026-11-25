"""Built-in, operator-verified weak-network regression suite."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from .config import DEFAULT_CONFIG, PRESETS, validate_config
from .test_templates import TEST_TIME_BUDGET_MINUTES, normalize_template, standard_template


CASES = (
    ("WN-01", "正常网络对照", 7, "登录并进入核心界面；完成一次查询或交互，记录正常耗时。",
     "连接与数据正常；操作只执行一次，作为后续弱网对照。"),
    ("WN-02", "轻度弱网", 0, "重复登录、页面切换及一次提交操作。",
     "允许适度变慢；界面可用，提交不重复，数据最终一致。"),
    ("WN-03", "高延迟", 2, "快速连续触发查询或交互，观察等待和返回顺序。",
     "有明确等待反馈；不会卡死、显示过期结果或重复提交。"),
    ("WN-04", "波动与抖动", 1, "持续进行核心操作并切换界面，观察超时与重试。",
     "短暂波动后可继续使用；失败提示明确，不丢失已确认状态。"),
    ("WN-05", "高丢包", 3, "执行一次重要操作并观察超时、重试及最终状态。",
     "不会无提示丢失操作；重试可控，服务端结果和界面一致。"),
    ("WN-06", "低带宽", 4, "打开资源较多的页面并执行核心操作。",
     "有合理加载反馈；不出现无限加载或重复请求，完成后内容正确。"),
    ("WN-07", "短时断网", 5, "正在操作时断网，观察提示；恢复后再次查询状态。",
     "断网有明确提示；恢复后可重连，数据不重复、不丢失。"),
    ("WN-08", "延迟断网", 6, "启动后先在正常网络操作；约 10 秒后断网，观察恢复。",
     "断网阶段提示与状态正确；恢复后能继续操作，无错误状态残留。"),
    ("WN-09", "较长断网恢复", None, "进行核心操作后保持断网约 30 秒，观察超时；恢复后重新进入功能。",
     "超时或重连提示明确；恢复后状态一致，不会重复提交。"),
)


def build_cases(base_config, template=None):
    """Build executable cases from a reusable template and a traffic target."""
    base = validate_config(base_config)
    template = normalize_template(template or standard_template())
    reset_keys = (*PRESETS[0]["values"].keys(), "duration_s", "start_delay_s",
                  "blackout_duration_s", "blackout", "blackout_loop", "blackout_mode", "adapter_ids")
    cases = []
    for row in template["cases"]:
        config = dict(base)
        for key in reset_keys:
            config[key] = DEFAULT_CONFIG[key]
        config.update(row.get("network", {}))
        if config.get("blackout") and "blackout_duration_s" not in row.get("network", {}):
            config["blackout_duration_s"] = max(1, int(config.get("duration_s", 10)))
        config["name"] = row["title"]
        cases.append({"id": row["id"], "group": row.get("group", "通用弱网"), "title": row["title"],
                      "steps": row["steps"], "expected": row["expected"],
                      "estimated_minutes": row.get("estimated_minutes", 4),
                      "human_verdict": row.get("human_verdict", True),
                      "config": validate_config(config), "result": "待测", "actual": "", "reason": "",
                      "stats": None, "started_at": None, "finished_at": None,
                      "elapsed_seconds": None})
    return cases


def planned_minutes(cases):
    """Return the operator-facing estimate for a set of cases."""
    return sum(max(1, int(case.get("estimated_minutes", 4))) for case in cases)


def budget_text(cases, budget=TEST_TIME_BUDGET_MINUTES):
    total = planned_minutes(cases)
    return f"预计 {total} / {budget} 分钟" + (" · 超出时长预算" if total > budget else "")


def _actual_minutes(case):
    elapsed = case.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)) and elapsed >= 0:
        return f"{elapsed / 60:.1f} 分钟"
    started, finished = case.get("started_at"), case.get("finished_at")
    try:
        if started and finished:
            seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
            return f"{max(0, seconds) / 60:.1f} 分钟"
    except (TypeError, ValueError):
        pass
    return "未完成"


def overall_result(cases):
    results = [case["result"] for case in cases]
    if any(result == "不通过" for result in results):
        return "不通过"
    if results and all(result == "不适用" for result in results):
        return "不适用"
    if "通过" in results and all(result in ("通过", "不适用") for result in results):
        return "通过"
    return "未完成"


def new_run(base_config, template=None, system_name="", *, cases=None, level=None):
    base = validate_config(base_config)
    selected = normalize_template(template or standard_template()) if cases is None or template is not None else None
    rows = build_cases(base, selected) if cases is None else deepcopy(list(cases))
    if not rows:
        raise ValueError("测试计划至少需要一条用例")
    seen_ids = set()
    for row in rows:
        case_id = row["id"]
        if case_id in seen_ids:
            raise ValueError(f"测试计划用例编号重复：{case_id}")
        seen_ids.add(case_id)
        row["config"] = validate_config(row["config"])
        row.setdefault("estimated_minutes", 4)
        row.update(result="待测", actual="", reason="", stats=None, started_at=None,
                   finished_at=None, elapsed_seconds=None)
    planned = planned_minutes(rows)
    return {"schema_version": 1, "template_id": selected["id"] if selected else "business-plan",
            "template_name": selected["name"] if selected else "业务场景测试计划",
            "template_source": selected.get("source", "") if selected else "Motu Net",
            "test_level": level or "",
            "system_name": str(system_name or "").strip(),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "finished_at": None, "status": "进行中", "overall": "未完成",
            "target": base["filter"], "planned_minutes": planned,
            "time_budget_minutes": TEST_TIME_BUDGET_MINUTES,
            "within_time_budget": planned <= TEST_TIME_BUDGET_MINUTES,
            "cases": rows}


def _grouped_cases(cases):
    groups = {}
    for case in cases:
        name = case.get("business_scene_name") or case.get("group") or "通用弱网"
        key = (case.get("business_scene_id") or name, name)
        groups.setdefault(key, []).append(case)
    return groups


def _network_summary(config):
    blackout = (f"{config.get('start_delay_s', 0)} 秒后断网，持续 {config.get('blackout_duration_s', 0)} 秒；"
                if config.get("blackout") else "")
    return (blackout + f"延迟 {config.get('delay_ms', 0):g} ms；抖动 {config.get('jitter_ms', 0):g} ms；"
            f"丢包 {config.get('loss_pct', 0):g}%；带宽 {config.get('bandwidth_kbps', 0):g} KiB/s；"
            f"重复 {config.get('duplicate_pct', 0):g}%；乱序 {config.get('reorder_pct', 0):g}%")


def _md_cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown_report(run):
    cases = run["cases"]
    passed = sum(case["result"] == "通过" for case in cases)
    failed = sum(case["result"] == "不通过" for case in cases)
    not_applicable = sum(case["result"] == "不适用" for case in cases)
    skipped = sum(case["result"] == "跳过" for case in cases)
    pending = len(cases) - passed - failed - not_applicable - skipped
    lines = ["# Motu Net 弱网测试报告", "", f"测试对象：{run.get('system_name') or '未填写'}",
             f"模板：{run.get('template_name', '标准弱网回归')}",
             f"模板来源：{run.get('template_source') or 'Motu Net'}", f"开始时间：{run['started_at']}",
             f"结束时间：{run.get('finished_at') or '未结束'}", f"执行状态：**{run.get('status', '进行中')}**",
             f"流量过滤：`{run['target']}`",
             f"预计时长：{run.get('planned_minutes', planned_minutes(cases))} / "
             f"{run.get('time_budget_minutes', TEST_TIME_BUDGET_MINUTES)} 分钟",
             f"总体结论：**{overall_result(cases)}**（通过 {passed} / 不通过 {failed} / 不适用 {not_applicable} / 跳过 {skipped} / 未完成 {pending} / 总计 {len(cases)}）", ""]
    level_name = run.get("plan_level_name") or run.get("test_level")
    if level_name:
        lines.insert(4, f"测试级别：{level_name}")
    for (_, group_name), group_cases in _grouped_cases(cases).items():
        lines.extend([f"## {group_name}", "", "| 编号 | 网络条件 | 预计(分) | 结果 | 捕获包 | 原因 |",
                      "| --- | --- | ---: | --- | ---: | --- |"])
        for case in group_cases:
            stats = case.get("stats") or {}
            lines.append(f"| {_md_cell(case['id'])} | {_md_cell(case['title'])} | {case.get('estimated_minutes', 4)} | {case['result']} | "
                         f"{stats.get('received', 0)} | {_md_cell(case.get('reason') or '')} |")
        for case in group_cases:
            stats = case.get("stats") or {}
            lines.extend(["", f"### {case['id']} {case['title']} · {case['result']}", "",
                          f"操作：{case['steps']}", "", f"预期：{case['expected']}", "",
                          f"预计用时：{case.get('estimated_minutes', 4)} 分钟", "",
                          f"实际用时：{_actual_minutes(case)}", "",
                          f"实际：{case.get('actual') or '未填写'}", ""])
            if case.get("reason"):
                lines.extend([f"原因：{case['reason']}", ""])
            lines.extend([f"网络条件：{_network_summary(case['config'])}",
                          f"流量过滤：`{case['config']['filter']}`",
                          f"捕获 {stats.get('received', 0)} 包，丢弃 {stats.get('dropped', 0)} 包；"
                          f"错误 {stats.get('errors', 0)}。", ""])
    lines.extend(["", "结论由测试人员判定；流量计数只能证明匹配情况，不代表业务功能自动通过。", ""])
    return "\n".join(lines)
