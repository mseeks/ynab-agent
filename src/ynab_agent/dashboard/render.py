"""Render the view-model to one self-contained HTML page (pure).

All CSS is inline, there is no JavaScript, and the page makes no network request
of its own — it is a static projection of an already-computed
:class:`~ynab_agent.dashboard.model.DashboardModel`. Every dynamic value is
HTML-escaped at the boundary.

The page is organized around the operator's questions, not the subsystems the
data came from: a masthead verdict, a plain-English **state of things**, then
**needs you** (the only acted-upon queue), **is it working** (health + flow),
**what it's done** (the conversation feed), the **budget**, and finally the
operator detail folded into progressive-disclosure rails. Structure comes from
hairlines, whitespace, type, and a single accent — not boxes; semantic colour
lives in dots and pills, not fills.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING

from ynab_agent.dashboard.read_model import STALE_WORKER_SECS
from ynab_agent.domain.config import HOUSEHOLD_TZ

if TYPE_CHECKING:
    from ynab_agent.dashboard.model import (
        DashboardModel,
        Lifecycle,
        QueueItem,
        RunTelemetry,
    )

_CSS = """
:root{--fg:#1a1a1a;--mut:#6b6b6b;--faint:#8a8a8a;--line:#e6e6e6;--bg:#fff;
--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--accent:#0969da;--card:#f6f8fa;
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,ui-serif,serif}
@media(prefers-color-scheme:dark){:root{--fg:#e6e6e6;--mut:#9a9a9a;
--faint:#7a7a7a;--line:#262626;--bg:#0d0d0d;--ok:#3fb950;--warn:#d29922;
--bad:#f85149;--accent:#58a6ff;--card:#161b22}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:820px;margin:0 auto;padding:30px 20px 72px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.mut{color:var(--mut)}.faint{color:var(--faint)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.accent{color:var(--accent)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex:none;
vertical-align:baseline}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.dot.bad{background:var(--bad)}.dot.mute{background:var(--faint)}
.dot.accent{background:var(--accent)}

/* Masthead — wordmark + the one composite verdict + the few facts. */
.mast{display:flex;align-items:flex-start;gap:14px;flex-wrap:wrap}
.mast h1{font-size:19px;margin:0;letter-spacing:-.01em;font-weight:650}
.mast .tag{color:var(--mut);font-size:12px;margin:2px 0 0}
.verdict{display:inline-flex;align-items:center;gap:7px;font-weight:650;
font-size:15px}
.verdict .dot{width:11px;height:11px}
.verdict.ok{color:var(--ok)}.verdict.warn{color:var(--warn)}
.verdict.bad{color:var(--bad)}
.facts{margin-left:auto;text-align:right;color:var(--mut);font-size:12px;
line-height:1.7}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0 0}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;
border-radius:999px;border:1px solid var(--line);font-size:12px;font-weight:550}
.chip.ok{border-color:transparent;color:var(--ok);
background:color-mix(in srgb,var(--ok) 14%,transparent)}
.chip.warn{border-color:transparent;color:var(--warn);
background:color-mix(in srgb,var(--warn) 16%,transparent)}
.chip.bad{border-color:transparent;color:var(--bad);
background:color-mix(in srgb,var(--bad) 14%,transparent)}
.srcs{display:flex;flex-wrap:wrap;gap:14px;margin:14px 0 0;font-size:11px;
color:var(--mut)}
.srcs .dot{margin-right:5px}

/* Section = an 11px uppercase label with a hairline underline. */
section{margin:30px 0 0}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;
color:var(--mut);margin:0 0 13px;font-weight:650;padding-bottom:6px;
border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:8px}
h2 .h2-aside{margin-left:auto;font-size:11px;font-weight:500;letter-spacing:0;
text-transform:none;color:var(--faint)}

/* State of things — the one serif voice, a lead with an accent rule. */
.narr{border-left:3px solid var(--line);padding:2px 0 2px 15px;margin:26px 0 0}
.narr.ok{border-color:var(--ok)}.narr.warn{border-color:var(--warn)}
.narr.bad{border-color:var(--bad)}
.narr .head{font-weight:650;font-size:15px;margin:0 0 4px}
.narr .body{font-family:var(--serif);font-size:16.5px;line-height:1.5;
margin:0;color:var(--fg);max-width:62ch}

