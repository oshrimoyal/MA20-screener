# מדריך הפעלה — MA20 Screener

מדריך זה מיועד למפעיל המערכת — אין צורך בידע בתכנות.

---

## מה המערכת עושה?

המערכת סורקת **את כל המניות הנסחרות בבורסות NYSE ו-NASDAQ** (כולל ETF
ומניות ADR זרות), ובכל ריצה מבצעת ארבעה שלבים:

1. **שלב 1 — תשתית נתונים:** משיגה את רשימת כל המניות, מסננת מניות עם
   שווי שוק מתחת למיליארד דולר, מורידה נתוני מסחר יומיים של 60 ימי
   מסחר אחרונים, ומחשבת אינדיקטורים (SMA20, ATR%, CCI14, גאפים פתוחים).
2. **שלב 2 — בדיקות גולמיות:** מריצה על כל מניה 6 בדיקות (מגמה
   חודשית/שבועית, נר אחרון וצורות, ווליום, מיקום מול ה-SMA20, גאפים מול
   המחיר, מצב CCI).
3. **שלב 3 — סינון:** בודקת כל מניה מול 6 קטגוריות; רק מניה שעוברת
   **את כל ששת הקטגוריות** נחשבת מועמדת.
4. **שלב 4 — פלט:** מייצרת קובץ CSV של כל המניות (שעברו ושנדחו) ושולחת
   לטלגרם רק את המועמדים שעברו.

---

## התקנה (פעם אחת בלבד)

יש לבצע פעם אחת על המחשב שעליו תרצה להריץ את המערכת.

1. **התקן Python 3.11 או חדש יותר.** באתר https://www.python.org
   הורד והתקן. בעת ההתקנה ב-Windows סמן את הצ׳קבוקס "Add Python to PATH".
2. **פתח טרמינל** (ב-Mac/Linux: Terminal; ב-Windows: PowerShell או
   Command Prompt).
3. **היכנס לתיקיית הפרויקט:**
   ```
   cd /הנתיב/אל/MA20-screener
   ```
4. **התקן את החבילות הדרושות:**
   ```
   pip install -r requirements.txt
   ```
   ההתקנה לוקחת בדרך כלל 1-2 דקות.

---

## קובץ ההגדרות — `config.yaml`

זה הקובץ היחיד שתצטרך לערוך. פתח אותו בכל עורך טקסט (Notepad, TextEdit,
VS Code וכו׳). הקובץ מחולק לשלושה אזורים:

### `telegram`
```yaml
telegram:
  token: "PUT-YOUR-BOT-TOKEN-HERE"
  chat_id: "PUT-YOUR-CHAT-ID-HERE"
```
- **`token`** — הטוקן של בוט הטלגרם שלך. את הטוקן תקבל מ-BotFather בטלגרם.
- **`chat_id`** — מזהה הצ׳אט שאליו לשלוח את ההודעות (יכול להיות צ׳אט
  פרטי, קבוצה, או ערוץ).

### `paths`
```yaml
paths:
  csv_dir: "./output"   # היכן יישמר קובץ ה-CSV
  log_dir: "./logs"     # היכן יישמרו קבצי ה-Log
```
ברירת המחדל מתאימה לרוב המשתמשים. אין צורך לשנות אלא אם תרצה תיקיות אחרות.

### `runtime`
```yaml
runtime:
  # SEC EDGAR — מקור החובה לחישוב שווי שוק
  # SEC דורש אימייל אמיתי בכותרת User-Agent (מדיניות "fair access").
  # החלף את הדוגמה ב-email האמיתי שלך.
  sec_user_agent: "MA20-Screener your-email@example.com"
  stooq_user_agent: "MA20-Screener your-email@example.com"

  # Phase B — שליפת היסטוריית מחירים מ-Stooq
  history_workers: 3
  history_sleep_ms: 500
  history_retries: 3              # ניסיונות נוספים על כשלי רשת
  history_retry_delay_s: 5        # השהיה התחלתית (5s, 10s, 20s)

  min_market_cap_usd: 1000000000  # סף שווי שוק = 1 מיליארד דולר
  history_trading_days: 60        # חלון היסטוריה = 60 ימי מסחר
  test_tickers: ""                # סריקה חלקית לבדיקה — ראה למטה
```

