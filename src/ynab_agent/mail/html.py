"""Minimalist HTML rendering for outbound email (SPEC §5).

Every email the agent sends carries an HTML part alongside the plain text.
The text stays canonical — same words, tested copy — this module only adds
presentation. The goal is glanceability: payee, amount, and the suggested
category readable in under a second, the reply hint understated.

Email-client constraints shape everything here: styles are inline (style
blocks don't survive Gmail reliably), layout is simple block flow (no
flexbox/grid in Outlook), and the palette is neutral ink-on-white. No images,
no buttons — the only call to action is "reply", and email clients already
own that.
"""

from __future__ import annotations

import html as _html
import re

# A restrained, print-like palette: ink for content, gray for chrome.
_INK = "#1f2937"
_MUTED = "#6b7280"
_HAIRLINE = "#e5e7eb"
_BOX_BG = "#f6f7f9"
_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
    "sans-serif"
)

_NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")


def escape(text: str) -> str:
    """HTML-escape user/model-supplied text (payees, memos, rationales)."""
    return _html.escape(text, quote=True)


def wrap_email(*blocks: str) -> str:
    """The shared shell: one narrow column of comfortable type."""
    inner = "\n".join(block for block in blocks if block)
    return (
        f'<div style="font-family:{_FONT};color:{_INK};font-size:14px;'
        f'line-height:1.55;max-width:560px;">{inner}</div>'
    )


def facts_block(
    *, payee: str, amount: str, date: str, memo: str | None = None
) -> str:
    """The transaction header: payee, the amount big, then date and memo."""
    memo_html = (
        f'<div style="font-size:13px;color:{_MUTED};margin-top:2px;">'
        f"{escape(memo.strip())}</div>"
        if memo and memo.strip()
        else ""
    )
    return (
        '<div style="margin:0 0 16px;">'
        f'<div style="font-size:15px;font-weight:600;">{escape(payee)}</div>'
        '<div style="font-size:24px;font-weight:700;'
        f'letter-spacing:-0.01em;margin:2px 0;">{escape(amount)}</div>'
        f'<div style="font-size:13px;color:{_MUTED};">{escape(date)}</div>'
        f"{memo_html}"
        "</div>"
    )


def suggestion_block(
    category: str, alternatives: tuple[str, ...], rationale: str | None
) -> str:
    """The proposal's centerpiece: the suggested category, set in a soft box."""
    alts_html = (
        f'<div style="font-size:13px;color:{_MUTED};margin-top:2px;">'
        f"or: {escape(', '.join(alternatives))}</div>"
        if alternatives
        else ""
    )
    rationale_html = (
        f'<div style="font-size:13px;color:{_MUTED};margin-top:6px;">'
        f"{escape(rationale)}</div>"
        if rationale
        else ""
    )
    return (
        f'<div style="margin:16px 0;padding:12px 14px;background:{_BOX_BG};'
        'border-radius:8px;">'
        '<div style="font-size:11px;font-weight:600;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:{_MUTED};">Suggested</div>'
        '<div style="font-size:16px;font-weight:600;margin-top:2px;">'
        f"{escape(category)}</div>"
        f"{alts_html}{rationale_html}"
        "</div>"
    )


def prompt_block(text: str) -> str:
    """A question the owner must answer — slightly louder than body text."""
    return (
        '<div style="font-size:15px;font-weight:500;margin:0 0 14px;">'
        f"{_break_lines(text)}</div>"
    )


def paragraphs(text: str) -> str:
    """Blank-line-separated blocks as paragraphs; numbered blocks as options.

    The generic transform behind :func:`text_to_html` — good typography for
    any plain-text body. A block whose every line reads ``N. …`` is laid out
    as an options list (the balance offer's shape), with the part before
    `` — `` bolded as the option's label.
    """
    out: list[str] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        if all(_NUMBERED.match(line) for line in lines):
            out.extend(_option_row(line) for line in lines)
        else:
            out.append(f'<p style="margin:0 0 14px;">{_break_lines(block)}</p>')
    return "\n".join(out)


def footer_block(text: str) -> str:
    """The understated how-to-reply hint, under a hairline."""
    return (
        f'<div style="margin-top:18px;padding-top:10px;border-top:1px solid '
        f'{_HAIRLINE};font-size:12.5px;color:{_MUTED};">{escape(text)}</div>'
    )


def text_to_html(text: str) -> str:
    """A clean HTML rendering of any plain-text body (the default for sends).

    Every email gets at least this: the shared shell and paragraph typography,
    so no message ever lands as raw unstyled text.
    """
    return wrap_email(paragraphs(text))


def _break_lines(block: str) -> str:
    return "<br>".join(escape(line) for line in block.split("\n"))


def _option_row(line: str) -> str:
    match = _NUMBERED.match(line)
    if match is None:  # pragma: no cover - callers pre-check
        return f'<p style="margin:0 0 14px;">{escape(line)}</p>'
    number, content = match.groups()
    label, separator, rest = content.partition(" — ")
    rest_html = (
        f'<div style="font-size:13px;color:{_MUTED};margin-left:1.5em;">'
        f"{escape(rest)}</div>"
        if separator
        else ""
    )
    return (
        '<div style="margin:0 0 10px;">'
        f'<div><span style="font-weight:700;">{number}.</span> '
        f'<span style="font-weight:600;">{escape(label)}</span></div>'
        f"{rest_html}"
        "</div>"
    )
