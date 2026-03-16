#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         SMART SPECS – Campus Navigation System for Visually Impaired        ║
║         Dr. B.R. Ambedkar National Institute of Technology, Jalandhar        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Version     : 3.0                                                           ║
║  Platform    : Raspberry Pi (production)  |  Windows/Linux (simulation)      ║
║  Python      : 3.8+                                                          ║
║  Hardware    : NEO-6M GPS Module + USB Microphone + Speaker                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Sources integrated:                                                         ║
║   • github.com/Uberi/speech_recognition  – STT engine & mic handling         ║
║   • github.com/geopy/geopy               – Geodesic distance & geocoding     ║
║   • github.com/FranzTscharf/Python-NEO-6M-GPS-Raspberry-Pi – NMEA parsing   ║
║   • github.com/AV-Lab/RoadSceneUnderstanding-ModifiedUNet – Scene awareness  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Install:                                                                    ║
║   pip install pynmea2 pyttsx3 speechrecognition geopy pyaudio pyserial       ║
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
import difflib
import threading
import logging
import platform
import json
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List

# ── Load environment variables from .env ──────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()  # reads .env in the working directory

# ── Logging ───────────────────────────────────────────────────────────────────
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


# ── Dependency check ──────────────────────────────────────────────────────────
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

import serial                           # noqa: E402
import pynmea2                          # noqa: E402
import pyttsx3                          # noqa: E402
import speech_recognition as sr        # noqa: E402
import googlemaps                      # noqa: E402
from geopy.geocoders import Nominatim  # noqa: E402
from geopy.distance import geodesic    # noqa: E402

# ── Google Maps client ────────────────────────────────────────────────────────
_GMAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
if _GMAPS_KEY:
    gmaps_client = googlemaps.Client(key=_GMAPS_KEY)
    logger.info("Google Maps API key loaded.")
else:
    gmaps_client = None
    logger.warning("GOOGLE_MAPS_API_KEY not found in .env – falling back to Nominatim.")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
class Config:
    """All tuneable parameters in one place."""

    # ── Mode ──────────────────────────────────────────────────────────────────
    SIMULATION_MODE: bool = True        # False = real NEO-6M hardware
    TEXT_INPUT_MODE: bool = True        # True = type destination via keyboard
                                        # False = use microphone (STT)

    # ── GPS / Serial (from FranzTscharf/Python-NEO-6M-GPS-Raspberry-Pi) ──────
    # Raspberry Pi 3/4  → /dev/serial0   (UART0, hardware serial)
    # Raspberry Pi 0/1  → /dev/ttyAMA0
    GPS_PORT: str    = "/dev/serial0"
    GPS_BAUD: int    = 9600             # NEO-6M default
    GPS_TIMEOUT: int = 1               # seconds per readline

    # ── Navigation ────────────────────────────────────────────────────────────
    UPDATE_INTERVAL: float  = 5.0       # seconds between spoken updates
    ARRIVED_METERS: int     = 15        # radius to declare arrival
    MIN_MOVE_METERS: float  = 2.0       # min displacement to update heading
    CAMPUS_RADIUS_METERS: float = 900.0 # NIT Jalandhar campus ~154 acres

    # ── Campus centre (NIT Jalandhar, GT Road bypass) ─────────────────────────
    CAMPUS_LAT: float = 31.3967
    CAMPUS_LON: float = 75.5303

    # ── Voice ─────────────────────────────────────────────────────────────────
    TTS_RATE: int     = 140
    TTS_VOLUME: float = 1.0
    POST_SPEECH_DELAY: float = 0.5      # silence after TTS before mic opens
    MAX_RETRIES: int  = 3               # listen attempts before giving up

    # ── Speech recognition (from Uberi/speech_recognition) ───────────────────
    # Indian English gives best results for NIT campus names
    STT_LANGUAGE: str = "en-IN"
    LISTEN_TIMEOUT: int       = 8       # seconds to wait for any speech
    PHRASE_TIME_LIMIT: int    = 12      # max phrase length
    ENERGY_THRESHOLD: int     = 300     # microphone sensitivity
    DYNAMIC_ENERGY: bool      = True    # auto-adjust to ambient noise

    # ── Fuzzy matching ────────────────────────────────────────────────────────
    FUZZY_THRESHOLD: float = 0.55       # 0.0–1.0; lower = more lenient


# ══════════════════════════════════════════════════════════════════════════════
# LOCATION FINDER  (Google Maps API – fully automated, no hardcoded places)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Destination:
    """Holds a resolved destination with name and coordinates."""
    name: str
    lat: float
    lon: float
    address: str = ""     # full address returned by Google Maps

    @property
    def coords(self) -> Tuple[float, float]:
        return (self.lat, self.lon)


