# -*- coding: utf-8 -*-
"""
locate_marker.py
模拟"机器人自己的摄像头"找ArUco标记,判断目标rack在画面哪个方向、大概多远
（真实机器人上，这段逻辑跑在机器人自己的摄像头上，输出的方向指令去驱动电机；
 这里没有电机，所以只在终端打印指令，方便先验证识别逻辑）

用法：
    python locate_marker.py A03
    (A03 换成你要找的 rack_id,跟 rack_positions.json 里的对应)

    不给参数的话,默认显示画面里检测到的所有marker
"""

import cv2
import json
import sys

POSITIONS_FILE = "rack_positions.json"

ARUCO_DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
DETECTOR_PARAMS = cv2.aruco.DetectorParameters()
detector = cv2.aruco.ArucoDetector(ARUCO_DICT, DETECTOR_PARAMS)

# 调这个阈值：marker在画面里的宽度(像素)超过这个值，就认为"已经到达/足够近"
ARRIVED_WIDTH_THRESHOLD = 200
# 画面中心附近多少像素以内，算作"正对目标"，不需要再转向
CENTER_TOLERANCE_PX = 40


def load_positions():
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"找不到 {POSITIONS_FILE}，请先运行 generate_markers.py")
        sys.exit(1)


def find_target_marker_id(positions, target_rack_id):
    for marker_id, info in positions.items():
        if info["rack_id"] == target_rack_id:
            return int(marker_id)
    return None


def guidance_for_marker(corner, frame_width):
    """
    根据marker在画面里的位置和大小，给出简单的方向指令
    corner: detectMarkers返回的单个marker的4个角点坐标
    """
    pts = corner.reshape(4, 2)
    center_x = pts[:, 0].mean()
    marker_width = pts[:, 0].max() - pts[:, 0].min()

    frame_center_x = frame_width / 2
    offset = center_x - frame_center_x

    if marker_width >= ARRIVED_WIDTH_THRESHOLD:
        return "已到达，停止移动", marker_width
    if abs(offset) <= CENTER_TOLERANCE_PX:
        return f"方向正确，继续前进 (marker宽度={marker_width:.0f}px)", marker_width
    elif offset > 0:
        return f"目标偏右 {offset:.0f}px，向右转", marker_width
    else:
        return f"目标偏左 {abs(offset):.0f}px，向左转", marker_width


def main():
    target_rack_id = sys.argv[1] if len(sys.argv) > 1 else None
    positions = load_positions()

    target_marker_id = None
    if target_rack_id:
        target_marker_id = find_target_marker_id(positions, target_rack_id)
        if target_marker_id is None:
            print(f"rack_positions.json 里找不到 rack_id={target_rack_id}")
            return
        print(f"目标: rack {target_rack_id} (marker_id={target_marker_id})")
    else:
        print("未指定目标，将显示画面里检测到的所有marker")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头")
        return

    print("按 'q' 退出")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = detector.detectMarkers(gray)

        display = frame.copy()
        if ids is not None:
            cv2.aruco.drawDetectedMarkers(display, corners, ids)

            for i, marker_id in enumerate(ids.flatten()):
                rack_info = positions.get(str(marker_id))
                label = rack_info["rack_id"] if rack_info else "未知marker"

                if target_marker_id is not None and marker_id == target_marker_id:
                    msg, width = guidance_for_marker(corners[i], frame.shape[1])
                    print(f"[找到目标] {label}: {msg}")
                    cv2.putText(display, msg, (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                else:
                    pts = corners[i].reshape(4, 2)
                    x, y = int(pts[:, 0].min()), int(pts[:, 1].min())
                    cv2.putText(display, label, (x, y - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
        elif target_marker_id is not None:
            cv2.putText(display, "未发现目标marker，转动寻找中...", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        cv2.imshow("Robot Camera - marker locate", display)
        if cv2.waitKey(30) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
