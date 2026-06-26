from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import Response, HTMLResponse
from pathlib import Path
from user_agents import parse
import redis.asyncio as aioredis
import asyncio
import base64
import os
import time
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

POCKET_ID_URL       = os.getenv("POCKET_ID_URL", "")
SESSION_PREFIX      = os.getenv("SESSION_PREFIX", "_oauth2_proxy-")
COOKIE_NAME         = os.getenv("OAUTH2_COOKIE_NAME", "_oauth2_proxy")
META_WRITE_INTERVAL = int(os.getenv("META_WRITE_INTERVAL", "60"))
META_TTL            = int(os.getenv("META_TTL", "604800"))
BLOCKLIST_CACHE_TTL = int(os.getenv("BLOCKLIST_CACHE_TTL", "5"))

_html_path = Path(__file__).parent / "forbidden.html"
if _html_path.exists():
    raw_html = _html_path.read_text()
    FORBIDDEN_HTML = raw_html.replace("{POCKET_ID_URL}", POCKET_ID_URL)
else:
    FORBIDDEN_HTML = (
        f"<html><body><h1>403 Forbidden</h1>"
        f"<p>Access denied. <a href='{POCKET_ID_URL}'>Return to login</a></p></body></html>"
    )

# ── In-memory state ───────────────────────────────────────────────────────────

redis_client: aioredis.Redis = None

# Keyed by "handle:destination" — gates Redis writes per destination per session.
# Pruned by background task every META_WRITE_INTERVAL seconds.
_write_cooldown: dict[str, float] = {}

# Keyed by user_id — short-lived cache so the blocklist check doesn't hit Redis
# on every one of the 20-200 subrequests in a single page load.
# Value: (is_blocked, expires_at_monotonic)
_blocklist_cache: dict[str, tuple[bool, float]] = {}

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    try:
        redis_client = aioredis.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        print(f"[authz] Redis connected: {redis_url}", flush=True)
    except Exception as e:
        print(f"[authz] Redis unavailable: {e}", flush=True)
        redis_client = None

    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()
    if redis_client:
        await redis_client.aclose()

app = FastAPI(lifespan=lifespan)

# ── Background cleanup ────────────────────────────────────────────────────────

async def _cleanup_loop():
    """
    Prunes both in-memory caches every META_WRITE_INTERVAL seconds.

    _write_cooldown entries older than META_WRITE_INTERVAL are already inert
    (a new request would pass the check anyway), so removing them just keeps
    memory bounded.

    _blocklist_cache entries are pruned once past their own short TTL so stale
    block/unblock states don't linger longer than intended.
    """
    while True:
        await asyncio.sleep(META_WRITE_INTERVAL)
        now = time.monotonic()

        stale_cd = [k for k, v in list(_write_cooldown.items()) if v < now - META_WRITE_INTERVAL]
        for k in stale_cd:
            _write_cooldown.pop(k, None)

        stale_bl = [k for k, (_, exp) in list(_blocklist_cache.items()) if exp < now]
        for k in stale_bl:
            _blocklist_cache.pop(k, None)

        if stale_cd or stale_bl:
            print(
                f"[authz] Pruned {len(stale_cd)} cooldown / {len(stale_bl)} blocklist "
                f"cache entries (cooldown remaining: {len(_write_cooldown)})",
                flush=True,
            )

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_ticket_handle(cookie_value: str) -> str | None:
    try:
        ticket_b64 = cookie_value.split("|")[0]
        decoded = base64.b64decode(ticket_b64 + "==").decode("utf-8")
        parts = decoded.split(".")
        if len(parts) < 2:
            return None
        redis_key = base64.urlsafe_b64decode(parts[1] + "==").decode("utf-8")
        if redis_key.startswith(SESSION_PREFIX):
            return redis_key.removeprefix(SESSION_PREFIX)
    except Exception as e:
        print(f"[authz] Cookie parse error: {e}", flush=True)
    return None


async def is_blocked(user_id: str) -> bool:
    """
    Returns True if the user is on the blocklist.

    Results are cached in-process for BLOCKLIST_CACHE_TTL seconds (default 5s)
    so that page loads with 20-200 subrequests only hit Redis once per user
    per cache window. The tradeoff is a newly blocked user gets at most
    BLOCKLIST_CACHE_TTL extra seconds of access before the block takes effect.
    """
    now = time.monotonic()
    cached = _blocklist_cache.get(user_id)
    if cached and now < cached[1]:
        return cached[0]
    blocked = bool(await redis_client.exists(f"blocklist:{user_id}"))
    _blocklist_cache[user_id] = (blocked, now + BLOCKLIST_CACHE_TTL)
    return blocked


