# oauth2-proxy-authz

A lightweight FastAPI sidecar that plugs into Traefik's `forwardAuth` middleware to add **group-based access control** and **session visibility** on top of [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy).

Designed to work alongside [oauth2-proxy-session-admin](https://github.com/arm64buidz/oauth2-proxy-session-admin), [Pocket-ID](https://github.com/pocket-id/pocket-id), and Redis as part of a self-hosted SSO stack.

---

## What it does

By itself, oauth2-proxy only answers one question: *is this user authenticated?* This service answers a second question: *is this authenticated user allowed here?*

On every request Traefik forwards for authorization, this service:

1. **Checks the blocklist** — if the user's ID has been blocked via the session-admin UI, it returns `403` immediately with a styled forbidden page.
2. **Checks group membership** — reads the `X-Auth-Request-Groups` header (populated by oauth2-proxy from the OIDC token) and compares it against the group required by the router rule (e.g. `?group=administrator`). Returns `403` if the user is not a member.
3. **Writes session metadata to Redis** — stores user ID, email, IP, browser/OS, and which service they accessed. This data is what powers the session-admin dashboard.

All three happen in a single `GET /auth` call, with in-memory caching and cooldown gating to keep Redis load minimal even on pages with many subrequests.

---

## Architecture overview

```
Browser
  │
  ▼
Traefik
  │  forwardAuth ──► oauth2-proxy  (authentication: is the user logged in?)
  │  forwardAuth ──► authz         (authorization: is the user allowed here?)
  │
  ▼
Your protected service
```

A Traefik router that requires both authentication and group membership uses two chained `forwardAuth` middlewares:

```yaml
# traefik dynamic config
middlewares:
  oauth2-auth:
    forwardAuth:
      address: "http://oauth2-proxy:4180"
      authResponseHeaders:
        - X-Auth-Request-User
        - X-Auth-Request-Email
        - X-Auth-Request-Access-Token
        - X-Auth-Request-Groups

  authz-group-admin:
    forwardAuth:
      address: "http://authz:8080/auth?group=administrator"

routers:
  my-protected-router:
    rule: Host(`app.example.duckdns.org`)
    middlewares:
      - oauth2-auth        # runs first — sets X-Auth-Request-* headers
      - authz-group-admin  # runs second — reads those headers
```

Traefik applies middlewares in order and passes the `authResponseHeaders` from the first call into the request seen by the second, so `authz` always has the group list available.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379` | Connection URL for the shared Redis instance |
| `POCKET_ID_URL` | *(empty)* | Base URL of your Pocket-ID instance. Used in the 403 forbidden page to provide a "return to login" link |
| `SESSION_PREFIX` | `_oauth2_proxy-` | Key prefix oauth2-proxy uses when storing session tickets in Redis. Must match your oauth2-proxy config |
| `OAUTH2_COOKIE_NAME` | `_oauth2_proxy` | Name of the session cookie. Must match your oauth2-proxy config |
| `META_WRITE_INTERVAL` | `60` | Seconds between Redis writes for a given session+destination pair. Prevents a flood of writes on pages with many subrequests |
| `META_TTL` | `604800` | TTL in seconds (default 7 days) applied to session metadata keys when the original session TTL cannot be determined |
| `BLOCKLIST_CACHE_TTL` | `5` | Seconds to cache a blocklist lookup in memory. A newly blocked user may have up to this many extra seconds of access |

---

## How session tracking works

Every authorized request passes through this service. When a request arrives, the service:

- Parses the `_oauth2_proxy` cookie to extract the **session ticket handle** — the key used to look up the session in Redis.
- Checks whether metadata for this handle already exists and what the live session TTL is.
- Writes (or updates) `session_meta:<handle>` and `session_destinations:<handle>` hash keys in Redis.
- Adds the handle to a `user_sessions:<user_id>` set for efficient per-user lookups.

To avoid hammering Redis, writes are gated by an in-memory cooldown keyed on `handle:destination`. The first request to a given service in a `META_WRITE_INTERVAL` window writes to Redis; subsequent requests in that window are dropped silently.

### Orphan cleanup

oauth2-proxy silently rotates session cookies on token refresh — the old Redis key is deleted and a new handle is issued — without cleaning up the `session_meta` and `session_destinations` keys. On the first write for a new handle, this service batch-checks all previously known handles for the same user and deletes any whose underlying proxy key is gone.

---

## API

### `GET /auth`

The forwardAuth endpoint called by Traefik.

**Query parameters:**

| Parameter | Description |
|---|---|
| `group` | *(optional)* Name of the group the user must belong to. If omitted, any authenticated user is allowed through |

**Returns `200 OK`** if the user is authenticated, not blocked, and (if `group` is set) is a member of that group.

**Returns `403 Forbidden`** (HTML) if the user is blocked or not in the required group.

---

## Docker

```yaml
authz:
  image: arm64buidz/oauth2-proxy-authz:2026.6
  environment:
    - REDIS_URL=redis://oauth2-proxy-redis:6379
    - POCKET_ID_URL=https://auth.example.duckdns.org
    - META_WRITE_INTERVAL=60
  networks:
    - proxy-auth
  depends_on:
    - oauth2-proxy-redis
  restart: unless-stopped
```

The service exposes port `8080` internally but does not need to be published externally — Traefik reaches it over the Docker network.

---

## Related projects

- **[oauth2-proxy-session-admin](https://github.com/arm64buidz/oauth2-proxy-session-admin)** — companion web UI and API for viewing and managing the sessions written by this service
- **[oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy)** — the authentication layer this service extends
- **[Pocket-ID](https://github.com/pocket-id/pocket-id)** — the OIDC identity provider used in the reference stack

For a full working example including Traefik config, docker-compose, and .env template, see the reference stack repository.
