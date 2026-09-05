"""What the conversation is about right now (§11.1 DISCOVER).

A farmer says "आलू का बीज" in one breath and "स्टॉक है क्या?" in the next.
The second sentence carries no product at all, and a turn handler that looks
only at the words in front of it has nothing to look up -- which is exactly
what happened on the first live calls: the lookup ran on "स्टॉक है क्या?",
found nothing, and the model filled the gap with an invented stock-out.

So the agent keeps a small, explicit memory of the *things* under discussion:
the product, the kind of product, the crop, and the choice it last offered.
Each user turn updates it; each answer reads it when the words alone are not
enough. It is deliberately not the conversation history (that is
:class:`~voice_worker.flow.context.ConversationMemory`) and it is not a model:
it is a few slots and the rules for filling them.

Two things this module learnt from the first calls that looped:

**The crop vocabulary is keyed by the catalogue's spelling.** ``crop_targets``
says ``paddy``; the first version of this table said ``rice``. "राइस का
सीड्स चाहिए" therefore narrowed to *no* product, and the farmer was read a
list of the wrong four seeds. The keys below are the catalogue's, and
:func:`same_crop` folds the spellings a catalogue import might use instead.

**Postpositions sit between the words farmers use.** "पशु की आहार" did not
match "पशु आहार", so a farmer asking for cattle feed was asked what they
wanted, three times. Matching now ignores का/की/के/वाला between the words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..text.script import whole_word

#: The kinds of product the catalogue is divided into (``categories.slug``
#: with the dashes the seed uses), and how farmers name each kind. The
#: English words are here because they are said: a farmer who grew up on
#: "seed" and "spray" does not switch to "बीज" and "छिड़काव" for the phone --
#: and the recogniser writes those English words in Devanagari ("सीड्स").
CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "seeds": ("बीज", "बीजा", "बीजों", "सीड", "सीड्स", "seed", "seeds", "बिजाई", "बीजाई"),
    "fertilisers": (
        "खाद",
        "खादें",
        "खाद्य",
        "उर्वरक",
        "फ़र्टिलाइज़र",
        "फर्टिलाइजर",
        "fertiliser",
        "fertilizer",
        "fertilisers",
        "fertilizers",
        "khad",
        "khaad",
    ),
    "crop-protection": (
        "दवा",
        "दवाई",
        "दवाइयाँ",
        "दवाइयां",
        "दवाइयों",
        "दवाएँ",
        "दवाएं",
        "कीटनाशक",
        "कीटनाशी",
        "स्प्रे",
        "फफूंदनाशक",
        "खरपतवारनाशक",
        "पेस्टिसाइड",
        "इंसेक्टिसाइड",
        "फंगीसाइड",
        "हर्बीसाइड",
        "केमिकल",
        "pesticide",
        "pesticides",
        "insecticide",
        "fungicide",
        "herbicide",
        "weedicide",
        "spray",
        "medicine",
        "medicines",
        "chemical",
        "dawa",
        "dawai",
    ),
    "cattle-feed": (
        "पशु आहार",
        "पशुआहार",
        "पशु का आहार",
        "पशुओं का आहार",
        "आहार",
        "चारा",
        "दाना",
        "दाने",
        "चोकर",
        "खली",
        "चूरी",
        "फीड",
        "कैटल फीड",
        "feed",
        "cattle feed",
        "cattle",
        "fodder",
        # The animal names a farmer for whom "cattle feed" is a catalogue
        # phrase: what they ask for is "गाय का खाना".
        "पशु",
        "पशुओं",
        "जानवर",
        "जानवरों",
        "मवेशी",
        "मवेशियों",
        "दुधारू",
        "पशुधन",
        "डेयरी",
        "livestock",
        "animal",
        "animals",
        "dairy",
        "एनिमल",
        "pashu",
        "janwar",
        # A particular animal names the kind too -- see ANIMAL_WORDS.
    ),
    "tools-equipment": (
        "औज़ार",
        "औजार",
        "उपकरण",
        "यंत्र",
        "पंप",
        "पम्प",
        "स्प्रेयर",
        "मशीन",
        "टूल",
        "टूल्स",
        "इक्विपमेंट",
        "tool",
        "tools",
        "sprayer",
        "pump",
        "equipment",
        "machine",
    ),
}


#: The animals farmers name, in the ways they say them. "cow ka feed" is what
#: a farmer says, and the recogniser writes the English word in Devanagari as
#: it heard it: "कौ", "कौस", "काऊ". Each tuple opens with the spoken Hindi
#: name, which is what the agent says back; the first Latin form is the
#: English.
ANIMAL_WORDS: dict[str, tuple[str, ...]] = {
    "cow": (
        "गाय",
        "गायों",
        "गइया",
        "गैया",
        "cows",
        "cow",
        "कौ",
        "कौस",
        "काऊ",
        "काउ",
        "काऊज़",
        "काउज़",
        "काऊस",
        "कौका",
        "कौके",
        "कौकी",
        "काऊका",
        "काउका",
        "gaay",
        "gai",
    ),
    "buffalo": (
        "भैंस",
        "भैंसों",
        "भैस",
        "buffaloes",
        "buffalo",
        "buffalos",
        "बफ़ेलो",
        "बफैलो",
        "बफेलो",
        "bhains",
        "bhais",
    ),
    "goat": ("बकरी", "बकरियों", "बकरा", "बकरे", "goats", "goat", "गोट", "bakri"),
    "poultry": (
        "मुर्गी",
        "मुर्गियों",
        "मुर्गा",
        "मुर्गे",
        "poultry",
        "hen",
        "hens",
        "chicken",
        "chickens",
        "हेन",
        "चिकन",
        "murgi",
    ),
}

#: Which animals each kind of product is for. "Cattle feed" -- पशु आहार -- is
#: the feed for cattle, and cattle in this belt are cows and buffaloes; the
#: catalogue carries nothing for goats or poultry, and says so rather than
#: guessing. This is what the kind *is*, not advice on what to give.
ANIMALS_FED: dict[str, tuple[str, ...]] = {"cattle-feed": ("cow", "buffalo")}

#: Kind words too general to be said back as the kind: "जानवर में हमारे पास"
#: is not a sentence. The farmer who said "फीड" or "दाना" hears their word.
_NOT_A_KIND_NAME: frozenset[str] = frozenset(
    {
        "पशु",
        "पशुओं",
        "जानवर",
        "जानवरों",
        "मवेशी",
        "मवेशियों",
        "दुधारू",
        "पशुधन",
        "डेयरी",
        "एनिमल",
        "livestock",
        "animal",
        "animals",
        "dairy",
        "cattle",
        "pashu",
        "janwar",
        "केमिकल",
        "chemical",
        "मशीन",
        "machine",
        # A verb, not a kind: "in spray, we have" is not a sentence either.
        "spray",
        "स्प्रे",
    }
)

#: "गाय-भैंस" is said as one word for the pair, and names both.
_CATTLE_PAIR = re.compile(
    whole_word("गाय[\\s\\-]*भैंस|गाय[\\s\\-]*भैंसों|cow[\\s\\-]*buffalo"), re.IGNORECASE
)

#: Crops, in the ways they are said, keyed by the catalogue's ``crop_targets``
#: spelling. The list is short and static on purpose: it is what farmers in
#: this belt grow, and a crop the catalogue has nothing for resolves to no
#: product either way. Each tuple opens with the spoken Hindi name, which is
#: what :func:`crop_spoken` says back.
CROP_WORDS: dict[str, tuple[str, ...]] = {
    "wheat": ("गेहूँ", "गेहूं", "गेहु", "गेंहू", "व्हीट", "wheat", "gehu", "gehun", "gehoon"),
    "paddy": ("धान", "चावल", "राइस", "पैडी", "paddy", "rice", "dhan", "chawal"),
    "potato": ("आलू", "आलु", "पोटैटो", "पोटेटो", "potato", "potatoes", "aloo", "alu"),
    "tomato": ("टमाटर", "टोमैटो", "टोमेटो", "tomato", "tomatoes", "tamatar"),
    "mustard": ("सरसों", "सरसो", "राई", "मस्टर्ड", "mustard", "sarson"),
    "sugarcane": ("गन्ना", "गन्ने", "ईख", "शुगरकेन", "sugarcane", "ganna", "ganne"),
    "maize": ("मक्का", "मक्के", "मकई", "भुट्टा", "कॉर्न", "मेज़", "maize", "corn", "makka", "makke"),
    "gram": ("चना", "चने", "काबुली चना", "gram", "chana", "chane", "chickpea"),
    "pigeon_pea": ("अरहर", "तुअर", "तूर", "arhar", "tur", "toor", "pigeon pea"),
    "moong": ("मूँग", "मूंग", "moong", "mung"),
    "urad": ("उड़द", "उरद", "urad", "urd"),
    "lentil": ("मसूर", "मसूरी", "masoor", "masur", "lentil", "lentils"),
    "soybean": ("सोयाबीन", "soybean", "soyabean"),
    "cotton": ("कपास", "कॉटन", "cotton", "kapas"),
    "onion": ("प्याज़", "प्याज", "ऑनियन", "onion", "onions", "pyaz", "pyaaz"),
    "chilli": ("मिर्च", "मिर्ची", "चिली", "chilli", "chili", "mirch"),
    "okra": ("भिंडी", "okra", "bhindi"),
    "brinjal": ("बैंगन", "बैगन", "brinjal", "baingan"),
    "cauliflower": ("गोभी", "फूलगोभी", "cauliflower", "gobhi"),
    "pea": ("मटर", "pea", "peas", "matar"),
    "garlic": ("लहसुन", "गार्लिक", "garlic", "lahsun"),
    "banana": ("केला", "केले", "banana", "kela", "kele"),
    "mango": ("आम", "मैंगो", "mango", "aam"),
    "bottle_gourd": ("लौकी", "घिया", "lauki", "bottle gourd"),
    # The rest of what this belt grows. Most have nothing in the catalogue
    # yet; they are here so "बाजरा के लिए क्या है?" is answered "nothing for
    # bajra yet" rather than "which product?".
    "barley": ("जौ", "बार्ली", "barley", "jau"),
    "pearl_millet": ("बाजरा", "bajra", "pearl millet"),
    "sorghum": ("ज्वार", "jowar", "sorghum"),
    "groundnut": ("मूँगफली", "मूंगफली", "groundnut", "peanut", "moongfali", "mungfali"),
    "sesame": ("तिल", "sesame", "til"),
    "linseed": ("अलसी", "linseed", "alsi"),
    "sunflower": ("सूरजमुखी", "sunflower", "surajmukhi"),
    "mint": ("मेंथा", "मिंट", "पुदीना", "mentha", "mint", "pudina"),
    "turmeric": ("हल्दी", "turmeric", "haldi"),
    "ginger": ("अदरक", "ginger", "adrak"),
    "coriander": ("धनिया", "coriander", "dhaniya"),
    "fenugreek": ("मेथी", "fenugreek", "methi"),
    "spinach": ("पालक", "spinach", "palak"),
    "cabbage": ("पत्तागोभी", "पत्ता गोभी", "बंदगोभी", "cabbage", "patta gobhi"),
    "capsicum": ("शिमला मिर्च", "capsicum", "shimla mirch"),
    "carrot": ("गाजर", "carrot", "gajar"),
    "radish": ("मूली", "radish", "mooli"),
    "cucumber": ("खीरा", "ककड़ी", "cucumber", "kheera"),
    "pumpkin": ("कद्दू", "सीताफल", "pumpkin", "kaddu"),
    "bitter_gourd": ("करेला", "bitter gourd", "karela"),
    "ridge_gourd": ("तोरई", "तुरई", "ridge gourd", "torai"),
    "watermelon": ("तरबूज", "तरबूज़", "watermelon", "tarbooz"),
    "muskmelon": ("खरबूजा", "खरबूज़ा", "muskmelon", "kharbooja"),
    "papaya": ("पपीता", "papaya", "papita"),
    "guava": ("अमरूद", "guava", "amrood"),
    "lemon": ("नींबू", "नीबू", "lemon", "nimbu"),
    "sweet_potato": ("शकरकंद", "sweet potato", "shakarkand"),
    "taro": ("अरबी", "taro", "arbi"),
    "vegetables": (
        "सब्ज़ी",
        "सब्जी",
        "सब्ज़ियाँ",
        "सब्जियाँ",
        "सब्ज़ियों",
        "सब्जियों",
        "vegetable",
        "vegetables",
        "sabzi",
        "sabji",
    ),
}

#: The crops a product tagged ``vegetables`` is for. A catalogue row says
#: "vegetables" once rather than listing twenty; a farmer says "टमाटर".
VEGETABLES: frozenset[str] = frozenset(
    {
        "potato",
        "tomato",
        "onion",
        "chilli",
        "okra",
        "brinjal",
        "cauliflower",
        "pea",
        "garlic",
        "bottle_gourd",
        "coriander",
        "fenugreek",
        "spinach",
        "cabbage",
        "capsicum",
        "carrot",
        "radish",
        "cucumber",
        "pumpkin",
        "bitter_gourd",
        "ridge_gourd",
        "sweet_potato",
        "taro",
    }
)

#: Spellings a catalogue import might carry for a crop this table keys
#: differently. Folded by :func:`canonical_crop` so "rice" in a row still
#: meets "paddy" in the vocabulary.
_CROP_ALIASES: dict[str, str] = {
    "rice": "paddy",
    "dhan": "paddy",
    "mung": "moong",
    "green gram": "moong",
    "pigeon pea": "pigeon_pea",
    "arhar": "pigeon_pea",
    "toor": "pigeon_pea",
    "tur": "pigeon_pea",
    "red gram": "pigeon_pea",
    "masoor": "lentil",
    "masur": "lentil",
    "chana": "gram",
    "chickpea": "gram",
    "bengal gram": "gram",
    "black gram": "urad",
    "lauki": "bottle_gourd",
    "corn": "maize",
    "makka": "maize",
    "sarson": "mustard",
    "aloo": "potato",
    "gehu": "wheat",
    "gehun": "wheat",
}

#: Postpositions and particles that sit between the words a farmer uses and
#: carry no meaning of their own: "पशु की आहार", "गेहूँ वाला बीज".
_PARTICLES: frozenset[str] = frozenset(
    {"का", "की", "के", "को", "में", "से", "वाला", "वाली", "वाले", "ka", "ki", "ke", "wala", "wali"}
)
_TOKEN = re.compile(r"\S+")


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
_ANIMALS = _compile(ANIMAL_WORDS)


def squeeze(text: str) -> str:
    """The utterance with its particles removed: "पशु की आहार" → "पशु आहार"."""
    return " ".join(t for t in _TOKEN.findall(text) if t.lower() not in _PARTICLES)


def category_in(text: str) -> str | None:
    """The kind of product named in an utterance, or None.

    An animal names a kind as well: "गाय का दाना" is cattle feed, and so is
    "बकरी का दाना" -- the nearest kind the catalogue has, which the reply
    then says is not for goats.
    """
    found = kind_word_in(text)
    if found is not None:
        return found[0]
    if animals_in(text):
        return "cattle-feed"
    return None


def kind_word_in(text: str) -> tuple[str, str] | None:
    """The kind named, and the word the farmer used for it: ("cattle-feed", "फीड")."""
    for candidate in (text, squeeze(text)):
        for key, pattern in _CATEGORIES:
            found = pattern.search(candidate)
            if found is not None:
                return key, found.group(0)
    return None


def animals_in(text: str) -> tuple[str, ...]:
    """The animals named in an utterance, in the order they were said."""
    named: list[str] = []
    for candidate in (text, squeeze(text)):
        if _CATTLE_PAIR.search(candidate):
            named.extend(("cow", "buffalo"))
        for key, pattern in _ANIMALS:
            if pattern.search(candidate):
                named.append(key)
    return tuple(dict.fromkeys(named))


def animal_spoken(animal: str, *, language: str = "hi-IN") -> str:
    """How the animal is said back: "गाय" for ``cow``, "cow" in English."""
    forms = ANIMAL_WORDS.get(animal, ())
    if language.lower().startswith("en"):
        for form in forms:
            if form.isascii():
                return form
        return animal
    return forms[0] if forms else animal


def animals_fed_by(category: str | None) -> tuple[str, ...]:
    """The animals a kind of product is for; empty for kinds that feed none."""
    return ANIMALS_FED.get(category or "", ())


def is_kind_word(text: str) -> bool:
    """True when the text is nothing but the name of a kind: "पशु आहार", "seeds".

    A catalogue may carry a product named after its own kind ("पशु आहार" as
    the plain pellet), and the matcher then reads the kind as that product.
    A farmer who said only the kind's name asked about the kind.
    """
    candidate = squeeze(text).strip()
    return bool(candidate) and any(p.fullmatch(candidate) for _, p in _CATEGORIES)


def is_crop_word(text: str) -> bool:
    """True when the text is nothing but a crop's name: "आलू", "potato"."""
    candidate = squeeze(text).strip()
    return bool(candidate) and any(p.fullmatch(candidate) for _, p in _CROPS)


