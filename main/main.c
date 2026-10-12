#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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
#include "wifi_secrets.h"

#define RGB_LED_GPIO 48
#define RGB_LED_COUNT 1
#define RGB_BRIGHTNESS_LIMIT 48
#define OLED_SCL_GPIO 17
#define OLED_SDA_GPIO 18
#define OLED_I2C_CLOCK_HZ 50000
#define OLED_I2C_ADDR_PRIMARY 0x3C
#define OLED_I2C_ADDR_SECONDARY 0x3D
#define OLED_WIDTH 128
#define OLED_HEIGHT 64
#define OLED_FRAME_BYTES (OLED_WIDTH * OLED_HEIGHT / 8)
#define WIFI_CONNECTED_BIT (1U << 0)
#define MAX_ACTION_BODY 512

static const char *TAG = "bridge";
static EventGroupHandle_t s_wifi_event_group;
static led_strip_handle_t s_rgb_strip;
static httpd_handle_t s_http_server;
static i2c_master_bus_handle_t s_oled_i2c_bus;
static esp_lcd_panel_io_handle_t s_oled_panel_io;
static esp_lcd_panel_handle_t s_oled_panel;
static uint8_t s_oled_blank_frame[OLED_FRAME_BYTES];
static uint8_t s_rgb_r;
static uint8_t s_rgb_g;
static uint8_t s_rgb_b;

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

static esp_err_t send_error(httpd_req_t *req, int status, const char *message)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "JSON allocation failed");
        return ESP_FAIL;
    }

    cJSON_AddBoolToObject(root, "ok", false);
    cJSON_AddStringToObject(root, "error", message);
    httpd_resp_set_status(req, status == 400 ? "400 Bad Request" :
                               status == 404 ? "404 Not Found" :
                               status == 503 ? "503 Service Unavailable" :
                               "500 Internal Server Error");
    esp_err_t err = send_json(req, root);
    cJSON_Delete(root);
    return err;
}

static esp_err_t root_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    return httpd_resp_sendstr(req, "ESP32 bridge is online");
}

static esp_err_t status_handler(httpd_req_t *req)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *rgb = cJSON_CreateObject();
    if (root == NULL || rgb == NULL) {
        cJSON_Delete(root);
        cJSON_Delete(rgb);
        return send_error(req, 500, "JSON allocation failed");
    }

    cJSON_AddBoolToObject(root, "ok", true);
    cJSON_AddNumberToObject(rgb, "r", s_rgb_r);
    cJSON_AddNumberToObject(rgb, "g", s_rgb_g);
    cJSON_AddNumberToObject(rgb, "b", s_rgb_b);
    cJSON_AddItemToObject(root, "rgb", rgb);
    cJSON_AddBoolToObject(root, "oled_ready", s_oled_panel != NULL);

    esp_err_t err = send_json(req, root);
    cJSON_Delete(root);
    return err;
}

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

