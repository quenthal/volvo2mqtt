import logging
import time
import paho.mqtt.client as mqtt
import json
import volvo
import util
import os
import requests
import threading
from control_queue import ControlQueue
from threading import Thread, Timer
from datetime import datetime
from babel.dates import format_datetime
from config import settings
from const import CLIMATE_START_URL, CLIMATE_STOP_URL, CAR_LOCK_URL, \
    CAR_UNLOCK_URL, availability_topic, icon_states, old_entity_ids, otp_mqtt_topic

mqtt_client: mqtt.Client
subscribed_topics = []
assumed_climate_state = {}
last_data_update = None
climate_timer = {}
engine_status = {}
devices = {}
active_schedules = {}
otp_code = None
control_queue = ControlQueue()
update_lock = threading.Lock()
climate_state_lock = threading.RLock()
climate_generation = {}
pending_climate = {}
climate_request_generation = {}
published_configs = {}
async_refresh_lock = threading.Lock()

def connect():
    client_id = "volvoAAOS2mqtt" if os.environ.get("IS_HA_ADDON") \
        else settings.get("mqtt_client_id", "volvoAAOS2mqtt_keycloak")
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id)
    except AttributeError:
        # paho-mqtt < 2.0 does not have CallbackAPIVersion
        client = mqtt.Client(client_id)

    if "logging" in settings["mqtt"] and settings["mqtt"]["logging"]:
        mqtt_logger = logging.getLogger("mqtt")
        client.enable_logger(mqtt_logger)

    client.on_message = safe_on_message
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect

    client.will_set(availability_topic, "offline", 0, False)
    if settings["mqtt"]["username"] and settings["mqtt"]["password"]:
        client.username_pw_set(settings["mqtt"]["username"], settings["mqtt"]["password"])
    port = 1883
    if util.keys_exists(settings["mqtt"], "port"):
        conf_port = settings["mqtt"]["port"]
        if isinstance(conf_port, int):
            if conf_port > 0:
                port = settings["mqtt"]["port"]
    client.connect(settings["mqtt"]["broker"], port)

    global mqtt_client
    mqtt_client = client
    client.loop_start()
    # Browser login replaces MQTT OTP input.

    create_login_sensor()


def create_otp_input():
    state_topic = otp_mqtt_topic + "/state"
    config = {
        "name": "Volvo OTP",
        "default_entity_id": "volvo_otp",
        "schema": "state",
        "command_topic": otp_mqtt_topic,
        "state_topic": state_topic,
        "unique_id": "volvoAAOS2mqtt_otp",
        "pattern": r"\d{6}",
        "icon": "mdi:two-factor-authentication",
        "mode": "text"
    }

    mqtt_client.publish(
        "homeassistant/text/volvoAAOS2mqtt/volvo_otp/config",
        json.dumps(config),
        retain=True
    )

    mqtt_client.publish(
        state_topic,
        "000000",
        retain=True
    )

def set_otp_state():
    mqtt_client.publish(
        otp_mqtt_topic + "/state",
        otp_code,
        retain=True
    )


def send_car_images(vin, data, device):
    if util.keys_exists(data, "images"):
        for entity in [{"name": "Exterior Image", "id": "exterior_image"},
                       {"name": "Interior Image", "id": "interior_image"}]:
            image_topic = f"homeassistant/image/{vin}_{entity['id']}/image_topic"
            config = {
                "name": entity["name"],
                "default_entity_id": f"volvo_{vin}_{entity['id']}",
                "schema": "state",
                "icon": "mdi:image-area",
                "content_type": "image/png",
                "image_topic": image_topic,
                "device": device,
                "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
                "availability_topic": availability_topic
            }

            mqtt_client.publish(
                f"homeassistant/image/volvoAAOS2mqtt/{vin}_{entity['id']}/config",
                json.dumps(config),
                retain=True
            )

            headers = {"User-Agent": "(Windows NT 10.0; Win64; x64)",
                       "sec-ch-ua-platform": "\"Windows\"", "sec-fetch-dest": "document", "Accept-Encoding": "gzip, deflate, br, zstd",
                       "sec-fetch-mode": "navigate", "sec-fetch-site": "none", "sec-fetch-user": "?1", "upgrade-insecure-requests": "1"}

            # Post Images
            if not os.path.exists(f'{entity["id"]}_{vin}.png'):
                data_path = "exteriorImageUrl" if entity["id"] == "exterior_image" else "internalImageUrl"
                response = requests.get(data["images"][data_path], headers=headers)
                if response.status_code == 200:
                    with open(f'{entity["id"]}_{vin}.png', 'wb') as image:
                        image.write(response.content)
                else:
                    logging.warning("Error getting car images: " + str(response.status_code) + " Message: " + response.text)

            if os.path.exists(f'{entity["id"]}_{vin}.png'):
                ext_image = open(f'{entity["id"]}_{vin}.png', mode="rb").read()
                mqtt_client.publish(
                    image_topic,
                    ext_image,
                    retain=True
                )


