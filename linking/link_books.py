#!/usr/bin/env python3
"""Link the books graph to Wikidata, DBpedia and Open Library (the 5-star step).

Input is the pipeline output (books_data.ttl). Output:
  links.ttl          owl:sameAs links for books, authors, series, editions, publishers and languages
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
import itertools
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
# Audiovisual work (film, TV, radio), theatrical production, fictional character. Wikidata types
# "Richard Hannay" both as a book series and as a fictional human.
NONBOOK_ROOTS = ("Q2431196", "Q7777570", "Q95074")
# Single works that Wikidata files under a series class: graphic novel, comic book album, comic book,
# manga volume, serialized fiction, penny dreadful.
WORK_CLASSES = {"Q725377", "Q2831984", "Q1760610", "Q125632018", "Q1347298", "Q3374808"}
# A removed subtitle/parenthetical naming a part or adaptation means "not the whole work".
PARTIAL = re.compile(r"\b(graphic novel|manga|vol\.?|volume|part|parts|book \d+|companion|"
                     r"omnibus|box(ed)? set|collection|illustrated|screenplay|script)\b", re.I)
NAME_STOP = {"jr", "sr", "dr", "sir", "ii", "iii"}
# Trailing words that don't distinguish publishers ("Avon" = "Avon Books"). "Press" and "Group" are
# kept: "Scholastic Press" is an imprint of Scholastic, "Penguin Group" the parent of Penguin Books.
CORPORATE = {"inc", "incorporated", "ltd", "limited", "llc", "co", "company", "corp", "corporation",
             "books", "book", "publishing", "publishers", "publisher", "publications", "and"}
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


ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)


def lookup_titles(title: str) -> list[str]:
    """Title variants plus each with its leading English article dropped, or with "The " added:
    Wikidata labels the novel "The Murder at the Vicarage", Goodreads calls it "Murder at the Vicarage"."""
    out = []
    for v in title_variants(title):
        out += [v, ARTICLE.sub("", v) if ARTICLE.match(v) else "The " + v]
    return list(dict.fromkeys(t for t in out if len(t) >= 2))


def title_key(text: str) -> str:
    """Normalized title without a leading article, for comparing labels."""
    return ARTICLE.sub("", norm_title(text))


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


# ISBNs. Checksum validation and the hyphenation probe follow Hao's linker (linking/external.py on main).

def isbn13(raw: str | None) -> str | None:
    """Checksum-valid ISBN-13 (an ISBN-10 is converted), else None, so a typo or an EAN never
    reaches a lookup."""
    digits = re.sub(r"[\s\-\u2010-\u2015]", "", raw or "").upper()
    if re.fullmatch(r"\d{9}[\dX]", digits):
        if sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits)) % 11: return None
        stem = "978" + digits[:9]
        return stem + str((10 - sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(stem)) % 10) % 10)
    if re.fullmatch(r"97[89]\d{10}", digits) and \
            sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(digits)) % 10 == 0:
        return digits
    return None


def isbn10(isbn: str) -> str | None:
    """The ISBN-10 of a 978- ISBN-13 (Wikidata often has only P957 on older editions)."""
    if not isbn.startswith("978"): return None
    stem = isbn[3:12]
    check = (11 - sum((10 - i) * int(c) for i, c in enumerate(stem)) % 11) % 11
    return stem + ("X" if check == 10 else str(check))


def isbn_forms(digits: str) -> set[str]:
    """Ways Wikidata may write an ISBN: compact, or hyphenated as [prefix-]group-publisher-title-check.
    The split points depend on ISBN range tables, so every split is tried; WDQS matches exact values."""
    stem, check = digits[:-1], digits[-1]
    prefix, middle = (stem[:3], stem[3:]) if len(digits) == 13 else ("", stem)
    forms = {digits}
    for i, j in itertools.combinations(range(1, len(middle)), 2):
        forms.add("-".join(([prefix] if prefix else []) + [middle[:i], middle[i:j], middle[j:], check]))
    return forms


def publisher_key(name: str) -> str:
    tokens = norm_title(name).split()
    while tokens and tokens[-1] in CORPORATE: tokens.pop()
    if tokens[:1] == ["the"]: tokens.pop(0)
    return " ".join(tokens)


def same_publisher(a: str, b: str) -> bool:
    key = publisher_key(a)
    return len(key) >= 3 and key == publisher_key(b)


def language_names(label: str) -> list[str]:
    """Names in a Goodreads (ISO 639-2) language label: "Bokmål, Norwegian; Norwegian Bokmål"."""
    names = []
    for part in (p.strip() for p in label.split(";")):
        if not part: continue
        names.append(part)
        head, comma, tail = part.partition(",")
        if comma and tail.strip(): names.append(f"{tail.strip()} {head.strip()}")
    return list(dict.fromkeys(names))


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


def edition_values(graph: Graph, prop: URIRef) -> dict[str, tuple[str, str]]:
    """Edition -> (resource, label) for a one-valued edition property (publisher, language)."""
    return {str(e): (str(o), str(graph.value(o, RDFS.label) or "")) for e, o in graph.subject_objects(prop)}


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
    titles: set[str] = field(default_factory=set)         # P1476 (editions)
    publishers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # P123 -> names
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


def isbn_items(client: Client, isbns: list[str], workers: int) -> dict[str, set[str]]:
    """ISBN-13 -> Wikidata items carrying it as ISBN-13 (P212) or ISBN-10 (P957)."""
    def one(batch: list[str]) -> list[tuple[str, str]]:
        back = {}
        for isbn in batch:
            i10 = isbn10(isbn)
            for form in isbn_forms(isbn) | (isbn_forms(i10) if i10 else set()): back[form] = isbn
        rows = client.sparql(f"SELECT ?item ?isbn WHERE {{ VALUES ?isbn {{ {' '.join(map(literal, sorted(back)))} }} "
                             f"VALUES ?p {{ wdt:P212 wdt:P957 }} ?item ?p ?isbn }}")
        return [(back[value(r, "isbn")], entity(r, "item")) for r in rows]
    found = defaultdict(set)
    with ThreadPoolExecutor(workers) as pool:
        for rows in pool.map(one, list(chunks(sorted(set(isbns)), 25))):
            for isbn, uri in rows:
                if uri: found[isbn].add(uri)
    return found


def fetch_edition_details(client: Client, uris: set[str], items: dict[str, Item]) -> None:
    """For edition items (already in `items`): formal titles, publishers, and works listing them (P747)."""
    langs = ", ".join(map(literal, TITLE_LANGS))
    for batch in chunks(sorted(uris), 200):
        for row in client.sparql(f"""SELECT ?item ?t ?w ?p ?pl WHERE {{ VALUES ?item {{ {entity_values(batch)} }}
              {{ ?item wdt:P1476 ?t }} UNION {{ ?w wdt:P747 ?item }} UNION
              {{ ?item wdt:P123 ?p OPTIONAL {{ {{ ?p rdfs:label ?pl }} UNION {{ ?p skos:altLabel ?pl }}
                                              FILTER(LANG(?pl) IN ({langs})) }} }} }}"""):
            item = items[value(row, "item")]
            if value(row, "t"): item.titles.add(value(row, "t"))
            if entity(row, "w"): item.works.add(entity(row, "w"))
            if entity(row, "p"):
                names = item.publishers[entity(row, "p")]
                if value(row, "pl"): names.add(value(row, "pl"))


def language_items(client: Client, names: list[str]) -> dict[str, dict[str, set[str]]]:
    """Name -> {"label": items, "alias": items}, among items with an ISO 639-2 code (P219), the
    standard Goodreads language names come from."""
    found = defaultdict(lambda: defaultdict(set))
    values = " ".join(f"{literal(n)}@en" for n in sorted(set(names)))
    for row in client.sparql(f"""SELECT ?name ?item ?alias WHERE {{ VALUES ?name {{ {values} }}
          {{ ?item rdfs:label ?name BIND(false AS ?alias) }} UNION {{ ?item skos:altLabel ?name BIND(true AS ?alias) }}
          ?item wdt:P219 [] }}"""):
        if entity(row, "item"):
            found[value(row, "name")]["alias" if value(row, "alias") == "true" else "label"].add(entity(row, "item"))
    return found


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
    wanted = {title_key(v) for v in title_variants(book.title)}
    exact_titles = {norm_title(v) for v in title_variants(book.title)}
    full = title_key(book.title)
    matches: dict[str, str] = {}
    exact: set[str] = set()  # works matched without adding or dropping a leading article
    shared: Counter = Counter()  # how many of our authors each work's P50 covers
    for uri in sorted(candidates):
        item = items.get(uri)
        labels = {title_key(l) for l in item.labels} if item else set()
        if not (labels & wanted): continue
        work = as_work(uri)
        if not work: continue
        short = "" if full in labels else "+short-title"  # matched only after dropping a subtitle
        hits = max(len(author_hits(names, item)), len(author_hits(names, items[work])))
        if hits:
            matches.setdefault(work, "title+author" + short)
            shared[work] = max(shared[work], hits)
        elif work == uri and described_by(names, item): matches.setdefault(work, "title+description" + short)
        else: continue
        if {norm_title(l) for l in item.labels} & exact_titles: exact.add(work)
    if not matches: return None, "no-candidate"
    # A description naming our author is weaker evidence than P50: "Murder at the Vicarage" the play is
    # "written by Agatha Christie" too, but only the novel has her as author.
    if any(m.startswith("title+author") for m in matches.values()):
        matches = {w: m for w, m in matches.items() if m.startswith("title+author")}
        # "Nightfall" by Asimov and Silverberg is the 1990 novel by both, not Asimov's 1941 story.
        most = max(shared[w] for w in matches)
        matches = {w: m for w, m in matches.items() if shared[w] == most}
    if len(matches) == 1: return next(iter(matches.items()))
    ranked = sorted(matches, key=lambda w: (-items[w].sitelinks, w))
    top, second = items[ranked[0]].sitelinks, items[ranked[1]].sitelinks
    if top >= 3 and top >= 2 * second: return ranked[0], matches[ranked[0]] + "+sitelinks"
    # No clear main item: a label equal to our title beats one equal only after an article change.
    # A stray 2020 item "Court of Mist and Fury" (same P50, no sitelinks) made the novel ambiguous.
    exact_matches = sorted(exact & set(matches))
    if len(exact_matches) == 1: return exact_matches[0], matches[exact_matches[0]] + "+exact-title"
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


def choose_edition(title: str, uris: set[str], items: dict[str, Item], classes: dict) -> tuple[str | None, str]:
    """The Wikidata edition for one ISBN: edition-level item whose title agrees with the book's."""
    editions = [u for u in sorted(uris) if u in items and category(items[u], classes) == "edition"]
    if not editions: return None, "not_an_edition"
    fits = [u for u in editions if any(titles_compatible(title, t) for t in items[u].labels | items[u].titles)]
    if len(fits) == 1: return fits[0], "isbn+title"
    return None, "several_items" if fits else "title_mismatch"


