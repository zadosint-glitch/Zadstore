# -*- coding: utf-8 -*-
"""
ZAD OSINT FILE STORE BOT
Telegram file / project seller: coins wallet, Fampay UPI QR (auto verify via Gmail),
gift codes, admin panel, GitHub auto-backup (redeploy = auto restore).
"""
import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import mailpay
from backup import GitHub, GitHubError
from db import Store, now
from ui import S, dt, esc, money, sc

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("zadstore")
HTML = ParseMode.HTML


# ============================ CONFIG ============================
def env(k, d=""):
    return (os.environ.get(k, d) or "").strip()


BOT_TOKEN = env("BOT_TOKEN")
OWNER_ID = int(env("OWNER_ID", "0") or 0)
BRAND = env("BRAND", "ZAD OSINT FILE STORE")
LOG_CHAT_ID = int(env("LOG_CHAT_ID", "0") or 0)
GMAIL_USER = env("GMAIL_USER")
GMAIL_PASS = env("GMAIL_APP_PASSWORD").replace(" ", "")
MAIL_FOLDER = env("MAIL_FOLDER", "INBOX")
SENDERS = [x.strip() for x in env("FAMPAY_SENDERS", "famapp,fampay").split(",") if x.strip()]
_cw = os.environ.get("CREDIT_WORDS")
CREDIT_WORDS = [x.strip().lower() for x in (_cw if _cw is not None else "received,credited,paid you,sent you,money in").split(",") if x.strip()]
POLL_SECONDS = max(8, int(env("POLL_SECONDS", "15") or 15))
BACKUP_SECONDS = max(30, int(env("BACKUP_SECONDS", "90") or 90))
USE_COLORS = env("BUTTON_COLORS", "1") != "0"
PORT = int(env("PORT", "10000") or 10000)
DATA_FILE = env("DATA_FILE", "data.json")
GH_TOKEN, GH_REPO = env("GITHUB_TOKEN"), env("GITHUB_REPO")
GH_BRANCH, GH_PATH = env("GITHUB_BRANCH", "main"), env("GITHUB_PATH", "data/data.json")

db = Store(DATA_FILE)
gh = GitHub(GH_TOKEN, GH_REPO, GH_BRANCH, GH_PATH) if (GH_TOKEN and GH_REPO) else None
BK = {"last_ok": 0, "rev": -1, "err": "", "snap": ""}
MAILST = {"err": "", "notified": 0, "last_poll": 0, "last_ok": 0}
BOT = None
TASKS = []
POLL_LOCK = None
ERR_SEEN = {}


# ============================ HELPERS ============================
def is_owner(uid):
    return uid == OWNER_ID


def is_admin(uid):
    return uid == OWNER_ID or uid in db.d["admins"]


def cfg():
    return db.d["settings"]


def upi_id():
    return cfg()["upi_id"] or env("UPI_ID")


def payee():
    return cfg()["payee"] or env("PAYEE_NAME") or "ZAD OSINT"


def mail_ok():
    return bool(GMAIL_USER and GMAIL_PASS)


def pay_ready():
    return bool(upi_id() and mail_ok())


def B(text, data=None, url=None, style=None):
    kw = {}
    if style and USE_COLORS:
        kw["api_kwargs"] = {"style": style}
    if url:
        return InlineKeyboardButton(text, url=url, **kw)
    return InlineKeyboardButton(text, callback_data=data, **kw)


def KB(*rows):
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def back(to="home", label=None):
    return B("%s %s" % (S["back"], label or "BACK"), to)


def page(items, p, size=6):
    total = max(1, (len(items) + size - 1) // size)
    p = max(0, min(int(p), total - 1))
    return items[p * size:(p + 1) * size], p, total


def nav(prefix, p, total):
    row = []
    if p > 0:
        row.append(B("\u25C0", "%s:%d" % (prefix, p - 1)))
    if total > 1:
        row.append(B("%d/%d" % (p + 1, total), "noop"))
    if p < total - 1:
        row.append(B("\u25B6", "%s:%d" % (prefix, p + 1)))
    return row if total > 1 else []


async def render(q, text, kb=None):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await q.message.delete()
        except Exception:
            pass
        await q.get_bot().send_message(q.message.chat_id, text, parse_mode=HTML, reply_markup=kb,
                                       disable_web_page_preview=True)


async def say(chat_id, text, kb=None):
    try:
        return await BOT.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb,
                                      disable_web_page_preview=True)
    except Forbidden:
        u = db.user(chat_id, create=False)
        if u and not u.get("blocked"):
            u["blocked"] = True
            db.touch()
    except TelegramError as e:
        log.warning("send fail %s: %s", chat_id, e)
    return None


async def notify_staff(text):
    for cid in {OWNER_ID, LOG_CHAT_ID}:
        if cid:
            await say(cid, text)


def head(title):
    return "%s <b>%s</b>\n%s\n" % (S["diamond"], sc(title), S["div"])


# ============================ HOME ============================
def home_text(uid):
    u = db.user(uid)
    role = ""
    if is_owner(uid):
        role = "  %s %s" % (S["crown"], sc("Owner"))
    elif is_admin(uid):
        role = "  %s %s" % (S["shield"], sc("Admin"))
    w = cfg()["welcome"]
    lines = [
        "%s <b>%s</b>%s" % (S["spark"], esc(BRAND), role),
        S["div"],
        "%s %s: <code>%s</code>" % (S["key"], sc("User ID"), uid),
        "%s %s: <b>%d</b> %s" % (S["wallet"], sc("Balance"), u["bal"], sc("Coins")),
        "%s %s: <b>%d</b> (%s %s)" % (S["folder"], sc("Files Purchased"), len(u["buys"]), money(u["spent"]), sc("spent")),
        "%s %s: %s" % (S["clock"], sc("Joined"), dt(u["joined"])),
        S["div"],
    ]
    if w:
        lines.append(esc(w))
    else:
        lines.append("%s %s" % (S["star"], sc("Browse & unlock premium files using coins")))
        lines.append("%s %s" % (S["gift"], sc("Redeem a gift code or deposit to add coins")))
    lines.append("%s %s" % (S["tri"], sc("Choose an option below")))
    return "\n".join(lines)


def home_kb(uid):
    rows = [
        [B("%s BUY FILE" % S["star"], "shop", style="primary"), B("%s WALLET" % S["wallet"], "wal", style="primary")],
        [B("%s REDEEM CODE" % S["gift"], "red", style="primary"), B("%s BUY COINS" % S["plus"], "dep", style="primary")],
        [B("%s TRANSFER COIN" % S["arrow"], "tr", style="primary"), B("%s LEADERBOARD" % S["trophy"], "lb", style="primary")],
        [B("%s HISTORY" % S["plan"], "his:0", style="success"), B("%s SUPPORT" % S["chat"], "sup", style="success")],
        [B("%s SEARCH" % S["eye"], "srch", style="primary"), B("%s MY PURCHASES" % S["download"], "mp:0", style="danger")],
    ]
    btns = [B(b["label"], url=b["url"]) for b in db.d["buttons"]]
    for i in range(0, len(btns), 2):
        rows.append(btns[i:i + 2])
    if is_admin(uid):
        rows.append([B("%s ADMIN PANEL" % S["settings"], "adm", style="primary")])
    return KB(*rows)


async def show_home(q=None, chat_id=None, uid=None):
    if q:
        await render(q, home_text(q.from_user.id), home_kb(q.from_user.id))
    else:
        await say(chat_id, home_text(uid), home_kb(uid))


# ============================ GATE ============================
def touch_user(tg):
    return db.user(tg.id, name=(tg.full_name or "User")[:40], username=tg.username or "")


async def gate(update):
    """Returns user dict or None (and replies) if the person cannot use the bot now."""
    tg = update.effective_user
    if not tg or update.effective_chat.type != "private":
        return None
    u = touch_user(tg)
    if u.get("blocked"):
        u["blocked"] = False
        db.touch()
    if u["banned"] and not is_admin(tg.id):
        await say(tg.id, "%s %s" % (S["ban"], sc("You are banned from this bot.")))
        return None
    if cfg()["maintenance"] and not is_admin(tg.id):
        await say(tg.id, "%s %s" % (S["warn"], sc("Bot is under maintenance. Please try again later.")))
        return None
    if db.d["channels"] and not is_admin(tg.id) and now() - JOIN_CACHE.get(tg.id, 0) > 600:
        miss = await join_missing(tg.id)
        if miss:
            rows = [[B("%s JOIN %s" % (S["plus"], c["title"]), url=c["link"], style="primary")] for c in miss]
            rows.append([B("%s I HAVE JOINED" % S["ok"], "fj", style="success")])
            await say(tg.id, head("Join Required") + sc("Please join the channel(s) below to use this bot, then tap I HAVE JOINED."), KB(*rows))
            return None
        JOIN_CACHE[tg.id] = now()
    return u


# ============================ SHOP ============================
def categories():
    return sorted({p["cat"] for p in db.d["products"].values() if p["active"]}, key=str.lower)


def cat_products(cat):
    return sorted([p for p in db.d["products"].values() if p["active"] and p["cat"] == cat], key=lambda x: x["id"])


async def h_shop(q, ctx, a, u):
    cats = categories()
    if not cats:
        return await render(q, head("Buy File") + sc("No files available yet. Please check back soon."), KB([back()]))
    if len(cats) == 1:
        return await h_cat(q, ctx, ["cat", "0", "0"], u)
    rows = [[B("%s %s (%d)" % (S["folder"], c, len(cat_products(c))), "cat:%d:0" % i, style="primary")] for i, c in enumerate(cats)]
    rows.append([B("%s SEARCH" % S["eye"], "srch", style="primary")])
    rows.append([back()])
    await render(q, head("Choose Category") + sc("Select a category to browse files."), KB(*rows))


async def h_cat(q, ctx, a, u):
    cats = categories()
    i = int(a[1]) if len(a) > 1 else 0
    if i >= len(cats):
        return await h_shop(q, ctx, a, u)
    items, p, total = page(cat_products(cats[i]), a[2] if len(a) > 2 else 0)
    rows = [[B("%s  %s \u2022 %s%s" % (S["star"], x["title"][:30], money(x["price"]), "" if db.in_stock(x) else " - SOLD OUT"), "p:%d" % x["id"])] for x in items]
    n = nav("cat:%d" % i, p, total)
    if n:
        rows.append(n)
    rows.append([B("%s SEARCH" % S["eye"], "srch", style="primary")])
    rows.append([back("shop" if len(cats) > 1 else "home")])
    await render(q, head(cats[i]) + sc("Tap a file to view details."), KB(*rows))


