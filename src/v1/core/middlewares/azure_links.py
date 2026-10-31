"""Keep each Azure link in an answer to one the conversation holds.

QA (#5): on a generic pipeline, the link in the final answer opened ADF
Studio's start page — the directory, subscription and factory pickers — instead
of the failed run. Studio opens on that page whenever a link's ``factory=`` is
missing or names a factory it cannot open.

The ADF tools build every link from the factory the run was found in
(``_run_url``), and on stage the generic cases carried those links through
intact. A link a model writes itself has nothing behind it:

- a ticket's 'Resource Group:' is a GUID, not the group's name, so a factory
  path built from the ticket opens no factory;
- a long link gets shortened, and its ``factory=`` goes with it;
- an id that is not a run gets a run link: the Triggered By GUID (an activity
  run), a Run Group ID, a parentRunId put into its child's link with another
  factory.

The prompts already say to pass links through as written, and they are at their
caps. So this middleware checks the final answers, the orchestrator's and each
subagent's, and in them every link to ADF Studio and every Azure portal link
that names a resource. Each is:

- **swapped** for the run's own link when it opens a run the conversation links,
  or one whose ID differs only past its first three groups (copy_check's slip).
  A tool's link for a run wins over any other copy of it. A parentRunId with no
  link of its own takes its run's link with its own ID: Execute Pipeline never
  crosses factories, so a parent runs in its child's factory;
- **kept** when the conversation holds it, read through markdown escapes,
  '&amp;' and percent-encoding;
- **kept** when it opens a Studio page of a factory that a held link opens;
- **repaired** when it is a held link with its end cut off, its case or
  punctuation changed, or a few characters slipped inside one of its GUIDs. One
  that differs by a name ('df-sales-prd' for 'df-sales-dev') is another link;
- **removed** otherwise. Its words stay ('the parent run', a run ID) unless all
  they ever said was "open this" ('open run', 'here', the URL itself), a bare
  URL goes with the '( )' round it, and an answer left with nothing says that
  the link was left out.

A bare link with text glued to it ('…/df-sample—it') is set apart from that
text, which the renderer would otherwise make part of the link.

What counts as held: in a subagent, its tools' results only. Its task text is
the orchestrator's writing, and a link the orchestrator made up there must not
come back to it as a subagent's finding. In the orchestrator, every tool result
but the answers of deepagents' general-purpose subagent (the one subagent that
does not check its own links), its own earlier answers, checked as each was
written, which keep a link once summarization drops the result it came from,
and what the user wrote. Never a system prompt (the prompts'
'https://adf.azure.com/...' is an example) or the summary that stands for the
older turns.

Any other link (a ServiceNow ticket, a knowledge-base article, the portal's
home page) is left as written. A message that calls tools is not an answer and
is not checked, and a check that errors leaves the answer as it was.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlsplit

from langchain.agents.middleware import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# deepagents adds this subagent with middleware of its own, so its answer is a
# model's writing that nothing checked against its tools' results.
UNCHECKED_SUBAGENTS = frozenset({"general-purpose"})

_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ANY_GUID = re.compile(_GUID, re.IGNORECASE)
# What a URL is made of (RFC 3986, and the letters of any language: a resource
# group can be named in one), less the quote and the brackets that close the text
# round one. A URL ends at the first character it cannot hold, so text glued to
# one ('…/df-sample—it') is not read as part of it.
_URL_CHARS = r"\w\-.~:/?#@!$&()*+,;=%"
# A URL in a source. No backslash: a result that is not JSON can hold a line
# break written '\n'. Its end is trimmed after (_trimmed).
_SOURCE_URL = re.compile(rf"https?://[{_URL_CHARS}]+", re.IGNORECASE)
_LINE_BREAK = re.compile(r"\r?\n|\\n")
# The HTML a ticket's text can carry round a URL: '&quot;' ends one as '"' does.
_ENTITIES = {"&amp;": "&", "&quot;": '"', "&#39;": "'", "&#x27;": "'", "&lt;": "<", "&gt;": ">"}
_ENTITY = re.compile("|".join(map(re.escape, _ENTITIES)), re.IGNORECASE)
# What ends a sentence or closes emphasis after a URL, rather than the URL.
_TRAILING = ".,;:!?*_~"
# A markdown backslash escape: '\_'.
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_HEAD = re.compile(r"(https?://[^/?#]*)(.*)", re.IGNORECASE | re.DOTALL)
_STUDIO_RUN = re.compile(r"(/monitoring/pipelineruns/)([^/?#]+)", re.IGNORECASE)
# Studio's start page, which opens nothing in particular: '/', '/en', '/en-us/home'.
_STUDIO_START = re.compile(r"/?(?:[a-z]{2}(?:-[a-z]{2})?/?)?(?:home/?)?", re.IGNORECASE)
# A portal link that names something: a resource path or an id.
_PORTAL_RESOURCE = re.compile(
    rf"{_GUID}|/subscriptions/|/resourcegroups/|/providers/|/resource/|factory=",
    re.IGNORECASE,
)
# 'parentRunId=x', 'Parent Run ID   : x', '**Parent Run ID:** x'.
_PARENT_RUN = re.compile(rf"parent\W{{0,3}}run\W{{0,3}}id\W{{0,6}}({_GUID})", re.IGNORECASE)
# 'runId=x': a line that prints a run of its own is a row, not a field of the run
# linked above it.
_PRINTED_RUN = re.compile(rf"run\W{{0,3}}id\W{{0,6}}({_GUID})", re.IGNORECASE)
# A run ID's first three groups, the ones that differ from run to run (see
# copy_check).
_ID_PREFIX = 18
# The most characters a slip changes in a GUID: a hyphen written twice, a digit
# dropped or swapped.
_SLIP = 3
# How much of a link a cut-off copy keeps, at least.
_CUT_KEEPS = 0.8
_PUNCTUATION = re.compile(r"[\W_]")

# How far the renderer runs a bare URL on (GFM's autolink literal): to the next
# space or '<'. A quote, a bracket, a '|' (a table cell's edge) or a backtick ends
# one here too; the tools never put one in a link.
_BARE = r"https?://[^\s<>`\"'\[\]|]+"
# In an answer, in the order they open, so a URL inside a link or a code span is
# read with it: a markdown link ('[label](<url>)' or '[label](url)', with or
# without a title, a '[1]' allowed in the label), an autolink ('<url>'), a code
# span, a bare URL.
_ANSWER_LINK = re.compile(
    r"\[(?P<label>(?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]\([ \t]*"
    r"(?:<(?P<angled>[^<>\n]*)>|(?P<dest>(?:[^\s()<>]|\([^\s()<>]*\))+))"
    r"(?P<title>[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'))?[ \t]*\)"
    r"|<(?P<auto>https?://[^<>\s]+)>"
    r"|`(?P<code>[^`\n]+)`"
    rf"|(?P<bare>{_BARE})",
    re.IGNORECASE,
)
_BARE_URL = re.compile(_BARE, re.IGNORECASE)
# The URL a bare one starts with, as written: its escapes ('\_') read with it.
_WRITTEN_URL = re.compile(rf"https?://[{_URL_CHARS}\\]+", re.IGNORECASE)

# A word: a name or an ID ('pl_orders_daily', a GUID, "parent's") is one.
_WORD = re.compile(r"[^\W_]+(?:[._'’-]+[^\W_]+)*")
# What a link's label starts with when all it says is "open this".
_OPEN_VERBS = frozenset(
    {
        "open",
        "view",
        "see",
        "click",
        "go",
        "check",
        "visit",
        "follow",
        "use",
        "tap",
        "launch",
        "browse",
    }
)
_POINTERS = frozenset(
    {
        "here",
        "link",
        "links",
        "url",
        "this",
        "the",
        "that",
        "it",
    }
)
_POINTING = _OPEN_VERBS | _POINTERS
# The words that say no more than that there is a link, and where it goes.
_LINK_WORDS = (
    frozenset(
        {
            "a",
            "an",
            "the",
            "this",
            "that",
            "these",
            "those",
            "it",
            "its",
            "there",
            "is",
            "are",
            "you",
            "can",
            "your",
            "to",
            "in",
            "on",
            "at",
            "for",
            "of",
            "from",
            "with",
            "via",
            "and",
            "or",
            "by",
            "urls",
            "adf",
            "azure",
            "data",
            "factory",
            "factories",
            "studio",
            "portal",
            "monitor",
            "monitoring",
            "page",
            "tab",
            "new",
            "run",
            "runs",
            "pipeline",
            "pipelines",
            "activity",
            "alert",
            "alerts",
            "details",
            "detail",
            "log",
            "logs",
            "output",
            "error",
            "errors",
            "failed",
            "parent",
            "child",
            "triggered",
            "started",
            "note",
            "more",
            "info",
            "resource",
            "overview",
        }
    )
    | _POINTING
)
# Stands where a link was removed until the text round it is tidied.
_REMOVED = "\ue000"
# The '( )', '[ ]' or emphasis round a removed link, and the space before it.
_REMOVED_WRAPPED = re.compile(
    rf"(?<![ \t])[ \t]*(?:\(\s*{_REMOVED}\s*\)|\[\s*{_REMOVED}\s*\]"
    rf"|(\*\*|__|~~|\*|_){_REMOVED}\1)"
)
# A removed link that starts a line or a list item, with the separator after it:
# '1. [open run](…) — Failed' is item 1, 'Failed'.
_REMOVED_FIRST = re.compile(
    rf"^([ \t]*(?:(?:[-*+>]|\d+[.)])[ \t]+)*){_REMOVED}[ \t]*(?:[—–·:,-][ \t]*)?"
)
# A removed link after the text it follows, with the separator or comma before it.
_REMOVED_AFTER = re.compile(rf"(?<![ \t])(?:(?<=\S)[ \t]+[—–·-]|,)?[ \t]*{_REMOVED}")
_LETTER_OR_DIGIT = re.compile(r"[^\W_]")
# What an answer says when the links were all it had.
LEFT_OUT = (
    "I couldn't verify that Azure link, so I've left it out. Ask me to look it up "
    "and I'll share a verified one."
)


def _plain(url: str) -> str:
    """``url`` as a browser follows it: markdown escapes and '&amp;' read through."""

    return _ESCAPE.sub(r"\1", url.strip()).replace("&amp;", "&")


def _key(url: str) -> str:
    """What every copy of one link shares: its scheme and host in lower case, the
    rest percent-decoded, no '/' at the end."""

    plain = _plain(url)
    match = _HEAD.fullmatch(plain)
    if match is None:
        return unquote(plain).rstrip("/")
    return (match[1].lower() + unquote(match[2])).rstrip("/")


def _trimmed(url: str) -> str:
    """``url`` without the punctuation or the ')' that closes the text round it."""

    opened, closed = url.count("("), url.count(")")
    end = len(url)
    while end:
        if url[end - 1] in _TRAILING:
            end -= 1
        elif url[end - 1] == ")" and closed > opened:
            closed -= 1
            end -= 1
        else:
            break
    return url[:end]


def _site(url: str) -> str | None:
    """'studio' or 'portal' for the hosts checked, else ``None``."""

    try:
        host = (urlsplit(_plain(url)).hostname or "").lower()
    except ValueError:
        return None
    if host in ("adf.azure.com", "ms-adf.azure.com") or host.endswith(".adf.azure.com"):
        return "studio"
    if host == "portal.azure.com" or host.endswith(".portal.azure.com"):
        return "portal"
    return None


def _names_something(url: str, site: str) -> bool:
    """Whether ``url`` opens something in particular: a Studio page past the start
    page, or a portal page with a resource path or an id in it."""

    if site == "portal":
        return bool(_PORTAL_RESOURCE.search(unquote(_plain(url))))
    parts = urlsplit(_plain(url))
    return bool(parts.query or parts.fragment) or not _STUDIO_START.fullmatch(parts.path)


def _checks(url: str) -> bool:
    """Whether ``url`` is a link this middleware checks."""

    site = _site(url)
    return site is not None and _names_something(url, site)


def _run_id(url: str) -> str | None:
    """The run a Studio link opens, in lower case, or ``None``."""

    try:
        match = _STUDIO_RUN.search(urlsplit(_plain(url)).path)
    except ValueError:
        return None
    return unquote(match[2]).strip().lower() if match else None


def _factory(url: str) -> str:
    """The factory a Studio link opens (its ``factory=``), in lower case, or ''."""

    try:
        values = parse_qs(urlsplit(_plain(url)).query).get("factory", [])
    except ValueError:
        return ""
    return values[0].strip().rstrip("/").lower() if values else ""


def _with_run(url: str, run_id: str) -> str:
    """``url``, a Studio run link, opening ``run_id`` instead."""

    return _STUDIO_RUN.sub(lambda match: match[1] + quote(run_id, safe=""), url, count=1)


def _shape(key: str) -> str:
    """``key`` in lower case, without its punctuation."""

    return _PUNCTUATION.sub("", key).lower()


def _slipped(written: str, held: str) -> bool:
    """Whether ``written`` is ``held`` with its end cut off, or a few characters
    slipped inside one of its GUIDs. A copy that differs by a name or a word opens
    something else, as far as anyone can tell."""

    if held.startswith(written):
        return len(written) >= _CUT_KEEPS * len(held)
    if abs(len(written) - len(held)) > _SLIP:
        return False
    prefix = len(os.path.commonprefix([written, held]))
    room = min(len(written), len(held)) - prefix
    suffix = 0
    while suffix < room and written[-1 - suffix] == held[-1 - suffix]:
        suffix += 1
    if max(len(written), len(held)) - prefix - suffix > _SLIP:
        return False
    end = len(held) - suffix
    return any(guid.start() <= prefix and end <= guid.end() for guid in _ANY_GUID.finditer(held))


def _shown(url: str) -> str:
    """``url`` for a log line: no subscription, no query beyond the factory."""

    try:
        parts = urlsplit(_plain(url))
    except ValueError:
        return url[:60]
    shown = f"{parts.netloc}{parts.path}"[:100]
    if factory := _factory(url):
        # '/subscriptions/s/resourcegroups/rg/providers/…/factories/f' -> 'rg/f'
        segments = factory.split("/")
        shown += f" (factory {'/'.join(segments[4:5] + segments[-1:])})"
    elif parts.query or parts.fragment:
        shown += " (+query)"
    return shown


def _strings(content: object) -> Iterator[str]:
    """The text of a message's content. A JSON result (ServiceNow's) is read string
    by string, so a URL in it reads as the tool returned it."""

    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                yield from _strings(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                yield from _strings(block["text"])
        return
    if not isinstance(content, str):
        return
    if content.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(content)
        except (ValueError, RecursionError):
            pass
        else:
            yield from _json_strings(data)
            return
    yield content


def _json_strings(data: object) -> Iterator[str]:
    """The strings in ``data``, in order. A stack, not recursion: JSON can nest
    deeper than Python recurses."""

    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _held_texts(messages: list[AnyMessage], whole_conversation: bool) -> list[str]:
    """The texts a link may come from, tool results first: a run's own link wins
    over one an answer or the user wrote."""

    unchecked = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls or []
        if call.get("name") == "task"
        and (call.get("args") or {}).get("subagent_type") in UNCHECKED_SUBAGENTS
    }
    results = [
        text
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id not in unchecked
        for text in _strings(message.content)
    ]
    if not whole_conversation:
        return results
    # Each was checked as it was written. One from before this check, or one it
    # failed open on, was not, and holds what it held then.
    answered = [
        text
        for message in messages
        if isinstance(message, AIMessage) and not message.tool_calls
        for text in _strings(message.content)
    ]
    # A HumanMessage a middleware added (the summary that stands for the older
    # turns) is marked with its source; what the user wrote is not.
    typed = [
        text
        for message in messages
        if isinstance(message, HumanMessage) and "lc_source" not in message.additional_kwargs
        for text in _strings(message.content)
    ]
    return results + answered + typed


@dataclass
class _LinkSources:
    """What an answer's links are checked against."""

    # Each held link by its key (_key), as the conversation holds it.
    held: dict[str, str] = field(default_factory=dict)
    # Each run a held link opens -> that link; a parentRunId with no link of its
    # own -> its run's link with its ID.
    runs: dict[str, str] = field(default_factory=dict)
    # The factories the held Studio links open (their factory=, in lower case).
    factories: set[str] = field(default_factory=set)
    # By site, each held link that opens no run: its key and its shape (_shape).
    near: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def checked(self, url: str) -> str | None:
        """The link to write for ``url``: itself, the link it should be, or
        ``None`` to remove it."""

        site = _site(url)
        if site is None or not _names_something(url, site):
            return url
        key = _key(url)
        run = _run_id(url) if site == "studio" else None
        if run is not None:
            # The run's link, even when another copy is held: one the user pasted
            # can open the run in a factory it is not in.
            new = self._run_link(run)
        elif key in self.held or (site == "studio" and _factory(url) in self.factories):
            return url
        else:
            new = self._nearest(key, site)
        # A link the model wrote as it is held, escapes and all, stays as written.
        return url if new is not None and _key(new) == key else new

    def _run_link(self, run: str) -> str | None:
        if run in self.runs:
            return self.runs[run]
        if len(run) < _ID_PREFIX:
            return None
        found = [held for held in self.runs if held[:_ID_PREFIX] == run[:_ID_PREFIX]]
        return self.runs[found[0]] if len(found) == 1 else None

    def _nearest(self, key: str, site: str) -> str | None:
        # A held link with its case or punctuation changed, or slipped. A run link
        # is never a near miss: it opens its run or none.
        shape = _shape(key)
        found = [
            held_key
            for held_key, held_shape in self.near.get(site, [])
            if shape == held_shape or _slipped(key, held_key)
        ]
        return self.held[found[0]] if len(found) == 1 else None


