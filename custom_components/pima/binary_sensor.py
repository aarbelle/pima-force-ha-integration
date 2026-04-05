import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    _LOGGER.warning("PIMA binary_sensor platform loaded")

    server = hass.data[DOMAIN]["server"]
    entities = {}  # zone_num -> PimaZoneBinarySensor

    def _create_sensor(zone_num):
        sensor = PimaZoneBinarySensor(server, zone_num)
        entities[zone_num] = sensor
        return sensor

    # If zones are already known (e.g. panel was connected before platform loaded)
    if server.zones:
        new = [_create_sensor(z) for z in server.zones if z not in entities]
        if new:
            async_add_entities(new)
            _LOGGER.warning("PIMA: registered %s zones at platform load", len(new))

    # pima_zones_initialized: panel told us how many zones exist.
    # Create all entities immediately as unavailable — they become available on first update.
    async def handle_zones_initialized(event):
        count = event.data.get("count", 0)
        _LOGGER.warning("PIMA zones initialized: count=%s", count)
        new = []
        for zone_num in range(1, count + 1):
            if zone_num not in entities:
                new.append(_create_sensor(zone_num))
        if new:
            async_add_entities(new)
            _LOGGER.warning("PIMA: created %s zone entities", len(new))

    hass.bus.async_listen("pima_zones_initialized", handle_zones_initialized)

    # pima_zone_names_updated: all zone names have been fetched from the panel.
    # Update both the entity's _attr_name AND the HA entity registry so the
    # display name persists correctly across restarts.
    async def handle_zone_names_updated(event):
        registry = er.async_get(hass)
        for zone_num, sensor in entities.items():
            name = server.zones.get(zone_num, {}).get("name", f"PIMA Zone {zone_num}")
            sensor._attr_name = name
            # Update the registry entry so HA displays the correct name
            entry = registry.async_get(sensor.entity_id)
            if entry and entry.name != name:
                # Only update if the user hasn't manually customised it
                if entry.name is None or entry.name.startswith("PIMA Zone"):
                    registry.async_update_entity(sensor.entity_id, name=name)
                    _LOGGER.warning("Updated registry name for zone %s: %s", zone_num, name)
            sensor.async_write_ha_state()

    hass.bus.async_listen("pima_zone_names_updated", handle_zone_names_updated)

    # pima_zone_update: state/attribute update arrives from panel
    async def handle_zone_update(event):
        zone = event.data.get("zone")
        if zone is None:
            return

        # Late-create if we somehow missed initialization
        if zone not in entities:
            _LOGGER.warning("PIMA: late-creating entity for zone %s", zone)
            async_add_entities([_create_sensor(zone)])

        sensor = entities[zone]
        sensor._attr_name = event.data.get("name", f"PIMA Zone {zone}")
        sensor._attr_is_on = event.data.get("open", False)
        sensor._attr_available = True
        sensor._attr_extra_state_attributes = _build_attrs(event.data)
        sensor.async_write_ha_state()

    hass.bus.async_listen("pima_zone_update", handle_zone_update)

    # Mark all unavailable on disconnect, available on reconnect
    async def handle_disconnected(event):
        _LOGGER.warning("PIMA: panel disconnected — marking zones unavailable")
        for sensor in entities.values():
            sensor._attr_available = False
            sensor.async_write_ha_state()

    hass.bus.async_listen("pima_disconnected", handle_disconnected)

    async def handle_connected(event):
        _LOGGER.warning("PIMA: panel reconnected")
        for sensor in entities.values():
            sensor._attr_available = True
            sensor.async_write_ha_state()

    hass.bus.async_listen("pima_connected", handle_connected)


def _build_attrs(data):
    return {
        "zone": data.get("zone"),
        "manual_bypassed": data.get("manual_bypassed", False),
        "auto_bypassed": data.get("auto_bypassed", False),
        "alarmed": data.get("alarmed", False),
        "armed": data.get("armed", False),
        "supervision_loss": data.get("supervision_loss", False),
        "low_battery": data.get("low_battery", False),
        "short": data.get("short", False),
        "cut_tamper": data.get("cut_tamper", False),
        "soak": data.get("soak", False),
        "chime": data.get("chime", False),
        "anti_mask": data.get("anti_mask", False),
        "duress": data.get("duress", False),
        "fire": data.get("fire", False),
        "medical": data.get("medical", False),
        "panic": data.get("panic", False),
        "last_event": data.get("last_event"),
    }


class PimaZoneBinarySensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, server, zone_num):
        self.server = server
        self.zone = zone_num
        self._attr_name = server.zones.get(zone_num, {}).get("name", f"PIMA Zone {zone_num}")
        self._attr_unique_id = f"pima_zone_{zone_num}"
        self._attr_is_on = server.zones.get(zone_num, {}).get("open", False)
        self._attr_available = False  # unavailable until first real update arrives
        self._attr_extra_state_attributes = {"zone": zone_num}
