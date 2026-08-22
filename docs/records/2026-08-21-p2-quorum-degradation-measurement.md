# P2 — pomiar zachowania aplikacji przy utracie kworum (newclaude16-r9)

**Run ID:** `213e521fc1ce4d97b0660173b9a1b352`  
**Data UTC:** `2026-08-22T15:44:28Z`  
**Wynik:** `degraded`  
**Kontrakt:** `{'expected': 'degraded', 'match': True, 'observed': 'degraded'}`

## Kryteria akceptacji

| kryterium | wynik |
| --- | --- |
| `target_guard` | `True` |
| `baseline_complete` | `True` |
| `processes_dead` | `True` |
| `survivor_non_primary` | `True` |
| `vip_write_rejected` | `True` |
| `direct_write_rejected` | `True` |
| `backend_error_exact` | `True` |
| `same_window_monitor` | `True` |
| `exact_runtime_placement` | `True` |
| `bounded_correlated_log` | `True` |
| `dropins_absent` | `True` |
| `restart_policy_restored` | `True` |
| `nodes_primary_synced` | `True` |
| `app_recovered` | `True` |
| `credential_profile_absent` | `True` |
| `classification_resolved` | `True` |
| `platform_verify` | `True` |
| `post_build_gate` | `True` |

## Bledy / nierozstrzygniete warunki

- brak

## Wersje

```json
{
  "client": "mariadb from 11.4.12-MariaDB, client 15.2 for Linux (x86_64) using  EditLine wrapper",
  "mariadb": "11.4.12-MariaDB-log",
  "os_backend": "Rocky Linux 9.8 (Blue Onyx); kernel 5.14.0-687.10.1.el9_8.0.1.x86_64",
  "os_proxysql": {
    "fcp1": "Rocky Linux 10.2 (Red Quartz); kernel 6.12.0-211.16.1.el10_2.0.1.x86_64",
    "fcp2": "Rocky Linux 10.2 (Red Quartz); kernel 6.12.0-211.16.1.el10_2.0.1.x86_64"
  },
  "proxysql": {
    "fcp1": "3.0.10-426-gf5f1e14",
    "fcp2": "3.0.10-426-gf5f1e14"
  }
}
```

## Topologia

```json
{
  "app_user": "app_user_n16",
  "galera": {
    "n16g1": "192.168.1.172",
    "n16g2": "192.168.1.173",
    "n16g3": "192.168.1.174"
  },
  "offline_hostgroup": 840,
  "proxysql": {
    "fcp1": "192.168.1.131",
    "fcp2": "192.168.1.132"
  },
  "vip": "192.168.1.139",
  "writer_hostgroup": 810
}
```

## Baseline

```json
{
  "app_write_ok": true,
  "galera": {
    "n16g1": "Primary/3/4",
    "n16g2": "Primary/3/4",
    "n16g3": "Primary/3/4"
  },
  "restart_policy": {
    "n16g2": "on-abnormal",
    "n16g3": "on-abnormal"
  },
  "runtime_writer": {
    "fcp1": {
      "hostgroup_id": 810,
      "hostname": "192.168.1.174",
      "status": "ONLINE"
    },
    "fcp2": {
      "hostgroup_id": 810,
      "hostname": "192.168.1.174",
      "status": "ONLINE"
    }
  },
  "vip_holder": "fcp1"
}
```

## Okno awarii

