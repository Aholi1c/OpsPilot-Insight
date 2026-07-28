# -*- coding: utf-8 -*-
"""Orchestrator：串行编排 Alert -> RCA -> Planner -> Executor -> Verifier 的五段闭环。

- 结构化上下文传递：Agent 间通过 AgentMessage 传递 Pydantic 序列化数据；
- 异常捕获与降级：任一阶段失败均记录降级说明并继续产出部分报告；
- 一次运行产出五类可观测产物：incident_report_*.json / trace_*.json /
  run_*.log / audit_*.jsonl / metrics_*.json。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .agents import AlertAgent, ExecutorAgent, PlannerAgent, RcaAgent, VerifierAgent
from .config import load_yaml
from .evaluation.cost import compute_cost_section, load_pricing
from .llm.base import LLMProvider, create_provider
from .mcp import build_adapters
from .models import Incident, IncidentReport
from .observability import AuditLog, JsonLogger, MetricsCollector, Tracer
from .rag import KnowledgeStore, create_retriever
from .skills import (
    AlertFusionSkill,
    CaseRetrievalSkill,
    ImpactMappingSkill,
    LogTraceRcaSkill,
    PostmortemSkill,
    RecoveryVerifySkill,
    RiskGuardSkill,
    RunbookRagSkill,
    SafeExecuteSkill,
    SkillContext,
)

# 项目根目录（src/opspilot/orchestrator.py 的上两级再上一级）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 场景描述（--list-scenarios 展示用）
SCENARIO_DESCRIPTIONS = {
    "db_pool_exhaustion": "订单服务数据库连接池耗尽（变更引入连接泄漏）",
    "container_oom": "支付服务容器 OOMKilled（变更引入缓存配置错误）",
    "network_latency": "网关到用户服务网络延迟劣化（网络 ACL 变更）",
}


class Orchestrator:
    """串行编排器：装配运行时依赖并驱动五个 Agent 完成一次自愈闭环。"""

    def __init__(
        self,
        project_root: Union[str, Path, None] = None,
        output_dir: Union[str, Path, None] = None,
        llm: Optional[LLMProvider] = None,
        console: bool = True,
        approval_handler: Optional[Callable[..., Dict[str, Any]]] = None,
        knowledge_dir: Union[str, Path, None] = None,
    ):
        self.project_root = Path(project_root) if project_root else _PROJECT_ROOT
        self.scenarios_dir = self.project_root / "examples" / "scenarios"
        self.output_dir = Path(output_dir) if output_dir else self.project_root / "output"
        self.config_path = self.project_root / "config" / "agents.yaml"
        self.whitelist_path = self.project_root / "config" / "action_whitelist.yaml"
        self.pricing_path = self.project_root / "config" / "pricing.yaml"
        # 知识库目录可注入（测试传临时拷贝，避免案例沉淀污染仓库种子数据）
        self.knowledge_dir = (
            Path(knowledge_dir) if knowledge_dir else _PROJECT_ROOT / "data" / "knowledge"
        )
        # 审批回调：None 时自动批准（--auto-approve / 测试），run_demo 可注入交互式审批
        self.approval_handler = approval_handler
        self.llm = llm or create_provider()
        self.console = console
        # 最近一次运行的产物路径（便于入口脚本/测试引用）
        self.last_artifacts: Dict[str, Path] = {}

    def list_scenarios(self) -> List[Dict[str, str]]:
        """扫描场景目录，返回可运行场景清单。"""
        scenarios = []
        for path in sorted(self.scenarios_dir.iterdir()):
            if path.is_dir() and (path / "alerts.json").exists():
                scenarios.append({
                    "name": path.name,
                    "description": SCENARIO_DESCRIPTIONS.get(path.name, "（无描述）"),
                })
        return scenarios

    def run(self, scenario: str) -> IncidentReport:
        """运行一个场景的完整诊断流水线，返回结构化报告。"""
        scenario_dir = self.scenarios_dir / scenario
        if not scenario_dir.is_dir():
            available = [s["name"] for s in self.list_scenarios()]
            raise ValueError(f"未知场景: {scenario}（可用: {available}）")

        run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # ---- 装配运行时：Tracer / Logger / 审计 / 指标 / MCP 适配器 / RAG / Skill / Agent ----
        tracer = Tracer()
        logger = JsonLogger(self.output_dir / f"run_{run_ts}.log", tracer, console=self.console)
        audit = AuditLog(self.output_dir / f"audit_{run_ts}.jsonl", tracer)
        metrics = MetricsCollector(trace_id=tracer.trace_id)
        adapters = build_adapters(scenario_dir)
        whitelist = load_yaml(self.whitelist_path).get("whitelist", {})
        knowledge_store = KnowledgeStore(self.knowledge_dir)
        retriever = create_retriever(knowledge_store)
        skill_context = SkillContext(
            tracer=tracer, logger=logger, adapters=adapters, scenario=scenario,
            audit=audit, metrics=metrics,
            extras={
                "action_whitelist": whitelist,
                "retriever": retriever,
                "knowledge_store": knowledge_store,
                "approval_handler": self.approval_handler,
                "idempotency_registry": set(),
            },
        )
        skills = {
            s.name: s for s in (
                AlertFusionSkill(), ImpactMappingSkill(), LogTraceRcaSkill(),
                SafeExecuteSkill(), RiskGuardSkill(), RecoveryVerifySkill(),
                PostmortemSkill(), CaseRetrievalSkill(), RunbookRagSkill(),
            )
        }
        agent_configs = load_yaml(self.config_path).get("agents", {})
        common_deps = dict(llm=self.llm, tracer=tracer, logger=logger,
                           skills=skills, skill_context=skill_context)
        alert_agent = AlertAgent(agent_configs.get("alert_agent", {}), **common_deps)
        rca_agent = RcaAgent(agent_configs.get("rca_agent", {}), **common_deps)
        planner_agent = PlannerAgent(agent_configs.get("planner_agent", {}), **common_deps)
        executor_agent = ExecutorAgent(agent_configs.get("executor_agent", {}), **common_deps)
        verifier_agent = VerifierAgent(agent_configs.get("verifier_agent", {}), **common_deps)

        raw_alerts = json.loads((scenario_dir / "alerts.json").read_text(encoding="utf-8"))["alerts"]

        degraded = False
        degradation_notes: List[str] = []
        timeline: List[Dict[str, str]] = []

        def _mark(event: str) -> None:
            timeline.append({"time": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event})

        logger.info(f"════ OpsPilot-Insight 流水线启动 ════", scenario=scenario, trace_id=tracer.trace_id,
                    rag_backend=retriever.backend_name)
        pipeline_started = time.perf_counter()

        with tracer.start_span("pipeline.run", kind="INTERNAL",
                               attributes={"scenario": scenario, "llm.provider": self.llm.provider_name}):
            # ---- 阶段 1：告警接入（失败则整体降级为最小事件对象）----
            _mark("AlertAgent 开始")
            try:
                alert_result = alert_agent.handle(self._make_message("incident_request", {
                    "alerts": raw_alerts,
                }, tracer))
                stage_ctx = alert_result.content
            except Exception as exc:  # noqa: BLE001 —— 降级路径
                logger.error(f"AlertAgent 阶段失败，进入降级模式: {exc}")
                degraded = True
                degradation_notes.append(f"告警接入阶段失败（{exc}），事件对象为最小降级版本")
                stage_ctx = {
                    "incident": Incident(
                        incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
                        title=f"{scenario} 事件（降级模式）",
                        severity="unknown",
                        raw_alert_count=len(raw_alerts),
                        affected_services=[],
                        summary="告警接入阶段失败，未能完成聚合与影响面评估",
                    ).model_dump(),
                    "first_alert_at": "",
                }
            _mark("AlertAgent 结束")

            # ---- 阶段 2：根因分析（失败则报告无根因，转人工）----
            _mark("RcaAgent 开始")
            try:
                rca_result = rca_agent.handle(self._make_message("incident", stage_ctx, tracer))
                stage_ctx = rca_result.content
            except Exception as exc:  # noqa: BLE001 —— 降级路径
                logger.error(f"RcaAgent 阶段失败，进入降级模式: {exc}")
                degraded = True
                degradation_notes.append(f"根因分析阶段失败（{exc}），报告不含根因结论，建议人工排查")
                stage_ctx = {**stage_ctx, "root_cause_candidates": [],
                             "selected_root_cause": None, "analysis": ""}
            _mark("RcaAgent 结束")

            # ---- 阶段 3：修复规划（失败则报告无方案，转人工）----
            _mark("PlannerAgent 开始")
            try:
                plan_result = planner_agent.handle(self._make_message("root_cause", stage_ctx, tracer))
                stage_ctx = plan_result.content
            except Exception as exc:  # noqa: BLE001 —— 降级路径
                logger.error(f"PlannerAgent 阶段失败，进入降级模式: {exc}")
                degraded = True
                degradation_notes.append(f"修复规划阶段失败（{exc}），报告不含修复方案，建议人工制定")
                stage_ctx = {**stage_ctx, "remediation_plan": None}
            _mark("PlannerAgent 结束")

            # ---- 阶段 4：安全执行（失败则报告无执行结果，转人工）----
            _mark("ExecutorAgent 开始")
            try:
                exec_result = executor_agent.handle(
                    self._make_message("remediation_plan", stage_ctx, tracer))
                stage_ctx = exec_result.content
            except Exception as exc:  # noqa: BLE001 —— 降级路径
                logger.error(f"ExecutorAgent 阶段失败，进入降级模式: {exc}")
                degraded = True
                degradation_notes.append(f"安全执行阶段失败（{exc}），报告不含执行结果，建议人工执行")
                stage_ctx = {**stage_ctx, "execution_result": None}
            _mark("ExecutorAgent 结束")

            # ---- 阶段 5：恢复验证与复盘（失败则报告无验证/复盘段落）----
            _mark("VerifierAgent 开始")
            try:
                verify_result = verifier_agent.handle(self._make_message(
                    "execution_result",
                    {**stage_ctx, "pipeline_timeline": list(timeline)}, tracer))
                stage_ctx = verify_result.content
            except Exception as exc:  # noqa: BLE001 —— 降级路径
                logger.error(f"VerifierAgent 阶段失败，进入降级模式: {exc}")
                degraded = True
                degradation_notes.append(f"验证复盘阶段失败（{exc}），报告不含验证与复盘结论，建议人工确认恢复效果")
                stage_ctx = {**stage_ctx, "verification_result": None, "postmortem": None}
            _mark("VerifierAgent 结束")

        metrics.record_pipeline((time.perf_counter() - pipeline_started) * 1000)

        # ---- 成本三维分解与预算控制（超预算不中断流程，仅记审计事件并在报告/看板标红）----
        pricing = load_pricing(self.pricing_path)
        cost_section = compute_cost_section(metrics.export(), tracer.export(), pricing)
        metrics.set_cost(cost_section)
        if cost_section["budget"]["exceeded"]:
            audit.record(
                "budget_alert",
                total_cost=cost_section["total_cost"],
                budget_limit=cost_section["budget"]["limit"],
                usage_ratio=cost_section["budget"]["usage_ratio"],
                currency=cost_section["currency"],
            )
            logger.warning(
                f"⚠ 本次事故处理成本 {cost_section['total_cost']} 超出预算上限 "
                f"{cost_section['budget']['limit']}（{cost_section['currency']}）",
            )

        # ---- 汇总报告并落盘 ----
        report = IncidentReport(
            report_id=f"RPT-{uuid.uuid4().hex[:8].upper()}",
            scenario=scenario,
            trace_id=tracer.trace_id,
            incident=stage_ctx["incident"],
            root_cause_candidates=stage_ctx.get("root_cause_candidates", []),
            selected_root_cause=stage_ctx.get("selected_root_cause"),
            similar_cases=stage_ctx.get("similar_cases", []),
            remediation_plan=stage_ctx.get("remediation_plan"),
            execution_result=stage_ctx.get("execution_result"),
            verification_result=stage_ctx.get("verification_result"),
            postmortem=stage_ctx.get("postmortem"),
            degraded=degraded,
            degradation_notes=degradation_notes,
            timeline=timeline,
        )

        report_path = self.output_dir / f"incident_report_{scenario}_{run_ts}.json"
        report_path.write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        trace_path = tracer.export_json(self.output_dir / f"trace_{tracer.trace_id}.json")
        metrics_path = metrics.export_json(self.output_dir / f"metrics_{tracer.trace_id}.json")
        audit.close()
        self.last_artifacts = {
            "report": report_path,
            "trace": trace_path,
            "log": self.output_dir / f"run_{run_ts}.log",
            "audit": audit.path,
            "metrics": metrics_path,
        }
        logger.info(
            "════ 流水线结束，产物已落盘 ════",
            report=str(report_path.name), trace=str(trace_path.name),
            audit=str(audit.path.name), metrics=str(metrics_path.name), degraded=degraded,
        )
        logger.close()
        return report

    @staticmethod
    def _make_message(message_type: str, content: Dict[str, Any], tracer: Tracer):
        """构造 Orchestrator 发往 Agent 的标准消息。"""
        from .models import AgentMessage

        return AgentMessage(
            sender="orchestrator", receiver="", message_type=message_type,
            content=content, trace_id=tracer.trace_id,
        )
