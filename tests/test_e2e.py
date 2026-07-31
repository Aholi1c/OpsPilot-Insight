# -*- coding: utf-8 -*-
"""端到端测试：4 个场景各跑一遍完整流水线。

断言点：
1. 报告字段完整（事件/根因候选/选定根因/修复方案/回滚计划）；
2. Trace span 树父子关联正确（单根、parent 均存在、trace_id 一致、span 均闭合）；
3. 五类产物（report/trace/log/audit/metrics）确实落盘；
4. db_pool_exhaustion 场景的根因能关联到引入连接泄漏的变更单。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

# src 路径由 tests/conftest.py 统一注入；此处仅保留项目根用于定位数据文件
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

from opspilot.models import IncidentReport  # noqa: E402
from opspilot.orchestrator import Orchestrator  # noqa: E402

SCENARIOS = ["db_pool_exhaustion", "container_oom", "network_latency", "transaction_risk_surge"]


@pytest.fixture()
def orchestrator(tmp_path) -> Orchestrator:
    """每个用例使用独立的临时输出目录与知识库拷贝（防止案例沉淀污染仓库种子数据）。"""
    knowledge_dir = tmp_path / "knowledge"
    shutil.copytree(_PROJECT_ROOT / "data" / "knowledge", knowledge_dir)
    return Orchestrator(output_dir=tmp_path / "output", console=False, knowledge_dir=knowledge_dir)


def _run(orchestrator: Orchestrator, scenario: str) -> IncidentReport:
    report = orchestrator.run(scenario)
    assert isinstance(report, IncidentReport)
    return report


# ---------------------------------------------------------------------------
# 场景清单
# ---------------------------------------------------------------------------

def test_list_scenarios(orchestrator):
    names = [item["name"] for item in orchestrator.list_scenarios()]
    assert names == sorted(SCENARIOS), "应恰好列出 4 个内置场景"


# ---------------------------------------------------------------------------
# 报告字段完整性（4 场景各跑一遍）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_report_completeness(orchestrator, scenario):
    report = _run(orchestrator, scenario)

    # 基础字段
    assert report.report_id.startswith("RPT-")
    assert report.scenario == scenario
    assert report.trace_id and len(report.trace_id) == 32
    assert not report.degraded, f"正常场景不应触发降级: {report.degradation_notes}"

    # 事件对象：告警去重聚合 + 影响面
    incident = report.incident
    assert incident.incident_id.startswith("INC-")
    assert incident.raw_alert_count > 0
    assert 0 < incident.deduped_alert_count <= incident.raw_alert_count
    assert incident.alert_groups, "应至少聚合出一个告警组"
    assert incident.affected_services, "应识别出受影响服务"
    assert incident.impact.get("blast_radius"), "应给出爆炸半径"
    assert incident.summary, "应生成事件概述"

    # 根因：候选 >= 2（主假设 + 备选），选定根因带证据链
    assert len(report.root_cause_candidates) >= 2
    selected = report.selected_root_cause
    assert selected is not None, "应选出根因"
    assert selected.category == scenario, "根因类别应与场景一致"
    assert selected.confidence > 0.5
    assert selected.evidences, "根因应带证据链"
    strengths = {ev.strength for ev in selected.evidences}
    assert strengths <= {"strong", "weak", "missing"}
    assert "strong" in strengths, "应至少有一条 strong 证据"
    sources = {ev.source for ev in selected.evidences}
    assert {"logs", "metrics", "traces", "changes"} <= sources, "证据链应覆盖四个维度"

    # 修复方案：步骤 + 风险等级 + 回滚计划
    plan = report.remediation_plan
    assert plan is not None, "应生成修复方案"
    assert plan.risk_level in ("low", "medium", "high")
    assert plan.steps, "方案应含修复步骤"
    assert plan.rollback_plan, "方案应含回滚计划"
    assert all(step.expected_effect for step in plan.steps)

    # 时间线：三个 Agent 均有开始/结束标记
    events = " ".join(item["event"] for item in report.timeline)
    for agent in ("AlertAgent", "RcaAgent", "PlannerAgent"):
        assert f"{agent} 开始" in events and f"{agent} 结束" in events


# ---------------------------------------------------------------------------
# Trace span 树父子关联
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_trace_span_tree(orchestrator, scenario):
    report = _run(orchestrator, scenario)
    trace_path = orchestrator.last_artifacts["trace"]
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    assert trace["trace_id"] == report.trace_id
    spans = trace["spans"]
    assert trace["span_count"] == len(spans) > 0

    span_ids = {s["span_id"] for s in spans}
    assert len(span_ids) == len(spans), "span_id 不应重复"

    roots = [s for s in spans if s["parent_span_id"] is None]
    assert len(roots) == 1, "span 树应有且仅有一个根"
    assert roots[0]["name"] == "pipeline.run"

    for span in spans:
        # trace_id 一致、父 span 必须存在、时间戳闭合、状态合法
        assert span["trace_id"] == report.trace_id
        if span["parent_span_id"] is not None:
            assert span["parent_span_id"] in span_ids, f"孤儿 span: {span['name']}"
        assert span["end_time"] is not None and span["end_time"] >= span["start_time"]
        assert span["status"] in ("OK", "ERROR", "UNSET")
        assert span["kind"] in ("INTERNAL", "CLIENT", "SERVER", "PRODUCER", "CONSUMER")

    # 关键层级：3 个 agent span 均挂在根 span 下；每个 skill span 有 agent 父级
    names = {s["name"] for s in spans}
    for expected in ("agent.AlertAgent", "agent.RcaAgent", "agent.PlannerAgent",
                     "skill.alert_fusion", "skill.impact_mapping", "skill.log_trace_rca"):
        assert expected in names, f"缺少关键 span: {expected}"
    root_id = roots[0]["span_id"]
    by_id = {s["span_id"]: s for s in spans}
    for span in spans:
        if span["name"].startswith("agent."):
            assert span["parent_span_id"] == root_id, "agent span 应直接挂在 pipeline 根下"
        if span["name"].startswith("skill."):
            parent = by_id[span["parent_span_id"]]
            assert parent["name"].startswith("agent."), "skill span 应由 agent span 派生"


# ---------------------------------------------------------------------------
# 产物落盘
# ---------------------------------------------------------------------------

def test_artifacts_written(orchestrator):
    _run(orchestrator, "db_pool_exhaustion")
    artifacts = orchestrator.last_artifacts
    assert set(artifacts) == {"report", "trace", "log", "audit", "metrics"}
    for kind, path in artifacts.items():
        assert path.exists(), f"{kind} 产物未落盘: {path}"
        assert path.stat().st_size > 0

    # 报告 JSON 可反序列化回 Pydantic 模型
    data = json.loads(artifacts["report"].read_text(encoding="utf-8"))
    IncidentReport.model_validate(data)

    # 日志为 JSON lines 且每行带 trace_id
    lines = artifacts["log"].read_text(encoding="utf-8").strip().splitlines()
    assert lines, "运行日志不应为空"
    for line in lines:
        record = json.loads(line)
        assert record.get("trace_id"), "每条日志应携带 trace_id"


# ---------------------------------------------------------------------------
# 场景语义正确性
# ---------------------------------------------------------------------------

def test_db_pool_root_cause_links_change(orchestrator):
    """db_pool_exhaustion 的根因必须关联到引入连接泄漏的变更单。"""
    report = _run(orchestrator, "db_pool_exhaustion")
    selected = report.selected_root_cause
    assert selected.related_change_id == "CHG-20260723-0012"
    assert "连接池" in selected.hypothesis
    assert "CHG-20260723-0012" in selected.hypothesis
    assert selected.service == "order-service"
    # 修复方案应包含回滚该变更的动作
    plan_text = json.dumps(report.remediation_plan.model_dump(), ensure_ascii=False)
    assert "CHG-20260723-0012" in plan_text or "回滚" in plan_text


def test_container_oom_root_cause(orchestrator):
    report = _run(orchestrator, "container_oom")
    selected = report.selected_root_cause
    assert selected.category == "container_oom"
    assert selected.service == "payment-service"
    assert selected.related_change_id == "CHG-20260723-0021"


def test_network_latency_root_cause(orchestrator):
    report = _run(orchestrator, "network_latency")
    selected = report.selected_root_cause
    assert selected.category == "network_latency"
    assert selected.related_change_id == "CHG-20260723-0007"


def test_unknown_scenario_raises(orchestrator):
    with pytest.raises(ValueError):
        orchestrator.run("no_such_scenario")
