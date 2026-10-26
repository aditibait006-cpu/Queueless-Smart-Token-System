
from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import secrets
from functools import wraps

app = Flask(__name__)
DATABASE = "queueless.db"

ADMIN_EMAIL = "admin@queueless.com"
ADMIN_PASSWORD = "Admin@123"

# Demo authentication for a college project
admin_tokens = set()


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def check_database():
    conn = get_db()
    try:
        rows = conn.execute(
            "PRAGMA index_list(tokens)"
        ).fetchall()
        print("Token table indexes:", [dict(row) for row in rows])
    finally:
        conn.close()



def initialize_database():
    conn = get_db()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_number TEXT NOT NULL,
                name TEXT NOT NULL,
                mobile TEXT NOT NULL,
                service_type TEXT NOT NULL,
                service_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Waiting',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                queue_date TEXT NOT NULL DEFAULT (date('now', 'localtime'))
            )
        """)

        columns = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(tokens)").fetchall()
        ]

        if "queue_date" not in columns:
            conn.execute("""
                ALTER TABLE tokens
                ADD COLUMN queue_date TEXT
            """)

            conn.execute("""
                UPDATE tokens
                SET queue_date = date(created_at, 'localtime')
                WHERE queue_date IS NULL
            """)

        indexes = conn.execute("PRAGMA index_list(tokens)").fetchall()

        has_old_unique_constraint = any(
            index["unique"]
            and [
                col["name"]
                for col in conn.execute(
                    f"PRAGMA index_info('{index['name']}')"
                ).fetchall()
            ] == ["service_type", "service_name", "token_number"]
            and index["origin"] == "u"
            for index in indexes
        )

        if has_old_unique_constraint:
            conn.execute("""
                CREATE TABLE tokens_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_number TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mobile TEXT NOT NULL,
                    service_type TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Waiting',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    queue_date TEXT,
                    UNIQUE(service_type, service_name, token_number, queue_date)
                )
            """)

            conn.execute("""
                INSERT INTO tokens_new
                (id, token_number, name, mobile, service_type,
                 service_name, status, created_at, queue_date)
                SELECT id, token_number, name, mobile, service_type,
                       service_name, status, created_at, queue_date
                FROM tokens
            """)

            conn.execute("DROP TABLE tokens")
            conn.execute("ALTER TABLE tokens_new RENAME TO tokens")

        conn.execute("DROP INDEX IF EXISTS idx_service_token_unique")

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_service_token_unique
            ON tokens(service_type, service_name, token_number, queue_date)
        """)

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()



def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")

        if not auth.startswith("Bearer "):
            return jsonify({"error": "Admin login required"}), 401

        token = auth[7:]

        if token not in admin_tokens:
            return jsonify({"error": "Invalid or expired login"}), 401

        return func(*args, **kwargs)

    return wrapper


def read_service_filters():
    service_type = str(
        request.args.get("service_type", "")
    ).strip().title()

    service_name = str(
        request.args.get("service_name", "")
    ).strip()

    return service_type, service_name


def valid_service(service_type, service_name):
    return (
        service_type in ("College", "Bank")
        and bool(service_name)
    )


@app.route("/")
def home():
    return send_from_directory(".", "preview.html")


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "running"})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))

    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        session_token = secrets.token_urlsafe(32)
        admin_tokens.add(session_token)

        return jsonify({
            "message": "Login successful",
            "token": session_token,
            "role": "admin"
        })

    return jsonify({"error": "Invalid email or password"}), 401



@app.route("/api/tokens", methods=["POST"])
def book_token():
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    mobile = str(data.get("mobile", "")).strip()
    service_type = str(data.get("service_type", "")).strip().title()
    service_name = str(data.get("service_name", "")).strip()

    if not name or not mobile or not service_name:
        return jsonify({"error": "Name, mobile and service name are required"}), 400

    if service_type not in ("College", "Bank"):
        return jsonify({"error": "Choose College or Bank only"}), 400

    if not mobile.isdigit() or len(mobile) != 10:
        return jsonify({"error": "Enter a valid 10-digit mobile number"}), 400

    if not valid_service(service_type, service_name):
        return jsonify({"error": "Choose a valid service"}), 400

    prefix = "COL" if service_type == "College" else "BNK"
    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        # Use today's local date for the queue.
        today = conn.execute(
            "SELECT date('now', 'localtime')"
        ).fetchone()[0]

        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   MAX(CAST(SUBSTR(token_number, 5) AS INTEGER)) AS last_number
            FROM tokens
            WHERE service_type = ?
              AND service_name = ?
              AND queue_date = ?
              AND token_number LIKE ?
            """,
            (service_type, service_name, today, prefix + "-%")
        ).fetchone()

        if row["total"] >= 50:
            conn.rollback()
            return jsonify({
                "error": "Daily limit of 50 tokens reached for this service"
            }), 409

        count = (row["last_number"] or 0) + 1
        token_number = f"{prefix}-{count:03d}"

        cursor = conn.execute(
            """
            INSERT INTO tokens
            (token_number, name, mobile, service_type,
             service_name, queue_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                token_number, name, mobile, service_type,
                service_name, today
            )
        )

        result = conn.execute(
            "SELECT * FROM tokens WHERE id = ?",
            (cursor.lastrowid,)
        ).fetchone()

        conn.commit()

        return jsonify({
            "message": "Token booked successfully",
            "token": dict(result)
        }), 201

    except sqlite3.IntegrityError:
        conn.rollback()
        app.logger.exception("Token booking integrity error")
        return jsonify({"error": "Token conflict; please try again"}), 409

    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Database error while booking token")
        return jsonify({"error": "Database error while booking token"}), 500

    finally:
        conn.close()




