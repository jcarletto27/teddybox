#!/bin/bash
echo "[SETUP] Install ESP32-ADF"
git clone -b v2.4 --depth 1 --shallow-submodules --recursive https://github.com/espressif/esp-adf.git || exit 0
cd esp-adf
export ADF_PATH=$PWD
git apply ../patches/esp-adf-sdcard-width.diff
cd ${ADF_PATH}/esp-idf
git apply ../../patches/malloc.diff
./install.sh
. ./export.sh
