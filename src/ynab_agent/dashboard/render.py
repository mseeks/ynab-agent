"""Render the view-model to one self-contained HTML page (pure).

All CSS is inline, the only script is a tiny pre-paint theme restore, and the
page makes no network request of its own — it is a static projection of an
already-computed :class:`~ynab_agent.dashboard.model.DashboardModel`. Every
dynamic value is HTML-escaped at the boundary.

The idiom is the **household ledger**: a warm paper-and-ink palette, serif
display numerals, structure from hairlines + whitespace + type (never box
fills), semantic color confined to 9px dots and value text, and one
fountain-ink accent for everything interactive. The page is organized around
the operator's questions, not the subsystems the data came from: a masthead
verdict, the **pipeline** hero (YNAB → agent → you → budget, the whole story
in four numbers beside the plain-English *state of things*), **needs you**
(the only acted-upon queue), **is it working**, the conversation feed, the
budget, the autonomy ladder, and the operator detail folded away.
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

# Warm ledger palette: aged paper, sepia ink, hairlines like faint rules, and
# a single fountain-ink accent. Semantic colors are warmed so a figure reads
# as ink, never as an alarm fill — ok is a muted olive, warn a gold-ochre,
# bad a deep oxblood, all distinct from the brighter interactive ink.
_LIGHT = (
    "--fg:#2b241c;--mut:#6a5d4a;--faint:#8a7c64;--hair:#cfc0a4;"
    "--line:#e7ddc9;--bg:#faf6ee;--tint:#f2ecdf;--accent:#34538f;"
    "--ok:#466632;--warn:#8a5a16;--bad:#7c2d26"
)
_DARK = (
    "--fg:#ece4d6;--mut:#b3a892;--faint:#978c77;--hair:#4a4133;"
    "--line:#2f2820;--bg:#14110d;--tint:#1c1813;--accent:#7d9bd8;"
    "--ok:#74b35f;--warn:#d8a34d;--bad:#e08a72"
)

_CSS = f"""
:root{{{_LIGHT};
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,ui-serif,serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
@media(prefers-color-scheme:dark){{:root:not([data-theme]){{{_DARK}}}}}
:root[data-theme=dark]{{{_DARK}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}}
main{{max-width:960px;margin:0 auto;padding:0 clamp(16px,4vw,40px) 72px}}
a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}
::selection{{background:color-mix(in srgb,var(--accent) 22%,transparent)}}
.mono{{font-family:var(--mono);font-size:.93em}}
.mut{{color:var(--mut)}}.faint{{color:var(--faint)}}
.ok{{color:var(--ok)}}.warn{{color:var(--warn)}}.bad{{color:var(--bad)}}
.num{{font-variant-numeric:tabular-nums}}
.ic{{width:14px;height:14px;flex:none}}
/* the one carrier of valence besides the value text itself */
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;flex:none}}
.dot.ok{{background:var(--ok)}}.dot.warn{{background:var(--warn)}}
.dot.bad{{background:var(--bad)}}.dot.mute{{background:var(--faint)}}
.dot.accent{{background:var(--accent)}}

/* Masthead — wordmark, the one verdict, source dots, the as-of stamp. */
header{{display:flex;align-items:center;flex-wrap:wrap;gap:8px 20px;
padding:26px 0 16px;border-bottom:1px solid var(--line)}}
h1{{font-family:var(--serif);font-size:22px;margin:0;font-weight:600;
letter-spacing:-.01em;display:inline-flex;align-items:center;gap:9px}}
h1 .mark{{width:19px;height:19px;color:var(--accent);flex:none}}
h1 .sub{{color:var(--faint);font-weight:500}}
.verdict{{display:inline-flex;align-items:center;gap:7px;font-size:13.5px;
font-weight:650}}
.verdict.ok{{color:var(--ok)}}.verdict.warn{{color:var(--warn)}}
.verdict.bad{{color:var(--bad)}}
.hmeta{{margin-left:auto;display:flex;flex-wrap:wrap;align-items:center;
gap:6px 16px;color:var(--mut);font-size:12px}}
.themetgl{{background:transparent;border:1px solid var(--line);
border-radius:999px;width:29px;height:29px;cursor:pointer;color:var(--mut);
font-size:13px;line-height:1;display:inline-flex;align-items:center;
justify-content:center}}
.themetgl:hover{{border-color:var(--fg);color:var(--fg)}}
.srcs{{display:flex;flex-wrap:wrap;gap:5px 15px;margin:11px 0 0;
font-size:11.5px;color:var(--mut)}}
.srcs span{{display:inline-flex;align-items:baseline}}
.srcs .dot{{width:7px;height:7px;margin-right:5px;position:relative;top:-1px}}
.chips{{display:flex;flex-wrap:wrap;gap:6px 18px;margin:12px 0 0;
font-size:12.5px}}
.chip{{display:inline-flex;align-items:center;gap:7px;color:var(--mut)}}
.chip b{{color:var(--fg);font-weight:600}}
.chip.warn b{{color:var(--warn)}}.chip.bad b{{color:var(--bad)}}
.chip.ok b{{color:var(--ok)}}

