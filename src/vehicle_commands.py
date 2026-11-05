"""Explicitly supported optional vehicle controls. No automatic command retries."""
import logging
import requests

BASE = "https://api.volvocars.com/connected-vehicle/v2/vehicles/{}/commands"
DEFINITIONS = {
    "flash_lights": ("FLASH", "flash", "Flash lights", "car-light-high"),
    "honk_horn": ("HONK", "honk", "Honk horn", "bullhorn"),
    "honk_and_flash": ("HONK_AND_FLASH", "honk-flash", "Honk and flash", "bullhorn"),
    "lock_reduced_guard": ("LOCK_REDUCED_GUARD", "lock-reduced-guard", "Lock with reduced guard", "shield-lock-outline"),
}


def discover(session, vin):
    try:
        response = session.get(BASE.format(vin), timeout=20, allow_redirects=False)
        if response.status_code != 200:
            logging.warning("Optional command discovery failed: HTTP %s", response.status_code)
            return []
        payload = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):return []
        supported = {x.get("command") for x in items if isinstance(x, dict) and isinstance(x.get("command"), str)}
        return [{"id": key, "domain": "button", "name": name, "icon": icon,
                 "url": "", "vehicle_command": command}
                for key, (command, endpoint, name, icon) in DEFINITIONS.items() if command in supported]
    except (requests.RequestException, ValueError):
        logging.warning("Optional command discovery unavailable")
        return []


def execute(session, vin, entity_id, allowed_ids):
    if entity_id not in DEFINITIONS or entity_id not in allowed_ids:
        return False
    endpoint = DEFINITIONS[entity_id][1]
    try:
        response = session.post(BASE.format(vin) + "/" + endpoint, json={}, timeout=20, allow_redirects=False)
        if response.status_code != 200:
            logging.warning("Vehicle command %s failed: HTTP %s (not retried)", entity_id, response.status_code)
            return False
        payload = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        status = data.get("invokeStatus") if isinstance(data, dict) else None
        # WAITING/RUNNING means accepted, not proof of physical completion.
        accepted = status in ("WAITING", "RUNNING", "COMPLETED", "SUCCESS")
        logging.info("Vehicle command %s: %s", entity_id, status if status in ("WAITING", "RUNNING", "COMPLETED", "SUCCESS", "FAILED", "UNKNOWN") else "unrecognized response")
        return accepted
    except (requests.RequestException, ValueError):
        logging.warning("Vehicle command %s result unknown (not retried)", entity_id)
        return False
