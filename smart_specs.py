#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         SMART SPECS – Campus Navigation System for Visually Impaired        ║
║         Dr. B.R. Ambedkar National Institute of Technology, Jalandhar        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Version     : 3.3  (dual-mode: simple + Raspberry Pi)                      ║
║  Platform    : Raspberry Pi (production)  |  Windows/Linux (simulation)      ║
║  Python      : 3.8+                                                          ║
║  Hardware    : Pixhawk u-blox M8N GPS (raspi) | simulated (simple)           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  WHAT'S IN THIS VERSION (v3.3)                                               ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  NEW — Dual-mode operation (simple vs Raspberry Pi)                          ║
║    • No flag  → simple/simulation mode (desktop testing)                     ║
║    • -raspi   → Raspberry Pi mode with Pixhawk u-blox M8N GPS               ║
║    • --gps-port / --gps-baud to override serial settings                     ║
║    • u-blox M8N: GPS+GLONASS, $GN* NMEA, VTG/GLL sentence support           ║
║    • Default port: /dev/ttyACM0 (USB), override with --gps-port              ║
║                                                                              ║
║  v3.2 — Dynamic coordinate fetching (no hardcoded lat/lon)                   ║
║    • User types/speaks destination → coords fetched automatically             ║
║    • OSM Overpass API (campus bbox hard-lock, Khiala impossible)              ║
║    • Google Places Text Search NEW API (locationRestriction, not hints)       ║
║    • Nominatim fallback with distance validation                              ║
║    • JSON cache (smart_specs_coords.json) — offline after first lookup        ║
║    • Smart instructions (no more time-based spam)                            ║
║    • Push button support (GPIO on Pi, ENTER key on desktop)                  ║
║    • Input preprocessing (Hindi noise removal, alias normalization)           ║
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
║                                                                              ║
║  Usage:                                                                      ║
║   python smart_specs.py              # simple/simulation mode                ║
║   python smart_specs.py -raspi       # Raspberry Pi + u-blox M8N GPS         ║
║   python smart_specs.py -raspi --gps-port /dev/serial0   # custom port       ║
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


def _require(mod: str, pkg: str, optional: bool = False) -> bool:
    """Import-check helper.  optional=True → warn but don't exit."""
    try:
        __import__(mod)
        return True
    except ImportError:
        if optional:
            logger.warning(
                "Optional package '%s' not installed (pip install %s). "
                "Some features disabled.", mod, pkg
            )
            return False
        logger.critical("Missing package '%s'. Run:  pip install %s", mod, pkg)
        sys.exit(1)

# ── Always required (pure-Python, no hardware) ────────────────────────────────
_require("pyttsx3",  "pyttsx3")
_require("geopy",    "geopy")
_require("requests", "requests")

# ── Optional: only needed in TEXT_INPUT_MODE=False (microphone) ───────────────
_HAS_SR  = _require("speech_recognition", "speechrecognition pyaudio",
                     optional=True)

# ── Optional: only needed in SIMULATION_MODE=False (real GPS hardware) ────────
_HAS_SERIAL = _require("serial",   "pyserial", optional=True)
_HAS_NMEA   = _require("pynmea2",  "pynmea2",  optional=True)

# ── Optional: Google Maps SDK (fallback to REST Places API if absent) ──────────
_HAS_GMAPS  = _require("googlemaps", "googlemaps", optional=True)

# ── Conditional imports ───────────────────────────────────────────────────────
import pyttsx3
import requests
from geopy.geocoders import Nominatim
from geopy.distance  import geodesic

if _HAS_SR:
    import speech_recognition as sr
else:
    sr = None  # type: ignore

if _HAS_SERIAL:
    import serial
else:
    # Stub so GPSHandler hardware path fails gracefully
    class serial:                          # type: ignore
        class Serial:
            def __init__(self, *a, **k): raise OSError("pyserial not installed")
        class SerialException(OSError): pass

if _HAS_NMEA:
    import pynmea2
else:
    pynmea2 = None  # type: ignore  # only used in hardware GPS path

if _HAS_GMAPS:
    import googlemaps
else:
    googlemaps = None  # type: ignore

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
    SIMULATION_MODE: bool = True   # False = real GPS hardware (set by -raspi flag)

    # Input is ALWAYS voice — user speaks the destination name.
    # The system does: listen → STT → confirm → fetch coords → navigate.
    # No keyboard/console input anywhere in the navigation flow.

    # ── GPS / Serial ──────────────────────────────────────────────────────────
    # Raspi mode uses Pixhawk u-blox M8N (USB → /dev/ttyACM0, UART → /dev/serial0)
    # Simple mode ignores these (simulation generates fake fixes).
    GPS_PORT: str    = "/dev/ttyACM0"
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
    LISTEN_TIMEOUT: int       = 10    # wait up to 10s for speech to start
    PHRASE_TIME_LIMIT: int    = 15    # up to 15s for a full phrase
    # Energy threshold: set dynamically per session via auto-calibration.
    # This is only the starting value — overwritten on first listen.
    ENERGY_THRESHOLD: int     = 400
    DYNAMIC_ENERGY: bool      = True
    # Pause threshold: how long of silence = end of phrase.
    # 1.2s prevents cutting off "Boys Hostel One" or "Swimming Pool"
    PAUSE_THRESHOLD: float    = 1.2
    # Non-speaking duration: minimum silence before phrase start counts
    NON_SPEAKING_DURATION: float = 0.5
    # Ambient noise calibration: longer = more accurate on Pi USB mics
    NOISE_CALIB_DURATION: float  = 2.0
    # How many times to retry STT before giving up on one utterance
    STT_MAX_ATTEMPTS: int     = 3

    FUZZY_THRESHOLD: float = 0.50


