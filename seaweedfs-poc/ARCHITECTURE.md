# SeaweedFS POC — architecture and topology

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

What makes this possible: `weed s3` does **not** proxy object data through the filer —
it resolves chunk locations via the master and streams straight from the volume servers.
So the gateway needs master + volume + filer access, and the client needs none of it.

> **Why the management plane publishes nothing.** It first bound the admin UI to
> `127.0.0.1:12535`, which looks safe. Validation caught that it was not: in Docker ≥ 28
> a published port installs a `FORWARD` rule `iif != swfs-mgmt, oif swfs-mgmt,
> daddr <container-ip>, dport 12535 → ACCEPT`, matching traffic from **any** other
> Docker bridge regardless of the host bind address — so the client network could reach
> the admin UI on its container IP. Marking `mgmt` internal removes the hole at the
> source instead of patching around it.

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
