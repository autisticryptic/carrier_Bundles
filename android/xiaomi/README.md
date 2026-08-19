# Xiaomi fastboot baseband inventory

This extractor inventories modem-related artifacts from official Xiaomi
fastboot ROM archives and stores the result in the root schema-v7 database.
It does not decode Qualcomm/MediaTek modem internals and does not infer IMS,
VoLTE, VoNR or VoWiFi runtime parameters from raw baseband firmware.

Default pinned source:

```text
Xiaomi 15 Ultra / xuanyuan / Global
HyperOS OS3.0.301.0.WOAMIXM / Android 16
https://bigota.d.miui.com/OS3.0.301.0.WOAMIXM/xuanyuan_global_images_OS3.0.301.0.WOAMIXM_20260428.0000.00_16.0_global_d98a2e098d.tgz
```

Build:

```bash
python3 android/xiaomi/build_baseband_catalog.py --skip-icon-sync
```

Use a local fastboot archive:

```bash
python3 android/xiaomi/build_baseband_catalog.py \
  --rom-path data/raw/xiaomi/xuanyuan_global_images_OS3.0.301.0.WOAMIXM_20260428.0000.00_16.0_global_d98a2e098d.tgz \
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

Because this catalog is an inventory-only source, it normally contains no
`carrier_profiles`; profile-level fields must come from a later semantic
adapter that can prove the meaning of individual modem or Android vendor keys.
