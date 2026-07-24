# -*- coding: utf-8 -*-
"""
patrol_monitor.py
全自动巡检LED监控：巡检条移动过程中持续识别LED颜色
    - 遇到 红/黄(amber) -> 发送"停止"指令给巡检条，开始录像，记录报警日志
    - 遇到 绿色 (或没检测到异常色) -> 发送"继续"指令，巡检条前进检查下一台

全程无需人工操作，脚本本身就是一个持续运行的服务（可用 systemd / supervisor 之类常驻）。

【重要】巡检条控制目前用的是"网络请求"占位实现（HTTP POST），
具体协议还没定下来，等你确定巡检条那边暴露的接口(HTTP/MQTT/串口)后，
只需要改 send_stop_command() / send_resume_command() 这两个函数内部的实现，
外面的状态机逻辑完全不用动。

【标定模式】
    python3 monitor_with_rules.py --calibrate --station <station_id> --vendor <厂商> --model "<型号>"
标定一个station的面板位置(panel_bbox)，以及(如果这个型号还没标定过)
每颗LED相对面板的位置(led_positions)。
    - panel_bbox 写入 config/runtime_config.json 对应station_id下
    - led_positions 写入 knowledge/<vendor>_<model>.json (跟rules同一个文件)
同一个vendor+model只需要标定一次led_positions，以后新的station只要
panel_bbox对得上、vendor/model选对，就能直接复用，不用重新框每颗LED。

【正常巡检模式】不加--calibrate参数，直接:
    python3 monitor_with_rules.py
会自动尝试用标定好的坐标做精确定点检测；如果当前station还没标定，
会退化成旧的整片ROI找轮廓的检测方式(不会直接跑不起来，但精度不如标定后)。

配置项都在最上面 CONFIG 区域，先改这些：
    RAIL_STOP_URL / RAIL_RESUME_URL   -> 巡检条的接口地址（占位，需替换成真实地址）
    ALERT_LOG_DIR / ALERT_VIDEO_DIR   -> 报警日志和视频的保存路径
    CONSECUTIVE_FRAMES_TO_CONFIRM     -> 连续几帧检测到同一异常颜色才真正报警（防误判抖动）
    RECORD_SECONDS_AFTER_TRIGGER      -> 报警后继续录多少秒才停止录像、恢复巡检
"""

import argparse
import os
import sys
import cv2

import json
import re
import time
import logging
import requests
import numpy as np
from pathlib import Path
from collections import deque
from datetime import datetime

from led_knowledge_lookup import LedKnowledgeBase
from config_manager import (
    get_runtime_config,
    load_runtime_config,
    get_current_station,
    set_station_panel_bbox,
    set_current_station,
)

# ============ CONFIG ============
RAIL_STOP_URL = "http://<rail-controller-ip>/api/stop"      # TODO: 换成真实地址
RAIL_RESUME_URL = "http://<rail-controller-ip>/api/resume"  # TODO: 换成真实地址
RAIL_REQUEST_TIMEOUT = 2.0   # 秒，网络请求超时时间，避免卡住主循环

CAMERA_SOURCE = "rtsp://admin:jiandandian@1@192.168.1.129:554/stream1"
# 本地USB摄像头填数字(0/1/2...)；网络摄像头(巡检条自带的那种)填RTSP地址字符串
SHOW_WINDOW = True           # 生产环境建议 False（无人值守，不需要显示画面）；调试时改 True
# 网页后台通过这个环境变量强制关闭窗口(以子进程方式在后台跑，没有屏幕可显示)，
# 不设置这个环境变量时，行为跟以前完全一样，终端直接跑不受影响
if os.environ.get("PATROL_SHOW_WINDOW") is not None:
    SHOW_WINDOW = os.environ.get("PATROL_SHOW_WINDOW") == "1"

ALERT_LOG_DIR = Path("alerts/logs")
ALERT_VIDEO_DIR = Path("alerts/videos")
OBSERVATION_LOG_DIR = Path("alerts/observations")   # 非报警颜色(绿色、蓝色等)的轻量级记录，只写日志不录像

