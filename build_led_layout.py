# -*- coding: utf-8 -*-
"""
build_led_layout.py

根据服务器正面照片，人工标定LED位置，生成layout数据库。

Usage:
    python build_led_layout.py server.jpg
"""

import cv2
import json
import sys
from pathlib import Path

OUTPUT_DIR = Path("layouts")
RADIUS = 10

leds = []
display = None


def mouse_callback(event, x, y, flags, param):
    global display

    if event == cv2.EVENT_LBUTTONDOWN:

        name = input(f"\nLED name for ({x}, {y}): ").strip()

        if not name:
            print("Skipped.")
            return

        leds.append({
            "name": name,
            "x": x,
            "y": y,
            "radius": RADIUS
        })

        cv2.circle(display, (x, y), RADIUS, (0, 255, 0), 2)
        cv2.putText(
            display,
            name,
            (x + 8, y - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0,255,0),
            1
        )

        cv2.imshow("LED Layout Builder", display)


def main():

    if len(sys.argv) != 2:
        print("Usage:")
        print("python build_led_layout.py server.jpg")
        return

    image_path = Path(sys.argv[1])

    if not image_path.exists():
        print("Image not found.")
        return

    image = cv2.imread(str(image_path))

    if image is None:
        print("Cannot read image.")
        return

    global display
    display = image.copy()

    print("\n==========================")
    print("Left Click : Add LED")
    print("S          : Save")
    print("Q          : Quit")
    print("==========================\n")

    vendor = input("Vendor : ").strip()
    model = input("Model  : ").strip()

    cv2.namedWindow("LED Layout Builder")
    cv2.setMouseCallback("LED Layout Builder", mouse_callback)

    while True:

        cv2.imshow("LED Layout Builder", display)

        key = cv2.waitKey(20) & 0xFF

        if key == ord("q"):
            break

        elif key == ord("s"):

            OUTPUT_DIR.mkdir(exist_ok=True)

            filename = f"{vendor}_{model}".replace(" ", "_")
            outfile = OUTPUT_DIR / f"{filename}_layout.json"

            data = {
                "vendor": vendor,
                "model": model,
                "image_width": image.shape[1],
                "image_height": image.shape[0],
                "leds": leds
            }

            with open(outfile, "w", encoding="utf-8") as f:
                json.dump(
                    data,
                    f,
                    indent=2,
                    ensure_ascii=False
                )

            print(f"\nSaved to {outfile}")

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()