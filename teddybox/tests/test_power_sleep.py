"""Check the production poweroff wake selection for every input combination."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'components/toniebox/toniebox_esp32_v1.6.C/board.c').read_text()
function = source[source.index('void audio_board_poweroff()'):]
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef int gpio_num_t;
#define WAKEUP_GPIO 7
#define EAR_BIG_GPIO 20
#define EAR_SMALL_GPIO 21
#define POWER_GPIO 45
#define SD_POWER_GPIO 47
#define GPIO_NUM_NC -1
#define RTC_GPIO_MODE_INPUT_ONLY 0
#define ESP_SLEEP_WAKEUP_ALL 0
#define ESP_EXT1_WAKEUP_ALL_LOW 0
#define ESP_LOGW(...) ((void)0)
#define ESP_ERROR_CHECK(call) assert((call) == 0)
#define BIT64(pin) (UINT64_C(1) << (pin))
static uint64_t levels, ext1;
static int ext0;
static uint64_t timer;
static bool slept;
static bool pwm_stopped;
static uint64_t isolated_pins;
typedef struct { uint64_t pin_bit_mask; int mode; } gpio_config_t;
#define GPIO_MODE_DISABLE 0
static void dac3100_deinit(void) {}
static void led_deinit(void) { pwm_stopped = true; }
static int gpio_config(const gpio_config_t *config) {
    assert(pwm_stopped && config->mode == GPIO_MODE_DISABLE);
    isolated_pins = config->pin_bit_mask;
    return 0;
}
static void audio_board_power(bool on) {
    assert(!on && isolated_pins);
    /* Isolate powered-off inputs/buses, preserve power, wake and flash pins. */
    assert(isolated_pins & BIT64(48));
    assert(isolated_pins & BIT64(13));
    const uint64_t preserved = BIT64(7) | BIT64(20) | BIT64(21) |
        BIT64(45) | BIT64(47) | (UINT64_C(0x3f) << 27) | BIT64(43) | BIT64(44);
    assert(!(isolated_pins & preserved));
}
static int gpio_get_level(int pin) { return (levels & BIT64(pin)) != 0; }
static int rtc_gpio_init(int pin) { return 0; }
static int rtc_gpio_set_direction(int pin, int mode) { return 0; }
static int rtc_gpio_pullup_en(int pin) { return 0; }
static int rtc_gpio_pulldown_dis(int pin) { return 0; }
static int esp_sleep_disable_wakeup_source(int source) { ext0 = -1; ext1 = timer = 0; return 0; }
static int esp_sleep_enable_ext0_wakeup(int pin, int level) {
    assert(level == 0); ext0 = pin; return 0;
}
static int esp_sleep_enable_ext1_wakeup(uint64_t mask, int mode) {
    assert(mode == ESP_EXT1_WAKEUP_ALL_LOW && mask); ext1 = mask; return 0;
}
static int esp_sleep_enable_timer_wakeup(uint64_t us) { timer = us; return 0; }
static int gpio_hold_en(int pin) { assert(pin == 45 || pin == 47); return 0; }
static void gpio_deep_sleep_hold_en(void) {}
static void esp_deep_sleep_start(void) { slept = true; }
'''
harness = r'''
int main(void) {
    for (int combination = 0; combination < 8; ++combination) {
        levels = 0;
        if (combination & 1) levels |= BIT64(7);
        if (combination & 2) levels |= BIT64(20);
        if (combination & 4) levels |= BIT64(21);
        slept = pwm_stopped = false;
        isolated_pins = 0;
        audio_board_poweroff(); assert(slept);
        if (ext0 >= 0) assert(levels & BIT64(ext0));
        assert(!(ext1 & ~levels)); /* No already-active wake source. */
        if (combination == 0) assert(timer == 60000000);
        else assert(ext0 >= 0 || ext1);
        if (combination == 6) {
            /* Docked: GPIO7 low, each released ear has a distinct wake source. */
            assert(ext0 == 20 && ext1 == BIT64(21));
        }
        if (combination & 1) assert(ext0 == 7);
    }
    puts("Deep-sleep wake-source regression checks passed");
}
'''
# Use the production board's pin assignments for the bus isolation mask.
board_defs = (root / 'components/toniebox/toniebox_esp32_v1.6.C/board_def.h').read_text()
for name, pin in re.findall(r'#define\s+(\w+)\s+GPIO_NUM_(\d+)', board_defs):
    if not re.search(r'#define\s+' + name + r'\b', preamble):
        preamble += f'\n#define {name} {pin}\n'
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + function + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-g', str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], cwd=directory,
                   env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, check=True)
