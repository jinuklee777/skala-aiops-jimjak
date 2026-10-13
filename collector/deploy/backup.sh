#!/usr/bin/env bash
# 수집기 데이터를 rclone 리모트(Google Drive)로 백업한다. 리눅스 서버의 cron이 하루 한 번 부른다.
# 1) 원본을 Drive에 복사한다. 서버에서 지운 파일은 Drive에서 지우지 않는다.
# 2) DB 스냅샷을 떠서 올린다. Drive에는 최근 BACKUP_DB_KEEP_DAYS일 치만 둔다.
# 3) BACKUP_KEEP_DAYS일이 지난 원본 폴더는 Drive와 같은지 확인한 뒤에만 서버에서 지운다.
# 실패하면 수집기 알림(웹훅·알림 명령)으로 알린다. 자세한 것은 README의 「백업과 복구」.
set -euo pipefail

cd "$(dirname "$0")/../.."  # 저장소 루트
export TZ=Asia/Seoul  # 원본 폴더 날짜가 한국 시각이다
REMOTE="${BACKUP_REMOTE:-gdrive:jimjak-collector}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
DB_KEEP_DAYS="${BACKUP_DB_KEEP_DAYS:-14}"
PYTHON="${PYTHON:-python3}"

log() { echo "$(date '+%F %T') $*"; }
notify() {
  "$PYTHON" -c 'import sys
from collector import alerts
from collector.config import load_config
alerts.send(load_config(), f"{alerts.PREFIX} 백업 실패: {sys.argv[1]}")' "$1" || true
}
STEP="준비"
trap 'log "실패: $BASH_COMMAND"; notify "$STEP 중 실패 (서버 ~/jimjak-backup.log 확인)"' ERR

DATA_DIR="$("$PYTHON" -c 'from collector.config import load_config; print(load_config().data_dir)')"
RAW="$DATA_DIR/raw"
TODAY="$(date +%F)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
log "백업 시작: $DATA_DIR → $REMOTE"

# 1) 원본. 쓰는 중인 임시 파일(.tmp)은 뺀다.
STEP="원본 복사"
rclone copy "$RAW" "$REMOTE/raw" --exclude "*.tmp" --stats-one-line --stats-log-level NOTICE
log "원본 복사 끝"

# 2) DB. 수집이 돌고 있어도 SQLite 백업 API는 한 시점의 일관된 사본을 뜬다.
STEP="DB 스냅샷"
"$PYTHON" - "$DATA_DIR/collector.db" "$TMP/collector.db" <<'EOF'
import gzip, shutil, sqlite3, sys
source, copy = sys.argv[1], sys.argv[2]
src, dst = sqlite3.connect(source, timeout=60), sqlite3.connect(copy)
src.backup(dst)
src.close()
dst.execute("PRAGMA journal_mode = DELETE")  # -wal 없이 파일 하나로 끝나게
result = dst.execute("PRAGMA quick_check").fetchone()[0]
dst.close()
if result != "ok":
    sys.exit(f"DB 스냅샷 검사 실패: {result}")
with open(copy, "rb") as plain, gzip.open(copy + ".gz", "wb") as packed:
    shutil.copyfileobj(plain, packed)
EOF
rclone copyto "$TMP/collector.db.gz" "$REMOTE/db/collector-$TODAY.db.gz"
rclone delete "$REMOTE/db" --min-age "${DB_KEEP_DAYS}d"  # Drive에서는 휴지통으로 간다
log "DB 스냅샷 올림: db/collector-$TODAY.db.gz"

# 3) 오래된 원본 정리. 폴더 이름(실행 날짜)으로 고르고, Drive와 해시까지 같을 때만 지운다.
STEP="오래된 원본 정리"
CUTOFF="$("$PYTHON" -c 'import sys, datetime as d; print(d.date.today() - d.timedelta(days=int(sys.argv[1])))' "$KEEP_DAYS")"
kept=0
for dir in "$RAW"/*/????-??-??; do
  [ -d "$dir" ] || continue
  day="$(basename "$dir")"
  job="$(basename "$(dirname "$dir")")"
  [[ "$day" < "$CUTOFF" ]] || continue
  if rclone check "$dir" "$REMOTE/raw/$job/$day" --one-way --exclude "*.tmp"; then
    rm -rf "$dir"
    log "서버에서 지움: raw/$job/$day (Drive와 같음)"
  else
    kept=$((kept + 1))
    log "남겨 둠: raw/$job/$day (Drive와 다름)"
  fi
done
if [ "$kept" -gt 0 ]; then
  notify "${KEEP_DAYS}일 지난 원본 폴더 ${kept}개가 Drive와 달라서 지우지 않았다 (서버 ~/jimjak-backup.log 확인)"
  exit 1
fi
log "백업 끝"
