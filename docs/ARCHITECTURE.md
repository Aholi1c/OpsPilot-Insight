# OpsPilot-Insight 架构文档

> 本文档描述系统的分层架构、各层职责、数据流、上下文传递结构、Trace 模型、
> 安全边界设计与评测数据飞轮。与 AgentTeams 框架的能力映射见
> [AGENTTEAMS_MAPPING.md](AGENTTEAMS_MAPPING.md)。

## 1. 总体分层

系统分为三层，层间单向依赖（协同层 → 能力层 → 基座层）：

| 层 | 组成 | 职责 |
| --- | --- | --- |
| **协同层** | Orchestrator + 5 Agent + `config/agents.yaml` | 角色编排、任务拆解、上下文传递、异常降级、审批交互、产物汇总 |
| **能力层** | 9 个标准化 Skill + Mock MCP 适配器 | 原子能力封装（诊断/执行/验证/复盘/RAG）、外部系统访问抽象 |
| **基座层** | 可观测四件套 + 评测引擎 + 成本模型 + RAG 存储 + LLM Provider | 横切能力：追踪、日志、审计、指标、评测、成本、知识检索、模型接入 |

### 1.1 协同层

- **Orchestrator**（`src/opspilot/orchestrator.py`）：一次 `run(scenario)` 完成一次自愈闭环。
  职责：装配运行时依赖（Tracer/Logger/Audit/Metrics/MCP/RAG/Skill/Agent）→ 按
  Alert→RCA→Planner→Executor→Verifier 顺序驱动 → 每阶段独立 try/except 降级 →
  成本结算与预算判定 → 汇总 `IncidentReport` 并落盘五类产物。
- **Agent**（`src/opspilot/agents/`）：均继承 `BaseAgent`，身份（角色/职责/决策边界/
  提示词模板/可用 Skill）由 `config/agents.yaml` 声明式定义，代码只实现 `handle(message)`。
  Agent 不直接访问外部系统，只通过 Skill 与 LLM Provider 工作。
- **职责边界**（详见 [AGENT_IDENTITY.md](AGENT_IDENTITY.md)）：

| Agent | 输入 | 输出 | 决策边界 |
| --- | --- | --- | --- |
| AlertAgent | 原始告警流 | `Incident` | 只做收敛与影响评估，不判根因 |
| RcaAgent | `Incident` | 根因候选+选定根因 | 只诊断不开方案，置信度不足如实标注 |
| PlannerAgent | 选定根因 | `RemediationPlan` | 只产方案不执行，medium+ 风险必须声明审批 |
| ExecutorAgent | `RemediationPlan` | `ExecutionResult` | 仅执行已审批的白名单动作，不调用 LLM |
| VerifierAgent | `ExecutionResult`+全上下文 | `VerificationResult`+`PostmortemReport` | 只验证复盘，不自行修复 |

### 1.2 能力层

- **Skill**（`src/opspilot/skills/`）：全部继承 `Skill` 基类（`base.py`），
  统一契约：类属性声明 `name` / `version` / `failure_policy`（abort/degrade）与
  `preconditions`；`execute()` 模板方法自动完成 Span 包裹（`skill.{name}`）、
  指标上报、异常按策略处理，统一返回 `SkillResult`。
  9 个 Skill 的输入输出与安全属性见 [SKILL_CATALOG.md](SKILL_CATALOG.md)。
- **MCP 适配器**（`src/opspilot/mcp/mock_adapters.py`）：6 个适配器
  （monitoring / monitoring_after / logging / tracing / change / execution）
  以真实 MCP 工具的语义提供数据与执行接口，当前实现读取
  `examples/scenarios/<场景>/*.json`。接入真实环境只需替换本模块，上层零改动。

### 1.3 基座层

- **可观测**（`src/opspilot/observability/`）：Tracer（见 §4）、JsonLogger
  （JSONL 日志，自动携带 trace_id/span_id）、AuditLog（审计事件流，见 §5）、
  MetricsCollector（LLM/Skill/Agent/RAG/pipeline 指标 + cost 段）。
- **评测引擎**（`src/opspilot/evaluation/`）：dataset_builder（Golden 构建，幂等）、
  evaluator（五项规则加权）、judges（Mock/DashScope Judge）、cost（三维成本+预算）。
- **RAG**（`src/opspilot/rag/`）：KnowledgeStore（runbooks + cases JSONL 存储）+
  可插拔检索器（LocalRetriever BM25 默认 / ChromaRetriever 可选，失败自动回退）。
