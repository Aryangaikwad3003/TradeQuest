import os
import random
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, g, flash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "super_secret_key_for_tradequest"
DATABASE = 'quiz.db'
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

class DBWrapper:
    def __init__(self, conn, is_postgres):
        self.conn = conn
        self.is_postgres = is_postgres

    def cursor(self):
        return CursorWrapper(self.conn.cursor(), self.is_postgres)

    def commit(self):
        if not self.is_postgres:
            self.conn.commit()

    def close(self):
        self.conn.close()

class CursorWrapper:
    def __init__(self, cursor, is_postgres):
        self.cursor = cursor
        self.is_postgres = is_postgres

    def execute(self, query, params=()):
        if self.is_postgres:
            query = query.replace('?', '%s')
        return self.cursor.execute(query, params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db_url = os.environ.get('DATABASE_URL')
        if db_url and db_url.startswith('postgres'):
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(db_url, row_factory=dict_row, autocommit=True)
            db = g._database = DBWrapper(conn, True)
        else:
            import sqlite3
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys = ON')
            db = g._database = DBWrapper(conn, False)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        
        is_pg = db.is_postgres
        pk_syntax = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS quizzes (
                id {pk_syntax},
                title TEXT NOT NULL,
                pass_percentage REAL NOT NULL,
                is_active INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS questions (
                id {pk_syntax},
                quiz_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option TEXT NOT NULL CHECK(correct_option IN ('A','B','C','D')),
                FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
            )
        ''')

        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS attempts (
                id {pk_syntax},
                quiz_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                score INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                percentage REAL NOT NULL,
                passed INTEGER NOT NULL,
                attempt_number INTEGER NOT NULL,
                quiz_attempt_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
            )
        ''')
        
        if not is_pg:
            try:
                cursor.cursor.execute('ALTER TABLE quizzes ADD COLUMN max_attempts INTEGER DEFAULT 3')
            except Exception:
                pass

            try:
                cursor.cursor.execute('ALTER TABLE attempts ADD COLUMN quiz_attempt_id INTEGER')
            except Exception:
                pass

        # Backfill quiz_attempt_id for existing attempts
        cursor.execute('SELECT id, quiz_id FROM attempts WHERE quiz_attempt_id IS NULL ORDER BY timestamp ASC, id ASC')
        null_attempts = cursor.fetchall()
        for att in null_attempts:
            cursor.execute('SELECT COALESCE(MAX(quiz_attempt_id), 0) as max_id FROM attempts WHERE quiz_id = ?', (att['quiz_id'],))
            current_max = cursor.fetchone()['max_id']
            cursor.execute('UPDATE attempts SET quiz_attempt_id = ? WHERE id = ?', (current_max + 1, att['id']))

        db.commit()

# --- Auth Decorator ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# USER ROUTES
# ==========================================

@app.route('/', methods=['GET'])
def index():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, title FROM quizzes WHERE is_active = 1')
    quizzes = cursor.fetchall()
    return render_template('index.html', quizzes=quizzes)

@app.route('/quiz/start', methods=['POST'])
def start_quiz():
    name = request.form.get('name')
    employee_id = request.form.get('employee_id')
    quiz_id = request.form.get('quiz_id')

    if not name or not employee_id or not quiz_id:
        flash("All fields are required.", "error")
        return redirect(url_for('index'))

    session['user_name'] = name
    session['employee_id'] = employee_id

    # Find the latest attempt for this user to increment attempt_number
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        'SELECT MAX(attempt_number) as max_attempt FROM attempts WHERE quiz_id = ? AND employee_id = ?',
        (quiz_id, employee_id)
    )
    result = cursor.fetchone()
    current_attempt = 1
    if result and result['max_attempt']:
        current_attempt = result['max_attempt'] + 1
    
    session[f'attempt_{quiz_id}'] = current_attempt

    return redirect(url_for('quiz_page', quiz_id=quiz_id))

