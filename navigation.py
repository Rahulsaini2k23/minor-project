#!/usr/bin/env python3
"""
Smart Specs Navigation System for Visually Impaired
====================================================
Author: AI Assistant
Version: 2.2
Compatibility: Python 3.8+  |  Windows (testing) + Raspberry Pi (production)
Description: GPS-based navigation with warm, interactive Voice I/O

Dependencies:
    pip install pynmea2 pyttsx3 speechrecognition geopy pyaudio pyserial

Hardware (production):
    - Raspberry Pi with UART GPS module on /dev/serial0
    - USB Microphone + Speaker
"""

# ==========================================
# IMPORTS
# ==========================================
import sys
import time
import math
import queue
import threading
import logging
import platform
from typing import Optional, Tuple

# ── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("navigation.log", mode="w", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# ── Dependency Validation ─────────────────────────────────────────────────────
def _require(import_name: str, install_name: str) -> None:
    try:
        __import__(import_name)
    except ImportError:
        logger.critical(
            "Required package '%s' not found. Install with:  pip install %s",
            import_name, install_name,
        )
        sys.exit(1)

_require("serial",             "pyserial")
_require("pynmea2",            "pynmea2")
_require("pyttsx3",            "pyttsx3")
_require("speech_recognition", "speechrecognition")
_require("geopy",              "geopy")

import serial                          # noqa: E402
import pynmea2                         # noqa: E402
import pyttsx3                         # noqa: E402
import speech_recognition as sr       # noqa: E402
from geopy.geocoders import Nominatim # noqa: E402
from geopy.distance import geodesic   # noqa: E402


# ==========================================
# CONFIGURATION
# ==========================================
class Config:
    """Single-source configuration. Edit these values to customise behaviour."""

    # ── Mode ──────────────────────────────
    SIMULATION_MODE: bool = True        # False = real GPS hardware

    # ── GPS ───────────────────────────────
    GPS_PORT: str    = "/dev/serial0"
    BAUD_RATE: int   = 9600
    GPS_TIMEOUT: int = 5

    # ── Navigation ────────────────────────
    UPDATE_INTERVAL: float = 4.0        # seconds between voice instructions
    DESTINATION_REACHED_METERS: int = 15

    # ── Voice ─────────────────────────────
    TTS_RATE: int     = 140             # words per minute (slightly slower = clearer)
    TTS_VOLUME: float = 1.0
    POST_SPEECH_DELAY: float = 0.5      # silence after TTS before mic opens

    # ── Geocoding ─────────────────────────
    GEOCODE_REGION: str     = "UK"
    GEOCODE_USER_AGENT: str = "smart_specs_navigation_v2"

    # ── Retry limits ──────────────────────
    MAX_LISTEN_RETRIES: int = 3         # before asking user if they want to continue


