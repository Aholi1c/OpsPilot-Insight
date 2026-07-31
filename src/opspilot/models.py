# -*- coding: utf-8 -*-
"""OpsPilot-Insight 核心数据模型（Pydantic）。

Agent / Skill / Orchestrator 之间传递的所有结构化数据都在这里定义，
保证跨组件的上下文传递是强类型、可序列化、可追溯的。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# 证据强度三档：strong=直接指向根因 / weak=相关但不充分 / missing=该维度无信号
EvidenceStrength = Literal["strong", "weak", "missing"]
RiskLevel = Literal["low", "medium", "high"]


def _now_iso() -> str:
    """本地时区 ISO8601 时间字符串。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Alert(BaseModel):
    """单条告警，兼容 Prometheus AlertManager webhook 的字段命名（camelCase 别名）。"""

    model_config = ConfigDict(populate_by_name=True)

    fingerprint: str
    status: str = "firing"
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    starts_at: str = Field(default="", alias="startsAt")
    ends_at: Optional[str] = Field(default=None, alias="endsAt")
    generator_url: Optional[str] = Field(default=None, alias="generatorURL")

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "unknown")

    @property
    def service(self) -> str:
        return self.labels.get("service", self.labels.get("job", "unknown"))

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "warning")


class AlertGroup(BaseModel):
    """按 (alertname, service) 聚合后的告警组。"""

    alertname: str
    service: str
    severity: str
    count: int
    first_starts_at: str
    summary: str = ""


class Incident(BaseModel):
    """由 AlertAgent 收敛出的事件对象（后续 Agent 的统一上下文入口）。"""

    incident_id: str
    title: str
    severity: str
    created_at: str = Field(default_factory=_now_iso)
    raw_alert_count: int = 0
    deduped_alert_count: int = 0
    alert_groups: List[AlertGroup] = Field(default_factory=list)
    affected_services: List[str] = Field(default_factory=list)
    impact: Dict[str, Any] = Field(default_factory=dict)
    summary: str = ""


class SkillResult(BaseModel):
    """Skill 执行结果的统一封装（成功/失败均返回，便于降级处理）。"""

    skill_name: str
    skill_version: str
    success: bool
    output: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: float = 0.0


class AgentMessage(BaseModel):
    """Agent 间通信的标准消息封装，携带 trace_id 以便全链路关联。"""

    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    sender: str
    receiver: str
    message_type: str
    content: Dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    timestamp: str = Field(default_factory=_now_iso)


class Evidence(BaseModel):
    """单条证据：来源维度 + 强度 + 描述 + 关键明细。

    supplemental 为协商模式下证据补充请求（evidence request loop）注入的
    补充采集证据（扩展时间窗日志 / 变更单详情），默认流程不产生。
    """

    source: Literal["logs", "metrics", "traces", "changes", "supplemental"]
    strength: EvidenceStrength
    description: str
    details: List[str] = Field(default_factory=list)


class RootCauseCandidate(BaseModel):
    """根因候选：假设 + 置信度 + 证据链。"""

    category: str
    hypothesis: str
    service: Optional[str] = None  # 主嫌疑服务
    confidence: float = 0.0
    evidences: List[Evidence] = Field(default_factory=list)
    related_change_id: Optional[str] = None


class RemediationStep(BaseModel):
    """修复/回滚计划中的单个步骤。"""

    order: int
    action: str
    action_type: str = ""  # 动作类型（对齐 config/action_whitelist.yaml 的白名单键）
    command: Optional[str] = None
    expected_effect: str = ""


class RemediationPlan(BaseModel):
    """由 PlannerAgent 生成的修复方案（含风险等级与回滚计划）。"""

    plan_id: str
    root_cause_category: str
    risk_level: RiskLevel
    approval_required: bool = True
    steps: List[RemediationStep] = Field(default_factory=list)
    rollback_plan: List[RemediationStep] = Field(default_factory=list)
    runbook_references: List[Dict[str, str]] = Field(default_factory=list)  # RunbookRag 检索依据
    narrative: str = ""


