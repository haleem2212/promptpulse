from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi import FastAPI, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uuid
import json
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests  # <-- for PayPal REST calls
import traceback  # <-- added for better logging

# ---------- NEW: DB imports ----------
from typing import Dict, Any, List, Optional
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, DateTime, Float, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import SQLAlchemyError

# -------------------------------------------------
# Env / paths
# -------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=env_path)

SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASS = os.getenv("SMTP_PASS")

# PayPal config
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_ENV = os.getenv("PAYPAL_ENV", "sandbox").lower()  # "sandbox" or "live"
PAYPAL_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_ENV == "sandbox" else "https://api-m.paypal.com"

USERS_FILE = os.path.join(BASE_DIR, "users.json")
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")

# ---------- NEW: Database URL ----------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ---------- NEW: SQLAlchemy base / engine ----------
Base = declarative_base()
SessionLocal = None
engine = None

if DATABASE_URL:
    # Allow both psycopg2 and "postgresql://" URLs
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# ---------- NEW: Models ----------
class User(Base):
    __tablename__ = "users"
    email = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    password = Column(Text, nullable=True)
    promos = Column(Boolean, default=False)

    has_paid = Column(Boolean, default=False)
    videos_left = Column(Integer, default=0)
    max_credits = Column(Integer, default=0)

    plan_name = Column(String, nullable=True)
    plan_started_at = Column(DateTime, nullable=True)
    plan_expiry = Column(DateTime, nullable=True)
    cancelled = Column(Boolean, default=False)

