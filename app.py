from flask import Flask, render_template, request, redirect, url_for
from flask import session, jsonify, flash, send_file, send_from_directory

import pymysql
import pymysql.cursors
import requests as _requests
import base64 as _base64


from flask_wtf.csrf import CSRFProtect, generate_csrf, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import qrcode
import uuid
import os
import secrets
import functools

from io import BytesIO
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont
from PIL import UnidentifiedImageError

# ======================================================
# FLASK APP
# ======================================================

app = Flask(__name__)

# ======================================================
# DEBUG / ENVIRONMENT MODE
# ======================================================
# This is the ONLY place debug mode is controlled. Never hardcode True.
# Set FLASK_DEBUG=1 only on your own machine — never on a deployed server.
DEBUG = os.environ.get("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes")


def _require_env(name):
    """Fetch a required secret from the environment, or fail fast on boot."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it before starting the app (e.g. in your .env / host's "
            f"environment settings)."
        )
    return val


# ======================================================
# SECRET KEY
# ======================================================
# Required in production. Only in local debug mode do we fall back to a
# random throwaway key (sessions just won't survive a restart).
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    if DEBUG:
        app.secret_key = secrets.token_hex(32)
        app.logger.warning(
            "FLASK_SECRET_KEY not set — using a random throwaway key for "
            "this process only. Set FLASK_SECRET_KEY in your environment "
            "for staging/production."
        )
    else:
        raise RuntimeError(
            "FLASK_SECRET_KEY environment variable is required outside "
            "debug mode. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )

# ======================================================
# SESSION COOKIE HARDENING
# ======================================================
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not DEBUG,   # requires HTTPS in production
    WTF_CSRF_TIME_LIMIT=None,          # token lifetime tracks the session, not a fixed window
)

# ======================================================
# CSRF PROTECTION (Flask-WTF)
# ======================================================
# Protects every state-changing request (POST/PUT/PATCH/DELETE) by default.
# Form-based pages need {{ csrf_token() }} as a hidden field (Flask-WTF
# injects this Jinja helper automatically once CSRFProtect is initialized).
# AJAX/fetch() calls need the token sent as an "X-CSRFToken" header instead —
# see the note further down near the JSON API routes for the front-end change
# this requires.
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def _handle_csrf_error(e):
    return jsonify({"success": False, "error": "Your session expired or the "
                     "request could not be verified. Please refresh and try again."}), 400


def _json_safe(default_response):
    """
    Decorator for fetch()-driven JSON endpoints (promo/referral/cart AJAX
    routes). Without this, an unhandled exception anywhere inside the view
    (a bad DB row, a type mismatch, a dropped connection, etc.) falls
    through to Flask's default error page, which is HTML — not JSON. The
    frontend's `await res.json()` then throws a parse error, which lands in
    its generic `catch` block and shows a misleading "Failed to validate.
    Check your connection." message even when the network is fine and the
    real problem is server-side.

    This wraps the view so it ALWAYS returns valid JSON: the real exception
    is logged server-side (check the logs to find the actual cause), while
    the client gets an honest, parseable error instead of a broken response.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                app.logger.exception(
                    f"Unhandled error in {fn.__name__} ({request.path}): {e}"
                )
                body = dict(default_response)
                return jsonify(body), 500
        return inner
    return decorator


# ======================================================
# RATE LIMITING (Flask-Limiter)
# ======================================================
# Generous global default; tighter limits are applied per-route below on
# the endpoints that matter most (login, payments, uploads).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://",   # swap for redis:// in a multi-instance deployment
)


@app.teardown_appcontext
def _close_db(exc=None):
    conn = _g.pop("db_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass

# ======================================================
# MYSQL CONFIG
# ======================================================

MYSQL_HOST     = _require_env("MYSQL_HOST")
MYSQL_USER     = _require_env("MYSQL_USER")
MYSQL_PORT     = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_PASSWORD = _require_env("MYSQL_PASSWORD")
MYSQL_DB       = _require_env("MYSQL_DB")

from flask import g as _g

def get_db():
    """Return a per-request pymysql connection (stored on Flask g)."""
    if "db_conn" not in _g:
        _g.db_conn = pymysql.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            port=MYSQL_PORT,
            password=MYSQL_PASSWORD,
            db=MYSQL_DB,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            charset="utf8mb4"
        )
    return _g.db_conn

class _MySQLCompat:
    """Shim so existing mysql.connection calls work unchanged."""
    @property
    def connection(self):
        return get_db()

mysql = _MySQLCompat()

# ======================================================
# BREVO (Sendinblue) EMAIL CONFIG
# Uses HTTP API — works on Render (no SMTP ports needed)
# Get your API key from: https://app.brevo.com/settings/keys/api
# ======================================================

BREVO_API_KEY    = _require_env("BREVO_API_KEY")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "noreplyanchoragecusat@gmail.com")
BREVO_SENDER_NAME  = os.environ.get("BREVO_SENDER_NAME", "Anchorage 2026")
BREVO_API_URL    = "https://api.brevo.com/v3/smtp/email"

from seo_routes import register_seo_routes
register_seo_routes(app)


def _brevo_send(to_email, to_name, subject, html_body, text_body,
                 attachment_bytes=None, attachment_filename=None, attachments=None):
    """
    Send an email via Brevo's HTTP API.
    attachment_bytes/attachment_filename: single attachment (legacy — still supported).
    attachments: optional list of {"bytes": <raw bytes>, "filename": <str>} dicts, used
        to send MULTIPLE attachments (e.g. several event tickets) in a single email.
    Returns True on success, raises RuntimeError on failure.
    Works on Render — uses HTTPS (port 443), no SMTP needed.
    """
    payload = {
        "sender":      {"email": BREVO_SENDER_EMAIL, "name": BREVO_SENDER_NAME},
        "to":          [{"email": to_email, "name": to_name}],
        "subject":     subject,
        "htmlContent": html_body,
        "textContent": text_body,
    }

    attach_list = []
    if attachments:
        for a in attachments:
            a_bytes = a.get("bytes")
            a_name  = a.get("filename")
            if a_bytes and a_name:
                attach_list.append({
                    "content": _base64.b64encode(a_bytes).decode("utf-8"),
                    "name":    a_name,
                })
    if attachment_bytes and attachment_filename:
        attach_list.append({
            "content": _base64.b64encode(attachment_bytes).decode("utf-8"),
            "name":    attachment_filename,
        })

    if attach_list:
        payload["attachment"] = attach_list

    headers = {
        "accept":       "application/json",
        "content-type": "application/json",
        "api-key":      BREVO_API_KEY,
    }

    resp = _requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=15)

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Brevo API error {resp.status_code}: {resp.text}"
        )
    return True

# ======================================================
# TIQR EVENTS — payment gateway
# ======================================================
# Public (unauthenticated) booking flow only. See:
#   POST /participant/booking/          - create a single booking
#   POST /participant/booking/bulk      - create multiple bookings (cart)
#   GET  /participant/booking/:uid/     - fetch booking details
# TiQR calls TIQR_WEBHOOK-configured URL on our side when a booking's
# payment status changes; see /tiqr-webhook below.
#
# NOTE: cart checkout uses a custom-amount-enabled ticket (see
# CUSTOM_AMOUNT_TICKET_ID / /create-order below), which DOES let us pass
# a discounted rupee amount to TiQR via custom_amount — the promo total
# computed for our own bookkeeping is also what gets charged. (The
# create-ticket admin endpoint further down, admin_create_tiqr_ticket,
# still creates plain fixed-price tickets with no custom-amount field —
# that path has no promo support and isn't used by the cart checkout.)
TIQR_BASE_URL = os.environ.get("TIQR_BASE_URL", "https://api.tiqr.events")

# Convenience fee kept for reporting/UI purposes only — not currently
# added into custom_amount below, so it is NOT transmitted to TiQR.
CONVENIENCE_FEE_PERCENT = 2.36


def apply_convenience_fee(amount_rupees):
    """Returns (convenience_fee, total_with_fee) rounded to 2 decimal places."""
    fee = round(amount_rupees * CONVENIENCE_FEE_PERCENT / 100, 2)
    return fee, round(amount_rupees + fee, 2)


def _tiqr_post(path, json_body, timeout=15):
    """POST helper for TiQR's public booking endpoints. Raises on network
    error or non-2xx; caller is responsible for catching and translating
    into a user-facing error."""
    resp = _requests.post(f"{TIQR_BASE_URL}{path}", json=json_body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _tiqr_get(path, timeout=15):
    resp = _requests.get(f"{TIQR_BASE_URL}{path}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ======================================================
# TiQR ORGANISER API — create events/tickets from code
# ======================================================
# NOTE: TIQR_SESSION_ID below defaults to "2039", which the user believes
# may be their session_id. This is UNVERIFIED — 2039 was previously
# identified as the Hydro Clash *ticket* ID, a completely different value
# from an organiser session_id. If get_tiqr_access_token() below raises
# an auth error (401/403), that confirms 2039 is not a valid session_id
# and a real one needs to be obtained from TiQR support/onboarding email.
TIQR_SESSION_ID = os.environ.get("TIQR_SESSION_ID", "2039")

_tiqr_access_token_cache = {"token": None}


def get_tiqr_access_token(force_refresh=False):
    """Exchanges TIQR_SESSION_ID for a Bearer access_token. Caches in memory
    for the life of the process (token is valid ~30 days per TiQR docs).
    Raises requests.HTTPError if the session_id is invalid."""
    if _tiqr_access_token_cache["token"] and not force_refresh:
        return _tiqr_access_token_cache["token"]

    resp = _requests.post(
        f"{TIQR_BASE_URL}/participant/booking/custom-token/",
        json={"session_id": TIQR_SESSION_ID},
        timeout=15,
    )
    resp.raise_for_status()  # will raise here if TIQR_SESSION_ID is invalid
    token = resp.json()["access_token"]
    _tiqr_access_token_cache["token"] = token
    return token


def create_tiqr_ticket(tiqr_event_id, ticket_name, price_rupees, limit=100):
    """Creates a ticket under an existing TiQR event via the organiser API.
    Returns the full TiQR response dict; response['id'] is the
    tiqr_ticket_id to store on the matching row in the local `events` table.
    Raises requests.HTTPError on failure (e.g. bad token, bad event id)."""
    token = get_tiqr_access_token()
    resp = _requests.post(
        f"{TIQR_BASE_URL}/organiser/event/{tiqr_event_id}/ticket/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": ticket_name,
            "description": f"{ticket_name} entry ticket",
            "amount": int(price_rupees * 100),  # TiQR expects paise
            "gst_on_ticket": 0,
            "fee_paid_by_buyer": True,
            "allow_bulk_booking": True,
            "allow_waitlist": False,
            "has_limit": True,
            "limit": limit,
            "minimum_booking": 1,
            "maximum_booking": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


@app.route("/admin/tiqr/create-ticket", methods=["POST"])
def admin_create_tiqr_ticket():
    """One-off admin endpoint: POST tiqr_event_id, ticket_name, price_rupees,
    local_event_id (and optionally limit) as JSON. Creates the ticket on
    TiQR and writes tiqr_ticket_id + fee back onto the local events row.
    Meant to be called manually (curl/Postman) while testing — not wired
    into any UI yet. Protected using this app's existing admin-session
    check (_admin_auth), same as other /admin/* routes."""
    if not _admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    tiqr_event_id = data.get("tiqr_event_id")
    ticket_name = data.get("ticket_name")
    price_rupees = data.get("price_rupees")
    local_event_id = data.get("local_event_id")
    limit = data.get("limit", 100)

    if not all([tiqr_event_id, ticket_name, price_rupees, local_event_id]):
        return jsonify({"error": "tiqr_event_id, ticket_name, price_rupees, "
                                  "local_event_id are all required"}), 400

    try:
        ticket = create_tiqr_ticket(tiqr_event_id, ticket_name, price_rupees, limit)
    except _requests.HTTPError as e:
        # Most likely cause right now: TIQR_SESSION_ID ("2039") is not a
        # real session_id and TiQR is rejecting the token exchange.
        return jsonify({
            "error": "TiQR API call failed",
            "detail": str(e),
            "hint": "If this is a 401/403, TIQR_SESSION_ID is invalid — "
                    "confirm the real session_id with TiQR support.",
        }), 502

    ticket_id = ticket.get("id")

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE events SET tiqr_ticket_id = %s, fee = %s WHERE id = %s",
        (ticket_id, price_rupees, local_event_id),
    )
    db.commit()

    return jsonify({
        "status": "ok",
        "tiqr_ticket_id": ticket_id,
        "local_event_id": local_event_id,
        "raw_response": ticket,
    })

# ======================================================
# ALTER TABLE — run these once in MySQL to add missing columns
# ======================================================
# ALTER TABLE events
#     ADD COLUMN event_date DATE    NOT NULL DEFAULT '2026-01-10',
#     ADD COLUMN event_time TIME    NOT NULL DEFAULT '00:00:00';
#
# After running, you can update individual events:
#   UPDATE events SET event_date='2026-01-10', event_time='09:00:00' WHERE id=1;
# ======================================================

# ======================================================
# TIQR TICKET MAPPING — run once in MySQL
# ======================================================
# Each local event needs to know which TiQR "ticket" ID to book against.
# ALTER TABLE events
#     ADD COLUMN tiqr_ticket_id INT NULL;
#
# Then for every paid event:
#   UPDATE events SET tiqr_ticket_id = <id from TiQR dashboard> WHERE id = <event_id>;
#
# Free events (price = 0) can leave this NULL — free registrations bypass
# TiQR entirely, same as they bypassed Razorpay before.
# ======================================================

# ======================================================
# TIQR BOOKINGS — run once in MySQL to create tracking table
# ======================================================
# TiQR's webhook only tells us a booking_uid + status — it does NOT echo
# back who the user was, which event(s) they registered for, or their
# team members. This table is OUR local record of what a booking_uid
# means, written when we create the booking (before redirecting to
# payment) and read back when the webhook confirms/fails it.
#
# CREATE TABLE IF NOT EXISTS tiqr_bookings (
#     id               INT AUTO_INCREMENT PRIMARY KEY,
#     booking_uid      VARCHAR(64) NOT NULL UNIQUE,
#     cart_group_id    VARCHAR(64) NOT NULL,   -- shared by all bookings from one checkout
#     user_id          INT NOT NULL,
#     event_id         INT NOT NULL,           -- ONE event per row, even for multi-event carts
#     status           VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending / confirmed / failed
#     payload_json      TEXT NOT NULL,          -- participant + members + promo snapshot for this event
#     created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
# );
# CREATE INDEX idx_tiqr_bookings_uid   ON tiqr_bookings(booking_uid);
# CREATE INDEX idx_tiqr_bookings_group ON tiqr_bookings(cart_group_id);
# ======================================================

# ======================================================
# TICKET CAP — run once in MySQL to add cap columns
# ======================================================
# ALTER TABLE events
#     ADD COLUMN max_tickets  INT DEFAULT NULL,   -- NULL = unlimited
#     ADD COLUMN tickets_sold INT NOT NULL DEFAULT 0;
#
# -- Set a cap per event:
#   UPDATE events SET max_tickets=200 WHERE id=1;
#   UPDATE events SET max_tickets=100 WHERE id=2;
#
# -- Check remaining seats:
#   SELECT title, max_tickets, tickets_sold,
#          COALESCE(max_tickets - tickets_sold, 'unlimited') AS remaining
#   FROM events;
#
# -- Reset sold count (e.g. after testing):
#   UPDATE events SET tickets_sold=0 WHERE id=1;
# ======================================================

# ======================================================
# GUEST CHECKOUT — run once in MySQL (login/register removed)
# ======================================================
# Accounts are gone — `users` rows are now created automatically as
# lightweight guest placeholders (see _get_or_create_user_id) and no
# longer carry a password. If `password` is NOT NULL on your table,
# relax it so the guest INSERT succeeds:
#
# ALTER TABLE users
#     MODIFY COLUMN password VARCHAR(255) NULL,
#     MODIFY COLUMN email    VARCHAR(255) NULL,
#     MODIFY COLUMN phone    VARCHAR(30)  NULL;
#
# reset_token / reset_token_expires (added below) are no longer read or
# written anywhere and can be dropped if you want to clean up:
#   ALTER TABLE users DROP COLUMN reset_token, DROP COLUMN reset_token_expires;
# ======================================================

# ======================================================
# PROMO CODE — SQL TO CREATE TABLE (run once in MySQL)
# ======================================================
# CREATE TABLE promo_codes (
#     id             INT AUTO_INCREMENT PRIMARY KEY,
#     code           VARCHAR(50) UNIQUE NOT NULL,
#     discount_type  ENUM('flat','percent') NOT NULL DEFAULT 'flat',
#     discount_value DECIMAL(10,2) NOT NULL DEFAULT 0,
#     max_uses       INT DEFAULT NULL,       -- NULL = unlimited
#     used_count     INT DEFAULT 0,
#     applies_to     ENUM('all','specific') NOT NULL DEFAULT 'all',
#     event_ids      TEXT DEFAULT NULL,      -- comma-separated event IDs when applies_to='specific'
#     expires_at     DATE DEFAULT NULL,
#     is_active      TINYINT(1) DEFAULT 1,
#     created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# );
#
# -- MANAGE CODES --
# Create 100% off (one-time, all events):
#   INSERT INTO promo_codes (code,discount_type,discount_value,max_uses,applies_to)
#   VALUES ('FULLPASS','percent',100,1,'all');
#
# Create 20% off (50 uses, all events):
#   INSERT INTO promo_codes (code,discount_type,discount_value,max_uses,applies_to)
#   VALUES ('EARLY20','percent',20,50,'all');
#
# Flat ₹100 off only for event IDs 5 and 7:
#   INSERT INTO promo_codes (code,discount_type,discount_value,max_uses,applies_to,event_ids)
#   VALUES ('PAPER100','flat',100,NULL,'specific','5,7');
#
# Deactivate:  UPDATE promo_codes SET is_active=0 WHERE code='EARLY20';
# Change amt:  UPDATE promo_codes SET discount_value=150 WHERE code='PAPER100';
# Change uses: UPDATE promo_codes SET max_uses=25 WHERE code='EARLY20';
# Reset count: UPDATE promo_codes SET used_count=0 WHERE code='EARLY20';
# View all:    SELECT code,discount_type,discount_value,max_uses,used_count,applies_to,event_ids FROM promo_codes;
# ======================================================

# ======================================================
# TEAM MEMBERS TABLE — run once in MySQL
# ======================================================
# CREATE TABLE IF NOT EXISTS registration_members (
#     id                  INT AUTO_INCREMENT PRIMARY KEY,
#     registration_code   VARCHAR(50) NOT NULL,
#     member_name         VARCHAR(255) NOT NULL,
#     member_email        VARCHAR(255) DEFAULT NULL,
#     member_phone        VARCHAR(30)  DEFAULT NULL,
#     member_college      VARCHAR(255) DEFAULT NULL,
#     member_dept         VARCHAR(100) DEFAULT NULL,
#     member_order        INT DEFAULT 0,
#     created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#     FOREIGN KEY (registration_code)
#         REFERENCES registrations(registration_code) ON DELETE CASCADE
# );
# CREATE INDEX idx_rm_code ON registration_members(registration_code);
# ======================================================

# ======================================================
# PROMO CODE HELPERS
# ======================================================

import datetime as _dt

def _validate_promo_code(code, cart_event_ids):
    """
    Validates a promo code against the DB.
    Returns (promo_dict, error_message).
    promo_dict contains: code, type, value, applies_to, applicable_ids, discount_amount
    cart_event_ids: list of int event IDs in the cart
    """
    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM promo_codes WHERE code=%s AND is_active=1",
        (code.strip().upper(),)
    )
    promo = cursor.fetchone()
    cursor.close()

    if not promo:
        return None, "Invalid promo code."

    if promo["expires_at"] and promo["expires_at"] < _dt.date.today():
        return None, "This promo code has expired."

    if promo["max_uses"] is not None and promo["used_count"] >= promo["max_uses"]:
        return None, "This promo code has reached its usage limit."

    # Determine which event IDs this code applies to
    if promo["applies_to"] == "specific":
        raw_ids = [x.strip() for x in (promo["event_ids"] or "").split(",") if x.strip()]
        applicable_ids = [int(x) for x in raw_ids if x.isdigit()]
        # Filter cart to only applicable events
        eligible_ids = [eid for eid in cart_event_ids if eid in applicable_ids]
    else:
        applicable_ids = None   # means all
        eligible_ids   = list(cart_event_ids)

    return {
        "code":           promo["code"],
        "type":           promo["discount_type"],
        "value":          float(promo["discount_value"]),
        "applies_to":     promo["applies_to"],
        "applicable_ids": applicable_ids,  # None = all
        "eligible_ids":   eligible_ids,    # subset of cart that gets discount
    }, None


def _apply_promo_discount(promo, cart_items, member_counts=None):
    """
    Given a validated promo dict and list of event dicts,
    returns (discount_amount, final_total).
    Only prices of eligible events are discounted.

    member_counts: optional {event_id: member_count} dict. For
    'per_member' priced events this multiplies the per-person fee by
    team size (via calculate_event_total) BEFORE the discount is
    computed — so a percent-off promo scales across every member on the
    team, and a flat-off promo is capped against the true team total
    rather than a single member's fee. Events missing from this dict (or
    when member_counts isn't passed at all, e.g. before the user has
    entered their team) default to a team size of 1, same as before.
    """
    member_counts = member_counts or {}
    eligible_ids = set(promo["eligible_ids"])

    def _total(item):
        return calculate_event_total(item, member_counts.get(item["id"], 1))

    eligible_total = sum(
        _total(item) for item in cart_items
        if item["id"] in eligible_ids
    )
    full_total = sum(_total(item) for item in cart_items)

    if promo["type"] == "flat":
        discount = min(promo["value"], eligible_total)
    else:  # percent
        discount = round(eligible_total * promo["value"] / 100, 2)

    final = max(0, full_total - discount)
    return round(discount, 2), round(final, 2)


def _increment_promo_used(code):
    """Increments used_count for a promo code after successful payment."""
    cursor = mysql.connection.cursor()
    cursor.execute(
        "UPDATE promo_codes SET used_count = used_count + 1 WHERE code=%s",
        (code,)
    )
    mysql.connection.commit()
    cursor.close()


# ======================================================
# CAMPUS AMBASSADOR — SQL TO CREATE TABLES (run once in MySQL)
# ======================================================
# Separate from promo_codes on purpose: a referral code is NOT a discount
# code. It only tracks which Campus Ambassador brought in a registration
# and awards that ambassador points — it never changes the price the
# participant pays. Points are awarded once per unique participant EMAIL
# per EVENT (a participant who registers for 3 events using the same
# ambassador's code earns that ambassador 3 points, but re-registering /
# a duplicate attempt for the same event does not earn a second point).
#
# CREATE TABLE IF NOT EXISTS campus_ambassadors (
#     id             INT AUTO_INCREMENT PRIMARY KEY,
#     name           VARCHAR(255) NOT NULL,
#     email          VARCHAR(255) DEFAULT NULL,
#     phone          VARCHAR(30)  DEFAULT NULL,
#     college        VARCHAR(255) DEFAULT NULL,
#     referral_code  VARCHAR(50) UNIQUE NOT NULL,
#     points         INT NOT NULL DEFAULT 0,
#     is_active      TINYINT(1) DEFAULT 1,
#     created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# );
# CREATE INDEX idx_ca_code ON campus_ambassadors(referral_code);
#
# CREATE TABLE IF NOT EXISTS ambassador_referrals (
#     id                  INT AUTO_INCREMENT PRIMARY KEY,
#     ambassador_id       INT NOT NULL,
#     referral_code       VARCHAR(50) NOT NULL,
#     event_id            INT NOT NULL,
#     participant_email   VARCHAR(255) NOT NULL,
#     registration_code   VARCHAR(50) DEFAULT NULL,
#     points_awarded      INT NOT NULL DEFAULT 1,
#     created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#     UNIQUE KEY uq_ambassador_participant_event (ambassador_id, participant_email, event_id),
#     FOREIGN KEY (ambassador_id) REFERENCES campus_ambassadors(id) ON DELETE CASCADE
# );
#
# -- MANAGE AMBASSADORS --
# Add an ambassador:
#   INSERT INTO campus_ambassadors (name, email, phone, college, referral_code)
#   VALUES ('Jane Doe', 'jane@example.com', '9999999999', 'ABC College', 'JANE10');
#
# Check a leaderboard:
#   SELECT name, referral_code, points FROM campus_ambassadors ORDER BY points DESC;
#
# See who a given ambassador referred:
#   SELECT ar.participant_email, ar.event_id, ar.registration_code, ar.created_at
#   FROM ambassador_referrals ar
#   JOIN campus_ambassadors ca ON ca.id = ar.ambassador_id
#   WHERE ca.referral_code = 'JANE10';
#
# Deactivate a code:  UPDATE campus_ambassadors SET is_active=0 WHERE referral_code='JANE10';
# ======================================================

# ======================================================
# CAMPUS AMBASSADOR HELPERS
# ======================================================

def _validate_referral_code(code):
    """
    Validates a referral code against the campus_ambassadors table.
    Returns (ambassador_dict, error_message). ambassador_dict contains
    id, name, referral_code. Does NOT affect price — attribution only.
    """
    code = (code or "").strip().upper()
    if not code:
        return None, "Enter a referral code."

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            "SELECT id, name, referral_code FROM campus_ambassadors "
            "WHERE referral_code=%s AND is_active=1",
            (code,)
        )
        amb = cursor.fetchone()
    except Exception as e:
        app.logger.warning(f"_validate_referral_code lookup error: {e}")
        amb = None
    finally:
        cursor.close()

    if not amb:
        return None, "Invalid referral code."

    return {"id": amb["id"], "name": amb["name"], "referral_code": amb["referral_code"]}, None


