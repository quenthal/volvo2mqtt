import json
import logging
import requests
import mqtt
import keycloak
import vehicle_commands
import energy
import polling
import util
import time
import re
import os.path
from threading import current_thread, Thread
from datetime import datetime, timedelta
from config import settings
from babel.dates import format_datetime
from json import JSONDecodeError
from const import charging_system_states, charging_connection_states, door_states, window_states, \
    VEHICLES_URL, VEHICLE_DETAILS_URL, RECHARGE_STATE_URL, CLIMATE_START_URL, \
    WINDOWS_STATE_URL, LOCK_STATE_URL, TYRE_STATE_URL, supported_entities, FUEL_BATTERY_STATE_URL, \
    STATISTICS_URL, ENGINE_DIAGNOSTICS_URL, VEHICLE_DIAGNOSTICS_URL, API_BACKEND_STATUS, WARNINGS_URL, engine_states, \
    otp_max_loops

session = polling.CountingSession()
endpoint_cache = polling.EndpointCache()
session.headers = {
    "vcc-api-key": "",
    "content-type": "application/json",
    "accept": "*/*"
}

token_expires_at: datetime
refresh_token = None
vins = []
supported_endpoints = {}
cached_requests = {}
vcc_api_keys = []
backend_status = ""


auth_client = None


def refresh_auth():
    global auth_client, token_expires_at
    if auth_client is None:
        auth_client = keycloak.configured_client()
    try:
        access_token = auth_client.access_token()
    except keycloak.LoginRequired:
        mqtt.set_login_required(True)
        raise
    session.headers.update({"authorization": "Bearer " + access_token})
    token_expires_at = datetime.fromtimestamp(auth_client.token["expires_at"], util.TZ)
    mqtt.set_login_required(False)
    return True


def authorize(renew_tokenfile=False):
    refresh_auth()
    get_vcc_api_keys()
    get_vehicles()
    check_supported_endpoints()
    Thread(target=backend_status_loop, name="backend_status_thread", daemon=True).start()


def get_vehicles():
    global vins
    vins = []
    if not settings.volvoData["vin"]:
        vehicles = session.get(VEHICLES_URL)
        data = vehicles.json()
        if vehicles.status_code == 200:
            if len(data["data"]) > 0:
                for vehicle in data["data"]:
                    vins.append(vehicle["vin"])
            else:
                raise Exception("No vehicle found in this Volvo account.")
        else:
            error = vehicles.json()
            raise Exception(
                "Error getting vehicles: " + str(vehicles.status_code) + ". " + str(error["error"].get("message")))
    else:
        if isinstance(settings.volvoData["vin"], list):
            # If setting is a list, copy
            vins = settings.volvoData["vin"]
        else:
            # If setting is a string, append to list
            vins.append(settings.volvoData["vin"])

    if len(vins) == 0:
        raise Exception("No vehicle found, exiting application!")
    else:
        initialize_climate(vins)
        initialize_scheduler(vins)
        mqtt.delete_old_entities()
        logging.info("Vin: " + str(vins) + " found!")


def get_vcc_api_keys(used_key=None):
    working_keys = None

    while not working_keys:
        setting_keys = settings.volvoData["vccapikey"]
        if isinstance(setting_keys, str):
            set_key_state(setting_keys)
        elif isinstance(setting_keys, list):
            for key in setting_keys:
                set_key_state(key)

        logging.debug("VCC API key status checked")
        working_keys = [key["key"] for key in vcc_api_keys if not key.get("extended") and key.get('key') != used_key]
        if len(working_keys) < 1:
            used_key = None
            logging.warning("No working VCCAPIKEY found, waiting 10 minutes. Then trying again!")
            mqtt.send_offline()
            time.sleep(600)
        else:
            mqtt.send_heartbeat()
            session.headers.update({"vcc-api-key": working_keys[0]})
            logging.info("Using a configured VCC API key")
            for key_dict in vcc_api_keys:
                if key_dict["key"] == working_keys[0]:
                    key_dict["in_use"] = True

        logging.debug("VCC API key status checked")