# ==========================================
# VOICE SCRIPT  –  all spoken phrases in one place
# ==========================================
class Script:
    """
    Central repository of every phrase the system speaks.
    Keeping them here makes it easy to adjust tone or translate.
    """

    STARTUP = (
        "Hello! Welcome to Smart Specs, your personal navigation assistant. "
        "I am here to guide you safely to wherever you need to go. "
        "Please give me just a moment while I set things up for you."
    )

    WAITING_GPS = (
        "I am acquiring your GPS signal right now. "
        "Please hold the device steady — this will only take a few seconds."
    )

    GPS_READY = (
        "Wonderful! I have a strong GPS signal and everything is ready. "
        "I am excited to help you get where you need to be today."
    )

    GPS_FAILED = (
        "I am sorry, I was unable to find a GPS signal. "
        "Could you please check that the GPS module is connected, "
        "then restart the device? I apologise for the inconvenience."
    )

    # Called at the start of every destination request
    ASK_DESTINATION = (
        "Where would you like to go today? "
        "Please say the name of your destination clearly after the beep, "
        "for example: 'Buckingham Palace' or 'Hyde Park'."
    )

    # Called when the mic is open and recording
    LISTENING_CUE = (
        "I am listening now. Please speak your destination clearly."
    )

    SEARCHING = "Alright, let me find that for you. One moment please."

    @staticmethod
    def found(name: str) -> str:
        return (
            f"Perfect! I found {name}. "
            "I will now begin guiding you there step by step. "
            "Please listen carefully to my instructions."
        )

    NOT_FOUND = (
        "I am sorry, I was not able to find that location. "
        "Could you please try saying it again, perhaps a little differently? "
        "For example, you could include the city name."
    )

    TIMEOUT = (
        "I did not quite catch that. "
        "Please do not worry — let us try again. "
        "Speak clearly and close to the microphone."
    )

    NOT_UNDERSTOOD = (
        "I am sorry, I could not understand what you said. "
        "Please try once more, slowly and clearly."
    )

    VOICE_UNAVAILABLE = (
        "I am having trouble connecting to the voice recognition service. "
        "Please check that you are connected to the internet, then try again."
    )

    NO_MIC = (
        "I cannot detect a microphone. "
        "Please connect a microphone and restart the system."
    )

    @staticmethod
    def retry_prompt(attempt: int, max_attempts: int) -> str:
        return (
            f"Let us try once more. Attempt {attempt} of {max_attempts}. "
            "Please say your destination clearly."
        )

    GIVE_UP = (
        "I am very sorry, I was unable to understand the destination after several tries. "
        "Let us start fresh. Where would you like to go?"
    )

    GPS_LOST = (
        "Oh dear, I seem to have lost the GPS signal. "
        "Please stay where you are for a moment while I try to reconnect."
    )

    GPS_REGAINED = (
        "Excellent! I have regained the GPS signal. "
        "Let us continue navigating."
    )

    @staticmethod
    def nav_start(name: str) -> str:
        return (
            f"We are on our way to {name}. "
            "I will let you know when to turn and how far you have to go. "
            "Take your time, there is no rush."
        )

    @staticmethod
    def distance_update(instruction: str, cardinal: str, distance_m: int) -> str:
        return (
            f"{instruction}, heading {cardinal}. "
            f"You are {distance_m} metres from your destination."
        )

    @staticmethod
    def first_update(cardinal: str, distance_m: int) -> str:
        return (
            f"Your destination is {distance_m} metres to the {cardinal}. "
            "Please start walking and I will guide you turn by turn."
        )

    @staticmethod
    def arrived(name: str) -> str:
        return (
            f"You have arrived at {name}! "
            "I am so glad I could guide you here safely. "
            "It has been a genuine pleasure assisting you today. "
            "Please take care of yourself, and whenever you need me again, "
            "I will be right here ready to help. "
            "Have a wonderful day. Goodbye for now!"
        )

    NAVIGATION_ERROR = (
        "I encountered a small issue with navigation. "
        "Please do not worry — I am retrying right now."
    )

    @staticmethod
    def shutdown(arrived: bool) -> str:
        if arrived:
            return (
                "Thank you so much for using Smart Specs today. "
                "It was truly my honour to guide you. "
                "Please stay safe, and do not hesitate to use me again anytime. "
                "Take great care. Goodbye!"
            )
        else:
            return (
                "Shutting down now. "
                "Thank you for using Smart Specs. "
                "I hope I was helpful today. "
                "Please stay safe, and I will be here whenever you need me. "
                "Goodbye for now!"
            )

    CONTINUE_PROMPT = (
        "Would you like to navigate somewhere else? "
        "Just say 'yes' to continue or 'no' to exit."
    )


# ==========================================
# THREAD-SAFE GPS DATA STORE
# ==========================================
class GPSData:
    """Thread-safe container for the latest GPS fix."""

    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._lat:      Optional[float] = None
        self._lon:      Optional[float] = None
        self._prev_lat: Optional[float] = None
        self._prev_lon: Optional[float] = None
        self._fix:      bool            = False

    def update(self, lat: float, lon: float) -> None:
        with self._lock:
            self._prev_lat = self._lat
            self._prev_lon = self._lon
            self._lat      = lat
            self._lon      = lon
            self._fix      = True

    def set_fix(self, state: bool) -> None:
        with self._lock:
            self._fix = state

    @property
    def has_fix(self) -> bool:
        with self._lock:
            return self._fix

    @property
    def position(self) -> Optional[Tuple[float, float]]:
        with self._lock:
            if self._lat is None or self._lon is None:
                return None
            return (self._lat, self._lon)

    @property
    def previous_position(self) -> Optional[Tuple[float, float]]:
        with self._lock:
            if self._prev_lat is None or self._prev_lon is None:
                return None
            return (self._prev_lat, self._prev_lon)


