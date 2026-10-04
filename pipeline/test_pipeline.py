"""Acceptance tests derived from Books_RDF_Pipeline_Codex_Handover.md §20."""

import csv
import json
from pathlib import Path
import random
import sys

import pytest
from owlrl import DeductiveClosure, OWLRL_Semantics
from rdflib import Graph, Literal
from rdflib.namespace import OWL, RDF, RDFS, XSD

sys.path.insert(0, str(Path(__file__).resolve().parent))
from books_pipeline import BOOKS, run, validate_vocabulary


ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("bookId", "title", "series", "author", "rating", "description", "language",
          "isbn", "genres", "characters", "bookFormat", "edition", "pages", "publisher",
          "publishDate", "firstPublishDate", "awards", "numRatings", "ratingsByStars",
          "likedPercent", "setting", "coverImg", "bbeScore", "bbeVotes", "price")


def row(book_id="100", title="Example Book", author="Alice Smith", **values):
    result = dict.fromkeys(FIELDS, "")
    result.update(bookId=book_id, title=title, author=author)
    result.update(values)
    return result


def build(tmp_path, rows):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "fixture.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    output = tmp_path / "data.ttl"
    graph, summary = run(source, ROOT / "ontology.ttl", output, tmp_path / "qa")
    reparsed = Graph().parse(output, format="turtle")
    assert set(graph) == set(reparsed)
    return graph, summary, tmp_path / "qa"


def subjects(graph, typ):
    return set(graph.subjects(RDF.type, typ))


def only(items):
    assert len(items) == 1
    return next(iter(items))


def issue(qa_dir, category):
    return json.loads((qa_dir / f"{category}.json").read_text(encoding="utf-8"))


def test_valid_turtle_book_and_edition(tmp_path):
    g, summary, _ = build(tmp_path, [row(isbn="9780439023481", pages="374", rating="4.33")])
    book, edition = only(subjects(g, BOOKS.Book)), only(subjects(g, BOOKS.BookEdition))
    assert (book, BOOKS.hasEdition, edition) in g
    assert (edition, BOOKS.pageCount, Literal(374, datatype=XSD.nonNegativeInteger)) in g
    assert not list(g.objects(book, BOOKS.pageCount))
    assert (book, BOOKS.rating, Literal("4.33", datatype=XSD.decimal)) in g
    assert summary["triple_count"] == len(g)


def test_two_editions_one_book_and_different_author(tmp_path):
    g, _, _ = build(tmp_path, [row("100"), row("200"), row("300", author="Bob")])
    assert len(subjects(g, BOOKS.Book)) == 2
    assert len(subjects(g, BOOKS.BookEdition)) == 3
    assert sorted(len(list(g.objects(book, BOOKS.hasEdition))) for book in subjects(g, BOOKS.Book)) == [1, 2]


def test_deterministic_uris_and_graph(tmp_path):
    rows = [row("100", genres="['Fantasy']"), row("200", genres="['Classics']"),
            row("300", title="Other", author="Bob")]
    g1, _, _ = build(tmp_path / "a", rows)
    g2, _, _ = build(tmp_path / "b", rows)
    random.Random(42).shuffle(rows)
    g3, _, _ = build(tmp_path / "c", rows)
    assert set(g1) == set(g2) == set(g3)


def test_person_normalization_without_fuzzy_merging(tmp_path):
    g, _, _ = build(tmp_path, [row("1", author="Richard Adams"),
                              row("2", author=" Richard   Adams "),
                              row("3", author="RICHARD ADAMS"),
                              row("4", title="Murakami A", author="Haruki Murakami"),
                              row("5", title="Murakami B", author="村上 春樹")])
    assert len(subjects(g, BOOKS.Person)) == 3
    assert len(subjects(g, BOOKS.Book)) == 3


