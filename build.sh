#!/usr/bin/env bash
set -euo pipefail

INVOKE_DIR="$(pwd -P)"
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
cd "${ROOT_DIR}"

IMAGE_BUNDLE="${ROOT_DIR}/dist/project-z-bridge-runtime.image.tar.gz"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
ENV_FILE="${ROOT_DIR}/.env"
ENV_TEMPLATE="${ROOT_DIR}/env"
DEFAULT_RELEASE_REPO="JimDunphy/Project-Z-Bridge-Runtime-Evaluation"
IMAGE_ASSET="project-z-bridge-runtime.image.tar.gz"
CHECKSUM_ASSET="SHA256SUMS"
TOOLS_DIR="${ROOT_DIR}/tools"
AI_RUNNER_TOOL="${TOOLS_DIR}/ai_runner.py"
COMPAT_TRACE_TOOL="${TOOLS_DIR}/compat_trace_report.py"

usage() {
  cat <<'EOF'
Usage: ./build.sh <command> [args...]

Commands:
  init [--assets PATH] [--replace-assets] [--version X.Y.Z] [--skip-assets]
      Prepare local runtime dirs, load the bundled Docker image, and import ZWC
      static assets. By default, init downloads zimbra.war from the supported
      GitHub release and extracts it. Use --assets to import a local war,
      extracted directory, .zip, or .tar.gz instead.

  update-image [--release TAG|latest] [--repo OWNER/REPO] [--restart] [--no-git-pull]
      Update only the Project Z-Bridge runtime image from a private GitHub
      Release. By default this first runs `git pull --ff-only` when this
      directory is a git checkout, then re-runs the updated script.
      A bare release tag is also accepted, for example:
      ./build.sh update-image v0.1.1-eval

  start
      Start Project Z-Bridge in Docker.

  stop
      Stop/remove the runtime container.

  restart
      Restart the runtime container.

  logs | log
      Tail runtime logs.

  status
      Show Docker Compose service status.

  health
      Check http://127.0.0.1:${BRIDGE_PORT:-7777}/healthz.

  doctor
      Print local runtime diagnostics for support/debugging.

  ai-runner [args...]
      Run the host-side AI runner in the foreground.

  ai-runner-health
      Query the host-side AI runner health endpoint.

  compat-trace-show [--tail N] [--foreground|--all] [--hide-values] [--summary-only|--calls-only] [trace.jsonl]
      Render SOAP/REST compatibility trace for humans.

  compat-trace-follow [--all] [--from-start] [--hide-values] [trace.jsonl]
      Follow SOAP/REST compatibility trace as a live call feed.

  compat-trace-dump [--tail N] [--method NAME] [--curl] [--base-url URL] [trace.jsonl]
      Pretty-print full developer trace request/response blocks.

  compat-trace-clear [trace.jsonl]
      Truncate the compatibility trace file for a clean repro.

  compat-trace-redact [trace.jsonl]
      Write trace JSONL to stdout with private detail blocks removed.

  load-image
      Load dist/project-z-bridge-runtime.image.tar.gz into Docker.

  help
      Show this help.
EOF
}

require_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "Missing required file: ${path}" >&2
    exit 2
  fi
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required but was not found in PATH" >&2
    exit 2
  fi
}