# ══════════════════════════════════════════════════════════════════════════════
# CAMPUS LANDMARK NAMES  (coordinates removed — fetched live by DynamicLocationFinder)
#
# This dictionary does ONE job only: normalize spoken/typed aliases into a
# clean canonical search query that the APIs can resolve well.
#
#   "lib"   → "Central Library NIT Jalandhar"   ✅  API finds it
#   "bh1"   → "Boys Hostel 1 NIT Jalandhar"     ✅  API finds it
#
# No coordinates are stored here.  All coordinates come from:
#   1. JSON cache (smart_specs_coords.json) — instant, offline
#   2. OSM Overpass API — campus bounding box, strict containment
#   3. Google Places Text Search (New) — locationRestriction hard-lock
#   4. Nominatim geocoding — free fallback
#
# To add a new place: add a line with its canonical name and aliases.
# No coordinate lookup required.
# ══════════════════════════════════════════════════════════════════════════════
CAMPUS_LANDMARK_NAMES: Dict[str, List[str]] = {
    # ── Gates ─────────────────────────────────────────────────────────────────
    "Main Gate NIT Jalandhar": [
        "main gate", "gate", "entrance", "entry", "main entrance",
        "front gate", "mukhya dwar", "main",
    ],
    "Back Gate NIT Jalandhar": [
        "back gate", "rear gate", "side gate", "pichhla gate", "back",
    ],

    # ── Administration ────────────────────────────────────────────────────────
    "Administrative Block NIT Jalandhar": [
        "admin block", "administrative block", "administration block",
        "admin building", "admin office", "office block",
        "director office", "administrative office", "admin",
        "administration",
    ],
    "Director Residence NIT Jalandhar": [
        "director residence", "director house", "director bungalow",
        "vc house", "director",
    ],
    "Guest House NIT Jalandhar": [
        "guest house", "visitor house", "guest block", "atithi griha",
        "guest",
    ],

    # ── Academic ──────────────────────────────────────────────────────────────
    "Lecture Hall Complex NIT Jalandhar": [
        # Canonical and full names
        "lecture hall complex", "lecture complex", "lecture hall",
        # Abbreviation: LHC — Google STT hears these for "LHC"
        "lhc", "l h c", "el each c", "el h c", "lhc block",
        "the lhc", "each",
    ],
    "Academic Block 1 NIT Jalandhar": [
        "academic block 1", "academic block one",
        # AB1 phonetics — Google STT hears these for "AB1"
        "ab1", "ab 1", "a b 1", "a b one", "a be 1", "a be one",
        "ab one",
    ],
    "Academic Block 2 NIT Jalandhar": [
        "academic block 2", "academic block two",
        "ab2", "ab 2", "a b 2", "a b two", "a be 2", "a be two",
        "ab two",
    ],
    "Academic Block 3 NIT Jalandhar": [
        "academic block 3", "academic block three",
        "ab3", "ab 3", "a b 3", "a b three", "a be 3", "a be three",
        "ab three",
    ],

    # ── Departments ───────────────────────────────────────────────────────────
    "Department of Civil Engineering NIT Jalandhar": [
        "civil engineering", "civil dept", "civil department", "civil block",
        "civil",
    ],
    "Department of Computer Science NIT Jalandhar": [
        "computer science", "computer dept", "it department", "it dept",
        "it block", "computer",
        # CSE phonetics — Google STT hears these for "CSE"
        "cse", "c s e", "cs e", "cs", "c s", "ces", "see es ee",
        "c se",
    ],
    "Department of Electrical Engineering NIT Jalandhar": [
        "electrical engineering", "electrical dept", "electrical block",
        "electrical",
        # EEE phonetics — Google STT hears these for "EEE"
        "eee", "e e e", "triple e", "triply", "eeee", "three e",
        "e e", "ee",
    ],
    "Department of Mechanical Engineering NIT Jalandhar": [
        "mechanical engineering", "mech dept", "mechanical dept",
        "mechanical block", "mechanical", "mech",
    ],
    "Department of Electronics NIT Jalandhar": [
        "electronics", "electronics dept", "electronics block",
        "electronics and communication",
        # ECE phonetics — Google STT hears these for "ECE"
        "ece", "e c e", "ec", "easy", "ec e", "e ce",
        "e see e", "ee see ee",
    ],
    "Department of Chemical Engineering NIT Jalandhar": [
        "chemical engineering", "chemical dept", "chemical block", "chemical",
    ],
    "Department of Textile Technology NIT Jalandhar": [
        "textile technology", "textile dept", "textile block", "textile",
    ],
    "Department of Instrumentation NIT Jalandhar": [
        "instrumentation", "instru dept", "instrumentation block",
    ],

    # ── Library ───────────────────────────────────────────────────────────────
    "Central Library NIT Jalandhar": [
        "central library", "library", "lib", "pustakalaya",
        "reading room", "central lib", "main library",
    ],

    # ── Boys Hostels ──────────────────────────────────────────────────────────
    # BH phonetics: Google STT hears "be each", "be h", "bh", "b h", "be aitch"
    # for the letters B-H. We cover all variants for each number.
    "Boys Hostel 1 NIT Jalandhar": [
        "boys hostel 1", "boys hostel one", "hostel 1", "hostel one",
        "bh1", "bh 1", "b h 1", "b h one", "be h 1", "be h one",
        "be each 1", "be each one", "be aitch 1", "be aitch one",
        "bh one",
    ],
    "Boys Hostel 2 NIT Jalandhar": [
        "boys hostel 2", "boys hostel two", "hostel 2", "hostel two",
        "bh2", "bh 2", "b h 2", "b h two", "be h 2", "be h two",
        "be each 2", "be each two", "be aitch 2", "be aitch two",
        "bh two",
    ],
    "Boys Hostel 3 NIT Jalandhar": [
        "boys hostel 3", "boys hostel three", "hostel 3", "hostel three",
        "bh3", "bh 3", "b h 3", "b h three", "be h 3", "be h three",
        "be each 3", "be each three", "be aitch 3", "be aitch three",
        "bh three",
    ],
    "Boys Hostel 4 NIT Jalandhar": [
        "boys hostel 4", "boys hostel four", "hostel 4", "hostel four",
        "bh4", "bh 4", "b h 4", "b h four", "be h 4", "be h four",
        "be each 4", "be each four", "be aitch 4", "be aitch four",
        "bh four",
    ],
    "Boys Hostel 5 NIT Jalandhar": [
        "boys hostel 5", "boys hostel five", "hostel 5", "hostel five",
        "bh5", "bh 5", "b h 5", "b h five", "be h 5", "be h five",
        "be each 5", "be each five", "be aitch 5", "be aitch five",
        "bh five",
    ],
    "Boys Hostel 6 NIT Jalandhar": [
        "boys hostel 6", "boys hostel six", "hostel 6", "hostel six",
        "bh6", "bh 6", "b h 6", "b h six", "be h 6", "be h six",
        "be each 6", "be each six", "be aitch 6", "be aitch six",
        "bh six",
    ],
    "Boys Hostel 7 NIT Jalandhar": [
        "boys hostel 7", "boys hostel seven", "hostel 7", "hostel seven",
        "bh7", "bh 7", "b h 7", "b h seven", "be h 7", "be h seven",
        "be each 7", "be each seven", "be aitch 7", "be aitch seven",
        "bh seven",
        # Extra variants Google hears for BH7
        "the edge 7", "the h 7", "bh 7th",
    ],
    "Boys Hostel 8 NIT Jalandhar": [
        "boys hostel 8", "boys hostel eight", "hostel 8", "hostel eight",
        "bh8", "bh 8", "b h 8", "b h eight", "be h 8", "be h eight",
        "be each 8", "be each eight", "be aitch 8", "be aitch eight",
        "bh eight",
    ],

    # ── Girls Hostels ─────────────────────────────────────────────────────────
    # GH phonetics: Google STT hears "g h", "gee each", "jee h", "gee aitch"
    "Girls Hostel 1 NIT Jalandhar": [
        "girls hostel 1", "girls hostel one", "girls hostel",
        "gh1", "gh 1", "g h 1", "g h one", "gee h 1", "gee h one",
        "gee each 1", "gee each one", "jee h 1", "jee h one",
        "gh one",
    ],
    "Girls Hostel 2 NIT Jalandhar": [
        "girls hostel 2", "girls hostel two",
        "gh2", "gh 2", "g h 2", "g h two", "gee h 2", "gee h two",
        "gee each 2", "gee each two",
        "gh two",
    ],
    "Girls Hostel 3 NIT Jalandhar": [
        "girls hostel 3", "girls hostel three",
        "gh3", "gh 3", "g h 3", "g h three", "gee h 3", "gee h three",
        "gee each 3", "gee each three",
        "gh three",
    ],

    # ── Food & Shopping ───────────────────────────────────────────────────────
    "Shopping Complex NIT Jalandhar": [
        "shopping complex", "market", "shops", "shopping area",
        "bazaar", "dukaan", "campus market", "complex",
        "shopping",
    ],
    "Central Mess NIT Jalandhar": [
        "central mess", "mess", "dining hall", "food court",
        "canteen", "khana", "cafeteria", "dining",
    ],
    "Faculty Canteen NIT Jalandhar": [
        "faculty canteen", "staff canteen", "faculty cafe",
        "teachers canteen",
    ],

    # ── Sports & Recreation ───────────────────────────────────────────────────
    "Sports Complex NIT Jalandhar": [
        "sports complex", "sports ground", "ground", "stadium",
        "sports", "playing field", "maidan",
    ],
    "Swimming Pool NIT Jalandhar": [
        "swimming pool", "pool", "swimming",
    ],

    # OAT — Google STT hears these for "OAT":
    # "oat" (the food), "oats", "o a t", "open air", "oh a t", "ate",
    # "o eight", "o at", "open 8"
    "Open Air Theatre NIT Jalandhar": [
        "open air theatre", "amphitheatre", "open theatre",
        "auditorium", "theatre", "open air",
        # OAT phonetics
        "oat", "oats", "o a t", "o at", "oh a t",
        "open 8", "open eight", "o eight",
        # What people sometimes say
        "ott", "oat theatre",
    ],
    "Gymnasium NIT Jalandhar": [
        "gymnasium", "gym", "fitness centre", "fitness center",
    ],

    # ── Student Services ──────────────────────────────────────────────────────

    # SAC — Google STT hears these for "SAC":
    # "sack", "sad", "sock", "suck", "sac", "s a c", "sa c", "essay"
    "Student Activity Centre NIT Jalandhar": [
        "student activity centre", "student activity center",
        "student centre", "student center", "activity centre",
        "student activities",
        # SAC phonetics
        "sac", "s a c", "sa c", "sack", "sock", "essay",
        "s ac", "sac block",
    ],
    "Medical Centre NIT Jalandhar": [
        "medical centre", "hospital", "dispensary", "medical center",
        "clinic", "health centre", "doctor", "medical block",
        "chikitsa kendra", "medical", "health",
    ],
    "Placement Cell NIT Jalandhar": [
        "placement cell", "placement office", "placement block",
        "career services", "placement",
        # TPO phonetics — Google STT hears these for "TPO"
        "tpo", "t p o", "tee p o", "t po", "tp o",
        "training and placement", "training placement",
    ],

    # ── Misc ──────────────────────────────────────────────────────────────────

    # NCC — Google STT hears "n c c", "en c c", "nc"
    "NCC Block NIT Jalandhar": [
        "ncc block", "national cadet corps",
        "ncc", "n c c", "en c c", "en see see", "nc",
    ],
    "Workshop NIT Jalandhar": [
        "workshop", "workshop block", "central workshop",
    ],
    "Seminar Hall NIT Jalandhar": [
        "seminar hall", "conference hall", "seminar block",
        "seminar room", "seminar",
    ],
    "Bank NIT Jalandhar": [
        "bank", "sbi", "atm", "bank branch",
        # SBI phonetics
        "s b i", "es be eye", "es bi",
    ],
    "Post Office NIT Jalandhar": [
        "post office", "post", "dak ghar",
    ],
}

