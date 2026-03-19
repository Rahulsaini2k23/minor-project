import argparse
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import math
from queue import Empty, Queue
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://maps.googleapis.com/maps/api/directions/json"
GEOCODE_API_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GEOLOCATION_API_URL = "https://www.googleapis.com/geolocation/v1/geolocate"
IP_GEOLOCATION_API_URL = "https://ipapi.co/json/"
ENV_API_KEY = "GOOGLE_MAPS_API_KEY"
LEGACY_ENV_API_KEY = "ENV_API_KEY"
KNOWN_ORIGIN_ALIASES = {
    "nit": "31.39051243263113,75.5359833748799",
}


class Ansi:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Ansi.BLUE,
        logging.INFO: Ansi.GREEN,
        logging.WARNING: Ansi.YELLOW,
        logging.ERROR: Ansi.RED,
        logging.CRITICAL: Ansi.RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, Ansi.WHITE)
        base_msg = super().format(record)
        return f"{color}{base_msg}{Ansi.RESET}"


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("SmartNavigation")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = ColorFormatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def color_text(text: str, color: str) -> str:
    return f"{color}{text}{Ansi.RESET}"


def strip_html(raw_html: str) -> str:
    no_tags = re.sub(r"<[^>]+>", "", raw_html)
    return html.unescape(no_tags).strip()


def infer_direction(maneuver: str, clean_instruction: str) -> str:
    maneuver = (maneuver or "").lower()
    instruction = clean_instruction.lower()

    if "right" in maneuver or "right" in instruction:
        return "RIGHT"
    if "left" in maneuver or "left" in instruction:
        return "LEFT"
    if "uturn" in maneuver or "u-turn" in instruction:
        return "U-TURN"
    if "straight" in maneuver or "continue" in instruction:
        return "STRAIGHT"
    if "roundabout" in maneuver:
        return "ROUNDABOUT"
    return "MOVE"


@dataclass
class RouteStep:
    instruction: str
    distance_text: str
    duration_text: str
    maneuver: str
    direction_hint: str
    start_lat: Optional[float]
    start_lng: Optional[float]
    end_lat: Optional[float]
    end_lng: Optional[float]


@dataclass
class RouteResult:
    start_address: str
    end_address: str
    total_distance_text: str
    total_duration_text: str
    steps: List[RouteStep]


class Speaker:
    def __init__(self, logger: logging.Logger, enabled: bool = True) -> None:
        self.logger = logger
        self.enabled = enabled
        self.say_cmd = self._find_say_command()
        if self.enabled and not self.say_cmd:
            self.logger.warning("TTS command not found; spoken output disabled.")
            self.enabled = False

    def _find_say_command(self) -> str:
        if sys.platform == "darwin" and shutil.which("say"):
            return "say"
        if shutil.which("spd-say"):
            return "spd-say"
        if shutil.which("espeak"):
            return "espeak"
        return ""

    def speak(self, message: str) -> None:
        if not self.enabled:
            return

        try:
            subprocess.run([self.say_cmd, message], check=False)
        except Exception as err:
            self.logger.warning("Failed to speak message: %s", err)


