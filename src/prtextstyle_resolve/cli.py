"""
cli.py -- command-line interface for prtextstyle_resolve.

Commands:
    convert <file.prtextstyle> --out-dir <path> [--category ...] [--subcategory ...]
        Decode every preset and write one Fusion `.setting` file per
        preset into `<out-dir>/Edit/Titles/<category>/<subcategory>/`,
        plus a `report.json` describing what was emitted and any
        per-preset warnings.

    build-drfx <folder> --out <bundle.drfx>
        Zip a folder (expected to contain an `Edit/` tree, e.g. the
        output of `convert`) into an installable `.drfx` bundle.

    list-presets <file.prtextstyle>
        Print the index and name of every preset found in the file,
        without decoding style attributes. Useful for a quick sanity
        check of what a .prtextstyle file contains.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import drfx, fusion_setting
from .font_resolver import system_font_index
from .parser import extract_presets, iter_presets
from .text_style import build_text_style

DEFAULT_CATEGORY = "CustomPresets"
DEFAULT_SUBCATEGORY = "TextStyles"

# Phase 7 task 2/3 outcomes: these are corpus-wide, not per-preset, findings
# (see docs/SCHEMA.md for the full field-presence audit
# behind each). Surfaced once at the top level of report.json rather than
# repeated on every preset entry.
STROKE_POSITION_NOTE = (
    "All strokes are emitted as CENTER position (Text+ OutsideOnly=0). A systematic "
    "presence/variability audit of every OLD-dialect field touching strokes (attrs.field_4/5/6 "
    "primary stroke, attrs.field_17[] additional strokes, and every sibling field id observed "
    "anywhere in attrs or the stroke-item vector across all 216 presets) found no boolean/enum "
    "field that varies in a way consistent with an inside/center/outside flag -- the only "
    "'extra' fields found on stroke items (field_3/field_4) turned out to be a per-stroke "
    "gradient descriptor (mirroring ShadowItem.field_8), not a position flag. No mapping was "
    "invented. Center matches every value read off the Premiere UI so far (OLD_SCHEMA_TRUTH.md); "
    "presets using inside or outside stroke placement in Premiere will not convert with the "
    "correct placement (Text+ itself also has no inside-only mode -- outside strokes could only "
    "ever be approximated as centered even if detected)."
)
BACKGROUND_NOTE = (
    "No background is emitted for any preset. No background field was confidently identified in "
    "the OLD dialect after a corpus-wide field-presence audit: TextStyle.field_39 (present in "
    "35/216 presets) is an isolated boolean with no correlated color/size field anywhere in the "
    "same table; attrs.field_15 (11/216) has no color/size correlate either; attrs.field_21 and "
    "attrs.field_23 are RGBColor-shaped and always present (216/216, unlike every confirmed "
    "toggleable color in this schema which is gated by a companion enabled flag), and decode to "
    "mostly-white with a handful of outlier colors -- consistent with incidental preview-swatch "
    "cache data (this table is literally named PreviewAttributes) rather than a toggleable "
    "background box. No field encodes a plausible background enable+color+size cluster. See "
    "docs/SCHEMA.md for the full audit; if the corpus does contain presets "
    "with a background turned on in the Premiere UI, a UI cheat sheet naming one would let this "
    "be re-investigated with a known-positive example to diff against."
)

# Warning-message categories emitted by `build_text_style` (text_style.py).
# These regexes pattern-match the *actual* warning strings already written
# into each preset's report entry -- they do not introduce any new
# hardcoded meaning, they just group/count what build_text_style already
# said, for a human-readable stderr summary. Order matters: first match
# wins, so put more specific patterns first.
_WARNING_CATEGORIES = [
    ("dialect mismatch (NOT old dialect -- field map may be wrong)", re.compile(r"^payload dialect detected as")),
    ("no font name resolved", re.compile(r"^no font name resolved")),
    ("unusual font string", re.compile(r"^unusual font string")),
    ("font not installed locally; Font/Style split not resolved (raw PostScript name emitted)", re.compile(r"not found by an exact PostScript-name match")),
    ("gradient fill: midpoints dropped; angle routed to ShadingMappingAngle1 (verify zero-ref/sign in Resolve)", re.compile(r"^gradient fill:")),
    ("fill alpha fixed at 1.0 (no confirmed fill-opacity field)", re.compile(r"^fill alpha fixed at 1\.0")),
    ("kerning routed to CharacterSpacing (1 + kerning/1000, uniform tracking)", re.compile(r"^kerning=")),
    ("stroke/shadow entries disabled in source; omitted", re.compile(r"^\d+ stroke/shadow entrie\(s\) present but disabled")),
    ("effect(s) dropped: exceeded 7-slot shading-element budget", re.compile(r"^\d+ effect\(s\) exceeded the")),
    ("shadow subfield missing while enabled; numeric default applied", re.compile(r": (opacity_pct|angle_deg|distance|size|blur) missing while enabled")),
    ("stroke width_px missing while enabled; defaulted to 0", re.compile(r": width_px missing while enabled")),
]


def _summarize_warnings(entries: List[Dict[str, Any]]) -> List[str]:
    """Aggregate the per-preset warning strings already produced by
    `build_text_style` into human-readable counted summary lines for the
    CLI's stderr output. Every uncertainty that surfaces per-preset in
    `report.json` is represented here too -- no silent fallpaths. Any
    warning that doesn't match a known category is still counted under
    'other' so nothing is silently dropped from the summary."""
    counts: Dict[str, int] = {label: 0 for label, _ in _WARNING_CATEGORIES}
    other = 0

    for entry in entries:
        for w in entry.get("warnings") or []:
            for label, rx in _WARNING_CATEGORIES:
                if rx.search(w):
                    counts[label] += 1
                    break
            else:
                other += 1

    lines: List[str] = []
    for label, _ in _WARNING_CATEGORIES:
        c = counts[label]
        if c:
            lines.append(f"  {c:>3} {label}")
    if other:
        lines.append(f"  {other:>3} other warning(s) (see report.json for full text)")
    return lines


def _unique_key(base: str, seen: Dict[str, int]) -> str:
    """Disambiguate a filename/MacroOperator key against collisions from
    prior presets in the same run (safe_key() can theoretically collapse
    two distinct names -- not observed in the 216-preset sample corpus,
    but handled defensively)."""
    count = seen.get(base, 0)
    seen[base] = count + 1
    if count == 0:
        return base
    return f"{base}_{count + 1}"


def cmd_convert(args: argparse.Namespace) -> int:
    src = Path(args.prtextstyle_file)
    out_dir = Path(args.out_dir)
    target_dir = drfx.titles_dir(out_dir, args.category, args.subcategory)

    report_path = Path(args.report) if args.report else out_dir / "report.json"

    entries: List[Dict[str, Any]] = []
    seen_keys: Dict[str, int] = {}
    ok_count = 0
    error_count = 0
    new_dialect_count = 0
    stroke_histogram: Dict[int, int] = {}
    shadow_histogram: Dict[int, int] = {}
    fonts_resolved = 0
    fonts_unresolved = 0

    # Query the installed-font database ONCE for the whole corpus (not per
    # preset -- fc-list enumerates the entire system font list, so calling
    # it 216 times would be needlessly slow). See font_resolver.py.
    font_index = system_font_index()

    font_mapping = None
    if getattr(args, "font_mapping", None):
        mapping_path = Path(args.font_mapping)
        if mapping_path.exists():
            with open(mapping_path, "r", encoding="utf-8") as f:
                font_mapping = json.load(f)

    for decoded in iter_presets(str(src)):
        if decoded.parse_error is not None or decoded.semantic is None:
            entries.append(
                {
                    "index": decoded.index,
                    "name": decoded.name,
                    "filename": None,
                    "parse_error": decoded.parse_error,
                }
            )
            error_count += 1
            continue

        style = build_text_style(
            decoded.index,
            decoded.name,
            decoded.semantic,
            font_index=font_index,
            font_mapping=font_mapping,
        )
        if style.dialect != "old":
            new_dialect_count += 1
        if style.font_ps_name is not None:
            if style.font_resolved:
                fonts_resolved += 1
            else:
                fonts_unresolved += 1
        base_key = fusion_setting.safe_key(f"{args.filename_prefix}{decoded.name}")
        key = _unique_key(base_key, seen_keys)
        filename = f"{key}.setting"
        out_path = target_dir / filename

        fusion_setting.write_setting_file(style, out_path, key)
        
        try:
            from . import thumbnail
            thumbnail.generate_thumbnail(style, out_path.with_suffix(".png"))
        except Exception as e:
            print(f"warning: thumbnail generation failed for {filename}: {e}", file=sys.stderr)

        entry = style.report_dict()
        entry["filename"] = filename
        entry["parse_error"] = None
        entries.append(entry)
        ok_count += 1
        stroke_histogram[len(style.strokes)] = stroke_histogram.get(len(style.strokes), 0) + 1
        shadow_histogram[len(style.shadows)] = shadow_histogram.get(len(style.shadows), 0) + 1

    report = {
        "source": str(src),
        "out_dir": str(out_dir),
        "category": args.category,
        "subcategory": args.subcategory,
        "total_presets": len(entries),
        "converted": ok_count,
        "errors": error_count,
        "new_dialect_presets": new_dialect_count,
        "stroke_count_histogram": stroke_histogram,
        "shadow_count_histogram": shadow_histogram,
        "fonts_resolved": fonts_resolved,
        "fonts_unresolved": fonts_unresolved,
        "stroke_position_note": STROKE_POSITION_NOTE,
        "background_note": BACKGROUND_NOTE,
        "presets": entries,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"converted {ok_count}/{len(entries)} presets -> {target_dir}", file=sys.stderr)
    if fonts_unresolved:
        print(
            f"NOTE: {fonts_unresolved}/{fonts_resolved + fonts_unresolved} distinct-font preset(s) "
            "could not be resolved to a real installed Font/Style split (font not installed/active "
            "on this machine); raw PostScript names were emitted unchanged as Font -- see "
            "report.json's per-preset font_resolved flag and docs/SCHEMA.md",
            file=sys.stderr,
        )
    if new_dialect_count:
        print(
            f"WARNING: {new_dialect_count} preset(s) were NOT old-dialect (version_tag absent); "
            "this decoder's field map is calibrated for the old dialect only -- see report.json",
            file=sys.stderr,
        )
    summary_lines = _summarize_warnings(entries)
    if summary_lines:
        print("warnings summary:", file=sys.stderr)
        for line in summary_lines:
            print(line, file=sys.stderr)
    if error_count:
        print(f"{error_count} preset(s) failed to decode; see {report_path}", file=sys.stderr)
    print(f"report written to {report_path}", file=sys.stderr)
    return 0 if error_count == 0 else 1


def cmd_build_drfx(args: argparse.Namespace) -> int:
    source_dir = Path(args.folder)
    output_path = Path(args.out)
    drfx.build_drfx(source_dir, output_path)
    print(f"wrote {output_path}", file=sys.stderr)
    return 0


def cmd_list_presets(args: argparse.Namespace) -> int:
    presets = extract_presets(args.prtextstyle_file)
    if args.json:
        payload = [{"index": p.index, "name": p.name} for p in presets]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for p in presets:
            print(f"{p.index}\t{p.name}")
    print(f"{len(presets)} preset(s)", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="prtextstyle_resolve",
        description="Convert Premiere Pro .prtextstyle presets into DaVinci Resolve/Fusion Text+ .setting files.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_convert = sub.add_parser("convert", help="Decode a .prtextstyle file and emit .setting files + report.json")
    p_convert.add_argument("prtextstyle_file", help="path to a .prtextstyle file")
    p_convert.add_argument("--out-dir", required=True, help="output directory (will contain Edit/Titles/...)")
    p_convert.add_argument("--category", default=DEFAULT_CATEGORY, help=f"Titles browser category folder (default: {DEFAULT_CATEGORY!r})")
    p_convert.add_argument("--subcategory", default=DEFAULT_SUBCATEGORY, help=f"Titles browser sub-category folder (default: {DEFAULT_SUBCATEGORY!r})")
    p_convert.add_argument("--filename-prefix", default="", help="optional prefix prepended to each .setting filename and MacroOperator key")
    p_convert.add_argument("--font-mapping", default=None, help="path to font_mapping.json for similar font substitution")
    p_convert.add_argument("--report", default=None, help="path to write report.json (default: <out-dir>/report.json)")
    p_convert.set_defaults(func=cmd_convert)

    p_build = sub.add_parser("build-drfx", help="Zip a folder (containing Edit/...) into an installable .drfx bundle")
    p_build.add_argument("folder", help="folder containing the Edit/ tree (e.g. --out-dir from `convert`)")
    p_build.add_argument("--out", required=True, help="output .drfx path")
    p_build.set_defaults(func=cmd_build_drfx)

    p_list = sub.add_parser("list-presets", help="List preset index/name pairs found in a .prtextstyle file")
    p_list.add_argument("prtextstyle_file", help="path to a .prtextstyle file")
    p_list.add_argument("--json", action="store_true", help="print as JSON instead of tab-separated text")
    p_list.set_defaults(func=cmd_list_presets)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
