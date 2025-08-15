import os
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app

# --- Firebase Initialization ---
# IMPORTANT: Use environment variables for secure credential management.
firebase_key_string = os.environ.get("FIREBASE_KEY")
db = None # Initialize db as None

try:
    if firebase_key_string:
        # Parse the JSON string into a dictionary
        firebase_key_dict = json.loads(firebase_key_string)
        cred = credentials.Certificate(firebase_key_dict)
        if not firebase_admin._apps:
            initialize_app(cred)
        db = firestore.client()
        print("Firebase initialized successfully from environment variable.")
    else:
        print("FIREBASE_KEY environment variable not found. The app will not be able to connect to Firestore.")
except Exception as e:
    print(f"Error initializing Firebase from environment variable: {e}")

# --- Global Constants and Data (now stored in Firestore) ---
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "a_very_secret_key")

# The fix: explicitly set the async mode to 'gevent'
socketio = SocketIO(app, async_mode='gevent')

# Load data from the database or files
# Note: This function is now a general utility for loading JSON files, not core data
def load_json_file(filename):
    filepath = os.path.join(BASE_DIR, 'data', filename)
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[ERROR] Could not load {filepath}: {e}")
        return {}

def save_json_file(filename, data):
    filepath = os.path.join(BASE_DIR, 'data', filename)
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    except IOError as e:
        print(f"[ERROR] Could not save {filepath}: {e}")

# Load initial data from JSON files
stadiums = load_json_file("stadiums.json")
fixtures = load_json_file("fixtures.json")
actual_results = load_json_file("actual_results.json")
prediction_deadlines = load_json_file("deadlines.json")
current_week = load_json_file("current_week.json").get("current_week", 1)


# Utility Functions for Firestore
def update_user_points_for_week(username, week, new_points):
    if not db: return # Add this check
    try:
        user_ref = db.collection('users').document(username)
        user_doc = user_ref.get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            points_by_week = user_data.get("points_by_week", {})
            points_by_week[str(week)] = new_points
            total_points = sum(points_by_week.values())
            user_ref.update({
                'points_by_week': points_by_week,
                'points': total_points
            })
            print(f"[DEBUG] Updated points for {username}: total={total_points}, week {week}={new_points}")
    except Exception as e:
        print(f"[ERROR] Failed to update user points: {e}")

def update_all_user_points_for_week(week):
    if not db: return # Add this check
    try:
        actuals = db.collection('actual_results').document(str(week)).get()
        if not actuals.exists:
            print(f"[INFO] No actual results for week {week}. Skipping point update.")
            return

        actual_week_data = actuals.to_dict().get("results", {})
        users_stream = db.collection('users').stream()

        for user_doc in users_stream:
            username = user_doc.id
            user_data = user_doc.to_dict()
            user_preds = user_data.get("predictions", {}).get(str(week), {})
            
            if not user_preds:
                continue

            points = 0
            for match, pred in user_preds.items():
                if match in actual_week_data:
                    actual = actual_week_data[match]
                    if pred['home'] == actual['home'] and pred['away'] == actual['away']:
                        points += 3
                    elif (pred['home'] > pred['away'] and actual['home'] > actual['away']) or \
                         (pred['home'] < pred['away'] and actual['home'] < actual['away']) or \
                         (pred['home'] == pred['away'] and actual['home'] == actual['away']):
                        points += 1
            
            update_user_points_for_week(username, week, points)

    except Exception as e:
        print(f"[ERROR] Failed to update all user points: {e}")


def attach_stadium_info(fixtures_list):
    for fixture in fixtures_list:
        home_team = fixture['match'].split(" vs ")[0]
        stadium_info = stadiums.get(home_team, {})
        fixture['stadium'] = stadium_info.get('stadium')
        fixture['city'] = stadium_info.get('city')
    return fixtures_list

def parse_fixtures_dates(fixtures_list):
    for fixture in fixtures_list:
        try:
            fixture_datetime = datetime.strptime(f"{fixture['date']}T{fixture['time']}", "%Y-%m-%dT%H:%M")
            fixture['datetime_obj'] = fixture_datetime
        except (KeyError, ValueError):
            fixture['datetime_obj'] = None
    return fixtures_list