@app.route('/quiz/<int:quiz_id>', methods=['GET'])
def quiz_page(quiz_id):
    if not session.get('user_name') or not session.get('employee_id'):
        flash("Please enter your details first.", "error")
        return redirect(url_for('index'))

    db = get_db()
    cursor = db.cursor()
    
    # Check if active
    cursor.execute('SELECT * FROM quizzes WHERE id = ?', (quiz_id,))
    quiz = cursor.fetchone()
    
    if not quiz or not quiz['is_active']:
        flash("This quiz is not currently active.", "error")
        return redirect(url_for('index'))

    cursor.execute('SELECT * FROM questions WHERE quiz_id = ?', (quiz_id,))
    questions = cursor.fetchall()
    
    shuffled_questions = []
    for q in questions:
        q_dict = dict(q)
        opts = [
            ('A', q_dict['option_a']),
            ('B', q_dict['option_b']),
            ('C', q_dict['option_c']),
            ('D', q_dict['option_d'])
        ]
        random.shuffle(opts)
        q_dict['shuffled_options'] = opts
        shuffled_questions.append(q_dict)
    random.shuffle(shuffled_questions)

    return render_template('quiz.html', quiz=quiz, questions=shuffled_questions)

@app.route('/quiz/<int:quiz_id>/submit', methods=['POST'])
def submit_quiz(quiz_id):
    if not session.get('user_name') or not session.get('employee_id'):
        return redirect(url_for('index'))

    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT * FROM quizzes WHERE id = ?', (quiz_id,))
    quiz = cursor.fetchone()
    if not quiz or not quiz['is_active']:
        return redirect(url_for('index'))

    cursor.execute('SELECT * FROM questions WHERE quiz_id = ?', (quiz_id,))
    questions = cursor.fetchall()
    
    total_questions = len(questions)
    if total_questions == 0:
        flash("This quiz has no questions.", "error")
        return redirect(url_for('index'))

    score = 0
    for q in questions:
        user_answer = request.form.get(f'question_{q["id"]}')
        if user_answer and user_answer == q['correct_option']:
            score += 1

    percentage = (score / total_questions) * 100
    passed = 1 if percentage >= quiz['pass_percentage'] else 0
    
    attempt_num = session.get(f'attempt_{quiz_id}', 1)

    # Determine the quiz_attempt_id
    cursor.execute('SELECT COALESCE(MAX(quiz_attempt_id), 0) as max_id FROM attempts WHERE quiz_id = ?', (quiz_id,))
    quiz_attempt_id = cursor.fetchone()['max_id'] + 1

    cursor.execute('''
        INSERT INTO attempts (quiz_id, user_name, employee_id, score, total_questions, percentage, passed, attempt_number, quiz_attempt_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (quiz_id, session['user_name'], session['employee_id'], score, total_questions, percentage, passed, attempt_num, quiz_attempt_id))
    db.commit()

    return render_template('result.html', quiz=quiz, score=score, total_questions=total_questions, percentage=percentage, passed=passed, attempt_num=attempt_num, max_attempts=quiz['max_attempts'])

@app.route('/quiz/<int:quiz_id>/reattempt', methods=['POST'])
def reattempt_quiz(quiz_id):
    if not session.get('user_name') or not session.get('employee_id'):
        return redirect(url_for('index'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM quizzes WHERE id = ?', (quiz_id,))
    quiz = cursor.fetchone()
    if not quiz or not quiz['is_active']:
        flash("This quiz is no longer active.", "error")
        return redirect(url_for('index'))

    # Increment attempt counter
    session[f'attempt_{quiz_id}'] = session.get(f'attempt_{quiz_id}', 1) + 1

    return redirect(url_for('quiz_page', quiz_id=quiz_id))


# ==========================================
# ADMIN ROUTES
# ==========================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            flash("Invalid password", "error")
    return render_template('admin/login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/admin', methods=['GET'])
@admin_required
def admin_dashboard():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT q.id, q.title, q.is_active, q.pass_percentage, q.max_attempts,
               (SELECT COUNT(*) FROM questions WHERE quiz_id = q.id) as num_questions
        FROM quizzes q
        ORDER BY q.created_at DESC
    ''')
    quizzes = cursor.fetchall()
    return render_template('admin/dashboard.html', quizzes=quizzes)

