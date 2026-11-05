import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import threading
import unittest
from unittest.mock import Mock
from http.server import ThreadingHTTPServer
import requests
from web_login import Service, ORIGIN

class WebLoginTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock()
        self.client.begin.return_value=("https://auth.volvocars.com/test", "verifier", "state")
        self.service=Service("test-password-123456789012345",self.client)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),self.service.handler())
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base="http://127.0.0.1:"+str(self.server.server_port)
        self.auth=("admin",self.service.password)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def form(self):
        response=requests.get(self.base+"/login",auth=self.auth,timeout=5)
        self.assertEqual(response.headers["Referrer-Policy"],"same-origin")
        self.assertIn("https://auth.volvocars.com",response.headers["Content-Security-Policy"])
        return response.cookies["volvo_login"]

    def post(self,headers,bad_nonce=False):
        nonce=self.form()
        return requests.post(self.base+"/login",auth=self.auth,
            headers={"Cookie":"volvo_login="+nonce,**headers},
            data={"nonce":"wrong" if bad_nonce else nonce},allow_redirects=False,timeout=5)

    def test_normal_origin(self):
        self.assertEqual(self.post({"Origin":ORIGIN}).status_code,303)

    def test_suppressed_origin_with_same_origin_metadata(self):
        for headers in [{"Origin":"null","Sec-Fetch-Site":"same-origin"},{"Sec-Fetch-Site":"same-origin"}]:
            with self.subTest(headers=headers):self.assertEqual(self.post(headers).status_code,303)

    def test_foreign_and_unverified_origins_rejected(self):
        for headers in [{},{"Origin":"null"},{"Origin":"https://evil.invalid","Sec-Fetch-Site":"same-origin"},{"Origin":"null","Sec-Fetch-Site":"cross-site"}]:
            with self.subTest(headers=headers):self.assertEqual(self.post(headers).status_code,403)
        self.client.begin.assert_not_called()

    def test_nonce_still_required(self):
        self.assertEqual(self.post({"Origin":"null","Sec-Fetch-Site":"same-origin"},True).status_code,400)
        self.client.begin.assert_not_called()

    def test_callback_requires_cookie_and_is_single_use(self):
        nonce=self.form();headers={"Origin":ORIGIN,"Cookie":"volvo_login="+nonce}
        requests.post(self.base+"/login",auth=self.auth,headers=headers,data={"nonce":nonce},allow_redirects=False,timeout=5)
        callback=self.base+"/oauth/callback?code=test&state=state"
        self.assertEqual(requests.get(callback,timeout=5).status_code,400)
        response=requests.get(callback,headers=headers,timeout=5)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers["Referrer-Policy"],"no-referrer")
        self.assertEqual(requests.get(callback,headers=headers,timeout=5).status_code,400)
        self.client.exchange.assert_called_once()
