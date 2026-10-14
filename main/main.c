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
