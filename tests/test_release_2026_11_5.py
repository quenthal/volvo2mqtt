import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import unittest
from unittest.mock import Mock,patch
from config import settings
settings.set("babelLocale","fi_FI")
import activity,freshness,polling
from activity import VehicleActivity,REST,ACTIVE,DRIVING

DOORS="https://api.volvocars.com/connected-vehicle/v2/vehicles/{0}/doors"
WINDOWS="https://api.volvocars.com/connected-vehicle/v2/vehicles/{0}/windows"
ENGINE="https://api.volvocars.com/connected-vehicle/v2/vehicles/{0}/engine-status"
ENERGY="https://api.volvocars.com/energy/v2/vehicles/{0}/state"
LOCATION="https://api.volvocars.com/location/v1/vehicles/{0}/location"
ODOMETER="https://api.volvocars.com/connected-vehicle/v2/vehicles/{0}/odometer"

def doors(lock="LOCKED",driver="CLOSED"):
    return {"data":{"centralLock":{"value":lock},"frontLeftDoor":{"value":driver}}}
def engine(value):
    return {"data":{"engineStatus":{"value":value}}}

class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.now=[1000.0]
        self.a=VehicleActivity(clock=lambda:self.now[0])

    def test_rest_intervals_and_first_observation_does_not_activate(self):
        self.assertEqual(self.a.observe(DOORS,"V",doors("UNLOCKED","OPEN")),())
        self.assertEqual(self.a.mode("V"),REST)
        self.assertEqual(self.a.interval(DOORS,"V",300),120)
        self.assertEqual(self.a.interval(WINDOWS,"V",300),180)
        self.assertEqual(self.a.interval(ENGINE,"V",300),180)
        self.assertEqual(self.a.interval(LOCATION,"V",300),3600)

    def test_door_change_activates_and_syncs_then_expires(self):
        self.a.observe(DOORS,"V",doors())
        with self.assertLogs(level="INFO") as logs:
            refetch=self.a.observe(DOORS,"V",doors("UNLOCKED"))
        self.assertEqual(set(refetch),{"doors","engine","energy","location"})
        self.assertEqual(self.a.mode("V"),ACTIVE)
        self.assertEqual(self.a.interval(DOORS,"V",300),30)
        self.assertEqual(self.a.interval(ENGINE,"V",300),60)
        self.assertTrue(any("REST -> ACTIVE" in line for line in logs.output))
        self.assertFalse(any("UNLOCKED" in line for line in logs.output))
        self.now[0]+=activity.ACTIVE_SECONDS+1
        self.assertEqual(self.a.mode("V"),REST)
        # Staying unlocked without changes does not keep fast polling.
        self.assertEqual(self.a.observe(DOORS,"V",doors("UNLOCKED")),())
        self.assertEqual(self.a.interval(DOORS,"V",300),120)

    def test_driving_then_park_burst_until_fresh_data(self):
        self.a.observe(ENGINE,"V",engine("STOPPED"))
        self.assertEqual(self.a.observe(ENGINE,"V",engine("RUNNING")),("energy",))
        self.assertEqual(self.a.mode("V"),DRIVING)
        self.assertEqual(self.a.interval(DOORS,"V",300),300)
        self.assertEqual(self.a.interval(LOCATION,"V",300),3600)
        self.assertEqual(self.a.interval(ENGINE,"V",300),60)
        stop_time=5000.0
        refetch=self.a.observe(ENGINE,"V",engine("STOPPED"),stop_time,stop_time)
        self.assertEqual(set(refetch),{"location","odometer","energy"})
        self.assertEqual(self.a.mode("V"),ACTIVE)
        for url in (LOCATION,ODOMETER,ENERGY):
            self.assertEqual(self.a.interval(url,"V",300),60)
        # Location still from before the stop: keep bursting; newer: done.
        self.a.observe(LOCATION,"V",{"data":{}},stop_time-600)
        self.assertEqual(self.a.interval(LOCATION,"V",300),60)
        self.a.observe(LOCATION,"V",{"data":{}},stop_time+20)
        self.assertEqual(self.a.interval(LOCATION,"V",300),900)
        self.now[0]+=activity.PARK_BURST_FAST+1
        self.assertEqual(self.a.interval(ODOMETER,"V",300),120)   # slower burst phase
        self.now[0]+=activity.PARK_BURST_SECONDS
        self.assertEqual(self.a.interval(ODOMETER,"V",300),3600)  # burst and ACTIVE over

    def test_command_touch_and_charging_and_budget(self):
        self.assertEqual(set(self.a.touch("V","command lock")),{"doors","engine","energy","location"})
        self.assertEqual(self.a.mode("V"),ACTIVE)
        self.assertEqual(self.a.interval(ENERGY,"V",300,charging=True),120)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.a.interval(DOORS,"V",300,daily_gets=activity.DAILY_BUDGET+1),120)
        self.assertEqual(self.a.interval(DOORS,"V",300,daily_gets=10),30)

    def test_engine_true_false_and_unknown_values(self):
        self.a.observe(ENGINE,"V",engine("false"))
        self.a.observe(ENGINE,"V",engine("true"))
        self.assertEqual(self.a.mode("V"),DRIVING)
        # Unknown or missing values must not invent a stop (and a park burst).
        for value in (None,"UNKNOWN","running"):
            self.assertEqual(self.a.observe(ENGINE,"V",engine(value)),())
            self.assertEqual(self.a.mode("V"),DRIVING)
        self.assertEqual(self.a.observe(ENGINE,"V",{"data":{}}),())

    def test_door_change_without_seen_trip_starts_location_burst_cancelled_by_driving(self):
        self.a.observe(ENGINE,"V",engine("STOPPED"));self.a.observe(DOORS,"V",doors())
        self.a.observe(DOORS,"V",doors("UNLOCKED"),3000.0)
        self.assertEqual(self.a.interval(LOCATION,"V",300),60)
        self.a.observe(ENGINE,"V",engine("RUNNING"))
        self.assertEqual(self.a.interval(LOCATION,"V",300),3600)

