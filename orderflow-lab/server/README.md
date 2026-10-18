# Flow Lab — backend (FastAPI + PostgreSQL)

สถานะ 18 ต.ค. 2026: **reference implementation ที่ผ่านเทสต์ในเครื่องทดสอบ** (PostgreSQL 16 แบบ local,
Python 3.13) ยังไม่ใช่ระบบ production และยังไม่เคยรันกับ TimescaleDB, Docker หรือ identity provider จริง

Flow Lab เป็นเครื่องมือวิเคราะห์ตลาดและสมุดบันทึกส่วนตัว **ไม่มีจุดเข้า/SL/TP และไม่สร้างสัญญาณซื้อขาย**
(ROADMAP ข้อ 6.1) backend นี้ไม่เรียก exchange หรือ trading API ใด ๆ ข้อมูลตลาดอ่านจากฐานข้อมูลที่
`ingest.py` โหลดจากไฟล์ของ collector (`live/`) เท่านั้น

> **สิทธิ์ข้อมูล:** การแสดงข้อมูลตลาด (รวมถึงค่าที่คำนวณต่อ เช่น CVD, กำแพง) ให้สมาชิกหรือคนนอกใช้ ต้องมี
> data licence จากแหล่งข้อมูลก่อน — ดู ROADMAP ข้อ 4 (4.2 Binance, 4.3 CME) ระหว่างนี้ใช้ได้เฉพาะงานวิจัยภายใน
>
> **เส้นทางข้อมูล:** เบราว์เซอร์/PWA ของสมาชิกต้องคุยกับ API นี้เท่านั้น ห้ามเรียก exchange, `window.claude`
> หรือ connector ของ claude.ai โดยตรง และห้ามฝัง key หรือ credential ใน frontend

## โครงสร้างไฟล์

```
server/
  migrations/0001_init.sql   schema, roles, RLS, triggers กันแก้ (TimescaleDB เป็น optional)
  migrate.py                 รัน migration ตามลำดับ ครั้งเดียวต่อไฟล์ (ตาราง schema_migrations + checksum)
  ingest.py                  โหลดไฟล์รายชั่วโมงของ collector แบบ idempotent + บันทึก data_quality
  app/                       FastAPI
    main.py                  create_app(), middleware (ปฏิเสธ credential ใน URL, no-store, access log ไม่มี token)
    auth.py                  ตรวจ Bearer JWT (HS256 สำหรับ dev/test)
    db.py                    connection pool, transaction ต่อ request พร้อม app.user_id
    common.py                entitlement (ข้อมูลหน่วง), decimal, cursor
    accounting.py            สูตรบัญชีเทรด (Decimal ล้วน) + สถิติ
    alerts_engine.py         บันทึก alert event แบบ idempotent (ใช้โดย worker ฝั่ง server)
    snapshots.py             สร้าง market snapshot จากข้อมูลในฐาน แล้วแช่แข็ง
    routers/                 me, market, journal, trades, alerts, export
    devtoken.py              สร้าง token สำหรับ dev
    openapi_export.py        เขียน openapi.json จากโค้ด
  openapi.json               API contract (สร้างจากแอป มีเทสต์ตรวจว่าตรงกับโค้ด)
  tests/                     pytest: เปิด PostgreSQL ชั่วคราวเอง
  docker-compose.yml         ทางเลือก TimescaleDB (ยังไม่ได้ทดสอบ)
  .env.example               ตัวอย่างตัวแปร (ไม่มีค่าจริง)
```

## 1. ติดตั้ง

```bash
cd orderflow-lab/server
python3 -m venv .venv && . .venv/bin/activate     # แนะนำ
pip install -r requirements.txt
```

## 2. เริ่ม PostgreSQL

### ก. ใช้ binaries ในเครื่อง (ทางที่ทดสอบแล้ว)

PostgreSQL ไม่ยอมรันด้วย root ถ้าเป็น root ให้ใส่ `runuser -u postgres --` หน้าคำสั่ง `initdb`/`pg_ctl`
และ `chown postgres` โฟลเดอร์ข้อมูลก่อน

