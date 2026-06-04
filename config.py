"""Central configuration for the TRI032S web viewer.

All tunables live here so the rest of the code stays declarative.
Camera-specific connection info (serial/IP) can optionally be placed in a
``CAM_INFO.json`` file next to this module; it is gitignored on purpose.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("viewer.config")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RECORDINGS_DIR = BASE_DIR / "recordings"

# --- Web server -------------------------------------------------------------
# 0.0.0.0 so other PCs on the office LAN (192.168.33.x) can reach it.
HOST = "0.0.0.0"
PORT = 8000

# --- Acquisition ------------------------------------------------------------
# Preferred pixel formats, in order. The first one the camera supports wins.
# BGR8 needs no conversion for OpenCV; RGB8/Mono8 are converted in software.
PREFERRED_PIXEL_FORMATS = ["BGR8", "RGB8", "Mono8"]

# Acquisition resolution (region of interest). None = leave the camera at its
# current/default full sensor size (recommended: gives the full field of view).
# NOTE: on machine-vision cameras Width/Height set a *crop*, not a downscale.
ACQUISITION_WIDTH: int | None = None
ACQUISITION_HEIGHT: int | None = None

# Cap the camera frame rate to keep GigE bandwidth manageable at full
# resolution. None = leave the camera default. Adjustable live from the UI.
ACQUISITION_FRAME_RATE: float | None = 30.0

# get_buffer timeout in milliseconds.
GRAB_TIMEOUT_MS = 2000

# Auto-reconnect: after this many consecutive grab failures the camera is
# closed and reopened (handles a yanked cable / power blip on an always-on
# server). Reconnect retries use exponential backoff capped at the max below.
RECONNECT_AFTER_FAILURES = 3
RECONNECT_BACKOFF_MAX_S = 10.0

# --- Streaming (WebRTC) -----------------------------------------------------
# Frames wider than this are downscaled before H.264 encoding (lower latency,
# less CPU/bandwidth). Snapshot and recording always use full resolution.
STREAM_MAX_WIDTH = 1280
STREAM_FPS = 30

# --- Snapshot ---------------------------------------------------------------
SNAPSHOT_FORMAT = "jpg"  # "jpg" or "png"
JPEG_QUALITY = 92

# --- Simulation -------------------------------------------------------------
# When True, if no camera can be opened the server streams a synthetic test
# pattern instead of failing. Lets the whole web stack be tested with no
# hardware connected; once the camera is plugged in it is used automatically.
ALLOW_SIMULATION = True
SIM_WIDTH = 1280
SIM_HEIGHT = 720
SIM_FPS = 30


def load_cam_info() -> dict:
    """Optional camera targeting. Returns {} when no CAM_INFO.json is present.

    Recognised keys: ``serial`` (str) and/or ``ip`` (str). When given, the
    matching device is selected instead of the first one found.
    """
    path = BASE_DIR / "CAM_INFO.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to read CAM_INFO.json: %s", exc)
        return {}
