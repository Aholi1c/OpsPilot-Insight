# -*- coding: utf-8 -*-
"""协商与反馈机制测试（协商模式为可选增强，默认关闭）。

覆盖点：
1. 机制 1（RCA 低置信度证据补充反馈环）：触发 -> MCP 补充采集 -> 二轮分析置信度回升，
   Trace 含 rca.evidence_request / rca.reanalysis span，审计与 AgentMessage 留痕；
2. 机制 1 重试上限：无扩展数据可补时，二轮后仍低于阈值走原有降级路径（转人工）；
3. 机制 2（多根因候选 -> 多方案并行 -> 决策选择）：自动选最优 + alternative_plans 落报告，
   Trace 含 plan.negotiation span 且其下并行挂载多个 PlannerAgent span；
4. 机制 2 交互决策：plan_selector 人工选择非最优方案同样生效；
5. 默认模式回归守护：不开协商时报告无协商字段、Trace 无协商 span。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

from opspilot.negotiation import rank_plan_options  # noqa: E402
from opspilot.orchestrator import Orchestrator  # noqa: E402


def _make_orchestrator(tmp_path, **kwargs) -> Orchestrator:
    """独立临时输出目录与知识库拷贝（与 test_e2e 相同的隔离策略）。"""
    knowledge_dir = tmp_path / "knowledge"
    if not knowledge_dir.exists():
        shutil.copytree(_PROJECT_ROOT / "data" / "knowledge", knowledge_dir)
    return Orchestrator(
        output_dir=tmp_path / "output", console=False,
        knowledge_dir=knowledge_dir, **kwargs,
    )


def _load_trace(orchestrator: Orchestrator) -> dict:
    return json.loads(orchestrator.last_artifacts["trace"].read_text(encoding="utf-8"))


def _load_audit_events(orchestrator: Orchestrator) -> list:
    lines = orchestrator.last_artifacts["audit"].read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# 机制 1：低置信度证据补充反馈环
# ---------------------------------------------------------------------------

def test_evidence_loop_triggered_and_confidence_recovers(tmp_path):
    """transaction_risk_surge 首轮置信度 0.85 < 阈值 0.9，触发证据请求；
    补充扩展时间窗日志后二轮分析置信度回升至阈值以上，流水线不降级。"""
    orch = _make_orchestrator(
        tmp_path, negotiation=True,
        negotiation_overrides={"rca_confidence_threshold": 0.9},
    )
    report = orch.run("transaction_risk_surge")

    # 反馈环触发且解决，未走降级
    assert not report.degraded
    loop = report.negotiation["evidence_loop"]
    assert loop["triggered"] is True
    assert loop["rounds_used"] == 1
    assert loop["resolved"] is True
    assert loop["requests"][0]["missing_evidence"][0] == "extended_time_window_logs"

    # 二轮置信度达标，且证据链包含补充采集的第五维证据
    selected = report.selected_root_cause
    assert selected is not None
    assert selected.confidence >= 0.9
    sources = {ev.source for ev in selected.evidences}
    assert "supplemental" in sources

    # Trace：反馈环两个新增 span 均存在且挂在 RcaAgent span 之下
    trace = _load_trace(orch)
    spans = {s["name"]: s for s in trace["spans"]}
    assert "rca.evidence_request" in spans
    assert "rca.reanalysis" in spans
    by_id = {s["span_id"]: s for s in trace["spans"]}
    for name in ("rca.evidence_request", "rca.reanalysis"):
        parent = by_id[spans[name]["parent_span_id"]]
        assert parent["name"] == "agent.RcaAgent"

    # 审计：请求 -> 补充 -> 二轮分析三类事件齐全
    event_types = [e["event_type"] for e in _load_audit_events(orch)]
    for expected in ("evidence_request", "evidence_supplement", "rca_reanalysis"):
        assert expected in event_types

    # 时间线：Agent 间请求-响应以结构化 AgentMessage 留痕
    messages = [
        item["agent_message"] for item in report.timeline if "agent_message" in item
    ]
    message_types = {m["message_type"] for m in messages}
    assert {"evidence_request", "evidence_supplement"} <= message_types
    request = next(m for m in messages if m["message_type"] == "evidence_request")
    assert request["sender"] == "RcaAgent" and request["receiver"] == "orchestrator"


def test_evidence_loop_retry_cap_falls_back_to_manual(tmp_path):
    """container_oom 无 logs_extended.json：补充采集为空，二轮后置信度仍低于
    阈值 0.96，重试上限（1 轮）已到，走原有降级路径转人工。"""
    orch = _make_orchestrator(
        tmp_path, negotiation=True,
        negotiation_overrides={"rca_confidence_threshold": 0.96},
    )
    report = orch.run("container_oom")

    assert report.degraded
    assert any("转人工" in note for note in report.degradation_notes)
    assert report.selected_root_cause is None
    assert report.remediation_plan is None, "无可信根因时不应生成自动方案"

    loop = report.negotiation["evidence_loop"]
    assert loop["triggered"] is True
    assert loop["rounds_used"] == 1, "重试上限 1 轮，不应死循环"
    assert loop["resolved"] is False

    event_types = [e["event_type"] for e in _load_audit_events(orch)]
    assert "evidence_request" in event_types
    assert "rca_low_confidence_handoff" in event_types


# ---------------------------------------------------------------------------
# 机制 2：多根因候选 -> 多方案并行 -> 决策选择
# ---------------------------------------------------------------------------

def test_plan_negotiation_auto_select_and_alternatives(tmp_path):
    """container_oom 协商模式产出双候选（变更引入 0.95 vs 内存泄漏 0.85，
    差距 0.10 < 0.15）：并行生成两方案，自动选打分最优（回滚变更方案），
    未选中方案记入 alternative_plans。"""
    orch = _make_orchestrator(tmp_path, negotiation=True)
    report = orch.run("container_oom")

    assert not report.degraded
    neg = report.negotiation["plan_negotiation"]
    assert neg["triggered"] is True
    assert neg["candidate_count"] == 2
    assert neg["confidence_gap"] < 0.15
    assert neg["decision"]["mode"] == "auto"

    # 自动决策选中"回滚变更"方案（置信度更高 + 恢复更快）
    assert report.selected_root_cause.related_change_id == "CHG-20260723-0021"
    plan_actions = {step.action_type for step in report.remediation_plan.steps}
    assert "rollback_change" in plan_actions

    # 落选的"内存泄漏"假设方案完整记入 alternative_plans（评审可见证据）
    assert len(report.alternative_plans) == 1
    alt = report.alternative_plans[0]
    assert alt["rank"] == 2
    assert alt["score"] < neg["decision"]["chosen_score"]
    assert alt["root_cause_confidence"] == 0.85
    assert alt["score_breakdown"]["formula"]
    alt_actions = {step["action_type"] for step in alt["plan"]["steps"]}
    assert "rolling_restart" in alt_actions and "rollback_change" not in alt_actions

    # Trace：plan.negotiation span 下并行挂载 2 个 PlannerAgent span
    trace = _load_trace(orch)
    neg_span = next(s for s in trace["spans"] if s["name"] == "plan.negotiation")
    planner_children = [
        s for s in trace["spans"]
        if s["parent_span_id"] == neg_span["span_id"] and s["name"] == "agent.PlannerAgent"
    ]
    assert len(planner_children) == 2, "并行方案生成应产出 2 个兄弟 PlannerAgent span"

    # 审计：协商触发与决策事件齐全，决策依据（打分明细）完整留痕
    events = {e["event_type"]: e for e in _load_audit_events(orch)}
    assert "plan_negotiation" in events
    selection = events["plan_selection"]
    assert selection["mode"] == "auto"
    assert len(selection["options"]) == 2
    assert all("score" in opt and "risk_level" in opt for opt in selection["options"])

    # 时间线：方案提交与决策的结构化 AgentMessage 留痕
    message_types = {
        item["agent_message"]["message_type"]
        for item in report.timeline if "agent_message" in item
    }
    assert {"plan_proposal", "plan_decision"} <= message_types


def test_plan_negotiation_interactive_selector(tmp_path):
    """交互决策：plan_selector 人工选择 rank2 方案（内存泄漏假设）时，
    选定方案与 alternative_plans 相应对调。"""
    def choose_second(options):
        assert len(options) == 2
        return {"index": 1, "selector": "sre-oncall", "mode": "interactive",
                "reason": "怀疑变更只是诱因，优先按泄漏路径处置"}

    orch = _make_orchestrator(tmp_path, negotiation=True, plan_selector=choose_second)
    report = orch.run("container_oom")

    assert not report.degraded
    decision = report.negotiation["plan_negotiation"]["decision"]
    assert decision["mode"] == "interactive"
    assert decision["selector"] == "sre-oncall"

    # 人工选中泄漏假设方案：无变更关联、以滚动重启止血
    assert report.selected_root_cause.related_change_id is None
    plan_actions = {step.action_type for step in report.remediation_plan.steps}
    assert "rolling_restart" in plan_actions

    # 打分最优但未被人工选中的回滚方案进入 alternative_plans
    assert len(report.alternative_plans) == 1
    assert report.alternative_plans[0]["rank"] == 1
    assert "人工决策未选中" in report.alternative_plans[0]["not_selected_reason"]


def test_rank_plan_options_deterministic():
    """打分排序纯函数：确定性、降序、rank 连续。"""
    pairs = [
        {"candidate": {"confidence": 0.85, "hypothesis": "泄漏"},
         "plan": {"risk_level": "medium",
                  "steps": [{"action_type": "rolling_restart"},
                            {"action_type": "manual_followup"}]}},
        {"candidate": {"confidence": 0.95, "hypothesis": "变更"},
         "plan": {"risk_level": "medium",
                  "steps": [{"action_type": "rollback_change"},
                            {"action_type": "config_update"}]}},
    ]
    first = rank_plan_options([dict(p) for p in pairs])
    second = rank_plan_options([dict(p) for p in pairs])
    assert [o["score"] for o in first] == [o["score"] for o in second]
    assert first[0]["score"] > first[1]["score"]
    assert [o["rank"] for o in first] == [1, 2]
    assert first[0]["candidate"]["hypothesis"] == "变更"


# ---------------------------------------------------------------------------
# 默认模式回归守护：不开协商时行为与原流水线完全一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["container_oom", "transaction_risk_surge"])
def test_default_mode_has_no_negotiation_artifacts(tmp_path, scenario):
    orch = _make_orchestrator(tmp_path)
    report = orch.run(scenario)

    assert not report.degraded
    assert report.negotiation == {}
    assert report.alternative_plans == []
    assert all("agent_message" not in item for item in report.timeline)

    span_names = {s["name"] for s in _load_trace(orch)["spans"]}
    for forbidden in ("rca.evidence_request", "rca.reanalysis", "plan.negotiation"):
        assert forbidden not in span_names

    event_types = {e["event_type"] for e in _load_audit_events(orch)}
    for forbidden in ("evidence_request", "plan_negotiation", "plan_selection"):
        assert forbidden not in event_types
