"""
fusion_setting.py -- emit a DaVinci Resolve / Fusion `.setting` file for a
single Text+ preset.

The output is a BARE TextPlus tool named "Template", matching the structure
of BMD's own built-in title templates (e.g. Headline.setting extracted from
Templates.drfx). BMD title templates use either bare TextPlus (static) or
GroupOperator (animated) -- never MacroOperator. The Edit-page Inspector
auto-exposes all TextPlus controls for a bare TextPlus title, so no
InstanceInput publishing is needed. (Phase 8's MacroOperator + InstanceInput
approach showed controls on the Fusion page but NOT on the Edit page, which
is the intended authoring surface.)

Shading element rules (unchanged from Phase 8):
- ElementShape{N} declared explicitly on every stroke/shadow element from
  index 2 upward (fixes the "white tile" bug).
- SelectElement=1 / Select=1 always present.
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Union

from .text_style import GradientStop, Shadow, Stroke, TextStyle

PathLike = Union[str, "os.PathLike[str]"]

COMP_WIDTH = 1920
COMP_HEIGHT = 1080

# Shadow Softness normalization. Design rule: every emitted numeric value
# must trace to a decoded prtext field via a principled unit conversion --
# no invented aesthetic fudge factors. blur (field_16) is decoded in px;
# it normalizes to Text+'s em-relative Softness the SAME way stroke
# Thickness does (`px / size_pt`). Phase 8 briefly added a `/2.5, cap 0.6`
# rescale on an *unverified* bloom hunch -- that was an invented constant
# with no prtext basis, now removed (the "white tile" bug was the missing
# ElementShape, not softness magnitude; the user's own production macro
# ships Softness=1 with no issue).
# size/spread (field_15) is decoded but deliberately NOT emitted as
# Text+'s per-element `Size{N}` control: user testing in DaVinci Resolve
# confirmed Size{N} has no visible effect on ElementShape=0 (Text Fill,
# displaced) shadow elements (Size3 0 -> 0.5 produced zero visible change).
# blur->Softness{N} is the only shadow-shape mapping emitted.

# ElementShape values (docs/SCHEMA.md, cross-checked
# against docs/SCHEMA.md and the user's own production
# macro): 0 = Text Fill (also used, displaced, for a shadow), 1 = Outline,
# 2 = Border box. This corpus never emits a Border element (no background
# data in the decoded model -- see cli.BACKGROUND_NOTE), so only the first
# two are used here.
ELEMENT_SHAPE_STROKE = "1"
ELEMENT_SHAPE_SHADOW = "0"

# Key name for the TextPlus tool in the .setting file. Matches the BMD
# convention seen in built-in title templates (Headline.setting etc.).
TOOL_KEY = "Template"

# Filesystem/Lua-identifier-unsafe characters. Japanese and other non-ASCII
# text is explicitly allowed (see task brief: "allow Japanese, disallow
# slashes/quotes").
_UNSAFE_CHARS = re.compile(r'[\/\\:*?"<>|\x00-\x1f]')


def safe_key(name: str) -> str:
    """Filesystem- and Fusion-key-safe form of a preset name.

    Allows Japanese (and any other non-ASCII) text. Disallows path
    separators, quotes, and other characters that are unsafe in a
    filename or a bare (unquoted) Fusion table key. Never inspects the
    *meaning* of the name -- purely a character-level sanitizer.
    """
    cleaned = _UNSAFE_CHARS.sub("_", name).strip()
    cleaned = cleaned.strip(".")  # avoid leading/trailing dots on some filesystems
    return cleaned or "preset"


def lua_string(s: str) -> str:
    """Escape a Python string for embedding in a double-quoted Fusion/Lua
    string literal."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n").replace("\t", "\\t")
    return out


def fmt_float(x: float) -> str:
    """Format a float the way hand-written .setting files do: no
    unnecessary trailing zeros, but always at least one digit."""
    r = round(float(x), 6)
    if r == int(r):
        # Still emit as float-looking (e.g. "1") -- Fusion accepts bare
        # integers for float inputs, matching observed sample files
        # (Width = Input { Value = 1920, }).
        return str(int(r))
    s = f"{r:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"


