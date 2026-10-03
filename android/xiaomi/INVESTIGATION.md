# xuanyuan missing VoWiFi: verified findings and remaining work

## Catalog evidence

Inspected a read-only Linux copy of
`SimAdmin/carrier-bundles-xiaomi15ultra-xuanyuan-baseband.sqlite3`, not the live
source database. Its metadata identifies the **full OTA**, not the smaller
firmware-only ZIP:

- Build: `OS3.0.301.0.WOAMIXM`, Android 16, Global.
- ROM SHA-256: `be2572f4082d294d9b4c25d74133daeba016733406320777d916acbb647c2135`.
- Schema: 7; 721 profiles; LTE ready 680; NR ready 657; all 721 VoWiFi unknown.
- Every carrier-policy source is an Android product APN table:
  `extracted/product/erofs-extract/etc/apns-conf.xml` (719 profiles), or
  `extracted/product/erofs-extract/etc/fiveG-apns-conf.xml` (2 profiles).
- No imported CarrierConfig/WFC policy, no `services.vowifi`, no VoWiFi access
  section. These profiles are **not decoded modem/MCFG profiles**, despite the
  database filename ending in `baseband`.

The old metadata lists only those two APN tables plus unrelated
`etc/aconfig_flags.pb`, `etc/linker.config.pb`, and
`usr/srec/en-US/offline_action_data.pb` from product. This matches two concrete
extractor defects: its global collector admitted every `.pb`, and any collected
file caused the full-OTA partition search to stop after product. The builder
already calls both the carrier-config extractor and importer; adding another
call is not the fix.

`_build_config` intentionally creates VoWiFi only from an explicit WFC flag or
static ePDG setting. APN-only input therefore explains the zero count exactly.
It would be incorrect to enable WFC or construct an extracted ePDG merely from
an IMS APN and PLMN.

## Local raw-artifact check

At investigation time `data/raw/xiaomi` was empty. `data/tmp` contained five
baseband images and their manifest; the remaining `xiaomi-real/extracted2`
entries were symlinks, not usable XML/APK/native-library contents. No full OTA,
product/system_ext image, APN XML, or carrier APK was available for an end-to-end
rerun of Android resource extraction.

The retained `modemfirmware.img` is real and matches the catalog's artifact
SHA-256 `bfab29aac6a9337cb168bbc76455fda068c59be899b917b0038af2c88a7a1fe6`.
An offline 7-Zip extraction of
`image/modem_pr/mcfg/configs/mcfg_sw/*` found **455 `mcfg_sw.mbn` files**; all
extracted lengths matched the archive listing. The directory also had four
other files. 7-Zip reported a FAT physical-size/end-of-archive warning, so this
is not a claim of full filesystem validation.

A diagnostic scan of the MBN bytes found **468 well-formed embedded
`IWLAN_S2B_CONFIG` XML documents in 410 MBNs**. This is discovery, not an MCFG
record/precedence decoder. Of the generic ePDG blocks, 73 explicitly enable a
static FQDN; 365 explicitly disable static FQDN, and 30 omit that flag. Some MBNs
contain multiple versions of the XML. Concrete examples:

- `generic/NA/TMO/Commercial/mcfg_sw.mbn`: embedded documents at `0x8ed1` and
  `0x11c0c` both contain `epdg.epc.mnc260.mcc310.pub.3gppnetwork.org`, but
  `static_fqdn_enabled` is **FALSE**.
- `generic/SEA/Smartfren/Commercial/VoWiFi/mcfg_sw.mbn`: XML at `0x8e06` contains
  `epdg.epc.mnc009.mcc510.pub.3gppnetwork.org`, also with static FQDN **FALSE**.
- `generic/EU/Vodafone/VoLTE/Netherlands/mcfg_sw.mbn`: XML at `0xaed7` contains
  `epdg.epc.mnc004.mcc204.pub.3gppnetwork.org` only in an **emergency** identifier
  list, not the normal static FQDN field.

Thus a strings-based ePDG importer would be unsafe. These records were **not**
imported or used to manufacture VoWiFi profiles. Operator directory names,
embedded ANDSF example PLMNs, or the MCC/MNC inside an endpoint are not sufficient
proof of the applicable SIM matching rules. Neither static enablement nor a
file named `VoWiFi` proves Android WFC policy availability.

## Changes and validation

- Scan all existing configured OTA partition groups even if product or loose
  ZIP members already supply APNs; do not stop at the first config file.
- Restrict config inventory to recognized config paths, including only
  `etc/CarrierSettings/*.pb`, in both collection and 7-Zip filtering.
- Invalidate v1 extraction manifests so cached APN-only inventories cannot bypass
  the corrected scan. Catalog schema remains v7.
- Regression coverage in `tests/test_xiaomi_extraction.py`: product-only/loose
  APNs do not stop the scan, old caches are rescanned, new caches are reused,
  unrelated protobufs are excluded, APNs do not imply WFC/ePDG.
- Existing `test_xiaomi.py` continues to exercise extract/import/seal/verify.
  Its ignored-member fixture now uses ZIP metadata rather than a fake payload
  that the old premature exit happened to hide.

Run:

```sh
python -m unittest discover -s tests -p 'test_xiaomi*.py' -v
```

The partition regression fixtures are synthetic and all six tests pass. The
actual local MBNs were inspected separately as described above. **A rebuilt
full-OTA catalog with recovered VoWiFi has not been demonstrated.** No catalog
rows or ePDG evidence were added, no existing database was changed, and no APK
parser was invented without source artifacts.

## Exact next actions

1. Restore an already-acquired copy of the pinned full OTA or its extracted
   `product`, `system_ext`, `mi_ext`, `vendor`, and `odm` partitions. Use a fresh
   work directory (or let manifest v2 invalidate the old cache). Ensure
   `payload-dumper-go` and `erofs-utils` are installed. Rerun locally; no device
   access is required.
2. Inventory carrier-related APKs and resource overlays in those partitions,
   including `app`, `priv-app`, and overlay locations. Decode the actual APKs
   with a resource-aware tool, inspect WFC/IWLAN keys and PLMN/carrier-ID/MVNO
   selectors, and add a small source-shaped parser regression fixture before
   enabling APK import. APK resources are currently excluded by the extractor;
   their exact presence/location in this ROM is **unverified**, not the proven
   sole cause.
3. Alternatively, pursue the retained MBNs without downloading a ROM: implement
   a bounded MCFG container/record decoder and independently establish selection
   rules, variant/record precedence, disabled static-FQDN semantics and emergency
   scope. Preserve original member/offset/hash provenance. Do not infer PLMN
   matches from operator directory names or endpoint strings.
4. Rebuild an unsealed v7 catalog, inspect actual WFC/ePDG evidence and counts,
   then run normal seal/verify and combined/minimal validation. Any standards
   fallback must remain `standard_derived`, never `extracted`.
