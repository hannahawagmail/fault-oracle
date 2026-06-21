// SPDX-License-Identifier: Apache-2.0
/*
 * cortex_ref_ras.c — ARM RAS Extension register accessors
 *
 * Provides helpers to read ERRXSTATUS_EL1, ERRXADDR_EL1, ERRXMISC_EL1
 * via system register MRS instructions (arm64 only).
 *
 * Reference: ARM Architecture Reference Manual, D17.2 RAS registers
 *
 * The ARM RAS Extension (Reliability, Availability, Serviceability) provides
 * a standardised set of system registers to report hardware errors. Every
 * error record is indexed via ERRSELR_EL1 before reading the ERRx registers,
 * making the interface multiplexed but architecturally uniform.
 *
 * Register overview:
 *   ERRSELR_EL1  — selects which error record to read/write (write first)
 *   ERRXSTATUS_EL1 — status register for the selected record (V, UE, CE, OF, SERR, IERR)
 *   ERRXADDR_EL1   — physical address of the faulting location (if available)
 *   ERRXMISC_EL1   — implementation-defined miscellaneous information
 */

#include <linux/types.h>
#include <linux/printk.h>
#include <linux/module.h>
#include <asm/sysreg.h>

/*
 * Include our own header to ensure declarations and definitions stay in sync.
 * The struct ras_error_record definition lives in cortex_ref_ras.h.
 */
#include "cortex_ref_ras.h"

/*
 * ERRXSTATUS_EL1 bit definitions (ARM DDI 0487, D17.2.7)
 *
 * Bit 63   — V    Valid: the error record holds a captured error.
 * Bit 32   — AV   Address Valid: ERRXADDR_EL1 holds a valid physical address.
 * Bit 31   — OF   Overflow: one or more errors were lost (counter overflowed).
 * Bit 30   — ER   Error reported: the error was reported to higher-level software.
 * Bit 29   — UE   Uncorrected Error.
 * Bit 28   — DE   Deferred Error (async UE, not yet handled).
 * Bit 27   — OF   (alias at bit 27 on some implementations — use bit 31 for overflow)
 * Bits[25:24] — CE Corrected Error count saturating field (00=no CE, 01/10/11=CEs).
 * Bits[15:8]  — IERR Implementation defined error code.
 * Bits[7:0]   — SERR Architecturally defined error type.
 */
#define ERRXSTATUS_V    BIT_ULL(63)
#define ERRXSTATUS_AV   BIT_ULL(32)
#define ERRXSTATUS_OF   BIT_ULL(31)
#define ERRXSTATUS_UE   BIT_ULL(29)
#define ERRXSTATUS_CE_MASK  (0x3ULL << 24)
#define ERRXSTATUS_IERR_MASK (0xFFULL << 8)
#define ERRXSTATUS_SERR_MASK (0xFFULL)

/*
 * cortex_ref_parse_status — decode raw ERRXSTATUS_EL1 into struct fields.
 *
 * This function performs only bit manipulation and is fully testable without
 * real ARM RAS hardware or an arm64 kernel. Call it on a value read from
 * ERRXSTATUS_EL1, or on a synthetic value in unit tests.
 *
 * @status: raw 64-bit value from ERRXSTATUS_EL1
 * @rec:    output struct to populate; must not be NULL
 */
void cortex_ref_parse_status(u64 status, struct ras_error_record *rec)
{
	rec->status = status;
	rec->valid  = !!(status & ERRXSTATUS_V);
	rec->ue     = !!(status & ERRXSTATUS_UE);
	rec->ce     = !!((status & ERRXSTATUS_CE_MASK) >> 24);
	rec->of     = !!(status & ERRXSTATUS_OF);
	rec->serr   = (u8)(status & ERRXSTATUS_SERR_MASK);
	rec->ierr   = (u8)((status & ERRXSTATUS_IERR_MASK) >> 8);
}
EXPORT_SYMBOL_GPL(cortex_ref_parse_status);

/*
 * cortex_ref_serr_name — return a human-readable name for an SERR code.
 *
 * SERR codes are architecturally defined in ARM DDI 0487 D17.2.7, Table D17-3.
 * The list below covers the most common values. Codes not in this table are
 * returned as "Unknown".
 *
 * @serr: 8-bit SERR field from ERRXSTATUS_EL1[7:0]
 * Returns: pointer to a static string; caller must not free it.
 */
const char *cortex_ref_serr_name(u8 serr)
{
	switch (serr) {
	case 0x00: return "No error";
	case 0x02: return "ECC error on cache data RAM";
	case 0x06: return "ECC error on cache tag/dirty RAM";
	case 0x12: return "Bus error";
	case 0xFF: return "Unknown";
	default:   return "Unknown";
	}
}
EXPORT_SYMBOL_GPL(cortex_ref_serr_name);

#ifdef CONFIG_ARM64

