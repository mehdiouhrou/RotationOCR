#!/usr/bin/env bash
set -euo pipefail

APP_USER_DEFAULT="www-data"
APP_DIR_DEFAULT="/opt/ocr_app"

usage() {
  cat <<'USAGE'
Usage:
  sudo bash scripts/install_server.sh --app-dir /opt/ocr_app [--user www-data] [--with-systemd] [--with-nginx]

Options:
  --app-dir PATH Répertoire d'installation du projet (défaut: /opt/ocr_app)
  --user USER           Utilisateur Linux qui exécute l'app (défaut: www-data)
  --with-systemd        Installe et active les unités systemd (ocr_app + ocr_worker)
  --with-nginx          Installe nginx + copie une config reverse-proxy (HTTP) vers sites-available
  -h, --help            Aide

Notes:
 - Ce script est pensé pour Ubuntu Server LTS (22.04/24.04).
  - Le code du projet doit déjà être présent dans --app-dir (git clone / rsync / scp).
  - Le script ne génère pas de secrets: éditez /opt/ocr_app/.env après installation.
USAGE
}

APP_DIR="${APP_DIR_DEFAULT}"
RUN_USER="${APP_USER_DEFAULT}"
WITH_SYSTEMD="0"
WITH_NGINX="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      APP_DIR="${2:-}"
      shift 2
      ;;
    --user)
      RUN_USER="${2:-}"
      shift 2
      ;;
    --with-systemd)
      WITH_SYSTEMD="1"
      shift 1
      ;;
    --with-nginx)
      WITH_NGINX="1"
      shift 1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Argument inconnu: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Lance ce script en root (sudo)." >&2
  exit 1
fi

if [[ -z "${APP_DIR}" || -z "${RUN_USER}" ]]; then
  echo "--app-dir et --user ne peuvent pas être vides." >&2
  exit 2
fi

if [[ ! -d "${APP_DIR}" ]]; then
  echo "Répertoire introuvable: ${APP_DIR}" >&2
  echo "Copie d'abord le projet à cet emplacement, puis relance." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update -y
PACKAGES=(
  ca-certificates
  curl
  git
  python3
  python3-venv
  python3-pip
  tesseract-ocr
  tesseract-ocr-fra
  tesseract-ocr-ara
  tesseract-ocr-eng
  poppler-utils
  ghostscript
)

if [[ "${WITH_NGINX}" == "1" ]]; then
  PACKAGES+=(nginx)
fi

apt-get install -y "${PACKAGES[@]}"

PYTHON_BIN="$(command -v python3)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 introuvable après installation." >&2
  exit 1
fi

VENV_DIR="${APP_DIR}/.venv"
if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${APP_DIR}/requirements.txt"

mkdir -p \
  "${APP_DIR}/uploads" \
  "${APP_DIR}/outputs" \
  "${APP_DIR}/fichiers_traites"

if [[ -f "${APP_DIR}/.env.example" && ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env" || true
fi

if [[ -f "${APP_DIR}/users.example.json" && ! -f "${APP_DIR}/users.json" ]]; then
  cp "${APP_DIR}/users.example.json" "${APP_DIR}/users.json"
  chmod 600 "${APP_DIR}/users.json" || true
fi

chown -R "${RUN_USER}:${RUN_USER}" \
  "${APP_DIR}/uploads" \
  "${APP_DIR}/outputs" \
  "${APP_DIR}/fichiers_traites"

if [[ -f "${APP_DIR}/jobs.db" ]]; then
  chown "${RUN_USER}:${RUN_USER}" "${APP_DIR}/jobs.db" || true
fi

if [[ -f "${APP_DIR}/.env" ]]; then
  chown "${RUN_USER}:${RUN_USER}" "${APP_DIR}/.env" || true
fi

if [[ -f "${APP_DIR}/users.json" ]]; then
  chown "${RUN_USER}:${RUN_USER}" "${APP_DIR}/users.json" || true
fi

if [[ "${WITH_SYSTEMD}" == "1" ]]; then
  GUNICORN_BIN="${VENV_DIR}/bin/gunicorn"
  PYTHON_WORKER="${VENV_DIR}/bin/python"

  if [[ ! -x "${GUNICORN_BIN}" ]]; then
    echo "gunicorn introuvable dans le venv: ${GUNICORN_BIN}" >&2
    exit 1
  fi
  if [[ ! -x "${PYTHON_WORKER}" ]]; then
    echo "python introuvable dans le venv: ${PYTHON_WORKER}" >&2
    exit 1
  fi

  cat >/etc/systemd/system/ocr_app.service <<EOF
[Unit]
Description=LocaGed OCR Flask App
After=network.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${GUNICORN_BIN} -w 2 -b 127.0.0.1:5050 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  cat >/etc/systemd/system/ocr_worker.service <<EOF
[Unit]
Description=LocaGed OCR Worker
After=network.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${PYTHON_WORKER} ${APP_DIR}/worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now ocr_app ocr_worker
fi

if [[ "${WITH_NGINX}" == "1" ]]; then
  SRC_CONF="${APP_DIR}/deploy/nginx_ocr_app.conf"
  DST_CONF="/etc/nginx/sites-available/ocr_app"
  if [[ ! -f "${SRC_CONF}" ]]; then
    echo "Fichier nginx manquant: ${SRC_CONF}" >&2
    exit 1
  fi

  cp "${SRC_CONF}" "${DST_CONF}"
  ln -sf "${DST_CONF}" /etc/nginx/sites-enabled/ocr_app

  nginx -t
  systemctl enable --now nginx
  systemctl reload nginx
fi

echo
echo "OK - Installation terminée."
echo "- Projet: ${APP_DIR}"
echo "- Venv: ${VENV_DIR}"
echo "- Utilisateur service: ${RUN_USER}"
echo
echo "Prochaines étapes:"
echo "1) Éditer ${APP_DIR}/.env (FLASK_SECRET_KEY + limites MAX_* selon ton volume)"
echo "2) Initialiser users.json (si besoin) puis créer un utilisateur applicatif:"
echo "   test -f ${APP_DIR}/users.json || cp ${APP_DIR}/users.example.json ${APP_DIR}/users.json"
echo "   sudo -u ${RUN_USER} -H bash -lc 'cd ${APP_DIR} && . .venv/bin/activate && python3 scripts/create_user.py equipe_scan \"***\"'"
echo "3) Vérifier:"
echo "   sudo systemctl status ocr_app ocr_worker --no-pager"
echo "   curl -fsS http://127.0.0.1:5050/health | cat"
echo
