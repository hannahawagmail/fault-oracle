#!/usr/bin/env bats
# SPDX-License-Identifier: Apache-2.0
# verify_edac_sysfs.bats — BATS tests for edac-reference/test/verify_edac_sysfs.sh

SCRIPT="${BATS_TEST_DIRNAME}/../../edac-reference/test/verify_edac_sysfs.sh"

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
# 4. Strict mode present
# ---------------------------------------------------------------------------

@test "script has set -e" {
    run grep -q "set -e" "$SCRIPT"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# 5. Without module loaded: exits non-zero or prints SKIP
# ---------------------------------------------------------------------------

@test "without edac module loaded exits non-zero or prints SKIP" {
    run bash "$SCRIPT" 2>/dev/null || true
    # Either the script exits non-zero (module missing) or outputs SKIP
    [ "$status" -ne 0 ] || [[ "$output" == *"SKIP"* ]] || [[ "$output" == *"skip"* ]]
}

# ---------------------------------------------------------------------------
# 6. --help or -h flag: exits 0 if supported
# ---------------------------------------------------------------------------

@test "help flag exits gracefully" {
    run bash "$SCRIPT" --help 2>/dev/null || true
    # If --help is supported, exit 0; if treated as unknown option, exit != 0
    # Either way must not crash (status < 128)
    [ "$status" -lt 128 ]
}

# ---------------------------------------------------------------------------
# 7. Script does not contain internal codenames or proprietary platform IDs
# ---------------------------------------------------------------------------

@test "script does not contain Amazon/AWS internal codenames" {
    # Forbidden patterns: internal platform codenames that would tie this
    # open-source reference script to proprietary infrastructure
    run bash -c "
        ! grep -qE 'K2[^a-zA-Z]|APCEA|Cordite[^a-zA-Z]|Carbon[^a-zA-Z]|Cinder[^a-zA-Z]|EC2-' '$SCRIPT'
    "
    [ "$status" -eq 0 ]
}
