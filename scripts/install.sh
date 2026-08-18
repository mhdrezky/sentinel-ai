#!/usr/bin/env bash
#
# Install Sentinel-AI for the current user (zero admin required).
#
# Installs sentinel-ai from the latest GitHub release via `uv tool install --from git@tag`.
# Designed to be run via:
#   curl -fsSL https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.sh | bash
#
# Environment (for remote piping; parameters do not pass through the pipe):
#   SENTINEL_SOURCE  Path to a local directory containing pyproject.toml
#
# Or pass flags when invoking bash directly:
#   bash install.sh --source /path/to/sentinel-ai

set -euo pipefail

SOURCE="${SENTINEL_SOURCE:-}"

usage() {
    cat <<'EOF'
Usage: install.sh [--source PATH]

  --source PATH  Install from a local checkout instead of the latest release

Environment variable: SENTINEL_SOURCE
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE="${2:-}"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

CONFIG_DIR="${HOME}/.sentinel-ai"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
GITHUB_REPO="mhdrezky/sentinel-ai"
RELEASES_API="https://api.github.com/repos/${GITHUB_REPO}/releases/latest"

step() { printf ' ==> %s\n' "$1"; }
ok() { printf ' ok  %s\n' "$1"; }
warn() { printf ' !   %s\n' "$1"; }

ensure_path() {
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
}

get_latest_release_tag() {
    local release tag
    release=$(curl -fsSL -H "User-Agent: sentinel-ai-installer" "${RELEASES_API}") || {
        echo "Could not resolve latest release from GitHub API" >&2
        return 1
    }
    tag=$(printf '%s' "${release}" | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    if [[ -z "${tag}" ]]; then
        echo "GitHub releases/latest returned no tag_name" >&2
        return 1
    fi
    printf '%s' "${tag}"
}

write_fallback_config() {
    mkdir -p "${CONFIG_DIR}"
    cat >"${CONFIG_FILE}" <<'EOF'
# Sentinel-AI configuration
# Edit this file to point [ai].base_url and [ai].model to your AI server.

[policy]
block_at_or_above = "high"
block_on_install_scripts = true
block_on_nonregistry_source = true
allowlist = []
denylist = []

[ai]
enabled = true
base_url = "http://localhost:8000/v1"
model = "local-model"
timeout_seconds = 20.0
max_output_tokens = 2048
fail_open = true
enable_thinking = false

[trivy]
enabled = true
binary_path = "trivy"
timeout_seconds = 60.0
skip_db_update = false
offline = false
EOF
}

printf '\nSentinel-AI Installer\n\n'

ensure_path

# --- 1. Ensure uv ---
if ! command -v uv >/dev/null 2>&1; then
    step "uv not found — installing via Astral installer"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ensure_path
    if ! command -v uv >/dev/null 2>&1; then
        echo "uv install finished but executable is not on PATH. Start a new terminal and re-run." >&2
        exit 1
    fi
    ok "uv installed at $(command -v uv)"
fi

# --- 2. Install sentinel-ai ---
release_tag=""
if [[ -n "${SOURCE}" && -d "${SOURCE}" ]]; then
    step "Installing from local source: ${SOURCE}"
    resolved="$(cd "${SOURCE}" && pwd)"
    uv tool install --force --from "${resolved}" sentinel-ai
else
    release_tag="$(get_latest_release_tag)"
    step "Installing from git release ${release_tag}"
    uv tool install --force "git+https://github.com/${GITHUB_REPO}.git@${release_tag}"
fi

ensure_path
if ! command -v sentinel-ai >/dev/null 2>&1; then
    echo "sentinel-ai binary not found on PATH after install. Start a new terminal and re-run." >&2
    exit 1
fi
ok "sentinel-ai installed at $(command -v sentinel-ai)"

# --- 3. Create host config from package ---
if [[ ! -f "${CONFIG_FILE}" ]]; then
    step "Creating default config at ${CONFIG_FILE}"
    mkdir -p "${CONFIG_DIR}"

    bundled_toml=""
    if [[ -n "${SOURCE}" && -d "${SOURCE}" ]]; then
        candidate="${SOURCE%/}/src/sentinel_ai/sentinel.toml"
        if [[ -f "${candidate}" ]]; then
            bundled_toml="${candidate}"
        fi
    fi

    if [[ -n "${bundled_toml}" ]]; then
        cp "${bundled_toml}" "${CONFIG_FILE}"
        ok "Config copied from local source"
    else
        config_tag="${release_tag:-$(get_latest_release_tag)}"
        toml_url="https://raw.githubusercontent.com/${GITHUB_REPO}/${config_tag}/src/sentinel_ai/sentinel.toml"
        step "Fetching config from release ${config_tag}"
        if toml="$(curl -fsSL "${toml_url}")" && printf '%s' "${toml}" | grep -qE '^\s*\[policy\]'; then
            printf '%s\n' "${toml}" >"${CONFIG_FILE}"
            ok "Config fetched from ${toml_url}"
        else
            warn "Could not fetch sentinel.toml from ${toml_url}"
            step "Falling back to default bundled config"
            write_fallback_config
            ok "Default config created"
        fi
    fi
    warn "Edit ${CONFIG_FILE} with your AI server settings"
else
    ok "Config already exists at ${CONFIG_FILE}"
fi

printf '\nSentinel-AI installed successfully\n\n'
printf '  Verify:  sentinel-ai doctor\n'
printf '  Config:  sentinel-ai config\n\n'
