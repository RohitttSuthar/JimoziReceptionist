from __future__ import annotations
import asyncio
import base64
import io
import logging
from collections import OrderedDict
from typing import Any
import numpy as np
from .config import settings

log = logging.getLogger(__name__)

class TTSService:
    """Lazy-loaded local Supertonic engine with bounded in-memory audio cache."""
    def __init__(self) -> None:
        self._engine = None
        self._voice_style = None
        self._voice_styles: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[tuple[str, str, str, int, float], str] = OrderedDict()

    async def synthesize(self, text: str, voice: str | None = None, lang: str | None = None) -> dict[str, Any]:
        voice = voice or settings.supertonic_voice
        lang = lang or settings.supertonic_language
        key = (text, voice, lang, settings.supertonic_steps, settings.supertonic_speed)
        if key in self._cache:
            self._cache.move_to_end(key)
            return {"audio_base64": self._cache[key], "format": "wav", "cached": True}
        if not settings.supertonic_enabled:
            return {"audio_base64": None, "format": None, "disabled": True}
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return {"audio_base64": self._cache[key], "format": "wav", "cached": True}
            try:
                await asyncio.to_thread(self._load_engine)
                style = self._voice_styles.get(voice)
                if style is None:
                    style = await asyncio.to_thread(self._engine.get_voice_style, voice_name=voice)
                    self._voice_styles[voice] = style
                wav, _duration = await asyncio.to_thread(
                    self._engine.synthesize, text, voice_style=style,
                    lang=lang, total_steps=settings.supertonic_steps, speed=settings.supertonic_speed,
                    max_chunk_length=180
                )
                wav = np.asarray(wav).squeeze()
                if wav.ndim != 1:
                    wav = wav.reshape(-1)
                import soundfile as sf
                buffer = io.BytesIO()
                sf.write(buffer, wav, 44100, format="WAV", subtype="PCM_16")
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                self._cache[key] = encoded
                self._cache.move_to_end(key)
                while len(self._cache) > max(1, settings.tts_cache_size):
                    self._cache.popitem(last=False)
                return {"audio_base64": encoded, "format": "wav", "cached": False}
            except Exception as exc:
                log.exception("Supertonic synthesis failed")
                return {"audio_base64": None, "format": None, "error": str(exc)}

    async def warmup(self) -> None:
        """Load local TTS in the background so the first customer turn avoids model startup."""
        if not settings.supertonic_enabled:
            return
        async with self._lock:
            try:
                await asyncio.to_thread(self._load_engine)
                voice = settings.supertonic_voice
                if voice not in self._voice_styles:
                    self._voice_styles[voice] = await asyncio.to_thread(
                        self._engine.get_voice_style, voice_name=voice
                    )
                log.info("Supertonic warm-up completed")
            except Exception:
                # Keep the service available; the first /api/speak call can retry and report errors.
                log.exception("Supertonic warm-up failed")

    def _load_engine(self) -> None:
        if self._engine is not None:
            return
        from supertonic import TTS
        self._engine = TTS(auto_download=True)

class GroundedRewriter:
    """Optional LLM rephrasing of a retrieved answer only. Off by default."""
    async def rewrite(self, question: str, grounded_answer: str, product_name: str) -> str | None:
        if not settings.enable_grounded_llm_rewrite or not settings.openai_api_key:
            return None
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=8.0, max_retries=0)
            response = await client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": (
                        f"आप {product_name} के हिंदी ग्राहक-सहायता रिसेप्शनिस्ट हैं। केवल दिए गए तथ्य इस्तेमाल करें। "
                        "नई जानकारी, तारीख, कीमत, वादा, नीति या अनुमान न जोड़ें। यदि उत्तर को सुरक्षित रूप से सरल नहीं बना सकते, "
                        "तो दिए गए उत्तर को ज्यों का त्यों लौटाएँ। केवल उत्तर दें।"
                    )},
                    {"role": "user", "content": f"ग्राहक का प्रश्न: {question}\nस्वीकृत तथ्य/उत्तर: {grounded_answer}"}
                ], temperature=0, max_tokens=100
            )
            candidate = (response.choices[0].message.content or "").strip()
            # Conservative guardrail: use rewrite only if it shares enough meaningful tokens with source.
            # Still not a mathematical hallucination guarantee, so strict mode is the default.
            import re
            norm = lambda s: set(re.findall(r"[\w]+", s.lower()))
            source = norm(grounded_answer)
            output = norm(candidate)
            if candidate and source and len(source & output) / max(1, len(output)) >= 0.45:
                return candidate
        except Exception:
            log.exception("Optional grounded LLM rewrite failed; returning canonical FAQ answer")
        return None