def test_contributor_roles_goodreads_and_unknown(tmp_path):
    text = ("Suzanne Collins, John Doe (Translator), Jane Doe (Illustrator), "
            "Alan Doe (Editor), Bob Doe (Narrator), "
            "Person X (Goodreads Author) (Translator), Jane Smith (Introduction)")
    g, _, qa = build(tmp_path, [row(author=text)])
    book, edition = only(subjects(g, BOOKS.Book)), only(subjects(g, BOOKS.BookEdition))
    people = {str(next(g.objects(p, RDFS.label))): p for p in subjects(g, BOOKS.Person)}
    assert (book, BOOKS.isWrittenBy, people["Suzanne Collins"]) in g
    for name, prop in (("John Doe", BOOKS.isTranslatedBy), ("Jane Doe", BOOKS.isIllustratedBy),
                       ("Alan Doe", BOOKS.isEditedBy), ("Bob Doe", BOOKS.isNarratedBy),
                       ("Person X", BOOKS.isTranslatedBy),
                       ("Jane Smith", BOOKS.hasEditionContributor)):
        assert (edition, prop, people[name]) in g
    assert (book, BOOKS.isWrittenBy, people["Person X"]) not in g
    assert (edition, BOOKS.isEditedBy, people["Jane Smith"]) not in g
    assert issue(qa, "unknown_contributor_roles")[0]["role"] == "Introduction"


def test_category_dedup_type_isolation_and_series(tmp_path):
    g, _, _ = build(tmp_path, [row("1", genres="['Fantasy', 'Audiobook']",
                                  bookFormat="Audiobook", series="The Hunger Games #1-3"),
                              row("2", title="Other", genres="['Fantasy']")])
    categories = subjects(g, BOOKS.BookCategory)
    assert len(categories) == 2
    audiobook_category = only({c for c in categories if (c, RDFS.label, Literal("Audiobook")) in g})
    audio_format = only(subjects(g, BOOKS.AudioFormat))
    assert audiobook_category != audio_format
    assert (audio_format, RDF.type, BOOKS.BookFormat) in g
    series = only(subjects(g, BOOKS.BookSeries))
    assert (series, RDFS.label, Literal("The Hunger Games")) in g
    book = only(set(g.subjects(BOOKS.seriesPosition, Literal("1-3", datatype=XSD.string))))
    assert (book, BOOKS.isPartOfSeries, series) in g


@pytest.mark.parametrize("author,authors,others", [
    ("Jane Carruth (Adapted By), Lewis Carroll (Original Story By), Rene Cloke (Illustrator)",
     {"Lewis Carroll"}, {("Rene Cloke", "isIllustratedBy"), ("Jane Carruth", "hasEditionContributor")}),
    ("Brian K. Vaughan (Goodreads Author) (Writer), Fiona Staples (Artist)",
     {"Brian K. Vaughan"}, {("Fiona Staples", "isIllustratedBy")}),
    ("P.C. Cast (Goodreads Author) (co-author), Kristin Cast (Goodreads Author) (co-author)",
     {"P.C. Cast", "Kristin Cast"}, set()),
    ("Ann Author, Tom Trans (Translator, Introduction), Ed Itor (Editor/Translator)",
     {"Ann Author"}, {("Tom Trans", "isTranslatedBy"), ("Tom Trans", "hasEditionContributor"),
                      ("Ed Itor", "isEditedBy"), ("Ed Itor", "isTranslatedBy")}),
    ("Kim Writer (Writer, Artist)", {"Kim Writer"}, {("Kim Writer", "isIllustratedBy")}),
])
def test_author_role_aliases_and_combined_roles(tmp_path, author, authors, others):
    g, summary, _ = build(tmp_path, [row(author=author)])
    assert summary["skipped_rows"] == 0
    book, edition = only(subjects(g, BOOKS.Book)), only(subjects(g, BOOKS.BookEdition))
    label = lambda node: str(g.value(node, RDFS.label))
    assert {label(p) for p in g.objects(book, BOOKS.isWrittenBy)} == authors
    found = {(label(o), str(p).rsplit("#", 1)[1]) for p, o in g.predicate_objects(edition)
             if (o, RDF.type, BOOKS.Person) in g}
    assert found == others


def test_edition_without_author_is_kept_and_reported(tmp_path):
    g, summary, qa = build(tmp_path, [row("1", title="Best Stories", author="Ed Itor (Editor)"),
                                     row("2", title="Scripture", author="J. Smith (Translator)")])
    assert summary["skipped_rows"] == 0
    assert len(subjects(g, BOOKS.Book)) == 2
    assert not list(g.triples((None, BOOKS.isWrittenBy, None)))
    assert {r["bookId"] for r in issue(qa, "works_without_author")} == {"1", "2"}


