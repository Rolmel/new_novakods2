"""
Achievement checker. Import and call check_achievements() after
any significant user action. Keeps admin.py clean.
"""

import psycopg2.extras


# Map slug -> check function signature:
# fn(conn, user_id, context) -> bool
# context is a dict with whatever the caller passes in
# Returns True if newly unlocked (for notification)


def _already_has(conn, user_id: int, slug: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM user_achievements WHERE user_id=%s AND slug=%s",
        (user_id, slug)
    )
    result = cur.fetchone() is not None
    cur.close()
    return result


def _award(conn, user_id: int, slug: str) -> bool:
    """Insert achievement. Returns True if newly awarded."""
    if _already_has(conn, user_id, slug):
        return False
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user_achievements (user_id, slug) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, slug)
    )
    # Also store a notification
    cur.execute(
        """
        INSERT INTO notifications (user_id, message)
        SELECT %s, '🏅 Jauns sasniegums: ' || name
        FROM achievements WHERE slug = %s
        """,
        (user_id, slug)
    )
    conn.commit()
    cur.close()
    return True


def _casino_stats(conn, user_id: int) -> dict:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT COUNT(*) AS total FROM casino_transactions WHERE user_id=%s",
        (user_id,)
    )
    total = cur.fetchone()['total']
    cur.close()
    return {'total_games': total}


def _wallet_balance(conn, user_id: int) -> float:
    cur = conn.cursor()
    cur.execute("SELECT balance FROM wallets WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row else 0.0


def _pred_stats(conn, user_id: int) -> dict:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM prediction_stats WHERE user_id=%s", (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else {}


def _friend_count(conn, user_id: int) -> int:
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) FROM friendships
           WHERE (sender_id=%s OR receiver_id=%s) AND status='accepted'""",
        (user_id, user_id)
    )
    n = cur.fetchone()[0]
    cur.close()
    return n


def _daily_streak(conn, user_id: int) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT streak FROM daily_claims WHERE user_id=%s", (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else 0


def _club_member_count(conn, user_id: int) -> int:
    """Max members in any club this user owns."""
    cur = conn.cursor()
    cur.execute(
        """SELECT MAX(cnt) FROM (
               SELECT COUNT(*) AS cnt
               FROM club_members cm
               JOIN clubs c ON c.id = cm.club_id
               WHERE c.owner_id = %s
               GROUP BY cm.club_id
           ) sub""",
        (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row and row[0] else 0


def check_achievements(conn, user_id: int, context: dict) -> list[str]:
    """
    Main entry point. Call after any significant action.

    context keys (all optional, only pass what's relevant):
        game        str   — 'slots'|'blackjack'|'highlow'|'tower'|'crash'|'poker'|'bingo'
        net         float — net coins won this action (positive = win)
        result      str   — game result string
        multiplier  float — crash/tower multiplier
        hl_streak   int   — current highlow streak
        pred_event  bool  — True if prediction-related action
        daily       bool  — True if daily bonus claimed
        club_action str   — 'created'|'member_joined'
        friend_added bool — True if friend request accepted

    Returns list of newly unlocked slugs (for front-end toast).
    """
    newly_unlocked = []

    def award(slug):
        if _award(conn, user_id, slug):
            newly_unlocked.append(slug)

    game   = context.get('game', '')
    net    = float(context.get('net', 0))
    result = context.get('result', '')

    # ── Casino ──────────────────────────────────────────
    if game in ('slots', 'blackjack', 'highlow', 'keno', 'bingo',
                'poker', 'tower', 'crash', 'holdem'):

        stats = _casino_stats(conn, user_id)

        if stats['total_games'] >= 1:
            award('first_spin')

        if game == 'slots' and stats['total_games'] >= 100:
            award('slots_100')

        if game == 'slots' and 'JACKPOT' in result:
            award('jackpot')

        if game == 'blackjack' and 'BLACKJACK' in result.upper():
            award('blackjack_nat')

        if net >= 500:
            award('big_win_500')

        if net >= 5000:
            award('big_win_5000')

        if game == 'tower' and context.get('topped_out'):
            award('tower_top')

        if game == 'crash':
            mult = float(context.get('multiplier', 0))
            if mult >= 10.0:
                award('crash_10x')

        if game == 'poker' and 'Straight Flush' in result:
            award('poker_sf')

        if game == 'highlow':
            streak = int(context.get('hl_streak', 0))
            if streak >= 10:
                award('hl_streak_10')

        if stats['total_games'] >= 500:
            award('total_bets_500')

    # ── Wallet milestones ────────────────────────────────
    if net > 0 or context.get('daily'):
        bal = _wallet_balance(conn, user_id)
        if bal >= 10_000:
            award('wallet_10k')
        if bal >= 100_000:
            award('wallet_100k')

    # ── Predictions ──────────────────────────────────────
    if context.get('pred_event'):
        ps = _pred_stats(conn, user_id)
        if ps:
            award('pred_first')
            wins = ps.get('total_wins', 0)
            if wins >= 5:
                award('pred_win_5')
            if wins >= 25:
                award('pred_win_25')
            if wins >= 100:
                award('pred_win_100')
            if ps.get('current_streak', 0) >= 5:
                award('pred_streak_5')

            staked   = ps.get('total_staked', 1) or 1
            returned = ps.get('total_returned', 0)
            roi      = (returned - staked) / staked * 100
            if roi >= 50 and ps.get('total_bets', 0) >= 20:
                award('pred_roi_50')

        # Check tier
        cur = conn.cursor()
        cur.execute(
            "SELECT tier FROM predictor_tiers WHERE user_id=%s", (user_id,)
        )
        row = cur.fetchone()
        cur.close()
        if row and row[0] == 'oracle':
            award('pred_oracle')

    # ── Daily streak ─────────────────────────────────────
    if context.get('daily'):
        streak = _daily_streak(conn, user_id)
        if streak >= 7:
            award('daily_7')
        if streak >= 30:
            award('daily_30')
        if streak >= 100:
            award('daily_100')

    # ── Social ───────────────────────────────────────────
    if context.get('club_action') == 'created':
        award('club_found')

    if context.get('club_action') == 'member_joined':
        if _club_member_count(conn, user_id) >= 10:
            award('club_10members')

    if context.get('friend_added'):
        if _friend_count(conn, user_id) >= 5:
            award('friends_5')

    return newly_unlocked