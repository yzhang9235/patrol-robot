

from pathlib import Path
import json

import sys

KNOWLEDGE_DIR = Path("knowledge")
CONFIG_FILE = Path("config/runtime_config.json")

# 扫描knowledge目录
def scan_knowledge():
    files = sorted(Path(KNOWLEDGE_DIR).glob("*.json"))

    models = []

    for f in files:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)

        vendor = data["vendor"]
        model = data["model"]

        models.append((vendor, model))

    return models

# 选择默认型号
def choose_default_model(models):
    print("\nAvailable server models:\n")

    for i, (vendor, model) in enumerate(models, start=1):
        print(f"[{i}] {vendor} / {model}")

    while True:
        try:
            idx = int(input("\nSelect default server: "))
            if 1 <= idx <= len(models):
                return models[idx - 1]
        except ValueError:
            pass

        print("Invalid selection.")
        
# 配置特殊station
def configure_station_models(models):
    station_map = {}
    while True:
        ans = input("\nAny station using different model? (y/n): ").lower()
        if ans != "y":
            break

        station = input("Station ID: ")
        print()

        for i, (vendor, model) in enumerate(models, start=1):
            print(f"[{i}] {vendor} / {model}")

        idx = int(input("Select model: "))
        station_map[station] = models[idx-1]

    return station_map

#保存设置好的config
def save_runtime_config(default_vm, station_map):
    CONFIG_FILE.parent.mkdir(exist_ok=True)

    data = {
        "default": {
            "vendor": default_vm[0],
            "model": default_vm[1]
        },
        "stations": {}
    }

    for station, vm in station_map.items():

        data["stations"][station] = {
            "vendor": vm[0],
            "model": vm[1]
        }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_runtime_config():
    if not CONFIG_FILE.exists():
        return None
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_runtime_config():

    # 是否强制重新配置
    force_config = "--configure" in sys.argv

    # 正常启动时读取已有配置
    if not force_config:
        config = load_runtime_config()
        if config is not None:
            print("Loading existing configuration...\n")
            return config

    # -----------------------------
    # 第一次运行 或 --configure
    # -----------------------------
    models = scan_knowledge()
    default_vm = choose_default_model(models)
    station_map = configure_station_models(models)

    save_runtime_config(default_vm, station_map)
    print("\nConfiguration saved.\n")

    return {
        "default": {
            "vendor": default_vm[0],
            "model": default_vm[1]
        },
        "stations": {
            station: {
                "vendor": vm[0],
                "model": vm[1]
            }
            for station, vm in station_map.items()
        }
    }