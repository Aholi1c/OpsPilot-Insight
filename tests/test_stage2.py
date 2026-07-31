# -*- coding: utf-8 -*-
"""阶段 2 测试：五段闭环 / 安全执行边界 / RAG 检索 / 案例沉淀。

覆盖点：
1. 五段流水线 e2e（3 场景 × 自动审批）：执行/验证/复盘段落齐全，Trace 覆盖 5 个 Agent；
2. 白名单拒绝：不在白名单的动作类型被拒绝并记录审计事件；
3. 幂等重复执行：同一动作重复执行直接跳过；
4. 执行失败自动回滚：network_latency 剧本"首动作失败 -> 回滚 -> 备选动作成功"；
5. RAG 检索相关性：查询"连接池"应命中对应 Runbook（RB-001）；
6. 案例沉淀幂等：同 incident_id 不重复写入知识库；
7. 审批拒绝路径：审批回调拒绝时不执行任何动作。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

# src 路径由 tests/conftest.py 统一注入；此处仅保留项目根用于定位数据文件
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

from opspilot.config import load_yaml  # noqa: E402
from opspilot.mcp import build_adapters  # noqa: E402
from opspilot.observability import AuditLog, JsonLogger, MetricsCollector, Tracer  # noqa: E402
from opspilot.orchestrator import Orchestrator  # noqa: E402
from opspilot.rag import KnowledgeStore, LocalRetriever  # noqa: E402
from opspilot.skills import SafeExecuteSkill, SkillContext  # noqa: E402

SCENARIOS = ["db_pool_exhaustion", "container_oom", "network_latency"]
ALL_AGENTS = ("AlertAgent", "RcaAgent", "PlannerAgent", "ExecutorAgent", "VerifierAgent")


@pytest.fixture()
def knowledge_dir(tmp_path) -> Path:
    """知识库临时拷贝：案例沉淀写入拷贝，不污染仓库种子数据。"""
    dst = tmp_path / "knowledge"
    shutil.copytree(_PROJECT_ROOT / "data" / "knowledge", dst)
    return dst


@pytest.fixture()
def orchestrator(tmp_path, knowledge_dir) -> Orchestrator:
    """approval_handler=None 即自动审批（--auto-approve / 测试路径）。"""
    return Orchestrator(output_dir=tmp_path / "output", console=False,
                        knowledge_dir=knowledge_dir)


def _make_skill_context(tmp_path, scenario: str = "db_pool_exhaustion"):
    """构造直接调用 Skill 所需的最小上下文（含审计与真实白名单）。"""
    tracer = Tracer()
    logger = JsonLogger(tmp_path / "run.log", tracer, console=False)
    audit = AuditLog(tmp_path / "audit.jsonl", tracer)
    adapters = build_adapters(_PROJECT_ROOT / "examples" / "scenarios" / scenario)
    whitelist = load_yaml(_PROJECT_ROOT / "config" / "action_whitelist.yaml")["whitelist"]
    return SkillContext(
        tracer=tracer, logger=logger, adapters=adapters, scenario=scenario,
        audit=audit, metrics=MetricsCollector(trace_id=tracer.trace_id),
        extras={"action_whitelist": whitelist, "idempotency_registry": set()},
    )


def _read_audit(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]


# ---------------------------------------------------------------------------
# 1. 五段闭环 e2e（3 场景 × 自动审批）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_five_stage_pipeline(orchestrator, scenario):
    report = orchestrator.run(scenario)
    assert not report.degraded, f"五段闭环不应降级: {report.degradation_notes}"

    # 时间线覆盖 5 个 Agent 的开始/结束
    events = " ".join(item["event"] for item in report.timeline)
    for agent in ALL_AGENTS:
        assert f"{agent} 开始" in events and f"{agent} 结束" in events

    # 执行段落：审批通过 + 实际执行成功
    assert report.execution_result is not None
    execution = report.execution_result.model_dump()
    assert execution["executed"] is True
    assert execution["status"] in ("success", "success_with_rollback")
    approval = execution["approval"]
    assert approval["approved"] is True
    assert approval["approver"] and approval["decided_at"]
    assert execution["risk_assessment"]["overall_risk"] in ("low", "medium", "high")
    # 每个已执行动作都有幂等键与检查点
    for action in execution["actions"]:
        if action["status"] == "success":
            assert action["idempotency_key"] and action["checkpoint_id"]

    # 验证段落：告警消除且指标回归基线
    assert report.verification_result is not None
    verification = report.verification_result.model_dump()
    assert verification["passed"] is True and verification["alerts_cleared"] is True
    assert verification["checks"], "应至少验证一项指标"
    assert all(chk["recovered"] for chk in verification["checks"])

    # 复盘段落：时间线/根因/处置/改进建议齐全，且案例已沉淀
    assert report.postmortem is not None
    postmortem = report.postmortem.model_dump()
    assert postmortem["timeline"] and postmortem["root_cause"]
    assert postmortem["actions_taken"] and postmortem["improvements"]
    assert postmortem["case_id"].startswith("CASE-"), "复盘后应沉淀新案例"

    # Trace 覆盖 5 个 agent span 与新增 skill span
    trace = json.loads(orchestrator.last_artifacts["trace"].read_text(encoding="utf-8"))
    names = {s["name"] for s in trace["spans"]}
    for expected in ("agent.ExecutorAgent", "agent.VerifierAgent",
                     "skill.risk_guard", "skill.safe_execute",
                     "skill.recovery_verify", "skill.postmortem",
                     "skill.case_retrieval", "skill.runbook_rag"):
        assert expected in names, f"缺少关键 span: {expected}"

    # RAG 增强：RCA 相似案例 + 方案 Runbook 依据
    assert report.similar_cases, "RCA 应检索到相似历史案例"
    assert report.remediation_plan.runbook_references, "方案应引用 Runbook 依据"


def test_audit_and_metrics_artifacts(orchestrator):
    report = orchestrator.run("db_pool_exhaustion")

    # 审计事件流：关键事件类型齐全且均带 trace_id
    events = _read_audit(orchestrator.last_artifacts["audit"])
    types = {e["event_type"] for e in events}
    assert {"whitelist_check", "approval", "checkpoint", "execute"} <= types
    assert all(e["trace_id"] == report.trace_id for e in events)

    # 指标产物：与 trace_id 关联，覆盖 llm/skill/agent/rag/pipeline 维度
    metrics = json.loads(orchestrator.last_artifacts["metrics"].read_text(encoding="utf-8"))
    assert metrics["trace_id"] == report.trace_id
    assert metrics["llm"]["call_count"] > 0
    assert metrics["llm"]["estimated_prompt_tokens"] > 0
    safe_execute = metrics["skills"]["safe_execute"]
    assert safe_execute["call_count"] >= 1 and safe_execute["success_rate"] == 1.0
    assert set(metrics["agents"]) == set(ALL_AGENTS)
    assert metrics["rag"]["query_count"] > 0
    assert metrics["pipeline_duration_ms"] > 0


# ---------------------------------------------------------------------------
# 2. 白名单拒绝
# ---------------------------------------------------------------------------

def test_whitelist_rejection(tmp_path):
    ctx = _make_skill_context(tmp_path)
    plan = {
        "plan_id": "PLAN-TEST-WL",
        "steps": [
            {"order": 1, "action": "直接删库跑路", "action_type": "drop_database",
             "command": "DROP DATABASE orders", "expected_effect": "-"},
            {"order": 2, "action": "调大连接池上限", "action_type": "scale_pool",
             "command": "set maxPoolSize=60", "expected_effect": "缓解"},
        ],
    }
    result = SafeExecuteSkill().execute({"incident_id": "INC-TEST-WL", "plan": plan}, ctx)
    assert result.success
    actions = {a["order"]: a for a in result.output["actions"]}
    assert actions[1]["status"] == "rejected_whitelist"
    assert actions[2]["status"] == "success", "白名单内动作不受影响"

    # 审计事件：拒绝的 whitelist_check 记录 allowed=False
    ctx.audit.close()
    checks = [e for e in _read_audit(ctx.audit.path) if e["event_type"] == "whitelist_check"]
    assert any(e["allowed"] is False and e["action_type"] == "drop_database" for e in checks)


def test_whitelist_reject_all_actions(tmp_path):
    """全部动作都不在白名单时，整体状态为 rejected 且未执行任何动作。"""
    ctx = _make_skill_context(tmp_path)
    plan = {"plan_id": "PLAN-TEST-WL2", "steps": [
        {"order": 1, "action": "危险操作", "action_type": "rm_rf", "expected_effect": "-"},
    ]}
    result = SafeExecuteSkill().execute({"incident_id": "INC-TEST-WL2", "plan": plan}, ctx)
    assert result.output["status"] == "rejected"
    assert result.output["executed"] is False


# ---------------------------------------------------------------------------
# 3. 幂等重复执行
# ---------------------------------------------------------------------------

def test_idempotent_duplicate_execution(tmp_path):
    ctx = _make_skill_context(tmp_path)
    plan = {"plan_id": "PLAN-TEST-IDEM", "steps": [
        {"order": 1, "action": "调大连接池上限", "action_type": "scale_pool",
         "command": "set maxPoolSize=60", "expected_effect": "缓解"},
    ]}
    skill = SafeExecuteSkill()
    payload = {"incident_id": "INC-TEST-IDEM", "plan": plan}

    first = skill.execute(payload, ctx)
    assert first.output["actions"][0]["status"] == "success"

    # 同一事件内重复执行同一动作：幂等键命中，直接跳过
    second = skill.execute(payload, ctx)
    assert second.output["actions"][0]["status"] == "skipped_idempotent"
    assert second.output["executed"] is False
    assert second.output["actions"][0]["idempotency_key"] == \
        first.output["actions"][0]["idempotency_key"]

    ctx.audit.close()
    skipped = [e for e in _read_audit(ctx.audit.path)
               if e["event_type"] == "execute" and e.get("status") == "skipped_idempotent"]
    assert len(skipped) == 1


# ---------------------------------------------------------------------------
# 4. 执行失败自动回滚（network_latency 剧本）
# ---------------------------------------------------------------------------

def test_network_latency_failure_triggers_rollback(orchestrator):
    report = orchestrator.run("network_latency")
    assert report.execution_result is not None
    execution = report.execution_result.model_dump()

    # 剧本：首动作（回滚 ACL 变更）注定失败 -> 自动回滚 -> 备选动作（切流）成功
    assert execution["rollback_performed"] is True
    assert execution["status"] == "success_with_rollback"
    assert execution["rollbacks"], "应有回滚记录"

    by_order = {a["order"]: a for a in execution["actions"]}
    assert by_order[1]["status"] == "failed", "首动作应按剧本失败"
    fallbacks = [a for a in execution["actions"] if a["fallback"]]
    assert fallbacks, "失败后续行的步骤应标记为备选动作"
    assert any(a["status"] in ("success", "manual") for a in fallbacks), "备选动作应成功"

    # 审计日志可见完整事件链：白名单 -> 审批 -> 检查点 -> 执行失败 -> 回滚 -> 备选执行
    events = _read_audit(orchestrator.last_artifacts["audit"])
    types = [e["event_type"] for e in events]
    assert {"whitelist_check", "approval", "checkpoint", "execute", "rollback"} <= set(types)
    fail_idx = next(i for i, e in enumerate(events)
                    if e["event_type"] == "execute" and e.get("status") == "failed")
    rollback_idx = next(i for i, e in enumerate(events) if e["event_type"] == "rollback")
    assert rollback_idx > fail_idx, "回滚事件应发生在执行失败之后"
    assert any(e["event_type"] == "execute" and e.get("status") == "success"
               for e in events[rollback_idx:]), "回滚后应有备选动作执行成功"

    # 复盘应体现回滚链路：效果描述含自动回滚，改进项含执行失败复盘
    assert report.postmortem is not None
    assert "回滚" in report.postmortem.effect
    improvements = " ".join(report.postmortem.improvements)
    assert "执行失败" in improvements


# ---------------------------------------------------------------------------
# 5. RAG 检索相关性
# ---------------------------------------------------------------------------

def test_rag_retrieval_relevance_connection_pool():
    """查询"连接池"应命中连接池相关 Runbook（RB-001 为最相关文档之一）。"""
    store = KnowledgeStore(_PROJECT_ROOT / "data" / "knowledge")
    retriever = LocalRetriever(store)
    hits = retriever.search("数据库连接池耗尽", doc_type="runbook", top_k=3)
    assert hits, "查询应有命中"
    ids = [h["doc"]["id"] for h in hits]
    assert "RB-001" in ids, f"应命中连接池 Runbook，实际: {ids}"
    assert all(h["score"] > 0 for h in hits)
    assert all(h["doc"]["type"] == "runbook" for h in hits), "doc_type 过滤应生效"


def test_rag_retrieval_case_filter():
    store = KnowledgeStore(_PROJECT_ROOT / "data" / "knowledge")
    retriever = LocalRetriever(store)
    hits = retriever.search("容器 OOMKilled 内存泄漏", doc_type="case", top_k=3)
    assert hits and all(h["doc"]["type"] == "case" for h in hits)
    assert any(h["doc"]["category"] == "container_oom" for h in hits)


# ---------------------------------------------------------------------------
# 6. 案例沉淀幂等
# ---------------------------------------------------------------------------

def test_case_persist_idempotent(tmp_path):
    store = KnowledgeStore(tmp_path / "kb")
    case = {"id": "CASE-TEST-0001", "incident_id": "INC-TEST-0001",
            "title": "测试案例", "category": "db_pool_exhaustion"}
    assert store.append_case(case) is True, "首次写入应成功"
    assert store.append_case(case) is False, "同 incident_id 重复写入应幂等跳过"
    assert len(store.all_docs("case")) == 1


def test_case_persisted_after_pipeline(orchestrator, knowledge_dir):
    """流水线跑完后新案例应写入知识库拷贝（种子 11 条 -> 12 条）。"""
    seed_count = len(KnowledgeStore(knowledge_dir).all_docs("case"))
    report = orchestrator.run("db_pool_exhaustion")
    store = KnowledgeStore(knowledge_dir)
    cases = store.all_docs("case")
    assert len(cases) == seed_count + 1
    persisted = cases[-1]
    assert persisted["incident_id"] == report.incident.incident_id
    assert persisted["id"] == report.postmortem.case_id
    # 仓库种子数据不应被写入
    repo_cases = KnowledgeStore(_PROJECT_ROOT / "data" / "knowledge").all_docs("case")
    assert all(c["incident_id"] != report.incident.incident_id for c in repo_cases)


# ---------------------------------------------------------------------------
# 7. 审批拒绝路径
# ---------------------------------------------------------------------------

def test_approval_rejection_blocks_execution(tmp_path, knowledge_dir):
    def deny(plan, risk, incident):
        return {"approved": False, "approver": "tester",
                "mode": "interactive", "reason": "测试拒绝"}

    orch = Orchestrator(output_dir=tmp_path / "output", console=False,
                        knowledge_dir=knowledge_dir, approval_handler=deny)
    report = orch.run("db_pool_exhaustion")
    execution = report.execution_result.model_dump()
    assert execution["status"] == "rejected"
    assert execution["executed"] is False
    assert execution["approval"]["approved"] is False
    assert execution["approval"]["approver"] == "tester"
    assert not execution["actions"], "审批被拒后不应执行任何动作"

    # 审批决定写入审计
    events = _read_audit(orch.last_artifacts["audit"])
    approvals = [e for e in events if e["event_type"] == "approval"]
    assert approvals and approvals[0]["approved"] is False
    assert approvals[0]["approver"] == "tester"
