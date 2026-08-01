"""In-memory fakes for the external services (Gmail, Anthropic, Stripe, Google OAuth).

Each fake implements only the exact surface the production code touches, so any
unexpected call fails loudly instead of silently succeeding.
"""

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from google.oauth2.credentials import Credentials as GoogleCredentials


# --- Stripe webhook signing -------------------------------------------------

def stripe_signature(payload: bytes, secret: str) -> str:
    """Produce a `stripe-signature` header exactly like Stripe / the stripe CLI."""
    t = int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256)
    return f"t={t},v1={mac.hexdigest()}"


def checkout_completed_event(email: str, session_id: str = "cs_test_e2e") -> bytes:
    return json.dumps({
        "id": "evt_test_e2e",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": session_id,
            "object": "checkout.session",
            "customer_details": {"email": email},
        }},
    }).encode()


# --- Google OAuth flow --------------------------------------------------------

def make_google_credentials(scopes: list[str]) -> GoogleCredentials:
    """A real google Credentials object (so to_json/from_authorized_user_info
    round-trips through the worker exactly as in production)."""
    return GoogleCredentials(
        token="ya29.fake-access-token",
        refresh_token="1//fake-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="test-client.apps.googleusercontent.com",
        client_secret="GOCSPX-test-placeholder",
        scopes=scopes,
        expiry=datetime.utcnow() + timedelta(hours=1),
    )


class FakeGoogleFlow:
    """Stands in for google_auth_oauthlib.flow.Flow."""

    STATE = "state-e2e-test"

    def __init__(self, granted_scopes: list[str]):
        self.granted_scopes = granted_scopes
        self.fetch_token_calls: list[dict] = []
        self.authorization_url_kwargs: dict | None = None
        self.credentials = None

    def authorization_url(self, **kwargs):
        self.authorization_url_kwargs = kwargs
        return f"https://accounts.google.example/auth?state={self.STATE}", self.STATE

    def fetch_token(self, authorization_response: str, state: str | None = None):
        self.fetch_token_calls.append(
            {"authorization_response": authorization_response, "state": state})
        assert state == self.STATE, "state mismatch"
        assert "code=" in authorization_response, "missing auth code"
        self.credentials = make_google_credentials(self.granted_scopes)


# --- Gmail --------------------------------------------------------------------

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class _Call:
    """Mimics a googleapiclient request object: .execute() returns the value."""

    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeGmail:
    """In-memory mailbox honoring the exact Gmail API surface the code uses:
    labels().list/create, messages().list/get/batchModify."""

    def __init__(self):
        self.labels_by_name: dict[str, str] = {}  # name -> id
        self.message_store: dict[str, dict] = {}  # id -> {sender, subject, body, mime, label_ids}
        self.batch_modify_calls: list[dict] = []
        self._label_seq = 0

    # -- seeding helpers --
    def add_label(self, name: str) -> str:
        if name not in self.labels_by_name:
            self._label_seq += 1
            self.labels_by_name[name] = f"Label_{self._label_seq}"
        return self.labels_by_name[name]

    def seed_message(self, mid: str, sender: str, subject: str, body: str,
                     mime: str = "text/plain", label_names: list[str] | None = None):
        self.message_store[mid] = {
            "sender": sender, "subject": subject, "body": body, "mime": mime,
            "label_ids": [self.add_label(n) for n in (label_names or [])],
        }

    def label_names_of(self, mid: str) -> set[str]:
        by_id = {v: k for k, v in self.labels_by_name.items()}
        return {by_id[lid] for lid in self.message_store[mid]["label_ids"]}

    # -- API surface --
    def users(self):
        return self

    def labels(self):
        gm = self

        class Labels:
            def list(self, userId):
                return _Call(lambda: {"labels": [
                    {"id": lid, "name": name} for name, lid in gm.labels_by_name.items()]})

            def create(self, userId, body):
                return _Call(lambda: {"id": gm.add_label(body["name"]),
                                      "name": body["name"]})
        return Labels()

    def messages(self):
        gm = self

        class Messages:
            def list(self, userId, q, maxResults, pageToken=None):
                assert "in:inbox" in q, f"query must target the inbox: {q}"
                excluded_names = set(re.findall(r"-label:(\S+)", q))
                excluded_ids = {gm.labels_by_name[n] for n in excluded_names
                                if n in gm.labels_by_name}

                def run():
                    hits = [{"id": mid} for mid, m in gm.message_store.items()
                            if not (set(m["label_ids"]) & excluded_ids)]
                    return {"messages": hits[:maxResults]}
                return _Call(run)

            def get(self, userId, id, format):
                m = gm.message_store[id]

                def run():
                    return {
                        "id": id,
                        "labelIds": list(m["label_ids"]),
                        "snippet": m["body"][:80],
                        "payload": {
                            "mimeType": m["mime"],
                            "headers": [{"name": "From", "value": m["sender"]},
                                        {"name": "Subject", "value": m["subject"]}],
                            "body": {"data": _b64(m["body"])},
                        },
                    }
                return _Call(run)

            def batchModify(self, userId, body):
                def run():
                    gm.batch_modify_calls.append(body)
                    assert not body.get("removeLabelIds"), "labels must only be added"
                    for mid in body["ids"]:
                        for lid in body["addLabelIds"]:
                            if lid not in gm.message_store[mid]["label_ids"]:
                                gm.message_store[mid]["label_ids"].append(lid)
                    return {}
                return _Call(run)
        return Messages()


