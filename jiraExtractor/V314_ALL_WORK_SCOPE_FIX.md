# v3.14 — All Work visible scope fix

## Problem in v3.13

A normal importer run always did this internally:

```python
scope = self.all_work_column_scope if self.all_work_visible_columns else "user"
```

Therefore `--all-work-column-scope both` was silently ignored unless an explicit
`--all-work-visible-columns` override was also supplied. The destination could
continue to display System columns (`Work -> Assignee -> Reporter ...`) even when
My Defaults had been restored and verified.

## v3.14 behavior

- `--all-work-column-scope` is honored on every run.
- Default scope is now `both` for site-clone fidelity.
- The full exported source My Defaults layout is mapped to destination field IDs.
- The same requested order is written to both:
  - `/rest/api/3/user/columns?accountId=...`
  - `/rest/api/3/settings/columns`
- Both endpoints are read back and independently verified.

This intentionally changes the destination site's System default columns when the
default `both` scope is used. Use `--all-work-column-scope user` if you do not want
that site-wide change.