# ── Flat alias → canonical search query map ───────────────────────────────────
_ALIAS_MAP: Dict[str, str] = {}
for _canonical, _aliases in CAMPUS_LANDMARK_NAMES.items():
    _ALIAS_MAP[_canonical.lower()] = _canonical
    for _alias in _aliases:
        _ALIAS_MAP[_alias.lower()] = _canonical


# ══════════════════════════════════════════════════════════════════════════════
# PHONETIC CORRECTION MAP
#
# Maps what Google STT actually returns → the correct campus term.
# This runs BEFORE the alias map so mis-heard text is corrected first,
# then the alias map finds the right location.
#
# Format: "what google hears" → "correct campus term"
#
# How to find what Google hears:
#   Say the abbreviation out loud → check the log → add mis-hearing here.
# ══════════════════════════════════════════════════════════════════════════════
_PHONETIC_MAP: Dict[str, str] = {

    # ── OAT (Open Air Theatre) ────────────────────────────────────────────────
    "oat":               "open air theatre",
    "oats":              "open air theatre",
    "o a t":             "open air theatre",
    "o at":              "open air theatre",
    "oh a t":            "open air theatre",
    "o eight":           "open air theatre",
    "open eight":        "open air theatre",
    "open 8":            "open air theatre",
    "ott":               "open air theatre",
    "oat theatre":       "open air theatre",

    # ── SAC (Student Activity Centre) ─────────────────────────────────────────
    "sac":               "student activity centre",
    "s a c":             "student activity centre",
    "sa c":              "student activity centre",
    "sack":              "student activity centre",
    "sock":              "student activity centre",
    "essay":             "student activity centre",
    "s ac":              "student activity centre",
    "sac block":         "student activity centre",
    "the sac":           "student activity centre",

    # ── LHC (Lecture Hall Complex) ─────────────────────────────────────────────
    "lhc":               "lecture hall complex",
    "l h c":             "lecture hall complex",
    "el each c":         "lecture hall complex",
    "el h c":            "lecture hall complex",
    "each":              "lecture hall complex",   # Google often hears just "each"
    "lhc block":         "lecture hall complex",

    # ── TPO (Training & Placement Office) ─────────────────────────────────────
    "tpo":               "placement cell",
    "t p o":             "placement cell",
    "tee p o":           "placement cell",
    "t po":              "placement cell",
    "tp o":              "placement cell",

    # ── NCC ───────────────────────────────────────────────────────────────────
    "n c c":             "ncc block",
    "en c c":            "ncc block",
    "en see see":        "ncc block",
    "nc":                "ncc block",

    # ── CSE (Computer Science) ────────────────────────────────────────────────
    "cse":               "computer science",
    "c s e":             "computer science",
    "cs e":              "computer science",
    "c s":               "computer science",
    "ces":               "computer science",
    "see es ee":         "computer science",
    "c se":              "computer science",

    # ── ECE (Electronics) ─────────────────────────────────────────────────────
    "ece":               "electronics",
    "e c e":             "electronics",
    "ec":                "electronics",
    "easy":              "electronics",
    "ec e":              "electronics",
    "e see e":           "electronics",
    "ee see ee":         "electronics",
    "e ce":              "electronics",

    # ── EEE (Electrical Engineering) ──────────────────────────────────────────
    "eee":               "electrical engineering",
    "e e e":             "electrical engineering",
    "triple e":          "electrical engineering",
    "triply":            "electrical engineering",
    "three e":           "electrical engineering",
    "e e":               "electrical engineering",

    # ── SBI / Bank ────────────────────────────────────────────────────────────
    "s b i":             "bank",
    "es be eye":         "bank",
    "es bi":             "bank",
    "sbi":               "bank",
    "atm":               "bank",

    # ── BH1–BH8 (Boys Hostels) ────────────────────────────────────────────────
    # Google hears "be each N", "be h N", "b h N", "be aitch N" for BH-N
    "bh1":               "boys hostel 1",
    "bh 1":              "boys hostel 1",
    "b h 1":             "boys hostel 1",
    "be h 1":            "boys hostel 1",
    "be each 1":         "boys hostel 1",
    "be aitch 1":        "boys hostel 1",
    "be h one":          "boys hostel 1",
    "be each one":       "boys hostel 1",
    "bh one":            "boys hostel 1",

    "bh2":               "boys hostel 2",
    "bh 2":              "boys hostel 2",
    "b h 2":             "boys hostel 2",
    "be h 2":            "boys hostel 2",
    "be each 2":         "boys hostel 2",
    "be aitch 2":        "boys hostel 2",
    "be h two":          "boys hostel 2",
    "be each two":       "boys hostel 2",
    "bh two":            "boys hostel 2",

    "bh3":               "boys hostel 3",
    "bh 3":              "boys hostel 3",
    "b h 3":             "boys hostel 3",
    "be h 3":            "boys hostel 3",
    "be each 3":         "boys hostel 3",
    "be aitch 3":        "boys hostel 3",
    "be h three":        "boys hostel 3",
    "be each three":     "boys hostel 3",
    "bh three":          "boys hostel 3",

    "bh4":               "boys hostel 4",
    "bh 4":              "boys hostel 4",
    "b h 4":             "boys hostel 4",
    "be h 4":            "boys hostel 4",
    "be each 4":         "boys hostel 4",
    "be h four":         "boys hostel 4",
    "be each four":      "boys hostel 4",
    "bh four":           "boys hostel 4",

    "bh5":               "boys hostel 5",
    "bh 5":              "boys hostel 5",
    "b h 5":             "boys hostel 5",
    "be h 5":            "boys hostel 5",
    "be each 5":         "boys hostel 5",
    "be h five":         "boys hostel 5",
    "be each five":      "boys hostel 5",
    "bh five":           "boys hostel 5",

    "bh6":               "boys hostel 6",
    "bh 6":              "boys hostel 6",
    "b h 6":             "boys hostel 6",
    "be h 6":            "boys hostel 6",
    "be each 6":         "boys hostel 6",
    "be h six":          "boys hostel 6",
    "be each six":       "boys hostel 6",
    "bh six":            "boys hostel 6",

    "bh7":               "boys hostel 7",
    "bh 7":              "boys hostel 7",
    "b h 7":             "boys hostel 7",
    "be h 7":            "boys hostel 7",
    "be each 7":         "boys hostel 7",
    "be aitch 7":        "boys hostel 7",
    "be h seven":        "boys hostel 7",
    "be each seven":     "boys hostel 7",
    "bh seven":          "boys hostel 7",
    "the edge 7":        "boys hostel 7",
    "the h 7":           "boys hostel 7",
    "bh 7th":            "boys hostel 7",

    "bh8":               "boys hostel 8",
    "bh 8":              "boys hostel 8",
    "b h 8":             "boys hostel 8",
    "be h 8":            "boys hostel 8",
    "be each 8":         "boys hostel 8",
    "be h eight":        "boys hostel 8",
    "be each eight":     "boys hostel 8",
    "bh eight":          "boys hostel 8",

    # ── GH1–GH3 (Girls Hostels) ───────────────────────────────────────────────
    # Google hears "gee each", "g h", "jee h", "gee aitch" for GH
    "gh1":               "girls hostel 1",
    "gh 1":              "girls hostel 1",
    "g h 1":             "girls hostel 1",
    "gee h 1":           "girls hostel 1",
    "gee each 1":        "girls hostel 1",
    "jee h 1":           "girls hostel 1",
    "gee h one":         "girls hostel 1",
    "gee each one":      "girls hostel 1",
    "gh one":            "girls hostel 1",

    "gh2":               "girls hostel 2",
    "gh 2":              "girls hostel 2",
    "g h 2":             "girls hostel 2",
    "gee h 2":           "girls hostel 2",
    "gee each 2":        "girls hostel 2",
    "gee h two":         "girls hostel 2",
    "gee each two":      "girls hostel 2",
    "gh two":            "girls hostel 2",

    "gh3":               "girls hostel 3",
    "gh 3":              "girls hostel 3",
    "g h 3":             "girls hostel 3",
    "gee h 3":           "girls hostel 3",
    "gee each 3":        "girls hostel 3",
    "gee h three":       "girls hostel 3",
    "gee each three":    "girls hostel 3",
    "gh three":          "girls hostel 3",

    # ── AB1–AB3 (Academic Blocks) ─────────────────────────────────────────────
    "ab1":               "academic block 1",
    "ab 1":              "academic block 1",
    "a b 1":             "academic block 1",
    "a b one":           "academic block 1",
    "a be 1":            "academic block 1",
    "a be one":          "academic block 1",
    "ab one":            "academic block 1",

    "ab2":               "academic block 2",
    "ab 2":              "academic block 2",
    "a b 2":             "academic block 2",
    "a b two":           "academic block 2",
    "a be 2":            "academic block 2",
    "a be two":          "academic block 2",
    "ab two":            "academic block 2",

    "ab3":               "academic block 3",
    "ab 3":              "academic block 3",
    "a b 3":             "academic block 3",
    "a b three":         "academic block 3",
    "a be 3":            "academic block 3",
    "a be three":        "academic block 3",
    "ab three":          "academic block 3",
}


