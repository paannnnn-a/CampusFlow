"""真实模式由模型决策；DemoPlanner 是明确受限的规则演示器，非大模型。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .contracts import CAMPUS_TZ
from .mock_tools import at, overlaps


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    arguments: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    type: str = Field(pattern="^function$")
    function: FunctionCall


def normalize_reply(raw):
    if not isinstance(raw, dict) or raw.get("role") != "assistant":
        raise ValueError("需要 assistant 消息")
    calls = [ToolCall.model_validate(c).model_dump() for c in raw.get("tool_calls", [])]
    if len({c["id"] for c in calls}) != len(calls):
        raise ValueError("重复调用 ID")
    content = raw.get("content") or ""
    if not isinstance(content, str) or (not content.strip() and not calls):
        raise ValueError("空模型回复")
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    if raw.get("_response_items"):
        message["_response_items"] = raw["_response_items"]
    if isinstance(raw.get("_chat_fields", {}).get("reasoning_content"), str):
        message["_chat_fields"] = {"reasoning_content": raw["_chat_fields"]["reasoning_content"]}
    return message, calls


def say(text):
    return {"role": "assistant", "content": text}


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_" + uuid4().hex, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class DemoPlanner:
    """支持 README 中的四类固定中文场景。开放式理解需切换 HTTPModelClient。"""
    def complete(self, messages, tools, *, system, state):
        latest = messages[state["turn_start"]]["content"]
        text = latest
        before = messages[:state["turn_start"]]
        last_assistant = next((m.get("content", "") for m in reversed(before) if m["role"] == "assistant"), "")
        # 仅在明确补问后合并上轮用户需求，避免旧任务污染新任务。
        if "【补问" in last_assistant:
            earlier = [m["content"] for m in before if m["role"] == "user"]
            text = " ".join(earlier[-3:] + [latest])
        traces = state["trace"]
        results = {t["tool_name"]: t["result"]["data"] for t in traces if t["result"]["success"]}
        if traces and traces[-1]["tool_name"] in {
            "book_study_seat", "cancel_study_reservation", "create_affair_draft", "register_activity",
            "cancel_activity_registration", "create_repair_ticket", "get_repair_status"}:
            t = traces[-1]
            return say(t["result"]["message"] + "\n" + json.dumps(t["result"]["data"], ensure_ascii=False))
        if "取消" in text:
            match = re.search(r"(RSV|REG)-[A-Za-z0-9]+", text)
            if not match:
                return say("【补问取消】请提供预约编号 RSV-… 或报名编号 REG-…。")
            key = "reservation_id" if match[1] == "RSV" else "registration_id"
            name = "cancel_study_reservation" if match[1] == "RSV" else "cancel_activity_registration"
            return call(name, **{key: match[0]})
        ticket = re.search(r"RP-[A-Za-z0-9]+", text)
        if ticket and any(k in text for k in ["状态", "进度", "查询"]):
            return call("get_repair_status", ticket_id=ticket[0])
        if any(k in text for k in ["报修", "空调", "水龙头"]):
            loc = re.search(r"(?:[A-Za-z]?\d+号楼|[一二三四五六七八九十]+号楼)\s*\d+[室宿舍]?", text)
            if not loc:
                return say("【补问报修】请提供宿舍位置，例如：3号楼502室。")
            problem = "空调不制冷" if "不制冷" in text else "水龙头漏水" if "漏水" in text else None
            if not problem:
                return say("【补问报修】请说明具体故障，例如：空调不制冷。")
            return call("create_repair_ticket", location=loc[0], problem=problem)
        if "学生证" in text:
            if "search_campus_knowledge" not in results:
                return call("search_campus_knowledge", query="学生证补办")
            if "get_affair_requirements" not in results:
                return call("get_affair_requirements", affair_type="学生证补办")
            name = re.search(r"姓名[：: ]*([^，,。\s]+)", text)
            sid = re.search(r"学号[：: ]*(\d+)", text)
            wants_draft = any(k in text for k in ["草稿", "申请", "办理"])
            source = results["search_campus_knowledge"].get("sources", [])
            citation = "\n".join("[%s] %s：%s" % (s["source_id"], s["title"], s["content"]) for s in source)
            if wants_draft and name and sid:
                return call("create_affair_draft", affair_type="学生证补办",
                            fields={"name": name[1], "student_id": sid[1], "reason": "学生证遗失"})
            return say(citation + "\n模拟材料：" + "、".join(results["get_affair_requirements"]["materials"])
                       + "\n【补问事务】如需模拟草稿，请补充姓名、学号，并说明生成草稿。")
        now = datetime.fromisoformat(state["now"]).astimezone(CAMPUS_TZ)
        if any(k in text for k in ["活动", "讲座"]):
            if "下周" not in text:
                return say("离线活动演示支持：下周有什么不与课表冲突的计算机讲座，帮我报名。")
            monday = now.date() + timedelta(days=7 - now.weekday())
            if "search_activities" not in results:
                return call("search_activities", keywords="计算机", start_time=at(monday, 0).isoformat(),
                            end_time=at(monday + timedelta(days=7), 0).isoformat())
            activities = results["search_activities"]["activities"]
            schedules = {t["arguments"]["date"]: t["result"]["data"]["courses"] for t in traces
                         if t["tool_name"] == "get_schedule" and t["result"]["success"]}
            for activity in activities:
                day = datetime.fromisoformat(activity["start_time"]).date().isoformat()
                if day not in schedules:
                    return call("get_schedule", date=day)
            compatible = []
            for a in activities:
                start, end = map(datetime.fromisoformat, [a["start_time"], a["end_time"]])
                if not any(overlaps(start, end, datetime.fromisoformat(c["start_time"]), datetime.fromisoformat(c["end_time"]))
                           for c in schedules[start.date().isoformat()]):
                    compatible.append(a)
            if not compatible:
                return say("模拟检索没有找到与课表不冲突的活动。")
            if "报名" in text:
                return call("register_activity", activity_id=compatible[0]["activity_id"])
            return say("模拟推荐（已逐日查课表，无冲突）：\n" + json.dumps(compatible, ensure_ascii=False))
        if any(k in text for k in ["课", "学习", "座位", "自习"]):
            match = re.search(r"\d{4}-\d{2}-\d{2}", text)
            day = match[0] if match else (now.date() + timedelta(days=1)).isoformat() if "明天" in text else now.date().isoformat() if "今天" in text else None
            if not day:
                return say("【补问时间】请说明哪一天，例如今天、明天或 YYYY-MM-DD。")
            if "get_schedule" not in results:
                return call("get_schedule", date=day)
            if not any(k in text for k in ["学习", "座位", "自习"]):
                return say("模拟课表：\n" + json.dumps(results["get_schedule"], ensure_ascii=False))
            # 明确支持 1–8 小时或 15–480 分钟；没有时长时追问。
            minutes = re.search(r"(\d+)分钟", text)
            hours = re.search(r"([1-8一二两三四五六七八])个?小时", text)
            duration = int(minutes[1]) if minutes else (int(hours[1]) if hours[1].isdigit() else {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8}[hours[1]]) * 60 if hours else None
            if duration is None:
                return say("【补问时间】你计划学习多长时间？例如两小时。")
            if "find_free_slots" not in results:
                return call("find_free_slots", date=day, duration_minutes=duration)
            lower, upper = (14, 18) if "下午" in text else (8, 12) if "上午" in text else (18, 22) if "晚上" in text else (8, 22)
            chosen = None
            for slot in results["find_free_slots"]["slots"]:
                start = max(datetime.fromisoformat(slot["start_time"]), at(day, lower), now)
                end = min(datetime.fromisoformat(slot["end_time"]), at(day, upper))
                if start + timedelta(minutes=duration) <= end:
                    chosen = {"start_time": start.isoformat(), "end_time": (start + timedelta(minutes=duration)).isoformat()}
                    break
            if not chosen:
                return say("模拟课表中，该时段没有足够长的连续空闲时间，请换个时段。")
            if "find_available_seats" not in results:
                return call("find_available_seats", **chosen)
            seats = results["find_available_seats"]["seats"]
            if not seats:
                return say("该时段模拟座位已满，请换个时间。")
            if "预约" in text:
                return call("book_study_seat", seat_id=seats[0]["seat_id"], **chosen)
            return say("模拟推荐座位：\n" + json.dumps(results["find_available_seats"], ensure_ascii=False))
        return say("当前是离线规则演示。可以试：今天有什么课；明天下午学习两小时并预约座位；学生证丢了；空调不制冷帮我报修；下周计算机讲座帮我报名。开放式问法请使用 llm 模式。")
