"""Läser styrketräningens set (övning, vikt, reps) ur en Garmin-FIT-fil.

Bakgrund: Intervals.icu:s API har inga styrkefält alls — deras OpenAPI-spec
innehåller inga properties som heter reps, weight, exercise eller sets.
Däremot exponerar de originalfilen från klockan via
GET /api/v1/activity/{id}/file, och en Garmin-FIT från ett styrkepass
innehåller set-meddelanden (global msg 225) med precis den datan:

    {'start_time': ..., 'duration': 29.541, 'repetitions': 5,
     'weight': 120.0, 'category': ['deadlift', 'deadlift', 'deadlift'],
     'category_subtype': [0, 0, 0], 'weight_display_unit': 'kilogram',
     'set_type': 'active', ...}

Två saker att hålla reda på i formatet:

1. `set_type` skiljer arbetsset ('active') från vilopauser ('rest').
   Vilopauserna har varken reps eller vikt och ska inte sparas som set.
2. `category` och `category_subtype` är ARRAY-fält (FIT tillåter flera
   övningar per set, t.ex. supersets) och kommer som listor med upprepade
   värden — ['deadlift', 'deadlift', 'deadlift'].
3. `category` är grov (53 värden). Det är `category_subtype` som säger
   vilken övning det faktiskt var, via en egen enum per kategori:
   deadlift/0 är konventionellt marklyft, deadlift/1 är raka marklyft.
   Läses bara kategorin blir 135 kg marklyft och 40 kg raka marklyft
   samma rad — se _EXERCISE_VARIANT_NAMES_SV.

Övningsnamnet översätts till svenska eftersom det som loggas via chatten
skrivs på svenska ("marklyft"). Utan översättning hade samma övning
hamnat under två olika namn beroende på källa, och då går det inte att
följa progression över tid — vilket är hela poängen med att spara datan.
"""
from __future__ import annotations

import logging
from functools import cache
from typing import Any, cast

log = logging.getLogger(__name__)

# Vikter utanför det här intervallet är inte riktiga lyft utan FIT:s
# "ogiltigt värde"-sentinels som slunkit igenom avkodningen.
_MIN_PLAUSIBLE_WEIGHT_KG = 0.5
_MAX_PLAUSIBLE_WEIGHT_KG = 500.0

_POUNDS_TO_KG = 0.45359237

# Aktivitetstyper från Intervals som räknas som styrketräning. Intervals
# använder "WeightTraining" för gympass; "Strength" täcker in varianter.
_STRENGTH_TYPES = ("weighttraining", "strength")


def is_strength_activity(activity: dict[str, Any]) -> bool:
    """Är det här passet styrketräning (och alltså värt en FIT-import)?

    Bor här, i modulen som äger styrkedatan, eftersom frågan i praktiken
    är "kan det finnas set-meddelanden att hämta för det här passet?".
    Samma funktion låg tidigare kopierad i både sync/intervals_client.py
    (för att avgöra om originalfilen ska laddas ner) och
    analysis/pipeline.py (för att koppla chattloggade set till dagens
    pass). Två kopior av samma regel betyder att en ny aktivitetstyp från
    Intervals måste läggas till på båda ställena — glöms det ena bort
    glider de tyst isär, och symptomet blir att styrkedata importeras men
    aldrig kopplas till passet (eller tvärtom).
    """
    haystack = f"{activity.get('type') or ''} {activity.get('sport') or ''}".lower()
    return any(t in haystack for t in _STRENGTH_TYPES)

