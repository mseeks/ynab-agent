"""GitHub reader: recent pull requests + their CI (the deploy panel).

A couple of read-only REST calls (the source-owns-its-IO pattern, like froot's):
the repo's recent PRs and, for each, the head commit's check-run conclusion. The
token needs no write scope; it is optional, so an unset token degrades the panel
to a red dot rather than failing the page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

import httpx

from ynab_agent.dashboard.model import Deploy, PrRow
from ynab_agent.settings import GitHubSettings

_API: Final = "https://api.github.com"
_API_VERSION: Final = "2022-11-28"
_TIMEOUT: Final = 15.0
_MAX_PRS: Final = 8


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _state(payload: dict[str, Any]) -> str:
    if payload.get("merged_at"):
        return "merged"
    if payload.get("state") == "open":
        return "open"
    return "closed"


def _ci_of(conclusions: list[Any]) -> str | None:
    """Reduce a head commit's check-run conclusions to one CI verdict."""
    if not conclusions:
        return None
    values = {str(c) for c in conclusions}
    if "failure" in values or "timed_out" in values:
        return "failed"
    if None in conclusions or "" in values:
        return "running"
    if values <= {"success", "skipped", "neutral"}:
        return "passed"
    return "running"


async def _ci(client: httpx.AsyncClient, repo: str, sha: str) -> str | None:
    """The head commit's combined check-run conclusion, best-effort."""
    try:
        resp = await client.get(f"/repos/{repo}/commits/{sha}/check-runs")
        resp.raise_for_status()
        runs = resp.json().get("check_runs")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(runs, list) or not runs:
        return None
    return _ci_of([r.get("conclusion") for r in runs if isinstance(r, dict)])


async def fetch() -> tuple[Deploy, str | None]:
    """Read recent PRs + CI for the configured repo; never raises."""
    settings = GitHubSettings()
    if settings.token is None:
        return Deploy(), "off"
    headers = {
        "Authorization": f"Bearer {settings.token.get_secret_value()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
    }
    repo = settings.repo
    prs: list[PrRow] = []
    try:
        async with httpx.AsyncClient(
            base_url=_API, timeout=_TIMEOUT, headers=headers
        ) as client:
            resp = await client.get(
                f"/repos/{repo}/pulls",
                params={
                    "state": "all",
                    "per_page": _MAX_PRS,
                    "sort": "created",
                    "direction": "desc",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    head = row.get("head")
                    sha = head.get("sha") if isinstance(head, dict) else None
                    ci = (
                        await _ci(client, repo, sha)
                        if isinstance(sha, str)
                        else None
                    )
                    prs.append(
                        PrRow(
                            number=int(row["number"]),
                            title=str(row.get("title", "")),
                            state=_state(row),
                            ci=ci,
                            url=str(row.get("html_url", "")),
                            when=_parse_dt(row.get("created_at")),
                        )
                    )
    except Exception as exc:  # never raise into gather — degrade to an error
        return Deploy(prs=tuple(prs)), f"{type(exc).__name__}: {exc}"
    return Deploy(prs=tuple(prs)), None
