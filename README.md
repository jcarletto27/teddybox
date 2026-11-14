# TeddyBox

Custom firmware for the ESP32-S3 Toniebox, with board support for revision
**1.6.C**. This checkout includes configurable cloud access and certificate
storage, playback/download reliability fixes, battery monitoring, recovery
controls, and an orderly low-power shutdown path.

## Setup and environment

Development has been tested on Linux, including WSL/Ubuntu. From this repository's
root (the directory containing this README):

```sh
./setup_libs.sh
source ./env_vars.sh
cd teddybox
```

The setup script clones ESP-ADF v2.4 and its ESP-IDF dependency, installs the
SDK tools, and applies the repository patches. The SD-card patch corrects the
ADF bus-width calculation so this board uses four-bit SDMMC mode. If `esp-adf`
already exists, the setup script exits at the clone step; check that the patches
are present in an existing installation.

Use Python **3.10** for this older IDF toolchain; the repository includes a
`.python-version` file. Source `env_vars.sh` in each new shell rather than
executing it. It resolves the SDK paths relative to the repository, prefers the
installed IDF 4.4 Python environment, and sets the compatibility policy needed
with CMake 4.

## Configure with menuconfig

Run the following from the `teddybox` application directory after sourcing the
environment:

```sh
idf.py menuconfig
```

Use the arrow keys and Enter to open **TeddyBox**. Space toggles a checkbox;
Enter edits a string or port. Save and exit when finished. The relevant options
are:

| Option | Purpose |
| --- | --- |
| **Use the official Tonie cloud with original certificates** | Select direct official-cloud access instead of TeddyCloud. |
| **Cloud hostname** | Set the server hostname, without `https://` or a URL path. |
| **Cloud HTTPS port** | Set the port of the listener serving box requests. |
| **Cloud certificate directory** | Directory on the device containing all three credential files. |
| **Audio Decoder** | Leave **ESP-OPUS-DECODER** selected for the current playback pipeline. |

The checked-in `sdkconfig` currently selects TeddyCloud at
`teddycloud.jcarletto.com:443`, with certificates in **`/sdcard/cert`**.
Fresh Kconfig defaults use port **8243** for TeddyCloud and certificate directory
**`/spiflash/cert`**. Official-cloud defaults are **`prod.de.tbs.toys:443`**.
Previously saved values survive mode changes: explicitly check the hostname,
port, and certificate directory when switching clouds.

Menuconfig changes require rebuilding and flashing the application:

```sh
idf.py build
```

## Certificates on the SD card

The SD-card certificate path lets you replace credentials without rewriting the
internal assets partition. Create this layout at the SD-card root:

```text
cert/
  ca.der
  client.der
  private.der
CONTENT/
  ...
```

In **TeddyBox → Cloud certificate directory**, select **`/sdcard/cert`**.
All three files are loaded from that directory. Both DER and PEM contents are
accepted under these filenames:

- `ca.der`: CA that trusts the selected cloud server.
- `client.der`: the box's client certificate.
- `private.der`: its matching private key.

After flashing a configuration that uses this path, later certificate changes
only require replacing the SD files while powered off and restarting. Startup
logs show which files were loaded. Keep private keys and backups private.

`/spiflash/cert` remains supported. It refers to the device's internal FAT assets
partition, not a folder on the SD card. App-only flashing does not replace that
partition. The web file server/upload interface is disabled in the current
startup path to conserve RAM.

### TeddyCloud or direct official cloud

For TeddyCloud, use its box-facing HTTPS listener or a reverse proxy configured
to serve `/v2/content/`. The CA must match that listener. Port 443 and port 8243
can serve different certificates or routes; changing the port alone does not
update trust or preserve the box's client identity through a proxy.

For direct official-cloud access, enable official mode, explicitly select
`prod.de.tbs.toys:443`, and use your own box's original CA, client certificate,
and key. Replacement TeddyCloud credentials do not authenticate to the official
cloud. The NFC tag's token is also required for content requests.

To use TeddyCloud as a cache, configure its upstream credentials separately and
enable its `cloud.enabled`, `cloud.enableV2Content`, and `cloud.cacheContent`
settings. An HTTP 404 after a successful TLS connection is a routing/content or
upstream configuration issue. For genuine tags affected by earlier bad requests,
check for an incorrectly saved `nocloud` flag.

TLS verifies the server chain and hostname. When certificate date checks are
enabled, startup synchronizes the clock using SNTP (`pool.ntp.org`, UDP 123)
before HTTPS. See [cloud setup and troubleshooting](teddybox/CLOUD.md) for details.

## Build, flash, and monitor

From `teddybox/`:

```sh
idf.py build
```

For an existing box with this repository's partition table, flash only the app:

```sh
python "$IDF_PATH/components/esptool_py/esptool/esptool.py" \
  -p /dev/ttyACM1 -b 921600 --chip esp32s3 \
  write_flash 0x180000 build/teddybox.bin
idf.py monitor -p /dev/ttyACM1
```

