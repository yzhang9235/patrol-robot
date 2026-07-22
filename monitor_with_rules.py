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
import re
import time
import logging
import requests
import numpy as np
from pathlib import Path
from collections import deque
from datetime import datetime

from led_knowledge_lookup import LedKnowledgeBase
from config_manager import get_runtime_config

# ============ CONFIG ============
RAIL_STOP_URL = "http://<rail-controller-ip>/api/stop"      # TODO: 换成真实地址
RAIL_RESUME_URL = "http://<rail-controller-ip>/api/resume"  # TODO: 换成真实地址
RAIL_REQUEST_TIMEOUT = 2.0   # 秒，网络请求超时时间，避免卡住主循环

CAMERA_SOURCE = "rtsp://admin:jiandandian@1@192.168.1.129:554/stream1"
# 本地USB摄像头填数字(0/1/2...)；网络摄像头(巡检条自带的那种)填RTSP地址字符串，
# 格式一般是: "rtsp://用户名:密码@摄像头IP:554/路径"
# 具体路径每个厂商不一样，先用VLC的"打开网络串流"功能试出正确地址，
# 确认能播放画面之后，把同一个地址填在这里，例如:
# CAMERA_SOURCE = "rtsp://admin:12345@192.168.1.100:554/stream1"
SHOW_WINDOW = True           # 生产环境建议 False（无人值守，不需要显示画面）；调试时改 True

ALERT_LOG_DIR = Path("alerts/logs")
ALERT_VIDEO_DIR = Path("alerts/videos")
OBSERVATION_LOG_DIR = Path("alerts/observations")   # 非报警颜色(绿色、蓝色等)的轻量级记录，只写日志不录像

# ---------- 旧文件自动清理 ----------
# 视频文件占空间最大，长期无人值守跑下去迟早把硬盘写满，加个按天数清理的机制。
# 日志是纯文本，占用很小，不清理问题也不大，这里只清理视频；
# 如果想连日志一起清，把 ALERT_LOG_DIR / OBSERVATION_LOG_DIR 也加进
# cleanup_old_files 的调用列表里就行
RETENTION_DAYS = 30              # 超过这么多天的旧录像会被自动删除
CLEANUP_CHECK_INTERVAL_SECONDS = 3600   # 每隔多久检查一次要不要清理(不用每帧都扫一遍目录)
OBSERVATION_LOG_MIN_INTERVAL = 30.0   # 同一个颜色至少间隔这么多秒才重复记一次，避免刷屏

CONSECUTIVE_FRAMES_TO_CONFIRM = 2    # 连续5帧都检测到红/黄，才判定为真实报警（约0.15秒@30fps）
RECORD_SECONDS_AFTER_TRIGGER = 6.5    # 触发确认后，继续录多少秒才停止录像、恢复巡检
PRE_TRIGGER_BUFFER_SECONDS = 2.0      # 报警前缓冲：把触发前2秒的画面也存进视频，方便看清"怎么变的"
# 两者相加 = 8.5秒，比模拟器10秒的切换周期留了约1.5秒安全余量
# (给确认延迟、指令发送等留出空间，避免录像跨到下一个unit)
VIDEO_FPS = 20.0

# ---------- 闪烁检测(仅用于观察记录里标注"常亮/闪烁"，不影响红黄的报警触发) ----------
# 原理很简单：滚动记录最近 BLINK_WINDOW_SECONDS 秒里每一帧"这个颜色有没有出现"，
# 出现比例接近100%就是常亮，比例在中间(有时候有、有时候没有)就是闪烁，
# 数据不够或者太少见就先标"unknown"，不瞎猜
BLINK_WINDOW_SECONDS = 2.0
BLINK_MIN_SAMPLES = 10          # 窗口里至少要有这么多帧样本才敢下判断，不够就是unknown
BLINK_SOLID_RATIO = 0.85        # 出现比例 >= 这个值 判定为常亮(solid)
BLINK_MIN_RATIO_TO_COUNT = 0.15  # 出现比例低于这个值，说明太偶尔出现了，不够格判定成"闪烁"，也是unknown

