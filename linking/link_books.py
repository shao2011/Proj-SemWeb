#!/usr/bin/env python3
"""Link the books graph to Wikidata, DBpedia and Open Library (the 5-star step).

Input is the pipeline output (books_data.ttl). Output:
  links.ttl          owl:sameAs links for books, authors, series and editions
  enrichment.ttl     firstPublishYear taken from Wikidata (P577) where the CSV had none
  link_report.json   counts per link kind and method
  link_issues.json   ambiguous, colliding, conflicting and rejected cases
  review_sample.csv  fixed random sample for manual precision checking

Every HTTP response is cached under --cache, so a rerun with a warm cache is offline.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDFS, XSD

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from books_pipeline import BOOKS  # noqa: E402

WD = "http://www.wikidata.org/entity/"
DBR = "http://dbpedia.org/resource/"
OL_BOOKS = "https://openlibrary.org/books/"
WDQS = "https://query.wikidata.org/sparql"
WD_API = "https://www.wikidata.org/w/api.php"
OL_API = "https://openlibrary.org/api/books"
USER_AGENT = ("Proj-SemWeb-books-linker/1.0 (HUST IT6390E student project; "
              "https://github.com/shao2011/Proj-SemWeb)")
TITLE_LANGS = ("en", "mul", "en-us", "en-gb")
# Class roots (checked with wdt:P279*): an item is an edition, a written work, a series, or not a book.
EDITION_ROOT = "Q3331189"                         # version, edition or translation
WRITTEN_ROOTS = ("Q7725634", "Q47461344", "Q571")  # literary work, written work, book
SERIES_ROOT = "Q7725310"                          # series of creative works
NONBOOK_ROOTS = ("Q2431196", "Q7777570")          # audiovisual work (film, TV, radio), theatrical production
# Single works that Wikidata files under a series class: graphic novel, comic book album, comic book,
# manga volume, serialized fiction, penny dreadful.
WORK_CLASSES = {"Q725377", "Q2831984", "Q1760610", "Q125632018", "Q1347298", "Q3374808"}
# A removed subtitle/parenthetical naming a part or adaptation means "not the whole work".
PARTIAL = re.compile(r"\b(graphic novel|manga|vol\.?|volume|part|parts|book \d+|companion|"
                     r"omnibus|box(ed)? set|collection|illustrated|screenplay|script)\b", re.I)
NAME_STOP = {"jr", "sr", "dr", "sir", "ii", "iii"}
SAFE_IRI = set("!$&'()*+,;=:@/-._~")


# ---------------------------------------------------------------- HTTP + cache

class Client:
    """JSON over HTTP with an on-disk cache keyed by URL + body."""

    def __init__(self, cache: Path, offline: bool = False) -> None:
        self.cache, self.offline, self.requests = cache, offline, 0
        cache.mkdir(parents=True, exist_ok=True)

    def get(self, url: str, data: str | None = None, accept: str = "application/json"):
        key = hashlib.sha256(f"{url}\n{data or ''}".encode()).hexdigest()
        path = self.cache / key[:2] / f"{key}.json"
        if path.exists(): return json.loads(path.read_text(encoding="utf-8"))
        if self.offline: raise RuntimeError(f"not in cache (offline): {url}")
        for attempt in range(7):
            try:
                request = urllib.request.Request(url, data=data.encode() if data else None,
                                                 headers={"User-Agent": USER_AGENT, "Accept": accept})
                with urllib.request.urlopen(request, timeout=180) as response:
                    body = json.load(response)
                if isinstance(body, dict) and "error" in body:  # e.g. Wikidata maxlag
                    raise urllib.error.URLError(str(body["error"])[:200])
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    raise
                time.sleep(min(90, 5 * 2 ** attempt))
        else:
            raise RuntimeError(f"giving up after retries: {url[:120]}")
        self.requests += 1
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return body

    def sparql(self, query: str) -> list[dict]:
        body = self.get(WDQS, urllib.parse.urlencode({"query": query}), "application/sparql-results+json")
        return body["results"]["bindings"]


def chunks(values: list, size: int):
    for start in range(0, len(values), size): yield values[start:start + size]


def qid(uri: str) -> str: return uri.rsplit("/", 1)[1]
def value(binding: dict, key: str) -> str | None: return binding[key]["value"] if key in binding else None
def is_item(uri: str | None) -> bool: return bool(uri) and re.fullmatch(re.escape(WD) + r"Q\d+", uri) is not None
def entity(binding: dict, key: str) -> str | None:
    """A real Wikidata item, or None for unknown-value placeholders (.well-known/genid/...)."""
    uri = value(binding, key)
    return uri if is_item(uri) else None
def literal(text: str) -> str: return json.dumps(text, ensure_ascii=False)  # valid SPARQL string syntax
def entity_values(uris: list[str]) -> str: return " ".join("wd:" + qid(u) for u in uris)


# ---------------------------------------------------------------- matching rules

def name_tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[.\-'’]", " ", re.sub(r"\(.*?\)", "", text))
    return [t for t in re.findall(r"[^\W\d_]+", text) if t not in NAME_STOP]


def same_name(a: str, b: str) -> bool:
    """Same surname and compatible given names (initials allowed), or near-identical spelling."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb: return False
    if sorted(ta) == sorted(tb) or "".join(ta) == "".join(tb): return True
    if ta[-1] != tb[-1]:
        return difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio() >= 0.92
    ga, gb = ta[:-1], tb[:-1]
    if not ga or not gb: return False
    x, y = ga[0], gb[0]
    return x[0] == y[0] and (x.startswith(y) or y.startswith(x) or len(x) == 1 or len(y) == 1)


