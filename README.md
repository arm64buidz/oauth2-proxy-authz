# oauth2-proxy-authz

A lightweight Python sidecar that adds **group-based authorization** on top of [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy). Works alongside [Pocket ID](https://github.com/pocket-id/pocket-id), [Redis](https://redis.io/), and [Traefik](https://traefik.io/) to restrict individual services to specific Pocket ID groups — rather than just "authenticated or not."

> **Note:** `oauth2-proxy-authz` can run independently. The companion [oauth2-proxy-session-admin](https://github.com/arm64buidz/oauth2-proxy-session-admin) provides a management UI on top of the same stack but is not required.

---

## How It Works

The full stack runs under a single `auth.` subdomain — no extra DNS records needed.

```
Request
  └─▶ Traefik
        ├─▶ oauth2-auth middleware → oauth2-proxy:4180       (authn: is the user logged in?)
        └─▶ authz-group-* middleware → authz:8080/auth       (authz: are they in the right group?)
                                            │
                                     200 Allow / 403 Deny → forbidden.html
```

Group metadata is periodically synced from Pocket ID and cached in Redis. The authz check is a simple Traefik `ForwardAuth` call with the target group as a query parameter.

---

## Prerequisites

- [Pocket ID](https://github.com/pocket-id/pocket-id) — OIDC provider
- [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) — authentication layer
- [Redis](https://redis.io/) — shared session/group store
- [Traefik](https://traefik.io/) — reverse proxy

All containers must share a Docker network. See the included `example-docker-compose.yaml`.

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/arm64buidz/oauth2-proxy-authz.git
cd oauth2-proxy-authz
```

### 2. Configure your environment

```bash
cp .env.example .env
```

| Variable              | Description                                                 | Example                           |
|-----------------------|-------------------------------------------------------------|-----------------------------------|
| `REDIS_URL`           | Redis connection string                                     | `redis://oauth2-proxy-redis:6379` |
| `POCKET_ID_URL`       | Base URL of your Pocket ID instance                         | `https://auth.example.duckdns.org` |
| `META_WRITE_INTERVAL` | How often (in seconds) to sync group metadata from Pocket ID | `60`                             |

### 3. Start the stack

```bash
docker compose up -d
```

---

## Traefik Integration

`oauth2-proxy-authz` runs on port `8080` and is called by Traefik as a `ForwardAuth` middleware. The group to check is passed as a query parameter.

### Middleware definition

```yaml
http:
  middlewares:
    oauth2-auth:
      forwardAuth:
        address: "http://oauth2-proxy:4180"
        trustForwardHeader: true
        authResponseHeaders:
          - X-Auth-Request-User
          - X-Auth-Request-Email
          - X-Auth-Request-Access-Token
          - X-Auth-Request-Groups

    authz-group-admin:
      forwardAuth:
        address: "http://authz:8080/auth?group=administrator"

    authz-group-user:
      forwardAuth:
        address: "http://authz:8080/auth?group=user"
```

### Applying to a router

Stack both middlewares — `oauth2-auth` first (authn), then your group middleware (authz):

```yaml
http:
  routers:
    my-service:
      rule: "Host(`auth.example.duckdns.org`) && PathPrefix(`/my-service`)"
      middlewares:
        - oauth2-auth
        - authz-group-admin   # only members of 'administrator' get through
      service: my-service
      priority: 250
```

To restrict to a different group, define a new middleware pointing to `/auth?group=<your-group-name>` and apply it the same way.

### Router priority

Because everything shares a single subdomain, router priority matters. The included example uses:

| Priority | Router                  |
|----------|-------------------------|
| 260      | `/oauth2/` callback     |
| 250      | Protected service paths |
| 100      | Pocket ID catch-all     |

---

## Service Ports

| Service          | Port   |
|------------------|--------|
| Pocket ID        | `1411` |
| oauth2-proxy     | `4180` |
| oauth2-proxy-authz | `8080` |

---

## Related Projects

- [oauth2-proxy-session-admin](https://github.com/arm64buidz/oauth2-proxy-session-admin) — session visibility and management UI (optional)
- [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy)
- [Pocket ID](https://github.com/pocket-id/pocket-id)

---

## License

MIT