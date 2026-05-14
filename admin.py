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
from dotenv import load_dotenv
import time
load_dotenv()

from achievements import check_achievements

# --- KONFIGURĀCIJA ---

from redis_games import (
    init_redis,
    bj_deal, bj_hit, bj_stand,
    poker_deal, poker_draw,
    tower_start, tower_step, tower_cashout,
    hl_start, hl_guess,
    bingo_new_card, bingo_call,
)

from win_card import generate_win_card
from flask import Response




ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'txt', 'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'mp4', 'mp3'}
MAX_FILE_SIZE    = 20 * 1024 * 1024
MAX_FOLDER_SIZE  = 20 * 1024 * 1024

MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW       = 300
login_attempts = {}

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins=os.environ.get('ALLOWED_ORIGIN', 'http://localhost:5000'))
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    raise RuntimeError('SECRET_KEY environment variable is not set')
app.secret_key = _secret
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400

BASE_DIR = Path(__file__).parent
app.config['UPLOAD_FOLDER'] = str(BASE_DIR / "static" / "uploads")
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

TIER_RANK = {
    'unranked': 0,
    'bronze':   1,
    'silver':   2,
    'gold':     3,
    'expert':   4,
    'oracle':   5,
}

def _get_user_tier(conn, user_id: int) -> str:
    cur = conn.cursor()
    cur.execute(
        "SELECT tier FROM predictor_tiers WHERE user_id = %s", (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else 'unranked'

def _tier_gte(user_tier: str, required: str) -> bool:
    return TIER_RANK.get(user_tier, 0) >= TIER_RANK.get(required, 0)

# --- DATABASE (PostgreSQL) ---
def get_db_connection():
    _db_url = os.environ.get('DATABASE_URL')
    if not _db_url:
        raise RuntimeError('DATABASE_URL environment variable is not set')
    conn = psycopg2.connect(_db_url)
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    # ensure global lobby exists
    cur.execute("""
        INSERT INTO chat_groups (id, name, created_by)
        VALUES (1, 'Lobby', NULL)
        ON CONFLICT (id) DO NOTHING
    """)
    # auto-join every user to lobby is handled client-side
    conn.commit()
    cur.close()
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
@login_required
def index():
    return render_template('index.html')

@app.route('/profile')
@login_required
def profile_redirect():
    return redirect(url_for('profile_page', username=session['user_name']))

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
    return render_template('index.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Veiksmīgi izlogojies.", 'success')
    return redirect(url_for('index'))









@app.route('/api/achievements/<int:user_id>')
@login_required
def api_user_achievements(user_id):
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.slug, a.name, a.description, a.icon, a.category, a.rarity,
               ua.unlocked_at
        FROM user_achievements ua
        JOIN achievements a ON a.slug = ua.slug
        WHERE ua.user_id = %s
        ORDER BY ua.unlocked_at DESC
    """, (user_id,))
    unlocked = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT slug, name, description, icon, category, rarity
        FROM achievements ORDER BY rarity DESC, category, name
    """)
    all_ach = [dict(r) for r in cur.fetchall()]

    unlocked_slugs = {r['slug'] for r in unlocked}
    for a in all_ach:
        a['unlocked'] = a['slug'] in unlocked_slugs

    cur.close()
    conn.close()
    return jsonify({'achievements': all_ach, 'count': len(unlocked)})

@app.route('/shop')
@login_required
def shop():
    user_id = session['user_id']
    conn    = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM cosmetics WHERE is_active=TRUE ORDER BY category, price")
    items = cur.fetchall()

    cur.execute("SELECT cosmetic_id FROM user_cosmetics WHERE user_id=%s", (user_id,))
    owned = {row['cosmetic_id'] for row in cur.fetchall()}

    cur.execute("SELECT equipped_skin FROM profiles WHERE user_id=%s", (user_id,))
    profile = cur.fetchone()
    equipped_skin_id = profile['equipped_skin'] if profile else None

    cur.close()
    conn.close()
    return render_template('shop.html', items=items, owned=owned,
                           balance=balance, equipped_skin_id=equipped_skin_id)

# ── Card skin CSS overrides ─────────────────────────────────
# Targets all card classes used across blackjack, highlow,
# holdem, videopoker templates.
CARD_SKIN_CSS: dict[str, str] = {
    'card_default': '',

    'card_gold': """
        .card:not(.back),.big-card:not(.empty),.hole-card:not(.back),
        .cc:not(.ph),.mc:not(.bk),.bigcard:not(.bk) {
            background:#fff9e0 !important;
            border:1px solid #e6a800 !important;
            box-shadow:0 4px 16px #f5c84222 !important;
        }
        .card.red,.big-card.red,.cc.rd,.mc.rd,.bigcard.rd { color:#8b0000 !important; }
        .card.back,.hole-card.back,.mc.bk,.bigcard.bk {
            background:linear-gradient(135deg,#8a6200,#c89800) !important;
        }
    """,

    'card_neon': """
        .card:not(.back),.big-card:not(.empty),.hole-card:not(.back),
        .cc:not(.ph),.mc:not(.bk),.bigcard:not(.bk) {
            background:#080812 !important;
            border:1px solid #00ff88 !important;
            box-shadow:0 0 10px #00ff8833 !important;
            color:#00ff88 !important;
        }
        .card.red,.big-card.red,.cc.rd,.mc.rd,.bigcard.rd { color:#ff0066 !important; }
        .card.back,.hole-card.back,.mc.bk,.bigcard.bk {
            background:linear-gradient(135deg,#001a0a,#003322) !important;
            border-color:#00ff88 !important;
        }
    """,

    'card_dark': """
        .card:not(.back),.big-card:not(.empty),.hole-card:not(.back),
        .cc:not(.ph),.mc:not(.bk),.bigcard:not(.bk) {
            background:#1a1a2e !important;
            border:1px solid #4a4a6a !important;
            color:#e0e0e0 !important;
            box-shadow:none !important;
        }
        .card.red,.big-card.red,.cc.rd,.mc.rd,.bigcard.rd { color:#ff6666 !important; }
        .card.back,.hole-card.back,.mc.bk,.bigcard.bk {
            background:linear-gradient(135deg,#0a0a1a,#1a1a3a) !important;
        }
    """,

    'card_galaxy': """
        .card:not(.back),.big-card:not(.empty),.hole-card:not(.back),
        .cc:not(.ph),.mc:not(.bk),.bigcard:not(.bk) {
            background:linear-gradient(135deg,#0d0221,#1a0535,#0a1628) !important;
            border:1px solid #7c3aed !important;
            box-shadow:0 0 14px #7c3aed33 !important;
            color:#e0d0ff !important;
        }
        .card.red,.big-card.red,.cc.rd,.mc.rd,.bigcard.rd { color:#ff88cc !important; }
        .card.back,.hole-card.back,.mc.bk,.bigcard.bk {
            background:linear-gradient(135deg,#2a0a4a,#3d1a6e) !important;
        }
    """,

    'card_retro': """
        .card:not(.back),.big-card:not(.empty),.hole-card:not(.back),
        .cc:not(.ph),.mc:not(.bk),.bigcard:not(.bk) {
            background:#e8dcc8 !important;
            border:2px solid #5a4a2a !important;
            border-radius:4px !important;
            color:#2a1a0a !important;
            font-family:'Courier New',monospace !important;
            box-shadow:3px 3px 0 #5a4a2a !important;
        }
        .card.red,.big-card.red,.cc.rd,.mc.rd,.bigcard.rd { color:#cc2200 !important; }
        .card.back,.hole-card.back,.mc.bk,.bigcard.bk {
            background:repeating-linear-gradient(
                45deg,#5a4a2a 0,#5a4a2a 4px,#2a1a0a 4px,#2a1a0a 8px
            ) !important;
        }
    """,
}


def _get_equipped_skin_css(user_id: int) -> str:
    """Returns the CSS string for the user's equipped card skin, or '' if none."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.slug FROM profiles p
            JOIN cosmetics c ON c.id = p.equipped_skin
            WHERE p.user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return CARD_SKIN_CSS.get(row['slug'], '')
    except Exception:
        pass
    return ''


@app.context_processor
def inject_skin():
    """Makes `card_skin_css` available in every Jinja template."""
    css = ''
    if 'user_id' in session:
        css = _get_equipped_skin_css(session['user_id'])
    return {'card_skin_css': css}

@app.route('/api/shop/equip', methods=['POST'])
@login_required
def api_shop_equip():
    user_id     = session['user_id']
    cosmetic_id = int((request.json or {}).get('cosmetic_id', 0))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Must own it
    cur.execute(
        "SELECT 1 FROM user_cosmetics WHERE user_id=%s AND cosmetic_id=%s",
        (user_id, cosmetic_id)
    )
    if not cur.fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Nav tavā īpašumā'}), 403

    cur.execute("SELECT category, slug FROM cosmetics WHERE id=%s", (cosmetic_id,))
    item = cur.fetchone()
    if not item or item['category'] != 'card_skin':
        conn.close()
        return jsonify({'ok': False, 'error': 'Nav kāršu izskats'}), 400

    cur2 = conn.cursor()
    cur2.execute(
        "INSERT INTO profiles (user_id, equipped_skin) VALUES (%s,%s) "
        "ON CONFLICT (user_id) DO UPDATE SET equipped_skin=%s",
        (user_id, cosmetic_id, cosmetic_id)
    )
    conn.commit()
    cur2.close()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'slug': item['slug']})


@app.route('/api/shop/unequip', methods=['POST'])
@login_required
def api_shop_unequip():
    user_id = session['user_id']
    conn    = get_db_connection()
    cur     = conn.cursor()
    cur.execute(
        "UPDATE profiles SET equipped_skin=NULL WHERE user_id=%s", (user_id,)
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

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
    cur2.execute(
    """INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
       VALUES (%s, %s, %s, %s, %s)""",
    (user_id, -item['price'], 'shop:buy', str(cosmetic_id), round(balance, 2))
    )
    conn.commit()
    cur2.close()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'balance': round(balance, 2)})

def _tournament_leaderboard(conn, tournament_id: int, limit: int = 10):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT te.user_id, u.username,
               te.chips, te.games_played, te.best_win,
               RANK() OVER (ORDER BY te.chips DESC) AS place
        FROM tournament_entries te
        JOIN users u ON u.id = te.user_id
        WHERE te.tournament_id = %s
        ORDER BY te.chips DESC
        LIMIT %s
    """, (tournament_id, limit))
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def _tournament_payout_tiers(prize_pool: int, player_count: int) -> list[dict]:
    """
    Returns list of {place, pct, amount} sorted by place.
    Always pays at least top-3 if enough players exist.
    """
    if player_count < 2:
        return [{'place': 1, 'pct': 100, 'amount': prize_pool}]

    tiers = [
        {'place': 1, 'pct': 50},
        {'place': 2, 'pct': 30},
        {'place': 3, 'pct': 20},
    ]
    # Trim to actual player count
    tiers = tiers[:player_count]

    # Re-normalise so they sum to 100
    total = sum(t['pct'] for t in tiers)
    result = []
    distributed = 0
    for i, t in enumerate(tiers):
        if i == len(tiers) - 1:
            amount = prize_pool - distributed
        else:
            amount = round(prize_pool * t['pct'] / total)
            distributed += amount
        result.append({'place': t['place'], 'pct': t['pct'], 'amount': amount})
    return result


@app.route('/api/tournaments/<int:tid>/join', methods=['POST'])
@login_required
def api_tournament_join(tid):
    user_id = session['user_id']
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM tournaments WHERE id = %s", (tid,))
    t = cur.fetchone()
    if not t:
        conn.close()
        return jsonify({'ok': False, 'error': 'Nav atrasts'}), 404
    if t['status'] != 'active':
        conn.close()
        return jsonify({'ok': False, 'error': 'Turnīrs nav aktīvs'}), 400

    cur.execute(
        "SELECT id FROM tournament_entries WHERE tournament_id = %s AND user_id = %s",
        (tid, user_id)
    )
    if cur.fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Jau piedalies'}), 400

    cur.execute(
        "SELECT COUNT(*) AS n FROM tournament_entries WHERE tournament_id = %s", (tid,)
    )
    if cur.fetchone()['n'] >= t['max_players']:
        conn.close()
        return jsonify({'ok': False, 'error': 'Turnīrs pilns'}), 400

    # Deduct entry fee from real wallet
    balance = get_or_create_balance(conn, user_id)
    if t['entry_fee'] > 0:
        if balance < t['entry_fee']:
            conn.close()
            return jsonify({'ok': False, 'error': 'Nepietiek monētu'}), 400
        balance -= t['entry_fee']
        cur2 = conn.cursor()
        cur2.execute(
            "UPDATE wallets SET balance = %s WHERE user_id = %s",
            (balance, user_id)
        )
        cur2.execute(
            "UPDATE tournaments SET prize_pool = prize_pool + %s WHERE id = %s",
            (t['entry_fee'], tid)
        )
        cur2.execute(
            """INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
               VALUES (%s, %s, 'tournament:entry', %s, %s)""",
            (user_id, -t['entry_fee'], str(tid), balance)
        )
        cur2.close()

    cur3 = conn.cursor()
    cur3.execute(
        """INSERT INTO tournament_entries (tournament_id, user_id, chips)
           VALUES (%s, %s, %s)""",
        (tid, user_id, t['start_coins'])
    )
    conn.commit()
    cur3.close()
    cur.close()
    conn.close()

    socketio.emit('tournament_update', {'tournament_id': tid},
                  room=f'tournament_{tid}')
    return jsonify({'ok': True, 'chips': t['start_coins'], 'balance': balance})

@app.route('/api/tournaments/<int:tid>/resolve', methods=['POST'])
@login_required
@admin_required
def api_tournament_resolve(tid):
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT prize_pool FROM tournaments WHERE id = %s", (tid,))
    t = cur.fetchone()
    cur.close()
    conn.close()
    if not t:
        return jsonify({'ok': False, 'error': 'Nav atrasts'})

    conn2 = get_db_connection()
    _resolve_tournament(conn2, tid)
    conn2.close()
    return jsonify({'ok': True, 'prize_pool': t['prize_pool']})


@app.route('/api/tournaments/<int:tid>/play/slots', methods=['POST'])
@login_required
def api_tournament_slots(tid):
    user_id = session['user_id']
    data    = request.json or {}
    bet     = int(data.get('bet', 10))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT * FROM tournament_entries WHERE tournament_id = %s AND user_id = %s",
        (tid, user_id)
    )
    entry = cur.fetchone()
    if not entry:
        conn.close()
        return jsonify({'error': 'Nav reģistrēts šim turnīram'}), 400

    cur.execute("SELECT status FROM tournaments WHERE id = %s", (tid,))
    t = cur.fetchone()
    if not t or t['status'] != 'active':
        conn.close()
        return jsonify({'error': 'Turnīrs nav aktīvs'}), 400

    chips = entry['chips']
    if bet <= 0 or bet > chips:
        conn.close()
        return jsonify({'error': 'Nepareizs likmjums'}), 400

    # Same slots logic as real game
    SYMBOLS = ['🍒', '🍋', '🔔', '⭐', '7️⃣', '💎']
    WEIGHTS  = [30, 25, 20, 15, 8, 2]
    reels    = random.choices(SYMBOLS, weights=WEIGHTS, k=3)

    if reels[0] == reels[1] == reels[2]:
        mults = {'💎': 50, '7️⃣': 20, '⭐': 10, '🔔': 7, '🍋': 5, '🍒': 3}
        mult  = mults.get(reels[0], 3)
        net   = bet * mult - bet
        result = f"JACKPOT! {reels[0]*3} x{mult}"
    elif len(set(reels)) < 3:
        net    = int(bet * 0.5)
        result = "Divi vienādi"
    else:
        net    = -bet
        result = "Neveiksmīgi"

    chips += net
    chips  = max(0, chips)

    best_win = max(entry['best_win'], net) if net > 0 else entry['best_win']

    cur2 = conn.cursor()
    cur2.execute(
        """UPDATE tournament_entries
           SET chips = %s, games_played = games_played + 1, best_win = %s
           WHERE tournament_id = %s AND user_id = %s""",
        (chips, best_win, tid, user_id)
    )
    conn.commit()
    cur2.close()
    cur.close()
    conn.close()

    socketio.emit('tournament_score', {
        'tournament_id': tid,
        'user_id':       user_id,
        'username':      session.get('user_name'),
        'chips':         chips,
    }, room=f'tournament_{tid}')

    return jsonify({
        'reels':  reels,
        'result': result,
        'net':    net,
        'chips':  chips,
    })


