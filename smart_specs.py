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
import sys
import time
import math
import queue
import difflib
import threading
import logging
import platform
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List

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

import serial                           # noqa: E402
import pynmea2                          # noqa: E402
import pyttsx3                          # noqa: E402
import speech_recognition as sr        # noqa: E402
from geopy.geocoders import Nominatim  # noqa: E402
from geopy.distance import geodesic    # noqa: E402


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
# NIT JALANDHAR CAMPUS LANDMARK DATABASE
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Landmark:
    name: str                           # canonical display name
    lat: float
    lon: float
    description: str                    # spoken context ("near the main road")
    aliases: List[str] = field(default_factory=list)  # alternate spoken names

    @property
    def coords(self) -> Tuple[float, float]:
        return (self.lat, self.lon)


# ─────────────────────────────────────────────────────────────────────────────
# All coordinates are approximate GPS decimal degrees for NIT Jalandhar.
# Measure with the actual device on campus to refine these values.
# ─────────────────────────────────────────────────────────────────────────────
CAMPUS_LANDMARKS: List[Landmark] = [
    # ── Entry ─────────────────────────────────────────────────────────────────
    Landmark("Main Gate",         31.3945, 75.5275,
             "the main entrance on GT Road bypass",
             ["gate", "main gate", "entrance", "mukhy dwar"]),

    Landmark("Back Gate",         31.3990, 75.5335,
             "the rear campus gate towards Bidhipur village",
            ["back gate", "rear gate", "pichla gate"]),

    # ── Academic ──────────────────────────────────────────────────────────────
    Landmark("Admin Block / Gole Building", 31.3967, 75.5303,
             "the circular administrative building at the centre of campus",
             ["admin", "admin block", "gole building", "director office",
              "administration", "gol building"]),

    Landmark("Central Library",   31.3972, 75.5298,
             "the three-storey central library",
             ["library", "central library", "pustakalaya", "lib"]),

    Landmark("CSE Department",    31.3975, 75.5315,
             "the Computer Science and Engineering department block",
             ["CSE", "computer science", "computer department", "IT",
              "information technology"]),

    Landmark("ECE Department",    31.3978, 75.5320,
             "the Electronics and Communication Engineering department",
             ["ECE", "electronics", "electronics department"]),

    Landmark("Civil Engineering", 31.3980, 75.5305,
             "the Civil Engineering department",
             ["civil", "civil department", "civil engineering"]),

    Landmark("Mechanical Engineering", 31.3982, 75.5310,
             "the Mechanical Engineering department",
             ["mechanical", "mech", "mechanical department"]),

    Landmark("Chemical Engineering", 31.3977, 75.5293,
             "the Chemical Engineering department",
             ["chemical", "chem department", "chemical engineering"]),

    Landmark("Biotechnology Department", 31.3974, 75.5290,
             "the Biotechnology department",
             ["biotech", "biotechnology", "biology department"]),

    Landmark("Physics Department", 31.3969, 75.5318,
             "the Physics department block",
             ["physics", "physics department"]),

    Landmark("Mathematics Department", 31.3971, 75.5321,
             "the Mathematics and Computing department",
             ["maths", "mathematics", "math department"]),

    Landmark("Workshop / IPE",    31.3983, 75.5295,
             "the industrial workshop and IPE department",
             ["workshop", "IPE", "production", "industrial engineering"]),

    Landmark("Seminar Hall",      31.3970, 75.5308,
             "the central seminar hall used for events and lectures",
             ["seminar hall", "seminar", "conference hall"]),

    Landmark("Lecture Hall Complex", 31.3966, 75.5312,
             "the main lecture hall complex",
             ["lecture hall", "LHC", "lecture complex", "classroom block"]),

    # ── Hostels ───────────────────────────────────────────────────────────────
    Landmark("Boys Hostel 1 (BH1)", 31.3955, 75.5318,
             "Boys Hostel 1, used for first-year students",
             ["BH1", "hostel 1", "boys hostel 1", "first year hostel"]),

    Landmark("Boys Hostel 2 (BH2)", 31.3952, 75.5322,
             "Boys Hostel 2",
             ["BH2", "hostel 2", "boys hostel 2"]),

    Landmark("Boys Hostel 3 (BH3)", 31.3949, 75.5325,
             "Boys Hostel 3",
             ["BH3", "hostel 3", "boys hostel 3"]),

    Landmark("Boys Hostel 4 (BH4)", 31.3947, 75.5328,
             "Boys Hostel 4",
             ["BH4", "hostel 4", "boys hostel 4"]),

    Landmark("Boys Hostel 6 (BH6)", 31.3955, 75.5332,
             "Boys Hostel 6",
             ["BH6", "hostel 6", "boys hostel 6"]),

    Landmark("Boys Hostel 7 (BH7)", 31.3952, 75.5335,
             "Boys Hostel 7",
             ["BH7", "hostel 7", "boys hostel 7"]),

    Landmark("Mega Boys Hostel – A Block (MBH-A)", 31.3943, 75.5310,
             "Mega Boys Hostel A Block",
             ["MBH", "MBH-A", "mega boys hostel", "mega hostel a"]),

    Landmark("Mega Boys Hostel – B Block (MBH-B)", 31.3941, 75.5315,
             "Mega Boys Hostel B Block",
             ["MBH-B", "mega hostel b", "mega boys b"]),

    Landmark("Mega Boys Hostel – F Block (MBH-F)", 31.3939, 75.5320,
             "Mega Boys Hostel F Block",
             ["MBH-F", "mega hostel f", "mega boys f"]),

    Landmark("Girls Hostel 1 (GH1)", 31.3960, 75.5340,
             "Girls Hostel 1, for first-year female students",
             ["GH1", "girls hostel 1", "girls hostel one"]),

    Landmark("Girls Hostel 2 (GH2)", 31.3957, 75.5343,
             "Girls Hostel 2",
             ["GH2", "girls hostel 2", "girls hostel two"]),

    Landmark("Mega Girls Hostel (MGH)", 31.3954, 75.5346,
             "Mega Girls Hostel, for senior female students",
             ["MGH", "mega girls hostel", "mega girls"]),

    # ── Facilities ────────────────────────────────────────────────────────────
    Landmark("Student Activity Centre (SAC)", 31.3950, 75.5305,
             "the Student Activity Centre with gym, club rooms and open-air theatre",
             ["SAC", "activity centre", "gym", "student centre", "student activity"]),

    Landmark("Open Air Theatre (OAT)", 31.3948, 75.5308,
             "the Open Air Theatre with seating for 1000 people",
             ["OAT", "open air theatre", "amphitheatre", "theatre"]),

    Landmark("Shopping Complex",  31.3963, 75.5290,
             "the campus shopping complex with book shop and Xerox",
             ["shopping complex", "market", "canteen", "shops", "dukaan",
              "book shop", "bookshop", "xerox"]),

    Landmark("Dispensary / Health Centre", 31.3968, 75.5285,
             "the campus health centre and dispensary",
             ["dispensary", "health centre", "hospital", "doctor",
              "medical", "clinic"]),

    Landmark("Guest House",       31.3980, 75.5288,
             "the institute guest house",
             ["guest house", "guesthouse", "atithi bhavan"]),

    Landmark("Post Office",       31.3963, 75.5286,
             "the NIT campus post office",
             ["post office", "dak ghar", "post"]),

    Landmark("Canara Bank ATM",   31.3966, 75.5292,
             "the Canara Bank branch and ATM",
             ["canara bank", "bank", "ATM", "Canara ATM"]),

    Landmark("State Bank ATM (SBI)", 31.3964, 75.5294,
             "the State Bank of India branch",
             ["SBI", "state bank", "SBI ATM"]),

    # ── Sports ────────────────────────────────────────────────────────────────
    Landmark("Sports Complex",    31.3942, 75.5295,
             "the outdoor sports complex with 400-metre track, football and cricket ground",
             ["sports complex", "ground", "sports", "football ground",
              "cricket ground", "track"]),

    Landmark("Swimming Pool",     31.3940, 75.5300,
             "the international-standard swimming pool with diving arena",
             ["swimming pool", "pool", "tairaki", "swimming"]),

    Landmark("Basketball Courts", 31.3945, 75.5288,
             "the flood-lit basketball courts",
             ["basketball", "basketball court"]),

    Landmark("Tennis Courts",     31.3947, 75.5285,
             "the lawn tennis courts",
             ["tennis", "tennis courts", "lawn tennis"]),

    Landmark("Badminton Hall",    31.3950, 75.5300,
             "the indoor badminton hall with four wooden courts",
             ["badminton", "badminton hall", "indoor sports"]),

    # ── Canteen / Food ────────────────────────────────────────────────────────
    Landmark("Boys Hostel Mess",  31.3953, 75.5327,
             "the main boys hostel mess and canteen",
             ["mess", "boys mess", "hostel mess", "khana"]),

    Landmark("Cafeteria",         31.3965, 75.5300,
             "the central campus cafeteria near the lecture halls",
             ["cafeteria", "food court", "central canteen", "chai"]),
]