def on_connect(client, userdata, flags, rc):
    # set a better name for the mqtt_loop Thread
    threading.current_thread().name = 'mqtt_thread'
    logging.info("MQTT connected")
    # The broker may have lost retained discovery; let the next refresh republish it.
    published_configs.clear()

    send_heartbeat()
    if len(subscribed_topics) > 0:
        for topic in subscribed_topics:
            mqtt_client.subscribe(topic)


def on_disconnect(client, userdata, rc):
    logging.warning(f"MQTT disconnected, reconnecting automatically rc={rc}")


def safe_on_message(client, userdata, msg):
    try:
        on_message(client, userdata, msg)
    except Exception as error:
        logging.error(f"Exception {error} processing received message on topic {msg.topic}")
        return None

def on_message(client, userdata, msg):
    payload = msg.payload.decode("UTF-8")
    if msg.retain and msg.topic.endswith("/command"):
        logging.warning("Ignoring retained command")
        return None
    for vehicle_vin, entities in volvo.supported_endpoints.items():
        for entity in entities:
            if not entity.get("vehicle_command"):
                continue
            command_topic = f"homeassistant/button/{vehicle_vin}_{entity['id']}/command"
            if msg.topic == command_topic:
                if msg.retain or payload != "PRESS":
                    logging.warning("Ignoring retained or invalid optional command")
                    return None
                control_queue.submit(vehicle_vin, entity["id"], "PRESS",
                    lambda vin=vehicle_vin, key=entity["id"]: volvo.execute_optional_command(vin,key))
                return None
    if msg.topic == otp_mqtt_topic:
        if msg.retain == 0:
            global otp_code
            otp_code = payload
            set_otp_state()
        else:
            logging.warning("Found retained OTP, this can't work! Please clean retained messages!")
        return None
    else:
        try:
            vin = msg.topic.split('/')[2].split('_')[0]
        except IndexError:
            logging.error(f"Error - Cannot get vin from MQTT topic : {msg.topic}!")
            return None

    if "climate_status" in msg.topic:
        if payload == "ON":
            start_climate(vin)
            return None
        elif payload == "OFF":
            stop_climate(vin)
            return None
        return None
    elif "lock_status" in msg.topic:
        if payload == "LOCK":
            lock_car(vin)
            return None
        elif payload == "UNLOCK":
            unlock_car(vin)
            return None
        return None
    elif "update_data" in msg.topic:
        if payload == "PRESS":
            update_car_data_async(True)
            return None
        return None
    elif "update_interval" in msg.topic:
        try:
            update_interval = int(payload)
            if update_interval >= 60 or update_interval == -1:
               settings.update({"updateInterval": int(update_interval)})
            else:
                logging.warning(f"Interval {update_interval} seconds is to low. Doing nothing!")
            update_car_data_async()
            return None
        except ValueError as error:
            logging.error(f"Unable to change update_interval {error} payload={payload}")
            return None
    elif "schedule" in msg.topic:
        try:
            d = json.loads(payload)
        except ValueError as e:
            logging.error(f"Can't set timer. Error: {e}, payload={payload}")
            return None

        if d["mode"] == "timer":
            start_climate_timer(d, vin)
            return None
        else:
            logging.warning("No schedule mode found, doing nothing")
            return None
    return None


