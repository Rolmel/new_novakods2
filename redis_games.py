import os
import json
import random
import redis

from flask import jsonify

# ── typing aliases so the signatures are clear ─────────────────────────────
from typing import Any


# =============================================================
#  REDIS CONNECTION
# =============================================================

_redis: redis.Redis | None = None

# How long (seconds) an abandoned game session lives before Redis evicts it.
# Blackjack / poker hands are short; tower can take longer.
_TTL = {
    'bj':    60 * 30,   # 30 minutes
    'poker': 60 * 30,
    'tower': 60 * 60,   # 1 hour
}


def init_redis() -> None:
    """Call once at application startup."""
    global _redis
    url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    _redis = redis.from_url(url, decode_responses=True)
    _redis.ping()          # fail fast if Redis is unreachable
    print(f'[redis_games] Connected to Redis at {url}')


def _r() -> redis.Redis:
    if _redis is None:
        raise RuntimeError('Redis not initialised — call init_redis() at startup.')
    return _redis


# =============================================================
#  KEY HELPERS
# =============================================================

def _key(prefix: str, user_id: int) -> str:
    return f'game:{prefix}:{user_id}'


def _save(prefix: str, user_id: int, state: dict) -> None:
    """Serialise state to Redis with TTL."""
    _r().setex(_key(prefix, user_id), _TTL[prefix], json.dumps(state))


def _load(prefix: str, user_id: int) -> dict | None:
    """Return deserialized state or None if key has expired / never existed."""
    raw = _r().get(_key(prefix, user_id))
    return json.loads(raw) if raw else None


def _delete(prefix: str, user_id: int) -> None:
    _r().delete(_key(prefix, user_id))


# =============================================================
#  SHARED CARD HELPERS  (duplicated here so this file is self-contained;
#  you can remove the copies in admin.py once you import from here)
# =============================================================

def _make_deck() -> list[dict]:
    suits = ['♠', '♥', '♦', '♣']
    ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    deck = [{'rank': r, 'suit': s} for s in suits for r in ranks]
    random.shuffle(deck)
    return deck


def _card_value(card: dict) -> int:
    r = card['rank']
    if r in ('J', 'Q', 'K'):
        return 10
    if r == 'A':
        return 11
    return int(r)


def _hand_total(hand: list[dict]) -> int:
    total = sum(_card_value(c) for c in hand)
    aces  = sum(1 for c in hand if c['rank'] == 'A')
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


# =============================================================
#  POKER RANK  (self-contained copy)
# =============================================================

def _poker_rank(hand: list[dict]) -> tuple[int, str, list]:
    from collections import Counter
    ranks_order = '23456789TJQKA'
    vals  = sorted(
        [ranks_order.index(c['rank'].replace('10', 'T')) for c in hand],
        reverse=True,
    )
    suits  = [c['suit'] for c in hand]
    flush  = len(set(suits)) == 1
    straight = (max(vals) - min(vals) == 4 and len(set(vals)) == 5)
    if set(vals) == {0, 1, 2, 3, 12}:
        straight = True
        vals = [3, 2, 1, 0, -1]
    cnt    = Counter(vals)
    freq   = sorted(cnt.values(), reverse=True)
    groups = sorted(cnt.keys(), key=lambda x: (cnt[x], x), reverse=True)

    if straight and flush:  return (8, 'Straight Flush',   groups)
    if freq[0] == 4:        return (7, 'Four of a Kind',   groups)
    if freq[:2] == [3, 2]:  return (6, 'Full House',       groups)
    if flush:               return (5, 'Flush',             groups)
    if straight:            return (4, 'Straight',          groups)
    if freq[0] == 3:        return (3, 'Three of a Kind',  groups)
    if freq[:2] == [2, 2]:  return (2, 'Two Pair',         groups)
    if freq[0] == 2:        return (1, 'Pair',             groups)
    return (0, 'High Card', groups)