version_ge() {
  local version="$1"
  local minimum="$2"
  local IFS=.
  local -a have need
  read -r -a have <<<"${version%%[-+]*}"
  read -r -a need <<<"${minimum%%[-+]*}"
  local i
  for ((i = 0; i < 3; i++)); do
    local h="${have[i]:-0}"
    local n="${need[i]:-0}"
    h="${h//[^0-9]/}"
    n="${n//[^0-9]/}"
    h="${h:-0}"
    n="${n:-0}"
    if ((10#${h} > 10#${n})); then
      return 0
    fi
    if ((10#${h} < 10#${n})); then
      return 1
    fi
  done
  return 0
}

docker_compose_version() {
  docker-compose version --short 2>/dev/null \
    || docker-compose version 2>/dev/null \
      | sed -n 's/^docker-compose version \([0-9][^, ]*\).*/\1/p' \
      | head -n 1
}

print_compose_version() {
  if docker compose version >/dev/null 2>&1; then
    docker compose version
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose version
  else
    echo "Docker Compose: not found"
  fi
}

compose_supported() {
  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi
  if docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    local version
    version="$(docker_compose_version)"
    [[ -n "${version}" ]] && version_ge "${version}" "1.29.0"
    return $?
  fi
  return 1
}

compose_command() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    echo "none"
  fi
}

require_compose() {
  require_docker
  if docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    local version
    version="$(docker_compose_version)"
    if [[ -n "${version}" ]] && version_ge "${version}" "1.29.0"; then
      return 0
    fi
    echo "docker-compose ${version:-unknown} is too old for this runtime package." >&2
    echo "Use Docker Compose v2, or standalone docker-compose 1.29.x or newer." >&2
    exit 2
  fi
  echo "Docker Compose is required but was not found." >&2
  echo "Use Docker Compose v2, or standalone docker-compose 1.29.x or newer." >&2
  exit 2
}

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "${name} is required but was not found in PATH" >&2
    exit 2
  fi
}

load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
  fi
  export BRIDGE_IMAGE="${BRIDGE_IMAGE:-project-z-bridge:runtime-eval}"
  export BRIDGE_PORT="${BRIDGE_PORT:-7777}"
}

require_python_tool() {
  local path="$1"
  require_command python3
  require_file "${path}"
}

runtime_bridge_running() {
  local container_id=""
  compose_supported || return 1
  [[ -f "${COMPOSE_FILE}" && -f "${ENV_FILE}" ]] || return 1
  container_id="$(compose_diagnostic ps -q bridge 2>/dev/null | head -n 1 || true)"
  [[ -n "${container_id}" ]] || return 1
  [[ "$(docker inspect -f '{{.State.Running}}' "${container_id}" 2>/dev/null || true)" == "true" ]]
}

ai_runner_runtime_defaults() {
  export BRIDGE_AI_RUNNER_PORT="${BRIDGE_AI_RUNNER_PORT:-8765}"
  export BRIDGE_AI_RUNNER_CONTEXT_DIR="${BRIDGE_AI_RUNNER_CONTEXT_DIR:-${ROOT_DIR}/data/ai-runner/context}"
  export BRIDGE_AI_RUNNER_COMPOSE_SESSION_DIR="${BRIDGE_AI_RUNNER_COMPOSE_SESSION_DIR:-${ROOT_DIR}/data/ai-runner/compose-sessions}"
}

ai_runner_health_url() {
  local bind="${BRIDGE_AI_RUNNER_BIND:-127.0.0.1}"
  local port="${BRIDGE_AI_RUNNER_PORT:-8765}"
  case "${bind}" in
    0.0.0.0|::|\[::\])
      bind="127.0.0.1"
      ;;
  esac
  printf 'http://%s:%s/health\n' "${bind}" "${port}"
}

compat_trace_host_path() {
  echo "${ROOT_DIR}/data/compat-trace.jsonl"
}

compat_trace_env_file_value() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || return 1
  awk -F= -v key="${key}" '
    /^[[:space:]]*#/ { next }
    $1 == key {
      value = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      print value
    }
  ' "${ENV_FILE}" | tail -n 1
}

