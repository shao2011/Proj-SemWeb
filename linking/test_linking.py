"""Offline tests for the linking rules (no network)."""

from collections import Counter
import sys
from pathlib import Path

import pytest
from rdflib import URIRef

sys.path.insert(0, str(Path(__file__).resolve().parent))
from link_books import (WD, BookRecord, Item, category, choose_work, dbpedia_uri, entity,  # noqa: E402
                        gyear, link_editions, one_to_one, same_name, title_variants, titles_compatible,
                        wikidata_year)

EDITION, NOVEL, SERIES, FILM = WD + "Q3331189", WD + "Q7725634", WD + "Q1667921", WD + "Q11424"
MUSICAL, SERIALIZED = WD + "Q7777570", WD + "Q1347298"
CLASSES = {EDITION: (False, False, True, False), NOVEL: (True, False, False, False),
           SERIES: (True, True, False, False), FILM: (False, False, False, True),
           MUSICAL: (False, False, False, True), SERIALIZED: (True, True, False, False)}


def book(title, *authors, ratings=100):
    return BookRecord(URIRef("urn:book:" + title), title, ratings, None, [],
                      [(URIRef("urn:person:" + a), a) for a in authors], [], [])


def item(label, cls, authors=(), sitelinks=0, works=(), description=None):
    it = Item(labels={label}, classes={cls}, sitelinks=sitelinks, works=set(works), description=description)
    for i, name in enumerate(authors): it.authors[WD + f"A{i}"].add(name)
    return it


@pytest.mark.parametrize("ours,theirs,expected", [
    ("J.K. Rowling", "Joanne Rowling", True),          # initial vs full given name
    ("J.R.R. Tolkien", "J. R. R. Tolkien", True),
    ("Antoine de Saint-Exupéry", "Antoine de Saint-Exupery", True),
    ("Gabriel García Márquez", "Gabriel Garcia Marquez", True),
    ("Homer", "Homer", True),
    ("Jack Thorne", "John Thorne", False),             # same surname, different person
    ("Rowling", "J.K. Rowling", False),                # surname alone is not enough
    ("Stephen King", "Stephen Fry", False),
])
def test_same_name(ours, theirs, expected):
    assert same_name(ours, theirs) is expected


def test_title_variants_drop_subtitles_but_not_parts():
    assert title_variants("Getting Things Done: The Art of Stress-Free Productivity")[-1] == "Getting Things Done"
    assert title_variants("Deathless (Leningrad Diptych, #1)")[-1] == "Deathless"
    assert title_variants("Twilight: The Graphic Novel, Vol. 1") == ["Twilight: The Graphic Novel, Vol. 1"]
    assert title_variants("Harry Potter and the Order of the Phoenix (Harry Potter, #5, Part 1)") == [
        "Harry Potter and the Order of the Phoenix (Harry Potter, #5, Part 1)"]


def test_goodreads_id_wins_and_editions_resolve_to_their_work():
    items = {WD + "E1": item("Dune", EDITION, works=[WD + "W1"]), WD + "W1": item("Dune", NOVEL, ["Frank Herbert"]),
             WD + "W2": item("Dune", NOVEL, ["Frank Herbert"], sitelinks=50)}
    assert choose_work(book("Dune", "Frank Herbert"), [WD + "E1"], {WD + "W2"}, items, CLASSES) == (
        WD + "W1", "goodreads-id")


def test_title_match_requires_author_and_rejects_films_and_series():
    items = {WD + "F": item("The Kite Runner", FILM, ["Khaled Hosseini"], sitelinks=80),
             WD + "S": item("The Kite Runner", SERIES, ["Khaled Hosseini"]),
             WD + "X": item("The Kite Runner", NOVEL, ["Someone Else"]),
             WD + "N": item("The Kite Runner", NOVEL, ["Khaled Hosseini"], sitelinks=60)}
    assert choose_work(book("The Kite Runner", "Khaled Hosseini"), [], set(items), items, CLASSES) == (
        WD + "N", "title+author")


def test_description_fallback_when_wikidata_has_no_author():
    items = {WD + "N": item("Kim", NOVEL, description="1901 novel by Rudyard Kipling")}
    assert choose_work(book("Kim", "Rudyard Kipling"), [], {WD + "N"}, items, CLASSES) == (
        WD + "N", "title+description")


def test_several_works_need_a_clear_sitelink_winner():
    clear = {WD + "A": item("Hamlet", NOVEL, ["William Shakespeare"], sitelinks=140),
             WD + "B": item("Hamlet", NOVEL, ["William Shakespeare"], sitelinks=1)}
    assert choose_work(book("Hamlet", "William Shakespeare"), [], set(clear), clear, CLASSES) == (
        WD + "A", "title+author+sitelinks")
    close = {WD + "A": item("Gone", NOVEL, ["Michael Grant"], sitelinks=2),
             WD + "B": item("Gone", NOVEL, ["Michael Grant"], sitelinks=0)}
    assert choose_work(book("Gone", "Michael Grant"), [], set(close), close, CLASSES) == (None, "ambiguous")


def test_one_to_one_never_maps_two_local_resources_to_one_target():
    votes = {"novel": Counter({"Q1": 1}), "graphic": Counter({"Q1": 1}), "tie": Counter({"Q2": 1, "Q3": 1})}
    chosen, issues = one_to_one(votes, {"novel": 5000, "graphic": 40})
    assert chosen == {"novel": "Q1"}
    assert {(i["local"], i["reason"]) for i in issues} == {("graphic", "shares_target"), ("tie", "conflicting_targets")}


