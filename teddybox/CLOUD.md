Use the original Tonie cloud
===========================

Run `idf.py menuconfig`, open **TeddyBox**, and enable **Use the official
Tonie cloud with original certificates**. Check the hostname and port:
`prod.de.tbs.toys`, port `443`. If you previously saved custom endpoint values,
change them explicitly; Kconfig keeps saved values when switching modes.

Place credentials from your own original box in the configured certificate
directory (default `/spiflash/cert`):

- `ca.der`: original server CA certificate
- `client.der`: original box client certificate
- `private.der`: matching original private key

Both DER and PEM are accepted under these filenames. Keep the private key
private. Replacement TeddyCloud credentials do not authenticate to Tonie.
The current build disables the web file server, so its upload interface is not
available by default. `/spiflash` is the on-device FAT assets partition, not a
folder on the SD card. App flashing does not update files in that partition.
Do not replace your only backup of original files.

For easier certificate updates, use the existing SD-card option:

1. Create a `cert` folder at the SD-card root with `ca.der`, `client.der`, and
   `private.der` (all three are loaded from the same directory).
2. In `idf.py menuconfig` → **TeddyBox** → **Cloud certificate directory**, set
   `/sdcard/cert`.
3. Rebuild and flash the app, then restart. Later certificate-file changes only
   require replacing the SD files while powered off and restarting.

For TeddyCloud, `ca.der` must trust the server certificate served by the chosen
listener or reverse proxy. The client certificate/key identify your box. Keep
TeddyCloud's original upstream Tonie credentials separate. PEM data is accepted
under the `.der` filenames. Treat the SD card as sensitive when it holds the key.

Build with `idf.py build` and flash the resulting application. Official mode
uses SNTP (`pool.ntp.org`, UDP port 123) to set the clock before making verified
HTTPS requests with the original client certificate and key. It checks the
server certificate chain and hostname. Content requests use the existing NFC
UID and tag token; the certificates alone do not grant access to every title.

Disable official mode and restore your custom host/port and credentials to use
TeddyCloud again. Defaults preserve this checkout's endpoint
`teddycloud.jcarletto.com:8243`.

Verification: `python3 tests/test_cloud.py` runs local parser, download-resume,
and certificate-loading checks using the production functions. Live cloud
access requires a device, its matching original credentials, and an authorized
tag; the host checks do not establish cloud interoperability.

TLS startup troubleshooting
---------------------------

When mbedTLS certificate expiry checks are enabled, TeddyCloud mode also uses
SNTP before HTTPS. Fetching `/v1/time` cannot bootstrap a clock required to
validate that HTTPS connection. Allow UDP port 123 to `pool.ntp.org`.

A handshake error `-0x2700` means certificate verification failed. The firmware
logs the verification flags and reason (such as an expired certificate or an
untrusted issuer). Keep `ca.der` matched to the selected server. A public HTTPS
reverse proxy on port 443 may use a different CA than TeddyCloud's client-certificate
listener on port 8243. Use the listener configured for box traffic; changing
ports does not change the loaded trust anchor. Do not disable verification to
work around a CA mismatch.

Content requests returning 404
------------------------------

A completed TLS handshake followed by HTTP 404 is an HTTP/content response,
not a TLS failure. Check that the selected listener serves `/v2/content/`
requests and that TeddyCloud has the content or its own upstream forwarding
configured. Box certificates do not configure TeddyCloud's upstream connection.
For direct official-cloud downloads, select official mode and explicitly set
`prod.de.tbs.toys:443`, with the matching original credentials.

The NFC reader must read the tag's eight four-byte token blocks before queuing
a cloud download. Look for `Tag token read successfully`; failed or partial
reads are retried after inventory, without submitting an empty or stale token.
The UID in the content URL uses tag byte order: `E048030012852680` becomes
`80268512000348e0`. This reversal is expected.

Using TeddyCloud as the upstream cache
-------------------------------------

Keep the firmware pointed at TeddyCloud, with official-cloud mode disabled.
In TeddyCloud, enable `cloud.enabled`, `cloud.enableV2Content`, and
`cloud.cacheContent`. Set its upstream to `prod.de.tbs.toys:443`. Upload your
original `ca.der`, `client.der`, and `private.der` through TeddyCloud's client
certificate upload for the applicable box (or global credentials where supported).
These are TeddyCloud's outgoing credentials; its server certificates and the
firmware's trust anchor for connecting to TeddyCloud serve a different connection.

For genuine tags affected by earlier requests, inspect their content settings
and clear an incorrectly saved `nocloud` flag. Earlier firmware sent an empty
token and uppercase UID text, either of which can trigger TeddyCloud's
custom-tag classification. The current firmware reads the token and sends a
lowercase UID. Keep protection enabled for actual custom tags.

TeddyCloud logs distinguish `Content marked as no cloud and no content locally
available`, `No local content available and cloud access disabled`, and
`Serve cloud content from ...`. Only the last branch attempts forwarding.
For Docker, follow the server logs with `docker logs --since 5m -f <container>`
while placing the tag. Check the active box's settings as well as global settings.
If no matching request reaches the container, inspect reverse-proxy routing.
Prefer a direct connection to the box-facing TLS listener when isolating proxy
issues; TLS termination does not automatically preserve client identity.
