# OpsPilot-Insight 方案 PPT 内容大纲（共 14 页）

> 用途：本文件为制作初赛方案 PPT 的完整内容稿。每页给出「页标题 / 要点内容（可直接放入 PPT 的短句）/ 建议配图」。
> 评审权重对齐：场景价值与行业可复制性 25%（P2、P12）、多 Agent 协同与自主闭环 25%（P4、P7）、Skill 工程体系与生态复用 25%（P6、P12）、工程落地运行验证安全可审计 20%（P5、P8、P11）、开放/开源贡献 5%（P13）。差异化创新（P9、P10）横跨多个维度加分。

---

## P1 封面

**页标题**：OpsPilot-Insight —— 零人工运维场景的多 Agent 自愈系统

**要点内容**：
- 副标题：内建可观测 · 评测 · 成本治理基座的多 Agent 自主协同闭环
- 赛道：新智基座丨AgentInfra——复杂任务多 Agent 自主协同
- 团队名 / 成员 / 日期（自行填写）

**建议配图**：深色科技风背景 + 项目 Logo 或看板首页截图（page1_home.png）作为底图。

---

## P2 问题与场景价值

**页标题**：告警处置仍靠人工：MTTR 高、经验流失、成本失控

**要点内容**：
- 行业典型现状（注明为行业典型数据，非本项目实测）：告警风暴下人工筛选定位根因，MTTR 常达数小时；7×24 值班人力成本高；处置经验散落在个人经验中，无法复用
- 三大痛点：定位慢（多信号源人工关联）、执行险（手工操作无防护）、不沉淀（复盘流于形式）
- 场景价值：故障自愈是 SRE/AIOps 的核心场景，流程高度标准化（告警→根因→处置→验证），是多 Agent 协同的天然落地场景
- 行业可复制性：金融、电商、云服务等所有有线上系统的行业均适用

**建议配图**：左侧"人工处置流程"时间轴（告警→拉群→排查→审批→操作→观察，标注小时级），右侧"OpsPilot 自愈流程"时间轴（分钟级），形成对比。

---

## P3 方案总览：三层架构

**页标题**：五 Agent 协同闭环 × 基础设施基座 × 数据资产层

**要点内容**：
- 编排层：Orchestrator 五段串行编排（Alert→RCA→Planner→Executor→Verifier），结构化上下文传递（Pydantic AgentMessage）+ 分段降级容错
- 能力层：9 个标准化 Skill + MCP 适配器（monitoring/logging/tracing/change/execution）+ 可插拔 LLM Provider（Mock/DashScope）
- 基座层（差异化）：自研 Tracer + JSONL 结构化日志 + 审计事件流 + 指标采集 + 评测引擎 + 成本治理
- 数据资产层：RAG 知识库（13 Runbook + 11 案例）+ Golden Dataset，随运行持续增长

**建议配图**：三层架构图（Mermaid 或绘图工具重绘），顶层五个 Agent 方块横排，中层 Skill/MCP/LLM，底层可观测/评测/成本基座 + 知识库，用箭头标注数据流。

---

## P4 多 Agent 协同闭环（评审维度：多 Agent 协同与自主闭环）

**页标题**：五段流水线：从告警到复盘的全自主闭环

**要点内容**：
- AlertAgent：告警去重聚合 + 影响面分析 → 产出结构化 Incident
- RcaAgent：指标/日志/链路/变更四维信号关联 + 相似案例检索 → 根因候选 + 证据链
- PlannerAgent：Runbook 检索增强 → 处置方案（含风险等级 + 回滚计划 + 白名单动作类型）
- ExecutorAgent：风险评估 → 审批 → 安全执行（失败自动回滚 + 备选动作）
- VerifierAgent：恢复前后指标对比验证 + 复盘报告 + 案例沉淀回写知识库
- 上下文传递机制：全部通过 Pydantic 强类型模型（Incident/Plan/Execution/Verification）逐段传递，无自由文本黑盒；任一段失败记录降级说明并继续产出部分报告

**建议配图**：五段流水线横向流程图，每个 Agent 下方标注其调用的 Skill；段间箭头上标注传递的结构化对象名。

---

## P5 安全边界设计（评审维度：安全可审计 + 个性化核验点）

**页标题**：高风险动作四重防护：白名单 · 审批 · 回滚 · 审计

**要点内容**：
- 白名单校验：config/action_whitelist.yaml 定义 7 类允许动作，白名单外一律拒绝并记审计
- 幂等键：sha256(incident_id + 动作内容)，同一事件重复动作直接跳过
- 人工审批：medium/high 风险方案必须审批才执行；审批人/时间/决定/理由全部写入审计流
- 回滚检查点：每个动作执行前记录状态快照，失败按检查点逆序自动回滚
- 回滚演示剧本（network_latency 场景，可离线复现）：首动作执行失败 → 自动回滚检查点 → 备选动作（流量切换）成功 → 人工跟进工单
- 审计证据链：whitelist_check → approval → checkpoint → execute(failed) → rollback → checkpoint → execute(success)，全部带 trace_id 落盘 audit_*.jsonl