def norm_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.replace("’", "'").replace("‘", "'").replace("&", "and")
    return " ".join(re.sub(r"[^\w']+", " ", text).split())


def title_variants(title: str) -> list[str]:
    """The title, minus a trailing parenthetical, minus a subtitle, unless the removed part names a part."""
    out = [title]
    match = re.fullmatch(r"(.+?)\s*\(([^)]*)\)\s*", title)
    if match and not PARTIAL.search(match.group(2)): out.append(match.group(1).strip())
    for text in list(out):
        head, sep, tail = text.partition(":")
        if sep and not PARTIAL.search(tail): out.append(head.strip())
    return list(dict.fromkeys(t for t in out if len(t) >= 2))


def titles_compatible(ours: str, theirs: str) -> bool:
    a, b = (re.sub(r"^(the|a|an) ", "", norm_title(t)) for t in (ours, theirs))
    if not a or not b: return False
    if a == b or b in {norm_title(v) for v in title_variants(ours)}: return True
    # "Volume 1" vs "Volume 2": numbers must agree, though one side may carry extra ones ("50th Anniversary").
    # Taken from the raw titles as integers: "Vol. 01" is volume 1, and NFKC would turn "13½" into "131⁄2".
    na, nb = ({int(n) for n in re.findall(r"\d+", t)} for t in (ours, theirs))
    if na and nb and not (na <= nb or nb <= na): return False
    if min(len(a), len(b)) >= 4 and (a.startswith(b + " ") or b.startswith(a + " ")): return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def dbpedia_uri(enwiki_url: str) -> str:
    """DBpedia IRI for an English Wikipedia article (non-ASCII kept, unsafe ASCII escaped)."""
    title = urllib.parse.unquote(enwiki_url.rsplit("/wiki/", 1)[1])
    return DBR + "".join(c if ord(c) > 127 or c.isalnum() or c in SAFE_IRI
                         else urllib.parse.quote(c) for c in title)


def wikidata_year(raw: str) -> int | None:
    match = re.match(r"^([+-]?\d{1,4})-", raw)
    return int(match.group(1)) if match else None


def gyear(year: int) -> Literal:
    """xsd:gYear needs four digits after the sign: -395 -> "-0395"."""
    return Literal(f"{'-' if year < 0 else ''}{abs(year):04d}", datatype=XSD.gYear)


# ---------------------------------------------------------------- local data

