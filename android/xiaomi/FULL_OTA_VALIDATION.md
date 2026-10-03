# Xiaomi full-OTA VoWiFi extraction — locally verified 2026-10-03

## Result

The fixed extractor was run locally against the complete pinned Global
`xuanyuan / OS3.0.301.0.WOAMIXM / Android 16` OTA, not synthetic firmware or
strings scanned from MCFG. **The rebuilt catalog has 380 statically ready
VoWiFi profiles**, compared with zero in the old APN-only catalog.

| Metric | Rebuilt catalog |
|---|---:|
| Profile rows | 941 |
| LTE IMS ready | 644 |
| NR IMS ready | 655 |
| VoWiFi ready | 380 |
| VoWiFi explicitly unsupported | 560 |
| VoWiFi unknown | 1 |

`ready` means static required fields are available and the SimAdmin consumer
can load the policy. **No actual SIM authentication, ePDG tunnel establishment,
or live carrier registration was performed.** Source WFC enablement is not proof
of subscription provisioning.

## Exact input and extraction

- Full OTA size: **9,035,445,935 bytes**.
- SHA256: `be2572f4082d294d9b4c25d74133daeba016733406320777d916acbb647c2135`.
  This equals the old catalog's recorded complete-ROM hash.
- `payload-dumper-go 2.0.1` archive SHA256:
  `ebca8aa8742ba50b6d6acea5172a31023adb57532fa96931fd71d980931f953d`.
- Selected Android partitions: product, mi_ext, system_ext, vendor, odm, system.
  Payload output verification stays enabled; the previous `-no-verify` is removed.
- EROFS partition extraction completed locally. The production selective
  extraction path was then run from the original OTA and its APK hashes were
  checked against the full filesystem extraction.
- Recognized policy APKs:
  - `system_ext/priv-app/CarrierConfig/CarrierConfig.apk`
  - `product/overlay/CarrierConfigResCommon_Sys.apk`
  - `product/overlay/MiuiCarrierConfigOverlay.apk`
- Also read the actual `mi_ext/product/etc/vendor_miui.xml` and IMS APN tables.
  No extra ROM was used to fill Xiaomi policy.

## What the old extractor missed

Finding product APNs used to terminate partition scanning; no APK resources
were imported. The new bounded stdlib reader handles plaintext assets and
Android binary XML while preserving selector attributes, scalar/array types,
original attribute spellings, APK/member hashes, and member paths. It does not
execute APK code, extract arbitrary ZIP paths, or need aapt in production.

A total of **455 XML documents** were decoded across the three APKs. The base
APK's malformed no-SIM placeholder is recorded and omitted because it is never
selected for a SIM-bearing PLMN. Other omitted/undecodable policy XML is a build
error. Oversized manifests are rejected before decompression; duplicate paths,
unsafe paths, qualified/unmodelled overlay resources and unsupported includes
fail closed.

## Source selection and precedence

The bundled CarrierConfig DEX was inspected to verify the static fallback chain,
including its `mDefaultPb` accumulator and flush points:

1. PLMN assets / unknown carrier-ID-dependent values.
2. The vendor resource and MIUI resource.
3. Loose MIUI overrides, if present, flush pending included defaults before
   applying their explicit values.
4. Included defaults and the explicit device resource, in the app's order.
5. Loose device overrides, if present.

Only the referenced assets are loaded:

- `assets/2g_and_3g_sunset.xml`
- `assets/globalization.xml`
- `assets/default_v2.9.6.xml`

The many other default-version files are **not** merged. The source app opens
these names directly through AssetManager; `assets/default/name.xml` in an
overlay is not silently substituted for base `assets/name.xml`.

Singleton MCC/MNC conditions use the app's numeric comparison; comma lists use
trimmed textual membership. Catalog PLMN spelling is retained. Carrier-ID
filenames are never treated as PLMNs. 379 carrier-ID assets have no established
mapping, so their keys begin as uncertain; later definite overrides can establish
a PLMN-invariant value. GID/SPN/IMSI/device-dependent conflicting values remain
withheld unless a later unconditional assignment resolves them.

MVNO/carrier-ID-restricted APNs are no longer generalized into PLMN-wide
profiles. Consequently the LTE/NR counts differ from the old APN-only snapshot;
this is not a claim that the removed broad APN associations were valid coverage.

## Provenance and derivation boundary

All **380 WFC=true facts** have exact APK/member/block/selector evidence:

| Final WFC=true source | Profiles |
|---|---:|
| Base APK `assets/default_v2.9.6.xml` | 308 |
| MIUI overlay `res/xml/vendor_device.xml` | 68 |
| Base APK `assets/2g_and_3g_sunset.xml` | 4 |

For these profiles the standard ePDG, AKA identities and IKE baseline are
explicitly **`standard_derived`**, not extracted Xiaomi hostnames or proven
reachable gateways. Source WFC flags and APN fields have separate extracted
leaf evidence. Mixed `/services`, `/access` and `/sip` objects are not falsely
attributed wholly to standards or wholly to firmware.

An unresolved possible static endpoint does not become a standard endpoint by
omission. Explicit WFC=false remains unsupported. `--no-standard-derived`
removes generated domains/identity/IKE fallback rather than relabelling them
as extracted data.

Post-install opconfig/APEX updates, subscriber provisioning and unresolved
carrier-ID/MVNO combinations are outside this static-ROM projection. The locally
extracted ROM did not contain active opconfig files or an enabled
`ro.miui.carrier.apex` property. This does not claim a handset can never acquire
such an update later.

## Validation and artifacts

- **97 Python tests passed**, including decoder bounds/types, scope/precedence,
  sunset/default includes, numeric MNC semantics, disabled/conditional values,
  APN scope and provenance regression tests.
- Actual SimAdmin consumer: **380 VoWiFi + 644 LTE ready profiles loaded**;
  **858 non-ready access results rejected** as expected.
- New four-source/three-variant set: `data/variants/2026-10-03-xiaomi-vowifi-final/`.
  All 12 SQLite integrity, foreign-key, schema-v7 and SHA256 checks passed.
- Set-level consumer regression: **189 removed accesses** use existing derived
  resolution; **12196 other projections unchanged**, NR preserved.
- The updated Xiaomi minimal retains all 380 ready VoWiFi profiles. Xiaomi
  full/no-icons are about **8.76 MiB**, runtime-minimal **2.66 MiB**. Icon syncing
  was explicitly skipped for this local rebuild, so its full input has no newly
  acquired icons. Other sources are their existing fixed snapshots.

The old 430 Xiaomi LTE deletions are not blindly repeated after adding real
source policy: stronger service/strategy facts require retention under the
existing conservative classifier. Runtime audit-evidence slimming still works.

The early 335-profile candidate was not submitted: review found the missing
sunset include and numeric-selector differences; these were fixed and the
catalog was rebuilt and retested. The final counts above supersede that draft.

Reproduce (new output filenames only):

```bash
python3 android/xiaomi/build_baseband_catalog.py \
  --rom-path /path/to/xuanyuan-full-ota.zip \
  --work-dir /path/to/work \
  --output /path/to/carrier-bundles-xiaomi15ultra-xuanyuan-baseband.sqlite3 \
  --skip-icon-sync

python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Local evidence lives in the sibling SimAdmin workspace:
`.local/evidence/xiaomi-full-ota-20261003/` (verified download, extraction logs,
source/DEX inspection, producer tests, consumer tests and artifact checks).
The multi-gigabyte ROM/images and APKs are not committed to Git. No 410 device
operations or live IMS tests were performed in this work.