```bash
PGBIN=/usr/lib/postgresql/16/bin
$PGBIN/initdb -D ~/flowlab-pg -U flowlab_owner --auth=scram-sha-256 --pwprompt -E UTF8 --locale=C.UTF-8
$PGBIN/pg_ctl -D ~/flowlab-pg -o "-p 5432 -c listen_addresses=127.0.0.1 -c timezone=UTC" -l ~/flowlab-pg.log start
psql -h 127.0.0.1 -U flowlab_owner -d postgres -c "CREATE DATABASE flowlab"
```

### ข. docker compose + TimescaleDB (ยังไม่ได้ทดสอบ)

```bash
cp .env.example .env        # ตั้ง FLOWLAB_DB_OWNER_PASSWORD
docker compose up -d db
```

ไฟล์ migration สร้าง hypertable ให้ `klines` และ `book_snapshots` เฉพาะเมื่อมี extension `timescaledb`
ถ้าไม่มีจะใช้ตาราง PostgreSQL ปกติ ส่วน `trades` เป็นตารางปกติเสมอ (ดู "เรื่องที่เจ้าของควรตัดสินใจ")

## 3. Migration และ role

```bash
export FLOWLAB_ADMIN_DATABASE_URL=postgresql://flowlab_owner:...@127.0.0.1:5432/flowlab
python3 migrate.py            # รอบสองจะตอบ "nothing to apply"
python3 migrate.py --status
```

migration สร้าง group role `flowlab_app` (API) และ `flowlab_ingest` (โหลดข้อมูลตลาด) ให้เอง จากนั้นสร้าง
login role (ทำครั้งเดียว ด้วยรหัสผ่านจริงของคุณ):

```sql
CREATE ROLE flowlab_api      LOGIN PASSWORD '...' IN ROLE flowlab_app;
CREATE ROLE flowlab_ingestor LOGIN PASSWORD '...' IN ROLE flowlab_ingest;
```

API จะ**ไม่ยอมเริ่ม**ถ้า `FLOWLAB_DATABASE_URL` เป็น superuser, role ที่มี BYPASSRLS, เจ้าของตาราง
หรือไม่ได้อยู่ใน `flowlab_app` เพราะ RLS จะไม่มีผล ห้ามแก้ไฟล์ migration ที่รันไปแล้ว (checksum จะไม่ตรงและ
`migrate.py` จะหยุด) ให้เพิ่มไฟล์ใหม่ `0002_...sql` แทน

## 4. ตัวแปรสภาพแวดล้อม

แอปอ่านจาก environment เท่านั้น (ไม่อ่านไฟล์ `.env` เอง ใช้ `set -a; . ./.env; set +a` ถ้าต้องการ)

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `FLOWLAB_DATABASE_URL` | — (ต้องมี) | login role ของ API (อยู่ใน `flowlab_app`) |
| `FLOWLAB_ADMIN_DATABASE_URL` | — | owner สำหรับ `migrate.py` เท่านั้น |
| `FLOWLAB_INGEST_DATABASE_URL` | — | login role ของ `ingest.py` (อยู่ใน `flowlab_ingest`) |
| `FLOWLAB_JWT_SECRET` | — (ต้องมี ≥ 32 ไบต์) | secret HS256 สำหรับ dev/test เท่านั้น |
| `FLOWLAB_JWT_ISSUER` / `FLOWLAB_JWT_AUDIENCE` | `flowlab-dev` / `flowlab-api` | ค่า `iss`/`aud` ที่ต้องตรง |
| `FLOWLAB_ENV` | `dev` | `dev`, `test`; `production` ถูกปฏิเสธจนกว่าจะมีการตรวจ JWKS |
| `FLOWLAB_DELAY_MINUTES` | `15` | ความหน่วงของสิทธิ์ delayed |
| `FLOWLAB_DEFAULT_ENTITLEMENT` | `delayed` | สิทธิ์ของสมาชิกที่ไม่มีแถวใน `entitlements` (`delayed` หรือ `none`) |
| `FLOWLAB_CORS_ORIGINS` | ว่าง | origin ของ PWA คั่นด้วย `,` |
| `FLOWLAB_DB_POOL_MIN` / `MAX` | `1` / `10` | ขนาด connection pool |

