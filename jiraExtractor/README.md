# Jira Destination-Site Importer v3.8

This importer recreates an exported Jira Cloud project configuration on another Jira Cloud site. It supports both destination field-management models:

- Legacy Field Configurations and Field Configuration Schemes
- New unified Field Schemes

It also restores custom-field contexts/options/default values, priorities and priority schemes, saved-filter columns, work-type membership/defaults, screen routing, workflow routing, and exported work-type icons when the source package contains the avatar image assets added in Exporter v1.4.


## New in v3.8: Epics, board Timeline settings, and List-view diagnostics

When used with **Source Exporter v1.6+**, v3.8 now consumes the exported Epic work-item data and recreates Epics in the destination project. It maps the source Epic work type, components, fix versions, priority, portable create-screen fields, and mapped custom-field options where Jira allows them. After creation it restores the exported Epic status when the destination workflow exposes a transition to that status. Re-runs reuse a unique same-summary/same-work-type Epic instead of blindly duplicating it. Results are written to `epic_import_results.json`, and source-to-destination issue mappings are added to `id_map.json`.

For each cloned board, v3.8 now reads the v1.6 board Timeline properties and writes them through Jira Software's supported board-property API. This covers the settings exported as:

- `jsw-roadmaps-classic-board-enable-roadmaps` — Enable timeline
- `jsw-roadmaps-cmp-enable-child-issue-planning` — Include child-level work items
- `jsw-roadmaps-prefer-child-issue-date-planning` — Prefer child issue start/due-date planning

The importer reads each property back after writing it and records the result in `board_timeline_verification.json`.

### Important: modern space List columns/order

The modern Jira **space List** is separate from saved-filter columns and Scrum/Kanban board columns. When the exporter cannot obtain a project-specific saved-view model, v3.8 uses Jira's supported authenticated-user default columns as the reproducible fallback: it maps the source field IDs to destination IDs, writes the exact order through `PUT /rest/api/3/user/columns`, and reads it back for verification. The report clearly marks this as user-scoped rather than claiming a public per-space saved-view write API exists.

## Source export requirement

Use a **Jira Source-Site Exporter v1.6 or newer** ZIP when you want Epics and board Timeline settings replicated. v1.4+ remains sufficient for work-type icons:

```powershell
.\run_exporter_windows.bat --project TES --output "D:\Jira Exports"
```

A v1.6 export includes the selected project's work-type avatar image files plus the Epic and board Timeline data consumed by v3.8. Older v1.4/v1.5 exports can still clone configuration and icons, but they do not contain the v1.6 Epic payload. Older v1.3 exports can still repair workflow and screen associations, but cannot faithfully transfer custom icons because they contain only the source-site `avatarId`.

## What v3.6 fixes

### Story Points duplicate prevention

When a brand-new Scrum project is created, Jira may materialize the company-managed `Story Points` field as part of template provisioning. v3.6 refreshes destination field metadata after project creation and immediately before creating any missing custom field, preventing a stale pre-project snapshot from causing a second `Story Points` Number field to be created. Existing global duplicates from older importer runs are not deleted automatically.


### Automatic Scrum-template board cleanup

Creating a new company-managed Scrum project through Jira automatically creates a project board such as `NEW board`. If that board is **not present in the source export**, v3.6 removes only that known template-generated board before creating/reusing the exported source boards. This also repairs projects created by older importer versions when you rerun with `--reuse-project`.

The cleanup is deliberately narrow: the importer will **not** delete arbitrary destination-only boards. It only deletes the board whose name exactly matches `<TARGET_PROJECT_KEY> board`, whose location is the target project, and whose name is absent from the source board set. The result is written to `board_cleanup_verification.json`.

### Renamed/custom work types such as `Planned Task`

If the source project uses `Planned Task` instead of Jira's normal Story work type, v3.6 creates/reuses `Planned Task`, adds it to the destination project's issue-type scheme, preserves the source scheme default, and removes destination-template extras such as Story from that project scheme when Jira permits it.

For safety, the importer does **not** globally rename Jira's destination Story work type, because a global rename can affect other projects on the destination site.

When a v1.4 export includes the custom avatar image, v3.6 first matches source icons against Jira's destination system-avatar catalogue and reuses the native Jira icon when possible. Only genuinely custom icons are uploaded, using the full exported image crop so they are not zoomed or distorted.

