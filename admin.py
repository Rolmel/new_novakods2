import os
import re
import time
import threading
import eventlet
eventlet.monkey_patch()
import random
from flask_socketio import SocketIO, emit, join_room, leave_room
from itertools import combinations
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import psycopg2
import psycopg2.extras

# --- KONFIGURĀCIJA ---

from redis_games import (
    init_redis,
    bj_deal, bj_hit, bj_stand,
    poker_deal, poker_draw,
    tower_start, tower_step, tower_cashout,
)


ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'txt', 'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'mp4', 'mp3'}
MAX_FILE_SIZE    = 20 * 1024 * 1024
MAX_FOLDER_SIZE  = 20 * 1024 * 1024

MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW       = 300
login_attempts = {}

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
app.secret_key = os.environ.get('SECRET_KEY', '12039ijojij0djwa98djjawjdwa98jdaw98dj98jd92j98jd8aj29dj2a98j98aj9dja0dajjijsijdlkakcnkjznndj29ejekjkjoma3tonvaw98va3oirhv3roiaw3vr98lislkc98y332kmf92ks92ka292kjl8s3l9uovj3nmu8m8uy3oaro87t3roanyvlrvhkjvmi8cw38r9mbhuhims4esxruit76tt878y8y898v32yr982mux32is')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400

BASE_DIR = Path(__file__).parent
app.config['UPLOAD_FOLDER'] = str(BASE_DIR / "static" / "uploads")
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# --- DATABASE (PostgreSQL) ---
def get_db_connection():
    conn = psycopg2.connect(
        os.environ.get('DATABASE_URL', 'postgresql://rolmel:yourpassword@localhost/novakods')
    )
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    conn.close()
    print("[db] PostgreSQL connection OK")

# --- PALĪGFUNKCIJAS ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_dir_size(path):
    total = 0
    if os.path.exists(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                total += os.path.getsize(os.path.join(dirpath, f))
    return total

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Lūdzu ielogojies!', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Lūdzu ielogojies!', 'error')
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            flash('Nav piekļuves!', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

def is_brute_force(ip):
    now = time.time()
    attempts = login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW]
    login_attempts[ip] = attempts
    return len(attempts) >= MAX_LOGIN_ATTEMPTS

def record_attempt(ip):
    now = time.time()
    login_attempts.setdefault(ip, []).append(now)

def validate_password(password):
    if len(password) < 8:
        return "Parolei jābūt vismaz 8 simbolus garai!"
    if not re.search(r'\d', password):
        return "Parolei jāsatur vismaz viens cipars!"
    return None

def sanitize_message(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# --- KĻŪDU APSTRĀDE ---
@app.errorhandler(413)
def file_too_large(e):
    flash('Fails ir pārāk liels! Maksimālais izmērs ir 10MB.', 'error')
    return redirect(url_for('bumbox'))

@app.errorhandler(404)
def not_found(e):
    return render_template('index.html'), 404

# --- MARŠRUTI ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('user', '').strip()
        password = request.form.get('pass', '')

        if not username or not password:
            flash("Aizpildi visus laukus!", 'error')
            return redirect(url_for('register'))
        if len(username) < 3 or len(username) > 30:
            flash("Lietotājvārdam jābūt 3–30 simbolus garam!", 'error')
            return redirect(url_for('register'))
        if not re.match(r'^[a-zA-Z0-9_]+$', username):
            flash("Lietotājvārds drīkst saturēt tikai burtus, ciparus un _!", 'error')
            return redirect(url_for('register'))
        pw_error = validate_password(password)
        if pw_error:
            flash(pw_error, 'error')
            return redirect(url_for('register'))

        hash_pw = generate_password_hash(password)
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                (username, hash_pw)
            )
            conn.commit()
            cur.close()
            flash("Reģistrācija veiksmīga! Tagad vari ielogoties.", 'success')
            return redirect(url_for('login'))
        except psycopg2.errors.UniqueViolation:
            flash("Lietotājvārds jau aizņemts!", 'error')
        finally:
            conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr
        if is_brute_force(ip):
            flash("Pārāk daudz mēģinājumu! Mēģini vēlāk.", 'error')
            return render_template('login.html')

        username = request.form.get('user', '').strip()
        password = request.form.get('pass', '')
        if not username or not password:
            flash("Aizpildi visus laukus!", 'error')
            return render_template('login.html')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id']   = user['id']
            session['user_name'] = user['username']
            session['is_admin']  = bool(user['is_admin'])
            # ensure wallet row exists
            conn2 = get_db_connection()
            get_or_create_balance(conn2, user['id'])
            conn2.close()
            session.permanent = True
            login_attempts.pop(ip, None)
            flash(f"Sveiks, {user['username']}! Veiksmīgi ielogojies.", 'success')
            return redirect(url_for('index'))
        else:
            record_attempt(ip)
            remaining = MAX_LOGIN_ATTEMPTS - len(login_attempts.get(ip, []))
            flash(f"Nepareizs lietotājvārds vai parole! (atlikuši {remaining} mēģinājumi)", 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Veiksmīgi izlogojies.", 'success')
    return redirect(url_for('index'))

@app.route('/bumbox')
@login_required
def bumbox():
    user_id  = session['user_id']
    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{user_id}")
    used_mb  = round(get_dir_size(user_dir) / (1024 * 1024), 2)

    conn  = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM user_files WHERE user_id = %s", (user_id,))
    files = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('bumbox.html', files=files, used_mb=used_mb, max_mb=100)

@app.route('/download/<int:file_id>')
@login_required
def download_file(file_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM user_files WHERE id = %s AND user_id = %s",
        (file_id, session['user_id'])
    )
    file_data = cur.fetchone()
    cur.close()
    conn.close()

    if file_data:
        user_dir = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{session['user_id']}")
        return send_from_directory(user_dir, file_data['filename'], as_attachment=True)

    flash("Fails nav atrasts.", 'error')
    return redirect(url_for('bumbox'))

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'GET':
        return redirect(url_for('bumbox'))
    if 'file' not in request.files:
        flash('Sistēmas kļūda: fails netika saņemts.', 'error')
        return redirect(url_for('bumbox'))

    f = request.files['file']
    if f.filename == '':
        flash('Lūdzu, vispirms izvēlies failu!', 'error')
        return redirect(url_for('bumbox'))
    if not allowed_file(f.filename):
        flash('Šāds faila tips nav atļauts!', 'error')
        return redirect(url_for('bumbox'))

    filename = secure_filename(f.filename)
    user_id  = session['user_id']
    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{user_id}")
    os.makedirs(user_dir, exist_ok=True)

    if get_dir_size(user_dir) >= MAX_FOLDER_SIZE:
        flash('Tava krātuve ir pilna (100MB)! Izdzēs kaut ko.', 'error')
        return redirect(url_for('bumbox'))

    f.save(os.path.join(user_dir, filename))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO user_files (filename, user_id) VALUES (%s, %s)", (filename, user_id))
    conn.commit()
    cur.close()
    conn.close()
    flash(f'Fails "{filename}" veiksmīgi augšupielādēts!', 'success')
    return redirect(url_for('bumbox'))

@app.route('/delete/<int:file_id>', methods=['POST'])
@login_required
def delete_file(file_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM user_files WHERE id = %s AND user_id = %s",
        (file_id, session['user_id'])
    )
    file = cur.fetchone()
    if file:
        user_dir  = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{session['user_id']}")
        file_path = os.path.join(user_dir, file['filename'])
        if os.path.exists(file_path):
            os.remove(file_path)
        cur.execute("DELETE FROM user_files WHERE id = %s", (file_id,))
        conn.commit()
        flash(f'Fails "{file["filename"]}" izdzēsts.', 'success')
    else:
        flash('Fails nav atrasts vai nav tava īpašums.', 'error')
    cur.close()
    conn.close()
    return redirect(url_for('bumbox'))

@app.route('/canvas')
@login_required
def canvas():
    return render_template('canvas.html')

@app.route('/api/canvas_data')
def get_canvas_data():
    conn   = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT x, y, color FROM canvas")
    pixels = cur.fetchall()
    cur.execute("""
        SELECT u.username, s.count FROM canvas_scores s
        JOIN users u ON s.user_id = u.id ORDER BY s.count DESC LIMIT 10
    """)
    scores = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({"pixels": [dict(p) for p in pixels], "scores": [dict(s) for s in scores]})

@app.route('/api/place', methods=['POST'])
def place_pixel():
    if 'user_id' not in session:
        return jsonify({"error": "No auth"}), 401
    data = request.json
    x, y = data.get('x'), data.get('y')
    color = data.get('color', '')
    if not isinstance(x, int) or not isinstance(y, int):
        return jsonify({"error": "Invalid coordinates"}), 400
    if not re.match(r'^#[0-9a-fA-F]{6}$', color):
        return jsonify({"error": "Invalid color"}), 400

    uid  = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO canvas (x, y, color, user_id) VALUES (%s, %s, %s, %s)
        ON CONFLICT (x, y) DO UPDATE SET color = EXCLUDED.color, user_id = EXCLUDED.user_id
    """, (x, y, color, uid))
    cur.execute(
        "INSERT INTO canvas_scores (user_id, count) VALUES (%s, 1) ON CONFLICT (user_id) DO UPDATE SET count = canvas_scores.count + 1",
        (uid,)
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

# --- ADMIN ---
@app.route('/api/clear_canvas', methods=['POST'])
@login_required
@admin_required
def clear_canvas():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM canvas")
    cur.execute("DELETE FROM canvas_scores")
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "canvas notīrīts"})

@app.route('/admin/set_admin/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def set_admin(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_admin = 1 WHERE id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

# --- ČATS ---
@app.route('/site')
@login_required
def site_chat():
    return render_template('site.html')

@app.route('/api/chat/groups', methods=['GET', 'POST'])
def handle_groups():
    if 'user_id' not in session:
        return jsonify({"error": "No auth"}), 401
    user_id = session['user_id']
    conn    = get_db_connection()

    if request.method == 'POST':
        group_name = request.json.get('name', '').strip()
        if not group_name or len(group_name) > 50:
            conn.close()
            return jsonify({"error": "Nederīgs grupas nosaukums"}), 400
        cur = conn.cursor()
        cur.execute("INSERT INTO chat_groups (name, created_by) VALUES (%s, %s) RETURNING id", (group_name, user_id))
        group_id = cur.fetchone()[0]
        cur.execute("INSERT INTO chat_members (group_id, user_id) VALUES (%s, %s)", (group_id, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok", "group_id": group_id})

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT g.id, g.name FROM chat_groups g
        JOIN chat_members m ON g.id = m.group_id
        WHERE m.user_id = %s
    """, (user_id,))
    groups = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(g) for g in groups])