# ---------- 旧文件自动清理 ----------
RETENTION_DAYS = 30              # 超过这么多天的旧录像会被自动删除
CLEANUP_CHECK_INTERVAL_SECONDS = 3600   # 每隔多久检查一次要不要清理(不用每帧都扫一遍目录)
OBSERVATION_LOG_MIN_INTERVAL = 30.0   # 同一个颜色至少间隔这么多秒才重复记一次，避免刷屏

CONSECUTIVE_FRAMES_TO_CONFIRM = 5     # 连续几帧都检测到红/黄，才判定为真实报警（防误判抖动）
RECORD_SECONDS_AFTER_TRIGGER = 6.5    # 触发确认后，继续录多少秒才停止录像、恢复巡检
PRE_TRIGGER_BUFFER_SECONDS = 2.0      # 报警前缓冲：把触发前2秒的画面也存进视频，方便看清"怎么变的"
VIDEO_FPS = 20.0

# ---------- 闪烁检测(仅用于观察记录里标注"常亮/闪烁"，不影响红黄的报警触发) ----------
BLINK_WINDOW_SECONDS = 2.0
BLINK_MIN_SAMPLES = 10          # 窗口里至少要有这么多帧样本才敢下判断，不够就是unknown
BLINK_SOLID_RATIO = 0.85        # 出现比例 >= 这个值 判定为常亮(solid)
BLINK_MIN_RATIO_TO_COUNT = 0.15  # 出现比例低于这个值，说明太偶尔出现了，不够格判定成"闪烁"，也是unknown

# ---------- 扫描区域(ROI)：仅在"当前station还没标定LED位置"时作为退化方案使用 ----------
# 标定好之后(led_positions + panel_bbox都有了)，检测会自动改用精确定点模式，
# 不再依赖这个大范围ROI找轮廓；这个值只在退化模式下生效
SCAN_ROI = (2100, 650, 2450, 850)

DEBUG_SHOW_AREA = False   # 标定面积用的调试开关，正常巡检时保持False
DEBUG_SHOW_HSV = True     # 调试模式：显示每个候选框的H/S数值，鼠标点击画面打印该点HSV

# ---------- 亮度/形状参数(退化模式下的轮廓检测用) ----------
BRIGHT_THRESHOLD = 254
MIN_AREA = 15
MAX_AREA = 200000
MIN_CIRCULARITY = 0.3
PADDING = 4

# ---------- 颜色分类参数 ----------
MIN_SATURATION_FOR_COLOR = 200
COLOR_PIXEL_MIN_RATIO = 0.15
ALERT_HUE_RANGE = (0, 18)
GREEN_HUE_RANGE = (58, 70)
BLUE_HUE_RANGE = (95, 130)
OK_COLORS = {"green"}

ALERT_COLORS_OVERRIDE = {"amber"}   # 手动指定：红色和琥珀色(amber)都触发报警

KNOWLEDGE_DIR = "knowledge"
# =================================
_LOG_LEVEL_NAME = os.environ.get("PATROL_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s\n",
)
logger = logging.getLogger("patrol_monitor")