@app.route('/tournaments')
@login_required
def tournaments_list():
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT t.*,
               COUNT(te.user_id) AS player_count
        FROM tournaments t
        LEFT JOIN tournament_entries te ON te.tournament_id = t.id
        GROUP BY t.id
        ORDER BY t.starts_at DESC
    """)
    tournaments = [dict(r) for r in cur.fetchall()]
    balance = get_or_create_balance(conn, session['user_id'])
    cur.close()
    conn.close()
    return render_template('tournaments.html', tournaments=tournaments, balance=balance)

@app.route('/tournaments/<int:tid>')
@login_required
def tournament_detail(tid):
    user_id = session['user_id']
    conn    = get_db_connection()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM tournaments WHERE id = %s", (tid,))
    t = cur.fetchone()
    if not t:
        conn.close()
        flash('Nav atrasts', 'error')
        return redirect(url_for('tournaments_list'))

    lb = _tournament_leaderboard(conn, tid)

    cur.execute(
        "SELECT * FROM tournament_entries WHERE tournament_id=%s AND user_id=%s",
        (tid, user_id)
    )
    my_entry = cur.fetchone()

    # Payouts (only relevant for finished)
    cur.execute(
        "SELECT * FROM tournament_payouts WHERE tournament_id = %s", (tid,)
    )
    payouts = cur.fetchall()

    balance = get_or_create_balance(conn, user_id)
    cur.close()
    conn.close()

    return render_template('tournament_detail.html',
                           t=dict(t), lb=lb,
                           my_entry=dict(my_entry) if my_entry else None,
                           payouts=[dict(p) for p in payouts],
                           balance=balance,
                           user_id=user_id,
                           is_admin=session.get('is_admin'))


@socketio.on('join_tournament_room')
def on_join_tournament_room(data):
    join_room(f'tournament_{int(data.get("tid", 0))}')

@app.route('/api/admin/tournaments', methods=['POST'])
@login_required
@admin_required
def api_admin_create_tournament():
    d = request.json or {}
    name        = d.get('name', '').strip()[:100]
    description = d.get('description', '').strip()
    game        = d.get('game', 'slots')
    entry_fee   = int(d.get('entry_fee', 0))
    start_coins = int(d.get('start_coins', 1000))
    starts_at   = d.get('starts_at')
    ends_at     = d.get('ends_at')
    max_players = int(d.get('max_players', 100))

    if not name or not starts_at or not ends_at:
        return jsonify({'ok': False, 'error': 'Trūkst lauki'}), 400

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO tournaments
            (name, description, game, entry_fee, start_coins,
             starts_at, ends_at, max_players, created_by, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
            CASE WHEN %s::timestamptz <= NOW() THEN 'active' ELSE 'upcoming' END)
        RETURNING id
    """, (name, description, game, entry_fee, start_coins,
          starts_at, ends_at, max_players, session['user_id'], starts_at))
    tid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'id': tid})

@app.route('/api/tournaments/<int:tid>/leaderboard')
@login_required
def api_tournament_leaderboard(tid):
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT prize_pool FROM tournaments WHERE id = %s", (tid,))
    t = cur.fetchone()
    cur.close()
    conn.close()
    lb = _tournament_leaderboard(get_db_connection(), tid, limit=50)
    return jsonify({'lb': lb, 'prize_pool': t['prize_pool'] if t else 0})

@app.route('/api/admin/tournaments/<int:tid>/activate', methods=['POST'])
@login_required
@admin_required
def api_admin_tournament_activate(tid):
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE tournaments SET status = 'active' WHERE id = %s AND status = 'upcoming'",
        (tid,)
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

def _start_tournament_scheduler():
    """
    Runs every 30 s. Activates tournaments whose start_time has passed,
    resolves those whose end_time has passed.
    """
    def loop():
        while True:
            socketio.sleep(30)
            try:
                conn = get_db_connection()
                cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

                # Activate upcoming tournaments
                cur.execute("""
                    UPDATE tournaments SET status = 'active'
                    WHERE status = 'upcoming' AND starts_at <= NOW()
                    RETURNING id, name
                """)
                for row in cur.fetchall():
                    app.logger.info(f'[tournament] activated {row["id"]} {row["name"]}')
                    socketio.emit('tournament_update', {'tournament_id': row['id']})

                conn.commit()

                # Find active tournaments past end time
                cur.execute("""
                    SELECT id FROM tournaments
                    WHERE status = 'active' AND ends_at <= NOW()
                """)
                expired = [r['id'] for r in cur.fetchall()]
                cur.close()
                conn.close()

                for tid in expired:
                    try:
                        conn2 = get_db_connection()
                        _resolve_tournament(conn2, tid)
                        conn2.close()
                    except Exception as e:
                        app.logger.error(f'[tournament] resolve error tid={tid}: {e}')

            except Exception as e:
                app.logger.error(f'[tournament scheduler] {e}')

    socketio.start_background_task(loop)

