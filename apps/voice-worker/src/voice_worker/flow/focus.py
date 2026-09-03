"""What the conversation is about right now (§11.1 DISCOVER).

A farmer says "आलू का बीज" in one breath and "स्टॉक है क्या?" in the next.
The second sentence carries no product at all, and a turn handler that looks
only at the words in front of it has nothing to look up -- which is exactly
what happened on the first live calls: the lookup ran on "स्टॉक है क्या?",
found nothing, and the model filled the gap with an invented stock-out.

So the agent keeps a small, explicit memory of the *things* under discussion:
the product, the kind of product, the crop. Each user turn updates it; each
answer reads it when the words alone are not enough. It is deliberately not
the conversation history (that is :class:`~voice_worker.flow.context.ConversationMemory`)
and it is not a model: it is three slots and the rules for filling them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..text.script import whole_word

#: The kinds of product the catalogue is divided into (``categories.slug``
#: with the dashes the seed uses), and how farmers name each kind. The
#: English words are here because they are said: a farmer who grew up on
#: "seed" and "spray" does not switch to "बीज" and "छिड़काव" for the phone.
CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "seeds": ("बीज", "बीजा", "सीड", "सीड्स", "seed", "seeds", "बिजाई"),
    "fertilisers": ("खाद", "खाद्य", "उर्वरक", "फ़र्टिलाइज़र", "फर्टिलाइजर", "fertiliser", "fertilizer"),
    "crop-protection": (
        "दवा",
        "दवाई",
        "दवाइयाँ",
        "दवाइयां",
        "कीटनाशक",
        "स्प्रे",
        "फफूंदनाशक",
        "खरपतवारनाशक",
        "pesticide",
        "insecticide",
        "fungicide",
        "herbicide",
        "spray",
        "medicine",
    ),
    "cattle-feed": ("पशु आहार", "पशुआहार", "चारा", "फीड", "feed", "cattle feed", "खली", "चूरी"),
    "tools-equipment": (
        "औज़ार",
        "औजार",
        "पंप",
        "पम्प",
        "स्प्रेयर",
        "मशीन",
        "टूल",
        "tools",
        "sprayer",
        "pump",
        "equipment",
        "machine",
    ),
}

#: Crops, in the ways they are said, keyed by the catalogue's ``crop_targets``
#: spelling. The list is short and static on purpose: it is what farmers in
#: this belt grow, and a crop the catalogue has nothing for resolves to no
#: product either way.
CROP_WORDS: dict[str, tuple[str, ...]] = {
    "wheat": ("गेहूँ", "गेहूं", "गेहु", "गेंहू", "wheat", "gehu", "gehun"),
    "rice": ("धान", "चावल", "paddy", "rice", "dhan"),
    "potato": ("आलू", "आलु", "potato", "potatoes", "aloo", "alu", "पोटैटो", "पोटेटो"),
    "tomato": ("टमाटर", "tomato", "tamatar", "टोमैटो"),
    "mustard": ("सरसों", "सरसो", "mustard", "sarson", "राई"),
    "sugarcane": ("गन्ना", "गन्ने", "ईख", "sugarcane", "ganna", "ganne"),
    "maize": ("मक्का", "मक्के", "मकई", "भुट्टा", "maize", "corn", "makka", "makke"),
    "gram": ("चना", "चने", "gram", "chana", "chane", "chickpea"),
    "pigeon pea": ("अरहर", "तुअर", "तूर", "arhar", "tur", "pigeon pea"),
    "mung": ("मूँग", "मूंग", "moong", "mung"),
    "urad": ("उड़द", "उरद", "urad", "urd"),
    "soybean": ("सोयाबीन", "soybean", "soyabean"),
    "cotton": ("कपास", "cotton", "kapas"),
    "onion": ("प्याज़", "प्याज", "onion", "pyaz", "pyaaz"),
    "chilli": ("मिर्च", "मिर्ची", "chilli", "chili", "mirch"),
    "okra": ("भिंडी", "okra", "bhindi"),
    "brinjal": ("बैंगन", "बैगन", "brinjal", "baingan"),
    "cauliflower": ("गोभी", "फूलगोभी", "cauliflower", "gobhi"),
    "pea": ("मटर", "pea", "peas", "matar"),
    "garlic": ("लहसुन", "garlic", "lahsun"),
    "banana": ("केला", "केले", "banana", "kela", "kele"),
    "mango": ("आम", "mango", "aam"),
}


def _compile(words: dict[str, tuple[str, ...]]) -> list[tuple[str, re.Pattern[str]]]:
    # Longer forms first, so "पशु आहार" is tried before "आहार" could be.
    return [
        (
            key,
            re.compile(
                whole_word("|".join(re.escape(w) for w in sorted(forms, key=len, reverse=True))),
                re.IGNORECASE,
            ),
        )
        for key, forms in words.items()
    ]


_CATEGORIES = _compile(CATEGORY_WORDS)
_CROPS = _compile(CROP_WORDS)


def category_in(text: str) -> str | None:
    """The kind of product named in an utterance, or None."""
    for key, pattern in _CATEGORIES:
        if pattern.search(text):
            return key
    return None


def crop_in(text: str) -> str | None:
    """The crop named in an utterance, in the catalogue's spelling, or None."""
    for key, pattern in _CROPS:
        if pattern.search(text):
            return key
    return None


@dataclass(frozen=True, slots=True)
class ProductInFocus:
    sku: str
    name: str


@dataclass
class ConversationFocus:
    """The product, kind of product and crop under discussion."""

    product: ProductInFocus | None = None
    category: str | None = None
    crop: str | None = None
    #: Set when the agent has just offered a hand-over ("जोड़ दूँ?"), so the
    #: farmer's "हाँ" on the next turn means "yes, connect me".
    offered_transfer: bool = False
    #: How many user turns since the product was last mentioned. A product
    #: from twenty turns ago is not what "इसका रेट?" means.
    _product_age: int = field(default=0, repr=False)

    #: After this many turns without a mention the product is forgotten.
    MAX_PRODUCT_AGE = 6

    def note_user_turn(self, transcript: str) -> None:
        """Read the kind of product and the crop off the words.

        The product itself is set by :meth:`note_product` from the lexicon,
        which knows the catalogue; this only reads the two vocabularies above.
        """
        category = category_in(transcript)
        crop = crop_in(transcript)
        if category is not None:
            self.category = category
        if crop is not None:
            self.crop = crop
        if category is not None or crop is not None:
            # A new kind or crop is a new subject; the old product no longer
            # answers "इसका रेट?".
            self.product = None
            self._product_age = 0
        elif self.product is not None:
            self._product_age += 1
            if self._product_age > self.MAX_PRODUCT_AGE:
                self.product = None

    def note_product(self, sku: str, name: str) -> None:
        """A product was resolved -- from the words, or from a tool result."""
        self.product = ProductInFocus(sku=sku, name=name)
        self._product_age = 0

    def forget_product(self) -> None:
        self.product = None
        self._product_age = 0


__all__ = (
    "CATEGORY_WORDS",
    "CROP_WORDS",
    "ConversationFocus",
    "ProductInFocus",
    "category_in",
    "crop_in",
)
