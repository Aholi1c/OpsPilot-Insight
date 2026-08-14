# Skill 清单（Skill Catalog）v2.0

> OpsPilot-Insight 的 Skill 体系：诊断类 3 个，执行/验证/复盘/RAG 类 6 个，
> 共 9 个，全部已实现。
>
> 所有 Skill 继承 `src/opspilot/skills/base.py` 的 `Skill` 基类，
> `execute()` 自动包 Trace Span（`skill.{name}`）并上报进程内指标，统一返回 `SkillResult`。

---

## 1. AlertFusion（alert_fusion）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `alert_fusion` v1.0.0 |
| **类型** | 诊断类 / 数据收敛 |
| **使用场景** | 告警风暴时对原始告警流去重、聚合，收敛为事件级视图 |
| **输入参数** | `alerts: List[dict]`（AlertManager 格式原始告警） |
| **输出结果** | `deduped_count`、`groups[]`（alertname/service/severity/count/first_starts_at/summary）、`affected_services[]`、`max_severity`、`first_alert_at` |
| **调用条件** | 存在至少 1 条原始告警；由 AlertAgent 在事件接入时调用 |
| **依赖工具** | 无外部依赖（纯内存计算，fingerprint 去重 + 二元组分组） |
| **失败处理** | `failure_policy=abort`：告警解析失败时上抛，由 Orchestrator 降级为最小事件对象 |
| **权限与安全** | 只读，不触碰任何生产资源 |
| **复用价值** | 高：任何告警源（Prometheus/云监控/自建）格式归一后均可复用 |
| **版本** | 1.0.0 |

## 2. ImpactMapping（impact_mapping）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `impact_mapping` v1.0.0 |
| **类型** | 诊断类 / 影响面评估 |
| **使用场景** | 确认受影响服务后，评估故障爆炸半径与用户可感知程度 |
| **输入参数** | `services: List[str]`（来自 AlertFusion 输出的服务列表） |
| **输出结果** | `blast_radius[]`（含上游传播）、`blast_radius_size`、`user_impact`（用户可感知判定）、`entry_services[]` |
| **调用条件** | AlertFusion 已识别出受影响服务；由 AlertAgent 串行调用 |
| **依赖工具** | MCP tracing 适配器（从链路数据推导服务依赖拓扑，反向 BFS） |
| **失败处理** | `failure_policy=degrade`：失败时返回 `success=False`，事件仅缺影响面字段，流水线继续 |
| **权限与安全** | 只读（仅查询链路拓扑） |
| **复用价值** | 高：接入真实拓扑源（CMDB/服务网格）后逻辑不变 |
| **版本** | 1.0.0 |

## 3. LogTraceRca（log_trace_rca）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `log_trace_rca` v1.0.0 |
| **类型** | 诊断类 / 多维根因分析 |
| **使用场景** | 事件确认后，关联日志+链路+指标+变更四维信号定位根因 |
| **输入参数** | `affected_services: List[str]`、`first_alert_at: str`（首告警时间） |
| **输出结果** | `candidates[]`（category/hypothesis/service/confidence/evidences[source,strength,description,details]/related_change_id）+ `signals`（四维原始信号摘要）；证据强度三档 strong/weak/missing；候选 ≥2（主假设 + 备选） |
| **调用条件** | Incident 含受影响服务与首告警时间（preconditions 校验）；由 RcaAgent 调用 |
| **依赖工具** | MCP logging / monitoring / tracing / change 四个适配器 |
| **失败处理** | `failure_policy=abort`：上抛后 Orchestrator 降级为"无根因结论，转人工排查" |
| **权限与安全** | 只读（仅查询观测数据与变更记录） |
| **复用价值** | 高：故障模式库（关键词/指标特征/慢调用特征）可持续沉淀扩展 |
| **版本** | 1.0.0 |

---

