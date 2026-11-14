"""Test production tag-token reads and tag-change dispatch with a fake reader.
Run: python3 tests/test_nfc.py (requires a C compiler).
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'main/nfc.c').read_text()

def section(start, end):
    return source[source.index(start):source.index(end, source.index(start))]

preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "nfc.h"
typedef int trf7962a_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
static bool nfc_valid, nfc_token_valid;
static uint8_t nfc_current_uid_rev[8], nfc_current_token[32];
static uint8_t received_data[256], received_length;
static uint8_t expected_uid[8], expected_token[32], published_token[32];
static int failure_block = -1, failure_kind, reads, plays, downloads;
static uint64_t published_uid;
static esp_err_t trf7962a_xmit(trf7962a_t trf, uint8_t *tx, uint8_t len,
                             uint8_t *rx, uint8_t *rx_len) {
    assert(len == 11 && tx[0] == 0x22 && tx[1] == 0x20);
    assert(!memcmp(tx + 2, expected_uid, 8));
    assert(tx[10] == reads % 8);
    ++reads;
    memset(rx, 0, 7); *rx_len = 7;
    memcpy(rx + 1, expected_token + tx[10] * 4, 4);
    if (tx[10] == failure_block) {
        if (failure_kind == 0) return ESP_FAIL;
        if (failure_kind == 1) *rx_len = 6;
        if (failure_kind == 2) rx[0] = 1;
    }
    return ESP_OK;
}
static void pb_play_content(uint64_t uid) { ++plays; assert(uid != 0); }
static void pb_play_content_token(uint64_t uid, uint8_t *token) {
    assert(token); ++downloads; published_uid = uid;
    memcpy(published_token, token, 32);
}
static void pb_stop(void) {}
'''
harness = r'''
int main(void) {
    const uint8_t uid[] = {0x80, 0x26, 0x85, 0x12, 0x00, 0x03, 0x48, 0xE0};
    memcpy(expected_uid, uid, 8);
    for (unsigned i = 0; i < 32; ++i) expected_token[i] = i + 1;
    uint8_t token[32];
    for (int block = 0; block < 8; ++block) {
        for (int kind = 0; kind < 3; ++kind) {
            reads = 0; failure_block = block; failure_kind = kind;
            memset(token, 0xA5, sizeof(token));
            assert(nfc_read_token(0, uid, token) == ESP_FAIL);
            for (unsigned i = 0; i < sizeof(token); ++i) assert(token[i] == 0xA5);
            assert(reads == block + 1);
        }
    }
    failure_block = -1; reads = 0;
    assert(nfc_read_token(0, uid, token) == ESP_OK);
    assert(reads == 8 && !memcmp(token, expected_token, 32));
    assert(nfc_get_current_token() == NULL);
    reads = 0;
    nfc_handle_tag(0, uid);
    assert(plays == 1 && downloads == 1 && reads == 8);
    assert(published_uid == UINT64_C(0xE048030012852680));
    assert(!memcmp(published_token, expected_token, 32));
    nfc_handle_tag(0, uid);
    assert(plays == 1 && downloads == 1 && reads == 8);
    /* A changed tag starts fresh, even if its memory read fails halfway. */
    expected_uid[0] = 0x81; expected_token[0] = 0x55;
    failure_block = 4; reads = 0;
    nfc_handle_tag(0, expected_uid);
    assert(plays == 2 && downloads == 1 && nfc_get_current_token() == NULL);
    for (unsigned i = 0; i < 32; ++i) assert(nfc_current_token[i] == 0);
    failure_block = -1; reads = 0;
    nfc_handle_tag(0, expected_uid);
    assert(plays == 2 && downloads == 2 && reads == 8);
    assert(published_uid == UINT64_C(0xE048030012852681));
    assert(!memcmp(published_token, expected_token, 32));
    puts("NFC token regression checks passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'esp_err.h').write_text('#pragma once\ntypedef int esp_err_t;\n')
    (path / 'test.c').write_text(preamble +
        section('uint64_t nfc_get_current_uid()', 'void nfc_dump') +
        section('static esp_err_t nfc_read_token', 'esp_err_t nfc_reset') +
        section('static void nfc_play()', 'void nfc_mainthread') + harness)
    subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Wno-unused-parameter',
                    '-Wno-unused-function', '-fsanitize=address,undefined', '-g',
                    '-I', str(path), '-I', str(root / 'main'), str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], cwd=directory,
                   env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, check=True)
