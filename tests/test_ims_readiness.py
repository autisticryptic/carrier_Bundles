"""Registration readiness and LTE/NR voice capabilities are distinct facts."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_contract import finalized_config


class ImsReadinessTests(unittest.TestCase):
    def config(self):
        return {
            "ims": {
                "home_domain": "ims.example.test",
                "authentication": {"scheme": "ims_aka"},
            },
            "access": {
                "lte": {"apn": "ims", "pcscf_discovery": ["pco"]},
                "nr": {"dnn": "ims", "pcscf_discovery": ["epco"]},
            },
            "services": {"volte": False, "vonr": False, "smsoip": True},
        }

    def test_voice_disabled_does_not_disable_complete_sms_only_ims(self):
        config = self.config()
        before = copy.deepcopy(config)
        encoded, status = finalized_config(config)
        self.assertEqual(status["lte"], "ready")
        self.assertEqual(status["nr"], "ready")
        self.assertEqual(json.loads(encoded)["services"], before["services"])
        self.assertEqual(config, before, "finalizing must not mutate source facts")

    def test_voice_flags_do_not_fill_missing_registration_configuration(self):
        for voice in (True, False, None):
            with self.subTest(voice=voice):
                config = self.config()
                config["services"].update(volte=voice, vonr=voice)
                del config["ims"]["authentication"]
                encoded, status = finalized_config(config)
                self.assertEqual(status["lte"], "partial")
                self.assertEqual(status["nr"], "partial")
                self.assertIn("/ims/authentication/scheme", json.loads(encoded)["readiness"]["lte_missing"])
                config["access"] = {}
                _, status = finalized_config(config)
                self.assertEqual(status["lte"], "unknown")
                self.assertEqual(status["nr"], "unknown")

    def test_wifi_explicit_service_opt_out_is_preserved(self):
        config = self.config()
        config["services"]["vowifi"] = False
        _, status = finalized_config(config)
        self.assertEqual(status["vowifi"], "unsupported")
        self.assertEqual(status["lte"], "ready")


if __name__ == "__main__":
    unittest.main()
