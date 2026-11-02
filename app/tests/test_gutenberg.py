from pathlib import Path

import pytest

from sleepradiopi.playback import gutenberg as gb

REAL = (Path(__file__).parent / "gutenberg_search.xml").read_bytes()      # a real answer to "pride prejudice" (2026-11-02)


def test_parses_a_real_search_page():
    books = gb.parse_search(REAL)
    assert books[0] == {"id": "1342", "title": "Pride and Prejudice", "author": "Jane Austen"}
    assert len(books) == 11 and all(b["id"].isdigit() and b["title"] for b in books)
    assert any("Finnish" in b["title"] for b in books)               # (other languages are in there: the title says so)


def test_a_book_with_no_author_has_none_not_its_download_count():
    book = next(b for b in gb.parse_search(REAL) if b["id"] == "10471")
    assert book["author"] == ""


def test_search_asks_for_the_words_and_steps_through_the_pages():
    asked = []

    def fetch(url, limit, headers=None):
        asked.append(url)
        return REAL

    gb.search("pride  & prejudice", fetch=fetch)
    gb.search("pride", offset=25, fetch=fetch)
    assert asked[0].endswith("?query=pride+%26+prejudice") and "start_index" not in asked[0]
    assert asked[1].endswith("?query=pride&start_index=26")
    assert gb.search("   ", fetch=fetch) == [] and len(asked) == 2     # (nothing to look for: no request)


def test_no_connection_and_rubbish_are_said_plainly():
    def down(url, limit, headers=None):
        raise OSError("no route")

    with pytest.raises(gb.GutenbergError, match="online"):
        gb.search("pride", fetch=down)
    with pytest.raises(gb.GutenbergError, match="readable"):
        gb.parse_search(b"<html>not a feed")


def test_the_book_number_must_be_a_number():
    assert gb.book_id(" 1342 ") == "1342"
    for bad in ("", "1342/../x", "abc", None):
        with pytest.raises(gb.GutenbergError):
            gb.book_id(bad)
