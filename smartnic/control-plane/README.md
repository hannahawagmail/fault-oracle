# SPDX-License-Identifier: Apache-2.0
# DPU Control Plane Setup

This document describes how to configure the DPU management plane for:
1. OvS-DPDK setup on DPU ARM cores (not host CPU)
2. Provisioning the DPU management network
3. Installing `hw-fault-exporter` on DPU ARM Linux
4. Configuring Prometheus scrape for DPU endpoints
5. Running eBPF/bpftrace probes on DPU Linux
6. Telemetry export strategies: push vs pull

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Accessing the DPU Management Plane](#accessing-the-dpu-management-plane)
3. [OvS-DPDK on DPU ARM Cores](#ovs-dpdk-on-dpu-arm-cores)
4. [DPU Management Network Provisioning](#dpu-management-network-provisioning)
5. [Installing hw-fault-exporter on DPU](#installing-hw-fault-exporter-on-dpu)
6. [Prometheus Scrape Configuration](#prometheus-scrape-configuration)
7. [eBPF on DPU Linux](#ebpf-on-dpu-linux)
8. [Telemetry Export: Push vs Pull](#telemetry-export-push-vs-pull)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

**DPU hardware** (any of):
- NVIDIA BlueField-3 (BF3): 16× Cortex-A78, 32GB LPDDR5, ConnectX-7 ASIC
- Marvell OCTEON 10: 24× Neoverse N2, 32GB LPDDR5
- AMD Pensando DSC-200: custom ASIC, 24GB DRAM
- Intel IPU E2100: 16× Cortex-A55, 16GB LPDDR5

**For QEMU simulation** (no physical DPU required):
```bash
# Launch ARM64 QEMU instance simulating DPU control plane
qemu-system-aarch64 \
  -machine virt -cpu cortex-a72 -m 1G -smp 2 \
  -kernel /path/to/Image \
  -append "console=ttyAMA0 root=/dev/vda rw" \
  -drive file=dpu-rootfs.qcow2,if=virtio \
  -netdev user,id=mgmt,hostfwd=tcp::2222-:22 \
  -device virtio-net-pci,netdev=mgmt \
  -nographic
```

**DPU software requirements:**
- Linux kernel >= 5.15 (for eBPF BTF, EDAC subsystem)
- Open vSwitch >= 2.17 with DPDK 21.11+
- Python >= 3.9 (for management scripts)
- systemd (for service management)

---

## Accessing the DPU Management Plane

The DPU management plane is accessed out-of-band — separately from the host.

### Via Management Ethernet

Each DPU has a dedicated management Ethernet port (distinct from the data plane
ports). On NVIDIA BlueField-3, this is the `oob_net0` interface on the DPU.

```bash
# From the data center management network
ssh admin@<dpu-management-ip>

# DPU management IP is typically provisioned via DHCP on the management VLAN
# or statically configured during rack installation
```

### Via Serial Console (RJ45)

The DPU exposes a serial console via the server's BMC or directly:
```bash
# Via BMC (iDRAC, iLO, IPMI SoL)
ipmitool -I lanplus -H <bmc-ip> -U admin -P <pass> sol activate

# The DPU ARM console is typically on a secondary serial device:
# /dev/ttyS1 or /dev/ttyUSB0 on BMC, depending on cabling
```

### Via Host (Emergency Access)

If the management network is unavailable, NVIDIA BlueField provides a
`rshim` driver that allows SSH over PCIe from the host to DPU:
```bash
# On host: load rshim driver (BlueField-specific)
modprobe rshim_pcie
# DPU ARM Linux appears as a USB-over-PCIe network interface
ssh admin@192.168.100.2   # BlueField default rshim IP
```

---

## OvS-DPDK on DPU ARM Cores

OvS on the DPU ARM replaces the host kernel's vSwitch. All OvS processing
runs on DPU ARM cores — no host CPU involvement.

### Architecture

```
Host VM (VirtIO-net VF)
         │
         │  PCIe VirtIO queues
         ▼
DPU ASIC (fast path: hardware-offloaded flows)
         │  (miss: new/unknown flows)
         ▼
DPU ARM Linux — OvS-DPDK
  ovs-vswitchd ─────────────────► ASIC flow table (via vendor SDK)
  ovsdb-server  (flow configuration, port management)
         │
         ▼
Physical network ports (400G QSFP-DD)
```

### Installation on DPU ARM Linux

```bash
# On DPU ARM Linux (aarch64)
apt-get install -y openvswitch-switch dpdk dpdk-dev

# Verify DPDK sees DPU data plane ports
dpdk-devbind.py --status

# Bind DPU data plane PF to vfio-pci for DPDK
echo vfio-pci > /sys/bus/pci/devices/0000:03:00.0/driver_override
echo 0000:03:00.0 > /sys/bus/pci/drivers/vfio-pci/bind
```

### OvS-DPDK Initialization

```bash
# Start OvS with DPDK enabled
export DPDK_DIR=/usr/lib/dpdk
ovs-vsctl --no-wait init
ovs-vsctl --no-wait set Open_vSwitch . other_config:dpdk-init=true
ovs-vsctl --no-wait set Open_vSwitch . other_config:dpdk-lcore-mask=0x3
ovs-vsctl --no-wait set Open_vSwitch . other_config:dpdk-socket-mem="512"

systemctl start openvswitch-switch

# Create the main bridge
ovs-vsctl add-br br-tenant -- set Bridge br-tenant datapath_type=netdev

# Add DPU data plane port (DPDK-bound)
ovs-vsctl add-port br-tenant dpdk0 \
  -- set Interface dpdk0 type=dpdk \
     options:dpdk-devargs=0000:03:00.0

# Enable hardware offload to DPU ASIC
ovs-vsctl set Open_vSwitch . other_config:hw-offload=true
```

### VXLAN Overlay Configuration

```bash
# Add VXLAN tunnel port
ovs-vsctl add-port br-tenant vxlan0 \
  -- set Interface vxlan0 type=vxlan \
     options:remote_ip=flow \
     options:key=flow \
     options:dst_port=4789

# The DPU ASIC handles VXLAN encap/decap at line rate once programmed
# ovs-vswitchd pushes match-action rules via vendor's TC offload API
```

### Verifying Offloaded Flows

```bash
# On DPU ARM Linux
ovs-appctl dpif/show          # Show datapath interfaces
ovs-appctl dpctl/dump-flows type=offloaded   # Flows in ASIC

# Flow counts
ovs-appctl coverage/show | grep -E 'upcall|dp_flow'
```

---

## DPU Management Network Provisioning

The DPU management network must be isolated from the data plane:

```bash
# On DPU ARM Linux: configure management interface
# BlueField-3: management port is oob_net0
ip addr add 10.0.1.42/24 dev oob_net0
ip link set oob_net0 up
ip route add default via 10.0.1.1 dev oob_net0

# Persist via systemd-networkd
cat > /etc/systemd/network/10-oob.network << 'EOF'
[Match]
Name=oob_net0

[Network]
Address=10.0.1.42/24
Gateway=10.0.1.1
DNS=10.0.0.1
EOF

systemctl restart systemd-networkd

# Verify management connectivity
ping -c 3 10.0.1.1
```

### Firewall on Management Interface

```bash
# Allow only SSH and Prometheus scrape on management interface
nft add table inet mgmt-filter
nft add chain inet mgmt-filter input { type filter hook input priority 0 \; policy drop \; }
nft add rule inet mgmt-filter input iifname oob_net0 tcp dport { 22, 9200 } accept
nft add rule inet mgmt-filter input iifname oob_net0 icmp accept
nft add rule inet mgmt-filter input ct state established,related accept
```

---

## Installing hw-fault-exporter on DPU

The `hw-fault-exporter` binary from the `exporter/` directory compiles for
ARM64 and runs identically on DPU ARM Linux.

### Build for ARM64 (cross-compile from x86_64 host)

```bash
# On build host
cd exporter/
GOARCH=arm64 GOOS=linux go build -o hw-fault-exporter-arm64 .

# Transfer to DPU
scp hw-fault-exporter-arm64 admin@<dpu-mgmt-ip>:/tmp/
```

### Automated Install via dpu-setup.sh

The included setup script handles the full installation:

```bash
# On DPU management shell
bash dpu-setup.sh --install-exporter
```

This installs:
- Binary to `/opt/hw-fault-exporter/hw-fault-exporter`
- Systemd unit from `control-plane/dpu-fault-exporter.service`
- Enables and starts the service

### Manual Installation Steps

```bash
# On DPU ARM Linux
install -d /opt/hw-fault-exporter
install -m 0755 /tmp/hw-fault-exporter-arm64 /opt/hw-fault-exporter/hw-fault-exporter

# Install systemd unit
install -m 0644 dpu-fault-exporter.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hw-fault-exporter

# Verify
systemctl status hw-fault-exporter
curl -s http://localhost:9200/metrics | grep edac
```

### Key Differences from Host Installation

| Setting              | Host (default)          | DPU                          |
|----------------------|-------------------------|------------------------------|
| Listen port          | `:9100`                 | `:9200`                      |
| AER collection       | enabled                 | `--disable-aer` (DPU manages)|
| MCE collection       | enabled (x86 only)      | disabled (ARM, uses EDAC)    |
| EDAC collection      | enabled                 | enabled (same subsystem)     |
| OOMScoreAdjust       | `-500`                  | `-900` (DPU critical process)|

---

## Prometheus Scrape Configuration

### Pull Model (Host Scrapes DPU Management IP)

Add to the host Prometheus `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'host-fault-exporter'
    static_configs:
      - targets: ['localhost:9100']

  - job_name: 'dpu-fault-exporter'
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets: ['10.0.1.42:9200']   # DPU management IP
    relabel_configs:
      - source_labels: [__address__]
        target_label: instance
      - target_label: role
        replacement: dpu
      - target_label: dpu_vendor
        replacement: bluefield3        # Set per DPU type
```

Generate the scrape snippet automatically:
```bash
# On DPU ARM Linux
bash dpu-setup.sh --configure-scrape 10.0.1.1   # host Prometheus IP
```

### Push Model (DPU Pushes to Prometheus Pushgateway)

Use when the management network is not directly reachable from the Prometheus
server (e.g., DPU management is on a separate L3 domain):

```bash
# On DPU ARM Linux: push metrics every 30 seconds
cat > /etc/cron.d/dpu-pushgateway << 'EOF'
*/1 * * * * root curl -s http://localhost:9200/metrics | \
  curl -s --data-binary @- \
  http://<pushgateway-host>:9091/metrics/job/dpu-fault-exporter/instance/dpu-42
EOF
```

Or use a dedicated push loop (more reliable than cron for sub-minute intervals):

```bash
cat > /usr/local/bin/dpu-push-metrics.sh << 'SCRIPT'
#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
PUSHGW="${1:-http://10.0.0.1:9091}"
INTERVAL="${2:-30}"
INSTANCE="$(hostname)"

while true; do
  curl -s http://localhost:9200/metrics \
    | curl -s --data-binary @- \
      "${PUSHGW}/metrics/job/dpu-fault-exporter/instance/${INSTANCE}"
  sleep "${INTERVAL}"
done
SCRIPT

chmod +x /usr/local/bin/dpu-push-metrics.sh
# Run as a systemd service (add a unit file similar to dpu-fault-exporter.service)
```

### Recommended Approach

Use **pull** when possible (simpler, stateless, better Prometheus native
support). Use **push** for DPUs behind NAT or when the management network has
no route to the Prometheus server.

---

## eBPF on DPU Linux

The same bpftrace scripts from `kernel-hardening/ebpf/` run on DPU ARM Linux
(kernel 5.15+ with `CONFIG_BPF=y`, `CONFIG_DEBUG_INFO_BTF=y`).

### Prerequisite Check

```bash
# On DPU ARM Linux
uname -r                          # Verify kernel version >= 5.15
ls /sys/kernel/btf/vmlinux        # Verify BTF available
bpftrace --version                # Should be 0.16+

# Install if missing (Debian/Ubuntu ARM64)
apt-get install -y bpftrace linux-headers-$(uname -r)
```

### Running eBPF Probes on DPU

```bash
# Monitor OvS vswitchd process forks (detect daemon crash loops)
bpftrace kernel-hardening/ebpf/detect-fork-storm.bt

# Monitor block I/O anomalies (for NVMe-oF target on DPU)
bpftrace kernel-hardening/ebpf/detect-io-anomaly.bt

# Monitor DPU ARM Linux memory pressure
bpftrace kernel-hardening/ebpf/detect-oom-pressure.bt
```

### DPU-Specific eBPF Use Cases

**Trace OvS upcalls to ARM cores** (packets that miss the ASIC fast path):

```bash
bpftrace -e '
tracepoint:net:netif_rx {
  @pkt_rate[comm] = count();
}
interval:s:1 {
  print(@pkt_rate);
  clear(@pkt_rate);
}' 
```

High upcall rates indicate the ASIC flow cache is thrashing — a signal that
OvS flow rules need tuning or that the ASIC flow table capacity is exceeded.

**Monitor EDAC CE events in real time** (without polling sysfs):

```bash
bpftrace -e '
tracepoint:edac:mc_corrected_error {
  printf("EDAC CE: mc=%d csrow=%d channel=%d\n",
    args->mc_index, args->csrow, args->channel);
  @ce_rate = count();
}'
```

---

## Telemetry Export: Push vs Pull

| Criterion                    | Pull (Prometheus scrapes DPU) | Push (DPU → Pushgateway)     |
|------------------------------|-------------------------------|------------------------------|
| Network requirement          | Prometheus reachable DPU mgmt | DPU reachable to Pushgateway |
| Prometheus native support    | First-class                   | Requires pushgateway         |
| Data freshness               | Scrape interval (30s typical) | Push interval (configurable) |
| State on failure             | No data (alert on absence)    | Last pushed value persists   |
| Multi-DPU aggregation        | Native in prometheus.yml      | All push to same gateway     |
| Configuration complexity     | Low (one scrape job)          | Moderate (push script/unit)  |
| Firewall direction           | Inbound to DPU port 9200      | Outbound from DPU            |

**Recommendation**: use pull in environments where Prometheus has layer-3
reachability to the DPU management VLAN. Use push when DPU management is
behind a NAT or firewall that only permits outbound connections.

---

## Troubleshooting

### Exporter not starting

```bash
# Check service status and journal
systemctl status hw-fault-exporter
journalctl -u hw-fault-exporter -n 50 --no-pager

# Common issue: port already in use
ss -tlnp | grep 9200

# Common issue: sysfs EDAC not initialized
ls /sys/devices/system/edac/mc/
# If empty: EDAC module not loaded
modprobe edac_core
# Then load vendor module or edac_cortex_ref.ko
```

### No EDAC controllers visible

```bash
# On DPU ARM Linux
dmesg | grep -i edac
cat /proc/modules | grep edac

# Load EDAC for Cortex-A (from this repo)
insmod /path/to/edac_cortex_ref.ko
ls /sys/devices/system/edac/mc/   # Should show mc0, mc1, ...
```

### OvS not offloading to ASIC

```bash
# Check hw-offload status
ovs-vsctl get Open_vSwitch . other_config:hw-offload
# Should return: "true"

# Check TC offload on the data plane interface
tc qdisc show dev dpdk0
# Should show: ingress qdisc for TC flower rules

# Dump offloaded flows
ovs-appctl dpctl/dump-flows type=offloaded | wc -l
# If 0: ASIC offload not working — check vendor kernel module loaded
lsmod | grep mlx5      # BlueField
lsmod | grep octeon    # Marvell
```

### Cannot reach DPU management IP from host Prometheus

```bash
# On host: check routing to DPU management VLAN
ip route get 10.0.1.42
# If no route: management VLAN not configured on host

# Alternative: use port forwarding via SSH tunnel for testing
ssh -L 9200:10.0.1.42:9200 admin@<bastion-host>
curl http://localhost:9200/metrics
```
