"""Consistent exception details, including contextual timeout messages."""

import asyncio
import errno
import re


class OperationTimeout(asyncio.TimeoutError):
    def __init__(self, operation, seconds):
        self.operation = operation
        self.seconds = seconds
        super().__init__(f"{operation}超时（{seconds:g} 秒）")


def is_timeout(exc):
    # Provider SDKs may expose ReadTimeout/ConnectTimeout rather than the
    # standard exception. Keep their context readable without importing SDKs.
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in type(exc).__name__.lower()


def is_transient_error(exc):
    """Classify temporary transport errors without importing provider SDKs."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        response = getattr(exc, "response", None)
        status = status or getattr(response, "status_code", None)
        text = (type(exc).__name__ + " " + str(exc)).lower()
        if status is None:
            status_text = re.search(r"(?:http\s*|status(?:_code)?\s*[:=]?\s*)([45]\d\d)\b", text)
            status = int(status_text.group(1)) if status_text else None
        if isinstance(status, int):
            if status in {408, 425, 429} or 500 <= status < 600:
                return True
            if 400 <= status < 500:
                return False
        if is_timeout(exc) or isinstance(exc, ConnectionError):
            return True
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in {
            errno.ECONNRESET,
            errno.ECONNREFUSED,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ETIMEDOUT,
            errno.EPIPE,
        }:
            return True
        if any(
            word in text
            for word in (
                "connectionerror",
                "connecterror",
                "networkerror",
                "readerror",
                "connection error",
                "connection reset",
                "connection refused",
                "temporary failure",
                "temporarily unavailable",
                "连接错误",
                "连接失败",
                "连接不上",
            )
        ):
            return True
        exc = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    return False


def retry_delay_minutes(exc, attempt, *, content_minutes=30):
    if is_transient_error(exc):
        return (2, 10, 30)[min(max(int(attempt) - 1, 0), 2)]
    return content_minutes


def describe_error(exc, *, operation=None, timeout_seconds=None):
    kind = type(exc).__name__
    message = str(exc).strip()
    if is_timeout(exc) and not isinstance(exc, OperationTimeout):
        label = (operation or "操作") + "超时"
        if timeout_seconds is not None:
            label += f"（{timeout_seconds:g} 秒）"
        message = label + ("：" + message if message else "")
    return kind + (": " + message if message else "")


def user_error(error, *, action=None):
    """Translate exceptions and persisted diagnostic strings for ordinary users.

    Raw provider responses may contain technical details or private addresses;
    keep those in logs and choose an actionable, authored explanation here.
    """
    text = str(error or "")
    diagnostic = type(error).__name__ + " " + text
    lower = diagnostic.lower()
    safe_prefixes = (
        "用法：",
        "没有找到这套穿搭",
        "没有找到对应穿搭",
        "多套穿搭包含这个描述",
        "请在指令里附上图片",
        "没有找到这个约定编号",
        "没有找到这个事件编号",
        "穿搭描述为空",
        "日程时间请使用",
        "事件结束日期早于开始日期",
    )
    if text.startswith(safe_prefixes) and not re.search(r"Error|Exception|HTTP|Traceback|https?://", text):
        return text
    if isinstance(error, OperationTimeout):
        return (
            f"{error.operation}超过 {error.seconds:g} 秒还没完成，请稍后重试；如果连续失败，请检查 AstrBot 的模型设置。"
        )
    if is_timeout(error) or "timeout" in lower or "超时" in text:
        seconds = re.search(r"[（(](\d+(?:\.\d+)?)\s*秒[）)]", text)
        duration = "超过 " + seconds.group(1) + " 秒还没完成" if seconds else "等待时间较长"
        return f"模型响应{duration}，请稍后重试；如果连续失败，请检查 AstrBot 的模型设置。"
    if "受限互动" in text:
        return "生成的内容里有和你通话、视频或见面等安排，请点重新生成，其他生活内容会正常保留。"
    if "人设" in text and any(word in text for word in ("找不到", "不存在", "读取")):
        return "人设读取没有完成，请在 AstrBot 人格设定里确认名称后重试。"
    if any(
        word in lower
        for word in (
            "noprovider",
            "connection",
            "connecterror",
            "network",
            "provider",
            "api key",
            "api_key",
            "unauthor",
            "401",
            "403",
            "429",
            "500",
            "502",
            "503",
            "504",
        )
    ) or any(word in text for word in ("没有可用的模型", "模型连接", "连接不上")):
        return "模型连接不上，请检查 AstrBot 的模型设置后点重试。"
    if "json" in lower or any(word in text for word in ("档案内容为空", "时间段", "时间轴", "临时搭配", "概况")):
        return "模型这次没有整理好内容，请点重试；如果连续失败，请换一个模型再试。"
    if any(word in text for word in ("重启", "中断", "卸载")):
        return "上次生成因插件重启或关闭而中断，请点重新生成继续。"
    if isinstance(error, (OSError, PermissionError)) or action == "保存":
        return "保存没有完成，原来的内容已保留，请检查电脑可用空间后重试。"
    return "这次处理没有完成，请稍后重试；如果反复失败，请检查 AstrBot 的模型设置。"


async def run_with_timeout(awaitable, seconds, operation):
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except OperationTimeout:
        # A longer outer operation must preserve the actual inner deadline.
        raise
    except Exception as exc:
        if is_timeout(exc):
            raise OperationTimeout(operation, seconds) from exc
        raise
