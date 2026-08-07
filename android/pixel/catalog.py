"""Import decoded Pixel CarrierSettings and Qualcomm MCFG inventory into SQLite."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import PARSER_NAME, PARSER_VERSION
from .carrier_settings import (
    CarrierSettingRecord,
    apn_as_dict,
    array_config,
    bool_config,
    config_map,
    config_value,
    has_ims_data,
    ims_apns,
    int_config,
    is_relevant_key,
    load_carrier_settings,
    normalized_apn,
    public_imsi_prefix,
    select_access_apn,
    text_config,
    translate_android_user_agent,
)
from .firmware import ExtractedPixelFirmware, sha256_file


STANDARDS_URI = "https://www.3gpp.org/ftp/Specs/archive/23_series/23.003/"
LICENSE_NOTE = (
    "Google Pixel device software; extraction and use remain subject to the "
    "terms published with the factory image"
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


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return result or "unknown"


def _country_from_canonical(name: str) -> str | None:
    match = re.search(r"_([a-z]{2})$", name)
    return match.group(1).upper() if match else None


def _display_name(name: str) -> str:
    return re.sub(r"[_=]+", " ", name).strip().title()


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "array"
    return "object"


def _source_snapshot(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    platform: str = "android",
    vendor: str | None = "Google",
    device_family: str | None = "Pixel",
    device: str | None,
    os_version: str | None,
    build_id: str | None,
    baseband_version: str | None,
    source_revision: str | None,
    source_uri: str,
    artifact_sha256: str | None,
    license_note: str = LICENSE_NOTE,
) -> int:
    cursor = connection.execute(
        """INSERT INTO source_snapshots(
               source_kind, platform, vendor, device_family, device_model,
               os_version, build_id, baseband_version, source_revision,
               source_uri, artifact_sha256, extracted_at, parser_name,
               parser_version, license_note
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_kind,
            platform,
            vendor,
            device_family,
            device,
            os_version,
            build_id,
            baseband_version,
            source_revision,
            source_uri,
            artifact_sha256,
            datetime.now(timezone.utc).isoformat(),
            PARSER_NAME,
            PARSER_VERSION,
            license_note,
        ),
    )
    return int(cursor.lastrowid)


def _record_key_path(record: CarrierSettingRecord, suffix: str) -> str:
    canonical_name = json.dumps(record.canonical_name, ensure_ascii=True)
    return f"settings[{canonical_name}].{suffix}"


def _evidence(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    source_id: int,
    table_name: str,
    row_key: str | None,
    field_name: str,
    source_path: str,
    key_path: str,
    kind: str = "extracted",
    confidence: int = 95,
) -> None:
    connection.execute(
        """INSERT INTO field_evidence(
               profile_id, source_id, table_name, row_key, field_name,
               source_path, key_path, evidence_kind, confidence
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            profile_id,
            source_id,
            table_name,
            row_key,
            field_name,
            source_path,
            key_path,
            kind,
            confidence,
        ),
    )


def _insert_raw_source_values(
    connection: sqlite3.Connection,
    source_id: int,
    record: CarrierSettingRecord,
) -> int:
    count = 0
    for index, apn in enumerate(ims_apns(record.setting)):
        value = apn_as_dict(apn)
        connection.execute(
            """INSERT INTO raw_config_values(
                   source_id, source_path, key_path, value_json, value_type
               ) VALUES (?, ?, ?, ?, 'object')""",
            (
                source_id,
                record.source_path,
                _record_key_path(record, f"apns.ims[{index}]"),
                json.dumps(value, ensure_ascii=True, sort_keys=True),
            ),
        )
        count += 1
    for index, config in enumerate(record.setting.configs.config):
        if not is_relevant_key(config.key):
            continue
        value = config_value(config)
        connection.execute(
            """INSERT INTO raw_config_values(
                   source_id, source_path, key_path, value_json, value_type
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                source_id,
                record.source_path,
                _record_key_path(record, f"configs[{index}].{config.key}"),
                json.dumps(value, ensure_ascii=True, sort_keys=True),
                _json_type(value),
            ),
        )
        count += 1
    return count


def _profile_suffix(carrier_id: Any) -> str:
    mvno_kind = carrier_id.WhichOneof("mvno_data")
    parts = [carrier_id.mcc_mnc]
    if mvno_kind:
        parts.extend((mvno_kind, getattr(carrier_id, mvno_kind)))
    readable = _slug("-".join(parts))[:48]
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:10]
    return f"{readable}-{digest}"


