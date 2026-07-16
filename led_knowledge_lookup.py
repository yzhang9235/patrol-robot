# -*- coding: utf-8 -*-
"""
led_knowledge_lookup.py
纯查表模块：根据 (厂商, 型号, 颜色, 状态) 去对应的 knowledge/<vendor>_<model>.json
里查出说明书原文描述。这里不包含任何"红色=严重故障"这类硬编码语义——
所有解释都来自 build_led_knowledge.py 从说明书里提取出的原文。

用法(在 patrol_monitor.py 或其他脚本里)：
    from led_knowledge_lookup import LedKnowledgeBase

    kb = LedKnowledgeBase(knowledge_dir="knowledge")
    kb.load("NVIDIA", "DGX A100")          # 加载某个厂商型号的规则文件
    matches = kb.lookup("NVIDIA", "DGX A100", color="red", pattern="solid")
    for m in matches:
        print(m["component"], m["description"])

如果某个station还没对应的说明书规则文件，lookup会返回空列表，
调用方应该自己决定兜底怎么处理(比如只报"检测到红色异常，暂无对应说明书解释")，
不要在这里悄悄补一个默认解释——没有依据的解释比没有解释更容易误导人。
"""

import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional


class LedKnowledgeBase:
    def __init__(self, knowledge_dir: str = "knowledge"):
        self.knowledge_dir = Path(knowledge_dir)
        self._cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _slug(vendor: str, model: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{vendor}_{model}").strip("_")

    def load(self, vendor: str, model: str) -> bool:
        """加载某个厂商型号的规则文件到缓存。返回是否加载成功。"""
        slug = self._slug(vendor, model)
        if slug in self._cache:
            return True
        path = self.knowledge_dir / f"{slug}.json"
        if not path.exists():
            return False
        with open(path, "r", encoding="utf-8") as f:
            self._cache[slug] = json.load(f)
        return True

    def lookup(self, vendor: str, model: str, color: str,
               pattern: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        查找匹配的规则。
        - 如果传了pattern(solid/blink)，优先精确匹配 color+pattern
        - 如果精确匹配没有结果，退化成只按color匹配，返回所有该颜色的规则
          (调用方可以看到"这个颜色在说明书里对应哪几种可能"，而不是被静默地
          猜中一个可能不对的)
        - 没加载过/没有这个型号的文件，返回空列表
        """
        slug = self._slug(vendor, model)
        data = self._cache.get(slug)
        if not data:
            if not self.load(vendor, model):
                return []
            data = self._cache[slug]

        rules = data.get("rules", [])
        color_matches = [r for r in rules if r.get("color") == color]

        if pattern:
            exact = [r for r in color_matches if r.get("pattern") == pattern]
            if exact:
                return exact

        return color_matches

    def describe(self, vendor: str, model: str, color: str,
                  pattern: Optional[str] = None) -> str:
        """返回一段人类可读的查表结果文字，供日志/报警消息直接使用。"""
        matches = self.lookup(vendor, model, color, pattern)
        if not matches:
            return f"检测到{color}色异常，但{vendor} {model}暂无对应的说明书规则记录，需要人工判断"

        if len(matches) == 1:
            m = matches[0]
            return f"[{m['component']}] {m['description']}"

        # 多条匹配(比如没有pattern信息、同一颜色对应好几种组件/状态)，全部列出，
        # 不擅自挑一个当作"最可能的答案"
        lines = [f"检测到{color}色异常，说明书里有{len(matches)}种可能对应的情况:"]
        for m in matches:
            lines.append(f"  - [{m['component']}/{m['pattern']}] {m['description']}")
        return "\n".join(lines)
