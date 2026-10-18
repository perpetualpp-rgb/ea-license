# สถานะงานตาม CLAUDE-HANDOFF.md (18 ต.ค. 2026)

สรุปว่าแต่ละขั้นของ handoff ทำอะไรไปแล้ว อยู่ไฟล์ไหน ทดสอบอย่างไร และอะไรที่ยังเป็นข้อมูลตัวอย่างหรือยังเชื่อมไม่ได้
ทุกตัวเลขผลเทสต์ด้านล่างมาจากการรันจริงในเครื่องทดสอบวันนี้

## ผลรันเทสต์

| ชุดเทสต์ | คำสั่ง | ผล |
|---|---|---|
| Python (engine, collector, check, journal resolver, parity JS↔Python) | `cd orderflow-lab && python3 -m unittest discover -s tests` | 109 ผ่าน |
| Backend (PostgreSQL 16 ชั่วคราว, ไม่มี TimescaleDB) | `cd orderflow-lab/server && python3 -m pytest -q` | 66 ผ่าน |
| แอป unit (vitest) | `cd orderflow-lab/app && npm test` | 31 ผ่าน |
| แอป e2e (Chromium 320/390/1440 px) | `cd orderflow-lab/app && npm run build && node e2e/check.mjs` | 46 ผ่าน, 0 ไม่ผ่าน, ไม่มี page error |
| Desk (Playwright, ข้อมูล stub) | `node orderflow-lab/desk/check.js` | ไม่มี page error, อ่าน journal 1004 รายการ |
| openapi.json ตรงกับโค้ด | `cd orderflow-lab/server && python3 -m app.openapi_export --check` | ตรงกัน |

## ทีละขั้นของ handoff

1. **ตรวจ REVIEW.md ก่อนแก้** — ทำซ้ำปัญหาก่อนแก้ทุกข้อ แล้วใส่ regression test (commit `bf6b57a`):
   เทสต์ที่ขึ้นกับเวลาจริง (ใส่นาฬิกาจำลอง), ช่วง id ที่ backfill ไม่สำเร็จหายเงียบ (ทำ ledger + `backfill_retry` + `trade_gap`),
   coverage 100% จากแถวเดียว, ผลลัพธ์กำแพงที่เดาว่า "ถูกกิน", ตัวตัดสินผล journal (แท่งเดียวแตะทั้ง SL และ TP = ไม่ชัด, TP2 = +1.5R)
2. **PWA React+TS 4 หน้า** — `app/` (ภาพรวม, วิเคราะห์, แจ้งเตือน, บันทึกของฉัน) ข้อมูลตัวอย่างมีป้ายทุกหน้า
   แยก DemoSource / ApiSource หลัง interface เดียว (`app/src/data/source.ts`)
3. **ต้นแบบที่ใช้ได้จริง** — สลับสินทรัพย์/timeframe, คำอธิบายศัพท์, บันทึกภาพตลาด (ภาพ + ค่าที่วัด + SHA-256),
   กฎแจ้งเตือน, ทบทวน journal, ส่งออก CSV/JSON; ทุกบันทึกขึ้นป้าย "ร่างในเครื่อง" ไม่มีข้อความว่าบันทึกบนเซิร์ฟเวอร์แล้ว
   (e2e ตรวจข้อนี้ทุกหน้า ทุกความกว้าง)
4. **Backend FastAPI + PostgreSQL** — `server/` (migration, RLS แยกข้อมูลรายสมาชิก, role แยก API/ingest, สิทธิ์ข้อมูลหน่วง 15 นาที
   ทำฝั่ง server, journal/snapshot/เทรด/แจ้งเตือน/ส่งออก, ingest จากไฟล์ collector, `openapi.json`)
   แอปในเบราว์เซอร์ของสมาชิกเรียกเฉพาะ API นี้ ไม่เรียก exchange, connector หรือ `window.claude`
   (ยกเว้นปุ่มดาวน์โหลดในรุ่น claude.ai artifact ซึ่งเป็นต้นแบบ)