## 5. รัน API

```bash
export FLOWLAB_DATABASE_URL=postgresql://flowlab_api:...@127.0.0.1:5432/flowlab
export FLOWLAB_JWT_SECRET=...     # ≥ 32 ไบต์
uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8080 --no-access-log

TOKEN=$(python3 -m app.devtoken --sub alice)          # token ทดสอบ อายุ 60 นาที
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/v1/me
```

ใช้ `--no-access-log` เพราะ access log ของ uvicorn บันทึก URL เต็ม แอปมี access log ของตัวเอง
(`flowlab.access`: method, path, status, เวลา, request id ไม่มี query string, header หรือ body)
token ใน URL (`?access_token=` ฯลฯ) ถูกปฏิเสธด้วย 400 เอกสาร API ดูได้ที่ `/docs` หรือ `openapi.json`

สิทธิ์ realtime ตั้งโดย admin/ระบบชำระเงิน (API แก้เองไม่ได้):

```sql
INSERT INTO flowlab.entitlements (owner_id, product, level, valid_until, source)
VALUES ('<user uuid จาก /v1/me>', 'market_data', 'realtime', now() + interval '30 days', 'admin');
```

## 6. โหลดข้อมูลจาก collector

```bash
export FLOWLAB_INGEST_DATABASE_URL=postgresql://flowlab_ingestor:...@127.0.0.1:5432/flowlab
python3 ingest.py --data ../data/live --collector-config ../live/config.json
```

* รันซ้ำได้: ไฟล์ที่ checksum ไม่เปลี่ยนจะข้าม (`ingested_files`) และทุกตารางใช้ business key + `ON CONFLICT DO NOTHING`
  (`--force` อ่านใหม่ทั้งหมดก็ไม่เพิ่มแถวซ้ำ)
* ตัวเลขอ่านเป็น `Decimal` → `NUMERIC` ไม่ผ่าน float; `takerSold` 1 = taker ขาย, 0 = taker ซื้อ, ค่าอื่น = `unknown`
  (ไม่นับเป็นขาย)
* แถวที่ collector backfill มาทีหลังจะอยู่ในไฟล์ชั่วโมงที่ช้ากว่าเวลาเกิดเหตุ: ไม่ถือว่าไฟล์ = ชั่วโมงของเหตุการณ์
  และติดป้าย `quality = late_arrival`
* `meta` เก็บใน `raw_events` (append-only) แล้วสร้าง `data_quality`: ช่วง disconnect/restart (book, kline),
  book resync ถึง book_synced, ช่วง id ที่ backfill/สูญหาย และสแกนหา aggTrade id ที่ขาดเอง ช่วงขาดคงป้ายไว้
  จนกว่าข้อมูลจะมาครบจริง (`open` → `partial`/`unrecoverable` → `recovered` + `repaired_at`)
* instrument ที่สร้างจากชื่อโฟลเดอร์มี `spec_status = unverified`, `tick_size`/`qty_step` เป็น NULL
  (ไม่เดา) ต้องกรอก contract spec จริงก่อนใช้งาน

## 7. รันเทสต์

```bash
cd orderflow-lab/server
python3 -m pytest -q
```

เทสต์เปิด PostgreSQL ชั่วคราวเอง (`initdb` + `pg_ctl` ในโฟลเดอร์ temp, port ว่าง) รัน migration, สร้าง role
แล้วทดสอบผ่าน `TestClient` และ SQL ตรง ถ้าหา binaries ไม่เจอจะ skip พร้อมเหตุผล (กำหนดที่อยู่ได้ด้วย
`FLOWLAB_PG_BIN`; ถ้ารันเป็น root จะรัน server ด้วย OS user `postgres` หรือ `FLOWLAB_PG_OS_USER`)

ผลรันล่าสุด (18 ต.ค. 2026, เครื่องทดสอบ, PostgreSQL 16.15 ไม่มี TimescaleDB): `66 passed`

