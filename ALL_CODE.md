# ESP32-S3 AI 控制台 —— 全工程代码总览

> 本文由工程源码自动汇总生成，下面每一段代码都与磁盘上的文件逐字一致（含中文注释）。
> 单文件方便通读，实际修改代码时请编辑对应的源文件。

## 一、这是什么

基于 ESP32-S3 的「AI 控灯控制台」，由三部分组成：

```
浏览器页面 --HTTP--> 笔记本网关(Python) --HTTP--> ESP32-S3 固件
                            |
                            +--HTTP--> 本地 Ollama 模型（意图识别）
```

1. 在浏览器输入中文，例如「亮绿色和红色，亮度 30%」；
2. 笔记本上的 `gateway.py` 先用本地正则解析，解析不出再交给本地 Ollama 判断意图；
3. 网关把决策换算成 RGB，POST 给 ESP32 的 `/api/action`，点亮 GPIO48 上的 WS2812；
4. 同时把对话文字渲染成 128x64 位图，通过 `/api/display` 显示在 SSD1306 OLED 上。

## 二、文件作用一览

| 文件 | 作用 |
| --- | --- |
| `main/main.c` | 设备端固件：Wi-Fi 连接、HTTP 服务器（4 个接口）、RGB 灯、OLED |
| `host_gateway/gateway.py` | 笔记本网关：Web 服务、调用 Ollama、指令校验、颜色换算、OLED 渲染 |
| `host_gateway/web/index.html` | 前端单页：聊天、示例按钮、设备地址配置、状态展示 |
| `host_gateway/tests/test_gateway.py` | 单元测试：模型输出校验、中文指令解析、混色、OLED 帧、地址校验 |
| `host_gateway/requirements.txt` | 网关依赖：Pillow（中文文字渲染） |
| `CMakeLists.txt` | ESP-IDF 顶层工程定义 |
| `main/CMakeLists.txt` | main 组件注册与依赖声明 |
| `main/idf_component.yml` | 第三方组件依赖：led_strip |
| `main/wifi_secrets.h.example` | Wi-Fi 凭据模板（真实的 `wifi_secrets.h` 被 git 忽略） |
| `.clangd` | 编辑器 clangd 语言服务器配置 |
| `.devcontainer/*` | VS Code 开发容器：Dockerfile 与 devcontainer.json |
| `.gitignore` | Git 忽略规则 |
| `dependencies.lock` | 组件管理器自动生成的锁定文件（勿手改） |
| `BRIDGE_SETUP.md` | 项目搭建与使用说明 |

## 三、快速上手

```powershell
# 1. 设备端：先复制凭据模板并填好 Wi-Fi，再编译烧录
copy main\wifi_secrets.h.example main\wifi_secrets.h
idf.py reconfigure
idf.py -p COM6 flash monitor

# 2. 网关（另开终端，保持 Ollama 运行）
cd host_gateway
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python gateway.py

# 3. 浏览器打开 http://127.0.0.1:8080/ ，填入串口日志中的 ESP32 地址
```

---

## 四、源码与配置

## main/main.c

