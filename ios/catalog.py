"""Compile Apple Carrier Bundle facts into a schema-v7 catalog."""

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

from catalog_contract import CONFIG_CONTRACT, compact, finalized_config

from . import PARSER_NAME, PARSER_VERSION
from .bundles import (
    CarrierBundleVariant,
    IOSMatchRule,
    canonical_json,
    find_bundle_root,
    hash_bundle_tree,
    load_carrier_bundle_variants,
)
from .sources import IPSWArtifact


STANDARDS_URI = "https://www.3gpp.org/ftp/Specs/archive/"
APPLE_UPDATE_SOURCE = "https://updates.cdn-apple.com/"
APPLE_LICENSE_NOTE = (
    "Apple device software; extraction and use remain subject to the terms "
    "accompanying the official IPSW"
)
IMS_TYPE_MASK = 131072
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
    field_evidence_imported: int = 0


@dataclass(frozen=True)
class IOSBundleSource:
    source_uri: str
    artifact_sha256: str | None
    source_revision: str | None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unknown"


def _country_from_bundle(name: str) -> str | None:
    match = re.search(r"_([A-Za-z]{2})(?:\.bundle)?$", name)
    return match.group(1).upper() if match else None


def _source_artifact(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    source_uri: str,
    artifact_sha256: str | None,
    source_revision: str | None,
    parser_name: str = PARSER_NAME,
    parser_version: str = PARSER_VERSION,
    license_note: str = APPLE_LICENSE_NOTE,
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
    stats: IOSImportStats,
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


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


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


def _profile_suffix(variant: CarrierBundleVariant, rule: IOSMatchRule) -> str:
    fields = (
        variant.bundle_name,
        variant.variant_name,
        rule.plmn,
        rule.gid1 or "",
        rule.gid2 or "",
        rule.iccid_prefix or "",
    )
    readable = _slug("-".join(item for item in fields[1:] if item))[:56]
    digest = hashlib.sha256("\0".join(fields).encode()).hexdigest()[:10]
    return f"{readable}-{digest}"


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
    if value in {"pap", "chap"}:
        return value
    if apn.get("username") == "" and apn.get("password") == "":
        return "none"
    return None


def _apn_document(apn: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "apn": apn.get("apn"),
            "auth_type": _auth_type(apn),
            "username": apn.get("username") or None,
            "password": apn.get("password") or None,
            "ip_family": _ip_family(apn.get("AllowedProtocolMask")),
            "roaming_ip_family": _ip_family(apn.get("AllowedProtocolMaskInRoaming")),
            "pcscf_required": _as_bool(apn.get("PcscfAddressRequired")),
        }
    )


def _identity_type(value: Any, template: str) -> str:
    normalized = str(value or "").casefold()
    if normalized in {"idfqdn", "id_fqdn"}:
        return "id_fqdn"
    if normalized in {"iduserfqdn", "id_rfc822_addr"} or "@" in template:
        return "id_rfc822_addr"
    if normalized in {"keyid", "idkeyid", "id_key_id"}:
        return "id_key_id"
    return "id_fqdn"


def _scalar_algorithm(value: Any) -> str | list[str] | None:
    if isinstance(value, list):
        return [str(item) for item in value]
    return str(value) if value not in (None, "") else None


def _proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "encryption": _scalar_algorithm(proposal.get("EncryptionAlgorithm")),
            "integrity": _scalar_algorithm(proposal.get("IntegrityAlgorithm")),
            "prf": _scalar_algorithm(proposal.get("PRFAlgorithm")),
            "dh_group": _as_int(proposal.get("DHGroup")),
            "authentication": proposal.get("AuthenticationMethod"),
            "eap_method": proposal.get("EAPMethod"),
            "lifetime_seconds": _as_int(proposal.get("Lifetime")),
        }
    )


