# Flow Lab — ต้นแบบแอป (รอบ 1: หน้าจอ 4 หน้า ด้วยข้อมูลตัวอย่าง)

PWA ภาษาไทย ใช้ได้ทั้งมือถือ (เริ่มที่ 390px รองรับถึง 320px) และคอม (1440px)
สร้างตาม `DESIGN-SPEC.md` และ `CLAUDE-HANDOFF.md` เป็น **เครื่องมือวิเคราะห์ ไม่มีระบบแนะนำจุดเข้า/SL/TP
และไม่มีปุ่มซื้อขาย**

> รุ่นปกติ (production) ข้อมูลตลาดเป็น **ข้อมูลตัวอย่าง** (มีป้ายบนทุกหน้า) เว้นแต่ตั้ง URL ของเซิร์ฟเวอร์ Flow Lab ·
> รุ่น claude.ai artifact (ดูส่วนตัวของเจ้าของ) แสดง **ข้อมูลจริงจาก Binance ผ่าน connector ของเจ้าของเอง** แบบอ่านอย่างเดียว
> มีป้าย "ข้อมูลจริงจาก Binance ผ่าน connector ของคุณ · ดูส่วนตัว ห้ามเผยแพร่/ขาย" บนทุกหน้า (ดูหัวข้อ "ดูข้อมูลจริงส่วนตัวผ่าน connector")

## เริ่มใช้งาน

```bash
cd orderflow-lab/app
npm install
npm run dev            # เปิด http://localhost:5173
npm test               # unit tests (vitest)
npm run build          # ตรวจ TypeScript แล้ว build รุ่น production ลง dist/ (ไม่มีโค้ด connector)
npm run build:artifact # build รุ่น claude.ai artifact (vite --mode artifact) ลง dist/artifact/ (มี connector ของเจ้าของ)
npm run build:single   # ทั้งสองรุ่น → dist/flow-lab-single.html (production) + dist/flow-lab-artifact.html (artifact) แล้วตรวจ bundle
npm run check:bundle   # ล้มเหลวถ้า bundle production มี "tool_execute" / "callTool" / ชื่อ endpoint ของ Binance
NODE_PATH=$(npm root -g) node e2e/check.mjs           # ทดสอบทุก flow ของรุ่น production ใน Chromium ที่ 320/390/1440 px (ต้อง build ก่อน, ต้องมี playwright)
NODE_PATH=$(npm root -g) node e2e/check-artifact.mjs  # ทดสอบ dist/flow-lab-artifact.html กับ connector จำลอง (ต้อง build:single ก่อน)
DATA=<โฟลเดอร์ข้อมูลของ collector> NODE_PATH=$(npm root -g) node e2e/check-api.mjs   # รุ่น production กับเซิร์ฟเวอร์ที่รันจริง (PostgreSQL + ingest + API)
```

## หน้าจอ

