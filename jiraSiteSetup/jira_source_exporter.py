#!/usr/bin/env python3
"""
Jira Cloud source-site configuration exporter.

This program is intentionally READ-ONLY. It uses Jira Cloud REST API GET requests
and read-only POST endpoints used for bulk workflow reads and JQL issue searches. It exports the
configuration, Epic work items, and List-view column state visible to the authenticated account into JSON/CSV/binary files and a ZIP.
Issue-type avatar image assets are included so renamed/customized work types can be reproduced visually.

Python: 3.10+
Dependency: requests
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
import re
import shutil
import sqlite3
import base64
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth


APP_NAME = "Jira Source-Site Exporter"
APP_VERSION = "1.9.0"
DEFAULT_PAGE_SIZE = 100
MAX_RETRIES = 6
USER_AGENT = f"jira-source-site-exporter/{APP_VERSION}"


class JiraApiError(RuntimeError):
    """Raised when Jira returns a non-successful response."""

    def __init__(
        self,
        method: str,
        url: str,
        status_code: int,
        message: str,
        response_text: str = "",
    ) -> None:
        super().__init__(f"{method} {url} -> HTTP {status_code}: {message}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.message = message
        self.response_text = response_text


@dataclass
class OperationRecord:
    name: str
    status: str
    endpoint: str
    item_count: int | None = None
    http_status: int | None = None
    output_file: str | None = None
    note: str | None = None
    elapsed_seconds: float | None = None


@dataclass
class ExportSettings:
    site_url: str
    email: str
    project_keys: list[str]
    include_users: bool
    include_group_members: bool
    include_project_properties: bool
    include_epics: bool
    include_browser_view_state: bool
    firefox_profile: Path | None
    output_parent: Path


class JiraClient:
    """Small Jira REST client with retry and pagination support."""

    def __init__(self, site_url: str, email: str, api_token: str) -> None:
        self.site_url = normalize_site_url(site_url)
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(email, api_token)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        expected: Sequence[int] = (200,),
        timeout: int = 60,
    ) -> Any:
        url = path if path.startswith("http") else urljoin(self.site_url + "/", path.lstrip("/"))
        last_error: JiraApiError | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise JiraApiError(method, url, 0, str(exc)) from exc
                time.sleep(min(2**attempt, 30))
                continue

            if response.status_code in expected:
                if response.status_code == 204 or not response.content:
                    return None
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type.lower():
                    return response.json()
                text = response.text.strip()
                if not text:
                    return None
                try:
                    return response.json()
                except ValueError:
                    return text

            message = extract_error_message(response)
            last_error = JiraApiError(
                method,
                response.url,
                response.status_code,
                message,
                response.text[:4000],
            )

            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if not retryable or attempt >= MAX_RETRIES:
                raise last_error

            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else min(2**attempt, 30)
            time.sleep(max(delay, 1))

        if last_error:
            raise last_error
        raise RuntimeError("Unexpected request failure")

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def get_bytes(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/octet-stream",
        timeout: int = 60,
    ) -> tuple[bytes, str]:
        """GET a binary Jira resource while retaining the same retry semantics."""
        url = path if path.startswith("http") else urljoin(self.site_url + "/", path.lstrip("/"))
        last_error: JiraApiError | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.session.get(
                    url, params=params, headers={"Accept": accept}, timeout=timeout
                )
            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    raise JiraApiError("GET", url, 0, str(exc)) from exc
                time.sleep(min(2**attempt, 30))
                continue

            if 200 <= response.status_code < 300:
                return response.content, response.headers.get("Content-Type", "application/octet-stream")

            message = extract_error_message(response)
            last_error = JiraApiError(
                "GET", response.url, response.status_code, message, response.text[:4000]
            )
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if not retryable or attempt >= MAX_RETRIES:
                raise last_error
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else min(2**attempt, 30)
            time.sleep(max(delay, 1))

        if last_error:
            raise last_error
        raise RuntimeError("Unexpected binary request failure")

    def post_read(self, path: str, *, json_body: Any) -> Any:
        """POST used only for Jira bulk-read endpoints; never writes configuration."""
        return self.request("POST", path, json_body=json_body)

    def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        item_key: str = "values",
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 10000,
    ) -> list[Any]:
        all_items: list[Any] = []
        start_at = 0
        base_params = dict(params or {})

        for _ in range(max_pages):
            page_params = dict(base_params)
            page_params["startAt"] = start_at
            page_params["maxResults"] = page_size
            payload = self.get(path, params=page_params)

            if isinstance(payload, list):
                all_items.extend(payload)
                break
            if not isinstance(payload, dict):
                break

            items = payload.get(item_key)
            if items is None:
                # A few Jira endpoints use other conventional container keys.
                for candidate in (
                    "projects",
                    "permissionSchemes",
                    "issueSecuritySchemes",
                    "notificationSchemes",
                    "boards",
                    "dashboards",
                ):
                    if isinstance(payload.get(candidate), list):
                        items = payload[candidate]
                        break
            if items is None:
                # Preserve an unusual response as a single item rather than losing it.
                all_items.append(payload)
                break
            if not isinstance(items, list):
                all_items.append(items)
                break

            all_items.extend(items)
            received = len(items)
            total = payload.get("total")
            is_last = payload.get("isLast")

            if is_last is True or received == 0:
                break
            if isinstance(total, int) and start_at + received >= total:
                break
            if received < page_size and total is None:
                break

            next_start = payload.get("startAt", start_at) + payload.get("maxResults", received or page_size)
            if next_start <= start_at:
                next_start = start_at + received
            if next_start <= start_at:
                break
            start_at = next_start

        return all_items


    def search_issues_jql(
        self,
        jql: str,
        *,
        fields: Sequence[str] = ("*all",),
        expand: str = "names,schema",
        page_size: int = 50,
        max_pages: int = 10000,
    ) -> dict[str, Any]:
        """Read all issues matching JQL, preferring Jira's enhanced-search endpoint.

        The enhanced endpoint uses nextPageToken rather than startAt. A legacy POST
        fallback is retained for Jira Cloud tenants where enhanced search is not yet
        available to the authenticated account.
        """
        issues: list[Any] = []
        names: dict[str, Any] = {}
        schema: dict[str, Any] = {}
        page_count = 0
        next_page_token: str | None = None

        try:
            for _ in range(max_pages):
                body: dict[str, Any] = {
                    "jql": jql,
                    "fields": list(fields),
                    "maxResults": page_size,
                }
                if expand:
                    body["expand"] = expand
                if next_page_token:
                    body["nextPageToken"] = next_page_token

                payload = self.post_read("/rest/api/3/search/jql", json_body=body)
                page_count += 1
                if not isinstance(payload, dict):
                    break

                page_issues = payload.get("issues") or []
                if isinstance(page_issues, list):
                    issues.extend(page_issues)

                if not names and isinstance(payload.get("names"), dict):
                    names = payload.get("names") or {}
                if not schema and isinstance(payload.get("schema"), dict):
                    schema = payload.get("schema") or {}

                next_page_token = payload.get("nextPageToken")
                if payload.get("isLast") is True or not next_page_token:
                    break

            return {
                "searchApi": "POST /rest/api/3/search/jql",
                "jql": jql,
                "fieldsRequested": list(fields),
                "expandRequested": expand,
                "pageCount": page_count,
                "totalCaptured": len(issues),
                "names": names,
                "schema": schema,
                "issues": issues,
            }
        except JiraApiError as exc:
            if exc.status_code not in (404, 405):
                raise

        # Compatibility fallback for tenants that do not expose enhanced search.
        issues = []
        names = {}
        schema = {}
        page_count = 0
        start_at = 0
        legacy_expand = [item.strip() for item in expand.split(",") if item.strip()]

        for _ in range(max_pages):
            body = {
                "jql": jql,
                "fields": list(fields),
                "maxResults": page_size,
                "startAt": start_at,
            }
            if legacy_expand:
                body["expand"] = legacy_expand
            payload = self.post_read("/rest/api/3/search", json_body=body)
            page_count += 1
            if not isinstance(payload, dict):
                break

            page_issues = payload.get("issues") or []
            if not isinstance(page_issues, list):
                page_issues = []
            issues.extend(page_issues)

            if not names and isinstance(payload.get("names"), dict):
                names = payload.get("names") or {}
            if not schema and isinstance(payload.get("schema"), dict):
                schema = payload.get("schema") or {}

            received = len(page_issues)
            total = payload.get("total")
            if received == 0:
                break
            if isinstance(total, int) and start_at + received >= total:
                break
            if received < page_size and total is None:
                break
            start_at += received

        return {
            "searchApi": "POST /rest/api/3/search",
            "jql": jql,
            "fieldsRequested": list(fields),
            "expandRequested": expand,
            "pageCount": page_count,
            "totalCaptured": len(issues),
            "names": names,
            "schema": schema,
            "issues": issues,
        }


class JiraExporter:
    def __init__(self, client: JiraClient, settings: ExportSettings) -> None:
        self.client = client
        self.settings = settings
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        site_slug = safe_filename(self.client.site_url.replace("https://", "").replace("http://", ""))
        self.root = settings.output_parent / f"jira_source_export_{site_slug}_{timestamp}"
        self.data_dir = self.root / "data"
        self.project_dir = self.data_dir / "projects"
        self.report_dir = self.root / "reports"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

        self.operations: list[OperationRecord] = []
        self.manual_actions: list[str] = []
        self.cache: dict[str, Any] = {}
        self.selected_projects: list[dict[str, Any]] = []

    def run(self) -> Path:
        print(f"\n{APP_NAME} v{APP_VERSION}")
        print(f"Source: {self.client.site_url}")
        print("Mode: READ-ONLY\n")

        self.export_identity_and_site()
        self.export_projects()
        self.export_global_reference_data()
        self.export_work_types_and_schemes()
        self.export_epics()
        self.export_fields_and_field_schemes()
        self.export_screens()
        self.export_statuses_workflows_and_schemes()
        self.export_permissions_and_notifications()
        self.export_filters_and_boards()
        self.export_list_view_server_columns()
        self.export_current_fields_column_views()
        self.finalize_effective_filter_columns()
        self.export_local_browser_view_state()
        self.export_users_groups_and_roles()
        self.export_project_specific_configuration()
        self.finalize_list_view_capture()
        self.build_reports()
        self.write_manifest()
        self.write_manual_actions()
        self.write_operation_report()
        zip_path = self.create_zip()

        print("\nExport complete.")
        print(f"Folder: {self.root}")
        print(f"ZIP:    {zip_path}")
        return zip_path

    # ------------------------- generic helpers -------------------------

    def save_json(self, relative_path: str | Path, payload: Any) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False, default=str)
            handle.write("\n")
        return path

    def perform(
        self,
        name: str,
        endpoint: str,
        output_file: str | Path,
        function: Callable[[], Any],
        *,
        optional: bool = False,
        note: str | None = None,
    ) -> Any | None:
        start = time.monotonic()
        print(f"[{len(self.operations) + 1:03d}] {name} ...", end=" ", flush=True)
        try:
            payload = function()
            path = self.save_json(output_file, payload)
            count = item_count(payload)
            elapsed = round(time.monotonic() - start, 3)
            self.operations.append(
                OperationRecord(
                    name=name,
                    status="success",
                    endpoint=endpoint,
                    item_count=count,
                    output_file=str(path.relative_to(self.root)),
                    note=note,
                    elapsed_seconds=elapsed,
                )
            )
            print(f"OK ({count if count is not None else 'saved'})")
            return payload
        except JiraApiError as exc:
            elapsed = round(time.monotonic() - start, 3)
            status = "unsupported_or_inaccessible" if optional else "failed"
            error_payload = {
                "operation": name,
                "endpoint": endpoint,
                "http_status": exc.status_code,
                "message": exc.message,
                "response_excerpt": exc.response_text,
                "optional": optional,
            }
            error_path = Path("data/errors") / f"{safe_filename(name)}.json"
            saved_error = self.save_json(error_path, error_payload)
            self.operations.append(
                OperationRecord(
                    name=name,
                    status=status,
                    endpoint=endpoint,
                    http_status=exc.status_code,
                    output_file=str(saved_error.relative_to(self.root)),
                    note=note or exc.message,
                    elapsed_seconds=elapsed,
                )
            )
            print(f"{status.upper()} (HTTP {exc.status_code})")
            return None
        except Exception as exc:  # noqa: BLE001 - exporter must continue
            elapsed = round(time.monotonic() - start, 3)
            error_payload = {
                "operation": name,
                "endpoint": endpoint,
                "message": f"{type(exc).__name__}: {exc}",
                "optional": optional,
            }
            error_path = Path("data/errors") / f"{safe_filename(name)}.json"
            saved_error = self.save_json(error_path, error_payload)
            self.operations.append(
                OperationRecord(
                    name=name,
                    status="failed",
                    endpoint=endpoint,
                    output_file=str(saved_error.relative_to(self.root)),
                    note=str(exc),
                    elapsed_seconds=elapsed,
                )
            )
            print(f"FAILED ({type(exc).__name__})")
            return None

    def perform_binary(
        self,
        name: str,
        endpoint: str,
        output_file: str | Path,
        function: Callable[[], tuple[bytes, str]],
        *,
        optional: bool = False,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        """Run a read-only binary export and record it in the operation report."""
        start = time.monotonic()
        print(f"[{len(self.operations) + 1:03d}] {name} ...", end=" ", flush=True)
        try:
            data, content_type = function()
            path = self.root / output_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            elapsed = round(time.monotonic() - start, 3)
            details = {
                "file": str(path.relative_to(self.root)),
                "contentType": content_type,
                "bytes": len(data),
            }
            self.operations.append(
                OperationRecord(
                    name=name,
                    status="success",
                    endpoint=endpoint,
                    output_file=details["file"],
                    note=(note + "; " if note else "") + f"{len(data)} bytes; {content_type}",
                    elapsed_seconds=elapsed,
                )
            )
            print(f"OK ({len(data)} bytes)")
            return details
        except JiraApiError as exc:
            elapsed = round(time.monotonic() - start, 3)
            status = "unsupported_or_inaccessible" if optional else "failed"
            error_payload = {
                "operation": name,
                "endpoint": endpoint,
                "http_status": exc.status_code,
                "message": exc.message,
                "response_excerpt": exc.response_text,
                "optional": optional,
            }
            saved_error = self.save_json(
                Path("data/errors") / f"{safe_filename(name)}.json", error_payload
            )
            self.operations.append(
                OperationRecord(
                    name=name, status=status, endpoint=endpoint,
                    http_status=exc.status_code,
                    output_file=str(saved_error.relative_to(self.root)),
                    note=note or exc.message, elapsed_seconds=elapsed,
                )
            )
            print(f"{status.upper()} (HTTP {exc.status_code})")
            return None
        except Exception as exc:  # noqa: BLE001
            elapsed = round(time.monotonic() - start, 3)
            saved_error = self.save_json(
                Path("data/errors") / f"{safe_filename(name)}.json",
                {"operation": name, "endpoint": endpoint, "message": f"{type(exc).__name__}: {exc}"},
            )
            self.operations.append(
                OperationRecord(
                    name=name, status="failed", endpoint=endpoint,
                    output_file=str(saved_error.relative_to(self.root)),
                    note=str(exc), elapsed_seconds=elapsed,
                )
            )
            print(f"FAILED ({type(exc).__name__})")
            return None

    def get_with_fallback(self, paths: Sequence[str], params: Mapping[str, Any] | None = None) -> Any:
        errors: list[JiraApiError] = []
        for path in paths:
            try:
                return self.client.get(path, params=params)
            except JiraApiError as exc:
                errors.append(exc)
                if exc.status_code not in (400, 404, 405):
                    raise
        if errors:
            raise errors[-1]
        raise RuntimeError("No fallback endpoints supplied")

    def paginate_with_fallback(
        self,
        paths: Sequence[str],
        *,
        params: Mapping[str, Any] | None = None,
        item_key: str = "values",
    ) -> list[Any]:
        errors: list[JiraApiError] = []
        for path in paths:
            try:
                return self.client.paginate(path, params=params, item_key=item_key)
            except JiraApiError as exc:
                errors.append(exc)
                if exc.status_code not in (400, 404, 405):
                    raise
        if errors:
            raise errors[-1]
        raise RuntimeError("No fallback endpoints supplied")

    # ------------------------- top-level exports -------------------------

    def export_identity_and_site(self) -> None:
        server_info = self.perform(
            "Server information",
            "GET /rest/api/3/serverInfo",
            "data/site/server_info.json",
            lambda: self.client.get("/rest/api/3/serverInfo"),
        )
        myself = self.perform(
            "Authenticated account",
            "GET /rest/api/3/myself",
            "data/site/authenticated_account.json",
            lambda: self.client.get("/rest/api/3/myself"),
        )
        global_permissions = self.perform(
            "My permissions",
            "GET /rest/api/3/mypermissions",
            "data/site/my_permissions.json",
            lambda: self.client.get(
                "/rest/api/3/mypermissions",
                params={
                    "permissions": (
                        "ADMINISTER,ADMINISTER_PROJECTS,BROWSE_PROJECTS,"
                        "CREATE_SHARED_OBJECTS,MANAGE_GROUP_FILTER_SUBSCRIPTIONS"
                    )
                },
            ),
            optional=True,
        )
        self.cache.update(
            {
                "server_info": server_info,
                "myself": myself,
                "my_permissions": global_permissions,
            }
        )

    def export_projects(self) -> None:
        projects = self.perform(
            "Projects/spaces",
            "GET /rest/api/3/project/search",
            "data/projects.json",
            lambda: self.client.paginate(
                "/rest/api/3/project/search",
                params={
                    "expand": "description,lead,issueTypes,url,projectKeys,permissions,insight",
                    "orderBy": "key",
                },
            ),
        )
        projects = projects if isinstance(projects, list) else []

        requested = {key.upper() for key in self.settings.project_keys}
        if requested:
            self.selected_projects = [p for p in projects if str(p.get("key", "")).upper() in requested]
            found = {str(p.get("key", "")).upper() for p in self.selected_projects}
            missing = sorted(requested - found)
            if missing:
                self.manual_actions.append(
                    "Requested project keys not visible to the exporting account: " + ", ".join(missing)
                )
        else:
            self.selected_projects = projects

        self.save_json("data/selected_projects.json", self.selected_projects)
        self.cache["projects"] = projects
        self.cache["selected_projects"] = self.selected_projects

    def export_global_reference_data(self) -> None:
        jobs = [
            (
                "Application roles",
                "GET /rest/api/3/applicationrole",
                "data/reference/application_roles.json",
                lambda: self.client.get("/rest/api/3/applicationrole"),
                True,
            ),
            (
                "Jira permission definitions",
                "GET /rest/api/3/permissions",
                "data/reference/permission_definitions.json",
                lambda: self.client.get("/rest/api/3/permissions"),
                True,
            ),
            (
                "Project categories",
                "GET /rest/api/3/projectCategory",
                "data/reference/project_categories.json",
                lambda: self.client.get("/rest/api/3/projectCategory"),
                True,
            ),
            (
                "Issue priorities",
                "GET /rest/api/3/priority/search",
                "data/reference/priorities.json",
                lambda: self.client.paginate("/rest/api/3/priority/search"),
                True,
            ),
            (
                "Priority schemes",
                "GET /rest/api/3/priorityscheme",
                "data/reference/priority_schemes.json",
                lambda: self.client.paginate("/rest/api/3/priorityscheme", params={"expand": "priorities,projects"}),
                True,
            ),
            (
                "Issue resolutions",
                "GET /rest/api/3/resolution",
                "data/reference/resolutions.json",
                lambda: self.client.get("/rest/api/3/resolution"),
                True,
            ),
            (
                "Issue link types",
                "GET /rest/api/3/issueLinkType",
                "data/reference/issue_link_types.json",
                lambda: self.client.get("/rest/api/3/issueLinkType"),
                True,
            ),
            (
                "Time tracking configuration",
                "GET /rest/api/3/configuration/timetracking",
                "data/reference/time_tracking.json",
                lambda: self.client.get("/rest/api/3/configuration/timetracking"),
                True,
            ),
            (
                "General Jira configuration",
                "GET /rest/api/3/configuration",
                "data/reference/general_configuration.json",
                lambda: self.client.get("/rest/api/3/configuration"),
                True,
            ),
        ]
        for name, endpoint, output, func, optional in jobs:
            result = self.perform(name, endpoint, output, func, optional=optional)
            if output == "data/reference/priorities.json":
                self.cache["priorities"] = result if isinstance(result, list) else unwrap_list(result)
            elif output == "data/reference/priority_schemes.json":
                self.cache["priority_schemes"] = result if isinstance(result, list) else unwrap_list(result)

        # Capture each priority scheme's complete ordered priority list and project
        # associations. The top-level scheme search can truncate nested pages, so
        # these per-scheme reads are the authoritative source for cloning.
        priority_scheme_details: dict[str, Any] = {}
        for scheme in self.cache.get("priority_schemes", []) or []:
            if not isinstance(scheme, dict):
                continue
            scheme_id = str(scheme.get("id", ""))
            if not scheme_id:
                continue
            priorities = self.perform(
                f"Priority scheme priorities {scheme_id}",
                f"GET /rest/api/3/priorityscheme/{scheme_id}/priorities",
                f"data/reference/priority_scheme_priorities/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.paginate(
                    f"/rest/api/3/priorityscheme/{sid}/priorities"
                ),
                optional=True,
            )
            projects = self.perform(
                f"Priority scheme projects {scheme_id}",
                f"GET /rest/api/3/priorityscheme/{scheme_id}/projects",
                f"data/reference/priority_scheme_projects/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.paginate(
                    f"/rest/api/3/priorityscheme/{sid}/projects"
                ),
                optional=True,
            )
            detail = dict(scheme)
            detail["priorities"] = priorities if isinstance(priorities, list) else unwrap_list(priorities)
            detail["projects"] = projects if isinstance(projects, list) else unwrap_list(projects)
            self.save_json(
                f"data/reference/priority_scheme_details/{safe_filename(scheme_id)}.json",
                detail,
            )
            priority_scheme_details[scheme_id] = detail
        self.cache["priority_scheme_details"] = priority_scheme_details

    def export_work_types_and_schemes(self) -> None:
        issue_types = self.perform(
            "Work types (issue types)",
            "GET /rest/api/3/issuetype",
            "data/work_types/issue_types.json",
            lambda: self.client.get("/rest/api/3/issuetype"),
        )
        schemes = self.perform(
            "Work type schemes",
            "GET /rest/api/3/issuetypescheme",
            "data/work_types/issue_type_schemes.json",
            lambda: self.client.paginate("/rest/api/3/issuetypescheme"),
        )
        mappings = self.perform(
            "Work type scheme mappings",
            "GET /rest/api/3/issuetypescheme/mapping",
            "data/work_types/issue_type_scheme_mappings.json",
            lambda: self.client.paginate("/rest/api/3/issuetypescheme/mapping"),
        )
        self.cache.update(
            {
                "issue_types": issue_types,
                "issue_type_schemes": schemes,
                "issue_type_scheme_mappings": mappings,
            }
        )

        # Jira's issue-type JSON contains only a site-local avatarId/iconUrl.  A
        # destination site cannot safely reuse that ID, so export the selected icon
        # pixels for every work type used by the selected projects.  The importer
        # uploads these bytes and selects the newly-created destination avatar.
        selected_issue_type_ids: set[str] = set()
        for project in self.selected_projects:
            for issue_type in project.get("issueTypes", []) or []:
                if issue_type.get("id") is not None:
                    selected_issue_type_ids.add(str(issue_type.get("id")))

        avatar_manifest: list[dict[str, Any]] = []
        for issue_type in issue_types if isinstance(issue_types, list) else []:
            source_issue_type_id = str(issue_type.get("id", ""))
            avatar_id = issue_type.get("avatarId")
            if (
                not source_issue_type_id
                or source_issue_type_id not in selected_issue_type_ids
                or avatar_id in (None, "")
            ):
                continue
            endpoint = f"/rest/api/3/universal_avatar/view/type/issuetype/avatar/{avatar_id}"
            rel = f"data/work_types/avatars/{safe_filename(source_issue_type_id)}.png"
            details = self.perform_binary(
                f"Work type avatar {issue_type.get('name')} ({source_issue_type_id})",
                f"GET {endpoint}",
                rel,
                lambda ep=endpoint: self.client.get_bytes(
                    ep, params={"size": "xlarge", "format": "png"}, accept="image/png"
                ),
                optional=True,
                note=f"source avatarId={avatar_id}",
            )
            if details:
                avatar_manifest.append(
                    {
                        "sourceIssueTypeId": source_issue_type_id,
                        "name": issue_type.get("name"),
                        "sourceAvatarId": avatar_id,
                        **details,
                    }
                )
        self.save_json("data/work_types/avatar_manifest.json", avatar_manifest)
        self.cache["issue_type_avatars"] = avatar_manifest

    def export_epics(self) -> None:
        """Export every visible Epic-level work item on the Jira site.

        Jira Cloud models Epic as hierarchy level 1. Detecting the hierarchy level
        instead of relying only on the literal name "Epic" also preserves renamed
        Epic work types and project-scoped Epic types. The export is intentionally
        site-wide even when --project is supplied; a selected-project subset is
        written separately for importers that only clone the requested projects.
        """
        if not self.settings.include_epics:
            self.operations.append(
                OperationRecord(
                    name="Epic work items",
                    status="skipped",
                    endpoint="POST /rest/api/3/search/jql",
                    item_count=0,
                    note="Skipped by --skip-epics or JIRA_INCLUDE_EPICS=false.",
                )
            )
            return

        issue_types = self.cache.get("issue_types") or []
        if not isinstance(issue_types, list):
            issue_types = unwrap_list(issue_types)

        # Include project-scoped issue types from project/search as well. This is
        # important for team-managed projects, where Jira can expose scoped Epic
        # work types in the project metadata even when the global list is less useful.
        all_issue_types: list[dict[str, Any]] = []
        seen_issue_type_ids: set[str] = set()
        for candidate in list(issue_types) + [
            issue_type
            for project in (self.cache.get("projects") or [])
            if isinstance(project, dict)
            for issue_type in (project.get("issueTypes") or [])
        ]:
            if not isinstance(candidate, dict):
                continue
            candidate_id = str(candidate.get("id", "")).strip()
            dedupe_key = candidate_id or json_compact(candidate)
            if dedupe_key in seen_issue_type_ids:
                continue
            seen_issue_type_ids.add(dedupe_key)
            all_issue_types.append(candidate)

        epic_types: list[dict[str, Any]] = []
        for issue_type in all_issue_types:
            if not isinstance(issue_type, dict):
                continue
            level = issue_type.get("hierarchyLevel")
            try:
                is_epic_level = int(level) == 1
            except (TypeError, ValueError):
                is_epic_level = False
            if is_epic_level:
                epic_types.append(issue_type)

        # Compatibility fallback for older responses that omit hierarchyLevel.
        if not epic_types:
            epic_types = [
                issue_type
                for issue_type in all_issue_types
                if isinstance(issue_type, dict)
                and str(issue_type.get("name", "")).strip().casefold() == "epic"
            ]

        self.save_json("data/issues/epics/epic_issue_types.json", epic_types)

        epic_type_ids = [
            str(issue_type.get("id", "")).strip()
            for issue_type in epic_types
            if str(issue_type.get("id", "")).strip()
        ]
        if epic_type_ids:
            type_clause = ", ".join(jql_value(value) for value in epic_type_ids)
            jql = f"issuetype in ({type_clause}) ORDER BY created ASC, key ASC"
        else:
            # This can still work if the authenticated account can search Epics but
            # Jira did not expose issue-type metadata to it.
            jql = 'issuetype = "Epic" ORDER BY created ASC, key ASC'
            self.manual_actions.append(
                "Epic issue-type hierarchy metadata was unavailable, so the Epic export fell back to JQL issuetype = Epic. If the Epic work type has been renamed and no hierarchyLevel=1 type was returned, verify data/issues/epics/sitewide.json."
            )

        payload = self.perform(
            "Epic work items (site-wide)",
            "POST /rest/api/3/search/jql",
            "data/issues/epics/sitewide.json",
            lambda query=jql: self.client.search_issues_jql(
                query,
                fields=("*all",),
                expand="names,schema",
                page_size=50,
            ),
            optional=True,
            note=(
                "Read-only JQL export of all visible hierarchy-level-1 Epic work items across the site; "
                "includes all fields the authenticated account can view."
            ),
        )

        issues = payload.get("issues", []) if isinstance(payload, dict) else []
        if not isinstance(issues, list):
            issues = []
        self.cache["epics"] = issues
        self.cache["epic_issue_types"] = epic_types

        # Store one file per Epic so import/debug tooling can address work items by key.
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            issue_key = str(issue.get("key", "")).strip()
            issue_id = str(issue.get("id", "")).strip()
            file_stem = safe_filename(issue_key or issue_id or "unknown_epic")
            self.save_json(f"data/issues/epics/by_key/{file_stem}.json", issue)

        # Also group the captured Epics by their source project/space.
        by_project: dict[str, list[dict[str, Any]]] = {}
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            fields = issue.get("fields") or {}
            project = fields.get("project") or {} if isinstance(fields, dict) else {}
            project_key = str(project.get("key", "")).strip() if isinstance(project, dict) else ""
            project_key = project_key or "UNKNOWN_PROJECT"
            by_project.setdefault(project_key, []).append(issue)

        for project_key, project_epics in sorted(by_project.items()):
            self.save_json(
                f"data/issues/epics/by_project/{safe_filename(project_key)}.json",
                {
                    "projectKey": project_key,
                    "totalCaptured": len(project_epics),
                    "issues": project_epics,
                },
            )

        selected_keys = {
            str(project.get("key", "")).strip().upper()
            for project in self.selected_projects
            if str(project.get("key", "")).strip()
        }
        selected_epics = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            fields = issue.get("fields") or {}
            project = fields.get("project") or {} if isinstance(fields, dict) else {}
            project_key = str(project.get("key", "")).strip().upper() if isinstance(project, dict) else ""
            if not selected_keys or project_key in selected_keys:
                selected_epics.append(issue)

        self.save_json(
            "data/issues/epics/selected_projects.json",
            {
                "selectedProjectKeys": sorted(selected_keys),
                "totalCaptured": len(selected_epics),
                "issues": selected_epics,
            },
        )

    def export_fields_and_field_schemes(self) -> None:
        fields = self.perform(
            "Fields",
            "GET /rest/api/3/field/search",
            "data/fields/fields.json",
            lambda: self.client.paginate(
                "/rest/api/3/field/search",
                params={"expand": "key,lastUsed,screensCount,contextsCount,isLocked,searcherKey"},
            ),
        )
        fields = fields if isinstance(fields, list) else []
        self.cache["fields"] = fields

        contexts_by_field: dict[str, Any] = {}
        context_issue_type_mappings: dict[str, Any] = {}
        context_project_mappings: dict[str, Any] = {}
        options_by_field_and_context: dict[str, Any] = {}
        default_values_grouped_by_field: dict[str, Any] = {}
        default_values_legacy_by_field: dict[str, Any] = {}

        # /field/search does not consistently return a top-level `custom` boolean.
        # Jira Cloud identifies custom fields reliably through `customfield_*` IDs
        # and the schema.custom module key.
        custom_fields = [
            field
            for field in fields
            if str(field.get("id", "")).startswith("customfield_")
            or bool((field.get("schema") or {}).get("custom"))
            or field.get("custom") is True
        ]
        for index, field in enumerate(custom_fields, 1):
            field_id = str(field.get("id", ""))
            if not field_id:
                continue
            label = f"Field contexts {field_id} ({index}/{len(custom_fields)})"
            contexts = self.perform(
                label,
                f"GET /rest/api/3/field/{field_id}/context",
                f"data/fields/contexts/{safe_filename(field_id)}.json",
                lambda fid=field_id: self.paginate_with_fallback(
                    [f"/rest/api/3/field/{fid}/context", f"/rest/api/3/field/{fid}/contexts"]
                ),
                optional=True,
            )
            if isinstance(contexts, list):
                contexts_by_field[field_id] = contexts

            issue_type_map = self.perform(
                f"Field context work type mappings {field_id}",
                f"GET /rest/api/3/field/{field_id}/context/issuetypemapping",
                f"data/fields/context_issue_type_mappings/{safe_filename(field_id)}.json",
                lambda fid=field_id: self.client.paginate(
                    f"/rest/api/3/field/{fid}/context/issuetypemapping"
                ),
                optional=True,
            )
            if issue_type_map is not None:
                context_issue_type_mappings[field_id] = issue_type_map

            project_map = self.perform(
                f"Field context project mappings {field_id}",
                f"GET /rest/api/3/field/{field_id}/context/projectmapping",
                f"data/fields/context_project_mappings/{safe_filename(field_id)}.json",
                lambda fid=field_id: self.client.paginate(
                    f"/rest/api/3/field/{fid}/context/projectmapping"
                ),
                optional=True,
            )
            if project_map is not None:
                context_project_mappings[field_id] = project_map

            if isinstance(contexts, list) and field_supports_options(field):
                for context in contexts:
                    context_id = str(context.get("id", ""))
                    if not context_id:
                        continue
                    options = self.perform(
                        f"Field options {field_id}/{context_id}",
                        f"GET /rest/api/3/field/{field_id}/context/{context_id}/option",
                        f"data/fields/options/{safe_filename(field_id)}__{safe_filename(context_id)}.json",
                        lambda fid=field_id, cid=context_id: self.client.paginate(
                            f"/rest/api/3/field/{fid}/context/{cid}/option"
                        ),
                        optional=True,
                    )
                    if options is not None:
                        options_by_field_and_context[f"{field_id}:{context_id}"] = options

            # Default values live on custom-field contexts and are independent of
            # whether the site uses legacy Field Configurations or new Field Schemes.
            # Export the current grouped API and the legacy compatibility API.
            grouped_defaults = self.perform(
                f"Field default values (grouped) {field_id}",
                f"GET /rest/api/2/field/{field_id}/context/defaultValues",
                f"data/fields/default_values_grouped/{safe_filename(field_id)}.json",
                lambda fid=field_id: self.client.paginate(
                    f"/rest/api/2/field/{fid}/context/defaultValues"
                ),
                optional=True,
                note="Preserves defaults grouped by context and work type when supported.",
            )
            if grouped_defaults is not None:
                default_values_grouped_by_field[field_id] = grouped_defaults

            legacy_defaults = self.perform(
                f"Field default values (legacy compatibility) {field_id}",
                f"GET /rest/api/2/field/{field_id}/context/defaultValue",
                f"data/fields/default_values_legacy/{safe_filename(field_id)}.json",
                lambda fid=field_id: self.client.paginate(
                    f"/rest/api/2/field/{fid}/context/defaultValue"
                ),
                optional=True,
                note="Compatibility export for Jira sites that have not fully moved to grouped defaults.",
            )
            if legacy_defaults is not None:
                default_values_legacy_by_field[field_id] = legacy_defaults

        self.cache.update(
            {
                "field_contexts": contexts_by_field,
                "field_context_issue_type_mappings": context_issue_type_mappings,
                "field_context_project_mappings": context_project_mappings,
                "field_options": options_by_field_and_context,
                "field_default_values_grouped": default_values_grouped_by_field,
                "field_default_values_legacy": default_values_legacy_by_field,
            }
        )

        field_configurations = self.perform(
            "Legacy field configurations",
            "GET /rest/api/3/fieldconfiguration",
            "data/fields/legacy_field_configurations.json",
            lambda: self.client.paginate("/rest/api/3/fieldconfiguration"),
            optional=True,
            note="Jira is gradually replacing legacy field configurations with Field Schemes.",
        )
        if isinstance(field_configurations, list):
            for configuration in field_configurations:
                config_id = str(configuration.get("id", ""))
                if not config_id:
                    continue
                self.perform(
                    f"Legacy field configuration items {config_id}",
                    f"GET /rest/api/3/fieldconfiguration/{config_id}/fields",
                    f"data/fields/legacy_field_configuration_items/{safe_filename(config_id)}.json",
                    lambda cid=config_id: self.client.paginate(
                        f"/rest/api/3/fieldconfiguration/{cid}/fields"
                    ),
                    optional=True,
                )

        legacy_schemes = self.perform(
            "Legacy field configuration schemes",
            "GET /rest/api/3/fieldconfigurationscheme",
            "data/fields/legacy_field_configuration_schemes.json",
            lambda: self.client.paginate("/rest/api/3/fieldconfigurationscheme"),
            optional=True,
        )
        legacy_mappings = self.perform(
            "Legacy field configuration scheme mappings",
            "GET /rest/api/3/fieldconfigurationscheme/mapping",
            "data/fields/legacy_field_configuration_scheme_mappings.json",
            lambda: self.client.paginate("/rest/api/3/fieldconfigurationscheme/mapping"),
            optional=True,
        )
        self.cache["legacy_field_configuration_schemes"] = legacy_schemes
        self.cache["legacy_field_configuration_scheme_mappings"] = legacy_mappings

        field_schemes = self.perform(
            "New Field Schemes (beta/opt-in)",
            "GET /rest/api/3/config/fieldschemes",
            "data/fields/new_field_schemes.json",
            lambda: self.client.paginate("/rest/api/3/config/fieldschemes"),
            optional=True,
            note="This endpoint may be unavailable unless the site has opted into the new Field Schemes API.",
        )
        if isinstance(field_schemes, list):
            for scheme in field_schemes:
                scheme_id = str(scheme.get("id", ""))
                if not scheme_id:
                    continue
                self.perform(
                    f"New Field Scheme details {scheme_id}",
                    f"GET /rest/api/3/config/fieldschemes/{scheme_id}",
                    f"data/fields/new_field_scheme_details/{safe_filename(scheme_id)}.json",
                    lambda sid=scheme_id: self.client.get(f"/rest/api/3/config/fieldschemes/{sid}"),
                    optional=True,
                )
                self.perform(
                    f"New Field Scheme fields {scheme_id}",
                    f"GET /rest/api/3/config/fieldschemes/{scheme_id}/fields",
                    f"data/fields/new_field_scheme_fields/{safe_filename(scheme_id)}.json",
                    lambda sid=scheme_id: self.client.paginate(
                        f"/rest/api/3/config/fieldschemes/{sid}/fields"
                    ),
                    optional=True,
                )
        self.cache["field_schemes"] = field_schemes

    def export_screens(self) -> None:
        screens = self.perform(
            "Screens",
            "GET /rest/api/3/screens",
            "data/screens/screens.json",
            lambda: self.client.paginate("/rest/api/3/screens"),
        )
        screens = screens if isinstance(screens, list) else []
        self.cache["screens"] = screens

        for screen in screens:
            screen_id = str(screen.get("id", ""))
            if not screen_id:
                continue
            tabs = self.perform(
                f"Screen tabs {screen_id}",
                f"GET /rest/api/3/screens/{screen_id}/tabs",
                f"data/screens/tabs/{safe_filename(screen_id)}.json",
                lambda sid=screen_id: self.client.get(f"/rest/api/3/screens/{sid}/tabs"),
                optional=True,
            )
            if isinstance(tabs, list):
                for tab in tabs:
                    tab_id = str(tab.get("id", ""))
                    if not tab_id:
                        continue
                    self.perform(
                        f"Screen tab fields {screen_id}/{tab_id}",
                        f"GET /rest/api/3/screens/{screen_id}/tabs/{tab_id}/fields",
                        f"data/screens/tab_fields/{safe_filename(screen_id)}__{safe_filename(tab_id)}.json",
                        lambda sid=screen_id, tid=tab_id: self.client.get(
                            f"/rest/api/3/screens/{sid}/tabs/{tid}/fields"
                        ),
                        optional=True,
                    )

        screen_schemes = self.perform(
            "Screen schemes",
            "GET /rest/api/3/screenscheme",
            "data/screens/screen_schemes.json",
            lambda: self.client.paginate("/rest/api/3/screenscheme"),
        )
        issue_type_screen_schemes = self.perform(
            "Work type screen schemes",
            "GET /rest/api/3/issuetypescreenscheme",
            "data/screens/issue_type_screen_schemes.json",
            lambda: self.client.paginate("/rest/api/3/issuetypescreenscheme"),
        )
        issue_type_screen_mappings = self.perform(
            "Work type screen scheme mappings",
            "GET /rest/api/3/issuetypescreenscheme/mapping",
            "data/screens/issue_type_screen_scheme_mappings.json",
            lambda: self.client.paginate("/rest/api/3/issuetypescreenscheme/mapping"),
        )
        self.cache.update(
            {
                "screen_schemes": screen_schemes,
                "issue_type_screen_schemes": issue_type_screen_schemes,
                "issue_type_screen_scheme_mappings": issue_type_screen_mappings,
            }
        )

    def export_statuses_workflows_and_schemes(self) -> None:
        status_categories = self.perform(
            "Status categories",
            "GET /rest/api/3/statuscategory",
            "data/workflows/status_categories.json",
            lambda: self.client.get("/rest/api/3/statuscategory"),
        )
        statuses = self.perform(
            "Statuses",
            "GET /rest/api/3/status",
            "data/workflows/statuses.json",
            lambda: self.client.get("/rest/api/3/status"),
        )

        workflows = self.perform(
            "Workflow search with transitions and rules",
            "GET /rest/api/3/workflow/search",
            "data/workflows/workflows_search.json",
            lambda: self.client.paginate(
                "/rest/api/3/workflow/search",
                params={
                    "expand": "transitions.rules,transitions.properties,statuses,operations,schemes,projects",
                    "orderBy": "name",
                },
            ),
        )
        workflows = workflows if isinstance(workflows, list) else []

        # New bulk read endpoint can expose richer workflow definitions on supported sites.
        bulk_workflow_results: list[Any] = []
        workflow_names = sorted(
            {
                str(workflow.get("id", {}).get("name") or workflow.get("name") or "").strip()
                for workflow in workflows
            }
            - {""}
        )
        for chunk_number, names in enumerate(chunks(workflow_names, 50), 1):
            result = self.perform(
                f"Bulk workflow definitions batch {chunk_number}",
                "POST /rest/api/3/workflows (read-only bulk retrieval)",
                f"data/workflows/bulk_read/batch_{chunk_number:03d}.json",
                lambda batch=names: self.client.post_read(
                    "/rest/api/3/workflows",
                    json_body={
                        "projectAndIssueTypes": [],
                        "workflowIds": [],
                        "workflowNames": batch,
                    },
                ),
                optional=True,
            )
            if result is not None:
                bulk_workflow_results.append(result)

        workflow_schemes = self.perform(
            "Workflow schemes",
            "GET /rest/api/3/workflowscheme",
            "data/workflows/workflow_schemes.json",
            lambda: self.client.get("/rest/api/3/workflowscheme"),
        )
        scheme_list = unwrap_list(workflow_schemes, preferred_keys=("values",))
        for scheme in scheme_list:
            scheme_id = str(scheme.get("id", "")) if isinstance(scheme, dict) else ""
            if not scheme_id:
                continue
            self.perform(
                f"Workflow scheme details {scheme_id}",
                f"GET /rest/api/3/workflowscheme/{scheme_id}",
                f"data/workflows/workflow_scheme_details/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.get(f"/rest/api/3/workflowscheme/{sid}"),
                optional=True,
            )
            self.perform(
                f"Workflow scheme workflow mappings {scheme_id}",
                f"GET /rest/api/3/workflowscheme/{scheme_id}/workflow",
                f"data/workflows/workflow_scheme_workflow_mappings/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.get(f"/rest/api/3/workflowscheme/{sid}/workflow"),
                optional=True,
            )
            self.perform(
                f"Workflow scheme draft {scheme_id}",
                f"GET /rest/api/3/workflowscheme/{scheme_id}/draft",
                f"data/workflows/workflow_scheme_drafts/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.get(f"/rest/api/3/workflowscheme/{sid}/draft"),
                optional=True,
            )

        self.cache.update(
            {
                "status_categories": status_categories,
                "statuses": statuses,
                "workflows": workflows,
                "bulk_workflow_results": bulk_workflow_results,
                "workflow_schemes": workflow_schemes,
            }
        )

    def export_permissions_and_notifications(self) -> None:
        permission_schemes = self.perform(
            "Permission schemes",
            "GET /rest/api/3/permissionscheme",
            "data/permissions/permission_schemes.json",
            lambda: self.client.get("/rest/api/3/permissionscheme"),
            optional=True,
        )
        for scheme in unwrap_list(permission_schemes, preferred_keys=("permissionSchemes", "values")):
            scheme_id = str(scheme.get("id", "")) if isinstance(scheme, dict) else ""
            if not scheme_id:
                continue
            self.perform(
                f"Permission scheme details {scheme_id}",
                f"GET /rest/api/3/permissionscheme/{scheme_id}",
                f"data/permissions/permission_scheme_details/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.get(
                    f"/rest/api/3/permissionscheme/{sid}",
                    params={"expand": "permissions,user,group,projectRole,field,all"},
                ),
                optional=True,
            )

        notification_schemes = self.perform(
            "Notification schemes",
            "GET /rest/api/3/notificationscheme",
            "data/permissions/notification_schemes.json",
            lambda: self.client.paginate(
                "/rest/api/3/notificationscheme",
                params={"expand": "all"},
            ),
            optional=True,
        )
        for scheme in unwrap_list(notification_schemes):
            scheme_id = str(scheme.get("id", "")) if isinstance(scheme, dict) else ""
            if not scheme_id:
                continue
            self.perform(
                f"Notification scheme details {scheme_id}",
                f"GET /rest/api/3/notificationscheme/{scheme_id}",
                f"data/permissions/notification_scheme_details/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.get(
                    f"/rest/api/3/notificationscheme/{sid}", params={"expand": "all"}
                ),
                optional=True,
            )

        security_schemes = self.perform(
            "Issue security schemes",
            "GET /rest/api/3/issuesecurityschemes",
            "data/permissions/issue_security_schemes.json",
            lambda: self.client.get("/rest/api/3/issuesecurityschemes"),
            optional=True,
        )
        for scheme in unwrap_list(security_schemes, preferred_keys=("issueSecuritySchemes", "values")):
            scheme_id = str(scheme.get("id", "")) if isinstance(scheme, dict) else ""
            if not scheme_id:
                continue
            self.perform(
                f"Issue security scheme details {scheme_id}",
                f"GET /rest/api/3/issuesecurityschemes/{scheme_id}",
                f"data/permissions/issue_security_scheme_details/{safe_filename(scheme_id)}.json",
                lambda sid=scheme_id: self.client.get(f"/rest/api/3/issuesecurityschemes/{sid}"),
                optional=True,
            )

        self.cache.update(
            {
                "permission_schemes": permission_schemes,
                "notification_schemes": notification_schemes,
                "issue_security_schemes": security_schemes,
            }
        )

    def export_filters_and_boards(self) -> None:
        filters = self.perform(
            "Accessible filters",
            "GET /rest/api/3/filter/search",
            "data/filters/filters.json",
            lambda: self.client.paginate(
                "/rest/api/3/filter/search",
                params={
                    "expand": (
                        "description,owner,jql,viewUrl,searchUrl,favourite,"
                        "favouritedCount,sharePermissions,subscriptions"
                    ),
                    "orderBy": "name",
                },
            ),
            optional=True,
            note="Jira returns only filters visible to the authenticated account; inaccessible private filters cannot be exported.",
        )
        self.cache["filters"] = filters
        if isinstance(filters, list):
            for filter_item in filters:
                filter_id = str(filter_item.get("id", ""))
                if not filter_id:
                    continue
                filter_columns = self.perform(
                    f"Filter columns {filter_id}",
                    f"GET /rest/api/3/filter/{filter_id}/columns",
                    f"data/filters/columns/{safe_filename(filter_id)}.json",
                    lambda fid=filter_id: self.client.get(f"/rest/api/3/filter/{fid}/columns"),
                    optional=True,
                )
                if filter_columns is not None:
                    self.cache.setdefault("filter_columns", {})[filter_id] = filter_columns

        boards = self.perform(
            "Boards",
            "GET /rest/agile/1.0/board",
            "data/boards/boards.json",
            lambda: self.client.paginate("/rest/agile/1.0/board"),
            optional=True,
        )
        boards = boards if isinstance(boards, list) else []
        board_configs: dict[str, Any] = {}
        for board in boards:
            board_id = str(board.get("id", ""))
            if not board_id:
                continue
            config = self.perform(
                f"Board configuration {board_id}",
                f"GET /rest/agile/1.0/board/{board_id}/configuration",
                f"data/boards/configuration/{safe_filename(board_id)}.json",
                lambda bid=board_id: self.client.get(
                    f"/rest/agile/1.0/board/{bid}/configuration"
                ),
                optional=True,
            )
            if config is not None:
                board_configs[board_id] = config

            # Jira's public Agile REST board configuration omits several settings
            # shown in the Board settings UI. Jira itself loads an internal, read-only
            # board edit model when opening Board settings. Capture that raw model as
            # an additional source artifact. The endpoint is undocumented and therefore
            # treated as optional: changes by Atlassian never stop the export.
            edit_model = self.perform(
                f"Board settings edit model {board_id}",
                f"GET /rest/greenhopper/1.0/rapidviewconfig/editmodel.json?rapidViewId={board_id}",
                f"data/boards/editmodel/{safe_filename(board_id)}.json",
                lambda bid=board_id: self.client.get(
                    "/rest/greenhopper/1.0/rapidviewconfig/editmodel.json",
                    params={"rapidViewId": bid},
                ),
                optional=True,
                note=(
                    "Undocumented Jira-internal read endpoint used by the Board settings UI. "
                    "Saved raw because it can contain settings not present in the public Agile board configuration, "
                    "including Timeline/Roadmap-related configuration on supported Jira Cloud builds."
                ),
            )
            if edit_model is not None:
                self.cache.setdefault("board_edit_models", {})[board_id] = edit_model
                timeline_candidates = extract_named_configuration(
                    edit_model,
                    keywords=(
                        "timeline",
                        "roadmap",
                        "childissues",
                        "childissue",
                        "childlevel",
                        "schedule",
                        "rollup",
                    ),
                )
                self.save_json(
                    f"data/boards/timeline_candidates/{safe_filename(board_id)}.json",
                    {
                        "boardId": board_id,
                        "source": "rapidviewconfig/editmodel.json",
                        "matches": timeline_candidates,
                        "note": (
                            "This is a keyword-indexed convenience view of the raw board edit model. "
                            "The raw edit model remains the source of truth."
                        ),
                    },
                )

            property_keys = self.perform(
                f"Board property keys {board_id}",
                f"GET /rest/agile/1.0/board/{board_id}/properties",
                f"data/boards/properties/{safe_filename(board_id)}__keys.json",
                lambda bid=board_id: self.client.get(f"/rest/agile/1.0/board/{bid}/properties"),
                optional=True,
            )
            if isinstance(property_keys, dict):
                for key in property_keys.get("keys", []):
                    property_key = key.get("key") if isinstance(key, dict) else key
                    if not property_key:
                        continue
                    self.perform(
                        f"Board property {board_id}/{property_key}",
                        f"GET /rest/agile/1.0/board/{board_id}/properties/{property_key}",
                        (
                            f"data/boards/properties/{safe_filename(board_id)}__"
                            f"{safe_filename(str(property_key))}.json"
                        ),
                        lambda bid=board_id, pkey=property_key: self.client.get(
                            f"/rest/agile/1.0/board/{bid}/properties/{pkey}"
                        ),
                        optional=True,
                    )
        self.cache["boards"] = boards
        self.cache["board_configurations"] = board_configs

    def export_list_view_server_columns(self) -> None:
        """Export Jira-supported List/issue-table column defaults in exact order.

        Jira exposes the authenticated user's default issue-table columns through
        GET /rest/api/3/user/columns. Atlassian's List UI can use these when the
        Configure columns source is "My defaults". This is distinct from saved-filter
        columns and from Scrum/Kanban workflow columns.

        The global/system defaults endpoint is also captured when the account has
        Jira-admin permission. Neither endpoint is claimed to be a project-specific
        saved-view API; finalization records the scope explicitly.
        """
        user_columns = self.perform(
            "Authenticated user default List columns",
            "GET /rest/api/3/user/columns",
            "data/list_views/user_default_columns.json",
            lambda: self.client.get("/rest/api/3/user/columns"),
            optional=True,
            note=(
                "Supported Jira REST endpoint. Preserves the calling user's default issue-table "
                "columns and their returned order. This can be the source of Jira List columns when "
                "the UI is using My defaults, but it is not by itself a project saved-view API."
            ),
        )
        system_columns = self.perform(
            "System default List columns",
            "GET /rest/api/3/settings/columns",
            "data/list_views/system_default_columns.json",
            lambda: self.client.get("/rest/api/3/settings/columns"),
            optional=True,
            note=(
                "Supported Jira REST endpoint requiring Jira-admin permission. These are the system "
                "issue-table defaults for users who do not have their own defaults."
            ),
        )
        self.cache["list_user_default_columns"] = user_columns
        self.cache["list_system_default_columns"] = system_columns

    def _csv_current_fields_payload(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        scope: str,
        project_key: str | None = None,
        filter_id: str | None = None,
        filter_name: str | None = None,
    ) -> dict[str, Any]:
        """Fetch Jira's legacy/current-fields CSV view and resolve its header to field IDs.

        Atlassian's UI describes ``CSV (current fields)`` as exporting the fields
        currently visible in the work-item navigator/List experience. Jira Cloud
        still serves the searchrequest-csv-current-fields view on many tenants.
        This is deliberately treated as a *probe*: it is useful evidence for the
        displayed order, but it is not claimed to be a public saved-space-view API.
        """
        raw, content_type = self.client.get_bytes(
            path,
            params=params,
            accept="text/csv,application/csv,text/plain,*/*",
            timeout=90,
        )
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            text = raw.decode("utf-8", errors="replace")
        lowered = text.lstrip().lower()
        if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
            raise RuntimeError("Jira returned HTML instead of the current-fields CSV view")
        if "application/json" in (content_type or "").lower() and text.lstrip().startswith(("{", "[")):
            raise RuntimeError("Jira returned JSON instead of a CSV current-fields export")

        reader = csv.reader(io.StringIO(text))
        header: list[str] = []
        for row in reader:
            if not row or not any(str(cell).strip() for cell in row):
                continue
            first = str(row[0]).strip().lstrip("\ufeff") if row else ""
            if len(row) == 1 and first.lower().startswith("sep="):
                continue
            header = [str(cell).strip().lstrip("\ufeff") for cell in row]
            break
        if not header:
            raise RuntimeError("CSV current-fields response did not contain a header row")

        field_catalog = build_field_catalog(self.cache.get("fields") or [])
        columns, unresolved = columns_from_csv_headers(header, field_catalog)
        return {
            "scope": scope,
            "projectKey": project_key,
            "filterId": filter_id,
            "filterName": filter_name,
            "requestPath": path,
            "requestParams": dict(params or {}),
            "contentType": content_type,
            "rawHeader": header,
            "columns": columns,
            "unresolvedHeaders": unresolved,
            "columnCount": len(columns),
            "note": (
                "This is a read-only CSV current-fields probe. It captures the order Jira exported for the "
                "authenticated account, but does not by itself prove a server-side saved space view."
            ),
        }

    def export_current_fields_column_views(self) -> None:
        """Capture current-field column order for All Work/List and every saved filter.

        v1.8 captured the supported user/system defaults and filter-specific layouts,
        but that can miss the *effective* columns visible in Jira's All Work/List UI.
        v1.9 adds Jira's CSV ``current fields`` view as an independent read-only probe.
        """
        all_work = self.perform(
            "All Work current-fields column probe",
            "GET /sr/jira.issueviews:searchrequest-csv-current-fields/temp/SearchRequest.csv",
            "data/all_work/current_fields.json",
            lambda: self._csv_current_fields_payload(
                "/sr/jira.issueviews:searchrequest-csv-current-fields/temp/SearchRequest.csv",
                params={"jqlQuery": "ORDER BY created DESC", "tempMax": 1},
                scope="authenticated-user All Work/current-fields",
            ),
            optional=True,
            note="Read-only probe of Jira's CSV (current fields) output for the authenticated account.",
        )
        self.cache["all_work_current_fields"] = all_work

        project_probes: dict[str, Any] = {}
        for project in self.selected_projects:
            key = str(project.get("key") or "").strip().upper()
            if not key:
                continue
            payload = self.perform(
                f"{key} List current-fields column probe",
                "GET /sr/jira.issueviews:searchrequest-csv-current-fields/temp/SearchRequest.csv",
                Path("data/projects") / safe_filename(key) / "list_view/current_fields.json",
                lambda pkey=key: self._csv_current_fields_payload(
                    "/sr/jira.issueviews:searchrequest-csv-current-fields/temp/SearchRequest.csv",
                    params={"jqlQuery": f'project = "{pkey}" ORDER BY created DESC', "tempMax": 1},
                    scope="project JQL current-fields probe",
                    project_key=pkey,
                ),
                optional=True,
                note=(
                    "Project-scoped current-fields probe. Useful for validating the effective List columns, "
                    "but not treated as an exact saved-view API."
                ),
            )
            if payload is not None:
                project_probes[key] = payload
        self.cache["project_list_current_fields"] = project_probes

        filter_probes: dict[str, Any] = {}
        filters = self.cache.get("filters") or []
        for filter_item in filters if isinstance(filters, list) else []:
            if not isinstance(filter_item, dict):
                continue
            fid = str(filter_item.get("id") or "").strip()
            name = str(filter_item.get("name") or "").strip()
            if not fid:
                continue
            path = f"/sr/jira.issueviews:searchrequest-csv-current-fields/{fid}/SearchRequest-{fid}.csv"
            payload = self.perform(
                f"Filter current-fields columns {name or fid}",
                f"GET {path}",
                Path("data/filters/current_fields") / f"{safe_filename(fid)}.json",
                lambda fpath=path, filter_id=fid, filter_name=name: self._csv_current_fields_payload(
                    fpath,
                    params={"tempMax": 1},
                    scope="saved-filter current-fields probe",
                    filter_id=filter_id,
                    filter_name=filter_name,
                ),
                optional=True,
                note=(
                    "Independent current-fields probe. Filter-specific REST columns remain authoritative when present."
                ),
            )
            if payload is not None:
                filter_probes[fid] = payload
        self.cache["filter_current_fields"] = filter_probes

        self.save_json(
            "data/all_work/capture_summary.json",
            {
                "userDefaultColumns": normalize_jira_column_items(self.cache.get("list_user_default_columns")),
                "systemDefaultColumns": normalize_jira_column_items(self.cache.get("list_system_default_columns")),
                "currentFieldsProbe": all_work,
                "important": (
                    "All Work/List columns can be influenced by user defaults, filter columns, and the newer space saved-view model. "
                    "The exporter stores each source separately instead of conflating them."
                ),
            },
        )

    def finalize_effective_filter_columns(self) -> None:
        """Write an effective ordered column layout for every exported filter.

        Jira returns HTTP 404 from ``GET /filter/{id}/columns`` when a saved
        filter does not have its own column layout. That does *not* mean the
        filter has no columns in the UI: Jira falls back to the authenticated
        user's default columns (or the system defaults). Earlier exporter
        versions only wrote files for filters with an explicit filter-specific
        layout, which made inherited layouts look as if they had been dropped.

        v1.9 preserves both concepts:
        - ``data/filters/columns/<id>.json`` remains the raw filter-specific
          layout when Jira exposes one.
        - ``data/filters/effective_columns/<id>.json`` is written for every
          visible filter and records the ordered layout that should be used to
          reproduce what the exporting account sees.
        """
        filters = self.cache.get("filters") or []
        explicit_by_filter = self.cache.get("filter_columns") or {}
        current_fields_by_filter = self.cache.get("filter_current_fields") or {}
        user_columns = normalize_jira_column_items(self.cache.get("list_user_default_columns"))
        system_columns = normalize_jira_column_items(self.cache.get("list_system_default_columns"))

        summary: list[dict[str, Any]] = []
        for filter_item in filters if isinstance(filters, list) else []:
            if not isinstance(filter_item, dict):
                continue
            filter_id = str(filter_item.get("id", "")).strip()
            if not filter_id:
                continue
            explicit = normalize_jira_column_items(explicit_by_filter.get(filter_id))
            current_probe = current_fields_by_filter.get(filter_id) if isinstance(current_fields_by_filter, dict) else None
            current_columns = normalize_jira_column_items((current_probe or {}).get("columns") if isinstance(current_probe, dict) else None)
            if explicit:
                source = "filter-specific"
                columns = explicit
                has_filter_layout = True
            elif current_columns:
                source = "saved-filter-current-fields-probe"
                columns = current_columns
                has_filter_layout = False
            elif user_columns:
                source = "authenticated-user-default"
                columns = user_columns
                has_filter_layout = False
            elif system_columns:
                source = "system-default"
                columns = system_columns
                has_filter_layout = False
            else:
                source = "not-captured"
                columns = []
                has_filter_layout = False

            payload = {
                "filterId": filter_id,
                "filterName": filter_item.get("name"),
                "hasFilterSpecificColumnLayout": has_filter_layout,
                "effectiveColumnSource": source,
                "columns": columns,
                "note": (
                    "Filter-specific columns were returned by Jira."
                    if has_filter_layout
                    else (
                        "Jira did not return a filter-specific layout; v1.9 prefers the filter's CSV current-fields probe, "
                        "then falls back to the exporting account's user defaults/system defaults."
                    )
                ),
            }
            rel = Path("data/filters/effective_columns") / f"{safe_filename(filter_id)}.json"
            self.save_json(rel, payload)
            summary.append({
                "filterId": filter_id,
                "filterName": filter_item.get("name"),
                "source": source,
                "columnCount": len(columns),
                "hasFilterSpecificColumnLayout": has_filter_layout,
                "output": rel.as_posix(),
            })

        self.cache["effective_filter_columns"] = summary
        self.save_json("data/filters/effective_columns_summary.json", summary)

    def export_local_browser_view_state(self) -> None:
        """Capture Firefox Jira-origin state that can hold personal List configuration.

        v1.6 only copied localStorage. v1.7 additionally snapshots IndexedDB and
        extracts Jira-scoped sessionStorage from Firefox session-restore files when
        available. Cookies are never copied. The exporter then looks for ordered
        field sequences rather than merely matching words such as "column".
        """
        if not self.settings.include_browser_view_state:
            return

        host = site_hostname(self.client.site_url)
        profiles = find_firefox_profiles(self.settings.firefox_profile)
        if not profiles:
            self.manual_actions.append(
                "No Firefox profile was found, so browser-local Jira List-view state could not be captured. "
                "If the source List layout is personal/unsaved, open it in Firefox and re-run on the same OS user, "
                "or pass --firefox-profile PATH."
            )
            self.operations.append(
                OperationRecord(
                    name="Firefox Jira List-view browser state",
                    status="unsupported_or_inaccessible",
                    endpoint="LOCAL Firefox profile localStorage/IndexedDB/sessionStorage",
                    item_count=0,
                    note="No Firefox profile found; no network request was made.",
                )
            )
            return

        captures: list[dict[str, Any]] = []
        all_sequences: list[dict[str, Any]] = []
        field_catalog = build_field_catalog(self.cache.get("fields") or [])
        selected_keys = [str(p.get("key", "")).upper() for p in self.selected_projects if p.get("key")]

        for profile in profiles:
            profile_capture: dict[str, Any] = {
                "profile": profile.name,
                "origins": [],
                "sessionStorage": None,
            }
            for origin_dir in find_firefox_origin_storage(profile, host):
                rel_base = (
                    Path("data/browser_view_state/firefox")
                    / safe_filename(profile.name)
                    / safe_filename(origin_dir.name)
                )
                origin_capture: dict[str, Any] = {
                    "origin": origin_dir.name,
                    "localStorage": None,
                    "indexedDB": [],
                }

                # localStorage
                db_path = origin_dir / "ls" / "data.sqlite"
                if db_path.exists():
                    try:
                        raw_dest = self.root / rel_base / "local_storage" / "data.sqlite"
                        raw_dest.parent.mkdir(parents=True, exist_ok=True)
                        backup_sqlite_database(db_path, raw_dest)
                        dump = dump_sqlite_database(raw_dest)
                        self.save_json(rel_base / "local_storage" / "dump.json", dump)
                        sequences = find_ordered_field_sequences(
                            dump,
                            field_catalog=field_catalog,
                            project_keys=selected_keys,
                            source_label=f"Firefox localStorage/{profile.name}/{origin_dir.name}",
                        )
                        self.save_json(
                            rel_base / "local_storage" / "detected_column_sequences.json", sequences
                        )
                        all_sequences.extend(sequences)
                        origin_capture["localStorage"] = {
                            "database": str((rel_base / "local_storage" / "data.sqlite").as_posix()),
                            "dump": str((rel_base / "local_storage" / "dump.json").as_posix()),
                            "sequenceCount": len(sequences),
                        }
                    except (OSError, sqlite3.Error) as exc:
                        self.manual_actions.append(
                            f"Could not read Firefox Jira localStorage from profile '{profile.name}': {exc}."
                        )

                # IndexedDB - capture every Jira-origin SQLite DB plus companion files.
                idb_root = origin_dir / "idb"
                if idb_root.exists():
                    raw_idb_root = self.root / rel_base / "indexeddb" / "raw"
                    try:
                        copy_tree_best_effort(idb_root, raw_idb_root)
                    except OSError as exc:
                        self.manual_actions.append(
                            f"Could not fully copy Firefox Jira IndexedDB for profile '{profile.name}': {exc}."
                        )
                    for sqlite_path in sorted(idb_root.rglob("*.sqlite")):
                        relative_db = sqlite_path.relative_to(idb_root)
                        try:
                            snapshot = self.root / rel_base / "indexeddb" / "sqlite" / relative_db
                            snapshot.parent.mkdir(parents=True, exist_ok=True)
                            backup_sqlite_database(sqlite_path, snapshot)
                            dump = dump_sqlite_database(snapshot)
                            dump_rel = rel_base / "indexeddb" / "dumps" / relative_db.with_suffix(".json")
                            self.save_json(dump_rel, dump)
                            sequences = find_ordered_field_sequences(
                                dump,
                                field_catalog=field_catalog,
                                project_keys=selected_keys,
                                source_label=f"Firefox IndexedDB/{profile.name}/{origin_dir.name}/{relative_db.as_posix()}",
                            )
                            all_sequences.extend(sequences)
                            origin_capture["indexedDB"].append(
                                {
                                    "database": str((rel_base / "indexeddb" / "sqlite" / relative_db).as_posix()),
                                    "dump": str(dump_rel.as_posix()),
                                    "sequenceCount": len(sequences),
                                }
                            )
                        except (OSError, sqlite3.Error) as exc:
                            origin_capture["indexedDB"].append(
                                {"database": relative_db.as_posix(), "error": str(exc)}
                            )
                profile_capture["origins"].append(origin_capture)

            # Firefox may persist sessionStorage in sessionstore-backups. Extract only
            # the current Jira origin's storage; never save the whole browsing session.
            try:
                session_storage = extract_firefox_jira_session_storage(profile, host)
                if session_storage:
                    rel_session = (
                        Path("data/browser_view_state/firefox")
                        / safe_filename(profile.name)
                        / "jira_session_storage.json"
                    )
                    self.save_json(rel_session, session_storage)
                    sequences = find_ordered_field_sequences(
                        session_storage,
                        field_catalog=field_catalog,
                        project_keys=selected_keys,
                        source_label=f"Firefox sessionStorage/{profile.name}",
                    )
                    all_sequences.extend(sequences)
                    profile_capture["sessionStorage"] = {
                        "file": str(rel_session.as_posix()),
                        "sequenceCount": len(sequences),
                    }
            except Exception as exc:  # noqa: BLE001 - local browser artifact is optional
                self.manual_actions.append(
                    f"Could not inspect Firefox Jira sessionStorage for profile '{profile.name}': {type(exc).__name__}: {exc}."
                )

            if profile_capture["origins"] or profile_capture["sessionStorage"]:
                captures.append(profile_capture)

        all_sequences = dedupe_column_sequences(all_sequences)
        self.cache["browser_list_column_sequences"] = all_sequences
        self.save_json(
            "data/browser_view_state/detected_list_column_sequences.json",
            {
                "sourceSite": self.client.site_url,
                "sequenceCount": len(all_sequences),
                "sequences": all_sequences,
                "note": (
                    "v1.7 only lists entries that contain an ordered sequence of Jira field identifiers/names. "
                    "Candidates are scored and later resolved per selected project; existence alone does not mean "
                    "the exact project List view was captured."
                ),
            },
        )
        self.save_json(
            "data/browser_view_state/firefox_capture_manifest.json",
            {
                "sourceSite": self.client.site_url,
                "host": host,
                "captures": captures,
                "detectedColumnSequenceCount": len(all_sequences),
                "security": (
                    "This folder contains Jira-origin localStorage/IndexedDB snapshots and Jira-scoped "
                    "sessionStorage extracted from the local Firefox profile. Cookies are not copied and the "
                    "full Firefox browsing session is not saved. Browser-local site data can still be sensitive."
                ),
            },
        )
        self.operations.append(
            OperationRecord(
                name="Firefox Jira List-view browser state",
                status="success" if captures else "unsupported_or_inaccessible",
                endpoint="LOCAL Firefox profile localStorage/IndexedDB/sessionStorage",
                item_count=len(all_sequences),
                output_file="data/browser_view_state/firefox_capture_manifest.json",
                note=(
                    "v1.7 scans localStorage, IndexedDB, and Jira-scoped sessionStorage for ordered Jira field "
                    "sequences, instead of treating keyword matches as successful List-view capture."
                ),
            )
        )
        if not all_sequences:
            self.manual_actions.append(
                "Firefox site storage was captured, but no ordered Jira field sequence was detected for the List view. "
                "The supported user-default columns API is still exported and will be evaluated as a fallback."
            )

    def export_users_groups_and_roles(self) -> None:
        roles = self.perform(
            "Global project roles",
            "GET /rest/api/3/role",
            "data/users_and_groups/project_roles.json",
            lambda: self.client.get("/rest/api/3/role"),
            optional=True,
        )
        if isinstance(roles, list):
            for role in roles:
                role_id = str(role.get("id", ""))
                if not role_id:
                    continue
                self.perform(
                    f"Project role details {role_id}",
                    f"GET /rest/api/3/role/{role_id}",
                    f"data/users_and_groups/project_role_details/{safe_filename(role_id)}.json",
                    lambda rid=role_id: self.client.get(f"/rest/api/3/role/{rid}"),
                    optional=True,
                )

        groups: list[Any] = []
        if self.settings.include_users or self.settings.include_group_members:
            group_result = self.perform(
                "Groups",
                "GET /rest/api/3/group/bulk",
                "data/users_and_groups/groups.json",
                lambda: self.client.paginate("/rest/api/3/group/bulk"),
                optional=True,
            )
            groups = group_result if isinstance(group_result, list) else []

        if self.settings.include_group_members:
            for group in groups:
                group_id = str(group.get("groupId", ""))
                group_name = str(group.get("name", ""))
                if not group_id and not group_name:
                    continue
                params: dict[str, Any] = {
                    "includeInactiveUsers": "true",
                }
                if group_id:
                    params["groupId"] = group_id
                else:
                    params["groupname"] = group_name
                self.perform(
                    f"Group members {group_name or group_id}",
                    "GET /rest/api/3/group/member",
                    f"data/users_and_groups/group_members/{safe_filename(group_name or group_id)}.json",
                    lambda query=params: self.client.paginate(
                        "/rest/api/3/group/member", params=query
                    ),
                    optional=True,
                )

        if self.settings.include_users:
            users = self.perform(
                "Users visible to Jira user search",
                "GET /rest/api/3/users/search",
                "data/users_and_groups/users.json",
                lambda: paginate_array_endpoint(
                    self.client,
                    "/rest/api/3/users/search",
                    params={"includeActive": "true", "includeInactive": "true"},
                ),
                optional=True,
                note="Email addresses may be absent because of Atlassian privacy settings.",
            )
            self.cache["users"] = users
        self.cache["groups"] = groups
        self.cache["project_roles"] = roles

    def export_project_specific_configuration(self) -> None:
        project_associations: list[dict[str, Any]] = []

        for project in self.selected_projects:
            key = str(project.get("key", "")).strip()
            project_id = str(project.get("id", "")).strip()
            if not key:
                continue
            pdir = Path("data/projects") / safe_filename(key)

            details = self.perform(
                f"Project details {key}",
                f"GET /rest/api/3/project/{key}",
                pdir / "project.json",
                lambda pkey=key: self.client.get(
                    f"/rest/api/3/project/{pkey}",
                    params={
                        "expand": "description,lead,issueTypes,url,projectKeys,permissions,insight"
                    },
                ),
            )
            components = self.perform(
                f"Components {key}",
                f"GET /rest/api/3/project/{key}/component",
                pdir / "components.json",
                lambda pkey=key: self.client.paginate(
                    f"/rest/api/3/project/{pkey}/component"
                ),
                optional=True,
            )
            versions = self.perform(
                f"Versions/releases {key}",
                f"GET /rest/api/3/project/{key}/version",
                pdir / "versions.json",
                lambda pkey=key: self.client.paginate(
                    f"/rest/api/3/project/{pkey}/version", params={"orderBy": "sequence"}
                ),
                optional=True,
            )
            project_statuses = self.perform(
                f"Project work type/status mappings {key}",
                f"GET /rest/api/3/project/{key}/statuses",
                pdir / "statuses_by_work_type.json",
                lambda pkey=key: self.client.get(f"/rest/api/3/project/{pkey}/statuses"),
                optional=True,
            )
            features = self.perform(
                f"Project features {key}",
                f"GET /rest/api/3/project/{key}/features",
                pdir / "features.json",
                lambda pkey=key: self.client.get(f"/rest/api/3/project/{pkey}/features"),
                optional=True,
            )
            role_actors = self.perform(
                f"Project role actors {key}",
                f"GET /rest/api/3/project/{key}/role",
                pdir / "role_actors.json",
                lambda pkey=key: export_project_role_actors(self.client, pkey),
                optional=True,
            )
            permission_scheme = self.perform(
                f"Project permission scheme {key}",
                f"GET /rest/api/3/project/{key}/permissionscheme",
                pdir / "permission_scheme.json",
                lambda pkey=key: self.client.get(
                    f"/rest/api/3/project/{pkey}/permissionscheme",
                    params={"expand": "permissions,user,group,projectRole,field,all"},
                ),
                optional=True,
            )
            notification_scheme = self.perform(
                f"Project notification scheme {key}",
                f"GET /rest/api/3/project/{key}/notificationscheme",
                pdir / "notification_scheme.json",
                lambda pkey=key: self.client.get(
                    f"/rest/api/3/project/{pkey}/notificationscheme", params={"expand": "all"}
                ),
                optional=True,
            )
            issue_security_scheme = self.perform(
                f"Project issue security scheme {key}",
                f"GET /rest/api/3/project/{key}/issuesecuritylevelscheme",
                pdir / "issue_security_scheme.json",
                lambda pkey=key: self.client.get(
                    f"/rest/api/3/project/{pkey}/issuesecuritylevelscheme"
                ),
                optional=True,
            )

            priority_scheme = None
            for detail in (self.cache.get("priority_scheme_details") or {}).values():
                projects_for_scheme = detail.get("projects") or []
                if any(
                    str(p.get("id", "")) == project_id
                    or str(p.get("key", "")).upper() == key.upper()
                    for p in projects_for_scheme
                    if isinstance(p, dict)
                ):
                    priority_scheme = detail
                    break
            if priority_scheme is None:
                # Jira's default priority scheme applies to projects not explicitly
                # assigned to another scheme. Some tenants omit those projects from
                # the scheme-projects endpoint, so use the exported default as fallback.
                priority_scheme = next(
                    (d for d in (self.cache.get("priority_scheme_details") or {}).values()
                     if isinstance(d, dict) and d.get("isDefault") is True),
                    None,
                )
            self.save_json(pdir / "priority_scheme.json", priority_scheme)

            association_payload: dict[str, Any] = {
                "projectKey": key,
                "projectId": project_id,
                "project": details,
                "permissionScheme": permission_scheme,
                "notificationScheme": notification_scheme,
                "issueSecurityScheme": issue_security_scheme,
                "priorityScheme": priority_scheme,
            }

            association_specs = [
                (
                    "issueTypeScheme",
                    f"/rest/api/3/issuetypescheme/project?projectId={project_id}",
                    "/rest/api/3/issuetypescheme/project",
                    {"projectId": project_id},
                    pdir / "issue_type_scheme_association.json",
                ),
                (
                    "issueTypeScreenScheme",
                    f"/rest/api/3/issuetypescreenscheme/project?projectId={project_id}",
                    "/rest/api/3/issuetypescreenscheme/project",
                    {"projectId": project_id},
                    pdir / "issue_type_screen_scheme_association.json",
                ),
                (
                    "fieldConfigurationScheme",
                    f"/rest/api/3/fieldconfigurationscheme/project?projectId={project_id}",
                    "/rest/api/3/fieldconfigurationscheme/project",
                    {"projectId": project_id},
                    pdir / "field_configuration_scheme_association.json",
                ),
                (
                    "workflowScheme",
                    f"/rest/api/3/workflowscheme/project?projectId={project_id}",
                    "/rest/api/3/workflowscheme/project",
                    {"projectId": project_id},
                    pdir / "workflow_scheme_association.json",
                ),
            ]
            for assoc_name, endpoint_label, endpoint_path, params, output in association_specs:
                if not project_id:
                    association_payload[assoc_name] = None
                    continue
                payload = self.perform(
                    f"Project {assoc_name} association {key}",
                    f"GET {endpoint_label}",
                    output,
                    lambda path=endpoint_path, query=params: self.client.get(path, params=query),
                    optional=True,
                )
                association_payload[assoc_name] = payload

            if self.settings.include_project_properties:
                property_payload = self.perform(
                    f"Project properties {key}",
                    f"GET /rest/api/3/project/{key}/properties",
                    pdir / "properties.json",
                    lambda pkey=key: export_entity_properties(
                        self.client, f"/rest/api/3/project/{pkey}/properties"
                    ),
                    optional=True,
                )
                association_payload["projectProperties"] = property_payload

            project_associations.append(association_payload)

            if bool(project.get("simplified")) or (isinstance(details, dict) and details.get("simplified")):
                self.manual_actions.append(
                    f"{key} appears to be team-managed/simplified. Jira does not expose every team-managed setting through the same scheme APIs as company-managed projects."
                )

            self.cache.setdefault("project_details", {})[key] = details
            self.cache.setdefault("components", {})[key] = components
            self.cache.setdefault("versions", {})[key] = versions
            self.cache.setdefault("project_statuses", {})[key] = project_statuses
            self.cache.setdefault("project_features", {})[key] = features
            self.cache.setdefault("project_role_actors", {})[key] = role_actors

        self.cache["project_associations"] = project_associations
        self.save_json("data/project_scheme_associations.json", project_associations)

    # ------------------------- reports and packaging -------------------------

    def finalize_list_view_capture(self) -> None:
        """Resolve the best available List-column source for each selected project.

        Exactness is deliberately conservative. A browser candidate is considered
        project-scoped only when its evidence contains the project key and List/column
        context. Otherwise Jira's supported user-default column order is retained as a
        fallback with scope marked as user-level rather than project-specific.
        """
        browser_sequences = self.cache.get("browser_list_column_sequences") or []
        field_catalog = build_field_catalog(self.cache.get("fields") or [])
        user_columns = normalize_jira_column_items(self.cache.get("list_user_default_columns"))
        system_columns = normalize_jira_column_items(self.cache.get("list_system_default_columns"))
        all_work_current_fields = self.cache.get("all_work_current_fields") or {}
        project_current_fields = self.cache.get("project_list_current_fields") or {}
        filter_columns = self.cache.get("filter_columns") or {}
        boards = self.cache.get("boards") or []
        board_configs = self.cache.get("board_configurations") or {}
        associations = self.cache.get("project_associations") or []

        summary_projects: list[dict[str, Any]] = []
        report_rows: list[dict[str, Any]] = []
        exact_count = 0

        for project in self.selected_projects:
            key = str(project.get("key", "")).strip().upper()
            pid = str(project.get("id", "")).strip()
            if not key:
                continue

            project_sequences = []
            for candidate in browser_sequences:
                evidence = flatten_text(candidate.get("evidence", "")).lower()
                source = str(candidate.get("source", ""))
                score = int(candidate.get("score", 0) or 0)
                scoped = key.lower() in evidence
                list_context = any(token in evidence for token in ("list", "allwork", "all work", "column", "fieldorder", "visiblefield"))
                adjusted = score + (6 if scoped else 0) + (3 if list_context else 0)
                item = dict(candidate)
                item["projectScoped"] = scoped
                item["listContext"] = list_context
                item["projectAdjustedScore"] = adjusted
                item["source"] = source
                project_sequences.append(item)
            project_sequences.sort(key=lambda x: (-int(x.get("projectAdjustedScore", 0)), -len(x.get("columns", []))))

            exact_candidate = next(
                (
                    c for c in project_sequences
                    if c.get("projectScoped") and c.get("listContext") and len(c.get("columns", [])) >= 3
                ),
                None,
            )

            # Board/filter column sources are included for diagnosis, but never confused
            # with the space List columns.
            project_boards: list[dict[str, Any]] = []
            for board in boards:
                if not isinstance(board, dict):
                    continue
                bid = str(board.get("id", ""))
                location = board.get("location") or {}
                config = board_configs.get(bid) or {}
                config_location = config.get("location") or {} if isinstance(config, dict) else {}
                board_project_key = str(
                    location.get("projectKey") or config_location.get("projectKey") or ""
                ).upper()
                board_project_id = str(
                    location.get("projectId") or config_location.get("projectId") or ""
                )
                if board_project_key != key and (not pid or board_project_id != pid):
                    continue
                filter_obj = (config.get("filter") or {}) if isinstance(config, dict) else {}
                fid = str(filter_obj.get("id", "")) if isinstance(filter_obj, dict) else ""
                project_boards.append(
                    {
                        "boardId": bid,
                        "boardName": board.get("name"),
                        "filterId": fid or None,
                        "filterColumns": normalize_jira_column_items(filter_columns.get(fid)),
                    }
                )

            property_candidates: list[dict[str, Any]] = []
            property_sequences: list[dict[str, Any]] = []
            assoc = next(
                (
                    a for a in associations
                    if isinstance(a, dict) and str(a.get("projectKey", "")).upper() == key
                ),
                None,
            )
            if isinstance(assoc, dict) and assoc.get("projectProperties") is not None:
                project_properties = assoc.get("projectProperties")
                property_candidates = extract_named_configuration(
                    project_properties,
                    keywords=("savedview", "saved view", "list", "columns", "fieldorder", "visiblefields", "allwork"),
                )
                raw_property_sequences = find_ordered_field_sequences(
                    project_properties,
                    field_catalog=field_catalog,
                    project_keys=[key],
                    source_label=f"Jira project properties/{key}",
                )
                for candidate in raw_property_sequences:
                    item = dict(candidate)
                    evidence = flatten_text(item.get("evidence", "")).lower()
                    item["projectScoped"] = True  # property endpoint is already scoped to this project
                    item["listContext"] = any(
                        token in evidence
                        for token in ("list", "allwork", "all work", "column", "fieldorder", "visiblefield", "savedview")
                    )
                    item["projectAdjustedScore"] = int(item.get("score", 0) or 0) + 6 + (3 if item["listContext"] else 0)
                    property_sequences.append(item)

            all_exact_candidates = property_sequences + project_sequences
            all_exact_candidates.sort(
                key=lambda x: (-int(x.get("projectAdjustedScore", 0)), -len(x.get("columns", [])))
            )
            exact_candidate = next(
                (
                    c for c in all_exact_candidates
                    if c.get("projectScoped") and c.get("listContext") and len(c.get("columns", [])) >= 3
                ),
                None,
            )

            project_probe = project_current_fields.get(key) if isinstance(project_current_fields, dict) else None
            project_probe_columns = normalize_jira_column_items(
                (project_probe or {}).get("columns") if isinstance(project_probe, dict) else None
            )

            if exact_candidate:
                chosen_source = str(exact_candidate.get("source") or "project_scoped_view_state")
                chosen_scope = (
                    "project-scoped Jira server property"
                    if chosen_source.startswith("Jira project properties/")
                    else "project-specific browser state"
                )
                chosen_columns = exact_candidate.get("columns", [])
                exact = True
                confidence = "high"
                exact_count += 1
            elif project_probe_columns:
                chosen_source = "jira_project_current_fields_probe"
                chosen_scope = "project-JQL CSV current-fields probe; effective display evidence, not a saved-view API"
                chosen_columns = project_probe_columns
                exact = False
                confidence = "server-probe-fallback"
            elif user_columns:
                chosen_source = "jira_user_default_columns"
                chosen_scope = "authenticated-user default; applies when List uses My defaults"
                chosen_columns = user_columns
                exact = False
                confidence = "supported-fallback"
            elif system_columns:
                chosen_source = "jira_system_default_columns"
                chosen_scope = "site system default; not a personal/project-specific List view"
                chosen_columns = system_columns
                exact = False
                confidence = "fallback"
            else:
                chosen_source = None
                chosen_scope = None
                chosen_columns = []
                exact = False
                confidence = "not-captured"

            payload = {
                "projectKey": key,
                "projectId": pid or None,
                "captureStatus": "exact" if exact else ("fallback" if chosen_columns else "not-captured"),
                "exactProjectListColumnOrderCaptured": exact,
                "chosenSource": chosen_source,
                "chosenScope": chosen_scope,
                "confidence": confidence,
                "columns": chosen_columns,
                "userDefaultColumns": user_columns,
                "systemDefaultColumns": system_columns,
                "allWorkCurrentFieldsProbe": all_work_current_fields,
                "projectCurrentFieldsProbe": project_probe,
                "browserCandidates": project_sequences[:50],
                "projectPropertyViewCandidates": property_candidates,
                "projectPropertyColumnSequences": property_sequences,
                "boardFilterColumnSources": project_boards,
                "important": (
                    "Saved-filter columns and Scrum/Kanban board columns are diagnostic sources only and are not "
                    "treated as the space List layout. If exactProjectListColumnOrderCaptured is false, the file "
                    "retains the best supported fallback without pretending it is an exact project saved view. "
                    "v1.9 also records Jira CSV current-fields probes for All Work, each saved filter, and each selected project."
                ),
            }
            rel = Path("data/projects") / safe_filename(key) / "list_view" / "column_capture.json"
            self.save_json(rel, payload)
            summary_projects.append(
                {
                    "projectKey": key,
                    "status": payload["captureStatus"],
                    "exact": exact,
                    "source": chosen_source,
                    "scope": chosen_scope,
                    "columnCount": len(chosen_columns),
                    "output": rel.as_posix(),
                }
            )
            for position, column in enumerate(chosen_columns, 1):
                report_rows.append(
                    {
                        "project_key": key,
                        "position": position,
                        "label": column.get("label"),
                        "value": column.get("value"),
                        "source": chosen_source,
                        "scope": chosen_scope,
                        "exact_project_list": exact,
                    }
                )

            if not exact:
                self.manual_actions.append(
                    f"{key}: exact project/space List column order was not proven by browser state. "
                    f"Exporter retained {chosen_source or 'no fallback'} instead. Open {key} List in Firefox, "
                    "arrange the desired columns, keep that tab open, and re-run v1.9 on the same OS user. "
                    "If Jira is using a shared saved view, its server-side view model may still require a tenant-specific internal endpoint."
                )

        self.cache["list_view_capture_summary"] = summary_projects
        self.save_json(
            "data/list_views/capture_summary.json",
            {
                "sourceSite": self.client.site_url,
                "selectedProjects": summary_projects,
                "exactProjectCaptures": exact_count,
                "userDefaultColumns": user_columns,
                "systemDefaultColumns": system_columns,
                "allWorkCurrentFieldsProbe": all_work_current_fields,
                "definitionOfExact": (
                    "An ordered Jira field sequence was found in Firefox Jira-origin browser state with both the "
                    "selected project key and List/column context in its evidence."
                ),
            },
        )
        path = self.report_dir / "list_view_columns.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["project_key", "position", "label", "value", "source", "scope", "exact_project_list"],
            )
            writer.writeheader()
            writer.writerows(report_rows)
        self.operations.append(
            OperationRecord(
                name="Project/space List-view column resolution",
                status="success" if summary_projects else "unsupported_or_inaccessible",
                endpoint="COMBINED Jira user/system columns + CSV current-fields probes + Firefox state + exported project properties",
                item_count=len(summary_projects),
                output_file="data/list_views/capture_summary.json",
                note=f"Exact project List column order proven for {exact_count}/{len(summary_projects)} selected projects.",
            )
        )

    def build_reports(self) -> None:
        self.build_transition_rules_report()
        self.build_project_matrix_report()
        self.build_field_report()
        self.build_board_report()
        self.build_epic_report()

    def build_epic_report(self) -> None:
        path = self.report_dir / "epics.csv"
        rows: list[dict[str, Any]] = []
        for issue in self.cache.get("epics", []) or []:
            if not isinstance(issue, dict):
                continue
            fields = issue.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {}
            project = fields.get("project") or {}
            issue_type = fields.get("issuetype") or {}
            status = fields.get("status") or {}
            assignee = fields.get("assignee") or {}
            reporter = fields.get("reporter") or {}
            priority = fields.get("priority") or {}
            parent = fields.get("parent") or {}
            components = fields.get("components") or []
            versions = fields.get("fixVersions") or []
            labels = fields.get("labels") or []

            rows.append(
                {
                    "issue_id": issue.get("id", ""),
                    "key": issue.get("key", ""),
                    "project_key": project.get("key", "") if isinstance(project, dict) else "",
                    "project_name": project.get("name", "") if isinstance(project, dict) else "",
                    "issue_type_id": issue_type.get("id", "") if isinstance(issue_type, dict) else "",
                    "issue_type_name": issue_type.get("name", "") if isinstance(issue_type, dict) else "",
                    "summary": fields.get("summary", ""),
                    "status_id": status.get("id", "") if isinstance(status, dict) else "",
                    "status_name": status.get("name", "") if isinstance(status, dict) else "",
                    "assignee_account_id": assignee.get("accountId", "") if isinstance(assignee, dict) else "",
                    "assignee_display_name": assignee.get("displayName", "") if isinstance(assignee, dict) else "",
                    "reporter_account_id": reporter.get("accountId", "") if isinstance(reporter, dict) else "",
                    "reporter_display_name": reporter.get("displayName", "") if isinstance(reporter, dict) else "",
                    "priority": priority.get("name", "") if isinstance(priority, dict) else "",
                    "parent_key": parent.get("key", "") if isinstance(parent, dict) else "",
                    "components": " | ".join(
                        str(component.get("name", ""))
                        for component in components
                        if isinstance(component, dict)
                    ),
                    "fix_versions": " | ".join(
                        str(version.get("name", ""))
                        for version in versions
                        if isinstance(version, dict)
                    ),
                    "labels": " | ".join(str(label) for label in labels) if isinstance(labels, list) else "",
                    "created": fields.get("created", ""),
                    "updated": fields.get("updated", ""),
                }
            )

        write_csv(
            path,
            rows,
            fieldnames=[
                "issue_id",
                "key",
                "project_key",
                "project_name",
                "issue_type_id",
                "issue_type_name",
                "summary",
                "status_id",
                "status_name",
                "assignee_account_id",
                "assignee_display_name",
                "reporter_account_id",
                "reporter_display_name",
                "priority",
                "parent_key",
                "components",
                "fix_versions",
                "labels",
                "created",
                "updated",
            ],
        )

    def build_transition_rules_report(self) -> None:
        path = self.report_dir / "workflow_transition_rules.csv"
        rows: list[dict[str, Any]] = []

        for workflow in self.cache.get("workflows", []) or []:
            workflow_id = workflow.get("id", {}) if isinstance(workflow, dict) else {}
            workflow_name = (
                workflow_id.get("name")
                if isinstance(workflow_id, dict)
                else workflow.get("name", "")
            )
            entity_id = workflow_id.get("entityId", "") if isinstance(workflow_id, dict) else ""
            status_by_id = {
                str(status.get("id")): status.get("name", "")
                for status in workflow.get("statuses", [])
                if isinstance(status, dict)
            }
            for transition in workflow.get("transitions", []) or []:
                rules = transition.get("rules") or {}
                from_ids = transition.get("from") or []
                if not isinstance(from_ids, list):
                    from_ids = [from_ids]
                from_names = [status_by_id.get(str(sid), str(sid)) for sid in from_ids]
                to_id = str(transition.get("to", ""))
                screen = transition.get("screen") or {}
                rows.append(
                    {
                        "workflow_name": workflow_name,
                        "workflow_entity_id": entity_id,
                        "workflow_active": workflow.get("isActive", ""),
                        "transition_id": transition.get("id", ""),
                        "transition_name": transition.get("name", ""),
                        "transition_type": transition.get("type", ""),
                        "from_status_ids": json_compact(from_ids),
                        "from_status_names": " | ".join(from_names),
                        "to_status_id": to_id,
                        "to_status_name": status_by_id.get(to_id, to_id),
                        "screen_id": screen.get("id", "") if isinstance(screen, dict) else "",
                        "screen_name": screen.get("name", "") if isinstance(screen, dict) else "",
                        "conditions_tree": json_compact(rules.get("conditionsTree")),
                        "validators": json_compact(rules.get("validators")),
                        "post_functions": json_compact(rules.get("postFunctions")),
                        "transition_properties": json_compact(transition.get("properties")),
                    }
                )

        write_csv(path, rows, fieldnames=[
            "workflow_name",
            "workflow_entity_id",
            "workflow_active",
            "transition_id",
            "transition_name",
            "transition_type",
            "from_status_ids",
            "from_status_names",
            "to_status_id",
            "to_status_name",
            "screen_id",
            "screen_name",
            "conditions_tree",
            "validators",
            "post_functions",
            "transition_properties",
        ])

    def build_project_matrix_report(self) -> None:
        path = self.report_dir / "project_configuration_matrix.csv"
        rows: list[dict[str, Any]] = []
        for assoc in self.cache.get("project_associations", []) or []:
            project = assoc.get("project") if isinstance(assoc.get("project"), dict) else {}
            rows.append(
                {
                    "project_key": assoc.get("projectKey", ""),
                    "project_id": assoc.get("projectId", ""),
                    "project_name": project.get("name", ""),
                    "project_type": project.get("projectTypeKey", ""),
                    "simplified_team_managed": project.get("simplified", ""),
                    "lead_account_id": nested_get(project, "lead", "accountId"),
                    "lead_display_name": nested_get(project, "lead", "displayName"),
                    "issue_type_scheme": summarize_object(assoc.get("issueTypeScheme")),
                    "issue_type_screen_scheme": summarize_object(assoc.get("issueTypeScreenScheme")),
                    "field_configuration_scheme": summarize_object(assoc.get("fieldConfigurationScheme")),
                    "workflow_scheme": summarize_object(assoc.get("workflowScheme")),
                    "permission_scheme": summarize_object(assoc.get("permissionScheme")),
                    "notification_scheme": summarize_object(assoc.get("notificationScheme")),
                    "issue_security_scheme": summarize_object(assoc.get("issueSecurityScheme")),
                }
            )
        write_csv(path, rows, fieldnames=[
            "project_key",
            "project_id",
            "project_name",
            "project_type",
            "simplified_team_managed",
            "lead_account_id",
            "lead_display_name",
            "issue_type_scheme",
            "issue_type_screen_scheme",
            "field_configuration_scheme",
            "workflow_scheme",
            "permission_scheme",
            "notification_scheme",
            "issue_security_scheme",
        ])

    def build_field_report(self) -> None:
        path = self.report_dir / "fields.csv"
        rows: list[dict[str, Any]] = []
        contexts_by_field = self.cache.get("field_contexts", {}) or {}
        grouped_defaults_by_field = self.cache.get("field_default_values_grouped", {}) or {}
        legacy_defaults_by_field = self.cache.get("field_default_values_legacy", {}) or {}
        for field in self.cache.get("fields", []) or []:
            field_id = str(field.get("id", ""))
            schema = field.get("schema") or {}
            rows.append(
                {
                    "field_id": field_id,
                    "field_key": field.get("key", ""),
                    "name": field.get("name", ""),
                    "custom": field.get("custom", ""),
                    "locked": field.get("isLocked", ""),
                    "orderable": field.get("orderable", ""),
                    "navigable": field.get("navigable", ""),
                    "searchable": field.get("searchable", ""),
                    "schema_type": schema.get("type", ""),
                    "schema_items": schema.get("items", ""),
                    "schema_custom": schema.get("custom", ""),
                    "schema_custom_id": schema.get("customId", ""),
                    "searcher_key": field.get("searcherKey", ""),
                    "contexts_count": len(contexts_by_field.get(field_id, [])),
                    "default_values_grouped_count": count_default_value_entries(grouped_defaults_by_field.get(field_id)),
                    "default_values_legacy_count": len(legacy_defaults_by_field.get(field_id, []) or []),
                    "screens_count": field.get("screensCount", ""),
                    "last_used": json_compact(field.get("lastUsed")),
                }
            )
        write_csv(path, rows, fieldnames=[
            "field_id",
            "field_key",
            "name",
            "custom",
            "locked",
            "orderable",
            "navigable",
            "searchable",
            "schema_type",
            "schema_items",
            "schema_custom",
            "schema_custom_id",
            "searcher_key",
            "contexts_count",
            "default_values_grouped_count",
            "default_values_legacy_count",
            "screens_count",
            "last_used",
        ])

    def build_board_report(self) -> None:
        path = self.report_dir / "boards.csv"
        rows: list[dict[str, Any]] = []
        configs = self.cache.get("board_configurations", {}) or {}
        edit_models = self.cache.get("board_edit_models", {}) or {}
        for board in self.cache.get("boards", []) or []:
            board_id = str(board.get("id", ""))
            config = configs.get(board_id) or {}
            filter_info = config.get("filter") or {}
            location = board.get("location") or config.get("location") or {}
            edit_model = edit_models.get(board_id)
            timeline_candidates = (
                extract_named_configuration(
                    edit_model,
                    keywords=(
                        "timeline",
                        "roadmap",
                        "childissues",
                        "childissue",
                        "childlevel",
                        "schedule",
                        "rollup",
                    ),
                )
                if edit_model is not None
                else []
            )
            rows.append(
                {
                    "board_id": board_id,
                    "name": board.get("name", ""),
                    "type": board.get("type", ""),
                    "location_type": location.get("type", "") if isinstance(location, dict) else "",
                    "location_project_id": location.get("projectId", "") if isinstance(location, dict) else "",
                    "location_project_key": location.get("projectKey", "") if isinstance(location, dict) else "",
                    "filter_id": filter_info.get("id", "") if isinstance(filter_info, dict) else "",
                    "filter_name": filter_info.get("name", "") if isinstance(filter_info, dict) else "",
                    "column_config": json_compact(config.get("columnConfig")),
                    "estimation": json_compact(config.get("estimation")),
                    "ranking": json_compact(config.get("ranking")),
                    "board_edit_model_captured": bool(edit_model is not None),
                    "timeline_candidate_count": len(timeline_candidates),
                }
            )
        write_csv(path, rows, fieldnames=[
            "board_id",
            "name",
            "type",
            "location_type",
            "location_project_id",
            "location_project_key",
            "filter_id",
            "filter_name",
            "column_config",
            "estimation",
            "ranking",
            "board_edit_model_captured",
            "timeline_candidate_count",
        ])

    def write_manifest(self) -> None:
        status_counts: dict[str, int] = {}
        for operation in self.operations:
            status_counts[operation.status] = status_counts.get(operation.status, 0) + 1

        manifest = {
            "exporter": {"name": APP_NAME, "version": APP_VERSION},
            "exportedAtUtc": datetime.now(timezone.utc).isoformat(),
            "sourceSite": self.client.site_url,
            "sourceServer": self.cache.get("server_info"),
            "authenticatedAccount": sanitize_account(self.cache.get("myself")),
            "selectedProjectKeys": [p.get("key") for p in self.selected_projects],
            "settings": {
                "includeUsers": self.settings.include_users,
                "includeGroupMembers": self.settings.include_group_members,
                "includeProjectProperties": self.settings.include_project_properties,
                "includeEpics": self.settings.include_epics,
                "includeBrowserViewState": self.settings.include_browser_view_state,
                "firefoxProfileOverride": str(self.settings.firefox_profile) if self.settings.firefox_profile else None,
            },
            "operationSummary": status_counts,
            "operations": [asdict(operation) for operation in self.operations],
            "securityNotice": (
                "This export can contain project names, user account IDs, email addresses, JQL, "
                "workflow rules, permission grants, exported Epic work-item fields, browser-local Jira view preferences (when captured), "
                "and other sensitive configuration. Store it securely."
            ),
        }
        self.save_json("manifest.json", manifest)

    def write_manual_actions(self) -> None:
        defaults = [
            "Private filters that are not visible to the authenticated account cannot be exported by the filter search API.",
            "Board creation is exposed, but not every board setting has an equivalent public write API. The exporter now saves Jira's internal read-only Board settings edit model when available, but the importer must still flag settings that Jira does not expose through a supported write API.",
            "Jira's project/space List view can include personal browser-local display state, and newer Jira Cloud experiences also support admin-saved shared views. v1.5 snapshots Firefox localStorage as a fallback for the displayed personal state; shared saved-view configuration is not exposed by the documented board REST API and may still require a tenant-specific internal endpoint for exact recreation.",
            "Marketplace-app workflow validators, conditions, triggers, or post-functions may require the same app and app-specific configuration on the destination site.",
            "Users cannot be cloned as Jira accounts by this exporter. Destination users must be invited/provisioned, then mapped by account ID/email/display name where available.",
            "Passwords, API tokens, OAuth secrets, app licences, billing, organization policies, domains, and product-access settings are intentionally not exported.",
            "Team-managed project configuration is not represented by the same global schemes used by company-managed projects, so some settings may require manual recreation.",
            "The destination site may assign different IDs to every field, field context, field option, status, workflow, screen, scheme, filter, board, component, and version. The importer must remap references.",
            "Default values that reference users, groups, projects, or versions require destination mappings. The importer should report any value it cannot map safely.",
        ]
        lines = [
            "# Manual actions and known Jira Cloud limitations",
            "",
            "The exporter is deliberately tolerant: unsupported or inaccessible endpoints are written to `data/errors/` and listed in `reports/endpoint_status.csv`.",
            "",
        ]
        for item in deduplicate(defaults + self.manual_actions):
            lines.append(f"- {item}")
        lines.append("")
        (self.root / "manual_actions.md").write_text("\n".join(lines), encoding="utf-8")

    def write_operation_report(self) -> None:
        path = self.report_dir / "endpoint_status.csv"
        rows = [asdict(record) for record in self.operations]
        write_csv(
            path,
            rows,
            fieldnames=[
                "name",
                "status",
                "endpoint",
                "item_count",
                "http_status",
                "output_file",
                "note",
                "elapsed_seconds",
            ],
        )

    def create_zip(self) -> Path:
        zip_path = self.root.with_suffix(".zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(self.root.rglob("*")):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(self.root.parent))
        return zip_path


# ------------------------- stand-alone helpers -------------------------


def normalize_site_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Jira site URL is required")
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        value = "https://" + value
    return value


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    return cleaned[:180] or "unnamed"


def extract_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            messages: list[str] = []
            if payload.get("errorMessages"):
                messages.extend(str(item) for item in payload["errorMessages"])
            errors = payload.get("errors")
            if isinstance(errors, dict):
                messages.extend(f"{key}: {value}" for key, value in errors.items())
            if payload.get("message"):
                messages.append(str(payload["message"]))
            if messages:
                return "; ".join(messages)
    except ValueError:
        pass
    text = response.text.strip()
    return text[:500] if text else response.reason or "Request failed"


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def item_count(payload: Any) -> int | None:
    if payload is None:
        return 0
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in (
            "values",
            "projects",
            "permissionSchemes",
            "issueSecuritySchemes",
            "notificationSchemes",
            "boards",
            "keys",
            "issues",
        ):
            if isinstance(payload.get(key), list):
                return len(payload[key])
        return len(payload)
    return 1


def unwrap_list(payload: Any, preferred_keys: Sequence[str] = ("values",)) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in tuple(preferred_keys) + (
            "permissionSchemes",
            "notificationSchemes",
            "issueSecuritySchemes",
            "projects",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def chunks(values: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def field_supports_options(field: Mapping[str, Any]) -> bool:
    if field.get("areOptionsSupported") is True:
        return True
    custom_type = str((field.get("schema") or {}).get("custom", "")).lower()
    markers = (
        "select",
        "multiselect",
        "radiobuttons",
        "multicheckboxes",
        "cascadingselect",
        "checkboxes",
    )
    return any(marker in custom_type for marker in markers)


def count_default_value_entries(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    total = 0
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("defaultValues"), list):
            total += len(item.get("defaultValues") or [])
        else:
            total += 1
    return total


def jql_value(value: Any) -> str:
    """Quote a literal safely for use as a JQL string value."""
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def json_compact(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def nested_get(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return "" if current is None else current


def summarize_object(payload: Any) -> str:
    if payload in (None, "", [], {}):
        return ""
    if isinstance(payload, dict):
        for key in ("name", "id", "schemeId"):
            if payload.get(key) not in (None, ""):
                name = payload.get("name")
                identifier = payload.get("id") or payload.get("schemeId")
                if name and identifier:
                    return f"{name} (ID {identifier})"
                return str(name or identifier)
        for container in (
            "values",
            "permissionSchemes",
            "notificationSchemes",
            "issueSecuritySchemes",
        ):
            values = payload.get(container)
            if isinstance(values, list) and values:
                return " | ".join(summarize_object(item) for item in values)
    if isinstance(payload, list):
        return " | ".join(summarize_object(item) for item in payload)
    return json_compact(payload)


def deduplicate(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def sanitize_account(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    allowed = {
        "accountId",
        "accountType",
        "displayName",
        "emailAddress",
        "active",
        "timeZone",
        "locale",
    }
    return {key: payload.get(key) for key in allowed if key in payload}


def paginate_array_endpoint(
    client: JiraClient,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    page_size: int = 100,
) -> list[Any]:
    all_items: list[Any] = []
    start_at = 0
    for _ in range(10000):
        query = dict(params or {})
        query.update({"startAt": start_at, "maxResults": page_size})
        payload = client.get(path, params=query)
        if not isinstance(payload, list):
            if payload is not None:
                all_items.append(payload)
            break
        all_items.extend(payload)
        if len(payload) < page_size:
            break
        start_at += len(payload)
    return all_items


def export_entity_properties(client: JiraClient, base_path: str) -> dict[str, Any]:
    keys_payload = client.get(base_path)
    result: dict[str, Any] = {"keys": keys_payload, "values": {}}
    keys = keys_payload.get("keys", []) if isinstance(keys_payload, dict) else []
    for item in keys:
        key = item.get("key") if isinstance(item, dict) else item
        if not key:
            continue
        try:
            result["values"][str(key)] = client.get(f"{base_path}/{key}")
        except JiraApiError as exc:
            result["values"][str(key)] = {
                "error": exc.message,
                "httpStatus": exc.status_code,
            }
    return result


def export_project_role_actors(client: JiraClient, project_key: str) -> dict[str, Any]:
    role_map = client.get(f"/rest/api/3/project/{project_key}/role")
    result: dict[str, Any] = {"roleUrls": role_map, "roles": {}}
    if not isinstance(role_map, dict):
        return result
    for role_name, role_url in role_map.items():
        try:
            result["roles"][role_name] = client.get(str(role_url))
        except JiraApiError as exc:
            result["roles"][role_name] = {
                "error": exc.message,
                "httpStatus": exc.status_code,
            }
    return result


def site_hostname(site_url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(site_url).hostname or "").lower()


def extract_named_configuration(payload: Any, *, keywords: Sequence[str]) -> list[dict[str, Any]]:
    """Return path/value pairs whose dictionary key contains a target keyword."""
    normalized = tuple(k.lower().replace("-", "").replace("_", "") for k in keywords)
    matches: list[dict[str, Any]] = []

    def walk(value: Any, path: list[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                compact = key_text.lower().replace("-", "").replace("_", "")
                child_path = path + [key_text]
                if any(token in compact for token in normalized):
                    matches.append({"path": ".".join(child_path), "value": child})
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + [f"[{index}]"])

    walk(payload, [])
    return matches


def find_firefox_profiles(explicit: Path | None = None) -> list[Path]:
    if explicit is not None:
        return [explicit] if explicit.exists() else []

    roots: list[Path] = []
    appdata = os.getenv("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Mozilla" / "Firefox" / "Profiles")
    roots.extend(
        [
            Path.home() / ".mozilla" / "firefox",
            Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles",
        ]
    )

    profiles: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue
            if not (candidate / "storage").exists():
                continue
            marker = str(candidate.resolve()).lower()
            if marker not in seen:
                seen.add(marker)
                profiles.append(candidate)
    return profiles


def find_firefox_origin_storage(profile: Path, host: str) -> list[Path]:
    default_storage = profile / "storage" / "default"
    if not default_storage.exists():
        return []
    prefix = f"https+++{host}".lower()
    result: list[Path] = []
    for candidate in default_storage.iterdir():
        if candidate.is_dir() and candidate.name.lower().startswith(prefix):
            result.append(candidate)
    return result


def decode_sqlite_value(value: Any) -> dict[str, Any]:
    if value is None or isinstance(value, (int, float, str)):
        return {"value": value}
    if isinstance(value, bytes):
        decoded: str | None = None
        encoding: str | None = None
        for candidate_encoding in ("utf-8", "utf-16-le", "utf-16"):
            try:
                candidate = value.decode(candidate_encoding)
                if candidate and sum(ch.isprintable() or ch in "\r\n\t" for ch in candidate) / len(candidate) >= 0.90:
                    decoded = candidate
                    encoding = candidate_encoding
                    break
            except (UnicodeDecodeError, ZeroDivisionError):
                continue
        result: dict[str, Any] = {
            "base64": base64.b64encode(value).decode("ascii"),
            "byteLength": len(value),
        }
        if decoded is not None:
            result["decoded"] = decoded
            result["encoding"] = encoding
        return result
    return {"value": str(value)}


def backup_sqlite_database(source: Path, destination: Path) -> None:
    """Create a consistent read-only SQLite snapshot, including committed WAL data."""
    source_uri = f"file:{source.as_posix()}?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True, timeout=2)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def dump_firefox_localstorage(db_path: Path) -> dict[str, Any]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        result: dict[str, Any] = {"database": db_path.name, "tables": {}}
        for (table_name,) in table_rows:
            if not re.match(r"^[A-Za-z0-9_]+$", str(table_name)):
                continue
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
            entries = []
            for row in conn.execute(f"SELECT * FROM {table_name}").fetchall():
                entry: dict[str, Any] = {}
                for column, value in zip(columns, row):
                    entry[str(column)] = decode_sqlite_value(value)
                entries.append(entry)
            result["tables"][str(table_name)] = {
                "columns": columns,
                "rows": entries,
            }
        return result
    finally:
        conn.close()



def dump_sqlite_database(db_path: Path) -> dict[str, Any]:
    """Generic read-only SQLite dump used for Firefox localStorage/IndexedDB."""
    return dump_firefox_localstorage(db_path)


def copy_tree_best_effort(source: Path, destination: Path) -> None:
    """Copy a browser-origin folder without failing merely because one file is locked."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        dest = destination / rel
        try:
            if path.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        except OSError:
            # A consistent SQLite snapshot is attempted separately for *.sqlite files.
            continue


