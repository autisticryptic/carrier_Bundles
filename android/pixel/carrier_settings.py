"""Decode and normalize Google's Pixel CarrierSettings protobuf files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .proto.carrier_list_pb2 import CarrierId, CarrierList
from .proto.carrier_settings_pb2 import (
    ApnItem,
    CarrierSettings,
    MultiCarrierSettings,
)


RELEVANT_KEY_PARTS = (
    "ims",
    "volte",
    "vonr",
    "wfc",
    "wifi_call",
    "epdg",
    "xcap",
    "emergency",
    "entitle",
    "pcscf",
    "mmtel",
)
RELEVANT_EXACT_KEYS = {
    "carrier_supports_ss_over_ut_bool",
    "carrier_vt_available_bool",
    "min_udp_port_4500_nat_timeout_sec_int",
    "require_entitlement_checks_bool",
}
RIL_NETWORK_TYPE_BY_ACCESS = {
    "lte_epc": 14,
    "nr_5gc": 20,
    "wifi_epdg": 18,
}
PROTOCOL_TO_IP_FAMILY = {
    ApnItem.IP: "ipv4",
    ApnItem.IPV6: "ipv6",
    ApnItem.IPV4V6: "ipv4v6",
}
AUTH_TYPE = {
    -1: "unspecified",
    0: "none",
    1: "pap",
    2: "chap",
    3: "pap_or_chap",
}


@dataclass(frozen=True)
class CarrierSettingRecord:
    canonical_name: str
    setting: CarrierSettings
    carrier_ids: tuple[CarrierId, ...]
    source_path: str


def load_carrier_settings(directory: Path) -> tuple[int, list[CarrierSettingRecord]]:
    carrier_list_path = directory / "carrier_list.pb"
    others_path = directory / "others.pb"
    if not carrier_list_path.is_file() or not others_path.is_file():
        raise RuntimeError(f"incomplete CarrierSettings directory: {directory}")

    carrier_list = CarrierList.FromString(carrier_list_path.read_bytes())
    settings: dict[str, CarrierSettings] = {}
    source_paths: dict[str, str] = {}
    others = MultiCarrierSettings.FromString(others_path.read_bytes())
    for setting in others.setting:
        settings[setting.canonical_name] = setting
        source_paths[setting.canonical_name] = "etc/CarrierSettings/others.pb"

    for path in sorted(directory.glob("*.pb")):
        if path.name in {"carrier_list.pb", "others.pb"}:
            continue
        setting = CarrierSettings.FromString(path.read_bytes())
        if not setting.canonical_name:
            continue
        settings[setting.canonical_name] = setting
        source_paths[setting.canonical_name] = f"etc/CarrierSettings/{path.name}"

    ids_by_name: dict[str, list[CarrierId]] = {}
    for entry in carrier_list.entry:
        ids_by_name.setdefault(entry.canonical_name, []).extend(entry.carrier_id)

    missing = sorted(set(ids_by_name) - set(settings))
    if missing:
        raise RuntimeError(
            f"CarrierSettings files are missing {len(missing)} carrier definitions; "
            f"first missing name: {missing[0]}"
        )

    records = [
        CarrierSettingRecord(
            canonical_name=name,
            setting=setting,
            carrier_ids=tuple(ids_by_name.get(name, ())),
            source_path=source_paths[name],
        )
        for name, setting in sorted(settings.items())
    ]
    return carrier_list.version, records


def is_relevant_key(key: str) -> bool:
    lowered = key.casefold()
    return lowered in RELEVANT_EXACT_KEYS or any(part in lowered for part in RELEVANT_KEY_PARTS)


def has_ims_data(record: CarrierSettingRecord) -> bool:
    if ims_apns(record.setting):
        return True
    return any(is_relevant_key(config.key) for config in record.setting.configs.config)


def config_map(setting: CarrierSettings) -> dict[str, Any]:
    return {config.key: config for config in setting.configs.config}


def config_value(config: Any) -> Any:
    value_kind = config.WhichOneof("value")
    if value_kind is None:
        return None
    value = getattr(config, value_kind)
    if value_kind in {"text_array", "int_array"}:
        return list(value.item)
    if value_kind == "bundle":
        return [
            {"key": nested.key, "value": config_value(nested)}
            for nested in value.config
        ]
    return value


def bool_config(configs: dict[str, Any], key: str) -> bool | None:
    config = configs.get(key)
    if config is None or config.WhichOneof("value") != "bool_value":
        return None
    return bool(config.bool_value)


def int_config(configs: dict[str, Any], key: str) -> int | None:
    config = configs.get(key)
    if config is None or config.WhichOneof("value") not in {"int_value", "long_value"}:
        return None
    return int(getattr(config, config.WhichOneof("value")))


def text_config(configs: dict[str, Any], key: str) -> str | None:
    config = configs.get(key)
    if config is None or config.WhichOneof("value") != "text_value":
        return None
    value = config.text_value.strip()
    return value or None


def array_config(configs: dict[str, Any], key: str) -> list[Any] | None:
    config = configs.get(key)
    if config is None or config.WhichOneof("value") not in {"text_array", "int_array"}:
        return None
    return list(getattr(config, config.WhichOneof("value")).item)


def ims_apns(setting: CarrierSettings) -> list[ApnItem]:
    return [apn for apn in setting.apns.apn if ApnItem.IMS in apn.type]


def apn_as_dict(apn: ApnItem) -> dict[str, Any]:
    value: dict[str, Any] = {
        "types": [ApnItem.ApnType.Name(item) for item in apn.type],
    }
    scalar_fields = (
        "name",
        "value",
        "bearer_bitmask",
        "server",
        "proxy",
        "port",
        "user",
        "password",
        "authtype",
        "protocol",
        "roaming_protocol",
        "mtu",
        "profile_id",
        "modem_cognitive",
        "user_visible",
        "user_editable",
        "apn_set_id",
        "skip_464xlat",
    )
    for field in scalar_fields:
        if not apn.HasField(field):
            continue
        raw = getattr(apn, field)
        descriptor = apn.DESCRIPTOR.fields_by_name[field]
        value[field] = descriptor.enum_type.values_by_number[raw].name if descriptor.enum_type else raw
    return value


def _network_types(apn: ApnItem) -> set[int] | None:
    mask = apn.bearer_bitmask.strip()
    if not mask or mask == "0":
        return None
    try:
        return {int(item) for item in mask.split("|")}
    except ValueError:
        return set()


def apn_applies_to(apn: ApnItem, access_kind: str) -> bool:
    network_types = _network_types(apn)
    return network_types is None or RIL_NETWORK_TYPE_BY_ACCESS[access_kind] in network_types


def normalized_apn(apn: ApnItem) -> dict[str, Any]:
    protocol = PROTOCOL_TO_IP_FAMILY.get(apn.protocol)
    roaming_protocol = PROTOCOL_TO_IP_FAMILY.get(apn.roaming_protocol)
    return {
        "apn_dnn": apn.value or None,
        "apn_auth_type": AUTH_TYPE.get(apn.authtype, "unspecified"),
        "apn_username": apn.user if apn.HasField("user") else None,
        "apn_password": apn.password if apn.HasField("password") else None,
        "ip_family": protocol,
        "roaming_ip_family": roaming_protocol,
        "mtu": apn.mtu if apn.HasField("mtu") and apn.mtu > 0 else None,
    }


def select_access_apn(setting: CarrierSettings, access_kind: str) -> tuple[ApnItem | None, bool]:
    candidates = [apn for apn in ims_apns(setting) if apn_applies_to(apn, access_kind)]
    if not candidates:
        return None, False
    distinct: dict[tuple[tuple[str, Any], ...], ApnItem] = {}
    for candidate in candidates:
        normalized = normalized_apn(candidate)
        distinct.setdefault(tuple(sorted(normalized.items())), candidate)
    if len(distinct) != 1:
        return None, True
    return next(iter(distinct.values())), False


def public_imsi_prefix(pattern: str) -> str | None:
    if not pattern:
        return None
    match = re.fullmatch(r"([0-9]+)[xX]*", pattern)
    return match.group(1) if match else None


def translate_android_user_agent(template: str | None) -> str | None:
    if not template:
        return None
    replacements = {
        "#MANUFACTURE#": "{manufacturer}",
        "#MODEL#": "{device_model}",
        "#BUILD#": "{os_build}",
        "#AV#": "{android_version}",
    }
    result = template
    for source, target in replacements.items():
        result = result.replace(source, target)
    return None if re.search(r"#[A-Z_]+#", result) else result
