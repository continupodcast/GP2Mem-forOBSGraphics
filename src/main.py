import json
import os
import threading
import struct
import re
import time
import queue
import ctypes

from dataclasses import dataclass
from typing import Optional, Any, Dict, List, Tuple

import dearpygui.dearpygui as dpg

import pymem
import pymem.process
import pymem.memory

from const import *

# Session clock: base_anchor + this offset = address of session elapsed timer (ms, u32)
# Freezes when game or emulator is paused — perfect for lap time calculation.
SESSION_CLOCK_OFFSET = 0x6C24

from helpers import (
    ordinal,
    fmt_bits_u8,
    fmt_time32,
    parse_offset_hex,
    parse_value_for_display,
    clamp_value,
    format_scalar_for_entry,
    unpack_typed,
    pack_typed,
    racepos_to_place,
    enum_display_list,
    enum_value_from_display,
)

def fmt_time32_plain(v):
    # Converts raw time32 intro a string 'm:ss.mmm'
    s = fmt_time32(v)
    return s.split(" (")[0] if s else None

# ----------------------------
# Native message boxes (Windows)
# ----------------------------
_user32 = None
try:
    _user32 = ctypes.windll.user32
except Exception:
    _user32 = None

MB_OK = 0x00000000
MB_OKCANCEL = 0x00000001
MB_YESNO = 0x00000004

MB_ICONERROR = 0x00000010
MB_ICONQUESTION = 0x00000020
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040

IDOK = 1
IDCANCEL = 2
IDYES = 6
IDNO = 7


def msg_info(title: str, text: str):
    if _user32:
        _user32.MessageBoxW(0, text, title, MB_OK | MB_ICONINFORMATION)
    else:
        print(f"[INFO] {title}: {text}")


def msg_warn(title: str, text: str):
    if _user32:
        _user32.MessageBoxW(0, text, title, MB_OK | MB_ICONWARNING)
    else:
        print(f"[WARN] {title}: {text}")


def msg_error(title: str, text: str):
    if _user32:
        _user32.MessageBoxW(0, text, title, MB_OK | MB_ICONERROR)
    else:
        print(f"[ERROR] {title}: {text}")


def msg_yesno(title: str, text: str) -> bool:
    if _user32:
        r = _user32.MessageBoxW(0, text, title, MB_YESNO | MB_ICONQUESTION)
        return r == IDYES
    # fallback
    print(f"[YESNO] {title}: {text} (default NO)")
    return False


# Specs
@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    offset: Optional[int]
    typ: Optional[str]
    count: int = 1
    display: str = "dec"
    bit_map_key: Optional[str] = None
    pretty_kind: Optional[str] = None
    widget: str = "entry"  # entry | enum | spin | computed
    enum: Optional[List[Tuple[int, str]]] = None
    spin_from: int = 0
    spin_to: int = 255


# Struct layout parsing
def parse_struct_layout(text: str) -> List[Dict]:
    fields = []
    type_re = re.compile(r"^(u8|i8|u16|i16|u32|i32|f32)(?:\[(\d+)\])?$")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        off_s, name, typ_s = parts[0], parts[1], parts[2]
        off = int(off_s, 16)
        m = type_re.match(typ_s)
        if not m:
            continue
        typ = m.group(1)
        cnt = int(m.group(2) or "1")

        display = "dec"
        pretty = None
        bit_key = name if name in BIT_MAPS else None
        enum = ENUM_FIELDS.get(name)

        if bit_key:
            display = "hex"
        if name.lower().startswith("time"):
            display = "hex"
            pretty = "time32"
        if name.lower().startswith("timer"):
            display = "hex"
        if name.startswith("p") and typ in ("i32",):
            typ = "u32"
            display = "hex"
        if name.startswith("p") and typ == "u32":
            display = "hex"
        if name.startswith("field_"):
            display = "hex"
        if typ in ("u32", "i32") and name.startswith("p"):
            display = "hex"
        if typ in ("u8", "i8") and (name.startswith("field_") or name.startswith("flags") or name.endswith("_")):
            display = "hex"

        fields.append({
            "offset": off,
            "name": name,
            "typ": typ,
            "count": cnt,
            "display": display,
            "pretty": pretty,
            "bit_key": bit_key,
            "enum": enum,
        })

    fields.append({
        "offset": 0x14,
        "name": "speed_raw_u32_alias",
        "typ": "u32",
        "count": 1,
        "display": "hex",
        "pretty": None,
        "bit_key": None,
        "enum": None,
    })

    fields.sort(key=lambda f: (f["offset"], f["name"]))
    return fields


# UI rows
class BaseRowDPG:
    def set_writable(self, writable: bool):
        raise NotImplementedError

    def refresh_from_snapshot(self, snap: Dict):
        raise NotImplementedError


class FieldRowDPG(BaseRowDPG):
    def __init__(self, app, parent_table, spec: FieldSpec):
        self.app = app
        self.spec = spec

        self.input_ids: List[int] = []
        self.value_text_id: Optional[int] = None
        self.pretty_text_id: Optional[int] = None
        self.write_btn_id: Optional[int] = None
        self.all_btn_id: Optional[int] = None

        self._enum_val_to_disp: Dict[int, str] = {}
        self._enum_disp_to_val: Dict[str, int] = {}

        with dpg.table_row(parent=parent_table):
            dpg.add_text(spec.label)
            if spec.typ is None or spec.widget == "computed":
                self.value_text_id = dpg.add_text("")
            else:
                if spec.widget == "enum":
                    if not spec.enum:
                        raise ValueError(f"enum row '{spec.key}' missing enum list")
                    values = enum_display_list(spec.enum)
                    for v, name in spec.enum:
                        disp = f"{int(v)}: {name}"
                        self._enum_val_to_disp[int(v)] = disp
                        self._enum_disp_to_val[disp] = int(v)
                    combo_id = dpg.add_combo(
                        items=values,
                        default_value=values[0] if values else "",
                        width=220,
                        callback=self._on_combo_change,
                    )
                    self.input_ids = [combo_id]
                elif spec.widget == "spin":
                    iid = dpg.add_input_int(
                        default_value=0,
                        width=120,
                        min_value=int(spec.spin_from),
                        max_value=int(spec.spin_to),
                        on_enter=True,
                        callback=self._on_enter,
                    )
                    self.input_ids = [iid]
                else:
                    if spec.count == 1:
                        iid = dpg.add_input_text(
                            default_value="",
                            width=140,
                            on_enter=True,
                            callback=self._on_enter,
                        )
                        self.input_ids = [iid]
                    else:
                        with dpg.group(horizontal=True):
                            for _ in range(spec.count):
                                iid = dpg.add_input_text(
                                    default_value="",
                                    width=90,
                                    on_enter=True,
                                    callback=self._on_enter,
                                )
                                self.input_ids.append(iid)
            if spec.typ is not None and spec.offset is not None:
                self.write_btn_id = dpg.add_button(label="Write", width=60, callback=self._on_write)
                self.all_btn_id = dpg.add_button(label="All", width=45, callback=self._on_all)
            else:
                dpg.add_text("")
                dpg.add_text("")

            # Pretty
            self.pretty_text_id = dpg.add_text("")

        self.set_writable(self.app.enable_writes)

    def _on_enter(self, _sender, _app_data, _user_data=None):
        self.write_selected()

    def _on_combo_change(self, _sender, _app_data, _user_data=None):
        self.write_selected()

    def _on_write(self, _sender, _app_data, _user_data=None):
        self.write_selected()

    def _on_all(self, _sender, _app_data, _user_data=None):
        self.write_all()

    def set_writable(self, writable: bool):
        if self.spec.typ is None:
            return

        if self.spec.widget == "enum":
            for iid in self.input_ids:
                dpg.configure_item(iid, enabled=bool(writable))
        else:
            # input_text / input_int support readonly
            for iid in self.input_ids:
                try:
                    dpg.configure_item(iid, readonly=not bool(writable), enabled=True)
                except Exception:
                    dpg.configure_item(iid, enabled=bool(writable))

        if self.write_btn_id is not None:
            dpg.configure_item(self.write_btn_id, enabled=bool(writable))
        if self.all_btn_id is not None:
            dpg.configure_item(self.all_btn_id, enabled=bool(writable))

    def _is_editing(self) -> bool:
        return any(dpg.is_item_active(iid) for iid in self.input_ids)

    def refresh_from_snapshot(self, snap: Dict):
        spec = self.spec
        buf = snap.get("buf", b"")

        if spec.typ is None:
            v = snap.get(spec.key, None)
            if self.value_text_id is not None:
                dpg.set_value(self.value_text_id, "" if v is None else str(v))
            if self.pretty_text_id is not None:
                dpg.set_value(self.pretty_text_id, "")
            return

        if self._is_editing():
            return

        if spec.offset is None:
            return

        try:
            val = unpack_typed(buf, spec.offset, spec.typ, spec.count)
        except Exception:
            return

        # set inputs
        if spec.widget == "enum" and spec.count == 1 and self.input_ids:
            vv = int(val) & 0xFF
            disp = self._enum_val_to_disp.get(vv, str(vv))
            dpg.set_value(self.input_ids[0], disp)
        else:
            if spec.count == 1 and self.input_ids:
                if spec.widget == "spin":
                    dpg.set_value(self.input_ids[0], int(val))
                else:
                    dpg.set_value(self.input_ids[0], format_scalar_for_entry(val, spec.typ, spec.display))
            elif spec.count > 1 and len(self.input_ids) == spec.count:
                for i, v in enumerate(val):
                    dpg.set_value(self.input_ids[i], format_scalar_for_entry(v, spec.typ, spec.display))

        # pretty
        pretty = ""
        if spec.pretty_kind == "time32" and spec.count == 1:
            pretty = fmt_time32(int(val))
        if spec.bit_map_key and spec.count == 1:
            pretty = fmt_bits_u8(int(val) & 0xFF, BIT_MAPS.get(spec.bit_map_key))
        if spec.widget == "enum" and spec.count == 1 and spec.enum:
            vv = int(val) & 0xFF
            name = dict(spec.enum).get(vv)
            if name:
                pretty = f"{vv}: {name}"

        if self.pretty_text_id is not None:
            dpg.set_value(self.pretty_text_id, pretty)

    def _parse_values(self) -> bytes:
        spec = self.spec
        if spec.typ is None or spec.offset is None:
            raise ValueError("Field is not writable.")

        if spec.widget == "enum":
            raw = dpg.get_value(self.input_ids[0])
            v = enum_value_from_display(raw)
            v = clamp_value(spec.typ, v)
            return pack_typed(spec.typ, v)

        vals: List[Any] = []
        raw_vals = [dpg.get_value(iid) for iid in self.input_ids]

        for rv in raw_vals:
            s = rv.strip() if isinstance(rv, str) else str(rv).strip()
            if spec.typ == "f32":
                v = float(s)
            else:
                v = parse_value_for_display(s, spec.display)
            v = clamp_value(spec.typ, v)
            vals.append(v)

        return pack_typed(spec.typ, vals if spec.count > 1 else vals[0])

    def write_selected(self):
        if not self.app._require_writes_enabled():
            return
        idx = self.app.selected_index
        if idx is None:
            msg_info("No selection", "Select a car first.")
            return

        try:
            payload = self._parse_values()
        except Exception as e:
            msg_error("Bad value", str(e))
            return

        self.app.write_field_bytes(idx, self.spec.offset, payload)
        self.app.refresh_once()

    def write_all(self):
        if not self.app._require_writes_enabled():
            return

        if not msg_yesno("Confirm", f"Write '{self.spec.label}' to ALL cars?"):
            return

        try:
            payload = self._parse_values()
        except Exception as e:
            msg_error("Bad value", str(e))
            return

        for i in range(self.app.car_count):
            self.app.write_field_bytes(i, self.spec.offset, payload)
        self.app.refresh_once()


class BitsRowDPG(BaseRowDPG):
    def __init__(self, app, parent_table, label: str, offset: int, bit_map_key: str):
        self.app = app
        self.label = label
        self.offset = offset
        self.bit_map_key = bit_map_key
        self.mapping = BIT_MAPS.get(bit_map_key, {})

        self.raw_input_id: Optional[int] = None
        self.pretty_text_id: Optional[int] = None
        self.write_btn_id: Optional[int] = None
        self.all_btn_id: Optional[int] = None

        self.bit_check_ids: Dict[int, int] = {}
        self._updating = False

        bits = sorted(self.mapping.keys())
        cols_per_row = 2 if len(bits) > 6 else 4

        with dpg.table_row(parent=parent_table):
            dpg.add_text(label)
            with dpg.group():
                self.raw_input_id = dpg.add_input_text(
                    default_value="0",
                    width=120,
                    on_enter=True,
                    callback=self._on_raw_enter_text,
                )
                if bits:
                    dpg.add_separator()
                    for r in range(0, len(bits), cols_per_row):
                        with dpg.group(horizontal=True):
                            for bit in bits[r:r + cols_per_row]:
                                cid = dpg.add_checkbox(
                                    label=f"b{bit} {self.mapping[bit]}",
                                    callback=self._on_toggle,
                                    user_data=bit,
                                )
                                self.bit_check_ids[bit] = cid
            self.write_btn_id = dpg.add_button(label="Write", width=60, callback=self._on_write)
            self.all_btn_id = dpg.add_button(label="All", width=45, callback=self._on_all)
            self.pretty_text_id = dpg.add_text("")

        self.set_writable(self.app.enable_writes)

    def _parse_byte_text(self, s: str) -> int:
        s = (s or "").strip().lower()
        if not s:
            raise ValueError("empty")

        if s.startswith("0b"):
            v = int(s[2:], 2)
        elif s.startswith("0x"):
            v = int(s[2:], 16)
        else:
            v = int(s, 10)

        return v & 0xFF

    def _on_raw_enter_text(self, _sender, _app_data, _user_data=None):
        if self._updating:
            return
        try:
            v = self._parse_byte_text(str(dpg.get_value(self.raw_input_id)))
        except Exception:
            return
        self._set_value(v)
        self.write_selected()

    def set_writable(self, writable: bool):
        if self.raw_input_id is not None:
            dpg.configure_item(self.raw_input_id, readonly=not bool(writable), enabled=True)
        for cid in self.bit_check_ids.values():
            dpg.configure_item(cid, enabled=bool(writable))
        if self.write_btn_id is not None:
            dpg.configure_item(self.write_btn_id, enabled=bool(writable))
        if self.all_btn_id is not None:
            dpg.configure_item(self.all_btn_id, enabled=bool(writable))

    def _set_value(self, v: int):
        v &= 0xFF
        self._updating = True

        if self.raw_input_id is not None:
            dpg.set_value(self.raw_input_id, f"0b{v:08b}")

        for bit, cid in self.bit_check_ids.items():
            dpg.set_value(cid, bool(v & (1 << bit)))
        names = fmt_bits_u8(v, self.mapping)
        bin_s = f"{v:08b}"
        pretty = f"{bin_s}  {names}" if names else bin_s
        if self.pretty_text_id is not None:
            dpg.set_value(self.pretty_text_id, pretty)

        self._updating = False

    def _value_from_checks(self) -> int:
        v = 0
        for bit, cid in self.bit_check_ids.items():
            if dpg.get_value(cid):
                v |= (1 << bit)
        return v & 0xFF

    def _on_toggle(self, _sender, _app_data, _user_data=None):
        if self._updating:
            return
        v = self._value_from_checks()
        self._set_value(v)

    def _on_raw_enter(self, _sender, _app_data, _user_data=None):
        if self._updating:
            return
        if self.raw_input_id is None:
            return
        try:
            v = int(dpg.get_value(self.raw_input_id)) & 0xFF
        except Exception:
            return
        self._set_value(v)
        self.write_selected()

    def refresh_from_snapshot(self, snap: Dict):
        if self.raw_input_id is not None and dpg.is_item_active(self.raw_input_id):
            return
        buf = snap.get("buf", b"")
        try:
            v = unpack_typed(buf, self.offset, "u8", 1)
        except Exception:
            return
        self._set_value(int(v))

    def _on_write(self, _sender, _app_data, _user_data=None):
        self.write_selected()

    def _on_all(self, _sender, _app_data, _user_data=None):
        self.write_all()

    def write_selected(self):
        if not self.app._require_writes_enabled():
            return
        idx = self.app.selected_index
        if idx is None:
            msg_info("No selection", "Select a car first.")
            return
        if self.raw_input_id is None:
            return
        try:
            v = self._parse_byte_text(str(dpg.get_value(self.raw_input_id)))
        except Exception as e:
            msg_error("Bad value", str(e))
            return
        self.app.write_field_bytes(idx, self.offset, pack_typed("u8", v))
        self.app.refresh_once()

    def write_all(self):
        if not self.app._require_writes_enabled():
            return
        if not msg_yesno("Confirm", f"Write '{self.label}' to ALL cars?"):
            return
        if self.raw_input_id is None:
            return
        try:
            v = self._parse_byte_text(str(dpg.get_value(self.raw_input_id)))
        except Exception as e:
            msg_error("Bad value", str(e))
            return
        payload = pack_typed("u8", v)
        for i in range(self.app.car_count):
            self.app.write_field_bytes(i, self.offset, payload)
        self.app.refresh_once()


