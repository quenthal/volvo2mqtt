"""Energy v2 field decoding and capability discovery."""
import requests

FIELDS = {
    "battery_charge_level": "batteryChargeLevel",
    "electric_range": "electricRange",
    "charging_system_status": "chargingStatus",
    "charging_connection_status": "chargerConnectionStatus",
    "estimated_charging_time": "estimatedChargingTimeToTargetBatteryChargeLevel",
    "estimated_charging_finish_time": "estimatedChargingTimeToTargetBatteryChargeLevel",
    "charging_type": "chargingType",
    "charging_power_status": "chargerPowerStatus",
    "charging_current_limit": "chargingCurrentLimit",
    "target_battery_charge_level": "targetBatteryChargeLevel",
    "charging_power": "chargingPower",
}


def value(data, field):
    if not isinstance(data, dict):return None
    data = data.get("data", data)
    if not isinstance(data, dict):return None
    item = data.get(field)
    if not isinstance(item, dict) or item.get("status", "OK") != "OK":return None
    result = item.get("value")
    return result if isinstance(result, (str, int, float)) and not isinstance(result, bool) else None


def capabilities(session, vin):
    try:
        response = session.get("https://api.volvocars.com/energy/v2/vehicles/" + vin + "/capabilities", timeout=20, allow_redirects=False)
        if response.status_code != 200:return set()
        payload = response.json()
        fields = payload.get("getEnergyState", {})
        if not isinstance(fields, dict):return set()
        supported = {k for k, v in fields.items() if isinstance(v, dict) and v.get("isSupported") is True}
        if "chargingSystemStatus" in supported:supported.add("chargingStatus")
        return {key for key, field in FIELDS.items() if field in supported}
    except (requests.RequestException, ValueError, AttributeError):
        return set()