@app.route('/api/chat/group/<int:group_id>/add_user', methods=['POST'])
def add_user_to_group(group_id):
    if 'user_id' not in session:
        return jsonify({"error": "No auth"}), 401
    new_username = request.json.get('username', '').strip()
    conn         = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM users WHERE username = %s", (new_username,))
    user_to_add = cur.fetchone()
    if user_to_add:
        try:
            cur.execute("INSERT INTO chat_members (group_id, user_id) VALUES (%s, %s)", (group_id, user_to_add['id']))
            conn.commit()
            status = "Pievienots!"
        except psycopg2.errors.UniqueViolation:
            status = "Lietotājs jau ir grupā!"
    else:
        status = "Lietotājs nav atrasts!"
    cur.close()
    conn.close()
    return jsonify({"status": status})

@app.route('/api/chat/group/<int:group_id>/messages', methods=['GET', 'POST'])
def handle_messages(group_id):
    if 'user_id' not in session:
        return jsonify({"error": "No auth"}), 401
    user_id = session['user_id']
    conn    = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT 1 FROM chat_members WHERE group_id = %s AND user_id = %s",
        (group_id, user_id)
    )
    is_member = cur.fetchone()
    if not is_member:
        cur.close()
        conn.close()
        return jsonify({"error": "Not a member"}), 403

    if request.method == 'POST':
        msg_text = request.json.get('message', '').strip()
        if not msg_text or len(msg_text) > 2000:
            cur.close()
            conn.close()
            return jsonify({"error": "Nederīga ziņa"}), 400
        msg_text = sanitize_message(msg_text)
        cur.execute(
            "INSERT INTO chat_messages (group_id, user_id, message) VALUES (%s, %s, %s)",
            (group_id, user_id, msg_text)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})

    cur.execute("""
        SELECT m.message, m.created_at, u.username
        FROM chat_messages m
        JOIN users u ON m.user_id = u.id
        WHERE m.group_id = %s
        ORDER BY m.created_at ASC
        LIMIT 200
    """, (group_id,))
    messages = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(m) for m in messages])











def make_deck():
    suits = ['♠', '♥', '♦', '♣']
    ranks = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
    deck = [{'rank': r, 'suit': s} for s in suits for r in ranks]
    random.shuffle(deck)
    return deck

def card_value(card):
    r = card['rank']
    if r in ('J', 'Q', 'K'): return 10
    if r == 'A': return 11
    return int(r)

# ==========================================
#  DAILY BONUS
# ==========================================
DAILY_BONUS_AMOUNT = 250

@app.route('/api/daily', methods=['POST'])
@login_required
def api_daily_bonus():
    from redis_games import _r
    user_id = session['user_id']
    redis   = _r()
    key     = f'daily:{user_id}'

    if redis.exists(key):
        ttl = redis.ttl(key)
        hours   = ttl // 3600
        minutes = (ttl % 3600) // 60
        return jsonify({
            'ok':      False,
            'message': f'Jau saņemts šodien! Atkal pieejams pēc {hours}h {minutes}m.',
            'ttl':     ttl
        }), 429

    conn    = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    balance += DAILY_BONUS_AMOUNT
    cur = conn.cursor()
    cur.execute(
        "UPDATE wallets SET balance = %s WHERE user_id = %s",
        (round(balance, 2), user_id)
    )
    record_transaction(
        conn, user_id, 'daily', 0,
        f'Dienas bonuss +{DAILY_BONUS_AMOUNT}', DAILY_BONUS_AMOUNT, balance
    )
    # cur2 = conn.cursor()
    # cur2.execute(
    #     """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
    #        VALUES (%s, %s, %s, %s)""",
    #     (user_id, DAILY_BONUS_AMOUNT, 'daily_bonus', round(balance, 2))
    # )
    # cur2.close()
    conn.commit()   

    redis.setex(key, 86400, '1')
    return jsonify({
        'ok':      True,
        'credits': DAILY_BONUS_AMOUNT,
        'balance': round(balance, 2),
        'message': f'+{DAILY_BONUS_AMOUNT} monētas! Atkal rīt.'
    })

@app.route('/dev/redis/state/<game>/<int:uid>')
def dev_redis_state(game, uid):
    if not app.debug:
        return '', 404
    from redis_games import dev_state
    return jsonify(dev_state(game, uid))

# =============================================
#  CASINO ROUTES
# =============================================

# --- PALĪGFUNKCIJAS ---
def get_or_create_balance(conn, user_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (%s, 1000) RETURNING balance",
            (user_id,)
        )
        conn.commit()
        cur.close()
        return 1000.0
    cur.close()
    return float(row['balance'])

def record_transaction(conn, user_id, game, bet, result, winnings, balance_after):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO casino_transactions
               (user_id, game, bet, result, winnings, balance_after)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user_id, game, bet, result, winnings, round(balance_after, 2))
    )
    cur.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
           VALUES (%s, %s, %s, %s, %s)""",
        (user_id, round(winnings, 2), f'casino:{game}', result[:100], round(balance_after, 2))
    )
    cur.close()

# ==========================================
#  CASINO SĀKUMLAPA
# ==========================================
@app.route('/casino')
@login_required
def casino_home():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT game, bet, result, winnings, balance_after, created_at
           FROM casino_transactions WHERE user_id = %s
           ORDER BY created_at DESC LIMIT 10""",
        (user_id,)
    )
    history = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('casino_home.html', balance=balance, history=history)

# ==========================================
#  SLOTS
# ==========================================
@app.route('/casino/slots')
@login_required
def slots():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_slots.html', balance=balance)

@app.route('/api/casino/slots', methods=['POST'])
@login_required
def api_slots():
    user_id = session['user_id']
    data = request.json
    bet = float(data.get('bet', 10))
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    if bet <= 0 or bet > balance:
        conn.close()
        return jsonify({"error": "Nepareizs likmjums"}), 400

    SYMBOLS = ['🍒', '🍋', '🔔', '⭐', '7️⃣', '💎']
    WEIGHTS = [30, 25, 20, 15, 8, 2]
    reels = random.choices(SYMBOLS, weights=WEIGHTS, k=3)

    if reels[0] == reels[1] == reels[2]:
        sym = reels[0]
        multipliers = {'💎': 50, '7️⃣': 20, '⭐': 10, '🔔': 7, '🍋': 5, '🍒': 3}
        mult = multipliers.get(sym, 3)
        winnings = bet * mult
        result = f"JACKPOT! {sym}{sym}{sym} x{mult}"
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        winnings = bet * 1.5
        result = "Divi vienādi!"
    else:
        winnings = 0
        result = "Neveiksmīgi"

    winnings = winnings - bet
    balance = balance + winnings
    cur = conn.cursor()
    cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
    record_transaction(conn, user_id, 'slots', bet, result, winnings, balance)
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "reels": reels,
        "result": result,
        "winnings": winnings,
        "net": winnings, 
        "balance": balance
    })

# ==========================================
#  BLACKJACK (delegated to redis_games)
# ==========================================
@app.route('/casino/blackjack')
@login_required
def blackjack():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_blackjack.html', balance=balance)

@app.route('/api/casino/blackjack/deal', methods=['POST'])
@login_required
def api_bj_deal():
    return bj_deal(app, session, request)

@app.route('/api/casino/blackjack/hit', methods=['POST'])
@login_required
def api_bj_hit():
    return bj_hit(app, session, request)

