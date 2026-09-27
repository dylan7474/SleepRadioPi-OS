"""Make track/artist text speakable before it reaches the offline voice.

Port of SleepRadio's SpeechTextNormalizer.kt. Numbers read digit-by-digit
and a handful of chronically mispronounced names are the two symptoms this
exists for. Deliberately narrow: it only rewrites standalone number-like
tokens and known-bad words, leaving everything else untouched.
"""

from __future__ import annotations

import re

# Band names whose letters are spelled out; without the spaces a voice says "ub" as a word.
PRONUNCIATION_OVERRIDES: dict[str, str] = {
    "UB40": "U B forty",
    "10cc": "ten C C",
}

# A run of digits, optionally with an ordinal suffix (1st, 22nd...) or a plural "s"
# (70s, 1990s) -- the "s" only when it ends the word, so "3Stooges" isn't taken for one.
_NUMBER_TOKEN = re.compile(r"\d+(?:(?:st|nd|rd|th)|s(?![A-Za-z]))?", re.IGNORECASE)
_ORDINAL_SUFFIX = re.compile(r"(st|nd|rd|th)$", re.IGNORECASE)

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

_ORDINAL_ENDINGS = {
    "one": "first", "two": "second", "three": "third", "four": "fourth",
    "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth",
    "twenty": "twentieth", "thirty": "thirtieth", "forty": "fortieth",
    "fifty": "fiftieth", "sixty": "sixtieth", "seventy": "seventieth",
    "eighty": "eightieth", "ninety": "ninetieth", "hundred": "hundredth",
    "thousand": "thousandth",
}


def cardinal_words(n: int) -> str:
    """Cardinal English up to 999,999; larger values fall back to digits."""
    if n < 0:
        return f"minus {cardinal_words(-n)}"
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rem = _TENS[n // 10], n % 10
        return tens if rem == 0 else f"{tens}-{_ONES[rem]}"
    if n < 1000:
        hundreds, rem = f"{_ONES[n // 100]} hundred", n % 100
        return hundreds if rem == 0 else f"{hundreds} and {cardinal_words(rem)}"
    if n < 1_000_000:
        thousands, rem = f"{cardinal_words(n // 1000)} thousand", n % 1000
        return thousands if rem == 0 else f"{thousands} {cardinal_words(rem)}"
    return str(n)


def _year_words(year: int) -> str:
    century, rest = divmod(year, 100)
    if 2000 <= year <= 2009:
        return "two thousand" if rest == 0 else f"two thousand and {cardinal_words(rest)}"
    if rest == 0:
        return f"{cardinal_words(century)} hundred"
    if rest < 10:
        return f"{cardinal_words(century)} oh {cardinal_words(rest)}"
    return f"{cardinal_words(century)} {cardinal_words(rest)}"


def _cardinal_or_year(digits: str, n: int) -> str:
    """A bare 4-digit token with no leading zero reads as a year."""
    return _year_words(n) if len(digits) == 4 and digits[0] != "0" else cardinal_words(n)


def _ordinal_words(n: int) -> str:
    cardinal = cardinal_words(n)
    sep = "-" if "-" in cardinal else " " if " " in cardinal else None
    if sep is None:
        return _ORDINAL_ENDINGS.get(cardinal, cardinal + "th")
    head, tail = cardinal.rsplit(sep, 1)
    return f"{head}{sep}{_ORDINAL_ENDINGS.get(tail, tail + 'th')}"


def _plural_words(words: str) -> str:
    return words[:-1] + "ies" if words.endswith("y") else words + "s"


def _number_token(token: str) -> str:
    if token.lower().endswith("s"):
        digits = token[:-1]
        if not digits.isdigit():
            return token
        return _plural_words(_cardinal_or_year(digits, int(digits)))
    m = _ORDINAL_SUFFIX.search(token)
    digits = token[: m.start()] if m else token
    if not digits.isdigit():
        return token
    return _ordinal_words(int(digits)) if m else _cardinal_or_year(digits, int(digits))


def _apply_overrides(text: str) -> str:
    for bad, good in PRONUNCIATION_OVERRIDES.items():
        text = re.sub(rf"(?i)\b{re.escape(bad)}\b", lambda _m, g=good: g, text)
    return text


def normalize_for_speech(text: str) -> str:
    if not text.strip():
        return text
    s = _apply_overrides(text)

    def spoken(m: re.Match) -> str:
        words = _number_token(m.group(0))
        # A number stuck to letters ("2Pac", "H2O") must stay a separate word.
        before = m.start() > 0 and s[m.start() - 1].isalpha()
        after = m.end() < len(s) and s[m.end()].isalpha()
        return (" " if before else "") + words + (" " if after else "")

    return _NUMBER_TOKEN.sub(spoken, s)