def call_directions_api(
    api_key: str,
    origin: str,
    destination: str,
    mode: str = "walking",
) -> Dict[str, Any]:
    params = {
        "origin": origin,
        "destination": destination,
        "mode": mode,
        "key": api_key,
    }
    request_url = f"{API_URL}?{urlencode(params)}"
    with urlopen(request_url, timeout=20) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def call_geocode_api(api_key: str, address: str) -> Dict[str, Any]:
    params = {
        "address": address,
        "key": api_key,
    }
    request_url = f"{GEOCODE_API_URL}?{urlencode(params)}"
    with urlopen(request_url, timeout=20) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def fetch_json(url: str, method: str = "GET", body: bytes = b"") -> Dict[str, Any]:
    req = Request(
        url=url,
        data=body if method.upper() != "GET" else None,
        method=method.upper(),
        headers={"Content-Type": "application/json", "User-Agent": "smart-navigation/1.0"},
    )
    with urlopen(req, timeout=15) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def get_live_origin(api_key: str, logger: logging.Logger) -> str:
    # First attempt: Google Geolocation API (better on devices with wifi/cell context)
    try:
        google_geo_url = f"{GEOLOCATION_API_URL}?key={api_key}"
        google_geo = fetch_json(google_geo_url, method="POST", body=b"{}")
        location = google_geo.get("location", {})
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is not None and lng is not None:
            logger.info("Live location resolved via Google Geolocation API.")
            return f"{lat},{lng}"
    except Exception:
        logger.warning("Google live geolocation unavailable, trying IP-based location.")

    # Fallback: IP geolocation service
    try:
        ip_geo = fetch_json(IP_GEOLOCATION_API_URL)
        lat = ip_geo.get("latitude")
        lng = ip_geo.get("longitude")
        if lat is not None and lng is not None:
            logger.info("Live location resolved via IP geolocation.")
            return f"{lat},{lng}"
    except Exception:
        logger.warning("IP geolocation also unavailable.")

    return ""


def parse_lat_lng(value: str) -> Optional[Tuple[float, float]]:
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].strip())
        lng = float(parts[1].strip())
    except ValueError:
        return None
    return lat, lng


def resolve_origin_alias(origin: Optional[str], logger: logging.Logger) -> Optional[str]:
    if not origin:
        return origin

    normalized = origin.strip().lower()
    if normalized in KNOWN_ORIGIN_ALIASES:
        resolved = KNOWN_ORIGIN_ALIASES[normalized]
        logger.info("Origin alias '%s' resolved to %s", origin, resolved)
        return resolved
    return origin


def get_live_origin_coords(api_key: str, logger: logging.Logger) -> Optional[Tuple[float, float]]:
    raw_origin = get_live_origin(api_key=api_key, logger=logger)
    if not raw_origin:
        return None
    return parse_lat_lng(raw_origin)


def geocode_address_to_latlng(api_key: str, address: str, logger: logging.Logger) -> Optional[str]:
    try:
        data = call_geocode_api(api_key=api_key, address=address)
        if data.get("status") != "OK":
            return None
        first = (data.get("results") or [{}])[0]
        location = first.get("geometry", {}).get("location", {})
        lat = location.get("lat")
        lng = location.get("lng")
        formatted = first.get("formatted_address")
        if lat is None or lng is None:
            return None
        if formatted:
            logger.info("Resolved destination to: %s", formatted)
        return f"{lat},{lng}"
    except Exception:
        return None


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_m * c


def infer_step_index_from_location(
    route: RouteResult,
    current_lat: float,
    current_lng: float,
    min_index: int = 0,
) -> int:
    if not route.steps:
        return 0

    start_index = max(0, min_index)
    best_idx = start_index
    best_distance = float("inf")

    for idx in range(start_index, len(route.steps)):
        step = route.steps[idx]
        if step.end_lat is None or step.end_lng is None:
            continue
        distance = haversine_meters(current_lat, current_lng, step.end_lat, step.end_lng)
        if distance < best_distance:
            best_distance = distance
            best_idx = idx

    return best_idx


