"""Verify board SD shutdown reaches real unmount and propagates failures."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'components/toniebox/toniebox_esp32_v1.6.C/board.c').read_text()
start = source.index('esp_err_t audio_board_sdcard_unmount()')
function = source[start:source.index('esp_err_t audio_board_sdcard_init(', start)]
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_LOGE(...) ((void)0)
static int sdcard_handle = 1;
static int stop_result, timer_result, unmount_result;
static bool mounted;
static unsigned timer_stops, unmounts;
static int esp_periph_stop(int handle) { assert(handle == 1); return stop_result; }
static int esp_periph_stop_timer(int handle) { ++timer_stops; return timer_result; }
static bool periph_sdcard_is_mounted(int handle) { return mounted; }
static int sdcard_unmount(void) { ++unmounts; return unmount_result; }
'''
harness = r'''
int main(void) {
    mounted = true;
    assert(audio_board_sdcard_unmount() == ESP_OK && timer_stops == 1 && unmounts == 1);
    unmount_result = 42;
    assert(audio_board_sdcard_unmount() == 42 && unmounts == 2);
    mounted = false;
    assert(audio_board_sdcard_unmount() == ESP_OK && unmounts == 2);
    timer_result = 43;
    assert(audio_board_sdcard_unmount() == 43 && unmounts == 2);
    stop_result = ESP_FAIL;
    assert(audio_board_sdcard_unmount() == ESP_FAIL && timer_stops == 4 && unmounts == 2);
    puts("SD host shutdown and failure propagation checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + function + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
