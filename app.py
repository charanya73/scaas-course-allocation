from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import sqlite3
import hashlib
import re
import os
import smtplib
import io
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
DB_PATH = 'scaas.db'

# ─── EMAIL CONFIG ─────────────────────────────────────────────────────────────────
# Email config is saved in the database — configure from Admin Dashboard → Emails tab
EMAIL_CONFIG = {
    'smtp_server': 'smtp.gmail.com',
    'smtp_port': 587,
    'sender_email': '',
    'sender_password': '',
    'sender_name': 'SCAAS Course Allocation',
    'enabled': False
}

def load_email_config():
    """Load email config from database into memory"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM email_config WHERE id=1').fetchone()
        conn.close()
        if row and row['sender_email']:
            EMAIL_CONFIG['sender_email']    = row['sender_email']
            EMAIL_CONFIG['sender_password'] = row['sender_password']
            EMAIL_CONFIG['sender_name']     = row['sender_name'] or 'SCAAS Course Allocation'
            EMAIL_CONFIG['enabled']         = bool(row['enabled'])
    except:
        pass

# ─── DATABASE ─────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        student_id TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL, department TEXT NOT NULL,
        cgpa REAL DEFAULT 0.0, completed_courses TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS courses (
        course_id INTEGER PRIMARY KEY AUTOINCREMENT, course_name TEXT NOT NULL,
        course_code TEXT UNIQUE NOT NULL, department TEXT NOT NULL,
        seat_capacity INTEGER NOT NULL, seats_remaining INTEGER NOT NULL,
        prerequisite TEXT DEFAULT 'None', day TEXT DEFAULT 'Monday',
        time_slot TEXT DEFAULT '09:00-10:30', venue TEXT DEFAULT 'TBD',
        start_date TEXT DEFAULT '', end_date TEXT DEFAULT '',
        description TEXT DEFAULT '', is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS allocation_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, preference_deadline TEXT,
        max_preferences INTEGER DEFAULT 5, allow_modifications INTEGER DEFAULT 1,
        allocation_run INTEGER DEFAULT 0, university_name TEXT DEFAULT 'University',
        semester TEXT DEFAULT 'Semester 1 2024-25',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
        course_id INTEGER NOT NULL, priority_rank INTEGER NOT NULL,
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY(course_id) REFERENCES courses(course_id) ON DELETE CASCADE,
        UNIQUE(student_id, course_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS allocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
        course_id INTEGER, allocation_status TEXT DEFAULT 'Pending',
        allocated_at TIMESTAMP, notes TEXT DEFAULT '',
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY(course_id) REFERENCES courses(course_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
        course_id INTEGER NOT NULL, rating INTEGER NOT NULL,
        teaching_quality INTEGER DEFAULT 3, content_quality INTEGER DEFAULT 3,
        difficulty_level INTEGER DEFAULT 3, would_recommend INTEGER DEFAULT 1,
        comments TEXT DEFAULT '', submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY(course_id) REFERENCES courses(course_id),
        UNIQUE(student_id, course_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS certificates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
        course_id INTEGER NOT NULL, issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        certificate_id TEXT UNIQUE NOT NULL,
        FOREIGN KEY(student_id) REFERENCES students(id),
        FOREIGN KEY(course_id) REFERENCES courses(course_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS email_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recipient_email TEXT NOT NULL,
        subject TEXT NOT NULL, email_type TEXT NOT NULL,
        status TEXT DEFAULT 'sent', sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS email_config (
        id INTEGER PRIMARY KEY,
        sender_email TEXT DEFAULT '',
        sender_password TEXT DEFAULT '',
        sender_name TEXT DEFAULT 'SCAAS Course Allocation',
        enabled INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL, token TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'student',
        expires_at TIMESTAMP NOT NULL,
        used INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('INSERT OR IGNORE INTO email_config (id, sender_email, sender_password, enabled) VALUES (1, \'\', \'\', 0)')
    c.execute('INSERT OR IGNORE INTO allocation_settings (id, preference_deadline, max_preferences, allow_modifications, allocation_run) VALUES (1, NULL, 5, 1, 0)')
    conn.commit()
    conn.close()

def migrate_db():
    """Runs on every startup — adds missing columns and fixes bad data."""
    conn = get_db()
    # Add start_date / end_date columns if upgrading from old DB
    try:
        conn.execute("ALTER TABLE courses ADD COLUMN start_date TEXT DEFAULT ''")
    except: pass
    try:
        conn.execute("ALTER TABLE courses ADD COLUMN end_date TEXT DEFAULT ''")
    except: pass
    # Fix all bad prerequisite values in one CASE statement
    conn.execute("""
        UPDATE courses SET prerequisite = CASE
            WHEN prerequisite IS NULL               THEN 'None'
            WHEN TRIM(prerequisite) = ''            THEN 'None'
            WHEN UPPER(TRIM(prerequisite)) = 'NONE' THEN 'None'
            ELSE UPPER(TRIM(prerequisite))
        END
    """)
    conn.commit()
    conn.close()

def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()

def validate_password(password):
    if len(password) < 8: return False, "Password must be at least 8 characters."
    if not re.search(r'[A-Z]', password): return False, "Need at least one uppercase letter (A-Z)."
    if not re.search(r'[a-z]', password): return False, "Need at least one lowercase letter (a-z)."
    if not re.search(r'[0-9]', password): return False, "Need at least one number (0-9)."
    if not re.search(r'[!@#$%^&*()_+\-=\[\]{}|;:,.<>?]', password): return False, "Need at least one special character (!@#$...)."
    return True, "OK"

def get_settings():
    conn = get_db()
    s = conn.execute('SELECT * FROM allocation_settings WHERE id=1').fetchone()
    conn.close()
    return dict(s) if s else {}

# ─── EMAIL SYSTEM ─────────────────────────────────────────────────────────────────
def send_email(to_email, to_name, subject, html_body, email_type='general'):
    conn = get_db()
    if not EMAIL_CONFIG['enabled'] or not EMAIL_CONFIG['sender_email']:
        conn.execute('INSERT INTO email_log (recipient_email, subject, email_type, status) VALUES (?,?,?,?)',
                     (to_email, subject, email_type, 'simulated - email not configured'))
        conn.commit(); conn.close()
        return True, 'simulated'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{EMAIL_CONFIG['sender_name']} <{EMAIL_CONFIG['sender_email']}>"
        msg['To'] = f"{to_name} <{to_email}>"
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port']) as server:
            server.starttls()
            server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
            server.sendmail(EMAIL_CONFIG['sender_email'], to_email, msg.as_string())
        conn.execute('INSERT INTO email_log (recipient_email, subject, email_type, status) VALUES (?,?,?,?)',
                     (to_email, subject, email_type, 'sent'))
        conn.commit(); conn.close()
        return True, 'sent'
    except Exception as e:
        conn.execute('INSERT INTO email_log (recipient_email, subject, email_type, status) VALUES (?,?,?,?)',
                     (to_email, subject, email_type, f'failed: {str(e)}'))
        conn.commit(); conn.close()
        return False, str(e)

def build_email_base(header_color, icon, title, subtitle, content):
    return f'''<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">
      <div style="background:{header_color};padding:28px;text-align:center;">
        <div style="font-size:40px;margin-bottom:8px;">{icon}</div>
        <h1 style="color:#fff;margin:0;font-size:22px;">{title}</h1>
        <p style="color:rgba(255,255,255,0.8);margin:6px 0 0;font-size:14px;">{subtitle}</p>
      </div>
      <div style="padding:28px;background:#fff;">{content}</div>
      <div style="background:#f8fafc;padding:14px;text-align:center;border-top:1px solid #e5e7eb;">
        <p style="color:#9ca3af;font-size:12px;margin:0;">This is an automated message from SCAAS. Do not reply to this email.</p>
      </div>
    </div>'''

def email_preferences_submitted(student, preferences, settings):
    rows = ''.join([f'<tr><td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;font-weight:bold;color:#1e40af;">Preference {p["priority_rank"]}</td><td style="padding:9px 14px;border-bottom:1px solid #e5e7eb;">{p["course_name"]} <span style="color:#6b7280;font-size:13px;">({p["course_code"]})</span></td></tr>' for p in preferences])
    content = f'''<p style="color:#374151;">Dear <b>{student["name"]}</b>,</p>
      <p style="color:#374151;">Your course preferences for <b>{settings.get("semester","this semester")}</b> have been successfully submitted.</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;">
        <thead><tr style="background:#1e40af;"><th style="padding:10px 14px;color:#fff;text-align:left;">Priority</th><th style="padding:10px 14px;color:#fff;text-align:left;">Course</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:14px;">
        <p style="margin:0;color:#92400e;font-size:14px;">⏳ You will receive your allocation result by email once the admin runs the allocation process.</p>
      </div>
      <p style="color:#6b7280;font-size:13px;margin-top:16px;">Student ID: {student["student_id"]} | Department: {student["department"]}</p>'''
    html = build_email_base('#1e40af', '📋', 'Preferences Submitted Successfully', settings.get('university_name','University'), content)
    return send_email(student['email'], student['name'], f"✅ Course Preferences Submitted – {settings.get('semester','')}", html, 'preferences_submitted')

def email_allocation_result(student, course, status, settings):
    if status == 'Allocated':
        content = f'''<p style="color:#374151;">Dear <b>{student["name"]}</b>,</p>
          <p style="color:#374151;">The course allocation for <b>{settings.get("semester","this semester")}</b> is complete. You have been successfully allocated a course!</p>
          <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:20px;margin:16px 0;text-align:center;">
            <h2 style="color:#065f46;margin:0 0 6px;">{course.get("course_name","")}</h2>
            <p style="color:#047857;margin:4px 0;">Code: <b>{course.get("course_code","")}</b> &nbsp;|&nbsp; Department: {course.get("department","")}</p>
          </div>
          <table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
            <tr style="background:#f8fafc;"><td style="padding:11px 14px;font-weight:bold;border-bottom:1px solid #e5e7eb;">📅 Day</td><td style="padding:11px 14px;border-bottom:1px solid #e5e7eb;">{course.get("day","")}</td></tr>
            <tr><td style="padding:11px 14px;font-weight:bold;border-bottom:1px solid #e5e7eb;">⏰ Time</td><td style="padding:11px 14px;border-bottom:1px solid #e5e7eb;">{course.get("time_slot","")}</td></tr>
            <tr style="background:#f8fafc;"><td style="padding:11px 14px;font-weight:bold;border-bottom:1px solid #e5e7eb;">📍 Venue</td><td style="padding:11px 14px;border-bottom:1px solid #e5e7eb;"><b style="color:#1e40af;">{course.get("venue","TBD")}</b></td></tr>
            <tr><td style="padding:11px 14px;font-weight:bold;">📖 About</td><td style="padding:11px 14px;">{course.get("description","")}</td></tr>
          </table>
          <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:14px;margin-top:16px;">
            <p style="margin:0;color:#1e40af;font-size:14px;">📍 Please report to <b>{course.get("venue","the assigned venue")}</b> on <b>{course.get("day","")}</b> at <b>{course.get("time_slot","")}</b> for your first class.</p>
          </div>
          <p style="color:#6b7280;font-size:13px;margin-top:16px;">Student ID: {student["student_id"]} | Department: {student["department"]}</p>'''
        html = build_email_base('#059669', '🎉', 'Course Allocated Successfully!', settings.get('university_name','University'), content)
        subject = f"🎉 Course Allocated – {settings.get('semester','')}"
    else:
        content = f'''<p style="color:#374151;">Dear <b>{student["name"]}</b>,</p>
          <p style="color:#374151;">The course allocation for <b>{settings.get("semester","")}</b> has been completed.</p>
          <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:18px;text-align:center;margin:16px 0;">
            <p style="color:#991b1b;font-weight:bold;margin:0;">Unfortunately, you could not be allocated a course in this round.</p>
          </div>
          <p style="color:#374151;">Please contact your academic advisor or visit the admin office for manual assignment.</p>
          <p style="color:#6b7280;font-size:13px;">Student ID: {student["student_id"]} | Department: {student["department"]}</p>'''
        html = build_email_base('#dc2626', '😔', 'Course Not Allocated', settings.get('university_name','University'), content)
        subject = f"Course Allocation Result – {settings.get('semester','')}"
    return send_email(student['email'], student['name'], subject, html, 'allocation_result')

def email_deadline_reminder(student, deadline, settings):
    content = f'''<p style="color:#374151;">Dear <b>{student["name"]}</b>,</p>
      <p style="color:#374151;">This is a reminder that you have <b>not yet submitted</b> your course preferences for <b>{settings.get("semester","this semester")}</b>.</p>
      <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:20px;margin:16px 0;text-align:center;">
        <p style="color:#92400e;font-size:18px;font-weight:bold;margin:0 0 6px;">Deadline: {deadline}</p>
        <p style="color:#b45309;margin:0;font-size:14px;">Submit your preferences before this deadline to be included in course allocation.</p>
      </div>
      <div style="text-align:center;margin:24px 0;">
        <a href="http://localhost:5000/preferences" style="background:#1e40af;color:#fff;padding:12px 30px;border-radius:6px;text-decoration:none;font-weight:bold;font-size:15px;">Submit Preferences Now →</a>
      </div>
      <p style="color:#6b7280;font-size:13px;">Student ID: {student["student_id"]} | Department: {student["department"]}</p>'''
    html = build_email_base('#d97706', '⏰', 'Reminder: Submit Course Preferences', settings.get('university_name','University'), content)
    return send_email(student['email'], student['name'], "⏰ REMINDER: Submit Course Preferences Before Deadline", html, 'deadline_reminder')

# ─── CERTIFICATE PDF ──────────────────────────────────────────────────────────────
def generate_certificate_pdf(student, course, cert_id, settings):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pw, ph = landscape(A4)
        c = rl_canvas.Canvas(buffer, pagesize=landscape(A4))

        # Background
        c.setFillColor(colors.HexColor('#f0f7ff'))
        c.rect(0, 0, pw, ph, fill=1, stroke=0)

        # Outer border
        c.setStrokeColor(colors.HexColor('#1e40af'))
        c.setLineWidth(6)
        c.rect(18, 18, pw-36, ph-36, fill=0, stroke=1)
        c.setStrokeColor(colors.HexColor('#3b82f6'))
        c.setLineWidth(1.5)
        c.rect(28, 28, pw-56, ph-56, fill=0, stroke=1)

        # Header band
        c.setFillColor(colors.HexColor('#1e40af'))
        c.rect(28, ph-108, pw-56, 80, fill=1, stroke=0)
        c.setFont('Helvetica-Bold', 22)
        c.setFillColor(colors.white)
        c.drawCentredString(pw/2, ph-62, settings.get('university_name','University').upper())
        c.setFont('Helvetica', 12)
        c.setFillColor(colors.HexColor('#bfdbfe'))
        c.drawCentredString(pw/2, ph-84, 'Student Course Allocation Automation System (SCAAS)')

        # Title
        c.setFont('Helvetica-Bold', 34)
        c.setFillColor(colors.HexColor('#1e3a8a'))
        c.drawCentredString(pw/2, ph-148, 'CERTIFICATE OF COURSE ENROLLMENT')

        # Gold line
        c.setStrokeColor(colors.HexColor('#f59e0b'))
        c.setLineWidth(3)
        c.line(pw/2-200, ph-162, pw/2+200, ph-162)

        # Body text
        c.setFont('Helvetica', 14)
        c.setFillColor(colors.HexColor('#4b5563'))
        c.drawCentredString(pw/2, ph-198, 'This is to certify that')

        # Student name
        c.setFont('Helvetica-Bold', 32)
        c.setFillColor(colors.HexColor('#1e40af'))
        c.drawCentredString(pw/2, ph-242, student['name'])
        nw = c.stringWidth(student['name'], 'Helvetica-Bold', 32)
        c.setStrokeColor(colors.HexColor('#1e40af'))
        c.setLineWidth(1.5)
        c.line(pw/2-nw/2, ph-250, pw/2+nw/2, ph-250)

        c.setFont('Helvetica', 13)
        c.setFillColor(colors.HexColor('#374151'))
        c.drawCentredString(pw/2, ph-272, f'Student ID: {student["student_id"]}  ·  Department: {student["department"]}')

        c.setFont('Helvetica', 14)
        c.drawCentredString(pw/2, ph-300, 'has been successfully enrolled in')

        # Course box
        c.setFillColor(colors.HexColor('#eff6ff'))
        c.setStrokeColor(colors.HexColor('#3b82f6'))
        c.setLineWidth(2)
        c.roundRect(pw/2-230, ph-352, 460, 42, 8, fill=1, stroke=1)
        c.setFont('Helvetica-Bold', 19)
        c.setFillColor(colors.HexColor('#1e40af'))
        c.drawCentredString(pw/2, ph-335, f'{course["course_name"]}  ({course["course_code"]})')

        # Course details
        c.setFont('Helvetica', 12)
        c.setFillColor(colors.HexColor('#4b5563'))
        c.drawCentredString(pw/2, ph-372, f'Department: {course["department"]}  ·  Venue: {course.get("venue","TBD")}  ·  Schedule: {course["day"]} {course["time_slot"]}')
        c.drawCentredString(pw/2, ph-392, f'Semester: {settings.get("semester","2024-25")}')

        # Signature lines
        c.setStrokeColor(colors.HexColor('#d1d5db'))
        c.setLineWidth(1)
        c.line(70, 105, 250, 105)
        c.line(pw-250, 105, pw-70, 105)
        c.setFont('Helvetica-Bold', 11)
        c.setFillColor(colors.HexColor('#374151'))
        c.drawCentredString(160, 92, 'Course Coordinator')
        c.drawCentredString(pw-160, 92, 'Academic Registrar')

        # Bottom info
        c.setFont('Helvetica', 10)
        c.setFillColor(colors.HexColor('#6b7280'))
        c.drawCentredString(pw/2, 105, f'Date of Issue: {datetime.now().strftime("%d %B %Y")}')
        c.drawCentredString(pw/2, 90, f'Certificate ID: {cert_id}')

        # Gold seal
        c.setFillColor(colors.HexColor('#f59e0b'))
        c.setStrokeColor(colors.HexColor('#d97706'))
        c.setLineWidth(2)
        c.circle(pw/2, 72, 26, fill=1, stroke=1)
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(colors.white)
        c.drawCentredString(pw/2, 77, 'OFFICIALLY')
        c.drawCentredString(pw/2, 68, 'CERTIFIED')

        c.save()
        buffer.seek(0)
        return buffer
    except ImportError:
        return None

# ─── AUTH ROUTES ──────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    conn = get_db()
    courses = conn.execute('SELECT * FROM courses WHERE is_active=1 ORDER BY department, course_name').fetchall()
    conn.close()
    return render_template('index.html', courses=courses)

@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        data = request.get_json()
        name = data.get('name','').strip()
        student_id = data.get('student_id','').strip()
        email = data.get('email','').strip().lower()
        password = data.get('password','')
        confirm = data.get('confirm_password','')
        department = data.get('department','').strip()
        cgpa = data.get('cgpa', 0)
        completed = data.get('completed_courses','').strip()
        if not all([name, student_id, email, password, department]):
            return jsonify({'success':False,'message':'All fields are required.'})
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            return jsonify({'success':False,'message':'Invalid email address.'})
        if password != confirm:
            return jsonify({'success':False,'message':'Passwords do not match.'})
        valid, msg = validate_password(password)
        if not valid: return jsonify({'success':False,'message':msg})
        try:
            cgpa_val = float(cgpa)
            if cgpa_val < 0 or cgpa_val > 10: return jsonify({'success':False,'message':'CGPA must be 0–10.'})
        except: return jsonify({'success':False,'message':'Invalid CGPA.'})
        try:
            conn = get_db()
            conn.execute('INSERT INTO students (name,student_id,email,password,department,cgpa,completed_courses) VALUES (?,?,?,?,?,?,?)',
                (name, student_id, email, hash_password(password), department, cgpa_val, completed))
            conn.commit(); conn.close()
            return jsonify({'success':True,'message':'Account created! Please login.'})
        except sqlite3.IntegrityError as e:
            if 'email' in str(e): return jsonify({'success':False,'message':'Email already registered.'})
            if 'student_id' in str(e): return jsonify({'success':False,'message':'Student ID already registered.'})
            return jsonify({'success':False,'message':'Registration failed.'})
    return render_template('signup.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        email = data.get('email','').strip().lower()
        password = data.get('password','')
        conn = get_db()
        student = conn.execute('SELECT * FROM students WHERE email=? AND password=?', (email, hash_password(password))).fetchone()
        conn.close()
        if student:
            session['student_id'] = student['id']
            session['student_name'] = student['name']
            session['role'] = 'student'
            return jsonify({'success':True,'redirect':url_for('dashboard')})
        return jsonify({'success':False,'message':'Invalid email or password.'})
    return render_template('login.html')

@app.route('/admin/signup', methods=['GET','POST'])
def admin_signup():
    if request.method == 'POST':
        data = request.get_json()
        name = data.get('name','').strip()
        email = data.get('email','').strip().lower()
        password = data.get('password','')
        confirm = data.get('confirm_password','')
        secret_key = data.get('secret_key','')
        if secret_key != 'UNIVERSITY2024ADMIN':
            return jsonify({'success':False,'message':'Invalid institution secret key.'})
        if not all([name, email, password]): return jsonify({'success':False,'message':'All fields required.'})
        if password != confirm: return jsonify({'success':False,'message':'Passwords do not match.'})
        valid, msg = validate_password(password)
        if not valid: return jsonify({'success':False,'message':msg})
        try:
            conn = get_db()
            conn.execute('INSERT INTO admins (name,email,password) VALUES (?,?,?)', (name, email, hash_password(password)))
            conn.commit(); conn.close()
            return jsonify({'success':True,'message':'Admin account created!'})
        except sqlite3.IntegrityError:
            return jsonify({'success':False,'message':'Email already registered as admin.'})
    return render_template('admin_signup.html')

@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        data = request.get_json()
        email = data.get('email','').strip().lower()
        password = data.get('password','')
        conn = get_db()
        admin = conn.execute('SELECT * FROM admins WHERE email=? AND password=?', (email, hash_password(password))).fetchone()
        conn.close()
        if admin:
            session['admin_id'] = admin['id']
            session['admin_name'] = admin['name']
            session['role'] = 'admin'
            return jsonify({'success':True,'redirect':url_for('admin_dashboard')})
        return jsonify({'success':False,'message':'Invalid email or password.'})
    return render_template('admin_login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ─── STUDENT ROUTES ───────────────────────────────────────────────────────────────
@app.route('/dashboard')
def dashboard():
    if session.get('role') != 'student': return redirect(url_for('login'))
    conn = get_db()
    student = conn.execute('SELECT * FROM students WHERE id=?', (session['student_id'],)).fetchone()
    allocation = conn.execute('''SELECT a.*, c.course_name, c.course_code, c.department, c.day, c.time_slot, c.venue
        FROM allocations a JOIN courses c ON a.course_id=c.course_id
        WHERE a.student_id=? AND a.allocation_status="Allocated"''', (session['student_id'],)).fetchone()
    prefs = conn.execute('''SELECT p.*, c.course_name, c.course_code, c.department
        FROM preferences p JOIN courses c ON p.course_id=c.course_id
        WHERE p.student_id=? ORDER BY p.priority_rank''', (session['student_id'],)).fetchall()
    settings = conn.execute('SELECT * FROM allocation_settings WHERE id=1').fetchone()
    has_feedback = False
    has_cert = False
    if allocation:
        has_feedback = conn.execute('SELECT id FROM feedback WHERE student_id=? AND course_id=?',
            (session['student_id'], allocation['course_id'])).fetchone() is not None
        has_cert = conn.execute('SELECT id FROM certificates WHERE student_id=? AND course_id=?',
            (session['student_id'], allocation['course_id'])).fetchone() is not None
    conn.close()
    return render_template('dashboard.html', student=student, allocation=allocation,
        preferences=prefs, settings=settings, has_feedback=has_feedback, has_cert=has_cert)

@app.route('/preferences', methods=['GET','POST'])
def preferences():
    if session.get('role') != 'student': return redirect(url_for('login'))
    settings = get_settings()
    conn = get_db()
    student = conn.execute('SELECT * FROM students WHERE id=?', (session['student_id'],)).fetchone()
    if request.method == 'POST':
        if not settings.get('allow_modifications') and settings.get('allocation_run'):
            conn.close()
            return jsonify({'success':False,'message':'Preference submission is closed.'})
        data = request.get_json()
        selected = data.get('preferences', [])
        if not selected:
            conn.close()
            return jsonify({'success':False,'message':'Select at least one course.'})
        max_prefs = settings.get('max_preferences', 5)
        if len(selected) > max_prefs:
            conn.close()
            return jsonify({'success':False,'message':f'Maximum {max_prefs} preferences allowed.'})
        time_map = {}
        for item in selected:
            cr = conn.execute('SELECT * FROM courses WHERE course_id=?', (item['course_id'],)).fetchone()
            if not cr:
                conn.close()
                return jsonify({'success':False,'message':'Invalid course.'})
            key = f"{cr['day']}_{cr['time_slot']}"
            if key in time_map:
                conn.close()
                return jsonify({'success':False,'message':f'Schedule conflict: {cr["course_name"]} overlaps with {time_map[key]}.'})
            time_map[key] = cr['course_name']
        completed = [x.strip().upper() for x in (student['completed_courses'] or '').split(',') if x.strip()]
        for item in selected:
            cr = conn.execute('SELECT * FROM courses WHERE course_id=?', (item['course_id'],)).fetchone()
            prereq = (cr['prerequisite'] or '').strip().upper()
            if prereq and prereq != 'NONE' and prereq not in completed:
                conn.close()
                return jsonify({'success':False,'message':f'Prerequisite not completed: "{cr["prerequisite"]}" is required for {cr["course_name"]}.'})
        conn.execute('DELETE FROM preferences WHERE student_id=?', (session['student_id'],))
        for item in selected:
            conn.execute('INSERT INTO preferences (student_id,course_id,priority_rank) VALUES (?,?,?)',
                (session['student_id'], item['course_id'], item['rank']))
        conn.commit()
        prefs_data = conn.execute('''SELECT p.priority_rank, c.course_name, c.course_code
            FROM preferences p JOIN courses c ON p.course_id=c.course_id
            WHERE p.student_id=? ORDER BY p.priority_rank''', (session['student_id'],)).fetchall()
        conn.close()
        email_preferences_submitted(dict(student), [dict(p) for p in prefs_data], settings)
        return jsonify({'success':True,'message':f'Preferences saved! Confirmation email sent to {student["email"]}'})
    courses = conn.execute('SELECT * FROM courses WHERE is_active=1 ORDER BY department, course_name').fetchall()
    existing_prefs = conn.execute('SELECT p.course_id, p.priority_rank FROM preferences p WHERE p.student_id=? ORDER BY p.priority_rank', (session['student_id'],)).fetchall()
    conn.close()
    return render_template('preferences.html', student=student, courses=courses, existing_prefs=existing_prefs, settings=settings)

@app.route('/my-allocation')
def my_allocation():
    if session.get('role') != 'student': return redirect(url_for('login'))
    conn = get_db()
    student = conn.execute('SELECT * FROM students WHERE id=?', (session['student_id'],)).fetchone()
    allocation = conn.execute('''SELECT a.*, c.course_name, c.course_code, c.department, c.day, c.time_slot, c.venue, c.description
        FROM allocations a JOIN courses c ON a.course_id=c.course_id WHERE a.student_id=?''', (session['student_id'],)).fetchone()
    prefs = conn.execute('''SELECT p.priority_rank, c.course_name, c.course_code
        FROM preferences p JOIN courses c ON p.course_id=c.course_id
        WHERE p.student_id=? ORDER BY p.priority_rank''', (session['student_id'],)).fetchall()
    conn.close()
    return render_template('my_allocation.html', student=student, allocation=allocation, preferences=prefs)

# ─── FEEDBACK ─────────────────────────────────────────────────────────────────────
@app.route('/feedback', methods=['GET','POST'])
def feedback():
    if session.get('role') != 'student': return redirect(url_for('login'))
    conn = get_db()
    student = conn.execute('SELECT * FROM students WHERE id=?', (session['student_id'],)).fetchone()
    allocation = conn.execute('''SELECT a.*, c.course_name, c.course_code, c.department
        FROM allocations a JOIN courses c ON a.course_id=c.course_id
        WHERE a.student_id=? AND a.allocation_status="Allocated"''', (session['student_id'],)).fetchone()
    if not allocation:
        conn.close()
        return render_template('feedback.html', student=student, allocation=None, existing=None)
    existing = conn.execute('SELECT * FROM feedback WHERE student_id=? AND course_id=?',
        (session['student_id'], allocation['course_id'])).fetchone()
    if request.method == 'POST':
        data = request.get_json()
        if existing:
            conn.close()
            return jsonify({'success':False,'message':'You already submitted feedback for this course.'})
        try:
            conn.execute('''INSERT INTO feedback (student_id,course_id,rating,teaching_quality,
                content_quality,difficulty_level,would_recommend,comments) VALUES (?,?,?,?,?,?,?,?)''',
                (session['student_id'], allocation['course_id'],
                 int(data.get('rating',3)), int(data.get('teaching_quality',3)),
                 int(data.get('content_quality',3)), int(data.get('difficulty_level',3)),
                 int(data.get('would_recommend',1)), data.get('comments','').strip()))
            conn.commit(); conn.close()
            return jsonify({'success':True,'message':'Thank you! Your feedback has been submitted.'})
        except Exception as e:
            conn.close()
            return jsonify({'success':False,'message':'Error submitting feedback.'})
    conn.close()
    return render_template('feedback.html', student=student, allocation=allocation, existing=existing)

# ─── CERTIFICATE ──────────────────────────────────────────────────────────────────
@app.route('/certificate')
def certificate():
    if session.get('role') != 'student': return redirect(url_for('login'))
    conn = get_db()
    student = conn.execute('SELECT * FROM students WHERE id=?', (session['student_id'],)).fetchone()
    allocation = conn.execute('''SELECT a.*, c.course_name, c.course_code, c.department, c.day, c.time_slot, c.venue
        FROM allocations a JOIN courses c ON a.course_id=c.course_id
        WHERE a.student_id=? AND a.allocation_status="Allocated"''', (session['student_id'],)).fetchone()
    cert = None
    if allocation:
        cert = conn.execute('SELECT * FROM certificates WHERE student_id=? AND course_id=?',
            (session['student_id'], allocation['course_id'])).fetchone()
    conn.close()
    return render_template('certificate.html', student=student, allocation=allocation, cert=cert)

@app.route('/certificate/generate', methods=['POST'])
def generate_certificate():
    if session.get('role') != 'student': return jsonify({'success':False,'message':'Unauthorized'})
    conn = get_db()
    student = conn.execute('SELECT * FROM students WHERE id=?', (session['student_id'],)).fetchone()
    allocation = conn.execute('''SELECT a.*, c.* FROM allocations a JOIN courses c ON a.course_id=c.course_id
        WHERE a.student_id=? AND a.allocation_status="Allocated"''', (session['student_id'],)).fetchone()
    if not allocation:
        conn.close()
        return jsonify({'success':False,'message':'No allocation found.'})
    existing = conn.execute('SELECT * FROM certificates WHERE student_id=? AND course_id=?',
        (session['student_id'], allocation['course_id'])).fetchone()
    if existing:
        conn.close()
        return jsonify({'success':True,'message':'Certificate ready!','cert_id':existing['certificate_id']})
    cert_id = f"SCAAS-{student['student_id']}-{allocation['course_code']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    conn.execute('INSERT INTO certificates (student_id,course_id,certificate_id) VALUES (?,?,?)',
        (session['student_id'], allocation['course_id'], cert_id))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Certificate generated!','cert_id':cert_id})

@app.route('/certificate/download/<cert_id>')
def download_certificate(cert_id):
    if session.get('role') != 'student': return redirect(url_for('login'))
    conn = get_db()
    cert = conn.execute('SELECT * FROM certificates WHERE certificate_id=? AND student_id=?',
        (cert_id, session['student_id'])).fetchone()
    if not cert: conn.close(); return "Certificate not found", 404
    student = conn.execute('SELECT * FROM students WHERE id=?', (session['student_id'],)).fetchone()
    course = conn.execute('SELECT * FROM courses WHERE course_id=?', (cert['course_id'],)).fetchone()
    settings = get_settings()
    conn.close()
    pdf_buffer = generate_certificate_pdf(dict(student), dict(course), cert_id, settings)
    if pdf_buffer:
        return send_file(pdf_buffer, as_attachment=True,
            download_name=f'Certificate_{student["student_id"]}_{course["course_code"]}.pdf',
            mimetype='application/pdf')
    return "Install reportlab first: pip install reportlab", 500

@app.route('/forgot-password', methods=['GET','POST'])
def forgot_password():
    if request.method == 'POST':
        data = request.get_json()
        email = data.get('email','').strip().lower()
        role  = data.get('role','student')
        conn  = get_db()
        if role == 'admin':
            user = conn.execute('SELECT * FROM admins WHERE email=?', (email,)).fetchone()
        else:
            user = conn.execute('SELECT * FROM students WHERE email=?', (email,)).fetchone()
        if not user:
            conn.close()
            return jsonify({'success':False,'message':'No account found with this email.'})
        import secrets
        token = secrets.token_urlsafe(32)
        expires = datetime.now().replace(microsecond=0)
        from datetime import timedelta
        expires = expires + timedelta(hours=1)
        conn.execute('DELETE FROM password_reset_tokens WHERE email=? AND role=?', (email, role))
        conn.execute('INSERT INTO password_reset_tokens (email,token,role,expires_at) VALUES (?,?,?,?)',
                     (email, token, role, expires))
        conn.commit()
        reset_link = url_for('reset_password', token=token, _external=True)
        content = f'''<p style="color:#374151;">Dear <b>{user["name"]}</b>,</p>
          <p style="color:#374151;">We received a request to reset your SCAAS password.</p>
          <div style="text-align:center;margin:28px 0;">
            <a href="{reset_link}" style="background:#1e40af;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:15px;">Reset My Password →</a>
          </div>
          <p style="color:#6b7280;font-size:13px;">This link expires in <b>1 hour</b>. If you did not request this, ignore this email.</p>'''
        html = build_email_base('#1e40af','🔑','Password Reset Request','SCAAS', content)
        send_email(email, user['name'], '🔑 Reset Your SCAAS Password', html, 'password_reset')
        conn.close()
        return jsonify({'success':True,'message':f'Password reset link sent to {email}. Check your inbox.'})
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET','POST'])
def reset_password(token):
    conn = get_db()
    record = conn.execute(
        'SELECT * FROM password_reset_tokens WHERE token=? AND used=0', (token,)).fetchone()
    if not record or datetime.strptime(str(record['expires_at']), '%Y-%m-%d %H:%M:%S') < datetime.now():
        conn.close()
        return render_template('reset_password.html', error='This reset link has expired or is invalid.', token=token)
    if request.method == 'POST':
        data = request.get_json()
        password = data.get('password','')
        confirm  = data.get('confirm_password','')
        if password != confirm:
            conn.close()
            return jsonify({'success':False,'message':'Passwords do not match.'})
        valid, msg = validate_password(password)
        if not valid:
            conn.close()
            return jsonify({'success':False,'message':msg})
        hashed = hash_password(password)
        if record['role'] == 'admin':
            conn.execute('UPDATE admins SET password=? WHERE email=?', (hashed, record['email']))
        else:
            conn.execute('UPDATE students SET password=? WHERE email=?', (hashed, record['email']))
        conn.execute('UPDATE password_reset_tokens SET used=1 WHERE token=?', (token,))
        conn.commit(); conn.close()
        return jsonify({'success':True,'message':'Password reset successful! You can now login.'})
    conn.close()
    return render_template('reset_password.html', token=token, error=None)

# ─── ADMIN ROUTES ─────────────────────────────────────────────────────────────────
@app.route('/admin')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    conn = get_db()
    total_students = conn.execute('SELECT COUNT(*) as c FROM students').fetchone()['c']
    total_courses = conn.execute('SELECT COUNT(*) as c FROM courses WHERE is_active=1').fetchone()['c']
    total_allocated = conn.execute('SELECT COUNT(*) as c FROM allocations WHERE allocation_status="Allocated"').fetchone()['c']
    total_prefs = conn.execute('SELECT COUNT(DISTINCT student_id) as c FROM preferences').fetchone()['c']
    students = conn.execute('SELECT * FROM students ORDER BY name').fetchall()
    courses = conn.execute('SELECT * FROM courses ORDER BY department, course_name').fetchall()
    allocations = conn.execute('''SELECT a.*, s.name as student_name, s.student_id as sid, s.email,
        s.department as student_dept, c.course_name, c.course_code
        FROM allocations a JOIN students s ON a.student_id=s.id
        LEFT JOIN courses c ON a.course_id=c.course_id ORDER BY a.allocated_at DESC''').fetchall()
    settings = conn.execute('SELECT * FROM allocation_settings WHERE id=1').fetchone()
    email_logs = conn.execute('SELECT * FROM email_log ORDER BY sent_at DESC LIMIT 50').fetchall()
    feedback_summary = conn.execute('''SELECT c.course_name, c.course_code,
        COUNT(f.id) as total_feedback, ROUND(AVG(f.rating),1) as avg_rating
        FROM courses c LEFT JOIN feedback f ON c.course_id=f.course_id
        WHERE c.is_active=1 GROUP BY c.course_id ORDER BY avg_rating DESC NULLS LAST''').fetchall()
    conn.close()
    return render_template('admin_dashboard.html',
        total_students=total_students, total_courses=total_courses,
        total_allocated=total_allocated, total_prefs=total_prefs,
        students=students, courses=courses, allocations=allocations,
        settings=settings, email_logs=email_logs, feedback_summary=feedback_summary)

def times_overlap(slot1, slot2):
    """Returns True if two time slots overlap.
    Supports HH:MM and HH:MM:SS formats e.g. '09:00-10:30' or '09:00:30-10:30:45'"""
    try:
        def to_secs(t):
            parts = t.strip().split(':')
            h = int(parts[0]) if len(parts) > 0 else 0
            m = int(parts[1]) if len(parts) > 1 else 0
            s = int(parts[2]) if len(parts) > 2 else 0
            return h*3600 + m*60 + s
        # slot format: "HH:MM-HH:MM" or "HH:MM:SS-HH:MM:SS"
        # Find the dash that separates start from end (after first colon group)
        def split_slot(slot):
            slot = slot.strip()
            # Find '-' that is not inside a time (i.e., after position 4)
            idx = slot.find('-', 4)
            if idx == -1:
                return None, None
            return slot[:idx], slot[idx+1:]
        s1_str, e1_str = split_slot(slot1)
        s2_str, e2_str = split_slot(slot2)
        if not s1_str or not s2_str:
            return slot1.strip() == slot2.strip()
        s1, e1 = to_secs(s1_str), to_secs(e1_str)
        s2, e2 = to_secs(s2_str), to_secs(e2_str)
        return s1 < e2 and s2 < e1
    except:
        return slot1.strip() == slot2.strip()

def get_venue_conflict(venue, day, time_slot, exclude_course_id=None):
    """Check if any active course has the same venue+day with an overlapping time slot."""
    conn = get_db()
    if exclude_course_id:
        rows = conn.execute(
            'SELECT course_id, course_name, time_slot FROM courses WHERE is_active=1 AND LOWER(TRIM(venue))=LOWER(TRIM(?)) AND day=? AND course_id!=?',
            (venue, day, int(exclude_course_id))).fetchall()
    else:
        rows = conn.execute(
            'SELECT course_id, course_name, time_slot FROM courses WHERE is_active=1 AND LOWER(TRIM(venue))=LOWER(TRIM(?)) AND day=?',
            (venue, day)).fetchall()
    conn.close()
    for row in rows:
        if times_overlap(time_slot, row['time_slot']):
            return row['course_name']
    return None

@app.route('/admin/check-venue-conflict', methods=['POST'])
def check_venue_conflict():
    if session.get('role') != 'admin':
        return jsonify({'conflict': False})
    data      = request.get_json()
    venue     = (data.get('venue') or '').strip()
    day       = (data.get('day') or '').strip()
    time_slot = (data.get('time_slot') or '').strip()
    course_id = data.get('course_id', '')
    if not venue or not day or not time_slot:
        return jsonify({'conflict': False})
    # Warn if TBD — not a real venue
    if venue.upper() == 'TBD':
        return jsonify({'conflict': True, 'course_name': None, 'tbd': True})
    conflict_name = get_venue_conflict(venue, day, time_slot, course_id if course_id else None)
    if conflict_name:
        return jsonify({'conflict': True, 'course_name': conflict_name, 'tbd': False})
    return jsonify({'conflict': False})

@app.route('/admin/course/add', methods=['POST'])
def add_course():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    data = request.get_json()
    if not all([data.get('course_name'), data.get('course_code'), data.get('department'), data.get('seat_capacity')]):
        return jsonify({'success':False,'message':'All fields required.'})
    venue     = (data.get('venue') or '').strip()
    day       = data.get('day','Monday')
    time_slot = data.get('time_slot','')
    # Force admin to enter a real venue
    if not venue or venue.upper() == 'TBD':
        return jsonify({'success':False,'message':'⚠️ Please enter a real Venue / Room Number. "TBD" is not allowed — a specific room must be assigned to every course.'})
    if not time_slot:
        return jsonify({'success':False,'message':'Please set a valid start time and duration.'})
    # Block if venue+day+time overlaps with any existing course
    conflict_name = get_venue_conflict(venue, day, time_slot)
    if conflict_name:
        return jsonify({'success':False,'message':f'⚠️ Venue Conflict: "{venue}" is already booked on {day} at {time_slot} for "{conflict_name}". Please choose a different venue, day, or time.'})
    prereq = (data.get('prerequisite') or '').strip()
    prereq = 'None' if (not prereq or prereq.upper() == 'NONE') else prereq.upper()
    try:
        conn = get_db()
        conn.execute('''INSERT INTO courses (course_name,course_code,department,seat_capacity,seats_remaining,
            prerequisite,day,time_slot,venue,start_date,end_date,description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (data['course_name'].strip(), data['course_code'].strip().upper(), data['department'],
             int(data['seat_capacity']), int(data['seat_capacity']), prereq, day, time_slot,
             venue, data.get('start_date','').strip(), data.get('end_date','').strip(),
             data.get('description','').strip()))
        conn.commit(); conn.close()
        return jsonify({'success':True,'message':f'Course {data["course_code"].upper()} added successfully!'})
    except sqlite3.IntegrityError:
        return jsonify({'success':False,'message':'Course code already exists.'})

@app.route('/admin/course/update', methods=['POST'])
def update_course():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    data      = request.get_json()
    venue     = (data.get('venue') or '').strip()
    day       = data.get('day','Monday')
    time_slot = data.get('time_slot','')
    course_id = int(data['course_id'])
    # Force admin to enter a real venue
    if not venue or venue.upper() == 'TBD':
        return jsonify({'success':False,'message':'⚠️ Please enter a real Venue / Room Number. "TBD" is not allowed — a specific room must be assigned to every course.'})
    if not time_slot:
        return jsonify({'success':False,'message':'Please set a valid start time and duration.'})
    # Block if venue+day+time overlaps with any other course
    conflict_name = get_venue_conflict(venue, day, time_slot, course_id)
    if conflict_name:
        return jsonify({'success':False,'message':f'⚠️ Venue Conflict: "{venue}" is already booked on {day} at {time_slot} for "{conflict_name}". Please choose a different venue, day, or time.'})
    prereq = (data.get('prerequisite') or '').strip()
    prereq = 'None' if (not prereq or prereq.upper() == 'NONE') else prereq.upper()
    conn = get_db()
    conn.execute('''UPDATE courses SET course_name=?,department=?,seat_capacity=?,seats_remaining=?,
        prerequisite=?,day=?,time_slot=?,venue=?,start_date=?,end_date=?,description=?,is_active=? WHERE course_id=?''',
        (data['course_name'], data['department'], int(data['seat_capacity']),
         int(data['seats_remaining']), prereq, day, time_slot, venue,
         data.get('start_date','').strip(), data.get('end_date','').strip(),
         data['description'], int(data.get('is_active',1)), course_id))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Course updated successfully!'})

@app.route('/admin/course/delete', methods=['POST'])
def delete_course():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    data = request.get_json()
    conn = get_db()
    conn.execute('UPDATE courses SET is_active=0 WHERE course_id=?', (data['course_id'],))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Course deactivated.'})

@app.route('/admin/settings/update', methods=['POST'])
def update_settings():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    data = request.get_json()
    conn = get_db()
    conn.execute('''UPDATE allocation_settings SET preference_deadline=?,max_preferences=?,
        allow_modifications=?,university_name=?,semester=?,updated_at=? WHERE id=1''',
        (data.get('preference_deadline') or None, int(data.get('max_preferences',5)),
         int(data.get('allow_modifications',1)), data.get('university_name','University'),
         data.get('semester','Semester 1'), datetime.now()))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Settings updated!'})

@app.route('/admin/email/send-reminders', methods=['POST'])
def send_reminders():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    settings = get_settings()
    deadline = settings.get('preference_deadline') or 'Check portal for deadline'
    conn = get_db()
    students = conn.execute('''SELECT * FROM students WHERE id NOT IN
        (SELECT DISTINCT student_id FROM preferences)''').fetchall()
    conn.close()

    count = sum(1 for s in students if email_deadline_reminder(dict(s), deadline, settings)[0])
    return jsonify({'success':True,'message':f'Reminder sent to {count} students who haven\'t submitted preferences.'})

@app.route('/admin/email/config', methods=['POST'])
def update_email_config():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    data = request.get_json()
    sender_email    = data.get('sender_email','').strip()
    sender_password = data.get('sender_password','').strip()
    sender_name     = data.get('sender_name','SCAAS Course Allocation').strip()
    enabled         = int(data.get('enabled', 0))
    conn = get_db()
    conn.execute('''INSERT OR REPLACE INTO email_config (id, sender_email, sender_password, sender_name, enabled, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)''', (sender_email, sender_password, sender_name, enabled, datetime.now()))
    conn.commit(); conn.close()
    EMAIL_CONFIG['sender_email']    = sender_email
    EMAIL_CONFIG['sender_password'] = sender_password
    EMAIL_CONFIG['sender_name']     = sender_name
    EMAIL_CONFIG['enabled']         = bool(enabled)
    status = 'enabled and ready' if enabled else 'saved (disabled)'
    return jsonify({'success':True,'message':f'Email configuration {status}!'})

@app.route('/admin/email/test', methods=['POST'])
def test_email():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    load_email_config()
    if not EMAIL_CONFIG['enabled'] or not EMAIL_CONFIG['sender_email']:
        return jsonify({'success':False,'message':'Email not configured. Please enter Gmail details and enable email first.'})
    conn = get_db()
    admin = conn.execute('SELECT * FROM admins WHERE id=?', (session['admin_id'],)).fetchone()
    conn.close()
    success, msg = send_email(admin['email'], admin['name'],
        '✅ SCAAS Email Test - Configuration Working',
        '''<div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto;padding:20px;">
        <div style="background:#1e40af;padding:20px;border-radius:8px;text-align:center;margin-bottom:20px;">
            <h2 style="color:#fff;margin:0;">✅ Email is Working!</h2>
        </div>
        <p>Your SCAAS email configuration is correctly set up.</p>
        <p>Students will now receive real emails when:</p>
        <ul style="color:#374151;">
            <li>They submit course preferences</li>
            <li>Admin runs allocation (course, venue, time sent)</li>
            <li>Admin sends deadline reminders</li>
        </ul>
        </div>''', 'test')
    if success:
        return jsonify({'success':True,'message':f'✅ Test email sent to {admin["email"]}! Check your inbox.'})
    return jsonify({'success':False,'message':f'❌ Failed: {msg}. Check your Gmail App Password.'})

@app.route('/admin/run-allocation', methods=['POST'])
def run_allocation():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    conn = get_db()
    conn.execute('DELETE FROM allocations')
    conn.execute('UPDATE courses SET seats_remaining = seat_capacity')
    students = conn.execute('SELECT * FROM students ORDER BY cgpa DESC, created_at ASC').fetchall()
    settings = get_settings()
    results = []
    allocated_count = 0

    for student in students:
        prefs = conn.execute('''SELECT p.priority_rank, c.* FROM preferences p
            JOIN courses c ON p.course_id=c.course_id
            WHERE p.student_id=? AND c.is_active=1 ORDER BY p.priority_rank''', (student['id'],)).fetchall()
        completed = [x.strip().upper() for x in (student['completed_courses'] or '').split(',') if x.strip()]
        allocated = False
        reason = 'No preferences submitted'
        for pref in prefs:
            prereq = (pref['prerequisite'] or '').strip().upper()
            if prereq and prereq != 'NONE' and prereq not in completed:
                reason = f'Prerequisite not met for P{pref["priority_rank"]}'; continue
            seats = conn.execute('SELECT seats_remaining FROM courses WHERE course_id=?', (pref['course_id'],)).fetchone()
            if seats and seats['seats_remaining'] > 0:
                conn.execute('INSERT INTO allocations (student_id,course_id,allocation_status,allocated_at) VALUES (?,?,?,?)',
                    (student['id'], pref['course_id'], 'Allocated', datetime.now()))
                conn.execute('UPDATE courses SET seats_remaining=seats_remaining-1 WHERE course_id=?', (pref['course_id'],))
                results.append({'student':student['name'],'sid':student['student_id'],'course':pref['course_name'],'preference':pref['priority_rank'],'status':'Allocated'})
                allocated = True; allocated_count += 1; break
            else: reason = f'No seats for P{pref["priority_rank"]}'
        if not allocated:
            conn.execute('INSERT INTO allocations (student_id,course_id,allocation_status,allocated_at,notes) VALUES (?,NULL,?,?,?)',
                (student['id'], 'Not Allocated', datetime.now(), reason))
            results.append({'student':student['name'],'sid':student['student_id'],'course':'Not Allocated','preference':'-','status':'Not Allocated','reason':reason})

    conn.execute('UPDATE allocation_settings SET allocation_run=1,allow_modifications=0,updated_at=? WHERE id=1', (datetime.now(),))
    conn.commit()

    # Send allocation emails
    for student in students:
        alloc = conn.execute('''SELECT a.*, c.* FROM allocations a
            LEFT JOIN courses c ON a.course_id=c.course_id WHERE a.student_id=?''', (student['id'],)).fetchone()
        if alloc:
            course_data = dict(alloc) if alloc['allocation_status'] == 'Allocated' else {}
            email_allocation_result(dict(student), course_data, alloc['allocation_status'], settings)
    conn.close()
    return jsonify({'success':True,'message':f'Done! {allocated_count}/{len(students)} allocated. Emails sent!',
                    'results':results,'allocated':allocated_count,'total':len(students)})

@app.route('/admin/reset-allocation', methods=['POST'])
def reset_allocation():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    conn = get_db()
    conn.execute('DELETE FROM allocations')
    conn.execute('UPDATE courses SET seats_remaining=seat_capacity')
    conn.execute('UPDATE allocation_settings SET allocation_run=0,allow_modifications=1 WHERE id=1')
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Allocation reset successfully.'})

@app.route('/admin/manual-override', methods=['POST'])
def manual_override():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    data = request.get_json()
    conn = get_db()
    existing = conn.execute('SELECT * FROM allocations WHERE student_id=? AND allocation_status="Allocated"', (data['student_id'],)).fetchone()
    if existing and existing['course_id']:
        conn.execute('UPDATE courses SET seats_remaining=seats_remaining+1 WHERE course_id=?', (existing['course_id'],))
    conn.execute('DELETE FROM allocations WHERE student_id=?', (data['student_id'],))
    conn.execute('INSERT INTO allocations (student_id,course_id,allocation_status,allocated_at,notes) VALUES (?,?,?,?,?)',
        (data['student_id'], data['course_id'], 'Allocated', datetime.now(), 'Manual override by admin'))
    conn.execute('UPDATE courses SET seats_remaining=seats_remaining-1 WHERE course_id=?', (data['course_id'],))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Manual override applied!'})

@app.route('/admin/student/delete', methods=['POST'])
def delete_student():
    if session.get('role') != 'admin': return jsonify({'success':False,'message':'Unauthorized'})
    data = request.get_json()
    conn = get_db()
    conn.execute('DELETE FROM students WHERE id=?', (data['student_id'],))
    conn.commit(); conn.close()
    return jsonify({'success':True,'message':'Student removed.'})

@app.route('/admin/reports')
def reports():
    if session.get('role') != 'admin': return redirect(url_for('admin_login'))
    conn = get_db()
    enrollment = conn.execute('''SELECT c.course_name, c.course_code, c.department, c.seat_capacity, c.seats_remaining, c.venue,
        (c.seat_capacity-c.seats_remaining) as enrolled,
        ROUND(CAST(c.seat_capacity-c.seats_remaining AS FLOAT)/c.seat_capacity*100,1) as utilization
        FROM courses c WHERE c.is_active=1 ORDER BY enrolled DESC''').fetchall()
    unallocated = conn.execute('''SELECT s.*, a.notes as reason FROM students s
        LEFT JOIN allocations a ON s.id=a.student_id AND a.allocation_status='Not Allocated'
        WHERE s.id NOT IN (SELECT student_id FROM allocations WHERE allocation_status="Allocated")
        ORDER BY s.cgpa DESC''').fetchall()
    popularity = conn.execute('''SELECT c.course_name, c.course_code, c.department,
        COUNT(p.id) as total_requests,
        SUM(CASE WHEN p.priority_rank=1 THEN 1 ELSE 0 END) as first_choice,
        SUM(CASE WHEN p.priority_rank=2 THEN 1 ELSE 0 END) as second_choice,
        SUM(CASE WHEN p.priority_rank=3 THEN 1 ELSE 0 END) as third_choice
        FROM courses c LEFT JOIN preferences p ON c.course_id=p.course_id
        WHERE c.is_active=1 GROUP BY c.course_id ORDER BY total_requests DESC''').fetchall()
    dept_stats = conn.execute('''SELECT s.department, COUNT(DISTINCT s.id) as total_students,
        COUNT(DISTINCT a.student_id) as allocated_students
        FROM students s LEFT JOIN allocations a ON s.id=a.student_id AND a.allocation_status='Allocated'
        GROUP BY s.department ORDER BY total_students DESC''').fetchall()
    feedback_report = conn.execute('''SELECT c.course_name, c.course_code, c.department,
        COUNT(f.id) as total_feedback, ROUND(AVG(f.rating),1) as avg_rating,
        ROUND(AVG(f.teaching_quality),1) as avg_teaching,
        ROUND(AVG(f.content_quality),1) as avg_content,
        ROUND(AVG(f.difficulty_level),1) as avg_difficulty,
        SUM(f.would_recommend) as would_recommend
        FROM courses c LEFT JOIN feedback f ON c.course_id=f.course_id
        WHERE c.is_active=1 GROUP BY c.course_id ORDER BY avg_rating DESC''').fetchall()
    conn.close()
    return render_template('reports.html', enrollment=enrollment, unallocated=unallocated,
        popularity=popularity, dept_stats=dept_stats, feedback_report=feedback_report)

@app.route('/api/course/<int:course_id>')
def api_course(course_id):
    conn = get_db()
    c = conn.execute('SELECT * FROM courses WHERE course_id=?', (course_id,)).fetchone()
    conn.close()
    return jsonify(dict(c)) if c else jsonify({})

if __name__ == '__main__':
    init_db()
    migrate_db()
    load_email_config()   # Load saved email settings from DB on startup
    print("=" * 55)
    print("  SCAAS - Student Course Allocation System")
    print("=" * 55)
    print(f"  Email: {'✅ ENABLED (' + EMAIL_CONFIG['sender_email'] + ')' if EMAIL_CONFIG['enabled'] else '❌ Not configured (go to Admin → Emails)'}")
    print(f"  URL:   http://localhost:5000")
    print("=" * 55)
    app.run(debug=False, host='0.0.0.0', port=5000)
