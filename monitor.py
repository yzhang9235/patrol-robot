# -*- coding: utf-8 -*-
"""
monitor.py
Phase 0 核心脚本：读取摄像头 -> 检测每个标定区域的LED颜色/闪烁状态 -> 输出告警

用法：
    python monitor.py

前置条件：
    先运行 calibrate.py 生成 regions.json

按 'q' 退出监控
"""

import cv2
import json
import time
import numpy as np
from collections import deque
from datetime import datetime

REGIONS_FILE = "regions.json"
LOG_FILE = "alerts.log"

# ---------- 可调参数 ----------
HISTORY_LEN = 30          # 每个区域保留最近多少帧的亮度值，用于判断闪烁
BLINK_STD_THRESHOLD = 12  # 亮度标准差超过这个值，判定为"闪烁/呼吸灯"（渐变式blink也能测到）
OFF_BRIGHTNESS_THRESHOLD = 40   # 降低阈值，减少因ROI偏大导致的误判
ALERT_COOLDOWN_SEC = 5     # 同一个区域同一种告警，冷却多少秒后才能再次触发

# HSV颜色范围（可根据实际灯光/纸张颜色微调）
COLOR_RANGES = {
    "red":    [(0, 80, 80), (10, 255, 255)],
    "red2":   [(170, 80, 80), (180, 255, 255)],  # 红色在HSV中横跨0度，需要两段
    "green":  [(40, 60, 60), (85, 255, 255)],
    "yellow": [(12, 80, 80), (35, 255, 255)],
    "blue":   [(90, 60, 60), (130, 255, 255)],
}


def classify_color(hsv_roi):
    """
    输入一个ROI的HSV图像，返回 (颜色分类, 亮度值)
    颜色分类: off / red / green / yellow / blue / unknown
    """
    brightness = np.percentile(hsv_roi[:, :, 2], 85)  # 取高亮部分，避免背景拉低均值
    if brightness < OFF_BRIGHTNESS_THRESHOLD:
        return "off", brightness

    h, w, _ = hsv_roi.shape
    total_pixels = h * w
    best_color, best_ratio = "unknown", 0.0

    for color, (lower, upper) in COLOR_RANGES.items():
        lower_np = np.array(lower, dtype=np.uint8)
        upper_np = np.array(upper, dtype=np.uint8)
        mask = cv2.inRange(hsv_roi, lower_np, upper_np)
        ratio = cv2.countNonZero(mask) / total_pixels
        if ratio > best_ratio:
            best_ratio = ratio
            best_color = "red" if color == "red2" else color

    if best_ratio < 0.15:  # 没有明显颜色占比，认为不可信
        return "unknown", brightness
    return best_color, brightness


class RegionState:
    def __init__(self, name):
        self.name = name
        self.brightness_history = deque(maxlen=HISTORY_LEN)  # 存每帧亮度值(连续量)
        self.color_history = deque(maxlen=HISTORY_LEN)
        self.last_alert_time = {}  # alert_type -> timestamp

    def update(self, color, brightness):
        self.brightness_history.append(brightness)
        self.color_history.append(color)

    def is_blinking(self):
        if len(self.brightness_history) < HISTORY_LEN:
            return False
        return np.std(self.brightness_history) >= BLINK_STD_THRESHOLD

    def dominant_color(self, window=10):
        recent = list(self.color_history)[-window:]
        if not recent:
            return "unknown"
        return max(set(recent), key=recent.count)

    def can_alert(self, alert_type):
        now = time.time()
        last = self.last_alert_time.get(alert_type, 0)
        if now - last >= ALERT_COOLDOWN_SEC:
            self.last_alert_time[alert_type] = now
            return True
        return False


def log_alert(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(f"\033[91m🚨 {line}\033[0m")  # 终端红色高亮
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def evaluate_alerts(region_state: RegionState):
    """
    告警规则引擎（Phase 0 简化版，Phase 2 会替换成LED pattern知识库匹配）
    """
    color = region_state.dominant_color()
    blinking = region_state.is_blinking()

    if color == "red" and not blinking:
        if region_state.can_alert("red_solid"):
            log_alert(f"[{region_state.name}] 红灯常亮 -> 疑似 Power Supply Failure")

    elif color == "red" and blinking:
        if region_state.can_alert("red_blink"):
            log_alert(f"[{region_state.name}] 红灯闪烁 -> 疑似严重故障/需要人工确认")

    elif color == "off":
        if region_state.can_alert("led_off"):
            log_alert(f"[{region_state.name}] 灯灭 -> 可能断电或传感器异常")

    elif color == "yellow" and blinking:
        if region_state.can_alert("yellow_blink"):
            log_alert(f"[{region_state.name}] 黄灯闪烁 -> 警告状态，建议关注")

    # green 常亮 / 不闪烁 = 正常，不告警


def draw_overlay(frame, region, color, blinking):
    x, y, w, h = region["x"], region["y"], region["w"], region["h"]
    color_bgr = {
        "red": (0, 0, 255), "green": (0, 255, 0),
        "yellow": (0, 255, 255), "blue": (255, 0, 0),
        "off": (128, 128, 128), "unknown": (200, 200, 200),
    }.get(color, (255, 255, 255))

    cv2.rectangle(frame, (x, y), (x + w, y + h), color_bgr, 2)
    label = f"{region['name']}: {color}"
    if blinking:
        label += " (blink)"
    cv2.putText(frame, label, (x, y - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color_bgr, 1)


def main():
    try:
        with open(REGIONS_FILE, "r", encoding="utf-8") as f:
            regions = json.load(f)
    except FileNotFoundError:
        print(f"找不到 {REGIONS_FILE}，请先运行 calibrate.py 完成标定")
        return

    if not regions:
        print("regions.json 里没有任何区域，请重新标定")
        return

    states = {r["name"]: RegionState(r["name"]) for r in regions}

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头")
        return

    print(f"开始监控 {len(regions)} 个区域，按 'q' 退出")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        for region in regions:
            x, y, w, h = region["x"], region["y"], region["w"], region["h"]
            roi_hsv = hsv_frame[y:y + h, x:x + w]
            if roi_hsv.size == 0:
                continue

            color, brightness = classify_color(roi_hsv)
            state = states[region["name"]]
            state.update(color, brightness)
            blinking = state.is_blinking()

            evaluate_alerts(state)
            draw_overlay(frame, region, color, blinking)

        cv2.imshow("IDC LED Monitor - Phase 0 (laptop camera)", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()