compat_trace_to_host_path() {
  local value="$1"
  value="${value/#\~/${HOME}}"
  case "${value}" in
    /data)
      echo "${ROOT_DIR}/data"
      ;;
    /data/*)
      echo "${ROOT_DIR}/data/${value#/data/}"
      ;;
    /*)
      echo "${value}"
      ;;
    *)
      echo "${ROOT_DIR}/${value}"
      ;;
  esac
}

compat_trace_configured_host_path() {
  local value="${BRIDGE_COMPAT_TRACE_PATH:-}"
  if [[ -z "${value}" ]]; then
    value="$(compat_trace_env_file_value BRIDGE_COMPAT_TRACE_PATH || true)"
  fi
  [[ -n "${value}" ]] || return 1
  compat_trace_to_host_path "${value}"
}

compat_trace_args_have_path() {
  local skip_next=0
  local arg
  for arg in "$@"; do
    if [[ "${skip_next}" -eq 1 ]]; then
      skip_next=0
      continue
    fi
    case "${arg}" in
      --source|--tail|--header-every|--method|--base-url)
        skip_next=1
        ;;
      --*)
        ;;
      *)
        return 0
        ;;
    esac
  done
  return 1
}

compat_trace_show() {
  require_python_tool "${COMPAT_TRACE_TOOL}"
  load_env
  if compat_trace_args_have_path "$@"; then
    python3 "${COMPAT_TRACE_TOOL}" "$@"
    return
  fi

  local configured_path
  configured_path="$(compat_trace_configured_host_path || true)"
  if [[ -n "${configured_path}" && -f "${configured_path}" ]]; then
    python3 "${COMPAT_TRACE_TOOL}" --source "host file: ${configured_path}" "$@" "${configured_path}"
    return
  fi

  local host_path
  host_path="$(compat_trace_host_path)"
  if [[ -f "${host_path}" ]]; then
    python3 "${COMPAT_TRACE_TOOL}" --source "runtime host file: ${host_path}" "$@" "${host_path}"
    return
  fi

  if runtime_bridge_running; then
    if compose exec -T bridge sh -lc 'trace="${BRIDGE_COMPAT_TRACE_PATH:-}"; if [ -z "$trace" ]; then trace="${BRIDGE_DATA_DIR:-/data}/compat-trace.jsonl"; fi; cat "$trace" 2>/dev/null' \
      | python3 "${COMPAT_TRACE_TOOL}" --source "runtime container" "$@" -; then
      return
    fi
  fi

  echo "build.sh compat-trace-show: compatibility trace not found yet." >&2
  echo "Enable BRIDGE_COMPAT_TRACE_ENABLED=1, restart the bridge, exercise traffic, then retry." >&2
  exit 2
}

compat_trace_follow() {
  compat_trace_show --follow "$@"
}

compat_trace_dump() {
  compat_trace_show --dump-full --tail 0 "$@"
}

compat_trace_clear_path() {
  local path="$1"
  mkdir -p "$(dirname "${path}")"
  : >"${path}"
  echo "Cleared compatibility trace: ${path}"
}

compat_trace_clear() {
  load_env
  if [[ $# -gt 1 ]]; then
    echo "build.sh compat-trace-clear: expected zero or one trace path argument" >&2
    exit 2
  fi

  if [[ $# -eq 1 ]]; then
    local explicit_path="$1"
    explicit_path="${explicit_path/#\~/${HOME}}"
    if [[ "${explicit_path}" != /* ]]; then
      explicit_path="${INVOKE_DIR}/${explicit_path}"
    fi
    compat_trace_clear_path "${explicit_path}"
    return
  fi

  if runtime_bridge_running; then
    compose exec -T bridge sh -lc 'trace="${BRIDGE_COMPAT_TRACE_PATH:-}"; if [ -z "$trace" ]; then trace="${BRIDGE_DATA_DIR:-/data}/compat-trace.jsonl"; fi; mkdir -p "$(dirname "$trace")"; : > "$trace"; printf "Cleared compatibility trace: %s\n" "$trace"'
    return
  fi

  local configured_path
  configured_path="$(compat_trace_configured_host_path || true)"
  if [[ -n "${configured_path}" ]]; then
    compat_trace_clear_path "${configured_path}"
    return
  fi

  compat_trace_clear_path "$(compat_trace_host_path)"
}

compat_trace_redact() {
  require_python_tool "${COMPAT_TRACE_TOOL}"
  load_env
  if compat_trace_args_have_path "$@"; then
    python3 "${COMPAT_TRACE_TOOL}" --redact "$@"
    return
  fi

  local configured_path
  configured_path="$(compat_trace_configured_host_path || true)"
  if [[ -n "${configured_path}" && -f "${configured_path}" ]]; then
    python3 "${COMPAT_TRACE_TOOL}" --redact "${configured_path}"
    return
  fi

  local host_path
  host_path="$(compat_trace_host_path)"
  if [[ -f "${host_path}" ]]; then
    python3 "${COMPAT_TRACE_TOOL}" --redact "${host_path}"
    return
  fi

  if runtime_bridge_running; then
    if compose exec -T bridge sh -lc 'trace="${BRIDGE_COMPAT_TRACE_PATH:-}"; if [ -z "$trace" ]; then trace="${BRIDGE_DATA_DIR:-/data}/compat-trace.jsonl"; fi; cat "$trace" 2>/dev/null' \
      | python3 "${COMPAT_TRACE_TOOL}" --redact -; then
      return
    fi
  fi

  echo "build.sh compat-trace-redact: compatibility trace not found yet." >&2
  echo "Enable BRIDGE_COMPAT_TRACE_ENABLED=1, restart the bridge, exercise traffic, then retry." >&2
  exit 2
}

ensure_env_file() {
  if [[ -f "${ENV_FILE}" ]]; then
    return 0
  fi
  if [[ -f "${ENV_TEMPLATE}" ]]; then
    echo "Missing .env. Copying env to .env; edit it before start." >&2
    cp "${ENV_TEMPLATE}" "${ENV_FILE}"
  else
    echo "Missing .env. Copying .env.example to .env; edit it before start." >&2
    cp .env.example .env
  fi
}

compose() {
  require_compose
  require_file "${COMPOSE_FILE}"
  ensure_env_file
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"
  else
    docker-compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
  fi
}

compose_diagnostic() {
  require_file "${COMPOSE_FILE}"
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"
  else
    docker-compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
  fi
}

load_image() {
  require_docker
  load_env
  if docker image inspect "${BRIDGE_IMAGE}" >/dev/null 2>&1; then
    echo "Docker image already loaded: ${BRIDGE_IMAGE}"
    return 0
  fi
  require_file "${IMAGE_BUNDLE}"
  echo "Loading Docker image from ${IMAGE_BUNDLE}..."
  gzip -dc "${IMAGE_BUNDLE}" | docker load
  if ! docker image inspect "${BRIDGE_IMAGE}" >/dev/null 2>&1; then
    echo "Loaded image bundle, but expected tag is missing: ${BRIDGE_IMAGE}" >&2
    echo "Check BRIDGE_IMAGE in .env or retag the loaded image." >&2
    exit 2
  fi
}

maybe_git_pull_and_reexec() {
  local subcmd="$1"
  shift
  if ! git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi

  echo "Updating runtime checkout with: git pull --ff-only"
  git -C "${ROOT_DIR}" pull --ff-only
  exec "${ROOT_DIR}/build.sh" "${subcmd}" --no-git-pull "$@"
}

ensure_dirs() {
  mkdir -p "${ROOT_DIR}/dist" "${ROOT_DIR}/static/zimbra" "${ROOT_DIR}/data"
}

asset_dest_is_empty() {
  [[ -z "$(find "${ROOT_DIR}/static/zimbra" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]
}

clear_assets() {
  find "${ROOT_DIR}/static/zimbra" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${path}" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${path}" | awk '{print $1}'
  else
    return 1
  fi
}

download_webclient_war() {
  local war_version="$1"
  local release_repo="${BUILD_ZIMBRA_RELEASE_REPO:-JimDunphy/DockerZimbraRHEL8}"
  local release_asset="${BUILD_ZIMBRA_RELEASE_ASSET:-zimbra.war}"
  local release_api=""
  local release_label="latest"
  local download_url=""
  local release_json=""
  local release_tag=""
  local asset_digest=""
  local asset_found="0"
  local asset_file="${release_asset##*/}"
  local tmp_war=""
  local out_war="${ROOT_DIR}/dist/${asset_file}"
  local actual_sha=""
  local expected_sha=""

  require_command curl
  mkdir -p "${ROOT_DIR}/dist"

  if [[ -n "${war_version}" ]]; then
    if [[ ! "${war_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.][A-Za-z0-9_-]+)?$ && ! "${war_version}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.][A-Za-z0-9_-]+)?$ ]]; then
      echo "init: --version expects an exact release version such as 10.1.16" >&2
      exit 2
    fi
    release_tag="${war_version#v}"
    release_tag="v${release_tag}"
    release_api="https://api.github.com/repos/${release_repo}/releases/tags/${release_tag}"
    release_label="${release_tag}"
    download_url="https://github.com/${release_repo}/releases/download/${release_tag}/${release_asset}"
  else
    release_api="https://api.github.com/repos/${release_repo}/releases/latest"
    download_url="https://github.com/${release_repo}/releases/latest/download/${release_asset}"
  fi

  release_json="$(curl -fsSL "${release_api}")" || {
    echo "init: failed to fetch release metadata from ${release_api}" >&2
    exit 2
  }

  release_tag="$(printf '%s\n' "${release_json}" | sed -n 's/^[[:space:]]*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  asset_found="$(printf '%s\n' "${release_json}" | awk -v asset="${release_asset}" '
    $0 ~ /"name":[[:space:]]*"/ && index($0, "\"" asset "\"") > 0 { found = 1 }
    END { print found ? 1 : 0 }')"
  asset_digest="$(printf '%s\n' "${release_json}" | awk -v asset="${release_asset}" '
    $0 ~ /"name":[[:space:]]*"/ { wanted = index($0, "\"" asset "\"") > 0 }
    wanted && $0 ~ /"digest":[[:space:]]*"/ {
      line = $0
      sub(/^[[:space:]]*"digest":[[:space:]]*"/, "", line)
      sub(/",?$/, "", line)
      print line
      exit
    }')"

  if [[ "${asset_found}" != "1" ]]; then
    echo "init: release metadata did not contain asset '${release_asset}'" >&2
    exit 2
  fi

  echo "Downloading ${release_asset} from ${release_repo} (${release_tag:-${release_label}})" >&2
  tmp_war="$(mktemp "${ROOT_DIR}/dist/.${asset_file}.XXXXXX")"
  if ! curl -fL --retry 3 --retry-delay 2 -o "${tmp_war}" "${download_url}"; then
    rm -f "${tmp_war}"
    echo "init: download failed: ${download_url}" >&2
    exit 2
  fi

  if [[ "${asset_digest}" == sha256:* ]]; then
    if actual_sha="$(sha256_file "${tmp_war}")"; then
      expected_sha="${asset_digest#sha256:}"
      if [[ "${actual_sha}" != "${expected_sha}" ]]; then
        rm -f "${tmp_war}"
        echo "init: checksum mismatch for ${release_asset}" >&2
        echo "Expected: ${expected_sha}" >&2
        echo "Actual:   ${actual_sha}" >&2
        exit 2
      fi
      echo "Verified SHA-256: ${actual_sha}" >&2
    else
      echo "init: no sha256 tool found; skipping checksum verification" >&2
    fi
  fi

  mv -f "${tmp_war}" "${out_war}"
  echo "Wrote ${out_war}" >&2
  printf '%s\n' "${out_war}"
}

