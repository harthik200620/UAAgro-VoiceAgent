"""The farmer's register, enforced after generation (§11.3).

The persona asks the model for the Hindi farmers actually speak -- "रेट" not
"मूल्य", "खाद" not "उर्वरक", "स्टॉक में है" not "उपलब्ध है" -- and the model
mostly obliges. Mostly is not enough on a phone line: one "उर्वरक की मात्रा"
in an otherwise fine answer marks the agent as a textbook, and a customer who
heard it on the first demo said so. So the register is also enforced here,
deterministically, on every sentence before it is spoken or remembered.

Two things are done, and only these two:

* **Words are swapped** using a fixed table of the formal words a model
  reaches for and the everyday word a farmer uses instead. Longer phrases are
  replaced before shorter ones, and every rule is a whole-word match, so
  "उपलब्धता" is not half-replaced.
* **Stage directions are removed.** A model under a strict prompt sometimes
  narrates -- "*(मैनेजर से कनेक्ट करने का प्रयास)*" -- and a synthesiser reads
  the brackets out loud. Anything in asterisks, square brackets or
  parentheses that is not a plain aside is dropped.

Nothing here rewrites meaning. A swap that could change a fact -- a dose, a
number, a product name -- is not in the table.
"""

from __future__ import annotations

import re

from .script import whole_word

#: Formal → spoken. Overlapping phrases are tried longest first, so
#: "उपलब्ध नहीं है" is one swap and not "उपलब्ध" followed by "नहीं है".
REPLACEMENTS: tuple[tuple[str, str], ...] = (
    # money and stock
    ("मूल्य", "रेट"),
    ("क़ीमत", "रेट"),
    ("कीमत", "रेट"),
    ("उपलब्ध नहीं है", "स्टॉक में नहीं है"),
    ("उपलब्ध नहीं हैं", "स्टॉक में नहीं हैं"),
    ("उपलब्ध है", "स्टॉक में है"),
    ("उपलब्ध हैं", "स्टॉक में हैं"),
    ("उपलब्धता", "स्टॉक"),
    ("अनुपलब्ध", "स्टॉक में नहीं"),
    ("भुगतान", "पेमेंट"),
    ("छूट", "डिस्काउंट"),
    # farming
    ("उर्वरक", "खाद"),
    ("कीटनाशक", "कीड़े की दवा"),
    ("फफूंदनाशक", "फफूंदी की दवा"),
    ("खरपतवारनाशक", "खरपतवार की दवा"),
    ("कृषि", "खेती"),
    ("फ़सल सुरक्षा", "फ़सल की दवा"),
    ("सिंचाई", "पानी"),
    ("मात्रा", "डोज़"),
    ("खुराक", "डोज़"),
    ("प्रति एकड़", "एक एकड़ में"),
    ("प्रति बीघा", "एक बीघा में"),
    ("हेक्टेयर", "ढाई एकड़"),
    ("उत्पाद", "माल"),
    ("उत्पादन", "पैदावार"),
    ("बुवाई", "बुआई"),
    # service and people
    ("केंद्र प्रबंधक", "सेंटर मैनेजर"),
    ("केन्द्र प्रबंधक", "सेंटर मैनेजर"),
    ("प्रबंधक", "मैनेजर"),
    ("विशेषज्ञ", "एक्सपर्ट"),
    ("सहायता", "मदद"),
    ("समाधान", "इलाज"),
    ("समस्या", "दिक्कत"),
    ("सूचना", "जानकारी"),
    ("विवरण", "डिटेल"),
    ("संपर्क करें", "फ़ोन करें"),
    ("संपर्क कीजिए", "फ़ोन कीजिए"),
    ("संपर्क", "फ़ोन"),
    ("प्रतीक्षा कीजिए", "रुकिए"),
    ("प्रतीक्षा करें", "रुकिए"),
    ("प्रतीक्षा", "इंतज़ार"),
    ("क्षण", "सेकंड"),
    ("प्रयास", "कोशिश"),
    ("अवश्य", "ज़रूर"),
    ("अत्यंत", "बहुत"),
    ("अतिरिक्त", "और"),
    ("आवश्यकता", "ज़रूरत"),
    ("आवश्यक", "ज़रूरी"),
    ("स्वागत है", "नमस्ते"),
    ("धन्यवाद है", "धन्यवाद"),
    ("विकल्प", "ऑप्शन"),
    ("सुझाव", "सलाह"),
    ("अनुशंसा", "सलाह"),
    ("निर्देश", "तरीक़ा"),
    ("प्रतिशत", "परसेंट"),
    ("किलोग्राम", "किलो"),
    ("मिलीलीटर", "एमएल"),
    ("दुकान", "सेंटर"),
    ("वितरण", "डिलीवरी"),
    ("शीघ्र", "जल्दी"),
    ("तुरंत", "अभी"),
)

_TABLE = {formal: spoken for formal, spoken in REPLACEMENTS}
_PATTERN = re.compile(
    whole_word("|".join(re.escape(k) for k in sorted(_TABLE, key=len, reverse=True)))
)

#: Narration the model should never have written: "*(...)*", "[...]",
#: "(मैनेजर से जोड़ने की कोशिश)". A parenthesis holding only a number or a
#: pack size is kept: "(50 किलो)" is content.
_ASTERISKS = re.compile(r"\*[^*\n]{0,160}\*")
_BRACKETS = re.compile(r"\[[^\]\n]{0,160}\]")
_PARENS = re.compile(r"\(([^)\n]{0,160})\)")
_MEASURE = re.compile(r"^[\d०-९ ,.\-%]+(किलो|ग्राम|लीटर|एमएल|kg|g|l|ml)?\s*$")  # noqa: RUF001
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_STOP = re.compile(r"\s+([।,?!])")


def _drop_narration(text: str) -> str:
    text = _ASTERISKS.sub(" ", text)
    text = _BRACKETS.sub(" ", text)
    return _PARENS.sub(lambda m: m.group(0) if _MEASURE.match(m.group(1)) else " ", text)


def farmers_register(text: str) -> str:
    """Swap textbook words for the spoken ones and drop stage directions."""
    if not text:
        return text
    cleaned = _drop_narration(text)
    swapped = _PATTERN.sub(lambda m: _TABLE[m.group(0)], cleaned)
    swapped = _SPACE_BEFORE_STOP.sub(r"\1", swapped)
    return _SPACES.sub(" ", swapped).strip()


__all__ = ("REPLACEMENTS", "farmers_register")