class _Writer:
    """Small indentation-aware line writer for the Lua-table-like syntax."""

    def __init__(self) -> None:
        self._lines: List[str] = []
        self._indent = 0

    def line(self, text: str) -> None:
        self._lines.append(("    " * self._indent) + text)

    def open(self, text: str) -> None:
        self.line(text)
        self._indent += 1

    def close(self, text: str = "}") -> None:
        self._indent -= 1
        self.line(text)

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def _input_scalar(w: _Writer, key: str, value: str) -> None:
    w.line(f"{key} = Input {{ Value = {value}, }},")


def _input_string(w: _Writer, key: str, value: str) -> None:
    w.line(f'{key} = Input {{ Value = "{lua_string(value)}", }},')


def _input_point(w: _Writer, key: str, x: float, y: float) -> None:
    """`Offset{N}`-style 2D point, using the plain `{ x, y }` tuple form
    that BMD's own title templates use (e.g. Headline.setting's
    `Offset4 = Input { Value = { 0.018, -0.02 }, }`)."""
    w.line(f"{key} = Input {{ Value = {{ {fmt_float(x)}, {fmt_float(y)} }}, }},")


def _input_number_wrapped(w: _Writer, key: str, value: float) -> None:
    """`OffsetZ{N}` / `Softness{N}`-style scalar wrapped in an explicit
    `Number {}` constructor. Matches the user's own production macro
    (`OffsetZ3 = Input { Value = Number { Value = -0.5 }, },`,
    `Softness3 = Input { Value = Number { Value = 1 }, },`) verbatim."""
    w.line(f"{key} = Input {{ Value = Number {{ Value = {fmt_float(value)} }}, }},")


def _resolved_size(style: TextStyle) -> float:
    """Size -> Text+ `Size` input, per the documented formula
    (docs/SCHEMA.md §1.5): `Size = font_size_pt /
    comp_width_px`. `style.size_pt` is a fixed 100.0 for the entire
    corpus (see text_style.FIXED_SIZE_PT)."""
    return style.size_pt / COMP_WIDTH


def _shadow_offset_xy(style: TextStyle, shadow: Shadow) -> Tuple[float, float]:
    """Premiere {angle,distance} -> Fusion Offset{X,Y} in 0..1 canvas
    coordinates. Convention (docs/SCHEMA.md, unchanged from
    Phase 6): angle is measured in screen space (0deg = +X/right,
    increasing CLOCKWISE, matching Premiere's Shadow Direction dial).
    Premiere 90 degrees is DOWN. Resolve Y is UP, so we negate Y.
    We use a fallback of 100.0 if size_pt is 0 to avoid division by zero."""
    theta = math.radians(shadow.angle_deg)
    scale = style.size_pt if style.size_pt > 0 else 100.0
    # Premiere angle is clockwise from right (0=right, 90=down).
    # User confirmed: angle≈162° in 03ゴピンク白黒ピンク → shadow goes to the RIGHT (+X in Resolve).
    # cos(162°) is negative, so negate dx to get the correct direction.
    dx = -shadow.distance_px * math.cos(theta) / scale
    dy = -shadow.distance_px * math.sin(theta) / scale
    return dx, dy


def _shadow_softness_xy(style: TextStyle, shadow: Shadow) -> float:
    """Premiere shadow BLUR (field_16) -> Text+ `SoftnessX{N}`/`SoftnessY{N}`.
    Text+ has a two-level blur model: `Softness{N}` is the master enable
    (0=sharp, 1=soft edges); `SoftnessX{N}`/`SoftnessY{N}` are the actual
    gaussian blur radii (confirmed: bignoodle_sub.setting uses Softness3=1
    + SoftnessX3=10/SoftnessY3=10; subscribe.setting exposes SoftnessX/Y
    with Default=0). Without SoftnessX/Y > 0, Softness=1 only softens the
    element edge, not a gaussian blur.
    # Normalized em-relative (`blur_px / size_pt`), same family as stroke
    # Thickness and Size.
    # Adobe's shadow blur uses a proprietary wide-tail falloff rather than
    # a pure Gaussian curve. Visually matching this in Resolve's standard
    # Gaussian Softness requires a ~4.0x multiplier to the radius.
    """
    if not style.size_pt:
        return 0.0
    return (shadow.blur_px * 4.0) / style.size_pt