@app.route('/api/casino/blackjack/stand', methods=['POST'])
@login_required
def api_bj_stand():
    return bj_stand(app, session, request)

# ==========================================
#  HIGH / LOW
# ==========================================
CARD_DECK_HL = [{'rank': r, 'suit': s}
    for s in ['♠','♥','♦','♣']
    for r in ['2','3','4','5','6','7','8','9','10','J','Q','K','A']]

@app.route('/casino/highlow')
@login_required
def highlow():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_highlow.html', balance=balance)

@app.route('/api/casino/highlow/start', methods=['POST'])
@login_required
def hl_start():
    deck = CARD_DECK_HL.copy()
    random.shuffle(deck)
    card = deck.pop()
    session['hl_deck'] = deck
    session['hl_current'] = card
    session['hl_streak'] = 0
    return jsonify({"card": card, "value": card_value(card), "streak": 0})

@app.route('/api/casino/highlow/guess', methods=['POST'])
@login_required
def hl_guess():
    user_id = session['user_id']
    data = request.json
    guess = data.get('guess')
    bet = float(data.get('bet', 10))

    deck = session.get('hl_deck', [])
    current = session.get('hl_current')
    streak = session.get('hl_streak', 0)
    if not current or not deck:
        return jsonify({"error": "Sāc jaunu spēli"}), 400

    next_card = deck.pop()
    curr_val = card_value(current)
    next_val = card_value(next_card)

    if (guess == 'high' and next_val > curr_val) or \
       (guess == 'low' and next_val < curr_val):
        correct = True
        streak += 1
    elif next_val == curr_val:
        correct = None
        streak = streak
    else:
        correct = False
        streak = 0

    session['hl_current'] = next_card
    session['hl_deck'] = deck
    session['hl_streak'] = streak

    winnings = 0
    balance = None
    if correct is True:
        multiplier = 1 + (streak * 0.5)
        winnings = bet * multiplier
        winnings = winnings - bet
        conn = get_db_connection()
        balance = get_or_create_balance(conn, user_id)
        balance += winnings
        cur = conn.cursor()
        cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
        record_transaction(conn, user_id, 'highlow', bet, f"Pareizi! Streak {streak}", winnings, balance)
        conn.commit()
        cur.close()
        conn.close()
    elif correct is False:
        conn = get_db_connection()
        balance = get_or_create_balance(conn, user_id)
        balance -= bet
        if balance < 0: balance = 0
        cur = conn.cursor()
        cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
        record_transaction(conn, user_id, 'highlow', bet, "Nepareizi", -bet, balance)
        conn.commit()
        cur.close()
        conn.close()

    return jsonify({
        "next_card": next_card,
        "next_value": next_val,
        "correct": correct,
        "streak": streak,
        "winnings": winnings,
        "net": winnings, 
        "balance": balance
    })

# ==========================================
#  KENO
# ==========================================
@app.route('/casino/keno')
@login_required
def keno():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_keno.html', balance=balance)

@app.route('/api/casino/keno', methods=['POST'])
@login_required
def api_keno():
    user_id = session['user_id']
    data = request.json
    bet = float(data.get('bet', 10))
    picks = data.get('picks', [])
    if not (1 <= len(picks) <= 10):
        return jsonify({"error": "Izvēlies 1-10 skaitļus"}), 400

    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    if bet <= 0 or bet > balance:
        conn.close()
        return jsonify({"error": "Nepareizs likmjums"}), 400

    drawn = random.sample(range(1, 81), 20)
    matches = len(set(picks) & set(drawn))

    pay_table = {
        1:  {1: 3},
        2:  {2: 9},
        3:  {2: 2, 3: 16},
        4:  {2: 1, 3: 4, 4: 50},
        5:  {3: 2, 4: 10, 5: 250},
        6:  {3: 1, 4: 4, 5: 50, 6: 1200},
        7:  {4: 2, 5: 15, 6: 100, 7: 4000},
        8:  {4: 1, 5: 8,  6: 50,  7: 750,  8: 10000},
        9:  {5: 4, 6: 20, 7: 100, 8: 2000, 9: 30000},
        10: {5: 2, 6: 10, 7: 50,  8: 500,  9: 5000, 10: 100000},
    }
    n = len(picks)
    multiplier = pay_table.get(n, {}).get(matches, 0)
    winnings = bet * multiplier
    winnings = winnings - bet

    balance += winnings
    cur = conn.cursor()
    cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
    result_str = f"{matches}/{n} sakritības, x{multiplier}"
    record_transaction(conn, user_id, 'keno', bet, result_str, winnings, balance)
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "drawn": drawn,
        "matches": matches,
        "multiplier": multiplier,
        "winnings": winnings,
        "net": winnings, 
        "balance": balance
    })

# ==========================================
#  BINGO
# ==========================================
@app.route('/casino/bingo')
@login_required
def bingo():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_bingo.html', balance=balance)

@app.route('/api/casino/bingo/new_card', methods=['POST'])
@login_required
def bingo_new_card():
    user_id = session['user_id']
    data = request.json
    bet = float(data.get('bet', 10))
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    if bet <= 0 or bet > balance:
        conn.close()
        return jsonify({"error": "Nepareizs likmjums"}), 400

    # Deduct bet immediately — loss is the default until bingo is hit
    balance -= bet
    cur = conn.cursor()
    cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
    record_transaction(conn, user_id, 'bingo', bet, 'Karte izsniegta (zaudējums)', -bet, balance)
    conn.commit()
    cur.close()

    ranges = [(1,15),(16,30),(31,45),(46,60),(61,75)]
    card = []
    for col_range in ranges:
        nums = random.sample(range(col_range[0], col_range[1]+1), 5)
        card.append(nums)
    card_t = [[card[col][row] for col in range(5)] for row in range(5)]
    card_t[2][2] = 'FREE'

    session['bingo_card'] = card_t
    session['bingo_bet'] = bet
    session['bingo_called'] = []
    conn.close()
    return jsonify({"card": card_t, "balance": round(balance, 2)})

@app.route('/api/casino/bingo/call', methods=['POST'])
@login_required
def bingo_call():
    user_id = session['user_id']
    card = session.get('bingo_card')
    called = session.get('bingo_called', [])
    bet = float(session.get('bingo_bet', 10))
    if not card:
        return jsonify({"error": "Nav aktīvas spēles"}), 400

    user_id = session['user_id']
    card = sessi
    all_nums = list(range(1, 76))
    remaining = [n for n in all_nums if n not in called]
    if not remaining:
        return jsonify({"error": "Visi skaitļi izsaukti"}), 400

    new_num = random.choice(remaining)
    called.append(new_num)
    session['bingo_called'] = called

    def check_bingo(card, called_set):
        for row in card:
            if all(c == 'FREE' or c in called_set for c in row):
                return True
        for col in range(5):
            if all(card[row][col] == 'FREE' or card[row][col] in called_set for row in range(5)):
                return True
        if all(card[i][i] == 'FREE' or card[i][i] in called_set for i in range(5)):
            return True
        if all(card[i][4-i] == 'FREE' or card[i][4-i] in called_set for i in range(5)):
            return True
        return False

    called_set = set(called)
    has_bingo = check_bingo(card, called_set)
    winnings = 0
    balance = None

    if has_bingo:
        multiplier = max(2, 30 - len(called))
        winnings = round(bet * multiplier, 2)   # bet already gone, add full payout
        conn = get_db_connection()
        balance = get_or_create_balance(conn, user_id)
        balance += winnings
        cur = conn.cursor()
        cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
        record_transaction(conn, user_id, 'bingo', bet, f"BINGO! {len(called)} izsaukumi, x{multiplier}", winnings, balance)
        conn.commit()
        cur.close()
        conn.close()
        for k in ['bingo_card','bingo_bet','bingo_called']:
            session.pop(k, None)

    return jsonify({
        "number": new_num,
        "called": called,
        "bingo": has_bingo,
        "winnings": 0,
        "balance": round(balance, 2)
    })

# ==========================================
#  POKER (delegated to redis_games)
# ==========================================
def poker_rank(hand):
    ranks_order = '23456789TJQKA'
    vals = sorted([ranks_order.index(c['rank'].replace('10','T')) for c in hand], reverse=True)
    suits = [c['suit'] for c in hand]
    flush = len(set(suits)) == 1
    straight = (max(vals) - min(vals) == 4 and len(set(vals)) == 5)
    if set(vals) == {0,1,2,3,12}:
        straight = True
        vals = [3,2,1,0,-1]
    from collections import Counter
    cnt = Counter(vals)
    freq = sorted(cnt.values(), reverse=True)
    groups = sorted(cnt.keys(), key=lambda x: (cnt[x], x), reverse=True)

    if straight and flush:    return (8, "Straight Flush", groups)
    if freq[0] == 4:          return (7, "Four of a Kind", groups)
    if freq[:2] == [3,2]:     return (6, "Full House", groups)
    if flush:                 return (5, "Flush", groups)
    if straight:              return (4, "Straight", groups)
    if freq[0] == 3:          return (3, "Three of a Kind", groups)
    if freq[:2] == [2,2]:     return (2, "Two Pair", groups)
    if freq[0] == 2:          return (1, "Pair", groups)
    return (0, "High Card", groups)

