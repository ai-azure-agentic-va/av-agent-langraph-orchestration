"""LangChain tool for the ServiceNow Knowledge Articles endpoint.

Registered on the ServiceNow subagent alongside the incident tools (see
:mod:`v1.core.subagents.servicenow.subagent`) — it is a ServiceNow-specific tool,
so it belongs with the rest of ServiceNow rather than at the orchestrator level.
The ``ownership_group`` parameter is a SEARCH filter, not an access control — it
is caller-supplied and therefore omittable. The corpus is open to every caller:
the teams confirmed they want it shared, the same decision that leaves incidents
unscoped (see :mod:`v1.core.middlewares.subagent_access`).

Three things this layer owns that the client does not:

* ``article_body`` arrives as HTML with entities; the output contract is
  markdown, so it is converted here (``<a href>`` becomes a markdown link —
  these articles are largely pointers to internal tooling, and stripping the
  links destroys the answer).
* an expiry gate on ``valid_to``, so a superseded article is never quoted back
  as current guidance.
* rendering, so the orchestrator reproduces a finished block rather than
  re-deriving one (same contract as the incident tools).
"""

from __future__ import annotations

import html
import os
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Any

from langchain_core.tools import tool
from pydantic import Field

from v1.core.tools.servicenow.tools import (
    SOURCE,
    ServiceNowToolInputError,
    _error_payload,
    get_servicenow_client,
)
from v1.utils.clients.servicenow import (
    ServiceNowConfigurationError,
    ServiceNowError,
    _reference_display,
    _reference_value,
)

# The endpoint's own page size is 100 and the whole corpus fits in one page
# today, so paging is untested against it. We ask for a bounded page anyway; the
# tool exposes no cursor, so when more articles matched than we fetched the
# rendered answer says "showing the top N" rather than claiming a total it
# cannot defend (see _resolve_totals).
MAX_ARTICLE_LIMIT = 25
try:
    DEFAULT_ARTICLE_LIMIT = min(
        max(int(os.getenv("SERVICENOW_KNOWLEDGE_DEFAULT_LIMIT", "5")), 1),
        MAX_ARTICLE_LIMIT,
    )
except ValueError:
    DEFAULT_ARTICLE_LIMIT = 5

# How much converted body text one article contributes to the rendered answer.
# Bodies run to ~4 KB, so this fits a WHOLE runbook: the answer to "share the
# steps to clean up /var" is the steps themselves, and a cap that lands mid-
# procedure fails the ask while still costing the context it spent. The link is a
# citation, not a substitute for the procedure.
_BODY_CHAR_LIMIT = 4000

