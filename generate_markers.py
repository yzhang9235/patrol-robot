# -*- coding: utf-8 -*-
"""
generate_markers.py
生成ArUco标记图片，贴在每个机柜上；同时生成 rack_positions.json 对照表

用法：
    先在下面 RACK_LIST 里填你的机柜编号，然后跑：
    python generate_markers.py

输出：
    markers/marker_0.png, marker_1.png, ...  (打印出来贴在对应机柜上)
    rack_positions.json                       (marker_id -> rack_id 对照表)
"""

import cv2
import json
import os

# ---------- 在这里填你的机柜列表 ----------
RACK_LIST = [
    {"rack_id": "A01", "notes": "过道左侧第1个机柜"},
    {"rack_id": "A02", "notes": "过道左侧第2个机柜"},
    {"rack_id": "A03", "notes": "过道左侧第3个机柜"},
    {"rack_id": "A04", "notes": "过道右侧第1个机柜"},
]
# -----------------------------------------

MARKER_SIZE_PX = 400     # 生成图片的像素尺寸，打印时按实际需要缩放
OUTPUT_DIR = "markers"
POSITIONS_FILE = "rack_positions.json"

# DICT_4X4_50: 4x4位的marker，最多50个不同ID，够小规模用；机柜多的话换成 DICT_5X5_250
ARUCO_DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if len(RACK_LIST) > 50:
        print("警告: DICT_4X4_50 最多支持50个marker，机柜数量超了，需要换更大的字典")
        return

    positions = {}

    for marker_id, rack in enumerate(RACK_LIST):
        img = cv2.aruco.generateImageMarker(ARUCO_DICT, marker_id, MARKER_SIZE_PX)
        filename = os.path.join(OUTPUT_DIR, f"marker_{marker_id}.png")
        cv2.imwrite(filename, img)

        positions[str(marker_id)] = {
            "rack_id": rack["rack_id"],
            "notes": rack.get("notes", "")
        }
        print(f"marker_{marker_id}.png  ->  {rack['rack_id']}")

    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)

    print(f"\n共生成 {len(RACK_LIST)} 个marker，对照表已保存到 {POSITIONS_FILE}")
    print(f"打印 {OUTPUT_DIR}/ 里的图片，贴在对应机柜上（建议实际打印尺寸 ≥10cm x 10cm，方便远距离识别）")


if __name__ == "__main__":
    main()
