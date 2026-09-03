# v3.9 Filter and Board Column Fix

This release addresses the failures seen in the FYP import log.

## Filter 02

The source JQL references `cf[10045]` (Defer Until). v3.8 performed repeated substitutions and could turn that into `cf[10052]`. v3.9 maps numeric custom-field references in one pass.

## Board columns

The source export already contains `To Do -> In Progress -> UNDER REVIEW -> Done` for both boards, including status mappings. v3.8 only wrote those settings to a manual report. v3.9 maps each source status by source mapping/name, writes the complete board layout through Jira's Board Settings GreenHopper endpoint, and verifies the result with `GET /rest/agile/1.0/board/{boardId}/configuration`.

Because the write endpoint is an internal Jira endpoint rather than a supported public API, the importer records a warning and leaves the board untouched if the endpoint is unavailable. It also refuses to send a partial mapping.

## List columns

Jira documents the user-default column setter as multipart form data. v3.8 sent URL-encoded form data and received HTTP 415. v3.9 sends multipart form data and verifies the returned order.
