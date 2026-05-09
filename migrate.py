import sqlite3
import psycopg2

src = sqlite3.connect('/var/www/html/novakods/produkcija.db')
src.row_factory = sqlite3.Row
dst = psycopg2.connect('postgresql://rolmel:yourpassword@localhost/novakods')
cur = dst.cursor()

# Users
for r in src.execute("SELECT * FROM useri"):
    cur.execute("""
        INSERT INTO users (id, username, password_hash, is_admin)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (username) DO NOTHING
    """, (r['id'], r['username'], r['password_hash'], bool(r['is_admin'])))

# Wallets
for r in src.execute("SELECT * FROM casino_players"):
    cur.execute("""
        INSERT INTO wallets (user_id, balance)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET balance = EXCLUDED.balance
    """, (r['user_id'], r['balance']))

# Casino transactions
for r in src.execute("SELECT * FROM casino_transactions"):
    cur.execute("""
        INSERT INTO casino_transactions (user_id, game, bet, result, winnings, balance_after)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (r['user_id'], r['game'], r['bet'], r['result'], r['winnings'], r['balance_after']))

# Canvas
for r in src.execute("SELECT * FROM canvas"):
    cur.execute("""
        INSERT INTO canvas (x, y, color, user_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (x, y) DO UPDATE SET color = EXCLUDED.color
    """, (r['x'], r['y'], r['color'], r['user_id']))

# Canvas scores
for r in src.execute("SELECT * FROM scores"):
    cur.execute("""
        INSERT INTO canvas_scores (user_id, count)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET count = EXCLUDED.count
    """, (r['user_id'], r['count']))

# Chat groups
for r in src.execute("SELECT * FROM chat_groups"):
    cur.execute("""
        INSERT INTO chat_groups (id, name, created_by)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (r['id'], r['name'], r['created_by']))

# Chat members
for r in src.execute("SELECT * FROM chat_members"):
    cur.execute("""
        INSERT INTO chat_members (group_id, user_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
    """, (r['group_id'], r['user_id']))

# Chat messages
for r in src.execute("SELECT * FROM chat_messages"):
    cur.execute("""
        INSERT INTO chat_messages (group_id, user_id, message)
        VALUES (%s, %s, %s)
    """, (r['group_id'], r['user_id'], r['message']))

# User files
for r in src.execute("SELECT * FROM user_files"):
    cur.execute("""
        INSERT INTO user_files (user_id, filename)
        VALUES (%s, %s)
    """, (r['user_id'], r['filename']))

# Fix sequences
cur.execute("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))")
cur.execute("SELECT setval('chat_groups_id_seq', (SELECT MAX(id) FROM chat_groups))")

dst.commit()
src.close()
dst.close()
print("Done! All data migrated.")
