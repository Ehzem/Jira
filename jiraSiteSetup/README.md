# Jira Source-Site Exporter

**Version 1.9.0**

A read-only Python exporter for Jira Cloud configuration. It creates a structured JSON/CSV package that can be used by the destination-site importer with source-to-destination ID remapping.

## What it exports

- Site/server information and the authenticated account
- Projects/spaces and project details
- Work types (issue types), work type schemes, mappings, and selected-project custom work-type avatar image assets
- Fields, custom-field contexts, context mappings, select-list options, and context default values
- Legacy field configurations/schemes when available
- New Field Schemes when the site has the beta/opt-in API
- Screens, tabs, fields, screen schemes, and work type screen schemes
- Statuses, workflows, transitions, conditions, validators, post-functions, transition screens, and workflow schemes
- Permission schemes, notification schemes, and issue security schemes when available
- Accessible filters and their JQL/sharing details
- Boards, board filter references, column/status mappings, estimation, ranking, and board properties exposed by Jira
- All visible Epic-level work items across the source site, including all fields visible to the exporting account
- Best-effort raw Board-settings edit models plus extracted Timeline/Roadmap configuration candidates for each board
- Jira's authenticated-user default issue-table/List columns and exact returned order via `GET /rest/api/3/user/columns`
- Jira system-default issue-table columns when the exporting account has Jira-admin permission
- Optional Firefox inspection of Jira-origin localStorage, IndexedDB, and Jira-scoped sessionStorage for project/space List-view field order
- Components, releases/versions, project features, project roles and actors
- Priorities, their names/descriptions/colors/icons/order, priority schemes, per-scheme defaults, and project associations
- Groups, group membership, and visible users unless skipped
- Project-to-scheme associations

The exporter also creates:

- `data/work_types/avatar_manifest.json`
- `data/work_types/avatars/` for downloaded selected work-type icons
- `reports/workflow_transition_rules.csv`
- `reports/project_configuration_matrix.csv`
- `reports/fields.csv`
- `reports/boards.csv`
- `reports/epics.csv`
- `data/issues/epics/sitewide.json`
- `data/issues/epics/by_key/`
- `data/issues/epics/by_project/`
- `data/issues/epics/selected_projects.json`
- `reports/endpoint_status.csv`
- `data/fields/default_values_grouped/`
- `data/fields/default_values_legacy/`
- `data/reference/priority_scheme_details/`
- `data/projects/<KEY>/priority_scheme.json`
- `data/boards/editmodel/<BOARD_ID>.json`
- `data/boards/timeline_candidates/<BOARD_ID>.json`
- `data/list_views/user_default_columns.json`
- `data/list_views/system_default_columns.json` when accessible
- `data/list_views/capture_summary.json`
- `data/projects/<KEY>/list_view/column_capture.json`
- `reports/list_view_columns.csv`
- `data/browser_view_state/firefox_capture_manifest.json` when browser-state capture is enabled and available
- `manual_actions.md`
- `manifest.json`

## Requirements

- Python 3.10 or newer
- A Jira Cloud account with Jira administrator/site administrator access for the broadest export
- An Atlassian API token

The script never saves the API token.

## Version 1.7 additions

Version 1.7 replaces the v1.5/v1.6 **localStorage-only** List-view heuristic with a multi-source capture and explicit validation model.

### 1. Supported Jira user-default columns API

The exporter now calls:

```text
GET /rest/api/3/user/columns
```

This is Atlassian's supported endpoint for the authenticated user's default issue-table columns. Jira's List UI can use these when **Configure columns → My defaults** is the active source. The order returned by Jira is preserved exactly in:

```text
data/list_views/user_default_columns.json
```

When the account has permission, v1.9 also calls:

```text
GET /rest/api/3/settings/columns
```

and saves the site system defaults separately.

### 2. Firefox localStorage + IndexedDB + sessionStorage

v1.6 only inspected Firefox localStorage. v1.9 additionally captures Jira-origin IndexedDB and extracts Jira-scoped sessionStorage from Firefox session-restore data. Cookies are **not** copied and the full browser session is **not** written to the export.