def _award_referral_point(referral, event_id, participant_email, registration_code=None):
    """
    Awards one point to the ambassador behind `referral` (the dict saved in
    session["referral"] / carried through the booking payload) for a single
    unique participant registration on a single event.

    Idempotent and safe to call multiple times for the same
    (ambassador, participant_email, event_id): the UNIQUE KEY on
    ambassador_referrals means a repeat call is a no-op (points are only
    ever incremented on the first, successful INSERT).

    Silently does nothing if `referral` is empty/None or participant_email
    is missing — referral attribution is best-effort and must never block
    or fail a registration.
    """
    if not referral or not referral.get("id"):
        return
    participant_email = (participant_email or "").strip().lower()
    if not participant_email:
        return

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO ambassador_referrals
                   (ambassador_id, referral_code, event_id, participant_email, registration_code)
               VALUES (%s, %s, %s, %s, %s)""",
            (referral["id"], referral.get("referral_code"), event_id,
             participant_email, registration_code)
        )
        # INSERT succeeded → this is genuinely a new unique (ambassador,
        # participant, event) combination, so award the point now.
        cursor.execute(
            "UPDATE campus_ambassadors SET points = points + 1 WHERE id=%s",
            (referral["id"],)
        )
        mysql.connection.commit()
    except pymysql.err.IntegrityError:
        # Duplicate (ambassador_id, participant_email, event_id) — this
        # participant was already credited to this ambassador for this
        # event. Not an error; just don't double-award.
        mysql.connection.rollback()
    except Exception as e:
        mysql.connection.rollback()
        app.logger.warning(f"_award_referral_point error (event_id={event_id}): {e}")
    finally:
        cursor.close()


def _authoritative_cart_total(event_ids, promo=None):
    """
    SECURITY: this is the single source of truth for what a set of events
    should cost. It is derived entirely from event prices in the DB and an
    already-validated promo dict from the session — it never trusts a
    client-supplied amount.
    NOTE: not currently called anywhere (the actual TiQR-facing total is
    computed inline in /create-order); kept here as a reusable helper.
    Doesn't account for per-event member counts (per_member pricing) —
    pass fully-priced event rows in, or use _apply_promo_discount(...,
    member_counts=...) directly if team size matters for the caller.
    Returns the pre-convenience-fee total in rupees, rounded to 2dp.
    """
    if not event_ids:
        return 0.0

    cursor = mysql.connection.cursor()
    fmt = ",".join(["%s"] * len(event_ids))
    cursor.execute(f"SELECT * FROM events WHERE id IN ({fmt})", tuple(event_ids))
    cart_items = cursor.fetchall()
    cursor.close()

    if promo and promo.get("code") and promo.get("eligible_ids") is not None:
        promo_for_calc = {
            "type":         promo.get("type"),
            "value":        promo.get("value"),
            "eligible_ids": promo.get("eligible_ids") or [],
        }
        _, final_total = _apply_promo_discount(promo_for_calc, cart_items)
        return round(final_total, 2)

    full_total = sum(get_price(item) for item in cart_items)
    return round(full_total, 2)

# ======================================================
# SPONSORS
# ======================================================
# Edit this list to add/remove/reorder sponsors — no template changes
# needed. "tier" controls which section + card size it renders in:
#   "title"   -> one big hero card at the top
#   "gold"    -> medium cards, 2nd row
#   "silver"  -> standard grid cards, 3rd row
#   "partner" -> small logo-only strip at the bottom
# "logo" is a path under /static (e.g. "sponsors/foo.png").
# "url" is the sponsor's website — opened in a new tab on click.
SPONSORS = [
    {
        "name": "Sample Title Sponsor",
        "tier": "title",
        "logo": "sponsors/title_placeholder.png",
        "url": "https://example.com",
        "blurb": "Replace this with your real title sponsor's name, logo and link.",
    },
    {
        "name": "Sample Gold Sponsor A",
        "tier": "gold",
        "logo": "sponsors/gold_placeholder.png",
        "url": "https://example.com",
        "blurb": "",
    },
    {
        "name": "Sample Gold Sponsor B",
        "tier": "gold",
        "logo": "sponsors/gold_placeholder.png",
        "url": "https://example.com",
        "blurb": "",
    },
    {
        "name": "Sample Silver Sponsor A",
        "tier": "silver",
        "logo": "sponsors/silver_placeholder.png",
        "url": "https://example.com",
        "blurb": "",
    },
    {
        "name": "Sample Silver Sponsor B",
        "tier": "silver",
        "logo": "sponsors/silver_placeholder.png",
        "url": "https://example.com",
        "blurb": "",
    },
    {
        "name": "Sample Silver Sponsor C",
        "tier": "silver",
        "logo": "sponsors/silver_placeholder.png",
        "url": "https://example.com",
        "blurb": "",
    },
    {
        "name": "Sample Community Partner A",
        "tier": "partner",
        "logo": "sponsors/partner_placeholder.png",
        "url": "https://example.com",
        "blurb": "",
    },
    {
        "name": "Sample Community Partner B",
        "tier": "partner",
        "logo": "sponsors/partner_placeholder.png",
        "url": "https://example.com",
        "blurb": "",
    },
]


@app.route("/sponsors")
def sponsors():
    tiers = {"title": [], "gold": [], "silver": [], "partner": []}
    for s in SPONSORS:
        tiers.setdefault(s.get("tier", "silver"), []).append(s)
    return render_template("sponsors.html", tiers=tiers)

def _check_ticket_availability(event_ids):
    """
    Checks whether all requested events still have tickets available.
    Returns (ok, sold_out_titles).
    ok=True means all events are available.
    sold_out_titles is a list of event names that are sold out.
    """
    if not event_ids:
        return True, []

    cursor = mysql.connection.cursor()
    fmt = ",".join(["%s"] * len(event_ids))
    cursor.execute(
        f"""SELECT id, title, max_tickets, tickets_sold
            FROM events
            WHERE id IN ({fmt})""",
        tuple(event_ids)
    )
    rows = cursor.fetchall()
    cursor.close()

    sold_out = []
    for row in rows:
        max_t  = row.get("max_tickets")
        sold   = row.get("tickets_sold") or 0
        if max_t is not None and sold >= max_t:
            sold_out.append(row.get("title") or f"Event #{row['id']}")

    return (len(sold_out) == 0), sold_out


def _reserve_tickets_or_fail(cursor, event_ids):
    """
    SECURITY/CORRECTNESS: atomically reserves a ticket for every event in
    event_ids by incrementing tickets_sold only when capacity allows — the
    check and the increment happen in the SAME guarded UPDATE statement, so
    two concurrent checkouts can't both pass a separate "is there room?"
    query and then both increment afterwards (that race is what let events
    be oversold before).

    MUST be called inside the same DB transaction as the registration
    INSERTs that follow it, using the same cursor/connection. If ok=False,
    the caller MUST roll back the transaction — none of the increments made
    in this call should be kept once any one of them fails, since InnoDB
    row locks held by this transaction's UPDATEs are what makes a concurrent
    request's UPDATE block (and then correctly fail its own capacity check)
    until this transaction commits or rolls back.

    Returns (ok, sold_out_titles).
    """
    if not event_ids:
        return True, []

    sold_out = []
    for eid in event_ids:
        cursor.execute(
            """UPDATE events
               SET tickets_sold = tickets_sold + 1
               WHERE id = %s
                 AND (max_tickets IS NULL OR tickets_sold < max_tickets)""",
            (eid,)
        )
        if cursor.rowcount != 1:
            cursor.execute("SELECT title FROM events WHERE id = %s", (eid,))
            row = cursor.fetchone()
            sold_out.append(row["title"] if row else f"Event #{eid}")

    return (len(sold_out) == 0), sold_out

# ======================================================
# PERSISTENT CART HELPERS
# ======================================================

def db_get_cart(user_id):
    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT event_id FROM user_cart WHERE user_id=%s ORDER BY added_at ASC",
        (user_id,)
    )
    rows = cursor.fetchall()
    cursor.close()
    return [row["event_id"] for row in rows]


def db_add_to_cart(user_id, event_id):
    cursor = mysql.connection.cursor()
    cursor.execute(
        "INSERT IGNORE INTO user_cart (user_id, event_id) VALUES (%s, %s)",
        (user_id, event_id)
    )
    mysql.connection.commit()
    cursor.close()


def db_remove_from_cart(user_id, event_id):
    cursor = mysql.connection.cursor()
    cursor.execute(
        "DELETE FROM user_cart WHERE user_id=%s AND event_id=%s",
        (user_id, event_id)
    )
    mysql.connection.commit()
    cursor.close()


def db_remove_events_from_cart(user_id, event_ids):
    """
    Removes specific event(s) from a user's cart — used right after a
    registration for those events is successfully written (and committed)
    to MySQL, so paid/free tickets disappear from the cart as soon as
    they're actually confirmed rather than waiting for the browser to land
    back on a particular page.
    """
    event_ids = [eid for eid in (event_ids or []) if eid]
    if not user_id or not event_ids:
        return
    cursor = mysql.connection.cursor()
    fmt = ",".join(["%s"] * len(event_ids))
    cursor.execute(
        f"DELETE FROM user_cart WHERE user_id=%s AND event_id IN ({fmt})",
        (user_id, *event_ids)
    )
    mysql.connection.commit()
    cursor.close()


def db_clear_cart(user_id):
    cursor = mysql.connection.cursor()
    cursor.execute("DELETE FROM user_cart WHERE user_id=%s", (user_id,))
    mysql.connection.commit()
    cursor.close()


def _get_pending_event_ids(user_id):
    """
    Returns the set of event IDs the user has a UPI payment sitting in
    'pending' (awaiting admin approval) for. Used to keep an event out of
    the cart / add-to-cart while a payment for it is still being reviewed —
    otherwise the same user could submit (or pay for) the same event twice
    before the first submission has been approved or rejected.
    """
    if not user_id:
        return set()

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            "SELECT event_ids FROM upi_payments WHERE user_id = %s AND status = 'pending'",
            (user_id,)
        )
        rows = cursor.fetchall()
    except Exception as e:
        app.logger.warning(f"_get_pending_event_ids: fetch error: {e}")
        return set()
    finally:
        cursor.close()

    pending = set()
    for row in rows:
        try:
            for eid in _json.loads(row.get("event_ids") or "[]"):
                pending.add(int(eid))
        except Exception:
            pass
    return pending


def sync_session_cart(user_id):
    guest_ids = session.get("cart", [])
    for eid in guest_ids:
        try:
            db_add_to_cart(user_id, int(eid))
        except Exception:
            pass
    session["cart"] = db_get_cart(user_id)
    session.modified = True


def get_price(item):
    # `fee` is the buyer-facing column (what's actually shown/charged) —
    # check it first so free/paid classification and TiQR booking amounts
    # never silently diverge from what the user sees on the page. `price`
    # is kept as a fallback for older rows that only have that column set.
    for col in ("fee", "price", "amount", "registration_fee"):
        if col in item and item[col] is not None:
            try:
                return int(float(item[col]))
            except (ValueError, TypeError):
                pass
    return 0


# ======================================================
# PRICING MODE — run once in MySQL
# ======================================================
# ALTER TABLE events
#     ADD COLUMN pricing_mode VARCHAR(20) NOT NULL DEFAULT 'flat';
#
# 'flat'       (default) — the event's `fee` is the total charge for the
#              whole team, no matter how many members are on it.
#              e.g. Boat Wars: fee=200, team of 4 still pays ₹200 total.
# 'per_member' — the event's `fee` is charged PER PERSON on the team; the
#              total is fee × number of members entered at registration.
#              e.g. IMO Insights: fee=99, a team of 3 pays ₹297 total.
#
# When adding a new event, specify which mode it uses:
#   INSERT INTO events (title, fee, pricing_mode, ...) VALUES ('IMO Insights', 99, 'per_member', ...);
#   INSERT INTO events (title, fee, pricing_mode, ...) VALUES ('Boat Wars', 200, 'flat', ...);
# ======================================================

def calculate_event_total(event_row, member_count):
    """Returns the total ₹ amount to charge for one event's registration,
    given how many people are on the team.

    'per_member' events: fee × member_count (minimum 1 — never charge 0
    people). 'flat' (or missing/unrecognized) events: just the fee,
    regardless of team size — this is the existing/default behavior.
    """
    base = get_price(event_row)
    mode = (event_row.get("pricing_mode") or "flat").strip().lower()
    if mode == "per_member":
        count = max(1, int(member_count or 1))
        return base * count
    return base


# ======================================================
# BOAT WARS EVENT — run once in MySQL to insert event
# ======================================================
# INSERT INTO events (title, price, icon, team, category, event_type,
#                     venue, event_date, event_time, max_tickets)
# VALUES ('Boat Wars', 300, '⛵', '2 Members', 'Competition',
#         'Flagship', 'Waterfront Arena', '2026-10-11', '09:00:00', 200)
# ON DUPLICATE KEY UPDATE title=title;
#
# -- Or if the row already exists (e.g. price=0, soon=true):
#   UPDATE events SET price=300, team='2 Members', max_tickets=200,
#          event_date='2026-10-11' WHERE LOWER(title) LIKE '%boat wars%';
# ======================================================

# ======================================================
# HOME
# ======================================================

@app.route("/")
def home():
    return render_template("index.html")

# ======================================================
# FAVICON / MANIFEST — served from static/assets/
# ======================================================

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static", "assets"),
        "favicon.ico", mimetype="image/vnd.microsoft.icon"
    )

@app.route("/favicon-32x32.png")
def favicon_32():
    return send_from_directory(
        os.path.join(app.root_path, "static", "assets"),
        "favicon-32x32.png", mimetype="image/png"
    )

@app.route("/favicon-16x16.png")
def favicon_16():
    return send_from_directory(
        os.path.join(app.root_path, "static", "assets"),
        "favicon-16x16.png", mimetype="image/png"
    )

@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return send_from_directory(
        os.path.join(app.root_path, "static", "assets"),
        "apple-touch-icon.png", mimetype="image/png"
    )

@app.route("/site.webmanifest")
def site_webmanifest():
    return send_from_directory(
        os.path.join(app.root_path, "static", "assets"),
        "site.webmanifest", mimetype="application/manifest+json"
    )

# ======================================================
# ABOUT
# ======================================================

@app.route("/about")
def about():
    return render_template("about.html")

# ======================================================
# PRIVACY POLICY
# ======================================================

@app.route("/privacy-policy")
def privacy_policy():
    return render_template("privacy-policy.html")

# ======================================================
# TERMS AND CONDITIONS
# ======================================================

@app.route("/terms-and-conditions")
def terms_and_conditions():
    return render_template("terms-and-conditions.html")

@app.route("/partners")
def partners():
    return render_template("partners.html")

# ======================================================
# REGISTER ACCOUNT
# ======================================================

# ======================================================
# GUEST IDENTITY
# ======================================================
# Accounts/login have been removed — checkout no longer requires signing
# up or signing in. Every browser session still gets one lightweight
# `users` row under the hood (created transparently on first checkout
# action) purely so the existing cart / registrations / upi_payments
# tables — all of which are keyed on user_id — keep working unchanged.
# The row starts as a placeholder and is overwritten with the visitor's
# real name/email/phone as soon as they submit the event-register form.
# ======================================================

def _get_or_create_user_id():
    """
    Returns this session's guest user_id, creating the row on first use.
    """
    user_id = session.get("user_id")
    if user_id:
        return user_id

    cursor = mysql.connection.cursor()
    cursor.execute(
        "INSERT INTO users (name, email, phone) VALUES (%s, %s, %s)",
        ("Guest", None, None)
    )
    mysql.connection.commit()
    user_id = cursor.lastrowid
    cursor.close()

    session["user_id"] = user_id
    session.modified = True
    return user_id


def _update_user_details(user_id, name, email, phone):
    """
    Overwrites the guest row's placeholder details with what the visitor
    actually entered on event-register, and mirrors them into the session
    so a returning-in-session visitor sees their info pre-filled next time.

    If `email` already belongs to a different users row (e.g. the same
    person checking out again in a new session), that collision would
    make the UPDATE below fail against the email UNIQUE constraint — so
    instead we adopt that existing row's id and keep using it, rather
    than silently leaving this guest row's email blank (which was
    breaking ticket emails, since every ticket-send path resolves the
    recipient address via `users`).

    Returns the user_id that should be used from this point on — it may
    differ from the one passed in if such a merge happened.
    """
    if not user_id:
        return user_id

    cursor = mysql.connection.cursor()
    try:
        if email:
            cursor.execute(
                "SELECT id FROM users WHERE email=%s AND id<>%s",
                (email, user_id)
            )
            existing = cursor.fetchone()
            if existing:
                user_id = existing["id"]
                cursor.execute(
                    "UPDATE users SET name=%s, phone=%s WHERE id=%s",
                    (name or None, phone or None, user_id)
                )
                mysql.connection.commit()
                cursor.close()
                session["user_id"]    = user_id
                session["user_name"]  = name or ""
                session["user_email"] = email or ""
                session["user_phone"] = phone or ""
                session.modified = True
                return user_id

        cursor.execute(
            "UPDATE users SET name=%s, email=%s, phone=%s WHERE id=%s",
            (name or None, email or None, phone or None, user_id)
        )
        mysql.connection.commit()
    except Exception as e:
        mysql.connection.rollback()
        app.logger.warning(f"_update_user_details: DB error for user {user_id}: {e}")
    finally:
        cursor.close()

    session["user_id"]    = user_id
    session["user_name"]  = name or ""
    session["user_email"] = email or ""
    session["user_phone"] = phone or ""
    session.modified = True
    return user_id


# ======================================================
# SESSION CHECK API  (used by index.html JS to show
# Boat Wars registration card when the visitor has an active
# guest session with a registration on file)
# ======================================================

@app.route("/api/session-check")
def session_check():
    user_id = session.get("user_id")
    if user_id:
        # Check if user already registered for Boat Wars
        already = False
        try:
            cursor = mysql.connection.cursor()
            cursor.execute(
                """SELECT r.id FROM registrations r
                   JOIN events e ON r.event_id = e.id
                   WHERE r.user_id = %s
                     AND LOWER(e.title) LIKE '%%hydro clash%%'
                   LIMIT 1""",
                (user_id,)
            )
            already = cursor.fetchone() is not None
            cursor.close()
        except Exception as e:
            app.logger.warning(f"session_check boat-wars query: {e}")
        return jsonify({
            "loggedin": True,
            "user_name": session.get("user_name", ""),
            "already_registered_boat_wars": already
        })
    return jsonify({"loggedin": False})

# ======================================================
# BOAT WARS  — add to cart helper route (POST)
# Finds the Boat Wars event by title and adds it to cart
# ======================================================

@app.route("/boat-wars/add-to-cart", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
def boat_wars_add_to_cart():
    user_id = _get_or_create_user_id()
    try:
        cursor = mysql.connection.cursor()
        cursor.execute(
            "SELECT id FROM events WHERE LOWER(title) LIKE '%%hydro clash%%' LIMIT 1"
        )
        row = cursor.fetchone()
        cursor.close()
    except Exception as e:
        app.logger.error(f"boat_wars_add_to_cart DB error: {e}")
        return jsonify({"success": False, "reason": "db_error"}), 500

    if not row:
        return jsonify({"success": False, "reason": "event_not_found"}), 404

    event_id = row["id"]

    # Check already registered
    try:
        cursor = mysql.connection.cursor()
        cursor.execute(
            "SELECT id FROM registrations WHERE user_id=%s AND event_id=%s LIMIT 1",
            (user_id, event_id)
        )
        already = cursor.fetchone()
        cursor.close()
    except Exception as e:
        app.logger.error(f"boat_wars already-registered check: {e}")
        return jsonify({"success": False, "reason": "db_error"}), 500

    if already:
        return jsonify({"success": False, "reason": "already_registered"}), 400

    # Add to cart
    if "cart" not in session:
        session["cart"] = []
    if event_id not in session["cart"]:
        session["cart"].append(event_id)
        session.modified = True
    try:
        db_add_to_cart(user_id, event_id)
    except Exception as e:
        app.logger.error(f"boat_wars db_add_to_cart error: {e}")

    return jsonify({"success": True, "event_id": event_id})

# ======================================================
# EVENTS
# ======================================================

@app.route("/events")
def events():
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM events ORDER BY id DESC")
    all_events = cursor.fetchall()

    # Fetch events the logged-in user has already registered for
    registered_event_ids = []
    pending_event_ids = []
    user_id = session.get("user_id")
    if user_id:
        cursor.execute(
            "SELECT DISTINCT event_id FROM registrations WHERE user_id = %s",
            (user_id,)
        )
        registered_event_ids = [row["event_id"] for row in cursor.fetchall()]

        # Events that have a UPI payment still awaiting admin verification
        pending_event_ids = list(_get_pending_event_ids(user_id))

    # Fetch sold-out event IDs (max_tickets reached)
    sold_out_ids = []
    try:
        cursor.execute(
            """SELECT id FROM events
               WHERE max_tickets IS NOT NULL
                 AND tickets_sold >= max_tickets"""
        )
        sold_out_ids = [row["id"] for row in cursor.fetchall()]
    except Exception as e:
        app.logger.warning(f"events: could not fetch sold_out_ids: {e}")

    cursor.close()
    return render_template(
        "events.html",
        events=all_events,
        registered_event_ids=registered_event_ids,
        sold_out_ids=sold_out_ids,
        pending_event_ids=pending_event_ids
    )

# ======================================================
# ADD TO CART
# ======================================================

@app.route("/add-to-cart/<int:event_id>", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
def add_to_cart(event_id):
    user_id = session.get("user_id")

    if user_id:
        # Block if the user has already registered for this event
        cursor = mysql.connection.cursor()
        cursor.execute(
            "SELECT id FROM registrations WHERE user_id = %s AND event_id = %s LIMIT 1",
            (user_id, event_id)
        )
        already = cursor.fetchone()
        cursor.close()
        if already:
            return jsonify({"success": False, "reason": "already_registered"}), 400

        # Block if a UPI payment for this event is still awaiting admin approval —
        # otherwise the user could submit/pay for the same event twice.
        if event_id in _get_pending_event_ids(user_id):
            return jsonify({"success": False, "reason": "payment_pending"}), 400

    if "cart" not in session:
        session["cart"] = []
    if event_id not in session["cart"]:
        session["cart"].append(event_id)
        session.modified = True

    if user_id:
        try:
            db_add_to_cart(user_id, event_id)
        except Exception as e:
            app.logger.error(f"db_add_to_cart error: {e}")

    return jsonify({"success": True})

# ======================================================
# CART SYNC  (called by cart.html JS before navigating to event-register)
# Accepts a list of dbIds from localStorage and syncs them into user_cart
# ======================================================

@app.route("/api/cart/sync", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
@_json_safe({"success": False, "reason": "server_error"})
def cart_sync():
    user_id = _get_or_create_user_id()
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"success": False, "reason": "invalid_json"}), 400

    event_ids = data.get("event_ids", [])
    pending_ids = _get_pending_event_ids(user_id)
    synced = []
    skipped_pending = []
    for raw_id in event_ids:
        try:
            eid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if eid in pending_ids:
            skipped_pending.append(eid)
            continue
        try:
            db_add_to_cart(user_id, eid)
            synced.append(eid)
        except Exception as e:
            app.logger.warning(f"cart_sync: could not add event {eid} for user {user_id}: {e}")

    # Refresh session cart from DB
    session["cart"] = db_get_cart(user_id)
    session.modified = True

    return jsonify({"success": True, "synced": synced, "skipped_pending": skipped_pending})


# ======================================================
# REMOVE FROM CART
# ======================================================

@app.route("/api/cart/remove/<int:event_id>", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
def remove_from_cart(event_id):
    cart = session.get("cart", [])
    if event_id in cart:
        cart.remove(event_id)
        session["cart"] = cart
        session.modified = True

    user_id = session.get("user_id")
    if user_id:
        try:
            db_remove_from_cart(user_id, event_id)
        except Exception as e:
            app.logger.error(f"db_remove_from_cart error: {e}")

    return jsonify({"success": True})

# ======================================================
# CART
# ======================================================

@app.route("/cart")
def cart():
    user_id = session.get("user_id")

    if user_id:
        cart_ids = db_get_cart(user_id)
        session["cart"] = cart_ids
        session.modified = True
    else:
        cart_ids = session.get("cart", [])

    clean_ids = []
    for i in cart_ids:
        try:
            clean_ids.append(int(i))
        except (ValueError, TypeError):
            pass

    # An event with a UPI payment still awaiting admin approval shouldn't sit
    # in the cart — otherwise the user could pay for / resubmit the same
    # event again before the first submission is reviewed. Drop it from the
    # cart entirely (DB + session) rather than just hiding it on this page.
    if user_id and clean_ids:
        pending_ids = _get_pending_event_ids(user_id)
        still_pending = [eid for eid in clean_ids if eid in pending_ids]
        if still_pending:
            for eid in still_pending:
                try:
                    db_remove_from_cart(user_id, eid)
                except Exception as e:
                    app.logger.warning(f"cart: could not remove pending event {eid} from cart: {e}")
            clean_ids = [eid for eid in clean_ids if eid not in pending_ids]
            session["cart"] = clean_ids
            session.modified = True

    if not clean_ids:
        return render_template("cart.html", cart_items=[], total=0)

    format_strings = ",".join(["%s"] * len(clean_ids))
    cursor = mysql.connection.cursor()
    cursor.execute(
        f"SELECT * FROM events WHERE id IN ({format_strings})",
        tuple(clean_ids)
    )
    cart_items = cursor.fetchall()
    cursor.close()

    total = sum(get_price(item) for item in cart_items)
    convenience_fee, total_with_fee = (0.0, 0.0) if total == 0 else apply_convenience_fee(total)

    promo = session.get("promo", {})

    return render_template(
        "cart.html",
        cart_items=cart_items,
        total=total,
        convenience_fee=convenience_fee,
        total_with_fee=total_with_fee,
        promo=promo
    )

# ======================================================
# VALIDATE PROMO CODE  (called by cart page AJAX)
# ======================================================

@app.route("/validate-promo", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
@_json_safe({"valid": False, "message": "Something went wrong on our end. Please try again."})
def validate_promo():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify({"valid": False, "message": "Enter a promo code."})

    # Optional: {event_id: member_count} for per_member-priced events, so
    # the discount preview matches team size (e.g. a 3-person team on a
    # ₹249/member event). Frontend can send this once team members have
    # been entered; if omitted (e.g. promo applied before team entry),
    # each event defaults to a team size of 1 — same as previous behavior.
    raw_member_counts = data.get("member_counts") or {}
    member_counts = {}
    for k, v in raw_member_counts.items():
        try:
            member_counts[int(k)] = max(1, int(v))
        except (TypeError, ValueError):
            continue

    # Get current cart event IDs
    user_id  = session.get("user_id")

    if user_id:
        # Always read from DB (source of truth) and sync session
        cart_ids = db_get_cart(user_id)
        session["cart"] = cart_ids
        session.modified = True
    else:
        # Guest: session cart may hold numeric IDs or legacy slugs;
        # also accept ids passed directly from the frontend as fallback
        raw_ids = data.get("cart_ids") or session.get("cart", [])
        cart_ids = []
        for i in raw_ids:
            try:
                cart_ids.append(int(i))
            except (TypeError, ValueError):
                pass  # skip non-numeric slugs

    if not cart_ids:
        return jsonify({"valid": False, "message": "Your cart is empty."})

    promo, error = _validate_promo_code(code, cart_ids)
    if error:
        return jsonify({"valid": False, "message": error})

    # Fetch cart items to compute discount
    fmt = ",".join(["%s"] * len(cart_ids))
    cursor = mysql.connection.cursor()
    cursor.execute(f"SELECT * FROM events WHERE id IN ({fmt})", tuple(cart_ids))
    cart_items = cursor.fetchall()
    cursor.close()

    discount, final_total = _apply_promo_discount(promo, cart_items, member_counts)

    # Compute convenience fee on the discounted total (only if not free)
    conv_fee, final_with_fee = (0.0, 0.0) if final_total == 0 else apply_convenience_fee(final_total)

    # Save to session — keep both 'discount' (₹ amount) and 'discount_amount' for consistent lookup
    session["promo"] = {
        "code":            promo["code"],
        "type":            promo["type"],
        "value":           promo["value"],          # rate: flat ₹ or percent %
        "applies_to":      promo["applies_to"],
        "applicable_ids":  promo["applicable_ids"],
        "eligible_ids":    promo["eligible_ids"],
        "discount":        discount,                # computed ₹ discount
        "discount_amount": discount,                # alias used by payment_success flat-promo logic
    }
    session.modified = True

    eligible_names = []
    if promo["applies_to"] == "specific":
        eligible_names = [
            item.get("title") or item.get("name") or f"Event #{item['id']}"
            for item in cart_items
            if item["id"] in set(promo["eligible_ids"])
        ]

    discount_label = (
        f"₹{int(promo['value'])} flat off"
        if promo["type"] == "flat"
        else f"{int(promo['value'])}% off"
    )

    return jsonify({
        "valid":            True,
        "message":          f"✓ Code applied! {discount_label}",
        "discount_type":    promo["type"],
        "discount_value":   promo["value"],
        "discount_amount":  discount,
        "final_total":      final_total,
        "convenience_fee":  conv_fee,
        "final_with_fee":   final_with_fee,
        "applies_to":       promo["applies_to"],
        "eligible_names":   eligible_names,
        "is_free":          final_total == 0,
    })


@app.route("/remove-promo", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
def remove_promo():
    session.pop("promo", None)
    session.modified = True
    return jsonify({"success": True})


# ======================================================
# VALIDATE REFERRAL CODE  (Campus Ambassador attribution)
# Separate field from promo code — carries NO discount, only credits
# the Ambassador with a point once the registration actually completes.
# ======================================================

@app.route("/validate-referral", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
@_json_safe({"valid": False, "message": "Something went wrong on our end. Please try again."})
def validate_referral():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify({"valid": False, "message": "Enter a referral code."})

    ambassador, error = _validate_referral_code(code)
    if error:
        return jsonify({"valid": False, "message": error})

    session["referral"] = {
        "id":            ambassador["id"],
        "referral_code": ambassador["referral_code"],
    }
    session.modified = True

    return jsonify({
        "valid":   True,
        "message": f"✓ Referral code applied !",
    })


@app.route("/remove-referral", methods=["POST"])
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
def remove_referral():
    session.pop("referral", None)
    session.modified = True
    return jsonify({"success": True})


# ======================================================
# EVENT REGISTER
# ======================================================

def _build_ticket_success_data(user_id, codes):
    """
    Looks up already-completed registrations (by code, scoped to this user)
    and shapes them into the same {id, name, icon, team, venue, day,
    category} form that CART_ITEMS uses — so the post-payment success view
    can reuse showTicketSuccess() unchanged. Returns (ordered_valid_codes,
    items_list, fname). Codes that don't belong to this user are silently
    dropped (defensive — e.g. a stale/tampered query string).
    """
    codes = [c.strip() for c in codes if c and c.strip()]
    if not user_id or not codes:
        return [], [], ""

    cursor = mysql.connection.cursor()
    try:
        fmt = ",".join(["%s"] * len(codes))
        cursor.execute(
            f"""
            SELECT r.registration_code, e.*
            FROM   registrations r
            JOIN   events        e ON e.id = r.event_id
            WHERE  r.user_id = %s AND r.registration_code IN ({fmt})
            """,
            (user_id, *codes)
        )
        rows = {row["registration_code"]: row for row in cursor.fetchall()}

        fname = ""
        cursor.execute(
            f"""
            SELECT member_name FROM registration_members
            WHERE registration_code IN ({fmt}) AND member_order = 0
            LIMIT 1
            """,
            tuple(codes)
        )
        m = cursor.fetchone()
        if m and m.get("member_name"):
            fname = m["member_name"].split()[0]
        else:
            cursor.execute("SELECT name FROM users WHERE id=%s", (user_id,))
            u = cursor.fetchone()
            if u and u.get("name"):
                fname = u["name"].split()[0]
    finally:
        cursor.close()

    ordered_codes = [c for c in codes if c in rows]
    items = []
    for c in ordered_codes:
        e = rows[c]
        items.append({
            "id":       e.get("id"),
            "name":     e.get("title") or e.get("name") or "Event",
            "icon":     e.get("icon") or "\U0001f3af",
            "team":     e.get("team") or "Solo",
            "venue":    e.get("venue") or "",
            "day":      str(e.get("event_day") or e.get("day") or ""),
            "category": e.get("category") or "",
        })
    return ordered_codes, items, (fname or "Participant")


@app.route("/event-register")
def event_register():
    user_id = _get_or_create_user_id()

    # ── Post-payment success view ───────────────────────────────────────
    # /payment-complete redirects here with ?tickets=CODE1,CODE2 once
    # payment is confirmed, so paid users land on the exact same polished
    # download-card success screen free checkouts already get.
    tickets_param = request.args.get("tickets", "")
    ticket_codes, purchased_tickets, ticket_fname = _build_ticket_success_data(
        user_id, tickets_param.split(",")
    ) if tickets_param else ([], [], "")

    cursor = mysql.connection.cursor()
    cursor.execute(
        """
        SELECT e.*
        FROM   user_cart uc
        JOIN   events    e  ON e.id = uc.event_id
        WHERE  uc.user_id = %s
        ORDER  BY uc.added_at ASC
        """,
        (user_id,)
    )
    rows = cursor.fetchall()
    cursor.close()

    if not rows and not ticket_codes:
        # Cart may be empty in DB if the user hasn't synced from localStorage yet.
        # The JS in cart.html calls /api/cart/sync before navigating here, so
        # if rows is still empty the cart is genuinely empty — redirect back.
        flash("Your cart is empty. Please add events before registering.")
        return redirect(url_for("cart"))

    session["cart"] = [row["id"] for row in rows]
    session.modified = True

    def normalize_event(row):
        r    = dict(row)
        name = r.get("title") or r.get("name") or r.get("event_name") or "Event"
        r["title"] = name
        r["name"]  = name

        fee = 0
        for col in ("fee", "price", "amount", "registration_fee"):
            if r.get(col) is not None:
                try:
                    fee = int(float(r[col])); break
                except (ValueError, TypeError):
                    pass
        r["fee"]   = fee
        r["price"] = fee

        mode = (r.get("pricing_mode") or "flat").strip().lower()
        if mode not in ("flat", "per_member"):
            mode = "flat"
        r["pricing_mode"] = mode

        r["icon"] = r.get("icon") or "\U0001f3af"

        cat = r.get("category") or r.get("event_type") or r.get("type") or ""
        r["category"]   = cat
        r["event_type"] = cat

        team = r.get("team") or r.get("team_size") or r.get("team_type") or ""
        if not team or str(team).strip().lower() in ("", "none", "null"):
            n = name.lower()
            if   "hydro clash"             in n: team = "2 Members"
            elif "the lost voyage"         in n: team = "3-4 Members"
            elif "naval frontiers"    in n: team = "Solo / Duo"
            elif "the pitch deck"            in n: team = "1-3 Members"
            elif "helm debate"                in n: team = "2 Members"
            elif "nautiq"                  in n: team = "2-3 Members"
            else:                               team = "Solo"
        r["team"]      = team
        r["team_size"] = team

        r["venue"] = r.get("venue") or ""
        day = r.get("event_day") or r.get("day") or r.get("event_date") or ""
        r["event_day"]  = str(day) if day else ""
        r["event_date"] = str(day) if day else ""
        return r

    cart_items = [normalize_event(row) for row in rows]

    promo = session.get("promo", {})

    return render_template(
        "event-register.html",
        cart_items=cart_items,
        promo=promo,
        ticket_codes=ticket_codes,
        purchased_tickets=purchased_tickets,
        ticket_fname=ticket_fname
    )


# ======================================================
# REGISTRATION WRITER (shared by free checkout + TiQR webhook)
# ======================================================

def _write_registration_for_event(cursor, user_id, event_id, event_row,
                                    promo_amount_paid, participant, members,
                                    payment_id, order_id):
    """
    Inserts ONE registration row (+ its team members) for an already-paid
    (or free) event. Does NOT commit — caller controls the transaction.
    Does NOT reserve ticket capacity — caller must have already called
    _reserve_tickets_or_fail for this event_id in the same transaction.

    Returns the registration_code.
    """
    reg_code = "ANCH-" + str(uuid.uuid4()).replace("-", "")[:8].upper()

    cursor.execute(
        """
        INSERT INTO registrations
            (user_id, event_id, payment_id, order_id, registration_code, checked_in, amount_paid)
        VALUES (%s, %s, %s, %s, %s, FALSE, %s)
        """,
        (user_id, event_id, payment_id, order_id, reg_code, int(promo_amount_paid))
    )

    primary_name = ((participant.get("first_name") or "") + " " + (participant.get("last_name") or "")).strip()

    rows_to_save = []
    if members:
        for idx, m in enumerate(members):
            fname = (m.get("fname") or m.get("first_name") or "").strip()
            lname = (m.get("lname") or m.get("last_name") or "").strip()
            full  = (fname + " " + lname).strip() or (primary_name if idx == 0 else f"Member {idx+1}")
            rows_to_save.append((
                reg_code, full,
                (m.get("email")   or "").strip() or None,
                (m.get("phone")   or "").strip() or None,
                (m.get("college") or "").strip() or None,
                (m.get("dept")    or "").strip() or None,
                idx,
            ))
    elif primary_name:
        rows_to_save.append((
            reg_code, primary_name,
            (participant.get("email")   or "").strip() or None,
            (participant.get("phone")   or "").strip() or None,
            (participant.get("college") or "").strip() or None,
            (participant.get("dept")    or "").strip() or None,
            0,
        ))

    if rows_to_save:
        try:
            cursor.executemany(
                """INSERT INTO registration_members
                       (registration_code, member_name, member_email, member_phone,
                        member_college, member_dept, member_order)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                rows_to_save
            )
        except Exception as me:
            app.logger.warning(f"registration_members insert skipped for {reg_code}: {me}")

    return reg_code


