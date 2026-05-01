#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-install}"
REPO_URL="${REPO_URL:-https://github.com/podcctv/VPS-traffic-monitor.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/vps-traffic-monitor-agent}"
SELF_PATH="${SELF_PATH:-/usr/local/bin/vtm-agent}"
CONFIG_PATH="${CONFIG_PATH:-/etc/vps-traffic-monitor/agent.env}"

usage() {
  cat <<USAGE
Usage:
  NODE_ID=... ENDPOINT=... API_KEY=... HMAC_SECRET=... [IFACE=eth0] [INTERVAL=120] bash agent-bootstrap.sh install
  NODE_ID=... ENDPOINT=... API_KEY=... HMAC_SECRET=... [IFACE=eth0] [INTERVAL=120] bash agent-bootstrap.sh deploy
  bash agent-bootstrap.sh upgrade
  bash agent-bootstrap.sh uninstall
  bash agent-bootstrap.sh remove
  bash agent-bootstrap.sh uninstall-all
  bash agent-bootstrap.sh remove-all
USAGE
}

choose_iface() {
  if [[ -n "${IFACE:-}" ]]; then
    return
  fi
  # one-liner install uses `curl ... | bash`, stdin is not a TTY in that case.
  # Prefer interactive selection from /dev/tty when available.
  local input_fd=0
  if [[ ! -t 0 ]]; then
    if [[ -r /dev/tty ]]; then
      input_fd=3
      exec 3</dev/tty
    else
      IFACE="all"
      return
    fi
  fi
  if [[ ! -t "$input_fd" ]]; then
    IFACE="all"
    return
  fi
  echo "选择要监控的网卡（默认: all=全部网卡）:"
  mapfile -t ifaces < <(ip -o link show | awk -F': ' '{print $2}' | awk -F'@' '{print $1}' | grep -E '^(eth|ens|enp|eno|bond|br|wg|tun)' || true)
  if [[ "${#ifaces[@]}" -eq 0 ]]; then
    read -r -u "$input_fd" -p "未检测到常见网卡，输入网卡名(留空=all): " picked
    IFACE="${picked:-all}"
    return
  fi
  echo "0) all (全部)"
  for i in "${!ifaces[@]}"; do
    echo "$((i+1))) ${ifaces[$i]}"
  done
  read -r -u "$input_fd" -p "请输入编号 [0]: " idx
  idx="${idx:-0}"
  if [[ "$idx" =~ ^[0-9]+$ ]] && (( idx >= 1 && idx <= ${#ifaces[@]} )); then
    IFACE="${ifaces[$((idx-1))]}"
  else
    IFACE="all"
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing command: $1" >&2
    exit 1
  fi
}

install_pkgs() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y git curl python3 vnstat
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git curl python3 vnstat
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git curl python3 vnstat
  else
    echo "unsupported package manager" >&2
    exit 1
  fi
}

sync_repo() {
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --all --prune
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
  else
    rm -rf "$INSTALL_DIR"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

write_config() {
  : "${NODE_ID:?NODE_ID is required}"
  : "${ENDPOINT:?ENDPOINT is required}"
  : "${API_KEY:?API_KEY is required}"
  : "${HMAC_SECRET:?HMAC_SECRET is required}"
  choose_iface
  IFACE="${IFACE:-all}"
  INTERVAL="${INTERVAL:-120}"

  install -d /etc/vps-traffic-monitor
  cat >"$CONFIG_PATH" <<CFG
ENDPOINT=$ENDPOINT
API_KEY=$API_KEY
HMAC_SECRET=$HMAC_SECRET
NODE_ID=$NODE_ID
IFACE=$IFACE
INTERVAL=$INTERVAL
CFG
}

install_service() {
  install -d /var/log/vps-traffic-monitor
  cat >/etc/systemd/system/vps-traffic-agent.service <<SERVICE
[Unit]
Description=VPS Traffic Monitor Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$CONFIG_PATH
ExecStart=/usr/bin/python3 $INSTALL_DIR/agent/traffic_agent.py \
  --endpoint \$ENDPOINT \
  --api-key \$API_KEY \
  --hmac-secret \$HMAC_SECRET \
  --node-id \$NODE_ID \
  --iface \$IFACE \
  --interval \$INTERVAL
Restart=always
RestartSec=10
StandardOutput=append:/var/log/vps-traffic-monitor/agent.log
StandardError=append:/var/log/vps-traffic-monitor/agent.log

[Install]
WantedBy=multi-user.target
SERVICE

  systemctl daemon-reload
  systemctl enable --now vnstat || true
  local monitor_ifaces
  monitor_ifaces="${IFACE:-all}"
  if [[ "$monitor_ifaces" == "all" ]]; then
    mapfile -t monitor_ifaces_list < <(ip -o link show | awk -F': ' '{print $2}' | awk -F'@' '{print $1}' | grep -E '^(eth|ens|enp|eno|bond|br|wg|tun)' || true)
    for iface in "${monitor_ifaces_list[@]:-}"; do
      [[ -n "$iface" ]] && vnstat --add -i "$iface" >/dev/null 2>&1 || true
    done
  else
    IFS=',' read -r -a monitor_ifaces_list <<<"$monitor_ifaces"
    for iface in "${monitor_ifaces_list[@]}"; do
      iface="${iface// /}"
      [[ -n "$iface" ]] && vnstat --add -i "$iface" >/dev/null 2>&1 || true
    done
  fi
  vnstat --reload >/dev/null 2>&1 || true
  systemctl enable --now vps-traffic-agent.service
}

detect_existing_agent() {
  local detected=0
  if [[ -d "$INSTALL_DIR" ]] || [[ -f "$CONFIG_PATH" ]] || systemctl list-unit-files | grep -q '^vps-traffic-agent.service'; then
    detected=1
  fi
  if [[ "$detected" -eq 1 ]]; then
    echo "[warn] detected existing vps-traffic-agent deployment on this node." >&2
    echo "[warn] install action will overwrite local agent files, config and systemd unit." >&2
  fi
}

refresh_self() {
  local source_script="$INSTALL_DIR/scripts/agent-bootstrap.sh"
  if [[ -f "$source_script" ]] && [[ -w "$(dirname "$SELF_PATH")" ]]; then
    install -m 0755 "$source_script" "$SELF_PATH"
    echo "self script refreshed: $SELF_PATH"
  fi
}

do_install() {
  detect_existing_agent
  install_pkgs
  sync_repo
  write_config
  install_service
  refresh_self
  echo "agent install done"
}

do_upgrade() {
  require_cmd git
  sync_repo
  install_service
  refresh_self
  echo "agent upgrade done"
}

do_uninstall() {
  systemctl disable --now vps-traffic-agent.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vps-traffic-agent.service
  systemctl daemon-reload
  rm -rf "$INSTALL_DIR"
  rm -rf /etc/vps-traffic-monitor
  rm -f /var/log/vps-traffic-monitor/agent.log
  echo "agent uninstall done (vnstat and its historical data are kept)"
}

do_uninstall_all() {
  do_uninstall
  systemctl disable --now vnstat >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vnstat.service /etc/systemd/system/vnstatd.service
  systemctl daemon-reload
  if command -v apt-get >/dev/null 2>&1; then
    apt-get purge -y vnstat >/dev/null 2>&1 || true
    apt-get autoremove -y >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf remove -y vnstat >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum remove -y vnstat >/dev/null 2>&1 || true
  fi
  rm -rf /var/lib/vnstat /var/log/vnstat /etc/vnstat.conf /etc/vnstat
  echo "agent uninstall-all done (vnstat package + all vnstat data removed)"
}

case "$ACTION" in
  install|deploy) do_install ;;
  upgrade) do_upgrade ;;
  uninstall|remove) do_uninstall ;;
  uninstall-all|remove-all) do_uninstall_all ;;
  -h|--help|help) usage ;;
  *) echo "unsupported action: $ACTION" >&2; usage; exit 1 ;;
esac
