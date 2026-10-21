#include "app_state.h"
#include "esp_log.h"
#include "led_strip.h"
#include "led_strip_rmt.h"

static led_strip_handle_t s_rgb_strip;
static uint8_t s_rgb_r;
static uint8_t s_rgb_g;
static uint8_t s_rgb_b;

/*
 * 初始化板载 RGB 灯：
 *   使用 RMT 外设驱动单颗 WS2812（GRB 顺序），初始化为熄灭状态。
 *   分辨率 10MHz 是 WS2812 时序的常规取值；单灯不需要 DMA。
 */
void bridge_led_init(void) {
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

  ESP_ERROR_CHECK(
      led_strip_new_rmt_device(&strip_config, &rmt_config, &s_rgb_strip));
  ESP_ERROR_CHECK(led_strip_clear(s_rgb_strip)); /* 上电先熄灭，避免乱闪 */
}
esp_err_t bridge_led_set_rgb(uint8_t red, uint8_t green, uint8_t blue) {
  if (s_rgb_strip == NULL)
    return ESP_ERR_INVALID_STATE;

  uint8_t scaled_red = (uint8_t)(red * RGB_BRIGHTNESS_LIMIT / 255);
  uint8_t scaled_green = (uint8_t)(green * RGB_BRIGHTNESS_LIMIT / 255);
  uint8_t scaled_blue = (uint8_t)(blue * RGB_BRIGHTNESS_LIMIT / 255);
  esp_err_t err = led_strip_set_pixel(s_rgb_strip, 0, scaled_red, scaled_green,
                                      scaled_blue);
  if (err == ESP_OK)
    err = led_strip_refresh(s_rgb_strip);
  if (err == ESP_OK) {
    s_rgb_r = red;
    s_rgb_g = green;
    s_rgb_b = blue;
  }
  return err;
}

void bridge_led_get_rgb(uint8_t *red, uint8_t *green, uint8_t *blue) {
  if (red != NULL)
    *red = s_rgb_r;
  if (green != NULL)
    *green = s_rgb_g;
  if (blue != NULL)
    *blue = s_rgb_b;
}
