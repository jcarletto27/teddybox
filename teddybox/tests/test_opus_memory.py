"""Check production Opus workspace ownership without a runtime heap allocation."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = '\n'.join(line for line in (root / 'main/opus_memory.c').read_text().splitlines()
                   if not line.startswith('#include'))
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define ESP_OK 0
#define ESP_ERR_INVALID_SIZE 1
#define ESP_ERR_NO_MEM 2
#define ESP_ERROR_CHECK(call) assert((call) == ESP_OK)
#define ESP_LOGI(...) ((void)0)
#define esp_log_level_set(...) ((void)0)
#define ESP_LOG_INFO 1
#define MALLOC_CAP_INTERNAL 1
#define MALLOC_CAP_8BIT 2
static unsigned allocations, initializations;
static union { uint64_t alignment; unsigned char bytes[30000]; } storage;
static void *heap_caps_malloc(size_t size, unsigned caps) {
    assert(++allocations == 1 && size == sizeof(storage.bytes));
    assert(caps == (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    return storage.bytes;
}
'''
harness = r'''
static unsigned foreign_destroyed;
static unsigned char foreign_state[8];
OpusMSDecoder *__real_opus_multistream_decoder_create(int32_t rate, int channels,
        int streams, int coupled, const unsigned char *mapping, int *error) {
    assert(streams == 2); if (error) *error = 0; return (OpusMSDecoder *)foreign_state;
}
void __real_opus_multistream_decoder_destroy(OpusMSDecoder *state) {
    assert((void *)state == foreign_state); ++foreign_destroyed;
}
int opus_multistream_decoder_get_size(int streams, int coupled) {
    if (streams != 1 || coupled < 0 || coupled > 1) return 0;
    return coupled ? 30000 : 18000;
}
int opus_multistream_decoder_init(OpusMSDecoder *state, int32_t rate, int channels,
        int streams, int coupled, const unsigned char *mapping) {
    ++initializations;
    if (rate != 48000 || !mapping) return -1;
    memset(state, 0xAB, opus_multistream_decoder_get_size(streams, coupled));
    return 0;
}
int main(void) {
    const unsigned char mapping[] = {0, 1};
    int error;
    assert(!__wrap_opus_multistream_decoder_create(48000, 2, 1, 1, mapping, &error) && error == -7);
    opus_memory_init(); opus_memory_init(); assert(allocations == 1);
    OpusMSDecoder *first = __wrap_opus_multistream_decoder_create(48000, 2, 1, 1, mapping, &error);
    assert(first && !error && initializations == 1);
    assert(!__wrap_opus_multistream_decoder_create(48000, 2, 1, 1, mapping, &error) && error == -7);
    __wrap_opus_multistream_decoder_destroy(NULL);
    assert(!__wrap_opus_multistream_decoder_create(48000, 2, 1, 1, mapping, &error));
    __wrap_opus_multistream_decoder_destroy(first);
    assert(!__wrap_opus_multistream_decoder_create(123, 2, 1, 1, mapping, &error) && error == -1);
    OpusMSDecoder *mono = __wrap_opus_multistream_decoder_create(48000, 1, 1, 0, mapping, NULL);
    assert(mono == first); __wrap_opus_multistream_decoder_destroy(mono);
    for (int track = 0; track < 100; ++track) {
        OpusMSDecoder *state = __wrap_opus_multistream_decoder_create(48000, 2, 1, 1, mapping, &error);
        assert(state == first && !error); __wrap_opus_multistream_decoder_destroy(state);
    }
    assert(allocations == 1); /* No large allocation under TLS or across tracks. */
    OpusMSDecoder *foreign = __wrap_opus_multistream_decoder_create(48000, 2, 2, 0, mapping, &error);
    assert((void *)foreign == foreign_state && !error);
    __wrap_opus_multistream_decoder_destroy(foreign); assert(foreign_destroyed == 1);
    puts("Opus reserved-workspace regression checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(preamble + source + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-g', str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], cwd=directory,
                   env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, check=True)