```c
/*
 * ESP32-S3 AI 控制台 —— 设备端固件
 *
 * 整体链路：
 *   网页输入文字 -> 笔记本网关(host_gateway/gateway.py) -> 本地 Ollama 模型
 *   -> 网关校验并换算成 RGB -> 调用本设备 HTTP 接口 -> GPIO48 RGB 灯 / OLED 屏
 *
 * 本文件承担的职责：
 *   1. 以 STA 模式连接 Wi-Fi，断线自动重连；
 *   2. 启动 HTTP 服务器，对外提供 4 个接口：
 *        GET  /             在线探针，返回一行纯文本
 *        GET  /api/status   返回 RGB 灯当前逻辑颜色与 OLED 是否就绪
 *        POST /api/action   执行 set_rgb 动作，点亮 GPIO48 上的 WS2812
 *        POST /api/display  接收 1024 字节 SSD1306 页面格式位图并刷到 OLED
 *   3. 初始化板载 RGB 灯（RMT 驱动）和 SSD1306 OLED（I2C 接口）。
 *
 * 使用前提：把 main/wifi_secrets.h.example 复制为 main/wifi_secrets.h
 *          并填写真实 Wi-Fi 名称与密码，否则编译会失败或连不上网。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ESP-IDF 组件头文件：JSON、I2C/OLED、事件循环、HTTP 服务、日志、Wi-Fi 等 */
#include "cJSON.h"
#include "driver/i2c_master.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_lcd_io_i2c.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_ssd1306.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "led_strip.h"
#include "led_strip_rmt.h"
#include "nvs_flash.h"
#include "wifi_secrets.h" /* 由 wifi_secrets.h.example 复制而来，内含 Wi-Fi 凭据 */

/* ---------------- 硬件与行为参数 ---------------- */

/* 板载 WS2812 RGB 灯：单颗灯珠接在 GPIO48 */
#define RGB_LED_GPIO 48
#define RGB_LED_COUNT 1
/* 亮度上限：实际写入灯珠的 RGB 分量会被等比例缩放到 0~48，
 * 目的是避免满亮刺眼、同时降低瞬时电流 */
#define RGB_BRIGHTNESS_LIMIT 48

/* OLED：0.96 寸 128x64 SSD1306，使用 I2C 接口 */
#define OLED_SCL_GPIO 17
#define OLED_SDA_GPIO 18
#define OLED_I2C_CLOCK_HZ 50000 /* 刻意用较低速率，兼容性优于速度 */
#define OLED_I2C_ADDR_PRIMARY 0x3C   /* 最常见的 SSD1306 地址 */
#define OLED_I2C_ADDR_SECONDARY 0x3D /* 备选地址，初始化时会自动探测 */
#define OLED_WIDTH 128
#define OLED_HEIGHT 64
/* 单色位图字节数：128 * 64 / 8 = 1024 字节 */
#define OLED_FRAME_BYTES (OLED_WIDTH * OLED_HEIGHT / 8)

/* Wi-Fi 事件组中代表"已获取 IP、可以开始服务"的标志位 */
#define WIFI_CONNECTED_BIT (1U << 0)
/* /api/action 请求体长度上限，防止超大请求耗尽内存 */
#define MAX_ACTION_BODY 512

static const char *TAG = "bridge"; /* 日志统一前缀，便于串口过滤 */

static EventGroupHandle_t s_wifi_event_group; /* 用于等待 Wi-Fi 连接完成 */
static led_strip_handle_t s_rgb_strip;        /* RGB 灯驱动句柄 */
static httpd_handle_t s_http_server;          /* HTTP 服务器句柄 */

static i2c_master_bus_handle_t s_oled_i2c_bus;    /* OLED 使用的 I2C 总线 */
static esp_lcd_panel_io_handle_t s_oled_panel_io; /* OLED 面板 IO 通道 */
static esp_lcd_panel_handle_t s_oled_panel;       /* OLED 面板；为 NULL 表示不可用 */
static uint8_t s_oled_blank_frame[OLED_FRAME_BYTES]; /* 全黑画面，用于初始化清屏 */

/* 灯当前的"逻辑颜色"，取值范围 0~255，与网关下发的值保持一致；
 * 实际写灯时才会乘上亮度限幅系数 */
static uint8_t s_rgb_r;
static uint8_t s_rgb_g;
static uint8_t s_rgb_b;

/*
 * 把 cJSON 对象序列化为 JSON 文本并作为响应发出。
 * 返回值沿用 httpd_resp_sendstr 的结果，字符串内存由本函数负责释放。
 */
static esp_err_t send_json(httpd_req_t *req, cJSON *root)
{
    char *body = cJSON_PrintUnformatted(root);
    if (body == NULL) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "JSON allocation failed");
        return ESP_FAIL;
    }

    httpd_resp_set_type(req, "application/json; charset=utf-8");
    esp_err_t err = httpd_resp_sendstr(req, body);
    cJSON_free(body);
    return err;
}

/*
 * 统一的错误响应：构造 {"ok":false,"error":msg}，
 * 并把内部的数字状态码映射成 HTTP 状态行文本。
 */
static esp_err_t send_error(httpd_req_t *req, int status, const char *message)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "JSON allocation failed");
        return ESP_FAIL;
    }

    cJSON_AddBoolToObject(root, "ok", false);
    cJSON_AddStringToObject(root, "error", message);
    /* 只支持 400/404/503/500 四种错误，其余一律按 500 处理 */
    httpd_resp_set_status(req, status == 400 ? "400 Bad Request" :
                               status == 404 ? "404 Not Found" :
                               status == 503 ? "503 Service Unavailable" :
                               "500 Internal Server Error");
    esp_err_t err = send_json(req, root);
    cJSON_Delete(root);
    return err;
}

/*
 * GET / ：最简单的在线探针。
 * 浏览器或网关只要能拿到 "ESP32 bridge is online"，就说明设备已连上 Wi-Fi。
 */
static esp_err_t root_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    return httpd_resp_sendstr(req, "ESP32 bridge is online");
}

/*
 * GET /api/status ：返回灯的当前逻辑颜色以及 OLED 是否成功初始化。
 * 网关在"只调亮度"的场景下会先读这里，拿到当前颜色再重新计算。
 */
static esp_err_t status_handler(httpd_req_t *req)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *rgb = cJSON_CreateObject();
    if (root == NULL || rgb == NULL) {
        /* 两个对象都要检查，任一失败就不能继续 */
        cJSON_Delete(root);
        cJSON_Delete(rgb);
        return send_error(req, 500, "JSON allocation failed");
    }

    cJSON_AddBoolToObject(root, "ok", true);
    /* 逻辑颜色（0~255），不是实际点灯的缩放值 */
    cJSON_AddNumberToObject(rgb, "r", s_rgb_r);
    cJSON_AddNumberToObject(rgb, "g", s_rgb_g);
    cJSON_AddNumberToObject(rgb, "b", s_rgb_b);
    cJSON_AddItemToObject(root, "rgb", rgb);
    /* OLED 未探测到时为 false，网关侧不会向不可用的屏推图 */
    cJSON_AddBoolToObject(root, "oled_ready", s_oled_panel != NULL);

    esp_err_t err = send_json(req, root);
    cJSON_Delete(root);
    return err;
}

/*
 * 从 JSON 对象里读取一个 0~255 的整数颜色分量。
 * 要求：字段存在、是数字、必须为整数（拒绝 3.5 这类小数）、且在 0~255 之间。
 * 校验通过后写入 *value 并返回 true。
 */
static bool read_rgb_value(const cJSON *root, const char *key, int *value)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    if (!cJSON_IsNumber(item) || item->valuedouble < 0.0 ||
        item->valuedouble > 255.0 || item->valuedouble != (double)item->valueint) {
        return false;
    }
    *value = item->valueint;
    return true;
}

/*
 * POST /api/action ：灯光控制的唯一写接口。
 *
 * 请求体示例：
 *   {"request_id":"demo-1","action":"set_rgb","r":0,"g":0,"b":255}
 *
 * 处理步骤：
 *   1. 校验请求体长度合法，并完整读取到栈上缓冲区；
 *   2. 解析 JSON，只接受 action == "set_rgb"；
 *   3. 校验 r/g/b 都是 0~255 的整数；
 *   4. 按 RGB_BRIGHTNESS_LIMIT 等比例缩放后写入灯珠并刷新；
 *   5. 记录逻辑颜色，返回 {"ok":true,"state":{"rgb":{...}}}，并回显 request_id。
 */
static esp_err_t action_handler(httpd_req_t *req)
{
    /* 空请求或超长请求直接拒绝 */
    if (req->content_len <= 0 || req->content_len > MAX_ACTION_BODY) {
        return send_error(req, 400, "Invalid request size");
    }

    /* 把完整请求体读进栈缓冲区，末尾补 '\0' 方便按字符串解析 */
    char body[MAX_ACTION_BODY + 1];
    size_t received = 0;
    while (received < (size_t)req->content_len) {
        int count = httpd_req_recv(req, body + received, req->content_len - received);
        if (count <= 0) {
            return ESP_FAIL; /* 连接中断 */
        }
        received += (size_t)count;
    }
    body[received] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return send_error(req, 400, "Invalid JSON");
    }

    /* 目前只实现 set_rgb 一个动作 */
    const cJSON *action = cJSON_GetObjectItemCaseSensitive(root, "action");
    if (!cJSON_IsString(action) || strcmp(action->valuestring, "set_rgb") != 0) {
        cJSON_Delete(root);
        return send_error(req, 400, "Unsupported action");
    }

    /* 三个颜色分量必须同时合法 */
    int r;
    int g;
    int b;
    if (!read_rgb_value(root, "r", &r) ||
        !read_rgb_value(root, "g", &g) ||
        !read_rgb_value(root, "b", &b)) {
        cJSON_Delete(root);
        return send_error(req, 400, "RGB values must be integers from 0 to 255");
    }

    /* 等比例缩小到亮度上限，避免满亮 */
    uint8_t scaled_r = (uint8_t)(r * RGB_BRIGHTNESS_LIMIT / 255);
    uint8_t scaled_g = (uint8_t)(g * RGB_BRIGHTNESS_LIMIT / 255);
    uint8_t scaled_b = (uint8_t)(b * RGB_BRIGHTNESS_LIMIT / 255);
    esp_err_t err = led_strip_set_pixel(s_rgb_strip, 0, scaled_r, scaled_g, scaled_b);
    if (err == ESP_OK) {
        err = led_strip_refresh(s_rgb_strip); /* 真正把数据推到灯珠 */
    }
    if (err != ESP_OK) {
        cJSON_Delete(root);
        ESP_LOGE(TAG, "RGB update failed: %s", esp_err_to_name(err));
        return send_error(req, 500, "RGB update failed");
    }

    /* 点灯成功后再更新逻辑颜色，保证状态接口与实际一致 */
    s_rgb_r = (uint8_t)r;
    s_rgb_g = (uint8_t)g;
    s_rgb_b = (uint8_t)b;

    /* 拼装响应：{"ok":true,"request_id":...,"state":{"rgb":{r,g,b}}} */
    cJSON *response = cJSON_CreateObject();
    cJSON *state = cJSON_CreateObject();
    cJSON *rgb = cJSON_CreateObject();
    if (response == NULL || state == NULL || rgb == NULL) {
        cJSON_Delete(root);
        cJSON_Delete(response);
        cJSON_Delete(state);
        cJSON_Delete(rgb);
        return send_error(req, 500, "JSON allocation failed");
    }

    cJSON_AddBoolToObject(response, "ok", true);
    /* 有 request_id 就原样回显，便于网关做请求对账 */
    const cJSON *request_id = cJSON_GetObjectItemCaseSensitive(root, "request_id");
    if (cJSON_IsString(request_id)) {
        cJSON_AddStringToObject(response, "request_id", request_id->valuestring);
    }
    cJSON_AddNumberToObject(rgb, "r", s_rgb_r);
    cJSON_AddNumberToObject(rgb, "g", s_rgb_g);
    cJSON_AddNumberToObject(rgb, "b", s_rgb_b);
    cJSON_AddItemToObject(state, "rgb", rgb);
    cJSON_AddItemToObject(response, "state", state);

    err = send_json(req, response);
    cJSON_Delete(root);
    cJSON_Delete(response);
    return err;
}

/*
 * POST /api/display ：接收一帧 128x64 单色位图并显示。
 *
 * 数据格式为 SSD1306 的页面(page)格式，固定 1024 字节，不带任何头部，
 * Content-Type 为 application/octet-stream。OLED 不可用或长度不符时返回错误。
 * 正常聊天流程里由笔记本网关渲染中文后调用，用户无需手动调用。
 */
static esp_err_t display_handler(httpd_req_t *req)
{
    /* OLED 没初始化成功时明确告知调用方 */
    if (s_oled_panel == NULL) {
        return send_error(req, 503, "OLED is not available");
    }
    if (req->content_len != OLED_FRAME_BYTES) {
        return send_error(req, 400, "Display frame must be 1024 bytes");
    }

    /* 一帧 1KB，用堆内存避免占用栈空间 */
    uint8_t *frame = malloc(OLED_FRAME_BYTES);
    if (frame == NULL) {
        return send_error(req, 500, "Display frame allocation failed");
    }

    /* 循环读取，直到收满 1024 字节 */
    size_t received = 0;
    while (received < OLED_FRAME_BYTES) {
        int count = httpd_req_recv(req, (char *)frame + received, OLED_FRAME_BYTES - received);
        if (count <= 0) {
            free(frame);
            return ESP_FAIL;
        }
        received += count;
    }

    /* 一次性把整幅画面刷到屏幕 */
    esp_err_t err = esp_lcd_panel_draw_bitmap(
        s_oled_panel, 0, 0, OLED_WIDTH, OLED_HEIGHT, frame);
    free(frame);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OLED frame update failed: %s", esp_err_to_name(err));
        return send_error(req, 500, "OLED frame update failed");
    }

    cJSON *response = cJSON_CreateObject();
    if (response == NULL) {
        return send_error(req, 500, "JSON allocation failed");
    }
    cJSON_AddBoolToObject(response, "ok", true);
    err = send_json(req, response);
    cJSON_Delete(response);
    return err;
}

/*
 * 启动 HTTP 服务器并注册全部路由。
 * max_uri_handlers 需要 >= 实际注册的路由数（这里是 4 个，留了 1 个余量）。
 */
static esp_err_t start_http_server(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 5;
    esp_err_t err = httpd_start(&s_http_server, &config);
    if (err != ESP_OK) {
        return err;
    }

    /* 四个路由：探针、状态查询、灯光控制、OLED 刷图 */
    const httpd_uri_t root_uri = {
        .uri = "/",
        .method = HTTP_GET,
        .handler = root_handler,
    };
    const httpd_uri_t status_uri = {
        .uri = "/api/status",
        .method = HTTP_GET,
        .handler = status_handler,
    };
    const httpd_uri_t action_uri = {
        .uri = "/api/action",
        .method = HTTP_POST,
        .handler = action_handler,
    };
    const httpd_uri_t display_uri = {
        .uri = "/api/display",
        .method = HTTP_POST,
        .handler = display_handler,
    };

    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &root_uri));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &status_uri));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &action_uri));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &display_uri));
    ESP_LOGI(TAG, "HTTP control server started on port 80");
    return ESP_OK;
}

/*
 * Wi-Fi 与 IP 事件回调：
 *   - STA 启动后主动发起连接；
 *   - 断线时清除标志位并立即重连（无限重试）；
 *   - 拿到 IP 后打印访问地址，并置位 WIFI_CONNECTED_BIT 通知主流程。
 */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "Wi-Fi disconnected; reconnecting");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        /* 拿到 IP 说明可以对外提供 HTTP 服务了 */
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Connected. Open http://" IPSTR "/", IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/*
 * 初始化 Wi-Fi STA 模式：
 *   1. 创建事件组、初始化 netif 与默认事件循环；
 *   2. 注册事件回调；
 *   3. 用 wifi_secrets.h 中的宏填充 SSID/密码并启动。
 * 若宏仍是占位符则直接报错返回，避免无意义的重连刷屏。
 */
static void init_wifi_station(void)
{
    s_wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(s_wifi_event_group == NULL ? ESP_ERR_NO_MEM : ESP_OK);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *sta_netif = esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(sta_netif == NULL ? ESP_FAIL : ESP_OK);

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               wifi_event_handler, NULL));

    /* SSID/密码来源于 wifi_secrets.h，用 snprintf 保证不越界 */
    wifi_config_t wifi_config = {0};
    snprintf((char *)wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid), "%s",
             APP_WIFI_SSID);
    snprintf((char *)wifi_config.sta.password, sizeof(wifi_config.sta.password), "%s",
             APP_WIFI_PASSWORD);
    /* 空密码视为开放网络，否则按 WPA2 处理 */
    wifi_config.sta.threshold.authmode =
        strlen(APP_WIFI_PASSWORD) == 0 ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;

    /* 占位符未替换时给出明确提示并放弃连接 */
    if (strlen(APP_WIFI_SSID) == 0 || strcmp(APP_WIFI_SSID, "YOUR_WIFI_SSID") == 0) {
        ESP_LOGE(TAG, "Set APP_WIFI_SSID in main/wifi_secrets.h before flashing.");
        return;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
}

/*
 * 初始化板载 RGB 灯：
 *   使用 RMT 外设驱动单颗 WS2812（GRB 顺序），初始化为熄灭状态。
 *   分辨率 10MHz 是 WS2812 时序的常规取值；单灯不需要 DMA。
 */
static void init_rgb_led(void)
{
    const led_strip_config_t strip_config = {
        .strip_gpio_num = RGB_LED_GPIO,
        .max_leds = RGB_LED_COUNT,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB, /* WS2812 内部按 GRB 排列 */
        .led_model = LED_MODEL_WS2812,
        .flags.invert_out = false,
    };
    const led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
        .flags.with_dma = false,
    };

    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &s_rgb_strip));
    ESP_ERROR_CHECK(led_strip_clear(s_rgb_strip)); /* 上电先熄灭，避免乱闪 */
}

/*
 * 初始化 SSD1306 OLED：
 *   1. 建立 I2C 主机总线（软件上拉）；
 *   2. 依次探测 0x3C、0x3D 两个常见地址，找不到就放弃（不影响其他功能）；
 *   3. 创建面板 IO 与 SSD1306 面板，复位、初始化、镜像、开显示；
 *   4. 刷一帧全黑清屏。
 * 任何一步失败都会把 s_oled_panel 置空，此时状态接口上报 oled_ready=false。
 */
static void init_oled(void)
{
    const i2c_master_bus_config_t bus_config = {
        .i2c_port = -1, /* 自动选择可用端口 */
        .sda_io_num = OLED_SDA_GPIO,
        .scl_io_num = OLED_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&bus_config, &s_oled_i2c_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OLED I2C bus init failed: %s", esp_err_to_name(err));
        return;
    }

    /* 先试主地址，失败再试备用地址 */
    uint8_t address = OLED_I2C_ADDR_PRIMARY;
    err = i2c_master_probe(s_oled_i2c_bus, address, 100);
    if (err != ESP_OK) {
        address = OLED_I2C_ADDR_SECONDARY;
        err = i2c_master_probe(s_oled_i2c_bus, address, 100);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "No OLED found at I2C address 0x3C or 0x3D");
        return;
    }

    /* 用 8 位命令/参数、控制字节在 bit6 的时序协议对接 SSD1306 */
    const esp_lcd_panel_io_i2c_config_t io_config = {
        .dev_addr = address,
        .scl_speed_hz = OLED_I2C_CLOCK_HZ,
        .control_phase_bytes = 1,
        .dc_bit_offset = 6,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    err = esp_lcd_new_panel_io_i2c(s_oled_i2c_bus, &io_config, &s_oled_panel_io);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OLED panel IO init failed: %s", esp_err_to_name(err));
        return;
    }

    /* SSD1306 面板参数：1 bit/像素，无硬件复位脚（reset_gpio_num = -1） */
    esp_lcd_panel_ssd1306_config_t ssd1306_config = {
        .height = OLED_HEIGHT,
    };
    const esp_lcd_panel_dev_config_t panel_config = {
        .bits_per_pixel = 1,
        .reset_gpio_num = -1,
        .vendor_config = &ssd1306_config,
    };
    err = esp_lcd_new_panel_ssd1306(s_oled_panel_io, &panel_config, &s_oled_panel);
    if (err == ESP_OK) {
        err = esp_lcd_panel_reset(s_oled_panel);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_init(s_oled_panel);
    }
    if (err == ESP_OK) {
        /* 两个方向都镜像，与已实测的屏幕方向保持一致 */
        err = esp_lcd_panel_mirror(s_oled_panel, true, true);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_disp_on_off(s_oled_panel, true);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OLED panel init failed: %s", esp_err_to_name(err));
        s_oled_panel = NULL; /* 标记 OLED 不可用，避免后续误用 */
        return;
    }

    /* 清屏，防止上电残留随机噪点 */
    err = esp_lcd_panel_draw_bitmap(
        s_oled_panel, 0, 0, OLED_WIDTH, OLED_HEIGHT, s_oled_blank_frame);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OLED clear failed: %s", esp_err_to_name(err));
        s_oled_panel = NULL;
        return;
    }
    ESP_LOGI(TAG, "SSD1306 OLED ready on SDA=%d SCL=%d address=0x%02X",
             OLED_SDA_GPIO, OLED_SCL_GPIO, address);
}

/*
 * 初始化 NVS（非易失存储）。
 * Wi-Fi 驱动内部依赖 NVS；若分区损坏或版本不符则擦除后重建。
 */
static void init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

/*
 * 固件入口。
 * 顺序：NVS -> RGB 灯 -> OLED -> Wi-Fi；
 * 阻塞等待拿到 IP 后才启动 HTTP 服务器，最后停在空循环里保持任务存活。
 */
void app_main(void)
{
    init_nvs();
    init_rgb_led();
    init_oled();
    init_wifi_station();

    /* 无限等待，直到 Wi-Fi 连接成功（事件回调里置位） */
    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_ERROR_CHECK(start_http_server());

    /* HTTP 服务器已在独立任务中运行，这里保持 app_main 不退出即可 */
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

```

