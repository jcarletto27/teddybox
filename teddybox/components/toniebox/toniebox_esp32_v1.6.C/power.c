#include "board.h"
#include "driver/adc.h"
#include "esp_adc_cal.h"

static esp_adc_cal_characteristics_t calibration;
static bool calibrated;

esp_err_t audio_board_power_monitor_init(void)
{
    /* rev 1.6.C: GPIO9/ADC1_CH8 (100k/33k), GPIO8/ADC1_CH7 (100k/100k).
     * https://tonies-wiki.revvox.de/docs/wiki/esp32/pinout/ */
    if (esp_adc_cal_check_efuse(ESP_ADC_CAL_VAL_EFUSE_TP_FIT) != ESP_OK)
        return ESP_ERR_NOT_SUPPORTED;
    esp_err_t ret = adc1_config_width(ADC_WIDTH_BIT_12);
    if (ret != ESP_OK)
        return ret;
    if ((ret = adc1_config_channel_atten(ADC1_CHANNEL_8, ADC_ATTEN_DB_11)) != ESP_OK ||
        (ret = adc1_config_channel_atten(ADC1_CHANNEL_7, ADC_ATTEN_DB_11)) != ESP_OK)
        return ret;
    calibrated = esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12,
                                         0, &calibration) == ESP_ADC_CAL_VAL_EFUSE_TP_FIT;
    return calibrated ? ESP_OK : ESP_ERR_NOT_SUPPORTED;
}

esp_err_t audio_board_power_read(uint32_t *battery_mv, uint32_t *charger_mv)
{
    if (!calibrated)
        return ESP_ERR_INVALID_STATE;
    uint32_t battery_raw = 0, charger_raw = 0;
    for (int sample = 0; sample < 32; ++sample)
    {
        int battery = adc1_get_raw(ADC1_CHANNEL_8);
        int charger = adc1_get_raw(ADC1_CHANNEL_7);
        if (battery <= 0 || battery >= 4095 || charger < 0 || charger >= 4095)
            return ESP_ERR_INVALID_STATE;
        battery_raw += battery;
        charger_raw += charger;
    }
    *battery_mv = (esp_adc_cal_raw_to_voltage(battery_raw / 32, &calibration) * 133 + 16) / 33;
    *charger_mv = esp_adc_cal_raw_to_voltage(charger_raw / 32, &calibration) * 2;
    return ESP_OK;
}