def eff_price(ctx, uid, p):
    """Price after an applied coupon. Returns (price, coupon_code or None)."""
    d = ctx.user_data.get("disc")
    if d and d.get("pid") == p["id"]:
        c = db.coupon_ok(d["code"], uid)
        if c:
            return max(1, p["price"] * (100 - c["pct"]) // 100), d["code"]
    return p["price"], None


async def h_prod(q, ctx, a, u):
    p = db.product(a[1])
    if not p or not p["active"]:
        return await render(q, sc("This file is not available."), KB([back("shop")]))
    uid = q.from_user.id
    owned = db.owns(uid, p["id"])
    price, code = eff_price(ctx, uid, p)
    pl = money(price) if not code else "<s>%s</s> %s" % (money(p["price"]), money(price))
    stock = p.get("stock", -1)
    txt = "%s<b>%s</b>\n\n%s\n\n%s %s: <b>%s</b>\n%s %s: %d" % (
        head("File Details"), esc(p["title"]), esc(p["desc"] or "-"),
        S["wallet"], sc("Price"), pl, S["chart_up"], sc("Sold"), p["sold"])
    if code:
        txt += "\n%s %s %s" % (S["ticket"], sc("Coupon applied:"), code)
    if stock >= 0:
        txt += "\n%s %s: %s" % (S["lock"], sc("Stock"), stock if stock else "SOLD OUT")
    rows = []
    if owned:
        txt += "\n\n%s %s" % (S["ok"], sc("You already own this file."))
        rows.append([B("%s DOWNLOAD AGAIN" % S["download"], "dl:%d" % p["id"], style="success")])
    elif stock != 0:
        rows.append([B("%s BUY WITH COINS" % S["wallet"], "buy:%d" % p["id"], style="primary")])
        rows.append([B("%s PAY VIA UPI (%s)" % (S["bolt"], money(price)), "upi:%d" % p["id"], style="success")])
        rows.append([B("%s HAVE A DISCOUNT CODE?" % S["ticket"], "dc:%d" % p["id"])])
    rows.append([back("shop")])
    await render(q, txt, KB(*rows))


async def h_buy(q, ctx, a, u):
    p = db.product(a[1])
    uid = q.from_user.id
    if not p or not p["active"]:
        return await render(q, sc("This file is not available."), KB([back("shop")]))
    if db.owns(uid, p["id"]):
        return await h_prod(q, ctx, ["p", a[1]], u)
    if not db.in_stock(p):
        return await q.answer("Sold out", show_alert=True)
    price, code = eff_price(ctx, uid, p)
    if u["bal"] < price:
        need = price - u["bal"]
        txt = "%s%s %s: <b>%d</b>\n%s %s: <b>%d</b>\n\n%s" % (
            head("Not Enough Coins"), S["wallet"], sc("Your balance"), u["bal"], S["minus"], sc("Short by"), need,
            sc("Deposit coins or pay directly via UPI."))
        return await render(q, txt, KB(
            [B("%s BUY COINS" % S["plus"], "dep", style="primary")],
            [B("%s PAY VIA UPI" % S["bolt"], "upi:%d" % p["id"], style="success")],
            [back("p:%d" % p["id"])]))
    txt = "%s%s\n%s %s: <b>%s</b>\n%s %s: %d" % (head("Confirm Purchase"), esc(p["title"]), S["wallet"], sc("Price"),
                                                  money(price), S["key"], sc("Balance after"), u["bal"] - price)
    await render(q, txt, KB([B("%s CONFIRM" % S["ok"], "buyy:%d" % p["id"], style="success"),
                             B("%s CANCEL" % S["no"], "p:%d" % p["id"], style="danger")]))


async def h_buyy(q, ctx, a, u):
    uid = q.from_user.id
    p = db.product(a[1])
    if not p or not p["active"]:
        return await render(q, sc("This file is not available."), KB([back("shop")]))
    if db.owns(uid, p["id"]):
        return await h_prod(q, ctx, ["p", a[1]], u)
    if not db.in_stock(p):
        return await q.answer("Sold out", show_alert=True)
    price, code = eff_price(ctx, uid, p)
    if not db.debit(uid, price, "Bought: " + p["title"][:30]):
        return await h_buy(q, ctx, a, u)
    db.grant(uid, p["id"], "wallet", paid=price)
    if code:
        db.use_coupon(code, uid)
        ctx.user_data.pop("disc", None)
    await render(q, "%s %s\n%s %s: <b>%d</b>" % (S["ok"], sc("Purchase successful! Sending your file..."), S["wallet"], sc("Balance"), db.user(uid)["bal"]),
                 KB([back("home", "HOME")]))
    await deliver(uid, p)
    await notify_staff("%s <b>%s</b>\n%s <code>%d</code> (%s) bought <b>%s</b> for %s via wallet" % (
        S["bolt"], sc("New Sale"), S["user"], uid, esc(u["name"]), esc(p["title"]), money(price)))


async def h_dl(q, ctx, a, u):
    p = db.product(a[1])
    if not p or not db.owns(q.from_user.id, p["id"]):
        return await q.answer("Not allowed", show_alert=True)
    await deliver(q.from_user.id, p)


async def deliver(chat_id, p):
    protect = cfg()["protect"]
    failed = 0
    for it in p["items"]:
        for attempt in range(2):
            try:
                t, f, c = it["t"], it["f"], (it.get("c") or None)
                if t == "text":
                    await BOT.send_message(chat_id, f, protect_content=protect)
                elif t == "document":
                    await BOT.send_document(chat_id, f, caption=c, protect_content=protect)
                elif t == "photo":
                    await BOT.send_photo(chat_id, f, caption=c, protect_content=protect)
                elif t == "video":
                    await BOT.send_video(chat_id, f, caption=c, protect_content=protect)
                elif t == "audio":
                    await BOT.send_audio(chat_id, f, caption=c, protect_content=protect)
                elif t == "voice":
                    await BOT.send_voice(chat_id, f, caption=c, protect_content=protect)
                elif t == "animation":
                    await BOT.send_animation(chat_id, f, caption=c, protect_content=protect)
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError as e:
                log.warning("deliver fail: %s", e)
                failed += 1
                break
        await asyncio.sleep(0.35)
    if failed:
        await say(chat_id, "%s %s" % (S["warn"], sc("Some files could not be sent. Contact support, you will not be charged twice.")))
        await notify_staff("%s Delivery problem for product <b>%s</b> (user <code>%s</code>) - %d item(s) failed. Re-upload the file." % (
            S["warn"], esc(p["title"]), chat_id, failed))
    else:
        await say(chat_id, "%s %s" % (S["ok"], sc("Delivered! Find it anytime in My Purchases.")), KB([back("home", "HOME")]))


# ============================ WALLET ============================
async def h_wal(q, ctx, a, u):
    txt = "%s%s %s: <b>%d</b>\n%s %s: %s\n%s %s: %s" % (
        head("My Wallet"), S["wallet"], sc("Balance"), u["bal"], S["plus"], sc("Total deposited"), money(u["deposited"]),
        S["minus"], sc("Total spent"), money(u["spent"]))
    txt += "\n\n%s %s" % (S["tri"], sc("1 Coin = 1 Rupee"))
    await render(q, txt, KB(
        [B("%s BUY COINS" % S["plus"], "dep", style="primary"), B("%s REDEEM CODE" % S["gift"], "red", style="primary")],
        [B("%s TRANSFER" % S["arrow"], "tr", style="primary"), B("%s HISTORY" % S["plan"], "his:0", style="success")],
        [back()]))


async def h_his(q, ctx, a, u):
    hist = list(reversed(u["hist"]))
    items, p, total = page(hist, a[1] if len(a) > 1 else 0, 8)
    if not hist:
        body = sc("No transactions yet.")
    else:
        body = "\n".join("%s <b>%s%d</b> \u2022 %s\n    <i>%s</i>" % (
            S["ok"] if h[1] >= 0 else S["minus"], "+" if h[1] >= 0 else "", h[1], esc(h[2]), dt(h[0], True)) for h in items)
    rows = []
    n = nav("his", p, total)
    if n:
        rows.append(n)
    rows.append([back("wal")])
    await render(q, head("Wallet History") + body, KB(*rows))


async def h_mp(q, ctx, a, u):
    buys = list(reversed(u["buys"]))
    items, p, total = page(buys, a[1] if len(a) > 1 else 0, 6)
    rows = []
    for b in items:
        if db.product(b["pid"]):
            rows.append([B("%s %s" % (S["download"], b["title"][:34]), "dl:%d" % b["pid"], style="success")])
    n = nav("mp", p, total)
    if n:
        rows.append(n)
    rows.append([back()])
    body = sc("Tap a file to download it again.") if buys else sc("You have not purchased anything yet.")
    await render(q, head("My Purchases") + body, KB(*rows))


async def h_lb(q, ctx, a, u):
    top = sorted([x for x in db.d["users"].values() if x["spent"] > 0], key=lambda x: -x["spent"])[:10]
    if not top:
        body = sc("No purchases yet. Be the first!")
    else:
        medals = ["1.", "2.", "3."]
        body = "\n".join("%s <b>%s</b> \u2022 %s" % (medals[i] if i < 3 else "%d." % (i + 1), esc(x["name"][:14]), money(x["spent"])) for i, x in enumerate(top))
    await render(q, head("Leaderboard") + body, KB([back()]))


async def h_sup(q, ctx, a, u):
    s = cfg()["support"].lstrip("@")
    rows = []
    if s:
        rows.append([B("%s CONTACT SUPPORT" % S["chat"], url="https://t.me/" + s, style="success")])
    rows.append([back()])
    await render(q, head("Support") + (sc("Need help? Contact support below.") if s else sc("Support contact is not set yet.")), KB(*rows))


# ---------- redeem ----------
async def h_red(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "redeem", "d": {}}
    await render(q, head("Redeem Gift Code") + sc("Send your gift code now."), KB([back("home", "CANCEL")]))


async def st_redeem(msg, ctx, st, text):
    uid = msg.from_user.id
    ok, r = db.redeem(uid, text)
    ctx.user_data.pop("st", None)
    if ok:
        await say(uid, "%s %s <b>+%d</b> %s\n%s: <b>%d</b>" % (S["ok"], sc("Code redeemed!"), r, sc("coins"), sc("Balance"), db.user(uid)["bal"]),
                  KB([back("home", "HOME")]))
    else:
        m = {"invalid": "Invalid code.", "used": "You already used this code.", "full": "This code has reached its usage limit."}[r]
        await say(uid, "%s %s" % (S["no"], sc(m)), KB([B("%s TRY AGAIN" % S["refresh"], "red"), back("home", "HOME")]))


# ---------- transfer ----------
async def h_tr(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "tr_to", "d": {}}
    await render(q, head("Transfer Coins") + sc("Send the User ID of the receiver."), KB([back("home", "CANCEL")]))


async def st_tr_to(msg, ctx, st, text):
    uid = msg.from_user.id
    if not text.strip().isdigit():
        return await say(uid, "%s %s" % (S["no"], sc("Send a numeric User ID.")))
    to = int(text.strip())
    t = db.user(to, create=False)
    if to == uid or not t:
        return await say(uid, "%s %s" % (S["no"], sc("User not found (they must have started the bot) or invalid ID.")))
    st["n"], st["d"] = "tr_amt", {"to": to}
    await say(uid, "%s %s <b>%s</b>\n%s" % (S["ok"], sc("Receiver:"), esc(t["name"]), sc("Now send the amount of coins.")))


async def st_tr_amt(msg, ctx, st, text):
    uid = msg.from_user.id
    u = db.user(uid)
    if not text.strip().isdigit() or int(text) < cfg()["min_transfer"]:
        return await say(uid, "%s %s %d" % (S["no"], sc("Send a valid amount. Minimum:"), cfg()["min_transfer"]))
    amt = int(text)
    if amt > u["bal"]:
        return await say(uid, "%s %s" % (S["no"], sc("Not enough balance.")))
    ctx.user_data["tr"] = {"to": st["d"]["to"], "amt": amt}
    ctx.user_data.pop("st", None)
    t = db.user(st["d"]["to"])
    await say(uid, "%s%s: <b>%s</b>\n%s: <b>%d</b>" % (head("Confirm Transfer"), sc("To"), esc(t["name"]), sc("Coins"), amt),
              KB([B("%s CONFIRM" % S["ok"], "trok", style="success"), B("%s CANCEL" % S["no"], "home", style="danger")]))


async def h_trok(q, ctx, a, u):
    tr = ctx.user_data.pop("tr", None)
    uid = q.from_user.id
    if not tr or not db.user(tr["to"], create=False):
        return await render(q, sc("Transfer expired. Start again."), KB([back()]))
    if not db.debit(uid, tr["amt"], "Transfer to %d" % tr["to"]):
        return await render(q, "%s %s" % (S["no"], sc("Not enough balance.")), KB([back()]))
    db.credit(tr["to"], tr["amt"], "Received from %d" % uid)
    await render(q, "%s %s <b>%d</b> %s" % (S["ok"], sc("Transferred"), tr["amt"], sc("coins.")), KB([back("home", "HOME")]))
    await say(tr["to"], "%s %s <b>+%d</b> %s <code>%d</code>" % (S["ok"], sc("You received"), tr["amt"], sc("coins from"), uid))


# ============================ PAYMENTS ============================
def make_qr(uri):
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=10, border=3)
        qr.add_data(uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio
    except Exception as e:
        log.warning("qrcode lib failed (%s), using fallback url", e)
        return "https://api.qrserver.com/v1/create-qr-code/?size=500x500&data=" + quote(uri, safe="")


async def start_upi(q, uid, kind, base, pid=None, coupon=None):
    if not pay_ready():
        return await say(uid, "%s %s" % (S["warn"], sc("Payments are not configured yet. Contact support.")), KB([back()]))
    # cancel previous pending order of this user
    for o in db.pending_orders():
        if o["uid"] == uid:
            await close_order(o, "cancelled")
    o = db.new_order(uid, kind, base, pid)
    if not o:
        return await say(uid, sc("Too many payments in progress. Try again in a minute."), KB([back()]))
    o["coupon"] = coupon
    uri = "upi://pay?pa=%s&pn=%s&am=%s&cu=INR&tn=%s" % (quote(upi_id()), quote(payee()), o["pay"], o["ref"])
    what = sc("Wallet deposit") if kind == "deposit" else esc(db.product(pid)["title"])
    cap = ("%s <b>%s</b>\n%s\n%s: <b>%s</b>\n"
           "%s <b>%s %s</b>\n%s %s: <code>%s</code>\n%s %s: <code>%s</code>\n%s %s: %d %s\n%s\n"
           "%s %s") % (
        S["bolt"], sc("Pay via UPI"), S["div"], sc("For"), what,
        S["wallet"], sc("Pay exactly"), money(o["pay"]), S["key"], sc("UPI ID"), esc(upi_id()),
        S["ticket"], sc("Order"), o["ref"], S["clock"], sc("Expires in"), cfg()["order_minutes"], sc("minutes"), S["div"],
        S["tri"], sc("Scan QR or use the UPI ID. Pay the EXACT amount (including paise). Verified automatically in a few seconds."))
    kb = KB([B("%s CHECK PAYMENT" % S["refresh"], "chk:%d" % o["id"], style="success")],
            [B("%s CANCEL" % S["no"], "cx:%d" % o["id"], style="danger")])
    try:
        await q.message.delete()
    except Exception:
        pass
    m = await BOT.send_photo(uid, make_qr(uri), caption=cap, parse_mode=HTML, reply_markup=kb)
    o["msg"] = [uid, m.message_id]
    db.touch()


async def close_order(o, status, text=None):
    if o["status"] != "pending":
        return
    o["status"] = status
    db.touch()
    if o.get("msg"):
        try:
            await BOT.delete_message(o["msg"][0], o["msg"][1])
        except Exception:
            pass
    if text:
        await say(o["uid"], text, KB([back("home", "HOME")]))


async def h_dep(q, ctx, a, u):
    if len(a) > 1:
        amt = int(a[1])
        if amt < cfg()["min_deposit"]:
            return await q.answer("Minimum deposit is %d" % cfg()["min_deposit"], show_alert=True)
        return await start_upi(q, q.from_user.id, "deposit", amt)
    ctx.user_data.pop("st", None)
    rows = [[B(money(x), "dep:%d" % x, style="primary") for x in (50, 100)], [B(money(x), "dep:%d" % x, style="primary") for x in (200, 500)],
            [B("%s CUSTOM AMOUNT" % S["spark"], "depc", style="success")], [back()]]
    await render(q, head("Buy Coins") + "%s %s\n%s %s %d" % (S["tri"], sc("1 Coin = 1 Rupee. Pay by UPI QR."), S["tri"], sc("Minimum:"), cfg()["min_deposit"]) + bonus_line(), KB(*rows))


async def h_depc(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "dep_custom", "d": {}}
    await render(q, head("Custom Amount") + sc("Send the amount in rupees (numbers only)."), KB([back("dep", "CANCEL")]))


async def st_dep_custom(msg, ctx, st, text):
    uid = msg.from_user.id
    t = text.strip()
    if not t.isdigit() or not (cfg()["min_deposit"] <= int(t) <= 50000):
        return await say(uid, "%s %s %d - 50000" % (S["no"], sc("Send a number between"), cfg()["min_deposit"]))
    ctx.user_data.pop("st", None)
    amt = int(t)

    class _Q:  # tiny shim so start_upi can reuse the same code path
        class message:
            @staticmethod
            async def delete():
                return None
    await start_upi(_Q, uid, "deposit", amt)


async def h_upi(q, ctx, a, u):
    p = db.product(a[1])
    uid = q.from_user.id
    if not p or not p["active"]:
        return await render(q, sc("This file is not available."), KB([back("shop")]))
    if db.owns(uid, p["id"]):
        return await h_prod(q, ctx, ["p", a[1]], u)
    if not db.in_stock(p):
        return await q.answer("Sold out", show_alert=True)
    price, code = eff_price(ctx, uid, p)
    await start_upi(q, uid, "product", price, p["id"], code)


async def h_cx(q, ctx, a, u):
    o = db.d["orders"].get(a[1])
    if o and o["uid"] == q.from_user.id and o["status"] == "pending":
        await close_order(o, "cancelled")
    await show_home(chat_id=q.from_user.id, uid=q.from_user.id)


async def h_chk(q, ctx, a, u):
    o = db.d["orders"].get(a[1])
    if not o or o["uid"] != q.from_user.id:
        return await q.answer("Order not found", show_alert=True)
    if o["status"] == "paid":
        return await q.answer("Already paid \u2713", show_alert=True)
    if o["status"] != "pending":
        return await q.answer("Order is %s" % o["status"], show_alert=True)
    if now() - o.get("last_chk", 0) < 8:
        return await q.answer("Please wait a few seconds...", show_alert=False)
    o["last_chk"] = now()
    await poll_once()
    if o["status"] == "pending":
        await q.answer("Payment not received yet. Pay the exact amount and try again in 20-30 seconds.", show_alert=True)
    else:
        await q.answer("Payment received \u2713", show_alert=False)


async def finalize(o, mail=None, by="auto"):
    if o["status"] != "pending":
        return False
    o["status"], o["paid"], o["by"] = "paid", now(), by
    if mail:
        seen = db.d["seen_mails"]
        seen.append(mail["mid"])
        if len(seen) > 800:
            del seen[:-600]
    uid = int(o["uid"])
    st = db.d["stats"]
    st["cash"] += o["base"]
    st["paid_orders"] += 1
    u = db.user(uid)
    p = db.product(o["pid"]) if o["kind"] == "product" else None
    sold, bonus = False, 0
    if o["kind"] == "product" and p and not db.owns(uid, p["id"]) and db.in_stock(p):
        db.grant(uid, p["id"], "upi", paid=o["base"])
        if o.get("coupon"):
            db.use_coupon(o["coupon"], uid)
        db.log(uid, 0, "Bought via UPI: " + p["title"][:30])
        sold = True
    else:
        # deposit, or product vanished / already owned -> credit wallet so money is never lost
        u["deposited"] += o["base"]
        st["deposits"] += o["base"]
        db.credit(uid, o["base"], "Deposit via UPI (%s)" % o["ref"])
        c = cfg()
        if o["kind"] == "deposit" and c.get("bonus_pct", 0) > 0 and o["base"] >= c.get("bonus_min", 0):
            bonus = o["base"] * c["bonus_pct"] // 100
            if bonus > 0:
                db.credit(uid, bonus, "Deposit bonus %d%%" % c["bonus_pct"])
    db.touch()
    if o.get("msg"):
        try:
            await BOT.delete_message(o["msg"][0], o["msg"][1])
        except Exception:
            pass
    if sold:
        await say(uid, "%s %s <b>%s</b>\n%s" % (S["ok"], sc("Payment received"), money(o["pay"]), sc("Sending your file...")))
        await deliver(uid, p)
    else:
        await say(uid, "%s %s <b>%s</b>\n%s %s: <b>%d</b>" % (S["ok"], sc("Payment received"), money(o["pay"]), S["wallet"], sc("New balance"), db.user(uid)["bal"]) + (("\n%s %s +%d" % (S["gift"], sc("Bonus"), bonus)) if bonus else ""),
                  KB([B("%s BUY FILE" % S["star"], "shop", style="primary"), back("home", "HOME")]))
    await notify_staff("%s <b>%s</b> (%s)\n%s <code>%d</code> %s \u2022 %s \u2022 %s" % (
        S["bolt"], sc("Payment received"), by, S["user"], uid, esc(u["name"]), money(o["pay"]),
        esc(p["title"]) if p else "Deposit"))
    return True


def _poll_blocking():
    return mailpay.fetch_mails(GMAIL_USER, GMAIL_PASS, SENDERS, MAIL_FOLDER, 2, 40, CREDIT_WORDS, ())


async def poll_once():
    if not mail_ok():
        return
    async with POLL_LOCK:
        if not db.pending_orders():
            return
        try:
            mails = await asyncio.to_thread(_poll_blocking)
            MAILST["err"], MAILST["last_ok"] = "", now()
        except Exception as e:
            err = ("%s: %s" % (type(e).__name__, e))[:300]
            MAILST["err"] = err
            log.warning("mail poll failed: %s", err)
            if now() - MAILST["notified"] > 3600:
                MAILST["notified"] = now()
                await say(OWNER_ID, "%s <b>%s</b>\n<code>%s</code>\n%s" % (
                    S["warn"], sc("Gmail check failed"), esc(err),
                    sc("Check GMAIL_USER, App Password and that IMAP is enabled. Payments can be approved manually in Admin > Orders.")))
            return
        MAILST["last_poll"] = now()
        for o, m in mailpay.match(mails, db.pending_orders(), db.d["seen_mails"]):
            try:
                await finalize(o, m, "auto")
            except Exception:
                log.exception("finalize failed")


async def payments_loop():
    while True:
        try:
            for o in list(db.pending_orders()):
                if now() > o["expires"] + 60:
                    await close_order(o, "expired", "%s %s %s" % (S["warn"], sc("Payment order expired:"), money(o["pay"])))
            if db.pending_orders():
                await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("payments loop")
        await asyncio.sleep(POLL_SECONDS)


# ============================ BACKUP / FLUSH ============================
def _push(rev, text):
    if not gh:
        return
    gh.put(text, "backup rev %d" % rev)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if BK["snap"] != today:
        folder = os.path.dirname(GH_PATH) or "data"
        gh.put(text, "daily snapshot " + today, "%s/snapshots/%s.json" % (folder, today))
        BK["snap"] = today
    BK["rev"], BK["last_ok"], BK["err"] = rev, now(), ""


async def backup_now(force=False):
    if not gh:
        return "GitHub backup is not configured"
    rev = db.d["_rev"]
    if not force and rev == BK["rev"]:
        return "No changes"
    text = db.dump()
    try:
        await asyncio.to_thread(_push, rev, text)
        return "OK"
    except Exception as e:
        BK["err"] = str(e)[:200]
        log.warning("backup failed: %s", e)
        return "FAILED: " + BK["err"]


async def flush_loop():
    n = 0
    while True:
        try:
            await asyncio.sleep(5)
            n += 5
            if db.dirty:
                db.flush_local()
            if n >= BACKUP_SECONDS:
                n = 0
                res = await backup_now()
                if res.startswith("FAILED") and now() - BK.get("notified", 0) > 3600:
                    BK["notified"] = now()
                    await say(OWNER_ID, "%s <b>%s</b>\n<code>%s</code>" % (S["warn"], sc("GitHub backup failed"), esc(res)))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("flush loop")


def ping_loop():
    url = env("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url.rstrip("/") + "/health", timeout=20).read()
        except Exception:
            pass


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def start_health():
    srv = HTTPServer(("0.0.0.0", PORT), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=ping_loop, daemon=True).start()
    log.info("health server on %d", PORT)


# ============================ ADMIN: PANEL ============================
BG = set()


def spawn(coro):
    t = asyncio.get_running_loop().create_task(coro)
    BG.add(t)
    t.add_done_callback(BG.discard)
    return t


def extract_item(msg):
    cap = msg.caption or ""
    if msg.document:
        return {"t": "document", "f": msg.document.file_id, "c": cap}
    if msg.photo:
        return {"t": "photo", "f": msg.photo[-1].file_id, "c": cap}
    if msg.video:
        return {"t": "video", "f": msg.video.file_id, "c": cap}
    if msg.audio:
        return {"t": "audio", "f": msg.audio.file_id, "c": cap}
    if msg.voice:
        return {"t": "voice", "f": msg.voice.file_id, "c": cap}
    if msg.animation:
        return {"t": "animation", "f": msg.animation.file_id, "c": cap}
    if msg.text:
        return {"t": "text", "f": msg.text, "c": ""}
    return None


async def h_home(q, ctx, a, u):
    await show_home(q)


async def h_adm(q, ctx, a, u):
    uid = q.from_user.id
    rows = [
        [B("%s ADD FILE" % S["plus"], "ap:new", style="success"), B("%s MANAGE FILES" % S["folder"], "apl:0", style="primary")],
        [B("%s GIFT CODES" % S["gift"], "gc", style="primary"), B("%s USERS" % S["users"], "us", style="primary")],
        [B("%s ORDERS" % S["ticket"], "or:0", style="primary"), B("%s BROADCAST" % S["broadcast"], "bc", style="primary")],
        [B("%s STATS" % S["graph"], "sts", style="primary"), B("%s SETTINGS" % S["settings"], "set", style="primary")],
        [B("%s COUPONS" % S["ticket"], "cp", style="primary"), B("%s BACKUP" % S["cloud"], "bk", style="primary")],
    ]
    if is_owner(uid):
        rows.append([B("%s ADMINS" % S["shield"], "ad", style="danger"), B("%s HOME BUTTONS" % S["spark"], "bt", style="danger")])
    rows.append([back()])
    await render(q, head("Admin Panel") + "%s %s" % (S["crown"], sc("Full control: files, coins, codes, users, payments.")), KB(*rows))


# ---------- add / edit product ----------
async def h_ap(q, ctx, a, u):
    uid = q.from_user.id
    sub = a[1] if len(a) > 1 else ""
    st = ctx.user_data.get("st")
    if sub == "new":
        ctx.user_data["st"] = {"n": "ap_title", "d": {}}
        return await render(q, head("Add File / Project") + sc("Step 1/5: Send the TITLE."), KB([back("adm", "CANCEL")]))
    if not st or not st["n"].startswith("ap_"):
        return await render(q, sc("Session expired. Start again."), KB([back("adm")]))
    d = st["d"]
    if sub == "cg":
        d["cat"] = "General"
        return await ask_desc(q, st)
    if sub == "skip":
        d["desc"] = ""
        return await ask_files(q, st)
    if sub == "done":
        if not d.get("items"):
            return await q.answer("Send at least one file or text first", show_alert=True)
        pid = db.add_product(d["title"], d["price"], d.get("desc", ""), d.get("cat", "General"), d["items"])
        ctx.user_data.pop("st", None)
        t, kb = prod_view(pid)
        await render(q, "%s %s\n\n%s" % (S["ok"], sc("File added!"), t), kb)


async def ask_desc(q_or_chat, st):
    st["n"] = "ap_desc"
    txt, kb = head("Add File") + sc("Step 4/5: Send a DESCRIPTION."), KB([B("%s SKIP" % S["fwd"], "ap:skip")], [back("adm", "CANCEL")])
    if hasattr(q_or_chat, "edit_message_text"):
        await render(q_or_chat, txt, kb)
    else:
        await say(q_or_chat, txt, kb)


async def ask_files(q_or_chat, st):
    st["n"] = "ap_files"
    st["d"]["items"] = []
    txt = head("Add File") + sc("Step 5/5: Now send the FILE(S) - document, photo, video, audio, or a text/link. Send many, then tap DONE.")
    kb = KB([B("%s DONE" % S["ok"], "ap:done", style="success")], [back("adm", "CANCEL")])
    if hasattr(q_or_chat, "edit_message_text"):
        await render(q_or_chat, txt, kb)
    else:
        await say(q_or_chat, txt, kb)


async def st_ap_title(msg, ctx, st, text):
    st["d"]["title"] = text.strip()[:80]
    st["n"] = "ap_price"
    await say(msg.chat_id, head("Add File") + sc("Step 2/5: Send the PRICE in rupees (number)."))


async def st_ap_price(msg, ctx, st, text):
    if not text.strip().isdigit() or int(text) < 1:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number like 99.")))
    st["d"]["price"] = int(text)
    st["n"] = "ap_cat"
    await say(msg.chat_id, head("Add File") + sc("Step 3/5: Send a CATEGORY name or tap General."), KB([B("General", "ap:cg")]))


async def st_ap_cat(msg, ctx, st, text):
    st["d"]["cat"] = text.strip()[:30] or "General"
    await ask_desc(msg.chat_id, st)


async def st_ap_desc(msg, ctx, st, text):
    st["d"]["desc"] = text.strip()[:700]
    await ask_files(msg.chat_id, st)


async def st_ap_files(msg, ctx, st, text):
    it = extract_item(msg)
    if not it:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Unsupported message type.")))
    st["d"]["items"].append(it)
    await say(msg.chat_id, "%s %s (%d)" % (S["ok"], sc("Added. Send more or tap DONE."), len(st["d"]["items"])),
              KB([B("%s DONE" % S["ok"], "ap:done", style="success")]))


def prod_view(pid):
    p = db.product(pid)
    txt = "%s<b>%s</b>\n%s %s: %s\n%s %s: %s\n%s %s: %d\n%s %s: %d\n%s %s: %s\n\n%s" % (
        head("File #%d" % p["id"]), esc(p["title"]), S["wallet"], sc("Price"), money(p["price"]), S["folder"], sc("Category"), esc(p["cat"]),
        S["download"], sc("Items"), len(p["items"]), S["chart_up"], sc("Sold"), p["sold"], S["rec"], sc("Status"),
        sc("Active") if p["active"] else sc("Hidden"), esc(p["desc"] or "-"))
    txt += "\n%s %s: %s" % (S["lock"], sc("Stock"), "unlimited" if p.get("stock", -1) < 0 else p["stock"])
    i = p["id"]
    kb = KB(
        [B("TITLE", "ape:%d:title" % i, style="primary"), B("PRICE", "ape:%d:price" % i, style="primary")],
        [B("DESCRIPTION", "ape:%d:desc" % i, style="primary"), B("CATEGORY", "ape:%d:cat" % i, style="primary")],
        [B("STOCK", "ape:%d:stock" % i, style="primary"), B("%s REPLACE FILES" % S["upload"], "ape:%d:files" % i, style="primary")],
        [B("%s TEST SEND" % S["download"], "aps:%d" % i, style="success"),
         B(("%s HIDE" if p["active"] else "%s SHOW") % S["eye"], "apt:%d" % i, style="success")],
        [B("%s DELETE" % S["trash"], "apx:%d" % i, style="danger")],
        [back("apl:0", "FILES")])
    return txt, kb


async def h_apl(q, ctx, a, u):
    prods = sorted(db.d["products"].values(), key=lambda x: -x["id"])
    items, p, total = page(prods, a[1] if len(a) > 1 else 0, 6)
    rows = [[B("#%d %s \u2022 %s%s" % (x["id"], x["title"][:24], money(x["price"]), "" if x["active"] else " (hidden)"), "apd:%d" % x["id"])] for x in items]
    n = nav("apl", p, total)
    if n:
        rows.append(n)
    rows.append([B("%s ADD FILE" % S["plus"], "ap:new", style="success")])
    rows.append([back("adm")])
    await render(q, head("Manage Files") + (sc("Select a file.") if prods else sc("No files yet.")), KB(*rows))


async def h_apd(q, ctx, a, u):
    if not db.product(a[1]):
        return await h_apl(q, ctx, ["apl", "0"], u)
    t, kb = prod_view(int(a[1]))
    await render(q, t, kb)


async def h_ape(q, ctx, a, u):
    pid, field = int(a[1]), a[2]
    if not db.product(pid):
        return await h_apl(q, ctx, ["apl", "0"], u)
    prompts = {"title": "Send the new TITLE.", "price": "Send the new PRICE (number).", "desc": "Send the new DESCRIPTION.",
               "cat": "Send the new CATEGORY.", "stock": "Send the STOCK number (how many can be sold, 0 = sold out) or - for unlimited.", "files": "Send the NEW file(s). Old files will be replaced. Tap DONE when finished."}
    ctx.user_data["st"] = {"n": "ed_" + field, "d": {"pid": pid, "items": []}}
    kb = [[B("%s DONE" % S["ok"], "edone:%d" % pid, style="success")]] if field == "files" else []
    await render(q, head("Edit File #%d" % pid) + sc(prompts[field]), KB(*kb, [back("apd:%d" % pid, "CANCEL")]))


async def h_edone(q, ctx, a, u):
    st = ctx.user_data.get("st")
    pid = int(a[1])
    p = db.product(pid)
    if not st or st["n"] != "ed_files" or not p:
        return await render(q, sc("Session expired."), KB([back("adm")]))
    if not st["d"]["items"]:
        return await q.answer("Send at least one file first", show_alert=True)
    p["items"] = st["d"]["items"]
    db.touch()
    ctx.user_data.pop("st", None)
    t, kb = prod_view(pid)
    await render(q, "%s %s\n\n%s" % (S["ok"], sc("Files replaced."), t), kb)


async def st_edit(msg, ctx, st, text):
    pid = st["d"]["pid"]
    p = db.product(pid)
    field = st["n"][3:]
    if not p:
        ctx.user_data.pop("st", None)
        return
    if field == "files":
        it = extract_item(msg)
        if not it:
            return await say(msg.chat_id, "%s %s" % (S["no"], sc("Unsupported message type.")))
        st["d"]["items"].append(it)
        return await say(msg.chat_id, "%s %s (%d)" % (S["ok"], sc("Added. Send more or tap DONE."), len(st["d"]["items"])),
                         KB([B("%s DONE" % S["ok"], "edone:%d" % pid, style="success")]))
    if field == "price":
        if not text.strip().isdigit() or int(text) < 1:
            return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number.")))
        p["price"] = int(text)
    elif field == "stock":
        t = text.strip()
        if t == "-":
            p["stock"] = -1
        elif t.isdigit():
            p["stock"] = int(t)
        else:
            return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number (0 = sold out) or - for unlimited.")))
    elif field == "title":
        p["title"] = text.strip()[:80]
    elif field == "desc":
        p["desc"] = text.strip()[:700]
    elif field == "cat":
        p["cat"] = text.strip()[:30] or "General"
    db.touch()
    ctx.user_data.pop("st", None)
    t, kb = prod_view(pid)
    await say(msg.chat_id, "%s %s\n\n%s" % (S["ok"], sc("Updated."), t), kb)


async def h_apt(q, ctx, a, u):
    p = db.product(a[1])
    if p:
        p["active"] = not p["active"]
        db.touch()
    await h_apd(q, ctx, a, u)


async def h_aps(q, ctx, a, u):
    p = db.product(a[1])
    if p:
        await deliver(q.from_user.id, p)


async def h_apx(q, ctx, a, u):
    p = db.product(a[1])
    if not p:
        return await h_apl(q, ctx, ["apl", "0"], u)
    await render(q, head("Delete File") + "%s <b>%s</b>\n%s" % (S["warn"], esc(p["title"]), sc("This cannot be undone. Users who bought it lose re-download access.")),
                 KB([B("%s YES DELETE" % S["trash"], "apxy:%s" % a[1], style="danger"), B("%s NO" % S["back"], "apd:%s" % a[1])]))


async def h_apxy(q, ctx, a, u):
    db.d["products"].pop(str(a[1]), None)
    db.touch()
    await h_apl(q, ctx, ["apl", "0"], u)


# ---------- gift codes ----------
async def h_gc(q, ctx, a, u):
    codes = sorted(db.d["gifts"].items(), key=lambda kv: -kv[1]["created"])
    items, p, total = page(codes, a[1] if len(a) > 1 else 0, 6)
    rows = [[B("%s %s \u2022 %dc \u2022 %d/%d" % (S["gift"], c, g["coins"], len(g["used"]), g["max"]), "gcv:" + c)] for c, g in items]
    n = nav("gc", p, total)
    if n:
        rows.append(n)
    rows.append([B("%s NEW CODE" % S["plus"], "gcn", style="success")])
    rows.append([back("adm")])
    await render(q, head("Gift Codes") + (sc("Tap a code for details.") if codes else sc("No codes yet.")), KB(*rows))


async def h_gcn(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "gc_coins", "d": {}}
    await render(q, head("New Gift Code") + sc("Step 1/3: How many COINS does this code give?"), KB([back("gc", "CANCEL")]))


async def st_gc_coins(msg, ctx, st, text):
    if not text.strip().isdigit() or int(text) < 1:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number.")))
    st["d"]["coins"], st["n"] = int(text), "gc_uses"
    await say(msg.chat_id, sc("Step 2/3: How many TOTAL USES (people who can redeem)?"))


async def st_gc_uses(msg, ctx, st, text):
    if not text.strip().isdigit() or int(text) < 1:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number.")))
    st["d"]["max"], st["n"] = int(text), "gc_code"
    await say(msg.chat_id, sc("Step 3/3: Send a custom CODE (A-Z, 0-9, - _ , 4-20 chars) or tap AUTO."), KB([B("%s AUTO" % S["spark"], "gca", style="success")]))


def make_gift(uid, d, code=None):
    code = (code or db.new_code()).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{4,20}", code) or code in db.d["gifts"]:
        return None
    db.d["gifts"][code] = {"coins": d["coins"], "max": d["max"], "used": [], "created": now(), "by": uid, "active": True}
    db.touch()
    return code


async def st_gc_code(msg, ctx, st, text):
    code = make_gift(msg.from_user.id, st["d"], text.strip())
    if not code:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Invalid or already exists. Try another or tap AUTO.")), KB([B("%s AUTO" % S["spark"], "gca")]))
    ctx.user_data.pop("st", None)
    await say(msg.chat_id, "%s %s\n<code>%s</code>\n%d %s \u2022 %d %s" % (S["ok"], sc("Gift code created:"), code, st["d"]["coins"], sc("coins"), st["d"]["max"], sc("uses")),
              KB([back("gc", "GIFT CODES")]))


async def h_gca(q, ctx, a, u):
    st = ctx.user_data.get("st")
    if not st or st["n"] != "gc_code":
        return await render(q, sc("Session expired."), KB([back("gc")]))
    code = make_gift(q.from_user.id, st["d"])
    ctx.user_data.pop("st", None)
    await render(q, "%s %s\n<code>%s</code>\n%d %s \u2022 %d %s" % (S["ok"], sc("Gift code created:"), code, st["d"]["coins"], sc("coins"), st["d"]["max"], sc("uses")),
                 KB([back("gc", "GIFT CODES")]))


async def h_gcv(q, ctx, a, u):
    g = db.d["gifts"].get(a[1])
    if not g:
        return await h_gc(q, ctx, ["gc", "0"], u)
    await render(q, "%s<code>%s</code>\n%s %s: %d\n%s %s: %d / %d\n%s %s: %s" % (
        head("Gift Code"), a[1], S["wallet"], sc("Coins"), g["coins"], S["users"], sc("Used"), len(g["used"]), g["max"], S["clock"], sc("Created"), dt(g["created"])),
        KB([B("%s DELETE" % S["trash"], "gcd:" + a[1], style="danger")], [back("gc")]))


async def h_gcd(q, ctx, a, u):
    db.d["gifts"].pop(a[1], None)
    db.touch()
    await h_gc(q, ctx, ["gc", "0"], u)


# ---------- users ----------
async def h_us(q, ctx, a, u):
    ctx.user_data.pop("st", None)
    await render(q, head("Users") + "%s %s: %d" % (S["users"], sc("Total"), len(db.d["users"])), KB(
        [B("%s SEARCH BY ID" % S["eye"], "uss", style="primary")],
        [B("%s RECENT USERS" % S["clock"], "usl:0", style="primary"), B("%s TOP BALANCES" % S["wallet"], "ust", style="primary")],
        [back("adm")]))


async def h_uss(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "us_search", "d": {}}
    await render(q, head("Search User") + sc("Send the numeric User ID."), KB([back("us", "CANCEL")]))


async def h_usl(q, ctx, a, u):
    us = sorted(db.d["users"].values(), key=lambda x: -x["joined"])
    items, p, total = page(us, a[1] if len(a) > 1 else 0, 8)
    rows = [[B("%s %s \u2022 %d" % (S["user"], x["name"][:18], x["bal"]), "usv:%d" % x["id"])] for x in items]
    n = nav("usl", p, total)
    if n:
        rows.append(n)
    rows.append([back("us")])
    await render(q, head("Recent Users"), KB(*rows))


async def h_ust(q, ctx, a, u):
    us = sorted(db.d["users"].values(), key=lambda x: -x["bal"])[:8]
    rows = [[B("%s %s \u2022 %d" % (S["wallet"], x["name"][:18], x["bal"]), "usv:%d" % x["id"])] for x in us]
    rows.append([back("us")])
    await render(q, head("Top Balances"), KB(*rows))


def user_view(uid):
    x = db.user(uid, create=False)
    uname = ("@" + x["username"]) if x.get("username") else "-"
    txt = "%s%s %s\n%s <code>%d</code> \u2022 %s\n%s %s: <b>%d</b>\n%s %s: %s \u2022 %s: %s\n%s %s: %d\n%s %s: %s\n%s %s: %s" % (
        head("User"), S["user"], esc(x["name"]), S["key"], x["id"], esc(uname), S["wallet"], sc("Balance"), x["bal"],
        S["plus"], sc("Deposited"), money(x["deposited"]), sc("Spent"), money(x["spent"]), S["folder"], sc("Files"), len(x["buys"]),
        S["clock"], sc("Joined"), dt(x["joined"]), S["ban"], sc("Banned"), "YES" if x["banned"] else "no")
    i = x["id"]
    kb = KB([B("%s ADD COINS" % S["plus"], "usa:%d" % i, style="success"), B("%s REMOVE COINS" % S["minus"], "usr:%d" % i, style="danger")],
            [B("%s MESSAGE" % S["chat"], "usg:%d" % i, style="primary"),
             B(("%s UNBAN" if x["banned"] else "%s BAN") % S["ban"], "usb:%d" % i, style="danger")],
            [back("us")])
    return txt, kb


async def h_usv(q, ctx, a, u):
    if not db.user(int(a[1]), create=False):
        return await render(q, sc("User not found."), KB([back("us")]))
    t, kb = user_view(int(a[1]))
    await render(q, t, kb)


async def h_usb(q, ctx, a, u):
    x = db.user(int(a[1]), create=False)
    if x and not is_admin(x["id"]):
        x["banned"] = not x["banned"]
        db.touch()
    await h_usv(q, ctx, a, u)


async def h_usx(q, ctx, a, u):
    """usa / usr / usg"""
    uid = int(a[1])
    kind = a[0]
    names = {"usa": ("us_add", "Send the number of coins to ADD."), "usr": ("us_rem", "Send the number of coins to REMOVE."),
             "usg": ("us_msg", "Send the message to deliver to this user (text, photo, file...).")}
    ctx.user_data["st"] = {"n": names[kind][0], "d": {"uid": uid}}
    await render(q, head("User Action") + sc(names[kind][1]), KB([back("usv:%d" % uid, "CANCEL")]))


async def st_us_search(msg, ctx, st, text):
    if not text.strip().isdigit() or not db.user(int(text), create=False):
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("User not found. Send a valid User ID.")))
    ctx.user_data.pop("st", None)
    t, kb = user_view(int(text))
    await say(msg.chat_id, t, kb)


