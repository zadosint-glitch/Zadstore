# -*- coding: utf-8 -*-
"""JSON store with atomic local save + revision counter (used for GitHub backup)."""
import copy
import json
import os
import random
import string
import tempfile
import time


def now():
    return int(time.time())


def _blank():
    return {
        "_rev": 0,
        "settings": {
            "upi_id": "",
            "payee": "",
            "support": "",
            "welcome": "",
            "min_deposit": 10,
            "min_transfer": 1,
            "order_minutes": 15,
            "maintenance": False,
            "protect": False,
            "bonus_pct": 0,
            "bonus_min": 500,
        },
        "admins": [],
        "users": {},
        "products": {},
        "next_pid": 1,
        "orders": {},
        "next_oid": 1,
        "gifts": {},
        "seen_mails": [],
        "buttons": [],
        "channels": [],
        "coupons": {},
        "stats": {"cash": 0, "paid_orders": 0, "deposits": 0, "sales": 0},
    }


class Store:
    def __init__(self, path="data.json"):
        self.path = path
        self.d = _blank()
        self.dirty = False
        self.saved_rev = -1

    # ---------- load / save ----------
    def load_local(self):
        """Return dict from local file or None."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "users" in data:
                return data
        except FileNotFoundError:
            return None
        except Exception:
            return None
        return None

    def adopt(self, data):
        base = _blank()
        for k, v in base.items():
            if k not in data:
                data[k] = copy.deepcopy(v)
        for k, v in base["settings"].items():
            data["settings"].setdefault(k, v)
        for k, v in base["stats"].items():
            data["stats"].setdefault(k, v)
        self.d = data
        self.saved_rev = data.get("_rev", 0)
        self.dirty = False

    def touch(self):
        self.d["_rev"] = self.d.get("_rev", 0) + 1
        self.dirty = True

    def dump(self):
        return json.dumps(self.d, ensure_ascii=False, separators=(",", ":"))

    def flush_local(self):
        if not self.dirty and os.path.exists(self.path):
            return
        text = self.dump()
        folder = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".data-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise

    # ---------- users ----------
    def user(self, uid, name=None, username=None, create=True):
        k = str(uid)
        u = self.d["users"].get(k)
        if u is None:
            if not create:
                return None
            u = {
                "id": int(uid), "name": name or "User", "username": username or "",
                "joined": now(), "bal": 0, "spent": 0, "deposited": 0,
                "banned": False, "blocked": False, "buys": [], "hist": [],
            }
            self.d["users"][k] = u
            self.touch()
        else:
            ch = False
            if name and u.get("name") != name:
                u["name"] = name
                ch = True
            if username is not None and u.get("username") != username:
                u["username"] = username
                ch = True
            if ch:
                self.touch()
        return u

    def log(self, uid, amt, note):
        u = self.user(uid, create=False)
        if not u:
            return
        u["hist"].append([now(), amt, note, u["bal"]])
        if len(u["hist"]) > 200:
            del u["hist"][:-200]

    def credit(self, uid, amt, note):
        u = self.user(uid)
        u["bal"] += int(amt)
        self.log(uid, int(amt), note)
        self.touch()
        return u["bal"]

    def debit(self, uid, amt, note):
        u = self.user(uid)
        amt = int(amt)
        if amt < 0 or u["bal"] < amt:
            return False
        u["bal"] -= amt
        self.log(uid, -amt, note)
        self.touch()
        return True

    # ---------- products ----------
    def add_product(self, title, price, desc, cat, items):
        pid = self.d["next_pid"]
        self.d["next_pid"] = pid + 1
        self.d["products"][str(pid)] = {
            "id": pid, "title": title, "price": int(price), "desc": desc,
            "cat": cat or "General", "items": items, "active": True,
            "sold": 0, "created": now(), "stock": -1,
        }
        self.touch()
        return pid

    def product(self, pid):
        return self.d["products"].get(str(pid))

    def grant(self, uid, pid, via, paid=None):
        """Record a purchase (no money movement)."""
        p = self.product(pid)
        u = self.user(uid)
        if not p:
            return False
        paid = p["price"] if paid is None else int(paid)
        u["buys"].append({"pid": int(pid), "t": now(), "paid": paid, "via": via, "title": p["title"]})
        u["spent"] += paid
        p["sold"] += 1
        if p.get("stock", -1) > 0:
            p["stock"] -= 1
        self.d["stats"]["sales"] += paid
        self.touch()
        return True

    def in_stock(self, p):
        return p.get("stock", -1) != 0

    def coupon_ok(self, code, uid):
        c = self.d["coupons"].get((code or "").strip().upper())
        if c and c.get("active", True) and int(uid) not in c["used"] and len(c["used"]) < c["max"]:
            return c
        return None

    def use_coupon(self, code, uid):
        c = self.d["coupons"].get(code)
        if c and int(uid) not in c["used"]:
            c["used"].append(int(uid))
            self.touch()

    def new_ccode(self):
        while True:
            c = "OFF" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            if c not in self.d["coupons"]:
                return c

    def owns(self, uid, pid):
        u = self.user(uid, create=False)
        return bool(u) and any(b["pid"] == int(pid) for b in u["buys"])

    # ---------- orders ----------
    def pending_orders(self):
        return [o for o in self.d["orders"].values() if o["status"] == "pending"]

    def unique_pay(self, base):
        taken = {o["pay"] for o in self.pending_orders()}
        for k in range(0, 100):
            amt = "%d.%02d" % (int(base), k)
            if amt not in taken:
                return amt
        return None

    def new_order(self, uid, kind, base, pid=None):
        pay = self.unique_pay(base)
        if pay is None:
            return None
        oid = self.d["next_oid"]
        self.d["next_oid"] = oid + 1
        t = now()
        ref = "ZAD" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(5))
        o = {
            "id": oid, "uid": int(uid), "kind": kind, "pid": pid, "base": int(base),
            "pay": pay, "ref": ref, "status": "pending", "created": t,
            "expires": t + int(self.d["settings"]["order_minutes"]) * 60,
            "msg": None, "paid": None, "by": None,
        }
        self.d["orders"][str(oid)] = o
        self.touch()
        # keep orders list small: drop old finished orders
        if len(self.d["orders"]) > 600:
            done = sorted([x for x in self.d["orders"].values() if x["status"] != "pending"], key=lambda x: x["id"])
            for x in done[:200]:
                self.d["orders"].pop(str(x["id"]), None)
        return o

    # ---------- gift codes ----------
    def new_code(self):
        while True:
            c = "ZAD-" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            if c not in self.d["gifts"]:
                return c

    def redeem(self, uid, code):
        """Return (ok, message_key_or_coins)."""
        code = (code or "").strip().upper()
        g = self.d["gifts"].get(code)
        if not g or not g.get("active", True):
            return False, "invalid"
        if int(uid) in g["used"]:
            return False, "used"
        if len(g["used"]) >= g["max"]:
            return False, "full"
        g["used"].append(int(uid))
        self.credit(uid, g["coins"], "Gift code " + code)
        return True, g["coins"]
