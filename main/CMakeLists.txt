# ESP32 主组件(main)注册文件
# 告诉构建系统：注册应用入口和各功能模块，头文件搜索路径为当前目录，
# 并声明依赖的 ESP-IDF 组件（缺任何一个都会在编译时报找不到头文件）。
idf_component_register(
    SRCS
        "main.c"
        "app_state.c"
        "bridge_action.c"
        "bridge_curtain.c"
        "bridge_http.c"
        "bridge_humidity.c"
        "bridge_led.c"
        "bridge_oled.c"
        "bridge_wifi.c"
    INCLUDE_DIRS "."
    REQUIRES
        json              # cJSON：解析请求、生成响应
        dht               # DHT11 单总线温湿度采集
        esp_driver_gpio   # GPIO 输出：控制雾化器驱动输入
        esp_driver_i2c    # I2C 主机驱动：驱动 OLED
        esp_event         # 默认事件循环：接收 Wi-Fi / IP 事件
        esp_http_server   # HTTP 服务器：提供 / 与 /api/* 接口
        esp_lcd           # LCD 抽象层：SSD1306 面板驱动
        esp_netif         # 网络接口管理
        esp_timer         # DHT11 采样时间戳
        esp_wifi          # Wi-Fi STA 连接
        freertos          # 任务、队列、事件组和互斥锁
        led_strip         # 第三方 WS2812 RGB 灯驱动
        nvs_flash         # 非易失存储（Wi-Fi 驱动内部依赖）
)
