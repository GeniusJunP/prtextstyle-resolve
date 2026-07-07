"""
font_resolver.py -- resolve a Premiere PostScript font name to a real
installed (family, style) pair, using macOS `fc-list` (fontconfig) as
the source of truth.

Why this exists (Phase 7, task 1): the OLD-dialect corpus stores a
single PostScript name per preset (e.g. `Toppan-BunkyuMidashiGoStdN-EB`)
and emits it verbatim as Text+ `Font`, leaving `Style` at the template
default. Real hand-authored Text+ titles split this into `Font =
"Noto Sans JP"` + `Style = "Black"`; Text+ may not resolve a raw
PostScript name the way it resolves a family+style pair. See
docs/SCHEMA.md point 3.

Method (per the task brief): query the actual installed font database
via `fc-list` for an EXACT postscriptname match, and read its real
family/style back. No network access. No fuzzy/heuristic matching, and
no guessing from the preset's (human, Japanese) display name -- a
PostScript name either matches an installed font's own postscriptname
byte-for-byte, or it doesn't, in which case the caller must fall back to
emitting the raw PostScript name unchanged and surfacing a warning (see
text_style.build_text_style). This module never decides that fallback
itself; it only ever answers "found" (with real data) or "not found".
"""
from __future__ import annotations

import functools
import subprocess
from typing import Dict, Optional, Tuple

FontIndex = Dict[str, Tuple[str, str]]  # postscript_name -> (family, style)

_FC_LIST_ARGS = ["fc-list", ":", "postscriptname", "family", "style"]


def _run_fc_list() -> str:
    """Invoke `fc-list` and return its stdout, or "" if it's unavailable,
    times out, or errors -- callers treat that identically to "no fonts
    found" (never raises; a missing `fc-list` binary must not crash the
    converter, it just means every font falls back to unresolved)."""
    try:
        result = subprocess.run(
            _FC_LIST_ARGS,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def parse_fc_list_output(text: str) -> FontIndex:
    """Parse `fc-list : postscriptname family style` output into
    {postscriptname: (family, style)}.

    Each line has the form:
        <family1>,<family2>,...:style=<style1>,<style2>,...:postscriptname=<ps>
    (colon-separates the three requested fields; each field may itself
    list several comma-separated localized alternates -- fc-list puts
    the unlocalized/default name first in every sample observed here,
    so the first entry in each comma list is treated as canonical).

    A PostScript name is assumed unique per installed font; if fc-list
    ever reports the same postscriptname twice (e.g. two installed
    copies), the first line wins and later duplicates are ignored --
    deterministic, not a guess.
    """
    index: FontIndex = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":")
        family_field = parts[0] if parts else ""
        style_field = ""
        ps_field = ""
        for part in parts[1:]:
            if part.startswith("style="):
                style_field = part[len("style=") :]
            elif part.startswith("postscriptname="):
                ps_field = part[len("postscriptname=") :]
        ps_field = ps_field.strip()
        if not ps_field:
            continue  # fc-list occasionally omits postscriptname for bitmap/legacy fonts
        family = family_field.split(",")[0].strip()
        style = style_field.split(",")[0].strip() if style_field else "Regular"
        index.setdefault(ps_field, (family, style))
    return index


@functools.lru_cache(maxsize=1)
def system_font_index() -> FontIndex:
    """The real, installed-font index, queried once per process and
    cached (fc-list enumerates the whole font database; re-running it
    per preset would be needlessly slow across a 216-preset corpus)."""
    return parse_fc_list_output(_run_fc_list())


def resolve_font(
    postscript_name: str,
    font_index: Optional[FontIndex] = None,
    font_mapping: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[str, str]]:
    """Exact-match `postscript_name` against an installed-font index.

    Returns `(family, style)` if found, else `None` -- never raises,
    never fuzzy-matches. `index` is injectable (tests pass a fake dict);
    omitting it queries the real system via `system_font_index()`.
    """
    index = font_index if font_index is not None else system_font_index()
    if postscript_name in index:
        return index[postscript_name]
    if font_mapping and postscript_name in font_mapping:
        mapped_name = font_mapping[postscript_name]
        if mapped_name in index:
            return index[mapped_name]
    return None
