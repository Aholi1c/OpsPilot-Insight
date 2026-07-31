# Skill Registry 架构设计（Skill Registry Design）

> 本文回答"Skill 生态如何扩展"：当 Skill 数量从 9 个增长到数十上百个、
> 贡献者从核心团队扩展到第三方时，如何做发现、安装、版本管理与质量管控。
> 当前代码中的进程内注册表（`orchestrator.py` 的 `{s.name: s}` 字典）是本设计的
> Local Registry 最小实现；本文描述其向完整 Registry 演进的路径。
> 配套文档：[SKILL_DEVELOPER_GUIDE.md](./SKILL_DEVELOPER_GUIDE.md)、
> [AGENTTEAMS_MAPPING.md](./AGENTTEAMS_MAPPING.md)。

---

## 一、设计目标

1. **发现与安装**：按名称/类型/关键词检索 Skill，一条命令安装到本地环境并
   自动接入 Orchestrator 注册表；
2. **版本管理**：同一 Skill 多版本共存、按 SemVer 约束解析、支持锁定与回滚；
3. **质量管控**：第三方贡献的 Skill 必须通过自动化门禁 + 人工 Review 才能进入
   共享层，安全敏感 Skill 施加更严格的审查；
4. **热加载与动态注册**：新增/升级 Skill 不重启长驻服务，注册表变更原子生效；
5. **生态兼容**：Skill 元数据可无损映射为 AgentTeams 工具（Tool/MCP）定义，
   一次开发、两端发布。

**非目标**：Registry 不承载 Skill 的编排逻辑（DAG 由 Agent/Orchestrator 负责），
也不做运行时沙箱（执行安全由白名单/审批/审计四件套保证）。

---

## 二、架构概览

### 2.1 三层 Registry

```
┌─────────────────────────────────────────────────────┐
│  Public Registry（公共层）                            │
│  面向社区的共享仓库；发布需全量质量门控 + 安全审查        │
└──────────────────────△──────────────────────────────┘
                       │ promote（晋升）
┌──────────────────────┴──────────────────────────────┐
│  Team Registry（团队层）                              │
│  组织内私有仓库（Git repo / OSS bucket）；             │
│  CI 门禁 + 团队 Review；沉淀组织专属运维能力            │
└──────────────────────△──────────────────────────────┘
                       │ install / pin
┌──────────────────────┴──────────────────────────────┐
│  Local Registry（本地层）                             │
│  进程内 {name: skill} 注册表 + skills.lock 锁文件；    │
│  当前实现：orchestrator.py 启动时静态装配 9 个 Skill    │
└─────────────────────────────────────────────────────┘
```

解析顺序 Local → Team → Public：本地已安装版本优先，保证离线可用
（与项目"默认零外部依赖"的原则一致）。

### 2.2 元数据格式（JSON Schema）

每个发布单元附带 `skill.json`，字段与 `Skill` 基类属性一一对应并做机器可校验扩展。
这也是开发者指南中 `input_schema` 从"轻量 dict"演进为 JSON Schema 的落点：

```json
{
  "name": "health_score",
  "version": "1.0.0",
  "description": "按错误率与 P99 时延计算服务健康分（0-100）",
  "entry_point": "opspilot_skills.health_score:HealthScoreSkill",
  "input_schema": {
    "type": "object",
    "properties": {
      "services": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["services"]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "scores": {"type": "object"},
      "unhealthy": {"type": "array", "items": {"type": "string"}}
    }
  },
  "failure_policy": "degrade",
  "dependencies": {
    "adapters": ["monitoring"],
    "configs": [],
    "python": ">=3.10"
  },
  "security": {"access": "read_only", "audit_events": []},
  "compat": {"opspilot_core": ">=2.0,<3.0"},
  "checksum": "sha256:..."
}
```

关键设计点：

- `entry_point` 采用 `module:Class` 形式，加载器 `importlib` 动态导入后校验
  其为 `Skill` 子类且元数据与 `skill.json` 一致（双重声明防篡改）；
- `dependencies.adapters` 显式声明 MCP 适配器键（monitoring / logging / tracing /
  change / execution / monitoring_after），装配期即可做依赖完备性检查，
  取代当前 `run()` 内 KeyError 的运行时暴露；
- `checksum` 用于安装完整性校验与审计追溯。

### 2.3 安装与加载流程

```
opspilot skill install health_score@^1.0
  1. 解析：按 SemVer 区间在 Local→Team→Public 逐层解析最高兼容版本
  2. 校验：checksum 校验 + skill.json 与代码元数据一致性校验
  3. 依赖检查：adapters/configs/compat 是否被当前环境满足，冲突即拒绝安装
  4. 落盘：写入本地 skills/ 目录，更新 skills.lock（name → 精确版本 + checksum）
  5. 注册：加载器实例化并写入注册表（见下）
```

**热加载与动态注册**：注册表从静态字典演进为 `SkillRegistry` 类，
内部维护 `{name: {version: skill_instance}}` 两级映射；更新采用
copy-on-write（新表构建完成后原子替换引用），执行中的流水线继续使用旧引用，
新流水线拿到新表——无需停机，也不会出现半更新状态。Agent 侧按
`config/agents.yaml` 的 `skills` 声明解析，可写 `health_score@^1.0`
表达版本约束，缺省取已安装最高版本。

---

## 三、版本发布机制

### 3.1 发布流程

