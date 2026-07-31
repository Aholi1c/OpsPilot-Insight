# 执行证据（Evidence）

> 本目录是一套**完整、干净**的运行产物快照，在恢复种子知识库、清空 `output/`
> 后按 README「快速开始」命令一次性生成，供评审在不运行代码的情况下核验产物
> 结构与闭环完整性。重新执行相同命令即可在 `output/` 复现同构产物。

## 生成方式

```bash
# 4 场景独立输出
python3 run_demo.py -s db_pool_exhaustion      --auto-approve -o output/db_pool_exhaustion
python3 run_demo.py -s container_oom           --auto-approve -o output/container_oom
python3 run_demo.py -s network_latency         --auto-approve -o output/network_latency
python3 run_demo.py -s transaction_risk_surge  --auto-approve -o output/transaction_risk_surge

# 回放评测（4 场景）
python3 scripts/replay_eval.py

# 评测区分度验证（好坏 case 断言）
python3 scripts/eval_discrimination_test.py

# 协商模式演示 1：多方案协商决策
python3 run_demo.py -s container_oom --negotiation --auto-approve -o output/nego_multiplan

# 协商模式演示 2：低置信度证据补充反馈环
python3 run_demo.py -s transaction_risk_surge --negotiation --rca-threshold 0.9 --auto-approve -o output/nego_evidence_loop
```

## 目录结构与文件说明（共 48 个文件）

```
evidence/
├── README.md                          # 本文件
├── eval_discrimination_report.md      # 评测区分度报告（好 case vs 坏 case）
├── db_pool_exhaustion/                # 场景 1：连接池耗尽（变更引入连接泄漏）
│   ├── incident_report_*.json
│   ├── trace_*.json
│   ├── audit_*.jsonl
│   ├── metrics_*.json
│   └── run_*.log
├── container_oom/                     # 场景 2：容器 OOMKilled（缓存配置错误）
│   ├── incident_report_*.json
│   ├── trace_*.json
│   ├── audit_*.jsonl
│   ├── metrics_*.json
│   └── run_*.log
├── network_latency/                   # 场景 3：网络延迟劣化（含回滚剧本）
│   ├── incident_report_*.json
│   ├── trace_*.json
│   ├── audit_*.jsonl
│   ├── metrics_*.json
│   └── run_*.log
├── transaction_risk_surge/            # 场景 4：金融交易风险激增（撞库攻击）
│   ├── incident_report_*.json
│   ├── trace_*.json
│   ├── audit_*.jsonl
│   ├── metrics_*.json
│   └── run_*.log
├── replay_eval/                       # 回放评测：4 场景回放产物 + 评测报告
│   ├── eval_report_*.json             # 评测报告（机器可读）
│   ├── eval_report_*.md               # 评测报告（人类可读）
│   ├── run_*.log                      # 回放运行日志
│   ├── audit_*.jsonl                  # 回放审计事件
│   ├── incident_report_*_<4场景>.json # 4 场景回放报告
│   ├── trace_*.json (×4)             # 4 场景 trace
│   └── metrics_*.json (×4)           # 4 场景指标
└── negotiation/                       # Agent 协商机制证据
    ├── nego_multiplan/                # 多方案协商决策演示（container_oom）
    │   ├── incident_report_*.json     # 含 alternative_plans 字段
    │   ├── trace_*.json               # 含 plan.negotiation span
    │   ├── audit_*.jsonl              # 含 plan_negotiation/plan_selection 事件
    │   ├── metrics_*.json
    │   └── run_*.log
    └── nego_evidence_loop/            # 证据补充反馈环演示（transaction_risk_surge）
        ├── incident_report_*.json
        ├── trace_*.json               # 含 rca.evidence_request/rca.reanalysis span
        ├── audit_*.jsonl              # 含 evidence_request/evidence_supplement 事件
        ├── metrics_*.json
        └── run_*.log
```

## 每场景产物说明

每个场景目录内 5 个文件（`<ts>` 为运行时间戳、`<trace_id>` 为全链路 ID）：

| 文件 | 内容 | 评审查验点 |
| --- | --- | --- |
| `incident_report_<场景>_<ts>.json` | 结构化闭环报告 | `selected_root_cause`（根因+证据链+置信度）、`remediation_plan`（步骤+风险+回滚计划）、`execution_result`（审批记录+逐动作状态）、`verification_result.passed=true`、`postmortem.case_id`（案例沉淀）、`degraded=false` |
| `trace_<trace_id>.json` | 全链路 Span 树（OTel 语义） | 根 span `pipeline.run` 下有 5 个 `agent.*` 子 span，其下有 `skill.*` / `llm.complete` / `execute.step_N`；父子 `span_id`/`parent_span_id` 正确关联；全部同一 `trace_id` |
| `run_<ts>.log` | JSONL 结构化日志 | 每行 JSON 携带 `trace_id`/`span_id`，与 trace 文件可关联 |
| `audit_<ts>.jsonl` | 审计事件流 | 事件链 `whitelist_check → approval → checkpoint → execute`；每条带 trace_id |
| `metrics_<trace_id>.json` | 进程内指标 | LLM 调用/token、Skill 成功率、Agent 耗时，`cost` 段三维分解 |

## 评审查验清单

### 1. 四场景闭环完整性
- 4 个场景目录各 5 个产物，report 中 `verification_result.passed=true`
- network_latency 的 `execution_result.status = "success_with_rollback"`（首动作失败→自动回滚→备选动作）

### 2. 回滚安全边界剧本（network_latency）
- `audit_*.jsonl`：事件链 `whitelist_check → approval → checkpoint → execute(failed) → rollback → checkpoint → execute(success)`
- `incident_report_*.json`：动作 1 失败，`fallback: true` 备选动作接管
- `trace_*.json`：`skill.safe_execute` 下有 `execute.step_1`(ERROR) + `execute.rollback` + `execute.step_2` span 序列

### 3. 金融场景（transaction_risk_surge）
- 根因：撞库攻击导致批量账号被盗
- 动作类型：`freeze_account` / `trigger_2fa` / `notify_team`（金融风控动作白名单）
- 4 项异常指标全部回归基线

### 4. 回放评测
- `replay_eval/eval_report_*.json`：4 场景总分均 100.0，`all_passed_85: true`
- 五维评分（根因/动作/验证/闭环/安全）均满分

### 5. 评测区分度
- `eval_discrimination_report.md`：好 case 均分 ≥90、坏 case 均分 ≤50、差距 ≥40、各维度错误可检出

### 6. 协商机制
- **多方案协商**（`negotiation/nego_multiplan/`）：
  - report 含 `alternative_plans` 数组（≥2 候选方案）
  - trace 含 `plan.negotiation` span
  - audit 含 `plan_negotiation` + `plan_selection` 事件
- **证据补充反馈环**（`negotiation/nego_evidence_loop/`）：
  - trace 含 `rca.evidence_request` + `rca.reanalysis` span
  - audit 含 `evidence_request` + `evidence_supplement` + `rca_reanalysis` 事件

## 一致性说明

- 生成前已将 `data/knowledge/cases.jsonl` 恢复为 11 条种子案例、清空 `output/`，
  证据不依赖任何历史运行状态；
- 全程 MockProvider（确定性输出）+ 自动审批，无 API Key、无网络；
- `output/` 目录在证据固化后已再次清空（运行时目录保持干净），
  按上述命令重跑即可得到同构产物（时间戳与随机 ID 不同，结构与结论一致）。
