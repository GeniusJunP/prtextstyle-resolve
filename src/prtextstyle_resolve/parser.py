"""
parser.py -- structural FlatBuffers decoder for Premiere Pro .prtextstyle
files, OLD dialect (the 2022 corpus dialect; see
docs/SCHEMA.md).

the field map used here is the UI-ground-truth-verified
map in docs/SCHEMA.md, which OVERTURNS the previous versions/3
semantic assignment this module used to carry (The parser read field_10 as
fill color and field_12 as fill opacity -- those are SHADOW fields; the
real fill lives at attrs.field_2). See docs/SCHEMA.md for
the full list of corrections and their evidence.

The low-level FlatBuffers primitives (vtable walking, string/table/vector
classification, generic_walk fallback) are UNCHANGED from previous versions --
those are format mechanics, not schema semantics, and remain correct.
Only the schema-driven named readers (decode_rgbcolor, decode_text_style,
decode_preview_attributes, decode_payload, and everything that used to be
named decode_effect_item) were rewritten against the verified field map.

IMPORTANT: this decoder makes zero use of the preset <Name> for anything
other than carrying it through to the output for human traceability. All
type/field decisions come from the FlatBuffers vtable structure alone --
see docs/SCHEMA.md for the evidence behind every field
assignment below, and docs/SCHEMA.md for the
raw structural dump this rewrite was checked against.

Dialect detection: OLD dialect payloads carry `TextStyle.field_23`
(version_tag) present; NEW dialect payloads (test_style_19.prtextstyle,
authored-from-scratch documents) do not. This decoder is schema-tuned for
OLD dialect; `decode_payload` surfaces `dialect` = "old" | "new" |
"unknown" so callers (cli.py) can warn loudly instead of silently
misinterpreting NEW-dialect input through the OLD field map.
"""
from __future__ import annotations

import base64
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------
# FlatBuffers primitives (unchanged from previous versions -- format mechanics)
# ---------------------------------------------------------------------


def u8(b: bytes, o: int) -> int:
    return b[o]


def u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def i16(b: bytes, o: int) -> int:
    return struct.unpack_from("<h", b, o)[0]


def u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def i32(b: bytes, o: int) -> int:
    return struct.unpack_from("<i", b, o)[0]


def f32(b: bytes, o: int) -> float:
    return struct.unpack_from("<f", b, o)[0]


def u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def f64(b: bytes, o: int) -> float:
    return struct.unpack_from("<d", b, o)[0]


VTableInfo = Tuple[int, int, int]  # (vtable_pos, vtable_size, object_size)
FieldSlot = Tuple[int, int, int]  # (field_pos_absolute, width_bytes, offset_in_object)


def vtable_of(blob: bytes, table_pos: int) -> Optional[VTableInfo]:
    """Return (vtable_pos, vtable_size, object_size) or None if invalid."""
    if table_pos < 0 or table_pos + 4 > len(blob):
        return None
    soffset = i32(blob, table_pos)
    vt_pos = table_pos - soffset
    if vt_pos < 0 or vt_pos + 4 > len(blob):
        return None
    vt_size = u16(blob, vt_pos)
    obj_size = u16(blob, vt_pos + 2)
    if vt_size < 4 or vt_size > 512 or obj_size < 4 or obj_size > 100000:
        return None
    if vt_pos + vt_size > len(blob):
        return None
    return vt_pos, vt_size, obj_size


def field_slots(blob: bytes, table_pos: int) -> Tuple[Dict[int, FieldSlot], Optional[VTableInfo]]:
    """
    field_id -> (field_pos_absolute, width_bytes, offset_in_object)
    Width = gap to the next field in *memory order* (sorted by
    offset-in-object), bounded by object_size for the last field. This
    matches FlatBuffers' actual memory layout (which is NOT vtable
    field-id order).
    """
    vt = vtable_of(blob, table_pos)
    if vt is None:
        return {}, None
    vt_pos, vt_size, obj_size = vt
    raw: List[Tuple[int, int]] = []
    for i in range(4, vt_size, 2):
        off = u16(blob, vt_pos + i)
        if off == 0:
            continue
        field_id = (i - 4) // 2
        raw.append((field_id, off))
    raw_sorted = sorted(raw, key=lambda x: x[1])
    slots: Dict[int, FieldSlot] = {}
    for idx, (fid, off) in enumerate(raw_sorted):
        nxt = raw_sorted[idx + 1][1] if idx + 1 < len(raw_sorted) else obj_size
        width = nxt - off
        slots[fid] = (table_pos + off, width, off)
    return slots, (vt_pos, vt_size, obj_size)


