"""Helpers that let the offline rules parser understand spoken Hindi.

Chrome's speech recognizer in hi-IN / mr-IN mode writes in Devanagari:
the owner says "Ramesh ko 500 udhaar diya" and the browser sends
"रमेश को 500 उधार दिया". The regex rules are written for romanized text,
so we first convert Devanagari to Latin letters, then number words to digits.

    to_latin("रमेश को ५०० उधार दिया")         -> "ramesh ko 500 udhaar diya"
    words_to_digits("ramesh ko paanch sau")   -> "ramesh ko 500"

This is deliberately simple (a lookup table plus one pronunciation rule). It
only needs to be good enough for our keywords and for fuzzy name matching,
not perfect transliteration.
"""

import re

VOWELS = {  # independent vowels, e.g. the उ in उधार
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ॲ": "e",
}
MATRAS = {  # vowel signs attached to a consonant, e.g. the ा in का
    "ा": "aa", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    # Letters with a nukta dot, as single characters: क़ ख़ ग़ ज़ ड़ ढ़ फ़ य़
    "क़": "k", "ख़": "kh", "ग़": "g", "ज़": "z",
    "ड़": "d", "ढ़": "dh", "फ़": "f", "य़": "y",
}
NUKTA_SOUNDS = {"ज": "z", "फ": "f"}       # ज + ़ = ज़ (z)
HALANT = "्"                              # "no vowel": joins consonants, as in प्रिया
NASAL = {"ं": "n", "ँ": "n"}              # anusvara / chandrabindu
VISARGA = "ः"
NUKTA = "़"
DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
CONSONANT_SOUNDS = set(CONSONANTS.values()) | set(NUKTA_SOUNDS.values())


def _word_to_latin(word: str) -> str:
    # 1. Split the word into syllable units: [consonant, vowel, vowel_is_inherent].
    units: list[list] = []
    i = 0
    while i < len(word):
        ch = word[i]
        if ch in CONSONANTS:
            sound = CONSONANTS[ch]
            if i + 1 < len(word) and word[i + 1] == NUKTA:
                sound = NUKTA_SOUNDS.get(ch, sound)
                i += 1
            nxt = word[i + 1] if i + 1 < len(word) else ""
            if nxt in MATRAS:
                units.append([sound, MATRAS[nxt], False])
                i += 1
            elif nxt == HALANT:
                units.append([sound, "", False])
                i += 1
            else:
                units.append([sound, "a", True])  # every consonant carries a hidden "a"
        elif ch in VOWELS:
            units.append(["", VOWELS[ch], False])
        elif ch in NASAL and units:
            units[-1][1] += NASAL[ch]
        elif ch == VISARGA and units:
            units[-1][1] += "h"
        elif ch not in (NUKTA, HALANT):
            units.append([ch, "", False])  # digits, Latin letters, anything else
        i += 1

    # 2. Schwa deletion: in Hindi the hidden "a" is usually NOT pronounced
    #    - at the end of a word:              रमेश  ramesha -> ramesh
    #    - between vowel+C and C+vowel:       सबको  sabako  -> sabko
    #    Work right to left, because each deletion changes what the earlier
    #    syllables see next to them.
    for k in range(len(units) - 1, -1, -1):
        cons, vowel, inherent = units[k]
        if not (inherent and vowel == "a"):
            continue
        is_last = k == len(units) - 1
        prev_has_vowel = k > 0 and units[k - 1][1] != ""
        next_is_cv = not is_last and units[k + 1][0] in CONSONANT_SOUNDS and units[k + 1][1] != ""
        if (is_last and len(units) > 1) or (prev_has_vowel and next_is_cv):
            units[k][1] = ""

    latin = "".join(cons + vowel for cons, vowel, _ in units)
    # A long "aa" at the end of a word is written as "a" in Hinglish: diyaa -> diya, kaa -> ka
    return re.sub(r"aa$", "a", latin)


def to_latin(text: str) -> str:
    """Convert any Devanagari words in `text` to Latin letters. Other text is unchanged."""
    text = text.translate(DIGITS).replace("।", " ").replace("॥", " ")  # danda = Hindi full stop
    return re.sub(r"[ऀ-ॿ]+", lambda m: _word_to_latin(m.group()), text)


# ----------------------------------------------------------- number words ---

NUMBER_WORDS = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhah": 6, "chhe": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "dedh": 1.5, "dhai": 2.5, "dhaai": 2.5, "adhai": 2.5,
    "bees": 20, "bis": 20, "tees": 30, "chalis": 40, "chaalis": 40, "pachaas": 50, "pachas": 50,
    "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90,
}
MULTIPLIERS = {"sau": 100, "hazaar": 1000, "hazar": 1000, "hajaar": 1000, "hajar": 1000, "lakh": 100_000}
# Number words safe to convert on their own. "do" (= "give"), "das" or "saath"
# (= "with") alone are too ambiguous, so they only count next to sau/hazaar.
STANDALONE = {"bees", "tees", "chalis", "chaalis", "pachaas", "pachas", "sattar", "assi", "nabbe"}


def words_to_digits(text: str) -> str:
    """'ramesh ko dedh hazaar' -> 'ramesh ko 1500'. Leaves everything else alone."""
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        total, current, has_multiplier, j = 0.0, 0.0, False, i
        while j < len(tokens):
            tok = tokens[j]
            if tok in MULTIPLIERS:
                current = (current or 1) * MULTIPLIERS[tok]
                has_multiplier = True
                if MULTIPLIERS[tok] >= 1000:     # "do hazaar paanch sau" = 2000 + 500
                    total, current = total + current, 0
            elif tok in NUMBER_WORDS or tok.isdigit():
                current += NUMBER_WORDS[tok] if tok in NUMBER_WORDS else int(tok)
            else:
                break
            j += 1

        value = total + current
        run = tokens[i:j]
        convertible = has_multiplier or (len(run) == 1 and run[0] in STANDALONE)
        if run and convertible and value == int(value):
            out.append(str(int(value)))
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)