def work_via_editions(book: BookRecord, editions: list[str], items: dict[str, Item],
                      classes: dict) -> tuple[str | None, str]:
    """A book's work through its linked Wikidata editions (P629, or the work's P747): exactly one work,
    a written work, compatible title, and no contradicting author."""
    works = {w for e in editions for w in items[e].works}
    if not works: return None, "edition_without_work"
    if len(works) > 1: return None, "editions_disagree"
    work = next(iter(works))
    item = items.get(work)
    if item is None or category(item, classes) != "work": return None, "not_a_work"
    if not any(titles_compatible(book.title, label) for label in item.labels): return None, "title_mismatch"
    if item.authors and not author_hits([n for _, n in book.authors], item): return None, "author_mismatch"
    return work, "isbn-edition"


def choose_language(label: str, hits: dict[str, dict[str, set[str]]]) -> tuple[str | None, str]:
    """Exact English label first, then alias; one item or nothing."""
    names = language_names(label)
    for kind in ("label", "alias"):
        found = {u for n in names for u in hits.get(n, {}).get(kind, ())}
        if len(found) == 1: return next(iter(found)), "iso639-" + kind
        if found: return None, "ambiguous"
    return None, "no_candidate"


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
    issues += drop_shared(links, isbn_of)
    return links, issues