- **LLM Provider**（`src/opspilot/llm/`）：`LLMProvider` 抽象 + MockProvider
  （确定性输出）+ DashScopeProvider（urllib 直连，读取真实 token usage）。
  环境变量 `OPSPILOT_LLM_PROVIDER` 切换。

## 2. 数据流

一次完整闭环的数据流（括号内为阶段产出的关键结构）：

```
alerts.json（AlertManager 格式原始告警流）
   │
   ▼
AlertAgent ── alert_fusion（去重+聚合）→ impact_mapping（爆炸半径）→ LLM 摘要
   │   产出: Incident（告警组/严重度/受影响服务/影响面/摘要）+ first_alert_at
   ▼
RcaAgent ── log_trace_rca（日志/链路/指标/变更四维信号关联）
   │        └─ case_retrieval（相似历史案例注入分析上下文）→ LLM 结论
   │   产出: RootCauseCandidate[]（假设+证据链+置信度）+ selected_root_cause
   ▼
PlannerAgent ── runbook_rag（预案检索）+ case_retrieval（历史处置参考）
   │            └─ 规则引擎生成方案骨架（steps 携带白名单 action_type）→ LLM 叙述
   │   产出: RemediationPlan（步骤/风险等级/审批要求/回滚计划/Runbook 引用）
   ▼
ExecutorAgent ── risk_guard（风险评估+审批判定）→ 审批交互点（y/n 或 auto）
   │             └─ safe_execute（白名单→幂等→检查点→执行→失败回滚→备选续行）
   │   产出: ExecutionResult（审批记录/逐动作状态/回滚记录）+ 审计事件流
   ▼
VerifierAgent ── recovery_verify（故障期 vs 修复后指标对比）
   │             └─ postmortem（时间线/根因/处置/效果/改进）→ 案例沉淀写回知识库
   │   产出: VerificationResult + PostmortemReport（含 case_id）
   ▼
IncidentReport（全量结构化汇总）→ output/ 五类产物落盘
```

**降级路径**：每个阶段被 Orchestrator 独立 try/except 包裹，失败时记录
`degradation_notes` 并以"该阶段字段为空/最小对象"的方式继续，保证任何单点故障
都能产出部分报告而非整体崩溃（如 RCA 失败 → 报告无根因结论转人工，Planner
仍会产出"冻结发布+转人工"的兜底方案）。

## 3. 上下文传递结构

Agent 间不共享内存对象，所有上下文经 **`AgentMessage`**（`src/opspilot/models.py`）
显式传递：

```python
class AgentMessage(BaseModel):
    message_id: str      # 自动生成，消息唯一标识
    sender: str          # 发送方（orchestrator / agent 名）
    receiver: str        # 接收方
    message_type: str    # incident_request / incident / root_cause / remediation_plan / execution_result
    content: Dict        # 阶段上下文（Pydantic 模型 model_dump() 后的字典）
    trace_id: str        # 全链路关联
    timestamp: str
```

设计要点：

1. **强类型内核 + 字典信封**：`content` 内的实体（Incident / RemediationPlan /
   ExecutionResult 等）均为 Pydantic 模型序列化结果——跨 Agent 传递时天然可
   序列化、可校验、可落盘重放，这也是 Golden Dataset 与回放评测得以实现的基础；
2. **累积式上下文**：每个 Agent 在上游 `content` 之上追加自己的产出
   （`stage_ctx = {**上游, 本阶段新增}`），下游 Agent 可访问全部历史上下文
   （如 VerifierAgent 复盘需要 incident + root_cause + plan + execution 全量信息）；
3. **trace_id 贯穿**：消息、日志、审计、Span、产物文件名共享同一 trace_id，
   任意产物可反查全链路。

## 4. Trace 模型

自研轻量 Tracer（`src/opspilot/observability/tracer.py`），**数据结构与语义对齐
OpenTelemetry**，未引入 SDK 依赖：

- **Span 字段**：`trace_id`（16 字节 hex）/ `span_id`（8 字节 hex）/
  `parent_span_id` / `name` / `kind`（INTERNAL/CLIENT/...）/ `start_time`、
  `end_time`（epoch 纳秒）/ `attributes` / `status`（UNSET/OK/ERROR）+ `status_message`；
- **自动父子关联**：通过 `contextvars` 维护"当前 Span"，`start_span()` 上下文
  管理器嵌套调用即自动建立父子关系，无需手工传递；
- **一次运行 = 一个 trace_id**：Orchestrator 每次 `run()` 新建 Tracer。

典型 Span 树（network_latency 场景，含回滚剧本）：

