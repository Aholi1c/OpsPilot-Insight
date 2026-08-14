# Skill 开发者指南（Skill Developer Guide）

> 本文面向希望为 OpsPilot-Insight 开发、维护或贡献 Skill 的工程师，
> 定义 Skill 的契约规范、版本管理、依赖管理、测试要求与贡献流程。
> 配套文档：[SKILL_CATALOG.md](./SKILL_CATALOG.md)（现有 Skill 清单）、
> [SKILL_REGISTRY_DESIGN.md](./SKILL_REGISTRY_DESIGN.md)（Registry 架构设计）。

---

## 一、概述

### 1.1 Skill 的定位

在 OpsPilot-Insight 的分层架构中，**Skill 是可复用的原子能力单元**，是 Agent 的
"手和脚"：Agent 负责"决策"（判断当前该做什么、如何组合结果、何时降级或升级人工），
Skill 负责"执行"（把一件事做完并给出结构化结果）。所有 Skill 继承
`src/opspilot/skills/base.py` 中的 `Skill` 抽象基类，由基类统一提供：

- **Trace 集成**：`execute()` 自动创建 `skill.{name}` Span，记录版本、成败与耗时；
- **指标上报**：自动向 `MetricsCollector` 上报每次调用的成功率与时延；
- **前置校验**：按 `preconditions` 声明校验入参完整性；
- **失败策略**：按 `failure_policy`（degrade / abort）统一处理异常；
- **标准化返回**：无论成败均返回 `SkillResult`（Pydantic 模型，JSON 可序列化）。

### 1.2 Skill vs Agent 的职责边界

| 维度 | Skill | Agent |
| --- | --- | --- |
| 职责 | 单一原子能力（去重、检索、执行一个动作序列） | 编排多个 Skill、调用 LLM、做业务决策 |
| 状态 | 无状态（全部输入来自 payload + context） | 持有角色身份与决策边界（`config/agents.yaml`） |
| LLM | **禁止直接调用 LLM**（确定性、可评测） | 可调用 LLM Provider 做叙述与判断增强 |
| 失败 | 按 failure_policy 返回结构化失败 | 决定降级路径（继续 / 转人工 / 中断） |
| 测试 | 单元测试可独立覆盖 | 依赖 Skill 与消息流的集成测试 |

一条经验法则：**如果一段逻辑换一个 Agent 也能原样复用，它就应该是 Skill**。

---

## 二、快速开始：编写你的第一个 Skill

### 2.1 最小代码示例

以下示例可直接放入 `src/opspilot/skills/` 运行（以"服务健康评分"为例）：

```python
# -*- coding: utf-8 -*-
"""HealthScore Skill：根据错误率与时延给服务打健康分。"""
from __future__ import annotations

from typing import Any, Dict

from .base import Skill, SkillContext


class HealthScoreSkill(Skill):
    name = "health_score"
    version = "1.0.0"
    description = "按错误率与 P99 时延计算服务健康分（0-100）"
    input_schema = {
        "services": "待评分的服务名列表",
    }
    output_schema = {
        "scores": "服务名 -> 健康分（0-100）",
        "unhealthy": "健康分低于 60 的服务列表",
    }
    preconditions = ["services"]     # payload 必须包含的键
    failure_policy = "degrade"       # 评分是增强能力，失败不阻断主流程

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        monitoring = context.adapters["monitoring"]   # 声明依赖 MCP monitoring 适配器
        scores: Dict[str, float] = {}
        for service in payload["services"]:
            metrics = monitoring.query_metrics(service)
            scores[service] = self._score(metrics)
        unhealthy = sorted(s for s, v in scores.items() if v < 60)
        context.logger.info(f"    健康评分完成: {len(scores)} 个服务", unhealthy=len(unhealthy))
        return {"scores": scores, "unhealthy": unhealthy}
```

三步接入系统：

1. 在 `src/opspilot/skills/__init__.py` 导出 `HealthScoreSkill`；
2. 在 `src/opspilot/orchestrator.py` 的 skills 注册表中实例化（`{s.name: s}` 字典）；
3. 在 `config/agents.yaml` 中把 `health_score` 加入目标 Agent 的 `skills` 列表。

