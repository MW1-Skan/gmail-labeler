"""End-to-end test of the full customer journey with all externals faked at the
API boundary (Stripe, Google OAuth, Gmail, Anthropic Batch API).

What real code runs: every FastAPI route, the session cookies, the SQLite layer,
Fernet token encryption, the worker's job loop body, shared/gmail_client (labels,
query, fetch, batchModify), shared/categories prompts and shared/parsing, and the
classifier's batch orchestration. Only the network edges are fake.
"""

import json
from types import SimpleNamespace as NS

import stripe

import web.app as webapp
import web.classifier as classifier
import web.worker as worker
from web import config, db
from web.tests import fakes

STRIPE_EMAIL = "buyer@example.com"           # email used at Stripe checkout
GMAIL_EMAIL = "customer.gmail@example.com"   # Google account that signs in

# subject marker -> category the fake LLM answers ("Junk" is deliberately invalid
# to prove that bad model output falls back to Other instead of crashing).
CLASSIFY_RULES = {
    "TLDR": "Newsletter",
    "FLASH SALE": "Marketing",
    "Order #1234": "Shopping",
    "imposition": "Papers",
    "bank statement": "Finance",
    "[GitHub]": "Dev",
    "verification code": "Security",
    "T2 Lyon": "Immo",
    "dinner": "Junk",
    "School enrolment": "Kids",
    "Community update": "Other",
    "Weekly promo": "Marketing",
}

SECRET_MARKER = "SECRET-BEYOND-EXCERPT-MARKER"
LONG_BODY = ("neighborhood news item " * 14) + SECRET_MARKER  # marker past char 300

EXPECTED_LABELS = {
    "m01": "Newsletter", "m02": "Marketing", "m03": "Shopping", "m04": "Papers",
    "m05": "Finance", "m06": "Dev", "m07": "Security", "m08": "Immo",
    "m09": "Other",   # invalid model answer "Junk" -> fallback
    "m10": "Kids",    # custom category added in the editor
    "m11": "Other",
    "m12": "Marketing",
}


def seed_mailbox(gm: fakes.FakeGmail):
    s = gm.seed_message
    s("m01", "TLDR <hi@tldr.tech>", "Your TLDR digest", "today in tech...")
    s("m02", "Shop <promo@shop.example>", "FLASH SALE 50% off", "everything must go")
    s("m03", "Amazon <ship@amazon.example>", "Order #1234 shipped", "your parcel is on its way")
    s("m04", "impots.gouv <ne-pas-repondre@dgfip.example>", "Votre avis d'imposition", "disponible")
    s("m05", "Bank <no-reply@bank.example>", "Your bank statement is ready", "see attachment")
    s("m06", "GitHub <notify@github.example>", "[GitHub] CI failed on main", "build 123 failed")
    s("m07", "Auth <otp@service.example>", "Your verification code", "code inside")
    s("m08", "SeLoger <alerte@seloger.example>", "Nouvelle annonce T2 Lyon", "voir l'annonce")
    s("m09", "Alice <alice@friends.example>", "Re: dinner saturday", "see you there!")
    s("m10", "School <office@school.example>", "School enrolment forms", "please return signed")
    s("m11", "Assoc <news@assoc.example>", "Community update", LONG_BODY)
    s("m12", "Brand <promo@brand.example>", "Weekly promo digest",
      "<html><body><p>Deals <b>inside</b></p></body></html>", mime="text/html")
    # Already labeled on a previous run: must be excluded by the query and untouched.
    s("m13", "TLDR <hi@tldr.tech>", "Your TLDR digest (yesterday)", "old news",
      label_names=["Newsletter"])


# --- fixture-ish helpers ----------------------------------------------------

def patch_stripe(monkeypatch, payment_status="paid"):
    created = {}

    def fake_create(**kwargs):
        created.update(kwargs)
        return NS(url="https://checkout.stripe.example/pay/cs_test_e2e", id="cs_test_e2e")

    def fake_retrieve(session_id):
        assert session_id == "cs_test_e2e"
        # customer_details mimics stripe-python v15 StripeObject: attribute
        # access only, deliberately NO .get() — it stopped being a dict.
        return NS(payment_status=payment_status,
                  customer_details=NS(email=STRIPE_EMAIL))

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", fake_retrieve)
    return created


def patch_google(monkeypatch, granted_scopes=None):
    flow = fakes.FakeGoogleFlow(granted_scopes if granted_scopes is not None
                                else config.GMAIL_SCOPES)
    monkeypatch.setattr(webapp, "_google_flow", lambda: flow)

    def fake_userinfo_get(url, headers=None, timeout=None):
        assert url == "https://www.googleapis.com/oauth2/v2/userinfo"
        assert headers["Authorization"] == "Bearer ya29.fake-access-token"
        return NS(json=lambda: {"email": GMAIL_EMAIL, "id": "google-sub-e2e"})

    monkeypatch.setattr(webapp.http, "get", fake_userinfo_get)
    return flow


