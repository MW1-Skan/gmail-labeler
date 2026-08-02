"""FastAPI app: landing → Stripe Checkout → Google OAuth → categories → job status.

Run:  .venv/bin/uvicorn web.app:app --reload   (from the repo root)
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import requests as http
import stripe
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google_auth_oauthlib.flow import Flow
from itsdangerous import BadSignature, URLSafeSerializer

from web import config, db
from shared.categories import FALLBACK_CATEGORY

log = logging.getLogger("web")
stripe.api_key = config.STRIPE_SECRET_KEY

app = FastAPI(title="Inbox Labeler")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("startup")
def startup():
    db.init_db()


# --- session cookie helpers ----------------------------------------------

def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(config.SESSION_SECRET, salt="session")


def get_session(request: Request) -> dict:
    raw = request.cookies.get("session")
    if not raw:
        return {}
    try:
        return _signer().loads(raw)
    except BadSignature:
        return {}


def set_session(response, data: dict):
    response.set_cookie("session", _signer().dumps(data),
                        httponly=True, samesite="lax", max_age=7 * 24 * 3600,
                        secure=config.BASE_URL.startswith("https"))


# --- landing & payment ----------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html",
                                      {"price_eur": config.PRICE_EUR_CENTS / 100})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


@app.post("/checkout")
def checkout():
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "eur",
                "unit_amount": config.PRICE_EUR_CENTS,
                "product_data": {"name": "Inbox cleanup — label your past 365 days of Gmail"},
            },
            "quantity": 1,
        }],
        success_url=f"{config.BASE_URL}/start?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{config.BASE_URL}/",
    )
    return RedirectResponse(session.url, status_code=303)


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, config.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    if event["type"] == "checkout.session.completed":
        # Read from the signature-verified raw payload: plain dicts, immune to
        # StripeObject API differences across stripe-python versions.
        obj = json.loads(payload)["data"]["object"]
        email = (obj.get("customer_details") or {}).get("email")
        if email:
            db.mark_paid(email)
    return {"ok": True}


@app.get("/start")
def start(request: Request, session_id: str):
    """Stripe success redirect — verify payment, then send to Google OAuth."""
    checkout_session = stripe.checkout.Session.retrieve(session_id)
    if checkout_session.payment_status != "paid":
        return RedirectResponse("/", status_code=303)
    # attribute access: StripeObject stopped being a dict in stripe-python v15
    details = checkout_session.customer_details
    email = details.email if details else None
    if email:
        db.mark_paid(email)
    response = RedirectResponse("/oauth/start", status_code=303)
    set_session(response, {"paid": True, "stripe_email": email})
    return response


# --- Google OAuth ---------------------------------------------------------

def _google_flow() -> Flow:
    return Flow.from_client_config(
        {"web": {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=config.GMAIL_SCOPES,
        redirect_uri=config.OAUTH_REDIRECT_URI,
    )


@app.get("/oauth/start")
def oauth_start(request: Request):
    session = get_session(request)
    if not session.get("paid"):
        return RedirectResponse("/", status_code=303)
    flow = _google_flow()
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["oauth_state"] = state
    response = RedirectResponse(auth_url, status_code=303)
    set_session(response, session)
    return response


def _oauth_retry_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"<p>{message}</p><p><a href='/oauth/start'>Connect your Google account again</a></p>",
        status_code=400)


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    session = get_session(request)
    if not session.get("paid") or "oauth_state" not in session:
        return RedirectResponse("/", status_code=303)
    if request.query_params.get("error"):  # user clicked Cancel on the consent screen
        return _oauth_retry_page("Google sign-in was cancelled, so we have no access "
                                 "to your mailbox. Your payment is still valid.")
    flow = _google_flow()
    # Rebuild the callback URL on the configured base: behind a proxy/tunnel (and in
    # plain local dev) request.url is http://, which oauthlib rejects and Google may
    # consider a redirect-URI mismatch.
    authorization_response = f"{config.OAUTH_REDIRECT_URI}?{request.url.query}"
    try:
        flow.fetch_token(authorization_response=authorization_response,
                         state=session["oauth_state"])
    except Exception as e:
        log.warning("OAuth token exchange failed: %s", e)
        return _oauth_retry_page("Google sign-in failed or the link expired. "
                                 "Your payment is still valid — please try again.")
    creds = flow.credentials
    if config.GMAIL_MODIFY_SCOPE not in (creds.scopes or []):
        return _oauth_retry_page(
            "The Gmail permission was not granted on the consent screen — we need it "
            "to create your labels (we only ever add labels, never delete or send "
            "anything). Please tick the Gmail checkbox this time.")

    userinfo = http.get("https://www.googleapis.com/oauth2/v2/userinfo",
                        headers={"Authorization": f"Bearer {creds.token}"},
                        timeout=10).json()
    email, sub = userinfo.get("email"), userinfo.get("id")
    if not email:
        return HTMLResponse("Could not read your Google account email.", status_code=400)

    user_id = db.upsert_user(email, sub)
    db.mark_paid(email)  # Gmail account may differ from the Stripe receipt email
    db.save_token(user_id, creds.to_json())
    db.seed_default_categories(user_id)

    session.update({"user_id": user_id, "email": email})
    response = RedirectResponse("/categories", status_code=303)
    set_session(response, session)
    return response


# --- categories editor ----------------------------------------------------

@app.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request):
    session = get_session(request)
    if "user_id" not in session:
        return RedirectResponse("/", status_code=303)
    categories = db.get_categories(session["user_id"])
    return templates.TemplateResponse(request, "categories.html", {
        "categories": categories,
        "fallback": FALLBACK_CATEGORY,
        "max_categories": config.MAX_CATEGORIES,
    })


@app.post("/categories")
async def categories_save(request: Request):
    session = get_session(request)
    if "user_id" not in session:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    names = form.getlist("name")
    descriptions = form.getlist("description")

    categories, seen = [], set()
    for name, desc in zip(names, descriptions):
        name, desc = name.strip()[:40], desc.strip()[:300]
        if not name or not desc or name.lower() in seen or name == FALLBACK_CATEGORY:
            continue
        seen.add(name.lower())
        categories.append((name, desc))
    categories = categories[:config.MAX_CATEGORIES - 1]
    # The fallback is always present and always last
    categories.append((FALLBACK_CATEGORY,
                       "everything else, and anything ambiguous."))

    db.replace_categories(session["user_id"], categories)
    db.create_job(session["user_id"], kind="backfill")
    return RedirectResponse("/status", status_code=303)


# --- job status -----------------------------------------------------------

@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    session = get_session(request)
    if "user_id" not in session:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "status.html", {"email": session.get("email")})


@app.get("/api/job")
def job_status(request: Request):
    session = get_session(request)
    if "user_id" not in session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    job = db.latest_job_for_user(session["user_id"])
    if not job:
        return {"status": "none"}
    return {"status": job["status"], "done": job["done_count"],
            "total": job["total"], "error": job["error"]}