POKER_PAYOUTS = {8: 50, 7: 25, 6: 9, 5: 6, 4: 4, 3: 3, 2: 2, 1: 1, 0: 0}

@app.route('/casino/poker')
@login_required
def poker():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_poker.html', balance=balance)

@app.route('/api/casino/poker/deal', methods=['POST'])
@login_required
def api_poker_deal():
    return poker_deal(app, session, request)

@app.route('/api/casino/poker/draw', methods=['POST'])
@login_required
def api_poker_draw():
    return poker_draw(app, session, request)

# ==========================================
#  TOWER GAME (delegated to redis_games)
# ==========================================
@app.route('/casino/tower')
@login_required
def tower():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_tower.html', balance=balance)

@app.route('/api/casino/tower/start', methods=['POST'])
@login_required
def api_tower_start():
    return tower_start(app, session, request)

@app.route('/api/casino/tower/step', methods=['POST'])
@login_required
def api_tower_step():
    return tower_step(app, session, request)

@app.route('/api/casino/tower/cashout', methods=['POST'])
@login_required
def api_tower_cashout():
    return tower_cashout(app, session, request)

# ==========================================
#  TRANSAKCIJU VĒSTURE
# ==========================================
@app.route('/casino/history')
@login_required
def casino_history():
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT game, bet, result, winnings, balance_after, created_at
           FROM casino_transactions WHERE user_id = %s
           ORDER BY created_at DESC LIMIT 100""",
        (user_id,)
    )
    history = cur.fetchall()
    balance = get_or_create_balance(conn, user_id)
    cur.close()
    conn.close()
    return render_template('casino_history.html', history=history, balance=balance)

# ==========================================
#  TEXAS HOLD'EM — Real-Time Multiplayer
# ==========================================
# (full hold'em code follows, already adapted to psycopg2 in-place)

_HOLDEM_LOCK = threading.Lock()
_SB_AMOUNT  = 10
_BB_AMOUNT  = 20
_MAX_SEATS  = 6

def _new_holdem_table(tid):
    return {
        'id': tid, 'name': f'Galds {tid}', 'seats': [None] * _MAX_SEATS,
        'players': {}, 'deck': [], 'community': [], 'pot': 0.0,
        'stage': 'waiting', 'dealer_seat': -1, 'bb_seat': -1, 'sb_seat': -1,
        'current_seat': -1, 'current_bet': 0.0, 'to_act': [], 'winners': [],
        'hand_num': 0,
    }

_HOLDEM_TABLES = {i: _new_holdem_table(i) for i in range(1, 6)}

def _feed(table_id, type_, name='', detail='', amount=None):
    socketio.emit('holdem_feed', {
        'type': type_, 'name': name, 'detail': detail, 'amount': amount
    }, room=f'htable_{table_id}')

def _best_holdem_hand(cards):
    best_score = None
    best_name  = 'High Card'
    for combo in combinations(cards, 5):
        rank_int, name, groups = poker_rank(list(combo))
        score = (rank_int, groups)
        if best_score is None or score > best_score:
            best_score = score
            best_name  = name
    return best_score, best_name

def _active_seats(table):
    return [
        s for s in range(_MAX_SEATS)
        if table['seats'][s] and table['seats'][s] in table['players']
        and not table['players'][table['seats'][s]]['folded']
    ]

def _order_from(table, start_seat):
    result = []
    for i in range(_MAX_SEATS):
        s   = (start_seat + i) % _MAX_SEATS
        uid = table['seats'][s]
        if not uid or uid not in table['players']:
            continue
        p = table['players'][uid]
        if p['folded'] or p['all_in']:
            continue
        result.append(uid)
    return result

def _public_state(table):
    players_pub = {}
    for uid, p in table['players'].items():
        players_pub[uid] = {
            'name': p['name'], 'balance': round(p['balance'], 2),
            'folded': p['folded'], 'all_in': p['all_in'], 'round_bet': p['round_bet'],
            'seat': p['seat'],
            'cards': (
                p.get('reveal_cards', ['back', 'back'])
                if p.get('reveal_cards') else
                (['back', 'back'] if p.get('cards') and not p['folded'] else [])
            ),
            'sitting_out': p.get('sitting_out', False),
        }
    return {
        'id': table['id'], 'name': table['name'], 'seats': table['seats'],
        'players': players_pub, 'community': table['community'],
        'pot': round(table['pot'], 2), 'stage': table['stage'],
        'dealer_seat': table['dealer_seat'], 'sb_seat': table.get('sb_seat', -1),
        'bb_seat': table.get('bb_seat', -1), 'current_seat': table['current_seat'],
        'current_bet': table['current_bet'], 'to_act': table['to_act'],
        'winners': table['winners'],
    }

def _broadcast_holdem(table_id):
    table = _HOLDEM_TABLES[table_id]
    pub   = _public_state(table)
    socketio.emit('holdem_state', pub, room=f'htable_{table_id}')
    for uid, p in table['players'].items():
        if p['cards'] and not p['folded']:
            socketio.emit('holdem_private_cards',
                          {'cards': p['cards']},
                          room=f'hplayer_{uid}')

def _holdem_deal_hand(table_id):
    table = _HOLDEM_TABLES[table_id]
    for p in table['players'].values():
        p['sitting_out'] = False
    occupied = [
        s for s in range(_MAX_SEATS)
        if table['seats'][s] and table['seats'][s] in table['players']
        and table['players'][table['seats'][s]]['balance'] >= _BB_AMOUNT
    ]
    if len(occupied) < 2:
        table['stage'] = 'waiting'
        _broadcast_holdem(table_id)
        return

    table['hand_num']   += 1
    table['deck']        = make_deck()
    table['community']   = []
    table['pot']         = 0.0
    table['stage']       = 'preflop'
    table['winners']     = []
    table['current_bet'] = float(_BB_AMOUNT)

    for p in table['players'].values():
        p['cards'] = []; p['folded'] = False; p['all_in'] = False; p['round_bet'] = 0.0
        p.pop('reveal_cards', None)

    if table['dealer_seat'] == -1 or table['dealer_seat'] not in occupied:
        table['dealer_seat'] = occupied[0]
    else:
        cur = table['dealer_seat']
        for i in range(1, _MAX_SEATS + 1):
            s = (cur + i) % _MAX_SEATS
            if s in occupied:
                table['dealer_seat'] = s
                break

    dealer = table['dealer_seat']
    n      = len(occupied)
    d_idx  = occupied.index(dealer)
    if n == 2:
        sb_seat = dealer
        bb_seat = [s for s in occupied if s != dealer][0]
    else:
        sb_seat = occupied[(d_idx + 1) % n]
        bb_seat = occupied[(d_idx + 2) % n]

    table['sb_seat'] = sb_seat
    table['bb_seat'] = bb_seat

    def _post(seat, amount):
        uid    = table['seats'][seat]
        p      = table['players'][uid]
        actual = min(amount, p['balance'])
        p['balance'] -= actual
        p['round_bet'] += actual
        table['pot']   += actual
        if p['balance'] == 0:
            p['all_in'] = True

    _post(sb_seat, _SB_AMOUNT)
    _post(bb_seat, _BB_AMOUNT)

    deck = table['deck']
    for uid in [table['seats'][s] for s in occupied]:
        table['players'][uid]['cards'] = [deck.pop(), deck.pop()]

    bb_uid = table['seats'][bb_seat]
    order  = _order_from(table, (bb_seat + 1) % _MAX_SEATS)
    if bb_uid in order and not table['players'][bb_uid]['all_in']:
        if order[-1] != bb_uid:
            order.remove(bb_uid)
            order.append(bb_uid)
    table['to_act']       = order
    table['current_seat'] = table['players'][order[0]]['seat'] if order else -1

    socketio.emit('holdem_event',
                  {'msg': f'🃏 Roka #{table["hand_num"]} — blinds {_SB_AMOUNT}/{_BB_AMOUNT}'},
                  room=f'htable_{table_id}')
    _broadcast_holdem(table_id)

def _holdem_showdown(table_id):
    table             = _HOLDEM_TABLES[table_id]
    table['stage']    = 'showdown'
    table['current_seat'] = -1
    table['to_act']   = []

    balance_before = {uid: p['balance'] for uid, p in table['players'].items()}
    active = [(uid, p) for uid, p in table['players'].items() if not p['folded']]

    if len(active) == 1:
        uid, p = active[0]
        p['balance'] += table['pot']
        table['winners'] = [{
            'uid': uid, 'name': p['name'],
            'amount': table['pot'], 'hand_name': 'Vinnētājs!',
            'cards': p['cards'],
        }]
        socketio.emit('holdem_event',
                      {'msg': f'🏆 {p["name"]} uzvar {table["pot"]:.0f} coins!'},
                      room=f'htable_{table_id}')
    else:
        community = table['community']
        ranked = []
        for uid, p in active:
            score, hand_name = _best_holdem_hand(p['cards'] + community)
            ranked.append((score, uid, hand_name))
        ranked.sort(key=lambda x: x[0], reverse=True)
        best_score = ranked[0][0]
        winners    = [(uid, hn) for sc, uid, hn in ranked if sc == best_score]
        share      = round(table['pot'] / len(winners), 2)
        for uid, hand_name in winners:
            table['players'][uid]['balance'] += share
            table['winners'].append({
                'uid': uid, 'name': table['players'][uid]['name'],
                'amount': share, 'hand_name': hand_name,
                'cards': table['players'][uid]['cards'],
            })
        for uid, p in active:
            p['reveal_cards'] = p['cards']
        w_names = ' & '.join(table['players'][uid]['name'] for uid, _ in winners)
        socketio.emit('holdem_event',
                      {'msg': f'🏆 {w_names} uzvar {table["pot"]:.0f} coins!'},
                      room=f'htable_{table_id}')

    conn = get_db_connection()
    cur = conn.cursor()
    for uid, p in table['players'].items():
        final_bal = max(0.0, p['balance'])
        cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (final_bal, uid))
        winnings = final_bal - balance_before.get(uid, final_bal)
        _record_holdem_transaction(cur, uid, winnings, final_bal)
    conn.commit()
    cur.close()
    conn.close()

    _broadcast_holdem(table_id)

    def _next():
        socketio.sleep(6)
        with _HOLDEM_LOCK:
            _holdem_try_new_hand(table_id)
    socketio.start_background_task(_next)

def _holdem_try_new_hand(table_id):
    table = _HOLDEM_TABLES[table_id]
    viable = [
        s for s in range(_MAX_SEATS)
        if table['seats'][s] and table['seats'][s] in table['players']
        and table['players'][table['seats'][s]]['balance'] >= _BB_AMOUNT
    ]
    if len(viable) >= 2:
        _holdem_deal_hand(table_id)
    else:
        table['stage'] = 'waiting'
        _broadcast_holdem(table_id)

def _holdem_check_action_complete(table_id):
    table  = _HOLDEM_TABLES[table_id]
    active = [uid for uid, p in table['players'].items() if not p['folded']]
    if len(active) == 1:
        _holdem_showdown(table_id)
        return
    if not table['to_act']:
        _broadcast_holdem(table_id)
        def _adv():
            socketio.sleep(1.5)
            with _HOLDEM_LOCK:
                _holdem_advance_stage(table_id)
        socketio.start_background_task(_adv)
        return
    next_uid              = table['to_act'][0]
    table['current_seat'] = table['players'][next_uid]['seat']
    _broadcast_holdem(table_id)

def _holdem_advance_stage(table_id):
    table  = _HOLDEM_TABLES[table_id]
    stage  = table['stage']
    for p in table['players'].values():
        p['round_bet'] = 0.0
    table['current_bet'] = 0.0

    if stage == 'preflop':
        table['stage'] = 'flop'
        table['community'] += [table['deck'].pop() for _ in range(3)]
    elif stage == 'flop':
        table['stage'] = 'turn'
        table['community'].append(table['deck'].pop())
    elif stage == 'turn':
        table['stage'] = 'river'
        table['community'].append(table['deck'].pop())
    elif stage == 'river':
        _holdem_showdown(table_id)
        return

    order = _order_from(table, (table['dealer_seat'] + 1) % _MAX_SEATS)
    table['to_act']       = order
    table['current_seat'] = table['players'][order[0]]['seat'] if order else -1

    socketio.emit('holdem_event',
                  {'msg': f'▶ {table["stage"].capitalize()}'},
                  room=f'htable_{table_id}')
    _broadcast_holdem(table_id)

def _holdem_remove_player(user_id):
    for tid, table in _HOLDEM_TABLES.items():
        if user_id not in table['players']:
            continue
        p    = table['players'][user_id]
        seat = p['seat']
        name = p['name']

        if table['stage'] not in ('waiting', 'showdown'):
            p['folded'] = True
            if user_id in table['to_act']:
                table['to_act'].remove(user_id)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s",
                     (max(0.0, p['balance']), user_id))
        conn.commit()
        cur.close()
        conn.close()

        table['seats'][seat] = None
        del table['players'][user_id]

        _feed(tid, 'leave', name=name, detail='🚪 Aizgāja')
        _broadcast_holdem(tid)

        if table['stage'] not in ('waiting', 'showdown'):
            active = [u for u, q in table['players'].items() if not q['folded']]
            if len(active) <= 1:
                _holdem_showdown(tid)
            elif not table['to_act']:
                def _adv():
                    socketio.sleep(1.5)
                    with _HOLDEM_LOCK:
                        _holdem_advance_stage(tid)
                socketio.start_background_task(_adv)
        break

# ── socket.io events ──
@socketio.on('connect')
def holdem_on_connect():
    if 'user_id' in session:
        join_room(f'hplayer_{session["user_id"]}')

@socketio.on('disconnect')
def holdem_on_disconnect():
    if 'user_id' in session:
        with _HOLDEM_LOCK:
            _holdem_remove_player(session['user_id'])

@socketio.on('holdem_get_tables')
def holdem_get_tables():
    info = []
    for tid, t in _HOLDEM_TABLES.items():
        info.append({
            'id': tid, 'name': t['name'], 'stage': t['stage'],
            'blinds': f'{_SB_AMOUNT}/{_BB_AMOUNT}',
            'seats': [{
                'idx': i, 'occupied': t['seats'][i] is not None,
                'player_name': t['players'][t['seats'][i]]['name'] if t['seats'][i] and t['seats'][i] in t['players'] else None,
                'balance': round(t['players'][t['seats'][i]]['balance'], 0) if t['seats'][i] and t['seats'][i] in t['players'] else None,
            } for i in range(_MAX_SEATS)],
        })
    emit('holdem_tables_list', info)

@socketio.on('holdem_watch_table')
def holdem_watch_table(data):
    tid = int(data.get('table_id', 1))
    join_room(f'htable_{tid}')
    table = _HOLDEM_TABLES.get(tid)
    if table:
        emit('holdem_state', _public_state(table))

@socketio.on('holdem_unwatch_table')
def holdem_unwatch_table(data):
    tid = int(data.get('table_id', 0))
    leave_room(f'htable_{tid}')

@socketio.on('holdem_join_table')
def holdem_join_table(data):
    if 'user_id' not in session:
        emit('holdem_error', {'msg': 'Nav autorizēts!'})
        return
    tid     = int(data.get('table_id', 1))
    seat    = int(data.get('seat', 0))
    user_id = session['user_id']

    with _HOLDEM_LOCK:
        for t in _HOLDEM_TABLES.values():
            if user_id in t['players']:
                emit('holdem_error', {'msg': 'Tu jau sēdi pie cita galda!'})
                return
        if tid not in _HOLDEM_TABLES:
            emit('holdem_error', {'msg': 'Galds nav atrasts'})
            return
        table = _HOLDEM_TABLES[tid]
        if seat < 0 or seat >= _MAX_SEATS:
            emit('holdem_error', {'msg': 'Nepareizs sēdekļa numurs'})
            return
        if table['seats'][seat] is not None:
            emit('holdem_error', {'msg': 'Šī vieta ir aizņemta!'})
            return

        conn    = get_db_connection()
        balance = get_or_create_balance(conn, user_id)
        conn.close()
        if balance < _BB_AMOUNT:
            emit('holdem_error', {'msg': f'Nepietiek naudas! Min: {_BB_AMOUNT} coins'})
            return

        hand_in_progress = table['stage'] not in ('waiting', 'showdown')
        table['seats'][seat] = user_id
        table['players'][user_id] = {
            'name': session.get('user_name', 'Spēlētājs'),
            'balance': balance, 'cards': [], 'folded': False,
            'all_in': False, 'round_bet': 0.0, 'seat': seat,
            'sitting_out': hand_in_progress,
        }

        join_room(f'htable_{tid}')
        emit('holdem_joined', {'table_id': tid, 'seat': seat, 'user_id': user_id})

        if hand_in_progress:
            socketio.emit('holdem_event',
                          {'msg': f'🙋 {session.get("user_name")} pievienojās — spēlēs nākamajā rokā.'},
                          room=f'htable_{tid}')
        else:
            socketio.emit('holdem_event',
                          {'msg': f'🙋 {session.get("user_name")} pievienojās (vieta {seat + 1})'},
                          room=f'htable_{tid}')

        _broadcast_holdem(tid)

        active = sum(
            1 for s in table['seats']
            if s is not None and s in table['players']
            and not table['players'][s].get('sitting_out', False)
        )
        if active >= 2 and table['stage'] == 'waiting':
            def _auto_start():
                socketio.sleep(3)
                with _HOLDEM_LOCK:
                    if _HOLDEM_TABLES[tid]['stage'] == 'waiting':
                        _holdem_deal_hand(tid)
            socketio.start_background_task(_auto_start)

@socketio.on('holdem_leave_table')
def holdem_leave_table(data):
    if 'user_id' not in session:
        return
    tid     = int(data.get('table_id', 0))
    user_id = session['user_id']
    with _HOLDEM_LOCK:
        _holdem_remove_player(user_id)
    leave_room(f'htable_{tid}')
    emit('holdem_left', {})

@socketio.on('holdem_action')
def holdem_action(data):
    if 'user_id' not in session:
        emit('holdem_error', {'msg': 'Nav autorizēts!'})
        return

    user_id = session['user_id']
    action  = data.get('action')
    amount  = float(data.get('amount', 0))

    with _HOLDEM_LOCK:
        table_id = None
        for tid, t in _HOLDEM_TABLES.items():
            if user_id in t['players']:
                table_id = tid
                break
        if table_id is None:
            emit('holdem_error', {'msg': 'Tu nesēdi pie galda!'})
            return

        table = _HOLDEM_TABLES[table_id]
        if table['stage'] in ('waiting', 'showdown'):
            emit('holdem_error', {'msg': 'Nav aktīvas spēles!'})
            return
        if not table['to_act'] or table['to_act'][0] != user_id:
            emit('holdem_error', {'msg': 'Nav tava kārta!'})
            return

        p       = table['players'][user_id]
        to_call = max(0.0, table['current_bet'] - p['round_bet'])

        if action == 'fold':
            p['folded'] = True
            table['to_act'].pop(0)
            _feed(table_id, 'fold', name=p['name'])
        elif action == 'check':
            if to_call > 0:
                emit('holdem_error', {'msg': 'Nevar check — ir aktīva likme!'})
                return
            table['to_act'].pop(0)
            _feed(table_id, 'check', name=p['name'])
        elif action == 'call':
            call_amt       = min(to_call, p['balance'])
            p['balance']   -= call_amt
            p['round_bet'] += call_amt
            table['pot']   += call_amt
            if p['balance'] == 0:
                p['all_in'] = True
            table['to_act'].pop(0)
            _feed(table_id, 'call', name=p['name'], amount=call_amt)
        elif action == 'raise':
            min_raise = table['current_bet'] + _BB_AMOUNT
            raise_to  = max(float(amount), min_raise)
            raise_by  = raise_to - p['round_bet']
            if raise_by >= p['balance']:
                raise_by   = p['balance']
                raise_to   = p['round_bet'] + raise_by
                p['all_in'] = True
            p['balance']    -= raise_by
            p['round_bet']  += raise_by
            table['pot']    += raise_by
            table['current_bet'] = p['round_bet']
            raiser_seat    = p['seat']
            table['to_act'].pop(0)
            new_order = _order_from(table, (raiser_seat + 1) % _MAX_SEATS)
            if user_id in new_order:
                new_order.remove(user_id)
            table['to_act'] = new_order
            _feed(table_id, 'raise', name=p['name'], amount=raise_by)
        else:
            emit('holdem_error', {'msg': 'Nezināma darbība'})
            return

        _holdem_check_action_complete(table_id)

@app.route('/casino/holdem')
@login_required
def casino_holdem_page():
    user_id = session['user_id']
    conn    = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_holdem.html', balance=balance,
                           user_id=user_id, user_name=session.get('user_name', 'Spēlētājs'))

def _record_holdem_transaction(cur, user_id, winnings, balance_after):
    cur.execute(
        """INSERT INTO casino_transactions (user_id, game, bet, result, winnings, balance_after)
           VALUES (%s, 'holdem', 0, 'hand_result', %s, %s)""",
        (user_id, winnings, balance_after)
    )

# ============================================================
#  PROFILE SYSTEM
# ============================================================
AVATAR_ALLOWED = {'png', 'jpg', 'jpeg', 'gif'}
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_DIR = str(BASE_DIR / "static" / "uploads" / "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)

COUNTRIES = sorted(list(dict.fromkeys([
    "Afghanistan","Albania","Algeria","Andorra","Angola","Argentina","Armenia",
    "Australia","Austria","Azerbaijan","Bahamas","Bahrain","Bangladesh","Belarus",
    "Belgium","Belize","Benin","Bolivia","Bosnia and Herzegovina","Botswana",
    "Brazil","Brunei","Bulgaria","Burkina Faso","Cambodia","Cameroon","Canada",
    "Chile","China","Colombia","Croatia","Cuba","Cyprus","Czech Republic",
    "Denmark","Dominican Republic","Ecuador","Egypt","El Salvador","Estonia",
    "Ethiopia","Finland","France","Georgia","Germany","Ghana","Greece",
    "Guatemala","Haiti","Honduras","Hungary","Iceland","India","Indonesia",
    "Iran","Iraq","Ireland","Israel","Italy","Jamaica","Japan","Jordan",
    "Kazakhstan","Kenya","Kosovo","Kuwait","Kyrgyzstan","Latvia","Lebanon",
    "Libya","Lithuania","Luxembourg","Malaysia","Malta","Mexico","Moldova",
    "Mongolia","Montenegro","Morocco","Mozambique","Myanmar","Nepal",
    "Netherlands","New Zealand","Nicaragua","Nigeria","North Korea","Norway",
    "Oman","Pakistan","Palestine","Panama","Paraguay","Peru","Philippines",
    "Poland","Portugal","Qatar","Romania","Russia","Rwanda","Saudi Arabia",
    "Senegal","Serbia","Singapore","Slovakia","Slovenia","Somalia","South Africa",
    "South Korea","South Sudan","Spain","Sri Lanka","Sudan","Sweden","Switzerland",
    "Syria","Taiwan","Tajikistan","Tanzania","Thailand","Tunisia","Turkey",
    "Turkmenistan","Uganda","Ukraine","United Arab Emirates","United Kingdom",
    "United States","Uruguay","Uzbekistan","Venezuela","Vietnam","Yemen","Zambia",
    "Zimbabwe", "Latvia"
])))

def _get_profile(conn, user_id):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO profiles (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,)
    )
    cur.close()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM profiles WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    return row

def _profile_stats(conn, user_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Casino stats from casino_transactions (game-specific detail)
    cur.execute("""
        SELECT
            COUNT(*)                                                        AS total_games,
            COALESCE(MAX(winnings), 0)                                      AS best_win
        FROM casino_transactions WHERE user_id = %s
    """, (user_id,))
    casino_row = cur.fetchone()

    # Financial totals from wallet_log (single source of truth after step 2)
    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), 0)    AS total_won,
            COALESCE(SUM(CASE WHEN delta < 0 THEN delta ELSE 0 END), 0)    AS total_lost
        FROM wallet_log WHERE user_id = %s
    """, (user_id,))
    ledger_row = cur.fetchone()

    cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    casino_balance_row = cur.fetchone()

    cur.execute("""
        SELECT game, bet, result, winnings
        FROM casino_transactions
        WHERE user_id = %s
        ORDER BY created_at DESC LIMIT 8
    """, (user_id,))
    recent_games = cur.fetchall()

    cur.execute("SELECT count FROM canvas_scores WHERE user_id = %s", (user_id,))
    pixel_row = cur.fetchone()

    cur.execute("SELECT COUNT(*) AS n FROM user_files WHERE user_id = %s", (user_id,))
    file_row = cur.fetchone()

    cur.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE user_id = %s", (user_id,))
    msg_row = cur.fetchone()

    cur.close()
    return {
        "total_games":    casino_row["total_games"]    if casino_row else 0,
        "total_won":      ledger_row["total_won"]      if ledger_row else 0,
        "total_lost":     ledger_row["total_lost"]     if ledger_row else 0,
        "best_win":       casino_row["best_win"]       if casino_row else 0,
        "casino_balance": float(casino_balance_row["balance"]) if casino_balance_row else 0,
        "recent_games":   [dict(r) for r in recent_games],
        "pixel_count":    pixel_row["count"] if pixel_row else 0,
        "file_count":     file_row["n"]      if file_row  else 0,
        "message_count":  msg_row["n"]       if msg_row   else 0,
    }

