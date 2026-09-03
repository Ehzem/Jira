# v1.9 All Work / List / filter-column capture

Jira has several different column systems. v1.9 exports them separately:

- `data/list_views/user_default_columns.json`: supported authenticated-user defaults.
- `data/list_views/system_default_columns.json`: supported system defaults when accessible.
- `data/all_work/current_fields.json`: read-only CSV current-fields probe for All Work/current search.
- `data/filters/columns/<id>.json`: explicit saved-filter column layout returned by Jira.
- `data/filters/current_fields/<id>.json`: CSV current-fields probe for each saved filter.
- `data/filters/effective_columns/<id>.json`: best effective ordered filter layout.
- `data/projects/<KEY>/list_view/current_fields.json`: project-JQL current-fields probe.
- `data/projects/<KEY>/list_view/column_capture.json`: resolved project List evidence and whether exact project-specific state was proven.

The modern Jira space List can also use a saved-view model. Atlassian documents the UI feature, but a public REST API for writing that saved-view model is not currently documented. v1.9 therefore does not claim an exact saved-view capture unless it finds project-scoped view state in the exported browser/project evidence.
