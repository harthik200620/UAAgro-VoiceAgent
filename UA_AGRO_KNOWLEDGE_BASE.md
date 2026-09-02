# UA AGRO — VOICE AGENT KNOWLEDGE BASE (SEED v0.1)

> **Status legend.** Every block is tagged.
> `[VERIFIED]` — sourced from UA Agro's public website or a public company record; safe to serve.
> `[TEMPLATE]` — correct *structure*, placeholder *content*. UA Agro staff must populate before go-live.
> `[VERIFY]` — general agronomy or market knowledge that is broadly correct for Uttar Pradesh but
> **must be confirmed by a UA Agro agronomist** for local varieties, seasons and regulations before
> the agent is allowed to speak it.
>
> **Ingestion rule for the build:** content tagged `[TEMPLATE]` or `[VERIFY]` is loaded into the
> database in `draft` state and is **not retrievable by the agent** until an agronomist marks it
> approved in the admin panel. Only `[VERIFIED]` blocks ship as published on day one.

---

## 1. COMPANY IDENTITY `[VERIFIED]`

| Field | Value |
|---|---|
| Legal entity | UA Agro Solutions Private Limited |
| Retail brand | Naveen Khushhali Kisan Sewa Kendra |
| Headquarters | Lucknow, Uttar Pradesh |
| Toll-free helpline | 1800 212 7074 |
| Missed-call number | +91 7232020335 |
| Email | contact@uaagro.in |
| Website | www.uaagro.in |
| Retail centres | 80+ |
| Districts served | 15 (central and eastern Uttar Pradesh) |
| Villages connected | 3,200+ |
| Farmers connected | ~1.5 lakh |
| Agronomists on staff | 160+ |
| Regional managers | 15+ |
| Stated expansion goal | 200+ stores by 2030 |
| Partner brands | Bayer, Crystal Crop Protection, and 23+ others |

### How the agent introduces the company

**Hindi:** *"नमस्ते! मैं यूए एग्रो के नवीन खुशहाली किसान सेवा केंद्र से बोल रहा हूँ। हम किसान भाइयों को
बीज, खाद, दवाई, पशु आहार और खेती के औज़ार — सब कुछ एक ही जगह उपलब्ध कराते हैं, और साथ में मुफ़्त
सलाह भी देते हैं।"*

**English:** *"Namaste! I'm calling from UA Agro's Naveen Khushhali Kisan Sewa Kendra. We provide
farmers with seeds, fertilisers, crop protection, cattle feed and farm equipment — all in one place —
along with free agronomic advice."*

### Business lines `[VERIFIED]`

1. **Seeds** — certified hybrid varieties across cereals, pulses, vegetables and oilseeds.
2. **Fertilisers and plant nutrition** — NPK blends, micronutrients, organic and bio-fertilisers.
3. **Crop protection** — insecticides, fungicides, herbicides.
4. **Cattle feed** — nutritionally balanced livestock feed.
5. **Tools and equipment** — sprayers, irrigation equipment, farm implements and machinery.

### Services `[VERIFIED]`

| Service | Hindi name | What the agent says it is |
|---|---|---|
| Kisan Gosthi | किसान गोष्ठी | Village-level farmer meetings run by UA Agro agronomists |
| Field advisory | खेत पर सलाह | An agronomist visits the farmer's field |
| In-store advisory | दुकान पर सलाह | Free consultation at any centre |
| Soil testing | मिट्टी जाँच | Soil sample testing with a report and recommendations |
| Drone spraying | ड्रोन से छिड़काव | Drone-based spray service |
| Digital advisory | डिजिटल सलाह | Advisory over phone and WhatsApp |
| FPO linkage | एफपीओ जुड़ाव | Connecting farmers with Farmer Producer Organisations |

**Booking rule:** the agent may **describe** any service and **create a ticket** to book it. It must
never quote a service price or promise a visit date — those come from the centre manager.

---

## 2. CENTRE DIRECTORY `[TEMPLATE]`

Load from the operations team's master list. One row per centre; this feeds `find_nearest_centre`.

