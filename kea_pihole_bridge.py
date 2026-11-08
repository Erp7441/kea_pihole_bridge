#!/usr/bin/env python3
"""Conservatively synchronize Kea DHCPv4 leases to Pi-hole v6 DNS hosts.

Standard library only. Dry-run by default. State tracks *only* records created
by this process. Configure each Pi-hole independently; do not share state files.
"""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def request(url, method="GET", data=None, headers=None):
    payload = None if data is None else json.dumps(data).encode()
    h = dict(headers or {})
    if payload is not None:
        h["Content-Type"] = "application/json"
    req = Request(url, data=payload, headers=h, method=method)
    for attempt in range(3):
        try:
            with urlopen(req, timeout=6) as response:
                body = response.read()
            return json.loads(body) if body else {}
        except HTTPError as exc:
            # Authentication/configuration failures are not transient.
            if exc.code < 500 or attempt == 2:
                raise
            reason = f"HTTP {exc.code}"
        except (OSError, ConnectionError) as exc:
            if attempt == 2:
                raise
            reason = str(exc)
        delay = 0.5 * (2 ** attempt)
        print(f"WARN: {method} {url.split('/api/')[0]} transient failure "
              f"({reason}); retrying in {delay:.1f}s", file=sys.stderr)
        time.sleep(delay)


def kea_leases(endpoints, username, password):
    credential = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": "Basic " + credential}
    errors = []
    for endpoint in endpoints:
        try:
            leases = []
            cursor = "start"
            seen = set()
            for _ in range(1000):
                data = request(endpoint.rstrip("/") + "/", "POST", {
                    "command": "lease4-get-page", "service": ["dhcp4"],
                    "arguments": {"from": cursor, "limit": 100}
                }, headers)
                response = data[0]
                if response.get("result") == 3:
                    return leases, endpoint
                if response.get("result") != 0:
                    raise ValueError(f"Kea returned {response.get('result')}: {response.get('text')}")
                page = response["arguments"]["leases"]
                if not page:
                    return leases, endpoint
                leases.extend(page)
                last = page[-1]["ip-address"]
                if last in seen:
                    raise ValueError("Kea pagination loop")
                seen.add(last)
                cursor = last
                if len(page) < 100:
                    return leases, endpoint
            raise ValueError("Kea pagination exceeded safety limit")
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("No complete Kea snapshot: " + "; ".join(errors))


def desired_records(leases, reserved):
    now = time.time()
    hostname_to_ips = {}
    ip_to_hostnames = {}
    for lease in leases:
        if lease.get("state") != 0:
            continue
        if float(lease.get("cltt", 0)) + float(lease.get("valid-lft", 0)) <= now:
            continue
        name = str(lease.get("hostname") or "").rstrip(".").lower()
        if not name.endswith(".lan") or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.lan", name):
            continue
        ip = str(ipaddress.IPv4Address(lease["ip-address"]))
        hostname_to_ips.setdefault(name, set()).add(ip)
