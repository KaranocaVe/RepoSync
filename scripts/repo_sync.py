#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


GITHUB_API_BASE = "https://api.github.com"
GITEE_API_BASE = "https://gitee.com/api/v5"
GITEE_WEB_BASE = "https://gitee.com"
GITCODE_API_BASE = "https://api.gitcode.com/api/v5"
GITCODE_WEB_BASE = "https://gitcode.com"
TARGET_PLATFORMS = ("gitee", "gitcode")
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
RELEASE_ARCHIVE_URL_MARKERS = ("/-/archive/", "/repository/archive/", "/archive/refs/tags/")
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SyncError(RuntimeError):
    """Raised for user-facing sync failures."""


class ApiError(SyncError):
    """Raised for API request failures."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class AssetComparison:
    match: bool
    missing: list[str]
    extra: list[str]
    changed: list[str]
    unknown_size: list[str]


class SummaryBuffer:
    def __init__(self, title: str):
        self.title = title
        self.lines: list[str] = []

    def heading(self, text: str) -> None:
        self.lines.append(f"### {text}")

    def bullet(self, text: str) -> None:
        self.lines.append(f"- {text}")

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def flush(self) -> None:
        path = os.getenv("GITHUB_STEP_SUMMARY")
        if not path:
            return
        payload = "\n".join([f"## {self.title}", *self.lines, ""])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(payload)


def log(message: str) -> None:
    print(f"[repo-sync] {message}", flush=True)


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").strip()


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "entry"


def safe_path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value) or "item"


def quote_component(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def append_query(url: str, params: dict[str, Any]) -> str:
    parsed = urllib.parse.urlsplit(url)
    current = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        current.append((key, str(value)))
    query = urllib.parse.urlencode(current)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def normalize_form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_positive_int(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SyncError(f"{field} must be an integer, got {value!r}") from exc
    if parsed < 1:
        raise SyncError(f"{field} must be >= 1, got {parsed}")
    return parsed


def write_github_output(path: str, name: str, value: str) -> None:
    marker = f"EOF_{uuid.uuid4().hex}"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{marker}\n{value}\n{marker}\n")


def maybe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    safe_command: str | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        rendered = safe_command or shlex.join(command)
        stderr = process.stderr.strip()
        stdout = process.stdout.strip()
        details = stderr or stdout or f"exit code {process.returncode}"
        raise SyncError(f"Command failed: {rendered}\n{details}")
    return process


def download_to_file(url: str, destination: Path, *, headers: dict[str, str] | None = None) -> None:
    headers = headers or {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=300) as response, open(destination, "wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in RETRYABLE_HTTP_CODES and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise SyncError(f"Failed to download {url}: HTTP {exc.code} {body}") from exc
        except Exception as exc:  # pragma: no cover - network failure path
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise SyncError(f"Failed to download {url}: {exc}") from exc


def probe_url_size(url: str, *, headers: dict[str, str] | None = None) -> int | None:
    headers = headers or {}
    for method in ("HEAD", "GET"):
        request_headers = dict(headers)
        if method == "GET":
            request_headers["Range"] = "bytes=0-0"
        request = urllib.request.Request(url, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and content_length.isdigit():
                    return int(content_length)
                content_range = response.headers.get("Content-Range")
                if content_range:
                    match = re.search(r"/(\d+)$", content_range)
                    if match:
                        return int(match.group(1))
                return None
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 405, 401, 403}:
                continue
        except Exception:
            continue
    return None


def curl_multipart_upload(
    url: str,
    file_path: Path,
    *,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    field_name: str = "file",
) -> Any:
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--location",
        "--request",
        method,
    ]
    for header_name, header_value in (headers or {}).items():
        command.extend(["--header", f"{header_name}: {header_value}"])
    command.extend(["--form", f"{field_name}=@{file_path}"])
    command.append(url)
    process = subprocess.run(command, text=True, capture_output=True)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit code {process.returncode}"
        raise SyncError(f"Upload failed: {detail}")
    output = process.stdout.strip()
    return maybe_json_loads(output) if output else None


def curl_raw_upload(url: str, file_path: Path, *, headers: dict[str, str] | None = None, method: str = "PUT") -> Any:
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--location",
        "--request",
        method,
        "--upload-file",
        str(file_path),
    ]
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    command.extend(["--header", f"Content-Type: {mime_type}"])
    for header_name, header_value in (headers or {}).items():
        command.extend(["--header", f"{header_name}: {header_value}"])
    command.append(url)
    process = subprocess.run(command, text=True, capture_output=True)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit code {process.returncode}"
        raise SyncError(f"Raw upload failed: {detail}")
    output = process.stdout.strip()
    return maybe_json_loads(output) if output else None


class HttpClient:
    def __init__(self, user_agent: str):
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        form: dict[str, Any] | None = None,
        expected: tuple[int, ...] | None = (200,),
        allow_404: bool = False,
    ) -> Any:
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if params:
            url = append_query(url, params)
        data = None
        if json_body is not None and form is not None:
            raise ValueError("json_body and form are mutually exclusive")
        if json_body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            data = json.dumps(json_body).encode("utf-8")
        elif form is not None:
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            normalized_form = {key: normalize_form_value(value) for key, value in form.items() if value is not None}
            data = urllib.parse.urlencode(normalized_form).encode("utf-8")
        request = urllib.request.Request(url, headers=request_headers, data=data, method=method.upper())
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = response.read()
                    if expected and response.status not in expected:
                        raise ApiError(f"{method} {url} returned unexpected status {response.status}", response.status)
                    if not payload:
                        return None
                    text = payload.decode("utf-8", errors="replace")
                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" in content_type or text[:1] in "{[":
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            return text
                    return text
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                if allow_404 and exc.code == 404:
                    return None
                if exc.code in RETRYABLE_HTTP_CODES and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(f"{method} {url} failed with HTTP {exc.code}: {body_text}", exc.code, body_text) from exc


def origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def load_config(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        raise SyncError(f"Config file does not exist: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise SyncError("Config root must be a mapping")
    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise SyncError("defaults must be a mapping")
    defaults = {
        "release_limit": parse_positive_int(defaults_raw.get("release_limit", 10), field="defaults.release_limit"),
        "max_parallel": parse_positive_int(defaults_raw.get("max_parallel", 3), field="defaults.max_parallel"),
    }
    repos_raw = raw.get("repos") or []
    if not isinstance(repos_raw, list):
        raise SyncError("repos must be a list")
    entries = [normalize_entry(item, defaults, index=index) for index, item in enumerate(repos_raw, start=1)]
    seen_ids: set[str] = set()
    for entry in entries:
        if entry["id"] in seen_ids:
            raise SyncError(f"Duplicate repo entry id: {entry['id']}")
        seen_ids.add(entry["id"])
    return defaults, entries


def normalize_entry(raw: Any, defaults: dict[str, Any], *, index: int | None = None) -> dict[str, Any]:
    location = f"repos[{index}]" if index is not None else "entry"
    if not isinstance(raw, dict):
        raise SyncError(f"{location} must be a mapping")
    source_raw = raw.get("source")
    if not isinstance(source_raw, dict):
        raise SyncError(f"{location}.source must be a mapping")
    full_name = str(source_raw.get("full_name", "")).strip()
    if not REPO_NAME_PATTERN.match(full_name):
        raise SyncError(f"{location}.source.full_name must match owner/repo, got {full_name!r}")
    entry_id = str(raw.get("id") or safe_slug(full_name))
    release_limit = parse_positive_int(raw.get("release_limit", defaults["release_limit"]), field=f"{location}.release_limit")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, dict):
        raise SyncError(f"{location}.targets must be a mapping")
    targets: dict[str, dict[str, Any]] = {}
    enabled_targets = 0
    for platform in TARGET_PLATFORMS:
        platform_raw = targets_raw.get(platform)
        target = normalize_target(platform_raw, platform=platform, location=f"{location}.targets.{platform}")
        targets[platform] = target
        if target["enabled"]:
            enabled_targets += 1
    if enabled_targets == 0:
        raise SyncError(f"{location} must enable at least one target platform")
    entry = {
        "id": entry_id,
        "source": {
            "full_name": full_name,
            "private": as_bool(source_raw.get("private"), default=False),
        },
        "release_limit": release_limit,
        "lfs": as_bool(raw.get("lfs"), default=False),
        "targets": targets,
    }
    return entry


def normalize_target(raw: Any, *, platform: str, location: str) -> dict[str, Any]:
    if raw is None:
        return {
            "enabled": False,
            "namespace": "",
            "name": "",
            "visibility": "public",
            "sync_releases": False,
        }
    if not isinstance(raw, dict):
        raise SyncError(f"{location} must be a mapping")
    enabled = as_bool(raw.get("enabled"), default=False)
    target = {
        "enabled": enabled,
        "namespace": str(raw.get("namespace", "")).strip(),
        "name": str(raw.get("name", "")).strip(),
        "visibility": str(raw.get("visibility", "public")).strip().lower(),
        "sync_releases": as_bool(raw.get("sync_releases"), default=True if enabled else False),
    }
    if not enabled:
        return target
    if not target["namespace"]:
        raise SyncError(f"{location}.namespace is required when {platform} is enabled")
    if not target["name"]:
        raise SyncError(f"{location}.name is required when {platform} is enabled")
    if target["visibility"] not in {"public", "private"}:
        raise SyncError(f"{location}.visibility must be public or private")
    return target


def select_manifest_entries(entries: list[dict[str, Any]], entry_ids_raw: str) -> list[dict[str, Any]]:
    if not entry_ids_raw.strip():
        return entries
    wanted = {item.strip() for item in entry_ids_raw.split(",") if item.strip()}
    selected = [entry for entry in entries if entry["id"] in wanted]
    found = {entry["id"] for entry in selected}
    missing = sorted(wanted - found)
    if missing:
        raise SyncError(f"Unknown entry_ids: {', '.join(missing)}")
    return selected


def build_matrix(args: argparse.Namespace) -> None:
    summary = SummaryBuffer("Matrix Resolution")
    defaults, entries = load_config(Path(args.config))
    if args.mode == "manifest":
        selected = select_manifest_entries(entries, args.entry_ids or "")
    else:
        if not args.repo_spec_json or not args.repo_spec_json.strip():
            raise SyncError("repo_spec_json is required when mode=adhoc")
        try:
            repo_spec = json.loads(args.repo_spec_json)
        except json.JSONDecodeError as exc:
            raise SyncError(f"repo_spec_json is not valid JSON: {exc}") from exc
        adhoc = normalize_entry(repo_spec, defaults)
        selected = [adhoc]
    matrix = {
        "include": [
            {
                "entry_id": entry["id"],
                "entry_json": json.dumps(entry, separators=(",", ":"), sort_keys=True),
            }
            for entry in selected
        ]
    }
    has_entries = "true" if selected else "false"
    summary.bullet(f"Mode: {args.mode}")
    summary.bullet(f"Matched entries: {len(selected)}")
    summary.bullet(f"Max parallel: {defaults['max_parallel']}")
    if selected:
        summary.bullet(f"Entry IDs: {', '.join(entry['id'] for entry in selected)}")
    else:
        summary.bullet("No repositories matched the current configuration")
    summary.flush()
    if args.github_output:
        write_github_output(args.github_output, "matrix", json.dumps(matrix, separators=(",", ":")))
        write_github_output(args.github_output, "max_parallel", str(defaults["max_parallel"]))
        write_github_output(args.github_output, "has_entries", has_entries)
    else:
        print(json.dumps({"matrix": matrix, "max_parallel": defaults["max_parallel"], "has_entries": has_entries}))


class GitHubSourceClient:
    def __init__(self, full_name: str, *, private: bool, source_token: str | None, actions_token: str | None):
        self.full_name = full_name
        self.private = private
        self.source_token = source_token
        self.actions_token = actions_token
        self.api_token = source_token or actions_token
        self.http = HttpClient("repo-sync-github-source")
        self._repo_data: dict[str, Any] | None = None

    def _api_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def get_repo(self) -> dict[str, Any]:
        if self._repo_data is None:
            repo = self.http.request(
                "GET",
                f"{GITHUB_API_BASE}/repos/{self.full_name}",
                headers=self._api_headers(),
                expected=(200,),
                allow_404=False,
            )
            if not isinstance(repo, dict):
                raise SyncError(f"Unexpected GitHub repo response for {self.full_name}")
            self._repo_data = repo
        return self._repo_data

    def clone_url(self) -> str:
        if self.private and not self.source_token:
            raise SyncError(f"{self.full_name} is marked private but SOURCE_GITHUB_TOKEN is not configured")
        if self.source_token:
            token = quote_component(self.source_token)
            return f"https://x-access-token:{token}@github.com/{self.full_name}.git"
        return f"https://github.com/{self.full_name}.git"

    def list_releases(self, limit: int) -> list[dict[str, Any]]:
        releases: list[dict[str, Any]] = []
        page = 1
        while len(releases) < limit:
            batch = self.http.request(
                "GET",
                f"{GITHUB_API_BASE}/repos/{self.full_name}/releases",
                headers=self._api_headers(),
                params={"per_page": min(100, limit), "page": page},
                expected=(200,),
            )
            if not batch:
                break
            if not isinstance(batch, list):
                raise SyncError(f"Unexpected GitHub releases response for {self.full_name}")
            draft_filtered = [item for item in batch if not as_bool(item.get("draft"), default=False)]
            for release in draft_filtered:
                releases.append(self.normalize_release(release))
                if len(releases) >= limit:
                    break
            if len(batch) < min(100, limit):
                break
            page += 1
        return releases[:limit]

    def normalize_release(self, raw: dict[str, Any]) -> dict[str, Any]:
        assets = []
        for asset in raw.get("assets") or []:
            assets.append(
                {
                    "name": asset["name"],
                    "size": int(asset.get("size") or 0),
                    "browser_download_url": asset.get("browser_download_url"),
                    "api_url": asset.get("url"),
                }
            )
        return {
            "tag_name": raw["tag_name"],
            "name": raw.get("name") or raw["tag_name"],
            "body": raw.get("body") or "",
            "prerelease": as_bool(raw.get("prerelease"), default=False),
            "assets": assets,
        }

    def download_release_asset(self, asset: dict[str, Any], destination: Path) -> None:
        headers = {"Accept": "application/octet-stream"}
        url = asset.get("browser_download_url")
        if self.source_token and asset.get("api_url"):
            url = asset["api_url"]
            headers["Authorization"] = f"Bearer {self.source_token}"
        download_to_file(url, destination, headers=headers)


class BaseTargetClient:
    platform_name = "target"
    api_base = ""
    web_base = ""

    def __init__(self, token: str):
        if not token:
            raise SyncError(f"{self.platform_name.upper()} token is required for enabled target")
        self.token = token
        self.http = HttpClient(f"repo-sync-{self.platform_name}")
        self._login: str | None = None

    def api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json_body: Any | None = None,
        expected: tuple[int, ...] | None = (200,),
        allow_404: bool = False,
    ) -> Any:
        request_params = dict(params or {})
        request_params["access_token"] = self.token
        return self.http.request(
            method,
            f"{self.api_base}{path}",
            params=request_params,
            form=form,
            json_body=json_body,
            expected=expected,
            allow_404=allow_404,
        )

    def current_user_login(self) -> str:
        if self._login is None:
            user = self.api_request("GET", "/user", expected=(200,))
            if not isinstance(user, dict):
                raise SyncError(f"Unexpected {self.platform_name} /user response")
            login = user.get("login") or user.get("username") or user.get("path") or user.get("name")
            if not login:
                raise SyncError(f"Unable to determine authenticated {self.platform_name} login")
            self._login = str(login)
        return self._login

    def authenticated_git_url(self, namespace: str, repo_name: str) -> str:
        username = quote_component(self.current_user_login())
        token = quote_component(self.token)
        host = urllib.parse.urlsplit(self.web_base).netloc
        return f"https://{username}:{token}@{host}/{namespace}/{repo_name}.git"

    def get_repo(self, namespace: str, repo_name: str) -> dict[str, Any] | None:
        repo = self.api_request("GET", f"/repos/{namespace}/{repo_name}", expected=(200,), allow_404=True)
        if repo is None:
            return None
        if not isinstance(repo, dict):
            raise SyncError(f"Unexpected {self.platform_name} repo response for {namespace}/{repo_name}")
        return repo

    def ensure_repo(self, target: dict[str, Any], source_repo: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        namespace = target["namespace"]
        repo_name = target["name"]
        existing = self.get_repo(namespace, repo_name)
        if existing is not None:
            existing_visibility = repo_visibility(existing)
            if existing_visibility != target["visibility"]:
                raise SyncError(
                    f"{self.platform_name} repo {namespace}/{repo_name} already exists as {existing_visibility}, "
                    f"expected {target['visibility']}"
                )
            return existing, False
        create_path = "/user/repos" if namespace == self.current_user_login() else f"/orgs/{namespace}/repos"
        payload = {
            "name": repo_name,
            "path": repo_name,
            "description": (source_repo.get("description") or f"Mirror of {source_repo.get('full_name')}")[:500],
            "homepage": source_repo.get("homepage") or None,
            "private": target["visibility"] == "private",
            "auto_init": False,
        }
        created = self.api_request("POST", create_path, form=payload, expected=(200, 201))
        if not isinstance(created, dict):
            raise SyncError(f"Unexpected {self.platform_name} create repo response for {namespace}/{repo_name}")
        return created, True

    def list_releases(self, namespace: str, repo_name: str) -> list[dict[str, Any]]:
        raw = self.api_request(
            "GET",
            f"/repos/{namespace}/{repo_name}/releases",
            params={"page": 1, "per_page": 100},
            expected=(200,),
        )
        items = raw if isinstance(raw, list) else raw.get("releases") if isinstance(raw, dict) else None
        if items is None:
            raise SyncError(f"Unexpected {self.platform_name} releases response for {namespace}/{repo_name}")
        return [self.normalize_release(item) for item in items if isinstance(item, dict)]

    def get_release_by_tag(self, namespace: str, repo_name: str, tag_name: str) -> dict[str, Any] | None:
        raw = self.api_request(
            "GET",
            f"/repos/{namespace}/{repo_name}/releases/tags/{quote_component(tag_name)}",
            expected=(200,),
            allow_404=True,
        )
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise SyncError(f"Unexpected {self.platform_name} release response for tag {tag_name}")
        return self.normalize_release(raw)

    def release_payload(self, release: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag_name": release["tag_name"],
            "name": release["name"],
            "body": release["body"],
            "prerelease": release["prerelease"],
        }

    def create_release(self, namespace: str, repo_name: str, release: dict[str, Any]) -> dict[str, Any]:
        raw = self.api_request(
            "POST",
            f"/repos/{namespace}/{repo_name}/releases",
            form=self.release_payload(release),
            expected=(200, 201),
        )
        if isinstance(raw, dict):
            return self.normalize_release(raw)
        fetched = self.get_release_by_tag(namespace, repo_name, release["tag_name"])
        if fetched is None:
            raise SyncError(f"{self.platform_name} release {release['tag_name']} was not found after creation")
        return fetched

    def update_release(self, namespace: str, repo_name: str, existing: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
        identifier = existing.get("id")
        if identifier is None:
            identifier = quote_component(existing["tag_name"])
        raw = self.api_request(
            "PATCH",
            f"/repos/{namespace}/{repo_name}/releases/{identifier}",
            form=self.release_payload(release),
            expected=(200,),
            allow_404=False,
        )
        if isinstance(raw, dict):
            return self.normalize_release(raw)
        fetched = self.get_release_by_tag(namespace, repo_name, release["tag_name"])
        if fetched is None:
            raise SyncError(f"{self.platform_name} release {release['tag_name']} was not found after update")
        return fetched

    def delete_release(self, namespace: str, repo_name: str, existing: dict[str, Any]) -> bool:
        release_id = existing.get("id")
        if release_id is None:
            return False
        try:
            self.api_request(
                "DELETE",
                f"/repos/{namespace}/{repo_name}/releases/{release_id}",
                expected=(200, 202, 204),
                allow_404=False,
            )
            return True
        except ApiError as exc:
            if exc.status in {404, 405}:
                return False
            raise

    def normalize_release(self, raw: dict[str, Any]) -> dict[str, Any]:
        assets = []
        for asset in raw.get("assets") or raw.get("attach_files") or []:
            if not isinstance(asset, dict) or self.is_generated_asset(asset):
                continue
            assets.append(
                {
                    "id": asset.get("id") or asset.get("attach_file_id"),
                    "name": asset.get("name") or asset.get("file_name"),
                    "size": asset.get("size"),
                    "browser_download_url": asset.get("browser_download_url") or asset.get("download_url"),
                }
            )
        return {
            "id": raw.get("id"),
            "tag_name": raw.get("tag_name"),
            "name": raw.get("name") or raw.get("tag_name"),
            "body": raw.get("body") or "",
            "prerelease": as_bool(raw.get("prerelease"), default=False),
            "has_prerelease_field": "prerelease" in raw,
            "assets": assets,
        }

    def is_generated_asset(self, asset: dict[str, Any]) -> bool:
        url = (asset.get("browser_download_url") or asset.get("download_url") or "").lower()
        return any(marker in url for marker in RELEASE_ARCHIVE_URL_MARKERS)

    def upload_release_asset(self, namespace: str, repo_name: str, release: dict[str, Any], file_path: Path) -> Any:
        release_id = release.get("id")
        if release_id is None:
            raise SyncError(f"{self.platform_name} release {release['tag_name']} is missing an id required for attachment upload")
        url = append_query(
            f"{self.api_base}/repos/{namespace}/{repo_name}/releases/{release_id}/attach_files",
            {"access_token": self.token},
        )
        return curl_multipart_upload(url, file_path)

    def asset_size(self, namespace: str, repo_name: str, release: dict[str, Any], asset: dict[str, Any]) -> int | None:
        size = asset.get("size")
        if isinstance(size, int):
            return size
        if isinstance(size, str) and size.isdigit():
            return int(size)
        url = asset.get("browser_download_url")
        if not url:
            return None
        return probe_url_size(append_query(url, {"access_token": self.token}))


class GiteeTargetClient(BaseTargetClient):
    platform_name = "gitee"
    api_base = GITEE_API_BASE
    web_base = GITEE_WEB_BASE


class GitCodeTargetClient(BaseTargetClient):
    platform_name = "gitcode"
    api_base = GITCODE_API_BASE
    web_base = GITCODE_WEB_BASE

    def update_release(self, namespace: str, repo_name: str, existing: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
        raw = self.api_request(
            "PATCH",
            f"/repos/{namespace}/{repo_name}/releases/{quote_component(existing['tag_name'])}",
            form=self.release_payload(release),
            expected=(200,),
            allow_404=False,
        )
        if isinstance(raw, dict):
            return self.normalize_release(raw)
        fetched = self.get_release_by_tag(namespace, repo_name, release["tag_name"])
        if fetched is None:
            raise SyncError(f"{self.platform_name} release {release['tag_name']} was not found after update")
        return fetched

    def upload_release_asset(self, namespace: str, repo_name: str, release: dict[str, Any], file_path: Path) -> Any:
        release_tag = release["tag_name"]
        upload_meta = self.api_request(
            "GET",
            f"/repos/{namespace}/{repo_name}/releases/{quote_component(release_tag)}/upload_url",
            expected=(200,),
            allow_404=True,
        )
        candidates = self.extract_upload_candidates(upload_meta)
        errors: list[str] = []
        for candidate in candidates:
            url = candidate["url"]
            headers = candidate.get("headers") or {}
            strategy = candidate["strategy"]
            try:
                if strategy == "raw":
                    return curl_raw_upload(url, file_path, headers=headers)
                return curl_multipart_upload(url, file_path, headers=headers, field_name=candidate.get("field_name", "file"))
            except SyncError as exc:
                errors.append(f"{strategy} {url}: {exc}")
        release_id = release.get("id")
        fallback_urls = []
        if release_id is not None:
            fallback_urls.append(
                append_query(
                    f"{self.api_base}/repos/{namespace}/{repo_name}/releases/{release_id}/attach_files",
                    {"access_token": self.token},
                )
            )
        fallback_urls.append(
            append_query(
                f"{self.api_base}/repos/{namespace}/{repo_name}/releases/{quote_component(release_tag)}/attach_files",
                {"access_token": self.token},
            )
        )
        for url in fallback_urls:
            for field_name in ("file", "attach_file"):
                try:
                    return curl_multipart_upload(url, file_path, field_name=field_name)
                except SyncError as exc:
                    errors.append(f"multipart {url} field={field_name}: {exc}")
        error_text = "\n".join(errors[-6:])
        raise SyncError(f"GitCode attachment upload failed for {release_tag}:\n{error_text}")

    def extract_upload_candidates(self, upload_meta: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        urls = extract_urls(upload_meta)
        headers = extract_headers(upload_meta)
        for url in urls:
            normalized = self.normalize_upload_url(url)
            if is_probably_presigned_url(normalized):
                candidates.append({"url": normalized, "strategy": "raw", "headers": headers})
            candidates.append({"url": normalized, "strategy": "multipart", "headers": headers, "field_name": "file"})
        return dedupe_candidates(candidates)

    def normalize_upload_url(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/api/"):
            return f"{origin(self.api_base)}{url}"
        if url.startswith("/"):
            return f"{self.web_base}{url}"
        return url

    def asset_size(self, namespace: str, repo_name: str, release: dict[str, Any], asset: dict[str, Any]) -> int | None:
        size = asset.get("size")
        if isinstance(size, int):
            return size
        if isinstance(size, str) and size.isdigit():
            return int(size)
        asset_name = asset.get("name")
        if not asset_name:
            return None
        url = append_query(
            f"{self.api_base}/repos/{namespace}/{repo_name}/releases/{quote_component(release['tag_name'])}/attach_files/{quote_component(asset_name)}/download",
            {"access_token": self.token},
        )
        return probe_url_size(url)


def extract_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://") or value.startswith("/"):
            urls.append(value)
        return urls
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"upload_url", "uploadurl", "url"} and isinstance(item, str):
                urls.append(item)
            else:
                urls.extend(extract_urls(item))
        return urls
    if isinstance(value, list):
        for item in value:
            urls.extend(extract_urls(item))
    return urls


def extract_headers(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        headers = value.get("headers")
        if isinstance(headers, dict):
            return {str(key): str(item) for key, item in headers.items()}
        for item in value.values():
            extracted = extract_headers(item)
            if extracted:
                return extracted
    if isinstance(value, list):
        for item in value:
            extracted = extract_headers(item)
            if extracted:
                return extracted
    return {}


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        key = (candidate["url"], candidate["strategy"], candidate.get("field_name", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def is_probably_presigned_url(url: str) -> bool:
    lowered = url.lower()
    parsed = urllib.parse.urlsplit(lowered)
    host = parsed.netloc
    if host not in {urllib.parse.urlsplit(GITCODE_API_BASE).netloc, urllib.parse.urlsplit(GITCODE_WEB_BASE).netloc}:
        return True
    return any(marker in lowered for marker in ("signature=", "x-amz-", "x-oss-", "x-cos-", "uploads/"))


def repo_visibility(repo: dict[str, Any]) -> str:
    private_value = repo.get("private")
    if isinstance(private_value, bool):
        return "private" if private_value else "public"
    if private_value is not None and str(private_value).lower() in {"1", "true"}:
        return "private"
    public_value = repo.get("public")
    if public_value is not None:
        return "private" if str(public_value) == "0" else "public"
    return "public"


def build_target_client(platform: str) -> BaseTargetClient:
    if platform == "gitee":
        return GiteeTargetClient(os.getenv("GITEE_TOKEN", "").strip())
    if platform == "gitcode":
        return GitCodeTargetClient(os.getenv("GITCODE_TOKEN", "").strip())
    raise SyncError(f"Unsupported platform: {platform}")


def mirror_clone(source_client: GitHubSourceClient, destination: Path) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    run_command(
        ["git", "clone", "--mirror", source_client.clone_url(), str(destination)],
        env=env,
        safe_command=f"git clone --mirror https://github.com/{source_client.full_name}.git {destination}",
    )


def add_remote(mirror_dir: Path, remote_name: str, remote_url: str) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    run_command(
        ["git", "--git-dir", str(mirror_dir), "remote", "add", remote_name, remote_url],
        env=env,
        safe_command=f"git --git-dir {mirror_dir} remote add {remote_name} [redacted-url]",
    )


def push_mirror(mirror_dir: Path, remote_name: str) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    run_command(
        ["git", "--git-dir", str(mirror_dir), "push", "--mirror", remote_name],
        env=env,
        safe_command=f"git --git-dir {mirror_dir} push --mirror {remote_name}",
    )


def ensure_git_lfs_available() -> None:
    run_command(["git", "lfs", "version"], safe_command="git lfs version")


def fetch_lfs(mirror_dir: Path) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    run_command(
        ["git", "--git-dir", str(mirror_dir), "lfs", "fetch", "--all", "origin"],
        env=env,
        safe_command=f"git --git-dir {mirror_dir} lfs fetch --all origin",
    )


def push_lfs(mirror_dir: Path, remote_name: str) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    run_command(
        ["git", "--git-dir", str(mirror_dir), "lfs", "push", "--all", remote_name],
        env=env,
        safe_command=f"git --git-dir {mirror_dir} lfs push --all {remote_name}",
    )


def detect_lfs(mirror_dir: Path) -> bool:
    refs = run_command(
        ["git", "--git-dir", str(mirror_dir), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        safe_command=f"git --git-dir {mirror_dir} for-each-ref refs/heads",
    ).stdout.splitlines()
    for ref_name in refs:
        result = subprocess.run(
            ["git", "--git-dir", str(mirror_dir), "show", f"{ref_name}:.gitattributes"],
            text=True,
            capture_output=True,
        )
        if result.returncode == 0 and "filter=lfs" in result.stdout:
            return True
    return False


def release_metadata_match(source_release: dict[str, Any], target_release: dict[str, Any]) -> bool:
    name_matches = normalize_text(source_release["name"]) == normalize_text(target_release.get("name"))
    body_matches = normalize_text(source_release["body"]) == normalize_text(target_release.get("body"))
    prerelease_matches = True
    if target_release.get("has_prerelease_field", True):
        prerelease_matches = as_bool(source_release.get("prerelease")) == as_bool(target_release.get("prerelease"))
    return name_matches and body_matches and prerelease_matches


def compare_release_assets(
    client: BaseTargetClient,
    target: dict[str, Any],
    source_release: dict[str, Any],
    target_release: dict[str, Any],
) -> AssetComparison:
    source_assets = {asset["name"]: int(asset["size"]) for asset in source_release["assets"]}
    target_assets_by_name: dict[str, int | None] = {}
    for asset in target_release.get("assets") or []:
        if not asset.get("name"):
            continue
        target_assets_by_name[asset["name"]] = client.asset_size(target["namespace"], target["name"], target_release, asset)
    missing = sorted(set(source_assets) - set(target_assets_by_name))
    extra = sorted(set(target_assets_by_name) - set(source_assets))
    changed = []
    unknown_size = []
    for name, source_size in source_assets.items():
        if name not in target_assets_by_name:
            continue
        target_size = target_assets_by_name[name]
        if target_size is None:
            unknown_size.append(name)
        elif target_size != source_size:
            changed.append(name)
    match = not missing and not extra and not changed
    return AssetComparison(match=match, missing=missing, extra=extra, changed=changed, unknown_size=unknown_size)


def get_cached_asset_path(
    source_client: GitHubSourceClient,
    cache_root: Path,
    source_release: dict[str, Any],
    asset: dict[str, Any],
    downloaded: dict[tuple[str, str], Path],
) -> Path:
    cache_key = (source_release["tag_name"], asset["name"])
    if cache_key in downloaded:
        return downloaded[cache_key]
    release_dir = cache_root / safe_path_component(source_release["tag_name"])
    asset_path = release_dir / safe_path_component(asset["name"])
    log(f"Downloading source asset {source_release['tag_name']} / {asset['name']}")
    source_client.download_release_asset(asset, asset_path)
    downloaded[cache_key] = asset_path
    return asset_path


def sync_releases_to_target(
    client: BaseTargetClient,
    target: dict[str, Any],
    source_client: GitHubSourceClient,
    source_releases: list[dict[str, Any]],
    asset_cache_root: Path,
    downloaded_assets: dict[tuple[str, str], Path],
    summary: SummaryBuffer,
) -> dict[str, int]:
    namespace = target["namespace"]
    repo_name = target["name"]
    target_releases = {release["tag_name"]: release for release in client.list_releases(namespace, repo_name)}
    stats = {"created": 0, "recreated": 0, "updated": 0, "skipped": 0}
    for source_release in source_releases:
        tag_name = source_release["tag_name"]
        existing = target_releases.get(tag_name)
        if existing is None:
            log(f"[{client.platform_name}] Creating release {tag_name}")
            created = client.create_release(namespace, repo_name, source_release)
            for asset in source_release["assets"]:
                asset_path = get_cached_asset_path(source_client, asset_cache_root, source_release, asset, downloaded_assets)
                client.upload_release_asset(namespace, repo_name, created, asset_path)
            stats["created"] += 1
            continue

        metadata_ok = release_metadata_match(source_release, existing)
        asset_comparison = compare_release_assets(client, target, source_release, existing)
        if metadata_ok and asset_comparison.match:
            stats["skipped"] += 1
            if asset_comparison.unknown_size:
                summary.bullet(
                    f"{client.platform_name} release {tag_name}: size probe unavailable for "
                    f"{', '.join(asset_comparison.unknown_size)}; treated as unchanged"
                )
            continue

        recreated = client.delete_release(namespace, repo_name, existing)
        if recreated:
            log(f"[{client.platform_name}] Recreating release {tag_name}")
            recreated_release = client.create_release(namespace, repo_name, source_release)
            for asset in source_release["assets"]:
                asset_path = get_cached_asset_path(source_client, asset_cache_root, source_release, asset, downloaded_assets)
                client.upload_release_asset(namespace, repo_name, recreated_release, asset_path)
            stats["recreated"] += 1
            continue

        log(f"[{client.platform_name}] Updating release {tag_name}")
        updated_release = client.update_release(namespace, repo_name, existing, source_release)
        names_to_upload = set(asset_comparison.missing + asset_comparison.changed)
        for asset in source_release["assets"]:
            if asset["name"] not in names_to_upload:
                continue
            asset_path = get_cached_asset_path(source_client, asset_cache_root, source_release, asset, downloaded_assets)
            client.upload_release_asset(namespace, repo_name, updated_release, asset_path)
        if asset_comparison.extra:
            summary.bullet(
                f"{client.platform_name} release {tag_name}: extra target attachments left in place "
                f"because delete-and-recreate is not available"
            )
        stats["updated"] += 1
    return stats


def handle_sync_entry(args: argparse.Namespace) -> None:
    try:
        entry = json.loads(args.entry_json)
    except json.JSONDecodeError as exc:
        raise SyncError(f"entry-json is not valid JSON: {exc}") from exc
    if not isinstance(entry, dict):
        raise SyncError("entry-json must decode to an object")
    entry_id = entry["id"]
    summary = SummaryBuffer(f"Repo Sync: {entry_id}")
    source_spec = entry["source"]
    source_token = os.getenv("SOURCE_GITHUB_TOKEN", "").strip() or None
    actions_token = os.getenv("ACTIONS_GITHUB_TOKEN", "").strip() or None
    source_client = GitHubSourceClient(
        source_spec["full_name"],
        private=as_bool(source_spec.get("private"), default=False),
        source_token=source_token,
        actions_token=actions_token,
    )

    try:
        summary.bullet(f"Source: {source_spec['full_name']}")
        summary.bullet(f"LFS requested: {'yes' if entry['lfs'] else 'no'}")
        repo_data = source_client.get_repo()
        repo_data["full_name"] = source_spec["full_name"]
        if as_bool(repo_data.get("private"), default=False) and not source_token:
            raise SyncError(f"{source_spec['full_name']} is private; SOURCE_GITHUB_TOKEN is required")

        enabled_targets = [(name, spec) for name, spec in entry["targets"].items() if spec["enabled"]]
        wants_releases = any(target["sync_releases"] for _, target in enabled_targets)
        source_releases = source_client.list_releases(entry["release_limit"]) if wants_releases else []
        summary.bullet(f"Recent releases loaded: {len(source_releases)}")

        with tempfile.TemporaryDirectory(prefix="repo-sync-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            mirror_dir = temp_dir / "source.git"
            asset_cache_root = temp_dir / "release-assets"
            downloaded_assets: dict[tuple[str, str], Path] = {}

            log(f"Cloning mirror for {source_spec['full_name']}")
            mirror_clone(source_client, mirror_dir)
            lfs_detected = detect_lfs(mirror_dir)
            summary.bullet(f"LFS detected in source refs: {'yes' if lfs_detected else 'no'}")
            if lfs_detected and not entry["lfs"]:
                summary.bullet("LFS objects were detected but entry.lfs is false, so only git refs will be mirrored")
            if entry["lfs"]:
                ensure_git_lfs_available()
                fetch_lfs(mirror_dir)

            target_errors: list[str] = []
            for platform_name, target in enabled_targets:
                namespace = target["namespace"]
                repo_name = target["name"]
                summary.heading(platform_name)
                try:
                    client = build_target_client(platform_name)
                    repo_info, created = client.ensure_repo(target, repo_data)
                    summary.bullet(
                        f"{platform_name} repo {namespace}/{repo_name}: {'created' if created else 'already existed'} "
                        f"({repo_visibility(repo_info)})"
                    )

                    remote_name = f"push-{platform_name}"
                    add_remote(mirror_dir, remote_name, client.authenticated_git_url(namespace, repo_name))
                    push_mirror(mirror_dir, remote_name)
                    summary.bullet(f"{platform_name}: git refs mirrored")

                    if entry["lfs"]:
                        push_lfs(mirror_dir, remote_name)
                        summary.bullet(f"{platform_name}: git lfs objects pushed")

                    if target["sync_releases"]:
                        release_stats = sync_releases_to_target(
                            client,
                            target,
                            source_client,
                            source_releases,
                            asset_cache_root,
                            downloaded_assets,
                            summary,
                        )
                        summary.bullet(
                            f"{platform_name}: releases created={release_stats['created']} "
                            f"recreated={release_stats['recreated']} updated={release_stats['updated']} "
                            f"skipped={release_stats['skipped']}"
                        )
                    else:
                        summary.bullet(f"{platform_name}: release sync disabled for this target")
                except Exception as exc:
                    message = f"{platform_name} failed: {exc}"
                    summary.bullet(message)
                    target_errors.append(message)

            if target_errors:
                raise SyncError("\n".join(target_errors))
    finally:
        summary.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mirror GitHub repositories into GitCode and Gitee")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_matrix_parser = subparsers.add_parser("build-matrix", help="Resolve config into a GitHub Actions matrix")
    build_matrix_parser.add_argument("--config", required=True, help="Path to config/repos.yaml")
    build_matrix_parser.add_argument("--mode", choices=("manifest", "adhoc"), default="manifest")
    build_matrix_parser.add_argument("--entry-ids", default="", help="Comma-separated manifest IDs")
    build_matrix_parser.add_argument("--repo-spec-json", default="", help="Adhoc entry JSON payload")
    build_matrix_parser.add_argument("--github-output", default="", help="Optional path to $GITHUB_OUTPUT")
    build_matrix_parser.set_defaults(func=build_matrix)

    sync_parser = subparsers.add_parser("sync-entry", help="Sync a single normalized repo entry")
    sync_parser.add_argument("--entry-json", required=True, help="Normalized repo entry JSON")
    sync_parser.set_defaults(func=handle_sync_entry)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except SyncError as exc:
        log(f"ERROR: {exc}")
        return 1
    except Exception:  # pragma: no cover - defensive crash path
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
