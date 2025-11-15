# backend/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
import time

# ✅ 1. Import sentry-sdk
import sentry_sdk
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

# ✅ 2. Initialize Sentry before creating app
sentry_sdk.init(
    dsn="https://f9c642c0fbed9851a68015038374a11d@o4510370374615040.ingest.us.sentry.io/4510370599862272",
    traces_sample_rate=1.0,  # capture 100% of performance traces
    profiles_sample_rate=1.0,  # optional: captures profiling data
)

from llm_utils import parse_prompt_with_llm, select_place_and_item
from google_places_client import search_places, place_to_checkout_url

app = FastAPI()

# ✅ 3. Attach Sentry middleware so all routes are monitored
app.add_middleware(SentryAsgiMiddleware)

# ✅ 4. (Optional) add CORS after Sentry
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # open for hackathon
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class OrderRequest(BaseModel):
    prompt: str
    location: Optional[str] = None
    allergies: List[str] = []
    dietary_rules: List[str] = []
    exclude_place_ids: List[str] = []   # 👈 NEW

@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}

@app.post("/api/order")
def create_order(req: OrderRequest) -> Dict[str, Any]:
    try:
        print(f"[api/order] prompt={req.prompt!r}, location={req.location!r}")

        if not req.location or not req.location.strip():
            return {"status": "error", "error": "Please enter a location (city, address, or ZIP)."}

        location_str = req.location.strip()

                constraints = parse_prompt_with_llm(
            req.prompt,
            allergies=req.allergies,
            dietary_rules=req.dietary_rules,
        )
        cuisine = constraints.get("cuisine") or "food"
        max_price = constraints.get("max_price")

        # Map max_price -> Google price_level 0–4 (rough heuristic)
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

        # 👇 Use BOTH the parsed cuisine and the raw prompt so
        #    keywords like "chicken" / "wings" stay in the query.
        prompt_text = req.prompt.strip()
        if cuisine and cuisine.lower() != "food":
            search_query = f"{cuisine} {prompt_text}"
        else:
            search_query = prompt_text or cuisine

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
            return {"status": "error", "error": "No restaurants found matching your request."}

        # 3) LLM: pick best place + item
        selection = select_place_and_item(req.prompt, places)
        idx = selection.get("place_index", 0)
        if not isinstance(idx, int) or idx < 0 or idx >= len(places):
            idx = 0

        chosen = places[idx]
        item_name = selection.get("item_name", "Recommended item")
        est_price = selection.get("estimated_total_price") or max_price or 0

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
        # ✅ Any unhandled exception automatically gets sent to Sentry
        print("[api/order][error]", e)
        raise  # re-raise so Sentry can capture it properly

def filter_places_by_allergens(places: List[Dict[str, Any]], allergies: List[str]) -> List[Dict[str, Any]]:
    if not allergies:
        return places

    bad = [a.lower() for a in allergies]
    filtered = []
    for p in places:
        text = " ".join([
            p.get("name", ""),
            p.get("formatted_address", ""),
            " ".join(p.get("types", []) or []),
        ]).lower()
        if any(word in text for word in bad):
            continue
        filtered.append(p)

    return filtered or places
