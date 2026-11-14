"""Exercise production voltage conversion and battery policy with simulated ADCs."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
power = (root / 'components/toniebox/toniebox_esp32_v1.6.C/power.c').read_text()
power = '\n'.join(line for line in power.splitlines() if not line.startswith('#include'))
battery = (root / 'main/battery.c').read_text()
battery = '\n'.join(line for line in battery.splitlines() if not line.startswith('#include'))
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef int esp_err_t;
typedef int esp_adc_cal_characteristics_t;
#define ESP_OK 0
#define ESP_ERR_NOT_SUPPORTED -1
#define ESP_ERR_INVALID_STATE -2
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOG_INFO 1
#define esp_log_level_set(...) ((void)0)
#define ADC_WIDTH_BIT_12 12
#define ADC1_CHANNEL_8 8
#define ADC1_CHANNEL_7 7
#define ADC_ATTEN_DB_11 11
#define ADC_UNIT_1 1
#define ESP_ADC_CAL_VAL_EFUSE_TP_FIT 3
static int raw_battery = 800, raw_charger, calibration_error;
static int esp_adc_cal_check_efuse(int mode) { assert(mode == 3); return calibration_error; }
static int adc1_config_width(int width) { assert(width == 12); return ESP_OK; }
static int adc1_config_channel_atten(int channel, int atten) {
    assert((channel == 7 || channel == 8) && atten == 11); return ESP_OK;
}
static int esp_adc_cal_characterize(int unit, int atten, int width, int ref, void *out) {
    assert(unit == 1 && atten == 11 && width == 12); return 3;
}
static int adc1_get_raw(int channel) { return channel == 8 ? raw_battery : raw_charger; }
static uint32_t esp_adc_cal_raw_to_voltage(int raw, const void *cal) { return raw; }
'''
harness = r'''
int main(void) {
    uint32_t pack, rail;
    assert(audio_board_power_read(&pack, &rail) == ESP_ERR_INVALID_STATE);
    calibration_error = -1;
    battery_init(); assert(!enabled && !battery_poll());
    calibration_error = 0; battery_init(); assert(enabled);
    raw_battery = 800; raw_charger = 2500;
    assert(audio_board_power_read(&pack, &rail) == ESP_OK);
    assert(pack == 3224 && rail == 5000);
    for (int i = 0; i < 10; ++i) assert(!battery_poll()); /* Dock inhibits cutoff. */
    raw_charger = 0;
    for (int i = 0; i < 4; ++i) assert(!battery_poll());
    raw_battery = 0; assert(!battery_poll()); /* Invalid measurement resets history. */
    raw_battery = 800;
    for (int i = 0; i < 4; ++i) assert(!battery_poll());
    assert(battery_poll()); /* Five consecutive low readings, not one sag. */
    raw_battery = 950; assert(!battery_poll()); assert(!warned); /* 3.83V recovery. */
    raw_battery = 880;
    for (int i = 0; i < 5; ++i) assert(!battery_poll());
    assert(warned && critical_samples == 0);
    raw_charger = 2500; assert(!battery_poll()); assert(!warned);
    raw_battery = 4095; assert(audio_board_power_read(&pack, &rail) != ESP_OK);
    puts("Battery conversion and cutoff regression checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + power + battery + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter',
                    '-Wno-unused-variable', '-fsanitize=address,undefined', '-g',
                    str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], cwd=directory,
                   env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, check=True)