## host_gateway/gateway.py

```python
"""ESP32 AI 控制台的笔记本网关。

本模块是整条链路的"大脑"，负责在浏览器、本地 Ollama 模型和 ESP32 之间中转：

    网页(index.html) --HTTP--> 本网关 --HTTP--> 本地 Ollama 拿决策
                                  \\--HTTP--> ESP32 /api/action 控制 RGB 灯
                                  \\--HTTP--> ESP32 /api/display 显示 OLED 文字

设计要点：
  * 模型输出不可全信：先做本地正则解析，再对模型返回的 JSON 做严格校验，
    只有合法颜色/亮度才会真正下发硬件指令；
  * 设备地址只允许局域网 IPv4，避免把控制接口暴露到公网；
  * OLED 文字渲染依赖 Pillow 和 Windows 中文字体，渲染与发送在独立线程里做，
    不阻塞聊天请求。

启动方式见项目根目录 BRIDGE_SETUP.md。
"""

import argparse
import ipaddress
import json
import os
import queue
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    # Pillow 只用于把文字渲染成 OLED 位图，属于可选依赖
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None


# 静态网页目录，do_GET 时从这里读取 index.html
WEB_ROOT = Path(__file__).parent / "web"
# 单个请求体上限（字节），防止超大请求
MAX_BODY_BYTES = 4096
# 每个会话保留最近多少条消息作为上下文
MAX_SESSION_MESSAGES = 12
# 最多同时保留多少个会话（超出后丢弃最早的）
MAX_SESSIONS = 64
# Ollama 对话接口与默认模型，可用环境变量覆盖
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5-2b-local")
# OLED 屏幕尺寸与一帧的字节数（128*64/8）
OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_FRAME_BYTES = OLED_WIDTH * OLED_HEIGHT // 8
# 每屏显示几行文字
OLED_LINES_PER_PAGE = 3
# 每屏停留秒数
OLED_PAGE_DELAY = 2.0

@lru_cache(maxsize=2)
def _oled_font(size):
    """加载 Windows 自带的中文字体，供 OLED 渲染使用。

    按优先级尝试微软雅黑、宋体、黑体；结果做了缓存，避免重复加载。
    找不到时抛 RuntimeError，由上层捕获并提示。
    """
    if ImageFont is None:
        raise RuntimeError("OLED text rendering requires Pillow; install host_gateway/requirements.txt.")
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
        path = font_dir / filename
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    raise RuntimeError("No Windows Chinese font was found for OLED text rendering.")


def _wrap_oled_text(text, font):
    """按屏幕像素宽度把文本折行（中英文混排按实际渲染宽度计算）。

    返回行列表；保留原文中的换行符，空文本返回一个空行。
    """
    lines = []
    line = ""
    width = 0
    for char in text.replace("\r\n", "\n").replace("\r", "\n"):
        if char == "\n":
            lines.append(line)
            line = ""
            width = 0
            continue
        char_width = font.getlength(char)
        # 再加一个字符就超宽时先断行
        if line and width + char_width > OLED_WIDTH:
            lines.append(line)
            line = ""
            width = 0
        line += char
        width += char_width
    if line or not lines:
        lines.append(line)
    return lines


def _image_to_ssd1306_frame(image):
    """把 Pillow 的 1 位图像转换成 SSD1306 的页面(page)格式字节流。

    SSD1306 显存按 8 行一页、先横向后纵向排列，每字节的 bit0~bit7
    对应同一列上的 8 个像素，因此需要按这个顺序重新打包成 1024 字节。
    """
    pixels = image.convert("1").load()
    frame = bytearray(OLED_FRAME_BYTES)
    offset = 0
    for page in range(OLED_HEIGHT // 8):
        for x in range(OLED_WIDTH):
            value = 0
            for bit in range(8):
                if pixels[x, page * 8 + bit]:
                    value |= 1 << bit
            frame[offset] = value
            offset += 1
    return bytes(frame)


def render_display_frames(text, role):
    """把一段文字渲染成若干帧 OLED 位图。

    role 显示在顶部（例如 "YOU" / "AI"），下面有一条分隔线，
    正文每 OLED_LINES_PER_PAGE 行分一屏，返回帧列表。
    """
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("OLED text rendering requires Pillow; install host_gateway/requirements.txt.")

    font = _oled_font(16)        # 正文 16px
    header_font = _oled_font(10)  # 标题 10px
    lines = _wrap_oled_text(text[:500], font)
    frames = []
    for start in range(0, len(lines), OLED_LINES_PER_PAGE):
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
        draw = ImageDraw.Draw(image)
        # 顶部角色标签 + 分隔线
        draw.text((2, 0), role, font=header_font, fill=1)
        draw.line((0, 11, OLED_WIDTH - 1, 11), fill=1)

        # 逐字符绘制，用 getbbox 修正基线，保证中英文对齐
        for row, line in enumerate(lines[start:start + OLED_LINES_PER_PAGE]):
            x = 0
            y = 13 + row * 16
            for char in line:
                bounds = font.getbbox(char)
                draw.text((round(x), y - bounds[1]), char, font=font, fill=1)
                x += font.getlength(char)
        frames.append(_image_to_ssd1306_frame(image))
    return frames


def _post_oled_frame(device_url, frame):
    """把一帧 1024 字节位图 POST 给 ESP32 的 /api/display 接口。

    设备返回非 ok 时抛 ValueError，由调用方记录日志。
    """
    request = urllib.request.Request(
        f"{device_url}/api/display",
        data=frame,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not result.get("ok"):
        raise ValueError(f"ESP32 rejected OLED frame: {result}")


def _oled_display_worker(display_queue):
    """OLED 显示后台线程：从队列取 (设备地址, 角色, 文本) 并逐屏播放。

    单独开线程是为了不拖慢 HTTP 请求；每屏之间有固定停顿，
    显示失败只打印日志，不影响聊天主流程。
    """
    while True:
        device_url, role, text = display_queue.get()
        try:
            for frame in render_display_frames(text, role):
                _post_oled_frame(device_url, frame)
                time.sleep(OLED_PAGE_DELAY)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[gateway] OLED display unavailable: {exc}")
        finally:
            display_queue.task_done()


def queue_oled_message(display_queue, device_url, role, text):
    """把一条待显示消息放入 OLED 队列（非阻塞）。

    队列满时丢弃最旧的一条再放入，保证最新的对话能显示；
    缺少设备地址或队列时直接忽略。
    """
    if display_queue is None or not device_url or not text:
        return
    item = (device_url, role, text[:500])
    try:
        display_queue.put_nowait(item)
    except queue.Full:
        # 丢最旧的，腾出位置
        try:
            display_queue.get_nowait()
            display_queue.task_done()
        except queue.Empty:
            pass
        try:
            display_queue.put_nowait(item)
        except queue.Full:
            pass


# 预设颜色到 RGB 分量的映射（yellow 用 96 的绿，观感更接近暖黄）
COLOR_RGB = {
    "off": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 96, 0),
    "white": (255, 255, 255),
}
# 各颜色在中文/英文里可能出现的说法，用于本地正则识别
COLOR_TERMS = {
    "red": ("红色", "红灯", "红光", "red"),
    "green": ("绿色", "绿灯", "绿光", "green"),
    "blue": ("蓝色", "蓝灯", "蓝光", "blue"),
    "yellow": ("黄色", "黄灯", "黄光", "yellow"),
    "white": ("白色", "白灯", "白光", "white"),
}
# 回复用户时使用的中文颜色名
COLOR_LABELS = {
    "off": "关闭",
    "red": "红色",
    "green": "绿色",
    "blue": "蓝色",
    "yellow": "黄色",
    "white": "白色",
}

# 要求模型输出严格符合的 JSON Schema（Ollama 的 format 参数会据此约束解码）
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["chat", "set_light", "unsupported"],
        },
        "colors": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["off", "red", "green", "blue", "yellow", "white"],
            },
            "maxItems": 5,
        },
        "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
        "reply": {"type": "string"},
    },
    "required": ["intent", "colors", "brightness", "reply"],
    "additionalProperties": False,
}

# 系统提示词：把模型限制成"聊天助手 + 灯光意图分类器"
SYSTEM_PROMPT = (
    "你是中文聊天助手和ESP32 RGB灯控制助手。"
    "只返回符合JSON Schema的JSON，不要输出JSON以外的文字。"
    "目前只能控制一颗RGB灯。用户明确要求控制灯时，intent=set_light，colors列出全部指定颜色，"
    "brightness填写0到100的亮度百分比；没有说亮度时为100。开灯但没指定颜色时使用white，关灯时使用off。"
    "颜色只允许off/red/green/blue/yellow/white。"
    "普通问候、聊天和知识问答使用intent=chat、colors=[]、brightness=100。"
    "用户要求控制空调、电机或其他未接入设备时，使用intent=unsupported、colors=[]、brightness=100。"
    "reply使用简短自然的中文。不要询问RGB数值。"
)

class OllamaError(Exception):
    """调用本地 Ollama 失败时抛出（连接不上、返回非法数据等）。"""
    pass


def validate_device_url(value):
    """校验并规范化 ESP32 设备地址。

    只接受 http 协议、无路径/查询串、端口为 80（或省略）的地址，
    且主机部分必须是局域网 IPv4（或 .local 主机名）。
    返回去掉结尾斜杠的规范地址；空输入返回空字符串。
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme != "http" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("请输入 http://192.168.x.x 形式的 ESP32 地址。")
    if parsed.username or parsed.password:
        raise ValueError("设备地址不能包含用户名或密码。")
    if parsed.port not in (None, 80):
        raise ValueError("此起步工程只支持 ESP32 的 HTTP 80 端口。")

    hostname = parsed.hostname.lower()
    if hostname.endswith(".local"):
        # mDNS 名称允许直接放行
        return f"http://{hostname}"
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError("请使用 ESP32 的局域网 IPv4 地址，例如 http://192.168.1.50。") from exc
    if address.version != 4 or not address.is_private:
        raise ValueError("ESP32 地址必须是局域网 IPv4 地址。")
    return f"http://{address}"


def is_negated_light_request(text):
    """判断是否是"否定式"的灯光指令（如"不要把灯调成蓝色"）。

    同时出现控制动词和否定词才算，用于在进入模型前直接短路，
    避免把否定句误判成控制命令。
    """
    normalized = text.strip().lower()
    has_control_verb = any(
        word in normalized
        for word in (
            "把灯",
            "开灯",
            "关灯",
            "关闭",
            "打开",
            "熄灭",
            "调成",
            "改成",
            "换成",
            "设为",
            "点亮",
            "亮度",
        )
    )
    has_negation = any(
        word in normalized
        for word in ("不要", "别把", "别开", "别关", "不需要开", "不想开", "don't", "do not")
    )
    return has_control_verb and has_negation


def _brightness_from_text(normalized):
    """从文本中抽取亮度百分比，抽不到返回 None。

    支持"百分之50""50%""亮度调到50""一半""最亮""最暗"等说法，
    多个数值时取最后一个；超出 0~100 直接抛 ValueError。
    """
    matches = re.findall(r"百分之\s*(-?\d{1,3})|(?<!\d)(-?\d{1,3})\s*(?:%|％)", normalized)
    values = [int(first or second) for first, second in matches]
    if not values:
        match = re.search(
            r"亮度\s*(?:(?:调到|调整到|设置为|设为|改为|到|为)\s*)?(-?\d{1,3})(?!\d)",
            normalized,
        )
        if match:
            values.append(int(match.group(1)))
    if values:
        brightness = values[-1]
    elif any(word in normalized for word in ("一半", "半亮", "一半亮")):
        brightness = 50
    elif any(word in normalized for word in ("最亮", "满亮", "全亮")):
        brightness = 100
    elif any(word in normalized for word in ("最暗", "最低亮度")):
        brightness = 0
    else:
        return None

    if not 0 <= brightness <= 100:
        raise ValueError("亮度范围是 0% 到 100%，请重新输入。")
    return brightness


def explicit_light_settings_from_text(text):
    """用本地规则解析明确的灯光指令，识别不出则返回 None。

    返回 {"colors": [...], "brightness": int}。这一步优先于模型：
    正则能确定的指令直接执行，既快又不会出现模型幻觉。
    注意刻意排除疑问句（"蓝色是什么"）和否定句，让它们继续走模型/聊天分支。
    """
    normalized = " ".join(text.strip().lower().split())
    # 问句式表达不是控制命令，交给模型回答
    if any(word in normalized for word in ("是什么", "什么意思", "为什么", "原理", "解释", "介绍一下", "教程")):
        return None
    if is_negated_light_request(normalized):
        return None

    # 可能表示"要操作灯"的动词/短语
    action_terms = (
        "把灯",
        "让灯",
        "给灯",
        "调成",
        "调为",
        "改成",
        "换成",
        "设为",
        "设置",
        "变成",
        "打开",
        "开灯",
        "点亮",
        "turn on",
        "turn off",
        "关灯",
        "关闭",
        "熄灭",
        "亮度",
        "亮一点",
        "暗一点",
        "调亮",
        "调暗",
        "半亮",
        "一半",
    )
    off_terms = ("关灯", "关掉灯", "熄灭", "关闭灯", "灭灯", "turn off")
    brightness = _brightness_from_text(normalized)
    # 命中的颜色按 COLOR_TERMS 的键顺序返回（红/绿/蓝/黄/白）
    colors = [
        color
        for color, terms in COLOR_TERMS.items()
        if any(term in normalized for term in terms)
    ]
    has_action = any(word in normalized for word in action_terms)
    # "绿色和红色"这类多色混光
    combined_colors = len(colors) > 1 and any(word in normalized for word in ("和", "与", "、", "+", "and"))
    # "亮红灯"这类以"亮+颜色"开头的说法
    bright_color_phrase = bool(colors) and any(
        normalized.startswith(prefix) for prefix in ("亮红", "亮绿", "亮蓝", "亮黄", "亮白", "点亮")
    )
    # 整句只有一个颜色词，如"红灯"
    exact_color_command = any(
        normalized == term for terms in COLOR_TERMS.values() for term in terms
    )

    # 关灯优先处理
    if any(word in normalized for word in off_terms) and (has_action or exact_color_command):
        return {"colors": ["off"], "brightness": 100}
    # 以上信号都没有，视为不是灯光指令
    if not (
        has_action
        or combined_colors
        or bright_color_phrase
        or exact_color_command
        or (colors and brightness is not None)
    ):
        return None
    if not colors and brightness is None:
        # "开灯"默认白光；"调亮"之类没有具体颜色时留空，由上层沿用当前颜色
        if any(word in normalized for word in ("开灯", "打开灯", "亮灯", "turn on")):
            colors = ["white"]
        else:
            return None
    return {"colors": colors, "brightness": 100 if brightness is None else brightness}


def compose_light_rgb(colors, brightness, current_rgb=None, remembered_rgb=None):
    """把颜色列表和亮度换算成最终要下发的 RGB。

    做三件事：
      1. 多颜色时把各分量相加并截断到 255（红+绿=黄）；
      2. 没有指定颜色（只调亮度）时，沿用设备当前颜色，设备已是黑的
         则用最近一次点亮的颜色记忆恢复；
      3. 按亮度百分比缩放，+50 是为了四舍五入更均匀。

    返回 (输出 RGB, 未缩放的基色) —— 基色用于更新"记忆颜色"，
    这样关灯后再"调亮度"仍能恢复原来的色。
    """
    if "off" in colors:
        # 关灯：输出黑色，记忆色保持不变
        return (0, 0, 0), remembered_rgb

    if colors:
        base = tuple(
            min(255, sum(COLOR_RGB[color][channel] for color in colors))
            for channel in range(3)
        )
    else:
        # 无颜色：优先用设备当前颜色，其次用记忆色，最后兜底白光
        source = current_rgb if current_rgb and max(current_rgb) else remembered_rgb
        if source and max(source):
            peak = max(source)
            base = tuple(round(value * 255 / peak) for value in source)
        else:
            base = COLOR_RGB["white"]

    output = tuple((value * brightness + 50) // 100 for value in base)
    return output, base


def parse_model_decision(content):
    """解析并严格校验模型返回的 JSON 决策。

    任何不合法的地方（JSON 解析失败、intent 不认识、颜色不在白名单、
    off 与其它颜色混用、亮度不是 0~100 的整数）都抛 ValueError，
    从而保证不会把脏指令发给硬件。返回统一的 dict。
    """
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("本地模型没有返回有效的 JSON 指令。") from exc
    if not isinstance(data, dict):
        raise ValueError("本地模型返回的数据格式不正确。")

    intent = data.get("intent")
    colors = data.get("colors")
    brightness = data.get("brightness")
    reply = data.get("reply")
    if intent not in ("chat", "set_light", "unsupported"):
        raise ValueError("本地模型返回了不支持的操作类型。")
    if not isinstance(reply, str):
        reply = ""
    # 去掉推理模型可能带上的 <think> 段，并限制长度
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()[:500]

    if intent == "set_light":
        # 颜色必须是非空列表、最多 5 个、全部在白名单内，且 off 不能与其它颜色共存
        if (
            not isinstance(colors, list)
            or not colors
            or len(colors) > 5
            or any(not isinstance(color, str) or color not in COLOR_RGB for color in colors)
            or ("off" in colors and len(colors) != 1)
        ):
            raise ValueError("本地模型提出了无效颜色，未执行硬件操作。")
        # 亮度必须是非 bool 的整数且落在 0~100
        if isinstance(brightness, bool) or not isinstance(brightness, int) or not 0 <= brightness <= 100:
            raise ValueError("本地模型提出了无效亮度，未执行硬件操作。")
        # 去重但保持顺序
        colors = list(dict.fromkeys(colors))
    else:
        # 聊天/不支持的动作强制清空硬件参数
        colors = []
        brightness = 100
    return {"intent": intent, "colors": colors, "brightness": brightness, "reply": reply}


def call_ollama(messages, ollama_url, model):
    """调用本地 Ollama 的 /api/chat，返回校验后的决策 dict。

    参数固定为不流式、温度 0、限制上下文和输出长度，并传入 JSON Schema
    约束输出格式；任何异常统一包装成 OllamaError 抛出。
    """
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": DECISION_SCHEMA,
        "options": {
            "temperature": 0,
            "num_ctx": 2048,
            "num_predict": 128,
        },
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
    }
    request = urllib.request.Request(
        ollama_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise OllamaError(f"Ollama 返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OllamaError(
            "无法连接本机 Ollama。请确认 Ollama 正在运行，并且模型已安装。"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OllamaError("Ollama 返回的数据不是有效JSON。") from exc

    message = result.get("message")
    if not isinstance(message, dict):
        raise OllamaError("Ollama 响应中缺少 message。")
    try:
        return parse_model_decision(message.get("content"))
    except ValueError as exc:
        raise OllamaError(str(exc)) from exc


def session_id_is_valid(value):
    """会话 ID 必须是不超过 80 位的字母/数字/下划线/连字符，防止被注入脏数据。"""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is not None


def configure_https(server, certfile=None, keyfile=None):
    """Optionally wrap the gateway socket in TLS for browser microphone access."""
    if bool(certfile) != bool(keyfile):
        raise ValueError("HTTPS 需要同时提供证书文件和私钥文件。")
    if not certfile:
        return "http"

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return "https"


class GatewayHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：对外暴露网页和 /api/* 接口。

    每个请求由 ThreadingHTTPServer 分配一个线程处理，
    共享状态（设备地址、会话历史、记忆颜色、OLED 队列）挂在 server 对象上。
    """

    server_version = "ESP32Bridge/1.0"

    def _send_json(self, status, payload):
        """统一发送 JSON 响应。

        额外做一件事：如果是 /api/chat 且带 reply，就把助手的回复
        投递到 OLED 队列，让设备屏幕也显示出来。
        """
        if (
            self.path == "/api/chat"
            and isinstance(payload, dict)
            and isinstance(payload.get("reply"), str)
        ):
            queue_oled_message(
                getattr(self.server, "display_queue", None),
                self.server.device_url,
                "AI",
                payload["reply"],
            )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        """读取并解析请求体 JSON，长度非法或格式错误时抛 ValueError。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效。") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或超过大小限制。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效的 UTF-8 JSON。") from exc

    def do_GET(self):
        """处理 GET 请求：首页、配置查询、设备状态、健康检查。"""
        if self.path == "/":
            # 返回前端单页
            page = (WEB_ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if self.path == "/api/config":
            # 供前端回填设备地址和模型名
            self._send_json(
                200,
                {
                    "device_url": self.server.device_url,
                    "model": self.server.ollama_model,
                },
            )
            return

        if self.path == "/api/device":
            # 代理查询 ESP32 的 /api/status，供页面展示
            if not self.server.device_url:
                self._send_json(503, {"ok": False, "error": "请先填写 ESP32 的局域网地址。"})
                return
            try:
                self._send_json(200, self._get_device_status())
            except (OSError, ValueError, TimeoutError) as exc:
                self._send_json(502, {"ok": False, "error": str(exc)})
            return

        if self.path == "/health":
            # 简单的进程存活探针
            self._send_json(
                200,
                {
                    "ok": True,
                    "device_url": self.server.device_url,
                    "model": self.server.ollama_model,
                },
            )
            return

        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        """处理 POST 请求：设置设备地址、核心的 /api/chat 对话与控灯。"""
        if self.path == "/api/device/config":
            # 保存/校验前端填写的 ESP32 地址
            try:
                payload = self._read_json()
                if not isinstance(payload, dict) or not isinstance(payload.get("device_url"), str):
                    raise ValueError("请求需要包含 device_url 字符串。")
                self.server.device_url = validate_device_url(payload["device_url"])
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, "device_url": self.server.device_url})
            return

        if self.path != "/api/chat":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return

        # ---- 以下是 /api/chat 的处理流程 ----
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            self._send_json(400, {"ok": False, "error": "请求需要包含 text 字符串。"})
            return

        text = payload["text"].strip()[:500]
        if not text:
            self._send_json(400, {"ok": False, "error": "请输入一句话。"})
            return
        # 会话 ID 非法就新开一个，保证多轮上下文可用
        session_id = payload.get("session_id")
        if not session_id_is_valid(session_id):
            session_id = uuid.uuid4().hex
        # 用户的输入同步推到 OLED 显示
        queue_oled_message(
            getattr(self.server, "display_queue", None),
            self.server.device_url,
            "YOU",
            text,
        )

        # 否定句直接短路，避免误操作灯光
        if is_negated_light_request(text):
            reply = "好的，我不会更改灯光。"
            self._remember_turn(session_id, text, reply)
            self._send_json(200, {"ok": True, "reply": reply, "action": None, "session_id": session_id})
            return

        # 先尝试本地规则解析
        try:
            explicit_settings = explicit_light_settings_from_text(text)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
            return

        # 本地规则命中就直接执行；否则交给模型判断意图
        try:
            decision = (
                {
                    "intent": "set_light",
                    **explicit_settings,
                    "reply": "",
                }
                if explicit_settings is not None
                else self._get_model_decision(session_id, text)
            )
        except OllamaError as exc:
            self._send_json(
                502,
                {
                    "ok": False,
                    "reply": str(exc),
                    "error": str(exc),
                    "session_id": session_id,
                },
            )
            return

        if decision["intent"] == "set_light":
            # 没连设备就不能控灯
            if not self.server.device_url:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "reply": "先连接 ESP32，再执行灯光控制。",
                        "action": None,
                        "session_id": session_id,
                    },
                )
                return

            colors = decision["colors"]
            brightness = decision["brightness"]
            current_rgb = None
            if not colors:
                # 只调亮度时需要先读设备当前颜色
                try:
                    status = self._get_device_status()
                except (OSError, ValueError, TimeoutError) as exc:
                    self._send_json(502, {"ok": False, "reply": str(exc), "error": str(exc), "session_id": session_id})
                    return
                rgb = status.get("rgb")
                if isinstance(rgb, dict):
                    values = (rgb.get("r"), rgb.get("g"), rgb.get("b"))
                    if all(
                        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255
                        for value in values
                    ):
                        current_rgb = values

            # 换算成最终 RGB，并保留基色用于记忆
            rgb_values, base_rgb = compose_light_rgb(
                colors,
                brightness,
                current_rgb=current_rgb,
                remembered_rgb=getattr(self.server, "last_lit_rgb", None),
            )
            action = {
                "request_id": uuid.uuid4().hex,
                "action": "set_rgb",
                "r": rgb_values[0],
                "g": rgb_values[1],
                "b": rgb_values[2],
            }
            # 下发到 ESP32
            try:
                device_result = self._post_device_action(action)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "reply": "ESP32 没有确认执行灯光设置。",
                        "action": action,
                        "error": str(exc),
                        "session_id": session_id,
                    },
                )
                return

            # 更新"最近一次点亮颜色"记忆（关灯时 base 为 None 不会覆盖）
            if any(base_rgb):
                self.server.last_lit_rgb = base_rgb
            if colors == ["off"]:
                reply = "已关灯。"
            elif colors:
                labels = "和".join(COLOR_LABELS[color] for color in colors)
                reply = f"已调成{labels}混色，亮度 {brightness}%。"
            else:
                reply = f"已将当前灯色亮度调到 {brightness}%。"
            self._remember_turn(session_id, text, reply)
            self._send_json(
                200,
                {
                    "ok": True,
                    "reply": reply,
                    "action": action,
                    "device": device_result,
                    "session_id": session_id,
                },
            )
            return

        # 非控灯分支：不支持的动作给固定话术，普通聊天用模型回复
        if decision["intent"] == "unsupported":
            reply = "目前只接入了 RGB 灯控制，暂时不能操作其他设备。"
        else:
            reply = decision["reply"] or "你好，有什么我可以帮你？"

        self._remember_turn(session_id, text, reply)
        self._send_json(
            200,
            {"ok": True, "reply": reply, "action": None, "session_id": session_id},
        )

    def _get_model_decision(self, session_id, text):
        """把该会话的历史消息 + 当前输入一起发给模型，返回决策 dict。"""
        with self.server.sessions_lock:
            history = list(self.server.sessions.get(session_id, []))
        messages = [*history, {"role": "user", "content": text}]
        return call_ollama(messages, self.server.ollama_url, self.server.ollama_model)

    def _remember_turn(self, session_id, user_text, assistant_text):
        """记录一轮对话到会话历史，并做两件清理：

        每个会话只保留最近 MAX_SESSION_MESSAGES 条；会话总数超过 MAX_SESSIONS
        时丢弃最早创建的会话，防止内存无限增长。
        """
        with self.server.sessions_lock:
            history = self.server.sessions.setdefault(session_id, [])
            history.extend(
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ]
            )
            self.server.sessions[session_id] = history[-MAX_SESSION_MESSAGES:]
            while len(self.server.sessions) > MAX_SESSIONS:
                oldest = next(iter(self.server.sessions))
                self.server.sessions.pop(oldest, None)

    def _post_device_action(self, action):
        """向 ESP32 的 /api/action 下发动作，返回设备响应 dict。

        连接失败、HTTP 错误码或设备未确认（ok 不为真）都会抛异常。
        """
        url = f"{self.server.device_url}/api/action"
        body = json.dumps(action).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ValueError(f"ESP32 返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise urllib.error.URLError(
                f"无法连接 ESP32：{exc.reason}。请确认同一 Wi-Fi 且设备 IP 正确。"
            ) from exc

        if not isinstance(result, dict) or not result.get("ok"):
            raise ValueError(f"ESP32 未确认执行：{result}")
        return result

    def _get_device_status(self):
        """读取 ESP32 的 /api/status，返回其响应 dict。"""
        if not self.server.device_url:
            raise ValueError("请先填写 ESP32 的局域网地址。")
        request = urllib.request.Request(f"{self.server.device_url}/api/status")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ValueError(f"ESP32 返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise urllib.error.URLError(f"无法连接 ESP32：{exc.reason}") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise ValueError(f"ESP32 状态响应无效：{result}")
        return result

    def log_message(self, fmt, *args):
        """覆盖默认日志格式，统一加上 [gateway] 前缀。"""
        print(f"[gateway] {self.address_string()} - {fmt % args}")


def main():
    """命令行入口：解析参数、初始化共享状态、启动 HTTP(S) 服务。"""
    parser = argparse.ArgumentParser(description="Laptop gateway for ESP32 and local Ollama")
    parser.add_argument("--device-url", default="", help="ESP32 URL, e.g. http://192.168.1.50")
    parser.add_argument("--host", default="127.0.0.1", help="Web bind address")
    parser.add_argument("--port", type=int, default=8080, help="Web port")
    parser.add_argument("--tls-certfile", help="PEM certificate for HTTPS")
    parser.add_argument("--tls-keyfile", help="PEM private key for HTTPS")
    parser.add_argument("--ollama-url", default=OLLAMA_URL, help="Local Ollama chat endpoint")
    parser.add_argument("--model", default=OLLAMA_MODEL, help="Installed local Ollama model")
    args = parser.parse_args()

    # 启动参数里的设备地址同样要走校验
    try:
        device_url = validate_device_url(args.device_url)
    except ValueError as exc:
        parser.error(str(exc))

    # 出于安全考虑，Ollama 只允许本机地址
    ollama_parts = urlsplit(args.ollama_url)
    if ollama_parts.scheme != "http" or ollama_parts.hostname not in ("127.0.0.1", "localhost"):
        parser.error("--ollama-url must point to a local HTTP Ollama server")
    if not args.model.strip():
        parser.error("--model cannot be empty")

    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    try:
        scheme = configure_https(server, args.tls_certfile, args.tls_keyfile)
    except (OSError, ssl.SSLError, ValueError) as exc:
        server.server_close()
        parser.error(f"HTTPS 配置失败：{exc}")

    # 共享状态挂在 server 实例上，供各请求线程访问
    server.device_url = device_url
    server.ollama_url = args.ollama_url
    server.ollama_model = args.model.strip()
    server.sessions = {}
    server.sessions_lock = threading.Lock()
    server.last_lit_rgb = None  # 最近一次点亮的基色，用于关灯后恢复
    server.display_queue = queue.Queue(maxsize=8)
    # OLED 播放线程设为 daemon，随主进程退出而结束
    threading.Thread(
        target=_oled_display_worker,
        args=(server.display_queue,),
        name="oled-display",
        daemon=True,
    ).start()
    display_host = args.host if args.host not in ("0.0.0.0", "::") else "127.0.0.1"
    print(f"网页地址：{scheme}://{display_host}:{args.port}/")
    print(f"ESP32 地址：{device_url or '尚未设置，请在网页中填写'}")
    print(f"本地模型：{server.ollama_model} ({server.ollama_url})")
    print("请保持 Ollama 和本机模型运行。按 Ctrl+C 停止网关。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止网关。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

```