async def st_us_coins(msg, ctx, st, text):
    uid = st["d"]["uid"]
    if not text.strip().isdigit() or int(text) < 1:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number.")))
    amt = int(text)
    ctx.user_data.pop("st", None)
    if st["n"] == "us_add":
        db.credit(uid, amt, "Added by admin")
        await say(uid, "%s <b>+%d</b> %s" % (S["gift"], amt, sc("coins were added to your wallet.")))
    else:
        if not db.debit(uid, amt, "Removed by admin"):
            return await say(msg.chat_id, "%s %s" % (S["no"], sc("User balance is lower than that.")))
    t, kb = user_view(uid)
    await say(msg.chat_id, "%s %s\n\n%s" % (S["ok"], sc("Done."), t), kb)


async def st_us_msg(msg, ctx, st, text):
    uid = st["d"]["uid"]
    ctx.user_data.pop("st", None)
    try:
        await BOT.copy_message(uid, msg.chat_id, msg.message_id)
        await say(msg.chat_id, "%s %s" % (S["ok"], sc("Message sent.")), KB([back("usv:%d" % uid, "USER")]))
    except TelegramError as e:
        await say(msg.chat_id, "%s %s <code>%s</code>" % (S["no"], sc("Could not send:"), esc(e)))


# ---------- orders ----------
async def h_or(q, ctx, a, u):
    os_ = sorted(db.d["orders"].values(), key=lambda x: -x["id"])
    items, p, total = page(os_, a[1] if len(a) > 1 else 0, 7)
    mark = {"pending": "\u25CB", "paid": "\u2713", "expired": "\u2718", "cancelled": "\u2718"}
    rows = [[B("%s #%d \u2022 %s \u2022 %s \u2022 %d" % (mark.get(o["status"], "?"), o["id"], money(o["pay"]), o["status"], o["uid"]), "ord:%d" % o["id"])] for o in items]
    n = nav("or", p, total)
    if n:
        rows.append(n)
    rows.append([back("adm")])
    await render(q, head("Orders") + (sc("Tap an order.") if os_ else sc("No orders yet.")), KB(*rows))


