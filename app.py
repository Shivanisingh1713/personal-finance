from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATHS = {
    "users": DATA_DIR / "users.db",
    "income": DATA_DIR / "income.db",
    "expenses": DATA_DIR / "expenses.db",
    "planning": DATA_DIR / "planning.db",
    "bills": DATA_DIR / "bills.db",
    "investments": DATA_DIR / "investments.db",
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))


def get_db(name: str) -> sqlite3.Connection:
    cache_key = f"db_{name}"
    if not hasattr(g, cache_key):
        connection = sqlite3.connect(DB_PATHS[name])
        connection.row_factory = sqlite3.Row
        setattr(g, cache_key, connection)
    return getattr(g, cache_key)


@app.teardown_appcontext
def close_connections(exception: Exception | None) -> None:
    for key in list(vars(g).keys()):
        if key.startswith("db_"):
            getattr(g, key).close()
            delattr(g, key)


def init_databases() -> None:
    DATA_DIR.mkdir(exist_ok=True)

    user_db = sqlite3.connect(DB_PATHS["users"])
    user_db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            profile_note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    user_db.commit()
    user_db.close()

    income_db = sqlite3.connect(DB_PATHS["income"])
    income_db.execute(
        """
        CREATE TABLE IF NOT EXISTS income_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            received_on TEXT NOT NULL,
            notes TEXT DEFAULT ''
        )
        """
    )
    income_db.commit()
    income_db.close()

    expenses_db = sqlite3.connect(DB_PATHS["expenses"])
    expenses_db.execute(
        """
        CREATE TABLE IF NOT EXISTS expense_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            merchant TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            spent_on TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'Manual',
            notes TEXT DEFAULT ''
        )
        """
    )
    expenses_db.execute(
        """
        CREATE TABLE IF NOT EXISTS bank_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            synced_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    expenses_db.commit()
    expenses_db.close()

    planning_db = sqlite3.connect(DB_PATHS["planning"])
    planning_db.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            monthly_limit REAL NOT NULL,
            alert_threshold REAL NOT NULL DEFAULT 80
        )
        """
    )
    planning_db.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            target_amount REAL NOT NULL,
            current_amount REAL NOT NULL DEFAULT 0,
            target_date TEXT NOT NULL,
            strategy TEXT DEFAULT ''
        )
        """
    )
    planning_db.commit()
    planning_db.close()

    bills_db = sqlite3.connect(DB_PATHS["bills"])
    bills_db.execute(
        """
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            biller TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            due_date TEXT NOT NULL,
            reminder_days INTEGER NOT NULL DEFAULT 3,
            status TEXT NOT NULL DEFAULT 'Scheduled',
            last_paid_on TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        )
        """
    )
    bills_db.commit()
    bills_db.close()

    investment_db = sqlite3.connect(DB_PATHS["investments"])
    investment_db.execute(
        """
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            instrument TEXT NOT NULL,
            platform TEXT NOT NULL,
            invested_amount REAL NOT NULL,
            current_value REAL NOT NULL,
            risk_level TEXT NOT NULL,
            invested_on TEXT NOT NULL,
            notes TEXT DEFAULT ''
        )
        """
    )
    investment_db.commit()
    investment_db.close()


init_databases()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


@app.before_request
def load_user() -> None:
    user_id = session.get("user_id")
    g.user = None
    if user_id:
        g.user = get_db("users").execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


@app.context_processor
def inject_globals():
    return {"today": date.today().isoformat()}


def parse_amount(field_name: str) -> float:
    try:
        return float(request.form.get(field_name, "0").strip())
    except ValueError as exc:
        raise ValueError(f"Invalid value for {field_name.replace('_', ' ')}.") from exc


def current_month_range() -> tuple[str, str]:
    today = date.today()
    start = today.replace(day=1)
    if today.month == 12:
        next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month = today.replace(month=today.month + 1, day=1)
    return start.isoformat(), next_month.isoformat()


def fetch_rows(query: str, params: tuple, database: str) -> list[sqlite3.Row]:
    return get_db(database).execute(query, params).fetchall()


def execute_write(query: str, params: tuple, database: str) -> None:
    db = get_db(database)
    db.execute(query, params)
    db.commit()


def monthly_expense_totals(user_id: int) -> dict[str, float]:
    start, end = current_month_range()
    rows = fetch_rows(
        """
        SELECT category, SUM(amount) AS total
        FROM expense_entries
        WHERE user_id = ? AND spent_on >= ? AND spent_on < ?
        GROUP BY category
        ORDER BY total DESC
        """,
        (user_id, start, end),
        "expenses",
    )
    return {row["category"]: row["total"] or 0 for row in rows}


def build_dashboard_data(user_id: int) -> dict:
    start, end = current_month_range()
    income_total = get_db("income").execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total FROM income_entries
        WHERE user_id = ? AND received_on >= ? AND received_on < ?
        """,
        (user_id, start, end),
    ).fetchone()["total"]
    expense_total = get_db("expenses").execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total FROM expense_entries
        WHERE user_id = ? AND spent_on >= ? AND spent_on < ?
        """,
        (user_id, start, end),
    ).fetchone()["total"]
    investment = get_db("investments").execute(
        """
        SELECT COALESCE(SUM(invested_amount), 0) AS invested,
               COALESCE(SUM(current_value), 0) AS current_value
        FROM investments
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    goals = fetch_rows(
        "SELECT * FROM goals WHERE user_id = ? ORDER BY target_date ASC",
        (user_id,),
        "planning",
    )
    bills = fetch_rows(
        "SELECT * FROM bills WHERE user_id = ? ORDER BY due_date ASC",
        (user_id,),
        "bills",
    )
    budgets = fetch_rows(
        "SELECT * FROM budgets WHERE user_id = ? ORDER BY category ASC",
        (user_id,),
        "planning",
    )
    expenses_by_category = monthly_expense_totals(user_id)
    monthly_series = defaultdict(lambda: {"income": 0.0, "expense": 0.0})

    income_rows = fetch_rows(
        "SELECT substr(received_on, 1, 7) AS month, amount FROM income_entries WHERE user_id = ?",
        (user_id,),
        "income",
    )
    for row in income_rows:
        monthly_series[row["month"]]["income"] += row["amount"]

    expense_rows = fetch_rows(
        "SELECT substr(spent_on, 1, 7) AS month, amount FROM expense_entries WHERE user_id = ?",
        (user_id,),
        "expenses",
    )
    for row in expense_rows:
        monthly_series[row["month"]]["expense"] += row["amount"]

    ordered_months = sorted(monthly_series.keys())[-6:]
    income_line = [round(monthly_series[month]["income"], 2) for month in ordered_months]
    expense_line = [round(monthly_series[month]["expense"], 2) for month in ordered_months]

    savings_rate = 0
    if income_total > 0:
        savings_rate = round(((income_total - expense_total) / income_total) * 100, 1)

    budget_progress = []
    for budget in budgets:
        spent = expenses_by_category.get(budget["category"], 0)
        progress = round((spent / budget["monthly_limit"]) * 100, 1) if budget["monthly_limit"] else 0
        budget_progress.append(
            {
                "category": budget["category"],
                "limit": budget["monthly_limit"],
                "spent": round(spent, 2),
                "progress": progress,
                "threshold": budget["alert_threshold"],
            }
        )

    return {
        "month_income": round(income_total, 2),
        "month_expenses": round(expense_total, 2),
        "net_balance": round(income_total - expense_total, 2),
        "savings_rate": savings_rate,
        "invested_total": round(investment["invested"], 2),
        "portfolio_value": round(investment["current_value"], 2),
        "portfolio_growth": round(investment["current_value"] - investment["invested"], 2),
        "goals": goals,
        "bills": bills,
        "budget_progress": budget_progress,
        "expense_categories": expenses_by_category,
        "chart_data": {
            "pie": {
                "labels": list(expenses_by_category.keys()),
                "values": [round(value, 2) for value in expenses_by_category.values()],
            },
            "line": {
                "labels": ordered_months,
                "income": income_line,
                "expenses": expense_line,
            },
        },
    }