**建议配图**：左侧四件套图标式列表；右侧 network_latency 回滚剧本时序图（动作 1 失败 ✗ → 回滚 ⟲ → 动作 2 成功 ✓），可直接截取 run_demo.py 终端输出。

---

## P6 Skill 工程体系（评审维度：Skill 工程体系与生态复用 + 个性化核验点）

**页标题**：9 个标准化 Skill：Schema 化输入输出 + 全要素规范

**要点内容**：
- Skill 清单表（建议做成表格）：

| Skill | 职责 | 所属 Agent |
| --- | --- | --- |
| AlertFusion | 告警去重聚合 | AlertAgent |
| ImpactMapping | 影响面分析 | AlertAgent |
| LogTraceRca | 日志/链路/指标/变更根因分析 | RcaAgent |
| CaseRetrieval | 相似历史案例检索 | RcaAgent / PlannerAgent |
| RunbookRag | Runbook 知识检索 | PlannerAgent |
| RiskGuard | 风险评估与审批把关 | ExecutorAgent |
| SafeExecute | 白名单/幂等/检查点安全执行 | ExecutorAgent |
| RecoveryVerify | 恢复验证（指标对比） | VerifierAgent |
| Postmortem | 复盘生成与案例沉淀 | VerifierAgent |

- 每个 Skill 满足赛事必选项：输入/输出 Schema、调用条件、依赖工具（MCP/RAG/LLM）、失败处理策略、验证方式、复用价值说明、版本号
- 统一基类约束（skills/base.py）：Schema 校验、异常捕获、Trace span 自动埋点
- 完整规范见 docs/SKILL_CATALOG.md（v2.0）

**建议配图**：Skill 清单表格 + 一个 Skill 的规范卡片示例（截取 SKILL_CATALOG.md 中任一 Skill 的定义，展示 Schema/调用条件/失败处理/版本字段）。

---

## P7 AgentTeams 架构能力映射（个性化核验点）

**页标题**：AgentTeams 五项能力逐项映射

**要点内容**（建议做成对照表）：

| AgentTeams 能力 | 本项目实现 |
| --- | --- |
| 角色编排 | config/agents.yaml 声明 5 个 Agent 的角色、职责、提示词模板与可用 Skill |
| 任务拆解 | Orchestrator 将"故障自愈"拆解为五段子任务，各 Agent 内再拆解为 Skill 调用链 |
| 上下文传递 | Pydantic 强类型 AgentMessage（Incident/Plan/Execution/Verification）逐段传递 |
| 协同执行 | 五段流水线 + 分段降级容错；ExecutorAgent 失败触发回滚后 VerifierAgent 仍完成验证复盘 |
| 状态追踪 | 自研 Tracer 全链路 span（22 个/次运行）+ JSONL 日志 + 审计事件，全部 trace_id 关联 |

**建议配图**：五行对照表；右侧可放一张 Trace 浏览器页面截图（page2_trace_browser.png）佐证状态追踪。

---

## P8 可观测与证据沉淀（评审维度：工程落地运行验证）

**页标题**：每次运行产出五类可验证证据，全链路 trace_id 关联

**要点内容**：
- 自研轻量 Tracer：OTel 语义 span 树，覆盖 5 Agent + 全部 Skill 与执行动作，22 span/次
- JSONL 结构化日志：每行携带 trace_id/span_id，可与 Trace 精确对齐
- 审计事件流：白名单校验/审批/检查点/执行/回滚全事件落盘
- 进程内指标：LLM 调用与 token、Skill 成功率、Agent 耗时、RAG 命中率、pipeline 总耗时
- 五类落盘产物：incident_report / trace / run.log / audit / metrics，外加评测报告
- 红线应对：所有结论均有文件级证据，评审可现场离线复跑验证

**建议配图**：五类产物文件示意图（output/ 目录树）+ Trace span 树截图（看板 Trace 浏览器页面）。

---

## P9 差异化创新 ①：自动评测引擎（Demo→Production 鸿沟）

**页标题**：从 Trace 自动构建 Golden Dataset，评测形成数据飞轮

**要点内容**：
- 痛点：多 Agent 系统改一处 Prompt 全链路行为漂移，无回归手段不敢上生产
- Golden Dataset：从运行 Trace 产物自动提取构建（同 case 幂等更新），人工校准 expected.json 优先
- 五维规则评估（0-100 加权）：根因命中 0.30 / 动作类型正确 0.20 / 验证一致 0.15 / 闭环完整 0.20 / 安全合规 0.15
- LLM-as-Judge：根因质量/方案合理性/复盘质量三维评分 + 评语（默认 MockJudge 离线可跑，可切 DashScope）
- 一键回放评测：scripts/replay_eval.py，3 场景回放 + 构建 + 评估 + 与上次对比，当前 3 场景全部 100 分
- 数据飞轮：运行 → Trace → Golden Dataset → 评测 → 改进 → 再运行