@dataclass
class BookRecord:
    uri: URIRef
    title: str
    num_ratings: int
    first_year: int | None
    edition_years: list[int]
    authors: list[tuple[URIRef, str]]
    series: list[tuple[URIRef, str]]
    editions: list[tuple[URIRef, str, str | None]]  # (uri, Goodreads bookId, isbn)

    @property
    def goodreads_ids(self) -> list[str]:
        return [m.group(1) for _, book_id, _ in self.editions if (m := re.match(r"(\d+)", book_id))]


def load_books(graph: Graph) -> list[BookRecord]:
    def label(node): return str(graph.value(node, RDFS.label) or "")
    records = []
    for book in set(graph.subjects(BOOKS.hasEdition, None)):
        editions = [(e, str(graph.value(e, BOOKS.bookId)), graph.value(e, BOOKS.isbn))
                    for e in graph.objects(book, BOOKS.hasEdition)]
        first = graph.value(book, BOOKS.firstPublishYear)
        records.append(BookRecord(
            uri=book, title=str(graph.value(book, BOOKS.title)),
            num_ratings=int(graph.value(book, BOOKS.numRatings) or 0),
            first_year=int(str(first)) if first is not None else None,
            edition_years=[int(str(y)) for e, _, _ in editions for y in graph.objects(e, BOOKS.publishYear)],
            authors=sorted(((a, label(a)) for a in graph.objects(book, BOOKS.isWrittenBy)), key=str),
            series=sorted(((s, label(s)) for s in graph.objects(book, BOOKS.isPartOfSeries)), key=str),
            editions=sorted(((e, b, str(i) if i else None) for e, b, i in editions), key=str)))
    return sorted(records, key=lambda r: (-r.num_ratings, str(r.uri)))


# ---------------------------------------------------------------- Wikidata

@dataclass
class Item:
    labels: set[str] = field(default_factory=set)
    description: str | None = None
    sitelinks: int = 0
    classes: set[str] = field(default_factory=set)
    works: set[str] = field(default_factory=set)          # P629 edition-of
    years: set[int] = field(default_factory=set)          # P577
    authors: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # P50 -> names
    series: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))   # P179 -> names
    enwiki: str | None = None


def goodreads_items(client: Client, ids: list[str]) -> dict[str, list[str]]:
    found = defaultdict(list)
    for batch in chunks(sorted(set(ids)), 400):
        rows = client.sparql(f"SELECT ?id ?item WHERE {{ VALUES ?id {{ {' '.join(map(literal, batch))} }} "
                             f"?item wdt:P2969 ?id }}")
        for row in rows: found[value(row, "id")].append(value(row, "item"))
    return found


def label_items(client: Client, titles: list[str]) -> dict[str, set[str]]:
    """Items with an author (P50) whose label or alias equals a title exactly."""
    found = defaultdict(set)
    for batch in chunks(sorted(set(titles)), 100):
        values = " ".join(f"{literal(t)}@{lang}" for t in batch for lang in TITLE_LANGS)
        rows = client.sparql(f"SELECT DISTINCT ?t ?item WHERE {{ VALUES ?t {{ {values} }} "
                             f"{{ ?item rdfs:label ?t }} UNION {{ ?item skos:altLabel ?t }} "
                             f"FILTER EXISTS {{ ?item wdt:P50 [] }} }}")
        for row in rows: found[value(row, "t")].add(value(row, "item"))
    return found


def search_items(client: Client, texts: list[str], workers: int) -> tuple[dict[str, list[str]], list[str]]:
    """Wikidata full-text entity search (case-insensitive, catches label variants SPARQL misses)."""
    failed = []
    def one(text):
        url = WD_API + "?" + urllib.parse.urlencode(dict(
            action="wbsearchentities", search=text, language="en", strictlanguage="false",
            type="item", limit=12, format="json", maxlag=5))
        try: return [hit["concepturi"] for hit in client.get(url).get("search", [])]
        except RuntimeError: failed.append(text); return []
    texts = sorted(set(texts))
    with ThreadPoolExecutor(workers) as pool: results = dict(zip(texts, pool.map(one, texts)))
    return results, sorted(failed)


