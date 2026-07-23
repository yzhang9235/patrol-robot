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

# 保存设置好的config
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

    # 第一次运行 或 --configure
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


# ============ 以下是新增：给标定模式(--calibrate)用的panel_bbox读写 ============

def set_station_panel_bbox(station_id, vendor, model, panel_bbox):
    """
    标定完成后调用：把某个station的面板锚点框(panel_bbox)写入config/runtime_config.json，
    同时记录这个station对应的vendor/model(标定时手动指定，以此为准)。
    这个station_id如果之前没配置过，会新建一条；已存在就整条覆盖更新
    (vendor/model也会跟着这次标定重新写，避免出现"panel_bbox是新的、
    vendor/model还是旧的"这种不一致)。

    panel_bbox: {"x":int, "y":int, "w":int, "h":int}

    如果这是第一次运行、config文件还完全不存在(既没跑过交互式配置向导，
    也没跑过--configure)，这里会顺手把default也一并建好(用这次标定的
    vendor/model当default)，保证main()那边正常读取runtime_config时
    不会因为default字段缺失而报错——不强制要求标定前必须先跑一遍配置向导。
    """
    config = load_runtime_config()
    if config is None:
        config = {
            "default": {"vendor": vendor, "model": model},
            "stations": {},
        }
    config.setdefault("stations", {})
    config["stations"][station_id] = {
        "vendor": vendor,
        "model": model,
        "panel_bbox": panel_bbox,
    }

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return config


def set_current_station(station_id):
    """
    记录"巡检条现在停在哪个station"。目前巡检条还不会移动，只有一个固定
    station，标定完会自动调用这个函数把它设成current_station；以后巡检条
    真的开始移动了，改成由巡检条上报当前站点、每次到站时调用这个函数更新，
    main()那边的逻辑完全不用跟着改。
    """
    config = load_runtime_config()
    if config is None:
        config = {"default": {}, "stations": {}}
    config["current_station"] = station_id

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return config


def get_current_station(config):
    """
    决定当前应该监控哪个station的LED位置，按优先级：
    1. config里显式记录的 current_station(以后巡检条移动后，这个值会被
       实时更新，这里始终优先读它)
    2. 如果没有显式设置，但config里恰好只有一个station标定过panel_bbox，
       自动选它——对应你现在"只有一台固定server"的情况，不用每次手动指定
    3. 都不满足(比如配置了多个station但没指定当前是哪个)，返回None，
       调用方要退化成旧的整片区域检测方式，并提示需要标定/指定
    """
    if config is None:
        return None

    explicit = config.get("current_station")
    if explicit and explicit in config.get("stations", {}):
        return explicit

    calibrated_stations = [
        sid for sid, entry in config.get("stations", {}).items()
        if entry.get("panel_bbox")
    ]
    if len(calibrated_stations) == 1:
        return calibrated_stations[0]

    return None