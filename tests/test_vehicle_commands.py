import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import unittest
from unittest.mock import Mock,patch
import requests
import vehicle_commands as commands

class CommandTests(unittest.TestCase):
    def test_discovery_whitelist(self):
        session=Mock()
        session.get.return_value=Mock(status_code=200)
        session.get.return_value.json.return_value={"data":[{"command":x} for x in ["FLASH","HONK","HONK_AND_FLASH","LOCK_REDUCED_GUARD","ENGINE_START"]]}
        entities=commands.discover(session,"TEST")
        self.assertEqual({x["id"] for x in entities},set(commands.DEFINITIONS))
        session.post.assert_not_called()

    def test_discovery_failure_no_buttons(self):
        session=Mock();session.get.return_value=Mock(status_code=403)
        self.assertEqual(commands.discover(session,"TEST"),[])

    def test_exact_paths_no_engine(self):
        for entity_id,(_,endpoint,_,_) in commands.DEFINITIONS.items():
            session=Mock();session.post.return_value=Mock(status_code=200)
            session.post.return_value.json.return_value={"data":{"invokeStatus":"WAITING"}}
            self.assertTrue(commands.execute(session,"TEST",entity_id,{entity_id}))
            self.assertEqual(session.post.call_args.args[0],commands.BASE.format("TEST")+"/"+endpoint)
            self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        session=Mock()
        self.assertFalse(commands.execute(session,"TEST","engine_start",{"engine_start"}))
        session.post.assert_not_called()

    def test_timeout_not_retried(self):
        session=Mock();session.post.side_effect=requests.Timeout()
        self.assertFalse(commands.execute(session,"TEST","honk_horn",{"honk_horn"}))
        self.assertEqual(session.post.call_count,1)

    def test_retained_wrong_payload_and_foreign_topic_do_not_execute(self):
        from config import settings
        settings.set("babelLocale","fi_FI")
        import mqtt,volvo
        entities={"TEST":[{"id":"flash_lights","vehicle_command":"FLASH"}]}
        with patch.object(volvo,"supported_endpoints",entities),patch.object(mqtt,"control_queue") as queue:
            for topic,payload,retained in [("homeassistant/button/TEST_flash_lights/command",b"PRESS",True),
                                           ("homeassistant/button/TEST_flash_lights/command",b"OTHER",False),
                                           ("homeassistant/button/OTHER_flash_lights/command",b"PRESS",False)]:
                mqtt.on_message(None,None,Mock(topic=topic,payload=payload,retain=retained))
            queue.submit.assert_not_called()
            mqtt.on_message(None,None,Mock(topic="homeassistant/button/TEST_flash_lights/command",payload=b"PRESS",retain=False))
            queue.submit.assert_called_once()
            self.assertEqual(queue.submit.call_args.args[:3],("TEST","flash_lights","PRESS"))
