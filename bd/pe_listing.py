"""Reading a PE firm's own portfolio page: entries, their labels, pagination, and how complete the reading is. Deterministic; no model.

An entry is a name the page shows (with the card's own text as the excerpt, which is always a piece of the page's visible text). What the
parser will not do: guess a status the page does not state (an unlabeled entry stays unlabeled), guess a company's website from its name, or call a
listing complete when a page failed, a stated total does not match, or the page appears to load more entries with scripts."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlunparse

from bd.accounts import is_valid_domain, normalize_domain
from bd.config import Config
from bd.htmltext import Node, Page, ld_objects, links, node_text, parse_html
from bd.pe_store import norm_name
from bd.sources import Fetched, Fetcher, FetchError, store_snapshot, visible_text
from bd.urls import host_in, hostname, normalize_url, safe_urljoin, safe_urlparse

HIST = re.compile(r"\b(exit(ed|s)?|realized|realised|former|past|previous|prior|historical|divest(ed|ments?)?|sold|alumni)\b", re.I)
CURR = re.compile(r"\b(current|active|existing)\b", re.I)
STOP_NAMES = {"home", "about", "about us", "contact", "contact us", "team", "our team", "news", "careers", "login", "log in", "search", "menu", "portfolio",
              "our portfolio", "companies", "investments", "learn more", "read more", "view", "view all", "visit website", "website", "view company",
              "privacy", "privacy policy", "terms", "terms of use", "sitemap", "insights", "approach", "our approach", "strategy", "people", "all",
              "skip to content", "back to top", "next", "previous", "more", "filter", "sort", "industries", "sectors", "funds", "esg", "newsletter"}
SOCIAL = ("linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com", "vimeo.com", "medium.com", "wikipedia.org",
          "google.com", "goo.gl", "bit.ly", "apple.com", "play.google.com", "crunchbase.com", "pitchbook.com", "prnewswire.com", "businesswire.com",
          "globenewswire.com", "sec.gov", "bloomberg.com", "reuters.com", "wsj.com", "forbes.com", "cdn.", "wp.com")
NAVISH = ("nav", "menu", "footer", "header", "cookie", "breadcrumb", "social", "sidebar", "newsletter", "share")
CARD_WORDS = re.compile(r"(compan|portfolio|item|card|invest|tile|logo|entry|holding|brand|grid-?item|cell)", re.I)
LABEL_CLASS = re.compile(r"(status|badge|tag|label|stage|exit|realiz|current|active|category|term)", re.I)
STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
          "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
PLACE = re.compile(r"\b([A-Z][A-Za-z.'’-]+(?: [A-Z][A-Za-z.'’-]+){0,2}),\s*(" + "|".join(sorted(STATES)) + r")\b")
HQ_WORDS = re.compile(r"(headquarter|\bHQ\b|based in|headquartered)", re.I)
TOTAL = [re.compile(r"(?:showing|displaying)\s+(?:\d+\s*[-–]\s*)?\d+\s+of\s+(\d+)", re.I),
         re.compile(r"\b(\d{1,4})\s+(?:portfolio\s+)?(?:companies|investments|results)\b", re.I)]
MORE = re.compile(r"\b(load more|show more|view more|see more|more companies)\b", re.I)
NEXT_TEXT = re.compile(r"^\s*(next(\s+page)?|older( posts?)?|›|»|→|>)\s*$", re.I)


@dataclass
class Entry:
    name: str
    href: str | None = None                 # the firm's own detail page for it, when the card links to one
    domain: str | None = None               # the company's own site, only when the card links out to it
    section: str | None = None              # the headings it sat under, as written
    state: str = "unlabeled"                # current | historical | unlabeled
    by_contrast: bool = False               # current only because the page has a separate exited/realized section it is not in
    location: str | None = None             # "Chicago, IL" only when the listing shows it
    location_kind: str | None = None        # headquarters (the listing says so) | office (a place is shown, not said to be the HQ)
    excerpt: str = ""
    page_url: str = ""                      # the page or API response this entry was read from
    snapshot_id: int | None = None


@dataclass
class Listing:
    entries: list[Entry] = field(default_factory=list)
    next_urls: list[str] = field(default_factory=list)
    stated_total: int | None = None
    dynamic_hint: str | None = None
    wp_api: str | None = None
    notes: list[str] = field(default_factory=list)


def _ancestors_navish(n: Node) -> bool:
    while n is not None:
        if n.tag in ("nav", "header", "footer", "aside"):
            return True
        tokens = " ".join(n.classes()) + " " + n.attrs.get("id", "") + " " + n.attrs.get("role", "")
        if any(w in tokens.lower() for w in NAVISH):
            return True
        n = n.parent
    return False


def _clean_name(s: str) -> str | None:
    s = " ".join((s or "").split()).strip(" |-–—·•")
    if not s or len(s) > 70 or len(s.split()) > 7 or s.lower() in STOP_NAMES or not re.search(r"[A-Za-z]", s):
        return None
    if re.search(r"\b(20\d\d|read more|learn more|press release)\b", s, re.I) and len(s.split()) > 4:
        return None
    return s


def _card_name(card: Node, base: str) -> str | None:
    for n in card.walk():
        if re.fullmatch(r"h[1-6]", n.tag) and (nm := _clean_name(node_text(n))):
            return nm
    for n in card.walk():
        if n.attrs.get("class") and re.search(r"(name|title)", " ".join(n.classes()), re.I) and (nm := _clean_name(node_text(n))):
            return nm
    for _href, text, a in links(card, base):
        if nm := _clean_name(text):
            return nm
    for n in card.walk():
        if n.tag == "img" and (nm := _clean_name(n.attrs.get("alt", ""))):
            return nm
    return None


def _company_site(card: Node, base: str, firm_host: str) -> tuple[str | None, str | None]:
    """(the firm's own detail link, the company's site) among a card's links. A site is an external link that is not social media, a news
    wire, a filing or a CDN."""
    detail = site = None
    for href, _t, _a in links(card, base):
        h = hostname(href)
        if not h:
            continue
        if h == firm_host:
            detail = detail or href
        elif not host_in(h, SOCIAL) and not any(h.startswith(s) for s in SOCIAL if s.endswith(".")):
            d = normalize_domain(h)
            if is_valid_domain(d) and site is None:
                site = d
    return detail, site


def _label_text(card: Node) -> str:
    bits = [" ".join(re.split(r"[-_\s]+", " ".join(card.classes())))]
    for n in card.walk():
        if n is not card and LABEL_CLASS.search(" ".join(n.classes())):
            bits.append(node_text(n))
            bits.append(" ".join(re.split(r"[-_\s]+", " ".join(n.classes()))))
    return " ".join(bits)


def _heading_state(stack: list[tuple[int, str]]) -> str | None:
    for _lvl, text in reversed(stack):
        if HIST.search(text):
            return "historical"
        if CURR.search(text):
            return "current"
    return None


def _signature(n: Node) -> tuple:
    toks = tuple(sorted(c for c in n.classes() if CARD_WORDS.search(c)))
    return (n.tag, toks)


def _find_groups(root: Node) -> list[list[Node]]:
    groups: list[list[Node]] = []
    for container in root.walk():
        if container.tag in ("script", "style", "head") or _ancestors_navish(container):
            continue
        kids = container.elements()
        if len(kids) < 3:
            continue
        by_sig: dict[tuple, list[Node]] = {}
        for k in kids:
            by_sig.setdefault(_signature(k), []).append(k)
        for sig, members in by_sig.items():
            if len(members) >= 3 and sig[0] in ("li", "div", "article", "a", "section", "figure", "tr", "td", "span"):
                groups.append(members)
    return groups


def parse_listing(html_text: str, url: str) -> Listing:
    page = parse_html(html_text)
    firm_host = hostname(url)
    out = Listing()
    root = page.root

    # 1. candidate cards: repeated siblings that each carry a short name
    cards: list[tuple[Node, str]] = []
    claimed: set[int] = set()
    for members in _find_groups(root):
        named = [(m, _card_name(m, url)) for m in members if len(node_text(m)) <= 500]
        named = [(m, n) for m, n in named if n]
        if len(named) < 3 or sum(1 for _m, n in named if n.lower() in STOP_NAMES) * 2 >= len(named):
            continue
        for m, n in named:
            if id(m) in claimed or any(id(a) in claimed for a in _chain(m)):
                continue
            claimed.add(id(m))
            cards.append((m, n))
    # a group nested inside another group's card is the card's own parts, not entries
    card_ids = {id(c) for c, _n in cards}
    cards = [(c, n) for c, n in cards if not any(id(a) in card_ids for a in _chain(c)[1:])]
    if not cards:
        cards = _anchor_cards(root, url)

    # 2. headings in document order give each card its section
    inside: set[int] = set()
    for c, _n in cards:
        for d in c.walk():
            inside.add(id(d))
    card_by_id = {id(c): n for c, n in cards}
    stack: list[tuple[int, str]] = []
    for node in root.walk():
        if id(node) in card_by_id:
            card, name = node, card_by_id[id(node)]
            detail, site = _company_site(card, url, firm_host)
            label = _label_text(card)
            state = "historical" if HIST.search(label) else "current" if CURR.search(label) else None
            hs = _heading_state(stack)
            text = node_text(card)
            m = PLACE.search(text.replace(name, " ", 1))
            out.entries.append(Entry(
                name=name, href=detail, domain=site, section=" > ".join(t for _l, t in stack) or None, state=state or hs or "unlabeled",
                location=f"{m.group(1)}, {m.group(2)}" if m else None,
                location_kind=("headquarters" if HQ_WORDS.search(text) else "office") if m else None, excerpt=text[:400], page_url=url))
        elif id(node) not in inside and re.fullmatch(r"h[1-6]", node.tag):
            lvl = int(node.tag[1])
            stack[:] = [(l, t) for l, t in stack if l < lvl]
            t = node_text(node)
            if t:
                stack.append((lvl, t))

    # 3. a separate exited/realized section makes the unlabeled entries outside it current, and says so
    if any(e.state == "historical" for e in out.entries):
        for e in out.entries:
            if e.state == "unlabeled":
                e.state, e.by_contrast = "current", True

    # 4. pagination, stated totals, signs of script-loaded content, the site's public API
    out.next_urls = _next_links(root, url)
    ptxt = node_text(root)
    for rx in TOTAL:
        m = rx.search(ptxt)
        if m and int(m.group(1)) >= len(out.entries) > 0:
            out.stated_total = int(m.group(1))
            break
    if MORE.search(ptxt):
        out.dynamic_hint = "the page has a 'load more' control; further entries may load with scripts"
    elif not out.entries and any(n.attrs.get("id") in ("root", "__next", "app", "___gatsby") for n in root.walk()):
        out.dynamic_hint = "no entries in the page's HTML; the listing appears to be drawn by scripts"
    for n in root.walk():
        if n.tag == "link" and "api.w.org" in n.attrs.get("rel", ""):
            out.wp_api = n.attrs.get("href")
    if not out.entries:
        for o in ld_objects(page):
            if o.get("@type") == "ItemList":
                for it in o.get("itemListElement", []):
                    item = it.get("item", it) if isinstance(it, dict) else {}
                    nm = _clean_name(str(item.get("name", "")))
                    if nm:
                        out.entries.append(Entry(name=nm, href=item.get("url"), excerpt=nm, page_url=url))
    return out


def _chain(n: Node) -> list[Node]:
    out = []
    while n is not None:
        out.append(n)
        n = n.parent
    return out


def _anchor_cards(root: Node, url: str) -> list[tuple[Node, str]]:
    """A fallback for pages that are a plain list of links: anchors under the firm's company/portfolio paths, or out to other sites."""
    host = hostname(url)
    found: list[tuple[Node, str]] = []
    seen: set[str] = set()
    for href, text, a in links(root, url):
        if _ancestors_navish(a):
            continue
        h, p = hostname(href), safe_urlparse(href).path.lower()
        internal = h == host and re.search(r"/(compan|portfolio|investment|holding)[a-z-]*/[^/]+", p)
        external = h != host and not host_in(h, SOCIAL)
        nm = _clean_name(text)
        if nm and (internal or external) and nm.lower() not in seen:
            seen.add(nm.lower())
            found.append((a, nm))
    return found if len(found) >= 3 else []