def test_year_kept_when_full_date_unknown(tmp_path):
    g, _, _ = build(tmp_path, [row("1", publishDate="2004", firstPublishDate="March 1999"),
                              row("2", title="Full", publishDate="September 14th 2008",
                                  firstPublishDate="09/14/08"),
                              row("3", title="Old", firstPublishDate="1813", publishDate="May 2003")])
    years = lambda title: {
        "edition": [str(y) for e in g.objects(book(title), BOOKS.hasEdition)
                    for y in g.objects(e, BOOKS.publishYear)],
        "first": [str(y) for y in g.objects(book(title), BOOKS.firstPublishYear)]}
    book = lambda title: only(set(g.subjects(BOOKS.title, Literal(title))))
    assert years("Example Book") == {"edition": ["2004"], "first": ["1999"]}
    assert years("Full") == {"edition": ["2008"], "first": []}  # two-digit first date: century unknown
    assert years("Old") == {"edition": ["2003"], "first": ["1813"]}
    assert not list(g.triples((None, BOOKS.firstPublishDate, None)))
    edition = only(set(g.objects(book("Full"), BOOKS.hasEdition)))
    assert (edition, BOOKS.publishDate, Literal("2008-09-14", datatype=XSD.date)) in g
    assert all(y.datatype == XSD.gYear for y in g.objects(None, BOOKS.publishYear))


def test_character_scoped_to_series_or_work(tmp_path):
    g, _, _ = build(tmp_path, [row("1", title="S One", series="Saga #1", characters="['John']"),
                              row("2", title="S Two", series="Saga #2", characters="['John']"),
                              row("3", title="U One", characters="['John']"),
                              row("4", title="U Two", characters="['John']")])
    books = {str(next(g.objects(b, BOOKS.title))): b for b in subjects(g, BOOKS.Book)}
    get = lambda title: only(set(g.objects(books[title], BOOKS.hasCharacter)))
    assert get("S One") == get("S Two")
    assert len({get("S One"), get("U One"), get("U Two")}) == 3


def test_representative_priority_and_union(tmp_path):
    rows = [row("3", numRatings="100", bbeVotes="9", description="low",
                genres="['Fantasy', 'Classics']", characters="['Amy']", setting="['North, West']",
                series="Saga #1", awards="['Pulitzer Prize for Fiction (1961)']"),
            row("2", numRatings="500", bbeVotes="1", description="winner",
                genres="['Fantasy', 'Adventure']", characters="['Bob']", setting="['South']",
                series="Saga #1", awards="['Locus Award Nominee for Best Novel (2000)']"),
            row("1", numRatings="300", bbeVotes="100", description="middle")]
    g, _, _ = build(tmp_path / "a", rows)
    book = only(subjects(g, BOOKS.Book))
    assert (book, BOOKS.description, Literal("winner")) in g
    assert len(set(g.objects(book, BOOKS.hasCategory))) == 3
    assert len(set(g.objects(book, BOOKS.hasCharacter))) == 2
    assert len(set(g.objects(book, BOOKS.setIn))) == 2
    assert len(set(g.objects(book, BOOKS.hasAwardRecognition))) == 2
    assert len(set(g.objects(book, BOOKS.isPartOfSeries))) == 1
    g2, _, _ = build(tmp_path / "b", [row("2", bbeVotes="7", numRatings="500", description="votes"),
                                     row("1", bbeVotes="5", numRatings="500", description="not chosen")])
    assert (only(subjects(g2, BOOKS.Book)), BOOKS.description, Literal("votes")) in g2
    g3, _, _ = build(tmp_path / "c", [row("2", bbeVotes="7", numRatings="500", description="later"),
                                     row("1", bbeVotes="7", numRatings="500", description="earlier")])
    assert (only(subjects(g3, BOOKS.Book)), BOOKS.description, Literal("earlier")) in g3


def test_authors_and_series_union_across_editions(tmp_path):
    g, _, qa = build(tmp_path, [row("1", author="Alice Smith, Carol", series="Saga #1"),
                               row("2", author="Alice Smith, Dave", series="Companion #2")])
    book = only(subjects(g, BOOKS.Book))
    assert len(set(g.objects(book, BOOKS.isWrittenBy))) == 3
    assert len(set(g.objects(book, BOOKS.isPartOfSeries))) == 2
    assert not list(g.objects(book, BOOKS.seriesPosition))
    assert issue(qa, "ambiguous_work_groups")


