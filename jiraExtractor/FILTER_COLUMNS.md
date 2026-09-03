# Saved-filter columns and ordering

Version 2.6 copies the columns configured for every exported saved filter and preserves their source order.

## What is copied

For each filter, the importer:

1. Reads the source `data/filters/columns/<filter-id>.json` file.
2. Extracts each Jira field ID from the exported `{ "label", "value" }` objects.
3. Remaps source custom-field IDs to their destination custom-field IDs.
4. Preserves system-field IDs such as `issuekey`, `summary`, `status`, and `parent`.
5. Sends the complete ordered list to the destination filter.
6. Reads the destination columns back and compares the exact order.

The result is written to:

```text
filter_column_verification.json
```

Each filter reports the source order, mapped destination order, actual order returned by Jira, and whether the result was verified.

## Jira restrictions

Jira only accepts navigable fields as saved-filter columns. If a source field is missing, unmapped, or not navigable on the destination, the importer records it in `manual_actions.md` rather than silently replacing it.

The importer first uses Jira's JSON request format. If a destination Jira tenant rejects that representation, it automatically retries using form-encoded repeated `columns` parameters.

## No new source export required

Source Exporter v1.3 already captures saved-filter columns. You can reuse the same v1.3 export ZIP you created for priorities and default values.