def start_climate_timer(d, vin):
    global active_schedules
    try:
        minute = int(d["start_time"].split(":")[1])
        hour = int(d["start_time"].split(":")[0])
        local_datetime = datetime.now(util.TZ)
        start_datetime = local_datetime.replace(hour=hour, minute=minute, second=0)
        timer_seconds = (start_datetime - local_datetime).total_seconds()
    except Exception as e:
        logging.error(f"Error creating climate timer: {e}")
        return None

    if timer_seconds > 0:
        Timer(timer_seconds, activate_climate_timer, (vin, start_datetime.isoformat(),)).start()
        active_schedules[vin]["timers"].append(start_datetime.isoformat())
        logging.debug(f"Climate timer set to {start_datetime}")
        update_car_data_async()
    else:
        logging.warning("Timer can not be set. Unusable start time entered")


def publish_local_state(vin, entity_id, state):
    entity=next((x for x in volvo.supported_endpoints.get(vin,[]) if x["id"]==entity_id),None)
    if entity:
        mqtt_client.publish(f"homeassistant/{entity['domain']}/{vin}_{entity_id}/state",state,retain=True)


def _unlock_car(vin):
    if not volvo.api_call(CAR_UNLOCK_URL,"POST",vin):return False
    publish_local_state(vin,"lock_status","UNLOCKING")
    # Only a confirmed state counts as success, so an unconfirmed request can be repeated.
    return volvo.check_lock_status(vin,"LOCKED")


def _lock_car(vin):
    if not volvo.api_call(CAR_LOCK_URL,"POST",vin):return False
    publish_local_state(vin,"lock_status","LOCKING")
    return volvo.check_lock_status(vin,"UNLOCKED")


def clear_climate_assumption(vin, generation=None):
    with climate_state_lock:
        if generation is not None and climate_generation.get(vin)!=generation:return
        timer=climate_timer.get(vin)
        if timer:timer.cancel()
        climate_generation[vin]=climate_generation.get(vin,0)+1
        assumed_climate_state[vin]="OFF"
        publish_local_state(vin,"climate_status","OFF")


def _stop_climate(vin):
    if not volvo.api_call(CLIMATE_STOP_URL,"POST",vin):return False
    clear_climate_assumption(vin)
    return True


def activate_climate_timer(vin, start_time):
    start_climate(vin)
    active_schedules[vin]["timers"].remove(start_time)
    update_car_data()


def _start_climate(vin):
    if not volvo.api_call(CLIMATE_START_URL,"POST",vin):return False
    with climate_state_lock:
        old=climate_timer.get(vin)
        if old:old.cancel()
        generation=climate_generation.get(vin,0)+1
        climate_generation[vin]=generation
        assumed_climate_state[vin]="ON"
        timer=Timer(1800,clear_climate_assumption,(vin,generation))
        timer.daemon=True
        climate_timer[vin]=timer
        timer.start()
        publish_local_state(vin,"climate_status","ON")
    return True


def update_loop():
    create_ha_devices()
    while True:
        if settings["updateInterval"] > 0:
            logging.info("Sending mqtt update...")
            try:
                send_heartbeat()
                update_car_data()
                logging.info("MQTT refresh done; daily vehicle API attempts: %s", volvo.session.snapshot())
            except Exception as error:
                logging.info(f"Exception {error} in Mqtt update. Next run in {settings['updateInterval']} seconds.")
            time.sleep(30)
        else:
            logging.info("Data update is disabled, doing nothing for 30 seconds")
            time.sleep(30)


def update_car_data_async(force_update=False):
    # MQTT callbacks must not wait for vehicle API requests; commands would queue behind them.
    if not async_refresh_lock.acquire(blocking=False):
        logging.info("Refresh already pending; request ignored")
        return
    def run():
        try:
            update_car_data(force_update)
        finally:
            async_refresh_lock.release()
    Thread(target=run,daemon=True,name="refresh_thread").start()


def update_car_data(force_update=False, overwrite=None):
    # Timer/command callbacks cannot run overlapping full refreshes.
    with update_lock:
        return _update_car_data(force_update,overwrite or {})