## 4. SafeExecute（safe_execute）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `safe_execute` v1.0.0 |
| **类型** | 执行类 / 安全执行框架 |
| **使用场景** | 审批通过后按方案逐步执行修复动作（Mock 执行，不触碰真实资源） |
| **输入参数** | `incident_id: str`、`plan: dict`（RemediationPlan 序列化，steps 携带 `action_type`） |
| **输出结果** | `status`（success / success_with_rollback / failed / rejected）、`executed`、`actions[]`（含幂等键/检查点/状态/备选标记）、`rollbacks[]`、`rollback_performed` |
| **调用条件** | 审批已通过（ExecutorAgent 保证）；preconditions 校验 incident_id + plan |
| **依赖工具** | MCP execution 适配器（模拟 K8s/配置中心）、`config/action_whitelist.yaml`、审计事件流 |
| **安全边界** | ① 白名单：白名单外动作类型一律拒绝；② 幂等键：`sha256(incident_id\|action)` 命中则跳过；③ 回滚检查点：每个动作执行前快照，失败时逆序自动回滚并续行备选动作 |
| **失败处理** | `failure_policy=abort`：框架自身异常上抛，Orchestrator 降级为“无执行结果，转人工”；单动作失败不中断，触发自动回滚后续行 |
| **权限与安全** | 全部关键节点（whitelist_check / checkpoint / execute / rollback）写入 `output/audit_*.jsonl` 审计留痕，均带 trace_id |
| **复用价值** | 高：换真实执行后端（K8s/Ansible）只需替换 execution 适配器，安全边界逻辑不变 |
| **版本** | 1.0.0 |

## 5. RiskGuard（risk_guard）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `risk_guard` v1.0.0 |
| **类型** | 守护类 / 执行前风险评估 |
| **使用场景** | 执行前评估方案风险（动作风险等级 / 影响半径 / 是否需人工审批） |
| **输入参数** | `plan: dict`、`incident: dict` |
| **输出结果** | `overall_risk`（low/medium/high，取白名单 default_risk 与方案 risk_level 的最高值）、`needs_approval`（medium/high 为 true）、`action_risks[]`、`impact_radius`、`unknown_action_types[]` |
| **调用条件** | ExecutorAgent 在审批交互前调用 |
| **依赖工具** | `config/action_whitelist.yaml`（各动作类型 default_risk） |
| **失败处理** | `failure_policy=degrade`：评估失败时 ExecutorAgent 按最保守策略处理（视为高风险、强制审批） |
| **权限与安全** | 只读；未知动作类型一律按 high 风险评定（默认拒绝倾向） |
| **复用价值** | 高：风险规则可持续扩展（频控/时间窗/变更冻结期等） |
| **版本** | 1.0.0 |

## 6. RecoveryVerify（recovery_verify）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `recovery_verify` v1.0.0 |
| **类型** | 验证类 / 指标对比 |
| **使用场景** | 修复执行后验证告警条件是否消除、关键指标是否回归基线 |
| **输入参数** | `incident: dict`（受影响服务清单） |
| **输出结果** | `passed`、`alerts_cleared`、`checks[]`（逐指标：故障峰值 / 基线 / 修复后尾值 / 是否恢复）、`summary` |
| **调用条件** | VerifierAgent 在执行阶段后调用 |
| **依赖工具** | MCP monitoring（故障期指标 metrics.json）+ monitoring_after（修复后指标 metrics_after.json）两个适配器 |
| **失败处理** | `failure_policy=degrade`：验证失败时报告标记“未验证，效果待人工确认”，不阻断复盘 |
| **权限与安全** | 只读（仅查询监控指标） |
| **复用价值** | 高：对接真实 Prometheus 后判定算法（异常方向识别 + 基线回归阈值）不变 |
| **版本** | 1.0.0 |

## 7. Postmortem（postmortem）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `postmortem` v1.0.0 |
| **类型** | 复盘类 / 报告生成 |
| **使用场景** | 事件闭环后自动生成复盘（时间线/根因/处置/效果/改进建议）并组装知识库案例 |
| **输入参数** | `incident`、`root_cause`、`plan`、`execution_result`、`verification`、`pipeline_timeline` |
| **输出结果** | `timeline[]`（合并流水线事件与审计关键事件）、`root_cause`、`actions_taken[]`、`effect`、`improvements[]`、`case`（待沉淀的知识库案例文档） |
| **调用条件** | VerifierAgent 在恢复验证后调用 |
| **依赖工具** | 审计事件流（提取审批/执行失败/回滚事件入时间线） |
| **失败处理** | `failure_policy=degrade`：生成失败时报告不含复盘段落，其余字段不受影响 |
| **权限与安全** | 只读（纯文本组装，改进建议为规则生成 + LLM 叙述增强） |
| **复用价值** | 高：案例文档结构与知识库 schema 对齐，形成“处置→沉淀→检索”经验闭环 |
| **版本** | 1.0.0 |