```json
{
  "app_code": "2027",
  "app_error": "--------------\nINSERT INTO app_degradation () VALUES ()\n--------------\n\nERROR 2027 (HY000) at line 1: Received malformed packetThe command exited with a non-zero return code.",
  "app_sqlstate": "HY000",
  "log_mark": {
    "inode": 100664724,
    "size": 7948735
  },
  "monitor_row": {
    "error": "NONE",
    "hostname": "192.168.1.172",
    "primary_partition": "NO",
    "time_start_us": 1787413514398042,
    "wsrep_local_state": 0
  },
  "node_code": "1047",
  "node_error": "--------------\nINSERT INTO app_degradation () VALUES ()\n--------------\n\nERROR 1047 (08S01) at line 1: WSREP has not yet prepared node for application useThe command exited with a non-zero return code.",
  "node_sqlstate": "08S01",
  "proxysql_log": "2026-08-22 15:45:14 MySQL_Monitor.cpp:2441:monitor_galera_thread(): [ERROR] Error on Galera check for 192.168.1.173:3306 after 0ms. Unable to create a connection. If the server is overload, increase mysql-monitor_connect_timeout. Error: timeout or error in creating new connection: Can't connect to server on '192.168.1.173' (115).\n2026-08-22 15:45:14 MySQL_Monitor.cpp:2717:monitor_galera_thread(): [ERROR] Server 192.168.1.173:3306 missed 3 Galera checks. Assuming offline\n2026-08-22 15:45:14 MySQL_Monitor.cpp:2441:monitor_galera_thread(): [ERROR] Error on Galera check for 192.168.1.174:3306 after 0ms. Unable to create a connection. If the server is overload, increase mysql-monitor_connect_timeout. Error: timeout or error in creating new connection: Can't connect to server on '192.168.1.174' (115).\n2026-08-22 15:45:14 MySQL_Monitor.cpp:2717:monitor_galera_thread(): [ERROR] Server 192.168.1.174:3306 missed 3 Galera checks. Assuming offline\n2026-08-22 15:45:14 MySQL_HostGroups_Manager.cpp:5614:converge_galera_config(): [WARNING] Galera: we couldn't find any healthy node for writer HG 810\n2026-08-22 15:45:14 [INFO] Galera: possible writer candidate for HG 810: 192.168.1.172:3306\n2026-08-22 15:45:14 [INFO] Galera: trying to use server 192.168.1.172:3306 as a writer for HG 810\n2026-08-22 15:45:14 MySQL_HostGroups_Manager.cpp:5164:update_galera_set_offline(): [WARNING] Galera: setting host 192.168.1.174:3306 offline because: timeout or error in creating new connection: Can't connect to server on '192.168.1.174' (115)\n2026-08-22 15:45:14 [INFO] Galera: Node status changed by ProxySQL, dumping all galera nodes status:\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n| hostname      | port | start_time       | check_time | primary_partition | read_only | wsrep_local_recv_queue | wsrep_local_state | wsrep_desync | wsrep_reject_queries | wsrep_sst_donor_rejects_queries | pxc_maint_mode | error                                                                                         |\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n| 192.168.1.140 | 3306 | 1787413509396795 | 1625       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.141 | 3306 | 1787413509396706 | 1318       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.142 | 3306 | 1787413509397052 | 1336       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.172 | 3306 | 1787413509396645 | 1914       | 0                 | 0         | 0                      | 0                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.173 | 3306 | 1787413514395942 | 0          | 0                 | 1         | 0                      | 0                 | 1            | 1                    | 1                               | 0              | timeout or error in creating new connection: Can't connect to server on '192.168.1.173' (115) |\n| 192.168.1.174 | 3306 | 1787413514396371 | 0          | 0                 | 1         | 0                      | 0                 | 1            | 1                    | 1                               | 0              | timeout or error in creating new connection: Can't connect to server on '192.168.1.174' (115) |\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n2026-08-22 15:45:14 [INFO] Generating runtime mysql servers records only.\n2026-08-22 15:45:14 [INFO] Dumping current MySQL Servers structures for hostgroup ALL\nHID: 10 , address: 192.168.1.140 , port: 3306 , gtid_port: 0 , weight: 1 , status: SHUNNED , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 10 , address: 192.168.1.141 , port: 3306 , gtid_port: 0 , weight: 1 , status: SHUNNED , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 10 , address: 192.168.1.142 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 810 , address: 192.168.1.174 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 20 , address: 192.168.1.141 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 20 , address: 192.168.1.140 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 840 , address: 192.168.1.173 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 840 , address: 192.168.1.172 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \n2026-08-22 15:45:14 [INFO] Dumping mysql_servers: ALL\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| hid | hostname      | port | gtid | weight | status | cmp | max_conns | max_lag | ssl | max_lat | comment | mem_pointer     |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| 10  | 192.168.1.140 | 3306 | 0    | 1      | 1      | 0   | 1000      | 0       | 0   | 0       |         | 140621643599552 |\n| 840 | 192.168.1.172 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639784064 |\n| 840 | 192.168.1.173 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639783488 |\n| 20  | 192.168.1.140 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 0   | 0       |         | 140621520610560 |\n| 20  | 192.168.1.141 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 0   | 0       |         | 140621520610368 |\n| 810 | 192.168.1.174 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621619675200 |\n| 10  | 192.168.1.142 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 0   | 0       |         | 140621643599936 |\n| 10  | 192.168.1.141 | 3306 | 0    | 1      | 1      | 0   | 1000      | 0       | 0   | 0       |         | 140621643599744 |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n2026-08-22 15:45:14 [INFO] Dumping mysql_servers_incoming\n+--------------+---------------+------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+\n| hostgroup_id | hostname      | port | gtid_port | weight | status | compression | max_connections | max_replication_lag | use_ssl | max_latency_ms | comment |\n+--------------+---------------+------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+\n| 10           | 192.168.1.140 | 3306 | 0         | 1      | 1      | 0           | 1000            | 0                   | 0       | 0              |         |\n| 810          | 192.168.1.172 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 1       | 0              |         |\n| 840          | 192.168.1.173 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 1       | 0              |         |\n| 20           | 192.168.1.140 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 0       | 0              |         |\n| 20           | 192.168.1.141 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 0       | 0              |         |\n| 840          | 192.168.1.174 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 1       | 0              |         |\n| 10           | 192.168.1.142 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 0       | 0              |         |\n| 10           | 192.168.1.141 | 3306 | 0         | 1      | 1      | 0           | 1000            | 0                   | 0       | 0              |         |\n+--------------+---------------+------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+\n2026-08-22 15:45:14 [INFO] Dumping mysql_servers LEFT JOIN mysql_servers_incoming\n+-----------------+--------------+---------------+------+\n| mem_pointer     | hostgroup_id | hostname      | port |\n+-----------------+--------------+---------------+------+\n| 140621639784064 | 840          | 192.168.1.172 | 3306 |\n| 140621619675200 | 810          | 192.168.1.174 | 3306 |\n+-----------------+--------------+---------------+------+\n2026-08-22 15:45:14 MySQL_HostGroups_Manager.cpp:1342:commit(): [WARNING] Removed server at address 140621639784064, hostgroup 840, address 192.168.1.172 port 3306. Setting status OFFLINE HARD and immediately dropping all free connections. Used connections will be dropped when trying to use them\n2026-08-22 15:45:14 MySQL_HostGroups_Manager.cpp:1342:commit(): [WARNING] Removed server at address 140621619675200, hostgroup 810, address 192.168.1.174 port 3306. Setting status OFFLINE HARD and immediately dropping all free connections. Used connections will be dropped when trying to use them\n2026-08-22 15:45:14 [INFO] Dumping mysql_servers JOIN mysql_servers_incoming\n+--------------+---------------+------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+-------------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+\n| hostgroup_id | hostname      | port | gtid_port | weight | status | compression | max_connections | max_replication_lag | use_ssl | max_latency_ms | comment | mem_pointer | gtid_port | weight | status | compression | max_connections | max_replication_lag | use_ssl | max_latency_ms | comment |\n+--------------+---------------+------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+-------------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+\n| 810          | 192.168.1.172 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 1       | 0              |         | 0           | 0         | 1      | 0      | 0           | 1000            | 0                   | 1       | 0              |         |\n| 840          | 192.168.1.174 | 3306 | 0         | 1      | 0      | 0           | 1000            | 0                   | 1       | 0              |         | 0           | 0         | 1      | 0      | 0           | 1000            | 0                   | 1       | 0              |         |\n+--------------+---------------+------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+-------------+-----------+--------+--------+-------------+-----------------+---------------------+---------+----------------+---------+\n2026-08-22 15:45:14 [INFO] Creating new server in HG 810 : 192.168.1.172:3306 , gtid_port=0, weight=1, status=0\n2026-08-22 15:45:14 [INFO] Creating new server in HG 840 : 192.168.1.174:3306 , gtid_port=0, weight=1, status=0\n2026-08-22 15:45:14 [INFO] Dumping current MySQL Servers structures for hostgroup ALL\nHID: 10 , address: 192.168.1.140 , port: 3306 , gtid_port: 0 , weight: 1 , status: SHUNNED , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 10 , address: 192.168.1.141 , port: 3306 , gtid_port: 0 , weight: 1 , status: SHUNNED , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 10 , address: 192.168.1.142 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 810 , address: 192.168.1.174 , port: 3306 , gtid_port: 0 , weight: 1 , status: OFFLINE_HARD , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 810 , address: 192.168.1.172 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 20 , address: 192.168.1.141 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 20 , address: 192.168.1.140 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 0 , max_latency_ms: 0 , comment: \nHID: 840 , address: 192.168.1.173 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 840 , address: 192.168.1.172 , port: 3306 , gtid_port: 0 , weight: 1 , status: OFFLINE_HARD , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 840 , address: 192.168.1.174 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \n2026-08-22 15:45:14 [INFO] Dumping mysql_servers: ALL\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| hid | hostname      | port | gtid | weight | status | cmp | max_conns | max_lag | ssl | max_lat | comment | mem_pointer     |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| 10  | 192.168.1.140 | 3306 | 0    | 1      | 1      | 0   | 1000      | 0       | 0   | 0       |         | 140621643599552 |\n| 840 | 192.168.1.174 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639790016 |\n| 840 | 192.168.1.172 | 3306 | 0    | 1      | 3      | 0   | 1000      | 0       | 1   | 0       |         | 140621639784064 |\n| 840 | 192.168.1.173 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639783488 |\n| 20  | 192.168.1.140 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 0   | 0       |         | 140621520610560 |\n| 20  | 192.168.1.141 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 0   | 0       |         | 140621520610368 |\n| 810 | 192.168.1.172 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639783104 |\n| 810 | 192.168.1.174 | 3306 | 0    | 1      | 3      | 0   | 1000      | 0       | 1   | 0       |         | 140621619675200 |\n| 10  | 192.168.1.142 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 0   | 0       |         | 140621643599936 |\n| 10  | 192.168.1.141 | 3306 | 0    | 1      | 1      | 0   | 1000      | 0       | 0   | 0       |         | 140621643599744 |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n2026-08-22 15:45:14 [INFO] Checksum for table mysql_servers is 0xE86D0176845F29B0\n2026-08-22 15:45:14 [INFO] Rebuilding 'Hostgroup_Manager_Mapping' due to checksums change - mysql_servers { old: 0xD69E1C1B243C28C7, new: 0x6845F29B0E86D017 }, mysql_replication_hostgroups { old:0x0, new:0x0 }\n2026-08-22 15:45:14 [INFO] MySQL_HostGroups_Manager::commit() locked for 2ms\n2026-08-22 15:45:14 [INFO] Dumping current MySQL Servers structures for hostgroup 810\nHID: 810 , address: 192.168.1.174 , port: 3306 , gtid_port: 0 , weight: 1 , status: OFFLINE_HARD , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 810 , address: 192.168.1.172 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \n2026-08-22 15:45:14 [INFO] Dumping mysql_servers: HG 810\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| hid | hostname      | port | gtid | weight | status | cmp | max_conns | max_lag | ssl | max_lat | comment | mem_pointer     |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| 810 | 192.168.1.172 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639783104 |\n| 810 | 192.168.1.174 | 3306 | 0    | 1      | 3      | 0   | 1000      | 0       | 1   | 0       |         | 140621619675200 |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n2026-08-22 15:45:14 [INFO] Dumping current MySQL Servers structures for hostgroup 820\n2026-08-22 15:45:14 [INFO] Dumping mysql_servers: HG 820\n+-----+----------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-------------+\n| hid | hostname | port | gtid | weight | status | cmp | max_conns | max_lag | ssl | max_lat | comment | mem_pointer |\n+-----+----------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-------------+\n+-----+----------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-------------+\n2026-08-22 15:45:14 [INFO] Dumping current MySQL Servers structures for hostgroup 830\n2026-08-22 15:45:14 [INFO] Dumping mysql_servers: HG 830\n+-----+----------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-------------+\n| hid | hostname | port | gtid | weight | status | cmp | max_conns | max_lag | ssl | max_lat | comment | mem_pointer |\n+-----+----------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-------------+\n+-----+----------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-------------+\n2026-08-22 15:45:14 [INFO] Dumping current MySQL Servers structures for hostgroup 840\nHID: 840 , address: 192.168.1.173 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 840 , address: 192.168.1.172 , port: 3306 , gtid_port: 0 , weight: 1 , status: OFFLINE_HARD , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \nHID: 840 , address: 192.168.1.174 , port: 3306 , gtid_port: 0 , weight: 1 , status: ONLINE , max_connections: 1000 , max_replication_lag: 0 , use_ssl: 1 , max_latency_ms: 0 , comment: \n2026-08-22 15:45:14 [INFO] Dumping mysql_servers: HG 840\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| hid | hostname      | port | gtid | weight | status | cmp | max_conns | max_lag | ssl | max_lat | comment | mem_pointer     |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n| 840 | 192.168.1.172 | 3306 | 0    | 1      | 3      | 0   | 1000      | 0       | 1   | 0       |         | 140621639784064 |\n| 840 | 192.168.1.173 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639783488 |\n| 840 | 192.168.1.174 | 3306 | 0    | 1      | 0      | 0   | 1000      | 0       | 1   | 0       |         | 140621639790016 |\n+-----+---------------+------+------+--------+--------+-----+-----------+---------+-----+---------+---------+-----------------+\n2026-08-22 15:45:14 MySQL_HostGroups_Manager.cpp:5614:converge_galera_config(): [WARNING] Galera: we couldn't find any healthy node for writer HG 810\n2026-08-22 15:45:14 [INFO] Galera: possible writer candidate for HG 810: 192.168.1.172:3306\n2026-08-22 15:45:14 [INFO] Galera: trying to use server 192.168.1.172:3306 as a writer for HG 810\n2026-08-22 15:45:14 MySQL_HostGroups_Manager.cpp:5195:update_galera_set_offline(): [WARNING] Galera: skipping setting offline node 192.168.1.172:3306 from hostgroup 810 because won't change the list of ONLINE nodes\n2026-08-22 15:45:14 [INFO] Galera: Node status changed by ProxySQL, dumping all galera nodes status:\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n| hostname      | port | start_time       | check_time | primary_partition | read_only | wsrep_local_recv_queue | wsrep_local_state | wsrep_desync | wsrep_reject_queries | wsrep_sst_donor_rejects_queries | pxc_maint_mode | error                                                                                         |\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n| 192.168.1.140 | 3306 | 1787413514396421 | 2456       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.141 | 3306 | 1787413509396706 | 1318       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.142 | 3306 | 1787413514397148 | 1638       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.172 | 3306 | 1787413514398042 | 2458       | 0                 | 0         | 0                      | 0                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.173 | 3306 | 1787413514395942 | 0          | 0                 | 1         | 0                      | 0                 | 1            | 1                    | 1                               | 0              | timeout or error in creating new connection: Can't connect to server on '192.168.1.173' (115) |\n| 192.168.1.174 | 3306 | 1787413514396371 | 0          | 0                 | 1         | 0                      | 0                 | 1            | 1                    | 1                               | 0              | timeout or error in creating new connection: Can't connect to server on '192.168.1.174' (115) |\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n2026-08-22 15:45:15 MySQL_Thread.cpp:4543:ProcessAllSessions_Healthy0(): [WARNING] Closing unhealthy client connection 127.0.0.1:34742 , user 'unknown' , hostgroup -1 , connection 0\n2026-08-22 15:45:15 MySQL_Session.cpp:5561:handler_minus1_LogErrorDuringQuery(): [WARNING] Error during query on (810,192.168.1.172,3306,121932): 1047, WSREP has not yet prepared node for application use\n2026-08-22 15:45:17 MySQL_Thread.cpp:4543:ProcessAllSessions_Healthy0(): [WARNING] Closing unhealthy client connection 127.0.0.1:44552 , user 'unknown' , hostgroup -1 , connection 0\n2026-08-22 15:45:19 MySQL_Thread.cpp:4543:ProcessAllSessions_Healthy0(): [WARNING] Closing unhealthy client connection 127.0.0.1:44554 , user 'unknown' , hostgroup -1 , connection 0\n2026-08-22 15:45:19 MySQL_Monitor.cpp:2441:monitor_galera_thread(): [ERROR] Error on Galera check for 192.168.1.174:3306 after 0ms. Unable to create a connection. If the server is overload, increase mysql-monitor_connect_timeout. Error: timeout or error in creating new connection: Can't connect to server on '192.168.1.174' (115).\n2026-08-22 15:45:19 MySQL_Monitor.cpp:2717:monitor_galera_thread(): [ERROR] Server 192.168.1.174:3306 missed 3 Galera checks. Assuming offline\n2026-08-22 15:45:19 MySQL_Monitor.cpp:2441:monitor_galera_thread(): [ERROR] Error on Galera check for 192.168.1.173:3306 after 1ms. Unable to create a connection. If the server is overload, increase mysql-monitor_connect_timeout. Error: timeout or error in creating new connection: Can't connect to server on '192.168.1.173' (115).\n2026-08-22 15:45:19 MySQL_Monitor.cpp:2717:monitor_galera_thread(): [ERROR] Server 192.168.1.173:3306 missed 3 Galera checks. Assuming offline\n2026-08-22 15:45:19 MySQL_HostGroups_Manager.cpp:5614:converge_galera_config(): [WARNING] Galera: we couldn't find any healthy node for writer HG 810\n2026-08-22 15:45:19 [INFO] Galera: possible writer candidate for HG 810: 192.168.1.172:3306\n2026-08-22 15:45:19 [INFO] Galera: trying to use server 192.168.1.172:3306 as a writer for HG 810\n2026-08-22 15:45:19 MySQL_HostGroups_Manager.cpp:5195:update_galera_set_offline(): [WARNING] Galera: skipping setting offline node 192.168.1.172:3306 from hostgroup 810 because won't change the list of ONLINE nodes\n2026-08-22 15:45:19 [INFO] Galera: Node status changed by ProxySQL, dumping all galera nodes status:\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n| hostname      | port | start_time       | check_time | primary_partition | read_only | wsrep_local_recv_queue | wsrep_local_state | wsrep_desync | wsrep_reject_queries | wsrep_sst_donor_rejects_queries | pxc_maint_mode | error                                                                                         |\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+\n| 192.168.1.140 | 3306 | 1787413519397654 | 1460       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.141 | 3306 | 1787413519397464 | 1282       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.142 | 3306 | 1787413519397899 | 1441       | 1                 | 0         | 0                      | 4                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.172 | 3306 | 1787413519397153 | 2216       | 0                 | 0         | 0                      | 0                 | 0            | 0                    | 0                               | 0              |                                                                                               |\n| 192.168.1.173 | 3306 | 1787413519396086 | 0          | 0                 | 1         | 0                      | 0                 | 1            | 1                    | 1                               | 0              | timeout or error in creating new connection: Can't connect to server on '192.168.1.173' (115) |\n| 192.168.1.174 | 3306 | 1787413519396768 | 0          | 0                 | 1         | 0                      | 0                 | 1            | 1                    | 1                               | 0              | timeout or error in creating new connection: Can't connect to server on '192.168.1.174' (115) |\n+---------------+------+------------------+------------+-------------------+-----------+------------------------+-------------------+--------------+----------------------+---------------------------------+----------------+-----------------------------------------------------------------------------------------------+",
  "proxysql_node": "fcp1",
  "runtime_survivor": {
    "hostgroup_id": 810,
    "hostname": "192.168.1.172",
    "status": "ONLINE"
  },
  "stopped": [
    "n16g2",
    "n16g3"
  ],
  "survivor": "n16g1",
  "survivor_status": "non-Primary",
  "window_ended_utc": "2026-08-22T15:45:23Z",
  "window_start_us": 1787413514121413,
  "window_started_utc": "2026-08-22T15:45:14Z"
}
```