The browser scanner looks for actual ordered sequences of Jira field IDs/names such as:

```text
Work → Status → Parent → Assignee → Start date → Due date
```

rather than treating any storage row containing the word `column` as success.

### 3. Per-project validation

For each selected project/space, v1.9 writes:

```text
data/projects/<KEY>/list_view/column_capture.json
```

The file contains:

- `exactProjectListColumnOrderCaptured`
- the chosen source and scope
- the ordered columns
- user/system defaults
- project-scoped browser candidates
- board filter columns for diagnosis only
- project-property view candidates

The exporter only sets `exactProjectListColumnOrderCaptured: true` when it finds an ordered browser-state field sequence with both the selected project key and List/column context. Otherwise it keeps the supported Jira user-default order as a clearly labelled fallback instead of pretending it captured a project-specific saved view.

A summary is written to:

```text
data/list_views/capture_summary.json
reports/list_view_columns.csv
```

### Important Jira limitation

Atlassian exposes saved-filter columns, user-default issue-table columns, and system-default issue-table columns through public REST APIs. Jira's newer **space saved views** can also persist List/Timeline display settings, but Atlassian does not currently document a public REST resource for retrieving the complete project saved-view model. v1.9 therefore captures all supported server-side column sources plus substantially more Firefox state and reports exactly which source was found.

For the strongest browser capture, leave the desired Jira List tab open in Firefox and run the exporter on the same OS user account.

## Version 1.6 additions

Version 1.6 adds a site-wide Epic work-item export. It identifies Jira Epic-level work types by `hierarchyLevel = 1` rather than relying only on the literal name `Epic`, so renamed Epic work types are captured as well. Atlassian documents Epic as hierarchy level 1 in Jira Cloud issue-type metadata.

The exporter performs a read-only JQL search for every Epic-level work item visible to the authenticated account and requests `*all` fields. The authoritative combined export is written to:

```text
data/issues/epics/sitewide.json
```

It also writes:

```text
data/issues/epics/epic_issue_types.json
data/issues/epics/by_key/<EPIC_KEY>.json
data/issues/epics/by_project/<PROJECT_KEY>.json
data/issues/epics/selected_projects.json
reports/epics.csv
```

The site-wide Epic capture still runs when `--project TES` is supplied, because the request was to retain any Epics created anywhere on the source Jira site. `selected_projects.json` is provided as a convenient subset for destination importers that are only cloning the selected project(s). Only Epics the authenticated Jira account is permitted to browse can be returned by Jira's search API.

To disable Epic work-item capture:

```powershell
py jira_source_exporter.py --skip-epics
```

Or set `JIRA_INCLUDE_EPICS=false` in `.env`.

## Version 1.5 additions

Version 1.5 adds two view-configuration capture paths that are separate from Jira's public Scrum/Kanban board-column API.

### Board Timeline settings

For every board, the exporter still saves the documented Agile board configuration and now also makes a read-only, best-effort request for Jira's Board-settings edit model. The raw response is saved to:

```text
data/boards/editmodel/<BOARD_ID>.json
```

A keyword-indexed convenience extract is written to:

```text
data/boards/timeline_candidates/<BOARD_ID>.json
```

This is intended to retain settings exposed by the Board settings UI that Jira does not include in the public Agile board-configuration response, including Timeline/Roadmap-related values when the current Jira Cloud build returns them. The Jira-internal endpoint is undocumented and therefore optional: if Atlassian changes or blocks it, the export continues and records the failure in the endpoint report/manual actions.

### Project/space List-view columns and order

Jira Cloud can keep personal/unsaved List-view field selection and order in browser-local state, while newer Jira experiences also support space-admin saved views. v1.9 supersedes this v1.5 localStorage-only method with user-default API capture plus Firefox localStorage, IndexedDB, and sessionStorage inspection. Browser artifacts and field-aware detections are written under:

```text
data/browser_view_state/firefox/<PROFILE>/<JIRA_ORIGIN>/
data/browser_view_state/detected_list_column_sequences.json
```

