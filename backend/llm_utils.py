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
def select_place_and_item(prompt: str, places: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Given the user prompt and a list of Google Places results,
    ask the LLM to choose the best restaurant and suggest a specific dish.
    """
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
        "You are an AI food-ordering assistant. "
        "The user describes what they want to eat. "
        "You are given a JSON list of candidate restaurants from Google Places. "
        "Choose the single best restaurant and suggest a specific dish or drink "
        "that matches the user's request.\n\n"
        "Respond ONLY with a JSON object with keys:\n"
        "  place_index (integer, index into the list),\n"
        "  item_name (string),\n"
        "  estimated_total_price (number, in USD)."
    )

    user_msg = (
        "User prompt:\n"
        f"{prompt}\n\n"
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
            "place_index": 0,
            "item_name": "Recommended item",
            "estimated_total_price": 0.0,
        }
    return data