| หน้า | ทำอะไรได้ |
|---|---|
| ภาพรวม | เลือกสินทรัพย์ (≤5), ราคา + ชนิดราคา + เปลี่ยนแปลง 24 ชม., ตลาด/หน่วย/สกุลราคา, ป้ายความสดรายสินทรัพย์พร้อม UTC+7, "สิ่งที่เกิดขึ้น" ≤3 ข้อพร้อมปุ่มดูหลักฐาน |
| วิเคราะห์ | กราฟลาก/ซูมได้ (Lightweight Charts) พร้อมปุ่ม "แท่งล่าสุด" และ "รีเซ็ตมุมมอง", แท่งเทียนกับช่วงข้อมูลเลือกแยกกัน, ชั้นข้อมูลแบบเดียวกับ desk: **กำแพง** (เส้นราคา ทึบ = อยู่มา ≥ 5 นาที ประ = ใหม่ หนา = ใหญ่), **ไม้ใหญ่** (จุดใต้แท่ง = ซื้อ บนแท่ง = ขาย), VWAP, Assist ±2σ/±3σ, POC/Value area, Range วันก่อนหน้า (Y-Hi/Top/Y-POC/Bottom/Y-Lo), เลขกลม, แผง CVD และแผง **OI** (เฉพาะ perpetual) · เส้นบอกค่าแท่งใต้เมาส์: O/H/L/C, V, ซื้อ/ขาย/Δ และกำแพงใกล้ราคานั้น · ชั้นที่วาดไม่ได้บอกเหตุผลใต้กราฟ · ไม่มีชั้น "แผน" (จุดเข้า/SL/TP) เพราะแอปสมาชิกเป็นเครื่องมือวิเคราะห์ แผนอยู่ใน desk ส่วนตัวเท่านั้น · ราคา last/mark/index แยกชนิด, แผง "กำแพงและไม้ใหญ่" "OI และ Funding" "คุณภาพข้อมูล", คำอธิบายแบบ bottom sheet, ปุ่มบันทึกภาพตลาด |
| แจ้งเตือน | สร้างกฎ (สินทรัพย์+ตลาด, ข้อมูลที่ใช้, เงื่อนไข, เกณฑ์+หน่วย, ช่วงเวลา, พักแจ้งซ้ำ, ช่องทาง, เปิด/ปิด), ประวัติแสดงค่าที่วัดได้เทียบเกณฑ์, แยกเหตุการณ์ย้อนหลังจากเหตุการณ์สด, พักกฎเมื่อข้อมูลที่ใช้ไม่ปกติ, กันแจ้งซ้ำด้วย dedupe key |
| บันทึกของฉัน | ข้อสังเกต (ภาพกราฟ ณ เวลาบันทึก + ค่าที่วัดได้ + เหตุผล + บททบทวนแยก + ประวัติแก้ไข) และเทรดส่วนตัวที่กรอกเอง (fills, ค่าธรรมเนียม, funding, ผลสุทธิ, Net R, Win rate ตามนิยาม), ส่งออก CSV/JSON |

## โครงสร้างโค้ด

```
src/data/types.ts      รูปแบบข้อมูลที่ทุกหน้าใช้
src/data/source.ts     interface MarketSource + ข้อความ error ที่ผู้ใช้เข้าใจ
src/data/demo.ts       DemoSource: ข้อมูลตัวอย่างแบบ deterministic (ป้าย demo)
src/data/api.ts        ApiSource: เรียกเฉพาะ API ของ Flow Lab (server/) ไม่เรียก exchange หรือ connector (e2e ตรวจว่าไม่มี host อื่น)
src/data/connector.ts  ConnectorSource: Binance ผ่าน connector ของเจ้าของ (มีเฉพาะในรุ่น artifact) อ่านอย่างเดียวตาม allowlist
src/domain/engine.ts   พอร์ต TypeScript ของ web/engine.js (กำแพง + tracker, เกณฑ์ไม้ใหญ่, profile) เทสต์เทียบผลกับ JS ตัวจริง
src/lib/build.ts       บอกว่าเป็นรุ่น artifact หรือไม่ (import.meta.env.MODE)
src/domain/            indicators (CVD/VWAP/profile), decimal, accounting, stats, alerts
src/journal/           store (IndexedDB/หน่วยความจำ, idempotent, revisions, snapshot แก้ไม่ได้), export
src/pages/             4 หน้า
e2e/check.mjs          ทดสอบ flow จริงในเบราว์เซอร์ + ถ่ายภาพ
e2e/check-api.mjs      เปิดเซิร์ฟเวอร์จริง (server/devstack.py) แล้วเทียบตัวเลขบนจอกับคำตอบของ API
```

สลับแหล่งข้อมูลได้ที่ ตั้งค่า → แหล่งข้อมูลตลาด หน้าจอไม่ต้องแก้

## ยังเป็นข้อมูลตัวอย่าง หรือยังเชื่อมไม่ได้ (ตรวจแล้ว)

