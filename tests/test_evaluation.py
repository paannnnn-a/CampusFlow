import pytest

from backend.agent.planner import DemoPlanner, say
from backend.core.llm_client import ModelError
from evaluate_agent import run_case, run_suite
from test_agent import ScriptedModel


def test_demo_behavior_suite_passes_all_eight_cases():
    report = run_suite(DemoPlanner())
    assert report["summary"] == {"passed": 8, "failed": 0, "skipped": 0}
    assert all(case["checks"] for case in report["cases"])


def test_evaluator_does_not_accept_unsupported_text_as_tool_success():
    case = run_case("schedule_today", ScriptedModel(say("今天没有课")))
    assert case["status"] == "failed"
    assert any(not c["passed"] and "实际调用" in c["check"] for c in case["checks"])


def test_evaluator_aborts_other_cases_after_model_connection_failure():
    model = ScriptedModel(ModelError("模型服务请求失败（HTTP 401）"))
    report = run_suite(model, mode="llm")
    assert report["summary"] == {"passed": 0, "failed": 1, "skipped": 7}
    assert len(model.requests) == 1
