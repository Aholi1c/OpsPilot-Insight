# -*- coding: utf-8 -*-
"""JSON lines 结构化日志：每条日志自动带 trace_id / span_id，实现日志与链路互查。

- 文件端：一行一个 JSON 对象（run_*.log）
- 控制台端：人类可读格式，展示 Agent/Skill 执行过程
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Optional, Union

from .tracer import Tracer


class JsonLogger:
    """结构化日志器（无第三方依赖）。"""

    def __init__(
        self,
        log_path: Optional[Union[str, Path]] = None,
        tracer: Optional[Tracer] = None,
        console: bool = True,
    ):
        self.tracer = tracer
        self.console = console
        self._fp: Optional[IO[str]] = None
        if log_path is not None:
            log_path = Path(log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = log_path.open("a", encoding="utf-8")

    def log(self, level: str, message: str, **fields: Any) -> None:
        now = datetime.now().astimezone()
        record = {
            "timestamp": now.isoformat(timespec="milliseconds"),
            "level": level,
            "message": message,
            "trace_id": self.tracer.trace_id if self.tracer else "",
            "span_id": (self.tracer.current_span_id or "") if self.tracer else "",
        }
        record.update(fields)
        if self._fp:
            self._fp.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._fp.flush()
        if self.console:
            extra = " ".join(f"{k}={v}" for k, v in fields.items())
            print(f"[{now.strftime('%H:%M:%S')}] {level:<5} {message}" + (f"  ({extra})" if extra else ""))

    def info(self, message: str, **fields: Any) -> None:
        self.log("INFO", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self.log("WARN", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.log("ERROR", message, **fields)

    def close(self) -> None:
        if self._fp:
            self._fp.close()
            self._fp = None