For the most useful capture, open the source Jira List view in Firefox under the same Windows/macOS/Linux user account, arrange the fields in the desired order, and run the exporter on that computer. If automatic profile discovery does not pick the right profile, pass:

```powershell
py jira_source_exporter.py --project TES --firefox-profile "C:\Users\YOUR_NAME\AppData\Roaming\Mozilla\Firefox\Profiles\YOUR_PROFILE"
```

To disable local browser-state capture:

```powershell
py jira_source_exporter.py --project TES --skip-browser-view-state
```

No browser cookies are intentionally copied, but localStorage is still browser-local site data and should be treated as sensitive. For personal/unsaved List layouts, restoration on another computer/site is a browser-state migration problem rather than a normal Jira REST import. Newer Jira space saved views can also persist columns and view settings server-side; Atlassian does not document those settings in the standard board REST configuration, so exact import of a saved shared view may require a tenant-specific internal endpoint.

## Version 1.4 additions

Version 1.4 downloads the selected project's custom work-type avatar images and records them in `data/work_types/avatar_manifest.json`. This makes custom icons portable between Jira sites instead of relying on a source-site `avatarId`, which is not sufficient by itself on a different destination site.

Use a fresh v1.4 export with Destination Importer v3.0 when you need a renamed/customized work type such as `Planned Task` to carry over its custom icon.

## Version 1.3 additions

Version 1.3 captures the complete priority configuration needed for cloning: the ordered priority list, renamed priority metadata, the default priority inside each scheme, and the scheme associated with each selected project.

## Version 1.2 additions

Version 1.2 exports default values for every accessible custom-field context. It saves both Jira's grouped context/work-type representation and the legacy compatibility representation so the destination importer can restore defaults on either legacy or new Field Scheme sites.

## Windows setup

Open Command Prompt or PowerShell in this folder:

```powershell
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
copy .env.example .env
notepad .env
py jira_source_exporter.py
```

You can also enter the URL, email, and token interactively instead of creating `.env`:

```powershell
py jira_source_exporter.py
```

To export only the `TES` project:

```powershell
py jira_source_exporter.py --project TES
```

To export multiple projects:

```powershell
py jira_source_exporter.py --project TES --project ABC
```

To write the output to the D drive:

```powershell
py jira_source_exporter.py --output "D:\Jira Exports"
```

## macOS/Linux setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
python3 jira_source_exporter.py
```

## Output

The script creates both a folder and ZIP such as:

```text
jira_source_export_your-site.atlassian.net_20260805T043300Z/
jira_source_export_your-site.atlassian.net_20260805T043300Z.zip
```

Use the generated ZIP as the input to Destination Importer v3.0. **Do not upload `.env`.**

## Important limitations

Jira Cloud does not expose every site setting through public REST APIs. The Timeline edit-model capture uses an undocumented Jira-internal read endpoint on a best-effort basis. For List views, v1.9 exports Jira user/system default columns through supported REST APIs and scans Firefox localStorage/IndexedDB/sessionStorage for project-scoped field order. Newer shared saved views may still require a tenant-specific internal endpoint for exact server-side recreation because Atlassian does not document the complete saved-view model in the public REST API. The exporter records unsupported/inaccessible endpoints instead of stopping. Review `reports/endpoint_status.csv`, `manual_actions.md`, and `data/errors/`.

Private filters invisible to the exporting account cannot be exported. Marketplace-app workflow rules may need the same app on the destination. Team-managed project configuration is not represented by the same global schemes as company-managed projects. User accounts must already exist or be provisioned on the destination and then mapped.

## Security

The export may contain emails, account IDs, project names, permission grants, JQL, board filters, workflow rules, Epic work-item fields, downloaded work-type icon assets, and—when enabled—browser-local Jira site data from Firefox localStorage. Store it securely.

## Typical command

```powershell
.\run_exporter_windows.bat --project TES --output "D:\Jira Exports"
```


## v1.9 column capture

v1.9 adds independent CSV current-fields probes for All Work, every selected project List, and every saved filter. See `V19_ALL_WORK_LIST_COLUMNS.md`.
