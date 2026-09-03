# v3.13 - All Work account-target fix

The source export already contains the full authenticated user's 19-column Jira **My Defaults** order. In the source UI the first visible columns are `Work`, `Status`, `Parent`; more columns continue horizontally.

Previous importers wrote `PUT /rest/api/3/user/columns` without `accountId`. Jira therefore updated the default columns of the account used by the importer. If the API credentials belong to a different admin account from the source/browser account, the browser account kept using destination system defaults such as `Work`, `Assignee`, `Reporter`, etc.

v3.13 reads `data/site/authenticated_account.json`, targets that exported Atlassian `accountId` explicitly on the destination, writes the complete exported My Defaults order, and reads the same account back for exact verification.

No `--all-work-visible-columns` override is required for this source export.
