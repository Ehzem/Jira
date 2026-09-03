# Priority replication

Version 2.5 replicates the priority behavior of the source project.

It copies:

- Priority names, including renamed Jira defaults
- Descriptions
- Status colors
- Priority icons
- Priority order inside the project priority scheme
- The scheme's default priority
- The priority scheme association with the destination project

## How renamed default priorities are matched

The importer first looks for a destination priority with the same name. When the source renamed Jira's original priorities, it can also match the destination's original priority by its built-in icon and status color. It then updates that priority to the source name and metadata.

If no compatible priority exists, it creates one.

## Default priority scheme

Jira's built-in Default priority scheme cannot be edited directly. When the source project uses it, the importer creates a project-specific clone named like:

```text
TES: Default priority scheme
```

The cloned scheme contains the mapped priorities in source order, uses the same default priority, and is associated with the destination project.

## Existing work items

When associating an existing project, Jira may require mappings for priority values that are not in the cloned scheme. The importer maps those values to the source scheme's default priority so the association can complete safely.

## Site-wide default

The project scheme's default is always copied. Jira's separate site-wide default is not changed unless you add:

```text
--sync-global-priority-default
```

That flag affects the whole destination Jira site, not only the target project.

## Modes

Default:

```text
--priority-mode replicate
```

This can rename compatible built-in priorities and create missing priorities.

To avoid renaming destination priorities:

```text
--priority-mode create-only
```

To omit priority handling:

```text
--priority-mode skip
```
