import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
import json
import unittest
from unittest.mock import Mock, patch
from config import settings
settings.set("babelLocale", "fi_FI")
import mqtt
import volvo
import util
import keycloak

class BridgeTests(unittest.TestCase):
    def test_null_normalization(self):
        self.assertEqual(util.drop_null_fields({"a":None,"b":{"value":None},"c":0,"d":False,"e":[None,{"v":None}]}),
            {"b":{},"c":0,"d":False,"e":[None,{}]})
        self.assertFalse(util.keys_exists(None,"value"))
        self.assertFalse(util.keys_exists({"x":{}},"x","value"))

    def test_malformed_sensor_does_not_raise(self):
        response=Mock(status_code=200,text="{}")
        response.json.return_value={"data":{"odometer":{"value":None}}}
        with patch.object(volvo,"refresh_auth"), patch.object(volvo.session,"get",return_value=response):
            self.assertIsNone(volvo.api_call("https://example.invalid/{}","GET","TEST","odometer"))

    def test_backend_message_preserved_and_bounded(self):
        entity={"id":"api_backend_status","domain":"sensor"}
        for value in ["NO_WARNING","APIERR_TIMEOUT","x"*255,"ä"*303,None]:
            with self.subTest(value=value), patch.object(mqtt,"mqtt_client",Mock(),create=True), \
                 patch.object(volvo,"vins",["TEST"]), patch.object(volvo,"supported_endpoints",{"TEST":[entity]}), \
                 patch.object(volvo,"backend_status",value), patch.object(mqtt,"update_ha_device"), \
                 patch.object(mqtt,"settings",{"babelLocale":"fi_FI"}):
                mqtt.update_car_data()
                calls=mqtt.mqtt_client.publish.call_args_list
                attrs=[c.args[1] for c in calls if c.args[0].endswith("/attributes")]
                states=[c.args[1] for c in calls if c.args[0].endswith("/state")]
                self.assertEqual(json.loads(attrs[0])["message"],value or "")
                self.assertLessEqual(len(states[0]),255)
                if value and len(value)<=255:self.assertEqual(states[0],value)

    def test_auth_required_is_published(self):
        client=Mock()
        client.access_token.side_effect=keycloak.LoginRequired("login")
        with patch.object(volvo,"auth_client",client), patch.object(mqtt,"set_login_required") as notify:
            with self.assertRaises(keycloak.LoginRequired):volvo.refresh_auth()
            notify.assert_called_once_with(True)

    def test_discovery_has_message_attributes(self):
        entity={"id":"api_backend_status","domain":"sensor","name":"API status","icon":"cloud"}
        with patch.object(mqtt,"mqtt_client",Mock(),create=True), patch.object(volvo,"vins",["TEST"]), \
             patch.object(volvo,"supported_endpoints",{"TEST":[entity]}), \
             patch.object(volvo,"get_vehicle_details",return_value={"identifiers":["TEST"]}), \
             patch.object(mqtt.time,"sleep"), patch.object(mqtt,"send_heartbeat"):
            mqtt.create_ha_devices()
            payloads=[json.loads(c.args[1]) for c in mqtt.mqtt_client.publish.call_args_list if c.args[0].endswith("/config")]
            config=next(x for x in payloads if x.get("unique_id")=="volvoAAOS2mqtt_TEST_api_backend_status")
            self.assertTrue(config["json_attributes_topic"].endswith("/attributes"))

if __name__=="__main__":unittest.main()
