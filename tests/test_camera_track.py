"""CameraTrack: numpy frames in → av.VideoFrame out with correct pts/time_base."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from carlabridge.sensors.frame_queue import FrameQueue
from carlabridge.streaming.webrtc import CameraTrack


async def test_camera_track_recv_yields_videoframe():
    fq = FrameQueue("city")
    fq.bind_loop()
    track = CameraTrack(fq, channel_id="city")
    rgb = np.zeros((120, 160, 3), dtype=np.uint8)
    rgb[:, :, 0] = 255  # full red, easy to verify
    fq.set_latest(rgb)
    frame = await asyncio.wait_for(track.recv(), timeout=1.0)
    assert frame.width == 160
    assert frame.height == 120
    assert frame.pts is not None
    assert frame.time_base is not None
    # Round-trip back to ndarray and verify the red channel survived.
    out = frame.to_ndarray(format="rgb24")
    assert out.shape == (120, 160, 3)
    assert out[0, 0, 0] == 255


async def test_camera_track_raises_on_wrong_payload_type():
    fq = FrameQueue("city")
    fq.bind_loop()
    track = CameraTrack(fq, channel_id="city")
    fq.set_latest("not an ndarray")
    with pytest.raises(TypeError):
        await asyncio.wait_for(track.recv(), timeout=1.0)


async def test_camera_track_pts_advances_between_frames():
    fq = FrameQueue("city")
    fq.bind_loop()
    track = CameraTrack(fq, channel_id="city")
    rgb = np.zeros((16, 16, 3), dtype=np.uint8)
    fq.set_latest(rgb)
    f1 = await asyncio.wait_for(track.recv(), timeout=1.0)
    await asyncio.sleep(0.05)
    fq.set_latest(rgb)
    f2 = await asyncio.wait_for(track.recv(), timeout=1.0)
    assert f2.pts > f1.pts
