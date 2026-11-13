#!/usr/bin/env python3
"""TCP/DNS reachability prober used to prove where the network boundaries are.

The important part is that this does NOT just assert "connection failed". It
classifies *how* the connection failed, because the distinction is what makes
the test meaningful:

  OPEN     TCP handshake completed                 -> host reachable, port open
  REFUSED  RST came back                           -> host reachable at L3 (!)
  TIMEOUT  nothing came back before the deadline   -> silently dropped
  UNREACH  ENETUNREACH / EHOSTUNREACH              -> no route
  DNSFAIL  the name does not resolve here

A target that is supposed to be unreachable must land in TIMEOUT / UNREACH /
DNSFAIL. REFUSED counts as a FAILURE: it means packets did reach the other
network and something answered, which is exactly the leak we are testing for.

Targets may be given as `label=host:port` or `host:port`. Passing a raw IP is
what makes the probe honest - a name that does not resolve proves very little
on its own, since Docker's embedded DNS is per-network anyway.
"""

from __future__ import annotations

import argparse
import errno
import socket
import sys

OPEN = "OPEN"
REFUSED = "REFUSED"
TIMEOUT = "TIMEOUT"
UNREACH = "UNREACH"
DNSFAIL = "DNSFAIL"

BLOCKED_RESULTS = {TIMEOUT, UNREACH, DNSFAIL}

COLOURS = {OPEN: "\033[32m", REFUSED: "\033[31m", TIMEOUT: "\033[33m",
           UNREACH: "\033[33m", DNSFAIL: "\033[33m"}
RESET = "\033[0m"


def probe(host: str, port: int, timeout: float) -> tuple[str, str]:
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return DNSFAIL, str(exc)

    addr = infos[0][4]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(addr)
        return OPEN, f"connected to {addr[0]}:{addr[1]}"
    except socket.timeout:
        return TIMEOUT, f"no response from {addr[0]}:{addr[1]} within {timeout}s"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return REFUSED, f"RST from {addr[0]}:{addr[1]}"
        if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return UNREACH, f"no route to {addr[0]}:{addr[1]}"
        return UNREACH, f"{exc.__class__.__name__}: {exc}"
    finally:
        sock.close()


def parse_target(raw: str) -> tuple[str, str, int]:
    label, _, endpoint = raw.rpartition("=")
    endpoint = endpoint or raw
    host, _, port = endpoint.rpartition(":")
    if not host:
        raise ValueError(f"target must be host:port, got {raw!r}")
    return (label or endpoint), host, int(port)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", help="[label=]host:port")
    parser.add_argument("--expect", choices=["blocked", "reachable"], required=True)
    parser.add_argument("--timeout", type=float, default=4.0)
    args = parser.parse_args()

    failures = 0
    for raw in args.targets:
        label, host, port = parse_target(raw)
        result, detail = probe(host, port, args.timeout)

        if args.expect == "reachable":
            ok = result == OPEN
        else:
            ok = result in BLOCKED_RESULTS

        verdict = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1

        colour = COLOURS.get(result, "")
        print(
            f"  [{verdict}] {label:<34} {colour}{result:<8}{RESET} {detail}",
            flush=True,
        )
        if not ok and result == REFUSED:
            print(
                "         ^ host answered at L3 - the networks are NOT isolated",
                flush=True,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