class ApprovalRecord(BaseModel):
    """人工审批记录（who / when / decision），同步写入审计日志。"""

    required: bool = True
    approved: bool = False
    approver: str = ""
    decided_at: str = Field(default_factory=_now_iso)
    mode: str = "auto"  # auto / interactive / policy
    reason: str = ""


class ActionExecution(BaseModel):
    """单个修复动作的执行记录（含幂等键与回滚检查点）。"""

    order: int
    action: str
    action_type: str = ""
    command: Optional[str] = None
    idempotency_key: str = ""
    status: Literal[
        "success", "failed", "skipped_idempotent", "rejected_whitelist", "manual",
    ] = "success"
    message: str = ""
    checkpoint_id: Optional[str] = None
    fallback: bool = False  # 是否为首选动作失败回滚后的备选动作
    duration_ms: float = 0.0


class RollbackRecord(BaseModel):
    """回滚记录：执行失败后按检查点逆序恢复现场。"""

    checkpoint_id: str
    action_order: int
    action: str
    status: str = "success"
    message: str = ""


class ExecutionResult(BaseModel):
    """ExecutorAgent 的执行结果：审批 + 逐动作结果 + 回滚记录。"""

    plan_id: str
    executed: bool = False
    status: str = "skipped"  # success / success_with_rollback / failed / rejected / skipped
    approval: Optional[ApprovalRecord] = None
    risk_assessment: Dict[str, Any] = Field(default_factory=dict)
    actions: List[ActionExecution] = Field(default_factory=list)
    rollback_performed: bool = False
    rollbacks: List[RollbackRecord] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class VerificationCheck(BaseModel):
    """单项指标的恢复验证：基线 vs 故障时 vs 修复后。"""

    metric: str
    service: str
    unit: str = ""
    baseline: float = 0.0
    before_fix: float = 0.0
    after_fix: float = 0.0
    recovered: bool = False


class VerificationResult(BaseModel):
    """VerifierAgent 的恢复验证结论。"""

    passed: bool = False
    alerts_cleared: bool = False
    checks: List[VerificationCheck] = Field(default_factory=list)
    summary: str = ""


class PostmortemReport(BaseModel):
    """事故复盘报告：时间线/根因/处置/效果/改进项，并沉淀为历史案例。"""

    incident_id: str
    title: str = ""
    # 协商模式下时间线条目可携带结构化 AgentMessage（value 为 dict），故用 Any
    timeline: List[Dict[str, Any]] = Field(default_factory=list)
    root_cause: str = ""
    actions_taken: List[str] = Field(default_factory=list)
    effect: str = ""
    improvements: List[str] = Field(default_factory=list)
    narrative: str = ""
    case_id: str = ""  # 沉淀到知识库的案例 ID（空表示未沉淀）


class IncidentReport(BaseModel):
    """最终诊断报告：一次流水线运行的全部结构化产物。"""

    report_id: str
    scenario: str
    generated_at: str = Field(default_factory=_now_iso)
    trace_id: str = ""
    incident: Incident
    root_cause_candidates: List[RootCauseCandidate] = Field(default_factory=list)
    selected_root_cause: Optional[RootCauseCandidate] = None
    similar_cases: List[Dict[str, Any]] = Field(default_factory=list)  # CaseRetrieval 命中的历史案例
    remediation_plan: Optional[RemediationPlan] = None
    execution_result: Optional[ExecutionResult] = None
    verification_result: Optional[VerificationResult] = None
    postmortem: Optional[PostmortemReport] = None
    degraded: bool = False
    degradation_notes: List[str] = Field(default_factory=list)
    # 协商模式（--negotiation）产物：默认模式下两者均为空，报告结论不受影响
    negotiation: Dict[str, Any] = Field(default_factory=dict)  # 反馈环与多方案协商的过程记录
    alternative_plans: List[Dict[str, Any]] = Field(default_factory=list)  # 未被选中的候选方案（含打分）
    # 协商模式下时间线条目可携带结构化 AgentMessage（value 为 dict），故用 Any
    timeline: List[Dict[str, Any]] = Field(default_factory=list)
