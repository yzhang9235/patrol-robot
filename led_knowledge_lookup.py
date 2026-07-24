# -*- coding: utf-8 -*-
"""
led_knowledge_lookup.py
纯查表模块：根据 (厂商, 型号, 颜色, 状态) 去对应的 knowledge/<vendor>_<model>.json
里查出说明书原文描述。这里不包含任何"红色=严重故障"这类硬编码语义——
所有解释都来自 build_led_knowledge.py 从说明书里提取出的原文。

【颜色别名说明】不同厂商说明书里，同一种琥珀/黄色指示灯，用词并不统一——
有的写"amber"，有的写"yellow"，有的写"orange"。build_led_knowledge.py
提取规则时会把这些词都归到"yellow"这个颜色桶里存进knowledge文件；
而monitor_with_rules.py摄像头检测端固定用"amber"这个标签。
如果查表时严格按字符串相等比较，会导致检测到"amber"却永远查不到
存的是"yellow"的规则。所以这里查表前先把颜色做一次归一化，
把amber/yellow/orange都当成同一个颜色处理，不管说明书用哪个词、
不管检测端标签叫什么，都能对上。

用法(在 monitor_with_rules.py 或其他脚本里)：
    from led_knowledge_lookup import LedKnowledgeBase

    kb = LedKnowledgeBase(knowledge_dir="knowledge")
    kb.load("NVIDIA", "DGX A100")          # 加载某个厂商型号的规则文件
    matches = kb.lookup("NVIDIA", "DGX A100", color="amber", pattern="solid")
    for m in matches:
        print(m["component"], m["description"])

    positions = kb.get_led_positions("NVIDIA", "DGX A100")

如果某个station还没对应的说明书规则文件，lookup会返回空列表，
调用方应该自己决定兜底怎么处理(比如只报"检测到红色异常，暂无对应说明书解释")，
不要在这里悄悄补一个默认解释——没有依据的解释比没有解释更容易误导人。
"""

import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional


# amber/yellow/orange 视为同一个颜色，统一归一化成"amber"再比较。
# 以后如果又冒出别的说法(比如"琥珀"被误识别成别的英文词)，
# 在这个字典里加一行映射就行，不用改lookup()里的逻辑
COLOR_ALIASES = {
    "amber": "amber",
    "yellow": "amber",
    "orange": "amber",
}


def normalize_color(color: Optional[str]) -> Optional[str]:
    """把颜色名归一化，amber/yellow/orange统一算作amber，其他颜色原样返回。
    大小写不敏感(说明书原文提取出来的、或者调用方传入的，大小写不一定统一)。
    """
    if color is None:
        return None
    return COLOR_ALIASES.get(color.strip().lower(), color.strip().lower())


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
        - 颜色比较前先归一化(amber/yellow/orange视为同一个)，不管说明书
          原文用哪个词、调用方传的是哪个词，只要指的是同一种颜色都能匹配上
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

        target_color = normalize_color(color)
        rules = data.get("rules", [])
        color_matches = [r for r in rules if normalize_color(r.get("color")) == target_color]

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

        lines = [f"检测到{color}色异常，说明书里有{len(matches)}种可能对应的情况:"]
        for m in matches:
            lines.append(f"  - [{m['component']}/{m['pattern']}] {m['description']}")
        return "\n".join(lines)

    # ============ LED相对位置模板的读写 ============

    def get_led_positions(self, vendor: str, model: str) -> List[Dict[str, Any]]:
        """
        返回这个型号标定好的LED相对位置模板列表。
        格式: [{"component_name":str, "rel_x":float, "rel_y":float,
                "rel_w":float, "rel_h":float}, ...]
        (rel_*是相对面板panel_bbox的比例坐标，0~1)
        没标定过就返回空列表，调用方要判断是否需要提示先标定。
        """
        slug = self._slug(vendor, model)
        if slug not in self._cache:
            if not self.load(vendor, model):
                return []
        data = self._cache.get(slug, {})
        return data.get("led_positions", [])

    def save_led_positions(self, vendor: str, model: str,
                            positions: List[Dict[str, Any]]):
        """
        把标定好的LED相对位置模板写回 knowledge/<vendor>_<model>.json，
        跟已有的rules字段写在同一个文件里，不新建单独的位置文件。
        这是对led_positions字段的整份覆盖(重新标定会覆盖旧的位置数据)，
        但不会动这个文件里已有的rules字段——先读出原文件内容，只替换
        led_positions这一个字段再写回去。
        """
        slug = self._slug(vendor, model)
        path = self.knowledge_dir / f"{slug}.json"

        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {
                "vendor": vendor,
                "model": model,
                "rules": [],
            }

        data["led_positions"] = positions

        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 更新缓存，避免保存后马上读取时拿到过期数据
        self._cache[slug] = data