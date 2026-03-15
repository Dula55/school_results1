#!/usr/bin/env python3
import os
import sqlite3
import hashlib
import secrets
import traceback
import sys
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, request, jsonify, session, render_template,
    redirect, url_for, make_response
)
from flask_cors import CORS
from flask_session import Session

# --------------------------
# Basic config / DB helpers
# --------------------------
sqlite3.register_converter("timestamp", lambda b: b.decode('utf-8') if b else None)

# Get the base directory for the application
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Use /tmp for database in production (Render uses ephemeral filesystem)
# BUT we need persistent storage - so we'll use a mounted volume or external DB
if os.environ.get('RENDER'):
    # For Render, we need to use a persistent disk or external database
    # Option 1: Use Render Disk (recommended)
    # Create a disk in Render dashboard and mount it at /opt/render/project/data
    PERSISTENT_DIR = '/opt/render/project/data'
    if os.path.exists(PERSISTENT_DIR):
        DB_PATH = os.path.join(PERSISTENT_DIR, 'davis_academy.db')
    else:
        # Fallback to /tmp but warn that data won't persist
        DB_PATH = '/tmp/davis_academy.db'
        print("⚠️ WARNING: Using /tmp for database - data will NOT persist between restarts!")
        print("⚠️ Create a persistent disk in Render dashboard and mount it at /opt/render/project/data")
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
    # Trim whitespace from password before hashing
    return hashlib.sha256(str(password).strip().encode()).hexdigest()

def generate_user_id(prefix):
    return f"{prefix}-{secrets.token_hex(4).upper()}"

def generate_random_password():
    return secrets.token_hex(4)

# --------------------------
# Flask app
# --------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# Critical: Session configuration MUST be set before initializing Session

# On Render, SECRET_KEY MUST be set as an environment variable.
# Without a stable key, every new deploy gets a different key → all existing
# sessions are immediately invalidated and every user is logged out.
# Set it in: Render dashboard → your service → Environment → Add env var.
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    if os.environ.get('RENDER'):
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Add it in Render dashboard → Environment → Add Environment Variable. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    _secret = 'dev-secret-key-change-in-production'
app.config['SECRET_KEY'] = _secret

app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
# Render serves over HTTPS — secure cookies are required there.
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RENDER'))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'school_session'
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Session storage - use persistent disk if available
if os.environ.get('RENDER'):
    PERSISTENT_DIR = '/opt/render/project/data'
    if os.path.exists(PERSISTENT_DIR):
        app.config['SESSION_FILE_DIR'] = os.path.join(PERSISTENT_DIR, 'flask_session')
    else:
        app.config['SESSION_FILE_DIR'] = '/tmp/flask_session'
        print("⚠️ WARNING: Using /tmp for session data - sessions will NOT persist between restarts!")
else:
    app.config['SESSION_FILE_DIR'] = os.path.join(BASE_DIR, 'flask_session')
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['SESSION_COOKIE_DOMAIN'] = None

# Create session directory
os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)
# Ensure the directory is writable
os.chmod(app.config['SESSION_FILE_DIR'], 0o777)

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

if os.environ.get('RENDER'):
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        ALLOWED_ORIGINS.append(render_url)
        ALLOWED_ORIGINS.append(render_url.replace('https://', 'http://'))

