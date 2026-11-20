# SeaweedFS POC — architecture and topology

## SeaweedFS S3 POC — distributed, three network planes, single host

A multi-node SeaweedFS cluster on one machine, split across three Docker networks so
that client, management and internal cluster traffic cannot reach each other.

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

## Cluster layout (one host, 16 containers: 14 infrastructure + 2 probes)

| Tier | Instances | Why this size |
|---|---|---|
| Master `weed master` | 3, Hashicorp Raft | Smallest real quorum: survives 1 loss, and 2-of-3 loss provably stops writes. |
| Volume `weed volume` | 3 — `dc1/rack1..3`, replication `010` | `010` = 2 copies in 2 racks; 3 racks means the surviving 2 still satisfy the placement rule. |
| Filer `weed filer` | 2 | Stateless front-ends over one shared Postgres `filemeta` table. Local LevelDB is explicitly disabled — two filers with private stores would silently diverge. |
| S3 gateway `weed s3` | 2 | The protocol boundary; each knows both filers (`-filer=filer1:8888,filer2:8888`). |
| Edge LB (nginx) | 1 | One published endpoint, health-checked failover. |
| Management | `admin`, `prometheus` | The only way an operator observes the cluster. |

## Three traffic planes

| Plane | Subnet | Members | Host exposure |
|---|---|---|---|
| **edge** | `10.77.10.0/24` | `s3-lb`, `s3-gw-1/2`, test `client` | `0.0.0.0:8333` — the only published port |
| **mgmt** | `10.77.20.0/24`, internal | `admin` (`.10`), `prometheus` (`.11`), `ops` | none; SSH tunnel to the fixed IPs |
| **cluster** | `10.77.30.0/24`, internal | masters, volumes, filers, `filer-db` | none, and no egress |

Exactly two component classes are multi-homed, each a deliberate gateway: `s3-gw-{1,2}`
bridge **edge ↔ cluster**, `admin`/`prometheus` bridge **mgmt ↔ cluster**. Masters,
volume servers, filers and the database have a *single* interface, on `cluster`.

**Must cross a boundary:** S3 requests (client → gateway), metrics/admin RPC (mgmt →
cluster). **Must never:** raft, volume heartbeats, chunk traffic, filer gRPC, Postgres.
`-telemetry=false` stops the masters phoning home; volume `-publicUrl` is pinned to the
internal name so no lookup can hand a client a volume address.

## Fault tolerance (verified, not assumed)

| Failure | Result |
|---|---|
| 1 volume server | Reads and writes continue from the second replica. |
| 1 master | Re-election; reads and writes continue. |
| 2 masters | Quorum lost → **writes correctly stop**, and resume on recovery. |
| 1 filer | Gateways fail over; shared metadata means no divergence. |
| 1 S3 gateway | nginx drops it after 2 failures; clients unaffected. |
| `filer-db` | **Outage** — the one accepted SPOF, see below. |

## Known limits

1. **`filer-db` is a SPOF** — deliberate. Production: replicated Postgres (Patroni /
   managed), or Cassandra/TiKV for larger namespaces.
2. **`s3-lb` is a SPOF** — in reality this role is a VIP or an external load balancer.
3. **The host sits in all three zones.** Host-originated traffic is not forwarded, so
   `internal` does not stop it (verified: the host reaches `master0:9333` directly). No
   single-host Docker setup avoids this; on real hardware the planes are separate
   VLANs/NICs. It is also what makes the SSH-tunnel path work.
4. Intra-cluster traffic is unauthenticated and unencrypted. Production enables gRPC
   mTLS (`security.toml`) and TLS on the S3 listener, so the boundary does not rest on
   reachability alone.

## How the separation is validated

`scripts/validate.sh boundary` classifies *how* each blocked connection fails:
`TIMEOUT`/`UNREACH`/`DNSFAIL` pass, but **`REFUSED` is scored as a failure** — an RST
would prove the packet reached the other network. It dials storage **by raw IP**, not
just by name, since a failed DNS lookup proves little on its own. It asserts that only
one port is published anywhere. And it runs the identical probe from a container *on*
the cluster network as a positive control — without that, every negative could just mean
the prober is broken. Identity gets the same treatment: anonymous GET, a read-only key
writing, and cross-bucket access must all return 403.

# SeaweedFS POC — Operation manual

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
