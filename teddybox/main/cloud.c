
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <strings.h>
#include <limits.h>
#include <unistd.h>
#include "esp_sntp.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"

#include "esp_log.h"
#include "esp_system.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_tls.h"
#include "nvs_flash.h"

#include "lwip/err.h"
#include "lwip/sockets.h"
#include "lwip/sys.h"
#include "lwip/netdb.h"
#include "lwip/dns.h"

#include "cloud.h"
#include "wifi.h"
#include "playback.h"

#ifndef CONFIG_TEDDYBOX_OFFICIAL_CLOUD
#define CONFIG_TEDDYBOX_OFFICIAL_CLOUD 0
#endif

#define CLOUD_HOST CONFIG_TEDDYBOX_CLOUD_HOST
#define CLOUD_PORT CONFIG_TEDDYBOX_CLOUD_PORT

static const char *TAG = "Cloud";

uint8_t *ca_der = NULL;
uint8_t *client_der = NULL;
uint8_t *private_der = NULL;
size_t ca_der_len = 0;
size_t client_der_len = 0;
size_t private_der_len = 0;
static esp_app_desc_t running_app_info;
static QueueHandle_t cloud_request_queue;
static bool cloud_ready;
static unsigned active_requests;

/********************************************************/
/* HTTP parser handling routines                        */
/********************************************************/

esp_err_t http_parser_closed_cbr(void *ctx)
{
    http_parser_t *parser = ctx;
    return parser->header_parsed &&
           parser->received_data_length == parser->content_length ? ESP_OK : ESP_FAIL;
}

esp_err_t http_parser_received_cbr(void *ctx, uint8_t *data, size_t length)
{
    http_parser_t *parser = ctx;
    if (!parser->header_buffer)
        return ESP_FAIL;

    while (!parser->header_parsed && length)
    {
        if (parser->header_size >= MAX_HTTP_HEADER_SIZE - 1)
            return ESP_FAIL;
        parser->header_buffer[parser->header_size++] = *data++;
        --length;
        parser->header_buffer[parser->header_size] = 0;
        if (parser->header_size < 4 ||
            memcmp(parser->header_buffer + parser->header_size - 4, "\r\n\r\n", 4))
            continue;

        int status;
        if (sscanf((char *)parser->header_buffer, "HTTP/1.%*d %d", &status) != 1)
            return ESP_FAIL;
        if (parser->http_status_cbr && parser->http_status_cbr(parser->ctx, status) != ESP_OK)
            return ESP_FAIL;

        size_t start = 0, end = 0, total = 0;
        bool have_length = false, have_range = false;
        char *line = strstr((char *)parser->header_buffer, "\r\n");
        while (line && line[2])
        {
            line += 2;
            if (!strncasecmp(line, "Content-Length:", 15))
            {
                if (have_length || sscanf(line + 15, "%zu", &parser->content_length) != 1)
                    return ESP_FAIL;
                have_length = true;
            }
            if (!strncasecmp(line, "Transfer-Encoding:", 18))
                return ESP_FAIL; /* Chunked framing is not supported. */
            if (!strncasecmp(line, "Content-Range:", 14))
            {
                if (sscanf(line + 14, " bytes %zu-%zu/%zu", &start, &end, &total) != 3 ||
                    end < start || end >= total)
                    return ESP_FAIL;
                have_range = true;
            }
            line = strstr(line, "\r\n");
        }
        if (!have_length || (status == 206 && (!have_range || end - start + 1 != parser->content_length)))
            return ESP_FAIL;
        parser->header_parsed = 1;
        if (parser->content_length_cbr &&
            parser->content_length_cbr(parser->ctx, start, parser->content_length) != ESP_OK)
            return ESP_FAIL;
    }
    if (!parser->header_parsed)
        return ESP_OK;
    if (length > parser->content_length - parser->received_data_length)
        return ESP_FAIL;
    if (length && parser->http_data_cbr)
    {
        esp_err_t ret = parser->http_data_cbr(parser->ctx, data, length);
        if (ret != ESP_OK)
            return ret;
    }
    parser->received_data_length += length;
    if (parser->received_data_length == parser->content_length)
        return CBR_CLOSE_OK;
    return ESP_OK;
}

