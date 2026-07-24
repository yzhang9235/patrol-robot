# Grounding DINO 自动标定版本

用 Grounding DINO 自动检测面板位置 + 每颗LED位置，减少手动逐颗框选的工作量。
标出来的数据格式跟原来手动版本(`monitor_with_rules.py --calibrate`)完全一样，
标完之后直接用原来的 `monitor_with_rules.py` 正常巡检即可，不需要改任何东西。

**这套代码没有在真实摄像头/真实GPU环境下跑通测试过**，逻辑是按Grounding DINO
官方demo的标准调用方式写的，但环境搭建、模型下载这些步骤请按下面一步步来，
中途报错很正常，遇到了随时把报错信息发回来一起排查。

---

## 1. 目录结构要求

这个文件夹要放在你原项目文件夹(`巡检/`)下面一层：

```
巡检/
  monitor_with_rules.py
  led_knowledge_lookup.py
  config_manager.py
  build_led_knowledge.py
  knowledge/
  config/
  grounding_dino_calibration/      <- 这个文件夹
    calibrate_with_dino.py
    dino_detector.py
    requirements.txt
    README.md
```

如果你的实际目录结构不一样，运行时用 `--project-dir` 参数指定一下
`巡检/` 这个文件夹的路径。

---

## 2. 安装依赖

### 2.1 基础依赖

```bash
cd 巡检/grounding_dino_calibration
pip3 install -r requirements.txt
```

### 2.2 安装 Grounding DINO 本体

Grounding DINO 不在pypi上正式发布，需要从github源码装：

```bash
git clone https://github.com/IDEA-Research/GroundingDINO.git
cd GroundingDINO
pip3 install -e .
cd ..
```

**Mac(没有NVIDIA GPU)注意**：GroundingDINO默认会尝试编译一个CUDA加速算子，
纯CPU的Mac编译这一步经常会失败。如果 `pip3 install -e .` 报错跟CUDA编译相关，
可以设置这个环境变量后重装，会自动退化成纯PyTorch实现(慢一些，但能跑)：

```bash
export BUILD_WITH_CUDA=0
pip3 install -e . --no-build-isolation
```

### 2.3 下载模型权重和配置文件

```bash
# 在 GroundingDINO 文件夹里执行
mkdir -p weights
cd weights
wget https://github.com/IDEA-Research/GroundingDINO/releases/download/v0.1.0-alpha/groundingdino_swint_ogc.pth
cd ..
```

模型结构配置文件已经在你clone下来的仓库里，路径是：
```
GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py
```

如果 `wget` 在你的网络环境下载不动，也可以直接用浏览器打开这个链接手动下载，
存到 `GroundingDINO/weights/groundingdino_swint_ogc.pth`。

---

## 3. 运行

```bash
cd 巡检/grounding_dino_calibration

python3 calibrate_with_dino.py \
    --station server_01 \
    --vendor NVIDIA \
    --model "DGX A100" \
    --dino-config /path/to/GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py \
    --dino-checkpoint /path/to/GroundingDINO/weights/groundingdino_swint_ogc.pth
```
把 `/path/to/GroundingDINO/` 换成你实际clone下来的路径。

例：
python3 -u calibrate_with_dino.py \
    --station server_01 \
    --vendor NVIDIA \
    --model "DGX A100" \
    --dino-config /Users/elaine/Desktop/巡检/grounding_dino_calibration/GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py \
    --dino-checkpoint /Users/elaine/Desktop/巡检/grounding_dino_calibration/GroundingDINO/weights/groundingdino_swint_ogc.pth

### 常用可选参数

```
--camera <地址>       不填就用脚本里写死的CAMERA_SOURCE_DEFAULT，
                       跟monitor_with_rules.py里的CAMERA_SOURCE保持一致就行
--device cpu          Mac没有NVIDIA GPU的话建议显式指定cpu
                       (不指定会自动判断，但显式指定更保险)
--box-threshold 0.25  检测框太少(漏检明显)就调低一点，比如0.2
                       检测框太多(一堆误检)就调高一点，比如0.4
--panel-prompt "..."  自定义面板检测的文字描述
--led-prompt "..."    自定义LED检测的文字描述
```

---

## 4. 使用流程

1. 脚本抓一帧摄像头画面
2. 用 `--panel-prompt` 描述去检测面板位置，把候选框标号显示出来，**你在终端输入编号选择正确的那个**；如果一个候选框都没有(检测不到)，会自动退化成手动点两下框选
3. 裁剪到面板范围附近，用 `--led-prompt` 描述去检测每一颗LED，把所有候选框标号显示出来
4. **你可以**：
   - 直接回车 = 全部保留
   - 输入编号(比如 `2 5`) = 删掉这几个误检的框
   - 输入 `add` = 手动补框一个漏检的LED
   - 输入 `done` = 确认，进入下一步
5. 对保留下来的每一颗LED，终端里挨个问名字(component_name)，不想起名字直接回车会用默认名 `led_1`/`led_2`...
6. 自动保存到跟原来手动版本一样的位置：
   - `knowledge/<vendor>_<model>.json` 里的 `led_positions`
   - `config/runtime_config.json` 里对应station的 `panel_bbox`
7. 弹出最终确认画面，肉眼核对一下框的位置对不对

---

## 5. 跟原来手动版本的关系

- 存储格式完全一致，两边随便混用：这次用Grounding DINO标定，
  下次某个型号想重新标定也可以直接用 `monitor_with_rules.py --calibrate`
  手动点选，覆盖的是同一份数据
- **建议第一次用新型号时，两种方式都试一下、对比效果**，如果Grounding DINO
  检测某个型号效果不好(比如面板反光严重、LED太小),完全可以退回纯手动点选，
  不影响整体流程

---

## 6. 常见问题

**Q: `ModuleNotFoundError: No module named 'groundingdino'`**
第2.2步没装成功，回去看看 `pip3 install -e .` 那一步有没有报错。

**Q: 检测框一个都没有 / 全是误检**
先试试调低/调高 `--box-threshold`；如果还是不行，试试换一下 `--panel-prompt`
或 `--led-prompt` 的措辞，Grounding DINO对文字描述的具体用词比较敏感，
比如"LED"识别不好可以试试"small round light"、"indicator light"这类更具体的描述。

**Q: CPU跑起来很慢**
纯CPU下单张图推理可能要几秒到十几秒，标定是一次性操作，能接受的话不用管；
如果频繁想要重新标定测试效果，可以考虑用有GPU的机器跑这一步，
标完的数据文件拷回Mac用就行，不影响正常巡检(巡检完全不用GPU)。