| ไฟล์ | ครอบคลุม |
|---|---|
| `test_db_schema.py` | migration รันซ้ำไม่ซ้ำ/ฐานใหม่รันสองรอบ, ไฟล์ที่ถูกแก้ถูกปฏิเสธ, RLS ครบทุกตารางรายบุคคล, role ไม่มีสิทธิ์พิเศษ, API ไม่ยอมรันเป็น owner, snapshot/raw_events/fills แก้ไม่ได้, retention purge |
| `test_ownership.py` | A อ่าน/แก้/ลบ/ส่งออกของ B ไม่ได้ ทั้งผ่าน API และ SQL ตรงในฐานะ role ของ API (ไม่มี `app.user_id` → 0 แถว), JWT ปลอม/หมดอายุ/`alg=none`, token ใน URL, log ไม่มี token |
| `test_api_journal.py` | retry ด้วย Idempotency-Key เดิม (รวมแบบพร้อมกัน) ได้แถวเดียว, key เดิม body ต่าง → 409, cursor + ช่วงวันที่, PATCH สร้าง revision, ลบ + tombstone, snapshot คงเดิมเมื่อมีข้อมูลมาทีหลัง, snapshot ตามสิทธิ์ delayed |
| `test_market_delayed.py` | delayed ไม่ได้แถวใหม่กว่า now−15m ใน bars (kline/trades ทุก tf), quality, instruments, metric-events; unknown side ไม่นับเป็นขาย; วันไม่มีข้อมูลไม่แสดง 100% |
| `test_accounting.py` | partial fills, partial exits, BE หลัง TP1, fee สกุลอื่น/ไม่ทราบ, funding, inverse vs linear, multiplier, ปัดเศษ Decimal ครั้งเดียว, net R N/A, correction, นิยามสถิติ |
| `test_api_trades.py` | สเปกสัญญาบังคับ, lifecycle + ล็อก risk, import_key, correction, inverse + funding ผ่าน API, สถิติ 520 เทรดทั้งช่วง |
| `test_alerts.py` | validation/CRUD, dedupe_key ไม่สร้างซ้ำ, backfill ไม่ส่งสด, cooldown, พักกฎเมื่อคุณภาพข้อมูลไม่พอ, delayed, ack |
| `test_export_openapi.py` | export JSON/CSV ของตัวเอง (กัน CSV formula injection), openapi.json ตรงกับโค้ด, ทุก `/v1` ต้องใช้ Bearer |
| `test_frontend_contract.py` | บันทึก response จริงของ `/v1/instruments` และ `/bars` เป็น fixture ใน `app/src/test/fixtures/server-*.json` ที่เทสต์ฝั่งแอปใช้ แล้ว fail ถ้า API เปลี่ยนจน fixture ไม่ตรง (สร้างใหม่: `FLOWLAB_UPDATE_FIXTURES=1`) |
| `test_ingest.py` | โหลดซ้ำ/force ไม่เพิ่มแถว, Decimal ตรง, taker side, late arrival, gaps จาก meta + สแกน id, gap กลายเป็น recovered เมื่อข้อมูลมาครบ |

ตรวจว่า `openapi.json` ตรงกับโค้ด: `python3 -m app.openapi_export --check` (สร้างใหม่: ไม่ใส่ `--check`)

## 8. API โดยสรุป

ทุก endpoint ใต้ `/v1` ต้องมี `Authorization: Bearer <JWT>` · ตัวเลขทศนิยมเป็น string · เวลาเป็น ISO-8601 UTC (`Z`)
และเวลาที่ส่งเข้าต้องมี timezone · error เป็น `{"error": {"code", "message", "details"}}` · ข้อมูลของสมาชิกอื่นตอบ 404
(ไม่บอกว่ามีอยู่) · ทุก response ใต้ `/v1` มี `Cache-Control: no-store, private`

