# -*- coding: utf-8 -*-
"""Orchestrator：串行编排 Alert -> RCA -> Planner -> Executor -> Verifier 的五段闭环。

- 结构化上下文传递：Agent 间通过 AgentMessage 传递 Pydantic 序列化数据；
- 异常捕获与降级：任一阶段失败均记录降级说明并继续产出部分报告；
- 协商模式（可选增强，默认关闭）：RCA 低置信度证据补充反馈环 +
  多根因候选多方案并行协商决策，全过程 Trace/审计留痕；
- 一次运行产出五类可观测产物：incident_report_*.json / trace_*.json /
  run_*.log / audit_*.jsonl / metrics_*.json。
"""
from __future__ import annotations

import contextvars
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .agents import AlertAgent, ExecutorAgent, PlannerAgent, RcaAgent, VerifierAgent
from .config import load_yaml
from .evaluation.cost import compute_cost_section, load_pricing
from .llm.base import LLMProvider, create_provider
from .mcp import build_adapters
from .models import AgentMessage, Incident, IncidentReport
from .negotiation import rank_plan_options
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
    "transaction_risk_surge": "支付平台异常交易激增（撞库攻击导致账号被盗，金融风控场景）",
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
        negotiation: Optional[bool] = None,
        negotiation_overrides: Optional[Dict[str, Any]] = None,
        plan_selector: Optional[Callable[..., Dict[str, Any]]] = None,
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
        # 协商模式开关（None=按 config/agents.yaml negotiation.enabled，默认关闭）
        # 与阈值覆盖（如 CLI --rca-threshold）；plan_selector 为多方案人工选择回调
        self.negotiation = negotiation
        self.negotiation_overrides = dict(negotiation_overrides or {})
        self.plan_selector = plan_selector
        self.llm = llm or create_provider()
        self.console = console
        # 最近一次运行的产物路径（便于入口脚本/测试引用）
        self.last_artifacts: Dict[str, Path] = {}

    def _negotiation_config(self) -> Dict[str, Any]:
        """合并协商配置：agents.yaml negotiation 段 <- 构造参数覆盖。"""
        cfg = dict(load_yaml(self.config_path).get("negotiation") or {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("rca_confidence_threshold", 0.6)
        cfg.setdefault("max_evidence_rounds", 1)
        cfg.setdefault("candidate_gap_threshold", 0.15)
        cfg.update(self.negotiation_overrides)
        if self.negotiation is not None:
            cfg["enabled"] = bool(self.negotiation)
        return cfg

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
        timeline: List[Dict[str, Any]] = []
        # 协商模式装配（默认 enabled=False，全部旁路，行为与原流水线一致）
        neg_cfg = self._negotiation_config()
        neg_enabled = bool(neg_cfg.get("enabled"))
        negotiation_section: Dict[str, Any] = {}
        alternative_plans: List[Dict[str, Any]] = []

        def _mark(event: str, message: Optional[AgentMessage] = None) -> None:
            """流水线时间线打点；协商事件附带结构化 AgentMessage（请求-响应留痕）。"""
            entry: Dict[str, Any] = {
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "event": event,
            }
            if message is not None:
                entry["agent_message"] = message.model_dump()
            timeline.append(entry)

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
                if neg_enabled:
                    stage_ctx = {**stage_ctx, "negotiation": neg_cfg}
                rca_result = rca_agent.handle(self._make_message("incident", stage_ctx, tracer))
                stage_ctx = rca_result.content
                # 协商机制 1：低置信度证据补充反馈环（重试上限防死循环）
                if neg_enabled:
                    stage_ctx = self._run_evidence_loop(
                        stage_ctx, neg_cfg, rca_agent, adapters, tracer, audit,
                        logger, _mark, negotiation_section,
                    )
                if stage_ctx.pop("low_confidence_handoff", False):
                    degraded = True
                    degradation_notes.append(
                        "根因置信度经证据补充后仍低于阈值"
                        f"（{neg_cfg.get('rca_confidence_threshold')}），转人工排查"
                    )
            except Exception as exc:  # noqa: BLE001 —— 降级路径
                logger.error(f"RcaAgent 阶段失败，进入降级模式: {exc}")
                degraded = True
                degradation_notes.append(f"根因分析阶段失败（{exc}），报告不含根因结论，建议人工排查")
                stage_ctx = {**stage_ctx, "root_cause_candidates": [],
                             "selected_root_cause": None, "analysis": ""}
            stage_ctx.pop("negotiation", None)
            _mark("RcaAgent 结束")

            # ---- 阶段 3：修复规划（失败则报告无方案，转人工）----
            # 协商机制 2：多根因候选置信度接近时，多方案并行生成 + 决策选择
            plan_negotiated = False
            if neg_enabled and stage_ctx.get("selected_root_cause"):
                try:
                    plan_negotiated, alternative_plans, stage_ctx = self._run_plan_negotiation(
                        stage_ctx, neg_cfg, planner_agent, tracer, audit,
                        logger, _mark, negotiation_section,
                    )
                except Exception as exc:  # noqa: BLE001 —— 协商失败退回默认单方案路径
                    logger.warning(f"多方案协商失败（{exc}），退回默认单方案规划路径")
                    plan_negotiated, alternative_plans = False, []
            if not plan_negotiated:
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
            negotiation=negotiation_section,
            alternative_plans=alternative_plans,
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

    # ------------------------------------------------------------------
    # 协商机制 1：RCA 低置信度证据补充反馈环
    # ------------------------------------------------------------------
    def _run_evidence_loop(
        self,
        stage_ctx: Dict[str, Any],
        neg_cfg: Dict[str, Any],
        rca_agent: RcaAgent,
        adapters: Dict[str, Any],
        tracer: Tracer,
        audit: AuditLog,
        logger: JsonLogger,
        _mark: Callable[..., None],
        negotiation_section: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理 RcaAgent 的证据补充请求：MCP 补充采集 -> 注入上下文 -> 二轮分析。

        重试上限 max_evidence_rounds（默认 1 轮）防死循环；全过程记录
        Trace（rca.evidence_request / rca.reanalysis 由 RcaAgent 侧产生）、
        审计事件与结构化 AgentMessage（pipeline_timeline）。
        """
        max_rounds = int(neg_cfg.get("max_evidence_rounds", 1))
        loop_record: Dict[str, Any] = {"triggered": False, "rounds_used": 0, "requests": []}
        rounds = 0
        while stage_ctx.pop("needs_more_evidence", False) and rounds < max_rounds:
            rounds += 1
            missing = list(stage_ctx.pop("missing_evidence", []))
            loop_record["triggered"] = True
            loop_record["requests"].append({"round": rounds, "missing_evidence": missing})
            _mark(
                f"RcaAgent 请求补充证据（第 {rounds} 轮）: {', '.join(missing)}",
                message=AgentMessage(
                    sender=rca_agent.name, receiver="orchestrator",
                    message_type="evidence_request",
                    content={"missing_evidence": missing, "round": rounds},
                    trace_id=tracer.trace_id,
                ),
            )

            # 通过 MCP 适配器补充采集（Mock 适配器提供扩展数据，无扩展数据则为空）
            extended_logs = adapters["logging"].query_extended_logs()
            change_details = (
                adapters["change"].get_change_details()
                if "change_ticket_details" in missing else []
            )
            supplemental = {"extended_logs": extended_logs, "change_details": change_details}
            audit.record(
                "evidence_supplement",
                round=rounds, missing_evidence=missing,
                extended_log_count=len(extended_logs),
                change_detail_count=len(change_details),
            )
            logger.info(
                f"  ⇄ Orchestrator 补充采集完成: 扩展时间窗日志 {len(extended_logs)} 条，"
                f"变更单详情 {len(change_details)} 条，注入上下文进行第 {rounds + 1} 轮分析"
            )
            _mark(
                f"Orchestrator 注入补充证据（扩展日志 {len(extended_logs)} 条 / "
                f"变更详情 {len(change_details)} 条）",
                message=AgentMessage(
                    sender="orchestrator", receiver=rca_agent.name,
                    message_type="evidence_supplement",
                    content={"extended_log_count": len(extended_logs),
                             "change_detail_count": len(change_details)},
                    trace_id=tracer.trace_id,
                ),
            )

            # 二轮分析（rca.reanalysis span 由 RcaAgent 内部创建）
            rca_result = rca_agent.handle(self._make_message("incident", {
                **stage_ctx,
                "negotiation": neg_cfg,
                "supplemental_evidence": supplemental,
            }, tracer))
            stage_ctx = rca_result.content

        loop_record["rounds_used"] = rounds
        selected = stage_ctx.get("selected_root_cause")
        loop_record["resolved"] = bool(selected)
        if loop_record["triggered"]:
            audit.record(
                "rca_reanalysis",
                rounds_used=rounds, resolved=loop_record["resolved"],
                confidence=(selected or {}).get("confidence"),
            )
        negotiation_section.update({
            "enabled": True,
            "config": {k: v for k, v in neg_cfg.items() if k != "enabled"},
            "evidence_loop": loop_record,
        })
        return stage_ctx

    # ------------------------------------------------------------------
    # 协商机制 2：多根因候选 -> 多方案并行生成 -> 决策选择
    # ------------------------------------------------------------------
    def _run_plan_negotiation(
        self,
        stage_ctx: Dict[str, Any],
        neg_cfg: Dict[str, Any],
        planner_agent: PlannerAgent,
        tracer: Tracer,
        audit: AuditLog,
        logger: JsonLogger,
        _mark: Callable[..., None],
        negotiation_section: Dict[str, Any],
    ):
        """置信度接近的多根因候选并行生成方案，按打分/人工决策选定其一。

        返回 (是否走了协商路径, alternative_plans, 新 stage_ctx)。
        """
        gap_threshold = float(neg_cfg.get("candidate_gap_threshold", 0.15))
        candidates = stage_ctx.get("root_cause_candidates") or []
        if len(candidates) < 2:
            return False, [], stage_ctx
        top_confidence = candidates[0]["confidence"]
        contenders = [
            c for c in candidates if top_confidence - c["confidence"] < gap_threshold
        ]
        if len(contenders) < 2:
            return False, [], stage_ctx

        gap = round(top_confidence - contenders[1]["confidence"], 4)
        logger.info(
            f"  ⚖ 检测到 {len(contenders)} 个置信度接近的根因候选（差距 {gap} < "
            f"{gap_threshold}），进入多方案协商模式"
        )
        audit.record(
            "plan_negotiation",
            candidate_count=len(contenders), confidence_gap=gap,
            gap_threshold=gap_threshold,
            hypotheses=[c["hypothesis"] for c in contenders],
        )
        _mark(f"多方案协商开始（{len(contenders)} 个候选并行规划）")

        # 并行方案生成：每个候选一个独立 PlannerAgent 会话，
        # span 结构为 plan.negotiation 下的多个并行 agent.PlannerAgent 兄弟节点
        with tracer.start_span("plan.negotiation", kind="INTERNAL", attributes={
            "negotiation.candidate_count": len(contenders),
            "negotiation.confidence_gap": gap,
            "negotiation.gap_threshold": gap_threshold,
        }):
            with ThreadPoolExecutor(max_workers=len(contenders)) as pool:
                futures = []
                for candidate in contenders:
                    message = self._make_message(
                        "root_cause", {**stage_ctx, "selected_root_cause": candidate}, tracer,
                    )
                    ctx = contextvars.copy_context()
                    futures.append(pool.submit(ctx.run, planner_agent.handle, message))
                results = [f.result() for f in futures]

        pairs = []
        for candidate, result in zip(contenders, results):
            plan = result.content.get("remediation_plan")
            if plan:
                pairs.append({"candidate": candidate, "plan": plan})
                _mark(
                    f"PlannerAgent 提交候选方案 {plan.get('plan_id')}"
                    f"（假设: {candidate['hypothesis'][:40]}…）",
                    message=AgentMessage(
                        sender=planner_agent.name, receiver="orchestrator",
                        message_type="plan_proposal",
                        content={"plan_id": plan.get("plan_id"),
                                 "root_cause_hypothesis": candidate["hypothesis"],
                                 "risk_level": plan.get("risk_level")},
                        trace_id=tracer.trace_id,
                    ),
                )
        if len(pairs) < 2:
            return False, [], stage_ctx

        # 决策：风险 × 置信度 × 预估恢复时长打分排序；
        # 交互模式由人工从多方案中选择（plan_selector），--auto-approve 自动选最优
        options = rank_plan_options(pairs)
        chosen_index, mode, selector, reason = 0, "auto", "score-ranking", "自动选择打分最优方案"
        if self.plan_selector is not None:
            decision = self.plan_selector(options)
            chosen_index = min(int(decision.get("index", 0)), len(options) - 1)
            mode = decision.get("mode", "interactive")
            selector = decision.get("selector", "unknown")
            reason = decision.get("reason", "")
        chosen = options[chosen_index]
        others = [o for i, o in enumerate(options) if i != chosen_index]

        audit.record(
            "plan_selection",
            mode=mode, selector=selector, reason=reason,
            chosen_plan_id=chosen["plan"].get("plan_id"),
            options=[{
                "rank": o["rank"], "plan_id": o["plan"].get("plan_id"),
                "score": o["score"], **o["score_breakdown"],
            } for o in options],
        )
        logger.info(
            f"  ✔ 多方案决策完成（{mode}）: 选定 {chosen['plan'].get('plan_id')}"
            f"（score={chosen['score']}），备选 {len(others)} 个记入 alternative_plans"
        )
        _mark(
            f"多方案决策: 选定 {chosen['plan'].get('plan_id')}（score={chosen['score']}，{mode}）",
            message=AgentMessage(
                sender="orchestrator", receiver=planner_agent.name,
                message_type="plan_decision",
                content={"chosen_plan_id": chosen["plan"].get("plan_id"),
                         "score": chosen["score"], "mode": mode, "selector": selector},
                trace_id=tracer.trace_id,
            ),
        )

        alternative_plans = [{
            "rank": o["rank"],
            "score": o["score"],
            "score_breakdown": o["score_breakdown"],
            "root_cause_hypothesis": o["candidate"]["hypothesis"],
            "root_cause_confidence": o["candidate"]["confidence"],
            "plan": o["plan"],
            "not_selected_reason": f"打分 {o['score']} 低于选定方案 {chosen['score']}"
            if mode == "auto" else f"人工决策未选中（{selector}）",
        } for o in others]

        negotiation_section.setdefault("enabled", True)
        negotiation_section.setdefault(
            "config", {k: v for k, v in neg_cfg.items() if k != "enabled"})
        negotiation_section["plan_negotiation"] = {
            "triggered": True,
            "candidate_count": len(contenders),
            "confidence_gap": gap,
            "decision": {
                "mode": mode, "selector": selector, "reason": reason,
                "chosen_plan_id": chosen["plan"].get("plan_id"),
                "chosen_score": chosen["score"],
                "chosen_hypothesis": chosen["candidate"]["hypothesis"],
            },
        }
        new_ctx = {
            **stage_ctx,
            "selected_root_cause": chosen["candidate"],
            "remediation_plan": chosen["plan"],
        }
        return True, alternative_plans, new_ctx

    @staticmethod
    def _make_message(message_type: str, content: Dict[str, Any], tracer: Tracer):
        """构造 Orchestrator 发往 Agent 的标准消息。"""
        from .models import AgentMessage

        return AgentMessage(
            sender="orchestrator", receiver="", message_type=message_type,
            content=content, trace_id=tracer.trace_id,
        )