/* Hero — the narrative beside the pipeline. */
.hero{{display:grid;gap:26px 44px;margin:26px 0 0;align-items:start;
grid-template-columns:minmax(290px,1fr) minmax(330px,1.1fr)}}
@media(max-width:760px){{.hero{{grid-template-columns:minmax(0,1fr)}}}}
@media(max-width:480px){{.pipe{{display:grid;
grid-template-columns:1fr 1fr;gap:16px 8px}}.parrow{{display:none}}}}
.narr{{border-left:3px solid var(--line);padding:3px 0 3px 16px}}
.narr.ok{{border-color:var(--ok)}}.narr.warn{{border-color:var(--warn)}}
.narr.bad{{border-color:var(--bad)}}
.narr .head{{font-family:var(--serif);font-weight:650;font-size:19px;
letter-spacing:-.01em;margin:0 0 6px}}
.narr .body{{font-family:var(--serif);font-size:15.5px;line-height:1.6;
margin:0;color:var(--mut);max-width:54ch}}
.pipeh{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;
color:var(--mut);font-weight:600;margin:0 0 14px;padding-bottom:7px;
border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}}
.pipe{{display:flex;align-items:stretch;gap:4px;flex-wrap:wrap}}
.stage{{flex:1 1 0;min-width:86px;text-align:center;padding:2px 4px}}
.stage .sv{{font-family:var(--serif);font-size:33px;font-weight:600;
line-height:1.05;font-variant-numeric:tabular-nums;letter-spacing:-.01em}}
.stage .sv.warn{{color:var(--warn)}}.stage .sv.acc{{color:var(--accent)}}
.stage .sl{{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
color:var(--mut);font-weight:600;margin-top:4px}}
.stage .sx{{font-size:11px;color:var(--faint);margin-top:1px}}
.parrow{{align-self:center;color:var(--hair);font-size:16px;flex:none;
padding-bottom:18px}}
.pipecap{{color:var(--faint);font-size:11.5px;margin:13px 0 0;
line-height:1.5}}

/* Section = an 11px uppercase label, an icon anchor, a hairline. */
section{{margin:34px 0 0}}
h2{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;
color:var(--mut);margin:0 0 12px;font-weight:650;padding-bottom:7px;
border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}}
h2 .h2-aside{{margin-left:auto;font-size:11px;font-weight:500;
letter-spacing:0;text-transform:none;color:var(--faint)}}

/* Needs you — the humanized queue. */
.qrow{{display:flex;align-items:baseline;gap:11px;padding:9px 0;
border-bottom:1px solid var(--line)}}
.qrow:last-child{{border-bottom:0}}
.qrow .dot{{position:relative;top:1px}}
.q-main{{flex:1;min-width:0}}
.q-title{{display:block;font-weight:550;overflow-wrap:anywhere}}
.q-sub{{display:block;color:var(--mut);font-size:12.5px;margin-top:1px}}
.q-age{{color:var(--faint);font-size:12px;white-space:nowrap;
font-variant-numeric:tabular-nums}}
.tag-pill{{display:inline-block;padding:0 7px;border-radius:999px;
font-size:10px;font-weight:650;text-transform:uppercase;letter-spacing:.05em;
border:1px solid var(--line);color:var(--accent);vertical-align:1px}}
.empty{{display:flex;align-items:center;gap:10px;color:var(--mut);
font-size:13.5px;padding:6px 0}}
.empty .big{{font-family:var(--serif);font-size:19px;line-height:1;
color:var(--ok)}}
.note{{color:var(--mut);font-size:12.5px;margin:11px 0 0;line-height:1.55}}

