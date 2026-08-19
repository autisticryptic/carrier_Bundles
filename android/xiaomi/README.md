# Xiaomi firmware baseband inventory

This extractor inventories modem-related artifacts from official Xiaomi
firmware ZIPs or fastboot ROM archives and stores the result in the root
schema-v7 database.
It does not decode Qualcomm/MediaTek modem internals and does not infer IMS,
VoLTE, VoNR or VoWiFi runtime parameters from raw baseband firmware.

Default pinned source is XM Firmware Updater's firmware-only package, extracted
from the official Xiaomi ROM and hosted on GitHub Releases:

```text
Xiaomi 15 Ultra / xuanyuan / Global
HyperOS OS3.0.301.0.WOAMIXM / Android 16
https://github.com/XiaomiFirmwareUpdaterReleases/firmware_xiaomi_xuanyuan/releases/download/stable-12.05.2026/fw_xuanyuan_xuanyuan_global-ota_full-OS3.0.301.0.WOAMIXM-user-16.0-a67f21cbf3.zip
MD5: f53a4b0b909e2977ce6f0a349ba5ea80
```

Build:

```bash
python3 android/xiaomi/build_baseband_catalog.py --skip-icon-sync
```

Use a local firmware ZIP or fastboot archive:

```bash
python3 android/xiaomi/build_baseband_catalog.py \
  --rom-path data/raw/xiaomi/fw_xuanyuan_xuanyuan_global-ota_full-OS3.0.301.0.WOAMIXM-user-16.0-a67f21cbf3.zip \
  --device-name "Xiaomi 15 Ultra" \
  --device xuanyuan \
  --region Global \
  --android-version 16 \
  --build-id OS3.0.301.0.WOAMIXM \
  --skip-icon-sync
```

The importer extracts known modem-related members such as `NON-HLOS.bin`,
`modem*.img`, `dsp*.img`, `adsp*.img`, `cdsp*.img`, `imagefv*.img` and
`xbl_config*.elf`. Each extracted member is recorded as a `modem_config`
`source_artifacts` row with the original archive member path, size and
SHA-256. The whole ROM is recorded as a `firmware_manifest` source.

The extractor writes `xiaomi-baseband-manifest.json` into the work directory
after the first extraction. A second output variant in the same GitHub Actions
job reuses that manifest and the extracted modem files instead of scanning and
decompressing the full fastboot archive again.

Because this catalog is an inventory-only source, it normally contains no
`carrier_profiles`; profile-level fields must come from a later semantic
adapter that can prove the meaning of individual modem or Android vendor keys.