# FIT:s exercise_category-enum (53 värden) översatt till svenska. Namnen är
# valda för att matcha vad man faktiskt skriver i chatten — "marklyft",
# inte "marklyftning" — så att FIT-importerade och chattloggade pass slås
# ihop till samma övning.
_EXERCISE_NAMES_SV = {
    "bench_press": "bänkpress",
    "calf_raise": "tåhävning",
    "cardio": "kondition",
    "carry": "bärövning",
    "chop": "vedhuggare",
    "core": "bålträning",
    "crunch": "crunch",
    "curl": "bicepscurl",
    "deadlift": "marklyft",
    "flye": "flyes",
    "hip_raise": "höftlyft",
    "hip_stability": "höftstabilitet",
    "hip_swing": "höftsving",
    "hyperextension": "ryggresning",
    "lateral_raise": "sidolyft",
    "leg_curl": "bencurl",
    "leg_raise": "benlyft",
    "lunge": "utfall",
    "olympic_lift": "olympiskt lyft",
    "plank": "planka",
    "plyo": "plyometrisk övning",
    "pull_up": "chins",
    "push_up": "armhävning",
    "row": "rodd",
    "shoulder_press": "axelpress",
    "shoulder_stability": "axelstabilitet",
    "shrug": "shrugs",
    "sit_up": "situps",
    "squat": "knäböj",
    "total_body": "helkroppsövning",
    "triceps_extension": "tricepsextension",
    "warm_up": "uppvärmning",
    "run": "löpning",
    "bike": "cykling",
    "cardio_sensors": "kondition",
    "move": "rörlighet",
    "pose": "position",
    "banded_exercises": "gummibandsövning",
    "battle_rope": "battle rope",
    "elliptical": "crosstrainer",
    "floor_climb": "floor climber",
    "indoor_bike": "inomhuscykel",
    "indoor_row": "roddmaskin",
    "ladder": "koordinationsstege",
    "sandbag": "sandsäck",
    "sled": "släde",
    "sledge_hammer": "slägga",
    "stair_stepper": "trappmaskin",
    "suspension": "suspensionsträning",
    "tire": "däck",
    "run_indoor": "löpning inomhus",
    "bike_outdoor": "cykling utomhus",
    "unknown": "okänd övning",
}


def _first_value(value: Any) -> Any:
    """Plockar ut ett enskilt värde ur FIT:s array-fält.

    category/category_subtype kommer som listor med upprepade värden
    (['deadlift', 'deadlift', 'deadlift']) eftersom FIT tillåter flera
    övningar per set. Vi tar det första — supersets med olika övningar i
    samma set är sällsynt nog att inte vara värt en egen datamodell.
    """
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