def link_wikidata_editions(isbn_of: dict[str, tuple[str, str]], found: dict[str, set[str]],
                           items: dict[str, Item], classes: dict) -> tuple[dict[str, str], list[dict]]:
    """Edition -> Wikidata edition item by ISBN (P212/P957), with the same rules as Open Library."""
    links, issues = {}, []
    for edition, (isbn, title) in sorted(isbn_of.items()):
        if not found.get(isbn): continue
        target, reason = choose_edition(title, found[isbn], items, classes)
        if target: links[edition] = target
        else: issues.append({"edition": edition, "reason": reason, "isbn": isbn, "ours": title,
                             "candidates": sorted(found[isbn])})
    issues += drop_shared(links, isbn_of)
    return links, issues


def drop_shared(links: dict[str, str], isbn_of: dict[str, tuple[str, str]]) -> list[dict]:
    """Unlink editions that share a target. Open Library merges volumes into one record, and Goodreads
    lists some books twice with one ISBN; owl:sameAs would make those editions one individual and,
    since an edition isEditionOf exactly one Book, merge their books as well."""
    shared = Counter(links.values())
    return [{"edition": e, "reason": "shared_target", "isbn": isbn_of[e][0], "target": links.pop(e)}
            for e in [e for e, target in links.items() if shared[target] > 1]]


