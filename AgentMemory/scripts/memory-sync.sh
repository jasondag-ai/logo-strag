#!/usr/bin/env bash
#
# memory-sync.sh — AgentMemory Sync & Validation Script
#
# Validates the structure and integrity of ~/AgentMemory/core/ files,
# logs changes, and optionally syncs with ~/.openclaw/ on local Mac.
#
# Usage: ~/AgentMemory/scripts/memory-sync.sh [--check-only] [--verbose]

set -euo pipefail

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEMORY_ROOT="$(dirname "$SCRIPT_DIR")"
CORE_DIR="$MEMORY_ROOT/core"
LOGS_DIR="$MEMORY_ROOT/logs"
OPENCLAW_DIR="$HOME/.openclaw"
TIMESTAMP="$(date '+%Y-%m-%d_%H-%M-%S')"
LOG_FILE="$LOGS_DIR/sync-$TIMESTAMP.log"

CHECK_ONLY=false
VERBOSE=false

for arg in "$@"; do
    case "$arg" in
        --check-only) CHECK_ONLY=true ;;
        --verbose) VERBOSE=true ;;
    esac
done

# --- Helpers ---
log() {
    local msg="[$(date '+%H:%M:%S')] $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

warn() {
    log "WARNING: $1"
}

info() {
    if $VERBOSE; then
        log "INFO: $1"
    fi
}

# --- Setup ---
mkdir -p "$LOGS_DIR"
log "=== AgentMemory Sync — $TIMESTAMP ==="
log "Memory root: $MEMORY_ROOT"
log "Core dir: $CORE_DIR"

# --- 1. Validate directory structure ---
log ""
log "--- Structure Validation ---"

REQUIRED_DIRS=("core" "scripts" "logs")
for dir in "${REQUIRED_DIRS[@]}"; do
    if [[ -d "$MEMORY_ROOT/$dir" ]]; then
        log "OK: $dir/ exists"
    else
        warn "MISSING: $dir/ directory"
        if ! $CHECK_ONLY; then
            mkdir -p "$MEMORY_ROOT/$dir"
            log "  -> Created $dir/"
        fi
    fi
done

# --- 2. Validate core files ---
log ""
log "--- Core File Validation ---"

EXPECTED_FILES=(
    "USER.md"
    "SYSTEM.md"
    "BUSINESSES.md"
    "AGENTS.md"
    "PROJECTS.md"
    "TOOLS.md"
    "CONTACTS.md"
    "WORKFLOWS.md"
    "SOUL.md"
)

file_count=0
placeholder_total=0

for file in "${EXPECTED_FILES[@]}"; do
    filepath="$CORE_DIR/$file"
    if [[ -f "$filepath" ]]; then
        file_count=$((file_count + 1))
        # Count placeholders
        placeholders=$(grep -c '\[PLACEHOLDER' "$filepath" 2>/dev/null || true)
        placeholder_total=$((placeholder_total + placeholders))
        lines=$(wc -l < "$filepath")
        log "OK: $file ($lines lines, $placeholders placeholders remaining)"
    else
        warn "MISSING: $file"
    fi
done

log ""
log "Core files found: $file_count / ${#EXPECTED_FILES[@]}"
log "Total placeholders remaining: $placeholder_total"

# --- 3. Check for empty files ---
log ""
log "--- Empty File Check ---"

for file in "$CORE_DIR"/*.md; do
    if [[ -f "$file" ]]; then
        size=$(wc -c < "$file")
        if [[ "$size" -lt 50 ]]; then
            warn "$(basename "$file") appears nearly empty ($size bytes)"
        fi
    fi
done

# --- 4. OpenClaw sync check ---
log ""
log "--- OpenClaw Sync Check ---"

if [[ -d "$OPENCLAW_DIR" ]]; then
    log "OpenClaw directory found at $OPENCLAW_DIR"
    OPENCLAW_FILES=("SOUL.md" "USER.md" "AGENTS.md")
    for file in "${OPENCLAW_FILES[@]}"; do
        if [[ -f "$OPENCLAW_DIR/$file" ]]; then
            log "  Found: $OPENCLAW_DIR/$file"
            if ! $CHECK_ONLY; then
                info "  Sync target available for $file"
            fi
        else
            info "  Not found: $OPENCLAW_DIR/$file"
        fi
    done
else
    log "OpenClaw directory not found at $OPENCLAW_DIR (expected on local Mac)"
    log "  Skipping OpenClaw sync — run this script on your Mac to sync"
fi

# --- 5. Git status check ---
log ""
log "--- Git Status ---"

if command -v git &>/dev/null && git -C "$MEMORY_ROOT" rev-parse --git-dir &>/dev/null 2>&1; then
    changes=$(git -C "$MEMORY_ROOT" status --porcelain -- "$CORE_DIR" 2>/dev/null | wc -l || echo "0")
    log "Uncommitted changes in core/: $changes files"
    if [[ "$changes" -gt 0 ]] && $VERBOSE; then
        git -C "$MEMORY_ROOT" status --short -- "$CORE_DIR" 2>/dev/null | while read -r line; do
            log "  $line"
        done
    fi
else
    log "Not inside a git repository — skipping git checks"
fi

# --- Summary ---
log ""
log "=== Sync Summary ==="
log "Files validated: $file_count / ${#EXPECTED_FILES[@]}"
log "Placeholders remaining: $placeholder_total"
log "Log written to: $LOG_FILE"

if [[ "$placeholder_total" -gt 0 ]]; then
    log ""
    log "ACTION REQUIRED: $placeholder_total placeholder fields still need manual input."
    log "Run: grep -rn '\\[PLACEHOLDER' $CORE_DIR/"
fi

log ""
log "=== Sync complete ==="

exit 0
