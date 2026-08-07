"""Normalize Apple Carrier Bundle IMS and VoWiFi facts into the shared catalog."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import PARSER_NAME, PARSER_VERSION
from .bundles import (
    CarrierBundleVariant,
    IOSMatchRule,
    canonical_json,
    find_bundle_root,
    hash_bundle_tree,
    load_carrier_bundle_variants,
    relevant_raw_values,
)
from .firmware import sha256_file
from .sources import IPSWArtifact


STANDARDS_URI = "https://www.3gpp.org/ftp/Specs/archive/23_series/23.003/"
APPLE_LICENSE_NOTE = (
    "Apple device software; extraction and use remain subject to the terms "
    "accompanying the IPSW"
)
IMS_TYPE_MASK = 131072
EMERGENCY_TYPE_MASK = 262144
TEMPLATE_TOKEN = re.compile(
    r"\$\{(?P<braced>[A-Za-z0-9_-]+)\}|\$(?P<plain>IMSI|impi_user|MCC|MNC)",
    re.IGNORECASE,
)


@dataclass
class IOSImportStats:
    bundles_seen: int = 0
    variants_seen: int = 0
    profiles_imported: int = 0
    match_rules_imported: int = 0
    access_configs_imported: int = 0
    ims_configs_imported: int = 0
    ike_configs_imported: int = 0
    entitlement_endpoints_imported: int = 0
    raw_values_imported: int = 0
    ambiguous_ims_apns: int = 0


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unknown"


def _country_from_bundle(name: str) -> str | None:
    match = re.search(r"_([A-Za-z]{2})(?:\.bundle)?$", name)
    return match.group(1).upper() if match else None


def _source_snapshot(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    platform: str,
    artifact: IPSWArtifact,
    source_revision: str | None,
    artifact_sha256: str | None,
    parser_name: str = PARSER_NAME,
    parser_version: str = PARSER_VERSION,
    vendor: str | None = "Apple",
    device_family: str | None = "iPhone",
    device_model: str | None = None,
    source_uri: str | None = None,
    license_note: str = APPLE_LICENSE_NOTE,
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
            device_model if device_model is not None else artifact.product_type,
            artifact.os_version,
            artifact.build_id,
            artifact.baseband_version,
            source_revision,
            source_uri if source_uri is not None else artifact.url,
            artifact_sha256,
            datetime.now(timezone.utc).isoformat(),
            parser_name,
            parser_version,
            license_note,
        ),
    )
    return int(cursor.lastrowid)


def _standard_source(connection: sqlite3.Connection, artifact: IPSWArtifact) -> int:
    cursor = connection.execute(
        """INSERT INTO source_snapshots(
               source_kind, platform, vendor, source_revision, source_uri,
               extracted_at, parser_name, parser_version, license_note
           ) VALUES (
               'standards_reference', 'shared', '3GPP', ?, ?, ?, ?, ?, ?
           )""",
        (
            "3GPP TS 23.003",
            STANDARDS_URI,
            datetime.now(timezone.utc).isoformat(),
            "3gpp-domain-templates",
            "1",
            "3GPP specification reference; no subscriber data",
        ),
    )
    return int(cursor.lastrowid)


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


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _as_bool(value: Any) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _nested(config: dict[str, Any], *keys: str) -> Any:
    value: Any = config
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _translate_template(value: str) -> str:
    names = {
        "imsi": "imsi",
        "impi_user": "impi_user",
        "mcc": "mcc",
        "mnc": "mnc3",
        "device": "device_model",
        "device_ref_id": "device_reference_id",
        "os": "os_name",
        "os_version": "os_version",
    }

    def replace(match: re.Match[str]) -> str:
        token = (match.group("braced") or match.group("plain")).casefold()
        replacement = names.get(token)
        return "{" + replacement + "}" if replacement else match.group(0)

    return TEMPLATE_TOKEN.sub(replace, value)


def _profile_suffix(rule: IOSMatchRule) -> str:
    fields = (rule.plmn, rule.gid1 or "", rule.gid2 or "", rule.iccid_prefix or "")
    readable = _slug("-".join(item for item in fields if item))[:56]
    digest = hashlib.sha256("\0".join(fields).encode()).hexdigest()[:10]
    return f"{readable}-{digest}"


def _insert_match(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    rule: IOSMatchRule,
    country: str | None,
    product_type: str,
) -> None:
    mcc, mnc = rule.plmn[:3], rule.plmn[3:]
    connection.execute(
        """INSERT OR IGNORE INTO plmns(
               plmn, mcc, mnc, mnc_length, country_iso2
           ) VALUES (?, ?, ?, ?, ?)""",
        (rule.plmn, mcc, mnc, len(mnc), country),
    )
    connection.execute(
        """INSERT INTO profile_match_rules(
               profile_id, plmn, iccid_prefix, gid1, gid2,
               device_model_pattern
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            profile_id,
            rule.plmn,
            rule.iccid_prefix,
            rule.gid1,
            rule.gid2,
            product_type,
        ),
    )


