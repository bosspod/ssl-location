# SSL Location Resolution POC

ระบบช่วยค้นหาและยืนยันพิกัดสถานที่สำหรับงานขนส่งในประเทศไทย โดยรับข้อมูลดิบ เช่น
ชื่อบริษัท ชื่อสาขา ที่อยู่ เบอร์โทรศัพท์ หรือข้อความที่มีข้อมูลหลายอย่างปะปนกัน แล้วค้นหา
ตำแหน่งที่มีความเป็นไปได้จากผู้ให้บริการแผนที่หลายราย ก่อนจัดอันดับและส่งผลลัพธ์ที่ดีที่สุด
กลับมาให้ผู้ใช้ตรวจสอบและยืนยัน

โปรเจกต์นี้พัฒนาด้วย FastAPI, Jinja2, HTMX, MongoDB, PyMongo Async, Pydantic,
httpx และ RapidFuzz พร้อมหน้าเว็บสำหรับทดลองใช้งานและ REST API สำหรับเชื่อมต่อกับระบบอื่น

## ความสามารถหลัก

- แยกชื่อสถานที่ บริษัท สาขา ที่อยู่ เบอร์โทรศัพท์ และองค์ประกอบของที่อยู่จากข้อความดิบ
- สร้างคำค้นทั้งภาษาไทยและภาษาอังกฤษ แล้วค้นหาหลายคำค้นพร้อมกัน
- รองรับ Google Maps, HERE, Longdo และ OpenStreetMap/Nominatim
- รวมผลลัพธ์ซ้ำ กรองสถานที่ผิดพื้นที่ และจัดอันดับจากหลักฐานหลายแหล่ง
- ใช้ OpenAI ช่วยค้นคว้าและเปรียบเทียบผู้สมัครเมื่อกำหนด `OPENAI_API_KEY`
- ตรวจสอบข้อมูลนิติบุคคลกับ DBD จากเลขทะเบียนหรือชื่อบริษัท
- ให้ผู้ใช้ยืนยันผลลัพธ์เพื่อบันทึกเป็น Location Master alias ใน MongoDB
- มี cache, rate limit, provider circuit breaker และ health check สำหรับการให้บริการ

AI ใช้สำหรับวิเคราะห์ข้อความ วางแผนคำค้น และประเมินหลักฐานเท่านั้น พิกัดที่ระบบส่งกลับต้องมา
จากผู้ให้บริการแผนที่หรือ Location Master ที่ผู้ใช้เคยยืนยันแล้ว ระบบไม่ให้ AI สร้างพิกัดขึ้นเอง

## ภาพรวมการทำงาน

```text
ข้อความดิบ
   │
   ▼
แยกและปรับรูปแบบข้อมูล ──► ค้นคว้าข้อมูลสาธารณะ/DBD (ถ้ากำหนดค่าไว้)
   │
   ▼
สร้างคำค้นหลายรูปแบบ
   │
   ▼
ค้นหาจาก Map Providers แบบขนาน
   │
   ▼
รวมผลซ้ำ → กรอง → จัดอันดับ → ประเมินหลักฐาน
   │
   ▼
bestMatch + alternatives ──► ผู้ใช้ยืนยัน ──► Location Master
```

ระบบจะค้น Location Master ก่อน หากพบ alias ที่เคยยืนยันตรงกัน จะคืนผลลัพธ์นั้นโดยไม่เรียก
ผู้ให้บริการภายนอก หากยังไม่พบจึงเริ่มค้นหาใหม่ ผลตอบกลับจะมีสถานะ `EXACT`, `HIGH`,
`MEDIUM`, `LOW`, `CONFLICT` หรือ `NO_RESULT` โดยเมื่อมีผู้สมัครที่ผ่านการกรองอย่างน้อยหนึ่งรายการ
ระบบจะส่งรายการอันดับสูงสุดใน `bestMatch` เสมอ

## สิ่งที่ต้องติดตั้ง

วิธีที่แนะนำต้องมี:

- Docker Desktop หรือ Docker Engine ที่รองรับ Docker Compose
- API key ของผู้ให้บริการแผนที่อย่างน้อยหนึ่งราย หากต้องการให้ระบบค้นหาสถานที่จริง

หากต้องการรันโดยไม่ใช้ Docker ต้องมี Python 3.12 ขึ้นไปและ MongoDB ด้วย

## ติดตั้งด้วย Docker Compose (แนะนำ)

1. สร้างไฟล์ environment จากตัวอย่าง

   ```bash
   cp .env.example .env
   ```

