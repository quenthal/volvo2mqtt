import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import base64
import hashlib
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
import keycloak

class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = keycloak.Client(dict(client_id="client", client_secret="secret",
            redirect_uri="http://localhost:8765/callback", scopes="conve:vehicle_relation"),
            Path(self.tmp.name)/"token.json")

    def response(self, code=200, body=None):
        return Mock(status_code=code, json=Mock(return_value=body if body is not None else
            dict(access_token="access", refresh_token="rotated", expires_in=300)))

    def seed(self, expired=True):
        self.client._save(dict(access_token="old-access", refresh_token="old-refresh",
            expires_at=time.time()+(-10 if expired else 300), client_id="client", issuer=keycloak.TOKEN_URL))

    def test_pkce_and_scopes(self):
        url, verifier, state = self.client.begin()
        q = parse_qs(urlsplit(url).query)
        self.assertIn("offline_access", q["scope"][0].split())
        self.assertEqual(q["state"], [state])
        self.assertEqual(q["code_challenge"], [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()])
        self.assertNotIn("secret", url)
        self.assertNotIn(verifier, url)

    def test_offline_access_can_be_disabled_for_client(self):
        self.client.config["request_offline_access"] = False
        url, _, _ = self.client.begin()
        scopes = parse_qs(urlsplit(url).query)["scope"][0].split()
        self.assertNotIn("offline_access", scopes)
        self.assertIn("openid", scopes)
        self.assertIn("conve:vehicle_relation", scopes)

    @patch("keycloak.requests.post")
    def test_exchange_and_restart(self, post):
        post.return_value = self.response()
        self.client.exchange("http://localhost:8765/callback?code=abc&state=expected", "verifier", "expected")
        kw=post.call_args.kwargs
        self.assertEqual(kw["auth"], ("client", "secret"))
        self.assertFalse(kw["allow_redirects"])
        self.assertEqual(kw["data"]["code_verifier"], "verifier")
        other = keycloak.Client(self.client.config, self.client.path)
        self.assertEqual(other.access_token(), "access")
        self.assertEqual(post.call_count, 1)

    @patch("keycloak.requests.post")
    def test_reject_callback_before_network(self, post):
        for url in ["http://evil/callback?code=a&state=s", "http://localhost:8765/callback?code=a&state=wrong",
                    "http://localhost:8765/callback?code=a&code=b&state=s", "http://localhost:8765/callback?error=denied&state=s"]:
            with self.subTest(url=url), self.assertRaises(keycloak.OAuthFailure):
                self.client.exchange(url, "verifier", "s")
        post.assert_not_called()

    @patch("keycloak.requests.post")
    def test_rotating_refresh_is_serialized(self, post):
        self.seed()
        post.return_value=self.response()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.client.access_token(), range(16)))
        self.assertEqual(set(results), {"access"})
        self.assertEqual(post.call_count,1)
        self.assertEqual(json.loads(self.client.path.read_text())["refresh_token"], "rotated")
        self.assertNotIn("code_verifier",post.call_args.kwargs["data"])

    @patch("keycloak.requests.post")
    def test_temporary_failure_preserves_token(self, post):
        self.seed(); before=self.client.path.read_bytes()
        post.return_value=self.response(503,{"error":"server_error"})
        with self.assertRaises(keycloak.OAuthFailure):self.client.access_token()
        self.assertEqual(self.client.path.read_bytes(),before)

    @patch("keycloak.requests.post")
    def test_invalid_grant_requests_login(self, post):
        self.seed();post.return_value=self.response(400,{"error":"invalid_grant"})
        with self.assertRaises(keycloak.LoginRequired):self.client.access_token()

    @patch("keycloak.requests.post")
    def test_redirect_not_followed(self, post):
        post.return_value=self.response(307,{})
        with self.assertRaises(keycloak.OAuthFailure):self.client._request({})
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_other_client_token_rejected(self):
        self.seed(False);self.client.token["client_id"]="other"
        with self.assertRaises(keycloak.LoginRequired):self.client.access_token()

    def test_missing_token_requests_login(self):
        with self.assertRaises(keycloak.LoginRequired):self.client.access_token()

    @patch("keycloak.requests.post")
    def test_missing_refresh_rejected(self, post):
        post.return_value=self.response(200,{"access_token":"a","expires_in":300})
        with self.assertRaises(keycloak.OAuthFailure):self.client._request({})

    @patch("keycloak.requests.post")
    def test_save_failure_keeps_rotated_token(self, post):
        self.seed();post.return_value=self.response()
        with patch("keycloak.os.replace",side_effect=OSError("disk")):
            with self.assertRaises(OSError):self.client.access_token()
        self.assertEqual(self.client.token["refresh_token"],"rotated")
        self.assertEqual(self.client.access_token(),"access")
        self.assertEqual(post.call_count,1)
        self.assertEqual(json.loads(self.client.path.read_text())["refresh_token"],"rotated")

if __name__ == "__main__":unittest.main()
