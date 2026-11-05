"""Interactive configuration; secrets are entered locally, never echoed."""
import getpass
import json
import os
from pathlib import Path
import tempfile
from keycloak import Client


def configure(path):
    config = json.loads(Path(__file__).with_name("settings.json").read_text(encoding="utf-8"))
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
    def ask(label, current="", secret=False):
        current = str(current or "")
        if current.startswith("REPLACE_"):
            current = ""
        if secret:
            answer = getpass.getpass(label + (" [Enter keeps saved value]" if current else "") + ": ")
        else:
            answer = input(label + (" [" + current + "]" if current else "") + ": ").strip()
        return answer or current
    oauth = config.setdefault("oauth", {})
    oauth["client_id"] = ask("New Volvo Client ID", oauth.get("client_id"))
    oauth["client_secret"] = ask("New Volvo Client Secret", oauth.get("client_secret"), True)
    oauth["redirect_uri"] = ask("EXACT callback URI accepted by Volvo", oauth.get("redirect_uri"))
    oauth["scopes"] = ask("Space-separated API scopes allowed by Volvo", oauth.get("scopes"))
    car = config.setdefault("volvoData", {})
    car["vccapikey"] = ask("New Volvo VCC API key", car.get("vccapikey"), True)
    car["vin"] = ask("VIN (empty selects vehicles from account)", car.get("vin"))
    mqtt = config.setdefault("mqtt", {})
    mqtt["broker"] = ask("MQTT broker IP or hostname", mqtt.get("broker"))
    mqtt["port"] = int(ask("MQTT port", mqtt.get("port", 1883)))
    mqtt["username"] = ask("MQTT username", mqtt.get("username"))
    mqtt["password"] = ask("MQTT password", mqtt.get("password"), True)
    config["TZ"] = ask("Timezone", config.get("TZ", "Europe/Helsinki"))
    config["updateInterval"] = int(ask("Update interval, seconds (minimum 60)", config.get("updateInterval", 300)))
    if not car["vccapikey"] or not mqtt["broker"] or not 1 <= mqtt["port"] <= 65535 or config["updateInterval"] < 60:
        raise ValueError("Missing API key/broker or invalid port/update interval")
    Client(oauth, "/data/keycloak-token.json")  # Local validation only; no network calls.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Settings saved with restricted file permissions. No Volvo or MQTT connection was made.")


if __name__ == "__main__":
    import sys
    try:
        configure(Path(sys.argv[1] if len(sys.argv) > 1 else "/setup/settings.json"))
    except (ValueError, OSError, KeyboardInterrupt) as error:
        raise SystemExit("Configuration was not completed: " + str(error)) from None
