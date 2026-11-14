Ear-button recovery
===================

After flashing this firmware, release both ears, then squeeze and hold both
continuously for five seconds to request an emergency software restart.
Releasing either ear before five seconds cancels the gesture. Both ears must
have been released together once since boot; this prevents repeated restarts
if they remain held during reboot. Single-ear volume controls still work.

A separate task checks the buttons without using playback, SD-card or I2C
operations, allowing recovery when those operations block the main loop.
It still requires the scheduler and GPIO reads to work. A complete CPU lockup
or disabled interrupts may require a physical power cycle.

This is an emergency reset: ongoing downloads/writes may be interrupted. Use
normal tag removal and idle shutdown when the box is responding. The gesture
does not resolve the underlying freeze; retain serial output from immediately
before the freeze to diagnose it.

Validation: `python3 tests/test_ear_restart.py` exercises the production task
with simulated button states and time, including short holds, interrupted
holds, single-ear presses and both ears held through boot.

Automatic recovery and crash dumps
----------------------------------

The main, playback, NFC and cloud tasks subscribe to the ESP-IDF task watchdog with
a 30-second timeout and panic/reboot enabled. The panic handler prints diagnostics
and reboots after saving a dump (`CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT=y`). Each task feeds its own watchdog
while it makes progress. A healthy idle task cannot hide a blocked playback
command handler. Playback also detects 45 seconds without decoder input progress;
a pipeline that cannot stop/terminate within bounded waits enters the panic
handler rather than freeing memory still in use.

Cloud transport gives up after 15 seconds without network progress. Download
startup is polled by the playback command loop, so removing a tag can cancel a
pending download immediately. Shared stream mutex waits are limited to two
seconds. Reference counting keeps requests/streams alive until both playback
and cloud worker have released them, including cancellation before connection.

The playback/download implementation retains a shared FatFS stream under that mutex:
FatFS caches file size in each open handle, so opening an independent reader
while a writer grows the file can leave the reader permanently at the old EOF.
Flushed byte counts limit what the decoder reads; final fsync completes before
the worker announces that its writes have stopped. Shutdown acknowledges stopped
playback and waits for cloud writers before unmounting the SD card.

Panic dumps are saved in the existing 128 KiB `coredump` partition. Startup logs
announce a saved dump. Keep the exact `build/teddybox.elf` and `build/teddybox.bin`
that you flashed; rebuilding replaces the ELF needed to decode that crash.
Close the serial monitor, source `../env_vars.sh`, then use this IDF 4.4 tool:

```sh
python "$IDF_PATH/components/espcoredump/espcoredump.py" \
  --chip esp32s3 --port /dev/ttyACM1 \
  info_corefile --save-core /tmp/teddybox-crash.elf build/teddybox.elf
```

Use your actual serial port. This IDF version does not provide
`idf.py coredump-info`. A new panic can overwrite the previous dump. Dumps can
contain credentials and audio data from RAM; share the decoded backtrace and
relevant logs rather than uploading the whole dump publicly. Hardware power
loss cannot produce a software panic dump.

Battery and sleep
-----------------

On board revision 1.6.C, GPIO9 measures the pack through R57/R58 (100k/33k),
and GPIO8 measures the charger rail through a 100k/100k divider. The board
layer uses calibrated ADC1 conversions and averages 32 samples. If calibration
is unavailable, it reports that monitoring is disabled rather than using an
unverified conversion to shut down the box.

For the original three-cell NiMH pack, five consecutive one-second readings
at or below 3.6 V log a warning; five at or below 3.3 V request orderly shutdown.
The warning clears above 3.7 V or when docked. A charger rail at least 4.0 V
inhibits low-battery shutdown. Invalid readings reset the consecutive-sample
counters. These readings are voltages, not a battery percentage or charge control.
Check the first logged voltages against a meter; board-level ADC behavior and
actual cutoff still require testing on your box.

GPIO7 remains low on the charger. Sleep excludes already-active inputs and,
using IDF 4.4's EXT0/EXT1 wake modes, retains ear-button wake while docked. If all
wake inputs are held low at shutdown, a one-minute timer provides a retry.
Peripheral power outputs remain held off in sleep and are released on boot.
Pending downloads count as activity for idle poweroff.
After 60 seconds without playback, a pending download, or a new ear-button
press, the application requests shutdown. Holding an ear does not continually
reset that timer. Every 30 seconds, `Sleep status` logs the elapsed idle time,
timeout, playback/download busy flag, and both ear inputs (1 means pressed).
Use these alongside `Idle poweroff` and `Entering deep sleep` to distinguish
an inhibited idle timer from a shutdown or wake problem.
Shutdown also unregisters the Wi-Fi reconnect/IP callbacks and stops the radio
after the cloud writer finishes, as required by ESP-IDF before entering sleep.
Look for `Stopping Wi-Fi before sleep` before `Entering deep sleep`.
NFC and accelerometer tasks acknowledge shutdown between bus transactions;
the NFC task leaves the watchdog before exiting. Playback no longer polls the
headset after acknowledging shutdown. The LED task completes its last update
before PWM is stopped. Only then does the board disable peripheral buses and
interrupt inputs, cut peripheral power, and hold the sleep configuration. A
shutdown acknowledgment timeout aborts rather than powering off an active bus.
SD shutdown stops its polling timer and calls the actual filesystem unmount,
which deinitializes the SDMMC host; disabling ADF peripheral events alone does
not unmount it.

Validation
----------

Run all host regression checks with:

```sh
for test in tests/test_*.py; do python3 "$test" || break; done
```

These compile production functions with hardware stubs and address/undefined
behavior sanitizers. They cover voltage conversion/cutoff filtering, cancellation
and stream lifetime, live-file EOF boundaries, sleep input combinations, ear
recovery, HTTP framing and certificate loading. They do not establish hardware
ADC accuracy, actual watchdog reboot timing or successful panic capture on flash.

Board references:
- https://tonies-wiki.revvox.de/docs/wiki/esp32/pinout/
- https://tonies-wiki.revvox.de/docs/wiki/general/battery-power-supply/

Opus and TLS memory
-------------------

The large single-stream mono/stereo Opus decoder state is reserved before
Wi-Fi starts, using the installed library's own size query and init API.
Linker wrappers reuse that workspace across tracks instead of requesting a
large contiguous block while TLS is active. The firmware logs the reserved
size at startup. Other multistream layouts retain the library's native path.
This does not install a global heap allocator or depend on the old SDK malloc
patch. Playback waits for a complete download so decoder and TLS working memory
are not needed simultaneously. Hardware testing must confirm sustained playback.

A tag placed before Wi-Fi or SNTP is ready remains queued rather than failing
immediately. Removing it still cancels the request while offline; playback's
existing finite startup deadline remains in force.

Uncached playback and TLS memory
--------------------------------

This board downloads an uncached or partial title completely before starting
the decoder. The precompiled Ogg parser needs a 16 KiB body buffer, 4 KiB lacing
array and 8 KiB granule array, plus PCM/sync/scratch memory. A previous heap
headroom heuristic still allowed these allocations to fail while TLS was active,
so playback now waits for `CC_STATE_FINISHED`. The cloud worker publishes that
state after `cloud_request` has destroyed the TLS connection and freed its
network buffers. There is no free-heap heuristic or early-playback threshold.

Keep the figure in place until downloading completes. Tag removal still
cancels the download, and partial files can resume on the next placement.
Already-complete local titles start immediately. Download progress counts as
activity, so idle poweroff does not interrupt a healthy download.

If another allocation fails, the log includes the requested size, available
heap and largest free block. Retain those figures and the matching ELF.
