"""Shared schema-v7 configuration document helpers."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


CONFIG_CONTRACT = "carrier-bundles-ims-v1"
CONFIG_SCHEMA_PATH = Path(__file__).with_name("config.schema.json")
READINESS_VALUES = {"ready", "partial", "unsupported", "unknown"}


def compact(value: Any) -> Any:
    """Remove unknown values without collapsing explicit false/zero/list values."""

    if isinstance(value, dict):
        return {
            str(key): compact(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [compact(item) for item in value if item is not None]
    return value


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge one source layer while replacing arrays atomically."""

    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(
        compact(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _path(config: dict[str, Any], pointer: str) -> Any:
    value: Any = config
    for token in pointer.strip("/").split("/"):
        if not token:
            continue
        if not isinstance(value, dict) or token not in value:
            return None
        value = value[token]
    return value


REQUIRED_PATHS = {
    "lte": (
        "/ims/home_domain",
        "/ims/authentication/scheme",
        "/access/lte/apn",
        "/access/lte/pcscf_discovery",
    ),
    "nr": (
        "/ims/home_domain",
        "/ims/authentication/scheme",
        "/access/nr/dnn",
        "/access/nr/pcscf_discovery",
    ),
    "vowifi": (
        "/ims/home_domain",
        "/ims/authentication/scheme",
        "/access/vowifi/epdg",
        "/access/vowifi/pcscf_discovery",
        "/access/vowifi/ike/eap_method",
        "/access/vowifi/ike/identities/idi",
    ),
}


SERVICE_KEYS = {"lte": "volte", "nr": "vonr", "vowifi": "vowifi"}


def evaluate_readiness(config: dict[str, Any]) -> dict[str, str]:
    """Evaluate static coverage without predicting network registration success."""

    services = config.get("services", {})
    access = config.get("access", {})
    missing: dict[str, list[str]] = {}
    statuses: dict[str, str] = {}
    for kind, required in REQUIRED_PATHS.items():
        service = services.get(SERVICE_KEYS[kind]) if isinstance(services, dict) else None
        if service is False:
            statuses[kind] = "unsupported"
            missing[kind] = []
            continue
        if not isinstance(access, dict) or kind not in access:
            statuses[kind] = "unknown" if service is None else "partial"
            missing[kind] = list(required)
            continue
        absent = [pointer for pointer in required if _path(config, pointer) in (None, [], {})]
        statuses[kind] = "ready" if not absent else "partial"
        missing[kind] = absent
    config["readiness"] = {
        f"{kind}_missing": paths for kind, paths in missing.items() if paths
    }
    return statuses


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    if config.get("protocol_baseline") != CONFIG_CONTRACT:
        raise ValueError(f"unsupported protocol_baseline: {config.get('protocol_baseline')}")
    if not CONFIG_SCHEMA_PATH.is_file():
        raise ValueError(f"missing config schema: {CONFIG_SCHEMA_PATH}")
    try:
        schema = json.loads(CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load config schema: {error}") from error
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("config schema must use JSON Schema draft 2020-12")
    schema_contract = schema.get("properties", {}).get("protocol_baseline", {}).get("const")
    if config.get("protocol_baseline") != schema_contract:
        raise ValueError("config does not satisfy config.schema.json")
    for section in ("ims", "access", "sip", "media", "services"):
        value = config.get(section)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"config.{section} must be an object")
    encoded = canonical_json(config)
    if "device_model_pattern" in encoded or "os_build_pattern" in encoded:
        raise ValueError("runtime device/system match fields are forbidden")


def finalized_config(config: dict[str, Any]) -> tuple[str, dict[str, str]]:
    result = compact(config)
    result.setdefault("protocol_baseline", CONFIG_CONTRACT)
    statuses = evaluate_readiness(result)
    validate_config(result)
    return canonical_json(result), statuses
