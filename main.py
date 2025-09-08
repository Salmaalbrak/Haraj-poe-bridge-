# -*- coding: utf-8 -*-
import os, re, asyncio
from typing import Optional, Dict, Any, List

import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

# -------- إعدادات هيدرز حراج --------
HARAJ_GRAPHQL_URL = "https://graphql.haraj.com/"
HARAJ_USER_AGENT  = os.getenv("HARAJ_USER_AGENT", "PoeHarajBot/1.0")
HARAJ_ACCESS_TOKEN = os.getenv("HARAJ_ACCESS_TOKEN", "")  # اختياري

DEFAULT_HEADERS: Dict[str, str] = {
    "Content-Type": "application/json",
    "trackId": "",
    "User-Agent": HARAJ_USER_AGENT,
}
if HARAJ_ACCESS_TOKEN:
    DEFAULT_HEADERS["Authorization"] = f"Bearer {HARAJ_ACCESS_TOKEN}"

# -------- تطبيق FastAPI --------
app = FastAPI(title="Haraj↔Poe Bridge", version="1.1.0")

# ===== النماذج =====
class Preferences(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    city: Optional[str] = None
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    # نستخدم هذين الحقلين مؤقتًا لتخزين (الاستخدام/الحجم)
    gear: Optional[str] = None   # usage: city/travel/offroad
    fuel: Optional[str] = None   # size: sedan/suv

class PoeMessage(BaseModel):
    user_id: str
    conversation_id: str
    text: str

# ===== مخزن جلسات بسيط =====
SESSIONS: Dict[str, Preferences] = {}
DIALOG: Dict[str, Dict[str, Any]] = {}  # conversation_id -> {step,prefs}

# ===== أدوات مساعدة للمحادثة =====
AR_PROMPTS = [
    ("price_max", "كم ميزانيتك القصوى تقريبًا؟ (مثلاً 60000)"),
    ("usage",     "استخدامك الأساسي؟ (مدينة / سفر / بر)"),
    ("size",      "تفضّل سيدان صغيرة ولا SUV؟ (اكتب: سيدان أو SUV)"),
    ("make",      "في ماركة محددة ببالك؟ لو ما يفرق قل: أي"),
    ("year_min",  "أقل موديل يناسبك كم؟ (مثلاً 2019)"),
    ("city",      "مدينة الشراء المفضلة؟ (اكتب اسم المدينة أو قل: أي)"),
]

def extract_int(text: str) -> Optional[int]:
    nums = re.findall(r"\d{4}|\d{2,7}", text)
    if not nums:
        return None
    for n in nums:
        if len(n) == 4 and 1980 <= int(n) <= 2035:
            return int(n)
    return int(nums[0])

def pick_usage(text: str) -> Optional[str]:
    t = text.replace(" ", "")
    if "مدينة" in t or "داخلي" in t: return "city"
    if "سفر" in t or "خط" in t:      return "travel"
    if "بر" in t or "اوف" in t:      return "offroad"
    return None

def pick_size(text: str) -> Optional[str]:
    t = text.lower()
    if "suv" in t or "جيب" in t or "كروس" in t: return "suv"
    if "سيدان" in t or "صغيرة" in t:            return "sedan"
    return None

def normalize_make(text: str) -> Optional[str]:
    t = text.strip().lower()
    if t in ["اي", "أي", "مايفرق", "بدون", "no", "any"]: return None
    m = {
        "تويوتا": "تويوتا", "toyota": "تويوتا",
        "هيونداي": "هيونداي", "hyundai": "هيونداي",
        "هوندا": "هوندا", "honda": "هوندا",
        "نيسان": "نيسان", "nissan": "نيسان",
        "كيا": "كيا", "kia": "كيا",
        "شفر": "شفروليه", "شفروليه": "شفروليه", "chevrolet": "شفروليه",
        "ford": "فورد", "فورد": "فورد",
    }
    for k, v in m.items():
        if k in t: return v
    return text.strip() if text.strip() else None

def normalize_city(text: str) -> Optional[str]:
    t = text.strip()
    if t in ["أي", "اي", "any", "no", "بدون"]: return None
    return t or None

def fill_pref(prefs: Preferences, key: str, user_text: str) -> Preferences:
    if key == "price_max":
        v = extract_int(user_text)
        if v: prefs.price_max = v
    elif key == "usage":
        v = pick_usage(user_text)
        if v: prefs.gear = v
    elif key == "size":
        v = pick_size(user_text)
        if v: prefs.fuel = v
    elif key == "make":
        v = normalize_make(user_text)
        if v is not None: prefs.make = v
    elif key == "year_min":
        v = extract_int(user_text)
        if v and 1990 <= v <= 2035: prefs.year_min = v
    elif key == "city":
        v = normalize_city(user_text)
        if v: prefs.city = v
    return prefs

def next_question(prefs: Preferences):
    for key, prompt in AR_PROMPTS:
        if key == "price_max" and not prefs.price_max: return key, prompt
        if key == "usage"     and not prefs.gear:      return key, prompt
        if key == "size"      and not prefs.fuel:      return key, prompt
        if key == "make"      and not prefs.make:      return key, prompt
        if key == "year_min"  and not prefs.year_min:  return key, prompt
        if key == "city"      and not prefs.city:      return key, prompt
    return None, None

def prefs_to_summary(p: Preferences) -> str:
    pairs: List[str] = []
    if p.make:      pairs.append(f"make:{p.make}")
    if p.model:     pairs.append(f"model:{p.model}")
    if p.city:      pairs.append(f"city:{p.city}")
    if p.year_min:  pairs.append(f"year_min:{p.year_min}")
    if p.price_max: pairs.append(f"price_max:{p.price_max}")
    if p.gear:      pairs.append(f"usage:{p.gear}")
    if p.fuel:      pairs.append(f"size:{p.fuel}")
    return " | ".join(pairs)

# ===== تحويل التفضيلات لفلاتر GraphQL (حسب ما يدعمه طرفك) =====
def prefs_to_filters(p: Preferences) -> Dict[str, Any]:
    f: Dict[str, Any] = {}
    if p.make:      f["make"] = p.make
    if p.model:     f["model"] = p.model
    if p.city:      f["city"] = p.city
    if p.year_min:  f["year_min"] = p.year_min
    if p.year_max:  f["year_max"] = p.year_max
    if p.price_min: f["price_min"] = p.price_min
    if p.price_max: f["price_max"] = p.price_max
    return f

# ===== استعلام GraphQL مبسّط =====
SEARCH_QUERY = """
query Search($page:Int!, $limit:Int!, $filters: SearchFilterInput) {
  search(page:$page, limit:$limit, filters:$filters) {
    items { title price year city gear fuel url }
  }
}
"""

async def gql_search(filters: Dict[str, Any], page=1, limit=10) -> Dict[str, Any]:
    payload = {"query": SEARCH_QUERY, "variables": {"page": page, "limit": limit, "filters": filters}}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(HARAJ_GRAPHQL_URL, headers=DEFAULT_HEADERS, json=payload)
        if r.status_code == 429:
            raise HTTPException(status_code=429, detail="rate limited")
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json().get("data", {}).get("search", {})

def format_results_ar(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = []
    for it in items[:10]:
        title = it.get("title", "")
        price = it.get("price", "")
        year  = it.get("year", "")
        city  = (it.get("city") or "")
        url   = it.get("url", "")
        lines.append(f"• {title} — {price} ريال — {year} — {city}\n{url}")
    return "\n\n".join(lines)

# ===== مسارات بسيطة =====
@app.get("/")
async def root():
    return {"ok": True, "service": "Haraj↔Poe Bridge"}

@app.head("/poe")
async def poe_head():
    return {}
@app.get("/poe")
async def poe_check():
    return {"ok": True, "message": "Poe bot is alive"}

# ===== مسار المحادثة مع Poe =====
@app.post("/poe")
async def poe_bridge(msg: PoeMessage, x_poe_access_key: Optional[str] = Header(default=None)):
    txt = (msg.text or "").strip()

    # بدء الترحيب
    if txt in ["", "اهلا", "أهلا", "مرحبا", "مرحباً", "السلام عليكم", "/start"]:
        DIALOG[msg.conversation_id] = {"step": 0, "prefs": Preferences()}
        greet = ("مرحباً بك! أنا مستشارك الشخصي للسيارات 🚗✨\n"
                 "جاهز أساعدك تختار سيارة تناسب ذوقك واحتياجك.\n"
                 "خلّينا نبدأ👌\n\n")
        _, q = next_question(Preferences())
        return {"text": greet + q}

    # إعادة ضبط
    if any(k in txt for k in ["ريست", "reset", "ابدأ من جديد", "مسح"]):
        DIALOG[msg.conversation_id] = {"step": 0, "prefs": Preferences()}
        _, q = next_question(Preferences())
        return {"text": "تمت إعادة الضبط ✅\n" + q}

    # استرجاع أو إنشاء حالة
    state = DIALOG.setdefault(msg.conversation_id, {"step": 0, "prefs": Preferences()})
    prefs: Preferences = state["prefs"]

    # أسئلة متتابعة
    key, _ = next_question(prefs)
    if key:  # لسه نحتاج إجابة
        prefs = fill_pref(prefs, key, txt)
        state["prefs"] = prefs
        key2, q2 = next_question(prefs)
        if key2:
            return {"text": f"تمام ✅\n{q2}"}

    # اكتملت التفضيلات الأساسية — نبحث
    filters = prefs_to_filters(prefs)

    # اقتراح ماركة تلقائي لو المستخدم ما حدّد ومحتاج شيء يمشي البحث
    if not prefs.make:
        if prefs.fuel == "sedan":
            prefs.make = "تويوتا"
        elif prefs.fuel == "suv":
            prefs.make = "هيونداي"
        if prefs.make:
            filters["make"] = prefs.make

    try:
        res = await gql_search(filters, page=1, limit=10)
        items = res.get("items", [])
        reply = format_results_ar(items)
        if not reply:
            reply = "ما لقيت نتائج مناسبة الآن. نقدر نعدّل الميزانية/السنة/المدينة ونحاول من جديد."
    except HTTPException as e:
        if e.status_code == 429:
            reply = "فيه حد (Rate limit) من جهة حراج. جرّبي بعد شوي."
        else:
            reply = f"صار خطأ أثناء البحث: {e.detail}"

    summary = prefs_to_summary(prefs)
    if summary:
        reply += f"\n\n🔎 تفضيلاتك الحالية: {summary}\n"
    reply += "اكتبي (ريست) لإعادة البدء أو عدّلي أي تفضيل برسالة."

    # نحفظ التفضيلات للمحادثة
    SESSIONS[msg.conversation_id] = prefs
    return {"text": reply}