def crop_in(text: str) -> str | None:
    """The crop named in an utterance, in the catalogue's spelling, or None."""
    for candidate in (text, squeeze(text)):
        for key, pattern in _CROPS:
            if pattern.search(candidate):
                return key
    return None


def canonical_crop(crop: str) -> str:
    """One spelling for a crop, whatever the catalogue row or the table used."""
    folded = crop.strip().lower().replace("-", " ").replace("_", " ")
    folded = _CROP_ALIASES.get(folded, folded)
    return folded.replace(" ", "_")


def same_crop(left: str, right: str) -> bool:
    return canonical_crop(left) == canonical_crop(right)


def crop_fits(crop: str, tags: tuple[str, ...]) -> bool:
    """Whether a product tagged ``tags`` is for ``crop``.

    Exact under either spelling, or the ``vegetables`` tag for any vegetable.
    """
    wanted = canonical_crop(crop)
    for tag in tags:
        folded = canonical_crop(tag)
        if folded == wanted:
            return True
        if folded == "vegetables" and wanted in VEGETABLES:
            return True
    return False


def crop_spoken(crop: str) -> str:
    """How the crop is said back: "धान" for ``paddy``."""
    forms = CROP_WORDS.get(canonical_crop(crop))
    return forms[0] if forms else crop