```csv
centre_code,centre_name,district,block,address_line,pincode,latitude,longitude,phone,manager_name,manager_phone,open_time,close_time,working_days,services_offered,is_active
NKSK-BBK-01,Naveen Khushhali Kisan Sewa Kendra - Barabanki,Barabanki,Fatehpur,"<address>",225301,26.9250,81.1900,<phone>,<name>,<phone>,08:00,19:00,"Mon-Sat","soil_testing;drone_spray;field_advisory",true
```

Agent behaviour for centre queries:
1. Match on the farmer's stored village, then block, then district, then pincode.
2. Return the **nearest open** centre. If it is closed now, say so and give its opening time.
3. Speak the address in a way a person can act on: landmark first, then road, then town.
4. Offer to WhatsApp the address, map link and phone number (`centre_location` template).

---

## 3. PRODUCT CATALOGUE `[TEMPLATE]`

The catalogue is **live data in Postgres**, not prose in this document. This section defines the
import contract and the rules the agent follows when speaking about products.

### 3.1 Import schema

```csv
sku,name_en,name_hi,brand,category,product_type,composition_json,active_ingredients,formulation,crop_targets,pest_targets,cib_registration_no,is_restricted,requires_licence,pack_size_value,pack_size_unit,mrp,gst_rate,lexicon_variants
```

`category` ∈ `seeds | fertilisers | crop_protection | cattle_feed | tools_equipment`
`product_type` ∈ `hybrid_seed | certified_seed | npk | straight_fertiliser | micronutrient |
bio_fertiliser | organic_manure | insecticide | fungicide | herbicide | pgr | cattle_feed |
sprayer | irrigation | implement`

`lexicon_variants` is the highest-leverage column in the file. It carries every way a farmer might
*say* the product, so the ASR post-correction pass can resolve it. Example for DAP:
`डीएपी;डी ए पी;dap;dee ay pee;डीएपी खाद;काली खाद`

### 3.2 What the agent must be able to answer for any product

| Question | Source |
|---|---|
| "क्या यह उपलब्ध है?" | `inventory.is_available` at the resolved centre — live, never cached |
| "रेट क्या है?" | `inventory.selling_price` / `discount_price` — live, never cached |
| "कितने की बोरी/बोतल आती है?" | `product_variants.pack_size_value` + `pack_size_unit` + price |
| "इसमें क्या-क्या है?" | `products.composition` — read as percentages of named nutrients/ingredients |
| "किस फ़सल में डालते हैं?" | `products.crop_targets` |
| "किस कीड़े/बीमारी के लिए है?" | `products.pest_targets` |
| "कितना डालना है?" | **`crop_recommendations` only** — never the model's own knowledge |
| "कोई सस्ता विकल्प?" | Same `product_type` + overlapping `active_ingredients`, in stock, ranked by price |

### 3.3 Composition example — how to speak a fertiliser `[VERIFY]`

For an NPK complex labelled 12-32-16, the agent says:

> *"इसमें बारह प्रतिशत नाइट्रोजन, बत्तीस प्रतिशत फ़ॉस्फ़ोरस और सोलह प्रतिशत पोटाश है। यानी बुवाई के
> समय जड़ बनने के लिए इसमें फ़ॉस्फ़ोरस ज़्यादा है।"*

Rules: read each figure as a percentage of the named nutrient, never as "twelve thirty-two sixteen".
Add **one** sentence of plain-language meaning. Do not add a dose unless asked, and if asked, take it
from `crop_recommendations`.

### 3.4 Stock-out behaviour

Never say only "नहीं है". Always: acknowledge → give an alternative → give a restock date if known →
offer a callback.

> *"अभी उस केंद्र पर यह उपलब्ध नहीं है जी। लेकिन इसी काम के लिए <alternative> है, जो <price> का
> आता है। और <product> की नई खेप <date> तक आने की उम्मीद है — चाहें तो मैं आपके लिए नोट कर दूँ,
> आते ही आपको ख़बर कर दी जाएगी?"*

---

## 4. CROP CALENDAR — UTTAR PRADESH `[VERIFY]`

General for the UP plains. Local sowing windows shift by district and by monsoon onset; the agronomy
team must confirm district-level dates before this is published.