### Workflow scheme synchronization

Existing same-name workflow schemes are no longer treated as automatically correct. v3.6 compares the source mapping with the destination and synchronizes the default workflow plus per-work-type mappings, including mappings such as:

- Epic -> Epic workflow
- Planned Task -> Planned Task workflow
- Task -> Task workflow
- Sub-task -> Sub-task workflow

If the destination workflow scheme is active, v3.6 creates/reuses an explicit draft, writes every source work-type mapping through Jira's draft mapping endpoint, verifies the draft before publish, follows Jira's asynchronous publish task to completion, and then re-reads the active scheme. A missing Epic mapping is now treated as a failed import rather than a warning that can be overlooked.

### Screen and issue-type-screen-scheme synchronization

Same-name screen schemes and issue-type screen schemes are synchronized rather than merely reused. v3.6:

- adds the source screen fields/tabs to the mapped destination screens;
- updates each screen scheme so its default/create/edit/view operation points to the cloned destination screens;
- restores the default issue-type-screen-scheme mapping;
- restores explicit mappings such as Epic -> Epic Screen Scheme;
- removes stale project-scheme mappings when needed.

This is the part that ensures the create dialog for Task and Planned Task uses the source default screen containing the required source fields.

## Existing destination project

Dry run:

```powershell
.\run_importer_windows.bat "D:\Jira Exports\jira_source_export_<site>_<timestamp>.zip" --target-project TES --reuse-project
```

Apply:

```powershell
.\run_importer_windows.bat "D:\Jira Exports\jira_source_export_<site>_<timestamp>.zip" --target-project TES --reuse-project --apply
```

Type:

```text
APPLY TES
```

## New destination project

```powershell
.\run_importer_windows.bat "D:\Jira Exports\jira_source_export_<site>_<timestamp>.zip" --target-project NEW --target-name "My Project" --apply
```

## Priority replication

The default mode is:

```text
--priority-mode replicate
```

It reproduces renamed priority names, descriptions, colors, icons, scheme order, scheme default, and project association. When the source uses Jira's uneditable default priority scheme, it creates a project-specific clone such as `TES: Default priority scheme`.

The target project's scheme default is always copied. To also change Jira's site-wide default priority, add:

```text
--sync-global-priority-default
```

See `PRIORITY_REPLICATION.md` for details.

## Field model selection

Automatic detection is the default:

```text
--field-model auto
```

Troubleshooting overrides:

```text
--field-model legacy
--field-model new
```

## Saved-filter columns

The importer preserves the exact filter column order and remaps custom-field IDs before applying it. It then reads the columns back from Jira and verifies the order. See `FILTER_COLUMNS.md`.

## Main reports

- `actions.csv`
- `id_map.json`
- `priority_replication.json`
- `priority_scheme_verification.json`
- `filter_column_verification.json`
- `default_value_results.json`
- `field_model_detection.json`
- `field_scheme_verification.json`
- `workflow_validation.json`
- `issue_type_scheme_verification.json`
- `issue_type_screen_scheme_verification.json`
- `workflow_scheme_verification.json`
- `board_configuration_manual.json`
- `manual_actions.md`
- `summary.json`

## Safety

- Dry-run by default
- Writes only with `--apply`
- Never globally deletes Jira objects
- May remove stale work-type membership or stale mappings from the target project's schemes so they match the source
- Reuses compatible destination objects and synchronizes their project associations/mappings
- Requires explicit `APPLY <project-key>` confirmation
- Does not save the API token

Priority names are global Jira configuration. In `replicate` mode, compatible built-in priorities may be renamed to match the source. Use `--priority-mode create-only` when the destination contains other projects whose existing priority names must not change.

## Continuing after an earlier partial import

If an earlier importer run already created the destination project, rerun v3.6 with `--reuse-project`. Do not pass `--target-name` again.

```powershell
.\run_importer_windows.bat "D:\path\to\jira_source_export.zip" --target-project NEW --reuse-project --apply
```

v3.6 is intentionally rerunnable: it synchronizes reused issue-type schemes, screen schemes, issue-type screen schemes, and workflow schemes instead of assuming an existing same-name scheme is already correct.


## v3.10 All Work/List columns

See `V310_ALL_WORK_LIST_COLUMNS.md` for the scope split and verification behavior.
