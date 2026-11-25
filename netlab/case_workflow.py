"""Guided weak-network cases with operator verdicts and durable reports."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy
import json
import os
from pathlib import Path
import uuid

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFileDialog

from .case_suite import TEST_TIME_BUDGET_MINUTES, build_cases, budget_text, markdown_report, new_run, overall_result, planned_minutes
from .config import DEFAULT_CONFIG, validate_config
from .platform_win import is_admin
from .test_templates import TemplateStore, normalize_template, standard_template


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class CaseWorkflow:
    def init_case_workflow(self):
        self.case_run = None
        self.last_case_run = None
        self.case_index = -1
        self.case_report_path = None
        self.case_pending_result = None
        self.case_note_drafts = {}
        self.template_store = None
        self.template_catalog = [standard_template()]
        self.current_template = self.template_catalog[0]
        self.case_preview = build_cases(self.collect() if hasattr(self, "name_field") else DEFAULT_CONFIG, self.current_template)

    def initialize_test_templates(self):
        """Load built-ins and user templates after ConfigStore has resolved its data root."""
        self.template_store = TemplateStore(self.store.root / "test-templates")
        try:
            self.template_catalog = self.template_store.list()
        except (OSError, ValueError, TypeError) as exc:
            self.template_catalog = [standard_template()]
            self.append_log(f"读取测试模板失败，已使用内置模板：{exc}")
        if not self.template_catalog:
            self.template_catalog = [standard_template()]
        self.current_template = self.template_catalog[0]
        try:
            self.case_preview = build_cases(self.collect() if hasattr(self, "name_field") else DEFAULT_CONFIG, self.current_template)
        except (ValueError, TypeError) as exc:
            self.case_preview = build_cases(DEFAULT_CONFIG, self.current_template)
            self.append_log(f"当前目标配置无效，模板预览已使用默认目标：{exc}")
        if hasattr(self, "case_template"):
            self.case_template.blockSignals(True)
            self.case_template.clear()
            for template in self.template_catalog:
                self.case_template.addItem(template["name"], template["id"])
            self.case_template.blockSignals(False)
            self.render_case_flow()

    def select_case_template(self, index):
        if index < 0 or index >= len(self.template_catalog):
            return
        if self.case_run and self.case_run.get("status") == "进行中":
            return
        selected = self.template_catalog[index]
        try:
            preview = build_cases(self.collect(), selected)
        except (ValueError, TypeError) as exc:
            self.error(f"当前流量目标配置无效，无法切换模板：{exc}")
            return
        self.current_template = selected
        self.show_case_preview(preview)

    def show_case_preview(self, cases, *, plan_run=None):
        """Replace the pending preview without starting a network session.

        The business-scene planner supplies a complete run envelope. Keeping it
        as a preview lets the UI change level/scenes while a previous report is
        still available for export.
        """
        if self.case_run and self.case_run.get("status") == "进行中":
            return False
        rows = deepcopy(list(cases or []))
        if not rows:
            return False
        if self.case_run:
            self.last_case_run = self.case_run
        self.case_run = None
        self.case_plan_preview = deepcopy(plan_run) if plan_run else None
        self.case_preview = rows
        self.case_index = -1
        if hasattr(self, "case_list"):
            self.case_list.clear()
            for case in rows:
                self.case_list.addItem("")
                self._render_case_item(self.case_list.item(self.case_list.count() - 1), case)
            self.case_list.setCurrentRow(0)
        self.render_case_flow()
        return True

    def clear_case_preview(self):
        if self.case_run and self.case_run.get("status") == "进行中":
            return
        if self.case_run:
            self.last_case_run = self.case_run
        self.case_run = None
        self.case_plan_preview = None
        self.case_preview = []
        self.case_index = -1
        self.case_list.clear()
        for name in ("case_title", "case_profile", "case_steps", "case_expected"):
            getattr(self, name).setText("")
        self.case_actual.clear()
        self.render_case_flow()

    def import_test_template(self):
        if not self.template_store:
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入弱网测试模板", "", "Motu Net 模板 (*.json)")
        if not path:
            return
        try:
            template = self.template_store.import_file(path)
            self.template_catalog = self.template_store.list()
            self.current_template = next(item for item in self.template_catalog if item["id"] == template["id"])
            self.case_template.blockSignals(True)
            self.case_template.clear()
            for item in self.template_catalog:
                self.case_template.addItem(item["name"], item["id"])
            self.case_template.setCurrentIndex(self.template_catalog.index(self.current_template))
            self.case_template.blockSignals(False)
            self.select_case_template(self.case_template.currentIndex())
            self.append_log(f"已导入弱网测试模板：{template['name']}")
        except (OSError, ValueError, TypeError) as exc:
            self.error(f"导入测试模板失败：{exc}")

    def export_test_template(self):
        if not self.current_template:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出弱网测试模板", f"{self.current_template['id']}.json", "Motu Net 模板 (*.json)")
        if not path:
            return
        try:
            if self.template_store:
                self.template_store.export(path, self.current_template)
            else:
                Path(path).write_text(json.dumps({"schema_version": 1, "template": self.current_template}, ensure_ascii=False, indent=2), encoding="utf-8")
            self.append_log(f"已导出弱网测试模板：{path}")
        except OSError as exc:
            self.error(f"导出测试模板失败：{exc}")

    def write_case_run(self):
        path = self.case_report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.case_run, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise
        report_path = path.with_suffix(".md")
        report_temp = report_path.with_name(report_path.name + ".tmp")
        try:
            report_temp.write_text(markdown_report(self.case_run), encoding="utf-8")
            os.replace(report_temp, report_path)
        except OSError:
            report_temp.unlink(missing_ok=True)
            raise

    def start_case_flow(self, plan_run=None, *, plan_cases=None, level=None):
        # QPushButton.clicked may pass its checked-state boolean to a slot with
        # optional arguments. A direct template-mode click means no plan.
        if isinstance(plan_run, bool):
            plan_run = None
        if self.busy or self.session_active or self.update_busy:
            self.error("请先停止当前单场景测试，再开始弱网用例流程。")
            return
        if self.case_run and self.case_run["status"] == "进行中":
            return
        if not is_admin():
            self.error("请先以管理员身份重启后使用，再开始弱网用例测试。", title="需要管理员权限")
            return
        previous_run = self.case_run
        previous_last_run = self.last_case_run
        previous_report_path = self.case_report_path
        previous_index = self.case_index
        try:
            base = validate_config(self.collect())
            if plan_run is not None:
                run = deepcopy(plan_run)
                if not isinstance(run, dict) or not isinstance(run.get("cases"), list) or not run["cases"]:
                    raise ValueError("测试计划缺少可执行用例")
                # The plan builder already validates each row. Revalidate the
                # target config here because the UI may have changed it since
                # the preview was generated.
                for case in run["cases"]:
                    case["config"] = validate_config(case["config"])
                    if case["config"]["filter"] != base["filter"]:
                        raise ValueError("流量目标已变化，请重新生成测试计划")
                    case.setdefault("group", case.get("business_scene_name", "通用弱网"))
                    case.setdefault("actual", "")
                    case.setdefault("reason", "")
                    case.setdefault("stats", None)
                    case.setdefault("started_at", None)
                    case.setdefault("finished_at", None)
                    case["result"] = "待测"
                run["status"] = "进行中"
                run["overall"] = "未完成"
                run["finished_at"] = None
                run["started_at"] = now()
                run["target"] = base["filter"]
                run.setdefault("system_name", self.case_system.text().strip())
            else:
                run = new_run(base, self.current_template, self.case_system.text().strip(), cases=plan_cases, level=level)
            run["planned_minutes"] = planned_minutes(run["cases"])
            run.setdefault("time_budget_minutes", TEST_TIME_BUDGET_MINUTES)
            run["within_time_budget"] = run["planned_minutes"] <= run["time_budget_minutes"]
            if not run["within_time_budget"]:
                raise ValueError(
                    f"本轮预计 {run['planned_minutes']} 分钟，超过 {run['time_budget_minutes']} 分钟预算；"
                    "请减少业务场景、降低测试级别或改用自定义条件。"
                )
            for case in run["cases"]:
                self.engine.validate_filter(case["config"]["filter"])
            self.case_run = run
            self.last_case_run = None
            self.case_index = 0
            self.case_note_drafts = {}
            filename = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json"
            self.case_report_path = self.store.root / "test-runs" / filename
            self.write_case_run()
        except (OSError, ValueError, RuntimeError) as exc:
            self.case_run = previous_run
            self.last_case_run = previous_last_run
            self.case_report_path = previous_report_path
            self.case_index = previous_index
            self.error(f"无法创建测试记录：{exc}")
            return
        self.record("case_run_started", path=str(self.case_report_path))
        self.begin_case()

    def begin_case(self):
        case = self.case_run["cases"][self.case_index]
        case["result"] = "进行中"
        case["started_at"] = now()
        try:
            self.write_case_run()
        except OSError as exc:
            self.abort_case_run(f"无法保存用例进度：{exc}")
            self.error(str(exc))
            return
        self.case_list.setCurrentRow(self.case_index)
        self.render_case_flow()
        if not self._start_config(case["config"], persist=False, require_admin=False):
            self.abort_case_run("网络条件启动前校验失败")

    def mark_case(self, result, reason=None):
        if not self.case_run or self.case_run["status"] != "进行中" or self.busy:
            return
        if self.case_list.currentRow() != self.case_index:
            return
        case = self.case_run["cases"][self.case_index]
        if case["result"] != "进行中":
            return
        actual = self.case_actual.toPlainText().strip()
        reason = str(reason if reason is not None else actual).strip() if result in ("不适用", "跳过") else ""
        if result == "不通过" and not actual:
            self.error("请填写实际现象，便于定位未通过的原因。")
            return
        if result in ("不适用", "跳过") and not reason:
            self.error(f"请填写{result}原因，便于复盘测试范围。")
            return
        snapshot = self.engine.snapshot()
        if result == "通过" and snapshot.get("received", 0) <= 0:
            self.error("尚未捕获匹配流量，不能判定通过。请确认目标 IP / 端口，并在被测系统中执行用例操作。")
            return
        if result == "通过" and snapshot.get("error"):
            self.error("网络引擎出现错误，不能判定通过；请检查日志或记录为不通过。")
            return
        self.case_pending_result = (result, actual, reason, snapshot)
        self.render_case_flow()
        if self.session_active or snapshot.get("running"):
            self.stop_test()
        else:
            self.complete_case()

    def complete_case(self):
        if not self.case_pending_result or not self.case_run or self.case_run["status"] != "进行中":
            return
        result, actual, reason, _ = self.case_pending_result
        self.case_pending_result = None
        case = self.case_run["cases"][self.case_index]
        snapshot = self.engine.snapshot()
        finished_at = now()
        elapsed_seconds = None
        try:
            elapsed_seconds = max(0, (datetime.fromisoformat(finished_at) -
                                     datetime.fromisoformat(case["started_at"])).total_seconds())
        except (TypeError, ValueError, KeyError):
            pass
        case.update(result=result, actual=actual, reason=reason, stats=snapshot,
                    finished_at=finished_at, elapsed_seconds=elapsed_seconds)
        self.record("case_result", case_id=case["id"], result=result, actual=actual, reason=reason)
        finished = self.case_index + 1 == len(self.case_run["cases"])
        if finished:
            self.case_run["status"] = "已完成"
            self.case_run["finished_at"] = now()
        self.case_run["overall"] = overall_result(self.case_run["cases"])
        try:
            self.write_case_run()
        except OSError as exc:
            self.case_run.update(status="保存失败", finished_at=now())
            self.append_log(f"弱网用例报告保存失败：{exc}")
            self.error(f"保存用例结果失败：{exc}")
            self.render_case_flow()
            self.set_running_controls()
            return
        self.case_index += 1
        if finished:
            self.append_log(f"弱网用例测试结束：{self.case_run['overall']}；报告保存在 {self.case_report_path}")
            self.case_list.setCurrentRow(len(self.case_run["cases"]) - 1)
            self.render_case_flow()
            self.set_running_controls()
        else:
            self.begin_case()

    def abort_case_run(self, reason="测试人员停止"):
        if not self.case_run or self.case_run["status"] != "进行中":
            return
        self.case_pending_result = None
        case = self.case_run["cases"][self.case_index]
        if case["result"] == "进行中":
            case.update(result="未执行", actual=reason, reason=reason, stats=self.engine.snapshot(), finished_at=now())
        self.case_run.update(status="已中止", finished_at=now(), overall=overall_result(self.case_run["cases"]))
        try:
            self.write_case_run()
        except OSError as exc:
            self.append_log(f"弱网用例报告保存失败：{exc}")
        self.record("case_run_aborted", reason=reason)
        self.render_case_flow()
        self.set_running_controls()

    def stop_case_flow(self):
        self.abort_case_run()
        self.stop_test()

    def case_job_done(self, name, error):
        if not self.case_run or self.case_run["status"] != "进行中":
            return
        if error:
            self.abort_case_run(f"网络操作失败：{error}")
        elif name == "stop" and self.case_pending_result:
            self.complete_case()
        else:
            self.render_case_flow()

    def case_session_finished(self, snapshot, reason):
        if self.case_run and self.case_run["status"] == "进行中":
            case = self.case_run["cases"][self.case_index]
            case["stats"] = snapshot
            if snapshot.get("error"):
                self.abort_case_run(f"网络引擎停止：{reason}")
            else:
                try:
                    self.write_case_run()
                except OSError as exc:
                    self.append_log(f"用例统计保存失败：{exc}")
                self.render_case_flow()

    def select_case_for_review(self, index):
        rows = self.case_run["cases"] if self.case_run else self.case_preview
        if index < 0 or index >= len(rows):
            return
        case = rows[index]
        self.case_title.setText(f"{case['id']}  {case.get('group', '通用弱网')} · {case['title']}")
        self.case_steps.setText(case["steps"])
        self.case_expected.setText(case["expected"])
        config = case["config"]
        if config["blackout"]:
            profile = f"{config['start_delay_s']} 秒后断网，持续 {config['blackout_duration_s']} 秒"
        else:
            profile = f"延迟 {config['delay_ms']:g} ms · 丢包 {config['loss_pct']:g}% · 带宽 {config['bandwidth_kbps']:g} KiB/s"
        self.case_profile.setText(f"预计用时 {case.get('estimated_minutes', 4)} 分钟 · {profile}")
        self.case_actual.blockSignals(True)
        self.case_actual.setPlainText(self.case_note_drafts.get(index, case["actual"]))
        self.case_actual.blockSignals(False)
        self.render_case_flow()

    def remember_case_note(self):
        if self.case_run and self.case_run["status"] == "进行中" and self.case_list.currentRow() == self.case_index:
            self.case_note_drafts[self.case_index] = self.case_actual.toPlainText()

    def _render_case_item(self, item, case):
        width = max(120, self.case_list.viewport().width() - 24)
        metrics = self.case_list.fontMetrics()
        prefix = f"{case['id']} · {case['result']}"
        item.setText(f"{metrics.elidedText(prefix, Qt.TextElideMode.ElideRight, width)}\n"
                     f"{metrics.elidedText(case['title'], Qt.TextElideMode.ElideRight, width)}")
        item.setSizeHint(QSize(width, metrics.height() * 2 + 34))
        item.setToolTip(f"{case['id']} · {case['title']} · {case['result']}")

    def render_case_flow(self):
        if not hasattr(self, "case_list"):
            return
        rows = self.case_run["cases"] if self.case_run else self.case_preview
        if hasattr(self, "case_list") and self.case_list.count() != len(rows):
            selected = min(max(self.case_list.currentRow(), 0), max(0, len(rows) - 1))
            self.case_list.clear()
            for case in rows:
                self.case_list.addItem("")
            if rows:
                self.case_list.setCurrentRow(selected)
        for index, case in enumerate(rows):
            self._render_case_item(self.case_list.item(index), case)
        running = bool(self.case_run and self.case_run["status"] == "进行中")
        done = sum(case["result"] in ("通过", "不通过", "不适用", "跳过") for case in rows)
        overall = overall_result(rows)
        planned = planned_minutes(rows)
        budget = ((self.case_run or getattr(self, "case_plan_preview", None) or {}).get(
            "time_budget_minutes", TEST_TIME_BUDGET_MINUTES))
        self.case_progress.setText(f"{done} / {len(rows)} 已判定  ·  预计 {planned} / {budget} 分钟  ·  总体：{overall}")
        self.case_state.setText(self.case_run["status"] if self.case_run else "待开始")
        template_name = self.case_run.get("template_name") if self.case_run else self.current_template.get("name")
        preview_run = getattr(self, "case_plan_preview", None) if not self.case_run else None
        if preview_run:
            template_name = preview_run.get("template_name", template_name)
        target = self.case_run["target"] if self.case_run else (preview_run.get("target") if preview_run else self.filter_preview.text())
        self.case_target.setText(f"模板：{template_name}  ·  {budget_text(rows, budget)}  ·  流量目标：{target}")
        selected = self.case_list.currentRow()
        current = running and selected == self.case_index
        can_mark = current and not self.busy and self.case_pending_result is None
        self.case_actual.setEnabled(can_mark)
        self.case_pass.setEnabled(can_mark)
        self.case_fail.setEnabled(can_mark)
        for name in ("case_na", "case_not_applicable", "case_skip"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(can_mark)
        over_budget = planned > budget
        self.case_start.setEnabled(bool(rows) and not over_budget and not running and not self.busy and not self.session_active and not self.update_busy)
        self.case_start.setToolTip(
            "已超过 60 分钟预算，请减少场景或降低测试级别。" if over_budget else
            "按计划逐项执行弱网用例，并在每项完成后记录结果。"
        )
        self.case_start.setText("开始新一轮" if self.case_run else "▶  开始测试")
        self.case_template.setEnabled(not running and not self.busy and not self.session_active)
        self.case_template_import.setEnabled(not running and not self.busy and not self.session_active)
        self.case_template_export.setEnabled(bool(self.current_template) and not self.busy)
        self.case_system.setEnabled(not running and not self.busy and not self.session_active)
        plan_mode = getattr(getattr(self, "case_mode", None), "currentData", lambda: None)() == "plan"
        can_configure = not running and not self.busy and not self.session_active and not self.update_busy
        for name in ("case_mode", "case_business", "case_level", "case_business_new", "case_business_import"):
            control = getattr(self, name, None)
            if control is not None:
                control.setEnabled(can_configure and (name == "case_mode" or plan_mode))
        edit_button = getattr(self, "case_business_edit", None)
        if edit_button is not None:
            selected_scene = self.case_business.currentData() if hasattr(self, "case_business") else None
            edit_button.setEnabled(can_configure and plan_mode and selected_scene not in
                                   ("all", "login", "lobby", "battle", "transaction"))
        conditions_button = getattr(self, "case_condition_button", None)
        if conditions_button is not None:
            conditions_button.setEnabled(can_configure and plan_mode and self.case_level.currentData() == "custom")
        self.case_stop.setVisible(running)
        self.case_stop.setEnabled(running and not self.busy)
        self.case_export.setEnabled(self.case_run is not None or self.last_case_run is not None)
        if self.case_run:
            self.case_result.setText(f"本轮结论：{overall}  ·  {budget_text(rows, budget)}  ·  JSON / Markdown 报告：{self.case_report_path.parent}")
        else:
            preview_run = getattr(self, "case_plan_preview", None)
            if preview_run:
                self.case_result.setText(f"{preview_run.get('plan_level_name', preview_run.get('template_name', '测试计划'))}：已生成 {len(rows)} 项，{budget_text(rows, budget)}；业务表现由测试人员逐项判定。")
            else:
                self.case_result.setText(f"选定流量目标后开始；{budget_text(rows, budget)}；业务表现由测试人员逐项判定。")

    def export_case_report(self):
        run = self.case_run or self.last_case_run
        if not run:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出弱网测试报告", "MotuNet-弱网测试报告.md", "Markdown (*.md)")
        if not path:
            return
        try:
            Path(path).write_text(markdown_report(run), encoding="utf-8")
            self.append_log(f"弱网用例报告已导出：{path}")
        except OSError as exc:
            self.error(str(exc))