def _ike_document(config: dict[str, Any], include_standard_derived: bool) -> dict[str, Any]:
    ike = _nested(config, "TechSettings", "IKE")
    if not isinstance(ike, dict) or not isinstance(ike.get("RemoteAddress"), str):
        return {}
    local = ike.get("LocalIdentifier")
    local_template = _translate_template(local) if isinstance(local, str) else None
    remote = ike.get("RemoteIdentifier")
    remote_template = _translate_template(remote) if isinstance(remote, str) else None
    endpoint = _translate_template(ike["RemoteAddress"]).rstrip(".")
    try:
        parsed = ipaddress.ip_address(endpoint)
        address_kind = "ipv4" if parsed.version == 4 else "ipv6"
    except ValueError:
        address_kind = "derived_template" if "{" in endpoint else "fqdn"

    proposals = [
        _proposal(item)
        for item in ike.get("Proposals", [])
        if isinstance(item, dict)
    ]
    child = _nested(config, "TechSettings", "ChildSAs", "FirstChild")
    child_proposals = [
        _proposal(item)
        for item in (child.get("ChildProposals", []) if isinstance(child, dict) else [])
        if isinstance(item, dict)
    ]
    first = proposals[0] if proposals else {}
    eap = str(first.get("eap_method", "EAP-AKA")).casefold()
    eap_method = {
        "eap-aka": "eap_aka",
        "eap-aka'": "eap_aka_prime",
        "eap-aka-prime": "eap_aka_prime",
        "eap-tls": "certificate",
    }.get(eap, "other")
    identities: dict[str, list[dict[str, Any]]] = {}
    if local_template:
        identities["idi"] = [
            {
                "identity_type": _identity_type(
                    ike.get("LocalIdentifierType"), local_template
                ),
                "source": (
                    "derived_imsi" if "{imsi}" in local_template else "configured_template"
                ),
                "value_template": local_template,
                "required": True,
            }
        ]
    if remote_template:
        identities["idr"] = [
            {
                "identity_type": _identity_type(
                    ike.get("RemoteIdentifierType"), remote_template
                ),
                "source": "configured_template",
                "value_template": remote_template,
                "required": True,
            }
        ]
    elif include_standard_derived:
        identities["idr"] = [
            {
                "identity_type": "id_fqdn",
                "source": "epdg_fqdn",
                "value_template": "{epdg_fqdn}",
                "required": True,
            }
        ]

    validate = _as_bool(ike.get("ValidateRemoteCertificate"))
    document = compact(
        {
            "epdg": [
                {
                    "address": endpoint,
                    "address_kind": address_kind,
                    "discovery": "static",
                    "roaming_scope": "both",
                }
            ],
            "pcscf_discovery": ["ike_cfg"],
            "ike": {
                "initial_port": 500,
                "natt_port": 4500,
                "eap_method": eap_method,
                "identities": identities,
                "request_internal_address": True,
                "request_pcscf": True,
                "nat_traversal": True,
                "nat_keepalive_enabled": _as_bool(ike.get("NATTKeepAliveEnabled")),
                "nat_keepalive_seconds": _as_int(ike.get("NATTKeepAliveInterval")),
                "dpd_enabled": _as_bool(ike.get("DeadPeerDetectionEnabled")),
                "dpd_interval_seconds": _as_int(ike.get("DeadPeerDetectionInterval")),
                "dpd_retry_interval_seconds": _as_int(
                    ike.get("DeadPeerDetectionRetryInterval")
                ),
                "dpd_max_retries": _as_int(ike.get("DeadPeerDetectionMaxRetries")),
                "ike_sa_proposals": proposals,
                "child_sa_proposals": child_proposals,
                "certificate": {
                    "policy": "system_trust" if validate is True else None,
                    "validate": validate,
                    "trusted_ca": ike.get("RemoteCertificateAuthorityName"),
                    "hostname": ike.get("RemoteCertificateHostname"),
                },
            },
        }
    )
    return document