def _next_links(root: Node, url: str) -> list[str]:
    base = safe_urlparse(url)
    out: list[str] = []

    def add(h: str) -> None:
        u = safe_urljoin(url, h.strip())
        p = safe_urlparse(u)
        if p.hostname and hostname(u) == hostname(url) and normalize_url(u) != normalize_url(url) and u not in out and p.scheme in ("http", "https"):
            out.append(u)

    for n in root.walk():
        if n.tag in ("a", "link") and "next" in n.attrs.get("rel", "").lower().split() and n.attrs.get("href"):
            add(n.attrs["href"])
    for href, text, a in links(root, url):
        label = (text + " " + a.attrs.get("aria-label", "")).strip()
        if NEXT_TEXT.match(text) or re.search(r"\bnext\b", a.attrs.get("aria-label", ""), re.I) or any(c.lower() in ("next", "next-page") for c in a.classes()):
            add(href)
        elif any(re.search(r"(pagination|pager|page-numbers|paging)", c, re.I) for p in _chain(a)[1:] for c in p.classes()):
            q = dict(parse_qsl(safe_urlparse(href).query))
            if any(k in q for k in ("page", "paged", "pg", "p")) or re.search(r"/page/\d+", href):
                add(href)
        del label
    return out


# ---- the crawl ---------------------------------------------------------------------------------------------------------------------------------
@dataclass
class PageResult:
    url: str
    ok: bool
    reason: str = "ok"
    entries: int = 0
    snapshot_id: int | None = None
    content_hash: str | None = None


