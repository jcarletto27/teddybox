#include <stdint.h>
#include "board.h"
#include "esp_log.h"
#include "battery.h"

static const char *TAG = "Battery";
static bool enabled, warned;
static unsigned low_samples, critical_samples;

void battery_init(void)
{
    esp_err_t ret = audio_board_power_monitor_init();
    enabled = ret == ESP_OK;
    esp_log_level_set(TAG, ESP_LOG_INFO);
    if (!enabled)
        ESP_LOGE(TAG, "Calibrated voltage monitoring unavailable: %s", esp_err_to_name(ret));
}

bool battery_poll(void)
{
    if (!enabled)
        return false;
    uint32_t battery_mv, charger_mv;
    if (audio_board_power_read(&battery_mv, &charger_mv) != ESP_OK ||
        battery_mv < 1000 || battery_mv > 6000)
    {
        low_samples = critical_samples = 0;
        ESP_LOGW(TAG, "Invalid ADC reading; not making a shutdown decision");
        return false;
    }
    /* Original three-cell NiMH pack: warn 3.6 V, stop 3.3 V. Do not use Li-ion
     * charge percentages. Require five consecutive seconds below threshold. */
    bool docked = charger_mv >= 4000;
    low_samples = !docked && battery_mv <= 3600 ? low_samples + 1 : 0;
    critical_samples = !docked && battery_mv <= 3300 ? critical_samples + 1 : 0;
    if (docked || battery_mv >= 3700)
        warned = false;
    if (low_samples == 5 && !warned)
    {
        warned = true;
        ESP_LOGW(TAG, "Low battery: %u mV; place box on charger", battery_mv);
    }
    static unsigned log_counter;
    if (log_counter++ % 30 == 0)
        ESP_LOGI(TAG, "Battery %u mV; charger rail %u mV (%s)",
                 battery_mv, charger_mv, docked ? "docked" : "battery");
    if (critical_samples >= 5)
    {
        ESP_LOGE(TAG, "Battery %u mV below cutoff for 5 seconds; shutting down", battery_mv);
        return true;
    }
    return false;
}