def fetch_items(client: Client, uris: set[str], items: dict[str, Item]) -> None:
    todo = sorted(u for u in uris if u not in items and is_item(u))
    langs = ", ".join(map(literal, TITLE_LANGS))
    for batch in chunks(todo, 200):
        values = entity_values(batch)
        for u in batch: items[u] = Item()
        for row in client.sparql(f"SELECT ?item ?l WHERE {{ VALUES ?item {{ {values} }} "
                                 f"{{ ?item rdfs:label ?l }} UNION {{ ?item skos:altLabel ?l }} "
                                 f"FILTER(LANG(?l) IN ({langs})) }}"):
            items[value(row, "item")].labels.add(value(row, "l"))
        for row in client.sparql(f"""SELECT ?item ?desc ?sl ?c ?w ?date ?art WHERE {{ VALUES ?item {{ {values} }}
              OPTIONAL {{ ?item schema:description ?desc FILTER(LANG(?desc) = "en") }}
              OPTIONAL {{ ?item wikibase:sitelinks ?sl }} OPTIONAL {{ ?item wdt:P31 ?c }}
              OPTIONAL {{ ?item wdt:P629 ?w }} OPTIONAL {{ ?item wdt:P577 ?date }}
              OPTIONAL {{ ?art schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }} }}"""):
            item = items[value(row, "item")]
            if value(row, "desc"): item.description = value(row, "desc")
            if value(row, "sl"): item.sitelinks = int(value(row, "sl"))
            if entity(row, "c"): item.classes.add(entity(row, "c"))
            if entity(row, "w"): item.works.add(entity(row, "w"))
            if value(row, "date") and (year := wikidata_year(value(row, "date"))) is not None: item.years.add(year)
            if value(row, "art"): item.enwiki = value(row, "art")
        for prop, attr in (("P50", "authors"), ("P179", "series")):
            for row in client.sparql(f"SELECT ?item ?x ?l WHERE {{ VALUES ?item {{ {values} }} ?item wdt:{prop} ?x . "
                                     f"OPTIONAL {{ {{ ?x rdfs:label ?l }} UNION {{ ?x skos:altLabel ?l }} "
                                     f"FILTER(LANG(?l) IN ({langs})) }} }}"):
                if not entity(row, "x"): continue
                names = getattr(items[value(row, "item")], attr)[entity(row, "x")]
                if value(row, "l"): names.add(value(row, "l"))


def fetch_classes(client: Client, classes: set[str], known: dict[str, tuple[bool, ...]]) -> None:
    """(written work, series, edition, not a book) flags per class, via the subclass hierarchy."""
    written = " ".join("wd:" + q for q in WRITTEN_ROOTS)
    nonbook = " ".join("wd:" + q for q in NONBOOK_ROOTS)
    for batch in chunks(sorted(c for c in classes if c not in known), 150):
        for row in client.sparql(f"""SELECT ?c ?w ?s ?e ?n WHERE {{ VALUES ?c {{ {entity_values(batch)} }}
              BIND(EXISTS {{ VALUES ?root {{ {written} }} ?c wdt:P279* ?root }} AS ?w)
              BIND(EXISTS {{ ?c wdt:P279* wd:{SERIES_ROOT} }} AS ?s)
              BIND(EXISTS {{ ?c wdt:P279* wd:{EDITION_ROOT} }} AS ?e)
              BIND(EXISTS {{ VALUES ?root {{ {nonbook} }} ?c wdt:P279* ?root }} AS ?n) }}"""):
            known[value(row, "c")] = tuple(value(row, k) == "true" for k in ("w", "s", "e", "n"))


def category(item: Item, classes: dict[str, tuple[bool, ...]]) -> str:
    """edition > known single-work class > not a book > series > written work > other."""
    flags = [classes.get(c, (False, False, False, False)) for c in item.classes]
    if any(f[2] for f in flags): return "edition"
    if any(qid(c) in WORK_CLASSES for c in item.classes): return "work"
    if any(f[3] for f in flags): return "other"   # e.g. the musical "Dear Evan Hansen"
    if any(f[1] for f in flags): return "series"  # e.g. typed both "novel series" and "literary work"
    if any(f[0] for f in flags): return "work"
    return "other"


