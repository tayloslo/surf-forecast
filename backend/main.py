"""
Surf Forecast API.

Endpoints:
  GET    /api/buoys/nearest?lat=&lon=       -> nearest NOAA buoy stations
  GET    /api/spots                          -> list saved spots
  POST   /api/spots                          -> create a spot
  GET    /api/spots/{id}                     -> get one spot (with prefs)
  PUT    /api/spots/{id}                     -> update a spot's prefs
  DELETE /api/spots/{id}                     -> delete a spot
  GET    /api/spots/{id}/forecast?days=      -> scored hourly forecast + live buoy reading

Run: uvicorn main:app --reload --port 8420
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
import logging

from database import init_db, get_session, Spot
from noaa_client import find_nearest_buoy, fetch_buoy_observation, fetch_marine_forecast, find_working_nearest_buoy
from scoring import score_hour, label_for_score

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("surf-api")

app = FastAPI(title="Surf Forecast API")

# Wide open CORS since this is a small shared tool with no auth; tighten
# with an explicit origins list if this ever needs to be locked down.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

init_db()


class SpotIn(BaseModel):
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    facing_direction: float = Field(270.0, ge=0, lt=360)
    swell_window_deg: float = Field(45.0, gt=0, le=180)
    min_good_period_s: float = Field(8.0, gt=0)
    ideal_period_s: float = Field(12.0, gt=0)
    min_good_height_m: float = Field(0.5, ge=0)
    ideal_height_m: float = Field(1.5, gt=0)
    max_good_height_m: float = Field(3.0, gt=0)
    max_good_wind_kmh: float = Field(15.0, gt=0)
    onshore_window_deg: float = Field(90.0, gt=0, le=180)
    nearest_buoy_id: Optional[str] = None


class SpotOut(SpotIn):
    id: int

    class Config:
        from_attributes = True


def _spot_to_out(spot: Spot) -> SpotOut:
    return SpotOut.model_validate(spot)


@app.get("/api/buoys/nearest")
def nearest_buoys(lat: float, lon: float, count: int = 5):
    return find_nearest_buoy(lat, lon, max_results=count)


@app.get("/api/spots", response_model=list[SpotOut])
def list_spots():
    db = get_session()
    try:
        return [_spot_to_out(s) for s in db.query(Spot).order_by(Spot.name).all()]
    finally:
        db.close()


@app.post("/api/spots", response_model=SpotOut)
async def create_spot(body: SpotIn):
    db = get_session()
    try:
        if not body.nearest_buoy_id:
            # Try nearest candidates in order and pick the first with a
            # live feed, so we don't silently attach a dead buoy.
            working_id, _ = await find_working_nearest_buoy(body.lat, body.lon)
            if not working_id:
                nearest = find_nearest_buoy(body.lat, body.lon, max_results=1)
                working_id = nearest[0]["id"] if nearest else None
            body.nearest_buoy_id = working_id
        spot = Spot(**body.model_dump())
        db.add(spot)
        db.commit()
        db.refresh(spot)
        return _spot_to_out(spot)
    finally:
        db.close()


@app.get("/api/spots/{spot_id}", response_model=SpotOut)
def get_spot(spot_id: int):
    db = get_session()
    try:
        spot = db.get(Spot, spot_id)
        if not spot:
            raise HTTPException(404, "Spot not found")
        return _spot_to_out(spot)
    finally:
        db.close()


@app.put("/api/spots/{spot_id}", response_model=SpotOut)
def update_spot(spot_id: int, body: SpotIn):
    db = get_session()
    try:
        spot = db.get(Spot, spot_id)
        if not spot:
            raise HTTPException(404, "Spot not found")
        for k, v in body.model_dump().items():
            setattr(spot, k, v)
        db.commit()
        db.refresh(spot)
        return _spot_to_out(spot)
    finally:
        db.close()


@app.delete("/api/spots/{spot_id}")
def delete_spot(spot_id: int):
    db = get_session()
    try:
        spot = db.get(Spot, spot_id)
        if not spot:
            raise HTTPException(404, "Spot not found")
        db.delete(spot)
        db.commit()
        return {"deleted": spot_id}
    finally:
        db.close()


@app.get("/api/spots/{spot_id}/forecast")
async def spot_forecast(spot_id: int, days: int = 7):
    days = max(1, min(days, 10))
    db = get_session()
    try:
        spot = db.get(Spot, spot_id)
        if not spot:
            raise HTTPException(404, "Spot not found")
        spot_copy = _spot_to_out(spot)
    finally:
        db.close()

    result = {"spot": spot_copy, "buoy_observation": None, "buoy_error": None,
              "forecast_error": None, "hours": []}

    try:
        forecast = await fetch_marine_forecast(spot_copy.lat, spot_copy.lon, days=days)
        result["hours"] = [score_hour(spot_copy_as_spot(spot_copy), h) for h in forecast["hours"]]
        for h in result["hours"]:
            h["label"] = label_for_score(h["score"])
    except Exception as e:
        log.exception("forecast fetch failed for spot %s", spot_id)
        result["forecast_error"] = f"Could not load forecast right now: {e}"

    obs = None
    if spot_copy.nearest_buoy_id:
        try:
            obs = await fetch_buoy_observation(spot_copy.nearest_buoy_id)
        except Exception:
            log.exception("buoy fetch failed for spot %s", spot_id)
            obs = None

    if not obs:
        # Configured buoy is dead/silent - search nearby for a live one
        # instead of just reporting an error every time.
        try:
            working_id, obs = await find_working_nearest_buoy(spot_copy.lat, spot_copy.lon)
            if working_id and working_id != spot_copy.nearest_buoy_id:
                db2 = get_session()
                try:
                    db_spot = db2.get(Spot, spot_id)
                    if db_spot:
                        db_spot.nearest_buoy_id = working_id
                        db2.commit()
                finally:
                    db2.close()
        except Exception as e:
            log.exception("buoy fallback search failed for spot %s", spot_id)

    if obs:
        result["buoy_observation"] = obs
    else:
        result["buoy_error"] = "No nearby NOAA buoy currently has live data."

    return result


def spot_copy_as_spot(spot_out: SpotOut) -> Spot:
    """score_hour() takes a Spot ORM-like object; build a lightweight
    stand-in from the already-validated Pydantic model instead of
    re-querying the DB, since we've already fetched it above."""
    s = Spot()
    for k, v in spot_out.model_dump().items():
        if k != "id":
            setattr(s, k, v)
    return s


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the frontend as static files from the same service, so a single
# deployed app gives you both the API and the UI on one URL. The catch-all
# route lets client-side navigation work if we ever add routing; for now
# every non-API path just serves index.html since this is a single-page app.
import os
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        candidate = os.path.join(_STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))
