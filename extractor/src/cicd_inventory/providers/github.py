"""GitHub provider — fetches workflow files via the GitHub GraphQL API."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml

from cicd_inventory.models import ActionReference, JobInfo, WorkflowRecord
from cicd_inventory.providers.base import RepositoryProvider
from cicd_inventory.queries import REPO_DISCOVERY_QUERY, RepoRef, build_workflow_batch_query

log = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.github.com/graphql"
_BATCH_SIZE = 20  # repos per aliased batch query
_RATE_LIMIT_BUFFER = 200  # sleep when remaining points drop below this


class GitHubProvider(RepositoryProvider):
    """Concrete provider that fetches workflows from GitHub via GraphQL."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        }
        self._parse_errors: list[str] = []

    @property
    def parse_errors(self) -> list[str]:
        """Messages for workflow files that could not be parsed into a record."""
        return list(self._parse_errors)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def fetch_workflows(self, orgs: list[str]) -> list[WorkflowRecord]:
        async with httpx.AsyncClient(headers=self._headers, timeout=60) as client:
            self._client = client
            records: list[WorkflowRecord] = []
            for org in orgs:
                log.info("Discovering repos in %s …", org)
                repos = await self._discover_repos(org)
                log.info("Found %d repos in %s, fetching workflows …", len(repos), org)
                org_records = await self._fetch_all_workflows(repos)
                records.extend(org_records)
                log.info("Collected %d workflow records from %s", len(org_records), org)
            return records

    # ------------------------------------------------------------------
    # Repo discovery (paginated)
    # ------------------------------------------------------------------

    async def _discover_repos(self, org: str) -> list[RepoRef]:
        repos: list[RepoRef] = []
        cursor: str | None = None

        while True:
            data = await self._graphql(
                REPO_DISCOVERY_QUERY,
                variables={"org": org, "cursor": cursor},
            )
            await self._check_rate_limit(data)

            org_data = data.get("organization")
            if not org_data:
                log.warning("No organization data returned for %s", org)
                break

            repo_conn = org_data["repositories"]
            for node in repo_conn["nodes"]:
                if node["isArchived"]:
                    continue
                if node["defaultBranchRef"] is None:
                    continue  # empty repo
                repos.append(
                    RepoRef(
                        owner=org,
                        name=node["name"],
                        default_branch=node["defaultBranchRef"]["name"],
                    )
                )

            page_info = repo_conn["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        return repos

    # ------------------------------------------------------------------
    # Workflow fetching (batched alias queries)
    # ------------------------------------------------------------------

    async def _fetch_all_workflows(self, repos: list[RepoRef]) -> list[WorkflowRecord]:
        records: list[WorkflowRecord] = []
        for i in range(0, len(repos), _BATCH_SIZE):
            batch = repos[i : i + _BATCH_SIZE]
            batch_records = await self._fetch_workflow_batch(batch)
            records.extend(batch_records)
        return records

    async def _fetch_workflow_batch(self, repos: list[RepoRef]) -> list[WorkflowRecord]:
        # Fire the GraphQL workflow-tree query and all REST run-status calls concurrently.
        results = await asyncio.gather(
            self._graphql(build_workflow_batch_query(repos)),
            *[self._fetch_run_statuses(repo) for repo in repos],
        )
        data: dict[str, Any] = results[0]
        status_maps: list[dict[str, str]] = list(results[1:])
        await self._check_rate_limit(data)

        # Build a {repo_name: {workflow_filename: conclusion}} lookup.
        repo_statuses: dict[str, dict[str, str]] = {repo.name: sm for repo, sm in zip(repos, status_maps)}

        records: list[WorkflowRecord] = []
        for i, repo in enumerate(repos):
            alias = f"repo_{i}"
            repo_data = data.get(alias)
            if not repo_data:
                continue

            tree = repo_data.get("object")
            if not tree:
                continue  # no .github/workflows directory

            entries = tree.get("entries", [])
            for entry in entries:
                filename: str = entry["name"]
                if not (filename.endswith(".yml") or filename.endswith(".yaml")):
                    continue
                if entry["type"] != "blob":
                    continue

                blob = entry.get("object")
                if not blob or not blob.get("text"):
                    continue

                raw_text: str = blob["text"]
                record = self._parse_workflow_yaml(raw_text, repo.owner, repo.name, filename)
                if record:
                    record.last_run_status = repo_statuses.get(repo.name, {}).get(filename)
                    records.append(record)

        return records

    async def _fetch_run_statuses(self, repo: RepoRef) -> dict[str, str]:
        """Return {workflow_filename: conclusion} for the most recent run of each workflow in a repo.

        Uses the GitHub REST API (one call per repo) to find the latest
        completed or in-progress run for every workflow file.
        """
        url = f"https://api.github.com/repos/{repo.owner}/{repo.name}/actions/runs"
        try:
            resp = await self._client.get(url, params={"per_page": "100", "branch": repo.default_branch})
            if resp.status_code in (403, 404):
                return {}
            resp.raise_for_status()
            runs: list[dict] = resp.json().get("workflow_runs", [])
        except Exception as exc:
            log.warning("Could not fetch run statuses for %s/%s: %s", repo.owner, repo.name, exc)
            return {}

        # The list is newest-first; keep only the first (most recent) entry per filename.
        statuses: dict[str, str] = {}
        for run in runs:
            path: str = run.get("path", "")
            filename = path.rsplit("/", 1)[-1] if path else ""
            if filename and filename not in statuses:
                # conclusion is None while the run is still queued / in_progress
                statuses[filename] = run.get("conclusion") or "running"
        return statuses

    # ------------------------------------------------------------------
    # YAML → WorkflowRecord
    # ------------------------------------------------------------------

    def _parse_workflow_yaml(
        self,
        text: str,
        org: str,
        repo: str,
        filename: str,
    ) -> WorkflowRecord | None:
        # A single unparseable file must never abort the run, and safe_load raises
        # more than YAMLError — e.g. a bare out-of-range date such as `2024-02-30`
        # reaches datetime.date() and comes back as a plain ValueError.
        try:
            doc: Any = yaml.safe_load(text) or {}
            if not isinstance(doc, dict):
                raise TypeError(f"expected a top-level YAML mapping, got {type(doc).__name__}")
            return self._build_record(doc, text, org, repo, filename)
        except Exception as exc:
            return self._skip(org, repo, filename, exc)

    def _skip(self, org: str, repo: str, filename: str, exc: Exception) -> None:
        """Record and log a workflow file that could not be turned into a record."""
        message = f"{org}/{repo}/{filename}: {type(exc).__name__}: {exc}"
        log.warning("Skipping unparseable workflow %s", message)
        self._parse_errors.append(message)
        return None

    def _build_record(
        self,
        doc: dict[str, Any],
        text: str,
        org: str,
        repo: str,
        filename: str,
    ) -> WorkflowRecord:
        workflow_name: str = doc.get("name") or filename
        # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
        # We must check both the string "on" (for quoted keys) and True (for
        # the common unquoted form used in virtually all GitHub Actions files).
        on_triggers: list[str] = _extract_triggers(doc.get("on") or doc.get(True, {}))
        jobs: list[JobInfo] = _extract_jobs(doc.get("jobs", {}))

        repo_url = f"https://github.com/{org}/{repo}"
        return WorkflowRecord(
            id=f"{org}/{repo}/{filename}",
            org=org,
            repo_name=repo,
            repo_url=repo_url,
            workflow_file=filename,
            workflow_path=f".github/workflows/{filename}",
            workflow_name=workflow_name,
            on_triggers=on_triggers,
            jobs=jobs,
            status_badge_url=f"{repo_url}/actions/workflows/{filename}/badge.svg",
            last_fetched=datetime.now(UTC),
            raw_yaml=text,
        )

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    async def _check_rate_limit(self, data: dict[str, Any]) -> None:
        rate = data.get("rateLimit")
        if not rate:
            return
        remaining: int = rate.get("remaining", 9999)
        log.debug("GitHub rate limit: %d remaining", remaining)
        if remaining < _RATE_LIMIT_BUFFER:
            reset_at_str: str = rate["resetAt"]
            reset_at = datetime.fromisoformat(reset_at_str.replace("Z", "+00:00"))
            sleep_secs = max(0, (reset_at - datetime.now(UTC)).total_seconds()) + 5
            log.warning(
                "Rate limit low (%d remaining). Sleeping %.0fs until %s …",
                remaining,
                sleep_secs,
                reset_at_str,
            )
            await asyncio.sleep(sleep_secs)

    # ------------------------------------------------------------------
    # HTTP helper with retry
    # ------------------------------------------------------------------

    async def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        for attempt in range(3):
            try:
                resp = await self._client.post(_GRAPHQL_URL, json=payload)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise
                wait = 2**attempt
                log.warning("Transport error (attempt %d/3): %s — retrying in %ds", attempt + 1, exc, wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (429, 502, 503):
                if attempt == 2:
                    resp.raise_for_status()
                wait = 2**attempt
                log.warning("HTTP %d (attempt %d/3) — retrying in %ds", resp.status_code, attempt + 1, wait)
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            body: dict[str, Any] = resp.json()

            if errors := body.get("errors"):
                # GraphQL partial errors — log but don't abort; data may still be present
                for err in errors:
                    log.warning("GraphQL error: %s", err.get("message", err))

            return body.get("data", {})

        return {}


# ---------------------------------------------------------------------------
# YAML parsing helpers
# ---------------------------------------------------------------------------


def _extract_triggers(on_field: Any) -> list[str]:
    """Normalise the ``on:`` key into a flat list of event names."""
    if on_field is None:
        return []
    if isinstance(on_field, str):
        return [on_field]
    if isinstance(on_field, list):
        return [str(t) for t in on_field]
    if isinstance(on_field, dict):
        return list(on_field.keys())
    return [str(on_field)]


def _normalize_runs_on(value: Any) -> str | list[str]:
    """Coerce a `runs-on:` value into the string / list-of-strings the model accepts.

    GitHub also allows a mapping form for runner groups, which is neither::

        runs-on:
          group: cvmfs-publish
          labels: [cvmfs, self-hosted]
    """
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        group = value.get("group")
        labels = value.get("labels")
        if labels is None:
            label_list: list[str] = []
        elif isinstance(labels, list):
            label_list = [str(label) for label in labels]
        else:
            label_list = [str(labels)]
        parts = ([f"group:{group}"] if group else []) + label_list
        return parts or "unknown"
    return str(value)


def _extract_jobs(jobs_field: Any) -> list[JobInfo]:
    if not isinstance(jobs_field, dict):
        return []
    result: list[JobInfo] = []
    for job_id, job_def in jobs_field.items():
        if not isinstance(job_def, dict):
            continue
        steps: list[dict] = job_def.get("steps", []) or []
        actions: list[ActionReference] = []
        for step in steps:
            if isinstance(step, dict) and (uses := step.get("uses")):
                actions.append(ActionReference(uses=uses, name=step.get("name")))
        runs_on = _normalize_runs_on(job_def.get("runs-on"))
        result.append(
            JobInfo(
                job_id=str(job_id),
                name=job_def.get("name"),
                runs_on=runs_on,
                steps_count=len(steps),
                actions_used=actions,
            )
        )
    return result