def get_route_with_fallback(
    api_key: str,
    origin: str,
    destination: str,
    preferred_mode: str,
    logger: logging.Logger,
) -> Tuple[RouteResult, str]:
    mode_order = [preferred_mode] + [
        m for m in ["walking", "driving", "bicycling", "transit"] if m != preferred_mode
    ]
    destination_candidates: List[Tuple[str, str]] = [("spoken/text destination", destination)]
    geocoded_destination = geocode_address_to_latlng(api_key=api_key, address=destination, logger=logger)
    if geocoded_destination and geocoded_destination != destination:
        destination_candidates.append(("geocoded destination", geocoded_destination))

    attempt_errors: List[str] = []
    for candidate_label, candidate_destination in destination_candidates:
        for mode in mode_order:
            raw_data = call_directions_api(
                api_key=api_key,
                origin=origin,
                destination=candidate_destination,
                mode=mode,
            )
            status = raw_data.get("status")
            if status == "OK":
                route = parse_route(raw_data)
                if mode != preferred_mode:
                    logger.warning(
                        "Route unavailable in %s mode, switched to %s mode.",
                        preferred_mode,
                        mode,
                    )
                if candidate_label != "spoken/text destination":
                    logger.info("Using %s for routing.", candidate_label)
                return route, mode

            attempt_errors.append(f"{candidate_label} + {mode}: {status}")
            if status not in {"ZERO_RESULTS", "NOT_FOUND"}:
                error_msg = raw_data.get("error_message", "No extra error message from API.")
                raise ValueError(
                    f"Google Directions API failed with status '{status}': {error_msg}"
                )

    raise ValueError(
        "No route found after trying multiple destination/mode combinations. "
        f"Attempts: {', '.join(attempt_errors)}"
    )


def parse_route(data: Dict[str, Any]) -> RouteResult:
    status = data.get("status")
    if status != "OK":
        error_msg = data.get("error_message", "No extra error message from API.")
        raise ValueError(f"Google Directions API failed with status '{status}': {error_msg}")

    routes = data.get("routes", [])
    if not routes:
        raise ValueError("No route found for the given origin/destination.")

    legs = routes[0].get("legs", [])
    if not legs:
        raise ValueError("Route exists, but no leg data was returned.")

    leg = legs[0]
    steps: List[RouteStep] = []
    for step in leg.get("steps", []):
        instruction_html = step.get("html_instructions", "")
        clean_instruction = strip_html(instruction_html)
        maneuver = step.get("maneuver", "")
        direction_hint = infer_direction(maneuver, clean_instruction)

        steps.append(
            RouteStep(
                instruction=clean_instruction,
                distance_text=step.get("distance", {}).get("text", "N/A"),
                duration_text=step.get("duration", {}).get("text", "N/A"),
                maneuver=maneuver or "N/A",
                direction_hint=direction_hint,
                start_lat=step.get("start_location", {}).get("lat"),
                start_lng=step.get("start_location", {}).get("lng"),
                end_lat=step.get("end_location", {}).get("lat"),
                end_lng=step.get("end_location", {}).get("lng"),
            )
        )

    return RouteResult(
        start_address=leg.get("start_address", "Unknown"),
        end_address=leg.get("end_address", "Unknown"),
        total_distance_text=leg.get("distance", {}).get("text", "N/A"),
        total_duration_text=leg.get("duration", {}).get("text", "N/A"),
        steps=steps,
    )


def color_for_direction(direction: str) -> str:
    if direction == "RIGHT":
        return Ansi.MAGENTA
    if direction == "LEFT":
        return Ansi.CYAN
    if direction == "STRAIGHT":
        return Ansi.GREEN
    if direction == "U-TURN":
        return Ansi.YELLOW
    if direction == "ROUNDABOUT":
        return Ansi.BLUE
    return Ansi.WHITE


def display_route(route: RouteResult, logger: logging.Logger) -> None:
    logger.info("Route started.")
    logger.info("From: %s", route.start_address)
    logger.info("To  : %s", route.end_address)
    logger.info("Total Distance: %s", route.total_distance_text)
    logger.info("Estimated Time: %s", route.total_duration_text)
    logger.info("-" * 60)
    logger.info("Turn-by-turn directions:")

    if not route.steps:
        logger.warning("No step-by-step instructions available.")
        return

    for idx, step in enumerate(route.steps, start=1):
        direction_color = color_for_direction(step.direction_hint)
        tag = color_text(f"[{step.direction_hint}]", direction_color)
        logger.info(
            "%d. %s %s (%s, %s)",
            idx,
            tag,
            step.instruction,
            step.distance_text,
            step.duration_text,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smart navigation with Google Directions API and color logs."
    )
    parser.add_argument("--origin", help='Origin address or "lat,lng"')
    parser.add_argument("--destination", help='Destination address or "lat,lng"')
    parser.add_argument(
        "--voice-destination",
        action="store_true",
        help="Capture destination using microphone",
    )
    parser.add_argument(
        "--voice-timeout",
        type=int,
        default=7,
        help="Seconds to wait for speech to start (default: 7)",
    )
    parser.add_argument(
        "--voice-phrase-limit",
        type=int,
        default=8,
        help="Max seconds for one spoken destination phrase (default: 8)",
    )
    parser.add_argument(
        "--mode",
        choices=["walking", "driving", "bicycling", "transit"],
        default="walking",
        help="Travel mode (default: walking)",
    )
    parser.add_argument(
        "--live-guidance",
        action="store_true",
        help="Enable continuous TTS guidance loop",
    )
    parser.add_argument(
        "--speak-interval",
        type=int,
        default=120,
        help="Seconds between automatic spoken reminders (default: 120)",
    )
    parser.add_argument(
        "--tts",
        choices=["auto", "on", "off"],
        default="auto",
        help="Text-to-speech mode (default: auto)",
    )
    return parser.parse_args()


