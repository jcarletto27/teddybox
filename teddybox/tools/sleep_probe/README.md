Standalone sleep probe for board revision 1.6.C
==============================================

This diagnostic replaces the application temporarily. It initializes no NFC,
accelerometer, audio, Wi-Fi, ADC, or filesystem components. It uses the same IDF
and configuration as the normal application and only controls the documented
peripheral power pins and LED outputs. It never writes the SD card or NVS.

Each test settles for two seconds, then requests ten seconds of timer-only deep
sleep. The first test holds peripheral rails ON; the second holds them OFF.
Successful timer wakes print `PASS` and alternate between these two tests. A
cold reset starts again with the ON test. External wake is disabled during
these tests. Ear buttons do not wake this diagnostic.

From the repository root, after sourcing `../env_vars.sh`, build with:

```sh
python3 tools/sleep_probe/build.py
```

Flash only `build/sleep-probe/teddybox_sleep_probe.bin` at app offset `0x180000`
using the existing app-only flash procedure. Keep the existing bootloader,
partition table, assets, certificates, and NVS. Do not run this project's full
`idf.py flash`. Reflash `build/teddybox.bin` to return to the normal application.

Capture logs from `SLEEP-PROBE`, especially the last test entered and the next
ROM reset reason. This is a diagnostic, not a low-power firmware replacement.
