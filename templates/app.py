from flask import Flask, render_template, request, redirect, url_for
from flask import session, jsonify, flash, send_file

from flask_mysqldb import MySQL
from flask_mail import Mail, Message

from werkzeug.security import generate_password_hash
from werkzeug.security import check_password_hash

import razorpay
import qrcode
import uuid
import os

from io import BytesIO
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

# ======================================================
# FLASK APP
# ======================================================

app = Flask(__name__)
app.secret_key = "anchorage_secret_key"

# ======================================================
# MYSQL CONFIG
# ======================================================

app.config["MYSQL_HOST"] = "yamabiko.proxy.rlwy.net"
app.config["MYSQL_USER"] = "root"
app.config["MYSQL_PORT"] = 28286
app.config["MYSQL_PASSWORD"] = "GAwExjueEjqyWMnhYaDFgIqxZzwPHObU"
app.config["MYSQL_DB"] = "anchorage2026"
app.config["MYSQL_CURSORCLASS"] = "DictCursor"

mysql = MySQL(app)

# ======================================================
# MAIL CONFIG
# ======================================================

app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = "yourgmail@gmail.com"
app.config["MAIL_PASSWORD"] = "your_gmail_app_password"
app.config["MAIL_DEFAULT_SENDER"] = "yourgmail@gmail.com"

mail = Mail(app)

# ======================================================
# RAZORPAY
# ======================================================

RAZORPAY_KEY_ID = "rzp_test_xxxxxxxxx"
RAZORPAY_KEY_SECRET = "xxxxxxxxxxxxx"

razor_client = razorpay.Client(
    auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
)

# ======================================================
# HOME
# ======================================================

@app.route("/")
def home():
    return render_template("index.html")

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
# REGISTER ACCOUNT
# ======================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name     = request.form.get("name")
        email    = request.form.get("email")
        password = request.form.get("password")

        hashed_password = generate_password_hash(password)

        cursor = mysql.connection.cursor()
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        existing_user = cursor.fetchone()

        if existing_user:
            flash("Email already exists")
            cursor.close()
            return redirect(url_for("register"))

        cursor.execute(
            "INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
            (name, email, hashed_password)
        )
        mysql.connection.commit()
        cursor.close()

        flash("Registration successful! Please log in.")
        return redirect(url_for("login"))

    return render_template("register-account.html")

# ======================================================
# LOGIN
# ======================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email")
        password = request.form.get("password")

        cursor = mysql.connection.cursor()
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()
        cursor.close()

        if user and check_password_hash(user["password"], password):
            session["loggedin"]  = True
            session["user_id"]   = user["id"]
            session["user_name"] = user["name"]
            return redirect(url_for("events"))

        flash("Invalid credentials. Please try again.")

    return render_template("login.html")

# ======================================================
# LOGOUT
# ======================================================

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# ======================================================
# EVENTS
# ======================================================

@app.route("/events")
def events():
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM events ORDER BY id DESC")
    all_events = cursor.fetchall()
    cursor.close()
    return render_template("events.html", events=all_events)

# ======================================================
# ADD TO CART
# ======================================================

@app.route("/add-to-cart/<int:event_id>", methods=["POST"])
def add_to_cart(event_id):
    if "cart" not in session:
        session["cart"] = []

    if event_id not in session["cart"]:
        session["cart"].append(event_id)
        session.modified = True

    return jsonify({"success": True})

# ======================================================
# REMOVE FROM CART  (single definition — duplicate removed)
# ======================================================

@app.route("/api/cart/remove/<int:event_id>", methods=["POST"])
def remove_from_cart(event_id):
    cart = session.get("cart", [])
    if event_id in cart:
        cart.remove(event_id)
        session["cart"] = cart
        session.modified = True
    return jsonify({"success": True})

# ======================================================
# CART
# ======================================================

@app.route("/cart")
def cart():
    cart_ids = session.get("cart", [])

    if not cart_ids:
        return render_template("cart.html", cart_items=[], total=0, razorpay_key=RAZORPAY_KEY_ID)

    format_strings = ",".join(["%s"] * len(cart_ids))
    cursor = mysql.connection.cursor()
    cursor.execute(
        f"SELECT * FROM events WHERE id IN ({format_strings})",
        tuple(cart_ids)
    )
    cart_items = cursor.fetchall()
    cursor.close()

    total = sum(item["price"] for item in cart_items)

    return render_template(
        "cart.html",
        cart_items=cart_items,
        total=total,
        razorpay_key=RAZORPAY_KEY_ID
    )

# ======================================================
# EVENT REGISTER  (participant details + payment page)
# ======================================================

@app.route("/event-register")
def event_register():
    # Must be logged in and have items in cart
    if not session.get("loggedin"):
        return redirect(url_for("login") + "?next=cart")

    cart_ids = session.get("cart", [])
    if not cart_ids:
        return redirect(url_for("cart"))

    format_strings = ",".join(["%s"] * len(cart_ids))
    cursor = mysql.connection.cursor()
    cursor.execute(
        f"SELECT * FROM events WHERE id IN ({format_strings})",
        tuple(cart_ids)
    )
    cart_items = cursor.fetchall()
    cursor.close()

    if not cart_items:
        return redirect(url_for("cart"))

    return render_template(
        "event-register.html",
        cart_items=cart_items,
        razorpay_key=RAZORPAY_KEY_ID
    )

# ======================================================
# CREATE RAZORPAY ORDER
# ======================================================

@app.route("/create-order", methods=["POST"])
def create_order():
    data   = request.get_json()
    amount = int(float(data["amount"]) * 100)   # paise

    order = razor_client.order.create({
        "amount":          amount,
        "currency":        "INR",
        "payment_capture": 1
    })

    return jsonify({
        "order_id": order["id"],
        "amount":   amount,
        "key":      RAZORPAY_KEY_ID
    })

