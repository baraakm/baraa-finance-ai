import os, json, sqlite3, uuid, datetime, mimetypes
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, session, send_file
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types

APP_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(APP_DIR / "finance.db")))
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("FLASK_SECRET", "change-this-secret-before-deploying")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
ALLOWED = {"png", "jpg", "jpeg", "webp", "pdf", "txt", "csv"}
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

SYSTEM_PROMPT = """
أنت Baraa Finance AI، مساعد مالي شخصي يتحدث باللهجة الأردنية البسيطة وبأسلوب واضح وودود.
مهمتك تنظيم الدخل والمصروفات والمدخرات وشرح التقارير، وليس تقديم ضمانات مالية.
الوقت الحالي يمرره الخادم بتوقيت Asia/Amman.
عند تسجيل عملية مالية، لا تخترع مبلغاً أو تاريخاً. لا تسجل عملية إلا إذا ذكر المستخدم مبلغاً واضحاً ونوعها واضح (دخل أو مصروف أو توفير).
ميّز بين: مصروف، دخل/حوالة واردة، استرجاع مبلغ، وتحويل بين حسابات المستخدم (التحويل الداخلي ليس دخلاً ولا مصروفاً).
إذا كانت العبارة ملتبسة، اسأل سؤالاً قصيراً قبل التسجيل.
لا تدّعِ أنك حولت أموالاً أو اتصلت بالبنك. لا تطلب رقم البطاقة أو CVV أو كلمة مرور أو رمز تحقق.
لا تنفذ أي تحويل مالي. أي ربط بنكي مستقبلي يجب أن يكون عبر مزود مرخص وموافقة صريحة لكل عملية.
إذا أرسل المستخدم صورة إيصال أو PDF، حلله فقط واذكر أي مبلغ/تاريخ غير واضح.
أجب باللغة العربية ما لم يطلب المستخدم غير ذلك.
أعد دائماً JSON صالحاً فقط بهذا الشكل:
{"reply":"ردك للمستخدم","transaction":null}
أو عند وجود عملية مؤكدة:
{"reply":"تم تسجيل المصروف...","transaction":{"kind":"expense","amount":5.0,"currency":"JOD","category":"مواصلات","description":"تكسي","date":"YYYY-MM-DD"}}
القيم المسموحة لـ kind: expense, income, refund, transfer, saving.
category أمثلة: طعام، مواصلات، مشتريات، فواتير، صحة، ترفيه، راتب، حوالة، أخرى.
التاريخ الافتراضي هو تاريخ اليوم بتوقيت عمّان. إذا قال المستخدم تاريخاً نسبياً مثل أمس، احسبه من التاريخ الحالي. لا تحول كلاماً افتراضياً أو مثالاً إلى معاملة حقيقية.
"""

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS transactions(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, amount REAL NOT NULL,
            currency TEXT DEFAULT 'JOD', category TEXT, description TEXT,
            tx_date TEXT NOT NULL, created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
            content TEXT NOT NULL, created_at TEXT NOT NULL)""")

init_db()

def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error":"يلزم تسجيل الدخول أولاً."}), 401
        return fn(*args, **kwargs)
    return wrapper

@app.get("/")
def home():
    return send_file(APP_DIR / "index.html")

@app.get("/logo.svg")
def logo():
    return send_file(APP_DIR / "logo.svg", mimetype="image/svg+xml")

@app.get("/manifest.json")
def manifest():
    return send_file(APP_DIR / "manifest.json", mimetype="application/manifest+json")

@app.post("/api/login")
def login():
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return jsonify({"error":"لم يتم ضبط APP_PASSWORD في إعدادات الخادم."}), 503
    data = request.get_json(silent=True) or {}
    if data.get("password") != password:
        return jsonify({"error":"كلمة المرور غير صحيحة."}), 401
    session["logged_in"] = True
    return jsonify({"ok":True})

@app.post("/api/logout")
@require_login
def logout():
    session.clear()
    return jsonify({"ok":True})

def amman_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Amman"))
    except Exception:
        return datetime.datetime.utcnow()

@app.get("/api/summary")
@require_login
def summary():
    with db() as con:
        rows = con.execute("SELECT kind, COALESCE(SUM(amount),0) total FROM transactions GROUP BY kind").fetchall()
        recent = con.execute("SELECT * FROM transactions ORDER BY tx_date DESC, created_at DESC LIMIT 30").fetchall()
        weekly = con.execute("""SELECT kind, COALESCE(SUM(amount),0) total FROM transactions
            WHERE tx_date >= ? GROUP BY kind""",
            ((amman_now().date() - datetime.timedelta(days=6)).isoformat(),)).fetchall()
    totals = {r["kind"]: float(r["total"]) for r in rows}
    week = {r["kind"]: float(r["total"]) for r in weekly}
    return jsonify({
        "totals": totals, "week": week,
        "recent": [dict(r) for r in recent],
        "today": amman_now().date().isoformat(),
        "now": amman_now().isoformat()
    })

@app.get("/api/messages")
@require_login
def get_messages():
    with db() as con:
        rows = con.execute("SELECT role,content,created_at FROM messages ORDER BY id DESC LIMIT 40").fetchall()
    return jsonify(list(reversed([dict(r) for r in rows])))

@app.post("/api/chat")
@require_login
def chat():
    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"error":"أضف GEMINI_API_KEY في Environment على Render أولاً."}), 503
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error":"اكتب رسالة أولاً."}), 400
    if len(message) > 12000:
        return jsonify({"error":"الرسالة طويلة جداً."}), 400

    now = amman_now()
    with db() as con:
        history = con.execute("SELECT role,content FROM messages ORDER BY id DESC LIMIT 12").fetchall()
        txs = con.execute("SELECT kind,amount,category,description,tx_date FROM transactions ORDER BY created_at DESC LIMIT 30").fetchall()
    history = list(reversed(history))
    finance_context = "العمليات الأخيرة المسجلة:\n" + json.dumps([dict(x) for x in txs], ensure_ascii=False)
    contents = [types.Part.from_text(text=f"{SYSTEM_PROMPT}\n\nالوقت الحالي في عمّان: {now.isoformat()}\n{finance_context}")]
    for item in history:
        role = "user" if item["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=item["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))

    try:
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.35
            )
        )
        raw = response.text or "{}"
        result = json.loads(raw)
        reply = str(result.get("reply") or "ما قدرت أجهز رد واضح، جرب مرة ثانية.")
        tx = result.get("transaction")
        saved = None
        if isinstance(tx, dict):
            kind = tx.get("kind")
            amount = tx.get("amount")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                amount = 0
            if kind in {"expense","income","refund","transfer","saving"} and 0 < amount <= 10000000:
                date_text = str(tx.get("date") or now.date().isoformat())
                try:
                    datetime.date.fromisoformat(date_text)
                except ValueError:
                    date_text = now.date().isoformat()
                saved = {
                    "id": str(uuid.uuid4()), "kind": kind, "amount": amount,
                    "currency": "JOD", "category": str(tx.get("category") or "أخرى")[:60],
                    "description": str(tx.get("description") or "")[:250],
                    "tx_date": date_text, "created_at": now.isoformat()
                }
                with db() as con:
                    con.execute("""INSERT INTO transactions
                    (id,kind,amount,currency,category,description,tx_date,created_at)
                    VALUES(:id,:kind,:amount,:currency,:category,:description,:tx_date,:created_at)""", saved)
        with db() as con:
            con.execute("INSERT INTO messages(role,content,created_at) VALUES(?,?,?)", ("user", message, now.isoformat()))
            con.execute("INSERT INTO messages(role,content,created_at) VALUES(?,?,?)", ("assistant", reply, now.isoformat()))
        return jsonify({"reply":reply, "transaction":saved})
    except Exception as exc:
        app.logger.exception("Gemini request failed")
        return jsonify({"error":"صار خطأ أثناء الاتصال بالمساعد. تأكد من مفتاح Gemini وإعدادات النموذج، ثم جرّب مرة ثانية."}), 502

@app.post("/api/upload")
@require_login
def upload():
    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"error":"أضف GEMINI_API_KEY في إعدادات الخادم أولاً."}), 503
    f = request.files.get("file")
    question = (request.form.get("question") or "حلل الملف واشرح أهم المعلومات بالعربية. إذا كان إيصالاً، استخرج المبلغ والتاريخ إن كانا واضحين، ولا تسجل أي عملية تلقائياً.").strip()
    if not f or not f.filename:
        return jsonify({"error":"اختار ملفاً أولاً."}), 400
    name = secure_filename(f.filename)
    ext = name.rsplit(".",1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED:
        return jsonify({"error":"الملفات المدعومة: صور JPG/PNG/WEBP وPDF وTXT وCSV."}), 400
    payload = f.read()
    if len(payload) > 10 * 1024 * 1024:
        return jsonify({"error":"حجم الملف أكبر من 10 ميغابايت."}), 400
    mime = f.mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        prompt = SYSTEM_PROMPT + "\nالوقت الحالي: " + amman_now().isoformat() + "\n\n" + question
        part = types.Part.from_bytes(data=payload, mime_type=mime)
        response = client.models.generate_content(
            model=MODEL,
            contents=[prompt, part],
            config=types.GenerateContentConfig(temperature=0.3)
        )
        return jsonify({"reply":response.text or "ما قدرت أقرأ الملف."})
    except Exception:
        app.logger.exception("Upload analysis failed")
        return jsonify({"error":"تعذر تحليل الملف. جرّب ملفاً أصغر أو بصيغة مدعومة."}), 502

@app.get("/health")
def health():
    return jsonify({"ok":True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
