# OpsPilot-Insight × 阿里云 AgentTeams 能力映射

> 赛事要求以 AgentTeams 为多 Agent 协同的设计基点。本项目采用自研轻量编排器实现，
> 但**协同架构严格按照 AgentTeams 的核心能力模型设计**——本文逐项说明能力映射关系、
> 设计上为何天然兼容，以及迁移到 AgentTeams 托管运行时的具体路径。

## 1. 能力映射总表

| AgentTeams 核心能力 | 本项目对应实现 | 位置 |
| --- | --- | --- |
| **角色编排**（Role Orchestration） | Orchestrator 统一装配与调度 + `agents.yaml` 声明式角色定义（角色/职责/决策边界/提示词模板/可用 Skill） | `src/opspilot/orchestrator.py`、`config/agents.yaml` |
| **任务拆解**（Task Decomposition） | "故障自愈"复杂任务固定拆解为五段流水线：告警收敛 → 根因分析 → 方案规划 → 安全执行 → 验证复盘，每段职责单一、边界清晰 | `orchestrator.py` 阶段 1-5、`docs/AGENT_IDENTITY.md` |
| **上下文传递**（Context Passing） | 结构化 `AgentMessage` 信封（sender/receiver/message_type/content/trace_id）+ 全部实体为 Pydantic 强类型模型，累积式上下文（下游可见全部上游产出） | `src/opspilot/models.py` |
| **协同执行**（Collaborative Execution） | 串行协同 + 分段异常降级（任一 Agent 失败，流水线不中断，记录降级说明继续产出部分结果）+ 人工审批交互点（human-in-the-loop） | `orchestrator.py` 各阶段 try/except、`run_demo.py` 审批回调 |
| **状态追踪**（State Tracking） | 全链路 Trace（一次协同一个 trace_id，Span 树覆盖 pipeline→Agent→Skill→动作）+ 审计事件流 + pipeline 时间线 + 进程内指标 | `src/opspilot/observability/` |

## 2. 逐项设计说明

### 2.1 角色编排 ↔ Orchestrator + agents.yaml

AgentTeams 的编排理念是"角色由配置声明，运行时按配置装配"。本项目完全一致：

- `config/agents.yaml` 声明 5 个角色的 name / role / responsibilities / inputs /
  outputs / skills / decision_boundary / prompt_template；
- Orchestrator 在 `run()` 时读取配置逐个实例化 Agent，注入公共依赖
  （LLM Provider / Tracer / Logger / Skill 注册表）；
- Agent 代码只实现 `handle(message)` 行为，身份与提示词全部外置——
  更换角色定义无需改代码，与 AgentTeams "配置即编排"的模式同构。

### 2.2 任务拆解 ↔ 五段流水线

"零人工故障自愈"被拆解为 5 个内聚子任务，每个 Agent 的决策边界显式声明且互斥
（如 RcaAgent 只诊断不开方案、PlannerAgent 只开方案不执行、ExecutorAgent 只执行
已审批白名单动作）。这与 AgentTeams 中"复杂任务 → 子任务 → 专职 Agent"的拆解
方法论一致；拆解粒度以"单一职责 + 可独立评测"为准（评测引擎正是按段打分）。

### 2.3 上下文传递 ↔ 结构化 AgentMessage / Pydantic 模型

- 消息信封 `AgentMessage` 与 AgentTeams 的消息模型语义对齐
  （发送方/接收方/消息类型/负载/追踪标识/时间戳）；
- 负载实体（Incident / RootCauseCandidate / RemediationPlan / ExecutionResult /
  VerificationResult / PostmortemReport）均为 Pydantic 模型，天然 JSON 可序列化——
  这意味着上下文可以直接跨进程/跨服务传输，不依赖共享内存；
- 采用累积式上下文（每段在上游 content 基础上追加），等价于 AgentTeams 的
  共享会话上下文语义。

### 2.4 协同执行 ↔ 串行 + 降级 + 审批