def test_first_date_invalid_isbn_comma_list_ratings(tmp_path):
    g, _, qa = build(tmp_path, [row("1", firstPublishDate="2001-01-01", isbn="9999999999999",
                                   setting="['Watership Down, Hampshire (United Kingdom)']",
                                   numRatings="225", ratingsByStars="['100', '80', '30', '10', '5']"),
                               row("2", firstPublishDate="1999-01-01"),
                               row("3", firstPublishDate="bad", ratingsByStars="['1', '2']")])
    book = only(subjects(g, BOOKS.Book))
    assert (book, BOOKS.firstPublishDate, Literal("1999-01-01", datatype=XSD.date)) in g
    assert len(set(g.objects(book, BOOKS.setIn))) == 1
    assert not list(g.triples((None, BOOKS.isbn, None)))
    assert issue(qa, "invalid_isbn") and issue(qa, "invalid_dates")
    assert issue(qa, "invalid_values")
    for prop, n in zip((BOOKS.fiveStarRatings, BOOKS.fourStarRatings, BOOKS.threeStarRatings,
                        BOOKS.twoStarRatings, BOOKS.oneStarRatings), (100, 80, 30, 10, 5)):
        # Representative row is 1 because it alone has a valid numRatings.
        assert (book, prop, Literal(n, datatype=XSD.nonNegativeInteger)) in g


def test_ambiguous_first_date_and_bad_star_distribution(tmp_path):
    g, _, qa = build(tmp_path, [row(firstPublishDate="07/11/60", publishDate="09/14/08",
                                   numRatings="10", ratingsByStars="['1', '2']")])
    book = only(subjects(g, BOOKS.Book))
    edition = only(subjects(g, BOOKS.BookEdition))
    assert not list(g.objects(book, BOOKS.firstPublishDate))
    assert (edition, BOOKS.publishDate, Literal("2008-09-14", datatype=XSD.date)) in g
    assert not any(list(g.objects(book, prop)) for prop in (
        BOOKS.fiveStarRatings, BOOKS.fourStarRatings, BOOKS.threeStarRatings,
        BOOKS.twoStarRatings, BOOKS.oneStarRatings))
    assert issue(qa, "invalid_dates")[0]["reason"] == "ambiguous_century"
    assert any(x["field"] == "ratingsByStars" for x in issue(qa, "invalid_values"))


def test_duplicate_edition_compatible_merge_and_series_conflict(tmp_path):
    g, _, qa = build(tmp_path, [row("1", series="Saga #1", isbn="9780439023481"),
                               row("1", series="Saga #2", pages="374")])
    book, edition = only(subjects(g, BOOKS.Book)), only(subjects(g, BOOKS.BookEdition))
    assert (edition, BOOKS.isbn, Literal("9780439023481")) in g
    assert (edition, BOOKS.pageCount, Literal(374, datatype=XSD.nonNegativeInteger)) in g
    assert not list(g.objects(book, BOOKS.seriesPosition))
    assert len(issue(qa, "duplicate_book_ids")) == 1
    assert issue(qa, "ambiguous_work_groups")


@pytest.mark.parametrize("award,expected", [
    ("Locus Award Nominee for Best Novel (2000)", BOOKS.AwardNomination),
    ("National Book Award Finalist for Fiction (1961)", BOOKS.AwardFinalistRecognition),
    ("Pulitzer Prize for Fiction (1961)", BOOKS.AwardWin),
    ("Example Award Shortlist for Fiction (2020)", BOOKS.AwardRecognition),
    ("Example Award (Semi-Finalist) (2020)", BOOKS.AwardRecognition),
    ("Example Award Joint Winner (2020)", BOOKS.AwardWin),
])
def test_award_status(tmp_path, award, expected):
    g, _, qa = build(tmp_path, [row(awards=repr([award]))])
    recognition = only(set(g.objects(only(subjects(g, BOOKS.Book)), BOOKS.hasAwardRecognition)))
    assert (recognition, RDF.type, expected) in g
    if expected == BOOKS.AwardRecognition: assert issue(qa, "award_parse_issues")
    else: assert ((recognition, RDF.type, BOOKS.AwardWin) in g) == (expected == BOOKS.AwardWin)


