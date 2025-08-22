# -*- coding: utf-8 -*-
import os, asyncio, re
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx

# ===== الإعدادات من البيئة =====
HARAJ_GRAPHQL_URL = os.getenv("HARAJ_GRAPHQL_URL", "https://graphql.haraj.com.sa")
HARAJ_USER_AGENT  = os.getenv("HARAJ_USER_AGENT", "PoeHarajBot/1.0")  # مهم: الهيدر المطلوب
HARAJ_ACCESS_TOKEN = os.getenv("HARAJ_ACCESS_TOKEN", "")              # اختياري

# الهيدرز الافتراضية: نضيف التوكن فقط إن وجد
DEFAULT_HEADERS: Dict[str, str] = {
    "Content-Type": "application/json",
    "trackId": "",
    "User-Agent": HARAJ_USER_AGENT
}
if HARAJ_ACCESS_TOKEN:
    DEFAULT_HEADERS["Authorization"] = f"Bearer {HARAJ_ACCESS_TOKEN}"

app = FastAPI(title="Haraj↔Poe Bridge", version="1.0.1")

# ===== الموديلات =====
class Preferences(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    city: Optional[str] = None
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    gear: Optional[str] = None
    fuel: Optional[str] = None

class PoeMessage(BaseModel):
    user_id: str
    conversation_id: str
    text: str

# جلسات بسيطة بالذاكرة
SESSIONS: Dict[str, Preferences] = {}

# ===== مساعدات استخراج المواصفات من نص المستخدم =====
def ar_normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def extract_prefs_from_text(text: str) -> Preferences:
    t = ar_normalize(text)
    p = Preferences()

    makes = ["تويوتا","نيسان","هوندا","هيونداي","كيا","لكزس","مرسيدس",
             "بي ام","bmw","شفر","فورد","مازدا","جيب","دودج","شيري","جيلي"]
    for mk in makes:
        if mk in t:
            p.make = "بي ام" if mk == "bmw" else mk
            break

    if "اوتومات" in t or "أوتومات" in t or "اوتو" in t: p.gear = "اوتوماتيك"
    elif "عادي" in t or "مانيوال" in t: p.gear = "عادي"

    if "هايبرد" in t: p.fuel = "هايبرد"
    elif "كهرب" in t or "كهرباء" in t: p.fuel = "كهرب"
    elif "ديزل" in t: p.fuel = "ديزل"
    elif "بنزين" in t: p.fuel = "بنزين"

    ys = re.findall(r"(20\d{2})", t)
    if len(ys) >= 2:
        p.year_min, p.year_max = sorted(map(int, ys[:2]))
    elif len(ys) == 1:
        p.year_min = int(ys[0])

    if "تحت" in t:
        m = re.search(r"تحت\s*(\d+)", t)
        if m:
            val = int(m.group(1))
            p.price_max = val * (1000 if val < 1000 else 1)

    rng = re.search(r"من\s*(\d+)\s*(?:الف|ألف)?\s*(?:الى|إلى|-)\s*(\d+)", t)
    if rng:
        a, b = int(rng.group(1)), int(rng.group(2))
        if a < 1000: a *= 1000
        if b < 1000: b *= 1000
        p.price_min, p.price_max = a, b

    cities = ["الرياض","جدة","الدمام","الخبر","مكة","المدينة",
              "بريدة","الطائف","أبها","حائل","ينبع","جازان"]
    for c in cities:
        if c in t:
            p.city = c
            break

    return p

def merge_prefs(a: Preferences, b: Preferences) -> Preferences:
    return Preferences(
        make=b.make or a.make,
        model=b.model or a.model,
        city=b.city or a.city,
        year_min=b.year_min or a.year_min,
        year_max=b.year_max or a.year_max,
        price_min=b.price_min or a.price_min,
        price_max=b.price_max or a.price_max,
        gear=b.gear or a.gear,
        fuel=b.fuel or a.fuel
    )

# ===== استعلام GraphQL (نموذجي) =====
SEARCH_QUERY = """
query Search($filters: SearchFilters!, $page: Int, $limit: Int) {
  Search(filters: $filters, page: $page, limit: $limit) {
    total
    items {
      id
      title
      price
      city { id name enName }
      car { make model year mileage fuel gear }
      url
      images { url }
    }
  }
}
"""

async def gql_search(filters: Dict[str, Any], page=1, limit=10) -> Dict[str, Any]:
    payload = {"query": SEARCH_QUERY, "variables": {"filters": filters, "page": page, "limit": limit}}
    async with httpx.AsyncClient(timeout=25) as client:
        tries = 0
        while True:
            r = await client.post(HARAJ_GRAPHQL_URL, headers=DEFAULT_HEADERS, json=payload)
            if r.status_code in (388, 429):
                wait = min(2 ** tries, 10)
                await asyncio.sleep(wait)
                tries += 1
                if tries > 4:
                    raise HTTPException(429, "Rate limited by Haraj")
                continue
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise HTTPException(400, f"GraphQL errors: {data['errors']}")
            return data["data"]["Search"]

def prefs_to_filters(p: Preferences) -> Dict[str, Any]:
    f: Dict[str, Any] = {}
    if p.make:      f["make"] = p.make
    if p.model:     f["model"] = p.model
    if p.city:      f["cityName"] = p.city
    if p.year_min:  f["yearMin"] = p.year_min
    if p.year_max:  f["yearMax"] = p.year_max
    if p.price_min: f["priceMin"] = p.price_min
    if p.price_max: f["priceMax"] = p.price_max
    if p.gear:      f["gear"] = "automatic" if "اوت" in p.gear else "manual"
    if p.fuel:
        mapping = {"بنزين":"gasoline","ديزل":"diesel","هايبرد":"hybrid","كهرب":"electric","كهرباء":"electric"}
        f["fuel"] = mapping.get(p.fuel, p.fuel)
    return f

def format_results_ar(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "ما لقيت نتائج على المواصفات الحالية. تبين أوسع البحث أو أعدّل الشروط؟"
    lines = []
    for it in items[:5]:
        car = it.get("car") or {}
        title = it.get("title") or f"{car.get('make','')} {car.get('model','')}".strip()
        price = it.get("price", "—")
        city  = (it.get("city") or {}).get("name", "")
        year  = car.get("year", "")
        gear  = car.get("gear", "")
        fuel  = car.get("fuel", "")
        url   = it.get("url", "")
        lines.append(f"• {title} — سنة {year} — {price} ريال — {city} — {fuel}/{gear}\n{url}")
    return "\n\n".join(lines)

# ===== المسارات =====
@app.get("/")
async def root():
    return {"ok": True, "service": "Haraj↔Poe Bridge"}

@app.post("/poe")
async def poe_bridge(msg: PoeMessage, request: Request):
    # تحقق من المفتاح
    access_key = request.headers.get("X-Access-Key")
    if access_key != "2AwRr0kpXdhU5AEbge8fP5yalCSsLDs":
        raise HTTPException(status_code=401, detail="Invalid access key")

    prefs = SESSIONS.get(msg.conversation_id, Preferences())
    extracted = extract_prefs_from_text(msg.text)
    prefs = merge_prefs(prefs, extracted)
    SESSIONS[msg.conversation_id] = prefs

    if any(k in msg.text for k in ["امسح", "reset", "ابدا من جديد"]):
        SESSIONS[msg.conversation_id] = Preferences()
        return {"text": "تم مسح التفضيلات، قولي مواصفاتك من جديد (ماركة/موديل/سعر/سنة...إلخ)."}

    filters = prefs_to_filters(prefs)
    try:
        res = await gql_search(filters, page=1, limit=10)
        reply = format_results_ar(res.get("items", []))
    except HTTPException as e:
        if e.status_code == 429:
            reply = "جرب بعد لحظات، فيه حد للطلبات (Rate limit)."
        else:
            reply = f"صار خطأ: {e.detail}"

    summary = " | ".join([f"{k}:{v}" for k, v in prefs.dict().items() if v])
    if summary:
        reply += "\n\n🔎 التفضيلات الحالية: " + summary

    return {"text": reply}

    