## Cleanup

```json
{
  "credential_history": [
    {
      "attempt": 1,
      "remove": {
        "error": "",
        "ok": true,
        "output": "",
        "rc": 0
      },
      "verify": {
        "error": "",
        "ok": true,
        "output": "ABSENT",
        "rc": 0
      }
    }
  ],
  "credential_profile_absent": true,
  "nodes": {
    "n16g2": {
      "dropin_absent": true,
      "errors": [],
      "reload_ok": true,
      "remove_ok": true,
      "restart_policy_after": "on-abnormal",
      "restart_policy_before": "on-abnormal",
      "restart_policy_restored": true,
      "start_enqueued": true
    },
    "n16g3": {
      "dropin_absent": true,
      "errors": [],
      "reload_ok": true,
      "remove_ok": true,
      "restart_policy_after": "on-abnormal",
      "restart_policy_before": "on-abnormal",
      "restart_policy_restored": true,
      "start_enqueued": true
    }
  }
}
```

## Recovery

```json
{
  "app_write_ok": true,
  "nodes": {
    "n16g1": "Primary/3/4",
    "n16g2": "Primary/3/4",
    "n16g3": "Primary/3/4"
  }
}
```

## Bramki zewnetrzne

```json
{
  "platform_verify": {
    "command": "make platform-verify",
    "ok": true,
    "rc": 0
  },
  "post_build_gate": {
    "command": "make lab-post-build-gate CLUSTER=newclaude16-r9",
    "ok": true,
    "rc": 0
  }
}
```

## Kontekst wykonania

- artifact: `/var/tmp/quorum-evidence-newclaude16-r9-213e521fc1ce4d97b0660173b9a1b352.json`
- run ID: `213e521fc1ce4d97b0660173b9a1b352`
- commit harnessu: `0cf12a1e20a8cc2b5f74593007113aebf4215769`
- pomiar: `QUORUM_RUN_ID=213e521fc1ce4d97b0660173b9a1b352 make lab-app-degradation-test CLUSTER=newclaude16-r9 CONFIRM=yes`
- platform recovery gate: `make platform-verify`
- tenant recovery gate: `make lab-post-build-gate CLUSTER=newclaude16-r9`
- final acceptance problems: `[]`