def normalize_jira_column_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize Jira ColumnItem arrays while preserving the server-returned order."""
    if not isinstance(payload, list):
        return []
    result: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            value = item.get("value") or item.get("id") or item.get("key")
            label = item.get("label") or item.get("name") or value
            if value is None and label is None:
                continue
            result.append({"label": label, "value": value, "raw": item})
        elif item is not None:
            result.append({"label": str(item), "value": str(item), "raw": item})
    return result


def columns_from_csv_headers(
    headers: Sequence[str], field_catalog: Mapping[str, Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve CSV current-fields headers to Jira field identifiers, preserving order."""
    columns: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen: set[str] = set()

    aliases = {
        "issue key": "issuekey",
        "work item key": "issuekey",
        "key": "issuekey",
        "issue type": "issuetype",
        "work type": "issuetype",
        "type": "issuetype",
        "fix version/s": "fixVersions",
        "fix versions": "fixVersions",
        "due date": "duedate",
        "start date": "startdate",
        "work": "work",
        "work item": "work",
    }

    for raw_header in headers:
        raw = str(raw_header or "").strip().lstrip("\ufeff")
        if not raw:
            continue
        variants = [raw]
        match = re.match(r"^(?:custom field|customfield)\s*\((.+)\)$", raw, flags=re.I)
        if match:
            variants.insert(0, match.group(1).strip())
        normalized_alias = aliases.get(raw.casefold())
        if normalized_alias:
            variants.insert(0, normalized_alias)

        resolved = None
        for variant in variants:
            resolved = _column_from_token(variant, field_catalog)
            if resolved:
                break
        if not resolved:
            unresolved.append(raw)
            continue
        value = str(resolved.get("value") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        columns.append({
            "position": len(columns) + 1,
            "label": resolved.get("label") or raw,
            "value": value,
            "csvHeader": raw,
            "raw": resolved.get("raw", raw),
        })
    return columns, unresolved


def _canonical_field_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(value or "").strip().lower())


