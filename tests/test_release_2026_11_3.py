import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import json,time,unittest,uuid
from unittest.mock import Mock,patch
from config import settings
settings.set("babelLocale","fi_FI")
import mqtt,volvo,vehicle_commands
from const import CLIMATE_START_URL,CLIMATE_STOP_URL,supported_entities
from control_queue import ControlQueue

class ClimateButtonTests(unittest.TestCase):
    def wait_idle(self,q):
        end=time.monotonic()+3
        while time.monotonic()<end:
            with q.condition:
                if not q.workers:return
            time.sleep(.01)
        self.fail("worker failed to drain")

    def test_switch_replaced_by_buttons(self):
        ids={e["id"]:e for e in supported_entities}
        self.assertNotIn("climate_status",ids)
        self.assertEqual(ids["climate_start"]["domain"],"button")
        self.assertEqual(ids["climate_stop"]["domain"],"button")

    def test_button_press_routes_to_shared_queue_group(self):
        with patch.object(mqtt,"control_queue") as q,patch.dict(volvo.supported_endpoints,{},clear=True):
            for name,target in (("climate_start","START"),("climate_stop","STOP")):
                mqtt.on_message(None,None,Mock(topic=f"homeassistant/button/VIN123_{name}/command",payload=b"PRESS",retain=0))
                self.assertEqual(q.submit.call_args.args[:3],("VIN123","climate",target))
            q.reset_mock()
            mqtt.on_message(None,None,Mock(topic="homeassistant/button/VIN123_climate_start/command",payload=b"ON",retain=0))
            mqtt.on_message(None,None,Mock(topic="homeassistant/button/VIN123_climate_stop/command",payload=b"PRESS",retain=1))
            q.submit.assert_not_called()

    def test_burst_sends_only_latest_climate_command(self):
        q=ControlQueue(.04,.01);calls=[]
        for target in ("START","STOP","START"):
            q.submit("car","climate",target,lambda t=target:calls.append(t) or True)
        self.wait_idle(q);self.assertEqual(calls,["START"])

    def test_commands_have_no_local_state_side_effects(self):
        for fn,url in ((mqtt._start_climate,CLIMATE_START_URL),(mqtt._stop_climate,CLIMATE_STOP_URL)):
            with patch.object(volvo,"api_call",return_value=True) as call,patch.object(mqtt,"publish_local_state") as publish,patch.object(mqtt,"Timer") as timer:
                self.assertTrue(fn("TEST"))
                self.assertEqual(call.call_args.args,(url,"POST","TEST"))
                publish.assert_not_called();timer.assert_not_called()

    def test_engine_state_poll_has_no_command_side_effect(self):
        response=Mock(status_code=200,text="")
        response.json.return_value={"data":{"engineStatus":{"value":"RUNNING"}}}
        with patch.object(volvo,"refresh_auth"),patch.object(volvo,"cached_request",return_value=response),\
                patch.object(mqtt,"control_queue") as q,patch.object(mqtt,"publish_local_state") as publish:
            self.assertEqual(volvo.api_call("https://example.invalid/{}","GET","TEST","engine_state"),"ON")
            q.submit.assert_not_called();publish.assert_not_called()

    def test_old_switch_discovery_removed(self):
        client=Mock()
        with patch.object(mqtt,"mqtt_client",client,create=True),patch.object(volvo,"vins",["VIN123"]):
            mqtt.delete_old_entities()
        published={c.args[0]:c.kwargs for c in client.publish.call_args_list}
        self.assertEqual(published["homeassistant/switch/volvoAAOS2mqtt/VIN123_climate_status/config"],{"payload":"","retain":True})
        self.assertIn("homeassistant/switch/VIN123_climate_status/state",published)

class OperationIdTests(unittest.TestCase):
    def post(self,status=200,body=None,error=None):
        response=Mock(status_code=status,text="")
        if error:response.json.side_effect=error
        else:response.json.return_value=body if body is not None else {"data":{"invokeStatus":"COMPLETED","message":""}}
        return response

    def test_api_call_sends_unique_operation_id_and_logs_it(self):
        with patch.object(volvo,"refresh_auth"),patch.object(volvo.session,"post",return_value=self.post()) as post:
            with self.assertLogs(level="INFO") as logs:
                self.assertTrue(volvo.api_call(CLIMATE_STOP_URL,"POST","TEST"))
                self.assertTrue(volvo.api_call(CLIMATE_STOP_URL,"POST","TEST"))
        ids=[c.kwargs["headers"]["vcc-api-operationId"] for c in post.call_args_list]
        self.assertEqual(len(set(ids)),2);[uuid.UUID(i) for i in ids]
        self.assertEqual(post.call_args.kwargs["json"],{})
        self.assertTrue(any("climatization-stop: COMPLETED" in line and ids[0] in line for line in logs.output))

    def test_rejections_and_non_json_bodies_are_logged_not_retried(self):
        cases=[self.post(422,{"error":{"message":"UNPROCESSABLE_ENTITY","description":"Error returned from car"}}),
               self.post(415,error=json.JSONDecodeError("x","",0)),
               self.post(200,{"data":{"invokeStatus":"CAR_ERROR"}})]
        for response in cases:
            with self.subTest(status=response.status_code),patch.object(volvo,"refresh_auth"),\
                    patch.object(volvo.session,"post",return_value=response) as post,self.assertLogs(level="INFO") as logs:
                self.assertFalse(volvo.api_call(CLIMATE_STOP_URL,"POST","TEST"))
                post.assert_called_once()
                self.assertIn("operationId",logs.output[-1])

    def test_optional_commands_send_operation_id(self):
        session=Mock();session.post.return_value=self.post(body={"data":{"invokeStatus":"COMPLETED"}})
        self.assertTrue(vehicle_commands.execute(session,"TEST","flash_lights",{"flash_lights"}))
        uuid.UUID(session.post.call_args.kwargs["headers"]["vcc-api-operationId"])

if __name__=="__main__":
    unittest.main()