| เรื่อง | สถานะ |
|---|---|
| ราคา, แท่งเทียน, CVD, OI, funding, กำแพง, ไม้ใหญ่, เหตุการณ์ | รุ่น production: **ข้อมูลจริงจากเซิร์ฟเวอร์ Flow Lab** เมื่อตั้ง URL + token (ตั้งค่า → แหล่งข้อมูลตลาด) ถ้าไม่ตั้งเป็น **ข้อมูลตัวอย่าง** จาก `DemoSource` · รุ่น artifact: **ข้อมูลจริง** จาก Binance ผ่าน connector ของเจ้าของ (ตารางด้านล่าง) ถ้า connector ใช้ไม่ได้จะกลับเป็นข้อมูลตัวอย่างพร้อมบอกเหตุผล |
| ApiSource | เรียก `/v1/instruments`, `/bars`, `/quote`, `/details`, `/observations` และ `/v1/alerts/events` ตาม `server/openapi.json` (เทสต์ `src/test/api.test.ts` ใช้ response ที่บันทึกจาก API จริงโดย `server/tests/test_frontend_contract.py`); ค่าที่ไม่มีเป็น null ไม่เติม 0; "ข้อมูลสด" เฉพาะสิทธิ์ realtime และเวลาของตลาดไม่เกิน 120 วินาที, สมาชิกไม่มีแพ็กเกจ = "ข้อมูลหน่วง" 15 นาที; token ผิดขึ้น "ต้องเข้าสู่ระบบใหม่" · **ทดสอบกับเซิร์ฟเวอร์ที่รันจริงแล้ว**: `e2e/check-api.mjs` เปิด PostgreSQL + ingest ไฟล์จาก collector + API (`server/devstack.py`) แล้วตรวจว่าตัวเลขบนจอตรงกับคำตอบของ API |
| เข้าสู่ระบบ / สมาชิก / สิทธิ์ข้อมูลหน่วง | สิทธิ์หน่วง/realtime ทำงานที่เซิร์ฟเวอร์และแอปแสดงตามนั้นแล้ว แต่ยังไม่มีหน้าล็อกอินจริง: ใช้ token สำหรับทดสอบจาก `server/devstack.py` |
| บันทึกและเทรดส่วนตัว | เก็บ **ในเบราว์เซอร์เครื่องนี้เท่านั้น** (IndexedDB) ทุกรายการมีป้าย "ร่างในเครื่อง" ไม่มีข้อความว่าบันทึกบนเซิร์ฟเวอร์แล้ว ล้างข้อมูลเบราว์เซอร์แล้วหาย |
| แจ้งเตือน | ประเมินกฎในหน้าแอปขณะเปิดอยู่เท่านั้น Telegram ยังไม่พร้อม (ต้องผูกผ่านเซิร์ฟเวอร์) |
| ภาพกราฟในบันทึก | เก็บเป็น JPEG data URL + SHA-256 ในเครื่อง ยังไม่มี object storage |
| PWA | มี manifest และ service worker ที่ cache เฉพาะไฟล์แอป ไม่ cache ข้อมูลสมาชิก ทำงานเมื่อเปิดผ่าน https บนโดเมนของเราเท่านั้น (ใน claude.ai artifact จะไม่ลงทะเบียน) |
| Dark mode | ยังไม่มี: DESIGN-SPEC กำหนดชุดสีสว่างชุดเดียว |

## ดูข้อมูลจริงส่วนตัวผ่าน connector (เฉพาะรุ่น claude.ai artifact)

