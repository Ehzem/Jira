# Legacy Field Configuration compatibility fix

Jira creates a new legacy Field Configuration from the destination site's own
Default Field Configuration. System fields can differ between sites. For
example, a source may expose `dataclassification`, while the destination does
not.

Version 2.7 reads the field items actually present in every destination
configuration before sending updates. Source-only items are skipped and written
to the reports instead of causing Jira to reject the full request.

On a rerun, non-default configurations with the source names are synchronized.
The shared Jira Default Field Configuration is never overwritten.

Review these files after the run:

- `legacy_field_configuration_verification.json`
- `actions.csv`
- `manual_actions.md`
