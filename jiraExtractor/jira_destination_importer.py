#!/usr/bin/env python3
"""
Jira Cloud destination-site configuration importer.

Consumes a TES export created by Jira Source-Site Exporter v1.4+ and
recreates that company-managed project configuration on another Jira Cloud site.
A different compatible export ZIP can still be supplied explicitly.

Safety:
- Dry-run by default.
- Writes only when --apply is supplied.
- Never deletes Jira configuration objects except a Jira-template default board that can be safely
  identified as `<destination project key> board` and is absent from the source export.
- Reuses same-name compatible objects when possible.
- Records every action, mapping, warning and manual follow-up.

Python 3.10+
Dependency: requests
"""
from __future__ import annotations

import argparse
import copy
import csv
import getpass
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zipfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlparse, urlencode

import requests
from requests.auth import HTTPBasicAuth

APP_NAME = "Jira Destination-Site Importer"
APP_VERSION = "3.17.0-tes"
MAX_RETRIES = 5
PAGE_SIZE = 100


class JiraError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, message: str, body: str = "") -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {message}")
        self.method = method
        self.url = url
        self.status = status
        self.message = message
        self.body = body


class FieldModelMismatch(RuntimeError):
    """The selected destination field model cannot accept the requested operation."""

    def __init__(self, attempted_model: str, suggested_model: str, error: JiraError) -> None:
        super().__init__(
            f"Destination rejected the {attempted_model} field model and appears to require "
            f"{suggested_model}: HTTP {error.status} - {error.message}"
        )
        self.attempted_model = attempted_model
        self.suggested_model = suggested_model
        self.error = error


@dataclass
class Action:
    phase: str
    entity: str
    source: str
    destination: str
    action: str
    status: str
    note: str = ""


class JiraClient:
    def __init__(self, site: str, email: str, token: str) -> None:
        self.site = normalize_site(site)
        self.s = requests.Session()
        self.s.auth = HTTPBasicAuth(email, token)
        self.s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"jira-destination-importer/{APP_VERSION}",
        })

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                body: Any = None, form: Any = None, raw: bytes | None = None,
                headers: Mapping[str, str] | None = None,
                expected: Sequence[int] | None = None,
                return_meta: bool = False,
                allow_redirects: bool = True) -> Any:
        url = path if path.startswith("http") else urljoin(self.site + "/", path.lstrip("/"))
        expected = tuple(expected or range(200, 300))
        last: JiraError | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                request_headers = dict(headers or {})
                if form is not None:
                    request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                r = self.s.request(
                    method, url, params=params,
                    json=body if form is None and raw is None else None,
                    data=form if form is not None else raw,
                    headers=request_headers or None, timeout=75,
                    allow_redirects=allow_redirects,
                )
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise JiraError(method, url, 0, str(exc)) from exc
                time.sleep(min(2 ** attempt, 20))
                continue
            if r.status_code in expected:
                if not r.content or r.status_code == 204:
                    payload: Any = None
                else:
                    try:
                        payload = r.json()
                    except ValueError:
                        payload = r.text
                if return_meta:
                    return payload, dict(r.headers), r.status_code
                return payload
            msg = extract_error(r)
            last = JiraError(method, r.url, r.status_code, msg, r.text[:5000])
            if r.status_code != 429 and not (500 <= r.status_code < 600):
                raise last
            if attempt >= MAX_RETRIES:
                raise last
            retry = r.headers.get("Retry-After")
            try:
                delay = int(retry) if retry else min(2 ** attempt, 20)
            except ValueError:
                delay = min(2 ** attempt, 20)
            time.sleep(max(delay, 1))
        raise last or RuntimeError("Unexpected request failure")

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body=body)

    def post_meta(self, path: str, body: Any) -> tuple[Any, dict[str, str], int]:
        payload, headers, status = self.request("POST", path, body=body, return_meta=True)
        return payload, headers, status

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body=body)

    def put_form(self, path: str, form: Any) -> Any:
        return self.request("PUT", path, form=form)

    def put_column_values(self, path: str, columns: Sequence[str],
                          *, params: Mapping[str, Any] | None = None) -> tuple[Any, str]:
        """Set Jira issue-navigator columns and negotiate the request encoding.

        Jira Cloud documents these endpoints as repeated HTML form fields, but
        different Jira Cloud edge versions have rejected different encodings.
        v3.16 also had a negotiation bug: a HTTP 500 from the first experimental
        wildcard Content-Type attempt aborted the method before the documented
        multipart strategy was ever tried.

        v3.17 removes the invalid wildcard attempt and deliberately tries each
        representation independently.  The first strategy is a real requests
        multipart upload with repeated ``columns`` parts and X-Atlassian-Token,
        which is the closest wire equivalent to Atlassian's documented form-data
        contract.  Every failed representation is collected instead of aborting
        negotiation.  The caller still performs an exact GET read-back before
        reporting success.
        """
        values = [str(x) for x in columns if str(x).strip()]
        errors: list[str] = []

        candidate_paths = [path]
        if "/rest/api/3/" in path:
            candidate_paths.append(path.replace("/rest/api/3/", "/rest/api/2/", 1))

        def parse_success(r: requests.Response) -> Any:
            if not r.content or r.status_code == 204:
                return None
            try:
                return r.json()
            except ValueError:
                return r.text

        def fail(name: str, candidate_path: str, r: requests.Response) -> None:
            errors.append(
                f"{name} via {candidate_path}: HTTP {r.status_code}: {extract_error(r)}"
            )

        def url_for(candidate_path: str) -> str:
            return candidate_path if candidate_path.startswith("http") else urljoin(
                self.site + "/", candidate_path.lstrip("/")
            )

        # Do one request per transport.  A 500 can be representation-specific
        # (as the user's v3.16 log proved), so the generic request() retry/raise
        # policy must not prevent later transports from being tested.
        for candidate_path in candidate_paths:
            url = url_for(candidate_path)

            # 1) Documented multipart form-data.  Let requests generate the
            # boundary itself; explicitly remove the Session's application/json
            # Content-Type so requests can set the correct multipart header.
            try:
                r = self.s.request(
                    "PUT", url, params=params,
                    files=[("columns", (None, value)) for value in values],
                    headers={
                        "Accept": "application/json",
                        "Content-Type": None,
                        "X-Atlassian-Token": "no-check",
                    },
                    timeout=75,
                )
                if 200 <= r.status_code < 300:
                    return parse_success(r), f"requests-multipart-columns via {candidate_path}"
                fail("requests-multipart-columns", candidate_path, r)
            except requests.RequestException as exc:
                errors.append(f"requests-multipart-columns via {candidate_path}: {exc}")

            # 2) Same multipart encoding using columns[] for Jira edge builds
            # whose form binder expects an explicit array-shaped field name.
            try:
                r = self.s.request(
                    "PUT", url, params=params,
                    files=[("columns[]", (None, value)) for value in values],
                    headers={
                        "Accept": "application/json",
                        "Content-Type": None,
                        "X-Atlassian-Token": "no-check",
                    },
                    timeout=75,
                )
                if 200 <= r.status_code < 300:
                    return parse_success(r), f"requests-multipart-columns-array via {candidate_path}"
                fail("requests-multipart-columns-array", candidate_path, r)
            except requests.RequestException as exc:
                errors.append(f"requests-multipart-columns-array via {candidate_path}: {exc}")

            # 3) Exact curl -d equivalent from Atlassian's prose docs.  Passing
            # a list of tuples preserves duplicate column names and their order.
            try:
                r = self.s.request(
                    "PUT", url, params=params,
                    data=[("columns", value) for value in values],
                    headers={"Accept": "application/json", "Content-Type": None},
                    timeout=75,
                )
                if 200 <= r.status_code < 300:
                    return parse_success(r), f"requests-urlencoded-columns via {candidate_path}"
                fail("requests-urlencoded-columns", candidate_path, r)
            except requests.RequestException as exc:
                errors.append(f"requests-urlencoded-columns via {candidate_path}: {exc}")

            # 4) JSON object fallback.  This is not the prose contract, but some
            # Atlassian generated examples expose this shape.
            try:
                r = self.s.request(
                    "PUT", url, params=params, json={"columns": values},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=75,
                )
                if 200 <= r.status_code < 300:
                    return parse_success(r), f"application-json-object via {candidate_path}"
                fail("application-json-object", candidate_path, r)
            except requests.RequestException as exc:
                errors.append(f"application-json-object via {candidate_path}: {exc}")

            # 5) Last-resort bodyless query representation.  It is not documented
            # for this endpoint, but is safe to probe because the subsequent GET
            # read-back must match exactly before the importer reports success.
            try:
                query_items: list[tuple[str, Any]] = []
                if params:
                    query_items.extend((str(k), v) for k, v in params.items())
                query_items.extend(("columns", value) for value in values)
                r = self.s.request(
                    "PUT", url, params=query_items, data=b"",
                    headers={"Accept": "application/json", "Content-Type": None},
                    timeout=75,
                )
                if 200 <= r.status_code < 300:
                    return parse_success(r), f"query-columns-bodyless via {candidate_path}"
                fail("query-columns-bodyless", candidate_path, r)
            except requests.RequestException as exc:
                errors.append(f"query-columns-bodyless via {candidate_path}: {exc}")

        raise JiraError(
            "PUT",
            path,
            415,
            "All supported column-write transports were rejected. " + " | ".join(errors),
        )

    def get_bytes(self, path: str, *, params: Mapping[str, Any] | None = None,
                  accept: str = "application/octet-stream") -> tuple[bytes, str]:
        url = path if path.startswith("http") else urljoin(self.site + "/", path.lstrip("/"))
        last: JiraError | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self.s.get(url, params=params, headers={"Accept": accept}, timeout=75)
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise JiraError("GET", url, 0, str(exc)) from exc
                time.sleep(min(2 ** attempt, 20)); continue
            if 200 <= r.status_code < 300:
                return r.content, r.headers.get("Content-Type", "application/octet-stream")
            last = JiraError("GET", r.url, r.status_code, extract_error(r), r.text[:5000])
            if r.status_code != 429 and not (500 <= r.status_code < 600):
                raise last
            if attempt >= MAX_RETRIES:
                raise last
            retry = r.headers.get("Retry-After")
            try: delay = int(retry) if retry else min(2 ** attempt, 20)
            except ValueError: delay = min(2 ** attempt, 20)
            time.sleep(max(delay, 1))
        raise last or RuntimeError("Unexpected binary request failure")

    def post_bytes(self, path: str, data: bytes, *, params: Mapping[str, Any] | None = None,
                   content_type: str = "image/png") -> Any:
        return self.request(
            "POST", path, params=params, raw=data,
            headers={
                "Accept": "application/json",
                "Content-Type": content_type,
                "X-Atlassian-Token": "no-check",
            },
        )

    def delete(self, path: str, *, body: Any = None) -> Any:
        return self.request("DELETE", path, body=body)

    def paginate(self, path: str, *, params: Mapping[str, Any] | None = None,
                 key: str = "values") -> list[Any]:
        out: list[Any] = []
        start = 0
        for _ in range(10000):
            p = dict(params or {})
            p["startAt"] = start
            p["maxResults"] = PAGE_SIZE
            payload = self.get(path, params=p)
            if isinstance(payload, list):
                out.extend(payload)
                break
            if not isinstance(payload, dict):
                break
            items = payload.get(key)
            if items is None:
                for candidate in ("projects", "boards", "permissionSchemes", "notificationSchemes"):
                    if isinstance(payload.get(candidate), list):
                        items = payload[candidate]
                        break
            if items is None:
                out.append(payload)
                break
            if not isinstance(items, list):
                out.append(items)
                break
            out.extend(items)
            if payload.get("isLast") is True or not items:
                break
            total = payload.get("total")
            if isinstance(total, int) and start + len(items) >= total:
                break
            start += len(items)
        return out


class ExportBundle:
    def __init__(self, zip_path: Path) -> None:
        self.zip_path = zip_path
        self.tmp = Path(tempfile.mkdtemp(prefix="jira_import_bundle_"))
        with zipfile.ZipFile(zip_path) as z:
            safe_extract_zip(z, self.tmp)
        roots = [p.parent for p in self.tmp.rglob("manifest.json")]
        if not roots:
            raise ValueError("The ZIP does not contain manifest.json from the Jira exporter.")
        self.root = roots[0]
        self.manifest = self.read("manifest.json")

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def read(self, rel: str, default: Any = None) -> Any:
        p = self.path(rel)
        if not p.exists():
            return default
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)

    def glob_json(self, pattern: str) -> list[tuple[Path, Any]]:
        out = []
        for p in sorted(self.root.glob(pattern)):
            try:
                with p.open("r", encoding="utf-8") as f:
                    out.append((p, json.load(f)))
            except Exception:
                continue
        return out