### 2.2 元数据字段的含义与约束

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `name` | `str` | 全局唯一，snake_case；作为注册表键与 Span 名（`skill.{name}`），发布后**不可变更** |
| `version` | `str` | Semantic Versioning（见第四章），随每次行为变更递增 |
| `description` | `str` | 一句话说明能力，供 Catalog 与 Registry 展示 |
| `input_schema` | `Dict[str, str]` | 参数名 → 中文说明的轻量 schema（后续演进为 JSON Schema，见 Registry 设计） |
| `output_schema` | `Dict[str, str]` | 输出键 → 说明；`run()` 返回值的键**必须是其子集或全集** |
| `preconditions` | `List[str]` | payload 必需键列表，基类在 `run()` 前自动校验，缺失即失败 |
| `failure_policy` | `str` | `degrade`（返回 `success=False` 的 SkillResult，调用方决定降级）或 `abort`（异常上抛，由 Orchestrator 兜底） |

**failure_policy 选择原则**：该 Skill 失败后流水线是否还有意义。入口/关键路径
Skill（如 `alert_fusion`、`safe_execute`）选 `abort`；增强类 Skill
（如 RAG 检索、健康评分）选 `degrade`。

### 2.3 execute() 与 run() 的分工

开发者**只实现 `run()`，永远不要覆写 `execute()`**：

- `run(payload, context)` 返回 `Dict[str, Any]`（结构化输出，键对齐 `output_schema`）；
- 异常处理：`run()` 内不要吞掉异常做静默降级——直接 `raise`，让基类按
  `failure_policy` 统一处理，这样 Trace Span 的 `ERROR` 状态、指标失败计数、
  日志中的失败策略字段才能保持一致；
- Trace 集成：基类已为整个 Skill 包了 Span；如需更细粒度（如逐动作），
  在 `run()` 内用 `context.tracer.start_span(...)` 开子 Span（参考
  `safe_execute.py` 的逐动作 Span）；
- 审计：安全敏感操作必须写 `context.audit`（若非 None），事件需携带 trace_id。

---

## 三、Skill 契约规范

### 3.1 八大必选要素

每个 Skill 发布前，必须在代码元数据与 `SKILL_CATALOG.md` 中完整声明以下八项。
**缺任何一项不予合入**（见第七章 Review 标准）：

| # | 要素 | 承载位置 | 规范要求 |
| --- | --- | --- | --- |
| 1 | **输入/输出 Schema** | `input_schema` / `output_schema` + Catalog | 每个键必须有说明；输出键与 `run()` 实际返回一致 |
| 2 | **调用条件** | `preconditions` + Catalog "调用条件"行 | 声明必需入参与业务前置（如"审批已通过"）及预期调用方 Agent |
| 3 | **依赖工具** | `run()` 内对 `context.adapters` / `context.extras` 的访问 + Catalog | 显式列出依赖的 MCP 适配器键名与配置文件（见第五章） |
| 4 | **失败处理** | `failure_policy` + Catalog | 声明策略并写明调用方的降级路径（"失败后会发生什么"） |
| 5 | **安全边界** | Catalog "权限与安全"行 | 声明只读/写操作、白名单约束、审计留痕点 |
| 6 | **复用价值** | Catalog "复用价值"行 | 说明脱离当前场景后哪些部分可原样复用、哪些需替换 |
| 7 | **验证方式** | `tests/` 用例 + Golden Dataset 覆盖 | 声明如何证明该 Skill 行为正确（见第六章） |
| 8 | **版本** | `version` 属性 + Catalog | SemVer，且与 Catalog 记载一致 |

### 3.2 在 SKILL_CATALOG.md 中注册

新 Skill 合入时，必须在 `SKILL_CATALOG.md` 追加一个与现有 9 个 Skill 相同格式的
表格小节（字段：名称/类型/使用场景/输入参数/输出结果/调用条件/依赖工具/失败处理/
权限与安全/复用价值/版本），并标注实现状态。Catalog 是 Skill 的**唯一权威契约文档**，
代码与 Catalog 不一致时以 Catalog 评审结论为准、代码必须修正。

---

## 四、版本管理与兼容性

### 4.1 Semantic Versioning 规则