def test_invalid_data_and_duplicate_id_do_not_crash(tmp_path):
    g, summary, qa = build(tmp_path, [row("1", publishDate="nonsense", isbn="B001UFP6JY",
                                        price="-3", genres="['broken'", author="Alice, Jane (Foreword)"),
                                    row("1", title="Other", author="Bob"),
                                    row("2", title="Valid", author="Carol")])
    assert len(subjects(g, BOOKS.BookEdition)) == 2
    assert summary["skipped_rows"] == 1
    for category in ("invalid_dates", "invalid_isbn", "invalid_values",
                     "malformed_list_fields", "unknown_contributor_roles", "duplicate_book_ids"):
        assert issue(qa, category)


def test_vocabulary_and_domain_sanity(tmp_path):
    g, _, _ = build(tmp_path, [row(isbn="9780439023481", pages="374", publisher="Press",
                                   bookFormat="Hardcover", rating="4.0", numRatings="1",
                                   genres="['Fiction']", characters="['John']", setting="['London']")])
    ontology = Graph().parse(ROOT / "ontology.ttl", format="turtle")
    validate_vocabulary(g, ontology)
    for prop in (BOOKS.isbn, BOOKS.pageCount, BOOKS.isPublishedBy, BOOKS.hasFormat):
        assert all((s, RDF.type, BOOKS.BookEdition) in g for s in g.subjects(prop, None))
    for prop in (BOOKS.rating, BOOKS.numRatings, BOOKS.hasCategory, BOOKS.hasCharacter, BOOKS.setIn):
        assert all((s, RDF.type, BOOKS.Book) in g for s in g.subjects(prop, None))
    assert not (subjects(g, BOOKS.Book) & subjects(g, BOOKS.BookEdition))
    for book in subjects(g, BOOKS.Book):
        assert list(g.objects(book, BOOKS.isWrittenBy)) and list(g.objects(book, BOOKS.hasEdition))
    for edition in subjects(g, BOOKS.BookEdition):
        assert len(set(g.subjects(BOOKS.hasEdition, edition))) == 1


def test_reasoning_integration(tmp_path):
    g, _, _ = build(tmp_path, [row(author="Alice, Bob (Translator), Ivy (Illustrator), Ed (Editor), "
                                          "Nat (Narrator)", series="Saga #1", bookFormat="Audio CD",
                                   awards="['Pulitzer Prize for Fiction (1961)']")])
    book, edition = only(subjects(g, BOOKS.Book)), only(subjects(g, BOOKS.BookEdition))
    people = {str(next(g.objects(p, RDFS.label))): p for p in subjects(g, BOOKS.Person)}
    combined = Graph()
    combined += Graph().parse(ROOT / "ontology.ttl", format="turtle")
    combined += g
    DeductiveClosure(OWLRL_Semantics).expand(combined)
    assert (people["Alice"], RDF.type, BOOKS.Author) in combined
    assert (book, BOOKS.hasContributor, people["Alice"]) in combined
    assert (people["Alice"], BOOKS.writes, book) in combined
    assert (people["Bob"], RDF.type, BOOKS.Translator) in combined
    assert (edition, RDF.type, BOOKS.TranslatedEdition) in combined
    assert (edition, BOOKS.hasEditionContributor, people["Bob"]) in combined
    assert (people["Bob"], BOOKS.translates, edition) in combined
    assert (book, RDF.type, BOOKS.SeriesBook) in combined
    assert (edition, RDF.type, BOOKS.AudiobookEdition) in combined
    assert (book, RDF.type, BOOKS.AwardWinningBook) in combined
    for name, role in (("Ivy", BOOKS.Illustrator), ("Ed", BOOKS.Editor), ("Nat", BOOKS.Narrator)):
        assert (people[name], RDF.type, role) not in g  # derived by the reasoner, not asserted
        assert (people[name], RDF.type, role) in combined
    assert (people["Bob"], RDF.type, BOOKS.Translator) not in g
    recognition = only(set(g.objects(book, BOOKS.hasAwardRecognition)))
    assert (recognition, BOOKS.isRecognitionFor, book) not in g
    assert (recognition, BOOKS.isRecognitionFor, book) in combined