/********************************************************/
/* TLS connection handling                              */
/********************************************************/

static esp_err_t cloud_request(cloud_req_t *req)
{
    esp_err_t ret = ESP_FAIL;

    struct esp_tls *tls = NULL;
    uint8_t *receive_buffer = NULL;
    char *auth_line = strdup("");
    char *range_line = strdup("");
    char *request = NULL;
    char *url = NULL;

    if (!auth_line || !range_line)
        goto exit;
    if (req->auth)
    {
        free(auth_line);
        auth_line = NULL;
        if (asprintf(&auth_line, "Authorization: BD %s\r\n", req->auth) < 0)
            goto exit;
    }
    if (req->range_start)
    {
        free(range_line);
        range_line = NULL;
        if (asprintf(&range_line, "Range: bytes=%u-\r\n", req->range_start) < 0)
            goto exit;
    }
    if (asprintf(&url, "https://%s:%d%s", req->host, req->port, req->path) < 0)
        goto exit;
    ESP_LOGI(TAG, "Connect to %s...", url);

    if (asprintf(&request, "GET %s HTTP/1.1\r\n"
                       "Host: %s\r\n"
                       "Connection: close\r\n"
                       "%s"
                       "%s"
                       "User-Agent: teddybox/1.0 (ESP32) %s\r\n"
                       "\r\n",
             req->path, req->host, auth_line, range_line, running_app_info.version) < 0)
        goto exit;

    // ESP_LOGI(TAG, "Header %s...", request);
    esp_tls_cfg_t cfg = {
        .cacert_buf = ca_der,
        .cacert_bytes = ca_der_len,
        .clientcert_buf = client_der,
        .clientcert_bytes = client_der_len,
        .clientkey_buf = private_der,
        .clientkey_bytes = private_der_len,
        .timeout_ms = 10000,
        .skip_common_name = !CONFIG_TEDDYBOX_OFFICIAL_CLOUD};

    receive_buffer = malloc(HTTP_RECEIVE_SIZE);
    if (!receive_buffer)
    {
        ESP_LOGE(TAG, "Allocation failed...");
        goto exit;
    }

    tls = esp_tls_init();
    if (!tls)
        goto exit;
    if (esp_tls_conn_new_sync(req->host, strlen(req->host), req->port, &cfg, tls) != 1)
    {
        int tls_code = 0, verify_flags = 0;
        esp_err_t error = esp_tls_get_and_clear_last_error(tls->error_handle, &tls_code, &verify_flags);
        ESP_LOGE(TAG, "Connection failed: %s, TLS error -0x%04x, certificate flags 0x%x, time %ld",
                 esp_err_to_name(error), tls_code < 0 ? -tls_code : tls_code,
                 verify_flags, (long)time(NULL));
#ifdef CONFIG_ESP_TLS_USING_MBEDTLS
        if (verify_flags)
        {
            char reason[256];
            mbedtls_x509_crt_verify_info(reason, sizeof(reason), "", verify_flags);
            ESP_LOGE(TAG, "Certificate verification: %s", reason);
        }
#endif
        goto exit;
    }

    ESP_LOGI(TAG, "Connection to '%s' established...", req->host);

    /* Short socket polls make cancellation responsive; the progress deadline
     * allows a slow server up to 15 seconds without advancing the transfer. */
    struct timeval timeout = {.tv_sec = 1};
    if (setsockopt(tls->sockfd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0 ||
        setsockopt(tls->sockfd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0)
        goto exit;
    int64_t last_progress = esp_timer_get_time();
    size_t written_bytes = 0;
    size_t request_len = strlen(request);
    do
    {
        if ((req->abort && __atomic_load_n(req->abort, __ATOMIC_ACQUIRE)) ||
            esp_timer_get_time() - last_progress >= 15000000)
            goto exit;
        int ret = esp_tls_conn_write(tls, request + written_bytes, request_len - written_bytes);
        if (ret > 0)
        {
            written_bytes += ret;
            last_progress = esp_timer_get_time();
            ESP_ERROR_CHECK(esp_task_wdt_reset());
        }
        else if (ret != ESP_TLS_ERR_SSL_WANT_READ && ret != ESP_TLS_ERR_SSL_WANT_WRITE)
        {
            ESP_LOGE(TAG, "esp_tls_conn_write returned: [0x%02X](%s)", ret, esp_err_to_name(ret));
            goto exit;
        }
        else
            vTaskDelay(pdMS_TO_TICKS(10));
    } while (written_bytes < request_len);

    bool connected = true;

    ret = ESP_OK;

    while (connected)
    {
        if ((req->abort && __atomic_load_n(req->abort, __ATOMIC_ACQUIRE)) ||
            esp_timer_get_time() - last_progress >= 15000000)
        {
            ret = ESP_FAIL;
            break;
        }
        int len = esp_tls_conn_read(tls, receive_buffer, HTTP_RECEIVE_SIZE);

        if (len == ESP_TLS_ERR_SSL_WANT_WRITE || len == ESP_TLS_ERR_SSL_WANT_READ || len == MBEDTLS_ERR_SSL_TIMEOUT)
        {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        if (len < 0)
        {
            ESP_LOGE(TAG, "esp_tls_conn_read returned [-0x%02X](%s)", -len, esp_err_to_name(len));
            ret = ESP_FAIL;
            connected = false;
            continue;
        }

        if (len == 0)
        {
            connected = false;
            continue;
        }

        last_progress = esp_timer_get_time();
        ESP_ERROR_CHECK(esp_task_wdt_reset());
        if (req->data_received_cbr)
        {
            esp_err_t cbr_ret = req->data_received_cbr(req->data_received_ctx, receive_buffer, len);

            switch (cbr_ret)
            {
            case ESP_OK:
                break;

            case ESP_FAIL:
                ret = ESP_FAIL;
                connected = false;
                continue;

            case CBR_CLOSE_OK:
                connected = false;
                continue;
            }
        }
    }

    ESP_LOGI(TAG, "connection closed");
    if (req->connection_closed_cbr)
    {
        if (req->connection_closed_cbr(req->connection_closed_ctx) != ESP_OK)
            ret = ESP_FAIL;
    }

exit:
    if (tls)
        esp_tls_conn_delete(tls);

    free(auth_line);
    free(range_line);
    free(request);
    free(url);
    free(receive_buffer);

    return ret;
}

/********************************************************/
/* time request handler                                 */
/********************************************************/

typedef struct
{
    char text[33];
    size_t length;
} cloud_time_response_t;

esp_err_t cloud_set_time_cbr(void *ctx, uint8_t *data, size_t length)
{
    cloud_time_response_t *response = ctx;
    if (length > sizeof(response->text) - 1 - response->length)
        return ESP_FAIL;
    memcpy(response->text + response->length, data, length);
    response->length += length;
    response->text[response->length] = 0;
    return ESP_OK;
}

esp_err_t cloud_set_time(void)
{
    bool needs_network_time = CONFIG_TEDDYBOX_OFFICIAL_CLOUD;
#ifdef CONFIG_MBEDTLS_HAVE_TIME_DATE
    needs_network_time = true;
#endif
    if (needs_network_time)
    {
        /* Expiry validation needs a clock before any HTTPS, including /v1/time. */
        if (!sntp_enabled())
        {
            ESP_LOGI(TAG, "Synchronizing clock via SNTP before HTTPS");
            sntp_setoperatingmode(SNTP_OPMODE_POLL);
            sntp_setservername(0, "pool.ntp.org");
            sntp_init();
        }
        return time(NULL) >= 1704067200 ? ESP_OK : ESP_FAIL;
    }

    cloud_time_response_t response = {0};
    http_parser_t http_parser_handler_ctx = {
        .ctx = &response,
        .http_data_cbr = &cloud_set_time_cbr,
        .header_buffer = malloc(MAX_HTTP_HEADER_SIZE)};
    cloud_req_t req = {
        .host = CLOUD_HOST,
        .port = CLOUD_PORT,
        .path = "/v1/time",
        .data_received_cbr = &http_parser_received_cbr,
        .data_received_ctx = &http_parser_handler_ctx};

    esp_err_t ret = cloud_request(&req);

    free(http_parser_handler_ctx.header_buffer);
    if (ret != ESP_OK || http_parser_closed_cbr(&http_parser_handler_ctx) != ESP_OK)
        return ESP_FAIL;
    char *end;
    long timestamp = strtol(response.text, &end, 10);
    if (timestamp < 1704067200 || (*end && strcmp(end, "\n") && strcmp(end, "\r\n")))
        return ESP_FAIL;
    struct timeval tv = {.tv_sec = timestamp};
    return settimeofday(&tv, NULL) == 0 ? ESP_OK : ESP_FAIL;
}

/********************************************************/
/* content download handler                             */
/********************************************************/

esp_err_t cloud_create_directories(const char *file)
{
    char *tmp = strdup(file);
    if (!tmp)
        return ESP_ERR_NO_MEM;
    char *p = tmp;

    ESP_LOGI(TAG, "[CDL] Create recursively dirs for '%s'", file);
    while ((p = strchr(p + 1, '/')))
    {
        *p = 0;
        ESP_LOGI(TAG, "[CDL]   Create '%s'", tmp);
        mkdir(tmp, S_IRWXU);
        *p = '/';
    }

    free(tmp);
    return ESP_OK;
}

esp_err_t cloud_content_status_cbr(void *ctx, int status_code)
{
    cloud_content_req_t *req = (cloud_content_req_t *)ctx;

    req->status_code = status_code;

    switch (req->status_code)
    {
    case 200:
        ESP_LOGI(TAG, "[CDL] HTTP %d received", req->status_code);

        __atomic_store_n(&req->state, CC_STATE_CONNECTED, __ATOMIC_RELEASE);
        xSemaphoreGive(req->update_sem);
        return ESP_OK;

    case 206:
        ESP_LOGI(TAG, "[CDL] HTTP %d received, partial content", req->status_code);

        __atomic_store_n(&req->state, CC_STATE_CONNECTED, __ATOMIC_RELEASE);
        xSemaphoreGive(req->update_sem);
        return ESP_OK;

    default:
        ESP_LOGE(TAG, "[CDL] Request failed for '%s': HTTP %d", req->location, req->status_code);

        /* The worker publishes failure after releasing the TLS/parser context. */
        return ESP_FAIL;
    }
}

esp_err_t cloud_content_length_cbr(void *ctx, size_t content_range, size_t content_length)
{
    cloud_content_req_t *req = (cloud_content_req_t *)ctx;

    if ((req->status_code == 206 && content_range != req->received) ||
        content_length > UINT32_MAX - content_range)
        return ESP_FAIL;
    req->received = content_range;
    req->content_length = content_range + content_length;

    ESP_LOGI(TAG, "[CDL] download %d-%d to '%s'", req->received, req->content_length, req->filename);

    if (req->content_length == 0)
    {
        ESP_LOGE(TAG, "[CDL] Zero-sized download, aborting");
        return ESP_FAIL;
    }

    if (cloud_create_directories(req->filename) != ESP_OK)
        return ESP_FAIL;
    req->handle = fopen(req->filename, content_range ? "rb+" : "wb+");
    if (!req->handle)
    {
        ESP_LOGE(TAG, "[CDL] Failed to create '%s'", req->filename);
        return ESP_FAIL;
    }

    __atomic_store_n(&req->state, CC_STATE_RECEIVING, __ATOMIC_RELEASE);

    return ESP_OK;
}

esp_err_t cloud_content_cbr(void *ctx, uint8_t *data, size_t length)
{
    cloud_content_req_t *req = (cloud_content_req_t *)ctx;

    if (req->content_length == 0 || !req->handle)
    {
        ESP_LOGE(TAG, "[CDL] Nothing allocated... aborting");
        return ESP_FAIL;
    }

    if (__atomic_load_n(&req->abort, __ATOMIC_ACQUIRE))
    {
        __atomic_store_n(&req->state, CC_STATE_ABORTED, __ATOMIC_RELEASE);
        ESP_LOGE(TAG, "[CDL] aborting requested");
        return ESP_FAIL;
    }

    if (xSemaphoreTake(req->file_sem, pdMS_TO_TICKS(2000)) != pdTRUE)
    {
        ESP_LOGE(TAG, "[CDL] File lock timed out");
        return ESP_FAIL;
    }
    if (fseek(req->handle, req->received, SEEK_SET) != 0 ||
        fwrite(data, 1, length, req->handle) != length || fflush(req->handle) != 0)
    {
        xSemaphoreGive(req->file_sem);
        ESP_LOGE(TAG, "[CDL] Write failed, bailing out");
        return ESP_FAIL;
    }
    __atomic_add_fetch(&req->received, length, __ATOMIC_RELEASE);
    xSemaphoreGive(req->file_sem);
    xSemaphoreGive(req->update_sem);

    /* give others the chance to react */
    vTaskDelay(1 / portTICK_RATE_MS);

    time_t current = time(NULL);
    static time_t start = 0;
    static time_t last = 0;
    if (start == 0)
    {
        start = current;
    }

    if (last != current)
    {
        last = current;

        float elapsed = difftime(current, start);
        if (elapsed <= 0)
            return ESP_OK;
        float speed = (req->received / elapsed) / 1024.0; // KiB/s
        float percent_complete = (req->received * 100.0f / req->content_length);
        float eta = ((req->content_length - req->received) / (req->received / elapsed));

        ESP_LOGI(TAG, "[CDL] %d bytes received (%2.2f%%), Speed: %2.2f KiB/s, ETA: %2.2f s", req->received, percent_complete, speed, eta);
    }
    return ESP_OK;
}

esp_err_t cloud_process_request(cloud_content_req_t *content_req)
{
    ESP_LOGI(TAG, "[CDL] Request for '%s' -> '%s'", content_req->location, content_req->filename);

    http_parser_t http_parser_handler_ctx = {
        .http_status_cbr = &cloud_content_status_cbr,
        .http_data_cbr = &cloud_content_cbr,
        .content_length_cbr = &cloud_content_length_cbr,
        .ctx = content_req,
        .header_buffer = malloc(MAX_HTTP_HEADER_SIZE)};

    cloud_req_t req = {
        .host = CLOUD_HOST,
        .port = CLOUD_PORT,
        .path = content_req->location,
        .auth = content_req->auth,
        .abort = &content_req->abort,
        .range_start = content_req->received,
        .data_received_cbr = &http_parser_received_cbr,
        .data_received_ctx = &http_parser_handler_ctx,
        .connection_closed_cbr = &http_parser_closed_cbr,
        .connection_closed_ctx = &http_parser_handler_ctx};

    esp_err_t ret = cloud_request(&req);

    free(http_parser_handler_ctx.header_buffer);
    if (content_req->handle)
    {
        if (xSemaphoreTake(content_req->file_sem, pdMS_TO_TICKS(2000)) == pdTRUE)
        {
            if (fflush(content_req->handle) != 0 || fsync(fileno(content_req->handle)) != 0)
                ret = ESP_FAIL;
            xSemaphoreGive(content_req->file_sem);
        }
        else
            ret = ESP_FAIL;
    }
    /* A shared FatFS stream stays open until playback and worker both release
     * it. Publish terminal state only after the worker's writes have stopped. */
    cloud_content_state_t state = ret == ESP_OK &&
        !__atomic_load_n(&content_req->abort, __ATOMIC_ACQUIRE) &&
        content_req->received == content_req->content_length && content_req->content_length > 0
        ? CC_STATE_FINISHED : CC_STATE_ERROR;
    __atomic_store_n(&content_req->state, state, __ATOMIC_RELEASE);
    cloud_content_cleanup(content_req); /* close before announcing the writer idle */
    __atomic_sub_fetch(&active_requests, 1, __ATOMIC_RELEASE);
    return ret;
}

/********************************************************/
/* cloud request code, called from external code        */
/********************************************************/

cloud_content_req_t *cloud_content_download(uint64_t nfc_uid, const uint8_t *nfc_token)
{
    if (!cloud_ready || !nfc_uid || !nfc_token)
        return NULL;
    cloud_content_req_t *req = calloc(1, sizeof(cloud_content_req_t));
    if (!req)
        return NULL;

    ESP_LOGI(TAG, "[CDL] Queue request for UID %llX", nfc_uid);

    /* fill input fields */
    req->references = 1;
    req->nfc_uid = nfc_uid;

    /* fill working variables based on input fields */
    __atomic_store_n(&req->state, CC_STATE_INIT, __ATOMIC_RELEASE);
    req->update_sem = xSemaphoreCreateBinary();
    req->file_sem = xSemaphoreCreateMutex();
    req->status_code = 0;
    req->content_length = 0;
    req->received = 0;
    req->location = malloc(64);
    req->filename = pb_build_filename(nfc_uid);
    req->auth = malloc(65);

    if (!req->update_sem || !req->file_sem || !req->location || !req->filename || !req->auth)
    {
        cloud_content_cleanup(req);
        return NULL;
    }

    /* when the file already exists, do a partial download */
    struct stat st;
    if (stat(req->filename, &st) == 0)
    {
        req->received = st.st_size;
        ESP_LOGI(TAG, "[CDL] Partial file, continue at %d", req->received);
    }


    if (nfc_token)
    {
        char *auth_ptr = req->auth;
        for (int pos = 0; pos < 32; pos++)
        {
            auth_ptr += sprintf(auth_ptr, "%02X", nfc_token[pos]);
        }
    }
    else
    {
        strcpy(req->auth, "(error)");
    }

    if (nfc_uid)
    {
        char *location_ptr = req->location;
        location_ptr += sprintf(req->location, "/v2/content/");

        for (int i = 0; i < 8; ++i)
        {
            uint8_t byte = (req->nfc_uid >> (i * 8)) & 0xFF;
            /* TeddyCloud's custom-tag UID check expects lowercase hex. */
            location_ptr += sprintf(location_ptr, "%02x", byte);
        }
    }
    else
    {
        strcpy(req->filename, "(error)");
    }

    __atomic_add_fetch(&active_requests, 1, __ATOMIC_RELEASE);
    __atomic_add_fetch(&req->references, 1, __ATOMIC_RELAXED);
    if (xQueueSend(cloud_request_queue, &req, pdMS_TO_TICKS(250)) != pdTRUE)
    {
        __atomic_sub_fetch(&active_requests, 1, __ATOMIC_RELEASE);
        cloud_content_cleanup(req); /* worker never accepted ownership */
        cloud_content_cleanup(req);
        return NULL;
    }

    return req;
}

cloud_content_state_t cloud_content_get_state(cloud_content_req_t *req)
{
    return __atomic_load_n(&req->state, __ATOMIC_ACQUIRE);
}

bool cloud_is_busy(void)
{
    return __atomic_load_n(&active_requests, __ATOMIC_ACQUIRE) != 0;
}

uint32_t cloud_content_received(cloud_content_req_t *req)
{
    return __atomic_load_n(&req->received, __ATOMIC_ACQUIRE);
}

void cloud_content_cleanup(cloud_content_req_t *req)
{
    if (__atomic_sub_fetch(&req->references, 1, __ATOMIC_ACQ_REL) != 0)
        return;
    if (req->handle && fclose(req->handle) != 0)
        ESP_LOGE(TAG, "Failed to close downloaded file");
    if (req->update_sem)
        vSemaphoreDelete(req->update_sem);
    if (req->file_sem)
        vSemaphoreDelete(req->file_sem);
    free(req->location);
    free(req->auth);
    free(req->filename);
    free(req);
}

/********************************************************/
/* certificate loading                                  */
/********************************************************/

esp_err_t cloud_load_cert(const char *path, uint8_t **ptr, size_t *length)
{
    struct stat st;
    if (stat(path, &st) != 0)
    {
        ESP_LOGE(TAG, "Certificate %s not found", path);
        return ESP_FAIL;
    }

    if (st.st_size <= 0)
        return ESP_FAIL;
    uint8_t *buffer = malloc(st.st_size + 1);
    if (!buffer)
        return ESP_ERR_NO_MEM;
    FILE *ca = fopen(path, "rb");
    if (!ca)
    {
        free(buffer);
        return ESP_FAIL;
    }
    bool loaded = fread(buffer, 1, st.st_size, ca) == (size_t)st.st_size;
    fclose(ca);
    if (!loaded)
    {
        free(buffer);
        return ESP_FAIL;
    }
    buffer[st.st_size] = 0;
    *ptr = buffer;
    *length = st.st_size;
    /* mbedTLS requires the terminator to be included for PEM input. */
    if (st.st_size >= 10 && !memcmp(buffer, "-----BEGIN", 10))
        ++*length;
    ESP_LOGI(TAG, "Loaded '%s' with %zu bytes", path, *length);

    return ESP_OK;
}

static void cloud_task(void *ctx)
{
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    bool clock_ready = false;
    TickType_t last_clock_attempt = 0;
    bool clock_attempted = false;
    while (true)
    {
        ESP_ERROR_CHECK(esp_task_wdt_reset());
        bool connected = wifi_is_connected();
        TickType_t now = xTaskGetTickCount();
        if (connected && !clock_ready &&
            (!clock_attempted || now - last_clock_attempt >= pdMS_TO_TICKS(1000)))
        {
            clock_ready = cloud_set_time() == ESP_OK;
            last_clock_attempt = xTaskGetTickCount();
            clock_attempted = true;
        }

        cloud_content_req_t *req;
        if (xQueuePeek(cloud_request_queue, &req, pdMS_TO_TICKS(100)) != pdTRUE)
            continue;
        bool cancelled = __atomic_load_n(&req->abort, __ATOMIC_ACQUIRE);
        if (!cancelled && (!connected || !clock_ready))
        {
            /* Keep the request queued through Wi-Fi/SNTP startup. Playback's
             * finite startup deadline can still cancel it while offline. */
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        if (xQueueReceive(cloud_request_queue, &req, 0) != pdTRUE)
            continue;
        if (cancelled)
        {
            __atomic_store_n(&req->state, CC_STATE_ERROR, __ATOMIC_RELEASE);
            cloud_content_cleanup(req);
            __atomic_sub_fetch(&active_requests, 1, __ATOMIC_RELEASE);
            continue;
        }
        cloud_process_request(req);
    }
}

void cloud_init(void)
{
    esp_log_level_set(TAG, ESP_LOG_INFO);

    esp_ota_get_partition_description(esp_ota_get_running_partition(), &running_app_info);

    cloud_request_queue = xQueueCreate(4, sizeof(cloud_content_req_t *));

    ESP_LOGI(TAG, "Loading certificates");
    if (cloud_load_cert(CONFIG_TEDDYBOX_CERT_DIRECTORY "/ca.der", &ca_der, &ca_der_len) != ESP_OK ||
        cloud_load_cert(CONFIG_TEDDYBOX_CERT_DIRECTORY "/client.der", &client_der, &client_der_len) != ESP_OK ||
        cloud_load_cert(CONFIG_TEDDYBOX_CERT_DIRECTORY "/private.der", &private_der, &private_der_len) != ESP_OK)
    {
        ESP_LOGE(TAG, "Cloud credentials unavailable");
        return;
    }

    if (cloud_request_queue && xTaskCreate(&cloud_task, "[TB] cloud", 5000, NULL, 5, NULL) == pdPASS)
        cloud_ready = true;
}
