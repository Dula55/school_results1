#!/usr/bin/env python3
import os
import sqlite3
import hashlib
import secrets
import traceback
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
if os.environ.get('RENDER'):
    DB_PATH = '/tmp/davis_academy.db'
else:
    DB_PATH = os.path.join(BASE_DIR, 'davis_academy.db')

def get_db():
    try:
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db
    except Exception as e:
        print(f"Database connection error: {e}")
        raise

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_user_id(prefix):
    return f"{prefix}-{secrets.token_hex(4).upper()}"

def generate_random_password():
    return secrets.token_hex(4)

# --------------------------
# Flask app
# --------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# Use environment variables for production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here-change-in-production')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
# Set secure cookies in production
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'False').lower() == 'true'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_DOMAIN'] = None
app.config['SESSION_COOKIE_NAME'] = 'school_session'
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Critical: Ensure session is saved on every request
app.config['SESSION_COOKIE_PATH'] = '/'

# Allowed origins for CORS - Update for production
ALLOWED_ORIGINS = {
    "http://localhost:5000",
    "http://127.0.0.1:5000",
}

# Add Render.com domain if in production
if os.environ.get('RENDER'):
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        ALLOWED_ORIGINS.add(render_url)
        ALLOWED_ORIGINS.add(render_url.replace('https://', 'http://'))

# Initialize extensions
Session(app)

# Configure CORS properly
CORS(app, 
     supports_credentials=True, 
     origins=list(ALLOWED_ORIGINS),
     allow_headers=['Content-Type', 'Authorization'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])

