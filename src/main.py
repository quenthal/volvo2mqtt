import logging
import time
import volvo
import keycloak
import web_login
from keycloak import LoginRequired, OAuthFailure
from volvo import authorize
from mqtt import update_loop, connect
from const import VERSION
from util import set_tz, setup_logging, set_mqtt_settings, validate_settings


if __name__ == '__main__':
    setup_logging()
    logging.info("Starting volvo2mqtt version " + VERSION)
    validate_settings()
    set_tz()
    set_mqtt_settings()
    volvo.auth_client = keycloak.configured_client()
    web_login.start(volvo.auth_client)
    connect()
    while True:
        try:
            authorize()
            break
        except (LoginRequired, OAuthFailure) as error:
            logging.warning("%s", error)
            time.sleep(60)
    update_loop()