| กลุ่ม | Endpoint |
|---|---|
| บัญชี | `GET /v1/me` |
| ข้อมูลตลาด | `GET /v1/instruments`, `GET /v1/instruments/{id}`, `GET .../{id}/bars?tf=&from=&to=&source=kline\|trades`, `GET .../{id}/quality?from=&to=`, `GET .../{id}/metric-events` |
| บันทึก | `POST /v1/journal/entries` (ต้องมี `Idempotency-Key`), `GET /v1/journal/entries?from=&to=&cursor=&limit=`, `GET/PATCH/DELETE /v1/journal/entries/{id}`, `GET .../{id}/revisions`, `POST /v1/journal/snapshots`, `GET /v1/journal/snapshots/{id}` |
| เทรดส่วนตัว | `POST/GET /v1/trades`, `GET/PATCH/DELETE /v1/trades/{id}`, `POST /v1/trades/{id}/fills`, `POST /v1/trades/{id}/cashflows`, `GET /v1/trades/stats?from=&to=` |
| แจ้งเตือน | `POST/GET /v1/alerts/rules`, `GET/PATCH/DELETE /v1/alerts/rules/{id}`, `GET /v1/alerts/events`, `GET /v1/alerts/events/{id}`, `POST /v1/alerts/events/{id}/ack` |
| ส่งออก | `GET /v1/export?format=csv\|json&from=&to=&dataset=` (เฉพาะข้อมูลของตัวเอง) |

หลักสำคัญ:

* **ข้อมูลหน่วง:** สมาชิก delayed ได้เฉพาะข้อมูลที่เก่ากว่า now − 15 นาที ในทุก endpoint ตลาด (bars, quality,
  instruments, metric-events), ใน snapshot และรายการ alert event — ตัดที่ query ฝั่ง server ไม่ใช่ซ่อนบนจอ
  gap ที่ซ่อมหลัง cut-off แสดงสถานะ ณ cut-off (`open`)
* **Idempotency:** retry ด้วย key เดิม + body เดิม ได้ entry เดิม (200 + `Idempotent-Replayed: true`)
  key เดิมแต่ body ต่าง → 409; fill/cashflow ใช้ `import_key` (ไม่ซ้ำต่อสมาชิก)
* **Revision:** PATCH ต้องส่ง `expected_revision` (ไม่ตรง → 409) ทุกการแก้เพิ่มแถวใน `journal_revisions`
  พร้อมค่าก่อน/หลัง การแก้ `reason_before_entry` ต้องมี `change_reason` และติดป้าย `pre_entry_amendment`
  ส่วนบททบทวนหลังออกอยู่ที่ `post_review` แยกกัน
* **Snapshot:** server คำนวณค่าจากข้อมูลในฐาน (klines, trade tape, gaps) ตามสิทธิ์ แล้วเก็บแบบแก้ไม่ได้
  (ไม่มี route แก้/ลบ, role ของ API ไม่มีสิทธิ์ UPDATE/DELETE, trigger ปฏิเสธแม้เป็น owner)
* **ลบข้อมูล (retention):** DELETE entry/trade ลบถาวรพร้อม revision/fills และเหลือเพียง tombstone (ชนิด + id + เวลา
  ไม่มีเนื้อหา) ให้เครื่องอื่นของสมาชิกลบ cache; snapshot ลบได้เฉพาะขั้นตอนลบบัญชีที่ owner รันด้วย
  `SET LOCAL flowlab.retention_purge = 'on'` แล้ว `DELETE FROM flowlab.users WHERE id = ...`

## 9. การแยกสิทธิ์ (ownership)

* API ตั้ง `SELECT set_config('app.user_id', <uuid>, true)` ในทุก transaction (เทียบเท่า `SET LOCAL` แต่ส่งค่าเป็น
  parameter) ค่าหายเมื่อ COMMIT/ROLLBACK จึงไม่ค้างข้าม request บน connection pool
* ทุกตารางรายบุคคลเปิด RLS ด้วย policy `owner_id = flowlab.app_user_id()`; ไม่ได้ตั้ง `app.user_id` → ไม่เห็นแถวใดเลย
  และทุก query ใน API ยังมี `WHERE owner_id = ...` ซ้ำอีกชั้น foreign key แบบ `(owner_id, id)` กันการอ้างข้อมูลข้ามบัญชี
