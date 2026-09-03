# v3.17.0-tes

- Fixed v3.16 transport negotiation aborting on HTTP 500 from the invalid wildcard Content-Type attempt.
- Removed `Content-Type: */*`.
- Added a real `requests` multipart/form-data request with repeated `columns` parts and `X-Atlassian-Token: no-check`.
- Added independent fallbacks for `columns[]` multipart, repeated URL-encoded fields, JSON, query parameters, and REST v2.
- Keeps exact GET read-back verification before reporting All Work column success.

# v3.15.0-tes

- Fixed All Work/My Defaults and System Defaults writes failing with HTTP 415.
- Replaced hand-built multipart/form-data with repeated application/x-www-form-urlencoded `columns` fields, matching Atlassian's curl examples.
- Corrected the runtime version banner to `3.15.0-tes`.
- Retains exact GET read-back verification for both user and system column scopes.

# v3.14

- Fixed All Work scope handling: v3.13 silently forced normal runs to `user` scope.
- `--all-work-column-scope` now works without requiring a visible-column override.
- Default scope changed to `both` so My Defaults and System defaults receive the full exported source All Work order and are both verified.

# v3.13.0 - All Work account targeting

- Restores the full exported All Work/My Defaults column order to the same Atlassian account that authenticated the source export.
- Passes the source accountId explicitly to PUT/GET `/rest/api/3/user/columns`, avoiding accidental configuration of a different destination admin account.
- Keeps all exported columns; it does not infer the layout from the visible portion of a screenshot.

# v3.12.0-tes

- Added `--all-work-visible-columns` to explicitly reproduce the modern All work items UI when Jira's REST user defaults and CSV current-fields disagree with what the browser visibly shows.
- Added `--all-work-column-scope user|system|both`; `system` uses `/rest/api/3/settings/columns` and is opt-in because it changes site-wide defaults.
- `work` is expanded to `issuetype`, `issuekey`, and `summary` for REST write-back.

# Changelog

## 3.11.0

- Fixes saved-filter column semantics in **All Work**. Filters that had an explicit source `/filter/{id}/columns` layout are recreated exactly; filters that returned Jira 404 are now reset with `DELETE /filter/{id}/columns` so they continue to inherit **My Defaults** instead of being given a fabricated explicit layout.
- Fixes the v3.10 All Work/My Defaults precedence bug: `data/list_views/user_default_columns.json` is now authoritative for `/rest/api/3/user/columns`; the CSV `current_fields` probe remains evidence only and is used only as a fallback.
- Adds verification that inherited filters really return Jira 404 after reset, which is Jira's representation of “no filter-specific layout.”
- Keeps explicit filter-column write/read-back verification and custom-field ID remapping unchanged.

## 3.10.0

- Separates **All Work / My-default columns** from **space List/saved-view columns** instead of conflating them.
- Reads v1.9 `data/all_work/current_fields.json`, per-filter current-fields probes, and project List current-fields evidence.
- Restores All Work/My-default column order through Jira's supported user-column endpoint and verifies read-back.
- Uses v1.9 effective/current-fields saved-filter layouts when a filter has no explicit `/filter/{id}/columns` layout.
- Expands the merged List's composite `Work` column to `issuetype`, `issuekey`, and `summary` when writing through Jira field-column APIs.
- If a project-specific saved List view differs from user defaults, reports the public-API limitation instead of silently overwriting All Work defaults.
- Retains v3.9 Filter 02 atomic custom-field JQL remapping, board-column write/verification, Timeline restore, and field-association batching.

# v3.9.0

- Fixed Filter 02 / adjacent custom-field JQL remapping: custom field references are now translated atomically, so `cf[10045]` cannot cascade through `cf[10046]`, `cf[10047]`, etc.
- Added automatic board column restoration using Jira's internal Board Settings (`/rest/greenhopper/1.0/rapidviewconfig/columns`) endpoint. Column names, exact order, and status mappings are translated to destination status IDs and read-back verified through the public board configuration API.
- Added `board_column_verification.json`. The importer refuses a partial/destructive board-column write if any source status cannot be mapped.
- Fixed List/default column import HTTP 415 by sending Jira's documented `multipart/form-data` body for `/rest/api/3/user/columns`.
- Fixed custom-field project association for sites with more than 50 mapped fields by sending batches of at most 50.

# v3.8.0-tes

- Restores saved-filter columns using Atlassian's documented repeated form-field transport, with read-back verification.
- Uses v1.8 effective filter-column layouts; remains compatible with v1.7 by falling back to exported user-default columns when a filter has no explicit layout.
- Treats custom fields used only by filter JQL, filter columns, or List columns as required fields so they are cloned/mapped before filters are created.
- Retries filter creation after refreshing custom-field mappings/JQL indexing; fixes filters such as `02 - Review Deferred Inbox` that reference `cf[...]`.
- Applies the source List/user-default column order to the authenticated destination user's supported Jira default columns and verifies the exact returned order.

