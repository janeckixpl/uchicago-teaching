#!/usr/bin/env bash
# setup_mac.sh — Bootstrap a new Mac for UChicago TA work.
# Safe to run multiple times (idempotent).
#
# Usage:
#   bash scripts/setup_mac.sh
#   bash <(curl -fsSL https://raw.githubusercontent.com/janeckixpl/uchicago-teaching/main/scripts/setup_mac.sh)

set -euo pipefail

REPO_URL="https://github.com/janeckixpl/uchicago-teaching.git"
REPO_DIR="$HOME/Desktop/teaching-claude"
CANVAS_TOKEN_FILE="$HOME/.uchicago_canvas_token"
ED_TOKEN_FILE="$HOME/.uchicago_ed_token"
CANVAS_BASE="https://canvas.uchicago.edu/api/v1"

# ── Terminal colours ──────────────────────────────────────────────────────────
GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; BLD='\033[1m'; RST='\033[0m'
ok()   { echo -e "${GRN}✓${RST}  $*"; }
warn() { echo -e "${YLW}⚠${RST}  $*"; }
info() { echo -e "   $*"; }
fail() { echo -e "${RED}✗${RST}  $*"; }
hdr()  { echo -e "\n${BLD}── $* ──${RST}"; }

# ── Result tracking ───────────────────────────────────────────────────────────
READY=(); ATTENTION=()
mark_ready() { READY+=("$1"); }
mark_attn()  { ATTENTION+=("$1"); }

# ── Helpers ───────────────────────────────────────────────────────────────────

# Read a secret interactively; skip if file exists and user declines update.
# Usage: read_token "Label" /path/to/file
read_token() {
    local label="$1" file="$2"
    if [[ -s "$file" ]]; then
        echo -n "   $label already saved. Update it? [y/N] "
        read -r ans
        if [[ ! "$ans" =~ ^[Yy]$ ]]; then
            ok "$label already configured"
            return 0
        fi
    fi
    local token=""
    while [[ -z "$token" ]]; do
        echo -n "   Paste $label (input hidden): "
        read -rs token; echo
    done
    printf '%s' "$token" > "$file"
    chmod 600 "$file"
    ok "$label saved to $file"
}

# ── Step 1: Clone or update repo ──────────────────────────────────────────────
hdr "Step 1 — Repository"
if [[ -d "$REPO_DIR/.git" ]]; then
    ok "Repo already at $REPO_DIR"
    info "Pulling latest…"
    git -C "$REPO_DIR" pull --ff-only 2>&1 | sed 's/^/   /'
    mark_ready "Repo up-to-date"
else
    info "Cloning $REPO_URL → $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR"
    ok "Repo cloned"
    mark_ready "Repo cloned"
fi

# ── Step 2: Canvas API token ──────────────────────────────────────────────────
hdr "Step 2 — Canvas API token"
read_token "Canvas API token" "$CANVAS_TOKEN_FILE"

# ── Step 3: Ed Discussion token ───────────────────────────────────────────────
hdr "Step 3 — Ed Discussion token"
read_token "Ed Discussion token" "$ED_TOKEN_FILE"

# ── Step 4: Claude Code ───────────────────────────────────────────────────────
hdr "Step 4 — Claude Code"
if command -v claude &>/dev/null; then
    ver=$(claude --version 2>/dev/null || echo "unknown")
    ok "Claude Code already installed ($ver)"
    mark_ready "Claude Code $ver"
else
    if ! command -v npm &>/dev/null; then
        warn "npm not found — install Node.js from https://nodejs.org then re-run this script"
        mark_attn "Claude Code: install Node.js first, then re-run"
    else
        info "Installing Claude Code via npm…"
        npm install -g @anthropic-ai/claude-code 2>&1 | tail -5 | sed 's/^/   /'
        if command -v claude &>/dev/null; then
            ok "Claude Code installed"
            mark_ready "Claude Code installed"
        else
            fail "Claude Code install may have failed — check npm output above"
            mark_attn "Claude Code: verify installation manually"
        fi
    fi
fi

# ── Step 5: System dependencies ───────────────────────────────────────────────
hdr "Step 5 — System dependencies (python3, git, pip3)"
all_sys_ok=true
for cmd in python3 git pip3; do
    if command -v "$cmd" &>/dev/null; then
        ok "$cmd → $(command -v "$cmd")"
    else
        fail "$cmd not found"
        mark_attn "$cmd missing — install via Homebrew: brew install $cmd"
        all_sys_ok=false
    fi
