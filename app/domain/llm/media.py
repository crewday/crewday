"""Media normalization helpers for capability-routed LLM calls."""

from __future__ import annotations

import base64
import binascii
import subprocess
import tempfile
from pathlib import Path
from typing import Final, Literal

from app.adapters.llm.ports import ChatInputAudioRef

__all__ = [
    "AudioInputTransform",
    "MediaTransformError",
    "coerce_audio_input_transform",
    "normalize_audio_ref",
]

AudioInputTransform = Literal["passthrough", "wav_16khz_mono"]

_AUDIO_CONVERSION_TIMEOUT_S: Final[int] = 15
_AUDIO_CONVERSION_MAX_BYTES: Final[int] = 25 * 1024 * 1024


class MediaTransformError(ValueError):
    """Raised when a media input cannot be normalized for the target model."""


def normalize_audio_ref(
    audio_ref: ChatInputAudioRef,
    *,
    transform: str,
    max_bytes: int = _AUDIO_CONVERSION_MAX_BYTES,
) -> ChatInputAudioRef:
    """Return ``audio_ref`` normalized for an LLM provider-model.

    ``passthrough`` copies the original input. ``wav_16khz_mono`` mirrors the
    admin playground's provider-model transform: decode base64 audio, convert
    with ffmpeg to 16 kHz mono signed-16-bit WAV, then re-encode as an
    OpenAI-compatible ``input_audio`` ref.
    """

    if transform == "passthrough":
        return {"data": audio_ref["data"], "format": audio_ref["format"]}
    if transform != "wav_16khz_mono":
        raise MediaTransformError(f"unsupported audio input transform {transform!r}")

    try:
        payload = base64.b64decode(audio_ref["data"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MediaTransformError("audio input is not valid base64") from exc
    if not payload:
        raise MediaTransformError("audio input is empty")

    converted = _ffmpeg_audio_to_wav_16khz_mono(
        payload,
        input_suffix=f".{audio_ref['format']}",
        max_bytes=max_bytes,
    )
    return {
        "data": base64.b64encode(converted).decode("ascii"),
        "format": "wav",
    }


def _ffmpeg_audio_to_wav_16khz_mono(
    payload: bytes, *, input_suffix: str, max_bytes: int
) -> bytes:
    if len(payload) > max_bytes:
        raise MediaTransformError("audio input exceeds size limit")
    with tempfile.TemporaryDirectory(prefix="crewday-llm-audio-") as tmp_raw:
        tmp = Path(tmp_raw)
        input_path = tmp / f"input{input_suffix}"
        output_path = tmp / "output.wav"
        try:
            input_path.write_bytes(payload)
        except OSError as exc:
            raise MediaTransformError("audio conversion failed") from exc
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            "-f",
            "wav",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=_AUDIO_CONVERSION_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaTransformError("audio conversion failed") from exc
        if result.returncode != 0 or not output_path.exists():
            raise MediaTransformError("audio conversion failed")
        try:
            converted = output_path.read_bytes()
        except OSError as exc:
            raise MediaTransformError("audio conversion failed") from exc

    if not converted:
        raise MediaTransformError("audio conversion produced empty output")
    if len(converted) > max_bytes:
        raise MediaTransformError("converted audio exceeds size limit")
    return converted


def coerce_audio_input_transform(value: str | None) -> AudioInputTransform:
    if value == "wav_16khz_mono":
        return "wav_16khz_mono"
    return "passthrough"
