#include <string.h>

#include "app_state.h"
#include "bridge_http_internal.h"
#include "esp_log.h"

static const char *TAG = "bridge_action";

static esp_err_t send_curtain_state_response(httpd_req_t *req,
                                             const cJSON *request_root) {
  cJSON *response = cJSON_CreateObject();
  cJSON *state = cJSON_CreateObject();
  cJSON *curtain = bridge_http_create_curtain_status();
  if (response == NULL || state == NULL || curtain == NULL) {
    cJSON_Delete(response);
    cJSON_Delete(state);
    cJSON_Delete(curtain);
    return bridge_http_send_error(req, 500, "JSON allocation failed");
  }
  cJSON_AddBoolToObject(response, "ok", true);
  const cJSON *request_id =
      cJSON_GetObjectItemCaseSensitive(request_root, "request_id");
  if (cJSON_IsString(request_id)) {
    cJSON_AddStringToObject(response, "request_id", request_id->valuestring);
  }
  cJSON_AddItemToObject(state, "curtain", curtain);
  cJSON_AddItemToObject(response, "state", state);
  esp_err_t err = bridge_http_send_json(req, response);
  cJSON_Delete(response);
  return err;
}

/* Caller holds s_state_mutex. */
static esp_err_t queue_curtain_command_locked(const stepper_command_t *command,
                                              bool calibration_jog) {
  if (s_stepper_busy)
    return ESP_ERR_INVALID_STATE;
  if (calibration_jog) {
    if (s_curtain_calibration_stage != 1)
      return ESP_ERR_INVALID_STATE;
  } else {
    if (!s_curtain_position_valid)
      return ESP_ERR_INVALID_STATE;
    s_curtain_position_valid = false;
    esp_err_t save_err = bridge_curtain_save_state_locked();
    if (save_err != ESP_OK) {
      s_curtain_position_valid = true;
      return save_err;
    }
  }

  s_stepper_busy = true;
  s_stepper_stop_requested = false;
  s_stepper_toward_open = command->toward_open;
  s_stepper_remaining_steps = command->steps;
  if (s_stepper_queue == NULL ||
      xQueueSend(s_stepper_queue, command, 0) != pdTRUE) {
    s_stepper_busy = false;
    s_stepper_remaining_steps = 0;
    if (!calibration_jog) {
      s_curtain_position_valid = true;
      bridge_curtain_save_state_locked();
    }
    return ESP_FAIL;
  }
  return ESP_OK;
}

/*
 * POST /api/action ：执行 RGB 灯、雾化器或窗帘动作。
 *
 * 请求体示例：
 *   {"request_id":"demo-1","action":"set_rgb","r":0,"g":0,"b":255}
 *
 * 处理步骤：
 *   action=set_rgb: 校验 RGB、限幅并更新 WS2812；
 *   action=set_humidifier: 校验布尔值并驱动 GPIO8 高/低电平；
 *   窗帘动作: 校验校准状态、步数和目标百分比后排入电机任务。
 */
