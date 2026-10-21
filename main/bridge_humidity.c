#include <math.h>
#include <stdint.h>

#include "app_state.h"
#include "dht.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "nvs.h"

static const char *TAG = "bridge_humidity";

/* Caller must hold s_state_mutex. */
esp_err_t bridge_humidifier_set_output_locked(bool on) {
  esp_err_t err = gpio_set_level(HUMIDIFIER_GPIO, on ? 1 : 0);
  if (err == ESP_OK) {
    s_humidifier_on = on;
  }
  return err;
}

/* Caller must hold s_state_mutex. A failed sensor read eventually fails safe to
 * off. */
void bridge_humidity_update_automation_locked(void) {
  if (!s_humidity_auto_mode) {
    return;
  }

  bool desired_on = false;
  if (s_sensor_ok) {
    if (s_humidity_pct <= (float)s_humidity_on_below) {
      desired_on = true;
    } else if (s_humidity_pct >= (float)s_humidity_off_at) {
      desired_on = false;
    } else {
      desired_on = s_humidifier_on;
    }
  }

  if (desired_on != s_humidifier_on) {
    esp_err_t err = bridge_humidifier_set_output_locked(desired_on);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Automatic humidifier update failed: %s",
               esp_err_to_name(err));
    }
  }
}

void bridge_humidifier_init(void) {
  const gpio_config_t config = {
      .pin_bit_mask = 1ULL << HUMIDIFIER_GPIO,
      .mode = GPIO_MODE_OUTPUT,
      .pull_up_en = GPIO_PULLUP_DISABLE,
      .pull_down_en = GPIO_PULLDOWN_ENABLE,
      .intr_type = GPIO_INTR_DISABLE,
  };
  ESP_ERROR_CHECK(gpio_config(&config));
  ESP_ERROR_CHECK(gpio_set_level(HUMIDIFIER_GPIO, 0));
  s_humidifier_on = false;
  ESP_LOGI(TAG,
           "Humidifier output ready on GPIO%d (active high, initially off)",
           HUMIDIFIER_GPIO);
}

static void load_humidity_settings(void) {
  nvs_handle_t handle;
  if (nvs_open("humidity", NVS_READONLY, &handle) != ESP_OK) {
    return;
  }

  uint8_t auto_mode = 0;
  uint8_t on_below = HUMIDITY_DEFAULT_ON_BELOW;
  uint8_t off_at = HUMIDITY_DEFAULT_OFF_AT;
  nvs_get_u8(handle, "auto", &auto_mode);
  nvs_get_u8(handle, "on_below", &on_below);
  nvs_get_u8(handle, "off_at", &off_at);
  nvs_close(handle);

  if (auto_mode > 1)
    auto_mode = 0;
  if (on_below < HUMIDITY_ON_BELOW_MIN || on_below > HUMIDITY_ON_BELOW_MAX ||
      off_at < HUMIDITY_OFF_AT_MIN || off_at > HUMIDITY_OFF_AT_MAX ||
      off_at - on_below < HUMIDITY_MIN_THRESHOLD_GAP ||
      off_at - on_below > HUMIDITY_MAX_THRESHOLD_GAP) {
    on_below = HUMIDITY_DEFAULT_ON_BELOW;
    off_at = HUMIDITY_DEFAULT_OFF_AT;
  }

  s_humidity_auto_mode = auto_mode == 1;
  s_humidity_on_below = on_below;
  s_humidity_off_at = off_at;
}

esp_err_t bridge_humidity_save_settings(bool auto_mode, uint8_t on_below,
                                        uint8_t off_at) {
  nvs_handle_t handle;
  esp_err_t err = nvs_open("humidity", NVS_READWRITE, &handle);
  if (err != ESP_OK)
    return err;

  err = nvs_set_u8(handle, "auto", auto_mode ? 1 : 0);
  if (err == ESP_OK)
    err = nvs_set_u8(handle, "on_below", on_below);
  if (err == ESP_OK)
    err = nvs_set_u8(handle, "off_at", off_at);
  if (err == ESP_OK)
    err = nvs_commit(handle);
  nvs_close(handle);
  return err;
}

static void dht11_task(void *arg) {
  (void)arg;
  while (true) {
    float humidity = 0.0f;
    float temperature = 0.0f;
    esp_err_t err = dht_read_float_data(DHT_TYPE_DHT11, DHT11_GPIO, &humidity,
                                        &temperature);
    bool valid = err == ESP_OK && isfinite(humidity) && isfinite(temperature) &&
                 humidity >= 0.0f && humidity <= 100.0f &&
                 temperature >= -40.0f && temperature <= 80.0f;

    xSemaphoreTake(s_state_mutex, portMAX_DELAY);
    if (valid) {
      s_humidity_pct = humidity;
      s_temperature_c = temperature;
      s_sensor_last_sample_us = esp_timer_get_time();
      s_sensor_failures = 0;
      s_sensor_ok = true;
    } else {
      if (s_sensor_failures < UINT8_MAX)
        s_sensor_failures++;
      if (s_sensor_failures >= DHT11_FAILURES_BEFORE_INVALID) {
        s_sensor_ok = false;
      }
    }
    bridge_humidity_update_automation_locked();
    xSemaphoreGive(s_state_mutex);

    if (!valid) {
      ESP_LOGW(TAG, "DHT11 read failed (%s)", esp_err_to_name(err));
    } else {
      ESP_LOGI(TAG, "DHT11 temperature=%.1f C humidity=%.1f%%", temperature,
               humidity);
    }
    vTaskDelay(pdMS_TO_TICKS(DHT11_SAMPLE_INTERVAL_MS));
  }
}

void bridge_humidity_init(void) {
  s_state_mutex = xSemaphoreCreateMutex();
  ESP_ERROR_CHECK(s_state_mutex == NULL ? ESP_ERR_NO_MEM : ESP_OK);
  load_humidity_settings();
  ESP_LOGI(TAG, "Humidity automation: %s, on below %u%% off at %u%%",
           s_humidity_auto_mode ? "enabled" : "disabled", s_humidity_on_below,
           s_humidity_off_at);
  ESP_ERROR_CHECK(xTaskCreate(dht11_task, "dht11", 4096, NULL, 5, NULL) ==
                          pdPASS
                      ? ESP_OK
                      : ESP_ERR_NO_MEM);
}
