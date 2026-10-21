# ESP32-S3 网页控制 RGB 灯、DHT11 与雾化器

本工程使用笔记本上的 Ollama 本地模型进行对话和灯光意图识别，控制链路如下：

`网页 / 语音输入 -> 笔记本网关 -> 本地 Ollama 意图识别 -> ESP32 HTTP 接口 -> GPIO48 RGB 灯 / GPIO8 雾化器；DHT11 -> 温湿度查询与自动控湿`

Ollama 运行在 `127.0.0.1:11434`，默认模型为 `qwen3.5-2b-local`。模型可返回聊天、灯光、雾化器开关或温湿度查询意图；网关验证后调用 ESP32，并使用设备实际读数回答。模型不需要安装到 ESP32。

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
- `现在温度和湿度是多少`
- `打开雾化器`
- `停止雾化`
- `你好`
- `打开空调`

笔记本和 ESP32 必须连接同一个非访客 Wi-Fi。当前设备接口没有登录验证，仅用于本地演示，不要把它暴露到公网。

## 三、手机语音聊天

网页提供手机浏览器语音识别和回复朗读。语音识别结果会先填入输入框，检查无误后再发送；模型回复由手机系统语音播报。此方案不需要给 ESP32 增加麦克风或扬声器模块。

手机浏览器使用麦克风时需要安全来源，因此局域网访问要用 HTTPS。首次配置时，在 `host_gateway` 目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_https.ps1 -IPAddress 笔记本的Wi-Fi地址
```

例如笔记本 Wi-Fi 地址是 `192.168.1.20`，就把参数替换为 `192.168.1.20`。脚本会在 `host_gateway/.https` 生成本地证书。把 `.https/local-ca.cer` 安装到手机的“受信任的 CA 证书”中；Android 菜单名称因系统版本不同可能略有区别。这个证书只用于你自己的局域网网关。不要分享 `.https/local-ca-key.pem`。

然后在 `host_gateway` 目录按脚本输出的命令启动网关。手机连接同一个 Wi-Fi，打开 `https://笔记本的Wi-Fi地址:8443/`，首次访问时允许麦克风权限。手机和笔记本仍需在同一局域网；Windows 防火墙如果询问，请只允许专用网络访问。

语音识别由手机浏览器提供，具体浏览器支持和网络要求会因手机而异；部分浏览器会把录音交给其语音识别服务处理，因此语音输入并非保证全程离线。识别出的文字再发送给笔记本上的本地 Ollama。回复朗读使用手机的中文语音服务。若语音识别不可用，可继续用文字聊天。

## 四、ESP32 接口

- `GET /`：检查设备是否在线。
- `GET /api/status`：读取 RGB 灯逻辑颜色值、雾化器开关状态和 OLED 状态。
- `POST /api/action`：执行 `set_rgb` 或 `set_humidifier`。
- `POST /api/humidity/config`：保存自动控湿开关、开启湿度阈值和关闭湿度阈值。

请求示例：

```json
{"request_id":"demo-1","action":"set_rgb","r":0,"g":0,"b":255}
```

雾化器开关请求：

```json
{"request_id":"demo-2","action":"set_humidifier","on":true}
```

GPIO8 按高电平开启、低电平关闭，固件启动时会先将其设为低电平。GPIO8 只能作为控制信号，雾化器应由匹配的驱动板/功率器件供电，不要直接由 ESP32 GPIO 给雾化片供电；使用前确保雾化器已加水，避免干烧。固件会限制 RGB 灯的最大亮度。OLED 文字由笔记本网关渲染后发送给 ESP32；蜂鸣器暂时还没有接入。

DHT11 数据脚接 GPIO4，VCC 接 3.3V，GND 与 ESP32 共地。网页显示当前温度和相对湿度；开启“自动控湿”后，湿度低于“开启湿度”阈值会打开雾化器，达到“关闭湿度”阈值会关闭，中间区间保持当前状态。默认阈值为 45% 开启、50% 关闭，设置保存在 ESP32 NVS。传感器连续读取失败时，自动模式会关闭雾化器；DHT11 精度有限，自动控制是开关式的，不是连续调节雾化强度。自动模式下网页手动开关和大模型的直接开关请求会被拒绝，避免与自动控制争抢输出。

网关会把多个颜色的 RGB 分量相加并限制在 `0` 到 `255`，再按亮度百分比缩放后发送给 ESP32。例如红色和绿色会混成黄色；板载只有一颗 RGB 灯，因此不能让红、绿作为两个独立光点同时显示。只说“把灯亮度调到 30%”时会沿用当前灯色；关灯后网关也会记住最近一次颜色，供后续亮度调整恢复。

## 五、OLED 文字显示

此配置按 0.96 寸、128x64、SSD1306 I2C 屏幕设置：SCL 接 GPIO17，SDA 接 GPIO18，VCC 接 3.3V，GND 接 GND。固件会探测常见的 `0x3C` 和 `0x3D` 地址，并使用较低 I2C 速率及与已验证 OLED 程序相同的屏幕方向。串口日志出现 `SSD1306 OLED ready` 且设备状态中的 `oled_ready` 为 `true`，表示初始化成功。

网关会先把用户输入、再把模型回复发送到 OLED。每条文字从第一行开始逐屏显示一次，显示完停在最后一屏；每屏约显示 2 秒。中文由笔记本上的 Windows 中文字体渲染，因此启动网关前需执行 `python -m pip install -r requirements.txt`。

`POST /api/display` 接收一帧 128x64 单色画面，数据为 1024 字节 SSD1306 页面格式；正常聊天不需要手动调用此接口。
