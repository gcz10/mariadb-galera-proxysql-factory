# Application Quorum Degradation Measurement Design

**Date:** 2026-08-21
**Status:** Approved; implementation plan: `docs/superpowers/plans/2026-08-21-p2-application-quorum-degradation.md`

## Goal

Produce a current, reproducible, evidence-backed report of what an application sees through the shared ProxySQL endpoint when its Galera cluster loses quorum.

The post-read action for an internal engineer is concrete: run the approved destructive laboratory measurement against `newclaude16-r9`, prove that cleanup and recovery completed, and prepare a filing-ready ProxySQL issue if the client still receives a protocol error instead of the backend database error.

This work does not implement a local routing controller or application workaround.

## Current Evidence

The existing laboratory test already models the required failure path:

- the client runs on the shared application host and connects through the ProxySQL VIP;
- two Galera processes are killed abruptly after disabling their automatic systemd restart;
- the surviving node must reach `non-Primary`, otherwise the result is rejected;
- one write is attempted through the VIP and another directly against the surviving node;
- cleanup always removes the temporary `Restart=no` drop-ins, restarts both stopped nodes without blocking, and waits for application writes to recover.

The current expected degraded contract is:

- the direct backend returns `ERROR 1047` / SQLSTATE `08S01` (`WSREP has not yet prepared node for application use`);
- the same operation through ProxySQL returns `ERROR 2027` (`Received malformed packet`);
- no write is accepted while the cluster lacks quorum;
- writes resume after the cluster reforms.

This behavior was recorded previously, but there is no durable run artifact proving it on the current `newclaude16-r9` fleet and current ProxySQL configuration.

The P4 investigation established the routing precondition empirically. ProxySQL moves unhealthy nodes to the tenant's offline hostgroup while another healthy candidate exists, but leaves one last node ONLINE in the writer hostgroup when every node is non-Primary. This measured “last man standing” behavior explains why a client query still reaches the surviving, non-writable backend.

ProxySQL's Galera documentation defines writer, backup, reader, and offline hostgroups, but does not textually document the “last man standing” case; the detailed health criteria are present only in flowchart images. Any statement about that behavior in the report must therefore be marked as measured, not documented.

