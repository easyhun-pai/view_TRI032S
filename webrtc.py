"""WebRTC video track that pulls frames from the shared CameraManager.

aiortc paces ``recv()`` via ``next_timestamp()`` (~STREAM_FPS), so we simply
hand it the latest frame each time, already downscaled for low latency.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from aiortc.mediastreams import VideoStreamTrack
from av import VideoFrame

import config

log = logging.getLogger("viewer.webrtc")

_BLACK = np.zeros((config.SIM_HEIGHT, config.SIM_WIDTH, 3), dtype=np.uint8)


class CameraVideoTrack(VideoStreamTrack):
    """A live video track sourced from the camera grab thread."""

    kind = "video"

    def __init__(self, camera) -> None:
        super().__init__()
        self._camera = camera

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        # Grab/resize is light; run in a thread to keep the event loop free.
        frame = await asyncio.to_thread(self._camera.get_stream_frame, config.STREAM_MAX_WIDTH)
        if frame is None:
            frame = _BLACK
        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame
