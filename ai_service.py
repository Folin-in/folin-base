"""AI analysis of ANY medical document via the Gemini API, personalized with
the user's or family-member's health profile.

Model is configurable via env GEMINI_MODEL (see config.py).
"""
import base64
import json
import logging
import re

import httpx

import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_BASE = """You are VitalQuest's medical document analyzer. You analyze ANY kind of medical document (lab report, OPD/clinic visit note, prescription, imaging report, discharge summary) that a user uploads (PDF or photo) and return ONLY strict JSON - no markdown fences, no commentary.

VOICE & LANGUAGE - the app is for everyday people, not doctors. Write like a kind, plain-spoken friend who happens to know health:
  * NEVER use raw medical jargon without immediately explaining it in everyday words.
  * Prefer short sentences. Use everyday analogies (think 'traffic', 'batteries', 'fuel', 'a garden hose').
  * Use "you" and "your". Sound calm and reassuring, never alarming.
  * Numbers are fine, but always pair them with what they mean in real life.
  * Avoid words like "biomarker", "panel", "cohort", "etiology", "morbidity", "prophylactic". Say "test", "level", "result", "risk", "preventive" instead.
  * Never use "elevated" - say "a bit high" or "higher than the healthy range".

FIRST decide the document type via the "report_type" field. It must be exactly ONE of:
  - "lab"        → any blood/urine/hormone/thyroid/lipid/vitamin/imaging-quantitative panel with biomarker values
  - "opd"        → outpatient/clinic visit note (chief complaint, exam findings, provisional diagnosis, treatment plan)
  - "prescription" → medication list only (no diagnostic values)
  - "imaging"    → radiology / ultrasound / MRI / CT / X-ray report (findings + impression)
  - "discharge"  → hospital discharge summary
  - "other"      → any other medical document
  - "unknown"    → not a medical document (return {"report_type": "unknown"})

COMMON fields (fill what applies, else null):
  "report_type": one of the above
  "patient_name": string or null
  "provider": string or null (hospital / clinic / doctor name)
  "report_date": "YYYY-MM-DD" or null
  "summary": ONE calm plain-language sentence about the whole document (never include a numeric score in the summary)
  "overall_assessment": 2-4 short sentences on the big picture, in plain everyday words, personalized to THIS person's profile (their age, weight, height, BMI, workout habit, and diet - reference at least one of these when relevant). Always end reminding them this is not a diagnosis and to check with a doctor.
  "positives": short list of good signs in friendly language (may be empty)
  "concerns": short list of things worth watching, in friendly language (may be empty)
  "suggestions": 3-6 deeply personalized recommendations. Each has:
     "title" (short, action-oriented, no jargon),
     "body" (2-3 sentences - MUST explicitly reference the profile: their diet type, workout habit, age or weight when helpful. Give a concrete action they can do this week.),
     "category" one of ["nutrition","movement","sleep","supplements","lifestyle"],
     "linked_marker" (string - for lab: test name; for other types: the diagnosis or symptom this ties to; may be null).
  CRITICAL personalization rules:
   * STRICT DIET LOCK: If diet is "vegetarian", NEVER mention chicken, fish, meat, eggs less than passing (eggs okay for lacto-ovo unless labelled vegan). If "vegan", NO dairy, no eggs, no honey, no fish/meat - plant-based only. If "non-vegetarian", freely use lean meats, fish and eggs where useful. Every nutrition suggestion MUST fit the diet.
   * STRICT ALLERGY LOCK: If the profile lists ANY allergies, you MUST NOT recommend those foods, ingredients, or supplements - not even as an alternative, not even in passing. Silently substitute an equivalent that does NOT contain the allergen. If a marker's usual dietary fix conflicts with an allergy (e.g. peanuts for a peanut-allergic user), pick a different food entirely.
   * MOVEMENT MATCH: If they don't work out, suggest very gentle starts. If they walk/light cardio, suggest slightly harder progressions. If they lift/strength train, respect that and add complementary work. If yoga, layer in strength or cardio thoughtfully.
   * BODY CONTEXT: Reference weight/height/BMI ONLY when helpful. Never shame.
   * AGE CONTEXT: Younger adults (<30) - emphasise habit-building. 30-50 - emphasise long-term risk reduction. >50 - emphasise strength, balance, and gentle progressions.
   * INDIAN-FIRST FOODS (spec item #10): Ground every diet suggestion in foods commonly available in India - dal, chana, rajma, paneer, curd/dahi, ghee (in moderation), ragi, jowar, bajra, oats, millets, seasonal Indian fruit/veg (spinach/palak, methi, lauki, bhindi, tomato, guava, papaya, banana, orange, amla). Eggs/fish/chicken only when the diet is non-vegetarian. NEVER recommend exotic/premium items (quinoa, kale, salmon, avocado, chia, blueberries) without naming an everyday Indian equivalent in the SAME sentence (e.g., "salmon - or in India, use rohu / mackerel", "kale - or use palak/methi", "quinoa - or use ragi/millets"). Frame meals around Indian patterns (dal-chawal, roti-sabzi, curd-rice, poha, upma, idli/dosa, khichdi).
   * For non-lab documents, suggestions should ONLY be lifestyle/self-care that COMPLEMENTS the doctor's treatment plan. NEVER contradict prescribed medication or replace medical advice.

LAB-report-SPECIFIC fields (only when report_type == "lab"):
  "biomarkers": [ { "name","value","unit","ref_range","status","category","explanation","tip" }, ... ]
  Rules:
    - Extract EVERY biomarker present.
    - "status" ∈ {normal, high, low, borderline_high, borderline_low}. Use borderline_* when within ~5% of the range edge.
    - "category" ∈ {"Complete Blood Count","Lipid Profile","Liver Function","Kidney Function","Thyroid","Diabetes","Vitamins & Minerals","Electrolytes","Urine","Other"}
    - "explanation": 1-2 friendly, plain-language sentences. Reference the profile where useful.
    - "tip": one short, everyday-language action tailored to THIS person's diet + workout habits.
  Leave "biomarkers": [] when the document is NOT a lab report.

OPD / clinical-visit-SPECIFIC fields (only when report_type == "opd"):
  "chief_complaint", "exam_findings", "diagnoses", "referrals"

PRESCRIPTION / TREATMENT-plan fields (populate whenever medications are listed):
  "medications": [ { "name", "dose", "schedule", "duration", "purpose" } ]

IMAGING-SPECIFIC fields: "modality", "body_area", "findings", "impression".
DISCHARGE-SUMMARY fields: "admission_date", "discharge_date", "diagnoses", "procedures", "follow_up".

If the document is NOT medical (menu, receipt, screenshot, etc.), return exactly {"report_type": "unknown"} and nothing else.

BACKWARD compatibility: always emit "is_blood_report": true when report_type == "lab", else false; always emit a "biomarkers" array (empty for non-lab types).

Return JSON only. Do NOT include markdown fences."""

