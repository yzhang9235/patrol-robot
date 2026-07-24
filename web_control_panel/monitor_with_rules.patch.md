# 需要给 monitor_with_rules.py 打的补丁

只改两处，都是"加一点点代码"，不删除、不改变你现在任何已有的行为——
直接在终端手动跑还是跟以前完全一样；只有当网页后台通过环境变量控制它时，
才会切换成无窗口(headless)、更详细日志(debug)的模式。

---

## 改动1：文件顶部加 `import os`（如果还没有的话）

找到这几行：
```python
import argparse
import sys
import cv2
```

改成：
```python
import argparse
import os
import sys
import cv2
```

---

## 改动2：SHOW_WINDOW 那一行，加上环境变量覆盖

找到这一行：
```python
SHOW_WINDOW = True           # 生产环境建议 False（无人值守，不需要显示画面）；调试时改 True
```

改成：
```python
SHOW_WINDOW = True           # 生产环境建议 False（无人值守，不需要显示画面）；调试时改 True
# 网页后台通过这个环境变量强制关闭窗口(以子进程方式在后台跑，没有屏幕可显示)，
# 不设置这个环境变量时，行为跟以前完全一样，终端直接跑不受影响
if os.environ.get("PATROL_SHOW_WINDOW") is not None:
    SHOW_WINDOW = os.environ.get("PATROL_SHOW_WINDOW") == "1"
```

---

## 改动3：logging.basicConfig 那一段，加上环境变量控制日志级别

找到这几行：
```python
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s\n",
)
logger = logging.getLogger("patrol_monitor")
```

改成：
```python
# 网页后台启动时会把这个设成DEBUG，这样"已加载N颗LED的标定坐标"这类调试信息
# 也能在网页的日志区看到；终端直接跑不设置这个环境变量，还是保持WARNING，
# 跟以前一样不会被debug信息刷屏
_LOG_LEVEL_NAME = os.environ.get("PATROL_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL_NAME, logging.WARNING),
    format="%(asctime)s [%(levelname)s] %(message)s\n",
)
logger = logging.getLogger("patrol_monitor")
```

---

改完这三处之后，把文件存回 `巡检/monitor_with_rules.py`（覆盖原文件），
终端直接跑 `python3 monitor_with_rules.py` 行为不变；
网页后台会通过设置这两个环境变量来控制它的运行方式。
