"""Catalogue of alertable events (single source of truth for worker, API and docs)."""
from __future__ import annotations

import re

SEVERITY_RANK = {"INFO": 0, "MODERATE": 1, "SEVERE": 2, "EXTREME": 3}

# Radar nowcast events (0-60 min). threshold = reflectivity (dBZ) contour that is vectorized.
RADAR_EVENTS: dict[str, dict] = {
    "RAIN_START": {"dbz": 20, "severity": "INFO", "dedup_min": 120,
                   "label": "Inizio pioggia", "title": "🌂 Pioggia in arrivo"},
    "HEAVY_RAIN": {"dbz": 35, "severity": "MODERATE", "dedup_min": 45,
                   "label": "Pioggia forte / rovescio", "title": "🌧️ Rovescio intenso"},
    "CLOUDBURST": {"dbz": 45, "severity": "SEVERE", "dedup_min": 45,
                   "label": "Temporale forte / nubifragio", "title": "⚠️ Temporale forte"},
    "HAIL": {"dbz": 55, "severity": "EXTREME", "dedup_min": 45,
             "label": "Grandine / downburst probabile", "title": "🚨 Possibile grandine"},
    "SNOW": {"dbz": 15, "severity": "MODERATE", "dedup_min": 120,
             "label": "Neve", "title": "❄️ Neve in arrivo"},
}

# Institutional CAP warnings (MeteoAlarm / Protezione Civile), hours to ~2 days ahead.
CAP_EVENTS: dict[str, dict] = {
    "CAP_THUNDERSTORM": {"label": "Allerta temporali", "match": r"thunderstorm"},
    "CAP_RAIN": {"label": "Allerta pioggia / alluvione", "match": r"rain|flood"},
    "CAP_WIND": {"label": "Allerta vento", "match": r"wind"},
    "CAP_SNOW_ICE": {"label": "Allerta neve / ghiaccio", "match": r"snow|ice"},
    "CAP_FOG": {"label": "Allerta nebbia", "match": r"fog"},
    "CAP_HEAT": {"label": "Allerta caldo", "match": r"high.?temperature|heat"},
    "CAP_COLD": {"label": "Allerta freddo", "match": r"low.?temperature|cold"},
    "CAP_COASTAL": {"label": "Allerta mareggiate", "match": r"coastal"},
    "CAP_FOREST_FIRE": {"label": "Allerta incendi boschivi", "match": r"forest.?fire|fire"},
    "CAP_AVALANCHE": {"label": "Allerta valanghe", "match": r"avalanche"},
}

ALL_EVENTS = set(RADAR_EVENTS) | set(CAP_EVENTS)
DEFAULT_ALERT_TYPES = ["HEAVY_RAIN", "CLOUDBURST", "HAIL", "CAP_THUNDERSTORM", "CAP_RAIN", "CAP_WIND"]

# vectorized threshold (dBZ) -> radar event (rain events; SNOW handled separately via snow mask)
THRESHOLD_EVENTS = {v["dbz"]: k for k, v in RADAR_EVENTS.items() if k != "SNOW"}


def cap_event_type(event_text: str) -> str | None:
    t = (event_text or "").lower()
    for key, spec in CAP_EVENTS.items():
        if re.search(spec["match"], t):
            return key
    return None


def sql_rank(col: str) -> str:
    return (f"(CASE {col} WHEN 'EXTREME' THEN 3 WHEN 'SEVERE' THEN 2 "
            f"WHEN 'MODERATE' THEN 1 ELSE 0 END)")
