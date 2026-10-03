"""Direct pruning leaves the original v7 format, and retains uncovered accesses."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from catalog_contract import finalized_config
from simulation_pruning.prune import IDENTITIES, decision, prune_profiles
from simulation_pruning.evidence import EXPECTED, validate_evidence
from tools.build_variants import build_variants
from tools.verify_catalog import verify_catalog
from test_catalog_variants import fixture


def standard(wifi=False):
    access={'lte':{'apn':'ims','auth_type':'none','ip_family':'ipv4v6','pcscf_discovery':['pco','epco']}}
    if wifi:
        access={'vowifi':{'apn':'ims','pcscf_discovery':['ike_cfg'],
            'epdg':[{'address':'epdg.epc.mnc026.mcc310.pub.3gppnetwork.org','discovery':'static'}],
            'ike':{'eap_method':'eap_aka','identities':{
                'idi':[{'identity_type':'id_rfc822_addr','source':'derived_imsi','value_template':'0{imsi}@nai.epc.mnc{mnc3}.mcc{mcc}.3gppnetwork.org'}],
                'idr':[{'identity_type':'id_fqdn','source':'epdg_fqdn','value_template':'{epdg_fqdn}'}]}}}}
    return {'protocol_baseline':'carrier-bundles-ims-v1','ims':{
        'home_domain':'ims.mnc026.mcc310.3gppnetwork.org','realm':'ims.mnc026.mcc310.3gppnetwork.org',
        'authentication':{'scheme':'ims_aka'},'identity_templates':copy.deepcopy(IDENTITIES)},'access':access,
        'services':{'volte':True,'vowifi':True,'smsoip':True},'sip':{'common':{'headers':[]}}}


def report_fixture(path):
    sources={name:'0'*64 for name in ('backend/src/connectivity/core/register.rs','backend/src/connectivity/core/digest_aka.rs',
        'backend/src/connectivity/modems/ims/vowifi/profiles.rs','backend/src/connectivity/modems/ims/vowifi/carrier_catalog_v7.rs',
        'offline-registration-sim/simulator.rs')}
    data={'suite_id':'simadmin-offline-derived-registration-v1','report_format':1,'evidence_kind':'offline_simulation',
        'passed':True,'hardware_used':False,'live_network_verified':False,'source_files_sha256':sources,
        'source_tree_sha256':hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'scenarios':[{'id':name,'passed':True,'expected_success':ok,'observed_success':ok} for name,ok in EXPECTED.items()]}
    path.write_text(json.dumps(data));return data


def set_profile(conn,pid,config):
    raw,status=finalized_config(config)
    conn.execute('UPDATE carrier_profiles SET config_json=?,lte_ims_status=?,nr_ims_status=?,vowifi_status=? WHERE profile_id=?',
        (raw,status['lte'],status['nr'],status['vowifi'],pid))

class SimulationPruningTests(unittest.TestCase):
    def test_standard_accesses_qualify_independently(self):
        self.assertTrue(decision(standard(),'lte','31026','ready')['covered'])
        self.assertTrue(decision(standard(True),'vowifi','31026','ready')['covered'])
        mixed=standard();mixed['access'].update(standard(True)['access'])
        mixed['access']['vowifi']['epdg'][0]['address']='special.operator.example'
        self.assertTrue(decision(mixed,'lte','31026','ready')['covered'])
        self.assertFalse(decision(mixed,'vowifi','31026','ready')['covered'])

    def test_lte_required_is_covered_but_wifi_and_disabled_remain_explicit(self):
        config=standard()
        config['ims']['security_agreement']='required'
        config['sip']['common']['register']={'security_agreement':'required'}
        self.assertTrue(decision(config,'lte','31026','ready')['covered'])
        wifi=standard(True)
        wifi['ims']['security_agreement']='required'
        self.assertFalse(decision(wifi,'vowifi','31026','ready')['covered'])
        for mode in ('disabled','omit','unknown',False,[]):
            config['sip']['common']['register']['security_agreement']=mode
            self.assertFalse(decision(config,'lte','31026','ready')['covered'])
        config['ims']['security_agreement']='disabled'
        config['sip']['common']['register']['security_agreement']='required'
        self.assertFalse(decision(config,'lte','31026','ready')['covered'])

    def test_special_and_unmodelled_requirements_are_retained(self):
        for mutate in (
            lambda c:c['ims'].update(realm='special.example'),
            lambda c:c['ims'].update(security_agreement='disabled'),
            lambda c:c['ims'].update(transport='tcp'),
            lambda c:c['access']['lte'].update(apn='operator-ims'),
            lambda c:c['access']['lte'].update(auth_type='pap'),
            lambda c:c['access']['lte'].update(ip_family='ipv6'),
            lambda c:c['access']['lte'].update(username='user'),
            lambda c:c['services'].update(volte=False),
            lambda c:c['services'].update(emergency=True),
            lambda c:c.update(entitlement={'required':True}),
            lambda c:c.update(media={'audio':{'codecs':['EVS']}}),
            lambda c:c.update(unknown={'policy':True}),
            lambda c:c['sip']['common'].update(register={'include_pani_initial':False}),
        ):
            config=standard();mutate(config)
            with self.subTest(config=config):self.assertFalse(decision(config,'lte','31026','ready')['covered'])
        for plmn in ('99999','','31026,310260'):
            self.assertFalse(decision(standard(),'lte',plmn,'ready')['covered'])
        self.assertFalse(decision(standard(),'lte','31026','unknown')['covered'])

    def test_new_database_directly_deletes_whole_or_partial_config_without_changing_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=fixture(root/'source.sqlite3')
            mixed=standard();mixed['access'].update(standard(True)['access']);mixed['access']['vowifi']['epdg'][0]['address']='special.operator.example'
            with closing(sqlite3.connect(source)) as conn,conn:
                set_profile(conn,'profile-0',standard())
                set_profile(conn,'profile-1',mixed)
            before=source.read_bytes();report=root/'report.json';report_fixture(report)
            built=build_variants([source],root/'out',simulation_report=report)
            self.assertEqual(source.read_bytes(),before)
            entry=built['catalogs'][0];summary=entry['minimal_elision']
            self.assertEqual(summary['access_sections_removed'],2)
            self.assertEqual(summary['profiles_removed'],1)
            output=root/'out'/entry['variants']['minimal-no-icons']['database']
            self.assertEqual(verify_catalog(output)['config_contract'],'carrier-bundles-ims-v1')
            with closing(sqlite3.connect(output)) as conn:
                self.assertEqual(conn.execute('pragma user_version').fetchone()[0],7)
                self.assertEqual(conn.execute("select count(*) from sqlite_schema where type='table' and name not like 'sqlite_%'").fetchone()[0],8)
                self.assertEqual(conn.execute("select count(*) from carrier_profiles where profile_id='profile-0'").fetchone()[0],0)
                raw,lte,wifi=conn.execute("select config_json,lte_ims_status,vowifi_status from carrier_profiles where profile_id='profile-1'").fetchone()
                kept=json.loads(raw)
                self.assertNotIn('lte',kept['access'])
                self.assertEqual(kept['access']['vowifi'],mixed['access']['vowifi'])
                self.assertEqual((lte,wifi),('unknown','ready'))
                self.assertEqual(kept['ims'],mixed['ims'])
                self.assertNotIn('_derive',kept)
                self.assertEqual(conn.execute('pragma foreign_key_check').fetchall(),[])
            for variant in ('full','no-icons'):
                file=root/'out'/entry['variants'][variant]['database']
                with closing(sqlite3.connect(file)) as conn:
                    self.assertEqual(conn.execute('select count(*) from carrier_profiles').fetchone()[0],2)

    def test_nr_and_other_uncovered_sip_policy_are_never_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            source=fixture(Path(directory)/'source.sqlite3')
            nr=standard();nr['access']['nr']={'dnn':'ims','pcscf_discovery':['epco','pco']}
            future=standard();future['sip']['vowifi']={'unknown_requirement':True}
            with closing(sqlite3.connect(source)) as conn,conn:
                conn.execute('pragma foreign_keys=ON')
                set_profile(conn,'profile-0',nr);set_profile(conn,'profile-1',future)
                result=prune_profiles(conn,{'sha256':'0'*64})
                self.assertEqual(result['profiles_removed'],0)
                records=[json.loads(r[0]) for r in conn.execute('select config_json from carrier_profiles order by profile_id')]
                self.assertEqual(records[0]['access']['nr'],nr['access']['nr'])
                self.assertEqual(records[1]['sip']['vowifi'],future['sip']['vowifi'])

    def test_reporting_does_not_count_missing_accesses_as_retained_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            source=fixture(Path(directory)/'source.sqlite3')
            mixed=standard();mixed['access'].update(standard(True)['access'])
            mixed['access']['vowifi']['epdg'][0]['address']='special.operator.example'
            with closing(sqlite3.connect(source)) as conn,conn:
                conn.execute('pragma foreign_keys=ON')
                set_profile(conn,'profile-0',standard())
                set_profile(conn,'profile-1',mixed)
                result=prune_profiles(conn,{'sha256':'0'*64})
                self.assertEqual(result['access_inventory']['lte'],
                    {'present':2,'source_ready':2,'model_covered':2})
                self.assertEqual(result['access_inventory']['vowifi'],
                    {'absent':1,'present':1,'source_ready':1,'retained':1})
                self.assertEqual(result['retained_reason_counts'],{'vowifi:custom_epdg_or_scope':1})
                self.assertEqual(result['classification_counts']['lte:standard_requirements_covered_by_offline_matrix'],2)
                self.assertNotIn('vowifi:not_ready_in_source',result['retained_reason_counts'])
                # Reporting changes do not enlarge the deletion scope.
                self.assertEqual(result['removed_by_access'],{'lte':2})
                self.assertEqual(result['profiles_removed'],1)

    def test_reporting_keeps_empty_profiles_distinct_from_uncovered_configurations(self):
        with tempfile.TemporaryDirectory() as directory:
            source=fixture(Path(directory)/'source.sqlite3')
            empty=standard();empty['access']={}
            with closing(sqlite3.connect(source)) as conn,conn:
                for pid in ('profile-0','profile-1'):set_profile(conn,pid,empty)
                result=prune_profiles(conn,{'sha256':'0'*64})
                self.assertEqual(result['access_inventory'],{'lte':{'absent':2},'vowifi':{'absent':2}})
                self.assertEqual(result['retained_reason_counts'],{})
                self.assertEqual(result['classification_counts'],{})
                self.assertEqual(result['profiles_removed'],0)
                self.assertEqual(conn.execute('select count(*) from carrier_profiles').fetchone()[0],2)

    def test_incomplete_or_inconsistent_simulation_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'report.json';base=report_fixture(path)
            self.assertEqual(validate_evidence(path)['scenarios'],21)
            for mutate in (
                lambda d:d.update(live_network_verified=True),
                lambda d:d['scenarios'].pop(),
                lambda d:d['scenarios'][-1].update(observed_success=False),
                lambda d:d.update(source_tree_sha256='f'*64),
                lambda d:d['source_files_sha256'].update({'../../secret':'0'*64}),
            ):
                data=copy.deepcopy(base);mutate(data);path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):validate_evidence(path)

if __name__=='__main__':unittest.main()
