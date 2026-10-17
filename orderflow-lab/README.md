# Order Flow Lab — เครื่องมืออ่าน order flow ทองคำและ BTC (ระยะ 1)

ห้องทดลองสำหรับสร้างเครื่องมืออ่าน order flow ของเราเอง ใช้ **ข้อมูลสาธารณะฟรี**
ทั้งหมด ไม่ต้องสมัครหรือใส่ API key รันด้วย Python 3 มาตรฐานล้วน ๆ (ไม่มี
numpy/pandas) เพื่อให้รันได้ทุกที่ รวมถึง cron ราคาถูก

> ระยะ 1 = เก็บข้อมูล + engine คำนวณ + รายงานบน terminal + **หน้าเว็บ dashboard แบบ static**
> พร้อม unit test ยังไม่มีระบบสมาชิก และ **ไม่ใช่คำแนะนำการลงทุน**

## แหล่งข้อมูลและข้อจำกัด (พูดตรง ๆ)

| สินทรัพย์ | แหล่ง | ได้อะไร | ทำฟีเจอร์ไหนได้ |
|---|---|---|---|
| **BTC** | OKX, Kraken, Coinbase (REST สาธารณะ) | order book หลายร้อยระดับ + trade ทีละรายการที่ระบุฝั่งผู้กดซื้อ/ขาย | **ครบ** CVD, กำแพง, big trades, volume profile, VWAP±σ |
| **ไฟล์แท่งเทียนใด ๆ** | ไฟล์ JSONL `{t,o,h,l,c,v}` ที่คุณมีสิทธิ์ใช้ | แท่งเทียน + volume | volume profile, Range/POC, VWAP±σ, เลขกลม, **CVD แบบประมาณ** (`report.py --bars`) |
| **โทเคนทอง** (XAUT, PAXG) | exchange เดียวกับ BTC | order book + trade เหมือน BTC | เหมือน BTC แต่เป็น **ตัวแทน** ของทอง ไม่ใช่ COMEX และตลาดบางกว่ามาก |

สิ่งที่ **ไม่มี** ในระยะนี้: order book และ tape ของ COMEX GC จริง (ต้องซื้อข้อมูล
ระดับ MBO) ทุกค่าที่คำนวณจากแท่งเทียนอย่างเดียวจะติดป้าย `approximate` ทั้งใน
JSON และในรายงาน

repo นี้ไม่เก็บข้อมูลตลาดของบุคคลที่สาม เทสต์ใช้ข้อมูลจำลอง (`tests/synth.py`) ·
ข้อมูลจาก TradingView ห้ามนำมาใส่ repo หรือใช้ทำบริการ: Terms of Use ข้อ 3 ให้สิทธิ์
แค่การแสดงผล และห้ามใช้เชิงพาณิชย์ · ก่อนเปิดบริการที่เก็บเงิน ให้ตรวจเงื่อนไข API
ของแต่ละ exchange ด้วย

## โครงสร้าง

```
orderflow-lab/
  collector.py        ดึง order book + trades ทุก N วินาที → data/*.jsonl
  report.py           สรุปผลจากข้อมูลที่เก็บ หรือจากไฟล์แท่งเทียน
  config.json         รายชื่อแหล่งข้อมูล, tick ของ profile, เกณฑ์ต่าง ๆ
  feeds/              parser ของแต่ละ exchange → รูปแบบเดียวกัน (Trade / BookSnapshot / Bar)
  web/
    index.html        หน้า dashboard (เปิดได้เลย ไม่ต้องมี server)
    app.js            ดึงข้อมูลจาก exchange ในเบราว์เซอร์ + วาดกราฟด้วย canvas
    engine.js         port ของ engine/ เป็น JavaScript (มีเทสต์ยืนยันว่าให้ค่าตรงกัน)
  engine/
    bars.py           รวม trades เป็นแท่งเทียน
    cvd.py            CVD, delta รายช่วง, CVD แบบประมาณจากแท่ง, divergence
    profile.py        volume profile, POC, value area (70%) / magnet (30%), HVN/LVN
    vwap.py           VWAP ±1σ/2σ/3σ ตำแหน่งราคา และรูปทรงแถบ (flat/expanding)
    walls.py          ตรวจกำแพง + ติดตามอายุ/สถานะ (new/holding/adding/pulling) และผล (filled/pulled)
    bigtrades.py      ไม้ใหญ่ (เกณฑ์ percentile) จัดกลุ่มตามราคา + โซน contested
    levels.py         เลขกลม
    summary.py        รวมทุกอย่างเป็น dict เดียว
  tests/              unit tests (unittest) รวม fixture จาก API จริง
    test_js_parity.py รัน engine.js ด้วย node เทียบกับ Python ทีละตัวเลข (ข้ามถ้าไม่มี node)
    render_check.js   เปิดหน้าเว็บใน Chromium แบบ headless แล้วจับภาพ (ต้องมี playwright)
    synth.py          ข้อมูลแท่งเทียนจำลองสำหรับเทสต์
```

## หน้าเว็บ dashboard (`web/`)

หน้าเดียว ไม่มี server: เบราว์เซอร์ของผู้ใช้ดึง order book และ trade จาก API สาธารณะ
ของ OKX / Kraken / Coinbase โดยตรง (ทั้งสามเปิด CORS) แล้วคำนวณด้วย `engine.js`

