#include <stdlib.h>
#include <string.h>

#include "app_state.h"
#include "bridge_http_internal.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "bridge_http";
static httpd_handle_t s_http_server;

/*
 * 把 cJSON 对象序列化为 JSON 文本并作为响应发出。
 * 返回值沿用 httpd_resp_sendstr 的结果，字符串内存由本函数负责释放。
 */
esp_err_t bridge_http_send_json(httpd_req_t *req, cJSON *root) {
  char *body = cJSON_PrintUnformatted(root);
  if (body == NULL) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                        "JSON allocation failed");
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
esp_err_t bridge_http_send_error(httpd_req_t *req, int status,
                                 const char *message) {
  cJSON *root = cJSON_CreateObject();
  if (root == NULL) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                        "JSON allocation failed");
    return ESP_FAIL;
  }

  cJSON_AddBoolToObject(root, "ok", false);
  cJSON_AddStringToObject(root, "error", message);
  /* 控制冲突(409)用于拒绝自动模式中的手动开关指令 */
  httpd_resp_set_status(req, status == 400   ? "400 Bad Request"
                             : status == 409 ? "409 Conflict"
                             : status == 404 ? "404 Not Found"
                             : status == 503 ? "503 Service Unavailable"
                                             : "500 Internal Server Error");
  esp_err_t err = bridge_http_send_json(req, root);
  cJSON_Delete(root);
  return err;
}

/*
 * 从 JSON 对象里读取一个 0~255 的整数颜色分量。
 * 要求：字段存在、是数字、必须为整数（拒绝 3.5 这类小数）、且在 0~255 之间。
 * 校验通过后写入 *value 并返回 true。
 */
bool bridge_http_read_integer_range(const cJSON *root, const char *key,
                                    int minimum, int maximum, int *value) {
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
  if (!cJSON_IsNumber(item) || item->valuedouble < minimum ||
      item->valuedouble > maximum ||
      item->valuedouble != (double)item->valueint) {
    return false;
  }
  *value = item->valueint;
  return true;
}

bool bridge_http_read_rgb_value(const cJSON *root, const char *key,
                                int *value) {
  return bridge_http_read_integer_range(root, key, 0, 255, value);
}

/*
 * GET / ：最简单的在线探针。
 * 浏览器或网关只要能拿到 "ESP32 bridge is online"，就说明设备已连上 Wi-Fi。
 */
static esp_err_t root_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/plain; charset=utf-8");
  return httpd_resp_sendstr(req, "ESP32 bridge is online");
}

cJSON *bridge_http_create_curtain_status(void) {
  bool busy;
  bool stop_requested;
  bool open_forward;
  bool toward_open;
  bool position_valid;
  int32_t position_steps;
  uint32_t travel_steps;
  uint32_t remaining_steps;
  uint8_t calibration_stage;
  xSemaphoreTake(s_state_mutex, portMAX_DELAY);
  busy = s_stepper_busy;
  stop_requested = s_stepper_stop_requested;
  open_forward = s_curtain_open_forward;
  toward_open = s_stepper_toward_open;
  position_valid = s_curtain_position_valid;
  position_steps = s_curtain_position_steps;
  travel_steps = s_curtain_travel_steps;
  remaining_steps = s_stepper_remaining_steps;
  calibration_stage = s_curtain_calibration_stage;
  xSemaphoreGive(s_state_mutex);

  cJSON *curtain = cJSON_CreateObject();
  if (curtain == NULL)
    return NULL;
  int position_pct = -1;
  if (travel_steps > 0 && position_steps >= 0) {
    position_pct = (int)(((uint64_t)position_steps * 100 + travel_steps / 2) /
                         travel_steps);
    if (position_pct > 100)
      position_pct = 100;
  }
  cJSON_AddBoolToObject(curtain, "busy", busy);
  cJSON_AddBoolToObject(curtain, "stop_requested", stop_requested);
  cJSON_AddBoolToObject(curtain, "moving_toward_open", toward_open);
  cJSON_AddBoolToObject(curtain, "calibrated",
                        calibration_stage == 0 && travel_steps > 0);
  cJSON_AddBoolToObject(curtain, "position_valid", position_valid);
  cJSON_AddStringToObject(curtain, "open_direction",
                          open_forward ? "forward" : "reverse");
  cJSON_AddNumberToObject(curtain, "position_steps", position_steps);
  cJSON_AddNumberToObject(curtain, "travel_steps", travel_steps);
  cJSON_AddNumberToObject(curtain, "position_pct", position_pct);
  cJSON_AddNumberToObject(curtain, "remaining_steps", remaining_steps);
  cJSON_AddNumberToObject(curtain, "calibration_stage", calibration_stage);
  return curtain;
}