- 故障自愈场景存在硬因果依赖（没有根因就没有方案，没有审批就不能执行），
  因此协同拓扑选择**串行流水线**——这是 AgentTeams 支持的基础协同模式之一；
- 每段独立异常捕获与降级：单 Agent 失败不拖垮团队，产出部分结果并标注
  `degradation_notes`，与 AgentTeams 的容错协同理念一致；
- ExecutorAgent 内置人工审批交互点（medium/high 风险必须审批），
  对应 AgentTeams 的 human-in-the-loop 协同环节。

### 2.5 状态追踪 ↔ Trace / 审计 / 指标

- 一次协同（一次 `run()`）= 一个 trace_id，Span 树完整记录"哪个 Agent 在什么时间
  调用了哪个 Skill/LLM/动作、耗时多少、成功与否"；
- 审计事件流记录全部安全敏感状态变迁（白名单校验/审批/检查点/执行/回滚/预算告警）；
- pipeline 时间线记录各 Agent 起止事件；指标记录各 Agent/Skill 的调用与耗时——
  四者共同构成 AgentTeams "协同过程可回放、可审计"的状态追踪要求，
  且全部产物可在 Streamlit 看板可视化。

## 3. 迁移到 AgentTeams 的路径

### 3.1 为什么设计上天然兼容

1. **角色定义已配置化**：`agents.yaml` 的字段（角色/职责/提示词/工具集）与
   AgentTeams 的 Agent 定义字段基本一一对应，可脚本化转换；
2. **上下文已全量可序列化**：Agent 间零共享内存、全部 Pydantic JSON 传递，
   从进程内消息切换到平台托管消息只是传输通道的替换；
3. **能力已工具化**：9 个 Skill 有统一的名称/版本/输入输出/失败策略契约，
   可直接注册为 AgentTeams 的工具（Tool/MCP 形态）；
4. **可观测已按开放语义建模**：Trace 遵循 OpenTelemetry 语义，可直接对接
   阿里云 ARMS/SLS，替换自研 Tracer 仅是导出端变更。

### 3.2 各层迁移工作量

| 层 | 迁移动作 | 改动量 |
| --- | --- | --- |
| **协同层 · Orchestrator** | 五段串行编排逻辑改由 AgentTeams 的团队编排定义承载（角色顺序、降级策略、审批节点在平台上声明）；本地 Orchestrator 退化为产物汇总器 | 主要改动点（编排逻辑平移，约一个模块） |
| **协同层 · Agent** | `agents.yaml` 转换为 AgentTeams Agent 定义；`handle()` 内的业务逻辑保留为 Agent 的执行体 | 低（配置转换为主） |
| **上下文传递** | `AgentMessage` 映射为平台消息格式；Pydantic 模型作为 payload schema 原样保留 | 低（信封适配） |
| **能力层 · Skill** | 注册为平台工具；Skill 实现零改动 | 低 |
| **能力层 · MCP 适配器** | Mock 适配器替换为真实 MCP Server 接入（AgentTeams 原生支持 MCP） | 中（数据源对接，接口已对齐） |
| **基座层 · LLM** | MockProvider 切换为 DashScopeProvider（已实现）或平台托管模型 | 零（环境变量） |
| **基座层 · 可观测** | Tracer 导出对接 ARMS/SLS；审计与指标结构保留 | 低（导出端） |
| **基座层 · 评测/RAG** | 评测引擎与知识库独立于编排框架，原样保留；RAG 可选升级为百炼知识库 | 零～低 |

### 3.3 迁移不变量

以下资产在迁移前后保持不变，是本项目的核心价值沉淀：

- 5 个 Agent 的角色定义与决策边界（`agents.yaml` / `AGENT_IDENTITY.md`）；
- 9 个 Skill 的能力契约与实现（`SKILL_CATALOG.md`）；
- 全部 Pydantic 数据模型（上下文 schema）；
- 安全执行四件套的策略配置（白名单/审批/幂等/回滚）；
- Golden Dataset 与评测规则（迁移后可继续用回放评测验证行为一致性——
  这正是自建评测引擎的意义：框架迁移的回归门禁）。
