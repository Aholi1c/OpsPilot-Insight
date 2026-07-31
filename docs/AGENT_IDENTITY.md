# Agent Identity 清单

> OpsPilot-Insight 全部 5 个 Agent 的身份定义。与 `config/agents.yaml` 保持一致；
> 阶段 2 起 5 个 Agent 均已实现并在五段串行闭环中启用。
>
> 字段说明：Name（标识）/ Role（角色）/ Capabilities（能力）/ Inputs（输入）/
> Outputs（输出）/ Dependencies（依赖）/ DecisionBoundary（决策边界）/ Trace（链路埋点）。

---

## 1. AlertAgent（已实现）

| 字段 | 内容 |
| --- | --- |
| **Name** | `alert_agent` / AlertAgent |
| **Role** | 告警接入与影响面评估专家 |
| **Capabilities** | ① 告警去重（fingerprint）与聚合（alertname/service 分组）② 影响面与爆炸半径评估（依赖拓扑反向传播）③ 生成事件级摘要，收敛为结构化 Incident |
| **Inputs** | AlertManager 格式原始告警流（`alerts.json`） |
| **Outputs** | `Incident`（告警组 / 严重度 / 受影响服务 / 影响面 / 摘要）+ 首告警时间 |
| **Dependencies** | Skill: `alert_fusion`、`impact_mapping`；MCP: tracing（服务依赖拓扑）；LLM: incident_summary 摘要生成 |
| **DecisionBoundary** | 只做告警整理与影响评估，不做根因判断，不触碰任何生产资源 |
| **Trace** | Span `agent.AlertAgent`（INTERNAL）→ 子 Span `skill.alert_fusion` / `skill.impact_mapping` / `llm.complete`（CLIENT） |

## 2. RcaAgent（已实现）

| 字段 | 内容 |
| --- | --- |
| **Name** | `rca_agent` / RcaAgent |
| **Role** | 多维根因分析专家 |
| **Capabilities** | ① 关联日志/链路/指标/变更四维异常信号 ② 生成根因候选（假设 + 证据链 + 证据强度 strong/weak/missing + 置信度）③ 输出可读根因分析结论 ④ 协商模式：置信度低于阈值时向 Orchestrator 发起证据补充请求（缺失数据类型清单），获补充证据后二轮分析（重试上限防死循环）；变更强相关时产出竞争性假设候选供多方案协商 |
| **Inputs** | `Incident`（含受影响服务与首告警时间）；协商模式二轮追加 `supplemental_evidence`（扩展时间窗日志 / 变更单详情） |
| **Outputs** | 根因候选列表（`RootCauseCandidate[]`）与选定根因（含证据链、关联变更单号）；协商模式可能输出 `needs_more_evidence` 信号 + 缺失数据类型清单 |
| **Dependencies** | Skill: `log_trace_rca`、`case_retrieval`（相似历史案例注入分析上下文）；MCP: logging / monitoring / tracing / change；LLM: rca_analysis 结论生成 |
| **DecisionBoundary** | 只做根因诊断与证据陈述，不生成修复方案；置信度不足时如实标注、不臆断（协商模式下先请求补充证据，重试上限后仍不足才转人工） |
| **Trace** | Span `agent.RcaAgent` → 子 Span `skill.log_trace_rca` / `llm.complete`；协商模式追加 `rca.evidence_request` / `rca.reanalysis` |

## 3. PlannerAgent（已实现）

| 字段 | 内容 |
| --- | --- |
| **Name** | `planner_agent` / PlannerAgent |
| **Role** | 修复方案规划专家 |
| **Capabilities** | ① 基于选定根因生成分步修复方案（含可执行命令与预期效果）② 评估风险等级（low/medium/high）并声明审批要求 ③ 生成对应回滚计划 ④ 协商模式：为多个置信度接近的根因候选并行生成独立方案（含各自风险评估），供打分决策 / 人工选择 |
| **Inputs** | 选定根因（含主嫌疑服务与关联变更信息） |
| **Outputs** | `RemediationPlan`（修复步骤 / 风险等级 / 审批要求 / 回滚计划 / 方案说明） |
| **Dependencies** | Skill: `runbook_rag`（方案引用 Runbook 依据）、`case_retrieval`（参考历史处置）；规则引擎（按根因类别生成方案骨架，steps 携带白名单动作类型）；LLM: plan_narrative 方案叙述 |
| **DecisionBoundary** | 只产出方案不执行任何变更；medium 及以上风险必须声明需人工审批；无可信根因时输出高风险"冻结发布 + 转人工"兜底方案 |
| **Trace** | Span `agent.PlannerAgent` → 子 Span `llm.complete`；协商模式下多个并行 `agent.PlannerAgent` span 挂在 `plan.negotiation` 下 |

