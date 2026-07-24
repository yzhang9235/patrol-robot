# -*- coding: utf-8 -*-
"""
dino_detector.py
Grounding DINO 模型的加载 + 推理封装，只做一件事：
给一张图 + 一句文字描述(prompt)，返回图里符合描述的候选框列表。

不涉及任何"巡检/标定"的业务逻辑，calibrate_with_dino.py 才是业务逻辑，
这里保持纯粹，方便以后如果换别的检测模型(比如别的开放词汇检测器)，
只需要照着同样的接口重写这一个文件，calibrate_with_dino.py 不用动。

依赖 & 权重下载说明见同目录 README.md，这里只假设模型文件已经下载好、
路径通过参数传进来。
"""

from dataclasses import dataclass
from typing import List
import numpy as np


@dataclass
class DetectionBox:
    """一个候选框，坐标是像素级绝对坐标(左上角x,y + 宽高)"""
    x: int
    y: int
    w: int
    h: int
    label: str      # 命中的是prompt里的哪个短语
    score: float     # 置信度 0~1，越高越可信


class DinoDetector:
    def __init__(self, config_path: str, checkpoint_path: str, device: str = "auto"):
        """
        config_path:     Grounding DINO 的模型结构配置文件路径
                          (官方仓库里的 GroundingDINO_SwinT_OGC.py)
        checkpoint_path:  权重文件路径 (官方发布的 groundingdino_swint_ogc.pth)
        device: "cuda" / "cpu" / "auto"(自动检测有没有GPU，没有就用cpu)
        """
        # 延迟导入：这样如果只是想看calibrate_with_dino.py的整体流程、
        # 还没装groundingdino这个包，也不会一import这个文件就直接报错，
        # 只有真的实例化DinoDetector(真的要跑检测)时才会要求依赖装好
        import torch
        from groundingdino.util.inference import load_model

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device

        print(f"[dino_detector] 加载模型中 (device={device})，第一次加载会慢一些...")
        self.model = load_model(config_path, checkpoint_path)
        self.model = self.model.to(device)
        print("[dino_detector] 模型加载完成")

    def detect(self, image_bgr: np.ndarray, text_prompt: str,
               box_threshold: float = 0.30, text_threshold: float = 0.25) -> List[DetectionBox]:
        """
        image_bgr: OpenCV读进来的图 (BGR格式, HxWx3)
        text_prompt: 用句号分隔的多个短语，比如 "server panel . LED indicator light ."
                     Grounding DINO对这个格式比较敏感，短语之间务必用" . "分隔
        box_threshold: 框本身的置信度阈值，低于这个分数的框直接丢弃
        text_threshold: 文字匹配置信度阈值

        返回: [DetectionBox, ...]，坐标已经换算成原图的像素绝对坐标
        """
        import torch
        import cv2
        from groundingdino.util.inference import predict

        # groundingdino要求RGB输入，且经过它自己的预处理(resize+normalize)。
        # 这里直接调用它官方的图像变换，避免自己复现predict内部的预处理细节，
        # 保证跟官方demo脚本行为一致
        from groundingdino.datasets import transforms as T

        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        h, w = image_rgb.shape[:2]

        transform = T.Compose([
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        # groundingdino的transform是给(PIL图, target)一起用的，这里没有target，传None
        from PIL import Image as PILImage
        pil_image = PILImage.fromarray(image_rgb)
        image_tensor, _ = transform(pil_image, None)

        boxes, logits, phrases = predict(
            model=self.model,
            image=image_tensor,
            caption=text_prompt,
            box_threshold=box_threshold,
            text_threshold=text_threshold,
            device=self.device,
        )

        # boxes返回的是归一化的(cx, cy, w, h)，中心点坐标+宽高，0~1比例
        # 换算成像素级(x, y, w, h)绝对坐标，跟项目里其他地方的坐标格式统一
        results = []
        for box, score, phrase in zip(boxes, logits, phrases):
            cx, cy, bw, bh = box.tolist()
            abs_w = bw * w
            abs_h = bh * h
            abs_x = cx * w - abs_w / 2
            abs_y = cy * h - abs_h / 2
            results.append(DetectionBox(
                x=int(round(abs_x)), y=int(round(abs_y)),
                w=int(round(abs_w)), h=int(round(abs_h)),
                label=phrase, score=float(score),
            ))
        return results
