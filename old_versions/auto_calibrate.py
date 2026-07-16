# -*- coding: utf-8 -*-
"""
auto_calibrate_v2.py
在原版基础上加入颜色分类过滤：只保留红/黄/绿三色候选，排除白色反光/白墙干扰

新增逻辑：
    1. 仍然用亮度阈值找候选区域（跟原来一样）
    2. 但每个候选区域内，再检查"是否有饱和的颜色像素"：
       - 如果轮廓内大部分像素饱和度很低（发白），判定为反光/白墙，直接丢弃
       - 否则统计非白色像素的色相(H)众数，归到 红/黄/绿 三类之一
    3. 只有能归到这三类之一的候选框才会被保留、显示、进入命名流程
    4. 屏幕上会显示每个候选框判定出的颜色，方便你现场核对准不准

调参提示：
    - 如果绿色LED总被漏掉，把 GREEN_HUE_RANGE 适当放宽
    - 如果黄色/红色互相误判，说明你这批LED的实际色相和默认区间有偏差，
      按屏幕上显示的H值实测调整对应区间
    - 如果LED中心过曝成纯白导致整体饱和度太低而被当成反光丢弃，
      可以尝试降低摄像头曝光（见下方 cap.set 那行，取消注释调整）
"""

import cv2
import json
import numpy as np

REGIONS_FILE = "regions.json"
WINDOW_NAME = "Auto Calibrate v2 - color-filtered LED detection"

# ---------- 亮度/形状参数（跟原版一致） ----------
BRIGHT_THRESHOLD = 200
MIN_AREA = 15
MAX_AREA = 15000
MIN_CIRCULARITY = 0.3
PADDING = 4

# ---------- 颜色分类参数 ----------
# OpenCV HSV: H范围0-179, S/V范围0-255
MIN_SATURATION_FOR_COLOR = 60   # 轮廓内像素饱和度超过这个值才算"有颜色"，用来剔除白色反光
COLOR_PIXEL_MIN_RATIO = 0.15    # 轮廓内至少这个比例的像素是"有颜色"的，否则判定为反光丢弃

RED_HUE_RANGES = [(0, 8), (172, 179)]   # 红色跨越色环两端，分两段
YELLOW_HUE_RANGE = (15, 35)
GREEN_HUE_RANGE = (40, 85)

COLOR_DISPLAY = {
    "red": (0, 0, 255),
    "yellow": (0, 255, 255),
    "green": (0, 255, 0),
}

frozen = False
candidates = []        # 冻结时的候选框列表 [(x,y,w,h,color_label), ...]
naming_index = -1
name_buffer = ""
regions = []


def classify_color(hsv_roi):
    """给一个HSV小图，返回 'red'/'yellow'/'green' 或 None（判定为反光/无法分类）"""
    h, s, v = cv2.split(hsv_roi)
    colored_mask = s > MIN_SATURATION_FOR_COLOR
    total = h.size
    colored_count = int(np.count_nonzero(colored_mask))

    if total == 0 or colored_count / total < COLOR_PIXEL_MIN_RATIO:
        return None  # 饱和度普遍太低，判定为白色反光/干扰，丢弃

    hues = h[colored_mask]

    red_mask = np.zeros_like(hues, dtype=bool)
    for lo, hi in RED_HUE_RANGES:
        red_mask |= (hues >= lo) & (hues <= hi)
    red_count = int(np.count_nonzero(red_mask))

    yellow_count = int(np.count_nonzero(
        (hues >= YELLOW_HUE_RANGE[0]) & (hues <= YELLOW_HUE_RANGE[1])))

    green_count = int(np.count_nonzero(
        (hues >= GREEN_HUE_RANGE[0]) & (hues <= GREEN_HUE_RANGE[1])))

    counts = {"red": red_count, "yellow": yellow_count, "green": green_count}
    best_color = max(counts, key=counts.get)

    if counts[best_color] == 0:
        return None  # 有饱和像素，但不落在红/黄/绿任何一段，可能是蓝色等其他干扰

    return best_color


