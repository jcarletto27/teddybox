"""Check the production Wi-Fi shutdown ordering and failure propagation."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/wifi.c').read_text()
functions = source[source.index('bool wifi_is_connected()'):source.index('void wifi_load_nvs(void)')]
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef int esp_err_t;
#define ESP_OK 0
#define IP_EVENT 1
#define WIFI_EVENT 2
#define IP_EVENT_STA_GOT_IP 3
#define ESP_EVENT_ANY_ID -1
#define ESP_LOGI(...) ((void)0)
static bool connected, ip_registered, wifi_registered;
static unsigned calls;
static int fail_at, stop_result;
static void got_ip_event_handler(void) {}
static void wifi_event_handler(void) {}
static int esp_event_handler_unregister(int base, int id, void (*handler)(void)) {
    ++calls;
    if (calls == 1) {
        assert(base == IP_EVENT && id == IP_EVENT_STA_GOT_IP);
        assert(handler == got_ip_event_handler);
        if (fail_at == 1) return 11;
        ip_registered = false;
    } else {
        assert(calls == 2 && base == WIFI_EVENT && id == ESP_EVENT_ANY_ID);
        assert(handler == wifi_event_handler);
        if (fail_at == 2) return 12;
        wifi_registered = false;
    }
    return ESP_OK;
}
static int esp_wifi_stop(void) {
    ++calls;
    /* Stop can emit disconnect events: no reconnect callback may remain. */
    assert(calls == 3 && !ip_registered && !wifi_registered && !connected);
    return stop_result;
}
'''
harness = r'''
int main(void) {
    for (fail_at = 0; fail_at <= 2; ++fail_at) {
        connected = ip_registered = wifi_registered = true;
        calls = 0;
        stop_result = 0;
        int result = wifi_shutdown();
        if (!fail_at) {
            assert(result == ESP_OK && calls == 3 && !wifi_is_connected());
        } else {
            assert(result == 10 + fail_at && calls == (unsigned)fail_at);
        }
    }
    fail_at = 0;
    connected = ip_registered = wifi_registered = true;
    calls = 0;
    stop_result = 42;
    assert(wifi_shutdown() == 42 && !wifi_is_connected());
    puts("Wi-Fi sleep shutdown regression checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + functions + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror',
                    str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
