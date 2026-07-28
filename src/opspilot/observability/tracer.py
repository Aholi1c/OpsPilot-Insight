# -*- coding: utf-8 -*-
"""自研轻量 Tracer（阶段 1 不引入 OpenTelemetry SDK，数据结构遵循 OTel 语义）。

- Span 字段：trace_id / span_id / parent_span_id / name / kind /
  start_time / end_time（epoch 纳秒）/ attributes / status
- 通过 contextvars 维护"当前 Span"，实现自动父子关联（兼容嵌套调用）
- 一次流水线运行 = 一个 Tracer 实例 = 一个 trace_id
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# 当前活跃 span_id（用于自动建立父子关系）
_current_span_id: ContextVar[Optional[str]] = ContextVar("opspilot_current_span_id", default=None)


def _gen_id(num_bytes: int) -> str:
    """生成十六进制 ID（trace_id 16 字节 / span_id 8 字节，遵循 OTel 惯例）。"""
    return uuid.uuid4().hex[: num_bytes * 2]


class Span:
    """单个 Span，语义对齐 OpenTelemetry。"""

    __slots__ = (
        "trace_id", "span_id", "parent_span_id", "name", "kind",
        "start_time", "end_time", "attributes", "status", "status_message",
    )

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        name: str,
        kind: str = "INTERNAL",
        attributes: Optional[Dict[str, Any]] = None,
    ):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.kind = kind  # INTERNAL / CLIENT / SERVER / PRODUCER / CONSUMER
        self.start_time = time.time_ns()
        self.end_time: Optional[int] = None
        self.attributes: Dict[str, Any] = dict(attributes or {})
        self.status = "UNSET"  # UNSET / OK / ERROR
        self.status_message = ""

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": round((self.end_time - self.start_time) / 1e6, 3) if self.end_time else None,
            "attributes": self.attributes,
            "status": self.status,
            "status_message": self.status_message,
        }


class Tracer:
    """轻量 Tracer：span 上下文管理器 + 自动父子关联 + JSON 导出。"""

    def __init__(self):
        self.trace_id = _gen_id(16)
        self.spans: List[Span] = []

    @property
    def current_span_id(self) -> Optional[str]:
        return _current_span_id.get()

    @contextmanager
    def start_span(self, name: str, kind: str = "INTERNAL", attributes: Optional[Dict[str, Any]] = None):
        """开启一个 Span；异常时自动标记 ERROR 并向上抛出。"""
        parent = _current_span_id.get()
        span = Span(self.trace_id, _gen_id(8), parent, name, kind, attributes)
        self.spans.append(span)
        token = _current_span_id.set(span.span_id)
        try:
            yield span
            if span.status == "UNSET":
                span.status = "OK"
        except Exception as exc:  # noqa: BLE001 —— 记录后原样抛出，由上层决定是否降级
            span.status = "ERROR"
            span.status_message = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span.end_time = time.time_ns()
            _current_span_id.reset(token)

    def export(self) -> Dict[str, Any]:
        """导出完整 span 树（扁平列表，通过 parent_span_id 还原树结构）。"""
        return {
            "trace_id": self.trace_id,
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
        }

    def export_json(self, path: Union[str, Path]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.export(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def render_tree(self) -> str:
        """渲染 ASCII span 树（用于控制台展示可观测结果）。"""
        children: Dict[Optional[str], List[Span]] = {}
        for s in self.spans:
            children.setdefault(s.parent_span_id, []).append(s)

        lines: List[str] = []

        def _walk(span: Span, depth: int) -> None:
            dur = f"{(span.end_time - span.start_time) / 1e6:.1f}ms" if span.end_time else "?"
            lines.append(f"{'  ' * depth}- {span.name} [{span.status}] ({dur})")
            for child in children.get(span.span_id, []):
                _walk(child, depth + 1)

        for root in children.get(None, []):
            _walk(root, 0)
        return "\n".join(lines)
