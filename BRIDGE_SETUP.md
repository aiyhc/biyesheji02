# ESP32-S3 网页控制 RGB 灯

本工程使用笔记本上的 Ollama 本地模型进行对话和灯光意图识别，控制链路如下：

`网页输入文字 -> 本地 Ollama -> 笔记本网关校验 -> ESP32 HTTP 接口 -> GPIO48 RGB 灯`

Ollama 运行在 `127.0.0.1:11434`，默认模型为 `qwen3.5-2b-local`。模型只负责返回受限的聊天或颜色意图；网关会再次检查颜色，只允许预设颜色通过，然后调用 ESP32。API 不需要密钥，也不需要把模型装到 ESP32。

## 一、配置并烧录 ESP32

1. 打开 `main/wifi_secrets.h`，填写你的 Wi-Fi 名称和密码。如果这个文件不存在，就把 `main/wifi_secrets.h.example` 复制一份并改名为 `wifi_secrets.h`，再填写信息。真实凭据文件已加入 Git 忽略列表，不会被提交。
2. 在 ESP-IDF 终端中进入本工程根目录。
3. 执行 `idf.py reconfigure`，让 ESP-IDF 获取 `espressif/led_strip` RGB 灯驱动组件。
4. 执行 `idf.py build` 编译工程。
5. 连接 ESP32-S3，烧录并打开串口监视器：

   ```powershell
   idf.py -p COMx flash monitor
   ```

   把 `COMx` 替换成开发板实际使用的串口号，例如 `COM6`。

6. 串口日志出现 ESP32 的 IP 地址后，在浏览器打开 `http://ESP32_IP/`。把 `ESP32_IP` 替换为日志中的实际地址；页面显示 `ESP32 bridge is online` 就说明设备已连上 Wi-Fi。

工程保留了原有 `sdkconfig`，其中包含 ESP32-S3-N16R8 的 Flash 和 PSRAM 配置。板载 RGB 灯使用 GPIO48。

## 二、启动笔记本网关

先确认 U 盘仍连接，并且 Ollama 正在运行。可以在 PowerShell 检查模型：

```powershell
ollama list
```

列表中应能看到 `qwen3.5-2b-local`。如果 Ollama 尚未启动，从 Windows 开始菜单打开 Ollama。

再开一个终端，进入工程目录后执行：

```powershell
cd host_gateway
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python gateway.py
```

测试通过后，打开网页：

`http://127.0.0.1:8080/`

在网页中填入串口日志显示的 ESP32 IP 地址，点击“连接设备”，然后试试这些指令：

- `把灯调成蓝色`
- `打开红灯`
- `亮绿色和红色，亮度 30%`
- `把灯亮度调到一半`
- `关灯`
- `你好`
- `打开空调`

笔记本和 ESP32 必须连接同一个非访客 Wi-Fi。当前设备接口没有登录验证，仅用于本地演示，不要把它暴露到公网。

## 三、ESP32 接口

- `GET /`：检查设备是否在线。
- `GET /api/status`：读取 RGB 灯当前的逻辑颜色值。
- `POST /api/action`：执行 `set_rgb` 操作，RGB 参数为 `0` 到 `255` 的整数。

请求示例：

```json
{"request_id":"demo-1","action":"set_rgb","r":0,"g":0,"b":255}
```

固件会限制 RGB 灯的最大亮度。OLED 文字由笔记本网关渲染后发送给 ESP32；蜂鸣器暂时还没有接入。

网关会把多个颜色的 RGB 分量相加并限制在 `0` 到 `255`，再按亮度百分比缩放后发送给 ESP32。例如红色和绿色会混成黄色；板载只有一颗 RGB 灯，因此不能让红、绿作为两个独立光点同时显示。只说“把灯亮度调到 30%”时会沿用当前灯色；关灯后网关也会记住最近一次颜色，供后续亮度调整恢复。

## 四、OLED 文字显示

此配置按 0.96 寸、128x64、SSD1306 I2C 屏幕设置：SCL 接 GPIO17，SDA 接 GPIO18，VCC 接 3.3V，GND 接 GND。固件会探测常见的 `0x3C` 和 `0x3D` 地址，并使用较低 I2C 速率及与已验证 OLED 程序相同的屏幕方向。串口日志出现 `SSD1306 OLED ready` 且设备状态中的 `oled_ready` 为 `true`，表示初始化成功。

网关会先把用户输入、再把模型回复发送到 OLED。每条文字从第一行开始逐屏显示一次，显示完停在最后一屏；每屏约显示 2 秒。中文由笔记本上的 Windows 中文字体渲染，因此启动网关前需执行 `python -m pip install -r requirements.txt`。

`POST /api/display` 接收一帧 128x64 单色画面，数据为 1024 字节 SSD1306 页面格式；正常聊天不需要手动调用此接口。