# --- ROUTES ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route('/register', methods=["GET", "POST"])
def register():
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    characters_folder = os.path.join(BASE_DIR, 'static', 'characters')
    characters = []
    try:
        character_files = [f for f in os.listdir(characters_folder) if f.lower().endswith(('.jpeg', '.jpg', '.png'))]
        characters = [
            {'id': i + 1, 'name': os.path.splitext(f)[0], 'image': f'characters/{f}'}
            for i, f in enumerate(character_files)
        ]
    except FileNotFoundError:
        flash("Character images not found. Contact the administrator.", "error")
        print(f"[ERROR] Directory not found: {characters_folder}")

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        confirm_password = request.form.get("confirm_password", "")
        character_id = request.form.get("character")

        if not username or not password or not confirm_password or not character_id:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "error")
            return redirect(url_for("register"))

        try:
            # Check if username already exists in Firestore
            user_ref = db.collection('users').document(username)
            if user_ref.get().exists:
                flash("Username already taken. Please choose another.", "error")
                return redirect(url_for("register"))
        except Exception as e:
            flash(f"Error checking user: {str(e)}", "error")
            return redirect(url_for("register"))

        selected_character = next((c for c in characters if str(c['id']) == character_id), None)
        if not selected_character:
            flash("Invalid character selected.", "error")
            return redirect(url_for("register"))

        hashed_pw = generate_password_hash(password)
        
        # Save user data to Firestore
        user_data = {
            "password": hashed_pw,
            "points": 0,
            "group": "A",
            "character": selected_character['name'],
            "predictions": {},
            "points_by_week": {}
        }
        
        try:
            user_ref.set(user_data)
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash(f"Error saving user data: {str(e)}", "error")
            return redirect(url_for("register"))

    return render_template("register.html", characters=characters)

@app.route('/login', methods=["GET", "POST"])
def login():
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form.get("password")

        if not username or not password:
            flash("Please enter username and password.", "error")
            return redirect(url_for("login"))

        try:
            user_doc = db.collection('users').document(username).get()

            if user_doc.exists:
                user_data = user_doc.to_dict()
                stored_hash = user_data.get("password")

                if stored_hash and check_password_hash(stored_hash, password):
                    session["user"] = username
                    flash(f"Welcome back, {username}!", "success")
                    return redirect(url_for("profile"))
                else:
                    flash("Invalid username or password.", "error")
            else:
                flash("Invalid username or password.", "error")
        except Exception as e:
            flash(f"Error logging in: {str(e)}", "error")
            
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
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    if "user" not in session:
        return redirect(url_for("login"))
    
    username = session["user"]
    now = datetime.utcnow()
    
    # Load fixtures and deadlines from JSON files
    global fixtures, prediction_deadlines, actual_results
    fixtures_with_info = attach_stadium_info(fixtures)
    fixtures_with_info = parse_fixtures_dates(fixtures_with_info)
    
    deadline_str = prediction_deadlines.get(str(current_week))
    prediction_deadline = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M") if deadline_str else datetime.max
    
    try:
        user_doc = db.collection('users').document(username).get()
        if not user_doc.exists:
            flash("User not found.", "error")
            return redirect(url_for("logout"))
            
        user_data = user_doc.to_dict()
        user_preds = user_data.get("predictions", {}).get(str(current_week), {})

        if request.method == "POST":
            if now > prediction_deadline:
                flash("Prediction deadline has passed.", "error")
                return redirect(url_for("profile"))
                
            user_week_preds = {}
            for fixture in fixtures_with_info:
                match = fixture["match"]
                home_key = match.replace(" ", "_").replace(".", "_") + "_home"
                away_key = match.replace(" ", "_").replace("-", "_") + "_away"
                home_score = request.form.get(home_key)
                away_score = request.form.get(away_key)
                
                if home_score and away_score and home_score.isdigit() and away_score.isdigit():
                    user_week_preds[match] = {"home": int(home_score), "away": int(away_score)}
            
            # Save predictions to Firestore
            user_data["predictions"][str(current_week)] = user_week_preds
            db.collection('users').document(username).update({"predictions": user_data["predictions"]})

            # Recalculate and save points
            pts = 0
            if str(current_week) in actual_results:
                actual_week_data = actual_results.get(str(current_week), {})
                for match, pred in user_week_preds.items():
                    if match in actual_week_data:
                        actual = actual_week_data[match]
                        if pred['home'] == actual['home'] and pred['away'] == actual['away']:
                            pts += 3
                        elif (pred['home'] > pred['away'] and actual['home'] > actual['away']) or \
                             (pred['home'] < pred['away'] and actual['home'] < actual['away']) or \
                             (pred['home'] == pred['away'] and actual['home'] == actual['away']):
                            pts += 1
            
            user_data["points_by_week"].setdefault(str(current_week), 0)
            user_data["points_by_week"][str(current_week)] = pts
            user_data["points"] = sum(user_data["points_by_week"].values())
            
            db.collection('users').document(username).update({
                "points_by_week": user_data["points_by_week"],
                "points": user_data["points"]
            })
            
            flash(f"Predictions saved! You earned {pts} points this week.", "success")
            return redirect(url_for("profile"))

    except Exception as e:
        flash(f"Error loading user data: {str(e)}", "error")
        return redirect(url_for("logout"))

    actuals = actual_results.get(str(current_week), {})
    return render_template("profile.html", username=username, user=user_data,
                           fixtures=fixtures_with_info, predictions=user_preds, actuals=actuals,
                           current_week=current_week, prediction_deadline=prediction_deadline, now=now)