# --- Anthropic Batch API --------------------------------------------------------

class FakeAnthropic:
    """Fakes client.messages.batches.{create,retrieve,results} plus messages.create.

    Classification rule: subject-substring -> category (supplied by the test), so
    the test can assert exactly which Gmail label every message must end up with.
    """

    def __init__(self, rules: dict[str, str]):
        self.rules = rules
        self.batch_requests: list[dict] = []
        self.all_prompt_text: list[str] = []      # every byte sent to the "LLM"
        self.retrieve_calls = 0
        self.messages = NS(batches=NS(create=self._create, retrieve=self._retrieve,
                                      results=self._results),
                           create=self._sync_create)

    def _classify_subject(self, subject: str) -> str:
        for marker, category in self.rules.items():
            if marker in subject:
                return category
        raise AssertionError(f"no classification rule matches subject: {subject!r}")

    def _answer_for(self, params: dict) -> str:
        user_prompt = params["messages"][0]["content"]
        self.all_prompt_text.append(params["system"])
        self.all_prompt_text.append(user_prompt)
        subjects = re.findall(r"^\s*Subject: (.*)$", user_prompt, re.MULTILINE)
        assert subjects, "user prompt has no Subject lines"
        return json.dumps([self._classify_subject(s) for s in subjects])

    def _create(self, requests):
        self.batch_requests = list(requests)
        for r in requests:
            assert r["params"]["model"], "model must be set"
        return NS(id="msgbatch_fake_e2e", processing_status="in_progress",
                  request_counts=NS(processing=len(requests), succeeded=0, errored=0))

    def _retrieve(self, batch_id):
        assert batch_id == "msgbatch_fake_e2e"
        self.retrieve_calls += 1
        if self.retrieve_calls == 1:  # first poll: still running -> progress callback
            return NS(id=batch_id, processing_status="in_progress",
                      request_counts=NS(processing=1, succeeded=1, errored=0))
        return NS(id=batch_id, processing_status="ended",
                  request_counts=NS(processing=0,
                                    succeeded=len(self.batch_requests), errored=0))

    def _usage_for(self, params: dict, text: str):
        prompt_len = len(params["system"]) + len(params["messages"][0]["content"])
        return NS(input_tokens=prompt_len // 4, output_tokens=len(text) // 4)

    def _results(self, batch_id):
        for r in self.batch_requests:
            text = self._answer_for(r["params"])
            yield NS(custom_id=r["custom_id"],
                     result=NS(type="succeeded",
                               message=NS(content=[NS(type="text", text=text)],
                                          usage=self._usage_for(r["params"], text))))

    def _sync_create(self, **params):
        text = self._answer_for(params)
        return NS(content=[NS(type="text", text=text)],
                  usage=self._usage_for(params, text))
