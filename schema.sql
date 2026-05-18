-- ============================================================
--  NovaKods — Full PostgreSQL Schema
--  Run once:  psql -U rolmel -d novakods -f /var/www/html/novakods/schema.sql
-- ============================================================
-- UPDATE users SET referral_code = encode(gen_random_bytes(4), 'hex') WHERE referral_code IS NULL;

CREATE TABLE IF NOT EXISTS club_war_weeks (
    id            SERIAL PRIMARY KEY,
    week_start    DATE NOT NULL,
    club1_id      INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
    club2_id      INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
    club1_correct INTEGER NOT NULL DEFAULT 0,
    club2_correct INTEGER NOT NULL DEFAULT 0,
    winner_id     INTEGER REFERENCES clubs(id),
    resolved      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (week_start, club1_id, club2_id)
);


CREATE TABLE IF NOT EXISTS battle_queue (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stake       INTEGER NOT NULL CHECK (stake > 0),
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id)
);

CREATE TABLE IF NOT EXISTS battles (
    id              SERIAL PRIMARY KEY,
    player1_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    player2_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id        INTEGER NOT NULL REFERENCES prediction_events(id) ON DELETE CASCADE,
    p1_option_id    INTEGER REFERENCES prediction_options(id),
    p2_option_id    INTEGER REFERENCES prediction_options(id),
    stake           INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'picking',
    -- picking | active | finished
    winner_id       INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS battles_player ON battles(player1_id, player2_id);
CREATE INDEX IF NOT EXISTS battles_event  ON battles(event_id, status);

CREATE TABLE IF NOT EXISTS highroller_sessions (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    unlocked   BOOLEAN NOT NULL DEFAULT FALSE,
    unlocked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS mission_definitions (
    id          SERIAL PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    goal        INTEGER NOT NULL DEFAULT 1,
    reward      INTEGER NOT NULL DEFAULT 50,
    category    TEXT NOT NULL DEFAULT 'casino'
);

CREATE TABLE IF NOT EXISTS user_missions (
    id          BIGSERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mission_id  INTEGER NOT NULL REFERENCES mission_definitions(id),
    progress    INTEGER NOT NULL DEFAULT 0,
    completed   BOOLEAN NOT NULL DEFAULT FALSE,
    day         DATE    NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE (user_id, mission_id, day)
);

CREATE INDEX IF NOT EXISTS user_missions_day ON user_missions(user_id, day);

INSERT INTO mission_definitions (slug, title, description, goal, reward, category) VALUES
('spin_3',          '3 Griezieni',          'Nospēlē slots 3 reizes',              3,   75,  'casino'),
('win_slots',       'Slots Uzvarētājs',      'Uzvar slots spēlē',                   1,   100, 'casino'),
('place_prediction','Prognozētājs',          'Liec prognozi',                        1,   100, 'predictions'),
('play_blackjack',  'Blackjack Spēlētājs',   'Nospēlē blackjack rokas',             2,   100, 'casino'),
('win_highlow',     'Augsts vai Zems',       'Uzvar High/Low spēlē',                3,   150, 'casino'),
('chat_message',    'Sabiedrisks',           'Nosūti ziņu Lobby čatā',              1,   50,  'social'),
('casino_any',      'Kazino Apmeklētājs',    'Spēlē jebkuru kazino spēli',          5,   200, 'casino'),
('prediction_win',  'Pareizs Prognozētājs',  'Uzvar prognozi (atrisināšanas brīdī)',1,   300, 'predictions')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE IF NOT EXISTS referrals (
    id            SERIAL PRIMARY KEY,
    referrer_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referred_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bonus_paid    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (referred_id)
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT UNIQUE;

CREATE TABLE IF NOT EXISTS season_rankings (
    id           SERIAL PRIMARY KEY,
    season       TEXT NOT NULL,        -- e.g. '2025-Q2'
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username     TEXT NOT NULL,
    category     TEXT NOT NULL,        -- 'predictions' | 'casino' | 'overall'
    score        NUMERIC(12,2) NOT NULL DEFAULT 0,
    rank_place   INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (season, user_id, category)
);

CREATE TABLE IF NOT EXISTS club_challenges (
    id              SERIAL PRIMARY KEY,
    challenger_id   INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
    defender_id     INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
    event_id        INTEGER NOT NULL REFERENCES prediction_events(id) ON DELETE CASCADE,
    challenger_option_id INTEGER NOT NULL REFERENCES prediction_options(id),
    defender_option_id   INTEGER,
    pot_per_club    INTEGER NOT NULL DEFAULT 100,
    status          TEXT NOT NULL DEFAULT 'pending',
    winner_club_id  INTEGER REFERENCES clubs(id),
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS progressive_jackpot (
    id         SERIAL PRIMARY KEY,
    game       TEXT    NOT NULL DEFAULT 'slots',
    amount     NUMERIC(12,2) NOT NULL DEFAULT 500.00,
    seed       NUMERIC(12,2) NOT NULL DEFAULT 500.00,
    last_win_at TIMESTAMPTZ,
    last_winner TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO progressive_jackpot (game, amount, seed)
VALUES ('slots', 500, 500) ON CONFLICT DO NOTHING;

ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS min_tier TEXT NOT NULL DEFAULT 'unranked';

CREATE TABLE IF NOT EXISTS predictor_follows (
    follower_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    following_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (follower_id, following_id)
);

CREATE TABLE IF NOT EXISTS prediction_comments (
    id          BIGSERIAL PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES prediction_events(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body        VARCHAR(500) NOT NULL,
    likes       INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prediction_comment_likes (
    comment_id  BIGINT  NOT NULL REFERENCES prediction_comments(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (comment_id, user_id)
);

CREATE INDEX IF NOT EXISTS pred_comments_event ON prediction_comments(event_id, created_at DESC);


ALTER TABLE prediction_events
    ADD COLUMN IF NOT EXISTS min_tier TEXT NOT NULL DEFAULT 'unranked',
    ADD COLUMN IF NOT EXISTS creator_tier TEXT;
-- creator_tier stores what tier the user was when they created it (display only)

CREATE TABLE IF NOT EXISTS prediction_duels (
    id            SERIAL PRIMARY KEY,
    challenger_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opponent_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id      INTEGER NOT NULL REFERENCES prediction_events(id) ON DELETE CASCADE,
    challenger_option_id INTEGER NOT NULL REFERENCES prediction_options(id),
    opponent_option_id   INTEGER,  -- NULL until opponent accepts
    stake         INTEGER NOT NULL CHECK (stake > 0),
    status        TEXT NOT NULL DEFAULT 'pending',
    -- pending | accepted | declined | resolved | expired
    winner_id     INTEGER REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
    resolved_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS duel_challenger ON prediction_duels(challenger_id);
CREATE INDEX IF NOT EXISTS duel_opponent   ON prediction_duels(opponent_id);
CREATE INDEX IF NOT EXISTS duel_event      ON prediction_duels(event_id, status);


CREATE TABLE IF NOT EXISTS achievements (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    icon        TEXT NOT NULL DEFAULT '🏅',
    category    TEXT NOT NULL DEFAULT 'casino',  -- casino|predictions|social|grind
    rarity      TEXT NOT NULL DEFAULT 'common'   -- common|rare|epic|legendary
);

CREATE TABLE IF NOT EXISTS user_achievements (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slug         TEXT    NOT NULL REFERENCES achievements(slug),
    unlocked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, slug)
);

-- Seed all achievements
INSERT INTO achievements (slug, name, description, icon, category, rarity) VALUES
-- Casino
('first_spin',      'Pirmā Grieziens',     'Nospēlē slots pirmo reizi',          '🎰', 'casino', 'common'),
('slots_100',       'Slots Veterāns',      'Nospēlē slots 100 reizes',            '🎰', 'casino', 'rare'),
('jackpot',         'Džekpots!',           'Laimē slots džekpotu',                '💎', 'casino', 'epic'),
('blackjack_nat',   'Natural 21',          'Dabūji blackjack dabisko',            '🃏', 'casino', 'rare'),
('big_win_500',     'Lielais Laimests',    'Laimē 500+ monētas vienā spēlē',     '💰', 'casino', 'rare'),
('big_win_5000',    'Leģenda',             'Laimē 5000+ monētas vienā spēlē',    '👑', 'casino', 'legendary'),
('tower_top',       'Torņa Karalis',       'Sasniiedz Tower virsotni',            '🗼', 'casino', 'epic'),
('crash_10x',       'Nervu Sitiens',       'Izmaksā Crash pie 10x vai vairāk',   '🚀', 'casino', 'epic'),
('poker_sf',        'Straight Flush',      'Dabūji Straight Flush Video Poker',  '♠️', 'casino', 'legendary'),
('hl_streak_10',    'Lasītājs',            '10 pareizas atbildes pēc kārtas HL', '🔮', 'casino', 'epic'),
-- Predictions
('pred_first',      'Pirmā Likme',         'Liec pirmo prognozi',                '📈', 'predictions', 'common'),
('pred_win_5',      'Sācējs Analītiķis',  'Uzvar 5 prognozes',                  '📊', 'predictions', 'common'),
('pred_win_25',     'Pieredzējis',         'Uzvar 25 prognozes',                 '🧠', 'predictions', 'rare'),
('pred_win_100',    'Eksperts',            'Uzvar 100 prognozes',                '⭐', 'predictions', 'epic'),
('pred_streak_5',   'Karstā Sērija',       '5 uzvaras pēc kārtas prognozēs',    '🔥', 'predictions', 'rare'),
('pred_roi_50',     'Peļņu Prāts',         'Sasniedz 50% ROI prognozēs',        '💹', 'predictions', 'epic'),
('pred_oracle',     'Orākuls',             'Sasniedz Oracle līmeni',             '🔮', 'predictions', 'legendary'),
-- Social / Clubs
('club_found',      'Dibinātājs',          'Izveido klubu',                      '🏛️', 'social', 'common'),
('club_10members',  'Populārs Klubs',      'Tavam klubam ir 10+ dalībnieki',     '👥', 'social', 'rare'),
('friends_5',       'Sabiedrisks',         'Pievieno 5 draugus',                 '🤝', 'social', 'common'),
-- Grind
('daily_7',         'Nedēļas Ieradums',    '7 dienu ikdienas sērija',            '📅', 'grind', 'common'),
('daily_30',        'Mēneša Veterāns',     '30 dienu ikdienas sērija',           '🏆', 'grind', 'rare'),
('daily_100',       'Neatvairāms',         '100 dienu ikdienas sērija',          '💎', 'grind', 'legendary'),
('wallet_10k',      'Desmitnieks',         'Sasniedz 10,000 monētu bilanci',     '💵', 'grind', 'rare'),
('wallet_100k',     'Miljonārs',           'Sasniedz 100,000 monētu bilanci',   '🤑', 'grind', 'legendary'),
('total_bets_500',  'Nelabojams',          'Nospēlē 500 casino spēles',          '🎲', 'grind', 'epic')
ON CONFLICT (slug) DO NOTHING;

-- Tracks per-user prediction performance (updated on each resolve)
CREATE TABLE IF NOT EXISTS prediction_stats (
    user_id        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    total_bets     INTEGER NOT NULL DEFAULT 0,
    total_wins     INTEGER NOT NULL DEFAULT 0,
    total_staked   BIGINT  NOT NULL DEFAULT 0,
    total_returned BIGINT  NOT NULL DEFAULT 0,  -- coins returned (winnings)
    current_streak INTEGER NOT NULL DEFAULT 0,
    best_streak    INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tier system: unranked → bronze → silver → gold → expert → oracle
CREATE TABLE IF NOT EXISTS predictor_tiers (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    tier       TEXT    NOT NULL DEFAULT 'unranked',  
    tier_since TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Weekly snapshot for season leaderboards (run by a background job)
CREATE TABLE IF NOT EXISTS prediction_weekly_lb (
    week_start DATE    NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    wins       INTEGER NOT NULL DEFAULT 0,
    roi_pct    NUMERIC(8,2) NOT NULL DEFAULT 0,  -- (returned-staked)/staked * 100
    profit     BIGINT  NOT NULL DEFAULT 0,
    PRIMARY KEY (week_start, user_id)
);

CREATE TABLE IF NOT EXISTS friendships (
    id         SERIAL PRIMARY KEY,
    sender_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'pending', -- pending|accepted|rejected
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sender_id, receiver_id)
);

CREATE TABLE IF NOT EXISTS direct_messages (
    id          BIGSERIAL PRIMARY KEY,
    sender_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message     VARCHAR(2000) NOT NULL,
    is_read     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS dm_conversation ON direct_messages(
    LEAST(sender_id, receiver_id),
    GREATEST(sender_id, receiver_id),
    created_at ASC
);

CREATE TABLE IF NOT EXISTS tournaments (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    description   TEXT         NOT NULL DEFAULT '',
    game          TEXT         NOT NULL DEFAULT 'slots',  -- which game counts
    entry_fee     INTEGER      NOT NULL DEFAULT 0,        -- coins from real wallet
    start_coins   INTEGER      NOT NULL DEFAULT 1000,     -- isolated chips
    prize_pool    INTEGER      NOT NULL DEFAULT 0,        -- auto-calculated
    starts_at     TIMESTAMPTZ  NOT NULL,
    ends_at       TIMESTAMPTZ  NOT NULL,
    status        TEXT         NOT NULL DEFAULT 'upcoming', -- upcoming|active|finished
    max_players   INTEGER      NOT NULL DEFAULT 100,
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tournament_entries (
    id              SERIAL PRIMARY KEY,
    tournament_id   INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    chips           INTEGER NOT NULL,   -- current isolated chip balance
    games_played    INTEGER NOT NULL DEFAULT 0,
    best_win        INTEGER NOT NULL DEFAULT 0,
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tournament_id, user_id)
);

CREATE TABLE IF NOT EXISTS tournament_payouts (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    place         INTEGER NOT NULL,
    amount        INTEGER NOT NULL,
    paid_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tournament_id, user_id)
);

CREATE INDEX IF NOT EXISTS tourn_entries_score
    ON tournament_entries(tournament_id, chips DESC);

CREATE TABLE IF NOT EXISTS social_feed (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT    NOT NULL,  -- 'big_win' | 'bingo' | 'jackpot' | 'crash_cashout'
    game       TEXT,
    amount     NUMERIC(12,2),
    multiplier NUMERIC(8,2),
    message    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS social_feed_user ON social_feed(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS social_feed_recent ON social_feed(created_at DESC);

-- ── Extensions ───────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive text for usernames

-- ============================================================
--  USERS & AUTH
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      CITEXT UNIQUE NOT NULL,
    password_hash TEXT   NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
--  WALLETS  (single source of truth for credits)
-- ============================================================

CREATE TABLE IF NOT EXISTS wallets (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance    BIGINT  NOT NULL DEFAULT 1000  -- stored as integer coins, never float
                       CHECK (balance >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_log (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta      BIGINT  NOT NULL,               -- positive = credit, negative = debit
    reason     TEXT    NOT NULL,               -- 'casino_win:slots', 'daily', 'prediction_payout' …
    ref_id     TEXT,                           -- optional foreign key as text (game_id, event_id …)
    balance_after BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    message TEXT,
    is_read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS wallet_log_user ON wallet_log(user_id, created_at DESC);


-- ============================================================
--  COSMETICS SHOP
-- ============================================================

CREATE TABLE IF NOT EXISTS cosmetics (
    id          SERIAL PRIMARY KEY,
    slug        TEXT    UNIQUE NOT NULL,   -- 'card_gold', 'card_neon' …
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT 'card_skin',  -- 'card_skin'|'avatar_frame'|'badge'
    price       INTEGER NOT NULL CHECK (price >= 0),
    asset_path  TEXT    NOT NULL DEFAULT '',           -- static file path
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

-- ============================================================
--  PROFILES
-- ============================================================

CREATE TABLE IF NOT EXISTS profiles (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    display_name VARCHAR(40)  NOT NULL DEFAULT '',
    title        VARCHAR(40)  NOT NULL DEFAULT '',
    country      VARCHAR(60)  NOT NULL DEFAULT '',
    avatar_path  TEXT         NOT NULL DEFAULT '',
    bio          VARCHAR(200) NOT NULL DEFAULT '',
    equipped_skin INTEGER REFERENCES cosmetics(id) ON DELETE SET NULL,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);



CREATE TABLE IF NOT EXISTS user_cosmetics (
    user_id      INTEGER NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
    cosmetic_id  INTEGER NOT NULL REFERENCES cosmetics(id)  ON DELETE CASCADE,
    purchased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, cosmetic_id)
);

-- ============================================================
--  DAILY BONUS  (track last claim — Redis is primary, this is backup)
-- ============================================================

CREATE TABLE IF NOT EXISTS daily_claims (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_claim DATE    NOT NULL DEFAULT CURRENT_DATE,
    streak     INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
--  CASINO
-- ============================================================

CREATE TABLE IF NOT EXISTS casino_transactions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game          TEXT    NOT NULL,   -- 'slots'|'blackjack'|'highlow'|'keno'|'bingo'|'poker'|'tower'|'holdem'
    bet           BIGINT  NOT NULL DEFAULT 0,
    result        TEXT    NOT NULL DEFAULT '',
    winnings      BIGINT  NOT NULL DEFAULT 0,   -- net (negative = loss)
    balance_after BIGINT  NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS casino_tx_user ON casino_transactions(user_id, created_at DESC);

-- PixelWar canvas
CREATE TABLE IF NOT EXISTS canvas (
    x       INTEGER NOT NULL,
    y       INTEGER NOT NULL,
    color   CHAR(7) NOT NULL DEFAULT '#000000',
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    painted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (x, y)
);

CREATE TABLE IF NOT EXISTS canvas_scores (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    count   INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
--  CHAT
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_groups (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(50) NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_members (
    group_id INTEGER NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         BIGSERIAL PRIMARY KEY,
    group_id   INTEGER NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    message    VARCHAR(2000) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chat_msg_group ON chat_messages(group_id, created_at ASC);

-- ============================================================
--  FILE STORAGE (Bumbox)
-- ============================================================

CREATE TABLE IF NOT EXISTS user_files (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT    NOT NULL,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
--  CLUBS
-- ============================================================

CREATE TABLE IF NOT EXISTS clubs (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(50) UNIQUE NOT NULL,
    description VARCHAR(200) NOT NULL DEFAULT '',
    owner_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invite_code TEXT UNIQUE NOT NULL DEFAULT encode(gen_random_bytes(6), 'hex'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS club_members (
    club_id   INTEGER NOT NULL REFERENCES clubs(id)  ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    role      TEXT    NOT NULL DEFAULT 'member',  -- 'owner'|'admin'|'member'
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (club_id, user_id)
);

-- Materialised leaderboard updated by trigger / background job
CREATE TABLE IF NOT EXISTS club_leaderboard (
    club_id      INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    total_won    BIGINT  NOT NULL DEFAULT 0,
    total_games  INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (club_id, user_id)
);

-- ============================================================
--  PREDICTION MARKET
-- ============================================================

-- An "event" is a real-world question with a deadline
CREATE TABLE IF NOT EXISTS prediction_events (
    id            SERIAL PRIMARY KEY,
    title         VARCHAR(200) NOT NULL,
    description   TEXT         NOT NULL DEFAULT '',
    category      TEXT         NOT NULL DEFAULT 'general',  -- 'sports'|'crypto'|'general'…
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    closes_at     TIMESTAMPTZ  NOT NULL,     -- betting deadline
    resolves_at   TIMESTAMPTZ,               -- when admin resolves it
    outcome       TEXT,                      -- NULL until resolved: 'yes'|'no' or custom label
    status        TEXT NOT NULL DEFAULT 'open',  -- 'open'|'closed'|'resolved'|'cancelled'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS pred_event_status ON prediction_events(status, closes_at);

-- Each possible answer for an event (binary = 2 rows: YES / NO)
CREATE TABLE IF NOT EXISTS prediction_options (
    id       SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES prediction_events(id) ON DELETE CASCADE,
    label    TEXT    NOT NULL,               -- 'Yes'|'No' or 'Team A'|'Draw'|'Team B'
    -- Implied probability tracked live (0.0–1.0); updated by market engine
    price    NUMERIC(6,4) NOT NULL DEFAULT 0.5 CHECK (price > 0 AND price <= 1)
);

CREATE INDEX IF NOT EXISTS pred_option_event ON prediction_options(event_id);

-- A user's position on one option
CREATE TABLE IF NOT EXISTS predictions (
    id          BIGSERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id)              ON DELETE CASCADE,
    event_id    INTEGER NOT NULL REFERENCES prediction_events(id)  ON DELETE CASCADE,
    option_id   INTEGER NOT NULL REFERENCES prediction_options(id) ON DELETE CASCADE,
    stake       BIGINT  NOT NULL CHECK (stake > 0),   -- coins wagered
    price_at_entry NUMERIC(6,4) NOT NULL,             -- implied prob when bet was placed
    payout      BIGINT,                               -- NULL until resolved
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, event_id)                        -- one position per event per user
);

CREATE INDEX IF NOT EXISTS pred_user   ON predictions(user_id);
CREATE INDEX IF NOT EXISTS pred_event  ON predictions(event_id, option_id);

-- Aggregate volume per option (updated by trigger for fast SocketIO reads)
CREATE TABLE IF NOT EXISTS prediction_volume (
    option_id    INTEGER PRIMARY KEY REFERENCES prediction_options(id) ON DELETE CASCADE,
    total_stake  BIGINT NOT NULL DEFAULT 0,
    backer_count INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Trigger: keep prediction_volume in sync ──────────────────
CREATE OR REPLACE FUNCTION trg_update_volume() RETURNS trigger AS $$
BEGIN
    INSERT INTO prediction_volume (option_id, total_stake, backer_count)
    VALUES (NEW.option_id, NEW.stake, 1)
    ON CONFLICT (option_id) DO UPDATE
        SET total_stake  = prediction_volume.total_stake  + NEW.stake,
            backer_count = prediction_volume.backer_count + 1,
            updated_at   = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS after_prediction_insert ON predictions;
CREATE TRIGGER after_prediction_insert
    AFTER INSERT ON predictions
    FOR EACH ROW EXECUTE FUNCTION trg_update_volume();

-- ── Trigger: update wallet updated_at on balance change ──────
CREATE OR REPLACE FUNCTION trg_wallet_touch() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS wallet_touch ON wallets;
CREATE TRIGGER wallet_touch
    BEFORE UPDATE ON wallets
    FOR EACH ROW EXECUTE FUNCTION trg_wallet_touch();

-- ============================================================
--  SEED — cosmetics shop (10 items, no external assets needed)
-- ============================================================

INSERT INTO cosmetics (slug, name, description, category, price, asset_path) VALUES
    ('card_default',    'Noklusējuma kārtis',  'Klasiskais izskats.',             'card_skin',    0,    'skins/default.css'),
    ('card_gold',       'Zelta kārtis',        'Spīdīgs zelta dizains.',          'card_skin',    500,  'skins/gold.css'),
    ('card_neon',       'Neona kārtis',        'Spilgts neona efekts.',           'card_skin',    750,  'skins/neon.css'),
    ('card_dark',       'Tumšās kārtis',       'Minimālistisks tumšs stils.',     'card_skin',    400,  'skins/dark.css'),
    ('card_galaxy',     'Galaktikas kārtis',   'Kosmosa estētika.',               'card_skin',   1200,  'skins/galaxy.css'),
    ('card_retro',      'Retro kārtis',        '8-bit pikseļu stils.',            'card_skin',    600,  'skins/retro.css'),
    ('badge_whale',     '🐋 Baļķis',           'Vairāk nekā 10 000 uzvarēts.',    'badge',          0,  'badges/whale.svg'),
    ('badge_streak',    '🔥 Sērijas karalis',  '10 uzvaras pēc kārtas.',          'badge',          0,  'badges/streak.svg'),
    ('frame_gold',      'Zelta rāmis',         'Zelta avatāra rāmis.',            'avatar_frame', 800,  'frames/gold.css'),
    ('frame_animated',  'Animētais rāmis',     'Pulsējošs violets rāmis.',        'avatar_frame',1500,  'frames/animated.css')
ON CONFLICT (slug) DO NOTHING;

-- ============================================================
--  VIEWS  (handy for profile page queries)
-- ============================================================

CREATE OR REPLACE VIEW v_user_stats AS
SELECT
    u.id                                        AS user_id,
    u.username,
    COALESCE(w.balance, 0)                      AS balance,
    COALESCE(cs.count,  0)                      AS pixel_count,
    COUNT(DISTINCT uf.id)                       AS file_count,
    COUNT(DISTINCT cm.message_id)               AS message_count,
    COUNT(DISTINCT ct.id)                       AS casino_games,
    COALESCE(SUM(CASE WHEN ct.winnings > 0 THEN ct.winnings ELSE 0 END), 0) AS total_won,
    COALESCE(SUM(CASE WHEN ct.winnings < 0 THEN ct.winnings ELSE 0 END), 0) AS total_lost,
    COALESCE(MAX(ct.winnings), 0)               AS best_win
FROM users u
LEFT JOIN wallets              w   ON w.user_id  = u.id
LEFT JOIN canvas_scores        cs  ON cs.user_id = u.id
LEFT JOIN user_files           uf  ON uf.user_id = u.id
LEFT JOIN (SELECT id AS message_id, user_id FROM chat_messages) cm ON cm.user_id = u.id
LEFT JOIN casino_transactions  ct  ON ct.user_id = u.id
GROUP BY u.id, w.balance, cs.count;