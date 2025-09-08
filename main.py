# -*- coding: utf-8 -*-
import os, re, asyncio
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

# ===================== الإعدادات =====================
HARAJ_GRAPHQL_URL = os.getenv("HARAJ_GRAPHQL_URL", "https://graphql.haraj.com.sa")
HARAJ_USER_AGENT  = os.getenv("HARAJ_USER_AGENT", "haraj/5.6.11 (iPhone; iOS 18.1; Scale/3.00)")  # << حطي اللي عطوك إذا تبين
# ما نحتاج توكن، لكن نخليه اختياري لو تغير شيء مستقبلًا:
HARAJ_ACCESS_TOKEN = os.getenv("HARAJ_ACCESS_TOKEN", "")

DEFAULT_HEADERS: Dict[str, str] = {
    "Content-Type": "application/json",
    "User-Agent": HARAJ_USER_AGENT,
    "trackId": ""
}
if HARAJ_ACCESS_TOKEN:
    DEFAULT_HEADERS["Authorization"] = f"Bearer {HARAJ_ACCESS_TOKEN}"

app = FastAPI(title="Haraj↔Poe Bridge", version="3.0")

# ===================== نماذج =====================
class Preferences(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    city: Optional[str] = None
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    gear: Optional[str] = None     # اوتوماتيك/عادي
    fuel: Optional[str] = None     # بنزين/ديزل/هايبرد/كهرب

class PoeMessage(BaseModel):
    user_id: str
    conversation_id: str
    text: str

# جلسات
SESSIONS: Dict[str, Preferences] = {}
SESSION_STEP: Dict[str, int] = {}        # مرحلة الحوار
SESSION_PROFILE: Dict[str, Dict] = {}    # نمط الحياة/الاهتمامات

# ===================== مساعدات =====================
def ar_normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def extract_prefs_from_text(text: str) -> Preferences:
    t = ar_normalize(text)
    p = Preferences()

    makes = ["تويوتا","نيسان","هوندا","هيونداي","كيا","لكزس","مرسيدس","بي ام","bmw","شفر","شيفروليه","فورد","مازدا","جيب","دودج","شيري","جيلي","mg"]
    for mk in makes:
        if mk in t:
            p.make = "بي ام" if mk == "bmw" else ("شيفروليه" if mk=="شفر" else mk)
            break

    # موديل بسيط: كلمة بعد الماركة
    if p.make:
        m = re.search(p.make + r"\s+([a-zA-Z\u0600-\u06FF0-9\-]+)", t)
        if m:
            cand = m.group(1)
            if cand not in ["تحت","من","الى","إلى","تخطي"]:
                p.model = cand

    # قير
    if any(k in t for k in ["اوتومات","أوتومات","اتوماتيك","اوتوماتيك","اوتو"]): p.gear = "اوتوماتيك"
    elif any(k in t for k in ["عادي","مانيوال","جير عادي"]): p.gear = "عادي"

    # وقود
    if "هايبرد" in t: p.fuel = "هايبرد"
    elif "كهرب" in t or "كهرباء" in t: p.fuel = "كهرب"
    elif "ديزل" in t: p.fuel = "ديزل"
    elif "بنزين" in t: p.fuel = "بنزين"

    # سنوات
    years = re.findall(r"(20\d{2})", t)
    if len(years) >= 2:
        p.year_min, p.year_max = sorted(map(int, years[:2]))
    elif len(years) == 1:
        p.year_min = int(years[0])

    # سعر: تحت/من-إلى
    m_under = re.search(r"تحت\s*(\d+)", t)
    if m_under:
        val = int(m_under.group(1)); p.price_max = val * (1000 if val < 1000 else 1)

    m_range = re.search(r"من\s*(\d+)\s*(?:الف|ألف)?\s*(?:الى|إلى|-)\s*(\d+)", t)
    if m_range:
        a, b = int(m_range.group(1)), int(m_range.group(2))
        if a < 1000: a *= 1000
        if b < 1000: b *= 1000
        p.price_min, p.price_max = a, b

    # مدن
    cities = ["الرياض","جدة","مكة","المدينة","الدمام","الخبر","بريدة","الطائف","أبها","حائل","ينبع","جازان","تبوك","نجران","القصيم","عسير"]
    for c in cities:
        if c in t:
            p.city = c; break

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

def prefs_to_filters(p: Preferences) -> Dict[str, Any]:
    f: Dict[str, Any] = {}
    if p.make:        f["make"] = p.make
    if p.model:       f["model"] = p.model
    if p.city:        f["cityName"] = p.city
    if p.year_min:    f["yearMin"] = p.year_min
    if p.year_max:    f["yearMax"] = p.year_max
    if p.price_min:   f["priceMin"] = p.price_min
    if p.price_max:   f["priceMax"] = p.price_max
    if p.gear:        f["gear"] = "automatic" if "اوت" in p.gear else "manual"
    if p.fuel:
        mapping = {"بنزين":"gasoline","ديزل":"diesel","هايبرد":"hybrid","كهرب":"electric","كهرباء":"electric"}
        f["fuel"] = mapping.get(p.fuel, p.fuel)
    return f

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
        return ""
    out = []
    for it in items[:5]:
        car = it.get("car") or {}
        title = it.get("title") or f"{car.get('make','')} {car.get('model','')}".strip()
        price = it.get("price", "—")
        city  = (it.get("city") or {}).get("name", "")
        year  = car.get("year", "")
        gear  = car.get("gear", "")
        fuel  = car.get("fuel", "")
        url   = it.get("url", "")
        line  = f"• {title} — سنة {year} — {price} ريال — {city} — {fuel}/{gear}\n{url}"
        out.append(line)
    return "\n\n".join(out)

# ===================== مسارات =====================
@app.get("/")
async def health():
    return {"status": "ok", "service": "Haraj↔Poe Bridge"}

@app.post("/poe")
async def poe(req: Request):
    # دعم فحص الوصول من Poe بدون body
    try:
        data = await req.json()
    except Exception:
        return {"ok": True, "service": "Haraj↔Poe Bridge"}

    if isinstance(data, dict) and data.get("type") == "settings":
        return {"ok": True, "service": "Haraj↔Poe Bridge"}
    if not isinstance(data, dict):
        return {"ok": True, "service": "Haraj↔Poe Bridge"}

    msg_node = data.get("message") or {}
    user_id = data.get("user_id") or data.get("userId") or msg_node.get("user_id") or msg_node.get("userId")
    conv_id = data.get("conversation_id") or data.get("conversationId") or msg_node.get("conversation_id") or msg_node.get("conversationId")
    text    = (data.get("text") or msg_node.get("text") or "").strip()

    if not (user_id and conv_id):
        return {"ok": True, "service": "Haraj↔Poe Bridge"}

    # reset
    low = text.lower()
    if any(k in low for k in ["reset", "امسح", "امسح الشروط", "ابدأ من جديد", "ابدا من جديد"]):
        SESSIONS[conv_id] = Preferences(); SESSION_STEP[conv_id] = 0; SESSION_PROFILE[conv_id] = {}
        return {"text": "تم مسح كل شيء ✅\nاكتب: مرحباً"}

    # تجهيز الحالة
    if conv_id not in SESSIONS: SESSIONS[conv_id] = Preferences()
    if conv_id not in SESSION_PROFILE: SESSION_PROFILE[conv_id] = {}
    step  = SESSION_STEP.get(conv_id, 0)
    prefs = SESSIONS.get(conv_id, Preferences())
    profile = SESSION_PROFILE[conv_id]

    # الترحيب الأول حسب طلبك
    greetings = {"مرحبا","مرحباً","هلا","السلام عليكم","hi","hello","hey","ابدأ","start"}
    if step == 0 or text in greetings:
        SESSION_STEP[conv_id] = 1
        return {"text": "مرحباً بك! أنا مستشارك الشخصي للسيارات. مستعد أساعدك تلقى سيارة تناسب ذوقك واحتياجاتك. خلينا نبدأ! قولي لي: وش السيارة اللي معك حالياً، وش أكثر شي يعجبك فيها؟"}

    # التقاط أي تفضيلات فنية مذكورة بالنص
    extracted = extract_prefs_from_text(text)
    prefs = merge_prefs(prefs, extracted)
    SESSIONS[conv_id] = prefs

    # تدفق الأسئلة الذكية
    if step == 1:
        profile["current_car"] = text
        SESSION_STEP[conv_id] = 2
        return {"text": "ما أكثر شيء تبحث عنه في السيارة الجديدة؟ (قوة، راحة، شكل، اقتصاد بالبنزين، سعر مناسب…)"}
    if step == 2:
        profile["priorities"] = text
        SESSION_STEP[conv_id] = 3
        return {"text": "ما هو استخدامك الأساسي للسيارة؟ (يومي داخل المدينة، سفر، طلعات بر…)"}
    if step == 3:
        profile["usage"] = text
        SESSION_STEP[conv_id] = 4
        return {"text": "تحب السيارات الصغيرة أم الكبيرة؟ (سيدان، هاتشباك، SUV…)"}
    if step == 4:
        profile["size_pref"] = text
        SESSION_STEP[conv_id] = 5
        return {"text": "هل تفضل موديلات حديثة أم لا تمانع السيارات المستعملة؟"}
    if step == 5:
        profile["new_or_used"] = text
        SESSION_STEP[conv_id] = 6
        return {"text": "كم ميزانيتك القصوى تقريبًا؟ اكتب رقم مثل: 70000"}

    if step == 6:
        if not prefs.price_max:
            m = re.search(r"(\d{4,7})", text.replace(",", ""))
            if m: prefs.price_max = int(m.group(1)); SESSIONS[conv_id] = prefs
        SESSION_STEP[conv_id] = 7
        return {"text": "أقل سنة تصنيع تناسبك؟ (مثل: 2018 أو 2020)"}

    if step == 7:
        m = re.search(r"(20\d{2}|19\d{2})", text)
        if m: prefs.year_min = int(m.group(1)); SESSIONS[conv_id] = prefs
        SESSION_STEP[conv_id] = 8  # للبحث

    # تحويل البروفايل لتلميحات عامة (اختياري)
    pri = (profile.get("priorities","") + " " + profile.get("usage","") + " " + profile.get("size_pref","")).lower()
    # (مكان لتكييفات إضافية إن حبيتي)

    # البحث
    filters = prefs_to_filters(prefs)
    try:
        res = await gql_search(filters, page=1, limit=10)
        items = res.get("items", [])
        reply = format_results_ar(items)
    except HTTPException as e:
        if e.status_code == 429:
            reply = "في ضغط حالياً. جرّبي بعد لحظات (Rate limit)."
        else:
            reply = f"صار خطأ أثناء البحث: {e.detail}"

    # fallback + ملخص
    if not reply:
        reply = "ما لقيت نتائج مطابقة تماماً الآن. تبي أوسع الشروط؟"
    summary = " | ".join([f"{k}:{v}" for k, v in prefs.dict().items() if v])
    if summary: reply += "\n\nالتفضيلات الحالية: " + summary

    # إنهاء الجولة
    SESSION_STEP[conv_id] = 0
    return {"text": reply}
