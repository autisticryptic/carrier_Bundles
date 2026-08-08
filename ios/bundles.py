"""Parse iPhone Carrier Bundles without expanding subscriber identities."""

from __future__ import annotations

import copy
import hashlib
import json
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_SIM = re.compile(
    r"^(?P<plmn>[0-9]{5,6})"
    r"(?:(?:_|-)GID1(?:-|_)(?P<gid1>[^_-]+))?"
    r"(?:(?:_|-)GID2(?:-|_)(?P<gid2>[^_-]+))?"
    r"(?:(?:_|-)ID(?:-|_)(?P<iccid>[0-9]+))?$",
    re.IGNORECASE,
)
RELEVANT_ROOT_KEYS = (
    "AttachAPN",
    "CarrierEntitlements",
    "EmergencyCalling",
    "IMSConfig",
    "NRSlicing",
    "SupportedSIMs",
    "SupportsImsCapability",
    "SupportsNRNSA",
    "SupportsVoNR",
    "SupportsVolteCapability",
    "TechSettings",
    "apns",
)


@dataclass(frozen=True)
class IOSMatchRule:
    source_value: str
    plmn: str
    gid1: str | None = None
    gid2: str | None = None
    iccid_prefix: str | None = None
    source_kind: str = "supported_sims"


@dataclass(frozen=True)
class CarrierBundleVariant:
    bundle_path: Path
    bundle_name: str
    bundle_identifier: str | None
    bundle_version: str | None
    variant_name: str
    config: dict[str, Any]
    matches: tuple[IOSMatchRule, ...]
    source_paths: tuple[str, ...]

    @property
    def stable_key(self) -> str:
        value = f"{self.bundle_name}\0{self.variant_name}"
        return hashlib.sha256(value.encode()).hexdigest()[:12]


def load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        value = plistlib.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected a plist dictionary: {path}")
    return value


def find_bundle_root(root: Path, tree_name: str) -> Path | None:
    """Resolve an exported Apple tree or a direct bundle collection."""

    candidates = (
        root / "System" / "Library" / tree_name / "iPhone",
        root / tree_name,
        root,
    )
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.bundle")):
            return candidate
    return None


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply Apple's plist overlay shape, including local ``_Exclude`` keys."""

    result = copy.deepcopy(base)
    excluded = overlay.get("_Exclude", ())
    if isinstance(excluded, str):
        excluded = (excluded,)
    if isinstance(excluded, list | tuple):
        for key in excluded:
            if isinstance(key, str):
                result.pop(key, None)
    for key, value in overlay.items():
        if key == "_Exclude":
            continue
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def parse_supported_sim(
    value: str, *, source_kind: str = "supported_sims"
) -> IOSMatchRule | None:
    match = SUPPORTED_SIM.fullmatch(value.strip())
    if match is None:
        return None
    plmn = match.group("plmn")
    if plmn.startswith("000"):
        return None
    return IOSMatchRule(
        source_value=value,
        plmn=plmn,
        gid1=match.group("gid1"),
        gid2=match.group("gid2"),
        iccid_prefix=match.group("iccid"),
        source_kind=source_kind,
    )


def _matches(config: dict[str, Any]) -> tuple[IOSMatchRule, ...]:
    values = config.get("SupportedSIMs", ())
    if not isinstance(values, list):
        return ()
    result: list[IOSMatchRule] = []
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        parsed = parse_supported_sim(value)
        if parsed is None:
            continue
        key = (parsed.plmn, parsed.gid1, parsed.gid2, parsed.iccid_prefix)
        if key not in seen:
            seen.add(key)
            result.append(parsed)
    return tuple(result)


def _device_override(bundle: Path, device_class: str) -> Path | None:
    wanted = device_class.casefold()
    matches: list[Path] = []
    for path in bundle.glob("overrides_*.plist"):
        tokens = path.stem.removeprefix("overrides_").split("_")
        if wanted in (token.casefold() for token in tokens):
            matches.append(path)
    if len(matches) > 1:
        raise ValueError(
            f"multiple {device_class} overrides in {bundle.name}: "
            + ", ".join(path.name for path in matches)
        )
    return matches[0] if matches else None


def _symlink_matches(bundle_root: Path) -> dict[str, list[IOSMatchRule]]:
    result: dict[str, list[IOSMatchRule]] = {}
    for link in bundle_root.iterdir():
        if not link.is_symlink():
            continue
        parsed = parse_supported_sim(link.name, source_kind="symlink")
        if parsed is None:
            continue
        target_name = link.resolve(strict=False).name
        if not target_name.endswith(".bundle"):
            continue
        result.setdefault(target_name, []).append(parsed)
    return result