```
开发 ──> 测试 ──> Review ──> 发布
 │        │         │          │
 分支      pytest    契约八要素  打 tag（skill/{name}/v{x.y.z}）
 skill/*  replay_   审查 +      生成 skill.json + checksum
          eval 门禁  安全清单    推送 Team/Public Registry
```

- 版本号必须符合开发者指南第四章的 SemVer 规则，CI 自动比对
  上一版本的 schema diff：检测到输入/输出键删除或类型变更而 MAJOR 未递增时，
  **直接拒绝发布**（机器兜底人为疏漏）；
- 发布产物不可变（immutable）：同一版本号不允许覆盖重传，修复必须发新 PATCH。

### 3.2 回滚机制

- **安装级回滚**：`skills.lock` 记录精确版本，`opspilot skill rollback health_score`
  回退到 lock 历史中的上一版本，重新原子注册即完成——因发布产物不可变，
  回滚总是可复现的；
- **验证手段**：回滚后运行 `scripts/replay_eval.py` 三场景回放，
  用 Golden Dataset 确认行为回到基线（这正是自建评测引擎作为"回归门禁"的价值）；
- **运行级兜底**：即使坏版本已上线，Skill 的 failure_policy 与 Orchestrator
  分段降级保证单 Skill 故障不拖垮流水线，为回滚争取时间。

### 3.3 Deprecation 策略

1. 标记：`skill.json` 增加 `"deprecated": {"since": "1.4.0", "removal": "2.0.0",
   "replacement": "xxx"}`，加载时打 WARN 日志与 Span 属性；
2. 缓冲期：至少一个 MINOR 周期（deprecated 声明后不得立即删除）；
3. 移除：仅在 MAJOR 版本移除，且迁移指南（模板见开发者指南 4.4）必须先行发布；
4. Registry 对 deprecated Skill 停止新装推荐，但保留旧版本供已锁定环境使用。

---

## 四、质量门控

### 4.1 自动化质量检查（CI 门禁，全部通过才能进入 Review）

| 检查项 | 内容 | 工具 |
| --- | --- | --- |
| 契约完整性 | skill.json 八要素齐备、与代码元数据一致、schema 可解析 | 元数据校验器 |
| 测试覆盖 | 单测覆盖正常/前置缺失/失败策略/边界四类用例；行覆盖率 ≥ 80% | pytest + coverage |
| 回放评测 | 三场景 replay_eval 分数不低于基线 | `scripts/replay_eval.py` |
| SemVer 合规 | schema diff 与版本号递增位匹配 | 版本比对器 |
| 安全扫描 | 依赖漏洞扫描、危险调用检测（网络/子进程/文件写出白名单目录） | pip-audit + AST 静态检查 |

### 4.2 人工 Review 要求

- 所有进入 Team/Public 层的 Skill 至少 1 名 maintainer Review；
- **执行类/写操作 Skill 双人 Review**，其中一人过安全审查清单
  （白名单/幂等/回滚/审计/审批五项，见开发者指南 7.4）；
- Review 结论落在 PR 上，与版本 tag 关联，可审计追溯。

### 4.3 社区评分与反馈（Public 层）

- 使用方可对 Skill 版本提交评分与 issue，Registry 聚合展示
  下载量 / 评分 / 已知问题 / 兼容性报告；
- 运行侧回流：`MetricsCollector` 采集的成功率与时延（脱敏后）可选上报，
  形成"真实运行质量画像"，作为推荐排序信号；
- 连续两个版本成功率显著劣化的 Skill 自动进入观察名单，触发 maintainer 复查。

---

## 五、与 AgentTeams 生态的对接

### 5.1 将 Skill 发布为 AgentTeams 生态组件

如 [AGENTTEAMS_MAPPING.md](./AGENTTEAMS_MAPPING.md) 第 3.1 节所述，Skill 已具备
统一"名称/版本/输入输出/失败策略"契约，注册为 AgentTeams 工具是元数据映射问题：

| skill.json 字段 | AgentTeams 工具定义 |
| --- | --- |
| `name` / `description` | 工具名称 / 描述 |
| `input_schema`（JSON Schema） | 工具参数 schema（直接复用） |
| `output_schema` | 工具返回值说明 |
| `version` / `compat` | 工具版本与运行时兼容声明 |
| `security.access` | 工具权限等级（只读工具可自动授权，写操作工具要求平台侧审批流） |

发布命令形态：`opspilot skill publish health_score --target agentteams`，
由 Registry 生成平台工具定义并推送，Skill 实现代码零改动。

### 5.2 接口适配层设计

新增 `SkillToolAdapter` 薄层（约一个模块），职责单一：

```
AgentTeams 工具调用(JSON 参数)
  ──> SkillToolAdapter：
      1. 参数校验（input_schema）
      2. 构造 SkillContext（tracer/logger 对接 ARMS/SLS 导出端，
         adapters 指向真实 MCP Server——AgentTeams 原生支持 MCP）
      3. skill.execute(payload, context)
  ──> SkillResult 序列化为工具返回值（success/output/error/duration_ms 原样透出）
```

设计不变量：适配层**不改写任何业务语义**——失败策略、审计事件、Span 结构
在两种运行时下保持一致，因此本地回放评测的结论对平台托管运行同样有效。
这保证了 Skill 资产"一次沉淀、两端复用"：本地 Orchestrator 用于开发调试与
回归评测，AgentTeams 用于生产托管，二者共享同一 Registry 与同一套质量门禁。
