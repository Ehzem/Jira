# Jira guest-role permission verification fix (v3.5)

Jira Cloud may add grants for the system project role `jira-guest-member` when a permission scheme is associated with a company-managed project. In the reported case the unassociated clone matched the source, then association added ten guest-role grants and the v3.4 exact verifier aborted.

v3.5 separates permission differences into three groups:

1. **Missing source grants** — fatal.
2. **Non-system destination extras** — fatal.
3. **Destination-only `jira-guest-member` grants** — recorded as Jira-managed platform policy and allowed.

This is deliberately narrow. No other role, group, application role, user, or permission is ignored. The report records both `exact` and `sourceCompatible` so the distinction remains visible.

The importer does not delete Jira's guest-role grants. Guest access is a Jira platform feature with platform-controlled permission behavior, so removing those entries merely to force byte-for-byte equality would be unsafe.