def pay_and_oauth(client, monkeypatch, granted_scopes=None):
    """Landing -> checkout -> /start -> /oauth/start; returns (fake flow, captured
    checkout kwargs), ready for the /oauth/callback step."""
    created = patch_stripe(monkeypatch)
    flow = patch_google(monkeypatch, granted_scopes)

    r = client.post("/checkout")
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://checkout.stripe.example/")

    r = client.get("/start", params={"session_id": "cs_test_e2e"})
    assert r.status_code == 303 and r.headers["location"] == "/oauth/start"

    r = client.get("/oauth/start")
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://accounts.google.example/auth")
    assert flow.authorization_url_kwargs == {"access_type": "offline", "prompt": "consent"}
    return flow, created


def patch_worker_externals(monkeypatch, gm: fakes.FakeGmail, llm: fakes.FakeAnthropic):
    revoked = []
    monkeypatch.setattr(worker, "build", lambda *a, **k: gm)
    monkeypatch.setattr(worker.http, "post",
                        lambda url, **kw: revoked.append((url, kw)) or NS(status_code=200))
    monkeypatch.setattr(classifier, "_client", lambda: llm)
    monkeypatch.setattr(classifier, "POLL_SECONDS", 0)
    return revoked


# --- the big one -------------------------------------------------------------

def test_full_customer_journey(client, monkeypatch):
    flow, created = pay_and_oauth(client, monkeypatch)

    # checkout session was built correctly
    assert created["mode"] == "payment"
    assert created["line_items"][0]["price_data"]["unit_amount"] == 500
    assert "{CHECKOUT_SESSION_ID}" in created["success_url"]

    # Stripe webhook (properly signed) marks the payer as paid
    payload = fakes.checkout_completed_event(STRIPE_EMAIL)
    r = client.post("/stripe-webhook", content=payload, headers={
        "stripe-signature": fakes.stripe_signature(payload, config.STRIPE_WEBHOOK_SECRET)})
    assert r.status_code == 200 and r.json() == {"ok": True}

    # OAuth callback: token exchange, user + encrypted token stored
    r = client.get("/oauth/callback",
                   params={"code": "auth-code-e2e", "state": fakes.FakeGoogleFlow.STATE})
    assert r.status_code == 303 and r.headers["location"] == "/categories"
    # the rebuilt authorization_response is proxy-proof: based on config, not request
    sent = flow.fetch_token_calls[0]["authorization_response"]
    assert sent.startswith(config.OAUTH_REDIRECT_URI + "?")

    with db.conn() as c:
        user = c.execute("SELECT * FROM users WHERE email = ?", (GMAIL_EMAIL,)).fetchone()
    assert user is not None and user["paid_at"] is not None
    assert user["token_encrypted"] is not None            # Fernet-encrypted at rest
    assert b"ya29" not in user["token_encrypted"]         # ...actually encrypted
    user_id = user["id"]

    # categories editor: defaults shown; save with a custom category and a
    # spoofed "Other" row (must be ignored — the canonical fallback is appended)
    r = client.get("/categories")
    assert r.status_code == 200 and "Newsletter" in r.text
    names_in, descs_in = [], []
    for name, desc in db.get_categories(user_id):
        if name != "Other":
            names_in.append(name)
            descs_in.append(desc)
    names_in += ["Kids", "Other"]
    descs_in += ["school and daycare mail", "attempt to hijack the fallback"]
    r = client.post("/categories", data={"name": names_in, "description": descs_in})
    assert r.status_code == 303 and r.headers["location"] == "/status"

    saved = db.get_categories(user_id)
    names = [n for n, _ in saved]
    assert names[-1] == "Other" and names.count("Other") == 1
    assert "Kids" in names
    assert dict(saved)["Other"] == "everything else, and anything ambiguous."

    # a backfill job is queued; status page + API agree
    assert client.get("/status").status_code == 200
    assert client.get("/api/job").json()["status"] == "pending"

    # --- worker phase ---
    gm = fakes.FakeGmail()
    seed_mailbox(gm)
    llm = fakes.FakeAnthropic(CLASSIFY_RULES)
    revoked = patch_worker_externals(monkeypatch, gm, llm)

    job = db.claim_next_job()
    assert job is not None and job["user_id"] == user_id
    assert db.claim_next_job() is None        # claimed exactly once
    worker.run_backfill_job(job)

    # job finished with full progress; batch progress callback path was exercised
    done = db.get_job(job["id"])
    assert done["status"] == "done", done["error"]
    assert done["total"] == 12 and done["done_count"] == 12
    assert llm.retrieve_calls == 2            # in_progress poll -> ended
    assert len(llm.batch_requests) == 2       # 12 emails -> groups of 10 + 2

    # every message got exactly the expected label, and nothing else
    for mid, expected in EXPECTED_LABELS.items():
        got = gm.label_names_of(mid)
        assert got == {expected}, f"{mid}: {got} != {expected}"
    assert gm.label_names_of("m13") == {"Newsletter"}     # untouched
    assert "Kids" in gm.labels_by_name                    # custom label created

    # privacy invariants: only sender/subject/<=300-char excerpt reached the LLM
    all_text = "\n".join(llm.all_prompt_text)
    assert SECRET_MARKER not in all_text
    for line in all_text.splitlines():
        if line.strip().startswith("Excerpt:"):
            assert len(line.split("Excerpt:", 1)[1].strip()) <= 300
    assert "- Kids: school and daycare mail" in llm.all_prompt_text[0]

    # one-shot promise: token revoked at Google and deleted from the DB
    assert len(revoked) == 1
    url, kw = revoked[0]
    assert url == "https://oauth2.googleapis.com/revoke"
    assert kw["params"]["token"] == "ya29.fake-access-token"
    assert db.load_token(user_id) is None

    # customer sees completion
    assert client.get("/api/job").json() == {
        "status": "done", "done": 12, "total": 12, "error": None}


