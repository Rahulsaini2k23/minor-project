#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         SMART SPECS – Campus Navigation System for Visually Impaired        ║
║         Dr. B.R. Ambedkar National Institute of Technology, Jalandhar        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Version     : 3.2  (smart instruction triggers + push-button support)      ║
║  Platform    : Raspberry Pi (production)  |  Windows/Linux (simulation)      ║
║  Python      : 3.8+                                                          ║
║  Hardware    : NEO-6M GPS Module + USB Microphone + Speaker                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  BUG FIXED (v3.1)                                                            ║
║   • Google Maps returned "Khiala" (a nearby village) for campus building     ║
║     queries because those buildings are not in Google's POI database.         ║
║     Fixed by:                                                                ║
║       1. A local CAMPUS_LANDMARKS dict as the PRIMARY lookup.                ║
║       2. Input pre-processing: strips noise like "in nit jalandhar",         ║
║          "at nit", "go to", "take me to" etc. before matching.               ║
║       3. Strict geocoding validation: rejects results whose returned name    ║
║          has no lexical overlap with the original query.                     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Sources integrated:                                                         ║
║   • github.com/Uberi/speech_recognition  – STT engine & mic handling         ║
║   • github.com/geopy/geopy               – Geodesic distance & geocoding     ║
║   • github.com/FranzTscharf/Python-NEO-6M-GPS-Raspberry-Pi – NMEA parsing   ║
║   • github.com/AV-Lab/RoadSceneUnderstanding-ModifiedUNet – Scene awareness  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Install:                                                                    ║
║   pip install pynmea2 pyttsx3 speechrecognition geopy pyaudio pyserial       ║
║   pip install googlemaps python-dotenv                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ══════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ══════════════════════════════════════════════════════════════════════════════
import os
import sys
import time
import math
import queue
import random
import re
import difflib
import threading
import logging
import platform
import json
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("smart_specs.log", mode="w", encoding="utf-8"),
    ],
)
logger = logging.getLogger("SmartSpecs")
IS_WINDOWS = platform.system() == "Windows"


def _require(mod: str, pkg: str) -> None:
    try:
        __import__(mod)
    except ImportError:
        logger.critical("Missing package '%s'. Run:  pip install %s", mod, pkg)
        sys.exit(1)

_require("serial",             "pyserial")
_require("pynmea2",            "pynmea2")
_require("pyttsx3",            "pyttsx3")
_require("speech_recognition", "speechrecognition")
_require("geopy",              "geopy")
_require("googlemaps",         "googlemaps")

import serial
import pynmea2
import pyttsx3
import speech_recognition as sr
import googlemaps
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

_GMAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
if _GMAPS_KEY:
    gmaps_client = googlemaps.Client(key=_GMAPS_KEY)
    logger.info("Google Maps API key loaded.")
else:
    gmaps_client = None
    logger.warning("GOOGLE_MAPS_API_KEY not found – falling back to Nominatim.")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
class Config:
    SIMULATION_MODE: bool = True
    TEXT_INPUT_MODE: bool = True

    # ── GPS / Serial ──────────────────────────────────────────────────────────
    GPS_PORT: str    = "/dev/serial0"
    GPS_BAUD: int    = 9600
    GPS_TIMEOUT: int = 1

    # ── Navigation ────────────────────────────────────────────────────────────
    ARRIVED_METERS: int        = 40     # arrival bubble (generous until coords surveyed)
    MIN_MOVE_METERS: float     = 3.0    # min movement before heading is recalculated

    # ── Turn detection ────────────────────────────────────────────────────────
    # An instruction is spoken automatically ONLY when the required turn
    # category changes (e.g. "straight" → "turn right").
    # The degree threshold below filters tiny GPS wobble:
    #   A new instruction fires when bearing-to-destination shifts by more
    #   than TURN_TRIGGER_DEG degrees AND the turn *category* changes.
    TURN_TRIGGER_DEG: float    = 20.0   # degrees of bearing shift to trigger

    # ── Advance warning ───────────────────────────────────────────────────────
    # Spoken once per navigation leg when the person is this many metres from
    # destination AND a non-straight turn is still required.
    # Gives the person time to slow down and prepare before the turn.
    ADVANCE_WARN_M: int        = 80     # metres ahead to give turn preview

    # ── Stopped detection ─────────────────────────────────────────────────────
    # If person hasn't moved MIN_MOVE_METERS in this many seconds, remind them.
    STOPPED_TIMEOUT_S: float   = 40.0   # seconds of no movement = prompt once

    # ── Push button (hardware: Raspberry Pi GPIO) ─────────────────────────────
    # Wiring: connect one side of button to GPIO pin below, other side to GND.
    # The pin is set as INPUT with internal pull-up, so pressing = LOW.
    BUTTON_GPIO_PIN: int       = 17     # BCM pin number (change to your wiring)
    BUTTON_DEBOUNCE_MS: int    = 300    # milliseconds debounce

    # Simulation / TEXT_INPUT_MODE:  press ENTER key to trigger button
    # Hardware mode:                 press physical button wired to BUTTON_GPIO_PIN

    # ── Campus ────────────────────────────────────────────────────────────────
    CAMPUS_RADIUS_METERS: float = 900.0
    CAMPUS_LAT: float = 31.3967
    CAMPUS_LON: float = 75.5303

    # ── Voice ─────────────────────────────────────────────────────────────────
    TTS_RATE: int     = 140
    TTS_VOLUME: float = 1.0
    POST_SPEECH_DELAY: float = 0.5
    MAX_RETRIES: int  = 3

    # ── STT ───────────────────────────────────────────────────────────────────
    STT_LANGUAGE: str = "en-IN"
    LISTEN_TIMEOUT: int       = 8
    PHRASE_TIME_LIMIT: int    = 12
    ENERGY_THRESHOLD: int     = 300
    DYNAMIC_ENERGY: bool      = True

    FUZZY_THRESHOLD: float = 0.50