# FIT:s category_subtype pekar ut VILKEN övning inom kategorin som gjordes,
# via en egen enum per kategori (deadlift_exercise_name, squat_exercise_name
# och så vidare). Utan den blir olika lyft samma rad: 135 kg konventionellt
# marklyft och 40 kg raka marklyft hamnade båda under "marklyft", och i
# knäböjen blandades vanlig knäböj (188 set, 40-115 kg) med frontböj (88 set,
# 40-70 kg). Progressionen gick då inte att läsa — serien såg ut att svänga
# vilt mellan pass när det i själva verket var olika övningar.
#
# Nycklarna är FIT-namnen, inte enum-numren: numren betyder olika saker i
# olika kategorier (subtype 1 = barbell_straight_leg_deadlift under deadlift
# men barbell_bench_press under bench_press), och namnen går att läsa.
#
# Att översätta till svenska slår dessutom ihop Garmins nästan-dubbletter:
# barbell_row och bent_over_row_with_barbell är samma lyft med två enum-
# värden, och blir en enda "skivstångsrodd" att följa över tid.
_EXERCISE_VARIANT_NAMES_SV = {
    # Förekommer i faktiska filer (inventerat över 98 gympass).
    "barbell_bench_press": "bänkpress",
    "close_grip_barbell_bench_press": "smalbänk",
    "jump_rope": "hopprep",
    "barbell_biceps_curl": "bicepscurl",
    "dumbbell_biceps_curl": "hantelcurl",
    "barbell_deadlift": "marklyft",
    "barbell_straight_leg_deadlift": "raka marklyft",
    "straight_leg_deadlift": "raka marklyft",
    "dumbbell_deadlift": "marklyft med hantlar",
    "kettlebell_swing": "kettlebellsving",
    "suspended_row": "hängande rodd",
    "suspended_inverted_row": "hängande rodd",
    "barbell_reverse_lunge": "omvänt utfall",
    "clean": "frivändning",
    "single_arm_kettlebell_snatch": "enarmsryck med kettlebell",
    "medicine_ball_slam": "medicinbollsslam",
    "push_up": "armhävning",
    "bent_over_row_with_barbell": "skivstångsrodd",
    "barbell_row": "skivstångsrodd",
    "one_arm_bent_over_row": "enarmsrodd",
    "barbell_push_press": "push press",
    "overhead_barbell_press": "axelpress",
    "single_arm_dumbbell_shoulder_press": "enarmspress med hantel",
    "push": "slädpush",
    # Klockan döper atletens knäböj till barbell_hack_squat. Det är
    # Garmins etikett, inte en annan övning — samma lyft, samma stång.
    # Översätts därför till "knäböj", precis som barbell_back_squat
    # nedan, så att serien blir EN att följa över tid. Gamla rader
    # döps om av migreringen i Store.init().
    "barbell_hack_squat": "knäböj",
    "barbell_front_squat": "frontböj",
    "goblet_squat": "goblet squat",
    "weighted_squat": "knäböj med vikt",
    "flip": "däckvältning",
    # Vanliga grannar som ännu inte dykt upp i någon fil, men som ligger nära
    # det som redan tränas. Utan dem skulle en ny övning visas på engelska.
    "barbell_back_squat": "knäböj",
    "back_squat": "knäböj",
    "overhead_squat": "överhuvudknäböj",
    "romanian_deadlift": "rumänsk marklyft",
    "sumo_deadlift": "sumomarklyft",
    "trap_bar_deadlift": "marklyft med trap bar",
    "rack_pull": "rack pull",
    "incline_barbell_bench_press": "lutande bänkpress",
    "dumbbell_bench_press": "hantelpress",
    "pull_up": "chins",
    "chin_up": "chins",
    "snatch": "ryck",
    "barbell_hip_thrust_on_floor": "höftlyft",
    "barbell_hip_thrust_with_bench": "höftlyft med bänk",
    "walking_lunge": "gående utfall",
    "front_raise": "framåtlyft",
}


@cache
def _subtype_names(category: str) -> dict[str, str]:
    """FIT:s subtyp-enum för en kategori, som {"1": "barbell_deadlift", ...}.

    Enumen ligger i SDK:ns profil (Profile["types"]) under namnet
    "<kategori>_exercise_name". Att läsa den därifrån istället för att
    hårdkoda numren betyder att en övning vi inte översatt ändå får ett
    läsbart namn ur filen, i stället för att kollapsa in i kategorin.

    Importen är lat (SDK:n dras bara in när en FIT-fil faktiskt tolkas) och
    resultatet cachas per kategori — funktionen anropas en gång per set.
    """
    from garmin_fit_sdk import Profile

    enum = Profile["types"].get(f"{category}_exercise_name") or {}
    return {str(key): str(value) for key, value in enum.items()}


def _exercise_name(category: Any, category_subtype: Any = None) -> str:
    """Översätter FIT:s kategori + subtyp till ett svenskt övningsnamn."""
    raw = _first_value(category)
    if raw is None:
        return _EXERCISE_NAMES_SV["unknown"]
    # Okända kategorier avkodas av SDK:n som råa heltal.
    key = str(raw).lower()

    subtype = _first_value(category_subtype)
    if subtype is not None:
        fit_name = _subtype_names(key).get(str(subtype))
        if fit_name:
            translated = _EXERCISE_VARIANT_NAMES_SV.get(fit_name)
            if translated:
                return translated
            log.info(
                "Oöversatt övningsvariant från FIT: %s/%s (%s).", key, subtype, fit_name
            )
            return fit_name.replace("_", " ")

    # Ingen subtyp i filen (eller en vi inte känner igen): kategorin är det
    # bästa vi vet. Det gäller t.ex. 13 marklyftsset på 90-130 kg där
    # klockan inte skrev någon subtyp alls.
    if key in _EXERCISE_NAMES_SV:
        return _EXERCISE_NAMES_SV[key]
    log.info("Okänd övningskategori från FIT: %r — sparar som den är.", raw)
    return key.replace("_", " ")


