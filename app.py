from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import json
import os
from dotenv import load_dotenv
load_dotenv()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "adminpass123")
import copy
from werkzeug.security import generate_password_hash, check_password_hash
from chat import init_chat, socketio  # Import SocketIO and chat logic
import eventlet
eventlet.monkey_patch()

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()  # Secure random key
app.permanent_session_lifetime = timedelta(days=5)

socketio.init_app(app)  # Explicitly initialize for gunicorn

if __name__ == "__main__":
    with app.app_context():
        print("Registered routes:")
        for rule in app.url_map.iter_rules():
            print(f"[DEBUG] Endpoint: {rule.endpoint}, URL: {rule}")
    port = int(os.getenv("PORT", 10000))  # Match Render's port for local testing
    socketio.run(app, host="0.0.0.0", port=port, debug=True)

# === Base directory for all data files ===
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def get_data_path(filename):
    filepath = os.path.join(BASE_DIR, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    return filepath

# === Load News Data ===
def load_news():
    try:
        with open(get_data_path("filtered_football_news.json"), "r", encoding="utf-8") as f:
            news = json.load(f)
            print(f"[DEBUG] Loaded {len(news)} articles from filtered_football_news.json: {news}")
            return news
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[ERROR] Failed to load filtered_football_news.json: {str(e)}")
        return []

# === Jinja filter to convert string to datetime ===
@app.template_filter('todatetime')
def todatetime_filter(value, format="%Y-%m-%dT%H:%M"):
    try:
        return datetime.strptime(value, format)
    except Exception:
        return value

# === Persistent Storage Helpers ===
def load_data(filename, default):
    filepath = get_data_path(filename)
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_data(filename, data):
    filepath = get_data_path(filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[DEBUG] Saved {filename} to {filepath}")
    except Exception as e:
        print(f"[ERROR] Failed to save {filename}: {str(e)}")
        raise

# === Load or Create Data Files ===
users = load_data("users.json", {})
predictions = load_data("predictions.json", {})
actual_results = load_data("actual_results.json", {})
fixtures = load_data("fixtures.json", [])
current_week = load_data("current_week.json", 1)
if isinstance(current_week, str) and current_week.isdigit():
    current_week = int(current_week)
prediction_deadlines = load_data("deadlines.json", {
    "1": "2025-06-09T23:59",
    "2": "2025-06-19T23:59",
})

stadium_traits = load_data("stadium_traits.json", [])

# === Helper to attach stadium info to fixtures ===
def attach_stadium_info(fixtures_list):
    club_to_stadium = {s["club"]: s for s in stadium_traits}
    fixtures_with_info = copy.deepcopy(fixtures_list)
    for fixture in fixtures_with_info:
        home_team = fixture["match"].split(" vs ")[0]
        stadium_data = club_to_stadium.get(home_team)
        if stadium_data:
            fixture["stadium"] = stadium_data.get("name")
            fixture["stadium_info"] = stadium_data
        else:
            fixture["stadium"] = None
            fixture["stadium_info"] = None
    return fixtures_with_info

# === Helper to parse fixture date and time strings ===
def parse_fixtures_dates(fixtures_list):
    for fixture in fixtures_list:
        date_str = fixture.get("date")
        time_str = fixture.get("time")
        if date_str and time_str:
            try:
                fixture["date_dt"] = datetime.strptime(f"{date_str}T{time_str}", "%Y-%m-%dT%H:%M")
            except Exception:
                fixture["date_dt"] = None
        else:
            fixture["date_dt"] = None
    return fixtures_list

# === Core Logic ===
def get_result(home, away):
    if home > away:
        return "win"
    elif home == away:
        return "draw"
    else:
        return "loss"

def calculate_points_for_user_week(username, week):
    user_preds = predictions.get(username, {}).get(str(week), {})
    actuals = actual_results.get(str(week), {})
    user_points = 0
    group = users.get(username, {}).get("group", "A")
    for fixture in fixtures:
        match = fixture["match"]
        if match not in user_preds or match not in actuals:
            continue
        pred_home = user_preds[match]["home"]
        pred_away = user_preds[match]["away"]
        actual_home = actuals[match]["home"]
        actual_away = actuals[match]["away"]
        pred_result = get_result(pred_home, pred_away)
        actual_result = get_result(actual_home, actual_away)
        if pred_result == actual_result:
            points = 1
            if pred_home == actual_home and pred_away == actual_away:
                points = 3
            others = [u for u, d in users.items() if d.get("group") == group and u != username]
            others_correct = False
            for other in others:
                other_preds = predictions.get(other, {}).get(str(week), {})
                if match in other_preds:
                    oh, oa = other_preds[match]["home"], other_preds[match]["away"]
                    if get_result(oh, oa) == actual_result:
                        others_correct = True
                        break
            if not others_correct:
                points *= 2
            user_points += points
    return user_points

def update_all_user_points_for_week(week):
    for username in users:
        pts = calculate_points_for_user_week(username, week)
        users[username].setdefault("points_by_week", {})[str(week)] = pts
        users[username]["points"] = sum(users[username].get("points_by_week", {}).values())
    save_data("users.json", users)

# === Routes ===
@app.route('/team_logo/<path:filename>')
def team_logo(filename):
    return send_from_directory('static/team_logo', filename)

@app.route('/stadium_image/<path:filename>')
def stadium_image(filename):
    return send_from_directory('static/stadium_image', filename)

@app.route('/')
def index():
    if "user" in session:
        return redirect(url_for("profile"))
    fresh_fixtures = attach_stadium_info(fixtures)
    fresh_fixtures = parse_fixtures_dates(fresh_fixtures)
    news = load_news()[:5]  # Load first 5 articles
    deadline_str = prediction_deadlines.get(str(current_week))
    deadline = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M") if deadline_str else None
    print(f"[DEBUG] Index - Deadline: {deadline}, News: {len(news)} articles")
    return render_template(
        "index.html",
        fixtures=fresh_fixtures,
        current_week=current_week,
        news=news,
        deadline=deadline
    )

@app.route('/news')
def news():
    news = load_news()  # Load all articles
    return render_template("news.html", news=news)

@app.route('/register', methods=["GET", "POST"])
def register():
    characters_folder = os.path.join(BASE_DIR, 'static', 'characters')
    characters = []
    try:
        character_files = [f for f in os.listdir(characters_folder) if f.lower().endswith(('.jpeg', '.jpg', '.png'))]
        characters = [
            {'id': i + 1, 'name': os.path.splitext(f)[0], 'image': f'characters/{f}'}
            for i, f in enumerate(character_files)
        ]
        print(f"[DEBUG] Characters folder: {os.path.abspath(characters_folder)}")
        print(f"[DEBUG] Found files: {character_files}")
        print(f"[DEBUG] Characters list: {characters}")
    except FileNotFoundError:
        flash("Character images not found. Contact the administrator.", "error")
        print(f"[ERROR] Directory not found: {characters_folder}")

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        confirm_password = request.form.get("confirm_password", "")
        character_id = request.form.get("character")

        print(f"[DEBUG] Form data: {request.form}")
        if not username or not password or not confirm_password or not character_id:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "error")
            return redirect(url_for("register"))

        if username in users:
            flash("Username already taken. Please choose another.", "error")
            return redirect(url_for("register"))

        selected_character = next((c for c in characters if str(c['id']) == character_id), None)
        if not selected_character:
            flash("Invalid character selected.", "error")
            return redirect(url_for("register"))

        hashed_pw = generate_password_hash(password)
        users[username] = {
            "password": hashed_pw,
            "points": 0,
            "group": "A",
            "points_by_week": {},
            "character": selected_character['name']
        }
        try:
            save_data("users.json", users)
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash(f"Error saving user data: {str(e)}", "error")
            return redirect(url_for("register"))

    return render_template("register.html", characters=characters)

@app.route('/login', methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form.get("password")

        if not username or not password:
            flash("Please enter username and password.", "error")
            return redirect(url_for("login"))

        user = users.get(username)
        if not user:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

        if "password" not in user:
            flash("User has no password set. Please register.", "error")
            return redirect(url_for("register"))

        if not check_password_hash(user["password"], password):
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

        session["user"] = username
        session["username"] = username
        flash(f"Welcome back, {username}!", "success")
        return redirect(url_for("profile"))

    return render_template("login.html")

@app.route('/logout')
def logout():
    session.pop("user", None)
    session.pop("username", None)
    session.pop("admin", None)
    flash("Logged out successfully.", "success")
    return redirect(url_for("index"))

@app.route('/profile', methods=["GET", "POST"])
def profile():
    if "user" not in session:
        return redirect(url_for("login"))
    username = session["user"]
    now = datetime.utcnow()
    deadline_str = prediction_deadlines.get(str(current_week))
    prediction_deadline = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M") if deadline_str else datetime.max
    fixtures_with_info = attach_stadium_info(fixtures)
    fixtures_with_info = parse_fixtures_dates(fixtures_with_info)
    if request.method == "POST":
        if now > prediction_deadline:
            flash("Prediction deadline has passed.", "error")
            return redirect(url_for("profile"))
        user_week_preds = {}
        for fixture in fixtures_with_info:
            match = fixture["match"]
            home_key = match.replace(" ", "_") + "_home"
            away_key = match.replace(" ", "_") + "_away"
            home_score = request.form.get(home_key)
            away_score = request.form.get(away_key)
            if home_score and away_score and home_score.isdigit() and away_score.isdigit():
                user_week_preds[match] = {"home": int(home_score), "away": int(away_score)}
        predictions.setdefault(username, {})[str(current_week)] = user_week_preds
        save_data("predictions.json", predictions)
        pts = calculate_points_for_user_week(username, current_week)
        users[username].setdefault("points_by_week", {})[str(current_week)] = pts
        users[username]["points"] = sum(users[username].get("points_by_week", {}).values())
        save_data("users.json", users)
        flash(f"Predictions saved! You earned {pts} points this week.", "success")
        return redirect(url_for("profile"))
    user_data = users.get(username, {"points": 0, "group": "A"})
    user_preds = predictions.get(username, {}).get(str(current_week), {})
    actuals = actual_results.get(str(current_week), {})
    return render_template("profile.html", username=username, user=user_data,
                           fixtures=fixtures_with_info, predictions=user_preds, actuals=actuals,
                           current_week=current_week, prediction_deadline=prediction_deadline, now=now)

@app.route('/leaderboard')
def leaderboard():
    sorted_users = sorted(users.items(), key=lambda x: x[1].get("points", 0), reverse=True)
    all_weeks = set()
    for _, data in sorted_users:
        if "points_by_week" in data:
            all_weeks.update(data["points_by_week"].keys())
    weeks = sorted(all_weeks, key=int)
    return render_template("leaderboard.html", users=sorted_users, weeks=weeks,
                           show_weekly=True, current_week=current_week)

@app.route('/admin', methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        password = request.form.get("password")
        if password != ADMIN_PASSWORD:
            flash("Incorrect admin password.", "error")
            return redirect(url_for("admin"))
        session["admin"] = True
        return redirect(url_for("admin_panel"))
    return render_template("admin_login.html")

@app.route('/admin/panel', methods=["GET", "POST"])
def admin_panel():
    if not session.get("admin"):
        flash("Admin login required.", "error")
        return redirect(url_for("admin"))
    
    global current_week, fixtures, prediction_deadlines, actual_results
    form_data = session.get("form_data", {})  # Initialize form_data
    
    if request.method == "POST":
        print(f"[DEBUG] Form data: {request.form}")
        form_data = dict(request.form)  # Update form_data
        session["form_data"] = form_data  # Store in session
        
        try:
            # Update Settings
            if request.form.get("save_settings"):
                new_week = request.form.get("current_week")
                if new_week and new_week.isdigit():
                    current_week = int(new_week)
                    save_data("current_week.json", current_week)
                    flash(f"Current week set to {current_week}", "success")
                else:
                    flash("Invalid week number.", "error")
                
                deadline_str = request.form.get("prediction_deadline")
                if deadline_str:
                    try:
                        datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
                        prediction_deadlines[str(current_week)] = deadline_str
                        save_data("deadlines.json", prediction_deadlines)
                        flash("Prediction deadline updated.", "success")
                    except ValueError:
                        flash("Invalid deadline format.", "error")
            
            # Update Fixtures
            if request.form.get("update_fixtures"):
                updated_fixtures = []
                i = 1
                while f"fixture_{i}_home" in request.form:
                    home = request.form.get(f"fixture_{i}_home", "").strip()
                    away = request.form.get(f"fixture_{i}_away", "").strip()
                    date = request.form.get(f"fixture_{i}_date", "").strip()
                    time = request.form.get(f"fixture_{i}_time", "").strip()
                    order = request.form.get(f"fixture_{i}_order", str(i)).strip()
                    print(f"[DEBUG] Fixture {i}: home={home}, away={away}, date={date}, time={time}, order={order}")
                    if home and away and date and time:
                        try:
                            datetime.strptime(f"{date}T{time}", "%Y-%m-%dT%H:%M")
                            updated_fixtures.append({
                                "match": f"{home} vs {away}",
                                "date": date,
                                "time": time,
                                "order": int(order) if order.isdigit() else i
                            })
                        except ValueError:
                            flash(f"Fixture {i}: Invalid date/time format. Use YYYY-MM-DD and HH:MM.", "error")
                    else:
                        missing = [f for f, v in [("home", home), ("away", away), ("date", date), ("time", time)] if not v]
                        if missing:
                            flash(f"Fixture {i}: Missing {', '.join(missing)}.", "error")
                    i += 1
                if updated_fixtures:
                    fixtures = sorted(updated_fixtures, key=lambda x: x.get("order", float('inf')))
                    save_data("fixtures.json", fixtures)
                    flash(f"Saved {len(fixtures)} fixtures.", "success")
                    # Clear saved fixtures from form_data
                    session["form_data"] = {k: v for k, v in form_data.items() if not any(f"fixture_{j}_" in k for j in range(1, len(fixtures) + 1))}
                else:
                    flash("No valid fixtures provided.", "error")
            
            # Update Scores
            if request.form.get("update_results"):
                week_actuals = actual_results.setdefault(str(current_week), {})
                for fixture in fixtures:
                    match = fixture["match"]
                    home_key = match.replace(" ", "_").replace(".", "_") + "_home"
                    away_key = match.replace(" ", "_").replace("-", "_") + "_away"
                    home_score = request.form.get(home_key)
                    away_score = request.form.get(away_key)
                    print(f"[DEBUG] Scores for {match}: home={home_score}, away={away_score}")
                    if home_score and away_score and home_score.isdigit() and away_score.isdigit():
                        week_actuals[match] = {
                            "home": int(home_score),
                            "away": int(away_score)
                        }
                    elif home_score or away_score:
                        flash(f"Invalid scores for {match}. Both home and away scores required.", "error")
                if week_actuals:
                    save_data("actual_results.json", actual_results)
                    update_all_user_points_for_week(current_week)
                    flash("Actual results updated and points recalculated.", "success")
                else:
                    flash("No valid scores provided.", "error")
            
            # Reset Season
            if request.form.get("reset_data"):
                session.pop("form_data", None)
                return redirect(url_for("admin_reset"))

        except Exception as e:
            flash(f"Error processing form: {str(e)}", "error")
            print(f"[DEBUG] Error processing form: {str(e)}")
        
        return redirect(url_for("admin_panel"))

    # GET: Render template
    week_actuals = actual_results.get(str(current_week), {})
    deadline_str = prediction_deadlines.get(str(current_week))
    prediction_deadline = None
    if deadline_str:
        try:
            prediction_deadline = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("Invalid deadline format in data.", "error")
    
    fixtures_with_info = attach_stadium_info(fixtures)
    fixtures_with_info = parse_fixtures_dates(fixtures_with_info)
    fixtures_with_info.sort(key=lambda x: x.get("order", float('inf')))
    
    print(f"[DEBUG] Rendering admin_panel with: current_week={current_week}, fixtures={len(fixtures_with_info)} fixtures")
    
    return render_template("admin_panel.html", 
                           current_week=current_week,
                           fixtures=fixtures_with_info,
                           actuals=week_actuals,
                           prediction_deadline=prediction_deadline,
                           form_data=form_data)

@app.route('/admin/reset')
def admin_reset():
    if not session.get("admin"):
        flash("Admin login required to perform reset.", "error")
        return redirect(url_for("admin"))
    global current_week, users, predictions, actual_results, fixtures, prediction_deadlines
    current_week = 1
    users = {}
    predictions = {}
    actual_results = {}
    fixtures = []
    prediction_deadlines = {}
    save_data("current_week.json", current_week)
    save_data("users.json", users)
    save_data("predictions.json", predictions)
    save_data("actual_results.json", actual_results)
    save_data("fixtures.json", fixtures)
    save_data("deadlines.json", prediction_deadlines)
    flash("All data has been reset. App is now in a clean state.", "success")
    return redirect(url_for("admin_panel"))

if __name__ == "__main__":
    with app.app_context():
        print("Registered routes:")
        for rule in app.url_map.iter_rules():
            print(f"[DEBUG] Endpoint: {rule.endpoint}, URL: {rule}")
    socketio.run(app, debug=True)