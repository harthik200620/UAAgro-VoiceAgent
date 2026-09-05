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
  vocabulary, the choice the agent just offered, and the conversation's focus
  (the product named two turns ago is what "स्टॉक है क्या?" refers to);
* calls the same tools the model would (stock, price, details, centre), so
  every figure spoken is grounded in this turn's lookup;
* hands what it found to a :class:`~voice_worker.flow.phrasebook.Phrasebook`,
  which says it in the caller's language and knows nothing about tools.

What it refuses to do: guess. A product it cannot resolve becomes a question
back to the farmer -- and never the same question a third time (§11.4): the
second miss asks differently and offers a person, the third hands over. A
product the catalogue does not carry is said not to be carried; a licensed
product is handed to a person. Nothing here is a model, so nothing here can
be wrong in a new way each time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import time
from typing import Any

import structlog

from uaagro_domain.enums import Intent

from ..text.lexicon import Lexicon, LexiconEntry, Match, normalise, similarity, stem
from ..text.script import whole_word
from ..tools.base import ToolContext, ToolRegistry, ToolResult
from .focus import (
    ConversationFocus,
    ProductInFocus,
    animals_fed_by,
    animals_in,
    category_in,
    crop_fits,
    crop_in,
    is_crop_word,
    is_kind_word,
    kind_word_in,
    squeeze,
)
from .phrasebook import (
    ASK_KIND_EN,
    ASK_KIND_HI,
    ASK_PRODUCT_PRICE_EN,
    ASK_PRODUCT_PRICE_HI,
    ASK_PRODUCT_STOCK_EN,
    ASK_PRODUCT_STOCK_HI,
    CATEGORY_SPOKEN,
    CATEGORY_SPOKEN_EN,
    HEARING_OK_EN,
    HEARING_OK_HI,
    MACHINE_EN,
    MACHINE_HI,
    MISUNDERSTOOD_EN,
    MISUNDERSTOOD_HI,
    OVERVIEW_EN,
    OVERVIEW_HI,
    Phrasebook,
    money,
    phrasebook_for,
)

log = structlog.get_logger(__name__)

#: A listing names this many products at most; more is a catalogue read out.
MAX_LISTED = 4
#: Two products may be offered as a choice; three is already a list.
MAX_CHOICE = 4
#: A third question back in a row is a hand-over, not a question (§11.4).
HANDOVER_AT = 3
#: A crop-only listing names this many per kind.
_PER_KIND = 3
#: Advice about a crop (§16.2) is never read as a yes-or-no about a product.
_ADVICE_INTENTS = frozenset(
    {Intent.CROP_RECOMMENDATION, Intent.DOSAGE_QUERY, Intent.PROBLEM_DIAGNOSIS}
)

#: "पहला वाला", "दूसरा", "the first one": which of the offered choices.
_ORDINALS: dict[str, int] = {
    "पहला": 0,
    "पहली": 0,
    "पहले": 0,
    "फर्स्ट": 0,
    "first": 0,
    "1st": 0,
    "एक नंबर": 0,
    "दूसरा": 1,
    "दूसरी": 1,
    "दूसरे": 1,
    "सेकंड": 1,
    "सेकेंड": 1,
    "second": 1,
    "2nd": 1,
    "तीसरा": 2,
    "तीसरी": 2,
    "तीसरे": 2,
    "थर्ड": 2,
    "third": 2,
    "3rd": 2,
    "चौथा": 3,
    "चौथी": 3,
    "चौथे": 3,
    "फोर्थ": 3,
    "fourth": 3,
    "4th": 3,
    "आख़िरी": -1,
    "आखिरी": -1,
    "आखरी": -1,
    "लास्ट": -1,
    "last": -1,
}
_ORDINAL = re.compile(
    whole_word("|".join(re.escape(w) for w in sorted(_ORDINALS, key=len, reverse=True))),
    re.IGNORECASE,
)
#: "सबका रेट", "दोनों का", "all of them": every offered choice at once.
_ALL_OF_THEM = re.compile(whole_word("सब|सबका|सबके|सभी|दोनों|तीनों|चारों|all|both|each"), re.IGNORECASE)
#: How close a farmer's word must be to one of the *offered* names. Looser
#: than the catalogue-wide threshold: the field is two to four products the
#: agent itself just named, so a near miss cannot land on a stranger.
_CHOICE_THRESHOLD = 0.6
_CHOICE_MARGIN = 0.08
#: Longest phrase, in tokens, compared against an offered name.
_CHOICE_PHRASE_TOKENS = 3
#: A token this short is a particle or a stray letter, not a product word.
_SHORT_TOKEN = 1
#: Words that carry nothing when deciding whether a turn is *only* animals.
_FILLER = frozenset(
    {
        "और",
        "and",
        "या",
        "or",
        "जी",
        "हाँ",
        "हां",
        "नहीं",
        "no",
        "yes",
        "the",
        "a",
        "में",
        "पर",
        "on",
        "in",
    }
)