def _access_document(
    config: dict[str, Any], *, include_standard_derived: bool, stats: IOSImportStats
) -> dict[str, Any]:
    candidates = _ims_apns(config)
    stats.ambiguous_ims_apns += int(len(candidates) > 1)
    apn = candidates[0] if candidates else None
    result: dict[str, Any] = {}
    if apn is not None:
        lte = _apn_document(apn)
        if include_standard_derived:
            lte["pcscf_discovery"] = ["pco", "epco"]
        result["lte"] = lte
        stats.access_configs_imported += 1
        has_nr = apn.get("Support5GSaHandOver") is True or isinstance(
            config.get("SupportsVoNR"), bool
        )
        if has_nr:
            nr = _apn_document(apn)
            nr["dnn"] = nr.pop("apn", None)
            if include_standard_derived:
                nr["pcscf_discovery"] = ["epco", "pco"]
            result["nr"] = compact(nr)
            stats.access_configs_imported += 1
    wifi = _ike_document(config, include_standard_derived)
    if wifi:
        result["vowifi"] = wifi
        stats.access_configs_imported += 1
        stats.ike_configs_imported += 1
    return result


def _standard_domain(plmn: str) -> str:
    return f"ims.mnc{plmn[3:].zfill(3)}.mcc{plmn[:3]}.3gppnetwork.org"


def _identity_templates(config: dict[str, Any]) -> list[dict[str, Any]]:
    sim = _nested(config, "IMSConfig", "SIM")
    sim = sim if isinstance(sim, dict) else {}
    result: list[dict[str, Any]] = []
    impi = sim.get("impiFormat")
    if isinstance(impi, str):
        value = impi.replace("imsi", "{imsi}").replace(
            "carrierDomain", "{home_domain}"
        )
        result.append(
            {
                "role": "impi",
                "source": "derived_imsi",
                "identity_type": "nai",
                "value_template": value,
                "use_when": "if_isim_missing",
            }
        )
    impu_values = sim.get("impuFormat")
    if isinstance(impu_values, str):
        impu_values = [impu_values]
    if isinstance(impu_values, list):
        for value in impu_values:
            if not isinstance(value, str):
                continue
            template = value.replace("imsi", "{imsi}").replace(
                "carrierDomain", "{home_domain}"
            )
            if not template.startswith(("sip:", "tel:")):
                template = "sip:" + template
            result.append(
                {
                    "role": "impu",
                    "source": "derived_imsi",
                    "identity_type": "sip_uri",
                    "value_template": template,
                    "use_when": "if_isim_missing",
                }
            )
    return result


def _ims_document(
    config: dict[str, Any], plmn: str, include_standard_derived: bool
) -> dict[str, Any]:
    signaling = _nested(config, "IMSConfig", "Signaling")
    signaling = signaling if isinstance(signaling, dict) else {}
    sim = _nested(config, "IMSConfig", "SIM")
    sim = sim if isinstance(sim, dict) else {}
    extracted_domain = sim.get("CarrierDomain")
    if not isinstance(extracted_domain, str) or "." not in extracted_domain:
        outgoing = signaling.get("OutgoingDomain")
        extracted_domain = (
            _translate_template(outgoing)
            if isinstance(outgoing, str) and "." in outgoing and outgoing != "1"
            else None
        )
    domain = extracted_domain or (_standard_domain(plmn) if include_standard_derived else None)
    templates = _identity_templates(config)
    if not templates and include_standard_derived:
        templates = [
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
        ]
    use_ipsec = signaling.get("UseIPSec")
    return compact(
        {
            "home_domain": domain,
            "realm": _standard_domain(plmn) if include_standard_derived else domain,
            "authentication": {
                "scheme": "ims_aka",
                "algorithm": signaling.get("DefaultAuthAlgorithm"),
            },
            "transport": "tcp" if signaling.get("ForceTcp") is True else None,
            "security_agreement": (
                "required" if use_ipsec is True else "disabled" if use_ipsec is False else None
            ),
            "identity_templates": templates,
        }
    )


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
        return "vowifi"
    return "common"


