from pathlib import Path
import json
import sys

# 关键修复：不再用相对路径(cwd依赖)，而是锚定在config_manager.py这个文件
# 本身实际所在的位置。不管是谁在哪个文件夹下import这个模块、
# 不管终端当前cwd是哪里，KNOWLEDGE_DIR/CONFIG_FILE永远指向巡检/目录下
# 正确的knowledge/、config/，不会再出现"写到子文件夹里的影子配置"这种问题
BASE_DIR = Path(__file__).resolve().parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
CONFIG_FILE = BASE_DIR / "config" / "runtime_config.json"

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
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    force_config = "--configure" in sys.argv

    if not force_config:
        config = load_runtime_config()
        if config is not None:
            print("Loading existing configuration...\n")
            return config

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


def set_station_panel_bbox(station_id, vendor, model, panel_bbox):
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
    config = load_runtime_config()
    if config is None:
        config = {"default": {}, "stations": {}}
    config["current_station"] = station_id

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return config


def get_current_station(config):
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