def _link_sources(messages: list[AnyMessage], whole_conversation: bool) -> _LinkSources:
    sources = _LinkSources()
    parents: dict[str, set[str]] = {}
    for text in _held_texts(messages, whole_conversation):
        if "azure.com" not in text.lower():
            continue
        text = _ENTITY.sub(lambda entity: _ENTITIES[entity[0].lower()], text)
        # The run links of the nearest line above with any, in this block: a run's
        # card links it on its header, and its fields (a parentRunId) follow.
        above: list[tuple[str, str]] = []
        for line in _LINE_BREAK.split(text):
            # An answer is held as it was written: 'rg\_data\_dev'.
            line = _ESCAPE.sub(r"\1", line)
            if not line.strip():
                above = []
                continue
            row_links: list[tuple[str, str]] = []
            for match in _SOURCE_URL.finditer(line):
                url = _trimmed(match[0])
                site = _site(url)
                if site is None:
                    continue
                key = _key(url)
                run = _run_id(url)
                if key not in sources.held:
                    sources.held[key] = url
                    if run is None:
                        sources.near.setdefault(site, []).append((key, _shape(key)))
                if site != "studio":
                    continue
                if factory := _factory(url):
                    sources.factories.add(factory)
                if run:
                    sources.runs.setdefault(run, url)
                    row_links.append((run, url))
            for match in _PARENT_RUN.finditer(line):
                parent = match[1].lower()
                links = row_links
                if not links and {run[1].lower() for run in _PRINTED_RUN.finditer(line)} <= {
                    parent
                }:
                    links = above
                row_link = next((url for run, url in links if run != parent), None)
                if row_link is not None:
                    parents.setdefault(parent, set()).add(_with_run(row_link, parent))
            if row_links:
                above = row_links
    for parent, links in parents.items():
        if parent not in sources.runs and len({_key(url) for url in links}) == 1:
            sources.runs[parent] = links.pop()
    return sources