def _contact_values(signaling: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "common": [],
        "cellular": [],
        "vowifi": [],
    }
    values = signaling.get("AdditionalContactParams")
    if not isinstance(values, dict):
        return result
    for selector, raw in values.items():
        if "register" not in str(selector).casefold() or not isinstance(raw, str):
            continue
        scope = _selector_scope(str(selector))
        for token in _split_parameters(raw):
            name, separator, value = token.partition("=")
            if name.strip():
                result[scope].append(
                    compact(
                        {
                            "name": name.strip(),
                            "action": "add",
                            "value_template": (
                                _translate_template(value.strip()) if separator else None
                            ),
                        }
                    )
                )
    return result


def _headers(signaling: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "common": [],
        "cellular": [],
        "vowifi": [],
    }
    values = signaling.get("AdditionalHeaders")
    if not isinstance(values, dict):
        return result
    for selector, raw in values.items():
        if "register" not in str(selector).casefold():
            continue
        for item in raw if isinstance(raw, list) else [raw]:
            if not isinstance(item, str) or ":" not in item:
                continue
            name, value = item.split(":", 1)
            if name.strip() and value.strip():
                result[_selector_scope(str(selector))].append(
                    {
                        "name": name.strip(),
                        "action": "add",
                        "value_template": _translate_template(value.strip()),
                    }
                )
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


def _sip_document(config: dict[str, Any]) -> dict[str, Any]:
    signaling = _nested(config, "IMSConfig", "Signaling")
    signaling = signaling if isinstance(signaling, dict) else {}
    contacts = _contact_values(signaling)
    headers = _headers(signaling)
    common = compact(
        {
            "register": {
                "requested_expires_seconds": _as_int(
                    signaling.get("RegistrationExpirationSeconds")
                ),
                "user_agent_template": (
                    _translate_template(signaling["UserAgentHeaderValue"])
                    if isinstance(signaling.get("UserAgentHeaderValue"), str)
                    else None
                ),
                "always_add_sip_instance": _as_bool(
                    signaling.get("AlwaysAddSipInstance")
                ),
                "country_of_origination_format": signaling.get(
                    "CountryOfOriginationFormat"
                ),
                "enable_cellular_network_info": _as_bool(
                    signaling.get("EnableCellularNetworkInfo")
                ),
                "security_agreement": (
                    "required" if signaling.get("UseIPSec") is True else
                    "disabled" if signaling.get("UseIPSec") is False else None
                ),
            },
            "headers": headers["common"],
            "contact_parameters": contacts["common"],
            "status_policy": compact(
                {
                    "honor_retry_after": _status_codes(
                        signaling.get("RetryAfterStatusCodes")
                    ),
                    "stop": _status_codes(
                        signaling.get("ForbiddenRegistrationErrorCodes")
                    ),
                    "retry": _status_codes(
                        signaling.get("ReRegisterOnErrorCodes"), "REGISTER"
                    ),
                }
            ),
            "dialogs": compact(
                {
                    "preconditions": signaling.get("Preconditions"),
                    "require_preconditions_when_mandatory": _as_bool(
                        signaling.get("RequirePreconditionsWhenMandatory")
                    ),
                    "support_p_early_media": _as_bool(
                        signaling.get("SupportPEarlyMediaHeader")
                    ),
                    "early_media_needs_header": _as_bool(
                        signaling.get("EarlyMediaNeedsHeader")
                    ),
                    "always_send_session_progress": _as_bool(
                        signaling.get("AlwaysSendSessionProgress")
                    ),
                    "ringing_timer_seconds": _as_int(
                        signaling.get("RingingTimerSeconds")
                    ),
                    "ringback_timer_seconds": _as_int(
                        signaling.get("RingbackTimerSeconds")
                    ),
                    "invite_error_responses_to_trigger_csfb": _status_codes(
                        signaling.get("InviteErrorResponsesToTriggerCSFB")
                    ),
                }
            ),
        }
    )
    result: dict[str, Any] = {"common": common} if common else {}
    if contacts["cellular"] or headers["cellular"]:
        cellular = compact(
            {
                "contact_parameters": contacts["cellular"],
                "headers": headers["cellular"],
            }
        )
        result["lte"] = cellular
        result["nr"] = cellular
    if contacts["vowifi"] or headers["vowifi"]:
        result["vowifi"] = compact(
            {
                "contact_parameters": contacts["vowifi"],
                "headers": headers["vowifi"],
            }
        )
    return result


