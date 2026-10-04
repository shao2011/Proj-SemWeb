"""Deterministic Goodreads CSV -> Books ontology instance graph.

The ontology is read for vocabulary validation, never modified or copied into
the output graph.  A row is parsed first, editions are resolved second, and
work groups are emitted only after deterministic aggregation.
"""

from __future__ import annotations

import argparse
import ast
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD


BOOKS = Namespace("https://shao2011.github.io/Proj-SemWeb/ontology#")
DEFAULT_BASE = "https://shao2011.github.io/Proj-SemWeb/resource/"
DEFAULT_TOP_N = 10000
CATEGORIES = (
    "skipped_rows", "duplicate_book_ids", "ambiguous_work_groups",
    "conflicting_work_values", "invalid_isbn", "invalid_dates",
    "malformed_list_fields", "unknown_contributor_roles", "works_without_author",
    "award_parse_issues", "rating_count_mismatch", "invalid_values",
)
LIST_FIELDS = ("genres", "characters", "awards", "setting")
REP_FIELDS = ("title", "description", "rating", "numRatings", "ratingsByStars",
              "likedPercent", "bbeScore", "bbeVotes")
EDITION_FIELDS = ("isbn", "edition", "pages", "publishDate", "publishYear", "publisher",
                  "language", "bookFormat", "coverImg")
STAR_PROPERTIES = (BOOKS.fiveStarRatings, BOOKS.fourStarRatings,
                   BOOKS.threeStarRatings, BOOKS.twoStarRatings, BOOKS.oneStarRatings)
ROLE_PROPERTIES = {"translator": BOOKS.isTranslatedBy, "illustrator": BOOKS.isIllustratedBy,
                   "editor": BOOKS.isEditedBy, "narrator": BOOKS.isNarratedBy}
# Goodreads role annotations (lower-cased, one per comma/slash/&/"and" token) -> ontology role.
# Anything not listed stays a generic hasEditionContributor and is reported in QA.
ROLE_ALIASES = {alias: role for role, aliases in {
    "author": ("author", "co-author", "coauthor", "co author", "co-writer", "writer", "creator",
               "created by", "series creator", "original creator", "original author",
               "original story", "original story by", "story", "text", "one of the authors",
               "pseudonym", "pseud", "pen name", "writing as", "heteronym", "تأليف", "著"),
    "translator": ("translator", "translation", "translated by", "trad", "traduction",
                   "traducteur", "traductor", "traducción", "tradutor", "tradutora", "tradução",
                   "traduttore", "übersetzer", "übersetzerin", "översättare", "çevirmen",
                   "penerjemah", "tõlkija", "ترجمة", "ترجمه", "مترجم", "تعريب"),
    "illustrator": ("illustrator", "illustrations", "illustration", "illustrated by",
                    "ilustrator", "ilustração", "ilustraciones", "ilustradora", "artist", "art",
                    "イラスト"),
    "editor": ("editor", "edited by", "co-editor", "anthologist", "compiler", "selected by"),
    "narrator": ("narrator", "narrated by", "reader", "reading", "read by"),
}.items() for alias in aliases}
ROLE_SPLIT = re.compile(r"\s*(?:[,/&]|\band\b)\s*")
AUDIO_FORMATS = {"audio", "audiobook", "audio book", "audio cd", "audio cassette",
                 "audible audio", "mp3 cd", "audio play"}
NONWIN = re.compile(r"\b(shortlist(?:ed)?|longlist(?:ed)?|runner[- ]up|honou?rable mention|semi[- ]?finalist|highly commended)\b", re.I)
MONTH_DATE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", re.I)


