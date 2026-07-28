# -*- coding: utf-8 -*-
"""阶段 3 测试：Golden Dataset / 规则评估 / 成本三维分解 / 预算告警 / 回放评测。

覆盖点：
1. 回放脚本：3 场景回放 + Golden 构建 + 评测报告一键完成，各场景总分 ≥ 85；
2. Golden Dataset 构建幂等：重复构建样本数与 case_id 不变，expected 为 curated；
3. 规则评估打分正确性：构造故意错误的 actual 应在对应规则项扣分；
4. 成本三维分解求和一致性：per-Agent / per-Skill / per-Model 求和均等于总成本；
5. 预算超限告警：极小预算下 metrics 标记 exceeded 且审计记录 budget_alert（不中断流程）；
6. token 估算 / 成本趋势聚合 / 评测报告对比等辅助能力。

全程离线（MockProvider + 自动审批 + 知识库临时拷贝）。
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from replay_eval import SCENARIOS, run_replay  # noqa: E402

from opspilot.evaluation.cost import (  # noqa: E402
    aggregate_cost_trend, compute_cost_section, load_pricing,
)
from opspilot.evaluation.dataset_builder import (  # noqa: E402
    build_golden_dataset, load_golden_dataset,
)
from opspilot.evaluation.evaluator import (  # noqa: E402
    evaluate_dataset, evaluate_sample, write_eval_report,
)
from opspilot.evaluation.judges import MockJudge  # noqa: E402
from opspilot.observability.metrics import estimate_tokens  # noqa: E402
from opspilot.orchestrator import Orchestrator  # noqa: E402


@pytest.fixture(scope="module")
def replay_env(tmp_path_factory):
    """模块级共享：完整跑一次回放评测（3 场景 + Golden 构建 + 评测报告）。"""
    root = tmp_path_factory.mktemp("stage3")
    output_dir = root / "output"
    golden_path = root / "golden" / "golden_dataset.jsonl"
    report = run_replay(output_dir=output_dir, golden_path=golden_path, console=False)
    return {"output_dir": output_dir, "golden_path": golden_path, "report": report}


# ---------------------------------------------------------------------------
# 1. 回放评测一键完成
# ---------------------------------------------------------------------------

def test_replay_three_scenarios_all_pass(replay_env):
    report = replay_env["report"]
    assert report["sample_count"] == 3
    assert report["all_passed_85"] is True
    scenarios = {r["scenario"] for r in report["results"]}
    assert scenarios == set(SCENARIOS)
    for result in report["results"]:
        assert result["total_score"] >= 85, f"{result['scenario']} 总分低于验收线"
        # Judge 三维度均有评分与评语
        judge_scores = result["judge"]["scores"]
        assert len(judge_scores) == 3
        assert all(1 <= s["score"] <= 5 and s["comment"] for s in judge_scores)


def test_eval_report_files_written(replay_env):
    output_dir = replay_env["output_dir"]
    json_reports = list(output_dir.glob("eval_report_*.json"))
    md_reports = list(output_dir.glob("eval_report_*.md"))
    assert json_reports and md_reports
    data = json.loads(json_reports[0].read_text(encoding="utf-8"))
    assert "overall_score" in data and "comparison" in data
    md_text = md_reports[0].read_text(encoding="utf-8")
    assert "评测报告" in md_text and "各场景得分" in md_text


def test_eval_report_comparison_with_previous(replay_env):
    """第二次落盘报告应能与上一份对比。"""
    samples = load_golden_dataset(replay_env["golden_path"])
    report = evaluate_dataset(samples, judge=MockJudge())
    paths = write_eval_report(report, replay_env["output_dir"])
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    comparison = data["comparison"]
    assert comparison["available"] is True
    assert len(comparison["per_case"]) == 3


# ---------------------------------------------------------------------------
# 2. Golden Dataset 构建幂等
# ---------------------------------------------------------------------------

def test_golden_dataset_idempotent_rebuild(replay_env):
    golden_path = replay_env["golden_path"]
    first = load_golden_dataset(golden_path)
    assert len(first) == 3
    assert all(s["expected"]["source"] == "curated" for s in first), \
        "3 个内置场景均应使用人工校准的 expected.json"

    # 重复构建：样本数与 case_id 集合不变（同 case 幂等覆盖更新）
    rebuilt = build_golden_dataset(replay_env["output_dir"], golden_path=golden_path)
    assert len(rebuilt) == len(first)
    assert {s["case_id"] for s in rebuilt} == {s["case_id"] for s in first}
    assert {s["case_id"] for s in rebuilt} == {f"GOLDEN-{s}" for s in SCENARIOS}


def test_golden_sample_fields_complete(replay_env):
    for sample in load_golden_dataset(replay_env["golden_path"]):
        assert sample["input"]["title"] and sample["input"]["alert_groups"]
        assert sample["expected"]["root_cause_keywords"]
        assert sample["actual"]["root_cause_hypothesis"]
        execution = sample["execution"]
        assert execution["span_count"] > 0
        assert execution["llm_call_count"] > 0
        assert execution["estimated_tokens"] > 0
        assert execution["estimated_cost"] > 0


# ---------------------------------------------------------------------------
# 3. 规则评估打分正确性
# ---------------------------------------------------------------------------

def test_rule_scoring_correct_sample_full_marks(replay_env):
    for sample in load_golden_dataset(replay_env["golden_path"]):
        result = evaluate_sample(sample)
        assert result["total_score"] == 100.0, \
            f"{sample['scenario']} Mock 流程即正确答案，应满分：{result['failed_rules']}"


def test_rule_scoring_penalizes_wrong_actual(replay_env):
    sample = copy.deepcopy(load_golden_dataset(replay_env["golden_path"])[0])
    baseline = evaluate_sample(sample)["total_score"]

    # 故意构造错误 actual：根因跑偏 / 变更单错 / 动作类型错 / 验证失败 / 闭环缺段
    sample["actual"]["root_cause_hypothesis"] = "磁盘空间不足导致写入失败"
    sample["actual"]["related_change_id"] = "CHG-99999999-9999"
    sample["actual"]["action_types"] = ["service_restart"]
    sample["actual"]["planned_action_types"] = ["service_restart"]
    sample["actual"]["verification_passed"] = False
    sample["actual"]["alerts_cleared"] = False
    sample["actual"]["stages"]["verification"] = False

    result = evaluate_sample(sample)
    assert result["total_score"] < baseline
    for rule_key in ("root_cause", "action_type", "verification", "loop_completeness"):
        assert rule_key in result["failed_rules"], f"{rule_key} 应被判为失败项"
    assert result["rules"]["root_cause"]["score"] == 0.0
    assert result["rules"]["verification"]["score"] == 0.0
    assert result["rules"]["loop_completeness"]["score"] == 80.0  # 缺 1 段


def test_rule_scoring_safety_compliance_violation(replay_env):
    sample = copy.deepcopy(load_golden_dataset(replay_env["golden_path"])[0])
    # 中风险方案无审批记录 + 有失败动作但未回滚 → 安全合规 0 分
    sample["actual"]["risk_level"] = "high"
    sample["actual"]["approval"] = None
    sample["actual"]["failed_action_count"] = 1
    sample["actual"]["rollback_performed"] = False
    sample["actual"]["rollback_count"] = 0
    result = evaluate_sample(sample)
    assert result["rules"]["safety_compliance"]["score"] == 0.0
    assert "safety_compliance" in result["failed_rules"]


# ---------------------------------------------------------------------------
# 4. 成本三维分解求和一致性
# ---------------------------------------------------------------------------

def _latest_metrics(output_dir: Path) -> dict:
    paths = sorted(output_dir.glob("metrics_*.json"), key=lambda p: p.stat().st_mtime)
    assert paths, "回放后应有 metrics 产物"
    return json.loads(paths[-1].read_text(encoding="utf-8"))


def test_cost_three_dimensions_sum_consistent(replay_env):
    metrics = _latest_metrics(replay_env["output_dir"])
    cost = metrics["cost"]
    total = cost["total_cost"]
    assert total > 0
    for dim in ("per_agent", "per_skill", "per_model"):
        dim_sum = sum(cost[dim].values())
        assert abs(dim_sum - total) < 1e-4, f"{dim} 求和 {dim_sum} != 总成本 {total}"
    assert cost["llm_call_count"] == metrics["llm"]["call_count"]
    # 逐次调用事件带齐归因字段
    for call in metrics["llm"]["calls"]:
        assert call["agent"] and call["model"]
        assert call["prompt_tokens"] > 0 and call["completion_tokens"] > 0


def test_cost_per_model_uses_pricing(replay_env):
    """Mock 模型单价来自 config/pricing.yaml，可手工复算总成本。"""
    pricing = load_pricing(_PROJECT_ROOT / "config" / "pricing.yaml")
    metrics = _latest_metrics(replay_env["output_dir"])
    price = pricing["models"]["mock"]
    expected_total = sum(
        c["prompt_tokens"] / 1000.0 * price["input_per_1k"]
        + c["completion_tokens"] / 1000.0 * price["output_per_1k"]
        for c in metrics["llm"]["calls"]
    )
    assert abs(expected_total - metrics["cost"]["total_cost"]) < 1e-4


def test_cost_trend_aggregation(replay_env):
    trend = aggregate_cost_trend(replay_env["output_dir"])
    assert len(trend) >= 3
    assert all(item["total_cost"] > 0 for item in trend)
    # 按时间升序
    stamps = [item["generated_at"] for item in trend]
    assert stamps == sorted(stamps)


def test_compute_cost_section_direct_bucket():
    """无 trace（无 Skill 耗时权重）时，Agent 直接推理成本落入 <Agent>:direct 桶。"""
    metrics = {"llm": {"calls": [
        {"agent": "RcaAgent", "model": "mock", "skill": "",
         "prompt_tokens": 1000, "completion_tokens": 500},
    ]}}
    pricing = {"currency": "CNY",
               "models": {"mock": {"input_per_1k": 0.01, "output_per_1k": 0.02}},
               "budget": {"per_incident": 1.0}}
    cost = compute_cost_section(metrics, None, pricing)
    assert cost["total_cost"] == pytest.approx(0.02)
    assert cost["per_skill"] == {"RcaAgent:direct": pytest.approx(0.02)}
    assert cost["budget"]["exceeded"] is False


# ---------------------------------------------------------------------------
# 5. 预算超限告警（不中断流程）
# ---------------------------------------------------------------------------

def test_budget_alert_triggered(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    shutil.copytree(_PROJECT_ROOT / "data" / "knowledge", knowledge_dir)
    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(
        "currency: CNY\n"
        "models:\n"
        "  mock:\n"
        "    input_per_1k: 0.008\n"
        "    output_per_1k: 0.02\n"
        "budget:\n"
        "  per_incident: 0.000001\n",  # 极小预算，必然超限
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    orchestrator = Orchestrator(output_dir=output_dir, console=False,
                                knowledge_dir=knowledge_dir)
    orchestrator.pricing_path = pricing_path
    report = orchestrator.run("db_pool_exhaustion")

    # 流程不中断：五段闭环照常完成
    assert report.verification_result is not None

    metrics = _latest_metrics(output_dir)
    budget = metrics["cost"]["budget"]
    assert budget["exceeded"] is True
    assert budget["usage_ratio"] > 1

    # 审计事件 budget_alert 已记录
    audit_paths = list(output_dir.glob("audit_*.jsonl"))
    assert audit_paths
    events = [json.loads(line)
              for line in audit_paths[0].read_text(encoding="utf-8").splitlines() if line]
    alerts = [e for e in events if e.get("event_type") == "budget_alert"]
    assert alerts, "超预算应记录 budget_alert 审计事件"
    assert alerts[0]["budget_limit"] == pytest.approx(0.000001)


# ---------------------------------------------------------------------------
# 6. token 估算
# ---------------------------------------------------------------------------

def test_estimate_tokens_mixed_text():
    assert estimate_tokens("") == 0
    # 3 个中文字符 / 1.5 = 2 token
    assert estimate_tokens("连接池") == 2
    # 8 个 ASCII 字符 / 4 = 2 token
    assert estimate_tokens("abcdefgh") == 2
    # 混合文本：向上取整且单调不减
    assert estimate_tokens("连接池 abcdefgh") >= 4
