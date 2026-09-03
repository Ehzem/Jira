# Custom-field default values

Version 2.4 restores default values stored on custom-field contexts. This is independent of whether the destination uses legacy Field Configurations or the newer Field Schemes model.

## Required source export

Use Jira Source-Site Exporter 1.2 or newer. Older exports contain contexts and options but do not contain default values.

The new exporter writes:

- `data/fields/default_values_grouped/<field-id>.json`
- `data/fields/default_values_legacy/<field-id>.json`

The grouped form is preferred because it can describe defaults by context and work type. The legacy form is kept as a compatibility fallback.

## ID remapping

Before setting a default, the importer remaps:

- Source context ID to destination context ID
- Source select-list option IDs to destination option IDs
- Source project ID to the destination project ID
- Source release/version IDs to destination release/version IDs

Text, date, date-time, number, URL and label values are copied directly. User/group/app-provided defaults can only be copied when Jira accepts the referenced destination identity or app field type.

## Work-type-specific defaults

Jira's currently documented write endpoint sets one default for the whole context. When a source context has different default values for different work types, the importer does not silently choose one. It writes the conflict to `default_value_results.json` and `manual_actions.md`.

## Existing destination contexts

Applying the importer sets the destination context default to the source value. This can affect every project/work type covered by a reused global context, so review the context scope before applying to a destination site that already contains unrelated projects.
