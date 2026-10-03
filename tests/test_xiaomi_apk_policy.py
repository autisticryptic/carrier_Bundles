"""Source-shaped Xiaomi policy tests: scopes, selected includes and provenance."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from android.xiaomi.apk_resources import ApkXmlDocument, AndroidAttribute
from android.xiaomi.apk_policy import compile_documents, document_blocks, CompiledPolicy, load_apk_policies
from android.xiaomi.carrier_config import _build_config, _parse_apns, _choose_apn, _parse_carrier_config, import_xiaomi_carrier_config_catalog
from android.xiaomi.firmware import ExtractedXiaomiCarrierConfig
from android.xiaomi.sources import XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0


def doc(member, xml, types=None):
    return ApkXmlDocument(member, ET.fromstring(xml), Path('CarrierConfig.apk'), 'a'*64, 'b'*64, types or {})


def source_set():
    base = {
        'res/xml/vendor.xml': doc('res/xml/vendor.xml', '<carrier_config_list/>'),
        'res/xml/vendor_miui.xml': doc('res/xml/vendor_miui.xml', '''<carrier_config_list><carrier_config>
          <boolean name="carrier_wfc_ims_available_bool" value="false"/>
        </carrier_config></carrier_config_list>'''),
        'res/xml/vendor_device.xml': doc('res/xml/vendor_device.xml', '<carrier_config_list/>'),
        'assets/default_selected.xml': doc('assets/default_selected.xml', '''<carrier_config_list>
          <carrier_config mcc="234" mnc="15,20"><boolean name="carrier_wfc_ims_available_bool" value="true"/></carrier_config>
        </carrier_config_list>'''),
        'assets/default_inactive.xml': doc('assets/default_inactive.xml', '''<carrier_config_list>
          <carrier_config mcc="234" mnc="15"><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>
        </carrier_config_list>'''),
    }
    device = {'res/xml/vendor_device.xml': doc('res/xml/vendor_device.xml', '''<carrier_config_list>
      <carrier_config include="default_selected.xml"/>
      <carrier_config mcc="234" mnc="20"><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>
    </carrier_config_list>''')}
    return base, device


class XiaomiApkPolicyTests(unittest.TestCase):
    def test_selected_defaults_and_final_disable_do_not_merge_all_versions(self):
        base, device = source_set()
        policies, summary = compile_documents(base, {}, device, {'23415','23420','310260'})
        p = {x.plmn:x for x in policies}
        self.assertIs(p['23415'].values['carrier_wfc_ims_available_bool'], True)
        self.assertIs(p['23420'].values['carrier_wfc_ims_available_bool'], False)
        self.assertIs(p['310260'].values['carrier_wfc_ims_available_bool'], False)
        self.assertEqual(summary['selected_default_assets'], ['assets/default_selected.xml'])
        origin=p['23415'].origins['carrier_wfc_ims_available_bool']
        self.assertEqual(origin['member'], 'assets/default_selected.xml')
        self.assertEqual(origin['selectors'], {'mcc':'234','mnc':'15,20'})

    def test_unknown_sim_predicate_is_withheld_not_promoted_to_plmn(self):
        base, device = source_set()
        device['res/xml/vendor_device.xml'] = doc('res/xml/vendor_device.xml', '''<carrier_config_list>
          <carrier_config include="default_selected.xml"/>
          <carrier_config mcc="234" mnc="15" gid1="BAE0"><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>
          <carrier_config mcc="234" mnc="20" spn="special"><boolean name="carrier_wfc_ims_available_bool" value="true"/></carrier_config>
        </carrier_config_list>''')
        p={x.plmn:x for x in compile_documents(base,{},device,set())[0]}
        self.assertNotIn('carrier_wfc_ims_available_bool',p['23415'].values)
        self.assertIn('carrier_wfc_ims_available_bool',p['23415'].uncertain)
        self.assertIs(p['23420'].values['carrier_wfc_ims_available_bool'],True,'same-value conditional does not change invariant')

    def test_later_unconditional_assignment_resolves_prior_uncertainty(self):
        root=doc('source.xml','''<carrier_config_list>
          <carrier_config mcc="234" mnc="15" imsi="23415.*"><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>
          <carrier_config mcc="234" mnc="15"><boolean name="carrier_wfc_ims_available_bool" value="true"/></carrier_config>
        </carrier_config_list>''')
        p=CompiledPolicy('23415')
        for b in document_blocks(root):p.apply(b)
        self.assertIs(p.values['carrier_wfc_ims_available_bool'],True);self.assertEqual(p.uncertain,{})

    def test_carrier_id_filename_is_never_plmn(self):
        base,device=source_set()
        base['assets/carrier_config_carrierid_10001_Example.xml']=doc('assets/carrier_config_carrierid_10001_Example.xml', '''<carrier_config_list><carrier_config>
          <string name="iwlan.epdg_static_address_string">private.example</string>
          <boolean name="carrier_wfc_ims_available_bool" value="false"/>
        </carrier_config></carrier_config_list>''')
        policies,summary=compile_documents(base,{},device,{'23415'})
        self.assertNotIn('10001',{x.plmn for x in policies})
        p=next(x for x in policies if x.plmn=='23415')
        self.assertIs(p.values['carrier_wfc_ims_available_bool'],True)
        self.assertNotIn('iwlan.epdg_static_address_string',p.values)
        self.assertIn('iwlan.epdg_static_address_string',p.uncertain)
        self.assertEqual(summary['carrier_id_assets_not_mapped'],1)

    def test_singleton_numeric_mnc_matches_android_without_changing_plmn(self):
        d=doc('res/xml/vendor_device.xml', '''<carrier_config_list><carrier_config mcc="234" mnc="1">
          <boolean name="carrier_wfc_ims_available_bool" value="false"/>
        </carrier_config></carrier_config_list>''')
        node=d.root[0];d.attribute_types[node]={'mnc':AndroidAttribute(0x10,1,None)}
        block=document_blocks(d)[0]
        for plmn in ('23401','234001'):
            p=CompiledPolicy(plmn,{'carrier_wfc_ims_available_bool':True});p.apply(block)
            self.assertIs(p.values['carrier_wfc_ims_available_bool'],False)
            self.assertEqual(p.plmn,plmn)
            self.assertEqual(p.uncertain,{})
        for spelling in ('1','01','001'):
            plain=doc('source.xml',f'<carrier_config mcc="234" mnc="{spelling}"><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>')
            for plmn in ('23401','234001'):
                p=CompiledPolicy(plmn,{'carrier_wfc_ims_available_bool':True});p.apply(document_blocks(plain)[0])
                self.assertIs(p.values['carrier_wfc_ims_available_bool'],False)
        listed=doc('source.xml','<carrier_config mcc="234" mnc="1,02"><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>')
        p=CompiledPolicy('23401',{'carrier_wfc_ims_available_bool':True});p.apply(document_blocks(listed)[0])
        self.assertIs(p.values['carrier_wfc_ims_available_bool'],True,'comma-list membership does not normalize numeric width')

    def test_miui_sunset_include_is_flushed_before_loose_and_device_overrides(self):
        base,device=source_set()
        base['res/xml/vendor_miui.xml']=doc('res/xml/vendor_miui.xml','''<carrier_config_list>
          <carrier_config><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>
          <carrier_config include="2g_and_3g_sunset.xml"/>
        </carrier_config_list>''')
        base['assets/2g_and_3g_sunset.xml']=doc('assets/2g_and_3g_sunset.xml','''<carrier_config_list>
          <carrier_config mcc="425" mnc="09,19"><boolean name="carrier_wfc_ims_available_bool" value="true"/></carrier_config>
        </carrier_config_list>''')
        loose=doc('mi_ext/product/etc/vendor_miui.xml','''<carrier_config_list>
          <carrier_config mcc="425" mnc="19"><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>
        </carrier_config_list>''')
        policies,summary=compile_documents(base,{},device,{'42509','42519'},[loose])
        values={p.plmn:p.values.get('carrier_wfc_ims_available_bool') for p in policies}
        self.assertIs(values['42509'],True);self.assertIs(values['42519'],False)
        self.assertEqual(summary['selected_default_assets'],['assets/2g_and_3g_sunset.xml','assets/default_selected.xml'])

    def test_standalone_xml_does_not_flatten_gid_or_carrierid_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'carrier_config_23415.xml'
            path.write_text('''<carrier_config_list>
              <carrier_config><boolean name="carrier_wfc_ims_available_bool" value="false"/></carrier_config>
              <carrier_config gid1="AB"><boolean name="carrier_wfc_ims_available_bool" value="true"/></carrier_config>
            </carrier_config_list>''')
            self.assertIsNone(_parse_carrier_config(path,Path(tmp)))
            other=path.with_name('carrier_config_carrierid_10001_Test.xml')
            other.write_text('<carrier_config><boolean name="carrier_wfc_ims_available_bool" value="true"/></carrier_config>')
            self.assertIsNone(_parse_carrier_config(other,Path(tmp)))

    def test_no_standard_derived_keeps_only_source_policy(self):
        config=_build_config({'carrier_wfc_ims_available_bool':True,
            'iwlan.epdg_static_address_string':'epdg.operator.example'},[],'23415',include_standard_derived=False)
        self.assertNotIn('home_domain',config['ims'])
        self.assertNotIn('identity_templates',config['ims'])
        self.assertNotIn('ike',config['access']['vowifi'])
        self.assertEqual(config['access']['vowifi']['epdg'][0]['address'],'epdg.operator.example')
        self.assertEqual(config['access']['vowifi']['epdg'][0]['discovery'],'static')

    def test_uncertain_endpoint_does_not_silently_become_standard_domain(self):
        config=_build_config({'carrier_wfc_ims_available_bool':True},[],'23415',include_standard_derived=True,
            uncertain_keys=('iwlan.epdg_static_address_string',))
        self.assertNotIn('epdg',config['access']['vowifi'])

    def test_real_shaped_apk_import_keeps_raw_wfc_and_derived_epdg_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);apk=root/'system_ext/priv-app/CarrierConfig/CarrierConfig.apk';apk.parent.mkdir(parents=True)
            base,device=source_set()
            base['res/xml/vendor_device.xml']=device['res/xml/vendor_device.xml']
            with zipfile.ZipFile(apk,'w') as z:
                z.writestr('AndroidManifest.xml','<manifest package="com.android.carrierconfig"/>')
                for member,d in base.items():z.writestr(member,ET.tostring(d.root))
            dbfile=root/'catalog.sqlite3'
            repo=Path(__file__).resolve().parents[1]
            subprocess.run([sys.executable,str(repo/'tools/init_db.py'),str(dbfile),'--release-id','fixture'],check=True,capture_output=True)
            source=ExtractedXiaomiCarrierConfig(root/'rom.zip','a'*64,root,(apk,),None)
            stats=import_xiaomi_carrier_config_catalog(dbfile,source,artifact=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0)
            self.assertEqual(stats.profiles_imported,2)
            with sqlite3.connect(dbfile) as db:
                pid,raw,status=db.execute("select profile_id,config_json,vowifi_status from carrier_profiles where profile_id like '%23415-%'").fetchone()
                self.assertEqual(status,'ready');config=json.loads(raw)
                self.assertTrue(config['services']['vowifi'])
                self.assertEqual(config['access']['vowifi']['epdg'][0]['discovery'],'standard_derived')
                raw_fact=db.execute("select source_path,source_key_path,source_value_json from field_evidence where profile_id=? and target_path='/services/vowifi' and evidence_kind='extracted'",(pid,)).fetchone()
                self.assertIn('CarrierConfig.apk!assets/default_selected.xml',raw_fact[0]);self.assertTrue(json.loads(raw_fact[2]))
                self.assertEqual(json.loads(raw_fact[1])['selectors'],{'mcc':'234','mnc':'15,20'})
                self.assertEqual(db.execute("select count(*) from field_evidence where profile_id=? and target_path='/access/vowifi/epdg' and evidence_kind='standard_derived'",(pid,)).fetchone()[0],1)
                self.assertEqual(db.execute("select count(*) from field_evidence where profile_id=? and target_path='/access/vowifi/epdg' and evidence_kind='extracted'",(pid,)).fetchone()[0],0)
                self.assertEqual(db.execute("select count(*) from field_evidence where target_path in ('/services','/access','/sip')").fetchone()[0],0,'no mixed section attributed wholly to standards')
                self.assertEqual(db.execute("select vowifi_status from carrier_profiles where profile_id like '%23420-%'").fetchone()[0],'unsupported')

    def test_manifest_size_and_duplicate_are_rejected_before_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'CarrierConfig.apk'
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr('AndroidManifest.xml',b'x'*2048)
            with patch('android.xiaomi.apk_policy.MAX_XML_BYTES',1024),self.assertRaisesRegex(ValueError,'oversized'):
                load_apk_policies((path,),set(),Path(tmp))
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with zipfile.ZipFile(path,'w') as z:
                    z.writestr('AndroidManifest.xml','<manifest package="com.android.carrierconfig"/>')
                    z.writestr('AndroidManifest.xml','<manifest package="com.android.carrierconfig"/>')
            with self.assertRaisesRegex(ValueError,'duplicate'):
                load_apk_policies((path,),set(),Path(tmp))

    def test_missing_or_conditional_include_fails_closed(self):
        base,device=source_set()
        for attrs in ('include="missing.xml"','include="../escape.xml"','include="default_selected.xml" gid1="1"'):
            device['res/xml/vendor_device.xml']=doc('res/xml/vendor_device.xml',f'<carrier_config_list><carrier_config {attrs}/></carrier_config_list>')
            with self.subTest(attrs=attrs),self.assertRaises(ValueError):compile_documents(base,{},device,{'23415'})

    def test_mvno_apn_cannot_become_generic_and_wifi_domain_is_labelled_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'apns-conf.xml'
            path.write_text('<apns><apn mcc="234" mnc="15" apn="private" type="ims" mvno_type="gid" mvno_match_data="AB"/></apns>')
            apns=_parse_apns(path,Path(tmp))
            self.assertEqual(len(apns),1);self.assertIsNone(_choose_apn(apns,'vowifi'))
            config=_build_config({'carrier_wfc_ims_available_bool':True},apns,'23415',include_standard_derived=True)
            self.assertEqual(config['access']['vowifi']['epdg'][0]['discovery'],'standard_derived')
            self.assertNotIn('apn',config['access']['vowifi'])
            no=_build_config({'carrier_wfc_ims_available_bool':False},apns,'23415',include_standard_derived=True)
            self.assertFalse(no['services']['vowifi'])
            self.assertNotIn('epdg',no['access']['vowifi'])

if __name__=='__main__':unittest.main()