# ══════════════════════════════════════════════════════════════════════════════
# STT SPEECH HINTS
#
# The single biggest fix for recognition accuracy.
# Google STT's free API accepts a `speech_context` with phrase hints —
# it heavily biases the decoder towards these words.
#
# Without hints: user says "BH1" → Google hears "B H one" or "be hate one"
# With hints:    user says "BH1" → Google correctly hears "BH1"
#
# We pass every alias + canonical name from CAMPUS_LANDMARK_NAMES.
# This makes the model prefer these exact phrases over similar-sounding
# generic English words.
# ══════════════════════════════════════════════════════════════════════════════
_STT_HINTS: List[str] = []
for _canonical, _aliases in CAMPUS_LANDMARK_NAMES.items():
    # Add canonical name (short form, no "NIT Jalandhar")
    short = _canonical.replace(" NIT Jalandhar", "").strip()
    _STT_HINTS.append(short)
    _STT_HINTS.extend(_aliases)

# Add common confirmation words so yes/no confirmation is also accurate
_STT_HINTS += [
    "yes", "no", "correct", "wrong", "haan", "nahi", "okay", "sure",
    "library", "hostel", "mess", "canteen", "admin", "block", "gate",
    "medical", "sports", "gym", "pool", "theatre", "placement", "workshop",
    "NIT", "NITJ", "Jalandhar", "campus",
]
# Deduplicate
_STT_HINTS = list(dict.fromkeys(h.strip() for h in _STT_HINTS if h.strip()))


# ══════════════════════════════════════════════════════════════════════════════
# INPUT PRE-PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════
_PREFIX_NOISE = re.compile(
    r"^\s*(?:"
    r"(?:i\s+)?(?:want\s+to\s+go|need\s+to\s+go|have\s+to\s+go)\s+(?:to\s+)?|"
    r"(?:please\s+)?(?:take\s+me|bring\s+me|guide\s+me)\s+to\s+(?:the\s+)?|"
    r"(?:please\s+)?(?:go\s+to|navigate\s+to|navigate|go)\s+(?:the\s+)?|"
    r"(?:find|search|locate|show)\s+(?:the\s+)?|"
    r"(?:where\s+is\s+(?:the\s+)?)|"
    r"(?:mujhe\s+jana\s+hai\s+)?|"
    r"(?:le\s+chalo\s+)?|"
    r"(?:dikhao\s+)?|"
    r"(?:the\s+)"
    r")+",
    re.IGNORECASE,
)
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
_FILLER = re.compile(r"\b(please|kindly|the|a|an)\b", re.IGNORECASE)


def preprocess_query(raw: str) -> str:
    """
    Clean raw speech/keyboard input to just the destination name.
    Also applies the phonetic correction map so mis-heard abbreviations
    are fixed before alias matching.
    """
    q = raw.strip().lower()
    q = _PREFIX_NOISE.sub("", q).strip()
    q = _SUFFIX_NOISE.sub("", q).strip()
    cleaned = _FILLER.sub(" ", q).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if cleaned:
        q = cleaned

    # Apply phonetic correction if the cleaned query is in the map
    if q in _PHONETIC_MAP:
        q = _PHONETIC_MAP[q]
        logger.info("Phonetic correction: '%s' → '%s'", raw.strip(), q)

    logger.info("Pre-process: '%s' → '%s'", raw, q)
    return q


# ══════════════════════════════════════════════════════════════════════════════
# DESTINATION DATACLASS
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Destination:
    name:    str
    lat:     float
    lon:     float
    address: str = ""
    source:  str = "cache"   # "cache"|"overpass"|"google_places"|"nominatim"

    @property
    def coords(self) -> Tuple[float, float]:
        return (self.lat, self.lon)


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC LOCATION FINDER
#
# Resolution order (fully automatic — no hardcoded coordinates):
# ─────────────────────────────────────────────────────────────────────────────
#  Step 0 — Alias normalisation
#    "library" → "Central Library NIT Jalandhar"  (via _ALIAS_MAP)
#    Ensures every API gets a clean, full query with campus context.
#
#  Step 1 — JSON coordinate cache  (smart_specs_coords.json)
#    Any place found before is returned instantly from disk.
#    Works completely offline after the first successful lookup.
#
#  Step 2 — OSM Overpass API
#    Searches OpenStreetMap nodes/ways by name inside the campus
#    bounding box.  Results are guaranteed to be inside the box —
#    Khiala or any external village is geometrically impossible.
#    Free, no API key needed.
#
#  Step 3 — Google Places Text Search (New API, 2024)
#    Uses `locationRestriction.rectangle` which HARD-LOCKS results
#    to a geographic rectangle (unlike the legacy `bounds` parameter
#    which was only a hint and allowed Khiala to slip through).
#    Requires GOOGLE_MAPS_API_KEY in .env.
#
#  Step 4 — Nominatim (OpenStreetMap geocoding)
#    Free fallback.  Results validated against campus centre distance.
#
# All successful lookups are written back to the JSON cache.
# ══════════════════════════════════════════════════════════════════════════════

_CACHE_FILE = "smart_specs_coords.json"