@app.route('/u/<username>')
def profile_page(username):
    conn  = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, username FROM users WHERE username = %s", (username,))
    owner = cur.fetchone()
    if not owner:
        cur.close()
        conn.close()
        flash("Lietotājs nav atrasts.", "error")
        return redirect(url_for('index'))

    profile = _get_profile(conn, owner['id'])
    stats   = _profile_stats(conn, owner['id'])
    conn.commit()
    cur.close()
    conn.close()

    avatar_url = None
    if profile and profile['avatar_path']:
        avatar_url = url_for('static', filename=f"uploads/avatars/{profile['avatar_path']}")
    profile_data = dict(profile) if profile else {}
    profile_data['avatar_url'] = avatar_url

    is_own_profile = ('user_id' in session and session['user_id'] == owner['id'])
    return render_template(
        'profile.html',
        owner=dict(owner),
        profile=profile_data,
        stats=stats,
        is_own_profile=is_own_profile,
        countries=[(c, c) for c in COUNTRIES],
    )

@app.route('/api/profile/update', methods=['POST'])
@login_required
def api_profile_update():
    user_id = session['user_id']
    data    = request.json or {}

    display_name = data.get('display_name', '')[:40].strip()
    title        = data.get('title', '')[:40].strip()
    country      = data.get('country', '')[:60].strip()

    for val in (display_name, title, country):
        if '<' in val or '>' in val:
            return jsonify({'ok': False, 'error': 'Nederīga vērtība'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO profiles (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,)
    )
    cur.execute("""
        UPDATE profiles
        SET display_name = %s, title = %s, country = %s
        WHERE user_id = %s
    """, (display_name, title, country, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'display_name': display_name})

@app.route('/api/profile/avatar', methods=['POST'])
@login_required
def api_profile_avatar():
    user_id = session['user_id']
    if 'avatar' not in request.files:
        return jsonify({'ok': False, 'error': 'Nav faila'}), 400

    f = request.files['avatar']
    if f.filename == '':
        return jsonify({'ok': False, 'error': 'Nav izvēlēts fails'}), 400

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in AVATAR_ALLOWED:
        return jsonify({'ok': False, 'error': 'Neatļauts formāts'}), 400

    data = f.read()
    if len(data) > AVATAR_MAX_BYTES:
        return jsonify({'ok': False, 'error': 'Fails pārāk liels (maks 2MB)'}), 400

    filename  = f"user_{user_id}.{ext}"
    save_path = os.path.join(AVATAR_DIR, filename)
    with open(save_path, 'wb') as out:
        out.write(data)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO profiles (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,)
    )
    cur.execute(
        "UPDATE profiles SET avatar_path = %s WHERE user_id = %s",
        (filename, user_id)
    )
    conn.commit()
    cur.close()
    conn.close()

    url = url_for('static', filename=f"uploads/avatars/{filename}")
    return jsonify({'ok': True, 'url': url})