class PitStopsRowDPG(BaseRowDPG):
    def __init__(self, app, parent_table):
        self.app = app
        self.offset = OFS["numPitStops"]

        self.count_combo_id: Optional[int] = None
        self.lap_input_ids: List[int] = []
        self.pretty_text_id: Optional[int] = None
        self.write_btn_id: Optional[int] = None
        self.all_btn_id: Optional[int] = None

        with dpg.table_row(parent=parent_table):
            dpg.add_text("Pit stops")
            with dpg.group():
                with dpg.group(horizontal=True):
                    dpg.add_text("Count")
                    self.count_combo_id = dpg.add_combo(
                        items=["0", "1", "2", "3"],
                        default_value="0",
                        width=70,
                        callback=self._on_count_change,
                    )

                dpg.add_text("Laps")
                with dpg.group(horizontal=True):
                    iid1 = dpg.add_input_int(default_value=0, width=70, min_value=0, max_value=255,
                                             min_clamped=True, max_clamped=True, step=0, step_fast=0, on_enter=True)
                    iid2 = dpg.add_input_int(default_value=0, width=70, min_value=0, max_value=255,
                                             min_clamped=True, max_clamped=True, step=0, step_fast=0, on_enter=True)
                    self.lap_input_ids.extend([iid1, iid2])

                iid3 = dpg.add_input_int(default_value=0, width=70, min_value=0, max_value=255,
                                         min_clamped=True, max_clamped=True, step=0, step_fast=0, on_enter=True)
                self.lap_input_ids.append(iid3)
            self.write_btn_id = dpg.add_button(label="Write", width=60, callback=self._on_write)
            self.all_btn_id = dpg.add_button(label="All", width=45, callback=self._on_all)
            self.pretty_text_id = dpg.add_text("")

        self._update_state()
        self.set_writable(self.app.enable_writes)

    def _on_count_change(self, _sender, _app_data, _user_data=None):
        self._update_state()

    def _get_count(self) -> int:
        if self.count_combo_id is None:
            return 0
        try:
            c = int(dpg.get_value(self.count_combo_id))
        except Exception:
            c = 0
        return max(0, min(3, c))

    def _update_state(self):
        c = self._get_count()
        for i, iid in enumerate(self.lap_input_ids):
            dpg.configure_item(iid, enabled=(i < c))

    def set_writable(self, writable: bool):
        if self.count_combo_id is not None:
            dpg.configure_item(self.count_combo_id, enabled=bool(writable))

        for iid in self.lap_input_ids:
            dpg.configure_item(iid, readonly=not bool(writable), enabled=True)

        if self.write_btn_id is not None:
            dpg.configure_item(self.write_btn_id, enabled=bool(writable))
        if self.all_btn_id is not None:
            dpg.configure_item(self.all_btn_id, enabled=bool(writable))

        if writable:
            self._update_state()
        else:
            for iid in self.lap_input_ids:
                dpg.configure_item(iid, enabled=False)

    def refresh_from_snapshot(self, snap: Dict):
        if self.count_combo_id is not None and dpg.is_item_active(self.count_combo_id):
            return
        if any(dpg.is_item_active(iid) for iid in self.lap_input_ids):
            return

        buf = snap.get("buf", b"")
        b = buf[self.offset:self.offset + 4]
        if len(b) != 4:
            return

        c, p1, p2, p3 = b[0], b[1], b[2], b[3]

        if self.count_combo_id is not None:
            dpg.set_value(self.count_combo_id, str(max(0, min(3, int(c)))))

        dpg.set_value(self.lap_input_ids[0], int(p1))
        dpg.set_value(self.lap_input_ids[1], int(p2))
        dpg.set_value(self.lap_input_ids[2], int(p3))

        self._update_state()

        if self.pretty_text_id is not None:
            dpg.set_value(self.pretty_text_id, f"bytes: {c},{p1},{p2},{p3}")

    def _payload(self) -> bytes:
        c = self._get_count()
        laps: List[int] = []
        for i in range(3):
            if i < c:
                v = int(dpg.get_value(self.lap_input_ids[i]))
                v = max(0, min(255, v))
            else:
                v = 0
            laps.append(v)
        return bytes([c] + laps)

    def _on_write(self, _sender, _app_data, _user_data=None):
        self.write_selected()

    def _on_all(self, _sender, _app_data, _user_data=None):
        self.write_all()

    def write_selected(self):
        if not self.app._require_writes_enabled():
            return
        idx = self.app.selected_index
        if idx is None:
            msg_info("No selection", "Select a car first.")
            return
        self.app.write_field_bytes(idx, self.offset, self._payload())
        self.app.refresh_once()

    def write_all(self):
        if not self.app._require_writes_enabled():
            return
        if not msg_yesno("Confirm", "Write pit stops to ALL cars?"):
            return
        payload = self._payload()
        for i in range(self.app.car_count):
            self.app.write_field_bytes(i, self.offset, payload)
        self.app.refresh_once()

# Full Struct tab
class FullStructTabDPG:
    def __init__(self, app):
        self.app = app
        self.fields = parse_struct_layout(STRUCT_LAYOUT_TEXT)

        self.filter_input_id: Optional[int] = None
        self.status_text_id: Optional[int] = None
        self.table_id: Optional[int] = None

        self.visible_fields: List[Dict] = []
        self.value_text_ids: List[int] = []
        self.pretty_text_ids: List[int] = []
        self.name_select_ids: List[int] = []

        self.selected_row: Optional[int] = None
        self._last_click_row: Optional[int] = None
        self._last_click_time: float = 0.0

        self.modal_id: Optional[int] = None

    def build(self, parent):
        with dpg.group(parent=parent):
            with dpg.group(horizontal=True):
                dpg.add_text("Filter:")
                self.filter_input_id = dpg.add_input_text(
                    default_value="",
                    width=240,
                    callback=self._on_filter_change,
                )
                dpg.add_text("(matches name or offset like 0x2E8)")
                dpg.add_button(label="Edit Selected", callback=self._on_edit_selected)

            self.status_text_id = dpg.add_text("Select a row and click 'Edit Selected' (double-click simulated).")

            self.table_id = dpg.add_table(
                header_row=True,
                row_background=True,
                resizable=True,
                borders_innerV=True,
                borders_innerH=True,
                scrollY=True,
                height=-1,
            )
            dpg.add_table_column(label="Offset", parent=self.table_id, init_width_or_weight=80)
            dpg.add_table_column(label="Name", parent=self.table_id, init_width_or_weight=220)
            dpg.add_table_column(label="Type", parent=self.table_id, init_width_or_weight=60)
            dpg.add_table_column(label="Count", parent=self.table_id, init_width_or_weight=60)
            dpg.add_table_column(label="Value", parent=self.table_id, init_width_or_weight=240)
            dpg.add_table_column(label="Pretty", parent=self.table_id, init_width_or_weight=320)

        self._rebuild()

        #empty, built on demand
        self.modal_id = dpg.add_window(
            label="Edit Field",
            modal=True,
            show=False,
            no_resize=True,
            no_move=False,
            width=720,
            height=320,
        )

    def _on_filter_change(self, _sender, _app_data, _user_data=None):
        self._rebuild()

    def _matches_filter(self, f: Dict, flt: str) -> bool:
        if not flt:
            return True
        flt = flt.lower().strip()
        if flt in f["name"].lower():
            return True
        off_s = f"0x{f['offset']:X}".lower()
        return flt in off_s

    def _rebuild(self):
        if self.table_id is None:
            return
        flt = ""
        if self.filter_input_id is not None:
            try:
                flt = dpg.get_value(self.filter_input_id) or ""
            except Exception:
                flt = ""

        try:
            dpg.delete_item(self.table_id, children_only=True, slot=1)
        except TypeError:
             #compatibility fallback
            ch = dpg.get_item_children(self.table_id)
            rows = ch.get(1, []) if isinstance(ch, dict) else []
            for rid in rows:
                dpg.delete_item(rid)

        self.visible_fields = []
        self.value_text_ids = []
        self.pretty_text_ids = []
        self.name_select_ids = []
        self.selected_row = None

        for f in self.fields:
            if not self._matches_filter(f, flt):
                continue
            row_index = len(self.visible_fields)
            self.visible_fields.append(f)

            with dpg.table_row(parent=self.table_id):
                dpg.add_text(f"0x{f['offset']:03X}")
                sid = dpg.add_selectable(
                    label=f["name"],
                    span_columns=True,
                    callback=self._on_row_clicked,
                    user_data=row_index,
                )
                self.name_select_ids.append(sid)
                dpg.add_text(f["typ"])
                dpg.add_text(str(f["count"]))
                vid = dpg.add_text("")
                pid = dpg.add_text("")
                self.value_text_ids.append(vid)
                self.pretty_text_ids.append(pid)

        if self.status_text_id is not None:
            dpg.set_value(self.status_text_id, f"{len(self.visible_fields)} fields shown.")

    def _on_row_clicked(self, _sender, _app_data, user_data):
        now = time.monotonic()
        row_index = int(user_data)

        if self.selected_row is not None and self.selected_row < len(self.name_select_ids):
            dpg.set_value(self.name_select_ids[self.selected_row], False)

        self.selected_row = row_index
        if row_index < len(self.name_select_ids):
            dpg.set_value(self.name_select_ids[row_index], True)

        if self._last_click_row == row_index and (now - self._last_click_time) < 0.35:
            self._open_edit_dialog(self.visible_fields[row_index])
            self._last_click_row = None
            self._last_click_time = 0.0
            return

        self._last_click_row = row_index
        self._last_click_time = now

    def _on_edit_selected(self, _sender, _app_data, _user_data=None):
        if self.selected_row is None:
            msg_info("No selection", "Select a field row first.")
            return
        if self.selected_row >= len(self.visible_fields):
            return
        self._open_edit_dialog(self.visible_fields[self.selected_row])

    def refresh_from_snapshot(self, snap: Dict):
        if not self.visible_fields:
            return
        buf = snap.get("buf", b"")

        for i, f in enumerate(self.visible_fields):
            try:
                off = f["offset"]
                typ = f["typ"]
                cnt = f["count"]
                val = unpack_typed(buf, off, typ, cnt)
            except Exception:
                continue

            if cnt == 1:
                value_str = format_scalar_for_entry(val, typ, f["display"])
            else:
                value_str = ", ".join(format_scalar_for_entry(v, typ, f["display"]) for v in val)

            pretty = ""
            if f["pretty"] == "time32" and cnt == 1 and typ in ("i32", "u32"):
                pretty = fmt_time32(int(val))
            if f["bit_key"] and cnt == 1 and typ in ("u8", "i8"):
                pretty = fmt_bits_u8(int(val) & 0xFF, BIT_MAPS.get(f["bit_key"]))
            if f["enum"] and cnt == 1:
                vv = int(val) & 0xFF
                nm = dict(f["enum"]).get(vv)
                if nm:
                    pretty = f"{vv}: {nm}"

            if i < len(self.value_text_ids):
                dpg.set_value(self.value_text_ids[i], value_str)
            if i < len(self.pretty_text_ids):
                dpg.set_value(self.pretty_text_ids[i], pretty)

    def _open_edit_dialog(self, f: Dict):
        if self.app.selected_index is None:
            msg_info("No selection", "Select a car in the overview first.")
            return
        if not self.app.enable_writes:
            msg_warn("Writes disabled", "Enable 'Enable writes' first to edit fields.")
            return

        snap = self.app.snap_by_index.get(self.app.selected_index)
        if not snap:
            return
        buf = snap.get("buf", b"")

        off = f["offset"]
        typ = f["typ"]
        cnt = f["count"]
        display = f["display"]
        enum = f.get("enum")
        bit_key = f.get("bit_key")

        try:
            cur = unpack_typed(buf, off, typ, cnt)
        except Exception:
            cur = 0 if cnt == 1 else tuple([0] * cnt)

        if self.modal_id is None:
            return

        dpg.delete_item(self.modal_id, children_only=True)

        with dpg.group(parent=self.modal_id):
            dpg.add_text(f"Field: {f['name']}")
            dpg.add_text(f"Offset: 0x{off:03X}   Type: {typ}   Count: {cnt}")

            dpg.add_separator()
            dpg.add_text("New value:")

            # build editor
            editor_ids: List[int] = []

            if enum and cnt == 1 and typ in ("u8", "i8"):
                values = enum_display_list(enum)
                cur_disp = f"{int(cur) & 0xFF}: {dict(enum).get(int(cur) & 0xFF, '')}".strip()
                eid = dpg.add_combo(items=values, default_value=cur_disp, width=420)
                editor_ids = [eid]

                def parse_payload():
                    v = enum_value_from_display(dpg.get_value(eid)) & 0xFF
                    return pack_typed("u8", v)

            elif bit_key and cnt == 1 and typ in ("u8", "i8") and bit_key in BIT_MAPS:
                mapping = BIT_MAPS[bit_key]
                raw_id = dpg.add_input_text(default_value=f"0x{int(cur) & 0xFF:02X}", width=120)
                editor_ids = [raw_id]
                checks: Dict[int, int] = {}
                with dpg.group(horizontal=True):
                    for bit in sorted(mapping.keys()):
                        c = dpg.add_checkbox(label=f"b{bit} {mapping[bit]}", default_value=bool(int(cur) & (1 << bit)))
                        checks[bit] = c

                def parse_payload():
                    try:
                        rv = parse_offset_hex(dpg.get_value(raw_id)) & 0xFF
                    except Exception:
                        rv = 0
                        for bit, cid in checks.items():
                            if dpg.get_value(cid):
                                rv |= (1 << bit)
                        rv &= 0xFF
                    return pack_typed("u8", rv)

            else:
                if cnt == 1:
                    default = format_scalar_for_entry(cur, typ, display)
                else:
                    default = ", ".join(format_scalar_for_entry(v, typ, display) for v in cur)
                eid = dpg.add_input_text(default_value=default, width=560)
                editor_ids = [eid]

                def parse_payload():
                    raw = dpg.get_value(eid).strip()
                    parts = [p for p in re.split(r"[\s,]+", raw) if p]
                    if not parts:
                        raise ValueError("empty")

                    if cnt > 1 and len(parts) == 1:
                        parts = parts * cnt
                    if cnt > 1 and len(parts) != cnt:
                        raise ValueError(f"need {cnt} values")

                    out = []
                    for p in parts[:cnt]:
                        if typ == "f32":
                            v = float(p)
                        else:
                            v = parse_value_for_display(p, display)
                        v = clamp_value(typ, v)
                        out.append(v)
                    return pack_typed(typ, out if cnt > 1 else out[0])

            dpg.add_spacer(height=8)

            def do_write_selected():
                try:
                    payload = parse_payload()
                except Exception as e:
                    msg_error("Bad value", str(e))
                    return
                self.app.write_field_bytes(self.app.selected_index, off, payload)
                self.app.refresh_once()
                dpg.configure_item(self.modal_id, show=False)

            def do_write_all():
                if not msg_yesno("Confirm", f"Write {f['name']} to ALL cars?"):
                    return
                try:
                    payload = parse_payload()
                except Exception as e:
                    msg_error("Bad value", str(e))
                    return
                for i in range(self.app.car_count):
                    self.app.write_field_bytes(i, off, payload)
                self.app.refresh_once()
                dpg.configure_item(self.modal_id, show=False)

            with dpg.group(horizontal=True):
                dpg.add_button(label="Write Selected", width=130, callback=lambda *_: do_write_selected())
                dpg.add_button(label="Write ALL", width=100, callback=lambda *_: do_write_all())
                dpg.add_button(label="Cancel", width=90, callback=lambda *_: dpg.configure_item(self.modal_id, show=False))

        dpg.configure_item(self.modal_id, label=f"Edit: {f['name']} @ 0x{off:03X}", show=True)


