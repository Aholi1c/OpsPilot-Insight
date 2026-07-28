# OpsPilot-Insight

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green.svg)](requirements.txt)
[![Tests](https://img.shields.io/badge/Tests-39%20passed-brightgreen.svg)](tests/)

> 本项目以 [Apache License 2.0](LICENSE) 开源发布（Copyright © OpsPilot-Insight Contributors）。

**零人工运维场景的多 Agent 自愈系统** —— 阿里云 × DataWhale「AgentInfra——复杂任务多 Agent 自主协同」赛道参赛项目。

## 项目简介

OpsPilot-Insight 是一个面向线上故障"零人工闭环处置"的多 Agent 协同系统：告警接入后，由 5 个各司其职的 Agent 串行协作完成 **告警收敛 → 根因分析 → 方案规划 → 安全执行 → 验证复盘** 的完整自愈闭环，全程结构化上下文传递、全链路 Trace 追踪、关键操作审计留痕，并内建评测引擎持续度量 Agent 决策质量与成本开销。协同设计对齐阿里云 AgentTeams 的核心能力模型（角色编排 / 任务拆解 / 上下文传递 / 协同执行 / 状态追踪），能力映射详见 [docs/AGENTTEAMS_MAPPING.md](docs/AGENTTEAMS_MAPPING.md)。

**核心特性**

- **5-Agent 自愈闭环**：AlertAgent（告警收敛+影响面）→ RcaAgent（四维信号根因分析）→ PlannerAgent（方案+风险+回滚计划）→ ExecutorAgent（安全执行四件套）→ VerifierAgent（指标验证+复盘+案例沉淀）；
- **9 个标准化 Skill**：统一基类、统一 `SkillResult` 返回、声明式失败策略（abort/degrade）、自动 Trace 埋点与指标上报，目录见 [docs/SKILL_CATALOG.md](docs/SKILL_CATALOG.md)；
- **安全执行边界**：动作白名单 + 幂等键 + 人工审批交互点 + 回滚检查点（失败自动逆序回滚并续行备选动作）；
- **RAG 知识库**：Runbook 预案库 + 历史案例库，检索后端可插拔（本地 BM25 默认离线 / Chroma 向量可选），复盘案例自动沉淀回知识库形成经验闭环；
- **全链路可观测**：自研轻量 Tracer（OTel 语义）+ JSONL 结构化日志 + 审计事件流 + 进程内指标，一次运行五类产物落盘；
- **评测引擎 + 成本追踪**：Golden Dataset + 五项规则加权评分 + LLM-as-Judge + 一键回放评测；LLM 成本 per-Agent/per-Skill/per-Model 三维分解与预算告警；
- **Streamlit 可观测看板**：系统总览 / Trace 浏览器 / 评测报告 / 成本分析四页面；
- **完全离线可复现**：默认 MockProvider + Mock MCP 适配器，无 API Key、无网络即可完整跑通全部功能与测试。

## 系统架构

三层架构：协同层（Agent 编排）/ 能力层（Skill + MCP 工具）/ 基座层（可观测 + 评测 + RAG）。
详细设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

```
┌─────────────────────────────────── 协同层（Collaboration）────────────────────────────────────┐
│                    Orchestrator（五段串行编排 · 对齐 AgentTeams 能力模型）                        │
│        结构化上下文传递（AgentMessage / Pydantic）· 分段异常降级 · 审批交互点 · 产物落盘             │
│                                                                                              │
│  alerts.json ►┌──────────┐ ┌─────────┐ ┌────────────┐ ┌─────────────┐ ┌─────────────┐        │
│               │AlertAgent│►│ RcaAgent│►│PlannerAgent│►│ExecutorAgent│►│VerifierAgent│► Report │
│               │ 告警收敛  │ │ 多维根因 │ │  方案规划   │ │  安全执行    │ │  验证+复盘   │        │
│               └────┬─────┘ └────┬────┘ └─────┬──────┘ └──────┬──────┘ └──────┬──────┘        │
└────────────────────┼────────────┼────────────┼───────────────┼───────────────┼───────────────┘
                     │            │            │               │               │
┌────────────────────▼────────────▼─── 能力层（Capability）─────▼───────────────▼───────────────┐
│  9 个标准化 Skill（统一基类 / SkillResult / 失败策略 / 自动埋点）                                 │
│   诊断类: alert_fusion · impact_mapping · log_trace_rca                                       │
│   执行类: risk_guard · safe_execute（白名单+幂等+审批+检查点回滚）                                │
│   验证复盘: recovery_verify · postmortem     RAG 类: case_retrieval · runbook_rag              │
│  ─────────────────────────────────────────────────────────────────────────────────────────   │
│  Mock MCP 适配器: monitoring / monitoring_after / logging / tracing / change / execution      │
│  （读 examples/scenarios/<场景>/*.json，接口与真实 MCP 工具对齐，可整体替换）                      │
└──────────────────────────────────────────┬───────────────────────────────────────────────────┘
                                           │
┌──────────────────────────────────────────▼── 基座层（Infrastructure）─────────────────────────┐
│  可观测: Tracer（OTel 语义 Span 树）· JsonLogger（JSONL, 自动携带 trace_id/span_id）             │
│         AuditLog（审计事件流）· MetricsCollector（LLM/Skill/Agent/RAG/pipeline 指标）           │
│  评  测: Golden Dataset · 五项规则加权评分 · LLM-as-Judge · 回放评测 · 成本三维分解+预算控制       │
│  R A G : KnowledgeStore（runbooks 13 条 + cases 11 条种子）· LocalRetriever(BM25)/Chroma 可选   │
│  L L M : Provider 抽象 —— MockProvider（默认, 确定性输出）/ DashScopeProvider（通义千问）         │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 快速开始

**环境要求**：Python 3.9+（macOS / Linux / Windows 均可）；主流程第三方依赖仅 `pydantic` 与 `pytest`，**无需 API Key、无需网络**。

**安装**：

```bash
cd opspilot-insight
python3 -m pip install -r requirements.txt
```

**三条命令体验核心功能**：

```bash
# ① 一键运行故障自愈演示（五段闭环 + 五类可观测产物落盘 output/）
python3 run_demo.py --scenario db_pool_exhaustion --auto-approve

# ② 一键回放评测（3 场景回放 + Golden Dataset 构建 + 评测报告）
python3 scripts/replay_eval.py

# ③ 运行全部测试（39 项，全程离线）
python3 -m pytest tests/ -v
```

## 完整功能演示

### 1. 故障自愈 Demo（run_demo.py）

```bash
python3 run_demo.py --list-scenarios                              # 列出内置 3 个场景
python3 run_demo.py --scenario db_pool_exhaustion --auto-approve  # 连接池耗尽（变更引入泄漏）
python3 run_demo.py --scenario container_oom --auto-approve       # 容器 OOMKilled（缓存配置错误）
python3 run_demo.py --scenario network_latency --auto-approve     # 网络延迟劣化：演示"首动作失败→自动回滚→备选动作"
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--scenario / -s` | 场景名（见 `--list-scenarios`） |
| `--auto-approve` | 自动批准 medium/high 风险方案；**不加则进入交互式审批** |
| `--output-dir / -o` | 产物输出目录（默认 `./output`） |

### 2. 人工审批交互

PlannerAgent 产出方案的 `risk_level` 为 medium/high 时，ExecutorAgent 必须获得审批才会执行：

- **交互模式（默认）**：终端展示方案摘要与风险评估，输入 `y/n` 决定放行；
- **自动模式**：`--auto-approve` 参数（或测试路径）自动批准；
- 两种模式下，审批决定（who / when / decision / reason）均写入审计流 `output/audit_*.jsonl`。

```bash
python3 run_demo.py --scenario network_latency    # 不加 --auto-approve，体验交互式审批
```

### 3. 回放评测（scripts/replay_eval.py）

```bash
python3 scripts/replay_eval.py                     # 3 场景回放 + Golden 构建 + 评测报告
python3 scripts/replay_eval.py -s container_oom    # 只回放指定场景（可多次传入 -s）
PYTHONPATH=src python3 -m opspilot.evaluation.build_dataset   # 仅增量构建 Golden Dataset
```

三层评估体系：**规则评估**（根因命中 0.30 / 动作类型 0.20 / 验证一致 0.15 / 闭环完整 0.20 / 安全合规 0.15，加权 0-100 分）+ **LLM-as-Judge**（根因质量/方案合理性/复盘质量三维 1-5 分，默认确定性 MockJudge）+ **回放测试**（控制台汇总表 + `eval_report_*.json/.md` 落盘，含与上次运行对比）。

### 4. Streamlit 可观测看板（可选）

```bash
python3 -m pip install -r requirements-ui.txt   # streamlit + pandas + plotly
streamlit run ui/dashboard.py                    # 只读本地 output/ 与 data/golden/，不联网
```

四个页面：**系统总览**（KPI 卡片 + 运行历史 + 成本趋势 + Agent 健康）、**Trace 浏览器**（Span 树 + 属性详情 + 审计时间线）、**评测报告**（得分条形图 + 五维雷达 + Judge 评语）、**成本分析**（预算进度 + 三维分解 + 跨运行趋势）。

## 输出产物说明

一次 `run_demo.py` 运行在 `output/` 生成 5 类产物，`replay_eval.py` 额外生成评测报告：

| 产物 | 结构说明 |
| --- | --- |
| `incident_report_<场景>_<时间>.json` | 结构化闭环报告：`incident`（事件）、`root_cause_candidates`+`selected_root_cause`（根因+证据链）、`remediation_plan`（方案+回滚计划）、`execution_result`（审批+逐动作状态+回滚记录）、`verification_result`（逐指标对比）、`postmortem`（复盘+沉淀案例 ID）、`timeline`、`degraded` |
| `trace_<trace_id>.json` | 完整 Span 树（OTel 语义：trace_id/span_id/parent_span_id/kind/attributes/status），覆盖 pipeline→5 Agent→Skill/LLM 调用/执行动作全层级 |
| `run_<时间>.log` | JSON lines 结构化日志，每行自动携带 trace_id/span_id，可与 Trace 关联检索 |
| `audit_<时间>.jsonl` | 审计事件流：`whitelist_check` / `approval` / `checkpoint` / `execute` / `rollback` / `budget_alert`，均带 trace_id |
| `metrics_<trace_id>.json` | 进程内指标：LLM 调用与 token、Skill 成功率与耗时、Agent 耗时、RAG 命中、pipeline 总耗时，以及 `cost` 段（三维成本分解 + 预算状态） |
| `eval_report_<时间>.json/.md` | 评测报告：总分、逐场景五项规则得分、Judge 三维评分与评语、成本、与上次运行对比 |

## 项目结构

```
opspilot-insight/
├── run_demo.py                     # 一键演示入口（--scenario / --list-scenarios / --auto-approve）
├── scripts/replay_eval.py          # 回放评测：3 场景回放 + Golden 构建 + 评测报告（离线）
├── requirements.txt                # 主依赖（pydantic + pytest）
├── requirements-optional.txt       # 可选依赖（chromadb，启用向量检索后端）
├── requirements-ui.txt             # 可选依赖（streamlit + pandas + plotly，看板专用）
├── LICENSE                         # Apache 2.0
├── config/
│   ├── agents.yaml                 # 5 个 Agent 角色、职责、提示词模板、可用 Skill（角色编排配置）
│   ├── action_whitelist.yaml       # 执行动作白名单（7 类动作 + 默认风险等级）
│   └── pricing.yaml                # 成本模型（模型单价 + 单次事故预算上限）
├── src/opspilot/
│   ├── models.py                   # Pydantic 模型（Incident/Plan/Execution/Verification 等）
│   ├── config.py                   # 自研 YAML 子集解析器（不引入 PyYAML）
│   ├── orchestrator.py             # 五段串行编排（上下文传递 + 降级 + 成本/预算集成）
│   ├── agents/                     # 5 个 Agent
│   ├── skills/                     # 9 个 Skill（诊断 3 + 执行 2 + 验证复盘 2 + RAG 2）
│   ├── llm/                        # Provider 抽象 + Mock + DashScope（urllib 直连）
│   ├── mcp/mock_adapters.py        # Mock MCP：monitoring(_after)/logging/tracing/change/execution
│   ├── rag/                        # KnowledgeStore + LocalRetriever(BM25)/ChromaRetriever
│   ├── observability/              # Tracer + JsonLogger + AuditLog + MetricsCollector
│   └── evaluation/                 # 评测引擎：dataset_builder + evaluator + judges + cost
├── data/
│   ├── knowledge/                  # RAG 种子数据（runbooks.jsonl 13 条 + cases.jsonl 11 条）
│   └── golden/                     # Golden Dataset（自动构建，幂等更新）
├── examples/scenarios/             # 3 个内置故障场景（含 expected.json 标准答案）
├── ui/                             # Streamlit 看板（dashboard.py + pages/ 三页面）
├── docs/
│   ├── ARCHITECTURE.md             # 详细架构文档（分层职责/数据流/Trace 模型/安全边界/数据飞轮）
│   ├── AGENTTEAMS_MAPPING.md       # 与阿里云 AgentTeams 的能力映射与迁移路径
│   ├── AGENT_IDENTITY.md           # 5 个 Agent 身份清单
│   ├── SKILL_CATALOG.md            # Skill 目录 v2.0（9 个已实现）
│   └── evidence/                   # 提交材料：一套完整的干净运行证据（含查验说明）
├── output/                         # 运行时产物目录（git 忽略，保留 .gitkeep）
└── tests/                          # 39 项测试（e2e / 阶段 2 / 阶段 3）
```

## 测试

```bash
python3 -m pytest tests/ -v        # 全量 39 项，全程离线，约 10 秒
```

- `test_e2e.py`：3 场景端到端闭环 + Span 树父子关系校验；
- `test_stage2.py`：五段闭环 / 白名单拒绝 / 幂等跳过 / 失败回滚 / RAG 检索 / 案例沉淀幂等；
- `test_stage3.py`：Golden 构建幂等 / 规则评分边界 / 成本三维一致性 / 预算告警 / 回放评测。

## 配置说明

### LLM Provider 切换

默认 `MockProvider`（确定性输出，离线）。切换通义千问：

```bash
export OPSPILOT_LLM_PROVIDER=dashscope
export DASHSCOPE_API_KEY=sk-xxx
python3 run_demo.py --scenario db_pool_exhaustion --auto-approve
```

LLM-as-Judge 同理：`export OPSPILOT_JUDGE=dashscope`（默认 MockJudge）。

### RAG 检索后端切换

默认 `LocalRetriever`（纯 Python BM25，零额外依赖）。切换 Chroma 向量检索：

```bash
python3 -m pip install -r requirements-optional.txt
export OPSPILOT_RAG_BACKEND=chroma
export DASHSCOPE_API_KEY=sk-xxx      # 向量化使用 DashScope embedding
# Chroma 不可用时自动回退本地检索，不影响主流程
```

### 预算控制（config/pricing.yaml）

`budget.per_incident` 为单次事故处理预算上限（默认 0.10 元）。超限**不中断流程**，仅记审计事件 `budget_alert`，并在 metrics/评测报告/看板标红。单价按模型配置（qwen-plus/turbo/max + mock 虚拟单价）。

### 动作白名单（config/action_whitelist.yaml）

7 类允许动作：`rollback_change` / `scale_pool` / `rolling_restart` / `config_update` / `traffic_switch` / `diagnostic_capture` / `manual_followup`，各带默认风险等级。白名单外动作一律拒绝并审计留痕；medium/high 风险方案必须经人工审批。

## FAQ

**Q1：为什么默认使用 Mock Provider，而不直接调用通义千问？**

这是刻意的工程决策，而非能力缺失：

1. **赛事离线复现保障**：评审红线是"按 README 无法复现 Demo 即取消评奖"。MockProvider 提供确定性输出，保证任何环境（无 API Key、无网络、任何时间）都能 100% 复现完整五段闭环、全部 39 项测试与评测报告；
2. **接口已预留 DashScope**：`src/opspilot/llm/` 下 Provider 抽象已完整实现 `DashScopeProvider`（urllib 直连、读取真实 token usage 计费），两个环境变量即可切换（见上文配置说明），业务代码零改动；
3. **执行链路本就不依赖 LLM**：ExecutorAgent 的安全执行为确定性逻辑（白名单/幂等/回滚），LLM 仅用于摘要、根因叙述、方案叙述与复盘增强，Mock 与真实 Provider 下闭环行为一致。

**Q2：Mock MCP 适配器与真实环境的差距？**

适配器接口按真实 MCP 工具语义设计（monitoring/logging/tracing/change/execution），仅数据源换成场景 JSON。接入真实环境时只需替换 `src/opspilot/mcp/` 的适配器实现，Agent/Skill 层零改动。

**Q3：如何与阿里云 AgentTeams 对应？**

本项目自研编排器按 AgentTeams 的能力模型设计（角色编排/任务拆解/上下文传递/协同执行/状态追踪），逐项映射与迁移路径见 [docs/AGENTTEAMS_MAPPING.md](docs/AGENTTEAMS_MAPPING.md)。

**Q4：评测分数如何计算，多少算通过？**

五项规则加权（根因 0.30 / 动作 0.20 / 验证 0.15 / 闭环 0.20 / 安全 0.15）满分 100；回放评测以"全部样本 ≥ 85 分"为通过线（`replay_eval.py` 退出码 0/1 可直接接入 CI）。

**Q5：知识库会被 Demo 运行污染吗？**

`replay_eval.py` 使用知识库临时拷贝，不写回仓库；`run_demo.py` 的案例沉淀按 incident_id 幂等写入 `data/knowledge/cases.jsonl`，提交版本已恢复为 11 条种子案例。

## 运行证据

`docs/evidence/` 目录内附一套完整的干净运行产物（3 场景报告 + trace + 审计 + 指标 + 评测报告）及查验说明，供评审在不运行代码的情况下核验产物结构；重新运行 `run_demo.py` / `replay_eval.py` 即可在 `output/` 复现同构产物。

## License

本项目以 [Apache License 2.0](LICENSE) 开源，Copyright © 2026 OpsPilot-Insight Contributors。