| Season | Months | Principal crops in UA Agro's districts |
|---|---|---|
| **Kharif** (monsoon) | Sowing Jun–Jul, harvest Sep–Oct | Paddy (धान), maize (मक्का), pigeon pea (अरहर), soybean, cotton, sugarcane (ratoon) |
| **Rabi** (winter) | Sowing Oct–Dec, harvest Mar–Apr | Wheat (गेहूँ), mustard (सरसों), potato (आलू), gram (चना), pea (मटर), lentil (मसूर), barley (जौ) |
| **Zaid** (summer) | Sowing Mar–Apr, harvest Jun | Moong (मूँग), cucurbits (लौकी, खीरा, तरबूज़), fodder maize, sunflower |
| **Sugarcane** (गन्ना) | Autumn planting Sep–Oct; spring planting Feb–Mar | Year-round crop; central/eastern UP is a major belt |

**Why the agent needs this:** the *seasonal hint* in the system prompt (§6.2 of the build spec) is
derived from this table. In late October, "आलू में क्या डालें?" almost always means planting-time
basal application, not a mid-season spray — and the agent should ask one clarifying question
(*"अभी बुवाई कर रहे हैं या फ़सल खड़ी है?"*) rather than assume.

### Call-volume implications for capacity planning

| Window | Expected driver |
|---|---|
| Jun–Jul | Kharif seed and basal fertiliser; peak DAP/urea demand |
| Oct–Nov | Rabi sowing — the largest peak of the year; wheat seed, DAP, potato seed |
| Dec–Jan | Rabi top-dressing, urea, frost and disease enquiries |
| Feb–Mar | Harvest-time, mustard and gram protection, sugarcane planting |
| Any time | Fertiliser subsidy news and price changes cause sudden volume spikes |

---

## 5. CROP RECOMMENDATION TABLE `[TEMPLATE + AGRONOMIST APPROVAL REQUIRED]`

**This is the safety-critical table.** No row is served to a farmer until an agronomist has approved
it in the admin panel. The agent is forbidden from generating a dose. If no approved row matches, it
says so and escalates.

### Import schema

```csv
crop,growth_stage,problem_type,problem_name_hi,product_sku,dose_value,dose_unit,dose_basis,
application_method,timing_note_hi,interval_days,max_applications,phi_days,
precaution_note_hi,region_scope,season,source_document,priority
```

- `dose_basis` ∈ `per_acre | per_bigha | per_katha | per_hectare | per_litre_water | per_plant`
  — **store all three of acre, bigha and katha** where they differ regionally, because a farmer in
  Barabanki thinks in bigha and the pack label is in acres. The `calculate_dose` tool converts, but
  the source row must be unambiguous.
- `phi_days` — pre-harvest interval. **The agent must speak this with every crop-protection
  recommendation.**
- `precaution_note_hi` — the agent must speak at least one precaution with every spray
  recommendation.

### Coverage the agronomy team should populate first

Ordered by expected call volume:

| Priority | Crop | Stages to cover |
|---|---|---|
| 1 | Wheat (गेहूँ) | Basal, first irrigation top-dress, weed control, rust/yellowing, late-season |
| 2 | Paddy (धान) | Nursery, transplant basal, tillering, stem borer / blast, panicle |
| 3 | Potato (आलू) | Basal at planting, earthing-up, late blight, aphid, storage |
| 4 | Mustard (सरसों) | Basal, aphid, white rust |
| 5 | Sugarcane (गन्ना) | Planting, ratoon management, borer, red rot |
| 6 | Gram / pea / lentil | Basal, pod borer, wilt |
| 7 | Maize (मक्का) | Basal, fall armyworm, top-dress |
| 8 | Vegetables | Nursery, transplant, common pests |

### Worked example of the *answer shape* the agent produces `[TEMPLATE — figures illustrative]`

Farmer: *"आलू में क्या डालें?"*

Agent:
> *"जी, अभी आप बुवाई कर रहे हैं या फ़सल खड़ी है?"*

Farmer: *"बुवाई कर रहे हैं, एक बीघा है।"*