def load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return

    try:
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # .env loading is best-effort; env vars can still be provided by shell.
        return


def load_possible_dotenv_files() -> None:
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    cwd = Path.cwd()

    for candidate in (cwd / ".env", script_dir / ".env", project_root / ".env"):
        load_dotenv_file(candidate)


def listen_for_destination(
    logger: logging.Logger,
    timeout: int = 7,
    phrase_time_limit: int = 8,
) -> str:
    try:
        import speech_recognition as sr  # type: ignore
    except Exception:
        logger.warning(
            "Voice input requires SpeechRecognition + microphone backend. Falling back to keyboard input."
        )
        return ""

    recognizer = sr.Recognizer()
    timeout = max(2, timeout)
    phrase_time_limit = max(2, phrase_time_limit)

    try:
        with sr.Microphone() as source:
            logger.info("Listening for destination from mic... Speak now.")
            logger.info("If recognition fails, type destination manually.")
            recognizer.adjust_for_ambient_noise(source, duration=0.7)
            audio = recognizer.listen(
                source,
                timeout=timeout,
                phrase_time_limit=phrase_time_limit,
            )
    except Exception as err:
        logger.warning("Could not access microphone: %s", err)
        return ""

    try:
        spoken_text = recognizer.recognize_google(audio).strip()
        if spoken_text:
            logger.info("Heard destination: %s", spoken_text)
            return spoken_text
    except Exception as err:
        logger.warning("Could not recognize destination from speech: %s", err)

    return ""


def build_spoken_step(step: RouteStep, step_index: int, total_steps: int) -> str:
    return (
        f"Step {step_index} of {total_steps}. "
        f"{step.direction_hint}. {step.instruction}. "
        f"Distance {step.distance_text}. Duration {step.duration_text}."
    )


def read_terminal_input(command_queue: Queue) -> None:
    while True:
        try:
            command = input().strip().lower()
            command_queue.put(command)
            if command == "q":
                return
        except EOFError:
            command_queue.put("q")
            return
        except Exception:
            command_queue.put("q")
            return