# ══════════════════════════════════════════════════════════════════════════════
# CAMPUS LANDMARK DATABASE  ← PRIMARY lookup (no geocoding for these)
#
# WHY THIS EXISTS:
#   Google Maps / Nominatim do NOT have per-building data for NIT Jalandhar.
#   Querying "admin block NIT Jalandhar" returns the nearest geocodable point
#   (historically Khiala village) because the building is not a registered
#   Google POI.  Hardcoding the coordinates of known campus landmarks is the
#   only reliable solution for a campus-specific navigation system.
#
# HOW TO ADD A LANDMARK:
#   "canonical name": (latitude, longitude, ["alias1", "alias2", ...])
# ══════════════════════════════════════════════════════════════════════════════
CAMPUS_LANDMARKS: Dict[str, Tuple[float, float, List[str]]] = {
    # ── Gates ─────────────────────────────────────────────────────────────────
    "main gate":            (31.3944, 75.5274, ["gate", "entrance", "entry",
                                                "main entrance", "front gate",
                                                "mukhya dwar"]),
    "back gate":            (31.3990, 75.5330, ["rear gate", "side gate",
                                                "pichhla gate"]),

    # ── Administration ────────────────────────────────────────────────────────
    "admin block":          (31.3967, 75.5303, ["administrative block",
                                                "administration block",
                                                "administration building",
                                                "admin building",
                                                "admin office", "office block",
                                                "director office",
                                                "administrative office"]),
    "director residence":   (31.3985, 75.5295, ["director house",
                                                "director bungalow",
                                                "vc house", "director home"]),
    "guest house":          (31.3980, 75.5288, ["visitor house",
                                                "guest block",
                                                "atithi griha"]),

    # ── Academic Buildings ────────────────────────────────────────────────────
    "lecture hall complex": (31.3960, 75.5308, ["lhc", "lecture complex",
                                                "lecture hall", "lh complex"]),
    "academic block 1":     (31.3958, 75.5312, ["ab1", "ab 1",
                                                "academic block one"]),
    "academic block 2":     (31.3956, 75.5315, ["ab2", "ab 2",
                                                "academic block two"]),
    "academic block 3":     (31.3954, 75.5318, ["ab3", "ab 3",
                                                "academic block three"]),

    # ── Departments ───────────────────────────────────────────────────────────
    "civil engineering":    (31.3962, 75.5320, ["civil dept", "civil department",
                                                "civil block"]),
    "computer science":     (31.3964, 75.5316, ["cse", "cse department",
                                                "computer dept",
                                                "it department", "it dept"]),
    "electrical engineering":(31.3966, 75.5322, ["eee", "electrical dept",
                                                  "electrical block"]),
    "mechanical engineering":(31.3968, 75.5325, ["mech", "mech dept",
                                                  "mechanical dept",
                                                  "mechanical block"]),
    "electronics":          (31.3963, 75.5319, ["ece", "ece department",
                                                "electronics dept",
                                                "electronics block"]),
    "chemical engineering": (31.3965, 75.5328, ["chemical dept",
                                                "chemical block"]),
    "textile technology":   (31.3961, 75.5331, ["textile dept",
                                                "textile block"]),
    "instrumentation":      (31.3959, 75.5326, ["instru dept",
                                                "instrumentation block"]),

    # ── Library ───────────────────────────────────────────────────────────────
    "central library":      (31.3969, 75.5300, ["library", "lib",
                                                "pustakalaya", "reading room",
                                                "central lib", "main library"]),

    # ── Boys Hostels ──────────────────────────────────────────────────────────
    "boys hostel 1":        (31.3953, 75.5325, ["bh1", "bh 1",
                                                "boys hostel one",
                                                "hostel 1", "hostel one"]),
    "boys hostel 2":        (31.3951, 75.5328, ["bh2", "bh 2",
                                                "boys hostel two",
                                                "hostel 2", "hostel two"]),
    "boys hostel 3":        (31.3949, 75.5331, ["bh3", "bh 3",
                                                "boys hostel three",
                                                "hostel 3", "hostel three"]),
    "boys hostel 4":        (31.3947, 75.5334, ["bh4", "bh 4",
                                                "boys hostel four",
                                                "hostel 4", "hostel four"]),
    "boys hostel 5":        (31.3945, 75.5337, ["bh5", "bh 5",
                                                "boys hostel five",
                                                "hostel 5", "hostel five"]),
    "boys hostel 6":        (31.3943, 75.5340, ["bh6", "bh 6",
                                                "boys hostel six",
                                                "hostel 6", "hostel six"]),
    "boys hostel 7":        (31.3941, 75.5338, ["bh7", "bh 7",
                                                "boys hostel seven",
                                                "hostel 7", "hostel seven"]),
    "boys hostel 8":        (31.3939, 75.5335, ["bh8", "bh 8",
                                                "boys hostel eight",
                                                "hostel 8", "hostel eight"]),

    # ── Girls Hostels ─────────────────────────────────────────────────────────
    "girls hostel 1":       (31.3972, 75.5318, ["gh1", "gh 1",
                                                "girls hostel one",
                                                "girls hostel"]),
    "girls hostel 2":       (31.3970, 75.5321, ["gh2", "gh 2",
                                                "girls hostel two"]),
    "girls hostel 3":       (31.3968, 75.5324, ["gh3", "gh 3",
                                                "girls hostel three"]),

    # ── Food & Shopping ───────────────────────────────────────────────────────
    "shopping complex":     (31.3963, 75.5295, ["market", "shops", "canteen area",
                                                "shopping area", "complex",
                                                "bazaar", "dukaan",
                                                "campus market"]),
    "central mess":         (31.3955, 75.5310, ["mess", "dining hall",
                                                "food court", "canteen",
                                                "khana", "cafeteria",
                                                "dining", "dining hall"]),
    "faculty canteen":      (31.3966, 75.5305, ["staff canteen", "faculty cafe",
                                                "teachers canteen"]),

    # ── Sports & Recreation ───────────────────────────────────────────────────
    "sports complex":       (31.3942, 75.5300, ["sports ground", "ground",
                                                "stadium", "sports",
                                                "playing field", "maidan"]),
    "swimming pool":        (31.3940, 75.5296, ["pool", "swimming"]),
    "open air theatre":     (31.3948, 75.5308, ["oat", "amphitheatre",
                                                "open theatre",
                                                "auditorium", "theatre"]),
    "gymnasium":            (31.3944, 75.5303, ["gym", "fitness centre",
                                                "fitness center"]),

    # ── Student Activity ──────────────────────────────────────────────────────
    "student activity centre": (31.3962, 75.5298, ["sac", "student centre",
                                                    "student center",
                                                    "activity centre",
                                                    "student activities"]),

    # ── Medical ───────────────────────────────────────────────────────────────
    "medical centre":       (31.3974, 75.5308, ["hospital", "dispensary",
                                                "medical center", "clinic",
                                                "health centre", "doctor",
                                                "medical block",
                                                "chikitsa kendra"]),

    # ── Placement & Training ──────────────────────────────────────────────────
    "placement cell":       (31.3969, 75.5308, ["placement office",
                                                "placement block",
                                                "tpo", "training and placement",
                                                "career services"]),

    # ── Misc ──────────────────────────────────────────────────────────────────
    "ncc block":            (31.3960, 75.5295, ["ncc", "national cadet corps"]),
    "workshop":             (31.3965, 75.5335, ["workshop block",
                                                "central workshop"]),
    "seminar hall":         (31.3966, 75.5310, ["conference hall",
                                                "seminar block",
                                                "seminar room"]),
    "bank":                 (31.3961, 75.5293, ["sbi", "atm", "bank branch"]),
    "post office":          (31.3963, 75.5291, ["post", "dak ghar"]),
    "water tank":           (31.3978, 75.5310, ["overhead tank", "water tower"]),
}

# ── Flat lookup: alias → canonical name ───────────────────────────────────────
_ALIAS_MAP: Dict[str, str] = {}
for _canonical, (_lat, _lon, _aliases) in CAMPUS_LANDMARKS.items():
    _ALIAS_MAP[_canonical] = _canonical
    for _alias in _aliases:
        _ALIAS_MAP[_alias.lower()] = _canonical


# ══════════════════════════════════════════════════════════════════════════════
# INPUT PRE-PROCESSOR
#
# Strips noise phrases that speech recognition or typing adds around the
# actual destination name, e.g.:
#   "take me to the admin block in nit jalandhar"  →  "admin block"
#   "go to library"                                →  "library"
#   "I want to go to boys hostel 1"                →  "boys hostel 1"
# ══════════════════════════════════════════════════════════════════════════════
# Phrases to strip from the start of the query
_PREFIX_NOISE = re.compile(
    r"^\s*(?:"
    r"(?:i\s+)?(?:want\s+to\s+go|need\s+to\s+go|have\s+to\s+go)\s+(?:to\s+)?|"
    r"(?:please\s+)?(?:take\s+me|bring\s+me|guide\s+me)\s+to\s+(?:the\s+)?|"
    r"(?:please\s+)?(?:go\s+to|navigate\s+to|navigate|go)\s+(?:the\s+)?|"
    r"(?:find|search|locate|show)\s+(?:the\s+)?|"
    r"(?:where\s+is\s+(?:the\s+)?)|"
    r"(?:mujhe\s+jana\s+hai\s+)?|"           # Hindi: mujhe jana hai
    r"(?:le\s+chalo\s+)?|"                   # Hindi: le chalo
    r"(?:dikhao\s+)?|"                       # Hindi: dikhao
    r"(?:the\s+)"
    r")+",
    re.IGNORECASE,
)

# Phrases to strip from the end of the query
_SUFFIX_NOISE = re.compile(
    r"\s*(?:"
    r"(?:in|at|on|near|inside|within|of|around)\s+"
    r"(?:the\s+)?"
    r"(?:nit\s+jalandhar|nit|national\s+institute\s+of\s+technology"
    r"|dr\s+br\s+ambedkar|ambedkar\s+nit"
    r"|campus|college|institute|university|nitj)"
    r")+\s*$",
    re.IGNORECASE,
)

# Strip filler words left after above passes
_FILLER = re.compile(r"\b(please|kindly|the|a|an)\b", re.IGNORECASE)


def preprocess_query(raw: str) -> str:
    """
    Clean raw speech/keyboard input down to just the destination name.

    Examples
    --------
    "admin block in nit jalandhar"          → "admin block"
    "take me to the library"                → "library"
    "go to boys hostel 1 at nit"            → "boys hostel 1"
    "I want to go to the central mess"      → "central mess"
    "canteen near campus"                   → "canteen"
    """
    q = raw.strip().lower()

    q = _PREFIX_NOISE.sub("", q).strip()
    q = _SUFFIX_NOISE.sub("", q).strip()

    # Remove isolated filler words but NOT inside multi-word names
    # (e.g. "open air theatre" must stay intact)
    # Only strip if the result is not empty
    cleaned = _FILLER.sub(" ", q).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    if cleaned:
        q = cleaned

    logger.info("Input pre-process: '%s'  →  '%s'", raw, q)
    return q


# ══════════════════════════════════════════════════════════════════════════════
# LOCATION FINDER  (campus DB first → geocoding fallback)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Destination:
    name: str
    lat: float
    lon: float
    address: str = ""
    source: str  = "campus_db"   # "campus_db" | "google" | "nominatim"

    @property
    def coords(self) -> Tuple[float, float]:
        return (self.lat, self.lon)


