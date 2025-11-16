# backend/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
import time
import os

from dotenv import load_dotenv
load_dotenv()

# -----------------------------
# Sentry
# -----------------------------
import sentry_sdk
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

sentry_sdk.init(
    dsn="https://f9c642c0fbed9851a68015038374a11d@o4510370374615040.ingest.us.sentry.io/4510370599862272",
    traces_sample_rate=1.0,
    profiles_sample_rate=1.0,
)

# -----------------------------
# Daytona (optional)
# -----------------------------
from daytona import Daytona, DaytonaConfig

DAYTONA_API_KEY = os.getenv("DAYTONA_API_KEY")
DAYTONA_ENABLED = bool(DAYTONA_API_KEY)

daytona = None
if DAYTONA_ENABLED:
    try:
        daytona = Daytona(DaytonaConfig(api_key=DAYTONA_API_KEY))
    except Exception as e:
        print("[daytona] init failed, disabling:", e)
        daytona = None

# -----------------------------
# Local imports
# -----------------------------
from llm_utils import (
    parse_prompt_with_llm,
    select_place_and_item,
    select_batch_order,
)
from google_places_client import search_places, place_to_checkout_url

# -----------------------------
# Allergen synonyms
# -----------------------------
ALLERGEN_SYNONYMS = {
    "dairy": [
        "dairy", "milk", "cheese", "butter", "cream", "yogurt", "ice cream",
        "mozzarella", "parmesan", "cheddar", "latte", "cappuccino"
    ],
    "milk": [
        "milk", "cheese", "butter", "cream", "yogurt", "ice cream"
    ],
    "peanut": [
        "peanut", "peanuts", "peanut butter", "satay"
    ],
    "peanuts": [
        "peanut", "peanuts", "peanut butter", "satay"
    ],
    "tree nut": [
        "almond", "almonds", "cashew", "cashews", "pistachio", "pistachios",
        "walnut", "walnuts", "pecan", "pecans", "hazelnut", "hazelnuts",
        "macadamia"
    ],
    "nuts": [
        "almond", "almonds", "cashew", "cashews", "pistachio", "pistachios",
        "walnut", "walnuts", "pecan", "pecans", "hazelnut", "hazelnuts",
        "macadamia"
    ],
    "gluten": [
        "gluten", "wheat", "barley", "rye", "bread", "pasta", "noodles", "pizza"
    ],
    "egg": [
        "egg", "eggs", "omelette", "mayo", "mayonnaise"
    ],
    "shellfish": [
        "shellfish", "shrimp", "prawn", "prawns", "crab", "lobster",
        "scallop", "scallops", "oyster", "oysters", "clam", "clams",
        "mussel", "mussels"
    ],
}

# -----------------------------
# FastAPI setup
# -----------------------------
app = FastAPI()
app.add_middleware(SentryAsgiMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # hackathon/demo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Request models
# -----------------------------
class OrderRequest(BaseModel):
    prompt: str
    location: Optional[str] = None
    allergies: List[str] = []
    dietary_rules: List[str] = []
    exclude_place_ids: List[str] = []

class BatchOrderRequest(OrderRequest):
    num_restaurants: int = 3  # how many different restaurants to include in the batch


# -----------------------------
# Utility
# -----------------------------
def filter_places_by_allergens(
    places: List[Dict[str, Any]],
    allergies: List[str],
) -> List[Dict[str, Any]]:
    """
    Coarse restaurant-level filter:
    - If the restaurant name/address/types clearly mention an allergen OR a common
      component/synonym (e.g. cheese/butter for dairy, shrimp for shellfish),
      we drop that place.
    - If all places get dropped, we fall back to the original list so we don't
      leave the user stranded; the LLM layer will still try to avoid unsafe items.
    """
    if not allergies:
        return places

    bad_terms: List[str] = []
    for a in allergies:
        base = a.lower().strip()
        bad_terms.append(base)
        if base in ALLERGEN_SYNONYMS:
            bad_terms.extend(ALLERGEN_SYNONYMS[base])

    bad_terms = list({t.strip() for t in bad_terms if t.strip()})

    filtered: List[Dict[str, Any]] = []
    for p in places:
        text = " ".join([
            p.get("name", ""),
            p.get("formatted_address", ""),
            " ".join(p.get("types", []) or []),
        ]).lower()

        if any(term in text for term in bad_terms):
            continue
        filtered.append(p)

    return filtered or places


# -----------------------------
# Basic routes
# -----------------------------
@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}