def author_hits(names: list[str], item: Item) -> dict[str, str]:
    """Our author name -> the Wikidata author (P50) it matches."""
    hits = {}
    for name in names:
        for author, labels in sorted(item.authors.items()):
            if any(same_name(name, label) for label in labels): hits[name] = author; break
    return hits


def described_by(names: list[str], item: Item) -> bool:
    """Fallback when P50 is missing: the English description says '... by <author>'."""
    match = re.search(r"\bby (.+)$", item.description or "")
    if not match: return False
    credited = re.split(r",\s*|\s+and\s+|\s*&\s*", match.group(1))
    return any(same_name(n, c) for n in names for c in credited)


def choose_work(book: BookRecord, goodreads: list[str], candidates: set[str],
                items: dict[str, Item], classes: dict) -> tuple[str | None, str]:
    """Pick one Wikidata work for a book: Goodreads ID first, then title + author."""
    def as_work(uri: str) -> str | None:
        item = items.get(uri)
        if item is None: return None
        kind = category(item, classes)
        if kind == "work": return uri
        if kind == "edition" and len(item.works) == 1:
            work = next(iter(item.works))
            return work if work in items and category(items[work], classes) == "work" else None
        return None

    names = [name for _, name in book.authors]
    # Wikidata's own Goodreads IDs are occasionally wrong, so the author must not contradict.
    by_id = {w for uri in goodreads if (w := as_work(uri))
             and (not items[w].authors or author_hits(names, items[w]))}
    if len(by_id) == 1: return next(iter(by_id)), "goodreads-id"
    wanted = {norm_title(v) for v in title_variants(book.title)}
    full = norm_title(book.title)
    matches: dict[str, str] = {}
    for uri in sorted(candidates):
        item = items.get(uri)
        labels = {norm_title(l) for l in item.labels} if item else set()
        if not (labels & wanted): continue
        work = as_work(uri)
        if not work: continue
        short = "" if full in labels else "+short-title"  # matched only after dropping a subtitle
        if author_hits(names, item) or author_hits(names, items[work]): matches.setdefault(work, "title+author" + short)
        elif work == uri and described_by(names, item): matches.setdefault(work, "title+description" + short)
    if not matches: return None, "no-candidate"
    if len(matches) == 1: return next(iter(matches.items()))
    ranked = sorted(matches, key=lambda w: (-items[w].sitelinks, w))
    top, second = items[ranked[0]].sitelinks, items[ranked[1]].sitelinks
    if top >= 3 and top >= 2 * second: return ranked[0], matches[ranked[0]] + "+sitelinks"
    return None, "ambiguous"


def one_to_one(votes: dict[str, Counter], weight: dict[str, int]) -> tuple[dict[str, str], list[dict]]:
    """Resolve local -> external votes so owl:sameAs never merges two local resources.

    Each local resource takes its majority target (ties are conflicts). If several local
    resources share a target, only the one with most votes (then largest weight) keeps it.
    """
    issues, chosen = [], {}
    for local, counter in sorted(votes.items()):
        ranked = counter.most_common()
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            issues.append({"local": local, "reason": "conflicting_targets", "targets": dict(counter)}); continue
        chosen[local] = ranked[0][0]
    by_target = defaultdict(list)
    for local, target in chosen.items(): by_target[target].append(local)
    for target, locals_ in by_target.items():
        if len(locals_) < 2: continue
        locals_.sort(key=lambda l: (-votes[l][target], -weight.get(l, 0), l))
        for loser in locals_[1:]:
            issues.append({"local": loser, "reason": "shares_target", "target": target, "kept": locals_[0]})
            del chosen[loser]
    return chosen, issues


# ---------------------------------------------------------------- Open Library

def openlibrary_editions(client: Client, isbns: list[str]) -> dict[str, dict]:
    found = {}
    for batch in chunks(sorted(set(isbns)), 50):
        url = OL_API + "?" + urllib.parse.urlencode({"bibkeys": ",".join("ISBN:" + i for i in batch),
                                                     "format": "json", "jscmd": "data"})
        for key, record in client.get(url).items(): found[key.removeprefix("ISBN:")] = record
    return found