/*
 * cortex_ref_read_ras_record — read one ARM RAS error record via MRS.
 *
 * Sequence (ARM DDI 0487, D17.2.2):
 *   1. Write the record index to ERRSELR_EL1  — selects which ERRx bank is visible.
 *   2. ISB — ensure the select takes effect before subsequent MRS reads.
 *   3. Read ERRXSTATUS_EL1, ERRXADDR_EL1, ERRXMISC_EL1 via MRS.
 *
 * ERRSELR_EL1:  System register that selects the active error record.
 *               Write the error record index (0..N-1) before reading ERRx regs.
 * ERRXSTATUS_EL1: Status of the selected error record.
 * ERRXADDR_EL1:   Physical address of the error, valid only if AV bit is set.
 * ERRXMISC_EL1:   Implementation-defined miscellaneous data.
 *
 * NOTE: On QEMU without KVM RAS support, the MRS instructions below will
 * take an Undefined Instruction exception. The poll-based inject_ce/inject_ue
 * path works on all QEMU configurations and does not require this function.
 *
 * @err_idx: index of the error record to read (0-based)
 * @rec:     output structure; status/addr/misc are set from raw register reads,
 *           then cortex_ref_parse_status() fills the decoded fields.
 */
void cortex_ref_read_ras_record(int err_idx, struct ras_error_record *rec)
{
	u64 status, addr, misc;

	if (!rec)
		return;

	/*
	 * Select the error record. ERRSELR_EL1 is a 64-bit system register;
	 * only bits[15:0] are architecturally significant (record index).
	 */
	asm volatile("msr errselr_el1, %0" : : "r"((u64)err_idx));

	/*
	 * ISB: instruction synchronisation barrier — ensures the ERRSELR_EL1
	 * write is visible to subsequent MRS instructions in program order.
	 */
	isb();

	/* Read the three RAS status registers for the selected record. */
	asm volatile("mrs %0, erxstatus_el1" : "=r"(status));
	asm volatile("mrs %0, erxaddr_el1"   : "=r"(addr));
	asm volatile("mrs %0, erxmisc0_el1"  : "=r"(misc));

	rec->addr = addr;
	rec->misc = misc;

	/* Decode the address only if ERRXSTATUS.AV (Address Valid) is set. */
	rec->erraddr = (status & ERRXSTATUS_AV) ? addr : 0ULL;

	/* Decode all status bit fields into the named struct members. */
	cortex_ref_parse_status(status, rec);

	pr_debug("cortex_ref_ras: record[%d] status=0x%016llx addr=0x%016llx misc=0x%016llx\n",
		 err_idx, status, addr, misc);
	pr_debug("cortex_ref_ras: record[%d] valid=%d ue=%d ce=%d of=%d serr=0x%02x (%s)\n",
		 err_idx, rec->valid, rec->ue, rec->ce, rec->of,
		 rec->serr, cortex_ref_serr_name(rec->serr));
}
EXPORT_SYMBOL_GPL(cortex_ref_read_ras_record);

/*
 * cortex_ref_clear_ras_record — clear the valid bit in a RAS error record.
 *
 * ARM RAS registers use write-1-to-clear semantics for the V (Valid) bit.
 * Writing ERRXSTATUS_EL1 with bit 63 set clears the valid flag and allows
 * the hardware to capture the next error into this record.
 *
 * Per ARM DDI 0487 D17.2.7: "Writing 1 to ERR<n>STATUS.V clears the record."
 *
 * @err_idx: index of the error record to clear (must match a previous read)
 */
void cortex_ref_clear_ras_record(int err_idx)
{
	/* Select the record — same sequence as the read path. */
	asm volatile("msr errselr_el1, %0" : : "r"((u64)err_idx));
	isb();

	/*
	 * Write V=1 to ERRXSTATUS_EL1. All other bits are RES0 or RAZ/WI
	 * when written from EL1 in normal world, so writing only the V bit
	 * is safe and portable across Cortex implementations.
	 */
	asm volatile("msr erxstatus_el1, %0" : : "r"(ERRXSTATUS_V));

	pr_debug("cortex_ref_ras: cleared RAS record[%d]\n", err_idx);
}
EXPORT_SYMBOL_GPL(cortex_ref_clear_ras_record);

#else /* !CONFIG_ARM64 */

/*
 * Stub implementations for non-arm64 builds.
 *
 * The RAS extension is arm64-only. These stubs allow the module to compile
 * on x86 build hosts and in cross-compilation environments where the kernel
 * target is not arm64.
 */

void cortex_ref_read_ras_record(int err_idx, struct ras_error_record *rec)
{
	if (rec) {
		rec->status  = 0;
		rec->addr    = 0;
		rec->misc    = 0;
		rec->serr    = 0;
		rec->ierr    = 0;
		rec->valid   = false;
		rec->ue      = false;
		rec->ce      = false;
		rec->of      = false;
		rec->erraddr = 0;
	}
	pr_debug("cortex_ref_ras: RAS register access not available (non-arm64 build)\n");
}
EXPORT_SYMBOL_GPL(cortex_ref_read_ras_record);

void cortex_ref_clear_ras_record(int err_idx)
{
	pr_debug("cortex_ref_ras: RAS register clear not available (non-arm64 build)\n");
}
EXPORT_SYMBOL_GPL(cortex_ref_clear_ras_record);

#endif /* CONFIG_ARM64 */