class Order(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    email = Column(String, nullable=False, index=True)
    plan = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    videos_left = Column(Integer, default=0)
    end_date = Column(String, nullable=True)          # keep as string to match your code
    created_at = Column(DateTime, default=datetime.utcnow)
    provider = Column(String, nullable=True)          # e.g., "paypal"
    paypal_order_id = Column(String, nullable=True)

# ---------- NEW: Create tables if DB is enabled ----------
if engine:
    Base.metadata.create_all(bind=engine)

# -------------------------------------------------
# App
# -------------------------------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="your-secret-key")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def now():
    return datetime.now()

def next_month(from_dt: datetime) -> datetime:
    return from_dt + timedelta(days=30)

# ---------- NEW: DB helper wrappers to preserve your API ----------

def _user_row_to_dict(u: User) -> Dict[str, Any]:
    return {
        "name": u.name,
        "email": u.email,
        "password": u.password,
        "promos": bool(u.promos),
        "has_paid": bool(u.has_paid),
        "videos_left": int(u.videos_left or 0),
        "max_credits": int(u.max_credits or 0),
        "plan_name": u.plan_name,
        "plan_started_at": u.plan_started_at.isoformat() if u.plan_started_at else None,
        "plan_expiry": u.plan_expiry.isoformat() if u.plan_expiry else None,
        "cancelled": bool(u.cancelled),
    }

def _dict_to_user_fields(d: Dict[str, Any]) -> Dict[str, Any]:
    # Convert iso strings back to datetimes for DB fields
    f = dict(d)
    if isinstance(f.get("plan_started_at"), str):
        try:
            f["plan_started_at"] = datetime.fromisoformat(f["plan_started_at"])
        except Exception:
            f["plan_started_at"] = None
    if isinstance(f.get("plan_expiry"), str):
        try:
            f["plan_expiry"] = datetime.fromisoformat(f["plan_expiry"])
        except Exception:
            f["plan_expiry"] = None
    return f

def db_load_users() -> Dict[str, Dict[str, Any]]:
    """Return a dict like your JSON: {email: user_dict}"""
    out: Dict[str, Dict[str, Any]] = {}
    with SessionLocal() as db:
        for u in db.query(User).all():
            out[u.email] = _user_row_to_dict(u)
    return out

def db_save_users(users: Dict[str, Dict[str, Any]]) -> None:
    """Upsert all provided users."""
    with SessionLocal() as db:
        for email, data in users.items():
            fields = _dict_to_user_fields(data)
            row = db.get(User, email)
            if not row:
                row = User(email=email)
                db.add(row)
            # set fields
            row.name = fields.get("name")
            row.password = fields.get("password")
            row.promos = bool(fields.get("promos", False))
            row.has_paid = bool(fields.get("has_paid", False))
            row.videos_left = int(fields.get("videos_left", 0) or 0)
            row.max_credits = int(fields.get("max_credits", 0) or 0)
            row.plan_name = fields.get("plan_name")
            row.plan_started_at = fields.get("plan_started_at")
            row.plan_expiry = fields.get("plan_expiry")
            row.cancelled = bool(fields.get("cancelled", False))
        db.commit()

def db_get_user(email: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        row = db.get(User, email)
        return _user_row_to_dict(row) if row else None

def db_save_single_user(email: str, data: Dict[str, Any]) -> None:
    with SessionLocal() as db:
        fields = _dict_to_user_fields(data)
        row = db.get(User, email)
        if not row:
            row = User(email=email)
            db.add(row)
        row.name = fields.get("name")
        row.password = fields.get("password")
        row.promos = bool(fields.get("promos", False))
        row.has_paid = bool(fields.get("has_paid", False))
        row.videos_left = int(fields.get("videos_left", 0) or 0)
        row.max_credits = int(fields.get("max_credits", 0) or 0)
        row.plan_name = fields.get("plan_name")
        row.plan_started_at = fields.get("plan_started_at")
        row.plan_expiry = fields.get("plan_expiry")
        row.cancelled = bool(fields.get("cancelled", False))
        db.commit()

def db_append_order(order_dict: Dict[str, Any]) -> None:
    """Append a single order (matches your 'load->append->save' pattern)."""
    with SessionLocal() as db:
        row = Order(
            id=order_dict.get("id"),
            email=order_dict.get("email"),
            plan=order_dict.get("plan"),
            amount=float(order_dict.get("amount", 0)),
            videos_left=int(order_dict.get("videos_left", 0) or 0),
            end_date=order_dict.get("end_date"),
            created_at=datetime.fromisoformat(order_dict["created_at"]) if isinstance(order_dict.get("created_at"), str) else (order_dict.get("created_at") or datetime.utcnow()),
            provider=order_dict.get("provider"),
            paypal_order_id=order_dict.get("paypal_order_id"),
        )
        db.add(row)
        db.commit()

def db_load_orders() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with SessionLocal() as db:
        for o in db.query(Order).order_by(Order.created_at.asc()).all():
            out.append({
                "id": o.id,
                "email": o.email,
                "plan": o.plan,
                "amount": o.amount,
                "videos_left": o.videos_left,
                "end_date": o.end_date,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "provider": o.provider,
                "paypal_order_id": o.paypal_order_id,
            })
    return out

# ---------- keep your old JSON helpers but make them DB-aware ----------

def load_users():
    if DATABASE_URL:
        return db_load_users()
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except:
            return {}
    return {}

def save_users(users: dict):
    if DATABASE_URL:
        return db_save_users(users)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=4)

def load_orders():
    if DATABASE_URL:
        return db_load_orders()
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except:
            return []
    return []

def save_orders(orders: list):
    if DATABASE_URL:
        # your code always: orders = load_orders(); orders.append(order); save_orders(orders)
        # In DB mode, only insert the last appended order to avoid duplicating everything.
        if orders:
            db_append_order(orders[-1])
        return
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)

# For styling/logic: a consistent list of plans
PLANS = {
    "basic": {"name": "Basic", "price": 24.99, "videos": 5},
    "pro": {"name": "Pro", "price": 49.99, "videos": 15},
    "elite": {"name": "Elite", "price": 99.99, "videos": 40},
}

