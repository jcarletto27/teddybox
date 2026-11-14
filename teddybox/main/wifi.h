
#pragma once

#include <stdbool.h>
#include "esp_err.h"

void wifi_init(void);
/* Stop event-driven reconnects and the radio before entering sleep. */
esp_err_t wifi_shutdown(void);
void wifi_save_nvs(void);
void wifi_load_nvs(void);
bool wifi_is_connected(void);
