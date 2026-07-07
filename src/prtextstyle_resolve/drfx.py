"""
drfx.py -- build a `.drfx` bundle (a plain ZIP file) from a loose folder
tree, and helpers for laying out the `Edit/Titles/<Category>/<SubCategory>`
tree that `.setting` files must live in for the Edit-page Titles browser.

See docs/SCHEMA.md §2 for the full format writeup.
"""
from __future__ import annotations

import zipfile
from pathlib import Path


def titles_dir(out_dir: Path, category: str, subcategory: str) -> Path:
    """Return (and create) `<out_dir>/Edit/Titles/<category>/<subcategory>`."""
    target = out_dir / "Edit" / "Titles" / category / subcategory
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_drfx(source_dir: Path, output_path: Path) -> None:
    """Zip `source_dir` (expected to contain an `Edit/` subtree, per the
    task's `convert` output layout) into `output_path` as a `.drfx`
    bundle.

    `source_dir` itself is NOT included as a path prefix in the archive --
    only its contents (e.g. `Edit/Titles/...`), matching the reference
    `build.sh`: `zip -r Custom-drfx.drfx Edit`.
    """
    source_dir = Path(source_dir).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for f in sorted(source_dir.rglob("*")):
            if not f.is_file():
                continue
            if f.name == ".DS_Store":
                continue
            # CRITICAL: never zip the output bundle into itself. If
            # `output_path` lives inside `source_dir`, rglob would pick it
            # up and zip.write would feed the growing archive back into
            # itself -- an unbounded loop that fills the disk (observed:
            # an 89GB runaway). Skip the output file (and any other .drfx
            # bundle) unconditionally.
            if f.resolve() == output_path or f.suffix.lower() == ".drfx":
                continue
            if f.name == "report.json" and f.parent == source_dir:
                # report.json is a sibling of Edit/, not part of the
                # installable bundle tree.
                continue
            zf.write(f, f.relative_to(source_dir))
