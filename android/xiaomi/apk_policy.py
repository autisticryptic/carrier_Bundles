"""Compile a conservative PLMN projection of the observed Xiaomi CarrierConfig chain.

No carrier-ID-to-PLMN guesses and no flattening of SIM/device predicates. A key
whose final value depends on an unsupported predicate is withheld, until a later
unconditional assignment proves its value. Only referenced default assets load.
This describes static firmware policy, not subscriber provisioning or registration.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .apk_resources import ApkXmlDocument, ApkXmlWarning, MAX_APK_BYTES, MAX_XML_BYTES, decode_xml, iter_apk_xml

ANDROID = '{http://schemas.android.com/apk/res/android}'
KNOWN_APKS = {
    'CarrierConfig.apk': 'com.android.carrierconfig',
    'CarrierConfigResCommon_Sys.apk': 'com.android.carrierconfig.overlay.common',
    'MiuiCarrierConfigOverlay.apk': 'com.android.carrierconfig.overlay.miui',
}
PLMN_FILE = re.compile(r'assets/carrier_config_(?:mccmnc_)?([0-9]{5,6})\.xml$')

@dataclass
class PolicyBlock:
    attributes: dict[str, str]
    values: dict[str, Any]
    path: Path
    member: str
    index: int
    apk_sha256: str
    member_sha256: str
    ambiguous_network: bool = False

    def evidence(self, key: str) -> dict[str, Any]:
        return {'path': str(self.path), 'member': self.member, 'block': self.index,
                'selectors': self.attributes, 'key': key,
                'apk_sha256': self.apk_sha256, 'member_sha256': self.member_sha256}

@dataclass
class CompiledPolicy:
    plmn: str
    values: dict[str, Any] = field(default_factory=dict)
    origins: dict[str, dict[str, Any]] = field(default_factory=dict)
    uncertain: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def apply(self, block: PolicyBlock) -> None:
        status = scope_match(block, self.plmn)
        if status is False:
            return
        for key, value in block.values.items():
            if status is None:
                if key not in self.values or self.values[key] != value or key in self.uncertain:
                    self.uncertain.setdefault(key, []).append(block.evidence(key))
                continue
            self.values[key] = value
            self.origins[key] = block.evidence(key)
            self.uncertain.pop(key, None)


def scope_match(block: PolicyBlock, plmn: str) -> bool | None:
    attrs = block.attributes
    uncertain = False
    for key, actual in (('mcc', plmn[:3]), ('mnc', plmn[3:]), ('mccmnc', plmn), ('plmn', plmn)):
        if key not in attrs:
            continue
        tokens = [x.strip() for x in attrs[key].split(',')]
        # MiuiCarrierConfigStub normalizes singleton MCC/MNC numerically,
        # but comma-list matching uses trimmed string equality. Preserve the
        # catalog PLMN spelling; this is source predicate evaluation only.
        if key in ('mcc', 'mnc') and len(tokens) == 1 and tokens[0].isdigit():
            matches = int(tokens[0]) == int(actual)
        else:
            matches = actual in tokens
        if not matches:
            return False
    if set(attrs) - {'mcc', 'mnc', 'mccmnc', 'plmn'}:
        uncertain = True
    return None if uncertain else True


def _values(element: ET.Element) -> dict[str, Any]:
    # Import at call time to avoid a circular module dependency.
    from .carrier_config import _parse_value, _tag
    values: dict[str, Any] = {}
    for child in element:
        name = child.get('name') or child.get('key')
        if not name:
            continue
        if _tag(child) in ('map', 'pbundle_as_map', 'bundle'):
            values[name] = _values(child)
        else:
            value = _parse_value(child)
            if value is not None:
                values[name] = value
    return values


def document_blocks(doc: ApkXmlDocument, forced_plmn: str | None = None) -> list[PolicyBlock]:
    root = doc.root
    if root.tag not in ('carrier_config_list', 'carrier_config'):
        raise ValueError(f'unsupported carrier XML root: {doc.member}')
    nodes = [root] if root.tag == 'carrier_config' else list(root)
    blocks = []
    for index, node in enumerate(nodes):
        if node.tag != 'carrier_config':
            raise ValueError(f'unsupported carrier XML child: {doc.member}/{node.tag}')
        attrs = dict(node.attrib)
        if forced_plmn is not None:
            if any(k in attrs for k in ('plmn', 'mccmnc', 'mcc', 'mnc')):
                # Preserve the intersection; a conflicting explicit scope must
                # not be overwritten by the filename.
                attrs['plmn'] = forced_plmn
            else:
                attrs['plmn'] = forced_plmn
        types = doc.attribute_types.get(node, {})
        ambiguous = 'mnc' in types and types['mnc'].raw_value is None and types['mnc'].value_type in (0x10, 0x11)
        blocks.append(PolicyBlock(attrs, _values(node), doc.apk_path, doc.member, index,
                                  doc.apk_sha256, doc.member_sha256, ambiguous))
    return blocks


def _explicit_networks(blocks: list[PolicyBlock]) -> set[str]:
    result: set[str] = set()
    for b in blocks:
        a = b.attributes
        for name in ('plmn', 'mccmnc'):
            for p in a.get(name, '').split(','):
                if re.fullmatch('[0-9]{5,6}', p): result.add(p)
        if b.ambiguous_network:
            continue
        for mcc in a.get('mcc', '').split(','):
            for mnc in a.get('mnc', '').split(','):
                if re.fullmatch('[0-9]{3}', mcc) and re.fullmatch('[0-9]{2,3}', mnc): result.add(mcc + mnc)
    return {p for p in result if not p.startswith(('000', '999'))}


def compile_documents(
    base: dict[str, ApkXmlDocument], common: dict[str, ApkXmlDocument],
    device: dict[str, ApkXmlDocument], known_plmns: set[str],
    loose_miui: list[ApkXmlDocument] | None = None,
    loose_device: list[ApkXmlDocument] | None = None,
) -> tuple[list[CompiledPolicy], dict[str, Any]]:
    """Static fallback chain verified against this ROM's CarrierConfig DEX.

    carrier-ID assets remain unselected: there is no trustworthy ID→PLMN map.
    Later MIUI defaults can prove particular keys independent of those assets;
    unproved earlier carrier-ID keys stay unknown, not silently defaulted.
    """
    unbound_keys: set[str] = set()
    plmn_blocks: list[PolicyBlock] = []
    ignored_assets = []
    for name, doc in base.items():
        matched = PLMN_FILE.fullmatch(name)
        if matched:
            plmn_blocks.extend(document_blocks(doc, matched.group(1)))
        elif name.startswith('assets/carrier_config_carrierid_'):
            ignored_assets.append(name)
            for b in document_blocks(doc): unbound_keys.update(b.values)

    def resource(name: str, overlay: dict[str, ApkXmlDocument]) -> list[PolicyBlock]:
        doc = overlay.get(name) or base.get(name)
        if doc is None: raise ValueError('missing CarrierConfig resource: ' + name)
        return document_blocks(doc)

    vendor = resource('res/xml/vendor.xml', common)
    miui = resource('res/xml/vendor_miui.xml', {})
    device_blocks = resource('res/xml/vendor_device.xml', device)
    # Includes accumulate in mDefaultPb. The actual helper flushes that
    # bundle before each loose-file result and before the device resource
    # result; vendor and vendor_miui resource reads do not flush it themselves.
    # In particular vendor_miui references 2g_and_3g_sunset.xml in this ROM.
    stages = [(vendor, False), (miui, False)]
    stages.extend((document_blocks(doc), True) for doc in loose_miui or [])
    stages.append((device_blocks, True))
    stages.extend((document_blocks(doc), True) for doc in loose_device or [])
    pending: list[PolicyBlock] = []
    included: list[str] = []
    chain: list[PolicyBlock] = []
    for blocks, flush in stages:
        explicit: list[PolicyBlock] = []
        for block in blocks:
            if 'include' not in block.attributes:
                explicit.append(block); continue
            if set(block.attributes) != {'include'} or block.values:
                raise ValueError('conditional/include-with-values needs explicit modelling')
            name = block.attributes['include']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+\.xml', name) or '..' in name:
                raise ValueError('unsafe included asset path')
            member = 'assets/' + name
            doc = device.get(member) or common.get(member) or base.get(member)
            if doc is None: raise ValueError('referenced CarrierConfig asset missing: ' + member)
            child_blocks = document_blocks(doc)
            if any('include' in b.attributes for b in child_blocks):
                raise ValueError('nested CarrierConfig include unsupported')
            pending.extend(child_blocks); included.append(member)
        if flush:
            chain.extend(pending); pending.clear()
        chain.extend(explicit)
    if pending: raise ValueError('unflushed CarrierConfig include state')
    candidates = set(known_plmns) | _explicit_networks(plmn_blocks + chain)
    policies = []
    for plmn in sorted(candidates):
        if not re.fullmatch('[0-9]{5,6}', plmn) or plmn.startswith(('000', '999')): continue
        p = CompiledPolicy(plmn)
        # Unknown carrier-ID selection cannot introduce arbitrary static ePDG,
        # IKE or service values. Definite later assignments can supersede it.
        for block in plmn_blocks: p.apply(block)
        p.uncertain.update({k: [{'reason': 'unresolved_carrier_id_asset'}] for k in unbound_keys})
        for block in chain: p.apply(block)
        for key in p.uncertain:
            p.values.pop(key, None); p.origins.pop(key, None)
        policies.append(p)
    return policies, {
        'selected_default_assets': included,
        'carrier_id_assets_not_mapped': len(ignored_assets),
        'projection': 'PLMN-invariant keys only; unresolved selector-dependent values withheld',
        'source_documents': len(base) + len(common) + len(device),
        'policies': len(policies),
    }


def load_apk_policies(paths: tuple[Path, ...], known_plmns: set[str], root_dir: Path):
    selected: dict[str, Path] = {}
    for path in paths:
        if path.suffix.lower() != '.apk': continue
        if path.name not in KNOWN_APKS: raise ValueError('unmodelled carrier overlay: ' + path.name)
        if path.name in selected: raise ValueError('ambiguous duplicate carrier APK: ' + path.name)
        selected[path.name] = path
    if not selected: return [], {}
    if 'CarrierConfig.apk' not in selected: raise ValueError('carrier overlay without base CarrierConfig APK')
    packages = {}
    omitted_no_sim = []
    for name, path in selected.items():
        # Bound manifest decompression BEFORE allocating its contents. The
        # document reader below validates every archive path/duplicate too.
        if path.stat().st_size > MAX_APK_BYTES:
            raise ValueError('APK size limit exceeded')
        with zipfile.ZipFile(path) as archive:
            manifests = [entry for entry in archive.infolist() if entry.filename == 'AndroidManifest.xml']
            if len(manifests) != 1 or manifests[0].file_size > MAX_XML_BYTES:
                raise ValueError('missing, duplicate or oversized APK manifest')
            with archive.open(manifests[0]) as stream:
                payload = stream.read(MAX_XML_BYTES + 1)
            if len(payload) > MAX_XML_BYTES or len(payload) != manifests[0].file_size:
                raise ValueError('APK manifest size mismatch')
            manifest = decode_xml(payload)
        package = manifest.get('package')
        if package != KNOWN_APKS[name]:
            raise ValueError('unexpected carrier package identity: ' + name)
        if name != 'CarrierConfig.apk':
            overlay = manifest.find('overlay')
            if overlay is None or overlay.get(ANDROID + 'targetPackage') != 'com.android.carrierconfig' or overlay.get(ANDROID + 'isStatic') != 'true':
                raise ValueError('unverified static CarrierConfig overlay')
            if any(k in overlay.attrib for k in (ANDROID+'requiredSystemPropertyName', ANDROID+'requiredSystemPropertyValue')):
                raise ValueError('conditional CarrierConfig overlay unsupported')
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter('always', ApkXmlWarning)
            documents = iter_apk_xml(path)
        for notice in notices:
            # The real APK ships a malformed no-SIM placeholder. It is never
            # selected for a SIM-bearing PLMN. Any other omitted source could
            # hide an override and must reject compilation, not broaden policy.
            message = str(notice.message)
            if '!assets/carrier_config_no_sim.xml: invalid plaintext XML: no element found' not in message:
                raise ValueError('carrier policy XML could not be decoded: ' + message)
            omitted_no_sim.append(path.relative_to(root_dir).as_posix() + '!assets/carrier_config_no_sim.xml')
        if any(d.member.startswith('res/xml-') for d in documents):
            raise ValueError('qualified CarrierConfig resources require configuration selection')
        allowed_resource = {'CarrierConfigResCommon_Sys.apk': 'res/xml/vendor.xml',
                            'MiuiCarrierConfigOverlay.apk': 'res/xml/vendor_device.xml'}.get(name)
        if allowed_resource and any(d.member.startswith('res/') and d.member != allowed_resource for d in documents):
            raise ValueError('overlay changes an unmodelled CarrierConfig resource')
        packages[name] = {d.member: d for d in documents}
    loose_miui, loose_device = [], []
    for path in paths:
        if path.name not in ('vendor_miui.xml','vendor_device.xml'): continue
        relative = path.relative_to(root_dir).as_posix()
        if '/mi_ext/' not in '/' + relative: raise ValueError('unmodelled loose carrier override: ' + relative)
        data = path.read_bytes()
        doc = ApkXmlDocument(relative, decode_xml(data), path, hashlib.sha256(data).hexdigest(), hashlib.sha256(data).hexdigest())
        (loose_miui if path.name == 'vendor_miui.xml' else loose_device).append(doc)
    policies, summary = compile_documents(packages['CarrierConfig.apk'], packages.get('CarrierConfigResCommon_Sys.apk', {}),
        packages.get('MiuiCarrierConfigOverlay.apk', {}), known_plmns, loose_miui, loose_device)
    summary['omitted_no_sim_placeholders'] = omitted_no_sim
    summary['runtime_override_boundary'] = 'static ROM fallback; post-install opconfig/APEX updates are not modelled'
    return policies, summary
