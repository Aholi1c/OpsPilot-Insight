# -*- coding: utf-8 -*-
"""Golden Dataset 构建 CLI：扫描 output/ 增量构建，同 case 幂等更新。

用法（项目根目录）：
    PYTHONPATH=src python -m opspilot.evaluation.build_dataset
    PYTHONPATH=src python -m opspilot.evaluation.build_dataset --output-dir output
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .dataset_builder import DEFAULT_GOLDEN_PATH, _PROJECT_ROOT, build_golden_dataset


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="opspilot.evaluation.build_dataset",
        description="从 output/ 运行产物自动构建 Golden Dataset（data/golden/golden_dataset.jsonl）",
    )
    parser.add_argument("--output-dir", default=str(_PROJECT_ROOT / "output"),
                        help="运行产物目录（默认 ./output）")
    parser.add_argument("--golden-path", default=str(DEFAULT_GOLDEN_PATH),
                        help="Golden Dataset 落盘路径（默认 data/golden/golden_dataset.jsonl）")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        print(f"错误: 产物目录不存在: {output_dir}（请先运行 run_demo.py 生成产物）")
        return 2
    samples = build_golden_dataset(output_dir, golden_path=args.golden_path)
    if not samples:
        print(f"未在 {output_dir} 发现可用的 incident_report_*.json 产物")
        return 1
    print(f"Golden Dataset 构建完成: {args.golden_path}（共 {len(samples)} 条样本）")
    for sample in samples:
        expected = sample["expected"]
        print(f"  - {sample['case_id']:<28} expected 来源={expected.get('source'):<8} "
              f"报告={sample['source_report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
