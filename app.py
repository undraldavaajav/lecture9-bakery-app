#!/usr/bin/env python3
"""Local bakery order tracker for MGOC50 Lecture 9.

Standard-library only: HTTP server, SQLite storage, and a single embedded page.
"""

from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "bakery.db"
PORT = 8000
BASE_HOURS = 8.0
HELPER_HOURS = 4.0
HELPER_COST = 100.0
PICKUP_TIME = "5:00 PM"
MESSAGE_DATES = {
    "Monday": "2026-11-23",
    "Tuesday": "2026-11-24",
    "Wednesday": "2026-11-25",
    "Thursday": "2026-11-26",
    "Friday": "2026-11-27",
    "Saturday": "2026-11-28",
    "Sunday": "2026-11-29",
}
MESSAGE_PATTERN = re.compile(
    r'<div class="meta"><b>(.*?)</b>\s*&middot;\s*(.*?)</div>'
    r'<p class="message">Name:\s*(.*?)\nOrder:\s*(.*?)\nPickup Date:\s*(.*?)</p>',
    re.S,
)


# ---------------------------------------------------------------------------
# PART 1: DATABASE AND EXTERNAL DATA
# SQLite tables plus the CSV and Instagram HTML importers.
# ---------------------------------------------------------------------------
def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def parse_pickup(text: str) -> str:
    value = f"{text.strip()} 2026"
    for pattern in ("%a %b %d %Y", "%a %d %b %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unknown pickup date: {text}")


def parse_item(text: str) -> tuple[str, int]:
    cleaned = text.lower().strip()
    number = re.search(r"\d+", cleaned)
    quantity = int(number.group()) if number else 1
    if "cupcake" in cleaned:
        return "cupcakes", quantity
    if "cake" in cleaned:
        return "cake", quantity
    raise ValueError(f"Unknown product: {text}")


def item_label(product: str, quantity: int) -> str:
    if product == "cupcakes":
        return f"{quantity} dozen cupcake{'s' if quantity != 1 else ''}"
    return f"{quantity} cake{'s' if quantity != 1 else ''}"


def price_for(product: str, quantity: int) -> float:
    return quantity * (40.0 if product == "cupcakes" else 120.0)


def hours_for(product: str, quantity: int) -> float:
    return quantity * (0.5 if product == "cupcakes" else 2.0)


def import_instagram_messages(conn: sqlite3.Connection) -> int:
    """Import messages not already in the requests table; safe to run repeatedly."""
    imported = 0
    for weekday, message_date in MESSAGE_DATES.items():
        source = f"InstagramOrders_{weekday}.html"
        text = (ROOT / source).read_text(encoding="utf-8")
        for username, message_time, customer, order_text, pickup_text in MESSAGE_PATTERN.findall(text):
            username = html.unescape(username).strip()
            customer = html.unescape(customer).strip()
            order_text = html.unescape(order_text).strip()
            pickup_text = html.unescape(pickup_text).strip()
            if order_text.lower() == "cancel":
                action, product, quantity = "cancel", None, None
            elif order_text.lower().startswith("change to"):
                action = "change"
                product, quantity = parse_item(order_text[9:])
            else:
                action = "new"
                product, quantity = parse_item(order_text)
            raw = f"Name: {customer}\nOrder: {order_text}\nPickup Date: {pickup_text}"
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO requests
                (message_date,message_time,username,customer,action,product,quantity,pickup_date,raw_message,source_file)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (message_date, message_time.strip(), username, customer, action, product, quantity,
                 parse_pickup(pickup_text), raw, source),
            )
            if conn.total_changes > before:
                imported += 1
    return imported


