"""Explicitly supported optional vehicle controls. No automatic command retries."""
import logging
import uuid
import requests

ACCEPTED = ("WAITING", "RUNNING", "COMPLETED", "SUCCESS")

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


def operation_id():
    # Optional vcc-api-operationId header; lets Volvo support trace a single command.
    return str(uuid.uuid4())


def log_result(command, response, payload, op_id):
    """Log one command outcome with its operation id. Acceptance is not proof of physical completion."""
    if response.status_code != 200:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        detail = error.get("description") or error.get("message") if isinstance(error, dict) else None
        logging.warning("Vehicle command %s rejected: HTTP %s %s (operationId %s, not retried)",
                        command, response.status_code, str(detail or "")[:120], op_id)
        return False
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    status = data.get("invokeStatus") if isinstance(data, dict) else None
    message = data.get("message") if isinstance(data, dict) else None
    logging.info("Vehicle command %s: %s message=%r (operationId %s)", command,
                 status if isinstance(status, str) else "unrecognized response", str(message or "")[:120], op_id)
    return status in ACCEPTED


def execute(session, vin, entity_id, allowed_ids):
    if entity_id not in DEFINITIONS or entity_id not in allowed_ids:
        return False
    endpoint = DEFINITIONS[entity_id][1]
    op_id = operation_id()
    try:
        response = session.post(BASE.format(vin) + "/" + endpoint, json={}, timeout=20, allow_redirects=False,
                                headers={"vcc-api-operationId": op_id})
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return log_result(endpoint, response, payload, op_id)
    except requests.RequestException:
        logging.warning("Vehicle command %s result unknown (operationId %s, not retried)", endpoint, op_id)
        return False
