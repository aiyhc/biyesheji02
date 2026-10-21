#include <stdint.h>

#include "app_state.h"
#include "driver/i2c_master.h"
#include "esp_lcd_io_i2c.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_ssd1306.h"
#include "esp_log.h"

static const char *TAG = "bridge_oled";
static i2c_master_bus_handle_t s_oled_i2c_bus;
static esp_lcd_panel_io_handle_t s_oled_panel_io;
static esp_lcd_panel_handle_t s_oled_panel;
static uint8_t s_oled_blank_frame[OLED_FRAME_BYTES];

/*
 * 初始化 SSD1306 OLED：
 *   1. 建立 I2C 主机总线（软件上拉）；
 *   2. 依次探测 0x3C、0x3D 两个常见地址，找不到就放弃（不影响其他功能）；
 *   3. 创建面板 IO 与 SSD1306 面板，复位、初始化、镜像、开显示；
 *   4. 刷一帧全黑清屏。
 * 任何一步失败都会把 s_oled_panel 置空，此时状态接口上报 oled_ready=false。
 */
void bridge_oled_init(void) {
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
  err =
      esp_lcd_new_panel_ssd1306(s_oled_panel_io, &panel_config, &s_oled_panel);
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
  err = esp_lcd_panel_draw_bitmap(s_oled_panel, 0, 0, OLED_WIDTH, OLED_HEIGHT,
                                  s_oled_blank_frame);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "OLED clear failed: %s", esp_err_to_name(err));
    s_oled_panel = NULL;
    return;
  }
  ESP_LOGI(TAG, "SSD1306 OLED ready on SDA=%d SCL=%d address=0x%02X",
           OLED_SDA_GPIO, OLED_SCL_GPIO, address);
}
bool bridge_oled_is_ready(void) { return s_oled_panel != NULL; }

esp_err_t bridge_oled_draw_frame(const uint8_t *frame) {
  if (s_oled_panel == NULL || frame == NULL)
    return ESP_ERR_INVALID_STATE;
  return esp_lcd_panel_draw_bitmap(s_oled_panel, 0, 0, OLED_WIDTH, OLED_HEIGHT,
                                   frame);
}
