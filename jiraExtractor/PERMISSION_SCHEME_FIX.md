# Permission scheme replication in v3.2

Earlier importer builds reused a destination permission scheme whenever its name matched the source. This is unsafe for Jira's shared defaults: a destination `Default software scheme` can have different grants from the source, but overwriting it could affect other projects.

v3.2 uses this rule instead:

1. Read the permission scheme actually assigned to the source project, including expanded grants.
2. Compare grants semantically across sites (project roles by role name, application roles by product key, and custom-field holders through the field ID map).
3. If a same-name destination scheme is already an exact match, reuse it.
4. If the same-name scheme differs, leave it untouched and create a dedicated `TES: <source scheme name>` clone.
5. Recreate every grant in the clone.
6. Read the clone back and verify it grant-for-grant.
7. Associate the verified clone with the destination project.
8. Read the project's assigned scheme back and verify both the scheme ID and grants.

The import report writes `permission_scheme_verification.json` with the source/destination grant counts and any differences.

The importer remains non-destructive: it does not delete or overwrite a conflicting shared/default permission scheme.