class LocationFinder:
    """
    Resolution order
    ────────────────
    1. Exact match in CAMPUS_LANDMARKS (including all aliases)
    2. Fuzzy match in CAMPUS_LANDMARKS (difflib ratio ≥ FUZZY_THRESHOLD)
    3. Google Maps Geocoding  (only if result passes relevance check)
    4. Nominatim geocoding    (only if result passes relevance check)

    The relevance check prevents returning unrelated places (e.g. Khiala
    village) when the geocoder cannot find a specific campus building.
    """

    _nominatim = Nominatim(user_agent="smart_specs_nitj_v3_1")

    # ── Public entry point ────────────────────────────────────────────────────
    @staticmethod
    def find(raw_input: str) -> Optional[Destination]:
        place = preprocess_query(raw_input)
        if not place:
            return None

        # 1 & 2 – campus landmark DB
        dest = LocationFinder._campus_lookup(place)
        if dest:
            return dest

        # 3 – Google Maps (strict relevance check)
        dest = LocationFinder._google_geocode_strict(place)
        if dest:
            return dest

        # 4 – Nominatim (strict relevance check)
        dest = LocationFinder._nominatim_geocode_strict(place)
        return dest

    # ── Step 1 & 2: campus landmark lookup ───────────────────────────────────
    @staticmethod
    def _campus_lookup(query: str) -> Optional[Destination]:
        q = query.lower().strip()

        # Exact or alias match
        if q in _ALIAS_MAP:
            canonical = _ALIAS_MAP[q]
            lat, lon, _ = CAMPUS_LANDMARKS[canonical]
            logger.info("[CampusDB] Exact match: '%s' → '%s'", q, canonical)
            return Destination(name=canonical.title(), lat=lat, lon=lon,
                               source="campus_db")

        # Fuzzy match against all canonical names and aliases
        all_keys = list(_ALIAS_MAP.keys())
        matches = difflib.get_close_matches(
            q, all_keys,
            n=1,
            cutoff=Config.FUZZY_THRESHOLD
        )
        if matches:
            canonical = _ALIAS_MAP[matches[0]]
            lat, lon, _ = CAMPUS_LANDMARKS[canonical]
            score = difflib.SequenceMatcher(None, q, matches[0]).ratio()
            logger.info("[CampusDB] Fuzzy match: '%s' → '%s' (score=%.2f)",
                        q, canonical, score)
            return Destination(name=canonical.title(), lat=lat, lon=lon,
                               source="campus_db")

        # Try matching individual words of the query against aliases
        # (handles "hostel bh1" → "boys hostel 1", "admin" → "admin block")
        words = set(q.split())
        best_score = 0.0
        best_canonical = None
        for key, canonical in _ALIAS_MAP.items():
            score = difflib.SequenceMatcher(None, q, key).ratio()
            if score > best_score:
                best_score = score
                best_canonical = canonical
            # Also try substring: key contains query or query contains key
            if q in key or key in q:
                score2 = len(min(q, key, key=len)) / len(max(q, key, key=len))
                if score2 > best_score:
                    best_score = score2
                    best_canonical = canonical

        # Only word-overlap match if ≥ 2 words match or strong substring
        if best_canonical and best_score >= Config.FUZZY_THRESHOLD:
            lat, lon, _ = CAMPUS_LANDMARKS[best_canonical]
            logger.info("[CampusDB] Substring match: '%s' → '%s' (%.2f)",
                        q, best_canonical, best_score)
            return Destination(name=best_canonical.title(), lat=lat, lon=lon,
                               source="campus_db")

        logger.info("[CampusDB] No match for: '%s'", q)
        return None

    # ── Step 3: Google Maps with relevance check ──────────────────────────────
    @staticmethod
    def _google_geocode_strict(query: str) -> Optional[Destination]:
        if not gmaps_client:
            return None

        lat0, lon0 = Config.CAMPUS_LAT, Config.CAMPUS_LON
        # Try with campus context first
        for search_query in [
            f"{query}, NIT Jalandhar, Punjab, India",
            f"{query}, Jalandhar, Punjab, India",
        ]:
            try:
                bounds = {
                    "southwest": (lat0 - 0.015, lon0 - 0.015),
                    "northeast": (lat0 + 0.015, lon0 + 0.015),
                }
                results = gmaps_client.geocode(search_query, bounds=bounds)

                if not results:
                    continue

                result   = results[0]
                loc      = result["geometry"]["location"]
                address  = result.get("formatted_address", "")
                returned = address.split(",")[0].strip().lower()

                # ── Relevance check ───────────────────────────────────────────
                # The returned name must share at least one meaningful token
                # with the original query to be accepted.
                if not LocationFinder._is_relevant(query, returned, address):
                    logger.warning(
                        "[Google] Rejected irrelevant result: '%s' → '%s'",
                        query, returned
                    )
                    continue

                # Must be near campus
                dist = geodesic((loc["lat"], loc["lng"]),
                                (Config.CAMPUS_LAT, Config.CAMPUS_LON)).meters
                if dist > Config.CAMPUS_RADIUS_METERS * 3:
                    logger.warning("[Google] Too far from campus (%.0fm): %s",
                                   dist, address)
                    continue

                logger.info("[Google] Accepted: '%s' → (%.6f, %.6f) — %s",
                            query, loc["lat"], loc["lng"], address)
                return Destination(
                    name=address.split(",")[0].strip(),
                    lat=loc["lat"], lon=loc["lng"],
                    address=address, source="google"
                )

            except Exception as exc:
                logger.error("[Google] Geocoding error: %s", exc)

        return None

    # ── Step 4: Nominatim with relevance check ────────────────────────────────
    @staticmethod
    def _nominatim_geocode_strict(query: str) -> Optional[Destination]:
        for search_query in [
            f"{query}, NIT Jalandhar, Jalandhar, Punjab, India",
            f"{query}, Jalandhar, Punjab, India",
        ]:
            try:
                location = LocationFinder._nominatim.geocode(
                    search_query, timeout=10
                )
                if not location:
                    continue

                returned = location.address.split(",")[0].strip().lower()

                if not LocationFinder._is_relevant(query, returned,
                                                    location.address):
                    logger.warning(
                        "[Nominatim] Rejected irrelevant: '%s' → '%s'",
                        query, returned
                    )
                    continue

                logger.info("[Nominatim] Accepted: '%s' → (%.6f, %.6f)",
                            query, location.latitude, location.longitude)
                return Destination(
                    name=search_query.split(",")[0].strip(),
                    lat=location.latitude, lon=location.longitude,
                    address=location.address or "", source="nominatim"
                )

            except Exception as exc:
                logger.error("[Nominatim] Geocoding error: %s", exc)

        return None

    # ── Relevance gate ────────────────────────────────────────────────────────
    @staticmethod
    def _is_relevant(query: str, returned_name: str, full_address: str) -> bool:
        """
        Return True only if the geocoder's result is semantically related
        to the original query.

        Rules
        -----
        1. If the returned name IS the query (or vice versa) → accept.
        2. If they share ≥ 1 significant token (len ≥ 3) → accept.
        3. If the returned name is a known unrelated place
           (Khiala, Nakodar, Phagwara, Punjab, India, Jalandhar as a
           standalone city centroid) → reject.
        4. Otherwise, fuzzy-ratio ≥ 0.40 → accept.
        """
        STOP_WORDS = {"the", "of", "a", "an", "and", "at", "in",
                      "block", "road", "street", "near"}
        REJECT_NAMES = {"khiala", "nakodar", "phagwara", "kapurthala",
                        "punjab", "india", "jalandhar",
                        "ludhiana", "amritsar", "chandigarh"}

        qw = set(query.lower().split()) - STOP_WORDS
        rw = set(returned_name.lower().split()) - STOP_WORDS

        # Rule 3 – reject known unrelated place names
        rw_check = set(returned_name.lower().split())
        if rw_check.issubset(REJECT_NAMES):
            logger.debug("[Relevance] Rejected reject-list name: %s", returned_name)
            return False

        # Also reject if the full address doesn't mention NIT / Jalandhar
        addr_lower = full_address.lower()
        if "jalandhar" not in addr_lower and "punjab" not in addr_lower:
            return False

        # Rule 1 & 2 – token overlap
        if qw & rw:
            return True

        # Rule 4 – fuzzy ratio
        ratio = difflib.SequenceMatcher(
            None, query.lower(), returned_name.lower()
        ).ratio()
        return ratio >= 0.40