5. **ข้อมูล/สูตร/หน่วย/บัญชี/gap** — Decimal 18 ตำแหน่งสำหรับเงิน, PnL linear/inverse + ค่าธรรมเนียม + funding,
   Net R เป็น N/A เมื่อไม่มี initial risk, นิยาม win rate ระบุชัด, gap แสดงเป็นช่องว่างไม่เติมศูนย์, unknown taker side ไม่นับเป็นขาย
   เชื่อม ApiSource กับสัญญา API จริงแล้ว: `server/tests/test_frontend_contract.py` บันทึก response จริงเป็น fixture
   ที่ `app/src/test/api.test.ts` ใช้ และจะ fail ถ้า API เปลี่ยน — เทสต์นี้เจอข้อบกพร่องหนึ่งข้อแล้วแก้
   (แท่งที่ยังไม่ปิดเคยรายงานเวลาข้อมูลเป็นเวลาจบแท่ง แทนที่จะเป็นเวลาสิ้นสุดข้อมูลจริง)
6. **ข้อมูลจริงจากแหล่งที่มีสิทธิ์เท่านั้น** — ยังไม่มี: แอปและ backend ใช้ข้อมูลตัวอย่าง/ข้อมูลทดสอบ;
   ข้อมูล TradingView ไม่ได้ใส่ใน repo; การใช้ข้อมูล Binance เชิงพาณิชย์ต้องมี data licence (ROADMAP ข้อ 4)

## ยังเป็นข้อมูลตัวอย่าง หรือยังเชื่อมไม่ได้

| เรื่อง | สถานะ |
|---|---|
| ราคา แท่งเทียน CVD OI funding กำแพง ไม้ใหญ่ เหตุการณ์ในแอป | ข้อมูลตัวอย่าง (`DemoSource`) |
| quote, observations, details, ประวัติแจ้งเตือน ผ่าน ApiSource | ยังไม่ต่อ ขึ้นข้อความว่ายังไม่รองรับ ไม่เดาค่า |
| แอปคุยกับ server ที่รันจริงผ่านเบราว์เซอร์ | ยังไม่ได้ทดสอบ (ทดสอบผ่าน fixture ที่บันทึกจาก server) |
| เข้าสู่ระบบ | backend ใช้ HS256 dev token; ยังไม่มี identity provider จริงและการตรวจ JWKS; หน้าแอปยังไม่มีหน้าล็อกอิน |
| ชำระเงิน / สมาชิก realtime | ยังไม่มี; สิทธิ์ต้องใส่ในตาราง `entitlements` เอง |
| แจ้งเตือนนอกแอป (Telegram) | ยังไม่มี worker และการส่งจริง |
| บันทึกในแอป | IndexedDB ในเครื่องเท่านั้น ยังไม่ส่งขึ้น server |
| ภาพในบันทึก | เก็บในเครื่อง; server เก็บแค่ key + hash ยังไม่มี object storage |
| คำนวณ metric/event จาก engine ลงฐานข้อมูล | ยังไม่มีตัวเติมข้อมูล |
| WebSocket gateway, rate limiting, backup/restore ที่ทดสอบแล้ว | ยังไม่มี |
| TimescaleDB, docker compose | เขียนไว้ ยังไม่ได้ทดสอบ |
| ตรวจกฎหมาย (ก.ล.ต.) และสิทธิ์ข้อมูล | ยังไม่ได้ทำ ต้องได้คำตอบก่อนเปิดให้คนนอกใช้ |

## เรื่องที่เจ้าของต้องตัดสินใจ

ดู `server/README.md` ข้อ 12: trades ไม่เป็น hypertable, สมาชิกที่ไม่มีแถวสิทธิ์ = ข้อมูลหน่วง, user id = UUIDv5(iss|sub),
DELETE เป็นการลบถาวร, breakeven = ผลสุทธิเป็นศูนย์พอดี และใช้ average-cost, instrument ที่ ingest สร้างเป็น `unverified`