github_auth_token() {
  local token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
  if [[ -z "${token}" ]] && command -v gh >/dev/null 2>&1; then
    token="$(gh auth token 2>/dev/null || true)"
  fi
  printf '%s' "${token}"
}

fetch_release_json() {
  local repo="$1"
  local release="$2"
  local api=""
  local token=""

  if [[ -z "${release}" || "${release}" == "latest" ]]; then
    api="https://api.github.com/repos/${repo}/releases/latest"
  else
    api="https://api.github.com/repos/${repo}/releases/tags/${release}"
  fi

  token="$(github_auth_token)"
  if [[ -n "${token}" ]]; then
    curl -fsSL -H "Authorization: Bearer ${token}" -H "Accept: application/vnd.github+json" "${api}" && return 0
  else
    curl -fsSL -H "Accept: application/vnd.github+json" "${api}" && return 0
  fi
  {
    echo "update-image: failed to fetch GitHub release metadata: ${api}" >&2
    echo "For a private repo, GitHub Release asset downloads require GitHub API auth." >&2
    echo "SSH git access is enough for git pull, but not for the release API." >&2
    echo "Authenticate with GITHUB_TOKEN/GH_TOKEN or an existing gh auth login session." >&2
    return 1
  }
}

release_tag_from_json() {
  sed -n 's/^[[:space:]]*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

release_asset_id_from_json() {
  local asset="$1"
  awk -v asset="${asset}" '
    { lines[NR] = $0 }
    $0 ~ "\"name\"[[:space:]]*:[[:space:]]*\"" asset "\"" {
      for (i = NR; i >= NR - 40 && i > 0; i--) {
        if (lines[i] ~ /"id"[[:space:]]*:[[:space:]]*[0-9]+/) {
          line = lines[i]
          sub(/^.*"id"[[:space:]]*:[[:space:]]*/, "", line)
          sub(/[^0-9].*$/, "", line)
          print line
          exit
        }
      }
    }'
}

download_release_asset() {
  local repo="$1"
  local asset_id="$2"
  local dest="$3"
  local token=""

  token="$(github_auth_token)"
  if [[ -n "${token}" ]]; then
    curl -fL -H "Authorization: Bearer ${token}" -H "Accept: application/octet-stream" \
      "https://api.github.com/repos/${repo}/releases/assets/${asset_id}" \
      -o "${dest}"
  else
    curl -fL -H "Accept: application/octet-stream" \
      "https://api.github.com/repos/${repo}/releases/assets/${asset_id}" \
      -o "${dest}"
  fi
}

verify_downloaded_image() {
  local image_file="$1"
  local checksums_file="$2"
  local expected=""
  local actual=""

  expected="$(awk -v asset="${IMAGE_ASSET}" '$2 == asset || $2 == "dist/" asset { print $1; exit }' "${checksums_file}")"
  if [[ -z "${expected}" ]]; then
    echo "update-image: ${CHECKSUM_ASSET} does not contain ${IMAGE_ASSET}" >&2
    exit 2
  fi
  if ! actual="$(sha256_file "${image_file}")"; then
    echo "update-image: no sha256 tool found; cannot verify downloaded image" >&2
    exit 2
  fi
  if [[ "${actual}" != "${expected}" ]]; then
    echo "update-image: checksum mismatch for ${IMAGE_ASSET}" >&2
    echo "Expected: ${expected}" >&2
    echo "Actual:   ${actual}" >&2
    exit 2
  fi
  echo "Verified SHA-256: ${actual}"
}

load_image_bundle_force() {
  local loaded_output=""
  local loaded_ref=""

  require_file "${IMAGE_BUNDLE}"
  load_env
  echo "Loading Docker image from ${IMAGE_BUNDLE}..."
  loaded_output="$(gzip -dc "${IMAGE_BUNDLE}" | docker load)"
  printf '%s\n' "${loaded_output}"
  loaded_ref="$(printf '%s\n' "${loaded_output}" | sed -n 's/^Loaded image: //p' | tail -n 1)"
  if ! docker image inspect "${BRIDGE_IMAGE}" >/dev/null 2>&1; then
    if [[ -n "${loaded_ref}" ]] && docker image inspect "${loaded_ref}" >/dev/null 2>&1; then
      docker tag "${loaded_ref}" "${BRIDGE_IMAGE}"
      echo "Tagged ${loaded_ref} as ${BRIDGE_IMAGE}"
    else
      echo "update-image: loaded image, but expected tag is missing: ${BRIDGE_IMAGE}" >&2
      echo "Set BRIDGE_IMAGE in .env to a tag contained in the image bundle, or publish a bundle with that tag." >&2
      exit 2
    fi
  fi
}

update_image() {
  local release="$1"
  local repo="$2"
  local restart="$3"
  local tmpdir=""
  local release_json=""
  local release_tag=""
  local image_asset_id=""
  local checksum_asset_id=""
  local backup=""

  require_docker
  require_command curl
  load_env

  tmpdir="$(mktemp -d "${ROOT_DIR}/dist/.update-image.XXXXXX")"
  trap 'rm -rf "${tmpdir}"' RETURN

  release_json="$(fetch_release_json "${repo}" "${release}")"
  release_tag="$(printf '%s\n' "${release_json}" | release_tag_from_json)"
  image_asset_id="$(printf '%s\n' "${release_json}" | release_asset_id_from_json "${IMAGE_ASSET}")"
  checksum_asset_id="$(printf '%s\n' "${release_json}" | release_asset_id_from_json "${CHECKSUM_ASSET}")"

  if [[ -z "${image_asset_id}" ]]; then
    echo "update-image: release ${release_tag:-${release}} does not contain ${IMAGE_ASSET}" >&2
    exit 2
  fi
  if [[ -z "${checksum_asset_id}" ]]; then
    echo "update-image: release ${release_tag:-${release}} does not contain ${CHECKSUM_ASSET}" >&2
    exit 2
  fi

  echo "Downloading Project Z-Bridge runtime image from ${repo} (${release_tag:-${release}})"
  download_release_asset "${repo}" "${image_asset_id}" "${tmpdir}/${IMAGE_ASSET}"
  download_release_asset "${repo}" "${checksum_asset_id}" "${tmpdir}/${CHECKSUM_ASSET}"
  verify_downloaded_image "${tmpdir}/${IMAGE_ASSET}" "${tmpdir}/${CHECKSUM_ASSET}"

  mkdir -p "${ROOT_DIR}/dist/archive"
  if [[ -f "${IMAGE_BUNDLE}" ]]; then
    backup="${ROOT_DIR}/dist/archive/${IMAGE_ASSET}.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "${IMAGE_BUNDLE}" "${backup}"
    echo "Backed up previous image bundle: ${backup}"
  fi
  mv -f "${tmpdir}/${IMAGE_ASSET}" "${IMAGE_BUNDLE}"
  cp -f "${tmpdir}/${CHECKSUM_ASSET}" "${ROOT_DIR}/dist/${CHECKSUM_ASSET}.latest"

  load_image_bundle_force
  if [[ "${restart}" == "1" ]]; then
    compose up -d --force-recreate
  else
    echo "Image updated. Run './build.sh restart' when ready to restart the bridge."
  fi
}

import_assets() {
  local assets="$1"
  local replace="$2"
  local dest="${ROOT_DIR}/static/zimbra"

  if [[ -z "${assets}" ]]; then
    if asset_dest_is_empty; then
      echo "No ZWC assets provided. /zimbra/ will not work until assets are imported." >&2
      echo "Run: ./build.sh init, or ./build.sh init --assets /path/to/zimbra.war" >&2
    else
      echo "Existing ZWC assets found under static/zimbra."
    fi
    return 0
  fi

  if [[ ! -e "${assets}" ]]; then
    echo "Assets path does not exist: ${assets}" >&2
    exit 2
  fi

  if ! asset_dest_is_empty; then
    if [[ "${replace}" != "1" ]]; then
      echo "static/zimbra is not empty. Use --replace-assets to overwrite it." >&2
      exit 2
    fi
    clear_assets
  fi

  case "${assets}" in
    *.war|*.zip)
      if ! command -v unzip >/dev/null 2>&1; then
        echo "unzip is required to import ${assets}" >&2
        exit 2
      fi
      unzip -q "${assets}" -d "${dest}"
      ;;
    *.tar.gz|*.tgz)
      tar -xzf "${assets}" -C "${dest}"
      ;;
    *)
      if [[ -d "${assets}" ]]; then
        cp -a "${assets}/." "${dest}/"
      else
        echo "Unsupported assets input. Use zimbra.war, .zip, .tar.gz, or an extracted directory." >&2
        exit 2
      fi
      ;;
  esac

  if [[ ! -d "${dest}/WEB-INF" || ! -d "${dest}/js" ]]; then
    echo "Warning: imported assets do not look like extracted classic ZWC assets." >&2
    echo "Expected directories such as WEB-INF/ and js/ under static/zimbra." >&2
  fi
  echo "Imported ZWC assets into ${dest}"
}

