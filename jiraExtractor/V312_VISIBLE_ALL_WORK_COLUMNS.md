# v3.12 visible All work items override

Jira Cloud's modern **All work items** page can display a column selection that does not match either:

- `GET /rest/api/3/user/columns` (My defaults), or
- CSV `current-fields`.

v3.12 adds an explicit operator override for that case.

For a source screen that visibly contains only **Work | Status | Parent**:

```powershell
python .\jira_destination_importer.py source_export.zip --source-project TES --target-project TES --reuse-project --all-work-visible-columns "work,status,parent" --all-work-column-scope user --apply
```

`work` is Jira's fixed composite column. The importer expands it to the REST-compatible fields `issuetype`, `issuekey`, and `summary`, followed by `status` and `parent`.

If the destination browser is explicitly using the **System** tab rather than **My defaults**, use `--all-work-column-scope both`. Be aware that `system`/`both` changes the site-wide issue navigator defaults for users who have not set their own defaults.
