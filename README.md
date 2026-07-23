--------------------------------------------------------------------------------
作者：Yining Zhang
日期：2026年7月17日
--------------------------------------------------------------------------------
实现说明

1. build_led_knowledge：
   用于读取设备手册、文档或图片，提取不同厂商和服务器型号对应的 LED 状态规则，
   并将规则存储至知识库（knowledge base）中。

2. monitor_with_rules：
   用于识别服务器 LED 状态，包括绿色、蓝色、红色和黄色（Amber）。

   系统会根据当前选择的服务器厂商和型号，从知识库中加载对应规则，
   并判断当前 LED 状态是否存在异常。

   - 蓝色状态的问题会记录至 observations 文件夹
   - 红色和黄色状态的问题会触发巡检设备停止运行，并启动视频录制，超过30天的视频会被自动清除
   - 所有异常记录、截图和视频文件均存储在 alerts 文件夹中

3. config_manager：
   用于管理当前机柜中的服务器配置信息，包括服务器厂商和型号。

   系统会根据保存的配置，自动选择知识库中对应厂商和型号的 LED 规则。
   首次运行时需要手动输入机柜服务器配置。
   后续运行会自动读取已保存的配置。
   如果服务器配置发生变化，可以通过以下命令重新配置：
       python monitor_with_rules.py --configure

4. led_position_db.py
   LED位置数据库：解决"每来一台新server都要重新框选ROI"的问题

--------------------------------------------------------------------------------
需要完善的部分

1. 当前版本尚未连接实际巡检设备。
   目前用于控制巡检设备停止、移动等操作的指令均为占位接口，
   后续需要与实际硬件控制模块进行集成。

2. 不同厂商可能存在不同类型的 LED 指示灯，
   同一种颜色在不同 LED 类型下可能代表不同含义（例如 HPE 服务器）。
   后续版本可能需要进一步记录 LED 名称、位置以及对应功能，
   以提高规则匹配的准确性。

3. 当前的判断闪烁的方式可能不够准确

4. 真实LED的形状可能不是圆的（有些是长条形/方形），
   MIN_CIRCULARITY 这个参数可能需要放宽甚至换个判断方式

5. 真实机房光线环境会和模拟有差异（背光、其他机柜的灯光反光），
   色相/饱和度这些参数大概率要重新标定，需要调整数值

--------------------------------------------------------------------------------
使用方法

一. 假设现在拿到一台从没见过的型号、装在一个新的station位置:

1. 先解析说明书,生成规则文件(只需要做一次,以后同型号不用再做)
    python3 build_led_knowledge.py 说明书.pdf --vendor NVIDIA --model "DGX A100"

2. 标定这个station的面板位置 + 这个型号的LED位置(每个型号第一次遇到时做一次)
    python3 monitor_with_rules.py --calibrate --station server_01 --vendor NVIDIA --model "DGX A100"
    - 点面板左上角→右下角
    - 因为这个型号还没标定过LED,会提示你逐颗框选、输入名字

3. 开始巡检
    python3 monitor_with_rules.py

二. 日常巡检
    python3 monitor_with_rules.py

三. 一个新的位置放的型号已经被存储过
    python3 monitor_with_rules.py --calibrate --station server_02 --vendor NVIDIA --model "DGX A100"
    - 只需要点这个新station的面板框(2个点)，因为NVIDIA DGX A100之前标定过LED位置,会提示"已标定N颗,是否复用"——直接回车就行,不用重新框LED

四. 一个站位上的型号换了
python3 build_led_knowledge.py 新说明书.pdf --vendor Dell --model "PowerEdge R760"
python3 monitor_with_rules.py --calibrate --station server_01 --vendor Dell --model "PowerEdge R760"
两步都要做——先建规则文件,再标定(这个型号是新的,所以还是要逐颗框LED)。

五. LED位置框歪了/摄像头挪动过,需要重新标定同一个station
python3 monitor_with_rules.py --calibrate --station server_01 --vendor NVIDIA --model "DGX A100"

--------------------------------------------------------------------------------




--------------------------------------------------------------------------------
IMPLEMENTATION NOTES

1. build_led_knowledge:
   Reads manuals, documents, or images, and stores LED rules for different server
   vendors and models into the knowledge base.

2. monitor_with_rules:
   Detects LED colors including green, blue, red, and amber.
   Based on the selected vendor's rules, it determines whether the current LED
   status indicates an issue.

   - Blue LED issues are saved into the "observations" folder.
   - Red and amber LED issues will trigger the inspection robot to stop and start
     video recording.
   - All related files are stored under the "alerts" folder.

3. config_manager:
   Stores information about the servers installed in the current rack, including
   vendor and model configurations.
   The system uses this configuration to select the corresponding LED rules from
   the knowledge base.

   The configuration only needs to be entered during the first setup.
   After that, the system will automatically load the saved configuration.
   If changes are needed, run:

       python monitor_with_rules.py --configure

--------------------------------------------------------------------------------
FUTURE WORK

1. The inspection robot has not been integrated yet.
   The current commands for stopping and moving the inspection system are only
   placeholders.

2. Different vendors may use different LEDs, and the same LED color may have
   different meanings depending on the LED type (for example, HPE servers).
   Future versions may need to include LED names/types in the knowledge base.

3. The code for controlling the physical stop mechanism has not been implemented
   yet.

--------------------------------------------------------------------------------
USAGE

1. Build LED rules from manuals or images:

       python build_led_knowledge.py HPE.png --vendor xxx --model "xxx"

   Example:

       python build_led_knowledge.py HPE.png \
       --vendor HPE \
       --model "ProLiant DL360 Gen10"

2. Start monitoring:

       python monitor_with_rules.py

--------------------------------------------------------------------------------



**What actually happens

led_positions (where each LED sits) — this is what --calibrate handles:

When you run --calibrate --station X --vendor Y --model Z, it checks: does knowledge/Y_Z.json already have led_positions for this vendor/model?
If yes → it shows you "already calibrated N LEDs" and asks: reuse, or redo? So yes, existing LED position data does show up and gets reused if you don't say y.
If no → you have to click through and frame every LED manually.
Either way, it always asks you to click the panel_bbox (2 points) fresh each time — that part is per-station (physical camera framing), not reusable across stations even for the same model.

**So the actual flow when you arrive at a new server
Run --calibrate for that station + vendor/model
It checks the knowledge file: led_positions → reuse-or-redo prompt (as above)
It doesn't touch rules at all — rules just sit there in the file already (assuming you ran build_led_knowledge.py for that model at some point in the past)
You only click the panel_bbox, and possibly LEDs if new/redoing
Saves panel_bbox → config/runtime_config.json, saves led_positions (if changed) → knowledge/<vendor>_<model>.json