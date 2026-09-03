#!/usr/bin/env python3
"""Offline integrity check for the importer and a source-export package."""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
EMBEDDED_EXPORT = BASE / "source_export.zip"
PROFILE = BASE / "source_profile.json"
REQUIRED = {
    "manifest.json",
    "data/projects/TES/project.json",
    "data/projects/TES/workflow_scheme_association.json",
    "data/projects/TES/field_configuration_scheme_association.json",
    "data/fields/fields.json",
    "data/workflows/statuses.json",
    "data/screens/screens.json",
    "data/filters/filters.json",
    "data/boards/boards.json",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def select_export() -> tuple[Path, bool]:
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1].strip('"'))
        if candidate.suffix.lower() == ".zip" and candidate.exists():
            return candidate, False
    return EMBEDDED_EXPORT, True


def main() -> int:
    export, embedded = select_export()
    if not export.exists():
        print(f"ERROR: source export not found: {export}", file=sys.stderr)
        return 2
    actual = sha256(export)
    if embedded and PROFILE.exists():
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        expected = profile.get("sourceExportSha256")
        if expected and actual != expected:
            print("ERROR: embedded export fingerprint does not match source_profile.json.", file=sys.stderr)
            return 2

    with zipfile.ZipFile(export) as z:
        names = z.namelist()
        roots = sorted({name.split("/", 1)[0] for name in names if "/" in name})
        if len(roots) != 1:
            print(f"ERROR: expected one export root folder, found {roots}.", file=sys.stderr)
            return 2
        root = roots[0] + "/"
        relative = {name[len(root):] for name in names if name.startswith(root)}
        missing = sorted(REQUIRED - relative)
        if missing:
            print("ERROR: source export is missing required files:")
            for item in missing:
                print(f"  - {item}")
            return 2
        manifest = json.loads(z.read(root + "manifest.json"))
        has_defaults = any(
            name.startswith(root + "data/fields/default_values_grouped/")
            or name.startswith(root + "data/fields/default_values_legacy/")
            for name in names
        )
        has_priority_details = (
            root + "data/projects/TES/priority_scheme.json" in names
            or any(name.startswith(root + "data/reference/priority_scheme_details/") for name in names)
        )
        has_filter_columns = any(name.startswith(root + "data/filters/columns/") and name.endswith(".json") for name in names)
        has_work_type_avatars = (root + "data/work_types/avatar_manifest.json") in names
        has_epics = any(name.startswith(root + "data/issues/epics/") and name.endswith(".json") for name in names)
        has_timeline = any(name.startswith(root + "data/boards/timeline_candidates/") and name.endswith(".json") for name in names) or any(
            name.startswith(root + "data/boards/properties/") and "jsw-roadmaps-" in name for name in names
        )
        has_list_candidates = any(name.endswith("/list_view_candidates.json") for name in names)
        has_explicit_list_views = any(name.startswith(root + "data/list_views/") and name.endswith(".json") for name in names)

    print("Package validation passed.")
    print(f"Source export: {export}")
    print(f"Source: {manifest.get('sourceSite')} / {', '.join(manifest.get('selectedProjectKeys') or [])}")
    print(f"Exporter version: {(manifest.get('exporter') or {}).get('version')}")
    print(f"SHA-256: {actual}")
    if has_defaults:
        print("Default-value capture: present")
    else:
        print("WARNING: Default-value capture is absent. APPLY mode will require Exporter v1.2+.")
    if has_priority_details:
        print("Priority-scheme detail capture: present")
    else:
        print("WARNING: Per-project priority-scheme detail is absent. Default schemes can be inferred, but Exporter v1.3+ is recommended.")
    if has_filter_columns:
        print("Saved-filter column capture: present")
    else:
        print("WARNING: Saved-filter column files are absent; filter column order cannot be replicated.")
    if has_work_type_avatars:
        print("Work-type avatar capture: present")
    else:
        print("WARNING: Work-type avatar assets are absent. Scheme/workflow/screen cloning can continue, but custom work-type icons need a fresh Exporter v1.4+ package.")
    if has_epics:
        print("Epic work-item capture: present (v3.10 can recreate exported Epics)")
    else:
        print("WARNING: Epic work-item capture is absent. Exporter v1.6+ is required to recreate Epics.")
    if has_timeline:
        print("Board Timeline property capture: present (v3.10 can apply it)")
    else:
        print("WARNING: Board Timeline property capture is absent.")
    if has_explicit_list_views:
        print("Space List-view capture: present (v3.10 can apply the exported/user-default ordered columns to the destination importing user and verify them).")
    elif has_list_candidates:
        print("WARNING: Only browser List-view candidates are present. They are diagnostic snapshots, not a replayable List-column configuration.")
    else:
        print("WARNING: Space List-view columns/order were not captured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
