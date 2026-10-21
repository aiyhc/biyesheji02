# ESP32 智能家居控制台：代码流程分析与入门指南

本文对应当前工程版本，介绍网页、笔记本网关、Ollama 模型和 ESP32-S3 固件如何协作。建议先按实际运行流程阅读，不必一开始逐行理解所有源代码。

## 1. 先记住四个部件

```text
手机或电脑浏览器
  ├─ 手动控制窗帘、灯、雾化器 ──HTTP──┐
  └─ 输入文字/语音转文字 ──HTTP──┐    │
                                  ▼    ▼
笔记本网关：host_gateway/gateway.py
  ├─ 普通聊天/设备指令 ──HTTP──> 本机 Ollama 大模型
  ├─ 校验模型 JSON，决定是否允许执行
  ├─ 代理设备状态、渲染 OLED 文字
  └─ 手动控制请求不经过模型，直接校验并转发
                                  │ HTTP / Wi-Fi
                                  ▼
ESP32-S3 固件：main/main.c
  ├─ 校验设备动作 JSON
  ├─ FreeRTOS 任务驱动 GPIO 和传感器
  └─ 返回灯、窗帘、雾化器、温湿度和 OLED 状态
```

这里是三个 HTTP 服务：笔记本网关同时提供网页和网关 API；Ollama 提供本机模型 API；ESP32 提供设备 API。Ollama 不是网页服务器。大模型运行在笔记本，不运行在 ESP32；ESP32 收到的是网关已经校验过的简单动作。

## 2. 工程文件地图

| 文件或目录 | 作用 |
| --- | --- |
| `main/main.c` | 本工程自己的 ESP32 应用逻辑：Wi-Fi、设备 HTTP 接口、传感器、GPIO、窗帘电机任务 |
| `main/CMakeLists.txt` | 声明固件编译时需要哪些 ESP-IDF 组件 |
| `main/idf_component.yml` | 声明 DHT11 和 WS2812 灯带驱动等第三方组件 |
| `managed_components/` | ESP-IDF 组件管理器下载的依赖代码；通常调用它们的 API，不直接改库源码 |
| `sdkconfig` | ESP32-S3 目标芯片、Flash、PSRAM 和其他 ESP-IDF 构建配置 |
| `host_gateway/gateway.py` | 笔记本 Python 网关：网页服务器、Ollama 请求、意图校验、设备代理 |
| `host_gateway/web/index.html` | 网页结构、样式及浏览器 JavaScript 交互逻辑 |
| `host_gateway/tests/test_gateway.py` | 网关的 Python 单元测试；不代替电机和传感器的实物测试 |
| `BRIDGE_SETUP.md` | 工程启动说明；硬件功能变更后需要同步更新 |

**关于 `main.c`：**目前项目自己编写的固件主逻辑基本集中在这一个 C 文件，但“ESP32 相关代码”并不都在其中。Wi-Fi、HTTP Server、GPIO、FreeRTOS、I2C、LCD、NVS 等底层实现来自 ESP-IDF，DHT11 和 RGB 灯带也调用了组件库。ESP-IDF 项目本身就是通过组件组合应用、驱动和系统服务。

## 3. 几个常见词

| 名词 | 在本项目中的意思 |
| --- | --- |
| HTTP 接口/路由 | 一条带路径的网络入口，例如 `POST /api/chat` 或 ESP32 的 `GET /api/status` |
| JSON | 网页、网关、ESP32 之间传递的数据格式；例如 `{"position_pct":30}` |
| 意图 `intent` | 模型判断用户想做什么，例如 `set_curtain`、`query_curtain` |
| FreeRTOS 任务 | ESP32 上并发运行的小程序；DHT11 采样和窗帘电机各自有任务 |
| Queue 队列 | HTTP 处理任务交给电机任务的动作清单；这样移动窗帘不会堵住网页请求 |
| Mutex 互斥锁 | 保护多个任务共享的窗帘位置、温湿度等状态，避免读写同时发生 |
| NVS | ESP32 的非易失存储；本项目用它保存控湿参数和窗帘校准位置 |
| 组件 | 可重复使用的驱动或系统模块，例如 `dht`、`led_strip`、`esp_http_server` |

## 4. ESP32 上电流程

固件入口是 `app_main()`（`main/main.c`）。它按顺序初始化雾化器安全输出、NVS、湿度控制、窗帘 GPIO 和队列、RGB 灯、OLED、Wi-Fi。拿到 Wi-Fi IP 后才启动 ESP32 设备 HTTP Server。

窗帘 GPIO 为 IN1=6、IN2=7、IN3=15、IN4=16；启动时四路先输出低电平。DHT11 数据脚是 GPIO4，雾化器控制脚 GPIO8（高电平开启），板载 RGB 使用 GPIO48，OLED 的 SCL/SDA 为 GPIO17/GPIO18。

注意：`app_main()` 是初始化顺序，不是所有功能的执行位置。窗帘电机在独立的 `stepper_task()` 里持续等待队列；DHT11 在 `dht11_task()` 里周期采样；HTTP Server 在 ESP-IDF 创建的服务任务中接收请求。