@dataclass
class Collected:
    entries: list[Entry] = field(default_factory=list)
    pages: list[PageResult] = field(default_factory=list)
    coverage: str = "unknown"                 # complete | partial | unknown
    coverage_note: str = ""
    error: str | None = None                  # set when no page could be read at all
    via: str = "html"                         # html | wordpress_api
    first_hash: str | None = None


def collect_listing(conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config, source_id: int | None, url: str) -> Collected:
    """Read a portfolio listing: the page, then its pagination (bounded), then the site's public WordPress API if the page shows no entries.
    Coverage is 'complete' only when every page was read, the pagination ended, nothing hints at script-loaded entries and any stated total
    matches; otherwise 'partial' with the reason. Never raises: a failure is returned. Snapshots are stored for every page read."""
    maxp = cfg.pe_portfolio.fetch.max_pages_per_source
    res = Collected()
    queue, seen = [url], {normalize_url(url)}
    notes: list[str] = []
    hint = wp_api = None
    stated: int | None = None
    truncated = False
    while queue and len(res.pages) < maxp:
        u = queue.pop(0)
        try:
            got = fetcher.get(u)
        except FetchError as e:
            res.pages.append(PageResult(u, False, e.reason))
            if not res.pages[0].ok and u == url:
                res.error = e.reason
                break
            notes.append(f"page {u} could not be read ({e.reason})")
            continue
        with conn:
            sid = store_snapshot(conn, cfg, source_id, got)
        if "html" not in got.content_type and got.content_type:
            res.pages.append(PageResult(u, False, f"unsupported_content_type:{got.content_type}", snapshot_id=sid))
            continue
        lst = parse_listing(got.body, got.final_url)
        truncated |= got.truncated
        for e in lst.entries:
            e.snapshot_id = sid
        res.entries += lst.entries
        res.pages.append(PageResult(u, True, "ok", len(lst.entries), sid, got.content_hash))
        if res.first_hash is None:
            res.first_hash = got.content_hash
        hint = hint or lst.dynamic_hint
        wp_api = wp_api or lst.wp_api
        stated = lst.stated_total or stated
        for nxt in lst.next_urls:
            k = normalize_url(nxt)
            if k not in seen:
                seen.add(k)
                queue.append(nxt)
    if queue:
        notes.append(f"stopped after {maxp} pages; {len(queue)} more were linked")
    if res.error:
        res.coverage, res.coverage_note = "unknown", f"the page could not be read ({res.error})"
        return res
    if not res.entries and wp_api:
        api = _wordpress(conn, fetcher, cfg, source_id, wp_api, url)
        if api.entries:
            api.first_hash = res.first_hash
            api.pages = res.pages + api.pages
            return api
        notes += api.coverage_note.split("; ") if api.coverage_note else []
    # one company shown twice in the same state is one entry; shown in two states is kept (a conflict the ledger will state)
    uniq: dict[tuple[str, str], Entry] = {}
    for e in res.entries:
        uniq.setdefault((norm_name(e.name), e.state), e)
    res.entries = list(uniq.values())
    failed = [p for p in res.pages if not p.ok]
    if not res.entries:
        res.coverage = "unknown"
        res.coverage_note = "; ".join(["no entries found in the page's HTML"] + ([hint] if hint else []) + notes)
        return res
    reasons = list(notes)
    if failed:
        reasons.append(f"{len(failed)} page(s) failed")
    if hint:
        reasons.append(hint)
    if stated is not None and stated != len(res.entries):
        reasons.append(f"the page states {stated} but {len(res.entries)} were found")
    if truncated:
        reasons.append("a page was larger than the read limit")
    res.coverage = "partial" if reasons else "complete"
    res.coverage_note = "; ".join(dict.fromkeys(reasons)) if reasons else f"all {len(res.pages)} page(s) read; pagination ended"
    return res