# ==========================================
#  PREDICTION MARKET
# ==========================================

def _update_option_prices(conn, event_id):
    """Recalculate implied probability for each option after a new bet."""
    cur = conn.cursor()
    cur.execute("""
        SELECT po.id, COALESCE(pv.total_stake, 0) as total_stake
        FROM prediction_options po
        LEFT JOIN prediction_volume pv ON pv.option_id = po.id
        WHERE po.event_id = %s
    """, (event_id,))
    rows = cur.fetchall()
    total = sum(r[1] for r in rows) or 1
    for option_id, stake in rows:
        price = round(max(0.01, stake / total), 4)
        cur.execute(
            "UPDATE prediction_options SET price = %s WHERE id = %s",
            (price, option_id)
        )
    cur.close()


@app.route('/predictions')
@login_required
def predictions_list():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT pe.*,
               COUNT(DISTINCT p.id)        AS total_bets,
               COALESCE(SUM(p.stake), 0)   AS total_volume
        FROM prediction_events pe
        LEFT JOIN predictions p ON p.event_id = pe.id
        WHERE pe.status IN ('open', 'closed', 'resolved')
        GROUP BY pe.id
        ORDER BY pe.closes_at ASC
    """)
    events = cur.fetchall()

    cur.execute("""
        SELECT event_id, option_id, stake
        FROM predictions WHERE user_id = %s
    """, (session['user_id'],))
    positions = {r['event_id']: dict(r) for r in cur.fetchall()}

    balance = get_or_create_balance(conn, session['user_id'])
    cur.close()
    conn.close()
    return render_template('predictions.html',
                           events=[dict(e) for e in events],
                           positions=positions,
                           balance=balance)


@app.route('/predictions/<int:event_id>')
@login_required
def prediction_detail(event_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM prediction_events WHERE id = %s", (event_id,))
    event = cur.fetchone()
    if not event:
        flash('Notikums nav atrasts.', 'error')
        conn.close()
        return redirect(url_for('predictions_list'))

    cur.execute("""
        SELECT po.*,
               COALESCE(pv.total_stake,  0) AS volume,
               COALESCE(pv.backer_count, 0) AS backers
        FROM prediction_options po
        LEFT JOIN prediction_volume pv ON pv.option_id = po.id
        WHERE po.event_id = %s
        ORDER BY po.id
    """, (event_id,))
    options = cur.fetchall()

    cur.execute("""
        SELECT p.*, po.label AS option_label
        FROM predictions p
        JOIN prediction_options po ON po.id = p.option_id
        WHERE p.event_id = %s AND p.user_id = %s
    """, (event_id, session['user_id']))
    my_position = cur.fetchone()

    cur.execute("""
        SELECT u.username, p.stake, po.label, p.created_at
        FROM predictions p
        JOIN users u              ON u.id  = p.user_id
        JOIN prediction_options po ON po.id = p.option_id
        WHERE p.event_id = %s
        ORDER BY p.created_at DESC LIMIT 20
    """, (event_id,))
    recent_bets = cur.fetchall()

    balance = get_or_create_balance(conn, session['user_id'])
    cur.close()
    conn.close()
    return render_template('prediction_detail.html',
                           event=dict(event),
                           options=[dict(o) for o in options],
                           my_position=dict(my_position) if my_position else None,
                           recent_bets=[dict(b) for b in recent_bets],
                           balance=balance,
                           is_admin=session.get('is_admin', False))


@app.route('/api/predictions/create', methods=['POST'])
@login_required
@admin_required
def api_prediction_create():
    data        = request.json or {}
    title       = data.get('title', '').strip()[:200]
    description = data.get('description', '').strip()
    category    = data.get('category', 'general')
    closes_at   = data.get('closes_at')
    options     = data.get('options', [])

    if not title or not closes_at or len(options) < 2:
        return jsonify({'error': 'Nepieciešams nosaukums, beigu datums un vismaz 2 opcijas'}), 400

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO prediction_events (title, description, category, created_by, closes_at)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """, (title, description, category, session['user_id'], closes_at))
    event_id = cur.fetchone()[0]

    equal_price = round(1.0 / len(options), 4)
    for label in options:
        label = label.strip()[:100]
        if label:
            cur.execute("""
                INSERT INTO prediction_options (event_id, label, price)
                VALUES (%s, %s, %s)
            """, (event_id, label, equal_price))

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'event_id': event_id})