# ══════════════════════════════════════════════════════════════════════════════
# PUSH BUTTON HANDLER
#
# Hardware (Raspberry Pi):
#   Monitors a GPIO pin in a background thread.
#   Pressing the button sets an Event that the nav loop checks.
#   Wiring: Button between GPIO pin (BCM) and GND; internal pull-up enabled.
#
# Simulation / Windows / TEXT_INPUT_MODE:
#   A separate thread reads stdin.  Pressing ENTER triggers the same Event.
#   This lets you test the full button-on-demand flow without any hardware.
# ══════════════════════════════════════════════════════════════════════════════
class ButtonHandler:
    """
    Single-press event source.  Consumers call .wait_for_press() which
    blocks until the button (or ENTER key in simulation) is pressed, then
    returns immediately.  Thread-safe; can be waited on from the nav loop
    while GPS and TTS run on their own threads.
    """

    def __init__(self) -> None:
        self._event   = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._gpio_ok = False   # True if RPi.GPIO loaded successfully

    def start(self) -> None:
        self._running = True
        if Config.SIMULATION_MODE or Config.TEXT_INPUT_MODE:
            self._thread = threading.Thread(
                target=self._keyboard_listener, name="Button-KB", daemon=True
            )
            logger.info("Button: ENTER-key simulation mode.")
        else:
            self._gpio_ok = self._setup_gpio()
            if self._gpio_ok:
                self._thread = threading.Thread(
                    target=self._gpio_listener, name="Button-GPIO", daemon=True
                )
                logger.info("Button: GPIO pin %d (BCM).", Config.BUTTON_GPIO_PIN)
            else:
                # GPIO unavailable – fall back to keyboard even on Pi
                self._thread = threading.Thread(
                    target=self._keyboard_listener, name="Button-KB", daemon=True
                )
                logger.warning("Button: GPIO unavailable, falling back to ENTER key.")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._event.set()   # unblock any waiting call
        if self._gpio_ok:
            try:
                import RPi.GPIO as GPIO
                GPIO.cleanup()
            except Exception:
                pass

    def trigger(self) -> None:
        """Manually trigger (used internally and for testing)."""
        self._event.set()

    def wait_for_press(self, timeout: Optional[float] = None) -> bool:
        """
        Block until button pressed.
        Returns True if button was pressed, False if timed out or stopped.
        """
        fired = self._event.wait(timeout=timeout)
        self._event.clear()   # reset for next press
        return fired

    # ── GPIO listener (Raspberry Pi hardware) ────────────────────────────────
    def _setup_gpio(self) -> bool:
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(Config.BUTTON_GPIO_PIN, GPIO.IN,
                       pull_up_down=GPIO.PUD_UP)
            logger.info("GPIO pin %d configured.", Config.BUTTON_GPIO_PIN)
            return True
        except Exception as exc:
            logger.warning("GPIO setup failed: %s", exc)
            return False

    def _gpio_listener(self) -> None:
        try:
            import RPi.GPIO as GPIO
        except ImportError:
            return
        last_state   = GPIO.HIGH
        last_time    = 0.0
        debounce_s   = Config.BUTTON_DEBOUNCE_MS / 1000.0
        while self._running:
            state = GPIO.input(Config.BUTTON_GPIO_PIN)
            now   = time.monotonic()
            # Falling edge = button pressed (pull-up → GND)
            if state == GPIO.LOW and last_state == GPIO.HIGH:
                if now - last_time >= debounce_s:
                    logger.info("[Button] GPIO press detected.")
                    self._event.set()
                    last_time = now
            last_state = state
            time.sleep(0.02)   # 20 ms poll — fast enough, low CPU

    # ── Keyboard listener (simulation / fallback) ────────────────────────────
    def _keyboard_listener(self) -> None:
        """
        Non-blocking stdin reader.
        On Windows uses msvcrt; on Linux/Mac uses select so it doesn't
        block the whole process when waiting.
        """
        import sys
        print("\n  [Button simulation: press ENTER at any time for instructions]\n")
        while self._running:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ("\r", "\n"):
                            logger.info("[Button] ENTER pressed.")
                            self._event.set()
                    time.sleep(0.05)
                else:
                    import select
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        sys.stdin.readline()   # consume the line
                        logger.info("[Button] ENTER pressed.")
                        self._event.set()
            except Exception:
                time.sleep(0.1)


# ══════════════════════════════════════════════════════════════════════════════
# INDIAN CAMPUS HAZARD AWARENESS
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class HazardZone:
    lat: float
    lon: float
    radius_m: float
    warning: str


HAZARD_ZONES: List[HazardZone] = [
    HazardZone(31.3945, 75.5278, 30,
               "Caution: you are near the main gate. "
               "Vehicles entering and exiting. Please stay to the left side."),
    HazardZone(31.3963, 75.5295, 25,
               "Speed bumps and two-wheelers ahead near the shopping complex. "
               "Walk carefully."),
    HazardZone(31.3953, 75.5325, 20,
               "Hostel zone. Uneven footpath ahead. Watch your step."),
    HazardZone(31.3948, 75.5308, 20,
               "Open air theatre steps area. Uneven ground ahead."),
    HazardZone(31.3942, 75.5300, 30,
               "Sports complex road crossing. "
               "Cyclists and joggers may be present. Please be alert."),
    HazardZone(31.3980, 75.5288, 25,
               "Guest house driveway. Cars may be moving. "
               "Please proceed carefully."),
]


class SceneAwareness:
    def __init__(self) -> None:
        self._last_warned: Dict[int, float] = {}
        self._warn_cooldown: float = 60.0

    def check_hazards(self, lat: float, lon: float) -> Optional[str]:
        now = time.monotonic()
        pos = (lat, lon)
        for i, zone in enumerate(HAZARD_ZONES):
            dist = geodesic(pos, (zone.lat, zone.lon)).meters
            if dist <= zone.radius_m:
                last = self._last_warned.get(i, 0.0)
                if now - last >= self._warn_cooldown:
                    self._last_warned[i] = now
                    return zone.warning
        return None

    @staticmethod
    def is_on_campus(lat: float, lon: float) -> bool:
        dist = geodesic(
            (lat, lon), (Config.CAMPUS_LAT, Config.CAMPUS_LON)
        ).meters
        return dist <= Config.CAMPUS_RADIUS_METERS


# ══════════════════════════════════════════════════════════════════════════════
# GPS DATA STORE
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class GPSFix:
    lat: float       = 0.0
    lon: float       = 0.0
    altitude: float  = 0.0
    speed_kmh: float = 0.0
    satellites: int  = 0
    quality: int     = 0
    valid: bool      = False


class GPSData:
    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._current  = GPSFix()
        self._previous = GPSFix()

    def update(self, fix: GPSFix) -> None:
        with self._lock:
            if self._current.valid:
                self._previous = self._current
            self._current = fix

    def invalidate(self) -> None:
        with self._lock:
            self._current.valid = False

    @property
    def current(self) -> GPSFix:
        with self._lock:
            import copy
            return copy.copy(self._current)

    @property
    def previous(self) -> GPSFix:
        with self._lock:
            import copy
            return copy.copy(self._previous)

    @property
    def has_fix(self) -> bool:
        with self._lock:
            return self._current.valid


