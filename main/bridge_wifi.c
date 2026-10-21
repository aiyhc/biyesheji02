#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "wifi_secrets.h"

static const char *TAG = "bridge_wifi";

/*
 * Wi-Fi 与 IP 事件回调：
 *   - STA 启动后主动发起连接；
 *   - 断线时清除标志位并立即重连（无限重试）；
 *   - 拿到 IP 后打印访问地址，并置位 WIFI_CONNECTED_BIT 通知主流程。
 */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data) {
  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
    esp_wifi_connect();
  } else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_DISCONNECTED) {
    xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    ESP_LOGW(TAG, "Wi-Fi disconnected; reconnecting");
    esp_wifi_connect();
  } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
    /* 拿到 IP 说明可以对外提供 HTTP 服务了 */
    const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)event_data;
    ESP_LOGI(TAG, "Connected. Open http://" IPSTR "/",
             IP2STR(&event->ip_info.ip));
    xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
  }
}

/*
 * 初始化 Wi-Fi STA 模式：
 *   1. 创建事件组、初始化 netif 与默认事件循环；
 *   2. 注册事件回调；
 *   3. 用 wifi_secrets.h 中的宏填充 SSID/密码并启动。
 * 若宏仍是占位符则直接报错返回，避免无意义的重连刷屏。
 */
void bridge_wifi_init(void) {
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

  /* SSID/密码来源于 wifi_secrets.h，用 snprintf 保证不越界 */
  wifi_config_t wifi_config = {0};
  snprintf((char *)wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid), "%s",
           APP_WIFI_SSID);
  snprintf((char *)wifi_config.sta.password, sizeof(wifi_config.sta.password),
           "%s", APP_WIFI_PASSWORD);
  /* 空密码视为开放网络，否则按 WPA2 处理 */
  wifi_config.sta.threshold.authmode =
      strlen(APP_WIFI_PASSWORD) == 0 ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;

  /* 占位符未替换时给出明确提示并放弃连接 */
  if (strlen(APP_WIFI_SSID) == 0 ||
      strcmp(APP_WIFI_SSID, "YOUR_WIFI_SSID") == 0) {
    ESP_LOGE(TAG, "Set APP_WIFI_SSID in main/wifi_secrets.h before flashing.");
    return;
  }

  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
  ESP_ERROR_CHECK(esp_wifi_start());
}
