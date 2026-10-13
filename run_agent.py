"""A 的独立入口；D 的 FastAPI/前端可直接调用 AgentService。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from backend.agent.contracts import CAMPUS_TZ
from backend.agent.mock_tools import MockCampus
from backend.agent.service import AgentService
from backend.core.llm_client import HTTPModelClient


def display(response, *, show_trace=False, as_json=False):
    if as_json:
        print(response.model_dump_json(indent=2))
        return
    print("\nAgent [%s]：%s" % (response.status, response.answer))
    if show_trace:
        print("工具轨迹：", " -> ".join(t.tool_name + ("[ok]" if t.result.success else "[failed]") for t in response.trace) or "无")
    if response.pending_confirmation:
        print("输入 /confirm 执行，或 /cancel 取消。")


def demo_all(service, show_trace, as_json):
    examples = [
        ["明天下午帮我找一个安静的地方学习两个小时，并预约座位。"],
        ["我的学生证丢了，怎么办？", "生成学生证补办草稿，姓名：张三，学号：20260001"],
        ["下周有什么不与我上课冲突的计算机讲座？帮我报名。"],
        ["我们宿舍空调不制冷，帮我报修。", "3号楼502室"],
    ]
    for turns in examples:
        sid = None
        for text in turns:
            print("\n用户：", text)
            response = service.chat(user_id="demo-student", session_id=sid, message=text)
            sid = response.session_id
            display(response, show_trace=show_trace, as_json=as_json)
        if response.pending_confirmation:
            # 仅此显式 --demo-all 批量示例自动确认，交互模式和 API 均需人工确认。
            print("演示脚本模拟用户点击：确认")
            response = service.confirm(user_id="demo-student", session_id=sid,
                confirmation_id=response.pending_confirmation.confirmation_id, approved=True)
            display(response, show_trace=show_trace, as_json=as_json)


def main():
    parser = argparse.ArgumentParser(description="CampusFlow A：独立 Agent 开发包")
    parser.add_argument("--mode", choices=["demo", "llm"], default="demo")
    parser.add_argument("--demo-all", action="store_true", help="运行四场景，并模拟用户点击确认（只允许离线模式）")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--json", action="store_true", help="显示 D 可消费的结构化响应")
    parser.add_argument("--now", help="仅 demo：固定带时区 ISO 8601 时间，便于复现")
    args = parser.parse_args()
    if args.mode == "llm" and (args.demo_all or args.now):
        parser.error("--demo-all/--now 仅用于 demo，真实模型交互需手动确认")
    clock = lambda: datetime.now(CAMPUS_TZ)
    if args.now:
        now = datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            parser.error("--now 必须包含时区，例如 2026-10-13T09:00:00+08:00")
        clock = lambda: now.astimezone(CAMPUS_TZ)
    client = None
    if args.mode == "demo":
        service = AgentService.demo(clock=clock)
    else:
        load_dotenv(Path(__file__).with_name(".env"), override=False)
        try:
            client = HTTPModelClient.from_env()
        except ValueError as exc:
            parser.error(str(exc))
        service = AgentService(client, MockCampus(now=clock()).registry(), clock=clock)
    try:
        if args.demo_all:
            demo_all(service, args.trace, args.json)
            return
        print("CampusFlow A | 模式：%s | 业务数据全部为 Mock" % args.mode)
        print("/confirm 确认；/cancel 拒绝；/new 新会话；/quit 退出")
        sid, pending = None, None
        while True:
            try:
                text = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text == "/quit":
                break
            if text == "/new":
                if pending:
                    print("请先 /cancel 当前待确认操作。")
                else:
                    sid = None
                continue
            try:
                if text in {"/confirm", "/cancel"}:
                    if not pending:
                        print("没有待确认操作。")
                        continue
                    response = service.confirm(user_id="demo-student", session_id=sid,
                        confirmation_id=pending.confirmation_id, approved=text == "/confirm")
                else:
                    response = service.chat(user_id="demo-student", session_id=sid, message=text)
                sid, pending = response.session_id, response.pending_confirmation
                display(response, show_trace=args.trace, as_json=args.json)
            except ValueError as exc:
                print("请求未执行：", str(exc))
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    # Windows 旧版 Python 的 GBK 输出会导致重定向文件乱码，统一输出 UTF-8。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()