cmd="${1:-help}"
shift || true

if [[ "${cmd}" == --* ]]; then
  cmd="${cmd#--}"
fi

case "${cmd}" in
  init)
    ensure_dirs
    ensure_env_file
    load_env
    assets=""
    replace="0"
    skip_assets="0"
    war_version="${BUILD_ZIMBRA_WAR_VERSION:-}"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --assets)
          if [[ $# -lt 2 || -z "${2:-}" ]]; then
            echo "init: --assets requires a path" >&2
            exit 2
          fi
          assets="${2:-}"
          shift 2
          ;;
        --assets=*)
          assets="${1#--assets=}"
          shift
          ;;
        --replace-assets)
          replace="1"
          shift
          ;;
        --skip-assets|--no-assets)
          skip_assets="1"
          shift
          ;;
        --version)
          if [[ $# -lt 2 || -z "${2:-}" ]]; then
            echo "init: --version requires a value" >&2
            exit 2
          fi
          war_version="$2"
          shift 2
          ;;
        --version=*)
          war_version="${1#--version=}"
          shift
          ;;
        *)
          echo "init: unknown argument: $1" >&2
          exit 2
          ;;
      esac
    done
    if [[ ! -f "${IMAGE_BUNDLE}" ]] && ! docker image inspect "${BRIDGE_IMAGE:-project-z-bridge:runtime-eval}" >/dev/null 2>&1; then
      echo "No local runtime image bundle found; downloading latest image release."
      update_image "latest" "${PROJECT_Z_BRIDGE_RELEASE_REPO:-${DEFAULT_RELEASE_REPO}}" "0"
    else
      load_image
    fi
    if [[ -z "${assets}" && "${skip_assets}" != "1" ]]; then
      if asset_dest_is_empty || [[ "${replace}" == "1" ]]; then
        assets="$(download_webclient_war "${war_version}")"
      fi
    fi
    import_assets "${assets}" "${replace}"
    echo "Init complete. Edit .env, then run: ./build.sh start"
    ;;
  load-image)
    load_image
    ;;
  update-image)
    original_update_arg_count=$#
    original_update_args=("$@")
    release="latest"
    repo="${PROJECT_Z_BRIDGE_RELEASE_REPO:-${DEFAULT_RELEASE_REPO}}"
    restart="0"
    no_git_pull="0"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --release)
          if [[ $# -lt 2 || -z "${2:-}" ]]; then
            echo "update-image: --release requires a tag or 'latest'" >&2
            exit 2
          fi
          release="$2"
          shift 2
          ;;
        --release=*)
          release="${1#--release=}"
          shift
          ;;
        --repo)
          if [[ $# -lt 2 || -z "${2:-}" ]]; then
            echo "update-image: --repo requires OWNER/REPO" >&2
            exit 2
          fi
          repo="$2"
          shift 2
          ;;
        --repo=*)
          repo="${1#--repo=}"
          shift
          ;;
        --restart)
          restart="1"
          shift
          ;;
        --no-git-pull)
          no_git_pull="1"
          shift
          ;;
        -*)
          echo "update-image: unknown argument: $1" >&2
          exit 2
          ;;
        *)
          if [[ "${release}" != "latest" ]]; then
            echo "update-image: multiple release tags were provided" >&2
            exit 2
          fi
          release="$1"
          shift
          ;;
      esac
    done
    if [[ "${no_git_pull}" != "1" ]]; then
      if (( original_update_arg_count > 0 )); then
        maybe_git_pull_and_reexec update-image "${original_update_args[@]}"
      else
        maybe_git_pull_and_reexec update-image
      fi
    fi
    ensure_dirs
    update_image "${release}" "${repo}" "${restart}"
    ;;
  start)
    ensure_dirs
    ensure_env_file
    load_image
    compose up -d
    echo "Started. Health: http://127.0.0.1:${BRIDGE_PORT}/healthz"
    echo "ZWC:    http://127.0.0.1:${BRIDGE_PORT}/zimbra/"
    ;;
  stop)
    load_env
    compose down
    ;;
  restart)
    load_env
    compose down
    load_image
    compose up -d
    ;;
  logs|log)
    load_env
    compose logs -f --tail=200
    ;;
  status)
    load_env
    compose ps
    ;;
  health)
    load_env
    url="http://127.0.0.1:${BRIDGE_PORT}/healthz"
    if command -v curl >/dev/null 2>&1; then
      curl -fsS "${url}"
      echo
    else
      echo "curl not found. Open: ${url}"
    fi
    ;;
  ai-runner)
    ensure_dirs
    load_env
    ai_runner_runtime_defaults
    require_python_tool "${AI_RUNNER_TOOL}"
    python3 "${AI_RUNNER_TOOL}" "$@"
    ;;
  ai-runner-health)
    load_env
    ai_runner_runtime_defaults
    url="$(ai_runner_health_url)"
    if command -v curl >/dev/null 2>&1; then
      curl -fsS "${url}"
      echo
    else
      require_command python3
      python3 - "${url}" <<'PY'
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as resp:
    print(resp.read().decode("utf-8"))
