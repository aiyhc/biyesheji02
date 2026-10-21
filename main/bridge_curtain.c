#include "app_state.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/task.h"
#include "nvs.h"

static const char *TAG = "bridge_curtain";

/* Caller holds s_state_mutex. */
esp_err_t bridge_curtain_save_state_locked(void) {
  nvs_handle_t handle;
  esp_err_t err = nvs_open("curtain", NVS_READWRITE, &handle);
  if (err != ESP_OK)
    return err;

  uint32_t position = s_curtain_position_valid && s_curtain_position_steps >= 0
                          ? (uint32_t)s_curtain_position_steps
                          : 0;
  err = nvs_set_u32(handle, "position", position);
  if (err == ESP_OK)
    err = nvs_set_u32(handle, "travel", s_curtain_travel_steps);
  if (err == ESP_OK)
    err = nvs_set_u8(handle, "valid", s_curtain_position_valid ? 1 : 0);
  if (err == ESP_OK)
    err = nvs_set_u8(handle, "open_fwd", s_curtain_open_forward ? 1 : 0);
  if (err == ESP_OK)
    err = nvs_set_u8(handle, "cal_stage", s_curtain_calibration_stage);
  if (err == ESP_OK)
    err = nvs_commit(handle);
  nvs_close(handle);
  return err;
}

static void load_curtain_state(void) {
  nvs_handle_t handle;
  if (nvs_open("curtain", NVS_READONLY, &handle) != ESP_OK)
    return;

  uint32_t position = 0;
  uint32_t travel = 0;
  uint8_t valid = 0;
  uint8_t open_forward = 1;
  uint8_t calibration_stage = 0;
  nvs_get_u32(handle, "position", &position);
  nvs_get_u32(handle, "travel", &travel);
  nvs_get_u8(handle, "valid", &valid);
  nvs_get_u8(handle, "open_fwd", &open_forward);
  nvs_get_u8(handle, "cal_stage", &calibration_stage);
  nvs_close(handle);

  if (open_forward > 1)
    open_forward = 1;
  s_curtain_open_forward = open_forward == 1;
  if (calibration_stage != 0 || valid != 1 || travel == 0 ||
      travel > CURTAIN_MAX_TRAVEL_STEPS || position > travel) {
    s_curtain_position_steps = 0;
    s_curtain_travel_steps =
        calibration_stage == 0 && travel <= CURTAIN_MAX_TRAVEL_STEPS ? travel
                                                                     : 0;
    s_curtain_position_valid = false;
    s_curtain_calibration_stage = 0;
  } else {
    s_curtain_position_steps = (int32_t)position;
    s_curtain_travel_steps = travel;
    s_curtain_position_valid = true;
    s_curtain_calibration_stage = 0;
  }
  ESP_LOGI(TAG, "Curtain state: %s, position=%ld/%lu steps, open=%s",
           s_curtain_position_valid ? "valid" : "needs calibration",
           (long)s_curtain_position_steps,
           (unsigned long)s_curtain_travel_steps,
           s_curtain_open_forward ? "forward" : "reverse");
}

