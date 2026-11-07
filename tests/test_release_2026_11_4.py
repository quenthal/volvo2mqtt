import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import time,unittest
from datetime import datetime,timezone
from unittest.mock import Mock,patch
from config import settings
settings.set("babelLocale","fi_FI")
import freshness,mqtt,volvo
from control_queue import ControlQueue
from polling import EndpointCache

class ButtonRepeatTests(unittest.TestCase):
    def wait_idle(self,q):
        end=time.monotonic()+3
        while time.monotonic()<end:
            with q.condition:
                if not q.workers:return
            time.sleep(.01)
        self.fail("worker failed to drain")

    def test_buttons_send_every_press_but_toggles_suppress(self):
        q=ControlQueue(.01,.01);button=Mock(return_value=True);toggle=Mock(return_value=True)
        for _ in range(2):
            q.submit("car","climate","START",button,suppress_repeat=False);self.wait_idle(q)
            q.submit("car","lock","LOCKED",toggle);self.wait_idle(q)
        self.assertEqual(button.call_count,2)
        self.assertEqual(toggle.call_count,1)

    def test_debounce_still_keeps_only_latest_press(self):
        q=ControlQueue(.05,.01);calls=[]
        with self.assertLogs(level="INFO") as logs:
            for target in ("START","STOP","START"):
                q.submit("car","climate",target,lambda t=target:calls.append(t) or True,suppress_repeat=False)
            self.wait_idle(q)
        self.assertEqual(calls,["START"])
        self.assertEqual(sum("replaced" in line for line in logs.output),2)

    def test_skipped_repeat_is_logged(self):
        q=ControlQueue(.01,.01)
        q.submit("car","lock","LOCKED",lambda:True);self.wait_idle(q)
        with self.assertLogs(level="INFO") as logs:
            q.submit("car","lock","LOCKED",lambda:True);self.wait_idle(q)
        self.assertTrue(any("skipped" in line for line in logs.output))

    def test_mqtt_buttons_do_not_suppress_repeats(self):
        with patch.object(mqtt,"control_queue") as q:
            mqtt.start_climate("V");mqtt.stop_climate("V")
        for call in q.submit.call_args_list:
            self.assertIs(call.kwargs["suppress_repeat"],False)
        entity={"id":"flash_lights","vehicle_command":"FLASH","domain":"button"}
        with patch.object(mqtt,"control_queue") as q,patch.dict(volvo.supported_endpoints,{"V":[entity]},clear=True):
            mqtt.on_message(None,None,Mock(topic="homeassistant/button/V_flash_lights/command",payload=b"PRESS",retain=0))
        self.assertIs(q.submit.call_args.kwargs["suppress_repeat"],False)

class FreshnessTests(unittest.TestCase):
    def test_newest_timestamp_nested_and_tolerant(self):
        payload={"data":{"frontLeftDoor":{"value":"CLOSED","timestamp":"2026-11-07T10:00:00.000Z"},
                         "hood":{"value":"CLOSED","timestamp":"2026-11-07T10:05:00Z"},
                         "bad":{"timestamp":"not a time"},"list":[{"timestamp":"2026-11-07T09:00:00+00:00"}]}}
        self.assertEqual(freshness.newest_timestamp(payload),datetime(2026,11,7,10,5,tzinfo=timezone.utc).timestamp())
        self.assertIsNone(freshness.newest_timestamp({"data":{"x":{"value":1}}}))

    def test_labels(self):
        self.assertEqual(freshness.label("https://api.volvocars.com/connected-vehicle/v2/vehicles/{0}/doors"),"doors")
        self.assertEqual(freshness.label("https://api.volvocars.com/energy/v2/vehicles/{0}/state"),"energy")
        self.assertEqual(freshness.label("https://api.volvocars.com/connected-vehicle/v2/vehicles/{0}"),"vehicle")

    def test_hourly_report_counts_new_timestamps_without_values(self):
        now=[datetime(2026,11,7,10,6,40,tzinfo=timezone.utc).timestamp()]
        stats=freshness.FreshnessStats(interval=3600,clock=lambda:now[0])
        url="https://api.volvocars.com/x/{0}/doors"
        old={"data":{"frontLeftDoor":{"value":"SECRET_VALUE","timestamp":"2026-11-07T10:05:00Z"}}}
        new={"data":{"frontLeftDoor":{"value":"SECRET_VALUE","timestamp":"2026-11-07T10:06:00Z"}}}
        self.assertIsNone(stats.record(url,"VIN",old))
        now[0]+=60;self.assertIsNone(stats.record(url,"VIN",old))
        now[0]+=3600
        with self.assertLogs(level="INFO") as logs:
            report=stats.record(url,"VIN",new)
        self.assertIn("doors n=3 new=2",report)
        self.assertIn("last 61 min",logs.output[0])
        # Timestamp-free fetches are counted; an older timestamp is not "new".
        stats.record("https://api.volvocars.com/x/{0}/fuel","VIN",{"data":{}})
        stats.record(url,"VIN",old)
        now[0]+=3600
        report=stats.record(url,"VIN",old)
        self.assertIn("doors n=2 new=0",report);self.assertIn("fuel n=1 new=0 untimed=1",report)
        self.assertNotIn("SECRET_VALUE",logs.output[0]);self.assertNotIn("VIN",logs.output[0])

    def test_cache_records_freshness_only_for_successful_fetches(self):
        cache=EndpointCache();cache.freshness=Mock();session=Mock()
        session.get.return_value=Mock(status_code=200,json=Mock(return_value={"data":{}}))
        cache.get(session,"https://api.volvocars.com/x/{}/doors","V")
        cache.get(session,"https://api.volvocars.com/x/{}/doors","V")  # cached, not re-recorded
        session.get.return_value=Mock(status_code=500)
        cache.get(session,"https://api.volvocars.com/x/{}/windows","V")
        cache.freshness.record.assert_called_once()

if __name__=="__main__":
    unittest.main()