@app.route('/api/predictions/<int:event_id>/bet', methods=['POST'])
@login_required
def api_prediction_bet(event_id):
    user_id   = session['user_id']
    data      = request.json or {}
    option_id = int(data.get('option_id', 0))
    stake     = float(data.get('stake', 0))

    if stake <= 0:
        return jsonify({'error': 'Likmei jābūt lielākai par 0'}), 400

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM prediction_events WHERE id = %s", (event_id,))
    event = cur.fetchone()
    if not event or event['status'] != 'open':
        conn.close()
        return jsonify({'error': 'Notikums nav pieejams likmēm'}), 400

    cur.execute("""
        SELECT * FROM prediction_options WHERE id = %s AND event_id = %s
    """, (option_id, event_id))
    option = cur.fetchone()
    if not option:
        conn.close()
        return jsonify({'error': 'Nepareiza opcija'}), 400

    cur.execute("""
        SELECT id FROM predictions WHERE user_id = %s AND event_id = %s
    """, (user_id, event_id))
    if cur.fetchone():
        conn.close()
        return jsonify({'error': 'Tu jau esi likuši likmi šim notikumam'}), 400

    balance = get_or_create_balance(conn, user_id)
    if stake > balance:
        conn.close()
        return jsonify({'error': 'Nepietiek naudas'}), 400

    balance -= stake
    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE wallets SET balance = %s WHERE user_id = %s",
        (round(balance, 2), user_id)
    )
    cur2.execute("""
        INSERT INTO predictions (user_id, event_id, option_id, stake, price_at_entry)
        VALUES (%s, %s, %s, %s, %s)
    """, (user_id, event_id, option_id, round(stake), round(float(option['price']), 4)))
    cur2.execute("""
        INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
        VALUES (%s, %s, %s, %s, %s)
    """, (user_id, -round(stake), 'prediction:bet', str(event_id), round(balance, 2)))

    conn.commit()

    _update_option_prices(conn, event_id)
    conn.commit()

    cur.execute("""
        SELECT po.id, po.label, po.price,
               COALESCE(pv.total_stake, 0)  AS volume,
               COALESCE(pv.backer_count, 0) AS backers
        FROM prediction_options po
        LEFT JOIN prediction_volume pv ON pv.option_id = po.id
        WHERE po.event_id = %s
        ORDER BY po.id
    """, (event_id,))
    updated_options = [dict(o) for o in cur.fetchall()]

    cur.close()
    cur2.close()
    conn.close()

    socketio.emit('prediction_price_update', {
        'event_id': event_id,
        'options':  updated_options
    }, room=f'prediction_{event_id}')

    return jsonify({'ok': True, 'balance': round(balance, 2), 'options': updated_options})