/* Needs you — the humanized queue. */
.qrow{display:flex;align-items:baseline;gap:11px;padding:9px 0;
border-bottom:1px solid var(--line)}
.qrow:last-child{border-bottom:0}
.qrow .dot{position:relative;top:1px}
.q-main{flex:1;min-width:0}
.q-title{display:block;font-weight:550;overflow-wrap:anywhere}
.q-sub{display:block;color:var(--mut);font-size:12.5px;margin-top:1px}
.q-age{color:var(--faint);font-size:12px;white-space:nowrap;
font-variant-numeric:tabular-nums}
.tag-pill{display:inline-block;padding:0 7px;border-radius:10px;
font-size:10.5px;font-weight:650;text-transform:uppercase;letter-spacing:.04em;
background:var(--card);color:var(--mut);vertical-align:1px}
.tag-pill.offer{color:var(--accent);
background:color-mix(in srgb,var(--accent) 14%,transparent)}
.empty{display:flex;align-items:center;gap:10px;color:var(--mut);
font-size:14px;padding:6px 0}
.empty .big{font-size:20px;line-height:1}
.note{color:var(--mut);font-size:12.5px;margin:11px 0 0}

/* Is it working — health pills + a single flow bar. */
.kpis{display:flex;flex-wrap:wrap;gap:18px;margin:0 0 16px}
.kpi{display:inline-flex;align-items:baseline;gap:7px;font-size:13px}
.kpi b{font-weight:600}
.flow{display:flex;height:22px;border-radius:5px;overflow:hidden;
margin:4px 0 10px;background:var(--card)}
.flow>span{display:block;min-width:2px}
.legend{display:flex;flex-wrap:wrap;gap:7px 16px;font-size:12px;
color:var(--mut)}
.legend .it{display:inline-flex;align-items:baseline;gap:6px}
.legend b{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}