def _insert_match(
    connection: sqlite3.Connection,
    profile_id: str,
    carrier_id: Any,
    device: str,
) -> str | None:
    plmn = carrier_id.mcc_mnc
    if not re.fullmatch(r"[0-9]{5,6}", plmn) or plmn.startswith("000"):
        return None
    mcc, mnc = plmn[:3], plmn[3:]
    connection.execute(
        """INSERT OR IGNORE INTO plmns(plmn, mcc, mnc, mnc_length)
           VALUES (?, ?, ?, ?)""",
        (plmn, mcc, mnc, len(mnc)),
    )
    mvno_kind = carrier_id.WhichOneof("mvno_data")
    values = {"spn": None, "gid1": None, "imsi_prefix": None}
    if mvno_kind == "spn":
        values["spn"] = carrier_id.spn
    elif mvno_kind == "gid1":
        values["gid1"] = carrier_id.gid1
    elif mvno_kind == "imsi":
        values["imsi_prefix"] = public_imsi_prefix(carrier_id.imsi)
    connection.execute(
        """INSERT INTO profile_match_rules(
               profile_id, plmn, imsi_prefix, gid1, spn, device_model_pattern
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            profile_id,
            plmn,
            values["imsi_prefix"],
            values["gid1"],
            values["spn"],
            device,
        ),
    )
    return plmn


def _insert_access_configs(
    connection: sqlite3.Connection,
    *,
    record: CarrierSettingRecord,
    profile_id: str,
    source_id: int,
    stats: ImportStats,
) -> dict[str, int]:
    configs = config_map(record.setting)
    wfc_available = bool_config(configs, "carrier_wfc_ims_available_bool")
    vonr_available = bool_config(configs, "carrier_vonr_available_bool")
    access_ids: dict[str, int] = {}
    for access_kind in ("lte_epc", "nr_5gc", "wifi_epdg"):
        apn, ambiguous = select_access_apn(record.setting, access_kind)
        if ambiguous:
            stats.ambiguous_access_configs += 1
        epdg = text_config(configs, "iwlan.epdg_static_address_string")
        needs_wifi_row = access_kind == "wifi_epdg" and (wfc_available is not None or epdg)
        if apn is None and not needs_wifi_row:
            continue
        values = normalized_apn(apn) if apn is not None else {
            "apn_dnn": None,
            "apn_auth_type": None,
            "apn_username": None,
            "apn_password": None,
            "ip_family": None,
            "roaming_ip_family": None,
            "mtu": None,
        }
        enabled = wfc_available if access_kind == "wifi_epdg" else (
            vonr_available if access_kind == "nr_5gc" else None
        )
        cursor = connection.execute(
            """INSERT INTO access_configs(
                   profile_id, access_kind, purpose, enabled, apn_dnn,
                   apn_auth_type, apn_username, apn_password, ip_family,
                   roaming_ip_family, mtu
               ) VALUES (?, ?, 'ims', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id,
                access_kind,
                enabled,
                values["apn_dnn"],
                values["apn_auth_type"],
                values["apn_username"],
                values["apn_password"],
                values["ip_family"],
                values["roaming_ip_family"],
                values["mtu"],
            ),
        )
        access_id = int(cursor.lastrowid)
        access_ids[access_kind] = access_id
        stats.access_configs_imported += 1
        if apn is not None:
            for field, value in values.items():
                if value is not None:
                    _evidence(
                        connection,
                        profile_id=profile_id,
                        source_id=source_id,
                        table_name="access_configs",
                        row_key=f"{profile_id}:{access_kind}:ims",
                        field_name=field,
                        source_path=record.source_path,
                        key_path=_record_key_path(record, "apns[type=IMS]"),
                    )

    wifi_access = access_ids.get("wifi_epdg")
    epdg = text_config(configs, "iwlan.epdg_static_address_string")
    if wifi_access and epdg:
        connection.execute(
            """INSERT INTO network_endpoints(
                   profile_id, access_id, service, address_kind, address,
                   transport, discovery_method, roaming_scope
               ) VALUES (?, ?, 'epdg', 'fqdn', ?, 'ikev2', 'static', 'home')""",
            (profile_id, wifi_access, epdg.rstrip(".")),
        )
        _evidence(
            connection,
            profile_id=profile_id,
            source_id=source_id,
            table_name="network_endpoints",
            row_key=f"{profile_id}:epdg:home:0",
            field_name="address",
            source_path=record.source_path,
            key_path=_record_key_path(
                record, "configs.iwlan.epdg_static_address_string"
            ),
        )
    roaming_epdg = text_config(configs, "iwlan.epdg_static_address_roaming_string")
    if wifi_access and roaming_epdg:
        connection.execute(
            """INSERT INTO network_endpoints(
                   profile_id, access_id, service, position, address_kind,
                   address, transport, discovery_method, roaming_scope
               ) VALUES (?, ?, 'epdg', 1, 'fqdn', ?, 'ikev2', 'static', 'visited')""",
            (profile_id, wifi_access, roaming_epdg.rstrip(".")),
        )

    xcap = text_config(configs, "imsss.ut_as_server_fqdn_string")
    if xcap:
        connection.execute(
            """INSERT INTO network_endpoints(
                   profile_id, service, address_kind, address, port,
                   discovery_method
               ) VALUES (?, 'xcap', 'fqdn', ?, ?, 'static')""",
            (profile_id, xcap.rstrip("."), int_config(configs, "imsss.ut_as_server_port_int")),
        )
    entitlement = text_config(configs, "imsserviceentitlement.entitlement_server_url_string")
    if entitlement:
        endpoint = connection.execute(
            """INSERT INTO network_endpoints(
                   profile_id, service, address_kind, address, transport,
                   discovery_method
               ) VALUES (?, 'entitlement', 'uri', ?, 'https', 'static')""",
            (profile_id, entitlement),
        ).lastrowid
        connection.execute(
            """INSERT INTO entitlement_configs(
                   profile_id, service, protocol, endpoint_id, required
               ) VALUES (?, 'vowifi', 'gsma_ts43', ?, ?)""",
            (
                profile_id,
                endpoint,
                bool_config(configs, "require_entitlement_checks_bool"),
            ),
        )
    return access_ids