เจ้าของเปิด `dist/flow-lab-artifact.html` เป็น artifact ส่วนตัวใน claude.ai แล้วแอปเรียก Binance ผ่าน connector Binance
ในบัญชี claude.ai ของเจ้าของเอง แบบเดียวกับ `desk/template.html`: `window.claude.use("mcp")` →
`callTool("Binance", "tool_execute", { toolName, arguments }, { cache: false })` ไม่มีเซิร์ฟเวอร์ของเรา ไม่เก็บข้อมูลไว้ที่ใด
ข้อมูลนี้ **ดูส่วนตัว ห้ามเผยแพร่/ขาย** และแยกจากข้อมูลตัวอย่างและจาก API สมาชิกด้วยป้ายสีม่วงบนทุกหน้า

- **อ่านอย่างเดียว**: ทุกการเรียกผ่าน allowlist ชุดเดียวกับ desk ก่อนส่ง (klineCandlestickData, orderBook, compressedAggregateTradesList,
  symbolPriceTicker, openInterestStatistics, markPrice, topTraderLongShortRatioPositions, takerBuySellVolume ของ USDⓈ-M และ
  spot.klines, spot.depth, spot.aggTrades) ชื่ออื่นถูกปฏิเสธก่อนถึง connector ไม่มีคำสั่งซื้อขาย บัญชี หรือโอนเงิน
- **ตอนเผยแพร่ artifact** ต้องประกาศ `capabilities: { mcp: { servers: [{ server: "Binance", tools: ["tool_execute"] }] } }`
  (และ `downloads` ถ้าจะส่งออกไฟล์จากหน้าบันทึก) · `tool_execute` ของ connector เรียกเครื่องมือ Binance ได้ทุกตัว
  ตัวกันจึงเป็น allowlist ในหน้า (`BIN_ALLOW` ใน `src/data/connector.ts`, เทสต์เทียบกับ desk ทุกครั้ง)
- **ถ้าใช้ไม่ได้** (ยังไม่เชื่อม connector, หมดอายุ, ยังไม่อนุญาต, องค์กรปิด, เปิดนอก claude.ai) แอปแสดงข้อมูลตัวอย่างพร้อมเหตุผล
  ภาษาไทยแบบเดียวกับ desk และปุ่ม "อนุญาตและดึงข้อมูล" / "ลองเชื่อมต่ออีกครั้ง" เมื่อกดแล้วมีโอกาสสำเร็จ

| ข้อมูล (รุ่น artifact) | มาจาก | จริงหรือประมาณ |
|---|---|---|
| ราคาล่าสุด + เวลา | aggTrade ล่าสุด (ถ้าดึงไม่ได้ใช้ราคาปิดของแท่ง 30 นาทีล่าสุด เวลาเป็นเวลาเปิดแท่ง) | จริง · "ข้อมูลสด" เมื่อเหตุการณ์ล่าสุดของตลาดอยู่ภายใน 120 วินาที (กราฟ: ภายใน 2 แท่งและไม่เกิน 120 วินาที) ไม่งั้น "ข้อมูลค้าง" |
| เปลี่ยนแปลง 24 ชม. | ราคาเปิดของแท่ง 1 นาทีแรกในช่วง 24 ชม. ย้อนหลัง | จริง · ถ้าไม่มีแท่งนั้นขึ้น "ยังไม่มีข้อมูล" ไม่ใช้ช่วงอื่นแทน |
| แท่งเทียน 1m/5m/15m/1h, CVD | klines ของ Binance (ช่วง 3 วันดึงหลายหน้า) ใช้ taker buy base volume | จริง · CVD คำนวณตรงจากปริมาณผู้กดซื้อ/ขาย ไม่ประมาณจากรูปแท่ง |
| VWAP, POC/value area | คำนวณจากแท่ง (สูตร profile จาก web/engine.js) | ประมาณจากแท่ง (เขียนไว้ใต้กราฟ) |
| กำแพง + สถานะ + กำแพงที่หาย | snapshot ของ order book ทุกครั้งที่หน้าขอข้อมูล → WallTracker ของ engine เก็บในหน่วยความจำต่อสินทรัพย์ | จริงจาก book แต่เห็นเฉพาะตอนที่หน้าดึง · อายุนับจากที่หน้านี้เห็นครั้งแรก · ไม่ได้ดูเกิน 90 วินาที = ปิดเป็น "ไม่ทราบ" แล้วนับใหม่ |
| ไม้ใหญ่ | aggTrades ที่หน้านี้ดึงมา (เก็บย้อนสูงสุด 4 ชม.) เกณฑ์ ≥ เปอร์เซ็นไทล์ที่ 99 | จริง · บอกจำนวนรายการ เวลาเริ่ม และช่วงที่ดึงไม่ทัน |
| OI | openInterestStatistics ราย 5 นาที (หน่วยเป็นสินทรัพย์ฐาน BTC / XAU) เปลี่ยนใน 5 นาที / 1 ชม. เทียบจุดที่ห่างพอดี | จริง (สถิติ 5 นาที ไม่ใช่ค่าทันที) · spot ไม่มี |
| Funding, mark, index | markPrice (อัตราประมาณการรอบถัดไป, เวลาชำระถัดไป) | จริง · spot ไม่มี |
| liquidation | ไม่ได้ดึง (ไม่อยู่ใน allowlist) | ขึ้นว่า "ไม่มีข้อมูลนี้" |
| คุณภาพข้อมูล | ต่อสตรีมจากผลการเรียกครั้งล่าสุดและเวลาของตลาด | ดึงพลาด = "หลุดการเชื่อมต่อ" พร้อมเหตุผล และแสดงค่าล่าสุดที่ดึงได้ |
| แจ้งเตือน | ประเมินในหน้าจากข้อมูลเดียวกันทุก 15 วินาทีขณะเปิดหน้า ไม่มีประวัติจากที่อื่น | ไม่มีการแจ้งเมื่อปิดหน้า |

