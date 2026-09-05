"""What the direct-answer layer says, in the caller's language (§11.1, §11.3).

The resolution logic in :mod:`voice_worker.flow.direct` decides *what* is
true -- which product, in stock or not, at what price, at which centre. This
module decides how that is said. Keeping the two apart is what lets the same
resolution serve a Hindi farmer and an English one without a language branch
in every method, and what makes a third language one more class here rather
than another branch in twenty places.

A phrasebook is a set of small functions that return complete sentences. They
take names and numbers, never tool results: the layer above has already read
the data, and a phrasebook that reached into a result dict would be a second
place that knew the tool's shape.

The Hindi is the farmers' Hindi (§11.3): रेट, स्टॉक, सेंटर, बैग -- the words
they use -- and numbers left as digits for :mod:`voice_worker.text.speech` to
say. The English keeps its digits too; the English voice reads them.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any

from ..text.lexicon import LexiconEntry
from ..text.numbers import digits_to_words
from .focus import CROP_WORDS, animal_spoken, canonical_crop, crop_spoken

# --------------------------------------------------------------------------- #
# Fixed lines. Exported by name: the agent, the tests and the panel refer to
# them, and a line that is also a constant is a line that can be asserted on.
# --------------------------------------------------------------------------- #

OVERVIEW_HI = "हमारे पास बीज, खाद, दवा, पशु आहार और खेती के औज़ार मिलते हैं। बताइए, क्या चाहिए?"
HEARING_OK_HI = "जी, साफ़ सुनाई दे रहा है। बताइए, क्या चाहिए?"
MACHINE_HI = "मैं यूए एग्रो का ऑटोमैटिक सहायक हूँ। चाहें तो किसी व्यक्ति से जोड़ दूँ?"
ASK_PRODUCT_PRICE_HI = "किस चीज़ का रेट पूछ रहे हैं? नाम बता दीजिए।"
ASK_PRODUCT_STOCK_HI = "किस चीज़ का स्टॉक देखना है? नाम बता दीजिए।"
#: The second time in a row nothing resolved: the kinds, and a way out.
ASK_KIND_HI = "बीज, खाद, दवा, पशु आहार या औज़ार — किस चीज़ की बात है? या सेंटर मैनेजर से बात करा दूँ?"
#: The third time: a person. Said by the agent, then the transfer tool.
MISUNDERSTOOD_HI = "माफ़ कीजिए, मैं ठीक से समझ नहीं पा रहा हूँ। मैं आपको सेंटर मैनेजर से जोड़ देता हूँ।"

OVERVIEW_EN = (
    "We have seeds, fertilisers, crop protection, cattle feed and farm tools. What do you need?"
)
HEARING_OK_EN = "Yes, I can hear you clearly. What do you need?"
MACHINE_EN = "I am UA Agro's automatic assistant. Shall I connect you to a person?"
ASK_PRODUCT_PRICE_EN = "Which product's price? Please tell me the name."
ASK_PRODUCT_STOCK_EN = "Which product would you like? Please tell me the name."
ASK_KIND_EN = (
    "Seeds, fertiliser, crop protection, cattle feed or tools — which one? "
    "Or shall I connect you to the centre manager?"
)
MISUNDERSTOOD_EN = "Sorry, I am not able to follow. I am connecting you to the centre manager."

#: How each kind of product is said back to the farmer.
CATEGORY_SPOKEN: dict[str, str] = {
    "seeds": "बीज",
    "fertilisers": "खाद",
    "crop-protection": "दवा",
    "cattle-feed": "पशु आहार",
    "tools-equipment": "औज़ार",
}
CATEGORY_SPOKEN_EN: dict[str, str] = {
    "seeds": "seeds",
    "fertilisers": "fertilisers",
    "crop-protection": "crop protection",
    "cattle-feed": "cattle feed",
    "tools-equipment": "tools",
}

_MONTHS_HI = (
    "जनवरी",
    "फ़रवरी",
    "मार्च",
    "अप्रैल",
    "मई",
    "जून",
    "जुलाई",
    "अगस्त",
    "सितंबर",
    "अक्टूबर",
    "नवंबर",
    "दिसंबर",
)
_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_DAYS_HI = {
    "mon": "सोमवार",
    "tue": "मंगलवार",
    "wed": "बुधवार",
    "thu": "गुरुवार",
    "fri": "शुक्रवार",
    "sat": "शनिवार",
    "sun": "रविवार",
}
_DAYS_EN = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
_INGREDIENT_HI = {
    "n": "नाइट्रोजन",
    "nitrogen": "नाइट्रोजन",
    "p": "फ़ॉस्फ़ोरस",
    "p2o5": "फ़ॉस्फ़ोरस",
    "phosphorus": "फ़ॉस्फ़ोरस",
    "phosphate": "फ़ॉस्फ़ोरस",
    "k": "पोटाश",
    "k2o": "पोटाश",
    "potash": "पोटाश",
    "potassium": "पोटाश",
    "s": "सल्फ़र",
    "sulphur": "सल्फ़र",
    "sulfur": "सल्फ़र",
    "zn": "ज़िंक",
    "zinc": "ज़िंक",
    "b": "बोरॉन",
    "boron": "बोरॉन",
    "ca": "कैल्शियम",
    "calcium": "कैल्शियम",
    "mg": "मैग्नीशियम",
    "magnesium": "मैग्नीशियम",
    "fe": "आयरन",
    "iron": "आयरन",
}

_KILOS = frozenset({"kg", "kilogram", "kilo"})
_GRAMS = frozenset({"g", "gm", "gram", "grams"})
_LITRES = frozenset({"l", "lt", "ltr", "litre", "liter"})
_PIECES = frozenset({"pc", "pcs", "piece", "pieces", "unit", "units", "nos"})
#: A pack this heavy is a bag; lighter is a packet.
_BAG_FROM_KG = Decimal(25)


# --------------------------------------------------------------------------- #
# Numbers, shared by both languages
# --------------------------------------------------------------------------- #


def money(value: Any) -> str:
    """Whole rupees: "1350" for Decimal("1350.00"), "1307" for 1307.44.

    A price is said the way a shopkeeper says it, and a synthesiser reading
    "दशमलव चार चार" after every figure is not that. The paise stay in the
    row for the invoice; they are not for the phone.
    """
    if value is None:
        return ""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return str(int(amount.to_integral_value(rounding="ROUND_HALF_UP")))


def plain(value: Any) -> str:
    """A percentage or a quantity with trailing zeros trimmed: "18", "0.5"."""
    if value is None:
        return ""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount.normalize():f}"


def _split_pack(pack: str) -> tuple[Decimal, str] | None:
    parts = pack.split()
    if len(parts) != 2:
        return None
    try:
        return Decimal(parts[0]), parts[1].lower()
    except InvalidOperation:
        return None


def _restock_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _phone_digits(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)[-10:]
    return digits if len(digits) == 10 else None


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


class Phrasebook(ABC):
    """Every sentence the direct layer can say, for one language."""

    #: The route code the sentences are written in.
    code: str
    #: The kind under discussion and the farmer's own word for it, when that
    #: word is in this language's script: a farmer who said "फीड" hears
    #: "फीड", not "पशु आहार".
    said_kind: tuple[str, str] | None = None

    # -- names ----------------------------------------------------------- #

    def animals(self, animals: Sequence[str]) -> str:
        """ "गाय और भैंस", "cows and buffaloes": the animals, said back."""
        return self.join(animal_spoken(a, language=self.code) for a in animals)

    def kind_said(self, category: str | None) -> str:
        """The kind, in the farmer's word for it if they used one."""
        if self.said_kind is not None and self.said_kind[0] == category:
            word = self.said_kind[1]
            if word.isascii() == self.code.lower().startswith("en"):
                return word
        return self.kind(category)

    @abstractmethod
    def product(self, entry: LexiconEntry, data: Mapping[str, Any] | None = None) -> str: ...

    @abstractmethod
    def kind(self, category: str | None) -> str: ...

    @abstractmethod
    def crop(self, crop: str) -> str: ...

    @abstractmethod
    def centre(self, name_hi: str, name_en: str) -> str: ...

    @abstractmethod
    def join(self, items: Iterable[str]) -> str: ...

    @abstractmethod
    def either(self, items: Iterable[str]) -> str:
        """ "A या B" -- a list to pick one from."""

    @abstractmethod
    def pack(self, pack: str) -> str: ...

    @abstractmethod
    def restock(self, value: Any) -> str: ...

    # -- fixed lines ------------------------------------------------------ #

    @abstractmethod
    def overview(self) -> str: ...

    @abstractmethod
    def hearing_ok(self) -> str: ...

    @abstractmethod
    def machine(self) -> str: ...

    @abstractmethod
    def ask_product(self, *, asks_price: bool) -> str: ...

    @abstractmethod
    def ask_kind(self) -> str: ...

    @abstractmethod
    def misunderstood(self) -> str: ...

    # -- resolving ------------------------------------------------------- #

    @abstractmethod
    def ambiguous(self, names: Sequence[str]) -> str: ...

    @abstractmethod
    def choice(
        self,
        names: str,
        *,
        crop: str | None,
        kind: str,
        more: bool = False,
        animals: str = "",
    ) -> str:
        """A few to pick from; ``more`` when the kind has others unnamed;
        ``animals`` when the farmer named who it is for."""

    @abstractmethod
    def pick_by_position(self, names: str, *, count: int = 2) -> str:
        """The second miss on a choice: how to answer it, by name or number."""

    @abstractmethod
    def listing_or_person(self, listing: str) -> str:
        """The second miss with a kind known: the list, and a way out."""

    @abstractmethod
    def not_carried_for_crop(self, crop: str, kind: str, others: str) -> str: ...

    @abstractmethod
    def nothing_for_crop(self, crop: str) -> str: ...

    @abstractmethod
    def nothing_in_kind(self, kind: str) -> str: ...

    @abstractmethod
    def listing(self, kind: str, names: str, *, more: bool, animals: str = "") -> str: ...

    @abstractmethod
    def again(self, text: str) -> str:
        """The same answer a second time running, owned as a repeat."""

    # -- crops ------------------------------------------------------------- #

    @abstractmethod
    def crop_listing(self, crop: str, groups: Sequence[tuple[str, str, bool]]) -> str:
        """Everything for a crop, by kind: ``(kind, names, more)`` per kind."""

    @abstractmethod
    def suits_crop(self, name: str, crop: str, crops: str) -> str:
        """ "Yes, X is for wheat (wheat and gram)." """

    @abstractmethod
    def not_for_crop(self, name: str, crops: str, crop: str, others: str, kind: str) -> str:
        """ "X is for paddy, not wheat. For wheat, in sprays, we have ..." """

    # -- animals ----------------------------------------------------------- #

    @abstractmethod
    def suits(self, subject: str, animals: str) -> str:
        """ "Yes, X is for cows and buffaloes." """

    @abstractmethod
    def suits_all(self, names: str, animals: str) -> str:
        """ "Yes, all of these are for cows: A, B and C. Which one?" """

    @abstractmethod
    def suits_again(self, animals: str) -> str:
        """The same confirmation asked a second way: shorter, not a rerun."""

    @abstractmethod
    def not_for_animal(self, subject: str, fed: str, animals: str) -> str:
        """ "X is for cows and buffaloes, not for goats. Ask the manager?" """

    @abstractmethod
    def not_feed(self, name: str, kind: str, animals: str) -> str:
        """ "Urea is fertiliser, not something to feed. Show the cattle feed?" """

    @abstractmethod
    def listing_more(self, kind: str, names: str, *, more: bool) -> str:
        """The next few of a kind, after "what else?"."""

    @abstractmethod
    def no_more(self, kind: str) -> str:
        """ "What else?" when everything of the kind has been read out."""

    # -- stock and price ------------------------------------------------- #

    @abstractmethod
    def carried_no_centre(self, name: str) -> str: ...

    @abstractmethod
    def carried_list_no_centre(self, names: str) -> str: ...

    @abstractmethod
    def restricted(self, name: str) -> str: ...

    @abstractmethod
    def not_stocked(self, name: str, centre: str) -> str: ...

    @abstractmethod
    def in_stock(
        self, name: str, pack: str, price: str, centre: str, *, asks_price: bool
    ) -> str: ...

    @abstractmethod
    def out_of_stock(self, name: str, centre: str, eta: str) -> str: ...

    @abstractmethod
    def alternative(self, name: str, price: str) -> str: ...

    @abstractmethod
    def call_ahead(self) -> str: ...

    @abstractmethod
    def centre_note(self, centre: str) -> str: ...

    @abstractmethod
    def price_line(self, name: str, pack: str, price: str) -> str: ...

    @abstractmethod
    def not_available(self, name: str) -> str: ...

    @abstractmethod
    def close_list(self, joined: str) -> str:
        """The list of prices as a sentence, before the centre note."""

    @abstractmethod
    def which_one(self) -> str: ...

    # -- composition and the centre -------------------------------------- #

    @abstractmethod
    def composition_part(self, ingredient: str, percent: Any) -> str: ...

    @abstractmethod
    def composition(self, name: str, parts: str) -> str: ...

    @abstractmethod
    def restricted_suffix(self) -> str: ...

    @abstractmethod
    def centre_sentence(
        self,
        *,
        name: str,
        address: str | None,
        open_time: time | None,
        close_time: time | None,
        working_days: Sequence[str],
        phone: str | None,
    ) -> str: ...


