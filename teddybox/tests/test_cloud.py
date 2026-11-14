"""Host regression checks for production cloud parser, resume, and credential I/O.

Run: python3 tests/test_cloud.py (requires a C compiler).
The runner extracts the hardware-independent production functions, supplying
only ESP logging and semaphore stubs; no cloud credentials or network needed.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/cloud.c').read_text()
playback_source = (root / 'main/playback.c').read_text()
filename_start = playback_source.index('char *pb_build_filename(uint64_t id)')
filename_function = playback_source[filename_start:playback_source.index('esp_err_t pb_check_file', filename_start)]

def section(start, end):
    return source[source.index(start):source.index(end, source.index(start))]

preamble = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <limits.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
typedef int esp_err_t;
typedef int SemaphoreHandle_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 257
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define xSemaphoreGive(...) ((void)0)
static int file_lock_available = 1;
#define xSemaphoreTake(...) file_lock_available
#define vTaskDelay(...) ((void)0)
#define portTICK_PERIOD_MS 1
#define portTICK_RATE_MS 1
#include "cloud.h"
'''
clock_stubs = r'''
#define CLOUD_HOST "test.invalid"
#define CLOUD_PORT 443
#define SNTP_OPMODE_POLL 0
static time_t fake_now;
static bool ntp_started;
static unsigned ntp_starts, https_calls;
static time_t fake_time(time_t *out) { if (out) *out = fake_now; return fake_now; }
#define time fake_time
static bool sntp_enabled(void) { return ntp_started; }
static void sntp_setoperatingmode(int mode) { assert(mode == SNTP_OPMODE_POLL); }
static void sntp_setservername(int index, const char *server) {
    assert(index == 0 && !strcmp(server, "pool.ntp.org"));
}
static void sntp_init(void) { ntp_started = true; ++ntp_starts; }
static int fake_settimeofday(const struct timeval *tv, const void *tz) {
    fake_now = tv->tv_sec; return 0;
}
#define settimeofday fake_settimeofday
static int transport_failure;
static esp_err_t cloud_request(cloud_req_t *req) {
    ++https_calls;
    if (req->auth) {
        if (transport_failure || (req->abort && *req->abort)) return ESP_FAIL;
        char content[] = "HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello";
        return req->data_received_cbr(req->data_received_ctx, (uint8_t *)content,
                                     strlen(content)) == CBR_CLOSE_OK ? ESP_OK : ESP_FAIL;
    }
    char response[] = "HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n1704067200";
    assert(req->data_received_cbr(req->data_received_ctx, (uint8_t *)response,
                                strlen(response)) == CBR_CLOSE_OK);
    return ESP_OK;
}
'''
request_stubs = r'''
static bool cloud_ready = true;
static unsigned active_requests;
static cloud_content_req_t *queued;
static int queue_accepts = 1;
#define xSemaphoreCreateBinary() 1
#define xSemaphoreCreateMutex() 1
#define vSemaphoreDelete(...) ((void)0)
#define xQueueSend(q, p, t) (queued = *(cloud_content_req_t **)(p), queue_accepts)
#define pdMS_TO_TICKS(ms) (ms)
#define pdTRUE 1
#define portMAX_DELAY 0
'''

harness = r'''
static unsigned char body[2048];
static size_t used;
static esp_err_t data_cb(void *ctx, uint8_t *data, size_t len) {
    assert(used + len <= sizeof(body));
    memcpy(body + used, data, len); used += len; return ESP_OK;
}
static http_parser_t parser(uint8_t *header) {
    used = 0;
    return (http_parser_t){.header_buffer = header, .http_data_cbr = data_cb};
}
int main(void) {
    uint8_t header[MAX_HTTP_HEADER_SIZE];
    const char *response = "HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nhello";
    for (size_t split = 0; split < strlen(response); ++split) {
        memset(header, 0xA5, sizeof(header));
        http_parser_t p = parser(header);
        assert(http_parser_received_cbr(&p, (uint8_t *)response, split) == ESP_OK);
        assert(http_parser_received_cbr(&p, (uint8_t *)response + split,
                                      strlen(response) - split) == CBR_CLOSE_OK);
        assert(used == 5 && !memcmp(body, "hello", 5));
        assert(http_parser_closed_cbr(&p) == ESP_OK);
    }
    http_parser_t p = parser(header);
    for (size_t i = 0; i < strlen(response); ++i)
        assert(http_parser_received_cbr(&p, (uint8_t *)response + i, 1) ==
               (i + 1 == strlen(response) ? CBR_CLOSE_OK : ESP_OK));
    p = parser(header);
    assert(http_parser_received_cbr(&p, (uint8_t *)response, strlen(response) - 1) == ESP_OK);
    assert(http_parser_closed_cbr(&p) == ESP_FAIL);
    p = parser(header);
    uint8_t large[1600];
    const char *prefix = "HTTP/1.1 200 OK\r\nContent-Length: 1500\r\n\r\n";
    size_t n = strlen(prefix); memcpy(large, prefix, n); memset(large + n, 'x', 1500);
    assert(http_parser_received_cbr(&p, large, n + 1500) == CBR_CLOSE_OK);
    assert(used == 1500);
    p = parser(header); memset(large, 'x', sizeof(large));
    assert(http_parser_received_cbr(&p, large, sizeof(large)) == ESP_FAIL);
    const char *invalid[] = {
        "HTTP/1.1 200 OK\r\n\r\n",
        "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        "HTTP/1.1 206 OK\r\nContent-Length: 5\r\n\r\nhello",
        "HTTP/1.1 206 OK\r\nContent-Length: 5\r\nContent-Range: bytes 5-9/9\r\n\r\nhello"
    };
    for (size_t i = 0; i < sizeof(invalid)/sizeof(*invalid); ++i) {
        p = parser(header);
        assert(http_parser_received_cbr(&p, (uint8_t *)invalid[i], strlen(invalid[i])) == ESP_FAIL);
    }
    cloud_content_req_t rejected = {.state = CC_STATE_INIT};
    assert(cloud_content_status_cbr(&rejected, 403) == ESP_FAIL);
    assert(rejected.state != CC_STATE_ERROR); /* Worker still owns the request. */
    FILE *f = fopen("content", "wb"); assert(f); fwrite("hello", 1, 5, f); fclose(f);
    cloud_content_req_t req = {.filename = "content", .received = 5, .status_code = 206};
    assert(cloud_content_length_cbr(&req, 4, 5) == ESP_FAIL);
    assert(cloud_content_length_cbr(&req, 5, 5) == ESP_OK);
    assert(req.content_length == 10);
    file_lock_available = 0;
    assert(cloud_content_cbr(&req, (uint8_t *)"world", 5) == ESP_FAIL);
    assert(req.received == 5);
    file_lock_available = 1;
    assert(cloud_content_cbr(&req, (uint8_t *)"world", 5) == ESP_OK);
    rewind(req.handle); char result[11] = {0};
    assert(fread(result, 1, 10, req.handle) == 10 && !strcmp(result, "helloworld"));
    fclose(req.handle);
    req.status_code = 200;
    assert(cloud_content_length_cbr(&req, 0, 5) == ESP_OK);
    assert(req.received == 0 && req.content_length == 5); fclose(req.handle);
    uint8_t *cert = NULL; size_t length = 0;
    assert(cloud_load_cert("missing", &cert, &length) == ESP_FAIL);
    f = fopen("cert", "wb"); fclose(f);
    assert(cloud_load_cert("cert", &cert, &length) == ESP_FAIL);
    f = fopen("cert", "wb"); fputs("-----BEGIN CERTIFICATE-----", f); fclose(f);
    assert(cloud_load_cert("cert", &cert, &length) == ESP_OK);
    assert(length == 28 && cert[length-1] == 0); free(cert);
    f = fopen("cert", "wb"); fwrite("\x30\x00\x01", 1, 3, f); fclose(f);
    assert(cloud_load_cert("cert", &cert, &length) == ESP_OK);
    assert(length == 3 && cert[0] == 0x30); free(cert);
    cloud_time_response_t timestamp = {0};
    assert(cloud_set_time_cbr(&timestamp, (uint8_t *)"1704", 4) == ESP_OK);
    assert(cloud_set_time_cbr(&timestamp, (uint8_t *)"067200", 6) == ESP_OK);
    assert(!strcmp(timestamp.text, "1704067200"));
    assert(cloud_set_time_cbr(&timestamp, large, 33) == ESP_FAIL);
#if CONFIG_TEDDYBOX_OFFICIAL_CLOUD || defined(CONFIG_MBEDTLS_HAVE_TIME_DATE)
    fake_now = 0;
    assert(cloud_set_time() == ESP_FAIL);
    assert(ntp_starts == 1 && https_calls == 0);
    assert(cloud_set_time() == ESP_FAIL);
    assert(ntp_starts == 1 && https_calls == 0);
    fake_now = 1704067200;
    assert(cloud_set_time() == ESP_OK);
    assert(ntp_starts == 1 && https_calls == 0);
#else
    assert(cloud_set_time() == ESP_OK);
    assert(https_calls == 1 && ntp_starts == 0 && fake_now == 1704067200);
#endif
    uint8_t tag_token[32]; memset(tag_token, 0xAB, sizeof(tag_token));
    cloud_content_req_t *download = cloud_content_download(UINT64_C(0xE004035021255395), tag_token);
    assert(download && queued == download);
    assert(!strcmp(download->location, "/v2/content/95532521500304e0"));
    assert(!strcmp(download->filename, "/sdcard/CONTENT/95532521/500304E0"));
    assert(strlen(download->auth) == 64 && !strncmp(download->auth, "ABAB", 4));
    assert(download->references == 2);
    cloud_content_cleanup(download); /* release playback during an active transfer */
    assert(queued->references == 1 && !strcmp(queued->location, "/v2/content/95532521500304e0"));
    transport_failure = 1;
    assert(cloud_process_request(queued) == ESP_FAIL); /* Worker releases last. */
    assert(!cloud_is_busy());
    transport_failure = 0;
    download = cloud_content_download(UINT64_C(0xE004035021255395), tag_token);
    free(download->filename); download->filename = strdup("cloud-content");
    assert(cloud_process_request(download) == ESP_OK);
    assert(download->state == CC_STATE_FINISHED && download->handle && download->references == 1);
    assert(!cloud_is_busy());
    f = fopen("cloud-content", "rb"); assert(f);
    memset(result, 0, sizeof(result));
    assert(fread(result, 1, 5, f) == 5 && !strcmp(result, "hello")); fclose(f);
    cloud_content_cleanup(download);
    queue_accepts = 0;
    assert(!cloud_content_download(UINT64_C(0xE004035021255395), tag_token));
    assert(!cloud_is_busy());
    puts("Cloud regression checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + request_stubs +
        section('esp_err_t http_parser_closed_cbr', '/********************************************************/\n/* TLS connection') +
        section('esp_err_t cloud_create_directories', 'esp_err_t cloud_process_request') +
        clock_stubs +
        section('typedef struct\n{\n    char text[33];', '/********************************************************/\n/* content download') +
        section('esp_err_t cloud_load_cert', 'static void cloud_task') +
        section('esp_err_t cloud_process_request', '/********************************************************/\n/* cloud request code') +
        filename_function +
        section('cloud_content_req_t *cloud_content_download', '/********************************************************/\n/* certificate loading') + harness)
    for config in (['-DCONFIG_TEDDYBOX_OFFICIAL_CLOUD=0'],
                   ['-DCONFIG_TEDDYBOX_OFFICIAL_CLOUD=0', '-DCONFIG_MBEDTLS_HAVE_TIME_DATE=1'],
                   ['-DCONFIG_TEDDYBOX_OFFICIAL_CLOUD=1', '-DCONFIG_MBEDTLS_HAVE_TIME_DATE=1']):
        subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter', '-Wno-unused-variable',
                        '-fsanitize=address,undefined', '-g', '-I', str(root / 'main'), *config,
                        str(path / 'test.c'), '-o', str(path / 'test')], check=True)
        subprocess.run([str(path / 'test')], cwd=directory,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, check=True)