Agent (from an **approved** row, with the dose converted per bigha by `calculate_dose`):
> *"ठीक है जी। एक बीघे आलू के लिए बुवाई के समय `<product>` `<dose>` डालिए, बीज के साथ नीचे। साथ में
> `<product2>` `<dose2>` मिला दीजिए। यह दोनों हमारे `<centre>` केंद्र पर उपलब्ध हैं — `<price>` में
> पड़ेगा। चाहें तो पूरी जानकारी व्हाट्सऐप पर भेज दूँ?"*

Note the shape: clarify → dose in **the farmer's own unit** → availability → price → offer WhatsApp.
Never a lecture. Never a dose without a source row.

---

## 6. COMMON PROBLEM VOCABULARY `[VERIFY]`

Farmers describe symptoms, not diagnoses. Map their words to `crop_problems` rows so the agent can
route correctly. This list feeds both intent classification and the ASR lexicon.

| What the farmer says | Literal meaning | Likely problem class |
|---|---|---|
| पत्ती पीली पड़ रही है | leaves turning yellow | nitrogen deficiency, waterlogging, or disease — **ask before advising** |
| झुलसा लग गया | blight / scorch | fungal blight |
| सुंडी / इल्ली लग गई | caterpillar | lepidopteran larvae |
| माहू / चेपा | aphid | sucking pest |
| तना छेदक | stem borer | borer complex |
| गेरुआ / रतुआ | rust | rust disease |
| फ़सल गिर गई | crop lodged | lodging — nutrition or wind |
| जड़ गल रही है | root rotting | root rot / waterlogging |
| दाना नहीं भर रहा | grain not filling | nutrition, heat, or disease |
| खरपतवार / घास बहुत है | too many weeds | weed management |
| दीमक लग गई | termite | soil pest |
| फूल झड़ रहे हैं | flower drop | nutritional or hormonal |

**Mandatory behaviour:** a symptom is not a diagnosis. The agent asks **at most two** clarifying
questions (crop and stage; what the affected part looks like), and if the picture is still ambiguous
it says so and offers a field visit or a transfer. It never guesses a disease and then prescribes for
the guess. Where a photograph would settle it, the agent offers the WhatsApp number and creates a
ticket for the agronomist.

---

## 7. UNIT CONVERSION `[VERIFY — bigha varies by district]`

The `calculate_dose` tool must handle all of these. **The bigha is not a fixed unit in Uttar Pradesh
— it varies by district and even by tehsil.** Store a per-district conversion factor in the `districts`
table, default to the locally prevailing value, and when the conversion materially changes the dose,
have the agent confirm: *"आपके यहाँ बीघा कितने का होता है जी?"*

| Unit | Typical relation | Note |
|---|---|---|
| 1 acre | 4,047 m² | Fixed |
| 1 hectare | 2.47 acres | Fixed |
| 1 bigha (UP, pucca) | ~0.625 acre | **District-dependent — verify per district** |
| 1 bigha (UP, kacha) | ~0.208 acre | **District-dependent** |
| 1 katha | 1/20 bigha | Varies with the local bigha |
| 1 quintal (कुंतल) | 100 kg | Fixed |
| 1 bag/bori (बोरी) of urea | 45 kg | Confirm current pack size |
| 1 bag/bori (बोरी) of DAP | 50 kg | Confirm current pack size |

Speaking rule: give the answer in the unit the farmer used, then the pack count.
*"एक बीघे के लिए लगभग `<x>` किलो — यानी `<n>` बोरी।"*

---

## 8. FREQUENTLY ASKED QUESTIONS `[VERIFIED where marked; rest TEMPLATE]`

These seed the Tier-3 answer cache. Each becomes a row in `answer_cache` with pre-synthesised audio.

