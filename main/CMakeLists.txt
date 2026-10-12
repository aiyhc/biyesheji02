idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES
        json
        esp_driver_i2c
        esp_event
        esp_http_server
        esp_lcd
        esp_netif
        esp_wifi
        led_strip
        nvs_flash
)