def test_second_run_is_idempotent_and_still_revokes(monkeypatch):
    """A rerun over an already-labeled mailbox processes 0 emails, never calls
    the LLM, completes cleanly, and still honors the revoke promise."""
    user_id = db.upsert_user(GMAIL_EMAIL, "google-sub-e2e")
    db.mark_paid(GMAIL_EMAIL)
    db.save_token(user_id, fakes.make_google_credentials(config.GMAIL_SCOPES).to_json())
    db.seed_default_categories(user_id)

    gm = fakes.FakeGmail()
    seed_mailbox(gm)
    for mid, label in EXPECTED_LABELS.items():          # everything already labeled
        # this user has the default category set (no "Kids"), so label m10 with a
        # default-set label — otherwise it would legitimately be a candidate again
        gm.message_store[mid]["label_ids"].append(
            gm.add_label("Other" if label == "Kids" else label))
    llm = fakes.FakeAnthropic(CLASSIFY_RULES)
    revoked = patch_worker_externals(monkeypatch, gm, llm)

    db.create_job(user_id, kind="backfill")
    worker.run_backfill_job(db.claim_next_job())

    job = db.latest_job_for_user(user_id)
    assert job["status"] == "done" and job["total"] == 0
    assert llm.batch_requests == [] and gm.batch_modify_calls == []
    assert len(revoked) == 1 and db.load_token(user_id) is None


# --- failure modes the local test evening is most likely to hit ---------------

def test_webhook_rejects_wrong_secret(client):
    """`stripe listen` printing a whsec different from web/.env -> every event 400s."""
    payload = fakes.checkout_completed_event(STRIPE_EMAIL)
    r = client.post("/stripe-webhook", content=payload, headers={
        "stripe-signature": fakes.stripe_signature(payload, "whsec_stale_from_last_time")})
    assert r.status_code == 400 and r.json() == {"error": "invalid signature"}


def test_webhook_rejects_garbage_payload(client):
    payload = b"not json at all"
    r = client.post("/stripe-webhook", content=payload, headers={
        "stripe-signature": fakes.stripe_signature(payload, config.STRIPE_WEBHOOK_SECRET)})
    assert r.status_code == 400


def test_start_with_unpaid_session_goes_home(client, monkeypatch):
    patch_stripe(monkeypatch, payment_status="unpaid")
    r = client.get("/start", params={"session_id": "cs_test_e2e"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_oauth_cancel_shows_friendly_retry(client, monkeypatch):
    pay_and_oauth(client, monkeypatch)[0]
    r = client.get("/oauth/callback",
                   params={"error": "access_denied", "state": fakes.FakeGoogleFlow.STATE})
    assert r.status_code == 400
    assert "cancelled" in r.text and "/oauth/start" in r.text


def test_oauth_unchecked_gmail_scope_shows_friendly_retry(client, monkeypatch):
    """Google's granular consent lets users untick the Gmail checkbox."""
    pay_and_oauth(client, monkeypatch,
                  granted_scopes=["openid",
                                  "https://www.googleapis.com/auth/userinfo.email"])
    r = client.get("/oauth/callback",
                   params={"code": "auth-code-e2e", "state": fakes.FakeGoogleFlow.STATE})
    assert r.status_code == 400
    assert "Gmail permission" in r.text and "/oauth/start" in r.text
    with db.conn() as c:  # no half-created account with a useless token
        assert c.execute("SELECT COUNT(*) n FROM users WHERE email = ?",
                         (GMAIL_EMAIL,)).fetchone()["n"] == 0


def test_oauth_token_exchange_failure_shows_friendly_retry(client, monkeypatch):
    flow, _ = pay_and_oauth(client, monkeypatch)

    def boom(**kwargs):
        raise ValueError("(invalid_grant) Bad Request")

    monkeypatch.setattr(flow, "fetch_token", boom)
    r = client.get("/oauth/callback",
                   params={"code": "expired", "state": fakes.FakeGoogleFlow.STATE})
    assert r.status_code == 400 and "try again" in r.text


def test_pages_require_session(client):
    assert client.get("/categories").status_code == 303
    assert client.post("/categories", data={"name": "X", "description": "y"}).status_code == 303
    assert client.get("/status").status_code == 303
    assert client.get("/api/job").status_code == 401
    assert client.get("/oauth/start").status_code == 303   # no paid session -> home


def test_landing_and_privacy_render(client):
    assert "5" in client.get("/").text
    assert client.get("/privacy").status_code == 200