def _update_car_data(force_update=False, overwrite=None):
    global last_data_update
    last_data_update = format_datetime(datetime.now(util.TZ), format="medium", locale=settings["babelLocale"])
    for vin in volvo.vins:
        for entity in volvo.supported_endpoints[vin]:
            if entity["domain"] in ["button"]:
                continue

            ov_entity_id = ""
            ov_vin = ""
            ov_state = ""
            if bool(overwrite):
                ov_entity_id = overwrite["entity_id"]
                ov_vin = overwrite["vin"]
                ov_state = overwrite["state"]

            if entity["id"] == "climate_status":
                state = pending_climate.get(vin, (None,assumed_climate_state[vin]))[1]
            elif entity["id"] == "last_data_update":
                state = last_data_update
            elif entity["id"] == "active_schedules":
                state = active_schedules[vin]
            elif entity["id"] == "update_interval":
                state = settings["updateInterval"]
            elif entity["id"] == "api_backend_status":
                if force_update:
                    state = volvo.get_backend_status()
                else:
                    state = volvo.backend_status
                message = state if isinstance(state, str) else ""
                mqtt_client.publish(
                    f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes",
                    json.dumps({"message": message}), retain=True)
                state = (message if len(message) <= 255 else message[:252] + "...") or "unknown"
            else:
                if entity["id"] == ov_entity_id and vin == ov_vin:
                    state = ov_state
                else:
                    state = volvo.api_call(entity["url"], "GET", vin, entity["id"], force_update)

            if entity["domain"] == "device_tracker" or entity["id"] == "active_schedules":
                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"
            elif entity["id"] == "warnings":
                mqtt_client.publish(
                    f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes",
                    json.dumps(state),
                    retain=True
                )
                if state:
                    state = sum(value == "FAILURE" for value in state.values())
                else:
                    state = 0

                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state"
            else:
                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state"

            logging.debug(f"update_car_data {topic} {state}")
            if state or state == 0:
                mqtt_client.publish(
                    topic,
                    json.dumps(state) if isinstance(state, dict) or isinstance(state, list) else state,
                    retain=True
                )
                update_ha_device(entity, vin, state)


