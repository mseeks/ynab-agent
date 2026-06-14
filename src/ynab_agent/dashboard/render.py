"""Render the view-model to one self-contained HTML page (pure).

All CSS is inline, the only script is a tiny pre-paint theme restore, and the
page makes no network request of its own — it is a static projection of an
already-computed :class:`~ynab_agent.dashboard.model.DashboardModel`. Every
dynamic value is HTML-escaped at the boundary.

The idiom is the **money desk**: a Swiss-broadsheet take on a household
budget. A faint graph-paper grid under a neutral mint-paper ground, opaque
modules with thick ink top-rules, numbered uppercase kickers, geometric-sans
display numerals, and a single emerald money-green accent (filled pills carry
the verdict; amber and oxide-red carry warn/bad). Form is a bento grid, not a
column: a full-width hero band — the plain-English state of things beside
nothing, then the four-figure pipeline, the lifecycle bar, and the health
checks — over modules for the owner queue, the budget, the conversation
feed, and the autonomy ladder, with operator detail folded into rails.
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

# Mint-paper + ink + one money-green accent. "Good" and the brand share the
# green on purpose (money in order IS the brand promise); warn is a dry
# amber, bad an oxide red, and the machine-at-work color is plain ink.
_LIGHT = (
    "--bg:#f4f6f3;--grid:rgba(20,26,22,.05);--card:#ffffff;"
    "--fg:#141a16;--mut:#5b655e;--faint:#8b948d;--line:#dde3dd;"
    "--rule:#141a16;--accent:#0c8d5c;--ok:#0c8d5c;--warn:#a96a00;"
    "--bad:#c2362b;--pillfg:#ffffff"
)
_DARK = (
    "--bg:#0c100e;--grid:rgba(230,235,231,.04);--card:#151b17;"
    "--fg:#e6ebe7;--mut:#9aa69e;--faint:#6f7a73;--line:#252d27;"
    "--rule:#e6ebe7;--accent:#2fbd84;--ok:#2fbd84;--warn:#dba344;"
    "--bad:#e3705f;--pillfg:#0c100e"
)

_CSS = f"""
:root{{{_LIGHT};
--disp:"Avenir Next",Futura,"Helvetica Neue",system-ui,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
@media(prefers-color-scheme:dark){{:root:not([data-theme]){{{_DARK}}}}}
:root[data-theme=dark]{{{_DARK}}}
*{{box-sizing:border-box}}
body{{margin:0;color:var(--fg);background:var(--bg) fixed;
background-image:linear-gradient(var(--grid) 1px,transparent 1px),
linear-gradient(90deg,var(--grid) 1px,transparent 1px);
background-size:26px 26px;
font:13.5px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}}
main{{max-width:1060px;margin:0 auto;padding:0 clamp(14px,3vw,34px) 64px}}
a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}
::selection{{background:color-mix(in srgb,var(--accent) 25%,transparent)}}
.mono{{font-family:var(--mono);font-size:.93em}}
.mut{{color:var(--mut)}}.faint{{color:var(--faint)}}
.ok{{color:var(--ok)}}.warn{{color:var(--warn)}}.bad{{color:var(--bad)}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none}}
.dot.ok{{background:var(--ok)}}.dot.warn{{background:var(--warn)}}
.dot.bad{{background:var(--bad)}}.dot.mute{{background:var(--faint)}}
.dot.accent{{background:var(--accent)}}