done
$all_sys_ok && mark_ready "python3 / git / pip3 present"

# ── Step 6: Python packages ───────────────────────────────────────────────────
hdr "Step 6 — Python packages (requests, boto3, anthropic)"
if command -v pip3 &>/dev/null; then
    # Install quietly; pip skips packages that are already at the required version
    if pip3 install -q --upgrade requests boto3 anthropic 2>&1 | grep -v "^$" | sed 's/^/   /'; then
        ok "requests, boto3, anthropic installed/up-to-date"
        mark_ready "Python packages installed"
    else
        fail "pip3 install failed — check output above"
        mark_attn "Python packages: pip3 install requests boto3 anthropic"
    fi
else
    warn "pip3 not available — skipping Python package install"
    mark_attn "Python packages: install pip3 first, then: pip3 install requests boto3 anthropic"
fi

# ── Step 7: SSH key reminder ──────────────────────────────────────────────────
hdr "Step 7 — EC2 SSH key"
SSH_KEY="$HOME/.ssh/uchicago-ta-bot-key.pem"
if [[ -f "$SSH_KEY" ]]; then
    ok "EC2 key already at $SSH_KEY"
    chmod 600 "$SSH_KEY"
    mark_ready "EC2 SSH key present"
else
    warn "EC2 key not found at $SSH_KEY"
    info "AirDrop it from the Mac that has it:"
    info "  1. On the source Mac: open Finder → ~/.ssh → right-click uchicago-ta-bot-key.pem → Share → AirDrop"
    info "  2. Accept on this Mac → move to ~/.ssh/ → chmod 600 ~/.ssh/uchicago-ta-bot-key.pem"
    mark_attn "EC2 SSH key: AirDrop uchicago-ta-bot-key.pem from other Mac → ~/.ssh/"
fi

# ── Step 8: Canvas API smoke test ─────────────────────────────────────────────
hdr "Step 8 — Canvas API smoke test"
if [[ -s "$CANVAS_TOKEN_FILE" ]]; then
    canvas_token=$(cat "$CANVAS_TOKEN_FILE")
    http_status=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $canvas_token" \
        "$CANVAS_BASE/courses?per_page=1&enrollment_type=teacher")
    unset canvas_token
    if [[ "$http_status" == "200" ]]; then
        ok "Canvas API responded 200 — token is valid"
        mark_ready "Canvas API token verified"
    elif [[ "$http_status" == "401" ]]; then
        fail "Canvas API returned 401 — token is invalid or expired"
        mark_attn "Canvas token: get a fresh one from canvas.uchicago.edu → Account → Settings → New Access Token"
    else
        warn "Canvas API returned HTTP $http_status — check network/VPN"
        mark_attn "Canvas API: unexpected status $http_status — check network/VPN"
    fi
else
    warn "Canvas token file empty — skipping API test"
    mark_attn "Canvas API: provide token in Step 2 to enable smoke test"
fi

# ── Step 9: Summary ───────────────────────────────────────────────────────────
echo -e "\n${BLD}════════════════════════════════════════${RST}"
echo -e "${BLD} Setup Summary${RST}"
echo -e "${BLD}════════════════════════════════════════${RST}"

if [[ ${#READY[@]} -gt 0 ]]; then
    echo -e "\n${GRN}Ready:${RST}"
    for item in "${READY[@]}"; do echo -e "  ${GRN}✓${RST}  $item"; done
fi

if [[ ${#ATTENTION[@]} -gt 0 ]]; then
    echo -e "\n${YLW}Needs attention:${RST}"
    for item in "${ATTENTION[@]}"; do echo -e "  ${YLW}⚠${RST}  $item"; done
fi

echo ""
if [[ ${#ATTENTION[@]} -eq 0 ]]; then
    echo -e "${GRN}${BLD}All good — you're fully operational.${RST}"
else
    echo -e "${YLW}${BLD}Fix the items above, then re-run this script to verify.${RST}"
fi

echo ""
echo -e "${BLD}Useful commands:${RST}"
echo "  cd ~/Desktop/teaching-claude"
echo "  python scripts/grade_submissions.py --help"
echo "  python scripts/ed_draft.py --help"
echo "  python scripts/publish_to_canvas.py --help"
echo "  python scripts/new_assignment.py --help"
