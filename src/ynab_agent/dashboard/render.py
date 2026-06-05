"""Render the view-model to one self-contained HTML page (pure).

All CSS is inline, there is no JavaScript, and the page makes no network request
of its own — it is a static projection of an already-computed
:class:`~ynab_agent.dashboard.model.DashboardModel`. Every dynamic value is
HTML-escaped at the boundary. Ordering is ops-first (is it alive → what's
flowing → earned autonomy → the human's queue → money → conversations → inbound
→ telemetry → failures → deploy), so it reads top to bottom.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ynab_agent.dashboard.model import (
        DashboardModel,
        Lifecycle,
        RunTelemetry,
    )

_CSS = """
:root{--fg:#1a1a1a;--mut:#6b6b6b;--line:#e4e4e4;--bg:#fff;--ok:#1a7f37;
--warn:#9a6700;--bad:#cf222e;--accent:#0969da;--card:#f2f2f2}
@media(prefers-color-scheme:dark){:root{--fg:#e6e6e6;--mut:#9a9a9a;
--line:#262626;--bg:#0d0d0d;--ok:#3fb950;--warn:#d29922;--bad:#f85149;
--accent:#58a6ff;--card:#1b1b1b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:34px 20px 72px}
h1{font-size:22px;margin:0;letter-spacing:-.01em}
.tag{color:var(--mut);margin:3px 0 0;font-size:13px}
.meta{color:var(--mut);font-size:12px;margin:12px 0 0}
.sources{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 0;font-size:12px}
section{margin:30px 0 0}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
color:var(--mut);margin:0 0 12px;font-weight:600;
border-bottom:1px solid var(--line);padding-bottom:6px}
.stats{display:flex;flex-wrap:wrap;gap:26px}
.stat .n{font-size:26px;font-weight:600;line-height:1.1}
.stat .l{color:var(--mut);font-size:12px;margin-top:2px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.dot.bad{background:var(--bad)}.dot.mute{background:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 12px 6px 0;vertical-align:top;
border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.04em}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.row{display:flex;align-items:baseline;gap:8px;padding:4px 0}
.mut{color:var(--mut)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.note{color:var(--mut);font-size:12px;margin:10px 0 0}
.barrow{display:grid;grid-template-columns:140px 1fr 34px;gap:10px;
align-items:center;margin:5px 0}
.barrow .lab{font-size:12px;color:var(--mut)}
.barrow .num{font-size:12px;text-align:right;font-variant-numeric:tabular-nums}
.bar{background:var(--card);border-radius:4px;height:16px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--accent);min-width:2px}
.ladder{display:flex;height:24px;border-radius:5px;overflow:hidden;
margin:2px 0 10px;font-size:11px;font-weight:600}
.ladder>span{display:flex;align-items:center;justify-content:center;color:#fff;
white-space:nowrap;min-width:0}
.ladder .obs{background:var(--mut)}.ladder .elig{background:var(--warn)}
.ladder .bless{background:var(--ok)}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
font-weight:600}
.pill.ok{background:rgba(63,185,80,.16);color:var(--ok)}
.pill.warn{background:rgba(210,153,34,.16);color:var(--warn)}
.pill.mute{background:var(--card);color:var(--mut)}
footer{margin:44px 0 0;padding-top:16px;border-top:1px solid var(--line);
color:var(--mut);font-size:12px;line-height:1.7}
footer b{color:var(--fg);font-weight:600}
"""

# The canonical W2 lifecycle order for the funnel (any other state is appended).
_LIFECYCLE_ORDER = (
    "discovered",
    "enriching",
    "hold_amazon",
    "awaiting_human",
    "open",
    "lapsed",
    "revising",
)
_TRUST_CLASS = {"trusted": "ok", "confirmed": "warn", "suggested": "mute"}
_CI_CLASS = {"passed": "ok", "failed": "bad", "running": "warn"}
_STATE_CLASS = {"merged": "ok", "open": "warn", "closed": "mut"}


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "—"
    secs = (now - _aware(when)).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 129600:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _dot(kind: str) -> str:
    return f'<span class="dot {kind}"></span>'


def _stat(n: object, label: str) -> str:
    return (
        f'<div class="stat"><div class="n">{escape(str(n))}</div>'
        f'<div class="l">{escape(label)}</div></div>'
    )


def _pill(text: str, cls: str) -> str:
    return f'<span class="pill {cls}">{escape(text)}</span>'


def _header(model: DashboardModel) -> str:
    stamp = model.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    dots = "".join(
        f"<span>{_dot('ok' if s.ok else 'bad')}{escape(s.name)} "
        f'<span class="mut">{escape(s.detail)}</span></span>'
        for s in model.sources
    )
    return (
        "<header><h1>ynab-agent</h1>"
        '<p class="tag">durable transaction triage &middot; '
        "earned-autonomy read-model</p>"
        f'<p class="meta">{escape(model.repo)} &middot; generated '
        f"{escape(stamp)} &middot; derived live, stored nowhere</p>"
        f'<div class="sources">{dots}</div></header>'
    )


def _heartbeat(model: DashboardModel) -> str:
    h = model.heartbeat
    now = model.generated_at
    poll_dot = "ok" if h.poll_live else "bad"
    poll = (
        f'<div class="row">{_dot(poll_dot)}<span class="mono">W1 poll</span> '
        f'<span class="mut">&middot; {escape(h.poll_status)} &middot; last '
        f"{escape(_ago(h.poll_last_start, now))}</span></div>"
    )
    if h.worker_last_span is not None:
        span_ago = (now - _aware(h.worker_last_span)).total_seconds()
        wdot = "ok" if span_ago < 3600 else "warn"
        worker = (
            f'<div class="row">{_dot(wdot)}<span class="mono">worker</span> '
            f'<span class="mut">&middot; last span '
            f"{escape(_ago(h.worker_last_span, now))}</span></div>"
        )
    else:
        worker = (
            f'<div class="row">{_dot("mute")}'
            '<span class="mono">worker</span> '
            '<span class="mut">&middot; no telemetry</span></div>'
        )
    return f"<section><h2>Is it alive?</h2>{poll}{worker}</section>"


def _lifecycle(model: DashboardModel) -> str:
    lc: Lifecycle = model.lifecycle
    counts = {s.state: s.count for s in lc.states}
    ordered = [s for s in _LIFECYCLE_ORDER if s in counts]
    ordered += [s for s in sorted(counts) if s not in _LIFECYCLE_ORDER]
    peak = max((counts[s] for s in ordered), default=0) or 1
    rows = "".join(
        f'<div class="barrow"><div class="lab">{escape(s)}</div>'
        f'<div class="bar"><span style="width:{counts[s] * 100 // peak}%">'
        f'</span></div><div class="num">{counts[s]}</div></div>'
        for s in ordered
    )
    if not ordered:
        rows = '<p class="note">No transactions in flight.</p>'
    stats = "".join(
        (
            _stat(lc.in_flight, "in flight"),
            _stat(lc.archived, "archived (recent)"),
            _stat(lc.terminated, "terminated (recent)"),
        )
    )
    return (
        "<section><h2>Transaction lifecycle</h2>"
        f'<div class="stats">{stats}</div>'
        f'<div style="margin-top:14px">{rows}</div></section>'
    )


def _ladder(model: DashboardModel) -> str:
    a = model.autonomy
    total = a.observe + a.eligible + a.blessed
    if total:
        segs = "".join(
            f'<span class="{cls}" style="flex:{n}" title="{label}: {n}">'
            f"{n if n * 100 // total >= 8 else ''}</span>"
            for n, cls, label in (
                (a.observe, "obs", "observe"),
                (a.eligible, "elig", "eligible"),
                (a.blessed, "bless", "blessed"),
            )
            if n
        )
        bar = f'<div class="ladder">{segs}</div>'
    else:
        bar = ""
    stats = "".join(
        (
            _stat(a.observe, "observe"),
            _stat(a.eligible, "eligible"),
            _stat(a.blessed, "blessed (auto)"),
            _stat(len(a.offers), "live offers"),
        )
    )
    if a.rules:
        rows = "".join(
            "<tr>"
            f'<td class="mono">{escape(r.payee)}</td>'
            f'<td class="mono mut">{escape(r.category)}</td>'
            f"<td>{_pill(r.trust, _TRUST_CLASS.get(r.trust, 'mute'))}</td>"
            f"<td>{escape(r.source)}</td>"
            f'<td class="num">{r.hits}</td>'
            f"<td>{'yes' if r.offered else '—'}</td>"
            "</tr>"
            for r in a.rules[:30]
        )
        table = (
            "<table><thead><tr><th>payee</th><th>category</th><th>trust</th>"
            "<th>source</th><th>hits</th><th>offered</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="note">No learned rules yet.</p>'
    note = (
        '<p class="note">Autonomy is earned then granted: a rule becomes '
        "<b>eligible</b> after K consistent confirms, and <b>blessed</b> "
        "(auto-applies) only once the owner accepts the offer. A correction "
        "demotes it back to observe.</p>"
    )
    return (
        "<section><h2>Autonomy ladder</h2>"
        f'<div class="stats">{stats}</div>{bar}{table}{note}</section>'
    )


def _queue(model: DashboardModel) -> str:
    now = model.generated_at
    if not model.queue:
        body = '<p class="note">Nothing awaiting the owner.</p>'
    else:
        rows = "".join(
            "<tr>"
            f"<td>{_pill(q.kind, 'warn')}</td>"
            f'<td class="mono">{escape(q.label)}</td>'
            f'<td class="mut">{escape(_ago(q.since, now))}</td>'
            "</tr>"
            for q in model.queue[:30]
        )
        body = (
            "<table><thead><tr><th>kind</th><th>what</th><th>waiting</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    return f"<section><h2>Awaiting a human</h2>{body}</section>"


def _budget(model: DashboardModel) -> str:
    b = model.budget
    if not b.available:
        return (
            "<section><h2>Budget &middot; YNAB</h2>"
            '<p class="note">Unavailable (YNAB not configured or '
            "unreachable).</p></section>"
        )
    sample = ", ".join(
        f"{escape(t.payee)} {escape(t.amount)}" for t in b.unapproved_sample
    )
    sample_note = f'<p class="note">{escape(sample)}</p>' if sample else ""
    if b.overspent:
        rows = "".join(
            "<tr>"
            f'<td class="mono">{escape(c.name)}</td>'
            f'<td class="bad mono">{escape(c.balance)}</td>'
            "</tr>"
            for c in b.overspent
        )
        over = (
            "<table><thead><tr><th>overspent category</th><th>balance</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        over = '<p class="note">No overspent categories.</p>'
    stats = "".join((_stat(b.unapproved, "unapproved"),))
    return (
        "<section><h2>Budget &middot; YNAB</h2>"
        f'<div class="stats">{stats}</div>{sample_note}'
        f'<div style="margin-top:12px">{over}</div></section>'
    )


def _conversations(model: DashboardModel) -> str:
    now = model.generated_at
    if not model.conversations:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{_pill(c.kind, 'mute')}</td>"
        f"<td>{escape(c.subject)}</td>"
        f'<td class="mut">{escape(c.preview)}</td>'
        f'<td class="mut">{escape(_ago(c.updated_at, now))}</td>'
        "</tr>"
        for c in model.conversations[:12]
    )
    return (
        "<section><h2>Conversations &middot; AgentMail</h2>"
        "<table><thead><tr><th>kind</th><th>subject</th><th>preview</th>"
        f"<th>updated</th></tr></thead><tbody>{rows}</tbody></table></section>"
    )


def _dispatch(model: DashboardModel) -> str:
    d = model.dispatch
    stats = "".join(
        (
            _stat(d.transaction, "→ transaction"),
            _stat(d.offer, "→ offer"),
            _stat(d.receipt, "→ receipt"),
            _stat(d.command, "→ command"),
            _stat(d.quarantine, "quarantined"),
            _stat(d.ignore, "ignored"),
        )
    )
    return (
        "<section><h2>Inbound &middot; W3 dispatch "
        f'({d.total} recent)</h2><div class="stats">{stats}</div></section>'
    )


def _telemetry(model: DashboardModel) -> str:
    t: RunTelemetry = model.telemetry
    now = model.generated_at
    if not t.available:
        return (
            "<section><h2>Run telemetry &middot; ClickHouse</h2>"
            '<p class="note">Unavailable (not configured or unreachable); '
            "Temporal carries the dashboard regardless.</p></section>"
        )
    if t.activities:
        rows = "".join(
            "<tr>"
            f'<td class="mono">{escape(a.name)}</td>'
            f'<td class="num">{a.count}</td>'
            f'<td class="mut">{a.avg_ms:.0f} ms</td>'
            f'<td class="mut">{a.max_ms:.0f} ms</td>'
            "</tr>"
            for a in t.activities
        )
        table = (
            "<table><thead><tr><th>activity</th><th>runs</th><th>avg</th>"
            f"<th>max</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="note">No spans in the window.</p>'
    errors = (
        "".join(
            f'<div class="row mono bad">{escape(e)}</div>'
            for e in t.recent_errors
        )
        if t.recent_errors
        else ""
    )
    summary = (
        f'<p class="note">{t.total_spans} spans &middot; '
        f"{t.error_spans} errored &middot; last "
        f"{escape(_ago(t.last_activity, now))} &middot; "
        f"{t.window_days}-day window.</p>"
    )
    return (
        "<section><h2>Run telemetry &middot; ClickHouse</h2>"
        f"{summary}{table}{errors}</section>"
    )


def _failures(model: DashboardModel) -> str:
    if not model.failures:
        return ""
    now = model.generated_at
    rows = "".join(
        "<tr>"
        f'<td class="mono">{escape(f.workflow_id)}</td>'
        f'<td class="bad">{escape(f.kind)}</td>'
        f'<td class="mut">{escape(f.reason or "—")}</td>'
        f'<td class="mut">{escape(_ago(f.when, now))}</td>'
        "</tr>"
        for f in model.failures
    )
    return (
        "<section><h2>Failures &middot; where a workflow did not close</h2>"
        "<table><thead><tr><th>workflow</th><th>kind</th><th>reason</th>"
        f"<th>when</th></tr></thead><tbody>{rows}</tbody></table></section>"
    )


def _deploy(model: DashboardModel) -> str:
    now = model.generated_at
    if not model.deploy.prs:
        return ""
    rows = "".join(
        "<tr>"
        f'<td><a href="{escape(p.url, quote=True)}">#{p.number}</a></td>'
        f"<td>{escape(p.title)}</td>"
        f'<td class="{_STATE_CLASS.get(p.state, "mut")}">{escape(p.state)}</td>'
        f"<td>{_ci_cell(p.ci)}</td>"
        f'<td class="mut">{escape(_ago(p.when, now))}</td>'
        "</tr>"
        for p in model.deploy.prs
    )
    return (
        "<section><h2>Deploy &middot; GitHub</h2>"
        "<table><thead><tr><th>pr</th><th>title</th><th>state</th><th>ci</th>"
        f"<th>opened</th></tr></thead><tbody>{rows}</tbody></table></section>"
    )


def _ci_cell(ci: str | None) -> str:
    if ci is None:
        return '<span class="mut">—</span>'
    return f'<span class="{_CI_CLASS.get(ci, "mut")}">{escape(ci)}</span>'


def _footer() -> str:
    return (
        "<footer><b>Safety envelope.</b> Every auto-apply passes the hard "
        "floor (amount ceiling, unreadable amount, per-run/day breaker) and "
        "the earned-autonomy gate (exactly one blessed rule), then a "
        "clean-context "
        "model <b>safety review</b> that can only hold it back. Autonomy is "
        "revoked — the rule demoted — on an explicit correction or a silent "
        "in-YNAB edit. Everything above is derived on this request from "
        "Temporal, YNAB, ClickHouse, AgentMail, and GitHub; nothing is stored. "
        "Reload to recompute.</footer>"
    )


def page(model: DashboardModel) -> str:
    """Render the whole dashboard as one self-contained HTML document."""
    parts = (
        _header(model),
        _heartbeat(model),
        _lifecycle(model),
        _ladder(model),
        _queue(model),
        _budget(model),
        _conversations(model),
        _dispatch(model),
        _telemetry(model),
        _failures(model),
        _deploy(model),
        _footer(),
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>ynab-agent &middot; ops</title>"
        f"<style>{_CSS}</style></head><body><main>"
        + "".join(parts)
        + "</main></body></html>"
    )