def _resolve_tournament(conn, tid: int):
    """Shared logic used by scheduler and admin manual resolve."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM tournaments WHERE id = %s", (tid,))
    t = cur.fetchone()
    if not t or t['status'] == 'finished':
        cur.close()
        return

    lb    = _tournament_leaderboard(conn, tid, limit=100)
    tiers = _tournament_payout_tiers(t['prize_pool'], len(lb))

    cur2 = conn.cursor()
    for tier in tiers:
        if tier['place'] - 1 >= len(lb):
            break
        winner = lb[tier['place'] - 1]
        uid    = winner['user_id']
        amount = tier['amount']
        if amount <= 0:
            continue
        w_bal = get_or_create_balance(conn, uid) + amount
        cur2.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (w_bal, uid))
        cur2.execute(
            """INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
               VALUES (%s, %s, 'tournament:payout', %s, %s)""",
            (uid, amount, str(tid), w_bal)
        )
        cur2.execute(
            """INSERT INTO tournament_payouts (tournament_id, user_id, place, amount)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (tid, uid, tier['place'], amount)
        )

    cur2.execute("UPDATE tournaments SET status = 'finished' WHERE id = %s", (tid,))
    conn.commit()
    cur2.close()
    cur.close()

    socketio.emit('tournament_finished', {
        'tournament_id': tid,
        'leaderboard':   lb[:10],
        'tiers':         tiers,
    }, room=f'tournament_{tid}')
    app.logger.info(f'[tournament] resolved tid={tid}, paid {len(tiers)} players')

@app.route('/api/win_card')
@login_required
def api_win_card():
    username   = session.get('user_name', 'Spēlētājs')
    game       = request.args.get('game', 'casino')
    amount     = float(request.args.get('amount', 0))
    multiplier = request.args.get('mult')
    mult_val   = float(multiplier) if multiplier else None

    if amount <= 0:
        return '', 400

    png = generate_win_card(username, game, amount, mult_val)
    return Response(png, mimetype='image/png', headers={
        'Content-Disposition': f'inline; filename="win_{game}.png"'
    })



# @app.route('/bumbox')
# @login_required
# def bumbox():
#     user_id  = session['user_id']
#     user_dir = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{user_id}")
#     used_mb  = round(get_dir_size(user_dir) / (1024 * 1024), 2)

#     conn  = get_db_connection()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cur.execute("SELECT * FROM user_files WHERE user_id = %s", (user_id,))
#     files = cur.fetchall()
#     cur.close()
#     conn.close()
#     return render_template('bumbox.html', files=files, used_mb=used_mb, max_mb=100)

# @app.route('/download/<int:file_id>')
# @login_required
# def download_file(file_id):
#     conn = get_db_connection()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cur.execute(
#         "SELECT * FROM user_files WHERE id = %s AND user_id = %s",
#         (file_id, session['user_id'])
#     )
#     file_data = cur.fetchone()
#     cur.close()
#     conn.close()

#     if file_data:
#         user_dir = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{session['user_id']}")
#         return send_from_directory(user_dir, file_data['filename'], as_attachment=True)

#     flash("Fails nav atrasts.", 'error')
#     return redirect(url_for('bumbox'))

# @app.route('/upload', methods=['GET', 'POST'])
# @login_required
# def upload():
#     if request.method == 'GET':
#         return redirect(url_for('bumbox'))
#     if 'file' not in request.files:
#         flash('Sistēmas kļūda: fails netika saņemts.', 'error')
#         return redirect(url_for('bumbox'))

#     f = request.files['file']
#     if f.filename == '':
#         flash('Lūdzu, vispirms izvēlies failu!', 'error')
#         return redirect(url_for('bumbox'))
#     if not allowed_file(f.filename):
#         flash('Šāds faila tips nav atļauts!', 'error')
#         return redirect(url_for('bumbox'))

#     filename = secure_filename(f.filename)
#     user_id  = session['user_id']
#     user_dir = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{user_id}")
#     os.makedirs(user_dir, exist_ok=True)

#     if get_dir_size(user_dir) >= MAX_FOLDER_SIZE:
#         flash('Tava krātuve ir pilna (100MB)! Izdzēs kaut ko.', 'error')
#         return redirect(url_for('bumbox'))

#     f.save(os.path.join(user_dir, filename))
#     conn = get_db_connection()
#     cur = conn.cursor()
#     cur.execute("INSERT INTO user_files (filename, user_id) VALUES (%s, %s)", (filename, user_id))
#     conn.commit()
#     cur.close()
#     conn.close()
#     flash(f'Fails "{filename}" veiksmīgi augšupielādēts!', 'success')
#     return redirect(url_for('bumbox'))

# @app.route('/delete/<int:file_id>', methods=['POST'])
# @login_required
# def delete_file(file_id):
#     conn = get_db_connection()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cur.execute(
#         "SELECT * FROM user_files WHERE id = %s AND user_id = %s",
#         (file_id, session['user_id'])
#     )
#     file = cur.fetchone()
#     if file:
#         user_dir  = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{session['user_id']}")
#         file_path = os.path.join(user_dir, file['filename'])
#         if os.path.exists(file_path):
#             os.remove(file_path)
#         cur.execute("DELETE FROM user_files WHERE id = %s", (file_id,))
#         conn.commit()
#         flash(f'Fails "{file["filename"]}" izdzēsts.', 'success')
#     else:
#         flash('Fails nav atrasts vai nav tava īpašums.', 'error')
#     cur.close()
#     conn.close()
#     return redirect(url_for('bumbox'))

# @app.route('/canvas')
# @login_required
# def canvas():
#     return render_template('canvas.html')

# @app.route('/api/canvas_data')
# def get_canvas_data():
#     conn   = get_db_connection()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cur.execute("SELECT x, y, color FROM canvas")
#     pixels = cur.fetchall()
#     cur.execute("""
#         SELECT u.username, s.count FROM canvas_scores s
#         JOIN users u ON s.user_id = u.id ORDER BY s.count DESC LIMIT 10
#     """)
#     scores = cur.fetchall()
#     cur.close()
#     conn.close()
#     return jsonify({"pixels": [dict(p) for p in pixels], "scores": [dict(s) for s in scores]})

# @app.route('/api/place', methods=['POST'])
# def place_pixel():
#     if 'user_id' not in session:
#         return jsonify({"error": "No auth"}), 401
#     data = request.json
#     x, y = data.get('x'), data.get('y')
#     color = data.get('color', '')
#     if not isinstance(x, int) or not isinstance(y, int):
#         return jsonify({"error": "Invalid coordinates"}), 400
#     if not re.match(r'^#[0-9a-fA-F]{6}$', color):
#         return jsonify({"error": "Invalid color"}), 400

#     uid  = session['user_id']
#     conn = get_db_connection()
#     cur = conn.cursor()
#     cur.execute("""
#         INSERT INTO canvas (x, y, color, user_id) VALUES (%s, %s, %s, %s)
#         ON CONFLICT (x, y) DO UPDATE SET color = EXCLUDED.color, user_id = EXCLUDED.user_id
#     """, (x, y, color, uid))
#     cur.execute(
#         "INSERT INTO canvas_scores (user_id, count) VALUES (%s, 1) ON CONFLICT (user_id) DO UPDATE SET count = canvas_scores.count + 1",
#         (uid,)
#     )
#     conn.commit()
#     cur.close()
#     conn.close()
#     return jsonify({"status": "ok"})

# # --- ADMIN ---
# @app.route('/api/clear_canvas', methods=['POST'])
# @login_required
# @admin_required
# def clear_canvas():
#     conn = get_db_connection()
#     cur = conn.cursor()
#     cur.execute("DELETE FROM canvas")
#     cur.execute("DELETE FROM canvas_scores")
#     conn.commit()
#     cur.close()
#     conn.close()
#     return jsonify({"status": "canvas notīrīts"})

# @app.route('/admin/set_admin/<int:user_id>', methods=['POST'])
# @login_required
# @admin_required
# def set_admin(user_id):
#     conn = get_db_connection()
#     cur = conn.cursor()
#     cur.execute("UPDATE users SET is_admin = 1 WHERE id = %s", (user_id,))
#     conn.commit()
#     cur.close()
#     conn.close()
#     return jsonify({"status": "ok"})

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

    # Lobby is open to all logged-in users
    if group_id != 1:
        cur.execute(
            "SELECT 1 FROM chat_members WHERE group_id = %s AND user_id = %s",
            (group_id, user_id)
        )
        if not cur.fetchone():
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


# ==========================================
#  FRIENDS & DIRECT MESSAGES
# ==========================================

@app.route('/api/friends/search')
@login_required
def api_friends_search():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, username FROM users
        WHERE username ILIKE %s AND id != %s
        LIMIT 10
    """, (f'%{q}%', session['user_id']))
    users = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(u) for u in users])


@app.route('/api/friends/request', methods=['POST'])
@login_required
def api_friends_request():
    user_id     = session['user_id']
    receiver_id = int((request.json or {}).get('user_id', 0))
    if receiver_id == user_id:
        return jsonify({'ok': False, 'error': 'Nevari sūtīt sev'}), 400
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO friendships (sender_id, receiver_id)
            VALUES (%s, %s)
            ON CONFLICT (sender_id, receiver_id) DO NOTHING
        """, (user_id, receiver_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'ok': False, 'error': str(e)}), 500
    cur.close()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/friends/respond', methods=['POST'])
