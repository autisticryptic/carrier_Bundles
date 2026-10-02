"""Directly remove covered access configurations; keep schema v7 / contract v1.

This classifier describes the requirements of the offline standard-core model,
not a live carrier guarantee. Unknown/stricter security, identity, media, service
or entitlement requirements are retained. NR is not deleted: no NR/5GS runtime
registration was exercised. No inheritance marker or materializer is emitted.
"""
import copy
from collections import Counter
import hashlib
import json
import re

from catalog_contract import evaluate_readiness, validate_config

POLICY_ID='simadmin-simulated-standard-pruning-v1'
IDENTITIES=[
 {'identity_type':'nai','role':'impi','source':'derived_imsi','use_when':'if_isim_missing','value_template':'{imsi}@{home_domain}'},
 {'identity_type':'sip_uri','role':'impu','source':'derived_imsi','use_when':'if_isim_missing','value_template':'sip:{imsi}@{home_domain}'},
]
FAMILIES={'ipv4v6','ipv4','ipv6'}

def encoded(value):return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'))
def empty(value):
    # False, zero and null are NOT absence. Only empty policy containers qualify.
    return isinstance(value,dict) and all(empty(v) for v in value.values()) or isinstance(value,list) and not value

def decision(config, access, plmn, status):
    def reject(reason):return {'covered':False,'reason':reason}
    if not re.fullmatch(r'[0-9]{5,6}',plmn or '') or plmn.startswith('999'):return reject('unverified_or_private_plmn')
    if status!='ready':return reject('not_ready_in_source')
    if config.get('protocol_baseline')!='carrier-bundles-ims-v1':return reject('unknown_contract')
    if set(config)-{'protocol_baseline','ims','access','sip','services','readiness','media','mobility','entitlement','emergency','supplementary_services'}:
        return reject('unknown_top_level_policy')
    for section in ('media','mobility','entitlement','emergency','supplementary_services'):
        if section in config and not empty(config[section]):return reject('unmodelled_'+section)
    domain=f'ims.mnc{plmn[3:].zfill(3)}.mcc{plmn[:3]}.3gppnetwork.org'
    epdg=f'epdg.epc.mnc{plmn[3:].zfill(3)}.mcc{plmn[:3]}.pub.3gppnetwork.org'
    def expand(value):
        return value.replace('{mcc}',plmn[:3]).replace('{mnc3}',plmn[3:].zfill(3)).replace('{mnc}',plmn[3:]).replace('{home_domain}',domain) if isinstance(value,str) else value
    ims=config.get('ims',{})
    if not isinstance(ims,dict) or set(ims)-{'home_domain','realm','authentication','identity_templates','transport','security_agreement'}:
        return reject('unmodelled_ims_policy')
    if expand(ims.get('home_domain'))!=domain or expand(ims.get('realm',domain))!=domain:return reject('custom_ims_domain_or_realm')
    if ims.get('transport','udp') not in ('udp','auto'):return reject('explicit_transport')
    if ims.get('security_agreement','auto')!='auto':return reject('explicit_security_policy')
    auth=ims.get('authentication',{})
    if not isinstance(auth,dict) or set(auth)-{'scheme','algorithm'} or auth.get('scheme')!='ims_aka' or auth.get('algorithm','AKAv1-MD5') not in ('AKAv1-MD5','AKAv2-MD5'):
        return reject('unsupported_authentication_policy')
    if 'identity_templates' in ims and encoded(ims['identity_templates'])!=encoded(IDENTITIES):return reject('custom_identity_policy')
    services=config.get('services',{})
    if not isinstance(services,dict):return reject('invalid_service_policy')
    for name,value in services.items():
        if name in ('ims','mmtel','smsoip') or name==('vowifi' if access=='vowifi' else 'volte'):
            if value is not True:return reject('explicit_service_opt_out')
        elif name in ('volte','vonr','vowifi'):
            if not isinstance(value,bool):return reject('unknown_service_value')
        elif name in ('emergency','ut_xcap','vilte'):
            if value is not False:return reject('additional_service_capability')
        else:return reject('unmodelled_service_'+name)
    sip=config.get('sip',{})
    if not isinstance(sip,dict) or set(sip)-{'common','lte','nr','vowifi'}:return reject('unmodelled_sip_policy')
    if access in sip and not empty(sip[access]):return reject('access_specific_sip_policy')
    common=sip.get('common',{})
    if not isinstance(common,dict):return reject('invalid_common_sip_policy')
    for name,value in common.items():
        if name=='register':
            if not isinstance(value,dict):return reject('invalid_register_policy')
            for key,item in value.items():
                if key=='requested_expires_seconds':
                    if type(item) is not int or not 60<=item<=86400:return reject('unsupported_register_expiry')
                elif key=='always_add_sip_instance':
                    if item is not True:return reject('explicit_instance_omit')
                else:return reject('unmodelled_register_'+key)
        elif name=='contact_parameters':
            standard=[{'action':'add','name':n} for n in ('+g.3gpp.mid-call','+g.3gpp.srvcc-alerting','+g.3gpp.ps2cs-srvcc-orig-pre-alerting')]
            if value!=[] and (access!='lte' or encoded(value)!=encoded(standard)):return reject('custom_contact_features')
        elif name in ('headers','dialogs','status_policy'):
            if not empty(value):return reject('nondefault_sip_'+name)
        else:return reject('unmodelled_sip_'+name)
    accesses=config.get('access',{})
    if not isinstance(accesses,dict):return reject('invalid_access_policy')
    current=accesses.get(access)
    if not isinstance(current,dict):return reject('missing_access')
    allowed={'apn','ip_family','roaming_ip_family','auth_type','pcscf_discovery'}
    if access=='vowifi':allowed|={'epdg','ike','enabled'}
    if set(current)-allowed:return reject('unmodelled_access_policy')
    if str(current.get('apn','ims')).lower()!='ims':return reject('custom_apn')
    # PDN activation is outside the SIP simulator. Do not delete a source's
    # single-family requirement based only on a successful fake byte channel.
    if current.get('ip_family','ipv4v6')!='ipv4v6' or current.get('roaming_ip_family','ipv4v6')!='ipv4v6':return reject('explicit_address_family')
    if current.get('auth_type','unspecified') not in ('none','unspecified'):return reject('apn_authentication_required')
    expected=['ike_cfg'] if access=='vowifi' else ['pco','epco']
    if current.get('pcscf_discovery')!=expected:return reject('different_pcscf_discovery')
    if access=='vowifi':
        if current.get('enabled',True) is not True:return reject('wifi_explicitly_disabled')
        endpoints=current.get('epdg')
        if not isinstance(endpoints,list) or len(endpoints)!=1 or not isinstance(endpoints[0],dict):return reject('epdg_selection_policy')
        e=endpoints[0]
        if set(e)-{'address','address_kind','discovery','roaming_scope'} or expand(e.get('address'))!=epdg or e.get('discovery')!='static' or e.get('roaming_scope','both') not in ('both','home'):
            return reject('custom_epdg_or_scope')
        ike=current.get('ike',{})
        defaults={'eap_method':'eap_aka','initial_port':500,'natt_port':4500,'nat_traversal':True,'request_internal_address':True,'request_pcscf':True,'nat_keepalive_seconds':20,'dpd_interval_seconds':600}
        if not isinstance(ike,dict) or set(ike)-set(defaults)-{'identities'}:return reject('unmodelled_ike_policy')
        for key,value in ike.items():
            if key!='identities' and encoded(value)!=encoded(defaults[key]):return reject('nondefault_ike_'+key)
        if ike.get('eap_method')!='eap_aka':return reject('unknown_eap_method')
        identities=ike.get('identities',{})
        if not isinstance(identities,dict) or set(identities)!={'idi','idr'}:return reject('ike_identity_policy')
        for key,identity_type,source,template in (
            ('idi','id_rfc822_addr','derived_imsi','0{imsi}@nai.epc.mnc{mnc3}.mcc{mcc}.3gppnetwork.org'),
            ('idr','id_fqdn','epdg_fqdn','{epdg_fqdn}')):
            entries=identities[key]
            if not isinstance(entries,list) or len(entries)!=1 or not isinstance(entries[0],dict):return reject('ike_identity_policy')
            entry=entries[0]
            if set(entry)-{'identity_type','source','value_template','required'} or entry.get('identity_type')!=identity_type or entry.get('source')!=source or entry.get('value_template')!=template or entry.get('required',True) is not True:
                return reject('ike_identity_policy')
    return {'covered':True,'reason':'standard_requirements_covered_by_offline_matrix',
            'evidence_scope':'SIP/AKA/fallback with verified home PLMN; healthy bearer and provisioned subscription are assumptions',
            'live_network_verified':False}