async def h_ord(q, ctx, a, u):
    o = db.d["orders"].get(a[1])
    if not o:
        return await h_or(q, ctx, ["or", "0"], u)
    p = db.product(o["pid"]) if o["kind"] == "product" else None
    txt = "%s%s <code>%s</code>\n%s <code>%d</code>\n%s %s: <b>%s</b> (%s %s)\n%s %s: %s\n%s %s: <b>%s</b>\n%s %s: %s" % (
        head("Order #%d" % o["id"]), sc("Ref"), o["ref"], S["user"], o["uid"], S["wallet"], sc("Pay"), money(o["pay"]), sc("base"), money(o["base"]),
        S["folder"], sc("For"), esc(p["title"]) if p else sc("Deposit"), S["rec"], sc("Status"), o["status"].upper(), S["clock"], sc("Created"), dt(o["created"], True))
    rows = []
    if o["status"] in ("pending", "expired", "cancelled"):
        rows.append([B("%s APPROVE MANUALLY" % S["ok"], "ora:%d" % o["id"], style="success")])
    if o["status"] == "pending":
        rows.append([B("%s REJECT" % S["no"], "orr:%d" % o["id"], style="danger")])
    rows.append([back("or:0", "ORDERS")])
    await render(q, txt, KB(*rows))


async def h_ora(q, ctx, a, u):
    o = db.d["orders"].get(a[1])
    if o and o["status"] != "paid":
        o["status"] = "pending"
        await finalize(o, None, "admin %d" % q.from_user.id)
    await h_ord(q, ctx, a, u)


async def h_orr(q, ctx, a, u):
    o = db.d["orders"].get(a[1])
    if o:
        await close_order(o, "cancelled", "%s %s" % (S["no"], sc("Your payment order was rejected by admin.")))
    await h_ord(q, ctx, a, u)


# ---------- broadcast ----------
async def h_bc(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "bc_msg", "d": {}}
    await render(q, head("Broadcast") + sc("Send the message you want to broadcast (text, photo, video, file - anything)."), KB([back("adm", "CANCEL")]))


async def st_bc_msg(msg, ctx, st, text):
    st["d"] = {"chat": msg.chat_id, "mid": msg.message_id}
    st["n"] = "bc_confirm"
    n = len([x for x in db.d["users"].values() if not x["banned"] and not x["blocked"]])
    await say(msg.chat_id, "%s%s %d %s" % (head("Confirm Broadcast"), sc("Send this message to"), n, sc("users?")),
              KB([B("%s SEND" % S["ok"], "bcy", style="success"), B("%s CANCEL" % S["no"], "adm", style="danger")]))


async def h_bcy(q, ctx, a, u):
    st = ctx.user_data.pop("st", None)
    if not st or st["n"] != "bc_confirm":
        return await render(q, sc("Session expired."), KB([back("adm")]))
    await render(q, "%s %s" % (S["broadcast"], sc("Broadcast started. You will get a report.")), KB([back("adm")]))
    spawn(run_broadcast(st["d"]["chat"], st["d"]["mid"], q.from_user.id))


async def run_broadcast(chat, mid, admin):
    sent = failed = 0
    for x in list(db.d["users"].values()):
        if x["banned"] or x["blocked"]:
            continue
        for attempt in range(2):
            try:
                await BOT.copy_message(x["id"], chat, mid)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                x["blocked"] = True
                db.touch()
                failed += 1
                break
            except TelegramError:
                failed += 1
                break
        await asyncio.sleep(0.06)
    await say(admin, "%s <b>%s</b>\n%s %d \u2022 %s %d" % (S["ok"], sc("Broadcast finished"), sc("Sent"), sent, sc("Failed"), failed))


