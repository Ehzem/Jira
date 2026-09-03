# Story Points duplicate-field fix (v3.6)

## Root cause
The importer preflight read `/rest/api/3/field/search` before creating the destination Scrum project. Jira can materialize the company-managed `Story Points` custom field while creating the Scrum project. The old importer continued using the stale pre-project field snapshot, failed to see the newly available field, and could POST a second Number field also named `Story Points`.

## Fix
v3.6 refreshes destination field metadata immediately after project creation and again before any custom-field create operation when no compatible field was found. It retries briefly to cover Jira provisioning latency.

This makes the source `Story Points` field reuse the existing destination `Story Points` field rather than creating a duplicate.

## Existing duplicates
v3.6 does not automatically delete existing global custom fields created by older importer runs. Custom fields are site-wide configuration and may already be referenced elsewhere. Remove an older duplicate only after identifying its field ID and confirming it is unused.
