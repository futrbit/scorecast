import os
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app
from google.cloud.firestore_v1.base_query import FieldFilter

# --- Firebase Initialization ---
# IMPORTANT: Use environment variables for secure credential management.
firebase_key_string = os.environ.get("FIREBASE_KEY")
db = None # Initialize db as None

try:
    if firebase_key_string:
        firebase_key_dict = json.loads(firebase_key_string)
        cred = credentials.Certificate(firebase_key_dict)
        if not firebase_admin._apps:
            initialize_app(cred)
        db = firestore.client()
        print("Firebase initialized successfully from environment variable.")
    else:
        print("FIREBASE_KEY environment variable not found. The app will not be able to connect to Firestore.")
except Exception as e:
    print(f"[ERROR] Error initializing Firebase from environment variable: {e}")

# --- Global Constants and Data ---
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
# Set a more robust BASE_DIR by assuming it's the current working directory
BASE_DIR = os.getcwd()
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "a_very_secret_key")

socketio = SocketIO(app, async_mode='gevent')

# --- Utility Function for Loading Local JSON Files ---
def load_static_json_file(filename):
    """Loads a static JSON file from the project's root directory."""
    filepath_relative = os.path.join(BASE_DIR, filename)
    filepath_src = os.path.join(BASE_DIR, 'src', filename) # For local testing if files are in src
    
    try:
        if os.path.exists(filepath_relative):
            with open(filepath_relative, 'r') as f:
                return json.load(f)
        elif os.path.exists(filepath_src):
            with open(filepath_src, 'r') as f:
                return json.load(f)
        else:
            print(f"[ERROR] Could not find {filename} at {filepath_relative} or {filepath_src}")
            return {}
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[ERROR] Could not load {filename}: {e}")
        return {}

# Load static stadium data once on app startup
stadiums = load_static_json_file("stadium_traits.json")

# --- Route for serving team logos and character images ---
@app.route('/static/<path:filename>')
def static_files(filename):
    """Serve all static files from the static directory."""
    # This path is correct for Render's file structure where the static directory is at the root
    filepath = os.path.join(BASE_DIR, 'static')
    if not os.path.exists(filepath):
        # Fallback for local testing if the static folder is in a 'src' subdirectory
        filepath = os.path.join(BASE_DIR, 'src', 'static')
    
    if not os.path.exists(filepath):
        print(f"[ERROR] Static directory not found: {filepath}")
        return "Static directory not found", 404

    return send_from_directory(filepath, filename)

# --- Utility Functions for Firestore Data Fetching ---

def get_current_week_from_firestore():
    """Fetches the current week from Firestore, defaults to 1."""
    if not db: return 1
    try:
        week_doc = db.collection('settings').document('state').get()
        if week_doc.exists:
            return week_doc.to_dict().get('current_week', 1)
    except Exception as e:
        print(f"[ERROR] Failed to fetch current week from Firestore: {e}")
    return 1

def get_deadlines_from_firestore():
    """Fetches prediction deadlines from Firestore."""
    if not db: return {}
    try:
        deadlines_doc = db.collection('deadlines').document('all_deadlines').get()
        if deadlines_doc.exists:
            return deadlines_doc.to_dict().get('deadlines', {})
    except Exception as e:
        print(f"[ERROR] Failed to fetch deadlines from Firestore: {e}")
    return {}

def get_actual_results_for_week(week):
    """Fetches actual results for a specific week from Firestore."""
    if not db: return {}
    try:
        results_doc = db.collection('actual_results').document(str(week)).get()
        if results_doc.exists:
            return results_doc.to_dict().get('results', {})
    except Exception as e:
        print(f"[ERROR] Failed to fetch actual results for week {week}: {e}")
    return {}


# --- Utility Functions ---

def update_user_points_for_week(username, week, new_points):
    """Updates a user's points in Firestore for a given week."""
    if not db: return
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
    """Recalculates points for all users for a specific week based on actual results."""
    if not db: return
    try:
        actual_week_data = get_actual_results_for_week(week)
        if not actual_week_data:
            print(f"[INFO] No actual results for week {week}. Skipping point update.")
            return

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
    """Adds stadium and city info to fixture objects using the globally loaded stadium data."""
    for fixture in fixtures_list:
        home_team = fixture['match'].split(" vs ")[0]
        # Use the globally loaded 'stadiums' variable
        stadium_info = stadiums.get(home_team, {})
        fixture['stadium'] = stadium_info.get('stadium')
        fixture['city'] = stadium_info.get('city')
    return fixtures_list

def parse_fixtures_dates(fixtures_list):
    """Parses date and time strings into datetime objects."""
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
    
@app.route('/check_db')
def check_db():
    if not db:
        return "Database not connected. Please check your environment variables.", 500
    try:
        # Attempt a simple query to verify connection
        db.collection('settings').document('state').get()
        return "Database connection successful! 🎉", 200
    except Exception as e:
        return f"Database connection failed: {e}", 500