2. เปิด `.env` แล้วกำหนดค่าอย่างน้อยหนึ่ง Map Provider ตัวอย่างเช่น Google Maps

   ```dotenv
   APP_ENV=development
   APP_PORT=8010
   GOOGLE_MAPS_API_KEY=your-google-maps-api-key
   SIGNING_SECRET=replace-with-a-long-random-secret
   ```

   สำหรับ Google Cloud ให้เปิดใช้งาน **Places API (New)** และ **Geocoding API** หากต้องการ
   แสดง Street View บนหน้าเว็บ ให้เปิด **Maps Embed API** และกำหนด
   `GOOGLE_MAPS_EMBED_API_KEY` เป็น browser key แยกต่างหากที่จำกัด HTTP referrer ในโหมด
   development ระบบจะใช้ `GOOGLE_MAPS_API_KEY` แทนเมื่อไม่ได้กำหนด embed key

3. สร้าง image และเริ่มระบบ

   ```bash
   docker compose up --build
   ```

4. เปิดหน้าเว็บที่ <http://localhost:8010>

   ค่า port มาจาก `APP_PORT` ใน `.env` หากไม่กำหนดจะใช้ port `8000`

5. หยุดระบบด้วย `Ctrl+C` หรือใช้คำสั่ง

   ```bash
   docker compose down
   ```

   ข้อมูล MongoDB จะยังอยู่ใน Docker volume ชื่อ `mongo_data` หลังหยุด container

ตรวจสอบสถานะระบบได้ที่:

- Liveness: <http://localhost:8010/health/live>
- Readiness และรายชื่อ provider ที่พร้อมใช้: <http://localhost:8010/health/ready>
- Swagger UI: <http://localhost:8010/docs>

## ติดตั้งและรันแบบ Local

1. สร้าง virtual environment และติดตั้งแพ็กเกจ

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   python -m pip install -e '.[dev]'
   ```

2. คัดลอกไฟล์ environment

   ```bash
   cp .env.example .env
   ```

3. เริ่ม MongoDB และสร้างผู้ใช้ให้ตรงกับ `DATABASE_URL` หรือแก้ URL ใน `.env` ให้ตรงกับ
   MongoDB ที่มีอยู่ ตัวอย่างสำหรับ MongoDB บนเครื่อง:

   ```dotenv
   DATABASE_URL=mongodb://location:location@localhost:27017/?authSource=admin
   MONGODB_DATABASE=ssl_location_optimization
   ```

   เมื่อรันแบบ local ต้องเปลี่ยน hostname ใน `DATABASE_URL` จาก `mongo` เป็น `localhost`
   ส่วนบริการ `web` ใน Docker Compose จะกำหนด URL ที่ใช้ hostname `mongo` ให้อัตโนมัติ

4. เริ่ม FastAPI development server

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
   ```

ตอนเริ่มระบบ แอปจะตรวจสอบการเชื่อมต่อ MongoDB และสร้าง indexes ที่จำเป็นแบบ idempotent
หากเชื่อมต่อฐานข้อมูลไม่ได้ แอปจะเริ่มทำงานไม่สำเร็จ

## การตั้งค่า `.env`

ไฟล์ `.env.example` มีค่าที่รองรับทั้งหมด ตารางต่อไปนี้คือค่าหลักที่มักต้องแก้ไข:

