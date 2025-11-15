# backend/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
import time
import os

# ✅ Load environment variables
from dotenv import load_dotenv
load_dotenv()

# ✅ Import Sentry SDK
import sentry_sdk
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

# ✅ Initialize Sentry
sentry_sdk.init(
    dsn="https://f9c642c0fbed9851a68015038374a11d@o4510370374615040.ingest.us.sentry.io/4510370599862272",
    traces_sample_rate=1.0,
    profiles_sample_rate=1.0,
)

# ✅ Daytona integration
from daytona import Daytona, DaytonaConfig

DAYTONA_API_KEY = os.getenv("DAYTONA_API_KEY")
daytona = Daytona(DaytonaConfig(api_key=DAYTONA_API_KEY))

from llm_utils import parse_prompt_with_llm, select_place_and_item
from google_places_client import search_places, place_to_checkout_url

# ✅ FastAPI app setup
app = FastAPI()

# ✅ Sentry middleware
app.add_middleware(SentryAsgiMiddleware)

# ✅ CORS (open for testing/hackathon)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ Request model
class OrderRequest(BaseModel):
    prompt: str
    location: Optional[str] = None
    allergies: List[str] = []
    dietary_rules: List[str] = []
    exclude_place_ids: List[str] = []

# ✅ Health check
@app.get("/health")
def health() -> Dict[str, bool]:
    return {"ok": True}

# ✅ Debug test for Sentry
@app.get("/debug-sentry")
def trigger_error():
    1 / 0

# ✅ Test Daytona route
@app.get("/run-daytona")
def run_daytona():
    sandbox = daytona.create()
    code = 'print("Hello from Daytona!")'
    result = sandbox.process.code_run(code)
    return {"exit_code": result.exit_code, "result": result.result}

# ✅ Main API route
@app.post("/api/order")
def create_order(req: OrderRequest) -> Dict[str, Any]:
    try:
        print(f"[api/order] prompt={req.prompt!r}, location={req.location!r}")

        if not req.location or not req.location.strip():
            return {"status": "error", "error": "Please enter a location (city, address, or ZIP)."}

        location_str = req.location.strip()

        # Step 1: LLM parsing
        constraints = parse_prompt_with_llm(
            req.prompt,
            allergies=req.allergies,
            dietary_rules=req.dietary_rules,
        )
        cuisine = constraints.get("cuisine") or "food"
        max_price = constraints.get("max_price")

        # Step 2: Map max_price -> Google price level
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

        # Step 3: Combine cuisine + raw prompt for search
        prompt_text = req.prompt.strip()
        if cuisine and cuisine.lower() != "food":
            search_query = f"{cuisine} {prompt_text}"
        else:
            search_query = prompt_text or cuisine

        # Step 4: Search Google Places
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

        # Step 5: Run a short Daytona sandbox snippet for debugging/logging
        sandbox = daytona.create()
        sandbox.process.code_run(f'print("Processing cuisine: {cuisine}")')

        # Step 6: LLM selection logic
        selection = select_place_and_item(
            req.prompt,
            places,
            allergies=req.allergies,
            dietary_rules=req.dietary_rules,
        )
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
        print("[api/order][error]", e)
        raise  # Sentry will capture it automatically

# ✅ Helper: Allergen filtering
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
