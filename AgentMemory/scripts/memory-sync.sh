#!/usr/bin/env bash
# memory-sync.sh — Agent Memory System Sync Script
# Syncs the AgentMemory repo and validates core files
#
# Usage: ./memory-sync.sh [--check-only] [--verbose]
#
# Last Updated: 2026-04-08

set -euo pipefail

# Configuration
MEMORY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CORE_DIR="${MEMORY_DIR}/core"
LOG_DIR="${MEMORY_DIR}/logs"
LOG_FILE="${LOG_DIR}/sync-$(date +%Y%m%d-%H%M%S).log"
REQUIRED_FILES=("USER.md" "SYSTEM.md" "BUSINESSES.md" "AGENTS.md" "TOOLS.md" "COMMUNICATIONS.md" "SOUL.md")

# Flags
CHECK_ONLY=false
VERBOSE=false

for arg in "$@"; do
    case $arg in
        --check-only) CHECK_ONLY=true ;;
        --verbose) VERBOSE=true ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# Logging
mkdir -p "$LOG_DIR"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg" >> "$LOG_FILE"
    if $VERBOSE; then
        echo "$msg"
    fi
}

info() {
    echo "$1"
    log "INFO: $1"
}

warn() {
    echo "WARNING: $1" >&2
    log "WARN: $1"
}

error() {
    echo "ERROR: $1" >&2
    log "ERROR: $1"
}

# Header
echo "========================================="
echo "  Agent Memory System — Sync & Validate"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================="
echo ""
log "Sync started"

# Step 1: Validate directory structure
info "Checking directory structure..."

DIRS_OK=true
for dir in core scripts logs templates; do
    if [ -d "${MEMORY_DIR}/${dir}" ]; then
        info "  [OK] ${dir}/"
    else
        warn "  [MISSING] ${dir}/ — creating..."
        if ! $CHECK_ONLY; then
            mkdir -p "${MEMORY_DIR}/${dir}"
            info "  [CREATED] ${dir}/"
        fi
        DIRS_OK=false
    fi
done
echo ""

# Step 2: Validate core files exist
info "Checking core memory files..."

FILES_OK=true
MISSING_FILES=()
for file in "${REQUIRED_FILES[@]}"; do
    if [ -f "${CORE_DIR}/${file}" ]; then
        SIZE=$(wc -c < "${CORE_DIR}/${file}" | tr -d ' ')
        info "  [OK] ${file} (${SIZE} bytes)"
    else
        warn "  [MISSING] ${file}"
        MISSING_FILES+=("$file")
        FILES_OK=false
    fi
done
echo ""

# Step 3: Check for placeholder fields
info "Scanning for unfilled placeholders..."

TOTAL_PLACEHOLDERS=0
for file in "${CORE_DIR}"/*.md; do
    if [ -f "$file" ]; then
        BASENAME=$(basename "$file")
        COUNT=$(grep -c '\[PLACEHOLDER' "$file" 2>/dev/null || true)
        if [ "$COUNT" -gt 0 ]; then
            info "  ${BASENAME}: ${COUNT} placeholder(s) remaining"
            TOTAL_PLACEHOLDERS=$((TOTAL_PLACEHOLDERS + COUNT))
        else
            info "  ${BASENAME}: fully populated"
        fi
    fi
done
echo ""

# Step 4: Git sync (if in a git repo and not check-only)
if [ -d "${MEMORY_DIR}/../.git" ] || [ -d "${MEMORY_DIR}/.git" ]; then
    info "Git repository detected."

    if $CHECK_ONLY; then
        info "  [CHECK-ONLY] Skipping git operations."
    else
        # Check for changes
        cd "${MEMORY_DIR}/.." 2>/dev/null || cd "${MEMORY_DIR}"
        CHANGES=$(git status --porcelain AgentMemory/ 2>/dev/null || true)
        if [ -n "$CHANGES" ]; then
            info "  Changes detected in AgentMemory/:"
            echo "$CHANGES" | while read -r line; do
                info "    $line"
            done
        else
            info "  No changes to sync."
        fi
    fi
else
    info "Not a git repository — skipping git sync."
fi
echo ""

# Step 5: Summary
echo "========================================="
echo "  Sync Summary"
echo "========================================="
echo ""
info "Directory structure: $(if $DIRS_OK; then echo 'OK'; else echo 'REPAIRED'; fi)"
info "Core files: ${#REQUIRED_FILES[@]} expected, $((${#REQUIRED_FILES[@]} - ${#MISSING_FILES[@]})) found"

if [ ${#MISSING_FILES[@]} -gt 0 ]; then
    warn "Missing files: ${MISSING_FILES[*]}"
fi

info "Placeholders remaining: ${TOTAL_PLACEHOLDERS}"
info "Log written to: ${LOG_FILE}"
echo ""

if [ "$TOTAL_PLACEHOLDERS" -gt 0 ]; then
    echo "Action needed: ${TOTAL_PLACEHOLDERS} placeholder fields still need real data."
    echo "Run with --verbose to see details, or review core/ files directly."
fi

log "Sync completed"
echo ""
echo "Done."
