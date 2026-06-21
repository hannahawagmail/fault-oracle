// SPDX-License-Identifier: Apache-2.0
/*
 * p4-simple-forward.p4 — P4_16 reference for DPU programmable data path
 *
 * Implements: L2 MAC forwarding + VXLAN decap/encap + 5-tuple ACL
 *
 * Compile for simulation:
 *   p4c --target bmv2 --arch v1model p4-simple-forward.p4
 *
 * For real DPU deployment, use vendor backend:
 *   BlueField-3: p4c-mlnx --target bf-switch
 *   Pensando:    p4c-pensando --target dpdk
 *
 * Architecture: v1model (standard reference architecture)
 */
#include <core.p4>
#include <v1model.p4>

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const bit<16> ETHERTYPE_IPV4 = 0x0800;
const bit<8>  IP_PROTO_UDP   = 17;
const bit<16> VXLAN_UDP_PORT = 4789;
const bit<8>  VXLAN_FLAGS    = 0x08;  // VNI present bit

// ---------------------------------------------------------------------------
// Headers
// ---------------------------------------------------------------------------

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> len;
    bit<16> checksum;
}

header vxlan_t {
    bit<8>  flags;
    bit<24> reserved;
    bit<24> vni;
    bit<8>  reserved2;
}

// Custom metadata carried between pipeline stages.
// ingress_port / egress_port mirror standard_metadata for convenience.
struct custom_metadata_t {
    bit<9>  ingress_port;
    bit<9>  egress_port;
    bit<1>  is_vxlan_decapped;
    bit<24> tenant_id;
}

// Header struct: outer + inner headers (for VXLAN encap/decap)
struct headers {
    ethernet_t ethernet;        // outer Ethernet
    ipv4_t     ipv4;            // outer IPv4
    udp_t      udp;             // outer UDP (VXLAN carrier)
    vxlan_t    vxlan;           // VXLAN header
    ethernet_t inner_ethernet;  // inner Ethernet (tenant frame)
    ipv4_t     inner_ipv4;      // inner IPv4 (tenant packet)
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

parser MyParser(
    packet_in             packet,
    out headers           hdr,
    inout custom_metadata_t meta,
    inout standard_metadata_t standard_metadata)
{
    state parse_start {
        meta.ingress_port      = standard_metadata.ingress_port;
        meta.egress_port       = 0;
        meta.is_vxlan_decapped = 0;
        meta.tenant_id         = 0;
        transition parse_ethernet;
    }

    state parse_ethernet {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_IPV4: parse_ipv4;
            default:        accept;
        }
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            IP_PROTO_UDP: parse_udp;
            default:      accept;
        }
    }

    state parse_udp {
        packet.extract(hdr.udp);
        transition select(hdr.udp.dstPort) {
            VXLAN_UDP_PORT: parse_vxlan;
            default:        accept;
        }
    }

    state parse_vxlan {
        packet.extract(hdr.vxlan);
        meta.tenant_id = hdr.vxlan.vni;
        transition parse_inner_ethernet;
    }

    state parse_inner_ethernet {
        packet.extract(hdr.inner_ethernet);
        transition select(hdr.inner_ethernet.etherType) {
            ETHERTYPE_IPV4: parse_inner_ipv4;
            default:        accept;
        }
    }

    state parse_inner_ipv4 {
        packet.extract(hdr.inner_ipv4);
        transition accept;
    }
}

// ---------------------------------------------------------------------------
// Checksum Verification
// ---------------------------------------------------------------------------

