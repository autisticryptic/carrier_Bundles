"""Validate offline evidence identity. Simulation success is NOT a deletion permit."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

EXPECTED = {
 'lte_required_sec_agree_first_request':True,'lte_required_does_not_override_disabled':False,
 'lte_aka_baseline':True,'wifi_aka_baseline':True,'wifi_421_cumulative':True,'wifi_494_cumulative':True,
 'wifi_without_fallback_counterexample':False,'lte_proxy_407':True,'lte_akav2_md5':True,
 'wifi_akav2_sha256':True,'lte_423_before_aka':True,'wifi_423_after_aka':True,
 'lte_interleaved_frames':True,'lte_udp_retransmission':True,'lte_auth_round_bound':False,
 'wifi_terminal_403':False,'lte_custom_domain_counterexample':False,'lte_wrong_digest_counterexample':False,
 'wifi_malformed_nonce':False,'lte_plain_md5_not_authorized':False,'lte_explicit_omit_sms_only':True,
}

def validate_evidence(path: Path, source_root: Path | None = None) -> dict:
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('suite_id')!='simadmin-offline-derived-registration-v1' or data.get('report_format')!=1:
        raise ValueError('unsupported simulation evidence format')
    if data.get('evidence_kind')!='offline_simulation' or data.get('passed') is not True or data.get('hardware_used') is not False or data.get('live_network_verified') is not False:
        raise ValueError('simulation flags must explicitly exclude hardware/live verification')
    cases=data.get('scenarios',[])
    if len(cases)!=len(EXPECTED) or {c.get('id') for c in cases}!=set(EXPECTED):raise ValueError('simulation matrix is incomplete')
    for case in cases:
        if case.get('passed') is not True or case.get('expected_success') is not EXPECTED[case['id']] or case.get('observed_success') is not EXPECTED[case['id']]:
            raise ValueError('unexpected simulation result: '+case['id'])
    files=data.get('source_files_sha256',{})
    for required in ('backend/src/connectivity/core/register.rs','backend/src/connectivity/core/digest_aka.rs',
                     'backend/src/connectivity/modems/ims/vowifi/profiles.rs',
                     'backend/src/connectivity/modems/ims/vowifi/carrier_catalog_v7.rs'):
        if required not in files:raise ValueError('simulation missing consumer source identity')
    harness=data.get('harness_files_sha256')
    if harness is not None:
        expected_assets={'simulator.rs','cellular_adapter.rs','wifi_adapter.rs'}
        if not isinstance(harness,dict) or set(harness)!=expected_assets:
            raise ValueError('incomplete simulation harness identity')
        assets=Path(__file__).resolve().parents[1]/'simulations/ims_registration'
        for name,digest in harness.items():
            if hashlib.sha256((assets/name).read_bytes()).hexdigest()!=digest:
                raise ValueError('simulation harness changed: '+name)
    elif 'offline-registration-sim/simulator.rs' not in files:
        raise ValueError('missing simulation harness identity')
    for name,digest in files.items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or (relative.parts[0] not in ('backend','offline-registration-sim') and name != 'VERSION'):
            raise ValueError('unsafe source path in simulation evidence')
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('invalid source digest')
        if source_root is not None:
            source=(source_root/name).resolve()
            if not source.is_relative_to(source_root.resolve()):raise ValueError('source link escapes checkout')
            if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise ValueError('consumer source changed since simulation: '+name)
    source_hash=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if source_hash!=data.get('source_tree_sha256'):raise ValueError('simulation source manifest checksum mismatch')
    return {'suite_id':data['suite_id'],'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'source_tree_sha256':source_hash,'scenarios':len(cases),
            'simulated_registrations':sum(EXPECTED.values()),'expected_rejections':len(cases)-sum(EXPECTED.values()),
            'source_checkout_verified':source_root is not None,'live_network_verified':False}