def set_key_state(key):
    global vcc_api_keys
    list_index = next((index for (index, d) in enumerate(vcc_api_keys) if d["key"] == key), None)

    if list_index or list_index == 0:
        extended, extended_until = check_vcc_api_key(key, vcc_api_keys[list_index]["extended_until"])
        vcc_api_keys[list_index] = ({"key": key, "extended": extended,
                                     "extended_until": extended_until, "in_use": False})
    else:
        extended, extended_until = check_vcc_api_key(key)
        vcc_api_keys.append({"key": key, "extended": extended,
                             "extended_until": extended_until, "in_use": False})


def check_vcc_api_key(test_key, extended_until=None):
    if extended_until:
        if extended_until >= datetime.now():
            logging.warning("VCC API key quota exhausted; reusable at: "
                            + format_datetime(extended_until, format="medium", locale=settings["babelLocale"]))
            return True, extended_until

    refresh_auth()

    token = session.headers.get("authorization")
    headers = {
        "vcc-api-key": test_key,
        "content-type": "application/json",
        "accept": "*/*",
        "authorization": token
    }

    response = session.get(VEHICLES_URL, headers=headers, timeout=20)
    data = response.json()
    if response.status_code == 200:
        logging.debug("VCC API key works")
        return False, None
    elif response.status_code == 403 and "message" in data:
        if "Out of call volume quota" in data["message"]:
            reuse_search = re.search(r"\d{2}\:\d{2}\:\d{2}", data["message"])
            if reuse_search:
                reusable_in = reuse_search.group(0).split(":")
                now = datetime.now()
                extended_until = now + timedelta(hours=int(reusable_in[0]),
                                                 minutes=int(reusable_in[1]),
                                                 seconds=int(reusable_in[2]) + 10)
                logging.warning("VCC API key quota exhausted; reusable at: "
                                + format_datetime(extended_until, format="medium", locale=settings["babelLocale"]))
        else:
            if "error" in data:
                logging.warning("Configured VCC API key failed: " + data["error"]["message"])
            else:
                logging.warning("Configured VCC API key failed. Statuscode " + str(response.status_code) +
                                " Message: " + response.text)
    else:
        if "error" in data:
            logging.warning("Configured VCC API key failed: " + data["error"]["message"])
        else:
            logging.warning("Configured VCC API key failed. Statuscode " + str(response.status_code) +
                            " Message: " + response.text)
    return True, extended_until


def change_vcc_api_key():
    used_vcc_api_key = session.headers.get("vcc-api-key")
    get_vcc_api_keys(used_vcc_api_key)


def get_vehicle_details(vin):
    response = session.get(VEHICLE_DETAILS_URL.format(vin), timeout=15)
    if response.status_code == 200:
        data = response.json()["data"]
        logging.debug(response.text)
        device = {
            "identifiers": [f"volvoAAOS2mqtt_{vin}"],
            "manufacturer": "Volvo",
            "model": data['descriptions']['model'],
            "name": f"{data['descriptions']['model']} ({data['modelYear']}) - {vin}",
        }
        mqtt.send_car_images(vin, data, device)
    elif response.status_code == 500 and not settings.volvoData["vin"]:
        # Workaround for some cars that are not returning vehicle details
        device = {
            "identifiers": [f"volvoAAOS2mqtt_{vin}"],
            "manufacturer": "Volvo",
            "model": vin,
            "name": f"Volvo - {vin}",
        }
    else:
        raise Exception("Getting vehicle details failed. Status Code: " + str(response.status_code) +
                        ". Error: " + response.text)

    return device


def check_supported_endpoints():
    global supported_endpoints
    for vin in vins:
        supported_endpoints[vin] = []
        refresh_auth()
        energy_supported = energy.capabilities(session, vin)
        for entity in supported_entities:
            if entity["id"] == "battery_charge_level" and entity["url"] == FUEL_BATTERY_STATE_URL \
                    and any("battery_charge_level" in d["id"] for d in supported_endpoints[vin]):
                # If battery charge level could be found in recharge-api, skip the second battery charge sensor
                continue

            if entity.get("url") == RECHARGE_STATE_URL and entity["id"] in energy_supported:
                supported_endpoints[vin].append(entity)
                continue
            if entity.get('url'):
                state = api_call(entity["url"], "GET", vin, entity["id"])
            else:
                state = ""

            if state is not None:
                logging.info("Success! " + entity["name"] + " is supported by your vehicle.")
                supported_endpoints[vin].append(entity)
            else:
                logging.info("Failed, " + entity["name"] + " is unfortunately not supported by your vehicle.")

        refresh_auth()
        supported_endpoints[vin].extend(vehicle_commands.discover(session, vin))


