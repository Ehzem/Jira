# Jira space List-view capture in v1.8

Jira has several unrelated column systems. v1.8 keeps them separate:

1. **Saved-filter columns** — `GET /rest/api/3/filter/{id}/columns`
2. **Scrum/Kanban board workflow columns** — Agile board configuration
3. **Authenticated-user default issue-table columns** — `GET /rest/api/3/user/columns`
4. **System default issue-table columns** — `GET /rest/api/3/settings/columns` when permitted
5. **Project/space List browser state** — Firefox localStorage, IndexedDB, and Jira-scoped sessionStorage

For each selected project, inspect:

```text
data/projects/<KEY>/list_view/column_capture.json
```

The key field is:

```json
"exactProjectListColumnOrderCaptured": true
```

If it is `false`, the exporter has retained the best available supported fallback but has **not** proven that the exact project-specific List/saved-view order was available.

Before exporting, open the desired List in Firefox, arrange the columns, and leave the tab open. Running on the same Firefox/OS profile gives the browser scanner the best chance to find personal state.