# ==========================================
# GPS HANDLER
# ==========================================
class GPSHandler:
    """Reads NMEA sentences from hardware or simulates movement for testing."""

    def __init__(self, gps_data: GPSData) -> None:
        self._data    = gps_data
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._serial: Optional[serial.Serial]    = None

    def start(self) -> None:
        self._running = True
        target = self._run_simulation if Config.SIMULATION_MODE else self._run_real_gps
        self._thread = threading.Thread(target=target, name="GPSThread", daemon=True)
        self._thread.start()
        logger.info("GPS started  [mode=%s]",
                    "SIMULATION" if Config.SIMULATION_MODE else "HARDWARE")

    def stop(self) -> None:
        self._running = False
        if self._serial and self._serial.is_open:
            self._serial.close()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("GPS stopped.")

    def _run_real_gps(self) -> None:
        try:
            self._serial = serial.Serial(
                Config.GPS_PORT, Config.BAUD_RATE, timeout=Config.GPS_TIMEOUT
            )
            logger.info("Opened %s at %d baud", Config.GPS_PORT, Config.BAUD_RATE)
        except serial.SerialException as exc:
            logger.error("Cannot open GPS port: %s", exc)
            self._data.set_fix(False)
            return

        while self._running:
            try:
                if self._serial.in_waiting > 0:
                    raw = self._serial.readline().decode("utf-8", errors="ignore").strip()
                    self._parse_nmea(raw)
            except serial.SerialException as exc:
                logger.error("Serial read error: %s", exc)
                self._data.set_fix(False)
                time.sleep(1)
            except Exception as exc:
                logger.debug("GPS error: %s", exc)

    def _parse_nmea(self, sentence: str) -> None:
        if not sentence.startswith(("$GPRMC", "$GPGGA", "$GNRMC", "$GNGGA")):
            return
        try:
            msg = pynmea2.parse(sentence)
            lat: float = msg.latitude   # type: ignore[attr-defined]
            lon: float = msg.longitude  # type: ignore[attr-defined]
            if lat == 0.0 and lon == 0.0:
                return
            self._data.update(lat, lon)
            logger.debug("GPS: %.6f, %.6f", lat, lon)
        except (pynmea2.ParseError, AttributeError) as exc:
            logger.debug("NMEA parse skip: %s", exc)

    def _run_simulation(self) -> None:
        logger.warning("=== SIMULATION MODE – no real GPS hardware ===")
        sim_lat, sim_lon = 51.5074, -0.1278  # London, UK
        while self._running:
            sim_lat += 0.0002
            sim_lon += 0.0001
            self._data.update(sim_lat, sim_lon)
            logger.info("[SIM] %.6f, %.6f", sim_lat, sim_lon)
            time.sleep(1.5)


# ==========================================
# NAVIGATION ENGINE
# ==========================================
class NavigationEngine:
    """Stateless navigation calculations."""

    @staticmethod
    def calculate_bearing(lat1: float, lon1: float,
                          lat2: float, lon2: float) -> float:
        lat1_r  = math.radians(lat1)
        lat2_r  = math.radians(lat2)
        d_lon_r = math.radians(lon2 - lon1)
        x = math.sin(d_lon_r) * math.cos(lat2_r)
        y = (math.cos(lat1_r) * math.sin(lat2_r)
             - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(d_lon_r))
        return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0

    @staticmethod
    def bearing_to_cardinal(bearing: float) -> str:
        directions = [
            "North", "North-East", "East", "South-East",
            "South", "South-West", "West", "North-West",
        ]
        return directions[round(bearing / 45.0) % 8]

    @staticmethod
    def get_turn_instruction(current_heading: float, dest_bearing: float) -> str:
        diff = dest_bearing - current_heading
        while diff >  180: diff -= 360
        while diff <= -180: diff += 360

        if   abs(diff) < 15:      return "Continue straight ahead"
        elif  15 <= diff <   45:  return "Bear slightly to the right"
        elif  45 <= diff <=  120: return "Turn right"
        elif       diff >   120:  return "Turn sharply right"
        elif -45 <  diff <=  -15: return "Bear slightly to the left"
        elif -120 <= diff <= -45: return "Turn left"
        else:                     return "Turn around"

    @staticmethod
    def geocode(place_name: str) -> Optional[Tuple[float, float]]:
        try:
            geolocator = Nominatim(user_agent=Config.GEOCODE_USER_AGENT)
            query    = f"{place_name}, {Config.GEOCODE_REGION}"
            location = geolocator.geocode(query, timeout=10)
            if location:
                logger.info("Geocoded '%s' -> (%.6f, %.6f)",
                            place_name, location.latitude, location.longitude)
                return (location.latitude, location.longitude)
            logger.warning("No geocoding result for: %s", query)
            return None
        except Exception as exc:
            logger.error("Geocoding error: %s", exc)
            return None