_POKER_PAYOUTS = {8: 50, 7: 25, 6: 9, 5: 6, 4: 4, 3: 3, 2: 2, 1: 1, 0: 0}


# =============================================================
#  DB HELPERS  (thin wrappers so this file doesn't import get_db_connection
#  directly — it receives the Flask `app` and uses its db function)
# =============================================================

def _get_balance(app, user_id):
    import psycopg2.extras
    from admin import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur2 = conn.cursor()
        cur2.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (%s, 1000)",
            (user_id,)
        )
        conn.commit()
        cur2.close()
        cur.close()
        conn.close()
        return 1000.0
    bal = float(row['balance'])
    cur.close()
    conn.close()
    return bal


def _set_balance(app, user_id, balance):
    from admin import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE wallets SET balance = %s WHERE user_id = %s",
        (round(balance, 2), user_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def _record_tx(app, user_id, game, bet, result, net, balance_after):
    from admin import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO casino_transactions
               (user_id, game, bet, result, winnings, balance_after)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user_id, game, bet, result, net, round(balance_after, 2))
    )
    cur.execute(
        """INSERT INTO wallet_log (user_id, delta, reason, ref_id, balance_after)
           VALUES (%s, %s, %s, %s, %s)""",
        (user_id, round(net, 2), f'casino:{game}', result[:100], round(balance_after, 2))
    )
    conn.commit()
    cur.close()
    conn.close()

# =============================================================
#  BLACKJACK
# =============================================================
#
#  Redis key: game:bj:<user_id>
#  State shape:
#  {
#    "deck":   [...],
#    "player": [...],
#    "dealer": [...],
#    "bet":    float,
#    "balance_after_deal": float   # balance already deducted
#  }
# =============================================================

def bj_deal(app, session, request) -> Any:
    user_id = session['user_id']
    bet     = float((request.json or {}).get('bet', 10))

    balance = _get_balance(app, user_id)
    if bet <= 0 or bet > balance:
        return jsonify({'error': 'Nepareizs likmjums'}), 400

    deck   = _make_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]

    balance -= bet
    _set_balance(app, user_id, balance)

    # ── store in Redis, NOT Flask session ───────────────────
    _save('bj', user_id, {
        'deck':   deck,
        'player': player,
        'dealer': dealer,
        'bet':    bet,
    })

    player_total    = _hand_total(player)
    blackjack_check = player_total == 21

    return jsonify({
        'player':         player,
        'dealer_visible': [dealer[0]],
        'player_total':   player_total,
        'blackjack':      blackjack_check,
        'balance':        round(balance, 2),
    })


def bj_hit(app, session, request) -> Any:
    user_id = session['user_id']
    state   = _load('bj', user_id)

    if not state:
        return jsonify({'error': 'Nav aktīvas spēles'}), 400

    deck   = state['deck']
    player = state['player']

    player.append(deck.pop())
    state['deck']   = deck
    state['player'] = player
    _save('bj', user_id, state)

    total = _hand_total(player)
    bust  = total > 21

    if bust:
        # Game over — record the loss and clean up
        bet     = state['bet']
        balance = _get_balance(app, user_id)
        _record_tx(app, user_id, 'blackjack', bet, 'Bust', -bet, balance)
        _delete('bj', user_id)

    return jsonify({'player': player, 'player_total': total, 'bust': bust})