# ---------- stats / settings / backup ----------
async def h_sts(q, ctx, a, u):
    us = db.d["users"].values()
    day = now() - 86400
    st = db.d["stats"]
    txt = "%s%s %s: %d (%s %d)\n%s %s: %d\n%s %s: %d\n%s %s: %s\n%s %s: %s\n%s %s: %s\n%s %s: %d\n%s %s: %d" % (
        head("Statistics"), S["users"], sc("Users"), len(db.d["users"]), sc("24h"), len([x for x in us if x["joined"] > day]),
        S["folder"], sc("Files"), len(db.d["products"]), S["ticket"], sc("Paid orders"), st["paid_orders"],
        S["plus"], sc("Cash received"), money(st["cash"]), S["wallet"], sc("Sales value"), money(st["sales"]),
        S["wallet"], sc("Coins in wallets"), money(sum(x["bal"] for x in us)), S["gift"], sc("Gift codes"), len(db.d["gifts"]),
        S["clock"], sc("Pending orders"), len(db.pending_orders()))
    await render(q, txt, KB([B("%s 7-DAY REPORT" % S["chart_up"], "rpt", style="primary")],
                            [B("%s USERS CSV" % S["download"], "exp:users", style="success"), B("%s SALES CSV" % S["download"], "exp:sales", style="success")],
                            [back("adm")]))


SET_PROMPTS = {
    "upi": ("set_upi", "Send your Fampay UPI ID (example: name@fam).", True),
    "payee": ("set_payee", "Send the payee name shown in the UPI app.", True),
    "support": ("set_support", "Send the support Telegram username (example: @zadosint) or - to clear.", False),
    "min": ("set_min", "Send the minimum deposit in rupees.", False),
    "mint": ("set_mint", "Send the minimum transfer in coins.", False),
    "mins": ("set_mins", "Send order expiry in minutes (5-60).", False),
    "bonus": ("set_bonus", "Send: PERCENT MIN_AMOUNT (example: 5 500 = 5 percent bonus on deposits of 500 or more). Send 0 to turn it off.", False),
    "welcome": ("set_welcome", "Send the new welcome text for the home screen, or - to reset.", False),
}


async def h_set(q, ctx, a, u):
    ctx.user_data.pop("st", None)
    c = cfg()
    gm = "%s" % (sc("connected") if mail_ok() else sc("NOT set"))
    gm_state = (" \u2022 " + sc("last ok: ") + dt(MAILST["last_ok"], True)) if MAILST["last_ok"] else ""
    txt = ("%s%s %s: <code>%s</code>\n%s %s: %s\n%s %s: %s\n%s %s: %d \u2022 %s: %d\n%s %s: %d %s\n%s %s: Gmail %s%s\n%s %s: %s") % (
        head("Settings"), S["key"], sc("UPI ID"), esc(upi_id() or "-"), S["user"], sc("Payee"), esc(payee()), S["chat"], sc("Support"), esc(c["support"] or "-"),
        S["wallet"], sc("Min deposit"), c["min_deposit"], sc("Min transfer"), c["min_transfer"], S["clock"], sc("Order expiry"), c["order_minutes"], sc("min"),
        S["bolt"], sc("Verify"), gm, gm_state, S["cloud"], sc("GitHub backup"), sc("on") if gh else sc("OFF"))
    own = is_owner(q.from_user.id)
    rows = []
    if own:
        rows.append([B("UPI ID", "sets:upi", style="danger"), B("PAYEE NAME", "sets:payee", style="danger")])
    rows += [
        [B("SUPPORT", "sets:support", style="primary"), B("WELCOME TEXT", "sets:welcome", style="primary")],
        [B("MIN DEPOSIT", "sets:min", style="primary"), B("MIN TRANSFER", "sets:mint", style="primary")],
        [B("ORDER EXPIRY", "sets:mins", style="primary"), B("DEPOSIT BONUS", "sets:bonus", style="primary")],
        [B("%s FORCE JOIN" % S["lock"], "fjs", style="primary")],
        [B(("%s MAINTENANCE: ON" if c["maintenance"] else "%s MAINTENANCE: OFF") % S["rec"], "sets:maint", style="success"),
         B(("%s PROTECT FILES: ON" if c["protect"] else "%s PROTECT FILES: OFF") % S["lock"], "sets:protect", style="success")],
        [back("adm")]]
    await render(q, txt, KB(*rows))


async def h_sets(q, ctx, a, u):
    key = a[1]
    if key in ("maint", "protect"):
        k = "maintenance" if key == "maint" else "protect"
        cfg()[k] = not cfg()[k]
        db.touch()
        return await h_set(q, ctx, a, u)
    if key not in SET_PROMPTS or (SET_PROMPTS[key][2] and not is_owner(q.from_user.id)):
        return await q.answer("Owner only", show_alert=True)
    ctx.user_data["st"] = {"n": SET_PROMPTS[key][0], "d": {}}
    await render(q, head("Setting") + sc(SET_PROMPTS[key][1]), KB([back("set", "CANCEL")]))


async def st_set(msg, ctx, st, text):
    key = st["n"][4:]
    t = text.strip()
    c = cfg()
    if SET_PROMPTS[key][2] and not is_owner(msg.from_user.id):
        return
    if key == "upi":
        if not re.fullmatch(r"[\w.\-]{2,}@[\w.\-]{2,}", t):
            return await say(msg.chat_id, "%s %s" % (S["no"], sc("That does not look like a UPI ID (name@bank).")))
        c["upi_id"] = t
    elif key == "payee":
        c["payee"] = t[:40]
    elif key == "support":
        c["support"] = "" if t == "-" else t.lstrip("@")[:40]
    elif key == "welcome":
        c["welcome"] = "" if t == "-" else t[:500]
    elif key == "bonus":
        parts = t.split()
        if not parts or not parts[0].isdigit() or int(parts[0]) > 100 or (len(parts) > 1 and not parts[1].isdigit()):
            return await say(msg.chat_id, "%s %s" % (S["no"], sc("Example: 5 500")))
        c["bonus_pct"] = int(parts[0])
        c["bonus_min"] = int(parts[1]) if len(parts) > 1 else c.get("bonus_min", 500)
    else:
        if not t.isdigit() or int(t) < 1:
            return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number.")))
        v = int(t)
        if key == "mins":
            v = max(5, min(60, v))
        c[{"min": "min_deposit", "mint": "min_transfer", "mins": "order_minutes"}[key]] = v
    db.touch()
    ctx.user_data.pop("st", None)
    await say(msg.chat_id, "%s %s" % (S["ok"], sc("Saved.")), KB([back("set", "SETTINGS")]))


async def h_bk(q, ctx, a, u):
    if not gh:
        txt = head("Backup") + "%s %s" % (S["warn"], sc("GitHub backup is not configured. Set GITHUB_TOKEN and GITHUB_REPO."))
    else:
        txt = "%s%s %s\n%s %s: %s\n%s %s: %d\n%s %s: <code>%s</code>" % (
            head("Backup"), S["cloud"], esc(GH_REPO + "/" + GH_PATH), S["clock"], sc("Last backup"),
            dt(BK["last_ok"], True) if BK["last_ok"] else sc("not yet"), S["graph"], sc("Data revision"), db.d["_rev"],
            S["warn"], sc("Last error"), esc(BK["err"] or "none"))
    rows = [[B("%s BACKUP NOW" % S["cloud"], "bkn", style="success"), B("%s GET data.json" % S["download"], "bkf", style="primary")], [back("adm")]]
    await render(q, txt, KB(*rows))


async def h_bkn(q, ctx, a, u):
    res = await backup_now(force=True)
    await q.answer(res[:180], show_alert=True)
    await h_bk(q, ctx, a, u)


async def h_bkf(q, ctx, a, u):
    if not is_owner(q.from_user.id):
        return await q.answer("Owner only", show_alert=True)
    bio = io.BytesIO(db.dump().encode("utf-8"))
    await BOT.send_document(q.from_user.id, bio, filename="data.json", caption="Data backup (keep it private)")


# ---------- admins / buttons (owner) ----------
async def h_ad(q, ctx, a, u):
    ctx.user_data.pop("st", None)
    rows = []
    for aid in db.d["admins"]:
        x = db.user(aid, create=False)
        rows.append([B("%s %s (%d)" % (S["trash"], (x["name"] if x else "Admin")[:16], aid), "adr:%d" % aid, style="danger")])
    rows.append([B("%s ADD ADMIN" % S["plus"], "adn", style="success")])
    rows.append([back("adm")])
    await render(q, head("Admins") + (sc("Tap an admin to remove.") if db.d["admins"] else sc("No admins yet. Only you (owner).")), KB(*rows))


async def h_adn(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "ad_add", "d": {}}
    await render(q, head("Add Admin") + sc("Send the Telegram numeric User ID of the new admin."), KB([back("ad", "CANCEL")]))


async def st_ad_add(msg, ctx, st, text):
    if not text.strip().isdigit():
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a numeric User ID.")))
    aid = int(text)
    if aid not in db.d["admins"] and aid != OWNER_ID:
        db.d["admins"].append(aid)
        db.touch()
    ctx.user_data.pop("st", None)
    await say(aid, "%s %s" % (S["shield"], sc("You are now an admin. Send /admin to open the panel.")))
    await say(msg.chat_id, "%s %s" % (S["ok"], sc("Admin added.")), KB([back("ad", "ADMINS")]))


async def h_adr(q, ctx, a, u):
    aid = int(a[1])
    if aid in db.d["admins"]:
        db.d["admins"].remove(aid)
        db.touch()
    await h_ad(q, ctx, a, u)


async def h_bt(q, ctx, a, u):
    ctx.user_data.pop("st", None)
    rows = [[B("%s %s" % (S["trash"], b["label"][:30]), "btr:%d" % i, style="danger")] for i, b in enumerate(db.d["buttons"])]
    rows.append([B("%s ADD BUTTON" % S["plus"], "btn", style="success")])
    rows.append([back("adm")])
    await render(q, head("Home Buttons") + sc("Add link buttons (channel, YouTube, group) to the home screen. Tap one to remove."), KB(*rows))


async def h_btn(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "bt_add", "d": {}}
    await render(q, head("Add Button") + sc("Send in this format:") + "\n<code>Label | https://t.me/yourchannel</code>", KB([back("bt", "CANCEL")]))


async def st_bt_add(msg, ctx, st, text):
    if "|" not in text:
        return await say(msg.chat_id, "%s Label | https://link" % S["no"])
    label, url = [x.strip() for x in text.split("|", 1)]
    if not label or not re.match(r"^(https?://|tg://)", url):
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Link must start with https:// or tg://")))
    db.d["buttons"].append({"label": label[:30], "url": url})
    db.touch()
    ctx.user_data.pop("st", None)
    await say(msg.chat_id, "%s %s" % (S["ok"], sc("Button added.")), KB([back("bt", "BUTTONS")]))


async def h_btr(q, ctx, a, u):
    i = int(a[1])
    if 0 <= i < len(db.d["buttons"]):
        db.d["buttons"].pop(i)
        db.touch()
    await h_bt(q, ctx, a, u)


# ============================ EXTRA FEATURES ============================
def bonus_line():
    c = cfg()
    if c.get("bonus_pct", 0) > 0:
        return "\n%s %s +%d%% %s %d+" % (S["gift"], sc("Bonus:"), c["bonus_pct"], sc("on deposits of"), c.get("bonus_min", 0))
    return ""


# ---------- search ----------
async def h_srch(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "search", "d": {}}
    await render(q, head("Search Files") + sc("Send a keyword (name, category or word from description)."), KB([back("shop", "CANCEL")]))


async def show_results(uid, term):
    t = term.lower().strip()
    res = [p for p in db.d["products"].values() if p["active"] and t and (t in p["title"].lower() or t in p["desc"].lower() or t in p["cat"].lower())]
    res = sorted(res, key=lambda x: -x["id"])[:10]
    rows = [[B("%s %s \u2022 %s" % (S["star"], x["title"][:30], money(x["price"])), "p:%d" % x["id"])] for x in res]
    rows.append([B("%s SEARCH AGAIN" % S["eye"], "srch", style="primary"), back("home", "HOME")])
    await say(uid, head("Search Results") + (sc("Tap a file.") if res else sc("Nothing found. Try another word.")), KB(*rows))


