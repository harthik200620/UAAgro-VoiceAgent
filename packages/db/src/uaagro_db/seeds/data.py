"""Seed content for local development and tests.

**All of this is fictional demo content.** UA Agro's real SKUs, prices, stock,
centre addresses, manager contacts and dosages are not public (KB §11) and must
be imported from the company's own systems before go-live.

Two consequences that are enforced rather than merely documented:

* Every ``crop_recommendations`` row seeds as ``draft``. KB §5 and §9 forbid
  serving a dose no agronomist has signed, and a test asserts these rows are
  unreachable by the agent.
* CIB&RC registration numbers use an obviously-synthetic ``CIR-DEMO-*`` format,
  so a demo row can never be mistaken for a real registration.

Quantities follow §21 Phase 1: 10 centres, 60 products, 12 crops, 40 crop
recommendations, 200 farmers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, NamedTuple

from uaagro_domain.enums import (
    DoseBasis,
    Formulation,
    ProductCategory,
    ProductType,
    Role,
    Season,
)

ORG_NAME = "UA Agro Solutions Private Limited"
BRAND_NAME = "Naveen Khushhali Kisan Sewa Kendra"


# --------------------------------------------------------------------------- #
# Districts -- 15 in central and eastern UP (§3)
# --------------------------------------------------------------------------- #


class DistrictSeed(NamedTuple):
    name: str
    name_hi: str
    code: str
    #: Acres per local bigha. KB §7: this varies by district and even tehsil.
    #: ``verified`` is False everywhere here because none of these have been
    #: confirmed with UA Agro's field teams -- the agent asks the farmer.
    bigha_acres: Decimal
    verified: bool = False


DISTRICTS: tuple[DistrictSeed, ...] = (
    DistrictSeed("Lucknow", "लखनऊ", "UP-LKO", Decimal("0.625")),
    DistrictSeed("Barabanki", "बाराबंकी", "UP-BBK", Decimal("0.625")),
    DistrictSeed("Sitapur", "सीतापुर", "UP-STP", Decimal("0.625")),
    DistrictSeed("Hardoi", "हरदोई", "UP-HRD", Decimal("0.625")),
    DistrictSeed("Unnao", "उन्नाव", "UP-UNO", Decimal("0.625")),
    DistrictSeed("Raebareli", "रायबरेली", "UP-RBL", Decimal("0.625")),
    DistrictSeed("Kanpur Nagar", "कानपुर नगर", "UP-KNP", Decimal("0.625")),
    DistrictSeed("Ayodhya", "अयोध्या", "UP-AYD", Decimal("0.625")),
    DistrictSeed("Farrukhabad", "फ़र्रुख़ाबाद", "UP-FRK", Decimal("0.625")),
    DistrictSeed("Kannauj", "कन्नौज", "UP-KNJ", Decimal("0.625")),
    DistrictSeed("Gonda", "गोंडा", "UP-GND", Decimal("0.208")),
    DistrictSeed("Basti", "बस्ती", "UP-BST", Decimal("0.208")),
    DistrictSeed("Sultanpur", "सुल्तानपुर", "UP-SLP", Decimal("0.625")),
    DistrictSeed("Pratapgarh", "प्रतापगढ़", "UP-PTP", Decimal("0.625")),
    DistrictSeed("Amethi", "अमेठी", "UP-AMT", Decimal("0.625")),
)


# --------------------------------------------------------------------------- #
# Centres -- 10
# --------------------------------------------------------------------------- #


class CentreSeed(NamedTuple):
    code: str
    district: str
    block: str
    pincode: str
    latitude: Decimal
    longitude: Decimal
    services: tuple[str, ...]


ALL_SERVICES = ("soil_testing", "drone_spray", "field_advisory", "in_store_advisory")
BASIC_SERVICES = ("in_store_advisory", "field_advisory")

CENTRES: tuple[CentreSeed, ...] = (
    CentreSeed(
        "NKSK-LKO-01",
        "Lucknow",
        "Mohanlalganj",
        "226301",
        Decimal("26.6800"),
        Decimal("80.9800"),
        ALL_SERVICES,
    ),
    CentreSeed(
        "NKSK-BBK-01",
        "Barabanki",
        "Fatehpur",
        "225301",
        Decimal("26.9250"),
        Decimal("81.1900"),
        ALL_SERVICES,
    ),
    CentreSeed(
        "NKSK-BBK-02",
        "Barabanki",
        "Ramnagar",
        "225207",
        Decimal("27.0100"),
        Decimal("81.3400"),
        BASIC_SERVICES,
    ),
    CentreSeed(
        "NKSK-STP-01",
        "Sitapur",
        "Biswan",
        "261201",
        Decimal("27.4900"),
        Decimal("80.9900"),
        ALL_SERVICES,
    ),
    CentreSeed(
        "NKSK-HRD-01",
        "Hardoi",
        "Sandila",
        "241204",
        Decimal("27.0700"),
        Decimal("80.5100"),
        BASIC_SERVICES,
    ),
    CentreSeed(
        "NKSK-UNO-01",
        "Unnao",
        "Purwa",
        "209801",
        Decimal("26.4600"),
        Decimal("80.7800"),
        BASIC_SERVICES,
    ),
    CentreSeed(
        "NKSK-RBL-01",
        "Raebareli",
        "Lalganj",
        "229206",
        Decimal("26.0500"),
        Decimal("81.2300"),
        ALL_SERVICES,
    ),
    CentreSeed(
        "NKSK-KNP-01",
        "Kanpur Nagar",
        "Bilhaur",
        "209201",
        Decimal("26.8400"),
        Decimal("80.0600"),
        ALL_SERVICES,
    ),
    CentreSeed(
        "NKSK-AYD-01",
        "Ayodhya",
        "Milkipur",
        "224201",
        Decimal("26.6000"),
        Decimal("82.1000"),
        BASIC_SERVICES,
    ),
    CentreSeed(
        "NKSK-GND-01",
        "Gonda",
        "Manakapur",
        "271302",
        Decimal("27.0500"),
        Decimal("82.2200"),
        BASIC_SERVICES,
    ),
)


# --------------------------------------------------------------------------- #
# Brands
# --------------------------------------------------------------------------- #


class BrandSeed(NamedTuple):
    name: str
    manufacturer: str
    is_partner: bool


BRANDS: tuple[BrandSeed, ...] = (
    BrandSeed("Bayer", "Bayer CropScience Ltd", True),
    BrandSeed("Crystal Crop", "Crystal Crop Protection Ltd", True),
    BrandSeed("IFFCO", "Indian Farmers Fertiliser Cooperative", True),
    BrandSeed("Kribhco", "Krishak Bharati Cooperative", True),
    BrandSeed("Coromandel", "Coromandel International Ltd", True),
    BrandSeed("UPL", "UPL Ltd", True),
    BrandSeed("Dhanuka", "Dhanuka Agritech Ltd", True),
    BrandSeed("Rallis", "Rallis India Ltd", True),
    BrandSeed("Khushhali", "UA Agro Solutions Pvt Ltd", False),
)


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #

CATEGORIES: tuple[tuple[ProductCategory, str, str], ...] = (
    (ProductCategory.SEEDS, "बीज", "seeds"),
    (ProductCategory.FERTILISERS, "खाद", "fertilisers"),
    (ProductCategory.CROP_PROTECTION, "दवाई", "crop-protection"),
    (ProductCategory.CATTLE_FEED, "पशु आहार", "cattle-feed"),
    (ProductCategory.TOOLS_EQUIPMENT, "औज़ार", "tools-equipment"),
)


# --------------------------------------------------------------------------- #
# Products -- 60
# --------------------------------------------------------------------------- #


class ProductSeed(NamedTuple):
    sku: str
    name_en: str
    name_hi: str
    brand: str
    category: ProductCategory
    product_type: ProductType
    pack_value: Decimal
    pack_unit: str
    mrp: Decimal
    #: Every way a farmer might *say* it (KB §3.1). The highest-leverage field
    #: in the catalogue: it drives the post-ASR correction pass.
    lexicon: tuple[str, ...]
    active_ingredients: tuple[str, ...] = ()
    formulation: Formulation | None = None
    crop_targets: tuple[str, ...] = ()
    pest_targets: tuple[str, ...] = ()
    composition: tuple[tuple[str, float], ...] = ()
    is_restricted: bool = False
    requires_licence: bool = False


def _cib(index: int) -> str:
    """Obviously-synthetic registration number for demo agrochemicals."""
    return f"CIR-DEMO-{index:04d}"


_FERTILISERS: tuple[ProductSeed, ...] = (
    ProductSeed(
        "FRT-DAP-50",
        "DAP 18-46-0",
        "डीएपी",
        "IFFCO",
        ProductCategory.FERTILISERS,
        ProductType.NPK,
        Decimal("50"),
        "kg",
        Decimal("1350"),
        ("डीएपी", "डी ए पी", "dap", "dee ay pee", "डीएपी खाद", "काली खाद"),
        composition=(("Nitrogen", 18.0), ("Phosphorus", 46.0)),
    ),
    ProductSeed(
        "FRT-URE-45",
        "Urea 46-0-0",
        "यूरिया",
        "IFFCO",
        ProductCategory.FERTILISERS,
        ProductType.STRAIGHT_FERTILISER,
        Decimal("45"),
        "kg",
        Decimal("267"),
        ("यूरिया", "urea", "यूरीया", "uria", "सफ़ेद खाद"),
        composition=(("Nitrogen", 46.0),),
    ),
    ProductSeed(
        "FRT-MOP-50",
        "Muriate of Potash 0-0-60",
        "पोटाश",
        "IFFCO",
        ProductCategory.FERTILISERS,
        ProductType.STRAIGHT_FERTILISER,
        Decimal("50"),
        "kg",
        Decimal("1700"),
        ("पोटाश", "potash", "एमओपी", "mop", "म्यूरेट"),
        composition=(("Potassium", 60.0),),
    ),
    ProductSeed(
        "FRT-SSP-50",
        "Single Super Phosphate",
        "सिंगल सुपर फॉस्फेट",
        "Coromandel",
        ProductCategory.FERTILISERS,
        ProductType.STRAIGHT_FERTILISER,
        Decimal("50"),
        "kg",
        Decimal("480"),
        ("एसएसपी", "ssp", "सुपर", "सिंगल सुपर", "फॉस्फेट"),
        composition=(("Phosphorus", 16.0), ("Sulphur", 11.0)),
    ),
    ProductSeed(
        "FRT-NPK-123216",
        "NPK 12-32-16",
        "एनपीके बारह बत्तीस सोलह",
        "Kribhco",
        ProductCategory.FERTILISERS,
        ProductType.NPK,
        Decimal("50"),
        "kg",
        Decimal("1470"),
        ("एनपीके", "npk", "एन पी के", "बारह बत्तीस सोलह"),
        composition=(("Nitrogen", 12.0), ("Phosphorus", 32.0), ("Potassium", 16.0)),
    ),
    ProductSeed(
        "FRT-ZNS-5",
        "Zinc Sulphate 21%",
        "जिंक सल्फेट",
        "Khushhali",
        ProductCategory.FERTILISERS,
        ProductType.MICRONUTRIENT,
        Decimal("5"),
        "kg",
        Decimal("340"),
        ("जिंक", "zinc", "जिंक सल्फेट", "जस्ता"),
        composition=(("Zinc", 21.0),),
    ),
    ProductSeed(
        "FRT-SUL-25",
        "Bentonite Sulphur 90%",
        "गंधक",
        "Khushhali",
        ProductCategory.FERTILISERS,
        ProductType.MICRONUTRIENT,
        Decimal("25"),
        "kg",
        Decimal("1100"),
        # Soil-applied granular sulphur. Deliberately does not claim the bare
        # word "सल्फर": the fungicide below answers to it equally, and a farmer
        # saying it alone genuinely means either. The lexicon reports that as
        # ambiguous so the agent asks rather than dispensing the wrong one.
        ("गंधक", "सल्फर दाना", "गंधक दाना", "sulphur granule"),
        composition=(("Sulphur", 90.0),),
    ),
    ProductSeed(
        "FRT-BOR-1",
        "Boron 20%",
        "बोरॉन",
        "Khushhali",
        ProductCategory.FERTILISERS,
        ProductType.MICRONUTRIENT,
        Decimal("1"),
        "kg",
        Decimal("420"),
        ("बोरॉन", "boron", "बोरान"),
        composition=(("Boron", 20.0),),
    ),
    ProductSeed(
        "FRT-VRM-40",
        "Vermicompost",
        "वर्मी कम्पोस्ट",
        "Khushhali",
        ProductCategory.FERTILISERS,
        ProductType.ORGANIC_MANURE,
        Decimal("40"),
        "kg",
        Decimal("380"),
        ("वर्मी कम्पोस्ट", "vermicompost", "केंचुआ खाद", "जैविक खाद"),
    ),
    ProductSeed(
        "FRT-BIO-1",
        "Azotobacter Bio-fertiliser",
        "एज़ोटोबैक्टर",
        "Khushhali",
        ProductCategory.FERTILISERS,
        ProductType.BIO_FERTILISER,
        Decimal("1"),
        "kg",
        Decimal("180"),
        ("बायो खाद", "जैविक", "एज़ोटोबैक्टर", "biofertiliser"),
    ),
    ProductSeed(
        "FRT-NPK-191919",
        "NPK 19-19-19 Water Soluble",
        "एनपीके उन्नीस",
        "Coromandel",
        ProductCategory.FERTILISERS,
        ProductType.NPK,
        Decimal("1"),
        "kg",
        Decimal("240"),
        ("उन्नीस उन्नीस", "19 19 19", "पानी में घुलने वाली खाद"),
        composition=(("Nitrogen", 19.0), ("Phosphorus", 19.0), ("Potassium", 19.0)),
    ),
    ProductSeed(
        "FRT-CAN-50",
        "Calcium Ammonium Nitrate",
        "सीएएन",
        "Coromandel",
        ProductCategory.FERTILISERS,
        ProductType.STRAIGHT_FERTILISER,
        Decimal("50"),
        "kg",
        Decimal("790"),
        ("सीएएन", "can", "कैल्शियम नाइट्रेट"),
        composition=(("Nitrogen", 25.0), ("Calcium", 8.0)),
    ),
    ProductSeed(
        "FRT-GYP-50",
        "Gypsum",
        "जिप्सम",
        "Khushhali",
        ProductCategory.FERTILISERS,
        ProductType.MICRONUTRIENT,
        Decimal("50"),
        "kg",
        Decimal("280"),
        ("जिप्सम", "gypsum", "चूना"),
        composition=(("Calcium", 23.0), ("Sulphur", 18.0)),
    ),
)

_CROP_PROTECTION: tuple[ProductSeed, ...] = (
    ProductSeed(
        "CP-IMD-250",
        "Imidacloprid 17.8% SL",
        "इमिडाक्लोप्रिड",
        "Bayer",
        ProductCategory.CROP_PROTECTION,
        ProductType.INSECTICIDE,
        Decimal("250"),
        "ml",
        Decimal("310"),
        ("इमिडा", "imidacloprid", "माहू की दवा", "चेपा की दवा"),
        ("Imidacloprid",),
        Formulation.SL,
        ("wheat", "paddy", "mustard"),
        ("aphid", "jassid", "whitefly"),
    ),
    ProductSeed(
        "CP-THI-100",
        "Thiamethoxam 25% WG",
        "थायोमेथोक्सम",
        "Crystal Crop",
        ProductCategory.CROP_PROTECTION,
        ProductType.INSECTICIDE,
        Decimal("100"),
        "g",
        Decimal("420"),
        ("थायो", "thiamethoxam", "रस चूसने वाले कीड़े की दवा"),
        ("Thiamethoxam",),
        Formulation.WG,
        ("paddy", "cotton"),
        ("aphid", "hopper"),
    ),
    ProductSeed(
        "CP-CHL-250",
        "Chlorantraniliprole 18.5% SC",
        "क्लोरएंट्रानिलिप्रोल",
        "Bayer",
        ProductCategory.CROP_PROTECTION,
        ProductType.INSECTICIDE,
        Decimal("250"),
        "ml",
        Decimal("1750"),
        ("सुंडी की दवा", "इल्ली की दवा", "coragen", "क्लोर"),
        ("Chlorantraniliprole",),
        Formulation.SC,
        ("paddy", "maize"),
        ("stem_borer", "fall_armyworm"),
    ),
    ProductSeed(
        "CP-CAR-500",
        "Cartap Hydrochloride 50% SP",
        "कार्टाप",
        "Dhanuka",
        ProductCategory.CROP_PROTECTION,
        ProductType.INSECTICIDE,
        Decimal("500"),
        "g",
        Decimal("560"),
        ("कार्टाप", "cartap", "तना छेदक की दवा"),
        ("Cartap Hydrochloride",),
        Formulation.WP,
        ("paddy",),
        ("stem_borer",),
    ),
    ProductSeed(
        "CP-LAM-250",
        "Lambda Cyhalothrin 5% EC",
        "लैम्ब्डा",
        "UPL",
        ProductCategory.CROP_PROTECTION,
        ProductType.INSECTICIDE,
        Decimal("250"),
        "ml",
        Decimal("340"),
        ("लैम्ब्डा", "lambda", "कीड़े की दवा"),
        ("Lambda Cyhalothrin",),
        Formulation.EC,
        ("gram", "pigeon_pea"),
        ("pod_borer",),
    ),
    ProductSeed(
        "CP-EMA-100",
        "Emamectin Benzoate 5% SG",
        "इमामेक्टिन",
        "Rallis",
        ProductCategory.CROP_PROTECTION,
        ProductType.INSECTICIDE,
        Decimal("100"),
        "g",
        Decimal("480"),
        ("इमामेक्टिन", "emamectin", "सुंडी"),
        ("Emamectin Benzoate",),
        Formulation.WG,
        ("vegetables", "maize"),
        ("caterpillar", "fall_armyworm"),
    ),
    ProductSeed(
        "CP-PRO-250",
        "Propiconazole 25% EC",
        "प्रोपिकोनाज़ोल",
        "Crystal Crop",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("250"),
        "ml",
        Decimal("470"),
        ("प्रोपिकोनाज़ोल", "गेरुआ की दवा", "रतुआ", "tilt"),
        ("Propiconazole",),
        Formulation.EC,
        ("wheat",),
        ("rust", "leaf_blight"),
    ),
    ProductSeed(
        "CP-TEB-250",
        "Tebuconazole 25.9% EC",
        "टेबुकोनाज़ोल",
        "Bayer",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("250"),
        "ml",
        Decimal("520"),
        ("टेबु", "tebuconazole", "फफूंद की दवा"),
        ("Tebuconazole",),
        Formulation.EC,
        ("wheat", "paddy"),
        ("rust", "blast"),
    ),
    ProductSeed(
        "CP-MAN-1000",
        "Mancozeb 75% WP",
        "मैंकोज़ेब",
        "UPL",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("1000"),
        "g",
        Decimal("420"),
        ("मैंकोज़ेब", "mancozeb", "डाइथेन", "झुलसा की दवा"),
        ("Mancozeb",),
        Formulation.WP,
        ("potato", "tomato"),
        ("late_blight",),
    ),
    ProductSeed(
        "CP-CYM-500",
        "Cymoxanil 8% + Mancozeb 64% WP",
        "साइमोक्सानिल",
        "Crystal Crop",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("500"),
        "g",
        Decimal("640"),
        ("साइमोक्सानिल", "curzate", "पछेती झुलसा"),
        ("Cymoxanil", "Mancozeb"),
        Formulation.WP,
        ("potato",),
        ("late_blight",),
    ),
    ProductSeed(
        "CP-CAB-500",
        "Carbendazim 50% WP",
        "कार्बेन्डाज़िम",
        "Dhanuka",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("500"),
        "g",
        Decimal("380"),
        ("कार्बेन्डाज़िम", "बाविस्टिन", "bavistin", "बीज उपचार"),
        ("Carbendazim",),
        Formulation.WP,
        ("wheat", "gram"),
        ("wilt", "seed_rot"),
    ),
    ProductSeed(
        "CP-TRI-250",
        "Trifloxystrobin + Tebuconazole 75% WG",
        "नैटिवो",
        "Bayer",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("250"),
        "g",
        Decimal("1450"),
        ("नैटिवो", "nativo", "फफूंदनाशक"),
        ("Trifloxystrobin", "Tebuconazole"),
        Formulation.WG,
        ("paddy",),
        ("blast",),
    ),
    ProductSeed(
        "CP-SUL-1000",
        "Sulphur 80% WDG",
        "सल्फर दवा",
        "Khushhali",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("1000"),
        "g",
        Decimal("290"),
        # Sprayable sulphur fungicide. Same reasoning as the granular form: the
        # bare word belongs to neither product.
        ("गंधक की दवा", "सल्फर दवा", "sulphur fungicide", "गंधक स्प्रे"),
        ("Sulphur",),
        Formulation.WG,
        ("mustard",),
        ("white_rust", "powdery_mildew"),
    ),
    ProductSeed(
        "CP-SUL-250",
        "Sulfosulfuron 75% WG",
        "सल्फोसल्फ्यूरॉन",
        "Crystal Crop",
        ProductCategory.CROP_PROTECTION,
        ProductType.HERBICIDE,
        Decimal("32"),
        "g",
        Decimal("410"),
        ("सल्फोसल्फ्यूरॉन", "गेहूँ की घास की दवा", "leader", "खरपतवारनाशक"),
        ("Sulfosulfuron",),
        Formulation.WG,
        ("wheat",),
        ("phalaris", "weeds"),
    ),
    ProductSeed(
        "CP-PEN-1000",
        "Pendimethalin 30% EC",
        "पेंडीमिथालिन",
        "UPL",
        ProductCategory.CROP_PROTECTION,
        ProductType.HERBICIDE,
        Decimal("1000"),
        "ml",
        Decimal("520"),
        ("पेंडी", "pendimethalin", "stomp", "घास की दवा"),
        ("Pendimethalin",),
        Formulation.EC,
        ("wheat", "potato"),
        ("weeds",),
    ),
    ProductSeed(
        "CP-BIS-100",
        "Bispyribac Sodium 10% SC",
        "बिस्पायरिबैक",
        "Dhanuka",
        ProductCategory.CROP_PROTECTION,
        ProductType.HERBICIDE,
        Decimal("100"),
        "ml",
        Decimal("450"),
        ("बिस्पायरिबैक", "nominee gold", "धान की घास की दवा"),
        ("Bispyribac Sodium",),
        Formulation.SC,
        ("paddy",),
        ("weeds",),
    ),
    ProductSeed(
        "CP-GLY-1000",
        "Glyphosate 41% SL",
        "ग्लाइफोसेट",
        "UPL",
        ProductCategory.CROP_PROTECTION,
        ProductType.HERBICIDE,
        Decimal("1000"),
        "ml",
        Decimal("430"),
        ("ग्लाइफोसेट", "glyphosate", "राउंडअप", "घास मारने की दवा"),
        ("Glyphosate",),
        Formulation.SL,
        ("non_crop",),
        ("weeds",),
        is_restricted=True,
        requires_licence=True,
    ),
    ProductSeed(
        "CP-2-4D-1000",
        "2,4-D Amine Salt 58% SL",
        "टू फोर डी",
        "Crystal Crop",
        ProductCategory.CROP_PROTECTION,
        ProductType.HERBICIDE,
        Decimal("1000"),
        "ml",
        Decimal("360"),
        ("टू फोर डी", "2 4 d", "चौड़ी पत्ती की दवा"),
        ("2,4-D Amine Salt",),
        Formulation.SL,
        ("wheat",),
        ("broadleaf_weeds",),
    ),
    ProductSeed(
        "CP-THM-100",
        "Thiophanate Methyl 70% WP",
        "थायोफेनेट",
        "Rallis",
        ProductCategory.CROP_PROTECTION,
        ProductType.FUNGICIDE,
        Decimal("100"),
        "g",
        Decimal("260"),
        ("थायोफेनेट", "roko", "बीजोपचार"),
        ("Thiophanate Methyl",),
        Formulation.WP,
        ("gram", "pea"),
        ("wilt",),
    ),
    ProductSeed(
        "CP-GA3-8",
        "Gibberellic Acid 0.001% L",
        "जिबरेलिक एसिड",
        "Khushhali",
        ProductCategory.CROP_PROTECTION,
        ProductType.PGR,
        Decimal("8"),
        "ml",
        Decimal("120"),
        ("जिबरेलिक", "ga3", "बढ़वार की दवा"),
        ("Gibberellic Acid",),
        Formulation.LIQUID,
        ("vegetables",),
        (),
    ),
)

_SEEDS: tuple[ProductSeed, ...] = (
    ProductSeed(
        "SED-WHT-HD3086",
        "Wheat HD-3086 Certified",
        "गेहूँ एचडी 3086",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("40"),
        "kg",
        Decimal("1650"),
        ("गेहूँ बीज", "एचडी 3086", "hd 3086", "wheat seed"),
        crop_targets=("wheat",),
    ),
    ProductSeed(
        "SED-WHT-DBW187",
        "Wheat DBW-187 Certified",
        "गेहूँ डीबीडब्ल्यू 187",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("40"),
        "kg",
        Decimal("1720"),
        ("डीबीडब्ल्यू", "dbw 187", "करण वंदना"),
        crop_targets=("wheat",),
    ),
    ProductSeed(
        "SED-PDY-PUSA1509",
        "Paddy Pusa Basmati 1509",
        "धान पूसा 1509",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("25"),
        "kg",
        Decimal("1450"),
        ("पूसा 1509", "बासमती बीज", "धान का बीज"),
        crop_targets=("paddy",),
    ),
    ProductSeed(
        "SED-PDY-SARJU52",
        "Paddy Sarju-52",
        "धान सरजू 52",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("25"),
        "kg",
        Decimal("1100"),
        ("सरजू", "sarju 52", "धान बीज"),
        crop_targets=("paddy",),
    ),
    ProductSeed(
        "SED-POT-KUFRI",
        "Potato Kufri Bahar Seed",
        "आलू कुफ़री बहार",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("50"),
        "kg",
        Decimal("1250"),
        ("आलू बीज", "कुफ़री", "kufri", "potato seed"),
        crop_targets=("potato",),
    ),
    ProductSeed(
        "SED-MUS-PUSA",
        "Mustard Pusa Bold",
        "सरसों पूसा बोल्ड",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("5"),
        "kg",
        Decimal("620"),
        ("सरसों बीज", "पूसा बोल्ड", "mustard seed"),
        crop_targets=("mustard",),
    ),
    ProductSeed(
        "SED-MAZ-HYB",
        "Maize Hybrid Pioneer 3396",
        "मक्का हाइब्रिड",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.HYBRID_SEED,
        Decimal("4"),
        "kg",
        Decimal("1450"),
        ("मक्का बीज", "makka", "corn seed"),
        crop_targets=("maize",),
    ),
    ProductSeed(
        "SED-GRM-JG14",
        "Gram JG-14",
        "चना जेजी 14",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("30"),
        "kg",
        Decimal("2400"),
        ("चना बीज", "jg 14", "gram seed"),
        crop_targets=("gram",),
    ),
    ProductSeed(
        "SED-PEA-AP3",
        "Pea Arkel",
        "मटर अर्कल",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("10"),
        "kg",
        Decimal("1150"),
        ("मटर बीज", "अर्कल", "pea seed"),
        crop_targets=("pea",),
    ),
    ProductSeed(
        "SED-LEN-K75",
        "Lentil K-75",
        "मसूर के 75",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("20"),
        "kg",
        Decimal("2100"),
        ("मसूर बीज", "masoor", "lentil"),
        crop_targets=("lentil",),
    ),
    ProductSeed(
        "SED-MNG-SML",
        "Moong SML-668",
        "मूँग एसएमएल",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("10"),
        "kg",
        Decimal("1300"),
        ("मूँग बीज", "moong", "मूंग"),
        crop_targets=("moong",),
    ),
    ProductSeed(
        "SED-TOM-HYB",
        "Tomato Hybrid Abhinav",
        "टमाटर हाइब्रिड",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.HYBRID_SEED,
        Decimal("10"),
        "g",
        Decimal("340"),
        ("टमाटर बीज", "tamatar", "tomato seed"),
        crop_targets=("tomato",),
    ),
    ProductSeed(
        "SED-ONI-NRD",
        "Onion Nasik Red",
        "प्याज़ नासिक रेड",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("1"),
        "kg",
        Decimal("1250"),
        ("प्याज़ बीज", "pyaaz", "onion seed"),
        crop_targets=("onion",),
    ),
    ProductSeed(
        "SED-SGC-CO0238",
        "Sugarcane Co-0238 Setts",
        "गन्ना को 0238",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("100"),
        "kg",
        Decimal("450"),
        ("गन्ना बीज", "ganna", "को 0238", "co 0238"),
        crop_targets=("sugarcane",),
    ),
    ProductSeed(
        "SED-ARH-NDA1",
        "Pigeon Pea NDA-1",
        "अरहर एनडीए 1",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.CERTIFIED_SEED,
        Decimal("20"),
        "kg",
        Decimal("2200"),
        ("अरहर बीज", "arhar", "tur", "पिजन पी"),
        crop_targets=("pigeon_pea",),
    ),
    ProductSeed(
        "SED-BER-LKI",
        "Bottle Gourd Lauki Hybrid",
        "लौकी हाइब्रिड",
        "Khushhali",
        ProductCategory.SEEDS,
        ProductType.HYBRID_SEED,
        Decimal("50"),
        "g",
        Decimal("290"),
        ("लौकी बीज", "घीया", "ghiya", "bottle gourd"),
    ),
)

_CATTLE_FEED: tuple[ProductSeed, ...] = (
    ProductSeed(
        "FEED-CTL-50",
        "Cattle Feed Pellet 20% Protein",
        "पशु आहार",
        "Khushhali",
        ProductCategory.CATTLE_FEED,
        ProductType.CATTLE_FEED,
        Decimal("50"),
        "kg",
        Decimal("1180"),
        ("पशु आहार", "cattle feed", "दाना", "चारा"),
        composition=(("Protein", 20.0), ("Fat", 3.0)),
    ),
    ProductSeed(
        "FEED-CTL-BYP",
        "Bypass Protein Feed",
        "बाईपास प्रोटीन",
        "Khushhali",
        ProductCategory.CATTLE_FEED,
        ProductType.CATTLE_FEED,
        Decimal("50"),
        "kg",
        Decimal("1420"),
        ("बाईपास", "bypass", "दूध बढ़ाने वाला दाना"),
        composition=(("Protein", 24.0),),
    ),
    ProductSeed(
        "FEED-MIN-5",
        "Mineral Mixture",
        "मिनरल मिक्सचर",
        "Khushhali",
        ProductCategory.CATTLE_FEED,
        ProductType.CATTLE_FEED,
        Decimal("5"),
        "kg",
        Decimal("480"),
        ("मिनरल", "mineral", "खनिज मिश्रण"),
    ),
    ProductSeed(
        "FEED-CAL-5",
        "Calcium Supplement Liquid",
        "कैल्शियम",
        "Khushhali",
        ProductCategory.CATTLE_FEED,
        ProductType.CATTLE_FEED,
        Decimal("5"),
        "l",
        Decimal("560"),
        ("कैल्शियम", "calcium", "पशु कैल्शियम"),
    ),
)

_TOOLS: tuple[ProductSeed, ...] = (
    ProductSeed(
        "TOOL-SPR-KNP",
        "Knapsack Sprayer 16L Manual",
        "नैपसैक स्प्रेयर",
        "Khushhali",
        ProductCategory.TOOLS_EQUIPMENT,
        ProductType.SPRAYER,
        Decimal("1"),
        "unit",
        Decimal("1450"),
        ("स्प्रे मशीन", "छिड़काव मशीन", "sprayer", "पंप"),
    ),
    ProductSeed(
        "TOOL-SPR-BAT",
        "Battery Sprayer 18L",
        "बैटरी स्प्रेयर",
        "Khushhali",
        ProductCategory.TOOLS_EQUIPMENT,
        ProductType.SPRAYER,
        Decimal("1"),
        "unit",
        Decimal("3200"),
        ("बैटरी पंप", "battery sprayer", "बैटरी वाली मशीन"),
    ),
    ProductSeed(
        "TOOL-IRR-DRP",
        "Drip Irrigation Kit 1 Acre",
        "ड्रिप सिंचाई",
        "Khushhali",
        ProductCategory.TOOLS_EQUIPMENT,
        ProductType.IRRIGATION,
        Decimal("1"),
        "kit",
        Decimal("24500"),
        ("ड्रिप", "drip", "टपक सिंचाई"),
    ),
    ProductSeed(
        "TOOL-IRR-SPK",
        "Sprinkler Set 1 Acre",
        "स्प्रिंकलर",
        "Khushhali",
        ProductCategory.TOOLS_EQUIPMENT,
        ProductType.IRRIGATION,
        Decimal("1"),
        "set",
        Decimal("18200"),
        ("स्प्रिंकलर", "sprinkler", "फव्वारा"),
    ),
    ProductSeed(
        "TOOL-IMP-SED",
        "Seed Drill 9 Tyne",
        "सीड ड्रिल",
        "Khushhali",
        ProductCategory.TOOLS_EQUIPMENT,
        ProductType.IMPLEMENT,
        Decimal("1"),
        "unit",
        Decimal("32000"),
        ("सीड ड्रिल", "seed drill", "बुवाई मशीन"),
    ),
    ProductSeed(
        "TOOL-IMP-ROT",
        "Rotavator 5 Feet",
        "रोटावेटर",
        "Khushhali",
        ProductCategory.TOOLS_EQUIPMENT,
        ProductType.IMPLEMENT,
        Decimal("1"),
        "unit",
        Decimal("74000"),
        ("रोटावेटर", "rotavator", "रोटरी"),
    ),
    ProductSeed(
        "TOOL-HND-KHR",
        "Khurpi Hand Weeder",
        "खुरपी",
        "Khushhali",
        ProductCategory.TOOLS_EQUIPMENT,
        ProductType.IMPLEMENT,
        Decimal("1"),
        "unit",
        Decimal("120"),
        ("खुरपी", "khurpi", "निराई का औज़ार"),
    ),
)

PRODUCTS: tuple[ProductSeed, ...] = (
    *_FERTILISERS,
    *_CROP_PROTECTION,
    *_SEEDS,
    *_CATTLE_FEED,
    *_TOOLS,
)


def cib_for(product: ProductSeed, index: int) -> str | None:
    """Agrochemicals need a registration number; nothing else does (§16.2)."""
    needs = {
        ProductType.INSECTICIDE,
        ProductType.FUNGICIDE,
        ProductType.HERBICIDE,
        ProductType.PGR,
    }
    return _cib(index) if product.product_type in needs else None


# --------------------------------------------------------------------------- #
# Crops -- 12
# --------------------------------------------------------------------------- #


class CropSeed(NamedTuple):
    name_en: str
    name_hi: str
    season: Season
    stages: tuple[tuple[str, str], ...]
    aliases: tuple[str, ...]


CROPS: tuple[CropSeed, ...] = (
    CropSeed(
        "wheat",
        "गेहूँ",
        Season.RABI,
        (
            ("sowing", "बुवाई"),
            ("crown_root", "पहली सिंचाई"),
            ("tillering", "कल्ले"),
            ("flowering", "बाली"),
            ("grain_fill", "दाना भरना"),
        ),
        ("gehun", "gehu", "wheat", "गेहूं"),
    ),
    CropSeed(
        "paddy",
        "धान",
        Season.KHARIF,
        (
            ("nursery", "नर्सरी"),
            ("transplant", "रोपाई"),
            ("tillering", "कल्ले"),
            ("panicle", "बाली"),
            ("grain_fill", "दाना"),
        ),
        ("dhan", "paddy", "rice", "चावल"),
    ),
    CropSeed(
        "potato",
        "आलू",
        Season.RABI,
        (
            ("planting", "बुवाई"),
            ("earthing_up", "मिट्टी चढ़ाना"),
            ("tuber_bulking", "कंद बनना"),
            ("maturity", "पकाई"),
        ),
        ("aloo", "potato", "आलु"),
    ),
    CropSeed(
        "mustard",
        "सरसों",
        Season.RABI,
        (("sowing", "बुवाई"), ("vegetative", "बढ़वार"), ("flowering", "फूल"), ("pod_fill", "फली")),
        ("sarson", "mustard", "रai"),
    ),
    CropSeed(
        "sugarcane",
        "गन्ना",
        Season.PERENNIAL,
        (
            ("planting", "बुवाई"),
            ("tillering", "कल्ले"),
            ("grand_growth", "बढ़वार"),
            ("maturity", "पकाई"),
        ),
        ("ganna", "sugarcane", "ईख"),
    ),
    CropSeed(
        "gram",
        "चना",
        Season.RABI,
        (("sowing", "बुवाई"), ("vegetative", "बढ़वार"), ("flowering", "फूल"), ("pod_fill", "फली")),
        ("chana", "gram", "chickpea"),
    ),
    CropSeed(
        "pea",
        "मटर",
        Season.RABI,
        (("sowing", "बुवाई"), ("vegetative", "बढ़वार"), ("flowering", "फूल"), ("pod_fill", "फली")),
        ("matar", "pea", "मटार"),
    ),
    CropSeed(
        "lentil",
        "मसूर",
        Season.RABI,
        (("sowing", "बुवाई"), ("vegetative", "बढ़वार"), ("flowering", "फूल")),
        ("masoor", "lentil", "मसुर"),
    ),
    CropSeed(
        "maize",
        "मक्का",
        Season.KHARIF,
        (("sowing", "बुवाई"), ("knee_high", "घुटना"), ("tasseling", "फूल"), ("grain_fill", "दाना")),
        ("makka", "maize", "corn", "मकई"),
    ),
    CropSeed(
        "moong",
        "मूँग",
        Season.ZAID,
        (("sowing", "बुवाई"), ("vegetative", "बढ़वार"), ("flowering", "फूल")),
        ("moong", "मूंग", "green gram"),
    ),
    CropSeed(
        "tomato",
        "टमाटर",
        Season.RABI,
        (("nursery", "नर्सरी"), ("transplant", "रोपाई"), ("flowering", "फूल"), ("fruiting", "फल")),
        ("tamatar", "tomato"),
    ),
    CropSeed(
        "onion",
        "प्याज़",
        Season.RABI,
        (("nursery", "नर्सरी"), ("transplant", "रोपाई"), ("bulbing", "गाँठ बनना")),
        ("pyaaz", "onion", "प्याज"),
    ),
)


# --------------------------------------------------------------------------- #
# Problem vocabulary (KB §6)
# --------------------------------------------------------------------------- #


class ProblemSeed(NamedTuple):
    key: str
    problem_type: str
    name_en: str
    name_hi: str
    aliases: tuple[str, ...]
    #: A symptom is not a diagnosis. Where the picture is genuinely ambiguous
    #: the agent must ask before advising, never guess and prescribe.
    requires_clarification: bool = False


PROBLEMS: tuple[ProblemSeed, ...] = (
    ProblemSeed(
        "leaf_yellowing",
        "deficiency",
        "Leaf yellowing",
        "पत्ती पीली पड़ना",
        ("पत्ती पीली", "पीली पत्ती", "pili patti", "yellowing"),
        True,
    ),
    ProblemSeed("blight", "disease", "Blight", "झुलसा", ("झुलसा", "jhulsa", "blight", "झुलसा रोग")),
    ProblemSeed(
        "caterpillar", "pest", "Caterpillar", "सुंडी", ("सुंडी", "इल्ली", "sundi", "illi", "कीड़ा")
    ),
    ProblemSeed("aphid", "pest", "Aphid", "माहू", ("माहू", "चेपा", "mahu", "chepa", "aphid")),
    ProblemSeed(
        "stem_borer", "pest", "Stem borer", "तना छेदक", ("तना छेदक", "stem borer", "तना भेदक")
    ),
    ProblemSeed("rust", "disease", "Rust", "गेरुआ", ("गेरुआ", "रतुआ", "geruaa", "ratua", "rust")),
    ProblemSeed(
        "lodging", "abiotic", "Lodging", "फ़सल गिरना", ("फ़सल गिर गई", "फसल गिरी", "lodging"), True
    ),
    ProblemSeed(
        "root_rot", "disease", "Root rot", "जड़ गलन", ("जड़ गल रही", "जड़ गलन", "root rot"), True
    ),
    ProblemSeed(
        "poor_grain_fill",
        "abiotic",
        "Poor grain filling",
        "दाना न भरना",
        ("दाना नहीं भर रहा", "दाना कमज़ोर"),
        True,
    ),
    ProblemSeed("weeds", "weed", "Weeds", "खरपतवार", ("खरपतवार", "घास", "ghaas", "weeds", "निराई")),
    ProblemSeed("termite", "pest", "Termite", "दीमक", ("दीमक", "deemak", "termite")),
    ProblemSeed(
        "flower_drop",
        "abiotic",
        "Flower drop",
        "फूल झड़ना",
        ("फूल झड़ रहे", "फूल गिर रहे", "flower drop"),
        True,
    ),
)


# --------------------------------------------------------------------------- #
# Crop recommendations -- 40, ALL SEEDED AS DRAFT (KB §5, §9)
# --------------------------------------------------------------------------- #


class RecommendationSeed(NamedTuple):
    crop: str
    stage: str | None
    problem: str | None
    sku: str
    dose_value: Decimal
    dose_unit: str
    dose_basis: DoseBasis
    method: str
    timing_hi: str
    phi_days: int | None = None
    precaution_hi: str | None = None
    interval_days: int | None = None
    max_applications: int | None = None


_PRECAUTION = "छिड़काव के समय दस्ताने और मास्क पहनें। बच्चों और पशुओं को खेत से दूर रखें।"
_BASAL = "बुवाई के समय बीज के साथ नीचे डालें।"
_TOPDRESS = "पहली सिंचाई के बाद खड़ी फ़सल में छिड़कें।"

RECOMMENDATIONS: tuple[RecommendationSeed, ...] = (
    # -- wheat ---------------------------------------------------------- #
    RecommendationSeed(
        "wheat",
        "sowing",
        None,
        "FRT-DAP-50",
        Decimal("50"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "wheat",
        "sowing",
        None,
        "FRT-MOP-50",
        Decimal("20"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "wheat",
        "crown_root",
        None,
        "FRT-URE-45",
        Decimal("45"),
        "kg",
        DoseBasis.PER_ACRE,
        "top_dress",
        _TOPDRESS,
    ),
    RecommendationSeed(
        "wheat",
        "tillering",
        None,
        "FRT-URE-45",
        Decimal("30"),
        "kg",
        DoseBasis.PER_ACRE,
        "top_dress",
        "दूसरी सिंचाई के बाद डालें।",
    ),
    RecommendationSeed(
        "wheat",
        "sowing",
        "weeds",
        "CP-PEN-1000",
        Decimal("700"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "बुवाई के तीन दिन के अंदर छिड़कें।",
        60,
        _PRECAUTION,
    ),
    RecommendationSeed(
        "wheat",
        "tillering",
        "weeds",
        "CP-SUL-250",
        Decimal("13"),
        "g",
        DoseBasis.PER_ACRE,
        "spray",
        "बुवाई के तीस दिन बाद छिड़कें।",
        60,
        _PRECAUTION,
    ),
    RecommendationSeed(
        "wheat",
        "flowering",
        "rust",
        "CP-PRO-250",
        Decimal("200"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "रोग दिखते ही छिड़कें।",
        35,
        _PRECAUTION,
        15,
        2,
    ),
    RecommendationSeed(
        "wheat",
        "tillering",
        "leaf_yellowing",
        "FRT-ZNS-5",
        Decimal("10"),
        "kg",
        DoseBasis.PER_ACRE,
        "soil",
        "पीलापन दिखने पर मिट्टी में डालें।",
    ),
    RecommendationSeed(
        "wheat",
        "sowing",
        None,
        "CP-CAB-500",
        Decimal("2"),
        "g",
        DoseBasis.PER_LITRE_WATER,
        "seed_treatment",
        "बीज को बोने से पहले उपचारित करें।",
        None,
        _PRECAUTION,
    ),
    RecommendationSeed(
        "wheat",
        "grain_fill",
        "aphid",
        "CP-IMD-250",
        Decimal("60"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "माहू दिखने पर छिड़कें।",
        40,
        _PRECAUTION,
        15,
        2,
    ),
    # -- paddy ---------------------------------------------------------- #
    RecommendationSeed(
        "paddy",
        "transplant",
        None,
        "FRT-DAP-50",
        Decimal("45"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        "रोपाई से पहले खेत में मिलाएँ।",
    ),
    RecommendationSeed(
        "paddy",
        "tillering",
        None,
        "FRT-URE-45",
        Decimal("35"),
        "kg",
        DoseBasis.PER_ACRE,
        "top_dress",
        "रोपाई के बीस दिन बाद डालें।",
    ),
    RecommendationSeed(
        "paddy",
        "panicle",
        None,
        "FRT-MOP-50",
        Decimal("15"),
        "kg",
        DoseBasis.PER_ACRE,
        "top_dress",
        "बाली निकलने से पहले डालें।",
    ),
    RecommendationSeed(
        "paddy",
        "tillering",
        "stem_borer",
        "CP-CAR-500",
        Decimal("400"),
        "g",
        DoseBasis.PER_ACRE,
        "broadcast",
        "तना छेदक दिखने पर डालें।",
        30,
        _PRECAUTION,
        20,
        2,
    ),
    RecommendationSeed(
        "paddy",
        "tillering",
        "stem_borer",
        "CP-CHL-250",
        Decimal("60"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "सुंडी दिखते ही छिड़कें।",
        25,
        _PRECAUTION,
        20,
        2,
    ),
    RecommendationSeed(
        "paddy",
        "panicle",
        "blight",
        "CP-TRI-250",
        Decimal("80"),
        "g",
        DoseBasis.PER_ACRE,
        "spray",
        "बाली आने पर छिड़कें।",
        30,
        _PRECAUTION,
        15,
        2,
    ),
    RecommendationSeed(
        "paddy",
        "transplant",
        "weeds",
        "CP-BIS-100",
        Decimal("100"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "रोपाई के बीस दिन बाद छिड़कें।",
        45,
        _PRECAUTION,
    ),
    RecommendationSeed(
        "paddy",
        "nursery",
        None,
        "FRT-ZNS-5",
        Decimal("10"),
        "kg",
        DoseBasis.PER_ACRE,
        "soil",
        "नर्सरी में मिट्टी में मिलाएँ।",
    ),
    # -- potato --------------------------------------------------------- #
    RecommendationSeed(
        "potato",
        "planting",
        None,
        "FRT-NPK-123216",
        Decimal("100"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "potato",
        "planting",
        None,
        "FRT-MOP-50",
        Decimal("40"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "potato",
        "earthing_up",
        None,
        "FRT-URE-45",
        Decimal("35"),
        "kg",
        DoseBasis.PER_ACRE,
        "top_dress",
        "मिट्टी चढ़ाते समय डालें।",
    ),
    RecommendationSeed(
        "potato",
        "tuber_bulking",
        "blight",
        "CP-MAN-1000",
        Decimal("600"),
        "g",
        DoseBasis.PER_ACRE,
        "spray",
        "झुलसा दिखते ही छिड़कें।",
        15,
        _PRECAUTION,
        10,
        3,
    ),
    RecommendationSeed(
        "potato",
        "tuber_bulking",
        "blight",
        "CP-CYM-500",
        Decimal("300"),
        "g",
        DoseBasis.PER_ACRE,
        "spray",
        "पछेती झुलसा में छिड़कें।",
        15,
        _PRECAUTION,
        10,
        3,
    ),
    RecommendationSeed(
        "potato",
        "planting",
        "weeds",
        "CP-PEN-1000",
        Decimal("700"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "बुवाई के बाद छिड़कें।",
        60,
        _PRECAUTION,
    ),
    RecommendationSeed(
        "potato",
        "tuber_bulking",
        "aphid",
        "CP-IMD-250",
        Decimal("60"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "माहू दिखने पर छिड़कें।",
        40,
        _PRECAUTION,
        15,
        2,
    ),
    # -- mustard -------------------------------------------------------- #
    RecommendationSeed(
        "mustard",
        "sowing",
        None,
        "FRT-DAP-50",
        Decimal("35"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "mustard",
        "sowing",
        None,
        "FRT-SUL-25",
        Decimal("10"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        "गंधक बुवाई के समय डालें।",
    ),
    RecommendationSeed(
        "mustard",
        "flowering",
        "aphid",
        "CP-IMD-250",
        Decimal("60"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "माहू दिखते ही छिड़कें।",
        40,
        _PRECAUTION,
        15,
        2,
    ),
    RecommendationSeed(
        "mustard",
        "vegetative",
        None,
        "CP-SUL-1000",
        Decimal("500"),
        "g",
        DoseBasis.PER_ACRE,
        "spray",
        "सफ़ेद रतुआ की रोकथाम के लिए।",
        20,
        _PRECAUTION,
        12,
        2,
    ),
    RecommendationSeed(
        "mustard",
        "vegetative",
        None,
        "FRT-URE-45",
        Decimal("25"),
        "kg",
        DoseBasis.PER_ACRE,
        "top_dress",
        "पहली सिंचाई के बाद डालें।",
    ),
    # -- sugarcane ------------------------------------------------------ #
    RecommendationSeed(
        "sugarcane",
        "planting",
        None,
        "FRT-DAP-50",
        Decimal("50"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        "नाली में बीज के साथ डालें।",
    ),
    RecommendationSeed(
        "sugarcane",
        "tillering",
        None,
        "FRT-URE-45",
        Decimal("60"),
        "kg",
        DoseBasis.PER_ACRE,
        "top_dress",
        "कल्ले निकलते समय डालें।",
    ),
    RecommendationSeed(
        "sugarcane",
        "planting",
        "termite",
        "CP-IMD-250",
        Decimal("150"),
        "ml",
        DoseBasis.PER_ACRE,
        "drench",
        "बुवाई के समय नाली में डालें।",
        None,
        _PRECAUTION,
    ),
    # -- gram / pea / lentil -------------------------------------------- #
    RecommendationSeed(
        "gram",
        "sowing",
        None,
        "FRT-DAP-50",
        Decimal("30"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "gram",
        "flowering",
        "caterpillar",
        "CP-LAM-250",
        Decimal("120"),
        "ml",
        DoseBasis.PER_ACRE,
        "spray",
        "फली छेदक दिखने पर छिड़कें।",
        30,
        _PRECAUTION,
        15,
        2,
    ),
    RecommendationSeed(
        "gram",
        "sowing",
        None,
        "CP-THM-100",
        Decimal("2"),
        "g",
        DoseBasis.PER_LITRE_WATER,
        "seed_treatment",
        "उकठा से बचाव के लिए बीजोपचार करें।",
        None,
        _PRECAUTION,
    ),
    RecommendationSeed(
        "pea",
        "sowing",
        None,
        "FRT-SSP-50",
        Decimal("60"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "lentil",
        "sowing",
        None,
        "FRT-DAP-50",
        Decimal("25"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    # -- maize ---------------------------------------------------------- #
    RecommendationSeed(
        "maize",
        "sowing",
        None,
        "FRT-NPK-123216",
        Decimal("80"),
        "kg",
        DoseBasis.PER_ACRE,
        "basal",
        _BASAL,
    ),
    RecommendationSeed(
        "maize",
        "knee_high",
        "caterpillar",
        "CP-EMA-100",
        Decimal("80"),
        "g",
        DoseBasis.PER_ACRE,
        "spray",
        "फॉल आर्मीवर्म दिखने पर छिड़कें।",
        21,
        _PRECAUTION,
        15,
        2,
    ),
)


# --------------------------------------------------------------------------- #
# Staff -- one per role (§10)
# --------------------------------------------------------------------------- #


class UserSeed(NamedTuple):
    email: str
    full_name: str
    role: Role
    #: Centre codes this user may see. Empty means org-wide.
    centres: tuple[str, ...] = ()


USERS: tuple[UserSeed, ...] = (
    UserSeed("admin@uaagro.in", "Aarav Mishra", Role.SUPER_ADMIN),
    UserSeed("ops@uaagro.in", "Priya Verma", Role.OPS_MANAGER),
    UserSeed(
        "barabanki.manager@uaagro.in",
        "Rakesh Yadav",
        Role.CENTRE_MANAGER,
        ("NKSK-BBK-01", "NKSK-BBK-02"),
    ),
    UserSeed("sitapur.manager@uaagro.in", "Sunita Devi", Role.CENTRE_MANAGER, ("NKSK-STP-01",)),
    UserSeed(
        "agronomist@uaagro.in",
        "Dr Neha Singh",
        Role.AGRONOMIST,
        ("NKSK-BBK-01", "NKSK-STP-01", "NKSK-LKO-01"),
    ),
    UserSeed("auditor@uaagro.in", "Vikram Rao", Role.AUDITOR),
    UserSeed("viewer@uaagro.in", "Anil Kumar", Role.READ_ONLY, ("NKSK-LKO-01",)),
)

#: Development password for every seeded account. Overridden by
#: ``UAAGRO_SEED_PASSWORD``; the loader refuses to run at all when
#: ``APP_ENV`` is staging or production.

# and the loader refuses to run when APP_ENV is staging or production.
DEFAULT_SEED_PASSWORD = "DevOnly!Passw0rd"  # noqa: S105


# --------------------------------------------------------------------------- #
# Farmer name pools -- 200 farmers are generated deterministically
# --------------------------------------------------------------------------- #

FARMER_FIRST_NAMES: tuple[tuple[str, str], ...] = (
    ("Ramesh", "रमेश"),
    ("Suresh", "सुरेश"),
    ("Rajesh", "राजेश"),
    ("Mahesh", "महेश"),
    ("Dinesh", "दिनेश"),
    ("Mukesh", "मुकेश"),
    ("Santosh", "संतोष"),
    ("Vinod", "विनोद"),
    ("Ashok", "अशोक"),
    ("Rajendra", "राजेन्द्र"),
    ("Shyam", "श्याम"),
    ("Mohan", "मोहन"),
    ("Sohan", "सोहन"),
    ("Krishna", "कृष्ण"),
    ("Balram", "बलराम"),
    ("Devendra", "देवेन्द्र"),
    ("Sunita", "सुनीता"),
    ("Kamla", "कमला"),
    ("Savitri", "सावित्री"),
    ("Rekha", "रेखा"),
    ("Geeta", "गीता"),
    ("Shanti", "शांति"),
    ("Urmila", "उर्मिला"),
    ("Pushpa", "पुष्पा"),
)

FARMER_SURNAMES: tuple[tuple[str, str], ...] = (
    ("Yadav", "यादव"),
    ("Verma", "वर्मा"),
    ("Singh", "सिंह"),
    ("Kumar", "कुमार"),
    ("Maurya", "मौर्य"),
    ("Pandey", "पांडेय"),
    ("Tiwari", "तिवारी"),
    ("Gupta", "गुप्ता"),
    ("Nishad", "निषाद"),
    ("Rajput", "राजपूत"),
    ("Shukla", "शुक्ला"),
    ("Dixit", "दीक्षित"),
)

VILLAGE_NAMES: tuple[str, ...] = (
    "Rampur",
    "Shivpuri",
    "Kishanganj",
    "Bhagwanpur",
    "Nayagaon",
    "Madhopur",
    "Sarai Ganj",
    "Chandpur",
    "Devipur",
    "Hariharpur",
    "Jalalpur",
    "Kamalpur",
    "Lakhanpur",
    "Mubarakpur",
    "Narayanpur",
    "Pipraul",
    "Raghunathpur",
    "Sultanpur Khurd",
    "Tikri",
    "Uska Bazar",
    "Vishunpur",
    "Bakhtiyarpur",
    "Gopalpur",
    "Dhanauli",
)

IRRIGATION_TYPES: tuple[str, ...] = ("tubewell", "canal", "rainfed", "borewell")
SOIL_TYPES: tuple[str, ...] = ("loam", "sandy_loam", "clay_loam", "alluvial")
FARMER_SEGMENTS: tuple[str, ...] = ("smallholder", "medium", "large", "progressive")


# --------------------------------------------------------------------------- #
# Answer cache -- seeds the head of the query distribution (KB §8, §9 Tier 3)
# --------------------------------------------------------------------------- #


class AnswerSeed(NamedTuple):
    intent_key: str
    variants: tuple[str, ...]
    answer_hi: str
    #: True for anything containing a price or stock figure. Such rows are
    #: never activated -- §9 sends those to Tier 1 on every turn.
    volatile: bool = False


ANSWERS: tuple[AnswerSeed, ...] = (
    AnswerSeed(
        "centre_timings",
        ("दुकान कितने बजे खुलती है", "सेंटर का टाइम क्या है", "कब तक खुला रहता है"),
        "जी, हमारा केंद्र सुबह आठ बजे से शाम सात बजे तक खुला रहता है। रविवार को बंद रहता है।",
    ),
    AnswerSeed(
        "is_product_genuine",
        ("क्या दवा असली है", "नकली तो नहीं है", "माल असली है क्या"),
        "जी बिल्कुल। हमारे सारे उत्पाद लाइसेंस वाले और जाँचे हुए होते हैं, और हर ख़रीद पर पक्का बिल मिलता है।",
    ),
    AnswerSeed(
        "soil_testing",
        ("मिट्टी जाँच कैसे कराएँ", "मिट्टी की जाँच", "soil test"),
        "जी, मिट्टी जाँच की सुविधा हमारे केंद्र पर है। आप खेत से नमूना लेकर आइए, हम जाँच करा कर "
        "रिपोर्ट और सलाह दे देंगे। मैं आपके लिए बुकिंग कर दूँ?",
    ),
    AnswerSeed(
        "drone_spraying",
        ("ड्रोन से छिड़काव", "ड्रोन सेवा", "drone spray"),
        "जी, ड्रोन से छिड़काव की सेवा उपलब्ध है। मैं आपका नाम और खेत का ब्यौरा नोट कर लेता हूँ, "
        "केंद्र से आपको रेट और तारीख़ बता दी जाएगी।",
    ),
    AnswerSeed(
        "franchise_enquiry",
        ("दुकान खोलनी है", "फ़्रैंचाइज़ी लेनी है", "डीलरशिप चाहिए"),
        "जी, यह अच्छी बात है। मैं आपकी बात हमारे ज़िम्मेदार अधिकारी तक पहुँचा देता हूँ, "
        "वो आपसे पूरी जानकारी लेकर बात करेंगे।",
    ),
    AnswerSeed(
        "are_you_a_robot",
        ("आप रोबोट हो क्या", "आप मशीन हो", "क्या आप इंसान हैं"),
        "जी, मैं यूए एग्रो का ऑटोमैटिक सहायक हूँ। किसी व्यक्ति से बात करनी हो तो तुरंत जोड़ दूँगा।",
    ),
    AnswerSeed(
        "services_offered",
        ("आप क्या क्या सेवा देते हैं", "कौन सी सुविधा है"),
        "जी, हम बीज, खाद, दवाई, पशु आहार और खेती के औज़ार देते हैं। साथ में मिट्टी जाँच, "
        "खेत पर सलाह, ड्रोन से छिड़काव और किसान गोष्ठी भी कराते हैं।",
    ),
    AnswerSeed(
        "kisan_gosthi",
        ("किसान गोष्ठी कब है", "गोष्ठी"),
        "जी, किसान गोष्ठी गाँव-गाँव में होती रहती है। मैं आपका नंबर नोट कर लेता हूँ, "
        "आपके इलाक़े में अगली गोष्ठी की ख़बर आपको दे दी जाएगी।",
    ),
    AnswerSeed(
        "bill_available", ("बिल मिलेगा", "पक्का बिल", "रसीद"), "जी हाँ, हर ख़रीद पर पक्का बिल मिलता है।"
    ),
    AnswerSeed(
        "credit_terms",
        ("उधार मिलेगा", "क्रेडिट", "बाद में पैसा दूँगा"),
        "जी, उधार की बात केंद्र प्रबंधक ही तय करते हैं। मैं आपको उनसे जोड़ देता हूँ।",
    ),
    AnswerSeed(
        "scheme_general",
        ("सब्सिडी", "सरकारी योजना", "पीएम किसान"),
        "जी, सरकारी योजनाओं की जानकारी मैं आम तौर पर दे सकता हूँ, लेकिन आपको योजना मिलेगी या "
        "नहीं यह मैं नहीं बता सकता। इसके लिए आप कृषि विभाग या हमारे केंद्र प्रबंधक से बात कर लीजिए।",
    ),
    AnswerSeed(
        "home_delivery",
        ("होम डिलीवरी है", "घर पहुँचाओगे", "delivery"),
        "जी, डिलीवरी की व्यवस्था केंद्र के हिसाब से अलग होती है। मैं आपको केंद्र प्रबंधक से "
        "जोड़ देता हूँ, वो सही बता देंगे।",
    ),
    AnswerSeed(
        "seed_guarantee",
        ("बीज की गारंटी है", "पैदावार की गारंटी"),
        "जी, बीज प्रमाणित होता है, लेकिन पैदावार मौसम और खेती पर निर्भर करती है, इसलिए मैं "
        "उसकी गारंटी नहीं दे सकता। बाक़ी जानकारी के लिए केंद्र प्रबंधक से जोड़ दूँ?",
    ),
    AnswerSeed(
        "callback_request",
        ("बाद में फ़ोन करना", "अभी व्यस्त हूँ"),
        "जी बिल्कुल, कोई बात नहीं। मैंने नोट कर लिया है, आपको बाद में कॉल कर लिया जाएगा।",
    ),
    AnswerSeed(
        "complaint_general",
        ("शिकायत करनी है", "माल ख़राब निकला", "दवा काम नहीं की"),
        "जी, मुझे खेद है। मैं आपकी शिकायत दर्ज कर रहा हूँ और आपको केंद्र प्रबंधक से जोड़ देता हूँ।",
    ),
)


# --------------------------------------------------------------------------- #
# Spam rules (§11.1) -- deliberately loose defaults
# --------------------------------------------------------------------------- #

SPAM_RULES: tuple[dict[str, Any], ...] = (
    {"rule_type": "call_velocity_hour", "action": "flag", "threshold": 6, "window_seconds": 3600},
    {"rule_type": "call_velocity_day", "action": "flag", "threshold": 20, "window_seconds": 86400},
    {"rule_type": "repeat_abandon", "action": "flag", "threshold": 4, "window_seconds": 86400},
)
