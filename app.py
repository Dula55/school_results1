#!/usr/bin/env python3
import os
import sqlite3
import hashlib
import secrets
import traceback
import sys
import time
import threading
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, request, jsonify, session, render_template,
    redirect, url_for, make_response
)
from flask_cors import CORS
from flask_session import Session

# Global lock for database initialization
db_init_lock = threading.Lock()
db_initialized = False

# --------------------------
# Basic config / DB helpers
# --------------------------
sqlite3.register_converter("timestamp", lambda b: b.decode('utf-8') if b else None)

# Get the base directory for the application
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Check if we're on Fly.io
ON_FLY = os.environ.get('FLY_APP_NAME') is not None

# Database path configuration
if ON_FLY:
    # On Fly.io, we can use the volume mount if configured
    PERSISTENT_DIR = '/data'
    if os.path.exists(PERSISTENT_DIR):
        DB_PATH = os.path.join(PERSISTENT_DIR, 'davis_academy.db')
        print(f"✅ Using persistent volume at {PERSISTENT_DIR}")
    else:
        DB_PATH = '/tmp/davis_academy.db'
        print("⚠️ WARNING: No volume mounted! Data will NOT persist!")
else:
    DB_PATH = os.path.join(BASE_DIR, 'davis_academy.db')

def get_db():
    """Get a database connection with proper error handling"""
    try:
        # Ensure the directory exists
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        # Connect with extended timeout and error handling
        db = sqlite3.connect(DB_PATH, timeout=30)
        db.row_factory = sqlite3.Row

        # Optimize database for concurrent access
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA busy_timeout = 5000")

        return db
    except Exception as e:
        print(f"Database connection error: {e}")
        traceback.print_exc()
        raise

def safe_db_operation(operation, *args, **kwargs):
    """Wrapper for database operations with retry logic"""
    max_retries = 3
    retry_delay = 1

    for attempt in range(max_retries):
        conn = None
        try:
            conn = get_db()
            result = operation(conn, *args, **kwargs)
            return result
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) or "busy" in str(e).lower():
                if attempt < max_retries - 1:
                    print(f"Database locked, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
            raise
        except Exception as e:
            print(f"Database operation error: {e}")
            traceback.print_exc()
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    raise Exception("Max retries exceeded for database operation")

def hash_password(password):
    """Hash password with SHA256 - trim whitespace and handle None"""
    if password is None:
        return None
    return hashlib.sha256(str(password).strip().encode()).hexdigest()

def generate_user_id(prefix):
    return f"{prefix}-{secrets.token_hex(4).upper()}"

def generate_random_password():
    return secrets.token_hex(4)

# --------------------------
# Flask app
# --------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# Secret key configuration
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    if ON_FLY:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Set it with: fly secrets set SECRET_KEY=$(python -c \"import secrets; print(secrets.token_hex(32))\")"
        )
    _secret = 'dev-secret-key-change-in-production'
app.config['SECRET_KEY'] = _secret

# Session configuration
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = ON_FLY
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'school_session'
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Fix: Correct session storage path for Fly.io
if ON_FLY:
    # Check if we have a volume mounted
    VOLUME_PATH = '/data'
    if os.path.exists(VOLUME_PATH):
        # Use the volume for sessions
        app.config['SESSION_FILE_DIR'] = os.path.join(VOLUME_PATH, 'flask_session')
        print(f"✅ Using volume for sessions at {app.config['SESSION_FILE_DIR']}")
    else:
        # Fallback to /tmp but warn
        app.config['SESSION_FILE_DIR'] = '/tmp/flask_session'
        print("⚠️ WARNING: Using /tmp for sessions - sessions will NOT persist between restarts!")
else:
    app.config['SESSION_FILE_DIR'] = os.path.join(BASE_DIR, 'flask_session')

# Create session directory with proper permissions
try:
    os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)
    os.chmod(app.config['SESSION_FILE_DIR'], 0o777)
    print(f"📁 Session directory created/verified at {app.config['SESSION_FILE_DIR']}")