**การแยก build**: โค้ด connector ถูกโหลดด้วย `import("./data/connector")` ในเงื่อนไข `import.meta.env.MODE === "artifact"`
(`src/state.tsx`) Vite แทนค่า MODE เป็นตัวอักษรตอน build รุ่น production จึงตัดทั้งกิ่งและไฟล์ connector ออก ส่วนรุ่น artifact
รวมทุกอย่างเป็นสคริปต์เดียว (`inlineDynamicImports`) เพื่อใส่ในหน้าเดียว `scripts/check-bundle.mjs` ตรวจว่า bundle production
ไม่มี "tool_execute", "callTool" หรือชื่อ endpoint และตรวจว่ารุ่น artifact มีจริง ตัวเลือก "Binance ผ่าน connector ของคุณ (ดูส่วนตัว)"
ในหน้าตั้งค่าก็มีเฉพาะรุ่น artifact

**token สำหรับทดสอบ API**: ในตั้งค่า → เซิร์ฟเวอร์ Flow Lab มีช่อง "token สำหรับทดสอบ" เก็บใน sessionStorage ของแท็บนั้นเท่านั้น
(ไม่ใช่ localStorage และไม่ใส่ใน URL) ส่งเป็น `Authorization: Bearer` ผ่าน `new ApiSource(url, () => token)`

## การเข้าถึง (accessibility) ที่ทดสอบอัตโนมัติ

ปุ่มและช่องกรอกทุกตัวกว้าง ≥ 44px สูง ≥ 40px (สวิตช์และลิงก์ในกราฟขยายพื้นที่สัมผัสด้วยขอบโปร่งใส),
ไม่มีการเลื่อนแนวนอนที่ 320/390/1440px, กล่องโต้ตอบรับ focus และปิดด้วย Escape, ฝั่งซื้อขายใช้ ▲/▼ + ข้อความร่วมกับสี,
กราฟมีคำอธิบายสำหรับโปรแกรมอ่านหน้าจอ คู่สีข้อความกับพื้นหลังทุกคู่ในชุดสีวัดแล้วผ่าน WCAG AA (≥ 4.5:1)
