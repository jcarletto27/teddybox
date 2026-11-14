"""Exercise production shutdown ordering and refusal to cut power on failure."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/main.c').read_text()
start = source.index('    ledman_change("poweroff");')
shutdown = source[start:source.index('    ESP_LOGE(TAG, "back, quite unexpected...', start)]
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <setjmp.h>
#define ESP_OK 0
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_ERROR_CHECK(call) do { if ((call) != 0) longjmp(failed, 1); } while (0)
#define pdMS_TO_TICKS(ms) (ms)
#define portTICK_PERIOD_MS 1
#define abort() longjmp(failed, 1)
static jmp_buf failed;
static char fail_at, steps[16];
static unsigned count;
static bool writer_busy, powered_off;
static int64_t now;
static int step(char id) { steps[count++] = id; steps[count] = 0; return fail_at == id; }
static void ledman_change(const char *state) { assert(!strcmp(state, "poweroff")); }
static int nfc_shutdown(void) { return step('N'); }
static int accel_shutdown(void) { return step('A'); }
static int pb_shutdown(void) { return step('B'); }
static int wifi_shutdown(void) { return step('W'); }
static int audio_board_sdcard_unmount(void) { return step('S'); }
static int ledman_shutdown(void) { return step('L'); }
static void rtc_checksum_update(void) { step('R'); }
static bool cloud_is_busy(void) { return writer_busy; }
static int64_t esp_timer_get_time(void) { now += 1000000; return now; }
static void vTaskDelay(int ticks) { assert(ticks > 0); }
static void audio_board_poweroff(void) { step('D'); powered_off = true; }
static void shutdown(void) {
'''
harness = r'''
}
static void reset(void) {
    count = 0; steps[0] = 0; powered_off = writer_busy = false; now = 0;
}
int main(void) {
    reset();
    shutdown();
    assert(powered_off && !strcmp(steps, "NABWSRLD"));
    const char failures[] = "NABWSL";
    for (unsigned i = 0; i < sizeof(failures) - 1; ++i) {
        reset(); fail_at = failures[i];
        if (!setjmp(failed)) { shutdown(); assert(!"ignored shutdown failure"); }
        assert(!powered_off);
    }
    reset(); fail_at = 0; writer_busy = true;
    if (!setjmp(failed)) { shutdown(); assert(!"ignored active writer"); }
    assert(!powered_off && !strcmp(steps, "NAB"));
    puts("Shutdown ordering and failure protection checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + shutdown + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror',
                    str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