@login_required
def api_friends_respond():
    user_id   = session['user_id']
    data      = request.json or {}
    sender_id = int(data.get('user_id', 0))
    action    = data.get('action')  # 'accept' or 'reject'
    if action not in ('accept', 'reject'):
        return jsonify({'ok': False, 'error': 'Nepareiza darbība'}), 400
    status = 'accepted' if action == 'accept' else 'rejected'
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE friendships SET status = %s
        WHERE sender_id = %s AND receiver_id = %s
    """, (status, sender_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
    if action == 'accept':
        conn2 = get_db_connection()
        check_achievements(conn2, user_id, {'friend_added': True})
        conn2.close()
    return jsonify({'ok': True})


@app.route('/api/friends/list')
@login_required
def api_friends_list():
    user_id = session['user_id']
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # accepted friends
    cur.execute("""
        SELECT u.id, u.username FROM friendships f
        JOIN users u ON u.id = CASE
            WHEN f.sender_id = %s THEN f.receiver_id
            ELSE f.sender_id END
        WHERE (f.sender_id = %s OR f.receiver_id = %s)
          AND f.status = 'accepted'
    """, (user_id, user_id, user_id))
    friends = cur.fetchall()
    # pending requests received
    cur.execute("""
        SELECT u.id, u.username FROM friendships f
        JOIN users u ON u.id = f.sender_id
        WHERE f.receiver_id = %s AND f.status = 'pending'
    """, (user_id,))
    pending = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({
        'friends': [dict(f) for f in friends],
        'pending': [dict(p) for p in pending]
    })

@app.route('/api/users/search')
@login_required
def api_users_search():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT u.id, u.username,
               COALESCE(w.balance, 0)  AS balance,
               COALESCE(p.avatar_path, '') AS avatar_path,
               COALESCE(p.title, '')   AS title,
               COUNT(ct.id)            AS total_games
        FROM users u
        LEFT JOIN wallets w             ON w.user_id  = u.id
        LEFT JOIN profiles p            ON p.user_id  = u.id
        LEFT JOIN casino_transactions ct ON ct.user_id = u.id
        WHERE u.username ILIKE %s
        GROUP BY u.id, w.balance, p.avatar_path, p.title
        ORDER BY u.username
        LIMIT 8
    """, (f'%{q}%',))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/dm/<int:other_id>', methods=['GET'])
@login_required
def api_dm_get(other_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # verify they are friends
    cur.execute("""
        SELECT 1 FROM friendships
        WHERE ((sender_id=%s AND receiver_id=%s)
            OR (sender_id=%s AND receiver_id=%s))
          AND status='accepted'
    """, (user_id, other_id, other_id, user_id))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Nav draugi'}), 403
    cur.execute("""
        SELECT dm.*, u.username AS sender_name
        FROM direct_messages dm
        JOIN users u ON u.id = dm.sender_id
        WHERE (sender_id=%s AND receiver_id=%s)
           OR (sender_id=%s AND receiver_id=%s)
        ORDER BY created_at ASC LIMIT 200
    """, (user_id, other_id, other_id, user_id))
    msgs = cur.fetchall()
    # mark as read
    cur2 = conn.cursor()
    cur2.execute("""
        UPDATE direct_messages SET is_read=TRUE
        WHERE receiver_id=%s AND sender_id=%s AND is_read=FALSE
    """, (user_id, other_id))
    conn.commit()
    cur2.close()
    cur.close()
    conn.close()
    return jsonify([dict(m) for m in msgs])


@app.route('/api/dm/<int:other_id>', methods=['POST'])
@login_required
def api_dm_send(other_id):
    user_id = session['user_id']
    text    = (request.json or {}).get('message', '').strip()
    if not text or len(text) > 2000:
        return jsonify({'error': 'Nederīga ziņa'}), 400
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT 1 FROM friendships
        WHERE ((sender_id=%s AND receiver_id=%s)
            OR (sender_id=%s AND receiver_id=%s))
          AND status='accepted'
    """, (user_id, other_id, other_id, user_id))
    if not cur.fetchone():
        conn.close()
        return jsonify({'error': 'Nav draugi'}), 403
    cur2 = conn.cursor()
    cur2.execute("""
        INSERT INTO direct_messages (sender_id, receiver_id, message)
        VALUES (%s, %s, %s)
    """, (user_id, other_id, sanitize_message(text)))
    conn.commit()
    cur2.close()
    cur.close()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/dm/unread')
@login_required
def api_dm_unread():
    user_id = session['user_id']
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT sender_id, COUNT(*) AS cnt
        FROM direct_messages
        WHERE receiver_id=%s AND is_read=FALSE
        GROUP BY sender_id
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({str(r['sender_id']): r['cnt'] for r in rows})








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
        ttl     = redis.ttl(key)
        hours   = ttl // 3600
        minutes = (ttl % 3600) // 60
        return jsonify({
            'ok':      False,
            'message': f'Jau saņemts šodien! Atkal pieejams pēc {hours}h {minutes}m.',
            'ttl':     ttl
        }), 429

    conn    = get_db_connection()
    streak  = _get_and_update_streak(conn, user_id)
    balance = get_or_create_balance(conn, user_id)

    # Base bonus + streak scaling
    bonus = DAILY_BONUS_AMOUNT
    if streak == 7:
        bonus = 1000
    elif streak == 30:
        bonus = 1000
    elif streak >= 3:
        bonus = int(DAILY_BONUS_AMOUNT * 1.5)

    balance += bonus
    cur = conn.cursor()
    cur.execute(
        "UPDATE wallets SET balance = %s WHERE user_id = %s",
        (round(balance, 2), user_id)
    )
    cur.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
           VALUES (%s, %s, %s, %s)""",
        (user_id, bonus, 'daily_bonus', round(balance, 2))
    )

    # Day 30 reward — free card_gold cosmetic
    cosmetic_awarded = None
    if streak == 30:
        cur.execute("SELECT id FROM cosmetics WHERE slug = 'card_gold'")
        cos = cur.fetchone()
        if cos:
            cur.execute(
                """INSERT INTO user_cosmetics (user_id, cosmetic_id)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                (user_id, cos[0])
            )
            cosmetic_awarded = 'Zelta kārtis'

    conn.commit()
    cur.close()
    conn.close()

    redis.setex(key, 86400, '1')

    msg = f'+{bonus} monētas! Sērija: {streak} dienas 🔥'
    if streak == 7:
        msg = f'🎉 7 dienu sērija! +{bonus} monētas!'
    if streak == 30:
        msg = f'🏆 30 dienu sērija! +{bonus} monētas + Zelta kārtis!'

    check_achievements(conn, user_id, {'daily': True})

    return jsonify({
        'ok':               True,
        'credits':          bonus,
        'balance':          round(balance, 2),
        'streak':           streak,
        'message':          msg,
        'cosmetic_awarded': cosmetic_awarded,
    })

# =============================================
#  CASINO ROUTES
# =============================================

# --- PALĪGFUNKCIJAS ---
def get_or_create_balance(conn, user_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO wallets (user_id, balance)
        VALUES (%s, 1000)
        ON CONFLICT (user_id) DO NOTHING
    """, (user_id,))
    conn.commit()
    cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    return float(row['balance'])

