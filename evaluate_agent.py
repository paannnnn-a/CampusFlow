"""固定校园场景的行为验收：规则或真实模型 + 每场景新建的 Mock 业务。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from time import perf_counter

from dotenv import load_dotenv

from backend.agent.mock_tools import MockCampus
from backend.agent.planner import DemoPlanner
from backend.agent.service import AgentService
from backend.core.llm_client import HTTPModelClient

FIXED_NOW = datetime.fromisoformat("2026-10-13T09:00:00+08:00")
CASES = {
    "schedule_today": ["今天有什么课？请查询我的课表。"],
    "missing_date": ["帮我查询课表。"],
    "study_confirm": ["明天下午找一个安静的座位学习两个小时，并帮我预约。"],
    "study_cancel": ["明天下午找一个座位学习两个小时，并帮我预约。"],
    "repair_followup": ["我们宿舍空调不制冷，帮我报修。", "3号楼502室"],
    "affair_sources": ["学生证丢了，补办需要什么材料和流程？"],
    "affair_draft": ["学生证丢了，我想生成补办申请草稿。", "姓名：张三，学号：20260001，请生成草稿。"],
    "activity_conflict": ["下周有什么不与我上课冲突的计算机讲座？帮我报名一场无课表冲突的。"],
}
WRITE_CASES = {"study_confirm", "study_cancel", "repair_followup", "affair_draft", "activity_conflict"}


def run_case(name, model):
    campus = MockCampus(now=FIXED_NOW)
    registry = campus.registry()
    # 此 runner 从不接收外部 registry，自动确认只可能作用于本场景的新建 Mock。
    assert all(registry.is_simulated(spec.name) for spec in registry.specs.values())
    service = AgentService(model, registry, clock=lambda: FIXED_NOW)
    responses, sid, checks = [], None, []
    started = perf_counter()

    def check(label, condition):
        checks.append({"check": label, "passed": bool(condition)})

    def record_count():
        return sum(len(records) for records in [campus.reservations, campus.registrations, campus.repairs, campus.drafts])

    for index, text in enumerate(CASES[name]):
        response = service.chat(user_id="evaluation-student", session_id=sid, message=text)
        sid = response.session_id
        responses.append(response)
        if index < len(CASES[name]) - 1:
            check("信息补全前不创建待提交动作", response.pending_confirmation is None)
            check("信息补全前没有业务写入", record_count() == 0)
        if response.status == "failed" or response.pending_confirmation:
            break
    pending = responses[-1].pending_confirmation
    check("确认前没有业务写入", record_count() == 0)
    if name in WRITE_CASES:
        check("应产生待确认动作", pending is not None)
        if pending:
            if name in {"study_confirm", "study_cancel"}:
                check("预约工具正确", pending.tool_name == "book_study_seat")
                args = pending.arguments
                try:
                    start, end = map(datetime.fromisoformat, [args["start_time"], args["end_time"]])
                    valid_interval = start.date().isoformat() == "2026-10-14" and start.hour >= 12 and end.hour <= 18 and (end - start).total_seconds() == 7200
                except (KeyError, ValueError, TypeError):
                    valid_interval = False
                check("规范化为明天下午连续两小时", valid_interval)
            if name == "repair_followup":
                check("报修工具与位置正确", pending.tool_name == "create_repair_ticket" and pending.arguments.get("location") == "3号楼502室")
                check("保留上轮空调不制冷故障", "空调" in pending.arguments.get("problem", "") and "制冷" in pending.arguments.get("problem", ""))
            if name == "affair_draft":
                fields = pending.arguments.get("fields", {})
                check("草稿字段正确", pending.tool_name == "create_affair_draft" and fields.get("name") == "张三" and fields.get("student_id") == "20260001")
            if name == "activity_conflict":
                check("排除课表冲突的活动", pending.tool_name == "register_activity" and pending.arguments.get("activity_id") == "ACT-FREE")
            response = service.confirm(user_id="evaluation-student", session_id=sid,
                confirmation_id=pending.confirmation_id, approved=name != "study_cancel")
            responses.append(response)
            check("本次确认后不出现额外待提交动作", response.pending_confirmation is None)
        check("最终状态正确", responses[-1].status == ("cancelled" if name == "study_cancel" else "completed"))
        check("业务记录数量正确", record_count() == (0 if name == "study_cancel" else 1))
    else:
        check("只读场景不请求写操作确认", pending is None)
        check("本轮正常结束", responses[-1].status == "completed")

    # 确认响应包含本轮前置轨迹；按 operation_id 去重后进行行为判断。
    seen = {}
    for response in responses:
        for entry in response.trace:
            seen.setdefault(entry.operation_id, entry)
    trace = list(seen.values())
    names = [entry.tool_name for entry in trace if entry.result.success]
    if name == "schedule_today":
        check("实际调用课表工具且使用可信当天", any(t.tool_name == "get_schedule" and t.arguments.get("date") == "2026-10-13" for t in trace))
    if name == "missing_date":
        check("缺少日期时补问而不自行查询", "get_schedule" not in names and bool(responses[-1].answer.strip()))
    if name.startswith("study_"):
        reads = ["get_schedule", "find_free_slots", "find_available_seats"]
        check("前置查询齐全", all(tool in names for tool in reads))
        if all(tool in names for tool in reads):
            check("前置查询顺序正确", [names.index(tool) for tool in reads] == sorted(names.index(tool) for tool in reads))
    if name in {"affair_sources", "affair_draft"}:
        check("查询知识与办理要求", "search_campus_knowledge" in names and "get_affair_requirements" in names)
        if name == "affair_sources":
            check("回答引用来源", any(marker in responses[-1].answer for marker in ["MOCK-POLICY-001", "学生证补办演示办法"]))
    if name == "activity_conflict":
        days = {t.arguments.get("date") for t in trace if t.tool_name == "get_schedule" and t.result.success}
        check("查询候选活动所在日期的课表", {"2026-10-19", "2026-10-20"}.issubset(days))
    if trace:
        check("全部业务结果保持模拟属性", all(t.result.simulated for t in trace))
    return {"case": name, "status": "passed" if all(c["passed"] for c in checks) else "failed",
            "checks": checks, "duration_seconds": round(perf_counter() - started, 3),
            "responses": [r.model_dump(mode="json") for r in responses],
            "record_count": record_count()}


def run_suite(model, names=None, *, mode="demo", configuration=None):
    cases = []
    for name in names or CASES:
        result = run_case(name, model)
        cases.append(result)
        # 模型基础调用已失败时终止，避免错误配置对所有场景重复发请求。
        if any(r["status"] == "failed" and not r["trace"] for r in result["responses"]):
            break
    expected = list(names or CASES)
    for name in expected[len(cases):]:
        cases.append({"case": name, "status": "skipped", "reason": "前序模型调用失败，请先修正模型配置"})
    return {"mode": mode, "fixed_campus_time": FIXED_NOW.isoformat(), "business_backend": "fresh in-memory Mock per case",
            "configuration": configuration, "grading": "结构化行为检查；不等同于全面语义质量评估",
            "summary": {status: sum(c["status"] == status for c in cases) for status in ["passed", "failed", "skipped"]}, "cases": cases}


def main(argv=None):
    parser = argparse.ArgumentParser(description="四类校园场景与确认流程的固定行为验收")
    parser.add_argument("--mode", choices=["demo", "llm"], default="demo")
    parser.add_argument("--case", action="append", choices=list(CASES), help="先选一个场景试跑，可重复指定")
    parser.add_argument("--output", help="JSON 报告路径，默认 docs/evaluation_demo.json 或 evaluation_llm.json")
    args = parser.parse_args(argv)
    model, config = DemoPlanner(), None
    if args.mode == "llm":
        load_dotenv(Path(__file__).with_name(".env"), override=False)
        try:
            model = HTTPModelClient.from_env()
        except ValueError as exc:
            print("模型配置未就绪：" + str(exc))
            return 2
        config = model.config.safe_summary()
    try:
        report = run_suite(model, args.case, mode=args.mode, configuration=config)
    finally:
        if args.mode == "llm":
            model.close()
    output = Path(args.output) if args.output else Path(__file__).with_name("docs") / ("evaluation_%s.json" % args.mode)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for case in report["cases"]:
        print("%s: %s" % (case["case"], case["status"]))
        for check in case.get("checks", []):
            if not check["passed"]:
                print("  未通过：" + check["check"])
    print("验收汇总：" + json.dumps(report["summary"], ensure_ascii=False))
    print("报告：" + str(output.absolute()))
    return 0 if not report["summary"]["failed"] and not report["summary"]["skipped"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
