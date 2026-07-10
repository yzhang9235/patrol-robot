# -*- coding: utf-8 -*-
"""
patrol_monitor.py
巡检条版：持续移动扫描 -> 自动检测LED(复用auto_calibrate的亮度+轮廓检测)
-> 单帧发现红/黄异常颜色 -> 停止巡检，原地观察是否闪烁(复用monitor.py的亮度方差判断)
-> 确认后录像 -> 保存文件 -> 恢复巡检

设计原则：巡检阶段不做任何停顿，全速跑摄像头帧率；只有真正抓到疑似异常时才停下来，
这样"巡检一圈的时间"只取决于轨道移动速度本身，不会被逐个检测LED的开销拖慢。

用法：
    python patrol_monitor.py

按 'q' 退出（无论在哪个状态）
"""

import cv2
import time
import os
import sys
from datetime import datetime
from collections import deque
import numpy as np

# ---------- 自动检测LED参数 (复用auto_calibrate.py的逻辑) ----------
BRIGHT_THRESHOLD = 200
MIN_AREA = 15
MAX_AREA = 5000
MIN_CIRCULARITY = 0.3
PADDING = 4

# ---------- 颜色分类参数 (复用monitor.py的逻辑) ----------
OFF_BRIGHTNESS_THRESHOLD = 40
COLOR_RANGES = {
    "red":    [(0, 80, 80), (10, 255, 255)],
    "red2":   [(170, 80, 80), (180, 255, 255)],
    "green":  [(40, 60, 60), (85, 255, 255)],
    "yellow": [(12, 80, 80), (35, 255, 255)],
    "blue":   [(90, 60, 60), (130, 255, 255)],
}
ANOMALY_COLORS = ("red", "yellow")  # 巡检时一旦看到这些颜色就触发停留观察

# ---------- 停留观察 & 录像参数 ----------
OBSERVE_FRAMES = 30          # 停下来观察多少帧判断是否闪烁(约1秒@30fps)
BLINK_STD_THRESHOLD = 12     # 亮度标准差超过这个值判定为闪烁
RECORD_DURATION_SEC = 10     # 确认异常后录像时长
OUTPUT_DIR = "recordings"
CAMERA_SOURCE = 0            # 换成巡检条实际摄像头index或RTSP地址


def detect_led_candidates(frame):
    """自动检测画面里的LED候选区域(亮度阈值+轮廓+圆度过滤)"""
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
        x, y = max(0, x - PADDING), max(0, y - PADDING)
        found.append((x, y, w + PADDING * 2, h + PADDING * 2))
    return found


def classify_color(hsv_roi):
    """返回 (颜色分类, 亮度值)，逻辑跟monitor.py一致"""
    brightness = np.percentile(hsv_roi[:, :, 2], 85)
    if brightness < OFF_BRIGHTNESS_THRESHOLD:
        return "off", brightness

    h, w, _ = hsv_roi.shape
    total = h * w
    best_color, best_ratio = "unknown", 0.0
    for color, (lower, upper) in COLOR_RANGES.items():
        mask = cv2.inRange(hsv_roi, np.array(lower, dtype=np.uint8), np.array(upper, dtype=np.uint8))
        ratio = cv2.countNonZero(mask) / total
        if ratio > best_ratio:
            best_ratio = ratio
            best_color = "red" if color == "red2" else color
    if best_ratio < 0.15:
        return "unknown", brightness
    return best_color, brightness


# ---------- 轨道控制占位函数 (TODO: 换成实际的控制API) ----------
def motor_move_continuous():
    print("[TODO-接轨道API] 开始持续巡检移动")


def motor_stop():
    print("[TODO-接轨道API] 停止移动")


def motor_resume():
    print("[TODO-接轨道API] 恢复巡检移动")


def get_current_position():
    """
    TODO: 换成读取轨道编码器/位置反馈的实际代码，返回当前位置对应的rack_id
    没有这个信息之前，先用时间戳当文件名区分
    """
    return "unknown_position"


def deliver_video(filepath):
    """TODO: 传输方式确定后，在这里加实际上传/发送代码"""
    print(f"[TODO-接传输方式] 视频已保存: {os.path.abspath(filepath)}")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cap = cv2.VideoCapture(CAMERA_SOURCE)
    if not cap.isOpened():
        print("无法打开摄像头")
        return

    print("按 'q' 退出")
    motor_move_continuous()

    state = "patrolling"   # patrolling / observing / recording
    observe_bbox = None
    observe_color = None
    brightness_history = deque(maxlen=OBSERVE_FRAMES)
    writer = None
    record_start_time = None
    record_filepath = None

    while True:
        ret, frame = cap.read()
        if not ret:
            print("读取画面失败")
            break

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        display = frame.copy()

        if state == "patrolling":
            candidates = detect_led_candidates(frame)
            triggered = None

            for (x, y, w, h) in candidates:
                roi_hsv = hsv[y:y + h, x:x + w]
                color, _ = classify_color(roi_hsv)
                box_color = (0, 255, 0)
                if color in ANOMALY_COLORS:
                    box_color = (0, 0, 255)
                    if triggered is None:
                        triggered = (x, y, w, h, color)
                cv2.rectangle(display, (x, y), (x + w, y + h), box_color, 2)

            if triggered:
                x, y, w, h, color = triggered
                print(f"[发现疑似异常] 颜色={color} 位置=({x},{y}) -> 停止观察")
                motor_stop()
                observe_bbox = (x, y, w, h)
                observe_color = color
                brightness_history.clear()
                state = "observing"

        elif state == "observing":
            x, y, w, h = observe_bbox
            roi_hsv = hsv[y:y + h, x:x + w]
            color, brightness = classify_color(roi_hsv)
            brightness_history.append(brightness)
            cv2.rectangle(display, (x, y), (x + w, y + h), (0, 165, 255), 2)
            cv2.putText(display, f"观察中 {len(brightness_history)}/{OBSERVE_FRAMES}",
                        (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)

            if len(brightness_history) >= OBSERVE_FRAMES:
                blinking = np.std(brightness_history) >= BLINK_STD_THRESHOLD
                print(f"[观察完成] 颜色={observe_color} 闪烁={blinking}")

                rack_id = get_current_position()
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                record_filepath = os.path.join(
                    OUTPUT_DIR, f"{rack_id}_{observe_color}_{'blink' if blinking else 'solid'}_{timestamp}.mp4"
                )
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                fps = 20
                fh, fw = frame.shape[:2]
                writer = cv2.VideoWriter(record_filepath, fourcc, fps, (fw, fh))
                record_start_time = time.time()
                print(f"[开始录像] {record_filepath}")
                state = "recording"

        elif state == "recording":
            writer.write(frame)
            elapsed = time.time() - record_start_time
            cv2.putText(display, f"REC {int(elapsed)}s/{RECORD_DURATION_SEC}s",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

            if elapsed >= RECORD_DURATION_SEC:
                writer.release()
                writer = None
                print(f"[录像完成] {record_filepath}")
                deliver_video(record_filepath)
                motor_resume()
                state = "patrolling"

        cv2.putText(display, f"状态: {state}", (10, display.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        cv2.imshow("Patrol Monitor", display)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            if writer is not None:
                writer.release()
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