## 5. 聊天指令如何变成设备动作

以“把窗帘打开到 30%”为例：

1. 用户在网页输入文字并按发送。`index.html` 的聊天表单 JavaScript 把 `{text, session_id}` 发到笔记本网关的 `POST /api/chat`。
2. 网关检查请求、会话和否定表达；可明确确定的部分指令先走本地规则。窗帘位置查询也有本地识别，直接读取设备状态，避免模型编造百分比。
3. 其他需要理解的自然语言由 `call_ollama()` 发送到本机 Ollama。请求包含系统提示词、最近对话和 `DECISION_SCHEMA`。
4. 模型只负责给出意图和目标参数，例如：

   ```json
   {"intent":"set_curtain","curtain_position_pct":30}
   ```

5. `parse_model_decision()` 检查意图和百分比必须是 0 到 100 的整数。模型不是硬件控制器，也不能自行生成 GPIO 操作。
6. 网关把经过验证的目标转换为 ESP32 动作，再通过 HTTP 发往设备。目标窗帘位置的动作类似：

   ```json
   {"action":"curtain_set_position","position_pct":30,"interval_ms":20}
   ```

7. ESP32 的 `action_handler()` 再校验 JSON、动作参数及窗帘位置是否可信。通过后把动作放入 FreeRTOS 队列；独立电机任务逐步驱动 GPIO。
8. ESP32 的 `/api/status` 返回当前灯光、环境和窗帘状态；网关的 `/api/device` 代理该状态。网页收到回复后及之后的定时刷新会更新状态显示。

网页上的窗帘滑块走另一条更短的路径：JavaScript 向网关 `POST /api/curtain`，网关校验后直接转发；**手动滑块不需要调用大模型**。同样，手动灯光和雾化器开关也有直接控制接口。

这里的“模型理解”不是模型知道 GPIO 或电机电路，而是模型根据文字上下文预测一个受限的 JSON 意图。真正决定允许什么动作的是网关和 ESP32 两层校验。

## 6. 窗帘百分比和行程校准

目前没有限位输入或编码器，因此 ESP32 不会直接测量布帘位置。它根据已校准总步数和已经发出的步数估算当前位置：

```text
目标步数 = 总行程步数 × 目标百分比 ÷ 100
需要移动的步数 = 目标步数 - 当前估算步数
```

校准操作在网页窗帘面板完成：

1. 把窗帘实际放到完全关闭的位置，选择“正转时打开”或“反转时打开”，点击“设为全关端点”。此按钮只记录当前位置，不会自动把窗帘拉到全关。
2. 用较小步数、较慢间隔点动打开；接近全开端点时在机械限位前停止。
3. 点击“设为全开端点”。ESP32 用从全关累计的步数作为窗帘全行程，保存方向、位置和行程信息。
4. 校准有效后，网页滑块和模型目标百分比才可执行。系统会拒绝未校准或位置不确定时的百分比命令。

位置和校准参数保存在 NVS。开始移动时固件先把位置标为可能不准确；动作正常结束或受控停止后再保存估算位置。如果移动途中断电，重启后位置会显示不确定，需要重新校准。手动拉动窗帘、机械打滑或电机堵转也会让步数估算偏离实际位置；当前没有传感器能自动发现这些偏差。

## 7. 两边的 HTTP 接口

浏览器只访问笔记本网关。网关再访问 ESP32，因此不要把两边的接口混为一谈。

| 所在位置 | 方法与路径 | 用途 |
| --- | --- | --- |
| 网关 | `GET /` | 返回控制网页 |
| 网关 | `GET /api/config` | 返回当前模型名和已设置的设备地址 |
| 网关 | `GET /api/device` | 让网关读取 ESP32 状态并补充灯光摘要 |
| 网关 | `GET /health` | 检查笔记本网关进程是否正常 |
| 网关 | `POST /api/device/config` | 设置 ESP32 的局域网地址 |
| 网关 | `POST /api/chat` | 聊天、模型意图识别和受控设备动作 |
| 网关 | `POST /api/light` | 网页手动设置 RGB 灯，不经过模型 |
| 网关 | `POST /api/humidifier` | 网页手动开关雾化器 |
| 网关 | `POST /api/environment/config` | 设置自动控湿与湿度阈值 |
| 网关 | `POST /api/curtain` | 网页校准、点动、开合度控制和停止 |
| ESP32 | `GET /` | 简单在线探针 |
| ESP32 | `GET /api/status` | 返回 RGB、温湿度、雾化器、OLED、窗帘状态 |
| ESP32 | `POST /api/action` | 网关向设备发送经校验的动作 JSON |
| ESP32 | `POST /api/humidity/config` | 保存自动控湿设置 |
| ESP32 | `POST /api/display` | 接收网关渲染的 OLED 位图 |

窗帘网页接口中的 `operation` 是 `set_closed`、`jog`、`set_open`、`set_position` 或 `stop`。网关会把它们转换成固件动作，例如 `curtain_set_closed`、`curtain_jog`、`curtain_set_open`、`curtain_set_position` 和 `stepper_stop`。`set_position` 必须带 `position_pct` 和 `interval_ms`；点动必须带方向、步数和步进间隔。