def execute_optional_command(vin, entity_id):
    allowed = {entity["id"] for entity in supported_endpoints.get(vin, []) if entity.get("vehicle_command")}
    if vin not in vins or entity_id not in allowed:
        return False
    refresh_auth()
    return vehicle_commands.execute(session, vin, entity_id, allowed)


def initialize_scheduler(vins):
    for vin in vins:
        topic = f"homeassistant/schedule/{vin}/command"
        mqtt.active_schedules[vin] = {"timers": []}
        mqtt.subscribed_topics = [topic]
        mqtt.mqtt_client.subscribe(topic)


def initialize_climate(vins):
    for vin in vins:
        mqtt.assumed_climate_state[vin] = "OFF"


def disable_climate(vin):
    mqtt.clear_climate_assumption(vin)


def check_lock_status(vin, old_state):
    target = "UNLOCKED" if old_state == "LOCKED" else "LOCKED"
    state = None
    for _ in range(3):
        time.sleep(10)
        state = api_call(LOCK_STATE_URL,"GET",vin,"lock_status",True)
        if state is not None:mqtt.publish_local_state(vin,"lock_status",state)
        if state == target:return True
    logging.warning("Lock target was not confirmed within bounded polling")
    return False


def check_engine_status(vin):
    # Compatibility stub: normal polling now observes driving state.
    return None


def backend_status_loop():
    logging.debug("backend_status_loop started")
    while True:
        last_status = get_backend_status()
        # Update every hour when not error, 10 minutes when got server error
        time.sleep(3600 if not last_status.startswith("APIERR_") else 600)


def get_backend_status():
    global backend_status
    try:
        response = session.get(API_BACKEND_STATUS, timeout=15)
        try:
            data = util.drop_null_fields(response.json())
            if util.keys_exists(data, "message"):
                if data["message"]:
                    backend_status = data["message"]
                else:
                    backend_status = "NO_WARNING"
            else:
                backend_status = "NO_WARNING"

        except JSONDecodeError as e:
            logging.error(f"Invalid json response from API Backend Status - {response} - {e}")
            backend_status = "APIERR_INVALID_JSON_RESPONSE"

    except requests.exceptions.ReadTimeout as e:
        logging.error(f"API Backend Status - timeout - {e}")
        backend_status = "APIERR_TIMEOUT"
    except requests.exceptions.RequestException as e:
        logging.error(f"API Backend Status - Error - {e}")
        backend_status = "APIERR_REQUEST_ERROR"

    return backend_status


def api_call(url, method, vin, sensor_id=None, force_update=False, key_change=False):
    refresh_auth()

    if method == "GET":
        response = cached_request(url, method, vin, force_update, key_change)
        if response is None:return None
    elif method == "POST":
        logging.debug(f"Starting {method} call against {url}")
        try:
            response = session.post(url.format(vin), json={}, timeout=20, allow_redirects=False)
        except requests.exceptions.RequestException as e:
            logging.error(f"Error getting data from {url} : {e}")
            return None
    else:
        logging.error(f"Unkown method posted: {method}. Returning nothing")
        return None

    logging.debug(f"Response status code: {response.status_code}")
    try:
        data = util.drop_null_fields(response.json())
    except JSONDecodeError as e:
        logging.error(f"Fetched json decode error, Volvo API seems to return garbage. Skipping update. Error: {e}")
        return None

    if method == "POST":
        if response.status_code != 200:
            logging.warning("Vehicle command rejected: HTTP %s (not retried)", response.status_code)
            return False
        result = data.get("data", data) if isinstance(data, dict) else {}
        status = result.get("invokeStatus") if isinstance(result, dict) else None
        accepted = status in ("WAITING", "RUNNING", "COMPLETED", "SUCCESS")
        logging.info("Vehicle command acceptance: %s", status if accepted else "not confirmed")
        return accepted

    if response.status_code == 200:
        logging.debug(response.text)
        try:
            parsed = parse_api_data(data, sensor_id)
            if sensor_id == "engine_state" and parsed == "ON":
                mqtt.clear_climate_assumption(vin)
            return parsed
        except (KeyError, TypeError, ValueError, IndexError):
            logging.warning("Skipping malformed API data for sensor %s", sensor_id)
            return None
    else:
        logging.debug(response.text)
        if url == CLIMATE_START_URL and response.status_code == 503:
            logging.warning("Car in use, cannot start pre climatization")
            mqtt.assumed_climate_state[vin] = "OFF"
            mqtt.update_car_data()
        elif "extended-vehicle" in url and response.status_code == 403:
            # Suppress 403 errors for unsupported extended-vehicle api cars
            logging.debug("Suppressed 403 for extended-vehicle API")
            return None
        elif response.status_code == 403 and "message" in data:
            if "Out of call volume quota" in data["message"]:
                logging.warning("Quota extended. Try to change VCCAPIKEY!")
                change_vcc_api_key()
                api_call(url, method, vin, sensor_id, force_update, True)
            else:
                logging.error(
                    f"API Call failed to {url}. Status Code: {response.status_code}. Error: {response.text}")
        else:
            logging.error(f"API Call failed to {url}. Status Code: {response.status_code}. Error: {response.text}")
        return None