def build_field_catalog(fields: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build aliases for Jira system/custom fields and new-List fixed columns."""
    catalog: dict[str, dict[str, Any]] = {}

    def add(alias: Any, value: str, label: str) -> None:
        key = _canonical_field_key(alias)
        if key:
            catalog.setdefault(key, {"value": value, "label": label})

    for field in fields:
        if not isinstance(field, Mapping):
            continue
        fid = str(field.get("id") or "").strip()
        fkey = str(field.get("key") or "").strip()
        name = str(field.get("name") or "").strip()
        canonical = fid or fkey or name
        if not canonical:
            continue
        label = name or fkey or fid
        for alias in (fid, fkey, name, (field.get("schema") or {}).get("system")):
            add(alias, canonical, label)

    # New Jira List has a fixed composite "Work" column in some rollouts, while
    # older/table APIs expose issue key/type/summary separately. Keep aliases so
    # browser state using either vocabulary can still be recognized.
    extras = {
        "work": ("work", "Work"),
        "workitem": ("work", "Work"),
        "workitems": ("work", "Work"),
        "issuekey": ("issuekey", "Key"),
        "key": ("issuekey", "Key"),
        "issuetype": ("issuetype", "Type"),
        "worktype": ("issuetype", "Type"),
        "summary": ("summary", "Summary"),
        "status": ("status", "Status"),
        "parent": ("parent", "Parent"),
        "assignee": ("assignee", "Assignee"),
        "reporter": ("reporter", "Reporter"),
        "priority": ("priority", "Priority"),
        "labels": ("labels", "Labels"),
        "component": ("components", "Components"),
        "components": ("components", "Components"),
        "fixversion": ("fixVersions", "Fix versions"),
        "fixversions": ("fixVersions", "Fix versions"),
        "duedate": ("duedate", "Due date"),
        "startdate": ("startdate", "Start date"),
        "created": ("created", "Created"),
        "updated": ("updated", "Updated"),
        "sprint": ("sprint", "Sprint"),
        "storypoints": ("storypoints", "Story points"),
    }
    for alias, (value, label) in extras.items():
        add(alias, value, label)
    return catalog


def _column_from_token(token: Any, field_catalog: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    if token is None:
        return None
    if isinstance(token, Mapping):
        for key in ("fieldId", "fieldID", "fieldKey", "field", "id", "key", "value", "name", "label"):
            if key in token:
                found = _column_from_token(token.get(key), field_catalog)
                if found:
                    return {**found, "raw": token}
        return None
    if not isinstance(token, (str, int)):
        return None
    raw = str(token).strip()
    if not raw or len(raw) > 200:
        return None
    # Strip common wrappers used by front-end state keys.
    variants = [raw]
    variants.extend(part for part in re.split(r"[.:/|]", raw) if part)
    for variant in reversed(variants):
        key = _canonical_field_key(variant)
        if key in field_catalog:
            item = dict(field_catalog[key])
            item["raw"] = token
            return item
        # Jira custom field ids are stable enough to recognize even if field metadata
        # was inaccessible for that field.
        match = re.search(r"customfield[_-]?(\d+)", variant, re.I)
        if match:
            value = f"customfield_{match.group(1)}"
            return {"value": value, "label": value, "raw": token}
    return None


def _sequence_from_list(items: Sequence[Any], field_catalog: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        column = _column_from_token(item, field_catalog)
        if not column:
            continue
        value = str(column.get("value") or "")
        if not value or value in seen:
            continue
        seen.add(value)
        columns.append(column)
    return columns


def _safe_json_parse(text: str) -> Any | None:
    text = text.strip()
    if len(text) < 2 or len(text) > 2_000_000:
        return None
    if not ((text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]"))):
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def find_ordered_field_sequences(
    payload: Any,
    *,
    field_catalog: Mapping[str, Mapping[str, Any]],
    project_keys: Sequence[str],
    source_label: str,
) -> list[dict[str, Any]]:
    """Find ordered lists that plausibly represent Jira List-view fields/columns."""
    candidates: list[dict[str, Any]] = []
    project_needles = [str(key).lower() for key in project_keys if key]
    context_words = ("column", "columns", "fieldorder", "field order", "visiblefield", "visible field", "listview", "list view", "allwork", "all work")

    def add_candidate(items: Sequence[Any], path: list[str], evidence_value: Any) -> None:
        if len(items) < 3 or len(items) > 100:
            return
        columns = _sequence_from_list(items, field_catalog)
        if len(columns) < 3:
            return
        # Require a meaningful proportion of the list to be field-like when the raw
        # list consists of simple values. This suppresses arbitrary Jira data arrays.
        recognized_ratio = len(columns) / max(len(items), 1)
        if recognized_ratio < 0.5 and len(columns) < 5:
            return
        path_text = ".".join(path)
        evidence_text = flatten_text(evidence_value)
        combined = f"{path_text} {evidence_text}"[:20000]
        lower = combined.lower()
        score = len(columns)
        if any(word in path_text.lower() for word in context_words):
            score += 8
        elif any(word in lower for word in context_words):
            score += 4
        if any(key in lower for key in project_needles):
            score += 5
        if "list" in lower or "allwork" in lower or "all work" in lower:
            score += 3
        if "column" in lower or "visiblefield" in lower or "fieldorder" in lower:
            score += 3
        candidates.append(
            {
                "source": source_label,
                "path": path_text,
                "score": score,
                "columns": [
                    {"position": i + 1, "label": c.get("label"), "value": c.get("value"), "raw": c.get("raw")}
                    for i, c in enumerate(columns)
                ],
                "evidence": combined[:4000],
            }
        )

    def walk(value: Any, path: list[str], depth: int = 0) -> None:
        if depth > 18:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                skey = str(key)
                if skey.lower() == "base64":
                    continue
                child_path = path + [skey]
                if isinstance(child, list):
                    add_candidate(child, child_path, value)
                walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            add_candidate(value, path, value)
            for index, child in enumerate(value[:500]):
                walk(child, path + [f"[{index}]"], depth + 1)
        elif isinstance(value, str):
            decoded = _safe_json_parse(value)
            if decoded is not None:
                walk(decoded, path + ["<json>"], depth + 1)

    walk(payload, [])
    return dedupe_column_sequences(candidates)


def dedupe_column_sequences(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, ...], dict[str, Any]] = {}
    for candidate in candidates:
        columns = candidate.get("columns") or []
        signature = tuple(str(c.get("value")) for c in columns if isinstance(c, Mapping) and c.get("value"))
        if len(signature) < 3:
            continue
        item = dict(candidate)
        current = best.get(signature)
        if current is None or int(item.get("score", 0)) > int(current.get("score", 0)):
            best[signature] = item
    result = list(best.values())
    result.sort(key=lambda x: (-int(x.get("score", 0)), -len(x.get("columns", [])), str(x.get("source", ""))))
    return result[:500]


def _read_mozlz4_json(path: Path) -> Any:
    data = path.read_bytes()
    header = b"mozLz40\x00"
    if not data.startswith(header):
        return json.loads(data.decode("utf-8"))
    payload = data[len(header):]
    try:
        import lz4.block  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Python package 'lz4' is required to inspect Firefox sessionStorage") from exc

    errors: list[Exception] = []
    # Some lz4 blocks embed their original size; Mozilla blocks commonly do not.
    try:
        raw = lz4.block.decompress(payload)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    for size in (1 << 20, 4 << 20, 16 << 20, 64 << 20, 256 << 20):
        try:
            raw = lz4.block.decompress(payload, uncompressed_size=size)
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    raise RuntimeError(f"Could not decompress Firefox jsonlz4 session file: {errors[-1]}")


def extract_firefox_jira_session_storage(profile: Path, host: str) -> dict[str, Any]:
    """Return only Jira-origin sessionStorage fragments from Firefox session restore."""
    candidates: list[Path] = []
    root_file = profile / "sessionstore.jsonlz4"
    if root_file.exists():
        candidates.append(root_file)
    backup_dir = profile / "sessionstore-backups"
    if backup_dir.exists():
        candidates.extend(path for path in backup_dir.iterdir() if path.is_file() and path.suffix == ".jsonlz4")
    candidates = sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True)

    matches: list[dict[str, Any]] = []
    host_lower = host.lower()
    for path in candidates[:8]:
        try:
            payload = _read_mozlz4_json(path)
        except Exception:
            continue

        def walk(value: Any, trail: list[str]) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    key_text = str(key)
                    lower_key = key_text.lower()
                    # Firefox sessionstore commonly uses origin URLs as keys inside
                    # a storage object. Retain only the Jira origin value.
                    if host_lower in lower_key:
                        matches.append(
                            {
                                "sessionFile": path.name,
                                "path": ".".join(trail + [key_text]),
                                "originKey": key_text,
                                "value": child,
                            }
                        )
                    else:
                        walk(child, trail + [key_text])
            elif isinstance(value, list):
                for idx, child in enumerate(value[:1000]):
                    walk(child, trail + [f"[{idx}]"])
            elif isinstance(value, str) and host_lower in value.lower():
                # URLs alone are not useful List state, so only keep nearby values
                # when the path itself suggests storage/session data.
                if any(token in ".".join(trail).lower() for token in ("storage", "session")):
                    matches.append(
                        {
                            "sessionFile": path.name,
                            "path": ".".join(trail),
                            "value": value,
                        }
                    )

        walk(payload, [])
        if matches:
            # The newest session file with Jira-origin storage is sufficient.
            break
    return {"host": host, "matches": matches}


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{k} {flatten_text(v)}" for k, v in value.items())
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    return str(value)


def find_list_view_candidates(
    dump: Mapping[str, Any], *, project_keys: Sequence[str], board_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Heuristic shortlist of localStorage entries likely tied to Jira List view.

    The full raw localStorage dump is always retained because Atlassian can rename
    browser-storage keys without notice.
    """
    needles = [
        "listview",
        "list view",
        "column",
        "columns",
        "fieldorder",
        "field order",
        "issue-list",
        "issuelist",
    ]
    needles.extend(str(v).lower() for v in project_keys if v)
    needles.extend(str(v).lower() for v in board_ids if v)

    matches: list[dict[str, Any]] = []
    tables = dump.get("tables", {}) if isinstance(dump, Mapping) else {}
    if not isinstance(tables, Mapping):
        return matches
    for table_name, table in tables.items():
        rows = table.get("rows", []) if isinstance(table, Mapping) else []
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            text = flatten_text(row).lower()
            explicit = any(marker in text for marker in needles[:8])
            scoped = any(marker and marker in text for marker in needles[8:])
            structured = any(token in text for token in ("{", "[", "field", "view"))
            if explicit or (scoped and structured):
                matches.append({"table": table_name, "rowIndex": index, "row": row})
    return matches


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_dotenv_file(path: Path) -> None:
    """Tiny .env loader to keep requests as the only external dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Jira Cloud site configuration into a portable JSON/CSV package."
    )
    parser.add_argument("--site", help="Source Jira URL, e.g. https://example.atlassian.net")
    parser.add_argument("--email", help="Atlassian account email used with the API token")
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Limit export to a project/space key. Repeat for multiple keys. Default: all visible projects.",
    )
    parser.add_argument(
        "--output",
        default=".",
        help="Parent folder for the export package. Default: current directory.",
    )
    parser.add_argument(
        "--skip-users",
        action="store_true",
        help="Do not export the Jira user-search result.",
    )
    parser.add_argument(
        "--skip-group-members",
        action="store_true",
        help="Export group names but not membership lists.",
    )
    parser.add_argument(
        "--skip-project-properties",
        action="store_true",
        help="Do not export project entity properties.",
    )
    parser.add_argument(
        "--skip-epics",
        action="store_true",
        help="Do not export site-wide Epic work items.",
    )
    parser.add_argument(
        "--skip-browser-view-state",
        action="store_true",
        help=(
            "Do not attempt to inspect Firefox Jira-origin localStorage, IndexedDB, and sessionStorage "
            "for project/space List-view columns and order."
        ),
    )
    parser.add_argument(
        "--firefox-profile",
        help=(
            "Optional Firefox profile folder to use for browser-local Jira List-view state (localStorage, IndexedDB, sessionStorage). "
            "If omitted, common Firefox profile locations are scanned automatically."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional .env path. Default: ./.env",
    )
    return parser.parse_args(argv)


def resolve_settings(args: argparse.Namespace) -> tuple[ExportSettings, str]:
    load_dotenv_file(Path(args.env_file))

    site = args.site or os.getenv("JIRA_SITE_URL") or input("Source Jira site URL: ").strip()
    email = args.email or os.getenv("JIRA_EMAIL") or input("Atlassian account email: ").strip()
    token = os.getenv("JIRA_API_TOKEN") or getpass.getpass(
        "Jira API token (hidden; never written to the export): "
    )

    if not site or not email or not token:
        raise ValueError("Site URL, email, and API token are required")

    env_projects = [
        item.strip().upper()
        for item in os.getenv("JIRA_PROJECT_KEYS", "").split(",")
        if item.strip()
    ]
    cli_projects = [item.strip().upper() for item in args.project if item.strip()]
    projects = cli_projects or env_projects

    include_users = not args.skip_users and parse_bool(os.getenv("JIRA_INCLUDE_USERS"), True)
    include_group_members = not args.skip_group_members and parse_bool(
        os.getenv("JIRA_INCLUDE_GROUP_MEMBERS"), True
    )
    include_properties = not args.skip_project_properties and parse_bool(
        os.getenv("JIRA_INCLUDE_PROJECT_PROPERTIES"), True
    )
    include_epics = not args.skip_epics and parse_bool(
        os.getenv("JIRA_INCLUDE_EPICS"), True
    )
    include_browser_view_state = not args.skip_browser_view_state and parse_bool(
        os.getenv("JIRA_INCLUDE_BROWSER_VIEW_STATE"), True
    )
    firefox_profile_value = args.firefox_profile or os.getenv("JIRA_FIREFOX_PROFILE")
    firefox_profile = (
        Path(firefox_profile_value).expanduser().resolve() if firefox_profile_value else None
    )

    settings = ExportSettings(
        site_url=normalize_site_url(site),
        email=email,
        project_keys=projects,
        include_users=include_users,
        include_group_members=include_group_members,
        include_project_properties=include_properties,
        include_epics=include_epics,
        include_browser_view_state=include_browser_view_state,
        firefox_profile=firefox_profile,
        output_parent=Path(args.output).expanduser().resolve(),
    )
    settings.output_parent.mkdir(parents=True, exist_ok=True)
    return settings, token


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        settings, token = resolve_settings(args)
        client = JiraClient(settings.site_url, settings.email, token)
        exporter = JiraExporter(client, settings)
        exporter.run()
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