| ตัวแปร | คำอธิบาย | จำเป็น |
|---|---|---|
| `APP_ENV` | สภาพแวดล้อม `development` หรือ `production` | มีค่าเริ่มต้น |
| `APP_PORT` | port ที่ Docker เปิดให้เข้าจากเครื่อง host | มีค่าเริ่มต้น `8010` ในไฟล์ตัวอย่าง |
| `DATABASE_URL` | MongoDB connection string | จำเป็น |
| `MONGODB_DATABASE` | ชื่อฐานข้อมูล | มีค่าเริ่มต้น |
| `SIGNING_SECRET` | ใช้ลงลายเซ็น confirmation token | ต้องเปลี่ยนใน production |
| `GOOGLE_MAPS_API_KEY` | Google Places และ Geocoding | เลือกอย่างน้อยหนึ่ง Map Provider |
| `GOOGLE_MAPS_EMBED_API_KEY` | key สำหรับ Street View iframe บนหน้าเว็บ | ไม่จำเป็น |
| `HERE_API_KEY` | HERE Geocoding and Search | ไม่จำเป็นใน development |
| `LONGDO_API_KEY` | Longdo Map Search | ไม่จำเป็นใน development |
| `OSM_NOMINATIM_BASE_URL` | URL ของ Nominatim | ต้องกำหนดคู่กับ `OSM_USER_AGENT` |
| `OSM_USER_AGENT` | User-Agent ที่ระบุแอปและผู้ดูแลจริง | ต้องกำหนดคู่กับ URL |
| `OPENAI_API_KEY` | เปิด AI research และ AI-assisted resolution | ไม่จำเป็น |
| `AI_RESEARCH_MODEL` | โมเดลที่ใช้วิเคราะห์และค้นคว้า | มีค่าเริ่มต้น |
| `DBD_API_ENABLED` | เปิด/ปิดการตรวจข้อมูลนิติบุคคล | มีค่าเริ่มต้น `true` |
| `DBD_CONSUMER_KEY` | credential สำหรับค้นชื่อบริษัทผ่าน DGA/GDX | จำเป็นเฉพาะการค้นด้วยชื่อ |
| `DBD_CONSUMER_SECRET` | credential สำหรับค้นชื่อบริษัทผ่าน DGA/GDX | จำเป็นเฉพาะการค้นด้วยชื่อ |
| `DBD_AGENT_ID` | agent ID สำหรับ DGA/GDX | จำเป็นเฉพาะการค้นด้วยชื่อ |
| `LOG_LEVEL` | ระดับ log เช่น `INFO` หรือ `DEBUG` | มีค่าเริ่มต้น |

ระบบตรวจเลขทะเบียนนิติบุคคล 13 หลักผ่าน DBD Open API ได้ ส่วนการค้นด้วยชื่อบริษัทใช้ endpoint
ของ DGA/GDX และต้องกำหนด `DBD_CONSUMER_KEY`, `DBD_CONSUMER_SECRET` และ
`DBD_AGENT_ID`

OpenStreetMap/Nominatim จะไม่เปิดใช้งานจนกว่าจะกำหนดทั้ง `OSM_NOMINATIM_BASE_URL` และ
`OSM_USER_AGENT` สำหรับ production ควรใช้ Nominatim ที่ดูแลเองหรือบริการที่มีสัญญารองรับ
ไม่ควรใช้ public endpoint โดยตรง

### ข้อกำหนดสำหรับ Production

เมื่อกำหนด `APP_ENV=production` ระบบจะไม่ยอมเริ่มทำงานหาก:

- `SIGNING_SECRET` ยังเป็นค่าเริ่มต้น
- กำหนดค่า OSM URL หรือ User-Agent มาเพียงค่าเดียว
- ใช้ public `nominatim.openstreetmap.org`
- Map Provider ทั้ง Google, HERE, Longdo และ OSM ตั้งค่าไม่ครบ

หากต้องการ deploy แบบลดความสามารถชั่วคราว สามารถกำหนด
`REQUIRE_ALL_PROVIDERS_IN_PRODUCTION=false` เพื่อไม่บังคับ provider ให้ครบทุกตัวได้

ข้อความที่ส่งให้ AI อาจมีชื่อ ที่อยู่ และเบอร์โทรศัพท์จาก input ก่อนใช้งานจริงควรจัดทำนโยบาย
ความเป็นส่วนตัว ระยะเวลาเก็บข้อมูล และฐานกฎหมายให้เหมาะสม รวมถึงตรวจสอบ license และเงื่อนไข
การเก็บข้อมูลของ Map Provider แต่ละราย

## การใช้งาน API

### ค้นหาสถานที่

```bash
curl -X POST http://localhost:8010/api/location/resolve \
  -H 'Content-Type: application/json' \
  -d '{"input":"บริษัท ตัวอย่าง จำกัด สาขาบางนา 123 ถนนสุขุมวิท กรุงเทพฯ 10260"}'
```

สามารถส่งบริบททางภูมิศาสตร์เพิ่มเติมได้:

```json
{
  "input": "คลังสินค้าบางนา บริษัท ตัวอย่าง จำกัด",
  "context": {
    "province": "สมุทรปราการ",
    "deliveryZone": "BKK-EAST",
    "depotLocation": [13.668, 100.635]
  }
}
```

ผลตอบกลับประกอบด้วยข้อมูลที่ parse แล้ว สถานะ `bestMatch`, `alternatives`, สถานะของแต่ละ
provider หลักฐานจาก research และเวลาประมวลผล ตัวอย่างโครงสร้างแบบย่อ:

