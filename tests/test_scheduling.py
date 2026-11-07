import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock,patch
from control_queue import ControlQueue
from polling import EndpointCache

class SchedulingTests(unittest.TestCase):
    def wait_idle(self,q):
        end=time.monotonic()+3
        while time.monotonic()<end:
            with q.condition:
                if not q.workers:return
            time.sleep(.01)
        self.fail("worker failed to drain")

    def test_toggle_burst_only_latest(self):
        q=ControlQueue(.04,.01);calls=[]
        for state in ('ON','OFF','ON'):
            q.submit('car','climate',state,lambda value=state:calls.append(value) or True)
        self.wait_idle(q);self.assertEqual(calls,['ON'])

    def test_inflight_then_latest_intent_serialized(self):
        q=ControlQueue(.01,.01);started=threading.Event();release=threading.Event();calls=[]
        def start():
            calls.append('ON');started.set();release.wait(1);return True
        q.submit('car','climate','ON',start)
        self.assertTrue(started.wait(1))
        for state in ('OFF','ON','OFF'):
            q.submit('car','climate',state,lambda value=state:calls.append(value) or True)
        release.set();self.wait_idle(q)
        self.assertEqual(calls,['ON','OFF'])

    def test_duplicate_success_suppressed(self):
        q=ControlQueue(.01,.01);action=Mock(return_value=True)
        q.submit('car','climate','ON',action);self.wait_idle(q)
        q.submit('car','climate','ON',action);self.wait_idle(q)
        action.assert_called_once()

    def test_simultaneous_endpoint_reads_share_request(self):
        cache=EndpointCache();session=Mock()
        session.get.return_value=Mock(status_code=200)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _:cache.get(session,'https://api.volvocars.com/vehicles/{}/doors','TEST'),range(16)))
        session.get.assert_called_once()

    def test_group_intervals_and_bounded_force(self):
        cache=EndpointCache();session=Mock();session.get.return_value=Mock(status_code=200)
        url='https://api.volvocars.com/vehicles/{}/doors'
        with patch('polling.time.monotonic',return_value=0):cache.get(session,url,'TEST')
        with patch('polling.time.monotonic',return_value=5):cache.get(session,url,'TEST',force=True)
        self.assertEqual(session.get.call_count,1)
        with patch('polling.time.monotonic',return_value=11):cache.get(session,url,'TEST',force=True)
        self.assertEqual(session.get.call_count,2)
        self.assertEqual(cache.interval(url,'TEST',300),60)
        energy='https://api.volvocars.com/energy/v2/vehicles/{}/state'
        self.assertEqual(cache.interval(energy,'TEST',300),300)
        cache.charging['TEST']=True
        self.assertEqual(cache.interval(energy,'TEST',300),120)

    def test_duplicate_completion_cleans_pending(self):
        q=ControlQueue(.01,.01);action=Mock(return_value=True);done=Mock()
        q.submit('car','climate','ON',action,done);self.wait_idle(q)
        q.submit('car','climate','ON',action,done);self.wait_idle(q)
        action.assert_called_once();self.assertEqual(done.call_count,2)

    def test_vehicle_request_counter_excludes_identity_and_counts_failure(self):
        import requests
        from polling import CountingSession
        session=CountingSession()
        with patch.object(requests.Session,'send',side_effect=requests.Timeout):
            for url in ['https://api.volvocars.com/test','https://auth.volvocars.com/test']:
                request=requests.Request('GET',url).prepare()
                with self.assertRaises(requests.Timeout):session.send(request)
        self.assertEqual(session.snapshot(),{'GET':1})