def _complete_paid_event(user_id, event_id, promo_snapshot, participant, members,
                          payment_id, order_id, eligible_total_price=0,
                          send_email=True, referral=None):
    """
    Reserves capacity + writes the registration for a single event that has
    just been confirmed as paid (called from the TiQR webhook), or is free
    (called directly from /create-order). Sends the ticket email for this
    one registration, unless send_email=False — pass False when the caller
    is completing several events from the same cart/booking together and
    will send one combined email itself after the loop (see the free-events
    loop in /create-order and the multi-event branch of
    _finalize_tiqr_booking) so the participant gets one email listing every
    ticket instead of one email per event. Idempotent at the caller's level
    via tiqr_bookings.status — this function itself does not de-duplicate.

    eligible_total_price: sum of prices of all promo-eligible events in the
    ORIGINAL cart this event was checked out with — needed to correctly
    split a flat-₹-amount promo discount across events (each event absorbs
    a share proportional to its own price). Ignored for percent promos.
    This is bookkeeping only (see TIQR EVENTS gap note) and has no effect
    on what TiQR actually charged.

    referral: optional dict {"id": <ambassador id>, "referral_code": <code>}
    from session["referral"] (or the persisted booking payload for the
    TiQR/paid path). When present, the participant's registration for this
    event awards the Campus Ambassador one point — but only the first time
    this participant is registered for this specific event (see
    _award_referral_point). Attribution never affects price.

    Returns (registration_code_or_None, error_message_or_None).
    """
    cursor = mysql.connection.cursor()
    try:
        # Already registered for this event? Don't double-charge a ticket slot.
        cursor.execute(
            "SELECT registration_code FROM registrations WHERE user_id=%s AND event_id=%s",
            (user_id, event_id)
        )
        existing = cursor.fetchone()
        if existing:
            cursor.close()
            return existing["registration_code"], None

        ok, sold_out = _reserve_tickets_or_fail(cursor, [event_id])
        if not ok:
            mysql.connection.rollback()
            cursor.close()
            return None, f"Sorry, tickets are sold out for: {', '.join(sold_out)}"

        event_row = _fetch_event_row(event_id) or {}
        member_count = max(1, len(members)) if members else 1
        base_price = calculate_event_total(event_row, member_count)
        amount_paid = base_price
        promo_eligible_ids = set(int(x) for x in (promo_snapshot or {}).get("eligible_ids") or [])
        if promo_snapshot and promo_snapshot.get("code") and event_id in promo_eligible_ids:
            # Best-effort discount bookkeeping only — see TIQR EVENTS gap
            # note near the top of the file: this does NOT change what
            # TiQR actually charged.
            ptype = promo_snapshot.get("type")
            if ptype == "percent":
                pct = float(promo_snapshot.get("value", 0))
                amount_paid = max(0, round(base_price * (1 - pct / 100), 2))
            elif ptype == "flat" and eligible_total_price > 0:
                flat_total = float(promo_snapshot.get("discount_amount") or promo_snapshot.get("discount", 0))
                share = base_price / eligible_total_price
                amount_paid = max(0, round(base_price - flat_total * share, 2))


        reg_code = _write_registration_for_event(
            cursor, user_id, event_id, event_row,
            amount_paid, participant, members, payment_id, order_id
        )
        mysql.connection.commit()
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        app.logger.error(f"_complete_paid_event insert error (event_id={event_id}): {e}")
        return None, f"Registration save failed: {e}"

    cursor.close()

    # ── Campus Ambassador referral attribution ──────────────────────────
    # Best-effort: never let a referral hiccup fail an already-successful
    # registration. Only fires for a brand-new registration_members write
    # above, not the "already registered" early-return, so a participant
    # can't be credited twice for the same event.
    if referral:
        try:
            _award_referral_point(
                referral, event_id,
                (participant or {}).get("email"),
                registration_code=reg_code,
            )
        except Exception as e:
            app.logger.warning(f"Referral point award failed for {reg_code}: {e}")

    if send_email:
        try:
            host_url = request.host_url
        except RuntimeError:
            # Called outside a request context is not expected here, but guard anyway.
            host_url = os.environ.get("APP_BASE_URL", "https://anchorage2026.example.com/")

        try:
            _send_combined_ticket_email([reg_code], host_url)
        except Exception as e:
            import traceback as _tb, sys
            _tb.print_exc(file=sys.stderr)
            app.logger.error(f"Ticket email failed for {reg_code}: {e}")

    return reg_code, None


def _fetch_event_row(event_id):
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM events WHERE id=%s", (event_id,))
    row = cursor.fetchone()
    cursor.close()
    return row


# ======================================================
# TIQR BOOKING TABLE HELPERS
# ======================================================

def _save_tiqr_booking(booking_uid, cart_group_id, user_id, event_id, payload):
    cursor = mysql.connection.cursor()
    cursor.execute(
        """INSERT INTO tiqr_bookings
               (booking_uid, cart_group_id, user_id, event_id, status, payload_json)
           VALUES (%s, %s, %s, %s, 'pending', %s)""",
        (booking_uid, cart_group_id, user_id, event_id, _json.dumps(payload))
    )
    mysql.connection.commit()
    cursor.close()


def _get_tiqr_booking(booking_uid):
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM tiqr_bookings WHERE booking_uid=%s", (booking_uid,))
    row = cursor.fetchone()
    cursor.close()
    return row


def _mark_tiqr_booking_status(booking_uid, status):
    cursor = mysql.connection.cursor()
    cursor.execute(
        "UPDATE tiqr_bookings SET status=%s WHERE booking_uid=%s",
        (status, booking_uid)
    )
    mysql.connection.commit()
    cursor.close()


# ======================================================
# CREATE TIQR BOOKING(S)
# ======================================================
# Replaces the old Razorpay order-creation step. Unlike Razorpay's embedded
# checkout modal, TiQR uses a hosted redirect page — the frontend should
# now do `window.location.href = redirect_url` with the value this route
# returns, instead of opening a Razorpay Checkout instance.
#
# NOTE: participant + event_registrations (team members) must now be sent
# to THIS endpoint (before payment), not to a later payment-success call —
# TiQR needs first_name/last_name/email/phone at booking-creation time, and
# there is no later step where the client can still attach that data once
# TiQR has redirected the user to pay.
# ======================================================