/*
 * GET /api/status ：返回灯的当前逻辑颜色、雾化器状态以及 OLED 是否就绪。
 * 网关在"只调亮度"的场景下会先读这里，拿到当前颜色再重新计算。
 */
static esp_err_t status_handler(httpd_req_t *req) {
  float temperature_c;
  float humidity_pct;
  bool sensor_ok;
  bool humidifier_on;
  bool auto_mode;
  uint8_t on_below_pct;
  uint8_t off_at_pct;
  int64_t last_sample_us;
  xSemaphoreTake(s_state_mutex, portMAX_DELAY);
  temperature_c = s_temperature_c;
  humidity_pct = s_humidity_pct;
  sensor_ok = s_sensor_ok;
  humidifier_on = s_humidifier_on;
  auto_mode = s_humidity_auto_mode;
  on_below_pct = s_humidity_on_below;
  off_at_pct = s_humidity_off_at;
  last_sample_us = s_sensor_last_sample_us;
  xSemaphoreGive(s_state_mutex);

  cJSON *root = cJSON_CreateObject();
  cJSON *rgb = cJSON_CreateObject();
  cJSON *curtain = bridge_http_create_curtain_status();
  if (root == NULL || rgb == NULL || curtain == NULL) {
    /* 两个对象都要检查，任一失败就不能继续 */
    cJSON_Delete(root);
    cJSON_Delete(rgb);
    cJSON_Delete(curtain);
    return bridge_http_send_error(req, 500, "JSON allocation failed");
  }

  uint8_t red, green, blue;
  bridge_led_get_rgb(&red, &green, &blue);

  cJSON_AddBoolToObject(root, "ok", true);
  /* 逻辑颜色（0~255），不是实际点灯的缩放值 */
  cJSON_AddNumberToObject(rgb, "r", red);
  cJSON_AddNumberToObject(rgb, "g", green);
  cJSON_AddNumberToObject(rgb, "b", blue);
  cJSON_AddItemToObject(root, "rgb", rgb);
  cJSON_AddBoolToObject(root, "humidifier_on", humidifier_on);
  cJSON_AddBoolToObject(root, "sensor_ok", sensor_ok);
  cJSON_AddNumberToObject(root, "temperature_c", temperature_c);
  cJSON_AddNumberToObject(root, "humidity_pct", humidity_pct);
  cJSON_AddNumberToObject(
      root, "sensor_age_ms",
      last_sample_us > 0 ? (esp_timer_get_time() - last_sample_us) / 1000 : -1);
  cJSON_AddBoolToObject(root, "auto_mode", auto_mode);
  cJSON_AddNumberToObject(root, "on_below_pct", on_below_pct);
  cJSON_AddNumberToObject(root, "off_at_pct", off_at_pct);
  cJSON_AddItemToObject(root, "curtain", curtain);
  /* OLED 未探测到时为 false，网关侧不会向不可用的屏推图 */
  cJSON_AddBoolToObject(root, "oled_ready", bridge_oled_is_ready());

  esp_err_t err = bridge_http_send_json(req, root);
  cJSON_Delete(root);
  return err;
}

