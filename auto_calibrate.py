# -*- coding: utf-8 -*-
"""
auto_calibrate.py
自动定位LED版标定工具：用亮度阈值 + 轮廓检测，自动找出画面里的"亮点"作为LED候选区域

用法：
    python3 auto_calibrate.py

操作：
    - 实时显示检测到的候选框（绿色 = 候选，编号显示）
    - 按 'a' 冻结当前检测结果，进入逐个命名模式（回车确认，Esc跳过这个不要）
    - 命名完所有候选框后自动保存到 regions.json 并退出
    - 'r' 恢复实时检测（如果已经冻结但想重新来）
    - '[' / ']' 调低/调高亮度阈值（画面里会显示当前值，太多干扰就调高，漏检就调低）
    - 'q' 不保存退出

调参提示：
    如果背景反光/白墙也被识别成候选框，调高亮度阈值(']')
    如果暗一点的LED漏检，调低亮度阈值('[')
    MIN_AREA / MAX_AREA / MIN_CIRCULARITY 这三个参数在代码顶部，可以按实际LED在画面里的大小和形状调整
"""

import cv2
import json
import numpy as np

REGIONS_FILE = "regions.json"
WINDOW_NAME = "Auto Calibrate - LED auto detection"

# ---------- 可调参数 ----------
BRIGHT_THRESHOLD = 200     # 亮度阈值(0-255)，超过这个值才算候选亮点
MIN_AREA = 15              # 候选区域最小像素面积，太小的当噪点过滤掉
MAX_AREA = 5000            # 候选区域最大像素面积，太大的当反光/白墙过滤掉
MIN_CIRCULARITY = 0.3      # 圆度过滤(0-1，1=完美圆形)，太不规则的形状过滤掉
PADDING = 4                # 保存框时在检测到的轮廓外围留一点边距

frozen = False
candidates = []       # 冻结时的候选框列表 [(x,y,w,h), ...]
naming_index = -1      # 当前正在命名第几个候选框，-1表示还没开始命名
name_buffer = ""
regions = []


def detect_led_candidates(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    v_channel = hsv[:, :, 2]

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
        found.append((x, y, w, h))

    return found


def draw_candidates(frame, boxes, highlight_index=-1):
    for i, (x, y, w, h) in enumerate(boxes):
        color = (0, 255, 255) if i == highlight_index else (0, 255, 0)
        thickness = 3 if i == highlight_index else 2
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, thickness)
        cv2.putText(frame, str(i + 1), (x, y - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)


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

    print("=" * 50)
    print("自动标定说明：")
    print("  实时显示候选LED区域（绿框+编号）")
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
            if key == 13 or key == 10:  # Enter 确认这个名字
                x, y, w, h = candidates[naming_index]
                name = name_buffer.strip() or f"led_{naming_index+1}"
                regions.append({"name": name, "x": x, "y": y, "w": w, "h": h})
                print(f"已添加: {name} -> ({x},{y},{w},{h})")
                name_buffer = ""
                naming_index += 1
            elif key == 27:  # Esc 跳过这个候选框，不加入结果
                print(f"跳过候选框 #{naming_index+1}")
                name_buffer = ""
                naming_index += 1
            elif key == 8 or key == 127:  # Backspace
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