"""Production playback polling, stop ownership, and partial-file read regressions."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/playback.c').read_text()
cloud = (root / 'main/cloud.c').read_text()

def section(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]

preamble = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <limits.h>
#include <unistd.h>
#include <fcntl.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef int esp_err_t;
typedef int SemaphoreHandle_t;
typedef unsigned TickType_t;
typedef void *audio_element_handle_t;
typedef void *TaskHandle_t;
typedef void *esp_periph_set_handle_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 1
#define ESP_ERR_TIMEOUT 2
#define ESP_ERR_NOT_FOUND 3
#define ESP_ERR_INVALID_STATE 4
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define AEL_IO_FAIL -2
#define AEL_IO_DONE -1
#define pdTRUE 1
#define xSemaphoreGive(...) ((void)0)
#define pdMS_TO_TICKS(ms) (ms)
#define vSemaphoreDelete(...) ((void)0)
#define __ESP_PERIPHERALS_H__
#include "cloud.h"
#include "toniebox.pb.taf-header.pb-c.h"
'''
# Use the real structs/constants; hardware and generated message implementations
# are unnecessary for these selected production functions.
header = '\n'.join(line for line in (root / 'main/playback.h').read_text().splitlines()
                   if not line.startswith('#include') and line != '#pragma once')
stubs = r'''
static const char *TAG;
static bool pb_playing, pb_default_content, pipeline_started;
static bool shutdown_complete, shutting_down;
static int playback_queue, queue_accepts = 1;
static jmp_buf panic;
static int stop_results[2];
static void test_abort(void) { longjmp(panic, 1); }
#define abort() test_abort()
static cloud_content_req_t *current_dl_req;
static pb_toniefile_t pb_toniefile_info;
static uint64_t pending_uid, pb_last_nfc_uid;
static uint32_t pending_received, pb_last_play_position;
static char *pending_filename;
static int64_t pending_progress_time, now;
static int pipeline;
static audio_element_handle_t music_decoder = (void *)1, i2s_stream_writer = (void *)2;
static int plays, stops, notifications, resets, failed_led;
static cloud_content_req_t *waiting_download;
static int wait_mode;
static int xSemaphoreTake(int sem, int ticks) {
    if (sem == 2) { /* file mutex */
        if (wait_mode == 1) { now += (int64_t)ticks * 1000; return 0; }
        return 1;
    }
    now += (int64_t)ticks * 1000;
    if (wait_mode == 2) waiting_download->abort = true;
    if (wait_mode == 3) waiting_download->received += 8;
    return 0;
}
static int64_t esp_timer_get_time(void) { return now; }
static int audio_pipeline_stop(int p) { ++stops; return ESP_OK; }
static int audio_element_wait_for_stop_ms(audio_element_handle_t element, int ticks) {
    assert(ticks == 2000 && (element == music_decoder || element == i2s_stream_writer));
    return stop_results[element == music_decoder ? 0 : 1];
}
static int audio_pipeline_terminate_with_ticks(int p, int ticks) {
    assert(ticks == 2000); return ESP_OK;
}
static void audio_pipeline_reset_ringbuffer(int p) { ++resets; }
static void audio_pipeline_reset_elements(int p) {}
#define AEL_STATE_INIT 0
static void audio_pipeline_change_state(int p, int state) {}
static void xTaskNotifyGive(TaskHandle_t task) { assert(task); ++notifications; }
static esp_err_t pb_req_handle_stop(pb_req_stop_t *req);
static int xQueueSend(int queue, void *message, int ticks) {
    assert(ticks == 250);
    if (!queue_accepts) return 0;
    pb_req_stop_t *req = *(pb_req_stop_t **)message;
    assert(req->hdr.type == PB_REQ_TYPE_STOP);
    pb_req_handle_stop(req); free(req); return pdTRUE;
}
static TaskHandle_t xTaskGetCurrentTaskHandle(void) { return (void *)1; }
static unsigned ulTaskNotifyTake(int clear, int ticks) {
    assert(clear == pdTRUE && ticks == 12000);
    unsigned count = notifications; notifications = 0; return count;
}
static void ledman_change(const char *state) { if (!strcmp(state, "failed")) ++failed_led; }
static esp_err_t pb_int_play_file(const char *file, uint64_t uid) {
    assert(file && uid == pending_uid); ++plays; pb_playing = true; pipeline_started = true; return ESP_OK;
}
void toniebox_audio_file_header__free_unpacked(TonieboxAudioFileHeader *message,
                                              ProtobufCAllocator *allocator) { free(message); }
'''
harness = r'''
static cloud_content_req_t *new_request(void) {
    cloud_content_req_t *r = calloc(1, sizeof(*r)); assert(r);
    r->references = 2; r->state = CC_STATE_INIT;
    r->handle = tmpfile(); assert(r->handle);
    current_dl_req = r;
    pending_filename = strdup("content"); pending_uid = 1;
    pending_received = 0; pending_progress_time = now;
    return r;
}
static void worker_finishes(cloud_content_req_t *r) {
    assert(r->references == 1 && r->abort);
    /* STOP must leave the worker's open write handle intact. */
    assert(fwrite("x", 1, 1, r->handle) == 1);
    int fd = fileno(r->handle);
    cloud_content_cleanup(r); /* Last reference closes the shared stream. */
    assert(fcntl(fd, F_GETFD) == -1);
}
int main(void) {
    cloud_content_req_t *r = new_request();
    pb_poll_download(); assert(plays == 0 && pending_filename);
    pb_req_stop_t stop = {0};
    assert(pb_req_handle_stop(&stop) == ESP_OK); /* Tag removal during INIT. */
    assert(!current_dl_req && !pending_filename); worker_finishes(r);

    r = new_request(); r->state = CC_STATE_RECEIVING;
    r->received = (PB_MIN_DL_BLOCKS + 1) * TONIEFILE_FRAME_SIZE;
    pb_poll_download(); assert(plays == 0 && pending_filename && current_dl_req == r);
    /* Receiving additional data still must not start the decoder. */
    pb_poll_download(); assert(plays == 0 && pending_filename);
    r->state = CC_STATE_FINISHED;
    pb_poll_download(); assert(plays == 1 && !pending_filename && current_dl_req == r);
    pb_poll_download(); assert(plays == 1);
    pb_toniefile_info.valid = true;
    pb_toniefile_info.filename = strdup("content");
    pb_toniefile_info.fd = r->handle;
    pb_toniefile_info.download = r;
    pb_int_stop(); assert(stops == 1 && !pb_toniefile_info.valid);
    worker_finishes(r);

    /* A resumed download with ample buffered content still waits for completion. */
    r = new_request(); r->state = CC_STATE_RECEIVING;
    r->received = (PB_MIN_DL_BLOCKS + 1) * TONIEFILE_FRAME_SIZE;
    pb_poll_download(); assert(plays == 1 && pending_filename && current_dl_req == r);
    r->received += 3200; now += 1000000;
    pb_poll_download(); assert(plays == 1 && pending_filename);
    pb_int_stop(); worker_finishes(r); /* Waiting remains cancellable. */
    r = new_request(); r->state = CC_STATE_FINISHED; r->received = 5000;
    pb_poll_download(); assert(plays == 2 && current_dl_req == r);
    pb_int_stop(); worker_finishes(r); /* Completed downloads also retain cleanup ownership. */

    r = new_request(); now += 30000000;
    pb_poll_download(); assert(failed_led == 1 && !pending_filename); worker_finishes(r);
    r = new_request(); r->state = CC_STATE_ERROR;
    pb_poll_download(); assert(failed_led == 2); worker_finishes(r);

    /* A separate reader must not expose uncommitted bytes or underflow at EOF. */
    FILE *file = tmpfile(); assert(file);
    assert(fseek(file, TONIEFILE_FRAME_SIZE, SEEK_SET) == 0);
    fwrite("abcdefgh", 1, 8, file); fflush(file);
    cloud_content_req_t download = {.received = TONIEFILE_FRAME_SIZE + 4};
    pb_toniefile_t info = {.fd = file, .download = &download, .initialized = true,
        .current_block = 1, .current_pos = TONIEFILE_FRAME_SIZE};
    assert(pb_ensure_block(&info, file) == 4);
    info.current_pos += 4;
    assert(pb_ensure_block(&info, file) == 0);
    download.received += 4;
    assert(pb_ensure_block(&info, file) == 4); /* Same block rereads after EOF. */
    info.current_pos = 2 * TONIEFILE_FRAME_SIZE;
    assert(pb_ensure_block(&info, file) == 0);
    /* Exercise the actual decoder callback at a live-file boundary. */
    uint32_t page = 0;
    TonieboxAudioFileHeader taf = {.n_track_page_nums = 1, .track_page_nums = &page};
    info.taf = &taf; info.valid = true; info.target_chapter = -1; info.target_pos = -1;
    info.current_pos = TONIEFILE_FRAME_SIZE; info.current_block = 1; info.current_block_avail = 0;
    download.file_sem = 2; download.update_sem = 1; download.state = CC_STATE_RECEIVING;
    download.received = TONIEFILE_FRAME_SIZE;
    waiting_download = &download; wait_mode = 3;
    char output[8];
    assert(pb_toniefile_cbr(NULL, output, 8, 0, &info) == 8);
    assert(!memcmp(output, "abcdefgh", 8));
    info.current_pos = TONIEFILE_FRAME_SIZE; wait_mode = 1;
    int64_t before = now;
    assert(pb_toniefile_cbr(NULL, output, 8, 0, &info) == AEL_IO_FAIL);
    assert(now - before == 2000000); /* One bounded lock wait. */
    info.current_pos = download.received; wait_mode = 2;
    assert(pb_toniefile_cbr(NULL, output, 8, 0, &info) == AEL_IO_FAIL);
    download.abort = false; wait_mode = 0; before = now;
    assert(pb_toniefile_cbr(NULL, output, 8, 0, &info) == AEL_IO_FAIL);
    assert(now - before == 30000000); /* Live-data wait has a finite deadline. */
    download.state = CC_STATE_FINISHED;
    assert(pb_toniefile_cbr(NULL, output, 8, 0, &info) == AEL_IO_DONE);
    assert(info.valid); /* Only pipeline owner closes/free decoder state. */
    fclose(file);

    /* Failed pipeline stop must panic before releasing a live decoder's buffers. */
    r = new_request(); pipeline_started = true;
    stop_results[0] = ESP_FAIL; /* This element has already stopped. */
    stop_results[1] = ESP_ERR_TIMEOUT; /* The other element really is stuck. */
    if (!setjmp(panic)) { pb_int_stop(); assert(false); }
    assert(current_dl_req == r && r->references == 2 && r->handle);
    stop_results[0] = stop_results[1] = ESP_OK;
    pb_int_stop(); worker_finishes(r);
    r = new_request(); pipeline_started = true;
    stop_results[0] = stop_results[1] = ESP_FAIL; /* Normal EOF in this ADF version. */
    if (!setjmp(panic)) pb_int_stop();
    else assert(!"Already-stopped elements must not panic");
    worker_finishes(r);
    stop_results[0] = stop_results[1] = ESP_OK;
    queue_accepts = 0; assert(pb_shutdown() == ESP_ERR_TIMEOUT);
    queue_accepts = 1; assert(pb_shutdown() == ESP_OK);
    assert(shutdown_complete && !notifications);
    assert(pb_queue_request(calloc(1, sizeof(pb_req_t))) == ESP_ERR_INVALID_STATE);
    puts("Playback cancellation, polling and EOF regression checks passed");
}
'''
code = preamble + header + stubs + \
    section(cloud, 'cloud_content_state_t cloud_content_get_state', '/********************************************************/\n/* certificate loading') + \
    section(source, 'void pb_toniefile_close', 'esp_err_t pb_seek') + \
    section(source, 'static esp_err_t pb_queue_request', 'esp_err_t pb_play_default') + \
    section(source, 'esp_err_t pb_shutdown(void)', 'bool pb_is_playing') + \
    section(source, 'static esp_err_t pb_int_abort_dl', 'static esp_err_t pb_int_play_file') + \
    section(source, 'static void pb_poll_download', 'static esp_err_t pb_req_handle_default') + harness
# Only cloud_is_busy uses this global in the selected accessor/cleanup range.
code = code.replace('static const char *TAG;', 'static const char *TAG;\nstatic unsigned active_requests;')
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'protobuf-c').mkdir()
    (path / 'protobuf-c/protobuf-c.h').write_text((root / 'main/proto/protobuf-c.h').read_text())
    (path / 'test.c').write_text(code)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter',
                    '-Wno-unused-variable', '-Wno-sign-compare', '-fsanitize=address,undefined', '-g',
                    '-I', str(path), '-I', str(root / 'main'), '-I', str(root / 'main/proto'),
                    '-I', str(root / 'main/proto/proto'), str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], cwd=directory,
                   env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, check=True)
