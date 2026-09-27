# zuno_register design

Synapse module gating signup behind proof of inbox control: the app posts an email address, the module mints a single-use registration token into Synapse's own store and emails it via Brevo. The gate is email verification, not an invite list, and not Synapse's built-in email registration: that one binds the address to the account as a 3PID, while here nothing about the account ever holds it. Synapse holds an opaque token; Redis holds a hash of the address; Brevo is the only party that sees the address.

## Route

`POST /_synapse/client/zuno/register/token`, body `{"email": "..."}`. Public and unauthenticated by nature: the caller has no account yet. The app then registers through the normal client API, completing the `m.login.registration_token` stage with the emailed code.

## Request flow

1. Route: POST at the exact path, else 404 `M_UNRECOGNIZED` (any other verb or sub-path).
2. `Content-Type` must be `application/json`, else 415 `M_UNKNOWN`. A browser sends cross-site JSON only after a CORS preflight, which this route never answers; a "simple" type would let any web page spend its visitors' per-IP allowance. The native app never preflights.
3. Body over 1 KB is 413 `M_TOO_LARGE`. Bad JSON, any key but `email`, or a bad address is 400 `M_INVALID_PARAM`. An address is a dot-atom local part and a domain of at least two labels, trimmed, as typed. Its record key is an HMAC-SHA256 of `lower(local@domain)`, any `+tag` dropped, under a key derived from Synapse's `macaroon_secret_key`: a Redis dump alone cannot say whether a given address has a record, and one inbox cannot mint itself a fresh record per spelling.
4. Per-IP token bucket (in memory, IPv6 by /64, via `request.getClientAddress()`, so the listener's `x_forwarded` applies). Over: 429 `M_LIMIT_EXCEEDED` with `retry_after_ms`. Counted before the registrar runs, a `400` never spends it.
5. Redis `sends:{key}`: INCR, window set atomically on the first count to `token_ttl`. Over `sends_per_address`: DECR and the silent `202 {}`, no send.
6. Redis `daily:{UTC date}` when `daily_cap` > 0: INCR, 48 h window. Over: DECR and 429 `M_LIMIT_EXCEEDED`. Checked after the address counter and never spent by a refused request, so a drained cap cannot be used to spend a stranger's record.
7. Redis `code:{key}`: a code that is there and that the store still reports valid is reused. Otherwise a fresh code is minted with `create_registration_token(code, 1, expiry)` (one retry on collision) and stored with `SET NX EX token_ttl`; a lost race takes the winner's code and deletes ours from the store. A consumed code is replaced outright.
8. Brevo send, one attempt: a send is not idempotent. Failure refunds both counters and answers 502 `M_UNKNOWN`; the code stays, so the next attempt resends it.
9. `202 {}`, `Cache-Control: no-store`. Byte-identical whether a mail went out or the address was over its limit.

The 202 for a limited address is truthful: the code it refers to is the one already in the inbox. A `429` there would confirm to anyone probing that the address was asked for recently. The timing still differs (no Brevo call); the per-IP limit blunts trials.

Redis or store failure is 502 (the retryable class for the client). Neither the address nor the code reaches a log line or an error body: failures log the exception's type, never its text (a database error's message can carry the token), and only Brevo's status, since its body may echo the address.

## Why Redis, and the private surface

The record has to survive a restart and be shared by every process that could serve the route, and it holds the live code so a resend can deliver the same one. It rides Synapse's own replication connection (`hs.get_outbound_redis_connection()`), keys prefixed `zuno_register:{server_name}:`, every command bounded by `redis_timeout`. Counters are one Lua `EVAL` so the window is set atomically with the first count.

That handle, the store (`hs.get_datastores().main`) and the macaroon secret (`hs.config.key.macaroon_secret_key`, the seed for the record key) are the three private Synapse attributes the module touches, isolated in `synapse_private.py`. They are outside the module-API compatibility promise: a Synapse bump means checking that file against the new version, with the dev pin in `pyproject.toml` as the reference. `redis.enabled: false` is a startup `ConfigError`. The trade accepted here is that private surface against holding an admin token: the store call can mint a token and nothing else.

## Config

```yaml
modules:
  - module: zuno_register.ZunoRegister
    config:
      brevo:
        api_key_path: /data/secrets/brevo_api_key   # or api_key
        sender_email: noreply@zuno.chat
        sender_name: Zuno                            # default
        template_id: 0                               # default: built-in copy; else {code, expires_in_hours}
        base_url: https://api.brevo.com              # default
        timeout: 10s                                 # default, one attempt
      token_ttl: 24h                                 # default; min 1h (the mail says whole hours)
      sends_per_address: 3                           # default, per token_ttl
      daily_cap: 500                                 # default; 0 disables
      rate_limit: {per_second: 0.00167, burst: 3}    # per IP; defaults (one per 10 min)
      redis_timeout: 2s                              # default, per command
```

- No `brevo` section: the module loads and registers nothing (Synapse answers 404). That is the off switch.
- Startup errors, all reported at once: `rate_limit.burst < sends_per_address` (the re-requests from one address would be refused before its last code), and a day of the per-IP rate at or over `daily_cap` (one address could close signup for everyone). At the defaults one address gets 147 requests a day against a cap of 500.
- Secrets: exactly one of inline or `_path`; the file is read verbatim minus one trailing newline.
- Durations: `500ms`, `10s`, `2h`, `1d` or integer seconds.
- Synapse must have `enable_registration: true` with `registration_requires_token: true`. Redis state is shared, so the resource works on any process; the per-IP limiter is per process.

## Client contract

Codes are ten characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no `I`/`O`/`0`/`1`). The app filters its input field to that alphabet and renders the TTL as a literal "24 hours"; widening the alphabet or changing `token_ttl` needs a client release alongside. The app must send `Content-Type: application/json`. It retries only `5xx`; every `4xx` is final. A silent 202 with nothing in the inbox means the address has used its sends this window, or a mail Brevo accepted and never delivered; the module cannot tell the two apart.

## Metrics

`zuno_register_requests_total{result}` with `result` in `ok|resent|email_limited|ip_limited|global_limited|invalid|too_large|unsupported_media_type|redis_error|store_error|brevo_error`; `zuno_register_upstream_requests_total{api,code}`, `zuno_register_upstream_seconds{api}`, `zuno_register_upstream_errors_total{api,reason}` with `api` in `brevo|redis|store`. Synapse's `synapse_http_server_*` label the route by class name `RegisterResource`.

## Tests

`make test`: unit tests with a fake store, a dict-backed fake Redis connection and a fake mailer, plus a real local Twisted server for the upstream client. `make e2e` (Docker, opt-in): the module inside `synapse:v1.161.0` with a Redis container and a stub Brevo; the emailed code registers a real user through Synapse's own registration-token stage.