def _is_printable_text(s: str) -> bool:
    """Reject decoded 'strings' that are just control-byte garbage that
    happens to satisfy a naive UTF-8 length+NUL check (e.g. a vtable
    soffset or vector length misread as string content)."""
    if not s:
        return True
    for ch in s:
        cp = ord(ch)
        if cp < 0x20 and ch not in ("\t", "\n", "\r"):
            return False
        if cp == 0x7F:
            return False
    return True


def looks_like_string(blob: bytes, pos: int) -> bool:
    try:
        if pos < 0 or pos + 4 > len(blob):
            return False
        length = u32(blob, pos)
        if length > 4096 or pos + 4 + length + 1 > len(blob):
            return False
        if blob[pos + 4 + length] != 0:
            return False
        s = blob[pos + 4 : pos + 4 + length].decode("utf-8")
        return _is_printable_text(s)
    except Exception:
        return False


def read_string(blob: bytes, pos: int) -> str:
    length = u32(blob, pos)
    return blob[pos + 4 : pos + 4 + length].decode("utf-8")


def looks_like_table(blob: bytes, pos: int) -> bool:
    return vtable_of(blob, pos) is not None


def looks_like_vector_of_offsets(blob: bytes, pos: int, allow_empty: bool = True) -> bool:
    try:
        if pos < 0 or pos + 4 > len(blob):
            return False
        length = u32(blob, pos)
        if length == 0:
            return allow_empty
        if length > 4096 or pos + 4 + length * 4 > len(blob):
            return False
        for i in range(length):
            elem_pos = pos + 4 + i * 4
            elem_rel = i32(blob, elem_pos)
            target = elem_pos + elem_rel
            if not (looks_like_string(blob, target) or looks_like_table(blob, target)):
                return False
        return True
    except Exception:
        return False


OffsetClass = Tuple[str, int]  # ("string" | "table" | "vector", target_pos)


def classify_offset_field(blob: bytes, field_pos: int) -> Optional[OffsetClass]:
    """
    Generic (schema-agnostic) fallback prober: given a >=4-byte field,
    try to interpret it as a uoffset_t and classify the target.
    Returns (kind, target) or None. kind in {'string','table','vector'}.
    Table is checked before string (a valid vtable is a much stronger
    signal than "these bytes happen to decode as UTF-8"), and vector
    elements are ALL checked # Because multiple fields decode as true booleans, strict matching avoids
    # single-element false positives. See docs/SCHEMA.md.

    NOTE: this generic prober is used only as a fallback for sub-trees
    without a fixed schema rule (e.g. EffectExtra, unknown attrs fields).
    Every field the schema-definition field map names has a dedicated
    schema-driven reader below and does NOT go through this prober --
    see schema documentation for the documented bug where the
    generic classifier can mis-ID small integers as tables.
    """
    try:
        rel = i32(blob, field_pos)
    except Exception:
        return None
    if rel <= 0:
        return None
    target = field_pos + rel
    if target < 0 or target >= len(blob):
        return None
    if looks_like_table(blob, target):
        return ("table", target)
    if looks_like_string(blob, target):
        return ("string", target)
    if looks_like_vector_of_offsets(blob, target):
        return ("vector", target)
    return None