# Changelog

## 3.7.0-tes
- Imports Epic work items exported by Source Exporter v1.6+, including mapped components, fix versions, priority, portable create-screen fields, custom-field option IDs, and source status where a valid destination transition is available.
- Adds adaptive Epic creation: Jira-rejected non-core optional fields are removed and retried instead of aborting the whole import.
- Adds `issue` and `epic` mappings to `id_map.json` and writes `epic_import_results.json`.
- Restores board Timeline settings from exported `jsw-roadmaps-*` board properties using the supported Jira Software board-property API, then verifies each value by reading it back.
- Adds `board_timeline_verification.json`.
- Distinguishes modern space List columns/order from saved-filter columns and board workflow columns. v3.7 reports v1.6 browser-only List candidates as non-replayable instead of silently treating them as imported.
- Adds `list_view_import_report.json` and validator checks for Epic/Timeline/List-view capture.

## 3.6.0-tes
- Refresh destination fields after project creation so Jira-template-created Story Points is visible before field cloning.
- Re-check/retry destination field metadata immediately before creating a custom field, preventing duplicate Story Points fields caused by Jira provisioning latency.
- Preserve existing duplicates for manual cleanup rather than deleting site-wide custom fields automatically.

# Changelog

## 3.5.0-tes

- Fixes permission-scheme verification on Jira sites with Guest access. Jira can inject destination-only `jira-guest-member` project-role grants when a permission scheme is associated with a company-managed project.
- Treats only those `jira-guest-member` extras as Jira-managed platform policy; all source grants must still be present and every other destination-only grant remains a fatal mismatch.
- Reuses an existing importer-created permission clone when it contains the complete source grant set plus only Jira-managed guest-role grants, preventing endless clone creation on reruns.
- Reports `jiraManagedExtra`, `exact`, and `sourceCompatible` separately in `permission_scheme_verification.json`.
- Keeps the destination guest grants intact rather than deleting Jira-managed access controls simply to force byte-for-byte equality.
- Includes all v3.4 duplicate-grant handling, v3.3 board cleanup, v3.2 permission cloning, v3.1 workflow status migration, and v3.0 workflow/icon fixes.

## 3.4.0-tes

- Removes Jira's automatically created Scrum-template board (for example `NEW board`) when that board is absent from the source export.
- The cleanup works both on a newly created destination project and on repair reruns using `--reuse-project`.
- Deletion is tightly guarded: only a board named exactly `<TARGET_PROJECT_KEY> board` located in the target project is eligible, and it is preserved if the source export contains the same board name.
- Arbitrary destination-only boards are never deleted.
- Uses Jira Software Cloud's supported `DELETE /rest/agile/1.0/board/{boardId}` endpoint.
- Adds `board_cleanup_verification.json` to the import report.

## 3.2.0-tes

- Fixed permission schemes not being faithfully transferred when Jira already had a same-name scheme such as `Default software scheme`.
- The importer now compares the source and destination permission grants semantically instead of assuming a same-name scheme is compatible.
- If the same-name destination scheme differs, it is preserved and a dedicated `TES: <source scheme>` clone is created rather than modifying a potentially shared Jira scheme.
- Recreates every source permission grant and maps project-role IDs by role name, application roles by product key, and custom-field holders through the destination field ID map.
- Verifies the cloned scheme grant-for-grant before association and verifies the target project is actually assigned to that exact scheme afterward.
- Adds `permission_scheme_verification.json` to the import report.
- Keeps the importer non-destructive: conflicting shared/default permission schemes are never overwritten or deleted.

## 3.1.0-tes

- Fixed active workflow-scheme publishing when Jira requires status migrations before a work type can move to a different workflow.
- The importer now calculates old-status -> target-status mappings from the live destination workflows and sends them in `statusMappings` when publishing a classic workflow-scheme draft.
- Added a `validateOnly=true` publish check before the asynchronous publish starts.
- Publish now preserves Jira's HTTP 303 response instead of following the redirect implicitly, then follows the returned task `Location` until completion.
- For the TES source Epic workflow, statuses that do not exist in the two-status Epic workflow are mapped to a semantically compatible target status; in-progress statuses fall back to a non-final TODO/initial status rather than DONE.
- Added fallback workflow-status discovery through the current `/rest/api/3/workflows/search` API when the deprecated classic workflow search endpoint is unavailable.

## 3.0.0-tes

