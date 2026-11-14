/* Play MP3 file from SD Card

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "soc/rtc_cntl_reg.h"
#include "driver/rtc_io.h"
#include "soc/rtc.h"

#include "esp_log.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "esp_vfs.h"
#include "esp_vfs_fat.h"
#include "wear_levelling.h"
#include "esp_partition.h"
#include "esp_event.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_core_dump.h"
#include "battery.h"

#include "esp_peripherals.h"
#include "periph_sdcard.h"
#include "board.h"
#include "playback.h"
#include "wifi.h"
#include "webserver.h"
#include "accel.h"
#include "dac3100.h"
#include "ota.h"
#include "nfc.h"
#include "cloud.h"
#include "ledman.h"

#include "config.h"

#include "esp_heap_trace.h"
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"

static const char *TAG = "[TB]";
static wl_handle_t s_test_wl_handle;

typedef struct
{
    uint32_t rtc_magic;
    uint32_t volume;
    uint64_t nfc_uid;
    uint32_t play_position;
    uint32_t rtc_check;
} rtc_mem_t;

static RTC_DATA_ATTR rtc_mem_t rtc_storage;

void dir_list(const char *path)
{
    DIR *dir = opendir(path);
    while (true)
    {
        struct dirent *de = readdir(dir);
        if (!de)
        {
            break;
        }
        ESP_LOGI(TAG, "  - %s", de->d_name);
    }
    closedir(dir);
}

void print_all_tasks(void *params)
{
    TaskStatus_t *taskStatusArray;
    UBaseType_t taskCount;
    UBaseType_t i;
    size_t free_heap_size;

    while (1)
    {
        taskCount = uxTaskGetNumberOfTasks();
        taskStatusArray = pvPortMalloc(taskCount * sizeof(TaskStatus_t));

        if (taskStatusArray != NULL)
        {
            taskCount = uxTaskGetSystemState(taskStatusArray, taskCount, NULL);

            free_heap_size = esp_get_free_heap_size();
            ESP_LOGI(TAG, "Task Count:     %d", taskCount);
            ESP_LOGI(TAG, "Free Heap Size: %d", free_heap_size);
            ESP_LOGI(TAG, "");
            ESP_LOGI(TAG, "StackMark Name");

            for (i = 0; i < taskCount; i++)
            {
                ESP_LOGI(TAG, "%6d %s",
                         uxTaskGetStackHighWaterMark(taskStatusArray[i].xHandle),
                         taskStatusArray[i].pcTaskName);
            }

            vPortFree(taskStatusArray);
        }

        vTaskDelay(pdMS_TO_TICKS(5000)); // Wait for 5 seconds
    }
}

static uint32_t rotl32a(uint32_t x, uint32_t n)
{
    return (x << n) | (x >> (32 - n));
}

static uint32_t rtc_checksum_calc()
{
    uint8_t *buf = (uint8_t *)&rtc_storage;
    uint32_t len = sizeof(rtc_storage) - sizeof(uint32_t);
    uint32_t chk = 0x55AA5A5A;

    for (int pos = 0; pos < len; pos++)
    {
        chk ^= buf[pos];
        chk = rotl32a(chk, 3);
        chk += buf[pos];
        chk = rotl32a(chk, 7);
    }
    return chk;
}

static void rtc_checksum_update()
{
    rtc_storage.rtc_check = rtc_checksum_calc();
}

static bool rtc_checksum_valid()
{
    return rtc_storage.rtc_check == rtc_checksum_calc();
}

/* Keep emergency recovery independent of playback, filesystem and I2C locks. */
static void ear_restart_task(void *ctx)
{
    bool armed = false;
    int64_t held_since = -1;
    while (true)
    {
        bool big = audio_board_ear_big();
        bool small = audio_board_ear_small();
        if (!big && !small)
            armed = true; /* Require release after boot to avoid a restart loop. */
        if (!armed || !big || !small)
            held_since = -1;
        else
        {
            int64_t now = esp_timer_get_time();
            if (held_since < 0)
                held_since = now;
            if (now - held_since >= 5000000)
            {
                ESP_LOGW(TAG, "Both ears held for 5 seconds; emergency restart");
                esp_restart();
            }
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

static void allocation_failed(size_t size, uint32_t caps, const char *function)
{
    /* The allocation has already failed; ROM output avoids allocating a log buffer. */
    esp_rom_printf("Allocation failed: %u bytes, caps=0x%x, free=%u, largest=%u (%s)\n",
                   (unsigned)size, (unsigned)caps,
                   (unsigned)heap_caps_get_free_size(caps),
                   (unsigned)heap_caps_get_largest_free_block(caps), function);
}

void app_main(void)
{
    ESP_ERROR_CHECK(heap_caps_register_failed_alloc_callback(allocation_failed));
        /* Initialize NVS — it is used to store PHY calibration data */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    esp_log_level_set("*", ESP_LOG_WARN);
    esp_log_level_set(TAG, ESP_LOG_INFO);
    ESP_LOGI(TAG, "Reset reason: %d; sleep wakeup cause: %d",
             esp_reset_reason(), esp_sleep_get_wakeup_cause());

    esp_periph_config_t periph_cfg = DEFAULT_ESP_PERIPH_SET_CONFIG();
    esp_periph_set_handle_t set = esp_periph_set_init(&periph_cfg);

    /* Monitor application progress, rather than only idle-task scheduling. */
    ESP_ERROR_CHECK(esp_task_wdt_init(30, true));
#ifdef CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH
    size_t dump_address, dump_size;
    if (esp_core_dump_image_get(&dump_address, &dump_size) == ESP_OK)
        ESP_LOGW(TAG, "Saved crash dump at 0x%x (%u bytes); retrieve with espcoredump.py info_corefile (see RECOVERY.md)",
                 (unsigned)dump_address, (unsigned)dump_size);
#endif
    ESP_LOGI(TAG, "Board init");
    audio_board_handle_t board_handle = audio_board_init();
    if (xTaskCreate(ear_restart_task, "[TB] ear reset", 2048, NULL, 6, NULL) != pdPASS)
        ESP_LOGE(TAG, "Failed to start ear-button restart handler");
    ledman_init();
    battery_init();

    ESP_LOGI(TAG, "Mount sdcard");
    audio_board_sdcard_init(set, SD_MODE_4_LINE);

    ESP_LOGI(TAG, "Mount assets");
    esp_vfs_fat_sdmmc_mount_config_t mount_config = {
        .format_if_mount_failed = false,
        .max_files = 3};
    esp_vfs_fat_spiflash_mount("/spiflash", NULL, &mount_config, &s_test_wl_handle);

    ESP_LOGI(TAG, "Start codec chip");
    audio_hal_ctrl_codec(audio_board_get_hal(), AUDIO_HAL_CODEC_MODE_DECODE, AUDIO_HAL_CTRL_START);

    ESP_LOGI(TAG, "Start handlers");

    pb_init(set);

    // xTaskCreate(print_all_tasks, "print_all_tasks", 4096, NULL, 5, NULL);

    if (rtc_storage.rtc_magic != 0xDEADC0DE || !rtc_checksum_valid())
    {
        ESP_LOGE(TAG, "RTC memory corrupted, reinit");
        memset(&rtc_storage, 0x00, sizeof(rtc_storage));
        rtc_storage.rtc_magic = 0xDEADC0DE;
        rtc_storage.volume = 50;
        rtc_storage.play_position = 0;
        rtc_checksum_update();
    }
    else if (rtc_storage.nfc_uid)
    {
        ESP_LOGI(TAG, "Inform playback handler UID %16llX / %d", rtc_storage.nfc_uid, rtc_storage.play_position);
        pb_set_last(rtc_storage.nfc_uid, rtc_storage.play_position);
    }

    audio_hal_set_volume(audio_board_get_hal(), rtc_storage.volume);

    dac3100_set_mute(true);

    pb_play_default(CONTENT_DEFAULT_STARTUP);

    bool ear_big_prev = false;
    bool ear_small_prev = false;

    accel_init(board_handle);
    wifi_init();
    cloud_init();
    nfc_init();
    /* already too much memory consumption, do not enable by default */
    // www_init();
    ota_init();

    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    int64_t last_battery_check = 0;
    int64_t last_activity_time = esp_timer_get_time();
    int64_t last_idle_report = last_activity_time;
    int64_t remute_time = 0;
    
    while (1)
    {
        ESP_ERROR_CHECK(esp_task_wdt_reset());
        vTaskDelay(pdMS_TO_TICKS(100));
        int64_t cur_time = esp_timer_get_time();

        if (cur_time - last_battery_check >= 1000000)
        {
            last_battery_check = cur_time;
            if (battery_poll())
                break;
        }
        bool ear_big = audio_board_ear_big();
        bool ear_small = audio_board_ear_small();

        if (ear_big && !ear_small && !ear_big_prev)
        {
            dac3100_set_mute(false);
            if (rtc_storage.volume < 100)
            {
                ESP_LOGI(TAG, "Volume up");
                rtc_storage.volume += 10;
                audio_hal_set_volume(audio_board_get_hal(), rtc_storage.volume);
                dac3100_beep(0, 0x140);
            }
            else
            {
                ESP_LOGI(TAG, "Volume up (limit)");
                dac3100_beep(1, 0x140);
            }
            remute_time = cur_time + 800000;
        }

        if (ear_small && !ear_big && !ear_small_prev)
        {
            dac3100_set_mute(false);
            if (rtc_storage.volume >= 10)
            {
                ESP_LOGI(TAG, "Volume down");
                rtc_storage.volume -= 10;
                audio_hal_set_volume(audio_board_get_hal(), rtc_storage.volume);
                dac3100_beep(2, 0x140);
            }
            else
            {
                ESP_LOGI(TAG, "Volume down (limit)");
                dac3100_beep(3, 0x140);
            }
            remute_time = cur_time + 800000;
        }

        if (remute_time != 0 && cur_time > remute_time)
        {
            remute_time = 0;
            dac3100_set_mute(!pb_is_playing());
        }

        bool playback_busy = pb_is_busy();
        /* A held input is not repeated user activity. In particular, a stuck
         * ear must not prevent automatic sleep indefinitely. */
        if (playback_busy || (ear_big && !ear_big_prev) || (ear_small && !ear_small_prev))
        {
            last_activity_time = cur_time;
        }

        if (cur_time - last_idle_report >= 30000000)
        {
            last_idle_report = cur_time;
            ESP_LOGI(TAG, "Sleep status: idle=%lld s, timeout=%lld s, playback/download busy=%d, ears=%d/%d",
                     (long long)((cur_time - last_activity_time) / 1000000),
                     (long long)(POWEROFF_TIMEOUT / 1000000),
                     playback_busy, ear_big, ear_small);
        }

        if (pb_is_playing())
        {
            rtc_storage.nfc_uid = pb_get_current_uid();
            rtc_storage.play_position = pb_get_play_position();
        }

        if ((cur_time - last_activity_time) > POWEROFF_TIMEOUT)
        {
            ESP_LOGI(TAG, "Idle poweroff after %lld seconds without playback or ear-button activity",
                     (long long)((cur_time - last_activity_time) / 1000000));
            break;
        }

        ear_big_prev = ear_big;
        ear_small_prev = ear_small;
    }

    ledman_change("poweroff");
    ESP_LOGI(TAG, "Stopping NFC and accelerometer before sleep");
    ESP_ERROR_CHECK(nfc_shutdown());
    ESP_ERROR_CHECK(accel_shutdown());
    ESP_ERROR_CHECK(pb_shutdown());
    int64_t stop_started = esp_timer_get_time();
    while (cloud_is_busy() && esp_timer_get_time() - stop_started < 12000000)
        vTaskDelay(pdMS_TO_TICKS(100));
    if (cloud_is_busy())
    {
        ESP_LOGE(TAG, "Shutdown could not quiesce cloud writer; preserving crash dump");
        abort();
    }
    ESP_ERROR_CHECK(wifi_shutdown());
    ESP_ERROR_CHECK(audio_board_sdcard_unmount());
    rtc_checksum_update();

    ESP_LOGI(TAG, "Poweroff");
    vTaskDelay(500 / portTICK_PERIOD_MS);
    ESP_ERROR_CHECK(ledman_shutdown());
    audio_board_poweroff();

    ESP_LOGE(TAG, "back, quite unexpected...");
}