# ---------------------------------------------------------------- main flow

def run(data_path: Path, out_dir: Path, cache: Path, offline: bool, workers: int, seed: int) -> dict:
    started = time.time()
    client = Client(cache, offline)
    graph = Graph().parse(data_path, format="turtle")
    books = load_books(graph)
    publisher_of = edition_values(graph, BOOKS.isPublishedBy)
    language_of = edition_values(graph, BOOKS.isInLanguage)
    del graph
    log = lambda msg: print(f"[{time.time() - started:6.0f}s] {msg}", flush=True)
    log(f"{len(books)} books loaded")

    goodreads = goodreads_items(client, [i for b in books for i in b.goodreads_ids])
    log(f"Goodreads-ID lookups: {len(goodreads)} hits")
    # The article forms go in their own batches, so the cached batches of the plain titles stay valid.
    plain = {v for b in books for v in title_variants(b.title)}
    by_label = label_items(client, sorted(plain))
    by_label.update(label_items(client, sorted({v for b in books for v in lookup_titles(b.title)} - plain)))
    log(f"exact-label lookups: {sum(map(len, by_label.values()))} candidates")

    items: dict[str, Item] = {}
    classes: dict[str, tuple[bool, bool, bool]] = {}

    def refresh(uris: set[str]) -> None:
        fetch_items(client, uris, items)
        fetch_items(client, {w for u in uris for w in items[u].works}, items)  # works behind editions
        fetch_classes(client, {c for it in items.values() for c in it.classes}, classes)

    candidates = {b.uri: {u for v in lookup_titles(b.title) for u in by_label.get(v, ())} for b in books}
    gr_of = {b.uri: [u for i in b.goodreads_ids for u in goodreads.get(i, ())] for b in books}
    refresh({u for b in books for v in title_variants(b.title) for u in by_label.get(v, ())}
            | {u for l in gr_of.values() for u in l})
    refresh({u for s in candidates.values() for u in s})  # items found only through the article forms
    log(f"details for {len(items)} items, {len(classes)} classes")
    decisions = {b.uri: choose_work(b, gr_of[b.uri], candidates[b.uri], items, classes) for b in books}

    unmatched = [b for b in books if decisions[b.uri][0] is None]
    texts = [v for b in unmatched for v in title_variants(b.title)]
    searched, search_failures = search_items(client, texts, workers)
    log(f"search for {len(unmatched)} unmatched books ({len(texts)} titles, {len(search_failures)} failed)")
    for b in unmatched: candidates[b.uri] |= {u for v in title_variants(b.title) for u in searched.get(v, ())}
    refresh({u for b in unmatched for u in candidates[b.uri]})
    for b in unmatched: decisions[b.uri] = choose_work(b, gr_of[b.uri], candidates[b.uri], items, classes)

    # Editions -> Wikidata by ISBN (P212/P957). Their works (P629, or the work's P747) give a second,
    # title-independent route to the book: used for books the title match missed, checked for the rest.
    isbn_of, isbn_invalid = {}, []
    for b in books:
        for e, _, raw in b.editions:
            if raw is None: continue
            if (isbn := isbn13(raw)): isbn_of[str(e)] = (isbn, b.title)
            else: isbn_invalid.append({"edition": str(e), "isbn": raw})
    found = isbn_items(client, [i for i, _ in isbn_of.values()], workers)
    edition_items = {u for s in found.values() for u in s}
    refresh(edition_items)
    fetch_edition_details(client, edition_items, items)
    refresh(edition_items)  # works found through P747
    wd_edition_links, wd_edition_issues = link_wikidata_editions(isbn_of, found, items, classes)
    log(f"Wikidata editions: {len(found)} of {len(isbn_of)} valid ISBNs found, {len(wd_edition_links)} linked")
    isbn_route, isbn_check, isbn_disagreements = Counter(), Counter(), []
    for b in books:
        editions = [wd_edition_links[str(e)] for e, _, _ in b.editions if str(e) in wd_edition_links]
        if not editions: continue
        work, reason = work_via_editions(b, editions, items, classes)
        isbn_route[reason] += 1
        if decisions[b.uri][0] is None:
            if work: decisions[b.uri] = (work, reason)
        elif work:
            isbn_check["agree" if work == decisions[b.uri][0] else "disagree"] += 1
            if work != decisions[b.uri][0]:
                isbn_disagreements.append({"book": str(b.uri), "title": b.title, "kept": decisions[b.uri][0],
                                           "method": decisions[b.uri][1], "via_isbn": work})

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

    # Publishers: the P123 of each linked Wikidata edition, when the name agrees; majority, one-to-one.
    publisher_votes = defaultdict(Counter)
    for edition, target in wd_edition_links.items():
        if edition not in publisher_of: continue
        local, name = publisher_of[edition]
        hits = [p for p, labels in items[target].publishers.items() if any(same_publisher(name, l) for l in labels)]
        if len(hits) == 1: publisher_votes[local][hits[0]] += 1
    publisher_links, publisher_issues = one_to_one(publisher_votes, {})

    # Languages: Goodreads uses ISO 639-2 English names; match them among items with an ISO 639-2 code.
    languages = dict(language_of.values())
    language_hits = language_items(client, [n for label in languages.values() for n in language_names(label)])
    language_decisions = {local: choose_language(label, language_hits) for local, label in languages.items()}
    language_links, language_issues = one_to_one(
        {local: Counter({t: 1}) for local, (t, _) in language_decisions.items() if t}, {})
    language_issues += [{"local": local, "label": languages[local], "reason": reason}
                        for local, (t, reason) in sorted(language_decisions.items()) if not t]
    refresh(set(publisher_links.values()) | set(language_links.values()))  # for their Wikipedia articles
    log(f"publishers linked: {len(publisher_links)} of {len(set(l for l, _ in publisher_of.values()))}, "
        f"languages linked: {len(language_links)} of {len(languages)}")

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
    ol = openlibrary_editions(client, [i for i, _ in isbn_of.values()])
    edition_links, edition_issues = link_editions(isbn_of, ol)
    log(f"Open Library: {len(ol)} of {len(isbn_of)} ISBNs found, {len(edition_links)} linked")

    # ------------------------------------------------------------ write RDF
    out_dir.mkdir(parents=True, exist_ok=True)
    links = Graph()
    for prefix, ns in (("books", BOOKS), ("owl", OWL), ("wd", WD), ("dbr", DBR), ("olb", OL_BOOKS)):
        links.bind(prefix, Namespace(str(ns)))
    dbpedia = Counter()
    for kind, mapping in (("book", book_links), ("person", person_links), ("series", series_links),
                          ("publisher", publisher_links), ("language", language_links)):
        for local, target in mapping.items():
            links.add((URIRef(local), OWL.sameAs, URIRef(target)))
            if items[target].enwiki:
                links.add((URIRef(local), OWL.sameAs, URIRef(dbpedia_uri(items[target].enwiki))))
                dbpedia[kind] += 1
    for mapping in (edition_links, wd_edition_links):
        for local, target in mapping.items(): links.add((URIRef(local), OWL.sameAs, URIRef(target)))
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
        "year_issues": year_issues, "edition_issues": edition_issues, "search_failures": search_failures,
        "invalid_isbns": isbn_invalid, "wikidata_edition_issues": wd_edition_issues,
        "isbn_route_disagreements": isbn_disagreements, "publisher_issues": publisher_issues,
        "language_issues": language_issues}
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
        "editions_with_isbn": len(isbn_of) + len(isbn_invalid), "isbn_invalid": len(isbn_invalid),
        "openlibrary_found": sum(i in ol for i, _ in isbn_of.values()),
        "editions_linked": len(edition_links),
        "edition_title_mismatch": sum(i["reason"] == "title_mismatch" for i in edition_issues),
        "edition_shared_target_dropped": sum(i["reason"] == "shared_target" for i in edition_issues),
        "wikidata_isbns_found": len(found), "wikidata_editions_linked": len(wd_edition_links),
        "wikidata_edition_rejected": dict(Counter(i["reason"] for i in wd_edition_issues)),
        "isbn_route_to_work": dict(isbn_route), "isbn_route_vs_title_match": dict(isbn_check),
        "publishers": len(set(l for l, _ in publisher_of.values())), "publishers_linked": len(publisher_links),
        "publisher_issues": len(publisher_issues),
        "languages": len(languages), "languages_linked": len(language_links),
        "link_triples": len(links), "enrichment_triples": len(enrichment),
        "http_requests_this_run": client.requests}
    (out_dir / "link_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    edition_labels = {e: f"{t} / ISBN {i}" for e, (i, t) in isbn_of.items()}
    write_review_sample(out_dir / "review_sample.csv", seed, by_uri, book_links, method, items, [
        ("person", "via-linked-book", person_links, {str(p): n for b in books for p, n in b.authors}, 40),
        ("series", "via-linked-book", series_links, {str(s): n for b in books for s, n in b.series}, 20),
        ("edition", "openlibrary-isbn+title", edition_links, edition_labels, 40),
        ("edition", "wikidata-isbn+title", wd_edition_links, edition_labels, 40),
        ("publisher", "via-linked-edition", publisher_links, dict(publisher_of.values()), 30),
        ("language", "iso639-name", language_links, languages, 30)])
    log("done")
    return report


def write_review_sample(path: Path, seed: int, by_uri, book_links, method, items, groups) -> None:
    """Books (random plus the weaker methods), then `groups`: (kind, method, links, local -> label, size)."""
    rng = random.Random(seed)
    def describe(target, ours):
        """The target's label closest to our name (the one the match used) and its English description."""
        item = items.get(target)
        if not item: return ""
        ours = ours.split(" / ")[0]
        label = max(sorted(item.labels), key=lambda l: difflib.SequenceMatcher(None, l.casefold(), ours.casefold()).ratio(),
                    default="")
        return f"{label} — {item.description or ''}"
    rows = []
    sampled = rng.sample(sorted(book_links), min(100, len(book_links)))
    for risky in ("short-title", "description", "sitelinks", "isbn-edition"):  # plus up to 25 of each
        pool = sorted(l for l in book_links if risky in method[l] and l not in sampled)
        sampled += rng.sample(pool, min(25, len(pool)))
    for local in sampled:
        book = by_uri[URIRef(local)]
        label = f"{book.title} / {', '.join(n for _, n in book.authors)}"
        rows.append(["book", method[local], local, label, book_links[local], describe(book_links[local], label), ""])
    for kind, how, links, labels, size in groups:
        for local in rng.sample(sorted(links), min(size, len(links))):
            rows.append([kind, how, local, labels[local], links[local], describe(links[local], labels[local]), ""])
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