| # | Question (Hindi) | Answer source |
|---|---|---|
| 1 | आपकी दुकान कहाँ है? | `find_nearest_centre` — live |
| 2 | दुकान कितने बजे खुलती है? | `centres.open_time` — live |
| 3 | क्या होम डिलीवरी है? | `[TEMPLATE]` — operations to confirm policy |
| 4 | उधार / क्रेडिट मिलेगा? | `[TEMPLATE]` — **always transfer to the manager**, never quote terms |
| 5 | मिट्टी जाँच कैसे कराएँ? | `[VERIFIED]` — soil testing is offered; create a ticket to book |
| 6 | ड्रोन से छिड़काव कराना है | `[VERIFIED]` — service exists; create a ticket, do not quote a price |
| 7 | क्या दवा असली है? | `[VERIFIED]` — lab-tested, government-licensed, ISO-certified products |
| 8 | बिल मिलेगा? | `[TEMPLATE]` — confirm and answer plainly |
| 9 | दुकान खोलनी है / फ़्रैंचाइज़ी लेनी है | `[VERIFIED]` — franchise programme exists → **high-priority ticket + transfer** |
| 10 | नौकरी चाहिए | `[VERIFIED]` — careers exist → capture details, create ticket, do not discuss terms |
| 11 | सब्सिडी / सरकारी योजना | **Explain generally only.** Never assert eligibility. Direct to official channel or a human |
| 12 | पिछली बार जो दवा ली थी, काम नहीं किया | Complaint → ticket + **transfer** |
| 13 | रेट कम नहीं हो सकता? | Bargaining → **transfer to manager**, agent never negotiates price |
| 14 | बीज की गारंटी है? | Never promise yield or germination outcomes → transfer |
| 15 | आप रोबोट हो क्या? | *"जी, मैं यूए एग्रो का ऑटोमैटिक सहायक हूँ। किसी व्यक्ति से बात करनी हो तो तुरंत जोड़ दूँगा।"* |

---

## 9. CONVERSATION SNIPPETS — THE TARGET REGISTER

Use these as few-shot exemplars in the system prompt and as fixtures in the eval suite.

### Greeting a known farmer
> *"नमस्ते रमेश जी! यूए एग्रो के नवीन खुशहाली किसान सेवा केंद्र से बोल रहा हूँ। बताइए, क्या मदद करूँ?"*

### Price and availability
> Farmer: *"डीएपी का रेट क्या चल रहा है?"*
> Agent: *"जी, आपके नज़दीकी `<centre>` केंद्र पर डीएपी की पचास किलो वाली बोरी `<price>` रुपये की है,
> और स्टॉक में है। कितनी बोरी चाहिए?"*

### Number confirmation
> Farmer: *"दस बोरी चाहिए।"*
> Agent: *"जी, दस बोरी डीएपी — सही समझा? तो कुल `<total>` रुपये होंगे।"*

### Not knowing something — the most important exemplar
> *"जी, इस बारे में मेरे पास पक्की जानकारी नहीं है, और मैं अंदाज़े से कुछ नहीं बताना चाहता। मैं आपको
> हमारे केंद्र प्रबंधक से जोड़ देता हूँ, वो सही बता देंगे।"*

### Handling an interruption gracefully
> *"जी बताइए…"* (agent stops mid-sentence, does not restart the interrupted sentence, listens)

### Waiting patiently
> Farmer: *"एक मिनट रुकिए।"*
> Agent: *"जी बिल्कुल, आराम से।"* … (silence up to 25 s) … *"जी, मैं लाइन पर हूँ।"*

### Warm transfer
> *"जी, मैं आपको `<centre>` के केंद्र प्रबंधक `<name>` जी से जोड़ रहा हूँ। एक क्षण रुकिए।"*

### Transfer unavailable
> *"अभी `<name>` जी लाइन पर व्यस्त हैं। मैंने आपका नंबर और आपकी बात नोट कर ली है — आज शाम `<time>`
> बजे तक आपको कॉल आ जाएगी। शिकायत नंबर `<ticket>` है, यह मैं व्हाट्सऐप पर भी भेज रहा हूँ।"*

### Outbound opening
> *"नमस्ते! मैं यूए एग्रो के नवीन खुशहाली किसान सेवा केंद्र से बोल रहा हूँ। यह एक ऑटोमैटिक कॉल है।
> क्या मैं `<name>` जी से बात कर रहा हूँ?… आपका दो मिनट का समय ले सकता हूँ?"*

### Outbound confirmation
> *"अगर आप यह ऑफ़र लेना चाहते हैं तो अपने फ़ोन पर एक दबाइए, या बस 'हाँ' बोल दीजिए।"*

### Opt-out — immediate and unconditional
> *"जी बिल्कुल, मैं आपका नंबर आज ही हटा देता हूँ। आगे से कॉल नहीं आएगी। असुविधा के लिए क्षमा कीजिए।"*

