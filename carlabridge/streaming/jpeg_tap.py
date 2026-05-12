"""JPEG encoder for MJPEG tap — numpy RGB → JPEG bytes via PyAV.

Used only by the MJPEG fallback path (spec §F2.2). WebRTC stays raw via the
aiortc VP8 encoder.

Each `encode_jpeg` call creates a one-shot `mjpeg` CodecContext — there's no
state to keep across frames for image2 mjpeg, so this is the simplest path.
For 1280x720@25fps it's ~6 ms per frame on the demo box; acceptable for a
fallback (frontend defaults to WebRTC).
"""

from __future__ import annotations

import fractions
import logging
from typing import TYPE_CHECKING

import av
import av.codec

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

log = logging.getLogger(__name__)


def encode_jpeg(rgb: "np.ndarray", *, quality: int = 80) -> bytes:
    """Encode an HxWx3 uint8 RGB array as JPEG bytes (full SOI/EOI included).

    `quality` is the FFmpeg/MJPEG qscale rough conversion — higher is better
    quality (1..100). Default 80 yields ~25-30 KB per 1280x720 frame.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(
            f"encode_jpeg expects HxWx3 uint8 ndarray, got shape {rgb.shape}"
        )
    h, w = rgb.shape[:2]
    frame = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format="yuvj420p")
    codec = av.codec.CodecContext.create("mjpeg", "w")
    codec.pix_fmt = "yuvj420p"
    codec.width = w
    codec.height = h
    codec.time_base = fractions.Fraction(1, 25)
    # MJPEG qscale: 1 (best) .. 31 (worst). Map quality 1..100 → qscale 31..1.
    q = max(1, min(31, int(round(31 - (quality - 1) * 30 / 99))))
    codec.options = {"qscale": str(q)}

    chunks: list[bytes] = []
    for packet in codec.encode(frame):
        chunks.append(bytes(packet))
    for packet in codec.encode(None):  # flush
        chunks.append(bytes(packet))
    return b"".join(chunks)


__all__ = ["encode_jpeg"]
