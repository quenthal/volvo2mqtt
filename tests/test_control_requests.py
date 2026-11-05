import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import unittest
from unittest.mock import Mock,patch
from config import settings
settings.set("babelLocale","fi_FI")
import volvo,mqtt
from const import CAR_UNLOCK_URL,CLIMATE_START_URL

class ControlTests(unittest.TestCase):
    def test_empty_json_and_acceptance(self):
        for url in (CAR_UNLOCK_URL,CLIMATE_START_URL):
            response=Mock(status_code=200,text="")
            response.json.return_value={"data":{"invokeStatus":"WAITING"}}
            with patch.object(volvo,"refresh_auth"),patch.object(volvo.session,"post",return_value=response) as post:
                self.assertTrue(volvo.api_call(url,"POST","TEST"))
                self.assertEqual(post.call_args.kwargs["json"],{})
                self.assertFalse(post.call_args.kwargs["allow_redirects"])
                post.assert_called_once()

    def test_rejection_is_not_retried(self):
        response=Mock(status_code=415,text="")
        response.json.return_value={"error":{"message":"unsupported"}}
        with patch.object(volvo,"refresh_auth"),patch.object(volvo.session,"post",return_value=response) as post:
            self.assertFalse(volvo.api_call(CAR_UNLOCK_URL,"POST","TEST"))
            post.assert_called_once()

    def test_no_optimistic_state_on_failure(self):
        for fn in (mqtt._unlock_car,mqtt._lock_car,mqtt._start_climate,mqtt._stop_climate):
            with patch.object(volvo,"api_call",return_value=False),patch.object(mqtt,"update_car_data") as update,patch.object(mqtt,"Thread") as thread,patch.object(mqtt,"Timer") as timer:
                fn("TEST")
                update.assert_not_called();thread.assert_not_called();timer.assert_not_called()

    def test_unknown_invocation_not_success(self):
        response=Mock(status_code=200,text="")
        response.json.return_value={"data":{"invokeStatus":"UNKNOWN"}}
        with patch.object(volvo,"refresh_auth"),patch.object(volvo.session,"post",return_value=response):
            self.assertFalse(volvo.api_call(CAR_UNLOCK_URL,"POST","TEST"))
