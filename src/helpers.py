import struct
import re

from typing import Optional, Any
from const import *

def ordinal(n: Optional[int]) -> str:
    if n is None:
        return "-"
    if 10 <= (n % 100) <= 20:
        return f"{n}th"
    return f"{n}{ {1:'st', 2:'nd', 3:'rd'}.get(n % 10, 'th') }"


def fmt_bits_u8(v: Optional[int], mapping: Optional[Dict[int, str]] = None) -> str:
    if v is None:
        return "-"
    bits = f"{v & 0xFF:08b}"
    hx = f"0x{v & 0xFF:02X}"
    if not mapping:
        return f"{hx} {bits}"
    names = [name for bit, name in mapping.items() if v & (1 << bit)]
    return f"{hx} {bits} | " + (", ".join(names) if names else "-")


def fmt_time32(raw: Optional[int]) -> str:
    if raw is None:
        return "-"
    raw = int(raw)
    active = bool(raw & 0x10000000)
    t = raw & 0x0FFFFFFF
    mins, rem = divmod(t, 60000)
    secs, ms = divmod(rem, 1000)
    prefix = "A " if active else ""
    return f"{prefix}{mins}:{secs:02d}.{ms:03d} (0x{raw & 0xFFFFFFFF:08X})"


def parse_offset_hex(s: str) -> int:
    """
    Bare digits -> HEX
    Supports -0x.. and negative bare digits (treated as hex too).
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("empty")
    sign = 1
    if s.startswith("-"):
        sign = -1
        s = s[1:].strip()

    low = s.lower()
    if low.startswith(("0x", "0b", "0o")):
        return sign * int(s, 0)

    return sign * int(s, 16)


def parse_value_int(s: str) -> int:
    """
    Bare digits -> DEC
    Accepts 0x.., 0b.., and also 8-bit binary strings like 00010010.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("empty")

    low = s.lower()
    if low.startswith(("+0x", "-0x", "0x", "+0b", "-0b", "0b", "+0o", "-0o", "0o")):
        return int(s, 0)

    if len(s) == 8 and set(s) <= {"0", "1"}:
        return int(s, 2)

    if any(c in "abcdefABCDEF" for c in s):
        return int(s, 16)

    return int(s, 10)


def parse_value_for_display(s: str, display: str) -> int:
    s = (s or "").strip()
    if not s:
        raise ValueError("empty")
    if display == "hex":
        return parse_offset_hex(s)
    if display == "bin":
        low = s.lower()
        if low.startswith("0b"):
            return int(s, 2)
        if set(s) <= {"0", "1"}:
            return int(s, 2)
        return parse_value_int(s)
    return parse_value_int(s)


def clamp_value(typ: str, v: Any):
    if typ == "f32":
        return float(v)
    _ch, _sz, mn, mx = TYPE_INFO[typ]
    v = int(v)
    if mn is not None:
        v = max(mn, v)
    if mx is not None:
        v = min(mx, v)
    return v


def format_scalar_for_entry(v: Any, typ: str, display: str) -> str:
    if v is None:
        return ""
    if typ == "f32":
        return f"{float(v):.6g}"
    bits = {"u8": 8, "i8": 8, "u16": 16, "i16": 16, "u32": 32, "i32": 32}.get(typ, 32)
    mask = (1 << bits) - 1
    vv = int(v)

    if display == "hex":
        width = bits // 4
        return f"0x{(vv & mask):0{width}X}"
    if display == "bin":
        return f"{(vv & mask):0{bits}b}"
    return str(vv)


def unpack_typed(buf: bytes, offset: int, typ: str, count: int = 1):
    ch, _sz, _mn, _mx = TYPE_INFO[typ]
    fmt = f"<{count}{ch}"
    need = struct.calcsize(fmt)
    data = buf[offset: offset + need]
    vals = struct.unpack(fmt, data)
    return vals[0] if count == 1 else vals


def pack_typed(typ: str, values):
    ch, _sz, _mn, _mx = TYPE_INFO[typ]
    if isinstance(values, (list, tuple)):
        fmt = f"<{len(values)}{ch}"
        return struct.pack(fmt, *values)
    fmt = f"<{ch}"
    return struct.pack(fmt, values)


def racepos_to_place(rp: int) -> Optional[int]:
    rp = int(rp) & 0xFF
    if rp >= 0xFE:
        return None
    return (rp // 2) + 1


def enum_display_list(enum_pairs: List[Tuple[int, str]]) -> List[str]:
    return [f"{v}: {name}" for v, name in enum_pairs]


def enum_value_from_display(s: str) -> int:
    # expecting "N: label"
    s = (s or "").strip()
    m = re.match(r"^\s*(-?\d+)\s*:", s)
    if not m:
        # fallback try parse int
        return parse_value_int(s)
    return int(m.group(1))