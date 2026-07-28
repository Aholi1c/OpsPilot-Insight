# -*- coding: utf-8 -*-
"""审计日志：ExecutorAgent 安全边界的事件流留痕（JSON lines）。

事件类型（event_type）：
- whitelist_check：白名单校验结果（放行/拒绝）
- approval       ：人工/自动审批决定（who / when / decision）
- checkpoint     ：动作执行前的回滚检查点快照
- execute        ：动作执行结果（success / failed / skipped_idempotent）
- rollback       ：回滚动作记录

每条事件自动携带 trace_id / span_id，可与 Trace、运行日志互查。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import IO, Any, List, Optional, Union

from .tracer import Tracer


class AuditLog:
    """JSONL 审计日志器（无第三方依赖）。"""

    def __init__(
        self,
        audit_path: Optional[Union[str, Path]] = None,
        tracer: Optional[Tracer] = None,
    ):
        self.tracer = tracer
        self.path = Path(audit_path) if audit_path else None
        self.events: List[dict] = []  # 内存副本，便于测试与复盘引用
        self._fp: Optional[IO[str]] = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = self.path.open("a", encoding="utf-8")

    def record(self, event_type: str, **fields: Any) -> dict:
        """写入一条审计事件，返回事件对象。"""
        event = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event_type": event_type,
            "trace_id": self.tracer.trace_id if self.tracer else "",
            "span_id": (self.tracer.current_span_id or "") if self.tracer else "",
        }
        event.update(fields)
        self.events.append(event)
        if self._fp:
            self._fp.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self._fp.flush()
        return event

    def close(self) -> None:
        if self._fp:
            self._fp.close()
            self._fp = None
