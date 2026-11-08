# Kea - Pi-hole DNS bridge

Synchronize active **Kea DHCPv4 leases** into **Pi-hole v6 Local DNS records** without running BIND, Kea DHCP-DDNS, or Pi-hole's built-in DHCP server.

`kea_pihole_bridge.py` is a standalone Python 3 script using only the standard library. It reads a complete, paginated lease snapshot from a Kea Control Agent, converts valid leases into local DNS entries, then updates the local Pi-hole v6 API. It is **dry-run by default** and can be scheduled with a systemd timer.

> [!IMPORTANT]
> This is **polling-based DNS synchronization**, not RFC 2136 dynamic DNS or a strongly consistent DNS database. Each Pi-hole answers DNS independently using its last synchronized local records. A typical one-minute timer means updates can lag by roughly a minute (plus any outage or retry time). This bridge does **not** implement Kea DHCP failover; configure Kea HA separately if desired.

## How it works

```text
Kea DHCPv4 + lease commands
    ├── Kea Control Agent A :8000 ─┐
    └── Kea Control Agent B :8000 ─┤  first complete response
                                  ▼
                       kea_pihole_bridge.py
                           (on each Pi-hole)
                                  │
                                  ▼
                       local Pi-hole v6 API
                         /api/config/dns/hosts
```

Run **one independent instance per Pi-hole**. Each bridge keeps its own ownership file and writes only to its local Pi-hole. The two bridges can query the same ordered list of Kea API endpoints, allowing automatic API-source fallback.

### What gets synchronized

- Only IPv4 leases with `state == 0` and an unexpired `cltt + valid-lft`.
- Only valid hostnames ending in `.lan` (this suffix is **hard-coded** in the current script).
- One `IP hostname` entry per eligible, nonconflicting lease.
- Duplicate hostnames across different IPs receive deterministic aliases such as `printer-170.lan` and `printer-180.lan` rather than arbitrarily choosing an IP.
- If an IP has **multiple different active hostnames**, those addresses are skipped.
- Existing static Pi-hole DNS entries are protected by their IPs and names.

It does **not** synchronize CNAMEs, Pi-hole blocklists, settings, IPv6 leases, or Kea DHCP reservations that have no active lease. Static DNS entries must be provisioned separately.

## Requirements

- Python **3.8+** on each Pi-hole host; no `pip` packages required.
- **Pi-hole v6**, with its API reachable from the script host (default: `http://127.0.0.1`).
- Kea DHCPv4 with a **Control Agent** reachable via HTTP(S), and the **`lease_cmds` hook** loaded in Kea DHCPv4 so `lease4-get-page` works.
- A Kea API account able to run `lease4-get-page` and a Pi-hole API password with permission to modify local DNS host entries.
- Network/firewall access from each bridge host to **TCP 8000** on the configured Kea API endpoints (or your custom port).
- systemd if using the example timer.

**Security:** The examples use HTTP on a trusted LAN. Kea Control Agent Basic authentication sends credentials in an encoding that does not encrypt them. For untrusted networks use TLS via a properly configured endpoint or a protected management network; never expose this API publicly.

## 1. Configure Kea

The bridge requires the DHCPv4 lease commands hook. Inspect `/etc/kea/kea-dhcp4.conf` and ensure the `Dhcp4` configuration contains an appropriate entry in `hooks-libraries` (alongside your existing HA hook if applicable):

```json
"hooks-libraries": [
  {
    "library": "/usr/lib/x86_64-linux-gnu/kea/hooks/libdhcp_lease_cmds.so"
  }
]
```

The **library path varies** by distribution and architecture. Locate it with your package manager, for example:

```bash
find /usr/lib /usr/lib64 -name 'libdhcp_lease_cmds.so' 2>/dev/null
```

Do not replace an existing `hooks-libraries` array: add the lease commands hook while preserving other required hooks, particularly a configured HA hook. Validate the Kea configuration with the matching distribution's Kea test command and apply it using your established safe restart/reconfiguration process.

Configure and secure the Kea Control Agent. The examples use documentation-only addresses `192.0.2.10` and `192.0.2.11` (RFC 5737), using HTTP Basic authentication with username `kea-api`. Replace these values with your own. The exact Control Agent configuration and service name depend on the Kea distribution.