#: "What have you got?" -- for cows, in seeds, at all. The answer is a list,
#: not a question back, and asking it twice is not the farmer failing to
#: choose: on the first live calls "कौस के लिए क्या-क्या दे सकते हैं?" after a
#: list was booked as a miss, changed the agent's tack, and the repeat handed
#: the call to a person. Broad on purpose; it only applies when the words
#: name no product, so "यूरिया क्या मिलेगा" still looks urea up.
_OVERVIEW = re.compile(
    r"क्या[\s\-]*क्या|कौन[\s\-]*कौन|कौन[\s\-]*(सी|से|सा) (चीज़|चीज|चीज़ें|चीजें|सामान|प्रोडक्ट)|"
    r"सब क्या|क्या सब|क्या मिलता|क्या मिलती|क्या मिलेगा|क्या मिलेगी|क्या मिल सकता|क्या मिल सकती|"
    r"क्या रखते|क्या बेचते|क्या दे (सकते|सकती|सकता|पाएंगे|पाओगे|दोगे|देंगे)|क्या (है|हैं) आपके पास|"
    r"आपके पास क्या|क्या उपलब्ध|क्या ऑप्शन|ऑप्शन|options|"
    r"what all|what (do|did|can|could|would) (you|u) "
    r"(have|sell|keep|stock|offer|carry|give|provide|suggest)|"
    r"(what|which)\b[^.?!]{0,40}\b(is|are) (there|available|in stock)|"
    r"what can (i|we) get|which (all|ones)|show me|list|what else|anything else|और क्या",
    re.IGNORECASE,
)
#: "Is this for cows?", "वो गाय का ही है न?", "can I give it to buffaloes?":
#: a yes-or-no about who a product or a kind is for. On the 10:39 call five
#: of these in a row -- "is it cow feed only?", "can be used for cow?", "cow
#: and buffalo?" -- were each answered with the product list, and "can" even
#: matched the fertiliser CAN. Read only when an animal is named or in focus.
_SUITABILITY = re.compile(
    r"ही है न|ही है ना|है न[ा]?\s*[?]|है ना\b|ना\s*[?]|न\s*[?]|"
    r"के लिए (है|हैं|चलेगा|चलेगी|ठीक|सही|काम|बढ़िया|अच्छा|दे सकते|दे सकता|दे सकती|खिला|इस्तेमाल|यूज़|यूज)|"
    r"को (भी )?(दे|खिला|दिया|खिलाया) (सकते|सकता|सकती|जा)|खिला सकते|खिलाना|खिला दें|खिला दूँ|खिला दूं|"
    r"को भी (दे|खिला|चलेगा|ठीक)|के लिए भी|"
    r"चलेगा|चलेगी|ठीक रहेगा|सही रहेगा|काम करेगा|"
    r"(में|पर) (डाल|छिड़क|लगा|दे) (सकते|सकता|सकती)|(में|पर) (डालें|डालूँ|डालूं|डालना|छिड़कें|लगाएँ|लगाएं)|"
    r"(में|पर) (चलेगा|चलेगी|ठीक|सही|काम)|"
    r"can (i|we|it|this|that|you|one) (use|give|feed|spray|apply|put|be used|be given)|"
    r"can be (used|given|fed|sprayed|applied)|"
    r"(is|it's|its|isn't|is it|are|these are) (it |this |that |these |they |those )?"
    r"(for|only for|meant for|good for|ok for|okay for|fine for|suitable|usable)|"
    r"suitable|works? (for|on)|includes?|right\s*[?]|only\s*[?]|also\s*[?]|too\s*[?]|"
    r"(spray|apply|put|use) (\S+ ){0,3}?(on|in|for)\b",
    re.IGNORECASE,
)
#: "हाँ", "बताओ", "yes": the farmer accepting what the agent just offered.
_YES = re.compile(
    whole_word(
        "हाँ|हां|हा|जी|ठीक|ठीक है|बताओ|बताइए|बता दो|बता दीजिए|बताइये|yes|yeah|ok|okay|sure|please"
    ),
    re.IGNORECASE,
)
#: Latin-script catalogue forms that are also English function words. "can be
#: used" is not the fertiliser CAN.
_NOT_A_NAME = frozenset(
    {"can", "cans", "will", "may", "one", "all", "for", "the", "and", "not", "use", "its", "it"}
)
#: "और क्या-क्या है?", "what else?": the list continues past what was read.
_MORE = re.compile(
    whole_word("और|अलावा|बाकी|बाक़ी|दूसरे|अन्य|else|other|others|more|rest|remaining"),
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
    whole_word(
        "रेट|दाम|क़ीमत|कीमत|भाव|मूल्य|प्राइस|प्राइसेस|कॉस्ट|price|prices|rate|cost|"
        "कितने का|कितने की|कितने रुपये|कितना है|kitne ka|kitne ki|kitna|bhav|daam|keemat|kimat"
    ),
    re.IGNORECASE,
)


def spoken_centre_name(name: str, *, language: str = "hi-IN") -> str:
    """ "लखनऊ सेंटर" for "नवीन खुशहाली किसान सेवा केंद्र - लखनऊ".

    The registered name is said once, in the greeting. In an answer the
    place is what identifies the centre, and the full brand read out before
    every price is six words a farmer waits through.
    """
    word = "centre" if language.lower().startswith("en") else "सेंटर"
    for separator in (" - ", " – ", " — "):  # noqa: RUF001
        if separator in name:
            place = name.rsplit(separator, 1)[1].strip()
            if place:
                return f"{place} {word}"
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
    #: "Lucknow centre", for a caller speaking English.
    name_en: str = ""
    address_en: str | None = None

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
            name_en=spoken_centre_name(str(centre.name or centre.name_hi), language="en-IN"),
            address_en=centre.address,
        )


@dataclass(frozen=True, slots=True)
class DirectAnswer:
    """A reply composed from data, ready to be spoken."""

    text: str
    intent: Intent
    results: list[ToolResult] = field(default_factory=list)
    #: The reply ended by offering a hand-over; a "हाँ" next means yes.
    offered_transfer: bool = False
    #: False when the reply is a question back -- a choice, "which one?",
    #: "name it" -- rather than an answer. Drives the §11.4 repeat counter.
    resolved: bool = True
    #: The agent has given up understanding and the line names a hand-over.
    handover: bool = False
    #: The reply answered *and* left the offered choice standing, so "दूसरा"
    #: still means something next turn ("सबका रेट" reads every price).
    keeps_choices: bool = False
    #: The language the text is written in. Hindi and English are composed
    #: here; anything else is Hindi for the agent to have rendered.
    language: str = "hi-IN"


@dataclass(frozen=True, slots=True)
class _Resolution:
    """What the words (and the focus) say the farmer is asking about."""

    product: LexiconEntry | None = None
    choices: tuple[LexiconEntry, ...] = ()
    ambiguous: tuple[LexiconEntry, ...] = ()
    category: str | None = None
    crop: str | None = None
    #: The choice was cut short: more of the kind exist than were offered.
    more: bool = False
    #: A crop and no kind: the answer is a listing across the kinds.
    spans_kinds: bool = False