@app.route('/register', methods=["GET", "POST"])
def register():
    print("[DEBUG] Register route accessed.")
    if not db:
        print("[ERROR] Database not connected. Redirecting to index.")
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    characters = []
    characters_folder_path = os.path.join(app.root_path, 'static', 'characters')
    try:
        if os.path.exists(characters_folder_path):
            character_files = [f for f in os.listdir(characters_folder_path) if f.lower().endswith(('.jpeg', '.jpg', '.png'))]
            characters = [
                {'id': i + 1, 'name': os.path.splitext(f)[0], 'image': f}
                for i, f in enumerate(character_files)
            ]
            print(f"[DEBUG] Found {len(characters)} characters.")
        else:
            print(f"[ERROR] Directory not found: {characters_folder_path}")
            flash("Character images not found. Contact the administrator.", "error")
    except Exception as e:
        print(f"[ERROR] Failed to load character images from {characters_folder_path}: {e}")
        flash(f"Failed to load characters: {str(e)}", "error")

    if request.method == "POST":
        print("[DEBUG] Processing POST request for registration.")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        character_image_name = request.form.get("character", "")
        
        print(f"[DEBUG] Form data received - Username: '{username}', Character: '{character_image_name}'")

        if not username or not password or not confirm_password or not character_image_name:
            print("[ERROR] Missing required form fields.")
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            print("[ERROR] Passwords do not match.")
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        if len(password) < 8:
            print("[ERROR] Password is too short.")
            flash("Password must be at least 8 characters long.", "error")
            return redirect(url_for("register"))
        
        selected_character = next((c for c in characters if c['image'] == character_image_name), None)
        if not selected_character:
            print(f"[ERROR] Selected character '{character_image_name}' not found in loaded characters list.")
            flash("Invalid character selection.", "error")
            return redirect(url_for("register"))


        try:
            print(f"[DEBUG] Checking if username '{username}' exists.")
            user_ref = db.collection('users').document(username)
            if user_ref.get().exists:
                print(f"[ERROR] Username '{username}' already exists.")
                flash("Username already taken. Please choose another.", "error")
                return redirect(url_for("register"))
            
            print(f"[DEBUG] Username '{username}' is available. Hashing password.")
            hashed_pw = generate_password_hash(password)
            
            user_data = {
                "password": hashed_pw,
                "points": 0,
                "group": "A",
                "character": selected_character['name'],
                "predictions": {},
                "points_by_week": {}
            }
            
            print(f"[DEBUG] Saving new user data for '{username}'.")
            user_ref.set(user_data)
            print(f"[DEBUG] User '{username}' successfully registered and saved.")
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("login"))
            
        except Exception as e:
            print(f"[CRITICAL ERROR] Error during registration process: {e}")
            flash(f"An unexpected error occurred: {str(e)}. Please try again.", "error")
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
            print(f"[ERROR] Firestore error during login: {e}")
            
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
    
    # Fetch all data from Firestore
    current_week = get_current_week_from_firestore()
    prediction_deadlines = get_deadlines_from_firestore()
    actual_results = get_actual_results_for_week(current_week)

    try:
        fixtures_docs = db.collection('fixtures').where(filter=FieldFilter("week", "==", current_week)).stream()
        fixtures_list = [doc.to_dict() for doc in fixtures_docs]
        fixtures_list.sort(key=lambda x: x.get("order", float('inf')))

        # Use the globally loaded 'stadiums' variable
        fixtures_with_info = attach_stadium_info(fixtures_list)
        fixtures_with_info = parse_fixtures_dates(fixtures_with_info)
    except Exception as e:
        print(f"[ERROR] Failed to load fixtures for profile: {e}")
        fixtures_with_info = []

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
            
            user_data["predictions"][str(current_week)] = user_week_preds
            db.collection('users').document(username).update({"predictions": user_data["predictions"]})
            
            flash(f"Predictions saved!", "success")
            return redirect(url_for("profile"))

    except Exception as e:
        flash(f"Error loading user data: {str(e)}", "error")
        print(f"[ERROR] Firestore error loading user data: {e}")
        return redirect(url_for("logout"))

    return render_template("profile.html", username=username, user=user_data,
                           fixtures=fixtures_with_info, predictions=user_preds, actuals=actual_results,
                           current_week=current_week, prediction_deadline=prediction_deadline, now=now)


@app.route('/leaderboard')
def leaderboard():
    if not db:
        flash("Database not connected. Please contact the administrator.", "error")
        return redirect(url_for("index"))
        
    current_week = get_current_week_from_firestore()

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
        print(f"[ERROR] Firestore error loading leaderboard data: {e}")
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
        
    # Fetch all data from Firestore
    current_week = get_current_week_from_firestore()
    prediction_deadlines = get_deadlines_from_firestore()

    if request.method == "POST":
        try:
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
        
            if "update_results" in request.form:
                fixtures_docs = db.collection('fixtures').where(filter=FieldFilter("week", "==", current_week)).stream()
                fixtures_list = [doc.to_dict() for doc in fixtures_docs]
                week_actuals = {}
                for fixture in fixtures_list:
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
            print(f"[ERROR] Firestore error processing form: {e}")
            
        return redirect(url_for("admin_panel"))

    # GET: Render template
    try:
        fixtures_docs = db.collection('fixtures').where(filter=FieldFilter("week", "==", current_week)).stream()
        fixtures_list = [doc.to_dict() for doc in fixtures_docs]
        fixtures_list.sort(key=lambda x: x.get("order", float('inf')))

        week_actuals = get_actual_results_for_week(current_week)
    except Exception as e:
        flash(f"Error loading admin data: {str(e)}", "error")
        print(f"[ERROR] Firestore error loading admin data: {e}")
        fixtures_list = []
        week_actuals = {}
    
    deadline_str = prediction_deadlines.get(str(current_week))
    prediction_deadline = None
    if deadline_str:
        try:
            prediction_deadline = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("Invalid deadline format in data.", "error")
    
    # Use the globally loaded 'stadiums' variable
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
        print(f"[ERROR] Firestore error during data reset: {e}")

    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    with app.app_context():
        print("Registered routes:")
        for rule in app.url_map.iter_rules():
            print(f"[DEBUG] Endpoint: {rule.endpoint}, URL: {rule}")
    socketio.run(app, debug=True)
