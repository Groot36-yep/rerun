"""Passwords, login sessions, and the home-network-only rule."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque

from .store import DATA_DIR, atomic_write_json, read_json

SESSION_DAYS = 30

# Loopback, home networks, link-local, and Tailscale's 100.64.0.0/10.
HOME_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
        "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10",
    )
]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk)
        test = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(test, expected)
    except (ValueError, TypeError):
        return False


def parse_networks(values) -> list:
    nets = []
    for v in values or []:
        try:
            nets.append(ipaddress.ip_network(str(v).strip(), strict=False))
        except ValueError:
            continue
    return nets


def ip_allowed(host: str, extra=()) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in [*HOME_NETWORKS, *extra])


class Sessions:
    """Login tokens. Only a hash of each token is written to disk."""

    def __init__(self):
        self.path = DATA_DIR / "sessions.json"
        self.items: dict[str, float] = read_json(self.path, {})

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self.items[self._key(token)] = time.time() + SESSION_DAYS * 86400
        self._save()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        exp = self.items.get(self._key(token))
        return bool(exp and exp > time.time())

    def revoke(self, token: str | None) -> None:
        if token and self.items.pop(self._key(token), None):
            self._save()

    def revoke_all(self) -> None:
        self.items = {}
        self._save()

    def _save(self) -> None:
        now = time.time()
        self.items = {k: v for k, v in self.items.items() if v > now}
        atomic_write_json(self.path, self.items, private=True)


class LoginLimiter:
    """At most 5 wrong passwords per address every 5 minutes."""

    def __init__(self, attempts: int = 5, window: int = 300):
        self.attempts, self.window = attempts, window
        self.failures: dict[str, deque] = defaultdict(deque)

    def wait_seconds(self, ip: str) -> int:
        q = self.failures[ip]
        now = time.time()
        while q and q[0] < now - self.window:
            q.popleft()
        return int(q[0] + self.window - now) + 1 if len(q) >= self.attempts else 0

    def fail(self, ip: str) -> None:
        self.failures[ip].append(time.time())

    def clear(self, ip: str) -> None:
        self.failures.pop(ip, None)