# Custom scripts
class CustomScriptsController:
    def __init__(self, app):
        self.app = app

        # runtime
        self.sc_active = False
        self.slip_active = False
        self._next_tick_due = 0.0

        # range tracking
        self.range_locked = False
        self.curseg_min_seen: Optional[int] = None
        self.curseg_max_seen: Optional[int] = None
        self.curseg_min_locked: Optional[int] = None
        self.curseg_max_locked: Optional[int] = None

        # fuel freeze
        self.freeze_fuel = True
        self.saved_fuel: Dict[int, int] = {}

        # SC settings
        self.tick_ms = 50.0
        self.base_kph = 60.0
        self.min_kph = 40.0
        self.max_kph = 80.0
        self.gap_segments = 100.0
        self.gain_kph_per_seg = 0.20
        self.speed_units_per_kph = 48.0
        self.order = "track"  # track | racepos
        self.free_run_enabled = True
        self.free_run_gap = 800.0

        # Slipstream settings
        self.slipstream_enabled = True
        self.slip_same_lap_only = True
        self.slip_require_same_side = True
        self.slip_min_kph = 100.0
        self.slip_range_seg = 2500.0
        self.slip_engine_mult = 1.80
        self.slip_hold_ms = 300.0

        # eligibility filters
        self.include_player = True
        self.ignore_pits = True
        self.ignore_invisible = True
        self.ignore_failflags = False
        self.ignore_invalid_racepos = True

        # slipstream state
        self._tick_counter = 0
        self._slip_until_tick: Dict[int, int] = {}
        self._eng_orig: Dict[int, int] = {}

        # fuel drain state
        self.fuel_drain_targets: List[int] = []
        self.fuel_drain_secs: float = 3.0
        self._fuel_drain_until: Dict[int, float] = {}  # idx → monotonic deadline

        # UI strings
        self.sc_status = "Inactive"
        self.slip_status = "Slip: Inactive"
        self.seen_range = "-"
        self.locked_range = "(unlocked)"
        self.slip_debug = "Slip: -"

    # ----- config -----
    def export_config(self) -> Dict[str, Any]:
        return {
            "safety_car": {
                "tick_ms": str(self.tick_ms),
                "base_kph": str(self.base_kph),
                "min_kph": str(self.min_kph),
                "max_kph": str(self.max_kph),
                "gap_segments": str(self.gap_segments),
                "gain_kph_per_seg": str(self.gain_kph_per_seg),
                "speed_units_per_kph": str(self.speed_units_per_kph),
                "order": self.order,
                "freeze_fuel": bool(self.freeze_fuel),
                "free_run_enabled": bool(self.free_run_enabled),
                "free_run_gap": str(self.free_run_gap),
                "include_player": bool(self.include_player),
                "ignore_pits": bool(self.ignore_pits),
                "ignore_invisible": bool(self.ignore_invisible),
                "ignore_failflags": bool(self.ignore_failflags),
                "ignore_invalid_racepos": bool(self.ignore_invalid_racepos),
            },
            "slipstream": {
                "enabled": bool(self.slipstream_enabled),
                "same_lap_only": bool(self.slip_same_lap_only),
                "require_same_side": bool(self.slip_require_same_side),
                "min_kph": str(self.slip_min_kph),
                "range_seg": str(self.slip_range_seg),
                "engine_mult": str(self.slip_engine_mult),
                "hold_ms": str(self.slip_hold_ms),
            },
            "fuel_drain": {
                "targets": list(self.fuel_drain_targets),
                "secs":    float(self.fuel_drain_secs),
            },
        }

    @staticmethod
    def _as_float(v, default: float) -> float:
        try:
            return float(v)
        except Exception:
            return float(default)

    @staticmethod
    def _as_bool(v, default: bool) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(default)

    def import_config(self, cfg: Optional[Dict[str, Any]]):
        if not isinstance(cfg, dict):
            return
        sc = cfg.get("safety_car", {})
        if isinstance(sc, dict):
            self.tick_ms = self._as_float(sc.get("tick_ms", self.tick_ms), self.tick_ms)
            self.base_kph = self._as_float(sc.get("base_kph", self.base_kph), self.base_kph)
            self.min_kph = self._as_float(sc.get("min_kph", self.min_kph), self.min_kph)
            self.max_kph = self._as_float(sc.get("max_kph", self.max_kph), self.max_kph)
            self.gap_segments = self._as_float(sc.get("gap_segments", self.gap_segments), self.gap_segments)
            self.gain_kph_per_seg = self._as_float(sc.get("gain_kph_per_seg", self.gain_kph_per_seg), self.gain_kph_per_seg)
            self.speed_units_per_kph = self._as_float(sc.get("speed_units_per_kph", self.speed_units_per_kph), self.speed_units_per_kph)
            self.order = str(sc.get("order", self.order))
            if self.order not in ("track", "racepos"):
                self.order = "track"
            self.freeze_fuel = self._as_bool(sc.get("freeze_fuel", self.freeze_fuel), self.freeze_fuel)
            self.free_run_enabled = self._as_bool(sc.get("free_run_enabled", self.free_run_enabled), self.free_run_enabled)
            self.free_run_gap = self._as_float(sc.get("free_run_gap", self.free_run_gap), self.free_run_gap)
            self.include_player = self._as_bool(sc.get("include_player", self.include_player), self.include_player)
            self.ignore_pits = self._as_bool(sc.get("ignore_pits", self.ignore_pits), self.ignore_pits)
            self.ignore_invisible = self._as_bool(sc.get("ignore_invisible", self.ignore_invisible), self.ignore_invisible)
            self.ignore_failflags = self._as_bool(sc.get("ignore_failflags", self.ignore_failflags), self.ignore_failflags)
            self.ignore_invalid_racepos = self._as_bool(sc.get("ignore_invalid_racepos", self.ignore_invalid_racepos), self.ignore_invalid_racepos)

        slip = cfg.get("slipstream", {})
        if isinstance(slip, dict):
            self.slipstream_enabled = self._as_bool(slip.get("enabled", self.slipstream_enabled), self.slipstream_enabled)
            self.slip_same_lap_only = self._as_bool(slip.get("same_lap_only", self.slip_same_lap_only), self.slip_same_lap_only)
            self.slip_require_same_side = self._as_bool(slip.get("require_same_side", self.slip_require_same_side), self.slip_require_same_side)
            self.slip_min_kph = self._as_float(slip.get("min_kph", self.slip_min_kph), self.slip_min_kph)
            self.slip_range_seg = self._as_float(slip.get("range_seg", self.slip_range_seg), self.slip_range_seg)
            self.slip_engine_mult = self._as_float(slip.get("engine_mult", self.slip_engine_mult), self.slip_engine_mult)
            self.slip_hold_ms = self._as_float(slip.get("hold_ms", self.slip_hold_ms), self.slip_hold_ms)

        fd = cfg.get("fuel_drain", {})
        if isinstance(fd, dict):
            raw_targets = fd.get("targets", self.fuel_drain_targets)
            if isinstance(raw_targets, list):
                self.fuel_drain_targets = [int(x) for x in raw_targets if str(x).strip().lstrip("-").isdigit()]
            self.fuel_drain_secs = max(0.1, self._as_float(fd.get("secs", self.fuel_drain_secs), self.fuel_drain_secs))

    # ----- helpers -----
    def _read_u8(self, idx: int, struct_ofs: int) -> int:
        return self.app.pm.read_uchar(self.app.field_addr(idx, struct_ofs))

    def _read_i16(self, idx: int, struct_ofs: int) -> int:
        return self.app.pm.read_short(self.app.field_addr(idx, struct_ofs))

    def _read_i32(self, idx: int, struct_ofs: int) -> int:
        return self.app.pm.read_int(self.app.field_addr(idx, struct_ofs))

    def _read_u32(self, idx: int, struct_ofs: int) -> int:
        return self.app.pm.read_uint(self.app.field_addr(idx, struct_ofs))

    def _write_i16_pos(self, idx: int, struct_ofs: int, val: int):
        v = int(val)
        v = max(0, min(32767, v))
        self.app.pm.write_short(self.app.field_addr(idx, struct_ofs), v)

    def _write_u32(self, idx: int, struct_ofs: int, val: int):
        self.app.pm.write_uint(self.app.field_addr(idx, struct_ofs), int(val) & 0xFFFFFFFF)

    @staticmethod
    def _sign(x: int) -> int:
        return 1 if x > 0 else (-1 if x < 0 else 0)

    # eligibility
    def _is_player(self, idx: int) -> bool:
        return bool(self._read_u8(idx, OFS["flags_7C"]) & 0x01)

    def _is_in_pits(self, idx: int) -> bool:
        if self._read_u8(idx, OFS["flags_23"]) & (1 << 5):
            return True
        if self._read_u8(idx, OFS["flags_16A"]) & (1 << 3):
            return True
        if self._read_u8(idx, OFS["display168"]) & (1 << 3):
            return True
        return False

    def _is_invisible(self, idx: int) -> bool:
        return bool(self._read_u8(idx, OFS["flags_90"]) & (1 << 7))

    def _has_failflags(self, idx: int) -> bool:
        ff1 = self._read_u8(idx, OFS["flagsFail1"])
        ff2 = self._read_u8(idx, OFS["flagsFail2"])
        return (ff1 | ff2) != 0

    def _racepos_invalid(self, idx: int) -> bool:
        rp = self._read_u8(idx, OFS["racePos"])
        return rp >= 0xFE

    def _eligible(self, idx: int) -> bool:
        if (not self.include_player) and self._is_player(idx):
            return False
        if self.ignore_invalid_racepos and self._racepos_invalid(idx):
            return False
        if self.ignore_pits and self._is_in_pits(idx):
            return False
        if self.ignore_invisible and self._is_invisible(idx):
            return False
        if self.ignore_failflags and self._has_failflags(idx):
            return False
        return True

    # range labels
    def reset_seen_range(self):
        self.curseg_min_seen = None
        self.curseg_max_seen = None
        self._update_range_labels()

    def lock_range_from_seen(self):
        if self.curseg_min_seen is None or self.curseg_max_seen is None:
            return
        self.curseg_min_locked = self.curseg_min_seen
        self.curseg_max_locked = self.curseg_max_seen
        self.range_locked = True
        self._update_range_labels()

    def unlock_range(self):
        self.range_locked = False
        self.curseg_min_locked = None
        self.curseg_max_locked = None
        self._update_range_labels()

    def _update_range_labels(self):
        if self.curseg_min_seen is None:
            self.seen_range = "-"
        else:
            self.seen_range = f"min={self.curseg_min_seen}  max={self.curseg_max_seen}"

        if self.range_locked and self.curseg_min_locked is not None:
            self.locked_range = f"min={self.curseg_min_locked}  max={self.curseg_max_locked}"
        else:
            self.locked_range = "(unlocked)"

    # slipstream enginePower
    def _restore_all_enginepower(self):
        for idx, orig in list(self._eng_orig.items()):
            try:
                self._write_i16_pos(idx, OFS["enginePower"], orig)
            except Exception:
                pass
        self._eng_orig.clear()
        self._slip_until_tick.clear()

    def _apply_enginepower_boost(self, idx: int, mult: float):
        if idx not in self._eng_orig:
            try:
                self._eng_orig[idx] = int(self._read_i16(idx, OFS["enginePower"]))
            except Exception:
                return
        base = self._eng_orig[idx]
        boosted = int(round(float(base) * float(mult)))
        boosted = max(0, min(32767, boosted))
        try:
            self._write_i16_pos(idx, OFS["enginePower"], boosted)
        except Exception:
            pass

    def _restore_enginepower_one(self, idx: int):
        if idx not in self._eng_orig:
            return
        orig = self._eng_orig.pop(idx)
        try:
            self._write_i16_pos(idx, OFS["enginePower"], orig)
        except Exception:
            pass
        self._slip_until_tick.pop(idx, None)

    # controls
    def sc_activate(self):
        if not self.app.pm:
            msg_error("Not connected", "Connect first.")
            return
        if not self.app.enable_writes:
            if msg_yesno("Enable writes?", "Safety car needs memory writes.\nEnable 'Enable writes' now?"):
                self.app.set_enable_writes(True)
            else:
                return

        self.saved_fuel.clear()
        if self.freeze_fuel:
            for idx in range(self.app.car_count):
                try:
                    if not self._eligible(idx):
                        continue
                    self.saved_fuel[idx] = self._read_u32(idx, OFS["fuelLoad"])
                except Exception:
                    pass

        self.sc_active = True
        self.sc_status = "ACTIVE"
        self._next_tick_due = 0.0

    def sc_release(self):
        self.sc_active = False
        self.sc_status = "Inactive"
        self.saved_fuel.clear()

    def slip_on(self):
        if not self.app.pm:
            msg_error("Not connected", "Connect first.")
            return
        if not self.app.enable_writes:
            if msg_yesno("Enable writes?", "Slipstream needs memory writes.\nEnable 'Enable writes' now?"):
                self.app.set_enable_writes(True)
            else:
                return

        self._restore_all_enginepower()
        self.slip_active = True
        self.slip_status = "Slip: ACTIVE"
        self._next_tick_due = 0.0

    def slip_off(self):
        self.slip_active = False
        self.slip_status = "Slip: Inactive"
        self._restore_all_enginepower()

    def tick_snapshot(self, cars: List[Dict]):
        if self.range_locked:
            self._update_range_labels()
            return
        try:
            for d in cars:
                buf = d.get("buf")
                if not buf:
                    continue
                cs = struct.unpack_from("<i", buf, OFS["curSeg"])[0]
                if self.curseg_min_seen is None or cs < self.curseg_min_seen:
                    self.curseg_min_seen = cs
                if self.curseg_max_seen is None or cs > self.curseg_max_seen:
                    self.curseg_max_seen = cs
        except Exception:
            pass
        self._update_range_labels()

    def update(self, now: float):
        if not (self.sc_active or self.slip_active or self._fuel_drain_until):
            return

        if not self.app.pm or not self.app.enable_writes:
            self.sc_active = False
            self.slip_active = False
            self.sc_status = "Inactive (writes disabled)"
            self.slip_status = "Slip: Inactive (writes disabled)"
            self.saved_fuel.clear()
            self._restore_all_enginepower()
            return

        interval = max(0.01, float(self.tick_ms) / 1000.0)
        if now < self._next_tick_due:
            return
        self._next_tick_due = now + interval

        self._tick_fast()

    def _tick_fast(self):
        self._tick_counter += 1

        base_kph = float(self.base_kph)
        min_kph = float(self.min_kph)
        max_kph = float(self.max_kph)
        gap_segments = float(self.gap_segments)
        gain = float(self.gain_kph_per_seg)
        units_per_kph = float(self.speed_units_per_kph)

        free_gap = float(self.free_run_gap)

        slip_min_kph = float(self.slip_min_kph)
        slip_range = float(self.slip_range_seg)
        slip_mult = float(self.slip_engine_mult)
        slip_hold_ms = float(self.slip_hold_ms)

        if max_kph < min_kph:
            min_kph, max_kph = max_kph, min_kph
        if units_per_kph <= 0 or gap_segments <= 0:
            return

        tick_ms = max(10.0, float(self.tick_ms))
        hold_ticks = max(1, int(round(max(0.0, slip_hold_ms) / tick_ms)))

        states = []
        cursegs = []
        eligible_set = set()

        for idx in range(self.app.car_count):
            try:
                if not self._eligible(idx):
                    continue
                lap = int(self._read_u8(idx, OFS["lapNr"]))
                curSeg = int(self._read_i32(idx, OFS["curSeg"]))
                segF = int(self._read_i16(idx, OFS["segDistFactor"]))
                rp = int(self._read_u8(idx, OFS["racePos"]))
                segPosX = int(self._read_i16(idx, OFS["segPosX"]))
                speed_units_cur = int(self._read_i16(idx, OFS["speed_i16"]))
            except Exception:
                continue

            eligible_set.add(idx)
            states.append({
                "index": idx,
                "lap": lap,
                "curSeg": curSeg,
                "segF": segF,
                "racePos": rp,
                "segPosX": segPosX,
                "speed_units_cur": speed_units_cur,
            })
            cursegs.append(curSeg)

        if not states:
            if self.slip_active:
                self._restore_all_enginepower()
            return

        if not self.range_locked:
            mn = min(cursegs)
            mx = max(cursegs)
            self.curseg_min_seen = mn if self.curseg_min_seen is None else min(self.curseg_min_seen, mn)
            self.curseg_max_seen = mx if self.curseg_max_seen is None else max(self.curseg_max_seen, mx)

        if self.range_locked and self.curseg_min_locked is not None and self.curseg_max_locked is not None:
            seg_min = self.curseg_min_locked
            seg_max = self.curseg_max_locked
        elif self.curseg_min_seen is not None and self.curseg_max_seen is not None:
            seg_min = self.curseg_min_seen
            seg_max = self.curseg_max_seen
        else:
            seg_min = min(cursegs)
            seg_max = max(cursegs)

        track_len = (seg_max - seg_min + 1)
        if track_len <= 0:
            track_len = 1

        for s in states:
            prog = (s["lap"] * track_len) + (s["curSeg"] - seg_min) + (float(s["segF"]) / 0x4000)
            s["progress"] = float(prog)

        self._update_range_labels()

        phys = sorted(states, key=lambda s: s["progress"], reverse=True)
        state_by_idx = {s["index"]: s for s in states}

        gap_to_ahead: Dict[int, float] = {}
        ahead_idx: Dict[int, int] = {}
        for j, s in enumerate(phys):
            if j == 0:
                gap_to_ahead[s["index"]] = 0.0
            else:
                a = phys[j - 1]
                gap = float(a["progress"] - s["progress"])
                if gap < 0:
                    gap = 0.0
                gap_to_ahead[s["index"]] = gap
                ahead_idx[s["index"]] = a["index"]

        # Slipstream
        if not self.slip_active:
            self.slip_debug = "Slip: (idle)"
        elif not self.slipstream_enabled:
            self._restore_all_enginepower()
            self.slip_debug = "Slip: OFF"
        else:
            for idx, aidx in ahead_idx.items():
                s = state_by_idx.get(idx)
                a = state_by_idx.get(aidx)
                if not s or not a:
                    continue

                if self.slip_same_lap_only and s["lap"] != a["lap"]:
                    continue

                gap = gap_to_ahead.get(idx, 999999.0)
                if gap <= 0.0 or gap > slip_range:
                    continue

                cur_kph = float(max(0, s["speed_units_cur"])) / units_per_kph
                if cur_kph <= slip_min_kph:
                    continue

                if self.slip_require_same_side:
                    sa = self._sign(int(a["segPosX"]))
                    sb = self._sign(int(s["segPosX"]))
                    if sa == 0 or sa != sb:
                        continue

                self._slip_until_tick[idx] = self._tick_counter + hold_ticks

            slip_active_set = set()
            for idx, until in list(self._slip_until_tick.items()):
                if idx not in eligible_set:
                    self._slip_until_tick.pop(idx, None)
                    continue
                if until >= self._tick_counter:
                    slip_active_set.add(idx)
                else:
                    self._slip_until_tick.pop(idx, None)

            for idx in list(self._eng_orig.keys()):
                if (idx not in slip_active_set) or (idx not in eligible_set):
                    self._restore_enginepower_one(idx)

            for idx in slip_active_set:
                self._apply_enginepower_boost(idx, slip_mult)

            self.slip_debug = f"Slip: {len(slip_active_set)} boosted"

        # SC ordering
        if self.order == "racepos":
            ordered = sorted(states, key=lambda s: (s["racePos"] >= 0xFE, s["racePos"]))
        else:
            ordered = phys

        # Safety Car (speed_i16)
        if self.sc_active:
            leader = ordered[0]
            leader_prog = float(leader["progress"])

            for rank, s in enumerate(ordered):
                idx = s["index"]

                if rank != 0 and self.free_run_enabled:
                    g = gap_to_ahead.get(idx, 0.0)
                    if g > free_gap:
                        continue

                target_prog = leader_prog - float(rank) * gap_segments
                err = target_prog - float(s["progress"])
                tgt_kph = base_kph + gain * err
                tgt_kph = max(min_kph, min(max_kph, tgt_kph))

                speed_units = int(round(tgt_kph * units_per_kph))
                try:
                    self._write_i16_pos(idx, OFS["speed_i16"], speed_units)
                except Exception:
                    pass

            if self.freeze_fuel and self.saved_fuel:
                for idx, fuel in self.saved_fuel.items():
                    try:
                        self._write_u32(idx, OFS["fuelLoad"], fuel)
                    except Exception:
                        pass

        # ── Fuel drain ────────────────────────────────────────────────────────
        if self._fuel_drain_until:
            now_mono = time.monotonic()
            done = []
            for idx, deadline in list(self._fuel_drain_until.items()):
                if now_mono >= deadline:
                    done.append(idx)
                    continue
                try:
                    self._write_u32(idx, OFS["fuelLoad"], 0)
                except Exception:
                    pass
            for idx in done:
                del self._fuel_drain_until[idx]

    def fuel_drain_fire(self):
        # Writes fuelLoad=0 in target cars during fuel_drain_secs seconds
        if not self.app.pm:
            return
        if not self.app.enable_writes:
            if msg_yesno("Enable writes?", "Fuel drain needs memory writes.\nEnable 'Enable writes' now?"):
                self.app.set_enable_writes(True)
            else:
                return
        targets = set(self.fuel_drain_targets)
        if not targets:
            return
        deadline = time.monotonic() + max(0.1, float(self.fuel_drain_secs))
        for idx in range(self.app.car_count):
            try:
                cid = int(self.app.pm.read_uchar(self.app.field_addr(idx, OFS["carId"])))
                raw_id = cid - 128 if cid > 40 else cid
                if raw_id in targets:
                    self._fuel_drain_until[idx] = deadline
            except Exception:
                pass