/* Masthead — a broadsheet brand row under a heavy ink bar. */
.topbar{{height:5px;background:var(--rule);border-radius:0 0 3px 3px}}
header{{display:flex;align-items:center;flex-wrap:wrap;gap:8px 18px;
padding:16px 0 14px}}
h1{{font-family:var(--disp);font-size:19px;margin:0;font-weight:700;
letter-spacing:-.01em;display:inline-flex;align-items:center;gap:10px}}
h1 .mark{{width:11px;height:11px;background:var(--accent);flex:none;
border-radius:2px}}
h1 .sub{{color:var(--faint);font-weight:500;font-size:13px;
letter-spacing:.02em}}
.pillv{{display:inline-flex;align-items:center;gap:7px;font-size:11px;
font-weight:800;text-transform:uppercase;letter-spacing:.08em;
padding:4px 12px;border-radius:999px;color:var(--pillfg)}}
.pillv.ok{{background:var(--accent)}}.pillv.warn{{background:var(--warn)}}
.pillv.bad{{background:var(--bad)}}
.hmeta{{margin-left:auto;display:flex;flex-wrap:wrap;align-items:center;
gap:6px 14px;color:var(--mut);font-size:11.5px;
font-family:var(--mono)}}
.themetgl{{background:var(--card);border:1px solid var(--line);
border-radius:7px;width:28px;height:28px;cursor:pointer;color:var(--mut);
font-size:13px;line-height:1;display:inline-flex;align-items:center;
justify-content:center}}
.themetgl:hover{{border-color:var(--fg);color:var(--fg)}}
.srcs{{display:flex;flex-wrap:wrap;gap:4px 16px;margin:0 0 14px;
font-size:11px;color:var(--mut)}}
.srcs span{{display:inline-flex;align-items:baseline}}
.srcs .dot{{width:7px;height:7px;margin-right:5px;position:relative;
top:-1px}}

/* Bento — numbered modules on the graph paper. */
.bento{{display:grid;gap:14px;
grid-template-columns:repeat(12,minmax(0,1fr))}}
.mod{{background:var(--card);border:1px solid var(--line);
border-top:3px solid var(--rule);border-radius:8px;
padding:16px 18px 18px;grid-column:span 12;min-width:0}}
.mod.hero{{border-top-color:var(--accent)}}
@media(min-width:920px){{
.m7{{grid-column:span 7}}.m5{{grid-column:span 5}}
.m-needs{{grid-row:span 2}}}}
h2{{font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;
color:var(--mut);margin:0 0 13px;font-weight:800;display:flex;
align-items:baseline;gap:9px}}
h2 .ix{{color:var(--accent);font-family:var(--mono);font-weight:700;
letter-spacing:0}}
h2 .h2-aside{{margin-left:auto;font-size:11px;font-weight:500;
letter-spacing:0;text-transform:none;color:var(--faint)}}

