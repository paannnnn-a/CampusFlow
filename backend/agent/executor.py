"""有界重试：只重试明确瞬时失败的读操作，写操作永不自动重试。"""
from __future__ import annotations

from time import perf_counter, sleep

from pydantic import ValidationError

from .contracts import Context, Trace, fail


class Executor:
    def __init__(self, registry, read_attempts=2, backoff_seconds=0.05):
        self.registry = registry
        self.read_attempts = read_attempts
        self.backoff_seconds = backoff_seconds

    def prepare(self, call):
        name = call["function"]["name"]
        if name not in self.registry.specs or not self.registry.ready(name):
            return None, fail("UNKNOWN_TOOL", "工具未注册或未绑定")
        try:
            args = self.registry.specs[name].args_type.model_validate_json(
                call["function"]["arguments"]
            ).model_dump(mode="json")
        except ValidationError as exc:
            # 不回显异常中的原始输入；只报告字段位置和校验信息。
            fields = [".".join(map(str, e["loc"])) or "arguments" for e in exc.errors()]
            return None, fail("INVALID_ARGUMENTS", "请补充或修正参数：" + ", ".join(fields))
        return args, None

    def run(self, current, *, user_id, approved=False):
        started = perf_counter()
        name, args = current["name"], current["arguments"]
        attempts = 0
        if current.get("error"):
            result = fail(**current["error"])
        elif self.registry.specs[name].write and not approved:
            result = fail("CONFIRMATION_REQUIRED", "写操作未获得确认")
        else:
            limit = 1 if self.registry.specs[name].write else self.read_attempts
            for attempt in range(limit):
                attempts += 1
                try:
                    result = self.registry.invoke(name, args, Context(
                        user_id=user_id, operation_id=current["operation_id"]
                    ))
                except (TimeoutError, ConnectionError):
                    if self.registry.specs[name].write:
                        result = fail("UNKNOWN_OUTCOME", "提交连接中断，结果未知；请按操作号查询，勿重复提交")
                    else:
                        result = fail("TOOL_UNAVAILABLE", "查询服务暂时不可用", retryable=True)
                except Exception:
                    code = "UNKNOWN_OUTCOME" if self.registry.specs[name].write else "TOOL_ERROR"
                    result = fail(code, "工具异常；写入结果须核实" if code == "UNKNOWN_OUTCOME" else "工具异常或返回格式不符合协议")
                if result.success or not result.retryable or attempt + 1 >= limit:
                    break
                sleep(self.backoff_seconds * (2 ** attempt))
        result = result.model_copy(update={"simulated": self.registry.is_simulated(name)})
        return Trace(tool_name=name, call_id=current["id"], operation_id=current["operation_id"], arguments=args,
                     result=result, attempts=attempts,
                     duration_ms=round((perf_counter() - started) * 1000, 2))