def log_wallet(conn, user_id, delta, reason, ref_id=None):
    """Write any balance change to wallet_log. Use for non-casino credits (daily, refunds, etc.)"""
    cur = conn.cursor()
    cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    balance_after = float(row[0]) if row else 0.0
    cur.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
           VALUES (%s, %s, %s, %s, %s)""",
        (user_id, round(delta, 2), reason, ref_id, round(balance_after, 2))
    )
    cur.close()

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
    if winnings >= _FEED_MIN_WIN:
        _post_feed(user_id, 'jackpot', 'slots', winnings,
               message=f"{result}")
    cur.close()
    conn.close()

    conn2 = get_db_connection()
    new_ach = check_achievements(conn2, user_id, {
        'game': 'slots', 'net': winnings, 'result': result
    })
    conn2.close()

    return jsonify({
        "reels": reels,
        "result": result,
        "winnings": winnings,
        "net": winnings,
        "balance": balance,
        "achievements": new_ach,
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

# @app.route('/api/casino/highlow/start', methods=['POST'])
# @login_required
# def hl_start():
#     deck = CARD_DECK_HL.copy()
#     random.shuffle(deck)
#     card = deck.pop()
#     session['hl_deck'] = deck
#     session['hl_current'] = card
#     session['hl_streak'] = 0
#     return jsonify({"card": card, "value": card_value(card), "streak": 0})

# @app.route('/api/casino/highlow/guess', methods=['POST'])
# @login_required
# def hl_guess():
#     user_id = session['user_id']
#     data = request.json
#     guess = data.get('guess')
#     bet = float(data.get('bet', 10))

#     deck = session.get('hl_deck', [])
#     current = session.get('hl_current')
#     streak = session.get('hl_streak', 0)
#     if not current or not deck:
#         return jsonify({"error": "Sāc jaunu spēli"}), 400

#     next_card = deck.pop()
#     curr_val = card_value(current)
#     next_val = card_value(next_card)

#     if (guess == 'high' and next_val > curr_val) or \
#        (guess == 'low' and next_val < curr_val):
#         correct = True
#         streak += 1
#     elif next_val == curr_val:
#         correct = None
#         streak = streak
#     else:
#         correct = False
#         streak = 0

#     session['hl_current'] = next_card
#     session['hl_deck'] = deck
#     session['hl_streak'] = streak

#     winnings = 0
#     balance = None
#     if correct is True:
#         multiplier = 1 + (streak * 0.5)
#         winnings = bet * multiplier
#         winnings = winnings - bet
#         conn = get_db_connection()
#         balance = get_or_create_balance(conn, user_id)
#         balance += winnings
#         cur = conn.cursor()
#         cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
#         record_transaction(conn, user_id, 'highlow', bet, f"Pareizi! Streak {streak}", winnings, balance)
#         conn.commit()
#         cur.close()
#         conn.close()
#     elif correct is False:
#         conn = get_db_connection()
#         balance = get_or_create_balance(conn, user_id)
#         balance -= bet
#         if balance < 0: balance = 0
#         cur = conn.cursor()
#         cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
#         record_transaction(conn, user_id, 'highlow', bet, "Nepareizi", -bet, balance)
#         conn.commit()
#         cur.close()
#         conn.close()

#     return jsonify({
#         "next_card": next_card,
#         "next_value": next_val,
#         "correct": correct,
#         "streak": streak,
#         "winnings": winnings,
#         "net": winnings, 
#         "balance": balance
#     })

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

# @app.route('/api/casino/bingo/new_card', methods=['POST'])
# @login_required
# def bingo_new_card():
#     user_id = session['user_id']
#     data = request.json
#     bet = float(data.get('bet', 10))
#     conn = get_db_connection()
#     balance = get_or_create_balance(conn, user_id)
#     if bet <= 0 or bet > balance:
#         conn.close()
#         return jsonify({"error": "Nepareizs likmjums"}), 400

#     # Deduct bet immediately — loss is the default until bingo is hit
#     balance -= bet
#     cur = conn.cursor()
#     cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
#     record_transaction(conn, user_id, 'bingo', bet, 'Karte izsniegta (zaudējums)', -bet, balance)
#     conn.commit()
#     cur.close()

#     ranges = [(1,15),(16,30),(31,45),(46,60),(61,75)]
#     card = []
#     for col_range in ranges:
#         nums = random.sample(range(col_range[0], col_range[1]+1), 5)
#         card.append(nums)
#     card_t = [[card[col][row] for col in range(5)] for row in range(5)]
#     card_t[2][2] = 'FREE'

#     session['bingo_card'] = card_t
#     session['bingo_bet'] = bet
#     session['bingo_called'] = []
#     conn.close()
#     return jsonify({"card": card_t, "balance": round(balance, 2)})

# @app.route('/api/casino/bingo/call', methods=['POST'])
# @login_required
# def bingo_call():
#     user_id = session['user_id']
#     card = session.get('bingo_card')
#     called = session.get('bingo_called', [])
#     bet = float(session.get('bingo_bet', 10))
#     if not card:
#         return jsonify({"error": "Nav aktīvas spēles"}), 400

#     user_id = session['user_id']
#     card = session.get('bingo_card')
#     all_nums = list(range(1, 76))
#     remaining = [n for n in all_nums if n not in called]
#     if not remaining:
#         return jsonify({"error": "Visi skaitļi izsaukti"}), 400

#     new_num = random.choice(remaining)
#     called.append(new_num)
#     session['bingo_called'] = called

#     def check_bingo(card, called_set):
#         for row in card:
#             if all(c == 'FREE' or c in called_set for c in row):
#                 return True
#         for col in range(5):
#             if all(card[row][col] == 'FREE' or card[row][col] in called_set for row in range(5)):
#                 return True
#         if all(card[i][i] == 'FREE' or card[i][i] in called_set for i in range(5)):
#             return True
#         if all(card[i][4-i] == 'FREE' or card[i][4-i] in called_set for i in range(5)):
#             return True
#         return False

#     called_set = set(called)
#     has_bingo = check_bingo(card, called_set)
#     winnings = 0
#     balance = None

#     if has_bingo:
#         multiplier = max(2, 30 - len(called))
#         winnings = round(bet * multiplier, 2)   # bet already gone, add full payout
#         conn = get_db_connection()
#         balance = get_or_create_balance(conn, user_id)
#         balance += winnings
#         cur = conn.cursor()
#         cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (balance, user_id))
#         record_transaction(conn, user_id, 'bingo', bet, f"BINGO! {len(called)} izsaukumi, x{multiplier}", winnings, balance)
#         conn.commit()
#         cur.close()
#         conn.close()
#         for k in ['bingo_card','bingo_bet','bingo_called']:
#             session.pop(k, None)

#     return jsonify({
#             "number": new_num,
#             "called": called,
#             "bingo": has_bingo,
#             "winnings": winnings,
#             "balance": round(balance, 2) if balance is not None else None
#         })

@app.route('/api/casino/highlow/start', methods=['POST'])
@login_required
def hl_start_route():
    return hl_start(app, session, request)

@app.route('/api/casino/highlow/guess', methods=['POST'])
@login_required
def hl_guess_route():
    return hl_guess(app, session, request)

@app.route('/api/casino/bingo/new_card', methods=['POST'])
@login_required
def bingo_new_card_route():
    return bingo_new_card(app, session, request)

@app.route('/api/casino/bingo/call', methods=['POST'])
@login_required
def bingo_call_route():
    return bingo_call(app, session, request)

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

@app.route('/casino/videopoker')
@login_required
def video_poker():
    user_id = session['user_id']
    conn = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_videopoker.html', balance=balance)

# Keep old URL working
@app.route('/casino/poker')
@login_required
def poker_redirect():
    return redirect(url_for('video_poker'))

@app.route('/api/casino/videopoker/deal', methods=['POST'])
@login_required
def api_poker_deal():
    return poker_deal(app, session, request)

@app.route('/api/casino/videopoker/draw', methods=['POST'])
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

from redis_games import holdem_save_table, holdem_load_table, holdem_load_all_tables

def _init_holdem_tables():
    # import deferred so init_redis() has already run
    return holdem_load_all_tables(_new_holdem_table)

_HOLDEM_TABLES = {}   # filled in after Redis is ready

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
    holdem_save_table(table_id, table)

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
    if not table:
        return
    emit('holdem_state', _public_state(table))
    # Re-send hole cards if this player has an active hand
    uid = session.get('user_id')
    if uid and uid in table['players']:
        p = table['players'][uid]
        if p.get('cards') and not p['folded']:
            emit('holdem_private_cards', {'cards': p['cards']})

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

    equipped_skin_name = None
    if profile and profile.get('equipped_skin'):
        cur.execute("SELECT name FROM cosmetics WHERE id = %s", (profile['equipped_skin'],))
        skin_row = cur.fetchone()
        equipped_skin_name = skin_row['name'] if skin_row else None

    cur.close()
    conn.close()

    is_own_profile = ('user_id' in session and session['user_id'] == owner['id'])

    profile_dict = dict(profile)
    profile_dict['equipped_skin_name'] = equipped_skin_name

    return render_template(
        'profile.html',
        owner=dict(owner),
        profile=profile_dict,
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
@app.route('/api/duels/challenge', methods=['POST'])
@login_required
def api_duel_challenge():
    user_id = session['user_id']
    data    = request.json or {}

    opponent_id = int(data.get('opponent_id', 0))
    event_id    = int(data.get('event_id', 0))
    option_id   = int(data.get('option_id', 0))
    stake       = int(data.get('stake', 0))

    if opponent_id == user_id:
        return jsonify({'ok': False, 'error': 'Nevari izaicināt sevi'}), 400
    if stake < 10:
        return jsonify({'ok': False, 'error': 'Minimālā likme: 10 coins'}), 400

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Event must be open
    cur.execute(
        "SELECT status FROM prediction_events WHERE id = %s", (event_id,)
    )
    event = cur.fetchone()
    if not event or event['status'] != 'open':
        conn.close()
        return jsonify({'ok': False, 'error': 'Notikums nav pieejams'}), 400

    # Option must belong to this event
    cur.execute(
        "SELECT id FROM prediction_options WHERE id = %s AND event_id = %s",
        (option_id, event_id)
    )
    if not cur.fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Nepareiza opcija'}), 400

    # Check challenger has no existing pending duel on this event vs this opponent
    cur.execute("""
        SELECT id FROM prediction_duels
        WHERE challenger_id = %s AND opponent_id = %s
          AND event_id = %s AND status = 'pending'
    """, (user_id, opponent_id, event_id))
    if cur.fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Ielūgums jau nosūtīts'}), 400

    # Deduct stake immediately (held in escrow)
    balance = get_or_create_balance(conn, user_id)
    if balance < stake:
        conn.close()
        return jsonify({'ok': False, 'error': 'Nepietiek monētu'}), 400

    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE wallets SET balance = balance - %s WHERE user_id = %s",
        (stake, user_id)
    )
    cur2.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
           VALUES (%s, %s, 'duel:escrow', %s)""",
        (user_id, -stake, balance - stake)
    )

    cur2.execute("""
        INSERT INTO prediction_duels
            (challenger_id, opponent_id, event_id, challenger_option_id, stake)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (user_id, opponent_id, event_id, option_id, stake))
    duel_id = cur2.fetchone()[0]

    # Notify opponent
    cur2.execute("""
        INSERT INTO notifications (user_id, message)
        VALUES (%s, %s)
    """, (opponent_id,
          f'⚔️ {session["user_name"]} izaicina tevi prognozēs! '
          f'{stake} coins uz spēles. /duels'))

    conn.commit()
    cur.close()
    cur2.close()
    conn.close()

    return jsonify({'ok': True, 'duel_id': duel_id})


@app.route('/api/duels/mine')
@login_required
def api_my_duels():
    user_id = session['user_id']
    conn    = get_db_connection()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            d.*,
            pe.title        AS event_title,
            po_c.label      AS challenger_label,
            po_o.label      AS opponent_label,
            uc.username     AS challenger_name,
            uo.username     AS opponent_name
        FROM prediction_duels d
        JOIN prediction_events  pe  ON pe.id  = d.event_id
        JOIN prediction_options po_c ON po_c.id = d.challenger_option_id
        LEFT JOIN prediction_options po_o ON po_o.id = d.opponent_option_id
        JOIN users uc ON uc.id = d.challenger_id
        JOIN users uo ON uo.id = d.opponent_id
        WHERE d.challenger_id = %s OR d.opponent_id = %s
        ORDER BY d.created_at DESC
        LIMIT 30
    """, (user_id, user_id))
    duels = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify({'duels': duels, 'user_id': user_id})


