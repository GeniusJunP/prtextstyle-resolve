"""
text_style.py -- dataclass model bridging the parser's assembled
`semantic` output (prtextstyle_resolve.parser.decode_payload) to the
Fusion Text+ emitter (prtextstyle_resolve.fusion_setting).

Phase 6 rewrite against the UI-ground-truth-verified OLD-dialect field
map (docs/SCHEMA.md). This REPLACES the previous versions
"LineSpacing/CharacterSpacing/Size from field_13/14/15" model entirely:
TRUTH.md reassigns TextStyle.field_13/14/15/16 to the PRIMARY SHADOW's
angle/distance/size/blur, which invalidates every one of previous versions's
"PROBABLE" text-metric readings of those same field ids. Size is fixed:
the user confirmed the entire 216-preset corpus is 100pt, and no field
in the verified map corresponds to point size, so `TextStyle.size_pt` is
always 100.0 here (not derived from any payload field).

Preset `name` is carried through for filesystem identification and as
the `StyledText` initial value ONLY -- never as a source for font/color/
effect attributes (hard rule #1 in the task brief: never use preset
names as a source of truth for style attributes).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .font_resolver import FontIndex, resolve_font

# The user confirmed the entire 2022 corpus is set to 100pt; no field in
# the verified OLD-dialect map corresponds to point size (field_15 is the
# PRIMARY SHADOW's size, not font size -- see OLD_SCHEMA_TRUTH.md). Size
# is therefore a fixed constant, never read from the payload.
FIXED_SIZE_PT = 100.0

# Text+ shading elements are indexed 1..8; element 1 is always the fill.
FILL_ELEMENT_INDEX = 1
FIRST_EFFECT_ELEMENT = 2
MAX_SHADING_ELEMENTS = 8
MAX_EFFECT_ELEMENTS = MAX_SHADING_ELEMENTS - FIRST_EFFECT_ELEMENT + 1  # 7


@dataclass(frozen=True)
class GradientStop:
    """One stop of a fill gradient. `position` is 0..1 along the gradient
    axis, taken from the stop's REAL stored location (GradientColorStop.
    field_1, omitted = 0.0) -- verified monotonic on corpus samples with 5 stops
    (0.0/0.338/0.649/0.809/1.0). `opacity` (0..1) comes from the matching
    GradientOpacityStop (field_0); the corpus is 100% opaque so this is
    effectively always 1.0. The per-stop `midpoint` (field_2) is decoded
    but not carried here -- Fusion's Gradient constructor has no per-stop
    midpoint control, so it is dropped (noted in build_text_style)."""

    position: float
    r: int
    g: int
    b: int
    opacity: float  # 0..1
    midpoint: float = 0.5

@dataclass(frozen=True)
class Fill:
    mode: str  # "solid" | "gradient"
    r: int = 255
    g: int = 255
    b: int = 255
    alpha: float = 1.0
    gradient_stops: List[GradientStop] = field(default_factory=list)
    gradient_angle_deg: Optional[float] = None


@dataclass(frozen=True)
class Stroke:
    """One stroke, mapped to a Text+ outline-type shading element.

    `position` is always "center": no inside/outside/center flag was
    found anywhere in the verified field map (attrs.field_17[] entries
    only carry color/enabled/width-increment), and TRUTH.md explicitly
    notes stroke position was never observed to vary across the
    calibration presets it checked. Treated as the structural default,
    not a per-preset inference.
    """

    element_index: int
    label: str
    r: int
    g: int
    b: int
    width_px: float
    position: str = "center"
    gradient_stops: List[GradientStop] = field(default_factory=list)
    gradient_angle_deg: Optional[float] = None


@dataclass(frozen=True)
class Shadow:
    """One shadow, mapped to a Text+ shadow-type shading element."""

    element_index: int
    label: str
    r: int
    g: int
    b: int
    opacity_pct: float
    angle_deg: float
    distance_px: float
    size_px: float
    blur_px: float
    gradient_stops: List[GradientStop] = field(default_factory=list)
    gradient_angle_deg: Optional[float] = None
    use_gradient: bool = False


@dataclass
class TextStyle:
    """A single Premiere text style preset, reduced to the fully-verified
    OLD-dialect field model and ready for Fusion Text+ emission."""

    index: int
    name: str
    font: Optional[str]
    fill: Optional[Fill]
    strokes: List[Stroke] = field(default_factory=list)
    shadows: List[Shadow] = field(default_factory=list)
    kerning: Optional[float] = None
    warnings: List[str] = field(default_factory=list)
    size_pt: float = FIXED_SIZE_PT
    dialect: str = "old"
    # Font/Style split (Phase 7 task 1). `font` remains the value emitted
    # as Text+ `Font` (resolved family name if found, else the raw
    # PostScript name unchanged -- see build_text_style). `font_style` is
    # the value emitted as Text+ `Style`, populated ONLY when the font
    # was resolved against a real installed font (never guessed).
    # `font_ps_name` is the original PostScript name from the source,
    # always preserved for traceability even when resolution succeeds
    # and `font` becomes a different (family) string.
    font_style: Optional[str] = None
    font_ps_name: Optional[str] = None
    font_resolved: bool = False

    def report_dict(self) -> Dict[str, Any]:
        """Serializable summary for report.json."""
        return {
            "index": self.index,
            "name": self.name,
            "dialect": self.dialect,
            "font": self.font,
            "font_style": self.font_style,
            "font_ps_name": self.font_ps_name,
            "font_resolved": self.font_resolved,
            "fill": (
                {
                    "mode": self.fill.mode,
                    "rgb": [self.fill.r, self.fill.g, self.fill.b] if self.fill.mode == "solid" else None,
                    "alpha": self.fill.alpha,
                    "gradient_stops": (
                        [
                            {"position": s.position, "rgb": [s.r, s.g, s.b], "opacity": s.opacity}
                            for s in self.fill.gradient_stops
                        ]
                        if self.fill.mode == "gradient"
                        else None
                    ),
                }
                if self.fill
                else None
            ),
            "strokes": [
                {
                    "element_index": s.element_index,
                    "label": s.label,
                    "rgb": [s.r, s.g, s.b],
                    "width_px": s.width_px,
                    "position": s.position,
                    "gradient_stops": [
                        {"position": g.position, "rgb": [g.r, g.g, g.b], "opacity": g.opacity}
                        for g in s.gradient_stops
                    ],
                    "gradient_angle_deg": s.gradient_angle_deg,
                }
                for s in self.strokes
            ],
            "shadows": [
                {
                    "element_index": sh.element_index,
                    "label": sh.label,
                    "rgb": [sh.r, sh.g, sh.b],
                    "opacity_pct": sh.opacity_pct,
                    "angle_deg": sh.angle_deg,
                    "distance_px": sh.distance_px,
                    "size_px": sh.size_px,
                    "blur_px": sh.blur_px,
                    "gradient_stops": [
                        {"position": g.position, "rgb": [g.r, g.g, g.b], "opacity": g.opacity}
                        for g in sh.gradient_stops
                    ],
                    "gradient_angle_deg": sh.gradient_angle_deg,
                }
                for sh in self.shadows
            ],
            "kerning": self.kerning,
            "size_pt": self.size_pt,
            "warnings": list(self.warnings),
        }


def _looks_like_ps_font_name(font: str) -> bool:
    """Loose structural sanity check, NOT used to alter the emitted value
    -- only to flag something a human should double check."""
    if not font or not font.strip():
        return False
    return True


def _opacity_stop_value(raw_opacity: Optional[float]) -> float:
    """Gradient opacity value -> 0..1. Absent = fully opaque (the whole
    corpus). If ever present, treat a >1 magnitude as a 0..100 percentage
    (the value's exact scale is uncalibrated -- see decode_gradient_opacity_stop)."""
    if raw_opacity is None:
        return 1.0
    return raw_opacity / 100.0 if raw_opacity > 1.0 else raw_opacity


def _resolve_gradient_stops(gradient: Dict[str, Any]) -> "tuple[List[GradientStop], Optional[float]]":
    """Shared color/opacity-stop resolution for fill, stroke, and shadow
    gradients -- same descriptor shape (decode_gradient) in all three
    sources (attrs.field_21, attrs.field_23, attrs.field_17[].field_4,
    TextStyle.field_37[].field_8)."""
    color_stops = gradient.get("color_stops") or []
    opacity_stops = gradient.get("opacity_stops") or []
    # Opacity ramp: (position, opacity 0..1). Separate from color stops
    # (Photoshop-style), so opacity at a color stop = nearest opacity stop.
    op_points = [
        (os_.get("position") if os_.get("position") is not None else 0.0, _opacity_stop_value(os_.get("opacity")))
        for os_ in opacity_stops
    ]

    def opacity_at(pos: float) -> float:
        if not op_points:
            return 1.0
        return min(op_points, key=lambda p: abs(p[0] - pos))[1]

    stops: List[GradientStop] = []
    for cs in color_stops:
        position = cs.get("position")
        position = position if position is not None else 0.0
        c = cs.get("color") or {"r": 255, "g": 255, "b": 255}
        stops.append(
            GradientStop(position=position, r=c["r"], g=c["g"], b=c["b"], opacity=opacity_at(position))
        )
    return stops, gradient.get("angle_deg")


def _resolve_gradient_fill(gradient: Dict[str, Any], warnings: List[str]) -> Fill:
    stops, angle = _resolve_gradient_stops(gradient)
    warnings.append(
        f"gradient fill: {len(stops)} stop(s) emitted with their real stored positions; "
        "per-stop midpoints are dropped (Fusion has no midpoint control); the attrs.field_21 "
        "gradient ANGLE is now routed to ShadingMappingAngle1 (degrees) -- verify zero-reference/"
        "sign in Resolve (may need +90 or a flip)"
    )
    return Fill(mode="gradient", gradient_stops=stops, gradient_angle_deg=angle)


# Confirmed default angle for ADDITIONAL shadows (TextStyle.field_37[].field_3
# omitted <-> ~135deg). NOT confirmed for the PRIMARY shadow -- reused here by
# analogy only, with an explicit warning.
_DEFAULT_SHADOW_ANGLE_DEG = 135.0


def _build_shadow(raw_shadow: Dict[str, Any], element_index: int, label: str, warnings: List[str]) -> Shadow:
    c = raw_shadow.get("color") or {"r": 0, "g": 0, "b": 0}
    opacity_pct = raw_shadow.get("opacity_pct")
    if opacity_pct is None:
        warnings.append(f"{label}: opacity_pct missing while enabled; defaulting to 100")
        opacity_pct = 100.0
    angle_deg = raw_shadow.get("angle_deg")
    if angle_deg is None:
        is_primary = raw_shadow.get("source") == "primary"
        warnings.append(
            f"{label}: angle_deg missing while enabled; defaulting to {_DEFAULT_SHADOW_ANGLE_DEG}"
            + (" (UNCONFIRMED for the primary shadow -- reused the additional-shadow default by analogy)" if is_primary else " (confirmed additional-shadow default)")
        )
        angle_deg = _DEFAULT_SHADOW_ANGLE_DEG
    distance = raw_shadow.get("distance")
    if distance is None:
        warnings.append(f"{label}: distance missing while enabled; defaulting to 0")
        distance = 0.0
    size = raw_shadow.get("size")
    if size is None:
        warnings.append(f"{label}: size missing while enabled; defaulting to 0")
        size = 0.0
    blur = raw_shadow.get("blur")
    if blur is None:
        warnings.append(f"{label}: blur missing while enabled; defaulting to 0")
        blur = 0.0
    grad_stops: List[GradientStop] = []
    grad_angle: Optional[float] = None
    gradient = raw_shadow.get("gradient")
    use_gradient = raw_shadow.get("use_gradient", False)
    if gradient and gradient.get("color_stops"):
        grad_stops, grad_angle = _resolve_gradient_stops(gradient)
        warnings.append(f"{label}: gradient ({len(grad_stops)} stop(s)) routed to the shading element")
    return Shadow(
        element_index=element_index,
        label=label,
        r=c["r"],
        g=c["g"],
        b=c["b"],
        opacity_pct=opacity_pct,
        angle_deg=angle_deg,
        distance_px=distance,
        size_px=size,
        blur_px=blur,
        gradient_stops=grad_stops,
        gradient_angle_deg=grad_angle,
        use_gradient=use_gradient,
    )


def build_text_style(
    index: int,
    name: str,
    semantic: Dict[str, Any],
    font_index: Optional[FontIndex] = None,
    font_mapping: Optional[Dict[str, str]] = None,
) -> TextStyle:
    """Build a TextStyle from parser.decode_payload's `semantic` dict (or
    the `.semantic` attribute of a `DecodedPreset` from `iter_presets`).

    `name` is used only for `StyledText`'s initial value and filesystem
    identification, per hard rule #1 -- it is never inspected to decide
    style attributes.

    `font_index` is the installed-font lookup table from
    `font_resolver.system_font_index()` (postscript_name -> (family,
    style)); pass an explicit dict in tests to avoid depending on the
    machine's actual installed fonts. Omitting it queries the real
    system once per call to `font_resolver.resolve_font` (which itself
    caches the underlying `fc-list` invocation process-wide) -- callers
    converting a whole corpus should still query
    `font_resolver.system_font_index()` once and pass it through
    explicitly to avoid re-deriving it per preset (see cli.py).
    """
    warnings: List[str] = []
    dialect = semantic.get("dialect", "old")
    if dialect != "old":
        warnings.append(
            f"payload dialect detected as {dialect!r}, not 'old' -- this decoder's field map is "
            "calibrated against the OLD dialect only (see docs/SCHEMA.md); "
            "values below may be WRONG for this preset"
        )

    # ---- Font / Style split (Phase 7 task 1; confirmed source field:
    # TextStyle.font_names[0]) ----
    # The source stores a single PostScript name (e.g.
    # "Toppan-BunkyuMidashiGoStdN-EB"). Real hand-authored Text+ titles
    # split this into Font="<family>" + Style="<style>" (see
    # docs/SCHEMA.md point 3) -- Text+ may not
    # resolve a raw PostScript name the way it resolves a family+style
    # pair. We resolve this by an EXACT match of the PostScript name
    # against the machine's real installed-font database (fc-list); if
    # it matches, Font/Style become the real family/style. If it does
    # NOT match (font not installed on this machine), we do NOT guess a
    # split from the PS name's own punctuation/suffixes -- that would be
    # exactly the kind of silent fallpath hard rule #2 forbids. Instead
    # the raw PS name is emitted unchanged as Font (matching the
    # pre-Phase-7 behavior), Style is left unset, and a warning is
    # recorded so this is visible in report.json/CLI output.
    ps_name = semantic.get("font")
    font: Optional[str] = ps_name
    font_style: Optional[str] = None
    font_resolved = False
    if ps_name is None:
        warnings.append("no font name resolved from TextStyle.font_names; Font left unset")
    else:
        if not _looks_like_ps_font_name(ps_name):
            warnings.append(f"unusual font string {ps_name!r}; emitted as-is without translation")
        resolved = resolve_font(ps_name, font_index, font_mapping)
        if resolved is not None:
            font, font_style = resolved
            font_resolved = True
        else:
            warnings.append(
                f"font {ps_name!r} not found by an exact PostScript-name match against the "
                "installed font database (fc-list); this font is likely not installed/active on "
                "this machine -- emitting the raw PostScript name as Font unchanged and leaving "
                "Style unset (no split was guessed). Install the font (or run on a machine where "
                "it's active) and re-convert to get a real Font/Style split."
            )

    # ---- Fill (confirmed: attrs.field_2 solid, or attrs.field_21 gradient
    # when field_21 actually has color stops -- attrs.field_22 ["gradient
    # flag"] is unreliable and no longer consulted) ----
    fill_semantic = semantic.get("fill") or {"mode": "solid", "color": {"r": 255, "g": 255, "b": 255}}
    if fill_semantic["mode"] == "gradient":
        fill = _resolve_gradient_fill(fill_semantic["gradient"], warnings)
    else:
        c = fill_semantic["color"]
        fill = Fill(mode="solid", r=c["r"], g=c["g"], b=c["b"], alpha=1.0)
    warnings.append(
        "fill alpha fixed at 1.0 (no confirmed per-fill opacity field exists in the OLD-dialect "
        "field map; TextStyle.field_12 is the SHADOW opacity, not fill opacity)"
    )

    # ---- Kerning (confirmed: attrs.field_7) -- NOW routed to Text+
    # `CharacterSpacing` (see fusion_setting.render_setting). A style-level
    # kerning value is uniform, so it is equivalent to Text+'s uniform
    # CharacterSpacing tracking; converted as 1 + kerning/1000 (Premiere's
    # 1/1000-em unit). Kept as an informational note (hard rule #2) because
    # Premiere technically distinguishes per-pair "kerning" from uniform
    # "tracking" -- for a reusable style this value is applied uniformly. ----
    kerning = semantic.get("kerning")
    if kerning is not None:
        warnings.append(
            f"kerning={kerning:g} (attrs.field_7) routed to CharacterSpacing="
            f"{1.0 + kerning / 1000.0:g} (1 + kerning/1000); applied as uniform tracking"
        )

    # ---- Strokes + shadows -> Text+ shading elements 2..8 ----
    # Element-index assignment scheme (documented, deterministic; REVISED in
    # Z-order scheme):
    #   1. primary stroke (if enabled)
    #   2. additional strokes, in source order (if enabled)
    #   3. primary shadow (if enabled)
    #   4. additional shadows, in source order (if enabled)
    # i.e. ALL strokes before ALL shadows. Phase 6 originally interleaved
    # (stroke, shadow, extra-stroke, extra-shadow), which for a 2-stroke+
    # shadow preset would have put an extra stroke BEHIND the primary
    # shadow -- wrong compositing (shadows must be behind every stroke, not
    # just the primary one). Fusion Text+ z-order is index-driven (element 1
    # = frontmost, higher index = further back; verified
    # docs/SCHEMA.md), so "strokes then shadows"
    # is enforced purely by assignment order here. Within strokes, source
    # order already yields thin-in-front/thick-behind (additional-stroke
    # widths are cumulative from the primary, see attrs.field_17 in
    # OLD_SCHEMA_TRUTH.md) -- that verified nesting is preserved unchanged.
    # The 216-preset corpus tops out at 7 enabled effects (see
    # docs/SCHEMA.md), i.e. exactly fits the 7-slot budget
    # (elements 2..8) with no fill+effects preset ever overflowing -- but
    # the overflow guard below is still enforced defensively.
    raw_strokes = semantic.get("strokes") or []
    raw_shadows = semantic.get("shadows") or []

    primary_strokes = [s for s in raw_strokes if s.get("source") == "primary"]
    extra_strokes = [s for s in raw_strokes if s.get("source") == "extra"]
    primary_shadows = [s for s in raw_shadows if s.get("source") == "primary"]
    extra_shadows = [s for s in raw_shadows if s.get("source") == "extra"]

    candidates: List[Dict[str, Any]] = []
    for s in primary_strokes:
        candidates.append({"kind": "stroke", "data": s, "label": "Stroke (primary)"})
    for i, s in enumerate(extra_strokes, start=1):
        candidates.append({"kind": "stroke", "data": s, "label": f"Stroke (extra {i})"})
    for sh in primary_shadows:
        candidates.append({"kind": "shadow", "data": sh, "label": "Shadow (primary)"})
    for i, sh in enumerate(extra_shadows, start=1):
        candidates.append({"kind": "shadow", "data": sh, "label": f"Shadow (extra {i})"})

    enabled_candidates = [c for c in candidates if c["data"].get("enabled")]
    disabled_count = len(candidates) - len(enabled_candidates)
    if disabled_count:
        warnings.append(f"{disabled_count} stroke/shadow entrie(s) present but disabled in source; omitted")

    strokes: List[Stroke] = []
    shadows: List[Shadow] = []
    element_index = FIRST_EFFECT_ELEMENT
    dropped: List[str] = []
    for c in enabled_candidates:
        if element_index > MAX_SHADING_ELEMENTS:
            dropped.append(c["label"])
            continue
        if c["kind"] == "stroke":
            s = c["data"]
            width = s.get("width_px")
            if width is None:
                warnings.append(f"{c['label']}: width_px missing while enabled; defaulting to 0")
                width = 0.0
            col = s.get("color") or {"r": 255, "g": 255, "b": 255}
            grad_stops: List[GradientStop] = []
            grad_angle: Optional[float] = None
            gradient = s.get("gradient")
            if gradient and gradient.get("color_stops"):
                grad_stops, grad_angle = _resolve_gradient_stops(gradient)
                warnings.append(f"{c['label']}: gradient ({len(grad_stops)} stop(s)) routed to the shading element")
            strokes.append(
                Stroke(
                    element_index=element_index,
                    label=c["label"],
                    r=col["r"],
                    g=col["g"],
                    b=col["b"],
                    width_px=width,
                    gradient_stops=grad_stops,
                    gradient_angle_deg=grad_angle,
                )
            )
        else:
            shadows.append(_build_shadow(c["data"], element_index, c["label"], warnings))
        element_index += 1

    if dropped:
        warnings.append(
            f"{len(dropped)} effect(s) exceeded the {MAX_EFFECT_ELEMENTS}-slot shading-element budget "
            f"(elements 2..{MAX_SHADING_ELEMENTS}) and were DROPPED: {', '.join(dropped)}"
        )

    return TextStyle(
        index=index,
        name=name,
        font=font,
        fill=fill,
        strokes=strokes,
        shadows=shadows,
        kerning=kerning,
        warnings=warnings,
        size_pt=FIXED_SIZE_PT,
        dialect=dialect,
        font_style=font_style,
        font_ps_name=ps_name,
        font_resolved=font_resolved,
    )
