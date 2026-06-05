"""The dashboard HTTP server — a tiny dependency-free asyncio responder.

Runs on the worker's own event loop (same process, same pod) and reuses the
worker's connected Temporal client. On each GET it fans the source readers out
concurrently, assembles the view, and renders one self-contained page; it stores
nothing between requests. GET-only, ``Connection: close``, ``Cache-Control:
no-store`` — built for a single viewer behind ``kubectl port-forward``, not the
public internet. Every reader degrades to an error string, so a source being
down yields a page with a red dot, never a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from ynab_agent.dashboard import (
    agentmail_source,
    clickhouse_source,
    github_source,
    read_model,
    render,
    temporal_source,
    ynab_source,
)
from ynab_agent.settings import DashboardSettings, GitHubSettings

if TYPE_CHECKING:
    from temporalio.client import Client

_log = logging.getLogger("ynab_agent.dashboard")

_READ_TIMEOUT: Final = 15.0
_MAX_HEAD_BYTES: Final = 64 * 1024


def _repo() -> str:
    """The watched repo, degrading to a default if config is unreadable."""
    try:
        return GitHubSettings().repo
    except Exception:  # never let a config read fail the page
        return "mseeks/ynab-agent"


async def build_html(client: Client) -> str:
    """Derive the whole view live and render it (the per-request work)."""
    now = datetime.now(UTC)
    temporal, clickhouse, ynab, agentmail, github = await asyncio.gather(
        temporal_source.fetch(client),
        clickhouse_source.fetch(),
        ynab_source.fetch(),
        agentmail_source.fetch(),
        github_source.fetch(),
    )
    # A dependent second hop: the awaiting ids are known only after Temporal
    # answers, so resolve them to YNAB facts now (best-effort; an empty map just
    # leaves the queue showing short ids — see ynab_source.resolve_queue).
    readout, _ = temporal
    queue_facts = await ynab_source.resolve_queue(
        tuple(item.ident for item in readout.awaiting)
    )
    model = read_model.assemble(
        now=now,
        repo=_repo(),
        temporal=temporal,
        clickhouse=clickhouse,
        ynab=ynab,
        agentmail=agentmail,
        github=github,
        queue_facts=queue_facts,
    )
    return render.page(model)


def _parse_path(request_line: bytes) -> tuple[str, str]:
    """Return ``(method, path)`` from a raw HTTP request line."""
    parts = request_line.decode("latin-1", "replace").split()
    if len(parts) < 2:
        return "", "/"
    return parts[0].upper(), parts[1].split("?", 1)[0]


async def _drain_headers(reader: asyncio.StreamReader) -> None:
    """Read and discard the request headers, bounded in size and time."""
    total = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT)
        total += len(line)
        if line in (b"\r\n", b"\n", b"") or total > _MAX_HEAD_BYTES:
            return


async def _respond(
    writer: asyncio.StreamWriter,
    status: str,
    content_type: str,
    body: bytes,
) -> None:
    """Write a complete HTTP/1.1 response and close the connection."""
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")
    writer.write(head + body)
    await writer.drain()


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    client: Client,
) -> None:
    """Serve one request: route a GET to the dashboard, else 404/405/500."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT)
        await _drain_headers(reader)
        method, path = _parse_path(request_line)
        if method != "GET":
            await _respond(writer, "405 Method Not Allowed", "text/plain", b"")
        elif path in ("/", "/index.html", "/dashboard"):
            html = await build_html(client)
            await _respond(
                writer, "200 OK", "text/html; charset=utf-8", html.encode()
            )
        elif path == "/healthz":
            await _respond(writer, "200 OK", "text/plain", b"ok")
        else:
            await _respond(writer, "404 Not Found", "text/plain", b"")
    except (TimeoutError, ConnectionError):
        pass
    except Exception as exc:  # never let a request take the worker down
        _log.exception("dashboard request failed")
        with contextlib.suppress(Exception):
            await _respond(
                writer,
                "500 Internal Server Error",
                "text/html; charset=utf-8",
                _error_html(exc).encode(),
            )
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


def _error_html(exc: Exception) -> str:
    """A minimal error page (the detail is for a human at a port-forward)."""
    from html import escape

    return (
        "<!doctype html><meta charset=utf-8>"
        "<title>ynab-agent &middot; error</title>"
        '<body style="font:15px system-ui;max-width:640px;margin:40px auto">'
        "<h1>dashboard error</h1><p>Could not build the read-model.</p>"
        f"<pre>{escape(type(exc).__name__)}: {escape(str(exc))}</pre></body>"
    )


async def start(client: Client) -> asyncio.Server:
    """Start the dashboard server on the configured host/port (returns it)."""
    settings = DashboardSettings()

    async def on_connect(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _handle(reader, writer, client)

    server = await asyncio.start_server(
        on_connect, host=settings.host, port=settings.port
    )
    _log.info("dashboard listening on %s:%d", settings.host, settings.port)
    return server