@app.route('/api/duels/<int:duel_id>/accept', methods=['POST'])
@login_required
def api_duel_accept(duel_id):
    user_id   = session['user_id']
    data      = request.json or {}
    option_id = int(data.get('option_id', 0))

    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT * FROM prediction_duels WHERE id = %s AND opponent_id = %s",
        (duel_id, user_id)
    )
    duel = cur.fetchone()
    if not duel:
        conn.close()
        return jsonify({'ok': False, 'error': 'Duelis nav atrasts'}), 404
    if duel['status'] != 'pending':
        conn.close()
        return jsonify({'ok': False, 'error': 'Duelis vairs nav aktīvs'}), 400

    # Can't pick same option as challenger
    if option_id == duel['challenger_option_id']:
        conn.close()
        return jsonify({'ok': False, 'error': 'Izvēlies citu opciju'}), 400

    # Verify option belongs to same event
    cur.execute(
        "SELECT id FROM prediction_options WHERE id = %s AND event_id = %s",
        (option_id, duel['event_id'])
    )
    if not cur.fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Nepareiza opcija'}), 400

    stake   = duel['stake']
    balance = get_or_create_balance(conn, user_id)
    if balance < stake:
        conn.close()
        return jsonify({'ok': False, 'error': 'Nepietiek monētu'}), 400

    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE wallets SET balance = balance - %s WHERE user_id = %s",
        (stake, user_id)
    )
    cur2.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
           VALUES (%s, %s, 'duel:escrow', %s)""",
        (user_id, -stake, balance - stake)
    )
    cur2.execute("""
        UPDATE prediction_duels
        SET status = 'accepted', opponent_option_id = %s
        WHERE id = %s
    """, (option_id, duel_id))

    # Notify challenger
    cur2.execute("""
        INSERT INTO notifications (user_id, message)
        VALUES (%s, %s)
    """, (duel['challenger_id'],
          f'⚔️ {session["user_name"]} pieņēma tavu izaicinājumu! '
          f'Uz spēles: {stake * 2} coins.'))

    conn.commit()
    cur.close()
    cur2.close()
    conn.close()

    return jsonify({'ok': True})

@app.route('/api/duels/<int:duel_id>/decline', methods=['POST'])
@login_required
def api_duel_decline(duel_id):
    user_id = session['user_id']
    conn    = get_db_connection()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT * FROM prediction_duels WHERE id = %s AND opponent_id = %s AND status='pending'",
        (duel_id, user_id)
    )
    duel = cur.fetchone()
    if not duel:
        conn.close()
        return jsonify({'ok': False, 'error': 'Nav atrasts'}), 404

    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE prediction_duels SET status = 'declined' WHERE id = %s", (duel_id,)
    )
    # Refund challenger
    cur2.execute(
        "UPDATE wallets SET balance = balance + %s WHERE user_id = %s",
        (duel['stake'], duel['challenger_id'])
    )
    cur2.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
           SELECT %s, %s, 'duel:refund',
                  balance FROM wallets WHERE user_id = %s""",
        (duel['challenger_id'], duel['stake'], duel['challenger_id'])
    )
    cur2.execute(
        "INSERT INTO notifications (user_id, message) VALUES (%s, %s)",
        (duel['challenger_id'],
         f'⚔️ {session["user_name"]} noraidīja tavu izaicinājumu. '
         f'{duel["stake"]} coins atgriezti.')
    )
    conn.commit()
    cur.close()
    cur2.close()
    conn.close()
    return jsonify({'ok': True})

def _resolve_duels_for_event(conn, event_id: int, winning_option_id: int):
    """
    Called after prediction_events is resolved.
    Pays out all accepted duels for this event.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM prediction_duels
        WHERE event_id = %s AND status = 'accepted'
    """, (event_id,))
    duels = cur.fetchall()

    cur2 = conn.cursor()
    for d in duels:
        pot = d['stake'] * 2

        if d['challenger_option_id'] == winning_option_id:
            winner_id = d['challenger_id']
            loser_id  = d['opponent_id']
        elif d['opponent_option_id'] == winning_option_id:
            winner_id = d['opponent_id']
            loser_id  = d['challenger_id']
        else:
            # Neither picked winner — refund both (edge case: >2 options)
            for uid in (d['challenger_id'], d['opponent_id']):
                cur2.execute(
                    "UPDATE wallets SET balance = balance + %s WHERE user_id = %s",
                    (d['stake'], uid)
                )
            cur2.execute(
                "UPDATE prediction_duels SET status='resolved', resolved_at=NOW() WHERE id=%s",
                (d['id'],)
            )
            continue

        # Pay winner
        cur2.execute(
            "UPDATE wallets SET balance = balance + %s WHERE user_id = %s",
            (pot, winner_id)
        )
        cur2.execute(
            """INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
               SELECT %s, %s, 'duel:win', %s, balance
               FROM wallets WHERE user_id = %s""",
            (winner_id, pot, str(d['id']), winner_id)
        )
        cur2.execute(
            "UPDATE prediction_duels SET status='resolved', winner_id=%s, resolved_at=NOW() WHERE id=%s",
            (winner_id, d['id'])
        )

        # Notifications
        cur2.execute(
            "INSERT INTO notifications (user_id, message) VALUES (%s, %s)",
            (winner_id, f'⚔️ Tu uzvarēji dueli! +{pot} coins.')
        )
        cur2.execute(
            "INSERT INTO notifications (user_id, message) VALUES (%s, %s)",
            (loser_id, f'⚔️ Tu zaudēji dueli. -{d["stake"]} coins.')
        )

    conn.commit()
    cur.close()
    cur2.close()

@app.route('/api/notifications')
@login_required
def api_notifications():
    user_id = session['user_id']
    conn    = get_db_connection()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, message, is_read, created_at
        FROM notifications
        WHERE user_id = %s
        ORDER BY created_at DESC LIMIT 20
    """, (user_id,))
    notes = [dict(r) for r in cur.fetchall()]
    cur.execute(
        "UPDATE notifications SET is_read = TRUE WHERE user_id = %s AND is_read = FALSE",
        (user_id,)
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'notifications': notes})

def _calculate_tier(stats: dict) -> str:
    """
    Tier logic based on win rate + minimum sample size.
    Returns one of: unranked, bronze, silver, gold, expert, oracle
    """
    n       = stats['total_bets']
    wins    = stats['total_wins']
    staked  = stats['total_staked'] or 1
    returned = stats['total_returned']
    roi     = (returned - staked) / staked * 100   # can be negative

    if n < 5:
        return 'unranked'

    win_rate = wins / n * 100

    if n >= 50 and win_rate >= 70 and roi >= 40:
        return 'oracle'
    if n >= 30 and win_rate >= 65 and roi >= 20:
        return 'expert'
    if n >= 20 and win_rate >= 60:
        return 'gold'
    if n >= 10 and win_rate >= 55:
        return 'silver'
    if n >= 5:
        return 'bronze'
    return 'unranked'


def _update_prediction_stats(conn, event_id: int, winning_option_id: int):
    """
    Recalculate prediction_stats and predictor_tiers for everyone
    who bet on this event. Call after payouts are written.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Fetch all bets on this event
    cur.execute("""
        SELECT user_id, stake, payout, (option_id = %s) AS won
        FROM predictions
        WHERE event_id = %s
    """, (winning_option_id, event_id))
    bets = cur.fetchall()

    cur2 = conn.cursor()
    for b in bets:
        uid     = b['user_id']
        won     = b['won']
        stake   = int(b['stake'])
        payout  = int(b['payout'] or 0)

        # Upsert prediction_stats
        cur2.execute("""
            INSERT INTO prediction_stats
                (user_id, total_bets, total_wins, total_staked,
                 total_returned, current_streak, best_streak)
            VALUES (%s, 1, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                total_bets     = prediction_stats.total_bets + 1,
                total_wins     = prediction_stats.total_wins + %s,
                total_staked   = prediction_stats.total_staked + %s,
                total_returned = prediction_stats.total_returned + %s,
                current_streak = CASE
                    WHEN %s THEN prediction_stats.current_streak + 1
                    ELSE 0 END,
                best_streak    = GREATEST(
                    prediction_stats.best_streak,
                    CASE WHEN %s THEN prediction_stats.current_streak + 1
                    ELSE prediction_stats.best_streak END),
                updated_at = NOW()
        """, (
            uid,
            1 if won else 0, stake, payout,
            1 if won else 0, 1 if won else 0,   # INSERT values
            1 if won else 0, stake, payout,       # UPDATE values
            won, won                              # streak logic
        ))

        # Recalculate tier
        cur2.execute("""
            SELECT total_bets, total_wins, total_staked, total_returned
            FROM prediction_stats WHERE user_id = %s
        """, (uid,))
        stats = cur2.fetchone()
        if stats:
            tier = _calculate_tier(stats)
            cur2.execute("""
                INSERT INTO predictor_tiers (user_id, tier, tier_since)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET tier = EXCLUDED.tier,
                        tier_since = CASE
                            WHEN predictor_tiers.tier != EXCLUDED.tier THEN NOW()
                            ELSE predictor_tiers.tier_since END
            """, (uid, tier))

    conn.commit()
    cur.close()
    cur2.close()

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

    events = [dict(e) for e in events]
    event_ids = [e['id'] for e in events]
    options_by_event = {}
    if event_ids:
        cur.execute("""
            SELECT po.event_id, po.id, po.label,
                COALESCE(pv.total_stake, 0)  AS volume,
                COALESCE(pv.backer_count, 0) AS backers
            FROM prediction_options po
            LEFT JOIN prediction_volume pv ON pv.option_id = po.id
            WHERE po.event_id = ANY(%s)
            ORDER BY po.event_id, po.id
        """, (event_ids,))
        for o in cur.fetchall():
            options_by_event.setdefault(o['event_id'], []).append(dict(o))
    for e in events:
        e['options'] = options_by_event.get(e['id'], [])

    balance = get_or_create_balance(conn, session['user_id'])
    cur.close()
    conn.close()
    return render_template('predictions.html',
                           events=events,
                           positions=positions,
                           balance=balance,
                           is_admin=session.get('is_admin', False))


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
    return render_template('prediction_detailed.html',
                           event=dict(event),
                           options=[dict(o) for o in options],
                           my_position=dict(my_position) if my_position else None,
                           recent_bets=[dict(b) for b in recent_bets],
                           balance=balance,
                           is_admin=session.get('is_admin', False))


@app.route('/api/predictions/create', methods=['POST'])
@login_required
def api_prediction_create():
    user_id = session['user_id']
    conn    = get_db_connection()
    tier    = _get_user_tier(conn, user_id)
    is_admin = session.get('is_admin', False)

    # Must be expert+ or admin
    if not is_admin and not _tier_gte(tier, 'expert'):
        conn.close()
        return jsonify({
            'error': 'Vajadzīgs Expert līmenis vai augstāks'
        }), 403

    data        = request.json or {}
    title       = data.get('title', '').strip()[:200]
    description = data.get('description', '').strip()
    category    = data.get('category', 'general')
    closes_at   = data.get('closes_at')
    options     = data.get('options', [])
    min_tier    = data.get('min_tier', 'unranked')

    # Experts can only create markets accessible to everyone or bronze+
    # Oracles can gate up to gold
    # Admins can gate to any tier
    allowed_min_tiers = {
        'expert': ['unranked', 'bronze', 'silver'],
        'oracle': ['unranked', 'bronze', 'silver', 'gold'],
    }
    if not is_admin:
        allowed = allowed_min_tiers.get(tier, ['unranked'])
        if min_tier not in allowed:
            min_tier = 'unranked'

    if not title or not closes_at or len(options) < 2:
        conn.close()
        return jsonify({
            'error': 'Nepieciešams nosaukums, datums un vismaz 2 opcijas'
        }), 400

    options = [o.strip()[:100] for o in options if o.strip()]

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO prediction_events
                (title, description, category, created_by, closes_at,
                 min_tier, creator_tier)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (title, description, category, user_id,
              closes_at, min_tier, tier))
        event_id = cur.fetchone()[0]

        equal_price = round(1.0 / len(options), 4)
        for label in options:
            cur.execute("""
                INSERT INTO prediction_options (event_id, label, price)
                VALUES (%s, %s, %s)
            """, (event_id, label, equal_price))

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True, 'event_id': event_id})

    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': str(e)}), 500


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

    # Tier gate
    user_tier = _get_user_tier(conn, user_id)
    if not _tier_gte(user_tier, event['min_tier']):
        conn.close()
        return jsonify({
            'error': f'Šis tirgus prasa {event["min_tier"]} līmeni. '
                     f'Tavs līmenis: {user_tier}'
        }), 403

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

        check_achievements(conn, w['user_id'], {'pred_event': True})

    cur2.execute("""
        UPDATE prediction_events
        SET status = 'resolved', outcome = %s, resolves_at = NOW()
        WHERE id = %s
    """, (winning_option['label'], event_id))

    conn.commit()

    _update_prediction_stats(conn, event_id, winning_option_id)
    _resolve_duels_for_event(conn, event_id, winning_option_id)  # before close

    socketio.emit('prediction_resolved', {
        'event_id':       event_id,
        'winning_option': dict(winning_option),
        'total_pot':      total_pot,
        'winner_count':   len(winners)
    }, room=f'prediction_{event_id}')

    cur.close()
    cur2.close()
    conn.close()

    return jsonify({'ok': True, 'total_pot': total_pot, 'winners': len(winners)})