- Fixed Epic workflow mappings that were present in the source but missing from the active destination workflow scheme after v2.9.
- Repairs workflow schemes after project association through explicit draft issue-type mappings.
- Verifies the draft contains every source mapping before publishing.
- Follows Jira's asynchronous workflow-scheme publish task through its `Location` URL and fails clearly if the task fails or times out.
- Verifies the final active workflow scheme and refuses to report success while a mapping such as Epic is still missing.
- Fixes distorted Task/Planned Task icons from v2.9.
- Reuses Jira system issue-type avatars directly when the source icon matches a destination system avatar.
- Uses the documented universal-avatar upload endpoint for genuinely custom icons and sends the full centered crop rather than a hard-coded 48-pixel crop.
- Existing v1.4 source exports can be reused; no new source export is required for this v3.0 repair.

## 2.9.0-tes

- Added exact custom work-type avatar replication when used with Source Exporter v1.4.
- Synchronizes reused issue-type schemes instead of accepting same-name schemes unchanged.
- Preserves the source issue-type-scheme default and removes stale destination-template membership such as Story from the target project scheme when Jira permits it.
- Synchronizes reused screen schemes so create/edit/view/default operations point to the cloned destination screens.
- Synchronizes default and explicit issue-type-screen-scheme mappings, including the Epic screen mapping.
- Synchronizes reused workflow-scheme default/per-work-type mappings.
- Updates active workflow schemes through Jira's draft/publish flow and refuses to guess status migrations when existing issues require them.
- Expanded verification artifacts for issue-type, screen-routing, and active workflow-scheme associations.

## 2.7.0

- Fixed legacy Field Configuration imports failing when a source-only Jira system field, such as `dataclassification`, is absent on the destination.
- Reads each destination field configuration's actual item inventory before updating it.
- Skips unavailable field IDs with a warning instead of rejecting the whole configuration.
- Isolates per-field update failures so one unsupported renderer or field cannot stop the import.
- Synchronizes reused non-default field configurations, allowing safe continuation after a partial run.
- Keeps Jira's shared Default Field Configuration unchanged.
- Added `legacy_field_configuration_verification.json`.
- Fixed issue type scheme creation to read Jira's `issueTypeSchemeId` response and associate the new scheme immediately.

## 2.6.0

- Added exact saved-filter column replication and ordering.
- Correctly extracts field IDs from Jira's exported `{label, value}` column objects.
- Remaps source custom-field IDs to destination IDs while preserving position.
- Rejects unmapped or explicitly non-navigable fields with a clear manual-action entry.
- Tries JSON first and form-encoded column parameters as a compatibility fallback.
- Reads every destination filter back and verifies the exact final order.
- Adds `filter_column_verification.json` and `FILTER_COLUMNS.md`.
- Keeps priority replication, dual field-model support, and default-value restoration from v2.5.

## 2.5.0

- Added priority and priority-scheme replication.
- Matches renamed Jira default priorities using their built-in icon and status color.
- Creates missing priorities and maps source IDs to destination IDs.
- Creates a project-specific clone when the source uses Jira's uneditable default priority scheme.
- Preserves the scheme priority order and its default priority.
- Associates the cloned priority scheme with the destination project.
- Adds migration mappings for existing destination work items whose priority is outside the source scheme.
- Adds `priority_replication.json` and `priority_scheme_verification.json` reports.
- Adds `--priority-mode` and `--sync-global-priority-default` options.
- Keeps dual legacy/new Field Scheme support and custom-field default-value replication from v2.4.

## 2.4.0

- Added custom-field default-value restoration.
- Continued support for both legacy Field Configurations and new Field Schemes.

## 2.8.0-tes

- Fixed `02 - Review Deferred Inbox` being skipped when Jira could not resolve the newly-created `Defer Until` field.
- Explicitly associates mapped custom fields with the destination project.
- Defers legacy field-item synchronization until after project/field association.
- Rewrites custom-field references in filter JQL to destination `cf[ID]` aliases.
- Refreshes navigability before setting ordered filter columns.
- Adds `custom_field_association_verification.json`.

## 3.4.0
- Permission-scheme grant creation is now idempotent.
- Jira-created/system-managed grants already present in a new permission scheme (especially `atlassian-addons-project-access`) are detected and reused rather than POSTed again.
- An HTTP 400 duplicate response is re-checked against the live scheme and accepted only when the exact semantic grant is confirmed present.
- Exact source-vs-destination permission grant verification remains mandatory before project association.

## 3.16.0-tes
- Added transport negotiation for All Work/My Defaults and System Defaults column writes after Jira Cloud returned HTTP 415 for both multipart and urlencoded requests on the destination tenant.
- Tries Atlassian's current Postman raw-JSON `Content-Type: */*` representation first, followed by JSON, native multipart, urlencoded, and REST v2 fallbacks.
- Logs the accepted transport and retains exact GET read-back verification.