def load_carrier_bundle_variants(
    bundle_root: Path,
    device_class: str,
    *,
    include_device_override: bool = False,
) -> list[CarrierBundleVariant]:
    """Load effective base and MVNO configurations for one device class."""

    result: list[CarrierBundleVariant] = []
    symlink_matches = _symlink_matches(bundle_root)
    for bundle in sorted(bundle_root.glob("*.bundle"), key=lambda path: path.name.casefold()):
        carrier_path = bundle / "carrier.plist"
        info_path = bundle / "Info.plist"
        if not carrier_path.is_file() or not info_path.is_file():
            continue
        carrier = load_plist(carrier_path)
        info = load_plist(info_path)
        override_path = (
            _device_override(bundle, device_class) if include_device_override else None
        )
        effective = carrier
        source_paths = [carrier_path.name]
        if override_path is not None:
            effective = deep_merge(effective, load_plist(override_path))
            source_paths.append(override_path.name)

        common = copy.deepcopy(effective)
        mvno_overrides = common.pop("MVNOOverrides", {})
        base_matches = list(_matches(common))
        mvno_match_keys: set[tuple[str, str | None, str | None, str | None]] = set()
        if isinstance(mvno_overrides, dict):
            for value in mvno_overrides.values():
                if not isinstance(value, dict):
                    continue
                for rule in _matches(value):
                    mvno_match_keys.add(
                        (rule.plmn, rule.gid1, rule.gid2, rule.iccid_prefix)
                    )
        # An exact MVNO rule replaces the base configuration for that match.
        # Less-specific base rules remain as fallbacks and are ordered by the
        # profile priority written by the catalog importer.
        base_matches = [
            rule
            for rule in base_matches
            if (rule.plmn, rule.gid1, rule.gid2, rule.iccid_prefix)
            not in mvno_match_keys
        ]
        base_match_keys = {
            (rule.plmn, rule.gid1, rule.gid2, rule.iccid_prefix)
            for rule in base_matches
        }
        for rule in symlink_matches.get(bundle.name, ()):
            key = (rule.plmn, rule.gid1, rule.gid2, rule.iccid_prefix)
            if key not in base_match_keys and key not in mvno_match_keys:
                base_matches.append(rule)
                base_match_keys.add(key)
        if base_matches:
            result.append(
                CarrierBundleVariant(
                    bundle_path=bundle,
                    bundle_name=str(info.get("CFBundleName") or bundle.stem),
                    bundle_identifier=info.get("CFBundleIdentifier"),
                    bundle_version=str(
                        info.get("CFBundleShortVersionString")
                        or info.get("CFBundleVersion")
                        or ""
                    )
                    or None,
                    variant_name="base",
                    config=common,
                    matches=tuple(base_matches),
                    source_paths=tuple(source_paths),
                )
            )

        if not isinstance(mvno_overrides, dict):
            continue
        for variant_name, value in sorted(mvno_overrides.items()):
            if not isinstance(value, dict):
                continue
            variant_matches = _matches(value)
            override_config = value.get("OverrideConfiguration", {})
            if not variant_matches or not isinstance(override_config, dict):
                continue
            variant = deep_merge(common, override_config)
            variant["SupportedSIMs"] = [item.source_value for item in variant_matches]
            result.append(
                CarrierBundleVariant(
                    bundle_path=bundle,
                    bundle_name=str(info.get("CFBundleName") or bundle.stem),
                    bundle_identifier=info.get("CFBundleIdentifier"),
                    bundle_version=str(
                        info.get("CFBundleShortVersionString")
                        or info.get("CFBundleVersion")
                        or ""
                    )
                    or None,
                    variant_name=str(variant_name),
                    config=variant,
                    matches=variant_matches,
                    source_paths=tuple(source_paths),
                )
            )
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
            if str(key).casefold() not in {"signature", "signature2"}
        }
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, bytes):
        return {"byte_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    return value


def relevant_raw_values(config: dict[str, Any]) -> dict[str, Any]:
    """Return only public static IMS/access roots, with signatures reduced to hashes."""

    return {
        key: _json_value(config[key])
        for key in RELEVANT_ROOT_KEYS
        if key in config
    }


def hash_bundle_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            target = path.readlink().as_posix().encode()
            digest.update(b"L")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(target).to_bytes(4, "big"))
            digest.update(target)
            continue
        if not path.is_file():
            continue
        digest.update(b"F")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        file_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(file_digest)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