## host_gateway/web/index.html

```html
<!doctype html>
<!--
  ESP32 AI 控制台 —— 前端单页
  由 gateway.py 在 GET / 时原样返回。

  页面三块功能：
    1. 聊天区：输入中文指令，POST /api/chat，交给网关 + 本地模型处理；
    2. 示例按钮：一键填入常用灯光指令；
    3. 设备区：填写 ESP32 局域网地址（POST /api/device/config），
       并展示从 /api/device 拿到的设备状态。
-->
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ESP32 控制台</title>
  <style>
    /* ---------- 主题变量与全局基础样式 ---------- */
    :root {
      color-scheme: light;
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
      color: #202522;
      background: #f4f6f3;
    }
    * { box-sizing: border-box; }
    body { margin: 0; }
    main { max-width: 760px; margin: 0 auto; padding: 28px 20px 48px; }

    /* ---------- 顶部标题与连接状态 ---------- */
    header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { margin: 0; font-size: 24px; font-weight: 650; }
    #connection { font-size: 13px; color: #59635d; }

    /* ---------- 示例指令按钮 ---------- */
    .examples { display: flex; flex-wrap: wrap; gap: 8px; margin: 24px 0 16px; }
    button, input { font: inherit; }
    .example, #refresh {
      border: 1px solid #cbd4cd;
      border-radius: 6px;
      background: #fff;
      color: #25352b;
      padding: 8px 12px;
      cursor: pointer;
    }
    .example:hover, #refresh:hover { background: #edf3ee; }

    /* ---------- 聊天消息列表 ---------- */
    #messages {
      min-height: 280px;
      max-height: 52vh;
      overflow-y: auto;
      border-top: 1px solid #d9dfda;
      border-bottom: 1px solid #d9dfda;
      padding: 14px 0;
    }
    .message { margin: 0 0 14px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
    .message .label { display: block; margin-bottom: 3px; color: #68736b; font-size: 12px; }
    .message.error { color: #a52c27; } /* 报错消息用红色标注 */

    /* ---------- 输入与发送 ---------- */
    form { display: flex; gap: 8px; margin-top: 14px; }
    .device-config { display: flex; gap: 8px; margin-top: 20px; }
    .device-config input { flex: 1; }
    .device-config button {
      border: 1px solid #9ca9a0;
      border-radius: 6px;
      padding: 0 12px;
      color: #25352b;
      background: white;
      cursor: pointer;
    }
    #config-result { min-height: 20px; margin: 6px 0 0; color: #59635d; font-size: 12px; }
    input {
      min-width: 0;
      flex: 1;
      border: 1px solid #aeb9b1;
      border-radius: 6px;
      padding: 11px 12px;
      background: white;
    }
    input:focus { outline: 2px solid #39835b; outline-offset: 1px; }
    #send {
      border: 0;
      border-radius: 6px;
      padding: 0 18px;
      color: white;
      background: #26764a;
      cursor: pointer;
    }
    #send:disabled { opacity: .55; cursor: wait; } /* 请求进行中禁用按钮 */
    #voice-input {
      border: 1px solid #9ca9a0;
      border-radius: 6px;
      padding: 0 12px;
      color: #25352b;
      background: white;
      cursor: pointer;
    }
    #voice-input[aria-pressed="true"] {
      border-color: #b43b31;
      color: #8f241d;
      background: #fff0ee;
    }
    #voice-input:disabled { opacity: .55; cursor: not-allowed; }
    .voice-options {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 32px;
      margin-top: 6px;
      color: #59635d;
      font-size: 12px;
    }
    .speech-toggle { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
    .speech-toggle input { flex: none; width: 16px; height: 16px; margin: 0; accent-color: #26764a; }
    #voice-status { text-align: right; }

    /* ---------- 设备状态展示 ---------- */
    .device-state { margin-top: 20px; }
    .device-state h2 { margin: 0 0 8px; font-size: 15px; font-weight: 600; }
    pre {
      margin: 0;
      padding: 12px;
      min-height: 52px;
      overflow: auto;
      border-radius: 6px;
      background: #e9ede9;
      font-size: 12px;
    }

    /* ---------- 窄屏适配 ---------- */
    @media (max-width: 520px) {
      main { padding: 20px 14px 32px; }
      header { align-items: flex-start; flex-direction: column; }
      #messages { min-height: 230px; }
    }
  </style>
</head>
<body>
  <main>
    <!-- 标题栏：右侧显示设备连接状态 -->
    <header>
      <h1>ESP32 控制台</h1>
      <span id="connection">正在检查设备…</span>
    </header>

    <!-- 常用指令快捷按钮，点击后自动填入并发送 -->
    <div class="examples" aria-label="示例指令">
      <button class="example" type="button" data-text="把灯调成蓝色">蓝灯</button>
      <button class="example" type="button" data-text="打开红灯">红灯</button>
      <button class="example" type="button" data-text="把灯调成绿色">绿灯</button>
      <button class="example" type="button" data-text="亮绿色和红色，亮度 30%">红绿混色 30%</button>
      <button class="example" type="button" data-text="把灯亮度调到一半">当前颜色 50%</button>
      <button class="example" type="button" data-text="关灯">关灯</button>
    </div>

    <!-- 对话消息区：aria-live 便于读屏软件播报新消息 -->
    <section id="messages" aria-live="polite">
      <p class="message"><span class="label">系统</span>本地模型已接入。可以正常聊天，也可以控制 RGB 灯的颜色和亮度。</p>
    </section>

    <!-- 聊天输入表单 -->
    <form id="chat-form">
      <input id="command" maxlength="500" autocomplete="off" placeholder="试试：红色和绿色，亮度 30%" required>
      <button id="voice-input" type="button" aria-pressed="false" title="开始语音识别">语音输入</button>
      <button id="send" type="submit">发送</button>
    </form>
    <div class="voice-options">
      <label class="speech-toggle"><input id="speak-replies" type="checkbox" checked>朗读回复</label>
      <span id="voice-status" role="status" aria-live="polite"></span>
    </div>

    <!-- 设备状态原始 JSON 展示 -->
    <section class="device-state">
      <h2>ESP32 状态</h2>
      <pre id="state">尚未读取</pre>
    </section>

    <!-- ESP32 局域网地址配置 -->
    <form id="device-form" class="device-config">
      <input id="device-url" inputmode="url" placeholder="http://192.168.1.50" aria-label="ESP32 局域网地址">
      <button type="submit">连接设备</button>
    </form>
    <p id="config-result" aria-live="polite"></p>
  </main>

  <script>
    // ---------- DOM 引用 ----------
    const messages = document.querySelector("#messages");
    const form = document.querySelector("#chat-form");
    const commandInput = document.querySelector("#command");
    const sendButton = document.querySelector("#send");
    const voiceInputButton = document.querySelector("#voice-input");
    const speakReplies = document.querySelector("#speak-replies");
    const voiceStatus = document.querySelector("#voice-status");
    const connection = document.querySelector("#connection");
    const state = document.querySelector("#state");
    const deviceForm = document.querySelector("#device-form");
    const deviceUrlInput = document.querySelector("#device-url");
    const configResult = document.querySelector("#config-result");
    const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition;
    let recognition = null;
    let isListening = false;
    let finalTranscript = "";
    let recognitionError = "";

    // ---------- 会话 ID：存本地，用于服务端串联多轮上下文 ----------
    const sessionKey = "esp32-bridge-session";
    let sessionId = localStorage.getItem(sessionKey);
    if (!sessionId) {
      sessionId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
      localStorage.setItem(sessionKey, sessionId);
    }

    /** 往消息区追加一条消息并滚动到底部 */
    function addMessage(label, text, isError = false) {
      const item = document.createElement("p");
      item.className = `message${isError ? " error" : ""}`; // 出错时套用红色样式
      const heading = document.createElement("span");
      heading.className = "label";
      heading.textContent = label;
      const body = document.createElement("span");
      body.textContent = text; // 用 textContent 而非 innerHTML，避免 XSS
      item.append(heading, body);
      messages.append(item);
      messages.scrollTop = messages.scrollHeight;
    }

    function setVoiceStatus(text) {
      voiceStatus.textContent = text;
    }

    function stopSpeaking() {
      if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    }

    function speakReply(text) {
      if (!speakReplies.checked || !("speechSynthesis" in window) || !text) return;
      stopSpeaking();
      const utterance = new SpeechSynthesisUtterance(text);
      const voices = window.speechSynthesis.getVoices();
      const chineseVoice = voices.find((voice) => /^zh[-_]cn$/i.test(voice.lang))
        || voices.find((voice) => voice.lang.toLowerCase().startsWith("zh"));
      utterance.lang = chineseVoice ? chineseVoice.lang : "zh-CN";
      if (chineseVoice) utterance.voice = chineseVoice;
      utterance.onerror = (event) => {
        if (event.error !== "canceled" && event.error !== "interrupted") {
          setVoiceStatus("手机语音播报失败，请检查系统语音设置");
        }
      };
      window.speechSynthesis.speak(utterance);
    }

    if (!window.isSecureContext) {
      voiceInputButton.disabled = true;
      setVoiceStatus("语音输入需要 HTTPS；当前安全连接未启用");
    } else if (!SpeechRecognitionAPI) {
      voiceInputButton.disabled = true;
      setVoiceStatus("此浏览器不支持语音识别，请使用 Android Chrome");
    } else {
      setVoiceStatus("语音识别可用");
    }

    if (!("speechSynthesis" in window) || !("SpeechSynthesisUtterance" in window)) {
      speakReplies.disabled = true;
      setVoiceStatus("此浏览器不支持语音播报");
    }

    voiceInputButton.addEventListener("click", () => {
      if (isListening) {
        voiceInputButton.textContent = "正在结束…";
        voiceInputButton.disabled = true;
        recognition.stop();
        return;
      }
      if (!SpeechRecognitionAPI || !window.isSecureContext) return;

      stopSpeaking();
      finalTranscript = "";
      recognitionError = "";
      recognition = new SpeechRecognitionAPI();
      recognition.lang = "zh-CN";
      recognition.interimResults = true;
      recognition.continuous = false;
      recognition.maxAlternatives = 1;
      recognition.onstart = () => {
        isListening = true;
        voiceInputButton.disabled = false;
        voiceInputButton.textContent = "停止聆听";
        voiceInputButton.setAttribute("aria-pressed", "true");
        setVoiceStatus("正在聆听…");
      };
      recognition.onresult = (event) => {
        const finalParts = [];
        const interimParts = [];
        for (let index = 0; index < event.results.length; index += 1) {
          const result = event.results[index];
          (result.isFinal ? finalParts : interimParts).push(result[0].transcript);
        }
        finalTranscript = finalParts.join(" ").trim();
        commandInput.value = [finalTranscript, interimParts.join(" ").trim()]
          .filter(Boolean).join(" ");
        setVoiceStatus(finalTranscript ? "已识别，请检查文字后发送" : "正在识别…");
      };
      recognition.onerror = (event) => {
        const messages = {
          "not-allowed": "麦克风权限被拒绝，请在浏览器设置中允许使用麦克风",
          "service-not-allowed": "浏览器语音识别服务不可用",
          "no-speech": "没有检测到语音，请再试一次",
          "audio-capture": "没有找到可用的麦克风",
          network: "语音识别服务连接失败，请检查手机网络"
        };
        recognitionError = messages[event.error] || `语音识别失败：${event.error}`;
        setVoiceStatus(recognitionError);
      };
      recognition.onend = () => {
        isListening = false;
        voiceInputButton.disabled = false;
        voiceInputButton.textContent = "语音输入";
        voiceInputButton.setAttribute("aria-pressed", "false");
        if (finalTranscript) {
          commandInput.value = finalTranscript;
          setVoiceStatus("已识别，请检查文字后发送");
        } else if (!recognitionError) {
          setVoiceStatus("没有识别到文字，请再试一次");
        }
      };
      try {
        recognition.start();
      } catch (error) {
        setVoiceStatus(`无法启动语音识别：${error.message}`);
      }
    });

    speakReplies.addEventListener("change", () => {
      if (!speakReplies.checked) stopSpeaking();
    });

    /** 查询 ESP32 状态，刷新顶部连接提示与状态区 */
    async function refreshDevice() {
      try {
        const response = await fetch("/api/device");
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "设备未响应");
        connection.textContent = "ESP32 已连接";
        state.textContent = JSON.stringify(data, null, 2);
      } catch (error) {
        connection.textContent = "ESP32 未连接";
        state.textContent = error.message;
      }
    }

    // ---------- 保存 ESP32 地址 ----------
    deviceForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      configResult.textContent = "正在保存并检查设备…";
      try {
        const response = await fetch("/api/device/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ device_url: deviceUrlInput.value.trim() })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        configResult.textContent = data.device_url
          ? `已设置：${data.device_url}`
          : "已清除设备地址。";
        await refreshDevice(); // 保存成功后立刻探测一次
      } catch (error) {
        configResult.textContent = error.message;
      }
    });

    // ---------- 发送聊天/控制指令 ----------
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = commandInput.value.trim();
      if (!text) return;

      addMessage("你", text);
      commandInput.value = "";
      sendButton.disabled = true; // 防止重复提交
      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, session_id: sessionId })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          addMessage("助手", data.reply || data.error || "请求失败", true);
        } else {
          addMessage("助手", data.reply);
          speakReply(data.reply);
          // 如果这次请求动了灯，顺便把返回的设备状态显示出来
          if (data.device) {
            state.textContent = JSON.stringify(data.device.state || data.device, null, 2);
          }
        }
      } catch (error) {
        addMessage("助手", error.message, true);
      } finally {
        sendButton.disabled = false;
        commandInput.focus();
        refreshDevice();
      }
    });

    // ---------- 示例按钮：填入文本后直接提交 ----------
    document.querySelectorAll(".example").forEach((button) => {
      button.addEventListener("click", () => {
        commandInput.value = button.dataset.text;
        form.requestSubmit();
      });
    });

    // ---------- 页面加载：回填已保存的设备地址并做一次探测 ----------
    fetch("/api/config")
      .then((response) => response.json())
      .then((data) => {
        deviceUrlInput.value = data.device_url || "";
        refreshDevice();
      })
      .catch(() => {
        connection.textContent = "网关未连接";
      });
  </script>
</body>
</html>

```

