# v3.10 All Work and space List columns

The importer now treats these as separate scopes:

1. **Saved-filter columns** are restored per filter with `/rest/api/3/filter/{id}/columns`.
2. **All Work / My defaults** are restored for the importing Jira account with `/rest/api/3/user/columns` and read back for verification.
3. **Space List** is checked against the exported project List evidence. When the source List is using My defaults/current-fields and the order matches, the restored user defaults reproduce it. If the source is a distinct saved space view, v3.10 reports that separately rather than claiming Jira's public REST API can write that saved-view model.

Use exporter v1.9 for the strongest column evidence. v3.10 remains backward compatible with v1.8 exports.
