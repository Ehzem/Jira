# Dual Jira field-model support

Jira Cloud is currently in a transition between two field-management models:

1. **Legacy model** — Field Configurations + Field Configuration Schemes.
2. **New model** — unified Field Schemes.

This importer uses the source TES field behavior as a canonical model and renders it into whichever model the destination accepts.

## Automatic behavior

The default is:

```text
--field-model auto
```

The importer probes both API families after the destination project exists:

- `/rest/api/3/config/fieldschemes`
- `/rest/api/3/fieldconfiguration`
- `/rest/api/3/fieldconfigurationscheme`

It then:

- creates a unified Field Scheme when the destination uses the new model;
- creates the source Field Configurations and Field Configuration Scheme when the destination uses the legacy model;
- performs one guarded fallback when Jira explicitly rejects the selected model and tells the importer to use the other one;
- records the decision in `field_model_detection.json` and `summary.json`.

## Source behavior preserved

The current embedded source export uses the legacy model. The importer preserves its behavior in either destination model, including:

- fields available for each work type;
- hidden versus visible behavior;
- required versus optional behavior;
- field descriptions;
- supported renderer settings;
- work-type-specific differences;
- project association.

For a new-model destination, hidden fields become fields that are not associated with that work type. Required and optional behavior becomes Field Scheme parameters and work-type overrides.

## Optional override

Automatic detection is recommended. For troubleshooting only:

```powershell
.\run_importer_windows.bat --target-project TES --reuse-project --field-model new --apply
```

or:

```powershell
.\run_importer_windows.bat --target-project TES --reuse-project --field-model legacy --apply
```

When a forced model is rejected, the importer stops rather than silently switching models. Remove the override or use `--field-model auto`.