Skill 版本遵循 [SemVer 2.0.0](https://semver.org/lang/zh-CN/)：`MAJOR.MINOR.PATCH`。

| 位 | 何时递增 | 示例 |
| --- | --- | --- |
| **MAJOR** | Breaking Change：删除/重命名输入输出键、改变键语义或类型、preconditions 新增必需键、failure_policy 从 degrade 改为 abort | `alerts` 参数改名为 `raw_alerts`；`groups[]` 元素结构变更 |
| **MINOR** | 向后兼容的能力新增：新增**可选**输入参数、新增输出键、放宽 preconditions、failure_policy 从 abort 改为 degrade | 输出新增 `confidence` 字段；`top_k` 增加默认值 |
| **PATCH** | 行为不变的修复与优化：bug 修复、性能优化、日志/注释调整 | 修复严重度排序在未知等级时的越界 |

版本号体现在三处且必须一致：类属性 `version`、`SkillResult.skill_version`
（基类自动写入）、`SKILL_CATALOG.md` 的版本行。Trace Span 也自动携带
`skill.version` 属性，因此**任何一次运行产物都可追溯到确切的 Skill 版本**。

### 4.2 Breaking Change 处理流程

1. **提案**：在 PR 描述中标注 `[BREAKING]`，说明变更动机、受影响的调用方
   （检索 `config/agents.yaml` 与各 Agent 源码中的引用点）；
2. **兼容期**：优先考虑"新增而非修改"——新键与旧键并存一个 MINOR 周期，
   旧键标注 deprecated；确实无法并存时才走 MAJOR；
3. **迁移**：MAJOR 变更必须附迁移指南（模板见 4.4）并同步更新所有调用方
   Agent、Catalog、相关测试与 Golden Dataset 期望值；
4. **回归门禁**：合入前必须通过 `scripts/replay_eval.py` 三场景回放评测，
   证明流水线端到端行为符合预期。

### 4.3 向后兼容的保证范围

**保证**（同一 MAJOR 内不变）：

- `name` 与已发布输入/输出键的名称、类型、语义；
- `preconditions` 不新增必需键；
- `failure_policy` 不从 degrade 收紧为 abort；
- `SkillResult` 封装结构（由 `models.py` 统一保证）。

**不保证**（不视为 Breaking）：

- 输出中列表的排序细节（除非 Catalog 明确承诺，如 alert_fusion 按严重度排序）；
- 日志文案、Span 内部子结构、耗时数值；
- `degrade` 失败时 `error` 字符串的具体措辞。

### 4.4 版本升级迁移指南模板

MAJOR 升级时在 PR 中附带以下内容（可归档至 `docs/migrations/`）：

```markdown
# {skill_name} v{old} → v{new} 迁移指南
## 变更摘要
- [BREAKING] 输入参数 `services` 重命名为 `affected_services`
## 受影响调用方
- AlertAgent（config/agents.yaml: alert_agent.skills）
## 迁移步骤
1. 调用处 payload 键改名；2. 更新对应测试断言；3. 重跑 replay_eval 确认三场景通过
## 回滚方式
- 恢复上一版本类实现（Registry 场景下按版本号回退安装，见 SKILL_REGISTRY_DESIGN.md）
```

---

## 五、依赖管理

### 5.1 对外部工具（MCP 适配器）的依赖声明

Skill 通过 `context.adapters[key]` 访问外部工具，可用键由
`src/opspilot/mcp/mock_adapters.py::build_adapters()` 装配，当前为：
`monitoring`、`monitoring_after`、`logging`、`tracing`、`change`、`execution`。
规范要求：

- **在 run() 开头一次性取出全部依赖适配器**，缺失时立即 `KeyError` 失败，
  而不是在深层逻辑中隐式访问——让依赖在 Trace 中第一时间暴露；
- 依赖必须同步登记在 Catalog 的"依赖工具"行（含配置文件依赖，如
  `safe_execute` 依赖 `config/action_whitelist.yaml`）；
- 非适配器类依赖（RAG 检索器、知识库、审批回调）统一走 `context.extras`，
  且必须做 `None` 判断并按 degrade 语义处理（参考 `case_retrieval`）。

### 5.2 Skill 间依赖的 DAG 管理

Skill 之间**不允许直接互相调用**——所有编排关系由 Agent 与 Orchestrator 承载，
Skill 间只存在**数据依赖**（下游 Skill 的输入来自上游 Skill 的输出）。当前 DAG：

```
alert_fusion ──> impact_mapping ──> log_trace_rca ──> (runbook_rag / case_retrieval)
                                          │                     │
                                          └──> risk_guard ──> safe_execute ──> recovery_verify ──> postmortem
```

该 DAG 由五段流水线的阶段顺序保证无环。新增 Skill 时需在 PR 中声明其在
DAG 中的位置（上游输入来源、下游消费方），禁止引入反向数据依赖
（如执行类 Skill 的输出回流给诊断类 Skill 作为必需输入）。

### 5.3 依赖冲突的检测与解决

当前进程内注册表（`{s.name: s}` 字典）的冲突形态与对策：

| 冲突类型 | 检测方式 | 解决策略 |
| --- | --- | --- |
| **同名 Skill 重复注册** | 注册表按 name 去重，后注册者静默覆盖——CI 中应断言注册数量与预期一致 | 命名冲突一律改名（name 全局唯一）；同能力多实现走版本升级而非并存 |
| **输入/输出键语义冲突**（上游输出键与下游预期不一致） | preconditions 校验 + 集成测试（`tests/test_e2e.py`）在装配期暴露 | 以下游 `preconditions` 为契约锚点，上游按 SemVer 规则演进 |
| **适配器键缺失/不匹配** | `run()` 开头显式取依赖，KeyError 即刻失败并留 Trace | 适配器键名视为公共契约，新增数据源时扩展 `build_adapters()` 而非改键名 |
| **配置文件版本漂移**（如白名单新增动作类型） | `risk_guard` 对未知动作类型按 high 风险默认拒绝 | 配置变更与 Skill 版本变更绑定在同一 PR，回放评测作为回归门禁 |

多版本共存（同一 Skill 的 v1/v2 并行）不在进程内注册表的支持范围，
由 Registry 层解决（见 [SKILL_REGISTRY_DESIGN.md](./SKILL_REGISTRY_DESIGN.md) 第二、三章）。

---

## 六、测试要求

### 6.1 单元测试（必选）

- 位置：`tests/` 下按能力主题组织（现有 `test_pipeline_safety.py`、`test_evaluation.py` 为范例）；
- **Mock 全部外部依赖**：适配器用最小假实现或场景 JSON 数据（`examples/scenarios/`），
  禁止单元测试访问网络或真实资源；
- 必须覆盖四类用例：正常路径、preconditions 缺失、依赖适配器异常时的
  failure_policy 行为（degrade 返回 `success=False` / abort 上抛）、边界输入
  （空列表、未知枚举值等）；
- 断言 `SkillResult` 的结构化字段而非日志文案。

### 6.2 集成测试（必选）

- 通过 `tests/test_e2e.py` 模式验证：Skill 装配进 Orchestrator 后，
  三个内置场景（container_oom / db_pool_exhaustion / network_latency）
  端到端可跑通，产出报告字段完整；
- 验证 Trace 中出现 `skill.{name}` Span 且属性（版本、成败）正确；
- 涉及执行/审计的 Skill 需验证 `output/audit_*.jsonl` 事件完整性。

### 6.3 评测要求（Golden Dataset 覆盖）

- 影响流水线决策产出的 Skill（诊断/规划/执行/验证类），其行为期望必须体现在
  `data/golden/golden_dataset.jsonl` 的期望字段中；
- 合入前运行 `python scripts/replay_eval.py`，三场景评测分数不得低于基线
  （评测报告落盘 `output/`，作为 PR 附件）；
- 纯增强类 Skill（RAG 检索等）至少保证评测分数不回退。

---

## 七、贡献指南

### 7.1 PR 流程

1. Fork / 分支命名：`skill/{name}-{简述}`（如 `skill/health-score-init`）；
2. 一个 PR 只做一件事：新增一个 Skill，或对一个 Skill 做一次版本演进；
3. PR 描述必须包含：契约八要素自查表、版本号与 SemVer 依据、DAG 位置声明、
   回放评测结果截图或报告路径；
4. Review 通过 + CI（pytest 全量 + replay_eval）绿灯后合入。

### 7.2 Review 标准

- **契约完整性**：八要素齐备，Catalog 小节已同步；
- **边界纪律**：不调用 LLM、不覆写 `execute()`、不吞异常、不引入 Skill 间直接调用；
- **确定性**：相同输入产生相同输出（时间戳等非确定字段除外并在 Catalog 说明）；
- **可观测**：关键内部步骤有日志，安全敏感操作有审计事件。

### 7.3 代码风格与文档要求

- 与现有代码一致：文件头 `# -*- coding: utf-8 -*-` + 中文模块 docstring、
  `from __future__ import annotations`、类型标注完整、中文注释解释"为什么"；
- 命名：类 `XxxSkill`（PascalCase），`name` 属性 snake_case 且与文件名一致；
- 文档：Catalog 小节 + （MAJOR 变更时）迁移指南。

### 7.4 安全审查清单

执行类 / 写操作 Skill 额外过以下清单，任一不满足即拒绝合入：

- [ ] 所有变更动作经过 `config/action_whitelist.yaml` 白名单校验，白名单外默认拒绝；
- [ ] 动作具备幂等键（重复执行可安全跳过）；
- [ ] 每个动作执行前创建回滚检查点，失败时可逆序回滚；
- [ ] 白名单校验/审批/检查点/执行/回滚全部写入审计事件流，且携带 trace_id；
- [ ] medium 及以上风险路径存在人工审批交互点，不可被 Skill 内部绕过；
- [ ] 不在日志/审计/输出中泄露密钥、Token 等敏感信息。
