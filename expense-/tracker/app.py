from flask import Flask, render_template, request, redirect, url_for, session, flash
from database.db import get_db, init_db
from controller.access_controller import UserAccessController, access_required, admin_required

app = Flask(__name__)
app.secret_key = "fleetflow_secret_key_2026"

# Initialize database on startup
with app.app_context():
    init_db()


# ------------------------------------------------------------------ #
# Common Routes                                                     #
# ------------------------------------------------------------------ #

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and user['password'] == password: # In production, use werkzeug.security
            session['user_id'] = user['user_id']
            session['email'] = user['email']
            session['role'] = user['role']
            flash("Welcome back to FleetFlow!", "success")
            return redirect(url_for("trucks"))

        return render_template("login.html", error="Invalid email or password")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        if not email or not password:
            return render_template("register.html", error="Email and password are required")

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (email, password, is_approved) VALUES (?, ?, 0)",
                (email, password)
            )
            conn.commit()
        except Exception as e:
            conn.close()
            return render_template("register.html", error="Registration failed. Email might already exist.")
        finally:
            conn.close()

        return render_template("register.html", success="Registration successful! Please wait for admin approval.")

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("landing"))


@app.route("/pending-approval")
def pending_approval():
    return render_template("pending_approval.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ------------------------------------------------------------------ #
# Fleet Management Module                                           #
# ------------------------------------------------------------------ #

@app.route("/trucks")
@access_required
def trucks():
    conn = get_db()
    trucks = conn.execute("SELECT * FROM trucks").fetchall()

    total_trucks = len(trucks)
    available_trucks = len([t for t in trucks if t['status'] == 'Available'])
    in_trip_trucks = len([t for t in trucks if t['status'] == 'In Trip'])
    maint_trucks = len([t for t in trucks if t['status'] == 'Maintenance'])

    conn.close()
    return render_template("trucks.html",
                           trucks=trucks,
                           total_trucks=total_trucks,
                           available_trucks=available_trucks,
                           in_trip_trucks=in_trip_trucks,
                           maint_trucks=maint_trucks,
                           block_content='dashboard')


@app.route("/trucks/add", methods=["GET", "POST"])
@access_required
def add_truck():
    if request.method == "POST":
        reg = request.form.get("registration_number")
        model = request.form.get("model")
        year = request.form.get("year")
        capacity = request.form.get("capacity_tons")
        status = request.form.get("status")
        last_service = request.form.get("last_service_date")

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO trucks (registration_number, model, year, capacity_tons, status, last_service_date) VALUES (?, ?, ?, ?, ?, ?)",
                (reg, model, year, capacity, status, last_service)
            )
            conn.commit()
            flash(f"Truck {reg} added successfully!", "success")
        except Exception as e:
            flash("Error adding truck.", "error")
        finally:
            conn.close()
        return redirect(url_for("trucks"))

    return render_template("truck_form.html", truck=None)


@app.route("/trucks/<int:id>/edit", methods=["GET", "POST"])
@access_required
def edit_truck(id):
    conn = get_db()
    truck = conn.execute("SELECT * FROM trucks WHERE truck_id = ?", (id,)).fetchone()

    if request.method == "POST":
        reg = request.form.get("registration_number")
        model = request.form.get("model")
        year = request.form.get("year")
        capacity = request.form.get("capacity_tons")
        status = request.form.get("status")
        last_service = request.form.get("last_service_date")

        try:
            conn.execute(
                "UPDATE trucks SET registration_number=?, model=?, year=?, capacity_tons=?, status=?, last_service_date=? WHERE truck_id=?",
                (reg, model, year, capacity, status, last_service, id)
            )
            conn.commit()
            flash("Truck updated successfully!", "success")
        except Exception as e:
            flash("Error updating truck.", "error")
        finally:
            conn.close()
        return redirect(url_for("trucks"))

    conn.close()
    return render_template("truck_form.html", truck=truck)


@app.route("/trucks/<int:id>/delete")
@access_required
def delete_truck(id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM trucks WHERE truck_id = ?", (id,))
        conn.commit()
        flash("Truck deleted successfully!", "success")
    except Exception as e:
        flash("Error deleting truck.", "error")
    finally:
        conn.close()
    return redirect(url_for("trucks"))


# ------------------------------------------------------------------ #
# Admin Module                                                       #
# ------------------------------------------------------------------ #

@app.route("/admin/users")
@admin_required
def admin_users():
    conn = get_db()
    pending_users = conn.execute("SELECT * FROM users WHERE is_approved = 0").fetchall()
    conn.close()
    return render_template("admin_users.html", pending_users=pending_users, block_content='dashboard')


@app.route("/admin/approve/<int:id>", methods=["GET", "POST"])
@admin_required
def approve_user(id):
    if request.method == "POST":
        conn = get_db()
        controller = UserAccessController(conn)
        controller.approve_user(id)
        conn.close()
        return redirect(url_for("admin_users"))

    return redirect(url_for("admin_users"))


if __name__ == "__main__":
    app.run(debug=True, port=5001)