## 8. 其他外设的代码入口

| 功能 | 当前接线 | 主要代码位置/作用 |
| --- | --- | --- |
| 板载 WS2812 RGB | GPIO48 | `init_rgb_led()` 初始化；`set_rgb` 动作设置颜色；亮度在固件中限幅 |
| 雾化器控制信号 | GPIO8，高电平开启 | 初始化时先输出低电平；`set_humidifier` 控制；自动模式与手动请求互斥 |
| DHT11 | GPIO4 | `dht11_task()` 周期采样，状态接口返回温度、湿度和数据有效标志 |
| 自动控湿 | DHT11 + GPIO8 | 低于开启阈值开启，高于关闭阈值关闭；默认 45%/50%，设置存 NVS |
| SSD1306 OLED | SCL=GPIO17、SDA=GPIO18 | ESP32 初始化和接收位图；中文文字由网关/Pillow 渲染后发送 |
| ULN2003 步进电机 | IN1/2/3/4 = GPIO6/7/15/16 | `stepper_task()` 按四相半步顺序驱动，窗帘位置以累计步数估算 |
| 蜂鸣器 | 尚未接入 | 当前固件没有蜂鸣器 GPIO 初始化或控制动作 |

手机语音输入由浏览器的语音识别接口把声音转成文字；模型收到的是文字，不是 ESP32 上的音频。回复朗读也由浏览器/手机处理。OLED 中文图像由网关生成，ESP32 不负责中文字库排版。

## 9. 常见失败该看哪里

| 现象 | 先检查什么 |
| --- | --- |
| 网页显示 ESP32 未连接 | 网页里的设备 IP 是否正确；电脑和 ESP32 是否在同一个 Wi-Fi；ESP32 串口是否已经打印 IP |
| 聊天提示 Ollama 错误/502 | Ollama 是否运行；模型名是否和 `ollama list` 一致；本机 `/api/chat` 是否可访问 |
| 窗帘控件不可用 | 是否烧录包含 `curtain` 状态字段的新固件；设备状态里是否有 `curtain` 对象 |
| 窗帘目标控制被拒绝 | 是否完成“设为全关端点 → 点动打开 → 设为全开端点”；窗帘位置是否仍可信 |
| 电机方向不符合预期 | 先停止，检查 IN1–IN4 顺序和网页“打开方向”；重新从全关端点校准 |
| 电机动作了但实际开合度不准 | 步进电机没有编码器时只能按步数估算；检查是否堵转、打滑、被手动移动，并重新校准 |
| OLED 不显示 | 串口检查 OLED 探测日志；检查 GPIO17/18、供电和 I2C 地址 |

常见 HTTP 状态码：`400` 通常是参数格式错误；`409` 通常表示窗帘正在移动、未校准或位置不确定；`502` 通常是网关连接 Ollama/ESP32 失败；`503` 通常是设备地址还未配置或服务不可用。

## 10. 测试与运行

网关单元测试只验证 Python 的参数校验、模型 JSON、窗帘命令解析等逻辑，不会让电机真的转动：

```powershell
cd host_gateway
python -m unittest discover -s tests -v
```

ESP-IDF 编译验证固件能否构建；只有接上真实 ESP32 并正确烧录，才算完成硬件验证：

```powershell
idf.py build
idf.py -p COMx flash monitor
```

实际测试建议记录：网页/模型指令、目标百分比、校准总步数、实际窗帘位置、是否到位、响应时间和失败原因。至少覆盖全关、25%、50%、75%、全开；每个位置重复多次。还要测试 Ollama 关闭、ESP32 断网、位置未校准、动作中停止、移动中断电后的表现。

## 11. 推荐阅读顺序

1. 先看本文件第 1 节，画出网页、网关、模型、ESP32 四层。
2. 在 `main/main.c` 读 `app_main()`，只理解上电初始化顺序。
3. 接着读 `action_handler()`，找到 `curtain_set_position` 分支；它负责接收、校验设备命令。
4. 再读 `queue_curtain_command_locked()` 和 `stepper_task()`，理解 HTTP 如何把动作交给 FreeRTOS 队列，任务如何逐步输出 GPIO。
5. 在 `host_gateway/gateway.py` 读 `SYSTEM_PROMPT`、`DECISION_SCHEMA`、`call_ollama()` 和 `parse_model_decision()`，理解模型输入、输出格式和校验。
6. 再跟 `GatewayHandler.do_POST()` 里的 `/api/chat`，看 `set_curtain` 如何变成 ESP32 动作、`query_curtain` 如何读取真实状态。
7. 最后看 `host_gateway/web/index.html` 的窗帘区、`sendCurtainRequest()` 和 `refreshDevice()`，理解按钮/滑块怎样发请求和刷新显示。

不要试图一次读完 `main.c`。先只追踪一条命令，例如“把窗帘打开到30%”：记录每一层收到的 JSON、调用的函数和返回状态。能把这条链路讲清楚，再逐个学习 C 语法、HTTP、JSON、任务和锁，理解会容易很多。