esp_err_t bridge_action_handler(httpd_req_t *req) {
  /* 空请求或超长请求直接拒绝 */
  if (req->content_len <= 0 || req->content_len > MAX_ACTION_BODY) {
    return bridge_http_send_error(req, 400, "Invalid request size");
  }

  /* 把完整请求体读进栈缓冲区，末尾补 '\0' 方便按字符串解析 */
  char body[MAX_ACTION_BODY + 1];
  size_t received = 0;
  while (received < (size_t)req->content_len) {
    int count =
        httpd_req_recv(req, body + received, req->content_len - received);
    if (count <= 0) {
      return ESP_FAIL; /* 连接中断 */
    }
    received += (size_t)count;
  }
  body[received] = '\0';

  cJSON *root = cJSON_Parse(body);
  if (root == NULL) {
    return bridge_http_send_error(req, 400, "Invalid JSON");
  }

  const cJSON *action = cJSON_GetObjectItemCaseSensitive(root, "action");
  if (!cJSON_IsString(action)) {
    cJSON_Delete(root);
    return bridge_http_send_error(req, 400, "Unsupported action");
  }

  if (strcmp(action->valuestring, "set_humidifier") == 0) {
    const cJSON *on_item = cJSON_GetObjectItemCaseSensitive(root, "on");
    if (!cJSON_IsBool(on_item)) {
      cJSON_Delete(root);
      return bridge_http_send_error(req, 400,
                                    "Humidifier on must be a boolean");
    }

    bool requested_on = cJSON_IsTrue(on_item);
    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (s_humidity_auto_mode) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(req, 409,
                                    "Automatic humidity control is active");
    }
    esp_err_t err = bridge_humidifier_set_output_locked(requested_on);
    xSemaphoreGive(s_state_mutex);
    if (err != ESP_OK) {
      cJSON_Delete(root);
      ESP_LOGE(TAG, "Humidifier GPIO update failed: %s", esp_err_to_name(err));
      return bridge_http_send_error(req, 500, "Humidifier GPIO update failed");
    }

    cJSON *response = cJSON_CreateObject();
    cJSON *state = cJSON_CreateObject();
    if (response == NULL || state == NULL) {
      cJSON_Delete(root);
      cJSON_Delete(response);
      cJSON_Delete(state);
      return bridge_http_send_error(req, 500, "JSON allocation failed");
    }
    cJSON_AddBoolToObject(response, "ok", true);
    const cJSON *request_id =
        cJSON_GetObjectItemCaseSensitive(root, "request_id");
    if (cJSON_IsString(request_id)) {
      cJSON_AddStringToObject(response, "request_id", request_id->valuestring);
    }
    cJSON_AddBoolToObject(state, "humidifier_on", s_humidifier_on);
    cJSON_AddItemToObject(response, "state", state);

    err = bridge_http_send_json(req, response);
    cJSON_Delete(root);
    cJSON_Delete(response);
    return err;
  }

  if (strcmp(action->valuestring, "stepper_stop") == 0) {
    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (s_stepper_busy)
      s_stepper_stop_requested = true;
    xSemaphoreGive(s_state_mutex);
    esp_err_t err = send_curtain_state_response(req, root);
    cJSON_Delete(root);
    return err;
  }

  if (strcmp(action->valuestring, "curtain_set_closed") == 0) {
    const cJSON *direction =
        cJSON_GetObjectItemCaseSensitive(root, "open_direction");
    if (!cJSON_IsString(direction) ||
        (strcmp(direction->valuestring, "forward") != 0 &&
         strcmp(direction->valuestring, "reverse") != 0)) {
      cJSON_Delete(root);
      return bridge_http_send_error(
          req, 400, "Open direction must be forward or reverse");
    }

    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (s_stepper_busy) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(req, 409,
                                    "Wait for the curtain motor to stop first");
    }
    bool old_open_forward = s_curtain_open_forward;
    int32_t old_position = s_curtain_position_steps;
    uint32_t old_travel = s_curtain_travel_steps;
    bool old_valid = s_curtain_position_valid;
    uint8_t old_stage = s_curtain_calibration_stage;
    s_curtain_open_forward = strcmp(direction->valuestring, "forward") == 0;
    s_curtain_position_steps = 0;
    s_curtain_travel_steps = 0;
    s_curtain_position_valid = false;
    s_curtain_calibration_stage = 1;
    esp_err_t err = bridge_curtain_save_state_locked();
    if (err != ESP_OK) {
      s_curtain_open_forward = old_open_forward;
      s_curtain_position_steps = old_position;
      s_curtain_travel_steps = old_travel;
      s_curtain_position_valid = old_valid;
      s_curtain_calibration_stage = old_stage;
    }
    xSemaphoreGive(s_state_mutex);
    if (err != ESP_OK) {
      cJSON_Delete(root);
      return bridge_http_send_error(req, 500,
                                    "Could not save curtain calibration");
    }
    err = send_curtain_state_response(req, root);
    cJSON_Delete(root);
    return err;
  }

  if (strcmp(action->valuestring, "curtain_set_open") == 0) {
    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (s_stepper_busy) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(req, 409,
                                    "Wait for the curtain motor to stop first");
    }
    if (s_curtain_calibration_stage != 1 || s_curtain_position_steps <= 0 ||
        s_curtain_position_steps > CURTAIN_MAX_TRAVEL_STEPS) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(
          req, 409, "Jog the curtain open from the closed endpoint first");
    }
    int32_t old_position = s_curtain_position_steps;
    uint32_t old_travel = s_curtain_travel_steps;
    bool old_valid = s_curtain_position_valid;
    uint8_t old_stage = s_curtain_calibration_stage;
    s_curtain_travel_steps = (uint32_t)s_curtain_position_steps;
    s_curtain_position_valid = true;
    s_curtain_calibration_stage = 0;
    esp_err_t err = bridge_curtain_save_state_locked();
    if (err != ESP_OK) {
      s_curtain_position_steps = old_position;
      s_curtain_travel_steps = old_travel;
      s_curtain_position_valid = old_valid;
      s_curtain_calibration_stage = old_stage;
    }
    xSemaphoreGive(s_state_mutex);
    if (err != ESP_OK) {
      cJSON_Delete(root);
      return bridge_http_send_error(req, 500,
                                    "Could not save curtain calibration");
    }
    err = send_curtain_state_response(req, root);
    cJSON_Delete(root);
    return err;
  }

  if (strcmp(action->valuestring, "curtain_set_position") == 0) {
    int position_pct;
    int interval_ms;
    if (!bridge_http_read_integer_range(root, "position_pct", 0, 100,
                                        &position_pct) ||
        !bridge_http_read_integer_range(
            root, "interval_ms", STEPPER_MIN_INTERVAL_MS,
            STEPPER_MAX_INTERVAL_MS, &interval_ms)) {
      cJSON_Delete(root);
      return bridge_http_send_error(req, 400,
                                    "Curtain position or speed is invalid");
    }

    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (s_stepper_busy) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(req, 409,
                                    "Curtain motor is already moving");
    }
    if (!s_curtain_position_valid || s_curtain_travel_steps == 0) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(
          req, 409, "Calibrate the curtain travel before position control");
    }
    uint32_t target_steps =
        (uint32_t)(((uint64_t)s_curtain_travel_steps * (uint32_t)position_pct +
                    50) /
                   100);
    if (target_steps == (uint32_t)s_curtain_position_steps) {
      xSemaphoreGive(s_state_mutex);
      esp_err_t err = send_curtain_state_response(req, root);
      cJSON_Delete(root);
      return err;
    }
    bool toward_open = target_steps > (uint32_t)s_curtain_position_steps;
    stepper_command_t command = {
        .forward =
            toward_open ? s_curtain_open_forward : !s_curtain_open_forward,
        .toward_open = toward_open,
        .steps = toward_open
                     ? target_steps - (uint32_t)s_curtain_position_steps
                     : (uint32_t)s_curtain_position_steps - target_steps,
        .interval_ms = (uint16_t)interval_ms,
    };
    esp_err_t err = queue_curtain_command_locked(&command, false);
    xSemaphoreGive(s_state_mutex);
    if (err != ESP_OK) {
      cJSON_Delete(root);
      return bridge_http_send_error(req, err == ESP_FAIL ? 503 : 500,
                                    "Could not start curtain movement");
    }
    err = send_curtain_state_response(req, root);
    cJSON_Delete(root);
    return err;
  }

  if (strcmp(action->valuestring, "curtain_jog") == 0) {
    const cJSON *direction =
        cJSON_GetObjectItemCaseSensitive(root, "direction");
    int steps;
    int interval_ms;
    if (!cJSON_IsString(direction) ||
        (strcmp(direction->valuestring, "open") != 0 &&
         strcmp(direction->valuestring, "close") != 0) ||
        !bridge_http_read_integer_range(root, "steps", STEPPER_MIN_STEPS,
                                        STEPPER_MAX_STEPS, &steps) ||
        !bridge_http_read_integer_range(
            root, "interval_ms", STEPPER_MIN_INTERVAL_MS,
            STEPPER_MAX_INTERVAL_MS, &interval_ms)) {
      cJSON_Delete(root);
      return bridge_http_send_error(
          req, 400, "Invalid curtain jog direction, steps, or interval");
    }

    bool toward_open = strcmp(direction->valuestring, "open") == 0;
    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (s_stepper_busy) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(req, 409,
                                    "Curtain motor is already moving");
    }
    if (s_curtain_calibration_stage != 1) {
      xSemaphoreGive(s_state_mutex);
      cJSON_Delete(root);
      return bridge_http_send_error(
          req, 409, "Mark the closed endpoint before calibration jogging");
    }
    stepper_command_t command = {
        .forward =
            toward_open ? s_curtain_open_forward : !s_curtain_open_forward,
        .toward_open = toward_open,
        .steps = (uint32_t)steps,
        .interval_ms = (uint16_t)interval_ms,
    };
    esp_err_t err = queue_curtain_command_locked(&command, true);
    xSemaphoreGive(s_state_mutex);
    if (err != ESP_OK) {
      cJSON_Delete(root);
      return bridge_http_send_error(req, err == ESP_FAIL ? 503 : 409,
                                    "Could not start curtain calibration jog");
    }
    err = send_curtain_state_response(req, root);
    cJSON_Delete(root);
    return err;
  }

  if (strcmp(action->valuestring, "set_rgb") != 0) {
    cJSON_Delete(root);
    return bridge_http_send_error(req, 400, "Unsupported action");
  }

  /* 三个颜色分量必须同时合法 */
  int r;
  int g;
  int b;
  if (!bridge_http_read_rgb_value(root, "r", &r) ||
      !bridge_http_read_rgb_value(root, "g", &g) ||
      !bridge_http_read_rgb_value(root, "b", &b)) {
    cJSON_Delete(root);
    return bridge_http_send_error(req, 400,
                                  "RGB values must be integers from 0 to 255");
  }

  esp_err_t err = bridge_led_set_rgb((uint8_t)r, (uint8_t)g, (uint8_t)b);
  if (err != ESP_OK) {
    cJSON_Delete(root);
    ESP_LOGE(TAG, "RGB update failed: %s", esp_err_to_name(err));
    return bridge_http_send_error(req, 500, "RGB update failed");
  }

  /* 拼装响应：{"ok":true,"request_id":...,"state":{"rgb":{r,g,b}}} */
  cJSON *response = cJSON_CreateObject();
  cJSON *state = cJSON_CreateObject();
  cJSON *rgb = cJSON_CreateObject();
  if (response == NULL || state == NULL || rgb == NULL) {
    cJSON_Delete(root);
    cJSON_Delete(response);
    cJSON_Delete(state);
    cJSON_Delete(rgb);
    return bridge_http_send_error(req, 500, "JSON allocation failed");
  }

  cJSON_AddBoolToObject(response, "ok", true);
  /* 有 request_id 就原样回显，便于网关做请求对账 */
  const cJSON *request_id =
      cJSON_GetObjectItemCaseSensitive(root, "request_id");
  if (cJSON_IsString(request_id)) {
    cJSON_AddStringToObject(response, "request_id", request_id->valuestring);
  }
  cJSON_AddNumberToObject(rgb, "r", r);
  cJSON_AddNumberToObject(rgb, "g", g);
  cJSON_AddNumberToObject(rgb, "b", b);
  cJSON_AddItemToObject(state, "rgb", rgb);
  cJSON_AddItemToObject(response, "state", state);

  err = bridge_http_send_json(req, response);
  cJSON_Delete(root);
  cJSON_Delete(response);
  return err;
}
