# -*- coding: utf-8 -*-
"""
app.py
运维网页控制台的后端。运维人员打开浏览器，点按钮就能完成：
    - 切换/新增站点(station)对应的厂商型号
    - 抓一帧摄像头画面，在网页上直接点选框出面板位置、每颗LED位置(替代原来cv2弹窗点选)
    - 启动/停止巡检(把 monitor_with_rules.py 当子进程管理)
    - 查看运行日志、最近报警记录，下载报警录像

跟原来的标定方式(monitor_with_rules.py --calibrate 手动点选 / Grounding DINO自动检测)
存储格式完全一致，都是同一份 knowledge/<vendor>_<model>.json 和
config/runtime_config.json，随便混用，不冲突。

【运行前提】
1. 这个文件夹要放在原项目文件夹(巡检/)下面一层，结构：
     巡检/
       monitor_with_rules.py   <- 需要先按同目录 monitor_with_rules.patch.md 打补丁
       led_knowledge_lookup.py
       config_manager.py
       knowledge/
       config/
       web_control_panel/       <- 这个文件夹
         app.py
         templates/index.html
2. 装依赖: pip3 install flask opencv-python
3. 运行: python3 app.py
4. 浏览器打开: http://localhost:5001 (同一局域网内的其他电脑，把localhost换成这台电脑的IP)
"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

import cv2
from flask import Flask, jsonify, request, render_template, send_from_directory, abort

# ---------------- 找到原项目文件夹，引入共用模块 ----------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from config_manager import (
    load_runtime_config,
    get_current_station,
    set_station_panel_bbox,
    set_current_station,
    scan_knowledge,
)
from led_knowledge_lookup import LedKnowledgeBase

# monitor_with_rules.py 只是为了拿到里面写死的 CAMERA_SOURCE 常量，
# 复用同一份摄像头地址配置，不用在这边重复写一遍容易改了一边忘了改另一边。
# 这个 import 本身不会启动巡检(monitor_with_rules.py 用了 if __name__=="__main__" 保护)
from monitor_with_rules import CAMERA_SOURCE

# 为了让 web 控制台直接复用 grounding_dino_calibration 里的自动标定逻辑，
# 这里把该目录加入搜索路径，后面可以直接 import dino_detector。
sys.path.insert(0, str(PROJECT_DIR / "grounding_dino_calibration"))

KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
ALERT_LOG_DIR = PROJECT_DIR / "alerts" / "logs"
ALERT_VIDEO_DIR = PROJECT_DIR / "alerts" / "videos"
MONITOR_SCRIPT = PROJECT_DIR / "monitor_with_rules.py"
MONITOR_LOG_FILE = PROJECT_DIR / "web_control_panel" / "monitor_process.log"

DINO_CONFIG_PATH = PROJECT_DIR / "grounding_dino_calibration" / "GroundingDINO" / "groundingdino" / "config" / "GroundingDINO_SwinT_OGC.py"
DINO_CHECKPOINT_PATH = PROJECT_DIR / "grounding_dino_calibration" / "GroundingDINO" / "weights" / "groundingdino_swint_ogc.pth"
DEFAULT_PANEL_PROMPT = "server front panel . indicator panel ."
DEFAULT_LED_PROMPT = "LED . indicator light . status light ."
PANEL_BOX_THRESHOLD = 0.30
LED_BOX_THRESHOLD = 0.25
TEXT_THRESHOLD = 0.25
PANEL_CROP_PADDING_RATIO = 0.08
# Force web/DINO to use the same resolution as the terminal flow
TARGET_WIDTH = 2880
TARGET_HEIGHT = 1620

_dino_detector = None


def _get_dino_detector():
    global _dino_detector
    if _dino_detector is None:
        try:
            from dino_detector import DinoDetector
        except Exception as exc:
            raise RuntimeError(f"加载 Grounding DINO 失败: {exc}") from exc

        if not DINO_CONFIG_PATH.exists():
            raise RuntimeError(f"找不到 Grounding DINO 配置文件: {DINO_CONFIG_PATH}")
        if not DINO_CHECKPOINT_PATH.exists():
            raise RuntimeError(f"找不到 Grounding DINO 权重文件: {DINO_CHECKPOINT_PATH}")

        _dino_detector = DinoDetector(str(DINO_CONFIG_PATH), str(DINO_CHECKPOINT_PATH), device="auto")
    return _dino_detector


def _run_auto_calibration(frame):
    detector = _get_dino_detector()

    panel_candidates_raw = detector.detect(
        frame,
        DEFAULT_PANEL_PROMPT,
        box_threshold=PANEL_BOX_THRESHOLD,
        text_threshold=TEXT_THRESHOLD,
    )
    if not panel_candidates_raw:
        raise RuntimeError("Grounding DINO 没有检测到面板区域，请换一张画面或调整提示词")

    panel_candidates = [
        {
            "x": int(round(box.x)),
            "y": int(round(box.y)),
            "w": int(round(box.w)),
            "h": int(round(box.h)),
            "score": round(float(box.score), 3),
            "label": box.label,
        }
        for box in panel_candidates_raw
    ]

    if not panel_candidates:
        raise RuntimeError("检测到的面板框无效")

    # For parity with terminal behavior, compute LED candidates for each panel candidate
    frame_h, frame_w = frame.shape[:2]
    leds_per_candidate = []
    for panel_box in panel_candidates_raw:
        px, py, pw, ph = panel_box.x, panel_box.y, panel_box.w, panel_box.h
        if pw <= 0 or ph <= 0:
            leds_per_candidate.append([])
            continue

        pad_x = max(2, int(pw * PANEL_CROP_PADDING_RATIO))
        pad_y = max(2, int(ph * PANEL_CROP_PADDING_RATIO))
        crop_x1 = max(0, px - pad_x)
        crop_y1 = max(0, py - pad_y)
        crop_x2 = min(frame_w, px + pw + pad_x)
        crop_y2 = min(frame_h, py + ph + pad_y)
        panel_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]

        led_candidates = detector.detect(
            panel_crop,
            DEFAULT_LED_PROMPT,
            box_threshold=LED_BOX_THRESHOLD,
            text_threshold=TEXT_THRESHOLD,
        )

        led_candidates_out = []
        for box in led_candidates:
            if box.w <= 0 or box.h <= 0:
                continue
            abs_x = crop_x1 + box.x
            abs_y = crop_y1 + box.y
            led_candidates_out.append({
                "x": int(round(abs_x)),
                "y": int(round(abs_y)),
                "w": int(round(box.w)),
                "h": int(round(box.h)),
                "score": round(float(box.score), 3),
                "label": box.label,
            })
        leds_per_candidate.append(led_candidates_out)

    return {
        "panel_bbox": None,
        "panel_candidates": panel_candidates,
        # leds_per_candidate: list of lists aligned with panel_candidates
        "leds_per_candidate": leds_per_candidate,
        # 保持兼容：也返回第0个候选的 leds（可能为空），但前端不应默认使用它
        "leds": leds_per_candidate[0] if leds_per_candidate else [],
        "force_user_select": True,
    }


app = Flask(__name__)

# 当前巡检子进程(全局变量，这是个单机小工具，不考虑多进程并发管理这么复杂的情况)
_monitor_process = None


# ============ 页面 ============

@app.route("/")
def index():
    return render_template("index.html")


# ============ 状态 & 站点/型号 ============

@app.route("/api/status")
def api_status():
    config = load_runtime_config() or {"default": {}, "stations": {}}
    current_station = get_current_station(config)
    current_vendor, current_model = None, None
    if current_station:
        entry = config.get("stations", {}).get(current_station, {})
        current_vendor = entry.get("vendor")
        current_model = entry.get("model")

    return jsonify({
        "running": _is_monitor_running(),
        "current_station": current_station,
        "current_vendor": current_vendor,
        "current_model": current_model,
        "stations": config.get("stations", {}),
        "default": config.get("default", {}),
    })


@app.route("/api/models")
def api_models():
    """返回knowledge目录下已有的所有厂商/型号，给前端下拉框用"""
    models = scan_knowledge()
    return jsonify([{"vendor": v, "model": m} for v, m in models])


@app.route("/api/current_station", methods=["POST"])
def api_set_current_station():
    if _is_monitor_running():
        return jsonify({"error": "巡检运行中，请先停止再切换站点"}), 409

    data = request.get_json(force=True)
    station_id = data.get("station_id", "").strip()
    if not station_id:
        return jsonify({"error": "station_id不能为空"}), 400

    config = load_runtime_config() or {}
    if station_id not in config.get("stations", {}):
        return jsonify({"error": f"station={station_id} 还没有标定过面板位置，不能设为当前站点"}), 400

    set_current_station(station_id)
    return jsonify({"ok": True})


# ============ 标定：抓画面 + 框面板 + 框LED ============

@app.route("/api/frame")
def api_frame():
    """抓一帧摄像头画面，返回base64编码的JPEG + 画面原始尺寸。
    只有巡检没在跑的时候才能抓(避免跟巡检子进程抢摄像头设备)。
    """
    if _is_monitor_running():
        return jsonify({"error": "巡检运行中，无法抓取画面用于标定，请先停止巡检"}), 409

    cap = cv2.VideoCapture(CAMERA_SOURCE)
    if not cap.isOpened():
        return jsonify({"error": f"摄像头打不开: {CAMERA_SOURCE}"}), 500

    ret, frame = cap.read()
    cap.release()
    if not ret:
        return jsonify({"error": "读取画面失败"}), 500

    # Resize to target resolution so web matches terminal detection behavior
    try:
        frame = cv2.resize(frame, (TARGET_WIDTH, TARGET_HEIGHT), interpolation=cv2.INTER_LINEAR)
    except Exception:
        # if resize fails for any reason, continue with original frame
        pass

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return jsonify({"error": "画面编码失败"}), 500

    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    h, w = frame.shape[:2]
    return jsonify({"image_base64": b64, "width": w, "height": h})


@app.route("/api/calibrate/auto", methods=["POST"])
def api_auto_calibrate():
    """用 Grounding DINO 自动识别当前画面里的面板和 LED，返回候选框结果给前端。"""
    if _is_monitor_running():
        return jsonify({"error": "巡检运行中，无法抓取画面用于标定，请先停止巡检"}), 409

    data = request.get_json(force=True) or {}
    station_id = (data.get("station_id") or "").strip()
    vendor = (data.get("vendor") or "").strip()
    model = (data.get("model") or "").strip()
    if not station_id or not vendor or not model:
        return jsonify({"error": "station_id/vendor/model不能为空"}), 400

    cap = cv2.VideoCapture(CAMERA_SOURCE)
    if not cap.isOpened():
        return jsonify({"error": f"摄像头打不开: {CAMERA_SOURCE}"}), 500

    ret, frame = cap.read()
    cap.release()
    if not ret:
        return jsonify({"error": "读取画面失败"}), 500

    # Resize to target resolution so DINO sees the same image as terminal
    try:
        frame = cv2.resize(frame, (TARGET_WIDTH, TARGET_HEIGHT), interpolation=cv2.INTER_LINEAR)
    except Exception:
        pass

    try:
        result = _run_auto_calibration(frame)
    except Exception as exc:
        return jsonify({"error": f"自动检测失败: {exc}"}), 500

    return jsonify({"ok": True, **result})


@app.route("/api/calibrate/panel", methods=["POST"])
def api_calibrate_panel():
    """保存某个station的面板锚点框(panel_bbox)。
    body: {station_id, vendor, model, x, y, w, h}  (像素绝对坐标，来自网页自动识别)
    """
    data = request.get_json(force=True)
    try:
        station_id = data["station_id"].strip()
        vendor = data["vendor"].strip()
        model = data["model"].strip()
        panel_bbox = {
            "x": int(data["x"]), "y": int(data["y"]),
            "w": int(data["w"]), "h": int(data["h"]),
        }
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "参数不完整或格式不对"}), 400

    if not station_id or not vendor or not model:
        return jsonify({"error": "station_id/vendor/model不能为空"}), 400
    if panel_bbox["w"] <= 0 or panel_bbox["h"] <= 0:
        return jsonify({"error": "面板框宽高必须大于0"}), 400

    set_station_panel_bbox(station_id, vendor, model, panel_bbox)

    config = load_runtime_config()
    if config and len(config.get("stations", {})) == 1:
        set_current_station(station_id)

    return jsonify({"ok": True, "panel_bbox": panel_bbox})


    @app.route("/api/calibrate/detect_leds", methods=["POST"])
    def api_detect_leds():
        """给定一个 panel_bbox，针对该裁剪区域重新运行 DINO 的 LED 检测并返回绝对坐标的候选框列表。"""
        if _is_monitor_running():
            return jsonify({"error": "巡检运行中，无法执行检测"}), 409

        data = request.get_json(force=True) or {}
        try:
            vendor = data.get("vendor", "").strip()
            model = data.get("model", "").strip()
            panel_bbox = data["panel_bbox"]
            px, py, pw, ph = int(panel_bbox["x"]), int(panel_bbox["y"]), int(panel_bbox["w"]), int(panel_bbox["h"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "参数不完整或格式不对"}), 400

        if pw <= 0 or ph <= 0:
            return jsonify({"error": "panel_bbox宽高必须大于0"}), 400

        cap = cv2.VideoCapture(CAMERA_SOURCE)
        if not cap.isOpened():
            return jsonify({"error": f"摄像头打不开: {CAMERA_SOURCE}"}), 500

        ret, frame = cap.read()
        cap.release()
        if not ret:
            return jsonify({"error": "读取画面失败"}), 500

        # Ensure same resizing as main flow
        try:
            frame = cv2.resize(frame, (TARGET_WIDTH, TARGET_HEIGHT), interpolation=cv2.INTER_LINEAR)
        except Exception:
            pass

        frame_h, frame_w = frame.shape[:2]
        pad_x = max(2, int(pw * PANEL_CROP_PADDING_RATIO))
        pad_y = max(2, int(ph * PANEL_CROP_PADDING_RATIO))
        crop_x1 = max(0, px - pad_x)
        crop_y1 = max(0, py - pad_y)
        crop_x2 = min(frame_w, px + pw + pad_x)
        crop_y2 = min(frame_h, py + ph + pad_y)
        panel_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]

        try:
            detector = _get_dino_detector()
        except Exception as exc:
            return jsonify({"error": f"加载 DINO 失败: {exc}"}), 500

        led_candidates = detector.detect(
            panel_crop,
            DEFAULT_LED_PROMPT,
            box_threshold=LED_BOX_THRESHOLD,
            text_threshold=TEXT_THRESHOLD,
        )

        out = []
        for box in led_candidates:
            if box.w <= 0 or box.h <= 0:
                continue
            abs_x = crop_x1 + box.x
            abs_y = crop_y1 + box.y
            out.append({
                "x": int(round(abs_x)),
                "y": int(round(abs_y)),
                "w": int(round(box.w)),
                "h": int(round(box.h)),
                "score": round(float(box.score), 3),
                "label": box.label,
            })

        return jsonify({"ok": True, "leds": out})


@app.route("/api/led_positions")
def api_get_led_positions():
    """返回某个型号已经标定过的LED相对位置(rel_x/rel_y/rel_w/rel_h)。
    前端负责结合当前这次标定的panel_bbox换算成绝对像素坐标做叠加显示。
    """
    vendor = request.args.get("vendor", "").strip()
    model = request.args.get("model", "").strip()
    if not vendor or not model:
        return jsonify({"error": "vendor/model不能为空"}), 400

    kb = LedKnowledgeBase(knowledge_dir=str(KNOWLEDGE_DIR))
    positions = kb.get_led_positions(vendor, model)
    return jsonify(positions)


@app.route("/api/calibrate/led_positions", methods=["POST"])
def api_save_led_positions():
    """保存某个型号的LED位置模板(整份覆盖)。
    body: {vendor, model, panel_bbox:{x,y,w,h}, leds:[{component_name,x,y,w,h}, ...]}
    leds里的x,y,w,h是像素绝对坐标(来自网页点选)，这里用panel_bbox换算成
    相对比例坐标再存进knowledge文件，跟原来手动标定/DINO标定用同一套换算公式。
    """
    data = request.get_json(force=True)
    try:
        vendor = data["vendor"].strip()
        model = data["model"].strip()
        panel_bbox = data["panel_bbox"]
        px, py, pw, ph = panel_bbox["x"], panel_bbox["y"], panel_bbox["w"], panel_bbox["h"]
        leds = data["leds"]
    except (KeyError, TypeError):
        return jsonify({"error": "参数不完整"}), 400

    if pw <= 0 or ph <= 0:
        return jsonify({"error": "panel_bbox宽高必须大于0"}), 400
    if not leds:
        return jsonify({"error": "至少要有一颗LED"}), 400

    led_positions = []
    for item in leds:
        try:
            name = item["component_name"].strip()
            x, y, w, h = item["x"], item["y"], item["w"], item["h"]
        except (KeyError, TypeError):
            return jsonify({"error": f"某一颗LED的数据格式不对: {item}"}), 400
        if not name:
            return jsonify({"error": "LED名字不能为空"}), 400
        led_positions.append({
            "component_name": name,
            "rel_x": (x - px) / pw,
            "rel_y": (y - py) / ph,
            "rel_w": w / pw,
            "rel_h": h / ph,
        })

    kb = LedKnowledgeBase(knowledge_dir=str(KNOWLEDGE_DIR))
    kb.save_led_positions(vendor, model, led_positions)
    return jsonify({"ok": True, "count": len(led_positions)})


# ============ 巡检启动/停止/日志 ============

def _is_monitor_running():
    global _monitor_process
    if _monitor_process is None:
        return False
    return _monitor_process.poll() is None


@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    global _monitor_process
    if _is_monitor_running():
        return jsonify({"error": "已经在运行了"}), 409

    config = load_runtime_config()
    if not config or not get_current_station(config):
        return jsonify({"error": "还没有设置当前站点，无法启动巡检"}), 400

    env = os.environ.copy()
    env["PATROL_SHOW_WINDOW"] = "0"   # 后台子进程没有屏幕，强制不开cv2窗口
    env["PATROL_LOG_LEVEL"] = "DEBUG"  # 网页日志区想看到更详细的信息

    MONITOR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(MONITOR_LOG_FILE, "a", encoding="utf-8")
    log_fp.write(f"\n\n===== 启动于 {datetime.now().isoformat()} =====\n")
    log_fp.flush()

    _monitor_process = subprocess.Popen(
        [sys.executable, str(MONITOR_SCRIPT)],
        cwd=str(PROJECT_DIR),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
    )
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    global _monitor_process
    if not _is_monitor_running():
        return jsonify({"error": "现在没有在运行"}), 409

    _monitor_process.terminate()
    try:
        _monitor_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _monitor_process.kill()
        _monitor_process.wait(timeout=5)

    return jsonify({"ok": True})


@app.route("/api/monitor/log_tail")
def api_monitor_log_tail():
    lines = int(request.args.get("lines", 100))
    if not MONITOR_LOG_FILE.exists():
        return jsonify({"text": ""})
    with open(MONITOR_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        all_lines = f.readlines()
    return jsonify({"text": "".join(all_lines[-lines:])})


# ============ 报警记录 ============

@app.route("/api/alerts/recent")
def api_alerts_recent():
    limit = int(request.args.get("limit", 20))
    records = []
    if ALERT_LOG_DIR.exists():
        for log_file in sorted(ALERT_LOG_DIR.glob("*.jsonl"), reverse=True):
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if len(records) >= limit * 3:   # 差不多够用了就不用把每个文件都扫完
                break

    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    records = records[:limit]

    for r in records:
        video_path = r.get("video_file", "")
        if video_path:
            r["video_url"] = f"/api/alerts/video/{Path(video_path).name}"
    return jsonify(records)


@app.route("/api/alerts/video/<path:filename>")
def api_alerts_video(filename):
    # 防止路径穿越，只允许访问文件名本身、不允许带目录跳转
    safe_name = Path(filename).name
    file_path = ALERT_VIDEO_DIR / safe_name
    if not file_path.exists():
        abort(404)
    return send_from_directory(str(ALERT_VIDEO_DIR), safe_name)


if __name__ == "__main__":
    print(f"项目目录: {PROJECT_DIR}")
    print(f"摄像头地址: {CAMERA_SOURCE}")
    print("网页控制台启动: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
