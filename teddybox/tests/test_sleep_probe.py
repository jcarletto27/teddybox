"""Exercise standalone sleep probe phase selection with simulated resets."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'tools/sleep_probe/main/sleep_probe.c').read_text()
source = '\n'.join(line for line in source.splitlines() if not line.startswith('#include'))
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef int esp_reset_reason_t;
typedef int esp_sleep_wakeup_cause_t;
typedef struct { uint64_t pin_bit_mask; int mode; } gpio_config_t;
#define GPIO_NUM_17 17
#define GPIO_NUM_18 18
#define GPIO_NUM_19 19
#define GPIO_NUM_45 45
#define GPIO_NUM_47 47
#define ESP_RST_DEEPSLEEP 8
#define ESP_SLEEP_WAKEUP_TIMER 4
#define GPIO_MODE_OUTPUT 1
#define ESP_PD_DOMAIN_RTC_PERIPH 0
#define ESP_PD_OPTION_ON 1
#define ESP_SLEEP_WAKEUP_ALL 0
#define ESP_ERROR_CHECK(call) assert((call) == 0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define RTC_DATA_ATTR
#define BIT64(pin) (UINT64_C(1) << (pin))
#define pdMS_TO_TICKS(ms) (ms)
static int reset, wake;
static uint64_t levels, timer;
static bool slept;
static int esp_reset_reason(void) { return reset; }
static int esp_sleep_get_wakeup_cause(void) { return wake; }
static int gpio_config(const gpio_config_t *cfg) { assert(cfg->mode == GPIO_MODE_OUTPUT); return 0; }
static int gpio_set_level(int pin, int level) {
    if (level) levels |= BIT64(pin); else levels &= ~BIT64(pin);
    return 0;
}
static int gpio_hold_dis(int pin) { return 0; }
static int gpio_hold_en(int pin) { return 0; }
static void gpio_deep_sleep_hold_en(void) {}
static void gpio_deep_sleep_hold_dis(void) {}
static void vTaskDelay(int ticks) { assert(ticks == 2000); }
static int esp_sleep_pd_config(int domain, int option) { return 0; }
static int esp_sleep_disable_wakeup_source(int source) { timer = 0; return 0; }
static int esp_sleep_enable_timer_wakeup(uint64_t us) { timer = us; return 0; }
static void esp_deep_sleep_start(void) { slept = true; }
'''
harness = r'''
static void boot(int reason, int cause, bool expect_on, unsigned expect_phase) {
    reset = reason; wake = cause; slept = false;
    app_main();
    assert(slept && timer == 10000000 && phase == expect_phase);
    assert(!!(levels & BIT64(45)) == expect_on);
    assert(!!(levels & BIT64(47)) != expect_on);
    assert(!(levels & LED_MASK));
}
int main(void) {
    boot(1, 0, true, 1);  /* Cold boot: baseline with power held on. */
    boot(8, 4, false, 2); /* Successful baseline: try powering off. */
    boot(8, 4, true, 1);  /* Both passed: repeat baseline. */
    boot(8, 4, false, 2);
    boot(1, 0, true, 1);  /* Failed sleep: restart baseline. */
    boot(8, 2, true, 1);  /* Unexpected wake is not a timer-test pass. */
    puts("Standalone sleep probe phase checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + source + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter',
                    '-Wno-unused-variable', str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
