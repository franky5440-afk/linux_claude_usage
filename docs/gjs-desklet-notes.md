# Cinnamon desklet（GJS / St 元件）實作筆記

這份是開發本 desklet 時實際踩到的問題與驗證方式。
共同點是**「語法過、測試綠、裝上去卻不會動」**——所以獨立記一份。

## 最重要的一條：測試綠 ≠ 東西會動

本專案曾經 28 條測試全綠、log 零錯誤，但截圖一看**表格欄位是錯位的**
（總額跑到最右欄、明細金額在中間欄，同一個概念出現在兩個位置）。
測試看不到、log 看不到，**只有眼睛看得到**。

⇒ 改動 desklet 之後，一定要真的裝上去跑，而且一定要截圖用眼睛確認。

## St 元件的坑

- **只吃像素單位。** `width: "50%"` 會變成 NaN；`height: 100%` 被算成 0，
  而且**不報錯**——它會安靜地畫出一個零厚度的東西，畫面上什麼都沒有。
- **不支援 `@theme_*` 變數。** 那是 GTK 的語法，St 的 CSS 解析器不認，
  填色會直接變透明，同樣不報錯。顏色一律寫死色值。
- **建構參數只吃 JS 屬性。** `new St.*({...})` 認得的是
  `vertical` / `x_expand` / `x_align` / `text` / `style_class` /
  `width` / `height` / `reactive` / `can_focus` 這類；
  其餘視覺設定要走 stylesheet，或在建立之後改 `.clutter_text.*`。
  寫在建構參數裡的未知鍵會被忽略，不會有人提醒你。

## 驗證步驟

```bash
# 1. 裝上去
cp desklet/claude-usage@lintzuyang/* \
   ~/.local/share/cinnamon/desklets/claude-usage@lintzuyang/

# 2. 記下目前的 log 行數，只看之後新增的部分
N=$(wc -l < ~/.xsession-errors)

# 3. 重載 Cinnamon
dbus-send --session --dest=org.Cinnamon --type=method_call /org/Cinnamon \
  org.Cinnamon.Eval string:"global.reexec_self()"

# 4. 等久一點：錯誤是在第一次抓資料時才觸發的，看太早會誤判「零錯誤」
sleep 50

# 5. 看錯誤
tail -n +$((N+1)) ~/.xsession-errors | grep -iE "CRITICAL|JS ERROR|nan"

# 6. 用眼睛確認版面
DISPLAY=${DISPLAY:-:0} gnome-screenshot -f /tmp/check.png
```

⚠️ **不要只 grep desklet 名稱。** `GLib-GObject-CRITICAL` 那幾行不含 desklet 名字，
只 grep `claude-usage` 會漏掉。要分辨是不是自己的錯誤，看行首的行程名——
`xapp-sn-watcher`、`IBus` 之類的與本專案無關。