async def write_session_meta(handle: str, user_id: str, metadata: dict):
    if not redis_client:
        return

    now_mono    = time.monotonic()
    destination = metadata.get("destination", "")

    # ── Cooldown gate (pure in-memory, zero Redis) ───────────────────────────
    # Keyed per handle+destination so visiting service1, service2, service3
    # within the same 60-second window each get their own independent cooldown.
    # Requests 2-200 for the same service in that window are dropped here
    # before any Redis call is made.
    cooldown_key = f"{handle}:{destination}"
    if now_mono - _write_cooldown.get(cooldown_key, 0) < META_WRITE_INTERVAL:
        return
    _write_cooldown[cooldown_key] = now_mono
    # ─────────────────────────────────────────────────────────────────────────

    try:
        meta_key = f"session_meta:{handle}"
        dest_key = f"session_destinations:{handle}"

        # One pipeline replaces the old separate EXISTS + HGETALL + TTL calls.
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.hgetall(meta_key)
            pipe.ttl(f"{SESSION_PREFIX}{handle}")
            existing_meta, ttl = await pipe.execute()

        is_new_session = not bool(existing_meta)
        existing_note  = existing_meta.get("note", "")
        effective_ttl  = ttl if ttl > 0 else META_TTL

        now_iso = datetime.now(timezone.utc).isoformat()
        update  = dict(metadata)
        update["last_seen"] = now_iso
        update["note"]      = existing_note

        if is_new_session:
            update["created_at"] = now_iso
            update["note"]       = ""
            print(f"[authz] New session: {user_id} ({metadata.get('email')}) ticket={handle[:12]}...", flush=True)

            # ── Orphan cleanup ───────────────────────────────────────────────
            # oauth2-proxy silently rotates the cookie on token refresh,
            # creating a new handle and deleting the old _oauth2_proxy-<handle>
            # key without touching session_meta / session_destinations.
            # On first write to the new handle, batch-check all prior handles
            # for this user and delete any whose proxy key is gone.
            known_handles = await redis_client.smembers(f"user_sessions:{user_id}")
            candidates = [h for h in known_handles if h != handle]
            if candidates:
                async with redis_client.pipeline(transaction=False) as pipe:
                    for h in candidates:
                        pipe.exists(f"{SESSION_PREFIX}{h}")
                    alive_flags = await pipe.execute()

                orphaned = [h for h, alive in zip(candidates, alive_flags) if not alive]
                if orphaned:
                    async with redis_client.pipeline(transaction=False) as pipe:
                        for h in orphaned:
                            pipe.delete(f"session_meta:{h}")
                            pipe.delete(f"session_destinations:{h}")
                            pipe.srem(f"user_sessions:{user_id}", h)
                        await pipe.execute()
                    print(f"[authz] Cleaned {len(orphaned)} orphaned handle(s) for {user_id}", flush=True)
            # ────────────────────────────────────────────────────────────────

        # ── Write pipeline (single round-trip) ───────────────────────────────
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.hset(meta_key, mapping=update)
            pipe.expire(meta_key, effective_ttl)
            if destination and destination != "unknown":
                pipe.hset(dest_key, destination, now_iso)
                pipe.expire(dest_key, effective_ttl)
            pipe.sadd(f"user_sessions:{user_id}", handle)
            pipe.expire(f"user_sessions:{user_id}", META_TTL)
            await pipe.execute()
        # ─────────────────────────────────────────────────────────────────────

    except Exception as e:
        print(f"[authz] Meta write failed: {e}", flush=True)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/auth")
async def auth(request: Request):
    groups     = request.headers.get("X-Auth-Request-Groups", "")
    required   = request.query_params.get("group", "")
    group_list = [g.strip() for g in groups.split(",") if g.strip()]

    user_id = request.headers.get("X-Auth-Request-User")

    if user_id and redis_client:
        if await is_blocked(user_id):
            print(f"[authz] BLOCKED: {user_id}", flush=True)
            return HTMLResponse(content=FORBIDDEN_HTML, status_code=403)

        cookie_value = request.cookies.get(COOKIE_NAME, "")
        handle = extract_ticket_handle(cookie_value) if cookie_value else None

        if handle:
            ua = parse(request.headers.get("User-Agent", ""))
            metadata = {
                "user_id":     user_id,
                "email":       request.headers.get("X-Auth-Request-Email", ""),
                "ip":          request.headers.get("X-Real-IP", ""),
                "device":      f"{ua.browser.family} / {ua.os.family}",
                "destination": request.headers.get("X-Forwarded-Host", "unknown"),
            }
            await write_session_meta(handle, user_id, metadata)
        else:
            print(f"[authz] Could not extract ticket handle for {user_id}", flush=True)

    if not required:
        return Response("OK", status_code=200)

    if required in group_list:
        return Response("OK", status_code=200)

    print(f"[authz] DENIED — '{required}' not in {group_list}", flush=True)
    return HTMLResponse(content=FORBIDDEN_HTML, status_code=403)