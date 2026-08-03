"""Pulls a 5-day forecast from the National Weather Service (api.weather.gov) -
free, no API key, and the most authoritative source for US locations (built on
NOAA's National Blend of Models). Scores each day for pool-swimming suitability.
"""
import os
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "waterguru-dashboard (contact via github.com/billford/waterguru-dashboard)"}


def _grid_forecast_url(lat: str, lon: str) -> str:
    points = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=UA, timeout=15).json()
    return points["properties"]["forecast"]


def swim_score(temp_f: float, pop: int, wind_mph: float, short_forecast: str) -> tuple[int, str]:
    """0-100 suitability score for outdoor swimming, plus a one-line reason."""
    score = 100
    reasons = []

    if temp_f < 70:
        score -= 40
        reasons.append("cool air temp")
    elif temp_f < 78:
        score -= 15

    if pop >= 60:
        score -= 45
        reasons.append("likely rain")
    elif pop >= 30:
        score -= 20
        reasons.append("chance of rain")

    if wind_mph >= 20:
        score -= 25
        reasons.append("windy")
    elif wind_mph >= 12:
        score -= 10

    lowered = short_forecast.lower()
    if "thunder" in lowered or "storm" in lowered:
        score -= 50
        reasons.append("storms")

    score = max(0, min(100, score))
    reason = ", ".join(reasons) if reasons else "warm, calm, dry"
    return score, reason


def _parse_wind_mph(wind_speed: str) -> float:
    # NWS gives strings like "8 mph" or "10 to 15 mph"
    parts = [int(p) for p in wind_speed.split() if p.isdigit()]
    return max(parts) if parts else 0.0


def fetch_forecast(lat: str, lon: str, days: int = 5) -> list[dict]:
    forecast_url = _grid_forecast_url(lat, lon)
    data = requests.get(forecast_url, headers=UA, timeout=15).json()
    periods = [p for p in data["properties"]["periods"] if p["isDaytime"]]

    out = []
    for p in periods[:days]:
        temp = p["temperature"]
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        wind = _parse_wind_mph(p["windSpeed"])
        score, reason = swim_score(temp, pop, wind, p["shortForecast"])
        icon = p["icon"].replace("size=medium", "size=large") if "size=" in p["icon"] else p["icon"] + "?size=large"
        out.append(
            {
                "name": p["name"],
                "date": p["startTime"][:10],
                "temp_f": temp,
                "pop_pct": pop,
                "wind_mph": wind,
                "short_forecast": p["shortForecast"],
                "icon": icon,
                "swim_score": score,
                "swim_reason": reason,
                "good_swim_day": score >= 65,
            }
        )
    return out


def export_weather(out_path: Path):
    lat = os.environ.get("WX_LAT")
    lon = os.environ.get("WX_LON")
    if not lat or not lon:
        print("WX_LAT/WX_LON not set, skipping weather export")
        return None
    forecast = fetch_forecast(lat, lon)
    payload = {"location": {"lat": lat, "lon": lon}, "days": forecast}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    out_path.write_text(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    print(export_weather(HERE / "site" / "data" / "weather.json"))
