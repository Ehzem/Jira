# Changelog

## 1.9.0

- Adds a read-only Jira **CSV current-fields probe** for the authenticated account's All Work/List columns.
- Adds a project-JQL current-fields probe for every selected space/project.
- Adds a current-fields probe for every exported saved filter.
- Effective filter-column resolution is now: explicit filter layout -> saved-filter current-fields probe -> user defaults -> system defaults.
- Keeps All Work defaults, saved-filter layouts, project List evidence, and board workflow columns as separate configuration scopes.
- Project List capture remains conservative: a current-fields probe is useful display evidence but is not mislabeled as an exact saved-space-view API.

# v1.8.0

- Adds `data/filters/effective_columns/<FILTER_ID>.json` for every visible filter.
- Preserves the effective ordered column layout even when Jira returns 404 because the filter inherits the user's/default column layout.
- Keeps raw filter-specific layouts separately under `data/filters/columns/`.
- Adds `data/filters/effective_columns_summary.json` so missing/inherited layouts are explicit rather than appearing dropped.

# Changelog

## 1.7.0

- Added supported `GET /rest/api/3/user/columns` capture for the authenticated user's default List/issue-table columns in exact Jira-returned order.
- Added optional `GET /rest/api/3/settings/columns` capture for Jira system-default columns.
- Expanded Firefox List-view capture from localStorage-only to Jira-origin localStorage **and IndexedDB**.
- Added Jira-scoped Firefox sessionStorage extraction from `sessionstore*.jsonlz4` files. The full browser session and cookies are not exported.
- Added field-aware scanning that looks for ordered Jira field sequences instead of keyword-only `list_view_candidates`.
- Added `data/browser_view_state/detected_list_column_sequences.json`.
- Added per-project `data/projects/<KEY>/list_view/column_capture.json`.
- Added `data/list_views/capture_summary.json` and `reports/list_view_columns.csv`.
- Exact project List capture is now conservative: the exporter reports `exactProjectListColumnOrderCaptured=true` only when a project-scoped browser sequence with List/column context is found.
- User/system defaults are preserved as explicit fallbacks and are never mislabeled as a project-specific saved view.
- Existing v1.6 Epic export, Timeline/board-property capture, filters, boards, workflows, schemes, fields, permissions, priorities, and other configuration exports remain intact.

## 1.6.0

- Added site-wide Epic-level work-item export using Jira hierarchy metadata and read-only JQL search.
- Added per-Epic, per-project, selected-project, and CSV outputs.
