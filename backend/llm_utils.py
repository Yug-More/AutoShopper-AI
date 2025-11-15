# backend/llm_utils.py
import os
import json
from typing import Dict, List, Any

from openai import OpenAI

from dotenv import load_dotenv 

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 1) Prompt -> constraints
def parse_prompt_with_llm(prompt: str,
                          allergies: List[str] | None = None,
                          dietary_rules: List[str] | None = None) -> Dict[str, Any]:
    """
    Use an LLM to extract structured ordering constraints from the user prompt.
    """
    allergies = allergies or []
    rules = dietary_rules or []

    system_msg = (
        "You extract structured food-order constraints from a user prompt.\n"
        "Always assume the user has allergies and dietary rules that must be respected.\n"
        "Never suggest anything likely to contain those allergens.\n"
        "Return ONLY a JSON object with keys:\n"
        "  cuisine (string or null),\n"
        "  max_price (number or null, in USD),\n"
        "  max_distance_km (number or null),\n"
        "  spice_level (string or null),\n"
        "  dietary (array of strings).\n"
        f"User allergies: {allergies}\n"
        f"User dietary rules: {rules}\n"
    )


    resp = client.chat.completions.create(
        model="gpt-4.1-mini",  # or another cheap model you have
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
    )

    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {
            "cuisine": None,
            "max_price": None,
            "max_distance_km": 5,
            "spice_level": None,
            "dietary": [],
        }
    return data


# 2) Choose best place + item from Places results
def select_place_and_item(
    prompt: str,
    places: List[Dict[str, Any]],
    allergies: List[str] | None = None,
    dietary_rules: List[str] | None = None,
) -> Dict[str, Any]:
    """
    Given the user prompt and a list of Google Places results,
    ask the LLM to choose the safest/best restaurant and a specific item.
    The LLM must:
      - Respect allergies and dietary rules.
      - Treat component ingredients as allergens (e.g. dairy includes milk, cheese, cream).
      - If it cannot find ANY safe item at ANY restaurant, it should signal
        no_safe_option = true and place_index = -1.
    """
    allergies = allergies or []
    rules = dietary_rules or []

    condensed = []
    for i, p in enumerate(places):
        condensed.append({
            "index": i,
            "name": p.get("name"),
            "rating": p.get("rating"),
            "user_ratings_total": p.get("user_ratings_total"),
            "price_level": p.get("price_level"),
            "types": p.get("types"),
            "address": p.get("formatted_address"),
        })

    system_msg = (
        "You are an AI food-ordering assistant.\n"
        "You receive:\n"
        "- A natural-language request for food or drink.\n"
        "- A list of candidate restaurants from Google Places.\n"
        "- A list of user allergies and dietary rules.\n\n"
        "You MUST be extremely strict about allergies and their component ingredients:\n"
        "- If the user is allergic to DAIRY, treat milk, cheese, butter, cream, yogurt, ice cream, "
        "  whey, etc. as unsafe.\n"
        "- If the user is allergic to PEANUTS, treat peanut butter, peanut oil, satay, etc. as unsafe.\n"
        "- If the user is allergic to TREE NUTS, treat almond, cashew, pistachio, walnut, pecan, "
        "  hazelnut, macadamia, etc. as unsafe.\n"
        "- If the user is allergic to SHELLFISH, treat shrimp, prawns, crab, lobster, scallops, oysters, clams, mussels as unsafe.\n"
        "- If the user is allergic to GLUTEN, treat wheat, barley, rye, regular bread, regular pasta, "
        "  normal pizza crust, etc. as unsafe.\n"
        "Always assume there may be hidden cross-contamination and err on the side of caution.\n\n"
        "If the user's prompt explicitly asks for an item made from an ingredient they are allergic to "
        "(e.g. 'peanut butter shake' but allergy includes 'peanut'), you MUST NOT invent a safe version.\n"
        "In that case, you should say there is no safe option.\n\n"
        "You must choose ONE restaurant and ONE reasonably realistic menu item.\n"
        "If you cannot find any restaurant + item that seems safe, you MUST respond with:\n"
        "  place_index = -1,\n"
        "  item_name = null,\n"
        "  estimated_total_price = 0,\n"
        "  no_safe_option = true.\n\n"
        "Otherwise, respond with a safe choice.\n\n"
        "Respond ONLY with a JSON object with keys:\n"
        "  place_index (integer),\n"
        "  item_name (string or null),\n"
        "  estimated_total_price (number),\n"
        "  no_safe_option (boolean, optional; default false)."
    )

    user_msg = (
        "User prompt:\n"
        f"{prompt}\n\n"
        f"User allergies: {allergies}\n"
        f"User dietary rules: {rules}\n\n"
        "Candidate restaurants (JSON list):\n"
        f"{json.dumps(condensed, ensure_ascii=False)}"
    )

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
    )

    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {
            "place_index": -1,
            "item_name": None,
            "estimated_total_price": 0.0,
            "no_safe_option": True,
        }
    return data


def select_batch_order(
    prompt: str,
    places: List[Dict[str, Any]],
    allergies: List[str] | None = None,
    dietary_rules: List[str] | None = None,
    max_restaurants: int = 3,
) -> Dict[str, Any]:
    """
    Given the user prompt and a list of Google Places results,
    choose up to max_restaurants different restaurants and suggest
    one item from each.

    Returns JSON like:
    {
      "selections": [
        {"place_index": 0, "item_name": "...", "estimated_total_price": 45.0},
        ...
      ]
    }
    """
    allergies = allergies or []
    rules = dietary_rules or []

    condensed = []
    for i, p in enumerate(places):
        condensed.append({
            "index": i,
            "name": p.get("name"),
            "rating": p.get("rating"),
            "user_ratings_total": p.get("user_ratings_total"),
            "price_level": p.get("price_level"),
            "types": p.get("types"),
            "address": p.get("formatted_address"),
        })

    system_msg = (
        "You are an AI catering and group-order assistant.\n"
        "The user describes what they want for a group or event.\n"
        "You are given a JSON list of candidate restaurants from Google Places.\n\n"
        "Your job is to plan a BATCH order by:\n"
        "- Choosing up to N different restaurants (N is given as max_restaurants).\n"
        "- For EACH chosen restaurant, suggest ONE primary item that the organizer "
        "  could order in bulk (trays, party platters, pizzas, etc.).\n"
        "- Respect all user allergies and dietary rules.\n"
        "- Try to provide variety across restaurants if possible (e.g. tacos + pizza + salad bar).\n\n"
        "Respond ONLY with a JSON object with key:\n"
        "  selections (array of objects), where each object has:\n"
        "    place_index (integer, index into the restaurant list),\n"
        "    item_name (string; describe the bulk order item),\n"
        "    estimated_total_price (number, rough estimate for a group-sized order).\n"
    )

    user_msg = (
        "User prompt (group / batch needs):\n"
        f"{prompt}\n\n"
        f"User allergies: {allergies}\n"
        f"User dietary rules: {rules}\n\n"
        f"max_restaurants: {max_restaurants}\n\n"
        "Candidate restaurants (JSON list):\n"
        f"{json.dumps(condensed, ensure_ascii=False)}"
    )

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
    )

    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: just take the first few places with generic items
        fallback_selections = []
        for i, p in enumerate(places[:max_restaurants]):
            fallback_selections.append({
                "place_index": i,
                "item_name": f"Recommended group item from {p.get('name','this restaurant')}",
                "estimated_total_price": 0.0,
            })
        data = {"selections": fallback_selections}
    return data