async def st_search(msg, ctx, st, text):
    ctx.user_data.pop("st", None)
    await show_results(msg.chat_id, text)


# ---------- user: discount coupon ----------
async def h_dc(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "coupon", "d": {"pid": int(a[1])}}
    await render(q, head("Discount Code") + sc("Send your discount code."), KB([back("p:%s" % a[1], "CANCEL")]))


async def st_coupon(msg, ctx, st, text):
    uid, pid = msg.from_user.id, st["d"]["pid"]
    code = text.strip().upper()
    c = db.coupon_ok(code, uid)
    ctx.user_data.pop("st", None)
    if not c:
        return await say(uid, "%s %s" % (S["no"], sc("Invalid, used or expired code.")), KB([back("p:%d" % pid, "FILE")]))
    ctx.user_data["disc"] = {"pid": pid, "code": code}
    await say(uid, "%s %s <b>%d%%</b> %s" % (S["ok"], sc("Coupon applied:"), c["pct"], sc("off")), KB([B("%s VIEW FILE" % S["star"], "p:%d" % pid, style="success")]))


# ---------- admin: coupons ----------
async def h_cp(q, ctx, a, u):
    codes = sorted(db.d["coupons"].items(), key=lambda kv: -kv[1]["created"])
    items, p, total = page(codes, a[1] if len(a) > 1 else 0, 6)
    rows = [[B("%s %s \u2022 %d%% \u2022 %d/%d" % (S["ticket"], c, g["pct"], len(g["used"]), g["max"]), "cpv:" + c)] for c, g in items]
    n = nav("cp", p, total)
    if n:
        rows.append(n)
    rows.append([B("%s NEW COUPON" % S["plus"], "cpn", style="success")])
    rows.append([back("adm")])
    await render(q, head("Discount Coupons") + (sc("Percent-off codes users enter on a file page.") if codes else sc("No coupons yet.")), KB(*rows))


async def h_cpn(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "cp_pct", "d": {}}
    await render(q, head("New Coupon") + sc("Step 1/3: Discount PERCENT (1-90)."), KB([back("cp", "CANCEL")]))


async def st_cp_pct(msg, ctx, st, text):
    if not text.strip().isdigit() or not (1 <= int(text) <= 90):
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number from 1 to 90.")))
    st["d"]["pct"], st["n"] = int(text), "cp_uses"
    await say(msg.chat_id, sc("Step 2/3: How many TOTAL USES?"))


async def st_cp_uses(msg, ctx, st, text):
    if not text.strip().isdigit() or int(text) < 1:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Send a number.")))
    st["d"]["max"], st["n"] = int(text), "cp_code"
    await say(msg.chat_id, sc("Step 3/3: Send a custom CODE (A-Z, 0-9, - _ , 4-20 chars) or tap AUTO."), KB([B("%s AUTO" % S["spark"], "cpa", style="success")]))


def make_coupon(uid, d, code=None):
    code = (code or db.new_ccode()).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{4,20}", code) or code in db.d["coupons"]:
        return None
    db.d["coupons"][code] = {"pct": d["pct"], "max": d["max"], "used": [], "created": now(), "by": uid, "active": True}
    db.touch()
    return code


async def st_cp_code(msg, ctx, st, text):
    code = make_coupon(msg.from_user.id, st["d"], text.strip())
    if not code:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Invalid or already exists. Try another or tap AUTO.")), KB([B("%s AUTO" % S["spark"], "cpa")]))
    ctx.user_data.pop("st", None)
    await say(msg.chat_id, "%s %s\n<code>%s</code>\n%d%% \u2022 %d %s" % (S["ok"], sc("Coupon created:"), code, st["d"]["pct"], st["d"]["max"], sc("uses")), KB([back("cp", "COUPONS")]))


async def h_cpa(q, ctx, a, u):
    st = ctx.user_data.get("st")
    if not st or st["n"] != "cp_code":
        return await render(q, sc("Session expired."), KB([back("cp")]))
    code = make_coupon(q.from_user.id, st["d"])
    ctx.user_data.pop("st", None)
    await render(q, "%s %s\n<code>%s</code>\n%d%% \u2022 %d %s" % (S["ok"], sc("Coupon created:"), code, st["d"]["pct"], st["d"]["max"], sc("uses")), KB([back("cp", "COUPONS")]))


async def h_cpv(q, ctx, a, u):
    g = db.d["coupons"].get(a[1])
    if not g:
        return await h_cp(q, ctx, ["cp", "0"], u)
    await render(q, "%s<code>%s</code>\n%s %d%% %s\n%s %s: %d / %d" % (head("Coupon"), a[1], S["ticket"], g["pct"], sc("off"), S["users"], sc("Used"), len(g["used"]), g["max"]),
                 KB([B("%s DELETE" % S["trash"], "cpd:" + a[1], style="danger")], [back("cp")]))


async def h_cpd(q, ctx, a, u):
    db.d["coupons"].pop(a[1], None)
    db.touch()
    await h_cp(q, ctx, ["cp", "0"], u)


# ---------- force join channels ----------
JOIN_CACHE = {}


async def join_missing(uid):
    miss = []
    for ch in db.d["channels"]:
        try:
            m = await BOT.get_chat_member(ch["id"], uid)
            if m.status in ("left", "kicked"):
                miss.append(ch)
        except TelegramError as e:
            await report_error("Force-join check failed for %s (make the bot ADMIN there): %s" % (ch.get("title"), e))
    return miss


async def h_fjs(q, ctx, a, u):
    ctx.user_data.pop("st", None)
    rows = [[B("%s %s" % (S["trash"], c["title"]), "fjr:%d" % i, style="danger")] for i, c in enumerate(db.d["channels"])]
    rows.append([B("%s ADD CHANNEL" % S["plus"], "fja", style="success")])
    rows.append([back("set", "SETTINGS")])
    await render(q, head("Force Join") + sc("Users must join these channels before using the bot. The bot must be ADMIN in each channel. Tap one to remove."), KB(*rows))


async def h_fja(q, ctx, a, u):
    ctx.user_data["st"] = {"n": "fj_add", "d": {}}
    await render(q, head("Add Channel") + sc("Send @channelusername. For a private channel send:") + "\n<code>-100123456789 | https://t.me/+invitelink</code>", KB([back("fjs", "CANCEL")]))


async def st_fj_add(msg, ctx, st, text):
    t, link = text.strip(), None
    if "|" in t:
        t, link = [x.strip() for x in t.split("|", 1)]
    cid = int(t) if t.lstrip("-").isdigit() else (t if t.startswith("@") else "@" + t)
    try:
        chat = await BOT.get_chat(cid)
        me = await BOT.get_chat_member(chat.id, BOT.id)
    except TelegramError as e:
        return await say(msg.chat_id, "%s <code>%s</code>\n%s" % (S["no"], esc(e), sc("Could not read that channel. Check the name and add the bot as admin.")))
    if me.status not in ("administrator", "creator"):
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Make the bot an ADMIN of that channel first, then send it again.")))
    link = link or (("https://t.me/" + chat.username) if chat.username else None)
    if not link:
        return await say(msg.chat_id, "%s %s" % (S["no"], sc("Private channel: send as -100id | invite link")))
    db.d["channels"].append({"id": chat.id, "title": (chat.title or "Channel")[:24], "link": link})
    db.touch()
    JOIN_CACHE.clear()
    ctx.user_data.pop("st", None)
    await say(msg.chat_id, "%s %s" % (S["ok"], sc("Channel added. Force join is ON.")), KB([back("fjs", "CHANNELS")]))


async def h_fjr(q, ctx, a, u):
    i = int(a[1])
    if 0 <= i < len(db.d["channels"]):
        db.d["channels"].pop(i)
        db.touch()
        JOIN_CACHE.clear()
    await h_fjs(q, ctx, a, u)


# ---------- reports / exports ----------
async def h_rpt(q, ctx, a, u):
    t0 = now()
    keys = [dt(t0 - i * 86400) for i in range(7)]
    agg = {k: [0, 0, 0] for k in keys}
    for x in db.d["users"].values():
        for b in x["buys"]:
            k = dt(b["t"])
            if k in agg:
                agg[k][0] += b["paid"]
                agg[k][1] += 1
    for o in db.d["orders"].values():
        if o["status"] == "paid" and o["kind"] == "deposit" and o.get("paid"):
            k = dt(o["paid"])
            if k in agg:
                agg[k][2] += o["base"]
    lines = ["%s <b>%s</b>\n    %s \u2022 %d %s \u2022 %s %s" % (S["bullet"], k, money(v[0]), v[1], sc("files"), sc("deposits"), money(v[2])) for k, v in agg.items()]
    tot = [sum(v[i] for v in agg.values()) for i in range(3)]
    lines.append("\n%s <b>%s</b>: %s \u2022 %d %s \u2022 %s %s" % (S["star"], sc("7 days"), money(tot[0]), tot[1], sc("files"), sc("deposits"), money(tot[2])))
    await render(q, head("7-Day Report") + "\n".join(lines), KB([back("sts", "STATS")]))


def csv_file(header, rows):
    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerow(header)
    w.writerows(rows)
    return io.BytesIO(sio.getvalue().encode("utf-8-sig"))


async def h_exp(q, ctx, a, u):
    uid = q.from_user.id
    if a[1] == "users":
        rows = [[x["id"], x["name"], x.get("username", ""), x["bal"], x["spent"], x["deposited"], dt(x["joined"]), x["banned"]] for x in db.d["users"].values()]
        bio = csv_file(["id", "name", "username", "balance", "spent", "deposited", "joined", "banned"], rows)
        name = "users.csv"
    else:
        rows = [[x["id"], b["title"], b["paid"], b["via"], dt(b["t"], True)] for x in db.d["users"].values() for b in x["buys"]]
        bio = csv_file(["user_id", "file", "paid", "via", "time"], rows)
        name = "sales.csv"
    await BOT.send_document(uid, bio, filename=name, caption=name)


# ---------- commands ----------
async def cmd_search(update, ctx):
    if not await gate(update):
        return
    uid = update.effective_user.id
    if not ctx.args:
        ctx.user_data["st"] = {"n": "search", "d": {}}
        return await say(uid, head("Search Files") + sc("Send a keyword."))
    await show_results(uid, " ".join(ctx.args))


async def cmd_help(update, ctx):
    if not await gate(update):
        return
    await say(update.effective_user.id, head("Help") + "\n".join([
        "/start - %s" % sc("open the store"), "/search word - %s" % sc("find files"), "/redeem CODE - %s" % sc("gift code"),
        "/cancel - %s" % sc("stop current step"), "/id - %s" % sc("your Telegram ID")]), KB([back("home", "HOME")]))


# ============================ ROUTING ============================
ROUTES = {
    "home": h_home, "shop": h_shop, "cat": h_cat, "p": h_prod, "buy": h_buy, "buyy": h_buyy, "dl": h_dl, "upi": h_upi,
    "wal": h_wal, "his": h_his, "mp": h_mp, "lb": h_lb, "sup": h_sup, "red": h_red, "tr": h_tr, "trok": h_trok,
    "dep": h_dep, "depc": h_depc, "cx": h_cx, "chk": h_chk,
    # admin
    "adm": h_adm, "ap": h_ap, "apl": h_apl, "apd": h_apd, "ape": h_ape, "edone": h_edone, "apt": h_apt, "aps": h_aps,
    "apx": h_apx, "apxy": h_apxy, "gc": h_gc, "gcn": h_gcn, "gca": h_gca, "gcv": h_gcv, "gcd": h_gcd,
    "us": h_us, "uss": h_uss, "usl": h_usl, "ust": h_ust, "usv": h_usv, "usb": h_usb, "usa": h_usx, "usr": h_usx, "usg": h_usx,
    "or": h_or, "ord": h_ord, "ora": h_ora, "orr": h_orr, "bc": h_bc, "bcy": h_bcy, "sts": h_sts, "set": h_set, "sets": h_sets,
    "bk": h_bk, "bkn": h_bkn, "bkf": h_bkf,
    "ad": h_ad, "adn": h_adn, "adr": h_adr, "bt": h_bt, "btn": h_btn, "btr": h_btr,
    "srch": h_srch, "dc": h_dc, "fj": h_home, "cp": h_cp, "cpn": h_cpn, "cpa": h_cpa, "cpv": h_cpv, "cpd": h_cpd,
    "fjs": h_fjs, "fja": h_fja, "fjr": h_fjr, "rpt": h_rpt, "exp": h_exp,
}
OWNER_ONLY = {"ad", "adn", "adr", "bt", "btn", "btr"}
KEEP_STATE = {"ap", "edone", "gca", "cpa", "bcy", "trok", "noop"}
LAZY_ANSWER = {"chk", "dl", "dep", "ap", "edone", "sets", "bkn", "bkf", "buy", "buyy", "upi"}
USER_ROUTES = {"home", "shop", "cat", "p", "buy", "buyy", "dl", "upi", "wal", "his", "mp", "lb", "sup", "red", "tr", "trok",
               "dep", "depc", "cx", "chk", "srch", "dc", "fj"}

