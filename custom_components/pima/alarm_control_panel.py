import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Map server state strings to HA alarm states
STATE_MAP = {
    "disarmed":      AlarmControlPanelState.DISARMED,
    "armed_away":    AlarmControlPanelState.ARMED_AWAY,
    "armed_home_1":  AlarmControlPanelState.ARMED_HOME,
    "armed_home_2":  AlarmControlPanelState.ARMED_HOME,
    "armed_home_3":  AlarmControlPanelState.ARMED_HOME,
    "armed_home_4":  AlarmControlPanelState.ARMED_HOME,
    "armed_shabbat": AlarmControlPanelState.ARMED_HOME,
}

# PIMA operation types (Appendix B)
OPTYPE_ARM_AWAY   = 12
OPTYPE_ARM_HOME1  = 13
OPTYPE_ARM_HOME2  = 14
OPTYPE_ARM_HOME3  = 15
OPTYPE_ARM_HOME4  = 16
OPTYPE_DISARM     = 17
OPTYPE_SHABBAT    = 43


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    _LOGGER.warning("PIMA alarm_control_panel platform loaded")
    server = hass.data[DOMAIN]["server"]

    panel = PimaAlarmControlPanel(server)
    async_add_entities([panel])

    async def handle_state(event):
        panel._attr_alarm_state = STATE_MAP.get(event.data.get("state"), AlarmControlPanelState.DISARMED)
        panel._attr_available = True
        panel.async_write_ha_state()

    hass.bus.async_listen("pima_state", handle_state)

    async def handle_connected(event):
        panel._attr_available = True
        panel.async_write_ha_state()

    hass.bus.async_listen("pima_connected", handle_connected)

    async def handle_disconnected(event):
        panel._attr_available = False
        panel.async_write_ha_state()

    hass.bus.async_listen("pima_disconnected", handle_disconnected)


class PimaAlarmControlPanel(AlarmControlPanelEntity):
    _attr_should_poll = False
    _attr_unique_id = "pima_alarm"
    _attr_name = "PIMA Alarm"
    _attr_code_format = CodeFormat.NUMBER
    _attr_code_arm_required = False
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
    )

    def __init__(self, server):
        self.server = server
        self._attr_alarm_state = STATE_MAP.get(server.state, AlarmControlPanelState.DISARMED)
        self._attr_available = server.connected

    async def async_alarm_disarm(self, code=None):
        _LOGGER.warning("PIMA: disarm requested")
        await self.server.send_operation(OPTYPE_DISARM, partition=0)

    async def async_alarm_arm_away(self, code=None):
        _LOGGER.warning("PIMA: arm away requested")
        await self.server.send_operation(OPTYPE_ARM_AWAY, partition=0)

    async def async_alarm_arm_home(self, code=None):
        _LOGGER.warning("PIMA: arm home requested")
        await self.server.send_operation(OPTYPE_ARM_HOME1, partition=0)
