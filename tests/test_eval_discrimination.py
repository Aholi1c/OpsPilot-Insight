# -*- coding: utf-8 -*-
"""评测区分度测试：好 case（Golden）应得高分，坏 case（故意错误方案）应得低分。

覆盖点：
1. 好 case（Golden 样本）总分均 ≥ 90；
2. 坏 case（6 条故意错误样本）总分均 < 50；
3. 好坏两组均分差距 ≥ 40 分，证明评测规则有区分度而非"太松"；
4. 每类错误类型（根因/动作/验证/闭环/安全/综合）在其对应维度得分显著低；
5. 坏 case 数据结构与 Golden Dataset 完全一致；
6. 单维度隔离扰动：只篡改某一维度的输入字段时，仅该维度掉分，
   其余四维分值与未扰动时完全一致，证明五个维度各自独立评分、互不串扰。

全程离线（MockJudge），复用 scripts/eval_discrimination_test.py 的评测逻辑，
不修改 evaluator.py 评分规则。
"""
from __future__ import annotations

from pathlib import Path

import pytest

# src/ 与 scripts/ 路径由 tests/conftest.py 统一注入
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

from eval_discrimination_test import (  # noqa: E402
    BAD_CASES_PATH, BAD_DIMENSION_MAX, BAD_TYPE_TO_RULE, DIMENSION_MUTATIONS,
    GOLDEN_PATH, ISOLATED_TARGET_MAX, bad_case_error_type, build_isolated_sample,
    run_discrimination,
)

from opspilot.evaluation.dataset_builder import load_golden_dataset  # noqa: E402
from opspilot.evaluation.evaluator import RULE_WEIGHTS, evaluate_sample  # noqa: E402


@pytest.fixture(scope="module")
def discrimination():
    """模块级共享：好/坏两组数据集各评测一次。"""
    return run_discrimination(console=False)


# ---------------------------------------------------------------------------
# 1. 好 case 得分 ≥ 90
# ---------------------------------------------------------------------------

def test_good_cases_score_above_90(discrimination):
    good = discrimination["good_report"]
    assert good["sample_count"] >= 3
    assert discrimination["good_avg"] >= 90, "好 case 均分低于 90"
    for result in good["results"]:
        assert result["total_score"] >= 90, \
            f"好 case {result['case_id']} 总分 {result['total_score']} 低于 90"


# ---------------------------------------------------------------------------
# 2. 坏 case 得分 < 50
# ---------------------------------------------------------------------------

def test_bad_cases_score_below_50(discrimination):
    bad = discrimination["bad_report"]
    assert bad["sample_count"] >= 6, "坏 case 应至少 6 条"
    assert discrimination["bad_avg"] <= 50, "坏 case 均分高于 50"
    for result in bad["results"]:
        assert result["total_score"] < 50, \
            f"坏 case {result['case_id']} 总分 {result['total_score']} 未低于 50"
        assert result["failed_rules"], f"坏 case {result['case_id']} 未命中任何失败规则"


# ---------------------------------------------------------------------------
# 3. 好坏差距 ≥ 40 分
# ---------------------------------------------------------------------------

def test_discrimination_gap(discrimination):
    gap = discrimination["gap"]
    assert gap >= 40, f"好坏均分差距 {gap} 不足 40 分，评测区分度不够"
    assert discrimination["all_passed"] is True


# ---------------------------------------------------------------------------
# 4. 每种错误类型在对应维度得分显著低
# ---------------------------------------------------------------------------

