"""Evidence-based priority for a company: deterministic, explainable, and separate from confidence. No model, no network.

Priority is how much a company deserves business-development attention NOW given what public evidence shows. It is not a probability of purchase or of
winning. It is built from five dimensions (weights in config): specific business need 30, timing and urgency 25, investment evidence 20, potential for
external help 15, evidence quality 10. Each dimension is read from anchor tables in config.yaml; the anchors say what a given score means.

Rules this module will not bend (each is a test in tests/test_opportunity_model.py):
  * A dimension nothing speaks to is UNKNOWN, which contributes 0 and is shown as unknown; it is never negative evidence and never rescaled away,
    so missing evidence cannot raise a score. Evidence coverage is shown beside the score.
  * One event is counted once, however many sources repeat it (and the same transaction seen as a signal and as a ledger deal is one event).
  * Nothing depends on company size, news volume, job count or portfolio size, and PE ownership adds nothing: a deal is a research trigger with a low
    need anchor.
  * Holding-duration inference, Low-confidence reports and unverified snippets are not evidence and are listed as excluded.
  * "Prospect now" needs a specific, current, supported hypothesis; it never means confirmed buying intent."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from bd.config import Config
from bd.pe_ownership import AUTHORITATIVE, is_stale
from bd.urls import hostname

DIMENSIONS = (("need", "Specific business need"), ("timing", "Timing and urgency"), ("investment", "Investment evidence"),
              ("external_help", "Potential for external help"), ("evidence_quality", "Evidence quality"))
CATEGORY_ORDER = ("Prospect now", "Validate first", "Nurture", "Monitor", "Insufficient evidence")
CONF_RANK = {"Unknown": 0, "Low": 1, "Medium": 2, "High": 3}
FAMILY = {"pe_platform_or_sponsor_exec": "platform_deal", "deal_completed": "platform_deal", "deal_announced": "platform_deal",
          "pe_addon": "add_on_deal", "add_on_completed": "add_on_deal", "add_on_announced": "add_on_deal"}
DEAL_KINDS = frozenset(FAMILY)
COMPANY_OWN = frozenset({"platform_migration", "vendor_deal", "rfp_match", "leadership_change", "leadership_transformation_ai_data", "open_exec_search",
                         "restructuring_layoffs", "cost_program", "ops_failure", "ai_commitment", "strategy_rebrand", "hiring_cluster", "hiring_leadership",
                         "hiring_interim", "hiring_single", "financial_pressure", "financial_distress", "activist", "ma_divestiture", "risk_factor"})
DEAL_LABELS = {"deal_completed": "completed acquisition", "deal_announced": "announced acquisition", "add_on_completed": "completed add-on acquisition",
               "add_on_announced": "announced add-on acquisition"}


def phrase(category: str) -> str:
    """A consulting category inside a sentence: lower-cased, except an acronym stays as it is ('AI implementation and adoption')."""
    return category if category[:3].isupper() or category[:2] == "AI" else category[:1].lower() + category[1:]


def kind_label(kind: str) -> str:
    from bd.labels import TYPE_LABELS
    return DEAL_LABELS.get(kind) or TYPE_LABELS.get(kind) or kind.replace("_", " ")


EXCLUDED_KINDS = {"pe_hold_2_4": "how long a sponsor has held a company is not evidence of a need, so it is not used"}


@dataclass(frozen=True)
class Item:
    id: str                              # signal:12 | deal:45 | addon:3
    kind: str
    event_date: date | None              # the date the source states; None = not stated
    source_type: str
    confidence: str                      # High | Medium | Low
    url: str
    summary: str
    role_or_person: str | None = None
    verified: bool = True                # code retrieved the page (signals are checked when stored; ledger evidence is page_verified)
    milestone: date | None = None        # a stated expected closing, never treated as a closing
    ownership_unverified: bool = False   # a deal whose ownership status is unclear, conflicting or stale
    categories: tuple[str, ...] = ()
    places: tuple[str, ...] = ()         # configured outside places the text names (set when the item is assessed); empty = none named

    @property
    def domain(self) -> str:
        return hostname(self.url)


@dataclass
class Cluster:
    """One event: every item about the same kind of event on (nearly) the same date. Counted once."""
    family: str
    items: list[Item]
    rep: Item
    quality: int
    domains: set[str]
    points: dict[str, int | None]
    categories: tuple[str, ...]

    @property
    def date(self) -> date | None:
        return self.rep.event_date


@dataclass
class DimensionScore:
    key: str
    label: str
    weight: int
    points: int | None                   # 0-100; None = unknown (no evidence speaks to it)
    contribution: float
    anchor: str                          # what this value means, in words
    evidence: list[str] = field(default_factory=list)


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str


@dataclass
class Hypothesis:
    category: str
    text: str
    evidence: list[str]
    initiative_owner: str                # who the evidence suggests would own the work, labelled as a hypothesis


@dataclass
class Assessment:
    subject: str
    score: int = 0
    dimensions: list[DimensionScore] = field(default_factory=list)
    known_dimensions: int = 0
    known_weight: int = 0
    confidence: str = "Unknown"              # in the consulting HYPOTHESIS (capped by how specific the need is)
    fact_confidence: str = "Unknown"         # that the facts themselves are accurate (the source's quality)
    confidence_reasons: list[str] = field(default_factory=list)
    category: str = "Insufficient evidence"
    rule: str = ""
    gates: list[Gate] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    facts: list[Item] = field(default_factory=list)
    counter: list[Item] = field(default_factory=list)
    excluded: list[tuple[Item, str]] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    next_step: str = ""
    discussion_question: str = ""
    scored_events: int = 0               # distinct scored events that bear on a need
    outside_events: int = 0              # of those, how many name a place outside the configured area (they may concern a parent or an affiliate)
    outside_places: list[str] = field(default_factory=list)
    eligible_events: int = 0             # scored events specific and current enough to support Prospect now
    eligible_outside: int = 0            # of those, how many name an outside place
    owner_function: str = ""
    named_people: list[str] = field(default_factory=list)
    events: int = 0
    need_before_counter: int | None = None   # the need anchor before a counter-signal reduced it

    def by_key(self, key: str) -> DimensionScore:
        return next(d for d in self.dimensions if d.key == key)


@dataclass
class Context:
    """What is known about the company beyond its evidence, used only for gates and unknowns (never for points)."""
    name: str
    ownership_notes: list[str] = field(default_factory=list)       # e.g. "Ownership by Alpha Capital is unclear"
    geography: str | None = None                                    # in_scope | out_of_scope | unknown
    identity_verified: bool = True
    last_scanned: str | None = None


def outside_places(text: str, cfg: Config) -> tuple[str, ...]:
    """The configured outside places a text names, in order of appearance. Whole words, case-sensitive; "New Mexico" is not Mexico, and "Ireland" inside
    "Northern Ireland" is not a second place."""
    spans: list[tuple[int, int, str]] = []
    for name in cfg.opportunity_model.outside_places:
        for m in re.finditer(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", text or ""):
            if name == "Mexico" and text[:m.start()].endswith("New "):
                continue
            spans.append((m.start(), m.end(), name))
    spans = [x for x in spans if not any(o is not x and o[0] <= x[0] and x[1] <= o[1] and (o[1] - o[0]) > (x[1] - x[0]) for o in spans)]
    seen: list[str] = []
    for _a, _b, name in sorted(spans):
        if name not in seen:
            seen.append(name)
    return tuple(seen)


# ---- turning items into events -----------------------------------------------------------------------------------------------------------------
def item_quality(i: Item, cfg: Config) -> int:
    m = cfg.opportunity_model
    q = m.quality.get(i.source_type, 0) if i.verified else 0
    return min(q, m.unknown_date_quality_cap) if i.event_date is None else q


def _points(table: dict[str, int], kinds: list[str]) -> int | None:
    vals = [table[k] for k in kinds if k in table]
    return max(vals) if vals else None


def cluster_events(items: list[Item], cfg: Config) -> list[Cluster]:
    m = cfg.opportunity_model
    groups: dict[str, list[Item]] = {}
    for i in items:
        groups.setdefault(FAMILY.get(i.kind, i.kind), []).append(i)
    out: list[Cluster] = []
    for fam, its in groups.items():
        dated = sorted((i for i in its if i.event_date), key=lambda i: (i.event_date, i.id))
        undated = [i for i in its if not i.event_date]
        buckets: list[list[Item]] = []
        for i in dated:
            if buckets and (i.event_date - buckets[-1][-1].event_date).days <= m.event_cluster_days:
                buckets[-1].append(i)
            else:
                buckets.append([i])
        if undated:
            buckets.append(undated)                         # dates unknown: they cannot be told apart, so they count as one
        for b in buckets:
            rep = max(b, key=lambda i: (item_quality(i, cfg), i.event_date or date.min, i.id))
            kinds = sorted({i.kind for i in b})
            domains = {i.domain for i in b if i.domain}
            q = item_quality(rep, cfg)
            if len(domains) >= 2 and q > 0:
                q = min(100, q + m.corroboration_bonus)
            cats = tuple(dict.fromkeys(c for i in b for c in i.categories))
            out.append(Cluster(fam, b, rep, q, domains, {"need": _points(m.need, kinds), "investment": _points(m.investment, kinds),
                                                         "external_help": _points(m.external_help, kinds)}, cats))
    return sorted(out, key=lambda c: (c.date or date.min, c.rep.id), reverse=True)


def timing_points(c: Cluster, today: date, cfg: Config) -> int | None:
    m = cfg.opportunity_model
    best: int | None = None
    ms = next((i.milestone for i in c.items if i.milestone and i.milestone >= today), None)
    if ms is not None:
        days = (ms - today).days
        for band in sorted(m.milestone, key=lambda b: b["within_days"]):
            if days <= band["within_days"]:
                best = band["points"]
                break
    if c.date is None:
        return best                                          # an unknown date says nothing about timing
    age = (today - c.date).days
    if age < 0:
        age = 0
    pts = 0
    for band in sorted(m.recency, key=lambda b: b["max_days"]):
        if age <= band["max_days"]:
            pts = band["points"]
            break
    return pts if best is None else max(best, pts)


# ---- the assessment ----------------------------------------------------------------------------------------------------------------------------
def _anchor_text(key: str, pts: int | None, cfg: Config) -> str:
    if pts is None:
        return "unknown: no evidence speaks to this"
    if key == "need":
        return ("an explicit, scoped program" if pts >= 90 else "a specific initiative or solicitation" if pts >= 70 else "a defined event that usually brings work"
                if pts >= 50 else "a generic indicator" if pts >= 25 else "context only")
    if key == "timing":
        return ("a stated milestone or an event in the last 30 days" if pts >= 85 else "an event in the last 90 days" if pts >= 65
                else "an event in the last 180 days" if pts >= 40 else "an older event" if pts > 0 else "older than a year")
    if key == "investment":
        return ("an open solicitation" if pts >= 90 else "a named vendor deal" if pts >= 70 else "a stated program or capital deployed" if pts >= 50
                else "effort implied, spend not stated")
    if key == "external_help":
        return ("a solicitation for outside services" if pts >= 90 else "interim or temporary capacity" if pts >= 70 else "a transition that usually needs added capacity"
                if pts >= 50 else "ambiguous" if pts >= 30 else "building in-house capacity")
    return "a company or regulator source with a stated date" if pts >= 85 else "a secondary source or an undated one" if pts >= 40 else "weak or unverified"


def assess(items: list[Item], today: date, cfg: Config, ctx: Context) -> Assessment:
    m = cfg.opportunity_model
    a = Assessment(subject=ctx.name)
    scored: list[Item] = []
    for i in items:
        if i.kind in EXCLUDED_KINDS:
            a.excluded.append((i, EXCLUDED_KINDS[i.kind]))
        elif i.kind in m.counter_kinds:
            if i.confidence != "Low" and i.verified:
                a.counter.append(i)
            else:
                a.excluded.append((i, "a counter-signal from an unverified or Low-confidence source"))
        elif i.confidence == "Low" or not i.verified:
            a.excluded.append((i, "Low confidence or not page-verified: stored, never scored"))
        else:
            scored.append(i)
    scored = [replace(i, places=outside_places(i.summary, cfg)) for i in scored]
    clusters = cluster_events(scored, cfg)
    a.events = len(clusters)

    def top(key: str) -> tuple[int | None, list[str]]:
        """The best distinct event for a dimension, plus a fraction of the next best one only when that one points at the same consulting category
        (corroboration of the same hypothesis). An unrelated event adds nothing to this dimension."""
        have = sorted((c for c in clusters if c.points[key] is not None), key=lambda c: (-c.points[key], c.rep.id))
        if not have:
            return None, []
        first = have[0]
        pts = float(first.points[key])
        ids = [first.rep.id]
        second = next((c for c in have[1:] if set(c.categories) & set(first.categories)), None)
        if second is not None:
            pts += m.second_event_credit * second.points[key]
            ids.append(second.rep.id)
        return min(100, round(pts)), ids

    need_pts, need_ev = top("need")
    best_need_cluster = max((c for c in clusters if c.points["need"] is not None), key=lambda c: (c.points["need"], c.quality), default=None)
    countering = [k for k in a.counter if best_need_cluster is not None and (best_need_cluster.date is None or k.event_date is None or k.event_date >= best_need_cluster.date)]
    a.need_before_counter = need_pts
    if countering and need_pts is not None:
        need_pts = round(need_pts * m.counter_need_factor)
    inv_pts, inv_ev = top("investment")
    ext_pts, ext_ev = top("external_help")
    timings = [(timing_points(c, today, cfg), c.rep.id) for c in clusters if c.points["need"] is not None]
    timings = [t for t in timings if t[0] is not None]
    tim_pts = max((t[0] for t in timings), default=None)
    tim_ev = [t[1] for t in sorted(timings, key=lambda t: -t[0])[:1]]
    qual = [(c.quality, c.rep.id) for c in clusters if c.points["need"] is not None]
    q_pts = max((q[0] for q in qual), default=None)
    q_ev = [q[1] for q in sorted(qual, key=lambda q: -q[0])[:1]]
    raw = {"need": (need_pts, need_ev), "timing": (tim_pts, tim_ev), "investment": (inv_pts, inv_ev), "external_help": (ext_pts, ext_ev),
           "evidence_quality": (q_pts, q_ev)}
    total = 0.0
    for key, label in DIMENSIONS:
        pts, ev = raw[key]
        w = getattr(m.weights, key)
        contrib = 0.0 if pts is None else w * pts / 100
        total += contrib
        a.dimensions.append(DimensionScore(key, label, w, pts, round(contrib, 1), _anchor_text(key, pts, cfg), ev))
    a.score = min(100, round(total))
    a.known_dimensions = sum(d.points is not None for d in a.dimensions)
    a.known_weight = sum(d.weight for d in a.dimensions if d.points is not None)

    _confidence(a, clusters, best_need_cluster, cfg)
    a.facts = [c.rep for c in clusters if c.points["need"] is not None]
    a.named_people = sorted({i.role_or_person for c in clusters for i in c.items if i.role_or_person})
    needed = [c for c in clusters if c.points["need"] is not None]
    outside = [c for c in needed if any(i.places for i in c.items)]
    a.outside_events, a.outside_places = len(outside), sorted({p for c in outside for i in c.items for p in i.places})
    a.scored_events = len(needed)
    pn = m.categories["prospect_now"]                    # the events that could make it Prospect now: specific enough AND current enough
    eligible = [c for c in needed if (c.points["need"] or 0) >= pn["min_need"] and (timing_points(c, today, cfg) or 0) >= pn["min_timing"]]
    a.eligible_events, a.eligible_outside = len(eligible), sum(any(i.places for i in c.items) for c in eligible)
    _hypotheses(a, clusters, cfg, ctx)
    _category(a, ctx, cfg, bool(countering), best_need_cluster)
    _unknowns(a, ctx)
    _next_step(a, ctx, best_need_cluster)
    return a


def _level(score: int, c) -> str:
    return "High" if score >= c.high else "Medium" if score >= c.medium else "Low"


def _confidence(a: Assessment, clusters: list[Cluster], best: Cluster | None, cfg: Config) -> None:
    """Two separate judgments. Fact confidence: how sure we can be that the facts are accurate (the best source's quality, corroboration, a stated date,
    verified ownership). Hypothesis confidence: how well supported the consulting hypothesis is, which can never exceed what the need anchor allows: an
    authoritative announcement can establish a transaction (High facts) without establishing consulting demand (Low hypothesis)."""
    c = cfg.opportunity_model.confidence
    if best is None:
        a.confidence = a.fact_confidence = "Unknown"
        a.confidence_reasons = ["no scored evidence of a need"]
        return
    score = best.quality
    why = [f"the best supporting event rests on a {best.rep.source_type.replace('_', ' ')} (quality {best.quality})"]
    same = [k for k in clusters if k is not best and k.points["need"] is not None and set(k.categories) & set(best.categories)]
    if same:
        add = c.corroboration_per_extra_event * min(2, len(same))
        score += add
        why.append(f"{len(same)} other distinct event(s) point to the same category (+{add})")
    if best.date is None:
        score -= c.unknown_date_penalty
        why.append(f"the source states no date (-{c.unknown_date_penalty})")
    if any(i.ownership_unverified for i in best.items):
        score -= c.unverified_ownership_penalty
        why.append(f"the ownership behind the deal is unclear, conflicting or stale (-{c.unverified_ownership_penalty})")
    if len(best.domains) >= 2:
        why.append("the event is reported by two independent sources")
    score = max(0, min(100, score))
    a.fact_confidence = _level(score, c)
    need = best.points["need"] or 0
    cap = "High" if need >= 75 else "Medium" if need >= 50 else "Low"
    a.confidence = min((a.fact_confidence, cap), key=lambda lv: CONF_RANK[lv])
    if CONF_RANK[cap] < CONF_RANK[a.fact_confidence]:
        why.append(f"the facts are {a.fact_confidence.lower()}-confidence, but the need anchor ({need}) supports only {cap.lower()} confidence in a consulting need")
    elif need >= 75:
        why.append("the need is specific (75 or more)")
    a.confidence_reasons = why + [f"fact confidence score {score} (High from {c.high}, Medium from {c.medium})"]


def _hypotheses(a: Assessment, clusters: list[Cluster], cfg: Config, ctx: Context) -> None:
    by_cat: dict[str, list[Cluster]] = {}
    for c in clusters:
        if c.points["need"] is None:
            continue
        for cat in c.categories:
            by_cat.setdefault(cat, []).append(c)
    ranked = sorted(by_cat.items(), key=lambda kv: (-max(c.points["need"] for c in kv[1]), kv[0]))
    for cat, cs in ranked[:3]:
        kinds = sorted({k for c in cs for k in (i.kind for i in c.items)})
        if all(k in DEAL_KINDS for k in kinds):
            owner = "Ownership of the initiative is unknown: a transaction can be run by the sponsor or by the company"
        elif any(i.kind == "pe_platform_or_sponsor_exec" and i.role_or_person for c in cs for i in c.items):
            owner = "Work potentially coordinated by the sponsor (a sponsor-linked executive is named)"
        elif all(any(i.places for i in c.items) for c in cs):
            where = ", ".join(sorted({p for c in cs for i in c.items for p in i.places}))
            owner = f"Ownership of the initiative is unknown: the evidence names {where}, so it may belong to a parent or an affiliate rather than this company"
        elif all(k in COMPANY_OWN for k in kinds):
            owner = "Work likely owned by the company itself (the evidence concerns its own operations)"
        else:
            owner = "Ownership of the initiative is unknown"
        what = ", ".join(kind_label(k).lower() for k in kinds)
        text = (f"Hypothesis: the evidence ({what}) could point to work in {phrase(cat)}. It establishes that the event happened, not that "
                f"{ctx.name} needs outside help, has a budget, or intends to buy.")
        a.hypotheses.append(Hypothesis(cat, text, [c.rep.id for c in cs], owner))
    if a.hypotheses:
        a.owner_function = cfg.opportunity_model.owner_function.get(a.hypotheses[0].category, "")


def _category(a: Assessment, ctx: Context, cfg: Config, countered: bool, best: Cluster | None) -> None:
    rules = cfg.opportunity_model.categories
    need, timing = a.by_key("need").points or 0, a.by_key("timing").points
    p = rules["prospect_now"]
    gates = [Gate("score", a.score >= p["min_score"], f"score {a.score}, needs {p['min_score']}"),
             Gate("specific need", need >= p["min_need"], f"need anchor {need}, needs {p['min_need']}"),
             Gate("current", (timing or 0) >= p["min_timing"], f"timing anchor {timing if timing is not None else 'unknown'}, needs {p['min_timing']}"),
             Gate("supported", CONF_RANK[a.confidence] >= CONF_RANK[str(p["min_confidence"])], f"confidence {a.confidence}, needs {p['min_confidence']}"),
             Gate("source quality", (a.by_key("evidence_quality").points or 0) >= p["min_source_quality"],
                  f"best source quality {a.by_key('evidence_quality').points if a.by_key('evidence_quality').points is not None else 'unknown'}, needs {p['min_source_quality']} "
                  "(a company release, filing or the firm's own page, or a second independent source)"),
             Gate("coverage", a.known_dimensions >= p["min_known_dimensions"], f"{a.known_dimensions} of 5 dimensions have evidence, needs {p['min_known_dimensions']}"),
             Gate("hypothesis", bool(a.hypotheses), "a consulting category is suggested by the evidence" if a.hypotheses else "the evidence suggests no consulting category"),
             Gate("no counterevidence", not countered, "none as new as the supporting event" if not countered else "a canceled program or spending restriction is as new or newer"),
             Gate("its own area", not (a.eligible_events and a.eligible_outside == a.eligible_events),
                  "a specific, current event does not name a place outside the configured area" if not (a.eligible_events and a.eligible_outside == a.eligible_events)
                  else f"every specific, current event names {', '.join(a.outside_places)}; nothing shows it involves this company's own operations (a parent or an affiliate may be the actor)"),
             Gate("ownership verified", not (best and any(i.ownership_unverified for i in best.items)),
                  "not a deal, or the deal's ownership is verified" if not (best and any(i.ownership_unverified for i in best.items))
                  else "the ownership behind the deal is unclear, conflicting or stale")]
    if ctx.geography == "out_of_scope":
        gates.append(Gate("in configured geography", False, "the company is outside the configured geography"))
    a.gates = gates
    v, n, mo = rules["validate_first"], rules["nurture"], rules["monitor"]
    if a.events == 0:
        a.category, a.rule = "Insufficient evidence", "no scored evidence of a need"
    elif all(g.ok for g in gates):
        a.category, a.rule = "Prospect now", "every gate for Prospect now holds"
    elif a.score >= v["min_score"] and (a.need_before_counter or 0) >= v["min_need"] and (timing or 0) >= v["min_timing"]:
        failed = ", ".join(g.name for g in gates if not g.ok)
        a.category, a.rule = "Validate first", f"a current, specific hypothesis exists but not every gate for Prospect now holds ({failed})"
    elif need >= n["min_need"]:
        a.category, a.rule = "Nurture", "the evidence of a need is specific but not current"
    elif a.score >= mo["min_score"]:
        a.category, a.rule = "Monitor", "there is some evidence, but it is generic or weak"
    else:
        a.category, a.rule = "Insufficient evidence", "the evidence is too thin to support a hypothesis"


def _unknowns(a: Assessment, ctx: Context) -> None:
    u = a.unknowns
    unknown_dims = [d.label.lower() for d in a.dimensions if d.points is None]
    if unknown_dims:
        u.append("No evidence speaks to: " + ", ".join(unknown_dims) + ". Each adds nothing to the score and none is counted as negative")
    if a.by_key("investment").points is None:
        u.append("Funding or procurement evidence: unknown")
    if not a.named_people:
        u.append("No executive is named in the evidence; the owner would be inferred from the function, not verified")
    if any(i.event_date is None for i in a.facts):
        u.append("At least one fact has no date stated by its source")
    if a.outside_events:
        u.append(f"{a.outside_events} of {a.scored_events} scored event{'s' if a.scored_events != 1 else ''} name{'' if a.outside_events != 1 else 's'} {', '.join(a.outside_places)}: "
                 "nothing in the evidence shows the event involves this company's own operations rather than a parent, an affiliate or a foreign operation")
    u.extend(ctx.ownership_notes)
    if ctx.geography == "unknown":
        u.append("Geography against the configured scope is unverified")
    if not ctx.identity_verified:
        u.append("Company identity (name, domain) is not verified")
    if ctx.last_scanned is None:
        u.append("The company has not been scanned for signals")


def _next_step(a: Assessment, ctx: Context, best: Cluster | None) -> None:
    steps: list[str] = []
    if a.events == 0:
        steps.append("Run a signal scan, then look for a dated, sourced event this company has announced; there is nothing to act on yet")
    else:
        failed = {g.name for g in a.gates if not g.ok}
        if "supported" in failed or "source quality" in failed:
            steps.append("Find a second, independent source for the main event, preferably the company's own release or a filing")
        if "its own area" in failed:
            steps.append(f"Confirm that the events in {', '.join(a.outside_places)} involve this company's own operations, not a parent or an affiliate, before treating them as its need")
        if "ownership verified" in failed:
            steps.append("Verify the ownership relationship on the sponsor's own page or a completion notice before relying on the deal")
        if "no counterevidence" in failed:
            steps.append("Read the counter-signal first: confirm whether the program was canceled or spending was restricted")
        if a.by_key("investment").points is None:
            steps.append("Look for a budget, a vendor selection or a procurement notice; none is in the evidence")
        if a.by_key("timing").points is None or "current" in failed:
            steps.append("Check what has happened since the newest event and whether it is still current")
        if not a.named_people and a.owner_function:
            steps.append(f"Identify who is responsible for this work ({a.owner_function}) from the company's own leadership page; the function is inferred, not a verified buyer")
        if not steps:
            steps.append("Read the cited sources in full, then decide whether a conversation about the hypothesis is warranted")
    a.next_step = ". ".join(s[0].upper() + s[1:] for s in steps[:3]) + "."
    support = [f for f in a.facts if a.hypotheses and f.id in a.hypotheses[0].evidence]       # the events the first hypothesis rests on, not just the newest fact
    support.sort(key=lambda f: (f.event_date or date.min, f.id), reverse=True)
    if a.hypotheses and support and "its own area" not in {g.name for g in a.gates if not g.ok}:
        f = support[0]
        when = f"on {f.event_date:%B %-d, %Y}" if f.event_date else "(date not stated)"
        a.discussion_question = (f"Public sources report {when}: \"{f.summary.rstrip('.')}\". How is {ctx.name} approaching "
                                 f"{phrase(a.hypotheses[0].category)} in light of that? (A question to explore; it assumes no problem, budget or intent.)")


def rules_text(cfg: Config) -> list[str]:
    """The category rules in words, for display. Built from the same thresholds the code uses."""
    r = cfg.opportunity_model.categories
    p, v, n, mo = r["prospect_now"], r["validate_first"], r["nurture"], r["monitor"]
    return [
        f"Prospect now: score at least {p['min_score']}, a specific need (need anchor at least {p['min_need']}), a current event (timing anchor at least {p['min_timing']}), "
        f"confidence {p['min_confidence']} or better, a source of quality at least {p['min_source_quality']} (a company release, filing or the firm's own page, or a second independent source), evidence in at least {p['min_known_dimensions']} of the 5 dimensions, a suggested consulting category, no counterevidence "
        "as new as the supporting event, verified ownership behind any deal, and a company inside the configured geography, and at least one of those specific, current events that does not name a place outside it (an event in India or England may be a parent's or an affiliate's, not this company's). It means a specific, current, supported hypothesis "
        "and a next step. It does not mean confirmed buying intent.",
        f"Validate first: a current, specific hypothesis exists (score at least {v['min_score']}, need at least {v['min_need']}, timing at least {v['min_timing']}) but at least one gate for Prospect now fails.",
        f"Nurture: the need is specific (need at least {n['min_need']}) but the evidence is not current.",
        f"Monitor: some evidence, generic or weak (score at least {mo['min_score']}).",
        "Insufficient evidence: no scored evidence of a need, or too little to support a hypothesis.",
    ]


# ---- loading evidence from the database ------------------------------------------------------------------------------------------------------------
def _categories_for(kind: str, summary: str, cfg: Config) -> tuple[str, ...]:
    m = cfg.opportunity_model
    if kind.startswith("hiring_"):
        s = summary.lower()
        for theme, cat in m.hiring_theme_categories.items():
            if theme.lower() in s:
                return (cat,)
        return ()
    return tuple(m.consulting_categories.get(kind, []))


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def load_items(conn: sqlite3.Connection, cfg: Config, account_id: int | None, company_id: int | None, today: date) -> tuple[list[Item], list[str]]:
    """Structured evidence for a company: its stored signals, plus the verified deals and add-ons in the ownership ledger. Returns (items, ownership notes)."""
    items: list[Item] = []
    notes: list[str] = []
    if account_id is not None:
        for r in conn.execute("SELECT * FROM signals WHERE account_id = ? ORDER BY event_date DESC, id", (account_id,)):
            items.append(Item(f"signal:{r['id']}", r["type"], _date(r["event_date"]), r["source_type"], r["confidence"], r["source_url"], r["summary"],
                              r["role_or_person"], True, None, False, _categories_for(r["type"], r["summary"], cfg)))
    if company_id is None and account_id is not None:
        row = conn.execute("SELECT id FROM pe_companies WHERE account_id = ?", (account_id,)).fetchone()
        company_id = row["id"] if row else None
    if company_id is not None:
        for o in conn.execute("SELECT o.*, f.name AS firm FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id WHERE o.company_id = ?", (company_id,)):
            unverified = o["status"] == "unclear" or bool(o["conflict_note"] and o["status"] != "current") or is_stale(o, today, cfg)
            if o["status"] == "unclear":
                notes.append(f"Ownership by {o['firm']} is unclear: {o['conflict_note'] or 'no source states it'}")
            elif is_stale(o, today, cfg) and o["status"] in ("current", "pending"):
                notes.append(f"Ownership by {o['firm']} was last verified {o['last_verified'] or 'never'}, which is past the {cfg.pe_portfolio.ownership_fresh_days}-day window")
            for e in conn.execute("SELECT * FROM pe_evidence WHERE subject_type = 'ownership' AND subject_id = ? AND verification = 'page_verified' "
                                  "AND claim IN ('announced', 'completed') ORDER BY id", (o["id"],)):
                kind = "deal_completed" if e["claim"] == "completed" else "deal_announced"
                items.append(Item(f"deal:{e['id']}", kind, _date(e["event_date"]), e["source_type"], "High" if e["source_type"] in AUTHORITATIVE else "Medium",
                                  e["url"], (e["excerpt"] or "")[:200], None, True,
                                  _date(o["expected_close"]) if kind == "deal_announced" and o["status"] == "pending" else None, unverified,
                                  _categories_for(kind, "", cfg)))
        for lk in conn.execute("SELECT * FROM pe_company_links WHERE parent_company_id = ? AND kind = 'add_on'", (company_id,)):
            kind = "add_on_completed" if lk["status"] == "completed" else "add_on_announced"
            for e in conn.execute("SELECT * FROM pe_evidence WHERE subject_type = 'link' AND subject_id = ? AND verification = 'page_verified' ORDER BY id", (lk["id"],)):
                items.append(Item(f"addon:{e['id']}", kind, _date(e["event_date"]), e["source_type"], "High" if e["source_type"] in AUTHORITATIVE else "Medium",
                                  e["url"], (e["excerpt"] or "")[:200], None, True, _date(lk["expected_close"]) if kind == "add_on_announced" else None, False,
                                  _categories_for(kind, "", cfg)))
    return items, notes


def assess_account(conn: sqlite3.Connection, cfg: Config, account_id: int, today: date) -> Assessment:
    from bd.pe_ownership import company_geography
    acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    items, notes = load_items(conn, cfg, account_id, None, today)
    co = conn.execute("SELECT id FROM pe_companies WHERE account_id = ?", (account_id,)).fetchone()
    geo = company_geography(conn, co["id"], cfg).status if co else {"yes": "in_scope", "no": "out_of_scope"}.get(acct["chicagoland"], "unknown")
    return assess(items, today, cfg, Context(acct["name"], notes, geo, bool(acct["domain"]), acct["last_scanned"]))


def assess_company(conn: sqlite3.Connection, cfg: Config, company_id: int, today: date) -> Assessment:
    from bd.pe_ownership import company_geography
    co = conn.execute("SELECT * FROM pe_companies WHERE id = ?", (company_id,)).fetchone()
    if co["account_id"]:
        return assess_account(conn, cfg, co["account_id"], today)
    items, notes = load_items(conn, cfg, None, company_id, today)
    return assess(items, today, cfg, Context(co["name"], notes, company_geography(conn, company_id, cfg).status, bool(co["domain"]), None))


# ---- portfolios ----------------------------------------------------------------------------------------------------------------------------------
@dataclass
class Holding:
    company: sqlite3.Row
    ownership: sqlite3.Row
    assessment: Assessment
    geography: str
    geography_reason: str


def rank_holdings(conn: sqlite3.Connection, cfg: Config, firm_id: int, today: date, statuses: tuple[str, ...] = ("current", "pending", "unclear"),
                  geography: str = "") -> list[Holding]:
    """A firm's holdings, ranked by what the evidence supports: category first, then score, then confidence. Filters: relationship status and, optionally,
    geography (in_scope / out_of_scope / unknown). Nobody is forced to have an opportunity."""
    from bd.pe_ownership import company_geography
    out: list[Holding] = []
    for o in conn.execute("SELECT * FROM pe_ownership WHERE firm_id = ? ORDER BY id", (firm_id,)).fetchall():
        if o["status"] not in statuses:
            continue
        c = conn.execute("SELECT * FROM pe_companies WHERE id = ?", (o["company_id"],)).fetchone()
        g = company_geography(conn, c["id"], cfg)
        if geography and g.status != geography:
            continue
        out.append(Holding(c, o, assess_company(conn, cfg, c["id"], today), g.status, g.reason))
    out.sort(key=lambda h: (CATEGORY_ORDER.index(h.assessment.category), -h.assessment.score, -CONF_RANK[h.assessment.confidence], h.company["name"].lower()))
    return out


@dataclass
class FirmSummary:
    holdings: int
    by_status: dict[str, int]
    assessed: int
    by_category: dict[str, int]
    coverage_note: str
    sources: list[sqlite3.Row] = field(default_factory=list)


def firm_summary(conn: sqlite3.Connection, cfg: Config, firm_id: int, today: date, ranked: list[Holding] | None = None) -> FirmSummary:
    """Counts and disclosure for a sponsor: how many distinct supported opportunities, never a sum of scores, and how complete the portfolio reading is.
    `ranked` is the firm's holdings when the caller has already ranked them (every holding is assessed, so a big portfolio is not assessed twice)."""
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM pe_ownership WHERE firm_id = ? GROUP BY status", (firm_id,)).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    held = rank_holdings(conn, cfg, firm_id, today) if ranked is None else [h for h in ranked if h.ownership["status"] != "historical"]
    by_cat: dict[str, int] = {}
    for h in held:
        by_cat[h.assessment.category] = by_cat.get(h.assessment.category, 0) + 1
    sources = conn.execute("SELECT * FROM pe_sources WHERE firm_id = ? ORDER BY id", (firm_id,)).fetchall()
    if not sources:
        dom = conn.execute("SELECT domain FROM pe_firms WHERE id = ?", (firm_id,)).fetchone()["domain"]
        note = ("No portfolio page is registered for this firm, so nothing has been collected. " + (
            f"Reading the PE portfolio pages will look for a Portfolio link on {dom}." if dom else
            "Add the firm's website domain or its portfolio page URL under Firm details."))
    elif all(s["coverage"] == "complete" and s["status"] == "ok" for s in sources):
        note = "The portfolio page was read to its end on its last reading. That is the firm's page as of that day, not independent proof of every holding."
    else:
        bits = []
        for s in sources:
            if s["status"] in ("failed", "blocked"):
                bits.append(f"the latest attempt to read {s['url']} failed ({s['last_error']}); what is shown is from the last good reading"
                            + (f" on {s['last_success'][:10]} (coverage {s['coverage']})" if s["last_success"] else ", and there has been none"))
            elif s["status"] == "never":
                bits.append(f"{s['url']} has not been read yet")
            elif s["coverage"] != "complete":
                bits.append(f"{s['url']} was read with {s['coverage']} coverage" + (f" ({s['coverage_note']})" if s["coverage_note"] else ""))
        note = "Partial coverage: " + "; ".join(bits) + ". The portfolio may be larger than what is listed here."
    return FirmSummary(sum(by_status.values()), by_status, len(held), by_cat, note, sources)