def _link_words_only(text: str) -> bool:
    return all(word.lower() in _LINK_WORDS for word in _WORD.findall(text))


def _pointing_only(text: str) -> bool:
    return all(word.lower() in _POINTING for word in _WORD.findall(text))


def _only_points(label: str) -> bool:
    """Whether a link's label only ever said "open this": no words but its URL,
    link words that start with a verb like 'open', or words that only point
    ('here', 'this link')."""

    words = [word.lower() for word in _WORD.findall(_BARE_URL.sub(" ", label))]
    if not words:
        return True
    if not all(word in _LINK_WORDS for word in words):
        return False
    return words[0] in _OPEN_VERBS or all(word in _POINTING for word in words)


def _emptied(line: str) -> bool:
    """Whether ``line``, a link taken out of it, is left saying nothing: no letter
    or digit, or a caption of link words with nothing after it but words that
    pointed at the link ('Link:', '- **Parent run:**', 'Note: see'). 'Run:
    failed' says something. A table row keeps its cells, and a line with a URL
    left in it is kept."""

    if "|" in line or _BARE_URL.search(line):
        return False
    if not _LETTER_OR_DIGIT.search(line):
        return True
    caption, colon, after = line.rpartition(":")
    return bool(colon) and _link_words_only(caption) and _pointing_only(after)


