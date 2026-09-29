import eventlet
eventlet.monkey_patch()

import os
from datetime import datetime, timedelta, date as _dt_date
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, send_from_directory, abort, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Upload folder — use Render's persistent disk if present
RENDER_DISK = '/var/data'
if os.path.isdir(RENDER_DISK):
    UPLOAD_FOLDER = os.path.join(RENDER_DISK, 'uploads')
else:
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'mp4', 'mov'}

app = Flask(__name__)

# Secret key — env var wins, local dev fallback
app.config['SECRET_KEY'] = os.environ.get(
    'SECRET_KEY',
    'change-this-in-production-please'
)

# Database — Postgres via DATABASE_URL on Render, SQLite locally
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    # SQLAlchemy 2.x requires 'postgresql://'
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
if not DATABASE_URL:
    DATABASE_URL = 'sqlite:///' + os.path.join(BASE_DIR, 'academy.db')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# ------------------------------------------------------------------
# IN-MEMORY STATE
# ------------------------------------------------------------------
online_users = {}                   # user_id -> socket sid
active_attendance_sessions = set()  # session_ids currently open for check-in
active_calls = {}                   # caller_id -> {callee_id, call_id, from_name}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ------------------------------------------------------------------
# REAL-TIME NOTIFIERS
# ------------------------------------------------------------------
def notify_student_dashboard(user_id):
    socketio.emit('dashboard_refresh', {}, room=f'user_{user_id}')


def notify_teacher_dashboard():
    socketio.emit('dashboard_refresh', {}, room='teachers')


# ------------------------------------------------------------------
# PENDING-EVENT HELPERS  (for late joiners)
# ------------------------------------------------------------------
def _get_open_attendance_payload():
    """Return the currently-open attendance window, or None."""
    if not active_attendance_sessions:
        return None
    sid = next(iter(active_attendance_sessions))
    sess = db.session.get(TrainingSession, sid)
    if not sess:
        return None
    return {
        'session_id': sess.session_id,
        'week_number': sess.week_number,
        'title': sess.title,
        'date': sess.date.strftime('%b %d, %Y') if sess.date else '',
    }


def _get_incoming_call_for(user_id):
    """Return the ringing call addressed to this user, or None."""
    for caller_id, info in list(active_calls.items()):
        if info['callee_id'] == user_id:
            return {
                'call_id': info['call_id'],
                'from_id': caller_id,
                'from_name': info['from_name'],
            }
    return None


# ------------------------------------------------------------------
# MODELS
# ------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    user_id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='student')
    account_status = db.Column(db.String(20), default='Active')
    display_name = db.Column(db.String(60))
    last_login = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    profile = db.relationship('StudentProfile', backref='user', uselist=False)

    def get_id(self):
        return str(self.user_id)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class StudentProfile(db.Model):
    __tablename__ = 'student_profiles'
    student_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), unique=True)
    full_name = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    profile_photo = db.Column(db.String(255))
    course_id = db.Column(db.Integer, db.ForeignKey('courses.course_id'))
    registration_date = db.Column(db.DateTime, default=datetime.utcnow)
    attendance_misses = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='Active')
    lock_reason = db.Column(db.String(255))


class Course(db.Model):
    __tablename__ = 'courses'
    course_id = db.Column(db.Integer, primary_key=True)
    course_name = db.Column(db.String(200))
    description = db.Column(db.Text)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    status = db.Column(db.String(20), default='Active')


class TrainingSession(db.Model):
    __tablename__ = 'sessions'
    session_id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.course_id'))
    week_number = db.Column(db.Integer)
    session_number = db.Column(db.Integer)
    day_index = db.Column(db.Integer)
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    date = db.Column(db.Date)
    start_time = db.Column(db.String(10))
    end_time = db.Column(db.String(10))
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))


class Attendance(db.Model):
    __tablename__ = 'attendance'
    attendance_id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.session_id'))
    status = db.Column(db.String(20))
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)


class Lesson(db.Model):
    __tablename__ = 'lessons'
    lesson_id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.course_id'))
    week_number = db.Column(db.Integer)
    title = db.Column(db.String(200))
    notes = db.Column(db.Text)
    objectives = db.Column(db.Text)
    teacher_notes = db.Column(db.Text)


class Assignment(db.Model):
    __tablename__ = 'assignments'
    assignment_id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.course_id'))
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.session_id'))
    title = db.Column(db.String(200))
    instructions = db.Column(db.Text)
    deadline = db.Column(db.DateTime)
    maximum_score = db.Column(db.Integer, default=100)
    status = db.Column(db.String(20), default='Published')


class Submission(db.Model):
    __tablename__ = 'submissions'
    submission_id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('assignments.assignment_id'))
    student_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    submission_status = db.Column(db.String(20), default='Submitted')
    file_location = db.Column(db.String(500))
    student_comment = db.Column(db.Text)
    automatic_score = db.Column(db.Integer)
    teacher_score = db.Column(db.Integer)
    final_score = db.Column(db.Integer)
    teacher_feedback = db.Column(db.Text)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    reviewed_at = db.Column(db.DateTime)

    assignment = db.relationship('Assignment', backref='submissions')
    student = db.relationship('User', foreign_keys=[student_id], backref='submissions')
    reviewer = db.relationship('User', foreign_keys=[reviewed_by], backref='reviews')


