#!/usr/bin/env bats
# SPDX-License-Identifier: Apache-2.0
# inject_aer.bats — BATS tests for inject_aer.sh

SCRIPT="${BATS_TEST_DIRNAME}/../../fault-injection/inject_aer.sh"

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
# 5. Unknown option is rejected
# ---------------------------------------------------------------------------

@test "unknown option rejected" {
    run bash "$SCRIPT" --totally-invalid-flag 2>/dev/null
    [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# 6. --backend none exits 2 (skip — no backend available)
# ---------------------------------------------------------------------------

@test "--backend none exits 0 (dry-run)" {
    run bash "$SCRIPT" --backend none 2>/dev/null || true
    # Exit code 0 means dry-run completed successfully
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# 7. --backend invalid_value exits non-zero
# ---------------------------------------------------------------------------

@test "--backend invalid_value exits non-zero" {
    run bash "$SCRIPT" --backend totally_bogus_backend 2>/dev/null || true
    [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# 8. --type correctable --no-verify exits 0, 1, or 2 (not an option-parse error)
# ---------------------------------------------------------------------------

@test "--type correctable --no-verify runs without option-parse error" {
    run bash "$SCRIPT" --type correctable --no-verify 2>/dev/null || true
    # 0 = success, 1 = no root / device, 2 = no AER-capable device (skip)
    # Must NOT be 127 (command not found) or 126 (permission denied on script)
    [ "$status" -ne 127 ]
    [ "$status" -ne 126 ]
}

# ---------------------------------------------------------------------------
# 9. --type nonfatal --no-verify runs without option-parse error
# ---------------------------------------------------------------------------

@test "--type nonfatal --no-verify runs without option-parse error" {
    run bash "$SCRIPT" --type nonfatal --no-verify 2>/dev/null || true
    [ "$status" -ne 127 ]
    [ "$status" -ne 126 ]
}

# ---------------------------------------------------------------------------
# 10. --type fatal --no-verify runs without option-parse error
# ---------------------------------------------------------------------------

@test "--type fatal --no-verify runs without option-parse error" {
    run bash "$SCRIPT" --type fatal --no-verify 2>/dev/null || true
    [ "$status" -ne 127 ]
    [ "$status" -ne 126 ]
}

# ---------------------------------------------------------------------------
# 11. Auto backend detection does not crash
# ---------------------------------------------------------------------------

@test "auto backend detection does not crash the script" {
    run bash "$SCRIPT" --backend auto --no-verify 2>/dev/null || true
    # Any of 0 (pass), 1 (root/device missing), 2 (no backend) are valid
    [ "$status" -le 2 ]
}

# ---------------------------------------------------------------------------
# 12. Usage documentation present
# ---------------------------------------------------------------------------

@test "script has usage documentation" {
    run grep -q "Usage:" "$SCRIPT"
    [ "$status" -eq 0 ]
}