class Importer:
    def __init__(self, bundle: ExportBundle, client: JiraClient, *, apply: bool,
                 source_key: str, target_key: str, target_name: str | None,
                 output_dir: Path, reuse_project: bool,
                 field_model_preference: str = "auto",
                 allow_missing_default_values: bool = False,
                 priority_mode: str = "replicate",
                 sync_global_priority_default: bool = False,
                 all_work_visible_columns: str | None = None,
                 all_work_column_scope: str = "both") -> None:
        self.b = bundle
        self.c = client
        self.apply = apply
        self.source_key = source_key.upper()
        self.target_key = target_key.upper()
        self.target_name_override = target_name
        self.reuse_project = reuse_project
        self.field_model_preference = field_model_preference
        self.allow_missing_default_values = allow_missing_default_values
        self.priority_mode = priority_mode
        self.sync_global_priority_default = sync_global_priority_default
        self.all_work_visible_columns = str(all_work_visible_columns or "").strip()
        self.all_work_column_scope = str(all_work_column_scope or "both").strip().lower()
        self.field_model_used = ""
        self.field_model_detection: dict[str, Any] = {
            "sourceModel": "legacy-field-configurations",
            "requestedDestinationModel": field_model_preference,
            "selectedDestinationModel": None,
            "fallbacks": [],
            "probes": {},
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.out = output_dir / f"jira_import_{self.target_key}_{stamp}"
        self.out.mkdir(parents=True, exist_ok=True)
        self.actions: list[Action] = []
        self.manual: list[str] = []
        self.idmap: dict[str, dict[str, str]] = {
            "project": {}, "issue_type": {}, "issue_type_scheme": {}, "field": {}, "field_context": {},
            "screen": {}, "screen_tab": {}, "screen_scheme": {},
            "issue_type_screen_scheme": {}, "field_configuration": {},
            "field_configuration_scheme": {}, "workflow": {}, "workflow_scheme": {},
            "status": {}, "filter": {}, "board": {}, "component": {}, "version": {},
            "permission_scheme": {}, "notification_scheme": {}, "project_role": {},
            "field_scheme": {}, "field_option": {},
            "priority": {}, "priority_scheme": {}, "issue_type_avatar": {},
            "issue": {}, "epic": {},
        }
        self.project = self.b.read(f"data/projects/{self.source_key}/project.json")
        if not isinstance(self.project, dict):
            raise ValueError(f"Project {self.source_key} was not found in the export.")
        self.target_project_id = ""
        self.dest_me: dict[str, Any] = {}
        self.dest_fields: list[dict[str, Any]] = []
        self.dest_issue_types: list[dict[str, Any]] = []
        self.dest_projects: list[dict[str, Any]] = []
        self.dest_statuses: list[dict[str, Any]] = []
        self.dest_workflows: list[dict[str, Any]] = []
        self.dest_project_roles: list[dict[str, Any]] = []
        self.created_fields: set[str] = set()
        self.created_contexts: set[str] = set()
        self.default_value_report: list[dict[str, Any]] = []
        self.priority_report: list[dict[str, Any]] = []
        self.permission_scheme_report: list[dict[str, Any]] = []
        self.filter_column_results: list[dict[str, Any]] = []
        self.legacy_field_configuration_results: list[dict[str, Any]] = []
        self.legacy_post_association_syncs: list[tuple[str, str, list[dict[str, Any]], bool]] = []
        self.custom_field_association_results: list[dict[str, Any]] = []
        self.dest_filter_fields: list[dict[str, Any]] = []
        self.board_manual_config: list[dict[str, Any]] = []
        self.board_cleanup_results: list[dict[str, Any]] = []
        self.epic_import_results: list[dict[str, Any]] = []
        self.board_timeline_results: list[dict[str, Any]] = []
        self.board_column_results: list[dict[str, Any]] = []
        self.list_view_results: list[dict[str, Any]] = []
        self._create_meta_cache: dict[str, dict[str, Any] | None] = {}
        self.project_created_this_run = False
        self._system_issue_type_avatar_hashes: dict[str, str] | None = None
        self._system_issue_type_avatar_ids: set[str] | None = None
        self.source_fingerprint = sha256_file(self.b.zip_path)

    def record(self, phase: str, entity: str, source: Any, destination: Any,
               action: str, status: str, note: str = "") -> None:
        self.actions.append(Action(phase, entity, str(source or ""), str(destination or ""), action, status, note))
        print(f"[{phase}] {status.upper():8} {entity}: {source} -> {destination or '-'} ({action})")
        if note:
            print(f"           {note}")

    def manual_add(self, text: str) -> None:
        if text not in self.manual:
            self.manual.append(text)

    def api(self, phase: str, entity: str, source: Any, destination: Any,
            action: str, method: str, path: str, body: Any = None,
            *, optional: bool = True) -> Any:
        if not self.apply:
            self.record(phase, entity, source, destination, action, "planned")
            return None
        try:
            if method == "POST": result = self.c.post(path, body)
            elif method == "PUT": result = self.c.put(path, body)
            elif method == "GET": result = self.c.get(path)
            elif method == "DELETE": result = self.c.delete(path, body=body)
            else: raise ValueError(f"Unsupported method {method}")
            self.record(phase, entity, source, destination, action, "success")
            return result
        except JiraError as exc:
            self.record(phase, entity, source, destination, action,
                        "skipped" if optional else "failed",
                        f"HTTP {exc.status}: {exc.message}")
            self.manual_add(f"{entity} '{source}' could not be {action}: HTTP {exc.status} - {exc.message}")
            if not optional:
                raise
            return None

    def run(self) -> Path:
        try:
            self.preflight()
            self.ensure_project()
            # Creating a software/Scrum project can materialize Jira-managed custom fields
            # (notably the company-managed Story Points field). The preflight field snapshot
            # was taken before project creation, so refresh it before matching/cloning fields.
            self.refresh_destination_fields()
            self.ensure_priorities_and_scheme()
            self.ensure_issue_types_and_scheme()
            self.ensure_fields()
            self.validate_workflow_field_mappings()
            self.ensure_field_contexts_and_options()
            self.ensure_screens_and_schemes()
            self.ensure_workflows_and_scheme()
            self.ensure_field_configurations()
            self.ensure_permission_scheme()
            self.ensure_notification_scheme()
            self.ensure_components_versions_properties()
            self.ensure_field_default_values()
            self.ensure_epics()
            self.ensure_filters_and_boards()
            self.verify()
        finally:
            self.write_outputs()
        return self.out

    def refresh_destination_fields(self) -> None:
        """Refresh destination field metadata after operations that can create Jira-managed fields.

        Jira's Scrum project template can create/materialize fields such as the company-managed
        ``Story Points`` custom field as a side effect of project creation.  Using the preflight
        snapshot after that point can make the importer believe the field is absent and create a
        duplicate Number field with the same name.
        """
        self.dest_fields = self.c.paginate(
            "/rest/api/3/field/search",
            params={"expand": "key,isLocked,searcherKey,contextsCount"},
        )
        try:
            self.dest_filter_fields = self.c.get("/rest/api/3/field") or []
        except JiraError:
            self.dest_filter_fields = self.dest_fields

    def preflight(self) -> None:
        phase = "preflight"
        self.dest_me = self.c.get("/rest/api/3/myself")
        self.record(phase, "destination account", self.dest_me.get("displayName"), self.dest_me.get("accountId"), "authenticate", "success")
        self.dest_projects = self.c.paginate("/rest/api/3/project/search", params={"orderBy": "key"})
        self.dest_fields = self.c.paginate("/rest/api/3/field/search", params={"expand": "key,isLocked,searcherKey,contextsCount"})
        try:
            self.dest_filter_fields = self.c.get("/rest/api/3/field") or []
        except JiraError:
            self.dest_filter_fields = self.dest_fields
        self.dest_issue_types = self.c.get("/rest/api/3/issuetype") or []
        self.dest_statuses = self.c.get("/rest/api/3/status") or []
        self.dest_workflows = self.c.paginate("/rest/api/3/workflow/search", params={"expand": "statuses,transitions.rules"})
        try:
            self.dest_project_roles = self.c.get("/rest/api/3/role") or []
        except JiraError:
            self.dest_project_roles = []
        exporter_ver = str((self.b.manifest.get("exporter") or {}).get("version", "0"))
        selected = [str(x).upper() for x in (self.b.manifest.get("selectedProjectKeys") or [])]
        if self.source_key not in selected:
            raise RuntimeError(f"The embedded export does not list project {self.source_key}. Selected projects: {selected}")
        if not any(self.b.path("data/fields/contexts").glob("*.json")):
            raise RuntimeError("The export contains no custom-field context files. Use a Source-Site Exporter v1.1+ package.")
        has_default_export = any(self.b.path("data/fields/default_values_grouped").glob("*.json")) or any(
            self.b.path("data/fields/default_values_legacy").glob("*.json")
        )
        note = "Custom-field contexts/options are present."
        if has_default_export:
            note += " Context default values are present."
        else:
            note += " Context default values were not captured by this older export."
        self.record(phase, "source export", exporter_ver, self.source_fingerprint[:16], "validate", "success", note)
        if self.apply and not has_default_export and not self.allow_missing_default_values:
            raise RuntimeError(
                "This source export does not contain custom-field default values. Rerun Source-Site Exporter v1.2+ "
                "and pass the new ZIP to this importer, or use --allow-missing-default-values to proceed without them."
            )
        self.record(phase, "safety mode", "guarded cleanup", "dry-run" if not self.apply else "apply", "validate", "success",
                    "The importer preserves destination Jira configuration objects. The only automatic deletion allowed is the Scrum-template board named '<target key> board' when that board is absent from the source export.")

    def ensure_project(self) -> None:
        phase = "project"
        existing = first(self.dest_projects, lambda p: str(p.get("key", "")).upper() == self.target_key)
        source_id = str(self.project.get("id", ""))
        source_name = str(self.project.get("name") or self.source_key)
        target_name = self.target_name_override or source_name
        if existing:
            if not self.reuse_project:
                raise RuntimeError(f"Destination project {self.target_key} already exists. Use --reuse-project to continue safely.")
            self.target_project_id = str(existing.get("id"))
            self.idmap["project"][source_id] = self.target_project_id
            self.record(phase, "project", self.source_key, self.target_key, "reuse", "success")
            return
        body = {
            "key": self.target_key,
            "name": target_name,
            "projectTypeKey": self.project.get("projectTypeKey", "software"),
            "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-scrum-template",
            "leadAccountId": self.dest_me.get("accountId"),
            "assigneeType": self.project.get("assigneeType", "UNASSIGNED"),
            "description": self.project.get("description", ""),
        }
        result = self.api(phase, "project", self.source_key, self.target_key, "create", "POST", "/rest/api/3/project", body, optional=False)
        if self.apply:
            self.target_project_id = str((result or {}).get("id", ""))
            if not self.target_project_id:
                refreshed = self.c.paginate("/rest/api/3/project/search", params={"keys": self.target_key})
                found = first(refreshed, lambda p: str(p.get("key", "")).upper() == self.target_key)
                self.target_project_id = str((found or {}).get("id", ""))
            if not self.target_project_id:
                raise RuntimeError("Jira created the project but did not return/resolve its project ID.")
        else:
            self.target_project_id = "<new-project-id>"
        self.idmap["project"][source_id] = self.target_project_id
        self.project_created_this_run = True

    def source_priority_scheme(self) -> dict[str, Any] | None:
        project_specific = self.b.read(f"data/projects/{self.source_key}/priority_scheme.json", None)
        if isinstance(project_specific, dict) and project_specific.get("id"):
            return project_specific

        schemes = self.b.read("data/reference/priority_schemes.json", []) or []
        schemes = schemes if isinstance(schemes, list) else unwrap(schemes)
        source_project_id = str(self.project.get("id", ""))
        for scheme in schemes:
            projects = page_values(scheme.get("projects"))
            if any(
                str(p.get("id", "")) == source_project_id
                or str(p.get("key", "")).upper() == self.source_key
                for p in projects if isinstance(p, dict)
            ):
                return scheme
        return first(schemes, lambda x: bool(x.get("isDefault"))) or (schemes[0] if schemes else None)

    def ensure_priorities_and_scheme(self) -> None:
        phase = "priorities"
        if self.priority_mode == "skip":
            self.record(phase, "priority configuration", self.source_key, self.target_key, "skip", "success")
            return

        source_scheme = self.source_priority_scheme()
        if not isinstance(source_scheme, dict):
            self.record(phase, "priority scheme", self.source_key, "", "clone", "skipped", "No source priority scheme was exported.")
            self.manual_add("No source priority scheme was exported. Rerun Source-Site Exporter v1.3+ for exact priority cloning.")
            return

        source_priorities = page_values(source_scheme.get("priorities"))
        if not source_priorities:
            scheme_id = str(source_scheme.get("id", ""))
            if scheme_id:
                source_priorities = self.b.read(f"data/reference/priority_scheme_priorities/{scheme_id}.json", []) or []
        if not source_priorities:
            # Exporter v1.1/v1.2 fallback. This is exact for the default scheme,
            # which includes all priorities, and best-effort for older non-default exports.
            source_priorities = self.b.read("data/reference/priorities.json", []) or []
            if not source_scheme.get("isDefault"):
                self.manual_add(
                    "The source export predates per-scheme priority lists. Priority membership is best-effort; "
                    "rerun Source-Site Exporter v1.3+ for an exact non-default scheme."
                )
        source_priorities = [p for p in source_priorities if isinstance(p, dict) and p.get("id")]
        if not source_priorities:
            self.record(phase, "priorities", self.source_key, "", "clone", "skipped", "No source priorities were exported.")
            return

        try:
            destination_priorities = self.c.paginate("/rest/api/3/priority/search")
        except JiraError:
            raw = self.c.get("/rest/api/3/priority")
            destination_priorities = raw if isinstance(raw, list) else unwrap(raw)

        used_destination_ids: set[str] = set()
        for src in source_priorities:
            sid = str(src.get("id"))
            name = str(src.get("name", ""))
            dest = first(destination_priorities, lambda x: norm(x.get("name")) == norm(name) and str(x.get("id")) not in used_destination_ids)
            match_method = "name"
            if dest is None and self.priority_mode == "replicate":
                signature = priority_signature(src)
                if signature:
                    dest = first(
                        destination_priorities,
                        lambda x: str(x.get("id")) not in used_destination_ids and priority_signature(x) == signature,
                    )
                    match_method = "built-in signature"
            if dest is not None:
                did = str(dest.get("id"))
                desired = priority_write_body(src)
                needs_update = any(
                    norm(dest.get(key)) != norm(value) if key != "statusColor"
                    else str(dest.get(key, "")).lower() != str(value).lower()
                    for key, value in desired.items()
                )
                if needs_update and self.priority_mode == "replicate":
                    self.api(
                        phase, "priority", f"{src.get('name')} ({sid})", did,
                        f"update matched by {match_method}", "PUT", f"/rest/api/3/priority/{did}", desired,
                        optional=False,
                    )
                else:
                    self.record(phase, "priority", f"{name} ({sid})", did, "reuse", "success", f"Matched by {match_method}.")
            else:
                desired = priority_write_body(src)
                result = self.api(
                    phase, "priority", f"{name} ({sid})", "", "create", "POST", "/rest/api/3/priority", desired,
                    optional=False,
                )
                did = str((result or {}).get("id", "")) if self.apply else f"<new-priority:{name}>"
                if self.apply and not did:
                    refreshed = self.c.paginate("/rest/api/3/priority/search")
                    created = first(refreshed, lambda x: norm(x.get("name")) == norm(name))
                    did = str((created or {}).get("id", ""))
            if did:
                self.idmap["priority"][sid] = did
                used_destination_ids.add(did)
                self.priority_report.append({
                    "sourcePriorityId": sid,
                    "sourceName": name,
                    "destinationPriorityId": did,
                    "destinationName": name,
                    "sourceDefault": bool(src.get("isDefault")),
                })

        mapped_ids = [self.idmap["priority"].get(str(p.get("id"))) for p in source_priorities]
        mapped_ids = [x for x in mapped_ids if x]
        source_default_id = str(source_scheme.get("defaultPriorityId") or "")
        if not source_default_id:
            default_src = first(source_priorities, lambda x: bool(x.get("isDefault")))
            source_default_id = str((default_src or {}).get("id", ""))
        destination_default_id = self.idmap["priority"].get(source_default_id) or (mapped_ids[0] if mapped_ids else "")
        if not mapped_ids or not destination_default_id:
            self.manual_add("Priority scheme could not be created because one or more priority IDs were not mapped.")
            return

        mapped_id_set = {str(x) for x in mapped_ids}
        # When an existing project is moved into the cloned scheme, Jira may need
        # a migration target for any destination priority not present in the source
        # scheme. Map those values to the source scheme's default rather than
        # allowing the association to fail or silently lose issue values.
        migration_in = {
            str(p.get("id")): int(destination_default_id) if str(destination_default_id).isdigit() else destination_default_id
            for p in destination_priorities
            if str(p.get("id", "")) and str(p.get("id")) not in mapped_id_set
        }

        source_name = str(source_scheme.get("name") or "Priority Scheme")
        # Jira's built-in default priority scheme cannot be edited. A target-specific
        # clone gives the destination project identical behavior without disturbing
        # unrelated projects on the destination site.
        target_scheme_name = source_name
        if bool(source_scheme.get("isDefault")) or norm(source_name) == norm("Default priority scheme"):
            target_scheme_name = f"{self.target_key}: {source_name}"

        try:
            destination_schemes = self.c.paginate(
                "/rest/api/3/priorityscheme", params={"expand": "priorities,projects"}
            )
        except JiraError as exc:
            self.record(phase, "priority scheme", source_name, "", "read", "skipped", f"HTTP {exc.status}: {exc.message}")
            self.manual_add(f"Priority schemes are unavailable on the destination: HTTP {exc.status} - {exc.message}")
            return

        existing_scheme = first(destination_schemes, lambda x: norm(x.get("name")) == norm(target_scheme_name))
        if existing_scheme:
            dest_scheme_id = str(existing_scheme.get("id"))
            current_priority_ids = [str(x.get("id")) for x in page_values(existing_scheme.get("priorities"))]
            current_project_ids = [str(x.get("id")) for x in page_values(existing_scheme.get("projects"))]
            add_priorities = [int(x) for x in mapped_ids if str(x).isdigit() and str(x) not in current_priority_ids]
            # The target-specific clone is importer-owned, so removing obsolete
            # priority membership on reruns is safe and keeps the scheme exact.
            remove_priorities = [
                int(x) for x in current_priority_ids
                if str(x).isdigit() and str(x) not in {str(v) for v in mapped_ids}
            ]
            add_projects = []
            if str(self.target_project_id).isdigit() and str(self.target_project_id) not in current_project_ids:
                add_projects = [int(self.target_project_id)]
            body: dict[str, Any] = {
                "name": target_scheme_name,
                "description": source_scheme.get("description", ""),
                "defaultPriorityId": int(destination_default_id) if str(destination_default_id).isdigit() else destination_default_id,
                "priorities": {
                    "add": {"ids": add_priorities},
                    "remove": {"ids": remove_priorities},
                },
                "projects": {"add": {"ids": add_projects}, "remove": {"ids": []}},
                "mappings": {
                    "in": migration_in,
                    "out": {str(x): int(destination_default_id) if str(destination_default_id).isdigit() else destination_default_id for x in remove_priorities},
                },
            }
            self.api(
                phase, "priority scheme", source_name, dest_scheme_id, "synchronize and associate",
                "PUT", f"/rest/api/3/priorityscheme/{dest_scheme_id}", body, optional=False,
            )
        else:
            body = {
                "name": target_scheme_name,
                "description": source_scheme.get("description", ""),
                "defaultPriorityId": int(destination_default_id) if str(destination_default_id).isdigit() else destination_default_id,
                "priorityIds": [int(x) if str(x).isdigit() else x for x in mapped_ids],
                "projectIds": [int(self.target_project_id)] if str(self.target_project_id).isdigit() else [],
                "mappings": {"in": migration_in, "out": {}},
            }
            result = self.api(
                phase, "priority scheme", source_name, target_scheme_name, "create and associate",
                "POST", "/rest/api/3/priorityscheme", body, optional=False,
            )
            dest_scheme_id = str((result or {}).get("id", "")) if self.apply else f"<new-priority-scheme:{target_scheme_name}>"
            if self.apply and not dest_scheme_id:
                refreshed = self.c.paginate("/rest/api/3/priorityscheme", params={"schemeName": target_scheme_name})
                found = first(refreshed, lambda x: norm(x.get("name")) == norm(target_scheme_name))
                dest_scheme_id = str((found or {}).get("id", ""))

        if dest_scheme_id:
            self.idmap["priority_scheme"][str(source_scheme.get("id", ""))] = dest_scheme_id
            self.priority_report.append({
                "sourceSchemeId": str(source_scheme.get("id", "")),
                "sourceSchemeName": source_name,
                "destinationSchemeId": dest_scheme_id,
                "destinationSchemeName": target_scheme_name,
                "sourceDefaultPriorityId": source_default_id,
                "destinationDefaultPriorityId": destination_default_id,
                "projectId": self.target_project_id,
            })

        if self.sync_global_priority_default:
            self.api(
                phase, "global default priority", source_default_id, destination_default_id,
                "set", "PUT", "/rest/api/3/priority/default", {"id": str(destination_default_id)},
                optional=False,
            )
        else:
            self.manual_add(
                "The target project's priority-scheme default was replicated. The site-wide global default "
                "was intentionally left unchanged; use --sync-global-priority-default if the entire destination site should match."
            )

        if self.apply and dest_scheme_id and str(dest_scheme_id).isdigit():
            try:
                verification = {
                    "scheme": first(
                        self.c.paginate("/rest/api/3/priorityscheme", params={"schemeId": [int(dest_scheme_id)], "expand": "priorities,projects"}),
                        lambda x: str(x.get("id")) == str(dest_scheme_id),
                    ),
                    "priorities": self.c.paginate(f"/rest/api/3/priorityscheme/{dest_scheme_id}/priorities"),
                    "projects": self.c.paginate(f"/rest/api/3/priorityscheme/{dest_scheme_id}/projects"),
                }
                (self.out / "priority_scheme_verification.json").write_text(
                    json.dumps(verification, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except JiraError as exc:
                self.manual_add(f"Priority scheme was created but verification failed: HTTP {exc.status} - {exc.message}")

    def _destination_system_issue_type_avatar_hashes(self) -> dict[str, str]:
        """Return SHA-256(image bytes) -> destination system avatar ID.

        Jira ships a catalogue of system issue-type icons. When the source work
        type selected one of those icons, reusing the destination system avatar
        is both sharper and more faithful than uploading Jira's rendered PNG as
        a new custom avatar. The hash comparison protects against assuming that
        site-local avatar IDs are portable.
        """
        if self._system_issue_type_avatar_hashes is not None:
            return self._system_issue_type_avatar_hashes
        hashes: dict[str, str] = {}
        ids: set[str] = set()
        try:
            payload = self.c.get("/rest/api/3/avatar/issuetype/system") or {}
            avatars = payload.get("system", []) if isinstance(payload, dict) else []
            for avatar in avatars if isinstance(avatars, list) else []:
                avatar_id = str((avatar or {}).get("id", ""))
                if not avatar_id:
                    continue
                ids.add(avatar_id)
                try:
                    data, _ = self.c.get_bytes(
                        f"/rest/api/3/universal_avatar/view/type/issuetype/avatar/{avatar_id}",
                        params={"size": "xlarge", "format": "png"}, accept="image/png",
                    )
                except JiraError:
                    continue
                hashes[hashlib.sha256(data).hexdigest()] = avatar_id
        except JiraError:
            pass
        self._system_issue_type_avatar_hashes = hashes
        self._system_issue_type_avatar_ids = ids
        return hashes

    def ensure_issue_type_avatar(self, src: dict[str, Any], destination_issue_type_id: str) -> None:
        """Replicate the selected issue-type icon from a v1.4+ export.

        v3.0 fixes two visual problems from v2.9:
        1. Source Jira system icons are matched back to Jira system avatars
           instead of being re-uploaded as custom images.
        2. Custom images are uploaded using the full exported image as the crop
           region. The avatar2 `size` query parameter is a crop size, not an
           output-size request; hard-coding 48 could zoom/crop larger exports.
        """
        phase = "work-types"
        source_issue_type_id = str(src.get("id", ""))
        name = str(src.get("name", source_issue_type_id))
        manifest = self.b.read("data/work_types/avatar_manifest.json", []) or []
        entry = first(manifest, lambda x: str(x.get("sourceIssueTypeId")) == source_issue_type_id)
        rel = str((entry or {}).get("file") or f"data/work_types/avatars/{source_issue_type_id}.png")
        if not self.b.exists(rel):
            if src.get("avatarId") not in (None, ""):
                self.record(phase, "work type avatar", name, destination_issue_type_id, "sync", "skipped",
                            "Avatar pixels were not present in this export; rerun Source-Site Exporter v1.4+ to transfer custom icons.")
            return

        if not self.apply:
            self.record(phase, "work type avatar", name, destination_issue_type_id, "match system avatar or upload full crop", "planned")
            return

        source_bytes = self.b.path(rel).read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        try:
            # Best case: the source selected one of Jira's built-in icons. Match
            # its pixels against the destination catalogue and select that system
            # avatar directly. This avoids raster re-upload artefacts entirely.
            system_hashes = self._destination_system_issue_type_avatar_hashes()
            source_avatar_id = str(src.get("avatarId") or (entry or {}).get("sourceAvatarId") or "")
            system_match = None
            # Jira's built-in issue-type avatar IDs are commonly stable between
            # sites. Prefer the same ID when it is explicitly present in the
            # destination system catalogue, then fall back to an exact pixel match.
            if source_avatar_id and source_avatar_id in (self._system_issue_type_avatar_ids or set()):
                system_match = source_avatar_id
            if not system_match:
                system_match = system_hashes.get(source_hash)
            if system_match:
                avatar_value: Any = int(system_match) if system_match.isdigit() else system_match
                self.c.put(f"/rest/api/3/issuetype/{destination_issue_type_id}", {"avatarId": avatar_value})
                self.idmap["issue_type_avatar"][source_issue_type_id] = system_match
                self.record(phase, "work type avatar", name, system_match, "reuse Jira system avatar", "success")
                return

            dest_details = self.c.get(f"/rest/api/3/issuetype/{destination_issue_type_id}") or {}
            current_avatar_id = dest_details.get("avatarId")
            if current_avatar_id not in (None, ""):
                try:
                    current_bytes, _ = self.c.get_bytes(
                        f"/rest/api/3/universal_avatar/view/type/issuetype/avatar/{current_avatar_id}",
                        params={"size": "xlarge", "format": "png"}, accept="image/png",
                    )
                    if hashlib.sha256(current_bytes).hexdigest() == source_hash:
                        self.idmap["issue_type_avatar"][source_issue_type_id] = str(current_avatar_id)
                        self.record(phase, "work type avatar", name, current_avatar_id, "reuse visual match", "success")
                        return
                except JiraError:
                    pass

            # Exporter v1.4 stores PNG bytes. Read its real dimensions and pass
            # the complete square region to Jira's crop API. If a future export
            # is non-square, use a centered square crop rather than the top-left.
            width = height = 0
            if len(source_bytes) >= 24 and source_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                width, height = struct.unpack(">II", source_bytes[16:24])
            if width > 0 and height > 0:
                crop = min(width, height)
                x = max((width - crop) // 2, 0)
                y = max((height - crop) // 2, 0)
            else:
                crop, x, y = 48, 0, 0

            uploaded = self.c.post_bytes(
                f"/rest/api/3/universal_avatar/type/issuetype/owner/{destination_issue_type_id}",
                source_bytes, params={"x": x, "y": y, "size": crop}, content_type="image/png",
            ) or {}
            new_avatar_id = str(uploaded.get("id", ""))
            if not new_avatar_id:
                raise RuntimeError("Jira uploaded the avatar but did not return an avatar ID.")
            avatar_value = int(new_avatar_id) if new_avatar_id.isdigit() else new_avatar_id
            self.c.put(f"/rest/api/3/issuetype/{destination_issue_type_id}", {"avatarId": avatar_value})
            self.idmap["issue_type_avatar"][source_issue_type_id] = new_avatar_id
            self.record(phase, "work type avatar", name, new_avatar_id, "upload and select full crop", "success",
                        f"crop x={x}, y={y}, size={crop} from {width or '?'}x{height or '?'} source")
        except (JiraError, RuntimeError) as exc:
            message = exc.message if isinstance(exc, JiraError) else str(exc)
            self.record(phase, "work type avatar", name, destination_issue_type_id, "replicate", "warning", message)
            self.manual_add(f"Work type icon for '{name}' was not replicated automatically: {message}")

    def ensure_issue_types_and_scheme(self) -> None:
        phase = "work-types"
        # Creating a project from a Jira template can add project-scoped work types
        # after preflight. Refresh here so same-name types are reused when safe.
        try:
            self.dest_issue_types = self.c.get("/rest/api/3/issuetype") or self.dest_issue_types
        except JiraError as exc:
            self.record(phase, "work type inventory", "destination", "cached", "refresh", "warning",
                        f"HTTP {exc.status}: {exc.message}")

        source_types = self.b.read("data/work_types/issue_types.json", []) or []
        project_type_ids = {str(x.get("id")) for x in self.project.get("issueTypes", [])}
        source_types = [x for x in source_types if str(x.get("id")) in project_type_ids]

        for src in source_types:
            sid = str(src.get("id")); name = str(src.get("name")); subtask = bool(src.get("subtask"))
            found = first(
                self.dest_issue_types,
                lambda x: norm(x.get("name")) == norm(name) and bool(x.get("subtask")) == subtask,
            )
            if found:
                did = str(found.get("id"))
                self.idmap["issue_type"][sid] = did
                self.record(phase, "work type", name, did, "reuse", "success")
            else:
                body = {
                    "name": name, "description": src.get("description", ""),
                    "type": "subtask" if subtask else "standard",
                }
                result = self.api(phase, "work type", name, "", "create", "POST", "/rest/api/3/issuetype", body)
                did = str((result or {}).get("id", "")) if self.apply else f"<new-issue-type:{name}>"
                if did:
                    self.idmap["issue_type"][sid] = did
                    if self.apply:
                        self.dest_issue_types.append({"id": did, "name": name, "subtask": subtask})
            if did:
                self.ensure_issue_type_avatar(src, did)

        assoc = self.b.read(f"data/projects/{self.source_key}/issue_type_scheme_association.json", {}) or {}
        vals = unwrap(assoc)
        source_scheme = ((vals[0] if vals else {}).get("issueTypeScheme") or {})
        source_scheme_id = str(source_scheme.get("id", ""))
        scheme_name = str(source_scheme.get("name") or f"{self.target_key}: Issue Type Scheme")
        source_mappings = self.b.read("data/work_types/issue_type_scheme_mappings.json", []) or []
        desired_ids = [
            self.idmap["issue_type"].get(str(m.get("issueTypeId")))
            for m in source_mappings
            if str(m.get("issueTypeSchemeId")) == source_scheme_id
        ]
        desired_ids = [str(x) for x in desired_ids if x]
        default_src = str(source_scheme.get("defaultIssueTypeId", ""))
        desired_default = self.idmap["issue_type"].get(default_src) or (desired_ids[0] if desired_ids else None)

        try:
            dest_schemes = self.c.paginate("/rest/api/3/issuetypescheme", params={"expand": "projects,issueTypes"})
        except JiraError:
            dest_schemes = []
        found_scheme = first(dest_schemes, lambda x: norm(x.get("name")) == norm(scheme_name))
        if found_scheme:
            dest_scheme_id = str(found_scheme.get("id"))
            self.record(phase, "work type scheme", scheme_name, dest_scheme_id, "reuse and synchronize", "success")
            if not self.apply:
                self.record(phase, "work type scheme membership/default", scheme_name, dest_scheme_id, "synchronize", "planned")
            else:
                try:
                    current_rows = self.c.paginate(
                        "/rest/api/3/issuetypescheme/mapping",
                        params={"issueTypeSchemeId": [int(dest_scheme_id)]} if dest_scheme_id.isdigit() else None,
                    )
                except JiraError:
                    current_rows = self.c.paginate("/rest/api/3/issuetypescheme/mapping")
                current_ids = {
                    str(row.get("issueTypeId")) for row in current_rows
                    if str(row.get("issueTypeSchemeId")) == dest_scheme_id
                }
                missing = [x for x in desired_ids if x not in current_ids]
                if missing:
                    self.api(phase, "work type scheme membership", scheme_name, ",".join(missing),
                             "add missing work types", "PUT", f"/rest/api/3/issuetypescheme/{dest_scheme_id}/issuetype",
                             {"issueTypeIds": missing}, optional=False)
                update_body: dict[str, Any] = {
                    "name": scheme_name, "description": source_scheme.get("description", ""),
                }
                if desired_default:
                    update_body["defaultIssueTypeId"] = desired_default
                self.api(phase, "work type scheme default", scheme_name, desired_default or "",
                         "synchronize", "PUT", f"/rest/api/3/issuetypescheme/{dest_scheme_id}", update_body, optional=False)
                extras = [x for x in current_ids if x not in set(desired_ids)]
                for extra in extras:
                    # This removes only the type from this target scheme; it does not
                    # delete the global Jira work type. If issues still use it Jira will
                    # reject the removal and the importer records a manual follow-up.
                    self.api(phase, "work type scheme membership", extra, dest_scheme_id,
                             "remove source-extra work type", "DELETE",
                             f"/rest/api/3/issuetypescheme/{dest_scheme_id}/issuetype/{extra}", optional=True)
        else:
            body: dict[str, Any] = {
                "name": scheme_name, "description": source_scheme.get("description", ""),
                "issueTypeIds": desired_ids,
            }
            if desired_default:
                body["defaultIssueTypeId"] = desired_default
            result = self.api(phase, "work type scheme", scheme_name, "", "create", "POST", "/rest/api/3/issuetypescheme", body)
            if self.apply:
                dest_scheme_id = str((result or {}).get("issueTypeSchemeId") or (result or {}).get("id") or "")
                if not dest_scheme_id:
                    refreshed = self.c.paginate("/rest/api/3/issuetypescheme", params={"queryString": scheme_name})
                    created_scheme = first(refreshed, lambda x: norm(x.get("name")) == norm(scheme_name))
                    dest_scheme_id = str((created_scheme or {}).get("id", ""))
            else:
                dest_scheme_id = f"<new-issue-type-scheme:{scheme_name}>"

        if source_scheme_id and dest_scheme_id:
            self.idmap["issue_type_scheme"][source_scheme_id] = dest_scheme_id
            self.api(phase, "work type scheme association", scheme_name, self.target_key, "associate", "PUT",
                     "/rest/api/3/issuetypescheme/project",
                     {"issueTypeSchemeId": dest_scheme_id, "projectId": self.target_project_id}, optional=False)

    # ---------- fields ----------
    def selected_source_filters(self) -> list[dict[str, Any]]:
        """Return only exported filters that belong to the selected source project.

        The same selection is used by field discovery and by filter creation so
        custom fields that appear only in filter JQL/columns are cloned *before*
        the filters are recreated.
        """
        filters = self.b.read("data/filters/filters.json", []) or []
        board_cfgs = {p.stem: data for p, data in self.b.glob_json("data/boards/configuration/*.json")}
        board_filter_ids = {
            str((cfg.get("filter") or {}).get("id"))
            for cfg in board_cfgs.values()
            if isinstance(cfg, dict)
            and str((cfg.get("location") or {}).get("key", "")).upper() == self.source_key
        }

        def belongs(f: dict[str, Any]) -> bool:
            if str(f.get("id")) in board_filter_ids or jql_mentions_project(str(f.get("jql", "")), self.source_key):
                return True
            for sp in f.get("sharePermissions", []) or []:
                if not isinstance(sp, dict):
                    continue
                project = sp.get("project") or {}
                if str(project.get("key", "")).upper() == self.source_key or str(project.get("id", "")) == str(self.project.get("id", "")):
                    return True
            return False

        return [f for f in filters if isinstance(f, dict) and belongs(f)]

    def _custom_field_ids_from_filter_jql(self, jql: str) -> set[str]:
        refs = {f"customfield_{m}" for m in re.findall(r"(?i)\bcf\[\s*(\d+)\s*\]", jql or "")}
        refs.update(f"customfield_{m}" for m in re.findall(r"(?i)\bcustomfield_(\d+)\b", jql or ""))
        return refs

    def relevant_source_field_ids(self) -> set[str]:
        ids: set[str] = set()
        screen_ids = self.relevant_screen_ids()
        for sid in screen_ids:
            tabs = self.b.read(f"data/screens/tabs/{sid}.json", []) or []
            for tab in tabs:
                arr = self.b.read(f"data/screens/tab_fields/{sid}__{tab.get('id')}.json", []) or []
                ids.update(str(x.get("id")) for x in arr if x.get("id"))
        # Fields referenced in workflow rules.
        for _, payload in self.b.glob_json("data/workflows/bulk_read/*.json"):
            for wf in payload.get("workflows", []) if isinstance(payload, dict) else []:
                if wf.get("name") in self.relevant_workflow_names():
                    collect_field_refs(wf, ids)

        # Fields that exist only in saved-filter JQL/columns must also be created
        # before filters are recreated. v3.7 missed this dependency, which could
        # leave cf[ID] references unmapped and make a filter silently fail to create.
        source_fields = self.b.read("data/fields/fields.json", []) or []
        source_by_name = {norm(x.get("name")): str(x.get("id")) for x in source_fields if isinstance(x, dict) and x.get("id") and x.get("name")}
        jql_operator = r"(?:=|!=|>=|<=|>|<|~|!~|\bIS\b|\bIS\s+NOT\b|\bIN\b|\bNOT\s+IN\b|\bWAS\b|\bCHANGED\b)"
        for f in self.selected_source_filters():
            filter_id = str(f.get("id", ""))
            jql = str(f.get("jql", ""))
            ids.update(self._custom_field_ids_from_filter_jql(jql))
            for field_name_norm, field_id in source_by_name.items():
                field_name = next((str(x.get("name")) for x in source_fields if isinstance(x, dict) and str(x.get("id")) == field_id), "")
                if not field_name:
                    continue
                if re.search(rf'(?i)"{re.escape(field_name)}"(?=\s*{jql_operator})', jql):
                    ids.add(field_id)
            raw_columns = self.b.read(f"data/filters/columns/{filter_id}.json", None)
            if not isinstance(raw_columns, list):
                effective = self.b.read(f"data/filters/effective_columns/{filter_id}.json", {}) or {}
                raw_columns = effective.get("columns") if isinstance(effective, dict) else None
            for column in raw_columns or []:
                value = str(column.get("value") or column.get("id") or "") if isinstance(column, dict) else str(column or "")
                if value:
                    ids.add(value)

        # The modern space List often derives its visible order from the user's
        # Jira default columns. Include those fields so list restoration does not
        # lose custom fields that are not present on a screen/workflow.
        list_capture = self.b.read(f"data/projects/{self.source_key}/list_view/column_capture.json", {}) or {}
        list_columns = list_capture.get("columns") if isinstance(list_capture, dict) else None
        if not isinstance(list_columns, list):
            list_columns = self.b.read("data/list_views/user_default_columns.json", []) or []
        for column in list_columns or []:
            value = str(column.get("value") or column.get("id") or "") if isinstance(column, dict) else str(column or "")
            if value:
                ids.add(value)
        return ids

    def ensure_fields(self) -> None:
        phase = "fields"
        source_fields = self.b.read("data/fields/fields.json", []) or []
        relevant = self.relevant_source_field_ids()
        source_by_id = {str(x.get("id")): x for x in source_fields}
        for sid in sorted(relevant):
            if not sid.startswith("customfield_"):
                self.idmap["field"][sid] = sid
                continue
            src = source_by_id.get(sid)
            if not src:
                self.manual_add(f"Field {sid} is referenced by a screen/workflow but its definition was not exported.")
                continue
            name = str(src.get("name"))
            schema_custom = str((src.get("schema") or {}).get("custom", ""))
            found = best_field_match(self.dest_fields, src)
            if not found and self.apply:
                # Jira can finish provisioning template-managed fields a moment after the
                # project-create request returns. Re-read before *any* custom-field POST so
                # a late-arriving Story Points field is reused rather than duplicated.
                for attempt in range(3):
                    self.refresh_destination_fields()
                    found = best_field_match(self.dest_fields, src)
                    if found:
                        break
                    if attempt < 2:
                        time.sleep(1.0)
            if found:
                did = str(found.get("id"))
                self.idmap["field"][sid] = did
                self.record(phase, "field", f"{name} ({sid})", did, "reuse", "success")
                continue
            same_name = [x for x in self.dest_fields if norm(x.get("name")) == norm(name)]
            if same_name:
                self.record(phase, "field", f"{name} ({sid})", "", "map", "failed", "A same-name destination field exists with an incompatible type.")
                self.manual_add(f"Field '{name}' exists on the destination with a different type. Resolve the conflict, then rerun.")
                continue
            if bool(src.get("isLocked")) or not cloneable_field(src):
                self.record(phase, "field", f"{name} ({sid})", "", "create", "skipped", "Managed/locked field is not safely creatable; the matching Jira product/app field must already exist.")
                self.manual_add(f"Managed/locked field '{name}' ({schema_custom}) was not found. Install/enable the same Jira product or app, then rerun.")
                continue
            body = {"name": name, "description": src.get("description", ""), "type": schema_custom}
            searcher = src.get("searcherKey")
            if searcher:
                body["searcherKey"] = searcher
            result = self.api(phase, "field", f"{name} ({sid})", "", "create", "POST", "/rest/api/3/field", body)
            did = str((result or {}).get("id", "")) if self.apply else f"<new-field:{name}>"
            if did:
                self.idmap["field"][sid] = did
                self.created_fields.add(sid)

    def validate_workflow_field_mappings(self) -> None:
        phase = "fields"
        refs: set[str] = set()
        for _, payload in self.b.glob_json("data/workflows/bulk_read/*.json"):
            if not isinstance(payload, dict):
                continue
            for workflow in payload.get("workflows", []) or []:
                if str(workflow.get("name")) in self.relevant_workflow_names():
                    collect_field_refs(workflow, refs)
        missing = sorted(x for x in refs if x.startswith("customfield_") and x not in self.idmap["field"])
        if not missing:
            self.record(phase, "workflow field mappings", len(refs), "complete", "validate", "success")
            return
        source_fields = {str(x.get("id")): str(x.get("name")) for x in (self.b.read("data/fields/fields.json", []) or [])}
        labels = [f"{fid} ({source_fields.get(fid, 'unknown')})" for fid in missing]
        message = "Required workflow fields have no destination mapping: " + ", ".join(labels)
        self.record(phase, "workflow field mappings", len(refs), len(missing), "validate", "failed" if self.apply else "warning", message)
        self.manual_add(message)
        if self.apply:
            raise RuntimeError(message)

    def ensure_field_contexts_and_options(self) -> None:
        phase = "field-contexts"
        context_files = self.b.glob_json("data/fields/contexts/*.json")
        for p, contexts in context_files:
            source_field_id = p.stem
            if source_field_id not in self.relevant_source_field_ids():
                continue
            dest_field_id = self.idmap["field"].get(source_field_id)
            if not dest_field_id:
                continue
            source_contexts = contexts if isinstance(contexts, list) else unwrap(contexts)
            if not source_contexts:
                continue
            if not self.apply or str(dest_field_id).startswith("<"):
                for ctx in source_contexts:
                    src_ctx_id = str(ctx.get("id", ""))
                    name = str(ctx.get("name", "Default Context"))
                    self.idmap["field_context"][f"{source_field_id}:{src_ctx_id}"] = f"<context:{name}>"
                    self.record(phase, "field context", f"{source_field_id}:{name}", "", "reuse/create and sync options", "planned")
                continue
            try:
                existing = self.c.paginate(f"/rest/api/3/field/{dest_field_id}/context")
            except JiraError as exc:
                self.manual_add(f"Contexts for field {source_field_id} could not be read: {exc.message}")
                continue
            for ctx in source_contexts:
                src_ctx_id = str(ctx.get("id", ""))
                name = str(ctx.get("name", "Default Context"))
                source_global = bool(ctx.get("isGlobalContext"))
                source_any_type = bool(ctx.get("isAnyIssueType"))
                found = first(existing, lambda x: norm(x.get("name")) == norm(name))
                if not found and len(existing) == 1 and source_global and bool(existing[0].get("isGlobalContext")):
                    found = existing[0]
                if found:
                    did = str(found.get("id"))
                    self.record(phase, "field context", f"{source_field_id}:{name}", did, "reuse", "success")
                    if source_field_id not in self.created_fields and (not source_any_type or not source_global):
                        self.manual_add(f"Verify the project/work-type scope for reused field '{source_field_id}'. The importer does not destructively narrow a shared destination field context.")
                else:
                    issue_type_ids: list[str] = []
                    itm = self.b.read(f"data/fields/context_issue_type_mappings/{source_field_id}.json", []) or []
                    for m in itm:
                        if str(m.get("contextId")) == src_ctx_id and m.get("issueTypeId"):
                            mapped = self.idmap["issue_type"].get(str(m.get("issueTypeId")))
                            if mapped:
                                issue_type_ids.append(mapped)
                    body = {
                        "name": name,
                        "description": ctx.get("description", ""),
                        "projectIds": [] if source_global else [self.target_project_id],
                        "issueTypeIds": [] if source_any_type else sorted(set(issue_type_ids)),
                    }
                    result = self.api(phase, "field context", f"{source_field_id}:{name}", "", "create", "POST", f"/rest/api/3/field/{dest_field_id}/context", body)
                    did = str((result or {}).get("id", ""))
                    if did:
                        self.created_contexts.add(f"{dest_field_id}:{did}")
                if did:
                    self.idmap["field_context"][f"{source_field_id}:{src_ctx_id}"] = did
                    self.ensure_options(source_field_id, src_ctx_id, dest_field_id, did)

    def ensure_options(self, sfid: str, scid: str, dfid: str, dcid: str) -> None:
        rel = f"data/fields/options/{sfid}__{scid}.json"
        options = self.b.read(rel, None)
        if not isinstance(options, list) or not options:
            return
        phase = "field-options"

        def fetch() -> list[dict[str, Any]]:
            if not self.apply:
                return []
            try:
                return self.c.paginate(f"/rest/api/3/field/{dfid}/context/{dcid}/option")
            except JiraError as exc:
                self.manual_add(f"Options for field {sfid} context {scid} could not be read: {exc.message}")
                return []

        existing = fetch()
        source_parents = [x for x in options if not x.get("optionId") and not x.get("parentOptionId")]
        source_children = [x for x in options if x.get("optionId") or x.get("parentOptionId")]
        existing_parent_values = {norm(x.get("value")) for x in existing if not x.get("optionId")}

        parent_create = [
            {"value": opt.get("value", ""), "disabled": bool(opt.get("disabled", False))}
            for opt in source_parents
            if norm(opt.get("value")) not in existing_parent_values
        ]
        for batch in chunks(parent_create, 1000):
            self.api(
                phase, "field options", f"{sfid}:{scid}", f"{dfid}:{dcid}", "create parents", "POST",
                f"/rest/api/3/field/{dfid}/context/{dcid}/option", {"options": batch}
            )

        if not self.apply:
            for opt in options:
                if opt.get("id"):
                    self.idmap["field_option"][str(opt.get("id"))] = f"<option:{opt.get('value', '')}>"
            return

        existing = fetch()
        dest_parents_by_value = {
            norm(x.get("value")): x for x in existing if not x.get("optionId")
        }
        for opt in source_parents:
            dest = dest_parents_by_value.get(norm(opt.get("value")))
            if dest and opt.get("id"):
                self.idmap["field_option"][str(opt.get("id"))] = str(dest.get("id"))

        child_create: list[dict[str, Any]] = []
        existing_child_keys = {
            (str(x.get("optionId") or ""), norm(x.get("value"))) for x in existing if x.get("optionId")
        }
        for opt in source_children:
            source_parent = str(opt.get("optionId") or opt.get("parentOptionId") or "")
            dest_parent = self.idmap["field_option"].get(source_parent)
            if not dest_parent:
                self.manual_add(
                    f"Cascading child option '{opt.get('value')}' for field {sfid} could not map parent option {source_parent}."
                )
                continue
            key = (str(dest_parent), norm(opt.get("value")))
            if key in existing_child_keys:
                continue
            child_create.append({
                "value": opt.get("value", ""),
                "disabled": bool(opt.get("disabled", False)),
                "optionId": dest_parent,
            })
        for batch in chunks(child_create, 1000):
            self.api(
                phase, "field options", f"{sfid}:{scid}", f"{dfid}:{dcid}", "create children", "POST",
                f"/rest/api/3/field/{dfid}/context/{dcid}/option", {"options": batch}
            )

        existing = fetch()
        parents = {norm(x.get("value")): x for x in existing if not x.get("optionId")}
        children = {
            (str(x.get("optionId") or ""), norm(x.get("value"))): x
            for x in existing if x.get("optionId")
        }
        for opt in source_parents:
            dest = parents.get(norm(opt.get("value")))
            if dest and opt.get("id"):
                self.idmap["field_option"][str(opt.get("id"))] = str(dest.get("id"))
        for opt in source_children:
            source_parent = str(opt.get("optionId") or opt.get("parentOptionId") or "")
            dest_parent = self.idmap["field_option"].get(source_parent, "")
            dest = children.get((str(dest_parent), norm(opt.get("value"))))
            if dest and opt.get("id"):
                self.idmap["field_option"][str(opt.get("id"))] = str(dest.get("id"))

    # ---------- screens ----------
    def relevant_issue_type_screen_scheme(self) -> dict[str, Any]:
        assoc = self.b.read(f"data/projects/{self.source_key}/issue_type_screen_scheme_association.json", {}) or {}
        vals = unwrap(assoc)
        return ((vals[0] if vals else {}).get("issueTypeScreenScheme") or {})

    def relevant_screen_scheme_ids(self) -> set[str]:
        itss = self.relevant_issue_type_screen_scheme()
        sid = str(itss.get("id", ""))
        mappings = self.b.read("data/screens/issue_type_screen_scheme_mappings.json", []) or []
        return {str(x.get("screenSchemeId")) for x in mappings if str(x.get("issueTypeScreenSchemeId")) == sid}

    def relevant_screen_ids(self) -> set[str]:
        ssids = self.relevant_screen_scheme_ids()
        schemes = self.b.read("data/screens/screen_schemes.json", []) or []
        out: set[str] = set()
        for s in schemes:
            if str(s.get("id")) in ssids:
                out.update(str(v) for v in (s.get("screens") or {}).values())
        return out

    def ensure_screens_and_schemes(self) -> None:
        phase = "screens"
        source_screens = self.b.read("data/screens/screens.json", []) or []
        try:
            dest_screens = self.c.paginate("/rest/api/3/screens")
        except JiraError:
            dest_screens = []

        # First reproduce the actual screens and every exported field on their tabs.
        for sid in sorted(self.relevant_screen_ids()):
            src = first(source_screens, lambda x: str(x.get("id")) == sid)
            if not src:
                continue
            name = str(src.get("name")); found = first(dest_screens, lambda x: norm(x.get("name")) == norm(name))
            created = False
            if found:
                did = str(found.get("id")); self.record(phase, "screen", name, did, "reuse", "success")
            else:
                result = self.api(phase, "screen", name, "", "create", "POST", "/rest/api/3/screens",
                                  {"name": name, "description": src.get("description", "")})
                did = str((result or {}).get("id", "")) if self.apply else f"<new-screen:{name}>"; created = True
            if not did:
                continue
            self.idmap["screen"][sid] = did
            tabs = self.b.read(f"data/screens/tabs/{sid}.json", []) or []
            if not self.apply:
                for tab in tabs:
                    stid = str(tab.get("id", "")); tname = str(tab.get("name", "Field Tab"))
                    self.idmap["screen_tab"][f"{sid}:{stid}"] = f"<new-tab:{tname}>"
                    self.record(phase, "screen tab and fields", f"{name}/{tname}", "", "sync", "planned")
                continue
            try:
                existing_tabs = self.c.get(f"/rest/api/3/screens/{did}/tabs") or []
            except JiraError:
                existing_tabs = []
            for index, tab in enumerate(tabs):
                tname = str(tab.get("name", "Field Tab")); stid = str(tab.get("id", ""))
                dtab = first(existing_tabs, lambda x: norm(x.get("name")) == norm(tname))
                if not dtab and created and index == 0 and existing_tabs:
                    dtab = existing_tabs[0]; old_name = str(dtab.get("name", ""))
                    if norm(old_name) != norm(tname):
                        self.api(phase, "screen tab", f"{name}/{old_name}", tname, "rename", "PUT",
                                 f"/rest/api/3/screens/{did}/tabs/{dtab.get('id')}", {"name": tname})
                        dtab = dict(dtab); dtab["name"] = tname
                if not dtab:
                    dtab = self.api(phase, "screen tab", f"{name}/{tname}", "", "create", "POST",
                                    f"/rest/api/3/screens/{did}/tabs", {"name": tname}) or {}
                    existing_tabs.append(dtab)
                dtid = str(dtab.get("id", ""))
                if not dtid:
                    continue
                self.idmap["screen_tab"][f"{sid}:{stid}"] = dtid
                try:
                    existing_fields = self.c.get(f"/rest/api/3/screens/{did}/tabs/{dtid}/fields") or []
                except JiraError:
                    existing_fields = []
                present = {str(x.get("id")) for x in existing_fields}
                fields = self.b.read(f"data/screens/tab_fields/{sid}__{stid}.json", []) or []
                for field in fields:
                    sfid = str(field.get("id"))
                    dfid = self.idmap["field"].get(sfid, sfid if not sfid.startswith("customfield_") else "")
                    if not dfid:
                        self.manual_add(f"Field '{field.get('name')}' could not be added to screen '{name}' because it has no destination mapping.")
                        continue
                    if str(dfid) in present:
                        continue
                    self.api(phase, "screen field", f"{name}/{field.get('name')}", dfid, "add", "POST",
                             f"/rest/api/3/screens/{did}/tabs/{dtid}/fields", {"fieldId": dfid})
                    present.add(str(dfid))

        # Then synchronize screen schemes. v2.8 merely reused same-name Jira
        # template schemes, which could leave Create/Edit/View pointing elsewhere.
        source_schemes = self.b.read("data/screens/screen_schemes.json", []) or []
        try:
            dest_schemes = self.c.paginate("/rest/api/3/screenscheme")
        except JiraError:
            dest_schemes = []
        for src in source_schemes:
            ssid = str(src.get("id"))
            if ssid not in self.relevant_screen_scheme_ids():
                continue
            name = str(src.get("name"))
            mapped_screens = {k: self.idmap["screen"].get(str(v)) for k, v in (src.get("screens") or {}).items()}
            mapped_screens = {k: v for k, v in mapped_screens.items() if v}
            found = first(dest_schemes, lambda x: norm(x.get("name")) == norm(name))
            if found:
                did = str(found.get("id"))
                self.record(phase, "screen scheme", name, did, "reuse and synchronize", "success")
                self.api(phase, "screen scheme mappings", name, did, "synchronize", "PUT",
                         f"/rest/api/3/screenscheme/{did}",
                         {"name": name, "description": src.get("description", ""), "screens": mapped_screens}, optional=False)
            else:
                result = self.api(phase, "screen scheme", name, "", "create", "POST", "/rest/api/3/screenscheme",
                                  {"name": name, "description": src.get("description", ""), "screens": mapped_screens})
                did = str((result or {}).get("id", "")) if self.apply else f"<new-screen-scheme:{name}>"
            if did:
                self.idmap["screen_scheme"][ssid] = did

        # Finally repair the issue-type -> screen-scheme routing itself. This is the
        # part that decides what fields actually appear when Create is opened.
        src_itss = self.relevant_issue_type_screen_scheme()
        src_id = str(src_itss.get("id", "")); name = str(src_itss.get("name", f"{self.target_key}: Issue Type Screen Scheme"))
        maps = self.b.read("data/screens/issue_type_screen_scheme_mappings.json", []) or []
        desired_maps: list[dict[str, str]] = []
        for mapping in maps:
            if str(mapping.get("issueTypeScreenSchemeId")) != src_id:
                continue
            sit = str(mapping.get("issueTypeId"))
            dit = "default" if sit == "default" else self.idmap["issue_type"].get(sit)
            dss = self.idmap["screen_scheme"].get(str(mapping.get("screenSchemeId")))
            if dit and dss:
                desired_maps.append({"issueTypeId": str(dit), "screenSchemeId": str(dss)})

        try:
            dest_itss = self.c.paginate("/rest/api/3/issuetypescreenscheme")
        except JiraError:
            dest_itss = []
        found = first(dest_itss, lambda x: norm(x.get("name")) == norm(name))
        if found:
            did = str(found.get("id")); self.record(phase, "work type screen scheme", name, did, "reuse and synchronize", "success")
            self.api(phase, "work type screen scheme metadata", name, did, "synchronize", "PUT",
                     f"/rest/api/3/issuetypescreenscheme/{did}",
                     {"name": name, "description": src_itss.get("description", "")}, optional=False)
            if not self.apply:
                self.record(phase, "work type screen scheme mappings", name, did, "synchronize", "planned")
            else:
                try:
                    current_all = self.c.paginate(
                        "/rest/api/3/issuetypescreenscheme/mapping",
                        params={"issueTypeScreenSchemeId": [int(did)]} if did.isdigit() else None,
                    )
                except JiraError:
                    current_all = self.c.paginate("/rest/api/3/issuetypescreenscheme/mapping")
                current = {
                    str(x.get("issueTypeId")): str(x.get("screenSchemeId"))
                    for x in current_all if str(x.get("issueTypeScreenSchemeId")) == did
                }
                desired = {x["issueTypeId"]: x["screenSchemeId"] for x in desired_maps}
                default_ss = desired.get("default")
                if default_ss:
                    self.api(phase, "default screen scheme mapping", name, default_ss, "synchronize", "PUT",
                             f"/rest/api/3/issuetypescreenscheme/{did}/mapping/default",
                             {"screenSchemeId": default_ss}, optional=False)

                remove_ids = [
                    issue_type_id for issue_type_id, current_ss in current.items()
                    if issue_type_id != "default" and (issue_type_id not in desired or desired.get(issue_type_id) != current_ss)
                ]
                if remove_ids:
                    self.api(phase, "work type screen mappings", ",".join(remove_ids), did, "remove stale mappings", "POST",
                             f"/rest/api/3/issuetypescreenscheme/{did}/mapping/remove",
                             {"issueTypeIds": remove_ids}, optional=False)
                append = [
                    {"issueTypeId": issue_type_id, "screenSchemeId": screen_scheme_id}
                    for issue_type_id, screen_scheme_id in desired.items()
                    if issue_type_id != "default" and current.get(issue_type_id) != screen_scheme_id
                ]
                if append:
                    self.api(phase, "work type screen mappings", name, did, "append source mappings", "PUT",
                             f"/rest/api/3/issuetypescreenscheme/{did}/mapping",
                             {"issueTypeMappings": append}, optional=False)
        else:
            result = self.api(phase, "work type screen scheme", name, "", "create", "POST",
                              "/rest/api/3/issuetypescreenscheme",
                              {"name": name, "description": src_itss.get("description", ""), "issueTypeMappings": desired_maps})
            did = str((result or {}).get("id", "")) if self.apply else f"<new-itss:{name}>"
        if did:
            self.idmap["issue_type_screen_scheme"][src_id] = did
            self.api(phase, "work type screen scheme association", name, self.target_key, "associate", "PUT",
                     "/rest/api/3/issuetypescreenscheme/project",
                     {"issueTypeScreenSchemeId": did, "projectId": self.target_project_id}, optional=False)

    def relevant_workflow_scheme(self) -> dict[str, Any]:
        assoc = self.b.read(f"data/projects/{self.source_key}/workflow_scheme_association.json", {}) or {}
        vals = assoc.get("values") if isinstance(assoc, dict) else []
        return ((vals[0] if vals else {}).get("workflowScheme") or {})

    def relevant_workflow_names(self) -> set[str]:
        scheme = self.relevant_workflow_scheme()
        names = set(str(v) for v in (scheme.get("issueTypeMappings") or {}).values())
        default = str(scheme.get("defaultWorkflow") or "")
        if default: names.add(default)
        return names

    def workflow_bulk(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        statuses: dict[str, dict[str, Any]] = {}; workflows: dict[str, dict[str, Any]] = {}
        for _, payload in self.b.glob_json("data/workflows/bulk_read/*.json"):
            if not isinstance(payload, dict): continue
            for s in payload.get("statuses", []): statuses[str(s.get("statusReference") or s.get("id"))] = s
            for w in payload.get("workflows", []): workflows[str(w.get("name"))] = w
        return list(statuses.values()), list(workflows.values())

    @staticmethod
    def _workflow_scheme_matches(active: Mapping[str, Any], desired_mappings: Mapping[str, str], default_workflow: str) -> bool:
        current = {str(k): str(v) for k, v in (active.get("issueTypeMappings") or {}).items()}
        # Exactness is enforced for the work types captured from the source. Extra
        # mappings for unrelated destination work types are intentionally ignored.
        mappings_ok = all(current.get(str(k)) == str(v) for k, v in desired_mappings.items())
        default_ok = (not default_workflow) or str(active.get("defaultWorkflow") or "") == default_workflow
        return mappings_ok and default_ok

    def _destination_workflow_statuses(self, workflow_name_value: str) -> tuple[list[dict[str, Any]], str]:
        """Return destination status metadata for a workflow and its initial status ID.

        Jira's draft-publish API requires explicit old->new status mappings when
        an issue type is moved to a workflow whose status set is different.  The
        legacy workflow/search response is still used when available because it
        contains the classic workflow's status IDs and initial transition.  A
        fallback to the current workflows/search API keeps this working on sites
        where the legacy endpoint has finally disappeared.
        """
        wanted = norm(workflow_name_value)

        def parse_old(item: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
            rows: list[dict[str, Any]] = []
            for st in item.get("statuses", []) or []:
                if not isinstance(st, Mapping):
                    continue
                sid = str(st.get("id") or st.get("statusReference") or "")
                if not sid:
                    continue
                rows.append({"id": sid, "name": str(st.get("name") or sid)})
            initial = ""
            for tr in item.get("transitions", []) or []:
                if not isinstance(tr, Mapping):
                    continue
                if str(tr.get("type") or "").upper() == "INITIAL":
                    initial = str(tr.get("to") or tr.get("toStatusReference") or "")
                    if initial:
                        break
            return rows, initial

        for item in self.dest_workflows:
            if norm(workflow_name(item)) == wanted:
                rows, initial = parse_old(item)
                if rows:
                    return rows, initial

        # Legacy classic endpoint.  It still exists on many Jira Cloud tenants,
        # even though Atlassian marks it deprecated.
        try:
            found = self.c.paginate(
                "/rest/api/3/workflow/search",
                params={"workflowName": [workflow_name_value], "expand": "statuses,transitions"},
            )
            for item in found:
                if isinstance(item, Mapping) and norm(workflow_name(item)) == wanted:
                    rows, initial = parse_old(item)
                    if rows:
                        self.dest_workflows.append(dict(item))
                        return rows, initial
        except JiraError:
            pass

        # Current workflow API.  Its workflow status entries reference the
        # response's top-level status catalogue by statusReference.
        try:
            payload = self.c.get("/rest/api/3/workflows/search", params={"queryString": workflow_name_value, "maxResults": 100}) or {}
            values = payload.get("values", []) if isinstance(payload, Mapping) else []
            catalogue = {
                str(x.get("statusReference") or x.get("id")): x
                for x in (payload.get("statuses", []) if isinstance(payload, Mapping) else [])
                if isinstance(x, Mapping)
            }
            for item in values:
                if not isinstance(item, Mapping) or norm(item.get("name")) != wanted:
                    continue
                rows: list[dict[str, Any]] = []
                for st in item.get("statuses", []) or []:
                    if not isinstance(st, Mapping):
                        continue
                    ref = str(st.get("statusReference") or st.get("id") or "")
                    meta = catalogue.get(ref, {})
                    sid = str(meta.get("id") or ref)
                    if sid:
                        rows.append({"id": sid, "name": str(meta.get("name") or sid)})
                initial = ""
                for tr in item.get("transitions", []) or []:
                    if isinstance(tr, Mapping) and str(tr.get("type") or "").upper() == "INITIAL":
                        ref = str(tr.get("toStatusReference") or tr.get("to") or "")
                        initial = str((catalogue.get(ref) or {}).get("id") or ref)
                        break
                if rows:
                    return rows, initial
        except JiraError:
            pass
        return [], ""

    def _status_category_for_id(self, status_id: str) -> str:
        for st in self.dest_statuses:
            if str(st.get("id")) != str(status_id):
                continue
            cat = st.get("statusCategory") or st.get("statusCategoryKey") or ""
            if isinstance(cat, Mapping):
                cat = cat.get("key") or cat.get("name") or ""
            return normalize_status_category(cat)
        return "TODO"

    def _choose_workflow_migration_status(self, old_status: Mapping[str, Any],
                                          target_statuses: Sequence[Mapping[str, Any]],
                                          target_initial: str) -> str:
        old_id = str(old_status.get("id") or "")
        old_name = norm(old_status.get("name"))
        by_id = {str(x.get("id")): x for x in target_statuses}
        if old_id and old_id in by_id:
            return old_id
        for st in target_statuses:
            if old_name and norm(st.get("name")) == old_name:
                return str(st.get("id"))

        old_cat = self._status_category_for_id(old_id)
        same_cat = [str(x.get("id")) for x in target_statuses if self._status_category_for_id(str(x.get("id"))) == old_cat]
        if target_initial and target_initial in same_cat:
            return target_initial
        if same_cat:
            return same_cat[0]

        # If the target workflow has no In Progress status (the source Epic
        # workflow intentionally has only backlog + done), moving an existing
        # in-progress item to a non-final TODO status is safer than marking it
        # done.  The initial status is preferred when possible.
        if old_cat == "IN_PROGRESS":
            todo = [str(x.get("id")) for x in target_statuses if self._status_category_for_id(str(x.get("id"))) == "TODO"]
            if target_initial and target_initial in todo:
                return target_initial
            if todo:
                return todo[0]

        if target_initial and target_initial in by_id:
            return target_initial
        return str(target_statuses[0].get("id")) if target_statuses else ""

    def _build_workflow_scheme_publish_status_mappings(self, active: Mapping[str, Any],
                                                       desired_mappings: Mapping[str, str],
                                                       default_workflow: str) -> list[dict[str, str]]:
        """Build Jira classic draft-publish status mappings for changed work types."""
        current_mappings = {str(k): str(v) for k, v in (active.get("issueTypeMappings") or {}).items()}
        current_default = str(active.get("defaultWorkflow") or "jira")
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for issue_type_id, new_workflow in desired_mappings.items():
            old_workflow = current_mappings.get(str(issue_type_id)) or current_default
            if norm(old_workflow) == norm(new_workflow):
                continue
            old_statuses, _ = self._destination_workflow_statuses(old_workflow)
            new_statuses, new_initial = self._destination_workflow_statuses(new_workflow)
            if not old_statuses or not new_statuses:
                raise RuntimeError(
                    f"Cannot calculate Jira status migration for issue type {issue_type_id}: "
                    f"could not read statuses for '{old_workflow}' -> '{new_workflow}'."
                )
            target_ids = {str(x.get("id")) for x in new_statuses}
            for old_status in old_statuses:
                old_id = str(old_status.get("id") or "")
                if not old_id or old_id in target_ids:
                    continue
                key = (str(issue_type_id), old_id)
                if key in seen:
                    continue
                new_id = self._choose_workflow_migration_status(old_status, new_statuses, new_initial)
                if not new_id:
                    raise RuntimeError(
                        f"Cannot calculate Jira status migration for issue type {issue_type_id}, status {old_id}."
                    )
                out.append({
                    "issueTypeId": str(issue_type_id),
                    "statusId": old_id,
                    "newStatusId": new_id,
                })
                seen.add(key)
                self.record(
                    "workflows", "workflow status migration",
                    f"{issue_type_id}:{old_status.get('name') or old_id}", new_id,
                    f"{old_workflow} -> {new_workflow}", "success",
                    f"old status {old_id} maps to destination status {new_id}",
                )
        return out

    def _publish_workflow_scheme_draft_and_wait(self, scheme_id: str, scheme_name: str,
                                                status_mappings: Sequence[Mapping[str, str]]) -> None:
        """Validate and publish a workflow-scheme draft, then follow Jira's async task."""
        body = {"statusMappings": list(status_mappings)}
        publish_path = f"/rest/api/3/workflowscheme/{scheme_id}/draft/publish"

        # Validation catches missing status migrations before Jira starts the
        # asynchronous publish operation and gives a much clearer error report.
        self.c.request(
            "POST", publish_path,
            params={"validateOnly": "true"}, body=body,
            expected=(204,), allow_redirects=False,
        )
        self.record("workflows", "workflow scheme draft", scheme_name, scheme_id,
                    "validate publish status mappings", "success",
                    f"{len(status_mappings)} migration mapping(s)")

        payload, headers, status = self.c.request(
            "POST", publish_path, body=body,
            expected=(200, 201, 202, 204, 303), return_meta=True,
            allow_redirects=False,
        )
        location = str(headers.get("Location") or headers.get("location") or "")
        if not location and isinstance(payload, Mapping):
            location = str(payload.get("self") or "")
        self.record("workflows", "workflow scheme draft", scheme_name, scheme_id, "publish", "success",
                    f"HTTP {status}; async task: {location or 'location header not returned'}")
        if not location:
            return
        last: Any = None
        for _ in range(90):
            last = self.c.get(location)
            task_status = str((last or {}).get("status") or "").upper() if isinstance(last, dict) else ""
            if task_status == "COMPLETE":
                self.record("workflows", "workflow scheme publish task", scheme_name, scheme_id, "wait", "success")
                return
            if task_status in {"FAILED", "CANCELLED", "CANCELED"}:
                detail = json.dumps(last, ensure_ascii=False)[:2000]
                raise RuntimeError(f"Jira workflow-scheme publish task ended as {task_status}: {detail}")
            time.sleep(2)
        detail = json.dumps(last, ensure_ascii=False)[:2000] if last is not None else "no task response"
        raise RuntimeError(f"Timed out waiting for Jira workflow-scheme publish task: {detail}")

    def _repair_active_workflow_scheme(self, scheme_id: str, scheme_name: str,
                                       desired_mappings: Mapping[str, str], default_workflow: str) -> None:
        """Repair missing/wrong active mappings through Jira's draft endpoints."""
        # Capture the pre-draft active mappings.  Jira needs old->new status
        # migration instructions for every work type whose new workflow does not
        # contain all statuses from its currently active workflow.
        active_before = self.c.get(f"/rest/api/3/workflowscheme/{scheme_id}") or {}
        status_mappings = self._build_workflow_scheme_publish_status_mappings(
            active_before, desired_mappings, default_workflow
        )

        try:
            draft = self.c.get(f"/rest/api/3/workflowscheme/{scheme_id}/draft") or {}
        except JiraError as exc:
            if exc.status != 404:
                raise
            draft = self.c.request("POST", f"/rest/api/3/workflowscheme/{scheme_id}/createdraft") or {}
            self.record("workflows", "workflow scheme draft", scheme_name, scheme_id, "create", "success")

        for issue_type_id, workflow_name_value in desired_mappings.items():
            self.c.put(
                f"/rest/api/3/workflowscheme/{scheme_id}/draft/issuetype/{issue_type_id}",
                {
                    "issueType": str(issue_type_id),
                    "workflow": str(workflow_name_value),
                    "updateDraftIfNeeded": False,
                },
            )
            self.record("workflows", "workflow mapping draft", issue_type_id, workflow_name_value, "set", "success")
        if default_workflow:
            self.c.put(
                f"/rest/api/3/workflowscheme/{scheme_id}/draft/default",
                {"workflow": default_workflow, "updateDraftIfNeeded": False},
            )

        draft = self.c.get(f"/rest/api/3/workflowscheme/{scheme_id}/draft") or {}
        if not self._workflow_scheme_matches(draft, desired_mappings, default_workflow):
            current = json.dumps(draft.get("issueTypeMappings") or {}, ensure_ascii=False)
            raise RuntimeError(
                f"Jira draft for workflow scheme '{scheme_name}' does not contain all requested mappings before publish. "
                f"Draft mappings: {current}"
            )
        self.record("workflows", "workflow scheme draft mappings", scheme_name, scheme_id, "verify", "success")
        self._publish_workflow_scheme_draft_and_wait(scheme_id, scheme_name, status_mappings)

        for _ in range(30):
            active = self.c.get(f"/rest/api/3/workflowscheme/{scheme_id}") or {}
            if self._workflow_scheme_matches(active, desired_mappings, default_workflow):
                self.record("workflows", "workflow scheme activation", scheme_name, scheme_id, "verify active mappings", "success")
                return
            time.sleep(2)
        active = self.c.get(f"/rest/api/3/workflowscheme/{scheme_id}") or {}
        raise RuntimeError(
            f"Workflow scheme '{scheme_name}' was published but its active mappings still do not match the source. "
            f"Active mappings: {json.dumps(active.get('issueTypeMappings') or {}, ensure_ascii=False)}"
        )

    def ensure_workflows_and_scheme(self) -> None:
        phase = "workflows"
        names = self.relevant_workflow_names()
        source_statuses, source_workflows = self.workflow_bulk()
        existing_workflows = {norm(workflow_name(x)): x for x in self.dest_workflows if workflow_name(x)}
        source_status_by_ref = {str(x.get("statusReference") or x.get("id")): x for x in source_statuses}
        dest_status_by_name = {norm(x.get("name")): x for x in self.dest_statuses}
        to_create: list[dict[str, Any]] = []
        needed_refs: set[str] = set()
        for w in source_workflows:
            name = str(w.get("name"))
            if name not in names:
                continue
            if norm(name) in existing_workflows:
                self.idmap["workflow"][name] = name
                self.record(phase, "workflow", name, name, "reuse", "success", "Existing workflows are not overwritten; verify if this is not a rerun of this importer.")
                continue
            if norm(name) in {"jira", "classic default workflow"}:
                self.idmap["workflow"][name] = name
                self.record(phase, "workflow", name, name, "reuse destination default", "success")
                continue
            clean = clean_workflow(w, self.idmap)
            to_create.append(clean)
            for st in clean.get("statuses", []):
                needed_refs.add(str(st.get("statusReference")))

        if to_create:
            status_refmap: dict[str, str] = {}
            status_defs: list[dict[str, Any]] = []
            for ref in sorted(needed_refs):
                src = source_status_by_ref.get(ref)
                if not src:
                    self.manual_add(f"Workflow references status {ref}, but its definition was not exported.")
                    continue
                name = str(src.get("name", ref))
                dest = dest_status_by_name.get(norm(name))

                # Jira's bulk workflow create API requires statusReference to be a
                # bare UUID generated for this request. It is a request-local link,
                # not the Jira numeric status ID and not a prefixed label.
                local_ref = str(uuid.uuid4())
                status_refmap[ref] = local_ref

                status_def: dict[str, Any] = {
                    "name": name,
                    "description": src.get("description", ""),
                    "statusCategory": normalize_status_category(src.get("statusCategory")),
                    "statusReference": local_ref,
                }
                if dest:
                    did = str(dest.get("id"))
                    status_def["id"] = did
                    self.idmap["status"][ref] = did
                    source_cat = normalize_status_category(src.get("statusCategory"))
                    dest_cat = normalize_status_category((dest.get("statusCategory") or {}).get("key") or (dest.get("statusCategory") or {}).get("name"))
                    if source_cat != dest_cat:
                        self.manual_add(f"Status '{name}' exists with category {dest_cat}, while the source uses {source_cat}. Jira status names are site-wide, so the existing status is reused.")
                        # Do not attempt to change a shared destination status's category.
                        status_def["statusCategory"] = dest_cat
                        status_def["description"] = dest.get("description", status_def["description"])
                else:
                    # This is temporary until Jira returns the real numeric status ID.
                    self.idmap["status"][ref] = local_ref
                status_defs.append(status_def)
            deep_replace_status_refs(to_create, status_refmap)
            payload = {"scope": {"type": "GLOBAL"}, "statuses": status_defs, "workflows": to_create}
            (self.out / "workflow_payload.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            has_placeholders = "<new-" in json.dumps(payload)
            if not has_placeholders:
                try:
                    validation = self.c.post("/rest/api/3/workflows/create/validation", {"payload": payload, "validationOptions": {"levels": ["ERROR", "WARNING"]}})
                    (self.out / "workflow_validation.json").write_text(json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8")
                    validation_errors = [
                        e for e in (validation.get("errors", []) if isinstance(validation, dict) else [])
                        if str(e.get("level", "")).upper() == "ERROR"
                    ]
                    if validation_errors:
                        messages = "; ".join(str(e.get("message") or e.get("code") or e) for e in validation_errors[:10])
                        self.record(phase, "workflow payload", len(to_create), "workflow_validation.json", "validate", "failed", messages)
                        self.manual_add(f"Jira rejected the workflow payload during validation: {messages}")
                        raise RuntimeError(f"Jira workflow validation failed: {messages}")
                    warning_count = sum(
                        1 for e in (validation.get("errors", []) if isinstance(validation, dict) else [])
                        if str(e.get("level", "")).upper() == "WARNING"
                    )
                    note = f"{warning_count} warning(s) returned." if warning_count else "No validation errors returned."
                    self.record(phase, "workflow payload", len(to_create), "workflow_validation.json", "validate", "success", note)
                except JiraError as exc:
                    self.record(phase, "workflow payload", len(to_create), "", "validate", "failed" if self.apply else "warning", f"HTTP {exc.status}: {exc.message}")
                    self.manual_add(f"Workflow validation returned an HTTP error: {exc.message}. Review workflow_payload.json.")
                    if self.apply:
                        raise RuntimeError(f"Workflow validation could not be completed safely: HTTP {exc.status} - {exc.message}") from exc
            else:
                self.record(phase, "workflow payload", len(to_create), "workflow_payload.json", "validate", "planned", "Live validation is deferred until newly-created custom fields have real destination IDs.")
            result = self.api(phase, "workflows", ", ".join(w.get("name", "") for w in to_create), "", "create", "POST", "/rest/api/3/workflows/create", payload, optional=False)
            for w in to_create:
                self.idmap["workflow"][str(w.get("name"))] = str(w.get("name"))
            if self.apply:
                self.dest_statuses = self.c.get("/rest/api/3/status") or self.dest_statuses
                refreshed = {norm(x.get("name")): x for x in self.dest_statuses}
                for ref, src in source_status_by_ref.items():
                    dest = refreshed.get(norm(src.get("name")))
                    if dest:
                        self.idmap["status"][ref] = str(dest.get("id"))

        scheme = self.relevant_workflow_scheme()
        source_id = str(scheme.get("id", "")); name = str(scheme.get("name", f"{self.target_key}: Workflow Scheme"))
        desired_mappings: dict[str, str] = {}
        for sit, wf_name in (scheme.get("issueTypeMappings") or {}).items():
            dit = self.idmap["issue_type"].get(str(sit))
            if dit and (wf_name in self.idmap["workflow"] or norm(wf_name) in existing_workflows):
                desired_mappings[str(dit)] = str(wf_name)
        default_wf = str(scheme.get("defaultWorkflow") or "")
        desired_body: dict[str, Any] = {
            "name": name, "description": scheme.get("description", ""),
            "issueTypeMappings": desired_mappings,
        }
        if default_wf:
            desired_body["defaultWorkflow"] = default_wf

        try:
            existing_schemes = unwrap(self.c.get("/rest/api/3/workflowscheme"))
        except JiraError:
            existing_schemes = []
        found = first(existing_schemes, lambda x: norm(x.get("name")) == norm(name))
        if found:
            did = str(found.get("id"))
            self.record(phase, "workflow scheme", name, did, "reuse and synchronize", "success")
            if not self.apply:
                self.record(phase, "workflow scheme mappings", name, did, "associate, then verify/repair each mapping", "planned")
            else:
                current = self.c.get(f"/rest/api/3/workflowscheme/{did}") or {}
                if self._workflow_scheme_matches(current, desired_mappings, default_wf):
                    self.record(phase, "workflow scheme mappings", name, did, "already synchronized", "success")
                else:
                    self.record(phase, "workflow scheme mappings", name, did,
                                "defer explicit repair until after project association", "warning")
        else:
            result = self.api(phase, "workflow scheme", name, "", "create", "POST", "/rest/api/3/workflowscheme",
                              desired_body, optional=False)
            did = str((result or {}).get("id", "")) if self.apply else f"<new-workflow-scheme:{name}>"

        if did:
            self.idmap["workflow_scheme"][source_id] = did
            self.api(phase, "workflow scheme association", name, self.target_key, "associate", "PUT",
                     "/rest/api/3/workflowscheme/project",
                     {"projectId": self.target_project_id, "workflowSchemeId": did}, optional=False)

            # Association is the final authority. Re-read the active scheme after
            # association and repair every source mapping explicitly if Jira did
            # not activate the whole-scheme update. This catches the exact v2.9
            # failure where Epic was absent while Task/Planned Task/Sub-Task were
            # active.
            if self.apply:
                active = self.c.get(f"/rest/api/3/workflowscheme/{did}") or {}
                if not self._workflow_scheme_matches(active, desired_mappings, default_wf):
                    missing = {k: v for k, v in desired_mappings.items() if str((active.get("issueTypeMappings") or {}).get(k)) != str(v)}
                    self.record(phase, "workflow scheme post-association", name, did, "repair missing/wrong mappings", "warning",
                                json.dumps(missing, ensure_ascii=False))
                    self._repair_active_workflow_scheme(did, name, desired_mappings, default_wf)
                else:
                    self.record(phase, "workflow scheme post-association", name, did, "verify active mappings", "success")

    def ensure_field_configurations(self) -> None:
        """Replicate source field behavior on either Jira field model.

        Jira Cloud sites can currently expose either:
        - legacy Field Configurations + Field Configuration Schemes, or
        - the newer unified Field Schemes API.

        Auto mode probes both models, chooses the one associated with the target
        project, and performs one guarded fallback when Jira explicitly says the
        other model is required. The source legacy configuration is first treated
        as a canonical per-field/per-work-type behavior model, then rendered into
        the destination model.
        """
        phase = "field-model"
        detection = self.detect_destination_field_model()
        selected = str(detection["selectedDestinationModel"])
        order = [selected]
        alternate = "legacy" if selected == "new" else "new"
        if detection.get("probes", {}).get(alternate, {}).get("available"):
            order.append(alternate)

        last_error: Exception | None = None
        for index, model in enumerate(order):
            try:
                if model == "new":
                    try:
                        destination_schemes = self.c.paginate("/rest/api/3/config/fieldschemes")
                    except JiraError as exc:
                        if requires_legacy_field_configuration(exc) or is_field_model_unavailable_error(exc):
                            raise FieldModelMismatch("new", "legacy", exc) from exc
                        raise
                    self.record(
                        phase, "destination field model", "unified Field Schemes", "new",
                        "select", "success",
                        "Source field behavior will be translated into the destination Field Scheme model.",
                    )
                    self.ensure_new_field_scheme_from_legacy(destination_schemes)
                else:
                    self.record(
                        phase, "destination field model", "Field Configurations", "legacy",
                        "select", "success",
                        "Source field configurations will be recreated using the legacy APIs.",
                    )
                    self.ensure_legacy_field_configurations()
                self.field_model_used = model
                self.field_model_detection["selectedDestinationModel"] = model
                self.ensure_custom_field_project_associations()
                if model == "legacy":
                    self.sync_legacy_items_after_project_association()
                return
            except FieldModelMismatch as exc:
                last_error = exc
                fallback = exc.suggested_model
                self.field_model_detection["fallbacks"].append({
                    "from": exc.attempted_model,
                    "to": fallback,
                    "status": exc.error.status,
                    "message": exc.error.message,
                })
                self.record(
                    phase, "destination field model", exc.attempted_model, fallback,
                    "fallback", "warning",
                    f"Jira rejected the {exc.attempted_model} model: HTTP {exc.error.status} - {exc.error.message}",
                )
                if self.field_model_preference != "auto":
                    raise RuntimeError(
                        f"--field-model {self.field_model_preference} was forced, but Jira requires {fallback}. "
                        "Rerun with --field-model auto or the required model."
                    ) from exc
                if index + 1 >= len(order) or order[index + 1] != fallback:
                    if self.field_model_detection.get("probes", {}).get(fallback, {}).get("available"):
                        order.append(fallback)
                    else:
                        raise RuntimeError(
                            f"Jira requires the {fallback} field model, but its API could not be read during capability detection."
                        ) from exc
                continue
            except JiraError as exc:
                last_error = exc
                raise

        raise RuntimeError(f"Unable to apply either Jira field model: {last_error}")

    def detect_destination_field_model(self) -> dict[str, Any]:
        phase = "field-model"

        def probe(label: str, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
            try:
                payload = self.c.get(path, params=params)
                result = {
                    "available": True,
                    "status": 200,
                    "message": "read succeeded",
                    "sampleCount": len(unwrap(payload)) if isinstance(payload, (dict, list)) else None,
                    "_payload": payload,
                }
                self.record(phase, f"{label} API", path, "available", "probe", "success")
                return result
            except JiraError as exc:
                result = {
                    "available": False,
                    "status": exc.status,
                    "message": exc.message,
                }
                status = "warning" if is_field_model_unavailable_error(exc) else "failed"
                self.record(phase, f"{label} API", path, "unavailable", "probe", status,
                            f"HTTP {exc.status}: {exc.message}")
                return result

        new_probe = probe("new Field Schemes", "/rest/api/3/config/fieldschemes", {"startAt": 0, "maxResults": 1})
        legacy_config_probe = probe("legacy Field Configurations", "/rest/api/3/fieldconfiguration", {"startAt": 0, "maxResults": 1})
        legacy_scheme_probe = probe("legacy Field Configuration Schemes", "/rest/api/3/fieldconfigurationscheme", {"startAt": 0, "maxResults": 1})
        legacy_available = bool(legacy_config_probe.get("available") and legacy_scheme_probe.get("available"))

        new_assoc: dict[str, Any] = {"available": False}
        legacy_assoc: dict[str, Any] = {"available": False}
        if new_probe.get("available"):
            new_assoc = probe(
                "new Field Scheme project association",
                "/rest/api/3/config/fieldschemes/projects",
                {"projectId": self.target_project_id, "startAt": 0, "maxResults": 100},
            )
        if legacy_available:
            legacy_assoc = probe(
                "legacy Field Configuration Scheme project association",
                "/rest/api/3/fieldconfigurationscheme/project",
                {"projectId": self.target_project_id, "startAt": 0, "maxResults": 100},
            )

        new_rows = unwrap(new_assoc.get("_payload"), ("values", "projects"))
        legacy_rows = unwrap(legacy_assoc.get("_payload"), ("values",))
        new_project_match = any(
            str(row.get("projectId")) == str(self.target_project_id)
            or str(self.target_project_id) in {str(x) for x in (row.get("projectIds") or [])}
            for row in new_rows if isinstance(row, dict)
        )
        legacy_project_match = any(
            str(self.target_project_id) in {str(x) for x in (row.get("projectIds") or [])}
            for row in legacy_rows if isinstance(row, dict)
        )

        def public_probe(value: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in value.items() if k != "_payload"}

        probes = {
            "new": {
                **public_probe(new_probe),
                "projectAssociationReadable": bool(new_assoc.get("available")),
                "targetProjectAssociated": new_project_match,
            },
            "legacy": {
                "available": legacy_available,
                "fieldConfigurations": public_probe(legacy_config_probe),
                "fieldConfigurationSchemes": public_probe(legacy_scheme_probe),
                "projectAssociationReadable": bool(legacy_assoc.get("available")),
                "targetProjectAssociated": legacy_project_match,
            },
        }
        self.field_model_detection["probes"] = probes

        requested = self.field_model_preference
        if requested in {"new", "legacy"}:
            if not probes[requested].get("available"):
                raise RuntimeError(
                    f"The requested --field-model {requested} is not readable on the destination. "
                    f"Use --field-model auto. Detection: {json.dumps(probes[requested], ensure_ascii=False)}"
                )
            selected = requested
            reason = "explicit command-line override"
        elif probes["new"].get("available") and not probes["legacy"].get("available"):
            selected = "new"
            reason = "only the unified Field Schemes API is available"
        elif probes["legacy"].get("available") and not probes["new"].get("available"):
            selected = "legacy"
            reason = "only the legacy Field Configuration APIs are available"
        elif probes["new"].get("available") and probes["legacy"].get("available"):
            new_match = bool(probes["new"].get("targetProjectAssociated"))
            legacy_match = bool(probes["legacy"].get("targetProjectAssociated"))
            if legacy_match and not new_match:
                selected = "legacy"
                reason = "both APIs respond, but the target project is currently associated through the legacy model"
            else:
                # A migrated site can keep deprecated read endpoints alive. Prefer
                # the new model when it owns the project or association is ambiguous.
                selected = "new"
                reason = "both APIs respond and the project is new-model or ambiguous; prefer Field Schemes with guarded legacy fallback"
        else:
            raise RuntimeError(
                "Neither Jira field-management API is available. The account must have Administer Jira permission, "
                "and the site must expose either Field Schemes or legacy Field Configurations."
            )

        self.field_model_detection["selectedDestinationModel"] = selected
        self.field_model_detection["selectionReason"] = reason
        self.record(phase, "field model selection", requested, selected, "detect", "success", reason)
        return self.field_model_detection

    def field_model_api(
        self, phase: str, entity: str, source: Any, destination: Any,
        action: str, method: str, path: str, body: Any,
        *, attempted_model: str, fallback_model: str,
    ) -> Any:
        """Run a model-defining write and convert explicit model rejection into fallback."""
        if not self.apply:
            self.record(phase, entity, source, destination, action, "planned")
            return None
        try:
            if method == "POST":
                result = self.c.post(path, body)
            elif method == "PUT":
                result = self.c.put(path, body)
            else:
                raise ValueError(f"Unsupported field-model method {method}")
            self.record(phase, entity, source, destination, action, "success")
            return result
        except JiraError as exc:
            mismatch = (
                attempted_model == "legacy" and requires_new_field_scheme(exc)
            ) or (
                attempted_model == "new" and requires_legacy_field_configuration(exc)
            )
            if mismatch:
                raise FieldModelMismatch(attempted_model, fallback_model, exc) from exc
            self.record(
                phase, entity, source, destination, action, "failed",
                f"HTTP {exc.status}: {exc.message}",
            )
            self.manual_add(
                f"{entity} '{source}' could not be {action}: HTTP {exc.status} - {exc.message}"
            )
            raise

    def ensure_new_field_scheme_from_legacy(self, destination_schemes: list[dict[str, Any]]) -> None:
        phase = "field-scheme"
        assoc = self.b.read(f"data/projects/{self.source_key}/field_configuration_scheme_association.json", {}) or {}
        vals = unwrap(assoc)
        src_scheme = ((vals[0] if vals else {}).get("fieldConfigurationScheme") or {})
        source_scheme_id = str(src_scheme.get("id", ""))
        if not source_scheme_id:
            self.record(phase, "field scheme", "source", "", "translate", "skipped", "No source field configuration scheme association was exported.")
            return

        scheme_name = str(src_scheme.get("name") or f"{self.target_key}: Field Scheme").strip()
        scheme_description = str(src_scheme.get("description") or "")
        found = first(destination_schemes, lambda x: norm(x.get("name")) == norm(scheme_name))
        if found:
            dest_scheme_id = str(found.get("id"))
            self.record(phase, "field scheme", scheme_name, dest_scheme_id, "reuse and sync", "success")
            self.manual_add(
                f"Field Scheme '{scheme_name}' was reused additively. Extra destination fields are not removed automatically."
            )
        else:
            result = self.field_model_api(
                phase, "field scheme", scheme_name, "", "create", "POST",
                "/rest/api/3/config/fieldschemes",
                {"name": scheme_name, "description": scheme_description},
                attempted_model="new", fallback_model="legacy",
            )
            dest_scheme_id = str((result or {}).get("id", "")) if self.apply else f"<new-field-scheme:{scheme_name}>"
        if not dest_scheme_id:
            raise RuntimeError("Jira did not return an ID for the destination Field Scheme.")
        self.idmap["field_scheme"][source_scheme_id] = dest_scheme_id

        source_fields = {str(x.get("id")): x for x in (self.b.read("data/fields/fields.json", []) or [])}
        mappings = self.b.read("data/fields/legacy_field_configuration_scheme_mappings.json", []) or []
        relevant_maps = [m for m in mappings if str(m.get("fieldConfigurationSchemeId")) == source_scheme_id]
        default_config_id = next(
            (str(m.get("fieldConfigurationId")) for m in relevant_maps if str(m.get("issueTypeId")) == "default"),
            "",
        )
        explicit_config_by_type = {
            str(m.get("issueTypeId")): str(m.get("fieldConfigurationId"))
            for m in relevant_maps
            if str(m.get("issueTypeId")) != "default"
        }
        project_source_types = [str(x.get("id")) for x in (self.project.get("issueTypes") or []) if x.get("id")]
        if not project_source_types:
            project_source_types = sorted(self.idmap["issue_type"].keys())

        config_ids = {default_config_id, *explicit_config_by_type.values()} - {""}
        config_items: dict[str, dict[str, dict[str, Any]]] = {}
        for config_id in config_ids:
            items = self.b.read(f"data/fields/legacy_field_configuration_items/{config_id}.json", []) or []
            config_items[config_id] = {str(item.get("id")): item for item in items if item.get("id")}

        all_source_field_ids: set[str] = set()
        for items in config_items.values():
            all_source_field_ids.update(items.keys())

        def map_field_id(source_field_id: str) -> str:
            already = self.idmap["field"].get(source_field_id)
            if already:
                return already
            if not source_field_id.startswith("customfield_"):
                self.idmap["field"][source_field_id] = source_field_id
                return source_field_id
            src = source_fields.get(source_field_id)
            if not src:
                return ""
            match = best_field_match(self.dest_fields, src)
            if match:
                did = str(match.get("id", ""))
                if did:
                    self.idmap["field"][source_field_id] = did
                    return did
            return ""

        type_config: dict[str, str] = {
            source_type_id: explicit_config_by_type.get(source_type_id, default_config_id)
            for source_type_id in project_source_types
        }
        type_dest: dict[str, str] = {
            source_type_id: self.idmap["issue_type"].get(source_type_id, "")
            for source_type_id in project_source_types
        }

        associations: dict[str, list[dict[str, Any]]] = {}
        parameter_updates: dict[str, list[dict[str, Any]]] = {}
        skipped_fields: list[str] = []
        all_dest_type_ids = {did for did in type_dest.values() if did}

        for source_field_id in sorted(all_source_field_ids):
            dest_field_id = map_field_id(source_field_id)
            if not dest_field_id or str(dest_field_id).startswith("<"):
                skipped_fields.append(source_field_id)
                continue

            visible_source_types: list[str] = []
            behavior_by_source_type: dict[str, dict[str, Any]] = {}
            for source_type_id in project_source_types:
                config_id = type_config.get(source_type_id, "")
                item = (config_items.get(config_id) or {}).get(source_field_id)
                if not item or bool(item.get("isHidden", False)):
                    continue
                if not type_dest.get(source_type_id):
                    continue
                visible_source_types.append(source_type_id)
                behavior_by_source_type[source_type_id] = field_scheme_parameters_from_legacy_item(item)

            if not visible_source_types:
                continue

            visible_dest_types = sorted({type_dest[x] for x in visible_source_types if type_dest.get(x)}, key=numeric_sort_key)
            association: dict[str, Any] = {"schemeIds": [numeric_or_string(dest_scheme_id)]}
            if set(visible_dest_types) != all_dest_type_ids:
                association["restrictedToWorkTypes"] = [numeric_or_string(x) for x in visible_dest_types]
            associations[dest_field_id] = [association]

            behavior_counts: dict[str, tuple[dict[str, Any], int]] = {}
            for source_type_id in visible_source_types:
                behavior = behavior_by_source_type[source_type_id]
                key = json.dumps(behavior, sort_keys=True, ensure_ascii=False)
                if key in behavior_counts:
                    old_behavior, count = behavior_counts[key]
                    behavior_counts[key] = (old_behavior, count + 1)
                else:
                    behavior_counts[key] = (behavior, 1)
            baseline = max(behavior_counts.values(), key=lambda x: x[1])[0] if behavior_counts else {}
            update_item: dict[str, Any] = {
                "schemeIds": [numeric_or_string(dest_scheme_id)],
                "parameters": baseline,
            }
            overrides: list[dict[str, Any]] = []
            for source_type_id in visible_source_types:
                behavior = behavior_by_source_type[source_type_id]
                if behavior == baseline:
                    continue
                override = dict(behavior)
                override["workTypeId"] = numeric_or_string(type_dest[source_type_id])
                overrides.append(override)
            if overrides:
                update_item["workTypeParameters"] = overrides
            parameter_updates[dest_field_id] = [update_item]

        if skipped_fields:
            labels = []
            for fid in skipped_fields:
                labels.append(f"{fid} ({(source_fields.get(fid) or {}).get('name', 'unknown')})")
            self.manual_add(
                "These source Field Scheme fields could not be mapped to destination fields and were skipped: "
                + ", ".join(labels)
            )

        if associations:
            self.apply_field_scheme_payload(
                phase, "field associations", "/rest/api/3/config/fieldschemes/fields",
                associations, dest_scheme_id, "add/update",
            )
        else:
            self.record(phase, "field associations", 0, dest_scheme_id, "add/update", "warning", "No source fields could be mapped.")

        if parameter_updates:
            self.apply_field_scheme_payload(
                phase, "field parameters", "/rest/api/3/config/fieldschemes/fields/parameters",
                parameter_updates, dest_scheme_id, "update",
            )

        project_body = {
            str(dest_scheme_id): {
                "projectIds": [numeric_or_string(self.target_project_id)]
            }
        }
        result = self.field_model_api(
            phase, "field scheme association", scheme_name, self.target_key,
            "associate", "PUT", "/rest/api/3/config/fieldschemes/projects",
            project_body, attempted_model="new", fallback_model="legacy",
        )
        self.record_field_scheme_results(phase, "field scheme association", result)

        if self.apply:
            try:
                project_schemes = self.c.get(
                    "/rest/api/3/config/fieldschemes/projects",
                    params={"projectId": self.target_project_id, "maxResults": 100},
                )
                (self.out / "field_scheme_verification.json").write_text(
                    json.dumps(project_schemes, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                rows = unwrap(project_schemes)
                matched = any(
                    str(row.get("projectId")) == str(self.target_project_id)
                    and str(row.get("schemeId")) == str(dest_scheme_id)
                    for row in rows
                )
                self.record(
                    phase, "field scheme association", self.target_key, dest_scheme_id,
                    "verify", "success" if matched else "warning",
                    "Destination project is associated with the translated Field Scheme."
                    if matched else "Jira did not return the expected project-to-scheme association; review field_scheme_verification.json.",
                )
            except JiraError as exc:
                self.record(
                    phase, "field scheme association", self.target_key, dest_scheme_id,
                    "verify", "warning", f"HTTP {exc.status}: {exc.message}",
                )

    def apply_field_scheme_payload(
        self, phase: str, entity: str, path: str, payload: dict[str, Any],
        destination_scheme_id: str, action: str,
    ) -> None:
        if not payload:
            return
        if not self.apply:
            self.record(phase, entity, len(payload), destination_scheme_id, action, "planned")
            return

        def submit(items: list[tuple[str, Any]]) -> None:
            part = dict(items)
            try:
                result = self.c.put(path, part)
                self.record(phase, entity, len(part), destination_scheme_id, action, "success")
                self.record_field_scheme_results(phase, entity, result)
            except JiraError as exc:
                if requires_legacy_field_configuration(exc):
                    raise FieldModelMismatch("new", "legacy", exc) from exc
                if len(items) > 1 and exc.status in {400, 409, 422}:
                    middle = len(items) // 2
                    submit(items[:middle])
                    submit(items[middle:])
                    return
                field_id = items[0][0] if items else "unknown"
                self.record(
                    phase, entity, field_id, destination_scheme_id, action, "skipped",
                    f"HTTP {exc.status}: {exc.message}",
                )
                self.manual_add(
                    f"{entity} for field '{field_id}' could not be applied: HTTP {exc.status} - {exc.message}"
                )

        entries = list(payload.items())
        for start in range(0, len(entries), 25):
            submit(entries[start:start + 25])

    def record_field_scheme_results(self, phase: str, entity: str, result: Any) -> None:
        if not self.apply or not isinstance(result, dict):
            return
        rows = result.get("results")
        if not isinstance(rows, list):
            return
        failures = [row for row in rows if row.get("success") is False]
        if not failures:
            self.record(phase, f"{entity} response", len(rows), "all successful", "inspect", "success")
            return
        details = "; ".join(
            f"field={row.get('fieldId', '-')}, scheme={row.get('schemeId', '-')}, error={row.get('errorMessage') or row.get('message') or 'unknown'}"
            for row in failures[:20]
        )
        self.record(phase, f"{entity} response", len(rows), len(failures), "inspect", "warning", details)
        self.manual_add(f"Some {entity} operations failed: {details}")

    def sync_legacy_field_configuration_items(
        self, configuration_name: str, configuration_id: str,
        desired_updates: list[dict[str, Any]],
    ) -> None:
        """Apply only field items that actually exist on the destination.

        Jira field configurations are seeded from the destination site's own
        default configuration. A source-only system field (for example,
        ``dataclassification``) may therefore be absent. Jira rejects the whole
        PUT when even one submitted ID is absent, so inventory the destination
        first, skip unavailable items, and isolate any remaining per-field error.
        """
        phase = "field-config"
        if not self.apply:
            self.record(
                phase, "field configuration items", configuration_name,
                configuration_id, f"sync {len(desired_updates)} source items", "planned",
            )
            return

        path = f"/rest/api/3/fieldconfiguration/{configuration_id}/fields"
        try:
            destination_items = self.c.paginate(path)
        except JiraError as exc:
            if requires_new_field_scheme(exc):
                raise FieldModelMismatch("legacy", "new", exc) from exc
            self.record(
                phase, "field configuration item inventory", configuration_name,
                configuration_id, "read", "failed", f"HTTP {exc.status}: {exc.message}",
            )
            self.manual_add(
                f"Field items for legacy configuration '{configuration_name}' could not be read: "
                f"HTTP {exc.status} - {exc.message}"
            )
            raise

        destination_by_id = {
            str(item.get("id")): item
            for item in destination_items
            if isinstance(item, dict) and item.get("id") is not None
        }
        unavailable = [
            str(item.get("id")) for item in desired_updates
            if str(item.get("id")) not in destination_by_id
        ]
        applicable = [
            item for item in desired_updates
            if str(item.get("id")) in destination_by_id
        ]

        if unavailable:
            unavailable_text = ", ".join(unavailable)
            self.record(
                phase, "unavailable field configuration items", configuration_name,
                configuration_id, f"skip {len(unavailable)}", "warning",
                f"Not present on this destination: {unavailable_text}",
            )
            self.manual_add(
                f"Legacy field configuration '{configuration_name}' skipped source-only field item(s) "
                f"not installed on the destination: {unavailable_text}."
            )

        def same_value(current: Mapping[str, Any], desired: Mapping[str, Any], key: str) -> bool:
            if key in {"isHidden", "isRequired"}:
                return bool(current.get(key, False)) == bool(desired.get(key, False))
            return str(current.get(key) or "") == str(desired.get(key) or "")

        changed: list[dict[str, Any]] = []
        for desired in applicable:
            current = destination_by_id[str(desired.get("id"))]
            keys = [k for k in ("isHidden", "isRequired", "description", "renderer") if k in desired]
            if any(not same_value(current, desired, key) for key in keys):
                changed.append(desired)

        result_row: dict[str, Any] = {
            "configurationName": configuration_name,
            "configurationId": configuration_id,
            "sourceItemCount": len(desired_updates),
            "destinationItemCount": len(destination_items),
            "unavailableSourceItemIds": unavailable,
            "applicableItemCount": len(applicable),
            "changedItemCount": len(changed),
            "successfulItemIds": [],
            "failedItems": [],
        }

        if not changed:
            self.record(
                phase, "field configuration items", configuration_name,
                configuration_id, "sync", "success",
                "No destination field-item changes were required.",
            )
            result_row["status"] = "verified-no-change"
            self.legacy_field_configuration_results.append(result_row)
            return

        def submit(batch: list[dict[str, Any]]) -> None:
            if not batch:
                return
            try:
                self.c.put(path, {"fieldConfigurationItems": batch})
                ids = [str(item.get("id")) for item in batch]
                result_row["successfulItemIds"].extend(ids)
                self.record(
                    phase, "field configuration items", configuration_name,
                    configuration_id, f"update {len(batch)}", "success",
                    f"Fields: {', '.join(ids)}",
                )
            except JiraError as exc:
                if requires_new_field_scheme(exc):
                    raise FieldModelMismatch("legacy", "new", exc) from exc
                # One unsupported field/renderer must not abort the entire clone.
                if len(batch) > 1:
                    middle = len(batch) // 2
                    submit(batch[:middle])
                    submit(batch[middle:])
                    return
                field_id = str(batch[0].get("id"))
                failure = {"fieldId": field_id, "status": exc.status, "message": exc.message}
                result_row["failedItems"].append(failure)
                self.record(
                    phase, "field configuration item", field_id,
                    configuration_id, "update", "skipped",
                    f"HTTP {exc.status}: {exc.message}",
                )
                self.manual_add(
                    f"Legacy field configuration '{configuration_name}' could not update field "
                    f"'{field_id}': HTTP {exc.status} - {exc.message}"
                )

        for batch in chunks(changed, 100):
            submit(batch)

        result_row["status"] = "partial" if result_row["failedItems"] else "applied"
        self.legacy_field_configuration_results.append(result_row)

    def ensure_legacy_field_configurations(self) -> None:
        phase = "field-config"
        assoc = self.b.read(f"data/projects/{self.source_key}/field_configuration_scheme_association.json", {}) or {}
        vals = unwrap(assoc); src_scheme = ((vals[0] if vals else {}).get("fieldConfigurationScheme") or {})
        source_scheme_id = str(src_scheme.get("id", ""))
        if not source_scheme_id:
            self.record(phase, "field configuration scheme", "source", "", "clone", "skipped", "No legacy field configuration scheme association in export.")
            return
        configs = self.b.read("data/fields/legacy_field_configurations.json", []) or []
        maps = self.b.read("data/fields/legacy_field_configuration_scheme_mappings.json", []) or []
        relevant_maps = [m for m in maps if str(m.get("fieldConfigurationSchemeId")) == source_scheme_id]
        relevant_config_ids = {str(m.get("fieldConfigurationId")) for m in relevant_maps}
        try:
            dest_configs = self.c.paginate("/rest/api/3/fieldconfiguration")
        except JiraError:
            dest_configs = []
        for src in configs:
            sid = str(src.get("id"))
            if sid not in relevant_config_ids:
                continue
            name = str(src.get("name")); found = first(dest_configs, lambda x: norm(x.get("name")) == norm(name)); created = False
            if found:
                did = str(found.get("id")); self.record(phase, "field configuration", name, did, "reuse", "success")
            else:
                result = self.field_model_api(
                    phase, "field configuration", name, "", "create", "POST",
                    "/rest/api/3/fieldconfiguration",
                    {"name": name, "description": src.get("description", "")},
                    attempted_model="legacy", fallback_model="new",
                )
                did = str((result or {}).get("id", "")) if self.apply else f"<new-field-config:{name}>"; created = True
            if not did:
                continue
            self.idmap["field_configuration"][sid] = did
            items = self.b.read(f"data/fields/legacy_field_configuration_items/{sid}.json", []) or []
            updates = []
            for item in items:
                sfid = str(item.get("id")); dfid = self.idmap["field"].get(sfid, sfid if not sfid.startswith("customfield_") else "")
                if not dfid:
                    continue
                entry = {"id": dfid, "isHidden": bool(item.get("isHidden", False)), "isRequired": bool(item.get("isRequired", False))}
                if item.get("description") is not None:
                    entry["description"] = item.get("description", "")
                if item.get("renderer"):
                    entry["renderer"] = item.get("renderer")
                updates.append(entry)
            # Defer custom configuration item updates until after the field
            # configuration scheme is associated with the target project and
            # custom fields are explicitly associated to that project. Jira can
            # otherwise omit newly-created fields from the configuration item
            # inventory, which makes them non-searchable/non-navigable.
            is_default_configuration = bool(src.get("isDefault"))
            if created or not is_default_configuration:
                self.legacy_post_association_syncs.append((name, did, updates, is_default_configuration))
            else:
                self.manual_add(
                    f"Verify required/hidden fields in reused default field configuration '{name}'. "
                    "The global default configuration is not overwritten automatically."
                )

        try:
            dest_schemes = self.c.paginate("/rest/api/3/fieldconfigurationscheme")
        except JiraError:
            dest_schemes = []
        sname = str(src_scheme.get("name")); found = first(dest_schemes, lambda x: norm(x.get("name")) == norm(sname)); scheme_created = False
        if found:
            dest_scheme_id = str(found.get("id")); self.record(phase, "field configuration scheme", sname, dest_scheme_id, "reuse", "success")
        else:
            result = self.field_model_api(
                phase, "field configuration scheme", sname, "", "create", "POST",
                "/rest/api/3/fieldconfigurationscheme",
                {"name": sname, "description": src_scheme.get("description", "")},
                attempted_model="legacy", fallback_model="new",
            )
            dest_scheme_id = str((result or {}).get("id", "")) if self.apply else f"<new-field-config-scheme:{sname}>"; scheme_created = True
        if dest_scheme_id:
            self.idmap["field_configuration_scheme"][source_scheme_id] = dest_scheme_id
            body_maps = []
            for m in relevant_maps:
                sit = str(m.get("issueTypeId")); dit = "default" if sit == "default" else self.idmap["issue_type"].get(sit)
                dfc = self.idmap["field_configuration"].get(str(m.get("fieldConfigurationId")))
                if dit and dfc:
                    body_maps.append({"issueTypeId": dit, "fieldConfigurationId": dfc})
            if scheme_created:
                self.field_model_api(
                    phase, "field configuration mappings", sname, dest_scheme_id, "update", "PUT",
                    f"/rest/api/3/fieldconfigurationscheme/{dest_scheme_id}/mapping",
                    {"mappings": body_maps},
                    attempted_model="legacy", fallback_model="new",
                )
            else:
                self.manual_add(f"Verify work-type mappings in reused field configuration scheme '{sname}'. It was not overwritten automatically.")
            self.field_model_api(
                phase, "field configuration association", sname, self.target_key, "associate", "PUT",
                "/rest/api/3/fieldconfigurationscheme/project",
                {"fieldConfigurationSchemeId": dest_scheme_id, "projectId": self.target_project_id},
                attempted_model="legacy", fallback_model="new",
            )

    def ensure_custom_field_project_associations(self) -> None:
        """Explicitly associate mapped custom fields with the target project.

        Jira Cloud can create a custom field and context successfully while the
        field is still absent from the project's active field configuration.
        Such a field can be added to a screen but remains non-searchable and is
        rejected from saved-filter JQL/columns. The association endpoint makes
        the field available to all work types on the target project.
        """
        phase = "field-association"
        destination_fields = sorted({
            str(destination_id)
            for source_id, destination_id in self.idmap.get("field", {}).items()
            if str(source_id).startswith("customfield_")
            and str(destination_id).startswith("customfield_")
            and not str(destination_id).startswith("<")
        })
        if not destination_fields:
            self.record(phase, "custom fields", "none", self.target_key, "associate", "skipped",
                        "No mapped custom fields required project association.")
            return

        report = {
            "projectKey": self.target_key,
            "projectId": self.target_project_id,
            "fieldIds": destination_fields,
            "status": "planned" if not self.apply else "pending",
            "batches": [],
        }
        self.custom_field_association_results.append(report)

        if not self.apply:
            self.record(phase, "custom fields", len(destination_fields), self.target_key,
                        "associate with project in <=50-field batches", "planned", ", ".join(destination_fields))
            return

        # Jira Cloud currently accepts at most 50 fields per association request.
        # v3.8 submitted all mapped fields at once, so this source's 51 fields caused
        # HTTP 400 and none were associated by that call.
        failed_batches: list[str] = []
        for offset in range(0, len(destination_fields), 50):
            batch = destination_fields[offset:offset + 50]
            body = {
                "associationContexts": [
                    {"identifier": int(self.target_project_id) if str(self.target_project_id).isdigit() else self.target_project_id,
                     "type": "PROJECT_ID"}
                ],
                "fields": [{"identifier": field_id, "type": "FIELD_ID"} for field_id in batch],
            }
            batch_result = {"fieldIds": batch, "status": "pending"}
            report["batches"].append(batch_result)
            try:
                self.c.put("/rest/api/3/field/association", body)
                batch_result["status"] = "success"
                self.record(phase, "custom fields", len(batch), self.target_key,
                            f"associate batch {offset // 50 + 1}", "success", ", ".join(batch))
            except JiraError as exc:
                batch_result["status"] = "warning"
                batch_result["error"] = f"HTTP {exc.status}: {exc.message}"
                failed_batches.append(batch_result["error"])
                self.record(phase, "custom fields", len(batch), self.target_key,
                            f"associate batch {offset // 50 + 1}", "warning", batch_result["error"])
        if failed_batches:
            report["status"] = "warning"
            report["error"] = "; ".join(failed_batches)
            self.manual_add(
                f"One or more custom-field association batches failed for project {self.target_key}: {report['error']}."
            )
        else:
            report["status"] = "success"

        # Jira may update field availability asynchronously. Refresh the field
        # inventory a few times so filter columns and legacy configurations use
        # the post-association navigability state.
        for attempt in range(5):
            try:
                refreshed = self.c.get("/rest/api/3/field") or []
                by_id = {str(item.get("id")): item for item in refreshed if isinstance(item, dict)}
                missing = [field_id for field_id in destination_fields if field_id not in by_id]
                non_navigable = [
                    field_id for field_id in destination_fields
                    if field_id in by_id and by_id[field_id].get("navigable") is False
                ]
                self.dest_filter_fields = refreshed
                if not missing and not non_navigable:
                    report["verification"] = "all fields visible and navigable"
                    break
                report["verification"] = {"missing": missing, "nonNavigable": non_navigable}
            except JiraError as exc:
                report["verificationError"] = f"HTTP {exc.status}: {exc.message}"
            if attempt < 4:
                time.sleep(1 + attempt)

    def sync_legacy_items_after_project_association(self) -> None:
        """Synchronize legacy field items after project/field association."""
        if not self.legacy_post_association_syncs:
            return
        pending = list(self.legacy_post_association_syncs)
        self.legacy_post_association_syncs.clear()
        for name, configuration_id, updates, is_default in pending:
            if is_default:
                continue
            # Retry inventory briefly because Jira may take a moment to populate
            # new custom fields in a newly-associated configuration.
            last_unavailable: list[str] = []
            for attempt in range(4):
                before = len(self.legacy_field_configuration_results)
                self.sync_legacy_field_configuration_items(name, configuration_id, updates)
                if len(self.legacy_field_configuration_results) > before:
                    row = self.legacy_field_configuration_results[-1]
                    last_unavailable = list(row.get("unavailableSourceItemIds") or [])
                    custom_unavailable = [x for x in last_unavailable if str(x).startswith("customfield_")]
                    if not custom_unavailable:
                        break
                if attempt < 3 and self.apply:
                    time.sleep(1 + attempt)

    def reconcile_filter_field_mappings(self, filters: list[dict[str, Any]] | None = None) -> None:
        """Refresh late-created destination fields needed by filters and List columns."""
        if self.apply:
            try:
                self.refresh_destination_fields()
            except JiraError:
                pass
        source_fields = {
            str(field.get("id")): field
            for field in (self.b.read("data/fields/fields.json", []) or [])
            if isinstance(field, dict) and field.get("id")
        }
        needed: set[str] = set()
        for f in filters or self.selected_source_filters():
            filter_id = str(f.get("id", ""))
            needed.update(self._custom_field_ids_from_filter_jql(str(f.get("jql", ""))))
            columns = self.b.read(f"data/filters/columns/{filter_id}.json", None)
            if not isinstance(columns, list):
                eff = self.b.read(f"data/filters/effective_columns/{filter_id}.json", {}) or {}
                columns = eff.get("columns") if isinstance(eff, dict) else []
            for col in columns or []:
                value = str(col.get("value") or col.get("id") or "") if isinstance(col, dict) else str(col or "")
                if value.startswith("customfield_"):
                    needed.add(value)

        capture = self.b.read(f"data/projects/{self.source_key}/list_view/column_capture.json", {}) or {}
        for col in (capture.get("columns") if isinstance(capture, dict) else []) or []:
            value = str(col.get("value") or col.get("id") or "") if isinstance(col, dict) else str(col or "")
            if value.startswith("customfield_"):
                needed.add(value)

        for source_id in sorted(needed):
            existing = self.idmap.get("field", {}).get(source_id)
            if existing and not str(existing).startswith("<"):
                continue
            src = source_fields.get(source_id)
            if not src:
                continue
            found = best_field_match(self.dest_fields, src)
            if found:
                destination_id = str(found.get("id", ""))
                if destination_id:
                    self.idmap["field"][source_id] = destination_id
                    self.record("filter-field-map", "field", f"{src.get('name')} ({source_id})", destination_id, "late map", "success")

    def validate_destination_jql(self, jql: str) -> str | None:
        """Return a validation error for JQL, or None when Jira accepts it."""
        if not self.apply:
            return None
        try:
            payload = self.c.request(
                "POST", "/rest/api/3/jql/parse",
                params={"validation": "strict"},
                body={"queries": [jql]},
            )
        except JiraError as exc:
            return f"JQL validation endpoint: HTTP {exc.status}: {exc.message}"
        if not isinstance(payload, dict):
            return None
        queries = payload.get("queries") or []
        if not queries:
            return None
        first_query = queries[0] if isinstance(queries[0], dict) else {}
        errors = first_query.get("errors") or first_query.get("errorMessages")
        if errors:
            return json.dumps(errors, ensure_ascii=False)
        return None

    def create_filter_with_retry(self, phase: str, src: dict[str, Any], selected_filters: list[dict[str, Any]]) -> tuple[str, str]:
        """Create one filter, retrying after Jira finishes indexing new custom fields."""
        name = str(src.get("name", ""))
        source_jql = str(src.get("jql", ""))
        if not self.apply:
            jql = self.rewrite_filter_jql(source_jql)
            self.record(phase, "filter", name, "", "create", "planned", jql)
            return f"<new-filter:{name}>", jql

        last_error: JiraError | None = None
        last_validation: str | None = None
        jql = self.rewrite_filter_jql(source_jql)
        for attempt in range(5):
            if attempt:
                self.reconcile_filter_field_mappings(selected_filters)
                jql = self.rewrite_filter_jql(source_jql)
            last_validation = self.validate_destination_jql(jql)
            body = {
                "name": name,
                "description": src.get("description", ""),
                "jql": jql,
                "favourite": bool(src.get("favourite", True)),
            }
            try:
                result = self.c.post("/rest/api/3/filter", body)
                did = str((result or {}).get("id", ""))
                self.record(phase, "filter", name, did, "create", "success", f"JQL: {jql}")
                return did, jql
            except JiraError as exc:
                last_error = exc
                if exc.status in {400, 404} and attempt < 4:
                    time.sleep(1 + attempt)
                    continue
                break

        note = f"HTTP {last_error.status}: {last_error.message}" if last_error else "Unknown filter creation failure"
        if last_validation:
            note += f"; JQL validation: {last_validation}"
        self.record(phase, "filter", name, "", "create", "skipped", note)
        self.manual_add(f"Filter '{name}' could not be created after retries. {note}. Rewritten JQL: {jql}")
        return "", jql

    def rewrite_filter_jql(self, source_jql: str) -> str:
        """Rewrite project and custom-field references for destination Jira.

        v3.8 rewrote custom-field IDs in a loop.  When source and destination IDs
        were adjacent (for example 10045 -> 10046, 10046 -> 10047, ...), a value
        rewritten early in the loop was rewritten *again* by later iterations.
        That is how source ``cf[10045]`` became ``cf[10052]`` and filter 02 failed.

        v3.9 performs an atomic one-pass numeric-ID substitution, so a source ID
        can be translated exactly once.
        """
        jql = rewrite_project_jql(source_jql, self.source_key, self.target_key)
        source_fields = {
            str(field.get("id")): field
            for field in (self.b.read("data/fields/fields.json", []) or [])
            if isinstance(field, dict) and field.get("id")
        }

        number_map: dict[str, str] = {}
        for source_id, destination_id in self.idmap.get("field", {}).items():
            if not str(source_id).startswith("customfield_") or not str(destination_id).startswith("customfield_"):
                continue
            source_match = re.search(r"(\d+)$", str(source_id))
            destination_match = re.search(r"(\d+)$", str(destination_id))
            if source_match and destination_match:
                number_map[source_match.group(1)] = destination_match.group(1)

        def replace_cf(match: re.Match[str]) -> str:
            source_number = match.group(1)
            destination_number = number_map.get(source_number)
            return f"cf[{destination_number}]" if destination_number else match.group(0)

        def replace_customfield(match: re.Match[str]) -> str:
            source_number = match.group(1)
            destination_number = number_map.get(source_number)
            return f"cf[{destination_number}]" if destination_number else match.group(0)

        # Each original reference is visited once.  Replacements are not rescanned.
        jql = re.sub(r"(?i)\bcf\[\s*(\d+)\s*\]", replace_cf, jql)
        jql = re.sub(r"(?i)\bcustomfield_(\d+)\b", replace_customfield, jql)

        # Also support quoted custom-field names in field position.  This happens
        # after numeric substitution so the generated cf[ID] aliases cannot cascade.
        for source_id, destination_id in self.idmap.get("field", {}).items():
            if not str(source_id).startswith("customfield_") or not str(destination_id).startswith("customfield_"):
                continue
            destination_match = re.search(r"(\d+)$", str(destination_id))
            if not destination_match:
                continue
            field_name = str((source_fields.get(str(source_id)) or {}).get("name") or "").strip()
            if not field_name:
                continue
            replacement = f"cf[{destination_match.group(1)}]"
            operator = r"(?:=|!=|>=|<=|>|<|~|!~|\bIS\b|\bIS\s+NOT\b|\bIN\b|\bNOT\s+IN\b|\bWAS\b|\bWAS\s+IN\b|\bWAS\s+NOT\s+IN\b|\bCHANGED\b)"
            quoted_pattern = rf'(?i)"{re.escape(field_name)}"(?=\s*{operator})'
            jql = re.sub(quoted_pattern, replacement, jql)
        return jql

    def _source_application_role_keys(self) -> list[str]:
        roles = self.b.read("data/reference/application_roles.json", []) or []
        return [
            str(x.get("key"))
            for x in roles
            if isinstance(x, dict) and x.get("key") and x.get("defined", True)
        ]

    def _destination_application_role_keys(self) -> set[str]:
        cached = getattr(self, "_dest_application_role_keys", None)
        if cached is not None:
            return set(cached)
        keys: set[str] = set()
        try:
            roles = self.c.get("/rest/api/3/applicationrole") or []
            if isinstance(roles, dict):
                roles = roles.get("values") or roles.get("applicationRoles") or []
            for role in roles:
                if isinstance(role, dict) and role.get("key"):
                    keys.add(str(role.get("key")))
        except JiraError:
            # Some Jira plans/accounts do not expose this read endpoint even though
            # permission-scheme writes using applicationRole still work.
            keys = set()
        self._dest_application_role_keys = set(keys)
        return keys

    def _source_application_role_default(self) -> str:
        keys = self._source_application_role_keys()
        return keys[0] if len(keys) == 1 else ""

    def _permission_holder_semantic(self, holder: Mapping[str, Any], *, source: bool) -> tuple[str, str]:
        """Return a cross-site semantic identity for a permission holder.

        Jira IDs for project roles/custom fields are site-specific, so the
        fingerprint deliberately compares role names and remapped field IDs.
        """
        htype = str(holder.get("type") or "")
        if htype == "projectRole":
            role_name = str((holder.get("projectRole") or {}).get("name") or "").strip()
            if role_name:
                return htype, role_name
            rid = str(holder.get("parameter") or holder.get("value") or "")
            if source:
                # Source exports normally include projectRole expansion. Retain
                # the raw ID only as a diagnostic fallback.
                return htype, rid
            role = first(self.dest_project_roles, lambda x: str(x.get("id")) == rid)
            return htype, str((role or {}).get("name") or rid)
        if htype == "applicationRole":
            value = str(holder.get("parameter") or holder.get("value") or "").strip()
            if value:
                return htype, value
            if source:
                inferred = self._source_application_role_default()
                return htype, inferred or "<ambiguous-application-role>"
            keys = self._destination_application_role_keys()
            return htype, next(iter(keys)) if len(keys) == 1 else "<application-role>"
        if htype == "group":
            name = str(
                holder.get("parameter")
                or (holder.get("group") or {}).get("name")
                or holder.get("value")
                or ""
            )
            return htype, name
        if htype == "user":
            value = str(
                holder.get("parameter")
                or holder.get("value")
                or (holder.get("user") or {}).get("accountId")
                or ""
            )
            return htype, value
        if htype in {"groupCustomField", "userCustomField"}:
            field_id = str(holder.get("parameter") or holder.get("value") or "")
            if source:
                field_id = str(self.idmap.get("field", {}).get(field_id, field_id))
            return htype, field_id
        # anyone, assignee, projectLead, reporter, sd.customer.portal.only, etc.
        return htype, str(holder.get("parameter") or holder.get("value") or "")

    def _permission_scheme_semantic_fingerprint(self, scheme: Mapping[str, Any], *, source: bool) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for grant in scheme.get("permissions", []) or []:
            if not isinstance(grant, dict):
                continue
            htype, holder_value = self._permission_holder_semantic(grant.get("holder") or {}, source=source)
            rows.append((str(grant.get("permission") or ""), htype, holder_value))
        return sorted(rows)

    @staticmethod
    def _is_jira_managed_permission_extra(row: tuple[str, str, str]) -> bool:
        """Return True for destination-only permission grants managed by Jira Cloud.

        Jira's Guest access feature injects a system project role named
        ``jira-guest-member`` into an assigned company-managed project's
        permission scheme. These grants can appear *after* a scheme is
        associated with a project, even when the unassociated scheme was an
        exact source clone. They are destination platform policy, not a
        user-authored source permission grant, so they must not make a
        portable source clone fail verification.

        We intentionally keep this exception extremely narrow: only extra
        grants whose holder is the Jira system project role
        ``jira-guest-member`` are ignored. Missing source grants and every
        other extra grant remain fatal.
        """
        _permission, holder_type, holder_value = row
        return holder_type == "projectRole" and holder_value.strip().casefold() == "jira-guest-member"

    def _permission_scheme_diff(
        self,
        source_fp: Sequence[tuple[str, str, str]],
        dest_fp: Sequence[tuple[str, str, str]],
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]], list[tuple[str, str, str]]]:
        """Return (missing, user_extra, jira_managed_extra).

        A destination permission scheme is portable-equivalent to the source
        when no source grant is missing and there are no destination-only
        grants except Jira-managed guest-role grants.
        """
        missing = [x for x in source_fp if x not in dest_fp]
        all_extra = [x for x in dest_fp if x not in source_fp]
        jira_managed_extra = [x for x in all_extra if self._is_jira_managed_permission_extra(x)]
        user_extra = [x for x in all_extra if not self._is_jira_managed_permission_extra(x)]
        return missing, user_extra, jira_managed_extra

    def _map_permission_holder(self, grant: Mapping[str, Any]) -> dict[str, Any]:
        holder = grant.get("holder") or {}
        htype = str(holder.get("type") or "")
        mapped: dict[str, Any] = {"type": htype}
        if not htype:
            raise RuntimeError(f"Permission {grant.get('permission')} has no holder type in the source export.")

        if htype == "projectRole":
            role_name = str((holder.get("projectRole") or {}).get("name") or "").strip()
            role_by_name = {norm(x.get("name")): str(x.get("id")) for x in self.dest_project_roles}
            rid = role_by_name.get(norm(role_name))
            if not rid:
                raise RuntimeError(
                    f"Permission {grant.get('permission')} requires project role '{role_name}', "
                    "but that role could not be resolved on the destination site."
                )
            mapped["parameter"] = rid
            source_role_id = str(holder.get("parameter") or holder.get("value") or "")
            if source_role_id:
                self.idmap["project_role"][source_role_id] = rid
            return mapped

        if htype == "applicationRole":
            app_key = str(holder.get("parameter") or holder.get("value") or "").strip()
            if not app_key:
                source_keys = self._source_application_role_keys()
                if len(source_keys) != 1:
                    raise RuntimeError(
                        f"Permission {grant.get('permission')} uses applicationRole without a key, "
                        f"and the source export contains {len(source_keys)} application roles; mapping is ambiguous."
                    )
                app_key = source_keys[0]
            dest_keys = self._destination_application_role_keys()
            if dest_keys and app_key not in dest_keys:
                raise RuntimeError(
                    f"Permission {grant.get('permission')} requires application role '{app_key}', "
                    "which is not available on the destination site."
                )
            mapped["parameter"] = app_key
            return mapped

        if htype == "group":
            group_name = str(holder.get("parameter") or (holder.get("group") or {}).get("name") or "").strip()
            if not group_name:
                raise RuntimeError(f"Permission {grant.get('permission')} has a group holder with no group name.")
            # Jira accepts the group name as parameter. The destination API will
            # reject the grant if that group does not exist, which is safer than
            # silently mapping to a different group.
            mapped["parameter"] = group_name
            return mapped

        if htype == "user":
            account_id = str(
                holder.get("parameter")
                or holder.get("value")
                or (holder.get("user") or {}).get("accountId")
                or ""
            ).strip()
            if not account_id:
                raise RuntimeError(f"Permission {grant.get('permission')} has a user holder with no account ID.")
            mapped["parameter"] = account_id
            return mapped

        if htype in {"groupCustomField", "userCustomField"}:
            source_field = str(holder.get("parameter") or holder.get("value") or "").strip()
            dest_field = str(self.idmap.get("field", {}).get(source_field, ""))
            if not dest_field:
                raise RuntimeError(
                    f"Permission {grant.get('permission')} references source custom field '{source_field}', "
                    "but that field has no destination mapping."
                )
            mapped["parameter"] = dest_field
            return mapped

        if htype in {"anyone", "assignee", "projectLead", "reporter", "sd.customer.portal.only"}:
            return mapped

        parameter = holder.get("parameter")
        if parameter is not None:
            mapped["parameter"] = str(parameter)
            return mapped
        raise RuntimeError(
            f"Permission {grant.get('permission')} uses unsupported holder type '{htype}' and cannot be cloned safely."
        )

    def _permission_clone_base_name(self, source_name: str) -> str:
        return f"{self.source_key}: {source_name}"

    def _ensure_permission_grants(self, source: Mapping[str, Any], scheme_id: str) -> None:
        """Add only permission grants that are genuinely missing.

        Jira Cloud may automatically seed a newly created permission scheme with
        system-managed project-role grants (notably the
        ``atlassian-addons-project-access`` role). Re-posting an identical
        permission/holder pair returns HTTP 400 "already exists". Treating
        that response as a failed clone made otherwise-correct imports abort.

        This routine reads the destination scheme first, compares grants using
        cross-site semantic holder identities, skips exact existing grants, and
        only POSTs missing grants. A final exact verification is still performed
        by the caller, so this does not weaken replication correctness.
        """
        phase = "permissions"
        try:
            current = self.c.get(
                f"/rest/api/3/permissionscheme/{scheme_id}",
                params={"expand": "permissions,user,group,projectRole,field,all"},
            )
            existing = set(self._permission_scheme_semantic_fingerprint(current, source=False))
        except JiraError as exc:
            raise RuntimeError(
                f"Could not read destination permission scheme {scheme_id} before cloning grants: {exc.message}"
            ) from exc

        failures: list[str] = []
        for grant in source.get("permissions", []) or []:
            if not isinstance(grant, dict):
                continue
            permission = str(grant.get("permission") or "")
            try:
                mapped_holder = self._map_permission_holder(grant)
            except RuntimeError as exc:
                failures.append(str(exc))
                self.record(phase, "permission grant", permission, scheme_id, "map holder", "failed", str(exc))
                continue

            htype, holder_value = self._permission_holder_semantic(mapped_holder, source=False)
            semantic_key = (permission, htype, holder_value)
            label = f"{permission} / {mapped_holder.get('type')}"

            if semantic_key in existing:
                self.record(
                    phase, "permission grant", label, scheme_id,
                    "already present", "success",
                    f"Exact holder already exists ({holder_value}); no duplicate POST required."
                )
                continue

            body = {"holder": mapped_holder, "permission": permission}
            try:
                self.api(
                    phase, "permission grant", label, scheme_id,
                    "create", "POST", f"/rest/api/3/permissionscheme/{scheme_id}/permission", body,
                    optional=False,
                )
                existing.add(semantic_key)
            except JiraError as exc:
                # Jira can race with/system-seed grants. Re-read before calling a
                # duplicate error a real failure.
                try:
                    refreshed = self.c.get(
                        f"/rest/api/3/permissionscheme/{scheme_id}",
                        params={"expand": "permissions,user,group,projectRole,field,all"},
                    )
                    refreshed_fp = set(self._permission_scheme_semantic_fingerprint(refreshed, source=False))
                except JiraError:
                    refreshed_fp = existing
                if semantic_key in refreshed_fp:
                    existing = refreshed_fp
                    self.record(
                        phase, "permission grant", label, scheme_id,
                        "already present after Jira response", "success",
                        f"Jira returned HTTP {exc.status}, but the exact grant is present; treating this as idempotent success."
                    )
                else:
                    failures.append(f"{permission}/{mapped_holder.get('type')}: HTTP {exc.status} - {exc.message}")

        if failures:
            raise RuntimeError(
                "The permission scheme was not associated because some source grants could not be recreated:\n- "
                + "\n- ".join(failures)
            )

    def _create_permission_scheme_clone(self, source: Mapping[str, Any], existing_schemes: Sequence[Mapping[str, Any]]) -> str:
        phase = "permissions"
        source_name = str(source.get("name") or "Permission Scheme")
        source_fp = self._permission_scheme_semantic_fingerprint(source, source=True)
        digest = hashlib.sha256(json.dumps(source_fp, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
        base = self._permission_clone_base_name(source_name)

        # Reuse an importer-created clone only when its grants are already exact.
        for scheme in existing_schemes:
            candidate_name = str(scheme.get("name") or "")
            if norm(candidate_name) != norm(base) and not norm(candidate_name).startswith(norm(base + " [clone ")):
                continue
            sid = str(scheme.get("id") or "")
            if not sid:
                continue
            try:
                full = self.c.get(
                    f"/rest/api/3/permissionscheme/{sid}",
                    params={"expand": "permissions,user,group,projectRole,field,all"},
                )
                dest_fp = self._permission_scheme_semantic_fingerprint(full, source=False)
                missing, user_extra, jira_managed_extra = self._permission_scheme_diff(source_fp, dest_fp)
                if not missing and not user_extra:
                    detail = candidate_name
                    action = "reuse exact clone"
                    if jira_managed_extra:
                        action = "reuse source-compatible clone"
                        detail += (
                            f"; Jira manages {len(jira_managed_extra)} guest-role grant(s) after project association"
                        )
                    self.record(phase, "permission scheme", source_name, sid, action, "success", detail)
                    return sid
            except JiraError:
                continue

        occupied = {norm(str(x.get("name") or "")) for x in existing_schemes}
        clone_name = base
        if norm(clone_name) in occupied:
            clone_name = f"{base} [clone {digest}]"
        suffix = 2
        while norm(clone_name) in occupied:
            clone_name = f"{base} [clone {digest}-{suffix}]"
            suffix += 1

        result = self.api(
            phase, "permission scheme", source_name, clone_name, "create exact clone",
            "POST", "/rest/api/3/permissionscheme",
            {"name": clone_name, "description": str(source.get("description") or "")},
            optional=False,
        )
        did = str((result or {}).get("id") or "") if self.apply else f"<new-permission-scheme:{clone_name}>"
        if not did:
            raise RuntimeError(f"Jira did not return an ID after creating permission scheme '{clone_name}'.")

        self._ensure_permission_grants(source, did)

        if self.apply:
            full = self.c.get(
                f"/rest/api/3/permissionscheme/{did}",
                params={"expand": "permissions,user,group,projectRole,field,all"},
            )
            dest_fp = self._permission_scheme_semantic_fingerprint(full, source=False)
            missing, user_extra, jira_managed_extra = self._permission_scheme_diff(source_fp, dest_fp)
            all_extra = user_extra + jira_managed_extra
            self.permission_scheme_report.append({
                "sourceName": source_name,
                "destinationName": clone_name,
                "destinationId": did,
                "sourceGrantCount": len(source_fp),
                "destinationGrantCount": len(dest_fp),
                "missing": missing,
                "extra": all_extra,
                "jiraManagedExtra": jira_managed_extra,
                "exact": not missing and not all_extra,
                "sourceCompatible": not missing and not user_extra,
            })
            if missing or user_extra:
                raise RuntimeError(
                    f"Permission scheme '{clone_name}' failed source-compatible verification. "
                    f"Missing grants: {missing[:10]}; non-system extra grants: {user_extra[:10]}"
                )
            detail = f"All {len(source_fp)} source grants are present."
            action = "verify exact"
            if jira_managed_extra:
                action = "verify source-compatible"
                detail += (
                    f" Jira added {len(jira_managed_extra)} platform-managed jira-guest-member grant(s); "
                    "these are recorded separately and do not count as source drift."
                )
            self.record(
                phase, "permission scheme grants", source_name, did, action,
                "success", detail
            )
        return did

    def ensure_permission_scheme(self) -> None:
        phase = "permissions"
        source = self.b.read(f"data/projects/{self.source_key}/permission_scheme.json", {}) or {}
        source_id = str(source.get("id", ""))
        name = str(source.get("name", ""))
        if not name:
            return

        try:
            schemes = unwrap(self.c.get("/rest/api/3/permissionscheme"), ("permissionSchemes", "values"))
        except JiraError:
            schemes = []

        source_fp = self._permission_scheme_semantic_fingerprint(source, source=True)
        same_name = first(schemes, lambda x: norm(x.get("name")) == norm(name))
        did = ""

        if same_name:
            same_id = str(same_name.get("id") or "")
            try:
                full = self.c.get(
                    f"/rest/api/3/permissionscheme/{same_id}",
                    params={"expand": "permissions,user,group,projectRole,field,all"},
                )
                dest_fp = self._permission_scheme_semantic_fingerprint(full, source=False)
            except JiraError as exc:
                dest_fp = []
                self.record(phase, "permission scheme", name, same_id, "compare", "warning", exc.message)

            missing, user_extra, jira_managed_extra = self._permission_scheme_diff(source_fp, dest_fp)
            if not missing and not user_extra:
                did = same_id
                action = "reuse exact" if not jira_managed_extra else "reuse source-compatible"
                detail = f"Existing destination scheme contains all {len(source_fp)} source grants."
                if jira_managed_extra:
                    detail += (
                        f" Jira also manages {len(jira_managed_extra)} jira-guest-member grant(s) on this site."
                    )
                self.record(phase, "permission scheme", name, did, action, "success", detail)
                self.permission_scheme_report.append({
                    "sourceName": name,
                    "destinationName": str(same_name.get("name") or name),
                    "destinationId": did,
                    "sourceGrantCount": len(source_fp),
                    "destinationGrantCount": len(dest_fp),
                    "missing": [],
                    "extra": list(jira_managed_extra),
                    "jiraManagedExtra": list(jira_managed_extra),
                    "exact": not jira_managed_extra,
                    "sourceCompatible": True,
                })
            else:
                # Never rewrite Jira's shared/default same-name scheme. Instead,
                # make a dedicated source-compatible clone and associate only that clone.
                extra = user_extra + jira_managed_extra
                self.record(
                    phase, "permission scheme", name, same_id, "preserve conflicting shared scheme",
                    "warning",
                    f"Destination same-name scheme differs (missing={len(missing)}, extra={len(extra)}); creating an exact project clone."
                )
                did = self._create_permission_scheme_clone(source, schemes)
        else:
            # No collision: preserve the source scheme name and clone all grants.
            result = self.api(
                phase, "permission scheme", name, name, "create",
                "POST", "/rest/api/3/permissionscheme",
                {"name": name, "description": str(source.get("description") or "")},
                optional=False,
            )
            did = str((result or {}).get("id") or "") if self.apply else f"<new-permission-scheme:{name}>"
            if did:
                self._ensure_permission_grants(source, did)
                if self.apply:
                    full = self.c.get(
                        f"/rest/api/3/permissionscheme/{did}",
                        params={"expand": "permissions,user,group,projectRole,field,all"},
                    )
                    dest_fp = self._permission_scheme_semantic_fingerprint(full, source=False)
                    missing, user_extra, jira_managed_extra = self._permission_scheme_diff(source_fp, dest_fp)
                    all_extra = user_extra + jira_managed_extra
                    self.permission_scheme_report.append({
                        "sourceName": name, "destinationName": name, "destinationId": did,
                        "sourceGrantCount": len(source_fp), "destinationGrantCount": len(dest_fp),
                        "missing": missing, "extra": all_extra,
                        "jiraManagedExtra": jira_managed_extra,
                        "exact": not missing and not all_extra,
                        "sourceCompatible": not missing and not user_extra,
                    })
                    if missing or user_extra:
                        raise RuntimeError(
                            f"Permission scheme '{name}' failed source-compatible verification. "
                            f"Missing grants: {missing[:10]}; non-system extra grants: {user_extra[:10]}"
                        )
                    detail = f"All {len(source_fp)} source grants are present."
                    action = "verify exact"
                    if jira_managed_extra:
                        action = "verify source-compatible"
                        detail += f" Jira manages {len(jira_managed_extra)} jira-guest-member grant(s)."
                    self.record(phase, "permission scheme grants", name, did, action, "success", detail)

        if did:
            self.idmap["permission_scheme"][source_id] = did
            self.api(
                phase, "permission scheme association", name, self.target_key, "associate",
                "PUT", f"/rest/api/3/project/{self.target_key}/permissionscheme",
                {"id": int(did) if str(did).isdigit() else did}, optional=False,
            )
            if self.apply:
                assigned = self.c.get(
                    f"/rest/api/3/project/{self.target_key}/permissionscheme",
                    params={"expand": "permissions,user,group,projectRole,field,all"},
                )
                assigned_id = str(assigned.get("id") or "")
                if assigned_id != str(did):
                    raise RuntimeError(
                        f"Permission scheme association verification failed: project {self.target_key} "
                        f"is assigned to {assigned_id}, expected {did}."
                    )
                assigned_fp = self._permission_scheme_semantic_fingerprint(assigned, source=False)
                missing, user_extra, jira_managed_extra = self._permission_scheme_diff(source_fp, assigned_fp)
                if missing or user_extra:
                    raise RuntimeError(
                        f"Project {self.target_key} is associated with permission scheme {did}, but its grants "
                        f"do not match the portable source configuration. Missing={missing[:10]}, "
                        f"non-system extra={user_extra[:10]}"
                    )
                action = "verify exact assigned scheme"
                detail = f"Assigned scheme {did} contains all {len(source_fp)} source grants."
                if jira_managed_extra:
                    action = "verify source-compatible assigned scheme"
                    detail += (
                        f" Jira injected {len(jira_managed_extra)} platform-managed jira-guest-member grant(s) "
                        "when the scheme was associated; these are destination guest-access policy, not source drift."
                    )
                    self.record(
                        phase, "permission scheme platform grants", "jira-guest-member", self.target_key,
                        "preserve Jira-managed guest access", "warning",
                        "; ".join(f"{perm}/{holder}" for perm, _htype, holder in jira_managed_extra[:20])
                    )
                self.record(
                    phase, "permission scheme association", name, self.target_key,
                    action, "success", detail
                )

    def ensure_notification_scheme(self) -> None:
        phase = "notifications"
        source = self.b.read(f"data/projects/{self.source_key}/notification_scheme.json", {}) or {}
        source_id = str(source.get("id", "")); name = str(source.get("name", ""))
        if not name:
            return
        try:
            schemes = self.c.paginate("/rest/api/3/notificationscheme", params={"expand": "all"}, key="values")
        except JiraError:
            schemes = []
        found = first(schemes, lambda x: norm(x.get("name")) == norm(name))
        if found:
            did = str(found.get("id")); self.record(phase, "notification scheme", name, did, "reuse", "success")
        else:
            role_by_name = {norm(x.get("name")): str(x.get("id")) for x in self.dest_project_roles}
            events = []
            for source_event in source.get("notificationSchemeEvents", []) or []:
                notifications = []
                for notice in source_event.get("notifications", []) or []:
                    ntype = str(notice.get("notificationType") or "")
                    item: dict[str, Any] = {"notificationType": ntype}
                    if ntype == "ProjectRole":
                        role_name = str((notice.get("projectRole") or {}).get("name") or "")
                        parameter = role_by_name.get(norm(role_name))
                        if not parameter:
                            self.manual_add(f"Notification recipient project role '{role_name}' could not be mapped and was skipped.")
                            continue
                        item["parameter"] = parameter
                    elif ntype in {"Group", "User", "EmailAddress"}:
                        self.manual_add(f"Notification recipient type '{ntype}' for event '{(source_event.get('event') or {}).get('name')}' requires destination identity mapping and was skipped.")
                        continue
                    elif ntype.endswith("CustomField"):
                        source_field = str(notice.get("parameter") or notice.get("recipient") or "")
                        dest_field = self.idmap["field"].get(source_field)
                        if not dest_field:
                            self.manual_add(f"Notification custom field '{source_field}' could not be mapped and was skipped.")
                            continue
                        item["parameter"] = dest_field
                    elif notice.get("parameter") is not None:
                        item["parameter"] = str(notice.get("parameter"))
                    notifications.append(item)
                event_id = (source_event.get("event") or {}).get("id")
                if event_id is not None:
                    events.append({"event": {"id": str(event_id)}, "notifications": notifications})
            body = {"name": name, "description": source.get("description", ""), "notificationSchemeEvents": events}
            result = self.api(phase, "notification scheme", name, "", "create", "POST", "/rest/api/3/notificationscheme", body)
            did = str((result or {}).get("id", "")) if self.apply else f"<new-notification-scheme:{name}>"
        if did:
            self.idmap["notification_scheme"][source_id] = did
            value: Any = int(did) if str(did).isdigit() else did
            self.api(phase, "notification scheme association", name, self.target_key, "associate", "PUT", f"/rest/api/3/project/{self.target_key}", {"notificationScheme": value})

    def ensure_components_versions_properties(self) -> None:
        phase = "project-config"
        components = self.b.read(f"data/projects/{self.source_key}/components.json", []) or []
        try: dest_components = self.c.get(f"/rest/api/3/project/{self.target_key}/components") if self.apply else []
        except JiraError: dest_components = []
        for src in components:
            name = str(src.get("name")); found = first(dest_components, lambda x: norm(x.get("name")) == norm(name))
            if found:
                did = str(found.get("id")); self.record(phase, "component", name, did, "reuse", "success")
            else:
                body = {"project": self.target_key, "name": name, "description": src.get("description", ""), "assigneeType": src.get("assigneeType", "PROJECT_DEFAULT")}
                result = self.api(phase, "component", name, "", "create", "POST", "/rest/api/3/component", body)
                did = str((result or {}).get("id", "")) if self.apply else f"<new-component:{name}>"
            if did: self.idmap["component"][str(src.get("id"))] = did
        versions = self.b.read(f"data/projects/{self.source_key}/versions.json", []) or []
        try: dest_versions = self.c.get(f"/rest/api/3/project/{self.target_key}/versions") if self.apply else []
        except JiraError: dest_versions = []
        for src in versions:
            name = str(src.get("name")); found = first(dest_versions, lambda x: norm(x.get("name")) == norm(name))
            if found:
                did = str(found.get("id")); self.record(phase, "version", name, did, "reuse", "success")
            else:
                body = {"projectId": int(self.target_project_id) if str(self.target_project_id).isdigit() else self.target_project_id, "name": name, "description": src.get("description", ""), "archived": bool(src.get("archived", False)), "released": bool(src.get("released", False))}
                for k in ("startDate", "releaseDate"):
                    if src.get(k): body[k] = src[k]
                result = self.api(phase, "version", name, "", "create", "POST", "/rest/api/3/version", body)
                did = str((result or {}).get("id", "")) if self.apply else f"<new-version:{name}>"
            if did: self.idmap["version"][str(src.get("id"))] = did
        props = self.b.read(f"data/projects/{self.source_key}/properties.json", {}) or {}
        for key, wrapper in (props.get("values") or {}).items():
            if key.startswith("jira-slack"):
                self.manual_add(f"Project property '{key}' is integration-specific and was not copied.")
                continue
            val = wrapper.get("value") if isinstance(wrapper, dict) else wrapper
            self.api(phase, "project property", key, self.target_key, "set", "PUT", f"/rest/api/3/project/{self.target_key}/properties/{key}", val)
        features_payload = self.b.read(f"data/projects/{self.source_key}/features.json", []) or []
        features = features_payload.get("features", []) if isinstance(features_payload, dict) else features_payload
        for feat in features:
            if not isinstance(feat, dict) or feat.get("toggleLocked"):
                continue
            key = feat.get("feature"); state = feat.get("state")
            if key and state in {"ENABLED", "DISABLED"}:
                self.api(phase, "project feature", key, state, "set", "PUT", f"/rest/api/3/project/{self.target_key}/features/{key}", {"state": state})

    def ensure_field_default_values(self) -> None:
        phase = "field-defaults"
        grouped_files = {p.stem: data for p, data in self.b.glob_json("data/fields/default_values_grouped/*.json")}
        legacy_files = {p.stem: data for p, data in self.b.glob_json("data/fields/default_values_legacy/*.json")}
        source_fields = {str(x.get("id")): x for x in (self.b.read("data/fields/fields.json", []) or [])}
        relevant = self.relevant_source_field_ids()

        if not grouped_files and not legacy_files:
            self.record(phase, "default values", "source export", "", "restore", "skipped", "No default-value data exists in this export.")
            return

        for sfid in sorted((set(grouped_files) | set(legacy_files)) & relevant):
            dfid = self.idmap["field"].get(sfid)
            if not dfid:
                continue
            defaults, source_mode = flatten_source_defaults(grouped_files.get(sfid), legacy_files.get(sfid))
            if not defaults:
                continue
            inferred_type = infer_default_value_type(source_fields.get(sfid, {}))
            transformed: list[dict[str, Any]] = []
            for src_default in defaults:
                mapped, error = self.map_default_value(sfid, src_default, inferred_type)
                if mapped is None:
                    self.default_value_report.append({
                        "sourceFieldId": sfid, "destinationFieldId": dfid,
                        "source": src_default, "status": "skipped", "reason": error,
                    })
                    self.manual_add(f"Default value for field {sfid} was not copied: {error}")
                    continue
                transformed.append(mapped)

            if not transformed:
                continue
            type_values = {str(x.get("type") or "") for x in transformed}
            if len(type_values) > 1:
                reason = f"Jira accepts one default-value type per field request, but source field {sfid} produced {sorted(type_values)}."
                self.manual_add(reason)
                self.record(phase, "field defaults", sfid, dfid, "set", "skipped", reason)
                continue

            body = {"defaultValues": transformed}
            before_actions = len(self.actions)
            self.api(
                phase, "field defaults", f"{sfid} ({source_mode})", dfid, "set", "PUT",
                f"/rest/api/2/field/{dfid}/context/defaultValue", body,
            )
            status = self.actions[-1].status if len(self.actions) > before_actions else "unknown"
            self.default_value_report.append({
                "sourceFieldId": sfid, "destinationFieldId": dfid,
                "sourceMode": source_mode, "defaultValues": transformed, "status": status,
            })

    def map_default_value(self, sfid: str, source: Mapping[str, Any], inferred_type: str) -> tuple[dict[str, Any] | None, str]:
        item = copy.deepcopy(dict(source))
        if item.get("_unsupportedPerIssueTypeDefaults") is not None:
            return None, "the source context has different defaults for different work types; Jira's current public write endpoint cannot reproduce that safely"
        source_context = str(item.get("contextId") or "")
        dest_context = self.idmap["field_context"].get(f"{sfid}:{source_context}")
        if not dest_context or str(dest_context).startswith("<"):
            return None, f"context {source_context} has no destination mapping"
        item["contextId"] = str(dest_context)
        item["type"] = str(item.get("type") or inferred_type or "")
        if not item["type"]:
            return None, "the default-value type could not be inferred"

        for key in ("optionId", "cascadingOptionId"):
            if item.get(key) is not None:
                mapped = self.idmap["field_option"].get(str(item.get(key)))
                if not mapped or str(mapped).startswith("<"):
                    return None, f"option {item.get(key)} has no destination mapping"
                item[key] = str(mapped)
        if isinstance(item.get("optionIds"), list):
            mapped_ids = []
            for value in item.get("optionIds") or []:
                mapped = self.idmap["field_option"].get(str(value))
                if not mapped or str(mapped).startswith("<"):
                    return None, f"option {value} has no destination mapping"
                mapped_ids.append(str(mapped))
            item["optionIds"] = mapped_ids

        if item.get("projectId") is not None:
            source_project = str(item.get("projectId"))
            mapped_project = self.idmap["project"].get(source_project)
            if not mapped_project:
                return None, f"project {source_project} has no destination mapping"
            item["projectId"] = str(mapped_project)
        for key in ("versionId",):
            if item.get(key) is not None:
                mapped = self.idmap["version"].get(str(item.get(key)))
                if not mapped:
                    return None, f"version {item.get(key)} has no destination mapping"
                item[key] = str(mapped)
        if isinstance(item.get("versionIds"), list):
            mapped_versions = []
            for value in item.get("versionIds") or []:
                mapped = self.idmap["version"].get(str(value))
                if not mapped:
                    return None, f"version {value} has no destination mapping"
                mapped_versions.append(str(mapped))
            item["versionIds"] = mapped_versions

        # The grouped read API wraps the actual polymorphic value. Scope markers
        # are intentionally removed because the current public write endpoint sets
        # one value for the whole context.
        item.pop("issueTypeId", None)
        item.pop("isAnyIssueType", None)
        return item, ""

    # ---------- saved-filter columns ----------
    def map_filter_columns(self, source_columns: list[Any]) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Map exported ColumnItem objects to ordered destination field IDs."""
        mapped: list[str] = []
        details: list[dict[str, Any]] = []
        problems: list[str] = []
        destination_by_id = {str(x.get("id") or x.get("key") or ""): x for x in self.dest_filter_fields if isinstance(x, dict)}

        for index, column in enumerate(source_columns):
            if isinstance(column, dict):
                source_id = str(column.get("value") or column.get("id") or "").strip()
                label = str(column.get("label") or source_id)
            else:
                source_id = str(column or "").strip()
                label = source_id
            if not source_id:
                problems.append(f"Column at position {index + 1} has no field ID.")
                continue

            # The merged Jira List can expose a fixed composite "Work" column.
            # The supported user/filter column APIs still use its constituent Jira
            # fields, so expand it deterministically for write-back.
            if norm(source_id) in {"work", "workitem", "work item"}:
                for constituent, constituent_label in (("issuetype", "Issue Type"), ("issuekey", "Key"), ("summary", "Summary")):
                    if constituent not in mapped:
                        mapped.append(constituent)
                        details.append({
                            "position": len(mapped), "label": constituent_label,
                            "sourceFieldId": source_id, "destinationFieldId": constituent,
                            "status": "expanded-from-work-composite",
                        })
                continue

            if source_id.startswith("customfield_"):
                destination_id = self.idmap["field"].get(source_id, "")
                if not destination_id:
                    problems.append(f"{label} ({source_id}) has no destination field mapping.")
                    details.append({
                        "position": index + 1, "label": label, "sourceFieldId": source_id,
                        "destinationFieldId": None, "status": "unmapped",
                    })
                    continue
            else:
                destination_id = source_id

            metadata = destination_by_id.get(str(destination_id))
            if metadata is not None and metadata.get("navigable") is False:
                problems.append(f"{label} ({destination_id}) is not navigable on the destination.")
                details.append({
                    "position": index + 1, "label": label, "sourceFieldId": source_id,
                    "destinationFieldId": destination_id, "status": "not-navigable",
                })
                continue

            if destination_id in mapped:
                problems.append(f"Duplicate mapped column {destination_id} at source position {index + 1} was ignored.")
                continue
            mapped.append(destination_id)
            details.append({
                "position": len(mapped), "label": label, "sourceFieldId": source_id,
                "destinationFieldId": destination_id, "status": "mapped",
            })
        return mapped, details, problems

    def ensure_filter_columns(self, phase: str, source_filter_id: str, filter_name: str,
                              destination_filter_id: str, source_columns: list[Any]) -> None:
        mapped_columns, mappings, problems = self.map_filter_columns(source_columns)
        report: dict[str, Any] = {
            "filterName": filter_name,
            "sourceFilterId": source_filter_id,
            "destinationFilterId": destination_filter_id,
            "sourceColumns": normalize_filter_column_items(source_columns),
            "mappings": mappings,
            "requestedDestinationOrder": mapped_columns,
            "actualDestinationOrder": None,
            "status": "planned" if not self.apply else "pending",
            "problems": list(problems),
            "transport": None,
        }
        self.filter_column_results.append(report)

        if problems:
            for problem in problems:
                self.manual_add(f"Filter '{filter_name}' columns: {problem}")

        if not mapped_columns:
            report["status"] = "skipped"
            self.record(phase, "filter columns", filter_name, destination_filter_id, "set exact order", "skipped",
                        "No valid mapped columns were available.")
            return

        if not self.apply or str(destination_filter_id).startswith("<"):
            self.record(phase, "filter columns", filter_name, destination_filter_id, "set exact order", "planned",
                        " -> ".join(mapped_columns))
            return

        path = f"/rest/api/3/filter/{destination_filter_id}/columns"
        # Atlassian documents this endpoint primarily as repeated form fields
        # (columns=summary&columns=status...). Use that canonical transport first
        # and keep JSON only as a compatibility fallback.
        try:
            form = [("columns", column_id) for column_id in mapped_columns]
            self.c.put_form(path, form)
            report["transport"] = "application/x-www-form-urlencoded"
        except JiraError as form_error:
            if form_error.status not in {400, 415}:
                report["status"] = "failed"
                report["error"] = f"HTTP {form_error.status}: {form_error.message}"
                self.record(phase, "filter columns", filter_name, destination_filter_id, "set exact order", "failed", report["error"])
                self.manual_add(f"Filter '{filter_name}' columns could not be set: {report['error']}")
                return
            try:
                self.c.put(path, {"columns": mapped_columns})
                report["transport"] = "application/json"
            except JiraError as json_error:
                report["status"] = "failed"
                report["error"] = (
                    f"form attempt: HTTP {form_error.status} - {form_error.message}; "
                    f"JSON attempt: HTTP {json_error.status} - {json_error.message}"
                )
                self.record(phase, "filter columns", filter_name, destination_filter_id, "set exact order", "failed", report["error"])
                self.manual_add(f"Filter '{filter_name}' columns could not be set: {report['error']}")
                return

        try:
            actual_payload = self.c.get(path) or []
            actual = [x["value"] for x in normalize_filter_column_items(actual_payload) if x.get("value")]
            report["actualDestinationOrder"] = actual
        except JiraError as exc:
            report["status"] = "unverified"
            report["verificationError"] = f"HTTP {exc.status}: {exc.message}"
            self.record(phase, "filter columns", filter_name, destination_filter_id, "set exact order", "warning",
                        f"Columns were submitted but could not be read back: {report['verificationError']}")
            self.manual_add(f"Verify the column order for filter '{filter_name}' manually; Jira did not allow read-back verification.")
            return

        if actual == mapped_columns:
            report["status"] = "verified"
            self.record(phase, "filter columns", filter_name, destination_filter_id, "set and verify exact order", "success",
                        " -> ".join(actual))
        else:
            report["status"] = "mismatch"
            note = f"Requested {mapped_columns}, Jira returned {actual}."
            self.record(phase, "filter columns", filter_name, destination_filter_id, "set and verify exact order", "warning", note)
            self.manual_add(f"Filter '{filter_name}' column order did not verify exactly. {note}")

    def ensure_filter_column_inheritance(self, phase: str, source_filter_id: str,
                                         filter_name: str, destination_filter_id: str,
                                         effective_source_columns: list[Any] | None = None,
                                         effective_source_mode: str = "") -> None:
        """Preserve a source filter that has no filter-specific column layout.

        Jira represents an inherited saved-filter layout by returning 404 from
        GET /filter/{id}/columns.  The correct destination operation is therefore
        DELETE /filter/{id}/columns (reset to My Defaults), not creating an explicit
        copy of whatever defaults happened to be effective on the source.
        """
        expected = normalize_filter_column_items(effective_source_columns or [])
        report: dict[str, Any] = {
            "filterName": filter_name,
            "sourceFilterId": source_filter_id,
            "destinationFilterId": destination_filter_id,
            "sourceColumns": expected,
            "mappings": [],
            "requestedDestinationOrder": None,
            "actualDestinationOrder": None,
            "status": "planned" if not self.apply else "pending",
            "problems": [],
            "transport": "DELETE-reset-to-user-defaults",
            "sourceColumnMode": effective_source_mode or "inherited-user-defaults",
            "sourceHasFilterSpecificColumnLayout": False,
        }
        self.filter_column_results.append(report)

        if not self.apply or str(destination_filter_id).startswith("<"):
            self.record(phase, "filter columns", filter_name, destination_filter_id,
                        "preserve inherited My Defaults", "planned",
                        "Source filter has no explicit column layout; destination will be reset to inherit user defaults.")
            return

        path = f"/rest/api/3/filter/{destination_filter_id}/columns"
        try:
            self.c.delete(path)
        except JiraError as exc:
            # Reset is idempotent in intent. Some Jira tenants can return 404 when
            # there was no explicit layout to remove; that already means inherited.
            if exc.status != 404:
                report["status"] = "failed"
                report["error"] = f"HTTP {exc.status}: {exc.message}"
                self.record(phase, "filter columns", filter_name, destination_filter_id,
                            "preserve inherited My Defaults", "warning", report["error"])
                self.manual_add(f"Filter '{filter_name}' could not be reset to inherited My Defaults: {report['error']}")
                return

        try:
            payload = self.c.get(path)
            actual = [x["value"] for x in normalize_filter_column_items(payload or []) if x.get("value")]
            report["actualDestinationOrder"] = actual
            # A 200 after DELETE means Jira still exposes an explicit layout. Treat
            # this as a mismatch instead of falsely claiming inheritance.
            report["status"] = "mismatch-explicit-layout-remains"
            note = f"Jira still returned an explicit filter layout after reset: {actual}."
            self.record(phase, "filter columns", filter_name, destination_filter_id,
                        "verify inherited My Defaults", "warning", note)
            self.manual_add(f"Filter '{filter_name}' should inherit My Defaults, but {note}")
        except JiraError as exc:
            if exc.status == 404:
                report["status"] = "verified-inherited-user-defaults"
                self.record(phase, "filter columns", filter_name, destination_filter_id,
                            "verify inherited My Defaults", "success",
                            "No filter-specific column layout exists (Jira 404), matching the source.")
            else:
                report["status"] = "unverified"
                report["verificationError"] = f"HTTP {exc.status}: {exc.message}"
                self.record(phase, "filter columns", filter_name, destination_filter_id,
                            "verify inherited My Defaults", "warning", report["verificationError"])

    def _board_project_key(self, board: Mapping[str, Any]) -> str:
        location = board.get("location") or {}
        return str(location.get("projectKey") or location.get("key") or "").upper()

    def _cleanup_template_default_board(
        self,
        phase: str,
        dest_boards: list[dict[str, Any]],
        source_board_names: set[str],
    ) -> list[dict[str, Any]]:
        """Remove only Jira's auto-created Scrum template board when it is not in the source.

        Creating a company-managed Scrum project through Jira's project API automatically creates
        a board named '<PROJECT KEY> board'. The source export can legitimately omit that board
        (for example, when it was deleted and replaced by Main Board / Active Sprint Board).
        Reusing the project in a later repair run must also be able to remove this known template
        artifact. We deliberately do *not* delete any other destination-only board.
        """
        expected_auto_name = f"{self.target_key} board"
        expected_auto_norm = norm(expected_auto_name)
        if expected_auto_norm in source_board_names:
            return dest_boards

        candidates = [
            b for b in dest_boards
            if self._board_project_key(b) == self.target_key
            and norm(b.get("name")) == expected_auto_norm
        ]
        if not candidates:
            self.board_cleanup_results.append({
                "expectedAutoBoardName": expected_auto_name,
                "destinationProject": self.target_key,
                "action": "none",
                "reason": "No Jira-template default board was present.",
            })
            return dest_boards

        remaining = list(dest_boards)
        for board in candidates:
            board_id = str(board.get("id", ""))
            board_name = str(board.get("name") or expected_auto_name)
            note = (
                "This board is Jira's automatically generated Scrum-template board and is not "
                "present in the source project's exported board set. No other destination-only "
                "board is eligible for automatic deletion."
            )
            if self.apply:
                try:
                    self.c.delete(f"/rest/agile/1.0/board/{board_id}")
                    self.record(phase, "template board", board_name, board_id, "delete source-absent auto board", "success", note)
                    remaining = [b for b in remaining if str(b.get("id", "")) != board_id]
                    status = "deleted"
                except JiraError as exc:
                    self.record(phase, "template board", board_name, board_id, "delete source-absent auto board", "failed",
                                f"HTTP {exc.status}: {exc.message}. {note}")
                    self.manual_add(
                        f"Delete Jira's auto-created board '{board_name}' (board {board_id}) from project {self.target_key}; "
                        f"the source export does not contain it. Jira returned HTTP {exc.status}: {exc.message}"
                    )
                    raise
            else:
                self.record(phase, "template board", board_name, board_id, "delete source-absent auto board", "planned", note)
                status = "planned"
            self.board_cleanup_results.append({
                "expectedAutoBoardName": expected_auto_name,
                "destinationProject": self.target_key,
                "boardId": board_id,
                "boardName": board_name,
                "action": status,
                "sourceContainsBoard": False,
                "projectCreatedThisRun": self.project_created_this_run,
            })
        return remaining

    # ---------- exported Epic work items ----------
    def _search_issues_jql(self, jql: str, fields: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Search destination issues using Jira's enhanced JQL API, with legacy fallback."""
        wanted = list(fields or ["summary", "issuetype", "status"])
        out: list[dict[str, Any]] = []
        token: str | None = None
        try:
            for _ in range(10000):
                params: dict[str, Any] = {"jql": jql, "maxResults": PAGE_SIZE, "fields": wanted}
                if token:
                    params["nextPageToken"] = token
                payload = self.c.get("/rest/api/3/search/jql", params=params) or {}
                if not isinstance(payload, dict):
                    break
                items = payload.get("issues") or []
                if not isinstance(items, list):
                    break
                out.extend(x for x in items if isinstance(x, dict))
                if payload.get("isLast") is True:
                    break
                token = payload.get("nextPageToken")
                if not token or not items:
                    break
            return out
        except JiraError as exc:
            if exc.status not in {404, 405, 410}:
                raise
        # Older Jira Cloud tenants may still expose only the startAt-based search API.
        start = 0
        for _ in range(10000):
            payload = self.c.get(
                "/rest/api/3/search",
                params={"jql": jql, "startAt": start, "maxResults": PAGE_SIZE, "fields": wanted},
            ) or {}
            if not isinstance(payload, dict):
                break
            items = payload.get("issues") or []
            if not isinstance(items, list):
                break
            out.extend(x for x in items if isinstance(x, dict))
            total = payload.get("total")
            if not items or (isinstance(total, int) and start + len(items) >= total):
                break
            start += len(items)
        return out

    def _create_fields_for_issue_type(self, issue_type_id: str) -> dict[str, Any] | None:
        if issue_type_id in self._create_meta_cache:
            return self._create_meta_cache[issue_type_id]
        try:
            result: dict[str, Any] = {}
            start = 0
            for _ in range(1000):
                payload = self.c.get(
                    f"/rest/api/3/issue/createmeta/{self.target_key}/issuetypes/{issue_type_id}",
                    params={"startAt": start, "maxResults": PAGE_SIZE},
                ) or {}
                if not isinstance(payload, dict):
                    break
                raw_fields = payload.get("fields")
                if isinstance(raw_fields, dict):
                    result.update(raw_fields)
                    break
                if not isinstance(raw_fields, list):
                    break
                for item in raw_fields:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("fieldId") or item.get("key") or "")
                    if key:
                        result[key] = item
                total = payload.get("total")
                if not raw_fields or (isinstance(total, int) and start + len(raw_fields) >= total):
                    break
                start += len(raw_fields)
            result = result or None
        except JiraError as exc:
            result = None
            self.record(
                "epics", "create metadata", issue_type_id, self.target_key, "read", "warning",
                f"Could not read create metadata (HTTP {exc.status}); Epic import will use a conservative field set.",
            )
        self._create_meta_cache[issue_type_id] = result
        return result

    def _map_issue_custom_value(self, value: Any) -> tuple[Any, bool]:
        """Translate IDs inside an exported custom-field value to destination IDs.

        Returns (mapped_value, safe).  Unsafe opaque Jira-managed values are omitted rather
        than sent with source-site IDs.
        """
        if value is None or isinstance(value, (str, int, float, bool)):
            return value, True
        if isinstance(value, list):
            mapped_items: list[Any] = []
            for item in value:
                mapped, safe = self._map_issue_custom_value(item)
                if not safe:
                    return None, False
                mapped_items.append(mapped)
            return mapped_items, True
        if not isinstance(value, dict):
            return None, False
        # Atlassian Document Format can be copied verbatim.
        if value.get("type") == "doc" and isinstance(value.get("content"), list):
            return copy.deepcopy(value), True
        if value.get("accountId"):
            return {"accountId": str(value.get("accountId"))}, True
        source_id = str(value.get("id") or "")
        if source_id:
            if source_id in self.idmap["field_option"]:
                mapped = self.idmap["field_option"][source_id]
                if mapped and not str(mapped).startswith("<"):
                    return {"id": str(mapped)}, True
            if source_id in self.idmap["version"]:
                return {"id": str(self.idmap["version"][source_id])}, True
            if source_id in self.idmap["component"]:
                return {"id": str(self.idmap["component"][source_id])}, True
            # Many select-like Jira fields also accept their option value.  This is safer
            # than sending a source-site numeric option id when an option mapping is absent.
            if value.get("value") is not None:
                return {"value": value.get("value")}, True
            if value.get("name") is not None and len(value) <= 8:
                return {"name": value.get("name")}, True
            return None, False
        # ADF fragments and plain JSON custom values without site-specific IDs are safe.
        clean = copy.deepcopy(value)
        for key in ("self", "avatarUrls", "iconUrl"):
            clean.pop(key, None)
        return clean, True

    def _build_epic_create_fields(
        self, issue: Mapping[str, Any], dest_issue_type_id: str
    ) -> tuple[dict[str, Any], list[str]]:
        source = issue.get("fields") or {}
        if not isinstance(source, dict):
            source = {}
        fields: dict[str, Any] = {
            "project": {"id": self.target_project_id},
            "issuetype": {"id": dest_issue_type_id},
            "summary": str(source.get("summary") or issue.get("key") or "Imported Epic"),
        }
        dropped: list[str] = []

        # Standard fields whose value shapes are portable or have explicit ID mappings.
        for key in ("description", "environment", "duedate"):
            if source.get(key) is not None:
                fields[key] = copy.deepcopy(source.get(key))
        if isinstance(source.get("labels"), list):
            fields["labels"] = list(source.get("labels") or [])
        components = []
        for component in source.get("components") or []:
            if not isinstance(component, dict):
                continue
            mapped = self.idmap["component"].get(str(component.get("id") or ""))
            if mapped:
                components.append({"id": str(mapped)})
        if components:
            fields["components"] = components
        versions = []
        for version in source.get("fixVersions") or []:
            if not isinstance(version, dict):
                continue
            mapped = self.idmap["version"].get(str(version.get("id") or ""))
            if mapped:
                versions.append({"id": str(mapped)})
        if versions:
            fields["fixVersions"] = versions
        priority = source.get("priority") or {}
        if isinstance(priority, dict) and priority.get("id"):
            mapped = self.idmap["priority"].get(str(priority.get("id")))
            if mapped:
                fields["priority"] = {"id": str(mapped)}
        for key in ("assignee", "reporter"):
            user = source.get(key) or {}
            if isinstance(user, dict) and user.get("accountId"):
                fields[key] = {"accountId": str(user.get("accountId"))}

        # Copy mapped custom fields only.  Create metadata below removes fields that are not
        # on the destination Epic create screen.
        for source_field_id, value in source.items():
            if not str(source_field_id).startswith("customfield_") or value is None:
                continue
            dest_field_id = self.idmap["field"].get(str(source_field_id))
            if not dest_field_id:
                dropped.append(str(source_field_id))
                continue
            mapped, safe = self._map_issue_custom_value(value)
            if safe:
                fields[str(dest_field_id)] = mapped
            else:
                dropped.append(str(source_field_id))

        meta = self._create_fields_for_issue_type(dest_issue_type_id) if self.apply else None
        if isinstance(meta, dict):
            allowed = set(meta)
            for key in list(fields):
                if key in {"project", "issuetype", "summary"}:
                    continue
                if key not in allowed:
                    fields.pop(key, None)
                    dropped.append(key)
        return fields, sorted(set(dropped))

    def _create_epic_adaptive(
        self, source_key: str, fields: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        if not self.apply:
            self.record("epics", "Epic", source_key, "", "create", "planned")
            return {"id": f"<new-epic:{source_key}>", "key": f"<{self.target_key}-new:{source_key}>"}, []
        working = copy.deepcopy(fields)
        removed: list[str] = []
        core = {"project", "issuetype", "summary"}
        for _ in range(5):
            body: dict[str, Any] = {
                "fields": working,
                "properties": [{
                    "key": "jira-clone.source",
                    "value": {
                        "sourceSite": self.b.manifest.get("sourceSite"),
                        "sourceProject": self.source_key,
                        "sourceKey": source_key,
                    },
                }],
            }
            try:
                result = self.c.post("/rest/api/3/issue", body)
                self.record("epics", "Epic", source_key, (result or {}).get("key", ""), "create", "success")
                return result if isinstance(result, dict) else {}, removed
            except JiraError as exc:
                if exc.status != 400:
                    self.record("epics", "Epic", source_key, "", "create", "failed", f"HTTP {exc.status}: {exc.message}")
                    raise
                try:
                    payload = json.loads(exc.body) if exc.body else {}
                except Exception:
                    payload = {}
                errors = payload.get("errors") if isinstance(payload, dict) else {}
                removable = [str(k) for k in (errors or {}) if str(k) in working and str(k) not in core]
                if not removable:
                    self.record("epics", "Epic", source_key, "", "create", "failed", f"HTTP 400: {exc.message}")
                    raise
                for key in removable:
                    working.pop(key, None)
                    removed.append(key)
                self.record(
                    "epics", "Epic fields", source_key, ", ".join(removable), "retry without rejected fields", "warning",
                    "Jira rejected these non-core create fields; the importer removed them and retried.",
                )
        raise RuntimeError(f"Could not create Epic {source_key} after removing rejected optional fields.")

    def _sync_epic_status(self, dest_key: str, source_status: Mapping[str, Any] | None) -> str:
        source_status = source_status or {}
        source_id = str(source_status.get("id") or "")
        source_name = str(source_status.get("name") or "")
        target_id = self.idmap["status"].get(source_id, "")
        if not self.apply:
            self.record("epics", "Epic status", source_name or source_id, dest_key, "transition", "planned")
            return "planned"
        try:
            current = self.c.get(f"/rest/api/3/issue/{dest_key}", params={"fields": "status"}) or {}
            current_status = ((current.get("fields") or {}).get("status") or {}) if isinstance(current, dict) else {}
            if (target_id and str(current_status.get("id")) == str(target_id)) or (
                source_name and norm(current_status.get("name")) == norm(source_name)
            ):
                self.record("epics", "Epic status", source_name or source_id, dest_key, "already correct", "success")
                return "already-correct"
            transitions = self.c.get(f"/rest/api/3/issue/{dest_key}/transitions") or {}
            choices = transitions.get("transitions") or [] if isinstance(transitions, dict) else []
            match = first(
                choices,
                lambda t: (
                    target_id and str((t.get("to") or {}).get("id")) == str(target_id)
                ) or (
                    source_name and norm((t.get("to") or {}).get("name")) == norm(source_name)
                ),
            )
            if match and match.get("id"):
                self.c.post(f"/rest/api/3/issue/{dest_key}/transitions", {"transition": {"id": str(match.get("id"))}})
                self.record("epics", "Epic status", source_name or source_id, dest_key, "transition", "success")
                return "transitioned"
            note = f"No currently available transition reaches source status '{source_name or source_id}'."
            self.record("epics", "Epic status", source_name or source_id, dest_key, "transition", "warning", note)
            self.manual_add(f"Epic {dest_key}: {note}")
            return "manual"
        except JiraError as exc:
            note = f"Could not restore Epic status: HTTP {exc.status} - {exc.message}"
            self.record("epics", "Epic status", source_name or source_id, dest_key, "transition", "warning", note)
            self.manual_add(f"Epic {dest_key}: {note}")
            return "failed"

    def ensure_epics(self) -> None:
        phase = "epics"
        payload = self.b.read(f"data/issues/epics/by_project/{self.source_key}.json", None)
        issues: list[dict[str, Any]] = []
        if isinstance(payload, list):
            issues = [x for x in payload if isinstance(x, dict)]
        elif isinstance(payload, dict):
            raw = payload.get("issues") or payload.get("values") or []
            if isinstance(raw, list):
                issues = [x for x in raw if isinstance(x, dict)]
        if not issues:
            selected = self.b.read("data/issues/epics/selected_projects.json", {}) or {}
            raw = selected.get("issues") if isinstance(selected, dict) else []
            if isinstance(raw, list):
                issues = [
                    x for x in raw if isinstance(x, dict)
                    and str((((x.get("fields") or {}).get("project") or {}).get("key") or "")).upper() == self.source_key
                ]
        if not issues:
            self.record(phase, "Epics", self.source_key, self.target_key, "import", "skipped", "No exported Epics were found for this project.")
            return

        destination_cache: dict[str, list[dict[str, Any]]] = {}
        for issue in issues:
            source_key = str(issue.get("key") or issue.get("id") or "Epic")
            src_fields = issue.get("fields") or {}
            src_type = src_fields.get("issuetype") or {} if isinstance(src_fields, dict) else {}
            source_type_id = str(src_type.get("id") or "") if isinstance(src_type, dict) else ""
            dest_type_id = self.idmap["issue_type"].get(source_type_id, "")
            if not dest_type_id:
                source_type_name = str(src_type.get("name") or "Epic") if isinstance(src_type, dict) else "Epic"
                found_type = first(self.dest_issue_types, lambda x: norm(x.get("name")) == norm(source_type_name))
                dest_type_id = str((found_type or {}).get("id") or "")
            if not dest_type_id or str(dest_type_id).startswith("<"):
                note = f"No destination work-type mapping exists for source Epic type {source_type_id}."
                self.record(phase, "Epic", source_key, "", "import", "failed" if self.apply else "warning", note)
                self.manual_add(note)
                continue

            if dest_type_id not in destination_cache and self.apply:
                try:
                    destination_cache[dest_type_id] = self._search_issues_jql(
                        f'project = "{self.target_key}" AND issuetype = {dest_type_id}',
                        ["summary", "issuetype", "status", "components"],
                    )
                except JiraError as exc:
                    destination_cache[dest_type_id] = []
                    self.record(phase, "destination Epic scan", dest_type_id, self.target_key, "search", "warning", f"HTTP {exc.status}: {exc.message}")
            summary = str(src_fields.get("summary") or "") if isinstance(src_fields, dict) else ""
            matches = [
                x for x in destination_cache.get(dest_type_id, [])
                if norm(((x.get("fields") or {}).get("summary"))) == norm(summary)
            ]
            dest_issue: dict[str, Any] | None = matches[0] if len(matches) == 1 else None
            action = "reuse" if dest_issue else "create"
            dropped: list[str] = []
            if dest_issue:
                dest_key = str(dest_issue.get("key") or "")
                dest_id = str(dest_issue.get("id") or "")
                self.record(phase, "Epic", source_key, dest_key, "reuse exact summary/type match", "success")
            else:
                fields, pre_dropped = self._build_epic_create_fields(issue, dest_type_id)
                dropped.extend(pre_dropped)
                created, retry_dropped = self._create_epic_adaptive(source_key, fields)
                dropped.extend(retry_dropped)
                dest_key = str((created or {}).get("key") or "")
                dest_id = str((created or {}).get("id") or "")
                if self.apply and created:
                    destination_cache.setdefault(dest_type_id, []).append({
                        "id": dest_id, "key": dest_key,
                        "fields": {"summary": summary, "issuetype": {"id": dest_type_id}},
                    })
            if not dest_key:
                continue
            self.idmap["epic"][source_key] = dest_key
            self.idmap["issue"][source_key] = dest_key
            if issue.get("id") and dest_id:
                self.idmap["issue"][str(issue.get("id"))] = dest_id
            if self.apply and not dest_key.startswith("<"):
                try:
                    self.c.put(
                        f"/rest/api/3/issue/{dest_key}/properties/jira-clone.source",
                        {"sourceSite": self.b.manifest.get("sourceSite"), "sourceProject": self.source_key, "sourceKey": source_key},
                    )
                except JiraError as exc:
                    self.record(phase, "Epic source marker", source_key, dest_key, "set property", "warning", f"HTTP {exc.status}: {exc.message}")
            status_result = self._sync_epic_status(dest_key, src_fields.get("status") if isinstance(src_fields, dict) else {})
            self.epic_import_results.append({
                "sourceKey": source_key,
                "destinationKey": dest_key,
                "action": action,
                "sourceSummary": summary,
                "sourceStatus": str(((src_fields.get("status") or {}).get("name") or "")) if isinstance(src_fields, dict) else "",
                "statusRestore": status_result,
                "droppedCreateFields": sorted(set(dropped)),
            })

    # ---------- Timeline / List view replication ----------
    def _source_timeline_properties(self, source_board_id: str) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for path, payload in self.b.glob_json(f"data/boards/properties/{source_board_id}__*.json"):
            if path.stem.endswith("__keys") or not isinstance(payload, dict):
                continue
            key = str(payload.get("key") or path.stem.split("__", 1)[-1])
            if not key.startswith("jsw-roadmaps-"):
                continue
            if "value" in payload:
                props[key] = payload.get("value")
        # v1.5+/v1.6 also exported a normalized candidate file.  Use it as a fallback
        # if board-property capture was unavailable on a tenant.
        if not props:
            candidates = self.b.read(f"data/boards/timeline_candidates/{source_board_id}.json", {}) or {}
            mappings = {
                "isRoadmapEnabled": "jsw-roadmaps-classic-board-enable-roadmaps",
                "isChildIssuePlanningEnabled": "jsw-roadmaps-cmp-enable-child-issue-planning",
                "prefersChildIssueDatePlanning": "jsw-roadmaps-prefer-child-issue-date-planning",
            }
            for item in candidates.get("matches", []) if isinstance(candidates, dict) else []:
                if not isinstance(item, dict):
                    continue
                key = mappings.get(str(item.get("path")))
                if key:
                    props[key] = item.get("value")
        return props

    def _source_board_column_payload(self, source_board_id: str, cfg: Mapping[str, Any], dest_board_id: str) -> tuple[dict[str, Any], list[str]]:
        """Build the GreenHopper board-column payload with destination status IDs.

        Jira's public Agile REST API exposes board columns read-only.  Jira Cloud's
        own Board settings UI still uses the internal GreenHopper
        ``rapidviewconfig/columns`` resource.  The source exporter already captured
        both the public columnConfig and the Board-settings edit model.
        """
        source_statuses = {
            str(x.get("id")): str(x.get("name") or "")
            for x in (self.b.read("data/workflows/statuses.json", []) or [])
            if isinstance(x, dict) and x.get("id")
        }
        # Refresh because workflows may have been created/associated earlier in this run.
        if self.apply:
            try:
                self.dest_statuses = self.c.get("/rest/api/3/status") or self.dest_statuses
            except JiraError:
                pass
        dest_by_name = {norm(x.get("name")): x for x in self.dest_statuses if isinstance(x, dict)}

        edit_model = self.b.read(f"data/boards/editmodel/{source_board_id}.json", {}) or {}
        rapid = edit_model.get("rapidListConfig") if isinstance(edit_model, dict) else {}
        source_columns = rapid.get("mappedColumns") if isinstance(rapid, dict) else None
        if not isinstance(source_columns, list) or not source_columns:
            source_columns = []
            for col in ((cfg.get("columnConfig") or {}).get("columns") or []):
                source_columns.append({
                    "name": col.get("name"),
                    "min": "",
                    "max": "",
                    "isKanPlanColumn": False,
                    "mappedStatuses": [
                        {"id": str(st.get("id")), "name": source_statuses.get(str(st.get("id")), "")}
                        for st in (col.get("statuses") or [])
                    ],
                })

        mapped_columns: list[dict[str, Any]] = []
        problems: list[str] = []
        for col in source_columns:
            if not isinstance(col, dict):
                continue
            mapped_statuses: list[dict[str, str]] = []
            for st in col.get("mappedStatuses") or []:
                if not isinstance(st, dict):
                    continue
                source_status_id = str(st.get("id") or "")
                source_name = str(st.get("name") or source_statuses.get(source_status_id) or "")
                destination_id = str(self.idmap.get("status", {}).get(source_status_id) or "")
                if not destination_id or not destination_id.isdigit():
                    dest = dest_by_name.get(norm(source_name)) if source_name else None
                    destination_id = str((dest or {}).get("id") or "")
                if destination_id:
                    mapped_statuses.append({"id": destination_id})
                else:
                    problems.append(
                        f"Column '{col.get('name')}' status '{source_name or source_status_id}' has no destination status mapping."
                    )
            mapped_columns.append({
                "mappedStatuses": mapped_statuses,
                "name": str(col.get("name") or ""),
                "min": str(col.get("min") or ""),
                "max": str(col.get("max") or ""),
                "isKanPlanColumn": bool(col.get("isKanPlanColumn", False)),
            })

        current_statistics = {"id": "none_"}
        if isinstance(rapid, dict) and isinstance(rapid.get("currentStatisticsField"), dict):
            src_stat = copy.deepcopy(rapid.get("currentStatisticsField"))
            field_id = str(src_stat.get("fieldId") or "")
            if field_id.startswith("customfield_"):
                dest_field = self.idmap.get("field", {}).get(field_id)
                if dest_field:
                    src_stat["fieldId"] = dest_field
                    old_id = str(src_stat.get("id") or "")
                    if old_id:
                        src_stat["id"] = old_id.replace(field_id, str(dest_field))
            current_statistics = src_stat

        payload = {
            "currentStatisticsField": current_statistics,
            "rapidViewId": int(dest_board_id) if str(dest_board_id).isdigit() else dest_board_id,
            "mappedColumns": mapped_columns,
        }
        return payload, problems

    @staticmethod
    def _normalized_board_columns(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
        columns = ((cfg.get("columnConfig") or {}).get("columns") or []) if isinstance(cfg, Mapping) else []
        return [
            {
                "name": str(col.get("name") or ""),
                "statuses": [str(st.get("id")) for st in (col.get("statuses") or []) if st.get("id") is not None],
            }
            for col in columns if isinstance(col, dict)
        ]

    def ensure_board_columns(self, source_board_id: str, dest_board_id: str, board_name: str, cfg: Mapping[str, Any]) -> None:
        """Restore board columns, order, and status mappings, then verify read-back."""
        phase = "board-columns"
        payload, problems = self._source_board_column_payload(source_board_id, cfg, dest_board_id)
        result: dict[str, Any] = {
            "sourceBoardId": source_board_id,
            "destinationBoardId": dest_board_id,
            "boardName": board_name,
            "requested": payload.get("mappedColumns") or [],
            "problems": problems,
            "status": "planned" if not self.apply else "pending",
        }
        self.board_column_results.append(result)
        for problem in problems:
            self.manual_add(f"Board '{board_name}': {problem}")

        # Never submit a destructive whole-column replacement if any source status
        # could not be mapped.  A partial payload could silently drop a column/status.
        source_status_count = sum(
            len((col.get("statuses") or []))
            for col in (((cfg.get("columnConfig") or {}).get("columns") or []))
            if isinstance(col, dict)
        )
        mapped_status_count = sum(len(col.get("mappedStatuses") or []) for col in payload.get("mappedColumns") or [])
        if problems or mapped_status_count != source_status_count:
            note = f"Refusing partial board-column write: mapped {mapped_status_count}/{source_status_count} source statuses."
            result["status"] = "skipped-incomplete-mapping"
            self.record(phase, "board columns", board_name, dest_board_id, "restore", "warning", note)
            return

        desired = [
            {"name": str(col.get("name") or ""), "statuses": [str(st.get("id")) for st in (col.get("mappedStatuses") or [])]}
            for col in payload.get("mappedColumns") or []
        ]

        if not self.apply:
            self.record(phase, "board columns", board_name, dest_board_id, "set exact order/status mapping", "planned",
                        " -> ".join(col["name"] for col in desired))
            return

        # First avoid rewriting if this is a rerun and the destination is already exact.
        try:
            before = self.c.get(f"/rest/agile/1.0/board/{dest_board_id}/configuration") or {}
            before_norm = self._normalized_board_columns(before)
            if before_norm == desired:
                result["status"] = "already-verified"
                result["actual"] = before_norm
                self.record(phase, "board columns", board_name, dest_board_id, "already exact", "success",
                            " -> ".join(col["name"] for col in before_norm))
                return
        except JiraError:
            pass

        try:
            self.c.put("/rest/greenhopper/1.0/rapidviewconfig/columns", payload)
            actual_cfg = self.c.get(f"/rest/agile/1.0/board/{dest_board_id}/configuration") or {}
            actual = self._normalized_board_columns(actual_cfg)
            result["actual"] = actual
            if actual == desired:
                result["status"] = "verified"
                self.record(phase, "board columns", board_name, dest_board_id, "set and verify exact order/status mapping", "success",
                            " -> ".join(col["name"] for col in actual))
            else:
                result["status"] = "mismatch"
                note = f"Requested {desired}; Jira returned {actual}."
                self.record(phase, "board columns", board_name, dest_board_id, "set and verify", "warning", note)
                self.manual_add(f"Board '{board_name}' column configuration did not verify exactly. {note}")
        except JiraError as exc:
            result["status"] = "failed"
            result["error"] = f"HTTP {exc.status}: {exc.message}"
            self.record(phase, "board columns", board_name, dest_board_id, "set via Jira Board settings endpoint", "warning", result["error"])
            self.manual_add(
                f"Board '{board_name}' columns could not be restored automatically: {result['error']}. "
                "Jira does not provide a supported public board-column write API; v3.9 uses the same internal GreenHopper resource used by Board settings, which Atlassian may change."
            )

    def ensure_board_timeline_settings(self, source_board_id: str, dest_board_id: str, board_name: str) -> None:
        phase = "board-timeline"
        props = self._source_timeline_properties(source_board_id)
        if not props:
            self.record(phase, "Timeline", board_name, dest_board_id, "restore", "skipped", "No Timeline board-property data was exported.")
            self.board_timeline_results.append({
                "sourceBoardId": source_board_id, "destinationBoardId": dest_board_id,
                "boardName": board_name, "status": "not-captured", "properties": {},
            })
            return
        result: dict[str, Any] = {
            "sourceBoardId": source_board_id,
            "destinationBoardId": dest_board_id,
            "boardName": board_name,
            "properties": {},
        }
        for key, value in props.items():
            if not self.apply:
                self.record(phase, "Timeline property", key, dest_board_id, f"set {json.dumps(value)}", "planned")
                result["properties"][key] = {"source": value, "status": "planned"}
                continue
            try:
                self.c.put(f"/rest/agile/1.0/board/{dest_board_id}/properties/{key}", value)
                actual = self.c.get(f"/rest/agile/1.0/board/{dest_board_id}/properties/{key}")
                if isinstance(actual, dict) and "value" in actual:
                    actual_value = actual.get("value")
                else:
                    actual_value = actual
                ok = actual_value == value
                self.record(
                    phase, "Timeline property", key, dest_board_id, "set and verify", "success" if ok else "warning",
                    "" if ok else f"Source={value!r}; destination read-back={actual_value!r}",
                )
                result["properties"][key] = {"source": value, "destination": actual_value, "status": "verified" if ok else "mismatch"}
            except JiraError as exc:
                note = f"HTTP {exc.status}: {exc.message}"
                self.record(phase, "Timeline property", key, dest_board_id, "set", "warning", note)
                self.manual_add(f"Board '{board_name}': Timeline property '{key}' could not be restored ({note}).")
                result["properties"][key] = {"source": value, "status": "failed", "error": note}
        result["status"] = "processed"
        self.board_timeline_results.append(result)

    def ensure_list_view_settings(self) -> None:
        """Restore All Work defaults and reconcile the modern space List column order.

        Jira exposes a supported user-default column API, but Atlassian does not
        document a public REST write API for the newer *space saved-view* model.
        v3.11 keeps those scopes separate and preserves filter inheritance:

        * All Work / My defaults -> form-encoded PUT /rest/api/3/user/columns, exact read-back.
        * Saved-filter columns -> handled independently by ensure_filter_columns().
        * Space List -> verified against the same user defaults when the source
          capture says that is the effective source; otherwise reported honestly
          as a saved-view/manual gap rather than silently overwriting another scope.
        """
        phase = "list-view"

        def read_column_source(path: str) -> list[Any]:
            payload = self.b.read(path, None)
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict) and isinstance(payload.get("columns"), list):
                return payload.get("columns") or []
            return []

        # --- All Work / authenticated-user defaults ---
        all_work_probe = self.b.read("data/all_work/current_fields.json", {}) or {}
        all_work_columns = read_column_source("data/all_work/current_fields.json")
        user_default_columns = read_column_source("data/list_views/user_default_columns.json")
        # /rest/api/3/user/columns is the supported source for the user's
        # persistent My Defaults configuration and is therefore authoritative for
        # write-back.  The CSV current-fields probe is only evidence of what happened
        # to be displayed/exported at capture time; it can differ when Jira is
        # showing a saved filter, Filter columns, or another transient search state.
        # v3.10 incorrectly preferred that probe and could overwrite a 19-column
        # My Defaults layout with a 15-column transient view.
        source_columns = user_default_columns or all_work_columns
        source_mode = "jira-user-default-columns" if user_default_columns else "all-work-current-fields-probe-fallback"

        # The exported My Defaults belong to the account that authenticated against
        # the *source* site. Jira account IDs are Atlassian-account identifiers and
        # remain stable across sites. Previous versions wrote /user/columns without
        # accountId, which silently configured whichever admin account happened to
        # run the importer. If the browser user is the source account but the script
        # credentials are a different Jira admin, All work items therefore continued
        # to show the destination System defaults (Work -> Assignee -> Reporter ...).
        source_account = self.b.read("data/site/authenticated_account.json", {}) or {}
        source_account_id = str(source_account.get("accountId") or "").strip() if isinstance(source_account, dict) else ""
        destination_auth_account_id = str(self.dest_me.get("accountId") or "").strip()
        all_work_user_account_id = source_account_id or destination_auth_account_id

        # v3.14: the visible All work items table can be on either My defaults or
        # System. A normal v3.13 run always forced scope to user, so a destination
        # currently rendering System columns stayed Work -> Assignee -> Reporter
        # even after /user/columns verified successfully. The selected scope is now
        # honored on every run; default is both for clone fidelity.
        #
        # Jira's modern All work items page can keep a visible column selection that
        # does not match either /rest/api/3/user/columns or CSV current-fields.
        # When the operator supplies the visible source layout explicitly, prefer it.
        # 'work' is expanded by map_filter_columns() to issuetype + issuekey + summary.
        if self.all_work_visible_columns:
            tokens = [x.strip() for x in self.all_work_visible_columns.split(",") if x.strip()]
            if tokens:
                source_columns = [{"label": x, "value": x} for x in tokens]
                source_mode = "operator-visible-all-work-override"

        capture = self.b.read(f"data/projects/{self.source_key}/list_view/column_capture.json", {}) or {}
        project_columns = capture.get("columns") if isinstance(capture, dict) else []
        if not isinstance(project_columns, list):
            project_columns = []
        project_mode = str(capture.get("chosenSource") or "") if isinstance(capture, dict) else ""
        project_exact = bool(capture.get("exactProjectListColumnOrderCaptured")) if isinstance(capture, dict) else False
        project_scope = str(capture.get("chosenScope") or "") if isinstance(capture, dict) else ""

        # Older exports may have only project capture/user defaults.
        if not source_columns and project_columns and project_mode in {
            "jira_user_default_columns", "jira_project_current_fields_probe"
        }:
            source_columns = project_columns
            source_mode = project_mode

        if not source_columns and not project_columns:
            note = "No ordered All Work/user-default or project List columns were exported."
            self.record(phase, "All Work/List columns", self.source_key, self.target_key, "restore", "warning", note)
            self.manual_add(note)
            self.list_view_results.append({
                "view": "all-work-and-space-list",
                "sourceProject": self.source_key,
                "destinationProject": self.target_key,
                "status": "not-captured",
                "note": note,
            })
            return

        self.reconcile_filter_field_mappings(self.selected_source_filters())
        mapped_all_work, all_work_mappings, all_work_problems = self.map_filter_columns(source_columns)
        mapped_project, project_mappings, project_problems = self.map_filter_columns(project_columns)

        all_work_report: dict[str, Any] = {
            "view": "all-work-user-defaults",
            "sourceProject": self.source_key,
            "destinationProject": self.target_key,
            "sourceMode": source_mode,
            "sourceColumns": normalize_filter_column_items(source_columns),
            "csvProbe": all_work_probe if isinstance(all_work_probe, dict) else None,
            "mappings": all_work_mappings,
            "requestedDestinationOrder": mapped_all_work,
            "actualDestinationOrder": None,
            "problems": all_work_problems,
            "sourceAuthenticatedAccountId": source_account_id or None,
            "destinationImporterAccountId": destination_auth_account_id or None,
            "targetUserAccountId": all_work_user_account_id or None,
            "writeScope": (
                "source authenticated user's Jira default columns on destination"
                if self.all_work_column_scope == "user"
                else ("site-wide Jira System columns" if self.all_work_column_scope == "system"
                      else "both authenticated-user My defaults and site-wide System columns")
            ),
            "status": "planned" if not self.apply else "pending",
        }
        self.list_view_results.append(all_work_report)
        for problem in all_work_problems:
            self.manual_add(f"All Work columns: {problem}")

        actual: list[str] = []
        if mapped_all_work:
            scope = self.all_work_column_scope
            targets: list[tuple[str, str, dict[str, Any] | None]] = []
            if scope in {"user", "both"}:
                user_params = {"accountId": all_work_user_account_id} if all_work_user_account_id else None
                targets.append(("My defaults", "/rest/api/3/user/columns", user_params))
            if scope in {"system", "both"}:
                targets.append(("System defaults", "/rest/api/3/settings/columns", None))

            if not self.apply:
                for target_label, _, target_params in targets:
                    account_note = f" accountId={target_params.get('accountId')}" if target_params and target_params.get("accountId") else ""
                    self.record(phase, "All Work columns/order", source_mode, target_label, "set defaults", "planned", " -> ".join(mapped_all_work) + account_note)
            else:
                target_results: dict[str, Any] = {}
                for target_label, path, target_params in targets:
                    try:
                        _, transport = self.c.put_column_values(path, mapped_all_work, params=target_params)
                        actual_payload = self.c.get(path, params=target_params) or []
                        target_actual = [x["value"] for x in normalize_filter_column_items(actual_payload) if x.get("value")]
                        target_results[target_label] = target_actual
                        if target_label == "My defaults":
                            actual = target_actual
                            all_work_report["actualDestinationOrder"] = actual
                        ok = target_actual == mapped_all_work
                        self.record(phase, "All Work columns/order", source_mode, target_label, "set and verify", "success" if ok else "warning",
                                    (f"transport={transport}; " + " -> ".join(target_actual)) if ok
                                    else f"transport={transport}; Requested {mapped_all_work}, Jira returned {target_actual}.")
                        if not ok:
                            self.manual_add(f"All Work {target_label} column order did not verify exactly. Requested {mapped_all_work}, Jira returned {target_actual}.")
                    except JiraError as exc:
                        target_results[target_label] = {"error": f"HTTP {exc.status}: {exc.message}"}
                        self.record(phase, "All Work columns/order", source_mode, target_label, "set defaults", "warning", target_results[target_label]["error"])
                        self.manual_add(f"Could not set All Work {target_label} columns: {target_results[target_label]['error']}")
                all_work_report["scopeResults"] = target_results
                statuses = []
                for value in target_results.values():
                    statuses.append(isinstance(value, list) and value == mapped_all_work)
                all_work_report["status"] = "verified" if statuses and all(statuses) else "mismatch-or-failed"
        else:
            all_work_report["status"] = "skipped"
            self.record(phase, "All Work columns/order", source_mode, self.all_work_column_scope, "set defaults", "skipped", "No mapped columns were available.")

        # --- Project/space List ---
        project_report: dict[str, Any] = {
            "view": "space-list",
            "sourceProject": self.source_key,
            "destinationProject": self.target_key,
            "sourceMode": project_mode,
            "sourceScope": project_scope,
            "sourceExactProjectCapture": project_exact,
            "sourceColumns": normalize_filter_column_items(project_columns),
            "mappings": project_mappings,
            "requestedDestinationOrder": mapped_project,
            "actualDestinationUserDefaults": actual if self.apply else None,
            "problems": project_problems,
            "status": "pending",
        }
        self.list_view_results.append(project_report)
        for problem in project_problems:
            self.manual_add(f"Space List columns: {problem}")

        if not mapped_project:
            project_report["status"] = "not-captured"
            self.record(phase, "space List columns/order", self.source_key, self.target_key, "verify", "warning", "No mapped project List columns were available in the source export.")
            return

        # If the source List is based on My defaults/current-fields and those map
        # to the same order we just restored, the destination List is reproducible
        # without inventing a separate saved-view write API.
        same_as_defaults = mapped_project == mapped_all_work
        list_uses_default_like_source = project_mode in {
            "jira_user_default_columns",
            "jira_project_current_fields_probe",
        } or "user default" in project_scope.lower() or "current-fields" in project_scope.lower()

        if same_as_defaults and list_uses_default_like_source:
            project_report["status"] = "verified-via-user-defaults" if self.apply and actual == mapped_all_work else "planned-via-user-defaults"
            self.record(
                phase,
                "space List columns/order",
                self.source_key,
                self.target_key,
                "use restored My defaults",
                "success" if self.apply and actual == mapped_all_work else "planned",
                " -> ".join(mapped_project),
            )
            return

        if project_exact:
            note = (
                "The exporter captured a project/saved-view-specific List order that differs from the source user's All Work defaults. "
                "Jira Cloud currently documents saved List views in the UI, but no public REST write endpoint for that saved-view model is available to this importer."
            )
        else:
            note = (
                "The source project List order is only a fallback/probe and differs from the All Work/My-default order. "
                "Applying it separately would overwrite the destination user's All Work defaults, so v3.11 does not pretend the two scopes can be restored independently."
            )
        project_report["status"] = "captured-but-not-independently-writable" if project_exact else "fallback-conflict"
        project_report["note"] = note
        self.record(phase, "space List columns/order", self.source_key, self.target_key, "restore separate saved view", "warning", note)
        self.manual_add(note)

    # ---------- filters/boards ----------
    def ensure_filters_and_boards(self) -> None:
        phase = "filters-boards"
        board_cfgs = {p.stem: data for p, data in self.b.glob_json("data/boards/configuration/*.json")}
        selected_filters = self.selected_source_filters()
        self.reconcile_filter_field_mappings(selected_filters)
        try:
            dest_filters = self.c.paginate("/rest/api/3/filter/search", params={"expand": "jql,owner,sharePermissions,editPermissions"})
        except JiraError:
            dest_filters = []
        for src in selected_filters:
            sid = str(src.get("id")); name = str(src.get("name")); jql = self.rewrite_filter_jql(str(src.get("jql", "")))
            found = first(dest_filters, lambda x: norm(x.get("name")) == norm(name) and str((x.get("owner") or {}).get("accountId", "")) == str(self.dest_me.get("accountId", "")))
            if found:
                did = str(found.get("id")); self.record(phase, "filter", name, did, "reuse", "success")
                update_body = {"name": name, "description": src.get("description", ""), "jql": jql, "favourite": bool(src.get("favourite", True))}
                update_result = self.api(phase, "filter", name, did, "update", "PUT", f"/rest/api/3/filter/{did}", update_body)
                # If Jira rejected a JQL update while newly-created fields were still
                # becoming searchable, refresh mappings and retry once directly.
                if self.apply and update_result is None:
                    self.reconcile_filter_field_mappings(selected_filters)
                    jql = self.rewrite_filter_jql(str(src.get("jql", "")))
                    try:
                        self.c.put(f"/rest/api/3/filter/{did}", {**update_body, "jql": jql})
                        self.record(phase, "filter", name, did, "retry update", "success", f"JQL: {jql}")
                    except JiraError as exc:
                        self.record(phase, "filter", name, did, "retry update", "warning", f"HTTP {exc.status}: {exc.message}")
            else:
                did, jql = self.create_filter_with_retry(phase, src, selected_filters)
            if not did:
                continue
            self.idmap["filter"][sid] = did
            share_body = [{"type": "project", "project": {"id": self.target_project_id}}] if self.target_project_id and not str(self.target_project_id).startswith("<") else []
            if share_body:
                self.api(phase, "filter sharing", name, did, "share with project", "PUT", f"/rest/api/3/filter/{did}", {"sharePermissions": share_body})
            elif not self.apply:
                self.record(phase, "filter sharing", name, self.target_key, "share with project", "planned")
            # Preserve the *semantics* of the source column configuration.
            # A real data/filters/columns/<id>.json means Jira has an explicit
            # filter-specific layout, so recreate it exactly. A missing file/404
            # means the source filter inherits My Defaults; do not fabricate an
            # explicit layout from the effective fallback, because that breaks the
            # Filter-vs-My-Defaults behavior in All Work.
            explicit_cols = self.b.read(f"data/filters/columns/{sid}.json", None)
            if isinstance(explicit_cols, list):
                self.ensure_filter_columns(phase, sid, name, did, explicit_cols)
                if self.filter_column_results:
                    self.filter_column_results[-1]["sourceColumnMode"] = "filter-specific"
                    self.filter_column_results[-1]["sourceHasFilterSpecificColumnLayout"] = True
            else:
                effective = self.b.read(f"data/filters/effective_columns/{sid}.json", {}) or {}
                effective_cols = effective.get("columns") if isinstance(effective, dict) else None
                effective_mode = str(effective.get("effectiveColumnSource") or "") if isinstance(effective, dict) else ""
                if not isinstance(effective_cols, list):
                    current_fields = self.b.read(f"data/filters/current_fields/{sid}.json", {}) or {}
                    effective_cols = current_fields.get("columns") if isinstance(current_fields, dict) else None
                    if isinstance(effective_cols, list):
                        effective_mode = "saved-filter-current-fields-probe"
                if not isinstance(effective_cols, list):
                    effective_cols = self.b.read("data/list_views/user_default_columns.json", []) or []
                    effective_mode = effective_mode or "authenticated-user-default"
                self.ensure_filter_column_inheritance(
                    phase, sid, name, did,
                    effective_cols if isinstance(effective_cols, list) else [],
                    effective_mode,
                )

        boards = self.b.read("data/boards/boards.json", []) or []
        source_project_boards: list[tuple[dict[str, Any], dict[str, Any]]] = []
        source_board_names: set[str] = set()
        for src in boards:
            sid = str(src.get("id"))
            cfg = board_cfgs.get(sid, {})
            if str((cfg.get("location") or {}).get("key", "")).upper() != self.source_key:
                continue
            source_project_boards.append((src, cfg))
            source_board_names.add(norm(src.get("name")))
        try:
            dest_boards = self.c.paginate("/rest/agile/1.0/board", params={"includePrivate": "true"})
        except JiraError:
            dest_boards = []

        # Jira creates '<target key> board' automatically with the Scrum project template.
        # If the source does not contain that board, remove only this known generated artifact.
        # This also repairs projects created by older importer versions when --reuse-project is used.
        dest_boards = self._cleanup_template_default_board(phase, dest_boards, source_board_names)

        source_statuses = {str(x.get("id")): str(x.get("name")) for x in (self.b.read("data/workflows/statuses.json", []) or [])}
        source_fields = {str(x.get("id")): str(x.get("name")) for x in (self.b.read("data/fields/fields.json", []) or [])}
        for src, cfg in source_project_boards:
            sid = str(src.get("id"))
            name = str(src.get("name"))
            found = first(dest_boards, lambda x: norm(x.get("name")) == norm(name) and self._board_project_key(x) == self.target_key)
            if found:
                did = str(found.get("id")); self.record(phase, "board", name, did, "reuse", "success")
            else:
                sfid = str((cfg.get("filter") or {}).get("id", "")); dfid = self.idmap["filter"].get(sfid)
                if not dfid:
                    self.manual_add(f"Board '{name}' could not be created because filter {sfid} has no destination mapping.")
                    continue
                body = {"name": name, "type": src.get("type", "scrum"), "filterId": int(dfid) if str(dfid).isdigit() else dfid, "location": {"type": "project", "projectKeyOrId": self.target_key}}
                result = self.api(phase, "board", name, "", "create", "POST", "/rest/agile/1.0/board", body)
                did = str((result or {}).get("id", "")) if self.apply else f"<new-board:{name}>"
            if did:
                self.idmap["board"][sid] = did
            manual_cfg = {
                "sourceBoardId": sid,
                "destinationBoardId": did,
                "name": name,
                "type": src.get("type"),
                "columns": [
                    {"name": col.get("name"), "statuses": [source_statuses.get(str(st.get("id")), str(st.get("id"))) for st in col.get("statuses", [])]}
                    for col in ((cfg.get("columnConfig") or {}).get("columns") or [])
                ],
                "estimation": cfg.get("estimation"),
                "ranking": cfg.get("ranking"),
                "subQuery": cfg.get("subQuery"),
            }
            self.board_manual_config.append(manual_cfg)
            if did:
                self.ensure_board_columns(sid, did, name, cfg)
                self.ensure_board_timeline_settings(sid, did, name)
            # Estimation/ranking remain separate from the board-column write.
            self.manual_add(f"Verify estimation/ranking settings for board '{name}' using board_configuration_manual.json. Board columns are now applied and read-back verified automatically when Jira's internal Board settings endpoint is available.")

        # Modern space List columns/order are separate from saved-filter columns and board columns.
        self.ensure_list_view_settings()

    def verify(self) -> None:
        phase = "verify"
        if not self.apply:
            self.record(phase, "import", self.source_key, self.target_key, "preview", "success",
                        "No Jira changes were made. Review the reports, then rerun with --apply.")
            return
        try:
            project = self.c.get(f"/rest/api/3/project/{self.target_key}")
            self.record(phase, "project", self.target_key, project.get("id"), "verify", "success")
        except JiraError as exc:
            self.record(phase, "project", self.target_key, "", "verify", "failed", exc.message); return

        verification: dict[str, Any] = {}
        checks = [
            ("work types", "/rest/api/3/issuetype/project", {"projectId": self.target_project_id}),
            ("components", f"/rest/api/3/project/{self.target_key}/components", None),
            ("versions", f"/rest/api/3/project/{self.target_key}/versions", None),
            ("work type scheme association", "/rest/api/3/issuetypescheme/project", {"projectId": self.target_project_id}),
            ("work type screen scheme association", "/rest/api/3/issuetypescreenscheme/project", {"projectId": self.target_project_id}),
            ("workflow scheme association", "/rest/api/3/workflowscheme/project", {"projectId": self.target_project_id}),
            ("permission scheme association", f"/rest/api/3/project/{self.target_key}/permissionscheme", {"expand": "permissions,user,group,projectRole,field,all"}),
        ]
        for label, path, params in checks:
            try:
                verification[label] = self.c.get(path, params=params)
                self.record(phase, label, self.target_key, "readable", "verify", "success")
            except JiraError as exc:
                verification[label] = {"error": exc.message}
                self.record(phase, label, self.target_key, "", "verify", "warning", exc.message)

        # Save the exact destination scheme objects/mappings that control the three
        # regressions fixed in v2.9, so a failed clone is diagnosable without guessing.
        try:
            its_id = next(iter(self.idmap["issue_type_scheme"].values()), "")
            if its_id and not its_id.startswith("<"):
                verification["work type scheme mappings"] = [
                    x for x in self.c.paginate("/rest/api/3/issuetypescheme/mapping")
                    if str(x.get("issueTypeSchemeId")) == str(its_id)
                ]
        except JiraError as exc:
            verification["work type scheme mappings"] = {"error": exc.message}
        try:
            itss_id = next(iter(self.idmap["issue_type_screen_scheme"].values()), "")
            if itss_id and not itss_id.startswith("<"):
                verification["work type screen mappings"] = [
                    x for x in self.c.paginate("/rest/api/3/issuetypescreenscheme/mapping")
                    if str(x.get("issueTypeScreenSchemeId")) == str(itss_id)
                ]
        except JiraError as exc:
            verification["work type screen mappings"] = {"error": exc.message}
        try:
            ws_id = next(iter(self.idmap["workflow_scheme"].values()), "")
            if ws_id and not ws_id.startswith("<"):
                verification["workflow scheme"] = self.c.get(f"/rest/api/3/workflowscheme/{ws_id}")
        except JiraError as exc:
            verification["workflow scheme"] = {"error": exc.message}

        (self.out / "verification.json").write_text(
            json.dumps(verification, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def write_outputs(self) -> None:
        (self.out / "id_map.json").write_text(json.dumps(self.idmap, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "board_configuration_manual.json").write_text(json.dumps(self.board_manual_config, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "board_cleanup_verification.json").write_text(json.dumps(self.board_cleanup_results, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "epic_import_results.json").write_text(json.dumps(self.epic_import_results, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "board_timeline_verification.json").write_text(json.dumps(self.board_timeline_results, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "board_column_verification.json").write_text(json.dumps(self.board_column_results, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "list_view_import_report.json").write_text(json.dumps(self.list_view_results, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "default_value_results.json").write_text(json.dumps(self.default_value_report, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "priority_replication.json").write_text(json.dumps(self.priority_report, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.out / "permission_scheme_verification.json").write_text(
            json.dumps(self.permission_scheme_report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.out / "filter_column_verification.json").write_text(
            json.dumps(self.filter_column_results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.out / "custom_field_association_verification.json").write_text(
            json.dumps(self.custom_field_association_results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.out / "legacy_field_configuration_verification.json").write_text(
            json.dumps(self.legacy_field_configuration_results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.out / "field_model_detection.json").write_text(
            json.dumps(self.field_model_detection, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.out / "manual_actions.md").write_text(
            "# Manual actions\n\n" + ("\n".join(f"- {x}" for x in self.manual) if self.manual else "- None recorded.\n"),
            encoding="utf-8",
        )
        with (self.out / "actions.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(Action.__dataclass_fields__.keys()))
            w.writeheader(); w.writerows(asdict(a) for a in self.actions)
        summary = {
            "application": {"name": APP_NAME, "version": APP_VERSION},
            "mode": "apply" if self.apply else "dry-run",
            "sourceSite": self.b.manifest.get("sourceSite"),
            "sourceExportSha256": self.source_fingerprint,
            "destinationSite": self.c.site,
            "sourceProject": self.source_key,
            "destinationProject": self.target_key,
            "fieldModel": {
                "requested": self.field_model_preference,
                "used": self.field_model_used or self.field_model_detection.get("selectedDestinationModel"),
                "fallbacks": self.field_model_detection.get("fallbacks", []),
            },
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "actions": len(self.actions),
                "success": sum(a.status == "success" for a in self.actions),
                "planned": sum(a.status == "planned" for a in self.actions),
                "warning": sum(a.status == "warning" for a in self.actions),
                "skipped": sum(a.status == "skipped" for a in self.actions),
                "failed": sum(a.status == "failed" for a in self.actions),
                "manualActions": len(self.manual),
                "filterColumnSets": len(self.filter_column_results),
                "verifiedFilterColumnSets": sum(x.get("status") == "verified" for x in self.filter_column_results),
                "epicsProcessed": len(self.epic_import_results),
                "boardColumnSetsProcessed": len(self.board_column_results),
                "boardColumnSetsVerified": sum(x.get("status") in {"verified", "already-verified"} for x in self.board_column_results),
                "timelineBoardsProcessed": len(self.board_timeline_results),
                "timelinePropertiesVerified": sum(
                    1 for board in self.board_timeline_results
                    for prop in (board.get("properties") or {}).values()
                    if isinstance(prop, dict) and prop.get("status") == "verified"
                ),
                "listViewResults": len(self.list_view_results),
                "legacyFieldConfigurationsChecked": len(self.legacy_field_configuration_results),
                "legacyFieldConfigurationItemFailures": sum(
                    len(x.get("failedItems", [])) for x in self.legacy_field_configuration_results
                ),
            },
        }
        (self.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

# ---------------- helpers ----------------

def safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract a ZIP while rejecting absolute paths and parent traversal."""
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Unsafe path in export ZIP: {member.filename}") from exc
    archive.extractall(destination)

def normalize_site(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    return value


def extract_error(r: requests.Response) -> str:
    try:
        p = r.json()
        if isinstance(p, dict):
            vals = p.get("errorMessages") or []
            errors = p.get("errors") or {}
            text = "; ".join(str(x) for x in vals)
            if errors: text = (text + "; " if text else "") + "; ".join(f"{k}: {v}" for k, v in errors.items())
            if text: return text
            for k in ("message", "error", "detail"):
                if p.get(k): return str(p[k])
    except Exception:
        pass
    return r.text.strip()[:1000] or r.reason


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def first(items: Iterable[Any], predicate) -> Any | None:
    for item in items or []:
        try:
            if predicate(item): return item
        except Exception:
            continue
    return None


def unwrap(payload: Any, keys: Sequence[str] = ("values", "permissionSchemes", "notificationSchemes")) -> list[Any]:
    if isinstance(payload, list): return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list): return payload[key]
    return []


def chunks(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size): yield list(items[i:i+size])


def best_field_match(dest: list[dict[str, Any]], src: dict[str, Any]) -> dict[str, Any] | None:
    name = norm(src.get("name")); stype = str((src.get("schema") or {}).get("custom", ""))
    exact = [x for x in dest if norm(x.get("name")) == name]
    for x in exact:
        if str((x.get("schema") or {}).get("custom", "")) == stype:
            return x
    return None

def cloneable_field(field: Mapping[str, Any]) -> bool:
    t = str((field.get("schema") or {}).get("custom", ""))
    prefix = "com.atlassian.jira.plugin.system.customfieldtypes:"
    if not t.startswith(prefix): return False
    suffix = t.split(":", 1)[1]
    allowed = {
        "datepicker", "datetime", "select", "multiselect", "multiuserpicker",
        "float", "textfield", "textarea", "url", "labels", "userpicker",
        "grouppicker", "multigrouppicker", "radiobuttons", "multicheckboxes",
        "cascadingselect", "readonlyfield",
    }
    return suffix in allowed


def collect_field_refs(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            collect_field_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_field_refs(v, out)
    elif isinstance(obj, str):
        out.update(re.findall(r"customfield_\d+", obj))

def clean_workflow(w: dict[str, Any], idmap: dict[str, dict[str, str]]) -> dict[str, Any]:
    keep = {"name", "description", "startPointLayout", "statuses", "transitions", "loopedTransitionContainerLayout"}
    out = {k: copy.deepcopy(v) for k, v in w.items() if k in keep}
    for st in out.get("statuses", []):
        st.pop("deprecated", None)
    def map_field_ids(text: str) -> str:
        return re.sub(r"customfield_\d+", lambda m: idmap["field"].get(m.group(0), m.group(0)), text)
    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            y = {}
            for k, v in x.items():
                if k == "id" and ("ruleKey" in x or "transitionScreen" in x):
                    continue
                if k in {"screenId"} and str(v) in idmap["screen"]:
                    y[k] = idmap["screen"][str(v)]
                elif k in {"field", "fieldId", "customFieldId"} and str(v) in idmap["field"]:
                    y[k] = idmap["field"][str(v)]
                else:
                    y[k] = walk(v)
            return y
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return map_field_ids(x)
        return x
    return walk(out)

def deep_replace_status_refs(workflows: list[dict[str, Any]], mapping: dict[str, str]) -> None:
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in list(x.items()):
                if k in {"statusReference", "toStatusReference", "fromStatusReference"} and str(v) in mapping:
                    x[k] = mapping[str(v)]
                else: walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(workflows)


def normalize_filter_column_items(payload: Any) -> list[dict[str, str]]:
    """Normalize Jira filter-column responses or older string-only exports."""
    if not isinstance(payload, list):
        return []
    out: list[dict[str, str]] = []
    for item in payload:
        if isinstance(item, dict):
            value = str(item.get("value") or item.get("id") or "").strip()
            label = str(item.get("label") or value)
        else:
            value = str(item or "").strip()
            label = value
        if value:
            out.append({"label": label, "value": value})
    return out


def jql_mentions_project(jql: str, key: str) -> bool:
    return re.search(rf"(?i)\bproject\s*(?:=|in\s*\()\s*[\"']?{re.escape(key)}\b", jql) is not None


def rewrite_project_jql(jql: str, source: str, target: str) -> str:
    # Replace quoted and unquoted source key tokens without touching longer words.
    return re.sub(rf"(?i)(\bproject\s*=\s*[\"']?){re.escape(source)}([\"']?)", rf"\1{target}\2", jql)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def workflow_name(item: Mapping[str, Any]) -> str:
    value = item.get("id")
    if isinstance(value, dict) and value.get("name"):
        return str(value.get("name"))
    return str(item.get("name") or "")


def normalize_status_category(value: Any) -> str:
    v = norm(value).replace(" ", "_")
    if v in {"done", "complete", "completed"}:
        return "DONE"
    if v in {"indeterminate", "in_progress", "inprogress"}:
        return "IN_PROGRESS"
    return "TODO"


def permission_fingerprint(scheme: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    rows = []
    for grant in scheme.get("permissions", []) or []:
        holder = grant.get("holder") or {}
        role_name = str((holder.get("projectRole") or {}).get("name") or "")
        parameter = role_name or str(holder.get("parameter") or "")
        rows.append((str(grant.get("permission") or ""), str(holder.get("type") or ""), parameter))
    return sorted(rows)


def numeric_or_string(value: Any) -> Any:
    text = str(value)
    return int(text) if text.isdigit() else value


def numeric_sort_key(value: Any) -> tuple[int, Any]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def field_scheme_parameters_from_legacy_item(item: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "isRequired": bool(item.get("isRequired", False)),
        "description": str(item.get("description") or ""),
    }
    renderer = str(item.get("renderer") or "")
    renderer_map = {
        "wiki-renderer": "atlassian-wiki-renderer",
        "text-renderer": "jira-text-renderer",
    }
    if renderer:
        params["rendererType"] = renderer_map.get(renderer, renderer)
    return params



def _jira_error_text(exc: JiraError) -> str:
    return norm(f"{exc.message} {exc.body}")


def requires_new_field_scheme(exc: JiraError) -> bool:
    text = _jira_error_text(exc)
    phrases = (
        "please use field scheme",
        "use field scheme instead",
        "cannot create a new field configuration",
        "field configuration model is no longer available",
        "field configurations are no longer supported",
    )
    return exc.status in {400, 404, 405, 409, 422} and any(p in text for p in phrases)


def requires_legacy_field_configuration(exc: JiraError) -> bool:
    text = _jira_error_text(exc)
    phrases = (
        "not opted in",
        "not opted-in",
        "beta program",
        "field schemes is not enabled",
        "field schemes are not enabled",
        "field scheme api is not available",
        "feature is not available",
        "experimental api is not enabled",
        "unknown endpoint",
        "resource not found",
    )
    if exc.status in {404, 405, 501}:
        return True
    return exc.status in {400, 409, 422} and any(p in text for p in phrases)


def is_field_model_unavailable_error(exc: JiraError) -> bool:
    return requires_new_field_scheme(exc) or requires_legacy_field_configuration(exc)

def infer_default_value_type(field: Mapping[str, Any]) -> str:
    custom = str((field.get("schema") or {}).get("custom") or "")
    suffix = custom.rsplit(":", 1)[-1]
    mapping = {
        "datepicker": "datepicker", "datetime": "datetimepicker",
        "select": "option.single", "radiobuttons": "option.single",
        "multiselect": "option.multiple", "multicheckboxes": "option.multiple",
        "cascadingselect": "option.cascading", "userpicker": "single.user.select",
        "multiuserpicker": "multi.user.select", "grouppicker": "grouppicker.single",
        "multigrouppicker": "grouppicker.multiple", "url": "url", "float": "float",
        "labels": "labels", "textfield": "textfield", "textarea": "textarea",
        "readonlyfield": "readonly", "version": "version.single",
        "multiversion": "version.multiple",
    }
    return mapping.get(suffix, "")


def flatten_source_defaults(grouped: Any, legacy: Any) -> tuple[list[dict[str, Any]], str]:
    if isinstance(grouped, list):
        output: list[dict[str, Any]] = []
        for context in grouped:
            if not isinstance(context, dict):
                continue
            context_id = str(context.get("contextId") or "")
            entries = context.get("defaultValues") or []
            if not isinstance(entries, list) or not entries:
                continue
            values: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("value") if isinstance(entry.get("value"), dict) else {
                    k: v for k, v in entry.items() if k not in {"issueTypeId", "isAnyIssueType"}
                }
                value = copy.deepcopy(raw)
                value["contextId"] = context_id
                values.append(value)
            if not values:
                continue
            fingerprints = {json.dumps(v, sort_keys=True, default=str) for v in values}
            if len(fingerprints) == 1:
                output.append(values[0])
                continue
            # Prefer a catch-all only when every issue-type-specific value is the
            # same. Different per-work-type defaults cannot be safely represented
            # by the current public write endpoint and are left for manual action.
            catch_all = None
            for entry, value in zip(entries, values):
                if isinstance(entry, dict) and entry.get("isAnyIssueType") is True:
                    catch_all = value
                    break
            if catch_all is not None:
                specific = [v for e, v in zip(entries, values) if not (isinstance(e, dict) and e.get("isAnyIssueType") is True)]
                if all(json.dumps(v, sort_keys=True, default=str) == json.dumps(catch_all, sort_keys=True, default=str) for v in specific):
                    output.append(catch_all)
                    continue
            # Preserve a sentinel that will be reported rather than silently choosing.
            output.append({"contextId": context_id, "_unsupportedPerIssueTypeDefaults": values})
        return output, "grouped"
    if isinstance(legacy, list):
        return [copy.deepcopy(x) for x in legacy if isinstance(x, dict)], "legacy"
    return [], "none"


def page_values(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("values", "priorities", "projects"):
            child = value.get(key)
            if isinstance(child, list):
                return [x for x in child if isinstance(x, dict)]
            if isinstance(child, dict) and isinstance(child.get("values"), list):
                return [x for x in child.get("values", []) if isinstance(x, dict)]
    return []


def priority_icon_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    return parsed.path if parsed.scheme and parsed.netloc else text


def priority_signature(priority: Mapping[str, Any]) -> tuple[str, str] | None:
    path = priority_icon_path(priority.get("iconUrl"))
    basename = Path(path).name.casefold() if path else ""
    if basename:
        stem, suffix = os.path.splitext(basename)
        stem = re.sub(r"_new$", "", stem)
        basename = stem + suffix
    color = str(priority.get("statusColor") or "").strip().casefold()
    if not basename and not color:
        return None
    return basename, color


def priority_write_body(priority: Mapping[str, Any]) -> dict[str, Any]:
    body = {
        "name": str(priority.get("name") or ""),
        "description": str(priority.get("description") or ""),
        "statusColor": str(priority.get("statusColor") or "#707070"),
        "iconUrl": priority_icon_path(priority.get("iconUrl")) or "/images/icons/priorities/medium_new.svg",
    }
    return body


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=APP_NAME)
    default_export = Path(__file__).resolve().with_name("source_export.zip")
    p.add_argument("export_zip", nargs="?", type=Path, default=default_export, help="Compatible source export ZIP; defaults to the embedded TES export")
    p.add_argument("--site", help="Destination Jira URL; otherwise prompted or read from JIRA_SITE_URL")
    p.add_argument("--email", help="Destination Jira email; otherwise prompted or read from JIRA_EMAIL")
    p.add_argument("--token", help="Destination API token; otherwise securely prompted or read from JIRA_API_TOKEN")
    p.add_argument("--source-project", default="TES", help="Source project key in export; default: TES")
    p.add_argument("--target-project", help="Destination project key; default: source key")
    p.add_argument("--target-name", help="Destination project name override")
    p.add_argument("--output", type=Path, default=Path.cwd(), help="Report output directory")
    p.add_argument("--apply", action="store_true", help="Actually write to Jira. Without this flag, only a dry-run plan is produced.")
    p.add_argument("--reuse-project", action="store_true", help="Reuse an existing destination project with the target key")
    p.add_argument(
        "--allow-missing-default-values", action="store_true",
        help="Allow an older source export that did not capture field defaults. Exact cloning should not use this flag.",
    )
    p.add_argument(
        "--field-model", choices=("auto", "new", "legacy"), default="auto",
        help="Destination field-management model. Default auto probes both and falls back safely.",
    )
    p.add_argument(
        "--priority-mode", choices=("replicate", "create-only", "skip"), default="replicate",
        help="Priority handling. replicate updates renamed built-ins or creates missing priorities; create-only never renames; skip omits priorities.",
    )
    p.add_argument(
        "--sync-global-priority-default", action="store_true",
        help="Also set Jira's site-wide default priority. The target scheme default is always replicated without this flag.",
    )
    p.add_argument(
        "--all-work-visible-columns",
        help=("Override the exported All work items column layout using comma-separated Jira field IDs. "
              "Use 'work' for Jira's fixed Work composite column, for example: work,status,parent. "
              "This is useful when the modern All work items UI differs from /user/columns and CSV current-fields."),
    )
    p.add_argument(
        "--all-work-column-scope", choices=("user", "system", "both"), default="both",
        help=("Where an --all-work-visible-columns override is written. user updates My defaults; "
              "system updates Jira's site-wide System columns; both updates both. Default: both. "
              "Using both makes the visible All work items layout match even when Jira is currently showing System columns."),
    )
    return p.parse_args()

def main() -> int:
    args = parse_args()
    if not args.export_zip.exists():
        print(f"Export ZIP not found: {args.export_zip}", file=sys.stderr); return 2
    bundle = ExportBundle(args.export_zip)
    try:
        selected = bundle.manifest.get("selectedProjectKeys") or []
        source = (args.source_project or (selected[0] if selected else "")).upper()
        if not source:
            print("No project key found. Use --source-project.", file=sys.stderr); return 2
        target = (args.target_project or source).upper()
        site = args.site or os.getenv("JIRA_SITE_URL") or input("Destination Jira URL: ").strip()
        email = args.email or os.getenv("JIRA_EMAIL") or input("Destination Jira email: ").strip()
        token = args.token or os.getenv("JIRA_API_TOKEN") or getpass.getpass("Destination Jira API token: ")
        if not site or not email or not token:
            print("Destination URL, email and token are required.", file=sys.stderr); return 2
        print(f"\n{APP_NAME} v{APP_VERSION}")
        print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
        print(f"Source project: {source}")
        print(f"Destination: {normalize_site(site)} / {target}\n")
        if args.apply:
            confirm = input(f"Type APPLY {target} to authorize creating/updating Jira configuration: ").strip()
            if confirm != f"APPLY {target}":
                print("Authorization text did not match. No changes made."); return 1
        importer = Importer(bundle, JiraClient(site, email, token), apply=args.apply,
                            source_key=source, target_key=target, target_name=args.target_name,
                            output_dir=args.output, reuse_project=args.reuse_project,
                            field_model_preference=args.field_model,
                            allow_missing_default_values=args.allow_missing_default_values,
                            priority_mode=args.priority_mode,
                            sync_global_priority_default=args.sync_global_priority_default,
                            all_work_visible_columns=args.all_work_visible_columns,
                            all_work_column_scope=args.all_work_column_scope)
        out = importer.run()
        print(f"\nCompleted. Reports: {out}")
        if not args.apply:
            print("This was a dry run. Review actions.csv and manual_actions.md, then rerun with --apply.")
        return 0
    finally:
        bundle.close()


if __name__ == "__main__":
    raise SystemExit(main())