**מקורות הנתונים החדשים:** המערכת השתנתה כדי לא להיות תלויה ב-Yahoo
Finance (שחוסם IP אגרסיבית). כל הנתונים מגיעים משלושה מקורות חינמיים
וציבוריים — ללא הרשמה ובלי API key:

1. **NASDAQ Trader + Wikipedia** — רשימת היקום (S&P 500 + NYSE + NASDAQ
   Global Select + NYSE American + NYSE Arca).
2. **SEC EDGAR** — שתי קריאות `https://data.sec.gov/...`:
   * `company_tickers_exchange.json` — מיפוי טיקר ↔ CIK ↔ בורסה.
   * XBRL Frames API — מספר המניות הסחירות (`CommonStockSharesOutstanding`)
     לכל החברות במכה אחת.
3. **Stooq** — נתוני OHLCV יומיים (`https://stooq.com/q/d/l/?s={ticker}.us&i=d`),
   60 ימי מסחר אחרונים פר טיקר.

**חישוב שווי שוק:** `שווי שוק = Close אחרון מ-Stooq × מניות סחירות מ-SEC`.
הסף $1B נשמר; המסנן עבר ל-Phase B.

**חוסן וכשלים:** Phase B מבצע retry אוטומטי עם backoff מעריכי על שגיאות
HTTP זמניות. אם Stooq מחזיר "אין נתונים" עבור טיקר — זה תשובה קבועה
(לא retry). שורות שגיאה בלוג תיראינה ספציפיות:
* `shares outstanding unavailable (no SEC CIK)` — ADR/חברה שלא רשומה ב-SEC.
* `shares outstanding unavailable` — SEC רשם אבל אין נתון shares במאזן.
* `stooq: Stooq did not return a CSV body for ...` — Stooq לא מכיר את הטיקר.
* `stooq error after N attempts: HTTPError: ...` — Stooq חסם זמנית; נסה שוב מאוחר יותר.
* `missing/NaN OHLCV (first missing date ...)` — נתונים חסרים בחלון 60 הימים.

**אם Stooq חוסם אותך** (נדיר — אבל אם זה קורה):
1. הורד את `history_workers` ל-1.
2. העלה את `history_sleep_ms` ל-1000 או 2000.

**סינון מקדים של non-stocks:** ה-parser דוחה אוטומטית (לפני שליחת הקריאה
לרשת) טיקרים שלפי השם הם preferred shares / debentures / subordinated
notes / trust preferred. דוגמאות שייפלו כבר ב-parse:
`AFGB` (Subordinated Debentures), `BEPH` (Preferred Limited Partnership
Units), `AIZN` (Subordinated Notes). זה חוסך אלפי קריאות. בלוג תראה
שורות כמו:
```
Phase A: dropped 1543 preferred/debenture/note entries from NYSE-proper list
  (matched Security Name pattern).
```
**`test_tickers`** מאפשר לבדוק את המערכת על מספר מצומצם של מניות לפני
ריצה מלאה. לדוגמה:
```yaml
  test_tickers: "AAPL,MSFT,NVDA,SPY"
```
כדי להפעיל סריקה על **כל המניות** של NYSE+NASDAQ, השאר את הערך ריק:
```yaml
  test_tickers: ""
```

---

## הפעלה

מהטרמינל, מתוך תיקיית הפרויקט:

```
python main.py
```

זהו. הריצה תארך:
- עם `test_tickers` של 5-10 מניות: שניות בודדות.
- ריצה מלאה (~6,000 מניות): 15-30 דקות בערך, תלוי במהירות האינטרנט.

במהלך הריצה תראה במסך התקדמות שלב אחר שלב, כולל מספר מניות שעברו /
נדחו בכל שלב. בסיום תיווצר קובץ CSV חדש בתיקיית `output/` ויישלחו
הודעות לטלגרם.

---

## איך לקרוא את ה-CSV

הקובץ ייקרא `MA20_Stocks_DD-MM-YYYY.csv` (לפי תאריך הריצה) ויכיל את **כל**
המניות שעברו את שלב 1+2 — גם אלו שעברו את הסינון, וגם אלו שנדחו.