def simulate_bank_transactions(role: str) -> list[dict]:
    base_amount = 18 if role == "student" else 45
    today = date.today()
    return [
        {
            "merchant": "Metro Grocery",
            "category": "Food",
            "amount": base_amount + 12,
            "spent_on": today.isoformat(),
            "source": "Bank API",
            "notes": "Auto-imported from mock bank statement",
        },
        {
            "merchant": "City Transit",
            "category": "Travel",
            "amount": base_amount - 6,
            "spent_on": (today - timedelta(days=1)).isoformat(),
            "source": "Bank API",
            "notes": "Tagged via simulated bank API",
        },
        {
            "merchant": "Utility Cloud",
            "category": "Utilities",
            "amount": base_amount + 25,
            "spent_on": (today - timedelta(days=2)).isoformat(),
            "source": "Bank API",
            "notes": "Categorized by mock statement parser",
        },
    ]


def simulate_investments(role: str) -> list[dict]:
    if role == "student":
        return [
            {
                "instrument": "Student Index Fund",
                "platform": "Campus Investment API",
                "invested_amount": 1200,
                "current_value": 1284,
                "risk_level": "Low",
                "invested_on": (date.today() - timedelta(days=180)).isoformat(),
                "notes": "Starter diversified portfolio",
            }
        ]
    return [
        {
            "instrument": "Balanced Growth ETF",
            "platform": "Mock Wealth API",
            "invested_amount": 6000,
            "current_value": 6720,
            "risk_level": "Medium",
            "invested_on": (date.today() - timedelta(days=420)).isoformat(),
            "notes": "Stable long-term allocation",
        },
        {
            "instrument": "Retirement Bond Basket",
            "platform": "Mock Wealth API",
            "invested_amount": 3200,
            "current_value": 3445,
            "risk_level": "Low",
            "invested_on": (date.today() - timedelta(days=560)).isoformat(),
            "notes": "Income-oriented debt exposure",
        },
    ]