def update_ha_device(entity, vin, state):
    icon_config = icon_states.get(entity["id"])
    try:
        if icon_config and state:
            logging.debug("Entity " + entity["id"] + " state " + str(state))
            if isinstance(state, float):
                icon = util.get_icon_between(icon_config, state)
            elif state.replace(".", "").isnumeric():
                state = float(state)
                icon = util.get_icon_between(icon_config, state)
            else:
                icon = icon_config[state]
        else:
            return None
    except:
        return None

    logging.debug("Updating icon to " + icon + " for " + entity["id"])
    config = {
        "name": entity['name'],
        "default_entity_id": f"volvo_{vin}_{entity['id']}",
        "schema": "state",
        "icon": f"mdi:{icon}" if icon else f"mdi:{entity['icon']}",
        "state_topic": f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state",
        "device": devices[vin],
        "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
        "availability_topic": availability_topic
    }
    if entity.get("device_class"):
        config["device_class"] = entity["device_class"]

    if entity.get("unit"):
        config["unit_of_measurement"] = entity["unit"]

    if entity.get("state_class"):
        config["state_class"] = entity["state_class"]

    if entity.get("domain") == "device_tracker" or entity.get("id") == "api_backend_status":
        config["json_attributes_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"

    if entity.get("domain") in ["switch", "lock", "button"]:
        command_topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/command"
        config["command_topic"] = command_topic
        subscribe_once(command_topic)

    config_topic = f"homeassistant/{entity['domain']}/volvoAAOS2mqtt/{vin}_{entity['id']}/config"
    payload = json.dumps(config)
    # The refresh loop runs every 30 s; republish discovery only when the icon or config changes.
    if published_configs.get(config_topic) != payload:
        if mqtt_client.publish(config_topic, payload, retain=True).rc == mqtt.MQTT_ERR_SUCCESS:
            published_configs[config_topic] = payload


def subscribe_once(topic):
    if topic not in subscribed_topics:
        subscribed_topics.append(topic)
        mqtt_client.subscribe(topic)


def create_ha_devices():
    global subscribed_topics, devices
    for vin in volvo.vins:
        device = volvo.get_vehicle_details(vin)
        devices[vin] = device
        for entity in volvo.supported_endpoints[vin]:
            config = {
                "name": entity['name'],
                "default_entity_id": f"volvo_{vin}_{entity['id']}",
                "schema": "state",
                "icon": f"mdi:{entity['icon']}",
                "state_topic": f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state",
                "device": device,
                "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
                "availability_topic": availability_topic
            }

            if entity.get("domain") == "button":
                config.pop("state_topic", None)
                config.pop("schema", None)
                config["payload_press"] = "PRESS"

            if entity.get("device_class"):
                config["device_class"] = entity["device_class"]

            if entity.get("unit"):
                config["unit_of_measurement"] = entity["unit"]

            if entity.get("state_class"):
                config["state_class"] = entity["state_class"]

            if (entity.get("domain") == "device_tracker" or entity.get("id") == "active_schedules"
                    or entity.get("id") in ("warnings", "api_backend_status")):
                config["json_attributes_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"

            if entity.get("domain") in ["switch", "lock", "button", "number"]:
                command_topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/command"
                config["command_topic"] = command_topic
                subscribe_once(command_topic)

                if entity["domain"] == "number":
                    config["min"] = entity["min"]
                    config["max"] = entity["max"]
                    config["mode"] = entity["mode"]
            elif entity.get("domain") == "image":
                config["url_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/image_url"

            config_topic = f"homeassistant/{entity['domain']}/volvoAAOS2mqtt/{vin}_{entity['id']}/config"
            published_configs[config_topic] = json.dumps(config)
            mqtt_client.publish(config_topic, published_configs[config_topic], retain=True)
    time.sleep(2)
    send_heartbeat()


def send_heartbeat():
    mqtt_client.publish(availability_topic, "online", retain=True)


def send_offline():
    mqtt_client.publish(availability_topic, "offline", retain=True)


def delete_old_entities():
    for vin in volvo.vins:
        for entity in old_entity_ids:
            topic = f"homeassistant/sensor/volvoAAOS2mqtt/{vin}_{entity}/config"
            mqtt_client.publish(topic, payload="", retain=True)


def create_login_sensor():
    mqtt_client.publish("homeassistant/binary_sensor/volvoAAOS2mqtt/login_required/config",
        json.dumps({"name": "Volvo login required", "unique_id": "volvoAAOS2mqtt_login_required",
                    "default_entity_id": "binary_sensor.volvo_login_required",
                    "state_topic": "volvoAAOS2mqtt/login_required/state",
                    "payload_on": "ON", "payload_off": "OFF", "entity_category": "diagnostic"}), retain=True)


def set_login_required(required):
    # No vehicle availability dependency: login can be required before vehicle setup.
    mqtt_client.publish("volvoAAOS2mqtt/login_required/state", "ON" if required else "OFF", retain=True)


def unlock_car(vin):
    control_queue.submit(vin,"lock","UNLOCKED",lambda:_unlock_car(vin))


def lock_car(vin):
    control_queue.submit(vin,"lock","LOCKED",lambda:_lock_car(vin))


def _submit_climate(vin, target):
    with climate_state_lock:
        generation=climate_request_generation.get(vin,0)+1
        climate_request_generation[vin]=generation
        pending_climate[vin]=(generation,target)
        publish_local_state(vin,"climate_status",target)
        def finished():
            with climate_state_lock:
                if pending_climate.get(vin,(None,None))[0]==generation:
                    pending_climate.pop(vin,None)
                    publish_local_state(vin,"climate_status",assumed_climate_state.get(vin,"OFF"))
        action = _start_climate if target=="ON" else _stop_climate
        # Submit under the same lock so a newer intent cannot be queued before an older one.
        control_queue.submit(vin,"climate",target,lambda:action(vin),finished)


def start_climate(vin):
    _submit_climate(vin,"ON")


def stop_climate(vin):
    _submit_climate(vin,"OFF")