* เปิดผ่าน GitHub Pages ของ repo นี้ที่ `/orderflow-lab/web/` (หลังรวมเข้า master)
  หรือรันในเครื่อง: `python3 -m http.server 8000` ที่โฟลเดอร์ราก แล้วเปิด
  `http://localhost:8000/orderflow-lab/web/`
* เลือกแหล่งข้อมูล (BTC 3 ตลาด / โทเคนทอง 3 ตลาด), ช่วงเวลา และขนาดแท่ง
* แสดง: แท่งเทียน + VWAP ±1σ/2σ + เลขกลม + ไม้ใหญ่ (วงกลมขนาดตามปริมาณ),
  volume profile แยกซื้อ|ขาย พร้อม POC / โซน 70% / แม่เหล็ก 30%, แถบกำแพงด้านขวา,
  กราฟ CVD, ตารางกำแพง (สถานะ/อายุ/ผลตอนหาย), โซนไม้ใหญ่ และ delta รายแท่ง
* รหัสสี: ฝั่งซื้อ/BID/แท่งขึ้น = เขียวทึบ, ฝั่งขาย/ASK/แท่งลง = แดงกลวง (รูปทรงเป็นตัวแยก
  ที่สอง สำหรับคนตาบอดสี) · รองรับโหมดมืดตามระบบ และปุ่มสลับ
* **ข้อจำกัด:** เริ่มเปิดหน้าจะมีข้อมูลแค่ trade ล่าสุด 500–1,000 รายการ (ไม่กี่นาที)
  แล้วค่อย ๆ สะสมขณะเปิดหน้าค้างไว้ ปิดแท็บ = ข้อมูลหาย ถ้าต้องการเก็บยาวใช้
  `collector.py` แทน · หน้าไม่ส่งอะไรไปที่ไหนนอกจากเรียก API ของ exchange

### ข้อตกลงสำคัญ
* `Trade.side` คือ **ฝั่งผู้กดซื้อ/ขาย (taker)** เสมอ Coinbase ส่งฝั่ง maker มา
  parser จะกลับด้านให้ ถ้าพลาดตรงนี้ CVD จะกลับเครื่องหมายทั้งหมด
* ขนาดเป็นหน่วยของตลาดนั้น (BTC, PAXG, สัญญา) เกณฑ์ big trade ใช้ percentile
  จึงใช้ได้ทุกหน่วย
* กำแพง = ระดับที่ใหญ่กว่ามัธยฐานของ 200 ระดับใกล้ราคา ≥ 4 เท่า และอยู่ห่างจาก
  ราคาไม่เกิน 1% (ปรับได้ใน `config.json`) ภาพนิ่งภาพเดียวบอกไม่ได้ว่ากำแพงจริงไหม
  ตัวติดตามจึงบันทึกว่ามันอยู่นานแค่ไหน โตหรือหด และตอนหายไปราคา "ไปถึง" (filled)
  หรือ "ถอนก่อน" (pulled) กำแพงที่เห็นแค่ครั้งเดียวแล้วหายถือเป็น noise ไม่บันทึก

## ใช้งาน

```bash
cd orderflow-lab
python3 -m unittest discover -s tests        # unit tests ทั้งหมด (รวม parity JS↔Python ถ้ามี node)
node tests/render_check.js                   # เปิด dashboard ใน Chromium แล้วจับภาพ (ต้องมี playwright)

python3 collector.py --once                   # ดึงทุกแหล่งหนึ่งรอบ
python3 collector.py --loop --interval 5      # เก็บต่อเนื่อง (Ctrl-C หยุด)
python3 collector.py --loop --only okx:BTC-USDT,kraken:PAXGUSD

python3 report.py --source okx:BTC-USDT --window-min 60
python3 report.py --source kraken:PAXGUSD --interval 5m
python3 report.py --bars my_bars.jsonl                  # โหมดแท่งเทียน จากไฟล์ของคุณ
python3 report.py --source okx:BTC-USDT --json          # สำหรับโปรแกรมอื่น
```

## การเก็บข้อมูลระยะยาว
`collector.py --loop` เขียนต่อท้ายไฟล์ JSONL เรื่อย ๆ และจำ trade id ล่าสุดไว้ใน
`data/state.json` จึงหยุด/รันใหม่ได้โดยไม่นับซ้ำ ข้อมูลสด ๆ ถูก git ignore
ไว้ทั้งหมด ถ้าจะเก็บนานเป็นวันควรรันบนเครื่องของคุณ
หรือ VPS แล้วค่อยนำไฟล์มา backtest

## ถัดไป (ระยะ 2)
1. backtest: สัญญาณจาก CVD/กำแพง/big trades ของโทเคนทองทำนายทิศ GC ได้จริงไหม
2. backend ที่เก็บข้อมูลต่อเนื่องและเสิร์ฟให้หน้าเว็บ (ตอนนี้หน้าเว็บเริ่มจากศูนย์ทุกครั้ง)
3. ระบบสมาชิก เมื่อข้อ 1 ให้ผลดีพอ

## ข้อสงวนสิทธิ์
เครื่องมือนี้เพื่อการศึกษาและวิจัยเท่านั้น ไม่ใช่คำแนะนำการลงทุน ค่าที่คำนวณได้อาจ
ไม่ครบหรือผิดพลาด ตรวจสอบกับข้อมูลสดก่อนตัดสินใจทุกครั้ง
