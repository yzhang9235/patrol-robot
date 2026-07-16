# -*- coding: utf-8 -*-
"""
patrol_monitor.py
全自动巡检LED监控：巡检条移动过程中持续识别LED颜色
    - 遇到 红/黄 -> 发送"停止"指令给巡检条，开始录像，记录报警日志
    - 遇到 绿色 (或没检测到异常色) -> 发送"继续"指令，巡检条前进检查下一台

全程无需人工操作，脚本本身就是一个持续运行的服务（可用 systemd / supervisor 之类常驻）。

【重要】巡检条控制目前用的是"网络请求"占位实现（HTTP POST），
具体协议还没定下来，等你确定巡检条那边暴露的接口(HTTP/MQTT/串口)后，
只需要改 send_stop_command() / send_resume_command() 这两个函数内部的实现，
外面的状态机逻辑完全不用动。

配置项都在最上面 CONFIG 区域，先改这些：
    RAIL_STOP_URL / RAIL_RESUME_URL   -> 巡检条的接口地址（占位，需替换成真实地址）
    ALERT_LOG_DIR / ALERT_VIDEO_DIR   -> 报警日志和视频的保存路径
    CONSECUTIVE_FRAMES_TO_CONFIRM     -> 连续几帧检测到同一异常颜色才真正报警（防误判抖动）
    RECORD_SECONDS_AFTER_TRIGGER      -> 报警后继续录多少秒才停止录像、恢复巡检
"""

import cv2
import json
import time
import logging
import requests
import numpy as np
from pathlib import Path
from collections import deque
from datetime import datetime

# ============ CONFIG ============
RAIL_STOP_URL = "http://<rail-controller-ip>/api/stop"      # TODO: 换成真实地址
RAIL_RESUME_URL = "http://<rail-controller-ip>/api/resume"  # TODO: 换成真实地址
RAIL_REQUEST_TIMEOUT = 2.0   # 秒，网络请求超时时间，避免卡住主循环

CAMERA_INDEX = 0
SHOW_WINDOW = True           # 生产环境建议 False（无人值守，不需要显示画面）；调试时改 True

ALERT_LOG_DIR = Path("alerts/logs")
ALERT_VIDEO_DIR = Path("alerts/videos")

CONSECUTIVE_FRAMES_TO_CONFIRM = 5     # 连续5帧都检测到红/黄，才判定为真实报警（约0.15秒@30fps）
RECORD_SECONDS_AFTER_TRIGGER = 6.5    # 触发确认后，继续录多少秒才停止录像、恢复巡检
PRE_TRIGGER_BUFFER_SECONDS = 2.0      # 报警前缓冲：把触发前2秒的画面也存进视频，方便看清"怎么变的"
# 两者相加 = 8.5秒，比模拟器10秒的切换周期留了约1.5秒安全余量
# (给确认延迟、指令发送等留出空间，避免录像跨到下一个unit)
VIDEO_FPS = 20.0

# ---------- 扫描区域(ROI) ----------
# 只在这个区域内找LED，区域外的一律忽略(手、背景反光、走廊灯光都不会被扫到)
# 设成 None 表示扫全画面(不建议正式使用)；建议按巡检条实际工作距离下，
# LED出现的画面位置实测填一个 (x1, y1, x2, y2) 矩形，留一点余量防止对位误差
SCAN_ROI = None   # 例如: (200, 150, 1000, 500)

# 标定模式：True时不做面积过滤，把每个候选框的实测面积打印在框旁边，
# 用来在实际工作距离下读出真实的面积数值，标定完 MIN_AREA/MAX_AREA 后改回 False
DEBUG_SHOW_AREA = False

# ---------- 亮度/形状参数 ----------
# 下面这些面积参数要按巡检条"实际工作距离"下实测的LED像素大小来调，
# 不要用近距离测试的结果(比如拿手机怼近镜头拍)，那样得出的MAX_AREA会偏小，
# 导致真实工作距离下太大或太小都被误判成反光/白墙滤掉
BRIGHT_THRESHOLD = 200
MIN_AREA = 15
MAX_AREA = 200000   # 大幅放宽：反光过滤已经交给饱和度判断(classify_color)负责，
                     # 这里的面积上限只用来防止"整片画面都过曝"这种极端情况，
                     # 不再承担区分近距离大LED和反光的职责，避免每天因光线/
                     # 距离的微小差异反复失效
MIN_CIRCULARITY = 0.3
PADDING = 4

