# v3.11 All Work saved-filter column fix

The source export supplied with this build proves two distinct column states:

- Filters `01 - Inbox` through `05 - Not This Semester` have real filter-specific layouts from `GET /rest/api/3/filter/{id}/columns`.
- `06 - Active Sprint`, `Active Sprint Tasks`, and the board filters return Jira 404 for that endpoint, which means they inherit **My Defaults** rather than owning a separate filter layout.

v3.10 incorrectly converted inherited filters into explicit filter layouts and also preferred the CSV current-fields probe over the persistent `/user/columns` export. This could make the destination All Work UI appear inconsistent even when individual API writes succeeded.

v3.11 restores the model faithfully:

1. Explicit source filter columns -> PUT the exact mapped order and verify it.
2. No explicit source filter layout -> DELETE destination filter columns to preserve inheritance and verify Jira returns 404.
3. Persistent My Defaults -> restore from `data/list_views/user_default_columns.json`.
4. CSV current-fields -> evidence/fallback only; never allowed to silently replace a different persistent My Defaults layout.

Jira users can still manually choose **My Defaults** while viewing a saved filter. Atlassian documents that this per-user display choice can override filter-specific columns. The REST filter-column configuration itself is nevertheless restored and verified by this importer.
