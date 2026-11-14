#!/bin/bash

# Check if the script is sourced
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "Error: This script should be sourced, not executed directly."
    echo "  Execute: '. ${0}' in your shell"
    exit 1
fi

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

echo "[ENV] Setting ADF/IDF paths"
export ADF_PATH="$repo_dir/esp-adf"
export IDF_PATH="$ADF_PATH/esp-idf"

# ESP-IDF 4.4 does not support current system Python releases. Prefer the
# environment created by its install script, even if another IDF was sourced
# earlier in this shell.
idf_tools_dir=${IDF_TOOLS_PATH:-"$HOME/.espressif"}
for idf_python in "$idf_tools_dir"/python_env/idf4.4_py*_env/bin; do
    if [[ -x "$idf_python/python" ]]; then
        export PATH="$idf_python:$PATH"
        unset IDF_PYTHON_ENV_PATH
        break
    fi
done

# CMake 4 removed the legacy policy default used by IDF 4.4's mbedTLS.
export CMAKE_POLICY_VERSION_MINIMUM=3.5

echo "[ENV] Exporting IDF vars"
. "$IDF_PATH/export.sh"

unset idf_python idf_tools_dir repo_dir

echo "[ENV] Done"