class ReactivationLog(db.Model):
    __tablename__ = 'reactivation_logs'
    log_id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    previous_status = db.Column(db.String(20))
    new_status = db.Column(db.String(20))
    reason = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(255))


class Conversation(db.Model):
    __tablename__ = 'conversations'
    conversation_id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('User', foreign_keys=[student_id])
    teacher = db.relationship('User', foreign_keys=[teacher_id])
    messages = db.relationship('Message', backref='conversation',
                               cascade='all, delete-orphan',
                               order_by='Message.sent_at')


class Message(db.Model):
    __tablename__ = 'messages'
    message_id      = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversations.conversation_id'))
    sender_id       = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    body            = db.Column(db.Text)
    media_url       = db.Column(db.String(500))
    media_type      = db.Column(db.String(20))
    sent_at         = db.Column(db.DateTime, default=datetime.utcnow)
    read_at         = db.Column(db.DateTime)

    sender = db.relationship('User', foreign_keys=[sender_id])


class CallLog(db.Model):
    __tablename__ = 'call_logs'
    call_id = db.Column(db.Integer, primary_key=True)
    caller_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    callee_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime)
    duration = db.Column(db.Integer)
    status = db.Column(db.String(20))


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def get_setting(key, default=None):
    s = db.session.get(SystemSetting, key)
    return s.value if s else default


def set_setting(key, value):
    s = db.session.get(SystemSetting, key)
    if s:
        s.value = str(value)
    else:
        db.session.add(SystemSetting(key=key, value=str(value)))
    db.session.commit()


def auto_score_submission(submission):
    base = 70
    if submission.file_location:
        base += 10
    if submission.student_comment:
        base += 10
    return min(base, 100)


def check_attendance_lock(student_user):
    max_abs = int(get_setting('max_absences', 2))
    profile = student_user.profile
    if not profile:
        return
    absences = Attendance.query.filter_by(
        student_id=student_user.user_id, status='Absent'
    ).count()
    profile.attendance_misses = absences
    if absences >= max_abs and student_user.account_status == 'Active':
        student_user.account_status = 'Locked'
        profile.status = 'Locked'
        profile.lock_reason = f'Attendance threshold reached ({absences} absences)'
        db.session.commit()


# ------------------------------------------------------------------
# ATTENDANCE MATH
# ------------------------------------------------------------------
def compute_student_stats(user):
    profile = user.profile
    course = db.session.get(Course, profile.course_id) if profile and profile.course_id else None

    total_sessions_scheduled = 0
    sessions_held = 0
    present = 0
    attendance_pct = 100

    if course:
        all_sessions = (TrainingSession.query
                        .filter_by(course_id=course.course_id)
                        .all())
        total_sessions_scheduled = len(all_sessions)

        today = _dt_date.today()
        held_ids = [s.session_id for s in all_sessions if s.date <= today]
        sessions_held = len(held_ids)

        if held_ids:
            present = (Attendance.query
                       .filter_by(student_id=user.user_id, status='Present')
                       .filter(Attendance.session_id.in_(held_ids))
                       .count())

        attendance_pct = int((present / sessions_held) * 100) if sessions_held else 100

    total_assignments = int(get_setting('assignments_total', 6))
    submissions = Submission.query.filter_by(student_id=user.user_id).all()
    submitted_ids = {s.assignment_id for s in submissions}
    scored = [s.final_score for s in submissions if s.final_score is not None]
    avg_score = int(sum(scored) / len(scored)) if scored else 0

    attendance_component = (
        (present / total_sessions_scheduled) * 100
        if total_sessions_scheduled else 0
    )
    submission_component = (
        (len(submitted_ids) / total_assignments) * 100
        if total_assignments else 0
    )
    score_component = avg_score

    progress = int(
        attendance_component * 0.4 +
        submission_component * 0.4 +
        score_component      * 0.2
    )
    progress = max(0, min(100, progress))

    return {
        'course': course,
        'total_sessions': sessions_held,
        'present': present,
        'attendance_pct': attendance_pct,
        'avg_score': avg_score,
        'progress': progress,
    }