def _insert_capabilities(
    connection: sqlite3.Connection,
    *,
    record: CarrierSettingRecord,
    profile_id: str,
) -> None:
    configs = config_map(record.setting)
    capabilities: dict[str, bool | None] = {
        "ims": True if ims_apns(record.setting) else None,
        "volte": bool_config(configs, "carrier_volte_available_bool"),
        "vonr": bool_config(configs, "carrier_vonr_available_bool"),
        "vowifi": bool_config(configs, "carrier_wfc_ims_available_bool"),
        "ut_xcap": bool_config(configs, "carrier_supports_ss_over_ut_bool"),
        "video": bool_config(configs, "carrier_vt_available_bool"),
    }
    sms_rats = array_config(configs, "imssms.sms_over_ims_supported_rats_int_array")
    emergency_rats = array_config(configs, "imsemergency.emergency_over_ims_supported_rats_int_array")
    if sms_rats is not None:
        capabilities["smsoip"] = bool(sms_rats)
    if emergency_rats is not None:
        capabilities["emergency"] = bool(emergency_rats)
    if capabilities["volte"] is True or capabilities["vowifi"] is True:
        capabilities["mmtel"] = True

    for service, supported in capabilities.items():
        if supported is None:
            continue
        provisioning = (
            bool_config(configs, "carrier_volte_provisioning_required_bool")
            if service == "volte"
            else None
        )
        connection.execute(
            """INSERT INTO service_capabilities(
                   profile_id, service, supported, provisioning_required
               ) VALUES (?, ?, ?, ?)""",
            (profile_id, service, supported, provisioning),
        )
    if emergency_rats is not None:
        connection.execute(
            "INSERT INTO emergency_configs(profile_id, supported) VALUES (?, ?)",
            (profile_id, bool(emergency_rats)),
        )


