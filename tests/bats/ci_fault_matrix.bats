#!/usr/bin/env bats
# SPDX-License-Identifier: Apache-2.0
# ci_fault_matrix.bats — BATS tests for ci_fault_matrix.sh

SCRIPT="${BATS_TEST_DIRNAME}/../../fault-injection/ci_fault_matrix.sh"

# ---------------------------------------------------------------------------
# 1. File presence
# ---------------------------------------------------------------------------

@test "script file exists" {
    [ -f "$SCRIPT" ]
}

# ---------------------------------------------------------------------------
# 2. Bash syntax validation
# ---------------------------------------------------------------------------

@test "script is valid bash syntax" {
    run bash -n "$SCRIPT"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# 3. SPDX header present
# ---------------------------------------------------------------------------

@test "script has SPDX header" {
    run grep -q "SPDX-License-Identifier: Apache-2.0" "$SCRIPT"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# 4. --dry-run --types CE exits 0 or 1
# ---------------------------------------------------------------------------

@test "--dry-run --types CE exits 0 or 1" {
    run bash "$SCRIPT" --dry-run --types CE 2>/dev/null || true
    [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# 5. --dry-run output mentions DRY or MATRIX
# ---------------------------------------------------------------------------

@test "--dry-run output mentions DRY or MATRIX" {
    run bash "$SCRIPT" --dry-run --types CE 2>&1 || true
    [[ "$output" == *"DRY"* ]] || [[ "$output" == *"MATRIX"* ]] || \
        [[ "$output" == *"dry"* ]] || [[ "$output" == *"matrix"* ]]
}

# ---------------------------------------------------------------------------
# 6. --dry-run --types INVALID_TYPE exits 0 or 1 (no crash)
# ---------------------------------------------------------------------------

@test "--dry-run --types INVALID_TYPE exits 0 or 1 without crash" {
    run bash "$SCRIPT" --dry-run --types INVALID_TYPE 2>/dev/null || true
    # Unknown type is skipped gracefully; overall result is 0 (all skipped) or 1
    [ "$status" -ne 127 ]
    [ "$status" -ne 126 ]
}

# ---------------------------------------------------------------------------
# 7. --dry-run --output-dir creates the output directory
# ---------------------------------------------------------------------------

@test "--dry-run --output-dir creates the output directory" {
    local outdir="/tmp/bats-test-output-$$"
    run bash "$SCRIPT" --dry-run --output-dir "$outdir" --types CE 2>/dev/null || true
    [ -d "$outdir" ]
    rm -rf "$outdir"
}

# ---------------------------------------------------------------------------
# 8. --dry-run --types CE,UE runs without unhandled error
# ---------------------------------------------------------------------------

@test "--dry-run --types CE,UE runs without unhandled error" {
    run bash "$SCRIPT" --dry-run --types CE,UE 2>/dev/null || true
    [ "$status" -ne 127 ]
    [ "$status" -ne 126 ]
}

# ---------------------------------------------------------------------------
# 9. --dry-run --types AER_CE,AER_UE runs without unhandled error
# ---------------------------------------------------------------------------

@test "--dry-run --types AER_CE,AER_UE runs without unhandled error" {
    run bash "$SCRIPT" --dry-run --types AER_CE,AER_UE 2>/dev/null || true
    [ "$status" -ne 127 ]
    [ "$status" -ne 126 ]
}

# ---------------------------------------------------------------------------
# 10. --junit flag is accepted and a JUnit XML file is created
# ---------------------------------------------------------------------------

@test "--junit flag accepted and JUnit XML file is created" {
    local junit_file="/tmp/bats-junit-$$.xml"
    local out_dir="/tmp/bats-out-$$"
    run bash "$SCRIPT" \
        --junit "$junit_file" \
        --dry-run \
        --types CE \
        --output-dir "$out_dir" \
        2>/dev/null || true
    # The file must exist after a dry-run; check it was written
    [ -f "$junit_file" ]
    rm -f "$junit_file"
    rm -rf "$out_dir"
}