def link_editions(isbn_of: dict[str, tuple[str, str]], ol: dict[str, dict]) -> tuple[dict[str, str], list[dict]]:
    """Edition -> Open Library edition by ISBN, kept when the titles agree and no other edition of ours
    points to the same record."""
    links, issues = {}, []
    for edition, (isbn, title) in sorted(isbn_of.items()):
        record = ol.get(isbn)
        if not record or not record.get("key", "").startswith("/books/"): continue
        theirs = record.get("title", "") + (": " + record["subtitle"] if record.get("subtitle") else "")
        if titles_compatible(title, theirs) or titles_compatible(title, record.get("title", "")):
            links[edition] = OL_BOOKS + record["key"].removeprefix("/books/")
        else:
            issues.append({"edition": edition, "reason": "title_mismatch", "isbn": isbn,
                           "ours": title, "openlibrary": theirs})
    # Open Library sometimes answers several ISBNs with one record (volumes 1-3 of a comic merged),
    # and Goodreads has duplicate entries with one ISBN. owl:sameAs would make those editions one
    # individual and, since an edition isEditionOf exactly one Book, merge their books as well.
    shared = Counter(links.values())
    for edition in [e for e, target in links.items() if shared[target] > 1]:
        issues.append({"edition": edition, "reason": "shared_target", "isbn": isbn_of[edition][0],
                       "openlibrary": links.pop(edition)})
    return links, issues


# ---------------------------------------------------------------- main flow

