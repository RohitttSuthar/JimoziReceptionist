"""Local Shruti Hindi/English/Hinglish speech-to-text adapter.

The model is downloaded once from Hugging Face and cached locally. No browser
SpeechRecognition service or cloud transcription API is used.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
os.environ.setdefault("NEEDLE_TELEMETRY", "0")

_MODEL: Any = None
_MODEL_LOCK = asyncio.Lock()


def _load_model() -> Any:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from huggingface_hub import hf_hub_download
    from needle import Whistle

    model_path = hf_hub_download(repo_id="udgamlabs/Shruti", filename="shruti.cact")
    _MODEL = Whistle(model_path)
    return _MODEL


async def warmup() -> None:
    """Load/download Shruti in the background so the first caller avoids cold start."""
    global _MODEL
    if _MODEL is not None:
        return
    async with _MODEL_LOCK:
        if _MODEL is not None:
            return
        try:
            await asyncio.to_thread(_load_model)
            log.info("Shruti STT model ready")
        except Exception as exc:
            # Startup should remain healthy if the model host is temporarily unavailable.
            log.warning("Shruti STT warm-up failed; it will retry on first transcription (%s)", exc)


async def transcribe_wav(wav_bytes: bytes) -> dict[str, str]:
    """Transcribe a small mono 16 kHz WAV payload; returns only text and language."""
    if not wav_bytes or len(wav_bytes) > 8 * 1024 * 1024:
        raise ValueError("Audio is empty or too large")
    async with _MODEL_LOCK:
        model = await asyncio.to_thread(_load_model)
        def run() -> dict[str, str]:
            with tempfile.NamedTemporaryFile(prefix="jimozi-stt-", suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                path = Path(f.name)
            try:
                result = model.transcribe(str(path))
                if isinstance(result, str):
                    return {"text": result.strip(), "language": ""}
                return {
                    "text": str((result or {}).get("text", "")).strip(),
                    "language": str((result or {}).get("language", "")).strip(),
                }
            finally:
                path.unlink(missing_ok=True)
        return await asyncio.to_thread(run)