def clean(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def canonical(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", clean(value)).split()).casefold()


def slug(value: str) -> str:
    out = re.sub(r"[^\w]+", "-", canonical(value), flags=re.UNICODE).strip("-")
    return out[:65].rstrip("-") or "item"


def stable_uri(base: str, kind: str, key: str, label: str | None = None) -> URIRef:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return URIRef(f"{base}{kind}/{slug(label or key)}--{digest}")


def edition_uri(base: str, book_id: str) -> URIRef:
    numeric = re.match(r"^(\d+)", book_id)
    readable = "goodreads-" + numeric.group(1) if numeric else book_id
    return stable_uri(base, "edition", book_id, readable)


class QA:
    def __init__(self) -> None:
        self.records: dict[str, list[dict]] = {name: [] for name in CATEGORIES}
        self.counters: Counter[str] = Counter()

    def add(self, category: str, **details: object) -> None:
        self.records[category].append(details)

    def write(self, directory: Path, counts: dict[str, int]) -> dict:
        directory.mkdir(parents=True, exist_ok=True)
        for category, records in self.records.items():
            (directory / f"{category}.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary = {**counts, "issue_counts": {k: len(v) for k, v in self.records.items()},
                   "date_heuristics": self.counters["date_heuristics"]}
        (directory / "qa_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary


@dataclass
class Contributor:
    name: str
    roles: tuple[str, ...]


@dataclass
class ParsedRow:
    index: int
    book_id: str
    title: str
    work_key: str
    primary_author: str
    contributors: list[Contributor]
    values: dict[str, object]

    def priority(self) -> tuple:
        nr = self.values.get("numRatings")
        bv = self.values.get("bbeVotes")
        return (-(nr if isinstance(nr, int) else -1),
                -(bv if isinstance(bv, int) else -1), self.book_id,
                json.dumps(self.values, sort_keys=True, default=str, ensure_ascii=False))


def split_contributors(raw: str) -> list[str]:
    pieces, start, depth = [], 0, 0
    for i, char in enumerate(raw):
        if char == "(": depth += 1
        elif char == ")": depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            pieces.append(raw[start:i].strip())
            start = i + 1
    pieces.append(raw[start:].strip())
    return [p for p in pieces if p]


def parse_contributors(raw: str, book_id: str, qa: QA) -> list[Contributor]:
    result = []
    for piece in split_contributors(raw):
        annotations = re.findall(r"\(([^()]*)\)", piece)
        name = re.sub(r"\s*\([^()]*\)", "", piece).strip()
        if not name:
            qa.add("invalid_values", bookId=book_id, field="author", raw_value=piece,
                   reason="missing_contributor_name")
            continue
        roles = []
        for annotation in annotations:
            if canonical(annotation) == "goodreads author": continue
            for token in ROLE_SPLIT.split(canonical(annotation)):
                token = token.strip(" -.:")
                if not token: continue
                role = ROLE_ALIASES.get(token)
                if role: roles.append(role)
                else:
                    roles.append("unknown")
                    qa.add("unknown_contributor_roles", role=annotation, token=token,
                           bookId=book_id, person=name)
        result.append(Contributor(name, tuple(dict.fromkeys(roles or ["author"]))))
    return result


def parse_list(raw: str, field: str, book_id: str, qa: QA) -> list[str] | None:
    if not clean(raw): return []
    try:
        value = ast.literal_eval(raw)
        if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) for x in value):
            raise ValueError("expected a list of strings")
        return [clean(x) for x in value if clean(x)]
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as exc:
        qa.add("malformed_list_fields", bookId=book_id, field=field, raw_value=raw,
               error=str(exc)[:200])
        return None


def parse_int(raw: str, field: str, book_id: str, qa: QA) -> int | None:
    if not clean(raw): return None
    value = clean(raw).replace(",", "")
    if not re.fullmatch(r"\+?\d+", value):
        qa.add("invalid_values", bookId=book_id, field=field, raw_value=raw, reason="invalid_integer")
        return None
    return int(value)


def parse_decimal(raw: str, field: str, book_id: str, qa: QA, minimum: int | None = None,
                  maximum: int | None = None) -> Decimal | None:
    if not clean(raw): return None
    try:
        value = Decimal(clean(raw).replace(",", ""))
        if not value.is_finite() or (minimum is not None and value < minimum) or (
                maximum is not None and value > maximum):
            raise InvalidOperation
        return value
    except InvalidOperation:
        qa.add("invalid_values", bookId=book_id, field=field, raw_value=raw,
               reason="invalid_or_out_of_range_decimal")
        return None


def parse_date(raw: str, field: str, book_id: str, qa: QA, pivot: int) -> tuple[date | None, int | None]:
    """Return (full date, year). The year survives when only year or month/year is known."""
    value = clean(raw)
    if not value: return None, None
    match = MONTH_DATE.fullmatch(value)
    if match:
        value = f"{match.group(1)} {match.group(2)} {match.group(3)}"
    formats = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d %Y", "%b %d %Y")
    # Slash dates are interpreted month/day/year only, matching dataset examples.
    formats = tuple(f for f in formats if f != "%d/%m/%Y")
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            return parsed, parsed.year
        except ValueError: pass
    short = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2})", value)
    if short:
        if field == "firstPublishDate":
            qa.add("invalid_dates", bookId=book_id, field=field, raw_value=raw,
                   reason="ambiguous_century")
            return None, None
        month, day, yy = map(int, short.groups())
        year = 2000 + yy if yy <= pivot else 1900 + yy
        try:
            parsed = date(year, month, day)
            qa.counters["date_heuristics"] += 1
            return parsed, parsed.year
        except ValueError:
            qa.add("invalid_dates", bookId=book_id, field=field, raw_value=raw,
                   reason="invalid_calendar_date")
            return None, None
    if re.fullmatch(r"\d{4}|[A-Za-z]+ \d{4}", value):
        year = int(value[-4:])
        qa.add("invalid_dates", bookId=book_id, field=field, raw_value=raw,
               reason="insufficient_date_precision", kept_year=year if year > 0 else None)
        return None, year if year > 0 else None
    qa.add("invalid_dates", bookId=book_id, field=field, raw_value=raw, reason="unparseable")
    return None, None