# --------------------------
# Database initialization
# --------------------------
def init_db():
    conn = None
    try:
        db_exists = os.path.exists(DB_PATH)
        if db_exists:
            print(f"📁 Using existing database at {DB_PATH}")
        else:
            print(f"📁 Creating new database at {DB_PATH}")

        conn = get_db()
        c = conn.cursor()

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

        # default admin - check if any admin exists
        c.execute("SELECT COUNT(*) as count FROM admins")
        if c.fetchone()['count'] == 0:
            admin_password = hash_password(os.environ.get('DEFAULT_ADMIN_PASSWORD', 'admin123'))
            c.execute('''
                INSERT INTO admins (username, password, name, role, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', ('admin', admin_password, 'System Administrator', 'admin', now))
            print("✅ Default admin created")

        # default teacher - check if any teacher exists
        c.execute("SELECT COUNT(*) as count FROM teachers")
        if c.fetchone()['count'] == 0:
            teacher_password = hash_password(os.environ.get('DEFAULT_TEACHER_PASSWORD', 'teacher123'))
            c.execute('''
                INSERT INTO teachers (username, password, name, email, subject, phone, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', ('john.doe', teacher_password, 'John Doe', 'john.doe@davis.edu', 'Mathematics', '555-0100', 'teacher', now))
            print("✅ Default teacher created")

        # default student - check if any student exists
        c.execute("SELECT COUNT(*) as count FROM students")
        if c.fetchone()['count'] == 0:
            student_password = hash_password(os.environ.get('DEFAULT_STUDENT_PASSWORD', 'student123'))
            student_id = generate_user_id('STU')
            c.execute('''
                INSERT INTO students (username, password, name, student_id, level, arm, phone, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', ('jane.smith', student_password, 'Jane Smith', student_id, 'SS3', 'A', '555-0200', 'student', now))
            print("✅ Default student created")

        conn.commit()
        print("✅ Database initialized successfully")
        verify_data(conn)
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        traceback.print_exc()
    finally:
        if conn:
            conn.close()

def verify_data(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as count FROM admins")
    print(f"📊 Admins in database: {c.fetchone()['count']}")
    c.execute("SELECT COUNT(*) as count FROM teachers")
    print(f"📊 Teachers in database: {c.fetchone()['count']}")
    c.execute("SELECT COUNT(*) as count FROM students")
    print(f"📊 Students in database: {c.fetchone()['count']}")

# --------------------------
# Critical: Ensure session is saved and cookies are set properly
# --------------------------
@app.before_request
def before_request():
    # Log session for debugging (only in development)
    if not os.environ.get('RENDER') and request.path.startswith('/api/'):
        print(f"Session before {request.path}: {dict(session)}")

@app.after_request
def after_request(response):
    # Log session after request (only in development)
    if not os.environ.get('RENDER') and request.path.startswith('/api/'):
        print(f"Session after {request.path}: {dict(session)}")
    
    # Set CORS headers for all responses
    origin = request.headers.get('Origin')
    if origin and origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Vary'] = 'Origin'
    
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    
    return response

# --------------------------
# Authentication decorator
# --------------------------
def login_required(role=None):
    def wrapper(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if 'user_id' not in session:
                if request.path.startswith('/api') or request.accept_mimetypes.best == 'application/json':
                    return jsonify({'error': 'Not logged in'}), 401
                return redirect(url_for('index'))
            if role and session.get('role') != role:
                if request.path.startswith('/api') or request.accept_mimetypes.best == 'application/json':
                    return jsonify({'error': 'Unauthorized'}), 403
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return wrapped
    return wrapper

# --------------------------
# Routes - pages (UPDATED: index is now welcome page)
# --------------------------
@app.route('/')
def index():
    """Welcome page - accessible to everyone"""
    return render_template("index.html")

@app.route('/login')
def login_page():
    """Legacy login route - redirects to index"""
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
# API - Students
# --------------------------
@app.route('/api/students', methods=['GET', 'OPTIONS'])
def get_students():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT username, name, student_id, level, arm FROM students ORDER BY name")
        students = [{k: row[k] for k in row.keys()} for row in c.fetchall()]
        conn.close()
        return jsonify(students)
    except Exception as e:
        print(f"Error fetching students: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to fetch students'}), 500

# --------------------------
# API - Current user
# --------------------------
@app.route('/api/current-user', methods=['GET', 'OPTIONS'])
def get_current_user():
    if request.method == 'OPTIONS':
        response = make_response('', 204)
        return response

    if not os.environ.get('RENDER'):
        print(f"Current session in /api/current-user: {dict(session)}")
    
    if 'user_id' in session and 'role' in session:
        # Get additional user data from database
        role = session.get('role')
        user_id = session.get('user_id')
        
        user_info = {
            'user': {
                'username': session.get('username', ''),
                'name': session.get('name', ''),
                'role': role,
                'user_id': user_id
            }
        }
        
        # Add role-specific fields
        if role == 'student':
            user_info['user']['student_id'] = session.get('student_id', user_id)
        
        return jsonify(user_info)
    
    return jsonify({'error': 'Not logged in'}), 401

# --------------------------
# API - Teacher Results
# --------------------------
@app.route('/api/teacher-results', methods=['GET', 'OPTIONS'])
def get_teacher_results():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    try:
        term = request.args.get('term')
        conn = get_db()
        c = conn.cursor()
        
        # Get all scores with student details
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
        rows = c.fetchall()
        
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
        
        conn.close()
        return jsonify(result_list)
        
    except Exception as e:
        print(f"Error fetching teacher results: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to fetch results'}), 500

# --------------------------
# API - Scores
# --------------------------
@app.route('/api/scores', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
def api_scores():
    if request.method == 'OPTIONS':
        return make_response('', 204)

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    try:
        if request.method == 'GET':
            student_id = request.args.get('student_id')
            term = request.args.get('term')
            session_param = request.args.get('session')

            conn = get_db()
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
                    result = [{
                        'student_id': student_id,
                        'term': term,
                        'session': session_param,
                        'subjects': subjects
                    }]
                else:
                    result = []
                conn.close()
                return jsonify(result)
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
                conn.close()
                return jsonify(results)

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

            conn = get_db()
            c = conn.cursor()
            c.execute('''
                SELECT id FROM scores
                WHERE student_id = ? AND term = ? AND session = ? AND subject = ?
            ''', (data['student_id'], data['term'], data['session'], data['subject']))
            if c.fetchone():
                conn.close()
                return jsonify({'error': 'Score already exists for this subject'}), 400

            now = datetime.now().isoformat()
            c.execute('''
                INSERT INTO scores
                (student_id, term, session, subject, ca1, ca2, ca3, exam, total, grade, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (data['student_id'], data['term'], data['session'], data['subject'],
                  ca1, ca2, ca3, exam, total, grade, now, now))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': 'Score added successfully'})

        if request.method == 'DELETE':
            student_id = request.args.get('student_id')
            term = request.args.get('term')
            session_param = request.args.get('session')
            subject = request.args.get('subject')
            if not all([student_id, term, session_param, subject]):
                return jsonify({'error': 'Missing parameters'}), 400

            conn = get_db()
            c = conn.cursor()
            c.execute('''
                DELETE FROM scores
                WHERE student_id = ? AND term = ? AND session = ? AND subject = ?
            ''', (student_id, term, session_param, subject))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': 'Score deleted successfully'})

    except Exception as e:
        print(f"Error in /api/scores: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to process scores'}), 500

# --------------------------
# API - Teachers
# --------------------------
@app.route('/api/teachers', methods=['GET', 'POST', 'OPTIONS'])
def teachers_api():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
        
    if request.method == 'GET':
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT username, name, email, subject, phone, role FROM teachers")
            teachers = [{k: r[k] for k in r.keys()} for r in c.fetchall()]
            conn.close()
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

            conn = get_db()
            c = conn.cursor()
            try:
                c.execute('''
                    INSERT INTO teachers (username, password, name, email, subject, phone, role, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (username, hashed_password, name, email, subject, phone, 'teacher', now))
                conn.commit()
                return jsonify({'success': True, 'username': username, 'password': password, 'name': name})
            except sqlite3.IntegrityError:
                return jsonify({'error': 'Teacher with this email already exists'}), 400
            finally:
                conn.close()
        except Exception as e:
            print(f"Error adding teacher: {e}")
            return jsonify({'error': str(e)}), 500

@app.route('/api/teachers/<username>', methods=['DELETE', 'OPTIONS'])
def delete_teacher(username):
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
        
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM teachers WHERE username = ?", (username,))
        conn.commit()
        deleted = c.rowcount > 0
        conn.close()
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
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
        
    if request.method == 'GET':
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT username, name, student_id, level, arm, phone, role FROM students")
            students = [{k: r[k] for k in r.keys()} for r in c.fetchall()]
            conn.close()
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

            conn = get_db()
            c = conn.cursor()
            try:
                c.execute('''
                    INSERT INTO students (username, password, name, student_id, level, arm, phone, role, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (username, hashed_password, name, student_id, level, arm, phone, 'student', now))
                conn.commit()
                return jsonify({'success': True, 'username': username, 'student_id': student_id, 'password': password, 'name': name})
            except sqlite3.IntegrityError:
                return jsonify({'error': 'Student with this ID or username already exists'}), 400
            finally:
                conn.close()
        except Exception as e:
            print(f"Error adding student: {e}")
            return jsonify({'error': str(e)}), 500

@app.route('/api/students/manage/<student_id>', methods=['DELETE', 'OPTIONS'])
def delete_student_manage(student_id):
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
        
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM scores WHERE student_id = ?", (student_id,))
        c.execute("DELETE FROM students WHERE student_id = ?", (student_id,))
        conn.commit()
        deleted = c.rowcount > 0
        conn.close()
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
        return make_response('', 204)

    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    if request.method == 'GET':
        try:
            student_id = request.args.get('student_id')
            conn = get_db()
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
            scores = [{k: r[k] for k in r.keys()} for r in c.fetchall()]
            conn.close()
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
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO scores
                (student_id, term, session, subject, ca1, ca2, ca3, exam, total, grade, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (student_id, term, session_val, subject, ca1, ca2, ca3, exam, total, grade, now, now))
            conn.commit()
            conn.close()
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
        return make_response('', 204)
    
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
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            DELETE FROM scores WHERE student_id = ? AND subject = ? AND term = ? AND session = ?
        ''', (student_id, subject, term, session_val))
        conn.commit()
        deleted = c.rowcount > 0
        conn.close()
        if deleted:
            return jsonify({'success': True})
        return jsonify({'error': 'Score not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scores/delete-sheet', methods=['POST', 'OPTIONS'])
def delete_score_sheet():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
        
    try:
        data = request.get_json() or {}
        student_id = data.get('student_id')
        term = data.get('term')
        session_val = data.get('session')
        if not all([student_id, term, session_val]):
            return jsonify({'error': 'Missing required fields'}), 400
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            DELETE FROM scores WHERE student_id = ? AND term = ? AND session = ?
        ''', (student_id, term, session_val))
        conn.commit()
        deleted = c.rowcount > 0
        conn.close()
        if deleted:
            return jsonify({'success': True})
        return jsonify({'error': 'Score sheet not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --------------------------
# API - Login / Logout / Check session
# --------------------------
@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON data'}), 400

        role = data.get('role')
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not all([role, username, password]):
            return jsonify({'error': 'All fields are required'}), 400

        hashed_password = hash_password(password)
        conn = get_db()
        c = conn.cursor()

        user = None
        if role == 'admin':
            c.execute("SELECT * FROM admins WHERE username = ? AND password = ?", (username, hashed_password))
        elif role == 'teacher':
            c.execute("SELECT * FROM teachers WHERE username = ? AND password = ?", (username, hashed_password))
        elif role == 'student':
            c.execute("SELECT * FROM students WHERE (username = ? OR student_id = ?) AND password = ?", (username, username, hashed_password))
        else:
            conn.close()
            return jsonify({'error': 'Invalid role selected'}), 400

        row = c.fetchone()
        if row:
            user = {k: row[k] for k in row.keys()}
        conn.close()

        if not user:
            return jsonify({'error': 'Invalid credentials'}), 401

        # Clear any existing session
        session.clear()
        
        # Set session data
        session.permanent = True
        session['role'] = role
        session['name'] = user.get('name', '')
        session['username'] = user.get('username', '')
        
        if role == 'student':
            session['user_id'] = user.get('student_id') or user.get('username')
            # Also store student_id separately for convenience
            if user.get('student_id'):
                session['student_id'] = user.get('student_id')
        else:
            session['user_id'] = user.get('username')
        
        # Force session save
        session.modified = True

        if not os.environ.get('RENDER'):
            print(f"Session after login: {dict(session)}")

        redirect_url = {
            'admin': '/admin_dashboard',
            'teacher': '/teacher_dashboard',
            'student': '/student_dashboard'
        }.get(role, '/')

        # Remove password from user object
        user.pop('password', None)
        
        return jsonify({'success': True, 'redirect': redirect_url, 'user': user})
    
    except Exception as e:
        print(f"Login error: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Server error occurred'}), 500

@app.route('/api/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    session.clear()
    response = jsonify({'success': True})
    # Clear session cookie
    response.set_cookie('school_session', '', expires=0)
    return response

@app.route('/api/check-session', methods=['GET', 'OPTIONS'])
def check_session():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if not os.environ.get('RENDER'):
        print(f"Session in check-session: {dict(session)}")
    
    if 'user_id' in session:
        return jsonify({
            'logged_in': True, 
            'role': session.get('role'), 
            'name': session.get('name'), 
            'user_id': session.get('user_id')
        })
    return jsonify({'logged_in': False})

@app.route('/api/change-password', methods=['POST', 'OPTIONS'])
def change_password():
    if request.method == 'OPTIONS':
        return make_response('', 204)
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
        
    try:
        data = request.get_json() or {}
        old_password = data.get('old_password')
        new_password = data.get('new_password')
        
        if not old_password or not new_password:
            return jsonify({'error': 'All fields are required'}), 400
        if len(new_password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        
        role = session.get('role')
        user_id = session.get('user_id')
        
        if not role or not user_id:
            return jsonify({'error': 'Not logged in'}), 401

        hashed_old = hash_password(old_password)
        hashed_new = hash_password(new_password)
        
        conn = get_db()
        c = conn.cursor()
        
        if role == 'admin':
            c.execute('UPDATE admins SET password = ? WHERE username = ? AND password = ?', 
                     (hashed_new, user_id, hashed_old))
        elif role == 'teacher':
            c.execute('UPDATE teachers SET password = ? WHERE username = ? AND password = ?', 
                     (hashed_new, user_id, hashed_old))
        else:  # student
            c.execute('UPDATE students SET password = ? WHERE (student_id = ? OR username = ?) AND password = ?', 
                     (hashed_new, user_id, user_id, hashed_old))
        
        if c.rowcount == 0:
            conn.close()
            return jsonify({'error': 'Current password is incorrect'}), 400
        
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"Change password error: {e}")
        return jsonify({'error': str(e)}), 500

# --------------------------
# Debug endpoints
# --------------------------
@app.route('/api/debug/users', methods=['GET'])
def debug_users():
    # Only allow debug in development or with special token
    if os.environ.get('RENDER') and not os.environ.get('DEBUG_ENABLED'):
        return jsonify({'error': 'Debug endpoint disabled in production'}), 403
    
    try:
        conn = get_db()
        c = conn.cursor()
        result = {'admins': [], 'teachers': [], 'students': []}
        
        c.execute("SELECT username, name, role, created_at FROM admins")
        for r in c.fetchall():
            result['admins'].append({k: r[k] for k in r.keys()})
        
        c.execute("SELECT username, name, email, subject, role FROM teachers")
        for r in c.fetchall():
            result['teachers'].append({k: r[k] for k in r.keys()})
        
        c.execute("SELECT username, name, student_id, level, arm, role FROM students")
        for r in c.fetchall():
            result['students'].append({k: r[k] for k in r.keys()})
        
        conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --------------------------
# Health check endpoint for Render
# --------------------------
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Render.com"""
    try:
        # Test database connection
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
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
# Run server
# --------------------------
if __name__ == '__main__':
    print("🚀 Davis Academy Portal Starting...")
    
    # Initialize database
    init_db()
    
    # Check if running on Render
    if os.environ.get('RENDER'):
        print("📡 Running on Render.com")
        port = int(os.environ.get('PORT', 10000))
        print(f"\n" + "="*50)
        print(f"📍 Server will start on port {port}")
        print("📍 Make sure to set these environment variables in Render:")
        print("   - SECRET_KEY: (set a secure random string)")
        print("   - SESSION_COOKIE_SECURE: True")
        print("   - DEFAULT_ADMIN_PASSWORD: (optional)")
        print("   - DEFAULT_TEACHER_PASSWORD: (optional)")
        print("   - DEFAULT_STUDENT_PASSWORD: (optional)")
        print("="*50 + "\n")
        
        # Production settings for Render
        app.run(debug=False, host='0.0.0.0', port=port)
    else:
        print("\n" + "="*50)
        print("📍 Server: http://localhost:5000")
        print("📍 Welcome page: http://localhost:5000")
        print("📍 Debug users: http://localhost:5000/api/debug/users")
        print("\n🔑 Default credentials:")
        print("   - Admin: admin / admin123")
        print("   - Teacher: john.doe / teacher123")
        print("   - Student: jane.smith / student123")
        print("="*50 + "\n")
        app.run(debug=True, host='0.0.0.0', port=5000)