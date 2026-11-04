# config/redis_client.py
import json
import atexit
import redis
from django.conf import settings

_client = None
_pool = None

def _build_pool():
    # Prefer Django cache LOCATION if you want to keep one source of truth
    url = getattr(settings, "REDIS_URL", None) or settings.CACHES["default"]["LOCATION"]
    # Examples: "redis://127.0.0.1:6379/1" or "rediss://...:6380/0"
    return redis.ConnectionPool.from_url(
        url,
        decode_responses=True,  # return str, not bytes
        max_connections=50,  # tune as needed
        health_check_interval=30,  # keep pool healthy
    )

def get_redis():
    """Return a process-wide singleton Redis client."""
    global _client, _pool
    if _client is None:
        _pool = _build_pool()
        _client = redis.Redis(connection_pool=_pool)
        # optional: close pool on process exit
        atexit.register(lambda: _pool.disconnect())
    return _client

# Convenience helpers (optional)
def set_json(key: str, value: dict, ttl: int | None = None):
    r = get_redis()
    data = json.dumps(value)
    if ttl is None or ttl <= 0:
        r.set(key, data)
    else:
        r.setex(key, ttl, data)

def get_json(key: str) -> dict | None:
    r = get_redis()
    raw = r.get(key)
    return json.loads(raw) if raw else None

def set_string(key: str, value: str, ttl: int | None = None):
    """
    Store a plain string in Redis.
    If ttl is provided (>0), set expiry in seconds.
    If ttl is None or <=0, store forever.
    """
    r = get_redis()
    if ttl is None or ttl <= 0:
        r.set(key, value)
    else:
        r.setex(key, ttl, value)

def get_string(key: str) -> str | None:
    """
    Get a plain string from Redis.
    Returns None if key is missing.
    """
    r = get_redis()
    return r.get(key)