def parse_isbn(raw: str, book_id: str, qa: QA) -> str | None:
    if not clean(raw): return None
    value = re.sub(r"[\s-]", "", clean(raw)).upper()
    reason = None
    if re.fullmatch(r"(?:9|0){10}|(?:9|0){13}", value): reason = "placeholder_value"
    elif re.fullmatch(r"[A-Z0-9]{10}", value) and not re.fullmatch(r"\d{9}[\dX]", value):
        reason = "non_isbn_identifier"
    elif len(value) not in (10, 13): reason = "invalid_length"
    elif len(value) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", value): reason = "non_isbn_identifier"
        elif sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(value)) % 11:
            reason = "invalid_checksum"
    elif not value.isdigit(): reason = "non_isbn_identifier"
    elif (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(value[:12])) % 10) % 10 != int(value[-1]):
        reason = "invalid_checksum"
    if reason:
        qa.add("invalid_isbn", bookId=book_id, raw_isbn=raw, reason=reason)
        return None
    return value


def parse_series(raw: str, book_id: str, qa: QA) -> tuple[str, str | None] | None:
    value = clean(raw)
    if not value: return None
    match = re.fullmatch(r"(.+?)\s*#\s*(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)", value)
    if match: return clean(match.group(1)), re.sub(r"\s+", "", match.group(2)).replace("–", "-")
    if "#" in value:
        safe = clean(value.split("#", 1)[0])
        qa.add("invalid_values", bookId=book_id, field="series", raw_value=raw,
               reason="unparsed_series_position")
        return (safe, None) if safe else None
    return value, None