def run(data_path: Path, out_dir: Path, cache: Path, offline: bool, workers: int, seed: int) -> dict:
    started = time.time()
    client = Client(cache, offline)
    graph = Graph().parse(data_path, format="turtle")
    books = load_books(graph)
    del graph
    log = lambda msg: print(f"[{time.time() - started:6.0f}s] {msg}", flush=True)
    log(f"{len(books)} books loaded")

    goodreads = goodreads_items(client, [i for b in books for i in b.goodreads_ids])
    log(f"Goodreads-ID lookups: {len(goodreads)} hits")
    by_label = label_items(client, [v for b in books for v in title_variants(b.title)])
    log(f"exact-label lookups: {sum(map(len, by_label.values()))} candidates")

    items: dict[str, Item] = {}
    classes: dict[str, tuple[bool, bool, bool]] = {}

    def refresh(uris: set[str]) -> None:
        fetch_items(client, uris, items)
        fetch_items(client, {w for u in uris for w in items[u].works}, items)  # works behind editions
        fetch_classes(client, {c for it in items.values() for c in it.classes}, classes)

    candidates = {b.uri: {u for v in title_variants(b.title) for u in by_label.get(v, ())} for b in books}
    gr_of = {b.uri: [u for i in b.goodreads_ids for u in goodreads.get(i, ())] for b in books}
    refresh({u for s in candidates.values() for u in s} | {u for l in gr_of.values() for u in l})
    log(f"details for {len(items)} items, {len(classes)} classes")
    decisions = {b.uri: choose_work(b, gr_of[b.uri], candidates[b.uri], items, classes) for b in books}

    unmatched = [b for b in books if decisions[b.uri][0] is None]
    texts = [v for b in unmatched for v in title_variants(b.title)]
    searched, search_failures = search_items(client, texts, workers)
    log(f"search for {len(unmatched)} unmatched books ({len(texts)} titles, {len(search_failures)} failed)")
    for b in unmatched: candidates[b.uri] |= {u for v in title_variants(b.title) for u in searched.get(v, ())}
    refresh({u for b in unmatched for u in candidates[b.uri]})
    for b in unmatched: decisions[b.uri] = choose_work(b, gr_of[b.uri], candidates[b.uri], items, classes)

    # Books: one Wikidata work per book and one book per work.
    by_uri = {b.uri: b for b in books}
    book_votes = {str(u): Counter({w: 1}) for u, (w, _) in decisions.items() if w}
    book_links, book_issues = one_to_one(book_votes, {str(b.uri): b.num_ratings for b in books})
    method = {str(u): m for u, (w, m) in decisions.items() if w}
    # Several books on one item, and the winner only matched a shortened title: the item is an
    # umbrella ("Batman: Year One" and "Batman: The Dark Knight Returns" both hit "Batman").
    for kept in sorted({i["kept"] for i in book_issues if "kept" in i}):
        if kept in book_links and "short-title" in method[kept]:
            book_issues.append({"local": kept, "reason": "umbrella_item", "target": book_links.pop(kept)})
    log(f"books linked: {len(book_links)}")

    # Authors and series: votes from every linked book. A series must link to an item that
    # Wikidata classifies as a series (P179 sometimes points at a character or franchise).
    refresh({s for work in book_links.values() for s in items[work].series})
    person_votes, series_votes = defaultdict(Counter), defaultdict(Counter)
    for local, work in book_links.items():
        book, item = by_uri[URIRef(local)], items[work]
        for person, name in book.authors:
            if (target := author_hits([name], item).get(name)): person_votes[str(person)][target] += 1
        for series, name in book.series:
            hits = [s for s, labels in item.series.items()
                    if norm_title(name) in {norm_title(l) for l in labels}
                    and s in items and category(items[s], classes) == "series"]
            if len(hits) == 1: series_votes[str(series)][hits[0]] += 1
    person_links, person_issues = one_to_one(person_votes, {})
    series_links, series_issues = one_to_one(series_votes, {})
    refresh(set(person_links.values()))
    log(f"persons linked: {len(person_links)}, series linked: {len(series_links)}")

    # First-publication year from P577 (earliest), never later than an edition we hold.
    year_added, year_issues, year_check = {}, [], Counter()
    for local, work in book_links.items():
        book, years = by_uri[URIRef(local)], items[work].years
        if not years: continue
        year = min(years)
        if book.first_year is not None:
            year_check["agree" if book.first_year == year else "disagree"] += 1
            if book.first_year != year:
                year_issues.append({"book": local, "reason": "differs_from_csv", "csv": book.first_year, "wikidata": year})
        elif book.edition_years and year > min(book.edition_years):
            year_issues.append({"book": local, "reason": "after_known_edition", "wikidata": year,
                                "edition_year": min(book.edition_years)})
        else:
            year_added[local] = year

    # Editions: Open Library by ISBN.
    isbn_of = {str(e): (i, b.title) for b in books for e, _, i in b.editions if i}
    ol = openlibrary_editions(client, [i for i, _ in isbn_of.values()])
    edition_links, edition_issues = link_editions(isbn_of, ol)
    log(f"Open Library: {len(ol)} of {len(isbn_of)} ISBNs found, {len(edition_links)} linked")

    # ------------------------------------------------------------ write RDF
    out_dir.mkdir(parents=True, exist_ok=True)
    links = Graph()
    for prefix, ns in (("books", BOOKS), ("owl", OWL), ("wd", WD), ("dbr", DBR), ("olb", OL_BOOKS)):
        links.bind(prefix, Namespace(str(ns)))
    dbpedia = Counter()
    for kind, mapping in (("book", book_links), ("person", person_links), ("series", series_links)):
        for local, target in mapping.items():
            links.add((URIRef(local), OWL.sameAs, URIRef(target)))
            if items[target].enwiki:
                links.add((URIRef(local), OWL.sameAs, URIRef(dbpedia_uri(items[target].enwiki))))
                dbpedia[kind] += 1
    for local, target in edition_links.items(): links.add((URIRef(local), OWL.sameAs, URIRef(target)))
    links.serialize(out_dir / "links.ttl", format="turtle")
    enrichment = Graph()
    enrichment.bind("books", Namespace(str(BOOKS)))
    for local, year in year_added.items():
        enrichment.add((URIRef(local), BOOKS.firstPublishYear, gyear(year)))
    enrichment.serialize(out_dir / "enrichment.ttl", format="turtle")

    # ------------------------------------------------------------ reports
    issues = {
        "unmatched_books": [{"book": str(b.uri), "title": b.title, "reason": decisions[b.uri][1]}
                            for b in books if decisions[b.uri][0] is None],
        "book_collisions": book_issues, "person_issues": person_issues, "series_issues": series_issues,
        "year_issues": year_issues, "edition_issues": edition_issues, "search_failures": search_failures}
    (out_dir / "link_issues.json").write_text(json.dumps(issues, indent=1, ensure_ascii=False), encoding="utf-8")
    persons = {str(p) for b in books for p, _ in b.authors}
    series = {str(s) for b in books for s, _ in b.series}
    report = {
        "books": len(books), "books_linked": len(book_links),
        "book_methods": dict(Counter(method[l] for l in book_links)),
        "books_unmatched_by_reason": dict(Counter(i["reason"] for i in issues["unmatched_books"])),
        "book_collisions_dropped": len(book_issues),
        "persons": len(persons), "persons_linked": len(person_links), "person_issues": len(person_issues),
        "series": len(series), "series_linked": len(series_links), "series_issues": len(series_issues),
        "dbpedia_links": dict(dbpedia),
        "first_year_added": len(year_added), "first_year_check_vs_csv": dict(year_check),
        "first_year_rejected_after_edition": sum(i["reason"] == "after_known_edition" for i in year_issues),
        "editions_with_isbn": len(isbn_of), "openlibrary_found": sum(i in ol for i, _ in isbn_of.values()),
        "editions_linked": len(edition_links),
        "edition_title_mismatch": sum(i["reason"] == "title_mismatch" for i in edition_issues),
        "edition_shared_target_dropped": sum(i["reason"] == "shared_target" for i in edition_issues),
        "link_triples": len(links), "enrichment_triples": len(enrichment),
        "http_requests_this_run": client.requests}
    (out_dir / "link_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    write_review_sample(out_dir / "review_sample.csv", seed, by_uri, book_links, method, person_links,
                        series_links, edition_links, items, isbn_of)
    log("done")
    return report