/* Hero — headline, the four-figure pipeline, the bar, the checks. */
.headline{{font-family:var(--disp);font-size:clamp(21px,3.4vw,28px);
font-weight:700;letter-spacing:-.015em;line-height:1.15;margin:2px 0 6px}}
.headline.warn{{color:var(--warn)}}.headline.bad{{color:var(--bad)}}
.lede{{color:var(--mut);font-size:13.5px;line-height:1.6;margin:0;
max-width:86ch}}
.pipe{{display:flex;align-items:stretch;margin:18px 0 0;
border-top:1px solid var(--line);flex-wrap:wrap}}
.stage{{flex:1 1 0;min-width:120px;padding:14px 16px 2px 16px;
border-right:1px solid var(--line)}}
.stage:first-child{{padding-left:0}}
.stage:last-child{{border-right:0}}
.stage .sv{{font-family:var(--disp);font-size:38px;font-weight:650;
line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.stage .sv.warn{{color:var(--warn)}}.stage .sv.acc{{color:var(--accent)}}
.stage .sl{{font-size:10px;text-transform:uppercase;letter-spacing:.11em;
color:var(--mut);font-weight:800;margin-top:7px}}
.stage .sx{{font-size:11px;color:var(--faint);margin-top:1px}}
.checks{{display:flex;flex-wrap:wrap;gap:8px 24px;margin:16px 0 10px}}
.kpi{{display:inline-flex;align-items:baseline;gap:8px;font-size:12.5px}}
.kpi b{{font-weight:650}}
.kpi .dot{{position:relative;top:1px}}
.flow{{display:flex;height:12px;border-radius:4px;overflow:hidden;
margin:2px 0 10px;background:var(--bg)}}
.flow>span{{display:block;min-width:3px}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:11.5px;
color:var(--mut)}}
.legend .it{{display:inline-flex;align-items:baseline;gap:6px}}
.legend .dot{{width:7px;height:7px;position:relative;top:-1px}}
.legend b{{color:var(--fg);font-weight:650;font-variant-numeric:tabular-nums}}

/* Needs you — the owner queue. */
.qrow{{display:flex;align-items:baseline;gap:11px;padding:10px 0 10px 12px;
border-bottom:1px solid var(--line);border-left:3px solid var(--warn);
margin:0 0 2px}}
.qrow.offer{{border-left-color:var(--accent)}}
.qrow:last-of-type{{border-bottom:0}}
.q-main{{flex:1;min-width:0}}
.q-title{{display:block;font-weight:600;overflow-wrap:anywhere;
font-size:13.5px}}
.q-sub{{display:block;color:var(--mut);font-size:12px;margin-top:1px}}
.q-age{{color:var(--faint);font-size:11px;white-space:nowrap;
font-family:var(--mono)}}
.tag-pill{{display:inline-block;padding:1px 8px;border-radius:999px;
font-size:9.5px;font-weight:800;text-transform:uppercase;
letter-spacing:.07em;background:var(--accent);color:var(--pillfg);
vertical-align:2px}}
.empty{{display:flex;align-items:center;gap:10px;color:var(--mut);
font-size:13px;padding:6px 0}}
.empty .big{{font-family:var(--disp);font-size:19px;line-height:1;
color:var(--accent)}}
.note{{color:var(--mut);font-size:12px;margin:11px 0 0;line-height:1.55}}

/* Budget — signed amounts, cheque-style. */
.bigstat{{display:flex;align-items:baseline;gap:10px;margin:0 0 8px}}
.bigstat .n{{font-family:var(--disp);font-size:30px;font-weight:650;
letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.bigstat .l{{color:var(--mut);font-size:12px}}
.over{{display:flex;flex-direction:column}}
.over .it{{display:flex;align-items:baseline;justify-content:space-between;
gap:12px;padding:7px 0;border-bottom:1px solid var(--line)}}
.over .it:last-child{{border-bottom:0}}
.over .amt{{font-family:var(--mono);font-weight:700;color:var(--bad);
font-size:13px;white-space:nowrap}}

/* What it's done — the conversation feed. */
.feed{{display:flex;flex-direction:column}}
.frow{{display:flex;align-items:baseline;gap:10px;padding:8px 0;
border-bottom:1px solid var(--line)}}
.frow:last-child{{border-bottom:0}}
.frow .g{{width:1.1em;text-align:center;flex:none;color:var(--faint)}}
.f-main{{flex:1;min-width:0}}
.f-title{{display:block;overflow-wrap:anywhere}}
.f-prev{{display:block;color:var(--faint);font-size:11.5px;margin-top:1px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.f-age{{color:var(--faint);font-size:11px;white-space:nowrap;
font-family:var(--mono)}}

/* Autonomy ladder — three cells, then the rule table. */
.rungs{{display:flex;align-items:stretch;margin:0 0 14px;
border-top:1px solid var(--line)}}
.rung{{flex:1 1 0;min-width:0;padding:12px 12px 0 12px;
border-right:1px solid var(--line)}}
.rung:first-child{{padding-left:0}}
.rung:last-child{{border-right:0}}
.rung .rv{{font-family:var(--disp);font-size:26px;font-weight:650;
line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.rung .rv.acc{{color:var(--accent)}}
.rung .rl{{font-size:9.5px;text-transform:uppercase;letter-spacing:.1em;
color:var(--mut);font-weight:800;margin-top:6px}}
.rung .rx{{font-size:10.5px;color:var(--faint);margin-top:1px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{text-align:left;padding:6px 12px 6px 0;vertical-align:baseline;
border-bottom:1px solid var(--line)}}
th{{color:var(--faint);font-weight:700;font-size:9.5px;
text-transform:uppercase;letter-spacing:.08em}}
.r{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}

/* Operator rails — folded detail. */
details.rail{{border-top:1px solid var(--line)}}
details.rail:first-of-type{{border-top:0}}
details.rail>summary{{cursor:pointer;list-style:none;display:flex;
align-items:center;gap:9px;font-size:12px;font-weight:650;
color:var(--mut);padding:10px 0}}
details.rail>summary:hover{{color:var(--fg)}}
details.rail>summary::-webkit-details-marker{{display:none}}
details.rail>summary::before{{content:"+";color:var(--accent);
font-family:var(--mono);font-weight:700}}
details.rail[open]>summary::before{{content:"\\2212"}}
details.rail>summary .rail-n{{margin-left:auto;color:var(--faint);
font-weight:500;font-family:var(--mono);font-size:11px}}
.rail-body{{padding:2px 0 16px}}
footer{{margin:30px 0 0;padding-top:14px;border-top:3px solid var(--rule);
color:var(--mut);font-size:11.5px;line-height:1.7;max-width:100ch}}
footer b{{color:var(--fg);font-weight:650}}
"""

# Pre-paint theme restore + the toggle: tiny and inline, so the page stays
# one self-contained file. Without JS it follows the OS preference.
_THEME_JS = (
    "(function(){var k='ynab-agent-theme',r=document.documentElement,"
    "s=localStorage.getItem(k);if(s)r.setAttribute('data-theme',s);"
    "window.__t=function(){var c=r.getAttribute('data-theme')||"
    "(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');"
    "var n=c==='dark'?'light':'dark';r.setAttribute('data-theme',n);"
    "localStorage.setItem(k,n);};})();"
)

_LIFECYCLE_ORDER = (
    "discovered",
    "enriching",
    "hold_amazon",
    "revising",
    "awaiting_human",
    "open",
    "lapsed",
)
# Each in-flight state's human label + flow colour: ink = the machine is
# working, amber = it's on you, green = settled-and-resting, faint = parked.
_STATE_META = {
    "discovered": ("discovered", "var(--faint)"),
    "enriching": ("enriching", "var(--fg)"),
    "hold_amazon": ("holding for Amazon detail", "var(--faint)"),
    "revising": ("revising", "var(--fg)"),
    "awaiting_human": ("awaiting you", "var(--warn)"),
    "open": ("resting (done)", "var(--ok)"),
    "lapsed": ("lapsed", "var(--faint)"),
}
_TRUST_CLASS = {"trusted": "ok", "confirmed": "warn", "suggested": "mut"}
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


def _applied_count(telemetry: RunTelemetry) -> int:
    """Recent writes to YNAB, from the commit activity's run count."""
    for activity in telemetry.activities:
        if activity.name == "commit_to_ynab":
            return activity.count
    return 0


def _kicker(index: str, title: str, aside: str = "") -> str:
    aside_html = f'<span class="h2-aside">{aside}</span>' if aside else ""
    return (
        f'<h2><span class="ix">{escape(index)}</span>{escape(title)}'
        f"{aside_html}</h2>"
    )


# ── Masthead ─────────────────────────────────────────────────────────────────
def _masthead(model: DashboardModel) -> str:
    h = model.health
    now = model.generated_at
    # The "as of" time reads in the household's own day (SPEC §13).
    stamp = now.astimezone(HOUSEHOLD_TZ).strftime("%Y-%m-%d %H:%M %Z")
    src = "".join(
        f"<span>{_dot('ok' if s.ok else ('mute' if s.off else 'bad'))}"
        f"{escape(s.name)}&nbsp;"
        f'<span class="faint">{escape(s.detail)}</span></span>'
        for s in model.sources
    )
    return (
        '<div class="topbar"></div><header>'
        '<h1><span class="mark"></span>ynab-agent '
        '<span class="sub">— the money desk</span></h1>'
        f'<span class="pillv {escape(h.tone)}">{escape(h.label)}</span>'
        f'<div class="hmeta"><span>{escape(model.repo)}</span>'
        f"<span>{escape(stamp)}</span>"
        '<button class="themetgl" onclick="__t()" title="theme">◐</button>'
        "</div></header>"
        f'<div class="srcs">{src}</div>'
    )


# ── Hero: headline · pipeline · lifecycle · health checks ───────────────────
def _stage(value: str, label: str, sub: str = "", value_cls: str = "") -> str:
    cls = f"sv {value_cls}".strip()
    sub_html = f'<div class="sx">{escape(sub)}</div>' if sub else ""
    return (
        f'<div class="stage"><div class="{cls}">{escape(value)}</div>'
        f'<div class="sl">{escape(label)}</div>{sub_html}</div>'
    )


def _hero(model: DashboardModel) -> str:
    n = model.narrative
    h = model.health
    now = model.generated_at
    body = " ".join(n.paragraphs)
    tone_cls = "" if n.tone == "ok" else f" {escape(n.tone)}"
    headline = (
        f'<div class="headline{tone_cls}">{escape(n.headline)}</div>'
        f'<p class="lede">{escape(body)}</p>'
    )

    unapproved = str(model.budget.unapproved) if model.budget.available else "—"
    applied = (
        str(_applied_count(model.telemetry))
        if model.telemetry.available
        else "—"
    )
    needs = h.needs_you
    pipe = (
        '<div class="pipe">'
        + _stage(unapproved, "unapproved", "new in YNAB")
        + _stage(str(model.lifecycle.in_flight), "in flight", "with the agent")
        + _stage(
            str(needs),
            "need you",
            "reply by email",
            "warn" if needs else "",
        )
        + _stage(
            applied, "applied", f"last {model.telemetry.window_days}d", "acc"
        )
        + "</div>"
    )

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
    checks = (
        '<div class="checks">'
        '<span class="kpi mut" style="text-transform:uppercase;'
        'font-size:10px;font-weight:800;letter-spacing:.11em">'
        "Is it working?</span>"
        + "".join(kpis)
        + "</div>"
        + _flow(model.lifecycle)
    )
    return f'<div class="mod hero">{headline}{pipe}{checks}</div>'


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
    aside = f"oldest {escape(_ago(_oldest(queue), now))}" if queue else ""
    return (
        '<div class="mod m7 m-needs">'
        + _kicker("01", "Needs you", aside)
        + body
        + "</div>"
    )


def _oldest(queue: tuple[QueueItem, ...]) -> datetime | None:
    # Normalize to aware before min() — mixed aware/naive comparison raises.
    sinces = [_aware(q.since) for q in queue if q.since is not None]
    return min(sinces) if sinces else None


def _qrow(q: QueueItem, now: datetime) -> str:
    title = escape(q.question or q.label)
    pill = '<span class="tag-pill">offer</span> ' if q.kind == "offer" else ""
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
    offer_cls = " offer" if q.kind == "offer" else ""
    return (
        f'<div class="qrow{offer_cls}">'
        f'<span class="q-main"><span class="q-title">{pill}{title}</span>'
        f'{sub}</span><span class="q-age">{age}</span></div>'
    )


# ── Budget ───────────────────────────────────────────────────────────────────
def _budget(model: DashboardModel) -> str:
    b = model.budget
    if not b.available:
        return ""
    if b.overspent:
        items = "".join(
            f'<span class="it"><span class="mut">{escape(c.name)}</span>'
            f'<span class="amt">{escape(c.balance)}</span></span>'
            for c in b.overspent
        )
        over = f'<div class="over">{items}</div>'
    else:
        over = '<p class="note">No overspent categories.</p>'
    noun = "y" if len(b.overspent) == 1 else "ies"
    return (
        '<div class="mod m5">'
        + _kicker("02", "Budget · YNAB")
        + (
            f'<div class="bigstat"><span class="n">{b.unapproved}</span>'
            '<span class="l">unapproved &middot; '
            f"{len(b.overspent)} overspent categor{noun}</span></div>"
        )
        + over
        + "</div>"
    )


# ── Autonomy ladder ──────────────────────────────────────────────────────────
def _autonomy(model: DashboardModel) -> str:
    a = model.autonomy
    rungs = (
        '<div class="rungs">'
        + (
            f'<div class="rung"><div class="rv">{a.observe}</div>'
            '<div class="rl">observing</div>'
            '<div class="rx">earning consistency</div></div>'
        )
        + (
            f'<div class="rung"><div class="rv">{a.eligible}</div>'
            '<div class="rl">eligible</div>'
            '<div class="rx">awaiting your yes</div></div>'
        )
        + (
            f'<div class="rung"><div class="rv acc">{a.blessed}</div>'
            '<div class="rl">blessed</div>'
            '<div class="rx">auto-applies, flagged</div></div>'
        )
        + "</div>"
    )
    if a.rules:
        rows = ""
        for r in a.rules[:30]:
            trust_cls = _TRUST_CLASS.get(r.trust, "mut")
            rows += (
                f"<tr><td>{escape(r.payee)}</td>"
                f'<td class="mut">{escape(r.category)}</td>'
                f'<td class="{trust_cls}">{escape(r.trust)}</td>'
                f'<td class="r">{r.hits}</td></tr>'
            )
        table = (
            "<table><thead><tr><th>payee</th><th>category</th><th>trust</th>"
            "<th class='r'>hits</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="note">No learned rules yet.</p>'
    note = (
        '<p class="note">Earned then granted: a rule becomes <b>eligible</b> '
        "after K consistent confirms, and <b>blessed</b> (auto-applies) only "
        "once you accept the offer. A correction — or a one-line "
        "&ldquo;stop auto-handling X&rdquo; — demotes it.</p>"
    )
    return (
        '<div class="mod m5">'
        + _kicker("03", "Autonomy ladder")
        + rungs
        + table
        + note
        + "</div>"
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
        '<div class="mod m7">'
        + _kicker("04", "What it's done", "recent threads")
        + f'<div class="feed">{rows}</div></div>'
    )


# ── Operator rails (progressive disclosure) ─────────────────────────────────
def _rail(title: str, count: str, inner: str) -> str:
    return (
        f'<details class="rail"><summary>{escape(title)}'
        f'<span class="rail-n">{escape(count)}</span></summary>'
        f'<div class="rail-body">{inner}</div></details>'
    )


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
        f'<td class="r">{a.count}</td>'
        f'<td class="r mut">{a.avg_ms:.0f}</td>'
        f'<td class="r mut">{a.max_ms:.0f}</td></tr>'
        for a in t.activities
    )
    table = (
        "<table><thead><tr><th>activity</th><th class='r'>runs</th>"
        "<th class='r'>avg ms</th><th class='r'>max ms</th></tr></thead>"
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


def _operator(model: DashboardModel) -> str:
    rails = (
        _telemetry_rail(model)
        + _failures_rail(model)
        + _dispatch_rail(model)
        + _deploy_rail(model)
    )
    return (
        '<div class="mod">'
        + _kicker("05", "Operator", "telemetry · failures · dispatch · deploy")
        + rails
        + "</div>"
    )


def _footer() -> str:
    return (
        "<footer><b>Safety envelope.</b> Every auto-apply passes the hard "
        "floor (amount ceiling, unreadable amount, per-run/day breaker) and "
        "the earned-autonomy gate (exactly one blessed rule), then a "
        "clean-context model <b>safety review</b> that can only hold it back. "
        "Autonomy is revoked — the rule demoted — on an explicit correction, "
        "a silent in-YNAB edit, or a one-line reply. Everything above is "
        "derived on this request from Temporal, YNAB, ClickHouse, AgentMail, "
        "and GitHub; nothing is stored. Reload to recompute.</footer>"
    )


def page(model: DashboardModel) -> str:
    """Render the whole dashboard as one self-contained HTML document."""
    modules = (
        _hero(model),
        _needs_you(model),
        _budget(model),
        _autonomy(model),
        _activity(model),
        _operator(model),
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>ynab-agent &middot; money desk</title>"
        f"<script>{_THEME_JS}</script>"
        f"<style>{_CSS}</style></head><body><main>"
        + _masthead(model)
        + f'<div class="bento">{"".join(m for m in modules if m)}</div>'
        + _footer()
        + "</main></body></html>"
    )