## host_gateway/tests/test_gateway.py

```python
"""gateway.py 的单元测试。

覆盖四类逻辑：
  1. 模型输出解析与非法指令拒绝（ModelDecisionTests）；
  2. 中文灯光指令的本地解析、RGB 混色与亮度换算（LightCommandTests）；
  3. OLED 文字渲染与帧格式（OledRenderingTests）；
  4. 设备地址合法性校验（DeviceUrlTests）。

运行方式（在 host_gateway 目录下）：
    python -m unittest discover -s tests
"""

import importlib.util
import queue
import unittest
from pathlib import Path


# 以文件路径方式导入 gateway.py（gateway 不是包，避免额外改动 sys.path）
MODULE_PATH = Path(__file__).resolve().parents[1] / "gateway.py"
SPEC = importlib.util.spec_from_file_location("gateway", MODULE_PATH)
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class ModelDecisionTests(unittest.TestCase):
    """测试 parse_model_decision：模型返回内容的校验与归一化。"""

    def test_set_light_decision(self):
        """合法的控灯指令应被完整解析出来。"""
        decision = gateway.parse_model_decision(
            '{"intent":"set_light","colors":["red","green"],"brightness":30,"reply":"好的。"}'
        )
        self.assertEqual(decision["intent"], "set_light")
        self.assertEqual(decision["colors"], ["red", "green"])
        self.assertEqual(decision["brightness"], 30)

    def test_non_action_fields_are_ignored(self):
        """聊天意图下，模型多给的颜色/亮度字段必须被清空，避免误控灯。"""
        decision = gateway.parse_model_decision(
            '{"intent":"chat","colors":["white"],"brightness":45,"reply":"你好。"}'
        )
        self.assertEqual(decision["intent"], "chat")
        self.assertEqual(decision["colors"], [])
        self.assertEqual(decision["brightness"], 100)

    def test_invalid_light_settings_are_rejected(self):
        """非法颜色、超范围亮度、off 与其它颜色混用都应抛 ValueError。"""
        invalid = (
            '{"intent":"set_light","colors":["purple"],"brightness":30,"reply":"好的。"}',
            '{"intent":"set_light","colors":["red"],"brightness":101,"reply":"好的。"}',
            '{"intent":"set_light","colors":["off","red"],"brightness":50,"reply":"好的。"}',
        )
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                gateway.parse_model_decision(content)


class LightCommandTests(unittest.TestCase):
    """测试本地中文指令解析（不经模型）与 RGB 合成。"""

    def test_explicit_color_and_mixed_color_commands(self):
        """单色、混色 + 亮度、单色改亮度、开灯默认白光等常见说法。"""
        self.assertEqual(
            gateway.explicit_light_settings_from_text("把灯调成蓝色"),
            {"colors": ["blue"], "brightness": 100},
        )
        self.assertEqual(
            gateway.explicit_light_settings_from_text("亮绿色和红色，亮度 30%"),
            {"colors": ["red", "green"], "brightness": 30},
        )
        self.assertEqual(
            gateway.explicit_light_settings_from_text("把红灯亮度调到一半"),
            {"colors": ["red"], "brightness": 50},
        )

        self.assertEqual(
            gateway.explicit_light_settings_from_text("开灯"),
            {"colors": ["white"], "brightness": 100},
        )

    def test_brightness_only_keeps_current_color(self):
        """只说亮度时 colors 为空，表示沿用设备当前颜色。"""
        self.assertEqual(
            gateway.explicit_light_settings_from_text("把灯亮度调到30%"),
            {"colors": [], "brightness": 30},
        )

    def test_off_negation_and_questions(self):
        """关灯可识别；否定句和疑问句必须不被当成控制指令。"""
        self.assertEqual(
            gateway.explicit_light_settings_from_text("关灯"),
            {"colors": ["off"], "brightness": 100},
        )
        self.assertIsNone(
            gateway.explicit_light_settings_from_text("不要把灯调成蓝色")
        )
        self.assertIsNone(gateway.explicit_light_settings_from_text("别关灯"))
        self.assertIsNone(
            gateway.explicit_light_settings_from_text("蓝色是什么")
        )
        self.assertTrue(gateway.is_negated_light_request("不要把灯调成蓝色"))
        self.assertFalse(gateway.is_negated_light_request("把灯调成蓝色"))

    def test_brightness_out_of_range_is_rejected(self):
        """亮度超过 100% 应直接报错。"""
        with self.assertRaises(ValueError):
            gateway.explicit_light_settings_from_text("红灯亮度调到120%")

    def test_rgb_mixing_and_brightness_scaling(self):
        """红+绿混成黄色；30% 亮度时各分量按比例缩小（四舍五入）。"""
        self.assertEqual(
            gateway.compose_light_rgb(["red", "green"], 100),
            ((255, 255, 0), (255, 255, 0)),
        )
        self.assertEqual(
            gateway.compose_light_rgb(["red", "green"], 30),
            ((77, 77, 0), (255, 255, 0)),
        )

    def test_brightness_only_preserves_current_or_remembered_color(self):
        """只调亮度时：优先按设备当前颜色归一化，其次用记忆颜色恢复。"""
        self.assertEqual(
            gateway.compose_light_rgb([], 50, current_rgb=(80, 0, 0)),
            ((128, 0, 0), (255, 0, 0)),
        )
        self.assertEqual(
            gateway.compose_light_rgb([], 40, current_rgb=(0, 0, 0), remembered_rgb=(0, 255, 0)),
            ((0, 102, 0), (0, 255, 0)),
        )


@unittest.skipIf(gateway.Image is None, "Pillow is not installed")
class OledRenderingTests(unittest.TestCase):
    """测试 OLED 渲染，未安装 Pillow 时整类跳过。"""

    def test_user_message_precedes_assistant_reply(self):
        """消息入队顺序必须是先用户后助手，保证屏幕上先显示提问。"""
        messages = queue.Queue(maxsize=4)
        gateway.queue_oled_message(messages, "http://192.168.1.50", "YOU", "user text")
        gateway.queue_oled_message(messages, "http://192.168.1.50", "AI", "assistant reply")

        self.assertEqual(messages.get_nowait()[1:], ("YOU", "user text"))
        self.assertEqual(messages.get_nowait()[1:], ("AI", "assistant reply"))

    def test_frame_uses_ssd1306_page_byte_order(self):
        """验证像素到 SSD1306 页面格式字节的映射顺序（低 bit 对应上方像素）。"""
        image = gateway.Image.new("1", (128, 64), 0)
        image.putpixel((3, 2), 1)
        image.putpixel((4, 15), 1)

        frame = gateway._image_to_ssd1306_frame(image)

        self.assertEqual(len(frame), 1024)
        self.assertEqual(frame[3], 1 << 2)        # 第 0 页，x=3，y=2
        self.assertEqual(frame[128 + 4], 1 << 7)  # 第 1 页，x=4，y=15

    def test_long_text_is_paginated_once(self):
        """长文本应被切成多屏，且每屏都是 1024 字节、至少有一屏有内容。"""
        frames = gateway.render_display_frames("中文内容" * 12, "YOU")

        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(frame) == 1024 for frame in frames))
        self.assertTrue(any(any(frame) for frame in frames))


class DeviceUrlTests(unittest.TestCase):
    """测试设备地址校验：只允许局域网地址。"""

    def test_private_device_url(self):
        """内网地址应被接受并去掉结尾斜杠。"""
        self.assertEqual(
            gateway.validate_device_url("http://192.168.1.50/"),
            "http://192.168.1.50",
        )

    def test_public_device_url_is_rejected(self):
        """公网地址应被拒绝，避免把控制接口暴露出去。"""
        with self.assertRaises(ValueError):
            gateway.validate_device_url("http://8.8.8.8")


class HttpsTests(unittest.TestCase):
    def test_https_is_optional(self):
        self.assertEqual(gateway.configure_https(object()), "http")

    def test_https_requires_both_certificate_and_key(self):
        with self.assertRaisesRegex(ValueError, "同时提供"):
            gateway.configure_https(object(), certfile="gateway.pem")

        with self.assertRaisesRegex(ValueError, "同时提供"):
            gateway.configure_https(object(), keyfile="gateway-key.pem")


if __name__ == "__main__":
    unittest.main()

```

