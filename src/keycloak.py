"""Volvo confidential-client OAuth with PKCE and persistent rotating tokens."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit, parse_qs
import requests

AUTH_URL = "https://auth.volvocars.com/realms/volvocars/protocol/openid-connect/auth"
TOKEN_URL = "https://auth.volvocars.com/realms/volvocars/protocol/openid-connect/token"

class LoginRequired(Exception):
    pass

class OAuthFailure(Exception):
    pass

class Client:
    def __init__(self, config, path):
        self.config = config
        self.path = Path(path)
        self.lock = threading.RLock()
        self.token = None
        self.dirty = False
        for key in ("client_id", "client_secret", "redirect_uri", "scopes"):
            if not config.get(key):
                raise ValueError("Missing oauth setting: " + key)
        uri = urlsplit(config["redirect_uri"])
        if uri.scheme not in ("http", "https") or not uri.netloc or uri.fragment or uri.query or uri.username:
            raise ValueError("Use a registered HTTP(S) callback without query or fragment")
        if uri.scheme == "http" and uri.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Non-loopback callbacks require HTTPS")

    def begin(self):
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        scopes = self.config["scopes"]
        if isinstance(scopes, str):
            scopes = scopes.split()
        base_scopes = ["openid"]
        if self.config.get("request_offline_access", True):
            base_scopes.append("offline_access")
        scopes = list(dict.fromkeys([*base_scopes, *scopes]))
        return AUTH_URL + "?" + urlencode(dict(response_type="code", client_id=self.config["client_id"],
            redirect_uri=self.config["redirect_uri"], scope=" ".join(scopes), state=state,
            code_challenge=challenge, code_challenge_method="S256")), verifier, state

    def exchange(self, callback, verifier, state):
        actual, expected = urlsplit(callback), urlsplit(self.config["redirect_uri"])
        if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path) or actual.fragment:
            raise OAuthFailure("Callback address mismatch")
        query = parse_qs(actual.query)
        if len(query.get("state", [])) != 1 or not secrets.compare_digest(query["state"][0], state):
            raise OAuthFailure("OAuth state mismatch")
        if "error" in query:
            raise OAuthFailure("Authorisation denied; start login again")
        if len(query.get("code", [])) != 1:
            raise OAuthFailure("Missing or duplicate authorisation code")
        with self.lock:
            self._save(self._request(dict(grant_type="authorization_code", code=query["code"][0],
                redirect_uri=self.config["redirect_uri"], code_verifier=verifier)))

    def _request(self, data):
        try:
            response = requests.post(TOKEN_URL, data=data,
                auth=(self.config["client_id"], self.config["client_secret"]),
                headers={"Accept": "application/json"}, timeout=30, allow_redirects=False)
        except requests.RequestException:
            raise OAuthFailure("Token service connection failed; will retry") from None
        try:
            payload = response.json()
        except ValueError:
            raise OAuthFailure("Token service returned invalid JSON") from None
        if response.status_code != 200:
            error = payload.get("error") if isinstance(payload, dict) else None
            if error == "invalid_grant":
                raise LoginRequired("Volvo authorisation expired or was revoked")
            if error in ("invalid_client", "unauthorized_client"):
                raise OAuthFailure("Check the new application's client credentials")
            raise OAuthFailure("Token service failed (HTTP %s)" % response.status_code)
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
            raise OAuthFailure("Invalid token response")
        try:
            lifetime = float(payload["expires_in"])
            if not 30 < lifetime < 31536000:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise OAuthFailure("Invalid token lifetime") from None
        if not payload.get("refresh_token"):
            raise OAuthFailure("Missing refresh token; check offline_access and application permissions")
        return dict(access_token=payload["access_token"], refresh_token=payload["refresh_token"],
            expires_at=time.time()+lifetime-30, client_id=self.config["client_id"], issuer=TOKEN_URL)

    def _save(self, token):
        # Preserve the rotated token in memory even if disk storage fails.
        self.token = token
        self.dirty = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".oauth-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(token, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self.dirty = False
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def access_token(self):
        with self.lock:
            if self.dirty:
                self._save(self.token)
            if self.token is None:
                try:
                    self.token = json.loads(self.path.read_text(encoding="utf-8"))
                except (FileNotFoundError, ValueError):
                    raise LoginRequired("Run the browser login command") from None
            token = self.token
            if not isinstance(token, dict) or token.get("client_id") != self.config["client_id"] or token.get("issuer") != TOKEN_URL:
                self.token = None
                raise LoginRequired("Token belongs to a different application or service")
            try:
                valid = float(token["expires_at"]) > time.time()
                if not token["access_token"] or not token["refresh_token"]:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                self.token = None
                raise LoginRequired("Invalid saved token; log in again") from None
            if not valid:
                try:
                    self._save(self._request(dict(grant_type="refresh_token", refresh_token=token["refresh_token"])))
                except LoginRequired:
                    self.token = None
                    raise
            return self.token["access_token"]


def configured_client():
    from config import settings
    return Client(settings.get("oauth", {}), os.environ.get("TOKEN_PATH", "/data/keycloak-token.json"))


def login():
    client = configured_client()
    url, verifier, state = client.begin()
    print("Open this URL in your browser and sign in to Volvo:\n" + url)
    print("After redirect, copy the COMPLETE callback URL from the address bar.")
    print("A loopback callback may show connection refused; the address still contains the code.")
    from getpass import getpass
    callback = getpass("Callback URL (hidden): ").strip()
    client.exchange(callback, verifier, state)
    print("Login saved. Start or restart volvo2mqtt.")

if __name__ == "__main__":
    try:
        login()
    except (OAuthFailure, LoginRequired, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