### Safety emergency — cached recording, spoken slowly
> *"जी, यह गंभीर बात है। तुरंत नज़दीकी अस्पताल या डॉक्टर के पास जाइए। दवा का डिब्बा या लेबल साथ ले
> जाइए। मैं अभी आपको हमारे विशेषज्ञ से जोड़ रहा हूँ।"*

---

## 10. ASR LEXICON `[TEMPLATE — expand from the live catalogue]`

Every entry here is injected as an STT keyterm where supported, and drives the post-recognition
fuzzy-correction pass. Generate the full list from `products` and `crops` at build time; this is the
hand-curated core.

### Fertiliser and nutrition
```
डीएपी | dap | डी ए पी | dee-ay-pee | काली खाद
यूरिया | urea | यूरीया | uria | सफ़ेद खाद
पोटाश | potash | एमओपी | mop | म्यूरेट
एनपीके | npk | एन पी के
सिंगल सुपर फॉस्फेट | ssp | एस एस पी | सुपर
जिंक | zinc | जिंक सल्फेट | जस्ता
सल्फर | sulphur | गंधक
वर्मी कम्पोस्ट | vermicompost | केंचुआ खाद
जैविक खाद | organic | बायो खाद | बायोफर्टिलाइज़र
```

### Crop protection
```
दवा | दवाई | davai | spray | छिड़काव | स्प्रे
कीटनाशक | insecticide | कीड़े की दवा
फफूंदनाशक | fungicide | बीमारी की दवा
खरपतवारनाशक | herbicide | घास की दवा | वीडीसाइड
बीज उपचार | seed treatment | बीजोपचार
```

### Crops
```
गेहूँ | gehun | wheat        धान | dhan | paddy | चावल
आलू | aloo | potato          सरसों | sarson | mustard
गन्ना | ganna | sugarcane    मक्का | makka | maize | corn
चना | chana | gram           अरहर | arhar | tur | pigeon pea
मटर | matar | pea            मसूर | masoor | lentil
मूँग | moong                 लौकी | ghiya | bottle gourd
टमाटर | tamatar | tomato     प्याज़ | pyaaz | onion
```

### Units and quantities
```
बीघा | bigha    एकड़ | acre | ekad    कट्ठा | katha
कुंतल | quintal | कुन्तल        बोरी | bori | bag | थैला
किलो | kilo | kg               लीटर | litre | ltr
```

### Escalation and intent keywords (also used by the escalation engine)
```
transfer:  आदमी | इंसान | मैनेजर | manager | साहब | किसी से बात | human
complaint: शिकायत | ख़राब | नक़ली | काम नहीं किया | नुक़सान | घाटा
emergency: ज़हर | पी लिया | खा लिया | साँस | बेहोश | उल्टी | चक्कर | आँख में | अस्पताल
optout:    मत करो कॉल | परेशान | नंबर हटाओ | दोबारा मत | बंद करो
```

**The `emergency` list is safety-critical.** It must be reviewed by a human, translated into every
supported language, and extended with regional phrasings. It is backed up by LLM classification, but
the keyword path exists so that detection does not depend on a model call succeeding.

---

## 11. WHAT THIS DOCUMENT DOES NOT CONTAIN

Stated plainly so nobody assumes coverage that is not here:

1. **Actual product SKUs, prices and stock.** UA Agro's public site lists categories and partner
   brands, not a priced catalogue. These must be imported from the company's own inventory system.
2. **Actual dosages.** No dose in this document is a real UA Agro recommendation. Every one is a
   placeholder. Serving an unapproved dose is the single highest-severity failure this system can
   produce.
3. **The real centre list with addresses and manager contacts.**
4. **District-specific bigha conversion factors.**
5. **UA Agro's credit, delivery, warranty and return policies.**
6. **Current offers.**

Before go-live, the admin panel's "unapproved content" counter must read **zero** for anything the
agent is permitted to speak, and the four highest-volume crops must have complete, agronomist-signed
recommendation coverage. Until then, restrict the agent's advisory scope to availability, price,
composition, centre information and service booking, and route every dosage question to a human.