def generic_walk(
    blob: bytes,
    table_pos: int,
    depth: int = 0,
    max_depth: int = 12,
    visited: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """Schema-agnostic fallback: walk any table, classifying each field by
    per-instance structural probing. Used only for sub-trees this decoder
    doesn't have a fixed schema rule for (e.g. EffectExtra)."""
    if visited is None:
        visited = frozenset()
    if depth > max_depth or table_pos in visited:
        return {"_error": "depth_or_cycle"}
    visited = visited | {table_pos}
    slots, _vt = field_slots(blob, table_pos)
    out: Dict[str, Any] = {}
    for fid, (field_pos, width, _off) in sorted(slots.items()):
        key = f"field_{fid}"
        cls = classify_offset_field(blob, field_pos) if width >= 4 else None
        if cls is not None:
            kind, target = cls
            if kind == "string":
                out[key] = {"kind": "string", "value": read_string(blob, target)}
            elif kind == "table":
                out[key] = {"kind": "table", "value": generic_walk(blob, target, depth + 1, max_depth, visited)}
            elif kind == "vector":
                length = u32(blob, target)
                elems: List[Dict[str, Any]] = []
                for i in range(length):
                    elem_pos = target + 4 + i * 4
                    rel = i32(blob, elem_pos)
                    etarget = elem_pos + rel
                    if looks_like_string(blob, etarget):
                        elems.append({"kind": "string", "value": read_string(blob, etarget)})
                    elif looks_like_table(blob, etarget):
                        elems.append({"kind": "table", "value": generic_walk(blob, etarget, depth + 1, max_depth, visited)})
                    else:
                        elems.append({"kind": "unknown"})
                out[key] = {"kind": "vector", "elements": elems}
        else:
            out[key] = _scalar_dict(blob, field_pos, width)
    return out


def _scalar_dict(blob: bytes, field_pos: int, width: int) -> Dict[str, Any]:
    d: Dict[str, Any] = {"kind": "scalar", "width": width}
    try:
        if width == 1:
            d["u8"] = blob[field_pos]
        elif width == 2:
            d["u16"] = u16(blob, field_pos)
        elif width >= 4:
            d["i32"] = i32(blob, field_pos)
            d["u32"] = u32(blob, field_pos)
            d["f32"] = round(f32(blob, field_pos), 6)
            if width >= 8:
                d["f64"] = round(f64(blob, field_pos), 6)
    except Exception:
        d["raw_hex"] = blob[field_pos : field_pos + width].hex()
    return d


# ---------------------------------------------------------------------
# Schema-driven field access helpers
# ---------------------------------------------------------------------


def get_field(blob: bytes, table_pos: Optional[int], fid: int) -> Optional[Tuple[int, int]]:
    """Return (field_pos, width) or None if the field is absent."""
    if table_pos is None:
        return None
    slots, _vt = field_slots(blob, table_pos)
    if fid not in slots:
        return None
    field_pos, width, _off = slots[fid]
    return field_pos, width


def get_offset_target(
    blob: bytes, table_pos: Optional[int], fid: int, expect_kind: Optional[str] = None
) -> Optional[int]:
    got = get_field(blob, table_pos, fid)
    if got is None:
        return None
    field_pos, width = got
    if width < 4:
        return None
    cls = classify_offset_field(blob, field_pos)
    if cls is None:
        return None
    if expect_kind and cls[0] != expect_kind:
        return None
    return cls[1]


def get_scalar_f32(blob: bytes, table_pos: Optional[int], fid: int) -> Optional[float]:
    got = get_field(blob, table_pos, fid)
    if got is None:
        return None
    field_pos, width = got
    if width < 4:
        return None
    return round(f32(blob, field_pos), 6)


def get_scalar_i32(blob: bytes, table_pos: Optional[int], fid: int) -> Optional[int]:
    got = get_field(blob, table_pos, fid)
    if got is None:
        return None
    field_pos, width = got
    if width < 4:
        return None
    return i32(blob, field_pos)


def get_scalar_bool(blob: bytes, table_pos: Optional[int], fid: int) -> Optional[bool]:
    got = get_field(blob, table_pos, fid)
    if got is None:
        return None
    field_pos, width = got
    if width < 1:
        return None
    return bool(blob[field_pos])


# ---------------------------------------------------------------------
# RGBColor -- verified: per-channel default is 255, and a fully-absent
# color table means WHITE, never None/black.
# See verified schema docs "RGBColor per-channel default = 255".
# ---------------------------------------------------------------------

RGBColorDict = Dict[str, int]  # always fully populated, r/g/b in 0..255


def decode_rgbcolor(blob: bytes, table_pos: Optional[int]) -> RGBColorDict:
    """Decode an RGBColor table. NEVER returns None and NEVER leaves a
    channel unset: a missing table, or a missing channel within a present
    table, both default to 255 (white channel) per the verified field
    map."""
    out: RGBColorDict = {"r": 255, "g": 255, "b": 255}
    if table_pos is None:
        return out
    slots, _vt = field_slots(blob, table_pos)
    for fid, name in ((0, "r"), (1, "g"), (2, "b")):
        if fid in slots:
            fp, w, _off = slots[fid]
            if w >= 1:
                out[name] = blob[fp]
    return out


def decode_vector_elements(blob: bytes, table_pos: Optional[int], fid: int) -> List[int]:
    target = get_offset_target(blob, table_pos, fid, "vector")
    if target is None:
        return []
    length = u32(blob, target)
    out: List[int] = []
    for i in range(length):
        elem_pos = target + 4 + i * 4
        rel = i32(blob, elem_pos)
        out.append(elem_pos + rel)
    return out


def decode_string_vector(blob: bytes, table_pos: Optional[int], fid: int) -> List[str]:
    out: List[str] = []
    for etarget in decode_vector_elements(blob, table_pos, fid):
        if looks_like_string(blob, etarget):
            out.append(read_string(blob, etarget))
    return out


# ---------------------------------------------------------------------
# Gradient descriptor: {angle, color_stops[], opacity_stops[]}.
# Shape confirmed identical for fill gradient and per-shadow gradient.
# ---------------------------------------------------------------------


def decode_gradient_color_stop(blob: bytes, table_pos: int) -> Dict[str, Any]:
    """A gradient COLOR stop.
    field_0 = color (RGBColor). field_1 = POSITION along the gradient axis
    (0..1; omitted = 0.0). field_2 = per-stop MIDPOINT
    (0..1, default 0.5)."""
    color_pos = get_offset_target(blob, table_pos, 0, "table")
    return {
        "color": decode_rgbcolor(blob, color_pos),
        "position": get_scalar_f32(blob, table_pos, 1),
        "midpoint": get_scalar_f32(blob, table_pos, 2),
    }


def decode_gradient_opacity_stop(blob: bytes, table_pos: int) -> Dict[str, Any]:
    """A gradient OPACITY stop.
    field_0 = opacity VALUE (omitted = fully opaque). field_1 = position (0..1, omitted
    = 0.0). field_2 = midpoint (0..1, default 0.5)."""
    return {
        "opacity": get_scalar_f32(blob, table_pos, 0),
        "position": get_scalar_f32(blob, table_pos, 1),
        "midpoint": get_scalar_f32(blob, table_pos, 2),
    }


def decode_gradient(blob: bytes, table_pos: Optional[int]) -> Optional[Dict[str, Any]]:
    if table_pos is None:
        return None
    color_stops = [decode_gradient_color_stop(blob, p) for p in decode_vector_elements(blob, table_pos, 1)]
    opacity_stops = [decode_gradient_opacity_stop(blob, p) for p in decode_vector_elements(blob, table_pos, 2)]
    return {
        "angle_deg": get_scalar_f32(blob, table_pos, 0),
        "color_stops": color_stops,
        "opacity_stops": opacity_stops,
    }


# ---------------------------------------------------------------------
# ShadowItem (TextStyle.field_37[] entries -- ADDITIONAL shadows) and
# StrokeItem (attrs.field_17[] entries -- ADDITIONAL strokes).
# ---------------------------------------------------------------------


def decode_shadow_item(blob: bytes, table_pos: int) -> Dict[str, Any]:
    """One TextStyle.field_37[] entry (an ADDITIONAL shadow).
    field_0=color # field_11=enabled, field_12=opacity%, field_13=angle(Premiere degree offset: 0=Up/omitted default ~135) field_4=distance
    field_5=size field_6=blur field_8=per-shadow gradient descriptor."""
    color_pos = get_offset_target(blob, table_pos, 0, "table")
    gradient_pos = get_offset_target(blob, table_pos, 8, "table")
    use_gradient = get_scalar_bool(blob, table_pos, 7) or False
    return {
        "color": decode_rgbcolor(blob, color_pos),
        "enabled": get_scalar_bool(blob, table_pos, 1),
        "opacity_pct": get_scalar_f32(blob, table_pos, 2),
        "angle_deg": get_scalar_f32(blob, table_pos, 3),
        "distance": get_scalar_f32(blob, table_pos, 4),
        "size": get_scalar_f32(blob, table_pos, 5),
        "blur": get_scalar_f32(blob, table_pos, 6),
        "gradient": decode_gradient(blob, gradient_pos),
        "use_gradient": use_gradient,
    }


def decode_stroke_item(blob: bytes, table_pos: int) -> Dict[str, Any]:
    """One attrs.field_17[] entry (an ADDITIONAL stroke).
    field_0=color field_1=enabled field_2=width **stored as an INCREMENT**
    over the running cumulative width (verified via corpus samples). field_4 is a per-extra-stroke gradient descriptor
    (empty in most samples, but some have a full 5-stop gradient there --
    same shape as the fill/shadow gradient, decoded with decode_gradient)."""
    color_pos = get_offset_target(blob, table_pos, 0, "table")
    gradient_pos = get_offset_target(blob, table_pos, 4, "table")
    return {
        "color": decode_rgbcolor(blob, color_pos),
        "enabled": get_scalar_bool(blob, table_pos, 1),
        "width_increment": get_scalar_f32(blob, table_pos, 2),
        "gradient": decode_gradient(blob, gradient_pos),
    }


# ---------------------------------------------------------------------
# PreviewAttributes == the "attrs" table == RunEntry[0].field_1.
# Verified (OLD_SCHEMA_TRUTH.md) to carry REAL style data in the OLD
# dialect, not just preview-swatch info as SCHEMA_DIALECTS.md originally
# guessed: fill color, primary stroke, kerning, additional strokes, and
# the gradient-fill-active flag all live here.
# ---------------------------------------------------------------------


def decode_preview_attributes(blob: bytes, table_pos: Optional[int]) -> Optional[Dict[str, Any]]:
    if table_pos is None:
        return None
    fill_color_target = get_offset_target(blob, table_pos, 2, "table")
    stroke_color_target = get_offset_target(blob, table_pos, 4, "table")
    extra_strokes = [decode_stroke_item(blob, p) for p in decode_vector_elements(blob, table_pos, 17)]
    gradient_flag_field = get_field(blob, table_pos, 22)
    fill_gradient_pos = get_offset_target(blob, table_pos, 21, "table")
    stroke_gradient_pos = get_offset_target(blob, table_pos, 23, "table")
    return {
        # field_2: FILL color (SOLID case). Confirmed on the real 216-preset
        # corpus: presets with a solid fill have this present with the
        # correct color, and an empty/absent attrs.field_21. Omitted means
        # white.
        "fill_color": decode_rgbcolor(blob, fill_color_target),
        "fill_color_present": fill_color_target is not None,
        # field_4/5/6: PRIMARY stroke. color omitted=white,
        # enabled omitted=off, width is the identity (unscaled) px value.
        "stroke_color": decode_rgbcolor(blob, stroke_color_target),
        "stroke_enabled": get_scalar_bool(blob, table_pos, 5),
        "stroke_width": get_scalar_f32(blob, table_pos, 6),
        # field_23: PRIMARY stroke gradient descriptor (69/216 presets have
        # color stops here). Same shape as the fill/shadow gradient.
        "stroke_gradient": decode_gradient(blob, stroke_gradient_pos),
        # field_7: kerning (confirmed via corpus samples: -50 <-> UI Kerning -50).
        # Fusion Text+ has no per-pair kerning input -- see text_style.py.
        "kerning": get_scalar_f32(blob, table_pos, 7),
        # field_17: ADDITIONAL strokes vector.
        "extra_strokes": extra_strokes,
        # field_21: the REAL fill gradient descriptor (GRADIENT case).
        # Confirmed on the real 216-preset corpus: presets with a gradient
        # fill have their real color stops here (field_2/fill_color may be
        # absent or stale/wrong in that case). TextStyle.field_40 is NOT
        # the fill gradient -- see decode_text_style's gradient field,
        # which is shadow-related, not fill-related.
        "fill_gradient": decode_gradient(blob, fill_gradient_pos),
        # field_22: gradient flag. Kept for traceability only -- CONFIRMED
        # UNRELIABLE: some presets have a real gradient fill in field_21
        # while field_22 is False/absent. Fill-mode resolution must check
        # attrs.field_21 for actual color stops instead, see _resolve_fill.
        "gradient_flag_present": gradient_flag_field is not None,
    }


def decode_run_entry(blob: bytes, table_pos: Optional[int]) -> Optional[Dict[str, Any]]:
    if table_pos is None:
        return None
    text_target = get_offset_target(blob, table_pos, 0, "string")
    attrs_target = get_offset_target(blob, table_pos, 1, "table")
    return {
        "preview_text": read_string(blob, text_target) if text_target is not None else None,
        "attributes": decode_preview_attributes(blob, attrs_target),
    }


def decode_text_style(blob: bytes, table_pos: int) -> Dict[str, Any]:
    """Decode TextStyle per the verified OLD-dialect field map
    (docs/SCHEMA.md). Size is NOT decoded here: the user
    confirmed the entire 2022 corpus is 100pt, and no field in the
    verified map corresponds to point size (field_15 here is SHADOW
    size, not font size -- see "Primary shadow" below). Leading/tracking/
    baseline (old field_13/14/16 top-level readings from previous versions) are
    NOT re-emitted either: TRUTH.md reassigns field_13/14 to shadow
    angle/distance, which invalidates the whole previous versions leading/tracking
    story -- those top-level field ids mean something else entirely.
    """
    run_entries = [decode_run_entry(blob, p) for p in decode_vector_elements(blob, table_pos, 0)]
    fonts = decode_string_vector(blob, table_pos, 1)

    # Primary shadow: TextStyle.field_10..16.
    shadow_color_target = get_offset_target(blob, table_pos, 10, "table")
    shadow_primary = {
        "color": decode_rgbcolor(blob, shadow_color_target),
        "enabled": get_scalar_bool(blob, table_pos, 11),
        "opacity_pct": get_scalar_f32(blob, table_pos, 12),
        "angle_deg": get_scalar_f32(blob, table_pos, 13),
        "distance": get_scalar_f32(blob, table_pos, 14),
        "size": get_scalar_f32(blob, table_pos, 15),
        "blur": get_scalar_f32(blob, table_pos, 16),
    }
    
    # Primary shadow gradient override (field_39 = flag, field_40 = gradient)
    if get_scalar_bool(blob, table_pos, 39):
        grad_pos = get_offset_target(blob, table_pos, 40, "table")
        if grad_pos:
            shadow_primary["gradient"] = decode_gradient(blob, grad_pos)
            shadow_primary["use_gradient"] = True

    # Additional shadows: TextStyle.field_37[] vector.
    extra_shadows = [decode_shadow_item(blob, p) for p in decode_vector_elements(blob, table_pos, 37)]

    # TextStyle.field_40: NOT the fill gradient (an earlier build assumed
    # it was, gated by attrs.field_22). Confirmed against the real
    # 216-preset corpus: the real fill gradient lives at attrs.field_21
    # (PreviewAttributes), and field_40 carries something else entirely
    # (shadow-related data). Kept here as a raw/structural field only --
    # decode_payload's fill resolution no longer reads it. See
    # docs/SCHEMA.md "Fill gradient".
    gradient_pos = get_offset_target(blob, table_pos, 40, "table")
    gradient = decode_gradient(blob, gradient_pos)

    version_tag = get_scalar_i32(blob, table_pos, 23)

    return {
        "preview_runs": run_entries,
        "font_names": fonts,
        "shadow_primary": shadow_primary,
        "extra_shadows": extra_shadows,
        "gradient": gradient,
        "version_tag": version_tag,
    }


# Historical analysis describes the magic as raw bytes "44 33 22 11"; as a
# little-endian u32 those bytes decode to 0x11223344 (verified against
# actual payloads -- the byte-sequence notation and the LE-int value are
# easy to conflate, this constant is the correct LE-int comparison).
ADOBE_MAGIC = 0x11223344


def _resolve_strokes(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Assemble the full, ORDERED, ABSOLUTE-width stroke list from the
    primary stroke (attrs.stroke_*) plus the additional-strokes vector
    (attrs.extra_strokes), applying the verified cumulative-sum rule:
    absolute width of stroke k = primary width + sum(increments[0..k-1]).
    Verified on corpus samples; see
    docs/SCHEMA.md "Additional strokes"."""
    strokes: List[Dict[str, Any]] = []
    primary_width = attrs.get("stroke_width")
    running_width = primary_width if primary_width is not None else 0.0

    # field_5 (stroke_enabled) is absent for 20 presets that nonetheless
    # have a real primary-stroke gradient in field_23 (and non-zero
    # field_6 width) -- an absent enabled flag must not mean "no stroke"
    # when there are real gradient color stops to render.
    stroke_gradient = attrs.get("stroke_gradient")
    has_gradient = bool(stroke_gradient and stroke_gradient.get("color_stops"))
    primary_enabled = bool(attrs.get("stroke_enabled")) or has_gradient

    strokes.append(
        {
            "source": "primary",
            "color": attrs.get("stroke_color", {"r": 255, "g": 255, "b": 255}),
            "enabled": primary_enabled,
            "width_px": primary_width,
            "gradient": stroke_gradient,
        }
    )

    for extra in attrs.get("extra_strokes") or []:
        increment = extra.get("width_increment")
        if increment is not None:
            running_width = (running_width or 0.0) + increment
        strokes.append(
            {
                "source": "extra",
                "color": extra.get("color", {"r": 255, "g": 255, "b": 255}),
                "enabled": bool(extra.get("enabled")),
                "width_px": running_width,
                "width_increment": increment,
                "gradient": extra.get("gradient"),
            }
        )
    return strokes


def _resolve_shadows(shadow_primary: Dict[str, Any], extra_shadows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Assemble the full, ORDERED shadow list: primary (TextStyle.field_10-16)
    first, then TextStyle.field_37[] additional shadows in order."""
    shadows: List[Dict[str, Any]] = [
        {
            "source": "primary",
            "color": shadow_primary.get("color", {"r": 0, "g": 0, "b": 0}),
            "enabled": bool(shadow_primary.get("enabled")),
            "opacity_pct": shadow_primary.get("opacity_pct"),
            "angle_deg": shadow_primary.get("angle_deg"),
            "distance": shadow_primary.get("distance"),
            "size": shadow_primary.get("size"),
            "blur": shadow_primary.get("blur"),
            # gradient descriptor field -- always None for the primary.
            "gradient": shadow_primary.get("gradient"),
            "use_gradient": shadow_primary.get("use_gradient", False),
        }
    ]
    for extra in extra_shadows:
        shadows.append(
            {
                "source": "extra",
                "color": extra.get("color", {"r": 0, "g": 0, "b": 0}),
                "enabled": bool(extra.get("enabled")),
                "opacity_pct": extra.get("opacity_pct"),
                "angle_deg": extra.get("angle_deg"),
                "distance": extra.get("distance"),
                "size": extra.get("size"),
                "blur": extra.get("blur"),
                "gradient": extra.get("gradient"),
                "use_gradient": extra.get("use_gradient", False),
            }
        )
    return shadows


def _resolve_fill(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve fill mode from the confirmed real source: attrs.field_21
    (PreviewAttributes), NOT TextStyle.field_40 (which is shadow-related,
    not fill-related). Detection is based on whether field_21 actually has
    color stops -- attrs.field_22 (the "gradient flag") is UNRELIABLE:
    some corpus presets have real gradient fills in field_21 with
    field_22=False. See docs/SCHEMA.md "Fill gradient"."""
    fill_gradient = attrs.get("fill_gradient")
    if fill_gradient and fill_gradient.get("color_stops"):
        return {"mode": "gradient", "gradient": fill_gradient}
    return {
        "mode": "solid",
        "color": attrs.get("fill_color", {"r": 255, "g": 255, "b": 255}),
    }


def decode_payload(blob: bytes) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Decode one Adobe-wrapped FlatBuffers blob.
    Returns (raw_dict, semantic_dict).

    `semantic` is the assembled, ready-to-consume model (fill mode+color,
    ordered strokes with absolute widths, ordered shadows, kerning, font,
    dialect) that text_style.build_text_style consumes -- all business
    logic (cumulative stroke widths, gradient-vs-solid resolution) lives
    here, not scattered across text_style.py/fusion_setting.py.
    """
    if len(blob) < 20:
        raise ValueError(f"payload too short ({len(blob)} bytes)")
    size_prefix = u32(blob, 0)
    magic = u32(blob, 8)
    if magic != ADOBE_MAGIC:
        raise ValueError(f"bad Adobe magic: {magic:#x}")
    if size_prefix != len(blob) - 12:
        raise ValueError(f"size prefix mismatch: {size_prefix} != {len(blob) - 12}")

    fb_start = 12
    root_rel = u32(blob, fb_start)
    root_table_pos = fb_start + root_rel

    style_pos = get_offset_target(blob, root_table_pos, 0, "table")
    if style_pos is None:
        raise ValueError("root.field0 did not resolve to a table")

    raw = decode_text_style(blob, style_pos)

    # dialect: OLD dialect (the 2022 corpus this converter targets)
    # carries version_tag (TextStyle.field_23); NEW dialect (freshly
    # authored documents, e.g. test_style_19.prtextstyle) does not. See
    # docs/SCHEMA.md.
    dialect = "old" if raw.get("version_tag") is not None else "new"

    run0 = raw["preview_runs"][0] if raw["preview_runs"] else None
    attrs = (run0 or {}).get("attributes") or {}

    font = raw["font_names"][0] if raw["font_names"] else None
    fill = _resolve_fill(attrs)
    strokes = _resolve_strokes(attrs)
    shadows = _resolve_shadows(raw["shadow_primary"], raw["extra_shadows"])
    kerning = attrs.get("kerning")

    semantic = {
        "dialect": dialect,
        "font": font,
        "fill": fill,
        "strokes": strokes,
        "shadows": shadows,
        "kerning": kerning,
    }
    return raw, semantic


# ---------------------------------------------------------------------
# .prtextstyle XML extraction (standalone reimplementation; no external
# project imports beyond the standard library)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class PresetPayload:
    """One preset extracted from the .prtextstyle XML, prior to decoding.

    `name` is carried only for filesystem/traceability purposes -- see
    module docstring. It must never influence how `blob` is decoded.
    """

    index: int
    name: str
    blob: bytes


def extract_presets(xml_path: str) -> List[PresetPayload]:
    """Return list of PresetPayload in document order."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    arb_params: Dict[str, ET.Element] = {}
    vfc_map: Dict[str, ET.Element] = {}
    for elem in root:
        oid = elem.get("ObjectID") or elem.get("ObjectUID")
        if elem.tag == "ArbVideoComponentParam" and oid:
            arb_params[oid] = elem
        elif elem.tag == "VideoFilterComponent" and oid:
            vfc_map[oid] = elem

    presets: List[PresetPayload] = []
    style_items = root.findall("StyleProjectItem")
    for idx, item in enumerate(style_items):
        name_el = item.find(".//Name")
        name = name_el.text if name_el is not None and name_el.text else f"preset_{idx}"

        comp_ref = item.find("Component")
        if comp_ref is None:
            continue
        ref_id = comp_ref.get("ObjectRef")
        vfc = vfc_map.get(ref_id) if ref_id else None
        if vfc is None:
            continue

        param0 = vfc.find('.//Param[@Index="0"]')
        if param0 is None:
            continue
        arb_ref = param0.get("ObjectRef")
        arb = arb_params.get(arb_ref) if arb_ref else None
        if arb is None:
            continue

        kf = arb.find("StartKeyframeValue")
        if kf is None or not kf.text:
            continue
        b64 = re.sub(r"\s+", "", kf.text)
        try:
            blob = base64.b64decode(b64)
        except Exception:
            continue

        presets.append(PresetPayload(index=idx, name=name, blob=blob))
    return presets


@dataclass(frozen=True)
class DecodedPreset:
    """A preset payload plus its decoded raw/semantic trees."""

    index: int
    name: str
    raw: Optional[Dict[str, Any]]
    semantic: Optional[Dict[str, Any]]
    parse_error: Optional[str]


def iter_presets(xml_path: str) -> Iterator[DecodedPreset]:
    """Extract and decode every preset in a .prtextstyle file, in order.

    Never raises on an individual preset's decode failure -- the failure
    is captured in `parse_error` so callers can report a per-preset status
    (matching decode_prtextstyle.py's CLI behaviour: 216/216 expected to
    parse cleanly for textpreset2022_12.prtextstyle).
    """
    for p in extract_presets(xml_path):
        try:
            raw, semantic = decode_payload(p.blob)
            yield DecodedPreset(index=p.index, name=p.name, raw=raw, semantic=semantic, parse_error=None)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see decode_prtextstyle.py CLI
            yield DecodedPreset(index=p.index, name=p.name, raw=None, semantic=None, parse_error=str(exc))
