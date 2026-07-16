# -*- coding: utf-8 -*-
"""
record_video.py
轨道摄像头版：收到目标rack_id -> 移动到该位置 -> 录一段视频 -> 保存文件

用法：
    python record_video.py A03

三个步骤里，只有"录像"这部分是我能直接给你完整代码的（不依赖你们轨道硬件细节）。
"移动到位置" 和 "传给运维" 这两处标了 TODO，需要你接上实际的轨道控制协议和文件传输方式。
"""

import cv2
import json
import time
import sys
import os
from datetime import datetime

POSITIONS_FILE = "track_positions.json"
OUTPUT_DIR = "recordings"

CAMERA_SOURCE = 0     # 换成实际摄像头index，或者网络摄像头的话换成 "rtsp://user:pass@ip:554/stream1"
RECORD_DURATION_SEC = 15   # 到达后录多少秒，可调；也可以改成"按 'q' 手动结束"


def load_positions():
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"找不到 {POSITIONS_FILE}")
        sys.exit(1)


def move_to_position(rack_id, positions):
    """
    TODO: 换成你们轨道系统实际的控制方式，比如：
        track_controller.move_to(info["position"])
        while not track_controller.is_arrived():
            time.sleep(0.1)
    这里先只是打印+模拟等待，方便你先跑通录像这部分逻辑
    """
    info = positions.get(rack_id)
    if info is None:
        print(f"track_positions.json 里找不到 rack_id={rack_id}")
        return False

    print(f"[TODO-接轨道API] 前往位置 {info['position']}{info.get('unit','')} ({rack_id})")
    time.sleep(1)  # 模拟移动耗时的占位，实际应该是等轨道到位确认信号
    print("已到达（模拟）")
    return True


def record_video(rack_id):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    cap = cv2.VideoCapture(CAMERA_SOURCE)
    if not cap.isOpened():
        print("无法打开摄像头，检查 CAMERA_SOURCE 设置")
        return None

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
    fps = 20

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(OUTPUT_DIR, f"{rack_id}_{timestamp}.mp4")
    writer = cv2.VideoWriter(filename, fourcc, fps, (width, height))

    print(f"开始录制: {filename}  (时长{RECORD_DURATION_SEC}秒，按 'q' 可提前结束)")
    start_time = time.time()

    while time.time() - start_time < RECORD_DURATION_SEC:
        ret, frame = cap.read()
        if not ret:
            print("读取画面失败，提前结束录制")
            break

        writer.write(frame)

        elapsed = int(time.time() - start_time)
        cv2.putText(frame, f"REC {rack_id}  {elapsed}s / {RECORD_DURATION_SEC}s",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.imshow("Recording - press q to stop early", frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            print("手动提前结束")
            break

    writer.release()
    cap.release()
    cv2.destroyAllWindows()

    print(f"录制完成: {filename}")
    return filename


def deliver_video(filepath):
    """
    TODO: 传给运维人员的方式还没定，先只是留占位
    确定方式后（企业微信webhook / NAS共享盘 / SPACE:TiD平台API），把实际上传代码写在这里，
    比如企业微信机器人上传文件、或者用 shutil.copy() 存到共享盘路径
    """
    print(f"[TODO-接传输方式] 视频已保存在本地: {os.path.abspath(filepath)}")


def main():
    if len(sys.argv) < 2:
        print("用法: python record_video.py <rack_id>")
        print("例如: python record_video.py A03")
        return

    rack_id = sys.argv[1]
    positions = load_positions()

    if not move_to_position(rack_id, positions):
        return

    filepath = record_video(rack_id)
    if filepath:
        deliver_video(filepath)


if __name__ == "__main__":
    main()