# ==========================================
# VOICE I/O MODULE
# ==========================================
class VoiceIO:
    """
    Thread-safe Text-to-Speech and Speech-to-Text.

    Windows COM fix
    ---------------
    pyttsx3 on Windows uses SAPI COM which requires pythoncom.CoInitialize()
    to be called in any background thread that uses the engine.
    Without it, runAndWait() silently does nothing after the first call.
    """

    _STOP = object()

    def __init__(self) -> None:
        self._q: queue.Queue     = queue.Queue()
        self._done               = threading.Event()
        self._done.set()
        self._recogniser         = sr.Recognizer()
        self._worker             = threading.Thread(
            target=self._tts_worker, name="TTSWorker", daemon=False
        )
        self._worker.start()

    # ── TTS worker ────────────────────────
    def _tts_worker(self) -> None:
        if IS_WINDOWS:
            try:
                import pythoncom
                pythoncom.CoInitialize()
                logger.debug("COM STA initialised for TTS thread.")
            except Exception as exc:
                logger.warning("pythoncom.CoInitialize() skipped: %s", exc)

        engine = self._create_engine()

        while True:
            item = self._q.get()
            if item is self._STOP:
                self._q.task_done()
                break

            text: str = item
            logger.info("[TTS] %s", text)

            spoke = False
            for attempt in range(1, 3):
                try:
                    if engine is None:
                        engine = self._create_engine()
                    if engine:
                        engine.say(text)
                        engine.runAndWait()
                        spoke = True
                        break
                except Exception as exc:
                    logger.error("TTS error (attempt %d/2): %s", attempt, exc)
                    engine = self._restart_engine(engine)

            if not spoke:
                print(f"\n[SPEECH]: {text}\n")

            self._done.set()
            self._q.task_done()

        if engine:
            try:
                engine.stop()
            except Exception:
                pass
        if IS_WINDOWS:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
        logger.info("TTS worker exiting.")

    @staticmethod
    def _create_engine() -> Optional[pyttsx3.Engine]:
        try:
            eng = pyttsx3.init()
            eng.setProperty("rate",   Config.TTS_RATE)
            eng.setProperty("volume", Config.TTS_VOLUME)
            logger.info("pyttsx3 engine ready  [rate=%d, vol=%.1f]",
                        Config.TTS_RATE, Config.TTS_VOLUME)
            return eng
        except Exception as exc:
            logger.error("pyttsx3 init failed: %s", exc)
            return None

    @staticmethod
    def _restart_engine(old: Optional[pyttsx3.Engine]) -> Optional[pyttsx3.Engine]:
        if old:
            try:
                old.stop()
            except Exception:
                pass
        time.sleep(0.25)
        return VoiceIO._create_engine()

    # ── Public TTS ────────────────────────
    def speak(self, text: str) -> None:
        """Queue a phrase for background playback. Returns immediately."""
        self._done.clear()
        self._q.put(text)

    def speak_and_wait(self, text: str, extra_silence: float = 0.0) -> None:
        """Queue a phrase and block until it has been fully spoken."""
        self.speak(text)
        self._done.wait()
        if extra_silence > 0.0:
            time.sleep(extra_silence)

    def stop(self) -> None:
        self._q.put(self._STOP)
        self._worker.join(timeout=10)

    # ── Public STT ────────────────────────
    def listen(self, prompt: Optional[str] = None,
               cue: Optional[str] = None) -> Optional[str]:
        """
        Speak prompt (if given), then listen for a spoken command.

        prompt  – spoken before the mic opens (e.g. "Where do you want to go?")
        cue     – short spoken cue right before recording starts
                  (e.g. "I am listening now.")

        Returns recognised text or None on failure.
        """
        if prompt:
            self.speak_and_wait(prompt, extra_silence=Config.POST_SPEECH_DELAY)

        try:
            with sr.Microphone() as source:
                logger.info("Adjusting for ambient noise ...")
                self._recogniser.adjust_for_ambient_noise(source, duration=0.8)

                # Speak the cue while the mic is open so there is no gap
                if cue:
                    self.speak_and_wait(cue, extra_silence=0.2)

                logger.info("Listening ...")
                print("\n  >> Speak now ... <<\n")   # visual cue on-screen too
                audio = self._recogniser.listen(source, timeout=8, phrase_time_limit=12)

            recognised: str = self._recogniser.recognize_google(audio)  # type: ignore[attr-defined]
            logger.info("Recognised: '%s'", recognised)
            return recognised.strip()

        except sr.WaitTimeoutError:
            logger.warning("Listen timeout.")
            return None
        except sr.UnknownValueError:
            logger.warning("Speech not understood.")
            return None
        except sr.RequestError as exc:
            logger.error("Google Speech API error: %s", exc)
            self.speak_and_wait(Script.VOICE_UNAVAILABLE)
            return None
        except OSError as exc:
            logger.error("Microphone error: %s", exc)
            self.speak_and_wait(Script.NO_MIC)
            return None
        except Exception as exc:
            logger.error("Unexpected listen error: %s", exc)
            return None