# --------------------------------------------------------------------------- #
# Hindi
# --------------------------------------------------------------------------- #


class HindiPhrasebook(Phrasebook):
    code = "hi-IN"

    def product(self, entry: LexiconEntry, data: Mapping[str, Any] | None = None) -> str:
        return str((data or {}).get("name_hi") or entry.name_hi)

    def kind(self, category: str | None) -> str:
        return CATEGORY_SPOKEN.get(category or "", "")

    def crop(self, crop: str) -> str:
        if canonical_crop(crop) == "vegetables":
            return "सब्ज़ियों"
        return crop_spoken(crop)

    def centre(self, name_hi: str, name_en: str) -> str:
        return name_hi

    def join(self, items: Iterable[str]) -> str:
        parts = [str(i) for i in items if str(i)]
        if len(parts) <= 1:
            return parts[0] if parts else ""
        return ", ".join(parts[:-1]) + " और " + parts[-1]

    def either(self, items: Iterable[str]) -> str:
        return " या ".join(str(i) for i in items if str(i))

    def pack(self, pack: str) -> str:
        """ "50 kg" → "50 किलो का बैग"; "1 L" → "1 लीटर की बोतल"."""
        split = _split_pack(pack)
        if split is None:
            return pack
        number, unit = split
        amount = money(number)
        if unit in _KILOS:
            return f"{amount} किलो {'का बैग' if number >= _BAG_FROM_KG else 'का पैकेट'}"
        if unit in _GRAMS:
            return f"{amount} ग्राम का पैकेट"
        if unit in _LITRES:
            return f"{amount} लीटर की बोतल"
        if unit == "ml":
            return f"{amount} एमएल की बोतल"
        if unit in _PIECES:
            return f"{amount} पीस"
        return f"{amount} {unit}"

    def restock(self, value: Any) -> str:
        day = _restock_date(value)
        return f"{day.day} {_MONTHS_HI[day.month - 1]}" if day else ""

    def overview(self) -> str:
        return OVERVIEW_HI

    def hearing_ok(self) -> str:
        return HEARING_OK_HI

    def machine(self) -> str:
        return MACHINE_HI

    def ask_product(self, *, asks_price: bool) -> str:
        return ASK_PRODUCT_PRICE_HI if asks_price else ASK_PRODUCT_STOCK_HI

    def ask_kind(self) -> str:
        return ASK_KIND_HI

    def misunderstood(self) -> str:
        return MISUNDERSTOOD_HI

    def ambiguous(self, names: Sequence[str]) -> str:
        return f"दो चीज़ें हैं — {' और '.join(names)}। कौन सी?"

    def choice(
        self,
        names: str,
        *,
        crop: str | None,
        kind: str,
        more: bool = False,
        animals: str = "",
    ) -> str:
        tail = " और भी हैं।" if more else ""
        if animals:
            return f"{animals} के लिए हमारे पास {names} हैं।{tail} कौन सा चाहिए?"
        if crop is not None:
            return f"{crop} के लिए हमारे पास {names} हैं।{tail} कौन सा चाहिए?"
        if kind:
            return f"{kind} में हमारे पास {names} हैं।{tail} कौन सा चाहिए?"
        return f"हमारे पास {names} हैं।{tail} कौन सा चाहिए?"

    def pick_by_position(self, names: str, *, count: int = 2) -> str:
        how = "पहला या दूसरा" if count <= 2 else "नाम, या पहला-दूसरा-तीसरा"
        return f"कौन सा — {names}? {how} बोल दीजिए। या सबका रेट बता दूँ?"

    def listing_or_person(self, listing: str) -> str:
        return listing.rstrip("।? ") + "? इनमें से कोई चाहिए, या सेंटर मैनेजर से बात करा दूँ?"

    def not_carried_for_crop(self, crop: str, kind: str, others: str) -> str:
        sentence = f"{crop} का {kind} अभी हमारे पास नहीं है।"
        if others:
            sentence += f" {kind} में {others} हैं।"
        return sentence

    def nothing_for_crop(self, crop: str) -> str:
        return f"{crop} के लिए अभी कुछ नहीं है। बीज, खाद या दवा — क्या चाहिए?"

    def nothing_in_kind(self, kind: str) -> str:
        return f"{kind} में अभी कुछ नहीं है।"

    def listing(self, kind: str, names: str, *, more: bool, animals: str = "") -> str:
        tail = " और भी हैं।" if more else ""
        if animals:
            return f"{animals} के लिए हमारे पास {names} हैं।{tail} कौन सा चाहिए?"
        return f"{kind} में हमारे पास {names} हैं।{tail} कौन सा चाहिए?"

    def listing_more(self, kind: str, names: str, *, more: bool) -> str:
        tail = " और भी हैं।" if more else " बस इतने ही हैं।"
        return f"{kind} में {names} भी हैं।{tail} कौन सा चाहिए?"

    def again(self, text: str) -> str:
        return f"जी, दोबारा बता देता हूँ। {text}"

    def crop_listing(self, crop: str, groups: Sequence[tuple[str, str, bool]]) -> str:
        parts = [f"{kind} में {names}{' वग़ैरह' if more else ''}" for kind, names, more in groups]
        kinds = self.either(kind for kind, _, _ in groups)
        return f"{crop} के लिए हमारे पास {self.join(parts)} हैं। {kinds} — क्या देखूँ?"

    def suits_crop(self, name: str, crop: str, crops: str) -> str:
        detail = f" यह {crops} के लिए बना है।" if crops and crops != crop else ""
        return f"हाँ, {name} {crop} के लिए है।{detail} रेट और स्टॉक बताऊँ?"

    def not_for_crop(self, name: str, crops: str, crop: str, others: str, kind: str) -> str:
        sentence = f"{name} {crops} के लिए है, {crop} के लिए नहीं।"
        if others:
            sentence += f" {crop} के लिए {kind} में {others} हैं। कौन सा देखूँ?"
        else:
            sentence += f" {crop} के लिए {kind} में अभी कुछ नहीं है।"
        return sentence

    def suits(self, subject: str, animals: str) -> str:
        return f"हाँ, {subject} {animals} के लिए ही है। रेट और स्टॉक बताऊँ?"

    def suits_all(self, names: str, animals: str) -> str:
        return f"हाँ, ये सब {animals} के लिए ही हैं — {names}। कौन सा देखूँ?"

    def suits_again(self, animals: str) -> str:
        return f"जी हाँ, {animals} के लिए ही है। बोलिए, कौन सा देखूँ?"

    def not_for_animal(self, subject: str, fed: str, animals: str) -> str:
        return f"{subject} {fed} के लिए है, {animals} के लिए नहीं। {animals} के लिए सेंटर मैनेजर से पूछ लूँ?"

    def not_feed(self, name: str, kind: str, animals: str) -> str:
        return f"{name} {kind} है, पशुओं को खिलाने की चीज़ नहीं। {animals} के लिए पशु आहार बताऊँ?"

    def no_more(self, kind: str) -> str:
        return f"{kind} में बस यही हैं। इनमें से कौन सा चाहिए?"

    def carried_no_centre(self, name: str) -> str:
        return f"{name} हमारे पास मिलता है। आप किस सेंटर के पास हैं? वहाँ का स्टॉक बता दूँ।"

    def carried_list_no_centre(self, names: str) -> str:
        return f"{names} हमारे पास मिलते हैं। आप किस सेंटर के पास हैं? वहाँ का रेट बता दूँ।"

    def restricted(self, name: str) -> str:
        return f"{name} लाइसेंस वाली दवा है, इसके लिए सेंटर मैनेजर से बात करनी होगी। जोड़ दूँ?"

    def not_stocked(self, name: str, centre: str) -> str:
        return f"{name} {centre} पर नहीं रखते।"

    def in_stock(self, name: str, pack: str, price: str, centre: str, *, asks_price: bool) -> str:
        if asks_price:
            head = f"{name}, {pack}: {price} रुपये।" if pack else f"{name}: {price} रुपये।"
            return f"{head} {centre} पर स्टॉक में है।"
        detail = f"{pack} {price} रुपये में" if pack else f"{price} रुपये में"
        return f"{name} स्टॉक में है, {detail}।"

    def out_of_stock(self, name: str, centre: str, eta: str) -> str:
        sentence = f"{name} अभी {centre} पर स्टॉक में नहीं है।"
        return f"{sentence} {eta} तक आ जाएगा।" if eta else sentence

    def alternative(self, name: str, price: str) -> str:
        return f" अभी {name} है, {price} रुपये में।"

    def call_ahead(self) -> str:
        return " आने से पहले सेंटर पर फ़ोन करके पक्का कर लीजिए।"

    def centre_note(self, centre: str) -> str:
        return f" यह {centre} का रेट है।"

    def price_line(self, name: str, pack: str, price: str) -> str:
        return f"{name} {pack} {price} रुपये" if pack else f"{name} {price} रुपये"

    def not_available(self, name: str) -> str:
        return f"{name} अभी नहीं है"

    def close_list(self, joined: str) -> str:
        return f"{joined}।"

    def which_one(self) -> str:
        return " कौन सा चाहिए?"

    def composition_part(self, ingredient: str, percent: Any) -> str:
        spoken = _INGREDIENT_HI.get(ingredient.strip().lower(), ingredient)
        return f"{spoken} {plain(percent)} परसेंट"

    def composition(self, name: str, parts: str) -> str:
        return f"{name} में {parts} है।"

    def restricted_suffix(self) -> str:
        return " यह लाइसेंस वाली दवा है, सेंटर मैनेजर से बात करनी होगी। जोड़ दूँ?"

    def centre_sentence(
        self,
        *,
        name: str,
        address: str | None,
        open_time: time | None,
        close_time: time | None,
        working_days: Sequence[str],
        phone: str | None,
    ) -> str:
        sentences: list[str] = []
        if address:
            # The spoken address is already a place phrase ("... के पास", "... रोड पर").
            sentences.append(f"{name} {address.strip().rstrip('।')} है।")
        else:
            sentences.append(f"{name} है।")
        if open_time is not None and close_time is not None:
            hours = f"{_hour_hi(open_time)} से {_hour_hi(close_time)} तक खुला रहता है"
            if working_days:
                hours += f", {self._days(working_days)}"
            sentences.append(hours + "।")
        digits = _phone_digits(phone)
        if digits:
            sentences.append(f"फ़ोन नंबर {digits_to_words(digits, paired=True)}।")
        return " ".join(sentences)

    def _days(self, days: Sequence[str]) -> str:
        keys = [d.lower()[:3] for d in days]
        if not keys or len(keys) >= 7:
            return "हर दिन"
        if keys == ["mon", "tue", "wed", "thu", "fri", "sat"]:
            return "सोमवार से शनिवार"
        if keys == ["mon", "tue", "wed", "thu", "fri"]:
            return "सोमवार से शुक्रवार"
        return self.join(_DAYS_HI.get(k, k) for k in keys)


