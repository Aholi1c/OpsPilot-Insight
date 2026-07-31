# -*- coding: utf-8 -*-
"""金融风控场景（transaction_risk_surge）端到端测试：验证 Skill 跨行业复用。

断言点：
1. 五段闭环在金融场景完整跑通（非运维行业数据，Skill 核心逻辑零修改）；
2. AlertFusion 复用：重复 fingerprint 告警去重（5 条原始 -> 4 条去重）；
3. ImpactMapping 复用：trace 含 api-gateway 入口，判定用户可感知；
4. LogTraceRca 复用：识别撞库攻击根因（外部攻击，无内部变更关联）；
5. RiskGuard/SafeExecute 复用：高风险方案强制审批，动作全部通过白名单校验并携带回滚检查点；
6. RecoveryVerify 复用：金融业务指标（交易成功率/欺诈告警量）恢复判定通过；
7. RunbookRag 复用：命中金融场景 Runbook RB-014。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

# src 路径由 tests/conftest.py 统一注入；此处仅保留项目根用于定位数据文件
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

from opspilot.orchestrator import Orchestrator  # noqa: E402

SCENARIO = "transaction_risk_surge"


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """模块级共享：金融场景完整跑一遍五段闭环（知识库临时拷贝防污染）。"""
    root = tmp_path_factory.mktemp("finance")
    knowledge_dir = root / "knowledge"
    shutil.copytree(_PROJECT_ROOT / "data" / "knowledge", knowledge_dir)
    orchestrator = Orchestrator(output_dir=root / "output", console=False,
                                knowledge_dir=knowledge_dir)
    return orchestrator.run(SCENARIO)


def test_pipeline_completes_without_degradation(report):
    assert report.scenario == SCENARIO
    assert not report.degraded, f"金融场景不应触发降级: {report.degradation_notes}"
    # 五段闭环各阶段产物齐备
    assert report.incident is not None
    assert report.selected_root_cause is not None
    assert report.remediation_plan is not None
    assert report.execution_result is not None
    assert report.verification_result is not None


def test_alert_fusion_dedup_reused(report):
    """AlertFusion 零修改复用：金融告警按 fingerprint 去重、按 (alertname, service) 聚合。"""
    incident = report.incident
    assert incident.raw_alert_count == 5
    assert incident.deduped_alert_count == 4, "重复推送的 AbnormalTransactionSurge 应被去重"
    assert {"risk-engine", "account-service", "payment-gateway"} <= set(incident.affected_services)


def test_impact_mapping_user_facing(report):
    """ImpactMapping 零修改复用：交易链路经 api-gateway 入口，判定用户可感知。"""
    impact = report.incident.impact
    assert impact.get("user_facing") is True
    assert impact.get("blast_radius")


def test_rca_identifies_credential_stuffing_attack(report):
    """LogTraceRca 复用：识别撞库攻击（外部攻击），不误绑无关内部变更。"""
    selected = report.selected_root_cause
    assert selected.category == SCENARIO
    assert selected.service == "risk-engine"
    assert selected.confidence > 0.5
    assert "撞库" in selected.hypothesis and "账号被盗" in selected.hypothesis
    # 窗口内仅有无关变更（marketing-portal），不应被判定为根因关联变更
    assert selected.related_change_id is None
    # 证据链覆盖日志/指标/链路/变更四个维度
    sources = {ev.source for ev in selected.evidences}
    assert {"logs", "metrics", "traces", "changes"} <= sources


def test_plan_uses_finance_actions_with_approval(report):
    """RiskGuard 复用：冻结账户属高风险动作，方案必须审批；动作类型对齐金融白名单。"""
    plan = report.remediation_plan
    assert plan.risk_level == "high"
    assert plan.approval_required is True
    assert [s.action_type for s in plan.steps] == ["freeze_account", "trigger_2fa", "notify_team"]
    assert plan.rollback_plan, "冻结类动作必须有解冻回滚计划"
    # RunbookRag 命中金融场景处置手册
    assert any(r["id"] == "RB-014" for r in plan.runbook_references)


def test_safe_execute_whitelist_and_checkpoints(report):
    """SafeExecute 零修改复用：白名单校验通过、逐动作携带幂等键与回滚检查点。"""
    execution = report.execution_result
    assert execution.executed and execution.status == "success"
    assert not execution.rollback_performed
    actions = execution.actions
    assert len(actions) == 3
    for action in actions:
        assert action.status == "success"
        assert action.idempotency_key, "动作应携带幂等键"
        assert action.checkpoint_id, "动作应登记回滚检查点"


def test_recovery_verify_finance_metrics(report):
    """RecoveryVerify 零修改复用：金融业务指标恢复判定（成功率回升 + 欺诈告警回落）。"""
    verification = report.verification_result
    assert verification.passed is True
    assert verification.alerts_cleared is True
    checked = {c.metric for c in verification.checks}
    assert "transaction_success_rate_percent" in checked
    assert "fraud_alert_count_per_min" in checked
    assert all(c.recovered for c in verification.checks)


def test_expected_json_alignment(report):
    """运行结果与人工校准答案（expected.json）对齐，保证评测可满分。"""
    expected = json.loads(
        (_PROJECT_ROOT / "examples" / "scenarios" / SCENARIO / "expected.json")
        .read_text(encoding="utf-8")
    )
    hypothesis = report.selected_root_cause.hypothesis
    assert all(kw in hypothesis for kw in expected["root_cause_keywords"])
    assert report.selected_root_cause.related_change_id == expected["related_change_id"]
    actual_types = [s.action_type for s in report.remediation_plan.steps]
    assert actual_types == expected["expected_action_types"]
