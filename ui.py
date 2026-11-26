# -*- coding: utf-8 -*-
"""UI helpers: icon dict, small-caps font, formatting."""
import re
import html
from datetime import datetime, timezone, timedelta

S = {
    # core status / decisions
    "ok":         "✓",
    "no":         "\u2718",
    "warn":       "\u26A0",
    "arrow":      "\u2192",
    "bullet":     "\u2022",
    "tri":        "\u25B8",
    "diamond":    "\u25C6",
    "star":       "\u2605",
    "spark":      "\u2726",
    "back":       "↲",
    "fwd":        "\u25B6",
    "plus":       "\u2295",
    "minus":      "\u2296",
    "rec":        "\u25C9",
    "rec_off":    "\u25CB",

    # dividers / borders
    "div":        "\u2501" * 16,
    "div_eq":     "\u2550" * 16,
    "div_dash":   "\u2508" * 16,
    "block_on":   "\u25A0",
    "block_off":  "\u25A1",
    "border_top": "\u2550" * 16,
    "border_mid": "\u2501" * 16,
    "border_bot": "\u2550" * 16,

    # process state
    "play":        "‣",
    "stop":        "\u25A0",
    "pause":       "\u2759\u2759",
    "refresh":     "\u21BB",
    "running":     "\u25B6",
    "stopped":     "■",
    "restarting":  "\u21BB",
    "stop_bot":    "■",

    # security / access
    "lock":     "\u25A3",
    "unlock":   "\u25A2",
    "secure":   "\u25C8",
    "key":      "\u2756",
    "shield":   "\u25C7",
    "ban":      "\u2694",
    "trash":    "\u2716",
    "eye":      "\u25C9",

    # people
    "user":   "\u25C8",
    "users":  "\u25CE",
    "crown":  "\u2654",

    # money / commerce
    "wallet":   "\u25C6",
    "premium":  "⌬",
    "lifetime": "\u2736",
    "gift":     "\u2726",
    "ticket":   "\u273F",
    "trophy":   "\u2605",

    # data / analytics
    "graph":    "\u25AA",
    "stats":    "\u25AA",
    "chart_up": "\u25B2",
    "plan":     "\u25A4",

    # comms
    "broadcast": "⚑",
    "chat":      "\u25AB",

    # storage / files
    "folder":   "\u25B8",
    "upload":   "\u25B4",
    "download": "\u25BE",
    "cloud":    "\u2601",

    # tools / time / energy
    "settings": "⚙",
    "cog":      "\u2699",
    "bolt":     "\u26A1",
    "clock":    "\u23F1",
}

# lowercase -> small caps (uppercase stays as it is, so "Balance" -> "Bᴀʟᴀɴᴄᴇ")
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_TOK = re.compile(r"(<[^>]*>|&[a-zA-Z#0-9]+;)")


def sc(text):
    """Small-caps font for STATIC text only. HTML tags/entities are left untouched."""
    parts = _TOK.split(text)
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            out.append(p)
        else:
            out.append("".join(_SC.get(c, c) for c in p))
    return "".join(out)


def esc(t):
    return html.escape(str(t), quote=False)


IST = timezone(timedelta(hours=5, minutes=30))


def dt(ts, with_time=False):
    d = datetime.fromtimestamp(int(ts), tz=IST)
    return d.strftime("%d %b %Y, %I:%M %p" if with_time else "%d %b %Y")


def money(n):
    try:
        n = float(n)
    except Exception:
        return "\u20B90"
    if n == int(n):
        return "\u20B9%d" % int(n)
    return "\u20B9%.2f" % n