class CacheIntegrationTests(unittest.TestCase):
    def test_malformed_engine_payloads_do_not_raise(self):
        cache=polling.EndpointCache();session=Mock()
        for body in ({"data":None},{"data":[]},[],{"data":{"engineStatus":None}}):
            session.get.return_value=Mock(status_code=200,json=Mock(return_value=body))
            cache.items.clear();cache.get(session,ENGINE,"V")

    def test_command_timeout_still_touches_without_retry(self):
        import requests,volvo
        from const import CLIMATE_START_URL
        with patch.object(volvo,"refresh_auth"),patch.object(volvo.session,"post",side_effect=requests.Timeout) as post,\
                patch.object(volvo.endpoint_cache,"touch") as touch,self.assertLogs(level="ERROR"):
            self.assertIsNone(volvo.api_call(CLIMATE_START_URL,"POST","V"))
        post.assert_called_once();touch.assert_called_once()

    def test_door_change_invalidates_other_groups_for_next_loop(self):
        cache=polling.EndpointCache();session=Mock()
        responses={DOORS:[doors(),doors("UNLOCKED")],ENGINE:[engine("STOPPED")]*3,ENERGY:[{}]*3}
        def get(url,**kw):
            template={u.format("V"):u for u in responses}[url]
            return Mock(status_code=200,json=Mock(return_value=responses[template].pop(0)))
        session.get.side_effect=get
        clock=[0.0]
        with patch("polling.time.monotonic",side_effect=lambda:clock[0]),patch.object(cache.activity,"clock",lambda:clock[0]):
            for url in (DOORS,ENGINE,ENERGY):cache.get(session,url,"V")
            clock[0]=130
            cache.get(session,DOORS,"V")          # REST doors 120 s: refetched, now UNLOCKED
            calls=session.get.call_count
            cache.get(session,ENGINE,"V");cache.get(session,ENERGY,"V")  # invalidated: fetched at once (location not cached here)
            self.assertEqual(session.get.call_count,calls+2)
            clock[0]=145
            cache.get(session,DOORS,"V")          # ACTIVE doors 30 s: still cached
            self.assertEqual(session.get.call_count,calls+2)

class FreshnessEventTests(unittest.TestCase):
    def test_fresh_data_logged_without_values_and_untimed_structure_once(self):
        stats=freshness.FreshnessStats(interval=10**9,clock=lambda:2000000000.0)
        old={"data":{"x":{"value":"SECRET","timestamp":"2033-05-18T03:30:00Z"}}}
        new={"data":{"x":{"value":"SECRET","timestamp":"2033-05-18T03:31:00Z"}}}
        stats.record(DOORS,"VIN1",old)
        with self.assertLogs(level="INFO") as logs:
            stats.record(DOORS,"VIN1",new)
            stats.record(ENERGY,"VIN1",{"batteryChargeLevel":{"status":"OK","value":87,"unit":"percentage","updatedAt":None}})
            stats.record(ENERGY,"VIN1",{"batteryChargeLevel":{"value":88}})
            stats.record("https://api.volvocars.com/x/{0}/statistics","VIN1",new)
        text="\n".join(logs.output)
        self.assertIn("Fresh data: doors car_time=03:31:00Z",text)
        self.assertIn("Untimed payload fields (energy): {batteryChargeLevel:{status,unit,updatedAt,value}}",text)
        self.assertEqual(text.count("Untimed payload fields"),1)
        self.assertNotIn("statistics",text)
        for secret in ("SECRET","VIN1","87","percentage"):self.assertNotIn(secret,text)

    def test_structure_masks_non_schema_keys(self):
        text=freshness.structure({"Not-a-schema-key":{"value":1},"batteryChargeLevel":{"x_1":2,"unit":3}})
        self.assertEqual(text,"{<key>:{value},batteryChargeLevel:{unit,<key>}}")

    def test_engine_stop_time_from_engine_field_only(self):
        cache=polling.EndpointCache();session=Mock()
        payload={"data":{"engineStatus":{"value":"STOPPED","timestamp":"2026-11-08T10:00:00Z"},"other":{"updatedAt":"2026-11-08T11:00:00Z"}}}
        session.get.return_value=Mock(status_code=200,json=Mock(return_value=payload))
        with patch.object(cache.activity,"observe",return_value=()) as observe:
            cache.get(session,ENGINE,"V")
        newest,event=observe.call_args.args[3:5]
        self.assertGreater(newest,event)

    def test_updated_at_counts_as_timestamp(self):
        self.assertIsNotNone(freshness.newest_timestamp({"a":{"updatedAt":"2026-11-08T10:00:00Z"}}))

if __name__=="__main__":
    unittest.main()