# ------------------------------------------------------------------
# AUTH ROUTES
# ------------------------------------------------------------------
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email        = request.form['email'].strip().lower()
        password     = request.form['password']
        full_name    = request.form['full_name'].strip()
        display_name = request.form.get('display_name', '').strip() or full_name
        phone        = request.form.get('phone', '').strip()

        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('register'))

        user = User(
            email=email,
            role='student',
            account_status='Active',
            display_name=display_name,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        course = Course.query.filter_by(status='Active').first()
        profile = StudentProfile(
            user_id=user.user_id,
            full_name=full_name,
            phone=phone,
            course_id=course.course_id if course else None,
        )
        db.session.add(profile)
        db.session.flush()
        grant_week_one_attendance(user)
        db.session.commit()

        notify_teacher_dashboard()
        flash('Account created. Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        user = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            flash('Invalid email or password.', 'danger')
            return redirect(url_for('login'))

        user.last_login = datetime.utcnow()
        db.session.commit()
        login_user(user)

        if not user.display_name:
            return redirect(url_for('set_display_name'))

        if user.role == 'student' and user.account_status == 'Locked':
            return redirect(url_for('locked'))

        return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/set-display-name', methods=['GET', 'POST'])
@login_required
def set_display_name():
    if request.method == 'POST':
        name = request.form.get('display_name', '').strip()
        if not name:
            flash('Please enter a display name.', 'danger')
            return redirect(url_for('set_display_name'))
        current_user.display_name = name[:40]
        db.session.commit()
        flash('Display name saved.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('set_display_name.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/locked')
@login_required
def locked():
    return render_template('locked.html', user=current_user)


@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'student':
        if current_user.account_status == 'Locked':
            return redirect(url_for('locked'))
        return student_dashboard()
    elif current_user.role == 'teacher':
        return redirect(url_for('teacher_dashboard'))
    else:
        return redirect(url_for('admin_dashboard'))


# ------------------------------------------------------------------
# STUDENT VIEWS
# ------------------------------------------------------------------
def student_dashboard():
    stats = compute_student_stats(current_user)
    course = stats['course']
    profile = current_user.profile

    assignments = Assignment.query.filter_by(
        course_id=course.course_id, status='Published'
    ).all() if course else []

    submissions = {s.assignment_id: s for s in Submission.query.filter_by(
        student_id=current_user.user_id
    ).all()}

    lessons = Lesson.query.filter_by(course_id=course.course_id).all() if course else []

    return render_template(
        'student_dashboard.html',
        profile=profile,
        course=course,
        attendance_pct=stats['attendance_pct'],
        present=stats['present'],
        total_sessions=stats['total_sessions'],
        avg_score=stats['avg_score'],
        progress=stats['progress'],
        assignments=assignments,
        submissions=submissions,
        lessons=lessons,
    )


def grant_week_one_attendance(user):
    profile = user.profile
    if not profile or not profile.course_id:
        return

    week1_sessions = (TrainingSession.query
                      .filter_by(course_id=profile.course_id, week_number=1)
                      .all())

    for sess in week1_sessions:
        existing = Attendance.query.filter_by(
            student_id=user.user_id, session_id=sess.session_id
        ).first()
        if not existing:
            db.session.add(Attendance(
                student_id=user.user_id,
                session_id=sess.session_id,
                status='Present',
                recorded_by=user.user_id,
                recorded_at=datetime.utcnow(),
            ))
    db.session.commit()


@app.route('/api/my-attendance')
@login_required
@role_required('student')
def api_my_attendance():
    stats = compute_student_stats(current_user)
    return jsonify({
        'attendance_pct': stats['attendance_pct'],
        'present': stats['present'],
        'total_sessions': stats['total_sessions'],
        'progress': stats['progress'],
        'avg_score': stats['avg_score'],
    })


# ------------------------------------------------------------------
# PENDING EVENTS API  (for late joiners)
# ------------------------------------------------------------------
@app.route('/api/pending-events')
@login_required
def api_pending_events():
    """
    Returns events a client should replay on page load:
      - attendance: currently-open attendance window (students only)
      - incoming_call: a ringing call addressed to this user
    """
    result = {'attendance': None, 'incoming_call': None}

    if current_user.role == 'student':
        payload = _get_open_attendance_payload()
        if payload:
            result['attendance'] = payload

    call = _get_incoming_call_for(current_user.user_id)
    if call:
        result['incoming_call'] = call

    return jsonify(result)


# ------------------------------------------------------------------
# TEACHER STATS API
# ------------------------------------------------------------------
@app.route('/api/teacher-stats')
@login_required
@role_required('teacher', 'admin')
def api_teacher_stats():
    students = (User.query
                .join(StudentProfile, User.user_id == StudentProfile.user_id)
                .filter(User.role == 'student')
                .filter(~User.email.like('%@bmk.com'))
                .all())

    active = [s for s in students if s.account_status == 'Active']
    locked = [s for s in students if s.account_status == 'Locked']

    submissions = Submission.query.all()
    pending = [s for s in submissions if s.submission_status in ('Submitted', 'Late')]

    today = _dt_date.today()
    current_session = TrainingSession.query.filter_by(date=today).first()

    if not current_session:
        current_session = (TrainingSession.query
                           .filter(TrainingSession.date >= today)
                           .order_by(TrainingSession.date.asc())
                           .first())
    if not current_session:
        current_session = (TrainingSession.query
                           .order_by(TrainingSession.date.desc())
                           .first())

    session_index = 0
    present_today = 0
    absent_today = 0
    if current_session:
        session_index = (TrainingSession.query
                         .filter(TrainingSession.course_id == current_session.course_id)
                         .filter(TrainingSession.date <= current_session.date)
                         .count())
        rows = Attendance.query.filter_by(session_id=current_session.session_id).all()
        present_today = sum(1 for a in rows if a.status == 'Present')
        absent_today  = sum(1 for a in rows if a.status == 'Absent')

    return jsonify({
        'total': len(students),
        'active_count': len(active),
        'locked_count': len(locked),
        'submitted_count': len(submissions),
        'pending_count': len(pending),
        'present_today': present_today,
        'absent_today': absent_today,
        'session_index': session_index,
        'current_session': {
            'week_number': current_session.week_number,
            'day_index': current_session.day_index,
        } if current_session else None,
    })


@app.route('/submit/<int:assignment_id>', methods=['POST'])
@login_required
@role_required('student')
def submit_assignment(assignment_id):
    if current_user.account_status == 'Locked':
        return redirect(url_for('locked'))

    assignment = db.session.get(Assignment, assignment_id) or abort(404)
    file = request.files.get('file')
    comment = request.form.get('comment', '')

    existing = Submission.query.filter_by(
        assignment_id=assignment_id, student_id=current_user.user_id
    ).first()

    filepath = existing.file_location if existing else None
    if file and file.filename and allowed_file(file.filename):
        fname = secure_filename(f"{current_user.user_id}_{assignment_id}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
        filepath = fname

    status = 'Submitted'
    if assignment.deadline and datetime.utcnow() > assignment.deadline:
        status = 'Late'

    if existing:
        existing.file_location = filepath
        existing.student_comment = comment
        existing.submitted_at = datetime.utcnow()
        existing.submission_status = status
        existing.automatic_score = auto_score_submission(existing)
    else:
        sub = Submission(
            assignment_id=assignment_id,
            student_id=current_user.user_id,
            file_location=filepath,
            student_comment=comment,
            submission_status=status,
            automatic_score=80 if filepath else 60
        )
        db.session.add(sub)
    db.session.commit()

    notify_teacher_dashboard()
    notify_student_dashboard(current_user.user_id)

    flash('Assignment submitted successfully.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/attendance/self-mark', methods=['POST'])
@login_required
@role_required('student')
def self_mark_attendance():
    if current_user.account_status == 'Locked':
        return jsonify({'ok': False, 'error': 'Account locked'}), 403

    session_id = int((request.json or {}).get('session_id') or 0)
    sess = db.session.get(TrainingSession, session_id)
    if not sess:
        return jsonify({'ok': False, 'error': 'Invalid session'}), 400

    existing = Attendance.query.filter_by(
        student_id=current_user.user_id, session_id=session_id
    ).first()
    if existing:
        return jsonify({'ok': True, 'status': existing.status, 'already': True})

    record = Attendance(
        student_id=current_user.user_id,
        session_id=session_id,
        status='Present',
        recorded_by=current_user.user_id,
        recorded_at=datetime.utcnow(),
    )
    db.session.add(record)
    db.session.commit()

    socketio.emit('attendance_marked', {
        'student_id': current_user.user_id,
        'student_name': current_user.display_name or (
            current_user.profile.full_name if current_user.profile else current_user.email
        ),
        'student_email': current_user.email,
        'session_id': session_id,
        'status': 'Present',
        'marked_at': datetime.utcnow().strftime('%H:%M:%S'),
    }, room='teachers')

    socketio.emit('attendance_confirmed', {
        'session_id': session_id,
        'status': 'Present',
    }, room=f'user_{current_user.user_id}')

    notify_teacher_dashboard()
    notify_student_dashboard(current_user.user_id)

    return jsonify({'ok': True, 'status': 'Present'})


# ------------------------------------------------------------------
# TEACHER / ADMIN VIEWS
# ------------------------------------------------------------------
@app.route('/teacher')
@login_required
@role_required('teacher', 'admin')
def teacher_dashboard():
    students = (User.query
                .join(StudentProfile, User.user_id == StudentProfile.user_id)
                .filter(User.role == 'student')
                .filter(~User.email.like('%@bmk.com'))
                .all())

    active = [s for s in students if s.account_status == 'Active']
    locked = [s for s in students if s.account_status == 'Locked']

    submissions = Submission.query.all()
    pending = [s for s in submissions if s.submission_status in ('Submitted', 'Late')]

    today = _dt_date.today()
    current_session = TrainingSession.query.filter_by(date=today).first()

    if not current_session:
        current_session = (TrainingSession.query
                           .filter(TrainingSession.date >= today)
                           .order_by(TrainingSession.date.asc())
                           .first())

    if not current_session:
        current_session = (TrainingSession.query
                           .order_by(TrainingSession.date.desc())
                           .first())

    session_index = 0
    present_today = 0
    absent_today  = 0

    if current_session:
        session_index = (TrainingSession.query
                         .filter(TrainingSession.course_id == current_session.course_id)
                         .filter(TrainingSession.date <= current_session.date)
                         .count())

        rows = Attendance.query.filter_by(session_id=current_session.session_id).all()
        present_today = sum(1 for a in rows if a.status == 'Present')
        absent_today  = sum(1 for a in rows if a.status == 'Absent')

    sessions = TrainingSession.query.order_by(TrainingSession.week_number).all()

    return render_template(
        'teacher_dashboard.html',
        total=len(students),
        active_count=len(active),
        locked_count=len(locked),
        locked_students=locked,
        submitted_count=len(submissions),
        pending_count=len(pending),
        present_today=present_today,
        absent_today=absent_today,
        pending_submissions=pending[:10],
        sessions=sessions,
        attendance_days_per_week=int(get_setting('attendance_days_per_week', 2)),
        course_weeks=int(get_setting('course_weeks', 6)),
        session_index=session_index,
        current_session=current_session,
    )


@app.route('/teacher/attendance-settings', methods=['POST'])
@login_required
@role_required('teacher', 'admin')
def update_attendance_settings():
    days  = request.form.get('attendance_days_per_week', '2')
    weeks = request.form.get('course_weeks', '6')

    try:
        days = max(1, min(7, int(days)))
    except (ValueError, TypeError):
        days = 2

    try:
        weeks = max(1, min(52, int(weeks)))
    except (ValueError, TypeError):
        weeks = 6

    set_setting('attendance_days_per_week', days)
    set_setting('course_weeks', weeks)

    flash(f'Attendance set to {days} day(s) per week for {weeks} weeks.', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/attendance/<int:session_id>', methods=['GET', 'POST'])
@login_required
@role_required('teacher', 'admin')
def take_attendance(session_id):
    sess = db.session.get(TrainingSession, session_id) or abort(404)
    students = (User.query
                .join(StudentProfile, User.user_id == StudentProfile.user_id)
                .filter(User.role == 'student')
                .filter(~User.email.like('%@bmk.com'))
                .all())

    if request.method == 'POST':
        for student in students:
            status = request.form.get(f'status_{student.user_id}')
            if not status:
                continue
            record = Attendance.query.filter_by(
                student_id=student.user_id, session_id=session_id
            ).first()
            if record:
                record.status = status
                record.recorded_by = current_user.user_id
                record.recorded_at = datetime.utcnow()
            else:
                db.session.add(Attendance(
                    student_id=student.user_id,
                    session_id=session_id,
                    status=status,
                    recorded_by=current_user.user_id
                ))
        db.session.commit()

        newly_locked = []
        for student in students:
            before = student.account_status
            check_attendance_lock(student)
            if student.account_status == 'Locked' and before == 'Active':
                newly_locked.append(student)

        for s in newly_locked:
            socketio.emit('student_locked', {
                'user_id': s.user_id,
                'full_name': s.profile.full_name if s.profile else s.email,
                'reason': s.profile.lock_reason if s.profile else '',
            }, room='teachers')
            socketio.emit('account_locked', {}, room=f'user_{s.user_id}')

        notify_teacher_dashboard()
        for s in students:
            notify_student_dashboard(s.user_id)

        flash('Attendance saved.', 'success')
        return redirect(url_for('teacher_dashboard'))

    existing = {a.student_id: a.status
                for a in Attendance.query.filter_by(session_id=session_id).all()}
    records = Attendance.query.filter_by(session_id=session_id).all()
    user_map = {u.user_id: u for u in students}

    return render_template('attendance.html',
                           session=sess,
                           students=students,
                           existing=existing,
                           records=records,
                           user_map=user_map)


@app.route('/teacher/reactivate/<int:student_id>', methods=['POST'])
@login_required
@role_required('teacher', 'admin')
def reactivate_student(student_id):
    student = db.session.get(User, student_id) or abort(404)
    reason = request.form.get('reason', 'Teacher reactivation')

    previous = student.account_status
    student.account_status = 'Active'
    if student.profile:
        student.profile.status = 'Active'
        student.profile.lock_reason = None
        student.profile.attendance_misses = 0

    db.session.add(ReactivationLog(
        student_id=student_id, teacher_id=current_user.user_id,
        previous_status=previous, new_status='Active', reason=reason
    ))
    db.session.commit()

    notify_teacher_dashboard()
    notify_student_dashboard(student_id)

    name = student.profile.full_name if student.profile else student.email
    flash(f'{name} reactivated.', 'success')
    return redirect(url_for('teacher_dashboard'))


@app.route('/teacher/submission/<int:submission_id>', methods=['GET', 'POST'])
@login_required
@role_required('teacher', 'admin')
def review_submission(submission_id):
    sub = db.session.get(Submission, submission_id) or abort(404)

    if request.method == 'POST':
        teacher_score = int(request.form.get('teacher_score') or sub.automatic_score or 0)
        feedback = request.form.get('feedback', '')
        sub.teacher_score = teacher_score
        sub.final_score = teacher_score
        sub.teacher_feedback = feedback
        sub.reviewed_by = current_user.user_id
        sub.reviewed_at = datetime.utcnow()
        sub.submission_status = 'Reviewed'
        db.session.commit()

        notify_teacher_dashboard()
        notify_student_dashboard(sub.student_id)

        flash('Submission reviewed.', 'success')
        return redirect(url_for('teacher_dashboard'))

    return render_template('review_submission.html', sub=sub)


@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ------------------------------------------------------------------
# ADMIN
# ------------------------------------------------------------------
@app.route('/admin')
@login_required
@role_required('admin')
def admin_dashboard():
    return render_template(
        'admin_dashboard.html',
        max_absences=get_setting('max_absences', 2),
        late_penalty=get_setting('late_penalty', '10%'),
        attendance_days_per_week=get_setting('attendance_days_per_week', 2),
        course_weeks=get_setting('course_weeks', 6),
        assignments_total=get_setting('assignments_total', 6),
    )


@app.route('/admin/settings', methods=['POST'])
@login_required
@role_required('admin')
def update_settings():
    set_setting('max_absences', request.form.get('max_absences', 2))
    set_setting('late_penalty', request.form.get('late_penalty', '10%'))
    set_setting('attendance_days_per_week',
                request.form.get('attendance_days_per_week', 2))
    set_setting('course_weeks', request.form.get('course_weeks', 6))
    set_setting('assignments_total',
                request.form.get('assignments_total', 6))
    flash('Settings updated.', 'success')
    return redirect(url_for('admin_dashboard'))


# ------------------------------------------------------------------
# CHAT
# ------------------------------------------------------------------
@app.route('/chat')
@login_required
def chat_home():
    if current_user.role == 'student':
        teachers = User.query.filter_by(role='teacher').all()
        for t in teachers:
            existing = Conversation.query.filter_by(
                student_id=current_user.user_id, teacher_id=t.user_id
            ).first()
            if not existing:
                db.session.add(Conversation(
                    student_id=current_user.user_id, teacher_id=t.user_id
                ))
        db.session.commit()

        convos = (Conversation.query
                  .filter_by(student_id=current_user.user_id)
                  .order_by(Conversation.last_message_at.desc())
                  .all())
    else:
        convos = (Conversation.query
                  .filter_by(teacher_id=current_user.user_id)
                  .order_by(Conversation.last_message_at.desc())
                  .all())

    return render_template('chat_home.html', convos=convos)


@app.route('/chat/<int:conversation_id>')
@login_required
def chat_thread(conversation_id):
    convo = db.session.get(Conversation, conversation_id) or abort(404)

    if current_user.user_id not in (convo.student_id, convo.teacher_id):
        abort(403)

    (Message.query
        .filter_by(conversation_id=conversation_id, read_at=None)
        .filter(Message.sender_id != current_user.user_id)
        .update({'read_at': datetime.utcnow()}))
    db.session.commit()

    return render_template('chat_thread.html', convo=convo, messages=convo.messages)


@app.route('/chat/<int:conversation_id>/send', methods=['POST'])
@login_required
def send_message(conversation_id):
    convo = db.session.get(Conversation, conversation_id) or abort(404)
    if current_user.user_id not in (convo.student_id, convo.teacher_id):
        abort(403)

    body = request.form.get('body', '').strip()
    file = request.files.get('file')

    media_url = None
    media_type = None

    if file and file.filename:
        if not allowed_file(file.filename):
            return jsonify({'ok': False, 'error': 'File type not allowed'}), 400

        ext = file.filename.rsplit('.', 1)[1].lower()
        if ext in {'png', 'jpg', 'jpeg', 'gif'}:
            media_type = 'image'
        elif ext in {'mp4', 'mov'}:
            media_type = 'video'

        if not media_type:
            return jsonify({'ok': False, 'error': 'Only images and videos allowed'}), 400

        fname = secure_filename(
            f"chat_{conversation_id}_{current_user.user_id}_{int(datetime.utcnow().timestamp())}_{file.filename}"
        )
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
        media_url = fname

    if not body and not media_url:
        return jsonify({'ok': False, 'error': 'Empty message'}), 400

    msg = Message(
        conversation_id=conversation_id,
        sender_id=current_user.user_id,
        body=body,
        media_url=media_url,
        media_type=media_type,
    )
    convo.last_message_at = datetime.utcnow()
    db.session.add(msg)
    db.session.commit()

    other = convo.teacher_id if current_user.user_id == convo.student_id else convo.student_id

    payload = {
        'conversation_id': conversation_id,
        'message_id': msg.message_id,
        'sender_id': msg.sender_id,
        'sender_name': current_user.display_name or (
            current_user.profile.full_name if current_user.profile else current_user.email
        ),
        'sender_role': current_user.role,
        'body': msg.body,
        'media_url': msg.media_url,
        'media_type': msg.media_type,
        'sent_at': msg.sent_at.strftime('%H:%M'),
    }

    socketio.emit('new_message', payload, room=f'user_{other}')
    socketio.emit('new_message', payload, room=f'user_{current_user.user_id}')

    return jsonify({'ok': True, 'message_id': msg.message_id})

@app.route('/healthz')
def healthz():
    from sqlalchemy import inspect
    import traceback

    # 1) What DB is in use?
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    scheme = uri.split('://')[0] if '://' in uri else 'unknown'

    # 2) Can we talk to it?
    db_ok = False
    db_err = None
    tables = []
    try:
        with app.app_context():
            db.session.execute(db.text('SELECT 1'))
            insp = inspect(db.engine)
            tables = insp.get_table_names()
        db_ok = True
    except Exception as e:
        db_err = ''.join(traceback.format_exception_only(type(e), e)).strip()

    # 3) Is SECRET_KEY set?
    secret_set = app.config['SECRET_KEY'] != 'change-this-in-production-please'

    return jsonify({
        'db_scheme': scheme,
        'db_ok': db_ok,
        'db_error': db_err,
        'tables': tables,
        'secret_set': secret_set,
    })


# ------------------------------------------------------------------
# SOCKETIO
# ------------------------------------------------------------------
def _online_payload():
    payload = []
    for uid in list(online_users.keys()):
        u = db.session.get(User, uid)
        if not u:
            continue
        payload.append({
            'user_id': u.user_id,
            'name': u.display_name or (u.profile.full_name if u.profile else u.email),
            'role': u.role,
        })
    return payload


@socketio.on('connect')
def on_connect():
    if current_user.is_authenticated:
        online_users[current_user.user_id] = request.sid
        join_room(f'user_{current_user.user_id}')

        if current_user.role == 'student':
            join_room('students')
        elif current_user.role in ('teacher', 'admin'):
            join_room('teachers')

        socketio.emit('online_users', _online_payload(), broadcast=True)


@socketio.on('disconnect')
def on_disconnect():
    if current_user.is_authenticated:
        online_users.pop(current_user.user_id, None)
        socketio.emit('online_users', _online_payload(), broadcast=True)


@socketio.on('request_online_users')
def handle_request_online_users():
    if current_user.is_authenticated:
        emit('online_users', _online_payload())


@socketio.on('start_attendance')
def handle_start_attendance(data):
    if current_user.role not in ('teacher', 'admin'):
        emit('attendance_started', {'error': 'Not allowed'})
        return

    today = _dt_date.today()
    sess = TrainingSession.query.filter_by(date=today).first()
    if not sess:
        sess = (TrainingSession.query
                .filter(TrainingSession.date >= today)
                .order_by(TrainingSession.date.asc())
                .first())
    if not sess:
        sess = (TrainingSession.query
                .order_by(TrainingSession.date.desc())
                .first())
    if not sess:
        emit('attendance_started', {'error': 'No sessions defined.'})
        return

    active_attendance_sessions.add(sess.session_id)

    teacher_name = current_user.display_name or (
        current_user.profile.full_name if current_user.profile else current_user.email
    )

    payload = {
        'session_id': sess.session_id,
        'week_number': sess.week_number,
        'title': sess.title,
        'date': sess.date.strftime('%b %d, %Y') if sess.date else '',
        'teacher_name': teacher_name,
    }

    emit('attendance_started', payload)
    socketio.emit('attendance_requested', payload, room='students')


@socketio.on('end_attendance')
def handle_end_attendance(data):
    if current_user.role not in ('teacher', 'admin'):
        return
    sid = int(data.get('session_id') or 0)
    active_attendance_sessions.discard(sid)
    emit('attendance_closed', {'session_id': sid})


@socketio.on('call_user')
def handle_call(data):
    if current_user.role not in ('teacher', 'admin'):
        emit('call_failed', {'reason': 'Only teachers can start calls.'})
        return

    callee_id = int(data['callee_id'])
    if callee_id not in online_users:
        emit('call_failed', {'reason': 'User is offline'})
        return

    log = CallLog(caller_id=current_user.user_id, callee_id=callee_id, status='ringing')
    db.session.add(log)
    db.session.commit()

    caller_name = current_user.display_name or (
        current_user.profile.full_name if current_user.profile else current_user.email
    )

    # Track ringing call so late joiners can find it
    active_calls[current_user.user_id] = {
        'callee_id': callee_id,
        'call_id': log.call_id,
        'from_name': caller_name,
    }

    emit('incoming_call', {
        'call_id': log.call_id,
        'from_id': current_user.user_id,
        'from_name': caller_name,
    }, room=f'user_{callee_id}')


@socketio.on('call_accepted')
def handle_accept(data):
    caller_id = int(data['caller_id'])
    emit('call_accepted', {'by_id': current_user.user_id}, room=f'user_{caller_id}')


@socketio.on('call_declined')
def handle_decline(data):
    caller_id = int(data['caller_id'])
    log = db.session.get(CallLog, data.get('call_id'))
    if log:
        log.status = 'declined'
        log.ended_at = datetime.utcnow()
        db.session.commit()
    active_calls.pop(caller_id, None)
    emit('call_declined', {'by_id': current_user.user_id}, room=f'user_{caller_id}')


@socketio.on('call_ended')
def handle_end(data):
    other_id = int(data['other_id'])
    log = db.session.get(CallLog, data.get('call_id'))
    if log:
        log.status = 'completed'
        log.ended_at = datetime.utcnow()
        if log.started_at:
            log.duration = int((log.ended_at - log.started_at).total_seconds())
        db.session.commit()

    active_calls.pop(current_user.user_id, None)
    active_calls.pop(other_id, None)

    emit('call_ended', {'by_id': current_user.user_id}, room=f'user_{other_id}')


@socketio.on('webrtc_offer')
def webrtc_offer(data):
    emit('webrtc_offer', {'from_id': current_user.user_id, 'sdp': data['sdp']},
         room=f"user_{data['to_id']}")


@socketio.on('webrtc_answer')
def webrtc_answer(data):
    emit('webrtc_answer', {'from_id': current_user.user_id, 'sdp': data['sdp']},
         room=f"user_{data['to_id']}")


@socketio.on('webrtc_ice')
def webrtc_ice(data):
    emit('webrtc_ice', {'from_id': current_user.user_id, 'candidate': data['candidate']},
         room=f"user_{data['to_id']}")


# ------------------------------------------------------------------
# SEED
# ------------------------------------------------------------------
def seed():
    if User.query.first():
        return

    admin = User(email='admin@bmk.com', role='admin', display_name='Admin')
    admin.set_password('admin123')
    db.session.add(admin)

    teacher = User(email='teacher@bmk.com', role='teacher', display_name='Mr. Konceptz')
    teacher.set_password('teacher123')
    db.session.add(teacher)
    db.session.flush()

    course = Course(
        course_name='BM_Konceptz 6-Week Photography Training — Batch 01',
        description='Foundational photography training',
        start_date=datetime(2026, 9, 22).date(),
        end_date=datetime(2026, 10, 27).date(),
        teacher_id=teacher.user_id,
        status='Active'
    )
    db.session.add(course)
    db.session.flush()

    ATTENDANCE_DAYS = 2
    COURSE_WEEKS = 6

    week_topics = [
        (1, 'Introduction & Exposure'),
        (2, 'Composition'),
        (3, 'Lighting'),
        (4, 'Portrait Photography'),
        (5, 'Event Photography'),
        (6, 'Final Practical'),
    ]

    start = datetime(2026, 9, 22).date()
    for wk, title in week_topics:
        for day_idx in range(1, ATTENDANCE_DAYS + 1):
            offset = (wk - 1) * 7 + (day_idx - 1) * (7 // ATTENDANCE_DAYS)
            d = start + timedelta(days=offset)
            db.session.add(TrainingSession(
                course_id=course.course_id,
                week_number=wk,
                session_number=day_idx,
                day_index=day_idx,
                title=f'{title} (Day {day_idx})',
                date=d,
                start_time='10:00',
                end_time='13:00',
                teacher_id=teacher.user_id,
            ))
    db.session.flush()

    db.session.add(Assignment(
        course_id=course.course_id,
        session_id=1,
        title='See the Difference',
        instructions='Submit 10 photographs demonstrating exposure control.',
        deadline=datetime(2026, 9, 29, 23, 59),
        maximum_score=100,
        status='Published'
    ))

    db.session.add(Lesson(
        course_id=course.course_id, week_number=1,
        title='Understanding Your Camera',
        notes='Camera types, parts, exposure triangle, ISO, aperture, shutter speed.',
        objectives='Identify parts of a camera and explain exposure.'
    ))

    for name, email in [('John Doe', 'john@bmk.com'),
                        ('Mary Jane', 'mary@bmk.com'),
                        ('Peter Obi', 'peter@bmk.com')]:
        u = User(email=email, role='student', display_name=name.split()[0])
        u.set_password('student123')
        db.session.add(u)
        db.session.flush()
        db.session.add(StudentProfile(
            user_id=u.user_id, full_name=name, phone='0800000000',
            course_id=course.course_id
        ))

    db.session.add(SystemSetting(key='max_absences', value='2'))
    db.session.add(SystemSetting(key='late_penalty', value='10%'))
    db.session.add(SystemSetting(key='attendance_days_per_week', value=str(ATTENDANCE_DAYS)))
    db.session.add(SystemSetting(key='course_weeks', value=str(COURSE_WEEKS)))
    db.session.add(SystemSetting(key='assignments_total', value='6'))

    db.session.commit()
    print('Database seeded. Admin: admin@bmk.com / admin123')
    print('Teacher: teacher@bmk.com / teacher123')
    print('Student: john@bmk.com / student123')


# ------------------------------------------------------------------
# BOOTSTRAP — runs under gunicorn AND `python app.py`
# ------------------------------------------------------------------
with app.app_context():
    db.create_all()
    seed()


# ------------------------------------------------------------------
# RUN  (only when started directly, not under gunicorn)
# ------------------------------------------------------------------
if __name__ == '__main__':
    socketio.run(app, debug=True, use_reloader=False,
                 host='0.0.0.0', port=5000)