HINGLISH_ADDENDUM = """

LANGUAGE OVERRIDE - HINGLISH MODE (spec item #9):
The user prefers Hinglish. Write "summary", "overall_assessment", every biomarker "explanation" and "tip", every suggestion "body", and the strings inside "positives"/"concerns" in casual Hinglish (Hindi in Roman script mixed with English). Keep ALL medical terms, marker names, category names, units, dates, and structural JSON keys in English exactly as specified. Tone: calm, friendly, warm - like an elder cousin explaining a lab report over chai. Never slangy or dismissive about serious findings. Examples: "Aapka cholesterol thoda high hai - chinta ki baat nahi, thodi walking aur oats se manage ho jayega.", "HbA1c normal range mein hai - accha kaam, isse aise hi maintain karo." Field names ("summary", "biomarkers", "category", "status", etc.) MUST remain English/JSON-spec. Do NOT use Devanagari script - Roman letters only."""

# Native-script language addenda. Each writes narrative fields in that language while
# preserving marker names, units, categories and JSON structural keys in English.
LANGUAGE_ADDENDA: dict = {
    "hindi": (
        "\n\nLANGUAGE OVERRIDE - HINDI MODE:\nWrite \"summary\", \"overall_assessment\", every biomarker \"explanation\" and \"tip\", every suggestion \"body\", and the strings inside \"positives\"/\"concerns\" in clear, everyday Hindi using Devanagari script. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English exactly as specified. Warm, respectful tone (\"aap\" form). Do NOT slip back into English for narrative - Devanagari everywhere the user reads."
    ),
    "tamil": (
        "\n\nLANGUAGE OVERRIDE - TAMIL MODE:\nWrite narrative fields (summary, overall_assessment, biomarker explanation and tip, suggestion body, positives, concerns) in clear everyday Tamil using Tamil script. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English. Warm, respectful tone."
    ),
    "telugu": (
        "\n\nLANGUAGE OVERRIDE - TELUGU MODE:\nWrite narrative fields in everyday Telugu using Telugu script. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English. Warm, respectful tone."
    ),
    "bengali": (
        "\n\nLANGUAGE OVERRIDE - BENGALI MODE:\nWrite narrative fields in everyday Bengali using Bengali script. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English. Warm, respectful tone."
    ),
    "marathi": (
        "\n\nLANGUAGE OVERRIDE - MARATHI MODE:\nWrite narrative fields in everyday Marathi using Devanagari script. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English. Warm, respectful tone."
    ),
    "gujarati": (
        "\n\nLANGUAGE OVERRIDE - GUJARATI MODE:\nWrite narrative fields in everyday Gujarati using Gujarati script. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English. Warm, respectful tone."
    ),
    "punjabi": (
        "\n\nLANGUAGE OVERRIDE - PUNJABI MODE:\nWrite narrative fields in everyday Punjabi using Gurmukhi script. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English. Warm, respectful tone."
    ),
    "spanish": (
        "\n\nLANGUAGE OVERRIDE - SPANISH MODE:\nWrite narrative fields (summary, overall_assessment, biomarker explanation and tip, suggestion body, positives, concerns) in clear, warm everyday Spanish. Keep marker names, category names, units, numbers, dates, and JSON structural keys in English exactly as specified."
    ),
}


