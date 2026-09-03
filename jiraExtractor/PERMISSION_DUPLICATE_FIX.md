# Permission scheme duplicate-grant fix (v3.4)

Jira Cloud can seed a newly-created permission scheme with system-managed grants, including grants for the `atlassian-addons-project-access` project role. Recreating the source scheme grant-by-grant therefore returned HTTP 400 (`... already exists`) for grants that Jira had already inserted correctly.

v3.4 reads the live destination scheme before writing grants. Exact existing permission/holder pairs are treated as successful reuse, only missing grants are created, and any duplicate-error response is re-read and accepted only if the exact grant is verifiably present.

The importer still performs an exact final grant comparison before associating the scheme to the destination project.
