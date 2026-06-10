#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/vpn-sales-bot"
BACKUP_ROOT="/opt/backups/vpn-sales-bot"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP_DIR="$(mktemp -d)"
SNAPSHOT_DIR="${TMP_DIR}/snapshot"
ARCHIVE_PATH="${BACKUP_ROOT}/vpn-sales-bot-${STAMP}.tar.gz"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

mkdir -p "${BACKUP_ROOT}" "${SNAPSHOT_DIR}"

export APP_DIR SNAPSHOT_DIR
python3 <<'PY'
import os
import shutil
import sqlite3
from pathlib import Path

app_dir = Path(os.environ["APP_DIR"])
snapshot_dir = Path(os.environ["SNAPSHOT_DIR"])

project_snapshot = snapshot_dir / "project"
shutil.copytree(
    app_dir,
    project_snapshot,
    ignore=shutil.ignore_patterns(
        ".venv",
        "__pycache__",
        "*.pyc",
        "*.pyo",
        "*.pyd",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.log",
        "*.pid",
    ),
)

db_source = app_dir / "data" / "app.db"
db_target = snapshot_dir / "app.db"
if db_source.exists():
    db_target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db_source)
    target = sqlite3.connect(db_target)
    source.backup(target)
    target.close()
    source.close()
PY

mkdir -p "${SNAPSHOT_DIR}/etc/nginx/sites-available" "${SNAPSHOT_DIR}/etc/x-ui" "${SNAPSHOT_DIR}/usr/local/x-ui/bin"

cp -a /etc/nginx/nginx.conf "${SNAPSHOT_DIR}/etc/nginx/" 2>/dev/null || true
cp -a /etc/nginx/sites-available/jolasekavpn.ru.conf "${SNAPSHOT_DIR}/etc/nginx/sites-available/" 2>/dev/null || true
cp -a /etc/x-ui/x-ui.db "${SNAPSHOT_DIR}/etc/x-ui/" 2>/dev/null || true
cp -a /usr/local/x-ui/bin/config.json "${SNAPSHOT_DIR}/usr/local/x-ui/bin/" 2>/dev/null || true

tar -C "${SNAPSHOT_DIR}" -czf "${ARCHIVE_PATH}" .
find "${BACKUP_ROOT}" -maxdepth 1 -type f -name 'vpn-sales-bot-*.tar.gz' -mtime +"${RETENTION_DAYS}" -delete

echo "${ARCHIVE_PATH}"
