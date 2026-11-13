#!/usr/bin/env bash
#
# End-to-end validation of the SeaweedFS POC.
#
#   topology    print which container sits on which network (the evidence)
#   happy       exercise the S3 API the way a tenant application would
#   boundary    prove the network separation holds, in both directions
#   resilience  kill one node of every tier and show the cluster survives
#
# Run without arguments to do all four.
#
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

COMPOSE=(docker compose)
CLIENT=swfs-client
OPS=swfs-ops
PROBE=swfs-cluster-probe

NET_EDGE=swfs-edge
NET_MGMT=swfs-mgmt
NET_CLUSTER=swfs-cluster

PASS=0
FAIL=0
SKIP=0

B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'

section() { printf '\n%s=== %s ===%s\n' "$B" "$*" "$N"; }
note()    { printf '       %s\n' "$*"; }
record() {
  if [ "$1" -eq 0 ]; then PASS=$((PASS + 1)); printf '  %sPASS%s %s\n' "$G" "$N" "$2"
  else FAIL=$((FAIL + 1)); printf '  %sFAIL%s %s\n' "$R" "$N" "$2"; fi
}
skip() { SKIP=$((SKIP + 1)); printf '  %sSKIP%s %s\n' "$Y" "$N" "$1"; }

ip_on() { # container network -> ipv4 or empty
  docker inspect -f "{{ with index .NetworkSettings.Networks \"$2\" }}{{ .IPAddress }}{{ end }}" "$1" 2>/dev/null
}

nets_of() {
  docker inspect -f '{{ range $k, $v := .NetworkSettings.Networks }}{{ $k }} {{ end }}' "$1" 2>/dev/null
}

wait_healthy() { # container [timeout_s]
  local ctr=$1 timeout=${2:-120} waited=0 state
  while [ "$waited" -lt "$timeout" ]; do
    state=$(docker inspect -f '{{ if .State.Health }}{{ .State.Health.Status }}{{ else }}{{ .State.Status }}{{ end }}' "$ctr" 2>/dev/null)
    case "$state" in
      healthy|running) return 0 ;;
    esac
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

probe_from() { # container expectation description targets...
  local ctr=$1 expect=$2 desc=$3; shift 3
  docker exec "$ctr" python3 /app/net_probe.py --expect "$expect" --timeout 4 "$@"
  record $? "$desc"
}

s3() { # run the workload script inside the client container
  docker exec -e PYTHONUNBUFFERED=1 "$CLIENT" python3 /app/s3_workload.py "$@"
}

