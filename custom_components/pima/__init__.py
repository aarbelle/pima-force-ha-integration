import logging
import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from .custom_components.pima.const import DOMAIN, DEFAULT_PORT
from .custom_components.pima.server import PimaServer

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("account"): cv.positive_int,
                vol.Required("password"): cv.string,
                vol.Optional("port", default=DEFAULT_PORT): cv.port,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    hass.data.setdefault(DOMAIN, {})

    if "server" in hass.data[DOMAIN]:
        _LOGGER.warning("PIMA already set up, skipping duplicate setup")
        return True

    conf = config[DOMAIN]
    account = conf["account"]
    password = conf["password"]
    port = conf["port"]

    server = PimaServer(hass, account=account, password=password, port=port)
    await server.start()

    hass.data[DOMAIN]["server"] = server

    hass.async_create_task(
        async_load_platform(hass, "binary_sensor", DOMAIN, {}, config)
    )
    hass.async_create_task(
        async_load_platform(hass, "alarm_control_panel", DOMAIN, {}, config)
    )

    return True