# ---------------------------------------------------------------------
# TextPlus Inputs -- the actual render-affecting values.
# ---------------------------------------------------------------------


def _emit_gradient(w: _Writer, n: int, stops: List[GradientStop], angle_deg: Optional[float]) -> None:
    """Emit Type{N}=1 (linear gradient) + ShadingGradient{N}=Gradient{...}
    + optional ShadingMappingAngle{N}, per docs/SCHEMA.md
    §1.4. Shared by fill (element 1), stroke, and shadow elements -- same
    descriptor shape, same Text+ controls, only the element index differs."""
    _input_scalar(w, f"Type{n}", "2")
    # Gradient DIRECTION: Premiere gradient_angle_deg -> Text+ element-N
    # `ShadingMappingAngle{N}` ("Mapping Angle", rotates the gradient on Z,
    # in degrees). This was the "gradient angle unrouted" hole; the control
    # is confirmed (subscribe.setting). Emitted degrees->degrees; the exact
    # zero-reference/sign of Premiere vs Fusion is the one residual item to
    # confirm in a render (may need a +90 or sign flip) -- flagged, but the
    # direction is now data-driven instead of always-default.
    # Default to 90.0 (Top to Bottom) if angle is missing in flatbuffers
    angle = angle_deg if angle_deg is not None else 90.0
    # Premiere rotates clockwise, Fusion rotates counter-clockwise.
    # The base formula is (-90 - angle).
    fusion_angle = (-90.0 - angle) % 360.0
    _input_scalar(w, f"ShadingMappingAngle{n}", fmt_float(fusion_angle))
    w.open(f"ShadingGradient{n} = Input {{")
    w.open("Value = Gradient {")
    w.open("Colors = {")
    
    interpolated_stops = []
    for i in range(len(stops)):
        interpolated_stops.append(stops[i])
        if i < len(stops) - 1:
            s1 = stops[i]
            s2 = stops[i+1]
            if s1.midpoint != 0.5:
                # Approximate the midpoint offset by inserting a 50% blended stop
                p_mid = s1.position + s1.midpoint * (s2.position - s1.position)
                c_mid_r = (s1.r + s2.r) / 2.0
                c_mid_g = (s1.g + s2.g) / 2.0
                c_mid_b = (s1.b + s2.b) / 2.0
                c_mid_a = (s1.opacity + s2.opacity) / 2.0
                interpolated_stops.append(GradientStop(
                    r=int(c_mid_r), g=int(c_mid_g), b=int(c_mid_b),
                    opacity=c_mid_a, position=p_mid, midpoint=0.5
                ))

    for stop in interpolated_stops:
        pos_key = fmt_float(stop.position)
        w.line(
            f"[{pos_key}] = {{ {fmt_float(stop.r / 255.0)}, {fmt_float(stop.g / 255.0)}, "
            f"{fmt_float(stop.b / 255.0)}, {fmt_float(stop.opacity)} }},"
        )
    w.close("},")
    w.close("},")
    w.close("},")


def _emit_fill(w: _Writer, fill: Any) -> None:
    if fill is None:
        return
    if fill.mode == "solid":
        _input_scalar(w, "Red1", fmt_float(fill.r / 255.0))
        _input_scalar(w, "Green1", fmt_float(fill.g / 255.0))
        _input_scalar(w, "Blue1", fmt_float(fill.b / 255.0))
        _input_scalar(w, "Alpha1", fmt_float(fill.alpha))
        return
    # Gradient fill. Stop positions/opacities are best-effort (see
    # text_style.GradientStop docstring) -- flagged in the preset's
    # warnings already.
    _emit_gradient(w, 1, fill.gradient_stops, fill.gradient_angle_deg)
    # Fallback flat color (element still needs Red1/Green1/Blue1/Alpha1 as
    # a baseline in case Type1/gradient isn't honoured by the target Fusion
    # version) -- use the first stop's color.
    if fill.gradient_stops:
        first = fill.gradient_stops[0]
        _input_scalar(w, "Red1", fmt_float(first.r / 255.0))
        _input_scalar(w, "Green1", fmt_float(first.g / 255.0))
        _input_scalar(w, "Blue1", fmt_float(first.b / 255.0))
        _input_scalar(w, "Alpha1", fmt_float(first.opacity))


