"""Prometheus series, registered on the default registry Synapse's metrics listener serves."""

from prometheus_client import Counter, Histogram

REQUESTS = Counter("zuno_register_requests_total", "Registration requests by outcome", ["result"])
UPSTREAM_REQUESTS = Counter(
    "zuno_register_upstream_requests_total", "Brevo responses by status", ["api", "code"]
)
UPSTREAM_SECONDS = Histogram(
    "zuno_register_upstream_seconds",
    "Brevo, Redis and store call latency per attempt",
    ["api"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
UPSTREAM_RETRIES = Counter(
    "zuno_register_upstream_retries_total", "Retried upstream attempts", ["api"]
)
UPSTREAM_ERRORS = Counter(
    "zuno_register_upstream_errors_total",
    "Brevo, Redis and store calls with no usable response",
    ["api", "reason"],
)
