

#include <string.h>
#include <sys/stat.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "trf7962a.h"

#include "playback.h"
#include "board.h"
#include "nfc.h"
#include "esp_task_wdt.h"

#define COUNT(x) (sizeof(x) / sizeof(x[0]))

static uint8_t slix_get_rand[] = {0x02, 0xB2, 0x04};
static uint8_t slix_get_inventory[] = {0x26, 0x01, 0x00};
static uint8_t slix_system_info[] = {0x22, 0x2b, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
static uint8_t slix_set_pass[] = {0x02, 0xB3, 0x04, 0x04, 0x00, 0x00, 0x00, 0x00};

static const uint32_t passes[] = {0x0F0F0F0F, 0x7FFD6E5B, 0x00000000};
static uint8_t received_data[256];
static uint8_t received_length;

static char dump_buf[129];

bool nfc_valid = false;
uint8_t nfc_current_uid_rev[8];
uint8_t nfc_current_token[32];
static bool nfc_token_valid;
static int nfc_retry = 0;

static const char *TAG = "[NFC]";
static bool task_started;
static TaskHandle_t shutdown_waiter;

typedef enum
{
    STATE_SEARCHING,
    STATE_TAG,
    STATE_SYSINFO
} nfc_state_t;

uint64_t nfc_get_current_uid()
{
    if (!nfc_valid)
    {
        return NFC_UID_INVALID;
    }
    uint64_t uid = ((uint64_t)nfc_current_uid_rev[7] << 56) |
                   ((uint64_t)nfc_current_uid_rev[6] << 48) |
                   ((uint64_t)nfc_current_uid_rev[5] << 40) |
                   ((uint64_t)nfc_current_uid_rev[4] << 32) |
                   ((uint64_t)nfc_current_uid_rev[3] << 24) |
                   ((uint64_t)nfc_current_uid_rev[2] << 16) |
                   ((uint64_t)nfc_current_uid_rev[1] << 8) |
                   ((uint64_t)nfc_current_uid_rev[0]);

    return uid;
}

uint8_t *nfc_get_current_token()
{
    if (!nfc_valid || !nfc_token_valid)
    {
        return NULL;
    }
    return nfc_current_token;
}

void nfc_dump(char *buffer, uint8_t *data, uint8_t length)
{
    int pos = 0;

    buffer[pos] = 0;

    for (int pos = 0; pos < length; pos++)
    {
        sprintf(&buffer[pos * 3], "%02X ", data[pos]);
    }
}

void nfc_log_dump(const char *title, uint8_t *data, uint8_t length)
{
    nfc_dump(dump_buf, data, length);
    ESP_LOGD(TAG, "%s: %s", title, dump_buf);
}

esp_err_t nfc_get_rand(trf7962a_t trf, uint8_t *rand)
{
    if (trf7962a_xmit(trf, slix_get_rand, sizeof(slix_get_rand), received_data, &received_length) != ESP_OK)
    {
        return ESP_FAIL;
    }
    nfc_log_dump("GET RANDOM", received_data, received_length);
    if (received_length != 5 || received_data[0] != 0)
    {
        ESP_LOGE(TAG, "received RAND with %d bytes, status %d", received_length, received_data[0]);
        return ESP_FAIL;
    }

    rand[0] = received_data[1];
    rand[1] = received_data[2];

    return ESP_OK;
}

/* ISO15693 addressed READ SINGLE BLOCK. Token occupies blocks 0 through 7.
 * The reader retains the two CRC bytes, as with inventory/random responses. */
static esp_err_t nfc_read_token(trf7962a_t trf, const uint8_t uid[8], uint8_t token[32])
{
    uint8_t command[11] = {0x22, 0x20};
    uint8_t candidate[32];
    memcpy(command + 2, uid, 8);
    for (uint8_t block = 0; block < 8; ++block)
    {
        command[10] = block;
        if (trf7962a_xmit(trf, command, sizeof(command), received_data, &received_length) != ESP_OK ||
            received_length != 7 || received_data[0] != 0)
        {
            ESP_LOGW(TAG, "Token read failed at block %u; retrying after inventory", block);
            return ESP_FAIL;
        }
        memcpy(candidate + block * 4, received_data + 1, 4);
    }
    /* Never publish a partial read or mix bytes from two different tags. */
    memcpy(token, candidate, sizeof(candidate));
    return ESP_OK;
}

esp_err_t nfc_reset(trf7962a_t trf)
{
    ESP_LOGD(TAG, "NFC Reset");
    if (trf7962a_reset(trf) != ESP_OK)
    {
        return ESP_FAIL;
    }
    trf7962a_field(trf, false);
    vTaskDelay(10 / portTICK_PERIOD_MS);
    trf7962a_field(trf, true);
    vTaskDelay(10 / portTICK_PERIOD_MS);
    return ESP_OK;
}

/* when token was detected, try to start playback using UID */
static void nfc_play()
{
    pb_play_content(nfc_get_current_uid());
}

/* later, when memory was read, call pb handler so it will be able to download the file */
static void nfc_play_token()
{
    pb_play_content_token(nfc_get_current_uid(), nfc_get_current_token());
}

static void nfc_stop()
{
    pb_stop();
}

static void nfc_handle_tag(trf7962a_t trf, const uint8_t uid[8])
{
    if (!nfc_valid || memcmp(nfc_current_uid_rev, uid, 8))
    {
        nfc_token_valid = false;
        memset(nfc_current_token, 0, sizeof(nfc_current_token));
        memcpy(nfc_current_uid_rev, uid, 8);
        nfc_valid = true;
        ESP_LOGI(TAG, "Tag entered: %llX", nfc_get_current_uid());
        nfc_play();
    }
    if (!nfc_token_valid &&
        nfc_read_token(trf, nfc_current_uid_rev, nfc_current_token) == ESP_OK)
    {
        nfc_token_valid = true;
        ESP_LOGI(TAG, "Tag token read successfully");
        nfc_play_token();
    }
}

void nfc_mainthread(void *arg)
{
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    nfc_state_t state = STATE_SEARCHING;
    trf7962a_t trf = (trf7962a_t)arg;

    while (!__atomic_load_n(&shutdown_waiter, __ATOMIC_ACQUIRE))
    {
        ESP_ERROR_CHECK(esp_task_wdt_reset());
        vTaskDelay(10 / portTICK_PERIOD_MS);

        switch (state)
        {
        case STATE_SYSINFO:
        {
            if (trf7962a_xmit(trf, slix_system_info, sizeof(slix_system_info), received_data, &received_length) != ESP_OK)
            {
                nfc_reset(trf);
                ESP_LOGE(TAG, "Failed to read SYSINFO");
                if (nfc_retry++ > NFC_RETRIES)
                {
                    state = STATE_SEARCHING;
                }
                break;
            }
            nfc_log_dump("SYSINFO", received_data, received_length);
            break;
        }

        case STATE_TAG:
        {
            if (trf7962a_xmit(trf, slix_get_inventory, sizeof(slix_get_inventory), received_data, &received_length) != ESP_OK)
            {
                nfc_reset(trf);
                // ESP_LOGE(TAG, "Failed to read INVENTORY");
                if (nfc_retry++ > NFC_RETRIES)
                {
                    state = STATE_SEARCHING;
                }
                break;
            }
            nfc_log_dump("INVENTORY", received_data, received_length);
            if (received_length != 12 || received_data[0] != 0)
            {
                nfc_reset(trf);
                // ESP_LOGE(TAG, "received INVENTORY with %d bytes, status %d", received_length, received_data[0]);
                if (nfc_retry++ > NFC_RETRIES)
                {
                    state = STATE_SEARCHING;
                }
                break;
            }

            nfc_retry = 0;

            nfc_dump(dump_buf, &received_data[2], 8);

            nfc_handle_tag(trf, &received_data[2]);
            vTaskDelay(250 / portTICK_PERIOD_MS);
            break;
        }

        case STATE_SEARCHING:
        {
            uint8_t rand[2];

            if (nfc_valid)
            {
                nfc_valid = false;
                nfc_token_valid = false;
                memset(nfc_current_token, 0, sizeof(nfc_current_token));
                nfc_dump(dump_buf, nfc_current_uid_rev, 8);
                ESP_LOGI(TAG, "Tag disappeared: %s", dump_buf);
                nfc_stop();
            }

            if (nfc_get_rand(trf, rand) != ESP_OK)
            {
                nfc_reset(trf);
                break;
            }
            if (trf7962a_xmit(trf, slix_get_inventory, sizeof(slix_get_inventory), received_data, &received_length) == ESP_OK)
            {
                ESP_LOGI(TAG, "Unlocked tag found");
                state = STATE_TAG;
                break;
            }
            ESP_LOGI(TAG, "Locked tag detected");

            bool unlocked = false;

            for (int pass = 0; pass < COUNT(passes); pass++)
            {
                ESP_LOGI(TAG, "Test pass 0x%08X", passes[pass]);

                nfc_reset(trf);

                if (nfc_get_rand(trf, rand) != ESP_OK)
                {
                    ESP_LOGE(TAG, "GET RANDOM failed unexpectedly");
                    continue;
                }

                ESP_LOGD(TAG, "  RAND %02X %02X", rand[0], rand[1]);

                slix_set_pass[4] = (passes[pass] >> 0) ^ rand[0];
                slix_set_pass[5] = (passes[pass] >> 8) ^ rand[1];
                slix_set_pass[6] = (passes[pass] >> 16) ^ rand[0];
                slix_set_pass[7] = (passes[pass] >> 24) ^ rand[1];

                nfc_log_dump("SET PASS", slix_set_pass, sizeof(slix_set_pass));
                if (trf7962a_xmit(trf, slix_set_pass, sizeof(slix_set_pass), received_data, &received_length) != ESP_OK)
                {
                    nfc_reset(trf);
                    ESP_LOGE(TAG, "  Password incorrect");
                    continue;
                }
                nfc_log_dump("  SET PASS", received_data, received_length);
                if (received_length != 3 || received_data[0] != 0)
                {
                    nfc_reset(trf);
                    ESP_LOGE(TAG, "  Password incorrect - %d bytes, status %d", received_length, received_data[0]);
                    continue;
                }
                unlocked = true;
                break;
            }

            if (unlocked)
            {
                ESP_LOGI(TAG, "Unlocked tag");
                nfc_retry = 0;
                state = STATE_TAG;
            }

            break;
        }
        }
    }
    ESP_ERROR_CHECK(esp_task_wdt_delete(NULL));
    xTaskNotifyGive(__atomic_load_n(&shutdown_waiter, __ATOMIC_ACQUIRE));
    vTaskDelete(NULL);
}

esp_err_t nfc_shutdown(void)
{
    if (!task_started)
        return ESP_OK;
    __atomic_store_n(&shutdown_waiter, xTaskGetCurrentTaskHandle(), __ATOMIC_RELEASE);
    if (!ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(3000)))
        return ESP_ERR_TIMEOUT;
    task_started = false;
    return ESP_OK;
}

void nfc_init()
{
    esp_log_level_set(TAG, ESP_LOG_INFO);
    trf7962a_t trf = audio_board_get_trf();

    if (nfc_reset(trf) != ESP_OK)
    {
        ESP_LOGE(TAG, "NFC chip not detected. Exiting.");
        return;
    }

    ESP_ERROR_CHECK(xTaskCreatePinnedToCore(nfc_mainthread, "[TB] NFC", 6000, trf,
                                          NFC_TASK_PRIO, NULL, tskNO_AFFINITY) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    task_started = true;
}
