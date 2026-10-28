from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import OrderedDict
from .config import settings

log = logging.getLogger(__name__)

class SessionStore:
    """TTL-bounded conversation history with optional shared Redis storage.

    Redis is preferred when REDIS_URL is configured. If Redis is unavailable, the
    service degrades to bounded process-local memory and emits a warning; that mode
    is not shared between workers and histories are lost on restart.
    """
    def __init__(self):
        self._data = OrderedDict()
        self._lock = asyncio.Lock()
        self._redis = None
        self._redis_checked = False

    async def _get_redis(self):
        if not settings.redis_url:
            return None
        if self._redis is not None:
            return self._redis
        if self._redis_checked:
            return None
        self._redis_checked = True
        try:
            from redis.asyncio import Redis
            client = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
            await client.ping()
            self._redis = client
            log.info("Shared Redis session storage enabled")
            return client
        except Exception:
            log.warning("Redis session storage unavailable; using process-local sessions", exc_info=True)
            return None

    def _redis_key(self, session_id: str) -> str:
        import hashlib
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return f"jimozi:session:{digest}"

    async def get(self, session_id: str):
        redis = await self._get_redis()
        if redis is not None:
            try:
                raw = await redis.get(self._redis_key(session_id))
                return json.loads(raw) if raw else []
            except Exception:
                log.warning("Redis session read failed; falling back to local state", exc_info=True)
        async with self._lock:
            item = self._data.get(session_id)
            if not item:
                return []
            created, history = item
            if time.time() - created > settings.session_ttl_seconds:
                self._data.pop(session_id, None)
                return []
            self._data.move_to_end(session_id)
            return list(history)

    async def append(self, session_id: str, user: str, assistant: str):
        redis = await self._get_redis()
        if redis is not None:
            try:
                key = self._redis_key(session_id)
                raw = await redis.get(key)
                history = json.loads(raw) if raw else []
                history.extend([{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])
                history = history[-max(2, settings.max_history_turns * 2):]
                await redis.set(key, json.dumps(history, ensure_ascii=False), ex=max(60, settings.session_ttl_seconds))
                return
            except Exception:
                log.warning("Redis session write failed; using local state", exc_info=True)
        async with self._lock:
            item = self._data.get(session_id)
            history = list(item[1]) if item and time.time() - item[0] <= settings.session_ttl_seconds else []
            history.extend([{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])
            history = history[-max(2, settings.max_history_turns * 2):]
            self._data[session_id] = (time.time(), history)
            self._data.move_to_end(session_id)
            while len(self._data) > settings.max_local_sessions:
                self._data.popitem(last=False)

    async def clear(self, session_id: str):
        redis = await self._get_redis()
        if redis is not None:
            try:
                await redis.delete(self._redis_key(session_id))
            except Exception:
                log.warning("Redis session clear failed", exc_info=True)
        async with self._lock:
            self._data.pop(session_id, None)

    async def close(self):
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
