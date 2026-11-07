set -u

# Determine repo folder based on the location of this script (portable).
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-1}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export GDK_BACKEND=wayland

exec python3 "$REPO_DIR/kalibret.py" "$@"
