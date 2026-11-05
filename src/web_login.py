"""Small HTTP origin for Cloudflare Tunnel; OAuth initiation requires a local password."""
import base64
import logging
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
from keycloak import Client, OAuthFailure, LoginRequired

ORIGIN = "https://volvo.konttaniemi.fi"
TERMS = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Volvo home integration - terms</title>
<h1>Personal Volvo home integration</h1><p>This is a private, non-commercial application operated by Eero Konttaniemi for his own vehicle and Home Assistant installation. It is not a public service or an official Volvo product.</p>
<h2>Use and data</h2><p>Vehicle status, battery, charging and location data are used for personal monitoring and home automations. Authorised controls may operate supported vehicle functions. Data is processed by the owner's self-hosted service and Home Assistant. OAuth credentials are stored on the owner's server. Cloudflare provides the HTTPS tunnel for this website and the OAuth callback.</p>
<h2>Access and retention</h2><p>Only the owner is authorised to use the application. Data is retained according to the owner's Home Assistant and backup settings. Access can be revoked through Volvo; locally retained data and backups must be removed separately.</p>
<h2>Limitations</h2><p>Availability and accuracy depend on Volvo's APIs and the owner's systems. The application must not be relied on as a safety system. No availability guarantee is provided.</p></html>"""

class Service:
    def __init__(self, password, client=None):
        if len(password) < 24:
            raise ValueError("WEB_LOGIN_PASSWORD must contain at least 24 characters")
        self.password, self.client = password, client
        self.pending = {}
        self.lock = threading.RLock()

    def get_client(self):
        with self.lock:
            if self.client is None:
                path = Path(os.environ.get("WEB_SETTINGS_PATH", "/setup/settings.json"))
                config = json.loads(path.read_text())
                if config.get("oauth", {}).get("redirect_uri") != ORIGIN + "/oauth/callback":
                    raise ValueError("Callback must match public origin")
                self.client = Client(config["oauth"], os.environ.get("TOKEN_PATH", "/data/keycloak-token.json"))
            return self.client

    def handler(self):
        service = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log callback codes, cookies or request URLs.

            def reply(self, status, body, headers=None):
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "same-origin" if urlsplit(self.path).path == "/login" else "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self' https://auth.volvocars.com; frame-ancestors 'none'")
                for key,value in (headers or {}).items():self.send_header(key,value)
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                expected = "Basic " + base64.b64encode(("admin:"+service.password).encode()).decode()
                return secrets.compare_digest(self.headers.get("Authorization", ""), expected)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ("/", "/terms"):
                    return self.reply(200, TERMS)
                if path == "/health":return self.reply(200,"ready")
                if path == "/login":
                    if not self.authorized():
                        return self.reply(401,"Authentication required",{"WWW-Authenticate":'Basic realm="Volvo login"'})
                    nonce=secrets.token_urlsafe(32)
                    with service.lock:
                        service.pending={k:v for k,v in service.pending.items() if v[0]>time.monotonic()}
                        if len(service.pending)>100:service.pending.clear()
                        service.pending[nonce]=(time.monotonic()+600,None,None)
                    return self.reply(200,'<form method="post" action="/login"><input type="hidden" name="nonce" value="'+nonce+'"><button>Sign in with Volvo</button></form>',
                        {"Set-Cookie":"volvo_login="+nonce+"; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=600"})
                if path == "/oauth/callback":
                    try:
                        cookie=SimpleCookie(self.headers.get("Cookie", ""))
                        nonce=cookie["volvo_login"].value
                        q=parse_qs(urlsplit(self.path).query)
                        with service.lock:
                            entry=service.pending.pop(nonce,None)
                        if not entry or entry[0]<time.monotonic() or not entry[1] or q.get("state") != [entry[2]]:
                            return self.reply(400,"Login expired or invalid. Start again at /login.")
                        service.get_client().exchange(ORIGIN+self.path,entry[1],entry[2])
                    except FileNotFoundError:
                        return self.reply(503,"Application settings are missing. Complete setup_keycloak.py in the LXC console, then start again at /login.")
                    except (OAuthFailure, LoginRequired) as error:
                        # These exception messages are fixed locally; no provider response bodies.
                        logging.warning("OAuth callback failed: %s", str(error))
                        return self.reply(400,"Login failed: " + escape(str(error)) + ". Start again at /login.")
                    except (KeyError,ValueError,OSError) as error:
                        logging.warning("OAuth callback failed: %s", type(error).__name__)
                        return self.reply(400,"Login failed due to local settings or storage. Start again at /login.")
                    return self.reply(200,"Volvo login saved. You may close this page.",{"Set-Cookie":"volvo_login=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"})
                self.reply(404,"Not found")

            def do_POST(self):
                if self.path != "/login" or not self.authorized():return self.reply(403,"Forbidden")
                origin = self.headers.get("Origin")
                # Some browsers suppress Origin on form POSTs under a restrictive
                # referrer policy. Only accept that case with same-origin Fetch
                # Metadata; the session cookie and one-time form nonce remain mandatory.
                if origin != ORIGIN and not (
                    origin in (None, "null") and self.headers.get("Sec-Fetch-Site") == "same-origin"
                ):
                    return self.reply(403,"Invalid origin. Open https://volvo.konttaniemi.fi/login again.")
                try:
                    length=int(self.headers.get("Content-Length","0"))
                    if not 0<length<1024:raise ValueError()
                    form=parse_qs(self.rfile.read(length).decode())
                    cookie=SimpleCookie(self.headers.get("Cookie", ""))
                    nonce=cookie["volvo_login"].value
                    if form.get("nonce") != [nonce]:raise ValueError()
                    with service.lock:
                        entry=service.pending.get(nonce)
                        if not entry or entry[0]<time.monotonic() or entry[1]:raise ValueError()
                        url,verifier,state=service.get_client().begin()
                        service.pending[nonce]=(time.monotonic()+600,verifier,state)
                    return self.reply(303,"Continue to Volvo",{"Location":url})
                except FileNotFoundError:
                    return self.reply(503,"Application settings are missing. Complete setup_keycloak.py in the LXC console, then start again at /login.")
                except (KeyError,ValueError,OSError):
                    return self.reply(400,"Settings invalid or login expired. Configure the application and try again.")
        return Handler


def start(client=None):
    service=Service(Path(os.environ.get("WEB_PASSWORD_FILE","/run/secrets/web_password")).read_text().strip(),client)
    server=ThreadingHTTPServer(("0.0.0.0",8080),service.handler())
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server

if __name__ == "__main__":
    server=start()
    try:
        while True:time.sleep(3600)
    except KeyboardInterrupt:server.shutdown()