# ---------- 扫描区域(ROI) ----------
# 只在这个区域内找LED，区域外的一律忽略(手、背景反光、走廊灯光都不会被扫到)
# 设成 None 表示扫全画面(不建议正式使用)；建议按巡检条实际工作距离下，
# LED出现的画面位置实测填一个 (x1, y1, x2, y2) 矩形，留一点余量防止对位误差
SCAN_ROI = (2100, 650, 2450, 850)  # 例如: (200, 150, 1000, 500)

# 标定模式：True时不做面积过滤，把每个候选框的实测面积打印在框旁边，
# 用来在实际工作距离下读出真实的面积数值，标定完 MIN_AREA/MAX_AREA 后改回 False
DEBUG_SHOW_AREA = False

# 调试模式：True时把每个候选框的中位数H(色相)/S(饱和度)值显示在框旁边，
# 而且不做颜色分类过滤(哪怕最终判定不出是哪个颜色也照样显示数值)。
# 用来对比"真实LED"和"背景反光/金属高光"的H/S数值到底差多少，
# 从而精确收紧 BLUE_HUE_RANGE / MIN_SATURATION_FOR_COLOR 这些阈值，
# 而不是凭感觉调。标定完记得改回 False。
DEBUG_SHOW_HSV = True

# ---------- 亮度/形状参数 ----------
# 下面这些面积参数要按巡检条"实际工作距离"下实测的LED像素大小来调，
# 不要用近距离测试的结果(比如拿手机怼近镜头拍)，那样得出的MAX_AREA会偏小，
# 导致真实工作距离下太大或太小都被误判成反光/白墙滤掉
BRIGHT_THRESHOLD = 254
MIN_AREA = 15
MAX_AREA = 200000   # 大幅放宽：反光过滤已经交给饱和度判断(classify_color)负责，
                     # 这里的面积上限只用来防止"整片画面都过曝"这种极端情况，
                     # 不再承担区分近距离大LED和反光的职责，避免每天因光线/
                     # 距离的微小差异反复失效
MIN_CIRCULARITY = 0.3
PADDING = 4

# ---------- 颜色分类参数 ----------
MIN_SATURATION_FOR_COLOR = 200
COLOR_PIXEL_MIN_RATIO = 0.15
ALERT_HUE_RANGE = (0, 18)
GREEN_HUE_RANGE = (58, 70)
BLUE_HUE_RANGE = (95, 130)
OK_COLORS = {"green"}


# ALERT_COLORS 不再是写死的全局常量：不同厂商说明书里同一个颜色的含义可能完全
# 不一样(比如这次NVIDIA的蓝色是ID识别灯，属于正常操作，不该触发报警)，所以
# "哪些颜色算异常"改成程序启动时根据当前厂商的knowledge文件自动推导，
# 推导逻辑见下面的 suggest_alert_colors()。如果自动推导的结果不对，
# 可以用 ALERT_COLORS_OVERRIDE 手动覆盖(填了就完全以这个为准，不再看推导结果)。
ALERT_COLORS_OVERRIDE = {"amber"}   # 手动指定：红色和琥珀色(amber，内部归到yellow这个桶)都触发报警

# ---------- LED含义知识库(Vendor Manual Parser生成的knowledge schema) ----------
# 用 build_led_knowledge.py 解析说明书生成 knowledge/<vendor>_<model>.json 后，
# 这里指定默认厂商型号；如果巡检条路径上不同server是不同厂商/型号，
# 在 STATION_VENDOR_MODEL 里按 station_id 覆盖，不用改代码逻辑，加一行配置就行
KNOWLEDGE_DIR = "knowledge"
runtime_config = None
# =================================

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s\n",
)
logger = logging.getLogger("patrol_monitor")


