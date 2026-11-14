"""Exercise the production recovery task with simulated GPIO/time, no main loop.
Run: python3 tests/test_ear_restart.py (requires a C compiler).
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/main.c').read_text()
task = source[source.index('static void ear_restart_task'):source.index('static void allocation_failed')]
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <setjmp.h>
#define ESP_LOGW(...) ((void)0)
#define pdMS_TO_TICKS(ms) (ms)
static jmp_buf finished;
static int64_t now;
static int scenario;
static bool audio_board_ear_big(void) {
    if (scenario == 0) return true; /* Held throughout startup. */
    if (scenario == 1) return now >= 100000; /* One ear only. */
    if (scenario == 2) return now >= 100000 && now < 5000000; /* Short hold. */
    if (scenario == 3) return now >= 100000; /* Normal restart. */
    if (scenario == 4) return now >= 100000; /* Brief release interrupts hold. */
    return now < 6000000 || now >= 6100000; /* Release after boot, then hold. */
}
static bool audio_board_ear_small(void) {
    if (scenario == 1) return false;
    if (scenario == 4 && now >= 3000000 && now < 3050000) return false;
    return audio_board_ear_big();
}
static int64_t esp_timer_get_time(void) { return now; }
static void esp_restart(void) { longjmp(finished, 1); }
static void vTaskDelay(int ms) {
    assert(ms == 50);
    now += (int64_t)ms * 1000;
    if (now > 12000000) longjmp(finished, 2);
}
'''
harness = r'''
int main(void) {
    for (scenario = 0; scenario < 6; ++scenario) {
        now = 0;
        int outcome = setjmp(finished);
        if (!outcome) ear_restart_task(NULL);
        if (scenario < 3) assert(outcome == 2);
        if (scenario == 3) assert(outcome == 1 && now == 5100000);
        if (scenario == 4) assert(outcome == 1 && now == 8050000);
        if (scenario == 5) assert(outcome == 1 && now == 11100000);
    }
    puts("Ear restart regression checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + task + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-g', str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], cwd=directory,
                   env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, check=True)
