#include <stdbool.h>

#include "app_state.h"
#include "esp_log.h"
#include "freertos/task.h"
#include "nvs_flash.h"

/*
 * 初始化 NVS（非易失存储）。
 * Wi-Fi 驱动内部依赖 NVS；若分区损坏或版本不符则擦除后重建。
 */
void bridge_nvs_init(void) {
  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
      err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    err = nvs_flash_init();
  }
  ESP_ERROR_CHECK(err);
}

/*
 * 固件入口。
 * 顺序：雾化器安全关闭 -> NVS -> RGB 灯 -> OLED -> Wi-Fi；
 * 阻塞等待拿到 IP 后才启动 HTTP 服务器，最后停在空循环里保持任务存活。
 */
void app_main(void) {
  bridge_humidifier_init();
  bridge_nvs_init();
  bridge_humidity_init();
  bridge_curtain_init();
  bridge_led_init();
  bridge_oled_init();
  bridge_wifi_init();

  /* 无限等待，直到 Wi-Fi 连接成功（事件回调里置位） */
  xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE,
                      portMAX_DELAY);
  ESP_ERROR_CHECK(bridge_http_start());

  /* HTTP 服务器已在独立任务中运行，这里保持 app_main 不退出即可 */
  while (true) {
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}