def _wordpress(conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config, source_id: int | None, api_base: str, listing_url: str) -> Collected:
    """A WordPress site's public REST API: the content types that look like a portfolio, read page by page. Public, no login."""
    res = Collected(via="wordpress_api")
    maxp = cfg.pe_portfolio.fetch.max_pages_per_source
    base = api_base if api_base.endswith("/") else api_base + "/"
    try:
        types_resp = fetcher.get(urljoin(base, "wp/v2/types"))
        types = json.loads(types_resp.body)
    except (FetchError, ValueError) as e:
        res.coverage_note = f"the site advertises an API but its content types could not be read ({getattr(e, 'reason', 'bad json')})"
        return res
    chosen = [(k, v.get("rest_base", k)) for k, v in types.items()
              if isinstance(v, dict) and re.search(r"(portfolio|compan|investment|holding|brand)", f"{k} {v.get('rest_base', '')} {v.get('name', '')}", re.I)
              and k not in ("post", "page", "attachment", "wp_block", "nav_menu_item")]
    if not chosen:
        res.coverage_note = "the site's API has no portfolio-like content type"
        return res
    notes: list[str] = []
    for _slug, rest in chosen:
        total = None
        for page_no in range(1, maxp + 1):
            u = urljoin(base, f"wp/v2/{rest}?per_page=100&page={page_no}&_fields=id,slug,link,title,class_list")
            try:
                got = fetcher.get(u)
                items = json.loads(got.body)
            except FetchError as e:
                if e.status == 400 and page_no > 1:        # WordPress answers 400 past the last page
                    break
                res.pages.append(PageResult(u, False, e.reason))
                notes.append(f"{rest} page {page_no} failed ({e.reason})")
                break
            except ValueError:
                res.pages.append(PageResult(u, False, "bad_json"))
                notes.append(f"{rest} page {page_no} was not JSON")
                break
            with conn:
                sid = store_snapshot(conn, cfg, source_id, got)
            res.pages.append(PageResult(u, True, "ok", len(items) if isinstance(items, list) else 0, sid, got.content_hash))
            total = int(got.headers["x-wp-total"]) if got.headers.get("x-wp-total", "").isdigit() else total
            if not isinstance(items, list):
                break
            text, _t = visible_text(got, cfg.pe_portfolio.text_cap_bytes)
            for it in items:
                raw = (it.get("title") or {}).get("rendered", "")
                nm = _clean_name(html.unescape(re.sub(r"<[^>]+>", "", raw)))
                if not nm or raw not in text:               # the excerpt must be on the page that was read
                    continue
                label = " ".join(re.split(r"[-_\s]+", " ".join(it.get("class_list") or [])))
                state = "historical" if HIST.search(label) else "current" if CURR.search(label) else "unlabeled"
                res.entries.append(Entry(name=nm, href=it.get("link"), section=rest, state=state, excerpt=raw[:400], page_url=u, snapshot_id=sid))
            if len(items) < 100:
                break
        else:
            notes.append(f"stopped after {maxp} API pages")
        if total is not None and sum(1 for e in res.entries if e.section == rest) != total:
            notes.append(f"{rest}: the API reports {total} items; {sum(1 for e in res.entries if e.section == rest)} were read")
    if any(e.state == "historical" for e in res.entries):
        for e in res.entries:
            if e.state == "unlabeled":
                e.state, e.by_contrast = "current", True
    res.coverage = "unknown" if not res.entries else ("partial" if notes else "complete")
    res.coverage_note = "; ".join(notes) if notes else f"read through the site's public WordPress API ({len(res.pages)} request(s)); totals matched"
    return res