## 8. CaseRetrieval（case_retrieval）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `case_retrieval` v1.0.0 |
| **类型** | RAG / 相似案例检索 |
| **使用场景** | 根因分析/方案规划时检索相似历史事故案例，注入 Agent 决策上下文 |
| **输入参数** | `query: str`（事件标题+概述+根因假设拼接）、`top_k: int`（默认 3） |
| **输出结果** | `cases[]`（id/title/symptoms/root_cause/resolution/duration_minutes/score）、`backend`（检索后端名） |
| **调用条件** | RcaAgent（辅助定位）与 PlannerAgent（参考当年处置）调用；知识库已装配 |
| **依赖工具** | RAG 检索器（默认 LocalRetriever BM25 零依赖，可选 ChromaRetriever）+ `data/knowledge/cases.jsonl` |
| **失败处理** | `failure_policy=degrade`：RAG 为增强项，检索失败不阻断主流程 |
| **权限与安全** | 只读；离线可用（默认后端无网络/无 Key 依赖） |
| **复用价值** | 高：案例库随 VerifierAgent 沉淀自动增长，检索效果持续提升 |
| **版本** | 1.0.0 |

## 9. RunbookRag（runbook_rag）—— 已实现

| 字段 | 内容 |
| --- | --- |
| **名称** | `runbook_rag` v1.0.0 |
| **类型** | RAG / Runbook 检索 |
| **使用场景** | 方案规划时检索标准运维手册条目，修复方案需引用 Runbook 作为合规依据 |
| **输入参数** | `query: str`（根因假设+类别）、`top_k: int`（默认 3） |
| **输出结果** | `runbooks[]`（id/title/steps/applicable_services/score）、`backend` |
| **调用条件** | PlannerAgent 在生成修复步骤前调用，命中结果写入 `plan.runbook_references` |
| **依赖工具** | RAG 检索器 + `data/knowledge/runbooks.jsonl`（13 条种子 Runbook） |
| **失败处理** | `failure_policy=degrade`：检索失败时方案不带 Runbook 引用，不阻断主流程 |
| **权限与安全** | 只读；离线可用 |
| **复用价值** | 高：Runbook 库可持续扩充，方案可追溯到具体规范条目 |
| **版本** | 1.0.0 |

---

> 白名单配置：`config/action_whitelist.yaml`（当前 10 类动作：rollback_change / scale_pool /
> rolling_restart / config_update / traffic_switch / diagnostic_capture / manual_followup /
> freeze_account / trigger_2fa / notify_team）；
> 白名单外动作类型默认拒绝，medium/high 风险方案必须经审批（`--auto-approve` 或交互式 y/n）。

---

## 版本管理与贡献

### 版本演进规则

所有 Skill 遵循 [Semantic Versioning 2.0.0](https://semver.org/lang/zh-CN/) 规范：

- **MAJOR**：Breaking Change（输入/输出键删除或语义变更、preconditions 新增必需键、failure_policy 收紧）；
- **MINOR**：向后兼容的能力新增（新增可选参数、新增输出键、放宽前置条件）；
- **PATCH**：行为不变的 bug 修复与性能优化。

版本号体现在类属性 `version`、`SkillResult.skill_version`（自动填入）以及本 Catalog 的"版本"行，
三处必须一致。详细规则与 Breaking Change 处理流程见 [Skill 开发者指南 · 第四章](./SKILL_DEVELOPER_GUIDE.md#四版本管理与兼容性)。

### 贡献新 Skill

1. 参照 [Skill 开发者指南](./SKILL_DEVELOPER_GUIDE.md) 的快速开始示例实现 Skill 并满足八大契约要素；
2. 在本 Catalog 追加对应小节（与现有 9 个 Skill 格式一致）；
3. 运行 `pytest` 全量测试 + `python scripts/replay_eval.py` 回放评测确认不回退；
4. 提交 PR 经 Review 合入（执行类 Skill 需双人 Review + 安全清单）。

详细 PR 流程、Review 标准与安全审查清单见 [Skill 开发者指南 · 第七章](./SKILL_DEVELOPER_GUIDE.md#七贡献指南)。

### Skill Registry 架构

随 Skill 数量增长与第三方贡献引入，注册表将从当前进程内字典演进为分层 Registry
（Local → Team → Public），支持版本解析、热加载、质量门控与 AgentTeams 生态发布。
完整设计见 [Skill Registry 架构设计](./SKILL_REGISTRY_DESIGN.md)。
