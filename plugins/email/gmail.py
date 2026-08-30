"""Read-only Gmail access (Phase 6) — OAuth + API, no third-party SDK.

Mirrors the Phase-4 Google scaffold from ``plugins/calendar`` (authorization
code with a one-shot local-loopback redirect; client credentials live in a
local JSON file; the *refresh token* lives in a separate local token file).
Scope is ``gmail.readonly`` — the assistant can read mail, never send or
modify. HTTP goes through ``httpx`` module verbs (tests monkeypatch those).

Everything time-related is naive-local, matching the rest of PLP.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.header
import email.utils
import json
import quopri
import re
import socket
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
    "?client_id={cid}&redirect_uri={redir}&response_type=code"
    f"&scope={GMAIL_SCOPE}"
    "&access_type=offline&prompt=consent&state=plp-email"
)
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://www.googleapis.com/gmail/v1"


# ---------------------------------------------------------------- credentials

def load_client_credentials(path: Path) -> dict:
    """Load the OAuth client JSON (GCP exports nest the client under
    ``installed`` — accept both shapes). Raises ValueError when unusable."""
    if not path.exists():
        raise ValueError(f"credentials file not found: {path}")
    doc = json.loads(path.read_text())
    base = dict(doc)
    if "installed" in doc and isinstance(doc["installed"], dict):
        base.update(doc["installed"])
    for key in ("client_id", "client_secret"):
        if not base.get(key):
            raise ValueError(f"missing {key!r} in {path}")
    return base


# ---------------------------------------------------------------- token file

def load_token(token_file: Path) -> dict | None:
    if not token_file.exists():
        return None
    try:
        doc = json.loads(token_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return doc if doc.get("refresh_token") else None


def save_token(token_file: Path, doc: dict) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(doc, indent=2) + "\n")


def get_access_token(token_file: Path, creds_path: Path, log=None) -> str | None:
    """Valid access token, refreshing when expired (and persisting the new
    one). None when there is no token at all (connect has never run)."""
    if log is None:
        import logging

        log = logging.getLogger("plp.email.token")
    doc = load_token(token_file)
    if doc is None:
        return None
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    if doc.get("expires_at", 0) > now + 60:
        return doc.get("access_token")
    creds = load_client_credentials(creds_path)
    r = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": doc["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
        timeout=30.0,
    )
    r.raise_for_status()
    new = r.json()
    doc = {
        **doc,
        "access_token": new["access_token"],
        "expires_at": now + int(new.get("expires_in", 3600)),
    }
    if new.get("refresh_token"):
        doc["refresh_token"] = new["refresh_token"]
    save_token(token_file, doc)
    log.info("Gmail access token refreshed")
    return doc["access_token"]


# ---------------------------------------------------------------- connect

def connect(
    credentials_path: Path,
    token_file: Path,
    open_browser: bool = True,
    code: str | None = None,
    log=None,
) -> dict:
    """One-time Gmail connect: OAuth2 authorization-code flow with a
    local-loopback redirect (read-only scope). ``code`` pre-filled skips the
    redirect wait (manual paste / tests). Writes the token file; returns a
    receipt dict."""
    if log is None:
        import logging

        log = logging.getLogger("plp.email.connect")

    try:
        base = load_client_credentials(credentials_path)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    redir = f"http://127.0.0.1:{port}/"

    received: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", ["?"])[0] == "plp-email" and q.get("code"):
                received["code"] = q["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>PLP email connected - you can close this tab.</h2></body></html>"
                )
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *a):  # silence
            pass

    url = GOOGLE_AUTH_URL.format(cid=base["client_id"], redir=redir)
    if code is None:
        server = HTTPServer(("127.0.0.1", port), _Handler)
        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()
        if open_browser:
            webbrowser.open(url)
        log.info("Gmail connect: open this URL to authorize:\n%s", url)
        t.join(timeout=300)
        server.server_close()
        if "code" not in received:
            return {"status": "error", "error": "no authorization code received (timeout or rejection)"}
        code = received["code"]

    r = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": base["client_id"],
            "client_secret": base["client_secret"],
            "redirect_uri": redir,
        },
        timeout=30.0,
    )
    r.raise_for_status()
    tokens = r.json()
    if "refresh_token" not in tokens:
        return {
            "status": "error",
            "error": "Google did not return a refresh token "
            "(already authorized? revoke access in your Google account, then retry)",
        }

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    save_token(
        token_file,
        {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": now + int(tokens.get("expires_in", 3600)),
        },
    )
    log.info("Gmail connected (read-only); token saved to %s", token_file)
    return {"status": "ok", "scope": GMAIL_SCOPE, "token_file": str(token_file)}


# ---------------------------------------------------------------- message

@dataclass
class GmailMessage:
    id: str
    sender: str
    subject: str
    date: dt.datetime | None  # naive local
    body: str


def _decode_header(value: str | None) -> str:
    """RFC 2047 encoded-word decode (=?utf-8?...?=)."""
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return value
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode("utf-8", "replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _decode_data(data: str, encoding: str) -> str:
    if not data:
        return ""
    if encoding == "q":
        # A "=" at end-of-line is a QP soft break (RFC 2045 line wrap) —
        # Python's quopri decodes =XX escapes but not these, so strip them.
        data = re.sub(r"=\r\n", "", data)
        data = re.sub(r"=\n", "", data)
        return quopri.decodestring(data.encode("utf-8", "replace")).decode("utf-8", "replace")
    try:
        return base64.b64decode(data).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return data


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")


def _extract_text(payload: dict) -> str:
    """Walk a message payload tree; prefer text/plain, fall back to html."""
    plain: list[str] = []
    html: list[str] = []

    def walk(node: dict | None) -> None:
        if not node:
            return
        mime = (node.get("mimeType") or "").lower()
        if mime.startswith("text/"):
            body = node.get("body") or {}
            data = body.get("data")
            if data:
                text = _decode_data(data, body.get("dataEncoding", "q"))
                if mime == "text/plain":
                    plain.append(text)
                else:
                    html.append(text)
        for sub in node.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        text = "\n".join(html)
        text = _TAG.sub(" ", text)
        return _WS.sub(" ", text).strip()
    return ""


def _header_value(payload: dict, name: str) -> str:
    for hf in payload.get("headerFields") or []:
        if hf.get("name", "").lower() == name:
            return hf.get("value") or ""
    for h in payload.get("headers") or []:
        if h.get("name", "").lower() == name:
            return h.get("value") or ""
    return ""


def _message_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        aware = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if aware is None:
        return None
    return aware.astimezone().replace(tzinfo=None)  # naive local


def message_from_resource(resource: dict) -> GmailMessage:
    """Build a decoded GmailMessage from a ``format=full`` API resource."""
    payload = resource.get("payload") or {}
    body = _extract_text(payload)
    return GmailMessage(
        id=resource.get("id", ""),
        sender=_decode_header(_header_value(payload, "from")),
        subject=_decode_header(_header_value(payload, "subject")),
        date=_message_date(_header_value(payload, "date")),
        body=body,
    )


# ---------------------------------------------------------------- client

class GmailClient:
    """Minimal read-only Gmail API client. ``token_provider`` yields the
    current access token (the plugin wires in refresh logic)."""

    def __init__(self, api_base: str, token_provider, timeout: float = 30.0) -> None:
        self.base = api_base.rstrip("/")
        self._token = token_provider
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def search(self, query: str, max_results: int = 50) -> list[str]:
        """Message ids matching a Gmail search query (e.g. ``newer_than:2d``)."""
        r = httpx.get(
            f"{self.base}/users/me/messages",
            params={"q": query, "maxResults": max_results},
            headers=self._headers(),
            timeout=self.timeout,
        )
        r.raise_for_status()
        return [m.get("id", "") for m in r.json().get("messages", []) if m.get("id")]

    def fetch(self, message_id: str) -> GmailMessage:
        r = httpx.get(
            f"{self.base}/users/me/messages/{message_id}",
            params={"format": "full"},
            headers=self._headers(),
            timeout=self.timeout,
        )
        r.raise_for_status()
        return message_from_resource(r.json())
