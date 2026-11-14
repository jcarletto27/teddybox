/* Standalone sleep-entry probe for Toniebox ESP32 board revision 1.6.C.
 * No application components, filesystem access, or wireless initialization.
 */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_system.h"

#define POWER_GPIO GPIO_NUM_45
#define SD_POWER_GPIO GPIO_NUM_47
#define LED_MASK (BIT64(GPIO_NUM_17) | BIT64(GPIO_NUM_18) | BIT64(GPIO_NUM_19))

static const char *TAG = "SLEEP-PROBE";
static RTC_DATA_ATTR unsigned phase;

void app_main(void)
{
    esp_reset_reason_t reset = esp_reset_reason();
    esp_sleep_wakeup_cause_t wake = esp_sleep_get_wakeup_cause();
    ESP_LOGI(TAG, "Minimal probe: reset=%d, wake=%d, retained phase=%u", reset, wake, phase);
    if (reset != ESP_RST_DEEPSLEEP || wake != ESP_SLEEP_WAKEUP_TIMER)
        phase = 0;
    else
        ESP_LOGI(TAG, "PASS: timer sleep with peripheral rails %s", phase == 1 ? "ON" : "OFF");

    /* Alternate two tests. A cold reset restarts with rails ON, so a failure
     * after the ON test passes can be distinguished from a baseline failure. */
    bool rails_on = phase != 1;
    gpio_config_t outputs = {
        .pin_bit_mask = BIT64(POWER_GPIO) | BIT64(SD_POWER_GPIO) | LED_MASK,
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_ERROR_CHECK(gpio_config(&outputs));
    ESP_ERROR_CHECK(gpio_set_level(POWER_GPIO, rails_on));
    ESP_ERROR_CHECK(gpio_set_level(SD_POWER_GPIO, !rails_on));
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_17, 0));
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_18, 0));
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_19, 0));
    ESP_ERROR_CHECK(gpio_hold_dis(POWER_GPIO));
    ESP_ERROR_CHECK(gpio_hold_dis(SD_POWER_GPIO));
    gpio_deep_sleep_hold_dis();
    ESP_LOGI(TAG, "Peripheral rails %s; settling for 2 seconds", rails_on ? "ON" : "OFF");
    vTaskDelay(pdMS_TO_TICKS(2000));

    ESP_ERROR_CHECK(gpio_hold_en(POWER_GPIO));
    ESP_ERROR_CHECK(gpio_hold_en(SD_POWER_GPIO));
    gpio_deep_sleep_hold_en();
    ESP_ERROR_CHECK(esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON));
    ESP_ERROR_CHECK(esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL));
    ESP_ERROR_CHECK(esp_sleep_enable_timer_wakeup(10000000));
    phase = rails_on ? 1 : 2;
    ESP_LOGW(TAG, "Entering 10-second timer-only deep sleep; peripheral rails %s", rails_on ? "ON" : "OFF");
    esp_deep_sleep_start();
}
