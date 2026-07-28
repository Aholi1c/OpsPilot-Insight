# -*- coding: utf-8 -*-
"""MockProvider：基于规则/模板的确定性 LLM 模拟实现（默认 Provider）。

设计要点：
- Agent 在 prompt 首行携带 [TASK=xxx] 标记，声明本次调用的任务类型；
- Mock 根据 prompt 中的场景特征关键词（告警名/报错样式/场景名）识别故障类别；
- 输出为可信的中文运维结论，保证三个内置场景的演示效果与可复现性。
"""
from __future__ import annotations

import re

from .base import LLMProvider

# 故障类别 -> 特征关键词（按优先级排列，命中即归类）
_CATEGORY_KEYWORDS = {
    "db_pool_exhaustion": [
        "db_pool_exhaustion", "DBConnectionPoolExhausted", "HikariPool",
        "Connection is not available", "连接池",
    ],
    "container_oom": [
        "container_oom", "OOMKilled", "OutOfMemoryError", "oom-killer",
        "CrashLoopBackOff", "内存",
    ],
    "network_latency": [
        "network_latency", "UpstreamTimeout", "upstream timed out",
        "HighP99Latency", "延迟",
    ],
}

# (task, category) -> 确定性输出模板
_TEMPLATES = {
    ("incident_summary", "db_pool_exhaustion"):
        "order-service 数据库连接池耗尽，订单查询/下单接口大面积超时报错，"
        "并经 api-gateway 以 504 形式扩散至用户侧，属 P1 级业务受损事件。",
    ("incident_summary", "container_oom"):
        "payment-service 容器内存持续增长触发 OOMKilled 并进入 CrashLoopBackOff，"
        "支付成功率下降，用户侧出现支付失败与重试，属 P1 级业务受损事件。",
    ("incident_summary", "network_latency"):
        "api-gateway 调用 user-service 的链路 P99 延迟由 80ms 恶化至 1.5s 以上，"
        "网关出现上游读超时，登录与个人中心页面明显变慢，属 P2 级体验受损事件。",
    ("rca_analysis", "db_pool_exhaustion"):
        "综合日志、指标、链路与变更四个维度：HikariPool 连接获取超时报错持续出现，"
        "连接池活跃连接数在发布后单调爬升至上限且不回落（典型泄漏曲线），"
        "慢链路集中在 getConnection 阶段；时间线与订单服务发布变更高度吻合，"
        "判定为该变更引入的连接泄漏导致连接池耗尽，建议优先回滚止血。",
    ("rca_analysis", "container_oom"):
        "综合分析：JVM 抛出 OutOfMemoryError、内核 oom-killer 记录与容器工作集内存"
        "逼近 limit 的曲线相互印证；内存开始爬升的时间点与支付服务开启本地缓存的"
        "变更时间一致，判定为缓存上限配置错误导致内存膨胀触发 OOM，建议回滚该变更。",
    ("rca_analysis", "network_latency"):
        "综合分析：链路数据显示 api-gateway 客户端侧耗时 1.5s 而 user-service 服务端"
        "处理仅 40ms 左右，耗时差集中在网络传输段；服务端指标平稳、无错误日志，"
        "且延迟恶化时间点与可用区 B 网络 ACL 策略变更吻合，判定为网络链路问题，"
        "建议回滚该网络变更并联动网络团队核查。",
    ("plan_narrative", "db_pool_exhaustion"):
        "建议先回滚可疑发布以止血，同时临时调大连接池上限缓解排队，再滚动重启释放"
        "已泄漏连接；全程观察错误率与池占用指标，30 分钟未恢复则升级人工介入。",
    ("plan_narrative", "container_oom"):
        "建议回滚引入缓存配置错误的发布，并临时上调内存 limit 提供缓冲，重启后采集"
        "heap dump 留存证据；观察内存曲线与重启次数，确认不再触发 OOMKilled。",
    ("plan_narrative", "network_latency"):
        "建议回滚可用区 B 的网络 ACL 变更，并将网关流量临时切换至可用区 A 兜底；"
        "同步网络团队核查专线质量，观察 P99 延迟回落至 100ms 内后再恢复默认路由。",
    ("postmortem_summary", "db_pool_exhaustion"):
        "本次事故由订单服务发布引入的连接泄漏导致连接池耗尽，处置链路为回滚止血、"
        "临时扩容连接池、滚动重启释放存量连接，修复后错误率与池占用均回归基线；"
        "后续需在发布流水线增加连接泄漏检查与连接池水位分级告警，缩短同类问题发现时间。",
    ("postmortem_summary", "container_oom"):
        "本次事故由支付服务本地缓存配置错误引发内存膨胀至 OOMKilled，处置链路为"
        "回滚变更、临时上调内存 limit、采集 heap dump 留证，修复后内存曲线平稳、"
        "支付成功率恢复；后续需将内存 limit 变更纳入容量评审并配置内存比例预警。",
    ("postmortem_summary", "network_latency"):
        "本次事故由可用区 B 网络 ACL 变更导致跨区链路延迟劣化，首选回滚动作因"
        "基线策略锁定执行失败，系统自动回滚检查点后由备选切流动作接管，延迟与超时"
        "均回归基线；后续需将网络变更纳入管控平台强制灰度，并预先核查执行权限。",
}

_FALLBACK = (
    "（Mock 输出）当前上下文未命中内置场景规则，建议人工复核：请结合日志、指标、"
    "链路与变更记录进一步确认根因后再制定修复方案。"
)


class MockProvider(LLMProvider):
    """规则/模板驱动的确定性 Mock LLM。"""

    provider_name = "mock"

    def complete(self, prompt: str, **kwargs) -> str:
        task = self._extract_task(prompt)
        category = self._detect_category(prompt)
        return _TEMPLATES.get((task, category), _FALLBACK)

    @staticmethod
    def _extract_task(prompt: str) -> str:
        """解析 prompt 中的 [TASK=xxx] 标记。"""
        match = re.search(r"\[TASK=([a-z_]+)\]", prompt)
        return match.group(1) if match else "unknown"

    @staticmethod
    def _detect_category(prompt: str) -> str:
        """按关键词计分识别故障类别，返回得分最高者。"""
        best_category, best_score = "unknown", 0
        for category, keywords in _CATEGORY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in prompt)
            if score > best_score:
                best_category, best_score = category, score
        return best_category