* role ของ API อ่านตารางตลาดได้อย่างเดียว; role ของ ingest เข้าตารางสมาชิกไม่ได้
* user id = UUIDv5(`iss` + "|" + `sub`) รู้ได้ก่อนแตะฐานข้อมูล สร้างแถว `users` อัตโนมัติเมื่อเรียกครั้งแรก
* production: ต้องตรวจ token ของ identity provider (เช่น Supabase Auth / OIDC ที่รวม LINE/Google) ด้วย JWKS
  (RS256/ES256, ตรวจ `iss`, `aud`, `exp`, `nbf`) แทน HS256 — ตอนนี้ `FLOWLAB_ENV=production` ถูกปฏิเสธโดยตั้งใจ

## 10. นิยามสถิติเทรด

ส่งกลับใน `GET /v1/trades/stats` ฟิลด์ `definitions` ด้วย สรุป:

* Realized PnL: average-cost บน fill ที่มีผล (fill ที่ถูก correction แทนไม่นับ) — linear `(exit−entry)×qty×multiplier`,
  inverse `qty×multiplier×(1/entry−1/exit)` (entry เป็น harmonic mean) กลับเครื่องหมายสำหรับ short
* Net PnL = gross − fees + funding + cashflow อื่น ในสกุล settlement (แปลงด้วย rate ที่บันทึกไว้; ไม่มี rate → ข้อมูลไม่ครบ)
* Net R = net PnL / initial risk ที่ล็อกเมื่อมี fill แรก; ไม่มี initial risk → `null` (N/A) ไม่เดา
* Win rate = เทรดปิดที่ net PnL > 0 / เทรดปิดที่ข้อมูลครบ; breakeven (= 0 ที่ 8 ตำแหน่ง) และข้อมูลไม่ครบแยกนับ
  (ออกที่ราคาทุนแต่เสียค่าธรรมเนียม = ขาดทุน ไม่ใช่ breakeven)
* Expectancy (ต่อสกุลและเป็น R), profit factor (ไม่มีขาดทุน → `null`), **realized-equity drawdown**
  (ไม่ใช่ mark-to-market drawdown)
* คำนวณครบทุกเทรดในช่วงวันที่ (ตาม `closed_at`) ไม่จำกัด 500 รายการ; ปัดเศษครั้งเดียวตอนท้าย (8 ตำแหน่ง, R 4 ตำแหน่ง, half-even)
* แยกตามสกุล settlement (USDT กับ BTC ไม่บวกรวมกัน); เทรดมาจากการกรอก/นำเข้าของสมาชิกเท่านั้น ไม่มีผลจำลอง

## 11. ทำแล้ว / ยังไม่ได้ทำ

**ทำแล้วและมีเทสต์:** schema + migration runner, roles + RLS + triggers กันแก้, auth HS256 (dev), `/v1/me`,
instruments/bars/quality/metric-events พร้อมการหน่วงฝั่ง server, journal (idempotency, cursor, ช่วงวันที่,
revision, ลบ + tombstone), snapshot แบบแช่แข็ง, เทรดส่วนตัว (fills, correction, import_key, cashflow/funding,
ล็อก initial risk, สถิติ), alert rules CRUD + event idempotent + backfill/cooldown/quality pause + ack,
export CSV/JSON, ingest แบบ idempotent + data_quality, openapi.json sync

**ยังไม่ได้ทำ / ยังเป็นข้อจำกัด:**

* ไม่มี identity provider จริง (ใช้ HS256 dev secret), ไม่มีการตรวจ JWKS, ไม่มีระบบสมัคร/ลบบัญชีผ่าน API
* ไม่มีระบบชำระเงิน — สิทธิ์ realtime ต้องใส่ใน `entitlements` เอง
* ไม่มี Telegram/LINE หรือ push: ช่องทางที่สร้างได้มีเพียง `in_app` (`telegram` ตอบ 422); ยังไม่มี worker ที่ประเมิน
  กฎจากข้อมูลจริงและส่ง delivery (`alert_deliveries` ค้างสถานะ `pending`) — มีเพียงฟังก์ชัน `record_alert_event`