**建议配图**：数据飞轮环形图（运行/Trace/Golden/评测/改进五节点循环）+ 评测报告页面截图（page3_evaluation_report.png，含五维雷达图）。

---

## P10 差异化创新 ②：成本治理

**页标题**：Token 成本三维分解 + 预算控制，让多 Agent 系统"算得清账"

**要点内容**：
- 痛点：多 Agent 系统 LLM 调用链长，成本黑盒，无法定位"钱花在哪个 Agent/Skill 上"
- 三维分解：per-Agent / per-Skill / per-Model，三个维度求和恒等于总成本（口径可审计）
- 成本模型：config/pricing.yaml 定义模型单价；DashScope 路径读取 API 真实 usage，Mock 路径按字符估算
- 预算控制：单次事故处理预算上限，超限不中断流程、记 budget_alert 审计事件并在报告与看板标红
- 跨运行趋势聚合：为容量规划与模型选型提供数据

**建议配图**：成本分析页面截图（page4_cost_analysis.png）：三维分解条形图 + 预算进度条 + 面积趋势图。

---

## P11 运行验证（评审维度：工程落地运行验证，红线应对）

**页标题**：可验证：39 个测试用例全通过，3 场景离线一键复现

**要点内容**：
- pytest 39 用例全部通过（test_e2e / test_stage2 / test_stage3），覆盖闭环、白名单、幂等、回滚、RAG、案例沉淀、评分、成本一致性、预算告警、回放评测
- 无 API Key、无网络：默认 MockProvider，3 个场景（db_pool_exhaustion / container_oom / network_latency）离线完整复现
- 一条命令验证：`python3 run_demo.py --scenario network_latency --auto-approve`（含失败回滚剧本）；`python3 scripts/replay_eval.py`（3 场景 100 分）
- Streamlit 四页看板：系统总览 / Trace 浏览器 / 评测报告 / 成本分析（Grafana 风格）
- 工程质量：核心零重依赖（主流程仅 pydantic），自研 YAML 子集解析器 / Tracer / 日志组件

**建议配图**：四页看板截图拼图（page1~page4 四张 PNG 各占一角）+ pytest 全绿终端截图。

---

## P12 复用价值与行业可复制性（评审维度：场景价值 25% + Skill 生态复用 25%）

**页标题**：标准化组件设计：换一套 MCP 适配器即可迁移新场景

**要点内容**：
- Skill 层复用：9 个 Skill 与业务解耦，Schema 化接口可直接编入其他 Agent 团队
- MCP 适配器契约：Mock 实现与真实系统（Prometheus/SLS/变更系统等）共享同一接口契约，迁移只需替换适配器
- 基座组件独立复用：Tracer / 评测引擎 / 成本治理不绑定运维场景，任何多 Agent 系统均可接入
- 知识资产可迁移：Runbook/案例采用 JSONL 标准格式，企业可导入自有知识库
- 适用行业：金融、电商、云服务、制造等所有存在线上系统运维的行业

**建议配图**：分层复用示意图：底部基座组件（可独立开源）→ 中部 Skill 库（跨场景复用）→ 顶部场景层（运维自愈 / 其他多 Agent 场景虚线框）。

---

## P13 落地计划与开源规划（评审维度：开放/开源贡献 5%）

**页标题**：复赛/决赛路线与 Apache 2.0 开源计划

**要点内容**：
- 复赛计划：接入真实 DashScope 推理与真实 Judge；扩充故障场景与 Golden Dataset 样本量；接入真实监控数据源 MCP 适配器
- 决赛计划：多事故并发编排；审批流对接 IM（钉钉/飞书）；评测门禁接入 CI
- 开源规划：以 Apache 2.0 协议开源；可观测/评测/成本组件拆分为独立包优先开源；提供 Skill 编写规范文档与贡献指南
- 社区贡献方向：多 Agent 系统评测方法论与 Golden Dataset 构建实践分享

**建议配图**：三段时间轴（初赛已完成 → 复赛 → 决赛），每段下挂 2-3 个里程碑。

---

## P14 结尾页

**页标题**：OpsPilot-Insight：让多 Agent 系统可信、可测、可算账

**要点内容**：
- 一句话总结：五 Agent 自愈闭环解决"跑得通"，内建可观测/评测/成本基座解决"敢上线"
- 三个数字回顾：5 Agent · 9 Skill · 39 测试用例全通过
- 团队信息 / 联系方式 / 代码仓库地址（自行填写）
- 致谢：阿里云 × DataWhale

**建议配图**：简洁收尾版式，可复用封面底图；下方放三个数字的大字卡片。