@app.get("/debug-sentry")
def trigger_error():
    1 / 0

@app.get("/run-daytona")
def run_daytona():
    if daytona is None:
        return {"error": "Daytona not configured or API key missing."}
    try:
        sandbox = daytona.create()
        result = sandbox.process.code_run('print("Hello from Daytona!")')
        return {"exit_code": result.exit_code, "result": result.result}
    except Exception as e:
        print("[daytona] /run-daytona failed:", e)
        return {"error": "Daytona sandbox could not be created (quota or config)."}


# -----------------------------
# Batch order route
# -----------------------------
@app.post("/api/batch_order")
def create_batch_order(req: BatchOrderRequest) -> Dict[str, Any]:
    try:
        print(
            f"[api/batch_order] prompt={req.prompt!r}, "
            f"location={req.location!r}, num_restaurants={req.num_restaurants}"
        )

        if not req.location or not req.location.strip():
            return {
                "status": "error",
                "error": "Please enter a location (city, address, or ZIP).",
            }

        location_str = req.location.strip()

        # 1) Understand prompt
        constraints = parse_prompt_with_llm(
            req.prompt,
            allergies=req.allergies,
            dietary_rules=req.dietary_rules,
        )
        cuisine = constraints.get("cuisine") or "food"
        max_price = constraints.get("max_price")

        # Map max_price -> Google price_level
        max_price_level = None
        if isinstance(max_price, (int, float)):
            if max_price <= 10:
                max_price_level = 1
            elif max_price <= 20:
                max_price_level = 2
            elif max_price <= 35:
                max_price_level = 3
            else:
                max_price_level = 4

        # Build search query
        prompt_text = req.prompt.strip()
        if cuisine and cuisine.lower() != "food":
            search_query = f"{cuisine} {prompt_text}"
        else:
            search_query = prompt_text or cuisine

        # 2) Search Google Places
        places = search_places(
            query=search_query,
            location_str=location_str,
            max_price_level=max_price_level,
            limit=25,
        )

        if not places:
            return {"status": "error", "error": "No restaurants found for this request."}

        # Exclude places by ID (refresh workflow)
        exclude_ids = set(req.exclude_place_ids or [])
        if exclude_ids:
            filtered = [p for p in places if p.get("place_id") not in exclude_ids]
            if filtered:
                places = filtered

        # Allergen filter
        places = filter_places_by_allergens(places, req.allergies or [])
        if not places:
            return {
                "status": "error",
                "error": "No restaurants safe for this group based on allergies.",
            }

        # 3) LLM batch planning
        plan = select_batch_order(
            req.prompt,
            places,
            allergies=req.allergies,
            dietary_rules=req.dietary_rules,
            max_restaurants=req.num_restaurants,
        )

        selections = plan.get("selections", [])
        if not isinstance(selections, list) or not selections:
            return {
                "status": "error",
                "error": "The planner could not build a batch order.",
            }

        restaurant_cards: List[Dict[str, Any]] = []
        used_indices: set[int] = set()

        for sel in selections:
            idx = sel.get("place_index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(places):
                continue
            if idx in used_indices:
                continue
            used_indices.add(idx)

            chosen = places[idx]
            item_name = sel.get("item_name", "Recommended group item")
            est_price = sel.get("estimated_total_price") or max_price or 0

            address = chosen.get("formatted_address", "Address unavailable")
            place_id = chosen.get("place_id")
            checkout_url = place_to_checkout_url(place_id) if place_id else ""

            restaurant_cards.append({
                "platform": "Google Maps",
                "restaurant_name": chosen.get("name", "Unknown restaurant"),
                "drink_name": item_name,
                "total_price": est_price,
                "eta_minutes": 35,
                "restaurant_address": address,
                "checkout_url": checkout_url,
                "place_id": place_id,
            })

        if not restaurant_cards:
            return {
                "status": "error",
                "error": "No valid restaurant selections for the batch.",
            }

        return {
            "status": "ok",
            "data": {"restaurants": restaurant_cards},
            "timestamp": int(time.time()),
        }

    except Exception as e:
        print("[api/batch_order][error]", e)
        raise


# -----------------------------
# Single order route
# -----------------------------
@app.post("/api/order")
def create_order(req: OrderRequest) -> Dict[str, Any]:
    try:
        print(f"[api/order] prompt={req.prompt!r}, location={req.location!r}")

        if not req.location or not req.location.strip():
            return {
                "status": "error",
                "error": "Please enter a location (city, address, or ZIP).",
            }

        location_str = req.location.strip()

        # 1) Parse constraints
        constraints = parse_prompt_with_llm(
            req.prompt,
            allergies=req.allergies,
            dietary_rules=req.dietary_rules,
        )
        cuisine = constraints.get("cuisine") or "food"
        max_price = constraints.get("max_price")

        # 2) Map max_price -> Google price level
        max_price_level = None
        if isinstance(max_price, (int, float)):
            if max_price <= 10:
                max_price_level = 1
            elif max_price <= 20:
                max_price_level = 2
            elif max_price <= 35:
                max_price_level = 3
            else:
                max_price_level = 4

        # 3) Build search query
        prompt_text = req.prompt.strip()
        if cuisine and cuisine.lower() != "food":
            search_query = f"{cuisine} {prompt_text}"
        else:
            search_query = prompt_text or cuisine

        # 4) Search Google Places
        places = search_places(
            query=search_query,
            location_str=location_str,
            max_price_level=max_price_level,
            limit=12,
        )

        exclude_ids = set(req.exclude_place_ids or [])
        if exclude_ids:
            filtered = [p for p in places if p.get("place_id") not in exclude_ids]
            if filtered:
                places = filtered

        places = filter_places_by_allergens(places, req.allergies or [])
        if not places:
            return {
                "status": "error",
                "error": "No restaurants found matching your request.",
            }

        # 5) Daytona (optional) – log + validate recommendation
        sandbox = None
        if daytona is not None:
            try:
                sandbox = daytona.create()
                sandbox.process.code_run("print('Daytona check OK')")
            except Exception as e:
                print("[daytona] skipped (create failed / quota):", e)
                sandbox = None

        # 6) LLM selection logic (strict allergy-aware)
        selection = select_place_and_item(
            req.prompt,
            places,
            allergies=req.allergies,
            dietary_rules=req.dietary_rules,
        )

        no_safe = bool(selection.get("no_safe_option"))
        idx = selection.get("place_index", -1)
        item_name = selection.get("item_name")
        est_price = selection.get("estimated_total_price")

        if (
            no_safe
            or idx is None
            or not isinstance(idx, int)
            or idx < 0
            or idx >= len(places)
            or not item_name
        ):
            return {
                "status": "error",
                "error": (
                    "We couldn't confidently find any other safe recommendations that match your "
                    "allergies and request near this location. "
                    "You may want to broaden your search or double-check directly with a restaurant."
                ),
            }

        chosen = places[idx]

        # Optional Daytona validation
        if sandbox is not None:
            try:
                validation_code = f"""
allergies = {[a.lower() for a in (req.allergies or [])]}
item = {item_name!r}.lower()
unsafe = [a for a in allergies if a in item]
print("unsafe_matches", unsafe)
"""
                validation_result = sandbox.process.code_run(validation_code)
                print("[daytona][validation]", (validation_result.result or "").strip())
            except Exception as e:
                print("[daytona] validation skipped:", e)

        # Fallback if the model didn't give a price
        if est_price is None:
            est_price = max_price or 0

        address = chosen.get("formatted_address", "Address unavailable")
        place_id = chosen.get("place_id")
        checkout_url = place_to_checkout_url(place_id) if place_id else ""

        data = {
            "platform": "Google Maps",
            "restaurant_name": chosen.get("name", "Unknown restaurant"),
            "drink_name": item_name,
            "total_price": est_price,
            "eta_minutes": 25,
            "restaurant_address": address,
            "checkout_url": checkout_url,
            "place_id": place_id,
        }

        return {"status": "ok", "data": data, "timestamp": int(time.time())}

    except Exception as e:
        print("[api/order][error]", e)
        raise