def test_each_bad_dimension_detectable(discrimination):
    checks = discrimination["dimension_checks"]
    # 6 类错误类型全部覆盖
    covered = {c["error_type"] for c in checks}
    assert covered == set(BAD_TYPE_TO_RULE), f"错误类型覆盖不全: {covered}"
    for check in checks:
        assert check["detectable"], \
            (f"坏 case {check['case_id']} 的主维度 {check['dimension']} "
             f"得分 {check['score']} 未显著低于判定线 {BAD_DIMENSION_MAX}")
    # 单一主错误类型的维度得分应显著低于好 case 该维度的满分表现
    good_results = discrimination["good_report"]["results"]
    for check in checks:
        if check["dimension"] == "multi":
            continue
        good_dim_scores = [r["rules"][check["dimension"]]["score"] for r in good_results]
        assert min(good_dim_scores) - check["score"] >= 40, \
            f"维度 {check['dimension']} 好坏得分差距不足 40"


# ---------------------------------------------------------------------------
# 5. 坏 case 数据结构与 Golden Dataset 完全一致
# ---------------------------------------------------------------------------

def test_bad_cases_schema_matches_golden():
    good_samples = load_golden_dataset(GOLDEN_PATH)
    bad_samples = load_golden_dataset(BAD_CASES_PATH)
    assert good_samples and bad_samples
    top_keys = set(good_samples[0].keys())
    for sample in bad_samples:
        assert set(sample.keys()) == top_keys, \
            f"坏 case {sample.get('case_id')} 顶层字段与 Golden 不一致"
        assert set(sample["expected"].keys()) == set(good_samples[0]["expected"].keys())
        # expected 保持正确答案（与同场景 Golden 一致），错误只体现在 actual
        golden = next(g for g in good_samples if g["scenario"] == sample["scenario"])
        assert sample["expected"]["root_cause_keywords"] == \
            golden["expected"]["root_cause_keywords"]
        assert bad_case_error_type(sample["case_id"]) in BAD_TYPE_TO_RULE
        assert set(RULE_WEIGHTS) <= {"root_cause", "action_type", "verification",
                                     "loop_completeness", "safety_compliance"}


# ---------------------------------------------------------------------------
# 6. 单维度隔离扰动：仅被扰动维度掉分，其余四维分值不变
# ---------------------------------------------------------------------------

def test_isolation_covers_every_rule_dimension(discrimination):
    """五个规则维度均有对应的单维度扰动用例。"""
    checks = discrimination["isolation_checks"]
    assert checks, "缺少单维度隔离扰动结果"
    assert set(DIMENSION_MUTATIONS) == set(RULE_WEIGHTS), "扰动未覆盖全部五个维度"
    assert {c["dimension"] for c in checks} == set(RULE_WEIGHTS)


def test_isolated_mutation_only_affects_target_dimension(discrimination):
    """扰动后：目标维度明显掉分，其余四维得分与扰动前完全一致。"""
    for check in discrimination["isolation_checks"]:
        assert check["mutated_score"] <= ISOLATED_TARGET_MAX, (
            f"{check['case_id']} 被扰动维度 {check['dimension']} 得分 "
            f"{check['mutated_score']} 未降至 {ISOLATED_TARGET_MAX} 以下")
        assert check["mutated_score"] < check["baseline_score"], \
            f"{check['case_id']} 扰动后得分未下降"
        assert not check["affected_others"], (
            f"{check['case_id']} 扰动 {check['dimension']} 时连带影响了其余维度: "
            f"{check['affected_others']}")
        assert check["isolated"] is True


def test_isolated_sample_keeps_expected_and_other_fields(discrimination):
    """扰动样本仅改写目标维度的输入字段，expected 与 Golden 保持一致。"""
    goldens = load_golden_dataset(GOLDEN_PATH)
    assert goldens
    golden = goldens[0]
    for rule_key in DIMENSION_MUTATIONS:
        mutated = build_isolated_sample(golden, rule_key)
        # expected（标准答案）不得被篡改
        assert mutated["expected"] == golden["expected"]
        assert mutated["scenario"] == golden["scenario"]
        assert mutated["case_id"].startswith(f"ISO-{rule_key}-")
        # 原 Golden 样本不受副作用影响（深拷贝）
        assert evaluate_sample(golden)["rules"][rule_key]["score"] == \
            pytest.approx(100.0)
