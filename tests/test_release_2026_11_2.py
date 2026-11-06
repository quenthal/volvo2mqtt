import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import json,tempfile,threading,time,unittest
from unittest.mock import Mock,patch
from config import settings
settings.set("babelLocale","fi_FI")
import keycloak,mqtt,volvo
from control_queue import ControlQueue
from polling import EndpointCache

class ReleaseFixTests(unittest.TestCase):
    def wait_idle(self,q):
        end=time.monotonic()+3
        while time.monotonic()<end:
            with q.condition:
                if not q.workers:return
            time.sleep(.01)
        self.fail("worker failed to drain")

    def test_unconfirmed_lock_is_not_suppressed(self):
        for fn in (mqtt._lock_car,mqtt._unlock_car):
            with patch.object(volvo,"api_call",return_value=True),patch.object(volvo,"check_lock_status",return_value=False),patch.object(mqtt,"publish_local_state"):
                self.assertFalse(fn("TEST"))
        q=ControlQueue(.01,.01);action=Mock(return_value=False)
        q.submit("car","lock","LOCKED",action);self.wait_idle(q)
        q.submit("car","lock","LOCKED",action);self.wait_idle(q)
        self.assertEqual(action.call_count,2)

    def test_duplicate_callback_runs_outside_queue_lock(self):
        q=ControlQueue(.01,.01);acquired=[]
        def done():
            # Another thread must be able to take the queue lock while the callback runs.
            t=threading.Thread(target=lambda:acquired.append(q.condition.acquire(timeout=.5)) or q.condition.release())
            t.start();t.join()
        q.submit("car","climate","ON",lambda:True);self.wait_idle(q)
        q.submit("car","climate","ON",lambda:True,done);self.wait_idle(q)
        self.assertEqual(acquired,[True])

    def test_climate_submission_is_atomic_with_generation(self):
        order=[]
        def submit(vin,group,target,action,done=None):
            order.append((mqtt.climate_request_generation[vin],target))
        with patch.object(mqtt.control_queue,"submit",side_effect=submit),patch.object(mqtt,"publish_local_state"),\
                patch.dict(mqtt.pending_climate,{},clear=True),patch.dict(mqtt.climate_request_generation,{},clear=True):
            threads=[threading.Thread(target=mqtt.start_climate if i%2 else mqtt.stop_climate,args=("TEST",)) for i in range(20)]
            for t in threads:t.start()
            for t in threads:t.join()
            self.assertEqual([g for g,_ in order],list(range(1,21)))
            self.assertEqual(mqtt.pending_climate["TEST"],(20,order[-1][1]))

    def test_refresh_commands_do_not_block_mqtt_thread(self):
        msg=Mock(topic="homeassistant/button/TEST_update_data/command",payload=b"PRESS",retain=0)
        release=threading.Event();calls=[]
        def slow(force):
            calls.append(force);release.wait(1)
        with patch.object(mqtt,"update_car_data",side_effect=slow),patch.dict(volvo.supported_endpoints,{},clear=True):
            for _ in range(5):mqtt.on_message(None,None,msg)  # returns immediately, at most one pending refresh
            release.set()
            end=time.monotonic()+2
            while mqtt.async_refresh_lock.locked() and time.monotonic()<end:time.sleep(.01)
        self.assertEqual(calls,[True])
        self.assertFalse(mqtt.async_refresh_lock.locked())

    def test_discovery_subscription_and_config_deduplicated(self):
        client=Mock();client.publish.return_value.rc=mqtt.mqtt.MQTT_ERR_SUCCESS;entity={"id":"lock_status","domain":"lock","name":"Lock state","icon":"lock"}
        with patch.object(mqtt,"mqtt_client",client,create=True),patch.object(mqtt,"subscribed_topics",[]),\
                patch.dict(mqtt.published_configs,{},clear=True),patch.dict(mqtt.devices,{"TEST":{}},clear=True):
            for state in ("LOCKED","LOCKED","UNLOCKED"):
                mqtt.update_ha_device(entity,"TEST",state)
            self.assertEqual(mqtt.subscribed_topics,["homeassistant/lock/TEST_lock_status/command"])
            self.assertEqual(client.subscribe.call_count,1)
            self.assertEqual(client.publish.call_count,2)
            # A failed publish is retried on the next refresh; reconnect forces republish.
            client.publish.return_value.rc=mqtt.mqtt.MQTT_ERR_NO_CONN
            mqtt.update_ha_device(entity,"TEST","LOCKED")
            client.publish.return_value.rc=mqtt.mqtt.MQTT_ERR_SUCCESS
            mqtt.update_ha_device(entity,"TEST","LOCKED")
            self.assertEqual(client.publish.call_count,4)
            mqtt.update_ha_device(entity,"TEST","LOCKED")
            self.assertEqual(client.publish.call_count,4)
            mqtt.published_configs.clear()
            mqtt.update_ha_device(entity,"TEST","LOCKED")
            self.assertEqual(client.publish.call_count,5)

    def test_rejected_refresh_token_not_resent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=keycloak.Client(dict(client_id="client",client_secret="secret",redirect_uri="http://localhost:8765/callback",
                scopes="openid"),Path(tmp)/"token.json")
            client._save(dict(access_token="a",refresh_token="r",expires_at=time.time()-10,client_id="client",issuer=keycloak.TOKEN_URL))
            rejected=Mock(status_code=400,json=Mock(return_value={"error":"invalid_grant"}))
            with patch("keycloak.requests.post",return_value=rejected) as post:
                for _ in range(3):
                    with self.assertRaises(keycloak.LoginRequired):client.access_token()
                self.assertEqual(post.call_count,1)
                post.return_value=Mock(status_code=200,json=Mock(return_value=dict(access_token="new",refresh_token="n",expires_in=300)))
                client.exchange("http://localhost:8765/callback?code=c&state=s","v","s")
                self.assertEqual(client.access_token(),"new")
            # A restarted process retries the saved token once.
            client._save(dict(access_token="a",refresh_token="r",expires_at=time.time()-10,client_id="client",issuer=keycloak.TOKEN_URL))
            other=keycloak.Client(client.config,client.path)
            with patch("keycloak.requests.post",return_value=rejected) as post:
                for _ in range(2):
                    with self.assertRaises(keycloak.LoginRequired):other.access_token()
                self.assertEqual(post.call_count,1)

    def test_external_login_during_rejected_refresh_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=dict(client_id="client",client_secret="secret",redirect_uri="http://localhost:8765/callback",scopes="openid")
            client=keycloak.Client(config,Path(tmp)/"token.json")
            client._save(dict(access_token="a",refresh_token="r",expires_at=time.time()-10,client_id="client",issuer=keycloak.TOKEN_URL))
            def reject(*args,**kwargs):
                # Another process saves a fresh login while Volvo rejects the old token.
                keycloak.Client(config,client.path)._save(dict(access_token="fresh",refresh_token="f",
                    expires_at=time.time()+300,client_id="client",issuer=keycloak.TOKEN_URL))
                return Mock(status_code=400,json=Mock(return_value={"error":"invalid_grant"}))
            with patch("keycloak.requests.post",side_effect=reject):
                with self.assertRaises(keycloak.LoginRequired):client.access_token()
            self.assertEqual(client.access_token(),"fresh")

    def test_key_change_bypasses_cache(self):
        cache=EndpointCache();session=Mock();session.get.return_value=Mock(status_code=403)
        cache.get(session,"https://api.volvocars.com/x/{}/doors","V")
        cache.get(session,"https://api.volvocars.com/x/{}/doors","V")
        self.assertEqual(session.get.call_count,1)
        cache.get(session,"https://api.volvocars.com/x/{}/doors","V",bypass=True)
        self.assertEqual(session.get.call_count,2)

    def test_wrapped_charging_power_unit(self):
        data={"data":{"chargingPower":{"status":"OK","value":7.2,"unit":"kW"}}}
        self.assertEqual(volvo.parse_api_data(data,"charging_power"),7200)

if __name__=="__main__":
    unittest.main()