/* What it's done — the conversation feed. */
.feed{display:flex;flex-direction:column}
.frow{display:flex;align-items:baseline;gap:10px;padding:8px 0;
border-bottom:1px solid var(--line)}
.frow:last-child{border-bottom:0}
.frow .g{width:1.2em;text-align:center;flex:none;color:var(--faint)}
.f-main{flex:1;min-width:0}
.f-title{display:block;overflow-wrap:anywhere}
.f-prev{display:block;color:var(--faint);font-size:12px;margin-top:1px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.f-age{color:var(--faint);font-size:12px;white-space:nowrap}

/* Budget — one tight line + overspent inline. */
.over{display:flex;flex-wrap:wrap;gap:6px 14px;margin:8px 0 0;font-size:13px}
.over .it{display:inline-flex;gap:6px}

/* Rails — progressive disclosure, hairline-topped. */
details.rail{border-top:1px solid var(--line);padding:9px 0 2px}
details.rail>summary{cursor:pointer;list-style:none;display:flex;
align-items:center;gap:8px;font-size:12px;color:var(--mut)}
details.rail>summary::-webkit-details-marker{display:none}
details.rail>summary::before{content:"\\25B8";color:var(--faint);font-size:10px}
details.rail[open]>summary::before{content:"\\25BE"}
details.rail>summary .rail-n{margin-left:auto;color:var(--faint)}
.rail-body{padding:12px 0 6px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:5px 12px 5px 0;vertical-align:top;
border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:10.5px;text-transform:uppercase;
letter-spacing:.04em}
.num{text-align:right;font-variant-numeric:tabular-nums}
footer{margin:40px 0 0;padding-top:15px;border-top:1px solid var(--line);
color:var(--mut);font-size:12px;line-height:1.7}
footer b{color:var(--fg);font-weight:600}
"""

_LIFECYCLE_ORDER = (
    "discovered",
    "enriching",
    "hold_amazon",
    "revising",
    "awaiting_human",
    "open",
    "lapsed",
)
# Each in-flight state's human label + flow colour (a cool ramp: accent =
# the agent is working, warn = it's on you, ok = settled-and-resting).
_STATE_META = {
    "discovered": ("discovered", "var(--faint)"),
    "enriching": ("enriching", "var(--accent)"),
    "hold_amazon": ("holding for Amazon detail", "var(--faint)"),
    "revising": ("revising", "var(--accent)"),
    "awaiting_human": ("awaiting you", "var(--warn)"),
    "open": ("resting (done)", "var(--ok)"),
    "lapsed": ("lapsed", "var(--faint)"),
}
_TRUST_CLASS = {"trusted": "ok", "confirmed": "warn", "suggested": "mute"}
_CI_CLASS = {"passed": "ok", "failed": "bad", "running": "warn"}
_STATE_CLASS = {"merged": "ok", "open": "warn", "closed": "mut"}
_KIND_GLYPH = {"proposal": "✉", "offer": "◆", "thread": "·"}


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "—"
    secs = (now - _aware(when)).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 129600:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _dot(kind: str) -> str:
    return f'<span class="dot {kind}"></span>'


def _meta(state: str) -> tuple[str, str]:
    return _STATE_META.get(state, (state, "var(--faint)"))


# ── Masthead ─────────────────────────────────────────────────────────────────
def _masthead(model: DashboardModel) -> str:
    h = model.health
    now = model.generated_at
    # Show the "as of" time in the household timezone (SPEC §13), so the
    # operator reads it in their own day, with the zone (CDT/CST) made explicit.
    stamp = now.astimezone(HOUSEHOLD_TZ).strftime("%Y-%m-%d %H:%M %Z")

    chips: list[str] = []
    if h.needs_you:
        suffix = "s" if h.needs_you == 1 else ""
        chips.append(
            f'<span class="chip warn">{h.needs_you} need{suffix} you</span>'
        )
    else:
        chips.append('<span class="chip ok">✓ all caught up</span>')
    poll_cls = "ok" if h.poll_live and not h.poll_stale else "bad"
    poll_age = escape(_ago(h.poll_last_start, now))
    stalled = " · stalled" if h.poll_stale else ""
    chips.append(
        f'<span class="chip">{_dot(poll_cls)} poll {poll_age}{stalled}</span>'
    )
    if model.budget.available:
        chips.append(
            f'<span class="chip">{model.budget.unapproved} unapproved</span>'
        )
    if h.real_failures:
        suffix = "s" if h.real_failures != 1 else ""
        chips.append(
            f'<span class="chip bad">{h.real_failures} failure{suffix}</span>'
        )

    src = "".join(
        f"<span>{_dot('ok' if s.ok else ('mute' if s.off else 'bad'))}"
        f"{escape(s.name)} "
        f'<span class="faint">{escape(s.detail)}</span></span>'
        for s in model.sources
    )
    return (
        "<header>"
        '<div class="mast">'
        "<div><h1>ynab-agent &middot; ops</h1>"
        '<p class="tag">durable transaction triage &middot; '
        "earned-autonomy read-model</p></div>"
        f'<div class="facts"><span class="verdict {escape(h.tone)}">'
        f"{_dot(h.tone)}{escape(h.label)}</span><br>{escape(model.repo)}<br>"
        f"{escape(stamp)}</div></div>"
        f'<div class="chips">{"".join(chips)}</div>'
        f'<div class="srcs">{src}</div>'
        "</header>"
    )


# ── State of things ──────────────────────────────────────────────────────────
def _narrative(model: DashboardModel) -> str:
    n = model.narrative
    body = " ".join(n.paragraphs)
    return (
        f'<section class="narr {escape(n.tone)}">'
        f'<p class="head">{escape(n.headline)}</p>'
        f'<p class="body">{escape(body)}</p></section>'
    )


# ── Needs you ────────────────────────────────────────────────────────────────
def _needs_you(model: DashboardModel) -> str:
    now = model.generated_at
    queue = model.needs_you
    if not queue:
        body = (
            '<div class="empty"><span class="big">✓</span>'
            "<span>You're all caught up — nothing is waiting on you."
            "</span></div>"
        )
    else:
        body = "".join(_qrow(q, now) for q in queue[:30])
    if model.handled:
        n = len(model.handled)
        plural = "s" if n != 1 else ""
        verb = "are" if n != 1 else "is"
        body += (
            f'<p class="note">{n} more proposal{plural} you already settled '
            f"in YNAB {verb} winding down — they lapse on their own, no reply "
            "needed.</p>"
        )
    aside = (
        f'<span class="h2-aside">oldest '
        f"{escape(_ago(_oldest(queue), now))}</span>"
        if queue
        else ""
    )
    return f"<section><h2>Needs you{aside}</h2>{body}</section>"


def _oldest(queue: tuple[QueueItem, ...]) -> datetime | None:
    # Normalize to aware before min() — mixed aware/naive comparison raises.
    sinces = [_aware(q.since) for q in queue if q.since is not None]
    return min(sinces) if sinces else None


def _qrow(q: QueueItem, now: datetime) -> str:
    dot = "accent" if q.kind == "offer" else "warn"
    title = escape(q.question or q.label)
    pill = (
        '<span class="tag-pill offer">offer</span> '
        if q.kind == "offer"
        else ""
    )
    sub_bits: list[str] = []
    if q.amount:
        sub_bits.append(escape(q.amount))
    if q.category:
        sub_bits.append(escape(q.category))
    if not sub_bits and q.payee:
        sub_bits.append(escape(q.payee))
    sub = (
        f'<span class="q-sub">{" &middot; ".join(sub_bits)}</span>'
        if sub_bits
        else ""
    )
    age = escape(_ago(q.since, now))
    return (
        f'<div class="qrow">{_dot(dot)}'
        f'<span class="q-main"><span class="q-title">{pill}{title}</span>'
        f'{sub}</span><span class="q-age">{age}</span></div>'
    )


# ── Is it working ────────────────────────────────────────────────────────────
def _working(model: DashboardModel) -> str:
    h = model.health
    now = model.generated_at
    if not h.poll_live:
        poll_word = "stopped"
    elif h.poll_stale:
        poll_word = "stalled — no fresh tick"
    else:
        poll_word = escape(h.poll_status)
    poll_ok = h.poll_live and not h.poll_stale
    poll_age = escape(_ago(h.poll_last_start, now))
    kpis = [
        f'<span class="kpi">{_dot("ok" if poll_ok else "bad")}'
        f'<b>poll</b> <span class="mut">{poll_word} '
        f"&middot; {poll_age}</span></span>"
    ]
    if h.worker_last_span is not None:
        wago = (now - _aware(h.worker_last_span)).total_seconds()
        wdot = "ok" if wago < STALE_WORKER_SECS else "warn"
        span_age = escape(_ago(h.worker_last_span, now))
        kpis.append(
            f'<span class="kpi">{_dot(wdot)}<b>worker</b> '
            f'<span class="mut">span {span_age}</span></span>'
        )
    else:
        kpis.append(
            f'<span class="kpi">{_dot("mute")}<b>worker</b> '
            '<span class="mut">no telemetry</span></span>'
        )
    if h.span_error_rate is not None:
        rate = h.span_error_rate
        rdot = "ok" if rate <= 0.05 else "warn"
        kpis.append(
            f'<span class="kpi">{_dot(rdot)}<b>{rate * 100:.1f}%</b> '
            '<span class="mut">span errors</span></span>'
        )
    return (
        "<section><h2>Is it working?</h2>"
        f'<div class="kpis">{"".join(kpis)}</div>'
        f"{_flow(model.lifecycle)}</section>"
    )


def _flow(lc: Lifecycle) -> str:
    counts = {s.state: s.count for s in lc.states}
    ordered = [s for s in _LIFECYCLE_ORDER if counts.get(s)]
    ordered += [
        s for s in sorted(counts) if counts.get(s) and s not in _LIFECYCLE_ORDER
    ]
    if not ordered:
        return '<p class="empty"><span>No transactions in flight.</span></p>'
    segs = "".join(
        f'<span style="flex:{counts[s]};background:{_meta(s)[1]}" '
        f'title="{escape(_meta(s)[0])}: {counts[s]}"></span>'
        for s in ordered
    )
    legend = "".join(
        '<span class="it"><span class="dot" '
        f'style="background:{_meta(s)[1]}"></span>'
        f"<b>{counts[s]}</b> {escape(_meta(s)[0])}</span>"
        for s in ordered
    )
    note = (
        " — the post-write window runs for weeks, so resting transactions "
        "haven't closed out yet"
        if lc.archived == 0 and counts.get("open")
        else ""
    )
    gloss = (
        ' &middot; <span class="faint">'
        f"{lc.archived} archived{note} &middot; "
        f"{lc.terminated} terminated (recent)</span>"
    )
    return (
        f'<div class="flow">{segs}</div>'
        f'<div class="legend">{legend}{gloss}</div>'
    )


# ── What it's done ───────────────────────────────────────────────────────────
def _activity(model: DashboardModel) -> str:
    now = model.generated_at
    if not model.conversations:
        return ""
    rows = ""
    for c in model.conversations[:10]:
        glyph = _KIND_GLYPH.get(c.kind, "·")
        prev = (
            f'<span class="f-prev">{escape(c.preview)}</span>'
            if c.preview
            else ""
        )
        age = escape(_ago(c.updated_at, now))
        rows += (
            f'<div class="frow"><span class="g">{glyph}</span>'
            f'<span class="f-main"><span class="f-title">'
            f"{escape(c.subject)}</span>{prev}</span>"
            f'<span class="f-age">{age}</span></div>'
        )
    return (
        "<section><h2>What it's done"
        '<span class="h2-aside">recent threads</span></h2>'
        f'<div class="feed">{rows}</div></section>'
    )


# ── Budget ───────────────────────────────────────────────────────────────────
def _budget(model: DashboardModel) -> str:
    b = model.budget
    if not b.available:
        return ""
    if b.overspent:
        items = "".join(
            f'<span class="it"><span class="bad mono">{escape(c.balance)}'
            f"</span> {escape(c.name)}</span>"
            for c in b.overspent
        )
        over = f'<div class="over">{items}</div>'
    else:
        over = '<p class="note">No overspent categories.</p>'
    noun = "y" if len(b.overspent) == 1 else "ies"
    head = (
        f"{b.unapproved} unapproved · "
        f"{len(b.overspent)} overspent categor{noun}"
    )
    return (
        "<section><h2>Budget &middot; YNAB"
        f'<span class="h2-aside">{escape(head)}</span></h2>{over}</section>'
    )


# ── Rails (progressive disclosure) ───────────────────────────────────────────
def _rail(title: str, count: str, inner: str) -> str:
    return (
        f'<details class="rail"><summary>{escape(title)}'
        f'<span class="rail-n">{escape(count)}</span></summary>'
        f'<div class="rail-body">{inner}</div></details>'
    )


def _autonomy_rail(model: DashboardModel) -> str:
    a = model.autonomy
    note = (
        '<p class="note">Earned then granted: a rule becomes <b>eligible</b> '
        "after K consistent confirms, and <b>blessed</b> (auto-applies) only "
        "once you accept the offer. A correction demotes it to observe.</p>"
    )
    if a.rules:
        rows = ""
        for r in a.rules[:30]:
            trust_cls = _TRUST_CLASS.get(r.trust, "mut")
            rows += (
                f"<tr><td>{escape(r.payee)}</td>"
                f'<td class="mut">{escape(r.category)}</td>'
                f'<td class="{trust_cls}">{escape(r.trust)}</td>'
                f'<td class="num">{r.hits}</td></tr>'
            )
        table = (
            "<table><thead><tr><th>payee</th><th>category</th><th>trust</th>"
            "<th class='num'>hits</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="note">No learned rules yet.</p>'
    count = f"{a.observe} observe · {a.eligible} eligible · {a.blessed} blessed"
    return _rail("Autonomy ladder", count, table + note)


def _telemetry_rail(model: DashboardModel) -> str:
    t: RunTelemetry = model.telemetry
    now = model.generated_at
    if not t.available:
        return _rail(
            "Run telemetry",
            "off",
            '<p class="note">ClickHouse not configured or unreachable; '
            "Temporal carries the page regardless.</p>",
        )
    rows = "".join(
        f'<tr><td class="mono">{escape(a.name)}</td>'
        f'<td class="num">{a.count}</td>'
        f'<td class="num mut">{a.avg_ms:.0f}</td>'
        f'<td class="num mut">{a.max_ms:.0f}</td></tr>'
        for a in t.activities
    )
    table = (
        "<table><thead><tr><th>activity</th><th class='num'>runs</th>"
        "<th class='num'>avg ms</th><th class='num'>max ms</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if t.activities
        else '<p class="note">No spans in the window.</p>'
    )
    last = escape(_ago(t.last_activity, now))
    summary = (
        f'<p class="note">{t.total_spans} spans &middot; {t.error_spans} '
        f"errored &middot; last {last} &middot; {t.window_days}-day window.</p>"
    )
    errs = "".join(
        f'<p class="note bad mono">{escape(e)}</p>' for e in t.recent_errors
    )
    return _rail(
        "Run telemetry", f"{t.total_spans} spans", summary + table + errs
    )


def _failures_rail(model: DashboardModel) -> str:
    if not model.failures:
        return ""
    now = model.generated_at
    real = [f for f in model.failures if not f.intentional]
    intentional = [f for f in model.failures if f.intentional]

    def _rows(items: list) -> str:  # type: ignore[type-arg]
        out = ""
        for f in items:
            cls = "bad" if not f.intentional else "mut"
            out += (
                f'<tr><td class="mono">{escape(f.workflow_id[:18])}</td>'
                f'<td class="{cls}">{escape(f.kind)}</td>'
                f'<td class="mut">{escape(f.reason or "—")}</td>'
                f'<td class="mut">{escape(_ago(f.when, now))}</td></tr>'
            )
        return out

    head = "<tr><th>workflow</th><th>kind</th><th>reason</th><th>when</th></tr>"
    real_html = (
        f"<table><thead>{head}</thead><tbody>{_rows(real)}</tbody></table>"
        if real
        else '<p class="note">No real failures — nothing broke.</p>'
    )
    intentional_html = ""
    if intentional:
        plural = "s" if len(intentional) != 1 else ""
        intentional_html = (
            f'<p class="note">{len(intentional)} intentional go-live / '
            f"re-test reset{plural} (housekeeping):</p>"
            f"<table><tbody>{_rows(intentional)}</tbody></table>"
        )
    extra = f" · {len(intentional)} resets" if intentional else ""
    count = f"{len(real)} real{extra}"
    listed = len(model.failures)
    more = ""
    if model.lifecycle.terminated > listed:
        more = (
            f'<p class="note">Showing the {listed} most recent of '
            f"{model.lifecycle.terminated} terminal workflows retained.</p>"
        )
    return _rail(
        "Failures & resets", count, real_html + intentional_html + more
    )


def _dispatch_rail(model: DashboardModel) -> str:
    d = model.dispatch
    plural = "es" if d.total != 1 else ""
    inner = (
        f'<p class="note">{d.total} inbound dispatch{plural} currently '
        "retained in Temporal "
        f"(→ {d.transaction} transaction · {d.offer} offer · {d.receipt} "
        f"receipt · {d.command} command · {d.quarantine} quarantined · "
        f"{d.ignore} ignored). Replies are processed continuously; this panel "
        "only counts what Temporal still holds, so it reads low after "
        "retention ages dispatches out — the telemetry above is the fuller "
        "record.</p>"
    )
    return _rail("Inbound · W3 dispatch", f"{d.total} retained", inner)


def _deploy_rail(model: DashboardModel) -> str:
    if not model.deploy.prs:
        return ""
    now = model.generated_at
    rows = ""
    for p in model.deploy.prs:
        state_cls = _STATE_CLASS.get(p.state, "mut")
        rows += (
            f'<tr><td><a href="{escape(p.url, quote=True)}">#{p.number}</a>'
            f"</td><td>{escape(p.title)}</td>"
            f'<td class="{state_cls}">{escape(p.state)}</td>'
            f"<td>{_ci_cell(p.ci)}</td>"
            f'<td class="mut">{escape(_ago(p.when, now))}</td></tr>'
        )
    table = (
        "<table><thead><tr><th>pr</th><th>title</th><th>state</th><th>ci</th>"
        f"<th>opened</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    return _rail("Deploy · GitHub", f"{len(model.deploy.prs)} PRs", table)


def _ci_cell(ci: str | None) -> str:
    if ci is None:
        return '<span class="mut">—</span>'
    return f'<span class="{_CI_CLASS.get(ci, "mut")}">{escape(ci)}</span>'


def _footer() -> str:
    return (
        "<footer><b>Safety envelope.</b> Every auto-apply passes the hard "
        "floor (amount ceiling, unreadable amount, per-run/day breaker) and "
        "the earned-autonomy gate (exactly one blessed rule), then a "
        "clean-context model <b>safety review</b> that can only hold it back. "
        "Autonomy is revoked — the rule demoted — on an explicit correction or "
        "a silent in-YNAB edit. Everything above is derived on this request "
        "from Temporal, YNAB, ClickHouse, AgentMail, and GitHub; nothing is "
        "stored. Reload to recompute.</footer>"
    )


def page(model: DashboardModel) -> str:
    """Render the whole dashboard as one self-contained HTML document."""
    parts = (
        _masthead(model),
        _narrative(model),
        _needs_you(model),
        _working(model),
        _activity(model),
        _budget(model),
        _autonomy_rail(model),
        _telemetry_rail(model),
        _failures_rail(model),
        _dispatch_rail(model),
        _deploy_rail(model),
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
