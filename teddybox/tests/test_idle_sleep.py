"""Exercise the production idle activity accounting with simulated time."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/main.c').read_text()
start = source.index('        bool playback_busy = pb_is_busy();')
activity = source[start:source.index('        if (pb_is_playing())', start)]
harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "config.h"
#define ESP_LOGI(...) ((void)0)
static bool busy;
static bool pb_is_busy(void) { return busy; }
static int64_t last_activity_time, last_idle_report;
static bool ear_big_prev, ear_small_prev;
static bool tick(int64_t cur_time, bool ear_big, bool ear_small) {
ACTIVITY
    ear_big_prev = ear_big;
    ear_small_prev = ear_small;
    return cur_time - last_activity_time > POWEROFF_TIMEOUT;
}
int main(void) {
    const int64_t second = 1000000;
    assert(!tick(0, false, false));
    assert(!tick(60 * second, false, false));
    assert(tick(61 * second, false, false));
    /* A new press restarts the timer; holding it cannot keep us awake. */
    assert(!tick(62 * second, true, false));
    assert(!tick(122 * second, true, false));
    assert(tick(123 * second, true, false));
    assert(!tick(124 * second, true, true));
    assert(tick(185 * second, true, true));
    /* Playback/download activity blocks sleep until 60 seconds after idle. */
    busy = true;
    assert(!tick(200 * second, false, false));
    assert(!tick(400 * second, false, false));
    busy = false;
    assert(!tick(460 * second, false, false));
    assert(tick(461 * second, false, false));
    puts("Idle sleep activity regression checks passed");
}
'''.replace('ACTIVITY', activity)
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror',
                    '-I', str(root / 'main'), str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
