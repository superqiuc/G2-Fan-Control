#!/usr/bin/env bash
# 构建风扇控制的 fnOS 安装包（.fpk）
# 用法：bash build.sh [arch]   默认 x86
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="${1:-x86}"
case "$ARCH" in
  x86|x86_64|amd64) ARCH=x86; PLATFORM=x86 ;;
  arm|arm64)        ARCH=arm; PLATFORM=arm ;;
  *) echo "未知架构：$ARCH" >&2; exit 1 ;;
esac
VERSION="$(awk -F'=' '/^version[[:space:]]*=/{gsub(/[[:space:]]/,"",$2);print $2}' "$ROOT/manifest")"
[ -n "$VERSION" ] || { echo "无法从 manifest 读取版本" >&2; exit 1; }

STAGE="$ROOT/.build/stage"
DIST="$ROOT/dist"
rm -rf "$STAGE"
mkdir -p "$STAGE" "$DIST"

cp "$ROOT/manifest" "$STAGE/manifest"
sed -i "s/^platform[[:space:]]*=.*/platform              = $PLATFORM/" "$STAGE/manifest"
cp -a "$ROOT/cmd" "$ROOT/config" "$ROOT/wizard" "$STAGE/"
cp "$ROOT/ICON.PNG" "$ROOT/ICON_256.PNG" "$STAGE/"
chmod 644 "$STAGE/ICON.PNG" "$STAGE/ICON_256.PNG"

# 统一应用目录权限后再打包
chmod -R a+rX "$ROOT/app"

# 应用负载（排除缓存与构建产物）
tar czf "$STAGE/app.tgz" -C "$ROOT/app" \
  --owner=0 --group=0 --numeric-owner --sort=name \
  --exclude='__pycache__' --exclude='*.pyc' \
  server ui www

# 统一权限：脚本可执行，其余只读
find "$STAGE/cmd" -type f -exec chmod 755 {} +
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f ! -path "$STAGE/cmd/*" -exec chmod 644 {} +

OUT="$DIST/g2fan-${VERSION}-${ARCH}.fpk"
rm -f "$OUT"
tar czf "$OUT" -C "$STAGE" \
  --owner=0 --group=0 --numeric-owner --sort=name \
  manifest cmd config wizard ICON.PNG ICON_256.PNG app.tgz

rm -rf "$STAGE"
echo "已生成：$OUT（$(du -h "$OUT" | cut -f1)）"
sha256sum "$OUT" > "$OUT.sha256"
cat "$OUT.sha256"