def _codec_map(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    result: list[dict[str, Any]] = []
    for payload, settings in value.items():
        if not isinstance(settings, dict):
            continue
        result.append(
            compact(
                {
                    "name": settings.get("EncodingName"),
                    "payload_type": _as_int(payload),
                    "sample_rate": _as_int(settings.get("SampleRate")),
                    "bitrate": settings.get("br"),
                    "bandwidth": settings.get("bw"),
                    "channels": _as_int(settings.get("ch-aw-recv")),
                    "fmtp": settings.get("fmtp"),
                }
            )
        )
    return result


def _media_document(config: dict[str, Any]) -> dict[str, Any]:
    media = _nested(config, "IMSConfig", "Media")
    if not isinstance(media, dict):
        return {}
    audio_codecs = _codec_map(media.get("AudioCodecs"))
    video_codecs = _codec_map(media.get("VideoCodecs"))
    return compact(
        {
            "audio": {"codecs": audio_codecs} if audio_codecs else None,
            "video": {"codecs": video_codecs} if video_codecs else None,
            "rtp": {
                "inactivity_timer_seconds": _as_int(
                    media.get("InactivityTimerRTPSeconds")
                )
            },
            "rtcp": {
                "interval_seconds": _as_int(media.get("RTCPIntervalSeconds")),
                "extended_reports": _as_bool(media.get("EnableRTCPExtendedReports")),
            },
            "sdp": {
                "include_max_red": _as_bool(media.get("IncludeSDPMaxRed"))
            },
            "ringback_tone": media.get("RingbackTone"),
        }
    )


def _services_document(config: dict[str, Any], media: dict[str, Any]) -> dict[str, Any]:
    voice = _nested(config, "IMSConfig", "Voice")
    voice = voice if isinstance(voice, dict) else {}
    xcap = _nested(config, "IMSConfig", "XCAP")
    xcap = xcap if isinstance(xcap, dict) else {}
    ike = _nested(config, "TechSettings", "IKE")
    video = _nested(config, "IMSConfig", "Video")
    sms = _nested(config, "IMSConfig", "SMS", "SupportedDomains")
    sms = sms if isinstance(sms, dict) else {}
    audio_names = {
        str(item.get("name", "")).casefold()
        for item in _nested(media, "audio", "codecs") or []
        if isinstance(item, dict)
    }
    volte = _as_bool(config.get("SupportsVolteCapability"))
    if volte is None:
        volte = _as_bool(voice.get("EnableVolteByDefault"))
    vilte = _as_bool(config.get("SupportsVtCapability"))
    if vilte is None and isinstance(video, dict):
        vilte = True
    return compact(
        {
            "ims": _as_bool(config.get("SupportsImsCapability")),
            "volte": volte,
            "vonr": _as_bool(config.get("SupportsVoNR")),
            "vowifi": (
                True if isinstance(ike, dict) and ike.get("RemoteAddress") else None
            ),
            "vilte": vilte,
            "hd_voice": bool(audio_names & {"amr-wb", "evs"}) if audio_names else None,
            "smsoip": True if sms.get("LTE") is True or sms.get("NR") is True else None,
            "mmtel": True if volte is True else None,
            "ut_xcap": _as_bool(xcap.get("supported")),
            "emergency": _as_bool(voice.get("E911OverITechSupported")),
            "conference": isinstance(
                _nested(config, "IMSConfig", "ConferenceCalling"), dict
            ) or None,
        }
    )


def _supplementary_document(config: dict[str, Any]) -> dict[str, Any]:
    xcap = _nested(config, "IMSConfig", "XCAP")
    xcap = xcap if isinstance(xcap, dict) else {}
    conference = _nested(config, "IMSConfig", "ConferenceCalling")
    conference = conference if isinstance(conference, dict) else {}
    return compact(
        {
            "xcap": {
                "supported": _as_bool(xcap.get("supported")),
                "naf_host": xcap.get("NafHost"),
                "naf_port": _as_int(xcap.get("NafPort")),
                "bsf_host": xcap.get("BsfHost"),
                "bsf_port": _as_int(xcap.get("BsfPort")),
                "content_type": xcap.get("ContentType"),
                "ims_registration_dependency": _as_bool(
                    xcap.get("imsRegistrationDependency")
                ),
                "supports_call_waiting": _as_bool(xcap.get("SupportsCW")),
                "supports_clir": _as_bool(xcap.get("SupportsCLIR")),
                "supports_call_forwarding_erasure": _as_bool(
                    xcap.get("SupportsCFErasure")
                ),
                "forbidden_http_statuses": _status_codes(
                    xcap.get("ForbiddenHttpErrorCodes")
                ),
            },
            "conference": {
                "server": conference.get("conferenceServer"),
                "always_subscribe_events": _as_bool(
                    conference.get("AlwaysSubscribeToConferenceEvents")
                ),
            },
        }
    )


def _mobility_document(config: dict[str, Any]) -> dict[str, Any]:
    tech = config.get("TechSettings")
    tech = tech if isinstance(tech, dict) else {}
    irat = tech.get("iRatPolicies")
    return compact(
        {
            "support_call_handover": _as_bool(tech.get("SupportCallHandover")),
            "support_context_switchover": _as_bool(
                tech.get("SupportContextSwitchOver")
            ),
            "wifi_calling_allowed_in_roaming": _as_bool(
                tech.get("WifiCallingAllowedInRoaming")
            ),
            "epdg_resolution_fallback": _as_bool(
                tech.get("EPDGResolutionFallbackEnabled")
            ),
            "preferred_technology": (
                irat.get("PreferredTechnology") if isinstance(irat, dict) else None
            ),
        }
    )


def _entitlement_document(config: dict[str, Any], stats: IOSImportStats) -> dict[str, Any]:
    value = config.get("CarrierEntitlements")
    if not isinstance(value, dict):
        return {}
    endpoint = value.get("ServerAddress")
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        return {}
    stats.entitlement_endpoints_imported += 1
    authentication = value.get("Authentication")
    return compact(
        {
            "protocol": "vendor_https",
            "endpoint": endpoint,
            "protocol_version": value.get("ProtocolVersion"),
            "update_period_hours": _as_int(value.get("UpdatePeriod")),
            "authentication": authentication if isinstance(authentication, dict) else None,
        }
    )


def _emergency_document(config: dict[str, Any]) -> dict[str, Any]:
    voice = _nested(config, "IMSConfig", "Voice")
    voice = voice if isinstance(voice, dict) else {}
    calling = config.get("EmergencyCalling")
    calling = calling if isinstance(calling, dict) else {}
    return compact(
        {
            "supported_over_ims": _as_bool(voice.get("E911OverITechSupported")),
            "fallback_to_cs_without_registration": _as_bool(
                voice.get("E911OverCSIfNoIMSReg")
            ),
            "numbers": calling.get("EmergencyNumbers"),
        }
    )


def _insert_match(
    connection: sqlite3.Connection,
    *,
    stats: IOSImportStats,
    profile_id: str,
    source_id: int,
    source_path: str,
    rule: IOSMatchRule,
) -> None:
    values = compact(
        {
            "plmn": rule.plmn,
            "iccid_prefix": rule.iccid_prefix[:17] if rule.iccid_prefix else None,
            "gid1": rule.gid1,
            "gid2": rule.gid2,
        }
    )
    cursor = connection.execute(
        """INSERT INTO profile_match_rules(
               profile_id, plmn, iccid_prefix, gid1, gid2
           ) VALUES (?, ?, ?, ?, ?)""",
        (
            profile_id,
            values.get("plmn"),
            values.get("iccid_prefix"),
            values.get("gid1"),
            values.get("gid2"),
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
            source_path=source_path,
            source_key_path=f"SupportedSIMs.{key}",
            value=value,
        )
    stats.match_rules_imported += 1


def _config_document(
    variant: CarrierBundleVariant,
    rule: IOSMatchRule,
    *,
    include_standard_derived: bool,
    stats: IOSImportStats,
) -> dict[str, Any]:
    media = _media_document(variant.config)
    access = _access_document(
        variant.config,
        include_standard_derived=include_standard_derived,
        stats=stats,
    )
    ims = _ims_document(variant.config, rule.plmn, include_standard_derived)
    if ims:
        stats.ims_configs_imported += 1
    return compact(
        {
            "protocol_baseline": CONFIG_CONTRACT,
            "ims": ims,
            "access": access,
            "sip": _sip_document(variant.config),
            "media": media,
            "services": _services_document(variant.config, media),
            "supplementary_services": _supplementary_document(variant.config),
            "mobility": _mobility_document(variant.config),
            "entitlement": _entitlement_document(variant.config, stats),
            "emergency": _emergency_document(variant.config),
        }
    )


def _config_evidence(
    connection: sqlite3.Connection,
    *,
    stats: IOSImportStats,
    profile_id: str,
    carrier_source_id: int,
    standard_source_id: int | None,
    source_path: str,
    config: dict[str, Any],
) -> None:
    for section, value in config.items():
        if section in {"protocol_baseline", "readiness"} or not value:
            continue
        _evidence(
            connection,
            stats=stats,
            profile_id=profile_id,
            source_id=carrier_source_id,
            target_kind="config",
            target_path=f"/{section}",
            source_path=source_path,
            source_key_path=section,
            value=value,
            evidence_kind="extracted",
            confidence=90,
        )
    if standard_source_id is not None:
        for target_path in ("/protocol_baseline", "/ims/realm"):
            value = (
                config.get("protocol_baseline")
                if target_path == "/protocol_baseline"
                else _nested(config, "ims", "realm")
            )
            if value is None:
                continue
            _evidence(
                connection,
                stats=stats,
                profile_id=profile_id,
                source_id=standard_source_id,
                target_kind="config",
                target_path=target_path,
                source_path="3GPP specifications",
                source_key_path="standard derivation",
                value=value,
                evidence_kind="standard_derived",
                confidence=60,
            )


def import_ios_catalog(
    database: Path,
    extracted_root: Path,
    *,
    artifact: IPSWArtifact,
    device_class: str,
    baseband_path: Path | None = None,
    include_standard_derived: bool = True,
    include_device_overrides: bool = False,
    bundle_sources: dict[str, IOSBundleSource] | None = None,
) -> IOSImportStats:
    """Import one bundle tree without storing device, OS or build identifiers."""

    del baseband_path
    carrier_root = find_bundle_root(extracted_root, "Carrier Bundles")
    if carrier_root is None:
        raise FileNotFoundError("iPhone Carrier Bundle tree was not found")
    stats = IOSImportStats()
    variants = load_carrier_bundle_variants(
        carrier_root,
        device_class,
        include_device_override=include_device_overrides,
    )
    stats.bundles_seen = len({item.bundle_path for item in variants})
    stats.variants_seen = len(variants)

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        release = connection.execute(
            "SELECT sealed FROM catalog_metadata WHERE singleton = 1"
        ).fetchone()
        if release is None or release[0] != 0:
            raise RuntimeError("iOS importer requires an unsealed v7 catalog")

        default_source_id = None
        source_ids: dict[str, int] = {}
        if bundle_sources is None:
            default_source_id = _source_artifact(
                connection,
                source_kind="carrier_bundle",
                source_uri=APPLE_UPDATE_SOURCE,
                artifact_sha256=hash_bundle_tree(carrier_root),
                source_revision=None,
            )
        else:
            artifact_source_ids: dict[IOSBundleSource, int] = {}
            for bundle_name, source in sorted(bundle_sources.items()):
                source_id = artifact_source_ids.get(source)
                if source_id is None:
                    source_id = _source_artifact(
                        connection,
                        source_kind="carrier_bundle",
                        source_uri=source.source_uri,
                        artifact_sha256=source.artifact_sha256,
                        source_revision=source.source_revision,
                    )
                    artifact_source_ids[source] = source_id
                source_ids[bundle_name] = source_id
        standard_source_id = None
        if include_standard_derived:
            standard_source_id = _source_artifact(
                connection,
                source_kind="standards_reference",
                source_uri=STANDARDS_URI,
                artifact_sha256=None,
                source_revision="3GPP TS 23.003/24.229/33.203/33.402",
                parser_name="3gpp-standard-deriver",
                parser_version="1",
                license_note="3GPP specification references; no subscriber data",
            )

        for variant in variants:
            carrier_source_id = source_ids.get(variant.bundle_path.name)
            if carrier_source_id is None:
                if default_source_id is None:
                    default_source_id = _source_artifact(
                        connection,
                        source_kind="carrier_bundle",
                        source_uri=APPLE_UPDATE_SOURCE,
                        artifact_sha256=hash_bundle_tree(variant.bundle_path),
                        source_revision="IPCC bundle without manifest mapping",
                    )
                carrier_source_id = default_source_id
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
                    "Name and scope are extracted from an official Carrier Bundle.",
                ),
            )
            for rule in variant.matches:
                profile_id = f"profile-{carrier_id}-{_profile_suffix(variant, rule)}"
                config = _config_document(
                    variant,
                    rule,
                    include_standard_derived=include_standard_derived,
                    stats=stats,
                )
                raw_config, statuses = finalized_config(config)
                connection.execute(
                    """INSERT INTO carrier_profiles(
                           profile_id, carrier_id, display_name, profile_kind,
                           priority, confidence, lte_ims_status, nr_ims_status,
                           vowifi_status, config_json, notes
                       ) VALUES (?, ?, ?, ?, ?, 90, ?, ?, ?, ?, ?)""",
                    (
                        profile_id,
                        carrier_id,
                        f"{variant.bundle_name} {rule.plmn}",
                        "mvno" if variant.variant_name != "base" else "default",
                        50 if variant.variant_name != "base" else 100,
                        statuses["lte"],
                        statuses["nr"],
                        statuses["vowifi"],
                        raw_config,
                        f"Carrier Bundle profile {variant.variant_name}",
                    ),
                )
                for source_name in variant.source_paths:
                    connection.execute(
                        """INSERT INTO profile_sources(
                               profile_id, source_id, source_profile_key,
                               source_path, contribution_kind, precedence
                           ) VALUES (?, ?, ?, ?, 'carrier_policy', 200)""",
                        (
                            profile_id,
                            carrier_source_id,
                            f"{variant.bundle_name}:{variant.variant_name}",
                            f"{variant.bundle_path.name}/{source_name}",
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
                source_path = f"{variant.bundle_path.name}/{variant.variant_name}"
                _insert_match(
                    connection,
                    stats=stats,
                    profile_id=profile_id,
                    source_id=carrier_source_id,
                    source_path=source_path,
                    rule=rule,
                )
                _config_evidence(
                    connection,
                    stats=stats,
                    profile_id=profile_id,
                    carrier_source_id=carrier_source_id,
                    standard_source_id=standard_source_id,
                    source_path=source_path,
                    config=json.loads(raw_config),
                )
                stats.profiles_imported += 1

        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("foreign key check failed after iOS import")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after iOS import")
    return stats
