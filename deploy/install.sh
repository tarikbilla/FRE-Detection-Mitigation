#!/usr/bin/env bash
# Install / update the FRE collector on a Raspberry Pi (Kali / Raspberry Pi OS / Debian).
#
#   sudo deploy/install.sh [--apt] [--train] [--no-start]
#
#   --apt       install the Python dependencies from apt (simulated first; aborts if
#               apt would remove any package)
#   --train     train the ML models into /var/lib/fre-platform/models if none exist
#   --no-start  install and enable, but do not (re)start the service now
#
# Idempotent: safe to re-run after `git pull`. Never overwrites an existing
# /etc/fre-platform/fre.env (that file holds the MongoDB secret).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER="${SUDO_USER:-$(stat -c %U "$REPO_DIR")}"
SVC_GROUP="$(id -gn "$SVC_USER")"
ENV_DIR=/etc/fre-platform
ENV_FILE=$ENV_DIR/fre.env
UNIT=/etc/systemd/system/fre-collector.service
STATE_DIR=/var/lib/fre-platform
APT_PKGS=(python3-venv python3-numpy python3-scipy python3-pandas python3-sklearn python3-joblib
          python3-pymongo python3-dnspython python3-dotenv python3-pytest python3-mongomock)

DO_APT=0; DO_TRAIN=0; DO_START=1
for a in "$@"; do
  case "$a" in
    --apt) DO_APT=1 ;; --train) DO_TRAIN=1 ;; --no-start) DO_START=0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

if (( DO_APT )); then
  log "checking apt changes (simulation)"
  sim="$(apt-get -s install --no-upgrade "${APT_PKGS[@]}")"
  if grep -q '^Remv' <<<"$sim"; then
    echo "apt would REMOVE packages - aborting; resolve manually:" >&2
    grep '^Remv' <<<"$sim" >&2
    exit 1
  fi
  echo "$sim" | grep -E 'upgraded|newly installed' || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-upgrade "${APT_PKGS[@]}"
fi

# sudo's secure_path often omits /usr/local/bin (where source builds install); systemd's PATH includes it
if ! command -v airspyhf_rx >/dev/null && [[ ! -x /usr/local/bin/airspyhf_rx ]]; then
  echo "WARNING: airspyhf_rx not found - only simulated/replay sources will work"
fi

log "python environment ($REPO_DIR/.venv)"
sudo -u "$SVC_USER" bash -c "cd '$REPO_DIR' && { [ -d .venv ] || python3 -m venv --system-site-packages .venv; } \
  && .venv/bin/pip install -q --no-deps --no-build-isolation -e ."
sudo -u "$SVC_USER" "$REPO_DIR/.venv/bin/python" -c "import fre_platform, sklearn, pymongo, scipy, pandas; print('imports ok, fre_platform', fre_platform.__version__)"

log "configuration ($ENV_FILE)"
install -d -m 0750 -o root -g "$SVC_GROUP" "$ENV_DIR"
if [[ ! -f $ENV_FILE ]]; then
  # MongoDB URI commented out: the service buffers locally until it is set.
  sed -e 's|^FRE_MONGODB_URI=|#FRE_MONGODB_URI=|' -e "s|^FRE_STATION_ID=.*|FRE_STATION_ID=$(hostname)|" \
      "$REPO_DIR/.env.example" > "$ENV_FILE"
  echo "created $ENV_FILE from template - add the rotated FRE_MONGODB_URI there"
else
  echo "keeping existing $ENV_FILE"
fi
chown root:"$SVC_GROUP" "$ENV_FILE"; chmod 0640 "$ENV_FILE"

install -d -m 0750 -o "$SVC_USER" -g "$SVC_GROUP" "$STATE_DIR" "$STATE_DIR/models" /var/log/fre-platform

if (( DO_TRAIN )) && [[ ! -f $STATE_DIR/models/fre_models.joblib ]]; then
  log "training ML models (several minutes on a Pi 4)"
  sudo -u "$SVC_USER" env FRE_ENV_FILE="$ENV_FILE" FRE_MODEL_PATH="$STATE_DIR/models/fre_models.joblib" \
    "$REPO_DIR/.venv/bin/fre-platform" --log-format text train --blocks-per-scenario 30 \
    --report "$STATE_DIR/models/training_report.json" >/dev/null
  echo "model saved to $STATE_DIR/models/fre_models.joblib"
fi

log "systemd unit ($UNIT)"
sed -e "s|@REPO_DIR@|$REPO_DIR|g" -e "s|@USER@|$SVC_USER|g" -e "s|@GROUP@|$SVC_GROUP|g" \
    "$REPO_DIR/deploy/fre-collector.service" > "$UNIT"
systemd-analyze verify "$UNIT" 2>&1 | grep -v 'Unknown key' || true
systemctl daemon-reload
systemctl enable fre-collector.service
if (( DO_START )); then
  systemctl restart fre-collector.service
  sleep 5
  systemctl --no-pager --lines=5 status fre-collector.service || true
fi
log "done"
