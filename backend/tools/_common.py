"""工具适配层公共部分。

把 B 的业务服务包装成 A 的 ``handler(args, context) -> ToolResult`` 形态。

约定（来自 ``tool_contract.md``）：
- ``success`` 必须是真正的布尔值；成功不带错误码，失败必须带错误码。
- ``data`` 必须是对象，没有记录时返回 ``{}`` 而不是 ``None``。
- ``simulated`` 最终由 registry 的绑定配置覆盖，这里统一填 ``True``。
- ``retryable=True`` 只用于无副作用的读故障；写操作永不设置。
- 业务异常一律转为明确错误码，不把堆栈或原始输入回显给模型。
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ..agent.contracts import ToolResult, fail, ok
from ..rag.knowledge_base import KnowledgeUnavailable


class BusinessError(Exception):
    """带错误码的业务异常。工具层直接映射为失败信封。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def coalesce(*exceptions) -> Tuple[type, ...]:
    """把多个业务异常类合并成可传给 ``except`` 的元组。"""
    return tuple(exceptions)


def read_tool(func) -> Any:
    """包装读工具。

    读操作无副作用，异常可安全重试；但只有明确的瞬时故障才标 ``retryable``。
    """

    def wrapper(args, context) -> ToolResult:
        try:
            data, message = func(args, context)
        except KnowledgeUnavailable as exc:
            # 检索后端暂时不可用，属于可安全重试的读故障。
            return fail("TOOL_UNAVAILABLE", str(exc), retryable=True)
        except BusinessError as exc:
            return fail(exc.code, exc.message)
        except ValueError as exc:
            return fail("INVALID_ARGUMENTS", str(exc))
        except Exception:
            # 不暴露内部异常细节，避免把实现信息传给模型。
            return fail("TOOL_ERROR", "查询服务异常，请稍后重试或联系管理员")
        return ok(dict(data), message)

    wrapper.__name__ = getattr(func, "__name__", "read_tool")
    wrapper.is_write = False
    return wrapper


def write_tool(func) -> Any:
    """包装写工具。

    写操作异常时结果可能已经落库，因此统一转 ``UNKNOWN_OUTCOME``，
    由 A 提示用户按操作号核实，而不是自动重试造成重复写入。
    """

    def wrapper(args, context) -> ToolResult:
        try:
            data, message = func(args, context)
        except KnowledgeUnavailable as exc:
            return fail("TOOL_UNAVAILABLE", str(exc))
        except BusinessError as exc:
            return fail(exc.code, exc.message)
        except ValueError as exc:
            return fail("INVALID_ARGUMENTS", str(exc))
        except Exception:
            return fail("UNKNOWN_OUTCOME", "写入结果未知，请按操作号核实后再决定是否重试")
        return ok(dict(data), message)

    wrapper.__name__ = getattr(func, "__name__", "write_tool")
    wrapper.is_write = True
    return wrapper
