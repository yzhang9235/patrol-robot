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

   - 蓝色状态的问题会记录至 observations 文件夹；
   - 红色和黄色状态的问题会触发巡检设备停止运行，并启动视频录制；
   - 所有异常记录、截图和视频文件均存储在 alerts 文件夹中。

3. config_manager：
   用于管理当前机柜中的服务器配置信息，包括服务器厂商和型号。

   系统会根据保存的配置，自动选择知识库中对应厂商和型号的 LED 规则。

   首次运行时需要手动输入机柜服务器配置。
   后续运行会自动读取已保存的配置。
   如果服务器配置发生变化，可以通过以下命令重新配置：

       python monitor_with_rules.py --configure

--------------------------------------------------------------------------------
后续计划

1. 当前版本尚未连接实际巡检设备。
   目前用于控制巡检设备停止、移动等操作的指令均为占位接口，
   后续需要与实际硬件控制模块进行集成。

2. 不同厂商可能存在不同类型的 LED 指示灯，
   同一种颜色在不同 LED 类型下可能代表不同含义（例如 HPE 服务器）。
   后续版本可能需要进一步记录 LED 名称、位置以及对应功能，
   以提高规则匹配的准确性。

3. 当前版本尚未实现实际控制巡检设备停止运行的硬件控制代码。

--------------------------------------------------------------------------------
使用方法

1. 从文档或图片中生成 LED 规则：

       python build_led_knowledge.py HPE.png --vendor xxx --model "xxx"

   示例：

       python build_led_knowledge.py HPE.png \
       --vendor HPE \
       --model "ProLiant DL360 Gen10"

2. 启动监控：

       python monitor_with_rules.py

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