@app.route('/api/predictions/<int:event_id>/resolve', methods=['POST'])
@login_required
@admin_required
def api_prediction_resolve(event_id):
    data              = request.json or {}
    winning_option_id = int(data.get('option_id', 0))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM prediction_events WHERE id = %s", (event_id,))
    event = cur.fetchone()
    if not event or event['status'] == 'resolved':
        conn.close()
        return jsonify({'error': 'Notikums jau atrisināts vai nav atrasts'}), 400

    cur.execute("""
        SELECT * FROM prediction_options WHERE id = %s AND event_id = %s
    """, (winning_option_id, event_id))
    winning_option = cur.fetchone()
    if not winning_option:
        conn.close()
        return jsonify({'error': 'Nepareiza uzvarētāja opcija'}), 400

    cur.execute("""
        SELECT COALESCE(SUM(stake), 0) AS total FROM predictions WHERE event_id = %s
    """, (event_id,))
    total_pot = float(cur.fetchone()['total'])

    cur.execute("""
        SELECT COALESCE(SUM(stake), 0) AS total
        FROM predictions WHERE event_id = %s AND option_id = %s
    """, (event_id, winning_option_id))
    winning_stakes = float(cur.fetchone()['total'])

    cur.execute("""
        SELECT * FROM predictions WHERE event_id = %s AND option_id = %s
    """, (event_id, winning_option_id))
    winners = cur.fetchall()

    cur2 = conn.cursor()
    for w in winners:
        payout = round((float(w['stake']) / winning_stakes) * total_pot, 2) if winning_stakes > 0 else 0
        w_balance = get_or_create_balance(conn, w['user_id']) + payout
        cur2.execute(
            "UPDATE wallets SET balance = %s WHERE user_id = %s",
            (round(w_balance, 2), w['user_id'])
        )
        cur2.execute(
            "UPDATE predictions SET payout = %s WHERE id = %s",
            (payout, w['id'])
        )
        cur2.execute("""
            INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
            VALUES (%s, %s, %s, %s, %s)
        """, (w['user_id'], payout, 'prediction:payout', str(event_id), round(w_balance, 2)))

    cur2.execute("""
        UPDATE prediction_events
        SET status = 'resolved', outcome = %s, resolves_at = NOW()
        WHERE id = %s
    """, (winning_option['label'], event_id))

    conn.commit()
    cur.close()
    cur2.close()
    conn.close()

    socketio.emit('prediction_resolved', {
        'event_id':       event_id,
        'winning_option': dict(winning_option),
        'total_pot':      total_pot,
        'winner_count':   len(winners)
    }, room=f'prediction_{event_id}')

    return jsonify({'ok': True, 'total_pot': total_pot, 'winners': len(winners)})


@socketio.on('watch_prediction')
def watch_prediction(data):
    join_room(f'prediction_{int(data.get("event_id", 0))}')

@socketio.on('unwatch_prediction')
def unwatch_prediction(data):
    leave_room(f'prediction_{int(data.get("event_id", 0))}')

# ==========================================
#  COSMETICS SHOP
# ==========================================
 
@app.route('/shop')
@login_required
def shop():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM cosmetics WHERE is_active = TRUE ORDER BY category, price"
    )
    items = cur.fetchall()
    cur.execute(
        "SELECT cosmetic_id FROM user_cosmetics WHERE user_id = %s", (user_id,)
    )
    owned = {row['cosmetic_id'] for row in cur.fetchall()}
    cur.close()
    conn.close()
    return render_template('shop.html', items=items, owned=owned, balance=balance)
 
 
@app.route('/api/shop/buy', methods=['POST'])
@login_required
def api_shop_buy():
    user_id     = session['user_id']
    cosmetic_id = int((request.json or {}).get('cosmetic_id', 0))
 
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 
    cur.execute(
        "SELECT * FROM cosmetics WHERE id = %s AND is_active = TRUE", (cosmetic_id,)
    )
    item = cur.fetchone()
    if not item:
        conn.close()
        return jsonify({'ok': False, 'error': 'Prece nav atrasta'}), 404
 
    cur.execute(
        "SELECT 1 FROM user_cosmetics WHERE user_id = %s AND cosmetic_id = %s",
        (user_id, cosmetic_id)
    )
    if cur.fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Jau pieder'}), 400
 
    balance = get_or_create_balance(conn, user_id)
    if balance < item['price']:
        conn.close()
        return jsonify({'ok': False, 'error': 'Nepietiek monētu'}), 400
 
    balance -= item['price']
    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE wallets SET balance = %s WHERE user_id = %s",
        (round(balance, 2), user_id)
    )
    cur2.execute(
        "INSERT INTO user_cosmetics (user_id, cosmetic_id) VALUES (%s, %s)",
        (user_id, cosmetic_id)
    )
    conn.commit()
    cur2.close()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'balance': round(balance, 2)})
 
 
# ==========================================
#  CLUBS
# ==========================================
 
@app.route('/clubs')
@login_required
def clubs_list():
    user_id = session['user_id']
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.*, u.username AS owner_name,
               COUNT(cm.user_id) AS member_count
        FROM clubs c
        JOIN users u ON u.id = c.owner_id
        LEFT JOIN club_members cm ON cm.club_id = c.id
        GROUP BY c.id, u.username
        ORDER BY member_count DESC, c.created_at DESC
    """)
    all_clubs = cur.fetchall()
    cur.execute(
        "SELECT club_id FROM club_members WHERE user_id = %s", (user_id,)
    )
    my_clubs = {r['club_id'] for r in cur.fetchall()}
    cur.close()
    conn.close()
    return render_template('clubs.html', clubs=all_clubs, my_clubs=my_clubs)
 
 
@app.route('/clubs/create', methods=['POST'])
@login_required
def create_club():
    user_id     = session['user_id']
    name        = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
 
    if not name or len(name) < 3:
        flash("Kluba nosaukumam jābūt vismaz 3 simbolus.", "error")
        return redirect(url_for('clubs_list'))
 
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clubs (name, description, owner_id)
            VALUES (%s, %s, %s) RETURNING id
        """, (name, description, user_id))
        club_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO club_members (club_id, user_id, role) VALUES (%s, %s, 'owner')",
            (club_id, user_id)
        )
        conn.commit()
        cur.close()
        flash(f"Klubs '{name}' izveidots!", "success")
        return redirect(url_for('club_detailed', club_id=club_id))
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash("Kluba nosaukums jau aizņemts!", "error")
        return redirect(url_for('clubs_list'))
    finally:
        conn.close()
 
 
@app.route('/clubs/join', methods=['POST'])
@login_required
def join_club():
    user_id     = session['user_id']
    invite_code = request.form.get('invite_code', '').strip()
 
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM clubs WHERE invite_code = %s", (invite_code,))
    club = cur.fetchone()
    if not club:
        flash("Kods nav derīgs.", "error")
        cur.close()
        conn.close()
        return redirect(url_for('clubs_list'))
 
    try:
        cur2 = conn.cursor()
        cur2.execute(
            "INSERT INTO club_members (club_id, user_id) VALUES (%s, %s)",
            (club['id'], user_id)
        )
        conn.commit()
        cur2.close()
        flash(f"Pievienojies klubam '{club['name']}'!", "success")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash("Tu jau esi šajā klubā.", "error")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('club_detailed', club_id=club['id']))
 
 
@app.route('/clubs/<int:club_id>/leave', methods=['POST'])
@login_required
def leave_club(club_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT owner_id FROM clubs WHERE id = %s", (club_id,))
    club = cur.fetchone()
    if club and club['owner_id'] == user_id:
        flash("Dibinātājs nevar atstāt klubu.", "error")
    else:
        cur2 = conn.cursor()
        cur2.execute(
            "DELETE FROM club_members WHERE club_id = %s AND user_id = %s",
            (club_id, user_id)
        )
        conn.commit()
        cur2.close()
        flash("Atstāji klubu.", "success")
    cur.close()
    conn.close()
    return redirect(url_for('clubs_list'))
 
 
@app.route('/clubs/<int:club_id>')
@login_required
def club_detail(club_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 
    cur.execute("""
        SELECT c.*, u.username AS owner_name
        FROM clubs c JOIN users u ON u.id = c.owner_id
        WHERE c.id = %s
    """, (club_id,))
    club = cur.fetchone()
    if not club:
        flash("Klubs nav atrasts.", "error")
        conn.close()
        return redirect(url_for('clubs_list'))
 
    cur.execute("""
        SELECT u.username, cm.role, cm.joined_at,
               COALESCE(w.balance, 0) AS balance,
               COALESCE(agg.total_won,  0) AS total_won,
               COALESCE(agg.total_games, 0) AS total_games
        FROM club_members cm
        JOIN users u ON u.id = cm.user_id
        LEFT JOIN wallets w ON w.user_id = cm.user_id
        LEFT JOIN (
            SELECT user_id,
                   SUM(CASE WHEN winnings > 0 THEN winnings ELSE 0 END) AS total_won,
                   COUNT(*) AS total_games
            FROM casino_transactions GROUP BY user_id
        ) agg ON agg.user_id = cm.user_id
        WHERE cm.club_id = %s
        ORDER BY total_won DESC
    """, (club_id,))
    members = cur.fetchall()
 
    is_member = any(m['username'] == session.get('user_name') for m in members)
    is_owner  = (club['owner_id'] == user_id)
    cur.close()
    conn.close()
    return render_template('club_detailed.html',
                           club=dict(club),
                           members=members,
                           is_member=is_member,
                           is_owner=is_owner)


if __name__ == '__main__':
    init_db()
    init_redis()
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)