@app.route('/leaderboard')
def leaderboard():
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    try:
        users_stream = db.collection('users').stream()
        all_users = {doc.id: doc.to_dict() for doc in users_stream}
        sorted_users = sorted(all_users.items(), key=lambda x: x[1].get("points", 0), reverse=True)
        
        all_weeks = set()
        for _, data in all_users.items():
            if "points_by_week" in data:
                all_weeks.update(data["points_by_week"].keys())
        weeks = sorted(all_weeks, key=int)
        
    except Exception as e:
        flash(f"Error loading leaderboard data: {str(e)}", "error")
        sorted_users = []
        weeks = []

    return render_template("leaderboard.html", users=sorted_users, weeks=weeks,
                           show_weekly=True, current_week=current_week)

@app.route('/admin', methods=["GET", "POST"])
def admin():
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
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
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    if not session.get("admin"):
        flash("Admin login required.", "error")
        return redirect(url_for("admin"))
        
    global current_week, fixtures, prediction_deadlines, actual_results
    
    if request.method == "POST":
        try:
            # Update Settings
            if "save_settings" in request.form:
                new_week = request.form.get("current_week")
                if new_week and new_week.isdigit():
                    current_week = int(new_week)
                    db.collection('settings').document('state').set({'current_week': current_week})
                    flash(f"Current week set to {current_week}", "success")
                else:
                    flash("Invalid week number.", "error")
                    
                deadline_str = request.form.get("prediction_deadline")
                if deadline_str:
                    try:
                        datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
                        prediction_deadlines[str(current_week)] = deadline_str
                        db.collection('deadlines').document('all_deadlines').set({'deadlines': prediction_deadlines})
                        flash("Prediction deadline updated.", "success")
                    except ValueError:
                        flash("Invalid deadline format.", "error")
        
            # Update Fixtures
            if "update_fixtures" in request.form:
                updated_fixtures = []
                i = 1
                while f"fixture_{i}_home" in request.form:
                    home = request.form.get(f"fixture_{i}_home", "").strip()
                    away = request.form.get(f"fixture_{i}_away", "").strip()
                    date = request.form.get(f"fixture_{i}_date", "").strip()
                    time = request.form.get(f"fixture_{i}_time", "").strip()
                    order = request.form.get(f"fixture_{i}_order", str(i)).strip()
                    
                    if home and away and date and time:
                        try:
                            datetime.strptime(f"{date}T{time}", "%Y-%m-%dT%H:%M")
                            fixture_doc = {
                                "match": f"{home} vs {away}",
                                "date": date,
                                "time": time,
                                "order": int(order) if order.isdigit() else i,
                                "week": current_week
                            }
                            updated_fixtures.append(fixture_doc)
                            
                        except ValueError:
                            flash(f"Fixture {i}: Invalid date/time format. Use YYYY-MM-DD and HH:MM.", "error")
                    i += 1
                
                if updated_fixtures:
                    batch = db.batch()
                    for fixture in updated_fixtures:
                        fixture_ref = db.collection('fixtures').document()
                        batch.set(fixture_ref, fixture)
                    batch.commit()
                    flash(f"Saved {len(updated_fixtures)} fixtures to Firestore.", "success")
                else:
                    flash("No valid fixtures provided.", "error")
        
            # Update Scores
            if "update_results" in request.form:
                week_actuals = {}
                for fixture in fixtures:
                    match = fixture["match"]
                    home_key = match.replace(" ", "_").replace(".", "_") + "_home"
                    away_key = match.replace(" ", "_").replace("-", "_") + "_away"
                    home_score = request.form.get(home_key)
                    away_score = request.form.get(away_key)
                    
                    if home_score and away_score and home_score.isdigit() and away_score.isdigit():
                        week_actuals[match] = {
                            "home": int(home_score),
                            "away": int(away_score)
                        }
                
                if week_actuals:
                    db.collection('actual_results').document(str(current_week)).set({'results': week_actuals})
                    update_all_user_points_for_week(current_week)
                    flash("Actual results updated and points recalculated.", "success")
                else:
                    flash("No valid scores provided.", "error")
        
            if "reset_data" in request.form:
                return redirect(url_for("admin_reset"))

        except Exception as e:
            flash(f"Error processing form: {str(e)}", "error")
            
        return redirect(url_for("admin_panel"))

    # GET: Render template
    # Load data from Firestore for display
    try:
        current_week_doc = db.collection('settings').document('state').get()
        if current_week_doc.exists:
            current_week = current_week_doc.to_dict().get('current_week', 1)

        # Use firestore.FieldFilter directly
        fixtures_docs = db.collection('fixtures').where(filter=firestore.FieldFilter("week", "==", current_week)).stream()
        fixtures_list = [doc.to_dict() for doc in fixtures_docs]
        fixtures_list.sort(key=lambda x: x.get("order", float('inf')))

        actuals_doc = db.collection('actual_results').document(str(current_week)).get()
        week_actuals = actuals_doc.to_dict().get('results', {}) if actuals_doc.exists else {}

        deadlines_doc = db.collection('deadlines').document('all_deadlines').get()
        prediction_deadlines = deadlines_doc.to_dict().get('deadlines', {}) if deadlines_doc.exists else {}

    except Exception as e:
        flash(f"Error loading admin data: {str(e)}", "error")
        fixtures_list = []
        week_actuals = {}
        prediction_deadlines = {}
    
    deadline_str = prediction_deadlines.get(str(current_week))
    prediction_deadline = None
    if deadline_str:
        try:
            prediction_deadline = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("Invalid deadline format in data.", "error")
    
    fixtures_with_info = attach_stadium_info(fixtures_list)
    fixtures_with_info = parse_fixtures_dates(fixtures_with_info)

    return render_template("admin_panel.html",
                           current_week=current_week,
                           fixtures=fixtures_with_info,
                           actuals=week_actuals,
                           prediction_deadline=prediction_deadline)

@app.route('/admin/reset')
def admin_reset():
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    if not session.get("admin"):
        flash("Admin login required to perform reset.", "error")
        return redirect(url_for("admin"))

    try:
        collections_to_reset = ['users', 'fixtures', 'actual_results', 'deadlines', 'settings']
        for collection_name in collections_to_reset:
            collection_ref = db.collection(collection_name)
            docs = collection_ref.stream()
            for doc in docs:
                doc.reference.delete()
        
        flash("All data has been reset. App is now in a clean state.", "success")
    except Exception as e:
        flash(f"Error during data reset: {str(e)}", "error")

    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    with app.app_context():
        print("Registered routes:")
        for rule in app.url_map.iter_rules():
            print(f"[DEBUG] Endpoint: {rule.endpoint}, URL: {rule}")
    socketio.run(app, debug=True)
