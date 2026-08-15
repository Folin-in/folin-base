"""Turns a user's health profile into the personalisation sentence every advice-writing
agent is given. The wording is load-bearing: the diet and allergy lines are what stop the
model suggesting meat to a vegetarian or peanuts to someone allergic to them."""
from app.constants import DIET_LABELS, WORKOUT_LABELS
from agents.tools.base import Tool

_NO_PROFILE = "No user profile provided - give general adult recommendations."
_HEADER = (
    "USER PROFILE - you MUST personalize every suggestion to THIS person. Reference their diet, "
    "workout habit, age, or BMI in the suggestion body. Never violate their diet: "
)
_DIET_LINES = {
    "vegetarian": "Diet: vegetarian - NO meat, chicken, fish or seafood in ANY food suggestion. Eggs and dairy are OK.",
    "vegan": "Diet: vegan - plant-based only. NO meat, fish, dairy, eggs, honey. Suggest tofu/tempeh/legumes/nuts/seeds/plant milk.",
}
_ALLERGY_PREFIX = "STRICT ALLERGIES (never suggest these foods, ingredients, or supplements, not even as an alternative): "


class ProfileContextTool(Tool):
    name = "profile_context"

    def run(self, name: str, hp: dict) -> str:
        parts: list[str] = []
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
            parts.append(f"BMI: {round(w / ((h / 100) ** 2), 1)}")
        if hp.get("workout") is True:
            wt = WORKOUT_LABELS.get(hp.get("workout_type") or "", hp.get("workout_type") or "regular exercise")
            parts.append(f"Works out regularly: {wt} - match this in movement tips")
        elif hp.get("workout") is False:
            parts.append("Does NOT currently work out - suggest very gentle starts only (walks, stretches, no gym advice)")
        diet = hp.get("diet")
        if diet:
            parts.append(_DIET_LINES.get(diet, f"Diet: {DIET_LABELS.get(diet, diet)} - lean meats, fish and eggs are OK to suggest."))
        allergies = [str(a).strip() for a in (hp.get("allergies") or []) if str(a).strip()]
        if allergies:
            parts.append(_ALLERGY_PREFIX + ", ".join(allergies))
        if not parts:
            return _NO_PROFILE
        return _HEADER + "; ".join(parts) + "."
