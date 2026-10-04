"""Data models for Narwal vacuum state."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from .const import ROOM_SUB_TYPE_NAMES, WorkingStatus

_LOGGER = logging.getLogger(__name__)


@dataclass
class CommandResponse:
    """Parsed command response."""

    result_code: int = 0
    success: bool = False
    raw: bytes = b""
    data: bytes = b""

    @classmethod
    def from_payload(cls, payload: bytes) -> CommandResponse:
        """Parse response: field 1 (varint) = success, field 2 (bytes) = data payload."""
        resp = cls(raw=payload)
        fields = parse_protobuf_fields(payload)
        if 1 in fields and isinstance(fields[1], int):
            resp.result_code = fields[1]
            resp.success = resp.result_code == 1
        if 2 in fields and isinstance(fields[2], bytes):
            resp.data = fields[2]
        return resp


@dataclass
class NarwalState:
    """Current vacuum state assembled from status broadcasts."""

    working_status: WorkingStatus = WorkingStatus.UNKNOWN
    battery_level: float = 0.0
    is_cleaning: bool = False
    is_paused: bool = False
    is_returning: bool = False
    is_docked: bool = False
    elapsed_time: int = 0
    cleaned_area: int = 0
    # Freo X Plus only (working_status push): task progress in percent and
    # the room currently being cleaned. None when unknown.
    progress: float | None = None
    current_room_id: int | None = None
    # Freo X Plus: last fault reported in base_status field 1, kept while
    # the task it interrupted stays paused. None = no fault.
    error_code: int | None = None
    error_reason: str | None = None
    device_reachable: bool = False
    # Freo X Plus firmware uses a different working-status enum layout;
    # set by NarwalClient from the product key.
    freo_x_plus: bool = False

    # Raw protobuf data for fields we don't fully decode yet
    raw_base_status: dict = field(default_factory=dict)
    raw_working_status: dict = field(default_factory=dict)

    def update_base_status(self, payload: bytes) -> None:
        """Update from robot_base_status protobuf.

        The payload field layout (confirmed via captures):
          field 2 (fixed32) = battery level as IEEE 754 float32
          field 3 (sub-message) = mode/state
            sub-field 1 = working status enum
            sub-field 2 = is_paused (in some captures, sub-field 4)
            sub-field 7 = is_returning
            sub-field 10 = dock sub-state (1=docked)
          field 13 (string) = user UUID
        """
        fields = parse_protobuf_fields(payload)
        self.raw_base_status = fields
        _LOGGER.debug(
            "Parsed base status fields: %s",
            {
                k: (v.hex() if isinstance(v, bytes) else v)
                for k, v in fields.items()
            },
        )

        # Field 2 = battery as IEEE 754 float32 (little-endian fixed32)
        if 2 in fields:
            val = fields[2]
            if isinstance(val, int):
                try:
                    self.battery_level = struct.unpack('<f', struct.pack('<I', val))[0]
                except Exception:
                    self.battery_level = float(val)
            elif isinstance(val, (float, int)):
                self.battery_level = float(val)

        # Field 3 = mode/state sub-message
        sub: dict = {}
        prev_status = self.working_status
        if 3 in fields and isinstance(fields[3], bytes):
            sub = parse_protobuf_fields(fields[3])
            if 1 in sub and isinstance(sub[1], int):
                raw_status = sub[1]
                if self.freo_x_plus:
                    raw_status = _translate_freo_x_plus_status(raw_status, sub, fields)
                try:
                    self.working_status = WorkingStatus(raw_status)
                except ValueError:
                    _LOGGER.warning(
                        "Unknown working_status value: %d, sub-fields: %s",
                        raw_status, sub,
                    )
                    self.working_status = WorkingStatus.UNKNOWN

                # Disambiguate raw_status=2: this firmware reuses the
                # PAUSED slot for any active task. Captured payloads
                # during scheduled clean: {1: 2, 4: 8/7/3} with no
                # is_paused flag (sub[2]). Use the explicit flags:
                #   sub[2] = is_paused  (1 = actually paused)
                #   sub[7] = is_returning (1 = en route to dock)
                # Without either flag set, treat as CLEANING.
                if not self.freo_x_plus and self.working_status == WorkingStatus.PAUSED:
                    if sub.get(7, 0) == 1:
                        self.working_status = WorkingStatus.RETURNING
                    elif sub.get(2, 0) != 1:
                        self.working_status = WorkingStatus.CLEANING

        if self.freo_x_plus:
            report = _parse_error_report(fields.get(1))
            if report:
                self.error_code, self.error_reason = report
                _LOGGER.warning(
                    "Robot fault 0x%08X: %s", self.error_code, self.error_reason,
                )
            elif self.working_status != WorkingStatus.PAUSED:
                # The report is pushed once; the robot then just shows the
                # interrupted task as paused until it resumes or is recalled.
                self.error_code = self.error_reason = None
            if self.error_code is not None:
                self.working_status = WorkingStatus.ERROR

        # Derive boolean flags from working_status (always, not just when
        # field 3 is present) so they stay in sync even if field 3 parsing
        # fails due to protobuf format variations.
        self.is_paused = self.working_status == WorkingStatus.PAUSED
        self.is_cleaning = self.working_status in (
            WorkingStatus.CLEANING, WorkingStatus.CLEANING_ALT
        )
        self.is_returning = self.working_status == WorkingStatus.RETURNING
        self.is_docked = self.working_status in (
            WorkingStatus.DOCKED, WorkingStatus.CHARGED,
            WorkingStatus.CHARGING, WorkingStatus.MOP_WASHING,
            WorkingStatus.MOP_DRYING, WorkingStatus.DUST_COLLECTING,
        )
        # Log state transitions at WARNING so we can diagnose enum
        # mismapping without asking the user to change log levels.
        log = _LOGGER.warning if self.working_status != prev_status else _LOGGER.debug
        log(
            "State update: status=%s battery=%.1f%% sub_fields=%s",
            self.working_status.name, self.battery_level, sub,
        )

    rooms: list[RoomInfo] = field(default_factory=list)

    def update_rooms_from_map(self, map_data: bytes) -> None:
        """Extract room list from map protobuf field 12 (repeated room entries).

        Field 12 contains user-visible rooms with proper sub_type classification
        and optional user-assigned names.  It appears as a repeated protobuf field
        so we must use parse_protobuf_repeated to collect all occurrences.
        """
        self.rooms = _parse_rooms_from_field12(map_data)
        _LOGGER.debug("Parsed %d rooms from map data (field 12)", len(self.rooms))

    def update_working_status(self, payload: bytes) -> None:
        """Update elapsed_time/cleaned_area from working_status protobuf.

        This firmware keeps sending working_status pushes even after
        cleaning ends and the vacuum returns to the dock (elapsed_time
        stays frozen at the cycle's final value). The push presence is
        therefore NOT a reliable "actively cleaning" signal — base_status
        is authoritative for working_status enum, and is now resolved
        correctly via update_base_status. Leave the enum alone here.
        """
        fields = parse_protobuf_fields(payload)
        self.raw_working_status = fields

        if 3 in fields and isinstance(fields[3], int):
            self.elapsed_time = fields[3]

        if self.freo_x_plus:
            # Freo X Plus layout (captured):
            #   1 (float32) progress %, 2 (float32) cleaned area m²,
            #   3 elapsed s, 5 {1: current room id}; 11/13/15 are constants
            #   (2700 / 18000 / 600), so field 13 is NOT the cleaned area.
            # Fields 1 and 2 only appear once the robot reaches the first
            # room; a new task restarts without them.
            self.progress = _as_float32(fields.get(1)) or 0.0
            self.cleaned_area = round((_as_float32(fields.get(2)) or 0.0) * 10000)
            room = fields.get(5)
            sub = parse_protobuf_fields(room) if isinstance(room, bytes) else {}
            self.current_room_id = sub.get(1) if isinstance(sub.get(1), int) else None
        elif 13 in fields and isinstance(fields[13], int):
            self.cleaned_area = fields[13]


def _parse_error_report(raw: object) -> tuple[int, str] | None:
    """Decode a Freo X Plus fault report (base_status field 1).

    Empty while all is well. On a fault the robot pushes once
    {1: code, 2: level, 3: Chinese diagnostic text ending with
    "产生错误的原因:<English cause>"}, e.g. code 0x02020042 with
    "right mop uninstall when mopping".
    """
    if not isinstance(raw, bytes) or not raw:
        return None
    try:
        report = parse_protobuf_fields(raw)
    except (IndexError, ValueError):
        return None
    code = report.get(1)
    if not isinstance(code, int) or not code:
        return None
    text = report.get(3)
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    reason = text.rsplit("原因:", 1)[-1].strip() if isinstance(text, str) else ""
    return code, reason or f"Error 0x{code:08X}"


def _as_float32(raw: object) -> float | None:
    """Reinterpret a fixed32 field (parsed as int) as an IEEE 754 float."""
    if not isinstance(raw, int):
        return None
    return round(struct.unpack("<f", struct.pack("<I", raw & 0xFFFFFFFF))[0], 2)


def _translate_freo_x_plus_status(raw_status: int, sub: dict, fields: dict) -> int:
    """Map a Freo X Plus working-status value onto the Ultra WorkingStatus enum.

    Captured on a Freo X Plus while driving it from the app
    (sub = field 3 of robot_base_status, fields[11] = 2 on the dock / 1 off it):
      {1: 1, 3: 1|6}        idle on the dock (1 right after docking)
      {1: 1, 3: 2|5|7}      idle off the dock (task stopped / abandoned)
      {1: 2, 4: 8|7|3}      vacuum task running (sub[4] = stage: 8 leaving
                            the dock, 7 heading to the room, 3 cleaning)
      {1: 2, 2: 1, 4: 3}    vacuum task paused
      {1: 3, 5: 12|11|7}    mop task running (sub[5] = stage)
      {1: 4, 7: 14|7}       vacuum-then-mop task running
      {1: 5, 6: 12|11|7}    vacuum & mop task running
      {1: 10, 10: 1|2}      returning to the dock (2 = docking manoeuvre)
    The task stage lives in sub-field (status + 2), so sub[7] is NOT an
    is_returning flag here; update_base_status skips the Ultra heuristics.
    """
    if raw_status == 1:
        if sub.get(3) in (1, 6) or fields.get(11) == 2:
            return WorkingStatus.DOCKED.value
        return WorkingStatus.STANDBY.value
    if raw_status in (2, 3, 4, 5):
        if sub.get(2) == 1:
            return WorkingStatus.PAUSED.value
        return WorkingStatus.CLEANING.value
    if raw_status == 10:
        return WorkingStatus.RETURNING.value
    return raw_status


@dataclass
class RoomInfo:
    """A room discovered from the vacuum's map.

    Parsed from map response field 12 (repeated). Each entry contains:
      field 1: room_id
      field 2: room_sub_type (ROOM_SUB_TYPE enum)
      field 3: user-assigned name (UTF-8, empty if not renamed by user)
      field 4: category (1=room, 2=utility/small space)
      field 8: instance_index (1-based, for numbering duplicates)
    """

    room_id: int
    room_sub_type: int = 0
    name: str = ""
    category: int = 0
    instance_index: int = 0

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        base = ROOM_SUB_TYPE_NAMES.get(self.room_sub_type, f"Room {self.room_id}")
        if self.instance_index > 1:
            return f"{base} {self.instance_index}"
        return base


def _parse_rooms_from_field12(map_data: bytes) -> list[RoomInfo]:
    """Extract rooms from repeated field 12 entries in the map protobuf."""
    all_fields = parse_protobuf_repeated(map_data)
    entries = all_fields.get(12, [])
    rooms: list[RoomInfo] = []
    for entry in entries:
        if not isinstance(entry, bytes):
            continue
        rf = parse_protobuf_fields(entry)
        room_id = rf.get(1)
        if not isinstance(room_id, int):
            continue
        name_raw = rf.get(3, "")
        if isinstance(name_raw, bytes):
            try:
                name = name_raw.decode("utf-8")
            except UnicodeDecodeError:
                name = ""
        elif isinstance(name_raw, str):
            name = name_raw
        else:
            name = ""
        rooms.append(RoomInfo(
            room_id=room_id,
            room_sub_type=rf.get(2, 0) if isinstance(rf.get(2), int) else 0,
            name=name,
            category=rf.get(4, 0) if isinstance(rf.get(4), int) else 0,
            instance_index=rf.get(8, 0) if isinstance(rf.get(8), int) else 0,
        ))
    return rooms


def parse_protobuf_repeated(data: bytes) -> dict[int, list]:
    """Parse protobuf collecting ALL occurrences of each field as a list.

    Standard parse_protobuf_fields keeps only the last value per field number,
    which silently drops repeated fields.  This variant returns
    {field_num: [value1, value2, ...]} so repeated entries are preserved.
    """
    fields: dict[int, list] = {}
    idx = 0
    while idx < len(data):
        tag_byte = data[idx]
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3

        if tag_byte & 0x80:
            tag_val = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                tag_val |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            wire_type = tag_val & 0x07
            field_num = tag_val >> 3
        else:
            idx += 1

        if wire_type == 0:
            val = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                val |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:
            if idx + 8 <= len(data):
                fields.setdefault(field_num, []).append(
                    int.from_bytes(data[idx : idx + 8], "little")
                )
                idx += 8
        elif wire_type == 2:
            length = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                length |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            if idx + length <= len(data):
                fields.setdefault(field_num, []).append(data[idx : idx + length])
                idx += length
            else:
                break
        elif wire_type == 5:
            if idx + 4 <= len(data):
                fields.setdefault(field_num, []).append(
                    int.from_bytes(data[idx : idx + 4], "little")
                )
                idx += 4
        else:
            break
    return fields


def parse_protobuf_fields(data: bytes) -> dict:
    """Minimal protobuf field parser. Returns {field_num: value}.

    Handles varint (wire type 0), 64-bit (1), length-delimited (2),
    and 32-bit (5). For length-delimited fields, returns raw bytes
    if they don't look like a string.
    """
    fields: dict = {}
    idx = 0
    while idx < len(data):
        if idx >= len(data):
            break
        tag_byte = data[idx]
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3

        # Handle multi-byte field numbers
        if tag_byte & 0x80:
            tag_val = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                tag_val |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            wire_type = tag_val & 0x07
            field_num = tag_val >> 3
        else:
            idx += 1

        if wire_type == 0:  # varint
            val = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                val |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            fields[field_num] = val

        elif wire_type == 1:  # 64-bit fixed
            if idx + 8 <= len(data):
                fields[field_num] = int.from_bytes(data[idx:idx+8], 'little')
                idx += 8

        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while idx < len(data):
                b = data[idx]
                length |= (b & 0x7F) << shift
                shift += 7
                idx += 1
                if b & 0x80 == 0:
                    break
            if idx + length <= len(data):
                raw = data[idx:idx+length]
                try:
                    text = raw.decode('utf-8')
                    if text.isprintable():
                        fields[field_num] = text
                    else:
                        fields[field_num] = raw
                except (UnicodeDecodeError, ValueError):
                    fields[field_num] = raw
                idx += length
            else:
                break

        elif wire_type == 5:  # 32-bit fixed
            if idx + 4 <= len(data):
                fields[field_num] = int.from_bytes(data[idx:idx+4], 'little')
                idx += 4

        else:
            break  # unknown wire type

    return fields