@app.route('/api/predictions/leaderboard')
@login_required
def api_prediction_leaderboard():
    """
    Top predictors by ROI (min 10 bets).
    Returns rank, tier badge, win rate, profit.
    """
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            u.id,
            u.username,
            COALESCE(p.display_name, u.username) AS display_name,
            ps.total_bets,
            ps.total_wins,
            ps.total_staked,
            ps.total_returned,
            ps.best_streak,
            pt.tier,
            ROUND(ps.total_wins::numeric / NULLIF(ps.total_bets, 0) * 100, 1) AS win_rate,
            (ps.total_returned - ps.total_staked) AS profit,
            ROUND(
                (ps.total_returned - ps.total_staked)::numeric
                / NULLIF(ps.total_staked, 1) * 100, 1
            ) AS roi_pct,
            RANK() OVER (
                ORDER BY
                    (ps.total_returned - ps.total_staked) DESC,
                    ps.total_wins DESC
            ) AS rank
        FROM prediction_stats ps
        JOIN users u ON u.id = ps.user_id
        LEFT JOIN profiles p ON p.user_id = ps.user_id
        LEFT JOIN predictor_tiers pt ON pt.user_id = ps.user_id
        WHERE ps.total_bets >= 5
        ORDER BY profit DESC
        LIMIT 50
    """)
    rows = cur.fetchall()

    # Also fetch caller's own stats even if not top 50
    my_stats = None
    user_id  = session['user_id']
    cur.execute("""
        SELECT ps.*, pt.tier,
            ROUND(ps.total_wins::numeric / NULLIF(ps.total_bets, 0) * 100, 1) AS win_rate,
            (ps.total_returned - ps.total_staked) AS profit
        FROM prediction_stats ps
        LEFT JOIN predictor_tiers pt ON pt.user_id = ps.user_id
        WHERE ps.user_id = %s
    """, (user_id,))
    my_stats = cur.fetchone()

    cur.close()
    conn.close()

    TIER_ICONS = {
        'oracle':   '🔮',
        'expert':   '⭐',
        'gold':     '🥇',
        'silver':   '🥈',
        'bronze':   '🥉',
        'unranked': '—',
    }

    def fmt(row):
        r = dict(row)
        r['tier_icon'] = TIER_ICONS.get(r.get('tier', 'unranked'), '—')
        return r

    return jsonify({
        'leaderboard': [fmt(r) for r in rows],
        'my_stats':    fmt(my_stats) if my_stats else None,
    })

@socketio.on('watch_prediction')
def watch_prediction(data):
    join_room(f'prediction_{int(data.get("event_id", 0))}')

@socketio.on('unwatch_prediction')
def unwatch_prediction(data):
    leave_room(f'prediction_{int(data.get("event_id", 0))}')

# ==========================================
#  COSMETICS SHOP
# ==========================================
 
 
 

 
 
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

@app.route('/join/<string:invite_code>')
@login_required
def join_club_by_link(invite_code):
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM clubs WHERE invite_code = %s", (invite_code,))
    club = cur.fetchone()
    if not club:
        flash("Ielūguma saite nav derīga.", "error")
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
        check_achievements(conn, user_id, {'club_action': 'created'})
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
def club_detailed(club_id):
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



    # ==========================================
#  ADMIN PANEL ROUTES
# ==========================================

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    conn = get_db_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT u.id, u.username, u.is_admin, u.created_at,
               COALESCE(w.balance, 0) AS balance,
               (SELECT COUNT(*) FROM casino_transactions WHERE user_id = u.id) AS game_count
        FROM users u
        LEFT JOIN wallets w ON u.id = w.user_id
        ORDER BY u.id DESC
    """)
    users = cur.fetchall()

    cur.execute("""
        SELECT t.*, u.username
        FROM casino_transactions t
        JOIN users u ON t.user_id = u.id
        ORDER BY t.created_at DESC LIMIT 50
    """)
    transactions = cur.fetchall()

    cur.execute("SELECT * FROM prediction_events ORDER BY id DESC")
    predictions = cur.fetchall()

    cur.execute("""
        SELECT c.*, u.username AS owner_name,
               COUNT(cm.user_id) AS member_count
        FROM clubs c
        JOIN users u ON u.id = c.owner_id
        LEFT JOIN club_members cm ON cm.club_id = c.id
        GROUP BY c.id, u.username
        ORDER BY c.created_at DESC
    """)
    clubs = cur.fetchall()

    cur.execute("""
        SELECT u.username, cs.count
        FROM canvas_scores cs
        JOIN users u ON u.id = cs.user_id
        ORDER BY cs.count DESC LIMIT 20
    """)
    canvas_scores = cur.fetchall()

    stats = _get_admin_stats_dict(cur)
    cur.close()
    conn.close()

    return render_template('admin.html',
                           users=users,
                           transactions=transactions,
                           predictions=predictions,
                           clubs=clubs,
                           canvas_scores=canvas_scores,
                           stats=stats)

@app.route('/api/admin/stats')
@login_required
@admin_required
def api_admin_stats():
    """Endpoint for the auto-refreshing stats in admin.html"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    stats = _get_admin_stats_dict(cur)
    cur.close()
    conn.close()
    return jsonify({"ok": True, "stats": stats})

def _get_admin_stats_dict(cur):
    """Helper to aggregate system-wide statistics"""
    cur.execute("SELECT COUNT(*) FROM users")
    u_count = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*) FROM casino_transactions WHERE created_at >= CURRENT_DATE")
    b_today = cur.fetchone()['count']
    
    cur.execute("SELECT SUM(balance) FROM wallets")
    t_bal = cur.fetchone()['sum'] or 0
    
    cur.execute("SELECT COUNT(*) FROM prediction_events WHERE status = 'open'")
    p_open = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*) FROM user_files")
    f_count = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*) FROM chat_messages")
    m_count = cur.fetchone()['count']
    
    return {
        "users": u_count,
        "bets_today": b_today,
        "total_balance": round(t_bal, 2),
        "open_predictions": p_open,
        "files": f_count,
        "messages": m_count
    }

# --- Admin API Actions ---

# ============================================================
# DROP-IN REPLACEMENT for the four broken admin API functions
# Replace everything from api_admin_save_user down to the end
# of the if __name__ == '__main__' block with this content.
# ============================================================

@app.route('/api/admin/user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def api_admin_save_user(user_id):
    data         = request.json or {}
    new_username = data.get('username', '').strip()
    new_balance  = float(data.get('balance', 0))

    if not new_username:
        return jsonify({"ok": False, "error": "Username cannot be empty"})

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET username = %s WHERE id = %s",
                    (new_username, user_id))

        old_bal = get_or_create_balance(conn, user_id)
        delta   = round(new_balance - old_bal, 2)
        cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s",
                    (round(new_balance, 2), user_id))
        cur.execute(
            """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
               VALUES (%s, %s, 'admin_edit', %s)""",
            (user_id, delta, round(new_balance, 2))
        )
        conn.commit()
        return jsonify({"ok": True})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"ok": False, "error": "Username already taken"})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route('/api/admin/user/<int:user_id>/toggle_admin', methods=['POST'])
@login_required
@admin_required
def api_admin_toggle_admin(user_id):
    if user_id == session['user_id']:
        return jsonify({"ok": False, "error": "Cannot change your own admin status"})
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_admin = NOT is_admin WHERE id = %s", (user_id,))
        conn.commit()
        cur.close()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route('/api/admin/prediction/<int:event_id>', methods=['POST'])
@login_required
@admin_required
def api_admin_save_prediction(event_id):
    data     = request.json or {}
    title    = data.get('title', '').strip()
    category = data.get('category', 'general')
    status   = data.get('status', 'open')
    if not title:
        return jsonify({"ok": False, "error": "Title cannot be empty"})
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE prediction_events
               SET title = %s, category = %s, status = %s WHERE id = %s""",
            (title, category, status, event_id)
        )
        conn.commit()
        cur.close()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


