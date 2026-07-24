# -*- coding: utf-8 -*-
"""
calibrate_with_dino.py
用 Grounding DINO 自动检测面板位置 + 每颗LED的位置，人工只需要确认/微调，
不用再从零逐颗手动框选。

跟原来 monitor_with_rules.py --calibrate 保存数据的格式、位置完全一致
(同一份 knowledge/<vendor>_<model>.json 里的led_positions字段，
同一份 config/runtime_config.json 里的panel_bbox)，所以标完之后
直接用原来的 monitor_with_rules.py 巡检，不需要改任何东西。

【使用前提】
1. 装好依赖 + 下载模型权重，见同目录 README.md
2. 这个文件夹要放在原项目文件夹(巡检/)下面一层，比如：
     巡检/
       monitor_with_rules.py
       led_knowledge_lookup.py
       config_manager.py
       knowledge/
       config/
       grounding_dino_calibration/       <- 这个文件夹
         calibrate_with_dino.py          <- 你在这里运行
         dino_detector.py
   这样才能找到共用的 knowledge/、config/ 目录和 led_knowledge_lookup.py /
   config_manager.py 这两个模块。如果你的目录结构不一样，用
   --project-dir 参数指定一下原项目文件夹的路径。

【用法】
    python3 calibrate_with_dino.py --station server_01 --vendor NVIDIA --model "DGX A100"

可选参数：
    --camera <rtsp地址或数字>   不填就用脚本里CAMERA_SOURCE_DEFAULT
    --panel-prompt "..."       检测面板整体位置用的文字描述，有默认值可以不填
    --led-prompt "..."         检测单颗LED用的文字描述，有默认值可以不填
    --box-threshold 0.3        框置信度阈值，检测框太多/太少时可以调这个
    --device cuda / cpu / auto
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

# ---------------- 找到并引入原项目里共用的模块 ----------------

def _add_project_dir_to_path(project_dir: str):
    p = Path(project_dir).resolve()
    if not (p / "led_knowledge_lookup.py").exists():
        print(f"警告: 在 {p} 下没找到 led_knowledge_lookup.py，"
              f"如果不是这个路径，请用 --project-dir 指定原项目文件夹")
    sys.path.insert(0, str(p))


CAMERA_SOURCE_DEFAULT = "rtsp://admin:jiandandian@1@192.168.1.129:554/stream1"

DEFAULT_PANEL_PROMPT = "server front panel . indicator panel ."
DEFAULT_LED_PROMPT = "LED . indicator light . status light ."

# 面板检测和LED检测的置信度阈值可以不一样：面板通常比较大、比较明显，
# 阈值可以设高一点减少误检；LED比较小、容易漏检，阈值可以适当放低，
# 漏检了大不了后面人工手动补一个，比"框太严格直接漏掉"更容易补救
PANEL_BOX_THRESHOLD = 0.30
LED_BOX_THRESHOLD = 0.25
TEXT_THRESHOLD = 0.25

# 检测LED时，是否先裁剪到"面板区域再往外扩一点"的范围里再检测，
# 而不是在整张原图上检测。裁剪的好处：图像里无关背景(墙、其他机柜、人)
# 被排除掉了，减少误检；也让每颗LED在裁剪后的图里占比更大，检测更准。
PANEL_CROP_PADDING_RATIO = 0.08  # 面板框基础上每边再扩这么多比例的余量


# ---------------- 人工确认/微调用的窗口交互 ----------------

def _draw_boxes(image, boxes, color=(0, 0, 255), show_score=True):
    display = image.copy()
    for i, box in enumerate(boxes):
        x, y, w, h = box.x, box.y, box.w, box.h
        cv2.rectangle(display, (x, y), (x + w, y + h), color, 2)
        label = f"#{i}"
        if show_score:
            label += f" {box.label}({box.score:.2f})"
        cv2.putText(display, label, (x, max(0, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    return display


def _pick_one_box_interactively(image, boxes, window_title):
    """展示所有候选框(带编号)，人工在终端输入选哪一个。
    返回选中的box，如果输入'0'个都不满意，返回None(调用方应该退化成手动点选)。
    """
    display = _draw_boxes(image, boxes)
    cv2.imshow(window_title, display)
    cv2.waitKey(1)  # 让窗口先画出来

    print(f"检测到 {len(boxes)} 个候选框：")
    for i, box in enumerate(boxes):
        print(f"  #{i}: label={box.label} score={box.score:.2f} "
              f"x={box.x} y={box.y} w={box.w} h={box.h}")

    while True:
        choice = input("输入编号选择正确的框，或输入 n 表示都不对(手动框选): ").strip().lower()
        cv2.destroyWindow(window_title)
        if choice == "n":
            return None
        try:
            idx = int(choice)
            if 0 <= idx < len(boxes):
                return boxes[idx]
        except ValueError:
            pass
        print("输入无效，请重新输入")
        # 重新显示窗口方便看编号
        cv2.imshow(window_title, display)
        cv2.waitKey(1)


def _manual_click_two_points(frame, window_title):
    """手动兜底：Grounding DINO没检测出来时，退化成人工点两次(左上->右下)"""
    points = []
    display = frame.copy()

    def _on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 2:
            points.append((x, y))
            cv2.circle(display, (x, y), 5, (0, 0, 255), -1)
            cv2.imshow(window_title, display)

    cv2.imshow(window_title, display)
    cv2.setMouseCallback(window_title, _on_click)
    print("手动框选：请点左上角，再点右下角（按q取消）")

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
    return x, y, w, h


def _confirm_and_edit_led_boxes(panel_crop_image, boxes, crop_offset_x, crop_offset_y):
    """
    展示Grounding DINO检测出的所有LED候选框(带编号)，人工可以：
      - 直接回车：全部保留
      - 输入若干个编号(空格分隔)：删掉这几个(误检的)
      - 输入 'add'：手动再框选补一个漏检的LED
    每一轮编辑完都会重新画一次框，直到人工输入"完成"结束。

    返回: [(x, y, w, h), ...]，坐标是相对panel_crop_image的(还没加回crop偏移量)
    """
    kept_boxes = [(b.x, b.y, b.w, b.h) for b in boxes]
    window_title = "LED候选框确认（回车=全部保留，输入编号删除，输入add手动补，输入done完成）"

    while True:
        display = panel_crop_image.copy()
        for i, (x, y, w, h) in enumerate(kept_boxes):
            cv2.rectangle(display, (x, y), (x + w, y + h), (0, 0, 255), 2)
            cv2.putText(display, f"#{i}", (x, max(0, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        cv2.imshow(window_title, display)
        cv2.waitKey(1)

        print(f"\n当前共 {len(kept_boxes)} 个LED候选框")
        cmd = input("操作(直接回车=保留继续 / 数字空格分隔=删除对应编号 / add=手动补一个 / done=确认完成): ").strip()

        if cmd == "":
            continue
        if cmd.lower() == "done":
            break
        if cmd.lower() == "add":
            cv2.destroyWindow(window_title)
            new_box = _manual_click_two_points(panel_crop_image, "手动补框：点左上角->右下角(q取消)")
            if new_box:
                kept_boxes.append(new_box)
            continue

        # 尝试解析成一串要删除的编号
        try:
            idx_to_remove = sorted({int(tok) for tok in cmd.split()}, reverse=True)
            for idx in idx_to_remove:
                if 0 <= idx < len(kept_boxes):
                    kept_boxes.pop(idx)
                else:
                    print(f"编号 {idx} 超出范围，忽略")
        except ValueError:
            print("没看懂这个输入，请重新输入")

    cv2.destroyWindow(window_title)
    return kept_boxes


def _name_each_led(panel_crop_image, boxes):
    """对确认好的每个LED框，在终端里挨个输入component_name。
    每问一个，就在窗口里高亮显示当前问的是哪一个，避免对不上号。
    返回: [{"component_name":, "x":, "y":, "w":, "h":}, ...] (x,y是相对panel_crop_image的坐标)
    """
    named = []
    window_title = "正在命名（当前高亮的是终端里正在问的这一颗）"
    for i, (x, y, w, h) in enumerate(boxes):
        display = panel_crop_image.copy()
        for j, (jx, jy, jw, jh) in enumerate(boxes):
            color = (0, 255, 255) if j == i else (0, 0, 255)
            thickness = 3 if j == i else 1
            cv2.rectangle(display, (jx, jy), (jx + jw, jy + jh), color, thickness)
        cv2.imshow(window_title, display)
        cv2.waitKey(1)

        default_name = f"led_{i + 1}"
        name = input(f"第{i + 1}/{len(boxes)}颗LED的名字 [直接回车用默认名'{default_name}']: ").strip()
        if not name:
            name = default_name
        named.append({"component_name": name, "x": x, "y": y, "w": w, "h": h})

    cv2.destroyWindow(window_title)
    return named


def main():
    parser = argparse.ArgumentParser(description="用Grounding DINO自动标定LED位置")
    parser.add_argument("--station", required=True, help="station_id")
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--camera", default=CAMERA_SOURCE_DEFAULT)
    parser.add_argument("--project-dir", default="..",
                         help="原项目文件夹路径(要能找到led_knowledge_lookup.py/config_manager.py)")
    parser.add_argument("--panel-prompt", default=DEFAULT_PANEL_PROMPT)
    parser.add_argument("--led-prompt", default=DEFAULT_LED_PROMPT)
    parser.add_argument("--box-threshold", type=float, default=None,
                         help="不填的话面板用0.30、LED用0.25分别使用各自默认值")
    parser.add_argument("--text-threshold", type=float, default=TEXT_THRESHOLD)
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    parser.add_argument("--dino-config", required=True,
                         help="Grounding DINO模型结构配置文件路径 (GroundingDINO_SwinT_OGC.py)")
    parser.add_argument("--dino-checkpoint", required=True,
                         help="Grounding DINO权重文件路径 (groundingdino_swint_ogc.pth)")
    args = parser.parse_args()

    _add_project_dir_to_path(args.project_dir)
    from led_knowledge_lookup import LedKnowledgeBase
    from config_manager import load_runtime_config, set_station_panel_bbox, set_current_station

    from dino_detector import DinoDetector

    panel_threshold = args.box_threshold if args.box_threshold is not None else PANEL_BOX_THRESHOLD
    led_threshold = args.box_threshold if args.box_threshold is not None else LED_BOX_THRESHOLD

    print(f"标定 station={args.station}  vendor={args.vendor}  model={args.model}")

    # ---- 抓一帧画面 ----
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"摄像头打不开: {args.camera}")
        return
    ret, frame = cap.read()
    cap.release()
    if not ret:
        print("读取帧失败")
        return
    print(f"抓到一帧，画面尺寸: {frame.shape[1]}x{frame.shape[0]}")

    detector = DinoDetector(args.dino_config, args.dino_checkpoint, device=args.device)

    # ---- 第一步：检测面板整体位置 ----
    print("\n---- 检测面板位置 ----")
    panel_candidates = detector.detect(
        frame, args.panel_prompt, box_threshold=panel_threshold, text_threshold=args.text_threshold)

    if not panel_candidates:
        print("Grounding DINO没检测到符合面板描述的区域，退化为手动框选")
        manual = _manual_click_two_points(frame, "手动框选面板：点左上角->右下角(q取消)")
        if manual is None:
            print("取消了，退出")
            return
        px, py, pw, ph = manual
    else:
        chosen = _pick_one_box_interactively(frame, panel_candidates, "面板候选框（选一个正确的）")
        if chosen is None:
            manual = _manual_click_two_points(frame, "手动框选面板：点左上角->右下角(q取消)")
            if manual is None:
                print("取消了，退出")
                return
            px, py, pw, ph = manual
        else:
            px, py, pw, ph = chosen.x, chosen.y, chosen.w, chosen.h

    panel_bbox = {"x": px, "y": py, "w": pw, "h": ph}
    print(f"面板框确定: {panel_bbox}")

    # ---- 第二步：裁剪到面板区域(留一点余量)，检测LED ----
    frame_h, frame_w = frame.shape[:2]
    pad_x = int(pw * PANEL_CROP_PADDING_RATIO)
    pad_y = int(ph * PANEL_CROP_PADDING_RATIO)
    crop_x1 = max(0, px - pad_x)
    crop_y1 = max(0, py - pad_y)
    crop_x2 = min(frame_w, px + pw + pad_x)
    crop_y2 = min(frame_h, py + ph + pad_y)
    panel_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]

    print("\n---- 检测LED位置 ----")
    led_candidates = detector.detect(
        panel_crop, args.led_prompt, box_threshold=led_threshold, text_threshold=args.text_threshold)
    print(f"Grounding DINO初步检测到 {len(led_candidates)} 个LED候选框")

    # ---- 第三步：人工确认/删误检/补漏检 ----
    kept_boxes = _confirm_and_edit_led_boxes(panel_crop, led_candidates, crop_x1, crop_y1)
    if not kept_boxes:
        print("一颗LED都没保留，标定终止")
        return

    # ---- 第四步：逐颗命名 ----
    named_boxes = _name_each_led(panel_crop, kept_boxes)

    # ---- 换算成相对panel_bbox的比例坐标(注意要把裁剪偏移量加回来) ----
    led_positions = []
    for item in named_boxes:
        abs_x = item["x"] + crop_x1
        abs_y = item["y"] + crop_y1
        led_positions.append({
            "component_name": item["component_name"],
            "rel_x": (abs_x - px) / pw,
            "rel_y": (abs_y - py) / ph,
            "rel_w": item["w"] / pw,
            "rel_h": item["h"] / ph,
        })

    # ---- 保存：跟原来手动标定版本用同一套存储逻辑，格式完全兼容 ----
    knowledge_base = LedKnowledgeBase(knowledge_dir=str(Path(args.project_dir) / "knowledge"))
    knowledge_base.save_led_positions(args.vendor, args.model, led_positions)
    print(f"\n已保存 {len(led_positions)} 颗LED位置到 "
          f"knowledge/{knowledge_base._slug(args.vendor, args.model)}.json")

    set_station_panel_bbox(args.station, args.vendor, args.model, panel_bbox)
    print(f"已保存station={args.station}的panel_bbox到 config/runtime_config.json")

    config = load_runtime_config()
    if config and len(config.get("stations", {})) == 1:
        set_current_station(args.station)
        print("当前只有这一个station，已自动设为current_station")

    # ---- 最终确认画面 ----
    display = frame.copy()
    cv2.rectangle(display, (px, py), (px + pw, py + ph), (255, 0, 0), 2)
    for item in led_positions:
        x = int(round(px + item["rel_x"] * pw))
        y = int(round(py + item["rel_y"] * ph))
        w = int(round(item["rel_w"] * pw))
        h = int(round(item["rel_h"] * ph))
        cv2.rectangle(display, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(display, item["component_name"], (x, max(0, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    cv2.imshow("标定结果确认（按任意键关闭）", display)
    print("按任意键关闭窗口，标定完成")
    cv2.waitKey(0)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