def _activate_plan_in_storage(email: str, plan_id: str):
    """Persist the plan activation in users.json (STACK CREDITS)."""
    selected = PLANS[plan_id]
    users = load_users()
    if email in users:
        current_left = int(users[email].get("videos_left", 0))
        new_total = current_left + selected["videos"]

        users[email]["has_paid"] = True
        users[email]["videos_left"] = new_total
        # keep progress bar cap at least as large as current total
        users[email]["max_credits"] = max(int(users[email].get("max_credits", 0)), new_total)
        users[email]["plan_name"] = plan_id
        users[email]["plan_started_at"] = now().isoformat()
        users[email]["plan_expiry"] = None
        users[email]["cancelled"] = False
        save_users(users)

# ---------------------------
# PayPal REST helpers (with detailed logging)
# ---------------------------
def _raise_with_body(resp: requests.Response):
    """Raise HTTPError but include response text for Render logs."""
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        msg = f"PayPal HTTPError {resp.status_code}: {body}"
        print(msg)
        raise requests.HTTPError(msg) from e

def paypal_access_token():
    """Get OAuth token."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise RuntimeError("Missing PAYPAL_CLIENT_ID or PAYPAL_SECRET env vars")
    print(f"[paypal] getting token (env={PAYPAL_ENV}) to {PAYPAL_BASE}")
    auth = (PAYPAL_CLIENT_ID, PAYPAL_SECRET)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials"}
    r = requests.post(f"{PAYPAL_BASE}/v1/oauth2/token", headers=headers, data=data, auth=auth, timeout=20)
    _raise_with_body(r)
    return r.json()["access_token"]

def paypal_create_order(total_gbp: float):
    """Create an order and return its id."""
    token = paypal_access_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": "GBP",
                "value": f"{total_gbp:.2f}",
            }
        }]
    }
    print(f"[paypal] create order body={body}")
    r = requests.post(f"{PAYPAL_BASE}/v2/checkout/orders", headers=headers, json=body, timeout=20)
    _raise_with_body(r)
    print(f"[paypal] create order response={r.json()}")
    return r.json()

def paypal_capture_order(order_id: str):
    token = paypal_access_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    print(f"[paypal] capture order {order_id}")
    r = requests.post(f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture", headers=headers, timeout=20)
    _raise_with_body(r)
    print(f"[paypal] capture response={r.json()}")
    return r.json()

# -------------------------------------------------
# Auth
# -------------------------------------------------
@app.post("/signup")
async def signup(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    terms: str = Form(...),
    promos: str = Form(None),
):
    users = load_users()
    if email in users:
        return templates.TemplateResponse(
            "confirmation.html",
            {"request": request, "message": "Email already in use, please login."},
        )

    users[email] = {
        "name": name,
        "email": email,
        "password": password,
        "promos": bool(promos),
        "has_paid": False,
        "videos_left": 0,
        "max_credits": 0,
        "plan_name": None,
        "plan_started_at": None,
        "plan_expiry": None,       # only set when cancelled
        "cancelled": False,        # indicates scheduled to end at plan_expiry
    }
    save_users(users)

    # Log them in
    request.session["user"] = email
    request.session["has_paid"] = False
    request.session["videos_left"] = 0
    request.session["max_credits"] = 0

    return RedirectResponse(url="/?message=Account+created+successfully", status_code=302)

@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    users = load_users()
    user = users.get(email)
    if user and user.get("password") == password:
        request.session["user"] = email
        request.session["has_paid"] = user.get("has_paid", False)
        request.session["videos_left"] = user.get("videos_left", 0)
        request.session["max_credits"] = user.get("max_credits", 0)
        return RedirectResponse(url="/?message=Logged+in+successfully", status_code=302)

    return templates.TemplateResponse(
        "confirmation.html",
        {"request": request, "message": "Invalid email or password."},
    )

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/?message=Logged+out+successfully")

# -------------------------------------------------
# Pages
# -------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, message: str = None):
    email = request.session.get("user")
    has_paid = request.session.get("has_paid")
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "message": message, "email": email, "has_paid": has_paid},
    )

@app.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    email = request.session.get("user")
    has_paid = request.session.get("has_paid")
    return templates.TemplateResponse(
        "pricing.html", {"request": request, "email": email, "has_paid": has_paid}
    )

@app.get("/checkout", response_class=HTMLResponse)
async def checkout(request: Request, plan: str = "basic"):
    selected = PLANS.get(plan, PLANS["basic"])
    email = request.session.get("user")

    user = None
    if email:
        users = load_users()
        user = users.get(email)

    return templates.TemplateResponse(
        "checkout.html",
        {
            "request": request,
            "plan_id": plan,
            "plan_name": selected["name"],
            "plan_price": selected["price"],
            "video_limit": selected["videos"],
            "email": email,
            "user": user,
            "paypal_client_id": os.getenv("PAYPAL_CLIENT_ID"),
            "paypal_plan_basic": os.getenv("PAYPAL_PLAN_BASIC"),
            "paypal_plan_pro": os.getenv("PAYPAL_PLAN_PRO"),
            "paypal_plan_elite": os.getenv("PAYPAL_PLAN_ELITE"),
        },
    )

# -------------------------------------------------
# Payment (your existing confirm route left as-is, but STACK credits)
# -------------------------------------------------
@app.post("/confirm-payment")
async def confirm_payment(
    request: Request,
    plan_id: str = Form(...),
    email: str = Form(None),
):
    selected = PLANS.get(plan_id)
    if not selected:
        return HTMLResponse("Invalid plan selected.", status_code=400)

    users = load_users()

    form = await request.form()
    if not email:
        email = form.get("email")

    if email and email not in users:
        name = form.get("name")
        password = form.get("password")
        promos = bool(form.get("promotions"))
        if not (name and password):
            return RedirectResponse(
                url="/pricing?message=Please+sign+up+before+purchase",
                status_code=302,
            )
        users[email] = {
            "name": name,
            "email": email,
            "password": password,
            "promos": promos,
            "has_paid": False,
            "videos_left": 0,
            "max_credits": 0,
            "plan_name": None,
            "plan_started_at": None,
            "plan_expiry": None,
            "cancelled": False,
        }

    # Record order
    end_date = (now() + timedelta(days=30)).strftime("%Y-%m-%d")
    orders = load_orders()
    orders.append(
        {
            "id": str(uuid.uuid4()),
            "email": email,
            "plan": selected["name"],
            "amount": selected["price"],
            "videos_left": selected["videos"],
            "end_date": end_date,
            "created_at": now().isoformat(),
        }
    )
    save_orders(orders)

    # STACK credits
    current_left = int(users.get(email, {}).get("videos_left", 0))
    new_left = current_left + selected["videos"]

    request.session["user"] = email
    request.session["has_paid"] = True
    request.session["videos_left"] = new_left
    request.session["max_credits"] = max(int(users.get(email, {}).get("max_credits", 0)), new_left)

    if email in users:
        users[email]["has_paid"] = True
        users[email]["videos_left"] = new_left
        users[email]["max_credits"] = max(int(users[email].get("max_credits", 0)), new_left)
        users[email]["plan_name"] = plan_id
        users[email]["plan_started_at"] = now().isoformat()
        users[email]["plan_expiry"] = None
        users[email]["cancelled"] = False
        save_users(users)

    return RedirectResponse(url="/?message=Payment+Confirmed", status_code=302)

# -------------------------------------------------
# PayPal endpoints (STACK credits)
# -------------------------------------------------
@app.post("/paypal/create-order")
async def paypal_create(request: Request, plan_id: str = Form(...)):
    plan = PLANS.get(plan_id)
    if not plan:
        return JSONResponse({"error": "Invalid plan"}, status_code=400)
    try:
        order = paypal_create_order(plan["price"])
        return JSONResponse({"id": order["id"]})
    except Exception as e:
        print("[paypal] create-order failed:", repr(e))
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/paypal/capture-order")
async def paypal_capture(request: Request, plan_id: str = Form(...), order_id: str = Form(...)):
    plan = PLANS.get(plan_id)
    if not plan:
        return JSONResponse({"error": "Invalid plan"}, status_code=400)

    email = request.session.get("user")
    users = load_users()

    form = await request.form()
    if not email:
        email = form.get("email")

    if email and email not in users:
        name = form.get("name")
        password = form.get("password")
        promos = bool(form.get("promotions"))
        if not (name and password):
            return JSONResponse({"error": "Missing account details"}, status_code=400)
        users[email] = {
            "name": name,
            "email": email,
            "password": password,
            "promos": promos,
            "has_paid": False,
            "videos_left": 0,
            "max_credits": 0,
            "plan_name": None,
            "plan_started_at": None,
            "plan_expiry": None,
            "cancelled": False,
        }
        save_users(users)

    try:
        result = paypal_capture_order(order_id)
        status = result.get("status", "")
        if status != "COMPLETED":
            return JSONResponse({"error": f"Capture failed: {status}"}, status_code=400)
    except Exception as e:
        print("[paypal] capture-order failed:", repr(e))
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

    # Record order locally
    end_date = (now() + timedelta(days=30)).strftime("%Y-%m-%d")
    orders = load_orders()
    orders.append(
        {
            "id": str(uuid.uuid4()),
            "paypal_order_id": order_id,
            "email": email,
            "plan": plan["name"],
            "amount": plan["price"],
            "videos_left": plan["videos"],
            "end_date": end_date,
            "created_at": now().isoformat(),
            "provider": "paypal",
        }
    )
    save_orders(orders)

    # STACK credits in storage + session
    current_left = int(users.get(email, {}).get("videos_left", 0))
    new_left = current_left + plan["videos"]

    request.session["user"] = email
    request.session["has_paid"] = True
    request.session["videos_left"] = new_left
    request.session["max_credits"] = max(int(users.get(email, {}).get("max_credits", 0)), new_left)

    _activate_plan_in_storage(email, plan_id)

    return JSONResponse({"ok": True})

@app.post("/paypal/activate")
async def paypal_activate(request: Request):
    data = await request.json()
    subscription_id = data.get("subscription_id")
    plan_key = data.get("plan_key")
    email = request.session.get("user")

    if not email:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)

    users = load_users()
    user = users.get(email)
    if not user:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)

    if plan_key == "basic":
        add_credits = 5
    elif plan_key == "pro":
        add_credits = 15
    elif plan_key == "elite":
        add_credits = 40
    else:
        return JSONResponse({"ok": False, "error": "Unknown plan"}, status_code=400)

    user["has_paid"] = True
    current_left = int(user.get("videos_left", 0))
    user["videos_left"] = current_left + add_credits
    user["max_credits"] = max(int(user.get("max_credits", 0)), user["videos_left"])
    user["plan_name"] = plan_key
    user["plan_started_at"] = now().isoformat()
    user["plan_expiry"] = None
    user["cancelled"] = False
    save_users(users)

    request.session["has_paid"] = True
    request.session["videos_left"] = user["videos_left"]
    request.session["max_credits"] = user["max_credits"]

    return JSONResponse({"ok": True})

# -------------------------------------------------
# Account page
# -------------------------------------------------
@app.get("/account", response_class=HTMLResponse)
async def account(request: Request):
    email = request.session.get("user")
    if not email:
        return RedirectResponse(url="/")
    users = load_users()
    user = users.get(email, {})
    has_paid = request.session.get("has_paid")
    videos_left = request.session.get("videos_left")
    max_credits = request.session.get("max_credits")
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": user,
            "has_paid": has_paid,
            "videos_left": videos_left,
            "max_credits": max_credits,
        },
    )

# Cancel membership (keeps access until plan_expiry)
@app.post("/cancel-membership")
async def cancel_membership(request: Request, password: str = Form(...)):
    email = request.session.get("user")
    if not email:
        return RedirectResponse(url="/?message=Please+login+first", status_code=302)

    users = load_users()
    user = users.get(email)
    if not user or user.get("password") != password:
        return RedirectResponse(url="/account?message=Incorrect+password", status_code=302)

    if user.get("cancelled"):
        return RedirectResponse(url="/account?message=Already+cancelled", status_code=302)

    started_str = user.get("plan_started_at")
    if started_str:
        try:
            started = datetime.fromisoformat(started_str)
        except Exception:
            started = now()
    else:
        started = now()
    expiry = next_month(started)

    user["cancelled"] = True
    user["plan_expiry"] = expiry.isoformat()
    save_users(users)

    return RedirectResponse(url="/account?message=Membership+cancelled", status_code=302)

# -------------------------------------------------
# Generate page & video
# -------------------------------------------------
def _enforce_expiry_in_session(request: Request):
    """If user's plan was cancelled and expiry passed, flip has_paid False and zero credits."""
    email = request.session.get("user")
    if not email:
        return
    users = load_users()
    user = users.get(email)
    if not user:
        return
    plan_expiry = user.get("plan_expiry")
    cancelled = user.get("cancelled", False)
    if cancelled and plan_expiry:
        try:
            expiry_dt = datetime.fromisoformat(plan_expiry)
            if now() > expiry_dt:
                user["has_paid"] = False
                user["videos_left"] = 0
                save_users(users)
                request.session["has_paid"] = False
                request.session["videos_left"] = 0
                request.session["max_credits"] = user.get("max_credits", 0)
        except Exception:
            pass