# Build a flat lookup dictionary: every alias → Landmark
_ALIAS_MAP: Dict[str, Landmark] = {}
for _lm in CAMPUS_LANDMARKS:
    _ALIAS_MAP[_lm.name.lower()] = _lm
    for _alias in _lm.aliases:
        _ALIAS_MAP[_alias.lower()] = _lm


# ══════════════════════════════════════════════════════════════════════════════
# CAMPUS LANDMARK RESOLVER
# ══════════════════════════════════════════════════════════════════════════════
class LandmarkResolver:
    """
    Resolves a spoken place name to a campus Landmark.
    Uses exact → prefix → fuzzy matching in order.
    Falls back to Nominatim geocoding if no campus match found.
    """

    @staticmethod
    def resolve(spoken: str) -> Optional[Landmark]:
        key = spoken.lower().strip()

        # 1. Exact match
        if key in _ALIAS_MAP:
            return _ALIAS_MAP[key]

        # 2. Substring match (user said part of the name)
        for alias, lm in _ALIAS_MAP.items():
            if key in alias or alias in key:
                return lm

        # 3. Fuzzy match (handles mispronunciation / STT errors)
        all_keys = list(_ALIAS_MAP.keys())
        matches = difflib.get_close_matches(
            key, all_keys, n=1, cutoff=Config.FUZZY_THRESHOLD
        )
        if matches:
            lm = _ALIAS_MAP[matches[0]]
            logger.info("Fuzzy matched '%s' → '%s'", spoken, lm.name)
            return lm

        return None     # not found in campus DB

    @staticmethod
    def geocode_external(place_name: str) -> Optional[Tuple[float, float]]:
        """Fall-back: geocode via OpenStreetMap Nominatim."""
        try:
            geolocator = Nominatim(user_agent="smart_specs_nitj_v3")
            query = f"{place_name}, Jalandhar, Punjab, India"
            location = geolocator.geocode(query, timeout=10)
            if location:
                logger.info("Geocoded '%s' → (%.6f, %.6f)",
                            place_name, location.latitude, location.longitude)
                return (location.latitude, location.longitude)
            logger.warning("Nominatim returned nothing for: %s", query)
        except Exception as exc:
            logger.error("Geocoding error: %s", exc)
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
        "Namaskar! Welcome to Smart Specs, your personal navigation assistant "
        "at Dr. B. R. Ambedkar National Institute of Technology, Jalandhar. "
        "I am here to guide you safely to any location on campus. "
        "Please give me a moment while I set everything up."
    )
    WAITING_GPS = (
        "I am acquiring your GPS signal. "
        "Please hold the device steady and ensure you are in an open area. "
        "This will only take a few seconds."
    )
    GPS_READY = (
        "GPS signal acquired. I have a strong fix with good satellite coverage. "
        "The system is fully ready. Let us begin."
    )
    GPS_FAILED = (
        "I am sorry, I was unable to acquire a GPS signal. "
        "Please ensure the GPS module is properly connected and try again in an open area. "
        "Shutting down for now."
    )
    NOT_ON_CAMPUS = (
        "It appears you may be outside the NIT Jalandhar campus boundary. "
        "Campus navigation works best within the 154-acre campus area. "
        "I will still try to guide you."
    )
    ASK_DESTINATION = (
        "Aap kahan jaana chahte hain? "  # Hindi: Where do you want to go?
        "Where would you like to go on campus? "
        "You can say the name of any building, hostel, department, or facility. "
        "For example: library, admin block, boys hostel 1, sports complex, "
        "or shopping complex."
    )
    LISTENING_CUE = "Please speak now."
    SEARCHING     = "Ji haan – Let me find that for you. One moment please."
    NOT_FOUND_CAMPUS = (
        "I am sorry, I could not find that location on the NIT Jalandhar campus. "
        "Please try again with a different name. "
        "For example you can say: library, admin block, BH1, mess, or dispensary."
    )
    TIMEOUT = (
        "Maine sunaa nahin – I did not quite catch that. "
        "Please speak clearly and close to the microphone and try again."
    )
    NOT_UNDERSTOOD = (
        "I am sorry, I could not understand. Please repeat slowly and clearly."
    )
    VOICE_UNAVAILABLE = (
        "Voice service is temporarily unavailable. "
        "Please check your internet connection and try again."
    )
    NO_MIC = "No microphone detected. Please connect a microphone and restart."
    GPS_LOST = (
        "GPS signal lost. Please stay where you are for a moment. "
        "I am trying to reconnect."
    )
    GPS_REGAINED = "GPS signal restored. Let us continue."
    NAV_ERROR     = "A small navigation error occurred. Retrying."
    CONTINUE_PROMPT = (
        "Aur kahan jaana hai? Do you need to go anywhere else on campus? "
        "Please say yes to navigate again or no to exit."
    )

    @staticmethod
    def found_campus(lm: Landmark) -> str:
        return (
            f"Found it! {lm.name} — {lm.description}. "
            f"I will now guide you there step by step. "
            f"Please walk at your own comfortable pace."
        )

    @staticmethod
    def found_external(name: str) -> str:
        return (
            f"I found {name} near the campus. "
            "Navigating now. Please follow my instructions carefully."
        )

    @staticmethod
    def nav_start(dest: str, dist_m: int) -> str:
        return (
            f"We are heading to {dest}. "
            f"The total distance is approximately {dist_m} metres. "
            "I will give you direction updates every few seconds."
        )

    @staticmethod
    def instruction(turn: str, cardinal: str, dist_m: int) -> str:
        return (
            f"Please {turn}, heading {cardinal}. "
            f"Distance remaining: {dist_m} metres."
        )

    @staticmethod
    def first_update(cardinal: str, dist_m: int) -> str:
        return (
            f"Your destination is {dist_m} metres to the {cardinal}. "
            "Please start walking and I will provide turn-by-turn directions."
        )

    @staticmethod
    def arrived(name: str) -> str:
        return (
            f"Aap pahunch gaye! You have arrived at {name}! "
            "I am delighted to have guided you here safely. "
            "Please take a moment to orient yourself. "
            "Have a wonderful time, and I am here whenever you need me again."
        )

    @staticmethod
    def retry_prompt(attempt: int, total: int) -> str:
        return (
            f"Let us try once more — attempt {attempt} of {total}. "
            "Please say the campus location name slowly and clearly."
        )

    GIVE_UP = (
        "I was not able to understand after several attempts. "
        "Let us start fresh — where would you like to go?"
    )

    @staticmethod
    def shutdown(completed_journey: bool) -> str:
        if completed_journey:
            return (
                "Dhanyavaad! Thank you so much for using Smart Specs today. "
                "It was a true honour to be your guide on campus. "
                "Please stay safe, and do come back whenever you need assistance. "
                "Alvida — goodbye!"
            )
        return (
            "Smart Specs shutting down. "
            "Thank you for using this system. "
            "Please take care and stay safe. Alvida — goodbye!"
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
        # Print available landmarks for user reference
        print("\n" + "═" * 60)
        print("  AVAILABLE CAMPUS LOCATIONS:")
        print("═" * 60)
        for lm in CAMPUS_LANDMARKS:
            print(f"  • {lm.name}")
        print("═" * 60)

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
        """Microphone-based destination input (original flow)."""
        result = self._voice.listen(prompt=Script.ASK_DESTINATION)
        if result:
            return result

        for attempt in range(2, Config.MAX_RETRIES + 1):
            if self._stop.is_set():
                return None
            msg = (Script.TIMEOUT if attempt == 2 else
                   Script.retry_prompt(attempt, Config.MAX_RETRIES))
            result = self._voice.listen(prompt=msg)
            if result:
                return result

        self._voice.speak_and_wait(Script.GIVE_UP)
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
        logger.info("Navigating to '%s' @ %s", dest_name, dest_coords)

        last_spoken  = 0.0
        gps_lost     = False
        last_hazard  = ""

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
                    msg      = Script.instruction(turn_str, cardinal, dist_m)
                else:
                    msg = Script.first_update(cardinal, dist_m)

                # ── Throttled speech ──────────────────────────────────────────
                now = time.monotonic()
                if now - last_spoken >= Config.UPDATE_INTERVAL:
                    logger.info("[NAV] %s", msg)
                    self._voice.speak(msg)
                    last_spoken = now

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
        """Microphone-based continue prompt (original flow)."""
        answer = self._voice.listen(
            prompt=Script.CONTINUE_PROMPT,
            cue="Please say yes or no."
        )
        if not answer:
            return True   # no response → assume yes
        al = answer.lower()
        if any(w in al for w in ["no", "nahi", "nahin", "band", "exit", "stop", "done"]):
            return False
        return True

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

                lm = LandmarkResolver.resolve(spoken)
                if lm:
                    dest_name   = lm.name
                    dest_coords = lm.coords
                    self._voice.speak_and_wait(Script.found_campus(lm))
                else:
                    # Try Nominatim for off-campus or unrecognised names
                    coords = LandmarkResolver.geocode_external(spoken)
                    if coords:
                        dest_name   = spoken.title()
                        dest_coords = coords
                        self._voice.speak_and_wait(Script.found_external(dest_name))
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