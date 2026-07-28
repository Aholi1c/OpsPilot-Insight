# -*- coding: utf-8 -*-
"""轻量 YAML 子集解析器（阶段 1 不允许引入 PyYAML，自研实现）。

支持 config/agents.yaml 所需的特性子集：
- 缩进表示的嵌套映射（2 空格缩进）
- 标量列表（"- item"）
- 块字面量（"key: |" 多行字符串，用于提示词模板）
- 注释行（#）与空行
- 基础标量类型转换（bool/int/float/字符串去引号）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Union


def load_yaml(path: Union[str, Path]) -> Any:
    """读取并解析 YAML 子集文件。"""
    text = Path(path).read_text(encoding="utf-8")
    return parse_yaml(text)


def parse_yaml(text: str) -> Any:
    return _Parser(text.splitlines()).parse_map(0)


def _scalar(raw: str) -> Any:
    """标量转换：去引号 / bool / null / 数字，否则原样字符串。"""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if s == "[]":
        return []  # 内联空列表
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


class _Parser:
    def __init__(self, lines: List[str]):
        self.lines = lines
        self.i = 0

    # ---- 工具方法 ----
    def _skip_blank(self) -> None:
        """跳过空行与注释行。"""
        while self.i < len(self.lines):
            stripped = self.lines[self.i].strip()
            if stripped == "" or stripped.startswith("#"):
                self.i += 1
            else:
                return

    def _indent_of(self, line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def _peek(self):
        """返回下一个有效行的 (indent, content)，不消费；结尾返回 None。"""
        j = self.i
        while j < len(self.lines):
            stripped = self.lines[j].strip()
            if stripped == "" or stripped.startswith("#"):
                j += 1
                continue
            return self._indent_of(self.lines[j]), stripped
        return None

    # ---- 各结构解析 ----
    def parse_map(self, indent: int) -> dict:
        result: dict = {}
        while True:
            self._skip_blank()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            cur = self._indent_of(line)
            if cur < indent:
                break  # 回到上层
            if cur > indent:
                raise ValueError(f"意外缩进（行 {self.i + 1}）: {line!r}")
            content = line.strip()
            if content.startswith("- "):
                raise ValueError(f"映射层级出现列表项（行 {self.i + 1}）: {line!r}")
            key, sep, rest = content.partition(":")
            if not sep:
                raise ValueError(f"非法映射行（行 {self.i + 1}）: {line!r}")
            key = key.strip()
            rest = rest.strip()
            self.i += 1
            if rest == "|":
                result[key] = self._parse_block_scalar(indent)
            elif rest == "":
                nxt = self._peek()
                if nxt is None or nxt[0] <= indent:
                    result[key] = None  # 空值
                elif nxt[1].startswith("- "):
                    result[key] = self._parse_list(nxt[0])
                else:
                    result[key] = self.parse_map(nxt[0])
            else:
                result[key] = _scalar(rest)
        return result

    def _parse_list(self, indent: int) -> list:
        items: list = []
        while True:
            self._skip_blank()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            cur = self._indent_of(line)
            if cur != indent or not line.strip().startswith("- "):
                break
            items.append(_scalar(line.strip()[2:]))
            self.i += 1
        return items

    def _parse_block_scalar(self, parent_indent: int) -> str:
        """解析 "key: |" 块字面量，按首个非空行缩进整体去缩进。"""
        collected: List[str] = []
        base_indent = None
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() == "":
                collected.append("")
                self.i += 1
                continue
            cur = self._indent_of(line)
            if cur <= parent_indent:
                break  # 块结束
            if base_indent is None:
                base_indent = cur
            collected.append(line[base_indent:] if cur >= base_indent else line.strip())
            self.i += 1
        # 去掉尾部空行
        while collected and collected[-1] == "":
            collected.pop()
        return "\n".join(collected)
