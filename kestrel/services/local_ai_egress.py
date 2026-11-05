"""Issue #166 M1: Connection spot check.

Looks at the network connections of Ollama processes for a few seconds and classifies each
remote address. Read-only: nothing is sent, nothing is changed. The doctor runs it when no model
request is in flight, so it is a check at rest: it does not watch an inference.

Honest limits (stated in the UI):
- It checks at rest, so traffic that only happens while a model runs is not seen.
- When the operating system will not let Kestrel list the connections, the result is
  "unverified", never clean.
- Sampling can miss very short connections.
- DNS lookups are not visible.
- Only Ollama's processes are watched, not the rest of the computer.
- macOS may restrict seeing other processes' connections.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from functools import lru_cache

from kestrel.web.errors import fixed_reason


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
    # Set when the connections could not be listed (access denied, process gone, a failed call).
    # A result with this set is never clean: not looking is not the same as seeing nothing.
    unverified: str | None = None

    @property
    def clean(self) -> bool:
        return not self.samples and self.error is None and self.unverified is None

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

_RESOLVE_TIMEOUT_S = 2.0


@lru_cache(maxsize=1)
def _ollama_service_ips() -> frozenset[str]:
    """Resolve Ollama's own service hostnames to addresses, lazily and once, with a timeout.

    Called only when a public connection was actually seen, never at import. This is a name
    lookup of ollama.com, registry.ollama.ai and ollama.ai (listed in docs/OUTBOUND-CALLS.md).
    A failed or slow lookup yields an empty set: the connection stays classified as public.
    """

    def lookup() -> set[str]:
        ips: set[str] = set()
        for host in sorted(_OLLAMA_SERVICE_HOSTS):
            try:
                for info in socket.getaddrinfo(host, None):
                    ips.add(str(info[4][0]))
            except OSError:
                continue
        return ips

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return frozenset(pool.submit(lookup).result(timeout=_RESOLVE_TIMEOUT_S))
    except (FutureTimeout, OSError):
        return frozenset()
    finally:
        pool.shutdown(wait=False)


def _classify_address(
    addr: str, port: int, service_ips: frozenset[str] = frozenset()
) -> str:
    """Classify a remote address: loopback, private, public, or ollama_service.
    Does no name lookup; pass `service_ips` to recognise Ollama's own services."""
    if addr in service_ips:
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

    If no Ollama process is found the result carries an error. If any process's connections
    cannot be listed, the result is marked unverified and is never clean.
    """
    import psutil

    try:
        pids = _find_ollama_pids()
    except (psutil.Error, OSError) as exc:
        return EgressResult(
            duration_s=duration_s,
            unverified=f"the list of running programs could not be read ({fixed_reason(exc)})",
        )
    if not pids:
        return EgressResult(
            duration_s=duration_s,
            error="No Ollama processes found. Is Ollama running?",
        )

    seen: set[tuple[int, str, int]] = set()  # (pid, addr, port) dedup
    found: list[tuple[int, str, str, int]] = []
    denied: set[int] = set()
    listed = 0
    stop = time.monotonic() + duration_s

    while True:
        for pid, proc_name in pids:
            try:
                proc = psutil.Process(pid)
                # psutil 6 renamed Process.connections to net_connections.
                lister = getattr(proc, "net_connections", None) or proc.connections
                conns = lister(kind="inet")
            except psutil.NoSuchProcess:
                continue  # it exited; nothing left to look at
            except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
                denied.add(pid)
                continue
            listed += 1
            for conn in conns:
                if conn.raddr is None or not conn.raddr:
                    continue
                addr, port = conn.raddr
                key = (pid, addr, port)
                if key in seen:
                    continue
                seen.add(key)
                found.append((pid, proc_name, addr, port))
        if time.monotonic() >= stop:
            break
        time.sleep(interval_s)

    service_ips = frozenset[str]()
    if any(_classify_address(a, p) == "public" for _, _, a, p in found):
        service_ips = _ollama_service_ips()
    samples: list[ConnectionSample] = []
    for pid, proc_name, addr, port in found:
        classification = _classify_address(addr, port, service_ips)
        if classification == "loopback":
            continue  # fine and expected
        samples.append(ConnectionSample(pid, proc_name, addr, port, classification))

    unverified = None
    if denied:
        unverified = (
            f"the operating system did not let Kestrel list the connections of {len(denied)} "
            f"of {len(pids)} Ollama process(es)"
        )
    elif listed == 0:
        unverified = "no Ollama connection list could be read"
    return EgressResult(samples=samples, duration_s=duration_s, unverified=unverified)


def format_egress_result(result: EgressResult) -> str:
    """Format the egress result for display in the health check UI."""
    if result.error:
        return f"Could not check: {result.error}"

    if result.clean:
        return (
            "No outbound connections seen from Ollama when checked at rest. "
            "This is a spot check. It does not watch a model run, it can miss very short "
            "connections and it cannot see name lookups."
        )

    parts: list[str] = []
    if result.unverified:
        parts.append(f"Unverified: {result.unverified}.")
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
        "This is a spot check made at rest. It does not watch a model run, it can miss very "
        "short connections and it cannot see name lookups."
    )
    return " ".join(parts)
