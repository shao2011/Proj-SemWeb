"""Local asserted-RDF extraction, deterministic scoring and durable audit state."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import unicodedata

from rdflib import Graph, Namespace
from rdflib.namespace import RDF, RDFS

BOOKS = Namespace("http://example.org/books#")
TARGETS = ("BookEdition", "Book", "Person", "BookSeries", "Language", "Publisher", "Place")
WD_TARGETS = TARGETS
ROLES = {BOOKS.isTranslatedBy: "translator", BOOKS.isIllustratedBy: "illustrator",
         BOOKS.isEditedBy: "editor", BOOKS.isNarratedBy: "narrator",
         BOOKS.hasEditionContributor: "contributor"}


def norm(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"(?<=\w)\.(?=\w)", " ", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def similarity(a: object, b: object) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    return round(100 * max(SequenceMatcher(None, a, b).ratio(),
                           SequenceMatcher(None, " ".join(sorted(a.split())),
                                           " ".join(sorted(b.split()))).ratio()), 2)


def best_similarity(value: str, others: list[str]) -> float:
    return max((similarity(value, other) for other in others), default=0.0)


def year(value: object) -> str:
    match = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(value or ""))
    return match.group(1) if match else ""


def weighted(features: dict[str, float], weights: dict[str, float]) -> float:
    return round(sum(weights[k] * features.get(k, 0) / 100 for k in weights), 2)


@dataclass
class Local:
    uri: str
    target: str
    label: str
    data: dict = field(default_factory=dict)


@dataclass
class Candidate:
    uri: str
    candidate_id: str
    features: dict[str, float]
    score: float
    conflicts: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def audit(self, rank: int) -> dict:
        return {"candidate_uri": self.uri, "candidate_id": self.candidate_id,
                "candidate_rank": rank, "feature_scores": self.features,
                "final_score": self.score, "hard_conflicts": self.conflicts,
                "evidence": self.evidence}


def decide(local: Local, candidates: list[Candidate], config: dict, strategy: str,
           required: bool = True, forced_conflict: str = "") -> dict:
    ordered = sorted(candidates, key=lambda c: (-c.score, c.candidate_id))
    plausible = [c for c in ordered if not c.conflicts]
    best = plausible[0] if plausible else None
    second = plausible[1] if len(plausible) > 1 else None
    margin = round(best.score - second.score, 2) if best and second else None
    threshold_key = local.target
    reason = "NO_CANDIDATE"
    if forced_conflict:
        reason = forced_conflict
    elif not best and ordered:
        all_conflicts = {x for c in ordered for x in c.conflicts}
        reason = next((x for x in ("ABSTRACTION_LEVEL_MISMATCH", "CONTEXT_CONTRADICTION",
                                   "WRONG_ENTITY_TYPE", "TITLE_CONTRADICTION", "INSUFFICIENT_TITLE_SANITY",
                                   "AUTHOR_CONTRADICTION", "ROLE_CONTRADICTION", "YEAR_CONTRADICTION",
                                   "PAGES_CONTRADICTION", "PUBLISHER_RELATED_ENTITY_CONFLICT",
                                   "AMBIGUOUS_CANDIDATES", "INSUFFICIENT_LABEL_MATCH", "INSUFFICIENT_CONTEXT")
                       if x in all_conflicts), "HARD_CONFLICT")
    elif best:
        if not required:
            reason = "INSUFFICIENT_CONTEXT"
        elif best.score < config["thresholds"][threshold_key]:
            reason = "BELOW_ACCEPT_THRESHOLD"
        elif second and margin < config["margins"][threshold_key]:
            reason = "INSUFFICIENT_MARGIN"
        else:
            reason = {"isbn": "EXACT_ISBN", "edition_anchor": "EDITION_ANCHORED_WORK",
                      "direct_p50": "DIRECT_P50_CONTEXT", "direct_p179": "DIRECT_P179_CONTEXT",
                      "direct_p123": "DIRECT_P123_CONTEXT",
                      "Language": "UNIQUE_EXACT_LANGUAGE_MATCH",
                      "Place": "UNIQUE_HIGH_CONFIDENCE_PLACE_MATCH"}.get(
                strategy, "HIGH_CONFIDENCE_CONTEXT_MATCH")
    accepted = reason in {"EXACT_ISBN", "EDITION_ANCHORED_WORK", "DIRECT_P50_CONTEXT",
                          "DIRECT_P179_CONTEXT", "DIRECT_P123_CONTEXT",
                          "UNIQUE_EXACT_LANGUAGE_MATCH", "UNIQUE_HIGH_CONFIDENCE_PLACE_MATCH",
                          "HIGH_CONFIDENCE_CONTEXT_MATCH"}
    return {"local_uri": local.uri, "target_type": local.target,
            "external_source": "Wikidata",
            "strategy": strategy, "decision": "ACCEPTED" if accepted else "NO_MATCH",
            "reason_code": reason, "candidate_uri": best.uri if accepted else None,
            "candidate_id": best.candidate_id if accepted else None,
            "best_score": best.score if best else None,
            "runner_up_score": second.score if second else None, "margin": margin,
            "hard_conflicts": sorted({x for c in ordered for x in c.conflicts} | ({forced_conflict} if forced_conflict else set())),
            "candidates": [c.audit(i) for i, c in enumerate(ordered, 1)]}


class LocalGraph:
    def __init__(self, path: Path):
        self.graph = Graph().parse(path, format="turtle")
        self.entities = {target: {} for target in TARGETS}
        self._extract()

    def _extract(self):
        g = self.graph
        label = lambda uri: str(g.value(uri, RDFS.label) or "")
        objects = lambda uri, pred: sorted(str(x) for x in g.objects(uri, pred))
        first = lambda uri, pred: next(iter(objects(uri, pred)), "")
        for target in TARGETS:
            for uri in sorted(g.subjects(RDF.type, BOOKS[target]), key=str):
                self.entities[target][str(uri)] = Local(str(uri), target, label(uri))
        for entity in self.entities["Book"].values():
            from rdflib import URIRef
            u = URIRef(entity.uri)
            entity.data = {"title": first(u, BOOKS.title) or entity.label,
                           "authors": [label(x) for x in g.objects(u, BOOKS.isWrittenBy)],
                           "author_uris": objects(u, BOOKS.isWrittenBy),
                           "editions": objects(u, BOOKS.hasEdition),
                           "series": objects(u, BOOKS.isPartOfSeries),
                           "places": objects(u, BOOKS.setIn),
                           "year": year(first(u, BOOKS.firstPublishDate))}
        parent = {e: b for b, book in self.entities["Book"].items() for e in book.data["editions"]}
        for entity in self.entities["BookEdition"].values():
            from rdflib import URIRef
            u = URIRef(entity.uri)
            entity.data = {"isbn": first(u, BOOKS.isbn), "edition": first(u, BOOKS.editionName),
                           "year": year(first(u, BOOKS.publishDate)),
                           "pages": first(u, BOOKS.pageCount),
                           "publisher": [label(x) for x in g.objects(u, BOOKS.isPublishedBy)],
                           "language": [label(x) for x in g.objects(u, BOOKS.isInLanguage)],
                           "format": [label(x) for x in g.objects(u, BOOKS.hasFormat)],
                           "book": parent.get(entity.uri, "")}
        for entity in self.entities["Person"].values():
            from rdflib import URIRef
            u = URIRef(entity.uri)
            books = objects(u, BOOKS.writes)  # asserted graph usually does not contain inverse
            books += sorted(str(x) for x in g.subjects(BOOKS.isWrittenBy, u))
            roles = set()
            if books: roles.add("author")
            for pred, role in ROLES.items():
                if any(g.subjects(pred, u)):
                    roles.add(role)
            entity.data = {"books": sorted(set(books)), "roles": sorted(roles)}
        for target, pred, reverse in (("BookSeries", BOOKS.isPartOfSeries, "books"),
                                      ("Publisher", BOOKS.isPublishedBy, "editions"),
                                      ("Place", BOOKS.setIn, "books")):
            for entity in self.entities[target].values():
                from rdflib import URIRef
                entity.data[reverse] = sorted(str(x) for x in g.subjects(pred, URIRef(entity.uri)))

    def sample(self, config: dict) -> dict[str, list[Local]]:
        rng = random.Random(config["seed"])
        result = {}
        for target in TARGETS:
            records = list(self.entities[target].values())
            wanted = config["sample_counts"][target]
            if target == "BookEdition":
                groups = [[x for x in records if x.data["isbn"]],
                          [x for x in records if not x.data["isbn"]]]
            elif target in {"Person", "Place", "BookSeries", "Publisher"}:
                groups = [[x for x in records if len(norm(x.label).split()) <= 1],
                          [x for x in records if len(norm(x.label).split()) > 1]]
            else:
                groups = [records]
            chosen = []
            for group in groups:
                chosen.extend(rng.sample(group, min(len(group), wanted // len(groups))))
            leftovers = sorted(set(x.uri for x in records) - set(x.uri for x in chosen))
            chosen.extend(self.entities[target][u] for u in rng.sample(leftovers, min(len(leftovers), wanted - len(chosen))))
            result[target] = sorted(chosen, key=lambda x: x.uri)
        # Fixed live regression anchors from the independent ISBN experiment.
        by_isbn = {x.data["isbn"]: x for x in self.entities["BookEdition"].values() if x.data["isbn"]}
        anchors = [by_isbn[x] for x in config.get("sample_anchor_isbns", []) if x in by_isbn]
        result["BookEdition"] = sorted({x.uri: x for x in result["BookEdition"] + anchors}.values(), key=lambda x:x.uri)
        # Keep dependency targets in the sample so Edition→Work and Work→Person
        # anchors can actually be exercised in a bounded dry run.
        parents = [self.entities["Book"].get(x.data.get("book")) for x in result["BookEdition"]]
        result["Book"] = sorted({x.uri: x for x in result["Book"] + [x for x in parents if x]}.values(), key=lambda x:x.uri)
        authors = [self.entities["Person"].get(u) for book in result["Book"] for u in book.data["author_uris"]]
        result["Person"] = sorted({x.uri:x for x in result["Person"] + [x for x in authors if x]}.values(), key=lambda x:x.uri)
        return result


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS responses (request_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.db.execute("CREATE TABLE IF NOT EXISTS decisions (scope TEXT, local_uri TEXT, config_hash TEXT, result TEXT NOT NULL, PRIMARY KEY(scope,local_uri))")
        self.db.execute("CREATE TABLE IF NOT EXISTS errors (scope TEXT, local_uri TEXT, code TEXT, detail TEXT, occurred_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(scope,local_uri))")
        self.db.commit()

    def response(self, key):
        row = self.db.execute("SELECT payload FROM responses WHERE request_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_response(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO responses(request_key,payload) VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def decision(self, scope, uri, config_hash):
        row = self.db.execute("SELECT result FROM decisions WHERE scope=? AND local_uri=? AND config_hash=?", (scope,uri,config_hash)).fetchone()
        return json.loads(row[0]) if row else None

    def save_decision(self, scope, uri, config_hash, value):
        self.db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?)", (scope,uri,config_hash,json.dumps(value, ensure_ascii=False)))
        self.db.execute("DELETE FROM errors WHERE scope=? AND local_uri=?", (scope,uri))
        self.db.commit()

    def save_error(self, scope, uri, code, detail):
        self.db.execute("INSERT OR REPLACE INTO errors(scope,local_uri,code,detail) VALUES (?,?,?,?)", (scope,uri,code,str(detail)[:2000]))
        self.db.commit()

    def results(self, scope):
        return [json.loads(row[0]) for row in self.db.execute("SELECT result FROM decisions WHERE scope=? ORDER BY local_uri", (scope,))]

    def errors(self, scope):
        return [dict(zip(("local_uri","code","detail","occurred_at"), row)) for row in self.db.execute(
            "SELECT local_uri,code,detail,occurred_at FROM errors WHERE scope=? ORDER BY local_uri", (scope,))]


def config_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def diagnostics(results: list[dict], errors: list[dict], sample_ids: dict) -> dict:
    counts = defaultdict(Counter)
    scores = defaultdict(list)
    margins = defaultdict(list)
    strategy_scores = defaultdict(list)
    strategy_margins = defaultdict(list)
    anchors = Counter()
    anchor_attempts = Counter()
    conflicts = Counter()
    type_rejections = Counter()
    no_match_pre_network = Counter()
    wikidata_searches_attempted = Counter()
    for row in results:
        target = row["target_type"]
        counts[target][row["decision"]] += 1
        counts[target][row["reason_code"]] += 1
        if row["external_source"] == "Wikidata":
            if row.get("network_attempted") is False:
                no_match_pre_network[target] += 1
            elif row.get("network_attempted") is True:
                wikidata_searches_attempted[target] += 1
        if row["best_score"] is not None: scores[target].append(row["best_score"])
        if row["margin"] is not None: margins[target].append(row["margin"])
        bucket = target + "/" + row["strategy"]
        if row["best_score"] is not None: strategy_scores[bucket].append(row["best_score"])
        if row["margin"] is not None: strategy_margins[bucket].append(row["margin"])
        if row["strategy"] in {"isbn", "edition_anchor"}:
            anchor_attempts[row["strategy"]] += 1
        for conflict in row["hard_conflicts"]:
            conflicts[conflict] += 1
            if conflict == "WRONG_ENTITY_TYPE": type_rejections[target] += 1
        if row["reason_code"] in {"EXACT_ISBN", "EDITION_ANCHORED_WORK"}:
            anchors[row["reason_code"]] += 1
    def summary(values):
        return {"count": len(values), "min": min(values), "median": sorted(values)[len(values)//2], "max": max(values)} if values else {"count": 0}
    return {"counts": {k: dict(v) for k,v in counts.items()},
            "coverage": {k: round(v["ACCEPTED"] / (v["ACCEPTED"] + v["NO_MATCH"]), 4)
                         for k,v in counts.items() if v["ACCEPTED"] + v["NO_MATCH"]},
            "score_distributions": {k:summary(v) for k,v in scores.items()},
            "margin_distributions": {k:summary(v) for k,v in margins.items()},
            "score_by_strategy": {k:summary(v) for k,v in strategy_scores.items()},
            "margin_by_strategy": {k:summary(v) for k,v in strategy_margins.items()},
            "trusted_anchors": dict(anchors), "hard_conflicts": dict(conflicts),
            "anchor_attempts": dict(anchor_attempts),
            "anchor_acceptance_rates": {
                "isbn": round(anchors["EXACT_ISBN"] / anchor_attempts["isbn"], 4) if anchor_attempts["isbn"] else None,
                "edition_anchor": round(anchors["EDITION_ANCHORED_WORK"] / anchor_attempts["edition_anchor"], 4)
                if anchor_attempts["edition_anchor"] else None},
            "candidate_type_rejections": dict(type_rejections),
            "no_match_pre_network": {"total": sum(no_match_pre_network.values()), "by_target": dict(no_match_pre_network)},
            "wikidata_searches_attempted": {"total": sum(wikidata_searches_attempted.values()), "by_target": dict(wikidata_searches_attempted)},
            "wikidata_searches_avoided": {"total": sum(no_match_pre_network.values()), "by_target": dict(no_match_pre_network)},
            "error_count": len(errors), "errors_by_code": dict(Counter(x["code"] for x in errors)),
            "sample_ids": sample_ids}