def seed_user_starter_data(user_id: int, role: str) -> None:
    income_db = get_db("income")
    exists = income_db.execute(
        "SELECT COUNT(*) AS count FROM income_entries WHERE user_id = ?",
        (user_id,),
    ).fetchone()["count"]
    if exists:
        return

    today = date.today()
    incomes = [
        (
            user_id,
            "Monthly Salary" if role == "general" else "Scholarship",
            "Salary" if role == "general" else "Allowance",
            4200 if role == "general" else 800,
            today.replace(day=3).isoformat(),
            "Starter dataset",
        ),
        (
            user_id,
            "Side Gig" if role == "general" else "Freelance Tutoring",
            "Investments" if role == "general" else "Side Income",
            650 if role == "general" else 180,
            today.replace(day=12).isoformat(),
            "Starter dataset",
        ),
    ]
    income_db.executemany(
        """
        INSERT INTO income_entries (user_id, source, category, amount, received_on, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        incomes,
    )
    income_db.commit()

    expense_db = get_db("expenses")
    expenses = [
        (
            user_id,
            "FreshMart",
            "Food",
            210 if role == "general" else 75,
            today.replace(day=5).isoformat(),
            "Manual",
            "Starter dataset",
        ),
        (
            user_id,
            "Apartment Rent",
            "Rent",
            1200 if role == "general" else 300,
            today.replace(day=1).isoformat(),
            "Manual",
            "Starter dataset",
        ),
        (
            user_id,
            "Fuel Pass",
            "Travel",
            95 if role == "general" else 40,
            today.replace(day=7).isoformat(),
            "Manual",
            "Starter dataset",
        ),
    ]
    expense_db.executemany(
        """
        INSERT INTO expense_entries (user_id, merchant, category, amount, spent_on, source, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        expenses,
    )
    expense_db.commit()

    planning_db = get_db("planning")
    planning_db.executemany(
        """
        INSERT INTO budgets (user_id, category, monthly_limit, alert_threshold)
        VALUES (?, ?, ?, ?)
        """,
        [
            (user_id, "Food", 400 if role == "general" else 150, 80),
            (user_id, "Travel", 180 if role == "general" else 90, 85),
            (user_id, "Rent", 1400 if role == "general" else 350, 90),
        ],
    )
    planning_db.executemany(
        """
        INSERT INTO goals (user_id, title, target_amount, current_amount, target_date, strategy)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                user_id,
                "Emergency Cushion",
                5000 if role == "general" else 1200,
                1650 if role == "general" else 350,
                (today + timedelta(days=240)).isoformat(),
                "Auto-transfer 15% of monthly surplus.",
            )
        ],
    )
    planning_db.commit()

    bills_db = get_db("bills")
    bills_db.executemany(
        """
        INSERT INTO bills (user_id, biller, category, amount, due_date, reminder_days, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                user_id,
                "Electricity Board",
                "Utilities",
                88 if role == "student" else 145,
                (today + timedelta(days=4)).isoformat(),
                3,
                "Scheduled",
                "Starter recurring bill",
            ),
            (
                user_id,
                "Internet Provider",
                "Subscription",
                35 if role == "student" else 60,
                (today + timedelta(days=8)).isoformat(),
                2,
                "Scheduled",
                "Starter recurring bill",
            ),
        ],
    )
    bills_db.commit()

    investment_db = get_db("investments")
    investment_db.executemany(
        """
        INSERT INTO investments
        (user_id, instrument, platform, invested_amount, current_value, risk_level, invested_on, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                user_id,
                item["instrument"],
                item["platform"],
                item["invested_amount"],
                item["current_value"],
                item["risk_level"],
                item["invested_on"],
                item["notes"],
            )
            for item in simulate_investments(role)
        ],
    )
    investment_db.commit()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "general")

        if not name or not email or not password:
            flash("Please complete all registration fields.", "danger")
            return render_template("register.html")

        try:
            db = get_db("users")
            cursor = db.execute(
                """
                INSERT INTO users (name, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    email,
                    generate_password_hash(password),
                    role,
                    datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
            session["user_id"] = cursor.lastrowid
            seed_user_starter_data(cursor.lastrowid, role)
            flash(
                "Your account is ready with starter data to explore the platform.",
                "success",
            )
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            flash("That email is already registered. Please sign in instead.", "warning")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db("users").execute(
            "SELECT * FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['name']}.", "success")
            return redirect(url_for("dashboard"))
        flash("Incorrect email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        note = request.form.get("profile_note", "").strip()
        execute_write(
            "UPDATE users SET profile_note = ? WHERE id = ?",
            (note, g.user["id"]),
            "users",
        )
        flash("Profile preferences updated.", "success")
        return redirect(url_for("profile"))
    user = get_db("users").execute(
        "SELECT * FROM users WHERE id = ?",
        (g.user["id"],),
    ).fetchone()
    return render_template("profile.html", user=user)


@app.route("/dashboard")
@login_required
def dashboard():
    report = build_dashboard_data(g.user["id"])
    return render_template(
        "dashboard.html",
        report=report,
        chart_data=json.dumps(report["chart_data"]),
    )


@app.route("/income", methods=["GET", "POST"])
@login_required
def income():
    if request.method == "POST":
        try:
            amount = parse_amount("amount")
            execute_write(
                """
                INSERT INTO income_entries (user_id, source, category, amount, received_on, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    g.user["id"],
                    request.form.get("source", "").strip(),
                    request.form.get("category", "").strip(),
                    amount,
                    request.form.get("received_on") or date.today().isoformat(),
                    request.form.get("notes", "").strip(),
                ),
                "income",
            )
            flash("Income entry recorded.", "success")
            return redirect(url_for("income"))
        except ValueError as exc:
            flash(str(exc), "danger")

    entries = fetch_rows(
        "SELECT * FROM income_entries WHERE user_id = ? ORDER BY received_on DESC, id DESC",
        (g.user["id"],),
        "income",
    )
    return render_template("income.html", entries=entries)


@app.route("/expenses", methods=["GET", "POST"])
@login_required
def expenses():
    if request.method == "POST":
        try:
            amount = parse_amount("amount")
            execute_write(
                """
                INSERT INTO expense_entries (user_id, merchant, category, amount, spent_on, source, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    g.user["id"],
                    request.form.get("merchant", "").strip(),
                    request.form.get("category", "").strip(),
                    amount,
                    request.form.get("spent_on") or date.today().isoformat(),
                    request.form.get("source", "Manual").strip() or "Manual",
                    request.form.get("notes", "").strip(),
                ),
                "expenses",
            )
            flash("Expense saved.", "success")
            return redirect(url_for("expenses"))
        except ValueError as exc:
            flash(str(exc), "danger")

    entries = fetch_rows(
        "SELECT * FROM expense_entries WHERE user_id = ? ORDER BY spent_on DESC, id DESC",
        (g.user["id"],),
        "expenses",
    )
    return render_template("expenses.html", entries=entries)


@app.route("/bank/sync", methods=["POST"])
@login_required
def bank_sync():
    transactions = simulate_bank_transactions(g.user["role"])
    db = get_db("expenses")
    db.executemany(
        """
        INSERT INTO expense_entries (user_id, merchant, category, amount, spent_on, source, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                g.user["id"],
                item["merchant"],
                item["category"],
                item["amount"],
                item["spent_on"],
                item["source"],
                item["notes"],
            )
            for item in transactions
        ],
    )
    db.execute(
        "INSERT INTO bank_sync_log (user_id, synced_at, payload) VALUES (?, ?, ?)",
        (g.user["id"], datetime.utcnow().isoformat(), json.dumps(transactions)),
    )
    db.commit()
    flash("Mock bank statement synced and categorized automatically.", "success")
    return redirect(url_for("expenses"))


@app.route("/planning", methods=["GET", "POST"])
@login_required
def planning():
    form_type = request.form.get("form_type")
    if request.method == "POST":
        try:
            if form_type == "budget":
                execute_write(
                    """
                    INSERT INTO budgets (user_id, category, monthly_limit, alert_threshold)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        g.user["id"],
                        request.form.get("category", "").strip(),
                        parse_amount("monthly_limit"),
                        parse_amount("alert_threshold"),
                    ),
                    "planning",
                )
                flash("Budget configured.", "success")
            elif form_type == "goal":
                execute_write(
                    """
                    INSERT INTO goals (user_id, title, target_amount, current_amount, target_date, strategy)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        g.user["id"],
                        request.form.get("title", "").strip(),
                        parse_amount("target_amount"),
                        parse_amount("current_amount"),
                        request.form.get("target_date") or date.today().isoformat(),
                        request.form.get("strategy", "").strip(),
                    ),
                    "planning",
                )
                flash("Financial goal added.", "success")
            return redirect(url_for("planning"))
        except ValueError as exc:
            flash(str(exc), "danger")

    budgets = fetch_rows(
        "SELECT * FROM budgets WHERE user_id = ? ORDER BY category ASC",
        (g.user["id"],),
        "planning",
    )
    goals = fetch_rows(
        "SELECT * FROM goals WHERE user_id = ? ORDER BY target_date ASC",
        (g.user["id"],),
        "planning",
    )
    expense_totals = monthly_expense_totals(g.user["id"])
    return render_template(
        "planning.html",
        budgets=budgets,
        goals=goals,
        expense_totals=expense_totals,
    )


@app.route("/bills", methods=["GET", "POST"])
@login_required
def bills():
    if request.method == "POST":
        try:
            execute_write(
                """
                INSERT INTO bills (user_id, biller, category, amount, due_date, reminder_days, status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    g.user["id"],
                    request.form.get("biller", "").strip(),
                    request.form.get("category", "").strip(),
                    parse_amount("amount"),
                    request.form.get("due_date") or date.today().isoformat(),
                    int(request.form.get("reminder_days", "3")),
                    "Scheduled",
                    request.form.get("notes", "").strip(),
                ),
                "bills",
            )
            flash("Bill scheduled with reminder settings.", "success")
            return redirect(url_for("bills"))
        except ValueError as exc:
            flash(str(exc), "danger")

    items = fetch_rows(
        "SELECT * FROM bills WHERE user_id = ? ORDER BY due_date ASC",
        (g.user["id"],),
        "bills",
    )
    return render_template("bills.html", bills=items)


@app.route("/bills/pay/<int:bill_id>", methods=["POST"])
@login_required
def pay_bill(bill_id: int):
    execute_write(
        """
        UPDATE bills
        SET status = 'Paid', last_paid_on = ?
        WHERE id = ? AND user_id = ?
        """,
        (date.today().isoformat(), bill_id, g.user["id"]),
        "bills",
    )
    flash("Bill paid through the simulated bill service.", "success")
    return redirect(url_for("bills"))


@app.route("/investments", methods=["GET", "POST"])
@login_required
def investments():
    if request.method == "POST":
        try:
            execute_write(
                """
                INSERT INTO investments
                (user_id, instrument, platform, invested_amount, current_value, risk_level, invested_on, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    g.user["id"],
                    request.form.get("instrument", "").strip(),
                    request.form.get("platform", "").strip(),
                    parse_amount("invested_amount"),
                    parse_amount("current_value"),
                    request.form.get("risk_level", "").strip(),
                    request.form.get("invested_on") or date.today().isoformat(),
                    request.form.get("notes", "").strip(),
                ),
                "investments",
            )
            flash("Investment position logged.", "success")
            return redirect(url_for("investments"))
        except ValueError as exc:
            flash(str(exc), "danger")

    positions = fetch_rows(
        "SELECT * FROM investments WHERE user_id = ? ORDER BY invested_on DESC, id DESC",
        (g.user["id"],),
        "investments",
    )
    total_invested = sum(item["invested_amount"] for item in positions)
    portfolio_value = sum(item["current_value"] for item in positions)
    return render_template(
        "investments.html",
        positions=positions,
        total_invested=round(total_invested, 2),
        portfolio_value=round(portfolio_value, 2),
    )


@app.route("/investments/sync", methods=["POST"])
@login_required
def sync_investments():
    db = get_db("investments")
    db.executemany(
        """
        INSERT INTO investments
        (user_id, instrument, platform, invested_amount, current_value, risk_level, invested_on, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                g.user["id"],
                item["instrument"],
                item["platform"],
                item["invested_amount"],
                item["current_value"],
                item["risk_level"],
                item["invested_on"],
                item["notes"],
            )
            for item in simulate_investments(g.user["role"])
        ],
    )
    db.commit()
    flash("Investment platform API synced a fresh portfolio snapshot.", "success")
    return redirect(url_for("investments"))


@app.route("/reports")
@login_required
def reports():
    report = build_dashboard_data(g.user["id"])
    recommendations = []
    if report["savings_rate"] < 20:
        recommendations.append(
            "Reduce variable spending categories and move at least 20% of monthly income into savings."
        )
    if report["portfolio_growth"] < 0:
        recommendations.append(
            "Review underperforming positions and rebalance toward lower-volatility instruments."
        )
    if not recommendations:
        recommendations.append(
            "Current habits support steady wealth growth. Continue tracking and review goals monthly."
        )
    recommendations.append(
        "Use budget thresholds and bill reminders together to avoid late fees and overspending."
    )
    return render_template(
        "reports.html",
        report=report,
        recommendations=recommendations,
    )


if __name__ == "__main__":
    init_databases()
    app.run(debug=True)
