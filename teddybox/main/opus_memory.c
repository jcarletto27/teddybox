#include <stdbool.h>
#include <stdint.h>
#include "esp_heap_caps.h"
#include "esp_err.h"
#include "esp_log.h"
#include "opus_memory.h"

/* Public libopus multistream APIs, supplied by the installed libesp_codec.a.
 * Reserve the largest mono/stereo state before network tasks fragment SRAM.
 * The application has one Opus decoder; the workspace survives track changes. */
typedef struct OpusMSDecoder OpusMSDecoder;
int opus_multistream_decoder_get_size(int streams, int coupled_streams);
int opus_multistream_decoder_init(OpusMSDecoder *state, int32_t rate, int channels,
                                 int streams, int coupled_streams, const unsigned char *mapping);

OpusMSDecoder *__real_opus_multistream_decoder_create(int32_t rate, int channels,
        int streams, int coupled_streams, const unsigned char *mapping, int *error);
void __real_opus_multistream_decoder_destroy(OpusMSDecoder *state);

static void *workspace;
static size_t workspace_size;
static bool claimed;

void opus_memory_init(void)
{
    if (workspace)
        return;
    int mono = opus_multistream_decoder_get_size(1, 0);
    int stereo = opus_multistream_decoder_get_size(1, 1);
    ESP_ERROR_CHECK(mono > 0 && stereo > 0 ? ESP_OK : ESP_ERR_INVALID_SIZE);
    workspace_size = mono > stereo ? mono : stereo;
    workspace = heap_caps_malloc(workspace_size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ESP_ERROR_CHECK(workspace ? ESP_OK : ESP_ERR_NO_MEM);
    esp_log_level_set("Opus", ESP_LOG_INFO);
    ESP_LOGI("Opus", "Reserved %u bytes for decoder state", (unsigned)workspace_size);
}

OpusMSDecoder *__wrap_opus_multistream_decoder_create(int32_t rate, int channels,
        int streams, int coupled_streams, const unsigned char *mapping, int *error)
{
    /* Preserve the library's normal path for other multistream layouts. */
    if (streams != 1 || channels > 2)
        return __real_opus_multistream_decoder_create(rate, channels, streams,
                                                      coupled_streams, mapping, error);
    int result = -7; /* OPUS_ALLOC_FAIL */
    int size = opus_multistream_decoder_get_size(streams, coupled_streams);
    if (size <= 0 || channels < 1 || channels > 2)
        result = -1; /* OPUS_BAD_ARG */
    else if (workspace && (size_t)size <= workspace_size &&
             !__atomic_exchange_n(&claimed, true, __ATOMIC_ACQ_REL))
    {
        result = opus_multistream_decoder_init(workspace, rate, channels, streams,
                                               coupled_streams, mapping);
        if (result == 0)
        {
            if (error)
                *error = 0;
            return workspace;
        }
        __atomic_store_n(&claimed, false, __ATOMIC_RELEASE);
    }
    if (error)
        *error = result;
    return NULL;
}

void __wrap_opus_multistream_decoder_destroy(OpusMSDecoder *state)
{
    if (state == workspace)
        __atomic_store_n(&claimed, false, __ATOMIC_RELEASE);
    else if (state)
        __real_opus_multistream_decoder_destroy(state);
}
