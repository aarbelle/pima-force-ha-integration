import asyncio
import json
import logging
from datetime import datetime, UTC



_LOGGER = logging.getLogger(__name__)


def _redact(msg: dict) -> dict:
    """Return a copy of a message dict with the password field masked."""
    if "password" in msg:
        msg = {**msg, "password": "***"}
    return msg


def _decode_hebrew(s: str) -> str:
    """Re-decode a latin-1 string as Windows-1255 to recover Hebrew characters."""
    try:
        return s.encode("latin-1").decode("windows-1255")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


class PimaServer:
    def __init__(self, hass, account, password, port):
        self.hass = hass
        self.account = int(account)
        self.password = str(password)
        self.port = int(port)
        self.server = None
        self.writer = None
        self.counter = 1
        self.state = "disarmed"
        self.connected = False
        self.last_seen = None
        self.zones = {}          # zone_num -> dict with "open", "name", etc.
        self.installed_zones = 0

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle_client, "0.0.0.0", self.port
        )
        _LOGGER.warning("PIMA server listening on port %s", self.port)

    async def handle_client(self, reader, writer):
        _LOGGER.warning("PIMA client connected")
        self.writer = writer
        self.connected = True
        self.last_seen = datetime.now(UTC)
        self._init_done = False  # DATA-REQs sent only after first null handshake
        self.hass.bus.async_fire("pima_connected", {})

        buffer = ""

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break

                self.last_seen = datetime.now(UTC)
                # Decode as latin-1 (1:1 byte mapping, never fails).
                # Hebrew names from PIMA are Windows-1255; latin-1 preserves
                # the raw bytes through JSON parsing so we can re-decode them
                # correctly per field afterwards.
                chunk = data.decode("latin-1").replace("\x00", "")
                if not chunk:
                    continue

                buffer += chunk

                while True:
                    start = buffer.find("{")
                    if start == -1:
                        buffer = ""
                        break

                    if start > 0:
                        buffer = buffer[start:]

                    depth = 0
                    in_string = False
                    escape = False
                    end = None

                    for i, ch in enumerate(buffer):
                        if in_string:
                            if escape:
                                escape = False
                            elif ch == "\\":
                                escape = True
                            elif ch == '"':
                                in_string = False
                        else:
                            if ch == '"':
                                in_string = True
                            elif ch == "{":
                                depth += 1
                            elif ch == "}":
                                depth -= 1
                                if depth == 0:
                                    end = i + 1
                                    break

                    if end is None:
                        break

                    raw = buffer[:end]
                    buffer = buffer[end:]

                    try:
                        msg = json.loads(raw)
                        _LOGGER.warning("RX: %s", _redact(msg))
                        await self.handle_message(msg)
                    except Exception as e:
                        _LOGGER.error("Parse error: %s | raw=%r", e, raw)

        except Exception as e:
            _LOGGER.exception("PIMA connection error: %s", e)

        finally:
            _LOGGER.warning("PIMA client disconnected")
            self.connected = False
            self.writer = None
            self.hass.bus.async_fire("pima_disconnected", {})

    async def handle_message(self, msg):
        frame_type = str(msg.get("frame_type", "")).upper()

        if frame_type == "NULL":
            await self.send_ack(msg)
            if not self._init_done:
                self._init_done = True
                _LOGGER.warning("PIMA handshake complete — sending initial DATA-REQs")
                await self._send_init_requests()
            return

        if frame_type == "EVENT":
            await self.send_ack(msg)
            self.process_event(msg)
            return

        if frame_type == "DATA":
            await self.send_ack(msg)
            self.process_data(msg)
            return

        if frame_type == "ACK":
            _LOGGER.warning("PIMA ACK received: %s", msg)
            return

        if frame_type == "NAK":
            _LOGGER.warning("PIMA NAK received: %s", msg)
            return

        _LOGGER.warning("PIMA unknown frame type: %s", msg)

    async def send_ack(self, msg):
        ack = {
            "frame_type": "ACK",
            "counter": int(msg.get("counter", 0)),
            "account": self.account,
            "kc": 1,
        }
        _LOGGER.warning("TX ACK: %s", _redact(ack))
        await self.send(ack)

    async def send(self, payload):
        if not self.writer:
            _LOGGER.warning("PIMA send skipped — no active connection")
            return

        try:
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            _LOGGER.warning("TX RAW: %s", _redact(json.loads(raw)))
            self.writer.write(raw.encode("utf-8"))
            await self.writer.drain()
        except Exception as e:
            _LOGGER.exception("PIMA send failed: %s", e)

    def _zone_event_payload(self, zone_num):
        """Build the full event payload for a zone from current state."""
        info = self.zones.get(zone_num, {})
        return {
            "zone": zone_num,
            "name": info.get("name", f"PIMA Zone {zone_num}"),
            "open": info.get("open", False),
            "manual_bypassed": info.get("manual_bypassed", False),
            "auto_bypassed": info.get("auto_bypassed", False),
            "alarmed": info.get("alarmed", False),
            "armed": info.get("armed", False),
            "supervision_loss": info.get("supervision_loss", False),
            "low_battery": info.get("low_battery", False),
            "short": info.get("short", False),
            "cut_tamper": info.get("cut_tamper", False),
            "soak": info.get("soak", False),
            "chime": info.get("chime", False),
            "anti_mask": info.get("anti_mask", False),
            "duress": info.get("duress", False),
            "fire": info.get("fire", False),
            "medical": info.get("medical", False),
            "panic": info.get("panic", False),
            "last_event": info.get("last_event"),
        }

    def _ensure_zone(self, zone_num):
        """Initialize zone dict if not present."""
        if zone_num not in self.zones:
            self.zones[zone_num] = {"open": False, "name": f"PIMA Zone {zone_num}"}

    def _notify_alarm(self, alarm_type: str, zone_num: int, active: bool):
        """Fire a pima_alarm event and create/dismiss a persistent notification."""
        zone_name = self.zones.get(zone_num, {}).get("name", f"PIMA Zone {zone_num}")
        notification_id = f"pima_alarm_{alarm_type}_{zone_num}"

        # Fire a dedicated alarm event so users can trigger automations on it.
        self.hass.bus.async_fire("pima_alarm", {
            "alarm_type": alarm_type,
            "zone": zone_num,
            "name": zone_name,
            "active": active,
        })

        if active:
            _TITLES = {
                "burglary": "PIMA Alarm: Break-In Detected",
                "fire":     "PIMA Alarm: Fire Detected",
                "medical":  "PIMA Alarm: Medical Emergency",
                "panic":    "PIMA Alarm: Panic Triggered",
                "duress":   "PIMA Alarm: Duress Code Used",
            }
            title = _TITLES.get(alarm_type, f"PIMA Alarm: {alarm_type.title()}")
            message = f"Zone {zone_num} ({zone_name}) has triggered a {alarm_type} alarm."
            _LOGGER.warning("PIMA ALARM %s: %s", alarm_type.upper(), message)
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": title,
                        "message": message,
                        "notification_id": notification_id,
                    },
                )
            )
        else:
            _LOGGER.warning("PIMA ALARM %s restored: zone %s (%s)", alarm_type.upper(), zone_num, zone_name)
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": notification_id},
                )
            )

    def process_event(self, msg):
        t = msg.get("type")
        q = msg.get("qualifier")
        zone = msg.get("zone")

        # ── Arm / Disarm events ──────────────────────────────────────────────
        # CID types:
        #   400 / 401 = local/remote arm-away (q=3) or disarm (q=1)
        #   403       = auto arming (q=3 only)
        #   407       = remote arm/disarm via app/upload (q=3/1)
        #   408       = fast arming (q=3 only → armed_away)
        #   441       = Home-x / Shabbat arming (q=3) or disarm (q=1)
        if t in (400, 401, 403, 407, 408, 441):
            if q == 1:
                self.state = "disarmed"
            elif q == 3:
                if t == 441:
                    self.state = "armed_home_1"   # Home1/2/3/4 all map here; refine if needed
                else:
                    self.state = "armed_away"
            self.hass.bus.async_fire("pima_state", {"state": self.state})
            return

        # ── Zone open / close (CID 760) ──────────────────────────────────────
        # 760-1-N = zone N open, 760-3-N = zone N closed
        if t == 760 and zone is not None:
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["open"] = (q == 1)   # qualifier 1 = event (open), 3 = restore (closed)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            return

        # ── Burglary / alarm events (CID 130) ────────────────────────────────
        # 130-1-N = alarm on zone N, 130-3-N = restore
        if t == 130 and zone is not None:
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["alarmed"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            self._notify_alarm("burglary", zone_num, q == 1)
            return

        # ── Zone bypass (CID 570) ────────────────────────────────────────────
        # 570-1-N = bypass zone N, 570-3-N = remove bypass
        if t == 570 and zone is not None:
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["manual_bypassed"] = (q == 1)   # q=1 = bypassed, q=3 = restored
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            return

        # ── Wireless zone supervision/battery/tamper ─────────────────────────
        if t == 381 and zone is not None:   # 381 = wireless supervision loss
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["supervision_loss"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            return

        if t == 384 and zone is not None:   # 384 = wireless low battery
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["low_battery"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            return

        if t == 144 and zone is not None:   # 144 = cut/short (tamper)
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["cut_tamper"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            return

        # ── Special zone alarm types ─────────────────────────────────────────
        if t == 110 and zone is not None:   # Fire alarm
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["fire"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            self._notify_alarm("fire", zone_num, q == 1)
            return

        if t == 100 and zone is not None:   # Medical alarm
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["medical"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            self._notify_alarm("medical", zone_num, q == 1)
            return

        if t in (120, 122) and zone is not None:   # Panic / silent panic
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["panic"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            self._notify_alarm("panic", zone_num, q == 1)
            return

        if t == 121 and zone is not None:   # Duress
            zone_num = int(zone)
            self._ensure_zone(zone_num)
            self.zones[zone_num]["duress"] = (q == 1)
            self.zones[zone_num]["last_event"] = t
            self.hass.bus.async_fire("pima_zone_update", self._zone_event_payload(zone_num))
            self._notify_alarm("duress", zone_num, q == 1)
            return

        _LOGGER.warning("Unhandled PIMA event: type=%s qualifier=%s zone=%s", t, q, zone)

    def process_data(self, msg):
        data_id = msg.get("id")
        params = msg.get("parameters", [])
        start_order = int(msg.get("start_order", 1))

        _LOGGER.warning(
            "PIMA DATA received: id=%s start_order=%s params=%s",
            data_id, start_order, params,
        )

        # ── 2310: Partition key status ───────────────────────────────────────
        # Response is one value per partition (start_order = partition index).
        # Values: 1=not exist, 2=disarmed, 3=armed_away, 4=home1,
        #         5=home2, 6=home3, 7=home4, 8=shabbat_on, 9=shabbat_off
        if data_id == 2310 and params:
            state_map = {
                1: None,             # partition does not exist
                2: "disarmed",
                3: "armed_away",
                4: "armed_home_1",
                5: "armed_home_2",
                6: "armed_home_3",
                7: "armed_home_4",
                8: "armed_shabbat",
                9: "armed_shabbat",
            }
            # Use the first existing (non-None) partition state for single-partition systems.
            # For multi-partition support this would need per-partition entities.
            for i, raw in enumerate(params):
                code = int(raw)
                new_state = state_map.get(code)
                if new_state is not None:
                    self.state = new_state
                    self.hass.bus.async_fire(
                        "pima_state",
                        {"state": self.state, "partition": start_order + i},
                    )
            return

        # ── 2148: Number of installed zones ──────────────────────────────────
        if data_id == 2148 and params:
            try:
                self.installed_zones = int(params[0])
                _LOGGER.warning("Installed zones: %s", self.installed_zones)

                for zone in range(1, self.installed_zones + 1):
                    self._ensure_zone(zone)

                self.hass.bus.async_fire(
                    "pima_zones_initialized",
                    {"count": self.installed_zones},
                )

                # Chain: now request zone status and zone names
                self.hass.async_create_task(self._request_zone_status())
                self.hass.async_create_task(self._request_zone_names())
            except Exception as e:
                _LOGGER.error("Failed parsing installed zones: %s", e)
            return

        # ── 260: Zone names ───────────────────────────────────────────────────
        # Each param is the name for zone (start_order + index).
        if data_id == 260 and params:
            for i, name in enumerate(params):
                zone_num = start_order + i
                self._ensure_zone(zone_num)
                clean_name = _decode_hebrew(name).strip()
                self.zones[zone_num]["name"] = clean_name if clean_name else f"PIMA Zone {zone_num}"
                _LOGGER.warning("Zone %s name: %s", zone_num, self.zones[zone_num]["name"])
            if msg.get("more") == "yes":
                last_zone = start_order + len(params) - 1
                self.hass.async_create_task(
                    self._request_zone_names(start_order=last_zone + 1)
                )
            else:
                # All zone names received — notify binary_sensor to update registry
                self.hass.bus.async_fire("pima_zone_names_updated", {})
            return

        # ── 2149: Zone status ─────────────────────────────────────────────────
        # Each param is a hex string encoding both zone number and status bits:
        #   Format:  B B6 B5 B4 B3 B2 | B1 B0
        #            [status bits]     | [zone number (0-based index)]
        #
        # The zone number occupies the LOW 2 hex digits (1 byte = bits 0..7),
        # and the status bits occupy the remaining HIGH bytes.
        #
        # Example: "80005"  → zone number = 0x05 = 5, status = 0x800 (bit 11 = Open)
        #          "800C"   → zone number = 0x0C = 12, status = 0x80 (bit 7 = Manual Bypassed)
        #          "A0019"  → zone number = 0x19 = 25, status = 0xA00 (bits 9+11 = Alarmed+Open)
        #          "81B"    → zone number = 0x1B = 27, status = 0x8 (bit 3 = Cut/Tamper)
        #
        # Per Appendix C bit definitions:
        #   0: Supervision Loss   8: Auto Bypassed
        #   1: Low Battery        9: Alarmed
        #   2: Short (wired)     10: Armed
        #   3: Cut (Tamper)      11: Open
        #   4: Soak              12: Duress
        #   5: Chime             13: Fire
        #   6: Anti Mask         14: Medical
        #   7: Manual Bypassed   15: Panic
        if data_id == 2149 and params:
            try:
                BIT_SUPERVISION  = 0
                BIT_LOW_BATTERY  = 1
                BIT_SHORT        = 2
                BIT_CUT_TAMPER   = 3
                BIT_SOAK         = 4
                BIT_CHIME        = 5
                BIT_ANTI_MASK    = 6
                BIT_MAN_BYPASS   = 7
                BIT_AUTO_BYPASS  = 8
                BIT_ALARMED      = 9
                BIT_ARMED        = 10
                BIT_OPEN         = 11
                BIT_DURESS       = 12
                BIT_FIRE         = 13
                BIT_MEDICAL      = 14
                BIT_PANIC        = 15

                for raw in params:
                    value = int(str(raw), 16)
                    zone_num = value & 0xFF          # low byte = zone number
                    status   = (value >> 8) & 0xFFFF # high bytes = status bits

                    if zone_num == 0:
                        continue
                    if self.installed_zones and zone_num > self.installed_zones:
                        continue

                    self._ensure_zone(zone_num)
                    z = self.zones[zone_num]

                    def bit(n):
                        return bool(status & (1 << n))

                    z["supervision_loss"] = bit(BIT_SUPERVISION)
                    z["low_battery"]      = bit(BIT_LOW_BATTERY)
                    z["short"]            = bit(BIT_SHORT)
                    z["cut_tamper"]       = bit(BIT_CUT_TAMPER)
                    z["soak"]             = bit(BIT_SOAK)
                    z["chime"]            = bit(BIT_CHIME)
                    z["anti_mask"]        = bit(BIT_ANTI_MASK)
                    z["manual_bypassed"]  = bit(BIT_MAN_BYPASS)
                    z["auto_bypassed"]    = bit(BIT_AUTO_BYPASS)
                    z["alarmed"]          = bit(BIT_ALARMED)
                    z["armed"]            = bit(BIT_ARMED)
                    z["open"]             = bit(BIT_OPEN)
                    z["duress"]           = bit(BIT_DURESS)
                    z["fire"]             = bit(BIT_FIRE)
                    z["medical"]          = bit(BIT_MEDICAL)
                    z["panic"]            = bit(BIT_PANIC)

                    self.hass.bus.async_fire(
                        "pima_zone_update",
                        self._zone_event_payload(zone_num),
                    )
                    _LOGGER.warning(
                        "Zone %s status updated: open=%s alarmed=%s bypassed=%s",
                        zone_num, z["open"], z["alarmed"], z["manual_bypassed"],
                    )

                # Handle pagination ("more":"yes")
                if msg.get("more") == "yes":
                    _LOGGER.warning("2149 has more data — but zone status is sparse; no follow-up needed.")
                else:
                    # 2149 is sparse — zones absent from the response are closed with no flags.
                    # Fire a zone_update for them so they become available in HA.
                    updated_zones = set()
                    for raw in params:
                        try:
                            updated_zones.add(int(str(raw), 16) & 0xFF)
                        except Exception:
                            pass

                    for zone_num in range(1, self.installed_zones + 1):
                        if zone_num not in updated_zones:
                            self._ensure_zone(zone_num)
                            # Confirm all flags are False (clean closed state)
                            z = self.zones[zone_num]
                            for flag in ("open", "alarmed", "manual_bypassed", "auto_bypassed",
                                         "supervision_loss", "low_battery", "short", "cut_tamper",
                                         "soak", "chime", "anti_mask", "duress", "fire", "medical", "panic"):
                                z.setdefault(flag, False)
                            self.hass.bus.async_fire(
                                "pima_zone_update",
                                self._zone_event_payload(zone_num),
                            )

            except Exception as e:
                _LOGGER.error("Failed parsing zone status 2149: %s | params=%s", e, params)
            return

    async def _send_init_requests(self):
        """Send initial DATA-REQs after the panel handshake (first null frame)."""
        try:
            # 2310 = partition key status
            await self.send({
                "frame_type": "DATA-REQ",
                "counter": self.counter,
                "account": self.account,
                "password": self.password,
                "id": 2310,
                "start_order": 1,
                "stop_order": 16,
            })
            _LOGGER.warning("TX DATA-REQ 2310 (partition status)")
            self.counter = self._next_counter(self.counter)

            # 2148 = installed zones count (2149 zone status is chained after this responds)
            await self.send({
                "frame_type": "DATA-REQ",
                "counter": self.counter,
                "account": self.account,
                "password": self.password,
                "id": 2148,
                "start_order": 1,
                "stop_order": 1,
            })
            _LOGGER.warning("TX DATA-REQ 2148 (installed zones)")
            self.counter = self._next_counter(self.counter)

        except Exception as e:
            _LOGGER.exception("Init DATA-REQ send failed: %s", e)

    async def _request_zone_status(self):
        """Request current zone status (sparse — only non-closed zones returned)."""
        _LOGGER.warning("Requesting zone status (2149)")
        await self.send({
            "frame_type": "DATA-REQ",
            "counter": self.counter,
            "account": self.account,
            "password": self.password,
            "id": 2149,
            "start_order": 1,
        })
        self.counter = self._next_counter(self.counter)

    async def _request_zone_names(self, start_order=1):
        """Request zone names from the panel in batches of up to 64."""
        stop_order = min(start_order + 63, self.installed_zones)
        _LOGGER.warning("Requesting zone names %s to %s", start_order, stop_order)
        await self.send({
            "frame_type": "DATA-REQ",
            "counter": self.counter,
            "account": self.account,
            "password": self.password,
            "id": 260,
            "start_order": start_order,
            "stop_order": stop_order,
        })
        self.counter = self._next_counter(self.counter)

    async def send_operation(self, optype, partition=1, order=None):
        payload = {
            "frame_type": "OPERATION",
            "counter": self.counter,
            "account": self.account,
            "password": self.password,
            "optype": int(optype),
            "opclass": 1,
            "partition": int(partition),
        }

        # Per Appendix B, arm/disarm operations use order=0.
        # Output operations (35/36) use a specific output number as order.
        if order is not None:
            payload["order"] = int(order)
        elif int(optype) in (12, 13, 14, 15, 16, 17, 43):
            payload["order"] = 0

        await self.send(payload)
        self.counter = self._next_counter(self.counter)

    @staticmethod
    def _next_counter(counter):
        counter += 1
        if counter > 9999:
            counter = 1
        return counter
