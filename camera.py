"""Single-owner camera manager for the Lucid TRI032S.

One background thread owns the camera connection, continuously grabs frames,
and publishes the most recent frame (as a BGR numpy array) for any number of
consumers (WebRTC tracks, snapshots, the recorder). This mirrors the GigE
Vision constraint that only one process may stream from the device at a time.

If the camera cannot be opened and ``ALLOW_SIMULATION`` is set, a synthetic
test pattern is produced instead, so the full web stack runs with no hardware.
When a real camera is later connected, restarting the server picks it up
automatically with no code changes.

arena_api usage follows the SDK's bundled examples (py_live_stream_opencv.py,
py_exposure.py).
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

import cv2
import numpy as np

import config
from recorder import Recorder

log = logging.getLogger("viewer.camera")


class CameraManager:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._frame_lock = threading.Lock()
        self._latest: np.ndarray | None = None  # full-res BGR
        self._frame_index = 0
        self._frame_event = threading.Condition(self._frame_lock)

        # Pending setting changes are applied by the grab thread (the only
        # thread that touches the device), avoiding cross-thread device access.
        self._pending_lock = threading.Lock()
        self._pending: dict[str, object] = {}

        self._settings_lock = threading.Lock()
        self._settings_cache: dict[str, dict] = {}

        self.recorder = Recorder(config.RECORDINGS_DIR)

        self.mode = "starting"  # "camera" | "simulation" | "error"
        self.model = None
        self.width = 0
        self.height = 0
        self._fps = 0.0
        self._fps_samples: list[float] = []

        # arena_api handles (populated when a real camera is opened)
        self._system = None
        self._device = None
        self._BufferFactory = None
        self._pixel_format = None
        self._channels = 3
        self._convert = None  # None | "rgb2bgr" | "gray2bgr"

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-grab", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.recorder.stop()
        self._close_device()

    # -- public API ---------------------------------------------------------
    @property
    def fps(self) -> float:
        return round(self._fps, 1)

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "model": self.model,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "recording": self.recorder.is_active,
            "recording_file": self.recorder.current_file,
        }

    def get_latest(self, copy: bool = False) -> np.ndarray | None:
        with self._frame_lock:
            if self._latest is None:
                return None
            return self._latest.copy() if copy else self._latest

    def get_stream_frame(self, max_width: int) -> np.ndarray | None:
        """Latest frame, downscaled for low-latency streaming."""
        frame = self.get_latest()
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if max_width and w > max_width:
            scale = max_width / w
            frame = cv2.resize(frame, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)
        return frame

    def set_setting(self, name: str, value) -> None:
        with self._pending_lock:
            self._pending[name] = value

    def get_settings(self) -> dict:
        with self._settings_lock:
            return dict(self._settings_cache)

    def start_recording(self) -> str:
        frame = self.get_latest()
        if frame is None:
            raise RuntimeError("No frame available yet")
        h, w = frame.shape[:2]
        fps = self._fps if self._fps > 1 else (config.ACQUISITION_FRAME_RATE or config.STREAM_FPS)
        return self.recorder.start(w, h, fps)

    def stop_recording(self) -> dict | None:
        return self.recorder.stop()

    # -- grab loop ----------------------------------------------------------
    def _run(self) -> None:
        opened = False
        try:
            opened = self._open_device()
        except Exception:
            log.exception("Error while opening camera")

        if opened:
            self.mode = "camera"
            log.info("Streaming from camera: %s (%dx%d)", self.model, self.width, self.height)
            self._refresh_settings()
        elif config.ALLOW_SIMULATION:
            self.mode = "simulation"
            self.model = "SIMULATION (no camera)"
            self.width, self.height = config.SIM_WIDTH, config.SIM_HEIGHT
            log.warning("No camera available - running in SIMULATION mode")
        else:
            self.mode = "error"
            log.error("No camera available and simulation disabled")
            return

        last = time.time()
        settings_refresh_at = 0.0
        sim_t = 0
        fail_count = 0
        while not self._stop.is_set():
            if self.mode == "camera":
                self._apply_pending()
                frame = self._grab_real()
                if frame is None:
                    fail_count += 1
                    if fail_count == 1:
                        log.warning("Frame grab failing; reconnecting after %d "
                                    "consecutive failures", config.RECONNECT_AFTER_FAILURES)
                    if fail_count >= config.RECONNECT_AFTER_FAILURES:
                        self._reconnect()
                        fail_count = 0
                    continue
                if fail_count:
                    log.info("Camera recovered after %d failure(s)", fail_count)
                    fail_count = 0
                now = time.time()
                if now - settings_refresh_at > 1.0:
                    self._refresh_settings()
                    settings_refresh_at = now
            else:
                frame = self._make_sim_frame(sim_t)
                sim_t += 1
                # pace the simulation to SIM_FPS
                time.sleep(max(0.0, 1.0 / config.SIM_FPS))

            # publish
            with self._frame_lock:
                self._latest = frame
                self._frame_index += 1
                self._frame_event.notify_all()

            # feed recorder (full resolution)
            if self.recorder.is_active:
                try:
                    self.recorder.write(frame)
                except Exception:
                    log.exception("Recorder write failed")

            # fps estimate
            now = time.time()
            dt = now - last
            last = now
            if dt > 0:
                self._fps_samples.append(1.0 / dt)
                if len(self._fps_samples) > 30:
                    self._fps_samples.pop(0)
                self._fps = sum(self._fps_samples) / len(self._fps_samples)

    # -- real camera --------------------------------------------------------
    def _open_device(self) -> bool:
        try:
            from arena_api.system import system
            from arena_api.buffer import BufferFactory
        except Exception as exc:
            log.warning("arena_api not importable: %s", exc)
            return False

        devices = system.create_device()
        if not devices:
            return False

        cam_info = config.load_cam_info()
        device = self._select_device(devices, cam_info)

        self._system = system
        self._device = device
        self._BufferFactory = BufferFactory

        nodemap = device.nodemap
        try:
            self.model = nodemap.get_node("DeviceModelName").value
        except Exception:
            self.model = "Lucid camera"

        self._configure_pixel_format(nodemap)
        self._configure_roi(nodemap)
        self._configure_frame_rate(nodemap)
        self._configure_stream(device)

        device.start_stream()

        # learn the actual streamed resolution from the first buffer
        try:
            buf = device.get_buffer(timeout=config.GRAB_TIMEOUT_MS)
            self.width, self.height = buf.width, buf.height
            device.requeue_buffer(buf)
        except Exception:
            log.exception("Failed to read first buffer")
        return True

    @staticmethod
    def _select_device(devices, cam_info: dict):
        serial = str(cam_info.get("serial", "")).strip()
        ip = str(cam_info.get("ip", "")).strip()
        if not (serial or ip):
            return devices[0]
        # device_infos is parallel to the devices list returned by create_device
        try:
            from arena_api.system import system
            for idx, info in enumerate(system.device_infos):
                if serial and str(info.get("serial", "")) == serial:
                    return devices[idx]
                if ip and str(info.get("ip", "")) == ip:
                    return devices[idx]
        except Exception:
            log.exception("Device selection by CAM_INFO failed; using first device")
        return devices[0]

    def _configure_pixel_format(self, nodemap) -> None:
        node = nodemap.get_node("PixelFormat")
        available = []
        try:
            available = list(node.enumentry_names)
        except Exception:
            pass
        chosen = None
        for pf in config.PREFERRED_PIXEL_FORMATS:
            if not available or pf in available:
                try:
                    node.value = pf
                    chosen = pf
                    break
                except Exception:
                    continue
        if chosen is None:
            chosen = node.value  # whatever the camera is already on
        self._pixel_format = chosen
        if chosen == "BGR8":
            self._channels, self._convert = 3, None
        elif chosen == "RGB8":
            self._channels, self._convert = 3, "rgb2bgr"
        elif chosen.startswith("Mono"):
            self._channels, self._convert = 1, "gray2bgr"
        else:
            # Bayer or other: 1 byte/pixel, let OpenCV debayer generically
            self._channels, self._convert = 1, "bayer2bgr"
        log.info("PixelFormat=%s channels=%d convert=%s", chosen, self._channels, self._convert)

    def _configure_roi(self, nodemap) -> None:
        if config.ACQUISITION_WIDTH and config.ACQUISITION_HEIGHT:
            try:
                nodes = nodemap.get_node(["Width", "Height"])
                nodes["Width"].value = int(config.ACQUISITION_WIDTH)
                nodes["Height"].value = int(config.ACQUISITION_HEIGHT)
            except Exception:
                log.exception("Failed to set ROI; using sensor default")

    def _configure_frame_rate(self, nodemap) -> None:
        if config.ACQUISITION_FRAME_RATE is None:
            return
        try:
            en = nodemap.get_node("AcquisitionFrameRateEnable")
            if en is not None:
                en.value = True
            fr = nodemap.get_node("AcquisitionFrameRate")
            if fr is not None:
                fr.value = float(min(config.ACQUISITION_FRAME_RATE, fr.max))
        except Exception:
            log.exception("Failed to set frame rate")

    @staticmethod
    def _configure_stream(device) -> None:
        tl = device.tl_stream_nodemap
        try:
            tl["StreamBufferHandlingMode"].value = "NewestOnly"
            tl["StreamAutoNegotiatePacketSize"].value = True
            tl["StreamPacketResendEnable"].value = True
        except Exception:
            log.exception("Failed to configure stream nodemap")

    def _grab_real(self) -> np.ndarray | None:
        try:
            buffer = self._device.get_buffer(timeout=config.GRAB_TIMEOUT_MS)
        except Exception as exc:
            # Quiet by design: the grab loop counts failures and reconnects;
            # logging a full traceback every timeout would flood the log.
            log.debug("get_buffer failed: %s", exc)
            return None
        try:
            item = self._BufferFactory.copy(buffer)
            self._device.requeue_buffer(buffer)
            bpp = int(len(item.data) / (item.width * item.height))
            array = (ctypes.c_ubyte * (bpp * item.width * item.height)).from_address(
                ctypes.addressof(item.pbytes)
            )
            raw = np.ndarray(buffer=array, dtype=np.uint8,
                             shape=(item.height, item.width, bpp)).copy()
            self._BufferFactory.destroy(item)
        except Exception:
            log.exception("Buffer conversion failed")
            return None
        return self._to_bgr(raw)

    def _to_bgr(self, raw: np.ndarray) -> np.ndarray:
        if self._convert is None:
            return raw
        if self._convert == "rgb2bgr":
            return cv2.cvtColor(raw, cv2.COLOR_RGB2BGR)
        if self._convert == "gray2bgr":
            return cv2.cvtColor(raw.reshape(raw.shape[0], raw.shape[1]), cv2.COLOR_GRAY2BGR)
        if self._convert == "bayer2bgr":
            return cv2.cvtColor(raw.reshape(raw.shape[0], raw.shape[1]), cv2.COLOR_BayerRG2BGR)
        return raw

    # -- settings (applied by grab thread) ----------------------------------
    def _apply_pending(self) -> None:
        with self._pending_lock:
            if not self._pending:
                return
            pending = self._pending
            self._pending = {}
        nodemap = self._device.nodemap
        for name, value in pending.items():
            try:
                self._apply_one(nodemap, name, value)
            except Exception:
                log.exception("Failed to apply setting %s=%r", name, value)

    def _apply_one(self, nodemap, name: str, value) -> None:
        if name == "exposure_auto":
            nodemap.get_node("ExposureAuto").value = "Continuous" if value else "Off"
        elif name == "exposure_time":
            nodemap.get_node("ExposureAuto").value = "Off"
            self._set_clamped(nodemap.get_node("ExposureTime"), float(value))
        elif name == "gain_auto":
            nodemap.get_node("GainAuto").value = "Continuous" if value else "Off"
        elif name == "gain":
            nodemap.get_node("GainAuto").value = "Off"
            self._set_clamped(nodemap.get_node("Gain"), float(value))
        elif name == "frame_rate":
            en = nodemap.get_node("AcquisitionFrameRateEnable")
            if en is not None:
                en.value = True
            self._set_clamped(nodemap.get_node("AcquisitionFrameRate"), float(value))

    @staticmethod
    def _set_clamped(node, value: float) -> None:
        if node is None:
            return
        value = max(node.min, min(node.max, value))
        node.value = value

    def _refresh_settings(self) -> None:
        if self.mode != "camera" or self._device is None:
            return
        nodemap = self._device.nodemap
        snapshot: dict[str, dict] = {}
        for key, node_name, kind in [
            ("exposure_auto", "ExposureAuto", "enum_bool"),
            ("exposure_time", "ExposureTime", "float"),
            ("gain_auto", "GainAuto", "enum_bool"),
            ("gain", "Gain", "float"),
            ("frame_rate", "AcquisitionFrameRate", "float"),
        ]:
            try:
                node = nodemap.get_node(node_name)
                if node is None:
                    continue
                if kind == "float":
                    snapshot[key] = {
                        "value": round(float(node.value), 2),
                        "min": round(float(node.min), 2),
                        "max": round(float(node.max), 2),
                        "writable": bool(node.is_writable),
                    }
                else:  # enum treated as bool (Continuous == auto on)
                    snapshot[key] = {
                        "value": str(node.value) == "Continuous",
                        "writable": bool(node.is_writable),
                    }
            except Exception:
                continue
        with self._settings_lock:
            self._settings_cache = snapshot

    # -- simulation ---------------------------------------------------------
    def _make_sim_frame(self, t: int) -> np.ndarray:
        w, h = config.SIM_WIDTH, config.SIM_HEIGHT
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        # animated gradient background (compute in int32 then wrap into uint8;
        # numpy 2.x raises on uint8 overflow from a Python int, so cast last)
        grad = ((np.linspace(0, 255, w, dtype=np.int32) + t * 2) % 255).astype(np.uint8)
        frame[:, :, 0] = grad
        frame[:, :, 1] = grad[::-1]
        frame[:, :, 2] = np.uint8((t * 3) % 255)
        # sweeping vertical bar
        x = int((t * 8) % w)
        cv2.rectangle(frame, (x, 0), (min(x + 12, w), h), (255, 255, 255), -1)
        # overlays
        cv2.putText(frame, "SIMULATION - no camera connected", (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(frame, "SIMULATION - no camera connected", (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, time.strftime("%Y-%m-%d %H:%M:%S"), (30, h - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f"frame {t}", (30, 110),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 0), 2, cv2.LINE_AA)
        return frame

    # -- reconnect ----------------------------------------------------------
    def _reconnect(self) -> None:
        """Close and reopen the device, retrying with backoff until it comes
        back (or the manager is stopped). The last frame stays on screen
        meanwhile, so viewers see a freeze rather than a crash."""
        log.warning("Camera lost - attempting to reconnect...")
        self._close_device()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self._open_device():
                    self._refresh_settings()
                    log.info("Camera reconnected: %s (%dx%d)", self.model, self.width, self.height)
                    return
            except Exception:
                log.exception("Reconnect attempt failed")
            # responsive sleep: wakes immediately if the server is shutting down
            self._stop.wait(backoff)
            backoff = min(backoff * 2, config.RECONNECT_BACKOFF_MAX_S)

    # -- teardown -----------------------------------------------------------
    def _close_device(self) -> None:
        if self._device is not None:
            try:
                self._device.stop_stream()
            except Exception:
                pass
        if self._system is not None:
            try:
                self._system.destroy_device()
            except Exception:
                pass
        self._device = None
        self._system = None