def _emit_stroke(w: _Writer, style: TextStyle, stroke: Stroke) -> None:
    n = stroke.element_index
    _input_scalar(w, f"Enabled{n}", "1")
    _input_string(w, f"Name{n}", stroke.label)
    # ElementShape=1 (Outline) declared explicitly on every stroke element,
    # not just 4+ -- see module docstring point 1 (the "landmine" case of
    # a 2nd stroke landing on element 3, Text+'s shadow-convention slot).
    _input_scalar(w, f"ElementShape{n}", ELEMENT_SHAPE_STROKE)
    # SoftnessX/Y must be explicitly 0 for strokes (hard edge).
    # Resolve's default for these axes is 5.0, so omitting them causes
    # visible blur even when no softness is intended.
    # (Softness{n} is the master enable and is not emitted -- SoftnessX/Y=0
    # already fully suppress the blur regardless of the master toggle.)
    _input_scalar(w, f"SoftnessX{n}", "0")
    _input_scalar(w, f"SoftnessY{n}", "0")
    # Strokes must not have an offset. Resolve defaults element 3 (the standard
    # shadow slot) to {0.05, -0.05}, which displaces strokes landing on this index.
    _input_point(w, f"Offset{n}", 0.0, 0.0)

    # Thickness: normalized as width_px / (2 * size_pt) (a fraction of the
    # glyph em-size), NOT width_px / comp_width. Premiere's stroke width_px
    # specifies the TOTAL stroke width (both sides of the glyph edge
    # combined), whereas Resolve's centered outline (OutsideOnly=0) applies
    # Thickness as a PER-SIDE width. Dividing by 2 converts the Premiere
    # total width into the per-side value Resolve expects.
    thickness = stroke.width_px / (2.0 * style.size_pt) if style.size_pt else 0.0
    _input_scalar(w, f"Thickness{n}", fmt_float(thickness))
    if stroke.gradient_stops:
        _emit_gradient(w, n, stroke.gradient_stops, stroke.gradient_angle_deg)
        # Fallback flat color, same rationale as _emit_fill: use the first
        # gradient stop's color as a baseline in case Type{N}/gradient isn't
        # honoured by the target Fusion version.
        first = stroke.gradient_stops[0]
        _input_scalar(w, f"Red{n}", fmt_float(first.r / 255.0))
        _input_scalar(w, f"Green{n}", fmt_float(first.g / 255.0))
        _input_scalar(w, f"Blue{n}", fmt_float(first.b / 255.0))
    else:
        _input_scalar(w, f"Red{n}", fmt_float(stroke.r / 255.0))
        _input_scalar(w, f"Green{n}", fmt_float(stroke.g / 255.0))
        _input_scalar(w, f"Blue{n}", fmt_float(stroke.b / 255.0))