def initialize(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT NOT NULL UNIQUE,
                customer TEXT NOT NULL,
                product TEXT NOT NULL CHECK(product IN ('cupcakes','cake')),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                pickup_date TEXT NOT NULL,
                pickup_time TEXT NOT NULL DEFAULT '5:00 PM',
                status TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted','cancelled')),
                source TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_date TEXT NOT NULL,
                message_time TEXT NOT NULL,
                username TEXT NOT NULL,
                customer TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('new','change','cancel')),
                product TEXT,
                quantity INTEGER,
                pickup_date TEXT NOT NULL,
                raw_message TEXT NOT NULL,
                source_file TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','applied','rejected')),
                decision_note TEXT,
                decided_at TEXT,
                UNIQUE(source_file, username, message_time)
            );
            CREATE TABLE IF NOT EXISTS helpers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pickup_date TEXT NOT NULL UNIQUE,
                booked_on TEXT NOT NULL,
                cost REAL NOT NULL DEFAULT 100,
                extra_hours REAL NOT NULL DEFAULT 4,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        if "picked_up_at" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN picked_up_at TEXT")
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return

        with (ROOT / "existing_orders.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                product, quantity = parse_item(row["item"])
                pickup = parse_pickup(row["pickup_day"])
                conn.execute(
                    "INSERT INTO orders(order_number,customer,product,quantity,pickup_date,source) VALUES(?,?,?,?,?,?)",
                    (row["order"], row["customer"], product, quantity, pickup, "existing_orders.csv"),
                )

        import_instagram_messages(conn)
        conn.execute("INSERT INTO meta(key,value) VALUES('seeded',?)", (datetime.now().isoformat(),))


# ---------------------------------------------------------------------------
# PART 2: BACK END
# Fixed rules, state transitions, and data-changing actions.
# ---------------------------------------------------------------------------
def active_order(conn: sqlite3.Connection, customer: str, pickup_date: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM orders WHERE lower(customer)=lower(?) AND pickup_date=?
        AND status='accepted' AND picked_up_at IS NULL
        ORDER BY id DESC LIMIT 1""",
        (customer, pickup_date),
    ).fetchone()


def helper_for(conn: sqlite3.Connection, pickup_date: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM helpers WHERE pickup_date=?", (pickup_date,)).fetchone()


def used_hours(conn: sqlite3.Connection, pickup_date: str, exclude_order_id: int | None = None) -> float:
    rows = conn.execute(
        "SELECT id,product,quantity FROM orders WHERE pickup_date=? AND status='accepted'", (pickup_date,)
    ).fetchall()
    return sum(hours_for(r["product"], r["quantity"]) for r in rows if r["id"] != exclude_order_id)


def evaluate_request(conn: sqlite3.Connection, req: sqlite3.Row) -> dict:
    notice_days = (date.fromisoformat(req["pickup_date"]) - date.fromisoformat(req["message_date"])).days
    if notice_days < 2:
        return {"ok": False, "reason": f"Only {notice_days} day{'s' if notice_days != 1 else ''} notice; 2 required.", "notice_days": notice_days}

    if req["action"] in ("change", "cancel"):
        target = active_order(conn, req["customer"], req["pickup_date"])
        if not target:
            return {"ok": False, "reason": "No active matching order yet.", "notice_days": notice_days}
        if req["action"] == "cancel":
            return {"ok": True, "reason": "On time and matching order found.", "notice_days": notice_days, "target_id": target["id"]}
        exclude_id = target["id"]
    else:
        target = None
        exclude_id = None

    required = hours_for(req["product"], req["quantity"])
    used = used_hours(conn, req["pickup_date"], exclude_id)
    helper = helper_for(conn, req["pickup_date"])
    capacity = BASE_HOURS + (HELPER_HOURS if helper else 0)
    projected = used + required
    if projected > capacity + 0.0001:
        short = projected - capacity
        helper_hint = " A helper would make it fit." if not helper and projected <= BASE_HOURS + HELPER_HOURS else ""
        return {"ok": False, "reason": f"Needs {projected:g} oven hours; capacity is {capacity:g}. Short by {short:g}.{helper_hint}",
                "notice_days": notice_days, "required_hours": required, "used_hours": used, "capacity": capacity}
    return {"ok": True, "reason": f"Fits: {projected:g} of {capacity:g} oven hours after this request.",
            "notice_days": notice_days, "required_hours": required, "used_hours": used, "capacity": capacity,
            "target_id": target["id"] if target else None}


def next_order_number(conn: sqlite3.Connection) -> str:
    numbers = []
    for row in conn.execute("SELECT order_number FROM orders"):
        match = re.search(r"\d+", row["order_number"])
        if match:
            numbers.append(int(match.group()))
    return f"#{max(numbers, default=100) + 1}"


def serialize_state() -> dict:
    with connect() as conn:
        orders = [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY pickup_date,id")]
        for order in orders:
            order["display_status"] = "picked up" if order["picked_up_at"] else order["status"]
        requests = []
        for row in conn.execute("SELECT * FROM requests ORDER BY message_date,id"):
            item = dict(row)
            item["evaluation"] = evaluate_request(conn, row) if row["status"] == "pending" else None
            item["item_label"] = item_label(row["product"], row["quantity"]) if row["product"] else "Cancel order"
            requests.append(item)
        helpers = [dict(row) for row in conn.execute("SELECT * FROM helpers ORDER BY pickup_date")]

        all_dates = [o["pickup_date"] for o in orders] + [r["pickup_date"] for r in requests]
        start = min(all_dates)
        end = max(all_dates)
        cursor = date.fromisoformat(start)
        last = date.fromisoformat(end)
        days = []
        while cursor <= last:
            iso = cursor.isoformat()
            day_orders = [o for o in orders if o["pickup_date"] == iso and o["status"] == "accepted"]
            helper = next((h for h in helpers if h["pickup_date"] == iso), None)
            hours = sum(hours_for(o["product"], o["quantity"]) for o in day_orders)
            revenue = sum(price_for(o["product"], o["quantity"]) for o in day_orders)
            capacity = BASE_HOURS + (HELPER_HOURS if helper else 0)
            days.append({"date": iso, "orders": day_orders, "hours": hours, "capacity": capacity,
                         "revenue": revenue, "helper": helper})
            cursor = date.fromordinal(cursor.toordinal() + 1)

        accepted = [o for o in orders if o["status"] == "accepted"]
        active = [o for o in accepted if not o["picked_up_at"]]
        summary = {
            "pending": sum(1 for r in requests if r["status"] == "pending"),
            "active_orders": len(active),
            "revenue": sum(price_for(o["product"], o["quantity"]) for o in accepted),
            "helper_cost": sum(h["cost"] for h in helpers),
            "oven_hours": sum(hours_for(o["product"], o["quantity"]) for o in accepted),
        }
        return {"summary": summary, "days": days, "orders": orders, "requests": requests, "helpers": helpers,
                "rules": {"base_hours": BASE_HOURS, "helper_hours": HELPER_HOURS, "helper_cost": HELPER_COST,
                          "pickup_time": PICKUP_TIME, "notice_days": 2}}


def fetch_external_data() -> tuple[bool, str]:
    with connect() as conn:
        imported = import_instagram_messages(conn)
    if imported:
        return True, f"Fetched {imported} new Instagram message{'s' if imported != 1 else ''}."
    return True, "Instagram files are already up to date."


def decide_request(request_id: int, decision: str) -> tuple[bool, str]:
    with connect() as conn:
        req = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            return False, "Request not found."
        if req["status"] != "pending":
            return False, "This request has already been decided."
        if decision == "reject":
            conn.execute("UPDATE requests SET status='rejected',decision_note='Baker rejected',decided_at=CURRENT_TIMESTAMP WHERE id=?", (request_id,))
            return True, "Request rejected."
        if decision != "accept":
            return False, "Unknown decision."

        evaluation = evaluate_request(conn, req)
        if not evaluation["ok"]:
            return False, evaluation["reason"]
        if req["action"] == "new":
            number = next_order_number(conn)
            conn.execute(
                "INSERT INTO orders(order_number,customer,product,quantity,pickup_date,source) VALUES(?,?,?,?,?,?)",
                (number, req["customer"], req["product"], req["quantity"], req["pickup_date"], req["source_file"]),
            )
            status, note = "accepted", f"Accepted as {number}"
        elif req["action"] == "change":
            target = active_order(conn, req["customer"], req["pickup_date"])
            before = item_label(target["product"], target["quantity"])
            conn.execute("UPDATE orders SET product=?,quantity=?,source=? WHERE id=?",
                         (req["product"], req["quantity"], req["source_file"], target["id"]))
            status, note = "applied", f"Changed from {before}"
        else:
            target = active_order(conn, req["customer"], req["pickup_date"])
            conn.execute("UPDATE orders SET status='cancelled',source=? WHERE id=?", (req["source_file"], target["id"]))
            status, note = "applied", f"Cancelled {target['order_number']}"
        conn.execute("UPDATE requests SET status=?,decision_note=?,decided_at=CURRENT_TIMESTAMP WHERE id=?",
                     (status, note, request_id))
        return True, note


def update_helper(payload: dict) -> tuple[bool, str]:
    pickup = payload.get("pickup_date", "")
    booked_on = payload.get("booked_on", "")
    action = payload.get("action", "book")
    try:
        pickup_date = date.fromisoformat(pickup)
        booking_date = date.fromisoformat(booked_on)
    except ValueError:
        return False, "Choose valid booking and pickup dates."
    if (pickup_date - booking_date).days < 2:
        return False, "A helper must be booked at least 2 days before pickup."
    with connect() as conn:
        existing = helper_for(conn, pickup)
        if action == "book":
            if existing:
                return False, "A helper is already booked for this day."
            conn.execute("INSERT INTO helpers(pickup_date,booked_on,cost,extra_hours) VALUES(?,?,?,?)",
                         (pickup, booked_on, HELPER_COST, HELPER_HOURS))
            return True, "Helper booked: 4 extra hours for $100."
        if action == "remove":
            if not existing:
                return False, "No helper is booked for this day."
            conn.execute("DELETE FROM helpers WHERE pickup_date=?", (pickup,))
            return True, "Helper booking removed."
    return False, "Unknown helper action."


def accept_day(message_date: str) -> tuple[bool, str]:
    try:
        date.fromisoformat(message_date)
    except ValueError:
        return False, "Choose a valid working date."
    with connect() as conn:
        request_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM requests WHERE message_date=? AND status='pending' ORDER BY id", (message_date,)
        )]
    if not request_ids:
        return True, "No pending requests for this day. Moved to the next day."
    accepted = 0
    rejected = 0
    for request_id in request_ids:
        with connect() as conn:
            req = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            evaluation = evaluate_request(conn, req)
            if not evaluation["ok"]:
                conn.execute(
                    """UPDATE requests SET status='rejected',decision_note=?,decided_at=CURRENT_TIMESTAMP
                    WHERE id=?""",
                    (f"Automatic rule rejection: {evaluation['reason']}", request_id),
                )
                rejected += 1
                continue
        ok, _ = decide_request(request_id, "accept")
        accepted += 1 if ok else 0
    return True, f"Processed {accepted}; automatically rejected {rejected}. Moved to the next day."


def mark_picked_up(order_id: int) -> tuple[bool, str]:
    with connect() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return False, "Order not found."
        if order["status"] == "cancelled":
            return False, "A cancelled order cannot be picked up."
        if order["picked_up_at"]:
            return False, "This order is already marked picked up."
        conn.execute("UPDATE orders SET picked_up_at=CURRENT_TIMESTAMP WHERE id=?", (order_id,))
        return True, f"{order['order_number']} marked picked up."


# ---------------------------------------------------------------------------
# PART 3: FRONT END — pages, facts, and one clear button per action
# ---------------------------------------------------------------------------
PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flour & Time · Bakery Order Tracker</title>
<style>
:root{--ink:#241f1c;--muted:#6f6762;--paper:#fffdfa;--cream:#f7f0e7;--line:#e6d9cc;--berry:#9c3559;--berry2:#70233f;--sage:#2f755f;--gold:#bd7a22;--red:#b93b35;--shadow:0 14px 34px rgba(82,55,38,.10)}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#fbf6ef,#f3e7df);color:var(--ink);font:16px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh}
button,input{font:inherit}button{cursor:pointer}.shell{max-width:1480px;margin:auto;padding:24px}.top{display:flex;align-items:center;gap:18px;background:rgba(255,253,250,.92);border:1px solid var(--line);border-radius:24px;padding:18px 22px;box-shadow:var(--shadow);position:sticky;top:12px;z-index:5;backdrop-filter:blur(12px)}
.brand{display:flex;align-items:center;gap:12px}.mark{width:48px;height:48px;border-radius:16px;background:var(--berry);color:white;display:grid;place-items:center;font-size:25px}.brand h1{font-family:Georgia,serif;font-size:24px;margin:0}.brand p{font-size:13px;color:var(--muted);margin:0}.nav{display:flex;gap:6px;margin-left:auto}.nav button{border:0;background:transparent;color:var(--muted);padding:10px 14px;border-radius:12px;font-weight:700}.nav button.active{background:var(--cream);color:var(--berry2)}
.workday{display:flex;align-items:center;gap:8px;margin-left:8px;font-size:14px;color:var(--muted)}.workday input{border:1px solid var(--line);border-radius:10px;padding:8px;background:white;color:var(--ink)}main{padding:28px 0 40px}.hero{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:22px}.eyebrow{color:var(--berry);font-size:13px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.hero h2{font-family:Georgia,serif;font-size:clamp(30px,4vw,48px);line-height:1.08;margin:5px 0}.hero p{color:var(--muted);margin:0;max-width:650px}.readme{color:var(--berry2);font-weight:800;text-decoration:none;border-bottom:2px solid #d7a2b5}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0}.stat,.panel,.day,.request,.rules-card{background:var(--paper);border:1px solid var(--line);border-radius:20px;box-shadow:0 8px 24px rgba(82,55,38,.06)}.stat{padding:18px}.stat span{color:var(--muted);font-size:14px}.stat strong{display:block;font:700 30px Georgia,serif;margin-top:4px}
.section-head{display:flex;justify-content:space-between;align-items:center;margin:28px 2px 12px}.section-head h3{font:700 24px Georgia,serif;margin:0}.section-head p{margin:0;color:var(--muted);font-size:14px}.calendar{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.day{padding:17px;min-height:210px}.day-head{display:flex;justify-content:space-between;gap:10px}.day h4{margin:0;font-size:17px}.date{color:var(--muted);font-size:13px}.helper{font-size:12px;font-weight:800;color:var(--sage);background:#e4f2ed;padding:4px 8px;border-radius:99px}.meter{height:9px;background:#eee3d8;border-radius:99px;overflow:hidden;margin:15px 0 8px}.meter i{display:block;height:100%;background:var(--sage);border-radius:99px}.meter i.warn{background:var(--gold)}.capacity-line{display:flex;justify-content:space-between;color:var(--muted);font-size:13px}.mini-orders{list-style:none;padding:0;margin:14px 0}.mini-orders li{display:flex;justify-content:space-between;gap:8px;border-top:1px dashed var(--line);padding:8px 0;font-size:13px}.helper-btn{width:100%;border:1px solid var(--line);background:white;color:var(--berry2);padding:8px;border-radius:10px;font-weight:750}
.inbox-tools,.action-bar{display:flex;gap:10px;flex-wrap:wrap}.action-bar{margin:0 0 14px}.filter,.tool-btn{border:1px solid var(--line);background:white;border-radius:99px;padding:8px 13px;color:var(--muted);font-weight:700}.filter.active{background:var(--berry);color:#fff;border-color:var(--berry)}.tool-btn{border-radius:12px;color:var(--berry2)}.tool-btn.primary{background:var(--berry);color:white;border-color:var(--berry)}.request-list{display:grid;gap:12px}.request{display:grid;grid-template-columns:150px 1fr auto;gap:18px;padding:18px;align-items:center}.request-meta{color:var(--muted);font-size:13px}.request-meta b{display:block;color:var(--ink);font-size:16px}.request h4{margin:0 0 4px;font-size:18px}.request p{margin:0;color:var(--muted)}.status{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:850;text-transform:uppercase;letter-spacing:.05em;border-radius:99px;padding:5px 9px}.ok{background:#e4f2ed;color:var(--sage)}.bad{background:#f9e5e2;color:var(--red)}.done{background:#eee8f2;color:#665070}.actions{display:flex;gap:8px}.actions button{border:0;border-radius:11px;padding:10px 13px;font-weight:800}.primary{background:var(--berry);color:white}.secondary{background:var(--cream);color:var(--berry2)}button:disabled{opacity:.45;cursor:not-allowed}
.panel{overflow:auto}.orders{width:100%;border-collapse:collapse;min-width:760px}.orders th,.orders td{text-align:left;padding:14px 16px;border-bottom:1px solid var(--line)}.orders th{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);background:#fcf8f3}.orders tr:last-child td{border:0}.cancelled{text-decoration:line-through;color:var(--muted)}.money{font-variant-numeric:tabular-nums}
.rules-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.rules-card{padding:20px}.rules-card .icon{font-size:28px}.rules-card h4{margin:8px 0 4px}.rules-card p{color:var(--muted);margin:0;font-size:14px}.hidden{display:none!important}.toast{position:fixed;right:24px;bottom:24px;background:var(--ink);color:white;padding:13px 17px;border-radius:12px;box-shadow:var(--shadow);z-index:20;max-width:360px}.empty{padding:34px;text-align:center;color:var(--muted)}
@media(max-width:900px){.top{align-items:flex-start;flex-wrap:wrap}.nav{order:3;width:100%;overflow:auto;margin:0}.workday{margin-left:auto}.stats{grid-template-columns:repeat(2,1fr)}.request{grid-template-columns:1fr}.actions{justify-content:flex-start}.rules-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.shell{padding:12px}.top{top:6px}.brand p{display:none}.workday{width:100%;margin:0;justify-content:space-between}.hero{display:block}.stats{grid-template-columns:1fr 1fr}.stat strong{font-size:24px}.rules-grid{grid-template-columns:1fr}.nav button{padding:8px 10px}.request{padding:15px}}
</style></head>
<body><div class="shell">
<header class="top"><div class="brand"><div class="mark">🧁</div><div><h1>Flour & Time</h1><p>Bakery order tracker</p></div></div><nav class="nav">
<button data-view="dashboard" class="active">Dashboard</button><button data-view="inbox">DM inbox</button><button data-view="orders">Orders</button><button data-view="rules">Rules</button></nav>
<label class="workday">Working date <input id="workDate" type="date" value="2026-11-23"></label></header>
<main><section class="hero"><div><div class="eyebrow">Week of 23 November 2026</div><h2 id="viewTitle">A clear view of every bake.</h2><p id="viewIntro">Plan oven time, process Instagram requests, and keep every pickup on track.</p></div><a class="readme" href="/README.html" target="_blank">How this app works</a></section>

<section id="dashboard" class="view"><div id="stats" class="stats"></div><div class="section-head"><div><h3>Oven plan</h3><p>All baking happens on pickup day. Pickup is at 5 PM.</p></div></div><div id="calendar" class="calendar"></div></section>

<section id="inbox" class="view hidden"><div class="section-head"><div><h3>Instagram requests</h3><p>Fixed rules check notice and capacity. You make the decision.</p></div><div class="inbox-tools"><button class="filter active" data-filter="pending">Pending</button><button class="filter" data-filter="all">All</button></div></div><div class="action-bar"><button class="tool-btn" onclick="fetchDMs()">Fetch new DMs</button><button class="tool-btn primary" onclick="acceptDay()">Accept compatible requests · next day</button></div><div id="requests" class="request-list"></div></section>

<section id="orders" class="view hidden"><div class="section-head"><div><h3>Order book</h3><p>Accepted orders, changes, cancellations, and pickups in one place.</p></div></div><div class="panel"><table class="orders"><thead><tr><th>Order</th><th>Customer</th><th>Product</th><th>Pickup</th><th>Oven</th><th>Revenue</th><th>Status</th><th>Action</th></tr></thead><tbody id="orderRows"></tbody></table></div></section>

<section id="rules" class="view hidden"><div class="section-head"><div><h3>Bakery rules</h3><p>Every recommendation in the app comes from these fixed rules.</p></div></div><div class="rules-grid">
<article class="rules-card"><div class="icon">⏱️</div><h4>Two days’ notice</h4><p>New orders, changes, cancellations, and helper bookings need at least two days’ notice.</p></article>
<article class="rules-card"><div class="icon">🔥</div><h4>Eight oven hours</h4><p>The oven has 8 hours each day. Every item is baked on its pickup day.</p></article>
<article class="rules-card"><div class="icon">🧁</div><h4>Cupcakes</h4><p>One dozen costs $40 and uses 30 minutes of oven time.</p></article>
<article class="rules-card"><div class="icon">🎂</div><h4>Cakes</h4><p>One cake costs $120 and uses 2 hours of oven time.</p></article>
<article class="rules-card"><div class="icon">🤝</div><h4>Optional helper</h4><p>A helper costs $100 for the day and adds 4 hours of capacity.</p></article>
<article class="rules-card"><div class="icon">✅</div><h4>Baker decides</h4><p>The app recommends what fits, but the baker accepts or rejects each new order.</p></article>
<article class="rules-card"><div class="icon">📦</div><h4>One product per order</h4><p>Each order contains cupcakes or cake, never both.</p></article>
<article class="rules-card"><div class="icon">🕔</div><h4>Pickup time</h4><p>Customers pick up completed orders at 5 PM.</p></article></div></section>
</main></div><div id="toast" class="toast hidden"></div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];let state=null,filter='pending';
const money=n=>new Intl.NumberFormat('en-CA',{style:'currency',currency:'CAD',maximumFractionDigits:0}).format(n);
const pretty=d=>new Date(d+'T12:00:00').toLocaleDateString('en-CA',{weekday:'short',month:'short',day:'numeric'});
const item=o=>o.product==='cupcakes'?`${o.quantity} dozen cupcakes`:`${o.quantity} cake${o.quantity>1?'s':''}`;
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.style.background=error?'#8f2f2a':'#241f1c';el.classList.remove('hidden');setTimeout(()=>el.classList.add('hidden'),3200)}
async function api(path,options={}){const res=await fetch(path,{headers:{'Content-Type':'application/json'},...options});const data=await res.json();if(!res.ok)throw new Error(data.error||'Something went wrong');return data}
async function load(){state=await api('/api/state');render()}
function render(){renderStats();renderCalendar();renderRequests();renderOrders()}
function renderStats(){const s=state.summary;$('#stats').innerHTML=`<article class="stat"><span>Pending DMs</span><strong>${s.pending}</strong></article><article class="stat"><span>Active orders</span><strong>${s.active_orders}</strong></article><article class="stat"><span>Order revenue</span><strong>${money(s.revenue)}</strong></article><article class="stat"><span>Helper cost</span><strong>${money(s.helper_cost)}</strong></article>`}
function renderCalendar(){$('#calendar').innerHTML=state.days.map(d=>{const pct=Math.min(100,d.hours/d.capacity*100);return `<article class="day"><div class="day-head"><div><h4>${pretty(d.date)}</h4><div class="date">${money(d.revenue)} orders</div></div>${d.helper?'<span class="helper">Helper +4h</span>':''}</div><div class="meter"><i class="${pct>85?'warn':''}" style="width:${pct}%"></i></div><div class="capacity-line"><span>${d.hours} hours used</span><span>${d.capacity} available</span></div><ul class="mini-orders">${d.orders.length?d.orders.map(o=>`<li><span>${o.customer}</span><b>${item(o)}</b></li>`).join(''):'<li><span>No accepted orders</span></li>'}</ul><button class="helper-btn" onclick="helper('${d.date}',${d.helper?'true':'false'})">${d.helper?'Remove helper':'Book helper · $100'}</button></article>`}).join('')}
function renderRequests(){let rows=state.requests.filter(r=>filter==='all'||r.status==='pending');$('#requests').innerHTML=rows.length?rows.map(r=>{const e=r.evaluation;const badge=r.status==='pending'?(e.ok?'<span class="status ok">Ready to take</span>':'<span class="status bad">Rule conflict</span>'):`<span class="status done">${r.status}</span>`;const verb=r.action==='new'?'New order':r.action==='change'?'Change':'Cancellation';const button=r.action==='new'?'Accept':r.action==='change'?'Apply change':'Apply cancellation';return `<article class="request"><div class="request-meta"><b>${r.customer}</b>@${r.username}<br>${pretty(r.message_date)} · ${r.message_time}</div><div><div>${badge}</div><h4>${verb}: ${r.item_label}</h4><p>Pickup ${pretty(r.pickup_date)} · ${e?e.reason:(r.decision_note||'Decision recorded.')}</p></div><div class="actions">${r.status==='pending'?`<button class="secondary" onclick="decide(${r.id},'reject')">Reject</button><button class="primary" ${e&&!e.ok?'disabled':''} onclick="decide(${r.id},'accept')">${button}</button>`:''}</div></article>`}).join(''):'<div class="panel empty">No requests in this view.</div>'}
function renderOrders(){$('#orderRows').innerHTML=state.orders.map(o=>`<tr class="${o.status==='cancelled'?'cancelled':''}"><td><b>${o.order_number}</b></td><td>${o.customer}</td><td>${item(o)}</td><td>${pretty(o.pickup_date)} · ${o.pickup_time}</td><td>${o.product==='cupcakes'?o.quantity*.5:o.quantity*2}h</td><td class="money">${money(o.product==='cupcakes'?o.quantity*40:o.quantity*120)}</td><td><span class="status ${o.display_status==='accepted'?'ok':'done'}">${o.display_status}</span></td><td>${o.display_status==='accepted'?`<button class="tool-btn" onclick="pickedUp(${o.id})">Mark picked up</button>`:'—'}</td></tr>`).join('')}
async function decide(id,decision){try{const out=await api('/api/request/decision',{method:'POST',body:JSON.stringify({id,decision})});toast(out.message);await load()}catch(e){toast(e.message,true)}}
async function helper(pickup,exists){const booked_on=$('#workDate').value;if(exists&&!confirm(`Remove the helper booked for ${pretty(pickup)}?`))return;try{const out=await api('/api/helper',{method:'POST',body:JSON.stringify({pickup_date:pickup,booked_on,action:exists?'remove':'book'})});toast(out.message);await load()}catch(e){toast(e.message,true)}}
async function fetchDMs(){try{const out=await api('/api/fetch',{method:'POST',body:'{}'});toast(out.message);await load()}catch(e){toast(e.message,true)}}
async function acceptDay(){const day=$('#workDate').value;try{const out=await api('/api/accept-day',{method:'POST',body:JSON.stringify({message_date:day})});const next=new Date(day+'T12:00:00');next.setDate(next.getDate()+1);$('#workDate').value=next.toISOString().slice(0,10);localStorage.setItem('bakeryWorkDate',$('#workDate').value);toast(out.message);await load()}catch(e){toast(e.message,true)}}
async function pickedUp(id){try{const out=await api('/api/order/picked-up',{method:'POST',body:JSON.stringify({id})});toast(out.message);await load()}catch(e){toast(e.message,true)}}
$$('.nav button').forEach(b=>b.onclick=()=>{$$('.nav button').forEach(x=>x.classList.toggle('active',x===b));$$('.view').forEach(v=>v.classList.toggle('hidden',v.id!==b.dataset.view));const copy={dashboard:['A clear view of every bake.','Plan oven time, process Instagram requests, and keep every pickup on track.'],inbox:['Turn DMs into decisions.','Each message is checked against notice and capacity before you act.'],orders:['Your full order book.','See the product, pickup, oven time, revenue, and status for every order.'],rules:['Simple rules. Reliable decisions.','No AI is used: every check is a transparent calculation.']}[b.dataset.view];$('#viewTitle').textContent=copy[0];$('#viewIntro').textContent=copy[1]});
$$('.filter').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;$$('.filter').forEach(x=>x.classList.toggle('active',x===b));renderRequests()});
$('#workDate').value=localStorage.getItem('bakeryWorkDate')||'2026-11-23';$('#workDate').onchange=e=>localStorage.setItem('bakeryWorkDate',e.target.value);load().catch(e=>toast(e.message,true));
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, content: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._text(PAGE)
        elif path == "/README.html":
            self._text((ROOT / "README.html").read_text(encoding="utf-8"))
        elif path == "/api/state":
            self._json(serialize_state())
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/request/decision":
                ok, message = decide_request(int(payload.get("id", 0)), payload.get("decision", ""))
            elif path == "/api/helper":
                ok, message = update_helper(payload)
            elif path == "/api/fetch":
                ok, message = fetch_external_data()
            elif path == "/api/accept-day":
                ok, message = accept_day(payload.get("message_date", ""))
            elif path == "/api/order/picked-up":
                ok, message = mark_picked_up(int(payload.get("id", 0)))
            else:
                self._json({"error": "Not found"}, 404)
                return
            self._json({"message": message} if ok else {"error": message}, 200 if ok else 400)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": f"Invalid request: {exc}"}, 400)
        except Exception as exc:
            self._json({"error": f"Server error: {exc}"}, 500)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    global PORT
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
    initialize()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Bakery tracker is running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