def _iter_apns(value: Any):
    if isinstance(value, dict):
        if isinstance(value.get("apn"), str):
            yield value
        for child in value.values():
            yield from _iter_apns(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_apns(child)


def _ims_apns(config: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for apn in _iter_apns(config.get("apns", [])):
        type_mask = _as_int(apn.get("type-mask")) or 0
        tech_mask = _as_int(apn.get("tech-type-mask")) or 0
        if not ((type_mask | tech_mask) & IMS_TYPE_MASK):
            continue
        marker = canonical_json(apn)
        if marker not in seen:
            seen.add(marker)
            result.append(apn)
    for apn in _iter_apns(config.get("AttachAPN", {})):
        if _as_int(apn.get("APNClass")) != 3:
            continue
        marker = canonical_json(apn)
        if marker not in seen:
            seen.add(marker)
            result.append(apn)
    return result


def _ip_family(value: Any) -> str | None:
    return {1: "ipv4", 2: "ipv6", 3: "ipv4v6"}.get(_as_int(value))


def _auth_type(apn: dict[str, Any]) -> str | None:
    value = str(apn.get("auth_type", "")).casefold()
    if value == "pap":
        return "pap"
    if value == "chap":
        return "chap"
    username, password = apn.get("username"), apn.get("password")
    if username == "" and password == "":
        return "none"
    return None


def _insert_access(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    source_id: int,
    source_path: str,
    config: dict[str, Any],
    stats: IOSImportStats,
) -> tuple[dict[str, int], dict[str, Any] | None]:
    candidates = _ims_apns(config)
    if len(candidates) > 1:
        stats.ambiguous_ims_apns += 1
    apn = candidates[0] if candidates else None
    supports_ims = _as_bool(config.get("SupportsImsCapability"))
    supports_vonr = _as_bool(config.get("SupportsVoNR"))
    ike = _nested(config, "TechSettings", "IKE")
    has_wifi = isinstance(ike, dict) and isinstance(ike.get("RemoteAddress"), str)
    has_nr_apn = apn is not None and apn.get("Support5GSaHandOver") is True
    access_ids: dict[str, int] = {}

    kinds: list[tuple[str, int | None]] = []
    if apn is not None:
        kinds.append(("lte_epc", supports_ims))
    if apn is not None and (has_nr_apn or supports_vonr is not None):
        kinds.append(("nr_5gc", supports_vonr))
    if has_wifi:
        kinds.append(("wifi_epdg", 1))

    for kind, enabled in kinds:
        cursor = connection.execute(
            """INSERT INTO access_configs(
                   profile_id, access_kind, purpose, enabled, apn_dnn,
                   apn_auth_type, apn_username, apn_password, ip_family,
                   roaming_ip_family, pcscf_required
               ) VALUES (?, ?, 'ims', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id,
                kind,
                enabled,
                apn.get("apn") if apn else None,
                _auth_type(apn) if apn else None,
                apn.get("username") or None if apn else None,
                apn.get("password") or None if apn else None,
                _ip_family(apn.get("AllowedProtocolMask")) if apn else None,
                _ip_family(apn.get("AllowedProtocolMaskInRoaming")) if apn else None,
                _as_bool(apn.get("PcscfAddressRequired")) if apn else None,
            ),
        )
        access_id = int(cursor.lastrowid)
        access_ids[kind] = access_id
        stats.access_configs_imported += 1
        if apn is not None:
            _evidence(
                connection,
                profile_id=profile_id,
                source_id=source_id,
                table_name="access_configs",
                row_key=f"{profile_id}:{kind}:ims",
                field_name="apn_dnn",
                source_path=source_path,
                key_path="apns[type-mask&131072]",
            )

    wifi_access = access_ids.get("wifi_epdg")
    if wifi_access is not None:
        attrs = _nested(config, "TechSettings", "ExtraConfigurationAttributeRequestv4")
        attrs6 = _nested(config, "TechSettings", "ExtraConfigurationAttributeRequestv6")
        requested = [*attrs] if isinstance(attrs, list) else []
        if isinstance(attrs6, list):
            requested.extend(attrs6)
        if any("pcscf" in str(item.get("Name", "")).casefold() for item in requested if isinstance(item, dict)):
            connection.execute(
                """INSERT INTO pcscf_discovery_methods(access_id, position, method)
                   VALUES (?, 0, 'ike_cfg')""",
                (wifi_access,),
            )
    return access_ids, apn


def _identity_type(value: Any, template: str) -> str:
    normalized = str(value or "").casefold()
    if normalized in {"idfqdn", "id_fqdn"}:
        return "id_fqdn"
    if normalized in {"iduserfqdn", "id_rfc822_addr"} or "@" in template:
        return "id_rfc822_addr"
    if normalized in {"keyid", "idkeyid", "id_key_id"}:
        return "id_key_id"
    return "id_fqdn"


def _proposal_value(proposal: dict[str, Any]) -> str:
    fields = (
        ("encr", proposal.get("EncryptionAlgorithm")),
        ("integ", proposal.get("IntegrityAlgorithm")),
        ("prf", proposal.get("PRFAlgorithm")),
        ("dh", proposal.get("DHGroup")),
        ("auth", proposal.get("AuthenticationMethod")),
        ("eap", proposal.get("EAPMethod")),
    )
    return ";".join(f"{key}={value}" for key, value in fields if value not in (None, ""))


def _scalar_algorithm(value: Any) -> str | None:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value) if value not in (None, "") else None


def _insert_ike(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    source_id: int,
    source_path: str,
    config: dict[str, Any],
    access_id: int | None,
    stats: IOSImportStats,
) -> None:
    if access_id is None:
        return
    ike = _nested(config, "TechSettings", "IKE")
    if not isinstance(ike, dict):
        return
    remote_address = ike.get("RemoteAddress")
    if not isinstance(remote_address, str) or not remote_address:
        return
    local_identity = ike.get("LocalIdentifier")
    local_template = (
        _translate_template(local_identity) if isinstance(local_identity, str) else None
    )
    remote_identity = ike.get("RemoteIdentifier")
    remote_template = (
        _translate_template(remote_identity) if isinstance(remote_identity, str) else None
    )
    proposals = ike.get("Proposals")
    proposals = proposals if isinstance(proposals, list) else []
    first = proposals[0] if proposals and isinstance(proposals[0], dict) else {}
    eap_value = str(first.get("EAPMethod", "EAP-AKA")).casefold()
    eap_method = {
        "eap-aka": "eap_aka",
        "eap-aka'": "eap_aka_prime",
        "eap-aka-prime": "eap_aka_prime",
        "eap-tls": "certificate",
    }.get(eap_value, "other")
    validate_certificate = ike.get("ValidateRemoteCertificate")
    certificate_policy = "system_trust" if validate_certificate is True else None
    lifetime = _as_int(first.get("Lifetime"))
    child = _nested(config, "TechSettings", "ChildSAs", "FirstChild")
    child_proposals = child.get("ChildProposals", []) if isinstance(child, dict) else []
    first_child = (
        child_proposals[0]
        if isinstance(child_proposals, list)
        and child_proposals
        and isinstance(child_proposals[0], dict)
        else {}
    )
    connection.execute(
        """INSERT INTO ike_configs(
               access_id, eap_method, local_identity_format,
               remote_identity_format, nat_keepalive_enabled,
               nat_keepalive_seconds, dpd_enabled, dpd_interval_seconds,
               dpd_retry_interval_seconds, dpd_max_retries,
               ike_sa_lifetime_seconds, child_sa_lifetime_seconds,
               certificate_policy, validate_remote_certificate,
               trusted_ca_ref, certificate_hostname
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            access_id,
            eap_method,
            local_template,
            remote_template,
            _as_bool(ike.get("NATTKeepAliveEnabled")),
            _as_int(ike.get("NATTKeepAliveInterval")),
            _as_bool(ike.get("DeadPeerDetectionEnabled")),
            _as_int(ike.get("DeadPeerDetectionInterval")),
            _as_int(ike.get("DeadPeerDetectionRetryInterval")),
            _as_int(ike.get("DeadPeerDetectionMaxRetries")),
            lifetime,
            _as_int(first_child.get("Lifetime")),
            certificate_policy,
            _as_bool(validate_certificate),
            ike.get("RemoteCertificateAuthorityName"),
            ike.get("RemoteCertificateHostname"),
        ),
    )
    stats.ike_configs_imported += 1

    endpoint = _translate_template(remote_address).rstrip(".")
    try:
        parsed_ip = ipaddress.ip_address(endpoint)
        address_kind = "ipv4" if parsed_ip.version == 4 else "ipv6"
    except ValueError:
        address_kind = "derived_template" if "{" in endpoint else "fqdn"
    connection.execute(
        """INSERT INTO network_endpoints(
               profile_id, access_id, service, address_kind, address,
               transport, discovery_method, roaming_scope
           ) VALUES (?, ?, 'epdg', ?, ?, 'ikev2', 'static', 'both')""",
        (profile_id, access_id, address_kind, endpoint),
    )
    _evidence(
        connection,
        profile_id=profile_id,
        source_id=source_id,
        table_name="network_endpoints",
        row_key=f"{profile_id}:epdg:0",
        field_name="address",
        source_path=source_path,
        key_path="TechSettings.IKE.RemoteAddress",
    )

    if local_template:
        source_policy = "derived_imsi" if "{imsi}" in local_template else "configured_template"
        connection.execute(
            """INSERT INTO ike_identity_rules(
                   access_id, role, position, identity_type, source_policy,
                   value_template, send_policy, required
               ) VALUES (?, 'idi', 0, ?, ?, ?, 'always', 1)""",
            (
                access_id,
                _identity_type(ike.get("LocalIdentifierType"), local_template),
                source_policy,
                local_template,
            ),
        )
    if remote_template:
        connection.execute(
            """INSERT INTO ike_identity_rules(
                   access_id, role, position, identity_type, source_policy,
                   value_template, send_policy, required
               ) VALUES (?, 'idr', 0, ?, 'configured_template', ?, 'always', 1)""",
            (
                access_id,
                _identity_type(ike.get("RemoteIdentifierType"), remote_template),
                remote_template,
            ),
        )

    for position, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            continue
        connection.execute(
            """INSERT INTO crypto_proposals(
                   access_id, phase, position, canonical_value, encryption,
                   integrity, prf, dh_group
               ) VALUES (?, 'ike_sa', ?, ?, ?, ?, ?, ?)""",
            (
                access_id,
                position,
                _proposal_value(proposal),
                _scalar_algorithm(proposal.get("EncryptionAlgorithm")),
                _scalar_algorithm(proposal.get("IntegrityAlgorithm")),
                _scalar_algorithm(proposal.get("PRFAlgorithm")),
                str(proposal["DHGroup"]) if "DHGroup" in proposal else None,
            ),
        )

    if isinstance(child_proposals, list):
        for position, proposal in enumerate(child_proposals):
            if not isinstance(proposal, dict):
                continue
            connection.execute(
                """INSERT INTO crypto_proposals(
                       access_id, phase, position, canonical_value,
                       encryption, integrity, dh_group
                   ) VALUES (?, 'child_sa', ?, ?, ?, ?, ?)""",
                (
                    access_id,
                    position,
                    _proposal_value(proposal),
                    _scalar_algorithm(proposal.get("EncryptionAlgorithm")),
                    _scalar_algorithm(proposal.get("IntegrityAlgorithm")),
                    str(proposal["DHGroup"]) if "DHGroup" in proposal else None,
                ),
            )


def _standard_domain(plmn: str) -> str:
    return f"ims.mnc{plmn[3:].zfill(3)}.mcc{plmn[:3]}.3gppnetwork.org"


def _split_parameters(value: str) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif character == '"':
            current.append(character)
            quoted = not quoted
        elif character == ";" and not quoted:
            token = "".join(current).strip()
            if token:
                result.append(token)
            current = []
        else:
            current.append(character)
    token = "".join(current).strip()
    if token:
        result.append(token)
    return result


def _selector_scope(selector: str) -> str:
    lowered = selector.casefold()
    if "[-wifi]" in lowered:
        return "cellular"
    if "[wifi" in lowered or ",wifi" in lowered:
        return "wifi"
    return "common"


def _register_contact_values(signaling: dict[str, Any]) -> dict[str, list[tuple[str, str | None]]]:
    result: dict[str, list[tuple[str, str | None]]] = {
        "common": [],
        "cellular": [],
        "wifi": [],
    }
    values = signaling.get("AdditionalContactParams")
    if not isinstance(values, dict):
        return result
    for selector, raw in values.items():
        if "register" not in str(selector).casefold() or not isinstance(raw, str) or not raw:
            continue
        scope = _selector_scope(str(selector))
        for token in _split_parameters(raw):
            name, separator, value = token.partition("=")
            name = name.strip()
            if name:
                result[scope].append(
                    (name, _translate_template(value.strip()) if separator else None)
                )
    return result


def _register_headers(signaling: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    result: dict[str, list[tuple[str, str]]] = {
        "common": [],
        "cellular": [],
        "wifi": [],
    }
    values = signaling.get("AdditionalHeaders")
    if not isinstance(values, dict):
        return result
    for selector, raw in values.items():
        if "register" not in str(selector).casefold():
            continue
        items = raw if isinstance(raw, list) else [raw]
        scope = _selector_scope(str(selector))
        for item in items:
            if not isinstance(item, str) or ":" not in item:
                continue
            name, value = item.split(":", 1)
            if name.strip() and value.strip():
                result[scope].append((name.strip(), _translate_template(value.strip())))
    return result


def _status_codes(value: Any, method: str | None = None) -> list[int]:
    if not isinstance(value, str):
        return []
    selected = value
    if method and ":" in value:
        selected = ""
        for group in value.split(";"):
            name, separator, codes = group.partition(":")
            if separator and name.strip().casefold() == method.casefold():
                selected = codes
                break
    return sorted(
        {
            int(item)
            for item in re.findall(r"(?<![0-9])[3-6][0-9]{2}(?![0-9])", selected)
        }
    )


def _insert_register_rules(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    signaling: dict[str, Any],
    access_ids: dict[str, int],
) -> None:
    contacts = _register_contact_values(signaling)
    headers = _register_headers(signaling)
    expires = _as_int(signaling.get("RegistrationExpirationSeconds"))
    user_agent = signaling.get("UserAgentHeaderValue")
    user_agent = _translate_template(user_agent) if isinstance(user_agent, str) else None
    retry_after = _status_codes(signaling.get("RetryAfterStatusCodes"))
    forbidden = _status_codes(signaling.get("ForbiddenRegistrationErrorCodes"))
    retry = _status_codes(signaling.get("ReRegisterOnErrorCodes"), "REGISTER")
    has_common = bool(
        expires
        or user_agent
        or contacts["common"]
        or headers["common"]
        or retry_after
        or forbidden
        or retry
        or contacts["cellular"]
        or contacts["wifi"]
        or headers["cellular"]
        or headers["wifi"]
    )
    if not has_common:
        return
    cursor = connection.execute(
        """INSERT INTO sip_register_configs(
               profile_id, scope, requested_expires_seconds,
               contact_mode, user_agent_template
           ) VALUES (?, 'common', ?, ?, ?)""",
        (
            profile_id,
            expires if expires and expires > 0 else None,
            "custom" if any(contacts.values()) else None,
            user_agent,
        ),
    )
    common_id = int(cursor.lastrowid)

    def insert_values(register_id: int, scope: str) -> None:
        for position, (name, value) in enumerate(contacts[scope]):
            connection.execute(
                """INSERT INTO sip_contact_parameters(
                       register_config_id, position, name, value_template
                   ) VALUES (?, ?, ?, ?)""",
                (register_id, position, name, value),
            )
        for position, (name, value) in enumerate(headers[scope]):
            connection.execute(
                """INSERT INTO sip_header_rules(
                       register_config_id, phase, position, header_name,
                       action, value_template
                   ) VALUES (?, 'all', ?, ?, 'add', ?)""",
                (register_id, position, name, value),
            )

    insert_values(common_id, "common")
    for code in retry_after:
        connection.execute(
            """INSERT INTO sip_status_policies(
                   register_config_id, status_code, action
               ) VALUES (?, ?, 'honor_retry_after')""",
            (common_id, code),
        )
    for code in forbidden:
        connection.execute(
            """INSERT INTO sip_status_policies(
                   register_config_id, status_code, action
               ) VALUES (?, ?, 'stop')""",
            (common_id, code),
        )
    for code in retry:
        connection.execute(
            """INSERT OR IGNORE INTO sip_status_policies(
                   register_config_id, status_code, action
               ) VALUES (?, ?, 'retry')""",
            (common_id, code),
        )

    access_scopes: list[tuple[str, str]] = []
    if contacts["cellular"] or headers["cellular"]:
        access_scopes.extend((kind, "cellular") for kind in ("lte_epc", "nr_5gc"))
    if contacts["wifi"] or headers["wifi"]:
        access_scopes.append(("wifi_epdg", "wifi"))
    for kind, scope in access_scopes:
        access_id = access_ids.get(kind)
        if access_id is None:
            continue
        cursor = connection.execute(
            """INSERT INTO sip_register_configs(
                   profile_id, access_id, parent_register_config_id, scope,
                   contact_mode
               ) VALUES (?, ?, ?, 'access', 'custom')""",
            (profile_id, access_id, common_id),
        )
        insert_values(int(cursor.lastrowid), scope)


def _insert_ims(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    rule: IOSMatchRule,
    source_id: int,
    standard_source_id: int | None,
    source_path: str,
    config: dict[str, Any],
    access_ids: dict[str, int],
    stats: IOSImportStats,
) -> None:
    signaling = _nested(config, "IMSConfig", "Signaling")
    signaling = signaling if isinstance(signaling, dict) else {}
    has_ims = bool(
        access_ids
        or isinstance(config.get("IMSConfig"), dict)
        or config.get("SupportsImsCapability") is True
    )
    if not has_ims:
        return
    standard_domain = _standard_domain(rule.plmn)
    outgoing = signaling.get("OutgoingDomain")
    extracted_domain = (
        _translate_template(outgoing)
        if isinstance(outgoing, str) and "." in outgoing and outgoing != "1"
        else None
    )
    if extracted_domain is None and standard_source_id is None:
        return
    home_domain = extracted_domain or standard_domain
    aka = signaling.get("DefaultAuthAlgorithm")
    aka = str(aka) if aka not in (None, "") else None
    force_tcp = signaling.get("ForceTcp")
    use_ipsec = signaling.get("UseIPSec")
    connection.execute(
        """INSERT INTO ims_configs(
               profile_id, home_domain, realm, private_identity_source,
               public_identity_source, authentication_scheme, aka_algorithm,
               transport_preference, ipsec_security_agreement
           ) VALUES (?, ?, ?, 'auto', 'auto', 'ims_aka', ?, ?, ?)""",
        (
            profile_id,
            home_domain,
            standard_domain if standard_source_id is not None else None,
            aka,
            "tcp" if force_tcp is True else "auto",
            "required" if use_ipsec is True else "disabled" if use_ipsec is False else "auto",
        ),
    )
    stats.ims_configs_imported += 1
    domain_source = source_id if extracted_domain else standard_source_id
    if domain_source is not None:
        _evidence(
            connection,
            profile_id=profile_id,
            source_id=domain_source,
            table_name="ims_configs",
            row_key=profile_id,
            field_name="home_domain",
            source_path=source_path if extracted_domain else "3GPP TS 23.003",
            key_path=(
                "IMSConfig.Signaling.OutgoingDomain"
                if extracted_domain
                else "IMS home network domain derivation"
            ),
            kind="extracted" if extracted_domain else "standard_derived",
            confidence=95 if extracted_domain else 60,
        )
    if standard_source_id is not None:
        _evidence(
            connection,
            profile_id=profile_id,
            source_id=standard_source_id,
            table_name="ims_configs",
            row_key=profile_id,
            field_name="realm",
            source_path="3GPP TS 23.003",
            key_path="IMS home network domain derivation",
            kind="standard_derived",
            confidence=60,
        )
        templates = (
            ("impi", "nai", "{imsi}@{home_domain}"),
            ("impu", "sip_uri", "sip:{imsi}@{home_domain}"),
        )
        for position, (role, identity_type, template) in enumerate(templates):
            connection.execute(
                """INSERT INTO ims_identity_templates(
                       profile_id, role, position, source_policy,
                       identity_type, value_template, use_when
                   ) VALUES (?, ?, ?, 'derived_imsi', ?, ?, 'if_isim_missing')""",
                (profile_id, role, position, identity_type, template),
            )
    _insert_register_rules(
        connection,
        profile_id=profile_id,
        signaling=signaling,
        access_ids=access_ids,
    )


def _insert_capabilities(
    connection: sqlite3.Connection, profile_id: str, config: dict[str, Any]
) -> None:
    voice = _nested(config, "IMSConfig", "Voice")
    voice = voice if isinstance(voice, dict) else {}
    xcap = _nested(config, "IMSConfig", "XCAP")
    xcap = xcap if isinstance(xcap, dict) else {}
    ike = _nested(config, "TechSettings", "IKE")
    capabilities = {
        "ims": _as_bool(config.get("SupportsImsCapability")),
        "volte": _as_bool(config.get("SupportsVolteCapability")),
        "vonr": _as_bool(config.get("SupportsVoNR")),
        "vowifi": 1 if isinstance(ike, dict) and ike.get("RemoteAddress") else None,
        "emergency": _as_bool(voice.get("E911OverITechSupported")),
        "ut_xcap": _as_bool(xcap.get("supported")),
    }
    if capabilities["volte"] is None:
        capabilities["volte"] = _as_bool(voice.get("EnableVolteByDefault"))
    for service, supported in capabilities.items():
        if supported is not None:
            connection.execute(
                """INSERT INTO service_capabilities(profile_id, service, supported)
                   VALUES (?, ?, ?)""",
                (profile_id, service, supported),
            )


def _insert_entitlement_and_emergency(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    config: dict[str, Any],
    stats: IOSImportStats,
) -> None:
    entitlement = config.get("CarrierEntitlements")
    if isinstance(entitlement, dict):
        address = entitlement.get("ServerAddress")
        if isinstance(address, str) and address.startswith(("https://", "http://")):
            connection.execute(
                """INSERT INTO network_endpoints(
                       profile_id, service, address_kind, address, transport,
                       discovery_method, roaming_scope
                   ) VALUES (?, 'entitlement', 'uri', ?, ?, 'static', 'both')""",
                (
                    profile_id,
                    address,
                    "https" if address.startswith("https://") else "tcp",
                ),
            )
            stats.entitlement_endpoints_imported += 1

    voice = _nested(config, "IMSConfig", "Voice")
    voice = voice if isinstance(voice, dict) else {}
    calling = config.get("EmergencyCalling")
    calling = calling if isinstance(calling, dict) else {}
    supported = _as_bool(voice.get("E911OverITechSupported"))
    numbers = calling.get("EmergencyNumbers")
    if supported is not None or isinstance(numbers, list):
        connection.execute(
            """INSERT INTO emergency_configs(
                   profile_id, supported, emergency_numbers
               ) VALUES (?, ?, ?)""",
            (
                profile_id,
                supported,
                json.dumps(numbers, ensure_ascii=True, sort_keys=True)
                if isinstance(numbers, list)
                else None,
            ),
        )


def _insert_raw_variant(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    variant: CarrierBundleVariant,
) -> int:
    count = 0
    source_path = f"{variant.bundle_path.name}/{variant.variant_name}"
    for key, value in relevant_raw_values(variant.config).items():
        connection.execute(
            """INSERT INTO raw_config_values(
                   source_id, source_path, key_path, value_json, value_type
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                source_id,
                source_path,
                key,
                json.dumps(value, ensure_ascii=True, sort_keys=True),
                "array" if isinstance(value, list) else "object" if isinstance(value, dict) else (
                    "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "text"
                ),
            ),
        )
        count += 1
    return count


def import_ios_catalog(
    database: Path,
    extracted_root: Path,
    *,
    artifact: IPSWArtifact,
    device_class: str,
    baseband_path: Path | None = None,
    include_standard_derived: bool = True,
) -> IOSImportStats:
    """Import one device/build snapshot from exported Carrier Bundle trees."""

    carrier_root = find_bundle_root(extracted_root, "Carrier Bundles")
    if carrier_root is None:
        raise FileNotFoundError("iPhone Carrier Bundle tree was not found")
    country_root = find_bundle_root(extracted_root, "CountryBundles")
    stats = IOSImportStats()
    variants = load_carrier_bundle_variants(carrier_root, device_class)
    stats.bundles_seen = len({item.bundle_path for item in variants})
    stats.variants_seen = len(variants)

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        release = connection.execute(
            "SELECT sealed FROM catalog_release WHERE singleton = 1"
        ).fetchone()
        if release is None or release[0] != 0:
            raise RuntimeError("iOS importer requires an unsealed catalog database")

        _source_snapshot(
            connection,
            source_kind="firmware_metadata",
            platform="ios",
            artifact=artifact,
            source_revision="IPSW",
            artifact_sha256=artifact.sha256,
        )
        carrier_source_id = _source_snapshot(
            connection,
            source_kind="ios_carrier_bundle",
            platform="ios",
            artifact=artifact,
            source_revision=f"{artifact.build_id}:{device_class}",
            artifact_sha256=hash_bundle_tree(carrier_root),
        )
        if country_root is not None:
            _source_snapshot(
                connection,
                source_kind="ios_country_bundle",
                platform="ios",
                artifact=artifact,
                source_revision=artifact.build_id,
                artifact_sha256=hash_bundle_tree(country_root),
            )
        standard_source_id = (
            _standard_source(connection, artifact) if include_standard_derived else None
        )
        if baseband_path is not None and baseband_path.is_file():
            baseband_source_id = _source_snapshot(
                connection,
                source_kind="firmware_metadata",
                platform="modem",
                artifact=artifact,
                source_revision=baseband_path.name,
                artifact_sha256=sha256_file(baseband_path),
                vendor="Qualcomm",
            )
            manifest = {
                "relative_path": artifact.baseband_path or baseband_path.name,
                "size": baseband_path.stat().st_size,
                "sha256": sha256_file(baseband_path),
                "parser_status": "inventoried_not_semantically_decoded",
            }
            connection.execute(
                """INSERT INTO raw_config_values(
                       source_id, source_path, key_path, value_json, value_type
                   ) VALUES (?, ?, 'artifact', ?, 'object')""",
                (
                    baseband_source_id,
                    artifact.baseband_path or baseband_path.name,
                    json.dumps(manifest, sort_keys=True),
                ),
            )

        for variant in variants:
            stats.raw_values_imported += _insert_raw_variant(
                connection, source_id=carrier_source_id, variant=variant
            )
            carrier_id = _slug(variant.bundle_name)
            country = _country_from_bundle(variant.bundle_path.name)
            brand = variant.config.get("CarrierName")
            connection.execute(
                """INSERT OR IGNORE INTO carriers(
                       carrier_id, canonical_name, brand_name, carrier_kind,
                       country_iso2, notes
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    carrier_id,
                    variant.bundle_name,
                    brand if isinstance(brand, str) and brand else variant.bundle_name,
                    "mvno" if variant.variant_name != "base" else "mno",
                    country,
                    "Name and scope are taken from an Apple iPhone Carrier Bundle.",
                ),
            )
            for rule in variant.matches:
                profile_id = (
                    f"ios-{_slug(artifact.product_type)}-{carrier_id}-"
                    f"{_slug(variant.variant_name)}-{_profile_suffix(rule)}"
                )
                display = f"{variant.bundle_name} {rule.plmn}"
                connection.execute(
                    """INSERT INTO carrier_profiles(
                           profile_id, carrier_id, display_name, profile_kind,
                           priority, confidence, notes
                       ) VALUES (?, ?, ?, ?, ?, 90, ?)""",
                    (
                        profile_id,
                        carrier_id,
                        display,
                        "mvno" if variant.variant_name != "base" else "device_specific",
                        50 if variant.variant_name != "base" else 100,
                        f"{artifact.device_name} ({artifact.product_type}) "
                        f"{artifact.os_version} {artifact.build_id}; "
                        f"bundle {variant.bundle_version or 'unknown'}." ,
                    ),
                )
                for source_name in variant.source_paths:
                    source_path = f"{variant.bundle_path.name}/{source_name}"
                    connection.execute(
                        """INSERT INTO profile_sources(
                               profile_id, source_id, source_profile_key,
                               source_path, source_priority
                           ) VALUES (?, ?, ?, ?, 80)""",
                        (
                            profile_id,
                            carrier_source_id,
                            f"{variant.bundle_name}:{variant.variant_name}",
                            source_path,
                        ),
                    )
                source_path = f"{variant.bundle_path.name}/{variant.variant_name}"
                _insert_match(
                    connection,
                    profile_id=profile_id,
                    rule=rule,
                    country=country,
                    product_type=artifact.product_type,
                )
                stats.match_rules_imported += 1
                access_ids, _apn = _insert_access(
                    connection,
                    profile_id=profile_id,
                    source_id=carrier_source_id,
                    source_path=source_path,
                    config=variant.config,
                    stats=stats,
                )
                _insert_ike(
                    connection,
                    profile_id=profile_id,
                    source_id=carrier_source_id,
                    source_path=source_path,
                    config=variant.config,
                    access_id=access_ids.get("wifi_epdg"),
                    stats=stats,
                )
                _insert_ims(
                    connection,
                    profile_id=profile_id,
                    rule=rule,
                    source_id=carrier_source_id,
                    standard_source_id=standard_source_id,
                    source_path=source_path,
                    config=variant.config,
                    access_ids=access_ids,
                    stats=stats,
                )
                _insert_capabilities(connection, profile_id, variant.config)
                _insert_entitlement_and_emergency(
                    connection,
                    profile_id=profile_id,
                    config=variant.config,
                    stats=stats,
                )
                stats.profiles_imported += 1

        connection.commit()
        foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise RuntimeError(f"foreign key check failed: {foreign_key_error}")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after iOS import")
    return stats
