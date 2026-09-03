"""Answers the model never needs to write (§9 Tier 1, §11.2).

The commonest questions on an agri helpline have exactly one correct answer
and it lives in a table: what does DAP cost, is urea in stock, where is the
centre and when does it open. Sending those through a language model costs
the whole latency budget -- 1.3 to 1.7 s to the first token on the endpoint
this deployment uses -- to paraphrase a number the tool already returned, and
on the first live calls the model did worse than paraphrase: with no centre
resolved and no stock in front of it, it *invented* a stock-out.

So these turns are answered here, from the tool result, in fixed sentences
written in the register the farmers use. What this module does:

* resolves *which* product the farmer means -- from the words, the catalogue
  vocabulary, and the conversation's focus (the product named two turns ago
  is what "स्टॉक है क्या?" refers to);
* calls the same tools the model would (stock, price, details, centre), so
  every figure spoken is grounded in this turn's lookup;
* composes the reply from templates, and says which centre the answer is for
  when it is the head office rather than the farmer's own.

What it refuses to do: guess. A product it cannot resolve becomes a question
back to the farmer; a product the catalogue does not carry is said not to be
carried; a licensed product is handed to a person. Nothing here is a model,
so nothing here can be wrong in a new way each time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import structlog

from uaagro_domain.enums import Intent

from ..text.lexicon import Lexicon, LexiconEntry, Match
from ..text.numbers import digits_to_words
from ..text.script import whole_word
from ..tools.base import ToolContext, ToolRegistry, ToolResult
from .focus import CATEGORY_WORDS, CROP_WORDS, ConversationFocus, category_in, crop_in

log = structlog.get_logger(__name__)

#: A listing names this many products at most; more is a catalogue read out.
MAX_LISTED = 4
#: Two products may be offered as a choice; three is already a list.
MAX_CHOICE = 4

#: How each kind of product is said back to the farmer.
CATEGORY_SPOKEN: dict[str, str] = {
    "seeds": "बीज",
    "fertilisers": "खाद",
    "crop-protection": "दवा",
    "cattle-feed": "पशु आहार",
    "tools-equipment": "औज़ार",
}
#: The line for "what do you have", when no kind is named.
OVERVIEW_HI = "हमारे पास बीज, खाद, दवा, पशु आहार और खेती के औज़ार मिलते हैं। बताइए, क्या चाहिए?"
HEARING_OK_HI = "जी, साफ़ सुनाई दे रहा है। बताइए, क्या चाहिए?"
MACHINE_HI = "मैं यूए एग्रो का ऑटोमैटिक सहायक हूँ। चाहें तो किसी व्यक्ति से जोड़ दूँ?"
ASK_PRODUCT_PRICE_HI = "किस चीज़ का रेट पूछ रहे हैं? नाम बता दीजिए।"
ASK_PRODUCT_STOCK_HI = "किस चीज़ का स्टॉक देखना है? नाम बता दीजिए।"

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
_DAYS_HI = {
    "mon": "सोमवार",
    "tue": "मंगलवार",
    "wed": "बुधवार",
    "thu": "गुरुवार",
    "fri": "शुक्रवार",
    "sat": "शनिवार",
    "sun": "रविवार",
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

_OVERVIEW = re.compile(
    r"क्या[\s\-]*क्या|कौन[\s\-]*कौन|सब क्या|क्या सब|what all|what do you (have|sell|keep)|"
    r"क्या मिलता|क्या रखते|क्या बेचते",
    re.IGNORECASE,
)
_HEARING = re.compile(
    r"आवाज़|आवाज|सुनाई|सुन (रहे|पा|रहा)|^\s*(hello|हेलो|हैलो|हलो|हाँ|हां|जी)\s*[।.!?]*\s*$",
    re.IGNORECASE,
)
_MACHINE = re.compile(
    whole_word(
        "मशीन|रोबोट|रोबोट|कंप्यूटर|कम्प्यूटर|रिकॉर्डिंग|रिकार्डिंग|असली|bot|robot|machine|"
        "computer|recording|ai|एआई"
    ),
    re.IGNORECASE,
)
_CENTRE_COUNT = re.compile(
    whole_word(r"कितने|कितनी|कितना|कौन[\s\-]*कौन|किन|किस[\s\-]*किस|how many|which all|where all"),
    re.IGNORECASE,
)
_PRICE_WORDS = re.compile(
    whole_word("रेट|दाम|क़ीमत|कीमत|भाव|मूल्य|price|rate|cost|कितने का|कितने की|कितने रुपये"),
    re.IGNORECASE,
)


def spoken_centre_name(name: str) -> str:
    """ "लखनऊ सेंटर" for "नवीन खुशहाली किसान सेवा केंद्र - लखनऊ".

    The registered name is said once, in the greeting. In an answer the
    place is what identifies the centre, and the full brand read out before
    every price is six words a farmer waits through.
    """
    for separator in (" - ", " – ", " — "):  # noqa: RUF001
        if separator in name:
            place = name.rsplit(separator, 1)[1].strip()
            if place:
                return f"{place} सेंटर"
    return name


@dataclass(frozen=True, slots=True)
class CentreFacts:
    """What the agent may say about the centre it answers for."""

    id: str
    code: str
    name: str
    address_spoken: str | None
    phone: str | None
    open_time: time
    close_time: time
    working_days: tuple[str, ...]
    #: True when this is the farmer's own centre; False for the head office
    #: standing in for an unknown caller.
    own: bool

    @classmethod
    def of(cls, centre: Any, *, own: bool) -> CentreFacts:
        return cls(
            id=str(centre.id),
            code=str(centre.code),
            name=spoken_centre_name(str(centre.name_hi or centre.name)),
            address_spoken=centre.address_spoken_hi or centre.address,
            phone=centre.phone,
            open_time=centre.open_time,
            close_time=centre.close_time,
            working_days=tuple(centre.working_days or ()),
            own=own,
        )


@dataclass(frozen=True, slots=True)
class DirectAnswer:
    """A reply composed from data, ready to be spoken."""

    text: str
    intent: Intent
    results: list[ToolResult] = field(default_factory=list)
    #: The reply ended by offering a hand-over; a "हाँ" next means yes.
    offered_transfer: bool = False


@dataclass(frozen=True, slots=True)
class _Resolution:
    """What the words (and the focus) say the farmer is asking about."""

    product: LexiconEntry | None = None
    choices: tuple[LexiconEntry, ...] = ()
    ambiguous: tuple[LexiconEntry, ...] = ()
    category: str | None = None
    crop: str | None = None


@dataclass
class DirectAnswers:
    """The deterministic half of the turn handler."""

    registry: ToolRegistry
    context: ToolContext
    lexicon: Lexicon | None = None
    centre: CentreFacts | None = None
    #: Said once per call: that a stock or price answer is for the head office.
    _centre_explained: bool = field(default=False, repr=False)
    _by_sku: dict[str, LexiconEntry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.lexicon is not None:
            self._by_sku = {entry.sku: entry for entry in self.lexicon.entries}

    # -- entry point ------------------------------------------------------- #

    async def answer(
        self, transcript: str, intent: Intent, focus: ConversationFocus
    ) -> DirectAnswer | None:
        """A reply for this turn, or None when the model should answer."""
        if intent in (Intent.PRICE_ENQUIRY, Intent.PRODUCT_AVAILABILITY):
            return await self._stock_or_price(transcript, intent, focus)
        if intent is Intent.PRODUCT_COMPOSITION:
            return await self._composition(transcript, focus)
        if intent is Intent.CENTRE_LOCATION:
            return await self._centre_details(transcript)
        if intent is Intent.UNKNOWN:
            return self._small_talk(transcript, focus)
        return None

    # -- stock and price ----------------------------------------------------- #

    async def _stock_or_price(
        self, transcript: str, intent: Intent, focus: ConversationFocus
    ) -> DirectAnswer | None:
        if self.lexicon is None:
            return None
        asks_price = intent is Intent.PRICE_ENQUIRY or bool(_PRICE_WORDS.search(transcript))

        if _OVERVIEW.search(transcript):
            listing = self._listing(focus.category or category_in(transcript), focus.crop)
            if listing is not None:
                return DirectAnswer(text=listing, intent=intent)

        found = self._resolve(transcript, focus)
        if found.ambiguous:
            names = " और ".join(e.name_hi for e in found.ambiguous[:2])
            return DirectAnswer(text=f"दो चीज़ें हैं — {names}। कौन सी?", intent=intent)
        if found.product is None:
            if found.choices:
                return DirectAnswer(text=self._choice(found), intent=intent)
            if found.category is not None:
                return DirectAnswer(text=self._not_carried(found), intent=intent)
            text = ASK_PRODUCT_PRICE_HI if asks_price else ASK_PRODUCT_STOCK_HI
            return DirectAnswer(text=text, intent=intent)

        entry = found.product
        focus.note_product(entry.sku, entry.name_hi)
        if self.centre is None:
            # No centre to look stock up for. The catalogue still knows the
            # product; the honest answer is the price list and a question.
            return DirectAnswer(
                text=f"{entry.name_hi} हमारे पास मिलता है। आप किस सेंटर के पास हैं? वहाँ का स्टॉक बता दूँ।",
                intent=intent,
            )

        result = await self.registry.execute(
            "check_availability", {"sku": entry.sku, "centre_code": self.centre.code}, self.context
        )
        if not result.ok:
            log.info("direct.availability_failed", error=result.error)
            return None
        text = self._availability_text(entry, result.data, asks_price=asks_price)
        offered = bool(result.data.get("restricted"))
        return DirectAnswer(text=text, intent=intent, results=[result], offered_transfer=offered)

    def _availability_text(
        self, entry: LexiconEntry, data: Mapping[str, Any], *, asks_price: bool
    ) -> str:
        assert self.centre is not None
        name = str(data.get("name_hi") or entry.name_hi)
        centre = self.centre.name

        if data.get("restricted"):
            return f"{name} लाइसेंस वाली दवा है, इसके लिए सेंटर मैनेजर से बात करनी होगी। जोड़ दूँ?"

        if data.get("stocked") is False:
            sentence = f"{name} {centre} पर नहीं रखते।"
            return self._with_alternative(sentence, data)

        pack = _pack_spoken(str(data.get("pack") or ""))
        if data.get("available"):
            price = _money(data.get("price"))
            if asks_price:
                sentence = f"{name}, {pack}: {price} रुपये।" if pack else f"{name}: {price} रुपये।"
                sentence += f" {centre} पर स्टॉक में है।"
            else:
                detail = f"{pack} {price} रुपये में" if pack else f"{price} रुपये में"
                sentence = f"{name} स्टॉक में है, {detail}।"
            return self._with_centre_note(sentence)

        sentence = f"{name} अभी {centre} पर स्टॉक में नहीं है।"
        eta = _restock_spoken(data.get("restock_eta"))
        if eta:
            sentence += f" {eta} तक आ जाएगा।"
        return self._with_alternative(sentence, data)

    def _with_alternative(self, sentence: str, data: Mapping[str, Any]) -> str:
        alternatives = data.get("alternatives") or []
        if alternatives:
            first = alternatives[0]
            alt_name = str(first.get("name_hi") or "")
            alt_price = _money(first.get("price"))
            if alt_name:
                sentence += f" अभी {alt_name} है, {alt_price} रुपये में।"
            return sentence
        phone = self.centre.phone if self.centre is not None else None
        if phone:
            sentence += " आने से पहले सेंटर पर फ़ोन करके पक्का कर लीजिए।"
        return sentence

    def _with_centre_note(self, sentence: str) -> str:
        """Say once whose stock this is when it is not the farmer's own centre."""
        if self.centre is None or self.centre.own or self._centre_explained:
            return sentence
        self._centre_explained = True
        return f"{sentence} यह {self.centre.name} का रेट है।"

    # -- resolving the product ----------------------------------------------- #

    def _resolve(self, transcript: str, focus: ConversationFocus) -> _Resolution:
        assert self.lexicon is not None
        category = category_in(transcript)
        crop = crop_in(transcript)

        mentions = [m for m in self.lexicon.find_all(transcript) if self._entry(m) is not None]
        clear = [m for m in mentions if not m.ambiguous]
        if clear:
            entry = self._entry(clear[0])
            assert entry is not None
            return _Resolution(product=entry, category=category, crop=crop)
        if mentions:
            first = mentions[0]
            pair = tuple(
                e
                for e in (self._by_sku.get(first.sku), self._by_sku.get(first.runner_up_sku or ""))
                if e is not None
            )
            return _Resolution(ambiguous=pair, category=category, crop=crop)

        # No product named. A kind and a crop narrow the catalogue; if that
        # leaves one product it is the answer, a few is a choice, none is
        # "we do not carry it".
        kind = category or (focus.category if crop is not None else None)
        which_crop = crop or (focus.crop if category is not None else None)
        if kind is not None or which_crop is not None:
            candidates = self._candidates(kind, which_crop)
            if len(candidates) == 1:
                return _Resolution(product=candidates[0], category=kind, crop=which_crop)
            if 1 < len(candidates) <= MAX_CHOICE:
                return _Resolution(choices=tuple(candidates), category=kind, crop=which_crop)
            if len(candidates) > MAX_CHOICE:
                return _Resolution(
                    choices=tuple(candidates[:MAX_CHOICE]), category=kind, crop=which_crop
                )
            return _Resolution(category=kind or "unknown", crop=which_crop)

        # Nothing in the words at all: the product under discussion, if any.
        if focus.product is not None:
            entry = self._by_sku.get(focus.product.sku)
            if entry is not None:
                return _Resolution(product=entry)
        return _Resolution()

    def _entry(self, match: Match) -> LexiconEntry | None:
        return self._by_sku.get(match.sku)

    def _candidates(self, category: str | None, crop: str | None) -> list[LexiconEntry]:
        assert self.lexicon is not None
        out: list[LexiconEntry] = []
        for entry in self.lexicon.entries:
            if category is not None and entry.category != category:
                continue
            if crop is not None and crop not in entry.crops:
                continue
            out.append(entry)
        return out

    def _choice(self, found: _Resolution) -> str:
        names = _join(e.name_hi for e in found.choices)
        if found.crop is not None:
            return f"{_crop_spoken(found.crop)} के लिए हमारे पास {names} हैं। कौन सा चाहिए?"
        kind = CATEGORY_SPOKEN.get(found.category or "", "")
        if kind:
            return f"{kind} में हमारे पास {names} हैं। कौन सा चाहिए?"
        return f"हमारे पास {names} हैं। कौन सा चाहिए?"

    def _not_carried(self, found: _Resolution) -> str:
        kind = CATEGORY_SPOKEN.get(found.category or "", "")
        if found.crop is not None and kind:
            others = self._candidates(found.category, None)[:MAX_LISTED]
            sentence = f"{_crop_spoken(found.crop)} का {kind} अभी हमारे पास नहीं है।"
            if others:
                sentence += f" {kind} में {_join(e.name_hi for e in others)} हैं।"
            return sentence
        if found.crop is not None:
            return f"{_crop_spoken(found.crop)} के लिए अभी कुछ नहीं है। बीज, खाद या दवा — क्या चाहिए?"
        if kind:
            return f"{kind} में अभी कुछ नहीं है।"
        return ASK_PRODUCT_STOCK_HI

    def _listing(self, category: str | None, crop: str | None) -> str | None:
        if category is None:
            return OVERVIEW_HI
        kind = CATEGORY_SPOKEN.get(category)
        if kind is None:
            return OVERVIEW_HI
        items = self._candidates(category, crop)
        if not items and crop is not None:
            items = self._candidates(category, None)
        if not items:
            return f"{kind} में अभी कुछ नहीं है।"
        names = _join(e.name_hi for e in items[:MAX_LISTED])
        more = " और भी हैं।" if len(items) > MAX_LISTED else ""
        return f"{kind} में हमारे पास {names} हैं।{more} कौन सा चाहिए?"

    # -- composition ---------------------------------------------------------- #

    async def _composition(self, transcript: str, focus: ConversationFocus) -> DirectAnswer | None:
        if self.lexicon is None:
            return None
        found = self._resolve(transcript, focus)
        if found.product is None:
            return None
        entry = found.product
        focus.note_product(entry.sku, entry.name_hi)
        result = await self.registry.execute(
            "get_product_details", {"sku": entry.sku}, self.context
        )
        if not result.ok:
            return None
        data = result.data
        name = str(data.get("name_hi") or entry.name_hi)
        parts: list[str] = []
        for item in data.get("composition") or []:
            ingredient = str(item.get("ingredient") or "").strip()
            percent = item.get("percent")
            if not ingredient or percent is None:
                continue
            parts.append(f"{_ingredient_spoken(ingredient)} {_plain(percent)} परसेंट")
        if parts:
            text = f"{name} में {_join(parts)} है।"
        elif data.get("active_ingredients"):
            text = f"{name} में {_join(str(a) for a in data['active_ingredients'][:2])} है।"
        else:
            return None
        if data.get("restricted"):
            text += " यह लाइसेंस वाली दवा है, सेंटर मैनेजर से बात करनी होगी। जोड़ दूँ?"
            return DirectAnswer(
                text=text,
                intent=Intent.PRODUCT_COMPOSITION,
                results=[result],
                offered_transfer=True,
            )
        return DirectAnswer(text=text, intent=Intent.PRODUCT_COMPOSITION, results=[result])

    # -- the centre ------------------------------------------------------------ #

    async def _centre_details(self, transcript: str) -> DirectAnswer | None:
        if _CENTRE_COUNT.search(transcript):
            # "कितने सेंटर हैं?" is a question about the network, not a place;
            # the knowledge base answers it, the model puts it into words.
            return None
        place = self._place_named(transcript)
        if place is not None:
            result = await self.registry.execute(
                "find_nearest_centre", {"district": place}, self.context
            )
            centres = result.data.get("centres") if result.ok else None
            if centres:
                first = centres[0]
                text = _centre_sentence(
                    name=spoken_centre_name(str(first.get("name_hi") or "")),
                    address=first.get("address_spoken_hi"),
                    open_time=_parse_time(first.get("open")),
                    close_time=_parse_time(first.get("close")),
                    working_days=(),
                    phone=first.get("phone"),
                )
                return DirectAnswer(text=text, intent=Intent.CENTRE_LOCATION, results=[result])
        if self.centre is None:
            return None
        text = _centre_sentence(
            name=self.centre.name,
            address=self.centre.address_spoken,
            open_time=self.centre.open_time,
            close_time=self.centre.close_time,
            working_days=self.centre.working_days,
            phone=self.centre.phone,
        )
        return DirectAnswer(text=text, intent=Intent.CENTRE_LOCATION)

    def _place_named(self, transcript: str) -> str | None:
        if self.lexicon is None:
            return None
        lowered = transcript.lower()
        for place in self.lexicon.places:
            for form in (place.name_hi, place.name_en):
                if form and form.lower() in lowered:
                    return place.name_en
        return None

    # -- small talk ------------------------------------------------------------ #

    def _small_talk(self, transcript: str, focus: ConversationFocus) -> DirectAnswer | None:
        stripped = transcript.strip()
        words = len(stripped.split())
        if _OVERVIEW.search(stripped) and self.lexicon is not None:
            listing = self._listing(category_in(stripped) or focus.category, crop_in(stripped))
            if listing is not None:
                return DirectAnswer(text=listing, intent=Intent.PRODUCT_AVAILABILITY)
        if words <= 8 and _MACHINE.search(stripped):
            return DirectAnswer(text=MACHINE_HI, intent=Intent.OUT_OF_SCOPE, offered_transfer=True)
        if words <= 6 and _HEARING.search(stripped):
            return DirectAnswer(text=HEARING_OK_HI, intent=Intent.OUT_OF_SCOPE)
        return None


