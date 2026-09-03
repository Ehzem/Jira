# Saved-filter creation fix — v2.8

The source export contained all eight filters. The missing destination filter was:

- `02 - Review Deferred Inbox`

Its source JQL was:

```jql
project = TES
AND issuetype = Task
AND status = "08 - DEFER BACKLOG UNPROCESSED"
AND "Defer Until" = startOfDay()
```

The destination rejected it because the newly-created `Defer Until` custom field had not yet been explicitly associated with the destination project/field configuration. Jira therefore could not resolve the field by display name and also omitted it from saved-filter columns.

Version 2.8 fixes both parts:

1. After selecting and associating either the legacy Field Configuration Scheme or new Field Scheme, the importer explicitly associates all mapped custom fields with the target project using `/rest/api/3/field/association`.
2. On legacy destinations, field-configuration item synchronization is deferred until after that association.
3. Saved-filter JQL custom-field names and source IDs are rewritten to destination aliases such as `cf[10079]`.
4. The importer refreshes field navigability before applying saved-filter columns.

The run produces `custom_field_association_verification.json` in addition to the existing filter-column report.