# Configure CORS
CORS(app,
     supports_credentials=True,
     origins=ALLOWED_ORIGINS,
     allow_headers=['Content-Type', 'Authorization', 'X-Requested-With'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
     expose_headers=['Content-Type', 'Authorization'])

# --------------------------
# Database initialization
# --------------------------
def init_db():
    """Initialize database with tables and default data"""
    conn = None
    try:
        db_exists = os.path.exists(DB_PATH)
        if db_exists:
            print(f"📁 Using existing database at {DB_PATH}")
        else:
            print(f"📁 Creating new database at {DB_PATH}")

        conn = get_db()
        c = conn.cursor()

        # Create tables
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

        now = datetime.now().isoformat()

        # Check and create default admin if none exists
        c.execute("SELECT COUNT(*) as count FROM admins")
        if c.fetchone()['count'] == 0:
            admin_password = hash_password('admin123')
            c.execute('''
                INSERT INTO admins (username, password, name, role, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', ('admin', admin_password, 'System Administrator', 'admin', now))
            print("✅ Default admin created")
        else:
            print("ℹ️ Admin account already exists, skipping creation")

        # DO NOT create default teacher accounts automatically
        c.execute("SELECT COUNT(*) as count FROM teachers")
        teacher_count = c.fetchone()['count']
        if teacher_count == 0:
            print("ℹ️ No teachers found. Teachers can be added by admin through the dashboard.")
        else:
            print(f"ℹ️ {teacher_count} teacher(s) already exist")

        # DO NOT create default student accounts automatically
        c.execute("SELECT COUNT(*) as count FROM students")
        student_count = c.fetchone()['count']
        if student_count == 0:
            print("ℹ️ No students found. Students can be added by admin through the dashboard.")
        else:
            print(f"ℹ️ {student_count} student(s) already exist")

        conn.commit()
        print("✅ Database initialized successfully")

        # Set proper permissions on database file
        if os.path.exists(DB_PATH):
            os.chmod(DB_PATH, 0o666)

        # Log database location and size
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        print(f"📊 Database size: {db_size} bytes")

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
    # Log the request
    print(f"📥 {request.method} {request.path}")
    print(f"   Headers: Origin={request.headers.get('Origin')}, Cookie={request.headers.get('Cookie')}")

    # Set session to be permanent
    session.permanent = True

    # Log session for debugging
    if request.path.startswith('/api/'):
        print(f"🔍 Session before {request.path}: {dict(session)}")

@app.after_request
def after_request(response):
    """Add headers and log after request"""
    # Add CORS headers for all responses
    origin = request.headers.get('Origin')
    if origin:
        # Check if origin is allowed
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Vary'] = 'Origin'

    # Add headers for all responses
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,X-Requested-With'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'

    # Log response
    if request.path.startswith('/api/'):
        print(f"📤 Response {response.status_code}")
        print(f"   Session after {request.path}: {dict(session)}")
        print(f"   Cookies set: {response.headers.get('Set-Cookie', 'None')}")

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
    """Welcome / login page - accessible to everyone"""
    return render_template("index.html")

# FIX: /login was missing entirely, causing a 404 on every unauthenticated
# redirect. The 404 handler returned index.html, which re-triggered
# check-session → dashboard → /api/current-user (Cookie=None, 401) →
# redirect /login → 404 → index.html → … in an infinite loop.
# Now /login is a valid route that redirects cleanly to /.
@app.route('/login')
def login_page():
    """Login page — redirect to index which contains the login UI."""
    # If the user already has a valid session, send them straight to their dashboard.
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
    """Get current user info from session.

    IMPORTANT: The JavaScript that calls this endpoint must pass
    credentials so the session cookie is forwarded:

        fetch('/api/current-user', { credentials: 'include' })

    Without credentials:'include', Cookie will be None even for a
    logged-in user, and this endpoint will always return 401.
    """
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    print(f"👤 /api/current-user - Session: {dict(session)}")

    if 'user_id' in session and 'role' in session:
        user_info = {
            'user': {
                'username': session.get('username', ''),
                'name': session.get('name', ''),
                'role': session.get('role'),
                'user_id': session.get('user_id')
            }
        }

        # Add role-specific fields
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
    """Check if user is logged in"""
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    print(f"🔑 /api/check-session - Session: {dict(session)}")

    if 'user_id' in session:
        return jsonify({
            'logged_in': True,
            'role': session.get('role'),
            'name': session.get('name'),
            'user_id': session.get('user_id')
        })
    return jsonify({'logged_in': False})

# --------------------------
# API - Login (FIXED: Better password handling)
# --------------------------
@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():

    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
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

        # Trim whitespace from username and password
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
                # Try both username and student_id
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
            # Log failed attempt for debugging
            print(f"❌ Login failed for {role}: {username}")
            return jsonify({'error': 'Invalid credentials'}), 401

        # Reset session
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

        return jsonify({
            "success": True,
            "redirect": redirect_url,
            "user": user
        })

    except Exception as e:
        print("LOGIN ERROR:", e)
        traceback.print_exc()
        return jsonify({'error': 'Server error'}), 500

# --------------------------
# API - Logout
# --------------------------
@app.route('/api/logout', methods=['POST', 'OPTIONS'])
def logout():
    """Logout user"""
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    session.clear()
    response = jsonify({'success': True})
    response.set_cookie('school_session', '', expires=0)
    return response

# --------------------------
# API - Students
# --------------------------
@app.route('/api/students', methods=['GET', 'OPTIONS'])
def get_students():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        def _get_students(conn):
            c = conn.cursor()
            c.execute("SELECT username, name, student_id, level, arm FROM students ORDER BY name")
            return [{k: row[k] for k in row.keys()} for row in c.fetchall()]

        students = safe_db_operation(_get_students)
        return jsonify(students)
    except Exception as e:
        print(f"Error fetching students: {e}")
        return jsonify({'error': 'Failed to fetch students'}), 500

# --------------------------
# API - Teacher Results
# --------------------------
@app.route('/api/teacher-results', methods=['GET', 'OPTIONS'])
def get_teacher_results():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        term = request.args.get('term')

        def _get_results(conn):
            c = conn.cursor()

            query = '''
                SELECT s.student_id, s.term, s.session, s.subject, s.ca1, s.ca2, s.ca3, s.exam, s.total, s.grade,
                       stu.name, stu.level, stu.arm
                FROM scores s
                JOIN students stu ON s.student_id = stu.student_id
            '''
            params = []

            if term:
                query += ' WHERE s.term = ?'
                params.append(term)

            query += ' ORDER BY stu.name, s.session DESC, s.term, s.subject'

            c.execute(query, params)
            return c.fetchall()

        rows = safe_db_operation(_get_results)

        # Group by student, term, session
        results = {}
        for row in rows:
            key = f"{row['student_id']}|{row['term']}|{row['session']}"
            if key not in results:
                results[key] = {
                    'student_id': row['student_id'],
                    'name': row['name'],
                    'level': row['level'],
                    'arm': row['arm'],
                    'term': row['term'],
                    'session': row['session'],
                    'subjects': [],
                    'total_score': 0,
                    'subject_count': 0
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
            results[key]['total_score'] += row['total']
            results[key]['subject_count'] += 1

        # Calculate averages
        result_list = []
        for key, data in results.items():
            if data['subject_count'] > 0:
                avg = data['total_score'] / data['subject_count']
                data['average'] = f"{avg:.1f}%"
            else:
                data['average'] = '-'
            result_list.append(data)

        return jsonify(result_list)

    except Exception as e:
        print(f"Error fetching teacher results: {e}")
        return jsonify({'error': 'Failed to fetch results'}), 500

# --------------------------
# API - Scores
# --------------------------
@app.route('/api/scores', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
def api_scores():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        if request.method == 'GET':
            student_id = request.args.get('student_id')
            term = request.args.get('term')
            session_param = request.args.get('session')

            def _get_scores(conn):
                c = conn.cursor()
                if student_id and term and session_param:
                    c.execute('''
                        SELECT * FROM scores
                        WHERE student_id = ? AND term = ? AND session = ?
                        ORDER BY subject
                    ''', (student_id, term, session_param))
                    rows = c.fetchall()
                    if rows:
                        subjects = [{k: row[k] for k in row.keys()} for row in rows]
                        return [{
                            'student_id': student_id,
                            'term': term,
                            'session': session_param,
                            'subjects': subjects
                        }]
                    return []
                else:
                    c.execute('''
                        SELECT DISTINCT student_id, term, session
                        FROM scores
                        ORDER BY student_id, session DESC, term
                    ''')
                    rows = c.fetchall()
                    results = []
                    for row in rows:
                        c.execute('''
                            SELECT * FROM scores
                            WHERE student_id = ? AND term = ? AND session = ?
                            ORDER BY subject
                        ''', (row['student_id'], row['term'], row['session']))
                        subject_rows = c.fetchall()
                        subjects = [{k: r[k] for k in r.keys()} for r in subject_rows]
                        results.append({
                            'student_id': row['student_id'],
                            'term': row['term'],
                            'session': row['session'],
                            'subjects': subjects
                        })
                    return results

            result = safe_db_operation(_get_scores)
            return jsonify(result)

        if request.method == 'POST':
            data = request.get_json() or {}
            required = ['student_id', 'term', 'session', 'subject', 'ca1', 'ca2', 'ca3', 'exam']
            if not all(k in data for k in required):
                return jsonify({'error': 'Missing required fields'}), 400

            # Calculate total and grade
            ca1 = data['ca1']
            ca2 = data['ca2']
            ca3 = data['ca3']
            exam = data['exam']
            total = ca1 + ca2 + ca3 + exam

            # Calculate grade
            if total >= 70:
                grade = 'A'
            elif total >= 60:
                grade = 'B'
            elif total >= 50:
                grade = 'C'
            elif total >= 45:
                grade = 'D'
            elif total >= 40:
                grade = 'E'
            else:
                grade = 'F'

            def _add_score(conn):
                c = conn.cursor()
                # Check if exists
                c.execute('''
                    SELECT id FROM scores
                    WHERE student_id = ? AND term = ? AND session = ? AND subject = ?
                ''', (data['student_id'], data['term'], data['session'], data['subject']))
                if c.fetchone():
                    return False

                now = datetime.now().isoformat()
                c.execute('''
                    INSERT INTO scores
                    (student_id, term, session, subject, ca1, ca2, ca3, exam, total, grade, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (data['student_id'], data['term'], data['session'], data['subject'],
                      ca1, ca2, ca3, exam, total, grade, now, now))
                conn.commit()
                return True

            success = safe_db_operation(_add_score)
            if success:
                return jsonify({'success': True, 'message': 'Score added successfully'})
            else:
                return jsonify({'error': 'Score already exists for this subject'}), 400

        if request.method == 'DELETE':
            student_id = request.args.get('student_id')
            term = request.args.get('term')
            session_param = request.args.get('session')
            subject = request.args.get('subject')
            if not all([student_id, term, session_param, subject]):
                return jsonify({'error': 'Missing parameters'}), 400

            def _delete_score(conn):
                c = conn.cursor()
                c.execute('''
                    DELETE FROM scores
                    WHERE student_id = ? AND term = ? AND session = ? AND subject = ?
                ''', (student_id, term, session_param, subject))
                conn.commit()
                return c.rowcount > 0

            deleted = safe_db_operation(_delete_score)
            if deleted:
                return jsonify({'success': True, 'message': 'Score deleted successfully'})
            else:
                return jsonify({'error': 'Score not found'}), 404

    except Exception as e:
        print(f"Error in /api/scores: {e}")
        return jsonify({'error': 'Failed to process scores'}), 500

# --------------------------
# API - Teachers
# --------------------------
@app.route('/api/teachers', methods=['GET', 'POST', 'OPTIONS'])
def teachers_api():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    if request.method == 'GET':
        try:
            def _get_teachers(conn):
                c = conn.cursor()
                c.execute("SELECT username, name, email, subject, phone, role FROM teachers")
                return [{k: r[k] for k in r.keys()} for r in c.fetchall()]

            teachers = safe_db_operation(_get_teachers)
            return jsonify(teachers)
        except Exception as e:
            print(f"Error fetching teachers: {e}")
            return jsonify({'error': str(e)}), 500

    if request.method == 'POST':
        try:
            data = request.get_json() or {}
            name = data.get('name')
            email = data.get('email')
            subject = data.get('subject')
            phone = data.get('phone')

            if not name or not email:
                return jsonify({'error': 'Name and email are required'}), 400

            username = email.split('@')[0]
            password = generate_random_password()
            hashed_password = hash_password(password)
            now = datetime.now().isoformat()

            def _add_teacher(conn):
                c = conn.cursor()
                try:
                    c.execute('''
                        INSERT INTO teachers (username, password, name, email, subject, phone, role, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (username, hashed_password, name, email, subject, phone, 'teacher', now))
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    return False

            success = safe_db_operation(_add_teacher)
            if success:
                return jsonify({'success': True, 'username': username, 'password': password, 'name': name})
            else:
                return jsonify({'error': 'Teacher with this email already exists'}), 400

        except Exception as e:
            print(f"Error adding teacher: {e}")
            return jsonify({'error': str(e)}), 500

@app.route('/api/teachers/<username>', methods=['DELETE', 'OPTIONS'])
def delete_teacher(username):
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'DELETE,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        def _delete_teacher(conn):
            c = conn.cursor()
            c.execute("DELETE FROM teachers WHERE username = ?", (username,))
            conn.commit()
            return c.rowcount > 0

        deleted = safe_db_operation(_delete_teacher)
        if deleted:
            return jsonify({'success': True})
        return jsonify({'error': 'Teacher not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --------------------------
# API - Students manage
# --------------------------
@app.route('/api/students/manage', methods=['GET', 'POST', 'OPTIONS'])
def students_manage_api():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    if request.method == 'GET':
        try:
            def _get_students(conn):
                c = conn.cursor()
                c.execute("SELECT username, name, student_id, level, arm, phone, role FROM students")
                return [{k: r[k] for k in r.keys()} for r in c.fetchall()]

            students = safe_db_operation(_get_students)
            return jsonify(students)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    if request.method == 'POST':
        try:
            data = request.get_json() or {}
            name = data.get('name')
            student_id = data.get('student_id')
            level = data.get('level')
            arm = data.get('arm')
            phone = data.get('phone')
            username = data.get('username', student_id)

            if not name or not student_id or not level:
                return jsonify({'error': 'Name, Student ID, and Class are required'}), 400

            password = generate_random_password()
            hashed_password = hash_password(password)
            now = datetime.now().isoformat()

            def _add_student(conn):
                c = conn.cursor()
                try:
                    c.execute('''
                        INSERT INTO students (username, password, name, student_id, level, arm, phone, role, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (username, hashed_password, name, student_id, level, arm, phone, 'student', now))
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    return False

            success = safe_db_operation(_add_student)
            if success:
                return jsonify({'success': True, 'username': username, 'student_id': student_id, 'password': password, 'name': name})
            else:
                return jsonify({'error': 'Student with this ID or username already exists'}), 400

        except Exception as e:
            print(f"Error adding student: {e}")
            return jsonify({'error': str(e)}), 500

@app.route('/api/students/manage/<student_id>', methods=['DELETE', 'OPTIONS'])
def delete_student_manage(student_id):
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'DELETE,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        def _delete_student(conn):
            c = conn.cursor()
            c.execute("DELETE FROM scores WHERE student_id = ?", (student_id,))
            c.execute("DELETE FROM students WHERE student_id = ?", (student_id,))
            conn.commit()
            return c.rowcount > 0

        deleted = safe_db_operation(_delete_student)
        if deleted:
            return jsonify({'success': True})
        return jsonify({'error': 'Student not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --------------------------
# API - Scores manage
# --------------------------
@app.route('/api/scores/manage', methods=['GET', 'POST', 'OPTIONS'])
def scores_manage_api():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    if request.method == 'GET':
        try:
            student_id = request.args.get('student_id')

            def _get_scores(conn):
                c = conn.cursor()
                if student_id:
                    c.execute('''
                        SELECT * FROM scores WHERE student_id = ?
                        ORDER BY session DESC, term, subject
                    ''', (student_id,))
                else:
                    c.execute('''
                        SELECT * FROM scores ORDER BY student_id, session DESC, term, subject
                    ''')
                return [{k: r[k] for k in r.keys()} for r in c.fetchall()]

            scores = safe_db_operation(_get_scores)
            return jsonify(scores)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    if request.method == 'POST':
        try:
            data = request.get_json() or {}
            student_id = data.get('student_id')
            term = data.get('term')
            session_val = data.get('session')
            subject = data.get('subject')
            ca1 = data.get('ca1', 0)
            ca2 = data.get('ca2', 0)
            ca3 = data.get('ca3', 0)
            exam = data.get('exam', 0)
            total = data.get('total', ca1 + ca2 + ca3 + exam)
            grade = data.get('grade')

            if not all([student_id, term, session_val, subject]):
                return jsonify({'error': 'Missing required fields'}), 400

            now = datetime.now().isoformat()

            def _save_score(conn):
                c = conn.cursor()
                c.execute('''
                    INSERT OR REPLACE INTO scores
                    (student_id, term, session, subject, ca1, ca2, ca3, exam, total, grade, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (student_id, term, session_val, subject, ca1, ca2, ca3, exam, total, grade, now, now))
                conn.commit()
                return True

            safe_db_operation(_save_score)
            return jsonify({'success': True})
        except Exception as e:
            print(f"Error in scores_manage_api POST: {e}")
            return jsonify({'error': str(e)}), 500

# --------------------------
# API - Delete score
# --------------------------
@app.route('/api/scores/delete', methods=['POST', 'OPTIONS'])
def delete_score_manage():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        data = request.get_json() or {}
        student_id = data.get('student_id')
        subject = data.get('subject')
        term = data.get('term')
        session_val = data.get('session')
        if not all([student_id, subject, term, session_val]):
            return jsonify({'error': 'Missing required fields'}), 400

        def _delete_score(conn):
            c = conn.cursor()
            c.execute('''
                DELETE FROM scores WHERE student_id = ? AND subject = ? AND term = ? AND session = ?
            ''', (student_id, subject, term, session_val))
            conn.commit()
            return c.rowcount > 0

        deleted = safe_db_operation(_delete_score)
        if deleted:
            return jsonify({'success': True})
        return jsonify({'error': 'Score not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scores/delete-sheet', methods=['POST', 'OPTIONS'])
def delete_score_sheet():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        data = request.get_json() or {}
        student_id = data.get('student_id')
        term = data.get('term')
        session_val = data.get('session')
        if not all([student_id, term, session_val]):
            return jsonify({'error': 'Missing required fields'}), 400

        def _delete_sheet(conn):
            c = conn.cursor()
            c.execute('''
                DELETE FROM scores WHERE student_id = ? AND term = ? AND session = ?
            ''', (student_id, term, session_val))
            conn.commit()
            return c.rowcount > 0

        deleted = safe_db_operation(_delete_sheet)
        if deleted:
            return jsonify({'success': True})
        return jsonify({'error': 'Score sheet not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --------------------------
# API - Change password (FIXED: Better handling)
# --------------------------
@app.route('/api/change-password', methods=['POST', 'OPTIONS'])
def change_password():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        data = request.get_json() or {}
        old_password = data.get('old_password')
        new_password = data.get('new_password')

        if not old_password or not new_password:
            return jsonify({'error': 'All fields are required'}), 400
        
        # Trim whitespace
        old_password = str(old_password).strip()
        new_password = str(new_password).strip()
        
        if len(new_password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400

        role = session.get('role')
        user_id = session.get('user_id')

        if not role or not user_id:
            return jsonify({'error': 'Not logged in'}), 401

        hashed_old = hash_password(old_password)
        hashed_new = hash_password(new_password)

        def _change_password(conn):
            c = conn.cursor()
            rows_affected = 0
            
            if role == 'admin':
                c.execute('UPDATE admins SET password = ? WHERE username = ? AND password = ?',
                         (hashed_new, user_id, hashed_old))
                rows_affected = c.rowcount
            elif role == 'teacher':
                c.execute('UPDATE teachers SET password = ? WHERE username = ? AND password = ?',
                         (hashed_new, user_id, hashed_old))
                rows_affected = c.rowcount
            else:  # student
                c.execute('UPDATE students SET password = ? WHERE (student_id = ? OR username = ?) AND password = ?',
                         (hashed_new, user_id, user_id, hashed_old))
                rows_affected = c.rowcount

            if rows_affected == 0:
                # Try case-insensitive approach for debugging
                if role == 'student':
                    c.execute("SELECT password FROM students WHERE student_id = ? OR username = ?", 
                             (user_id, user_id))
                elif role == 'teacher':
                    c.execute("SELECT password FROM teachers WHERE username = ?", (user_id,))
                elif role == 'admin':
                    c.execute("SELECT password FROM admins WHERE username = ?", (user_id,))
                
                row = c.fetchone()
                if row:
                    stored_hash = row['password']
                    print(f"Password mismatch - Stored: {stored_hash}, Provided: {hashed_old}")
                return False

            conn.commit()
            return True

        success = safe_db_operation(_change_password)
        if success:
            print(f"✅ Password changed successfully for {role}: {user_id}")
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Current password is incorrect'}), 400

    except Exception as e:
        print(f"Change password error: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# --------------------------
# Health check
# --------------------------
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        def _check_db(conn):
            conn.execute("SELECT 1").fetchone()
            return True

        safe_db_operation(_check_db)
        
        # Return database info
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
# Backup endpoint (optional)
# --------------------------
@app.route('/api/backup', methods=['POST', 'OPTIONS'])
def backup_database():
    """Create a backup of the database"""
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', request.headers.get('Origin', '*'))
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
        response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        return response

    if 'user_id' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        if os.path.exists(DB_PATH):
            backup_path = DB_PATH + '.backup'
            import shutil
            shutil.copy2(DB_PATH, backup_path)
            return jsonify({'success': True, 'backup_path': backup_path})
        return jsonify({'error': 'Database not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
# Initialize database
# --------------------------
# Called at module level so it runs under BOTH:
#   - gunicorn (Render): imports this module directly; __main__ block never runs
#   - python app.py (local dev): also runs here, before the __main__ block below
print("🚀 Davis Academy Portal Starting...")
print(f"Python version: {sys.version}")
print(f"Session directory: {app.config['SESSION_FILE_DIR']}")
print(f"Database path: {DB_PATH}")

# Check if we're on Render and using persistent storage
if os.environ.get('RENDER'):
    PERSISTENT_DIR = '/opt/render/project/data'
    if os.path.exists(PERSISTENT_DIR):
        print(f"✅ Using persistent storage at {PERSISTENT_DIR}")
    else:
        print("⚠️ WARNING: No persistent storage found!")
        print("⚠️ To persist data, create a disk in Render dashboard:")
        print("   1. Go to your service → Disks → Add Disk")
        print("   2. Mount path: /opt/render/project/data")
        print("   3. Size: 1 GB (minimum)")
        print("⚠️ Without persistent storage, all data will be lost on restart!")

init_db()

# --------------------------
# Run server (local dev only)
# --------------------------
if __name__ == '__main__':
    if os.environ.get('RENDER'):
        # Should not reach here on Render (gunicorn is used), but just in case:
        port = int(os.environ.get('PORT', 10000))
        app.run(debug=False, host='0.0.0.0', port=port)
    else:
        print("\n" + "="*50)
        print("📍 Server: http://localhost:5000")
        print("\n🔑 Default admin credentials:")
        print("   - Admin: admin / admin123")
        print("\n📝 Note: No default teacher or student accounts are created.")
        print("   Add teachers and students through the admin dashboard.")
        print("="*50 + "\n")
        app.run(debug=True, host='0.0.0.0', port=5000)