עמודות מרכזיות:
- **`Ticker`** — סמל המניה.
- **`Status`** — `Passed` או `Rejected`.
- **`Reason`** — אם עברה: "Passed all 6 categories". אם נדחתה: "Failed in
  categories 1, 4" (לדוגמה). אם נכשלה בקטגוריה 3 בגלל הסינון השלילי:
  "Failed in category 3 (negative filter)".
- **`Trend_Month` / `Trend_Week`** — מגמה חודשית / שבועית
  (`rising`/`falling`/`consolidating`).
- **`Candle_Color`** — צבע הנר האחרון.
- **`Candle_Patterns`** — צורות שזוהו (Hammer, Bullish Engulfing וכו׳),
  מופרדות בפסיק.
- **`Volume_Today` / `Volume_Color_Today`** — נפח היום ו-צבעו.
- **`SMA20_Position` / `SMA20_Distance` / `SMA20_Role` / `SMA20_Breakout`**
  — תיאור המיקום מול הממוצע הנע.
- **`Gap_Above` / `Gap_Below` / `Gap_Inside`** — Yes/No לגאפים פתוחים.
- **`CCI_Value` / `CCI_Slope` / `CCI_Zone`** — מצב ה-CCI.
- **`Chart_Link`** — קישור ישיר לגרף ב-TradingView.

ניתן לפתוח את הקובץ ב-Excel / Google Sheets ולסנן/למיין כרגיל.

---

## איך לקרוא את הודעות הטלגרם

כל ריצה שולחת רצף הודעות לבוט שהגדרת.

**הודעה ראשונה — כותרת:**
```
📊 MA20 Stocks | 24/05/2026 | 7 candidates
```
(`7 candidates` = מספר המועמדים שעברו את כל הקטגוריות.)

**הודעות המשך** — חבילות של 5 מניות בכל הודעה. עבור כל מניה מופיע
בלוק כזה:
```
━━━━━━━━━━━━━━━━━

🎯 $AAPL

📈 Month: rising | Week: consolidating

🕯 Candle: green + hammer

📊 Volume: green after 4+ red days

📏 SMA20: above, close (flirting) (support)

⚡ Gap: open above

🌡 CCI: 87, slope positive

🔗 https://www.tradingview.com/chart/?symbol=NASDAQ:AAPL
```
לחיצה על הקישור פותחת את הגרף ישירות ב-TradingView.

**אם לא נמצאו מועמדים** באותה ריצה, יישלח:
```
MA20 Stocks | 24/05/2026 | 0 candidates today
```

---

## פתרון תקלות

1. **המערכת לא רצה / שגיאה בהפעלה.** ודא שהתקנת את כל החבילות:
   ```
   pip install -r requirements.txt
   ```

2. **אין הודעות בטלגרם.** ודא ש-`token` ו-`chat_id` בקובץ `config.yaml`
   נכונים. בקובץ ה-Log תראה את השגיאה המדויקת.

3. **הקובץ CSV נוצר אבל לא נשלחה הודעה לטלגרם.** ייתכן שהיתה תקלת
   רשת. המערכת מנסה לשלוח, ובמקרה של כישלון מנסה שוב פעם אחת אחרי 5
   שניות. אם גם הניסיון השני נכשל — הקובץ ה-CSV עדיין נוצר ושמור,
   ותוכל לפתוח אותו. הפעלה חוזרת בדקות הקרובות תיצור קובץ חדש ותנסה
   שוב לשלוח לטלגרם.

4. **לוגים.** כל ריצה יוצרת קובץ Log חדש בתיקייה `logs/` עם שם בפורמט
   `run_YYYYMMDD_HHMMSS.log`. בקובץ זה תוכל לראות כל מה שקרה בריצה,
   כולל לאיזו מניה נכשלה ולמה (חוסר נתונים, שווי שוק נמוך וכו׳).
   במקרה של תקלה — שלח את קובץ ה-Log למי שיכול לעזור.

5. **ריצה מהירה לבדיקה.** אם אתה רוצה לוודא שהמערכת תקינה בלי לחכות
   לסריקה מלאה — הכנס לקובץ `config.yaml` בערך `test_tickers` רשימה
   קצרה של סימבולים, למשל:
   ```yaml
     test_tickers: "AAPL,MSFT,NVDA,SPY"
   ```
   והרץ. תוך מספר שניות תקבל קובץ CSV ותראה אם המערכת עובדת.