require_up() {
  if ! docker inspect "$CLIENT" >/dev/null 2>&1; then
    printf '%sStack is not running.%s Start it first:\n\n  docker compose up -d --wait\n\n' "$R" "$N"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
do_topology() {
  section "Topology"
  printf '  %-18s %s\n' "CONTAINER" "NETWORKS"
  for ctr in $(docker ps --filter "name=swfs-" --format '{{.Names}}' | sort); do
    printf '  %-18s %s\n' "$ctr" "$(nets_of "$ctr")"
  done

  printf '\n  %-18s %s\n' "PUBLISHED" "(host-visible ports)"
  docker ps --filter "name=swfs-" --format '  {{.Names}}\t{{.Ports}}' |
    grep -e '->' || note "none"

  # A published port installs a FORWARD ACCEPT for the container IP that
  # matches traffic from any other Docker bridge, whatever the host bind
  # address is. So "how many ports are published" IS a boundary question.
  local published
  published=$(docker ps --filter "name=swfs-" --format '{{.Ports}}' | grep -c -e '->')
  if [ "$published" -eq 1 ]; then
    record 0 "exactly one published port in the whole deployment (s3-lb:8333)"
  else
    record 1 "exactly one published port expected, found $published"
  fi
}

# ---------------------------------------------------------------------------
do_happy() {
  section "Happy path - S3 API from the client network"
  s3 full
  record $? "client completes bucket/object/multipart/authz workload over S3 only"
}

# ---------------------------------------------------------------------------
do_boundary() {
  section "Boundary - what the client plane can reach"

  local m0 v1 f1 db gw1c lb_edge admin_mgmt prom_mgmt
  m0=$(ip_on swfs-master0 "$NET_CLUSTER")
  v1=$(ip_on swfs-volume1 "$NET_CLUSTER")
  f1=$(ip_on swfs-filer1 "$NET_CLUSTER")
  db=$(ip_on swfs-filer-db "$NET_CLUSTER")
  gw1c=$(ip_on swfs-s3-gw-1 "$NET_CLUSTER")
  lb_edge=$(ip_on swfs-s3-lb "$NET_EDGE")
  admin_mgmt=$(ip_on swfs-admin "$NET_MGMT")
  prom_mgmt=$(ip_on swfs-prometheus "$NET_MGMT")

  note "cluster IPs: master0=$m0 volume1=$v1 filer1=$f1 filer-db=$db s3-gw-1=$gw1c"
  note "mgmt IPs:    admin=$admin_mgmt prometheus=$prom_mgmt"
  note "edge IPs:    s3-lb=$lb_edge"
  echo

  # Positive control first: if these fail, every negative below is meaningless.
  probe_from "$CLIENT" reachable "client CAN reach the published S3 endpoint" \
    "s3-lb (service name)=s3-lb:8333" \
    "s3-gw-1 (edge iface)=s3-gw-1:8333"

  # Names simply do not exist on the client network.
  probe_from "$CLIENT" blocked "client cannot RESOLVE any storage component" \
    "master0 by name=master0:9333" \
    "volume1 by name=volume1:8080" \
    "filer1 by name=filer1:8888" \
    "filer-db by name=filer-db:5432"

  # The part that actually matters: bypass DNS and dial the real addresses.
  probe_from "$CLIENT" blocked "client cannot reach the cluster plane by raw IP" \
    "master0 raw ip=${m0}:9333" \
    "volume1 raw ip=${v1}:8080" \
    "filer1 raw ip=${f1}:8888" \
    "filer-db raw ip=${db}:5432" \
    "s3-gw-1 internal iface=${gw1c}:8333"

  probe_from "$CLIENT" blocked "client cannot reach the management plane" \
    "admin ui=${admin_mgmt}:12535" \
    "prometheus=${prom_mgmt}:9090"

  section "Boundary - what the management plane can reach"

  probe_from "$OPS" reachable "operator CAN reach the management tools" \
    "admin ui=admin:12535" \
    "prometheus=prometheus:9090"

  probe_from "$OPS" blocked "operator cannot address the storage tier directly" \
    "master0 raw ip=${m0}:9333" \
    "volume1 raw ip=${v1}:8080" \
    "filer1 raw ip=${f1}:8888" \
    "filer-db raw ip=${db}:5432"

  probe_from "$OPS" blocked "management plane cannot reach the client plane" \
    "s3-lb raw ip=${lb_edge}:8333"

  section "Boundary - positive control from inside the cluster"
  # Same addresses, same probe code, from a container that IS on the cluster
  # network. Without this, the failures above could just mean "probe broken".
  "${COMPOSE[@]}" --profile probe up -d cluster-probe >/dev/null 2>&1
  if wait_healthy "$PROBE" 30; then
    probe_from "$PROBE" reachable "the very same addresses ARE reachable from the cluster network" \
      "master0 raw ip=${m0}:9333" \
      "volume1 raw ip=${v1}:8080" \
      "filer1 raw ip=${f1}:8888" \
      "filer-db raw ip=${db}:5432"
    probe_from "$PROBE" blocked "cluster network has no path to the client plane" \
      "s3-lb raw ip=${lb_edge}:8333"
  else
    skip "cluster-side positive control (could not start $PROBE)"
  fi

  section "Known limitation (informational, not a failure)"
  note "The Docker host is attached to all three bridges, so from the VM itself"
  note "every container IP is routable. A single-machine POC cannot fix that;"
  note "on real hardware these planes are separate VLANs/NICs. See ARCHITECTURE.md."
  if timeout 3 bash -c "</dev/tcp/${m0}/9333" 2>/dev/null; then
    note "confirmed: host -> master0:9333 is open (expected on single-host Docker)"
  else
    note "host -> master0:9333 is not open from this shell"
  fi
}

# ---------------------------------------------------------------------------
raft_leader_note() {
  # Best effort, informational only: hashicorp raft logs the transition.
  local who=""
  for m in swfs-master0 swfs-master1 swfs-master2; do
    if docker logs --tail 2000 "$m" 2>&1 | grep -q "entering leader state"; then
      who="$who ${m#swfs-}"
    fi
  done
  [ -n "$who" ] && note "masters that have held leadership so far:$who"
}

do_resilience() {
  section "Resilience - baseline"
  s3 setup >/dev/null 2>&1
  local key="resilience/payload.bin" sha
  sha=$(s3 write --key "$key" --size-mb 8 | tail -1)
  if [ -z "$sha" ]; then record 1 "baseline 8 MiB object written"; return; fi
  record 0 "baseline 8 MiB object written (sha256 ${sha:0:16}...)"

  # --- storage tier -------------------------------------------------------
  section "Resilience - lose a volume server (replication 010: 2 copies, 2 racks)"
  "${COMPOSE[@]}" stop volume2 >/dev/null 2>&1
  note "volume2 (rack2) stopped; waiting for the masters to notice"
  sleep 12
  s3 verify --key "$key" --sha256 "$sha"
  record $? "existing object still readable with volume2 down"
  s3 rw-check --key "resilience/during-volume-outage.bin"
  record $? "new writes still accepted with volume2 down"
  "${COMPOSE[@]}" start volume2 >/dev/null 2>&1
  wait_healthy swfs-volume2 && note "volume2 back"

  # --- control plane ------------------------------------------------------
  section "Resilience - lose one master (raft quorum 2 of 3 survives)"
  raft_leader_note
  "${COMPOSE[@]}" stop master0 >/dev/null 2>&1
  note "master0 stopped; allowing time for a re-election"
  sleep 15
  s3 verify --key "$key" --sha256 "$sha"
  record $? "reads survive the loss of one master"
  s3 rw-check --key "resilience/during-master-outage.bin"
  record $? "writes survive the loss of one master (volume assignment still works)"

  # Quorum must be real, not decorative: with 2 of 3 masters gone there is no
  # leader, so no volume can be assigned and writes MUST fail. A cluster that
  # happily keeps writing here is not actually running a consensus protocol.
  section "Resilience - lose quorum (2 of 3 masters down) - writes must STOP"
  "${COMPOSE[@]}" stop master1 >/dev/null 2>&1
  sleep 10
  if docker exec -e S3_MAX_ATTEMPTS=1 "$CLIENT" \
       python3 /app/s3_workload.py rw-check --key "resilience/no-quorum.bin" >/dev/null 2>&1; then
    record 1 "writes are refused while raft quorum is lost (they succeeded - unexpected)"
  else
    record 0 "writes are refused while raft quorum is lost"
  fi

  "${COMPOSE[@]}" start master0 master1 >/dev/null 2>&1
  wait_healthy swfs-master0 && wait_healthy swfs-master1 && note "masters back"
  sleep 10
  s3 rw-check --key "resilience/after-quorum-restored.bin"
  record $? "writes resume once quorum is restored"

  # --- metadata tier ------------------------------------------------------
  section "Resilience - lose a filer (2 filers, shared metadata store)"
  "${COMPOSE[@]}" stop filer1 >/dev/null 2>&1
  sleep 8
  s3 verify --key "$key" --sha256 "$sha"
  record $? "reads survive the loss of filer1"
  s3 rw-check --key "resilience/during-filer-outage.bin"
  record $? "writes survive the loss of filer1"
  "${COMPOSE[@]}" start filer1 >/dev/null 2>&1
  wait_healthy swfs-filer1 && note "filer1 back"

  # --- gateway tier -------------------------------------------------------
  section "Resilience - lose an S3 gateway (2 gateways behind the edge LB)"
  "${COMPOSE[@]}" stop s3-gw-1 >/dev/null 2>&1
  sleep 6
  s3 verify --key "$key" --sha256 "$sha"
  record $? "client is unaffected by the loss of s3-gw-1 (LB fails over)"
  "${COMPOSE[@]}" start s3-gw-1 >/dev/null 2>&1
  wait_healthy swfs-s3-gw-1 && note "s3-gw-1 back"

  section "Resilience - final consistency check"
  sleep 5
  s3 verify --key "$key" --sha256 "$sha"
  record $? "object is byte-identical after four node failures"
}

# ---------------------------------------------------------------------------
require_up

case "${1:-all}" in
  topology)   do_topology ;;
  happy)      do_happy ;;
  boundary)   do_boundary ;;
  resilience) do_resilience ;;
  all)        do_topology; do_happy; do_boundary; do_resilience ;;
  *) echo "usage: $0 [topology|happy|boundary|resilience|all]"; exit 2 ;;
esac

section "Summary"
printf '  %spassed: %d%s   %sfailed: %d%s   skipped: %d\n\n' "$G" "$PASS" "$N" "$R" "$FAIL" "$N" "$SKIP"
[ "$FAIL" -eq 0 ]
