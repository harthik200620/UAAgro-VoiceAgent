"""Domain enumerations.

Every enum here is stored in Postgres as a text column with a CHECK constraint
rather than a native PG enum: adding a value must be a migration, not an
``ALTER TYPE`` that locks the table during a seasonal peak (§3).
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """§10 access roles, ordered least to most privileged."""

    READ_ONLY = "read_only"
    AUDITOR = "auditor"
    AGRONOMIST = "agronomist"
    CENTRE_MANAGER = "centre_manager"
    OPS_MANAGER = "ops_manager"
    SUPER_ADMIN = "super_admin"


#: Privilege ordering. Used by the API role dependencies; a role satisfies a
#: requirement when its rank is greater than or equal to the required rank.
ROLE_RANK: dict[Role, int] = {
    Role.READ_ONLY: 0,
    Role.AUDITOR: 1,
    Role.AGRONOMIST: 2,
    Role.CENTRE_MANAGER: 3,
    Role.OPS_MANAGER: 4,
    Role.SUPER_ADMIN: 5,
}

#: Roles whose visibility is limited to the centres in ``user_centre_access``.
#: Enforced in Postgres RLS, not only here (§10).
CENTRE_SCOPED_ROLES: frozenset[Role] = frozenset(
    {Role.CENTRE_MANAGER, Role.AGRONOMIST, Role.READ_ONLY}
)


class AccessLevel(StrEnum):
    READ = "read"
    WRITE = "write"


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    """§10 ``calls.status``. Every terminal state is reachable and recorded --
    §1 N8 requires a complete record even for failed and abandoned calls."""

    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    REJECTED_SPAM = "rejected_spam"
    ABANDONED = "abandoned"


TERMINAL_CALL_STATUSES: frozenset[CallStatus] = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
        CallStatus.FAILED,
        CallStatus.REJECTED_SPAM,
        CallStatus.ABANDONED,
    }
)


class CallOutcome(StrEnum):
    RESOLVED = "resolved"
    TRANSFERRED = "transferred"
    TICKET_CREATED = "ticket_created"
    ABANDONED_SILENCE = "abandoned_silence"
    CALLER_HUNG_UP = "caller_hung_up"
    OPTED_OUT = "opted_out"
    OFFER_ACCEPTED = "offer_accepted"
    OFFER_DECLINED = "offer_declined"
    NOT_REACHED = "not_reached"
    SYSTEM_FAILURE = "system_failure"
    REJECTED_SPAM = "rejected_spam"


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class QualityTier(StrEnum):
    """§5.1 speech quality tier, surfaced in the admin panel.

    C is shown as measurably worse rather than quietly shipped as equivalent.
    """

    A = "A"
    B = "B"
    C = "C"


class TurnStrategy(StrEnum):
    """§5.2: who decides the turn is over.

    The first two are *vendor-decided* -- the recogniser fuses recognition and
    endpointing and emits the decision itself. The last two run locally, in
    front of a recogniser that only transcribes.
    """

    FLUX_SEMANTIC = "flux_semantic"
    SONIOX_ENDPOINT = "soniox_endpoint"
    SMART_TURN_V3 = "smart_turn_v3"
    VAD_SILENCE = "vad_silence"


#: Strategies where the recogniser itself ends the turn.
#:
#: Named once rather than compared against a literal at each call site: adding
#: a third vendor-decided engine and missing one of those comparisons produces
#: two detectors racing to end the turn, which cuts the caller off mid-sentence
#: and raises nothing. :func:`voice_worker.adapters.factory.build_speech_stack`
#: refuses the mismatch, and this is the set it refuses against.
VENDOR_DECIDED_TURN_STRATEGIES: frozenset[TurnStrategy] = frozenset(
    {TurnStrategy.FLUX_SEMANTIC, TurnStrategy.SONIOX_ENDPOINT}
)


class ConsentType(StrEnum):
    PROMOTIONAL_VOICE = "promotional_voice"
    PROMOTIONAL_WHATSAPP = "promotional_whatsapp"
    TRANSACTIONAL = "transactional"
    RECORDING = "recording"


class ConsentChannel(StrEnum):
    VOICE = "voice"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    IN_STORE = "in_store"
    WEB = "web"


class ProductCategory(StrEnum):
    SEEDS = "seeds"
    FERTILISERS = "fertilisers"
    CROP_PROTECTION = "crop_protection"
    CATTLE_FEED = "cattle_feed"
    TOOLS_EQUIPMENT = "tools_equipment"


class ProductType(StrEnum):
    HYBRID_SEED = "hybrid_seed"
    CERTIFIED_SEED = "certified_seed"
    NPK = "npk"
    STRAIGHT_FERTILISER = "straight_fertiliser"
    MICRONUTRIENT = "micronutrient"
    BIO_FERTILISER = "bio_fertiliser"
    ORGANIC_MANURE = "organic_manure"
    INSECTICIDE = "insecticide"
    FUNGICIDE = "fungicide"
    HERBICIDE = "herbicide"
    PGR = "pgr"
    CATTLE_FEED = "cattle_feed"
    SPRAYER = "sprayer"
    IRRIGATION = "irrigation"
    IMPLEMENT = "implement"


#: Product types whose recommendation must always carry a pre-harvest interval
#: and a spoken precaution (§16.2, KB §5).
CROP_PROTECTION_TYPES: frozenset[ProductType] = frozenset(
    {ProductType.INSECTICIDE, ProductType.FUNGICIDE, ProductType.HERBICIDE, ProductType.PGR}
)


class Formulation(StrEnum):
    SC = "SC"
    EC = "EC"
    WG = "WG"
    WP = "WP"
    SL = "SL"
    GRANULE = "granule"
    POWDER = "powder"
    LIQUID = "liquid"


class LandUnit(StrEnum):
    """Farmers speak in local units; never answer in metric only (§3)."""

    BIGHA = "bigha"
    ACRE = "acre"
    KATHA = "katha"
    HECTARE = "hectare"


class DoseBasis(StrEnum):
    PER_ACRE = "per_acre"
    PER_BIGHA = "per_bigha"
    PER_KATHA = "per_katha"
    PER_HECTARE = "per_hectare"
    PER_LITRE_WATER = "per_litre_water"
    PER_PLANT = "per_plant"


class ApprovalState(StrEnum):
    """Crop advisory approval (§9, KB §5). Nothing but APPROVED is servable."""

    DRAFT = "draft"
    PENDING_AGRONOMIST = "pending_agronomist"
    APPROVED = "approved"
    REJECTED = "rejected"


class Season(StrEnum):
    KHARIF = "kharif"
    RABI = "rabi"
    ZAID = "zaid"
    PERENNIAL = "perennial"


class TransferReason(StrEnum):
    """§12.3: the tool argument is a fixed enum. A free-text reason is rejected."""

    SAFETY_EMERGENCY = "safety_emergency"
    EXPLICIT_REQUEST = "explicit_request"
    ABUSE_OR_ANGER = "abuse_or_anger"
    LEGAL_OR_DISPUTE = "legal_or_dispute"
    REPEATED_MISUNDERSTANDING = "repeated_misunderstanding"
    LOW_RECOGNITION_CONFIDENCE = "low_recognition_confidence"
    NEGATIVE_SENTIMENT = "negative_sentiment"
    COMPLEX_OR_HIGH_VALUE_ORDER = "complex_or_high_value_order"
    DEALERSHIP_ENQUIRY = "dealership_enquiry"
    PRODUCT_COMPLAINT = "product_complaint"
    RESTRICTED_PRODUCT = "restricted_product"
    MISSING_DATA = "missing_data"
    SCHEME_ELIGIBILITY = "scheme_eligibility"
    CALLER_DISPUTES_ANSWER = "caller_disputes_answer"


#: §12.1 -- these bypass every further agent turn and every availability check.
IMMEDIATE_TRANSFER_REASONS: frozenset[TransferReason] = frozenset(
    {
        TransferReason.SAFETY_EMERGENCY,
        TransferReason.EXPLICIT_REQUEST,
        TransferReason.ABUSE_OR_ANGER,
        TransferReason.LEGAL_OR_DISPUTE,
    }
)


class TransferUrgency(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class Intent(StrEnum):
    """§11.2 supported intents."""

    PRODUCT_AVAILABILITY = "product_availability"
    PRICE_ENQUIRY = "price_enquiry"
    CROP_RECOMMENDATION = "crop_recommendation"
    PROBLEM_DIAGNOSIS = "problem_diagnosis"
    DOSAGE_QUERY = "dosage_query"
    PRODUCT_COMPOSITION = "product_composition"
    CENTRE_LOCATION = "centre_location"
    ORDER_STATUS = "order_status"
    SERVICE_REQUEST = "service_request"
    SCHEME_QUERY = "scheme_query"
    COMPLAINT = "complaint"
    TALK_TO_HUMAN = "talk_to_human"
    DEALERSHIP_ENQUIRY = "dealership_enquiry"
    SAFETY_EMERGENCY = "safety_emergency"
    OUT_OF_SCOPE = "out_of_scope"
    SPAM = "spam"
    UNKNOWN = "unknown"


class TicketType(StrEnum):
    COMPLAINT = "complaint"
    CALLBACK = "callback"
    LEAD = "lead"
    SERVICE_BOOKING = "service_booking"
    DEALERSHIP = "dealership"
    SAFETY_INCIDENT = "safety_incident"
    DATA_REQUEST = "data_request"


class TicketPriority(StrEnum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class CampaignStatus(StrEnum):
    """§13.1 lifecycle. RUNNING is reachable only through the compliance gate
    and a second approver (four-eyes)."""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ContactStatus(StrEnum):
    PENDING = "pending"
    SCRUBBED_OUT = "scrubbed_out"
    DIALING = "dialing"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    OPTED_OUT = "opted_out"
    MAX_ATTEMPTS = "max_attempts"
    EXCLUDED_CONSENT = "excluded_consent"
    EXCLUDED_WINDOW = "excluded_window"


class ExclusionReason(StrEnum):
    """Why the compliance gate removed a contact. Surfaced per-check in the UI
    so the operator sees exactly how many each rule took out (§13.1)."""

    NO_CONSENT = "no_consent"
    CONSENT_EXPIRED = "consent_expired"
    DND_REGISTERED = "dnd_registered"
    INTERNAL_DNC = "internal_dnc"
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    FREQUENCY_CAP = "frequency_cap"
    DUPLICATE_ACTIVE_CAMPAIGN = "duplicate_active_campaign"
    INVALID_NUMBER = "invalid_number"
    BLOCKLISTED = "blocklisted"


class InterestLevel(StrEnum):
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    CALLBACK_REQUESTED = "callback_requested"
    WRONG_PERSON = "wrong_person"
    NO_RESPONSE = "no_response"


class MessageDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class WhatsAppTemplate(StrEnum):
    """§14. Category drives cost: marketing is ~7.5x utility in India, so a
    dosage card is utility and miscategorising it is a real budget error."""

    OFFER_DETAILS = "offer_details"
    PRODUCT_PRICE_LIST = "product_price_list"
    DOSAGE_INSTRUCTIONS = "dosage_instructions"
    CENTRE_LOCATION = "centre_location"
    CALLBACK_CONFIRMATION = "callback_confirmation"
    ORDER_STATUS_UPDATE = "order_status_update"
    TICKET_ACK = "ticket_ack"


class MessageCategory(StrEnum):
    MARKETING = "marketing"
    UTILITY = "utility"
    SERVICE = "service"
    AUTHENTICATION = "authentication"


TEMPLATE_CATEGORY: dict[WhatsAppTemplate, MessageCategory] = {
    WhatsAppTemplate.OFFER_DETAILS: MessageCategory.MARKETING,
    WhatsAppTemplate.PRODUCT_PRICE_LIST: MessageCategory.UTILITY,
    WhatsAppTemplate.DOSAGE_INSTRUCTIONS: MessageCategory.UTILITY,
    WhatsAppTemplate.CENTRE_LOCATION: MessageCategory.UTILITY,
    WhatsAppTemplate.CALLBACK_CONFIRMATION: MessageCategory.UTILITY,
    WhatsAppTemplate.ORDER_STATUS_UPDATE: MessageCategory.UTILITY,
    WhatsAppTemplate.TICKET_ACK: MessageCategory.UTILITY,
}


class FlowType(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class TelephonyProvider(StrEnum):
    EXOTEL = "exotel"
    PLIVO = "plivo"
    TWILIO = "twilio"
    GENERIC_SIP = "generic_sip"
    SIMULATOR = "simulator"


class AudioCodec(StrEnum):
    """§4.3 / §23-8: the codec differs per provider and conflating them produces
    white noise on the line. Exotel is linear16; Twilio and Plivo are mulaw."""

    LINEAR16 = "linear16"
    MULAW = "mulaw"


PROVIDER_CODEC: dict[TelephonyProvider, AudioCodec] = {
    TelephonyProvider.EXOTEL: AudioCodec.LINEAR16,
    TelephonyProvider.PLIVO: AudioCodec.MULAW,
    TelephonyProvider.TWILIO: AudioCodec.MULAW,
    TelephonyProvider.GENERIC_SIP: AudioCodec.MULAW,
    TelephonyProvider.SIMULATOR: AudioCodec.LINEAR16,
}


class SpamRuleType(StrEnum):
    BLOCKLIST = "blocklist"
    CALL_VELOCITY_HOUR = "call_velocity_hour"
    CALL_VELOCITY_DAY = "call_velocity_day"
    REPEAT_ABANDON = "repeat_abandon"
    NUMBER_RANGE = "number_range"


class SpamAction(StrEnum):
    REJECT = "reject"
    FLAG = "flag"
    RATE_LIMIT = "rate_limit"


class AuditAction(StrEnum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    MFA_ENROLLED = "mfa_enrolled"
    TOKEN_REFRESH = "token_refresh"  # noqa: S105 -- audit action name, not a secret
    TOKEN_REUSE_DETECTED = "token_reuse_detected"  # noqa: S105 -- audit action name
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    PUBLISH = "publish"
    ROLLBACK = "rollback"
    APPROVE = "approve"
    EXPORT = "export"
    RECORDING_ACCESS = "recording_access"
    PHONE_DECRYPT = "phone_decrypt"
    CALL_LISTEN_IN = "call_listen_in"
    CALL_BARGE_IN = "call_barge_in"
    CAMPAIGN_APPROVAL = "campaign_approval"
    DATA_ERASURE = "data_erasure"


class OrderStatus(StrEnum):
    """Where a farmer's order actually is.

    §10 defines no order tables, yet §6.3 requires ``get_order_status`` and
    §6.2 puts the last three orders in every turn's context. The states here
    are the ones a farmer asks about by name -- "भेजा क्या?", "कब आएगा?" -- rather
    than an internal workflow, because the answer is read out loud.
    """

    PLACED = "placed"
    CONFIRMED = "confirmed"
    PACKED = "packed"
    DISPATCHED = "dispatched"
    READY_FOR_PICKUP = "ready_for_pickup"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class FulfilmentMode(StrEnum):
    PICKUP = "pickup"
    DELIVERY = "delivery"