def bj_stand(app, session, request) -> Any:
    user_id = session['user_id']
    state   = _load('bj', user_id)

    if not state:
        return jsonify({'error': 'Nav aktīvas spēles'}), 400

    deck   = state['deck']
    player = state['player']
    dealer = state['dealer']
    bet    = float(state['bet'])

    # Dealer draws to 17
    while _hand_total(dealer) < 17:
        dealer.append(deck.pop())

    player_total = _hand_total(player)
    dealer_total = _hand_total(dealer)

    if dealer_total > 21 or player_total > dealer_total:
        winnings = bet * 2
        result   = 'Uzvara!'
    elif player_total == dealer_total:
        winnings = bet
        result   = 'Neizšķirts'
    else:
        winnings = 0
        result   = 'Dīleris uzvar'

    net     = winnings - bet
    balance = _get_balance(app, user_id) + winnings
    _set_balance(app, user_id, balance)
    _record_tx(app, user_id, 'blackjack', bet, result, net, balance)
    _delete('bj', user_id)

    return jsonify({
        'player':       player,
        'dealer':       dealer,
        'player_total': player_total,
        'dealer_total': dealer_total,
        'result':       result,
        'winnings':     winnings,
        'net':          net,
        'balance':      round(balance, 2),
    })


# =============================================================
#  VIDEO POKER  (5-Card Draw)
# =============================================================
#
#  Redis key: game:poker:<user_id>
#  State shape:
#  { "deck": [...], "hand": [...], "bet": float }
# =============================================================

def poker_deal(app, session, request) -> Any:
    user_id = session['user_id']
    bet     = float((request.json or {}).get('bet', 10))

    balance = _get_balance(app, user_id)
    if bet <= 0 or bet > balance:
        return jsonify({'error': 'Nepareizs likmjums'}), 400

    deck = _make_deck()
    hand = [deck.pop() for _ in range(5)]

    balance -= bet
    _set_balance(app, user_id, balance)

    _save('poker', user_id, {'deck': deck, 'hand': hand, 'bet': bet})

    return jsonify({'hand': hand, 'balance': round(balance, 2)})


def poker_draw(app, session, request) -> Any:
    user_id = session['user_id']
    state   = _load('poker', user_id)

    if not state:
        return jsonify({'error': 'Nav aktīvas spēles'}), 400

    discard: list[int] = (request.json or {}).get('discard', [])
    hand = state['hand']
    deck = state['deck']
    bet  = float(state['bet'])

    for i in discard:
        if 0 <= i < 5:
            hand[i] = deck.pop()

    rank, desc, _ = _poker_rank(hand)
    multiplier    = _POKER_PAYOUTS[rank]
    winnings      = bet * multiplier if multiplier > 0 else 0
    net           = winnings - bet

    balance = _get_balance(app, user_id) + winnings
    if balance < 0:
        balance = 0.0
    _set_balance(app, user_id, balance)
    _record_tx(app, user_id, 'poker', bet, desc, net, balance)
    _delete('poker', user_id)

    return jsonify({
        'hand':       hand,
        'rank':       desc,
        'multiplier': multiplier,
        'winnings':   winnings,
        'net':        net,
        'balance':    round(balance, 2),
    })


# =============================================================
#  TOWER
# =============================================================
#
#  Redis key: game:tower:<user_id>
#  State shape:
#  {
#    "tower_data":  [[safe_col_indices], ...],  # 10 floors, pre-generated
#    "level":       int,                         # 0 = not yet climbed
#    "bet":         float,
#    "difficulty":  str,
#    "alive":       bool
#  }
# =============================================================

_TOWER_SAFE_COUNTS = {'easy': 3, 'medium': 2, 'hard': 1}
_TOWER_MULT_BASE   = {'easy': 1.3, 'medium': 1.6, 'hard': 2.5}
_TOWER_COLS        = 4


def _tower_multipliers(difficulty: str) -> list[float]:
    base = _TOWER_MULT_BASE.get(difficulty, 1.6)
    return [round(base ** (i + 1), 2) for i in range(10)]


