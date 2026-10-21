#ifndef APP_STATE_H
#define APP_STATE_H

#include <stdbool.h>
#include <stdint.h>

#include "app_config.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#define WIFI_CONNECTED_BIT (1U << 0)

typedef struct {
  bool forward;
  bool toward_open;
  uint32_t steps;
  uint16_t interval_ms;
} stepper_command_t;

/* Shared runtime state. Access sensor, humidifier, and curtain fields under
 * s_state_mutex. */
extern EventGroupHandle_t s_wifi_event_group;
extern bool s_humidifier_on;
extern SemaphoreHandle_t s_state_mutex;
extern float s_temperature_c;
extern float s_humidity_pct;
extern bool s_sensor_ok;
extern uint8_t s_sensor_failures;
extern int64_t s_sensor_last_sample_us;
extern bool s_humidity_auto_mode;
extern uint8_t s_humidity_on_below;
extern uint8_t s_humidity_off_at;
extern QueueHandle_t s_stepper_queue;
extern bool s_stepper_busy;
extern bool s_stepper_stop_requested;
extern bool s_stepper_toward_open;
extern uint32_t s_stepper_remaining_steps;
extern bool s_curtain_open_forward;
extern int32_t s_curtain_position_steps;
extern uint32_t s_curtain_travel_steps;
extern bool s_curtain_position_valid;
extern uint8_t s_curtain_calibration_stage;

void bridge_nvs_init(void);
void bridge_wifi_init(void);
void bridge_led_init(void);
esp_err_t bridge_led_set_rgb(uint8_t red, uint8_t green, uint8_t blue);
void bridge_led_get_rgb(uint8_t *red, uint8_t *green, uint8_t *blue);
void bridge_oled_init(void);
bool bridge_oled_is_ready(void);
esp_err_t bridge_oled_draw_frame(const uint8_t *frame);
void bridge_humidifier_init(void);
void bridge_humidity_init(void);
esp_err_t bridge_humidifier_set_output_locked(bool on);
void bridge_humidity_update_automation_locked(void);
esp_err_t bridge_humidity_save_settings(bool auto_mode, uint8_t on_below,
                                        uint8_t off_at);
void bridge_curtain_init(void);
esp_err_t bridge_curtain_save_state_locked(void);
esp_err_t bridge_http_start(void);

#endif
