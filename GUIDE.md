# מדריך הפעלה — MA20 Screener

מדריך זה מיועד למפעיל המערכת — אין צורך בידע בתכנות.

---

## מה המערכת עושה?

המערכת סורקת **את מניות NYSE + NASDAQ עם שווי שוק ≥ $1B**
(~1,900 מניות), ובכל ריצה מבצעת ארבעה שלבים:

1. **שלב 1 — תשתית נתונים:** מבקשת מ-FMP screener את כל המניות
   ב-NYSE ו-NASDAQ ששווי השוק שלהן ≥ $1B (לא ETFs, לא Mutual Funds,
   actively trading, US בלבד), מורידה נתוני מסחר יומיים של 60 ימי
   מסחר אחרונים, מסננת מניות שאינן עומדות בשני תנאי הנזילות ביחד —
   שווי שוק ≥ $1B **וגם** ווליום של מעל מיליון מניות ביום המסחר
   האחרון — ומחשבת אינדיקטורים (SMA20, ATR%, CCI14, גאפים פתוחים).
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
  # FMP — Phase A (universe via company-screener) + Phase B (OHLCV).
  # דורש את חבילת FMP Starter (או יותר). הירשם ב-
  # https://site.financialmodelingprep.com/pricing-plans, רכוש Starter,
  # והעתק את ה-API key מה-dashboard לשורה הבאה.
  # Starter = 300 קריאות/דקה, ללא תקרת יום.
  fmp_api_key: "PUT-YOUR-FMP-API-KEY-HERE"

  # Phase B — concurrency profile (תואם ל-FMP Starter tier)
  history_workers: 1
  history_sleep_ms: 250
  history_retries: 3              # ניסיונות נוספים על כשלי רשת
  history_retry_delay_s: 5        # השהיה התחלתית לשגיאות גנריות

  min_market_cap_usd: 1000000000  # סף שווי שוק = 1 מיליארד דולר
  min_last_day_volume: 1000000    # סף ווליום ליום המסחר האחרון = מיליון מניות
  history_trading_days: 60        # חלון היסטוריה = 60 ימי מסחר
  test_tickers: ""                # סריקה חלקית לבדיקה — ראה למטה
```

**מסנן הנזילות (שני תנאים ביחד):** בסוף Phase B כל מניה נבחנת מול **שני**
הספים, ורק מי שעומדת ב**שניהם** ממשיכה הלאה:

1. `שווי שוק >= 1,000,000,000$` — מתוך תשובת ה-screener של FMP.
2. `ווליום ביום המסחר האחרון > 1,000,000 מניות` — הנפח של הנר האחרון
   בחלון 60 הימים, **לא ממוצע**. זהו אותו יום מסחר שכל שאר הבדיקות
   מנתחות. ההשוואה חדה: בדיוק מיליון מניות **נדחה**.

מניה שנכשלת ולו בתנאי אחד נדחית ואינה ממשיכה לשלב הבא. בלוג תראה בדיוק
באיזה סף היא נכשלה — ואם נכשלה בשניהם, שניהם יופיעו באותה שורה:
```
THINVOL — FAIL: last-day volume 250,000 not above 1,000,000
SMALLCP — FAIL: market cap $200,000,000 below $1,000,000,000
BOTHBAD — FAIL: market cap $200,000,000 below $1,000,000,000; last-day volume 300,000 not above 1,000,000
```
אפשר לשנות כל אחד מהספים בקובץ ההגדרות בלי לגעת בקוד. להורדת סף
הווליום למחצית המיליון, לדוגמה: `min_last_day_volume: 500000`.

**מקורות הנתונים:** המערכת משתמשת אך ורק ב-FMP (Starter ומעלה):

1. **Phase A — `/stable/company-screener`**: שתי קריאות (NASDAQ + NYSE).
   כל קריאה מחזירה את כל המניות בבורסה הספציפית עם
   `marketCap ≥ $1B`, `isEtf=false`, `isFund=false`,
   `isActivelyTrading=true`, `country=US`. ה-response כולל את
   `marketCap` ו-`exchangeShortName` ישירות.

   **שני פילטרים נוספים ב-client**:
   * כל ticker שמכיל `$` נזרק (preferred shares).
   * כל ticker שמכיל `-` נזרק (class B shares כמו `BRK.B` שמומרים
     ל-`BRK-B`; FMP חוסם אותם ממילא ב-tier הנוכחי).

2. **Phase B — `/stable/historical-price-eod/full?symbol=X`**:
   קריאה אחת לכל טיקר (~1,900 קריאות לריצה מלאה של NASDAQ + NYSE).
   מחזיר OHLCV יומי.

Starter = 300 קריאות/דקה, ללא תקרת יום. ריצה מלאה ~1,902 קריאות =
**~8 דקות** עם הקונפיג הסטנדרטי.

**חישוב שווי שוק:** `שווי שוק = marketCap` מתוך תשובת ה-screener
(כבר ב-USD; ה-screener כבר סינן ≥ $1B server-side).

**חוסן וכשלים:** Phase B מבצע retry אוטומטי עם backoff מעריכי. אם
טיקר חסר ב-FMP — מסומן כ-"לא נמצא" (לא retry). שגיאה 429 (rate limit)
מקבלת backoff נפרד (10s, 20s, 40s). שורות שגיאה בלוג:
* `fmp historical: FMP returned no historical rows for ...` — FMP לא
  מכיר את הטיקר או שאין נתונים בחלון 60 הימים.
* `fmp historical rate-limited after N attempts: ...` — חרגנו מ-300
  קריאות/דקה. אם זה קורה הרבה, העלה את `history_sleep_ms` או הורד
  את `history_workers`.
* `fmp historical error after N attempts: HTTPError: ...` — שגיאת
  רשת אחרת אחרי כל הניסיונות.
* `missing/NaN OHLCV (first missing date ...)` — FMP החזיר נתונים אבל
  חסרים ימים בתוך החלון.
* `market cap $... below $1,000,000,000` — דחייה הגנתית של ticker
  שעבר את ה-screener אך marketCap ירד בין לבין (נדיר; קורה אם הריצה
  ב-test mode שמדלגת על ה-screener).

**`test_tickers`** מאפשר לבדוק את המערכת על מספר מצומצם של מניות לפני
ריצה מלאה. לדוגמה:
```yaml
  test_tickers: "AAPL,MSFT,NVDA,BRK-B"
```
כדי להפעיל סריקה על **כל מניות NYSE + NASDAQ עם $1B+**, השאר את הערך ריק:
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
- עם `test_tickers` של 5-10 מניות: ~15-20 שניות (Phase A: 1 quote
  call לכל test-ticker; Phase B: 1 historical call לכל test-ticker).
- ריצה מלאה (~1,900 מניות NYSE+NASDAQ $1B+): **~8 דקות** עם
  `workers=1, sleep_ms=250`. מהיר יותר עם `workers=4`.

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
