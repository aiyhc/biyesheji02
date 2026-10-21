#include "app_state.h"

EventGroupHandle_t s_wifi_event_group;
bool s_humidifier_on;
SemaphoreHandle_t s_state_mutex;
float s_temperature_c;
float s_humidity_pct;
bool s_sensor_ok;
uint8_t s_sensor_failures;
int64_t s_sensor_last_sample_us;
bool s_humidity_auto_mode;
uint8_t s_humidity_on_below = HUMIDITY_DEFAULT_ON_BELOW;
uint8_t s_humidity_off_at = HUMIDITY_DEFAULT_OFF_AT;
QueueHandle_t s_stepper_queue;
bool s_stepper_busy;
bool s_stepper_stop_requested;
bool s_stepper_toward_open;
uint32_t s_stepper_remaining_steps;
bool s_curtain_open_forward = true;
int32_t s_curtain_position_steps;
uint32_t s_curtain_travel_steps;
bool s_curtain_position_valid;
uint8_t s_curtain_calibration_stage;
