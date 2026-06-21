/* SPDX-License-Identifier: Apache-2.0 */
/*
 * cortex_ref_ras.h — Public interface for ARM RAS Extension register accessors.
 *
 * Include this header to use the helpers implemented in cortex_ref_ras.c.
 * The struct and function declarations here match the definitions in that file.
 *
 * Reference: ARM Architecture Reference Manual, D17.2 RAS registers
 */
#ifndef _CORTEX_REF_RAS_H
#define _CORTEX_REF_RAS_H

#include <linux/types.h>

/**
 * struct ras_error_record - decoded contents of a single ARM RAS error record.
 *
 * Populated by cortex_ref_read_ras_record() or cortex_ref_parse_status().
 * All fields map directly to bit fields defined in ARM DDI 0487 D17.2.
 */
struct ras_error_record {
	u64  status;   /* ERRXSTATUS_EL1 — raw 64-bit status register value */
	u64  addr;     /* ERRXADDR_EL1   — raw 64-bit address register value */
	u64  misc;     /* ERRXMISC_EL1   — implementation defined */
	u8   serr;     /* status[7:0]    — error type code (SERR field) */
	u8   ierr;     /* status[15:8]   — implementation defined error code */
	bool valid;    /* status[63]     — ERR<n>STATUS.V: record contains valid error */
	bool ue;       /* status[29]     — ERR<n>STATUS.UE: uncorrected error */
	bool ce;       /* status[24:25]  — ERR<n>STATUS.CE: corrected error (nonzero) */
	bool of;       /* status[27]     — ERR<n>STATUS.OF: counter overflow */
	u64  erraddr;  /* ERRXADDR_EL1 physical address if ERRXSTATUS.AV is set */
};

/* Read one ARM RAS error record into *rec (arm64 only; stub on other archs). */
void cortex_ref_read_ras_record(int err_idx, struct ras_error_record *rec);

/* Write-1-to-clear ERRXSTATUS_EL1.V for the specified error record. */
void cortex_ref_clear_ras_record(int err_idx);

/* Decode raw ERRXSTATUS_EL1 bit fields into the named members of *rec. */
void cortex_ref_parse_status(u64 status, struct ras_error_record *rec);

/* Return a human-readable name for an SERR code (static string, do not free). */
const char *cortex_ref_serr_name(u8 serr);

#endif /* _CORTEX_REF_RAS_H */