def _insert_standard_ims(
    connection: sqlite3.Connection,
    *,
    record: CarrierSettingRecord,
    profile_id: str,
    plmn: str,
    extracted_source_id: int,
    standard_source_id: int,
) -> None:
    mcc, mnc = plmn[:3], plmn[3:]
    home_domain = f"ims.mnc{int(mnc):03d}.mcc{mcc}.3gppnetwork.org"
    configs = config_map(record.setting)
    ipsec_value = bool_config(configs, "ims.sip_over_ipsec_enabled_bool")
    ipsec = "auto" if ipsec_value is None else ("required" if ipsec_value else "disabled")
    transport_value = int_config(configs, "ims.sip_preferred_transport_int")
    transport = {0: "udp", 1: "tcp", 2: "auto", 3: "tls"}.get(
        transport_value, "auto"
    )
    connection.execute(
        """INSERT INTO ims_configs(
               profile_id, home_domain, realm, private_identity_source,
               public_identity_source, transport_preference,
               ipsec_security_agreement
           ) VALUES (?, ?, ?, 'auto', 'auto', ?, ?)""",
        (profile_id, home_domain, home_domain, transport, ipsec),
    )
    for field in ("home_domain", "realm"):
        _evidence(
            connection,
            profile_id=profile_id,
            source_id=standard_source_id,
            table_name="ims_configs",
            row_key=profile_id,
            field_name=field,
            source_path="3GPP TS 23.003",
            key_path="IMS home network domain derivation",
            kind="standard_derived",
            confidence=60,
        )
    if ipsec_value is not None:
        _evidence(
            connection,
            profile_id=profile_id,
            source_id=extracted_source_id,
            table_name="ims_configs",
            row_key=profile_id,
            field_name="ipsec_security_agreement",
            source_path=record.source_path,
            key_path=_record_key_path(
                record, "configs.ims.sip_over_ipsec_enabled_bool"
            ),
        )
    if transport != "auto":
        _evidence(
            connection,
            profile_id=profile_id,
            source_id=extracted_source_id,
            table_name="ims_configs",
            row_key=profile_id,
            field_name="transport_preference",
            source_path=record.source_path,
            key_path=_record_key_path(
                record, "configs.ims.sip_preferred_transport_int"
            ),
            confidence=85,
        )

    templates = (
        ("impi", "nai", "{imsi}@{home_domain}"),
        ("impu", "sip_uri", "sip:{imsi}@{home_domain}"),
    )
    for position, (role, identity_type, template) in enumerate(templates):
        connection.execute(
            """INSERT INTO ims_identity_templates(
                   profile_id, role, position, source_policy, identity_type,
                   value_template, use_when
               ) VALUES (?, ?, ?, 'derived_imsi', ?, ?, 'if_isim_missing')""",
            (profile_id, role, position, identity_type, template),
        )

    expires = int_config(configs, "ims.registration_expiry_timer_sec_int")
    user_agent = translate_android_user_agent(
        text_config(configs, "ims.ims_user_agent_string")
    )
    if expires and expires <= 0:
        expires = None
    if expires or user_agent:
        connection.execute(
            """INSERT INTO sip_register_configs(
                   profile_id, scope, requested_expires_seconds,
                   user_agent_template
               ) VALUES (?, 'common', ?, ?)""",
            (profile_id, expires, user_agent),
        )


