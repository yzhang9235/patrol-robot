# -*- coding: utf-8 -*-
"""
calibrate.py
标定工具：用鼠标在摄像头画面上框选"LED区域"，保存坐标到 regions.json

用法：
    python calibrate.py

操作：
    - 鼠标左键拖拽画框,框住一个模拟LED的位置
    - 松开鼠标后，直接在窗口里打字输入名字（不用切到终端），回车确认
    - Backspace 删除字符
    - 's' 保存并退出（必须在"非命名状态"下按）
    - 'r' 重置所有框
    - 'q' 不保存直接退出
"""

import cv2
import json
import os

REGIONS_FILE = "regions.json"

drawing = False
naming = False          # 是否处于"正在输入名字"状态
ix, iy = -1, -1
drag_x, drag_y = -1, -1
pending_box = None      # 刚画完、等待命名的框 (x0, y0, w, h)
current_frame = None
name_buffer = ""
regions = []  # [{"name": str, "x": int, "y": int, "w": int, "h": int}]

WINDOW_NAME = "Calibrate - drag box, type name in window, Enter to confirm"


def mouse_callback(event, x, y, flags, param):
    global ix, iy, drawing, current_frame, pending_box, naming, name_buffer, drag_x, drag_y

    if naming:
        return  # 正在命名时，忽略鼠标操作，避免误触新框

    if event == cv2.EVENT_LBUTTONDOWN:
        drawing = True
        ix, iy = x, y
        drag_x, drag_y = x, y

    elif event == cv2.EVENT_MOUSEMOVE:
        if drawing:
            drag_x, drag_y = x, y

    elif event == cv2.EVENT_LBUTTONUP:
        drawing = False
        x0, y0 = min(ix, x), min(iy, y)
        w, h = abs(x - ix), abs(y - iy)
        if w < 5 or h < 5:
            return  # 太小，忽略误触
        pending_box = (x0, y0, w, h)
        naming = True
        name_buffer = ""


def draw_existing_regions(frame):
    for r in regions:
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(frame, r["name"], (x, y - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)


def draw_naming_ui(frame):
    """在画面顶部画一个输入框，显示正在输入的名字"""
    x0, y0, w, h = pending_box
    cv2.rectangle(frame, (x0, y0), (x0 + w, y0 + h), (0, 255, 255), 2)

    overlay_text = f"输入名字后回车 (Enter): {name_buffer}_"
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 40), (40, 40, 40), -1)
    cv2.putText(frame, overlay_text, (10, 27),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)


def main():
    global current_frame, naming, name_buffer, pending_box, regions

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头，检查设备权限/是否被其他程序占用")
        return

    cv2.namedWindow(WINDOW_NAME)
    cv2.setMouseCallback(WINDOW_NAME, mouse_callback)

    print("=" * 50)
    print("标定说明：")
    print("  1. 拖拽鼠标框选一个'LED'位置")
    print("  2. 松开鼠标后，直接在视频窗口里打字（不用点终端），Enter确认")
    print("  3. 全部框完后，鼠标点一下视频窗口，再按 's' 保存退出")
    print("  'r' 清空重来 | 'q' 不保存退出")
    print("=" * 50)

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        current_frame = frame
        display = frame.copy()
        draw_existing_regions(display)

        if drawing:
            cv2.rectangle(display, (ix, iy), (drag_x, drag_y), (0, 255, 255), 2)

        if naming:
            draw_naming_ui(display)

        cv2.imshow(WINDOW_NAME, display)
        key = cv2.waitKey(30) & 0xFF

        if naming:
            # 命名状态下，键盘只用来输入名字，不响应 s/r/q
            if key == 13 or key == 10:  # Enter
                x0, y0, w, h = pending_box
                name = name_buffer.strip() or f"region_{len(regions)+1}"
                regions.append({"name": name, "x": x0, "y": y0, "w": w, "h": h})
                print(f"已添加: {name} -> ({x0},{y0},{w},{h})，当前共 {len(regions)} 个区域")
                naming = False
                name_buffer = ""
                pending_box = None
            elif key == 8 or key == 127:  # Backspace
                name_buffer = name_buffer[:-1]
            elif key == 27:  # Esc 取消这次命名
                naming = False
                pending_box = None
                name_buffer = ""
            elif 32 <= key <= 126:  # 可打印ASCII字符
                name_buffer += chr(key)
            continue

        # 非命名状态下才响应功能键
        if key == ord('s'):
            with open(REGIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(regions, f, ensure_ascii=False, indent=2)
            print(f"\n已保存 {len(regions)} 个区域到 {os.path.abspath(REGIONS_FILE)}")
            break
        elif key == ord('r'):
            regions.clear()
            print("已清空所有区域")
        elif key == ord('q'):
            print("退出，未保存")
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()