@app.route('/admin/quiz/create', methods=['GET', 'POST'])
@admin_required
def create_quiz():
    if request.method == 'POST':
        title = request.form.get('title')
        pass_percentage = request.form.get('pass_percentage')
        max_attempts = request.form.get('max_attempts')
        if title and pass_percentage and max_attempts:
            db = get_db()
            cursor = db.cursor()
            cursor.execute('INSERT INTO quizzes (title, pass_percentage, is_active, max_attempts) VALUES (?, ?, 0, ?)', (title, pass_percentage, max_attempts))
            db.commit()
            return redirect(url_for('admin_dashboard'))
        else:
            flash("All fields are required.", "error")
    return render_template('admin/create_quiz.html')

@app.route('/admin/quiz/<int:quiz_id>/questions', methods=['GET', 'POST'])
@admin_required
def manage_questions(quiz_id):
    db = get_db()
    cursor = db.cursor()
    
    if request.method == 'POST':
        # Handles dynamic form where inputs are arrays
        questions = request.form.getlist('question_text[]')
        option_as = request.form.getlist('option_a[]')
        option_bs = request.form.getlist('option_b[]')
        option_cs = request.form.getlist('option_c[]')
        option_ds = request.form.getlist('option_d[]')
        correct_options = request.form.getlist('correct_option[]')
        
        for i in range(len(questions)):
            q_text = questions[i].strip()
            opt_a = option_as[i].strip()
            opt_b = option_bs[i].strip()
            opt_c = option_cs[i].strip()
            opt_d = option_ds[i].strip()
            correct = correct_options[i].strip()
            
            if q_text and opt_a and opt_b and opt_c and opt_d and correct:
                cursor.execute('''
                    INSERT INTO questions (quiz_id, question_text, option_a, option_b, option_c, option_d, correct_option)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (quiz_id, q_text, opt_a, opt_b, opt_c, opt_d, correct))
        
        db.commit()
        flash("Questions added successfully.", "success")
        return redirect(url_for('manage_questions', quiz_id=quiz_id))

    cursor.execute('SELECT * FROM quizzes WHERE id = ?', (quiz_id,))
    quiz = cursor.fetchone()
    
    cursor.execute('SELECT * FROM questions WHERE quiz_id = ?', (quiz_id,))
    existing_questions = cursor.fetchall()
    
    return render_template('admin/questions.html', quiz=quiz, questions=existing_questions)

@app.route('/admin/quiz/<int:quiz_id>/toggle', methods=['POST'])
@admin_required
def toggle_quiz(quiz_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT is_active FROM quizzes WHERE id = ?', (quiz_id,))
    row = cursor.fetchone()
    if row:
        new_status = 0 if row['is_active'] else 1
        cursor.execute('UPDATE quizzes SET is_active = ? WHERE id = ?', (new_status, quiz_id))
        db.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/quiz/<int:quiz_id>/delete', methods=['POST'])
@admin_required
def delete_quiz(quiz_id):
    db = get_db()
    cursor = db.cursor()
    # The ON DELETE CASCADE in the schema ensures questions and attempts are also removed
    cursor.execute('DELETE FROM quizzes WHERE id = ?', (quiz_id,))
    db.commit()
    flash("Quiz deleted successfully.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/quiz/<int:quiz_id>/results', methods=['GET'])
@admin_required
def quiz_results(quiz_id):
    db = get_db()
    cursor = db.cursor()
    
    cursor.execute('SELECT * FROM quizzes WHERE id = ?', (quiz_id,))
    quiz = cursor.fetchone()
    
    cursor.execute('''
        SELECT * FROM attempts 
        WHERE quiz_id = ? 
        ORDER BY timestamp DESC
    ''', (quiz_id,))
    attempts = cursor.fetchall()
    
    # Leaderboard: Only passed attempts, lowest attempt number, then highest score
    cursor.execute('''
        WITH RankedAttempts AS (
            SELECT *,
                   ROW_NUMBER() OVER(PARTITION BY employee_id ORDER BY attempt_number ASC, score DESC) as rn
            FROM attempts
            WHERE quiz_id = ? AND passed = 1
        )
        SELECT * FROM RankedAttempts WHERE rn = 1
        ORDER BY attempt_number ASC, score DESC
    ''', (quiz_id,))
    leaderboard = cursor.fetchall()
    
    return render_template('admin/results.html', quiz=quiz, attempts=attempts, leaderboard=leaderboard)

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