Use your actual serial port and close any monitor before flashing. This app-only
command preserves the existing bootloader, partition table, assets, credentials,
and NVS settings. Keep the matching `build/teddybox.elf` if you need to decode a
crash. `make flash` is a legacy convenience target with a hardcoded tool path and
default serial device; the explicit command above selects the image directly.

## Playback and download changes

- NFC downloads require a successfully read 32-byte tag token. Failed/partial
  reads are retried, and requests use the lowercase UID in tag byte order.
- Complete local titles play immediately. Missing or partial titles download
  completely before playback starts, allowing TLS memory to be released before
  the decoder allocates its working buffers. Keep the figure in place until
  download completion; removing it cancels the request. Partial files can resume.
- Requests placed before Wi-Fi or clock synchronization is ready remain queued
  within the existing startup deadline. Cancellation also works while offline.
- HTTP parsing, response validation, resume handling, partial-file bounds, stream
  ownership, and bounded network/file waits have regression coverage. File users
  release their references before shutdown unmounts storage.
- Opus state is reserved before network startup and reused across tracks to
  reduce large contiguous allocations after heap fragmentation. Allocation
  failures log the requested size, free heap, and largest available block.
- I²S configuration leaves the board's unconnected MCLK unset. The accelerometer
  task stack was increased from 2 KiB to 4 KiB after a logging stack overflow.

## Battery and low-power shutdown

The firmware requests sleep after **60 seconds** without playback, a pending
playback download, or a new ear-button press. A continuously held ear does not
keep resetting the idle timer. `Sleep status` logs every 30 seconds show elapsed
idle time, the timeout, playback/download activity, and both ear inputs.

The shutdown path now:

1. Waits for NFC and accelerometer tasks to stop between bus transactions.
2. Stops playback and waits for the cloud writer to finish releasing its files.
3. Unregisters Wi-Fi reconnect/IP callbacks and stops the radio.
4. Stops SD polling and actually unmounts the filesystem/deinitializes the SDMMC
   host. The previous ADF stop call only disabled event dispatch.
5. Preserves volume and playback position in RTC memory, finishes LED updates,
   stops PWM, and disables peripheral buses and interrupt inputs.
6. Cuts peripheral power, holds the power outputs, and enters deep sleep.

Playback no longer polls the headset/codec after acknowledging shutdown. Failed
shutdown acknowledgments or an active writer prevent cutting power underneath
ongoing I/O and enter the recovery path instead.

Wake setup excludes already-active inputs so the charger-held-low signal does
not immediately wake the box. Ear-button wake is supported while docked. If all
wake inputs are held low, a one-minute timer provides a retry. The idle timeout
is currently a constant in `teddybox/main/config.h`, not a menuconfig option.

Battery monitoring uses calibrated, averaged ADC readings for the original
three-cell NiMH pack. Five consecutive one-second readings at or below **3.6 V**
produce a warning; five at or below **3.3 V** request orderly shutdown. Charger
presence inhibits that cutoff. Invalid readings reset the counters; unavailable
calibration disables voltage-based shutdown. These are voltage thresholds,
not a battery percentage estimate or charging control.

**Validation status:** the standalone probe successfully entered timer-only deep
sleep with peripheral power both ON and OFF on the tested box. The full firmware
previously cold-rebooted at sleep entry; the latest task, SD-host, PWM, and I/O
shutdown changes build and pass host checks, but full-application sleep/wake
behavior still needs device confirmation. See [recovery and sleep details](teddybox/RECOVERY.md).

## Recovery and diagnostics

Release both ears after boot, then hold both continuously for **five seconds**
to request an emergency software restart. Releasing either ear cancels the
hold. This runs separately from playback and I²C operations, but requires a
working scheduler; it can interrupt writes, so use orderly shutdown normally.

Main, playback, NFC, and cloud tasks use a 30-second watchdog. Playback also
checks for decoder stalls. Panics save a dump to the existing `coredump`
partition before rebooting. A startup message about a saved dump may refer to a
previous crash. Decoding instructions are in [RECOVERY.md](teddybox/RECOVERY.md).

The [standalone sleep probe](teddybox/tools/sleep_probe/README.md) isolates sleep
from the application and alternates ten-second timer tests with peripheral
power ON/OFF. It is diagnostic firmware, not the normal player. Flash
`build/teddybox.bin` to restore normal operation after using it.

## Host regression checks

Run from `teddybox/` with Python 3 and a host C compiler:

```sh
for test in tests/test_*.py; do
  python3 "$test" || break
done
```

The checks exercise production functions with hardware stubs, including cloud
framing/startup, NFC tokens, playback cancellation and file ownership, Opus
workspace reuse, battery filtering, idle activity, shutdown ordering/failures,
SD unmount, wake-source selection, ear recovery, and the sleep probe. Several
checks also use address/undefined-behavior sanitizers. Passing host checks does
not establish live cloud compatibility, ADC accuracy, or device power draw.
