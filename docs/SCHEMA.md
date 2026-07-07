# Premiere Pro `.prtextstyle` Schema to Text+ Mapping

This document details the parsing and conversion rules implemented in `prtextstyle-resolve` for translating Premiere Pro `.prtextstyle` files into DaVinci Resolve Text+ (`.setting`) presets.

## Supported Versions & Dialect Detection

`.prtextstyle` files are inherently XML documents, but modern Premiere Pro versions store the actual style properties as a Base64-encoded **FlatBuffers** binary blob within the XML. 

This parser strictly targets the **FlatBuffers payload** embedded within these files.

- **Supported (Premiere Pro 2022〜2023): "Old Dialect"**
  - Contains `version_tag` (typically `"5.0.0.0"`) in `TextStyle.field_23`.
  - The properties and field mappings in this tool are designed for this specific schema format.
- **UNSUPPORTED (Premiere Pro 24.x+): "New Dialect"**
  - Lacks `version_tag` in `TextStyle.field_23`. Found in freshly authored documents in modern Premiere versions.
  - **WARNING**: While the parser will not crash when reading these files (due to FlatBuffers' default-field fallback behavior), the internal field meanings have drastically changed. Mapped properties will be entirely incorrect, and thus this format is strictly **unsupported**.

## Core Schema Rules

### 1. Fonts & Sizing
- **Font Names**: Decoded from `TextStyle.field_1[]` (vector of strings).
- **Size**: Point size is assumed to be a fixed `100.0`.
  - *Conversion*: Mapped to Text+ `Size` using the formula `size_pt / 1920` (Comp Width).
- **Leading / Tracking / Baseline Shift**: Not emitted.

### 2. Fill Properties
- **Fill Resolution**:
  - The real fill properties live in the PreviewAttributes (`attrs`) table.
  - The fill is treated as **gradient** if `attrs.field_21` is present and contains color stops.
  - The gradient flag at `attrs.field_22` is ignored because it is unreliable.
  - If no valid gradient exists, it falls back to a **solid** fill.
- **Solid Fill**:
  - Color is sourced from `attrs.field_2` (RGBColor table).
  - Defaults to White (`r: 255, g: 255, b: 255`) if absent.
  - Fill alpha is fixed to `1.0` in the output.

### 3. Stroke (Outline) Properties
- **Primary Stroke**:
  - Color: `attrs.field_4`
  - Enabled flag: `attrs.field_5` (Forced true if a valid stroke gradient is present).
  - Width: `attrs.field_6` (Identity, unscaled px).
  - Gradient: `attrs.field_23`.
- **Additional Strokes**:
  - Found in `attrs.field_17[]`.
  - **Width Accumulation**: Additional stroke widths are stored as increments. The absolute width for a given stroke $k$ is calculated as the primary width plus the sum of all increments before $k$.
- **Text+ Conversion**:
  - `ElementShape=1` (Outline) is enforced for all stroke elements.
  - `SoftnessX` and `SoftnessY` are explicitly set to `0` to ensure a hard edge.
  - Text+ `Offset` is explicitly fixed at `{0.0, 0.0}` to override DaVinci's default offset for certain elements.
  - **Thickness Scaling**: `Thickness = width_px / (2.0 * size_pt)`. Total width is divided by 2 because Text+ Outline is applied per-side (centered outline).

### 4. Shadow Properties
- **Primary Shadow**:
  - Defined in `TextStyle.field_10` to `field_16` (color, enabled, opacity_pct, angle_deg, distance, size, blur).
  - Gradient override exists at `field_40`, activated by a flag at `field_39`.
- **Additional Shadows**:
  - Decoded from `TextStyle.field_37[]` with the same property structure.
- **Text+ Conversion**:
  - **Offsets**: `angle` and `distance` are converted to 2D coordinates. Premiere's angle rotates clockwise from the right, while Resolve's Y-axis is inverted (UP).
    - $dx = -distance \times \cos(\theta) / scale$
    - $dy = -distance \times \sin(\theta) / scale$
    - *(where scale = size_pt)*
  - **Blur (Softness)**: Premiere's shadow blur (`field_16`) translates to Text+ `SoftnessX` and `SoftnessY` with a specific wide-tail multiplier:
    - $SoftnessX, Y = (blur\_px \times 4.0) / size\_pt$
    - The `Softness` master toggle is set to `1` when blur > 0.
  - **Size / Spread**: Premiere's `size` (spread at `field_15`) is decoded but **NOT** mapped to Text+ `Size{N}`. However, if `size_px > 0`, the shadow uses `ElementShape=1` (Outline) and calculates thickness. Otherwise, it uses `ElementShape=0` (Text Fill displaced).
  - **Opacity**: Converted from percentage `opacity_pct / 100.0`.

### 5. Gradient Interpretation
Gradients are identical in structure whether applied to fills, strokes, or shadows:
- **Mapping Angle**: Text+ mapping angles rotate counter-clockwise, opposite to Premiere.
  - $fusion\_angle = (-90.0 - angle) \pmod{360}$
- **Color Stops & Midpoints**: Premiere includes midpoints (default `0.5`). If a midpoint deviates from `0.5`, the script approximates the Premiere falloff by automatically inserting a `50%` blended color stop at the calculated offset position.

### 6. Kerning
- Sourced from `attrs.field_7`.
- Converted to Text+ `CharacterSpacing` via: $1.0 + \frac{kerning}{1000.0}$.

## Fields Fixed to Defaults or Unmapped
The following attributes are explicitly handled via fixed default values or are left unmapped to match observed ground truth behavior:
- **Point Size**: Hardcoded to `100.0`.
- **Shadow Size (Spread)**: Deliberately unmapped for pure shadows (`ElementShape=0`) because Text+ does not respond to `Size{N}` for text fill shapes.
- **Leading / Tracking / Baseline Shift**: Not mapped.
- **Font Fallbacks**: No multi-font fallback array is supported in output; only the first string from `TextStyle.field_1[]` is assigned to `Font`.
- **Stroke Offsets**: Always forced to `0.0`.
- **Solid Fill Alpha**: Solid fill mode explicitly emits `Alpha1 = 1.0` (Opaque), as standard decoding of RGB fields never assigns per-channel alphas unless derived from a gradient opacity stop.
- **Background / Border elements**: Not supported or found in the corpus (no data emitted for `ElementShape=2`).
