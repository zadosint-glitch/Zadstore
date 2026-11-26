# -*- coding: utf-8 -*-
"""Fampay payment verification through Gmail (IMAP + App Password).

Flow: bot creates an order with an exact amount -> user pays -> Fampay sends a
"money received" email to your Gmail -> we read it, find the amount, match the
pending order, mark it paid. Each email is used only once (Message-ID stored).
"""
import email
import html as htmllib
import imaplib
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

AMT_RES = [
    re.compile(r"(?:\u20B9|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I),
    re.compile(r"amount[^0-9]{0,20}([0-9][0-9,]*\.[0-9]{1,2})", re.I),
]


def _dec(s):
    try:
        return Decimal(s.replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def find_amounts(text):
    out = []
    for rx in AMT_RES:
        for m in rx.finditer(text):
            d = _dec(m.group(1))
            if d is not None and d not in out:
                out.append(d)
    return out


def _decode(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _body(msg):
    plain, htm = [], []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            txt = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (plain if ct == "text/plain" else htm).append(txt)
    if plain:
        return "\n".join(plain)
    t = "\n".join(htm)
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t))


def parse_mail(raw, credit_words=(), debit_words=()):
    msg = email.message_from_bytes(raw)
    subject = _decode(msg.get("Subject"))
    sender = _decode(msg.get("From"))
    mid = (msg.get("Message-ID") or "").strip() or "noid-%d" % hash(raw)
    try:
        ts = int(parsedate_to_datetime(msg.get("Date")).timestamp())
    except Exception:
        ts = int(time.time())
    text = subject + "\n" + _body(msg)
    low = text.lower()
    credit = True
    if credit_words:
        credit = any(w in low for w in credit_words)
    return {
        "mid": mid, "subject": subject, "from": sender, "ts": ts,
        "text": text, "amounts": find_amounts(text), "credit": credit,
    }


def fetch_mails(user, password, senders, folder="INBOX", days=2, limit=40,
                credit_words=(), debit_words=(), timeout=25):
    """Blocking. Run in a thread. Returns list of parsed mails (newest last)."""
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=timeout)
    try:
        M.login(user, password)
        typ, _ = M.select('"%s"' % folder if " " in folder or "[" in folder else folder, readonly=True)
        if typ != "OK":
            raise RuntimeError("Cannot open mail folder: " + folder)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        ids = set()
        for tok in senders:
            tok = tok.strip()
            if not tok:
                continue
            typ, data = M.search(None, "SINCE", since, "FROM", '"%s"' % tok)
            if typ == "OK" and data and data[0]:
                ids.update(data[0].split())
        ids = sorted(ids, key=lambda x: int(x))[-limit:]
        out = []
        for i in ids:
            typ, data = M.fetch(i, "(BODY.PEEK[])")
            if typ != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
                continue
            try:
                out.append(parse_mail(data[0][1], credit_words, debit_words))
            except Exception:
                continue
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def match(mails, pending, seen):
    """Pure function. Returns list of (order, mail). One mail -> one order."""
    results, used = [], set()
    seen = set(seen)
    for m in sorted(mails, key=lambda x: x["ts"]):
        if m["mid"] in seen or not m["credit"]:
            continue
        cands = []
        for o in pending:
            if o["id"] in used:
                continue
            if Decimal(o["pay"]) in m["amounts"] and o["created"] - 120 <= m["ts"] <= o["expires"] + 120:
                cands.append(o)
        if not cands:
            continue
        low = m["text"].lower()
        byref = [o for o in cands if o["ref"].lower() in low]
        pick = sorted(byref or cands, key=lambda o: o["created"])[0]
        used.add(pick["id"])
        results.append((pick, m))
    return results
