#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpsPilot-Insight 一键演示入口。

用法：
    python3 run_demo.py --scenario db_pool_exhaustion --auto-approve  # 自动审批跑完五段闭环
    python3 run_demo.py --scenario network_latency                    # 交互式审批（提示 y/n）
    python3 run_demo.py --list-scenarios                              # 列出可用场景
    python3 run_demo.py -s container_oom --auto-approve --no-sediment  # 反复演示不污染知识库

协商模式（Agent 间反馈协商，可选增强，默认关闭）：
    # 机制 2 演示：container_oom 双根因候选（变更引入配置错误 vs 应用内存泄漏）
    # 置信度接近 -> 多方案并行生成 -> 打分决策（交互模式下人工从多方案中选择）
    python3 run_demo.py -s container_oom --negotiation --auto-approve
    # 机制 1 演示：调高置信度阈值触发证据补充反馈环（扩展时间窗日志 + 变更单详情）
    python3 run_demo.py -s transaction_risk_surge --negotiation --rca-threshold 0.9 --auto-approve

默认使用 MockProvider（无 API Key、无网络即可完整跑通）；
如需切换通义千问：export OPSPILOT_LLM_PROVIDER=dashscope && export DASHSCOPE_API_KEY=sk-xxx
如需切换向量检索：pip install -r requirements-optional.txt && export OPSPILOT_RAG_BACKEND=chroma
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent

# 将 src 加入模块搜索路径（免安装直接运行）
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from opspilot.models import IncidentReport  # noqa: E402
from opspilot.orchestrator import Orchestrator  # noqa: E402

_LINE = "═" * 72


def interactive_approval(plan: dict, risk: dict, incident: dict) -> dict:
    """交互式审批：控制台展示方案摘要，等待操作人 y/n 决定。"""
    print(f"\n{_LINE}")
    print("  ⚠ 人工审批交互点：以下修复方案需要审批后才可执行")
    print(_LINE)
    print(f"方案 {plan.get('plan_id')}  综合风险: {risk.get('overall_risk')}  "
          f"影响服务: {', '.join(risk.get('impact_radius', {}).get('services', []) or ['未知'])}")
    for step in plan.get("steps", []):
        print(f"  {step.get('order')}. [{step.get('action_type')}] {step.get('action')}")
    while True:
        answer = input("是否批准执行？[y/n] ").strip().lower()
        if answer in ("y", "yes", "n", "no"):
            approved = answer in ("y", "yes")
            return {
                "approved": approved,
                "approver": "demo-operator",
                "mode": "interactive",
                "reason": "操作人控制台确认" if approved else "操作人控制台拒绝",
            }
        print("请输入 y 或 n")


def interactive_plan_selection(options: list) -> dict:
    """多方案协商交互点：展示各候选方案打分明细，等待操作人选择其一。"""
    print(f"\n{_LINE}")
    print("  ⚖ 多方案协商交互点：多个根因候选置信度接近，请从以下方案中选择")
    print(_LINE)
    for option in options:
        plan = option["plan"]
        breakdown = option["score_breakdown"]
        print(f"[{option['rank']}] 方案 {plan.get('plan_id')}  score={option['score']}")
        print(f"    根因假设: {option['candidate'].get('hypothesis')}"
              f"（置信度 {breakdown['confidence']}）")
        print(f"    风险等级: {breakdown['risk_level']}  "
              f"预估恢复时长: {breakdown['estimated_recovery_minutes']} 分钟")
        for step in plan.get("steps", []):
            print(f"      {step.get('order')}. [{step.get('action_type')}] {step.get('action')}")
    while True:
        answer = input(f"请选择方案 [1-{len(options)}] ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return {
                "index": int(answer) - 1,
                "selector": "demo-operator",
                "mode": "interactive",
                "reason": "操作人从多候选方案中控制台选定",
            }
        print(f"请输入 1 到 {len(options)} 之间的数字")