def _hour_hi(moment: time) -> str:
    hour = moment.hour
    if 5 <= hour < 12:
        part = "सुबह"
    elif 12 <= hour < 16:
        part = "दोपहर"
    elif 16 <= hour < 20:
        part = "शाम"
    else:
        part = "रात"
    twelve = hour % 12 or 12
    if moment.minute == 30:
        return f"{part} साढ़े {twelve} बजे"
    return f"{part} {twelve} बजे"


# --------------------------------------------------------------------------- #
# English
# --------------------------------------------------------------------------- #


class EnglishPhrasebook(Phrasebook):
    code = "en-IN"

    def product(self, entry: LexiconEntry, data: Mapping[str, Any] | None = None) -> str:
        return str((data or {}).get("name_en") or entry.name_en or entry.name_hi)

    def kind(self, category: str | None) -> str:
        return CATEGORY_SPOKEN_EN.get(category or "", "")

    def crop(self, crop: str) -> str:
        """The first Latin form the vocabulary lists: "paddy" for धान."""
        for form in CROP_WORDS.get(canonical_crop(crop), ()):
            if form.isascii():
                return form
        return crop.replace("_", " ")

    def centre(self, name_hi: str, name_en: str) -> str:
        return name_en or name_hi

    def join(self, items: Iterable[str]) -> str:
        parts = [str(i) for i in items if str(i)]
        if len(parts) <= 1:
            return parts[0] if parts else ""
        return ", ".join(parts[:-1]) + " and " + parts[-1]

    def either(self, items: Iterable[str]) -> str:
        return " or ".join(str(i) for i in items if str(i))

    def pack(self, pack: str) -> str:
        split = _split_pack(pack)
        if split is None:
            return pack
        number, unit = split
        amount = money(number)
        if unit in _KILOS:
            return f"{amount} kg {'bag' if number >= _BAG_FROM_KG else 'pack'}"
        if unit in _GRAMS:
            return f"{amount} gram pack"
        if unit in _LITRES:
            return f"{amount} litre bottle"
        if unit == "ml":
            return f"{amount} ml bottle"
        if unit in _PIECES:
            return f"{amount} pieces"
        return f"{amount} {unit}"

    def restock(self, value: Any) -> str:
        day = _restock_date(value)
        return f"{day.day} {_MONTHS_EN[day.month - 1]}" if day else ""

    def overview(self) -> str:
        return OVERVIEW_EN

    def hearing_ok(self) -> str:
        return HEARING_OK_EN

    def machine(self) -> str:
        return MACHINE_EN

    def ask_product(self, *, asks_price: bool) -> str:
        return ASK_PRODUCT_PRICE_EN if asks_price else ASK_PRODUCT_STOCK_EN

    def ask_kind(self) -> str:
        return ASK_KIND_EN

    def misunderstood(self) -> str:
        return MISUNDERSTOOD_EN

    def ambiguous(self, names: Sequence[str]) -> str:
        return f"There are two — {' and '.join(names)}. Which one?"

    def choice(
        self,
        names: str,
        *,
        crop: str | None,
        kind: str,
        more: bool = False,
        animals: str = "",
    ) -> str:
        tail = " There are more." if more else ""
        if animals:
            return f"For {animals} we have {names}.{tail} Which one would you like?"
        if crop is not None:
            return f"For {crop} we have {names}.{tail} Which one would you like?"
        if kind:
            return f"In {kind} we have {names}.{tail} Which one would you like?"
        return f"We have {names}.{tail} Which one would you like?"

    def pick_by_position(self, names: str, *, count: int = 2) -> str:
        how = "first or second" if count <= 2 else "the name, or first, second, third"
        return f"Which one — {names}? Say {how}, or I can read every price."

    def listing_or_person(self, listing: str) -> str:
        return (
            listing.rstrip("?. ") + "? Any of these, or shall I connect you to the centre manager?"
        )

    def not_carried_for_crop(self, crop: str, kind: str, others: str) -> str:
        sentence = f"We do not have {crop} {kind} right now."
        if others:
            sentence += f" In {kind} we have {others}."
        return sentence

    def nothing_for_crop(self, crop: str) -> str:
        return (
            f"Nothing for {crop} right now. "
            "Seeds, fertiliser or crop protection — what do you need?"
        )

    def nothing_in_kind(self, kind: str) -> str:
        return f"Nothing in {kind} right now."

    def listing(self, kind: str, names: str, *, more: bool, animals: str = "") -> str:
        tail = " There are more." if more else ""
        if animals:
            return f"For {animals} we have {names}.{tail} Which one would you like?"
        return f"In {kind} we have {names}.{tail} Which one would you like?"

    def listing_more(self, kind: str, names: str, *, more: bool) -> str:
        tail = " There are more." if more else " That is all of them."
        return f"In {kind} we also have {names}.{tail} Which one would you like?"

    def again(self, text: str) -> str:
        return f"Once more: {text}"

    def crop_listing(self, crop: str, groups: Sequence[tuple[str, str, bool]]) -> str:
        parts = [f"in {kind}, {names}{' and more' if more else ''}" for kind, names, more in groups]
        kinds = self.either(kind for kind, _, _ in groups)
        kinds = kinds[:1].upper() + kinds[1:]
        return f"For {crop} we have {self.join(parts)}. {kinds} — which shall I look at?"

    def suits_crop(self, name: str, crop: str, crops: str) -> str:
        detail = f" It is made for {crops}." if crops and crops != crop else ""
        return f"Yes, {name} is for {crop}.{detail} Shall I tell you the price and stock?"

    def not_for_crop(self, name: str, crops: str, crop: str, others: str, kind: str) -> str:
        sentence = f"{name} is for {crops}, not for {crop}."
        if others:
            sentence += f" For {crop}, in {kind}, we have {others}. Which one shall I check?"
        else:
            sentence += f" For {crop} we have nothing in {kind} right now."
        return sentence

    def suits(self, subject: str, animals: str) -> str:
        return f"Yes, {subject} is for {animals}. Shall I tell you the price and stock?"

    def suits_all(self, names: str, animals: str) -> str:
        return f"Yes, all of these are for {animals}: {names}. Which one shall I check?"

    def suits_again(self, animals: str) -> str:
        return f"Yes, it is for {animals}. Which one shall I check?"

    def not_for_animal(self, subject: str, fed: str, animals: str) -> str:
        return (
            f"{subject} is for {fed}, not for {animals}. "
            f"Shall I ask the centre manager about {animals}?"
        )

    def not_feed(self, name: str, kind: str, animals: str) -> str:
        return (
            f"{name} is {kind}, not something to feed animals. "
            f"Shall I list the cattle feed for {animals}?"
        )

    def no_more(self, kind: str) -> str:
        return f"That is everything in {kind}. Which of those would you like?"

    def carried_no_centre(self, name: str) -> str:
        return f"We do carry {name}. Which centre are you near? I will check the stock there."

    def carried_list_no_centre(self, names: str) -> str:
        return f"We carry {names}. Which centre are you near? I will tell you the price there."

    def restricted(self, name: str) -> str:
        return f"{name} is a licensed product; the centre manager handles it. Shall I connect you?"

    def not_stocked(self, name: str, centre: str) -> str:
        return f"{centre} does not stock {name}."

    def in_stock(self, name: str, pack: str, price: str, centre: str, *, asks_price: bool) -> str:
        if asks_price:
            head = f"{name}, {pack}: {price} rupees." if pack else f"{name}: {price} rupees."
            return f"{head} In stock at {centre}."
        detail = f"{pack} at {price} rupees" if pack else f"{price} rupees"
        return f"{name} is in stock, {detail}."

    def out_of_stock(self, name: str, centre: str, eta: str) -> str:
        sentence = f"{name} is not in stock at {centre} right now."
        return f"{sentence} Expected by {eta}." if eta else sentence

    def alternative(self, name: str, price: str) -> str:
        return f" {name} is available now, at {price} rupees."

    def call_ahead(self) -> str:
        return " Please call the centre to confirm before coming."

    def centre_note(self, centre: str) -> str:
        return f" That is the {centre} price."

    def price_line(self, name: str, pack: str, price: str) -> str:
        return f"{name} {pack} {price} rupees" if pack else f"{name} {price} rupees"

    def not_available(self, name: str) -> str:
        return f"{name} is not available"

    def close_list(self, joined: str) -> str:
        return f"{joined}."

    def which_one(self) -> str:
        return " Which one would you like?"

    def composition_part(self, ingredient: str, percent: Any) -> str:
        return f"{plain(percent)} percent {ingredient}"

    def composition(self, name: str, parts: str) -> str:
        return f"{name} contains {parts}."

    def restricted_suffix(self) -> str:
        return " It is a licensed product; the centre manager handles it. Shall I connect you?"

    def centre_sentence(
        self,
        *,
        name: str,
        address: str | None,
        open_time: time | None,
        close_time: time | None,
        working_days: Sequence[str],
        phone: str | None,
    ) -> str:
        sentences = [f"{name} is at {address.strip().rstrip('.')}." if address else f"{name}."]
        if open_time is not None and close_time is not None:
            hours = f"Open {_hour_en(open_time)} to {_hour_en(close_time)}"
            if working_days:
                hours += f", {self._days(working_days)}"
            sentences.append(hours + ".")
        digits = _phone_digits(phone)
        if digits:
            sentences.append(f"Phone number {' '.join(digits)}.")
        return " ".join(sentences)

    def _days(self, days: Sequence[str]) -> str:
        keys = [d.lower()[:3] for d in days]
        if not keys or len(keys) >= 7:
            return "every day"
        if keys == ["mon", "tue", "wed", "thu", "fri", "sat"]:
            return "Monday to Saturday"
        if keys == ["mon", "tue", "wed", "thu", "fri"]:
            return "Monday to Friday"
        return self.join(_DAYS_EN.get(k, k) for k in keys)