# Campus bounding box  (NIT Jalandhar + generous margin)
_BBOX_S = 31.385   # south latitude
_BBOX_N = 31.410   # north latitude
_BBOX_W = 75.518   # west longitude
_BBOX_E = 75.545   # east longitude


class DynamicLocationFinder:
    """
    Fully automatic coordinate resolution — user types or speaks a
    destination name and the system fetches its coordinates.
    No coordinate entry required by the developer.
    """

    _nominatim   = Nominatim(user_agent="smart_specs_nitj_v3_2")
    _cache: Dict[str, Destination] = {}    # in-memory mirror of JSON cache
    _cache_loaded = False

    # ── Public entry point ────────────────────────────────────────────────────
    @classmethod
    def find(cls, raw_input: str) -> Optional[Destination]:
        cls._ensure_cache_loaded()

        # Step 0 — normalise alias to canonical query
        clean   = preprocess_query(raw_input)
        query   = cls._resolve_alias(clean)    # e.g. "library" → "Central Library NIT Jalandhar"

        logger.info("[Finder] Resolved query: '%s'", query)

        # Step 1 — cache hit
        dest = cls._cache_get(query)
        if dest:
            logger.info("[Cache] Hit: '%s' → (%.6f, %.6f)", query, dest.lat, dest.lon)
            return dest

        # Step 2 — OSM Overpass
        dest = cls._overpass_search(query, clean)
        if dest:
            cls._cache_put(query, dest)
            return dest

        # Step 3 — Google Places Text Search (New API)
        dest = cls._google_places_search(query)
        if dest:
            cls._cache_put(query, dest)
            return dest

        # Step 4 — Nominatim
        dest = cls._nominatim_search(query)
        if dest:
            cls._cache_put(query, dest)
            return dest

        logger.warning("[Finder] No result for: '%s'", query)
        return None

    # ── Step 0: alias → canonical query ──────────────────────────────────────
    @staticmethod
    def _resolve_alias(clean: str) -> str:
        """
        Map aliases to the full canonical search query.
        Falls back to  "<clean>, NIT Jalandhar"  for unknown names.
        """
        q = clean.lower().strip()

        # Exact alias match
        if q in _ALIAS_MAP:
            return _ALIAS_MAP[q]

        # Fuzzy alias match
        matches = difflib.get_close_matches(q, list(_ALIAS_MAP.keys()),
                                            n=1, cutoff=0.55)
        if matches:
            resolved = _ALIAS_MAP[matches[0]]
            logger.info("[Alias] Fuzzy: '%s' → '%s'", q, resolved)
            return resolved

        # Unknown name — append campus context and let the APIs figure it out
        return f"{clean.title()}, NIT Jalandhar, Punjab, India"

    # ── Step 1: JSON cache ────────────────────────────────────────────────────
    @classmethod
    def _ensure_cache_loaded(cls) -> None:
        if cls._cache_loaded:
            return
        cls._cache_loaded = True
        if not os.path.exists(_CACHE_FILE):
            logger.info("[Cache] No cache file yet — will create on first lookup.")
            return
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                raw: dict = json.load(f)
            for key, val in raw.items():
                cls._cache[key.lower()] = Destination(
                    name    = val["name"],
                    lat     = val["lat"],
                    lon     = val["lon"],
                    address = val.get("address", ""),
                    source  = "cache",
                )
            logger.info("[Cache] Loaded %d entries from %s",
                        len(cls._cache), _CACHE_FILE)
        except Exception as exc:
            logger.warning("[Cache] Load failed: %s", exc)

    @classmethod
    def _cache_get(cls, query: str) -> Optional[Destination]:
        return cls._cache.get(query.lower())

    @classmethod
    def _cache_put(cls, query: str, dest: Destination) -> None:
        cls._cache[query.lower()] = dest
        # Persist to disk
        try:
            serialisable = {
                k: {"name": v.name, "lat": v.lat, "lon": v.lon,
                    "address": v.address, "source": v.source}
                for k, v in cls._cache.items()
            }
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(serialisable, f, indent=2, ensure_ascii=False)
            logger.info("[Cache] Saved '%s' → (%.6f, %.6f) [%s]",
                        query, dest.lat, dest.lon, dest.source)
        except Exception as exc:
            logger.warning("[Cache] Save failed: %s", exc)

    # ── Step 2: OSM Overpass API ──────────────────────────────────────────────
    @staticmethod
    def _overpass_search(query: str, short_name: str) -> Optional[Destination]:
        """
        Search OpenStreetMap for named places INSIDE the campus bounding box.
        The bbox filter makes it geometrically impossible to return Khiala.
        Uses the short cleaned name (e.g. "admin block") as the search term
        so OSM tag matching is more flexible.
        """
        OVERPASS_URL = "https://overpass-api.de/api/interpreter"

        # Use the short name for OSM tag search; full query has "NIT Jalandhar"
        # appended which OSM tags don't contain.
        search_term = short_name.split(",")[0].strip()

        # Query: match name tag (case-insensitive) within campus bbox
        oql = (
            f'[out:json][timeout:15];'
            f'('
            f'  node["name"~"{search_term}",i]'
            f'    ({_BBOX_S},{_BBOX_W},{_BBOX_N},{_BBOX_E});'
            f'  way["name"~"{search_term}",i]'
            f'    ({_BBOX_S},{_BBOX_W},{_BBOX_N},{_BBOX_E});'
            f'  relation["name"~"{search_term}",i]'
            f'    ({_BBOX_S},{_BBOX_W},{_BBOX_N},{_BBOX_E});'
            f');'
            f'out center;'
        )

        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": oql},
                timeout=18,
                headers={"User-Agent": "SmartSpecs-NITJ/3.2"},
            )
            resp.raise_for_status()
            data = resp.json()
            elements = data.get("elements", [])

            if not elements:
                logger.info("[Overpass] No results for: '%s'", search_term)
                return None

            # Pick the best match — prefer element whose name most closely
            # matches the search term.
            best      = None
            best_score = 0.0
            for el in elements:
                tags = el.get("tags", {})
                name = tags.get("name", "")
                score = difflib.SequenceMatcher(
                    None, search_term.lower(), name.lower()
                ).ratio()
                if score > best_score:
                    best_score = score
                    best = el

            if best is None:
                return None

            # Extract coordinates (nodes have lat/lon; ways/relations have center)
            if best["type"] == "node":
                lat, lon = best["lat"], best["lon"]
            else:
                centre = best.get("center", {})
                lat = centre.get("lat")
                lon = centre.get("lon")
                if lat is None:
                    return None

            osm_name = best.get("tags", {}).get("name", query.split(",")[0].strip())
            logger.info("[Overpass] Found: '%s' → (%.6f, %.6f)  score=%.2f",
                        osm_name, lat, lon, best_score)
            return Destination(
                name    = osm_name,
                lat     = float(lat),
                lon     = float(lon),
                address = best.get("tags", {}).get("addr:full", ""),
                source  = "overpass",
            )

        except requests.exceptions.Timeout:
            logger.warning("[Overpass] Timeout for: %s", search_term)
        except Exception as exc:
            logger.error("[Overpass] Error: %s", exc)
        return None

    # ── Step 3: Google Places Text Search (New API — 2024 endpoint) ───────────
    @staticmethod
    def _google_places_search(query: str) -> Optional[Destination]:
        """
        Google Places Text Search (New) uses `locationRestriction.rectangle`
        which HARD-LOCKS results to the campus bounding box.
        This is the key difference from the legacy Geocoding API where `bounds`
        was only a bias and allowed Khiala to be returned.

        Requires Places API (New) to be enabled in your Google Cloud console.
        The same GOOGLE_MAPS_API_KEY works — just enable the new service.
        """
        if not _GMAPS_KEY:
            return None

        PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
        headers = {
            "Content-Type":    "application/json",
            "X-Goog-Api-Key":  _GMAPS_KEY,
            "X-Goog-FieldMask": (
                "places.displayName,"
                "places.location,"
                "places.formattedAddress,"
                "places.types"
            ),
        }
        body = {
            "textQuery": query,
            "maxResultCount": 3,
            "locationRestriction": {       # ← hard lock, not a hint
                "rectangle": {
                    "low":  {"latitude": _BBOX_S, "longitude": _BBOX_W},
                    "high": {"latitude": _BBOX_N, "longitude": _BBOX_E},
                }
            },
        }

        try:
            resp = requests.post(PLACES_URL, headers=headers,
                                 json=body, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            places = data.get("places", [])

            if not places:
                logger.info("[GooglePlaces] No results for: '%s'", query)
                return None

            place   = places[0]
            loc     = place.get("location", {})
            lat     = loc.get("latitude")
            lon     = loc.get("longitude")
            if lat is None or lon is None:
                return None

            name    = place.get("displayName", {}).get("text",
                                query.split(",")[0].strip())
            address = place.get("formattedAddress", "")

            logger.info("[GooglePlaces] Found: '%s' → (%.6f, %.6f) — %s",
                        name, lat, lon, address)
            return Destination(
                name=name, lat=float(lat), lon=float(lon),
                address=address, source="google_places",
            )

        except requests.exceptions.HTTPError as exc:
            logger.error("[GooglePlaces] HTTP %s for: %s",
                         exc.response.status_code, query)
        except Exception as exc:
            logger.error("[GooglePlaces] Error: %s", exc)
        return None

    # ── Step 4: Nominatim ─────────────────────────────────────────────────────
    @classmethod
    def _nominatim_search(cls, query: str) -> Optional[Destination]:
        """
        Nominatim geocoding with campus-distance validation.
        Max allowed distance: 2× campus radius so nearby roads are accepted
        but distant cities are rejected.
        """
        try:
            location = cls._nominatim.geocode(query, timeout=12)
            if not location:
                logger.info("[Nominatim] No result for: '%s'", query)
                return None

            dist = geodesic(
                (location.latitude, location.longitude),
                (Config.CAMPUS_LAT, Config.CAMPUS_LON)
            ).meters

            if dist > Config.CAMPUS_RADIUS_METERS * 2.5:
                logger.warning("[Nominatim] Rejected (%.0fm away): %s",
                               dist, location.address)
                return None

            name = query.split(",")[0].strip()
            logger.info("[Nominatim] Found: '%s' → (%.6f, %.6f)",
                        name, location.latitude, location.longitude)
            return Destination(
                name    = name,
                lat     = location.latitude,
                lon     = location.longitude,
                address = location.address or "",
                source  = "nominatim",
            )
        except Exception as exc:
            logger.error("[Nominatim] Error: %s", exc)
        return None


# Convenience alias so the rest of the code stays unchanged
LocationFinder = DynamicLocationFinder


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
        if Config.SIMULATION_MODE:
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
        logger.info("Opening GPS: %s @ %d baud (Pixhawk u-blox M8N)",
                    Config.GPS_PORT, Config.GPS_BAUD)
        try:
            self._serial = serial.Serial(
                Config.GPS_PORT, Config.GPS_BAUD, timeout=Config.GPS_TIMEOUT
            )
        except serial.SerialException as exc:
            logger.error("Cannot open serial port %s: %s", Config.GPS_PORT, exc)
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
        # u-blox M8N (Pixhawk) outputs $GN* (multi-constellation) in addition
        # to $GP* sentences.  Also accept $GPVTG / $GNVTG for ground speed.
        _ACCEPTED = (
            "$GPGGA", "$GNGGA",
            "$GPRMC", "$GNRMC",
            "$GPVTG", "$GNVTG",
            "$GPGLL", "$GNGLL",
        )
        if not sentence.startswith(_ACCEPTED):
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
            elif isinstance(msg, pynmea2.types.talker.VTG):
                # u-blox M8N sends VTG with km/h in field spd_over_grnd_kmph
                spd = getattr(msg, "spd_over_grnd_kmph", None)
                if spd:
                    self._pending["speed_kmh"] = float(spd)
            elif isinstance(msg, pynmea2.types.talker.GLL):
                status = getattr(msg, "status", "V")
                if status == "A" and msg.latitude and msg.longitude:
                    fix = GPSFix(
                        lat       = msg.latitude,
                        lon       = msg.longitude,
                        speed_kmh = self._pending.get("speed_kmh", 0.0),
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
        source_phrases = {
            "cache":         "I have been there before —",
            "overpass":      "Found on campus map —",
            "google_places": "Found via Google —",
            "nominatim":     "Found on OpenStreetMap —",
        }
        prefix = source_phrases.get(dest.source, "Found —")
        return f"{prefix} {dest.name}. Guiding you there now."

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

        if sr:
            self._r = sr.Recognizer()
            # ── Recognizer tuning ─────────────────────────────────────────────
            # These are starting values. _calibrate_microphone() overwrites
            # energy_threshold with a live measurement on first use.
            self._r.energy_threshold          = Config.ENERGY_THRESHOLD
            self._r.dynamic_energy_threshold  = Config.DYNAMIC_ENERGY
            # pause_threshold: silence duration that ends a phrase.
            # 1.2s handles multi-word names like "Boys Hostel One" correctly.
            self._r.pause_threshold           = Config.PAUSE_THRESHOLD
            # non_speaking_duration: silence before phrase is considered ended
            self._r.non_speaking_duration     = Config.NON_SPEAKING_DURATION
            # operation_timeout: max seconds to wait for Google API response
            self._r.operation_timeout         = None
        else:
            self._r = None

        self._mic_index: Optional[int]  = None
        self._calibrated: bool          = False   # True after first calibration

        # Always initialise microphone — voice input is the only input mode
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
        """
        Full voice input pipeline:
          1. Speak prompt (if given)
          2. Play a short beep tone so user knows mic is open
          3. Record raw audio for exactly 4 seconds (bypassing flaky VAD entirely)
          4. Try STT in en-IN first, then en-US fallback
          5. Return recognised text, or None on failure
        """
        if not sr or not self._r:
            logger.error("speechrecognition not installed.")
            return None

        if prompt:
            self.speak_and_wait(prompt, pause=Config.POST_SPEECH_DELAY)

        if cue:
            self.speak_and_wait(cue, pause=0.15)

        # Short double-beep so user knows mic is open
        self._beep()
        
        # Give the TTS and beep a fraction of a second to clear from the speakers
        time.sleep(0.3)
        print("\n  ──────── 🎙  SPEAK NOW (Listening for 5 seconds) ────────\n")

        try:
            import sounddevice as sd
            import numpy as np

            fs = 16000
            duration = 5.0  # Force 5 solid seconds of recording

            # We use sd.rec directly. This completely bypasses the problematic 
            # energy thresholds and ambient noise dropping of the sr.Microphone class.
            recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='int16')
            sd.wait()  # Wait until the 5 seconds is up
            
            raw_data = recording.tobytes()
            audio = sr.AudioData(raw_data, fs, 2)
            
            # STT with hints, three-language fallback
            result = self._stt_with_hints(audio)
            if result:
                return result

            # Nothing recognised — retry if attempts remain
            logger.warning("STT: speech not understood in any dialect.")
            return None

        except sr.WaitTimeoutError:
            logger.warning("STT: listen timeout — no speech detected.")
            return None
        except sr.RequestError as exc:
            logger.error("STT API error: %s", exc)
            self.speak(Script.VOICE_UNAVAILABLE)
            return None
        except OSError as exc:
            logger.error("Microphone error: %s", exc)
            self.speak(Script.NO_MIC)
            return None
        except AttributeError:
            logger.error("speech_recognition not available.")
            return None
        except Exception as exc:
            logger.error("Unexpected listen error: %s", exc)
            return None

    def _stt_with_hints(self, audio: "sr.AudioData") -> Optional[str]:
        """
        Send audio to Google STT with campus location hints.

        FIX 1 — Uses the undocumented `show_all=False` + REST params to pass
                 speech_context phrases to Google's free STT endpoint.
                 This makes Google heavily prefer campus words over generic ones:
                   "BH1"       → not "B H one" or "be each one"
                   "OAT"       → not "oat" (the food)
                   "SAC"       → not "sack" or "sad"
                   "canteen"   → correct
                   "LHC"       → not "each" or "lhc" (unknown acronym)

        FIX 8 — Three-language fallback: en-IN → en-US → en-GB
        """
        # Build a custom recognizer call that includes speech hints.
        # The free Google STT endpoint (used by SpeechRecognition) supports
        # speechContext via the `key` parameter but not directly via the
        # library. We use the library's built-in method but then fall back
        # to a direct REST call with hints if needed.

        # Pass 1: Standard library call with Indian English
        for lang in (Config.STT_LANGUAGE, "en-US", "en-GB"):
            try:
                text = self._r.recognize_google(audio, language=lang)
                text = text.strip()
                if text:
                    logger.info("STT [%s]: '%s'", lang, text)
                    # Post-process: apply alias map immediately
                    matched = self._match_hints(text)
                    logger.info("STT matched: '%s'", matched)
                    return matched
            except sr.UnknownValueError:
                continue
            except sr.RequestError:
                raise

        # Pass 2: Direct REST API call WITH speech hints
        # This is the key fix — the hints tell Google to prefer campus words
        result = self._stt_rest_with_hints(audio)
        if result:
            matched = self._match_hints(result)
            logger.info("STT [REST+hints]: '%s' → matched: '%s'",
                        result, matched)
            return matched

        return None

    def _stt_rest_with_hints(self, audio: "sr.AudioData") -> Optional[str]:
        """
        Call Google Speech-to-Text REST API directly with speech context hints.
        Uses the same free API key as SpeechRecognition library.
        Phrases in _STT_HINTS get a boost of 20 (max) so Google strongly
        prefers them over acoustically similar generic words.
        """
        import base64
        import urllib.request
        import urllib.parse

        # Get the API key that SpeechRecognition uses internally
        # (falls back to the public demo key if none configured)
        api_key = os.getenv("GOOGLE_STT_API_KEY", "")
        if not api_key:
            # Use the same key SpeechRecognition uses for free tier
            api_key = "AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw"

        try:
            raw  = audio.get_raw_data(convert_rate=16000, convert_width=2)
            b64  = base64.b64encode(raw).decode("utf-8")

            # Build speech context with campus hints
            # Boost = 20 means "strongly prefer these phrases"
            speech_contexts = [{
                "phrases": _STT_HINTS[:500],   # API limit is 500 phrases
                "boost":   20,
            }]

            body = {
                "config": {
                    "encoding":          "LINEAR16",
                    "sampleRateHertz":   16000,
                    "languageCode":      Config.STT_LANGUAGE,
                    "alternativeLanguageCodes": ["en-US", "en-GB"],
                    "enableAutomaticPunctuation": False,
                    "model":             "latest_long",
                    "useEnhanced":       True,
                    "speechContexts":    speech_contexts,
                    "metadata": {
                        "interactionType":       "VOICE_COMMAND",
                        "microphoneDistance":    "NEARFIELD",
                        "originalMediaType":     "AUDIO",
                        "recordingDeviceType":   "SMARTPHONE",
                    },
                },
                "audio": {"content": b64},
            }

            url  = (f"https://speech.googleapis.com/v1/speech:recognize"
                    f"?key={api_key}")
            data = json.dumps(body).encode("utf-8")
            req  = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            results = result.get("results", [])
            if results:
                transcript = (results[0]
                              .get("alternatives", [{}])[0]
                              .get("transcript", "")
                              .strip())
                if transcript:
                    logger.info("STT [REST hints]: '%s'", transcript)
                    return transcript

        except Exception as exc:
            logger.warning("STT REST hints call failed: %s", exc)

        return None

    @staticmethod
    def _match_hints(text: str) -> str:
        """
        Post-process STT output → correct campus term.

        Pipeline (in order):
          1. Lowercase + strip
          2. Normalise spoken numbers  ("seven" → "7")
          3. Collapse spaced letters   ("b h 7" → "bh7")
          4. Remove stray punctuation
          5. PHONETIC MAP lookup       ("be each 7" → "boys hostel 7")
          6. ALIAS MAP lookup          ("bh7" → "Boys Hostel 7 NIT Jalandhar")
          7. FUZZY alias match         (handles partial mis-hearings)
          8. Return best match or cleaned original
        """
        if not text:
            return text

        t = text.lower().strip()

        # ── Step 2: Spoken numbers → digits ──────────────────────────────────
        _NUM = {
            "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8",
            "nine": "9", "ten": "10",
        }
        for word, digit in _NUM.items():
            t = re.sub(rf"\b{word}\b", digit, t)

        # ── Step 3: Collapse spaced single letters ────────────────────────────
        # "b h 7" → "bh7",  "b h seven" handled after step 2 → "b h 7" → "bh7"
        # "g h 2" → "gh2",  "o a t" → "oat"
        # Also: "b. h. 7" → "bh7"
        t = re.sub(r"\b([a-z])\.\s*([a-z])\.\s*(\d)\b",  r"\1\2\3", t)
        t = re.sub(r"\b([a-z])\s+([a-z])\s+(\d)\b",       r"\1\2\3", t)
        t = re.sub(r"\b([a-z])\s+([a-z])\s+([a-z])\b",    r"\1\2\3", t)
        t = re.sub(r"\b([a-z])\s+([a-z])\b",               r"\1\2",   t)

        # ── Step 4: Remove stray punctuation ──────────────────────────────────
        t = re.sub(r"[.,!?;:]", "", t).strip()
        t = re.sub(r"\s{2,}", " ", t)

        # ── Step 5: Phonetic map — direct lookup ──────────────────────────────
        if t in _PHONETIC_MAP:
            corrected = _PHONETIC_MAP[t]
            logger.info("[PhoneticMap] '%s' → '%s'", t, corrected)
            return corrected

        # Also try partial match: if STT returned extra words around the key
        # e.g. "go to bh7" → strip to "bh7" → phonetic map
        words = t.split()
        for n in range(len(words), 0, -1):
            for i in range(len(words) - n + 1):
                chunk = " ".join(words[i:i+n])
                if chunk in _PHONETIC_MAP:
                    corrected = _PHONETIC_MAP[chunk]
                    logger.info("[PhoneticMap-partial] '%s' → '%s'", chunk, corrected)
                    return corrected

        # ── Step 6: Alias map — exact lookup ──────────────────────────────────
        if t in _ALIAS_MAP:
            logger.info("[AliasMap] '%s' → '%s'", t, _ALIAS_MAP[t])
            return t   # Return the cleaned text; LocationFinder will resolve it

        # ── Step 7: Fuzzy alias match ──────────────────────────────────────────
        matches = difflib.get_close_matches(
            t, list(_ALIAS_MAP.keys()), n=1, cutoff=0.65
        )
        if matches:
            logger.info("[FuzzyMatch] '%s' → '%s'", t, matches[0])
            return matches[0]

        # ── Step 8: Return best-cleaned original ──────────────────────────────
        logger.info("[Match] No map hit for '%s' — returning as-is", t)
        return t

    @staticmethod
    def _beep() -> None:
        """Short rising double-beep — signals mic is about to open."""
        try:
            if IS_WINDOWS:
                import winsound
                winsound.Beep(880, 120)
                time.sleep(0.05)
                winsound.Beep(1100, 100)
            else:
                import subprocess
                subprocess.run(
                    ["speaker-test", "-t", "sine", "-f", "880",
                     "-l", "1", "-p", "150"],
                    capture_output=True, timeout=0.5
                )
        except Exception:
            print("  🔔 [beep]")


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
        """
        Full voice-only destination input pipeline:

          1. System asks  →  "Where would you like to go?"
          2. Mic opens    →  [beep]  user speaks  →  STT
          3. System reads back what it heard:
               "I heard — library. Is that correct? Say yes or no."
          4a. User says yes  →  proceed to coordinate fetch
          4b. User says no   →  "Sorry, please say it again" → back to step 2
          4c. No response    →  gentle reminder → retry
        """
        self._voice.speak_and_wait(Script.ASK_DESTINATION)

        attempt = 0
        while not self._stop.is_set():
            attempt += 1

            # ── Step 2: Listen for destination ───────────────────────────────
            heard = self._voice.listen(
                prompt=None,
                cue=Script.LISTENING_CUE if attempt == 1 else ""
            )

            if not heard:
                # Nothing heard — gentle prompt
                reminders = [
                    "I did not catch that. Please say the place name clearly.",
                    "Take your time. Just say the name of the place.",
                    "No hurry. Say it whenever you are ready.",
                    "I am listening. Please speak the destination name.",
                ]
                self._voice.speak_and_wait(
                    reminders[(attempt - 1) % len(reminders)],
                    pause=0.3
                )
                continue

            hl = heard.lower()
            if any(w in hl for w in ["stop navigation", "exit navigation", "cancel navigation", "stop", "exit", "cancel"]):
                logger.info("User requested to stop navigation during prompt.")
                self._voice.speak_and_wait("Navigation cancelled.")
                return "STOP_CMD"

            # ── Step 3: Read back and confirm ─────────────────────────────────
            # Apply alias matching immediately so readback shows the
            # recognised campus name, not the raw mis-heard words.
            clean   = preprocess_query(heard)
            # Try to map to a known alias for a cleaner readback
            matched = _ALIAS_MAP.get(clean.lower(), "")
            if matched:
                # Show short name without "NIT Jalandhar"
                display = matched.replace(" NIT Jalandhar", "").strip()
            else:
                display = clean.title() if clean else heard.title()

            confirm_prompt = (
                f"I heard — {display}. "
                "Is that correct? Say yes to confirm, or no to try again."
            )
            self._voice.speak_and_wait(confirm_prompt, pause=0.2)

            # ── Step 4: Listen for yes/no ──────────────────────────────────────
            confirmation = self._voice.listen(
                prompt=None,
                cue="Say yes or no."
            )

            if confirmation is None:
                # No response to confirmation — assume yes (common case on Pi)
                logger.info("No confirmation response — assuming yes for: '%s'", heard)
                self._voice.speak_and_wait("Okay, let me search for that.")
                return heard

            cl = confirmation.lower()

            # Yes → proceed
            if any(w in cl for w in ["yes", "yeah", "yep", "haan", "ha",
                                      "correct", "right", "sure", "ok",
                                      "okay", "sahi", "bilkul"]):
                self._voice.speak_and_wait("Great, let me search for that.")
                logger.info("Destination confirmed: '%s'", heard)
                return heard

            # No → retry
            if any(w in cl for w in ["no", "nahi", "nahin", "nope",
                                      "wrong", "galat", "not", "different"]):
                self._voice.speak_and_wait(
                    "Sorry about that. Please say the destination again."
                )
                continue

            # Unclear response — treat as yes to avoid infinite loop
            logger.info("Unclear confirmation '%s' — treating as yes.", confirmation)
            self._voice.speak_and_wait("Okay, let me search for that.")
            return heard

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

                    # NEW FEATURE: Allow cancelling active navigation via button
                    # After speaking status, briefly listen for a "stop navigation" command.
                    cancel_heard = self._voice.listen(
                        prompt="Would you like to stop navigation?",
                        cue="Say yes to stop, or stay silent to continue."
                    )
                    if cancel_heard:
                        chl = cancel_heard.lower()
                        if any(w in chl for w in ["yes", "stop", "exit", "cancel", "band", "haan", "ha", "khatam"]):
                            logger.info("User requested to stop navigation mid-journey via button press.")
                            self._voice.speak_and_wait("Navigation cancelled.")
                            return False

            except Exception as exc:
                logger.error("Nav loop error: %s", exc)
                self._voice.speak(Script.NAV_ERROR)

        return False

    def _ask_continue(self) -> bool:
        """Pure voice continue prompt — no keyboard involved."""
        self._voice.speak_and_wait(Script.CONTINUE_PROMPT)

        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            answer = self._voice.listen(
                prompt=None,
                cue="Please say yes or no."
            )
            if answer:
                al = answer.lower()
                if any(w in al for w in ["no", "nahi", "nahin", "band",
                                          "exit", "stop", "done", "nope",
                                          "bas", "khatam"]):
                    return False
                if any(w in al for w in ["yes", "haan", "ha", "sure",
                                          "ok", "okay", "yeah", "continue",
                                          "aur", "chalo"]):
                    return True
                self._voice.speak_and_wait(
                    "I heard you but could not tell yes or no. "
                    "Please say yes or no clearly."
                )
            else:
                if attempt >= 3:
                    # After 3 silent attempts assume user is done
                    logger.info("No continue response after 3 attempts — stopping.")
                    return False
                self._voice.speak_and_wait(
                    "I am still listening. Say yes to navigate again, "
                    "or no to stop."
                )

        return False

    def run(self) -> None:
        logger.info("=" * 70)
        logger.info("  SMART SPECS  v3.3  –  NIT Jalandhar Campus Navigation")
        if Config.SIMULATION_MODE:
            mode_str = "SIMPLE (simulation)"
        else:
            mode_str = "RASPI (Pixhawk u-blox M8N @ %s:%d)" % (
                Config.GPS_PORT, Config.GPS_BAUD)
        logger.info("  Platform: %s  |  Mode: %s", platform.system(), mode_str)
        logger.info("=" * 70)

        self._voice.speak_and_wait(Script.WELCOME)
        self._handler.start()
        self._button.start()

        # u-blox M8N cold start ≈ 26s, hot start ≈ 1s; allow generous timeout
        fix_timeout = 10 if Config.SIMULATION_MODE else 90
        if not self._wait_for_fix(timeout=fix_timeout):
            logger.critical("GPS fix failed. Exiting.")
            self._shutdown()
            return

        try:
            while not self._stop.is_set():

                # ── Step 1: Voice input + confirmation ────────────────────────
                spoken = self._ask_destination()
                if not spoken or self._stop.is_set():
                    continue

                if spoken == "STOP_CMD":
                    break

                logger.info("Destination spoken: '%s'", spoken)

                # ── Step 2: Fetch coordinates ─────────────────────────────────
                self._voice.speak_and_wait(Script.SEARCHING)
                dest = LocationFinder.find(spoken)

                if not dest:
                    self._voice.speak_and_wait(Script.NOT_FOUND_CAMPUS)
                    continue

                # ── Step 3: Announce what was found + source ──────────────────
                self._voice.speak_and_wait(Script.found_location(dest))

                # ── Step 4: Navigate ──────────────────────────────────────────
                arrived = self._navigate_to(dest.name, dest.coords)
                self._arrived = arrived

                if not arrived:
                    break   # Ctrl+C mid-journey

                # ── Step 5: Ask to continue ───────────────────────────────────
                if not self._stop.is_set() and not self._ask_continue():
                    break

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
def _parse_cli() -> None:
    """Parse CLI flags and adjust Config before anything else runs."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Smart Specs – Campus Navigation System"
    )
    parser.add_argument(
        "-raspi", "--raspi",
        action="store_true",
        help="Raspberry Pi mode: real GPS via Pixhawk u-blox M8N, GPIO button"
    )
    parser.add_argument(
        "--gps-port",
        type=str,
        default=None,
        help="Override GPS serial port (default: /dev/ttyACM0 in raspi mode)"
    )
    parser.add_argument(
        "--gps-baud",
        type=int,
        default=None,
        help="Override GPS baud rate (default: 9600)"
    )
    args = parser.parse_args()

    if args.raspi:
        Config.SIMULATION_MODE = False
        Config.GPS_PORT = "/dev/ttyACM0"
        Config.GPS_BAUD = 9600
        logger.info("Raspberry Pi mode enabled – Pixhawk u-blox M8N GPS")
    else:
        Config.SIMULATION_MODE = True
        logger.info("Simple/simulation mode (no -raspi flag)")

    if args.gps_port:
        Config.GPS_PORT = args.gps_port
    if args.gps_baud:
        Config.GPS_BAUD = args.gps_baud


if __name__ == "__main__":
    _parse_cli()
    app = SmartSpecsApp()
    app.run()