class LocationFinder:
    """
    Geocodes any user-spoken place name via Google Maps Geocoding API.
    No hardcoded landmarks — everything is looked up live.
    Falls back to Nominatim if Google Maps is unavailable.
    """

    _nominatim = Nominatim(user_agent="smart_specs_nitj_v3")

    @staticmethod
    def find(place_name: str) -> Optional[Destination]:
        """
        Look up any place by name. Automatically appends 'NIT Jalandhar'
        context for campus locations, but also works for any address.
        """
        place = place_name.strip()
        if not place:
            return None

        # Try with campus context first (helps find campus-specific places)
        dest = LocationFinder._try_geocode(
            f"{place}, NIT Jalandhar, Punjab, India"
        )
        if dest:
            return dest

        # Try without campus context (for general locations)
        dest = LocationFinder._try_geocode(
            f"{place}, Jalandhar, Punjab, India"
        )
        if dest:
            return dest

        # Try the raw query as-is (for full addresses)
        dest = LocationFinder._try_geocode(place)
        return dest

    @staticmethod
    def _try_geocode(query: str) -> Optional[Destination]:
        """Try Google Maps first, then Nominatim."""
        dest = LocationFinder._google_geocode(query)
        if dest:
            return dest
        return LocationFinder._nominatim_geocode(query)

    @staticmethod
    def _google_geocode(query: str) -> Optional[Destination]:
        """Geocode via Google Maps Geocoding API."""
        if not gmaps_client:
            return None
        try:
            results = gmaps_client.geocode(query)
            # Log the raw response so the user can see it in the console
            print("\n=== GOOGLE MAPS API RESPONSE ===")
            print(json.dumps(results, indent=2))
            print("==================================\n")
            
            if results:
                result = results[0]
                loc = result["geometry"]["location"]
                address = result.get("formatted_address", "")
                # Extract a short readable name from the address
                name = address.split(",")[0] if address else query.split(",")[0].strip()
                logger.info("[Google Maps] '%s' → (%.6f, %.6f) — %s",
                            query, loc["lat"], loc["lng"], address)
                return Destination(
                    name=name, lat=loc["lat"], lon=loc["lng"], address=address
                )
            logger.warning("[Google Maps] No results for: %s", query)
        except Exception as exc:
            logger.error("[Google Maps] Geocoding error: %s", exc)
        return None

    @staticmethod
    def _nominatim_geocode(query: str) -> Optional[Destination]:
        """Geocode via OpenStreetMap Nominatim (free fallback)."""
        try:
            location = LocationFinder._nominatim.geocode(query, timeout=10)
            if location:
                logger.info("[Nominatim] '%s' → (%.6f, %.6f)",
                            query, location.latitude, location.longitude)
                return Destination(
                    name=query.split(",")[0].strip(),
                    lat=location.latitude, lon=location.longitude,
                    address=location.address or ""
                )
            logger.warning("[Nominatim] No results for: %s", query)
        except Exception as exc:
            logger.error("[Nominatim] Geocoding error: %s", exc)
        return None
# ══════════════════════════════════════════════════════════════════════════════
# INDIAN CAMPUS HAZARD AWARENESS
# (Inspired by AV-Lab/RoadSceneUnderstanding – adapted for campus pedestrian use)
# In production, replace _simulate_scene with camera + UNet inference.
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class HazardZone:
    lat: float
    lon: float
    radius_m: float
    warning: str    # spoken warning


# Known hazard zones on NIT Jalandhar campus
HAZARD_ZONES: List[HazardZone] = [
    HazardZone(31.3945, 75.5278, 30,
               "Caution: you are near the main gate. Vehicles entering and exiting. Please stay to the left side."),
    HazardZone(31.3963, 75.5295, 25,
               "Speed bumps and two-wheelers ahead near the shopping complex. Walk carefully."),
    HazardZone(31.3953, 75.5325, 20,
               "Hostel zone. Uneven footpath ahead. Watch your step."),
    HazardZone(31.3948, 75.5308, 20,
               "Open air theatre steps area. Uneven ground ahead."),
    HazardZone(31.3942, 75.5300, 30,
               "Sports complex road crossing. Cyclists and joggers may be present. Please be alert."),
    HazardZone(31.3980, 75.5288, 25,
               "Guest house driveway. Cars may be moving. Please proceed carefully."),
]


class SceneAwareness:
    """
    Campus hazard detection.
    In production: run a camera frame through the modified UNet from
    AV-Lab/RoadSceneUnderstanding to classify road scene elements
    (footpath, road, vehicle, person, obstacle) and generate warnings.
    Here we use GPS-based proximity to known hazard zones.
    """

    def __init__(self) -> None:
        self._last_warned: Dict[int, float] = {}   # zone_index → last_warn_time
        self._warn_cooldown: float = 60.0           # re-warn after 60 seconds

    def check_hazards(self, lat: float, lon: float) -> Optional[str]:
        """
        Returns a hazard warning string if the user is within a hazard zone,
        or None if all clear.
        """
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
        """Return True if coordinates are within the NIT Jalandhar campus boundary."""
        dist = geodesic(
            (lat, lon), (Config.CAMPUS_LAT, Config.CAMPUS_LON)
        ).meters
        return dist <= Config.CAMPUS_RADIUS_METERS


