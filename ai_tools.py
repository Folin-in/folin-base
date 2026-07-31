"""Helper tools for the AI agent: profile-context building and response parsing.
No prompt text and no API calls here - see ai_prompts.py and ai_agent.py."""
import json
import re


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


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start:end + 1])
