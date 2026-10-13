"""仅供 A 独立开发：全部模拟、进程内记录，不访问校园系统或 SQL。"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from threading import RLock
from uuid import uuid4

from .contracts import CAMPUS_TZ, fail, ok
from .tool_registry import SPECS, ToolRegistry


def overlaps(a, b, c, d):
    return a < d and c < b


def at(day, hour):
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return datetime(day.year, day.month, day.day, hour, tzinfo=CAMPUS_TZ)


class MockCampus:
    def __init__(self, now=None):
        self.now = now or datetime.now(CAMPUS_TZ)
        self.lock = RLock()
        self.reservations = {}
        self.registrations = {}
        self.repairs = {}
        self.drafts = {}
        self.idempotency = {}
        self.seats = [{"seat_id": "LIB-A301-01", "location": "图书馆 A301 静音区"},
                      {"seat_id": "LIB-A301-02", "location": "图书馆 A301 静音区"}]
        monday = self.now.date() + timedelta(days=7 - self.now.weekday())
        self.activities = [
            {"activity_id": "ACT-CONFLICT", "title": "计算机讲座：算法", "start_time": at(monday, 14).isoformat(),
             "end_time": at(monday, 16).isoformat(), "location": "教学楼 101"},
            {"activity_id": "ACT-FREE", "title": "计算机讲座：人工智能", "start_time": at(monday + timedelta(days=1), 18).isoformat(),
             "end_time": at(monday + timedelta(days=1), 20).isoformat(), "location": "报告厅"},
        ]

    def registry(self):
        registry = ToolRegistry()
        for spec in SPECS:
            registry.bind(spec.name, self._handler(spec), simulated=True)
        return registry

    def _handler(self, spec):
        def handle(args, context):
            # 写冲突校验与写入在同一锁内；只模拟单进程原子性。
            with self.lock:
                key = (context.user_id, spec.name, context.operation_id)
                normalized = args.model_dump(mode="json")
                if spec.write and key in self.idempotency:
                    previous_args, previous_result = self.idempotency[key]
                    if previous_args != normalized:
                        return fail("IDEMPOTENCY_KEY_REUSED", "同一操作号不能用于不同参数")
                    return deepcopy(previous_result)
                result = getattr(self, spec.name)(args, context)
                if spec.write:
                    self.idempotency[key] = (normalized, deepcopy(result))
                return result
        return handle

    def courses(self, day):
        items = [{"course": "数据结构", "start_time": at(day, 9).isoformat(), "end_time": at(day, 10).isoformat()}]
        if date.fromisoformat(day).weekday() == 0:
            items.append({"course": "高等数学", "start_time": at(day, 14).isoformat(), "end_time": at(day, 16).isoformat()})
        return items

    def get_schedule(self, args, context):
        return ok({"date": args.date, "courses": self.courses(args.date)}, "模拟课表")

    def find_free_slots(self, args, context):
        cursor, stop = at(args.date, 8), at(args.date, 22)
        slots = []
        for course in self.courses(args.date):
            start, end = map(datetime.fromisoformat, [course["start_time"], course["end_time"]])
            if (start - cursor).total_seconds() >= args.duration_minutes * 60:
                slots.append({"start_time": cursor.isoformat(), "end_time": start.isoformat()})
            cursor = max(cursor, end)
        if (stop - cursor).total_seconds() >= args.duration_minutes * 60:
            slots.append({"start_time": cursor.isoformat(), "end_time": stop.isoformat()})
        return ok({"slots": slots}, "模拟空闲时段")

    def find_available_seats(self, args, context):
        available = [seat for seat in self.seats if not any(
            record["seat_id"] == seat["seat_id"] and record["status"] == "active"
            and overlaps(args.start_time, args.end_time, datetime.fromisoformat(record["start_time"]),
                         datetime.fromisoformat(record["end_time"]))
            for record in self.reservations.values())]
        return ok({"seats": available, "start_time": args.start_time.isoformat(), "end_time": args.end_time.isoformat()}, "模拟可用座位")

    def book_study_seat(self, args, context):
        if args.start_time < self.now:
            return fail("PAST_TIME", "不能预约已过去的时段")
        if args.start_time.date() != args.end_time.date():
            return fail("INVALID_INTERVAL", "演示预约须在同一天")
        if not any(s["seat_id"] == args.seat_id for s in self.seats):
            return fail("NOT_FOUND", "座位不存在")
        if any(overlaps(args.start_time, args.end_time, datetime.fromisoformat(c["start_time"]),
                        datetime.fromisoformat(c["end_time"])) for c in self.courses(args.start_time.date().isoformat())):
            return fail("COURSE_CONFLICT", "与课表冲突，请选择其他时段")
        for record in self.reservations.values():
            if record["status"] == "active" and overlaps(args.start_time, args.end_time,
                    datetime.fromisoformat(record["start_time"]), datetime.fromisoformat(record["end_time"])):
                if record["seat_id"] == args.seat_id or record["user_id"] == context.user_id:
                    return fail("RESERVATION_CONFLICT", "时段已被预约，请重新查询座位")
        rid = "RSV-" + uuid4().hex[:8]
        data = {"reservation_id": rid, **args.model_dump(mode="json"), "status": "active"}
        self.reservations[rid] = {**data, "user_id": context.user_id}
        return ok(data, "模拟预约成功，未提交学校官方系统")

    def _cancel(self, records, record_id, context, id_field):
        record = records.get(record_id)
        if not record or record["user_id"] != context.user_id:
            return fail("NOT_FOUND", "记录不存在或不属于当前用户")
        record["status"] = "cancelled"
        return ok({id_field: record_id, "status": "cancelled"}, "模拟记录已取消")

    def cancel_study_reservation(self, args, context):
        return self._cancel(self.reservations, args.reservation_id, context, "reservation_id")

    def search_campus_knowledge(self, args, context):
        if not any(word in args.query for word in ["学生证", "证件"]):
            return ok({"sources": []}, "模拟资料中未找到依据，请联系负责部门")
        return ok({"sources": [{"source_id": "MOCK-POLICY-001", "title": "学生证补办演示办法（模拟资料）",
            "content": "准备姓名、学号和遗失说明，向学生事务中心申请补办。此内容仅用于演示。"}]}, "模拟知识检索结果")

    def get_affair_requirements(self, args, context):
        if args.affair_type != "学生证补办":
            return fail("UNSUPPORTED_AFFAIR", "演示仅提供学生证补办")
        return ok({"affair_type": args.affair_type, "required_fields": ["name", "student_id", "reason"],
                   "materials": ["身份证明", "遗失说明"], "steps": ["准备材料", "联系学生事务中心"]}, "模拟办理要求")

    def create_affair_draft(self, args, context):
        if args.affair_type != "学生证补办":
            return fail("UNSUPPORTED_AFFAIR", "演示仅提供学生证补办")
        if any(not args.fields.get(key, "").strip() for key in ["name", "student_id", "reason"]):
            return fail("MISSING_FIELDS", "缺少姓名、学号或遗失说明")
        did = "DFT-" + uuid4().hex[:8]
        data = {"draft_id": did, **args.model_dump(), "status": "draft"}
        self.drafts[did] = {**data, "user_id": context.user_id}
        return ok(data, "已保存模拟申请草稿，尚未正式提交")

    def search_activities(self, args, context):
        return ok({"activities": [a for a in self.activities if args.keywords in a["title"]
            and args.start_time <= datetime.fromisoformat(a["start_time"])
            and datetime.fromisoformat(a["end_time"]) <= args.end_time]}, "模拟活动检索结果")

    def register_activity(self, args, context):
        activity = next((a for a in self.activities if a["activity_id"] == args.activity_id), None)
        if not activity:
            return fail("NOT_FOUND", "活动不存在")
        start, end = map(datetime.fromisoformat, [activity["start_time"], activity["end_time"]])
        if start < self.now:
            return fail("PAST_TIME", "活动已经开始")
        for course in self.courses(start.date().isoformat()):
            if overlaps(start, end, datetime.fromisoformat(course["start_time"]), datetime.fromisoformat(course["end_time"])):
                return fail("COURSE_CONFLICT", "活动与课表冲突")
        if any(r["activity_id"] == args.activity_id and r["user_id"] == context.user_id
               and r["status"] == "active" for r in self.registrations.values()):
            return fail("DUPLICATE_REGISTRATION", "不能重复报名")
        rid = "REG-" + uuid4().hex[:8]
        data = {"registration_id": rid, "activity_id": args.activity_id, "status": "active"}
        self.registrations[rid] = {**data, "user_id": context.user_id}
        return ok(data, "模拟报名成功，未提交官方系统")

    def cancel_activity_registration(self, args, context):
        return self._cancel(self.registrations, args.registration_id, context, "registration_id")

    def create_repair_ticket(self, args, context):
        tid = "RP-" + uuid4().hex[:8]
        data = {"ticket_id": tid, **args.model_dump(), "status": "pending"}
        self.repairs[tid] = {**data, "user_id": context.user_id}
        return ok(data, "已创建模拟工单，未通知学校维修人员")

    def get_repair_status(self, args, context):
        record = self.repairs.get(args.ticket_id)
        if not record or record["user_id"] != context.user_id:
            return fail("NOT_FOUND", "工单不存在或不属于当前用户")
        return ok({k: v for k, v in record.items() if k != "user_id"}, "模拟工单状态")