@dataclass(frozen=True, slots=True)
class ProductInFocus:
    sku: str
    name: str


@dataclass
class ConversationFocus:
    """The product, kind of product, crop and offered choice under discussion."""

    product: ProductInFocus | None = None
    category: str | None = None
    crop: str | None = None
    #: Set when the agent has just offered a hand-over ("जोड़ दूँ?"), so the
    #: farmer's "हाँ" on the next turn means "yes, connect me".
    offered_transfer: bool = False
    #: The products the agent just offered as a choice ("A, B और C हैं। कौन
    #: सा?"). The next turn is read against these before anything else:
    #: "पहला वाला", "दूसरा", or the name said back in the farmer's own
    #: inflection ("मसूरी").
    choices: tuple[ProductInFocus, ...] = ()
    #: Every product read out for the kind under discussion, in order, so
    #: "और क्या-क्या है?" continues the list instead of starting it again.
    listed: tuple[str, ...] = ()
    #: The animals the farmer has named ("गाय", "cow and buffalo"): the
    #: reply speaks of them, not of "पशु आहार".
    animals: tuple[str, ...] = ()
    #: The kind and the farmer's own word for it ("cattle-feed", "फीड"), so
    #: the reply uses their word where it is in the reply's script.
    kind_word: tuple[str, str] | None = None
    #: What the agent last said, so the same sentence is never said twice
    #: running without acknowledging that it is being repeated.
    last_reply: str = ""
    #: What the agent offered to do at the end of its last line -- ("listing",
    #: "cattle-feed") for "पशु आहार बताऊँ?", ("lookup", sku) for "रेट और
    #: स्टॉक बताऊँ?" -- so that "हाँ" does it.
    offered: tuple[str, str] | None = None
    #: Consecutive turns the agent has asked something back without the
    #: farmer's words resolving to anything. §11.4: the same question is
    #: never asked a third time -- the second miss changes tack, the third
    #: hands the call to a person.
    misses: int = 0
    #: How many user turns since the product was last mentioned. A product
    #: from twenty turns ago is not what "इसका रेट?" means.
    _product_age: int = field(default=0, repr=False)

    #: After this many turns without a mention the product is forgotten.
    MAX_PRODUCT_AGE = 6

    def note_user_turn(self, transcript: str) -> None:
        """Read the kind of product and the crop off the words.

        The product itself is set by :meth:`note_product` from the lexicon,
        which knows the catalogue; this only reads the two vocabularies above.
        The offered choice is left alone: the direct layer decides whether
        this turn answers it or moves on.
        """
        category = category_in(transcript)
        crop = crop_in(transcript)
        animals = animals_in(transcript)
        previous = self.category
        if category is not None and category != self.category:
            # A new kind: what was read out of the old one is history, and
            # so are the animals unless the new kind feeds them.
            self.listed = ()
            if not animals_fed_by(category):
                self.animals = ()
        if animals:
            self.animals = animals
        kind_word = kind_word_in(transcript)
        if kind_word is not None and kind_word[1].lower() not in _NOT_A_KIND_NAME:
            self.kind_word = kind_word
        elif category is not None and category != self.category:
            self.kind_word = None
        if category is not None:
            self.category = category
        if crop is not None:
            self.crop = crop
        if (crop is not None and kind_word is not None) or (
            category is not None and category != previous
        ):
            # A new kind, or a crop with a kind ("गेहूँ का बीज"), is a new
            # subject; the old product no longer answers "इसका रेट?". The
            # same kind again, an animal, or a crop on its own is not: "ये
            # भैंस को भी दे सकते हैं?" and "और धान में?" are about the product
            # in hand.
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
        self.choices = ()
        self.listed = ()
        self.misses = 0

    def forget_product(self) -> None:
        self.product = None
        self._product_age = 0

    def offer(self, choices: tuple[ProductInFocus, ...]) -> None:
        """The agent has just asked the farmer to pick one of these."""
        self.choices = choices
        self.listed = tuple(dict.fromkeys((*self.listed, *(c.sku for c in choices))))

    def note_miss(self) -> int:
        """The agent asked something back. Returns how many in a row."""
        self.misses += 1
        return self.misses

    def note_answered(self) -> None:
        """A turn resolved: the streak of questions back is over."""
        self.misses = 0
        self.choices = ()
        self.listed = ()


__all__ = (
    "ANIMALS_FED",
    "ANIMAL_WORDS",
    "CATEGORY_WORDS",
    "CROP_WORDS",
    "VEGETABLES",
    "ConversationFocus",
    "ProductInFocus",
    "animal_spoken",
    "animals_fed_by",
    "animals_in",
    "canonical_crop",
    "category_in",
    "crop_fits",
    "crop_in",
    "crop_spoken",
    "is_crop_word",
    "is_kind_word",
    "kind_word_in",
    "same_crop",
    "squeeze",
)
