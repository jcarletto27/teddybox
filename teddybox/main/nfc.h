#pragma once

#include <stdint.h>
#include "esp_err.h"

#define NFC_TASK_PRIO 5
#define NFC_UID_INVALID 0

#define NFC_RETRIES 5

void nfc_init();
esp_err_t nfc_shutdown(void);
uint64_t nfc_get_current_uid();