STATES = {
    "redeem": st_redeem, "tr_to": st_tr_to, "tr_amt": st_tr_amt, "dep_custom": st_dep_custom,
    "ap_title": st_ap_title, "ap_price": st_ap_price, "ap_cat": st_ap_cat, "ap_desc": st_ap_desc, "ap_files": st_ap_files,
    "ed_stock": st_edit, "ed_title": st_edit, "ed_price": st_edit, "ed_desc": st_edit, "ed_cat": st_edit, "ed_files": st_edit,
    "gc_coins": st_gc_coins, "gc_uses": st_gc_uses, "gc_code": st_gc_code,
    "us_search": st_us_search, "us_add": st_us_coins, "us_rem": st_us_coins, "us_msg": st_us_msg,
    "bc_msg": st_bc_msg, "set_upi": st_set, "set_payee": st_set, "set_support": st_set, "set_min": st_set,
    "set_mint": st_set, "set_bonus": st_set, "search": st_search, "coupon": st_coupon,
    "cp_pct": st_cp_pct, "cp_uses": st_cp_uses, "cp_code": st_cp_code, "fj_add": st_fj_add, "set_mins": st_set, "set_welcome": st_set, "ad_add": st_ad_add, "bt_add": st_bt_add,
}
FILE_STATES = {"ap_files", "ed_files", "us_msg", "bc_msg"}


class QProxy:
    """Wraps a callback query so q.answer() can only be sent once (Telegram allows one answer)."""

    def __init__(self, q):
        self._q = q
        self._done = False
        self.data = q.data

    def __getattr__(self, n):
        return getattr(self._q, n)

    async def answer(self, text=None, show_alert=False):
        if self._done:
            return
        self._done = True
        try:
            await self._q.answer(text, show_alert=show_alert)
        except Exception:
            pass


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.callback_query
    if not raw or not raw.data:
        return
    q = QProxy(raw)
    try:
        await _on_cb(update, ctx, q)
    finally:
        await q.answer()


async def _on_cb(update, ctx, q):
    a = q.data.split(":")
    cmd = a[0]
    if cmd not in LAZY_ANSWER:
        await q.answer()
    if cmd == "noop":
        return
    u = await gate(update)
    if not u:
        return
    fn = ROUTES.get(cmd)
    if not fn:
        return
    uid = q.from_user.id
    if cmd not in USER_ROUTES and not is_admin(uid):
        return
    if cmd in OWNER_ONLY and not is_owner(uid):
        return
    if cmd not in KEEP_STATE:
        ctx.user_data.pop("st", None)
    try:
        await fn(q, ctx, a, u)
    except Exception as e:
        log.exception("callback %s failed", q.data)
        try:
            await render(q, "%s %s" % (S["warn"], sc("Something went wrong. Please try again.")), KB([back()]))
        except Exception:
            pass
        await report_error("cb %s: %s" % (q.data, e))


async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    u = await gate(update)
    if not u:
        return
    uid = msg.from_user.id
    st = ctx.user_data.get("st")
    if st:
        fn = STATES.get(st["n"])
        if fn:
            if st["n"][:3] not in ("us_", "ap_", "ed_", "gc_", "bc_", "set", "ad_", "bt_", "cp_", "fj_") or is_admin(uid):
                if st["n"] in ("ad_add", "bt_add") and not is_owner(uid):
                    ctx.user_data.pop("st", None)
                    return
                text = msg.text or ""
                if not text and st["n"] not in FILE_STATES:
                    return await say(uid, "%s %s" % (S["no"], sc("Please send text. Use /cancel to stop.")))
                try:
                    return await fn(msg, ctx, st, text)
                except Exception as e:
                    log.exception("state %s failed", st["n"])
                    await say(uid, "%s %s" % (S["warn"], sc("Something went wrong. Try again or /cancel.")))
                    return await report_error("state %s: %s" % (st["n"], e))
        ctx.user_data.pop("st", None)
    if msg.text:
        await show_home(chat_id=uid, uid=uid)


async def cmd_start(update, ctx):
    ctx.user_data.pop("st", None)
    if await gate(update):
        await show_home(chat_id=update.effective_user.id, uid=update.effective_user.id)


async def cmd_admin(update, ctx):
    if not await gate(update) or not is_admin(update.effective_user.id):
        return
    ctx.user_data.pop("st", None)
    await say(update.effective_user.id, head("Admin Panel") + sc("Open the panel from the home screen."), KB([B("%s ADMIN PANEL" % S["settings"], "adm", style="primary")]))


async def cmd_cancel(update, ctx):
    ctx.user_data.pop("st", None)
    ctx.user_data.pop("tr", None)
    if await gate(update):
        await say(update.effective_user.id, "%s %s" % (S["ok"], sc("Cancelled.")), KB([back("home", "HOME")]))


async def cmd_id(update, ctx):
    await update.effective_message.reply_text("Your ID: %d" % update.effective_user.id)


async def cmd_redeem(update, ctx):
    if not await gate(update):
        return
    if not ctx.args:
        return await say(update.effective_user.id, sc("Usage: /redeem CODE"))
    await st_redeem(update.effective_message, ctx, {}, ctx.args[0])


async def cmd_addcoins(update, ctx):
    uid = update.effective_user.id
    if not is_admin(uid) or len(ctx.args) != 2 or not ctx.args[0].isdigit() or not ctx.args[1].lstrip("-").isdigit():
        return
    t, n = int(ctx.args[0]), int(ctx.args[1])
    if not db.user(t, create=False):
        return await say(uid, sc("User not found."))
    if n >= 0:
        db.credit(t, n, "Added by admin")
    elif not db.debit(t, -n, "Removed by admin"):
        return await say(uid, sc("User balance too low."))
    await say(uid, "%s %s <b>%d</b>" % (S["ok"], sc("New balance:"), db.user(t)["bal"]))


async def cmd_backup(update, ctx):
    if is_owner(update.effective_user.id):
        res = await backup_now(force=True)
        await say(OWNER_ID, "%s %s" % (S["cloud"], esc(res)))


async def cmd_testmail(update, ctx):
    if not is_owner(update.effective_user.id):
        return
    if not mail_ok():
        return await say(OWNER_ID, "%s GMAIL_USER / GMAIL_APP_PASSWORD not set." % S["warn"])
    await say(OWNER_ID, sc("Reading Gmail..."))
    try:
        mails = await asyncio.to_thread(_poll_blocking)
    except Exception as e:
        return await say(OWNER_ID, "%s <code>%s</code>" % (S["no"], esc("%s: %s" % (type(e).__name__, e))))
    if not mails:
        return await say(OWNER_ID, "%s %s %s" % (S["warn"], sc("No emails from"), esc(", ".join(SENDERS))))
    out = []
    for m in mails[-5:]:
        out.append("%s <b>%s</b>\n%s \u2022 amounts: %s \u2022 credit: %s" % (
            S["bullet"], esc(m["subject"][:60]), dt(m["ts"], True), esc(", ".join(str(x) for x in m["amounts"]) or "none"), "yes" if m["credit"] else "NO"))
    await say(OWNER_ID, "%s\n\n%s" % (head("Last emails"), "\n\n".join(out)))


async def report_error(text):
    key = text[:80]
    if now() - ERR_SEEN.get(key, 0) < 600:
        return
    ERR_SEEN[key] = now()
    if len(ERR_SEEN) > 200:
        ERR_SEEN.clear()
    await say(OWNER_ID, "%s <b>Error</b>\n<code>%s</code>" % (S["warn"], esc(text[:500])))


async def on_error(update, ctx):
    log.error("unhandled error", exc_info=ctx.error)
    await report_error("%s: %s" % (type(ctx.error).__name__, ctx.error))


# ============================ BOOT ============================
BOOT = {"note": ""}


def boot_restore():
    local = db.load_local()
    remote, remote_ok = None, False
    if gh:
        for i in range(5):
            try:
                txt = gh.get()
                remote_ok = True
                if txt:
                    try:
                        remote = json.loads(txt)
                        if not isinstance(remote, dict) or "users" not in remote:
                            raise ValueError("no users key")
                    except Exception as e:
                        sys.exit("GitHub backup file is corrupted (%s). Fix %s/%s or restore a snapshot, then redeploy." % (e, GH_REPO, GH_PATH))
                break
            except GitHubError as e:
                log.warning("GitHub read failed (%d/5): %s", i + 1, e)
                time.sleep(3 * (i + 1))
        if not remote_ok and local is None:
            sys.exit("Cannot reach GitHub backup (check GITHUB_TOKEN / GITHUB_REPO / branch). Refusing to start with empty data.")
    pick, src = None, "fresh"
    if local and remote:
        pick, src = (remote, "github") if remote.get("_rev", 0) >= local.get("_rev", 0) else (local, "local")
    elif remote:
        pick, src = remote, "github"
    elif local:
        pick, src = local, "local"
    if pick:
        db.adopt(pick)
    db.dirty = True
    if env("OWNER_ID"):
        db.user(OWNER_ID, "Owner")
    db.flush_local()
    BOOT["note"] = "%s (rev %d, %d users, %d files)" % (src, db.d["_rev"], len(db.d["users"]), len(db.d["products"]))
    log.info("data loaded from %s", BOOT["note"])


async def post_init(app):
    global BOT, POLL_LOCK
    BOT = app.bot
    POLL_LOCK = asyncio.Lock()
    loop = asyncio.get_running_loop()
    TASKS.append(loop.create_task(payments_loop()))
    TASKS.append(loop.create_task(flush_loop()))
    try:
        await BOT.set_my_commands([("start", "Open the store"), ("redeem", "Redeem a gift code"), ("cancel", "Cancel current action")])
    except Exception:
        pass
    await say(OWNER_ID, "%s <b>%s</b>\n%s %s\n%s Gmail: %s \u2022 UPI: %s \u2022 GitHub: %s" % (
        S["ok"], sc("Bot started"), S["cloud"], esc(BOOT["note"]), S["bolt"],
        "ok" if mail_ok() else "NOT SET", "ok" if upi_id() else "NOT SET", "ok" if gh else "NOT SET"))


async def post_shutdown(app):
    for t in TASKS:
        t.cancel()
    try:
        db.flush_local()
        if gh:
            _push(db.d["_rev"], db.dump())
            log.info("final backup pushed")
    except Exception as e:
        log.warning("final backup failed: %s", e)


def main():
    if not BOT_TOKEN or not OWNER_ID:
        sys.exit("Set BOT_TOKEN and OWNER_ID environment variables.")
    boot_restore()
    start_health()
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    priv = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=priv))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=priv))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=priv))
    app.add_handler(CommandHandler("id", cmd_id, filters=priv))
    app.add_handler(CommandHandler("redeem", cmd_redeem, filters=priv))
    app.add_handler(CommandHandler("search", cmd_search, filters=priv))
    app.add_handler(CommandHandler("help", cmd_help, filters=priv))
    app.add_handler(CommandHandler("addcoins", cmd_addcoins, filters=priv))
    app.add_handler(CommandHandler("backup", cmd_backup, filters=priv))
    app.add_handler(CommandHandler("testmail", cmd_testmail, filters=priv))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(priv & ~filters.COMMAND, on_msg))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
