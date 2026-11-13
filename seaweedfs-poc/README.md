# SeaweedFS S3 POC — distributed, three network planes, single host

A multi-node SeaweedFS cluster on one machine, split across three Docker networks so
that client, management and internal cluster traffic cannot reach each other.

**The write-up is [ARCHITECTURE.md](./ARCHITECTURE.md)** (≤1 page). This file is the
operating manual.

```
                 host :8333                  ssh -L to 10.77.20.10 / .11
                      |                                   |
   ===== edge (10.77.10.0/24) =====   == mgmt (10.77.20.0/24, internal) ==
        s3-lb (nginx)                           admin        prometheus
        s3-gw-1   s3-gw-2                           \           /
        client (test harness)                        \         /
             \        /                               \       /
   ========== cluster (10.77.30.0/24, internal) ================
        master0  master1  master2         (raft quorum)
        volume1  volume2  volume3         (dc1 / rack1..3, replication 010)
        filer1   filer2                   (stateless, shared metadata)
        filer-db (postgres)
```

## Requirements

Ubuntu 24.04 with Docker Engine ≥ 24 and the Compose v2 plugin. Roughly 2 GB RAM and
2 GB disk for the demo workload.

```bash
sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker "$USER"   # then log out and back in
```

## Run it

```bash
cd seaweedfs-poc
docker compose up -d --build --wait     # ~2-3 min on a first run (image pulls)
docker compose ps
```

`--wait` blocks until every health check passes, so when it returns the cluster is
ready. Then:

```bash
./scripts/validate.sh            # topology + happy path + boundary + resilience
```

Individual phases:

```bash
./scripts/validate.sh topology
./scripts/validate.sh happy
./scripts/validate.sh boundary
./scripts/validate.sh resilience    # takes ~2 min, stops and restarts nodes
```

The script exits non-zero if any assertion fails.

## Using it as a client

```bash
# from the test client container (it lives on the edge network only)
docker compose exec client python3 /app/s3_workload.py full

# or from anywhere that can reach the VM
aws --endpoint-url http://<vm-ip>:8333 s3 ls s3://poc-bucket/
#   AWS_ACCESS_KEY_ID=poc-app-rw AWS_SECRET_ACCESS_KEY=poc-app-rw-secret-change-me
```

Credentials live in [config/s3-identities.json](./config/s3-identities.json) and are
mirrored into [.env](./.env) for the client container. `app-rw` and `app-ro` are scoped
to `poc-bucket`; only `svc-admin` may create buckets (`-autoCreateBucket=false`).

## Management access

The management plane publishes **nothing** — see the note in
[ARCHITECTURE.md](./ARCHITECTURE.md) on why a `127.0.0.1` publication would still have
been reachable from the client network. Reach it by tunnelling to its fixed addresses:

```bash
ssh -L 12535:10.77.20.10:12535 -L 9090:10.77.20.11:9090 user@vm
```

- Admin UI — <http://127.0.0.1:12535> (`admin` / `change_me_poc`)
- Prometheus — <http://127.0.0.1:9090>

Sitting on the VM itself, `curl http://10.77.20.10:12535/` works directly: host-origin
traffic is not forwarded, so `internal: true` does not block it.

Break-glass cluster CLI goes through the admin container, which is the one management
component with cluster access:

```bash
docker compose exec admin weed shell -master=master0:9333,master1:9333,master2:9333
# > volume.list
# > cluster.check
```

## What the validation actually proves

Beyond the happy path, `boundary` asserts:

- **exactly one port is published** in the entire deployment — in Docker ≥ 28 a
  published port is a hole in the inter-bridge firewall no matter which host address it
  is bound to, so the count is itself a security property;
- the client container **cannot resolve** `master0`, `volume1`, `filer1`, `filer-db`;
- it **cannot reach them by raw IP** either — and a `REFUSED` (RST) is scored as a
  *failure*, because that would mean the packet crossed the boundary;
- it cannot reach the management plane, and the management plane cannot reach the
  client plane;
- the **same probe from a container on the cluster network succeeds** — the positive
  control that makes the negatives meaningful;
- anonymous requests, read-only keys writing, and cross-bucket access all get 403.

And `resilience` kills one node in every tier — volume, master, filer, gateway —
re-verifying an 8 MiB object's SHA-256 after each, then takes a second master down to
show writes genuinely stop when raft quorum is lost, and resume when it returns.

All 22 assertions pass on a clean Ubuntu 24.04 / Docker 29 host.

## Next steps (not done here, on purpose)

- **Remove the two SPOFs:** replicate `filer-db` (Patroni / managed Postgres, or move
  to Cassandra/TiKV), and replace `s3-lb` with a VIP or an external load balancer.
- **Encrypt and authenticate intra-cluster traffic** via `security.toml` gRPC mTLS plus
  TLS on the S3 listener, so the boundary does not depend on reachability alone.
- **Erasure coding** (`weed scaffold -config=master` → `ec` settings) for cold data,
  which costs far less than 2× replication.
- Alerting rules on the Prometheus data, backups of the filer metadata
  (`weed filer.meta.backup`), and `weed filer.sync` for cross-site replication.

### How this changes for real multi-node / multi-DC

Carries over as-is: the component split and counts, replication `010`, the shared
metadata store, the gateway-per-boundary model, and the validation harness — the probes
are IP-based, so they work just as well against VLANs as against Docker bridges.

Changes: the three Docker bridges become three real L2/L3 segments (separate NICs or
VLANs) with a firewall between them, which also removes limitation #3 in
ARCHITECTURE.md — the host is no longer in every zone. `-dataCenter`/`-rack` stop being
labels and start describing real failure domains, so replication moves from `010` to
`200`/`210` (copies in other data centres) and masters are spread across DCs — though
at that point WAN latency on raft and on synchronous replication becomes the design
constraint, and the usual answer is one cluster per DC with `weed filer.sync` between
them rather than one cluster stretched across DCs. Gateways get scaled per DC behind a
geo-aware LB with `-dataCenter` set so reads stay local.

## Troubleshooting

**Subnet clash** (`Pool overlaps with other one on this address space`) — edit the
three `ipam.config.subnet` values in `docker-compose.yml`.

**Reset everything**, including data:

```bash
docker compose --profile probe down -v
```

**Logs for one component:**

```bash
docker compose logs -f master0 filer1 s3-gw-1
```