Verify the endpoint **from each bridge host** (not just from the Kea server):

```bash
curl --fail --silent --show-error \
  --user "kea-api:$(cat /etc/kea-pihole-bridge/kea-password)" \
  --header 'Content-Type: application/json' \
  --data '{"command":"lease4-get-page","service":["dhcp4"],"arguments":{"from":"start","limit":100}}' \
  http://192.0.2.10:8000/
```

You should receive a JSON array with `result: 0` and `arguments.leases`. An empty but successful lease list is valid. A connection timeout often means a firewall rule is blocking the bridge host; check the Kea server's persistent firewall rules, not just the listener.

> [!WARNING]
> An API response from a Kea **HA standby** is not necessarily a guarantee that its lease view is complete or fresh during every failure mode. The script accepts the first endpoint returning a complete pagination sequence; it does not interrogate the HA role or reconcile snapshots across peers. Validate Kea HA lease synchronization and test failover independently.

## 2. Install the script on each Pi-hole host

Copy `kea_pihole_bridge.py` onto **each** Pi-hole server:

```bash
sudo install -o root -g root -m 755 \
  kea_pihole_bridge.py /usr/local/sbin/kea_pihole_bridge.py

sudo install -d -o root -g root -m 700 \
  /etc/kea-pihole-bridge /var/lib/kea-pihole-bridge
```

Create two credential files. Replace the placeholder values with the actual Kea API password and the **local Pi-hole's** API password. If your Pi-holes have different passwords, install the corresponding password on each respective host.

```bash
sudo install -o root -g root -m 600 /dev/null \
  /etc/kea-pihole-bridge/kea-password
sudo install -o root -g root -m 600 /dev/null \
  /etc/kea-pihole-bridge/pihole-password

sudoedit /etc/kea-pihole-bridge/kea-password
sudoedit /etc/kea-pihole-bridge/pihole-password
```

Put **only the password** in each file, with no labels or quotes. Do not commit these files to Git.

For Pi-hole running in Docker/Podman, run the bridge **on the container host**, as root in these examples, and point `PIHOLE_URL` at an API address reachable from that host. You do not need to install Python inside the Pi-hole container.

## 3. Configure the bridge

**Generic example, not a record of any particular deployment:** create `/etc/kea-pihole-bridge/bridge.env` on **each** Pi-hole host *if you choose to use a systemd environment file*. Existing deployments may set these variables directly in their systemd service or another way; preserve your actual working service layout.

```ini
KEA_ENDPOINTS=http://192.0.2.10:8000,http://192.0.2.11:8000
KEA_USER=kea-api
KEA_PASSWORD_FILE=/etc/kea-pihole-bridge/kea-password
PIHOLE_URL=http://127.0.0.1
PIHOLE_PASSWORD_FILE=/etc/kea-pihole-bridge/pihole-password
```

```bash
sudo chmod 600 /etc/kea-pihole-bridge/bridge.env
```

**Always set your own endpoint list explicitly**. The publishable version of the script intentionally requires `KEA_ENDPOINTS` and has no deployment-specific address defaults.

| Setting | Default | Meaning |
| --- | --- | --- |
| `KEA_ENDPOINTS` | **Required** | Comma-separated Kea Control Agent URLs; tried in order. |
| `KEA_USER` | `kea-api` | Kea HTTP Basic authentication username. |
| `KEA_PASSWORD_FILE` | **Required** | File containing the Kea API password. |
| `PIHOLE_URL` | `http://127.0.0.1` | Local Pi-hole API base URL. |
| `PIHOLE_PASSWORD_FILE` | **Required** | File containing the local Pi-hole API password. |
| `--state PATH` | `/var/lib/kea-pihole-bridge/owned.json` | Locally owned DNS record manifest. **Never share across servers.** |
| `--apply` | Off | Write DNS changes; without it, only print a dry run. |

> [!NOTE]
> systemd `EnvironmentFile=` uses environment-file syntax; use simple unquoted values as shown. Protect the file even though the passwords are stored separately.

