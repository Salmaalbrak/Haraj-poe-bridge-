# -*- coding: utf-8 -*-
import os, asyncio, re
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

# ===== إعدادات من البيئة =====
HARAJ_GRAPHQL_URL = os.getenv("HARAJ_GRAPHQL_URL", "https://graphql.haraj.com.sa")
HARAJ_USER_AGENT  = os.getenv("HARAJ_USER_AGENT", "HarajServerBot/1.0")
HARAJ_ACCESS_TOKEN = os.getenv("HARAJ_ACCESS_TOKEN", "")  # اختياري

# الهيدرز (User-Agent مطلوب، التوكن اختياري)
DEFAULT_HEADERS: Dict[str, str] = {
    "Content-Type": "application/json",
    "trackId": "",
    "User-Agent": HARAJ_USER_AGENT
}
if HARAJ_ACCESS_TOKEN:
    DEFAULT_HEADERS["Authorization"] = f"Bearer {HARAJ_ACCESS_TOKEN}"

app = FastAPI(title="Haraj↔Poe Bridge", version="2.0")

# ===== نماذج =====
class Preferences(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    city: Optional[str] = None
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    gear: Optional[str] = None      # "اوتوماتيك" / "عادي"
    fuel: Optional[str] = None      # "بنزين" "ديزل" "هايبرد" "كهرب"

class PoeMessage(BaseModel):
    user_id: str
    conversation_id: str
    text: str

SESSIONS: Dict[str, Preferences] = {}

# ===== تحليل نص المستخدم → تفضيلات =====
def ar_normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def extract_prefs_from_text(text: str) -> Preferences:
    t = ar_normalize(text)
    p = Preferences()

    makes = [
        "تويوتا","نيسان","هوندا","هيونداي","كيا","لكزس","مرسيدس",
        "بي ام","bmw","شفر","شيفروليه","فورد","مازدا","جيب","دودج","شيري","جيلي","mg"
    ]
    for mk in makes:
        if mk in t:
            p.make = "بي ام" if mk == "bmw" else ("شيفروليه" if mk=="شفر" else mk)
            break

    # موديل بسيط (كلمة بعد الماركة)
    if p.make:
        m = re.search(p.make + r"\s+([a-zA-Z\u0600-\u06FF0-9]+)", t)
        if m:
            cand = m.group(1)
            if cand not in ["تحت","من","الى","إلى"]:
                p.model = cand

    # القير
    if any(k in t for k in ["اوتومات","أوتومات","اوتو","اتوماتيك","اوتوماتيك"]):
        p.gear = "اوتوماتيك"
    elif any(k in t for k in ["عادي","مانيوال","جير عادي"]):
        p.gear = "عادي"

    # الوقود
    if "هايبرد" in t: p.fuel = "هايبرد"
    elif "كهرب" in t or "كهرباء" in t: p.fuel = "كهرب"
    elif "ديزل" in t: p.fuel = "ديزل"
    elif "بنزين" in t: p.fuel = "بنزين"

    # السنوات
    years = re.findall(r"(20\d{2})", t)
    if len(years) >= 2:
        p.year_min, p.year_max = sorted(map(int, years[:2]))
    elif len(years) == 1:
        p.year_min = int(years[0])

    # السعر تحت/من-إلى
    m_under = re.search(r"تحت\s*(\d+)", t)
    if m_under:
        val = int(m_under.group(1))
        p.price_max = val * (1000 if val < 1000 else 1)
    m_range = re.search(r"من\s*(\d+)\s*(?:الف|ألف)?\s*(?:الى|إلى|-)\s*(\d+)", t)
    if m_range:
        a, b = int(m_range.group(1)), int(m_range.group(2))
        if a < 1000: a *= 1000
        if b < 1000: b *= 1000
        p.price_min, p.price_max = a, b

    # المدن
    cities = ["الرياض","جدة","مكة","المدينة","الدمام","الخبر","بريدة","الطائف","أبها","حائل","ينبع","جازان","تبوك","عسير","نجران","القصيم"]
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

# ===== تحويل التفضيلات → فلاتر GraphQL =====
def prefs_to_filters(p: Preferences) -> Dict[str, Any]:
    f: Dict[str, Any] = {}
    if p.make:        f["make"] = p.make
    if p.model:       f["model"] = p.model
    if p.city:        f["cityName"] = p.city
    if p.year_min:    f["yearMin"] = p.year_min
    if p.year_max:    f["yearMax"] = p.year_max
    if p.price_min:   f["priceMin"] = p.price_min
    if p.price_max:   f["priceMax"] = p.price_max
    if p.gear:
        f["gear"] = "automatic" if "اوت" in p.gear else "manual"
    if p.fuel:
        mapping = {"بنزين":"gasoline","ديزل":"diesel","هايبرد":"hybrid","كهرب":"electric","كهرباء":"electric"}
        f["fuel"] = mapping.get(p.fuel, p.fuel)
    return f

# ===== استعلام GraphQL =====
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
            # حد الطلبات (rate limit) أو كود خاص
            if r.status_code in (388, 429):
                await asyncio.sleep(min(2 ** tries, 10))
                tries += 1
                if tries > 4:
                    raise HTTPException(429, "Rate limited by Haraj")
                continue
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise HTTPException(400, f"GraphQL errors: {data['errors']}")
            return data["data"]["Search"]

def format_results_ar(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "ما لقيت نتائج على المواصفات الحالية. تبين أعدّل الشروط؟"
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
async def health():
    return {"status": "ok", "service": "Haraj↔Poe Bridge"}

@app.post("/poe")
async def poe_bridge(req: Request):
    # دعم فحص Poe حتى لو بدون body
    try:
        data = await req.json()
    except Exception:
        return {"ok": True, "service": "Haraj↔Poe Bridge"}

    # Poe يرسل user_id / conversation_id / text
    # لو مو موجودين نرجّع ok عشان الفحص
    if not isinstance(data, dict) or not all(k in data for k in ("user_id","conversation_id","text")):
        return {"ok": True, "service": "Haraj↔Poe Bridge"}

    msg = PoeMessage(**data)

    prefs = SESSIONS.get(msg.conversation_id, Preferences())
    extracted = extract_prefs_from_text(msg.text)
    prefs = merge_prefs(prefs, extracted)
    SESSIONS[msg.conversation_id] = prefs

    # أوامر مسح
    if any(k in msg.text for k in ["امسح","امسح الشروط","reset","ابدأ من جديد","ابدا من جديد"]):
        SESSIONS[msg.conversation_id] = Preferences()
        return {"text": "تم مسح الشروط. عطيني مواصفاتك من جديد (ماركة/موديل/سعر/سنة/مدينة…)."}

    filters = prefs_to_filters(prefs)
    try:
        res = await gql_search(filters, page=1, limit=10)
        reply = format_results_ar(res.get("items", []))
    except HTTPException as e:
        reply = "فيه ضغط على خدمة حراج الآن (Rate limit). جرّبي بعد لحظات." if e.status_code == 429 else f"صار خطأ أثناء البحث: {e.detail}"

    summary = " | ".join([f"{k}:{v}" for k, v in prefs.dict().items() if v])
    if summary:
        reply += "\n\n— الشروط الحالية: " + summary

    return {"text": reply}