@app.route("/create-order", methods=["POST"])
@limiter.limit("20 per minute")
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
def create_order():
    user_id = _get_or_create_user_id()

    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    raw_ids = data.get("event_ids", [])
    try:
        event_ids = sorted(set(int(x) for x in raw_ids if str(x).isdigit()))
    except (TypeError, ValueError):
        event_ids = []

    if not event_ids:
        return jsonify({"error": "No events specified"}), 400

    participant = data.get("participant") or {}
    first_name   = (participant.get("first_name") or "").strip()
    last_name    = (participant.get("last_name") or "").strip()
    email        = (participant.get("email") or "").strip()
    phone_number = (participant.get("phone") or participant.get("phone_number") or "").strip()

    if not first_name or not email or not phone_number:
        return jsonify({"error": "first_name, email and phone are required"}), 400

    # Persist what they just entered onto their guest row in MySQL —
    # this is the "account" now: no separate login/register step.
    user_id = _update_user_details(
        user_id, (first_name + " " + last_name).strip(), email, phone_number
    )

    event_reg_map = {}
    for er in (data.get("event_registrations") or []):
        try:
            eid = int(er.get("event_id", 0))
        except (TypeError, ValueError):
            continue
        event_reg_map[eid] = er.get("members") or []

    # ── Ticket cap check (informational — re-checked atomically for real
    #    at webhook-confirm time before anything is reserved) ──
    try:
        ok, sold_out = _check_ticket_availability(event_ids)
        if not ok:
            return jsonify({
                "error": f"Sorry, tickets are sold out for: {', '.join(sold_out)}"
            }), 400
    except Exception as e:
        app.logger.warning(f"Ticket cap check error in create_order: {e}")

    promo = session.get("promo")
    promo_eligible = set(promo.get("eligible_ids") or event_ids) if promo else set()
    referral = session.get("referral")

    # ── Load events, split into free vs paid ────────────────────────────
    fmt = ",".join(["%s"] * len(event_ids))
    cursor = mysql.connection.cursor()
    cursor.execute(f"SELECT * FROM events WHERE id IN ({fmt})", tuple(event_ids))
    all_events = {row["id"]: row for row in cursor.fetchall()}
    cursor.close()

    missing = [eid for eid in event_ids if eid not in all_events]
    if missing:
        return jsonify({"error": f"Unknown event id(s): {missing}"}), 400

    free_event_ids = [eid for eid in event_ids if get_price(all_events[eid]) == 0]
    paid_event_ids = [eid for eid in event_ids if eid not in free_event_ids]

    eligible_total_price = sum(
        calculate_event_total(all_events[eid], max(1, len(event_reg_map.get(eid, []))))
        for eid in event_ids if eid in promo_eligible
    ) if promo and promo.get("type") == "flat" else 0

    # ── Free events: complete instantly, no TiQR involved ───────────────
    free_codes = []
    for eid in free_event_ids:
        code, err = _complete_paid_event(
            user_id, eid, promo, participant, event_reg_map.get(eid, []),
            payment_id="FREE", order_id="FREE_ORDER",
            eligible_total_price=eligible_total_price,
            send_email=False,
            referral=referral,
        )
        if err:
            return jsonify({"error": err}), 400
        free_codes.append(code)

    if free_codes:
        try:
            _send_combined_ticket_email(free_codes, request.host_url)
        except Exception as e:
            import traceback as _tb, sys
            _tb.print_exc(file=sys.stderr)
            app.logger.error(f"Combined ticket email failed for free codes {free_codes}: {e}")

    if free_event_ids:
        # Registration for these is already committed to MySQL above —
        # drop them from the cart now instead of waiting for a later
        # full-cart clear (which only happens if the whole checkout was free).
        try:
            db_remove_events_from_cart(user_id, free_event_ids)
        except Exception as e:
            app.logger.warning(f"create_order: could not remove free events from cart: {e}")

    if not paid_event_ids:
        # Everything was free — nothing to redirect for.
        try:
            db_clear_cart(user_id)
        except Exception:
            pass
        session["cart"] = []
        session.modified = True
        return jsonify({
            "free": True,
            "redirect_url": None,
            "ticket_codes": free_codes,
        })

    # ── Paid events: create one TiQR booking for the entire cart ───────

    cart_group_id = str(uuid.uuid4())
    callback_url = url_for("payment_complete", _external=True)

    # ── Single booking for the entire cart ───────────────────────────────
    # We always use ticket 3091 (custom_amount enabled) with the summed
    # cart total as custom_amount. This produces ONE booking → ONE TiQR
    # payment page → ONE checkout, regardless of how many events are in
    # the cart. The webhook fires once and we complete ALL event
    # registrations at that point.
    CUSTOM_AMOUNT_TICKET_ID = 3091

    # Sum up the full cart total (rupees). Per-member events are multiplied
    # by their actual team size entered in Step 2.
    cart_total_rupees = sum(
        calculate_event_total(
            all_events[eid],
            max(1, len(event_reg_map.get(eid, [])))
        )
        for eid in paid_event_ids
    )

    # ── Apply the promo discount to what we actually charge ─────────────
    # BUGFIX: this used to be skipped entirely, so a validated promo code
    # discounted the on-screen cart total but TiQR still charged the full
    # price via custom_amount below. Ticket 3091 is a custom-amount ticket
    # (see CUSTOM_AMOUNT_TICKET_ID above), so the discounted rupee amount
    # can simply be sent as-is — same per-event discount math as
    # _complete_paid_event uses for its bookkeeping.
    if promo and promo.get("code"):
        promo_paid_eligible_ids = promo_eligible & set(paid_event_ids)
        eligible_paid_total = sum(
            calculate_event_total(
                all_events[eid],
                max(1, len(event_reg_map.get(eid, [])))
            )
            for eid in paid_event_ids if eid in promo_paid_eligible_ids
        )
        if promo.get("type") == "percent":
            discount = round(eligible_paid_total * float(promo.get("value", 0)) / 100, 2)
        else:  # flat
            discount = min(float(promo.get("value", 0)), eligible_paid_total)
        cart_total_rupees = max(0, round(cart_total_rupees - discount, 2))

    # custom_amount is in paise (rupees × 100). quantity=1 because
    # custom_amount already encodes the full cart total.
    cart_booking_payload = {
        "first_name":    first_name,
        "last_name":     last_name,
        "phone_number":  phone_number,
        "email":         email,
        "ticket":        CUSTOM_AMOUNT_TICKET_ID,
        "quantity":      1,
        "custom_amount": int(cart_total_rupees * 100),
        "meta_data":     {"event_ids": paid_event_ids},
        "callback_url":  callback_url,
    }

    created = []  # list of (event_id, booking_uid, redirect_url)
    try:
        resp = _tiqr_post("/participant/booking/", cart_booking_payload)
        uid          = resp.get("booking", {}).get("uid")
        redirect_url = resp.get("payment", {}).get("url_to_redirect")
        if not uid or not redirect_url:
            raise ValueError(f"Unexpected TiQR response: {resp}")
        app.logger.info(
            f"TiQR cart booking uid={uid} total=₹{cart_total_rupees} "
            f"events={paid_event_ids} cart_group={cart_group_id}"
        )
        # All paid events share the same booking uid — they will all be
        # completed together when the webhook confirms payment.
        for eid in paid_event_ids:
            created.append((eid, uid, redirect_url))

    except Exception as e:
        app.logger.error(f"TiQR booking creation failed: {e}")
        return jsonify({
            "error": "Could not start payment with TiQR. Please try again, "
                     "or contact support if this keeps happening."
        }), 502

    # ── Persist ONE tiqr_bookings row for the entire cart ───────────────
    # All paid events share the same booking uid (one TiQR booking covers
    # the full cart). We store the complete per-event member map in
    # payload_json so the webhook can complete every event registration in
    # one go. event_id is set to 0 (sentinel) because this row represents
    # multiple events; the webhook reads event_registrations instead.
    cart_uid, cart_redirect = created[0][1], created[0][2]
    cart_payload = {
        "participant":          participant,
        "event_registrations":  [
            {
                "event_id": eid,
                "members":  event_reg_map.get(eid, []),
            }
            for eid in paid_event_ids
        ],
        "promo":                promo,
        "referral":             referral,
        "eligible_total_price": eligible_total_price,
    }
    try:
        # event_id=0 signals "multi-event cart booking" to the webhook.
        _save_tiqr_booking(cart_uid, cart_group_id, user_id, 0, cart_payload)
    except Exception as e:
        app.logger.error(f"Could not save tiqr_bookings row for uid={cart_uid}: {e}")
        return jsonify({"error": "Internal error starting payment. Please try again."}), 500

    session["tiqr_cart_group_id"] = cart_group_id
    session.modified = True

    app.logger.info(
        f"create_order: 1 cart booking uid={cart_uid} covers {len(paid_event_ids)} "
        f"event(s) — cart_group={cart_group_id}"
    )

    return jsonify({
        "free": False,
        "redirect_url": cart_redirect,
        "cart_group_id": cart_group_id,
        "free_ticket_codes": free_codes,
    })


# ======================================================
# TIQR WEBHOOK
# Called server-to-server by TiQR when a booking's payment status changes.
# This — NOT the browser redirect back to callback_url — is the trusted
# source of truth for completing a registration.
# ======================================================

def _finalize_tiqr_booking(record, status, booking_id=None):
    """
    Shared completion logic for a tiqr_bookings row that has just been
    determined to be 'confirmed' or 'failed' -- used by BOTH the
    /tiqr-webhook handler (the normal path) and the reconciliation
    fallback in /payment-complete (for when the webhook never arrives --
    e.g. a stale/misconfigured webhook URL in the TiQR dashboard).

    Caller must have already checked record["status"] == "pending"
    (idempotency) before calling this.

    Returns True if the booking ended up confirmed, False otherwise.
    """
    booking_uid = record["booking_uid"]

    if status != "confirmed":
        if status == "failed":
            _mark_tiqr_booking_status(booking_uid, "failed")
        return False

    try:
        saved = _json.loads(record["payload_json"])
    except Exception as e:
        app.logger.error(f"_finalize_tiqr_booking: could not parse payload_json for {booking_uid}: {e}")
        return False

    user_id   = record["user_id"]
    promo     = saved.get("promo")
    referral  = saved.get("referral")
    participant = saved.get("participant") or {}
    eligible_total_price = saved.get("eligible_total_price", 0)
    payment_id_val = booking_id or "TIQR"

    event_registrations = saved.get("event_registrations")
    if event_registrations:
        any_err = False
        confirmed_codes = []
        for er in event_registrations:
            eid     = er.get("event_id")
            members = er.get("members") or []
            code, err = _complete_paid_event(
                user_id, eid, promo, participant, members,
                payment_id=payment_id_val, order_id=booking_uid,
                eligible_total_price=eligible_total_price,
                send_email=False,
                referral=referral,
            )
            if err:
                app.logger.error(
                    f"_finalize_tiqr_booking: completion failed for uid={booking_uid} "
                    f"event_id={eid}: {err}"
                )
                any_err = True
            elif code:
                confirmed_codes.append(code)
        if any_err:
            _mark_tiqr_booking_status(booking_uid, "failed")
            return False
        if confirmed_codes:
            try:
                host_url = request.host_url
            except RuntimeError:
                host_url = os.environ.get("APP_BASE_URL", "https://anchorage2026.example.com/")
            try:
                _send_combined_ticket_email(confirmed_codes, host_url)
            except Exception as e:
                import traceback as _tb, sys
                _tb.print_exc(file=sys.stderr)
                app.logger.error(
                    f"_finalize_tiqr_booking: combined email failed for uid={booking_uid} "
                    f"codes={confirmed_codes}: {e}"
                )
        # Registrations are committed to MySQL above — drop these events
        # from the cart now (server-side), rather than only clearing it
        # if/when the browser lands back on /payment-complete.
        try:
            db_remove_events_from_cart(user_id, [er.get("event_id") for er in event_registrations])
        except Exception as e:
            app.logger.warning(f"_finalize_tiqr_booking: could not remove events from cart: {e}")
        _mark_tiqr_booking_status(booking_uid, "confirmed")
        if promo and promo.get("code"):
            try:
                _increment_promo_used(promo["code"])
            except Exception as e:
                app.logger.error(f"Promo increment error: {e}")
        return True
    else:
        code, err = _complete_paid_event(
            user_id, record["event_id"], promo,
            participant, saved.get("members") or [],
            payment_id=payment_id_val, order_id=booking_uid,
            eligible_total_price=eligible_total_price,
            referral=referral,
        )
        if err:
            app.logger.error(f"_finalize_tiqr_booking: completion failed for {booking_uid}: {err}")
            _mark_tiqr_booking_status(booking_uid, "failed")
            return False
        try:
            db_remove_events_from_cart(user_id, [record["event_id"]])
        except Exception as e:
            app.logger.warning(f"_finalize_tiqr_booking: could not remove event from cart: {e}")
        _mark_tiqr_booking_status(booking_uid, "confirmed")
        if promo and promo.get("code"):
            try:
                _increment_promo_used(promo["code"])
            except Exception as e:
                app.logger.error(f"Promo increment error: {e}")
        return True


def _poll_tiqr_booking_status(booking_uid):
    """
    Fallback for when TiQR's webhook hasn't arrived (misconfigured/stale
    webhook URL in the TiQR dashboard, dropped delivery, etc). Asks TiQR
    directly for the booking's current status via the public GET endpoint
    that _tiqr_get() already wraps.

    Returns 'confirmed' / 'failed' / 'pending' / None (lookup failed or
    response shape unrecognized).

    NOTE: field names below are a best-effort guess based on the shape of
    the POST /participant/booking/ response used elsewhere in this file.
    Verify the actual GET /participant/booking/:uid/ response shape
    against TiQR's docs/support and adjust if needed -- same
    "treat as unverified" caveat as the rest of the TiQR integration here.
    """
    try:
        resp = _tiqr_get(f"/participant/booking/{booking_uid}/")
    except Exception as e:
        app.logger.warning(f"_poll_tiqr_booking_status: TiQR GET failed for {booking_uid}: {e}")
        return None

    raw_status = (
        resp.get("booking", {}).get("status")
        or resp.get("payment", {}).get("status")
        or resp.get("status")
    )
    if not raw_status:
        app.logger.warning(
            f"_poll_tiqr_booking_status: unrecognized response shape for "
            f"{booking_uid}: {resp}"
        )
        return None

    raw_status = str(raw_status).lower()
    if raw_status in ("confirmed", "paid", "success", "charged"):
        return "confirmed"
    if raw_status in ("failed", "cancelled", "canceled"):
        return "failed"
    return "pending"


@app.route("/tiqr-webhook", methods=["POST"])
@csrf.exempt  # external caller, no session/CSRF token available
def tiqr_webhook():
    app.logger.info(f"tiqr_webhook: received call from {request.remote_addr}")
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"received": False, "error": "Invalid JSON"}), 400

    meta = payload.get("meta_data", {})
    booking_uid = meta.get("booking_uid")
    status = meta.get("booking_status")
    booking_id = meta.get("booking_id")

    if not booking_uid or not status:
        app.logger.warning(f"tiqr_webhook: missing booking_uid/status in payload: {payload}")
        return jsonify({"received": True}), 200

    record = _get_tiqr_booking(booking_uid)
    if not record:
        app.logger.error(f"tiqr_webhook: unknown booking_uid={booking_uid}")
        return jsonify({"received": True}), 200

    if record["status"] != "pending":
        return jsonify({"received": True}), 200

    _finalize_tiqr_booking(record, status, booking_id)
    return jsonify({"received": True}), 200


# ======================================================
# PAYMENT COMPLETE
# The page the user's browser lands on after TiQR's hosted payment page
# (this URL is what we sent as `callback_url`). The webhook above is what
# actually completes the registration — this page just reflects status
# back to the user, and may need to wait a moment for the webhook to land.
# ======================================================