# ══════════════════════════════════════════════════════════════════════════════
# THREAD-SAFE GPS DATA STORE
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class GPSFix:
    lat: float       = 0.0
    lon: float       = 0.0
    altitude: float  = 0.0
    speed_kmh: float = 0.0    # from GPRMC speed over ground
    satellites: int  = 0      # from GPGGA num_sats
    quality: int     = 0      # 0=invalid, 1=GPS, 2=DGPS
    valid: bool      = False


class GPSData:
    """Thread-safe, lock-protected GPS state container."""

    def __init__(self) -> None:
        self._lock      = threading.Lock()
        self._current   = GPSFix()
        self._previous  = GPSFix()

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
# GPS HANDLER  (based on FranzTscharf/Python-NEO-6M-GPS-Raspberry-Pi)
# ══════════════════════════════════════════════════════════════════════════════
class GPSHandler:
    """
    Reads NEO-6M NMEA sentences via UART and populates GPSData.
    Parses both $GPGGA (position, altitude, satellites, fix quality)
    and $GPRMC (position, speed, date/time).

    Simulation path: walks across NIT Jalandhar campus from main gate
    towards the Admin Block for testing without hardware.
    """

    def __init__(self, gps_data: GPSData) -> None:
        self._data     = gps_data
        self._running  = False
        self._thread: Optional[threading.Thread] = None
        self._serial: Optional[serial.Serial]    = None
        # State shared between GGA and RMC callbacks
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

    # ── Hardware path ─────────────────────────────────────────────────────────
    def _hardware(self) -> None:
        try:
            self._serial = serial.Serial(
                Config.GPS_PORT, Config.GPS_BAUD, timeout=Config.GPS_TIMEOUT
            )
            logger.info("Serial open: %s @ %d baud", Config.GPS_PORT, Config.GPS_BAUD)
        except serial.SerialException as exc:
            logger.error("Cannot open serial port: %s", exc)
            return

        while self._running:
            try:
                raw_bytes = self._serial.readline()
                if not raw_bytes:
                    continue
                # NEO-6M sends ASCII NMEA; decode ignoring corrupt bytes
                line = raw_bytes.decode("ascii", errors="replace").strip()
                if line:
                    self._parse(line)
            except serial.SerialException as exc:
                logger.error("Serial read error: %s", exc)
                self._data.invalidate()
                time.sleep(1)
            except Exception as exc:
                logger.debug("GPS loop error: %s", exc)

    # ── NMEA parser (from FranzTscharf repo, upgraded to Python 3 + pynmea2) ──
    def _parse(self, sentence: str) -> None:
        """
        Parse a single NMEA sentence.
        Uses pynmea2's .latitude / .longitude decimal-degree properties
        (avoids the manual DDMM.MMMM conversion bug in the original repo).
        """
        if not sentence.startswith(("$GPGGA", "$GPRMC", "$GNRMC", "$GNGGA")):
            return
        try:
            msg = pynmea2.parse(sentence, check=False)

            # ── GPGGA: position + fix quality + altitude + satellites ──────────
            if isinstance(msg, pynmea2.types.talker.GGA):
                if msg.gps_qual == 0:
                    self._data.invalidate()
                    return
                fix = GPSFix(
                    lat        = msg.latitude,
                    lon        = msg.longitude,
                    altitude   = float(msg.altitude) if msg.altitude else 0.0,
                    satellites = int(msg.num_sats) if msg.num_sats else 0,
                    quality    = int(msg.gps_qual),
                    speed_kmh  = self._pending.get("speed_kmh", 0.0),
                    valid      = True,
                )
                self._data.update(fix)
                logger.debug("GGA lat=%.6f lon=%.6f sats=%d alt=%.1fm",
                             fix.lat, fix.lon, fix.satellites, fix.altitude)

            # ── GPRMC: position + speed over ground + validity ────────────────
            elif isinstance(msg, pynmea2.types.talker.RMC):
                if msg.status != "A":   # A = Active/valid, V = Void
                    self._data.invalidate()
                    return
                # Speed: NMEA gives knots; convert to km/h
                spd_knots = float(msg.spd_over_grnd) if msg.spd_over_grnd else 0.0
                self._pending["speed_kmh"] = spd_knots * 1.852
                # Update position from RMC as well (in case GGA is absent)
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
            pass    # sentence type has no lat/lon (e.g. GPGSV)

    # ── Simulation path ───────────────────────────────────────────────────────
    def _sim(self) -> None:
        """
        Walk from the NIT Jalandhar Main Gate towards the Admin Block.
        Each step ≈ 5 m of real movement.
        """
        logger.warning("=== SIMULATION – walking campus route, NIT Jalandhar ===")

        # Route: Main Gate → Shopping Complex → Admin Block
        waypoints = [
            (31.3945, 75.5275),   # Main Gate
            (31.3950, 75.5280),
            (31.3955, 75.5285),
            (31.3960, 75.5288),
            (31.3963, 75.5290),   # Shopping Complex
            (31.3964, 75.5292),
            (31.3965, 75.5295),
            (31.3966, 75.5298),
            (31.3967, 75.5300),
            (31.3967, 75.5303),   # Admin Block
        ]
        sat = 8   # simulate good fix
        for lat, lon in waypoints:
            if not self._running:
                break
            fix = GPSFix(lat=lat, lon=lon, altitude=233.0,
                         satellites=sat, quality=1, speed_kmh=3.5, valid=True)
            self._data.update(fix)
            logger.info("[SIM] lat=%.6f  lon=%.6f  sats=%d", lat, lon, sat)
            time.sleep(2.0)

        # Hold last position
        while self._running:
            time.sleep(2.0)