# ======================================================
# PAYMENT SUCCESS
# ======================================================

@app.route("/payment-success", methods=["POST"])
def payment_success():
    data       = request.get_json()
    payment_id = data.get("payment_id")
    order_id   = data.get("order_id")
    user_id    = session.get("user_id")
    cart_ids   = session.get("cart", [])

    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    cursor = mysql.connection.cursor()

    for event_id in cart_ids:
        # Skip if already registered for this event
        cursor.execute(
            "SELECT id FROM registrations WHERE user_id=%s AND event_id=%s",
            (user_id, event_id)
        )
        if cursor.fetchone():
            continue

        reg_code = str(uuid.uuid4()).replace("-", "")[:12].upper()
        cursor.execute(
            """
            INSERT INTO registrations
                (user_id, event_id, payment_id, order_id, registration_code, checked_in)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, event_id, payment_id, order_id, reg_code, False)
        )

    mysql.connection.commit()
    cursor.close()

    session["cart"] = []
    session.modified = True

    return jsonify({"success": True})

# ======================================================
# GENERATE QR CODE
# ======================================================

@app.route("/qr/<registration_code>")
def generate_dynamic_qr(registration_code):
    verify_url = request.host_url + "scan/" + registration_code

    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(verify_url)
    qr.make(fit=True)

    img    = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)

    return send_file(buffer, mimetype="image/png")

# ======================================================
# GENERATE TICKET IMAGE
# ======================================================

@app.route("/ticket/<registration_code>")
def generate_ticket(registration_code):
    cursor = mysql.connection.cursor()
    cursor.execute(
        """
        SELECT registrations.*, users.name, users.email, events.title
        FROM registrations
        JOIN users  ON registrations.user_id  = users.id
        JOIN events ON registrations.event_id = events.id
        WHERE registration_code=%s
        """,
        (registration_code,)
    )
    ticket_data = cursor.fetchone()
    cursor.close()

    if not ticket_data:
        return "Invalid Ticket", 404

    template_path = os.path.join("static", "tickets", "template.png")
    ticket = Image.open(template_path)
    draw   = ImageDraw.Draw(ticket)

    font_path   = "arial.ttf"
    normal_font = ImageFont.truetype(font_path, 28)
    small_font  = ImageFont.truetype(font_path, 22)

    draw.text((180, 300), ticket_data["name"],              font=normal_font, fill="white")
    draw.text((180, 380), ticket_data["title"],             font=normal_font, fill="white")
    draw.text((180, 460), ticket_data["registration_code"], font=small_font,  fill="white")

    # Embed QR
    verify_url = request.host_url + "scan/" + registration_code
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(verify_url)
    qr.make(fit=True)

    qr_buf = BytesIO()
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)

    qr_image = Image.open(qr_buf).resize((220, 220))
    ticket.paste(qr_image, (850, 250))

    output = BytesIO()
    ticket.save(output, format="PNG")
    output.seek(0)

    return send_file(output, mimetype="image/png")

# ======================================================
# EMAIL TICKET
# ======================================================

@app.route("/send-ticket/<registration_code>")
def send_ticket(registration_code):
    cursor = mysql.connection.cursor()
    cursor.execute(
        """
        SELECT registrations.*, users.name, users.email, events.title
        FROM registrations
        JOIN users  ON registrations.user_id  = users.id
        JOIN events ON registrations.event_id = events.id
        WHERE registration_code=%s
        """,
        (registration_code,)
    )
    ticket_data = cursor.fetchone()
    cursor.close()

    if not ticket_data:
        return "Invalid Ticket", 404

    msg = Message(
        subject="Your Anchorage 2026 Ticket",
        recipients=[ticket_data["email"]]
    )
    msg.body = (
        f"Hello {ticket_data['name']},\n\n"
        f"Your registration is confirmed!\n\n"
        f"Event: {ticket_data['title']}\n"
        f"Registration Code: {ticket_data['registration_code']}\n\n"
        f"Present the attached ticket at the venue.\n\n"
        f"— Team Anchorage 2026"
    )

    import requests as req
    response = req.get(request.host_url + "ticket/" + registration_code)
    msg.attach("ticket.png", "image/png", response.content)

    mail.send(msg)
    return "Ticket sent successfully!"

# ======================================================
# QR SCAN / ENTRY VERIFICATION
# ======================================================

@app.route("/scan/<registration_code>")
def scan_ticket(registration_code):
    cursor = mysql.connection.cursor()
    cursor.execute(
        """
        SELECT registrations.*, users.name, users.email, events.title
        FROM registrations
        JOIN users  ON registrations.user_id  = users.id
        JOIN events ON registrations.event_id = events.id
        WHERE registration_code=%s
        """,
        (registration_code,)
    )
    ticket = cursor.fetchone()

    if not ticket:
        cursor.close()
        return "<h1 style='color:red;'>❌ INVALID TICKET</h1>", 404

    if ticket["checked_in"]:
        cursor.close()
        return (
            f"<h1 style='color:orange;'>⚠️ ALREADY CHECKED IN</h1>"
            f"<h2>{ticket['name']}</h2>"
            f"<h3>{ticket['title']}</h3>"
        )

    cursor.execute(
        "UPDATE registrations SET checked_in=TRUE, checked_in_time=NOW() WHERE registration_code=%s",
        (registration_code,)
    )
    mysql.connection.commit()
    cursor.close()

    return (
        f"<h1 style='color:lime;'>✅ ENTRY VERIFIED</h1>"
        f"<h2>{ticket['name']}</h2>"
        f"<h3>{ticket['title']}</h3>"
        f"<p>Code: {ticket['registration_code']}</p>"
    )

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
    app.run(debug=True)
