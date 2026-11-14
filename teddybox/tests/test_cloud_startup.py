"""Exercise the production cloud task through offline startup and cancellation."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/cloud.c').read_text()
task = source[source.index('static void cloud_task'):source.index('void cloud_init')]
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <setjmp.h>
typedef unsigned TickType_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define pdTRUE 1
#define pdMS_TO_TICKS(ms) (ms)
#define ESP_ERROR_CHECK(call) assert((call) == ESP_OK)
#define CC_STATE_INIT 0
#define CC_STATE_ERROR 6
typedef struct { bool abort; int state; } cloud_content_req_t;
static cloud_content_req_t request;
static unsigned active_requests = 1;
static int cloud_request_queue;
static jmp_buf finished;
static unsigned ticks, scenario, processed, cleaned, watchdog_feeds;
static bool queued = true;
static int esp_task_wdt_add(void *task) { return ESP_OK; }
static int esp_task_wdt_reset(void) { ++watchdog_feeds; return ESP_OK; }
static unsigned xTaskGetTickCount(void) { return ticks; }
static bool wifi_is_connected(void) { return scenario == 0 && ticks >= 300; }
static int cloud_set_time(void) { return ticks >= 1200 ? ESP_OK : ESP_FAIL; }
static void vTaskDelay(unsigned delay) { ticks += delay; assert(ticks < 4000); }
static int xQueuePeek(int queue, cloud_content_req_t **out, unsigned timeout) {
    if (!queued) longjmp(finished, 1);
    if (scenario == 1 && ticks >= 500) request.abort = true;
    *out = &request; return pdTRUE;
}
static int xQueueReceive(int queue, cloud_content_req_t **out, unsigned timeout) {
    assert(request.abort || (wifi_is_connected() && cloud_set_time() == ESP_OK));
    assert(queued); queued = false; *out = &request; return pdTRUE;
}
static void cloud_content_cleanup(cloud_content_req_t *req) { assert(req == &request); ++cleaned; }
static void cloud_process_request(cloud_content_req_t *req) {
    assert(!req->abort && ticks >= 1200); ++processed; --active_requests;
}
'''
harness = r'''
int main(void) {
    for (scenario = 0; scenario < 2; ++scenario) {
        ticks = processed = cleaned = watchdog_feeds = 0;
        active_requests = 1; queued = true; request = (cloud_content_req_t){0};
        if (!setjmp(finished)) cloud_task(NULL);
        assert(!active_requests && watchdog_feeds > 1);
        if (scenario == 0) assert(processed == 1 && cleaned == 0);
        else assert(!processed && cleaned == 1 && request.state == CC_STATE_ERROR);
    }
    puts("Cloud startup readiness and offline cancellation checks passed");
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
