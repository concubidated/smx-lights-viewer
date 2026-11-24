# Source this file: `source env.sh`
# Keeps the Python venv and all PlatformIO packages/toolchains inside the repo.
_SMX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export PLATFORMIO_CORE_DIR="$_SMX_ROOT/.pio-core"
# shellcheck disable=SC1091
source "$_SMX_ROOT/.venv/bin/activate"