# ---------- 颜色分类参数 ----------
MIN_SATURATION_FOR_COLOR = 60
COLOR_PIXEL_MIN_RATIO = 0.15
RED_HUE_RANGES = [(0, 8), (172, 179)]
YELLOW_HUE_RANGE = (15, 35)
GREEN_HUE_RANGE = (40, 85)
ALERT_COLORS = {"red", "yellow"}
OK_COLORS = {"green"}
# =================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("patrol_monitor")


def ensure_dirs():
    for d in (ALERT_LOG_DIR, ALERT_VIDEO_DIR):
        d.mkdir(parents=True, exist_ok=True)


def classify_color(hsv_roi):
    h, s, v = cv2.split(hsv_roi)
    colored_mask = s > MIN_SATURATION_FOR_COLOR
    total = h.size
    colored_count = int(np.count_nonzero(colored_mask))

    if total == 0 or colored_count / total < COLOR_PIXEL_MIN_RATIO:
        return None

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
        return None
    return best_color


def detect_led_candidates(frame):
    """返回 [(x,y,w,h,color_label), ...]，只保留能归类为红/黄/绿的候选框"""
    roi_offset_x, roi_offset_y = 0, 0
    if SCAN_ROI is not None:
        x1, y1, x2, y2 = SCAN_ROI
        frame = frame[y1:y2, x1:x2]
        roi_offset_x, roi_offset_y = x1, y1

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
        if not DEBUG_SHOW_AREA and (area < MIN_AREA or area > MAX_AREA):
            continue
        perimeter = cv2.arcLength(c, True)
        if perimeter == 0:
            continue
        circularity = 4 * np.pi * area / (perimeter ** 2)
        if not DEBUG_SHOW_AREA and circularity < MIN_CIRCULARITY:
            continue

        x, y, w, h = cv2.boundingRect(c)
        x = max(0, x - PADDING)
        y = max(0, y - PADDING)
        w = w + PADDING * 2
        h = h + PADDING * 2

        sx = max(0, x - 3)
        sy = max(0, y - 3)
        roi = hsv_full[sy:sy + h + 6, sx:sx + w + 6]
        color_label = classify_color(roi)
        if color_label is None:
            if not DEBUG_SHOW_AREA:
                continue
            color_label = "?"  # 标定模式下颜色分类失败也照样显示面积，方便观察

        # 换算回原始画面坐标(如果用了SCAN_ROI裁剪，坐标要加回偏移量)
        found.append((x + roi_offset_x, y + roi_offset_y, w, h, color_label, int(area)))

    return found


def send_stop_command(reason_color, station_id="unknown"):
    """通知巡检条停止。目前是HTTP占位实现，等接口定了改这里就行。"""
    try:
        resp = requests.post(
            RAIL_STOP_URL,
            json={"action": "stop", "reason": reason_color, "station": station_id},
            timeout=RAIL_REQUEST_TIMEOUT,
        )
        logger.info(f"已发送停止指令 (reason={reason_color}) -> 响应状态 {resp.status_code}")
    except Exception as e:
        # 网络请求失败不应该让整个监控脚本崩掉——但必须大声记录下来，
        # 因为这意味着巡检条可能没有真的停下来，需要人工介入排查通信问题
        logger.error(f"停止指令发送失败: {e}（巡检条可能未真正停止，请检查通信链路）")


def send_resume_command(station_id="unknown"):
    try:
        resp = requests.post(
            RAIL_RESUME_URL,
            json={"action": "resume", "station": station_id},
            timeout=RAIL_REQUEST_TIMEOUT,
        )
        logger.info(f"已发送继续指令 -> 响应状态 {resp.status_code}")
    except Exception as e:
        logger.error(f"继续指令发送失败: {e}")


def write_alert_log(color_label, video_path):
    ensure_dirs()
    ts = datetime.now()
    record = {
        "timestamp": ts.isoformat(),
        "color": color_label,
        "video_file": str(video_path),
    }
    log_file = ALERT_LOG_DIR / f"{ts:%Y-%m-%d}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.warning(f"报警记录已写入: {log_file} -> {record}")