# DELETE routes — JS calls DELETE /api/admin/user/<id> etc.
@app.route('/api/admin/user/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def api_admin_delete_user(user_id):
    if user_id == session['user_id']:
        return jsonify({"ok": False, "error": "Cannot delete yourself"})
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        cur.close()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route('/api/admin/transaction/<int:tx_id>', methods=['DELETE'])
@login_required
@admin_required
def api_admin_delete_transaction(tx_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM casino_transactions WHERE id = %s", (tx_id,))
        conn.commit()
        cur.close()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route('/api/admin/prediction/<int:event_id>', methods=['DELETE'])
@login_required
@admin_required
def api_admin_delete_prediction(event_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM prediction_events WHERE id = %s", (event_id,))
        conn.commit()
        cur.close()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route('/api/admin/danger/<string:action>', methods=['POST'])
@login_required
@admin_required
def api_admin_danger(action):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if action == 'clear_canvas':
            cur.execute("DELETE FROM canvas")
            cur.execute("DELETE FROM canvas_scores")
            conn.commit()
            return jsonify({"ok": True, "message": "Canvas cleared"})
        elif action == 'reset_balances':
            cur.execute("UPDATE wallets SET balance = 1000")
            conn.commit()
            return jsonify({"ok": True, "message": "All balances reset to 1000"})
        else:
            return jsonify({"ok": False, "error": "Unknown action"})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()
# ── Paste these routes into admin.py, just before if __name__ == '__main__' ──

@app.route('/api/admin/club/<int:club_id>', methods=['POST'])
@login_required
@admin_required
def api_admin_save_club(club_id):
    data        = request.json or {}
    name        = data.get('name', '').strip()[:50]
    description = data.get('description', '').strip()[:200]
    if not name:
        return jsonify({"ok": False, "error": "Name cannot be empty"})
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE clubs SET name = %s, description = %s WHERE id = %s",
            (name, description, club_id)
        )
        conn.commit()
        cur.close()
        return jsonify({"ok": True})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"ok": False, "error": "Club name already taken"})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route('/api/admin/club/<int:club_id>', methods=['DELETE'])
@login_required
@admin_required
def api_admin_delete_club(club_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM clubs WHERE id = %s", (club_id,))
        conn.commit()
        cur.close()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


@app.route('/api/admin/club/<int:club_id>/reset_invite', methods=['POST'])
@login_required
@admin_required
def api_admin_reset_invite(club_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE clubs SET invite_code = encode(gen_random_bytes(6), 'hex') "
            "WHERE id = %s RETURNING invite_code",
            (club_id,)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return jsonify({"ok": True, "invite_code": row[0]})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()

def _get_and_update_streak(conn, user_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT last_claim, streak FROM daily_claims WHERE user_id = %s",
        (user_id,)
    )
    row = cur.fetchone()
    from datetime import date
    today = date.today()

    if not row:
        streak = 1
        cur.execute(
            """INSERT INTO daily_claims (user_id, last_claim, streak)
               VALUES (%s, %s, %s)""",
            (user_id, today, streak)
        )
    else:
        diff = (today - row['last_claim']).days
        if diff == 1:
            streak = row['streak'] + 1
        elif diff == 0:
            streak = row['streak']  # same day, shouldn't happen but safe
        else:
            streak = 1  # broke the chain
        cur.execute(
            """UPDATE daily_claims SET last_claim = %s, streak = %s
               WHERE user_id = %s""",
            (today, streak, user_id)
        )

    conn.commit()
    cur.close()
    return streak



_CRASH_WAIT = 8   # seconds between rounds (betting phase)
_CRASH_TICK = 0.1 # broadcast interval in seconds

def _start_crash_loop():
    from redis_games import (
        crash_default_state, crash_save, crash_load,
        crash_multiplier_now
    )
    def loop():
        import time, math
        state = crash_default_state()
        state['round_id'] = 1
        crash_save(state)

        while True:
            # ── WAITING phase ──
            state['phase'] = 'waiting'
            crash_save(state)
            socketio.emit('crash_phase', {'phase': 'waiting', 'round_id': state['round_id']})
            socketio.sleep(_CRASH_WAIT)

            # ── RUNNING phase ──
            state['phase']      = 'running'
            state['start_time'] = time.time()
            crash_save(state)
            socketio.emit('crash_phase', {'phase': 'running'})

            while True:
                socketio.sleep(_CRASH_TICK)
                state = crash_load()
                if not state:
                    break
                mult = crash_multiplier_now(state['start_time'])
                socketio.emit('crash_tick', {'mult': mult})

                if mult >= state['crash_point']:
                    break

            # ── CRASHED phase ──
            state = crash_load() or state
            state['phase'] = 'crashed'
            final_mult = state['crash_point']
            crash_save(state)
            socketio.emit('crash_phase', {
                'phase': 'crashed',
                'mult':  final_mult
            })

            # Pay out anyone still active (they lose — already deducted at bet time)
            _crash_resolve_round(state)

            # Reset for next round
            state = crash_default_state()
            state['round_id'] = state.get('round_id', 0) + 1
            crash_save(state)

    socketio.start_background_task(loop)


def _crash_resolve_round(state: dict):
    """Players who cashed out already got paid. Log losers."""
    conn = get_db_connection()
    cur  = conn.cursor()
    for uid_str, p in state['players'].items():
        if p['cashed_out']:
            continue  # already paid
        # loss already deducted at bet time, just log it
        user_id = int(uid_str)
        cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        bal = float(row[0]) if row else 0.0
        cur.execute(
            """INSERT INTO casino_transactions
               (user_id, game, bet, result, winnings, balance_after)
               VALUES (%s, 'crash', %s, %s, %s, %s)""",
            (user_id, p['bet'], f"Crash @ {state['crash_point']}x", -p['bet'], bal)
        )
    conn.commit()
    cur.close()
    conn.close()


@socketio.on('crash_bet')
def crash_bet(data):
    from redis_games import crash_load, crash_save
    if 'user_id' not in session:
        return
    user_id = session['user_id']
    bet     = float(data.get('bet', 10))
    state   = crash_load()

    if not state or state['phase'] != 'waiting':
        emit('crash_error', {'msg': 'Likmes pieņem tikai starp raundiem!'})
        return

    conn    = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    if bet <= 0 or bet > balance:
        conn.close()
        emit('crash_error', {'msg': 'Nepareizs likmjums'})
        return

    balance -= bet
    cur = conn.cursor()
    cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s",
                (round(balance, 2), user_id))
    cur.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
           VALUES (%s, %s, 'casino:crash_bet', %s)""",
        (user_id, -bet, round(balance, 2))
    )
    conn.commit()
    cur.close()
    conn.close()

    state['players'][str(user_id)] = {
        'bet': bet, 'cashed_out': False, 'cashout_mult': None,
        'name': session.get('user_name', '?')
    }
    crash_save(state)
    emit('crash_bet_ok', {'balance': round(balance, 2), 'bet': bet})
    socketio.emit('crash_players', {'players': state['players']})


@socketio.on('crash_cashout')
def crash_cashout(data):
    from redis_games import crash_load, crash_save, crash_multiplier_now
    if 'user_id' not in session:
        return
    user_id  = session['user_id']
    uid_str  = str(user_id)
    state    = crash_load()

    if not state or state['phase'] != 'running':
        emit('crash_error', {'msg': 'Spēle nav aktīva'})
        return
    if uid_str not in state['players']:
        emit('crash_error', {'msg': 'Tu neesi likuši likmi'})
        return
    if state['players'][uid_str]['cashed_out']:
        emit('crash_error', {'msg': 'Jau izmaksāts'})
        return

    mult     = crash_multiplier_now(state['start_time'])
    p        = state['players'][uid_str]
    winnings = round(p['bet'] * mult, 2)
    net      = round(winnings - p['bet'], 2)

    p['cashed_out']   = True
    p['cashout_mult'] = mult
    crash_save(state)

    conn    = get_db_connection()
    balance = get_or_create_balance(conn, user_id) + winnings
    cur     = conn.cursor()
    cur.execute("UPDATE wallets SET balance = %s WHERE user_id = %s",
                (round(balance, 2), user_id))
    cur.execute(
        """INSERT INTO casino_transactions
           (user_id, game, bet, result, winnings, balance_after)
           VALUES (%s, 'crash', %s, %s, %s, %s)""",
        (user_id, p['bet'], f"Cashout @ {mult}x", net, round(balance, 2))
    )
    cur.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, balance_after)
           VALUES (%s, %s, 'casino:crash_win', %s)""",
        (user_id, net, round(balance, 2))
    )
    conn.commit()
    if net >= _FEED_MIN_WIN:
        _post_feed(user_id, 'crash_cashout', 'crash', net,
               multiplier=mult,
               message=f"Izmaksāja @ {mult:.2f}x")
    cur.close()
    conn.close()

    emit('crash_cashout_ok', {
        'mult':    mult,
        'winnings': winnings,
        'balance': round(balance, 2)
    })
    socketio.emit('crash_players', {'players': state['players']})

@app.route('/casino/crash')
@login_required
def casino_crash():
    user_id = session['user_id']
    conn    = get_db_connection()
    balance = get_or_create_balance(conn, user_id)
    conn.close()
    return render_template('casino_crash.html', balance=balance)


_FEED_MIN_WIN = 500  # only post if net win >= this

def _post_feed(user_id: int, event_type: str, game: str,
               amount: float, multiplier: float = None, message: str = None):
    """Insert a social feed event. Call after any notable win."""
    if amount < _FEED_MIN_WIN:
        return
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO social_feed
               (user_id, event_type, game, amount, multiplier, message)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, event_type, game,
             round(amount, 2),
             round(multiplier, 2) if multiplier else None,
             message)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        app.logger.error(f'_post_feed error: {e}')


@app.route('/api/feed')
@login_required
def api_social_feed():
    user_id = session['user_id']
    conn    = get_db_connection()
    cur     = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get clubs this user belongs to
    cur.execute(
        "SELECT club_id FROM club_members WHERE user_id = %s", (user_id,)
    )
    club_ids = [r['club_id'] for r in cur.fetchall()]

    if not club_ids:
        cur.close()
        conn.close()
        return jsonify([])

    # Get feed from clubmates (last 30 events)
    cur.execute("""
        SELECT sf.event_type, sf.game, sf.amount, sf.multiplier,
               sf.message, sf.created_at, u.username
        FROM social_feed sf
        JOIN users u ON u.id = sf.user_id
        WHERE sf.user_id IN (
            SELECT user_id FROM club_members WHERE club_id = ANY(%s)
        )
        ORDER BY sf.created_at DESC
        LIMIT 30
    """, (club_ids,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = []
    icons = {
        'jackpot':       '🎰',
        'big_win':       '🏆',
        'bingo':         '🅱️',
        'crash_cashout': '🚀',
    }
    for r in rows:
        icon = icons.get(r['event_type'], '💰')
        text = r['message'] or f"{r['game']} +{r['amount']:.0f} coins"
        if r['multiplier']:
            text += f" @ {r['multiplier']:.2f}x"
        result.append({
            'icon':     icon,
            'username': r['username'],
            'text':     text,
            'amount':   float(r['amount']),
            'time':     r['created_at'].strftime('%H:%M'),
        })
    return jsonify(result)

if __name__ == '__main__':
    init_db()
    init_redis()
    _start_crash_loop()
    _start_tournament_scheduler()          # ← add this
    _HOLDEM_TABLES.update(_init_holdem_tables())
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)