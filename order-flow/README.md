# Order Flow Screener (ทองคำ · คริปโต)

หน้าเว็บ static (HTML + JS ล้วน) ดึงข้อมูลสาธารณะจาก Binance ผ่าน WebSocket แล้วคำนวณทุกอย่างในเบราว์เซอร์ ไม่ต้องมี backend หรือ API key

- **Perp**: Binance USDⓈ-M Futures เช่น `XAUUSDT` (ทองคำ), `XAGUSDT` (เงิน), `BTCUSDT`
- **Spot**: Binance Spot เช่น `PAXGUSDT` (โทเคนทอง), `BTCUSDT`
- เปิดสัญลักษณ์อื่นได้จากช่อง symbol หรือ URL: `order-flow/?market=futures&symbol=ETHUSDT`

## สิ่งที่แสดง

| ส่วน | รายละเอียด |
| --- | --- |
| ราคา & CVD | แท่งเทียน 1s–15m, VWAP ±1σ, POC, Cumulative Delta (reset ทุก session), แจ้ง bullish/bearish divergence |
| Volume Z-score | `z = (V_t − mean(V_{t−N..t−1})) / sd(...)` แท่ง 1s–5m, N = 20/50/100 |
| Volatility | Realized vol 30 นาที (annualized, 365 วัน), percentile + regime, Parkinson, ATR14, range ปัจจุบัน ÷ ATR |
| Order Book | 20 ระดับ, spread, OBI 5/20 ระดับ + EMA |
| Volume Profile | ของ session: POC, Value Area 70% |
| Time & Sales | กรองเทรดใหญ่ ≥2× / 5× / 10× ของขนาดเฉลี่ย 500 เทรดล่าสุด |
| Signals | volume spike z ≥ 3, เทรดใหญ่ ≥ 10×, CVD divergence, เปลี่ยน volatility regime |

Session เริ่มที่ UTC 00:00 (07:00 เวลาไทย) หรือ NY 17:00 (รอบวันของทอง/FX)

## ข้อจำกัด

- XAUUSD ของโบรกเกอร์ Forex/CFD ไม่มี volume กลาง จึงใช้สัญญา XAUUSDT ของ Binance แทน (ราคาใกล้ทองสปอต แต่มี basis/funding)
- ประวัติก่อนเปิดหน้าเว็บมาจากแท่ง 1 นาที (delta จาก taker-buy volume, VWAP/profile จาก typical price) หลังจากนั้นใช้ trade จริงทุกไม้
- Binance บล็อกบางประเทศ/เครือข่าย (เช่น สหรัฐฯ) — หน้าเว็บจะแสดงข้อความเตือน

## ไฟล์

- `engine.js` — ฟังก์ชันคำนวณล้วน (z-score, realized vol, ATR, OBI, value area, VWAP, resample, divergence)
- `app.js` — เชื่อมต่อ Binance, เก็บ state, วาดกราฟ canvas
- `engine.test.js` — `node --test order-flow/engine.test.js`

แนวคิดจาก [zadescoxp/order-flow-screener](https://github.com/zadescoxp/order-flow-screener) (Apache-2.0) — โค้ดที่นี่เขียนใหม่ทั้งหมด
