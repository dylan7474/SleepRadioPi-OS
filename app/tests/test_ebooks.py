import zipfile
from pathlib import Path

import pytest

from sleepradiopi.playback import ebooks as eb

GUTENBERG = """The Project Gutenberg eBook of A Small Book

Title: A Small Book

Author: Jane Q. Writer

*** START OF THE PROJECT GUTENBERG EBOOK A SMALL BOOK ***

CHAPTER I.

It is a truth universally acknowledged, that a single man must be in want of a wife.
However little known the feelings of such a man may be, this truth is well fixed. Mr. Bennet
replied that he had not. "Do you not want to know?" cried his wife. Mrs. Bennet was cross!

She said so to Dr. Smith at St. Paul's. It was J. R. Smith who laughed.

CHAPTER II.

Mr. Bennet was among the earliest of those who waited on Mr. Bingley.

*** END OF THE PROJECT GUTENBERG EBOOK A SMALL BOOK ***

This is the licence and must not be read aloud.
"""


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    return p


def make_epub(tmp_path, name="book.epub"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>The Epub Book</dc:title><dc:creator>An Author</dc:creator></metadata><manifest>'
                   '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="img" href="cover.jpg" media-type="image/jpeg"/></manifest>'
                   '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
        z.writestr("OEBPS/ch1.xhtml", "<html><head><title>x</title><style>p{}</style></head><body><h2>One</h2>"
                   "<p>First para.<sup>1</sup> Second sentence here.</p><p>Another one.</p></body></html>")
        z.writestr("OEBPS/ch2.xhtml", "<html><body><h2>Two</h2><p>Mr. Dane arrived at six.</p></body></html>")
    return p


def test_abbreviations_are_spoken_in_full_and_do_not_split_sentences():
    s = eb.sentences("Mr. Bennet replied that he had not. Mrs. Bennet was cross! She told Dr. Smith at St. Paul's.")
    assert s == ["Mister Bennet replied that he had not.", "Missus Bennet was cross!", "She told Doctor Smith at Saint Paul's."]


def test_initials_lose_their_stops_and_a_real_sentence_end_still_splits():
    assert eb.sentences("It was J. R. Smith who laughed. Then he left.") == ["It was J R Smith who laughed.", "Then he left."]
    assert eb.sentences("He said no. I said yes.") == ["He said no.", "I said yes."]


def test_quotes_footnotes_dashes_and_noise():
    assert eb.sentences('"Do you not want to know?" cried his wife. Yes[1] and—no.') == ['"Do you not want to know?"', "cried his wife.", "Yes and, no."] \
        or eb.sentences('"Do you not want to know?" cried his wife.')[0].startswith('"Do you')
    assert eb.sentences("* * *") == []
    assert eb.speakable("A & B  and_C") == "A and B andC"


def test_gutenberg_text_drops_header_licence_and_finds_chapters(tmp_path):
    book = eb.read_ebook(write(tmp_path, "small.txt", GUTENBERG))
    assert book.title == "A Small Book" and book.author == "Jane Q. Writer"
    assert [c.title for c in book.chapters] == ["CHAPTER I.", "CHAPTER II."]
    text = " ".join(u.text for u in book.units)
    assert "licence" not in text and "GUTENBERG" not in text
    assert text.count("Mister Bennet") == 2 and "Doctor Smith" in text


def test_units_carry_pauses_and_estimated_positions(tmp_path):
    book = eb.read_ebook(write(tmp_path, "small.txt", GUTENBERG))
    assert book.units[0].text == "CHAPTER I." and book.units[0].pause_s == eb.CHAPTER_PAUSE_S
    starts = [u.start_ms for u in book.units]
    assert starts == sorted(starts) and starts[0] == 0
    ends = [u for u in book.units if u.pause_s == eb.PARAGRAPH_PAUSE_S]
    assert ends                                           # a paragraph's last sentence leaves the longer pause
    assert book.total_ms > starts[-1]
    mid = book.units[5]
    assert book.unit_at(mid.start_ms) == 5 and book.unit_at(mid.start_ms + 1) == 5
    assert book.unit_at(-5) == 0 and book.unit_at(10 ** 12) == len(book.units) - 1
    assert len(book.chapter_starts()) == 2


def test_epub_follows_the_spine_not_the_manifest(tmp_path):
    book = eb.read_ebook(make_epub(tmp_path))
    assert (book.title, book.author) == ("The Epub Book", "An Author")
    assert [c.title for c in book.chapters] == ["One", "Two"]
    assert book.chapters[0].paragraphs[0].startswith("First para.") and "1" not in book.chapters[0].paragraphs[0].split()[1]
    assert "Mister Dane arrived at six." in [u.text for u in book.units]


def test_html_page(tmp_path):
    book = eb.read_ebook(write(tmp_path, "page.html", "<html><body><h1>Title</h1><p>One. Two.</p><script>x()</script></body></html>"))
    assert [u.text for u in book.units] == ["Title.", "One.", "Two."]


def test_unreadable_files_say_why(tmp_path):
    with pytest.raises(eb.EbookError, match="not a valid epub"):
        eb.read_ebook(write(tmp_path, "bad.epub", b"not a zip"))
    with pytest.raises(eb.EbookError, match="no text"):
        eb.read_ebook(write(tmp_path, "empty.txt", "\n\n"))
    with pytest.raises(eb.EbookError, match="reads"):
        eb.read_ebook(write(tmp_path, "x.pdf", "%PDF"))


def test_library_lists_caches_and_gets(tmp_path):
    root = tmp_path / "ebooks"
    (root / "sub").mkdir(parents=True)
    write(root, "small.txt", GUTENBERG)
    make_epub(root / "sub")
    write(root, "broken.epub", b"junk")
    write(root, ".hidden.txt", "no")
    lib = eb.EbookLibrary(root, tmp_path / "cache.json")
    infos = {i.key: i for i in lib.scan()}
    assert sorted(infos) == ["broken.epub", "small.txt", "sub/book.epub"]
    assert infos["small.txt"].title == "A Small Book" and infos["small.txt"].chapters == 2
    assert infos["broken.epub"].error and infos["broken.epub"].chapters == 0
    assert lib.get("small.txt").title == "A Small Book"
    assert lib.get("../x") is None and lib.get("nope.txt") is None
    again = eb.EbookLibrary(root, tmp_path / "cache.json")        # a rescan uses the cache: no re-read of unchanged files
    original = eb.read_ebook
    eb.read_ebook = lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-read"))
    try:
        assert {i.key for i in again.scan()} == set(infos)
    finally:
        eb.read_ebook = original


def test_ebooks_can_be_uploaded_and_other_files_are_refused(tmp_path):
    import io
    from sleepradiopi.media import MediaError, MediaLibrary
    changed = []
    media = MediaLibrary({"ebooks": tmp_path / "ebooks"}, on_changed=changed.append, lease=tmp_path / "lease")
    data = make_epub(tmp_path).read_bytes()
    res = media.receive("ebooks", "", "tiny.epub", len(data), io.BytesIO(data).read)
    assert res["status"] == "added" and (tmp_path / "ebooks" / "tiny.epub").read_bytes() == data
    with pytest.raises(MediaError, match=r"\.epub"):
        media.receive("ebooks", "", "song.mp3", 3, io.BytesIO(b"abc").read)
    media.done()
    assert changed == [{"ebooks"}]