# ══════════════════════════════════════════════════════════════════════════════
# NAVIGATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════
class NavigationEngine:
    """
    Bearing, cardinal, turn instruction, and distance calculations.
    All static – no state, fully testable.
    Uses geopy.distance.geodesic (WGS-84 ellipsoid) for accuracy.
    """

    @staticmethod
    def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Forward azimuth from (lat1,lon1) to (lat2,lon2). Result: 0–360°."""
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
    def turn(current_heading: float, dest_bearing: float) -> str:
        diff = dest_bearing - current_heading
        while diff >  180: diff -= 360
        while diff <= -180: diff += 360

        if   abs(diff) < 15:      return "continue straight ahead"
        elif  15 <= diff <  45:   return "bear slightly to your right"
        elif  45 <= diff <= 120:  return "turn right"
        elif  diff > 120:         return "turn sharply right"
        elif -45 <  diff <= -15:  return "bear slightly to your left"
        elif -120 <= diff <= -45: return "turn left"
        else:                     return "turn around completely"

    @staticmethod
    def distance_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        return geodesic(a, b).meters


# ══════════════════════════════════════════════════════════════════════════════
# VOICE SCRIPTS  – all spoken text in one place for easy editing
# ══════════════════════════════════════════════════════════════════════════════
class Script:

    WELCOME = (
        "Namaskar! Welcome to Smart Specs, your campus navigation companion "
        "at NIT Jalandhar. "
        "I will help you reach any place on campus. "
        "Just a moment while I get ready."
    )
    WAITING_GPS = (
        "Getting your GPS location now. "
        "Please stay in an open area, this will be quick."
    )
    GPS_READY = (
        "Got it! Your location is locked in. "
        "We are all set, let us go."
    )
    GPS_FAILED = (
        "Sorry, I could not get a GPS signal right now. "
        "Please check the GPS module and try again outdoors."
    )
    NOT_ON_CAMPUS = (
        "You seem to be outside the campus area. "
        "I will still do my best to guide you."
    )
    ASK_DESTINATION = (
        "Where would you like to go? "
        "Just tell me the name, like library, hostel, or admin block."
    )
    LISTENING_CUE = "Go ahead, I am listening."
    SEARCHING     = "Let me look that up for you."
    NOT_FOUND_CAMPUS = (
        "Hmm, I could not find that place. "
        "Could you try a different name? "
        "For example: library, mess, BH1, or sports complex."
    )
    TIMEOUT = (
        "I did not catch that. Could you try again please?"
    )
    NOT_UNDERSTOOD = (
        "Sorry, I missed that. Please say it once more."
    )
    VOICE_UNAVAILABLE = (
        "The voice service is not available right now. "
        "Please check your internet connection."
    )
    NO_MIC = "No microphone found. Please connect one and restart."
    GPS_LOST = (
        "I have lost the GPS signal. "
        "Please stay still for a moment, I am reconnecting."
    )
    GPS_REGAINED = "Got the signal back! Let us keep going."
    NAV_ERROR     = "Oops, a small error. Let me recalculate."
    CONTINUE_PROMPT = (
        "Would you like to go somewhere else? "
        "Say yes or no."
    )

    @staticmethod
    def found_location(dest: Destination) -> str:
        address_info = f" near {dest.address.split(',')[1].strip()}" if dest.address and len(dest.address.split(',')) > 1 else ""
        return (
            f"Great, I found {dest.name}{address_info}. "
            f"Let me guide you there now."
        )

    @staticmethod
    def found_external(name: str) -> str:
        return (
            f"I found {name} nearby. "
            "Let me take you there."
        )

    @staticmethod
    def nav_start(dest: str, dist_m: int) -> str:
        return (
            f"Alright, {dest} is about {dist_m} metres away. "
            "I will guide you step by step."
        )

    # ── Varied, natural navigation phrases ────────────────────────────────
    _TURN_PHRASES = [
        "Please {turn}, towards {cardinal}. About {dist} metres to go.",
        "Go ahead and {turn}. You are heading {cardinal}, {dist} metres left.",
        "{turn} now, towards {cardinal}. Still {dist} metres away.",
        "Keep going, {turn}, direction {cardinal}. {dist} metres remaining.",
        "You are doing well. {turn}, heading {cardinal}. {dist} metres more.",
    ]

    _FIRST_PHRASES = [
        "Your destination is about {dist} metres to the {cardinal}. Start walking, I will guide you.",
        "It is {dist} metres towards {cardinal}. Go ahead and I will keep you on track.",
        "About {dist} metres in the {cardinal} direction. Let us start moving.",
    ]

    _PROGRESS_PHRASES = [
        "You are getting closer. {dist} metres to go, keep heading {cardinal}.",
        "Almost halfway there. {dist} metres remaining, {cardinal} direction.",
        "Good progress! {dist} metres left. Keep going {cardinal}.",
        "You are on the right path. {dist} metres more towards {cardinal}.",
        "Nicely done, {dist} metres remaining. Stay on course, {cardinal}.",
    ]

    @staticmethod
    def instruction(turn: str, cardinal: str, dist_m: int, update_count: int = 0) -> str:
        """Pick a varied, natural instruction based on context."""
        if update_count > 0 and update_count % 3 == 0:
            # Every 3rd update, give an encouraging progress message
            template = random.choice(Script._PROGRESS_PHRASES)
            return template.format(dist=dist_m, cardinal=cardinal)
        template = random.choice(Script._TURN_PHRASES)
        # Capitalize the turn instruction if it starts the sentence
        turn_cap = turn[0].upper() + turn[1:] if turn else turn
        return template.format(turn=turn_cap, cardinal=cardinal, dist=dist_m)

    @staticmethod
    def first_update(cardinal: str, dist_m: int) -> str:
        template = random.choice(Script._FIRST_PHRASES)
        return template.format(dist=dist_m, cardinal=cardinal)

    @staticmethod
    def arrived(name: str) -> str:
        phrases = [
            f"You have reached {name}! Great job getting here.",
            f"Here we are, {name}! Hope the walk was smooth.",
            f"We made it to {name}. I am glad I could help.",
        ]
        return random.choice(phrases)

    @staticmethod
    def retry_prompt(attempt: int, total: int) -> str:
        return (
            f"Let us try again, attempt {attempt} of {total}. "
            "Please type or say the location name clearly."
        )

    GIVE_UP = (
        "I could not get that. Let us start over. "
        "Where would you like to go?"
    )

    @staticmethod
    def shutdown(completed_journey: bool) -> str:
        if completed_journey:
            return (
                "Thank you for using Smart Specs! "
                "It was great guiding you today. Take care and see you next time!"
            )
        return (
            "Smart Specs shutting down. "
            "Take care and stay safe. See you next time!"
        )


# ══════════════════════════════════════════════════════════════════════════════
# VOICE I/O MODULE
# (Built on Uberi/speech_recognition + pyttsx3 with Windows COM fix)
# ══════════════════════════════════════════════════════════════════════════════
class VoiceIO:
    """
    Thread-safe TTS and STT.

    TTS: single dedicated worker thread owns the pyttsx3 engine.
         pythoncom.CoInitialize() called inside the worker → fixes Windows
         SAPI silent-after-first-call bug.

    STT: Google Speech Recognition with Indian English (en-IN) language code
         for best accuracy with Indian-accented English and Hindi place names.
         Falls back gracefully on network errors.
    """

    _STOP = object()

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._done           = threading.Event()
        self._done.set()

        # Configure recogniser (from Uberi/speech_recognition best practices)
        self._r = sr.Recognizer()
        self._r.energy_threshold    = Config.ENERGY_THRESHOLD
        self._r.dynamic_energy_threshold = Config.DYNAMIC_ENERGY
        self._r.pause_threshold     = 0.8   # seconds of silence = end of phrase

        # Select microphone – skip gracefully if not available
        self._mic_index: Optional[int] = None
        if not Config.TEXT_INPUT_MODE:
            self._mic_index = self._select_microphone()

        self._worker = threading.Thread(
            target=self._tts_worker, name="TTS", daemon=False
        )
        self._worker.start()

    # ── Microphone selection ──────────────────────────────────────────────────
    @staticmethod
    def _select_microphone() -> Optional[int]:
        """
        List all microphones and prefer a USB mic (best for Raspberry Pi use).
        Returns device_index or None (use system default).
        """
        try:
            names = sr.Microphone.list_microphone_names()
            logger.info("Available microphones:")
            for i, name in enumerate(names):
                logger.info("  [%d] %s", i, name)

            # Prefer USB microphone for Raspberry Pi field use
            for i, name in enumerate(names):
                nl = name.lower()
                if any(kw in nl for kw in ["usb", "respeaker", "uac", "ps3"]):
                    logger.info("Selected USB mic: [%d] %s", i, name)
                    return i
        except Exception as exc:
            logger.warning("Mic enumeration failed: %s", exc)
        return None   # fall back to default

    # ── TTS worker ────────────────────────────────────────────────────────────
    def _tts_worker(self) -> None:
        # KEY FIX: Windows COM Single-Threaded Apartment
        if IS_WINDOWS:
            try:
                import pythoncom
                pythoncom.CoInitialize()
                logger.debug("COM STA initialised.")
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

            # ── Attempt 1 & 2: pyttsx3 with FRESH engine each time ────────
            #     Creating a new engine per utterance avoids the Windows bug
            #     where runAndWait() returns instantly without producing audio
            #     on the second and subsequent calls.
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
                    logger.error("TTS pyttsx3 error (attempt %d): %s", attempt, exc)
                finally:
                    # Always destroy the engine after each utterance
                    if engine:
                        try:
                            engine.stop()
                        except Exception:
                            pass
                        del engine

            # ── Fallback: Windows SAPI5 via PowerShell ────────────────────
            if not spoke and IS_WINDOWS:
                spoke = self._speak_sapi_fallback(text)

            # ── Last resort: print to console ─────────────────────────────
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
        logger.info("TTS worker done.")

    @staticmethod
    def _new_engine() -> Optional[pyttsx3.Engine]:
        try:
            eng = pyttsx3.init()
            eng.setProperty("rate",   Config.TTS_RATE)
            eng.setProperty("volume", Config.TTS_VOLUME)
            # Prefer a female voice (clearer for navigation) if available
            voices = eng.getProperty("voices")
            for v in voices:
                if "female" in v.name.lower() or "zira" in v.name.lower():
                    eng.setProperty("voice", v.id)
                    logger.info("TTS voice: %s", v.name)
                    break
            return eng
        except Exception as exc:
            logger.error("pyttsx3 init failed: %s", exc)
            return None

    @staticmethod
    def _speak_sapi_fallback(text: str) -> bool:
        """Direct Windows SAPI5 speech via PowerShell – works even when
        pyttsx3 silently fails to produce audio."""
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
                logger.info("[TTS-SAPI] Spoke via PowerShell fallback.")
                return True
            logger.warning("[TTS-SAPI] PowerShell returned %d: %s",
                           result.returncode, result.stderr.decode(errors="replace"))
        except subprocess.TimeoutExpired:
            logger.warning("[TTS-SAPI] PowerShell speech timed out.")
        except Exception as exc:
            logger.error("[TTS-SAPI] Fallback failed: %s", exc)
        return False

    # ── Public TTS ────────────────────────────────────────────────────────────
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

    # ── Public STT ────────────────────────────────────────────────────────────
    def listen(self, prompt: Optional[str] = None,
               cue: str = Script.LISTENING_CUE) -> Optional[str]:
        """
        Speak prompt fully, then open mic and listen.
        Returns recognised text or None.

        Uses Indian English (en-IN) for best accuracy with:
        - Indian-accented English
        - Hindi campus location names (BH1, GH, SAC, OAT, etc.)
        """
        if prompt:
            self.speak_and_wait(prompt, pause=Config.POST_SPEECH_DELAY)

        mic_kwargs = {"device_index": self._mic_index} if self._mic_index else {}

        try:
            with sr.Microphone(**mic_kwargs) as source:
                logger.info("Calibrating ambient noise ...")
                self._r.adjust_for_ambient_noise(source, duration=0.8)

                self.speak_and_wait(cue, pause=0.2)
                print("\n  ──────── 🎙  SPEAK NOW ────────\n")

                audio = self._r.listen(
                    source,
                    timeout=Config.LISTEN_TIMEOUT,
                    phrase_time_limit=Config.PHRASE_TIME_LIMIT,
                )

            # Primary: Google STT with Indian English
            try:
                text = self._r.recognize_google(
                    audio, language=Config.STT_LANGUAGE
                )
                logger.info("Recognised [en-IN]: '%s'", text)
                return text.strip()
            except sr.UnknownValueError:
                # Fallback: try plain English if Indian English fails
                try:
                    text = self._r.recognize_google(audio, language="en-US")
                    logger.info("Recognised [en-US fallback]: '%s'", text)
                    return text.strip()
                except sr.UnknownValueError:
                    logger.warning("Speech not understood in either dialect.")
                    return None

        except sr.WaitTimeoutError:
            logger.warning("Listen timeout.")
            return None
        except sr.RequestError as exc:
            logger.error("Google STT API error: %s", exc)
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
    """
    NIT Jalandhar Campus Navigation System for Visually Impaired.

    Flow
    ────
    1.  Warm welcome in Hindi + English.
    2.  Start GPS, wait for fix, report satellite count.
    3.  Detect if user is on campus; advise if not.
    4.  Loop:
          a. Ask for destination (Hindi/English prompt, up to 3 retries).
          b. Resolve: campus DB → fuzzy → Nominatim geocoding.
          c. Navigate with turn-by-turn updates + hazard warnings.
          d. Warm arrival message in Hindi + English.
          e. Ask if user wants to navigate again.
    5.  Graceful Hindi + English goodbye.
    """

    def __init__(self) -> None:
        self._gps      = GPSData()
        self._handler  = GPSHandler(self._gps)
        self._voice    = VoiceIO()
        self._nav      = NavigationEngine()
        self._scene    = SceneAwareness()
        self._stop     = threading.Event()
        self._arrived  = False

    # ── GPS wait ──────────────────────────────────────────────────────────────
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
        logger.info("GPS fix: %.6f, %.6f  sats=%d  alt=%.0fm",
                    fix.lat, fix.lon, fix.satellites, fix.altitude)

        quality_msg = (
            f"GPS ready. I have {fix.satellites} satellites in view "
            f"and a good position fix."
        ) if fix.satellites >= 4 else Script.GPS_READY

        self._voice.speak_and_wait(quality_msg)

        if not SceneAwareness.is_on_campus(fix.lat, fix.lon):
            self._voice.speak(Script.NOT_ON_CAMPUS)

        return True

    # ── Destination input with smart retries ──────────────────────────────────
    def _ask_destination(self) -> Optional[str]:
        if Config.TEXT_INPUT_MODE:
            return self._ask_destination_text()
        return self._ask_destination_voice()

    def _ask_destination_text(self) -> Optional[str]:
        """Keyboard-based destination input with TTS prompt."""
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

    def _ask_destination_voice(self) -> None:
        """
        Microphone-based destination input.
        Keeps listening indefinitely until the user speaks a location.
        Never gives up — only Ctrl+C stops it.
        """
        self._voice.speak_and_wait(Script.ASK_DESTINATION)

        attempt = 0
        while not self._stop.is_set():
            attempt += 1

            # Listen for speech
            result = self._voice.listen(
                prompt=None,   # don't repeat the full prompt every time
                cue=Script.LISTENING_CUE if attempt == 1 else ""
            )

            if result:
                return result

            # Didn't hear anything — gentle reminder and try again
            reminders = [
                "I am still listening. Please say the location name.",
                "Take your time. Just say where you would like to go.",
                "I did not catch that. Please try again whenever you are ready.",
                "No hurry. Just say the name of the place clearly.",
                "I am right here, waiting. Please speak the destination.",
            ]
            reminder = reminders[attempt % len(reminders)]
            logger.info("Listen attempt %d — no response, retrying.", attempt)
            self._voice.speak_and_wait(reminder, pause=0.3)

        return None

    # ── Navigation loop ───────────────────────────────────────────────────────
    def _navigate_to(self, dest_name: str,
                     dest_coords: Tuple[float, float]) -> bool:
        """
        Guide user to destination. Returns True if arrived, False if interrupted.
        """
        fix = self._gps.current
        initial_dist = int(self._nav.distance_m(
            (fix.lat, fix.lon), dest_coords
        ))
        self._voice.speak_and_wait(Script.nav_start(dest_name, initial_dist))
        logger.info("Navigating to '%s' @ (%.6f, %.6f)", dest_name,
                    dest_coords[0], dest_coords[1])

        last_spoken    = 0.0
        gps_lost       = False
        last_hazard    = ""
        update_count   = 0        # track how many updates spoken
        last_msg       = ""       # avoid exact same message twice in a row

        while not self._stop.is_set():

            # ── GPS health ────────────────────────────────────────────────────
            if not self._gps.has_fix:
                if not gps_lost:
                    self._voice.speak(Script.GPS_LOST)
                    gps_lost = True
                time.sleep(2)
                continue
            if gps_lost:
                self._voice.speak(Script.GPS_REGAINED)
                gps_lost = False

            fix = self._gps.current
            pos = (fix.lat, fix.lon)

            try:
                dist = self._nav.distance_m(pos, dest_coords)

                # ── Arrived ───────────────────────────────────────────────────
                if dist < Config.ARRIVED_METERS:
                    self._voice.speak_and_wait(Script.arrived(dest_name))
                    logger.info("Arrived at '%s'.", dest_name)
                    return True

                # ── Hazard check (SceneAwareness) ────────────────────────────
                hazard = self._scene.check_hazards(fix.lat, fix.lon)
                if hazard and hazard != last_hazard:
                    self._voice.speak(hazard)
                    last_hazard = hazard
                    time.sleep(1)
                    continue

                # ── Build navigation instruction ──────────────────────────────
                brg      = self._nav.bearing(fix.lat, fix.lon,
                                             dest_coords[0], dest_coords[1])
                cardinal = self._nav.cardinal(brg)
                prev     = self._gps.previous
                dist_m   = int(dist)

                if (prev.valid and
                        self._nav.distance_m((prev.lat, prev.lon), pos)
                        >= Config.MIN_MOVE_METERS):
                    heading  = self._nav.bearing(prev.lat, prev.lon,
                                                 fix.lat, fix.lon)
                    turn_str = self._nav.turn(heading, brg)
                    msg      = Script.instruction(turn_str, cardinal, dist_m,
                                                  update_count)
                else:
                    msg = Script.first_update(cardinal, dist_m)

                # Avoid repeating the exact same sentence
                if msg == last_msg:
                    msg = Script.instruction(
                        "keep walking", cardinal, dist_m, update_count
                    )

                # ── Throttled speech ──────────────────────────────────────────
                now = time.monotonic()
                if now - last_spoken >= Config.UPDATE_INTERVAL:
                    logger.info("[NAV] %s", msg)
                    self._voice.speak(msg)
                    last_spoken = now
                    last_msg    = msg
                    update_count += 1

                # Log satellite / speed info periodically
                if fix.satellites:
                    logger.debug("sats=%d  spd=%.1f km/h  alt=%.0fm",
                                 fix.satellites, fix.speed_kmh, fix.altitude)

            except Exception as exc:
                logger.error("Nav loop error: %s", exc)
                self._voice.speak(Script.NAV_ERROR)

            time.sleep(0.5)

        return False

    # ── Ask continue ──────────────────────────────────────────────────────────
    def _ask_continue(self) -> bool:
        if Config.TEXT_INPUT_MODE:
            return self._ask_continue_text()
        return self._ask_continue_voice()

    def _ask_continue_text(self) -> bool:
        """Keyboard-based continue prompt with TTS."""
        self._voice.speak_and_wait(Script.CONTINUE_PROMPT)
        try:
            answer = input("\n  ▶ Navigate again? (yes/no): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if any(w in answer for w in ["no", "nahi", "nahin", "band", "exit", "stop", "done"]):
            return False
        return True

    def _ask_continue_voice(self) -> bool:
        """
        Microphone-based continue prompt.
        Keeps listening until it gets a clear yes or no.
        """
        self._voice.speak_and_wait(Script.CONTINUE_PROMPT)

        while not self._stop.is_set():
            answer = self._voice.listen(
                prompt=None,
                cue="Please say yes or no."
            )
            if answer:
                al = answer.lower()
                if any(w in al for w in ["no", "nahi", "nahin", "band",
                                          "exit", "stop", "done"]):
                    return False
                if any(w in al for w in ["yes", "haan", "ha", "sure",
                                          "ok", "yeah", "continue"]):
                    return True
                # Heard something but not yes/no — ask again
                self._voice.speak_and_wait(
                    "I heard you, but could you say yes or no?"
                )
            else:
                self._voice.speak_and_wait(
                    "I am still listening. Please say yes or no."
                )

        return False

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info("=" * 70)
        logger.info("  SMART SPECS  v3.0  –  NIT Jalandhar Campus Navigation")
        logger.info("  Platform: %s  |  Mode: %s",
                    platform.system(),
                    "SIMULATION" if Config.SIMULATION_MODE else "HARDWARE")
        logger.info("=" * 70)

        self._voice.speak_and_wait(Script.WELCOME)
        self._handler.start()

        fix_timeout = 10 if Config.SIMULATION_MODE else 60
        if not self._wait_for_fix(timeout=fix_timeout):
            logger.critical("GPS fix failed. Exiting.")
            self._shutdown()
            return

        try:
            while not self._stop.is_set():

                # 1. Get destination
                spoken = self._ask_destination()
                if not spoken or self._stop.is_set():
                    continue

                logger.info("User said: '%s'", spoken)
                self._voice.speak_and_wait(Script.SEARCHING)

                # 2. Resolve destination
                dest_name:   str
                dest_coords: Tuple[float, float]

                dest = LocationFinder.find(spoken)
                if dest:
                    dest_name   = dest.name
                    dest_coords = dest.coords
                    self._voice.speak_and_wait(Script.found_location(dest))
                else:
                    self._voice.speak_and_wait(Script.NOT_FOUND_CAMPUS)
                    continue

                # 3. Navigate
                arrived = self._navigate_to(dest_name, dest_coords)
                self._arrived = arrived

                if not arrived:
                    break  # user Ctrl+C'd mid-journey

                # 4. Ask to continue
                if not self._stop.is_set() and not self._ask_continue():
                    break

        except KeyboardInterrupt:
            logger.info("Ctrl+C – shutting down.")
        finally:
            self._shutdown()

    # ── Shutdown ──────────────────────────────────────────────────────────────
    def _shutdown(self) -> None:
        logger.info("Shutting down ...")
        self._stop.set()
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