# ==========================================
# MAIN APPLICATION
# ==========================================
class SmartSpecsApp:
    """
    Top-level application controller.

    Lifecycle
    ---------
    1. Warm welcome.
    2. Start GPS, wait for fix.
    3. Loop:
         a. Ask for destination (with retries and helpful prompts).
         b. Geocode.
         c. Navigate with polite turn-by-turn instructions.
         d. Warm arrival farewell.
         e. Ask if the user wants to go somewhere else.
    4. Graceful shutdown with a heartfelt goodbye.
    """

    def __init__(self) -> None:
        self._gps_data   = GPSData()
        self._gps        = GPSHandler(self._gps_data)
        self._voice      = VoiceIO()
        self._nav        = NavigationEngine()
        self._stop       = threading.Event()
        self._arrived    = False       # tracks whether user completed a journey

    # ── GPS acquisition ───────────────────────────────────────────────────────
    def _wait_for_gps_fix(self, timeout: int = 30) -> bool:
        self._voice.speak_and_wait(Script.WAITING_GPS)
        logger.info("Waiting for GPS fix (timeout=%ds) ...", timeout)

        deadline = time.monotonic() + timeout
        while not self._gps_data.has_fix:
            if time.monotonic() > deadline:
                self._voice.speak_and_wait(Script.GPS_FAILED)
                return False
            time.sleep(0.5)

        logger.info("GPS fix acquired.")
        self._voice.speak_and_wait(Script.GPS_READY)
        return True

    # ── Destination input with retries ────────────────────────────────────────
    def _ask_for_destination(self) -> Optional[str]:
        """
        Ask the user for a destination with up to MAX_LISTEN_RETRIES attempts.
        Provides clear, warm feedback on each failure.
        Returns the recognised text or None if all attempts fail.
        """
        # First attempt – full polite ask
        result = self._voice.listen(
            prompt=Script.ASK_DESTINATION,
            cue=Script.LISTENING_CUE,
        )
        if result:
            return result

        # Retry loop with increasingly gentle prompts
        for attempt in range(2, Config.MAX_LISTEN_RETRIES + 1):
            if self._stop.is_set():
                return None

            # Decide which error phrase to say based on what went wrong
            # (both timeout and not-understood land here as None)
            retry_msg = Script.retry_prompt(attempt, Config.MAX_LISTEN_RETRIES)
            result = self._voice.listen(
                prompt=retry_msg,
                cue=Script.LISTENING_CUE,
            )
            if result:
                return result

        self._voice.speak_and_wait(Script.GIVE_UP)
        return None

    # ── Navigation loop ───────────────────────────────────────────────────────
    def _navigate_to(self, destination_name: str,
                     destination_coords: Tuple[float, float]) -> bool:
        """
        Issue voice guidance until destination is reached or stop is requested.
        Returns True if the user arrived, False if navigation was interrupted.
        """
        self._voice.speak_and_wait(Script.nav_start(destination_name))
        logger.info("Navigating to '%s' at %s", destination_name, destination_coords)

        last_spoken    = 0.0
        gps_was_lost   = False

        while not self._stop.is_set():

            # ── GPS health ────────────────────────────────────────────────────
            if not self._gps_data.has_fix:
                if not gps_was_lost:
                    self._voice.speak(Script.GPS_LOST)
                    gps_was_lost = True
                time.sleep(2)
                continue

            if gps_was_lost:
                self._voice.speak(Script.GPS_REGAINED)
                gps_was_lost = False

            position = self._gps_data.position
            if position is None:
                time.sleep(0.5)
                continue

            try:
                # ── Arrival check ─────────────────────────────────────────────
                distance_m = int(geodesic(position, destination_coords).meters)

                if distance_m < Config.DESTINATION_REACHED_METERS:
                    self._voice.speak_and_wait(Script.arrived(destination_name))
                    logger.info("Arrived at '%s'.", destination_name)
                    return True     # <-- journey completed successfully

                # ── Build instruction ─────────────────────────────────────────
                bearing  = self._nav.calculate_bearing(
                    position[0], position[1],
                    destination_coords[0], destination_coords[1],
                )
                cardinal = self._nav.bearing_to_cardinal(bearing)
                previous = self._gps_data.previous_position

                if previous is not None and previous != position:
                    heading = self._nav.calculate_bearing(
                        previous[0], previous[1], position[0], position[1]
                    )
                    turn        = self._nav.get_turn_instruction(heading, bearing)
                    instruction = Script.distance_update(turn, cardinal, distance_m)
                else:
                    instruction = Script.first_update(cardinal, distance_m)

                # ── Throttled speech ──────────────────────────────────────────
                now = time.monotonic()
                if now - last_spoken >= Config.UPDATE_INTERVAL:
                    logger.info("[NAV] %s", instruction)
                    self._voice.speak(instruction)
                    last_spoken = now

            except Exception as exc:
                logger.error("Navigation loop error: %s", exc)
                self._voice.speak(Script.NAVIGATION_ERROR)

            time.sleep(0.5)

        return False    # interrupted before arrival

    # ── Ask to continue ───────────────────────────────────────────────────────
    def _ask_continue(self) -> bool:
        """
        After arriving, ask if the user wants to navigate somewhere else.
        Returns True = yes continue, False = no exit.
        """
        answer = self._voice.listen(
            prompt=Script.CONTINUE_PROMPT,
            cue="Please say yes or no.",
        )
        if answer and any(word in answer.lower() for word in ["yes", "yeah", "sure", "please", "yep"]):
            return True
        if answer and any(word in answer.lower() for word in ["no", "nope", "done", "exit", "stop", "quit"]):
            return False
        # Ambiguous or no input → assume they want to continue
        return True

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("  SMART SPECS NAVIGATION SYSTEM  v2.2")
        logger.info("  Platform : %s", platform.system())
        logger.info("  Mode     : %s",
                    "SIMULATION" if Config.SIMULATION_MODE else "HARDWARE")
        logger.info("=" * 60)

        # ── Warm welcome ──────────────────────────────────────────────────────
        self._voice.speak_and_wait(Script.STARTUP)

        # ── GPS ───────────────────────────────────────────────────────────────
        self._gps.start()
        fix_timeout = 10 if Config.SIMULATION_MODE else 60
        if not self._wait_for_gps_fix(timeout=fix_timeout):
            logger.critical("Aborting: GPS fix not acquired.")
            self._shutdown()
            return

        # ── Main interaction loop ─────────────────────────────────────────────
        try:
            while not self._stop.is_set():

                # 1. Get destination from user (with polite retries)
                dest = self._ask_for_destination()
                if not dest or self._stop.is_set():
                    continue

                # 2. Geocode
                self._voice.speak_and_wait(Script.SEARCHING)
                coords = self._nav.geocode(dest)

                if not coords:
                    self._voice.speak_and_wait(Script.NOT_FOUND)
                    continue

                # 3. Navigate
                self._voice.speak_and_wait(Script.found(dest))
                arrived = self._navigate_to(dest, coords)
                self._arrived = arrived

                # 4. If interrupted mid-journey, exit gracefully
                if not arrived:
                    break

                # 5. After arrival, ask if they want to go somewhere else
                if self._stop.is_set():
                    break
                go_again = self._ask_continue()
                if not go_again:
                    break

        except KeyboardInterrupt:
            logger.info("Shutdown requested (Ctrl+C).")
        finally:
            self._shutdown()

    # ── Shutdown ──────────────────────────────────────────────────────────────
    def _shutdown(self) -> None:
        logger.info("Shutting down ...")
        self._stop.set()
        self._gps.stop()
        self._voice.speak_and_wait(Script.shutdown(self._arrived))
        self._voice.stop()
        logger.info("System stopped cleanly.")


# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    app = SmartSpecsApp()
    app.run()