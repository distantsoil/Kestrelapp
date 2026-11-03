"""Issue #166 M1: Connection spot check.

Samples the network connections of Ollama processes during a probe window and
classifies each remote address. Read-only: nothing is sent, nothing is changed.

Honest limits (stated in the UI):
- Sampling can miss very short connections.
- DNS lookups are not visible.
- Only Ollama's processes are watched, not the rest of the computer.
- macOS may restrict seeing other processes' connections.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field


@dataclass
class ConnectionSample:
    """A single observed remote connection from an Ollama process."""

    pid: int
    proc_name: str
    remote_addr: str
    remote_port: int
    classification: str  # "loopback" | "private" | "public" | "ollama_service"


@dataclass
class EgressResult:
    """The result of a connection spot check."""

    samples: list[ConnectionSample] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None

    @property
    def clean(self) -> bool:
        return not self.samples and self.error is None

    @property
    def public_connections(self) -> list[ConnectionSample]:
        return [s for s in self.samples if s.classification == "public"]

    @property
    def private_connections(self) -> list[ConnectionSample]:
        return [s for s in self.samples if s.classification == "private"]

    @property
    def ollama_service_connections(self) -> list[ConnectionSample]:
        return [s for s in self.samples if s.classification == "ollama_service"]


# Known Ollama service hostnames (update, model download).
# These are resolved once and matched by IP to avoid DNS in the hot path.
_OLLAMA_SERVICE_HOSTS = frozenset({
    "ollama.com",
    "registry.ollama.ai",
    "ollama.ai",
})

# Resolve to IP sets at import time (best effort; failures are fine).
_OLLAMA_SERVICE_IPS: frozenset[str] = frozenset()


def _resolve_ollama_service_ips() -> None:
    """Resolve known Ollama service hostnames to IP addresses."""
    global _OLLAMA_SERVICE_IPS
    ips: set[str] = set()
    for host in _OLLAMA_SERVICE_HOSTS:
        try:
            for addr in socket.getaddrinfo(host, None, socket.AF_INET):
                ips.add(addr[4][0])
            for addr in socket.getaddrinfo(host, None, socket.AF_INET6):
                # IPv6: take the address part.
                ips.add(addr[4][0].replace("[", "").replace("]", ""))
        except (socket.gaierror, OSError):
            pass
    _OLLAMA_SERVICE_IPS = frozenset(ips)


_resolve_ollama_service_ips()


def _classify_address(addr: str, port: int) -> str:
    """Classify a remote address: loopback, private, public, or ollama_service."""
    # Check known Ollama service IPs first.
    if addr in _OLLAMA_SERVICE_IPS:
        return "ollama_service"

    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "public"  # Unparseable; treat as public (safe default).

    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    return "public"


def _find_ollama_pids() -> list[tuple[int, str]]:
    """Find PIDs of Ollama processes (server, model runners, tray app)."""
    import psutil

    pids: list[tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "ollama" in name:
                pids.append((proc.info["pid"], proc.info.get("name") or "ollama"))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return pids


def sample_connections(duration_s: float = 3.0, interval_s: float = 0.1) -> EgressResult:
    """Sample network connections of Ollama processes for the given duration.

    Returns an EgressResult with the classified connections observed.
    If no Ollama processes are found, returns a clean result with a note.
    """
    import psutil

    pids = _find_ollama_pids()
    if not pids:
        return EgressResult(
            duration_s=duration_s,
            error="No Ollama processes found. Is Ollama running?",
        )

    seen: set[tuple[int, str, int]] = set()  # (pid, addr, port) dedup
    samples: list[ConnectionSample] = []
    stop = time.monotonic() + duration_s

    while time.monotonic() < stop:
        for pid, proc_name in pids:
            try:
                proc = psutil.Process(pid)
                conns = proc.connections(kind="inet")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

            for conn in conns:
                if conn.raddr is None:
                    continue
                addr, port = conn.raddr
                key = (pid, addr, port)
                if key in seen:
                    continue
                seen.add(key)

                # Skip loopback connections (they're fine and expected).
                classification = _classify_address(addr, port)
                if classification == "loopback":
                    continue

                samples.append(ConnectionSample(
                    pid=pid,
                    proc_name=proc_name,
                    remote_addr=addr,
                    remote_port=port,
                    classification=classification,
                ))
        time.sleep(interval_s)

    return EgressResult(samples=samples, duration_s=duration_s)


def format_egrade_result(result: EgressResult) -> str:
    """Format the egress result for display in the health check UI."""
    if result.error:
        return f"Could not check: {result.error}"

    if result.clean:
        return (
            "No outbound connections seen from Ollama during the test. "
            "This is a spot check. It can miss very short connections "
            "and it cannot see name lookups."
        )

    parts: list[str] = []
    if result.ollama_service_connections:
        hosts = ", ".join(
            f"{s.remote_addr}:{s.remote_port}" for s in result.ollama_service_connections
        )
        parts.append(f"Ollama's own services: {hosts}")
    if result.private_connections:
        hosts = ", ".join(
            f"{s.remote_addr}:{s.remote_port} ({s.proc_name})"
            for s in result.private_connections
        )
        parts.append(f"Ollama connected to another computer on your network: {hosts}")
    if result.public_connections:
        hosts = ", ".join(
            f"{s.remote_addr}:{s.remote_port} ({s.proc_name})"
            for s in result.public_connections
        )
        parts.append(f"Ollama connected to a public address: {hosts}")

    parts.append(
        "This is a spot check. It can miss very short connections "
        "and it cannot see name lookups."
    )
    return " ".join(parts)