def prune_profiles(connection,evidence):
    changes=[];reasons=Counter();counts=Counter();removed_profiles=0
    evidence_rows_deleted=0; evidence_parents_trimmed=0
    for pid,raw,lte,nr,wifi in connection.execute('SELECT profile_id,config_json,lte_ims_status,nr_ims_status,vowifi_status FROM carrier_profiles ORDER BY profile_id').fetchall():
        config=json.loads(raw)
        matches=connection.execute('SELECT DISTINCT plmn FROM profile_match_rules WHERE profile_id=? AND is_exclusion=0 AND plmn IS NOT NULL',(pid,)).fetchall()
        plmn=matches[0][0] if len(matches)==1 else ''
        verdicts={kind:decision(config,kind,plmn,status) for kind,status in (('lte',lte),('vowifi',wifi))}
        for kind,verdict in verdicts.items():reasons[kind+':'+verdict['reason']]+=1
        selected=[kind for kind,verdict in verdicts.items() if verdict['covered']]
        if not selected:continue
        kept=copy.deepcopy(config)
        for kind in selected:
            del kept['access'][kind]
            if isinstance(kept.get('sip'),dict):kept['sip'].pop(kind,None)
            counts[kind]+=1
        # NR and all other access types survive. A row is deleted only when
        # every access it actually contained was removed and no per-NR SIP data
        # remains. No compressed/stub reconstruction format is used.
        whole=not kept['access'] and all(empty(kept.get('sip',{}).get(kind,{})) for kind in ('lte','nr','vowifi') if kind not in selected)
        record={'profile_id':pid,'plmn':plmn,'removed_accesses':selected,'removed_profile':whole,
                'before_sha256':hashlib.sha256(encoded(config).encode()).hexdigest(),
                'decisions':verdicts,'simulation_sha256':evidence['sha256'],'live_network_verified':False}
        if whole:
            connection.execute('DELETE FROM carrier_profiles WHERE profile_id=?',(pid,));removed_profiles+=1
        else:
            # Compute status on a copy: do not replace removed access data with
            # a newly expanded missing-field list. Existing diagnostics for the
            # retained accesses stay byte-for-byte equivalent as JSON values.
            statuses=evaluate_readiness(copy.deepcopy(kept));validate_config(kept)
            connection.execute('UPDATE carrier_profiles SET config_json=?,lte_ims_status=?,nr_ims_status=?,vowifi_status=? WHERE profile_id=?',
                (encoded(kept),statuses['lte'],statuses['nr'],statuses['vowifi'],pid))
            for kind in selected:
                for prefix in ('/access/'+kind,'/sip/'+kind):
                    evidence_rows_deleted+=connection.execute("DELETE FROM field_evidence WHERE profile_id=? AND target_kind='config' AND (target_path=? OR substr(target_path,1,?)=?)",
                        (pid,prefix,len(prefix)+1,prefix+'/')).rowcount
            # Extractors often store a normalized whole access object as the
            # evidence value. Trim it only when it exactly equals the original
            # config subtree; otherwise retain the uninterpreted source fact.
            for key in ('access','sip'):
                for eid,raw_value in connection.execute("SELECT evidence_id,source_value_json FROM field_evidence WHERE profile_id=? AND target_kind='config' AND target_path=?",(pid,'/'+key)).fetchall():
                    if raw_value is not None and key in config and encoded(json.loads(raw_value))==encoded(config[key]):
                        connection.execute('UPDATE field_evidence SET source_value_json=? WHERE evidence_id=?',(encoded(kept.get(key,{})),eid))
                        evidence_parents_trimmed+=1
            record['after_sha256']=hashlib.sha256(encoded(kept).encode()).hexdigest()
        changes.append(record)
    return {'policy':POLICY_ID,'profiles_changed':len(changes),'profiles_removed':removed_profiles,
            'access_sections_removed':sum(counts.values()),'removed_by_access':dict(counts),'nr_accesses_removed':0,
            'retained_reason_counts':dict(sorted(reasons.items())),'fields_removed':0,
            'evidence_rows_deleted_for_partial_accesses':evidence_rows_deleted,
            'normalized_evidence_parents_trimmed':evidence_parents_trimmed,
            'changes':changes,'evidence_scope':'offline requirements model, not live carrier certification',
            'simulation_evidence':evidence,'contract_unchanged':'carrier-bundles-ims-v1'}