class GP2ViewerApp:
    def __init__(self):
        self.pm: Optional[pymem.Pymem] = None

        self.base_anchor: int = 0
        self.anchor_struct_offset: int = 0x14
        self.record_size: int = 0x330
        self.car_count: int = DEFAULT_CAR_COUNT

        self.sorted_cars: List[Dict] = []
        self.snap_by_index: Dict[int, Dict] = {}
        self.selected_index: Optional[int] = None
        self.focused_car_index: Optional[int] = None

        # JSON export
        self.json_export_enabled: bool = False
        self.json_export_folder: str = r""
        self.ui_json_export_checkbox = None
        self.ui_json_export_folder   = None
        self.ui_json_export_status   = None

        # ── Timing / sector state ──────────────────────────────────────────
        # Main reference table: car_id -> best lap + sectors of that lap
        # { car_id: { "best_lap": int, "s1": int, "s2": int, "s3": int } }
        self._ref_table: Dict[int, Dict] = {}

        # Global best per sector (across all drivers)
        self._global_best_s1:  int = 0
        self._global_best_s2:  int = 0
        self._global_best_s3:  int = 0

        # Track previous timeLast per car to detect lap completion
        self._prev_time_last: Dict[int, int] = {}   # car_id -> timeLast seen last tick

        # Track previous lapNr per car to detect out-lap (box exit)
        self._prev_lap_nr: Dict[int, int] = {}       # car_id -> lapNr seen last tick

        # Out-lap flag per car: True means current lap started from pits → ignore sectors
        self._out_lap: Dict[int, bool] = {}

        # Sectors of the current in-progress lap (updated at each split)
        # car_id -> { "s1": int|None, "s2": int|None, "spl1": int, "spl2": int }
        self._live_sectors: Dict[int, Dict] = {}

        # Post-lap display window: car_id -> monotonic timestamp of crossing the line
        self._post_lap_ts: Dict[int, float] = {}
        self._last_completed: Dict[int, Dict] = {}
        self.POST_LAP_SECS = 15.0

        # Session clock at the moment each car last crossed the start/finish line
        # raw_id -> session_clock_ms captured when lapNr changed
        self._lap_start_session_ms: Dict[int, int] = {}

        # ── Race gap state ─────────────────────────────────────────────────
        self._race_track_length:     Optional[int]    = None
        self._race_prev_leader_lap:  Optional[int]    = None
        self._race_prev_leader_9e:   Optional[int]    = None
        self._race_tick_counter:     int              = 0

        # config state (strings like old UI)
        self.process_name: str = DEFAULT_PROCESS_NAME
        self.base_anchor_text: str = DEFAULT_BASE_ANCHOR
        self.anchor_ofs_text: str = DEFAULT_ANCHOR_STRUCT_OFFSET
        self.record_size_text: str = DEFAULT_RECORD_SIZE
        self.refresh_ms: int = int(DEFAULT_REFRESH_MS)
        self.sort_mode: str = "RacePos (0xA4)"
        self.enable_writes: bool = False

        self.auto_refresh: bool = False
        self._next_refresh_due: float = 0.0

        self._autosave_due: Optional[float] = None
        self._loading_config: bool = True
        self._cfg_cache: Dict[str, Any] = {}

        self._grid_scan_running: bool = False

        # UI ids
        self.ui_process = None
        self.ui_base_anchor = None
        self.ui_anchor_ofs = None
        self.ui_record_size = None
        self.ui_refresh_ms = None
        self.ui_sort_mode = None
        self.ui_enable_writes = None
        self.ui_status = None

        # overview
        self.ov_table = None
        self.ov_row_ids: Dict[int, int] = {}
        self.ov_sel_ids: Dict[int, int] = {}
        self.ov_cell_ids: Dict[int, List[int]] = {}

        # details rows
        self.rows_by_tab: Dict[str, List[BaseRowDPG]] = {}

        # full struct tab
        self.full_struct = FullStructTabDPG(self)
        self.full_struct_tab_id: Optional[int] = None

        # scripts
        self.scripts = CustomScriptsController(self)
        self._scripts_ui_ids: Dict[str, int] = {}

        # thread->ui queue
        self.ui_queue: "queue.Queue[callable]" = queue.Queue()

        self.load_config()
        self.scripts.import_config(self._cfg_cache.get("scripts", {}))
        self._loading_config = False


    def load_config(self):
        self._cfg_cache = {}
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                self._cfg_cache = cfg
        except Exception:
            return

        cfg = self._cfg_cache
        self.process_name = cfg.get("process", self.process_name)
        self.base_anchor_text = cfg.get("base_anchor", self.base_anchor_text)
        self.anchor_ofs_text = cfg.get("anchor_struct_offset", self.anchor_ofs_text)
        self.record_size_text = cfg.get("record_size", self.record_size_text)
        try:
            self.refresh_ms = int(cfg.get("refresh_ms", self.refresh_ms))
        except Exception:
            pass
        self.sort_mode = cfg.get("sort_mode", self.sort_mode)

    def save_config(self):
        cfg: Dict[str, Any] = {
            "process": self.process_name.strip(),
            "base_anchor": self.base_anchor_text.strip(),
            "anchor_struct_offset": self.anchor_ofs_text.strip(),
            "record_size": self.record_size_text.strip(),
            "refresh_ms": int(self.refresh_ms),
            "sort_mode": self.sort_mode,
            "scripts": self.scripts.export_config(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def schedule_save_config(self, delay_s: float = 0.4):
        if self._loading_config:
            return
        self._autosave_due = time.monotonic() + float(delay_s)

    # ---------- memory addressing ----------
    def record_start_addr(self, car_index: int) -> int:
        return self.base_anchor - self.anchor_struct_offset + (car_index * self.record_size)

    def field_addr(self, car_index: int, struct_offset: int) -> int:
        return self.record_start_addr(car_index) + struct_offset

    def read_focused_car_index(self) -> Optional[int]:
        
        # Returns the idx (0-25) of the car currently in camera focus,
        # or None if it cannot be determined.

        # The game stores the focused car in two mirrored addresses:
        #     En_Foco_1 = base_anchor + EN_FOCO_OFFSET_1
        #     En_Foco_2 = base_anchor + EN_FOCO_OFFSET_2

        # The value stored is a linear mapping:
        #     value = EN_FOCO_IDX0_VALUE + idx * record_size
        # So:
        #     idx = (value - EN_FOCO_IDX0_VALUE) // record_size
        
        if not self.pm or not self.base_anchor:
            return None
        try:
            addr = (self.base_anchor + EN_FOCO_OFFSET_1) & 0xFFFFFFFF
            value = self.pm.read_uint(addr)
            idx = (value - EN_FOCO_IDX0_VALUE) // self.record_size
            if 0 <= idx < self.car_count:
                return idx
        except Exception:
            pass
        # fallback: try second address
        try:
            addr = (self.base_anchor + EN_FOCO_OFFSET_2) & 0xFFFFFFFF
            value = self.pm.read_uint(addr)
            idx = (value - EN_FOCO_IDX0_VALUE) // self.record_size
            if 0 <= idx < self.car_count:
                return idx
        except Exception:
            pass
        return None

    # ── Helpers ────────────────────────────────────────────────────────────

    def _fmt_time(self, raw: int) -> Optional[str]:
        # Convert raw i32 game time to 'm:ss.mmm'. Returns None if zero/invalid.
        if raw <= 0:
            return None
        ms = raw % 1000
        s  = (raw // 1000) % 60
        m  = raw // 60000
        return f"{m}:{s:02d}.{ms:03d}"

    def _fmt_gap(self, delta_ms: int) -> str:
        # Format a gap in ms as '+s.mmm' or '-s.mmm'.
        sign    = "+" if delta_ms >= 0 else "-"
        abs_ms  = abs(delta_ms)
        s_part  = abs_ms // 1000
        ms_part = abs_ms % 1000
        return f"{sign}{s_part}.{ms_part:03d}"

    def _sector_color(self, val: int, personal_best: int, global_best: int,
                      has_personal_ref: bool) -> str:
        # 
        # Color logic:
        #   - val <= 0               → empty
        #   - val <= global_best     → purple  (best of all, including ties)
        #   - val < personal_best    → green   (personal best)
        #   - no personal ref yet    → green   (first valid lap for this driver)
        #   - otherwise              → yellow  (slower than personal best)
        # 
        if val <= 0:
            return "empty"
        if global_best > 0 and val <= global_best:
            return "purple"
        if not has_personal_ref:
            return "green"
        if personal_best > 0 and val < personal_best:
            return "green"
        return "yellow"

    # All 26 raw driver IDs (used to pre-populate timing table on reset)
    ALL_RAW_IDS = [33, 12, 26, 16, 36, 22, 14, 18, 31, 38, 13, 28, 34, 10, 27, 15, 30, 11, 23, 35, 19, 21, 29, 25, 20, 24]

    def reset_timing_table(self):
        # Reset all timing to initial state. Pre-populates table with all 26 drivers.# 
        # Pre-populate ref_table with all drivers, no times yet
        self._ref_table = {
            raw_id: {"raw_id": raw_id, "best_lap": 0, "s1": 0, "s2": 0, "s3": 0}
            for raw_id in self.ALL_RAW_IDS
        }
        self._global_best_s1   = 0
        self._global_best_s2   = 0
        self._global_best_s3   = 0
        self._prev_time_last   = {}
        self._prev_lap_nr      = {}
        self._out_lap          = {}
        self._live_sectors     = {}
        self._post_lap_ts      = {}
        self._last_completed   = {}
        self._lap_start_session_ms = {}
        self._race_track_length     = None
        self._race_prev_leader_lap  = None
        self._race_prev_leader_9e   = None
        self._race_tick_counter     = 0

    def _read_session_clock_ms(self) -> int:
        # Read the session elapsed timer (ms, u32) from memory. Returns 0 on error.
        if not self.pm or not self.base_anchor:
            return 0
        try:
            addr = (self.base_anchor + SESSION_CLOCK_OFFSET) & 0xFFFFFFFF
            return self.pm.read_uint(addr)
        except Exception:
            return 0

    def _process_all_laps(self):
        
        # Called every refresh cycle. Scans all cars and updates _ref_table
        # when any car completes a lap. Also detects out-laps.
        # Key: always use raw_id (normalized) as the dict key, never raw car_id,
        # to avoid duplicates when the player controls a car (car_id += 128).
        
        seen_raw_ids = set()
        session_clock_ms = self._read_session_clock_ms()

        # Pre-select best slot per raw_id: prefer the one with active data
        best_slot: Dict[int, Dict] = {}
        for d in self.sorted_cars:
            car_id = d.get("carId", -1)
            if car_id < 0:
                continue
            raw_id = car_id - 128 if car_id > 40 else car_id
            if raw_id not in self.ALL_RAW_IDS:
                continue
            lap_nr_check = d.get("lapNr", 0)
            last_check   = d.get("timeLast", 0)
            if lap_nr_check == 0 and last_check == 0:
                continue
            # If we already have a slot for this raw_id, keep the one with more data
            if raw_id in best_slot:
                existing = best_slot[raw_id]
                existing_score = (existing.get("timeLast", 0) > 0) + (existing.get("lapNr", 0) > 0)
                new_score      = (last_check > 0) + (lap_nr_check > 0)
                if new_score <= existing_score:
                    continue
            best_slot[raw_id] = d

        for raw_id, d in best_slot.items():

            lap_nr = d.get("lapNr", 0)
            last   = d.get("timeLast",     0)
            spl1   = d.get("timeLastSpl1", 0)
            spl2   = d.get("timeLastSpl2", 0)

            prev_lap  = self._prev_lap_nr.get(raw_id, -1)
            prev_last = self._prev_time_last.get(raw_id, 0)

            # Detect lap number change → car started a new lap
            if prev_lap != -1 and lap_nr != prev_lap:
                # out-lap = the lap that just completed was lap 0 (installation/formation)
                self._out_lap[raw_id] = (prev_lap == 0)
                # Capture session clock as the start of this new lap
                if session_clock_ms > 0:
                    self._lap_start_session_ms[raw_id] = session_clock_ms

            # Detect lap completion: timeLast changed
            lap_just_completed = (last > 0 and last != prev_last and prev_last > 0)

            if lap_just_completed:
                is_out_lap = self._out_lap.get(raw_id, False)

                if not is_out_lap and spl1 > 0 and spl2 > spl1 and last > spl2:
                    s1 = spl1
                    s2 = spl2 - spl1
                    s3 = last  - spl2

                    # Update ref_table: keep personal best lap only
                    entry = self._ref_table.get(raw_id, {})
                    if entry.get("best_lap", 0) == 0 or last < entry["best_lap"]:
                        self._ref_table[raw_id] = {
                            "raw_id":   raw_id,
                            "best_lap": last,
                            "s1":       s1,
                            "s2":       s2,
                            "s3":       s3,
                        }

                    # Update global sector bests
                    if self._global_best_s1 == 0 or s1 < self._global_best_s1:
                        self._global_best_s1 = s1
                    if self._global_best_s2 == 0 or s2 < self._global_best_s2:
                        self._global_best_s2 = s2
                    if self._global_best_s3 == 0 or s3 < self._global_best_s3:
                        self._global_best_s3 = s3

                    # Store for post-lap color display
                    self._last_completed[raw_id] = {"s1": s1, "s2": s2, "s3": s3, "lap": last}

                self._post_lap_ts[raw_id] = time.monotonic()
                self._out_lap[raw_id] = False

            # Only update tracking state if the slot has live data.
            # If last==0 and we already have saved times, the car disappeared
            # from the slot — preserve saved state, don't update tracking.
            if last > 0 or raw_id not in self._ref_table or self._ref_table[raw_id].get("best_lap", 0) == 0:
                self._prev_time_last[raw_id] = last
                self._prev_lap_nr[raw_id]    = lap_nr

            # Store live sector splits for this car (for timing table live columns)
            if last > 0 or spl1 > 0:
                split_nr_car = d.get("splitNr", 2)
                self._live_sectors[raw_id] = {
                    "split_nr": split_nr_car,
                    "spl1":     spl1,
                    "spl2":     spl2,
                }

    def _build_timing_table_json(self) -> list:
        # Build the sorted timing table for the React overlay.
        # Always returns all 26 drivers. Drivers without a time sort to the end.
        # Includes live S1/S2 columns for the current in-progress lap.
        rows = []
        for raw_id in self.ALL_RAW_IDS:
            entry   = self._ref_table.get(raw_id, {"raw_id": raw_id, "best_lap": 0, "s1": 0, "s2": 0, "s3": 0})
            lap_ms  = entry.get("best_lap", 0)
            pb_s1   = entry.get("s1", 0)
            pb_s2   = entry.get("s2", 0)
            pb_s3   = entry.get("s3", 0)
            has_pb  = lap_ms > 0

            # Live sector data for current lap
            live    = self._live_sectors.get(raw_id, {})
            split_nr = live.get("split_nr", 2)
            spl1    = live.get("spl1", 0)
            spl2    = live.get("spl2", 0)

            # Live S1: visible when split_nr is 0 or 1
            live_s1_ms    = spl1 if split_nr in (0, 1) and spl1 > 0 else 0
            live_s2_ms    = (spl2 - spl1) if split_nr == 1 and spl2 > spl1 > 0 else 0
            live_s1_color = self._sector_color(live_s1_ms, pb_s1, self._global_best_s1, has_pb) if live_s1_ms > 0 else "empty"
            live_s2_color = self._sector_color(live_s2_ms, pb_s2, self._global_best_s2, has_pb) if live_s2_ms > 0 else "empty"

            rows.append({
                "raw_id":       raw_id,
                "best_lap":     self._fmt_time(lap_ms),
                "best_lap_ms":  lap_ms,
                "s1":           self._fmt_time(pb_s1),
                "s2":           self._fmt_time(pb_s2),
                "s3":           self._fmt_time(pb_s3),
                "s1_color":     self._sector_color(pb_s1, pb_s1, self._global_best_s1, has_pb) if has_pb else "empty",
                "s2_color":     self._sector_color(pb_s2, pb_s2, self._global_best_s2, has_pb) if has_pb else "empty",
                "s3_color":     self._sector_color(pb_s3, pb_s3, self._global_best_s3, has_pb) if has_pb else "empty",
                "live_s1":      self._fmt_time(live_s1_ms),
                "live_s2":      self._fmt_time(live_s2_ms),
                "live_s1_color": live_s1_color,
                "live_s2_color": live_s2_color,
            })
        # Sort: drivers with time first (ascending), without time at the end
        rows.sort(key=lambda r: r["best_lap_ms"] if r["best_lap_ms"] > 0 else 10**9)
        for i, r in enumerate(rows):
            r["pos"] = i + 1
        return rows

    def _build_race_json(self) -> dict:
        """Build race.json for the Race overlay.

        Gap formula (stable, uses full-lap average speed):
            vel  = track_length / timeLast_ms   (units/ms)
            gap  = delta_field_9E / vel
                 = delta_field_9E * timeLast_ms / track_length

        track_length captured once when leader lap 1→2.
        Gap only shown once track_length is known AND car has timeLast > 0.

        OUT detection: field_9E frozen >10s outside pits AND not all cars frozen
        (all-frozen means pause or pre-race — don't flag anyone OUT).
        """
        cars = self.sorted_cars
        if not cars:
            return {"cars": [], "leader_lap": 0, "track_length": 0}

        session_clock = self._read_session_clock_ms()

        # ── Capture track_length ───────────────────────────────────────────
        # field_9E is cumulative. At any lap change N-1 → N, the delta of field_9E
        # over the previous lap = exactly 1 track_length.
        # We store field_9E at the previous lap change to compute the delta.
        leader      = cars[0]
        leader_lap  = int(leader.get("lapNr", 0) or 0)
        prev_ll     = self._race_prev_leader_lap
        leader_9e   = int(leader.get("field_9E", 0) or 0)

        if self._race_track_length is None and prev_ll is not None and leader_lap > prev_ll and leader_lap >= 2:
            prev_9e = self._race_prev_leader_9e
            if prev_9e is not None and leader_9e > prev_9e:
                # delta = exactly one lap of track distance
                self._race_track_length = leader_9e - prev_9e
            elif leader_lap == 2 and leader_9e > 0:
                # First lap completed from scratch — field_9E = 1 × track_length
                self._race_track_length = leader_9e

        # Store field_9E at each lap change to compute delta next time
        if prev_ll is not None and leader_lap > prev_ll:
            self._race_prev_leader_9e = leader_9e

        self._race_prev_leader_lap = leader_lap

        track_length = self._race_track_length

        # ── OUT detection: bit 7 of flags_90 = car invisible = out of race ──
        # ── Build rows ─────────────────────────────────────────────────────
        rows = []
        for i, d in enumerate(cars):
            car_id  = int(d.get("carId", 0) or 0)
            raw_id  = car_id - 128 if car_id > 40 else car_id
            pos     = int(d.get("place_sorted", 0) or 0)
            lap     = int(d.get("lapNr", 0) or 0)
            in_pits = bool(d.get("in_pits_guess", False))
            pos9e   = int(d.get("field_9E", 0) or 0)
            time_last_raw = int(d.get("timeLast", 0) or 0)
            time_last_ms  = time_last_raw & 0x0FFFFFFF if time_last_raw > 0 else 0

            is_out  = bool(d.get("is_invisible", False))

            gap_ms  = 0
            gap_str = ""

            if pos > 1 and track_length and i > 0 and not is_out:
                ahead    = cars[i - 1]
                ahead_9e = int(ahead.get("field_9E", 0) or 0)
                delta    = max(0, ahead_9e - pos9e)

                if time_last_ms > 0 and delta >= 0:
                    # gap = delta_units * timeLast / track_length
                    gap_ms  = int(delta * time_last_ms / track_length)
                    mins    = gap_ms // 60000
                    secs    = (gap_ms % 60000) / 1000.0
                    if mins > 0:
                        gap_str = f"+{mins}:{secs:06.3f}"
                    else:
                        gap_str = f"+{secs:.3f}"

            rows.append({
                "car_id":  raw_id,
                "pos":     pos,
                "lap":     lap,
                "gap_str": gap_str,
                "gap_ms":  gap_ms,
                "in_pits": in_pits,
                "is_out":  is_out,
            })

        return {
            "cars":         rows,
            "leader_lap":   leader_lap,
            "track_length": track_length or 0,
        }

    def export_focused_car_json(self):
        # Write focused_car.json, sector_times.json, timing_table.json, race.json.
        if not self.json_export_enabled:
            return
        try:
            now = time.monotonic()

            # Process all cars first (lap completions, out-laps, sector updates)
            self._process_all_laps()

            focused_idx = self.focused_car_index
            snap        = self.snap_by_index.get(focused_idx) if focused_idx is not None else None

            # ── focused_car.json ──────────────────────────────────────────
            if snap is None:
                focused_data = {"focused": False}
            else:
                focused_data = {
                    "focused":      True,
                    "car_id":       snap.get("carId"),
                    "idx":          focused_idx,
                    "position":     snap.get("place_sorted"),
                    "lap":          snap.get("lapNr"),
                    "kph":          round(snap.get("kph", 0.0), 2),
                    "gear":         snap.get("gear"),
                    "revs":         snap.get("revs"),
                    "throttle_pct": round(snap.get("throttle_pct", 0.0), 1),
                }

            # ── sector_times.json ─────────────────────────────────────────
            if snap is None:
                sector_data = {"focused": False}
            else:
                car_id   = snap.get("carId", -1)
                raw_id   = car_id - 128 if car_id > 40 else car_id
                split_nr = snap.get("splitNr", 2)
                spl1     = snap.get("timeLastSpl1", 0)
                spl2     = snap.get("timeLastSpl2", 0)
                last     = snap.get("timeLast",     0)

                is_out_lap   = self._out_lap.get(raw_id, False) or (last == 0)
                post_ts      = self._post_lap_ts.get(raw_id, 0)
                in_post_lap  = (post_ts > 0 and (now - post_ts) < self.POST_LAP_SECS)
                # Can't be in post-lap if it's an out-lap
                if is_out_lap:
                    in_post_lap = False

                # Personal best from ref table
                pb_entry = self._ref_table.get(raw_id, {})
                pb_s1    = pb_entry.get("s1",  0)
                pb_s2    = pb_entry.get("s2",  0)
                pb_s3    = pb_entry.get("s3",  0)
                pb_lap   = pb_entry.get("best_lap", 0)
                has_pb   = pb_lap > 0

                # Timing table sorted → P1 is leader
                table    = self._build_timing_table_json()
                leader   = table[0] if table else None
                leader_s1  = leader["s1"]   if leader else None  # formatted string
                leader_s2  = leader["s2"]   if leader else None
                leader_lap_ms = leader["best_lap_ms"] if leader else 0

                # Position of focused driver in timing table
                focused_pos = next(
                    (r["pos"] for r in table if r["raw_id"] == raw_id), None
                )

                # ── GAP calculation ───────────────────────────────────────
                # E1 (split_nr=2): no gap
                # E2 (split_nr=0, S1 done): gap = cur_s1 vs leader_s1_ms
                # E3 (split_nr=1, S1+S2 done): gap = (cur_s1+cur_s2) vs leader_(s1+s2)_ms
                # E4 (post_lap): gap = pb_lap vs leader_lap
                gap_str = None
                gap_ms  = None

                if in_post_lap and leader_lap_ms > 0:
                    # Use the lap that JUST completed, not the personal best
                    last_completed_lap = self._last_completed.get(raw_id, {}).get("lap", 0)
                    if last_completed_lap > 0:
                        gap_ms  = last_completed_lap - leader_lap_ms
                        gap_str = self._fmt_gap(gap_ms)

                elif not in_post_lap and not is_out_lap and leader:
                    leader_s1_ms = leader.get("s1_ms", 0)
                    leader_s2_ms = leader.get("s2_ms", 0)
                    # We need raw ms from ref table for leader
                    leader_entry = self._ref_table.get(leader["raw_id"], {})
                    ls1_ms = leader_entry.get("s1", 0)
                    ls2_ms = leader_entry.get("s2", 0)

                    if split_nr == 0 and spl1 > 0 and ls1_ms > 0:
                        gap_ms  = spl1 - ls1_ms
                        gap_str = self._fmt_gap(gap_ms)
                    elif split_nr == 1 and spl2 > 0 and ls1_ms > 0 and ls2_ms > 0:
                        cur_s1s2    = spl2
                        leader_s1s2 = ls1_ms + ls2_ms
                        gap_ms  = cur_s1s2 - leader_s1s2
                        gap_str = self._fmt_gap(gap_ms)

                # ── Sector colors ─────────────────────────────────────────
                if in_post_lap:
                    # Colors of the lap that just completed
                    post_entry = self._last_completed.get(raw_id, {})
                    lc_s1 = post_entry.get("s1", 0)
                    lc_s2 = post_entry.get("s2", 0)
                    lc_s3 = post_entry.get("s3", 0)
                    c1 = self._sector_color(lc_s1, pb_s1, self._global_best_s1, has_pb)
                    c2 = self._sector_color(lc_s2, pb_s2, self._global_best_s2, has_pb)
                    c3 = self._sector_color(lc_s3, pb_s3, self._global_best_s3, has_pb)
                elif is_out_lap:
                    c1 = c2 = c3 = "empty"
                else:
                    # Live: color sectors as they complete
                    c1 = "empty"
                    c2 = "empty"
                    c3 = "empty"
                    if split_nr in (0, 1) and spl1 > 0:
                        c1 = self._sector_color(spl1, pb_s1, self._global_best_s1, has_pb)
                    if split_nr == 1 and spl2 > spl1 > 0:
                        cur_s2 = spl2 - spl1
                        c2 = self._sector_color(cur_s2, pb_s2, self._global_best_s2, has_pb)

                sector_data = {
                    "focused":          True,
                    "car_id":           raw_id,
                    "position":         focused_pos,
                    "split_nr":         split_nr,
                    "in_post_lap":      in_post_lap,
                    "is_out_lap":       is_out_lap,
                    "gap_str":          gap_str,
                    "gap_ms":           gap_ms,
                    "s1_color":         c1,
                    "s2_color":         c2,
                    "s3_color":         c3,
                    "last_lap_str":     self._fmt_time(last),
                    "session_clock_ms": self._read_session_clock_ms(),
                    "lap_start_ms":     self._lap_start_session_ms.get(raw_id, 0),
                }

            # ── timing_table.json ─────────────────────────────────────────
            table_data = {
                "table": self._build_timing_table_json(),
                "global_best_s1": self._fmt_time(self._global_best_s1),
                "global_best_s2": self._fmt_time(self._global_best_s2),
                "global_best_s3": self._fmt_time(self._global_best_s3),
            }

            # Write qualy/practice files every tick
            for filename, data in [
                ("focused_car.json",   focused_data),
                ("sector_times.json",  sector_data),
                ("timing_table.json",  table_data),
            ]:
                path = os.path.join(self.json_export_folder, filename)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)

            # Write race.json every 5 ticks (gap calc doesn't need sub-second updates)
            self._race_tick_counter += 1
            if self._race_tick_counter >= 5:
                self._race_tick_counter = 0
                race_data = self._build_race_json()
                path = os.path.join(self.json_export_folder, "race.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(race_data, f, indent=2)

            if self.ui_json_export_status is not None:
                dpg.set_value(self.ui_json_export_status, f"OK → {self.json_export_folder}")

        except Exception as e:
            if self.ui_json_export_status is not None:
                dpg.set_value(self.ui_json_export_status, f"Error: {e}")

    def write_field_bytes(self, car_index: int, struct_offset: int, payload: bytes):
        if struct_offset < 0 or struct_offset + len(payload) > self.record_size:
            raise ValueError("Write would go out of record bounds.")
        assert self.pm is not None
        addr = self.field_addr(car_index, struct_offset)
        self.pm.write_bytes(addr, payload, len(payload))

    # ---------- UI ----------
    def _set_status(self, s: str):
        if self.ui_status is not None:
            dpg.set_value(self.ui_status, s + (' | v 0.3 by ilya-ssh'))

    def set_enable_writes(self, v: bool):
        self.enable_writes = bool(v)
        if self.ui_enable_writes is not None:
            dpg.set_value(self.ui_enable_writes, self.enable_writes)
        self._apply_write_state()
        self.schedule_save_config()

    def _apply_write_state(self):
        for rows in self.rows_by_tab.values():
            for r in rows:
                r.set_writable(self.enable_writes)

    def _require_writes_enabled(self) -> bool:
        if not self.enable_writes:
            msg_warn("Writes disabled", "Enable 'Enable writes' first.")
            return False
        if not self.pm:
            msg_error("Not connected", "Connect to the process first.")
            return False
        return True

    def connect(self):
        self.auto_refresh = False

        proc = str(dpg.get_value(self.ui_process)).strip()
        if not proc:
            msg_error("Input error", "Process name is empty.")
            return

        try:
            self.anchor_struct_offset = parse_offset_hex(str(dpg.get_value(self.ui_anchor_ofs)))
            self.record_size = parse_offset_hex(str(dpg.get_value(self.ui_record_size)))
        except Exception as e:
            msg_error("Input error", f"Bad anchor/record hex:\n{e}")
            return

        try:
            pm = pymem.Pymem(proc)
        except Exception as e:
            msg_error("Connect error", f"Failed to open process '{proc}':\n{e}")
            return

        self.pm = pm

        try:
            self.base_anchor = parse_offset_hex(str(dpg.get_value(self.ui_base_anchor)))
        except Exception as e:
            msg_error("Input error", f"Bad base anchor:\n{e}")
            return

        self.process_name = proc
        self.base_anchor_text = str(dpg.get_value(self.ui_base_anchor))
        self.anchor_ofs_text = str(dpg.get_value(self.ui_anchor_ofs))
        self.record_size_text = str(dpg.get_value(self.ui_record_size))

        self._set_status(
            f"Connected to {proc} | base_anchor=0x{self.base_anchor:08X} | "
            f"anchor_ofs=0x{self.anchor_struct_offset:X} | rec=0x{self.record_size:X}"
        )

        self.refresh_once()
        self.start_auto_refresh()


    def start_auto_refresh(self):
        self.auto_refresh = True
        self._next_refresh_due = 0.0

    def stop_auto_refresh(self):
        self.auto_refresh = False

    def _on_select_car(self, _sender, _app_data, user_data):
        idx = int(user_data)
        if self.selected_index is not None and self.selected_index in self.ov_sel_ids:
            dpg.set_value(self.ov_sel_ids[self.selected_index], False)
        self.selected_index = idx
        if idx in self.ov_sel_ids:
            dpg.set_value(self.ov_sel_ids[idx], True)
        self._render_details()

    def _parse_record(self, index: int, record_start: int, buf: bytes) -> Dict:
        def u8(off: int) -> int:
            return buf[off] if off < len(buf) else 0

        def i16(off: int) -> int:
            return struct.unpack_from("<h", buf, off)[0]

        def i32(off: int) -> int:
            return struct.unpack_from("<i", buf, off)[0]

        def u32(off: int) -> int:
            return struct.unpack_from("<I", buf, off)[0]

        d: Dict = {"index": index, "record_start": record_start, "buf": buf}

        d["lapNr"] = u8(OFS["lapNr"])
        d["csIndex"] = u8(OFS["csIndex"])
        d["curSeg"] = i32(OFS["curSeg"])
        d["segDistFactor"] = i16(OFS["segDistFactor"])
        d["segDist"] = i16(OFS["segDist"])

        d["gear"] = u8(OFS["gear"])
        d["revs"] = i16(OFS["revs"])
        d["enginePower"] = i16(OFS["enginePower"])
        d["throttle"] = i16(OFS["throttle"])

        d["fuelLoad"] = u32(OFS["fuelLoad"])
        d["numPitStops"] = u8(OFS["numPitStops"])
        d["damageRel"] = i32(OFS["damageRel"])

        d["flags_7C"] = u8(OFS["flags_7C"])

        pb = u8(OFS["place_byte"])
        d["place_byte_raw"] = pb
        d["place_byte_rank"] = (pb // 2) + 1

        rp = u8(OFS["racePos"])
        d["racePos_raw"] = rp
        d["racePos_place"] = racepos_to_place(rp)

        d["carId"] = u8(OFS["carId"])

        d["splitNr"]      = u8(OFS["splitNr"])
        d["timeLastSpl1"] = i32(OFS["timeLastSpl1"])
        d["timeLastSpl2"] = i32(OFS["timeLastSpl2"])
        d["timeLast"]     = i32(OFS["timeLast"])
        d["timeBestSpl1"] = i32(OFS["timeBestSpl1"])
        d["timeBestSpl2"] = i32(OFS["timeBestSpl2"])
        d["timeBest"]     = i32(OFS["timeBest"])
        d["timeLapStart"]  = u32(OFS["timeLapStart"])
        d["in_pits_guess"] = bool(u8(OFS["flags_23"]) & (1 << 5))
        d["field_9E"]      = i32(OFS["field_9E"])
        d["is_invisible"]  = bool(u8(OFS["flags_90"]) & (1 << 7))

        speed_raw = u32(OFS["speed_raw_u32"])
        d["speed_raw"] = speed_raw
        d["kph"] = speed_raw / SPEED_RAW_PER_KPH if speed_raw else 0.0

        if d["enginePower"] > 0:
            d["throttle_pct"] = (d["throttle"] / d["enginePower"]) * 100.0
        else:
            d["throttle_pct"] = 0.0

        d["kph_calc"] = f"{d['kph']:.2f}"
        return d

    def _read_all(self):
        pm = self.pm
        assert pm is not None

        cars: List[Dict] = []

        rs0 = self.record_start_addr(0)
        total = int(self.record_size) * int(self.car_count)

        try:
            blob = pm.read_bytes(rs0, total)
            if not blob or len(blob) != total:
                raise RuntimeError("short bulk read")
            for i in range(self.car_count):
                off = i * self.record_size
                buf = blob[off:off + self.record_size]
                cars.append(self._parse_record(i, rs0 + off, buf))
        except Exception:
            for i in range(self.car_count):
                rs = self.record_start_addr(i)
                try:
                    buf = pm.read_bytes(rs, self.record_size)
                except Exception:
                    buf = b"\x00" * self.record_size
                cars.append(self._parse_record(i, rs, buf))

        mode = self.sort_mode

        if mode == "RacePos (0xA4)":
            cars.sort(
                key=lambda d: (
                    (d.get("racePos_raw", 0xFF) >= 0xFE),
                    d.get("racePos_raw", 0xFF),
                    -d.get("lapNr", 0),
                    -d.get("csIndex", 0),
                    -d.get("segDistFactor", 0),
                ),
                reverse=False,
            )
        elif mode == "Lap+csIndex+segDistFactor":
            cars.sort(
                key=lambda d: (
                    d.get("lapNr", 0),
                    d.get("csIndex", 0),
                    d.get("segDistFactor", 0),
                    d.get("segDist", 0),
                    -d.get("index", 0),
                ),
                reverse=True,
            )
        elif mode == "Lap+curSeg+segDistFactor":
            cars.sort(
                key=lambda d: (
                    d.get("lapNr", 0),
                    d.get("curSeg", 0),
                    d.get("segDistFactor", 0),
                    d.get("segDist", 0),
                    -d.get("index", 0),
                ),
                reverse=True,
            )
        elif mode == "Legacy place byte (0x66)":
            cars.sort(key=lambda d: (d.get("place_byte_rank", 9999), d.get("index", 0)))
        else:
            cars.sort(key=lambda d: d.get("index", 0))

        for rank, d in enumerate(cars, start=1):
            d["place_sorted"] = rank

        self.sorted_cars = cars
        self.snap_by_index = {d["index"]: d for d in cars}

        # Read focused car once per refresh cycle
        self.focused_car_index = self.read_focused_car_index()

    def _render_overview(self):
        if self.ov_table is None:
            return

        # reorder rows based on sorted list (move appends in sequence)
        for d in self.sorted_cars:
            idx = d["index"]
            row_id = self.ov_row_ids.get(idx)
            if row_id is not None:
                dpg.move_item(row_id, parent=self.ov_table)

        for d in self.sorted_cars:
            idx = d["index"]
            cell_ids = self.ov_cell_ids.get(idx)
            if not cell_ids:
                continue

            values = [
                ">>>" if d.get("index") == self.focused_car_index else "",
                ordinal(d.get("place_sorted", 9999)),
                ordinal(d.get("racePos_place", None)),
                str(d.get("racePos_raw", "")),
                ordinal(d.get("place_byte_rank", 9999)),
                str(d.get("carId", "-")),
                str(idx),
                str(d.get("lapNr", 0)),
                str(d.get("csIndex", 0)),
                str(d.get("segDistFactor", 0)),
                f"{d.get('kph', 0.0):.2f}",
                str(d.get("gear", 0)),
                str(d.get("revs", 0)),
                f"{d.get('throttle_pct', 0.0):.1f}",
                str(d.get("fuelLoad", 0)),
                str(d.get("numPitStops", 0)),
                str(d.get("damageRel", 0)),
            ]

            for i, s in enumerate(values):
                if i < len(cell_ids):
                    dpg.set_value(cell_ids[i], s)

        # ensure selection
        if self.selected_index is None and self.sorted_cars:
            self.selected_index = self.sorted_cars[0]["index"]
            if self.selected_index in self.ov_sel_ids:
                dpg.set_value(self.ov_sel_ids[self.selected_index], True)

    def _render_details(self):
        if self.selected_index is None:
            return
        snap = self.snap_by_index.get(self.selected_index)
        if not snap:
            return
        # update normal tabs
        for rows in self.rows_by_tab.values():
            for r in rows:
                r.refresh_from_snapshot(snap)
        # update full struct only when its tab is shown
        if self.full_struct_tab_id is not None and dpg.is_item_shown(self.full_struct_tab_id):
            self.full_struct.refresh_from_snapshot(snap)
        self._sync_scripts_status_ui()

    def refresh_once(self):
        if not self.pm:
            return
        try:
            self._read_all()
            self.scripts.tick_snapshot(self.sorted_cars)
            self._render_overview()
            self._render_details()
            self.export_focused_car_json()
        except Exception as e:
            self._set_status(f"Refresh failed: {e}")
            self.stop_auto_refresh()

    # ---------- grid scan (threaded) ----------
    def _iter_committed_regions(self, rw_only: bool = True):
        if not self.pm:
            return
        handle = self.pm.process_handle

        rw_access = {PAGE_READWRITE, PAGE_WRITECOPY, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY}
        rd_access = rw_access | {PAGE_READONLY, PAGE_EXECUTE_READ}

        addr = 0
        max_addr = 0xFFFFFFFF

        while addr < max_addr:
            try:
                mbi = pymem.memory.virtual_query(handle, addr)
            except Exception:
                break

            base = int(mbi.BaseAddress)
            size = int(mbi.RegionSize)
            state = int(mbi.State)
            protect = int(mbi.Protect)

            addr = base + max(1, size)

            if state != MEM_COMMIT:
                continue
            if protect & PAGE_GUARD:
                continue

            access = protect & 0xFF
            if access == 0 or access == PAGE_NOACCESS:
                continue

            if rw_only:
                if access not in rw_access:
                    continue
            else:
                if access not in rd_access:
                    continue

            yield base, size

    def _score_candidate_record_start(self, record_start0: int, check_n: int) -> Optional[Dict[str, Any]]:
        if not self.pm:
            return None

        stride = int(self.record_size)
        check_n = max(8, min(int(check_n), 40))

        try:
            buf = self.pm.read_bytes(int(record_start0) & 0xFFFFFFFF, stride * check_n)
            if not buf or len(buf) != stride * check_n:
                return None
        except Exception:
            return None

        rp = []
        pb = []
        cid = []
        team = []

        for i in range(check_n):
            base = i * stride
            try:
                lap_i = buf[base + OFS["lapNr"]]
                gear_i = buf[base + OFS["gear"]]
                team_i = buf[base + OFS["teamNr"]]
                rp_i = buf[base + OFS["racePos"]]
                pb_i = buf[base + OFS["place_byte"]]
                cid_i = buf[base + OFS["carId"]]
            except Exception:
                return None

            if lap_i > 250:
                return None
            if gear_i > 12:
                return None
            if team_i > 60:
                return None

            team.append(team_i)
            rp.append(rp_i)
            pb.append(pb_i)
            cid.append(cid_i)

        rp_pat = sum(1 for i, v in enumerate(rp) if v == ((2 * i) & 0xFF))
        pb_pat = sum(1 for i, v in enumerate(pb) if v == ((2 * i) & 0xFF))
        eq_rp_pb = sum(1 for i in range(check_n) if rp[i] == pb[i])

        cid_max = max(cid) if cid else 0
        cid_nonzero = [v for v in cid if v != 0]
        cid_dup_nonzero = len(cid_nonzero) - len(set(cid_nonzero))

        team_unique = len(set(team))
        team_dups = check_n - team_unique

        score = 0
        score += rp_pat * 10
        score += pb_pat * 10
        score += eq_rp_pb * 4
        score += team_dups * 2

        if cid_max > 99:
            score -= 2000
        score -= cid_dup_nonzero * 400

        base_anchor = int(record_start0 + int(self.anchor_struct_offset)) & 0xFFFFFFFF

        return {
            "score": score,
            "record_start0": int(record_start0) & 0xFFFFFFFF,
            "base_anchor": base_anchor,
        }

    def _liveness_score_candidate(self, record_start0: int, sample_n: int = 6, wait_ms: int = 120) -> int:
        if not self.pm:
            return 0
        stride = int(self.record_size)
        sample_n = max(1, min(int(sample_n), int(self.car_count), 12))

        def snap() -> Optional[List[Tuple[int, int, int]]]:
            try:
                b = self.pm.read_bytes(int(record_start0) & 0xFFFFFFFF, stride * sample_n)
                if not b or len(b) != stride * sample_n:
                    return None
            except Exception:
                return None

            out: List[Tuple[int, int, int]] = []
            for i in range(sample_n):
                base = i * stride
                try:
                    t = struct.unpack_from("<i", b, base + OFS["timerVarSeq"])[0]
                    r = struct.unpack_from("<h", b, base + OFS["revs"])[0]
                    th = struct.unpack_from("<h", b, base + OFS["throttle"])[0]
                except Exception:
                    return None
                out.append((t, r, th))
            return out

        a = snap()
        if a is None:
            return 0
        time.sleep(max(0, int(wait_ms)) / 1000.0)
        b = snap()
        if b is None:
            return 0

        return sum(1 for i in range(min(len(a), len(b))) if a[i] != b[i])

    def _scan_record_start_by_grid_pattern(
        self,
        min_required: int = 24,
        chunk_size: int = 0x400000,
        rw_only: bool = True,
        max_candidates: int = 40,
    ) -> Optional[Dict[str, Any]]:
        if not self.pm:
            return None

        stride = int(self.record_size)
        if stride <= 0 or stride > 0x20000:
            return None

        min_required = max(8, min(int(min_required), 40))
        pattern = bytes([(2 * i) & 0xFF for i in range(min_required)])

        field_candidates = [OFS["racePos"], OFS["place_byte"]]
        overlap = (stride * (min_required - 1)) + 1

        seen: set[int] = set()
        candidates: List[Dict[str, Any]] = []

        for region_base, region_size in self._iter_committed_regions(rw_only=rw_only):
            region_end = region_base + region_size
            addr = region_base

            while addr < region_end:
                read_size = min(chunk_size + overlap, region_end - addr)
                try:
                    buf = self.pm.read_bytes(addr, read_size)
                except Exception:
                    addr += chunk_size
                    continue

                for field_ofs in field_candidates:
                    for delta in range(stride):
                        start = delta + field_ofs
                        if start >= len(buf):
                            break

                        seq = buf[start::stride]
                        pos = seq.find(pattern)
                        if pos == -1:
                            continue

                        record_start0 = int(addr + delta + pos * stride) & 0xFFFFFFFF
                        if record_start0 in seen:
                            continue
                        seen.add(record_start0)

                        info = self._score_candidate_record_start(record_start0, check_n=min_required)
                        if not info:
                            continue

                        candidates.append(info)
                        candidates.sort(key=lambda d: d["score"], reverse=True)
                        if len(candidates) > max_candidates:
                            candidates = candidates[:max_candidates]

                addr += chunk_size

        if not candidates:
            return None

        top = candidates[: min(6, len(candidates))]
        best = None
        best_total = -10**9
        for c in top:
            live = self._liveness_score_candidate(c["record_start0"], sample_n=6, wait_ms=120)
            total = int(c["score"]) + live * 600
            c["live_changed"] = live
            c["total_score"] = total
            if total > best_total:
                best_total = total
                best = c
        return best

    def find_base_from_grid(self):
        if self._grid_scan_running:
            return

        proc = str(dpg.get_value(self.ui_process)).strip()
        if not proc:
            msg_error("Input error", "Process name is empty.")
            return

        if not self.pm:
            try:
                self.pm = pymem.Pymem(proc)
            except Exception as e:
                msg_error("Connect error", f"Failed to open process '{proc}':\n{e}")
                return

        try:
            self.anchor_struct_offset = parse_offset_hex(str(dpg.get_value(self.ui_anchor_ofs)))
            self.record_size = parse_offset_hex(str(dpg.get_value(self.ui_record_size)))
        except Exception as e:
            msg_error("Input error", f"Bad anchor/record hex:\n{e}")
            return

        self.stop_auto_refresh()
        self._grid_scan_running = True
        self._set_status("Scanning memory for LIVE car structs… (starting grid / race init)")

        def worker():
            err = None
            best = None
            try:
                for need in (24, 20, 16, 12):
                    best = self._scan_record_start_by_grid_pattern(min_required=need, rw_only=True)
                    if best:
                        break
                if not best:
                    for need in (24, 20, 16, 12):
                        best = self._scan_record_start_by_grid_pattern(min_required=need, rw_only=False)
                        if best:
                            break
            except Exception as e:
                err = str(e)

            def done():
                self._grid_scan_running = False
                if err:
                    msg_error("Scan failed", err)
                    self._set_status("Grid scan failed.")
                    return
                if not best:
                    msg_info(
                        "Not found",
                        "Could not find a valid LIVE car struct array.\n\n"
                        "Try:\n"
                        "- click it right after race is initialized / on the grid\n"
                        "- ensure record_size is correct (often 0x330)\n",
                    )
                    self._set_status("Grid scan: not found.")
                    return

                self.base_anchor = int(best["base_anchor"]) & 0xFFFFFFFF
                self.base_anchor_text = f"0x{self.base_anchor:08X}"
                dpg.set_value(self.ui_base_anchor, self.base_anchor_text)

                self._set_status(
                    "Grid scan OK | "
                    f"record_start0=0x{best['record_start0']:08X} | "
                    f"base_anchor=0x{self.base_anchor:08X} | "
                    f"static={best.get('score')} | live_changed={best.get('live_changed', 0)}"
                )

                self.refresh_once()
                self.start_auto_refresh()

            self.ui_queue.put(done)

        threading.Thread(target=worker, daemon=True).start()

    def _sync_scripts_status_ui(self):
        # update status strings in UI if present
        if "sc_status" in self._scripts_ui_ids:
            dpg.set_value(self._scripts_ui_ids["sc_status"], self.scripts.sc_status)
        if "slip_status" in self._scripts_ui_ids:
            dpg.set_value(self._scripts_ui_ids["slip_status"], self.scripts.slip_status)
        if "slip_debug" in self._scripts_ui_ids:
            dpg.set_value(self._scripts_ui_ids["slip_debug"], self.scripts.slip_debug)
        if "seen_range" in self._scripts_ui_ids:
            dpg.set_value(self._scripts_ui_ids["seen_range"], self.scripts.seen_range)
        if "locked_range" in self._scripts_ui_ids:
            dpg.set_value(self._scripts_ui_ids["locked_range"], self.scripts.locked_range)
        if "fd_status" in self._scripts_ui_ids:
            pending = len(self.scripts._fuel_drain_until)
            total   = len(self.scripts.fuel_drain_targets)
            if pending:
                dpg.set_value(self._scripts_ui_ids["fd_status"], f"Drenando: {pending} autos activos...")
            else:
                dpg.set_value(self._scripts_ui_ids["fd_status"], f"Listo — {total} autos configurados")

    def _build_scripts_ui(self, parent):
        with dpg.group(parent=parent):
            with dpg.tab_bar():
                with dpg.tab(label="Safety Car"):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Status:")
                        self._scripts_ui_ids["sc_status"] = dpg.add_text(self.scripts.sc_status)
                        dpg.add_spacer(width=20)
                        dpg.add_text("Tick (ms):")
                        dpg.add_input_float(
                            default_value=float(self.scripts.tick_ms),
                            width=120,
                            callback=lambda s, a, u: self._set_script_attr("tick_ms", float(a)),
                        )
                    dpg.add_separator()

                    with dpg.group(horizontal=True):
                        dpg.add_text("Seen curSeg range:")
                        self._scripts_ui_ids["seen_range"] = dpg.add_text(self.scripts.seen_range)
                        dpg.add_button(label="Reset seen", callback=lambda *_: (self.scripts.reset_seen_range(), self._sync_scripts_status_ui()))
                    with dpg.group(horizontal=True):
                        dpg.add_text("Locked range:")
                        self._scripts_ui_ids["locked_range"] = dpg.add_text(self.scripts.locked_range)
                        dpg.add_button(label="Lock from seen", callback=lambda *_: (self.scripts.lock_range_from_seen(), self._sync_scripts_status_ui()))
                        dpg.add_button(label="Unlock", callback=lambda *_: (self.scripts.unlock_range(), self._sync_scripts_status_ui()))

                    dpg.add_separator()

                    with dpg.group(horizontal=True):
                        dpg.add_text("Base / Min / Max (kph):")
                        dpg.add_input_float(default_value=float(self.scripts.base_kph), width=110,
                                            callback=lambda s, a, u: self._set_script_attr("base_kph", float(a)))
                        dpg.add_input_float(default_value=float(self.scripts.min_kph), width=110,
                                            callback=lambda s, a, u: self._set_script_attr("min_kph", float(a)))
                        dpg.add_input_float(default_value=float(self.scripts.max_kph), width=110,
                                            callback=lambda s, a, u: self._set_script_attr("max_kph", float(a)))

                    with dpg.group(horizontal=True):
                        dpg.add_text("Gap behind leader (seg):")
                        dpg.add_input_float(default_value=float(self.scripts.gap_segments), width=130,
                                            callback=lambda s, a, u: self._set_script_attr("gap_segments", float(a)))
                        dpg.add_text("Gain (kph/seg):")
                        dpg.add_input_float(default_value=float(self.scripts.gain_kph_per_seg), width=130,
                                            callback=lambda s, a, u: self._set_script_attr("gain_kph_per_seg", float(a)))

                    with dpg.group(horizontal=True):
                        dpg.add_text("Speed units per kph:")
                        dpg.add_input_float(default_value=float(self.scripts.speed_units_per_kph), width=130,
                                            callback=lambda s, a, u: self._set_script_attr("speed_units_per_kph", float(a)))

                    with dpg.group(horizontal=True):
                        dpg.add_text("Order:")
                        dpg.add_combo(
                            items=["track", "racepos"],
                            default_value=self.scripts.order,
                            width=130,
                            callback=lambda s, a, u: self._set_script_attr("order", str(a)),
                        )

                    dpg.add_checkbox(
                        label="Freeze fuel during SC (fuelLoad)",
                        default_value=bool(self.scripts.freeze_fuel),
                        callback=lambda s, a, u: self._set_script_attr("freeze_fuel", bool(a)),
                    )

                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(
                            label="Free-run if gap to car ahead >",
                            default_value=bool(self.scripts.free_run_enabled),
                            callback=lambda s, a, u: self._set_script_attr("free_run_enabled", bool(a)),
                        )
                        dpg.add_input_float(
                            default_value=float(self.scripts.free_run_gap),
                            width=130,
                            callback=lambda s, a, u: self._set_script_attr("free_run_gap", float(a)),
                        )
                        dpg.add_text("seg")

                    dpg.add_separator()
                    dpg.add_text("Eligibility / filters")
                    dpg.add_checkbox(label="Include player car", default_value=bool(self.scripts.include_player),
                                     callback=lambda s, a, u: self._set_script_attr("include_player", bool(a)))
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(label="Ignore pits", default_value=bool(self.scripts.ignore_pits),
                                         callback=lambda s, a, u: self._set_script_attr("ignore_pits", bool(a)))
                        dpg.add_checkbox(label="Ignore invisible", default_value=bool(self.scripts.ignore_invisible),
                                         callback=lambda s, a, u: self._set_script_attr("ignore_invisible", bool(a)))
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(label="Ignore fail flags", default_value=bool(self.scripts.ignore_failflags),
                                         callback=lambda s, a, u: self._set_script_attr("ignore_failflags", bool(a)))
                        dpg.add_checkbox(label="Ignore invalid racePos", default_value=bool(self.scripts.ignore_invalid_racepos),
                                         callback=lambda s, a, u: self._set_script_attr("ignore_invalid_racepos", bool(a)))

                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Activate", callback=lambda *_: (self.scripts.sc_activate(), self._sync_scripts_status_ui()))
                        dpg.add_button(label="Release", callback=lambda *_: (self.scripts.sc_release(), self._sync_scripts_status_ui()))

                with dpg.tab(label="Slipstream"):
                    with dpg.group(horizontal=True):
                        self._scripts_ui_ids["slip_status"] = dpg.add_text(self.scripts.slip_status)
                        dpg.add_spacer(width=20)
                        self._scripts_ui_ids["slip_debug"] = dpg.add_text(self.scripts.slip_debug)

                    dpg.add_checkbox(label="Slipstream enabled", default_value=bool(self.scripts.slipstream_enabled),
                                     callback=lambda s, a, u: self._set_script_attr("slipstream_enabled", bool(a)))
                    dpg.add_checkbox(label="Same lap only", default_value=bool(self.scripts.slip_same_lap_only),
                                     callback=lambda s, a, u: self._set_script_attr("slip_same_lap_only", bool(a)))
                    dpg.add_checkbox(label="Require segPosX sign match", default_value=bool(self.scripts.slip_require_same_side),
                                     callback=lambda s, a, u: self._set_script_attr("slip_require_same_side", bool(a)))

                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_text("Min speed (kph):")
                        dpg.add_input_float(default_value=float(self.scripts.slip_min_kph), width=130,
                                            callback=lambda s, a, u: self._set_script_attr("slip_min_kph", float(a)))
                        dpg.add_text("Max gap (seg):")
                        dpg.add_input_float(default_value=float(self.scripts.slip_range_seg), width=130,
                                            callback=lambda s, a, u: self._set_script_attr("slip_range_seg", float(a)))
                    with dpg.group(horizontal=True):
                        dpg.add_text("enginePower mult:")
                        dpg.add_input_float(default_value=float(self.scripts.slip_engine_mult), width=130,
                                            callback=lambda s, a, u: self._set_script_attr("slip_engine_mult", float(a)))
                        dpg.add_text("Hold (ms):")
                        dpg.add_input_float(default_value=float(self.scripts.slip_hold_ms), width=130,
                                            callback=lambda s, a, u: self._set_script_attr("slip_hold_ms", float(a)))

                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Slip ON", callback=lambda *_: (self.scripts.slip_on(), self._sync_scripts_status_ui()))
                        dpg.add_button(label="Slip OFF", callback=lambda *_: (self.scripts.slip_off(), self._sync_scripts_status_ui()))

                with dpg.tab(label="Fuel Drain"):
                    dpg.add_text("Escribe fuelLoad=0 durante N segundos en los autos indicados.")
                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_text("Targets (car_ids, ej: 36 22 28):")
                    self._scripts_ui_ids["fd_targets"] = dpg.add_input_text(
                        default_value=" ".join(str(x) for x in self.scripts.fuel_drain_targets),
                        width=300,
                        hint="ej: 36 22 28",
                        callback=lambda s, a, u: self._on_fd_targets_change(a),
                    )
                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_text("Duración (seg):")
                        dpg.add_input_float(
                            default_value=float(self.scripts.fuel_drain_secs),
                            width=100,
                            min_value=0.1,
                            max_value=60.0,
                            callback=lambda s, a, u: self._set_script_attr("fuel_drain_secs", float(a)),
                        )
                    dpg.add_separator()
                    self._scripts_ui_ids["fd_status"] = dpg.add_text(
                        f"Listo — {len(self.scripts.fuel_drain_targets)} autos configurados"
                    )
                    dpg.add_button(
                        label="▶ FIRE",
                        callback=lambda *_: (self.scripts.fuel_drain_fire(), self._sync_scripts_status_ui()),
                    )

    def _do_reset_timing(self):
        """Reset timing table and immediately write the empty table to JSON."""
        self.reset_timing_table()
        if not self.json_export_enabled:
            return
        try:
            table_data = {
                "table": self._build_timing_table_json(),
                "global_best_s1": None,
                "global_best_s2": None,
                "global_best_s3": None,
            }
            path = os.path.join(self.json_export_folder, "timing_table.json")
            with open(path, "w", encoding="utf-8") as f:
                import json as _json
                _json.dump(table_data, f, indent=2)
            if self.ui_json_export_status is not None:
                dpg.set_value(self.ui_json_export_status, "Timing reset OK")
        except Exception as e:
            if self.ui_json_export_status is not None:
                dpg.set_value(self.ui_json_export_status, f"Reset error: {e}")

    def _browse_export_folder(self):
        """Open a native Windows folder picker and update the export folder."""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(initialdir=self.json_export_folder, title="Select export folder")
            root.destroy()
            if folder:
                self.json_export_folder = folder
                if self.ui_json_export_folder is not None:
                    dpg.set_value(self.ui_json_export_folder, folder)
        except Exception as e:
            msg_error("Folder picker", str(e))

    def _set_script_attr(self, name: str, value: Any):
        setattr(self.scripts, name, value)
        self.schedule_save_config()
        self._sync_scripts_status_ui()

    def _on_fd_targets_change(self, text: str):
        targets = []
        for tok in text.replace(",", " ").split():
            try:
                targets.append(int(tok))
            except ValueError:
                pass
        self.scripts.fuel_drain_targets = targets
        self.schedule_save_config()
        self._sync_scripts_status_ui()

    def build_ui(self):
        with dpg.window(label="GP2Mem 0.3", tag="primary_window",no_scrollbar=True) as primary:
            dpg.add_text("Connection / Controls")
            with dpg.group(horizontal=True):
                dpg.add_text("Process:")
                self.ui_process = dpg.add_input_text(default_value=self.process_name, width=220)
                dpg.add_text("Base Anchor:")
                self.ui_base_anchor = dpg.add_input_text(default_value=self.base_anchor_text, width=140)
                dpg.add_text("Anchor Ofs:")
                self.ui_anchor_ofs = dpg.add_input_text(default_value=self.anchor_ofs_text, width=90)
                dpg.add_text("Record:")
                self.ui_record_size = dpg.add_input_text(default_value=self.record_size_text, width=90)

                dpg.add_button(label="Connect", callback=lambda *_: self.connect())
                dpg.add_button(label="Find Base (Grid)", callback=lambda *_: self.find_base_from_grid())
                dpg.add_button(label="Save Config", callback=lambda *_: self.save_config())

            with dpg.group(horizontal=True):
                self.ui_enable_writes = dpg.add_checkbox(
                    label="Enable writes",
                    default_value=bool(self.enable_writes),
                    callback=lambda s, a, u: self.set_enable_writes(bool(a)),
                )

                dpg.add_text("Refresh (ms):")
                self.ui_refresh_ms = dpg.add_input_int(
                    default_value=int(self.refresh_ms),
                    width=120,
                    min_value=50,
                    max_value=5000,
                    callback=lambda s, a, u: self._on_refresh_ms_change(int(a)),
                )

                dpg.add_text("Sort:")
                self.ui_sort_mode = dpg.add_combo(
                    items=[
                        "RacePos (0xA4)",
                        "Lap+csIndex+segDistFactor",
                        "Lap+curSeg+segDistFactor",
                        "Legacy place byte (0x66)",
                        "Index",
                    ],
                    default_value=self.sort_mode,
                    width=260,
                    callback=lambda s, a, u: self._on_sort_change(str(a)),
                )

                dpg.add_button(label="Refresh Once", callback=lambda *_: self.refresh_once())
                dpg.add_button(label="Start Auto", callback=lambda *_: self.start_auto_refresh())
                dpg.add_button(label="Stop Auto", callback=lambda *_: self.stop_auto_refresh())

            # JSON export row
            with dpg.group(horizontal=True):
                self.ui_json_export_checkbox = dpg.add_checkbox(
                    label="Export focused_car.json",
                    default_value=bool(self.json_export_enabled),
                    callback=lambda s, a, u: setattr(self, "json_export_enabled", bool(a)),
                )
                dpg.add_text("Folder:")
                self.ui_json_export_folder = dpg.add_input_text(
                    default_value=self.json_export_folder,
                    width=400,
                    callback=lambda s, a, u: setattr(self, "json_export_folder", str(a).strip() or "."),
                )
                dpg.add_button(label="Browse", callback=lambda *_: self._browse_export_folder())
                dpg.add_button(label="Reset Timing", callback=lambda *_: self._do_reset_timing())
                self.ui_json_export_status = dpg.add_text("—")

            dpg.add_separator()
            # Main content area (leaves space for footer)
            with dpg.child_window(border=False, height=-FOOTER_H-8,  no_scrollbar=True,no_scroll_with_mouse=True,):
                with dpg.table(resizable=True, borders_innerV=True, policy=dpg.mvTable_SizingStretchProp):
                    dpg.add_table_column(label="Overview", init_width_or_weight=0.60)
                    dpg.add_table_column(label="Details", init_width_or_weight=0.40)
                    with dpg.table_row():
                        with dpg.child_window(border=True, height=-1):
                            self._build_overview()
                        with dpg.child_window(border=True, height=-1):
                            self._build_details()

            # Footer (always visible, no scroll needed)
            with dpg.child_window(border=False, height=FOOTER_H, no_scrollbar=True,
        no_scroll_with_mouse=True,):
                self.ui_status = dpg.add_text("v 0.3 by ilya-ssh",)

        dpg.set_primary_window(primary, True)
        self._apply_write_state()

    def _on_refresh_ms_change(self, v: int):
        self.refresh_ms = max(50, int(v))
        self.schedule_save_config()

    def _on_sort_change(self, s: str):
        self.sort_mode = s
        self.schedule_save_config()
        self.refresh_once()

    def _build_overview(self):
        dpg.add_text("Overview (click Sel to select car)")
        self.ov_table = dpg.add_table(
            header_row=True,
            row_background=True,
            resizable=True,
            borders_innerV=True,
            borders_innerH=True,
            scrollY=True,
            height=-1,
        )

        cols = ["Sel", "Foco", "Pos", "PosR", "racePos", "PosB", "CarId", "Idx", "Lap", "csIdx", "SegF", "kph", "Gear", "Revs", "Thr%", "Fuel", "Pit", "Damage"]
        for c in cols:
            dpg.add_table_column(label=c, parent=self.ov_table)

        for idx in range(self.car_count):
            with dpg.table_row(parent=self.ov_table) as row_id:
                self.ov_row_ids[idx] = row_id

                sel_id = dpg.add_selectable(label="", width=30, callback=self._on_select_car, user_data=idx)
                self.ov_sel_ids[idx] = sel_id

                cell_ids = []
                for _ in range(len(cols) - 1):
                    cell_ids.append(dpg.add_text(""))
                self.ov_cell_ids[idx] = cell_ids

    def _build_details(self):
        with dpg.tab_bar():
            # helper to build a tab with a standard field table
            def build_fields_tab(tab_label: str, builders: List[BaseRowDPG]):
                with dpg.tab(label=tab_label):
                    tbl = dpg.add_table(
                        header_row=True,
                        row_background=True,
                        resizable=True,
                        borders_innerV=True,
                        borders_innerH=True,
                        scrollY=True,
                        height=-1,
                    )
                    dpg.add_table_column(label="Field", parent=tbl, init_width_or_weight=200)
                    dpg.add_table_column(label="Value", parent=tbl, init_width_or_weight=250)
                    dpg.add_table_column(label="Write", parent=tbl, init_width_or_weight=70)
                    dpg.add_table_column(label="All", parent=tbl, init_width_or_weight=60)
                    dpg.add_table_column(label="Pretty", parent=tbl, init_width_or_weight=320)

                    self.rows_by_tab[tab_label] = []
                    for row in builders:
                        # builders already constructed rows; ignore
                        pass
                    return tbl

            # Race
            with dpg.tab(label="Race"):
                tbl = dpg.add_table(header_row=True, row_background=True, resizable=True, borders_innerV=True, borders_innerH=True, scrollY=True, height=-1)
                for lab, w in [("Field", 200), ("Value", 250), ("Write", 70), ("All", 60), ("Pretty", 320)]:
                    dpg.add_table_column(label=lab, parent=tbl, init_width_or_weight=w)

                self.rows_by_tab["Race"] = [
                    FieldRowDPG(self, tbl, FieldSpec("lapNr", "lapNr", OFS["lapNr"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("splitNr", "splitNr", OFS["splitNr"], "u8", display="dec", widget="enum", enum=SPLIT_ENUM)),
                    FieldRowDPG(self, tbl, FieldSpec("racePos", "racePos (raw)", OFS["racePos"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("csIndex", "csIndex", OFS["csIndex"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("curSeg", "curSeg", OFS["curSeg"], "i32", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("segDist", "segDist", OFS["segDist"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("segDistFactor", "segDistFactor", OFS["segDistFactor"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("segPosX", "segPosX", OFS["segPosX"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("xPos", "xPos", OFS["xPos"], "i32", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("yPos", "yPos", OFS["yPos"], "i32", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("gear", "gear", OFS["gear"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("teamNr", "teamNr", OFS["teamNr"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("place_byte", "CpyFromRacePos_ (0x66)", OFS["place_byte"], "u8", display="hex")),
                ]

            # Timing
            with dpg.tab(label="Timing"):
                tbl = dpg.add_table(header_row=True, row_background=True, resizable=True, borders_innerV=True, borders_innerH=True, scrollY=True, height=-1)
                for lab, w in [("Field", 200), ("Value", 250), ("Write", 70), ("All", 60), ("Pretty", 320)]:
                    dpg.add_table_column(label=lab, parent=tbl, init_width_or_weight=w)

                self.rows_by_tab["Timing"] = [
                    FieldRowDPG(self, tbl, FieldSpec("timeLapStart", "timeLapStart", OFS["timeLapStart"], "i32", display="hex", pretty_kind="time32")),
                    FieldRowDPG(self, tbl, FieldSpec("timePrevBest", "timePrevBest", OFS["timePrevBest"], "i32", display="hex", pretty_kind="time32")),
                    FieldRowDPG(self, tbl, FieldSpec("timeLastSpl1", "timeLastSpl1", OFS["timeLastSpl1"], "i32", display="hex", pretty_kind="time32")),
                    FieldRowDPG(self, tbl, FieldSpec("timeLastSpl2", "timeLastSpl2", OFS["timeLastSpl2"], "i32", display="hex", pretty_kind="time32")),
                    FieldRowDPG(self, tbl, FieldSpec("timeLast", "timeLast", OFS["timeLast"], "i32", display="hex", pretty_kind="time32")),
                    FieldRowDPG(self, tbl, FieldSpec("timeBestSpl1", "timeBestSpl1", OFS["timeBestSpl1"], "i32", display="hex", pretty_kind="time32")),
                    FieldRowDPG(self, tbl, FieldSpec("timeBestSpl2", "timeBestSpl2", OFS["timeBestSpl2"], "i32", display="hex", pretty_kind="time32")),
                    FieldRowDPG(self, tbl, FieldSpec("timeBest", "timeBest", OFS["timeBest"], "i32", display="hex", pretty_kind="time32")),
                ]

            # Controls
            with dpg.tab(label="Controls"):
                tbl = dpg.add_table(header_row=True, row_background=True, resizable=True, borders_innerV=True, borders_innerH=True, scrollY=True, height=-1)
                for lab, w in [("Field", 200), ("Value", 250), ("Write", 70), ("All", 60), ("Pretty", 320)]:
                    dpg.add_table_column(label=lab, parent=tbl, init_width_or_weight=w)

                self.rows_by_tab["Controls"] = [
                    FieldRowDPG(self, tbl, FieldSpec("kph_calc", "kph (calc, read-only)", None, None, widget="computed")),
                    FieldRowDPG(self, tbl, FieldSpec("speed_raw", "speed_raw_u32 @0x14", OFS["speed_raw_u32"], "u32", display="hex")),
                    FieldRowDPG(self, tbl, FieldSpec("speed_i16", "speed_i16 @0x16", OFS["speed_i16"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("speed2", "speed2 @0x5C", OFS["speed2"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("revs", "revs", OFS["revs"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("enginePower", "enginePower", OFS["enginePower"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("throttle", "throttle", OFS["throttle"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("anaThrottle", "anaThrottle", OFS["anaThrottle"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("anaBrake", "anaBrake", OFS["anaBrake"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("anaSteer", "anaSteer", OFS["anaSteer"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("advspeed_70", "advspeed_70", OFS["advspeed_70"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("accel_72", "accel_72", OFS["accel_72"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("prevGear", "prevGear", OFS["prevGear"], "u8", display="dec")),
                ]

            # Pit / Failure
            with dpg.tab(label="Pit / Failure"):
                tbl = dpg.add_table(header_row=True, row_background=True, resizable=True, borders_innerV=True, borders_innerH=True, scrollY=True, height=-1)
                for lab, w in [("Field", 200), ("Value", 250), ("Write", 70), ("All", 60), ("Pretty", 320)]:
                    dpg.add_table_column(label=lab, parent=tbl, init_width_or_weight=w)

                self.rows_by_tab["Pit / Failure"] = [
                    FieldRowDPG(self, tbl, FieldSpec("fuelLoad", "fuelLoad (u32)", OFS["fuelLoad"], "u32", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("fuelLoadLaps", "fuelLoadLaps (i16)", OFS["fuelLoadLaps"], "i16", display="dec")),
                    PitStopsRowDPG(self, tbl),
                    FieldRowDPG(self, tbl, FieldSpec("failLap", "failLap (lap#)", OFS["failLap"], "u8", display="dec", widget="spin", spin_from=0, spin_to=255)),
                    FieldRowDPG(self, tbl, FieldSpec("failSeg", "failSeg", OFS["failSeg"], "u8", display="dec", widget="spin", spin_from=0, spin_to=255)),
                    FieldRowDPG(self, tbl, FieldSpec("failType", "failType (0xAC)", OFS["failType"], "u8", widget="enum", enum=FAIL_TYPE_ENUM)),
                    FieldRowDPG(self, tbl, FieldSpec("failureType_", "failureType_ (0x272)", OFS["failureType_"], "u8", widget="enum", enum=FAIL_TYPE_ENUM)),
                    FieldRowDPG(self, tbl, FieldSpec("ComeInType", "ComeInType (0x273)", OFS["ComeInType"], "u8", widget="enum", enum=COME_IN_ENUM)),
                    FieldRowDPG(self, tbl, FieldSpec("numStopsDone", "numStopsDone", OFS["numStopsDone"], "u8", display="dec")),
                ]

            # Tires / Damage
            with dpg.tab(label="Tires / Damage"):
                tbl = dpg.add_table(header_row=True, row_background=True, resizable=True, borders_innerV=True, borders_innerH=True, scrollY=True, height=-1)
                for lab, w in [("Field", 200), ("Value", 250), ("Write", 70), ("All", 60), ("Pretty", 320)]:
                    dpg.add_table_column(label=lab, parent=tbl, init_width_or_weight=w)

                self.rows_by_tab["Tires / Damage"] = [
                    FieldRowDPG(self, tbl, FieldSpec("damageRel", "damageRel", OFS["damageRel"], "i32", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("brakes", "brakes", OFS["brakes"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("lockedWheelRel", "lockedWheelRel", OFS["lockedWheelRel"], "u8", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("tirewear", "tirewear[4]", OFS["tirewear"], "i32", count=4, display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("suspTravel", "suspTravel[4]", OFS["suspTravel"], "i32", count=4, display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("wheelSpin", "wheelSpin[4]", OFS_WHEELSPIN, "i32", count=4, display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("damagedParts", "damagedParts[4]", OFS_DAMAGEDPARTS, "i32", count=4, display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("littleCarDisp", "littleCarDisp", OFS["littleCarDisp"], "u8", display="hex", bit_map_key="littleCarDisp")),
                    FieldRowDPG(self, tbl, FieldSpec("PlankStuff_D6", "PlankStuff_D6", OFS["PlankStuff_D6"], "u8", display="hex", bit_map_key="PlankStuff_D6")),
                ]

            # Flags
            with dpg.tab(label="Flags"):
                tbl = dpg.add_table(header_row=True, row_background=True, resizable=True, borders_innerV=True, borders_innerH=True, scrollY=True, height=-1)
                for lab, w in [("Field", 200), ("Value", 250), ("Write", 70), ("All", 60), ("Pretty", 320)]:
                    dpg.add_table_column(label=lab, parent=tbl, init_width_or_weight=w)

                self.rows_by_tab["Flags"] = [
                    BitsRowDPG(self, tbl, "flags_23", OFS["flags_23"], "flags_23"),
                    BitsRowDPG(self, tbl, "field_3D", OFS["field_3D"], "field_3D"),
                    BitsRowDPG(self, tbl, "flags_7C", OFS["flags_7C"], "flags_7C"),
                    BitsRowDPG(self, tbl, "drvAids", OFS["drvAids"], "drvAids"),
                    BitsRowDPG(self, tbl, "usedDevices", OFS["usedDevices"], "usedDevices"),
                    BitsRowDPG(self, tbl, "flags_90", OFS["flags_90"], "flags_90"),
                    BitsRowDPG(self, tbl, "flags_91", OFS["flags_91"], "flags_91"),
                    BitsRowDPG(self, tbl, "flags_AD", OFS["flags_AD"], "flags_AD"),
                    BitsRowDPG(self, tbl, "ledInfo", OFS["ledInfo"], "ledInfo"),
                    BitsRowDPG(self, tbl, "display168", OFS["display168"], "display168"),
                    BitsRowDPG(self, tbl, "flags_169", OFS["flags_169"], "flags_169"),
                    BitsRowDPG(self, tbl, "flags_16A", OFS["flags_16A"], "flags_16A"),
                ]

            # Aero / Weight
            with dpg.tab(label="Aero / Weight"):
                tbl = dpg.add_table(header_row=True, row_background=True, resizable=True, borders_innerV=True, borders_innerH=True, scrollY=True, height=-1)
                for lab, w in [("Field", 200), ("Value", 250), ("Write", 70), ("All", 60), ("Pretty", 320)]:
                    dpg.add_table_column(label=lab, parent=tbl, init_width_or_weight=w)

                self.rows_by_tab["Aero / Weight"] = [
                    FieldRowDPG(self, tbl, FieldSpec("weight_ida", "weight (IDA @0x44)", OFS["weight_ida"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("legacy58", "legacy i16 @0x58", OFS["legacy_i16_at_58"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("downforceRelRW", "downforceRelRW", OFS["downforceRelRW"], "i32", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("downforceRelFW", "downforceRelFW", OFS["downforceRelFW"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("rearwingRel", "rearwingRel", OFS["rearwingRel"], "i32", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("fuelUnit", "fuelUnit", OFS["fuelUnit"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("gripFactor", "gripFactor", OFS["gripFactor"], "i16", display="dec")),
                    FieldRowDPG(self, tbl, FieldSpec("gearRatios", "gearRatios[6]", OFS["gearRatios"], "u8", count=6, display="dec")),
                ]

            # Custom Scripts
            with dpg.tab(label="Custom Scripts"):
                self._build_scripts_ui(parent=dpg.last_item())

            # Full Struct
            with dpg.tab(label="Full Struct") as tab_id:
                self.full_struct_tab_id = tab_id
                self.full_struct.build(parent=dpg.last_item())

    # ---------- main loop ----------
    def update(self):
        now = time.monotonic()

        # run queued UI tasks (from threads)
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass

        # autosave debounce
        if self._autosave_due is not None and now >= self._autosave_due:
            self._autosave_due = None
            self.save_config()

        # scripts tick (independent of auto refresh)
        try:
            self.scripts.update(now)
            self._sync_scripts_status_ui()
        except Exception:
            pass

        # auto refresh
        if self.auto_refresh and (not self._grid_scan_running) and self.pm:
            interval = max(0.05, float(self.refresh_ms) / 1000.0)
            if now >= self._next_refresh_due:
                self._next_refresh_due = now + interval
                self.refresh_once()

    def on_exit(self):
        try:
            self.stop_auto_refresh()
            self.scripts.sc_release()
            self.scripts.slip_off()
            self.save_config()
        except Exception:
            pass

    def run(self):
        dpg.create_context()

        # Dear PyGui default style is already dark-ish. You can tweak here if desired.

        self.build_ui()

        dpg.create_viewport(title="GP2Mem 0.3", width=1550, height=850, small_icon="assets/gp2mem.ico", large_icon="assets/gp2mem.ico",)
        dpg.setup_dearpygui()
        dpg.show_viewport()

        dpg.set_exit_callback(lambda: self.on_exit())

        while dpg.is_dearpygui_running():
            self.update()
            dpg.render_dearpygui_frame()

        dpg.destroy_context()


if __name__ == "__main__":
    GP2ViewerApp().run()