## 4. ExecutorAgent（已实现，阶段 2）

| 字段 | 内容 |
| --- | --- |
| **Name** | `executor_agent` / ExecutorAgent |
| **Role** | 安全执行专家 |
| **Capabilities** | ① 执行前风险评估（RiskGuard：动作风险等级 / 影响半径 / 审批判定）② 人工审批交互点（medium/high 风险必须审批，决定 who/when/decision 写入审计）③ 安全执行（白名单校验 + 幂等键 + 回滚检查点 + 失败自动回滚续行备选动作） |
| **Inputs** | PlannerAgent 产出的 `RemediationPlan`（steps 携带 action_type） |
| **Outputs** | `ExecutionResult`（审批记录 / 风险评估 / 逐动作状态 / 回滚记录）+ 审计事件流 `output/audit_*.jsonl` |
| **Dependencies** | Skill: `risk_guard`、`safe_execute`；MCP: execution（模拟 K8s/配置中心）；`config/action_whitelist.yaml`；不调用 LLM（执行链路要求确定性） |
| **DecisionBoundary** | 仅执行已审批方案中的白名单动作，白名单外一律拒绝并审计留痕；审批被拒不执行任何动作；动作失败自动按检查点逆序回滚后续行备选动作 |
| **Trace** | Span `agent.ExecutorAgent` → 子 Span `skill.risk_guard` / `skill.safe_execute` → 每个动作 `execute.step_N`（CLIENT，含幂等键/状态属性）与 `execute.rollback` |

## 5. VerifierAgent（已实现，阶段 2）

| 字段 | 内容 |
| --- | --- |
| **Name** | `verifier_agent` / VerifierAgent |
| **Role** | 恢复验证与复盘专家 |
| **Capabilities** | ① 对比故障期与修复后指标，判定告警消除与基线回归 ② 生成复盘报告（时间线 / 根因 / 处置 / 效果 / 改进建议）③ 事故沉淀为历史案例写入知识库（同 incident_id 幂等），形成经验闭环 |
| **Inputs** | `ExecutionResult` + 全流水线上下文（含 pipeline_timeline） |
| **Outputs** | `VerificationResult`（逐指标对比明细）、`PostmortemReport`（含沉淀案例 ID） |
| **Dependencies** | Skill: `recovery_verify`、`postmortem`；MCP: monitoring / monitoring_after；RAG: KnowledgeStore（案例沉淀）；LLM: postmortem_summary 复盘叙述 |
| **DecisionBoundary** | 只做验证与复盘，不自行修复；验证不通过如实标注“效果待人工确认”并给出改进项；案例沉淀严格幂等不重复写入 |
| **Trace** | Span `agent.VerifierAgent` → 子 Span `skill.recovery_verify` / `skill.postmortem` / `llm.complete` |

---

## 协作关系（五段串行闭环）

```
AlertAgent ──Incident──► RcaAgent ──RootCause──► PlannerAgent ──Plan──►
    ExecutorAgent（风险评估→审批→安全执行）──ExecResult──► VerifierAgent
         │ 动作失败                                        │ 验证+复盘
         ▼                                                ▼
    检查点逆序自动回滚 → 备选动作续行              案例沉淀→知识库（供 RAG 检索）
```

所有 Agent 由 Orchestrator 统一装配与调度，共享同一个 Tracer（一次运行一个 trace_id），
Agent 间通过 `AgentMessage`（Pydantic 序列化）传递结构化上下文。

## 协商与反馈回路（可选增强，`--negotiation` 开启，默认关闭）

串行主线之上，Agent 间具备两条反馈协商回路（详见 ARCHITECTURE.md §6）：

```
① 证据补充反馈环
RcaAgent ──needs_more_evidence+缺失清单──► Orchestrator
    ◄──MCP 补充采集（扩展时间窗日志/变更单详情）──┘
RcaAgent 二轮分析（重试上限 max_evidence_rounds 防死循环，仍不足才转人工）

② 多方案协商决策
RcaAgent 产出置信度接近的竞争候选（差距 < candidate_gap_threshold）
    ──► PlannerAgent × N 并行独立规划 ──► 打分排序（风险×置信度×恢复时长）
    ──► 人工选择 / --auto-approve 自动选最优，落选方案记入 alternative_plans
```

两条回路的"请求-响应"均以结构化 `AgentMessage` 记入 pipeline_timeline，
并有专属 Span（`rca.evidence_request` / `rca.reanalysis` / `plan.negotiation`）
与审计事件（`evidence_request` / `evidence_supplement` / `rca_reanalysis` /
`plan_negotiation` / `plan_selection`）留痕。