def ensure_dirs():
    for d in (ALERT_LOG_DIR, ALERT_VIDEO_DIR, OBSERVATION_LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def cleanup_old_videos():
    """删掉超过 RETENTION_DAYS 天的旧录像文件，避免长期无人值守跑到把硬盘写满。
    只删视频，不动日志(日志是文本，占用小，而且是排查问题的历史记录，更值得留久一点)。
    """
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


# 组件名里出现这些词，大概率是"故障/异常"相关的指示灯，而不是"正常操作提示"
# (比如NVIDIA那个蓝色ID灯，组件名是"ID Button"，不匹配这些词，就不会被
# 自动归为报警颜色——这正是我们想要的，蓝色ID灯是正常操作，不该触发报警)
FAULT_COMPONENT_KEYWORDS = re.compile(
    r"fault|error|warn|alarm|fail|故障|异常|告警|错误", re.IGNORECASE)


def suggest_alert_colors(knowledge_base: "LedKnowledgeBase", vendor: str, model: str):
    """
    扫描某个厂商型号的knowledge文件，把"组件名里带故障/异常相关词"的规则
    用到的颜色，作为建议的报警颜色返回。green永远不建议报警(哪怕说明书里
    真的有个组件叫"Fault"但颜色恰好是绿的，这种极端情况留给
    ALERT_COLORS_OVERRIDE去手动纠正，不在这里自动处理)。
    返回 (建议的颜色集合, 用于打印的详细依据列表)
    """
    knowledge_base.load(vendor, model)
    slug = knowledge_base._slug(vendor, model)
    data = knowledge_base._cache.get(slug)
    if not data:
        return set(), []

    suggested = set()
    evidence = []
    for rule in data.get("rules", []):
        component = rule.get("component", "")
        color = rule.get("color")
        if not color or color == "green":
            continue
        if FAULT_COMPONENT_KEYWORDS.search(component):
            suggested.add(color)
            evidence.append(f"{color} <- [{component}] {rule.get('description', '')}")

    return suggested, evidence


def resolve_alert_colors(
        knowledge_base,
        default_vendor,
        default_model):
    """得到最终生效的 ALERT_COLORS：手动覆盖优先，否则用知识库自动推导的结果。"""
    if ALERT_COLORS_OVERRIDE is not None:
        logger.debug(f"报警颜色使用手动覆盖设置: {ALERT_COLORS_OVERRIDE}")
        return set(ALERT_COLORS_OVERRIDE)

    all_vendor_models = {
        (default_vendor, default_model)
    }
    all_vendor_models.update(
        station_vendor_model.values()
    )
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

    # Alert（红+Amber）
    if 0 <= median_hue <= 18:
        return "amber", median_hue, median_sat

    # Green
    elif 58 <= median_hue <= 70:
        return "green", median_hue, median_sat

    # Blue
    elif 95 <= median_hue <= 130:
        return "blue", median_hue, median_sat

    return None, median_hue, median_sat


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
        color_label, median_hue, median_sat = classify_color(roi)

        max_v_in_roi = int(np.max(roi[:, :, 2])) if roi.size else 0
        if not DEBUG_SHOW_AREA and max_v_in_roi < BRIGHT_THRESHOLD:
            continue

        color_label, median_hue, median_sat = classify_color(roi)
        if color_label is None:
            if not DEBUG_SHOW_AREA and not DEBUG_SHOW_HSV:
                continue
            color_label = "?"  # 标定模式下颜色分类失败也照样显示面积/HSV，方便观察

        # 换算回原始画面坐标(如果用了SCAN_ROI裁剪，坐标要加回偏移量)
        found.append((x + roi_offset_x, y + roi_offset_y, w, h, color_label, int(area),
                      median_hue, median_sat))

    return found


def _is_placeholder_url(url: str) -> bool:
    """RAIL_STOP_URL/RAIL_RESUME_URL 还是模板里的 <rail-controller-ip> 占位符时，
    真去发网络请求只会必然失败、每次触发都报错刷屏，没有意义——先跳过，
    等真实地址配置好之后这个判断会自动失效，逻辑不用再改。
    """
    return "<" in url and ">" in url


def send_stop_command(reason_color, station_id="unknown"):
    """通知巡检条停止。目前是HTTP占位实现，等接口定了改这里就行。"""
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
        # 网络请求失败不应该让整个监控脚本崩掉——但必须大声记录下来，
        # 因为这意味着巡检条可能没有真的停下来，需要人工介入排查通信问题
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
    """
    根据最近一段时间"每一帧这个颜色有没有出现"的历史，估计是常亮还是闪烁。
    presence_history: [(timestamp, {出现的颜色集合}), ...] 按时间顺序
    返回 "solid" / "blink" / "unknown"
    """
    if len(presence_history) < BLINK_MIN_SAMPLES:
        return "unknown"

    total = len(presence_history)
    present_count = sum(1 for _, colors in presence_history if color in colors)
    ratio = present_count / total

    if ratio >= BLINK_SOLID_RATIO:
        return "solid"
    if ratio < BLINK_MIN_RATIO_TO_COUNT:
        return "unknown"  # 太偶尔出现了，可能只是检测抖动，不够格判定成规律闪烁
    return "blink"


def write_observation_log(color_label, explanation):
    """给非报警颜色(比如蓝色ID灯之类)写一条轻量级记录，不录像。
    不记录绿色(正常状态太频繁，记了也没什么信息量，调用方那边已经过滤掉了)。
    调用方自己控制调用频率(见main()里的防刷屏逻辑)，这里不做节流。
    """
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


def write_alert_log(color_label, pattern, video_path, explanation):
    ensure_dirs()
    ts = datetime.now()
    record = {
        "timestamp": ts.isoformat(),
        "color": color_label,
        "pattern": pattern,
        "video_file": str(video_path),
        "explanation": explanation,
    }
    log_file = ALERT_LOG_DIR / f"{ts:%Y-%m-%d}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.debug(f"报警记录已写入: {log_file} -> {record}")


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
        station_vendor_model[station] = (
            vm["vendor"],
            vm["model"]
        )


    knowledge_base = LedKnowledgeBase(
        knowledge_dir=KNOWLEDGE_DIR
    )

    alert_colors = resolve_alert_colors(
        knowledge_base,
        default_vendor,
        default_model
    )

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

    # 用一个环形缓冲存最近几秒的帧，报警触发时把"触发前"的画面也一起写进视频
    buffer_maxlen = int(PRE_TRIGGER_BUFFER_SECONDS * VIDEO_FPS)
    frame_buffer = deque(maxlen=buffer_maxlen)

    # 每一帧"出现了哪些颜色"的历史，滚动保留最近 BLINK_WINDOW_SECONDS 秒，
    # 用来判断某个颜色是常亮还是闪烁(见 estimate_color_pattern)
    color_presence_history = deque()

    state = "PATROLLING"   # PATROLLING(正常巡检) / ALERTING(已停止,正在录像)
    consecutive_alert_frames = 0
    consecutive_alert_color = None
    alert_start_time = None
    video_writer = None
    last_observed_color = None
    last_observed_pattern = None
    last_observation_log_time = 0.0
    video_path = None

    cleanup_old_videos()   # 启动时先清理一次
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
            candidates = detect_led_candidates(frame)
            colors_found = {c[4] for c in candidates}

            now_for_history = time.time()
            color_presence_history.append((now_for_history, frozenset(colors_found)))
            while (color_presence_history
                   and now_for_history - color_presence_history[0][0] > BLINK_WINDOW_SECONDS):
                color_presence_history.popleft()

            if SHOW_WINDOW:
                display = frame.copy()
                for (x, y, w, h, color_label, area, median_hue, median_sat) in candidates:
                    # 只画异常灯
                    if color_label != "amber":
                        continue

                    cv2.rectangle(display, (x, y), (x + w, y + h), (0, 0, 255), 2)

                    label = f"{color_label} H={median_hue} S={median_sat}"

                    cv2.putText(
                        display,
                        label,
                        (x, y - 6),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (0, 0, 255),
                        2,
                    )
                    
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
                                ph, ps, pv = hsv_frame[my, mx]
                                print(f"点击坐标=({mx},{my})  H={ph} S={ps} V={pv}")
                    cv2.setMouseCallback("patrol_monitor (debug)", _on_mouse_click)

                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            if DEBUG_SHOW_AREA:
                # 标定模式下不触发报警逻辑，只用来读数值
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

                # ---- 非报警颜色的轻量级观察记录 ----
                # 不录像，只写一行日志。不记录绿色——绿色是最常见的正常状态，
                # 记下来意义不大，只会让observation日志被刷屏。颜色和闪烁状态都
                # 跟上次一样、且没超过节流间隔的话也不重复写。蓝色这类"正常但
                # 特殊"的状态意外出现时，留个痕迹方便回查。
                non_alert_colors = colors_found - alert_colors - OK_COLORS
                if non_alert_colors:
                    observed_color = sorted(non_alert_colors)[0]
                    observed_pattern = estimate_color_pattern(color_presence_history, observed_color)
                    now_ts = time.time()
                    # 只看颜色变没变，不看闪烁状态(pattern)变没变——pattern是靠
                    # 滑动窗口估算出来的，样本不够时会有"unknown -> blink"这种
                    # 短暂波动，如果拿pattern变化也当"状态变了"去触发重新记录，
                    # 同一次闪烁会被记好几条几乎一样的。颜色不变就只算一次。
                    color_changed = observed_color != last_observed_color
                    interval_passed = (now_ts - last_observation_log_time) >= OBSERVATION_LOG_MIN_INTERVAL
                    # 颜色刚变化、但闪烁状态还没攒够样本判断出来时，先不急着记这一条——
                    # 等下一次循环判断出常亮/闪烁之后再记，这样(通常是)唯一的一条记录
                    # 信息量更完整。真遇到一直判断不出来的情况，靠30秒兜底(interval_passed)
                    # 还是会记一条，不会永远不记。
                    still_gathering = color_changed and observed_pattern == "unknown"
                    if (color_changed or interval_passed) and not still_gathering:
                        vendor, model = station_vendor_model.get(
                            "unknown", (default_vendor, default_model))
                        obs_pattern_for_lookup = observed_pattern if observed_pattern != "unknown" else None
                        obs_explanation = knowledge_base.describe(
                            vendor, model, color=observed_color, pattern=obs_pattern_for_lookup)
                        write_observation_log(observed_color, obs_explanation)
                        last_observed_color = observed_color
                        last_observed_pattern = observed_pattern
                        last_observation_log_time = now_ts

                if consecutive_alert_frames >= CONSECUTIVE_FRAMES_TO_CONFIRM:
                    # ---- 触发报警 ----
                    # 注意：触发条件仍然只看颜色(红/黄)，不看闪不闪——这里只是把
                    # 闪烁状态一起估算出来，放进说明书查表和日志里，让报警记录
                    # 更精确，但不改变"什么时候触发"这件事本身。
                    # 目前还没有"巡检条现在停在哪个station"的外部状态输入，
                    # 所以这里统一用 DEFAULT_VENDOR/MODEL 查表；等巡检条那边能
                    # 告诉脚本当前station_id后，可以按 STATION_VENDOR_MODEL 查出
                    # 对应厂商型号，实现"不同server用不同说明书解释"
                    vendor, model = station_vendor_model.get(
                        "unknown", (default_vendor, default_model))
                    alert_pattern = estimate_color_pattern(color_presence_history, consecutive_alert_color)
                    alert_pattern_for_lookup = alert_pattern if alert_pattern != "unknown" else None
                    explanation = knowledge_base.describe(
                        vendor, model, color=consecutive_alert_color, pattern=alert_pattern_for_lookup)
                    logger.warning(
                        f"检测到异常颜色: {consecutive_alert_color}({alert_pattern})，触发停止+录像\n"
                        f"说明书查表结果: {explanation}")
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
                    alert_pattern_saved = alert_pattern
                    alert_explanation_saved = explanation
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
                    write_alert_log(consecutive_alert_color_saved, alert_pattern_saved, video_path, alert_explanation_saved)
                    send_resume_command()
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
    main()