def test_dbpedia_uri_matches_dbpedia_iri_form():
    assert dbpedia_uri("https://en.wikipedia.org/wiki/Harry_Potter_and_the_Philosopher%27s_Stone") == \
        "http://dbpedia.org/resource/Harry_Potter_and_the_Philosopher's_Stone"
    assert dbpedia_uri("https://en.wikipedia.org/wiki/Antoine_de_Saint-Exup%C3%A9ry") == \
        "http://dbpedia.org/resource/Antoine_de_Saint-Exupéry"
    assert dbpedia_uri("https://en.wikipedia.org/wiki/What_If%3F_(book)") == \
        "http://dbpedia.org/resource/What_If%3F_(book)"


def test_wikidata_year_and_edition_titles():
    assert wikidata_year("1997-06-26T00:00:00Z") == 1997
    assert wikidata_year("-0750-01-01T00:00:00Z") == -750
    # BCE works (Plato, Sun Tzu) must still give a valid xsd:gYear: sign plus four digits.
    assert [str(gyear(y)) for y in (-750, -5, 1, 1997)] == ["-0750", "-0005", "0001", "1997"]
    assert wikidata_year("http://www.wikidata.org/.well-known/genid/abc") is None
    assert titles_compatible("The Hunger Games", "The Hunger Games")
    assert titles_compatible("Dune", "Dune (Dune Chronicles #1)")
    assert not titles_compatible("The Hunger Games", "Catching Fire")


def test_unknown_value_placeholders_are_not_items():
    genid = {"x": {"type": "uri", "value": "http://www.wikidata.org/.well-known/genid/be449f3c21954e7d"}}
    assert entity(genid, "x") is None
    assert entity({"x": {"type": "uri", "value": WD + "Q42"}}, "x") == WD + "Q42"


def test_goodreads_id_is_rejected_when_wikidata_names_another_author():
    # Real case: Wikidata gave Kaye Gibbons' Goodreads ID to Zaynab Alkali's "The Virtuous Woman".
    items = {WD + "W": item("The Virtuous Woman", NOVEL, ["Zaynab Alkali"]),
             WD + "N": item("A Virtuous Woman", NOVEL, ["Kaye Gibbons"])}
    assert choose_work(book("A Virtuous Woman", "Kaye Gibbons"), [WD + "W"], {WD + "N"}, items, CLASSES) == (
        WD + "N", "title+author")


def test_match_through_shortened_title_is_marked():
    items = {WD + "B": item("Batman", NOVEL, ["Frank Miller"], sitelinks=90)}
    assert choose_work(book("Batman: Year One", "Frank Miller"), [], {WD + "B"}, items, CLASSES) == (
        WD + "B", "title+author+short-title")


def test_edition_title_rules():
    assert titles_compatible("The Love Verb", "Love Verb")
    assert not titles_compatible("The Wall", "The Walls of Troy and Other Stories")
    assert not titles_compatible("The Morganville Vampires, Volume 1", "The Morganville Vampires Volume 2")
    assert titles_compatible("Saga, Vol. 2", "Saga")  # Open Library title without the volume
    assert titles_compatible("Catch-22", "Catch-22 (50th Anniversary Edition)")
    assert titles_compatible("Bleach, Volume 02", "Bleach, Volume 2")
    assert titles_compatible("The 13½ Lives of Captain Bluebear", "The 13 1/2 lives of Captain Bluebear")
    assert not titles_compatible("Akira, Vol. 1", "Akira, Vol. 4")


@pytest.mark.parametrize("classes,expected", [
    ({SERIES, NOVEL}, "series"),        # "A Court of Thorns and Roses" series item is also typed literary work
    ({MUSICAL, NOVEL}, "other"),        # stage musical with a book credit
    ({SERIALIZED, NOVEL}, "work"),      # Dickens-style novel published in parts
    ({EDITION, NOVEL}, "edition"),
    ({FILM}, "other"),
])
def test_category_precedence(classes, expected):
    it = Item(classes=set(classes))
    assert category(it, CLASSES) == expected


def test_open_library_record_shared_by_two_editions_links_neither():
    # Real cases: Goodreads lists Wolf Hall twice with one ISBN; Open Library answers the ISBNs of
    # Transmetropolitan vol. 1 and vol. 2 with one record titled "Transmetropolitan".
    isbn_of = {"wolf-a": ("9780312429980", "Wolf Hall"), "wolf-b": ("9780312429980", "Wolf Hall"),
               "tm-1": ("9781563894459", "Transmetropolitan, Vol. 1: Back on the Street"),
               "tm-2": ("9781563894817", "Transmetropolitan, Vol. 2: Lust for Life"),
               "dune": ("9780441013593", "Dune")}
    ol = {"9780312429980": {"key": "/books/OL1M", "title": "Wolf Hall"},
          "9781563894459": {"key": "/books/OL2M", "title": "Transmetropolitan"},
          "9781563894817": {"key": "/books/OL2M", "title": "Transmetropolitan"},
          "9780441013593": {"key": "/books/OL3M", "title": "Dune"}}
    links, issues = link_editions(isbn_of, ol)
    assert links == {"dune": "https://openlibrary.org/books/OL3M"}
    assert sorted((i["edition"], i["reason"]) for i in issues) == [
        ("tm-1", "shared_target"), ("tm-2", "shared_target"),
        ("wolf-a", "shared_target"), ("wolf-b", "shared_target")]
