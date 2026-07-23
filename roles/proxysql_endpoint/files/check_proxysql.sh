#!/bin/bash
# ISC-26: report healthy ONLY when the local ProxySQL client port accepts
# connections. keepalived's vrrp_script uses this to drop VIP priority (and
# release the VIP) when ProxySQL on this node is down, so the VIP never points
# at an unhealthy instance. TCP open on 6033 == ProxySQL live and listening.
timeout 2 bash -c "true </dev/tcp/127.0.0.1/6033" 2>/dev/null