def _tidied(text: str) -> str:
    """``text`` with the marks of removed links gone, and a line left saying
    nothing dropped. An answer left with nothing says the link was left out."""

    kept = []
    for line in text.split("\n"):
        if _REMOVED in line:
            line = _REMOVED_FIRST.sub(r"\1", _REMOVED_WRAPPED.sub("", line))
            line = _REMOVED_AFTER.sub("", line)
            if _emptied(line):
                continue
        kept.append(line)
    tidied = "\n".join(kept)
    return tidied if _LETTER_OR_DIGIT.search(tidied) else LEFT_OUT


def _checked_text(text: str, sources: _LinkSources) -> tuple[str, list[str]]:
    """``text`` with its Azure links checked, and what changed."""

    changes: list[str] = []

    def check(url: str) -> str | None:
        new = sources.checked(url)
        if new is None:
            changes.append(f"removed {_shown(url)}")
        elif new != url:
            changes.append(f"{_shown(url)} -> {_shown(new)}")
        return new

    def bare(written: str, rendered: bool = True) -> str:
        # The renderer links all of ``linked``: past the URL, that is text glued
        # to it ('—it', '}'), which a link it checks is set apart from.
        linked = _trimmed(written)
        match = _WRITTEN_URL.match(linked)
        url = _trimmed(match[0]) if match else linked
        new = check(url)
        rest = written[len(url) :]
        glued = rendered and len(url) < len(linked)
        if new is None:
            return _REMOVED + (f" {rest}" if glued else rest)
        if glued and _checks(url):
            if new == url:
                changes.append(f"set apart the text glued to {_shown(url)}")
            return f"{new} {rest}"
        return written if new == url else new + rest

    def link(match: re.Match[str]) -> str:
        if match["label"] is not None:
            label = match["label"]
            url = match["angled"] if match["angled"] is not None else match["dest"]
            new = check(url)
            if new == url:
                return match[0]
            if new is None:
                if _only_points(label):
                    return _REMOVED
                # The words stay, a URL in them checked as a bare one is.
                return _BARE_URL.sub(lambda url: bare(url[0]), label)
            if _plain(label) == _plain(url):
                label = new
            return f"[{label}](<{new}>{match['title'] or ''})"
        if match["auto"] is not None:
            new = check(match["auto"])
            if new == match["auto"]:
                return match[0]
            return _REMOVED if new is None else f"<{new}>"
        if match["code"] is not None:
            # Code is not rendered as a link: nothing in it runs on.
            code = _BARE_URL.sub(lambda url: bare(url[0], rendered=False), match["code"])
            if code == match["code"]:
                return match[0]
            code = code.replace(_REMOVED, "").strip()
            return f"`{code}`" if code else _REMOVED
        return bare(match["bare"])

    checked = _ANSWER_LINK.sub(link, text)
    if not changes:
        return text, changes
    return _tidied(checked), changes


