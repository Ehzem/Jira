# v3.15 - All Work HTTP 415 fix

The v3.14 run reached the correct All Work targets but Jira rejected both writes:

- `PUT /rest/api/3/user/columns` -> HTTP 415
- `PUT /rest/api/3/settings/columns` -> HTTP 415

v3.14 manually constructed a `multipart/form-data` request. Although the OpenAPI schema labels the request body as multipart/form-data, Atlassian's documented curl examples use repeated `-d columns=...` fields. Curl sends those as `application/x-www-form-urlencoded` unless multipart is explicitly requested.

v3.15 therefore sends the ordered repeated column fields as normal HTML form data:

`columns=issuetype&columns=issuekey&columns=summary&...`

The importer then GETs the same endpoint and verifies the returned order exactly. Both My Defaults and System Defaults remain supported through `--all-work-column-scope both`.

The package version string has also been corrected to `3.15.0-tes` so the console header identifies the code actually being run.