def print_report(report: IncidentReport, orchestrator: Orchestrator) -> None:
    """在控制台打印结构化诊断报告。"""
    incident = report.incident
    print(f"\n{_LINE}")
    print(f"  OpsPilot-Insight 诊断报告  {report.report_id}")
    print(_LINE)
    print(f"场景        : {report.scenario}")
    print(f"事件        : {incident.title}（{incident.incident_id}，严重度 {incident.severity}）")
    print(f"Trace ID    : {report.trace_id}")
    if report.degraded:
        print(f"降级说明    : {'；'.join(report.degradation_notes)}")

    print(f"\n── 告警摘要 {'─' * 58}")
    print(f"原始告警 {incident.raw_alert_count} 条 -> 去重 {incident.deduped_alert_count} 条 -> "
          f"聚合 {len(incident.alert_groups)} 组")
    for group in incident.alert_groups:
        print(f"  · [{group.severity}] {group.alertname} @ {group.service} ×{group.count}  {group.summary}")
    if incident.summary:
        print(f"事件概述: {incident.summary}")

    print(f"\n── 影响面 {'─' * 60}")
    impact = incident.impact
    print(f"受影响服务: {', '.join(incident.affected_services) or '（未知）'}")
    if impact:
        print(f"爆炸半径  : {', '.join(impact.get('blast_radius', []))}（{impact.get('blast_radius_size', '?')} 个服务）")
        print(f"用户影响  : {impact.get('user_impact', '未知')}")

    print(f"\n── 根因分析 {'─' * 58}")
    if report.selected_root_cause:
        rc = report.selected_root_cause
        print(f"根因结论（置信度 {rc.confidence}）: {rc.hypothesis}")
        print("证据链:")
        for ev in rc.evidences:
            print(f"  [{ev.strength:^7}] {ev.source:<8} {ev.description}")
            for detail in ev.details:
                print(f"            · {detail}")
        # 注意：selected 与 candidates 中的对象经序列化后非同一实例，需按内容比较
        others = [c for c in report.root_cause_candidates if c.model_dump() != rc.model_dump()]
        for cand in others:
            print(f"备选假设（置信度 {cand.confidence}）: {cand.hypothesis}")
    else:
        print("未得出可信根因，建议人工排查")
    if report.similar_cases:
        print("相似历史案例（RAG 检索）:")
        for case in report.similar_cases:
            print(f"  · [{case.get('id')}] {case.get('title')}（score={case.get('score')}）")

    if report.negotiation:
        print(f"\n── 协商与反馈（--negotiation） {'─' * 40}")
        loop = report.negotiation.get("evidence_loop") or {}
        if loop.get("triggered"):
            requests = "；".join(
                f"第 {r['round']} 轮请求 {', '.join(r['missing_evidence'])}"
                for r in loop.get("requests", [])
            )
            outcome = "置信度回升，继续自动流程" if loop.get("resolved") else "仍低于阈值，转人工"
            print(f"证据补充反馈环: 已触发（{requests}）-> {outcome}")
        else:
            print("证据补充反馈环: 未触发（首轮置信度已达标）")
        plan_neg = report.negotiation.get("plan_negotiation") or {}
        if plan_neg.get("triggered"):
            decision = plan_neg.get("decision", {})
            print(f"多方案协商: {plan_neg.get('candidate_count')} 个候选置信度接近"
                  f"（差距 {plan_neg.get('confidence_gap')}），并行生成方案后决策")
            print(f"决策结果  : 选定 {decision.get('chosen_plan_id')}"
                  f"（score={decision.get('chosen_score')}，方式={decision.get('mode')}）")
        else:
            print("多方案协商: 未触发（候选置信度差距未达阈值）")
        for alt in report.alternative_plans:
            plan = alt.get("plan", {})
            print(f"备选方案（未采纳，rank={alt.get('rank')}，score={alt.get('score')}）: "
                  f"{plan.get('plan_id')}")
            print(f"  假设: {alt.get('root_cause_hypothesis')}（置信度 {alt.get('root_cause_confidence')}）")
            print(f"  原因: {alt.get('not_selected_reason')}")

    print(f"\n── 修复方案 {'─' * 58}")
    plan = report.remediation_plan
    if plan:
        print(f"方案 {plan.plan_id}  风险等级: {plan.risk_level}  需人工审批: {'是' if plan.approval_required else '否'}")
        if plan.runbook_references:
            refs = "、".join(f"{r.get('id')} {r.get('title')}" for r in plan.runbook_references)
            print(f"Runbook 依据: {refs}")
        if plan.narrative:
            print(f"方案说明: {plan.narrative}")
        print("修复步骤:")
        for step in plan.steps:
            print(f"  {step.order}. [{step.action_type}] {step.action}")
            if step.command:
                print(f"     $ {step.command}")
            print(f"     预期: {step.expected_effect}")
        print("回滚计划:")
        for step in plan.rollback_plan:
            print(f"  {step.order}. {step.action}")
            if step.command:
                print(f"     $ {step.command}")
    else:
        print("未生成自动修复方案，转人工处理")

    print(f"\n── 审批与执行 {'─' * 56}")
    execution = report.execution_result
    if execution:
        if execution.approval:
            ap = execution.approval
            print(f"审批记录: {'通过' if ap.approved else '拒绝'}  审批人={ap.approver}  "
                  f"方式={ap.mode}  时间={ap.decided_at}")
            if ap.reason:
                print(f"审批说明: {ap.reason}")
        print(f"执行状态: {execution.status}（实际执行={'是' if execution.executed else '否'}）")
        status_icons = {"success": "✓", "failed": "✗", "skipped_idempotent": "↷",
                        "rejected_whitelist": "⛔", "manual": "✎"}
        for action in execution.actions:
            icon = status_icons.get(action.status, "·")
            fallback = "（备选动作）" if action.fallback else ""
            print(f"  {icon} 动作 {action.order} [{action.action_type}] {action.action}{fallback}")
            print(f"    状态={action.status}  幂等键={action.idempotency_key or '-'}"
                  + (f"  检查点={action.checkpoint_id}" if action.checkpoint_id else ""))
            if action.message:
                print(f"    {action.message}")
        if execution.rollback_performed:
            print("自动回滚记录（按检查点逆序）:")
            for rb in execution.rollbacks:
                print(f"  ⟲ 动作 {rb.action_order} 检查点 {rb.checkpoint_id}: {rb.message}")
        for note in execution.notes:
            print(f"备注: {note}")
    else:
        print("未执行（降级或无方案）")

    print(f"\n── 恢复验证 {'─' * 58}")
    verification = report.verification_result
    if verification:
        print(f"验证结论: {verification.summary}")
        for check in verification.checks:
            icon = "✓" if check.recovered else "✗"
            print(f"  {icon} {check.metric} @ {check.service}: 基线 {check.baseline}{check.unit} "
                  f"-> 故障 {check.before_fix}{check.unit} -> 修复后 {check.after_fix}{check.unit}")
    else:
        print("未完成恢复验证")

    print(f"\n── 事故复盘 {'─' * 58}")
    pm = report.postmortem
    if pm:
        if pm.narrative:
            print(f"复盘总结: {pm.narrative}")
        print(f"根因: {pm.root_cause}")
        print("处置动作:")
        for action in pm.actions_taken:
            print(f"  · {action}")
        print(f"效果: {pm.effect}")
        print("改进建议:")
        for item in pm.improvements:
            print(f"  · {item}")
        print(f"知识库沉淀: {'已写入案例 ' + pm.case_id if pm.case_id else '未写入（已存在或不可用）'}")
    else:
        print("未生成复盘报告")

    print(f"\n── 可观测产物 {'─' * 56}")
    for kind, path in orchestrator.last_artifacts.items():
        print(f"  {kind:<7}: {path}")
    print(_LINE)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_demo",
        description="OpsPilot-Insight：多 Agent 自愈系统五段闭环演示（告警->根因->方案->执行->验证复盘）",
    )
    parser.add_argument("--scenario", "-s", help="要运行的场景名（见 --list-scenarios）")
    parser.add_argument("--list-scenarios", action="store_true", help="列出全部可用场景")
    parser.add_argument("--output-dir", "-o", default=None, help="产物输出目录（默认 ./output）")
    parser.add_argument("--auto-approve", action="store_true",
                        help="自动批准需审批的修复方案（默认交互式提示 y/n）")
    parser.add_argument("--negotiation", action="store_true",
                        help="开启 Agent 间反馈协商机制（低置信度证据补充反馈环 + 多方案协商决策）")
    parser.add_argument("--rca-threshold", type=float, default=None, metavar="FLOAT",
                        help="覆盖 RCA 置信度阈值（协商模式下低于该值触发证据补充请求，默认 0.6）")
    parser.add_argument("--no-sediment", action="store_true",
                        help="复盘案例不写回仓库知识库（使用临时拷贝，适合反复演示）")
    args = parser.parse_args()

    overrides = {}
    if args.rca_threshold is not None:
        overrides["rca_confidence_threshold"] = args.rca_threshold

    # --no-sediment：知识库改用临时拷贝，复盘案例只写入临时目录，退出时清理
    tmp_root = None
    knowledge_dir = None
    try:
        if args.no_sediment:
            tmp_root = Path(tempfile.mkdtemp(prefix="opspilot_demo_"))
            knowledge_dir = tmp_root / "knowledge"
            shutil.copytree(_PROJECT_ROOT / "data" / "knowledge", knowledge_dir)
            print("提示: --no-sediment 已启用，复盘案例写入知识库临时拷贝，"
                  "不修改 data/knowledge/ 种子数据")

        orchestrator = Orchestrator(
            output_dir=args.output_dir,
            approval_handler=None if args.auto_approve else interactive_approval,
            knowledge_dir=knowledge_dir,
            negotiation=args.negotiation or None,
            negotiation_overrides=overrides,
            plan_selector=None if args.auto_approve else interactive_plan_selection,
        )

        if args.list_scenarios:
            print("可用场景：")
            for item in orchestrator.list_scenarios():
                print(f"  - {item['name']:<24} {item['description']}")
            return 0

        if not args.scenario:
            parser.print_help()
            return 2

        try:
            report = orchestrator.run(args.scenario)
        except ValueError as exc:  # 未知场景等入参错误
            print(f"错误: {exc}", file=sys.stderr)
            return 2

        print_report(report, orchestrator)
        return 0
    finally:
        if tmp_root is not None:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