def run_live_guidance(
    route: RouteResult,
    logger: logging.Logger,
    speaker: Speaker,
    interval_sec: int,
    api_key: str,
) -> None:
    if not route.steps:
        logger.warning("No steps available for live guidance.")
        return

    logger.info("-" * 60)
    logger.info(
        "Live guidance started: Enter=re-speak current step | n=next step | q=quit guidance"
    )

    command_queue: Queue = Queue()
    input_thread = threading.Thread(target=read_terminal_input, args=(command_queue,), daemon=True)
    input_thread.start()

    step_idx = 0
    last_spoken_at = 0.0
    interval_sec = max(5, interval_sec)

    def update_step_from_live_location() -> Optional[Tuple[float, float]]:
        nonlocal step_idx
        coords = get_live_origin_coords(api_key=api_key, logger=logger)
        if not coords:
            return None
        current_lat, current_lng = coords
        # Keep guidance monotonic to avoid jumping back to older steps.
        step_idx = infer_step_index_from_location(
            route=route,
            current_lat=current_lat,
            current_lng=current_lng,
            min_index=step_idx,
        )
        return coords

    def speak_current_step(reason: str, use_live_location: bool = False) -> None:
        nonlocal last_spoken_at
        live_coords_text = ""
        if use_live_location:
            coords = update_step_from_live_location()
            if coords:
                live_coords_text = f" @ {coords[0]:.6f},{coords[1]:.6f}"
            else:
                logger.warning("Live location not available; using previous step context.")

        current_step = route.steps[step_idx]
        spoken = build_spoken_step(current_step, step_idx + 1, len(route.steps))
        logger.info("%s%s: %s", reason, live_coords_text, spoken)
        speaker.speak(spoken)
        last_spoken_at = time.monotonic()

    speak_current_step("Now heading", use_live_location=True)

    while True:
        try:
            command = command_queue.get(timeout=1)
            if command == "q":
                logger.info("Live guidance stopped by user.")
                return
            if command == "":
                speak_current_step("Direction from your current point", use_live_location=True)
                continue
            if command == "n":
                if step_idx < len(route.steps) - 1:
                    step_idx += 1
                    speak_current_step("Moved to next step")
                else:
                    logger.info("You are at final step already.")
                    speaker.speak("You are already at the final step.")
                continue
            logger.info("Unknown command '%s'. Use Enter, n, or q.", command)
        except Empty:
            if time.monotonic() - last_spoken_at >= interval_sec:
                speak_current_step(f"Auto reminder ({interval_sec}s)", use_live_location=True)


def main() -> None:
    logger = setup_logger()
    args = parse_args()
    load_possible_dotenv_files()

    origin = resolve_origin_alias(args.origin, logger)
    destination = args.destination
    mode = args.mode

    api_key = os.getenv(ENV_API_KEY, "").strip() or os.getenv(LEGACY_ENV_API_KEY, "").strip()
    if not api_key:
        logger.error(
            "Missing API key. Set %s (preferred) or %s in shell/.env",
            ENV_API_KEY,
            LEGACY_ENV_API_KEY,
        )
        sys.exit(1)

    if not origin:
        logger.info("Detecting your live current location for origin...")
        origin = get_live_origin(api_key=api_key, logger=logger)
        if origin:
            logger.info("Using live origin coordinates: %s", origin)
        else:
            logger.warning("Could not detect live location. Falling back to manual origin input.")
            origin = input("Enter origin (address or lat,lng): ").strip()

    if not destination and args.voice_destination:
        destination = listen_for_destination(
            logger=logger,
            timeout=args.voice_timeout,
            phrase_time_limit=args.voice_phrase_limit,
        )

    if not destination:
        destination = input("Enter destination (address or lat,lng): ").strip()

    if not origin or not destination:
        logger.error("Origin and destination are required.")
        sys.exit(1)

    logger.info("Requesting route from Google Directions API...")
    try:
        route, used_mode = get_route_with_fallback(
            api_key=api_key,
            origin=origin,
            destination=destination,
            preferred_mode=mode,
            logger=logger,
        )
        if used_mode != mode:
            mode = used_mode
    except (HTTPError, URLError) as net_err:
        logger.error("Network/API request failed: %s", net_err)
        sys.exit(1)
    except ValueError as parse_err:
        logger.error("Route parsing failed: %s", parse_err)
        sys.exit(1)
    except Exception as err:
        logger.error("Unexpected error: %s", err)
        sys.exit(1)

    display_route(route, logger)

    tts_enabled = args.tts != "off"
    speaker = Speaker(logger=logger, enabled=tts_enabled)
    if args.tts == "on" and not speaker.enabled:
        logger.warning("TTS forced on but no supported speech command found.")

    if args.live_guidance:
        run_live_guidance(
            route=route,
            logger=logger,
            speaker=speaker,
            interval_sec=args.speak_interval,
            api_key=api_key,
        )


if __name__ == "__main__":
    main()