@app.route("/api/tokens/<token_number>", methods=["GET"])
def get_token_status(token_number):
    service_type, service_name = read_service_filters()

    if not valid_service(service_type, service_name):
        return jsonify({
            "error": "Provide service_type and service_name"
        }), 400

    conn = get_db()

    try:
        token = conn.execute(
            """
            SELECT id, token_number, name, service_type, service_name,
                   status, created_at
            FROM tokens
            WHERE token_number = ?
              AND service_type = ?
              AND service_name = ?
              AND date(created_at) = date('now', 'localtime')
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                token_number.strip().upper(),
                service_type,
                service_name
            )
        ).fetchone()

        if token is None:
            return jsonify({"error": "Token not found"}), 404

        ahead = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM tokens
            WHERE service_type = ?
              AND service_name = ?
              AND date(created_at) = date('now', 'localtime')
              AND id < ?
              AND status = 'Waiting'
            """,
            (
                service_type,
                service_name,
                token["id"]
            )
        ).fetchone()["total"]

        return jsonify({
            "token": dict(token),
            "ahead": ahead,
            "wait": ahead * 5
        })

    except sqlite3.Error:
        app.logger.exception("Error checking token status")
        return jsonify({"error": "Could not check token status"}), 500

    finally:
        conn.close()



@app.route("/api/queue", methods=["GET"])
def public_queue():
    service_type, service_name = read_service_filters()

    if not valid_service(service_type, service_name):
        return jsonify({
            "error": "Provide service_type and service_name"
        }), 400

    conn = get_db()

    try:
        rows = conn.execute(
            """
            SELECT token_number, service_type, service_name,
                   status, created_at
            FROM tokens
            WHERE service_type = ?
              AND service_name = ?
              AND date(created_at) = date('now', 'localtime')
            ORDER BY id ASC
            """,
            (service_type, service_name)
        ).fetchall()

        return jsonify({
            "queue": [dict(row) for row in rows]
        })

    finally:
        conn.close()


@app.route("/api/admin/queue", methods=["GET"])
@admin_required
def admin_queue():
    service_type, service_name = read_service_filters()
    conn = get_db()

    try:
        if service_type or service_name:
            if not valid_service(service_type, service_name):
                return jsonify({
                    "error": "Provide both service_type and service_name"
                }), 400

            rows = conn.execute(
                """
                SELECT *
                FROM tokens
                WHERE service_type = ?
                  AND service_name = ?
                  AND date(created_at) = date('now', 'localtime')
                ORDER BY id ASC
                """,
                (service_type, service_name)
            ).fetchall()
        else:
            # No filters means return all today's services.
            rows = conn.execute(
                """
                SELECT *
                FROM tokens
                WHERE date(created_at) = date('now', 'localtime')
                ORDER BY id ASC
                """
            ).fetchall()

        return jsonify({
            "queue": [dict(row) for row in rows]
        })

    finally:
        conn.close()


@app.route("/api/admin/next", methods=["POST"])
@admin_required
def call_next_token():
    data = request.get_json(silent=True) or {}

    service_type = str(
        data.get("service_type", "")
    ).strip().title()

    service_name = str(
        data.get("service_name", "")
    ).strip()

    if not valid_service(service_type, service_name):
        return jsonify({
            "error": "Choose a centre and a specific service"
        }), 400

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        current = conn.execute(
            """
            SELECT id
            FROM tokens
            WHERE service_type = ?
              AND service_name = ?
              AND status = 'Serving'
              AND date(created_at) = date('now', 'localtime')
            ORDER BY id ASC
            LIMIT 1
            """,
            (service_type, service_name)
        ).fetchone()

        if current:
            conn.execute(
                "UPDATE tokens SET status = 'Completed' WHERE id = ?",
                (current["id"],)
            )

        next_token = conn.execute(
            """
            SELECT id, token_number
            FROM tokens
            WHERE service_type = ?
              AND service_name = ?
              AND status = 'Waiting'
              AND date(created_at) = date('now', 'localtime')
            ORDER BY id ASC
            LIMIT 1
            """,
            (service_type, service_name)
        ).fetchone()

        if next_token:
            conn.execute(
                "UPDATE tokens SET status = 'Serving' WHERE id = ?",
                (next_token["id"],)
            )

        conn.commit()

        return jsonify({
            "message": "Queue updated",
            "next_token_id": (
                next_token["id"] if next_token else None
            ),
            "called": (
                next_token["token_number"] if next_token else None
            ),
            "service_type": service_type,
            "service_name": service_name
        })

    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Error updating queue")
        return jsonify({"error": "Could not update queue"}), 500

    finally:
        conn.close()


@app.route("/api/admin/reset", methods=["POST"])
@admin_required
def reset_queue():
    data = request.get_json(silent=True) or {}

    service_type = str(data.get("service_type", "")).strip().title()
    service_name = str(data.get("service_name", "")).strip()

    if not valid_service(service_type, service_name):
        return jsonify({
            "error": "Choose a centre and a specific service"
        }), 400

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            """
            DELETE FROM tokens
            WHERE service_type = ?
              AND service_name = ?
              AND date(created_at) = date('now', 'localtime')
            """,
            (service_type, service_name)
        )

        conn.commit()

        return jsonify({
            "message": f"{service_type} - {service_name} queue cleared"
        })

    except sqlite3.Error:
        conn.rollback()
        app.logger.exception("Error resetting queue")
        return jsonify({"error": "Could not reset queue"}), 500

    finally:
        conn.close()

initialize_database()

if __name__ == "__main__":
    check_database()
    app.run(debug=True)
