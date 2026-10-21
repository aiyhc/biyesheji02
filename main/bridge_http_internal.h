#ifndef BRIDGE_HTTP_INTERNAL_H
#define BRIDGE_HTTP_INTERNAL_H

#include <stdbool.h>

#include "cJSON.h"
#include "esp_err.h"
#include "esp_http_server.h"

#define MAX_ACTION_BODY 512

esp_err_t bridge_http_send_json(httpd_req_t *req, cJSON *root);
esp_err_t bridge_http_send_error(httpd_req_t *req, int status,
                                 const char *message);
bool bridge_http_read_integer_range(const cJSON *root, const char *key,
                                    int minimum, int maximum, int *value);
bool bridge_http_read_rgb_value(const cJSON *root, const char *key, int *value);
cJSON *bridge_http_create_curtain_status(void);
esp_err_t bridge_action_handler(httpd_req_t *req);

#endif
