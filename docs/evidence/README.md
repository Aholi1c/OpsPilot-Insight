# 执行证据（Evidence）

> 本目录是一套**完整、干净**的运行产物快照，在恢复种子知识库、清空 `output/`
> 后按 README「快速开始」命令一次性生成，供评审在不运行代码的情况下核验产物
> 结构与闭环完整性。重新执行相同命令即可在 `output/` 复现同构产物。

## 生成方式

```bash
# 分别输出到独立目录，保证每套产物边界清晰（-o 指定输出目录）
python3 run_demo.py -s db_pool_exhaustion --auto-approve -o <dir>
python3 run_demo.py -s container_oom      --auto-approve -o <dir>
python3 run_demo.py -s network_latency    --auto-approve -o <dir>
python3 scripts/replay_eval.py            -o <dir>
```

## 目录结构与文件说明

```
evidence/
├── db_pool_exhaustion/      # 场景 1：连接池耗尽（变更引入连接泄漏）单次闭环产物
├── container_oom/           # 场景 2：容器 OOMKilled（缓存配置错误）单次闭环产物
├── network_latency/         # 场景 3：网络延迟劣化 —— 含"首动作失败→自动回滚→备选动作"剧本
└── replay_eval/             # 回放评测：3 场景回放产物 + 评测报告
```

每个场景目录内 6 个文件（`<ts>` 为运行时间戳、`<trace_id>` 为该次运行的全链路 ID）：

| 文件 | 内容 | 评审查验点 |
| --- | --- | --- |
| `incident_report_<场景>_<ts>.json` | 结构化闭环报告 | `selected_root_cause`（根因+证据链+置信度）、`remediation_plan`（步骤+风险+回滚计划）、`execution_result`（审批记录+逐动作状态）、`verification_result.passed=true`、`postmortem.case_id`（案例沉淀）、`degraded=false` |
| `trace_<trace_id>.json` | 全链路 Span 树（OTel 语义） | 根 span `pipeline.run` 下有 5 个 `agent.*` 子 span，其下有 `skill.*` / `llm.complete` / `execute.step_N`；父子 `span_id`/`parent_span_id` 正确关联；全部同一 `trace_id` |
| `run_<ts>.log` | JSONL 结构化日志 | 每行 JSON 携带 `trace_id`/`span_id`，与 trace 文件可关联 |
| `audit_<ts>.jsonl` | 审计事件流 | 事件链 `whitelist_check → approval → checkpoint → execute`；每条带 trace_id |
| `metrics_<trace_id>.json` | 进程内指标 | LLM 调用/token、Skill 成功率、Agent 耗时，`cost` 段三维分解（per_agent/per_skill/per_model 三个维度求和相等）与预算状态 |
| `console.log` | 控制台完整输出 | 五段闭环人类可读报告（与 JSON 报告内容一致） |

`replay_eval/` 额外包含：

| 文件 | 内容 | 评审查验点 |
| --- | --- | --- |
| `eval_report_<ts>.json` | 评测报告（机器可读） | 3 场景总分均 100.0，`all_passed_85: true`；五项规则得分（根因/动作/验证/闭环/安全）、Judge 三维评分与评语、各场景成本 |
| `eval_report_<ts>.md` | 评测报告（人类可读） | 同上的 Markdown 汇总表 |
| `console.log` | 回放评测控制台输出 | 「回放评测汇总」表：总平均分 100.0，全部样本 ≥ 85 ✓ 通过 |
| 其余 report/trace/metrics/audit/log | 回放的 3 次闭环产物 | 结构同上（注：3 次回放在同秒完成时共用同一 audit/log 文件名，事件按 trace_id 区分） |

## 重点查验：network_latency 的安全边界剧本

`network_latency/` 是安全执行四件套的完整演示，建议按以下顺序查验：

1. `audit_*.jsonl`：可见完整事件链
   `whitelist_check → approval → checkpoint → execute(failed) → rollback → checkpoint → execute(success)`；
2. `incident_report_*.json` 的 `execution_result`：
   `status = "success_with_rollback"`，动作 1（rollback_change）失败、
   自动回滚后备选动作 2（traffic_switch，`fallback: true`）成功、
   动作 3（manual_followup）创建跟进工单；
3. `trace_*.json`：`skill.safe_execute` 下可见 `execute.step_1`（status=ERROR）
   与 `execute.rollback`、`execute.step_2` 的 span 序列。

## 一致性说明

- 生成前已将 `data/knowledge/cases.jsonl` 恢复为 11 条种子案例、清空 `output/`，
  证据不依赖任何历史运行状态；
- 全程 MockProvider（确定性输出）+ 自动审批，无 API Key、无网络；
- `output/` 目录在证据固化后已再次清空（运行时目录保持干净），
  按 README 命令重跑即可得到同构产物（时间戳与随机 ID 不同，结构与结论一致）。
