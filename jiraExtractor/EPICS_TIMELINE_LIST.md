# Epic, Timeline, and List-view handling in v3.8

## Epics

Source path: `data/issues/epics/by_project/<SOURCE_KEY>.json` (with `selected_projects.json` fallback).

The importer creates the exported Epic in the destination project after work types, fields, workflows, components, versions, priorities, and field defaults have been prepared. It maps source IDs to destination IDs and restores the exported status through Jira's transition API when a matching transition is available.

Output: `epic_import_results.json`.

## Board Timeline

Preferred source path: `data/boards/properties/<BOARD_ID>__jsw-roadmaps-*.json`.

Fallback source path: `data/boards/timeline_candidates/<BOARD_ID>.json`.

After each destination board is created or reused, v3.8 writes the exported Timeline properties to that destination board and reads them back for verification.

Output: `board_timeline_verification.json`.

## Space List columns/order

The current v1.6 TES export contains `list_view_candidates.json`, but not an explicit ordered List-view configuration that can be safely replayed. Saved-filter columns are still imported separately and correctly; they are not substituted for the space List configuration.

Output: `list_view_import_report.json`.

If exact space List replication is required, the exporter must first capture a stable Jira saved-view/List schema or another explicit representation. The importer intentionally avoids replaying opaque browser storage or undocumented payloads blindly.