_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_END_RE = re.compile(
    r"</(?:p|div|li|ul|ol|tr|h[1-6]|blockquote)\s*>|<br\s*/?>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_markdown(raw: Any) -> str:
    """Flatten a ServiceNow ``article_body`` (HTML + entities) to markdown text.

    Anchors become markdown links so the pointer survives; block-level tags
    become newlines; every other tag is dropped and entities are unescaped.
    Deliberately not a general HTML parser — this endpoint emits a small, known
    tag vocabulary, and the alternative is a dependency for a dozen lines.

    An anchor whose text already equals its href renders as the bare URL: a
    ``[https://x](https://x)`` link reads as noise, and the plain URL is still
    auto-linked by the renderer.
    """

    text = "" if raw is None else str(raw)
    if not text:
        return ""

    def _anchor(match: re.Match[str]) -> str:
        href = html.unescape(match.group(1)).strip()
        label = html.unescape(_TAG_RE.sub("", match.group(2))).strip()
        if not href:
            return label
        if not label or label == href:
            return href
        return f"[{label}]({href})"

    text = _ANCHOR_RE.sub(_anchor, text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    # Normalize CRLF and the non-breaking spaces ServiceNow sprinkles through
    # its bodies, so the blank-line collapse below actually sees blank lines.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [line.strip() for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def is_expired(article: Mapping[str, Any], *, today: date | None = None) -> bool:
    """Whether ``valid_to`` has passed. An absent or unparsable date is NOT expired.

    Failing open is the right default: the field is optional on the instance, and
    dropping every article whose expiry we could not read would silently empty
    the article list.
    """

    raw = _reference_value(article.get("valid_to")).strip()
    if not raw:
        return False
    try:
        valid_to = datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return valid_to < (today or date.today())


def normalize_article(article: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a raw article to the fields the agent may show. ``sys_id`` is dropped."""

    return {
        "number": _reference_value(article.get("number")).strip(),
        "title": _reference_value(article.get("short_description")).strip(),
        # display-only: an empty category must render as 'Not available', never as
        # the raw sys_id the value side carries.
        "category": _reference_display(article.get("category")).strip(),
        "author": _reference_display(article.get("author")).strip(),
        "ownership_group": _reference_display(article.get("ownership_group")).strip(),
        "published": _reference_value(article.get("published")).strip(),
        "valid_to": _reference_value(article.get("valid_to")).strip(),
        "article_url": _reference_value(article.get("article_url")).strip(),
        "body": html_to_markdown(article.get("article_body")),
    }


def _render_article(item: Mapping[str, Any], *, body_limit: int = _BODY_CHAR_LIMIT) -> str:
    number = item.get("number") or "Unknown"
    title = item.get("title") or "Untitled"
    url = item.get("article_url")
    heading = f"[{number}]({url})" if url else number
    lines = [f"**{heading}** — {title}"]

    meta = []
    if item.get("category"):
        meta.append(f"Category: {item['category']}")
    if item.get("author"):
        meta.append(f"Author: {item['author']}")
    if item.get("published"):
        meta.append(f"Published: {item['published']}")
    if meta:
        lines.append(" | ".join(meta))
    # No separate "Article Link:" line: the requirements template puts the
    # hyperlink on its own row because its number column is plain text, but the
    # header above already MAKES the number the link. Adding the row back prints
    # the same URL twice.

    body = (item.get("body") or "") if body_limit else ""
    if len(body) > body_limit:
        body = body[:body_limit].rstrip() + "\n\n… article continues; open the link for the full text."
    if body:
        lines.append(body)
        # Attribution belongs to an article we actually quoted — on the browse
        # view there is no body, and the header already names the article.
        lines.append(f"Source: Knowledge Article {number}")
    return "\n\n".join(lines)


def _resolve_totals(
    envelope: Mapping[str, Any], fetched: list[Any], live: list[Any]
) -> tuple[int | None, int]:
    """Return ``(live_total, expired_dropped)`` — both None/0 when unknowable.

    The expiry gate runs client-side, so a live total only exists when the page
    we fetched covered the WHOLE result set. Past that, the API's ``total_count``
    counts expired articles we never looked at, and subtracting the ones we did
    see invents a number: a 20-match / 10-live corpus fetched 10-at-a-time
    reports "found 15". Unknown must stay unknown — the same rule the client
    applies to ``total_count`` itself.
    """

    api_total = envelope.get("total_count")
    complete = not envelope.get("has_more") and (
        not isinstance(api_total, int) or api_total <= len(fetched)
    )
    if not complete:
        return None, 0
    return len(live), len(fetched) - len(live)


def _render_answer(
    items: list[dict[str, Any]],
    *,
    total: int | None,
    expired_dropped: int,
    with_bodies: bool = True,
) -> str:
    """Render the answer. ``with_bodies=False`` is the BROWSE view — index only.

    Bodies are dropped for an unfiltered "list every article" browse, where six
    stacked runbooks is a wall of text and number + title + category is all you
    need to PICK one. They are NOT dropped for a search, however many articles
    matched: a search names a symptom or a procedure, and the answer to "share
    the steps to clean up /var" is the steps — withholding them because five
    articles came back returns an index nobody asked for. (This used to key off
    the RESULT COUNT, which failed exactly that case.)
    """

    if not items:
        return "No knowledge articles matched that search."
    shown = len(items)
    if total is None:
        # More matched than we fetched, and there is no cursor to offer.
        header = f"Showing the top {shown} knowledge article{'s' if shown != 1 else ''}."
    elif total > shown:
        header = f"Found {total} knowledge articles; showing {shown}."
    else:
        header = f"Found {shown} knowledge article{'s' if shown != 1 else ''}."
    if expired_dropped:
        header += (
            f" ({expired_dropped} expired article"
            f"{'s were' if expired_dropped != 1 else ' was'} excluded.)"
        )
    body_limit = _BODY_CHAR_LIMIT if with_bodies else 0
    return "\n\n---\n\n".join(
        [header, *(_render_article(item, body_limit=body_limit) for item in items)]
    )


def _resolve_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_ARTICLE_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ServiceNowToolInputError("limit must be an integer")
    return min(max(limit, 1), MAX_ARTICLE_LIMIT)


@tool
async def servicenow_search_knowledge(
    query: Annotated[
        str,
        Field(
            description=(
                "Search terms for knowledge-article titles AND bodies. EVERY word "
                "you pass must appear VERBATIM in the article — there is no "
                "stemming, so 'logging' does not match an article that says "
                "'login'. Pass ONLY the distinctive terms (3-5 words): the product "
                "plus the symptom, or the quoted error text. NEVER paste the user's "
                "whole sentence. 'I get Access denied when signing into the "
                "Example Portal' returns NOTHING; 'Access denied' or 'Example "
                "Portal login' finds the "
                "article. If a search comes back empty, retry once with fewer, more "
                "distinctive words before reporting no results. Pass an empty "
                "string to list every article."
            )
        ),
    ] = "",
    title_contains: Annotated[
        str,
        Field(
            description=(
                "Optional substring the article TITLE must contain. Use only to "
                "narrow further; `query` already searches titles."
            )
        ),
    ] = "",
    ownership_group: Annotated[
        str,
        Field(
            description=(
                "Optional EXACT owning-group name for the articles (this is the "
                "knowledge scoping field; it is a different ServiceNow list from "
                "the incidents' assignment_group). Omit to search every article "
                "the endpoint exposes."
            )
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(description=f"Max articles to return (1-{MAX_ARTICLE_LIMIT})."),
    ] = DEFAULT_ARTICLE_LIMIT,
) -> dict[str, Any]:
    """Search ServiceNow knowledge articles (KB…) — the written PROCEDURES.

    Pick this tool from the SHAPE of the ask, never its topic: these articles and
    the incidents cover the same subjects. A PROCEDURE ask belongs here — "share
    the steps to <do X>", "how do I run / perform <X>", "what is the process for
    <X>", "is there a runbook for <X>", or a KB number. They want INSTRUCTIONS
    THEY CAN FOLLOW, and they do NOT have to say "knowledge article", "KB" or
    "runbook" for this to be the right tool. A FAILURE ask — a pasted error, a
    symptom, a ticket number, "<X> is broken / not working" — is an incident
    search instead, and incidents are the default when the shape is genuinely
    unclear. "How do I fix <X>" is a procedure; "<X> is not working" is a failure.

    Returns published KB articles with their converted body text and a link on
    the article number. Expired articles are excluded. These are ServiceNow's own
    knowledge articles — a different corpus from the `ai_search_tool` knowledge base.
    """

    try:
        resolved_limit = _resolve_limit(limit)
        filters: dict[str, Any] = {}
        if isinstance(query, str) and query.strip():
            # ponytail: passed through as-is. `keyword` is an AND-of-substrings
            # filter with no stemming, so a conversational sentence matches
            # nothing — the arg description carries that contract rather than a
            # stopword stripper here, which still could not turn 'logging' into
            # 'login'. Add a stemmer only if steering the caller proves unreliable.
            filters["keyword"] = query.strip()
        if isinstance(title_contains, str) and title_contains.strip():
            filters["short_description_contains"] = title_contains.strip()
        if isinstance(ownership_group, str) and ownership_group.strip():
            filters["ownership_group"] = ownership_group.strip()

        client = await get_servicenow_client()
        # Fetch a wider page than we render: the expiry gate runs client-side, so
        # asking for exactly `limit` rows would return fewer than requested
        # whenever an expired article lands in the page.
        envelope = await client.list_knowledge_articles(
            filters=filters, limit=min(resolved_limit * 2, MAX_ARTICLE_LIMIT * 2)
        )
        raw_articles = envelope.get("articles") or []
        live = [a for a in raw_articles if not is_expired(a)]
        total, expired_dropped = _resolve_totals(envelope, raw_articles, live)
        items = [normalize_article(a) for a in live[:resolved_limit]]

        return {
            "ok": True,
            "source": SOURCE,
            "kind": "knowledge_search",
            "rendered_answer": _render_answer(
                items,
                total=total,
                expired_dropped=expired_dropped,
                # No filters at all = "list every article", the browse view.
                with_bodies=bool(filters),
            ),
            "articles": items,
            "degraded": bool(envelope.get("degraded")),
        }
    except ServiceNowToolInputError as exc:
        return _error_payload(exc, kind="invalid_input")
    except (ServiceNowError, ServiceNowConfigurationError) as exc:
        return _error_payload(exc)


__all__ = [
    "DEFAULT_ARTICLE_LIMIT",
    "MAX_ARTICLE_LIMIT",
    "html_to_markdown",
    "is_expired",
    "normalize_article",
    "servicenow_search_knowledge",
]
