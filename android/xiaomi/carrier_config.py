"""Import Xiaomi CarrierConfig/APN XML into a schema-v7 catalog."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_contract import CONFIG_CONTRACT, compact, finalized_config

from . import PARSER_VERSION
from .firmware import ExtractedXiaomiCarrierConfig, digest_file
from .sources import XiaomiFastbootArtifact


PARSER_NAME = "android/xiaomi/carrier-config"
STANDARDS_URI = "https://www.3gpp.org/ftp/Specs/archive/23_series/23.003/"
LICENSE_NOTE = (
    "Xiaomi device software; extraction and use remain subject to the terms "
    "published with the ROM package"
)
RELEVANT_KEY_PARTS = (
    "ims",
    "volte",
    "vonr",
    "wfc",
    "wifi_call",
    "epdg",
    "ike",
    "apn",
    "pcscf",
    "entitle",
    "emergency",
    "xcap",
)
AUTH_TYPE = {
    "-1": "unspecified",
    "0": "none",
    "1": "pap",
    "2": "chap",
    "3": "pap_or_chap",
}


@dataclass(frozen=True)
class XiaomiXmlConfig:
    path: Path
    source_path: str
    plmn: str
    values: dict[str, Any]


@dataclass(frozen=True)
class XiaomiApn:
    path: Path
    source_path: str
    plmn: str
    carrier: str | None
    values: dict[str, Any]


@dataclass
class ImportStats:
    xml_configs_seen: int = 0
    xml_configs_imported: int = 0
    apns_seen: int = 0
    ims_apns_imported: int = 0
    profiles_imported: int = 0
    field_evidence_imported: int = 0


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold()


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return result or "unknown"


def _display_name(value: str) -> str:
    return re.sub(r"[_=.-]+", " ", value).strip().title()


def _domain(plmn: str) -> str:
    return f"ims.mnc{int(plmn[3:]):03d}.mcc{plmn[:3]}.3gppnetwork.org"


def _epdg(plmn: str) -> str:
    return f"epdg.epc.mnc{int(plmn[3:]):03d}.mcc{plmn[:3]}.pub.3gppnetwork.org"


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _plmn_from_text(value: str) -> str | None:
    match = re.search(r"(?<!\d)([0-9]{5,6})(?!\d)", value)
    if not match or match.group(1).startswith("000"):
        return None
    return match.group(1)


def _mcc_mnc(mcc: str | None, mnc: str | None) -> str | None:
    if not mcc or not mnc:
        return None
    value = f"{mcc.strip()}{mnc.strip()}"
    if re.fullmatch(r"[0-9]{5,6}", value) and not value.startswith("000"):
        return value
    return None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().casefold()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip(), 0)
    except ValueError:
        return None


def _scalar_text(element: ET.Element) -> str | None:
    return element.get("value") or (element.text.strip() if element.text else None)


def _parse_value(element: ET.Element) -> Any:
    kind = _tag(element)
    if kind in {"bool", "boolean"}:
        return _bool(_scalar_text(element))
    if kind in {"int", "integer", "long"}:
        return _int(_scalar_text(element))
    if kind == "string":
        value = _scalar_text(element)
        return value.strip() if isinstance(value, str) and value.strip() else None
    if kind in {"int-array", "integer-array"}:
        return [
            parsed
            for parsed in (_int(item.get("value") or item.text) for item in element)
            if parsed is not None
        ]
    if kind == "string-array":
        return [
            value.strip()
            for value in (item.get("value") or item.text or "" for item in element)
            if value.strip()
        ]
    return None


def _parse_carrier_config(path: Path, root_dir: Path) -> XiaomiXmlConfig | None:
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    root = tree.getroot()
    plmn = (
        root.get("mccmnc")
        or root.get("plmn")
        or _mcc_mnc(root.get("mcc"), root.get("mnc"))
        or _plmn_from_text(path.name)
    )
    if plmn is None:
        return None
    values: dict[str, Any] = {}
    for element in root.iter():
        name = element.get("name") or element.get("key")
        if not name:
            continue
        value = _parse_value(element)
        if value is not None:
            values[name] = value
    if not values:
        return None
    return XiaomiXmlConfig(
        path=path,
        source_path=_relative(path, root_dir),
        plmn=plmn,
        values=values,
    )


def _apn_types(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().casefold() for item in re.split(r"[,|]", value) if item.strip()}


def _apn_applies(apn: XiaomiApn, access_kind: str) -> bool:
    mask = apn.values.get("bearer_bitmask")
    if not isinstance(mask, str) or not mask.strip() or mask.strip() == "0":
        return True
    try:
        values = {int(item) for item in mask.split("|") if item}
    except ValueError:
        return True
    ril = {"lte": 14, "vowifi": 18, "nr": 20}[access_kind]
    return ril in values


def _parse_apns(path: Path, root_dir: Path) -> list[XiaomiApn]:
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []
    result: list[XiaomiApn] = []
    for element in tree.getroot().iter():
        if _tag(element) != "apn":
            continue
        types = _apn_types(element.get("type") or element.get("types"))
        if "ims" not in types:
            continue
        plmn = (
            element.get("mccmnc")
            or element.get("numeric")
            or _mcc_mnc(element.get("mcc"), element.get("mnc"))
        )
        if plmn is None or not re.fullmatch(r"[0-9]{5,6}", plmn):
            continue
        values = {
            "apn": element.get("apn") or element.get("value"),
            "auth_type": AUTH_TYPE.get(element.get("authtype", "-1"), "unspecified"),
            "username": element.get("user"),
            "password": element.get("password"),
            "ip_family": _ip_family(element.get("protocol")),
            "roaming_ip_family": _ip_family(element.get("roaming_protocol")),
            "mtu": _int(element.get("mtu")),
            "bearer_bitmask": element.get("bearer_bitmask") or element.get("bearer"),
        }
        if values["apn"]:
            result.append(
                XiaomiApn(
                    path=path,
                    source_path=_relative(path, root_dir),
                    plmn=plmn,
                    carrier=element.get("carrier"),
                    values=compact(values),
                )
            )
    return result


def _ip_family(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    return {
        "ip": "ipv4",
        "ipv4": "ipv4",
        "ipv6": "ipv6",
        "ipv4v6": "ipv4v6",
        "ipvv4v6": "ipv4v6",
    }.get(normalized)


def _is_relevant_config(path: Path) -> bool:
    name = path.name.casefold()
    return (
        name.startswith("carrier_config")
        or name in {"apns-conf.xml", "fiveg-apns-conf.xml", "epdg_apns_conf.xml"}
    )


def load_xiaomi_xml_configs(
    extracted: ExtractedXiaomiCarrierConfig,
) -> tuple[list[XiaomiXmlConfig], list[XiaomiApn]]:
    configs: list[XiaomiXmlConfig] = []
    apns: list[XiaomiApn] = []
    for path in extracted.config_files:
        if path.suffix.casefold() != ".xml" or not _is_relevant_config(path):
            continue
        if path.name.casefold() in {"apns-conf.xml", "fiveg-apns-conf.xml"}:
            apns.extend(_parse_apns(path, extracted.root_dir))
        else:
            parsed = _parse_carrier_config(path, extracted.root_dir)
            if parsed is not None:
                configs.append(parsed)
    return configs, apns


def _source_artifact(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    source_uri: str,
    artifact_sha256: str | None,
    source_revision: str | None,
    parser_name: str = PARSER_NAME,
    parser_version: str = PARSER_VERSION,
    license_note: str = LICENSE_NOTE,
) -> int:
    cursor = connection.execute(
        """INSERT INTO source_artifacts(
               source_kind, source_uri, artifact_sha256, source_revision,
               extracted_at, parser_name, parser_version, license_note
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_kind,
            source_uri,
            artifact_sha256,
            source_revision,
            datetime.now(timezone.utc).isoformat(),
            parser_name,
            parser_version,
            license_note,
        ),
    )
    return int(cursor.lastrowid)


