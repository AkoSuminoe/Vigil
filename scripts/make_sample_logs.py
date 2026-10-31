"""Generate the fake sample logs in sample_logs/ (seeded, so output is reproducible).

All hosts use private or documentation IP ranges and all ids are random; there is
no real personal data.

    python scripts/make_sample_logs.py
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "sample_logs"


class Log:
    def __init__(self, seed: int, start: str) -> None:
        self.rng = random.Random(seed)
        self.t = datetime.fromisoformat(start)
        self.lines: list[str] = []

    def tick(self, lo: float = 0.5, hi: float = 9.0) -> None:
        self.t += timedelta(seconds=self.rng.uniform(lo, hi))

    def emit(self, level: str, service: str, message: str) -> None:
        self.lines.append(f"{self.t:%Y-%m-%d %H:%M:%S} {level} [{service}] {message}")

    def raw(self, line: str) -> None:
        self.lines.append(line)

    def uid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def req(self) -> str:
        return f"req-{self.rng.getrandbits(32):08x}"

    def client_ip(self) -> str:
        return f"203.0.113.{self.rng.randint(2, 254)}"

    def write(self, name: str, out_dir: Path = OUT_DIR) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        print(f"{out_dir.name}/{name}: {len(self.lines)} lines")

    def noise(self, n: int, services: list[str]) -> None:
        """Healthy background traffic."""
        r = self.rng
        for _ in range(n):
            self.tick(1, 6)
            svc = r.choice(services)
            roll = r.random()
            if roll < 0.7:
                self.emit("INFO", svc, f"GET /api/v1/{r.choice(['items', 'cart', 'me', 'orders'])} -> 200 ({r.randint(8, 160)}ms) req={self.req()}")
            elif roll < 0.85:
                self.emit("DEBUG", svc, f"gc pause {r.randint(2, 40)}ms heap={r.randint(200, 900)}MB")
            else:
                self.emit("WARNING", svc, f"slow handler {r.randint(800, 1500)}ms for GET /api/v1/items")


def payments_incident() -> None:
    """Checkout outage: a slow ledger query exhausts the payments DB pool -> cascading 500s."""
    log = Log(seed=7, start="2026-10-31 10:00:00")
    r = log.rng
    paths = ["GET /api/v1/products", "GET /api/v1/cart", "POST /api/v1/cart/items", "GET /api/v1/orders", "POST /api/v1/checkout"]
    log.emit("INFO", "api-gateway", "starting api-gateway v3.14.2 on 0.0.0.0:8080")
    log.emit("INFO", "payments", "HikariPool-1 - Start completed (maximumPoolSize=20)")

    def healthy_traffic(n: int) -> None:
        for _ in range(n):
            log.tick(1, 8)
            roll = r.random()
            if roll < 0.55:
                log.emit("INFO", "api-gateway", f"{r.choice(paths)} -> 200 ({r.randint(12, 180)}ms) client={log.client_ip()} req={log.req()}")
            elif roll < 0.70:
                log.emit("INFO", "payments", f"charge authorised order=ord_{r.randint(10000, 99999)} amount={r.randint(5, 400)}.{r.randint(0, 99):02d} EUR")
            elif roll < 0.80:
                log.emit("INFO", "orders", f"order ord_{r.randint(10000, 99999)} created with {r.randint(1, 6)} items")
            elif roll < 0.86:
                log.emit("WARNING", "api-gateway", f"cache miss ratio {r.randint(31, 44)}% above target 30% for key prefix product:")
            elif roll < 0.91:
                log.emit("WARNING", "auth", f"JWT expired for session {log.uid()}, asking client to refresh")
            elif roll < 0.95:
                log.emit("DEBUG", "inventory", f"stock sync batch {r.randint(100, 999)} applied ({r.randint(10, 90)} skus)")
            else:
                log.emit("INFO", "inventory", f"reserved {r.randint(1, 4)} units of sku-{r.randint(1000, 9999)}")

    healthy_traffic(130)

    # Unrelated, contained issue: event broker blips.
    for _ in range(4):
        log.tick(5, 30)
        log.emit("ERROR", "inventory", "failed to publish event to topic stock-updates: broker 10.0.4.12:9092 not available, will retry")
        healthy_traffic(6)

    # 10:18-ish: nightly reconciliation starts a slow query that holds connections.
    log.emit("INFO", "payments", "reconciliation job started for ledger window 2026-10-30")
    pool = 6
    for _ in range(25):
        log.tick(2, 6)
        pool = min(20, pool + r.randint(0, 2))
        if r.random() < 0.5:
            log.emit("WARNING", "payments", f"slow query {r.randint(4200, 9800)}ms: SELECT * FROM ledger_entries WHERE account_id = {r.randint(10000, 99999)} ORDER BY created_at")
        if pool >= 15:
            log.emit("WARNING", "payments", f"HikariPool-1 - pool usage high: active={pool}, idle={20 - pool}, waiting={max(0, pool - 15) * 3}")
        healthy_traffic(2)

    # Pool exhausted: payments can't get connections, orders and gateway cascade.
    log.emit("CRITICAL", "payments", "HikariPool-1 - Connection is not available, request timed out after 30000ms (total=20, active=20, idle=0, waiting=48)")
    for i in range(70):
        log.tick(0.3, 3)
        roll = r.random()
        if roll < 0.30:
            log.emit("ERROR", "payments", f"Timeout acquiring DB connection after 5000ms (active=20, idle=0, waiting={r.randint(30, 90)}) order=ord_{r.randint(10000, 99999)}")
        elif roll < 0.38:
            log.emit("CRITICAL", "payments", f"HikariPool-1 - Connection is not available, request timed out after 30000ms (total=20, active=20, idle=0, waiting={r.randint(40, 120)})")
        elif roll < 0.58:
            log.emit("ERROR", "orders", f"upstream payments returned 503 for order ord_{r.randint(10000, 99999)} after {r.randint(5000, 5200)}ms")
        elif roll < 0.85:
            log.emit("ERROR", "api-gateway", f"POST /api/v1/checkout -> 500 ({r.randint(5000, 30500)}ms) client={log.client_ip()} req={log.req()}")
        elif roll < 0.92:
            log.emit("WARNING", "api-gateway", f"circuit breaker payments-svc half-open, failure rate {r.randint(52, 97)}%")
        else:
            log.emit("INFO", "api-gateway", f"{r.choice(paths[:3])} -> 200 ({r.randint(15, 200)}ms) client={log.client_ip()} req={log.req()}")
        if i == 35:
            log.emit("ERROR", "payments", "java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available")
            log.raw("    at com.zaxxer.hikari.pool.HikariPool.createTimeoutException(HikariPool.java:696)")
            log.raw("    at com.shop.payments.ChargeRepository.save(ChargeRepository.java:88)")
            log.raw("    at com.shop.payments.ChargeService.authorise(ChargeService.java:142)")

    log.emit("WARNING", "payments", "reconciliation job still running after 1500s")
    healthy_traffic(15)
    log.write("payments_incident.log")


def auth_service() -> None:
    """Mostly healthy auth stack with a Redis failover blip, SMTP retries and an expiring cert."""
    log = Log(seed=21, start="2026-10-31 08:00:00")
    r = log.rng
    log.emit("WARNING", "auth", "TLS certificate for auth.internal.example expires in 6 days (2026-11-06)")
    for i in range(330):
        log.tick(1, 12)
        roll = r.random()
        if roll < 0.40:
            log.emit("INFO", "auth", f"login succeeded user={log.uid()} method={r.choice(['password', 'sso', 'passkey'])} ip={log.client_ip()}")
        elif roll < 0.52:
            log.emit("INFO", "auth", f"token refreshed session={log.uid()}")
        elif roll < 0.62:
            log.emit("WARNING", "auth", f"login failed: invalid password user={log.uid()} ip={log.client_ip()} attempt={r.randint(1, 3)}")
        elif roll < 0.66:
            log.emit("WARNING", "api-gateway", f"rate limit exceeded for client {log.client_ip()} on POST /api/v1/login ({r.randint(21, 60)} req/min)")
        elif roll < 0.74:
            log.emit("INFO", "notifications", f"email queued template=login_alert id={log.uid()}")
        elif roll < 0.80:
            log.emit("DEBUG", "redis", f"keyspace hits={r.randint(10000, 90000)} misses={r.randint(100, 2000)}")
        else:
            log.emit("INFO", "api-gateway", f"GET /api/v1/me -> 200 ({r.randint(8, 90)}ms) req={log.req()}")

        if 120 <= i < 150 and r.random() < 0.6:
            log.emit("ERROR", "auth", f"failed to store session {log.uid()}: READONLY You can't write against a read only replica (redis 10.0.2.7:6379)")
        if i == 150:
            log.emit("INFO", "redis", "sentinel: failover complete, new master 10.0.2.8:6379")
        if 200 <= i < 300 and r.random() < 0.25:
            log.emit("ERROR", "notifications", f"SMTP send failed for message {log.uid()}: 421 4.7.0 Try again later (smtp relay 198.51.100.25:587), retry {r.randint(1, 5)}/5")
        if i % 97 == 0 and i:
            log.emit("WARNING", "auth", "TLS certificate for auth.internal.example expires in 6 days (2026-11-06)")
    log.write("auth_service.log")


def batch_worker() -> None:
    """Nightly batch box: disk filling up, exports failing on ENOSPC, flaky uploads."""
    log = Log(seed=42, start="2026-10-31 02:00:00")
    r = log.rng
    disk = 78
    for i in range(300):
        log.tick(2, 15)
        roll = r.random()
        job = f"export-{r.randint(1000, 9999)}"
        if roll < 0.35:
            log.emit("INFO", "scheduler", f"dispatched job {job} to worker-{r.randint(1, 4)}")
        elif roll < 0.60:
            log.emit("INFO", "worker", f"job {job} finished in {r.randint(3, 240)}s ({r.randint(1, 900)} MB written)")
        elif roll < 0.68:
            log.emit("WARNING", "storage", f"upload of {job}.parquet to bucket reports-archive slow, retrying (attempt {r.randint(2, 4)}/5)")
        elif roll < 0.74:
            log.emit("DEBUG", "worker", f"heartbeat ok load={r.uniform(0.2, 3.9):.2f}")
        else:
            log.emit("INFO", "reporting", f"rendered dashboard snapshot {log.uid()}")

        if i % 25 == 0:
            disk = min(99, disk + r.randint(1, 3))
            level = "ERROR" if disk >= 95 else "WARNING"
            log.emit(level, "worker", f"disk usage on /var/lib/batch at {disk}% ({disk * 5} GB of 500 GB)")
        if disk >= 93 and r.random() < 0.2:
            log.emit("ERROR", "worker", f"job {job} failed: OSError [Errno 28] No space left on device: '/var/lib/batch/tmp/{job}.part'")
        if i in (60, 190):
            log.emit("ERROR", "reporting", "unhandled exception while rendering weekly summary")
            log.raw("Traceback (most recent call last):")
            log.raw('  File "/srv/reporting/render.py", line 211, in render_weekly')
            log.raw("    totals = sum(row.value for row in rows)")
            log.raw("TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'")
    log.write("batch_worker.log")


# --- Eval scenarios: a known root cause that is outnumbered by its symptoms. ---
# The answers live in evals/answers/<name>.json.

EVAL_DIR = Path(__file__).resolve().parent.parent / "evals" / "logs"


def eval_dns_outage() -> None:
    """CoreDNS loses its upstreams; every service sees generic timeouts and 503s."""
    log = Log(seed=101, start="2026-10-31 14:00:00")
    r = log.rng
    services = ["api-gateway", "orders", "inventory", "search"]
    log.noise(120, services)
    log.emit("WARNING", "coredns", "plugin/forward: upstream 10.0.0.2:53 slow to respond (2100ms)")
    for i in range(110):
        log.tick(0.4, 3)
        if i % 18 == 0:
            log.emit("ERROR", "coredns", "plugin/forward: no healthy upstreams for zone svc.cluster.local.")
        roll = r.random()
        if roll < 0.40:
            log.emit("ERROR", "api-gateway", f"GET /api/v1/{r.choice(['items', 'search', 'orders'])} -> 503 upstream connect error ({r.randint(3000, 3100)}ms) req={log.req()}")
        elif roll < 0.65:
            log.emit("ERROR", "orders", f"call to inventory timed out after 3000ms order=ord_{r.randint(10000, 99999)}")
        elif roll < 0.85:
            log.emit("ERROR", "search", f"index refresh failed: request to elastic timed out after {r.randint(5000, 5100)}ms")
        else:
            log.emit("INFO", r.choice(services), f"GET /api/v1/me -> 200 ({r.randint(8, 60)}ms) req={log.req()}")
    log.noise(30, services)
    log.write("dns_outage.log", EVAL_DIR)


def eval_expired_internal_cert() -> None:
    """cert-manager fails to renew the internal mTLS cert; then every internal call fails TLS."""
    log = Log(seed=202, start="2026-10-31 23:30:00")
    r = log.rng
    services = ["api-gateway", "checkout", "accounts"]
    log.noise(90, services)
    log.emit("WARNING", "cert-manager", "certificate internal-mtls expires in 25m, renewal pending")
    for _ in range(3):
        log.tick(30, 90)
        log.emit("ERROR", "cert-manager", "failed to renew certificate internal-mtls: ACME order failed: 429 rateLimited, retry in 3600s")
        log.noise(8, services)
    for _ in range(120):
        log.tick(0.3, 2.5)
        roll = r.random()
        if roll < 0.45:
            log.emit("ERROR", "api-gateway", f"POST /api/v1/{r.choice(['checkout', 'login', 'cart'])} -> 502 bad gateway req={log.req()}")
        elif roll < 0.75:
            log.emit("ERROR", "checkout", f"rpc to accounts.internal:8443 failed: tls handshake error, connection reset (attempt {r.randint(1, 3)}/3)")
        elif roll < 0.92:
            log.emit("ERROR", "accounts", f"rejected client connection from {log.client_ip()}: remote error: tls: bad certificate")
        else:
            log.emit("INFO", "api-gateway", f"GET /healthz -> 200 ({r.randint(1, 5)}ms)")
    log.write("expired_internal_cert.log", EVAL_DIR)


def eval_redis_maxmemory() -> None:
    """Redis hits maxmemory with noeviction; session writes fail and users get logged out."""
    log = Log(seed=303, start="2026-10-31 09:00:00")
    r = log.rng
    services = ["api-gateway", "auth", "cart"]
    log.noise(100, services)
    for used in (88, 93, 97):
        log.tick(20, 60)
        log.emit("WARNING", "redis", f"used_memory at {used}% of maxmemory (policy=noeviction)")
    for i in range(115):
        log.tick(0.3, 2.5)
        if i % 20 == 0:
            log.emit("ERROR", "redis", "OOM command not allowed when used memory > 'maxmemory'")
        roll = r.random()
        if roll < 0.40:
            log.emit("ERROR", "api-gateway", f"GET /api/v1/{r.choice(['cart', 'me', 'orders'])} -> 401 unauthorized req={log.req()}")
        elif roll < 0.70:
            log.emit("ERROR", "auth", f"failed to persist session {log.uid()}: write failed, returning 500")
        elif roll < 0.88:
            log.emit("ERROR", "cart", f"could not save cart for session {log.uid()}, request aborted")
        else:
            log.emit("INFO", "auth", f"login succeeded user={log.uid()} method=password")
    log.write("redis_maxmemory.log", EVAL_DIR)


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    payments_incident()
    auth_service()
    batch_worker()
    eval_dns_outage()
    eval_expired_internal_cert()
    eval_redis_maxmemory()