control MyVerifyChecksum(
    inout headers           hdr,
    inout custom_metadata_t meta)
{
    apply {
        verify_checksum(
            hdr.ipv4.isValid(),
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.totalLen,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragOffset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr
            },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

// ---------------------------------------------------------------------------
// Ingress Control
// ---------------------------------------------------------------------------

control MyIngress(
    inout headers             hdr,
    inout custom_metadata_t   meta,
    inout standard_metadata_t standard_metadata)
{
    // -----------------------------------------------------------------------
    // Actions
    // -----------------------------------------------------------------------

    // drop: mark packet for discard; no further processing.
    action drop() {
        mark_to_drop(standard_metadata);
    }

    // forward: set the egress port for unicast forwarding.
    action forward(bit<9> port) {
        standard_metadata.egress_spec = port;
        meta.egress_port = port;
    }

    // vxlan_decap: remove outer VXLAN/UDP/IP/Ethernet headers.
    // Promotes inner_ethernet to be the new outer Ethernet frame.
    // The DPU ASIC executes this as a single header-strip operation.
    action vxlan_decap() {
        // Copy inner Ethernet fields to outer Ethernet header
        hdr.ethernet.dstAddr   = hdr.inner_ethernet.dstAddr;
        hdr.ethernet.srcAddr   = hdr.inner_ethernet.srcAddr;
        hdr.ethernet.etherType = hdr.inner_ethernet.etherType;

        // Invalidate tunnel headers (tells deparser to omit them)
        hdr.ipv4.setInvalid();
        hdr.udp.setInvalid();
        hdr.vxlan.setInvalid();
        hdr.inner_ethernet.setInvalid();

        // Promote inner IPv4 as the new outer IPv4
        // (inner_ipv4 remains valid; deparser will emit it after ethernet)
        meta.is_vxlan_decapped = 1;
    }

    // vxlan_encap: wrap the existing Ethernet frame in a VXLAN/UDP/IP/Ethernet
    // tunnel header. vtep_ip is the local VTEP (tunnel endpoint) source IP.
    // The DPU ASIC executes this as a single header-push operation.
    action vxlan_encap(bit<32> vtep_ip, bit<24> vni) {
        // Shift the existing Ethernet into inner position
        hdr.inner_ethernet.dstAddr   = hdr.ethernet.dstAddr;
        hdr.inner_ethernet.srcAddr   = hdr.ethernet.srcAddr;
        hdr.inner_ethernet.etherType = hdr.ethernet.etherType;
        hdr.inner_ethernet.setValid();

        // Shift existing IPv4 into inner position
        hdr.inner_ipv4 = hdr.ipv4;
        hdr.inner_ipv4.setValid();

        // Build outer VXLAN header
        hdr.vxlan.setValid();
        hdr.vxlan.flags     = VXLAN_FLAGS;
        hdr.vxlan.reserved  = 0;
        hdr.vxlan.vni       = vni;
        hdr.vxlan.reserved2 = 0;

        // Build outer UDP header (source port is entropy hash; dst is VXLAN)
        hdr.udp.setValid();
        hdr.udp.srcPort  = 0xC000;  // arbitrary ephemeral port for ECMP entropy
        hdr.udp.dstPort  = VXLAN_UDP_PORT;
        hdr.udp.len      = 0;       // updated by egress recirculation or hardware
        hdr.udp.checksum = 0;       // UDP checksum optional for VXLAN (RFC 7348)

        // Build outer IPv4 header
        hdr.ipv4.setValid();
        hdr.ipv4.version        = 4;
        hdr.ipv4.ihl            = 5;
        hdr.ipv4.diffserv       = 0;
        hdr.ipv4.totalLen       = 0;   // filled by hardware/egress
        hdr.ipv4.identification = 0;
        hdr.ipv4.flags          = 0;
        hdr.ipv4.fragOffset     = 0;
        hdr.ipv4.ttl            = 64;
        hdr.ipv4.protocol       = IP_PROTO_UDP;
        hdr.ipv4.hdrChecksum    = 0;   // computed in MyComputeChecksum
        hdr.ipv4.srcAddr        = vtep_ip;
        hdr.ipv4.dstAddr        = hdr.inner_ipv4.dstAddr;  // route to inner dst

        // Outer Ethernet: retain original src/dst (ARP-resolved by control plane)
        hdr.ethernet.etherType = ETHERTYPE_IPV4;

        meta.tenant_id = vni;
    }

    // -----------------------------------------------------------------------
    // Tables
    // -----------------------------------------------------------------------

    // l2_forward: exact match on destination MAC → output port.
    // Control plane populates this table via P4Runtime or vendor SDK.
    table l2_forward {
        key = {
            hdr.ethernet.dstAddr: exact;
        }
        actions = {
            forward;
            drop;
        }
        size = 1024;
        default_action = drop();
    }

    // vxlan_tunnel_table: exact match on VXLAN VNI → decapsulate.
    // Each VNI corresponds to one tenant VXLAN segment.
    table vxlan_tunnel_table {
        key = {
            hdr.vxlan.vni: exact;
        }
        actions = {
            vxlan_decap;
            drop;
        }
        size = 256;
        default_action = drop();
    }

    // acl_table: ternary match on 5-tuple fields for security policy.
    // Supports wildcard rules (e.g., block all traffic from a /24 subnet).
    // Higher priority rules take precedence (largest priority value wins).
    table acl_table {
        key = {
            hdr.ipv4.srcAddr:  ternary;
            hdr.ipv4.dstAddr:  ternary;
            hdr.ipv4.protocol: exact;
            hdr.udp.dstPort:   ternary;
        }
        actions = {
            forward;
            drop;
        }
        size = 512;
        const default_action = forward(0);
    }

    // -----------------------------------------------------------------------
    // Apply block: pipeline logic
    // -----------------------------------------------------------------------
    apply {
        if (hdr.vxlan.isValid()) {
            // VXLAN path: first decapsulate, then forward inner frame
            vxlan_tunnel_table.apply();
            // After decap, forward based on inner (now outer) Ethernet dstAddr
            l2_forward.apply();
        } else {
            // Non-VXLAN path: apply ACL, then forward
            if (hdr.ipv4.isValid()) {
                acl_table.apply();
            }
            l2_forward.apply();
        }
    }
}

// ---------------------------------------------------------------------------
// Egress Control (pass-through for this reference implementation)
// ---------------------------------------------------------------------------

control MyEgress(
    inout headers             hdr,
    inout custom_metadata_t   meta,
    inout standard_metadata_t standard_metadata)
{
    apply {
        // Pass-through. Production implementations add:
        //   - MTU enforcement and packet truncation
        //   - DSCP remarking based on traffic class
        //   - Per-port egress policing
    }
}

// ---------------------------------------------------------------------------
// Checksum Computation
// ---------------------------------------------------------------------------

control MyComputeChecksum(
    inout headers           hdr,
    inout custom_metadata_t meta)
{
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.totalLen,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragOffset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr
            },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
        // Also recompute inner IPv4 checksum if present (after VXLAN decap path)
        update_checksum(
            hdr.inner_ipv4.isValid(),
            {
                hdr.inner_ipv4.version,
                hdr.inner_ipv4.ihl,
                hdr.inner_ipv4.diffserv,
                hdr.inner_ipv4.totalLen,
                hdr.inner_ipv4.identification,
                hdr.inner_ipv4.flags,
                hdr.inner_ipv4.fragOffset,
                hdr.inner_ipv4.ttl,
                hdr.inner_ipv4.protocol,
                hdr.inner_ipv4.srcAddr,
                hdr.inner_ipv4.dstAddr
            },
            hdr.inner_ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

// ---------------------------------------------------------------------------
// Deparser
// ---------------------------------------------------------------------------

control MyDeparser(
    packet_out  packet,
    in headers  hdr)
{
    apply {
        // Emit headers in wire order. setInvalid() headers are skipped.
        packet.emit(hdr.ethernet);        // always emitted
        packet.emit(hdr.ipv4);            // outer IPv4 (omitted after decap)
        packet.emit(hdr.udp);             // outer UDP  (omitted after decap)
        packet.emit(hdr.vxlan);           // VXLAN hdr  (omitted after decap)
        packet.emit(hdr.inner_ethernet);  // inner Eth  (present during encap)
        packet.emit(hdr.inner_ipv4);      // inner IPv4 (present during encap/decap)
    }
}

// ---------------------------------------------------------------------------
// Main package instantiation (v1model)
// ---------------------------------------------------------------------------

V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;