except Exception as e:
    print(f"⚠️ Could not create session directory: {e}")

# Initialize session extension
Session(app)

# CORS configuration
ALLOWED_ORIGINS = [
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]

if ON_FLY:
    fly_app_url = f"https://{os.environ.get('FLY_APP_NAME')}.fly.dev"
    ALLOWED_ORIGINS.append(fly_app_url)
    ALLOWED_ORIGINS.append(fly_app_url.replace('https://', 'http://'))

# Configure CORS
CORS(app,
     supports_credentials=True,
     origins=ALLOWED_ORIGINS,
     allow_headers=['Content-Type', 'Authorization', 'X-Requested-With'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
     expose_headers=['Content-Type', 'Authorization'])

# --------------------------
# Database initialization with worker safety
# --------------------------
def init_db():
    """Initialize database with tables and default data - thread-safe for multiple workers"""
    global db_initialized
    
    # Use lock to prevent multiple workers from initializing simultaneously
    with db_init_lock:
        # Check if already initialized by another worker
        if db_initialized:
            print("ℹ️ Database already initialized by another worker")
            return
            
        conn = None
        try:
            db_exists = os.path.exists(DB_PATH)
            if db_exists:
                print(f"📁 Using existing database at {DB_PATH}")
            else:
                print(f"📁 Creating new database at {DB_PATH}")

            conn = get_db()
            c = conn.cursor()

            # Create tables if they don't exist
            c.execute('''CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT,
                role TEXT DEFAULT 'admin',
                created_at TEXT
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS teachers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT,
                email TEXT,
                subject TEXT,
                phone TEXT,
                role TEXT DEFAULT 'teacher',
                created_at TEXT
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT,
                student_id TEXT UNIQUE,
                level TEXT,
                arm TEXT,
                phone TEXT,
                role TEXT DEFAULT 'student',
                created_at TEXT
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT,
                term TEXT,
                session TEXT,
                subject TEXT,
                ca1 INTEGER DEFAULT 0,
                ca2 INTEGER DEFAULT 0,
                ca3 INTEGER DEFAULT 0,
                exam INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                grade TEXT,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(student_id, term, session, subject)
            )''')
            
            conn.commit()
            
            # Only try to create default admin if table is empty
            c.execute("SELECT COUNT(*) as count FROM admins")
            if c.fetchone()['count'] == 0:
                admin_password = hash_password('admin123')
                c.execute('''
                    INSERT OR IGNORE INTO admins (username, password, name, role, created_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', ('admin', admin_password, 'System Administrator', 'admin', datetime.now().isoformat()))
                conn.commit()
                print("✅ Default admin created")
            else:
                print("ℹ️ Admin account already exists, skipping creation")

            # Log counts of teachers and students
            c.execute("SELECT COUNT(*) as count FROM teachers")
            teacher_count = c.fetchone()['count']
            print(f"👥 Teachers in database: {teacher_count}")
            
            c.execute("SELECT COUNT(*) as count FROM students")
            student_count = c.fetchone()['count']
            print(f"👥 Students in database: {student_count}")

            print("✅ Database initialized successfully")
            
            # Set proper permissions on database file
            if os.path.exists(DB_PATH):
                try:
                    os.chmod(DB_PATH, 0o666)
                except:
                    pass

            db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
            print(f"📊 Database size: {db_size} bytes")
            
            # Mark as initialized
            db_initialized = True

        except Exception as e:
            print(f"❌ Database initialization error: {e}")
            traceback.print_exc()
        finally:
            if conn:
                conn.close()

# --------------------------
# Session and CORS handling
# --------------------------
@app.before_request
def before_request():
    """Setup before each request"""
    if not request.path.startswith('/static/'):
        print(f"📥 {request.method} {request.path}")
        print(f"   Headers: Origin={request.headers.get('Origin')}, Cookie={request.headers.get('Cookie')}")
    
    # Set session to be permanent
    session.permanent = True
    
    # Log session contents for debugging
    if 'user_id' in session:
        print(f"🔍 Session before request: {dict(session)}")

@app.after_request
def after_request(response):
    """Add headers and log after request"""
    origin = request.headers.get('Origin')
    if origin and origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Vary'] = 'Origin'

    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'

    # Log session after request
    if 'user_id' in session:
        print(f"📤 Response {response.status_code}")
        print(f"   Session after request: {dict(session)}")
        print(f"   Cookies set: {response.headers.get('Set-Cookie')}")

    return response

# --------------------------
# Authentication decorator
# --------------------------
def login_required(role=None):
    def wrapper(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if 'user_id' not in session:
                print(f"❌ Access denied - No user_id in session")
                if request.path.startswith('/api'):
                    return jsonify({'error': 'Not logged in'}), 401
                return redirect(url_for('index'))
            if role and session.get('role') != role:
                print(f"❌ Access denied - Wrong role. Expected {role}, got {session.get('role')}")
                if request.path.startswith('/api'):
                    return jsonify({'error': 'Unauthorized'}), 403
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return wrapped
    return wrapper

# --------------------------
# Routes - pages
# --------------------------
@app.route('/')
def index():
    return render_template("index.html")

@app.route('/login')
def login_page():
    if 'user_id' in session and 'role' in session:
        dest = {
            'admin': 'admin_dashboard',
            'teacher': 'teacher_dashboard',
            'student': 'student_dashboard',
        }.get(session.get('role'))
        if dest:
            return redirect(url_for(dest))
    return redirect(url_for('index'))

@app.route('/admin_dashboard')
@login_required(role='admin')
def admin_dashboard():
    return render_template("admin_dashboard.html", name=session.get('name'))

@app.route('/teacher_dashboard')
@login_required(role='teacher')
def teacher_dashboard():
    return render_template("teachers_dashboard.html", name=session.get('name'))

@app.route('/student_dashboard')
@login_required(role='student')
def student_dashboard():
    return render_template("student_dashboard.html", name=session.get('name'))

# --------------------------
# API - Current user
# --------------------------
@app.route('/api/current-user', methods=['GET', 'OPTIONS'])
def get_current_user():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    if 'user_id' in session and 'role' in session:
        user_info = {
            'user': {
                'username': session.get('username', ''),
                'name': session.get('name', ''),
                'role': session.get('role'),
                'user_id': session.get('user_id')
            }
        }

        if session.get('role') == 'student' and session.get('student_id'):
            user_info['user']['student_id'] = session.get('student_id')
            user_info['user']['level'] = session.get('level', '')
            user_info['user']['arm'] = session.get('arm', '')

        return jsonify(user_info)

    return jsonify({'error': 'Not logged in'}), 401

# --------------------------
# API - Check session
# --------------------------
@app.route('/api/check-session', methods=['GET', 'OPTIONS'])
def check_session():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    if 'user_id' in session:
        return jsonify({
            'logged_in': True,
            'role': session.get('role'),
            'name': session.get('name'),
            'user_id': session.get('user_id')
        })
    return jsonify({'logged_in': False})

# --------------------------
# API - Login
# --------------------------
@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'POST,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': 'Invalid JSON'}), 400

        role = data.get('role')
        username = data.get('username')
        password = data.get('password')

        if not role or not username or not password:
            return jsonify({'error': 'Missing credentials'}), 400

        username = str(username).strip()
        password = str(password).strip()
        
        hashed_password = hash_password(password)

        def _find_user(conn):
            c = conn.cursor()

            if role == 'admin':
                c.execute("SELECT * FROM admins WHERE username=? AND password=?",
                          (username, hashed_password))
            elif role == 'teacher':
                c.execute("SELECT * FROM teachers WHERE username=? AND password=?",
                          (username, hashed_password))
            elif role == 'student':
                c.execute(
                    "SELECT * FROM students WHERE (username=? OR student_id=?) AND password=?",
                    (username, username, hashed_password)
                )
            else:
                return None

            row = c.fetchone()
            if row:
                return {k: row[k] for k in row.keys()}
            return None

        user = safe_db_operation(_find_user)

        if not user:
            print(f"❌ Login failed for {role}: {username}")
            return jsonify({'error': 'Invalid credentials'}), 401

        # Clear old session and set new one
        session.clear()
        session.permanent = True
        session['role'] = role
        session['name'] = user.get('name')
        session['username'] = user.get('username')

        if role == 'student':
            session['user_id'] = user.get('student_id') or user.get('username')
            session['student_id'] = user.get('student_id')
            session['level'] = user.get('level', '')
            session['arm'] = user.get('arm', '')
        else:
            session['user_id'] = user.get('username')

        session.modified = True

        print(f"✅ LOGIN SUCCESS - {role}: {username}")
        print(f"✅ SESSION DATA: {dict(session)}")

        redirect_url = {
            "admin": "/admin_dashboard",
            "teacher": "/teacher_dashboard",
            "student": "/student_dashboard"
        }.get(role, "/")

        if "password" in user:
            del user["password"]

        response = jsonify({
            "success": True,
            "redirect": redirect_url,
            "user": user
        })
        
        # Ensure cookie is set
        response.set_cookie(
            'school_session',
            session.get('_id', ''),
            httponly=True,
            secure=ON_FLY,
            samesite='Lax',
            max_age=86400  # 24 hours
        )
        
        return response

    except Exception as e:
        print("LOGIN ERROR:", e)
        traceback.print_exc()
        return jsonify({'error': 'Server error'}), 500

# --------------------------
# API - Logout
# --------------------------
@app.route('/api/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'POST,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    session.clear()
    response = jsonify({'success': True})
    response.set_cookie('school_session', '', expires=0)
    return response

# --------------------------
# API - Get all students (for teachers)
# --------------------------
@app.route('/api/students', methods=['GET', 'OPTIONS'])
@login_required(role='teacher')
def get_students():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    try:
        def _get_students(conn):
            c = conn.cursor()
            c.execute("SELECT student_id, name, level, arm, username FROM students ORDER BY name")
            rows = c.fetchall()
            return [{k: row[k] for k in row.keys()} for row in rows]

        students = safe_db_operation(_get_students)
        return jsonify(students)
    except Exception as e:
        print(f"Error fetching students: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to load students'}), 500

# --------------------------
# API - Teacher results summary
# --------------------------
@app.route('/api/teacher-results', methods=['GET', 'OPTIONS'])
@login_required(role='teacher')
def teacher_results():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    try:
        term_filter = request.args.get('term')
        
        def _get_teacher_results(conn):
            c = conn.cursor()
            
            query = """
                SELECT s.student_id, s.name, s.level, s.arm, 
                       sc.term, sc.session, sc.subject, sc.ca1, sc.ca2, sc.ca3, sc.exam, sc.total, sc.grade
                FROM scores sc
                JOIN students s ON sc.student_id = s.student_id
            """
            params = []
            
            if term_filter:
                query += " WHERE sc.term = ?"
                params.append(term_filter)
                
            query += " ORDER BY s.name, sc.term, sc.session"
            
            c.execute(query, params)
            rows = c.fetchall()
            
            results = {}
            for row in rows:
                key = f"{row['student_id']}_{row['term']}_{row['session']}"
                
                if key not in results:
                    results[key] = {
                        'student_id': row['student_id'],
                        'name': row['name'],
                        'level': row['level'],
                        'arm': row['arm'],
                        'term': row['term'],
                        'session': row['session'],
                        'subjects': []
                    }
                
                results[key]['subjects'].append({
                    'subject': row['subject'],
                    'ca1': row['ca1'],
                    'ca2': row['ca2'],
                    'ca3': row['ca3'],
                    'exam': row['exam'],
                    'total': row['total'],
                    'grade': row['grade']
                })
            
            result_list = []
            for result in results.values():
                subjects = result['subjects']
                if subjects:
                    total_score = sum(s['total'] for s in subjects)
                    avg = round(total_score / len(subjects), 2)
                    result['average'] = avg
                result_list.append(result)
            
            return result_list

        results = safe_db_operation(_get_teacher_results)
        return jsonify(results)
        
    except Exception as e:
        print(f"Error fetching teacher results: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to load results'}), 500

# --------------------------
# API - Scores CRUD
# --------------------------
@app.route('/api/scores', methods=['GET', 'OPTIONS'])
@login_required()
def get_scores():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'GET,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    try:
        student_id = request.args.get('student_id')
        term = request.args.get('term')
        session_val = request.args.get('session')
        
        if not student_id or not term or not session_val:
            return jsonify({'error': 'Missing parameters'}), 400

        if session.get('role') == 'student' and session.get('student_id') != student_id:
            return jsonify({'error': 'Unauthorized'}), 403

        def _get_scores(conn):
            c = conn.cursor()
            c.execute("""
                SELECT subject, ca1, ca2, ca3, exam, total, grade
                FROM scores
                WHERE student_id = ? AND term = ? AND session = ?
                ORDER BY subject
            """, (student_id, term, session_val))
            
            rows = c.fetchall()
            subjects = [{k: row[k] for k in row.keys()} for row in rows]
            
            return [{
                'student_id': student_id,
                'term': term,
                'session': session_val,
                'subjects': subjects
            }]

        scores = safe_db_operation(_get_scores)
        return jsonify(scores)

    except Exception as e:
        print(f"Error fetching scores: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to load scores'}), 500

@app.route('/api/scores', methods=['POST', 'OPTIONS'])
@login_required(role='teacher')
def create_score():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'POST,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    try:
        data = request.get_json()
        required = ['student_id', 'term', 'session', 'subject', 'ca1', 'ca2', 'ca3', 'exam', 'total', 'grade']
        
        if not all(k in data for k in required):
            return jsonify({'error': 'Missing required fields'}), 400

        def _create_score(conn):
            c = conn.cursor()
            now = datetime.now().isoformat()
            
            c.execute("""
                INSERT OR REPLACE INTO scores 
                (student_id, term, session, subject, ca1, ca2, ca3, exam, total, grade, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['student_id'], data['term'], data['session'], data['subject'],
                data['ca1'], data['ca2'], data['ca3'], data['exam'],
                data['total'], data['grade'], now, now
            ))
            
            conn.commit()
            return {'success': True}

        result = safe_db_operation(_create_score)
        return jsonify(result)

    except Exception as e:
        print(f"Error creating score: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to save score'}), 500

@app.route('/api/scores/delete', methods=['POST', 'OPTIONS'])
@login_required(role='teacher')
def delete_score():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'POST,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    try:
        data = request.get_json()
        required = ['student_id', 'subject', 'term', 'session']
        
        if not all(k in data for k in required):
            return jsonify({'error': 'Missing required fields'}), 400

        def _delete_score(conn):
            c = conn.cursor()
            c.execute("""
                DELETE FROM scores
                WHERE student_id = ? AND subject = ? AND term = ? AND session = ?
            """, (data['student_id'], data['subject'], data['term'], data['session']))
            
            conn.commit()
            return {'success': True, 'deleted': c.rowcount > 0}

        result = safe_db_operation(_delete_score)
        return jsonify(result)

    except Exception as e:
        print(f"Error deleting score: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to delete score'}), 500

# --------------------------
# API - Change password
# --------------------------
@app.route('/api/change-password', methods=['POST', 'OPTIONS'])
@login_required()
def change_password():
    if request.method == 'OPTIONS':
        response = make_response()
        origin = request.headers.get('Origin', '*')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
        response.headers['Access-Control-Allow-Methods'] = 'POST,OPTIONS'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    try:
        data = request.get_json()
        old_password = data.get('old_password')
        new_password = data.get('new_password')
        
        if not old_password or not new_password:
            return jsonify({'error': 'Missing passwords'}), 400
            
        if len(new_password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400

        role = session.get('role')
        username = session.get('username')
        
        if not role or not username:
            return jsonify({'error': 'Not logged in'}), 401

        def _change_password(conn):
            c = conn.cursor()
            
            table_map = {
                'admin': 'admins',
                'teacher': 'teachers',
                'student': 'students'
            }
            
            table = table_map.get(role)
            if not table:
                return {'error': 'Invalid role'}, 400
            
            old_hashed = hash_password(old_password)
            
            if role == 'student':
                c.execute(f"SELECT * FROM {table} WHERE student_id = ? AND password = ?", 
                         (username, old_hashed))
            else:
                c.execute(f"SELECT * FROM {table} WHERE username = ? AND password = ?", 
                         (username, old_hashed))
            
            user = c.fetchone()
            if not user:
                print(f"Password mismatch for {role}: {username}")
                return {'error': 'Current password is incorrect'}, 401
            
            new_hashed = hash_password(new_password)
            
            if role == 'student':
                c.execute(f"UPDATE {table} SET password = ? WHERE student_id = ?", 
                         (new_hashed, username))
            else:
                c.execute(f"UPDATE {table} SET password = ? WHERE username = ?", 
                         (new_hashed, username))
            
            conn.commit()
            print(f"✅ Password changed successfully for {role}: {username}")
            return {'success': True}

        result = safe_db_operation(_change_password)
        
        if isinstance(result, tuple) and len(result) == 2 and 'error' in result[0]:
            return jsonify(result[0]), result[1]
            
        return jsonify(result)

    except Exception as e:
        print(f"Error changing password: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to change password'}), 500

# --------------------------
# Health check
# --------------------------
@app.route('/health', methods=['GET'])
def health_check():
    try:
        def _check_db(conn):
            conn.execute("SELECT 1").fetchone()
            return True

        safe_db_operation(_check_db)
        
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        return jsonify({
            'status': 'healthy',
            'database': {
                'path': DB_PATH,
                'exists': os.path.exists(DB_PATH),
                'size_bytes': db_size
            },
            'session_dir': app.config['SESSION_FILE_DIR']
        }), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

# --------------------------
# Error handlers
# --------------------------
@app.errorhandler(404)
def not_found_error(error):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'API endpoint not found'}), 404
    return render_template('index.html'), 404

@app.errorhandler(500)
def internal_error(error):
    print(f"Internal server error: {error}")
    traceback.print_exc()
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal server error'}), 500
    return render_template('index.html'), 500

# --------------------------
# Initialize database (with worker safety)
# --------------------------
print("🚀 Davis Academy Portal Starting...")
print(f"Python version: {sys.version}")
print(f"Running on Fly.io: {ON_FLY}")
print(f"Session directory: {app.config['SESSION_FILE_DIR']}")
print(f"Database path: {DB_PATH}")

if ON_FLY:
    VOLUME_PATH = '/data'
    if os.path.exists(VOLUME_PATH):
        print(f"✅ Using persistent volume at {VOLUME_PATH}")
        try:
            test_file = os.path.join(VOLUME_PATH, 'test.txt')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            print("✅ Volume is writable")
        except Exception as e:
            print(f"⚠️ Volume permissions issue: {e}")
    else:
        print("⚠️ WARNING: No volume mounted! Data will NOT persist!")

# Initialize the database
init_db()

# --------------------------
# Run server (for local dev only)
# --------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)