@app.route("/payment-complete")
def payment_complete():
    cart_group_id = session.get("tiqr_cart_group_id") or request.args.get("cart_group_id")
    if not cart_group_id:
        return redirect(url_for("cart"))

    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT * FROM tiqr_bookings WHERE cart_group_id=%s",
        (cart_group_id,)
    )
    rows = cursor.fetchall()
    cursor.close()

    if not rows:
        return redirect(url_for("cart"))

    statuses = {r["status"] for r in rows}
    n = len(rows)

    # -- Reconciliation fallback -----------------------------------------
    # If our DB still thinks any row is pending, don't just trust that --
    # ask TiQR directly. This covers the case where TiQR's webhook never
    # arrives (e.g. a stale/misconfigured webhook URL in the TiQR
    # dashboard) so the page can self-heal instead of refreshing forever.
    if "pending" in statuses:
        reconciled_any = False
        for r in rows:
            if r["status"] != "pending":
                continue
            live_status = _poll_tiqr_booking_status(r["booking_uid"])
            if live_status in ("confirmed", "failed"):
                _finalize_tiqr_booking(r, live_status)
                reconciled_any = True
                continue

            # live_status is None (the GET poll itself failed -- e.g. TiQR's
            # endpoint 500ing for this uid) or "pending" (webhook genuinely
            # hasn't landed yet). As a last resort when the poll couldn't
            # tell us anything, fall back to the status TiQR embeds in this
            # redirect's own query string, e.g.
            # ?status=CHARGED&status_id=21&order_id=...&signature=...
            #
            # CAUTION: these params are NOT documented in TiQR's API docs
            # (only the server-to-server webhook payload is) -- this is
            # reverse-engineered from an observed redirect. There's also a
            # `signature` param that looks like it could be an HMAC, but we
            # don't have a confirmed signing secret/algorithm from TiQR to
            # verify it against, so we can't check it yet. Get that from
            # TiQR support and validate `signature` here before leaning on
            # this harder -- until then this is only a "the webhook is
            # probably just late/lost" nudge for a poll that already failed,
            # not an independently verified payment confirmation.
            if live_status is None:
                callback_status = request.args.get("status", "").upper()
                tiqr_order_id   = request.args.get("order_id", "")
                if callback_status == "CHARGED":
                    app.logger.warning(
                        f"payment_complete: TiQR poll failed for "
                        f"uid={r['booking_uid']}, falling back to unverified "
                        f"callback status=CHARGED (order_id={tiqr_order_id}) "
                        f"for cart_group={cart_group_id}"
                    )
                    _finalize_tiqr_booking(r, "confirmed", booking_id=tiqr_order_id)
                    reconciled_any = True
        if reconciled_any:
            cursor = mysql.connection.cursor()
            cursor.execute(
                "SELECT * FROM tiqr_bookings WHERE cart_group_id=%s",
                (cart_group_id,)
            )
            rows = cursor.fetchall()
            cursor.close()
            statuses = {r["status"] for r in rows}
            n = len(rows)

    if statuses == {"confirmed"}:
        session["cart"] = []
        session.pop("tiqr_cart_group_id", None)
        session.pop("promo", None)
        session.pop("referral", None)
        session.modified = True

        cursor = mysql.connection.cursor()
        fmt = ",".join(["%s"] * len(rows))
        cursor.execute(
            f"SELECT event_id, registration_code FROM registrations "
            f"WHERE order_id IN ({fmt}) AND user_id=%s",
            (*[r["booking_uid"] for r in rows], session.get("user_id"))
        )
        reg_rows = cursor.fetchall()
        cursor.close()
        codes = [c["registration_code"] for c in reg_rows]

        # Registrations are already committed to MySQL (and _finalize_tiqr_booking
        # already removed these events from the cart server-side when the
        # webhook landed) — this is just an idempotent safety net for the case
        # where this page reconciled the booking itself, above.
        try:
            db_remove_events_from_cart(
                session.get("user_id"), [r["event_id"] for r in reg_rows]
            )
        except Exception:
            pass

        # Send paid users to the exact same download-card success screen
        # free checkouts land on — event-register.html renders it from
        # ?tickets=CODE1,CODE2 (see _build_ticket_success_data / the
        # event_register route) instead of us hand-rolling a plain page here.
        return redirect(url_for("event_register", tickets=",".join(codes)))

    if "failed" in statuses:
        failed = sum(1 for r in rows if r["status"] == "failed")
        detail = (
            f"{failed} of {n} payments could not be completed."
            if n > 1 else "The payment could not be completed."
        )
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{background:#000810;color:#fff;font-family:monospace;padding:36px;font-size:14px}}</style>
</head><body>
<p style="color:#ff4466;font-size:1.2rem;margin-bottom:16px">✕ PAYMENT FAILED</p>
<p>{detail} Nothing was charged for the failed item(s).
Please return to your <a href="/cart" style="color:#00f2ff">cart</a> and try again.</p>
</body></html>"""

    # Still pending — webhook hasn't landed yet. Auto-refresh briefly.
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="3">
<style>body{{background:#000810;color:#fff;font-family:monospace;padding:36px;font-size:14px}}</style>
</head><body>
<p style="color:#ffd166;font-size:1.2rem;margin-bottom:16px">⏳ CONFIRMING PAYMENT…</p>
<p>This page will refresh automatically. Please don't close this tab.</p>
</body></html>"""

# ======================================================
# UPI MANUAL PAYMENT SUBMIT
# ======================================================
#
# Run this SQL once to create the upi_payments table:
#
# CREATE TABLE IF NOT EXISTS upi_payments (
#     id                  INT AUTO_INCREMENT PRIMARY KEY,
#     user_id             INT,
#     txn_id              VARCHAR(100) NOT NULL,
#     amount              DECIMAL(10,2) NOT NULL DEFAULT 0,
#     cloudinary_url      VARCHAR(512) DEFAULT NULL,   -- permanent public image URL
#     cloudinary_public_id VARCHAR(255) DEFAULT NULL,  -- needed if you ever want to delete it
#     screenshot_path     VARCHAR(512) DEFAULT NULL,   -- local disk fallback path (rarely used)
#     event_ids           TEXT DEFAULT NULL,
#     event_registrations TEXT DEFAULT NULL,
#     participant         TEXT DEFAULT NULL,
#     referral_code       VARCHAR(50) DEFAULT NULL,   -- Campus Ambassador code, if any
#     status              ENUM('pending','approved','rejected') NOT NULL DEFAULT 'pending',
#     admin_note          TEXT DEFAULT NULL,
#     created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#     updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
#     FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
# );
# CREATE INDEX idx_upi_user   ON upi_payments(user_id);
# CREATE INDEX idx_upi_status ON upi_payments(status);
# CREATE INDEX idx_upi_txn    ON upi_payments(txn_id);
#
# -- If the table already exists (migrating FROM the Google Drive version):
# ALTER TABLE upi_payments
#     MODIFY COLUMN user_id INT NULL,
#     ADD COLUMN cloudinary_url       VARCHAR(512) DEFAULT NULL AFTER screenshot_path,
#     ADD COLUMN cloudinary_public_id VARCHAR(255) DEFAULT NULL AFTER cloudinary_url,
#     DROP COLUMN drive_file_id,
#     DROP COLUMN drive_view_url;
#
# -- If the table already exists and just needs the referral column added
# -- (Campus Ambassador feature):
# ALTER TABLE upi_payments
#     ADD COLUMN referral_code VARCHAR(50) DEFAULT NULL AFTER participant;
#
# ======================================================
#
# CLOUDINARY SETUP (free tier — no credit card needed):
# 1. Sign up at https://cloudinary.com
# 2. Go to your Dashboard — copy the three values shown there:
#      Cloud name, API Key, API Secret
# 3. Set these as environment variables on your host:
#      CLOUDINARY_CLOUD_NAME = <cloud name>
#      CLOUDINARY_API_KEY    = <api key>
#      CLOUDINARY_API_SECRET = <api secret>
# 4. pip install cloudinary  (add "cloudinary" to requirements.txt)
#
# That's it — no folder sharing, no service accounts, no permissions
# dance. Every upload returns a permanent public URL immediately.
# ======================================================

import json as _json

UPI_SCREENSHOT_DIR = os.path.join("static", "upi_screenshots")

_cloudinary_configured = False


def _ensure_cloudinary_configured():
    """Configure the Cloudinary SDK once per process — reads env vars at call time."""
    global _cloudinary_configured
    if _cloudinary_configured:
        return True
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key    = os.environ.get("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
    if not (cloud_name and api_key and api_secret):
        app.logger.error(
            f"Cloudinary not configured — CLOUD_NAME set: {bool(cloud_name)}, "
            f"API_KEY set: {bool(api_key)}, API_SECRET set: {bool(api_secret)}"
        )
        return False
    try:
        import cloudinary
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        _cloudinary_configured = True
        app.logger.info(f"Cloudinary configured successfully (cloud: {cloud_name})")
        return True
    except Exception as e:
        app.logger.error(f"Cloudinary config failed: {type(e).__name__}: {e}")
        return False


def _upload_to_cloudinary(file_bytes, filename):
    """
    Upload file_bytes to Cloudinary under the 'upi_screenshots' folder.
    Returns (secure_url, public_id) or (None, None) on failure.
    """
    if not _ensure_cloudinary_configured():
        return None, None
    try:
        import cloudinary.uploader
        result = cloudinary.uploader.upload(
            BytesIO(file_bytes),
            folder="upi_screenshots",
            public_id=filename.rsplit(".", 1)[0],
            resource_type="image",
            overwrite=False,
        )
        secure_url = result.get("secure_url")
        public_id  = result.get("public_id")
        if not secure_url:
            app.logger.error(f"Cloudinary upload returned no secure_url: {result}")
            return None, None
        return secure_url, public_id
    except Exception as e:
        app.logger.error(f"Cloudinary upload failed: {type(e).__name__}: {e}")
        return None, None


def _ensure_screenshot_dir():
    os.makedirs(UPI_SCREENSHOT_DIR, exist_ok=True)


ALLOWED_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
# Pillow-verified format -> the extension we'll actually store/serve as.
_SAFE_IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
MAX_SCREENSHOT_SIZE   = 10 * 1024 * 1024   # 10 MB
MAX_SCREENSHOT_PIXELS = 40_000_000         # guards against decompression-bomb-style images


def _validate_and_normalize_image(file_bytes):
    """
    SECURITY: never trust a client-supplied filename extension or
    Content-Type header — both are attacker-controlled, so a malicious file
    can claim to be "screenshot.jpg" while containing anything at all.
    Instead: (1) verify the bytes actually decode as a real raster image via
    Pillow, then (2) re-encode them from the decoded pixel data before they
    are stored or uploaded anywhere. Re-encoding (rather than passing the
    original bytes through) is the part that matters — it discards anything
    appended/prepended to the genuine image data and drops embedded
    metadata, instead of just trusting that "Pillow could find an image
    somewhere in this blob" means the blob is safe to store as-is.
    Returns (clean_bytes, safe_ext) on success, or (None, error_message).
    """
    try:
        probe = Image.open(BytesIO(file_bytes))
        probe.verify()                              # cheap structural check
        img = Image.open(BytesIO(file_bytes))        # verify() invalidates the handle; reopen
        img.load()                                   # force full decode now, not lazily later
    except (UnidentifiedImageError, OSError, ValueError):
        return None, "That file isn't a valid image. Please upload a JPG, PNG, WEBP, or GIF screenshot."

    fmt = (img.format or "").upper()
    safe_ext = _SAFE_IMAGE_FORMATS.get(fmt)
    if not safe_ext:
        return None, "Unsupported image format. Please upload a JPG, PNG, WEBP, or GIF screenshot."

    width, height = img.size
    if width <= 0 or height <= 0 or width * height > MAX_SCREENSHOT_PIXELS:
        return None, "Image resolution is invalid or too large."

    out = BytesIO()
    try:
        if fmt == "JPEG":
            img.convert("RGB").save(out, format="JPEG", quality=90)
        elif fmt == "GIF":
            img.save(out, format="GIF", save_all=bool(getattr(img, "is_animated", False)))
        else:
            img.save(out, format=fmt)
    except Exception:
        return None, "Could not process that image. Please try a different file."

    return out.getvalue(), safe_ext


@app.route("/upi-payment-submit", methods=["POST"])
@limiter.limit("10 per hour")
@csrf.exempt  # fetch()-driven multipart endpoint; see CSRF note near the top of the file
def upi_payment_submit():
    """
    Handles UPI manual payment submission from the event-register page.
    Accepts multipart/form-data with:
      - txn_id              : UPI/UTR transaction ID (string, required)
      - amount              : amount in rupees (numeric, required)
      - screenshot          : image file (required)
      - event_ids           : JSON array of event IDs
      - event_registrations : JSON array of {event_id, type, members}
      - participant         : JSON object with primary participant details

    Saves the screenshot to disk, stores a pending record in upi_payments,
    and returns JSON {success, ref, message} or {error}.
    """
    user_id = _get_or_create_user_id()

    # ── 1. Parse fields ────────────────────────────────────────────────
    txn_id = (request.form.get("txn_id") or "").strip().upper()
    if not txn_id:
        return jsonify({"success": False, "error": "Transaction ID is required"}), 400

    try:
        amount = float(request.form.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid amount"}), 400

    event_ids_raw = request.form.get("event_ids", "[]")
    try:
        event_ids = _json.loads(event_ids_raw)
        if not isinstance(event_ids, list):
            event_ids = []
        event_ids = [int(x) for x in event_ids]
    except Exception:
        event_ids = []

    event_registrations_raw = request.form.get("event_registrations", "[]")
    participant_raw          = request.form.get("participant", "{}")

    try:
        _participant_for_user = _json.loads(participant_raw) if participant_raw else {}
        user_id = _update_user_details(
            user_id,
            ((_participant_for_user.get("first_name", "") + " " +
              _participant_for_user.get("last_name", "")).strip()),
            _participant_for_user.get("email", ""),
            _participant_for_user.get("phone", ""),
        )
    except Exception as e:
        app.logger.warning(f"upi_payment_submit: could not update user details: {e}")

    # Referral code: read from the already-validated session (set by
    # /validate-referral), not directly from the form — this way an
    # unvalidated/garbage code typed into the form can never be persisted
    # or later matched to an ambassador.
    referral_session = session.get("referral") or {}
    referral_code_to_store = referral_session.get("referral_code")

    # ── 2. Validate & save screenshot ─────────────────────────────────
    screenshot = request.files.get("screenshot")
    if not screenshot or screenshot.filename == "":
        return jsonify({"success": False, "error": "Payment screenshot is required"}), 400

    # Read into memory first — size check before doing any decode work.
    screenshot.stream.seek(0)
    file_bytes = screenshot.stream.read()
    if not file_bytes:
        return jsonify({"success": False, "error": "Uploaded file is empty"}), 400
    if len(file_bytes) > MAX_SCREENSHOT_SIZE:
        return jsonify({"success": False, "error": "Screenshot too large (max 10 MB)"}), 400

    # SECURITY: don't trust the filename extension or browser-supplied
    # Content-Type — validate the actual bytes and re-encode them.
    file_bytes, result = _validate_and_normalize_image(file_bytes)
    if file_bytes is None:
        return jsonify({"success": False, "error": result}), 400
    ext = result

    safe_filename = f"upi_{user_id}_{uuid.uuid4().hex[:8]}.{ext}"
    cloudinary_url, cloudinary_public_id = _upload_to_cloudinary(file_bytes, safe_filename)

    if not cloudinary_url:
        app.logger.error("Cloudinary upload failed — rejecting submission")
        return jsonify({"success": False, "error": "Failed to upload screenshot. Please check your connection and try again."}), 500

    save_path = None  # local disk no longer used

    # ── 3. Check for duplicate txn_id (prevent double submission) ─────
    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            "SELECT id FROM upi_payments WHERE txn_id = %s AND user_id = %s LIMIT 1",
            (txn_id, user_id)
        )
        dup = cursor.fetchone()
        if dup:
            cursor.close()
            return jsonify({
                "success": False,
                "error": f"A submission with Transaction ID '{txn_id}' already exists for your account."
            }), 409
    except Exception as e:
        app.logger.warning(f"upi duplicate check error: {e}")

    # ── 4. Ticket availability check ──────────────────────────────────
    if event_ids:
        try:
            ok, sold_out = _check_ticket_availability(event_ids)
            if not ok:
                cursor.close()
                return jsonify({
                    "success": False,
                    "error": f"Sorry, tickets are sold out for: {', '.join(sold_out)}"
                }), 400
        except Exception as e:
            app.logger.warning(f"upi_payment_submit cap check error: {e}")

    # ── 5. Insert pending record ───────────────────────────────────────
    ref = "UPI-" + uuid.uuid4().hex[:8].upper()
    try:
        cursor.execute(
            """
            INSERT INTO upi_payments
                (user_id, txn_id, amount, cloudinary_url, cloudinary_public_id, screenshot_path,
                 event_ids, event_registrations, participant, referral_code, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            """,
            (
                user_id,
                txn_id,
                amount,
                cloudinary_url,
                cloudinary_public_id,
                save_path,
                _json.dumps(event_ids),
                event_registrations_raw,
                participant_raw,
                referral_code_to_store,
            )
        )
        mysql.connection.commit()
    except Exception as e:
        cursor.close()
        app.logger.error(f"upi_payments insert error: {e}")
        return jsonify({"success": False, "error": f"Database error: {e}"}), 500

    cursor.close()

    # Pull these events out of the cart right away — a payment for them is
    # now pending review, so they shouldn't sit in the cart inviting a
    # second submission/payment before this one is approved or rejected.
    for eid in event_ids:
        try:
            db_remove_from_cart(user_id, eid)
        except Exception as e:
            app.logger.warning(f"upi_payment_submit: could not remove event {eid} from cart: {e}")
    session["cart"] = [eid for eid in session.get("cart", []) if eid not in event_ids]
    session.modified = True

    # ── 6. Send confirmation email to user ────────────────────────────
    try:
        participant_info = _json.loads(participant_raw) if participant_raw else {}
        user_name  = (
            (participant_info.get("first_name", "") + " " + participant_info.get("last_name", "")).strip()
            or session.get("user_name", "Participant")
        )
        user_email = participant_info.get("email") or session.get("user_email", "")

        if user_email:
            html_body = f"""
<div style="font-family:Arial,sans-serif;background:#000810;color:#fff;
            padding:32px;max-width:600px;margin:auto;
            border:1px solid #ffd16622;">
  <h1 style="color:#ffd166;font-size:20px;letter-spacing:4px;margin-bottom:4px;">
    ANCHORAGE 2026
  </h1>
  <p style="color:#ffd16688;font-size:12px;letter-spacing:3px;margin-bottom:24px;">
    UPI PAYMENT RECEIVED — PENDING VERIFICATION
  </p>
  <p style="font-size:16px;margin-bottom:8px;">Hello <strong>{user_name}</strong>,</p>
  <p style="color:#ccc;line-height:1.7;">
    We have received your UPI payment submission. Our team will verify it
    within <strong style="color:#ffd166;">24 hours</strong> and send your
    tickets to this email once confirmed.
  </p>
  <table style="margin:24px 0;border-collapse:collapse;width:100%;">
    <tr>
      <td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">
        TRANSACTION ID
      </td>
      <td style="padding:8px 0;color:#ffd166;font-size:13px;font-family:monospace;">
        {txn_id}
      </td>
    </tr>
    <tr>
      <td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">
        AMOUNT
      </td>
      <td style="padding:8px 0;color:#fff;font-size:13px;">
        &#8377;{int(amount)}
      </td>
    </tr>
    <tr>
      <td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">
        REFERENCE
      </td>
      <td style="padding:8px 0;color:#00f2ff;font-size:13px;font-family:monospace;">
        {ref}
      </td>
    </tr>
    <tr>
      <td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">
        STATUS
      </td>
      <td style="padding:8px 0;color:#ffd166;font-size:13px;">
        &#x23F0; Pending Verification
      </td>
    </tr>
  </table>
  <p style="color:#888;font-size:12px;line-height:1.6;">
    If you have any questions, reply to this email or contact our team.<br><br>
    &mdash; Team Anchorage 2026 &middot; Dept of Ship Technology, CUSAT
  </p>
</div>"""

            text_body = (
                f"Hello {user_name},\n\n"
                f"We received your UPI payment submission for Anchorage 2026.\n\n"
                f"Transaction ID : {txn_id}\n"
                f"Amount         : Rs.{int(amount)}\n"
                f"Reference      : {ref}\n"
                f"Status         : Pending Verification\n\n"
                f"Our team will verify within 24 hours and send your tickets.\n\n"
                f"-- Team Anchorage 2026"
            )

            _brevo_send(
                to_email=user_email,
                to_name=user_name,
                subject=f"[Anchorage 2026] UPI Payment Received — Ref: {ref}",
                html_body=html_body,
                text_body=text_body,
            )
    except Exception as e:
        # Non-fatal — log but don't fail the response
        app.logger.warning(f"upi confirmation email error: {e}")

    return jsonify({
        "success": True,
        "ref":     ref,
        "message": "Payment submitted successfully. You'll receive a confirmation email within 24 hours.",
    })


# ======================================================
# ADMIN — SERVE UPI SCREENSHOT (auth-gated)
# ======================================================
# Screenshots are stored in static/upi_screenshots/ but are NOT
# served publicly. This endpoint checks admin auth first,
# then streams the file so the raw path is never guessable.
# ======================================================

@app.route("/admin/upi-screenshot/<filename>")
def admin_upi_screenshot(filename):
    """
    Serve a local-disk UPI screenshot — admin only.
    This endpoint is only reached for payments where Cloudinary upload
    failed and the file was saved locally instead. Cloudinary-backed
    screenshots open directly via cloudinary_url and never hit this route.
    """
    if not _admin_auth():
        return "Unauthorised", 403

    safe_name = os.path.basename(filename)
    full_path = os.path.join(os.getcwd(), UPI_SCREENSHOT_DIR, safe_name)

    if not os.path.exists(full_path):
        return "Screenshot not found on disk — Cloudinary upload may have failed entirely.", 404

    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png",  "webp": "image/webp", "gif": "image/gif"}
    mime = mime_map.get(ext, "application/octet-stream")

    return send_file(full_path, mimetype=mime)


@app.route("/admin/cloudinary-debug")
def admin_cloudinary_debug():
    """
    Diagnostic route — admin only. Checks Cloudinary configuration and
    attempts a tiny live test upload so you can see exactly where the
    failure is, without going through the full registration flow.
    REMOVE OR COMMENT OUT once Cloudinary is confirmed working.
    """
    if not _admin_auth():
        return redirect(url_for("admin_login"))

    lines = []
    lines.append(f"CLOUDINARY_CLOUD_NAME set: {bool(CLOUDINARY_CLOUD_NAME)}")
    lines.append(f"CLOUDINARY_CLOUD_NAME value: {CLOUDINARY_CLOUD_NAME or '(empty)'}")
    lines.append(f"CLOUDINARY_API_KEY set: {bool(CLOUDINARY_API_KEY)}")
    lines.append(f"CLOUDINARY_API_SECRET set: {bool(CLOUDINARY_API_SECRET)}")

    configured = _ensure_cloudinary_configured()
    lines.append(f"Cloudinary configured: {'OK' if configured else 'FAILED'}")

    if configured:
        try:
            import cloudinary.uploader
            test_bytes = b"Anchorage 2026 Cloudinary connectivity test."
            result = cloudinary.uploader.upload(
                BytesIO(test_bytes),
                folder="upi_screenshots",
                public_id="cloudinary_debug_test",
                resource_type="image",
                overwrite=True,
            )
            secure_url = result.get("secure_url")
            if secure_url:
                lines.append(f"✓ TEST UPLOAD SUCCEEDED")
                lines.append(f"View it: {secure_url}")
            else:
                lines.append(f"❌ TEST UPLOAD RETURNED NO URL: {result}")
        except Exception as e:
            lines.append(f"❌ TEST UPLOAD FAILED: {type(e).__name__}: {e}")

    body = "<br>".join(lines)
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{background:#000810;color:#0f0;font-family:monospace;padding:30px;font-size:13px;line-height:1.8}}
a{{color:#00f2ff}}</style></head>
<body><h2 style="color:#fff">🔍 CLOUDINARY DEBUG</h2><br>{body}
<br><br><a href="/admin/upi-payments">← back</a></body></html>"""


# ======================================================
# TICKET LAYOUT + GENERATION
# Canvas: 941 × 1672 px
# Exact pixel positions matched to template.png.
# All main text fields render in YELLOW (#FFD700).
# ticket_id renders in CYAN (#00F5FF).
# payment_status excluded.
# font_size in each field is the MAXIMUM — text
# auto-shrinks to fit the box width.
# ======================================================

PRINT_AREAS = {
    "attendee_name": {"x": 490, "y": 520,  "width": 240, "height": 55, "font_size": 45, "align": "left",   "color": "#0B0B45"},
    "event_name":    {"x": 490, "y": 432,  "width": 240, "height": 55, "font_size": 30, "align": "center", "color": "#0B0B45"},

    "venue":         {"x": 490, "y": 690, "width": 240, "height": 55, "font_size": 26, "align": "left",   "color": "#0B0B45"},
    "ticket_id":     {"x": 490, "y": 610, "width": 240, "height": 55, "font_size": 24, "align": "left",   "color": "#0B0B45"},
    "qr_code":       {"x": 430, "y": 1135, "size": 375},
    "group_code":    {"x": 160, "y": 1235, "width": 250, "height": 30, "font_size": 18, "align": "left", "color": "#0B0B45"},
    "member_1": {"x": 237, "y": 869, "width": 390, "height": 42, "font_size": 18, "color": "#0B0B45"},
    "member_2": {"x": 237, "y": 921, "width": 390, "height": 42, "font_size": 18, "color": "#0B0B45"},
    "member_3": {"x": 237, "y": 973, "width": 390, "height": 42, "font_size": 18, "color": "#0B0B45"},
    "member_4": {"x": 237, "y": 1025, "width": 390, "height": 42, "font_size": 18, "color": "#0B0B45"},
}


def _build_ticket_image(ticket_data, host_url):
    """
    Opens the Anchorage 2026 ticket template (941×1672 px) and stamps all
    fields defined in PRINT_AREAS onto it.  Returns a BytesIO PNG buffer.

    Text rendering rules:
      • font_size is the MAXIMUM — auto-shrinks so text always fits box width.
      • align="center" horizontally centres text inside the box.
      • align="left"   left-aligns text from box x.
      • All fields use color #0B0B45 (see PRINT_AREAS).
    """
    FONTS_DIR     = os.path.join("static", "fonts")
    template_path = os.path.join("static", "tickets", "template.png")
    ticket        = Image.open(template_path).convert("RGBA")
    draw          = ImageDraw.Draw(ticket)

    # ── Font loader with in-call cache ────────────────────────────────────
    _FONT_CACHE: dict = {}

    def _load_font(font_filename, size):
        key = (font_filename, size)
        if key in _FONT_CACHE:
            return _FONT_CACHE[key]
        candidates = [
            os.path.join(FONTS_DIR, font_filename),
            os.path.join(FONTS_DIR, "Montserrat-Bold.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    fnt = ImageFont.truetype(path, size)
                    _FONT_CACHE[key] = fnt
                    return fnt
                except Exception:
                    pass
        fnt = ImageFont.load_default()
        _FONT_CACHE[key] = fnt
        return fnt

    def _fit_font(text, font_filename, max_size, max_width, min_size=8):
        """
        Return the largest font (≤ max_size, ≥ min_size) where the rendered
        text width fits within max_width pixels.  Fully scalable — tries every
        integer size from max down to min.
        """
        for size in range(max_size, min_size - 1, -1):
            fnt = _load_font(font_filename, size)
            bb  = fnt.getbbox(text)
            if (bb[2] - bb[0]) <= max_width:
                return fnt
        return _load_font(font_filename, min_size)

    MAIN_FONT = "Montserrat-Bold.ttf"
    MONO_FONT = "ShareTechMono-Regular.ttf"   # has ₹ glyph; falls back gracefully

    def _stamp(area, text, font_file=None):
        """
        Stamp text into a PRINT_AREAS box.
        • Scales font down from area["font_size"] until text fits area["width"].
        • Respects area["align"]: "center" or "left".
        • Uses area["color"].
        """
        if not text:
            return
        ff       = font_file or MAIN_FONT
        max_size = area["font_size"]
        box_w    = area["width"]
        box_x    = area["x"]
        box_y    = area["y"]
        color    = area.get("color", "#0B0B45")
        align    = area.get("align", "left")

        fnt = _fit_font(text, ff, max_size, box_w)
        bb  = fnt.getbbox(text)
        tw  = bb[2] - bb[0]

        if align == "center":
            tx = box_x + (box_w - tw) // 2
        else:
            tx = box_x

        draw.text((tx, box_y), text, font=fnt, fill=color)

    # ── Data ──────────────────────────────────────────────────────────────
    all_names = ticket_data.get("all_member_names", [])
    is_group  = ticket_data.get("is_group", False)

    # ── 1. ATTENDEE NAME — always the account holder only ─────────────────
    name_text = str(
        ticket_data.get("attendee_name", ticket_data.get("name", "—"))
    ).upper()
    _stamp(PRINT_AREAS["attendee_name"], name_text)

    # ── 2. EVENT NAME ─────────────────────────────────────────────────────
    event_text = str(ticket_data.get("event_name", "ANCHORAGE 2026")).upper()
    _stamp(PRINT_AREAS["event_name"], event_text)

    # ── 3. VENUE ──────────────────────────────────────────────────────────
    venue_text = str(
        ticket_data.get("venue", ticket_data.get("event_name", "ANCHORAGE 2026"))
    ).upper()
    _stamp(PRINT_AREAS["venue"], venue_text)

    # ── 4. TICKET ID ──────────────────────────────────────────────────────
    reg_code = str(ticket_data.get("registration_code", "—"))
    _stamp(PRINT_AREAS["ticket_id"], reg_code, font_file=MONO_FONT)

    # ── 5. GROUP MEMBERS (up to 4 rows) ────────────────────────────────────
    if is_group and all_names:
        member_slots = ["member_1", "member_2", "member_3", "member_4"]
        for idx, slot in enumerate(member_slots):
            if idx >= len(all_names):
                break
            nm = (all_names[idx] or "").strip()
            if nm:
                _stamp(PRINT_AREAS[slot], nm.upper())

    # ── 6. QR CODE ────────────────────────────────────────────────────────
    qr_cfg = PRINT_AREAS["qr_code"]
    qr_data = ticket_data["registration_code"]

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=2,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    qr_img = qr_img.resize((qr_cfg["size"], qr_cfg["size"]), Image.LANCZOS)
    ticket.paste(qr_img, (qr_cfg["x"], qr_cfg["y"]))

    # ── Return as PNG ──────────────────────────────────────────────────────
    output = BytesIO()
    ticket.convert("RGB").save(output, format="PNG", dpi=(150, 150))
    output.seek(0)
    return output


def _get_ticket_data(registration_code):
    """Fetch all fields needed to fill the ticket from the DB, including team members."""
    cursor = mysql.connection.cursor()
    cursor.execute(
        """
        SELECT
            r.registration_code,
            r.amount_paid,
            r.created_at,
            COALESCE(rm.member_name,  u.name)  AS name,
            COALESCE(rm.member_email, u.email) AS email,
            e.title                                AS event_title,
            COALESCE(e.event_date, '2026-01-10') AS event_date,
            COALESCE(e.event_time, '00:00:00')   AS event_time
        FROM registrations r
        JOIN users  u ON r.user_id  = u.id
        JOIN events e ON r.event_id = e.id
        LEFT JOIN registration_members rm
               ON rm.registration_code = r.registration_code
              AND rm.member_order = 0
        WHERE r.registration_code = %s
        """,
        (registration_code,)
    )
    row = cursor.fetchone()

    if row:
        try:
            cursor.execute(
                """SELECT member_name, member_email, member_phone,
                          member_college, member_dept, member_order
                   FROM   registration_members
                   WHERE  registration_code = %s
                   ORDER  BY member_order ASC""",
                (registration_code,)
            )
            members = cursor.fetchall()
            row = dict(row)
            row["team_members"] = list(members) if members else []
        except Exception as e:
            app.logger.warning(f"Could not fetch team members for {registration_code}: {e}")
            row = dict(row)
            row["team_members"] = []

    cursor.close()
    return row


def _build_ticket_payload(row):
    """Convert a DB row into the dict _build_ticket_image() expects."""
    if not row:
        return None

    # Format date
    import datetime
    ev_date = row.get("event_date")
    if ev_date:
        try:
            if isinstance(ev_date, (datetime.date, datetime.datetime)):
                ev_date = ev_date.strftime("%d %b %Y")
            elif isinstance(ev_date, str):
                # Parse YYYY-MM-DD string from COALESCE or DB
                parsed = datetime.datetime.strptime(ev_date[:10], "%Y-%m-%d")
                ev_date = parsed.strftime("%d %b %Y")
            else:
                ev_date = str(ev_date)
        except Exception:
            ev_date = str(ev_date)
    else:
        ev_date = "10 Jan 2026"

    # Format time
    ev_time = row.get("event_time")
    if ev_time:
        try:
            if isinstance(ev_time, datetime.timedelta):
                total_seconds = int(ev_time.total_seconds())
                hours, remainder = divmod(abs(total_seconds), 3600)
                minutes = remainder // 60
                suffix = "AM" if hours < 12 else "PM"
                hours = hours % 12 or 12
                ev_time = f"{hours:02d}:{minutes:02d} {suffix}"
            elif isinstance(ev_time, str):
                # Parse HH:MM:SS string from COALESCE or DB
                parts = ev_time.split(":")
                h, m = int(parts[0]), int(parts[1])
                suffix = "AM" if h < 12 else "PM"
                h = h % 12 or 12
                ev_time = f"{h:02d}:{m:02d} {suffix}"
            else:
                ev_time = str(ev_time)
        except Exception:
            ev_time = str(ev_time)
    else:
        ev_time = "12:00 AM"

    amount = row.get("amount_paid", 0) or 0
    amount_str = "FREE" if int(amount) == 0 else f"\u20B9{int(amount)}"

    # Build attendee display name:
    # Always show ONLY the account holder's name in the attendee_name field.
    # Group member names are shown separately in the group members section.
    raw_members = row.get("team_members") or []
    all_member_names = [
        (m.get("member_name") or "").strip()
        for m in raw_members
        if (m.get("member_name") or "").strip()
    ]

    # attendee_name is always just the account holder (registrant) name
    attendee_display = row["name"].upper()

    # teammates_csv = all members except the leader (member_order > 0)
    has_order = raw_members and "member_order" in raw_members[0]
    if has_order:
        teammate_names = [
            (m.get("member_name") or "").strip()
            for m in raw_members
            if m.get("member_order", 0) > 0 and (m.get("member_name") or "").strip()
        ]
    else:
        teammate_names = [n for n in all_member_names[1:] if n]

    teammates_csv = ", ".join(teammate_names) if teammate_names else ""

    return {
        "registration_code": row["registration_code"],
        "name":              row["name"],
        "attendee_name":     attendee_display,
        "attendee_id":       str(row.get("attendee_id") or row.get("college_id") or "").upper(),
        "email":             row["email"],
        "event_name":        (row.get("event_title") or "ANCHORAGE 2026").upper(),
        "event_date":        ev_date,
        "event_time":        ev_time,
        "venue":             str(row.get("venue") or "").upper(),
        "amount_paid":       amount,
        "amount_paid_str":   amount_str,
        "team_members":      raw_members,
        "all_member_names":  all_member_names,
        "teammates_csv":     teammates_csv,
        "is_group":          len(all_member_names) > 1,
    }


# ======================================================
# GENERATE QR CODE  (standalone endpoint)
# ======================================================

@app.route("/qr/<registration_code>")
def generate_dynamic_qr(registration_code):
    qr = qrcode.QRCode(version=None, box_size=10, border=5)
    qr.add_data(registration_code)
    qr.make(fit=True)

    img    = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)

    return send_file(buffer, mimetype="image/png")


# ======================================================
# DEBUG TICKET ENDPOINT — REMOVE BEFORE PRODUCTION
# Visit /ticket-debug/<any-valid-code> to diagnose 500s
# ======================================================

@app.route("/ticket-debug/<registration_code>")
def debug_ticket(registration_code):
    # Locked down: this leaks filesystem paths, DB row contents, and full
    # tracebacks, so it must never be reachable outside local debugging by
    # an authenticated admin.
    if not DEBUG or not _admin_auth():
        return "Not found", 404

    import traceback
    result = {}

    # 1. Check template
    template_path = os.path.join("static", "tickets", "template.png")
    result["template_path"]   = template_path
    result["template_exists"] = os.path.exists(template_path)
    result["cwd"]             = os.getcwd()

    if result["template_exists"]:
        try:
            img = Image.open(template_path)
            result["template_size"] = list(img.size)
            result["template_mode"] = img.mode
        except Exception as e:
            result["template_open_error"] = str(e)

    # 2. Check fonts
    font_candidates = [
        os.path.join("static", "fonts", "ShareTechMono-Regular.ttf"),
        os.path.join("static", "fonts", "Orbitron-Regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    result["fonts"] = {f: os.path.exists(f) for f in font_candidates}

    # 3. Fetch DB row
    try:
        row = _get_ticket_data(registration_code)
        result["db_row_found"] = row is not None
        if row:
            result["db_row_keys"] = list(row.keys())
    except Exception as e:
        result["db_error"] = str(e)
        result["db_traceback"] = traceback.format_exc()

    # 4. Try building ticket end-to-end
    if result.get("db_row_found") and result.get("template_exists"):
        try:
            payload = _build_ticket_payload(row)
            result["payload_ok"] = True
            output  = _build_ticket_image(payload, request.host_url)
            result["ticket_build"] = "SUCCESS"
            result["output_bytes"] = output.getbuffer().nbytes
        except Exception as e:
            result["ticket_build"] = "FAILED"
            result["ticket_error"] = str(e)
            result["ticket_traceback"] = traceback.format_exc()

    return jsonify(result)


# ======================================================
# GENERATE & DOWNLOAD TICKET IMAGE
# GET /ticket/<code>          → inline preview (browser)
# GET /ticket/<code>?download=1 → download attachment
# ======================================================

@app.route("/ticket/<registration_code>")
def generate_ticket(registration_code):
    try:
        row = _get_ticket_data(registration_code)
    except Exception as e:
        app.logger.error(f"generate_ticket DB error for {registration_code}: {e}")
        return "Database error retrieving ticket", 500

    if not row:
        return "Invalid Ticket", 404

    payload = _build_ticket_payload(row)
    if not payload:
        return "Ticket data error", 500

    # Check template exists before trying to build
    template_path = os.path.join("static", "tickets", "template.png")
    if not os.path.exists(template_path):
        app.logger.error(f"Ticket template not found at {template_path}")
        return (
            f"Ticket template missing. "
            f"Please place your ticket template PNG at: static/tickets/template.png",
            500
        )

    try:
        output = _build_ticket_image(payload, request.host_url)
    except Exception as e:
        app.logger.error(f"generate_ticket image build error for {registration_code}: {e}")
        return f"Ticket generation failed: {e}", 500

    download = request.args.get("download", "0") == "1"
    filename = f"anchorage2026_ticket_{registration_code}.png"

    return send_file(
        output,
        mimetype="image/png",
        as_attachment=download,
        download_name=filename
    )


# ======================================================
# EMAIL TICKET  (internal helper + public endpoint)
# ======================================================

def _get_ticket_data_direct(registration_code):
    """
    FIX 2: Thread-safe version of _get_ticket_data that opens its OWN
    pymysql connection instead of relying on Flask's g (which is
    request-scoped and unavailable in background threads).
    Caller is responsible for handling exceptions.
    """
    conn = pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        port=MYSQL_PORT,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        charset="utf8mb4",
        connect_timeout=10,
    )
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                r.registration_code,
                r.amount_paid,
                r.created_at,
                COALESCE(rm.member_name,  u.name)  AS name,
                COALESCE(rm.member_email, u.email) AS email,
                e.title                                AS event_title,
                COALESCE(e.event_date, '2026-01-10') AS event_date,
                COALESCE(e.event_time, '00:00:00')   AS event_time
            FROM registrations r
            JOIN users  u ON r.user_id  = u.id
            JOIN events e ON r.event_id = e.id
            LEFT JOIN registration_members rm
                   ON rm.registration_code = r.registration_code
                  AND rm.member_order = 0
            WHERE r.registration_code = %s
            """,
            (registration_code,)
        )
        row = cursor.fetchone()

        if row:
            try:
                cursor.execute(
                    """SELECT member_name, member_email, member_phone,
                              member_college, member_dept, member_order
                       FROM   registration_members
                       WHERE  registration_code = %s
                       ORDER  BY member_order ASC""",
                    (registration_code,)
                )
                members = cursor.fetchall()
                row = dict(row)
                row["team_members"] = list(members) if members else []
            except Exception as e:
                app.logger.warning(f"Could not fetch team members for {registration_code}: {e}")
                row = dict(row)
                row["team_members"] = []

        cursor.close()
        return row
    finally:
        conn.close()


def _send_ticket_email(registration_code, host_url):
    """
    Builds the ticket PNG image and sends it via Brevo HTTP API.
    Uses _get_ticket_data_direct() (its own DB connection) so it is
    safe to call from background threads where Flask g is unavailable.
    No SMTP — works on Render free tier.
    Raises on failure so callers can log/handle.
    """
    import sys, traceback as _tb

    # ── 1. Fetch DB data (thread-safe, own connection) ──────────────────
    try:
        row = _get_ticket_data_direct(registration_code)
    except Exception as e:
        _tb.print_exc(file=sys.stderr)
        raise RuntimeError(f"DB fetch failed for {registration_code}: {e}")

    if not row:
        raise ValueError(f"No registration found for code: {registration_code}")

    # ── 2. Build ticket payload ─────────────────────────────────────────
    try:
        payload = _build_ticket_payload(row)
    except Exception as e:
        _tb.print_exc(file=sys.stderr)
        raise RuntimeError(f"Ticket payload build failed: {e}")

    if not payload:
        raise RuntimeError(f"Ticket payload is empty for {registration_code}")

    # ── 3. Generate ticket PNG image ─────────────────────────────────────
    template_path = os.path.join("static", "tickets", "template.png")
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f"Ticket template not found at {template_path}. "
            "Place template.png in static/tickets/ and redeploy."
        )

    try:
        ticket_buf = _build_ticket_image(payload, host_url)
        ticket_buf.seek(0)
        img_bytes = ticket_buf.read()
        if not img_bytes:
            raise RuntimeError("Generated ticket image is empty (0 bytes)")
    except Exception as e:
        _tb.print_exc(file=sys.stderr)
        raise RuntimeError(f"Ticket image generation failed for {registration_code}: {e}")

    # ── 4. Build email content ────────────────────────────────────────────
    teammates_csv = payload.get("teammates_csv", "")
    team_html_row = (
        f"<tr><td style='padding:8px 0;color:#888;font-size:13px;letter-spacing:2px'>TEAM MEMBERS</td>"
        f"<td style='padding:8px 0;color:#fff;font-size:13px'>{teammates_csv}</td></tr>"
        if teammates_csv else ""
    )
    team_txt_line = f"Team Members: {teammates_csv}\n" if teammates_csv else ""

    subject = f"\U0001f39f\ufe0f Your Anchorage 2026 Ticket \u2014 {payload['event_name']}"

    html_body = f"""
<div style="font-family:Arial,sans-serif;background:#000810;color:#fff;padding:32px;max-width:600px;margin:auto;border:1px solid #00f2ff22;">
    <h1 style="color:#00f2ff;font-size:22px;letter-spacing:4px;margin-bottom:4px;">ANCHORAGE 2026</h1>
    <p style="color:#00f2ff88;font-size:12px;letter-spacing:3px;margin-bottom:24px;">NAVIGATE &middot; INNOVATE &middot; DOMINATE</p>
    <p style="font-size:16px;margin-bottom:8px;">Hello <strong>{row['name']}</strong>,</p>
    <p style="color:#ccc;line-height:1.7;">
        Your registration for <strong style="color:#00f2ff;">{payload['event_name']}</strong> is confirmed!
        Your ticket PNG is attached to this email.
    </p>
    <table style="margin:24px 0;border-collapse:collapse;width:100%;">
        <tr><td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">EVENT</td>
            <td style="padding:8px 0;color:#fff;font-size:13px;">{payload['event_name']}</td></tr>
        <tr><td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">DATE</td>
            <td style="padding:8px 0;color:#fff;font-size:13px;">{payload['event_date']}</td></tr>
        <tr><td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">TIME</td>
            <td style="padding:8px 0;color:#fff;font-size:13px;">{payload['event_time']}</td></tr>
        <tr><td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">REGISTRANT</td>
            <td style="padding:8px 0;color:#fff;font-size:13px;">{row['name']}</td></tr>
        {team_html_row}
        <tr><td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">AMOUNT PAID</td>
            <td style="padding:8px 0;color:#00f2ff;font-size:13px;">{payload['amount_paid_str']}</td></tr>
        <tr><td style="padding:8px 0;color:#888;font-size:13px;letter-spacing:2px;">TICKET ID</td>
            <td style="padding:8px 0;color:#ffd166;font-size:13px;font-family:monospace;">{row['registration_code']}</td></tr>
    </table>
    <p style="color:#888;font-size:12px;line-height:1.6;">
        &#9875; Keep this pass safe &middot; Verify at counter<br>
        Scan the QR code on your ticket at the entrance for quick check-in.<br><br>
        Download ticket anytime: <a href="{host_url}ticket/{registration_code}" style="color:#00f2ff;">{host_url}ticket/{registration_code}</a>
    </p>
    <p style="margin-top:24px;color:#666;font-size:11px;">&mdash; Team Anchorage 2026 &middot; Dept of Ship Technology, CUSAT</p>
</div>"""

    text_body = (
        f"Hello {row['name']},\n\n"
        f"Your registration is confirmed!\n\n"
        f"Event      : {payload['event_name']}\n"
        f"Date       : {payload['event_date']}\n"
        f"Time       : {payload['event_time']}\n"
        f"Registrant : {row['name']}\n"
        f"{team_txt_line}"
        f"Amount Paid: {payload['amount_paid_str']}\n"
        f"Ticket ID  : {row['registration_code']}\n\n"
        f"Your ticket PNG is attached to this email.\n"
        f"Download anytime: {host_url}ticket/{registration_code}\n\n"
        f"Present the ticket at the venue.\n\n"
        f"-- Team Anchorage 2026"
    )

    # ── 5. Send via Brevo HTTP API ────────────────────────────────────────
    # Only the primary registrant gets the email — team members' tickets
    # are included in the same attachment, but no separate mail is sent
    # to each of them.
    try:
        _brevo_send(
            to_email=row["email"],
            to_name=row["name"],
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            attachment_bytes=img_bytes,
            attachment_filename=f"anchorage2026_ticket_{row['registration_code']}.png",
        )
        app.logger.info(
            f"Ticket email sent via Brevo to {row['email']} for {registration_code} "
            f"(attachment: {len(img_bytes)} bytes)"
        )
    except Exception as e:
        _tb.print_exc(file=sys.stderr)
        raise RuntimeError(f"Brevo send failed for {registration_code}: {e}")


def _send_combined_ticket_email(registration_codes, host_url):
    """
    Sends a SINGLE confirmation email covering one or more registration
    codes (e.g. 2-3 events bought together in one checkout / one approved
    UPI submission), with one ticket PNG attached per event — instead of
    firing a separate email per event.

    Recipient = the primary registrant's email only. Team members are
    listed inside that one email but do not each receive their own copy.

    Raises only if NOT EVEN ONE ticket/email could be sent; per-ticket
    failures are logged and skipped so the rest still go out.
    """
    import sys, traceback as _tb

    registration_codes = [c for c in (registration_codes or []) if c]
    if not registration_codes:
        return

    # ── 1. Build every ticket (own DB fetch + PNG) ───────────────────────
    tickets, build_errors = [], []
    for code in registration_codes:
        try:
            row = _get_ticket_data_direct(code)
            if not row:
                raise ValueError(f"No registration found for code: {code}")
            payload = _build_ticket_payload(row)
            if not payload:
                raise RuntimeError(f"Ticket payload build failed for {code}")
            ticket_buf = _build_ticket_image(payload, host_url)
            ticket_buf.seek(0)
            img_bytes = ticket_buf.read()
            if not img_bytes:
                raise RuntimeError(f"Generated ticket image is empty (0 bytes) for {code}")
            tickets.append({"code": code, "row": row, "payload": payload, "img_bytes": img_bytes})
        except Exception as e:
            _tb.print_exc(file=sys.stderr)
            build_errors.append(f"{code}: {e}")
            app.logger.error(f"Combined ticket build failed for {code}: {e}")

    if not tickets:
        raise RuntimeError(f"Could not build any tickets to email: {'; '.join(build_errors)}")

    primary_row   = tickets[0]["row"]
    primary_name  = primary_row.get("name") or "Participant"
    primary_email = (primary_row.get("email") or "").strip()
    n = len(tickets)

    # ── 2. Subject + one combined HTML/text body listing every event ────
    subject = (
        f"\U0001f39f\ufe0f Your Anchorage 2026 Ticket \u2014 {tickets[0]['payload']['event_name']}"
        if n == 1 else
        f"\U0001f39f\ufe0f Your Anchorage 2026 Tickets \u2014 {n} Events Confirmed"
    )

    rows_html, rows_txt = "", ""
    for t in tickets:
        p, r = t["payload"], t["row"]
        teammates_csv = p.get("teammates_csv", "")
        team_html = (
            f"<div style='color:#888;font-size:12px;margin-top:2px;'>Team: {teammates_csv}</div>"
            if teammates_csv else ""
        )
        rows_html += f"""
        <tr>
          <td style="padding:10px 0;border-top:1px solid #ffffff14;color:#fff;font-size:13px;">
            <strong style="color:#00f2ff;">{p['event_name']}</strong><br>
            <span style="color:#888;font-size:12px;">{p['event_date']} &middot; {p['event_time']}</span>
            {team_html}
          </td>
          <td style="padding:10px 0;border-top:1px solid #ffffff14;color:#ffd166;font-size:12px;font-family:monospace;text-align:right;">
            {r['registration_code']}
          </td>
          <td style="padding:10px 0;border-top:1px solid #ffffff14;color:#00f2ff;font-size:13px;text-align:right;">
            {p['amount_paid_str']}
          </td>
        </tr>"""
        rows_txt += (
            f"- {p['event_name']} | {p['event_date']} {p['event_time']} | "
            f"Ticket: {r['registration_code']} | {p['amount_paid_str']}\n"
        )
        if teammates_csv:
            rows_txt += f"    Team: {teammates_csv}\n"

    intro = (
        f"Your registration for <strong style=\"color:#00f2ff;\">{tickets[0]['payload']['event_name']}</strong> is confirmed!"
        if n == 1 else
        f"Your registrations for <strong style=\"color:#00f2ff;\">{n} events</strong> are confirmed!"
    )
    attach_note   = "Your ticket PNG is attached" if n == 1 else f"All {n} ticket PNGs are attached"
    intro_txt     = (
        "Your registration is confirmed!" if n == 1 else f"Your registrations for {n} events are confirmed!"
    )

    html_body = f"""
<div style="font-family:Arial,sans-serif;background:#000810;color:#fff;padding:32px;max-width:600px;margin:auto;border:1px solid #00f2ff22;">
    <h1 style="color:#00f2ff;font-size:22px;letter-spacing:4px;margin-bottom:4px;">ANCHORAGE 2026</h1>
    <p style="color:#00f2ff88;font-size:12px;letter-spacing:3px;margin-bottom:24px;">NAVIGATE &middot; INNOVATE &middot; DOMINATE</p>
    <p style="font-size:16px;margin-bottom:8px;">Hello <strong>{primary_name}</strong>,</p>
    <p style="color:#ccc;line-height:1.7;">
        {intro} {attach_note} to this email.
    </p>
    <table style="margin:24px 0;border-collapse:collapse;width:100%;">
      <tr>
        <td style="padding:0 0 8px 0;color:#888;font-size:11px;letter-spacing:2px;">EVENT</td>
        <td style="padding:0 0 8px 0;color:#888;font-size:11px;letter-spacing:2px;text-align:right;">TICKET ID</td>
        <td style="padding:0 0 8px 0;color:#888;font-size:11px;letter-spacing:2px;text-align:right;">PAID</td>
      </tr>
      {rows_html}
    </table>
    <p style="color:#888;font-size:12px;line-height:1.6;">
        &#9875; Keep {"this pass" if n == 1 else "these passes"} safe &middot; Verify at counter<br>
        Scan the QR code on {"the ticket" if n == 1 else "each ticket"} at the entrance for quick check-in.
    </p>
    <p style="margin-top:24px;color:#666;font-size:11px;">&mdash; Team Anchorage 2026 &middot; Dept of Ship Technology, CUSAT</p>
</div>"""

    text_body = (
        f"Hello {primary_name},\n\n"
        f"{intro_txt}\n\n"
        f"{rows_txt}\n"
        f"{attach_note}.\n\n"
        f"Present each ticket at the venue.\n\n"
        f"-- Team Anchorage 2026"
    )

    attachments = [
        {"bytes": t["img_bytes"], "filename": f"anchorage2026_ticket_{t['code']}.png"}
        for t in tickets
    ]

    # ── 3. Recipient = the primary registrant only ──────────────────────
    # (Team members' names/tickets still appear inside the one email —
    # they just don't each get their own separate copy.)
    if not primary_email:
        raise RuntimeError("No recipient email address found for combined ticket email")
    recipients = {primary_email.lower(): primary_name}

    # ── 4. One email per recipient, all tickets attached ────────────────
    send_errors, sent_any = [], False
    for email_addr, name in recipients.items():
        try:
            _brevo_send(
                to_email=email_addr,
                to_name=name,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
                attachments=attachments,
            )
            sent_any = True
            app.logger.info(
                f"Combined ticket email sent to {email_addr} for codes: "
                f"{[t['code'] for t in tickets]}"
            )
        except Exception as e:
            send_errors.append(f"{email_addr}: {e}")
            app.logger.warning(f"Combined ticket email failed for {email_addr}: {e}")

    if not sent_any:
        raise RuntimeError(f"Brevo send failed for all recipients: {'; '.join(send_errors)}")


@app.route("/send-ticket/<registration_code>")
def send_ticket(registration_code):
    """
    Public endpoint to (re-)send the ticket email.
    Called from the success page "RESEND EMAIL" button.
    Uses Brevo HTTP API directly — fast enough to call synchronously.
    """
    host_url = request.host_url
    try:
        _send_ticket_email(registration_code, host_url)
        app.logger.info(f"Resend ticket email OK for {registration_code}")
        return jsonify({"success": True, "message": "Ticket sent to your email."})
    except Exception as e:
        import traceback as _tb, sys
        _tb.print_exc(file=sys.stderr)
        app.logger.error(f"send_ticket error for {registration_code}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ======================================================
# QR SCAN / ENTRY VERIFICATION
# ======================================================

@app.route("/scan/<registration_code>")
def scan_ticket(registration_code):
    cursor = mysql.connection.cursor()
    cursor.execute(
        """
        SELECT r.*, COALESCE(rm.member_name, u.name) AS name,
               COALESCE(rm.member_email, u.email) AS email,
               e.title AS event_title,
               COALESCE(e.event_date, '2026-01-10') AS event_date,
               COALESCE(e.event_time, '00:00:00')   AS event_time
        FROM   registrations r
        JOIN   users  u ON r.user_id  = u.id
        JOIN   events e ON r.event_id = e.id
        LEFT JOIN registration_members rm
               ON rm.registration_code = r.registration_code
              AND rm.member_order = 0
        WHERE  r.registration_code = %s
        """,
        (registration_code,)
    )
    ticket = cursor.fetchone()

    if not ticket:
        cursor.close()
        return _scan_page("invalid", "INVALID TICKET",
                          "No registration found for this code.",
                          registration_code, None, []), 404

    members = []
    try:
        cursor.execute(
            """SELECT member_name, member_email, member_phone,
                      member_college, member_dept, member_order
               FROM   registration_members
               WHERE  registration_code = %s
               ORDER  BY member_order ASC""",
            (registration_code,)
        )
        members = cursor.fetchall() or []
    except Exception as e:
        app.logger.warning(f"scan_ticket: could not fetch members: {e}")

    already_in = bool(ticket.get("checked_in"))
    if not already_in:
        cursor.execute(
            "UPDATE registrations SET checked_in=TRUE, checked_in_time=NOW() WHERE registration_code=%s",
            (registration_code,)
        )
        mysql.connection.commit()
    cursor.close()

    if already_in:
        return _scan_page("duplicate", "ALREADY CHECKED IN",
                          "This ticket was already scanned.",
                          registration_code, ticket, members)

    return _scan_page("success", "ENTRY VERIFIED",
                      "Ticket is valid. Welcome to Anchorage 2026!",
                      registration_code, ticket, members)


def _scan_page(status, title, subtitle, code, ticket, members):
    COLORS = {"success": ("#06d6a0","#0a2e22"), "duplicate": ("#ffd166","#2e2200"), "invalid": ("#ff4466","#2e0011")}
    ICONS  = {"success": "✅", "duplicate": "⚠️", "invalid": "❌"}
    accent, bg_tint = COLORS.get(status, ("#00f2ff","#000810"))
    icon = ICONS.get(status, "❓")

    member_html = ""
    if members:
        all_csv = ", ".join((m.get("member_name") or "").strip() for m in members if (m.get("member_name") or "").strip())
        rows = ""
        for i, m in enumerate(members):
            name  = (m.get("member_name")    or "—").strip()
            meta  = " · ".join(filter(None, [
                (m.get("member_college") or "").strip(),
                (m.get("member_dept")    or "").strip(),
                (m.get("member_phone")   or "").strip(),
                (m.get("member_email")   or "").strip(),
            ]))
            badge = ('<span style="color:#00f2ff;font-size:10px;letter-spacing:2px;border:1px solid #00f2ff44;padding:2px 7px">LEADER</span>'
                     if m.get("member_order", i) == 0
                     else f'<span style="color:#ffffff44;font-size:11px">#{i+1}</span>')
            rows += f"<tr><td style='padding:9px 4px;border-bottom:1px solid #ffffff0a;vertical-align:top'>{badge}</td><td style='padding:9px 8px;border-bottom:1px solid #ffffff0a'><div style='color:#fff;font-size:15px;font-weight:600'>{name}</div><div style='color:#ffffff55;font-size:11px'>{meta}</div></td></tr>"

        member_html = f"""<div style="margin-top:22px">
          <div style="font-family:monospace;font-size:10px;letter-spacing:4px;color:#00f2ff88;margin-bottom:8px">◈ TEAM MEMBERS</div>
          <div style="font-family:monospace;font-size:12px;color:#ffffff66;margin-bottom:10px;word-break:break-word">{all_csv}</div>
          <table style="width:100%;border-collapse:collapse">{rows}</table>
        </div>"""

    ticket_html = ""
    if ticket:
        import datetime as _dt
        ev_date = ticket.get("event_date") or "10 Jan 2026"
        try:
            if isinstance(ev_date, (_dt.date, _dt.datetime)):
                ev_date = ev_date.strftime("%d %b %Y")
            elif isinstance(ev_date, str) and ev_date[:4].isdigit():
                ev_date = _dt.datetime.strptime(ev_date[:10], "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            pass
        amount = ticket.get("amount_paid", 0) or 0
        amount_str = "FREE" if int(amount) == 0 else f"₹{int(amount)}"
        checked_time = ticket.get("checked_in_time") or "Just now"

        def row(label, val, color="#fff"):
            return f"<tr><td style='padding:9px 0;color:#ffffff44;font-size:11px;letter-spacing:3px;font-family:monospace;padding-right:16px;white-space:nowrap'>{label}</td><td style='padding:9px 0;color:{color};font-size:14px'>{val}</td></tr>"

        ticket_html = f"""<table style="width:100%;border-collapse:collapse;margin-top:18px">
          {row("EVENT",      ticket.get("event_title","—"))}
          {row("DATE",       ev_date)}
          {row("REGISTRANT", ticket.get("name","—"))}
          {row("EMAIL",      ticket.get("email","—"), "#ffffff88")}
          {row("AMOUNT",     amount_str, "#00f2ff")}
          {row("TICKET ID",  ticket.get("registration_code","—"), "#ffd166")}
          {row("CHECK-IN",   str(checked_time), "#ffffff55")}
        </table>"""

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scan — Anchorage 2026</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{background:#000810;color:#fff;font-family:'Segoe UI',Arial,sans-serif;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:24px 16px 60px}}.card{{max-width:480px;width:100%;border:1px solid {accent}44;background:{bg_tint};box-shadow:0 0 60px {accent}18}}.top{{background:{accent}18;border-bottom:1px solid {accent}33;padding:28px 24px;text-align:center}}.icon{{font-size:3rem;display:block;margin-bottom:12px}}.ttl{{font-family:monospace;font-size:1.2rem;letter-spacing:5px;color:{accent};text-transform:uppercase;margin-bottom:6px}}.sub{{font-size:13px;color:#ffffff66}}.body{{padding:22px}}.brand{{font-family:monospace;font-size:9px;letter-spacing:4px;color:#ffffff1a;text-align:center;margin-top:24px;padding-top:16px;border-top:1px solid #ffffff0a}}</style>
</head><body><div class="card">
<div class="top"><span class="icon">{icon}</span><div class="ttl">{title}</div><div class="sub">{subtitle}</div></div>
<div class="body">{ticket_html}{member_html}<div class="brand">⚓ ANCHORAGE 2026 · DEPT OF SHIP TECHNOLOGY, CUSAT</div></div>
</div></body></html>"""

# ======================================================
# ADMIN — UPI PAYMENT REVIEW
# ======================================================
#
# Simple password-protected admin panel to list, approve, and reject
# pending UPI payments. Approval triggers the same registration +
# ticket email flow as Razorpay.
#
# Set ADMIN_SECRET to any strong password in your environment:
#   export ADMIN_SECRET="your-secret-here"
# Or hard-code it below for quick use (rotate before production).
# ======================================================

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "anchorage_admin_2026")


def _admin_auth():
    """Returns True if the current session has admin access."""
    return session.get("admin_authenticated") is True


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_SECRET:
            session["admin_authenticated"] = True
            return redirect(url_for("admin_upi_payments"))
        error = "Incorrect password."
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Admin Login — Anchorage 2026</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#000810;color:#fff;font-family:'Segoe UI',Arial,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}}
  .box{{max-width:360px;width:100%;border:1px solid #00f2ff33;padding:36px;background:#00111e}}
  h2{{font-family:monospace;letter-spacing:4px;color:#00f2ff;margin-bottom:24px;font-size:1rem}}
  input{{width:100%;background:rgba(0,242,255,0.05);border:1px solid #00f2ff33;color:#fff;
         padding:12px 14px;font-size:1rem;outline:none;border-radius:2px;margin-bottom:14px}}
  input:focus{{border-color:#00f2ff}}
  button{{width:100%;background:#00f2ff;color:#000;border:none;padding:13px;
           font-family:monospace;font-size:0.85rem;letter-spacing:3px;cursor:pointer;font-weight:700;border-radius:2px}}
  .err{{color:#ff4466;font-size:0.8rem;margin-bottom:10px;font-family:monospace}}
</style></head>
<body><div class="box">
  <h2>⚓ ADMIN LOGIN</h2>
  {'<div class="err">⚠ '+error+'</div>' if error else ''}
  <form method="POST">
    <input type="hidden" name="csrf_token" value="{generate_csrf()}">
    <input type="password" name="password" placeholder="Admin password" autofocus>
    <button type="submit">LOGIN →</button>
  </form>
</div></body></html>"""


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/upi-payments")
def admin_upi_payments():
    if not _admin_auth():
        return redirect(url_for("admin_login"))

    _csrf_tok = generate_csrf()
    status_filter = request.args.get("status", "pending")

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            """
            SELECT up.*, u.name AS user_name, u.email AS user_email
            FROM   upi_payments up
            JOIN   users u ON u.id = up.user_id
            WHERE  up.status = %s
            ORDER  BY up.user_id ASC, up.created_at DESC
            LIMIT  500
            """,
            (status_filter,)
        )
        payments = cursor.fetchall()
    except Exception as e:
        payments = []
        app.logger.error(f"admin_upi_payments fetch error: {e}")
    finally:
        cursor.close()

    # Fetch event titles so we can show "Event Name — ₹price" instead of raw IDs
    event_titles = {}
    try:
        cur_ev = mysql.connection.cursor()
        cur_ev.execute("SELECT id, title FROM events")
        for row in cur_ev.fetchall():
            event_titles[row["id"]] = row["title"]
        cur_ev.close()
    except Exception as e:
        app.logger.warning(f"admin_upi_payments event title fetch error: {e}")

    # Count by status
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    try:
        cur2 = mysql.connection.cursor()
        cur2.execute("SELECT status, COUNT(*) AS cnt FROM upi_payments GROUP BY status")
        for row in cur2.fetchall():
            counts[row["status"]] = row["cnt"]
        cur2.close()
    except Exception:
        pass

    # ── Group payments by account (user_id) ────────────────────────────
    # Each account may have submitted multiple UPI payments (e.g. registered
    # for events in separate batches). We group them so the admin sees the
    # FULL set of events + total amount owed by that one account together.
    grouped = {}
    order = []
    for p in payments:
        uid = p.get("user_id")
        key = uid if uid is not None else f"deleted_{p['id']}"
        if key not in grouped:
            grouped[key] = {
                "user_id":    uid,
                "user_name":  p.get("user_name") or "(deleted account)",
                "user_email": p.get("user_email") or "—",
                "payments":   [],
                "total_amount": 0,
            }
            order.append(key)
        grouped[key]["payments"].append(p)
        grouped[key]["total_amount"] += float(p.get("amount") or 0)

    def render_screenshot_tag(p):
        # IMPORTANT: this admin app runs as a SEPARATE Render service from
        # the main registration site. Screenshots saved to local disk only
        # exist on the MAIN SITE's container — this app can never read that
        # filesystem. The only screenshot link that can ever work here is
        # the Cloudinary URL. If it's missing, the upload on the main site
        # failed and there is no way to view that screenshot from this
        # app — it must be fixed at the source (main site).
        cloud_url = p.get("cloudinary_url") or ""
        if cloud_url:
            return (f'<a href="{cloud_url}" target="_blank" '
                    f'style="color:#00f2ff;text-decoration:none">📷 View Screenshot ↗</a>')
        return ('<span style="color:#ff4466;font-size:11px">'
                '⚠ No image link — upload failed on main site</span>')

    def render_event_chips(p):
        try:
            eids = _json.loads(p.get("event_ids") or "[]")
        except Exception:
            eids = []
        if not eids:
            return '<span style="color:#666">—</span>'
        chips = ""
        for eid in eids:
            title = event_titles.get(int(eid), f"Event #{eid}")
            chips += (f'<span style="display:inline-block;background:rgba(0,242,255,0.08);'
                      f'border:1px solid #00f2ff33;color:#00f2ff;padding:3px 9px;'
                      f'border-radius:10px;font-size:11px;margin:2px 3px 2px 0">{title}</span>')
        return chips

    account_blocks = ""
    for key in order:
        g       = grouped[key]
        n_subs  = len(g["payments"])
        total   = int(g["total_amount"])
        uid_disp = g["user_id"] if g["user_id"] is not None else "—"

        sub_rows = ""
        for p in g["payments"]:
            created = str(p.get("created_at") or "")[:16]
            amt     = int(p.get("amount") or 0)
            txn     = p.get("txn_id") or "—"
            pid     = p.get("id")
            status  = p.get("status") or "pending"
            ss_tag  = render_screenshot_tag(p)
            ev_chips = render_event_chips(p)

            approve_btn = reject_btn = ""
            if status == "pending":
                approve_btn = (
                    f'<form method="POST" action="/admin/upi-payments/{pid}/approve" style="display:inline">'
                    f'<input type="hidden" name="csrf_token" value="{_csrf_tok}">'
                    f'<input type="hidden" name="note" id="note_a_{pid}">'
                    f'<button onclick="return confirmAction({pid},\'approve\')" '
                    f'style="background:#06d6a0;color:#000;border:none;padding:7px 16px;'
                    f'font-family:monospace;font-size:0.72rem;letter-spacing:1.5px;cursor:pointer;'
                    f'border-radius:2px;margin-right:8px">✓ APPROVE</button></form>'
                )
                reject_btn = (
                    f'<form method="POST" action="/admin/upi-payments/{pid}/reject" style="display:inline">'
                    f'<input type="hidden" name="csrf_token" value="{_csrf_tok}">'
                    f'<input type="hidden" name="note" id="note_r_{pid}">'
                    f'<button onclick="return confirmAction({pid},\'reject\')" '
                    f'style="background:#ff4466;color:#fff;border:none;padding:7px 16px;'
                    f'font-family:monospace;font-size:0.72rem;letter-spacing:1.5px;cursor:pointer;'
                    f'border-radius:2px">✕ REJECT</button></form>'
                )

            sub_rows += f"""<tr>
              <td style="padding-left:24px">#{pid}</td>
              <td>{created}</td>
              <td style="font-family:monospace;color:#ffd166">{txn}</td>
              <td>{ev_chips}</td>
              <td style="color:#00f2ff;font-weight:700">₹{amt}</td>
              <td>{ss_tag}</td>
              <td><span style="color:{'#06d6a0' if status=='approved' else '#ff4466' if status=='rejected' else '#ffd166'}">{status.upper()}</span></td>
              <td>
                <input type="text" id="adminNote_{pid}" placeholder="Optional note"
                       style="background:#0a1a2a;border:1px solid #ffffff22;color:#fff;
                              padding:5px 9px;font-size:0.78rem;border-radius:2px;
                              width:150px;margin-bottom:6px;display:block">
                {approve_btn}{reject_btn}
              </td>
            </tr>"""

        account_blocks += f"""
        <div class="account-block">
          <div class="account-header">
            <div>
              <strong style="font-size:1rem;color:#fff">{g['user_name']}</strong>
              <span style="color:#888;font-size:12px;margin-left:10px">{g['user_email']}</span>
              <span style="color:#555;font-size:11px;margin-left:10px">(user_id: {uid_disp})</span>
            </div>
            <div style="text-align:right">
              <span style="color:#888;font-size:11px;letter-spacing:1px">
                {n_subs} SUBMISSION{'S' if n_subs != 1 else ''} AWAITING REVIEW
              </span><br>
              <span style="color:#ffd166;font-size:1.15rem;font-weight:700">
                TOTAL: ₹{total}
              </span>
            </div>
          </div>
          <table class="sub-table">
            <thead><tr>
              <th style="padding-left:24px">SUB #</th><th>DATE</th><th>TXN ID</th>
              <th>EVENT(S)</th><th>AMOUNT</th><th>PROOF</th><th>STATUS</th><th>ACTIONS</th>
            </tr></thead>
            <tbody>{sub_rows}</tbody>
          </table>
        </div>"""

    tab = lambda s, lbl: (
        f'<a href="/admin/upi-payments?status={s}" '
        f'style="padding:8px 20px;font-family:monospace;font-size:0.78rem;letter-spacing:2px;'
        f'color:{"#00f2ff" if status_filter==s else "#ffffff55"};'
        f'border-bottom:2px solid {"#00f2ff" if status_filter==s else "transparent"};'
        f'text-decoration:none;margin-right:8px">'
        f'{lbl} ({counts.get(s,0)})</a>'
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>UPI Payments — Admin</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#000810;color:#fff;font-family:'Segoe UI',Arial,sans-serif;padding:24px;font-size:14px}}
  h1{{font-family:monospace;letter-spacing:4px;color:#00f2ff;margin-bottom:20px;font-size:1.1rem}}
  table{{width:100%;border-collapse:collapse}}
  th{{font-family:monospace;font-size:9.5px;letter-spacing:2px;color:#00f2ff88;
      padding:8px 12px;border-bottom:1px solid #00f2ff22;text-align:left;white-space:nowrap}}
  td{{padding:10px 12px;border-bottom:1px solid #ffffff0a;vertical-align:top;font-size:12.5px}}
  tr:hover td{{background:rgba(0,242,255,0.02)}}
  .tabs{{margin-bottom:20px;border-bottom:1px solid #ffffff11;padding-bottom:0}}
  .logout{{float:right;font-family:monospace;font-size:0.75rem;color:#ff446688;
           text-decoration:none;letter-spacing:2px}}
  .logout:hover{{color:#ff4466}}
  .account-block{{
    border:1px solid #ffffff14;border-radius:6px;margin-bottom:22px;
    background:rgba(255,255,255,0.015);overflow:hidden;
  }}
  .account-header{{
    display:flex;justify-content:space-between;align-items:center;
    padding:16px 18px;background:rgba(255,209,102,0.04);
    border-bottom:1px solid #ffffff10;
  }}
  .sub-table{{margin:0}}
</style>
<script>
function confirmAction(id, action) {{
  const note = document.getElementById('adminNote_'+id).value;
  if (action === 'approve') {{
    document.getElementById('note_a_'+id).value = note;
  }} else {{
    document.getElementById('note_r_'+id).value = note;
  }}
  return confirm('Are you sure you want to ' + action.toUpperCase() + ' submission #'+id+'?');
}}
</script>
</head><body>
<h1>⚓ UPI PAYMENT ADMIN <a href="/admin/logout" class="logout">LOGOUT</a></h1>
<div class="tabs">
  {tab("pending","PENDING")}{tab("approved","APPROVED")}{tab("rejected","REJECTED")}
</div>
{'<p style="color:#ffffff44;font-family:monospace;font-size:0.8rem;padding:20px 0">No payments found.</p>' if not payments else ''}
{account_blocks}
</body></html>"""


@app.route("/admin/upi-payments/<int:payment_id>/approve", methods=["POST"])
def admin_upi_approve(payment_id):
    """
    Approves a pending UPI payment:
    1. Looks up the upi_payments record
    2. Runs the same registration-writing logic used elsewhere in this file
    3. Sends ticket emails
    4. Updates upi_payments.status → 'approved'
    """
    if not _admin_auth():
        return redirect(url_for("admin_login"))

    admin_note = request.form.get("note", "").strip()

    cursor = mysql.connection.cursor()
    try:
        cursor.execute("SELECT * FROM upi_payments WHERE id = %s", (payment_id,))
        rec = cursor.fetchone()
    except Exception as e:
        cursor.close()
        return f"DB error: {e}", 500

    if not rec:
        cursor.close()
        return "Payment record not found", 404

    if rec["status"] != "pending":
        cursor.close()
        return f"Payment is already {rec['status']}", 400

    user_id = rec["user_id"]
    txn_id  = rec["txn_id"]
    amount  = float(rec["amount"] or 0)

    # Parse stored JSON
    try:
        event_ids = _json.loads(rec["event_ids"] or "[]")
        event_ids = [int(x) for x in event_ids]
    except Exception:
        event_ids = []

    try:
        event_registrations = _json.loads(rec["event_registrations"] or "[]")
    except Exception:
        event_registrations = []

    try:
        participant = _json.loads(rec["participant"] or "{}")
    except Exception:
        participant = {}

    # Resolve the stored referral code (captured at submit time from the
    # participant's validated session) to an ambassador, if any. Re-validated
    # here rather than trusted blindly, in case the code was deactivated
    # between submission and admin approval.
    referral = None
    stored_referral_code = rec.get("referral_code")
    if stored_referral_code:
        referral, _ref_err = _validate_referral_code(stored_referral_code)

    if not event_ids:
        cursor.close()
        return "No event IDs in record — cannot register.", 400

    # ── Fetch events ───────────────────────────────────────────────────
    try:
        fmt = ",".join(["%s"] * len(event_ids))
        cursor.execute(f"SELECT * FROM events WHERE id IN ({fmt})", tuple(event_ids))
        all_events = {row["id"]: row for row in cursor.fetchall()}
    except Exception as e:
        cursor.close()
        return f"Event fetch error: {e}", 500

    # ── Existing registrations (don't reserve a new ticket for these) ───
    try:
        fmt3 = ",".join(["%s"] * len(event_ids))
        cursor.execute(
            f"SELECT event_id, registration_code FROM registrations "
            f"WHERE user_id=%s AND event_id IN ({fmt3})",
            (user_id, *event_ids)
        )
        already_registered = {r["event_id"]: r["registration_code"] for r in cursor.fetchall()}
    except Exception as e:
        cursor.close()
        return f"Existing registrations fetch error: {e}", 500

    new_event_ids = [eid for eid in event_ids if eid not in already_registered]

    # ── Reserve tickets atomically before inserting anything ────────────
    ok, sold_out = _reserve_tickets_or_fail(cursor, new_event_ids)
    if not ok:
        mysql.connection.rollback()
        cursor.close()
        return f"Sold out: {', '.join(sold_out)}", 400

    # ── Build members map ──────────────────────────────────────────────
    event_reg_map = {}
    for er in (event_registrations or []):
        try:
            eid = int(er.get("event_id", 0))
        except (TypeError, ValueError):
            continue
        event_reg_map[eid] = er.get("members") or []

    primary_fname = (participant.get("first_name") or "").strip()
    primary_lname = (participant.get("last_name")  or "").strip()
    primary_name  = (primary_fname + " " + primary_lname).strip()

    # ── Insert registrations ───────────────────────────────────────────
    registered_codes = []
    newly_registered = []  # (event_id, reg_code) — excludes already_registered skips
    try:
        for event_id in event_ids:
            existing_code = already_registered.get(event_id)
            if existing_code:
                registered_codes.append(existing_code)
                continue

            event_row  = all_events.get(event_id)
            base_price = get_price(event_row) if event_row else 0
            reg_code   = "ANCH-" + uuid.uuid4().hex[:8].upper()

            cursor.execute(
                """INSERT INTO registrations
                       (user_id, event_id, payment_id, order_id, registration_code, checked_in, amount_paid)
                   VALUES (%s, %s, %s, %s, %s, FALSE, %s)""",
                (user_id, event_id, txn_id, f"UPI-{payment_id}", reg_code, int(base_price))
            )

            # Save team members
            members_for_event = event_reg_map.get(event_id, [])
            rows_to_save = []
            if members_for_event:
                for idx, m in enumerate(members_for_event):
                    fname = (m.get("fname") or m.get("first_name") or "").strip()
                    lname = (m.get("lname") or m.get("last_name")  or "").strip()
                    full  = (fname + " " + lname).strip() or (primary_name if idx == 0 else f"Member {idx+1}")
                    rows_to_save.append((
                        reg_code, full,
                        (m.get("email")   or "").strip() or None,
                        (m.get("phone")   or "").strip() or None,
                        (m.get("college") or "").strip() or None,
                        (m.get("dept")    or "").strip() or None,
                        idx,
                    ))
            elif primary_name:
                rows_to_save.append((
                    reg_code, primary_name,
                    (participant.get("email")   or "").strip() or None,
                    (participant.get("phone")   or "").strip() or None,
                    (participant.get("college") or "").strip() or None,
                    (participant.get("dept")    or "").strip() or None,
                    0,
                ))

            if rows_to_save:
                try:
                    cursor.executemany(
                        """INSERT INTO registration_members
                               (registration_code, member_name, member_email, member_phone,
                                member_college, member_dept, member_order)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        rows_to_save
                    )
                except Exception as me:
                    app.logger.warning(f"admin_upi_approve: members insert skipped for {reg_code}: {me}")

            # tickets_sold was already incremented atomically above.

            registered_codes.append(reg_code)
            newly_registered.append((event_id, reg_code))

        # Update upi_payments status
        cursor.execute(
            "UPDATE upi_payments SET status='approved', admin_note=%s WHERE id=%s",
            (admin_note or "Approved by admin", payment_id)
        )

        # Remove exactly the events just registered from the cart — not a
        # blanket clear, since the user's cart may have picked up other
        # items while this UPI payment sat pending admin review.
        try:
            db_remove_events_from_cart(user_id, event_ids)
        except Exception:
            pass

        mysql.connection.commit()

    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        app.logger.error(f"admin_upi_approve insert error: {e}")
        return f"Registration failed: {e}", 500

    cursor.close()

    # ── Campus Ambassador referral attribution ──────────────────────────
    # Best-effort, same as the online-payment path — one point per unique
    # (ambassador, participant email, event) combination.
    if referral:
        participant_email = (participant.get("email") or "").strip()
        for eid, reg_code in newly_registered:
            try:
                _award_referral_point(referral, eid, participant_email, registration_code=reg_code)
            except Exception as e:
                app.logger.warning(f"admin_upi_approve: referral point award failed for {reg_code}: {e}")

    # ── Send ticket email (one combined email for the whole order) ─────
    host_url = request.host_url
    email_errors = []
    try:
        _send_combined_ticket_email(registered_codes, host_url)
        app.logger.info(f"Combined ticket email sent for approved UPI payment #{payment_id}: {registered_codes}")
    except Exception as e:
        import traceback as _tb, sys
        _tb.print_exc(file=sys.stderr)
        email_errors.append(str(e))
        app.logger.error(f"Combined ticket email failed for UPI payment #{payment_id}: {e}")

    msg = f"✓ Approved! Registered {len(registered_codes)} ticket(s): {', '.join(registered_codes)}."
    if email_errors:
        msg += f" ⚠ Email errors: {'; '.join(email_errors)}"

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{background:#000810;color:#fff;font-family:monospace;padding:36px;font-size:14px}}</style>
</head><body>
<p style="color:#06d6a0;font-size:1.1rem;margin-bottom:16px">✓ APPROVED</p>
<p style="color:#fff88;line-height:1.8">{msg}</p>
<br><a href="/admin/upi-payments" style="color:#00f2ff;letter-spacing:2px">← BACK TO LIST</a>
</body></html>"""


@app.route("/admin/upi-payments/<int:payment_id>/reject", methods=["POST"])
def admin_upi_reject(payment_id):
    """Rejects a pending UPI payment and sends a notification email."""
    if not _admin_auth():
        return redirect(url_for("admin_login"))

    admin_note = request.form.get("note", "").strip() or "Rejected by admin"

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            "SELECT up.*, u.name AS user_name, u.email AS user_email "
            "FROM upi_payments up JOIN users u ON u.id = up.user_id WHERE up.id = %s",
            (payment_id,)
        )
        rec = cursor.fetchone()
    except Exception as e:
        cursor.close()
        return f"DB error: {e}", 500

    if not rec:
        cursor.close()
        return "Payment record not found", 404

    if rec["status"] != "pending":
        cursor.close()
        return f"Payment is already {rec['status']}", 400

    # Parse event_ids before cursor is closed so we can restore cart
    rejected_event_ids = []
    try:
        rejected_event_ids = [int(x) for x in _json.loads(rec.get("event_ids") or "[]")]
    except Exception:
        pass

    try:
        cursor.execute(
            "UPDATE upi_payments SET status='rejected', admin_note=%s WHERE id=%s",
            (admin_note, payment_id)
        )
        mysql.connection.commit()
    except Exception as e:
        cursor.close()
        return f"DB update error: {e}", 500

    cursor.close()

    # ── Restore the user's cart so they can re-register easily ────────
    # Only add back events the user isn't already registered for
    if rejected_event_ids and rec.get("user_id"):
        rejected_user_id = rec["user_id"]
        for eid in rejected_event_ids:
            try:
                db_add_to_cart(rejected_user_id, eid)
            except Exception as ce:
                app.logger.warning(f"admin_upi_reject: could not restore cart event {eid}: {ce}")

    # ── Notify user by email ───────────────────────────────────────────
    user_name  = rec.get("user_name")  or "Participant"
    user_email = rec.get("user_email") or ""
    txn_id     = rec.get("txn_id")    or "—"
    amount     = int(rec.get("amount") or 0)

    if user_email:
        try:
            html_body = f"""
<div style="font-family:Arial,sans-serif;background:#000810;color:#fff;
            padding:32px;max-width:600px;margin:auto;border:1px solid #ff446622">
  <h1 style="color:#ff4466;font-size:20px;letter-spacing:4px;margin-bottom:4px">ANCHORAGE 2026</h1>
  <p style="color:#ff446688;font-size:12px;letter-spacing:3px;margin-bottom:24px">
    UPI PAYMENT — ACTION REQUIRED
  </p>
  <p style="font-size:16px;margin-bottom:8px">Hello <strong>{user_name}</strong>,</p>
  <p style="color:#ccc;line-height:1.7;">
    Unfortunately, we could not verify your UPI payment (Txn ID:
    <strong style="color:#ffd166">{txn_id}</strong>, Amount: ₹{amount}).
  </p>
  <p style="color:#ccc;line-height:1.7;margin-top:12px">
    <strong>Reason:</strong> {admin_note}
  </p>
  <p style="color:#ccc;line-height:1.7;margin-top:12px">
    Please re-register with a valid payment, or contact us if you believe this is an error.
  </p>
  <p style="margin-top:24px;color:#666;font-size:11px">
    &mdash; Team Anchorage 2026 &middot; Dept of Ship Technology, CUSAT
  </p>
</div>"""
            text_body = (
                f"Hello {user_name},\n\n"
                f"Your UPI payment (Txn ID: {txn_id}, Amount: Rs.{amount}) "
                f"could not be verified.\n\nReason: {admin_note}\n\n"
                f"Please re-register or contact us.\n\n-- Team Anchorage 2026"
            )
            _brevo_send(
                to_email=user_email,
                to_name=user_name,
                subject=f"[Anchorage 2026] UPI Payment Could Not Be Verified — {txn_id}",
                html_body=html_body,
                text_body=text_body,
            )
        except Exception as e:
            app.logger.warning(f"admin_upi_reject email error: {e}")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{background:#000810;color:#fff;font-family:monospace;padding:36px;font-size:14px}}</style>
</head><body>
<p style="color:#ff4466;font-size:1.1rem;margin-bottom:16px">✕ REJECTED</p>
<p>Payment #{payment_id} (Txn: {txn_id}) has been rejected. Notification email sent to {user_email}.</p>
<br><a href="/admin/upi-payments" style="color:#00f2ff;letter-spacing:2px">← BACK TO LIST</a>
</body></html>"""


# ======================================================
# MERCHANDISE
# Fully separate from the event cart/checkout/TiQR machinery above —
# its own session key (merch_cart), its own tables (merch_products,
# merch_stock, merch_orders, merch_order_items). No payment is collected
# at order time — this is a pre-order/demand-tally flow: customers submit
# their cart + details, we review total demand across all orders, and
# only THEN decide whether to proceed (see admin_merch_confirm /
# admin_merch_cancel in app_admin.py). Payment collection is a manual
# follow-up outside this flow for now.
# Schema: see merch_schema.sql.
# ======================================================

MERCH_SIZES = ["S", "M", "L", "XL", "XXL", "XXXL"]
MERCH_COLORS = ["Black", "White"]


def _variant_options(p):
    """Which set of variant values a product's picker should offer.
    variant_type on merch_products is 'size' (default) or 'color'."""
    return MERCH_COLORS if p.get("variant_type") == "color" else MERCH_SIZES


def _normalize_variant(value, p):
    """Match the casing each variant type is stored/compared in:
    sizes as 'M', 'XL', ...; colors as 'Black', 'White', ..."""
    value = (value or "").strip()
    if p and p.get("variant_type") == "color":
        return value.title()
    return value.upper()


def _get_merch_products():
    """
    Active products with their stock broken out per size (or a single ''
    key for non-sized products), e.g.
    [{..., "stock": {"S": 10, "M": 0, ...}}, {..., "stock": {"": 5}}]
    """
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM merch_products WHERE active = 1 ORDER BY id ASC")
    products = cursor.fetchall()
    cursor.execute("SELECT product_id, size, quantity FROM merch_stock")
    stock_rows = cursor.fetchall()
    cursor.close()

    stock_by_product = {}
    for row in stock_rows:
        stock_by_product.setdefault(row["product_id"], {})[row["size"] or ""] = row["quantity"]

    for p in products:
        p["stock"] = stock_by_product.get(p["id"], {})
    return products


def _merch_product_map():
    return {p["id"]: p for p in _get_merch_products()}


@app.context_processor
def _inject_merch_cart_count():
    """
    Makes merch_cart_count available in every template automatically (a
    cheap sum straight from the session, no DB hit) so the nav badge can
    show it without every existing template needing a code change.
    """
    try:
        raw = session.get("merch_cart", [])
        count = sum(max(1, int(line.get("quantity", 1))) for line in raw)
    except Exception:
        count = 0
    return {"merch_cart_count": count}


def _merch_cart_items():
    """
    Reads session['merch_cart'] (list of {product_id, size, quantity}) and
    re-derives price/availability from the DB on every call — the session
    only ever stores the bare selection, never a cached price, so a price
    change takes effect immediately and can't be spoofed client-side.
    Returns (items, total_rupees).
    """
    raw = session.get("merch_cart", [])
    if not raw:
        return [], 0.0

    products = _merch_product_map()
    items = []
    total = 0.0
    for line in raw:
        p = products.get(line.get("product_id"))
        if not p:
            continue
        size = line.get("size") if p["has_sizes"] else None
        try:
            qty = max(1, int(line.get("quantity", 1)))
        except (TypeError, ValueError):
            qty = 1
        available = p["stock"].get(size or "", 0)
        line_total = round(float(p["price"]) * qty, 2)
        items.append({
            "product_id": p["id"],
            "name": p["name"],
            "image_url": p["image_url"],
            "price": float(p["price"]),
            "size": size,
            "quantity": qty,
            "available": available,
            "line_total": line_total,
        })
        total += line_total
    return items, round(total, 2)


def _send_merch_order_confirmation(order_code):
    """
    Sent right after an order is SUBMITTED — no payment has been taken.
    This just confirms we've received the order; a separate email goes
    out later (see admin_merch_confirm in app_admin.py) once we've
    reviewed total demand across all orders and decided to proceed —
    payment is only arranged at that point, not now.
    """
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM merch_orders WHERE order_code = %s", (order_code,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return
    cursor.execute("SELECT * FROM merch_order_items WHERE order_id = %s", (order["id"],))
    items = cursor.fetchall()
    cursor.close()

    rows_html = "".join(
        f"<tr><td style='padding:6px 10px;color:#ccc;border-bottom:1px solid #ffffff14'>"
        f"{it['product_name']}{' (' + it['size'] + ')' if it['size'] else ''}</td>"
        f"<td style='padding:6px 10px;color:#ccc;text-align:center;border-bottom:1px solid #ffffff14'>{it['quantity']}</td>"
        f"<td style='padding:6px 10px;color:#ccc;text-align:right;border-bottom:1px solid #ffffff14'>"
        f"₹{float(it['unit_price']) * it['quantity']:.2f}</td></tr>"
        for it in items
    )
    address = order["address_line1"]
    if order["address_line2"]:
        address += ", " + order["address_line2"]
    if order.get("landmark"):
        address += f" (near {order['landmark']})"
    address += f", {order['city']}, {order['state']} - {order['pincode']}"

    html_body = f"""<div style="background:#000810;padding:28px;font-family:sans-serif;color:#fff">
  <p style="color:#00f2ff;letter-spacing:2px;font-size:12px;margin-bottom:16px">ORDER RECEIVED</p>
  <p style="font-size:16px">Hi <strong>{order['name']}</strong>, we've got your order on file!</p>
  <p style="color:#aaa;margin-top:8px">Order code: <strong style="color:#ffd166">{order_code}</strong></p>
  <table style="width:100%;margin-top:16px;border-collapse:collapse">
    <thead><tr>
      <th style="text-align:left;padding:6px 10px;color:#888;font-size:11px">ITEM</th>
      <th style="padding:6px 10px;color:#888;font-size:11px">QTY</th>
      <th style="text-align:right;padding:6px 10px;color:#888;font-size:11px">AMOUNT</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="margin-top:16px;font-weight:600;font-size:15px">Total (once confirmed): ₹{order['total_amount']}</p>
  <p style="color:#ffd166;margin-top:16px;font-weight:600">No payment is needed right now.</p>
  <p style="color:#aaa;margin-top:8px;line-height:1.6">
    We're first collecting orders from everyone interested — once we've reviewed the total count,
    we'll confirm your order by email and let you know how to complete payment. We'll ship to:<br>
    {address}
  </p>
  <p style="margin-top:24px;color:#666;font-size:11px">&mdash; Team Anchorage 2026 &middot; Dept of Ship Technology, CUSAT</p>
</div>"""
    text_body = (
        f"Hi {order['name']}, we've received your order ({order_code}).\n"
        f"Total (once confirmed): Rs.{order['total_amount']}\n"
        f"No payment is needed right now — we're first gauging total demand. "
        f"We'll email you once your order is confirmed and let you know how to pay.\n"
        f"We'll ship to: {address}\n\n"
        f"-- Team Anchorage 2026"
    )
    _brevo_send(
        to_email=order["email"],
        to_name=order["name"],
        subject=f"[Anchorage 2026] Order Received — {order_code}",
        html_body=html_body,
        text_body=text_body,
    )


@app.route("/shop")
def shop_alias():
    """Lets people reach the store via /shop (e.g. anchorage.com/shop)
    without duplicating the actual /merch route/template."""
    return redirect(url_for("merch_shop"))


@app.route("/merch")
def merch_shop():
    products = _get_merch_products()
    return render_template(
        "merch_shop.html", products=products, sizes=MERCH_SIZES, colors=MERCH_COLORS
    )


@app.route("/merch/add", methods=["POST"])
@limiter.limit("60 per hour")
@csrf.exempt  # fetch()-driven JSON endpoint, same pattern as /validate-promo
def merch_add_to_cart():
    data = request.get_json(silent=True) or {}
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid product"}), 400

    try:
        quantity = max(1, int(data.get("quantity", 1)))
    except (TypeError, ValueError):
        quantity = 1

    products = _merch_product_map()
    p = products.get(product_id)
    if not p:
        return jsonify({"error": "Product not found"}), 404

    size = None
    if p["has_sizes"]:
        size = _normalize_variant(data.get("size"), p)
        if size not in _variant_options(p):
            label = "colour" if p.get("variant_type") == "color" else "size"
            return jsonify({"error": f"Please select a valid {label}"}), 400

    available = p["stock"].get(size or "", 0)
    if available <= 0:
        label = f"{p['name']}" + (f" ({size})" if size else "")
        return jsonify({"error": f"{label} is out of stock"}), 400

    cart = session.get("merch_cart", [])
    for line in cart:
        if line.get("product_id") == product_id and line.get("size") == size:
            line["quantity"] = min(line.get("quantity", 0) + quantity, available)
            break
    else:
        cart.append({"product_id": product_id, "size": size, "quantity": min(quantity, available)})

    session["merch_cart"] = cart
    session.modified = True

    items, total = _merch_cart_items()
    return jsonify({
        "success": True,
        "cart_count": sum(i["quantity"] for i in items),
        "total": total,
    })


@app.route("/merch-cart")
def merch_cart():
    items, total = _merch_cart_items()
    return render_template("merch_cart.html", items=items, total=total)


@app.route("/merch/cart/update", methods=["POST"])
@limiter.limit("60 per hour")
@csrf.exempt
def merch_cart_update():
    """action='remove' drops the line; action='set_quantity' (default)
    updates it, clamped to current stock."""
    data = request.get_json(silent=True) or {}
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid product"}), 400

    products = _merch_product_map()
    p = products.get(product_id)
    raw_size = (data.get("size") or "").strip()
    size = _normalize_variant(raw_size, p) if raw_size else None
    action = data.get("action", "set_quantity")

    cart = session.get("merch_cart", [])
    new_cart = []
    for line in cart:
        if line.get("product_id") == product_id and (line.get("size") or None) == size:
            if action == "remove":
                continue
            available = p["stock"].get(size or "", 0) if p else line.get("quantity", 1)
            try:
                qty = int(data.get("quantity", line.get("quantity", 1)))
            except (TypeError, ValueError):
                qty = line.get("quantity", 1)
            line["quantity"] = max(1, min(qty, available if available > 0 else qty))
        new_cart.append(line)

    session["merch_cart"] = new_cart
    session.modified = True

    items, total = _merch_cart_items()
    return jsonify({"success": True, "items": items, "total": total})


@app.route("/merch-checkout")
def merch_checkout():
    items, total = _merch_cart_items()
    if not items:
        return redirect(url_for("merch_shop"))
    return render_template("merch_checkout.html", items=items, total=total)


@app.route("/merch/order/submit", methods=["POST"])
@limiter.limit("15 per hour")
@csrf.exempt  # fetch()-driven JSON endpoint; see CSRF note near the top of the file
def merch_order_submit():
    """
    No payment is collected here — this just records the order. Buyer +
    shipping details, plus a re-typed phone number as a lightweight typo
    check (no OTP — just catches fat-fingering). We'll manually review
    total demand across every submitted order before deciding whether to
    proceed; only THEN do we reach back out about payment (see
    admin_merch_confirm in app_admin.py).
    Stock IS reserved atomically here, since this is now the only point
    an order gets recorded at all — same guarded `quantity >= X` pattern
    used everywhere else in this module. If you want this to work as an
    open-ended demand tally rather than a hard cap, just set generous
    stock numbers via merch_schema.sql.
    """
    user_id = _get_or_create_user_id()
    data = request.get_json(silent=True) or {}

    name          = (data.get("name") or "").strip()
    email         = (data.get("email") or "").strip()
    phone         = (data.get("phone") or "").strip()
    phone_confirm = (data.get("phone_confirm") or "").strip()
    addr1         = (data.get("address_line1") or "").strip()
    addr2         = (data.get("address_line2") or "").strip()
    landmark      = (data.get("landmark") or "").strip()
    city          = (data.get("city") or "").strip()
    state         = (data.get("state") or "").strip()
    pincode       = (data.get("pincode") or "").strip()

    if not all([name, email, phone, phone_confirm, addr1, city, state, pincode]):
        return jsonify({"error": "Please fill in all required fields"}), 400

    if phone != phone_confirm:
        return jsonify({"error": "Phone numbers don't match — please check and re-enter."}), 400

    try:
        _update_user_details(user_id, name, email, phone)
    except Exception as e:
        app.logger.warning(f"merch_order_submit: could not update user details: {e}")

    items, total = _merch_cart_items()
    if not items:
        return jsonify({"error": "Your merch cart is empty"}), 400

    for it in items:
        if it["available"] < it["quantity"]:
            label = it["name"] + (f" ({it['size']})" if it["size"] else "")
            return jsonify({
                "error": f"Only {it['available']} left of {label} — please adjust your cart."
            }), 400

    order_code = "MERCH-" + uuid.uuid4().hex[:8].upper()

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO merch_orders
                (order_code, user_id, name, email, phone,
                 address_line1, address_line2, landmark, city, state, pincode,
                 total_amount, order_status)
            VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,'pending')
            """,
            (order_code, user_id, name, email, phone,
             addr1, addr2 or None, landmark or None, city, state, pincode, total)
        )
        order_id = cursor.lastrowid
        for it in items:
            cursor.execute(
                """
                INSERT INTO merch_order_items
                    (order_id, product_id, product_name, size, quantity, unit_price)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (order_id, it["product_id"], it["name"], it["size"], it["quantity"], it["price"])
            )

        # Reserve stock now — this is the only point an order gets
        # recorded at all, so it's the natural place to do it. Rolls back
        # the WHOLE order (not just the short line) if anything is short,
        # so a buyer never ends up with a silently partial order.
        short = []
        for it in items:
            if it["size"] is None:
                cursor.execute(
                    "UPDATE merch_stock SET quantity = quantity - %s "
                    "WHERE product_id = %s AND size IS NULL AND quantity >= %s",
                    (it["quantity"], it["product_id"], it["quantity"])
                )
            else:
                cursor.execute(
                    "UPDATE merch_stock SET quantity = quantity - %s "
                    "WHERE product_id = %s AND size = %s AND quantity >= %s",
                    (it["quantity"], it["product_id"], it["size"], it["quantity"])
                )
            if cursor.rowcount == 0:
                short.append(it["name"] + (f" ({it['size']})" if it["size"] else ""))

        if short:
            mysql.connection.rollback()
            cursor.close()
            return jsonify({
                "error": f"Someone just took the last of: {', '.join(short)} — "
                         f"please adjust your cart and try again."
            }), 400

        mysql.connection.commit()
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        app.logger.error(f"merch_order_submit insert error: {e}")
        return jsonify({"error": f"Database error: {e}"}), 500
    cursor.close()

    session["merch_cart"] = []
    session.modified = True

    try:
        _send_merch_order_confirmation(order_code)
    except Exception as e:
        app.logger.warning(f"merch order confirmation email failed for {order_code}: {e}")

    return jsonify({"success": True, "order_code": order_code})



@app.route("/merch-order-success/<order_code>")
def merch_order_success(order_code):
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM merch_orders WHERE order_code = %s", (order_code,))
    order = cursor.fetchone()
    if not order:
        cursor.close()
        return redirect(url_for("merch_shop"))
    cursor.execute("SELECT * FROM merch_order_items WHERE order_id = %s", (order["id"],))
    items = cursor.fetchall()
    cursor.close()
    return render_template("merch_success.html", order=order, items=items)


# ======================================================
# ADMIN SCANNER
# ======================================================

@app.route("/admin-scanner")
def admin_scanner():
    return render_template("admin-scanner.html")

# ======================================================
# RUN
# ======================================================

if __name__ == "__main__":
    # DEBUG is controlled by the FLASK_DEBUG env var (see top of file).
    # Never run with debug=True against a public/production deployment —
    # it exposes the interactive debugger (arbitrary code execution) on
    # any unhandled exception.
    app.run(debug=DEBUG)
