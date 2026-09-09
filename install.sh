#!/bin/bash
# Claude 用量監控 Desklet 安裝腳本
# 執行後請依照提示操作

set -euo pipefail

DESKLET_DIR="$HOME/.local/share/cinnamon/desklets/claude-usage@lintzuyang"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/desklet/claude-usage@lintzuyang"

echo "=== Claude 用量監控 Desklet 安裝 ==="
echo ""

# 檢查來源目錄
if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "錯誤：找不到 desklet 來源目錄 $SOURCE_DIR"
    exit 1
fi

# 建立目標目錄
echo "建立安裝目錄：$DESKLET_DIR"
mkdir -p "$DESKLET_DIR"

# 複製檔案
echo "複製檔案..."
cp "$SOURCE_DIR/metadata.json" "$DESKLET_DIR/"
cp "$SOURCE_DIR/desklet.js" "$DESKLET_DIR/"
cp "$SOURCE_DIR/settings-schema.json" "$DESKLET_DIR/"
cp "$SOURCE_DIR/stylesheet.css" "$DESKLET_DIR/"

echo ""
echo "=== 安裝完成 ==="
echo ""
echo "後續步驟："
echo "1. 開啟 Cinnamon 設定 → Desklets（小工具）"
echo "2. 在「可用的 Desklets」中找到「Claude 用量監控」"
echo "3. 點擊「新增到桌面」或雙擊啟用"
echo "4. 右鍵點擊桌面上的 desklet → 設定，調整更新間隔、顯示選項等"
echo ""
echo "注意："
echo "- Collector 需要先執行一次才會有資料："
echo "  cd ~/Claude/linux_claude_usage && .venv/bin/python -m collector.main"
echo "- 或加入排程（每分鐘跑一次）："
echo "  * * * * * cd ~/Claude/linux_claude_usage && .venv/bin/python -m collector.main"
echo ""
echo "Desklet 目錄：$DESKLET_DIR"