def _checked_message(message: AIMessage, sources: _LinkSources, label: str) -> AIMessage:
    """``message`` with its Azure links checked (the same instance when none changed)."""

    changes: list[str] = []
    content = message.content
    if isinstance(content, str):
        new_content, changes = _checked_text(content, sources)
    elif isinstance(content, list):
        new_content = []
        for block in content:
            if isinstance(block, str):
                text, block_changes = _checked_text(block, sources)
                if block_changes:
                    changes += block_changes
                    block = text
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                text, block_changes = _checked_text(block["text"], sources)
                if block_changes:
                    changes += block_changes
                    block = {**block, "text": text}
            new_content.append(block)
    else:
        return message
    if not changes:
        return message
    logger.warning("Link check changed links in %s: %s", label, "; ".join(changes))
    return message.model_copy(update={"content": new_content})


def links_checked(
    request: ModelRequest, response: object, whole_conversation: bool, label: str
) -> object:
    """``response`` with the Azure links of each final answer in it checked
    (``label`` names the answer in the log)."""

    if isinstance(response, ExtendedModelResponse):
        response.model_response = links_checked(
            request, response.model_response, whole_conversation, label
        )
        return response
    if isinstance(response, AIMessage):
        messages = [response]
    elif isinstance(response, ModelResponse):
        messages = response.result
    else:
        return response
    sources: _LinkSources | None = None
    checked = []
    for message in messages:
        if (
            isinstance(message, AIMessage)
            and not message.tool_calls
            and "azure.com" in str(message.content).lower()
        ):
            try:
                sources = sources or _link_sources(request.messages, whole_conversation)
                message = _checked_message(message, sources, label)
            except Exception:  # the answer matters more than its links — fail open
                logger.warning("Link check of %s errored; failing open", label, exc_info=True)
        checked.append(message)
    if isinstance(response, AIMessage):
        return checked[0]
    response.result = checked
    return response


class AzureLinksMiddleware(AgentMiddleware):
    """Keep each ADF Studio and Azure portal link in a final answer to one the
    conversation holds: a run's link is swapped for its own, a slip is put back
    and any other link is removed."""

    def __init__(self, *, whole_conversation: bool = False, label: str = "the answer") -> None:
        # The orchestrator also holds its earlier answers and what the user
        # wrote; a subagent holds its tools' results only.
        self.whole_conversation = whole_conversation
        self.label = label

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return links_checked(request, handler(request), self.whole_conversation, self.label)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        response = await handler(request)
        return links_checked(request, response, self.whole_conversation, self.label)