def _emit_shadow(w: _Writer, style: TextStyle, shadow: Shadow, cumulative_size_px: float) -> None:
    n = shadow.element_index
    _input_scalar(w, f"Enabled{n}", "1")
    _input_string(w, f"Name{n}", shadow.label)
    
    if shadow.size_px > 0:
        _input_scalar(w, f"ElementShape{n}", ELEMENT_SHAPE_STROKE)
        thickness = (cumulative_size_px + shadow.size_px) / (2.0 * style.size_pt) if style.size_pt else 0.0
        _input_scalar(w, f"Thickness{n}", fmt_float(thickness))
    else:
        _input_scalar(w, f"ElementShape{n}", ELEMENT_SHAPE_SHADOW)
        
    offset_x, offset_y = _shadow_offset_xy(style, shadow)
    _input_point(w, f"Offset{n}", offset_x, offset_y)
    # blur -> Softness{N} (master enable) + SoftnessX{N}/SoftnessY{N} (blur radii).
    # size/spread (field_15) is NOT emitted: user testing confirmed Size{N}
    # has no visual effect on ElementShape=0 (Text Fill displaced) shadow
    # elements in DaVinci Resolve (changing Size3 from 0 to 0.5 produced no
    # visible change).
    softness_xy = _shadow_softness_xy(style, shadow)
    if softness_xy > 0:
        _input_scalar(w, f"Softness{n}", "1")
        _input_scalar(w, f"SoftnessX{n}", fmt_float(softness_xy))
        _input_scalar(w, f"SoftnessY{n}", fmt_float(softness_xy))
    else:
        _input_scalar(w, f"Softness{n}", "1")
        _input_scalar(w, f"SoftnessX{n}", "0")
        _input_scalar(w, f"SoftnessY{n}", "0")

    # If the shadow has gradient data AND the gradient toggle is true, emit Type{N}=2 (gradient)
    if shadow.use_gradient and shadow.gradient_stops:
        _emit_gradient(w, n, shadow.gradient_stops, shadow.gradient_angle_deg)
        first = shadow.gradient_stops[0]
        _input_scalar(w, f"Red{n}", fmt_float(first.r / 255.0))
        _input_scalar(w, f"Green{n}", fmt_float(first.g / 255.0))
        _input_scalar(w, f"Blue{n}", fmt_float(first.b / 255.0))
    else:
        _input_scalar(w, f"Type{n}", "0")
        _input_scalar(w, f"Red{n}", fmt_float(shadow.r / 255.0))
        _input_scalar(w, f"Green{n}", fmt_float(shadow.g / 255.0))
        _input_scalar(w, f"Blue{n}", fmt_float(shadow.b / 255.0))
        
    if shadow.opacity_pct < 100.0:
        opacity_val = shadow.opacity_pct / 100.0
        _input_scalar(w, f"Opacity{n}", fmt_float(opacity_val))


def render_setting(style: TextStyle, key: str) -> str:
    """Render the full `.setting` file contents for one TextStyle.

    `key` is used only for the on-disk filename stem; the internal tool
    name is always "Template" (BMD convention for title templates).
    The output is a bare TextPlus tool -- no MacroOperator wrapper, no
    InstanceInputs, no ActiveTool -- matching BMD's own built-in title
    templates (e.g. Headline.setting). The Edit-page Inspector auto-
    exposes all TextPlus controls for this form.
    """
    w = _Writer()
    w.open("{")
    w.open("Tools = ordered() {")
    w.open(f"{TOOL_KEY} = TextPlus {{")
    w.line("CtrlWZoom = false,")
    w.line("NameSet = true,")
    w.open("Inputs = {")

    _input_scalar(w, "Width", str(COMP_WIDTH))
    _input_scalar(w, "Height", str(COMP_HEIGHT))
    _input_scalar(w, "UseFrameFormatSettings", "1")
    w.line("Center = Input { Value = { 0.5, 0.5 }, },")
    _input_scalar(w, "SelectElement", "1")
    _input_scalar(w, "Select", "1")
    _input_string(w, "StyledText", style.name)
    if style.font:
        _input_string(w, "Font", style.font)
    if style.font_style:
        _input_string(w, "Style", style.font_style)
    _input_scalar(w, "Size", fmt_float(_resolved_size(style)))
    if style.kerning is not None:
        spacing = 1.0 + style.kerning / 1000.0
        _input_scalar(w, "CharacterSpacing", fmt_float(spacing))

    _emit_fill(w, style.fill)

    max_stroke_width_px = 0.0

    for stroke in style.strokes:
        _emit_stroke(w, style, stroke)
        if stroke.width_px > max_stroke_width_px:
            max_stroke_width_px = stroke.width_px

    for shadow in style.shadows:
        _emit_shadow(w, style, shadow, max_stroke_width_px)

    w.close("},")
    w.line("ViewInfo = OperatorInfo { Pos = { 0, 0 } },")
    w.close("},")

    w.close("},")  # Tools
    w.close("}")

    return w.render()


def write_setting_file(style: TextStyle, out_path: PathLike, key: str) -> None:
    """Render and write `style` to `out_path` (a str or Path)."""
    content = render_setting(style, key)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