## CMakeLists.txt

```cmake
# ESP-IDF 工程最顶层 CMakeLists.txt
# 以下固定写法必须保持这个顺序，cmake 才能正确加载 IDF 的构建脚本
cmake_minimum_required(VERSION 3.16)

# 引入 ESP-IDF 提供的 project.cmake（依赖环境变量 IDF_PATH）
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# 声明工程名，会作为生成的固件名：02biyesheji.bin
project(02biyesheji)

```

## main/CMakeLists.txt

```cmake
# ESP32 主组件(main)注册文件
# 告诉构建系统：源文件只有 main.c，头文件搜索路径为当前目录，
# 并声明依赖的 ESP-IDF 组件（缺任何一个都会在编译时报找不到头文件）。
idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES
        json              # cJSON：解析请求、生成响应
        esp_driver_i2c    # I2C 主机驱动：驱动 OLED
        esp_event         # 默认事件循环：接收 Wi-Fi / IP 事件
        esp_http_server   # HTTP 服务器：提供 / 与 /api/* 接口
        esp_lcd           # LCD 抽象层：SSD1306 面板驱动
        esp_netif         # 网络接口管理
        esp_wifi          # Wi-Fi STA 连接
        led_strip         # 第三方 WS2812 RGB 灯驱动
        nvs_flash         # 非易失存储（Wi-Fi 驱动内部依赖）
)

```