def _inventory_mcfg(
    connection: sqlite3.Connection,
    *,
    mcfg_dir: Path | None,
    device: str,
    device_name: str | None,
    os_version: str,
    build_id: str,
    baseband_version: str | None,
    source_uri: str,
) -> int:
    if mcfg_dir is None:
        return 0
    count = 0
    for path in sorted(mcfg_dir.rglob("mcfg_sw.mbn")):
        relative = path.relative_to(mcfg_dir).as_posix()
        digest = sha256_file(path)
        source_id = _source_snapshot(
            connection,
            source_kind="qualcomm_mcfg",
            platform="modem",
            vendor="Qualcomm",
            device_family=device_name or "Pixel",
            device=device,
            os_version=os_version,
            build_id=build_id,
            baseband_version=baseband_version,
            source_revision=relative,
            source_uri=source_uri,
            artifact_sha256=digest,
        )
        manifest = {
            "relative_path": relative,
            "size": path.stat().st_size,
            "sha256": digest,
            "parser_status": "inventoried_not_semantically_decoded",
        }
        connection.execute(
            """INSERT INTO raw_config_values(
                   source_id, source_path, key_path, value_json, value_type
               ) VALUES (?, ?, 'artifact', ?, 'object')""",
            (source_id, relative, json.dumps(manifest, sort_keys=True)),
        )
        count += 1
    return count


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
    stats = ImportStats()
    carrier_list_version, records = load_carrier_settings(firmware.carrier_settings_dir)
    stats.carrier_settings_seen = len(records)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        release = connection.execute(
            "SELECT sealed FROM catalog_release WHERE singleton = 1"
        ).fetchone()
        if release is None or release[0] != 0:
            raise RuntimeError("Pixel importer requires an unsealed catalog database")

        _source_snapshot(
            connection,
            source_kind="firmware_metadata",
            device_family=device_name or "Pixel",
            device=device,
            os_version=os_version,
            build_id=build_id,
            baseband_version=firmware.baseband_version,
            source_revision=None,
            source_uri=source_uri,
            artifact_sha256=factory_sha256,
        )
        settings_source_id = _source_snapshot(
            connection,
            source_kind="android_carrier_config",
            device_family=device_name or "Pixel",
            device=device,
            os_version=os_version,
            build_id=build_id,
            baseband_version=firmware.baseband_version,
            source_revision=str(carrier_list_version),
            source_uri=source_uri,
            artifact_sha256=factory_sha256,
        )
        standard_source_id: int | None = None
        if include_standard_derived:
            standard_source_id = _source_snapshot(
                connection,
                source_kind="standards_reference",
                platform="shared",
                vendor="3GPP",
                device_family=None,
                device=None,
                os_version=None,
                build_id=None,
                baseband_version=None,
                source_revision="3GPP TS 23.003",
                source_uri=STANDARDS_URI,
                artifact_sha256=None,
                license_note="3GPP specification reference; no subscriber data",
            )

        for record in records:
            if not has_ims_data(record) or not record.carrier_ids:
                continue
            stats.carrier_settings_imported += 1
            stats.relevant_raw_values += _insert_raw_source_values(
                connection, settings_source_id, record
            )
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
                    "Name and scope are taken from Google Pixel CarrierSettings.",
                ),
            )

            seen_matches: set[tuple[str, str | None, str | None]] = set()
            for match in record.carrier_ids:
                mvno_kind = match.WhichOneof("mvno_data")
                mvno_value = getattr(match, mvno_kind) if mvno_kind else None
                match_key = (match.mcc_mnc, mvno_kind, mvno_value)
                if match_key in seen_matches:
                    continue
                seen_matches.add(match_key)
                if not re.fullmatch(r"[0-9]{5,6}", match.mcc_mnc) or match.mcc_mnc.startswith("000"):
                    continue
                profile_id = f"pixel-{_slug(device)}-{carrier_slug}-{_profile_suffix(match)}"
                display = f"{_display_name(record.canonical_name)} {match.mcc_mnc}"
                connection.execute(
                    """INSERT INTO carrier_profiles(
                           profile_id, carrier_id, display_name, profile_kind,
                           priority, confidence, notes
                       ) VALUES (?, ?, ?, ?, 100, 85, ?)""",
                    (
                        profile_id,
                        carrier_slug,
                        display,
                        "mvno" if mvno_kind else "device_specific",
                        f"{device_name or 'Pixel'} ({device}) CarrierSettings "
                        f"{record.setting.version}",
                    ),
                )
                connection.execute(
                    """INSERT INTO profile_sources(
                           profile_id, source_id, source_profile_key,
                           source_path, source_priority
                       ) VALUES (?, ?, ?, ?, 80)""",
                    (
                        profile_id,
                        settings_source_id,
                        record.canonical_name,
                        record.source_path,
                    ),
                )
                plmn = _insert_match(connection, profile_id, match, device)
                if plmn is None:
                    continue
                _insert_access_configs(
                    connection,
                    record=record,
                    profile_id=profile_id,
                    source_id=settings_source_id,
                    stats=stats,
                )
                _insert_capabilities(connection, record=record, profile_id=profile_id)
                if include_standard_derived and standard_source_id is not None:
                    _insert_standard_ims(
                        connection,
                        record=record,
                        profile_id=profile_id,
                        plmn=plmn,
                        extracted_source_id=settings_source_id,
                        standard_source_id=standard_source_id,
                    )
                stats.profiles_imported += 1

        stats.mcfg_files_inventoried = _inventory_mcfg(
            connection,
            mcfg_dir=firmware.mcfg_dir,
            device=device,
            device_name=device_name,
            os_version=os_version,
            build_id=build_id,
            baseband_version=firmware.baseband_version,
            source_uri=source_uri,
        )
        connection.commit()
        foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise RuntimeError(f"foreign key check failed: {foreign_key_error}")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after Pixel import")
    return stats