static esp_err_t humidity_config_handler(httpd_req_t *req) {
  if (req->content_len <= 0 || req->content_len > MAX_ACTION_BODY) {
    return bridge_http_send_error(req, 400, "Invalid request size");
  }

  char body[MAX_ACTION_BODY + 1];
  size_t received = 0;
  while (received < (size_t)req->content_len) {
    int count =
        httpd_req_recv(req, body + received, req->content_len - received);
    if (count <= 0)
      return ESP_FAIL;
    received += (size_t)count;
  }
  body[received] = '\0';

  cJSON *root = cJSON_Parse(body);
  if (root == NULL)
    return bridge_http_send_error(req, 400, "Invalid JSON");

  const cJSON *auto_item = cJSON_GetObjectItemCaseSensitive(root, "auto_mode");
  int on_below_pct;
  int off_at_pct;
  if (!cJSON_IsBool(auto_item) ||
      !bridge_http_read_integer_range(root, "on_below_pct",
                                      HUMIDITY_ON_BELOW_MIN,
                                      HUMIDITY_ON_BELOW_MAX, &on_below_pct) ||
      !bridge_http_read_integer_range(root, "off_at_pct", HUMIDITY_OFF_AT_MIN,
                                      HUMIDITY_OFF_AT_MAX, &off_at_pct) ||
      off_at_pct - on_below_pct < HUMIDITY_MIN_THRESHOLD_GAP ||
      off_at_pct - on_below_pct > HUMIDITY_MAX_THRESHOLD_GAP) {
    cJSON_Delete(root);
    return bridge_http_send_error(req, 400,
                                  "Invalid humidity automation settings");
  }

  bool auto_mode = cJSON_IsTrue(auto_item);
  esp_err_t err = bridge_humidity_save_settings(
      auto_mode, (uint8_t)on_below_pct, (uint8_t)off_at_pct);
  if (err != ESP_OK) {
    cJSON_Delete(root);
    ESP_LOGE(TAG, "Saving humidity settings failed: %s", esp_err_to_name(err));
    return bridge_http_send_error(req, 500, "Could not save humidity settings");
  }

  xSemaphoreTake(s_state_mutex, portMAX_DELAY);
  s_humidity_auto_mode = auto_mode;
  s_humidity_on_below = (uint8_t)on_below_pct;
  s_humidity_off_at = (uint8_t)off_at_pct;
  if (auto_mode) {
    bridge_humidity_update_automation_locked();
  } else if (s_humidifier_on) {
    err = bridge_humidifier_set_output_locked(false);
  }
  bool humidifier_on = s_humidifier_on;
  xSemaphoreGive(s_state_mutex);
  cJSON_Delete(root);
  if (err != ESP_OK)
    return bridge_http_send_error(req, 500,
                                  "Could not update humidifier output");

  cJSON *response = cJSON_CreateObject();
  if (response == NULL)
    return bridge_http_send_error(req, 500, "JSON allocation failed");
  cJSON_AddBoolToObject(response, "ok", true);
  cJSON_AddBoolToObject(response, "auto_mode", auto_mode);
  cJSON_AddNumberToObject(response, "on_below_pct", on_below_pct);
  cJSON_AddNumberToObject(response, "off_at_pct", off_at_pct);
  cJSON_AddBoolToObject(response, "humidifier_on", humidifier_on);
  err = bridge_http_send_json(req, response);
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
static esp_err_t display_handler(httpd_req_t *req) {
  /* OLED 没初始化成功时明确告知调用方 */
  if (!bridge_oled_is_ready()) {
    return bridge_http_send_error(req, 503, "OLED is not available");
  }
  if (req->content_len != OLED_FRAME_BYTES) {
    return bridge_http_send_error(req, 400, "Display frame must be 1024 bytes");
  }

  /* 一帧 1KB，用堆内存避免占用栈空间 */
  uint8_t *frame = malloc(OLED_FRAME_BYTES);
  if (frame == NULL) {
    return bridge_http_send_error(req, 500, "Display frame allocation failed");
  }

  /* 循环读取，直到收满 1024 字节 */
  size_t received = 0;
  while (received < OLED_FRAME_BYTES) {
    int count = httpd_req_recv(req, (char *)frame + received,
                               OLED_FRAME_BYTES - received);
    if (count <= 0) {
      free(frame);
      return ESP_FAIL;
    }
    received += count;
  }

  /* 一次性把整幅画面刷到屏幕 */
  esp_err_t err = bridge_oled_draw_frame(frame);
  free(frame);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "OLED frame update failed: %s", esp_err_to_name(err));
    return bridge_http_send_error(req, 500, "OLED frame update failed");
  }

  cJSON *response = cJSON_CreateObject();
  if (response == NULL) {
    return bridge_http_send_error(req, 500, "JSON allocation failed");
  }
  cJSON_AddBoolToObject(response, "ok", true);
  err = bridge_http_send_json(req, response);
  cJSON_Delete(response);
  return err;
}

/*
 * 启动 HTTP 服务器并注册全部路由。
 * max_uri_handlers 需要 >= 实际注册的路由数（这里是 5 个，留了 1 个余量）。
 */
esp_err_t bridge_http_start(void) {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.max_uri_handlers = 6;
  esp_err_t err = httpd_start(&s_http_server, &config);
  if (err != ESP_OK) {
    return err;
  }

  /* 五个路由：探针、状态、设备动作、控湿设置、OLED 刷图 */
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
      .handler = bridge_action_handler,
  };
  const httpd_uri_t humidity_config_uri = {
      .uri = "/api/humidity/config",
      .method = HTTP_POST,
      .handler = humidity_config_handler,
  };
  const httpd_uri_t display_uri = {
      .uri = "/api/display",
      .method = HTTP_POST,
      .handler = display_handler,
  };

  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &root_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &status_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &action_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &humidity_config_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &display_uri));
  ESP_LOGI(TAG, "HTTP control server started on port 80");
  return ESP_OK;
}