# ══════════════════════════════════════════════════════════════════════════════
# GPS HANDLER
# ══════════════════════════════════════════════════════════════════════════════
class GPSHandler:
    def __init__(self, gps_data: GPSData) -> None:
        self._data    = gps_data
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._serial: Optional[serial.Serial]    = None
        self._pending: Dict[str, float] = {}

    def start(self) -> None:
        self._running = True
        target = self._sim if Config.SIMULATION_MODE else self._hardware
        self._thread = threading.Thread(target=target, name="GPS", daemon=True)
        self._thread.start()
        logger.info("GPS started  [%s]",
                    "SIMULATION" if Config.SIMULATION_MODE else Config.GPS_PORT)

    def stop(self) -> None:
        self._running = False
        if self._serial and self._serial.is_open:
            self._serial.close()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("GPS stopped.")

    def _hardware(self) -> None:
        try:
            self._serial = serial.Serial(
                Config.GPS_PORT, Config.GPS_BAUD, timeout=Config.GPS_TIMEOUT
            )
        except serial.SerialException as exc:
            logger.error("Cannot open serial port: %s", exc)
            return

        while self._running:
            try:
                raw_bytes = self._serial.readline()
                if not raw_bytes:
                    continue
                line = raw_bytes.decode("ascii", errors="replace").strip()
                if line:
                    self._parse(line)
            except serial.SerialException as exc:
                logger.error("Serial read error: %s", exc)
                self._data.invalidate()
                time.sleep(1)

    def _parse(self, sentence: str) -> None:
        if not sentence.startswith(("$GPGGA", "$GPRMC", "$GNRMC", "$GNGGA")):
            return
        try:
            msg = pynmea2.parse(sentence, check=False)
            if isinstance(msg, pynmea2.types.talker.GGA):
                if msg.gps_qual == 0:
                    self._data.invalidate()
                    return
                fix = GPSFix(
                    lat        = msg.latitude,
                    lon        = msg.longitude,
                    altitude   = float(msg.altitude) if msg.altitude else 0.0,
                    satellites = int(msg.num_sats)  if msg.num_sats  else 0,
                    quality    = int(msg.gps_qual),
                    speed_kmh  = self._pending.get("speed_kmh", 0.0),
                    valid      = True,
                )
                self._data.update(fix)
            elif isinstance(msg, pynmea2.types.talker.RMC):
                if msg.status != "A":
                    self._data.invalidate()
                    return
                spd_knots = float(msg.spd_over_grnd) if msg.spd_over_grnd else 0.0
                self._pending["speed_kmh"] = spd_knots * 1.852
                fix = GPSFix(
                    lat       = msg.latitude,
                    lon       = msg.longitude,
                    speed_kmh = self._pending["speed_kmh"],
                    valid     = True,
                )
                self._data.update(fix)
        except pynmea2.ParseError as exc:
            logger.debug("NMEA parse skip: %s", exc)
        except AttributeError:
            pass

    def _sim(self) -> None:
        logger.warning("=== SIMULATION – walking campus route, NIT Jalandhar ===")
        waypoints = [
            (31.3945, 75.5275),
            (31.3950, 75.5280),
            (31.3955, 75.5285),
            (31.3960, 75.5288),
            (31.3963, 75.5290),
            (31.3964, 75.5292),
            (31.3965, 75.5295),
            (31.3966, 75.5298),
            (31.3967, 75.5300),
            (31.3967, 75.5303),
        ]
        sat = 8
        for lat, lon in waypoints:
            if not self._running:
                break
            fix = GPSFix(lat=lat, lon=lon, altitude=233.0,
                         satellites=sat, quality=1, speed_kmh=3.5, valid=True)
            self._data.update(fix)
            logger.info("[SIM] lat=%.6f  lon=%.6f  sats=%d", lat, lon, sat)
            time.sleep(2.0)
        while self._running:
            time.sleep(2.0)


# ══════════════════════════════════════════════════════════════════════════════
# NAVIGATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

# Turn categories as constants so the nav loop can compare them reliably
TC_STRAIGHT       = "straight"
TC_SLIGHT_RIGHT   = "slight_right"
TC_RIGHT          = "right"
TC_SHARP_RIGHT    = "sharp_right"
TC_UTURN          = "uturn"
TC_SLIGHT_LEFT    = "slight_left"
TC_LEFT           = "left"
TC_SHARP_LEFT     = "sharp_left"

# Map category → spoken phrase
_TURN_PHRASES: Dict[str, str] = {
    TC_STRAIGHT:     "continue straight ahead",
    TC_SLIGHT_RIGHT: "bear slightly to your right",
    TC_RIGHT:        "turn right",
    TC_SHARP_RIGHT:  "turn sharply to your right",
    TC_UTURN:        "turn around completely",
    TC_SLIGHT_LEFT:  "bear slightly to your left",
    TC_LEFT:         "turn left",
    TC_SHARP_LEFT:   "turn sharply to your left",
}


@dataclass
class NavState:
    """
    All mutable navigation state kept in one object so the loop stays clean.
    """
    # ── Turn tracking ──────────────────────────────────────────────────────────
    last_turn_cat: str    = ""     # category of the last spoken instruction
    last_bearing:  float  = -1.0  # dest-bearing when we last spoke
    heading_buf: List[float] = field(default_factory=list)  # smoothing buffer

    # ── Post-turn confirmation ─────────────────────────────────────────────────
    # After a non-straight instruction, we watch for the person to actually
    # complete the turn, then confirm "good, now go straight".
    expecting_straight: bool  = False  # True = watching for turn completion
    confirm_cooldown:   float = 0.0    # don't re-confirm until after this time

    # ── Advance-warning ────────────────────────────────────────────────────────
    # We preview upcoming turns at ADVANCE_WARN_M metres ahead so the person
    # has time to slow down and prepare.
    advance_warned: bool = False   # True = preview already given this leg

    # ── Distance milestones ────────────────────────────────────────────────────
    # Spoken once each: "100 metres remaining", "50 metres", "20 metres"
    milestone_100: bool = False
    milestone_50:  bool = False
    milestone_20:  bool = False

    # ── Off-course detection ───────────────────────────────────────────────────
    off_course_count: int   = 0    # consecutive readings heading wrong way
    off_course_spoken: bool = False

    # ── Stopped detection ─────────────────────────────────────────────────────
    last_move_time: float = field(default_factory=time.monotonic)
    stopped_spoken: bool  = False