#: The farmer wants every offered product priced, not one of them.
_ALL = "all"
#: A turn the direct layer has read and decided the model should answer:
#: a crop question about a product the catalogue does not tag by crop.
_MODEL = DirectAnswer(text="", intent=Intent.UNKNOWN, resolved=False)


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
    #: The sentences of the language the caller was heard in this turn.
    words: Phrasebook = field(default_factory=lambda: phrasebook_for("hi-IN"), repr=False)

    def __post_init__(self) -> None:
        if self.lexicon is not None:
            self._by_sku = {entry.sku: entry for entry in self.lexicon.entries}

    # -- entry point ------------------------------------------------------- #

    async def answer(
        self,
        transcript: str,
        intent: Intent,
        focus: ConversationFocus,
        *,
        language: str = "hi-IN",
    ) -> DirectAnswer | None:
        """A reply for this turn, or None when the model should answer."""
        self.words = phrasebook_for(language)
        self.words.said_kind = focus.kind_word
        reply = await self._reply(transcript, intent, focus)
        if reply is None:
            return None
        if reply.text == focus.last_reply and not reply.handover:
            # The same sentence twice running is what a farmer hears as not
            # being listened to. Own the repeat, at least.
            focus.last_reply = reply.text
            return replace(reply, text=self.words.again(reply.text), language=self.words.code)
        focus.last_reply = reply.text
        return replace(reply, language=self.words.code)

    async def _reply(
        self, transcript: str, intent: Intent, focus: ConversationFocus
    ) -> DirectAnswer | None:
        accepted = await self._accept_offer(transcript, focus)
        if accepted is not None:
            return self._settle(accepted, focus, progress=True)
        about_animals = await self._animal_question(transcript, focus, intent)
        if about_animals is not None:
            return self._settle(about_animals, focus, progress=True)
        about_crop = await self._crop_question(transcript, focus, intent)
        if about_crop is _MODEL:
            return None
        if about_crop is not None:
            return self._settle(about_crop, focus, progress=True)
        if intent is Intent.UNKNOWN:
            # "आप मशीन हो?" carries a word the tools category also claims.
            # Small talk is settled first, so it never becomes a product.
            chatter = await self._small_talk(transcript, focus)
            if chatter is not None:
                return self._settle(chatter, focus, progress=False)
            if self._names_something(transcript, focus):
                # A product, a kind or a crop said on its own -- "मसूर का
                # बीज", or "मसूरी" in answer to a choice -- is a request for
                # it. The rule classifier needs a verb it did not hear.
                intent = Intent.PRODUCT_AVAILABILITY
            elif focus.misses >= HANDOVER_AT - 1:
                # Twice asked, twice nothing usable, and still nothing: the
                # model would only ask a third way. A person, instead.
                return self._settle(self._ask(intent, asks_price=False), focus, progress=False)

        progress = self._names_something(transcript, focus)
        reply = await self._answer(transcript, intent, focus)
        if reply is None:
            return None
        return self._settle(reply, focus, progress=progress)

    async def _answer(
        self, transcript: str, intent: Intent, focus: ConversationFocus
    ) -> DirectAnswer | None:
        if intent in (Intent.PRICE_ENQUIRY, Intent.PRODUCT_AVAILABILITY):
            return await self._stock_or_price(transcript, intent, focus)
        if intent is Intent.PRODUCT_COMPOSITION:
            return await self._composition(transcript, focus)
        if intent is Intent.CENTRE_LOCATION:
            return await self._centre_details(transcript)
        if intent is Intent.UNKNOWN:
            return await self._small_talk(transcript, focus)
        return None

    def _settle(
        self, reply: DirectAnswer, focus: ConversationFocus, *, progress: bool
    ) -> DirectAnswer:
        """Book-keep the miss counter and the offered choice; change tack."""
        if reply.resolved:
            focus.misses = 0
            if not reply.keeps_choices:
                focus.choices = ()
            return reply
        if progress:
            # The farmer said something usable -- a crop that narrowed
            # sixteen seeds to two -- and is being asked a *new* question.
            # That is the conversation moving, not the agent failing.
            focus.misses = 0
        return self._change_tack(reply, focus, focus.note_miss())

    def _change_tack(
        self, reply: DirectAnswer, focus: ConversationFocus, misses: int
    ) -> DirectAnswer:
        """Never the same question a third time (§11.4).

        The first miss asks. The second asks differently -- the kinds, or
        how to answer a choice -- and offers a person. The third stops
        asking and hands over.
        """
        if misses <= 1:
            return reply
        if misses >= HANDOVER_AT:
            return DirectAnswer(
                text=self.words.misunderstood(), intent=reply.intent, resolved=False, handover=True
            )
        if focus.choices:
            # Every name that was offered, not the first two: a farmer who
            # wanted the third of four must still hear it.
            offered = self._entries(focus.choices)[:MAX_CHOICE]
            names = self.words.either(self.words.product(e) for e in offered)
            return DirectAnswer(
                text=self.words.pick_by_position(names, count=len(offered)),
                intent=reply.intent,
                resolved=False,
            )
        if focus.category is not None:
            listing = self._listing(focus.category, focus.crop, focus)
            if listing is not None and focus.choices:
                return DirectAnswer(
                    text=self.words.listing_or_person(listing),
                    intent=reply.intent,
                    resolved=False,
                    offered_transfer=True,
                )
        return DirectAnswer(
            text=self.words.ask_kind(), intent=reply.intent, resolved=False, offered_transfer=True
        )

    # -- what the words name ----------------------------------------------- #

    def _names_something(self, transcript: str, focus: ConversationFocus) -> bool:
        if self.lexicon is None:
            return False
        if focus.choices and self._from_choices(transcript, focus) is not None:
            return True
        if category_in(transcript) is not None or crop_in(transcript) is not None:
            return True
        return self._names_product(transcript)

    def _names_product(self, transcript: str) -> bool:
        return bool(self._mentions(transcript))

    def _mentions(self, transcript: str) -> list[Match]:
        """The products the words name -- by a name of their own.

        A product called after its kind ("पशु आहार", the plain pellet, in
        the पशु आहार category) is matched by the kind's name, and a farmer
        who said only the kind was asking about the kind: those mentions are
        left to the kind-and-crop narrowing, which lists what there is.
        """
        if self.lexicon is None:
            return []
        return [
            m
            for m in self.lexicon.find_all(transcript)
            if self._entry(m) is not None
            and not is_kind_word(m.matched_text)
            and not is_crop_word(m.matched_text)
            and m.matched_text.lower() not in _NOT_A_NAME
        ]

    def _entry(self, match: Match) -> LexiconEntry | None:
        return self._by_sku.get(match.sku)

    def _entries(self, choices: tuple[ProductInFocus, ...]) -> list[LexiconEntry]:
        return [e for e in (self._by_sku.get(c.sku) for c in choices) if e is not None]

    def _ask(self, intent: Intent, *, asks_price: bool) -> DirectAnswer:
        return DirectAnswer(
            text=self.words.ask_product(asks_price=asks_price), intent=intent, resolved=False
        )

    # -- stock and price ----------------------------------------------------- #

    async def _stock_or_price(
        self, transcript: str, intent: Intent, focus: ConversationFocus
    ) -> DirectAnswer | None:
        if self.lexicon is None:
            return None
        asks_price = intent is Intent.PRICE_ENQUIRY or bool(_PRICE_WORDS.search(transcript))

        picked = self._from_choices(transcript, focus)
        if picked is None and focus.choices and asks_price and not self._names_product(transcript):
            # "उसका रेट बता दो" straight after a list: "उसका" is the list.
            picked = _ALL
        if picked == _ALL:
            return await self._prices_for(focus.choices, intent)

        if picked is None:
            overview = await self._overview(transcript, focus, intent)
            if overview is not None:
                return overview

        found = (
            _Resolution(product=picked)
            if isinstance(picked, LexiconEntry)
            else self._resolve(transcript, focus)
        )
        if found.product is None:
            return self._without_product(found, intent, focus, asks_price=asks_price)
        return await self._lookup(found.product, intent, focus, asks_price=asks_price)

    async def _overview(
        self, transcript: str, focus: ConversationFocus, intent: Intent
    ) -> DirectAnswer | None:
        """ "What have you got for cows?": the list, as an *answer*.

        The kind in the words beats the kind in focus ("खाद में क्या-क्या
        है?" after a seed list is about fertiliser), the crop carries over
        only while the kind does, and a kind with a single product is looked
        up outright -- a list of one is a stock question in disguise. The
        list read out stays the offered choice, so "पहला वाला" still works;
        the miss counter does not move, because the farmer asked something
        and was told.
        """
        if not _OVERVIEW.search(transcript) or self._names_product(transcript):
            return None
        crop = crop_in(transcript)
        category = category_in(transcript) or (
            focus.category if crop is None else self._inherited_kind(focus, crop)
        )
        if crop is None and category == focus.category:
            crop = focus.crop
        if category is not None:
            items = self._candidates(category, crop) or self._candidates(category, None)
            if len(items) == 1:
                asks_price = bool(_PRICE_WORDS.search(transcript))
                return await self._lookup(items[0], intent, focus, asks_price=asks_price)
        mismatch = self._wrong_animals(category, focus)
        if mismatch is not None:
            return mismatch
        # "और क्या-क्या है?" after a list: the ones not read out yet.
        after = focus.listed if focus.listed and _MORE.search(transcript) else ()
        listing = self._listing(category, crop, focus, after=after)
        if listing is None:
            return None
        return DirectAnswer(
            text=listing,
            intent=Intent.PRODUCT_AVAILABILITY,
            keeps_choices=bool(focus.choices) and (category is not None or crop is not None),
        )

    def _without_product(
        self, found: _Resolution, intent: Intent, focus: ConversationFocus, *, asks_price: bool
    ) -> DirectAnswer:
        """The words did not settle on one product: a choice, a refusal, a question."""
        if found.ambiguous:
            pair = found.ambiguous[:2]
            focus.offer(tuple(ProductInFocus(e.sku, e.name_hi) for e in pair))
            names = [self.words.product(e) for e in pair]
            return DirectAnswer(text=self.words.ambiguous(names), intent=intent, resolved=False)
        mismatch = self._wrong_animals(found.category, focus)
        if mismatch is not None:
            return mismatch
        if found.spans_kinds and found.crop is not None:
            listing = self._crop_listing(found.crop, focus)
            if listing is None:
                return DirectAnswer(
                    text=self.words.nothing_for_crop(self.words.crop(found.crop)), intent=intent
                )
            return DirectAnswer(text=listing, intent=intent, resolved=False)
        if found.choices:
            focus.offer(tuple(ProductInFocus(e.sku, e.name_hi) for e in found.choices))
            return DirectAnswer(
                text=self._choice(found, focus.animals), intent=intent, resolved=False
            )
        if found.category is not None:
            return DirectAnswer(text=self._not_carried(found), intent=intent)
        return self._ask(intent, asks_price=asks_price)

    # -- a standing offer ------------------------------------------------------ #

    async def _accept_offer(self, transcript: str, focus: ConversationFocus) -> DirectAnswer | None:
        """ "हाँ" after "पशु आहार बताऊँ?" or "रेट और स्टॉक बताऊँ?" does it.

        The offer stands for exactly one turn; anything but a short yes
        drops it and is read on its own.
        """
        offered, focus.offered = focus.offered, None
        if offered is None:
            return None
        if len(transcript.split()) > 4 or _YES.search(transcript) is None:
            return None
        what, which = offered
        if what == "listing":
            listing = self._listing(which, None, focus)
            if listing is None:
                return None
            return DirectAnswer(text=listing, intent=Intent.PRODUCT_AVAILABILITY)
        entry = self._by_sku.get(which)
        if entry is None:
            return None
        return await self._lookup(entry, Intent.PRICE_ENQUIRY, focus, asks_price=True)

    # -- animals ------------------------------------------------------------- #

    async def _animal_question(
        self, transcript: str, focus: ConversationFocus, intent: Intent
    ) -> DirectAnswer | None:
        """ "Is this for cows?" -- answered from what the kind is for.

        Fires on a suitability question with an animal named or in focus,
        and on an animal named on its own ("Cow and buffalo?") while a kind
        is under discussion. A request ("गाय का दाना चाहिए") is not a
        question about suitability and is left to the stock path, which
        speaks of the animal too.
        """
        if self.lexicon is None:
            return None
        named = animals_in(transcript)
        asks = _SUITABILITY.search(transcript) is not None and not _OVERVIEW.search(transcript)
        bare = named and self._only_animals(transcript)
        if not (asks and (named or focus.animals)) and not bare:
            return None
        animals = named or focus.animals
        if not animals:
            return None

        words = self.words
        mentions = [m for m in self._mentions(transcript) if not m.ambiguous]
        entry = self._entry(mentions[0]) if mentions else None
        if entry is None and focus.product is not None and not focus.choices:
            entry = self._by_sku.get(focus.product.sku)
        category = kind_word_in(transcript)
        kind = category[0] if category is not None else (entry.category if entry else None)
        if kind is None:
            kind = focus.category
        if kind is None:
            return None

        fed = animals_fed_by(kind)
        if entry is not None and not fed:
            # "Can I give urea to the cows?" -- it is not feed at all.
            focus.category = "cattle-feed"
            focus.animals = animals
            focus.offered = ("listing", "cattle-feed")
            return DirectAnswer(
                text=words.not_feed(
                    words.product(entry), words.kind(entry.category), words.animals(animals)
                ),
                intent=Intent.PRODUCT_AVAILABILITY,
                offered_transfer=False,
            )
        if not fed:
            return None
        wrong = tuple(a for a in animals if a not in fed)
        subject = words.product(entry) if entry is not None else words.kind_said(kind)
        if wrong:
            focus.animals = tuple(a for a in animals if a in fed)
            return DirectAnswer(
                text=words.not_for_animal(subject, words.animals(fed), words.animals(wrong)),
                intent=Intent.PRODUCT_AVAILABILITY,
                offered_transfer=True,
                keeps_choices=True,
            )
        focus.animals = animals
        if entry is not None:
            focus.note_product(entry.sku, entry.name_hi)
            focus.offered = ("lookup", entry.sku)
            return DirectAnswer(
                text=words.suits(subject, words.animals(animals)),
                intent=Intent.PRODUCT_AVAILABILITY,
            )
        items = self._candidates(kind, None)[:MAX_LISTED]
        focus.offer(tuple(ProductInFocus(e.sku, e.name_hi) for e in items))
        names = words.join(words.product(e) for e in items)
        text = words.suits_all(names, words.animals(animals))
        if text == focus.last_reply:
            # Asked a second way, answered a second way -- not read out again.
            text = words.suits_again(words.animals(animals))
        return DirectAnswer(text=text, intent=Intent.PRODUCT_AVAILABILITY, keeps_choices=True)

    # -- crops --------------------------------------------------------------- #

    async def _crop_question(
        self, transcript: str, focus: ConversationFocus, intent: Intent
    ) -> DirectAnswer | None:
        """ "Is this for wheat?", "गेहूँ में डाल सकते हैं?" -- from the crop tags.

        The catalogue tags seeds and sprays with the crops they are for;
        that is a fact about the product, and it is answered here. What to
        put on a crop, and how much, is advice and stays on the advice path
        (§16.2) -- so the advice intents are not read as this.
        """
        if self.lexicon is None or intent in _ADVICE_INTENTS:
            return None
        named = crop_in(transcript)
        asks = _SUITABILITY.search(transcript) is not None and not _OVERVIEW.search(transcript)
        # "और धान में?" straight after "yes, it is for wheat": the same
        # question about the next crop, with the product still in hand.
        bare = (
            named is not None
            and focus.product is not None
            and not focus.choices
            and self._only_crop(transcript)
        )
        if not asks and not bare:
            return None
        crop = named or focus.crop
        if crop is None or animals_in(transcript):
            return None
        mentions = [m for m in self._mentions(transcript) if not m.ambiguous]
        entry = self._entry(mentions[0]) if mentions else None
        if entry is None and focus.product is not None and not focus.choices:
            entry = self._by_sku.get(focus.product.sku)
        if entry is None:
            return None
        if not entry.crops:
            # A product the catalogue does not tag by crop (fertiliser,
            # feed, tools): nothing here to say yes or no from, and a stock
            # figure is not an answer to "can I put it on wheat?". The model
            # has the knowledge base. A crop said on its own is read as a
            # new subject instead.
            return _MODEL if asks else None
        words = self.words
        name = words.product(entry)
        crops = words.join(words.crop(c) for c in entry.crops)
        focus.crop = crop
        if crop_fits(crop, entry.crops):
            focus.note_product(entry.sku, entry.name_hi)
            focus.offered = ("lookup", entry.sku)
            return DirectAnswer(
                text=words.suits_crop(name, words.crop(crop), crops),
                intent=Intent.PRODUCT_AVAILABILITY,
            )
        others = self._candidates(entry.category, crop)[:MAX_LISTED]
        focus.offer(tuple(ProductInFocus(e.sku, e.name_hi) for e in others))
        return DirectAnswer(
            text=words.not_for_crop(
                name,
                crops,
                words.crop(crop),
                words.join(words.product(e) for e in others),
                words.kind_said(entry.category),
            ),
            intent=Intent.PRODUCT_AVAILABILITY,
            keeps_choices=bool(others),
        )

    def _crop_listing(self, crop: str, focus: ConversationFocus | None) -> str | None:
        """Everything for a crop, kind by kind: "गेहूँ के लिए बीज में ..., दवा में ...".

        A crop named on its own spans the catalogue. Listing four sprays and
        seeds unlabelled, cut at four, told the farmer nothing about the
        shape of what there is.
        """
        assert self.lexicon is not None
        groups: list[tuple[str, str, bool]] = []
        offered: list[ProductInFocus] = []
        for category in CATEGORY_SPOKEN:
            if not self._kind_tagged(category):
                continue
            items = self._candidates(category, crop)
            if not items:
                continue
            shown = items[:_PER_KIND]
            offered.extend(ProductInFocus(e.sku, e.name_hi) for e in shown)
            names = self.words.join(self.words.product(e) for e in shown)
            groups.append((self.words.kind_said(category), names, len(items) > _PER_KIND))
        if not groups:
            return None
        if focus is not None:
            focus.offer(tuple(offered))
        return self.words.crop_listing(self.words.crop(crop), groups)

    def _kind_tagged(self, category: str) -> bool:
        """Whether any product of the kind carries crop tags at all."""
        assert self.lexicon is not None
        return any(e.crops for e in self.lexicon.entries if e.category == category)

    @staticmethod
    def _only_crop(transcript: str) -> bool:
        """ "और धान में?" -- a crop and nothing else worth reading."""
        tokens = [t for t in normalise(squeeze(transcript)).split() if t not in _FILLER]
        rest = [t for t in tokens if crop_in(t) is None]
        return len(rest) <= 1

    @staticmethod
    def _only_animals(transcript: str) -> bool:
        """ "Cow and buffalo?" -- animals and nothing else worth reading."""
        tokens = [t for t in normalise(squeeze(transcript)).split() if t not in _FILLER]
        rest = [t for t in tokens if not animals_in(t)]
        return len(rest) <= 1

    def _wrong_animals(
        self, category: str | None, focus: ConversationFocus, *, subject: str | None = None
    ) -> DirectAnswer | None:
        """The kind about to be listed is not for an animal the farmer named."""
        if not focus.animals or category is None:
            return None
        fed = animals_fed_by(category)
        if not fed:
            return None
        wrong = tuple(a for a in focus.animals if a not in fed)
        if not wrong:
            return None
        words = self.words
        focus.animals = tuple(a for a in focus.animals if a in fed)
        return DirectAnswer(
            text=words.not_for_animal(
                subject or words.kind_said(category), words.animals(fed), words.animals(wrong)
            ),
            intent=Intent.PRODUCT_AVAILABILITY,
            offered_transfer=True,
        )

    async def _lookup(
        self, entry: LexiconEntry, intent: Intent, focus: ConversationFocus, *, asks_price: bool
    ) -> DirectAnswer | None:
        """One product resolved: its stock and price at the centre."""
        mismatch = self._wrong_animals(entry.category, focus, subject=self.words.product(entry))
        if mismatch is not None:
            return mismatch
        focus.note_product(entry.sku, entry.name_hi)
        if self.centre is None:
            # No centre to look stock up for. The catalogue still knows the
            # product; the honest answer is the price list and a question.
            text = self.words.carried_no_centre(self.words.product(entry))
            return DirectAnswer(text=text, intent=intent)

        result = await self._availability(entry)
        if result is None:
            return None
        text = self._availability_text(entry, result.data, asks_price=asks_price)
        offered = bool(result.data.get("restricted"))
        return DirectAnswer(text=text, intent=intent, results=[result], offered_transfer=offered)

    async def _availability(self, entry: LexiconEntry) -> ToolResult | None:
        assert self.centre is not None
        result = await self.registry.execute(
            "check_availability", {"sku": entry.sku, "centre_code": self.centre.code}, self.context
        )
        if not result.ok:
            log.info("direct.availability_failed", error=result.error)
            return None
        return result

    def _availability_text(
        self, entry: LexiconEntry, data: Mapping[str, Any], *, asks_price: bool
    ) -> str:
        words = self.words
        name = words.product(entry, data)
        centre = self._centre_name()

        if data.get("restricted"):
            return words.restricted(name)
        if data.get("stocked") is False:
            return self._with_alternative(words.not_stocked(name, centre), data)

        pack = words.pack(str(data.get("pack") or ""))
        if data.get("available"):
            price = money(data.get("price"))
            sentence = words.in_stock(name, pack, price, centre, asks_price=asks_price)
            return self._with_centre_note(sentence)

        eta = words.restock(data.get("restock_eta"))
        return self._with_alternative(words.out_of_stock(name, centre, eta), data)

    def _with_alternative(self, sentence: str, data: Mapping[str, Any]) -> str:
        alternatives = data.get("alternatives") or []
        if alternatives:
            first = alternatives[0]
            name = first.get("name_en") if self.words.code.startswith("en") else None
            alt_name = str(name or first.get("name_hi") or "")
            if alt_name:
                sentence += self.words.alternative(alt_name, money(first.get("price")))
            return sentence
        if self.centre is not None and self.centre.phone:
            sentence += self.words.call_ahead()
        return sentence

    def _with_centre_note(self, sentence: str) -> str:
        """Say once whose stock this is when it is not the farmer's own centre."""
        if self.centre is None or self.centre.own or self._centre_explained:
            return sentence
        self._centre_explained = True
        return sentence + self.words.centre_note(self._centre_name())

    def _centre_name(self) -> str:
        assert self.centre is not None
        return self.words.centre(self.centre.name, self.centre.name_en)

    # -- resolving the product ----------------------------------------------- #

    def _resolve(self, transcript: str, focus: ConversationFocus) -> _Resolution:
        """What the words name: a product, a pair to choose between, a kind
        and crop that narrow the catalogue, or the product already in focus."""
        assert self.lexicon is not None
        category = category_in(transcript)
        crop = crop_in(transcript)

        mentions = self._mentions(transcript)
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
        kind = category or self._inherited_kind(focus, crop)
        which_crop = crop or (focus.crop if category is not None else None)
        if kind is not None or which_crop is not None:
            return self._narrowed(kind, which_crop)

        # Nothing in the words at all: the product under discussion, if any.
        if focus.product is not None:
            entry = self._by_sku.get(focus.product.sku)
            if entry is not None:
                return _Resolution(product=entry)
        return _Resolution()

    def _inherited_kind(self, focus: ConversationFocus, crop: str | None) -> str | None:
        """The kind a crop said on its own narrows: the one under discussion,
        if the catalogue tags that kind by crop at all. "धान" after a seed
        list is the paddy seeds; "बाजरा" after the fertilisers is a new
        question, because no fertiliser is tagged for any crop."""
        if crop is None or focus.category is None:
            return None
        return focus.category if self._kind_tagged(focus.category) else None

    def _narrowed(self, kind: str | None, crop: str | None) -> _Resolution:
        if kind is None and crop is not None:
            # The crop alone spans the catalogue: listed by kind, not as a
            # choice of four unlabelled products.
            return _Resolution(crop=crop, spans_kinds=True)
        candidates = self._candidates(kind, crop)
        if len(candidates) == 1:
            return _Resolution(product=candidates[0], category=kind, crop=crop)
        if candidates:
            return _Resolution(
                choices=tuple(candidates[:MAX_CHOICE]),
                category=kind,
                crop=crop,
                more=len(candidates) > MAX_CHOICE,
            )
        return _Resolution(category=kind or "unknown", crop=crop)

    def _candidates(self, category: str | None, crop: str | None) -> list[LexiconEntry]:
        """The products of a kind, for a crop.

        Fertiliser, feed and tools carry no crop tags in the catalogue: the
        crop does not narrow them, and "गेहूँ के लिए खाद" lists the
        fertilisers rather than saying there is none for wheat.
        """
        assert self.lexicon is not None
        if crop is not None and category is not None and not self._kind_tagged(category):
            crop = None
        return [
            entry
            for entry in self.lexicon.entries
            if (category is None or entry.category == category)
            and (crop is None or crop_fits(crop, entry.crops))
        ]

    def _choice(self, found: _Resolution, animals: tuple[str, ...] = ()) -> str:
        names = self.words.join(self.words.product(e) for e in found.choices)
        crop = self.words.crop(found.crop) if found.crop is not None else None
        fed = animals if animals_fed_by(found.category) else ()
        return self.words.choice(
            names,
            crop=crop,
            kind=self.words.kind_said(found.category),
            more=found.more,
            animals=self.words.animals(fed),
        )

    def _not_carried(self, found: _Resolution) -> str:
        kind = self.words.kind_said(found.category)
        if found.crop is not None and kind:
            others = self._candidates(found.category, None)[:MAX_LISTED]
            names = self.words.join(self.words.product(e) for e in others)
            return self.words.not_carried_for_crop(self.words.crop(found.crop), kind, names)
        if found.crop is not None:
            return self.words.nothing_for_crop(self.words.crop(found.crop))
        if kind:
            return self.words.nothing_in_kind(kind)
        return self.words.ask_product(asks_price=False)

    def _listing(
        self,
        category: str | None,
        crop: str | None,
        focus: ConversationFocus | None = None,
        *,
        after: tuple[str, ...] = (),
    ) -> str | None:
        """What there is of a kind, four at a time.

        ``after`` is what was already read out: the farmer asked "और
        क्या-क्या है?", and the answer is the next four, or that there are
        no more -- never the same four again.
        """
        kind = self.words.kind_said(category)
        if not kind:
            if crop is not None:
                return self._crop_listing(crop, focus) or self.words.nothing_for_crop(
                    self.words.crop(crop)
                )
            return self.words.overview()
        items = self._candidates(category, crop)
        if not items and crop is not None:
            items = self._candidates(category, None)
        if not items:
            return self.words.nothing_in_kind(kind)
        heard = set(after)
        continuing = any(e.sku in heard for e in items)
        if continuing:
            items = [e for e in items if e.sku not in heard]
            if not items:
                return self.words.no_more(kind)
        shown = items[:MAX_LISTED]
        if focus is not None:
            # What was listed is what "पहला वाला" refers to next turn.
            focus.offer(tuple(ProductInFocus(e.sku, e.name_hi) for e in shown))
        names = self.words.join(self.words.product(e) for e in shown)
        more = len(items) > MAX_LISTED
        if continuing:
            return self.words.listing_more(kind, names, more=more)
        animals = focus.animals if focus is not None and animals_fed_by(category) else ()
        return self.words.listing(kind, names, more=more, animals=self.words.animals(animals))

    # -- answering a choice ------------------------------------------------- #

    def _from_choices(self, transcript: str, focus: ConversationFocus) -> LexiconEntry | str | None:
        """Which of the offered products the farmer picked, or "all", or None.

        Read before the catalogue-wide scan because the farmer is answering
        the agent's own question: "पहला वाला", "दूसरा", or the name in their
        own inflection ("मसूरी"). A crop or a kind is a new subject, resolved
        against the whole catalogue instead: the list read out was cut at
        four, and "धान" after four other seeds must find the paddy seeds it
        did not name.
        """
        if not focus.choices or self.lexicon is None:
            return None
        entries = self._entries(focus.choices)
        if not entries:
            return None
        if _ALL_OF_THEM.search(transcript):
            return _ALL
        by_position = self._ordinal(transcript, entries)
        if by_position is not None:
            return by_position
        if crop_in(transcript) is not None or category_in(transcript) is not None:
            return None
        if _OVERVIEW.search(transcript):
            # "What all do you have?" is asking for the list, not picking
            # from it; a stray syllable must not land on an offered name.
            return None
        return self._closest(transcript, entries)

    @staticmethod
    def _ordinal(transcript: str, entries: list[LexiconEntry]) -> LexiconEntry | None:
        found = _ORDINAL.search(transcript)
        if found is None:
            return None
        index = _ORDINALS[found.group(0).lower()]
        if -len(entries) <= index < len(entries):
            return entries[index]
        return None

    @staticmethod
    def _closest(transcript: str, entries: list[LexiconEntry]) -> LexiconEntry | None:
        """The offered product whose name the words come closest to, if one
        stands clearly apart from the rest."""
        tokens = [t for t in normalise(squeeze(transcript)).split() if len(t) > _SHORT_TOKEN]
        if not tokens:
            return None
        phrases = [
            " ".join(tokens[start : start + size])
            for size in range(1, _CHOICE_PHRASE_TOKENS + 1)
            for start in range(max(1, len(tokens) - size + 1))
        ]
        stemmed = {phrase: " ".join(stem(t) for t in phrase.split()) for phrase in phrases}

        scored: list[tuple[float, LexiconEntry]] = []
        for entry in entries:
            best = 0.0
            for variant in entry.normalised_variants():
                variant_stem = " ".join(stem(t) for t in variant.split())
                for phrase in phrases:
                    best = max(
                        best,
                        similarity(phrase, variant),
                        similarity(stemmed[phrase], variant_stem),
                    )
            scored.append((best, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        top_score, top = scored[0]
        if top_score < _CHOICE_THRESHOLD:
            return None
        if len(scored) > 1 and top_score - scored[1][0] < _CHOICE_MARGIN:
            return None
        return top

    async def _prices_for(
        self, choices: tuple[ProductInFocus, ...], intent: Intent
    ) -> DirectAnswer | None:
        """ "सबका रेट बता दो": every offered product, one lookup each."""
        entries = self._entries(choices)
        if not entries:
            return None
        words = self.words
        if self.centre is None:
            names = words.join(words.product(e) for e in entries)
            return DirectAnswer(
                text=words.carried_list_no_centre(names), intent=intent, resolved=False
            )

        parts: list[str] = []
        results: list[ToolResult] = []
        for entry in entries[:MAX_CHOICE]:
            result = await self._availability(entry)
            if result is None:
                continue
            results.append(result)
            data = result.data
            name = words.product(entry, data)
            if data.get("available"):
                pack = words.pack(str(data.get("pack") or ""))
                parts.append(words.price_line(name, pack, money(data.get("price"))))
            else:
                parts.append(words.not_available(name))
        if not parts:
            return None
        text = self._with_centre_note(words.close_list(words.join(parts))) + words.which_one()
        return DirectAnswer(text=text, intent=intent, results=results, keeps_choices=True)

    # -- composition ---------------------------------------------------------- #

    async def _composition(self, transcript: str, focus: ConversationFocus) -> DirectAnswer | None:
        if self.lexicon is None:
            return None
        if (
            category_in(transcript) is not None
            or focus.product is None
            or _MORE.search(transcript) is not None
        ):
            # "खाद में क्या-क्या है?" reads as a composition question to the
            # rules, but it names a kind, not a product: the farmer is asking
            # what there is, and the catalogue answers that. So is "और
            # क्या-क्या है?" straight after a product -- what *else*. "इसमें
            # क्या है?" with a product under discussion is left to be what
            # it is.
            overview = await self._overview(transcript, focus, Intent.PRODUCT_AVAILABILITY)
            if overview is not None:
                return overview
        found = self._resolve(transcript, focus)
        if found.product is None:
            return await self._overview(transcript, focus, Intent.PRODUCT_AVAILABILITY)
        entry = found.product
        focus.note_product(entry.sku, entry.name_hi)
        result = await self.registry.execute(
            "get_product_details", {"sku": entry.sku}, self.context
        )
        if not result.ok:
            return None
        data = result.data
        words = self.words
        parts = [
            words.composition_part(str(item.get("ingredient") or "").strip(), item.get("percent"))
            for item in data.get("composition") or []
            if str(item.get("ingredient") or "").strip() and item.get("percent") is not None
        ]
        if not parts:
            parts = [str(a) for a in (data.get("active_ingredients") or [])[:2]]
        if not parts:
            return None
        text = words.composition(words.product(entry, data), words.join(parts))
        if data.get("restricted"):
            return DirectAnswer(
                text=text + words.restricted_suffix(),
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
                return DirectAnswer(
                    text=self._found_centre(centres[0]),
                    intent=Intent.CENTRE_LOCATION,
                    results=[result],
                )
        if self.centre is None:
            return None
        english = self.words.code.startswith("en")
        address = (
            (self.centre.address_en or self.centre.address_spoken)
            if english
            else self.centre.address_spoken
        )
        text = self.words.centre_sentence(
            name=self._centre_name(),
            address=address,
            open_time=self.centre.open_time,
            close_time=self.centre.close_time,
            working_days=self.centre.working_days,
            phone=self.centre.phone,
        )
        return DirectAnswer(text=text, intent=Intent.CENTRE_LOCATION)

    def _found_centre(self, row: Mapping[str, Any]) -> str:
        if self.words.code.startswith("en"):
            registered = str(row.get("name") or row.get("name_hi") or "")
            name = spoken_centre_name(registered, language="en-IN")
            address = row.get("address") or row.get("address_spoken_hi")
        else:
            name = spoken_centre_name(str(row.get("name_hi") or row.get("name") or ""))
            address = row.get("address_spoken_hi") or row.get("address")
        return self.words.centre_sentence(
            name=name,
            address=address,
            open_time=_parse_time(row.get("open")),
            close_time=_parse_time(row.get("close")),
            working_days=(),
            phone=row.get("phone"),
        )

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

    async def _small_talk(self, transcript: str, focus: ConversationFocus) -> DirectAnswer | None:
        stripped = transcript.strip()
        words = len(stripped.split())
        if self.lexicon is not None:
            overview = await self._overview(stripped, focus, Intent.PRODUCT_AVAILABILITY)
            if overview is not None:
                return overview
        if words <= 8 and _MACHINE.search(stripped):
            return DirectAnswer(
                text=self.words.machine(), intent=Intent.OUT_OF_SCOPE, offered_transfer=True
            )
        if words <= 6 and _HEARING.search(stripped):
            return DirectAnswer(text=self.words.hearing_ok(), intent=Intent.OUT_OF_SCOPE)
        return None


def _parse_time(value: Any) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return None


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
    "CentreFacts",
    "DirectAnswer",
    "DirectAnswers",
    "spoken_centre_name",
)