static esp_err_t action_handler(httpd_req_t *req)
{
    if (req->content_len <= 0 || req->content_len > MAX_ACTION_BODY) {
        return send_error(req, 400, "Invalid request size");
    }

    char body[MAX_ACTION_BODY + 1];
    size_t received = 0;
    while (received < (size_t)req->content_len) {
        int count = httpd_req_recv(req, body + received, req->content_len - received);
        if (count <= 0) {
            return ESP_FAIL;
        }
        received += (size_t)count;
    }
    body[received] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return send_error(req, 400, "Invalid JSON");
    }

    const cJSON *action = cJSON_GetObjectItemCaseSensitive(root, "action");
    if (!cJSON_IsString(action) || strcmp(action->valuestring, "set_rgb") != 0) {
        cJSON_Delete(root);
        return send_error(req, 400, "Unsupported action");
    }

    int r;
    int g;
    int b;
    if (!read_rgb_value(root, "r", &r) ||
        !read_rgb_value(root, "g", &g) ||
        !read_rgb_value(root, "b", &b)) {
        cJSON_Delete(root);
        return send_error(req, 400, "RGB values must be integers from 0 to 255");
    }

    uint8_t scaled_r = (uint8_t)(r * RGB_BRIGHTNESS_LIMIT / 255);
    uint8_t scaled_g = (uint8_t)(g * RGB_BRIGHTNESS_LIMIT / 255);
    uint8_t scaled_b = (uint8_t)(b * RGB_BRIGHTNESS_LIMIT / 255);
    esp_err_t err = led_strip_set_pixel(s_rgb_strip, 0, scaled_r, scaled_g, scaled_b);
    if (err == ESP_OK) {
        err = led_strip_refresh(s_rgb_strip);
    }
    if (err != ESP_OK) {
        cJSON_Delete(root);
        ESP_LOGE(TAG, "RGB update failed: %s", esp_err_to_name(err));
        return send_error(req, 500, "RGB update failed");
    }

    s_rgb_r = (uint8_t)r;
    s_rgb_g = (uint8_t)g;
    s_rgb_b = (uint8_t)b;

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

static esp_err_t display_handler(httpd_req_t *req)
{
    if (s_oled_panel == NULL) {
        return send_error(req, 503, "OLED is not available");
    }
    if (req->content_len != OLED_FRAME_BYTES) {
        return send_error(req, 400, "Display frame must be 1024 bytes");
    }

    uint8_t *frame = malloc(OLED_FRAME_BYTES);
    if (frame == NULL) {
        return send_error(req, 500, "Display frame allocation failed");
    }

    size_t received = 0;
    while (received < OLED_FRAME_BYTES) {
        int count = httpd_req_recv(req, (char *)frame + received, OLED_FRAME_BYTES - received);
        if (count <= 0) {
            free(frame);
            return ESP_FAIL;
        }
        received += count;
    }

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

static esp_err_t start_http_server(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 5;
    esp_err_t err = httpd_start(&s_http_server, &config);
    if (err != ESP_OK) {
        return err;
    }

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
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Connected. Open http://" IPSTR "/", IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

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

    wifi_config_t wifi_config = {0};
    snprintf((char *)wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid), "%s",
             APP_WIFI_SSID);
    snprintf((char *)wifi_config.sta.password, sizeof(wifi_config.sta.password), "%s",
             APP_WIFI_PASSWORD);
    wifi_config.sta.threshold.authmode =
        strlen(APP_WIFI_PASSWORD) == 0 ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;

    if (strlen(APP_WIFI_SSID) == 0 || strcmp(APP_WIFI_SSID, "YOUR_WIFI_SSID") == 0) {
        ESP_LOGE(TAG, "Set APP_WIFI_SSID in main/wifi_secrets.h before flashing.");
        return;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
}

static void init_rgb_led(void)
{
    const led_strip_config_t strip_config = {
        .strip_gpio_num = RGB_LED_GPIO,
        .max_leds = RGB_LED_COUNT,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB,
        .led_model = LED_MODEL_WS2812,
        .flags.invert_out = false,
    };
    const led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
        .flags.with_dma = false,
    };

    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &s_rgb_strip));
    ESP_ERROR_CHECK(led_strip_clear(s_rgb_strip));
}

static void init_oled(void)
{
    const i2c_master_bus_config_t bus_config = {
        .i2c_port = -1,
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
        err = esp_lcd_panel_mirror(s_oled_panel, true, true);
    }
    if (err == ESP_OK) {
        err = esp_lcd_panel_disp_on_off(s_oled_panel, true);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OLED panel init failed: %s", esp_err_to_name(err));
        s_oled_panel = NULL;
        return;
    }

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

static void init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

void app_main(void)
{
    init_nvs();
    init_rgb_led();
    init_oled();
    init_wifi_station();

    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_ERROR_CHECK(start_http_server());

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