PY
    fi
    ;;
  compat-trace-show)
    compat_trace_show "$@"
    ;;
  compat-trace-follow)
    compat_trace_follow "$@"
    ;;
  compat-trace-dump)
    compat_trace_dump "$@"
    ;;
  compat-trace-clear)
    compat_trace_clear "$@"
    ;;
  compat-trace-redact)
    compat_trace_redact "$@"
    ;;
  doctor)
    load_env
    echo "Runtime:"
    echo "  Root: ${ROOT_DIR}"
    echo "  .env: $([[ -f "${ENV_FILE}" ]] && echo present || echo missing)"
    echo "  Compose file: $([[ -f "${COMPOSE_FILE}" ]] && echo present || echo missing)"
    echo
    echo "Configuration:"
    echo "  Image: ${BRIDGE_IMAGE}"
    echo "  Host port: ${BRIDGE_PORT}"
    echo "  Public base URL: ${BRIDGE_PUBLIC_BASE_URL:-unset}"
    echo "  Cookie secure: ${BRIDGE_COOKIE_SECURE:-unset}"
    if [[ "${BRIDGE_PUBLIC_BASE_URL:-}" == http://127.0.0.1:* && "${BRIDGE_COOKIE_SECURE:-}" == "1" ]]; then
      echo "  Warning: BRIDGE_COOKIE_SECURE=1 with localhost HTTP may be browser-dependent."
    fi
    echo
    echo "Docker:"
    docker --version || true
    print_compose_version || true
    echo "  Compose command: $(compose_command)"
    if compose_supported; then
      echo "  Compose support: ok"
    else
      echo "  Compose support: unsupported or missing"
      echo "  Required: Docker Compose v2, or standalone docker-compose 1.29.x or newer"
    fi
    echo
    echo "Image:"
    docker image inspect "${BRIDGE_IMAGE}" >/dev/null 2>&1 && echo "  Loaded: yes" || echo "  Loaded: no"
    if [[ -f "${IMAGE_BUNDLE}" ]]; then
      echo "  Bundle: $(ls -lh "${IMAGE_BUNDLE}" | awk '{print $5 " " $9}')"
    else
      echo "  Bundle: missing"
    fi
    echo
    echo "Runtime files:"
    if [[ -d "${ROOT_DIR}/data" ]]; then
      echo "  data/: present"
      [[ -w "${ROOT_DIR}/data" ]] && echo "  data/ writable: yes" || echo "  data/ writable: no"
    else
      echo "  data/: missing"
    fi
    if asset_dest_is_empty; then
      echo "  static/zimbra/: missing/empty"
    else
      echo "  static/zimbra/ entries: $(find static/zimbra -maxdepth 1 -mindepth 1 | wc -l)"
    fi
    echo "  AI runner tool: $([[ -f "${AI_RUNNER_TOOL}" ]] && echo present || echo missing)"
    echo "  Compat trace tool: $([[ -f "${COMPAT_TRACE_TOOL}" ]] && echo present || echo missing)"
    echo "  Compat trace path: $(compat_trace_configured_host_path || compat_trace_host_path)"
    echo
    echo "URLs:"
    echo "  Health: http://127.0.0.1:${BRIDGE_PORT}/healthz"
    echo "  ZWC:    http://127.0.0.1:${BRIDGE_PORT}/zimbra/"
    echo
    echo "Compose config:"
    if [[ ! -f "${ENV_FILE}" ]]; then
      echo "  skipped: .env is missing"
    elif ! compose_supported; then
      echo "  skipped: Compose is unsupported or missing"
    elif compose_diagnostic config >/tmp/project-z-bridge-compose-config.out 2>&1; then
      echo "  ok"
    else
      echo "  failed"
      sed -n '1,20p' /tmp/project-z-bridge-compose-config.out
    fi
    echo
    echo "Compose status:"
    if [[ ! -f "${ENV_FILE}" ]]; then
      echo "  skipped: .env is missing"
    elif ! compose_supported; then
      echo "  skipped: Compose is unsupported or missing"
    else
      compose_diagnostic ps || true
    fi
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: ${cmd}" >&2
    usage >&2
    exit 2
    ;;
esac