# --------------------------------------------------------------------------- #
# Wording helpers
# --------------------------------------------------------------------------- #


def _join(items: Any) -> str:
    """ "A, B और C" -- the way a list is said."""
    parts = [str(i) for i in items if str(i)]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " और " + parts[-1]


def _crop_spoken(crop: str) -> str:
    forms = CROP_WORDS.get(crop)
    return forms[0] if forms else crop


def _category_word(category: str) -> str:
    forms = CATEGORY_WORDS.get(category)
    return forms[0] if forms else category


def _money(value: Any) -> str:
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
    return str(int(amount.to_integral_value(rounding=ROUND_HALF_UP)))


def _plain(value: Any) -> str:
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


def _pack_spoken(pack: str) -> str:
    """ "50 kg" → "50 किलो का बैग"; "1 L" → "1 लीटर की बोतल"."""
    parts = pack.split()
    if len(parts) != 2:
        return pack
    amount, unit = parts[0], parts[1].lower()
    try:
        number = Decimal(amount)
    except InvalidOperation:
        return pack
    if unit in {"kg", "kilogram", "kilo"}:
        container = "का बैग" if number >= 25 else "का पैकेट"
        return f"{_money(number)} किलो {container}"
    if unit in {"g", "gm", "gram", "grams"}:
        return f"{_money(number)} ग्राम का पैकेट"
    if unit in {"l", "lt", "ltr", "litre", "liter"}:
        return f"{_money(number)} लीटर की बोतल"
    if unit in {"ml"}:
        return f"{_money(number)} एमएल की बोतल"
    if unit in {"pc", "pcs", "piece", "pieces", "unit", "units", "nos"}:
        return f"{_money(number)} पीस"
    return f"{_money(number)} {unit}"