def _evidence(
    connection: sqlite3.Connection,
    *,
    stats: ImportStats,
    profile_id: str,
    source_id: int,
    target_kind: str,
    target_path: str,
    source_path: str,
    source_key_path: str,
    value: Any,
    evidence_kind: str = "extracted",
    confidence: int = 80,
) -> None:
    connection.execute(
        """INSERT INTO field_evidence(
               profile_id, source_id, target_kind, target_path, source_path,
               source_key_path, source_value_json, evidence_kind, confidence
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            profile_id,
            source_id,
            target_kind,
            target_path,
            source_path,
            source_key_path,
            json.dumps(value, ensure_ascii=True, sort_keys=True),
            evidence_kind,
            confidence,
        ),
    )
    stats.field_evidence_imported += 1


def _config_bool(values: dict[str, Any], key: str) -> bool | None:
    value = values.get(key)
    return value if isinstance(value, bool) else None


def _config_int(values: dict[str, Any], key: str) -> int | None:
    value = values.get(key)
    return value if isinstance(value, int) else None


def _config_text(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _choose_apn(apns: list[XiaomiApn], access_kind: str) -> XiaomiApn | None:
    candidates = [apn for apn in apns if _apn_applies(apn, access_kind)]
    if not candidates:
        return None
    distinct = {json.dumps(item.values, sort_keys=True): item for item in candidates}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def _access_from_apn(apn: XiaomiApn, *, nr: bool, include_standard_derived: bool) -> dict[str, Any]:
    values = {
        key: value
        for key, value in apn.values.items()
        if key in {"apn", "auth_type", "username", "password", "ip_family", "roaming_ip_family", "mtu"}
    }
    if nr:
        values["dnn"] = values.pop("apn", None)
        if include_standard_derived:
            values["pcscf_discovery"] = ["epco", "pco"]
    elif include_standard_derived:
        values["pcscf_discovery"] = ["pco", "epco"]
    return compact(values)


def _build_config(
    values: dict[str, Any],
    apns: list[XiaomiApn],
    plmn: str,
    *,
    include_standard_derived: bool,
) -> dict[str, Any]:
    volte = _config_bool(values, "carrier_volte_available_bool")
    vonr = _config_bool(values, "carrier_vonr_available_bool")
    vowifi = _config_bool(values, "carrier_wfc_ims_available_bool")
    ipsec = _config_bool(values, "ims.sip_over_ipsec_enabled_bool")
    transport = {0: "udp", 1: "tcp", 2: "auto", 3: "tls"}.get(
        _config_int(values, "ims.sip_preferred_transport_int")
    )
    access: dict[str, Any] = {}
    lte_apn = _choose_apn(apns, "lte")
    if lte_apn is not None:
        access["lte"] = _access_from_apn(
            lte_apn, nr=False, include_standard_derived=include_standard_derived
        )
    nr_apn = _choose_apn(apns, "nr")
    if nr_apn is not None:
        access["nr"] = _access_from_apn(
            nr_apn, nr=True, include_standard_derived=include_standard_derived
        )
    static_epdg = _config_text(values, "iwlan.epdg_static_address_string")
    if vowifi is not None or static_epdg:
        wifi: dict[str, Any] = {"enabled": vowifi}
        wifi_apn = _choose_apn(apns, "vowifi")
        if wifi_apn is not None:
            wifi.update(
                _access_from_apn(
                    wifi_apn, nr=False, include_standard_derived=False
                )
            )
        endpoints = []
        if static_epdg:
            endpoints.append(
                {
                    "address": static_epdg.rstrip("."),
                    "discovery": "static",
                    "roaming_scope": "home",
                }
            )
        elif vowifi is True and include_standard_derived:
            endpoints.append(
                {
                    "address": _epdg(plmn),
                    "discovery": "standard_derived",
                    "roaming_scope": "home",
                }
            )
        if endpoints:
            wifi["epdg"] = endpoints
        if include_standard_derived:
            wifi["pcscf_discovery"] = ["ike_cfg"]
            wifi["ike"] = {
                "initial_port": 500,
                "natt_port": 4500,
                "eap_method": "eap_aka",
                "request_internal_address": True,
                "request_pcscf": True,
                "nat_traversal": True,
                "identities": {
                    "idi": [
                        {
                            "identity_type": "id_rfc822_addr",
                            "source": "derived_imsi",
                            "value_template": (
                                "0{imsi}@nai.epc.mnc{mnc3}.mcc{mcc}.3gppnetwork.org"
                            ),
                        }
                    ],
                    "idr": [
                        {
                            "identity_type": "id_fqdn",
                            "source": "epdg_fqdn",
                            "value_template": "{epdg_fqdn}",
                        }
                    ],
                },
            }
        access["vowifi"] = compact(wifi)

    register_expires = _config_int(values, "ims.registration_expiry_timer_sec_int")
    config = {
        "protocol_baseline": CONFIG_CONTRACT,
        "ims": {
            "home_domain": _domain(plmn),
            "realm": _domain(plmn),
            "authentication": {"scheme": "ims_aka"},
            "transport": transport,
            "security_agreement": (
                "required" if ipsec is True else "disabled" if ipsec is False else None
            ),
            "identity_templates": [
                {
                    "role": "impi",
                    "source": "derived_imsi",
                    "identity_type": "nai",
                    "value_template": "{imsi}@{home_domain}",
                    "use_when": "if_isim_missing",
                },
                {
                    "role": "impu",
                    "source": "derived_imsi",
                    "identity_type": "sip_uri",
                    "value_template": "sip:{imsi}@{home_domain}",
                    "use_when": "if_isim_missing",
                },
            ],
        },
        "access": access,
        "sip": {
            "common": {
                "register": {
                    "requested_expires_seconds": (
                        register_expires if register_expires and register_expires > 0 else None
                    )
                }
            }
        },
        "services": {
            "ims": bool(apns),
            "volte": volte,
            "vonr": vonr,
            "vowifi": vowifi,
            "ut_xcap": _config_bool(values, "carrier_supports_ss_over_ut_bool"),
            "vilte": _config_bool(values, "carrier_vt_available_bool"),
        },
    }
    return compact(config)


def _has_relevant_values(values: dict[str, Any]) -> bool:
    return any(any(part in key.casefold() for part in RELEVANT_KEY_PARTS) for key in values)


def _merge_values(configs: list[XiaomiXmlConfig]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for config in sorted(configs, key=lambda item: item.source_path.casefold()):
        result.update(config.values)
    return result


def import_xiaomi_carrier_config_catalog(
    database: Path,
    extracted: ExtractedXiaomiCarrierConfig,
    *,
    artifact: XiaomiFastbootArtifact,
    include_standard_derived: bool = True,
) -> ImportStats:
    """Import Xiaomi carrier profile data from CarrierConfig/APN XML files."""

    configs, apns = load_xiaomi_xml_configs(extracted)
    stats = ImportStats(xml_configs_seen=len(configs), apns_seen=len(apns))
    configs_by_plmn: dict[str, list[XiaomiXmlConfig]] = {}
    apns_by_plmn: dict[str, list[XiaomiApn]] = {}
    for config in configs:
        configs_by_plmn.setdefault(config.plmn, []).append(config)
    for apn in apns:
        apns_by_plmn.setdefault(apn.plmn, []).append(apn)

    candidates = sorted(set(configs_by_plmn) | set(apns_by_plmn))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        release = connection.execute(
            "SELECT sealed FROM catalog_metadata WHERE singleton = 1"
        ).fetchone()
        if release is None or release[0] != 0:
            raise RuntimeError("Xiaomi carrier config importer requires an unsealed v7 catalog")

        source_revision = json.dumps(
            {
                "device_name": artifact.device_name,
                "codename": artifact.codename,
                "region": artifact.region,
                "android_version": artifact.android_version,
                "build_id": artifact.build_id,
                "package_kind": artifact.package_kind,
                "config_files": len(extracted.config_files),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        config_source_id = _source_artifact(
            connection,
            source_kind="carrier_config",
            source_uri=artifact.url,
            artifact_sha256=extracted.rom_sha256,
            source_revision=source_revision,
        )
        standard_source_id = None
        if include_standard_derived:
            standard_source_id = _source_artifact(
                connection,
                source_kind="standards_reference",
                source_uri=STANDARDS_URI,
                artifact_sha256=None,
                source_revision="3GPP TS 23.003/24.229/33.203",
                parser_name="3gpp-standard-deriver",
                parser_version="1",
                license_note="3GPP specification references; no subscriber data",
            )

        for plmn in candidates:
            plmn_configs = configs_by_plmn.get(plmn, [])
            plmn_apns = apns_by_plmn.get(plmn, [])
            values = _merge_values(plmn_configs)
            if not plmn_apns and not _has_relevant_values(values):
                continue
            carrier_name = next((item.carrier for item in plmn_apns if item.carrier), None)
            canonical_name = carrier_name or f"plmn_{plmn}"
            carrier_id = _slug(canonical_name if carrier_name else f"xiaomi-{plmn}")
            display_name = carrier_name or f"PLMN {plmn}"
            connection.execute(
                """INSERT OR IGNORE INTO carriers(
                       carrier_id, canonical_name, brand_name, carrier_kind, notes
                   ) VALUES (?, ?, ?, 'unknown', ?)""",
                (
                    carrier_id,
                    canonical_name,
                    _display_name(display_name),
                    "Name and scope are extracted from Xiaomi CarrierConfig/APN XML.",
                ),
            )
            raw_config, statuses = finalized_config(
                _build_config(
                    values,
                    plmn_apns,
                    plmn,
                    include_standard_derived=include_standard_derived,
                )
            )
            profile_id = f"profile-{carrier_id}-{plmn}-{hashlib.sha256(plmn.encode()).hexdigest()[:10]}"
            connection.execute(
                """INSERT INTO carrier_profiles(
                       profile_id, carrier_id, display_name, profile_kind,
                       priority, confidence, lte_ims_status, nr_ims_status,
                       vowifi_status, config_json, notes
                   ) VALUES (?, ?, ?, 'default', 100, 70, ?, ?, ?, ?, ?)""",
                (
                    profile_id,
                    carrier_id,
                    f"{_display_name(display_name)} {plmn}",
                    statuses["lte"],
                    statuses["nr"],
                    statuses["vowifi"],
                    raw_config,
                    f"Xiaomi CarrierConfig/APN XML for {artifact.build_id}",
                ),
            )
            stats.profiles_imported += 1
            connection.execute(
                """INSERT INTO profile_match_rules(profile_id, plmn)
                   VALUES (?, ?)""",
                (profile_id, plmn),
            )
            primary_source_path = (
                plmn_configs[0].source_path if plmn_configs else plmn_apns[0].source_path
            )
            connection.execute(
                """INSERT INTO profile_sources(
                       profile_id, source_id, source_profile_key, source_path,
                       contribution_kind, precedence
                   ) VALUES (?, ?, ?, ?, 'carrier_policy', 200)""",
                (profile_id, config_source_id, plmn, primary_source_path),
            )
            if standard_source_id is not None:
                connection.execute(
                    """INSERT INTO profile_sources(
                           profile_id, source_id, source_profile_key, source_path,
                           contribution_kind, precedence
                       ) VALUES (?, ?, '3GPP IMS baseline', ?,
                                 'standard_default', 0)""",
                    (profile_id, standard_source_id, STANDARDS_URI),
                )
            _evidence(
                connection,
                stats=stats,
                profile_id=profile_id,
                source_id=config_source_id,
                target_kind="match_rule",
                target_path=f"/{plmn}/plmn",
                source_path=primary_source_path,
                source_key_path="plmn",
                value=plmn,
            )
            for section in ("access", "sip", "services"):
                value = json.loads(raw_config).get(section)
                if value:
                    _evidence(
                        connection,
                        stats=stats,
                        profile_id=profile_id,
                        source_id=config_source_id,
                        target_kind="config",
                        target_path=f"/{section}",
                        source_path=primary_source_path,
                        source_key_path=section,
                        value=value,
                    )
            if standard_source_id is not None:
                normalized = json.loads(raw_config)
                for pointer, value in (
                    ("/ims/home_domain", normalized["ims"]["home_domain"]),
                    ("/ims/realm", normalized["ims"]["realm"]),
                    ("/ims/identity_templates", normalized["ims"]["identity_templates"]),
                ):
                    _evidence(
                        connection,
                        stats=stats,
                        profile_id=profile_id,
                        source_id=standard_source_id,
                        target_kind="config",
                        target_path=pointer,
                        source_path="3GPP TS 23.003",
                        source_key_path="standard derivation",
                        value=value,
                        evidence_kind="standard_derived",
                        confidence=60,
                    )
            stats.xml_configs_imported += len(plmn_configs)
            stats.ims_apns_imported += len(plmn_apns)

        notes = {
            "source_kind": "xiaomi_carrier_config_catalog",
            "device_name": artifact.device_name,
            "codename": artifact.codename,
            "region": artifact.region,
            "android_version": artifact.android_version,
            "build_id": artifact.build_id,
            "package_kind": artifact.package_kind,
            "rom_sha256": extracted.rom_sha256,
            "config_files": [_relative(path, extracted.root_dir) for path in extracted.config_files],
            "profiles_imported": stats.profiles_imported,
        }
        connection.execute(
            "UPDATE catalog_metadata SET notes = ? WHERE singleton = 1",
            (json.dumps(notes, ensure_ascii=True, sort_keys=True),),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("foreign key check failed after Xiaomi CarrierConfig import")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after Xiaomi CarrierConfig import")

    return stats