def write_review_sample(path: Path, seed: int, by_uri, book_links, method, person_links, series_links,
                        edition_links, items, isbn_of) -> None:
    rng = random.Random(seed)
    names = {str(p): n for b in by_uri.values() for p, n in b.authors}
    series_names = {str(s): n for b in by_uri.values() for s, n in b.series}
    def describe(target): return f"{sorted(items[target].labels)[:1]} — {items[target].description or ''}"
    rows = []
    sampled = rng.sample(sorted(book_links), min(100, len(book_links)))
    for risky in ("short-title", "description", "sitelinks"):  # plus up to 25 of each weaker method
        pool = sorted(l for l in book_links if risky in method[l] and l not in sampled)
        sampled += rng.sample(pool, min(25, len(pool)))
    for local in sampled:
        book = by_uri[URIRef(local)]
        rows.append(["book", method[local], local, f"{book.title} / {', '.join(n for _, n in book.authors)}",
                     book_links[local], describe(book_links[local]), ""])
    for local in rng.sample(sorted(person_links), min(40, len(person_links))):
        rows.append(["person", "via-linked-book", local, names[local], person_links[local],
                     describe(person_links[local]), ""])
    for local in rng.sample(sorted(series_links), min(20, len(series_links))):
        rows.append(["series", "via-linked-book", local, series_names[local], series_links[local],
                     describe(series_links[local]), ""])
    for local in rng.sample(sorted(edition_links), min(40, len(edition_links))):
        isbn, title = isbn_of[local]
        rows.append(["edition", "isbn+title", local, f"{title} / ISBN {isbn}", edition_links[local], "", ""])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["kind", "method", "local", "local_label", "target", "target_label", "verdict"])
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "pipeline/output/books_data.ttl")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "linking/output")
    parser.add_argument("--cache", type=Path, default=ROOT / "linking/.cache")
    parser.add_argument("--offline", action="store_true", help="fail instead of making HTTP requests")
    parser.add_argument("--workers", type=int, default=4, help="parallel Wikidata searches (be polite)")
    parser.add_argument("--seed", type=int, default=42, help="seed for the review sample")
    args = parser.parse_args()
    print(json.dumps(run(args.data, args.out_dir, args.cache, args.offline, args.workers, args.seed), indent=1))


if __name__ == "__main__": main()