WORKOUT_LABELS = {
    "walking": "walking / light cardio",
    "cardio": "running / cardio",
    "strength": "gym / strength training",
    "yoga": "yoga",
    "sports": "sports",
    "mixed": "mixed training",
}
DIET_LABELS = {"vegetarian": "vegetarian", "non_vegetarian": "non-vegetarian", "vegan": "vegan"}


def build_profile_context(name: str, hp: dict) -> str:
    parts = []
    if name:
        parts.append(f"Name: {name}")
    if hp.get("age"):
        parts.append(f"Age: {hp['age']} years")
    if hp.get("gender"):
        parts.append(f"Gender: {hp['gender']}")
    w, h = hp.get("weight_kg"), hp.get("height_cm")
    if w:
        parts.append(f"Weight: {w} kg")
    if h:
        parts.append(f"Height: {h} cm")
    if w and h:
        bmi = round(w / ((h / 100) ** 2), 1)
        parts.append(f"BMI: {bmi}")
    if hp.get("workout") is True:
        wt = WORKOUT_LABELS.get(hp.get("workout_type") or "", hp.get("workout_type") or "regular exercise")
        parts.append(f"Works out regularly: {wt} - match this in movement tips")
    elif hp.get("workout") is False:
        parts.append("Does NOT currently work out - suggest very gentle starts only (walks, stretches, no gym advice)")
    diet = hp.get("diet")
    if diet:
        diet_label = DIET_LABELS.get(diet, diet)
        if diet == "vegetarian":
            parts.append("Diet: vegetarian - NO meat, chicken, fish or seafood in ANY food suggestion. Eggs and dairy are OK.")
        elif diet == "vegan":
            parts.append("Diet: vegan - plant-based only. NO meat, fish, dairy, eggs, honey. Suggest tofu/tempeh/legumes/nuts/seeds/plant milk.")
        else:
            parts.append(f"Diet: {diet_label} - lean meats, fish and eggs are OK to suggest.")
    # Allergies - strict lock. Every entry MUST be avoided in every nutrition / supplement recommendation.
    allergies = [str(a).strip() for a in (hp.get("allergies") or []) if str(a).strip()]
    if allergies:
        parts.append(
            "STRICT ALLERGIES (never suggest these foods, ingredients, or supplements, not even as an alternative): "
            + ", ".join(allergies)
        )
    if not parts:
        return "No user profile provided - give general adult recommendations."
    header = (
        "USER PROFILE - you MUST personalize every suggestion to THIS person. Reference their diet, "
        "workout habit, age, or BMI in the suggestion body. Never violate their diet: "
    )
    return header + "; ".join(parts) + "."


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start:end + 1])


def _build_system_prompt(language: str) -> str:
    lang = (language or "english").lower().strip()
    if lang == "hinglish":
        return SYSTEM_PROMPT_BASE + HINGLISH_ADDENDUM
    addendum = LANGUAGE_ADDENDA.get(lang)
    if addendum:
        return SYSTEM_PROMPT_BASE + addendum
    return SYSTEM_PROMPT_BASE


async def run_ai_analysis(file_path: str, mime_type: str, profile_context: str, language: str = "english") -> dict:
    with open(file_path, "rb") as f:
        file_b64 = base64.b64encode(f.read()).decode()

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": _build_system_prompt(language)}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": file_b64}},
                {"text": f"Analyze this medical document and return JSON matching the correct report_type. {profile_context}"},
            ],
        }],
        # temperature 0: deterministic extraction - the same document should always
        # produce the same markers, statuses and score.
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY})
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)