## main/idf_component.yml

```yaml
# ESP-IDF 组件管理器清单：声明本工程需要的第三方组件。
# 执行 `idf.py reconfigure` 时会读取本文件，并把依赖下载到 managed_components/ 目录。
dependencies:
  # WS2812/RGB 灯驱动；^2.5.5 表示接受 2.x 中的兼容版本
  espressif/led_strip: "^2.5.5"

```

## main/wifi_secrets.h.example

```c
/* Wi-Fi 凭据模板
 *
 * 用法：把本文件复制为同目录下的 wifi_secrets.h，再填入真实信息。
 *   - wifi_secrets.h 已被 .gitignore 忽略，不会被提交到仓库；
 *   - 真实文件是 main.c 中 #include "wifi_secrets.h" 的目标，
 *     缺少它会导致编译失败。
 */
#ifndef WIFI_SECRETS_H
#define WIFI_SECRETS_H

/* 改成你的 Wi-Fi 名称（SSID）与密码；开放网络可把密码留空 */
#define APP_WIFI_SSID "YOUR_WIFI_SSID"
#define APP_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

#endif

```

## host_gateway/requirements.txt

```text
# 网关运行所需的第三方 Python 依赖
# Pillow：把中文字符渲染成 128x64 单色位图，再推送给 ESP32 的 OLED 显示
Pillow>=10.0

```

