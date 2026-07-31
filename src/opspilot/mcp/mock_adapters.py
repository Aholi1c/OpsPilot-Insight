# -*- coding: utf-8 -*-
"""Mock MCP 适配器：模拟 monitoring / logging / tracing / change / execution 等运维工具。

接口形态对齐 MCP tool 的调用语义（结构化入参 + 结构化返回），后续可平滑
替换为真实 MCP Server 客户端；数据源为场景目录下的 JSON 文件。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def _parse_time(value: str) -> Optional[datetime]:
    """解析 ISO8601 时间字符串，失败返回 None。"""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class BaseMockAdapter:
    """Mock 适配器基类：负责从场景目录加载对应 JSON 文件。"""

    source_name = "base"
    file_name = ""

    def __init__(self, scenario_dir: Union[str, Path]):
        self.scenario_dir = Path(scenario_dir)

    def _load(self) -> Dict[str, Any]:
        path = self.scenario_dir / self.file_name
        if not path.exists():
            raise FileNotFoundError(f"场景数据文件不存在: {path}")
        return json.loads(path.read_text(encoding="utf-8"))


class MonitoringAdapter(BaseMockAdapter):
    """监控指标查询（模拟 Prometheus 风格的时间序列）。"""

    source_name = "monitoring"
    file_name = "metrics.json"

    def query_metrics(self, service: Optional[str] = None, name: Optional[str] = None) -> List[Dict[str, Any]]:
        metrics = self._load().get("metrics", [])
        if service:
            metrics = [m for m in metrics if m.get("service") == service]
        if name:
            metrics = [m for m in metrics if name in m.get("name", "")]
        return metrics


class MonitoringAfterAdapter(MonitoringAdapter):
    """修复后指标查询（读 metrics_after.json，模拟执行后的恢复曲线）。"""

    source_name = "monitoring_after"
    file_name = "metrics_after.json"


class LoggingAdapter(BaseMockAdapter):
    """日志查询（模拟集中式日志平台）。"""

    source_name = "logging"
    file_name = "logs.json"
    # 扩展时间窗日志（协商模式证据补充采集用，场景目录可选提供）
    extended_file_name = "logs_extended.json"

    def query_logs(
        self,
        service: Optional[str] = None,
        level: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        logs = self._load().get("logs", [])
        if service:
            logs = [x for x in logs if x.get("service") == service]
        if level:
            logs = [x for x in logs if x.get("level") == level]
        if keyword:
            logs = [x for x in logs if keyword in x.get("message", "")]
        return logs

    def query_extended_logs(self) -> List[Dict[str, Any]]:
        """查询扩展时间窗日志（响应 RcaAgent 的证据补充请求）。

        场景目录未提供 logs_extended.json 时返回空列表（模拟真实环境
        中日志平台保留周期外无数据可补的情形）。
        """
        path = self.scenario_dir / self.extended_file_name
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("logs", [])


class TracingAdapter(BaseMockAdapter):
    """分布式链路查询（模拟 APM/Tracing 平台）。"""

    source_name = "tracing"
    file_name = "traces.json"

    def query_traces(self, service: Optional[str] = None, min_duration_ms: float = 0.0) -> List[Dict[str, Any]]:
        """按服务/最小耗时过滤 trace（trace 内任一 span 命中即保留整条）。"""
        traces = self._load().get("traces", [])
        result = []
        for trace in traces:
            spans = trace.get("spans", [])
            hit = [
                s for s in spans
                if (service is None or s.get("service") == service)
                and float(s.get("duration_ms", 0)) >= min_duration_ms
            ]
            if hit:
                result.append(trace)
        return result

    def get_service_dependencies(self) -> List[Dict[str, str]]:
        """从 span 父子关系推导服务依赖边（caller -> callee）。"""
        edges: List[Dict[str, str]] = []
        seen = set()
        for trace in self._load().get("traces", []):
            spans = {s["span_id"]: s for s in trace.get("spans", [])}
            for span in spans.values():
                parent = spans.get(span.get("parent_span_id"))
                if not parent:
                    continue
                caller, callee = parent.get("service"), span.get("service")
                if caller and callee and caller != callee and (caller, callee) not in seen:
                    seen.add((caller, callee))
                    edges.append({"caller": caller, "callee": callee})
        return edges


class ChangeAdapter(BaseMockAdapter):
    """变更记录查询（模拟发布/变更管理系统）。"""

    source_name = "change"
    file_name = "changes.json"

    def query_changes(
        self,
        service: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        changes = self._load().get("changes", [])
        if service:
            changes = [c for c in changes if c.get("service") == service]
        since_dt, until_dt = _parse_time(since or ""), _parse_time(until or "")
        result = []
        for change in changes:
            end = _parse_time(change.get("end_time", ""))
            if since_dt and end and end < since_dt:
                continue
            if until_dt and end and end > until_dt:
                continue
            result.append(change)
        return result

    def get_change_details(self) -> List[Dict[str, Any]]:
        """查询全部变更单详情（含 diff_summary，响应证据补充请求）。"""
        return [
            {
                "change_id": c.get("change_id"),
                "service": c.get("service"),
                "title": c.get("title"),
                "diff_summary": c.get("diff_summary", ""),
                "release_ticket": c.get("release_ticket", ""),
            }
            for c in self._load().get("changes", [])
        ]


class ExecutionAdapter(BaseMockAdapter):
    """执行适配器（模拟 K8s / 配置中心等变更操作，不触碰任何真实资源）。

    场景目录可选提供 execution.json 配置剧本：指定某个动作注定失败
    （用于演示执行失败 -> 自动回滚 -> 备选动作的完整链路）；
    无剧本文件时所有动作默认执行成功。
    """

    source_name = "execution"
    file_name = "execution.json"

    def _scripted_results(self) -> List[Dict[str, Any]]:
        path = self.scenario_dir / self.file_name
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("scripted_results", [])

    def create_checkpoint(self, action_type: str, target: str) -> Dict[str, Any]:
        """执行前创建回滚检查点（当前状态快照，Mock 为可回放的描述对象）。"""
        return {
            "checkpoint_id": f"CKPT-{uuid.uuid4().hex[:8].upper()}",
            "action_type": action_type,
            "target": target,
            "snapshot": f"{target} 当前版本/配置快照（模拟）",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def execute_action(
        self, action_type: str, action: str, command: Optional[str] = None,
        order: int = 0,
    ) -> Dict[str, Any]:
        """执行单个动作：命中剧本则按剧本返回，否则模拟成功。"""
        for scripted in self._scripted_results():
            match = scripted.get("match", {})
            if match.get("order") not in (None, order):
                continue
            if match.get("action_type") not in (None, action_type):
                continue
            keyword = match.get("action_contains")
            if keyword and keyword not in action:
                continue
            return {
                "status": scripted.get("status", "failed"),
                "message": scripted.get("message", "（剧本未提供失败原因）"),
            }
        return {"status": "success", "message": f"动作执行成功（Mock）: {action_type}"}

    def rollback_to(self, checkpoint: Dict[str, Any]) -> Dict[str, Any]:
        """回滚到指定检查点（Mock 恒成功，真实实现对接 K8s/配置中心）。"""
        return {
            "status": "success",
            "message": f"已恢复至检查点 {checkpoint.get('checkpoint_id')}（{checkpoint.get('snapshot')}）",
        }


def build_adapters(scenario_dir: Union[str, Path]) -> Dict[str, BaseMockAdapter]:
    """按场景目录构建全部 Mock MCP 适配器。"""
    return {
        "monitoring": MonitoringAdapter(scenario_dir),
        "monitoring_after": MonitoringAfterAdapter(scenario_dir),
        "logging": LoggingAdapter(scenario_dir),
        "tracing": TracingAdapter(scenario_dir),
        "change": ChangeAdapter(scenario_dir),
        "execution": ExecutionAdapter(scenario_dir),
    }