def main():
    ensure_dirs()
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        logger.error("无法打开摄像头，退出")
        return

    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480

    # 用一个环形缓冲存最近几秒的帧，报警触发时把"触发前"的画面也一起写进视频
    buffer_maxlen = int(PRE_TRIGGER_BUFFER_SECONDS * VIDEO_FPS)
    frame_buffer = deque(maxlen=buffer_maxlen)

    state = "PATROLLING"   # PATROLLING(正常巡检) / ALERTING(已停止,正在录像)
    consecutive_alert_frames = 0
    consecutive_alert_color = None
    alert_start_time = None
    video_writer = None
    video_path = None

    logger.info("巡检监控已启动，全自动运行中...")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                logger.error("读取摄像头帧失败，重试中...")
                time.sleep(0.5)
                continue

            frame_buffer.append(frame.copy())
            candidates = detect_led_candidates(frame)
            colors_found = {c[4] for c in candidates}

            if SHOW_WINDOW:
                display = frame.copy()
                for (x, y, w, h, color_label, area) in candidates:
                    cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)
                    label = f"{color_label} area={area}" if DEBUG_SHOW_AREA else color_label
                    cv2.putText(display, label, (x, y - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
                cv2.putText(display, f"state={state}", (10, frame_h - 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
                if DEBUG_SHOW_AREA:
                    cv2.putText(display, "DEBUG_SHOW_AREA=True 标定模式：先记录数值再关掉", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
                cv2.imshow("patrol_monitor (debug)", display)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            if DEBUG_SHOW_AREA:
                # 标定模式下不触发报警逻辑，只用来读数值
                continue

            if state == "PATROLLING":
                alert_hits = colors_found & ALERT_COLORS
                if alert_hits:
                    this_color = sorted(alert_hits)[0]
                    if this_color == consecutive_alert_color:
                        consecutive_alert_frames += 1
                    else:
                        consecutive_alert_color = this_color
                        consecutive_alert_frames = 1
                else:
                    consecutive_alert_frames = 0
                    consecutive_alert_color = None

                if consecutive_alert_frames >= CONSECUTIVE_FRAMES_TO_CONFIRM:
                    # ---- 触发报警 ----
                    logger.warning(f"检测到异常颜色: {consecutive_alert_color}，触发停止+录像")
                    send_stop_command(consecutive_alert_color)

                    ts = datetime.now()
                    video_path = ALERT_VIDEO_DIR / f"alert_{ts:%Y%m%d_%H%M%S}.mp4"

                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    video_writer = cv2.VideoWriter(
                        str(video_path), fourcc, VIDEO_FPS, (frame_w, frame_h))
                    # 把缓冲区里"触发前"的画面先写进去(固定帧数，回放时长固定为
                    # PRE_TRIGGER_BUFFER_SECONDS，不受当时循环速度影响)
                    for buffered_frame in frame_buffer:
                        video_writer.write(buffered_frame)

                    # ---- 关键修复：录像结束条件用"目标帧数"而不是"墙钟时间" ----
                    # 之前是数墙钟时间是否过了N秒，但每秒实际能处理/写入多少帧
                    # 会因为系统负载波动，导致同样等N秒，写入的帧数不一样，
                    # 回放出来的视频时长就忽长忽短。
                    # 现在改成：无论实际循环跑多快/多慢，都写够固定帧数，
                    # 不够就在当前帧到位时"追帧"补齐，保证每次视频文件严格
                    # 等于 RECORD_SECONDS_AFTER_TRIGGER 这个时长。
                    post_trigger_target_frames = int(round(RECORD_SECONDS_AFTER_TRIGGER * VIDEO_FPS))
                    post_trigger_frames_written = 0
                    next_frame_deadline = time.time()

                    alert_start_time = time.time()
                    state = "ALERTING"
                    consecutive_alert_frames = 0
                    consecutive_alert_color_saved = consecutive_alert_color
                    consecutive_alert_color = None

            elif state == "ALERTING":
                if video_writer is not None:
                    now = time.time()
                    # 追帧写入：把从上次写入到现在这段时间里"应该有的帧数"都补上
                    # (循环慢了就多补几帧，循环快了这次就可能一帧都不写，
                    # 保证不管实际循环速度如何，最终写入的总帧数是固定的)
                    while (next_frame_deadline <= now
                           and post_trigger_frames_written < post_trigger_target_frames):
                        video_writer.write(frame)
                        post_trigger_frames_written += 1
                        next_frame_deadline += 1.0 / VIDEO_FPS

                if post_trigger_frames_written >= post_trigger_target_frames:
                    if video_writer is not None:
                        video_writer.release()
                        video_writer = None
                    write_alert_log(consecutive_alert_color_saved, video_path)
                    send_resume_command()
                    state = "PATROLLING"

    except KeyboardInterrupt:
        logger.info("收到中断信号，正在安全退出...")
    finally:
        if video_writer is not None:
            video_writer.release()
        cap.release()
        if SHOW_WINDOW:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()