## 4. Dry run, then first synchronization

Run on **each** Pi-hole host:

```bash
sudo bash -c 'set -a; . /etc/kea-pihole-bridge/bridge.env; set +a; \
  python3 /usr/local/sbin/kea_pihole_bridge.py'
```

Example dry-run summary:

```text
Kea source=http://192.0.2.10:8000 leases=22 desired=19 static=12 owned=0 add=19 remove=0
ALIASED duplicate hostnames: printer.lan
ADD 192.0.2.25 workstation.lan
...
DRY RUN: no changes made
```

Review the additions carefully. In particular, check for conflicting static Pi-hole names, stale leases and unexpected aliases. Then run:

```bash
sudo bash -c 'set -a; . /etc/kea-pihole-bridge/bridge.env; set +a; \
  python3 /usr/local/sbin/kea_pihole_bridge.py --apply'
```

Expected final line: `Applied and verified`.

Re-run the dry run; for a converged state it should show `add=0 remove=0`.

DNS check (change the hostname/IP to match a live Kea lease):

```bash
dig @127.0.0.1 workstation.lan A +short
dig @127.0.0.1 -x 192.0.2.25 +short
```

The script writes Pi-hole's `dns.hosts` entries; Pi-hole is responsible for serving their associated local forward and reverse responses. Existing records unrelated to the bridge should stay intact.

## 5. Run automatically with systemd

On **each** Pi-hole host, create `/etc/systemd/system/kea-pihole-bridge.service`:

```ini
[Unit]
Description=Kea DHCP to Pi-hole DNS synchronization
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
EnvironmentFile=/etc/kea-pihole-bridge/bridge.env
ExecStart=/usr/bin/python3 /usr/local/sbin/kea_pihole_bridge.py --apply
```

Create `/etc/systemd/system/kea-pihole-bridge.timer`:

```ini
[Unit]
Description=Synchronize Kea leases to Pi-hole DNS every minute

[Timer]
OnBootSec=60s
OnUnitInactiveSec=60s
AccuracySec=5s
Unit=kea-pihole-bridge.service

[Install]
WantedBy=timers.target
```

Enable the **timer** (the `oneshot` service need not be enabled):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kea-pihole-bridge.timer
sudo systemctl start kea-pihole-bridge.service

systemctl list-timers --all | grep kea-pihole
systemctl status kea-pihole-bridge.service --no-pager
journalctl -u kea-pihole-bridge.service -n 30 --no-pager
```

An `inactive (dead)` service with `status=0/SUCCESS` is **normal** for a successful `Type=oneshot` run. Check the timer's `active` status for scheduling.

With `OnUnitInactiveSec=60s`, the next run is scheduled roughly one minute after the previous run finished, not at a fixed wall-clock minute.

## 6. Validate source failover

Test fallback **without stopping a production DHCP server**:

```bash
sudo bash -c 'set -a; . /etc/kea-pihole-bridge/bridge.env; set +a; \
  KEA_ENDPOINTS=http://192.0.2.254:8000,http://192.0.2.11:8000 \
  python3 /usr/local/sbin/kea_pihole_bridge.py'
```

Use a deliberately unavailable test IP only if it is confirmed unused in your network. The successful summary should show `Kea source=http://192.0.2.11:8000` (substitute your working endpoint) and `DRY RUN: no changes made` when DNS is up to date.

For a full HA test, independently verify that the standby Kea DHCP server takes over lease allocation; test DNS from clients against the surviving Pi-hole. This script does **not** detect DHCP HA roles or decide which Kea peer should serve DHCP.

## 7. Static records, CNAMEs and Pi-hole synchronization

Each bridge maintains two categories of Pi-hole `dns.hosts` entries:

1. **Owned:** entries recorded in its local `/var/lib/kea-pihole-bridge/owned.json`; eligible for bridge removal once their lease disappears.
2. **Static:** all other entries present on that Pi-hole; preserved and used to reserve both hostnames and IPs against automatic insertion.

