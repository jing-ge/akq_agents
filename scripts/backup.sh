#!/usr/bin/env bash
# 备份 data/meta.db + parquet/ohlcv 到 data/backup/YYYYMMDD/
#
# M20: oracle review P0-2 — 没任何备份, SSD 坏了 10 天积累 + 25K factor_metrics 全没。
# 用法:
#   ./scripts/backup.sh          # 备份到 data/backup/$(date +%Y%m%d)/
#   ./scripts/backup.sh --prune  # 顺手清理 4 周前的旧备份
#
# 推荐 crontab 每周日 03:00 自动跑:
#   0 3 * * 0 cd /path/to/project && ./scripts/backup.sh --prune >> data/backup.log 2>&1

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

DATE_TAG="$(date +%Y%m%d)"
BACKUP_DIR="data/backup/$DATE_TAG"
mkdir -p "$BACKUP_DIR"

# ---- 备份 meta.db (核心: factor_proposals/_metrics, portfolio_snapshots/_nav, job_runs) ----
if [[ -f "data/meta.db" ]]; then
    # 用 sqlite3 .backup 命令在线热备份, 比 cp 更安全 (即便 daemon 在写也不损坏)
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "data/meta.db" ".backup '$BACKUP_DIR/meta.db'"
        echo "[backup] meta.db -> $BACKUP_DIR/meta.db ($(du -h "$BACKUP_DIR/meta.db" | awk '{print $1}'))"
    else
        cp "data/meta.db" "$BACKUP_DIR/meta.db"
        echo "[backup] (warning: no sqlite3, 用 cp 可能不一致) meta.db -> $BACKUP_DIR/"
    fi
else
    echo "[backup] data/meta.db 不存在, 跳过"
fi

# ---- 备份 parquet (OHLCV 历史, 重建数据底层) ----
if [[ -d "data/parquet" ]]; then
    # 用 rsync 增量, 避免每周完整 copy 60+ MB
    if command -v rsync >/dev/null 2>&1; then
        # --link-dest 用上一次备份做 hardlink, 不变的 parquet 不占空间
        # 用 || true 防 ls 没匹配时 set -e 挂
        LAST_BACKUP=$(ls -1dt data/backup/*/parquet 2>/dev/null | head -1 || true)
        if [[ -n "$LAST_BACKUP" && "$LAST_BACKUP" != "$BACKUP_DIR/parquet" ]]; then
            rsync -a --link-dest="$(cd "$LAST_BACKUP" && pwd)" "data/parquet/" "$BACKUP_DIR/parquet/"
        else
            rsync -a "data/parquet/" "$BACKUP_DIR/parquet/"
        fi
        echo "[backup] parquet -> $BACKUP_DIR/parquet ($(du -sh "$BACKUP_DIR/parquet" | awk '{print $1}'))"
    else
        cp -r "data/parquet" "$BACKUP_DIR/parquet"
        echo "[backup] (warning: no rsync, full copy) parquet -> $BACKUP_DIR/parquet"
    fi
else
    echo "[backup] data/parquet 不存在, 跳过"
fi

# ---- 写一个 manifest 让 alerter 能读到最后备份时间 ----
cat > "data/backup/LAST_BACKUP" <<EOF
date=$DATE_TAG
ts=$(date '+%Y-%m-%d %H:%M:%S')
path=$BACKUP_DIR
size=$(du -sh "$BACKUP_DIR" 2>/dev/null | awk '{print $1}')
EOF
echo "[backup] manifest -> data/backup/LAST_BACKUP"

# ---- 清理 4 周前的备份 (可选) ----
if [[ "${1:-}" == "--prune" ]]; then
    CUTOFF=$(date -v -28d +%Y%m%d 2>/dev/null || date -d '28 days ago' +%Y%m%d)  # macOS / linux 兼容
    echo "[backup] prune: 删除 $CUTOFF 之前的备份"
    find data/backup -mindepth 1 -maxdepth 1 -type d | while read -r d; do
        tag=$(basename "$d")
        if [[ "$tag" =~ ^[0-9]{8}$ && "$tag" < "$CUTOFF" ]]; then
            rm -rf "$d"
            echo "  pruned: $d"
        fi
    done
fi

echo "[backup] DONE."
