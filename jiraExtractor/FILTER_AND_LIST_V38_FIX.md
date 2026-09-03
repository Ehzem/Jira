# v3.8 Filter + List fixes

This release addresses three failures found in the supplied source export:

1. Filter-specific column layouts existed for filters 01–05, but inherited/default layouts were represented as HTTP 404 by Jira and therefore looked missing. v1.8 exports an effective layout for every filter and v3.8 can also use the v1.7 user-default fallback.
2. `02 - Review Deferred Inbox` uses `cf[10045]` (`Defer Until`) in JQL. v3.8 now includes filter-only custom fields in field discovery, rewrites source custom-field IDs to destination IDs, validates/retries JQL after refreshing Jira field metadata, and then creates the filter.
3. The supplied v1.7 List capture is a user-default fallback (`exactProjectListColumnOrderCaptured=false`). v3.8 now applies that ordered list to the destination importing user's supported `/rest/api/3/user/columns` configuration and verifies it. This is user-scoped, not a claim that Jira exposes a public per-space saved-view write API.