The bridge never imports CNAME records. Manage static entries and CNAMEs through a separate workflow. If you use **Nebula Sync**, be careful: synchronizing **all** `dns.hosts` while two independent bridge instances modify those entries can create competing writers. Restrict sync to carefully chosen settings, and test that Pi-hole accepts them. A DNS configuration HTTP 400 is a Pi-hole API rejection; do not assume which field caused it without inspecting the error.

**Do not share `owned.json`** between Pi-hole instances, or blindly copy one instance's whole `dns.hosts` configuration over another. One-way static/CNAME management requires an additional mechanism and is outside this bridge's scope.

## Safety behavior and limitations

- **Dry-run by default:** no DNS writes without `--apply`.
- **Complete-page requirement:** an endpoint must return a successful complete Kea pagination sequence before the script uses it; otherwise the next endpoint is tried.
- **No stale/inactive leases:** excludes expired and `state != 0` entries.
- **Ownership-limited deletion:** only records the bridge previously recorded as owned are candidates for removal.
- **Static protection:** existing non-owned records reserve their IP and name.
- **Mass-deletion guard:** if a run would remove more than `max(2, 25% of owned records)`, it aborts without applying its changes. Investigate lease-source freshness rather than disabling the guard.
- **Retries:** transient network/server failures are retried up to three times per HTTP request. Authentication and other client errors are not retried.
- **Partial-apply recovery:** ownership is saved after each mutation. It uses an atomic file replacement but is **not a transaction** across multiple Pi-hole API changes. Inspect logs and rerun after resolving failures.
- **No automatic restoration of expired leases:** expired leases disappear from desired DNS and may be removed on a later successful run.
- **No multi-writer coordination:** concurrent bridge processes targeting the *same* Pi-hole/state file are unsupported. The included systemd oneshot timer avoids overlapping starts of the same service unit.
- **No CNAME or IPv6 support:** only DHCPv4 `.lan` A/PTR-style local hosts are handled.

The bridge protects names/IPs that are already static, but it cannot know whether a manually inserted DNS entry is intentional unless it is outside its own ownership manifest. Back up both Pi-hole settings and `owned.json` before large migrations.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `No complete Kea snapshot: ... timed out` | Confirm Kea agent listening, correct endpoint, route and persistent firewall allowing the Pi-hole host to TCP 8000. |
| Kea returns unknown command or error | Confirm `libdhcp_lease_cmds.so` hook is loaded and Kea Control Agent can forward commands to DHCPv4. |
| HTTP 401/403 | Verify Kea Basic credentials or local Pi-hole API password and permissions. |
| Pi-hole `/api/config` HTTP 400 | Inspect the API rejection and check for read-only or invalid configuration. Do not turn on broad synchronization indiscriminately. |
| `Safety stop: ... managed records would be removed` | Inspect source lease count, HA replication health and lease expiry; do not force bulk deletions. |
| `ALIASED duplicate hostnames` | Two active leases have one hostname; inspect the actual devices and use stable unique client names or Kea reservations where appropriate. |
| `SKIPPED ambiguous addresses` | Multiple active hostnames claim the same address. Inspect leases. |
| `add=0 remove=0` | Normal converged state. |
| `inactive (dead)` after systemd run | Normal for a successful `Type=oneshot` service; check exit status and timer. |
| Missing CNAME or static name on second Pi-hole | The bridge does not sync these. Configure a separate static/CNAME synchronization mechanism. |

For diagnosis, run a dry run and inspect the service logs:

```bash
sudo systemctl start kea-pihole-bridge.service
journalctl -u kea-pihole-bridge.service -n 50 --no-pager
```

When testing credentials, avoid commands that print password files or expose secrets in shell arguments/logs.

## Uninstall / rollback

Stop the scheduling first:

```bash
sudo systemctl disable --now kea-pihole-bridge.timer
```

The bridge has no automatic "uninstall all managed DNS entries" mode. **Do not delete `owned.json` to uninstall**: doing so makes its previously owned DNS records appear static and they will no longer be recognized as bridge-owned.

If you intend to remove the bridge-generated entries, inspect and back up `owned.json`, then remove only those exact records through the Pi-hole API after confirming they are no longer needed. Keep existing static hosts intact. Finally, you may remove the systemd units, script, credentials, and state through your normal configuration-management process.