def detect_led_candidates(frame):
    hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    v_channel = hsv_full[:, :, 2]

    _, mask = cv2.threshold(v_channel, BRIGHT_THRESHOLD, 255, cv2.THRESH_BINARY)
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    found = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_AREA or area > MAX_AREA:
            continue
        perimeter = cv2.arcLength(c, True)
        if perimeter == 0:
            continue
        circularity = 4 * np.pi * area / (perimeter ** 2)
        if circularity < MIN_CIRCULARITY:
            continue

        x, y, w, h = cv2.boundingRect(c)
        x = max(0, x - PADDING)
        y = max(0, y - PADDING)
        w = w + PADDING * 2
        h = h + PADDING * 2

        # 稍微扩大一点取样区域，把过曝中心周围的色晕也纳入判断
        sx = max(0, x - 3)
        sy = max(0, y - 3)
        roi = hsv_full[sy:sy + h + 6, sx:sx + w + 6]
        color_label = classify_color(roi)

        if color_label is None:
            continue  # 不是红/黄/绿，判定为反光或其他干扰，直接丢弃

        found.append((x, y, w, h, color_label))

    return found


def draw_candidates(frame, boxes, highlight_index=-1):
    for i, box in enumerate(boxes):
        x, y, w, h, color_label = box
        base_color = COLOR_DISPLAY.get(color_label, (255, 255, 255))
        thickness = 3 if i == highlight_index else 2
        cv2.rectangle(frame, (x, y), (x + w, y + h), base_color, thickness)
        label = f"{i + 1}:{color_label}"
        cv2.putText(frame, label, (x, y - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, base_color, 2)


def draw_naming_ui(frame):
    bar_text = f"[{naming_index+1}/{len(candidates)}] 输入名字后回车 (Esc=跳过这个): {name_buffer}_"
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 40), (40, 40, 40), -1)
    cv2.putText(frame, bar_text, (10, 27),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
    draw_candidates(frame, candidates, highlight_index=naming_index)


def main():
    global frozen, candidates, naming_index, name_buffer, regions, BRIGHT_THRESHOLD

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头")
        return

    # 如果LED中心总是过曝成纯白导致颜色判断不准，可以尝试降低曝光（不同摄像头效果不一，需要实测）：
    # cap.set(cv2.CAP_PROP_EXPOSURE, -6)
    # cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)

    print("=" * 50)
    print("自动标定说明（v2 加入颜色过滤）：")
    print("  只保留判定为 红/黄/绿 的候选区域，白色反光会被自动丢弃")
    print("  按 'a' 冻结并开始逐个命名 | 'r' 恢复实时检测")
    print("  '[' 调低亮度阈值 | ']' 调高亮度阈值")
    print("  'q' 不保存退出")
    print("=" * 50)

    live_boxes = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if not frozen:
            live_boxes = detect_led_candidates(frame)

        display = frame.copy()
        info = f"threshold={BRIGHT_THRESHOLD}  candidates={len(live_boxes)}  " \
               f"(冻结按a / 阈值[ ])"
        cv2.putText(display, info, (10, display.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)

        if naming_index >= 0:
            draw_naming_ui(display)
        else:
            draw_candidates(display, live_boxes if not frozen else candidates)

        cv2.imshow(WINDOW_NAME, display)
        key = cv2.waitKey(30) & 0xFF

        if naming_index >= 0:
            if key == 13 or key == 10:
                x, y, w, h, color_label = candidates[naming_index]
                name = name_buffer.strip() or f"led_{naming_index+1}"
                regions.append({
                    "name": name, "x": x, "y": y, "w": w, "h": h,
                    "color": color_label
                })
                print(f"已添加: {name} -> ({x},{y},{w},{h}) color={color_label}")
                name_buffer = ""
                naming_index += 1
            elif key == 27:
                print(f"跳过候选框 #{naming_index+1}")
                name_buffer = ""
                naming_index += 1
            elif key == 8 or key == 127:
                name_buffer = name_buffer[:-1]
            elif 32 <= key <= 126:
                name_buffer += chr(key)

            if naming_index >= len(candidates):
                with open(REGIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(regions, f, ensure_ascii=False, indent=2)
                print(f"\n全部完成，已保存 {len(regions)} 个区域到 {REGIONS_FILE}")
                break
            continue

        if key == ord('a') and not frozen:
            frozen = True
            candidates = list(live_boxes)
            if not candidates:
                print("当前没有检测到候选框，先调整亮度阈值或摄像头角度")
                frozen = False
                continue
            naming_index = 0
            print(f"已冻结，共 {len(candidates)} 个候选框，开始逐个命名")
        elif key == ord('r'):
            frozen = False
            print("恢复实时检测")
        elif key == ord('['):
            BRIGHT_THRESHOLD = max(0, BRIGHT_THRESHOLD - 10)
        elif key == ord(']'):
            BRIGHT_THRESHOLD = min(255, BRIGHT_THRESHOLD + 10)
        elif key == ord('q'):
            print("退出，未保存")
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()