```
pipeline.run (INTERNAL)
├── agent.AlertAgent
│   ├── skill.alert_fusion
│   ├── skill.impact_mapping
│   └── llm.complete (CLIENT)
├── agent.RcaAgent
│   ├── skill.log_trace_rca
│   ├── skill.case_retrieval
│   └── llm.complete (CLIENT)
├── agent.PlannerAgent
│   ├── skill.runbook_rag
│   ├── skill.case_retrieval
│   └── llm.complete (CLIENT)
├── agent.ExecutorAgent          # 不含 llm.complete：执行链路确定性要求
│   ├── skill.risk_guard
│   └── skill.safe_execute
│       ├── execute.step_1 (CLIENT, status=ERROR)   # 首动作失败
│       ├── execute.rollback (CLIENT)               # 检查点逆序回滚
│       ├── execute.step_2 (CLIENT)                 # 备选动作
│       └── execute.step_3 (CLIENT)
└── agent.VerifierAgent
    ├── skill.recovery_verify
    ├── skill.postmortem
    └── llm.complete (CLIENT)
```

导出为 `output/trace_<trace_id>.json`，Streamlit 看板的 Trace 浏览器直接消费该文件。

## 5. 安全边界设计

执行是唯一"改变世界"的环节，ExecutorAgent 以四层防线约束（全部留痕于
`output/audit_*.jsonl`，每条审计事件带 trace_id）：

| 防线 | 机制 | 审计事件 |
| --- | --- | --- |
| ① 白名单 | `config/action_whitelist.yaml` 定义 10 类允许动作；白名单外一律拒绝（动作状态 `rejected_whitelist`） | `whitelist_check` |
| ② 人工审批 | risk_guard 判定 overall_risk；medium/high 必须审批（交互式 y/n 或 --auto-approve），拒绝则整个方案不执行 | `approval` |
| ③ 幂等键 | `sha256(incident_id + 动作内容)`，同一事件内重复动作直接跳过（`skipped_idempotent`） | `execute` |
| ④ 回滚检查点 | 每个动作执行前记录快照；失败时按检查点**逆序**自动回滚，随后续行备选（fallback）动作 | `checkpoint` / `rollback` |

补充设计：

- **ExecutorAgent 不调用 LLM**：执行链路要求完全确定性，LLM 只出现在
  摘要/分析/叙述等非决断环节；
- **未知动作类型按 high 风险评定**（默认拒绝倾向）；
- **风险评估失败按最保守策略**：risk_guard 降级时视为高风险、强制审批；
- **预算越界不熔断**：成本超 `budget.per_incident` 只记 `budget_alert` 审计事件
  并标红，不中断自愈流程（可用性优先于成本约束的取舍，阈值可配）。

## 6. 协商与反馈机制（可选增强，默认关闭）

五个 Agent 并非只沿固定流水线单向传递结果：开启协商模式后，Agent 之间具备
**"提出请求 → 补充响应 → 二次决策"** 的反馈协商能力，全过程以结构化
`AgentMessage`、专属 Span 与审计事件留痕（自主协同可观测）。

**开启方式**：`config/agents.yaml` 的 `negotiation.enabled: true`，或
`run_demo.py --negotiation`（`--rca-threshold` 可临时覆盖置信度阈值）。
默认关闭时全部逻辑旁路，四场景运行结果与原流水线完全一致。

### 6.1 机制 1：RCA 低置信度证据补充反馈环

RcaAgent 根因置信度低于阈值（`rca_confidence_threshold`，默认 0.6）时，
**不直接降级转人工**，而是向 Orchestrator 发起证据补充请求：

```
RcaAgent 首轮分析 → Top1 置信度 < 阈值
   │  返回 needs_more_evidence + 缺失数据类型清单
   │  （extended_time_window_logs / change_ticket_details / ...，
   │    由 weak/missing 证据维度推导）
   ▼
Orchestrator 经 MCP 适配器补充采集
   │  logging.query_extended_logs()（场景目录 logs_extended.json）
   │  change.get_change_details()（变更单 diff 详情）
   ▼
RcaAgent 二轮分析（补充证据作为第五维 supplemental 证据参与置信度计算）
   │  达标 → 正常续行；仍低于阈值且重试上限已到（max_evidence_rounds，
   ▼  默认 1 轮防死循环）→ 走原有降级路径转人工
```