def parse_award(raw: str, book_id: str, qa: QA) -> tuple[str, str, int] | None:
    value = clean(raw)
    match = re.fullmatch(r"(.+?)\s*\((\d{4})\)", value)
    if not match:
        qa.add("award_parse_issues", bookId=book_id, raw_award=raw, parsed_name=None,
               parsed_year=None, parsed_status=None, issue="missing_or_ambiguous_year")
        return None
    name, year_raw = match.groups()
    year = int(year_raw)
    if year < 1000 or year > 9999:
        qa.add("award_parse_issues", bookId=book_id, raw_award=raw, parsed_name=name,
               parsed_year=year, parsed_status=None, issue="invalid_year")
        return None
    if re.search(r"\bnominee\b", name, re.I):
        status = "nomination"
        name = re.sub(r"\s+Nominee\b", "", name, flags=re.I)
    elif NONWIN.search(name):
        status = "generic"
        name = NONWIN.sub("", name)
        name = re.sub(r"\(\s*\)", "", name)
        qa.add("award_parse_issues", bookId=book_id, raw_award=raw,
               parsed_name=clean(name), parsed_year=year, parsed_status=status,
               issue="unsupported_nonwin_status")
    elif re.search(r"\bfinalist\b", name, re.I):
        status = "finalist"
        name = re.sub(r"\s+Finalist\b", "", name, flags=re.I)
    else:
        status = "win"
        name = re.sub(r"\b(?:joint\s+)?winner\b", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -,")
    if not name:
        qa.add("award_parse_issues", bookId=book_id, raw_award=raw, parsed_name=None,
               parsed_year=year, parsed_status=status, issue="missing_award_name")
        return None
    return name, status, year


def parse_row(raw: dict, index: int, qa: QA, pivot: int) -> ParsedRow | None:
    book_id, title = clean(raw.get("bookId")), clean(raw.get("title"))
    if not book_id or not title:
        qa.add("skipped_rows", row=index, bookId=book_id, title=title,
               reason="missing_book_id" if not book_id else "missing_title")
        return None
    contributors = parse_contributors(clean(raw.get("author")), book_id, qa)
    if not contributors:
        qa.add("skipped_rows", row=index, bookId=book_id, title=title,
               reason="missing_contributors")
        return None
    authors = [c.name for c in contributors if "author" in c.roles]
    if not authors:
        # Anthologies, scriptures, adaptations: keep the book, group it by its first
        # credited contributor, and emit no isWrittenBy (unknown author under the OWA).
        qa.add("works_without_author", row=index, bookId=book_id, title=title,
               contributors=[[c.name, list(c.roles)] for c in contributors])
    primary = authors[0] if authors else contributors[0].name
    values: dict[str, object] = {}
    for field in LIST_FIELDS: values[field] = parse_list(clean(raw.get(field)), field, book_id, qa)
    values["series"] = parse_series(clean(raw.get("series")), book_id, qa)
    values["isbn"] = parse_isbn(clean(raw.get("isbn")), book_id, qa)
    for field in ("publishDate", "firstPublishDate"):
        values[field], values[field.replace("Date", "Year")] = parse_date(
            clean(raw.get(field)), field, book_id, qa, pivot)
    for field in ("pages", "numRatings", "bbeVotes"):
        values[field] = parse_int(clean(raw.get(field)), field, book_id, qa)
    for field in ("rating", "bbeScore"):
        values[field] = parse_decimal(clean(raw.get(field)), field, book_id, qa)
    values["likedPercent"] = parse_decimal(clean(raw.get("likedPercent")), "likedPercent", book_id, qa, 0, 100)
    for field in ("description", "language", "bookFormat", "edition", "publisher"):
        values[field] = clean(raw.get(field)) or None
    url = clean(raw.get("coverImg"))
    if url:
        parts = urlsplit(url)
        if parts.scheme.lower() in {"http", "https"} and parts.netloc and not re.search(r"\s", url):
            values["coverImg"] = url
        else:
            values["coverImg"] = None
            qa.add("invalid_values", bookId=book_id, field="coverImg", raw_value=url,
                   reason="invalid_absolute_url")
    else: values["coverImg"] = None
    stars = parse_list(clean(raw.get("ratingsByStars")), "ratingsByStars", book_id, qa)
    if stars is not None and len(stars) == 5 and all(re.fullmatch(r"\d+", x) for x in stars):
        values["ratingsByStars"] = tuple(int(x) for x in stars)
        nr = values["numRatings"]
        if nr is not None and sum(values["ratingsByStars"]) != nr:
            total = sum(values["ratingsByStars"])
            qa.add("rating_count_mismatch", bookId=book_id, numRatings=nr,
                   sumRatingsByStars=total, difference=total - nr,
                   differencePercent=round(100 * (total - nr) / nr, 3) if nr else None)
    else:
        values["ratingsByStars"] = None
        if stars is not None and clean(raw.get("ratingsByStars")):
            qa.add("invalid_values", bookId=book_id, field="ratingsByStars",
                   raw_value=raw.get("ratingsByStars"), reason="expected_five_nonnegative_integers")
    return ParsedRow(index, book_id, title, canonical(title) + "|" + canonical(primary),
                     primary, contributors, values)


class Emitter:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/") + "/"
        self.graph = Graph()
        self.graph.bind("books", BOOKS)
        self.graph.bind("resource", Namespace(self.base))
        self.graph.bind("rdfs", RDFS)
        self.graph.bind("xsd", XSD)
        self.entities: dict[str, dict[str, URIRef]] = defaultdict(dict)
        self.labels: dict[URIRef, str] = {}

    def entity(self, kind: str, key: str, label: str, rdf_type: URIRef) -> URIRef:
        if key not in self.entities[kind]:
            uri = stable_uri(self.base, kind, key, label)
            self.entities[kind][key] = uri
            self.graph.add((uri, RDF.type, rdf_type))
        uri = self.entities[kind][key]
        if uri not in self.labels or (canonical(label), label) < (canonical(self.labels[uri]), self.labels[uri]):
            self.labels[uri] = label
        return uri

    def finish(self) -> None:
        for uri, label in self.labels.items(): self.graph.add((uri, RDFS.label, Literal(label)))


def emit_literal(graph: Graph, subject: URIRef, prop: URIRef, value: object, datatype: URIRef | None = None) -> None:
    if value is not None and value != "":
        graph.add((subject, prop, Literal(value, datatype=datatype)))


def resolve_editions(rows: list[ParsedRow], qa: QA) -> dict[str, list[ParsedRow]]:
    grouped: dict[str, list[ParsedRow]] = defaultdict(list)
    for row in rows: grouped[row.book_id].append(row)
    result = {}
    for book_id, members in grouped.items():
        members.sort(key=ParsedRow.priority)
        chosen_work = members[0].work_key
        accepted = [r for r in members if r.work_key == chosen_work]
        rejected = [r for r in members if r.work_key != chosen_work]
        for row in rejected:
            qa.add("skipped_rows", row=row.index, bookId=row.book_id, title=row.title,
                   reason="duplicate_id_conflicting_work")
        if len(members) > 1:
            conflicts = [f for f in (*EDITION_FIELDS, *REP_FIELDS, "series", "author")
                         if len({str(r.values.get(f) if f != "author" else r.primary_author)
                                 for r in members if r.values.get(f) is not None or f == "author"}) > 1]
            if rejected: conflicts.append("work_key")
            qa.add("duplicate_book_ids", bookId=book_id,
                   row_indices=sorted(r.index for r in members), conflicting_fields=sorted(set(conflicts)))
        result[book_id] = accepted
    return result


def chosen_edition_values(rows: list[ParsedRow]) -> dict[str, object]:
    return {field: next((r.values[field] for r in rows if r.values.get(field) is not None), None)
            for field in EDITION_FIELDS}


def emit_edition(em: Emitter, book: URIRef, book_id: str, rows: list[ParsedRow]) -> None:
    g = em.graph
    edition = edition_uri(em.base, book_id)
    g.add((edition, RDF.type, BOOKS.BookEdition))
    g.add((book, BOOKS.hasEdition, edition))
    emit_literal(g, edition, BOOKS.bookId, book_id)
    values = chosen_edition_values(rows)
    for field, prop, dtype in (("isbn", BOOKS.isbn, None), ("edition", BOOKS.editionName, None),
                               ("pages", BOOKS.pageCount, XSD.nonNegativeInteger),
                               ("publishDate", BOOKS.publishDate, XSD.date),
                               ("coverImg", BOOKS.coverImage, XSD.anyURI)):
        value = values[field]
        if isinstance(value, date): value = value.isoformat()
        emit_literal(g, edition, prop, value, dtype)
    year = values["publishDate"].year if values["publishDate"] else values["publishYear"]
    if year: emit_literal(g, edition, BOOKS.publishYear, f"{year:04d}", XSD.gYear)
    for field, kind, typ, prop in (("publisher", "publisher", BOOKS.Publisher, BOOKS.isPublishedBy),
                                   ("language", "language", BOOKS.Language, BOOKS.isInLanguage),
                                   ("bookFormat", "format", BOOKS.BookFormat, BOOKS.hasFormat)):
        value = values[field]
        if value:
            resource = em.entity(kind, canonical(value), value, typ)
            if field == "bookFormat" and canonical(value) in AUDIO_FORMATS:
                g.add((resource, RDF.type, BOOKS.AudioFormat))
            g.add((edition, prop, resource))
    for row in rows:
        for contributor in row.contributors:
            person = em.entity("person", canonical(contributor.name), contributor.name, BOOKS.Person)
            for role in contributor.roles:
                if role == "author": continue
                g.add((edition, ROLE_PROPERTIES.get(role, BOOKS.hasEditionContributor), person))


def emit_work(em: Emitter, key: str, rows: list[ParsedRow], editions: dict[str, list[ParsedRow]], qa: QA) -> None:
    rows.sort(key=ParsedRow.priority)
    rep = rows[0]
    g = em.graph
    book = em.entity("book", key, rep.title, BOOKS.Book)
    for field, prop, dtype in (("title", BOOKS.title, None), ("description", BOOKS.description, None),
                               ("rating", BOOKS.rating, XSD.decimal),
                               ("numRatings", BOOKS.numRatings, XSD.nonNegativeInteger),
                               ("likedPercent", BOOKS.likedPercent, XSD.decimal),
                               ("bbeScore", BOOKS.bbeScore, XSD.decimal),
                               ("bbeVotes", BOOKS.bbeVotes, XSD.nonNegativeInteger)):
        value = rep.title if field == "title" else rep.values.get(field)
        emit_literal(g, book, prop, value, dtype)
        observed = sorted({str(r.title if field == "title" else r.values.get(field))
                           for r in rows if (r.title if field == "title" else r.values.get(field)) is not None})
        if len(observed) > 1:
            qa.add("conflicting_work_values", work_uri=str(book), property=field,
                   observed_values=observed, chosen_value=str(value), chosen_bookId=rep.book_id)
    stars = rep.values.get("ratingsByStars")
    if stars:
        for prop, value in zip(STAR_PROPERTIES, stars):
            emit_literal(g, book, prop, value, XSD.nonNegativeInteger)
    star_values = {str(r.values["ratingsByStars"]) for r in rows if r.values.get("ratingsByStars") is not None}
    if len(star_values) > 1:
        qa.add("conflicting_work_values", work_uri=str(book), property="ratingsByStars",
               observed_values=sorted(star_values), chosen_value=str(stars), chosen_bookId=rep.book_id)
    first_dates = [r.values["firstPublishDate"] for r in rows if r.values["firstPublishDate"] is not None]
    if first_dates: emit_literal(g, book, BOOKS.firstPublishDate, min(first_dates).isoformat(), XSD.date)
    first_years = [r.values["firstPublishYear"] for r in rows if r.values["firstPublishYear"]]
    if first_years: emit_literal(g, book, BOOKS.firstPublishYear, f"{min(first_years):04d}", XSD.gYear)
    series = {canonical(r.values["series"][0]): r.values["series"][0]
              for r in rows if r.values["series"] and r.values["series"][0]}
    positions = {r.values["series"][1] for r in rows if r.values["series"] and r.values["series"][1]}
    for series_key, name in sorted(series.items()):
        resource = em.entity("series", series_key, name, BOOKS.BookSeries)
        g.add((book, BOOKS.isPartOfSeries, resource))
    if len(series) == 1 and len(positions) == 1:
        emit_literal(g, book, BOOKS.seriesPosition, next(iter(positions)), XSD.string)
    if len(positions) > 1 or len(series) > 1:
        qa.add("ambiguous_work_groups", work_key=key, row_count=len(rows),
               book_ids=sorted({r.book_id for r in rows}), titles=sorted({r.title for r in rows}),
               primary_authors=sorted({r.primary_author for r in rows}),
               warning="conflicting_series_position_or_membership")
    if len(rows) > 12:
        qa.add("ambiguous_work_groups", work_key=key, row_count=len(rows),
               book_ids=sorted({r.book_id for r in rows}), titles=sorted({r.title for r in rows}),
               primary_authors=sorted({r.primary_author for r in rows}), warning="many_editions")
    for row in rows:
        for contributor in row.contributors:
            if "author" in contributor.roles:
                person = em.entity("person", canonical(contributor.name), contributor.name, BOOKS.Person)
                g.add((book, BOOKS.isWrittenBy, person))
        for field, kind, typ, prop in (("genres", "category", BOOKS.BookCategory, BOOKS.hasCategory),
                                       ("setting", "place", BOOKS.Place, BOOKS.setIn)):
            for label in row.values[field] or []:
                resource = em.entity(kind, canonical(label), label, typ)
                g.add((book, prop, resource))
        for label in row.values["characters"] or []:
            identity = (next(iter(series)) if len(series) == 1 else key) + "|" + canonical(label)
            resource = em.entity("character", identity, label, BOOKS.Character)
            g.add((book, BOOKS.hasCharacter, resource))
        for award_raw in row.values["awards"] or []:
            award = parse_award(award_raw, row.book_id, qa)
            if not award: continue
            name, status, year = award
            award_key = canonical(name)
            award_uri = em.entity("award", award_key, name, BOOKS.Award)
            recognition_key = f"{key}|{award_key}|{year}|{status}"
            rec_type = {"win": BOOKS.AwardWin, "nomination": BOOKS.AwardNomination,
                        "finalist": BOOKS.AwardFinalistRecognition, "generic": BOOKS.AwardRecognition}[status]
            recognition = em.entity("recognition", recognition_key, f"{name} ({year}; {status})", rec_type)
            g.add((book, BOOKS.hasAwardRecognition, recognition))
            g.add((recognition, BOOKS.forAward, award_uri))
            emit_literal(g, recognition, BOOKS.awardYear, str(year), XSD.gYear)
    for book_id in sorted({r.book_id for r in rows}):
        emit_edition(em, book, book_id, editions[book_id])


def validate_vocabulary(data: Graph, ontology: Graph) -> None:
    predicates = {p for _, p, _ in data if str(p).startswith(str(BOOKS))}
    declared = {s for s, _, _ in ontology.triples((None, RDF.type, OWL.ObjectProperty))} | {
        s for s, _, _ in ontology.triples((None, RDF.type, OWL.DatatypeProperty))}
    unknown = predicates - declared
    if unknown: raise ValueError(f"Undeclared local predicates: {sorted(map(str, unknown))}")
    classes = {s for s in ontology.subjects(RDF.type, OWL.Class)}
    unknown_types = {t for t in data.objects(None, RDF.type)
                     if str(t).startswith(str(BOOKS)) and t not in classes}
    if unknown_types: raise ValueError(f"Undeclared local classes: {sorted(map(str, unknown_types))}")


def select_top_ids(input_csv: Path, top_n: int) -> set[str] | None:
    """bookIds of the top_n most-rated editions (ties: smaller bookId); None keeps every row."""
    if top_n <= 0: return None
    ratings: dict[str, int] = {}
    with input_csv.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            book_id = clean(raw.get("bookId"))
            count = clean(raw.get("numRatings")).replace(",", "")
            if book_id:
                ratings[book_id] = max(ratings.get(book_id, -1), int(count) if count.isdigit() else -1)
    return set(sorted(ratings, key=lambda b: (-ratings[b], b))[:top_n])


def run(input_csv: Path, ontology_path: Path, output_ttl: Path, qa_dir: Path,
        base: str = DEFAULT_BASE, pivot: int = 26, top_n: int = 0) -> tuple[Graph, dict]:
    ontology = Graph().parse(ontology_path, format="turtle")
    qa = QA()
    rows = []
    input_count = selected_count = 0
    selected = select_top_ids(input_csv, top_n)
    with input_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"bookId", "title", "author"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing required CSV columns: {sorted(required - set(reader.fieldnames or []))}")
        for index, raw in enumerate(reader, start=2):
            input_count += 1
            if selected is not None and clean(raw.get("bookId")) not in selected: continue
            selected_count += 1
            try:
                parsed = parse_row(raw, index, qa, pivot)
                if parsed: rows.append(parsed)
            except Exception as exc:
                qa.add("skipped_rows", row=index, bookId=clean(raw.get("bookId")),
                       title=clean(raw.get("title")), reason="fatal_parse_error", error=str(exc)[:300])
    editions = resolve_editions(rows, qa)
    works: dict[str, list[ParsedRow]] = defaultdict(list)
    for members in editions.values(): works[members[0].work_key].extend(members)
    emitter = Emitter(base)
    for key in sorted(works): emit_work(emitter, key, works[key], editions, qa)
    emitter.finish()
    validate_vocabulary(emitter.graph, ontology)
    output_ttl.parent.mkdir(parents=True, exist_ok=True)
    emitter.graph.serialize(destination=output_ttl, format="turtle")
    counts = {"input_rows": input_count, "top_n": top_n, "selected_rows": selected_count,
              "parsed_rows": selected_count - len(qa.records["skipped_rows"]),
              "skipped_rows": len(qa.records["skipped_rows"]),
              "triple_count": len(emitter.graph), "Book": len(works),
              "BookEdition": len(editions)}
    for kind, name in (("person", "Person"), ("series", "BookSeries"),
                       ("category", "BookCategory"), ("character", "Character"),
                       ("publisher", "Publisher"), ("language", "Language"),
                       ("format", "BookFormat"), ("place", "Place"),
                       ("award", "Award"), ("recognition", "AwardRecognition")):
        counts[name] = len(emitter.entities[kind])
    summary = qa.write(qa_dir, counts)
    return emitter.graph, summary


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "books_1.Best_Books_Ever.csv")
    parser.add_argument("--ontology", type=Path, default=root / "ontology.ttl")
    parser.add_argument("--output", type=Path, default=root / "pipeline/output/books_data.ttl")
    parser.add_argument("--qa-dir", type=Path, default=root / "pipeline/output/qa")
    parser.add_argument("--resource-base", default=DEFAULT_BASE)
    parser.add_argument("--two-digit-year-pivot", type=int, default=26,
                        help="Fixed pivot for edition publishDate (default: 26, the 2026 handover year)")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Keep the N most-rated bookIds (default: 10000; 0 keeps all rows)")
    args = parser.parse_args()
    if not 0 <= args.two_digit_year_pivot <= 99: parser.error("pivot must be 0..99")
    if args.top_n < 0: parser.error("--top-n must be >= 0")
    _, summary = run(args.input, args.ontology, args.output, args.qa_dir,
                     args.resource_base, args.two_digit_year_pivot, args.top_n)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__": main()
