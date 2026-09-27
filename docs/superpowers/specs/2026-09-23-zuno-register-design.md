# zuno_register design spec (2026-09-23)

Port of the gateway's `POST /register/token` into a Synapse module, so the gateway can be retired without adopting Synapse's built-in email registration (which binds the address to the account as a 3PID).

## Goal

- `POST /_synapse/client/zuno/register/token`, body `{"email": "..."}`, same contract the app already has: `202 {}` on accept, `4xx` final, `5xx` retryable.
- Token minted straight into Synapse's store (`create_registration_token`), never through the Admin API, so no admin token exists.
- Per-address state in the Redis Synapse's replication already uses. One live code per address per token TTL; a re-request resends the same code. The send limit is what is rate-limited.
- Nothing about the account ever holds the email. Neither the address nor the code is logged.

## Flow

1. `Content-Type` must be `application/json` (415). Body over 1 KB is 413. Bad JSON or address is 400 `M_INVALID_PARAM`. Address rules ported: trimmed, RFC-parsed, dotted domain, key `HMAC-SHA256(derived from macaroon_secret_key, lower(local-without-+tag@domain))`.
2. Per-IP in-memory token bucket, IPv6 by /64, from `request.getClientAddress()`. Over: 429 `M_LIMIT_EXCEEDED` + `retry_after_ms`.
3. Redis `sends:{hash}`: atomic INCR with the window set on first use to the token TTL. Over `sends_per_address`: DECR, silent `202 {}`.
4. Redis `daily:{YYYY-MM-DD}` (`daily_cap` > 0 only): INCR, expiry 48 h on first use. Over: DECR both, 429.
5. Redis `code:{hash}` GET. Present and `registration_token_is_valid`: reuse. Else mint (retry once on collision), `SET NX EX ttl`; a lost race takes the winner's code and deletes ours from the store.
6. Brevo send (no retry: a send is not idempotent). Failure: DECR both counters, 502 `M_UNKNOWN`. The code stays in Redis for the next attempt.
7. `202 {}`.

Redis or store failure is 502. Any other method or sub-path is 404 `M_UNRECOGNIZED`.

## Private Synapse surface

`api._hs.get_outbound_redis_connection()`, `api._hs.get_datastores().main` (`create_registration_token`, `registration_token_is_valid`, `delete_registration_token`) and `api._hs.config.key.macaroon_secret_key` (seed for the record key), isolated in `synapse_private.py`. Checked against the pinned Synapse in dev deps; `redis.enabled` false is a startup `ConfigError`.

## Config

```yaml
modules:
  - module: zuno_register.ZunoRegister
    config:
      brevo:
        api_key_path: /data/secrets/brevo_api_key   # or api_key
        sender_email: noreply@zuno.chat
        sender_name: Zuno                            # default
        template_id: 0                               # default: built-in copy
        base_url: https://api.brevo.com              # default
        timeout: 10s                                 # default
      token_ttl: 24h                                 # default; min 1h
      sends_per_address: 3                           # default
      daily_cap: 500                                 # default; 0 disables
      rate_limit: {per_second: 0.00167, burst: 3}    # per IP; defaults
      redis_timeout: 2s                              # default, per command
```

No `brevo` section: nothing registered. `rate_limit.burst >= sends_per_address` and a day of the per-IP rate under `daily_cap` are startup errors.

## Metrics

`zuno_register_requests_total{result}`, `zuno_register_upstream_requests_total{api,code}`, `zuno_register_upstream_seconds{api}`, `zuno_register_upstream_errors_total{api,reason}`; `api` in `brevo|redis|store`.

## Tests

Unit: fake ModuleApi/store, dict-backed fake Redis, real local Twisted server for the upstream client. e2e (opt-in, Docker): the module in `synapse:v1.161.0` with a Redis container and a stub Brevo.
