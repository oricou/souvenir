#!/usr/bin/env bash
# Install the Souvenir server as a systemd user service: it starts now, at each
# login, and again if it crashes. No root needed (except for --linger).
#
#   tools/install_service.sh                  install and start
#   tools/install_service.sh -- --port 8080   same, passing options to server.py
#   tools/install_service.sh --password       ask a password for other devices (typed
#                                             here, kept in ~/.config/souvenir/env, mode 600)
#   tools/install_service.sh --no-password    remove that password
#   tools/install_service.sh --linger         also run when you are not logged in
#   tools/install_service.sh --dry-run        only print the service file
#   tools/install_service.sh --uninstall      stop and remove the service
#
# Afterwards:
#   systemctl --user status souvenir          is it running?
#   journalctl --user -u souvenir -f          its messages
#   systemctl --user restart souvenir         after changing server.py
set -euo pipefail

NAME=souvenir
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNIT=$UNIT_DIR/$NAME.service

action=install linger=0 password=keep server_args=()
ENV_FILE=${XDG_CONFIG_HOME:-$HOME/.config}/$NAME/env  # SOUVENIR_PASSWORD, read by the service
while (($#)); do
    case $1 in
        --uninstall) action=uninstall ;;
        --dry-run) action=dry-run ;;
        --linger) linger=1 ;;
        --password) password=ask ;;
        --no-password) password=remove ;;
        -h | --help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --) shift; server_args=("$@"); break ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

if [[ $action == uninstall ]]; then
    systemctl --user disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Removed $UNIT"
    exit 0
fi

PYTHON=$(command -v python3) || { echo "python3 not found" >&2; exit 1; }
for a in "${server_args[@]}"; do
    # Kept simple: systemd's quoting rules differ from the shell's.
    [[ $a =~ ^[A-Za-z0-9_./:=,+-]+$ ]] || { echo "unsupported character in server option: $a" >&2; exit 2; }
done

service="[Unit]
Description=Souvenir: private map of GPS traces and photos
After=network.target
# Give up after 3 failed starts in a minute (e.g. port already used) instead of looping.
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
WorkingDirectory=$ROOT
# Optional (the leading -): holds SOUVENIR_PASSWORD, kept out of this file and of ps.
EnvironmentFile=-$ENV_FILE
ExecStart=$PYTHON -u $ROOT/server.py${server_args[*]:+ ${server_args[*]}}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target"

if [[ $action == dry-run ]]; then
    echo "# would write $UNIT"
    echo "$service"
    exit 0
fi

systemctl --user show-environment >/dev/null 2>&1 || {
    echo "systemctl --user is not available in this session (no user systemd/dbus)." >&2
    exit 1
}

if [[ $password == ask ]]; then
    read -rsp "Password for other devices: " pw1; echo
    read -rsp "Same password again: " pw2; echo
    [[ -n $pw1 && $pw1 == "$pw2" ]] || { echo "Passwords empty or different, nothing changed." >&2; exit 1; }
    mkdir -p "$(dirname "$ENV_FILE")"
    umask 077
    pw1=${pw1//\\/\\\\}; pw1=${pw1//\"/\\\"}  # escape \ and " for systemd
    printf 'SOUVENIR_PASSWORD="%s"\n' "$pw1" >"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Password saved in $ENV_FILE (readable by you only)."
elif [[ $password == remove ]]; then
    rm -f "$ENV_FILE" && echo "Password removed: the site is open to the network again."
fi

mkdir -p "$UNIT_DIR"
echo "$service" >"$UNIT"
echo "Wrote $UNIT"
systemctl --user daemon-reload
systemctl --user enable "$NAME" >/dev/null
systemctl --user restart "$NAME" # restart: picks up a changed service file too

if ((linger)); then
    sudo loginctl enable-linger "$USER" && echo "Linger on: the server also runs when you are not logged in."
fi

# Check that the site answers (the first start may index photos for a few seconds).
port=8080
for ((i = 0; i < ${#server_args[@]}; i++)); do
    [[ ${server_args[i]} == --port ]] && port=${server_args[i + 1]:-8080}
    [[ ${server_args[i]} == --port=* ]] && port=${server_args[i]#--port=}
done
for _ in $(seq 60); do
    if curl -sf -o /dev/null "http://127.0.0.1:$port/api/maps"; then
        echo "Running: http://127.0.0.1:$port/"
        exit 0
    fi
    systemctl --user is-active --quiet "$NAME" || break
    sleep 1
done
echo "The service did not answer on port $port. Its last messages:" >&2
journalctl --user -u "$NAME" -n 20 --no-pager >&2 || true
exit 1