def tower_start(app, session, request) -> Any:
    user_id    = session['user_id']
    data       = request.json or {}
    bet        = float(data.get('bet', 10))
    difficulty = data.get('difficulty', 'medium')

    balance = _get_balance(app, user_id)
    if bet <= 0 or bet > balance:
        return jsonify({'error': 'Nepareizs likmjums'}), 400

    safe = _TOWER_SAFE_COUNTS.get(difficulty, 2)
    tower_data = [
        random.sample(range(_TOWER_COLS), safe)
        for _ in range(10)
    ]

    balance -= bet
    _set_balance(app, user_id, balance)

    _save('tower', user_id, {
        'tower_data': tower_data,
        'level':      0,
        'bet':        bet,
        'difficulty': difficulty,
        'alive':      True,
    })

    return jsonify({
        'level':       0,
        'cols':        _TOWER_COLS,
        'multipliers': _tower_multipliers(difficulty),
        'balance':     round(balance, 2),
    })


def tower_step(app, session, request) -> Any:
    user_id = session['user_id']
    col     = int((request.json or {}).get('col', 0))
    state   = _load('tower', user_id)

    if not state or not state['alive']:
        return jsonify({'error': 'Nav aktīvas spēles'}), 400

    level      = state['level']
    bet        = float(state['bet'])
    difficulty = state['difficulty']
    tower_data = state['tower_data']

    if level >= 10:
        return jsonify({'error': 'Esi sasniedzis virsotni!'}), 400

    safe_positions: list[int] = tower_data[level]
    hit_bomb = col not in safe_positions

    if hit_bomb:
        state['alive'] = False
        _save('tower', user_id, state)   # keep so front-end can reveal bombs
        _delete('tower', user_id)

        balance = _get_balance(app, user_id)
        _record_tx(app, user_id, 'tower', bet, f'Bumba! Stāvs {level + 1}', -bet, balance)

        return jsonify({
            'hit_bomb':       True,
            'safe_positions': safe_positions,
            'balance':        round(balance, 2),
        })

    # Safe — advance
    level += 1
    state['level'] = level
    _save('tower', user_id, state)

    base         = _TOWER_MULT_BASE.get(difficulty, 1.6)
    current_mult = round(base ** level, 2)

    if level >= 10:
        # Auto-cashout at the top
        winnings = round(bet * current_mult, 2)
        net      = winnings - bet
        balance  = _get_balance(app, user_id) + winnings
        _set_balance(app, user_id, balance)
        _record_tx(app, user_id, 'tower', bet, f'Virsotne! x{current_mult}', net, balance)
        _delete('tower', user_id)

        return jsonify({
            'hit_bomb':    False,
            'level':       level,
            'multiplier':  current_mult,
            'topped_out':  True,
            'winnings':    winnings,
            'net':         net,
            'balance':     round(balance, 2),
        })

    return jsonify({
        'hit_bomb':   False,
        'level':      level,
        'multiplier': current_mult,
        'topped_out': False,
    })


def tower_cashout(app, session, request) -> Any:
    user_id = session['user_id']
    state   = _load('tower', user_id)

    if not state or not state['alive'] or state['level'] == 0:
        return jsonify({'error': 'Nav ko izmaksāt'}), 400

    level      = state['level']
    bet        = float(state['bet'])
    difficulty = state['difficulty']

    base         = _TOWER_MULT_BASE.get(difficulty, 1.6)
    current_mult = round(base ** level, 2)
    winnings     = round(bet * current_mult, 2)
    net          = winnings - bet

    balance = _get_balance(app, user_id) + winnings
    _set_balance(app, user_id, balance)
    _record_tx(app, user_id, 'tower', bet, f'Cashout stāvs {level}, x{current_mult}', net, balance)
    _delete('tower', user_id)

    return jsonify({
        'winnings':   winnings,
        'multiplier': current_mult,
        'net':        net,
        'balance':    round(balance, 2),
    })


# =============================================================
#  DIAGNOSTIC ENDPOINT  (wire up in admin.py for dev only)
# =============================================================
#
#  @app.route('/dev/redis/state/<game>/<int:uid>')
#  def dev_redis_state(game, uid):
#      if not app.debug: return '', 404
#      return jsonify(_load(game, uid) or {})

def dev_state(game: str, user_id: int) -> dict:
    """Return raw Redis state for debugging."""
    return _load(game, user_id) or {}