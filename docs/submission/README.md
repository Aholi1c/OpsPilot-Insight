# 初赛提交材料目录说明

本目录为「新智基座丨AgentInfra——复杂任务多 Agent 自主协同」赛道初赛提交材料，全部文件仅涉及本目录，不影响项目代码。

## 文件清单

| 文件 | 用途 | 提交方式 |
| --- | --- | --- |
| `作品简介.md` | 初赛必交「作品简介」（正文 497 字，≤500 字要求内），直接复制正文提交至报名平台文本框 | 必交 |
| `方案PPT大纲.md` | 初赛必交「方案 PPT」的完整内容稿（14 页，逐页含标题/要点/配图建议），按此制作 PPT 后导出 PDF 提交 | 必交（转 PPT/PDF） |
| `评审对照检查表.md` | 按评审 5 大维度 + 个性化核验点的自查与答辩准备表，不对外提交，供团队内部使用 | 内部自查 |
| `答辩QA准备.md` | 评审高概率追问 15 题的问答演练材料（问题/追问点/标准答案/证据位置），不对外提交 | 内部自查 |
| `README.md` | 本说明文件 | 内部使用 |

## 制作 PPT 的注意事项

1. 严格按大纲页序制作，确保评审 5 个权重维度（25/25/25/20/5）均有专门页面覆盖；
2. 大纲中标注的 4 张看板截图位于仓库上层目录：`page1_home.png` / `page2_trace_browser.png` / `page3_evaluation_report.png` / `page4_cost_analysis.png`，建议重新运行看板截取最新图；
3. 所有数字（5 Agent / 9 Skill / 61 测试用例 / 10 类白名单动作 / 14 Runbook / 11 案例 / 4 场景 100 分 / 坏 case 区分度差距 65.5 分）提交前按 `评审对照检查表.md` 第七节复核一遍；
4. 行业 MTTR/人力成本数据在 PPT 中务必标注"行业典型数据"，避免被质疑为本项目实测。

## 代码包打包建议（可选提交物，强烈建议提交）

赛事红线为"无可验证材料原则上淘汰"，本项目可离线复现是核心优势，建议随 PPT 一并提交代码包：

```bash
cd /path/to/agent-infra
zip -r opspilot-insight.zip opspilot-insight \
  -x "opspilot-insight/.venv/*" \
  -x "opspilot-insight/output/*" \
  -x "opspilot-insight/.pytest_cache/*" \
  -x "*/__pycache__/*" \
  -x "*/.DS_Store"
```

排除说明：
- `.venv/`：虚拟环境体积大且与平台相关，评审侧按 `requirements.txt` 自行安装（主流程仅需 pydantic + pytest）；
- `output/`：运行产物由评审侧一键复现生成，无需打包（如希望附带示例产物，可单独保留 1 组 trace/audit/eval_report 文件）；
- `.pytest_cache/`、`__pycache__/`：缓存文件。

代码包内保留 `README.md`（项目根目录），其中"快速开始"三条命令即为评审验证入口：

```bash
python3 -m pip install -r requirements.txt
python3 run_demo.py --scenario network_latency --auto-approve   # 五段闭环 + 回滚剧本
python3 -m pytest tests/ -v                                     # 61 用例
python3 scripts/replay_eval.py                                  # 回放评测（4 场景）
```

## 提交操作建议

1. 平台文本框：粘贴 `作品简介.md` 正文（不含标题与引用行）；
2. 附件上传：PPT 导出的 PDF + `opspilot-insight.zip` 代码包；
3. 若平台仅允许单附件：将 PDF 放入代码包根目录一并压缩；
4. 提交前最后跑一遍 `评审对照检查表.md` 第七节的自查清单。