```json
{
  "requestId": "...",
  "query": "...",
  "parsed": {},
  "status": "HIGH",
  "bestMatch": {
    "candidateId": "...",
    "name": "บริษัท ตัวอย่าง จำกัด",
    "address": "...",
    "latitude": 13.668,
    "longitude": 100.635,
    "confidenceScore": 0.87,
    "confirmationToken": "..."
  },
  "alternatives": [],
  "providers": [],
  "research": {},
  "processingTimeMs": 1234
}
```

### ยืนยันสถานที่

ส่ง `rawInput` เดิมและ object `bestMatch` หรือรายการจาก `alternatives` กลับมาโดยไม่แก้ไข
โดยเฉพาะ `confirmationToken` ตัวอย่างต่อไปนี้ใช้ `jq` เก็บ candidate ทั้งก้อนจากผลค้นหา:

```bash
RAW_INPUT='บริษัท ตัวอย่าง จำกัด สาขาบางนา 123 ถนนสุขุมวิท กรุงเทพฯ 10260'

curl -sS -X POST http://localhost:8010/api/location/resolve \
  -H 'Content-Type: application/json' \
  --data "$(jq -n --arg input "$RAW_INPUT" '{input: $input}')" \
  > resolve-response.json

jq -n \
  --arg rawInput "$RAW_INPUT" \
  --slurpfile result resolve-response.json \
  '{rawInput: $rawInput, candidate: $result[0].bestMatch}' \
  | curl -sS -X POST http://localhost:8010/api/location/confirm \
      -H 'Content-Type: application/json' \
      --data-binary @-
```

ในงานจริงควรส่ง candidate object ทั้งก้อนจากผล `/resolve` เนื่องจาก token ผูกกับข้อมูลสำคัญ
ของ candidate และระบบจะปฏิเสธข้อมูลที่ถูกเปลี่ยนแปลงหรือ token ที่ไม่ถูกต้อง

## การทดสอบและตรวจคุณภาพโค้ด

รัน test suite, Ruff, mypy และ coverage ผ่าน Docker:

```bash
docker compose run --rm test
```

หรือรันใน virtual environment:

```bash
ruff check .
mypy app
pytest --cov=app --cov-report=term-missing
```

การทดสอบไม่ต้องใช้ credential ของ Google หรือ provider ภายนอก เพราะใช้ adapter ที่ควบคุม
ผลลัพธ์ไว้แล้ว โปรเจกต์กำหนด statement coverage ขั้นต่ำไว้ที่ 99.99%

## โครงสร้างโปรเจกต์

```text
app/
├── main.py              # สร้าง FastAPI app, lifespan และ health checks
├── config.py            # อ่านและตรวจสอบ environment settings
├── database.py          # เชื่อมต่อ MongoDB และสร้าง indexes
├── routes/              # Web UI และ REST API routes
├── providers/           # Google, HERE, Longdo และ OSM adapters
├── services/            # parsing, search, research, scoring และ resolution flow
├── templates/           # Jinja2/HTMX templates
└── static/              # CSS และ JavaScript ของหน้าเว็บ
tests/                    # unit และ integration tests
compose.yaml              # MongoDB, web และ test services
Dockerfile                # runtime และ test images
pyproject.toml            # dependencies และการตั้งค่าเครื่องมือพัฒนา
```

## การแก้ปัญหาเบื้องต้น

**เปิดหน้าเว็บไม่ได้** — ตรวจว่าใช้ port ตรงกับ `APP_PORT` และดู log ด้วย
`docker compose logs -f web`

**Readiness ตอบ `503`** — มักเกิดจากแอปเชื่อมต่อ MongoDB ไม่ได้ ตรวจ `DATABASE_URL`, สถานะ
container ด้วย `docker compose ps` และ log ของ `mongo`

**ค้นหาแล้วได้ `NO_RESULT`** — ตรวจ `/health/ready` ว่ามี provider อยู่ในรายการ และตรวจว่า API
key เปิด API ที่จำเป็น มีสิทธิ์ใช้งาน และไม่มี restriction ที่บล็อก server request

**Street View ไม่แสดง** — เปิด Maps Embed API และตรวจ restriction ของ
`GOOGLE_MAPS_EMBED_API_KEY` ให้ยอมรับ origin ที่ใช้เปิดหน้าเว็บ

**ระบบไม่เริ่มใน production** — ตรวจ `SIGNING_SECRET`, provider credentials และข้อกำหนด OSM
ในหัวข้อ Production ด้านบน