def cached_request(url, method, vin, force_update=False, key_change=False):
    return endpoint_cache.get(session,url,vin,settings.get("updateInterval",300),force_update or key_change)


def parse_api_data(data, sensor_id=None):
    if sensor_id in energy.FIELDS:
        field = energy.FIELDS[sensor_id]
        result = energy.value(data, field)
        if sensor_id == "electric_range":
            return util.convert_metric_values(result) if result is not None else None
        if sensor_id == "charging_system_status":
            return charging_system_states.get(result, result)
        if sensor_id == "charging_connection_status":
            return charging_connection_states.get(result, result)
        if sensor_id in ("estimated_charging_time", "estimated_charging_finish_time"):
            status = energy.value(data, "chargingStatus")
            if status is None:return None
            if status != "CHARGING":
                return 0 if sensor_id == "estimated_charging_time" else ""
            if result is None:return None
            minutes = float(result)
            if minutes < 0:return None
            if sensor_id == "estimated_charging_time":return minutes
            return format_datetime(datetime.now(util.TZ) + timedelta(minutes=minutes), format="medium", locale=settings["babelLocale"])
        if sensor_id == "charging_power" and result is not None:
            item = data.get("chargingPower", {})
            unit = item.get("unit")
            if unit in ("kW", "kilowatt"):return float(result) * 1000
            if unit not in ("W", "watt", None):return None
        return result
    else:
        if sensor_id != "api_backend_status":
            data = data["data"]

        if sensor_id == "lock_status":
            return data["centralLock"]["value"] if util.keys_exists(data, "centralLock") and data["centralLock"]["value"] is not None else None
        elif sensor_id == "battery_capacity":
            return data["batteryCapacityKWH"] if util.keys_exists(data, "batteryCapacityKWH") else None
        elif sensor_id == "odometer":
            return util.convert_metric_values(int(data["odometer"]["value"])) \
                if util.keys_exists(data, "odometer") and data["odometer"]["value"] is not None else None
        elif sensor_id == "window_front_left":
            return window_states[data["frontLeftWindow"]["value"]] if util.keys_exists(data, "frontLeftWindow") and data["frontLeftWindow"]["value"] is not None \
                else None
        elif sensor_id == "window_front_right":
            return window_states[data["frontRightWindow"]["value"]] if util.keys_exists(data, "frontRightWindow") and data["frontRightWindow"]["value"] is not None \
                else None
        elif sensor_id == "window_rear_left":
            return window_states[data["rearLeftWindow"]["value"]] if util.keys_exists(data, "rearLeftWindow") and data["rearLeftWindow"]["value"] is not None \
                else None
        elif sensor_id == "window_rear_right":
            return window_states[data["rearRightWindow"]["value"]] if util.keys_exists(data, "rearRightWindow") and data["rearRightWindow"]["value"] is not None \
                else None
        elif sensor_id == "door_front_left":
            return door_states[data["frontLeftDoor"]["value"]] if util.keys_exists(data, "frontLeftDoor") and data["frontLeftDoor"]["value"] is not None else None
        elif sensor_id == "door_front_right":
            return door_states[data["frontRightDoor"]["value"]] \
                if util.keys_exists(data, "frontRightDoor") and data["frontRightDoor"]["value"] is not None else None
        elif sensor_id == "door_rear_left":
            return door_states[data["rearLeftDoor"]["value"]] if util.keys_exists(data, "rearLeftDoor") and data["rearLeftDoor"]["value"] is not None else None
        elif sensor_id == "door_rear_right":
            return door_states[data["rearRightDoor"]["value"]] if util.keys_exists(data, "rearRightDoor") and data["rearRightDoor"]["value"] is not None else None
        elif sensor_id == "tailgate":
            return door_states[data["tailgate"]["value"]] if util.keys_exists(data, "tailgate") and data["tailgate"]["value"] is not None else None
        elif sensor_id == "sunroof":
            return door_states[data["sunroof"]["value"]] if util.keys_exists(data, "sunroof") and data["sunroof"]["value"] is not None else None
        elif sensor_id == "engine_hood":
            return door_states[data["hood"]["value"]] if util.keys_exists(data, "hood") and data["hood"]["value"] is not None else None
        elif sensor_id == "tank_lid":
            return door_states[data["tankLid"]["value"]] if util.keys_exists(data, "tankLid") and data["tankLid"]["value"] is not None else None
        elif sensor_id == "tyre_front_left":
            return data["frontLeft"]["value"] if util.keys_exists(data, "frontLeft") and data["frontLeft"]["value"] is not None else None
        elif sensor_id == "tyre_front_right":
            return data["frontRight"]["value"] if util.keys_exists(data, "frontRight") and data["frontRight"]["value"] is not None else None
        elif sensor_id == "tyre_rear_left":
            return data["rearLeft"]["value"] if util.keys_exists(data, "rearLeft") and data["rearLeft"]["value"] is not None else None
        elif sensor_id == "tyre_rear_right":
            return data["rearRight"]["value"] if util.keys_exists(data, "rearRight") and data["rearRight"]["value"] is not None else None
        elif sensor_id == "engine_state":
            return engine_states[data["engineStatus"]["value"]] if util.keys_exists(data, "engineStatus") and data["engineStatus"]["value"] is not None else None
        elif sensor_id == "fuel_level":
            if util.keys_exists(data, "fuelAmount") and data["fuelAmount"]["value"] is not None:
                fuel_amount = float(data["fuelAmount"]["value"])
                if fuel_amount > 0:
                    return fuel_amount
            return None
        elif sensor_id == "average_fuel_consumption":
            average_fuel_con = 0
            if util.keys_exists(data, "averageFuelConsumption") and data["averageFuelConsumption"]["value"] is not None:
                average_fuel_con = float(data["averageFuelConsumption"]["value"])
            elif util.keys_exists(data, "averageFuelConsumptionAutomatic") and data["averageFuelConsumptionAutomatic"]["value"] is not None:
                average_fuel_con = float(data["averageFuelConsumptionAutomatic"]["value"])

            if average_fuel_con > 0:
                return average_fuel_con
            return None
        elif sensor_id == "average_speed":
            average_speed = 0
            if util.keys_exists(data, "averageSpeed") and data["averageSpeed"]["value"] is not None:
                average_speed = float(data["averageSpeed"]["value"])
            elif util.keys_exists(data, "averageSpeedAutomatic") and data["averageSpeedAutomatic"]["value"] is not None:
                average_speed = float(data["averageSpeedAutomatic"]["value"])

            if average_speed != 0:
                return util.convert_metric_values(average_speed)

            return None
        elif sensor_id == "location":
            coordinates = {}
            if util.keys_exists(data, "geometry"):
                raw_data = data["geometry"]
                if util.keys_exists(raw_data, "coordinates"):
                    coordinates = {"longitude": raw_data["coordinates"][0],
                                   "latitude": raw_data["coordinates"][1],
                                   "gps_accuracy": 1}
            return coordinates
        elif sensor_id == "distance_to_empty_tank":
            if util.keys_exists(data, "distanceToEmptyTank") and data["distanceToEmptyTank"]["value"] is not None:
                distance_to_empty = int(data["distanceToEmptyTank"]["value"])
                if distance_to_empty > 0:
                    return util.convert_metric_values(data["distanceToEmptyTank"]["value"])
            return None
        elif sensor_id == "distance_to_empty_battery":
            if util.keys_exists(data, "distanceToEmptyBattery") and data["distanceToEmptyBattery"]["value"] is not None:
                distance_to_empty = int(data["distanceToEmptyBattery"]["value"])
                if distance_to_empty > 0:
                    return util.convert_metric_values(data["distanceToEmptyBattery"]["value"])
            return None
        elif sensor_id == "hours_to_service":
            return data["engineHoursToService"]["value"] if util.keys_exists(data, "engineHoursToService") and data["engineHoursToService"]["value"] is not None else None
        elif sensor_id == "km_to_service":
            if util.keys_exists(data, "distanceToService") and data["distanceToService"]["value"] is not None:
                km_to_service = int(data["distanceToService"]["value"])
                if km_to_service > 0:
                    return util.convert_metric_values(data["distanceToService"]["value"])
            return None
        elif sensor_id == "time_to_service":
            if util.keys_exists(data, "timeToService") and data["timeToService"]["value"] is not None:
                return str(data["timeToService"]["value"]) + " " + data["timeToService"]["unit"]
            else:
                return None
        elif sensor_id == "service_warning_status":
            return data["serviceWarning"]["value"] if util.keys_exists(data, "serviceWarning") and data["serviceWarning"]["value"] is not None else None
        elif sensor_id == "average_energy_consumption":
            average_energy_con = 0
            if util.keys_exists(data, "averageEnergyConsumption") and data["averageEnergyConsumption"]["value"] is not None:
                average_energy_con = data["averageEnergyConsumption"]["value"]
            elif util.keys_exists(data, "averageEnergyConsumptionAutomatic") and data["averageEnergyConsumptionAutomatic"]["value"] is not None:
                average_energy_con = data["averageEnergyConsumptionAutomatic"]["value"]

            if average_energy_con != 0:
                return average_energy_con
            return None
        elif sensor_id == "washer_fluid_warning":
            return data["washerFluidLevelWarning"]["value"] if util.keys_exists(data, "washerFluidLevelWarning") and data["washerFluidLevelWarning"]["value"] is not None else None
        elif sensor_id == "warnings":
            warnings = 0
            cleaned_data = {}
            for key, dicts in data.items():
                contains_data = sum(value == "NO_WARNING" or value == "FAILURE" for value in dicts.values())
                if contains_data:
                    warnings = warnings + 1
                    cleaned_data[key] = dicts["value"]

            return cleaned_data if warnings > 0 else None
        elif sensor_id == "trip_fuel_consumption":
            trip_fuel_con = 0
            if util.keys_exists(data, "averageFuelConsumptionAutomatic") and data["averageFuelConsumptionAutomatic"]["value"] is not None:
                trip_fuel_con = float(data["averageFuelConsumptionAutomatic"]["value"])
            if trip_fuel_con > 0:
                return trip_fuel_con
            return None
        elif sensor_id == "trip_distance":
            return util.convert_metric_values(int(data["tripMeterAutomatic"]["value"])) \
                if util.keys_exists(data, "tripMeterAutomatic") and data["tripMeterAutomatic"]["value"] is not None else None
        elif sensor_id == "trip_energy_consumption":
            trip_energy_con = 0
            if util.keys_exists(data, "averageEnergyConsumptionAutomatic") and data["averageEnergyConsumptionAutomatic"]["value"] is not None:
                trip_energy_con = data["averageEnergyConsumptionAutomatic"]["value"]
            if trip_energy_con != 0:
                return trip_energy_con
            return None
        elif sensor_id == "trip_speed":
            trip_speed = 0
            if util.keys_exists(data, "averageSpeedAutomatic") and data["averageSpeedAutomatic"]["value"] is not None:
                trip_speed = float(data["averageSpeedAutomatic"]["value"])
            if trip_speed != 0:
                return util.convert_metric_values(trip_speed)
            return None
        else:
            return None