@app.get("/generate", response_class=HTMLResponse)
async def generate(request: Request):
    email = request.session.get("user")
    if not email:
        return RedirectResponse(url="/?message=please+login+first")

    _enforce_expiry_in_session(request)

    has_paid = request.session.get("has_paid")
    videos_left = request.session.get("videos_left")
    max_credits = request.session.get("max_credits")

    if not has_paid:
        return RedirectResponse(url="/pricing?message=Upgrade+to+access", status_code=302)

    return templates.TemplateResponse(
        "generate.html",
        {
            "request": request,
            "email": email,
            "has_paid": has_paid,
            "videos_left": videos_left,
            "plan_total": max_credits,
        },
    )

@app.post("/generate-video", response_class=HTMLResponse)
async def generate_video(request: Request, prompt: str = Form(...)):
    email = request.session.get("user")
    has_paid = request.session.get("has_paid")
    videos_left = request.session.get("videos_left", 0)
    max_credits = request.session.get("max_credits", 1)

    _enforce_expiry_in_session(request)
    has_paid = request.session.get("has_paid")
    videos_left = request.session.get("videos_left", videos_left)

    if not email or not has_paid or videos_left <= 0:
        return RedirectResponse(url="/pricing?message=Upgrade+to+generate+videos", status_code=302)

    try:
        import replicate
        REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
        os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

        output = replicate.run(
            "minimax/hailuo-02",
            input={"prompt": prompt},
        )
        generated_video_url = output[0] if isinstance(output, list) else output
    except Exception:
        generated_video_url = "https://sample-videos.com/video321/mp4/720/big_buck_bunny_720p_1mb.mp4"

    videos_left = int(videos_left) - 1
    if videos_left < 0:
        videos_left = 0

    request.session["videos_left"] = videos_left

    users = load_users()
    if email in users:
        users[email]["videos_left"] = videos_left
        save_users(users)

    return templates.TemplateResponse(
        "generate.html",
        {
            "request": request,
            "videos_left": videos_left,
            "plan_total": max_credits,
            "generated_video_url": generated_video_url,
            "prompt": prompt,
            "email": email,
            "has_paid": True,
        },
    )

# -------------------------------------------------
# Run
# -------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