* ไม่มี object storage: snapshot เก็บเพียง `image_object_key` + `image_sha256` ยังอัปโหลด/ดาวน์โหลดภาพไม่ได้
* ไม่มี WebSocket gateway (เมื่อทำ ต้องส่ง token ทางข้อความแรกหรือ header ไม่ใช่ URL)
* ยังไม่ได้คำนวณ metric/events จาก engine (`metric_events` มีตารางและ endpoint แต่ไม่มีตัวเติมข้อมูล)
* ingest โหลด trades/kline/book/meta (+ mark/oi/liq เป็น raw_events); ยังไม่มีตาราง normalized ของ mark/funding/OI/liquidation
  และ collector ยังไม่เก็บ raw depth diff จึง replay order book ละเอียดกว่า bucket 1 วินาทีไม่ได้
* ยังไม่มีงานลบ tombstone/idempotency record เก่า, งานลบ snapshot ที่ไม่มี entry อ้างถึง, backup/restore ที่ทดสอบจริง
* TimescaleDB และ docker compose ยังไม่ได้ทดสอบ; ไม่มี rate limiting; ไม่มี research workspace (แยกจากสถิติสมาชิกโดยตั้งใจ)
* bars แบ่งช่วงตาม UTC (1d เริ่ม 00:00 UTC) ยังไม่รองรับ session ตาม IANA timezone
* TP1 hit rate ไม่คำนวณ (personal trade ไม่มีฟิลด์ TP1 และไม่ใช่ win rate)
* ยังไม่ได้ตรวจกฎหมาย/สิทธิ์ข้อมูล (ROADMAP ข้อ 4) — ห้ามเปิดให้คนนอกใช้ก่อนได้คำตอบ

## 12. เรื่องที่เจ้าของควรตัดสินใจ/ตรวจ

1. **trades ไม่เป็น hypertable:** business key (venue, market, symbol, agg_trade_id) ไม่มีเวลา แต่ hypertable ต้องมีคอลัมน์เวลาในทุก unique key
   ทางเลือก: ใส่ `event_ts` ใน key (aggTrade ของ Binance มีเวลาคงที่) หรือย้าย trades ไป ClickHouse ภายหลัง
2. **สมาชิกไม่มีแถว entitlement = delayed** (`FLOWLAB_DEFAULT_ENTITLEMENT`) ตามแพ็กเกจฟรีใน ROADMAP — ถ้าต้องการให้
   ต้องสมัครก่อนจึงเห็นข้อมูล ให้ตั้ง `none`
3. **user id = UUIDv5(iss|sub):** ถ้าเปลี่ยน identity provider id จะเปลี่ยน ต้องมีขั้นตอน migrate บัญชี
4. **DELETE เป็นการลบถาวร** (ไม่ใช่ soft delete) ตามหลัก PDPA; ลบ alert rule จะลบประวัติ event ของกฎนั้นด้วย
5. **Alert ของสมาชิก delayed** ถูกบันทึกแต่ไม่ส่งแบบสด (`suppressed_entitlement`) และ observation ที่เก่ากว่า 120 วินาที
   ถือเป็น backfill อัตโนมัติ
6. **Breakeven = net PnL เป็นศูนย์พอดีที่ 8 ตำแหน่ง** ไม่มีช่วงยอมรับ; average-cost (ไม่ใช่ FIFO) สำหรับ realized PnL;
   spot ที่จ่าย fee เป็นเหรียญฐานยังไม่ลดจำนวนเหรียญในสถานะ
7. **instrument ที่ ingest สร้าง** ตั้ง multiplier = 1 (qty เป็นเหรียญฐาน) และ `unverified` — ต้องยืนยันกับ contract spec จริง
8. ข้อสังเกตต่อ collector: รอบแก้ REVIEW แล้ว ช่วง id ที่ backfill ไม่สำเร็จจะค้างใน ledger และลองใหม่พร้อม `backfill_retry`
   (ไม่ถูกทิ้ง) ส่วนช่วงที่เกิน `BACKFILL_MAX` เขียนเป็น `trade_gap` พร้อมเหตุผล — ingest ยังสแกน id ที่ขาดซ้ำอีกชั้น
   ที่ยังเหลือ: collector ไม่ backfill kline/book หลังหลุด จึงเป็น `unrecoverable`; การหยุด process ไม่มี `disconnected`
   ingest จึงอนุมาน downtime จาก `connected` ครั้งถัดไป