Upstream issue [sysown/proxysql#1596](https://github.com/sysown/proxysql/issues/1596) documents crashes in older ProxySQL versions after a backend returns `1047`. A maintainer stated that the original issue was fixed in 1.4.9. A later report against 2.0.4 was explicitly treated as potentially different. The current report must not claim that ProxySQL 3.0.10's malformed packet is the same bug; it may only note that both observations involve a backend `1047` reaching ProxySQL's MySQL session/result handling path.

## Scope

This work includes:

1. A pre-failure baseline:
   - current ProxySQL, MariaDB, and client versions;
   - healthy Galera state (`Primary`, size 3, all Synced);
   - current writer identity and ProxySQL hostgroup placement;
   - a successful application write through the VIP.
2. One destructive, laboratory-only quorum-loss measurement on `newclaude16-r9`.
3. Correlated evidence from the same failure window:
   - application error through the VIP;
   - direct backend error;
   - latest ProxySQL Galera monitor row for the surviving backend;
   - runtime ProxySQL hostgroup placement;
   - ProxySQL log lines showing the backend error received by ProxySQL.
4. Recovery proof:
   - all temporary systemd drop-ins removed;
   - all three nodes back in `Primary`, Synced, size 3;
   - application write through the VIP succeeds again;
   - shared-platform verification and the complete n16 post-build gate pass.
5. A filing-ready upstream issue draft if the degraded behavior reproduces.
6. Documentation of the measured result and the issue URL after filing.

This work excludes:

- a local ProxySQL scheduler or control loop;
- automatic mutation of `mysql_users`, `mysql_servers`, or Galera hostgroup activation during an outage;
- treating `ERROR 2027` as a permanent application API contract;
- changing client retry policy;
- testing a production-profile cluster;
- claiming a relation to upstream issue #1596 beyond the shared error-processing context supported by evidence.

## Measurement Procedure

### 1. Baseline

Record all version strings and live state before mutation. The baseline must show a usable client path and a healthy three-node cluster. If the application cannot write before the experiment, stop: the test would not isolate quorum loss.

### 2. Create quorum loss

Use the existing degradation harness. It disables automatic abnormal-restart behavior only on the two selected nodes, then kills `mariadbd` with SIGKILL. The test must verify that both processes are actually absent and that the survivor reaches `non-Primary` within the existing deadline.

A graceful `systemctl stop` is not an acceptable substitute: Galera can treat graceful departures differently and the survivor may remain Primary. A result collected without `non-Primary` is invalid.

### 3. Capture the client/backend contrast

Within the same failure window:

- attempt an application write from the application host through the shared VIP;
- attempt the same class of write directly against the surviving node;
- collect only bounded, timestamped ProxySQL log context relevant to the attempt;
- query the newest `mysql_server_galera_log` row for the survivor;
- query `runtime_mysql_servers` for the tenant's writer and offline hostgroups.

The report must distinguish:

- the error ProxySQL received from the backend;
- the error ProxySQL returned to the client;
- the node's Galera state;
- the node's ProxySQL hostgroup and status.

Secrets must never appear in process arguments, logs, or the issue draft. Use existing protected client profiles and environment-loaded credentials.

### 4. Cleanup and recovery

Cleanup is mandatory even when evidence collection fails. For each stopped node:

1. remove the temporary systemd drop-in;
2. reload systemd;
3. enqueue MariaDB startup without blocking on cluster formation;
4. continue cleanup for the other node even if one operation times out.

After cleanup, wait for the survivor to report `Primary` and for an application write through the VIP to succeed. Then run the platform verification and complete n16 gate. Failure to restore the fleet is a test failure regardless of the reproduced error codes.

## Contracts

### Safety

A write must not succeed through either path while the cluster lacks quorum. Any accepted write is a critical failure and blocks issue filing until data integrity is understood.

### Diagnosability

The measurement has two accepted outcomes:

- **Degraded reproduced:** VIP returns `2026` or `2027` while the backend returns `1047` / `08S01`. Prepare an upstream issue.
- **Clean behavior:** VIP returns the backend database error or a clean connection failure. Do not file the malformed-packet issue; change the repository's expected contract from `degraded` to `clean` and make that transition explicit.

Any third outcome is unresolved. Do not silently classify it as either contract.

### Recovery

The application must resume without manual database repair after the two nodes restart and the cluster reforms. Temporary service overrides must be absent on every node.

## Upstream Issue Requirements

The issue draft must include:

- ProxySQL, MariaDB, OS, and client versions;
- a minimal topology description: shared ProxySQL pair, one three-node Galera tenant, one application client through the VIP;
- deterministic reproduction steps without repository-specific secrets;
- expected result: preserve backend `1047` / SQLSTATE `08S01`, or return a clean connection error;
- actual result from the current run;
- bounded ProxySQL logs proving what backend error ProxySQL received;
- Galera monitor and runtime hostgroup evidence;
- recovery result;
- a link to #1596 only as related historical context, with an explicit statement that sameness is not established.

The issue must not include private IPs unless necessary for readability; use symbolic names in the distilled reproduction.

## Acceptance Criteria

The measurement is complete only when all of the following are true:

- baseline application write passed;
- two database processes were confirmed dead;
- the survivor was confirmed `non-Primary`;
- both client paths rejected writes;
- exact client and backend error codes were captured;
- ProxySQL monitor, hostgroup, and log evidence came from the same failure window;
- both stopped nodes restarted and temporary drop-ins were removed;
- cluster returned to three Primary/Synced nodes;
- application writes resumed through the VIP;
- platform verification passed;
- complete n16 post-build gate passed;
- the outcome was classified as `degraded`, `clean`, or unresolved without inference;
- when degraded reproduces, the issue draft satisfies every upstream requirement above.
