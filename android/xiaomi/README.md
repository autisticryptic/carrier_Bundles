# Xiaomi carrier/baseband catalog

This extractor imports public carrier configuration from Xiaomi full OTA or
fastboot ROM archives and stores the result in the root schema-v7 database. It
also inventories modem-related artifacts. It does not decode Qualcomm/MediaTek
modem internals.

Default pinned source is a full OTA mirror for:

```text
Xiaomi 15 Ultra / xuanyuan / Global
HyperOS OS3.0.301.0.WOAMIXM / Android 16
https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/OS3.0.301.0.WOAMIXM/xuanyuan_global-ota_full-OS3.0.301.0.WOAMIXM-user-16.0-a67f21cbf3.zip
```

Build:

```bash
python3 android/xiaomi/build_baseband_catalog.py
```

Use a local full OTA ZIP or fastboot archive:

```bash
python3 android/xiaomi/build_baseband_catalog.py \
  --rom-path data/raw/xiaomi/xuanyuan_global-ota_full-OS3.0.301.0.WOAMIXM-user-16.0-a67f21cbf3.zip \
  --device-name "Xiaomi 15 Ultra" \
  --device xuanyuan \
  --region Global \
  --android-version 16 \
  --build-id OS3.0.301.0.WOAMIXM
```

Full OTA extraction requires `payload-dumper-go`, `erofs-utils` and 7-Zip.
Fastboot extraction requires 7-Zip. The importer reads supported PLMN-scoped
`CarrierConfig` XML and IMS APN tables to produce `carrier_profiles`; if a ROM
produces zero profiles, the build fails by default. Use `--allow-empty-profiles`
only for diagnostics.

For full OTAs, finding APNs in `product` does not stop inspection of `mi_ext`,
`system_ext`, `vendor` and `odm`. Carrier-config extraction manifest v3 invalidates
older product-only or APK-incomplete inventories; this does not change catalog schema
v7. Only protobufs under `etc/CarrierSettings` are inventoried, not unrelated
Android feature-flag/linker/speech protobufs.

**Full-OTA validation completed:** the fixed pinned ROM was downloaded and
rebuilt locally. Its CarrierConfig APK/resources now produce **380 statically
ready VoWiFi profiles**, all accepted by the actual SimAdmin catalog consumer.
See [full OTA validation](FULL_OTA_VALIDATION.md) for source hashes, selected
includes/overlay order, negative tests and derivation boundaries. This is not
live registration or subscription-provisioning verification.

The bounded stdlib APK XML decoder imports the verified static resource chain;
only referenced default assets are merged. Unknown carrier-ID/SIM predicates
are not broadened to public PLMN rules. WFC flags come from source evidence;
standard fallback ePDG/IKE values remain labelled `standard_derived`.

**Coverage limits:** APNs alone do not prove VoWiFi support. CarrierSettings
protobuf semantics, fastboot `super.img`, MCFG record selection, dynamic
opconfig/APEX updates and all carrier-ID/MVNO combinations remain unimplemented.
The earlier [xuanyuan investigation](INVESTIGATION.md) is retained as history;
its missing-input/zero-VoWiFi status is superseded by the full-OTA validation.

The importer also extracts known modem-related members such as `NON-HLOS.bin`,
`modem*.img`, `dsp*.img`, `adsp*.img`, `cdsp*.img`, `imagefv*.img` and
`xbl_config*.elf`. Each extracted member is recorded as a `modem_config`
`source_artifacts` row with the original archive member path, size and SHA-256.

Firmware-only packages, including XM Firmware Updater's small `fw_*.zip`
artifacts, do not contain Android carrier configuration files and cannot produce
a useful carrier profile catalog by themselves.
