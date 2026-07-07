"""prtextstyle_resolve -- convert Premiere Pro .prtextstyle text style
presets into DaVinci Resolve / Fusion Text+ `.setting` files and `.drfx`
bundles.

Only structurally-confirmed fields (see docs/SCHEMA.md)
are ever emitted; preset names are used solely for filesystem
identification and the initial `StyledText` value, never to infer style
attributes.
"""
from .text_style import Fill, GradientStop, Shadow, Stroke, TextStyle, build_text_style

__all__ = [
    "TextStyle",
    "Fill",
    "GradientStop",
    "Stroke",
    "Shadow",
    "build_text_style",
]

__version__ = "0.2.0"