留痕：Span `rca.evidence_request` / `rca.reanalysis`（挂在 agent.RcaAgent 下）、
审计事件 `evidence_request` / `evidence_supplement` / `rca_reanalysis` /
`rca_low_confidence_handoff`、`pipeline_timeline` 中的结构化 AgentMessage
（`evidence_request` / `evidence_supplement`），报告 `negotiation.evidence_loop`
记录触发轮次与结果。

### 6.2 机制 2：多根因候选 → 多方案并行 → 决策选择

RcaAgent 产出多个置信度接近的根因候选（差距 < `candidate_gap_threshold`，
默认 0.15，如 container_oom 的"变更引入内存膨胀 0.95" vs "应用内存泄漏
0.85"竞争假设）时，进入多方案协商：

1. **并行方案生成**：PlannerAgent 为每个候选独立生成方案（含各自风险评估），
   Span 结构为 `plan.negotiation` 下多个并行 `agent.PlannerAgent` 兄弟节点；
2. **量化决策**（`src/opspilot/negotiation.py`）：
   `score = 置信度 × 风险因子 / (1 + 预估恢复时长/60)`，确定性可复现，
   打分明细全部写入审计事件 `plan_selection`；
3. **选择**：交互模式由人工从多方案中选择（复用审批交互通道），
   `--auto-approve` 自动选打分最优；
4. **落选方案留证**：未选中方案（含打分明细与落选原因）记入报告
   `alternative_plans` 字段，决策过程记入 `negotiation.plan_negotiation`。

### 6.3 与原流水线的关系

- 两个机制均为 Orchestrator 内的**可选增强分支**：协商配置未开启时代码
  路径完全旁路；协商路径自身异常也会回退默认单方案路径（不引入新故障点）；
- 机制 1 失败的最终去向仍是原有降级路径（转人工），保持决策边界不变；
- 相关测试：`tests/test_negotiation.py`（触发/重试上限/多方案生成与选择/
  默认模式回归守护）。

## 7. 评测数据飞轮

系统内建"运行 → 沉淀 → 评测 → 改进"的数据闭环：

```
        ┌───────────────────────────────────────────────────────┐
        │                                                       │
        ▼                                                       │
  ① 运行闭环（run_demo / replay_eval）                           │
        │  产出 incident_report / trace / audit / metrics        │
        ▼                                                       │
  ② 双向沉淀                                                    │
        │  ├─ 知识沉淀: VerifierAgent 将复盘案例写回 cases.jsonl   │
        │  │   （incident_id 幂等）→ 供后续 RAG 检索，检索质量随    │
        │  │   运行次数增长                                       │
        │  └─ 评测沉淀: build_dataset 从产物提取 Golden 样本       │
        │      （同 case 幂等更新；expected 以人工校准的            │
        │      expected.json 为准，缺失时从产物固化）               │
        ▼                                                       │
  ③ 评测（evaluator + judges）                                   │
        │  五项规则加权评分 + LLM-as-Judge 三维评分                │
        │  → eval_report_*.json/.md（含与上次运行对比）            │
        ▼                                                       │
  ④ 改进定位                                                    │
        │  失败项定位到具体维度（根因未命中/动作错误/安全违规...）    │
        │  + 成本三维分解定位开销热点 → 调整 Skill 规则/提示词/预案   │
        └───────────────────────────────────────────────────────┘
```

关键性质：

- **幂等性**：案例沉淀与 Golden 更新都以稳定键幂等，反复运行不产生重复数据；
- **可回归**：`replay_eval.py` 退出码语义（全部 ≥ 85 分为 0）可直接作为 CI 门禁，
  任何改动是否劣化 Agent 决策质量可被自动发现；
- **标准答案人工校准优先**：`examples/scenarios/<场景>/expected.json` 为 curated
  真值，避免"用自己的输出评自己"的自证循环。

## 8. 扩展点一览

| 扩展目标 | 需要改动 | 不需要改动 |
| --- | --- | --- |
| 接入真实监控/日志/执行系统 | `src/opspilot/mcp/` 适配器 | Agent / Skill / Orchestrator |
| 切换真实 LLM | 环境变量（DashScopeProvider 已实现） | 全部业务代码 |
| 新增故障场景 | `examples/scenarios/<新场景>/` 数据 + expected.json | 代码零改动 |
| 新增修复动作类型 | `config/action_whitelist.yaml` + Planner 规则 | 安全执行框架 |
| 新增 Agent / Skill | `config/agents.yaml` + 继承基类实现 | 编排与可观测基座 |
| 迁移到 AgentTeams | 见 [AGENTTEAMS_MAPPING.md](AGENTTEAMS_MAPPING.md) §3 | 能力层与基座层 |