static void stepper_task(void *arg) {
  (void)arg;
  static const uint8_t phase_masks[8] = {
      0x01, 0x03, 0x02, 0x06, 0x04, 0x0C, 0x08, 0x09,
  };
  const gpio_num_t pins[4] = {
      STEPPER_IN1_GPIO,
      STEPPER_IN2_GPIO,
      STEPPER_IN3_GPIO,
      STEPPER_IN4_GPIO,
  };
  uint8_t phase = 7;
  stepper_command_t command;

  while (true) {
    if (xQueueReceive(s_stepper_queue, &command, portMAX_DELAY) != pdTRUE)
      continue;

    bool gpio_failed = false;
    for (uint32_t index = 0; index < command.steps; ++index) {
      xSemaphoreTake(s_state_mutex, portMAX_DELAY);
      bool stop = s_stepper_stop_requested;
      xSemaphoreGive(s_state_mutex);
      if (stop)
        break;

      phase = (uint8_t)((phase + (command.forward ? 1 : 7)) % 8);
      uint8_t mask = phase_masks[phase];
      esp_err_t err = ESP_OK;
      for (int pin = 0; pin < 4 && err == ESP_OK; ++pin) {
        err = gpio_set_level(pins[pin], (mask >> pin) & 1U);
      }
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "Stepper GPIO update failed: %s", esp_err_to_name(err));
        gpio_failed = true;
        break;
      }

      xSemaphoreTake(s_state_mutex, portMAX_DELAY);
      s_stepper_remaining_steps = command.steps - index - 1;
      if (s_curtain_calibration_stage == 1) {
        int32_t delta = command.toward_open ? 1 : -1;
        int64_t next_position = (int64_t)s_curtain_position_steps + delta;
        if (next_position > CURTAIN_MAX_TRAVEL_STEPS ||
            next_position < -(int64_t)CURTAIN_MAX_TRAVEL_STEPS) {
          gpio_failed = true;
          s_stepper_stop_requested = true;
        } else {
          s_curtain_position_steps = (int32_t)next_position;
        }
      } else if (s_curtain_travel_steps > 0) {
        if (command.toward_open &&
            (uint32_t)s_curtain_position_steps < s_curtain_travel_steps) {
          s_curtain_position_steps++;
        } else if (!command.toward_open && s_curtain_position_steps > 0) {
          s_curtain_position_steps--;
        }
      }
      xSemaphoreGive(s_state_mutex);
      if (gpio_failed)
        break;
      vTaskDelay(pdMS_TO_TICKS(command.interval_ms));
    }

    for (int pin = 0; pin < 4; ++pin)
      gpio_set_level(pins[pin], 0);
    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    s_stepper_busy = false;
    s_stepper_stop_requested = false;
    s_stepper_remaining_steps = 0;
    if (s_curtain_calibration_stage == 0 && s_curtain_travel_steps > 0) {
      s_curtain_position_valid = !gpio_failed;
      esp_err_t save_err = bridge_curtain_save_state_locked();
      if (save_err != ESP_OK) {
        s_curtain_position_valid = false;
        ESP_LOGE(TAG, "Saving curtain position failed: %s",
                 esp_err_to_name(save_err));
      }
    }
    xSemaphoreGive(s_state_mutex);
  }
}

void bridge_curtain_init(void) {
  const uint64_t pin_mask =
      (1ULL << STEPPER_IN1_GPIO) | (1ULL << STEPPER_IN2_GPIO) |
      (1ULL << STEPPER_IN3_GPIO) | (1ULL << STEPPER_IN4_GPIO);
  const gpio_config_t config = {
      .pin_bit_mask = pin_mask,
      .mode = GPIO_MODE_OUTPUT,
      .pull_up_en = GPIO_PULLUP_DISABLE,
      .pull_down_en = GPIO_PULLDOWN_ENABLE,
      .intr_type = GPIO_INTR_DISABLE,
  };
  ESP_ERROR_CHECK(gpio_config(&config));
  ESP_ERROR_CHECK(gpio_set_level(STEPPER_IN1_GPIO, 0));
  ESP_ERROR_CHECK(gpio_set_level(STEPPER_IN2_GPIO, 0));
  ESP_ERROR_CHECK(gpio_set_level(STEPPER_IN3_GPIO, 0));
  ESP_ERROR_CHECK(gpio_set_level(STEPPER_IN4_GPIO, 0));
  load_curtain_state();

  s_stepper_queue = xQueueCreate(1, sizeof(stepper_command_t));
  ESP_ERROR_CHECK(s_stepper_queue == NULL ? ESP_ERR_NO_MEM : ESP_OK);
  ESP_ERROR_CHECK(xTaskCreate(stepper_task, "stepper", 3072, NULL, 5, NULL) ==
                          pdPASS
                      ? ESP_OK
                      : ESP_ERR_NO_MEM);
  ESP_LOGI(TAG, "ULN2003 stepper ready: IN1=%d IN2=%d IN3=%d IN4=%d",
           STEPPER_IN1_GPIO, STEPPER_IN2_GPIO, STEPPER_IN3_GPIO,
           STEPPER_IN4_GPIO);
}