def _restock_spoken(value: Any) -> str:
    if not value:
        return ""
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return ""
    return f"{day.day} {_MONTHS_HI[day.month - 1]}"


def _ingredient_spoken(ingredient: str) -> str:
    return _INGREDIENT_HI.get(ingredient.strip().lower(), ingredient)


def _parse_time(value: Any) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return None


def _hour_spoken(moment: time) -> str:
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


def _days_spoken(days: Sequence[str]) -> str:
    keys = [d.lower()[:3] for d in days]
    if not keys or len(keys) >= 7:
        return "हर दिन"
    if keys == ["mon", "tue", "wed", "thu", "fri", "sat"]:
        return "सोमवार से शनिवार"
    if keys == ["mon", "tue", "wed", "thu", "fri"]:
        return "सोमवार से शुक्रवार"
    return _join(_DAYS_HI.get(k, k) for k in keys)


def _centre_sentence(
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
        hours = f"{_hour_spoken(open_time)} से {_hour_spoken(close_time)} तक खुला रहता है"
        if working_days:
            hours += f", {_days_spoken(working_days)}"
        sentences.append(hours + "।")
    if phone:
        digits = re.sub(r"\D", "", phone)[-10:]
        if len(digits) == 10:
            sentences.append(f"फ़ोन नंबर {digits_to_words(digits, paired=True)}।")
    return " ".join(sentences)


__all__ = (
    "ASK_PRODUCT_PRICE_HI",
    "ASK_PRODUCT_STOCK_HI",
    "CATEGORY_SPOKEN",
    "HEARING_OK_HI",
    "MACHINE_HI",
    "OVERVIEW_HI",
    "CentreFacts",
    "DirectAnswer",
    "DirectAnswers",
    "spoken_centre_name",
)
