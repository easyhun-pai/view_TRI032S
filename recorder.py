"""MP4 recorder, driven frame-by-frame from the camera grab thread.

A single writer is fed the full-resolution BGR frames the grab loop already
produces, so recording stays in sync with the actual acquired frames and we
never open a second connection to the camera.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

import cv2

log = logging.getLogger("viewer.recorder")


class Recorder:
    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._writer: cv2.VideoWriter | None = None
        self._path: Path | None = None
        self._size: tuple[int, int] | None = None  # (w, h)
        self._frames = 0

    @property
    def is_active(self) -> bool:
        return self._writer is not None

    @property
    def current_file(self) -> str | None:
        return self._path.name if self._path else None

    def start(self, width: int, height: int, fps: float) -> str:
        """Open a new .mp4 file. Returns the filename. Idempotent-safe."""
        with self._lock:
            if self._writer is not None:
                raise RuntimeError("Recording already in progress")
            fps = float(fps) if fps and fps > 0 else 30.0
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.out_dir / f"recording_{ts}.mp4"
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(str(path), fourcc, fps, (width, height))
            if not writer.isOpened():
                raise RuntimeError("Failed to open VideoWriter (codec/path issue)")
            self._writer = writer
            self._path = path
            self._size = (width, height)
            self._frames = 0
            log.info("Recording started: %s (%dx%d @ %.1ffps)", path.name, width, height, fps)
            return path.name

    def write(self, frame_bgr) -> None:
        """Write one BGR frame. Called from the grab thread; cheap no-op when idle."""
        with self._lock:
            if self._writer is None:
                return
            h, w = frame_bgr.shape[:2]
            if self._size != (w, h):
                frame_bgr = cv2.resize(frame_bgr, self._size)
            self._writer.write(frame_bgr)
            self._frames += 1

    def stop(self) -> dict | None:
        """Close the file. Returns info about the saved clip, or None if idle."""
        with self._lock:
            if self._writer is None:
                return None
            self._writer.release()
            info = {"file": self._path.name, "frames": self._frames}
            log.info("Recording stopped: %s (%d frames)", self._path.name, self._frames)
            self._writer = None
            self._path = None
            self._size = None
            self._frames = 0
            return info