def _hour_en(moment: time) -> str:
    twelve = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    if moment.minute:
        return f"{twelve}:{moment.minute:02d} {suffix}"
    return f"{twelve} {suffix}"


# --------------------------------------------------------------------------- #


_HINDI = HindiPhrasebook()
_ENGLISH = EnglishPhrasebook()


def phrasebook_for(language: str | None) -> Phrasebook:
    """The phrasebook for a route code. Everything that is not English is
    composed in Hindi; the agent has the model render it for a third language
    (§11.1), with these sentences as the grounded source."""
    bare = (language or "").split("-")[0].lower()
    return _ENGLISH if bare == "en" else _HINDI


__all__ = (
    "ASK_KIND_EN",
    "ASK_KIND_HI",
    "ASK_PRODUCT_PRICE_EN",
    "ASK_PRODUCT_PRICE_HI",
    "ASK_PRODUCT_STOCK_EN",
    "ASK_PRODUCT_STOCK_HI",
    "CATEGORY_SPOKEN",
    "CATEGORY_SPOKEN_EN",
    "HEARING_OK_EN",
    "HEARING_OK_HI",
    "MACHINE_EN",
    "MACHINE_HI",
    "MISUNDERSTOOD_EN",
    "MISUNDERSTOOD_HI",
    "OVERVIEW_EN",
    "OVERVIEW_HI",
    "EnglishPhrasebook",
    "HindiPhrasebook",
    "Phrasebook",
    "money",
    "phrasebook_for",
    "plain",
)
