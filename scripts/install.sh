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

# --- 4. Install Trivy (optional; warn on failure) ---
TRIVY_BIN_DIR="${CONFIG_DIR}/bin"
TRIVY_BIN="${TRIVY_BIN_DIR}/trivy"
TRIVY_RELEASES_API="https://api.github.com/repos/aquasecurity/trivy/releases/latest"

get_latest_trivy_release() {
    local release tag version
    release=$(curl -fsSL -H "User-Agent: sentinel-ai-installer" "${TRIVY_RELEASES_API}") || return 1
    tag=$(printf '%s' "${release}" | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    if [[ -z "${tag}" ]]; then
        echo "GitHub trivy releases/latest returned no tag_name" >&2
        return 1
    fi
    version="${tag#v}"
    printf '%s %s\n' "${tag}" "${version}"
}

trivy_asset_name() {
    local version="$1"
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    case "${os}" in
        Linux)
            case "${arch}" in
                x86_64 | amd64) printf 'trivy_%s_Linux-64bit.tar.gz' "${version}" ;;
                aarch64 | arm64) printf 'trivy_%s_Linux-ARM64.tar.gz' "${version}" ;;
                *) return 1 ;;
            esac
            ;;
        Darwin)
            case "${arch}" in
                arm64) printf 'trivy_%s_macOS-ARM64.tar.gz' "${version}" ;;
                x86_64) printf 'trivy_%s_macOS-64bit.tar.gz' "${version}" ;;
                *) return 1 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

update_config_trivy_binary_path() {
    local binary_path="$1"
    local current sentinel_bin_prefix normalized_current
    [[ -f "${CONFIG_FILE}" ]] || return 0
    current=$(grep -E '^[[:space:]]*binary_path[[:space:]]*=' "${CONFIG_FILE}" | head -1 | sed -E 's/^[[:space:]]*binary_path[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/' || true)
    sentinel_bin_prefix="${CONFIG_DIR}/bin/"
    normalized_current="${current//\\//}"
    if [[ -n "${current}" && "${current}" != "trivy" && "${normalized_current}" != "${sentinel_bin_prefix}"* ]]; then
        return 0
    fi
    if sed --version >/dev/null 2>&1; then
        sed -i -E "s#^([[:space:]]*binary_path[[:space:]]*=[[:space:]]*)\"[^\"]*\"#\1\"${binary_path}\"#" "${CONFIG_FILE}"
    else
        sed -i '' -E "s#^([[:space:]]*binary_path[[:space:]]*=[[:space:]]*)\"[^\"]*\"#\1\"${binary_path}\"#" "${CONFIG_FILE}"
    fi
}

show_trivy_manual_install_hint() {
    warn "Install Trivy manually from https://github.com/aquasecurity/trivy/releases"
    warn "Then set [trivy].binary_path in ${CONFIG_FILE} (see README)"
}

trivy_installed=false
mkdir -p "${TRIVY_BIN_DIR}"

if [[ -x "${TRIVY_BIN}" ]] && "${TRIVY_BIN}" --version >/dev/null 2>&1; then
    ok "Trivy already installed at ${TRIVY_BIN}"
    trivy_installed=true
fi

if [[ "${trivy_installed}" != true ]]; then
    step "Installing latest Trivy to ${TRIVY_BIN_DIR}"
    tmp_dir=""
    tmp_archive=""
    if trivy_release="$(get_latest_trivy_release)"; then
        trivy_tag="${trivy_release%% *}"
        trivy_version="${trivy_release#* }"
        if asset="$(trivy_asset_name "${trivy_version}")"; then
            tmp_dir="$(mktemp -d)"
            tmp_archive="${tmp_dir}/trivy.${asset##*.}"
            url="https://github.com/aquasecurity/trivy/releases/download/${trivy_tag}/${asset}"
            if curl -fsSL "${url}" -o "${tmp_archive}"; then
                if [[ "${asset}" == *.tar.gz ]]; then
                    tar -xzf "${tmp_archive}" -C "${tmp_dir}"
                else
                    warn "Unsupported Trivy archive: ${asset}"
                    tmp_archive=""
                fi
                if [[ -f "${tmp_dir}/trivy" ]]; then
                    cp "${tmp_dir}/trivy" "${TRIVY_BIN}"
                    chmod +x "${TRIVY_BIN}"
                    if "${TRIVY_BIN}" --version >/dev/null 2>&1; then
                        ok "Trivy ${trivy_tag} installed at ${TRIVY_BIN}"
                        trivy_installed=true
                    fi
                fi
            fi
        else
            warn "Unsupported OS/arch for automatic Trivy install: $(uname -s)/$(uname -m)"
        fi
    fi
    if [[ "${trivy_installed}" != true ]]; then
        warn "Could not install Trivy automatically"
        show_trivy_manual_install_hint
    fi
    if [[ -n "${tmp_dir}" && -d "${tmp_dir}" ]]; then
        rm -rf "${tmp_dir}"
    fi
fi

if [[ "${trivy_installed}" == true ]]; then
    update_config_trivy_binary_path "${TRIVY_BIN}"
fi

printf '\nSentinel-AI installed successfully\n\n'
printf '  Verify:  sentinel-ai doctor\n'
printf '  Config:  sentinel-ai config\n\n'
