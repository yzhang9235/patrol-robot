# -*- coding: utf-8 -*-
"""
build_led_knowledge.py
Vendor Manual Parser：读入某个厂商/型号的LED状态说明书(PDF或txt)，
提取"颜色 + 状态(常亮/闪烁) + 含义"规则，生成结构化的 knowledge schema JSON。

核心设计原则：
    - 规则的 description 必须是说明书原文摘录，不允许在代码里预设
      "红色闪烁=严重故障"这种猜测性解释——不同厂商、不同型号、
      甚至同一说明书里不同组件的LED，同样的颜色状态含义可能完全不同，
      这类语义解释只能来自说明书本身。
    - 每条规则尽量标注它属于哪个组件(component)，比如"电源指示灯"、
      "风扇故障灯"、"网口活动灯"——同一份说明书里，红色常亮在电源灯上
      和在网口灯上的含义通常是两回事，不能混成一条规则。
    - 按厂商/型号分开存成独立的 JSON 文件(knowledge/<vendor>_<model>.json)，
      monitor侧运行时根据每个station配置的厂商型号加载对应文件查表，
      新增一个厂商只需要跑一遍这个脚本产出新文件，不需要碰monitor的代码。

用法：
    添加支持：pip install pillow pytesseract
    另外还需要装OCR引擎本体(pytesseract只是Python调用接口，不是OCR引擎)：
        Mac:   brew install tesseract tesseract-lang   (tesseract-lang 带中文语言包)
        Linux: apt install tesseract-ocr tesseract-ocr-chi-sim
    python3 build_led_knowledge.py 说明书.pdf --vendor NVIDIA --model "DGX A100"
    python3 build_led_knowledge.py 说明书.txt --vendor Dell --model "PowerEdge R760"
    python3 build_led_knowledge.py 说明书截图.png --vendor Supermicro --model "SYS-X12"

输出：
    knowledge/<vendor>_<model>.json   —— 结构化规则(给monitor用)
    knowledge/<vendor>_<model>.raw.txt —— 所有候选原文摘录(给人工核对用)

【必须人工核对】自动提取无法保证100%准确，尤其是"component归属"这一步是
启发式的(找规则行前面最近的一行像标题的文字)，可能会归错。生成后务必打开
JSON过一遍，删掉误抓的、补上漏掉的、把component改成实际正确的名称。
图片/截图输入走OCR识别，准确率比PDF直接提取文字更低，务必更仔细核对——
每条来自OCR的规则JSON里都会带 "ocr": true 标记，方便你知道哪些需要重点复查。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import pdfplumber

from PIL import Image
import pytesseract

# ---------------- 关键词表：只用来"找出候选行"，不用来生成含义 ----------------
COLOR_KEYWORDS = {
    "red": ["red", "红", "红色"],
    "yellow": ["yellow", "amber", "orange", "黄", "黄色", "琥珀", "橙"],
    "green": ["green", "绿", "绿色"],
    "blue": ["blue", "蓝", "蓝色"],
    "off": ["off", "熄灭", "unlit", "not lit"],
}

PATTERN_KEYWORDS = {
    "solid": ["solid", "steady", "constant", "常亮", "常量", "持续点亮"],
    "blink": ["blink", "blinking", "flash", "flashing", "闪烁", "闪动"],
}

CONTEXT_KEYWORDS = ["led", "indicator", "light", "lamp", "指示灯", "状态灯", "灯"]

HEADING_MAX_WORDS = 8

# pytesseract语言参数：同时识别简体中文和英文。说明书混排中英文很常见，
# 只用默认的纯英文模式会把中文部分全部识别错
OCR_LANGUAGES = "chi_sim+eng"


def _keyword_hit(lower_line: str, keyword: str) -> bool:
    if re.search(r"[a-zA-Z]", keyword):
        return re.search(r"(?<![a-zA-Z])" + re.escape(keyword) + r"(?![a-zA-Z])", lower_line) is not None
    return keyword in lower_line


def detect_color(line: str) -> Optional[str]:
    lower = line.lower()
    for color, keywords in COLOR_KEYWORDS.items():
        if any(_keyword_hit(lower, kw) for kw in keywords):
            return color
    return None


def detect_pattern(line: str) -> Optional[str]:
    lower = line.lower()
    for pattern, keywords in PATTERN_KEYWORDS.items():
        if any(_keyword_hit(lower, kw) for kw in keywords):
            return pattern
    return None


def has_context(line: str) -> bool:
    lower = line.lower()
    return any(_keyword_hit(lower, k) for k in CONTEXT_KEYWORDS)


def looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    words = stripped.split()
    if len(words) > HEADING_MAX_WORDS:
        return False
    if detect_color(stripped) is not None:
        return False
    if detect_pattern(stripped) is not None:
        return False
    if stripped.endswith(":") or stripped.endswith("："):
        return True
    if has_context(stripped):
        return True
    if (len(words) <= 4
            and not any(ch.isdigit() for ch in stripped)
            and all(w[0].isupper() for w in words if w[0].isalpha())):
        return True
    return False


LABEL_STOPWORDS = {
    "press", "turn", "cause", "causes", "indicates", "indicate", "allows",
    "provides", "shows", "displays", "use", "uses", "click", "perform",
    "performs", "lets", "let", "see", "refer", "note", "important", "when",
    "here", "this", "the", "to", "for", "with",
}


def extract_leading_label(line: str) -> Optional[str]:
    stripped = re.sub(r"^[‣•\-\*\u2022]+\s*", "", line.strip())
    words = stripped.split()
    label_words = []
    for w in words:
        core = re.sub(r"[^A-Za-z]", "", w)
        if not core:
            break
        if not core[0].isupper():
            break
        lower_core = core.lower()
        if lower_core in LABEL_STOPWORDS:
            break
        if detect_color(w) is not None or detect_pattern(w) is not None:
            break
        label_words.append(core)
        if len(label_words) >= 3:
            break
    if not label_words:
        return None
    return " ".join(label_words)


def extract_rules(text_by_page: List[str], source_name: str, is_ocr: bool = False) -> List[Dict[str, Any]]:
    rules = []
    rule_id = 0

    for page_num, page_text in enumerate(text_by_page, start=1):
        current_component = "unknown"

        for raw_line in page_text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue

            label = extract_leading_label(line)
            if label:
                current_component = label

            if looks_like_heading(line):
                current_component = line.rstrip(":：").strip()
                continue

            color = detect_color(line)
            if color is None:
                continue

            pattern = detect_pattern(line)
            if pattern is None and not has_context(line):
                continue

            rule_id += 1
            rules.append({
                "id": f"r{rule_id}",
                "component": current_component,
                "color": color,
                "pattern": pattern if pattern else "unknown",
                "description": line,
                "page": page_num,
                "source": source_name,
                "confidence": "high" if pattern else "low",
                "ocr": is_ocr,
            })

    return rules


def read_pdf_pages(pdf_path: Path) -> List[str]:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def read_txt_pages(txt_path: Path) -> List[str]:
    return [txt_path.read_text(encoding="utf-8", errors="ignore")]


def read_image_pages(image_path: Path) -> List[str]:
    try:
        img = Image.open(image_path)
    except Exception as e:
        print(f"图片打不开: {e}")
        sys.exit(1)

    try:
        text = pytesseract.image_to_string(img, lang=OCR_LANGUAGES)
    except pytesseract.TesseractNotFoundError:
        print("找不到 tesseract 引擎本体(pytesseract只是Python接口，还需要装OCR引擎)。")
        print("Mac:   brew install tesseract tesseract-lang")
        print("Linux: apt install tesseract-ocr tesseract-ocr-chi-sim")
        sys.exit(1)
    except Exception as e:
        print(f"OCR识别失败: {e}")
        print(f"如果提示找不到语言包，Mac上试试: brew reinstall tesseract-lang")
        sys.exit(1)

    return [text]


def main():
    parser = argparse.ArgumentParser(description="Vendor Manual Parser: 从说明书生成LED knowledge schema")
    parser.add_argument("input_path", type=str, help="说明书文件路径(.pdf / .txt / .png / .jpg / .jpeg)")
    parser.add_argument("--vendor", type=str, required=True, help="厂商名称，比如 NVIDIA / Dell / Supermicro")
    parser.add_argument("--model", type=str, required=True, help="型号，比如 'DGX A100' / 'PowerEdge R760'")
    parser.add_argument("--out-dir", type=str, default="knowledge", help="输出目录，默认 knowledge/")
    args = parser.parse_args()

    input_path = Path(args.input_path)
    if not input_path.exists():
        print(f"文件不存在: {input_path}")
        sys.exit(1)

    suffix = input_path.suffix.lower()
    is_ocr = False

    if suffix == ".pdf":
        pages = read_pdf_pages(input_path)
        empty_pages = sum(1 for p in pages if not p.strip())
        if empty_pages:
            print(f"提醒: 有 {empty_pages}/{len(pages)} 页没提取到文字，"
                  f"可能是扫描版/图片版页面，这部分内容会被跳过。"
                  f"如果LED说明恰好在这些页里，需要把该页截图存成图片单独跑一遍")
    elif suffix == ".txt":
        pages = read_txt_pages(input_path)
    elif suffix in [".png", ".jpg", ".jpeg"]:
        pages = read_image_pages(input_path)
        is_ocr = True
    else:
        print(f"不支持的文件类型: {suffix} (目前支持 .pdf / .txt / .png / .jpg / .jpeg)")
        sys.exit(1)

    rules = extract_rules(pages, source_name=input_path.name, is_ocr=is_ocr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{args.vendor}_{args.model}").strip("_")

    knowledge = {
        "vendor": args.vendor,
        "model": args.model,
        "source_file": input_path.name,
        "extracted_at": datetime.now().isoformat(),
        "rule_count": len(rules),
        "rules": rules,
    }

    json_path = out_dir / f"{slug}.json"
    # 【新增】如果这个型号之前已经用--calibrate标定过LED位置，
    # 重新解析说明书时要保留这份数据，不能被这次整份覆盖掉
    existing_led_positions = []
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            existing_data = json.load(f)
        existing_led_positions = existing_data.get("led_positions", [])

    knowledge = {
        "vendor": args.vendor,
        "model": args.model,
        "source_file": input_path.name,
        "extracted_at": datetime.now().isoformat(),
        "rule_count": len(rules),
        "rules": rules,
        "led_positions": existing_led_positions,
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(knowledge, f, ensure_ascii=False, indent=2)

    raw_path = out_dir / f"{slug}.raw.txt"
    with open(raw_path, "w", encoding="utf-8") as f:
        for r in rules:
            f.write(f"[{r['confidence']}][第{r['page']}页][{r['component']}]"
                     f"[{r['color']}/{r['pattern']}]{'[OCR]' if r['ocr'] else ''} {r['description']}\n")

    print(f"提取到 {len(rules)} 条规则")
    print(f"规则文件: {json_path}")
    print(f"人工核对用原文摘录: {raw_path}")
    low_conf = sum(1 for r in rules if r["confidence"] == "low")
    if low_conf:
        print(f"注意: 有 {low_conf} 条规则没识别出常亮/闪烁状态(confidence=low)，"
              f"这些需要打开原文件人工确认")
    if is_ocr:
        print(f"注意: 这次是OCR图片识别，准确率比PDF文字提取低，"
              f"每条规则都标了 \"ocr\": true，务必逐条对照原图核对")


if __name__ == "__main__":
    main()