def _weight_kg(raw_weight: Any, display_unit: Any) -> float | None:
    """Normaliserar vikten till kilo, eller None för kroppsviktsövningar."""
    if raw_weight is None:
        return None
    try:
        weight = float(raw_weight)
    except (TypeError, ValueError):
        return None

    # Garmin lagrar alltid vikten i kilo i själva fältet; display_unit
    # säger bara hur klockan visade den. Ett pund-värde här vore alltså
    # oväntat, men konverteringen kostar inget och gör tolkningen entydig.
    if str(display_unit).lower() == "pound":
        weight *= _POUNDS_TO_KG

    if not (_MIN_PLAUSIBLE_WEIGHT_KG <= weight <= _MAX_PLAUSIBLE_WEIGHT_KG):
        return None
    return round(weight, 2)


def parse_set_messages(set_mesgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Omvandlar avkodade set-meddelanden till våra set-rader.

    Tar avkodade meddelanden (inte råa bytes) så att den går att testa
    utan en riktig FIT-fil. Returnerar en lista grupperad per övning:

        [{"exercise": "marklyft",
          "sets": [{"reps": 5, "weight_kg": 120.0}, ...]}, ...]

    Övningarna kommer i den ordning de först utfördes, och setsen inom
    varje övning i kronologisk ordning.
    """
    # Sortera kronologiskt. message_index är den pålitliga ordningen inom
    # filen; start_time kan saknas på enstaka meddelanden.
    def _order_key(mesg: dict[str, Any]) -> int:
        index = mesg.get("message_index")
        try:
            return int(index)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    ordered = sorted(set_mesgs, key=_order_key)

    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for mesg in ordered:
        # Vilopauser mellan set är egna meddelanden utan reps eller vikt.
        if str(mesg.get("set_type", "")).lower() != "active":
            continue

        reps = mesg.get("repetitions")
        weight = _weight_kg(mesg.get("weight"), mesg.get("weight_display_unit"))
        if reps is None and weight is None:
            # Varken reps eller vikt — inget att spara.
            continue

        exercise = _exercise_name(
            mesg.get("category"), mesg.get("category_subtype")
        )
        if exercise not in grouped:
            grouped[exercise] = []
            order.append(exercise)

        entry: dict[str, Any] = {}
        if reps is not None:
            entry["reps"] = int(reps)
        if weight is not None:
            entry["weight_kg"] = weight
        grouped[exercise].append(entry)

    return [{"exercise": name, "sets": grouped[name]} for name in order]


def parse_strength_sets(fit_bytes: bytes) -> list[dict[str, Any]]:
    """Avkodar en FIT-fil och returnerar dess styrkeset per övning.

    Returnerar en tom lista om filen inte är en FIT-fil eller saknar
    set-meddelanden (t.ex. ett pass som laddats upp som TCX/GPX).
    """
    # En FIT-fil har den bokstavliga strängen '.FIT' på position 8-12.
    if len(fit_bytes) < 12 or fit_bytes[8:12] != b".FIT":
        log.info("Filen är inte en FIT-fil — hoppar över styrkeimport.")
        return []

    from garmin_fit_sdk import Decoder, Stream

    messages, errors = Decoder(Stream.from_byte_array(bytearray(fit_bytes))).read()
    if errors:
        log.warning("FIT-avkodning gav %d varningar (fortsätter ändå).", len(errors))

    # messages typas som en TypedDict med ett fält per meddelandetyp;
    # set_mesgs-raderna är i praktiken vanliga dictar.
    set_mesgs = cast("list[dict[str, Any]]", messages.get("set_mesgs") or [])
    if not set_mesgs:
        return []
    return parse_set_messages(set_mesgs)
