"""结构化日志（A10）：运行时日志统一带 run_id / attempt / seq / node 字段，单行 JSON。

- ``log_event``：以 extra 注入运行字段；缺失字段不出现（不编造 NULL）；
- ``RunLogFormatter``：LogRecord → 单行 JSON（ts / level / logger / message + 运行字段
  + 调用方附加的可序列化字段）；
- ``configure_run_logging``：给运行时根 logger 挂 handler（幂等，可重复调用）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

# 运行追踪字段（计划 §7.1-A10：每条日志带 run_id / attempt / seq / node）
RUN_FIELDS = ("run_id", "attempt", "seq", "node", "event", "status", "worker_id")

RUN_LOG_NAME = "agent-hub.runtime"

# logging.LogRecord 的标准属性（不作为自定义字段重复输出）
_STANDARD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    }
)


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    run_id: str | None = None,
    attempt: int | None = None,
    seq: int | None = None,
    node: str | None = None,
    **fields,  # 其余可序列化附加字段（worker_id / status / event 等）
) -> None:
    extra = {
        key: value
        for key, value in {"run_id": run_id, "attempt": attempt, "seq": seq, "node": node, **fields}.items()
        if value is not None
    }
    logger.log(level, message, extra=extra)


class RunLogFormatter(logging.Formatter):
    """LogRecord → 单行 JSON：标准字段 + 运行字段 + 附加可序列化字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if isinstance(value, str | int | float | bool):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_run_logging(level: int = logging.INFO, stream=None) -> logging.Logger:
    """运行时根 logger 配置（幂等）；返回该 logger，集成方可在其上继续挂 handler。"""
    logger = logging.getLogger(RUN_LOG_NAME)
    has_run_formatter = any(
        isinstance(getattr(h, "formatter", None), RunLogFormatter) for h in logger.handlers
    )
    if not has_run_formatter:
        handler = logging.StreamHandler(stream) if stream is not None else logging.StreamHandler()
        handler.setFormatter(RunLogFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