/* Is it working — kpis + the lifecycle flow. */
.kpis{{display:flex;flex-wrap:wrap;gap:8px 26px;margin:0 0 16px}}
.kpi{{display:inline-flex;align-items:baseline;gap:8px;font-size:13px}}
.kpi b{{font-weight:600}}
.kpi .dot{{position:relative;top:1px}}
.flow{{display:flex;height:10px;border-radius:999px;overflow:hidden;
margin:4px 0 12px;background:var(--tint)}}
.flow>span{{display:block;min-width:3px}}
.legend{{display:flex;flex-wrap:wrap;gap:7px 18px;font-size:12px;
color:var(--mut)}}
.legend .it{{display:inline-flex;align-items:baseline;gap:6px}}
.legend .dot{{width:7px;height:7px;position:relative;top:-1px}}
.legend b{{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}}

/* What it's done — the conversation feed. */
.feed{{display:flex;flex-direction:column}}
.frow{{display:flex;align-items:baseline;gap:10px;padding:8px 0;
border-bottom:1px solid var(--line)}}
.frow:last-child{{border-bottom:0}}
.frow .g{{width:1.1em;text-align:center;flex:none;color:var(--hair)}}
.f-main{{flex:1;min-width:0}}
.f-title{{display:block;overflow-wrap:anywhere}}
.f-prev{{display:block;color:var(--faint);font-size:12px;margin-top:1px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.f-age{{color:var(--faint);font-size:12px;white-space:nowrap}}

/* Budget — overspent categories as signed serif figures. */
.over{{display:flex;flex-wrap:wrap;gap:8px 26px;margin:4px 0 0}}
.over .it{{display:inline-flex;align-items:baseline;gap:8px}}
.over .amt{{font-family:var(--serif);font-size:17px;font-weight:600;
color:var(--bad);font-variant-numeric:tabular-nums}}

/* Autonomy — the earned-trust ladder. */
.ladder{{display:flex;align-items:stretch;gap:4px;flex-wrap:wrap;
margin:2px 0 16px}}
.rung{{flex:1 1 0;min-width:96px;text-align:center;padding:2px 4px}}
.rung .rv{{font-family:var(--serif);font-size:26px;font-weight:600;
line-height:1.05;font-variant-numeric:tabular-nums}}
.rung .rv.acc{{color:var(--accent)}}
.rung .rl{{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
color:var(--mut);font-weight:600;margin-top:4px}}
.rung .rx{{font-size:11px;color:var(--faint);margin-top:1px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{text-align:left;padding:6px 14px 6px 0;vertical-align:baseline;
border-bottom:1px solid var(--line)}}
th{{color:var(--faint);font-weight:600;font-size:10.5px;
text-transform:uppercase;letter-spacing:.05em}}
.r{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}

/* Rails — progressive disclosure. */
details.rail{{border-top:1px solid var(--line);padding:0}}
details.rail>summary{{cursor:pointer;list-style:none;display:flex;
align-items:center;gap:9px;font-size:12px;font-weight:600;color:var(--mut);
padding:11px 0}}
details.rail>summary:hover{{color:var(--fg)}}
details.rail>summary::-webkit-details-marker{{display:none}}
details.rail>summary::before{{content:"\\25B8";color:var(--hair);
font-size:10px}}
details.rail[open]>summary::before{{content:"\\25BE"}}
details.rail>summary .rail-n{{margin-left:auto;color:var(--faint);
font-weight:500}}
.rail-body{{padding:2px 0 18px}}
footer{{margin:44px 0 0;padding-top:16px;border-top:1px solid var(--line);
color:var(--mut);font-size:12px;line-height:1.7;max-width:92ch}}
footer b{{color:var(--fg);font-weight:600}}
"""

# Pre-paint theme restore + the toggle: tiny and inline, so the page stays one
# self-contained file. Without JS it simply follows the OS preference.
_THEME_JS = (
    "(function(){var k='ynab-agent-theme',r=document.documentElement,"
    "s=localStorage.getItem(k);if(s)r.setAttribute('data-theme',s);"
    "window.__t=function(){var c=r.getAttribute('data-theme')||"
    "(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');"
    "var n=c==='dark'?'light':'dark';r.setAttribute('data-theme',n);"
    "localStorage.setItem(k,n);};})();"
)

# Hairline line-icons (inline SVG, stroke=currentColor so they theme
# themselves). Anchors for the eye on the wordmark and section labels — never
# on the figures.
_ICONS = {
    "ledger": (
        '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 '
        "1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 "
        '1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>'
    ),
    "inbox": (
        '<path d="M22 12h-6l-2 3h-4l-2-3H2"/>'
        '<path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45'
        '-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'
    ),
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "mail": (
        '<rect width="20" height="16" x="2" y="4" rx="2"/>'
        '<path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>'
    ),
    "wallet": (
        '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 '
        '1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2"/>'
        '<path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>'
    ),
    "ladder": (
        '<path d="M8 3v18"/><path d="M16 3v18"/><path d="M8 8h8"/>'
        '<path d="M8 13h8"/><path d="M8 18h8"/>'
    ),
}


def _icon(name: str, cls: str = "ic") -> str:
    return (
        f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{_ICONS[name]}</svg>'
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
# Each in-flight state's human label + flow colour (accent = the agent is
# working, warn = it's on you, ok = settled-and-resting, faint = parked).
_STATE_META = {
    "discovered": ("discovered", "var(--faint)"),
    "enriching": ("enriching", "var(--accent)"),
    "hold_amazon": ("holding for Amazon detail", "var(--faint)"),
    "revising": ("revising", "var(--accent)"),
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


# ── Masthead ─────────────────────────────────────────────────────────────────
def _masthead(model: DashboardModel) -> str:
    h = model.health
    now = model.generated_at
    # The "as of" time reads in the household's own day (SPEC §13), zone
    # spelled out.
    stamp = now.astimezone(HOUSEHOLD_TZ).strftime("%Y-%m-%d %H:%M %Z")

    src = "".join(
        f"<span>{_dot('ok' if s.ok else ('mute' if s.off else 'bad'))}"
        f"{escape(s.name)}&nbsp;"
        f'<span class="faint">{escape(s.detail)}</span></span>'
        for s in model.sources
    )

    chips: list[str] = []
    if h.needs_you:
        suffix = "s" if h.needs_you == 1 else ""
        chips.append(
            f'<span class="chip warn">{_dot("warn")}'
            f"<b>{h.needs_you}</b> need{suffix} you</span>"
        )
    else:
        chips.append(
            f'<span class="chip ok">{_dot("ok")}<b>✓</b> all caught up</span>'
        )
    poll_cls = "ok" if h.poll_live and not h.poll_stale else "bad"
    poll_age = escape(_ago(h.poll_last_start, now))
    stalled = " · stalled" if h.poll_stale else ""
    chips.append(
        f'<span class="chip">{_dot(poll_cls)}poll <b>{poll_age}</b>'
        f"{stalled}</span>"
    )
    if model.budget.available:
        chips.append(
            f'<span class="chip">{_dot("mute")}'
            f"<b>{model.budget.unapproved}</b> unapproved</span>"
        )
    if h.real_failures:
        suffix = "s" if h.real_failures != 1 else ""
        chips.append(
            f'<span class="chip bad">{_dot("bad")}'
            f"<b>{h.real_failures}</b> failure{suffix}</span>"
        )

    return (
        "<header>"
        f"<h1>{_icon('ledger', 'mark')}ynab-agent "
        '<span class="sub">· ledger</span></h1>'
        f'<span class="verdict {escape(h.tone)}">{_dot(h.tone)}'
        f"{escape(h.label)}</span>"
        f'<div class="hmeta"><span>{escape(model.repo)}</span>'
        f"<span>{escape(stamp)}</span>"
        '<button class="themetgl" onclick="__t()" title="theme">◐</button>'
        "</div></header>"
        f'<div class="chips">{"".join(chips)}</div>'
        f'<div class="srcs">{src}</div>'
    )


# ── Hero: the narrative beside the pipeline ──────────────────────────────────
def _stage(value: str, label: str, sub: str = "", value_cls: str = "") -> str:
    cls = f"sv {value_cls}".strip()
    sub_html = f'<div class="sx">{escape(sub)}</div>' if sub else ""
    return (
        f'<div class="stage"><div class="{cls}">{escape(value)}</div>'
        f'<div class="sl">{escape(label)}</div>{sub_html}</div>'
    )


_ARROW = '<span class="parrow">▸</span>'


def _hero(model: DashboardModel) -> str:
    n = model.narrative
    body = " ".join(n.paragraphs)
    narrative = (
        f'<div class="narr {escape(n.tone)}">'
        f'<p class="head">{escape(n.headline)}</p>'
        f'<p class="body">{escape(body)}</p></div>'
    )

    unapproved = str(model.budget.unapproved) if model.budget.available else "—"
    applied = (
        str(_applied_count(model.telemetry))
        if model.telemetry.available
        else "—"
    )
    needs = model.health.needs_you
    pipeline = (
        '<div><p class="pipeh">'
        f"{_icon('activity')}The pipeline</p>"
        '<div class="pipe">'
        + _stage(unapproved, "unapproved", "in YNAB")
        + _ARROW
        + _stage(str(model.lifecycle.in_flight), "in flight", "agent", "acc")
        + _ARROW
        + _stage(
            str(needs), "need you", "reply by email", "warn" if needs else ""
        )
        + _ARROW
        + _stage(applied, "applied", f"last {model.telemetry.window_days}d")
        + "</div>"
        '<p class="pipecap">New YNAB transactions flow left to right: the '
        "agent triages, asks only when unsure, and writes back what you (or "
        "a rule you blessed) decided.</p></div>"
    )
    return f'<div class="hero">{narrative}{pipeline}</div>'


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
    return f"<section><h2>{_icon('inbox')}Needs you{aside}</h2>{body}</section>"


def _oldest(queue: tuple[QueueItem, ...]) -> datetime | None:
    # Normalize to aware before min() — mixed aware/naive comparison raises.
    sinces = [_aware(q.since) for q in queue if q.since is not None]
    return min(sinces) if sinces else None


def _qrow(q: QueueItem, now: datetime) -> str:
    dot = "accent" if q.kind == "offer" else "warn"
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
        f"<section><h2>{_icon('activity')}Is it working?</h2>"
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
        f"<section><h2>{_icon('mail')}What it's done"
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
            f'<span class="it"><span class="amt">{escape(c.balance)}'
            f'</span> <span class="mut">{escape(c.name)}</span></span>'
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
        f"<section><h2>{_icon('wallet')}Budget &middot; YNAB"
        f'<span class="h2-aside">{escape(head)}</span></h2>{over}</section>'
    )


# ── Autonomy ladder ──────────────────────────────────────────────────────────
def _autonomy(model: DashboardModel) -> str:
    a = model.autonomy
    rungs = (
        '<div class="ladder">'
        + (
            f'<div class="rung"><div class="rv">{a.observe}</div>'
            '<div class="rl">observing</div>'
            '<div class="rx">earning consistency</div></div>'
        )
        + _ARROW
        + (
            f'<div class="rung"><div class="rv">{a.eligible}</div>'
            '<div class="rl">eligible</div>'
            '<div class="rx">awaiting your yes</div></div>'
        )
        + _ARROW
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
        f"<section><h2>{_icon('ladder')}Autonomy ladder</h2>"
        f"{rungs}{table}{note}</section>"
    )


# ── Rails (progressive disclosure) ───────────────────────────────────────────
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
    parts = (
        _masthead(model),
        _hero(model),
        _needs_you(model),
        _working(model),
        _activity(model),
        _budget(model),
        _autonomy(model),
        "<section>"
        + _telemetry_rail(model)
        + _failures_rail(model)
        + _dispatch_rail(model)
        + _deploy_rail(model)
        + "</section>",
        _footer(),
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>ynab-agent &middot; ledger</title>"
        f"<script>{_THEME_JS}</script>"
        f"<style>{_CSS}</style></head><body><main>"
        + "".join(parts)
        + "</main></body></html>"
    )
