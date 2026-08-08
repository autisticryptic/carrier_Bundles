"""Compile decoded Pixel CarrierSettings into a schema-v7 catalog."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_contract import CONFIG_CONTRACT, compact, finalized_config

from . import PARSER_NAME, PARSER_VERSION
from .carrier_settings import (
    CarrierSettingRecord,
    array_config,
    bool_config,
    config_map,
    has_ims_data,
    ims_apns,
    int_config,
    load_carrier_settings,
    normalized_apn,
    public_imsi_prefix,
    select_access_apn,
    text_config,
    translate_android_user_agent,
)
from .firmware import ExtractedPixelFirmware


STANDARDS_URI = "https://www.3gpp.org/ftp/Specs/archive/23_series/23.003/"
LICENSE_NOTE = (
    "Google device software; extraction and use remain subject to the terms "
    "published with the official factory image"
)


@dataclass
class ImportStats:
    carrier_settings_seen: int = 0
    carrier_settings_imported: int = 0
    profiles_imported: int = 0
    access_configs_imported: int = 0
    ambiguous_access_configs: int = 0
    mcfg_files_inventoried: int = 0
    relevant_raw_values: int = 0
    field_evidence_imported: int = 0


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return result or "unknown"


def _country_from_canonical(name: str) -> str | None:
    match = re.search(r"_([a-z]{2})$", name)
    return match.group(1).upper() if match else None


def _display_name(name: str) -> str:
    return re.sub(r"[_=]+", " ", name).strip().title()


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
    confidence: int = 95,
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


def _record_key_path(record: CarrierSettingRecord, suffix: str) -> str:
    canonical_name = json.dumps(record.canonical_name, ensure_ascii=True)
    return f"settings[{canonical_name}].{suffix}"


def _profile_suffix(carrier_id: Any) -> str:
    mvno_kind = carrier_id.WhichOneof("mvno_data")
    parts = [carrier_id.mcc_mnc]
    if mvno_kind:
        parts.extend((mvno_kind, getattr(carrier_id, mvno_kind)))
    readable = _slug("-".join(parts))[:48]
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:10]
    return f"{readable}-{digest}"


def _domain(plmn: str) -> str:
    return f"ims.mnc{int(plmn[3:]):03d}.mcc{plmn[:3]}.3gppnetwork.org"


def _epdg(plmn: str) -> str:
    return f"epdg.epc.mnc{int(plmn[3:]):03d}.mcc{plmn[:3]}.pub.3gppnetwork.org"


def _match_values(carrier_id: Any) -> dict[str, Any] | None:
    plmn = carrier_id.mcc_mnc
    if not re.fullmatch(r"[0-9]{5,6}", plmn) or plmn.startswith("000"):
        return None
    result: dict[str, Any] = {"plmn": plmn}
    mvno_kind = carrier_id.WhichOneof("mvno_data")
    if mvno_kind == "spn":
        result["spn"] = carrier_id.spn
    elif mvno_kind == "gid1":
        result["gid1"] = carrier_id.gid1
    elif mvno_kind == "imsi":
        prefix = public_imsi_prefix(carrier_id.imsi)
        if prefix:
            result["imsi_prefix"] = prefix[:14]
    return result


def _apn_config(apn: Any) -> dict[str, Any]:
    values = normalized_apn(apn)
    return compact(
        {
            "apn": values["apn_dnn"],
            "auth_type": values["apn_auth_type"],
            "username": values["apn_username"],
            "password": values["apn_password"],
            "ip_family": values["ip_family"],
            "roaming_ip_family": values["roaming_ip_family"],
            "mtu": values["mtu"],
        }
    )


def _access_config(
    record: CarrierSettingRecord,
    plmn: str,
    *,
    include_standard_derived: bool,
    stats: ImportStats,
) -> dict[str, Any]:
    configs = config_map(record.setting)
    result: dict[str, Any] = {}
    for source_kind, target_kind in (("lte_epc", "lte"), ("nr_5gc", "nr")):
        apn, ambiguous = select_access_apn(record.setting, source_kind)
        stats.ambiguous_access_configs += int(ambiguous)
        if apn is None:
            continue
        item = _apn_config(apn)
        if target_kind == "nr":
            item["dnn"] = item.pop("apn", None)
            if include_standard_derived:
                item["pcscf_discovery"] = ["epco", "pco"]
        elif include_standard_derived:
            item["pcscf_discovery"] = ["pco", "epco"]
        result[target_kind] = compact(item)
        stats.access_configs_imported += 1

    wfc = bool_config(configs, "carrier_wfc_ims_available_bool")
    static_epdg = text_config(configs, "iwlan.epdg_static_address_string")
    roaming_epdg = text_config(configs, "iwlan.epdg_static_address_roaming_string")
    wifi_apn, ambiguous = select_access_apn(record.setting, "wifi_epdg")
    stats.ambiguous_access_configs += int(ambiguous)
    if wfc is not None or static_epdg or wifi_apn is not None:
        wifi: dict[str, Any] = {"enabled": wfc}
        if wifi_apn is not None:
            wifi.update(_apn_config(wifi_apn))
        endpoints: list[dict[str, Any]] = []
        if static_epdg:
            endpoints.append(
                {
                    "address": static_epdg.rstrip("."),
                    "discovery": "static",
                    "roaming_scope": "home",
                }
            )
        elif wfc is True and include_standard_derived:
            endpoints.append(
                {
                    "address": _epdg(plmn),
                    "discovery": "standard_derived",
                    "roaming_scope": "home",
                }
            )
        if roaming_epdg:
            endpoints.append(
                {
                    "address": roaming_epdg.rstrip("."),
                    "discovery": "static",
                    "roaming_scope": "visited",
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
                                "0{imsi}@nai.epc.mnc{mnc3}.mcc{mcc}."
                                "3gppnetwork.org"
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
        result["vowifi"] = compact(wifi)
        stats.access_configs_imported += 1
    return result


def _services(record: CarrierSettingRecord) -> dict[str, Any]:
    configs = config_map(record.setting)
    result: dict[str, Any] = {
        "ims": True if ims_apns(record.setting) else None,
        "volte": bool_config(configs, "carrier_volte_available_bool"),
        "vonr": bool_config(configs, "carrier_vonr_available_bool"),
        "vowifi": bool_config(configs, "carrier_wfc_ims_available_bool"),
        "ut_xcap": bool_config(configs, "carrier_supports_ss_over_ut_bool"),
        "vilte": bool_config(configs, "carrier_vt_available_bool"),
    }
    sms_rats = array_config(configs, "imssms.sms_over_ims_supported_rats_int_array")
    emergency_rats = array_config(
        configs, "imsemergency.emergency_over_ims_supported_rats_int_array"
    )
    if sms_rats is not None:
        result["smsoip"] = bool(sms_rats)
    if emergency_rats is not None:
        result["emergency"] = bool(emergency_rats)
    if result.get("volte") is True or result.get("vowifi") is True:
        result["mmtel"] = True
    provisioning = bool_config(
        configs, "carrier_volte_provisioning_required_bool"
    )
    if provisioning is not None:
        result["volte_provisioning_required"] = provisioning
    return compact(result)


def _ims_and_sip(record: CarrierSettingRecord, plmn: str) -> tuple[dict[str, Any], dict[str, Any]]:
    configs = config_map(record.setting)
    ipsec = bool_config(configs, "ims.sip_over_ipsec_enabled_bool")
    transport_value = int_config(configs, "ims.sip_preferred_transport_int")
    transport = {0: "udp", 1: "tcp", 2: "auto", 3: "tls"}.get(transport_value)
    ims = compact(
        {
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
        }
    )
    expires = int_config(configs, "ims.registration_expiry_timer_sec_int")
    user_agent = translate_android_user_agent(
        text_config(configs, "ims.ims_user_agent_string")
    )
    register = compact(
        {
            "requested_expires_seconds": expires if expires and expires > 0 else None,
            "user_agent_template": user_agent,
        }
    )
    sip = {"common": {"register": register}} if register else {}
    return ims, sip


def _media(record: CarrierSettingRecord) -> dict[str, Any]:
    configs = config_map(record.setting)
    media: dict[str, Any] = {}
    audio: dict[str, Any] = {}
    for key, name in (
        ("imsvoice.amr_codec_attribute_mode_set_int_array", "AMR-NB"),
        ("imsvoice.amr_wb_codec_attribute_mode_set_int_array", "AMR-WB"),
        ("imsvoice.evs_codec_attribute_bandwidth_int_array", "EVS"),
    ):
        modes = array_config(configs, key)
        if modes is not None:
            audio.setdefault("codecs", []).append({"name": name, "modes": modes})
    if audio:
        media["audio"] = audio
    return media


def _entitlement(record: CarrierSettingRecord) -> dict[str, Any]:
    configs = config_map(record.setting)
    endpoint = text_config(
        configs, "imsserviceentitlement.entitlement_server_url_string"
    )
    if not endpoint:
        return {}
    return compact(
        {
            "protocol": "gsma_ts43",
            "endpoint": endpoint,
            "required": bool_config(configs, "require_entitlement_checks_bool"),
        }
    )


def _insert_match_and_evidence(
    connection: sqlite3.Connection,
    *,
    stats: ImportStats,
    profile_id: str,
    source_id: int,
    record: CarrierSettingRecord,
    values: dict[str, Any],
) -> None:
    cursor = connection.execute(
        """INSERT INTO profile_match_rules(
               profile_id, plmn, imsi_prefix, gid1, spn
           ) VALUES (?, ?, ?, ?, ?)""",
        (
            profile_id,
            values.get("plmn"),
            values.get("imsi_prefix"),
            values.get("gid1"),
            values.get("spn"),
        ),
    )
    rule_id = int(cursor.lastrowid)
    for key, value in values.items():
        _evidence(
            connection,
            stats=stats,
            profile_id=profile_id,
            source_id=source_id,
            target_kind="match_rule",
            target_path=f"/{rule_id}/{key}",
            source_path=record.source_path,
            source_key_path=_record_key_path(record, f"carrier_id.{key}"),
            value=value,
        )


def _config_evidence(
    connection: sqlite3.Connection,
    *,
    stats: ImportStats,
    profile_id: str,
    settings_source_id: int,
    standard_source_id: int | None,
    record: CarrierSettingRecord,
    config: dict[str, Any],
) -> None:
    # Keep evidence compact but sufficient to audit every normalized section.
    for section in ("access", "sip", "media", "services", "entitlement", "emergency"):
        value = config.get(section)
        if value:
            _evidence(
                connection,
                stats=stats,
                profile_id=profile_id,
                source_id=settings_source_id,
                target_kind="config",
                target_path=f"/{section}",
                source_path=record.source_path,
                source_key_path=_record_key_path(record, section),
                value=value,
                confidence=85,
            )
    if standard_source_id is not None:
        for pointer, value in (
            ("/ims/home_domain", config["ims"]["home_domain"]),
            ("/ims/realm", config["ims"]["realm"]),
            ("/ims/identity_templates", config["ims"]["identity_templates"]),
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


def import_pixel_catalog(
    database: Path,
    firmware: ExtractedPixelFirmware,
    *,
    device: str,
    device_name: str | None = None,
    os_version: str,
    build_id: str,
    source_uri: str,
    factory_sha256: str,
    include_standard_derived: bool = True,
) -> ImportStats:
    """Import one official firmware without storing device or OS identifiers."""

    del device, device_name, os_version, build_id
    stats = ImportStats()
    carrier_list_version, records = load_carrier_settings(firmware.carrier_settings_dir)
    stats.carrier_settings_seen = len(records)
    if firmware.mcfg_dir is not None:
        stats.mcfg_files_inventoried = sum(1 for _ in firmware.mcfg_dir.rglob("mcfg_sw.mbn"))

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        release = connection.execute(
            "SELECT sealed FROM catalog_metadata WHERE singleton = 1"
        ).fetchone()
        if release is None or release[0] != 0:
            raise RuntimeError("Pixel importer requires an unsealed v7 catalog")

        settings_source_id = _source_artifact(
            connection,
            source_kind="carrier_settings",
            source_uri=source_uri,
            artifact_sha256=factory_sha256,
            source_revision=str(carrier_list_version),
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

        for record in records:
            if not has_ims_data(record) or not record.carrier_ids:
                continue
            stats.carrier_settings_imported += 1
            carrier_slug = _slug(record.canonical_name)
            is_mvno = any(item.WhichOneof("mvno_data") for item in record.carrier_ids)
            connection.execute(
                """INSERT OR IGNORE INTO carriers(
                       carrier_id, canonical_name, brand_name, carrier_kind,
                       country_iso2, notes
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    carrier_slug,
                    record.canonical_name,
                    _display_name(record.canonical_name),
                    "mvno" if is_mvno else "mno",
                    _country_from_canonical(record.canonical_name),
                    "Name and scope are extracted from an official CarrierSettings file.",
                ),
            )

            seen: set[tuple[tuple[str, Any], ...]] = set()
            for carrier_id in record.carrier_ids:
                match = _match_values(carrier_id)
                if match is None:
                    continue
                match_key = tuple(sorted(match.items()))
                if match_key in seen:
                    continue
                seen.add(match_key)
                plmn = match["plmn"]
                ims, sip = _ims_and_sip(record, plmn)
                config: dict[str, Any] = {
                    "protocol_baseline": CONFIG_CONTRACT,
                    "ims": ims,
                    "access": _access_config(
                        record,
                        plmn,
                        include_standard_derived=include_standard_derived,
                        stats=stats,
                    ),
                    "sip": sip,
                    "media": _media(record),
                    "services": _services(record),
                    "entitlement": _entitlement(record),
                }
                raw_config, statuses = finalized_config(config)
                profile_id = f"profile-{carrier_slug}-{_profile_suffix(carrier_id)}"
                connection.execute(
                    """INSERT INTO carrier_profiles(
                           profile_id, carrier_id, display_name, profile_kind,
                           priority, confidence, lte_ims_status, nr_ims_status,
                           vowifi_status, config_json, notes
                       ) VALUES (?, ?, ?, ?, 100, 85, ?, ?, ?, ?, ?)""",
                    (
                        profile_id,
                        carrier_slug,
                        f"{_display_name(record.canonical_name)} {plmn}",
                        "mvno" if carrier_id.WhichOneof("mvno_data") else "default",
                        statuses["lte"],
                        statuses["nr"],
                        statuses["vowifi"],
                        raw_config,
                        f"CarrierSettings profile version {record.setting.version}",
                    ),
                )
                connection.execute(
                    """INSERT INTO profile_sources(
                           profile_id, source_id, source_profile_key,
                           source_path, contribution_kind, precedence
                       ) VALUES (?, ?, ?, ?, 'carrier_policy', 200)""",
                    (
                        profile_id,
                        settings_source_id,
                        record.canonical_name,
                        record.source_path,
                    ),
                )
                if standard_source_id is not None:
                    connection.execute(
                        """INSERT INTO profile_sources(
                               profile_id, source_id, source_profile_key,
                               source_path, contribution_kind, precedence
                           ) VALUES (?, ?, '3GPP IMS baseline', ?,
                                     'standard_default', 0)""",
                        (profile_id, standard_source_id, STANDARDS_URI),
                    )
                _insert_match_and_evidence(
                    connection,
                    stats=stats,
                    profile_id=profile_id,
                    source_id=settings_source_id,
                    record=record,
                    values=match,
                )
                _config_evidence(
                    connection,
                    stats=stats,
                    profile_id=profile_id,
                    settings_source_id=settings_source_id,
                    standard_source_id=standard_source_id,
                    record=record,
                    config=json.loads(raw_config),
                )
                stats.profiles_imported += 1

        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("foreign key check failed after Pixel import")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after Pixel import")
    return stats
