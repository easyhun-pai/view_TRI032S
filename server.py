"""FastAPI server: WebRTC signalling + camera control for the TRI032S viewer.

Run:
    .\\.venv\\Scripts\\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000

Then open http://<server-ip>:8000 from any PC on the office LAN.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import cv2
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aiortc import RTCPeerConnection, RTCSessionDescription

import config
from camera import CameraManager
from webrtc import CameraVideoTrack

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("viewer.server")

camera = CameraManager()
pcs: set[RTCPeerConnection] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    camera.start()
    log.info("Camera manager started (mode will settle shortly: %s)", camera.mode)
    yield
    # shutdown
    for pc in list(pcs):
        await pc.close()
    pcs.clear()
    camera.stop()


app = FastAPI(title="TRI032S Web Viewer", lifespan=lifespan)


# --- WebRTC signalling ------------------------------------------------------
class Offer(BaseModel):
    sdp: str
    type: str


@app.post("/api/offer")
async def offer(params: Offer):
    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state_change():
        log.info("PeerConnection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            pcs.discard(pc)

    pc.addTrack(CameraVideoTrack(camera))

    await pc.setRemoteDescription(RTCSessionDescription(sdp=params.sdp, type=params.type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


# --- status & settings ------------------------------------------------------
@app.get("/api/status")
async def status():
    return {**camera.status(), "viewers": len(pcs)}


@app.get("/api/settings")
async def get_settings():
    return camera.get_settings()


class SettingChange(BaseModel):
    name: str
    value: float | bool


@app.post("/api/settings")
async def set_setting(change: SettingChange):
    allowed = {"exposure_auto", "exposure_time", "gain_auto", "gain", "frame_rate"}
    if change.name not in allowed:
        raise HTTPException(400, f"Unknown setting '{change.name}'")
    if camera.mode != "camera":
        raise HTTPException(409, "Settings unavailable in simulation mode (no camera)")
    camera.set_setting(change.name, change.value)
    return {"ok": True}


# --- recording --------------------------------------------------------------
@app.post("/api/recording/start")
async def recording_start():
    if camera.recorder.is_active:
        raise HTTPException(409, "Already recording")
    try:
        name = camera.start_recording()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return {"ok": True, "file": name}


@app.post("/api/recording/stop")
async def recording_stop():
    info = camera.stop_recording()
    if info is None:
        raise HTTPException(409, "Not recording")
    return {"ok": True, **info}


@app.get("/api/recordings")
async def list_recordings():
    files = sorted(config.RECORDINGS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)} for p in files]


@app.get("/api/recordings/{name}")
async def download_recording(name: str):
    # prevent path traversal
    path = (config.RECORDINGS_DIR / name).resolve()
    if path.parent != config.RECORDINGS_DIR.resolve() or not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="video/mp4", filename=name)


# --- snapshot ---------------------------------------------------------------
@app.get("/api/snapshot")
async def snapshot():
    frame = camera.get_latest(copy=True)
    if frame is None:
        raise HTTPException(503, "No frame available yet")
    fmt = config.SNAPSHOT_FORMAT.lower()
    if fmt == "png":
        ok, buf = cv2.imencode(".png", frame)
        media = "image/png"
    else:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY])
        media = "image/jpeg"
    if not ok:
        raise HTTPException(500, "Encode failed")
    headers = {"Content-Disposition": f'inline; filename="snapshot.{fmt}"'}
    return Response(content=buf.tobytes(), media_type=media, headers=headers)


# --- static frontend (mounted last so /api/* wins) --------------------------
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=config.HOST, port=config.PORT, reload=False)