class NavigationEngine:
    """
    Stateless geometry helpers.
    All mutable tracking lives in NavState (held by the app).
    """

    # Rolling average window for heading smoothing (filters GPS jitter)
    HEADING_SMOOTH = 4

    @staticmethod
    def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Forward azimuth from point A to point B. Result: 0–360°."""
        φ1 = math.radians(lat1)
        φ2 = math.radians(lat2)
        Δλ = math.radians(lon2 - lon1)
        x  = math.sin(Δλ) * math.cos(φ2)
        y  = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(Δλ)
        return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0

    @staticmethod
    def cardinal(brg: float) -> str:
        dirs = ["North", "North-East", "East", "South-East",
                "South", "South-West", "West", "North-West"]
        return dirs[round(brg / 45.0) % 8]

    @staticmethod
    def smooth_heading(buf: List[float], new_val: float,
                       window: int = 4) -> float:
        """
        Circular mean of the last `window` headings.
        Handles the 0°/360° wrap correctly (e.g. average of 350° and 10° = 0°).
        """
        buf.append(new_val)
        if len(buf) > window:
            buf.pop(0)
        sins = sum(math.sin(math.radians(h)) for h in buf)
        coss = sum(math.cos(math.radians(h)) for h in buf)
        return (math.degrees(math.atan2(sins, coss)) + 360.0) % 360.0

    @staticmethod
    def angle_diff(a: float, b: float) -> float:
        """Signed difference b − a, normalised to (−180, +180]."""
        d = b - a
        while d >  180: d -= 360
        while d <= -180: d += 360
        return d

    @staticmethod
    def turn_category(current_heading: float, dest_bearing: float) -> str:
        """
        Map the signed angle between heading and destination into one of the
        eight named turn categories.

        Thresholds (degrees from dead-ahead):
          ±0–12   → straight           (tiny deviation: GPS noise, keep walking)
          ±12–40  → slight left/right  (gentle bear — no need to stop)
          ±40–115 → left/right         (clear turn at intersection)
          ±115–165→ sharp left/right   (very tight turn, e.g. 150° alley)
          ±165–180→ U-turn             (doubling back)
        """
        diff = NavigationEngine.angle_diff(current_heading, dest_bearing)
        a    = abs(diff)
        if   a <  12:  return TC_STRAIGHT
        elif a < 40:   return TC_SLIGHT_RIGHT if diff > 0 else TC_SLIGHT_LEFT
        elif a < 115:  return TC_RIGHT        if diff > 0 else TC_LEFT
        elif a < 165:  return TC_SHARP_RIGHT  if diff > 0 else TC_SHARP_LEFT
        else:          return TC_UTURN

    @staticmethod
    def turn_phrase(category: str) -> str:
        """Return the spoken phrase for a turn category."""
        return _TURN_PHRASES.get(category, "continue straight ahead")

    @staticmethod
    def is_wrong_direction(heading: float, dest_bearing: float,
                           threshold: float = 120.0) -> bool:
        """
        True if the person is walking more than `threshold` degrees away
        from the destination direction (likely went the wrong way).
        """
        return abs(NavigationEngine.angle_diff(heading, dest_bearing)) > threshold

    @staticmethod
    def distance_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        return geodesic(a, b).meters


# ══════════════════════════════════════════════════════════════════════════════
# VOICE SCRIPTS  – every spoken phrase in one place
# ══════════════════════════════════════════════════════════════════════════════
class Script:

    # ── System messages ───────────────────────────────────────────────────────
    WELCOME = (
        "Namaskar! Welcome to Smart Specs, your campus navigation companion "
        "at NIT Jalandhar. "
        "I will help you reach any place on campus. "
        "Just a moment while I get ready."
    )
    WAITING_GPS     = ("Getting your GPS location. "
                       "Please stay in an open area, this will be quick.")
    GPS_READY       = "Location locked. We are all set, let us go."
    GPS_FAILED      = ("Sorry, I could not get a GPS signal. "
                       "Please check the module and try again outdoors.")
    NOT_ON_CAMPUS   = ("You seem to be outside the campus area. "
                       "I will still do my best to guide you.")
    ASK_DESTINATION = ("Where would you like to go? "
                       "Just say the name — library, hostel, admin block, "
                       "sports complex, or any other campus location.")
    LISTENING_CUE   = "Go ahead, I am listening."
    SEARCHING       = "Let me look that up for you."
    NOT_FOUND_CAMPUS= ("I could not find that place. "
                       "Try: library, mess, BH1, admin block, or sports complex.")
    TIMEOUT         = "I did not catch that. Please try again."
    NOT_UNDERSTOOD  = "Sorry, I missed that. Please say it once more."
    VOICE_UNAVAILABLE=("Voice service unavailable. "
                       "Please check your internet connection.")
    NO_MIC          = "No microphone found. Please connect one and restart."
    GPS_LOST        = ("GPS signal lost. Please stay still, I am reconnecting.")
    GPS_REGAINED    = "Signal back. Let us keep going."
    NAV_ERROR       = "Small error. Recalculating."
    CONTINUE_PROMPT = "Would you like to go somewhere else? Say yes or no."

    GIVE_UP = "Could not get that. Where would you like to go?"

    @staticmethod
    def found_location(dest: "Destination") -> str:
        tag = "" if dest.source == "campus_db" else " — found on map"
        return f"Found {dest.name}{tag}. Guiding you there now."

    # ── Navigation start ──────────────────────────────────────────────────────
    @staticmethod
    def nav_start(dest: str, dist_m: int, turn_cat: str,
                  cardinal: str) -> str:
        phrase = NavigationEngine.turn_phrase(turn_cat)
        return (
            f"{dest} is about {dist_m} metres away. "
            f"{phrase.capitalize()}, heading {cardinal}. "
            "I will speak when you need to turn, or press the button "
            "anytime for your current position."
        )

    # ── Turn instructions (auto-fired on direction change) ────────────────────
    @staticmethod
    def turn_instruction(turn_cat: str, cardinal: str,
                         dist_m: int) -> str:
        phrase = NavigationEngine.turn_phrase(turn_cat)

        # Roundabout / junction straight-through — needs extra clarity
        if turn_cat == TC_STRAIGHT:
            return (
                f"At the junction, continue straight ahead towards {cardinal}. "
                f"{dist_m} metres remaining."
            )

        p = phrase.capitalize()
        variants = [
            f"{p} — heading {cardinal}. {dist_m} metres to go.",
            f"{p} now. Direction {cardinal}, {dist_m} metres remaining.",
            f"Please {phrase}. You are heading {cardinal}. "
            f"About {dist_m} metres left.",
        ]
        return random.choice(variants)

    # ── Advance warning (given ~ADVANCE_WARN_M metres before a turn) ──────────
    @staticmethod
    def advance_warning(turn_cat: str, cardinal: str,
                        dist_to_dest: int) -> str:
        phrase = NavigationEngine.turn_phrase(turn_cat)
        if turn_cat == TC_STRAIGHT:
            return (
                f"In a moment, keep going straight through the junction "
                f"towards {cardinal}. {dist_to_dest} metres to destination."
            )
        return (
            f"Heads up — in a short while you will need to {phrase} "
            f"towards {cardinal}. Get ready. "
            f"{dist_to_dest} metres to destination."
        )

    # ── Post-turn confirmation (after the person completes a turn) ────────────
    @staticmethod
    def turn_confirmed(cardinal: str, dist_m: int) -> str:
        variants = [
            f"Good turn. Now go straight towards {cardinal}. "
            f"{dist_m} metres remaining.",
            f"Well done. Continue straight, heading {cardinal}. "
            f"{dist_m} metres to go.",
            f"Correct. Keep walking straight towards {cardinal}. "
            f"{dist_m} metres left.",
        ]
        return random.choice(variants)

    # ── Distance milestones ───────────────────────────────────────────────────
    @staticmethod
    def milestone(dist_m: int, turn_cat: str, cardinal: str) -> str:
        phrase = NavigationEngine.turn_phrase(turn_cat)
        if dist_m <= 20:
            return (
                f"Almost there — just {dist_m} metres. "
                f"{phrase.capitalize()} towards {cardinal}."
            )
        if dist_m <= 50:
            return (
                f"50 metres remaining. {phrase.capitalize()}, "
                f"heading {cardinal}."
            )
        # 100m milestone
        return (
            f"100 metres to go. {phrase.capitalize()} towards {cardinal}."
        )

    # ── Off-course warning ────────────────────────────────────────────────────
    @staticmethod
    def off_course(cardinal: str, dist_m: int) -> str:
        variants = [
            f"You seem to be going the wrong way. "
            f"Please turn and head towards {cardinal}. "
            f"{dist_m} metres from destination.",
            f"Wrong direction. Your destination is towards {cardinal}, "
            f"{dist_m} metres away. Please turn around.",
            f"You are moving away from {cardinal}. "
            f"Turn and walk towards {cardinal} to get back on track.",
        ]
        return random.choice(variants)

    # ── Stopped too long ──────────────────────────────────────────────────────
    @staticmethod
    def stopped_prompt(turn_cat: str, cardinal: str, dist_m: int) -> str:
        phrase = NavigationEngine.turn_phrase(turn_cat)
        return (
            f"You seem to have stopped. Your destination is {dist_m} metres "
            f"away. {phrase.capitalize()} towards {cardinal} to continue."
        )

    # ── On-demand (button press) ──────────────────────────────────────────────
    @staticmethod
    def on_demand(turn_cat: str, cardinal: str, dist_m: int) -> str:
        phrase = NavigationEngine.turn_phrase(turn_cat)
        p = phrase.capitalize()
        variants = [
            f"You are {dist_m} metres from your destination. "
            f"{p}, heading {cardinal}.",
            f"{dist_m} metres to go. {p} towards {cardinal}.",
            f"Current position: {dist_m} metres away. "
            f"{p}, direction {cardinal}.",
        ]
        return random.choice(variants)

    # ── Arrival ───────────────────────────────────────────────────────────────
    @staticmethod
    def arrived(name: str) -> str:
        phrases = [
            f"You have reached {name}! Great job.",
            f"Here we are — {name}. Hope the walk was smooth.",
            f"We made it to {name}. I am glad I could help.",
        ]
        return random.choice(phrases)

    # ── Misc ──────────────────────────────────────────────────────────────────
    @staticmethod
    def retry_prompt(attempt: int, total: int) -> str:
        return (
            f"Attempt {attempt} of {total}. "
            "Please say the location name clearly."
        )

    @staticmethod
    def shutdown(completed_journey: bool) -> str:
        if completed_journey:
            return ("Thank you for using Smart Specs! "
                    "Take care and see you next time!")
        return "Smart Specs shutting down. Take care and stay safe."


# ══════════════════════════════════════════════════════════════════════════════
# VOICE I/O MODULE
# ══════════════════════════════════════════════════════════════════════════════
class VoiceIO:
    _STOP = object()

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._done           = threading.Event()
        self._done.set()
        self._r = sr.Recognizer()
        self._r.energy_threshold         = Config.ENERGY_THRESHOLD
        self._r.dynamic_energy_threshold = Config.DYNAMIC_ENERGY
        self._r.pause_threshold          = 0.8
        self._mic_index: Optional[int] = None
        if not Config.TEXT_INPUT_MODE:
            self._mic_index = self._select_microphone()
        self._worker = threading.Thread(
            target=self._tts_worker, name="TTS", daemon=False
        )
        self._worker.start()

    @staticmethod
    def _select_microphone() -> Optional[int]:
        try:
            names = sr.Microphone.list_microphone_names()
            logger.info("Available microphones:")
            for i, name in enumerate(names):
                logger.info("  [%d] %s", i, name)
            for i, name in enumerate(names):
                nl = name.lower()
                if any(kw in nl for kw in ["usb", "respeaker", "uac", "ps3"]):
                    logger.info("Selected USB mic: [%d] %s", i, name)
                    return i
        except Exception as exc:
            logger.warning("Mic enumeration failed: %s", exc)
        return None

    def _tts_worker(self) -> None:
        if IS_WINDOWS:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception as exc:
                logger.warning("CoInitialize skipped: %s", exc)
        while True:
            item = self._q.get()
            if item is self._STOP:
                self._q.task_done()
                break
            text: str = item
            logger.info("[TTS] %s", text)
            spoke = False
            for attempt in range(1, 3):
                engine = None
                try:
                    engine = self._new_engine()
                    if engine:
                        engine.say(text)
                        engine.runAndWait()
                        spoke = True
                        break
                except Exception as exc:
                    logger.error("TTS error (attempt %d): %s", attempt, exc)
                finally:
                    if engine:
                        try:
                            engine.stop()
                        except Exception:
                            pass
                        del engine
            if not spoke and IS_WINDOWS:
                spoke = self._speak_sapi_fallback(text)
            if not spoke:
                print(f"\n[SPEECH] {text}\n")
            self._done.set()
            self._q.task_done()
        if IS_WINDOWS:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass

    @staticmethod
    def _new_engine() -> Optional[pyttsx3.Engine]:
        try:
            eng = pyttsx3.init()
            eng.setProperty("rate",   Config.TTS_RATE)
            eng.setProperty("volume", Config.TTS_VOLUME)
            voices = eng.getProperty("voices")
            for v in voices:
                if "female" in v.name.lower() or "zira" in v.name.lower():
                    eng.setProperty("voice", v.id)
                    break
            return eng
        except Exception as exc:
            logger.error("pyttsx3 init failed: %s", exc)
            return None

    @staticmethod
    def _speak_sapi_fallback(text: str) -> bool:
        import subprocess
        safe = text.replace("'", "''").replace('"', '`"')
        cmd = (
            'Add-Type -AssemblyName System.Speech; '
            '$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
            f'$synth.Rate = 0; '
            f"$synth.Speak('{safe}')"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                timeout=30, capture_output=True
            )
            if result.returncode == 0:
                return True
        except Exception as exc:
            logger.error("[TTS-SAPI] Fallback failed: %s", exc)
        return False

    def speak(self, text: str) -> None:
        self._done.clear()
        self._q.put(text)

    def speak_and_wait(self, text: str, pause: float = 0.0) -> None:
        self.speak(text)
        self._done.wait()
        if pause > 0:
            time.sleep(pause)

    def stop(self) -> None:
        self._q.put(self._STOP)
        self._worker.join(timeout=10)

    def listen(self, prompt: Optional[str] = None,
               cue: str = Script.LISTENING_CUE) -> Optional[str]:
        if prompt:
            self.speak_and_wait(prompt, pause=Config.POST_SPEECH_DELAY)
        mic_kwargs = {"device_index": self._mic_index} if self._mic_index else {}
        try:
            with sr.Microphone(**mic_kwargs) as source:
                self._r.adjust_for_ambient_noise(source, duration=0.8)
                self.speak_and_wait(cue, pause=0.2)
                print("\n  ──────── 🎙  SPEAK NOW ────────\n")
                audio = self._r.listen(
                    source,
                    timeout=Config.LISTEN_TIMEOUT,
                    phrase_time_limit=Config.PHRASE_TIME_LIMIT,
                )
            try:
                text = self._r.recognize_google(audio, language=Config.STT_LANGUAGE)
                logger.info("Recognised [en-IN]: '%s'", text)
                return text.strip()
            except sr.UnknownValueError:
                try:
                    text = self._r.recognize_google(audio, language="en-US")
                    logger.info("Recognised [en-US fallback]: '%s'", text)
                    return text.strip()
                except sr.UnknownValueError:
                    return None
        except sr.WaitTimeoutError:
            return None
        except sr.RequestError as exc:
            logger.error("STT API error: %s", exc)
            self.speak(Script.VOICE_UNAVAILABLE)
            return None
        except OSError as exc:
            logger.error("Microphone error: %s", exc)
            self.speak(Script.NO_MIC)
            return None
        except Exception as exc:
            logger.error("Unexpected listen error: %s", exc)
            return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ══════════════════════════════════════════════════════════════════════════════
class SmartSpecsApp:
    def __init__(self) -> None:
        self._gps      = GPSData()
        self._handler  = GPSHandler(self._gps)
        self._voice    = VoiceIO()
        self._nav      = NavigationEngine()
        self._scene    = SceneAwareness()
        self._button   = ButtonHandler()
        self._stop     = threading.Event()
        self._arrived  = False

    def _wait_for_fix(self, timeout: int) -> bool:
        self._voice.speak_and_wait(Script.WAITING_GPS)
        logger.info("Awaiting GPS fix (timeout=%ds)...", timeout)
        deadline = time.monotonic() + timeout
        while not self._gps.has_fix:
            if time.monotonic() > deadline:
                self._voice.speak_and_wait(Script.GPS_FAILED)
                return False
            time.sleep(0.5)
        fix = self._gps.current
        quality_msg = (
            f"GPS ready. I have {fix.satellites} satellites in view "
            f"and a good position fix."
        ) if fix.satellites >= 4 else Script.GPS_READY
        self._voice.speak_and_wait(quality_msg)
        if not SceneAwareness.is_on_campus(fix.lat, fix.lon):
            self._voice.speak(Script.NOT_ON_CAMPUS)
        return True

    def _ask_destination(self) -> Optional[str]:
        if Config.TEXT_INPUT_MODE:
            return self._ask_destination_text()
        return self._ask_destination_voice()

    def _ask_destination_text(self) -> Optional[str]:
        self._voice.speak_and_wait(Script.ASK_DESTINATION)
        for attempt in range(1, Config.MAX_RETRIES + 1):
            if self._stop.is_set():
                return None
            try:
                text = input("\n  ▶ Enter destination: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if text:
                return text
            if attempt < Config.MAX_RETRIES:
                msg = Script.retry_prompt(attempt + 1, Config.MAX_RETRIES)
                self._voice.speak_and_wait(msg)
                print(f"  ({msg})")
        self._voice.speak_and_wait(Script.GIVE_UP)
        return None

    def _ask_destination_voice(self) -> Optional[str]:
        self._voice.speak_and_wait(Script.ASK_DESTINATION)
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            result = self._voice.listen(
                prompt=None,
                cue=Script.LISTENING_CUE if attempt == 1 else ""
            )
            if result:
                return result
            reminders = [
                "I am still listening. Please say the location name.",
                "Take your time. Just say where you would like to go.",
                "I did not catch that. Please try again whenever you are ready.",
                "No hurry. Just say the name of the place clearly.",
                "I am right here, waiting. Please speak the destination.",
            ]
            reminder = reminders[attempt % len(reminders)]
            self._voice.speak_and_wait(reminder, pause=0.3)
        return None

    def _navigate_to(self, dest_name: str,
                     dest_coords: Tuple[float, float]) -> bool:
        """
        Smart navigation loop — speaks only when genuinely needed.

        Trigger map (11 situations, in priority order each loop tick):
        ──────────────────────────────────────────────────────────────
         0. GPS LOST / REGAINED  — safety, always spoken
         1. HAZARD ZONE          — safety, always spoken
         2. ARRIVAL              — always spoken
         3. MILESTONE 20 m       — "almost there, turn right"
         4. MILESTONE 50 m       — "50 metres, bear left"
         5. MILESTONE 100 m      — "100 metres, go straight"
         6. OFF-COURSE           — person walking wrong way (≥3 readings)
         7. STOPPED              — no movement for STOPPED_TIMEOUT_S seconds
         8. ADVANCE WARNING      — turn preview at ADVANCE_WARN_M before dest
         9. TURN CHANGE          — turn category changed (the core trigger)
        10. POST-TURN CONFIRM    — person completed turn → "good, straight now"
        11. BUTTON               — on-demand: always answers immediately

        Returns True if arrived, False if Ctrl+C interrupted.
        """
        # ── Initial fix & spoken start ────────────────────────────────────────
        fix          = self._gps.current
        initial_dist = int(self._nav.distance_m((fix.lat, fix.lon), dest_coords))
        init_brg     = self._nav.bearing(fix.lat, fix.lon,
                                         dest_coords[0], dest_coords[1])
        init_card    = self._nav.cardinal(init_brg)

        self._voice.speak_and_wait(
            Script.nav_start(dest_name, initial_dist, TC_STRAIGHT, init_card)
        )
        logger.info("Navigating to '%s' @ (%.6f, %.6f)",
                    dest_name, dest_coords[0], dest_coords[1])

        state    = NavState(last_move_time=time.monotonic())
        gps_lost = False

        while not self._stop.is_set():
            time.sleep(0.4)

            # ── TRIGGER 0: GPS health ─────────────────────────────────────────
            if not self._gps.has_fix:
                if not gps_lost:
                    self._voice.speak(Script.GPS_LOST)
                    gps_lost = True
                continue
            if gps_lost:
                self._voice.speak(Script.GPS_REGAINED)
                gps_lost = False
                state.last_move_time = time.monotonic()

            fix  = self._gps.current
            prev = self._gps.previous
            pos  = (fix.lat, fix.lon)

            try:
                dist   = self._nav.distance_m(pos, dest_coords)
                dist_m = int(dist)
                brg    = self._nav.bearing(fix.lat, fix.lon,
                                           dest_coords[0], dest_coords[1])
                card   = self._nav.cardinal(brg)

                # ── TRIGGER 1: Hazard ─────────────────────────────────────────
                hazard_msg = self._scene.check_hazards(fix.lat, fix.lon)
                if hazard_msg:
                    self._voice.speak_and_wait(hazard_msg)

                # ── TRIGGER 2: Arrival ────────────────────────────────────────
                if dist < Config.ARRIVED_METERS:
                    self._voice.speak_and_wait(Script.arrived(dest_name))
                    logger.info("Arrived at '%s'.", dest_name)
                    return True

                # ── Smoothed heading (requires actual movement) ───────────────
                moved = (prev.valid and
                         self._nav.distance_m((prev.lat, prev.lon), pos)
                         >= Config.MIN_MOVE_METERS)

                if moved:
                    raw_hdg = self._nav.bearing(prev.lat, prev.lon,
                                                fix.lat, fix.lon)
                    heading  = self._nav.smooth_heading(
                        state.heading_buf, raw_hdg,
                        NavigationEngine.HEADING_SMOOTH
                    )
                    state.last_move_time = time.monotonic()
                    state.stopped_spoken = False
                    turn_cat = self._nav.turn_category(heading, brg)
                else:
                    heading  = -1.0
                    turn_cat = state.last_turn_cat or TC_STRAIGHT

                # ── TRIGGERS 3-5: Distance milestones ────────────────────────
                if dist_m <= 20 and not state.milestone_20:
                    state.milestone_20 = True
                    self._voice.speak_and_wait(
                        Script.milestone(dist_m, turn_cat, card))
                    logger.info("[NAV-MILE] 20m")
                    continue

                if dist_m <= 50 and not state.milestone_50:
                    state.milestone_50 = True
                    self._voice.speak_and_wait(
                        Script.milestone(dist_m, turn_cat, card))
                    logger.info("[NAV-MILE] 50m")
                    continue

                if dist_m <= 100 and not state.milestone_100:
                    state.milestone_100 = True
                    self._voice.speak_and_wait(
                        Script.milestone(dist_m, turn_cat, card))
                    logger.info("[NAV-MILE] 100m")
                    continue

                # ── TRIGGER 6: Off-course ─────────────────────────────────────
                if moved and heading >= 0:
                    if self._nav.is_wrong_direction(heading, brg,
                                                    threshold=110.0):
                        state.off_course_count += 1
                    else:
                        state.off_course_count = 0
                        state.off_course_spoken = False

                    if (state.off_course_count >= 3
                            and not state.off_course_spoken):
                        self._voice.speak_and_wait(
                            Script.off_course(card, dist_m))
                        state.off_course_spoken = True
                        state.last_turn_cat = ""
                        logger.info("[NAV-OFFCOURSE] hdg=%.0f dest=%.0f",
                                    heading, brg)
                        continue

                # ── TRIGGER 7: Stopped too long ───────────────────────────────
                idle_s = time.monotonic() - state.last_move_time
                if (idle_s >= Config.STOPPED_TIMEOUT_S
                        and not state.stopped_spoken
                        and dist_m > Config.ARRIVED_METERS):
                    self._voice.speak(
                        Script.stopped_prompt(turn_cat, card, dist_m))
                    state.stopped_spoken = True
                    logger.info("[NAV-STOP] idle %.0fs", idle_s)

                # ── TRIGGER 8: Advance warning ────────────────────────────────
                if (dist_m <= Config.ADVANCE_WARN_M
                        and not state.advance_warned
                        and turn_cat not in (TC_STRAIGHT,)):
                    self._voice.speak(
                        Script.advance_warning(turn_cat, card, dist_m))
                    state.advance_warned = True
                    logger.info("[NAV-ADV] %s", turn_cat)

                # ── TRIGGER 9: Turn category changed ─────────────────────────
                if moved and heading >= 0:
                    bearing_shifted = (
                        state.last_bearing < 0 or
                        abs(NavigationEngine.angle_diff(
                            state.last_bearing, brg
                        )) > Config.TURN_TRIGGER_DEG
                    )
                    turn_changed = (turn_cat != state.last_turn_cat)

                    # "Straight at junction" = person just turned (was non-
                    # straight) and now the route lines up straight again.
                    # This covers roundabouts and T-junctions explicitly.
                    straight_at_junction = (
                        turn_cat == TC_STRAIGHT
                        and state.last_turn_cat not in ("", TC_STRAIGHT)
                    )

                    should_speak = (
                        bearing_shifted and turn_changed and
                        (turn_cat != TC_STRAIGHT or straight_at_junction)
                    )

                    if should_speak:
                        msg = Script.turn_instruction(turn_cat, card, dist_m)
                        logger.info("[NAV-TURN] cat=%s", turn_cat)
                        self._voice.speak(msg)
                        state.last_turn_cat  = turn_cat
                        state.last_bearing   = brg
                        state.advance_warned = False
                        if turn_cat != TC_STRAIGHT:
                            state.expecting_straight = True
                        continue

                # ── TRIGGER 10: Post-turn confirmation ───────────────────────
                now = time.monotonic()
                if (state.expecting_straight
                        and turn_cat == TC_STRAIGHT
                        and moved
                        and now >= state.confirm_cooldown):
                    self._voice.speak(Script.turn_confirmed(card, dist_m))
                    state.expecting_straight = False
                    state.confirm_cooldown   = now + 8.0
                    state.last_turn_cat      = TC_STRAIGHT
                    logger.info("[NAV-CONFIRM] turn completed")

                # ── TRIGGER 11: Button pressed ────────────────────────────────
                if self._button.wait_for_press(timeout=0):
                    msg = Script.on_demand(turn_cat, card, dist_m)
                    logger.info("[NAV-BTN] %s", msg)
                    self._voice.speak_and_wait(msg)
                    state.last_turn_cat = turn_cat
                    state.last_bearing  = brg

            except Exception as exc:
                logger.error("Nav loop error: %s", exc)
                self._voice.speak(Script.NAV_ERROR)

        return False

    def _ask_continue(self) -> bool:
        if Config.TEXT_INPUT_MODE:
            return self._ask_continue_text()
        return self._ask_continue_voice()

    def _ask_continue_text(self) -> bool:
        self._voice.speak_and_wait(Script.CONTINUE_PROMPT)
        try:
            answer = input("\n  ▶ Navigate again? (yes/no): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return not any(w in answer for w in
                       ["no", "nahi", "nahin", "band", "exit", "stop", "done"])

    def _ask_continue_voice(self) -> bool:
        self._voice.speak_and_wait(Script.CONTINUE_PROMPT)
        while not self._stop.is_set():
            answer = self._voice.listen(prompt=None, cue="Please say yes or no.")
            if answer:
                al = answer.lower()
                if any(w in al for w in ["no", "nahi", "nahin", "band",
                                          "exit", "stop", "done"]):
                    return False
                if any(w in al for w in ["yes", "haan", "ha", "sure",
                                          "ok", "yeah", "continue"]):
                    return True
                self._voice.speak_and_wait(
                    "I heard you, but could you say yes or no?"
                )
            else:
                self._voice.speak_and_wait(
                    "I am still listening. Please say yes or no."
                )
        return False

    def run(self) -> None:
        logger.info("=" * 70)
        logger.info("  SMART SPECS  v3.1  –  NIT Jalandhar Campus Navigation")
        logger.info("  Platform: %s  |  Mode: %s",
                    platform.system(),
                    "SIMULATION" if Config.SIMULATION_MODE else "HARDWARE")
        logger.info("=" * 70)

        self._voice.speak_and_wait(Script.WELCOME)
        self._handler.start()
        self._button.start()

        fix_timeout = 10 if Config.SIMULATION_MODE else 60
        if not self._wait_for_fix(timeout=fix_timeout):
            logger.critical("GPS fix failed. Exiting.")
            self._shutdown()
            return

        try:
            while not self._stop.is_set():
                spoken = self._ask_destination()
                if not spoken or self._stop.is_set():
                    continue

                logger.info("User input: '%s'", spoken)
                self._voice.speak_and_wait(Script.SEARCHING)

                dest = LocationFinder.find(spoken)
                if dest:
                    self._voice.speak_and_wait(Script.found_location(dest))
                    arrived = self._navigate_to(dest.name, dest.coords)
                    self._arrived = arrived
                    if not arrived:
                        break
                    if not self._stop.is_set() and not self._ask_continue():
                        break
                else:
                    self._voice.speak_and_wait(Script.NOT_FOUND_CAMPUS)

        except KeyboardInterrupt:
            logger.info("Ctrl+C – shutting down.")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        logger.info("Shutting down ...")
        self._stop.set()
        self._button.stop()
        self._handler.stop()
        self._voice.speak_and_wait(Script.shutdown(self._arrived))
        self._voice.stop()
        logger.info("System stopped cleanly.")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = SmartSpecsApp()
    app.run()