## .clangd

```yaml
# clangd 语言服务器配置（仅影响编辑器补全/跳转，不影响编译）
# 移除 GCC 特有的 -f* / -m* 参数，否则 clangd 解析编译参数会报错、补全失效
CompileFlags:
    Remove: [-f*, -m*]

```

## .devcontainer/Dockerfile

```dockerfile
# 开发容器镜像：基于乐鑫官方 ESP-IDF 镜像
# 用于在 VS Code Dev Containers 里获得一致的 ESP-IDF 编译环境
ARG DOCKER_TAG=latest
FROM espressif/idf:${DOCKER_TAG}

# 统一字符编码，避免中文日志/文件名乱码
ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

# udev：容器内识别并访问串口设备（烧录/监视需要）
RUN apt-get update -y && apt-get install udev -y

# 进入容器自动加载 ESP-IDF 环境变量（idf.py 等命令可用）
RUN echo "source /opt/esp/idf/export.sh > /dev/null 2>&1" >> ~/.bashrc

# 使用官方入口脚本，默认打开 bash
ENTRYPOINT [ "/opt/esp/entrypoint.sh" ]
CMD ["/bin/bash", "-c"]

```

## .devcontainer/devcontainer.json

```json
{
	"name": "ESP-IDF QEMU",
	"build": {
		"dockerfile": "Dockerfile"
	},
	"customizations": {
		"vscode": {
			"settings": {
				"terminal.integrated.defaultProfile.linux": "bash",
				"idf.gitPath": "/usr/bin/git"
			},
			"extensions": [
				"espressif.esp-idf-extension",
				"espressif.esp-idf-web"
			]
		}
	},
	"runArgs": ["--privileged"]
}
```

## .gitignore

```text
# macOS
.DS_Store
.AppleDouble
.LSOverride

# Directory metadata
.directory

# Temporary files
*~
*.swp
*.swo
*.bak
*.tmp

# Log files
*.log

# Build artifacts and directories
**/build/
build/
*.o
*.a
*.out
*.exe # For any host-side utilities compiled on Windows

# ESP-IDF specific build outputs
*.bin
*.elf
*.map
flasher_args.json # Generated in build directory
sdkconfig.old
sdkconfig
main/wifi_secrets.h
host_gateway/.https/

# ESP-IDF dependencies
# For older versions or manual component management
/components/.idf/
**/components/.idf/
# For modern ESP-IDF component manager
managed_components/
# If ESP-IDF tools are installed/referenced locally to the project
.espressif/

# CMake generated files
CMakeCache.txt
CMakeFiles/
cmake_install.cmake
install_manifest.txt
CTestTestfile.cmake

# Python environment files
*.pyc
*.pyo
*.pyd
__pycache__/
*.egg-info/
dist/

# Virtual environment folders
venv/
.venv/
env/

# Language Servers
.clangd/
.ccls-cache/
compile_commands.json

# Windows specific
Thumbs.db
ehthumbs.db
Desktop.ini

# User-specific configuration files
*.user
*.workspace # General workspace files, can be from various tools
*.suo       # Visual Studio Solution User Options
*.sln.docstates # Visual Studio

```

## dependencies.lock

```yaml
dependencies:
  espressif/led_strip:
    component_hash: 28c6509a727ef74925b372ed404772aeedf11cce10b78c3f69b3c66799095e2d
    dependencies:
    - name: idf
      require: private
      version: '>=4.4'
    source:
      registry_url: https://components.espressif.com/
      type: service
    version: 2.5.5
  idf:
    source:
      type: idf
    version: 5.4.4
direct_dependencies:
- espressif/led_strip
manifest_hash: 82b8747b90c3ed4922275d5ecf98f7d7d19c33c2497dc0896b06118c383d87b0
target: esp32s3
version: 2.0.0

```

## BRIDGE_SETUP.md

````markdown
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
- `GET /api/status`：读取 RGB 灯当前的逻辑颜色值。
- `POST /api/action`：执行 `set_rgb` 操作，RGB 参数为 `0` 到 `255` 的整数。

请求示例：

```json
{"request_id":"demo-1","action":"set_rgb","r":0,"g":0,"b":255}
```

固件会限制 RGB 灯的最大亮度。OLED 文字由笔记本网关渲染后发送给 ESP32；蜂鸣器暂时还没有接入。

网关会把多个颜色的 RGB 分量相加并限制在 `0` 到 `255`，再按亮度百分比缩放后发送给 ESP32。例如红色和绿色会混成黄色；板载只有一颗 RGB 灯，因此不能让红、绿作为两个独立光点同时显示。只说“把灯亮度调到 30%”时会沿用当前灯色；关灯后网关也会记住最近一次颜色，供后续亮度调整恢复。

## 五、OLED 文字显示

此配置按 0.96 寸、128x64、SSD1306 I2C 屏幕设置：SCL 接 GPIO17，SDA 接 GPIO18，VCC 接 3.3V，GND 接 GND。固件会探测常见的 `0x3C` 和 `0x3D` 地址，并使用较低 I2C 速率及与已验证 OLED 程序相同的屏幕方向。串口日志出现 `SSD1306 OLED ready` 且设备状态中的 `oled_ready` 为 `true`，表示初始化成功。

网关会先把用户输入、再把模型回复发送到 OLED。每条文字从第一行开始逐屏显示一次，显示完停在最后一屏；每屏约显示 2 秒。中文由笔记本上的 Windows 中文字体渲染，因此启动网关前需执行 `python -m pip install -r requirements.txt`。

`POST /api/display` 接收一帧 128x64 单色画面，数据为 1024 字节 SSD1306 页面格式；正常聊天不需要手动调用此接口。

````