def ensure_dirs():
    for d in (ALERT_LOG_DIR, ALERT_VIDEO_DIR, OBSERVATION_LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def cleanup_old_videos():
    """删掉超过 RETENTION_DAYS 天的旧录像文件，避免长期无人值守跑到把硬盘写满。"""
    cutoff = time.time() - RETENTION_DAYS * 86400
    deleted_count = 0
    deleted_bytes = 0
    for video_file in ALERT_VIDEO_DIR.glob("*.mp4"):
        try:
            mtime = video_file.stat().st_mtime
            if mtime < cutoff:
                size = video_file.stat().st_size
                video_file.unlink()
                deleted_count += 1
                deleted_bytes += size
        except OSError as e:
            logger.error(f"清理旧录像失败: {video_file} -> {e}")

    if deleted_count:
        logger.warning(
            f"清理了 {deleted_count} 个超过{RETENTION_DAYS}天的旧录像，"
            f"释放约 {deleted_bytes / 1024 / 1024:.1f} MB 空间")


FAULT_COMPONENT_KEYWORDS = re.compile(
    r"fault|error|warn|alarm|fail|故障|异常|告警|错误", re.IGNORECASE)


def suggest_alert_colors(knowledge_base: "LedKnowledgeBase", vendor: str, model: str):
    knowledge_base.load(vendor, model)
    slug = knowledge_base._slug(vendor, model)
    data = knowledge_base._cache.get(slug)
    if not data:
        return set(), []

    suggested = set()
    evidence = []
    for rule in data.get("rules", []):
        component = rule.get("component", "")
        color = normalize_color(rule.get("color"))   # 归一化：yellow/orange也算amber
        if not color or color == "green":
            continue
        if FAULT_COMPONENT_KEYWORDS.search(component):
            suggested.add(color)
            evidence.append(f"{color} <- [{component}] {rule.get('description', '')}")

    return suggested, evidence


def resolve_alert_colors(knowledge_base, default_vendor, default_model, station_vendor_model):
    """得到最终生效的 ALERT_COLORS：手动覆盖优先，否则用知识库自动推导的结果。"""
    if ALERT_COLORS_OVERRIDE is not None:
        logger.debug(f"报警颜色使用手动覆盖设置: {ALERT_COLORS_OVERRIDE}")
        return set(ALERT_COLORS_OVERRIDE)

    all_vendor_models = {(default_vendor, default_model)}
    all_vendor_models.update(station_vendor_model.values())

    combined = set()
    for vendor, model in all_vendor_models:
        suggested, evidence = suggest_alert_colors(knowledge_base, vendor, model)
        if suggested:
            logger.debug(f"{vendor} {model} 自动推导出的报警颜色: {suggested}")
            for line in evidence:
                logger.debug(f"    依据: {line}")
        else:
            logger.warning(
                f"{vendor} {model} 没能从knowledge文件里自动推导出报警颜色"
                f"(可能是knowledge文件不存在，或者组件名里没有fault/error这类关键词)，"
                f"建议检查，或者用 ALERT_COLORS_OVERRIDE 手动指定")
        combined |= suggested

    if not combined:
        logger.error("最终没有任何报警颜色生效！巡检脚本会把所有颜色都当成正常，"
                      "不会触发任何报警，请检查knowledge文件或设置 ALERT_COLORS_OVERRIDE")
    return combined


def classify_color(hsv_roi):
    h, s, v = cv2.split(hsv_roi)
    colored_mask = s > MIN_SATURATION_FOR_COLOR

    if np.count_nonzero(colored_mask) == 0:
        return None, None, None

    hues = h[colored_mask]
    sats = s[colored_mask]

    median_hue = int(np.median(hues))
    median_sat = int(np.median(sats))

    if 0 <= median_hue <= 18:
        return "amber", median_hue, median_sat
    elif 58 <= median_hue <= 70:
        return "green", median_hue, median_sat
    elif 95 <= median_hue <= 130:
        return "blue", median_hue, median_sat

    return None, median_hue, median_sat


def detect_led_candidates(frame):
    """退化模式：在SCAN_ROI整片区域里找轮廓。仅当当前station还没标定
    LED位置时使用；标定完之后正常巡检走 detect_led_candidates_by_positions()。
    返回格式跟标定模式统一成9元组，component_name固定是None，方便调用方
    (main循环)不用区分是哪种模式来的候选框，用同一套代码处理。
    """
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
        color_label, median_hue, median_sat = classify_color(roi)
        if color_label is None:
            if not DEBUG_SHOW_AREA and not DEBUG_SHOW_HSV:
                continue
            color_label = "?"

        found.append((x + roi_offset_x, y + roi_offset_y, w, h, color_label, int(area),
                      median_hue, median_sat, None))

    return found


def resolve_absolute_led_rois(panel_bbox, led_positions):
    """把标定好的LED相对坐标(0~1)，按当前station实测的panel_bbox换算成
    画面里的绝对像素坐标。
    panel_bbox: {"x":,"y":,"w":,"h":}
    led_positions: [{"component_name":,"rel_x":,"rel_y":,"rel_w":,"rel_h":}, ...]
    返回: [(component_name, x, y, w, h), ...]
    """
    px, py, pw, ph = panel_bbox["x"], panel_bbox["y"], panel_bbox["w"], panel_bbox["h"]
    rois = []
    for item in led_positions:
        x = int(round(px + item["rel_x"] * pw))
        y = int(round(py + item["rel_y"] * ph))
        w = int(round(item["rel_w"] * pw))
        h = int(round(item["rel_h"] * ph))
        rois.append((item["component_name"], x, y, w, h))
    return rois


def detect_led_candidates_by_positions(frame, absolute_rois):
    """标定模式：按标定好的LED坐标逐个取色判断，代替"整片区域找轮廓"。
    好处：报警时能精确定位是哪颗LED(component_name)，而不是笼统的"检测到红色"。
    返回: [(x,y,w,h,color_label,area,median_hue,median_sat,component_name), ...]
    """
    hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    frame_h, frame_w = frame.shape[:2]
    found = []
    for component_name, x, y, w, h in absolute_rois:
        x = max(0, min(x, frame_w - 1))
        y = max(0, min(y, frame_h - 1))
        w = max(1, min(w, frame_w - x))
        h = max(1, min(h, frame_h - y))
        roi = hsv_full[y:y + h, x:x + w]
        if roi.size == 0:
            continue

        color_label, median_hue, median_sat = classify_color(roi)
        if color_label is None:
            if not DEBUG_SHOW_HSV:
                continue
            color_label = "?"

        found.append((x, y, w, h, color_label, w * h, median_hue, median_sat, component_name))

    return found


def _is_placeholder_url(url: str) -> bool:
    return "<" in url and ">" in url


def send_stop_command(reason_color, station_id="unknown"):
    if _is_placeholder_url(RAIL_STOP_URL):
        logger.warning("RAIL_STOP_URL还是占位地址(<rail-controller-ip>)，跳过实际发送——"
                       "巡检条不会真的停下来！这不是正常状态，请尽快配置真实地址")
        return
    try:
        resp = requests.post(
            RAIL_STOP_URL,
            json={"action": "stop", "reason": reason_color, "station": station_id},
            timeout=RAIL_REQUEST_TIMEOUT,
        )
        logger.debug(f"已发送停止指令 (reason={reason_color}) -> 响应状态 {resp.status_code}")
    except Exception as e:
        logger.error(f"停止指令发送失败: {e}（巡检条可能未真正停止，请检查通信链路）")


def send_resume_command(station_id="unknown"):
    if _is_placeholder_url(RAIL_RESUME_URL):
        logger.warning("RAIL_RESUME_URL还是占位地址(<rail-controller-ip>)，跳过实际发送——"
                       "巡检条不会真的收到继续指令！这不是正常状态，请尽快配置真实地址")
        return
    try:
        resp = requests.post(
            RAIL_RESUME_URL,
            json={"action": "resume", "station": station_id},
            timeout=RAIL_REQUEST_TIMEOUT,
        )
        logger.debug(f"已发送继续指令 -> 响应状态 {resp.status_code}")
    except Exception as e:
        logger.error(f"继续指令发送失败: {e}")


def estimate_color_pattern(presence_history, color):
    if len(presence_history) < BLINK_MIN_SAMPLES:
        return "unknown"

    total = len(presence_history)
    present_count = sum(1 for _, colors in presence_history if color in colors)
    ratio = present_count / total

    if ratio >= BLINK_SOLID_RATIO:
        return "solid"
    if ratio < BLINK_MIN_RATIO_TO_COUNT:
        return "unknown"
    return "blink"


def write_observation_log(color_label, explanation):
    ts = datetime.now()
    record = {
        "timestamp": ts.isoformat(),
        "color": color_label,
        "explanation": explanation,
    }
    log_file = OBSERVATION_LOG_DIR / f"{ts:%Y-%m-%d}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.debug(f"非报警观察记录: {record}")


def write_alert_log(color_label, pattern, video_path, explanation, components=None):
    ensure_dirs()
    ts = datetime.now()
    record = {
        "timestamp": ts.isoformat(),
        "color": color_label,
        "pattern": pattern,
        "video_file": str(video_path),
        "explanation": explanation,
        "components": components or [],   # 【新增】标定模式下能精确记录是哪几颗LED触发的
    }
    log_file = ALERT_LOG_DIR / f"{ts:%Y-%m-%d}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.debug(f"报警记录已写入: {log_file} -> {record}")


# ============ 标定模式：交互式框选面板 + LED位置 ============

def _interactive_click_two_points(frame, window_title):
    """弹窗让人工点两次(左上角->右下角)，返回(x,y,w,h)，按q取消返回None"""
    points = []
    display = frame.copy()

    def _on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 2:
            points.append((x, y))
            cv2.circle(display, (x, y), 5, (0, 0, 255), -1)
            cv2.imshow(window_title, display)

    cv2.imshow(window_title, display)
    cv2.setMouseCallback(window_title, _on_click)

    while len(points) < 2:
        key = cv2.waitKey(20) & 0xFF
        if key == ord('q'):
            cv2.destroyWindow(window_title)
            return None

    cv2.destroyWindow(window_title)
    (x1, y1), (x2, y2) = points
    x, y = min(x1, x2), min(y1, y2)
    w, h = abs(x2 - x1), abs(y2 - y1)
    if w == 0 or h == 0:
        return None
    return (x, y, w, h)


def _interactive_calibrate_leds(frame, panel_bbox):
    """
    交互式框选面板内每一颗LED的绝对坐标，换算成相对panel_bbox的比例坐标。
    操作：每颗LED先点左上角、再点右下角，框完在终端输入component_name
    (建议跟knowledge文件里的component名对齐，方便报警时精确查故障说明)，
    回车后继续框下一颗；还没开始点下一颗时按q结束。
    返回: [{"component_name":..., "rel_x":..., "rel_y":..., "rel_w":..., "rel_h":...}, ...]
    """
    px, py, pw, ph = panel_bbox["x"], panel_bbox["y"], panel_bbox["w"], panel_bbox["h"]
    display = frame.copy()
    cv2.rectangle(display, (px, py), (px + pw, py + ph), (255, 0, 0), 2)

    window_name = "框选每颗LED：先点左上角再点右下角，每颗框完在终端输入名字（q结束）"
    positions = []
    current_points = []

    def _on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            current_points.append((x, y))
            cv2.circle(display, (x, y), 4, (0, 255, 255), -1)
            cv2.imshow(window_name, display)

    cv2.imshow(window_name, display)
    cv2.setMouseCallback(window_name, _on_click)

    print("=" * 50)
    print("开始框选LED。每颗LED：先点左上角，再点右下角。")
    print("框完一颗后，回到终端输入这颗LED的名字（比如 power_led）。")
    print("还没开始点下一颗时，在弹窗里按 q 可结束标定。")
    print("=" * 50)

    while True:
        key = cv2.waitKey(20) & 0xFF
        if key == ord('q') and len(current_points) == 0:
            break

        if len(current_points) == 2:
            (x1, y1), (x2, y2) = current_points
            x, y = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)

            if w == 0 or h == 0:
                print("这一颗框的宽或高是0，忽略，请重新框这一颗")
                current_points = []
                continue

            name = input(f"这一颗LED的名字(component_name) [x={x},y={y},w={w},h={h}]: ").strip()
            if not name:
                print("名字不能为空，这一颗作废，请重新框")
                current_points = []
                continue

            positions.append({
                "component_name": name,
                "rel_x": (x - px) / pw,
                "rel_y": (y - py) / ph,
                "rel_w": w / pw,
                "rel_h": h / ph,
            })
            cv2.rectangle(display, (x, y), (x + w, y + h), (0, 0, 255), 2)
            cv2.putText(display, name, (x, y - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
            cv2.imshow(window_name, display)
            print(f"已记录: {name}，当前共 {len(positions)} 颗。继续框下一颗，或按q结束")
            current_points = []

    cv2.destroyWindow(window_name)
    return positions


def run_calibration_mode():
    """
    标定入口。用法:
        python3 monitor_with_rules.py --calibrate --station <station_id> \
            --vendor <厂商> --model "<型号>"
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--station", type=str, required=True, help="station_id，比如 rack_03_slot_12")
    parser.add_argument("--vendor", type=str, required=True)
    parser.add_argument("--model", type=str, required=True)
    args = parser.parse_args()

    station_id = args.station
    vendor = args.vendor
    model = args.model

    print(f"标定 station={station_id}  vendor={vendor}  model={model}")

    cap = cv2.VideoCapture(CAMERA_SOURCE)
    if not cap.isOpened():
        print("摄像头打不开，先解决这个再标定")
        return
    ret, frame = cap.read()
    cap.release()
    if not ret:
        print("读取帧失败")
        return

    print(f"抓到一帧，画面尺寸: {frame.shape[1]}x{frame.shape[0]}")

    # ---- 第一步：标定面板整体框 ----
    print("请在弹出的窗口里，先点面板左上角，再点右下角（按q取消）")
    panel_bbox_tuple = _interactive_click_two_points(frame, "点击面板左上角 -> 右下角 (q取消)")
    if panel_bbox_tuple is None:
        print("取消了，退出")
        return
    px, py, pw, ph = panel_bbox_tuple
    panel_bbox = {"x": px, "y": py, "w": pw, "h": ph}
    print(f"面板框标定完成: {panel_bbox}")

    # ---- 第二步：这个型号如果已经标定过LED位置，问是否复用 ----
    knowledge_base = LedKnowledgeBase(knowledge_dir=KNOWLEDGE_DIR)
    existing_positions = knowledge_base.get_led_positions(vendor, model)

    if existing_positions:
        print(f"{vendor} {model} 已经标定过 {len(existing_positions)} 颗LED的位置。")
        redo = input("要重新标定吗？(y=重新标定 / 直接回车=复用已有位置): ").strip().lower()
        led_positions = _interactive_calibrate_leds(frame, panel_bbox) if redo == "y" else existing_positions
    else:
        print(f"{vendor} {model} 还没有标定过LED位置，开始标定每一颗LED")
        led_positions = _interactive_calibrate_leds(frame, panel_bbox)

    if not led_positions:
        print("没有任何LED位置数据，标定终止")
        return

    # ---- 第三步：保存 ----
    knowledge_base.save_led_positions(vendor, model, led_positions)
    print(f"已把 {len(led_positions)} 颗LED的位置写入 knowledge/{knowledge_base._slug(vendor, model)}.json")

    set_station_panel_bbox(station_id, vendor, model, panel_bbox)
    print(f"已把station={station_id}的面板锚点框写入 config/runtime_config.json")

    config = load_runtime_config()
    if config and len(config.get("stations", {})) == 1:
        set_current_station(station_id)
        print(f"当前只有这一个station，已自动设为current_station")

    # ---- 第四步：画出来确认 ----
    absolute_rois = resolve_absolute_led_rois(panel_bbox, led_positions)
    display = frame.copy()
    cv2.rectangle(display, (px, py), (px + pw, py + ph), (255, 0, 0), 2)
    for name, x, y, w, h in absolute_rois:
        cv2.rectangle(display, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(display, name, (x, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    cv2.imshow("标定结果确认（按任意键关闭）", display)
    print("按任意键关闭窗口，标定完成")
    cv2.waitKey(0)
    cv2.destroyAllWindows()


# ============ 正常巡检模式 ============

def main():
    ensure_dirs()

    if _is_placeholder_url(RAIL_STOP_URL) or _is_placeholder_url(RAIL_RESUME_URL):
        logger.warning(
            "=" * 60 + "\n"
            "启动检查: RAIL_STOP_URL / RAIL_RESUME_URL 还是占位地址！\n"
            "这个状态下巡检条不会真的被停下来或恢复，只是脚本自己在\n"
            "本地判断颜色、写日志——如果这是正式部署环境，请立刻停下来\n"
            "把这两个地址改成真实地址，否则出了故障也不会真的停车\n"
            + "=" * 60)

    runtime_config = get_runtime_config()

    default_vendor = runtime_config["default"]["vendor"]
    default_model = runtime_config["default"]["model"]

    station_vendor_model = {}
    for station, vm in runtime_config["stations"].items():
        station_vendor_model[station] = (vm["vendor"], vm["model"])

    knowledge_base = LedKnowledgeBase(knowledge_dir=KNOWLEDGE_DIR)
    alert_colors = resolve_alert_colors(knowledge_base, default_vendor, default_model, station_vendor_model)

    # ---- 确定当前station，尝试加载标定好的LED绝对坐标 ----
    current_station_id = get_current_station(runtime_config)
    absolute_led_rois = None
    current_vendor, current_model = default_vendor, default_model

    if current_station_id is None:
        logger.warning(
            "无法确定当前station（config里没有current_station，也没有唯一一个"
            "已标定panel_bbox的station）。将退化使用旧的SCAN_ROI整片区域检测方式，"
            "建议先跑: python3 monitor_with_rules.py --calibrate --station <id> "
            "--vendor <vendor> --model \"<model>\" 完成标定")
    else:
        station_entry = runtime_config["stations"].get(current_station_id, {})
        panel_bbox = station_entry.get("panel_bbox")
        current_vendor = station_entry.get("vendor", default_vendor)
        current_model = station_entry.get("model", default_model)

        if not panel_bbox:
            logger.warning(
                f"station={current_station_id} 还没有标定panel_bbox，"
                f"将退化使用旧的SCAN_ROI整片区域检测方式，建议先标定")
        else:
            led_positions = knowledge_base.get_led_positions(current_vendor, current_model)
            if not led_positions:
                logger.warning(
                    f"{current_vendor} {current_model} 还没有标定led_positions，"
                    f"将退化使用旧的SCAN_ROI整片区域检测方式，建议先标定")
            else:
                absolute_led_rois = resolve_absolute_led_rois(panel_bbox, led_positions)
                logger.debug(
                    f"station={current_station_id} 已加载{len(absolute_led_rois)}颗LED的标定坐标，"
                    f"使用精确定点检测模式")

    cap = cv2.VideoCapture(CAMERA_SOURCE)
    if not cap.isOpened():
        if isinstance(CAMERA_SOURCE, str):
            logger.error(
                f"无法打开摄像头，退出。CAMERA_SOURCE当前是网络地址: {CAMERA_SOURCE}\n"
                f"排查建议: 1) 先用VLC的'打开网络串流'确认这个地址真的能播放画面 "
                f"2) 检查账号密码、端口、路径是否正确 "
                f"3) 确认这台电脑跟摄像头在同一个网络、能ping通")
        else:
            logger.error("无法打开摄像头，退出")
        return

    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480

    buffer_maxlen = int(PRE_TRIGGER_BUFFER_SECONDS * VIDEO_FPS)
    frame_buffer = deque(maxlen=buffer_maxlen)

    color_presence_history = deque()

    state = "PATROLLING"
    consecutive_alert_frames = 0
    consecutive_alert_color = None
    alert_start_time = None
    video_writer = None
    last_observed_color = None
    last_observed_pattern = None
    last_observation_log_time = 0.0
    video_path = None

    cleanup_old_videos()
    last_cleanup_check_time = time.time()

    logger.debug("巡检监控已启动，全自动运行中...")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                logger.error("读取摄像头帧失败，重试中...")
                time.sleep(0.5)
                continue

            now_for_cleanup = time.time()
            if now_for_cleanup - last_cleanup_check_time >= CLEANUP_CHECK_INTERVAL_SECONDS:
                cleanup_old_videos()
                last_cleanup_check_time = now_for_cleanup

            frame_buffer.append(frame.copy())

            if absolute_led_rois is not None:
                candidates = detect_led_candidates_by_positions(frame, absolute_led_rois)
            else:
                candidates = detect_led_candidates(frame)

            colors_found = {c[4] for c in candidates}

            now_for_history = time.time()
            color_presence_history.append((now_for_history, frozenset(colors_found)))
            while (color_presence_history
                   and now_for_history - color_presence_history[0][0] > BLINK_WINDOW_SECONDS):
                color_presence_history.popleft()

            if SHOW_WINDOW:
                display = frame.copy()
                for (x, y, w, h, color_label, area, median_hue, median_sat, component_name) in candidates:
                    if color_label != "amber":
                        continue

                    cv2.rectangle(display, (x, y), (x + w, y + h), (0, 0, 255), 2)
                    label = f"{color_label} H={median_hue} S={median_sat}"
                    if component_name:
                        label += f" [{component_name}]"

                    cv2.putText(display, label, (x, y - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)

                cv2.putText(display, f"state={state}", (10, frame_h - 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
                if DEBUG_SHOW_AREA:
                    cv2.putText(display, "DEBUG_SHOW_AREA=True 标定模式：先记录数值再关掉", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
                if DEBUG_SHOW_HSV:
                    cv2.putText(display, "DEBUG_SHOW_HSV=True 标定模式：点画面可打印该点H/S值", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
                cv2.imshow("patrol_monitor (debug)", display)

                if DEBUG_SHOW_HSV:
                    def _on_mouse_click(event, mx, my, flags, param):
                        if event == cv2.EVENT_LBUTTONDOWN:
                            hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                            if 0 <= my < hsv_frame.shape[0] and 0 <= mx < hsv_frame.shape[1]:
                                ph_, ps_, pv_ = hsv_frame[my, mx]
                                print(f"点击坐标=({mx},{my})  H={ph_} S={ps_} V={pv_}")
                    cv2.setMouseCallback("patrol_monitor (debug)", _on_mouse_click)

                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            if DEBUG_SHOW_AREA:
                continue

            if state == "PATROLLING":
                alert_hits = colors_found & alert_colors
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

                non_alert_colors = colors_found - alert_colors - OK_COLORS
                if non_alert_colors:
                    observed_color = sorted(non_alert_colors)[0]
                    observed_pattern = estimate_color_pattern(color_presence_history, observed_color)
                    now_ts = time.time()
                    color_changed = observed_color != last_observed_color
                    interval_passed = (now_ts - last_observation_log_time) >= OBSERVATION_LOG_MIN_INTERVAL
                    still_gathering = color_changed and observed_pattern == "unknown"
                    if (color_changed or interval_passed) and not still_gathering:
                        obs_pattern_for_lookup = observed_pattern if observed_pattern != "unknown" else None
                        obs_explanation = knowledge_base.describe(
                            current_vendor, current_model, color=observed_color, pattern=obs_pattern_for_lookup)
                        write_observation_log(observed_color, obs_explanation)
                        last_observed_color = observed_color
                        last_observed_pattern = observed_pattern
                        last_observation_log_time = now_ts

                if consecutive_alert_frames >= CONSECUTIVE_FRAMES_TO_CONFIRM:
                    alert_pattern = estimate_color_pattern(color_presence_history, consecutive_alert_color)
                    alert_pattern_for_lookup = alert_pattern if alert_pattern != "unknown" else None
                    explanation = knowledge_base.describe(
                        current_vendor, current_model, color=consecutive_alert_color,
                        pattern=alert_pattern_for_lookup)

                    # 【新增】标定模式下能精确列出是哪几颗LED触发的这次报警
                    alert_components = sorted({
                        c[8] for c in candidates
                        if c[4] == consecutive_alert_color and c[8]
                    })

                    logger.warning(
                        f"检测到异常颜色: {consecutive_alert_color}({alert_pattern})"
                        f"{' 触发LED=' + str(alert_components) if alert_components else ''}，"
                        f"触发停止+录像\n说明书查表结果: {explanation}")
                    send_stop_command(consecutive_alert_color, station_id=current_station_id or "unknown")

                    ts = datetime.now()
                    video_path = ALERT_VIDEO_DIR / f"alert_{ts:%Y%m%d_%H%M%S}.mp4"

                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    video_writer = cv2.VideoWriter(
                        str(video_path), fourcc, VIDEO_FPS, (frame_w, frame_h))
                    for buffered_frame in frame_buffer:
                        video_writer.write(buffered_frame)

                    post_trigger_target_frames = int(round(RECORD_SECONDS_AFTER_TRIGGER * VIDEO_FPS))
                    post_trigger_frames_written = 0
                    next_frame_deadline = time.time()

                    alert_start_time = time.time()
                    state = "ALERTING"
                    consecutive_alert_frames = 0
                    consecutive_alert_color_saved = consecutive_alert_color
                    alert_pattern_saved = alert_pattern
                    alert_explanation_saved = explanation
                    alert_components_saved = alert_components
                    consecutive_alert_color = None

            elif state == "ALERTING":
                if video_writer is not None:
                    now = time.time()
                    while (next_frame_deadline <= now
                           and post_trigger_frames_written < post_trigger_target_frames):
                        video_writer.write(frame)
                        post_trigger_frames_written += 1
                        next_frame_deadline += 1.0 / VIDEO_FPS

                if post_trigger_frames_written >= post_trigger_target_frames:
                    if video_writer is not None:
                        video_writer.release()
                        video_writer = None
                    write_alert_log(consecutive_alert_color_saved, alert_pattern_saved, video_path,
                                     alert_explanation_saved, alert_components_saved)
                    send_resume_command(station_id=current_station_id or "unknown")
                    state = "PATROLLING"

    except KeyboardInterrupt:
        logger.debug("收到中断信号，正在安全退出...")
    finally:
        if video_writer is not None:
            video_writer.release()
        cap.release()
        if SHOW_WINDOW:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        run_calibration_mode()
    else:
        main()