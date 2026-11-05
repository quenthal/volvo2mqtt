import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import unittest
from unittest.mock import Mock,patch
from config import settings
settings.set("babelLocale","fi_FI")
import energy,volvo

class EnergyTests(unittest.TestCase):
    def test_error_values_not_published_as_zero(self):
        self.assertIsNone(energy.value({"chargingPower":{"status":"ERROR","value":100}},"chargingPower"))
        self.assertIsNone(energy.value({"chargingPower":{"status":"OK","value":None}},"chargingPower"))
        self.assertEqual(energy.value({"chargingPower":{"status":"OK","value":0}},"chargingPower"),0)

    def test_new_fields_and_units(self):
        data={"chargingCurrentLimit":{"status":"OK","value":16,"unit":"ampere"},
              "targetBatteryChargeLevel":{"status":"OK","value":80},
              "chargingPower":{"status":"OK","value":7.2,"unit":"kW"},
              "chargingType":{"status":"OK","value":"AC"}}
        self.assertEqual(volvo.parse_api_data(data,"charging_current_limit"),16)
        self.assertEqual(volvo.parse_api_data(data,"target_battery_charge_level"),80)
        self.assertEqual(volvo.parse_api_data(data,"charging_power"),7200)
        self.assertEqual(volvo.parse_api_data(data,"charging_type"),"AC")

    def test_battery_fallback(self):
        self.assertEqual(volvo.parse_api_data({"data":{"batteryChargeLevel":{"value":65}}},"battery_charge_level"),65)

    def test_capabilities_keep_temporarily_missing_power(self):
        session=Mock();session.get.return_value=Mock(status_code=200)
        session.get.return_value.json.return_value={"getEnergyState":{"chargingPower":{"isSupported":True},"chargingSystemStatus":{"isSupported":True},"transferLimit":{"isSupported":False}}}
        self.assertEqual(energy.capabilities(session,"TEST"),{"charging_power","charging_system_status"})
        session.post.assert_not_called()

    def test_unknown_status_survives(self):
        self.assertEqual(volvo.parse_api_data({"chargingStatus":{"value":"NEW_STATE","status":"OK"}},"charging_system_status"),"NEW_STATE")

    def test_runtime_and_web_share_token_owner(self):
        from web_login import Service
        client=Mock();client.access_token.return_value="test";client.token={"expires_at":2000000000}
        with patch.object(volvo,"auth_client",client),patch.object(volvo.mqtt,"set_login_required"):
            service=Service("test-password-123456789012345",client)
            self.assertIs(service.get_client(),volvo.auth_client)
            service.get_client().exchange("callback","verifier","state")
            volvo.refresh_auth()
            client.access_token.assert_called_once()
