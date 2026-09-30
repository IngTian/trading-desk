-- The book's schema. THIS FILE IS THE SOURCE OF TRUTH.
--
-- Extracted from hub.db at schema version 9 on 2026-09-07, when the database
-- moved out of the repository. The author: "data is data and code is code... ideally i
-- think it's better to have the data and code separated. so this repo is pure tool
-- instead of tool combined with data."
--
-- Before that, the schema lived only in sqlite_master. That was deliberate --
-- SQLite keeps DDL verbatim, comments and all, so a separate file would have been a
-- second copy that drifts. It stopped being right the moment the data left: 30
-- tables, 32 triggers and 9 views of constraints are CODE, and code that exists
-- only inside a binary cannot be reviewed, diffed, or used to create a fresh book.
--
-- EVERY CONSTRAINT HERE IS LOAD-BEARING and most carry the story of the bug that
-- produced them. Read the comments before relaxing anything.
--
-- Create a new book:   desk-migrate --new /path/to/hub.db
-- Check an existing:   desk-migrate --check /path/to/hub.db
--
-- Ordered tables -> indexes -> triggers -> views: a view may reference another view
-- and a trigger may reference anything.

PRAGMA foreign_keys = OFF;
BEGIN;

-- ==========================================================================
-- TABLES (30)
-- ==========================================================================
CREATE TABLE "account_snapshots" (
            as_of      TEXT NOT NULL
                       CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            total      REAL NOT NULL CHECK (total > 0),
            risk_free  REAL CHECK (risk_free IS NULL OR risk_free >= 0),
            source     TEXT NOT NULL CHECK (source IN ('statement','estimate')),
            notes      TEXT,
            PRIMARY KEY (as_of, account_id),
            CHECK (risk_free IS NULL OR risk_free <= total)
        ) STRICT;

-- AN ACCOUNT IS A NAME. The author, 2026-09-09: "an account is simply an account with a name
-- with whatever currencies (we can hold USD/CAD simultaneously inside the tfsa)".
--
-- `kind` was CHECK (kind IN ('tfsa','rrsp','non-registered','fhsa')) -- a Canadian tax
-- taxonomy baked into the schema, and `currency` was NOT NULL and single, which claimed a
-- TFSA is CAD *or* USD when their holds both. Both are dropped rather than corrected: they
-- were read by nothing in the schema and nothing in the Python, and a field that is wrong
-- and unread is not a field. Currency lives on the instrument, where it always did.
CREATE TABLE accounts (
            id        TEXT PRIMARY KEY NOT NULL,
            name      TEXT NOT NULL,
            number    TEXT,
            opened    TEXT,
            notes     TEXT
        , is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1))) STRICT;




CREATE TABLE bets (
    id             TEXT PRIMARY KEY NOT NULL,
    -- ADDED v13, AND THE SLUG STOPS BEING WHAT YOU TYPE. The author: "i dont want to write bets
    -- big-techs-and-semis. i just want to write Big Techs and Semis."
    --
    -- They are right, and daylogs already had the rule: whatever is NOT sigiled is free text.
    -- The id was doing two jobs -- a stable key that `!bet` can carry as one whitespace-free
    -- token, and the thing a person reads -- and the second job is what forced hyphens into
    -- prose. So `name` is the free text, `id` is a slug DERIVED from it once, and the two
    -- diverge freely afterwards: renaming edits the name and leaves every foreign key alone.
    -- NULL means the bet predates this column, and `Bet.name` falls back to the id.
    name           TEXT,
    status         TEXT NOT NULL CHECK (status IN
                     ('forming','live','weakened','falsified','closed','retired','abandoned')),
                   -- 'retired' and 'abandoned' added: consumer-rate-path was
                   -- retired without ever being tested, and the draft had no
                   -- value for it. 'weakened' because 9(c)'s k-of-n is not binary.
    opened         TEXT CHECK (opened IS NULL OR opened GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    closed         TEXT CHECK (closed IS NULL OR closed GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),

    thesis         TEXT,
    main_support   TEXT,

    -- A BET IS ALLOCATION PLUS BELIEF. It is NOT a risk-limit object.
    -- The author, 2026-09-05: "the leg wins. the curve only applies to the leg and not
    -- the bet. the bet only cares about total portfolio money, thesis,
    -- falsifiers, etc."
    -- So there is no loss limit here. Stops live on trades, and a bet's
    -- all-stops-at-once total is REPORTED (v_bet_all_stops, rule 7d) and never
    -- enforced -- enforcing it would retighten every existing leg whenever a new
    -- leg is added, which is the operational failure this design avoids.

    -- 权 dials. Declared BEFORE entry (rules 2 and 6). No CHECK against 50:
    -- exceeding the default writes a `deviations` row, it is not an error.
    max_weight_pct REAL CHECK (max_weight_pct IS NULL OR max_weight_pct > 0),

    -- The pod's capital, FROZEN IN CURRENCY at declaration. The author: "we assume that
    -- the bet's total count of the portfolio stays the same. if it moves, that's
    -- rare and we need to adjust the whole thing." A frozen amount is what makes
    -- every leg's share of the bet stable -- a live percentage of a moving account
    -- total would drift every time the account changes, which is the drift this
    -- whole design exists to remove. Re-baselining is a deliberate, recorded act.
    allocation_base     REAL CHECK (allocation_base IS NULL OR allocation_base > 0),
    allocation_ccy      TEXT,
    allocation_set_on   TEXT CHECK (allocation_set_on IS NULL OR
                          allocation_set_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    allocation_rebased_from REAL,   -- the prior figure, when re-baselined
    allocation_rebase_why   TEXT,
    min_gap_days   INTEGER CHECK (min_gap_days IS NULL OR min_gap_days >= 0),
    min_gap_move_pct REAL CHECK (min_gap_move_pct IS NULL OR min_gap_move_pct > 0),

    -- review_date dropped in v15: `r` wrote it, no screen read it, 0 of 6 bets had one
    notes          TEXT,                      -- mistaken for "preserved the content"

    CHECK (allocation_base IS NULL OR (allocation_ccy IS NOT NULL
                                       AND allocation_set_on IS NOT NULL)),
    CHECK (allocation_rebased_from IS NULL OR allocation_rebase_why IS NOT NULL),
    -- `CHECK (status <> 'live' OR (thesis IS NOT NULL AND main_support IS NOT NULL))`
    -- WAS HERE AND WENT IN v13, with the two falsifier triggers and the Python gate above
    -- it. THREE separate walls around one act, which is why removing the first two was not
    -- enough and a test is what found this one still standing.
    --
    -- The author, 2026-09-24: "i think we are still enforcing rules which shouldnt be at this
    -- stage. let me explore the best shape of those rules before committing too early."
    -- Nothing in the desk can write `thesis` or `main_support` either, so this demanded
    -- columns the tool could not fill -- a wall with no door, for the fourth time.
    CHECK (status = 'forming' OR opened IS NOT NULL),
    CHECK (status NOT IN ('closed','falsified') OR closed IS NOT NULL),
    CHECK (closed IS NULL OR opened IS NULL OR closed >= opened)
) STRICT;


CREATE TABLE confirmations (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL CHECK (scope IN ('trade','bet','book')),
    trade_id    TEXT REFERENCES trades(id) ON DELETE CASCADE,
    bet_id      TEXT REFERENCES bets(id) ON DELETE CASCADE,
    field       TEXT NOT NULL,
    issue       TEXT NOT NULL,
    opened_on   TEXT CHECK (opened_on IS NULL OR opened_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
                -- NULLABLE: several gaps were found while building the ledger and
                -- have no honest discovery date
    resolved_on TEXT,
    resolution  TEXT,
    notes       TEXT,
    CHECK (resolved_on IS NULL OR resolution IS NOT NULL),
    CHECK ((scope = 'trade' AND trade_id IS NOT NULL)
        OR (scope = 'bet'   AND bet_id   IS NOT NULL)
        OR (scope = 'book'  AND trade_id IS NULL AND bet_id IS NULL))
) STRICT;



CREATE TABLE events (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('insert','update','delete')),
    table_name TEXT NOT NULL,
    row_key    TEXT NOT NULL,
    before     TEXT,
    after      TEXT,
    reason     TEXT
) STRICT;

CREATE TABLE falsifiers (
    id          INTEGER PRIMARY KEY,
    bet_id      TEXT REFERENCES bets(id) ON DELETE CASCADE,
    trade_id    TEXT REFERENCES trades(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
                -- DEFERRED because a trade cites its exit falsifier and the
                -- falsifier names its trade: without it the pair is unwritable
                -- in either order.
    role        TEXT NOT NULL CHECK (role IN ('main-support','side-leg')),
    leg_label   TEXT,                          -- '1 rate path', '2 consumer mix'
    wrong_if    TEXT NOT NULL,
    watch_for   TEXT,
    threshold   TEXT,                          -- 'Core CPI >= 0.4% m/m twice'
    action      TEXT CHECK (action IS NULL OR action IN
                  ('exit','trim','re-rate','redo-analysis','leg-dead')),
                -- every CMG row prescribes one; the draft could hold none

    known_by            TEXT CHECK (known_by IS NULL OR
                          known_by GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    known_by_precision  TEXT CHECK (known_by_precision IS NULL OR
                          known_by_precision IN ('day','month','quarter')),
    known_by_prose      TEXT,                  -- 'the Q3 print', 'late Oct'
    cadence             TEXT,                  -- 'monthly, ~mid-month, 08:30 ET'
    source              TEXT,                  -- 'BLS CPI', 'CME FedWatch'

    written_on  TEXT CHECK (written_on IS NULL OR
                  written_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    provenance  TEXT NOT NULL CHECK (provenance IN
                  ('at-entry','added-later','reconstructed-from-note')),
                -- without these two, a falsifier written today can be cited by a
                -- close 19 days earlier and read as scoreable. Verified.

    resolved_on TEXT CHECK (resolved_on IS NULL OR resolved_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    outcome     TEXT CHECK (outcome IS NULL OR outcome IN ('held','broken','ambiguous')),
    revoked_on  TEXT CHECK (revoked_on IS NULL OR revoked_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    revoked_why TEXT,
                -- 2026-08-20-soxl's falsifier was REVOKED rather than obeyed.
                -- "no trigger was written" and "the written one was withdrawn"
                -- are different findings and the second is more damning.

    kind        TEXT CHECK (kind IS NULL OR kind IN ('vehicle','parent-evidence')),
    notes       TEXT,

    CHECK ((bet_id IS NOT NULL) <> (trade_id IS NOT NULL)),
    CHECK (trade_id IS NOT NULL OR kind IS NULL),
    CHECK (resolved_on IS NULL OR written_on IS NULL OR resolved_on >= written_on),
    CHECK ((resolved_on IS NULL) = (outcome IS NULL)),
    CHECK (revoked_on IS NULL OR revoked_why IS NOT NULL),
    -- a dateless falsifier must at least say WHEN in prose, or it is unusable
    CHECK (known_by IS NOT NULL OR known_by_prose IS NOT NULL OR cadence IS NOT NULL)
) STRICT;

CREATE TABLE fills (
    id          INTEGER PRIMARY KEY,
    trade_id    TEXT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    -- outbox_id dropped in v14: the outbox table went with the unbuilt order-proposal flow
                -- UNIQUE: reporting one order's fill twice would double-count
                -- exposure with nothing to stop it. NULL covers historical fills.
    on_date     TEXT CHECK (on_date IS NULL OR on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    kind        TEXT NOT NULL CHECK (kind IN ('open','add','trim','close')),
    shares      REAL NOT NULL CHECK (shares <> 0),
    price       REAL CHECK (price IS NULL OR price > 0),
    fee         REAL CHECK (fee IS NULL OR fee >= 0),
                -- >= 0: a negative statement-sourced fee would offset the whole
                -- realised column and look authoritative doing it
    fee_source  TEXT CHECK (fee_source IS NULL OR fee_source IN ('statement','estimated')),
    settled_base REAL CHECK (settled_base IS NULL OR settled_base > 0),
    settled_base_source TEXT CHECK (settled_base_source IS NULL OR
                          settled_base_source IN ('statement','derived-from-fx')),
                -- the FX conversion cost has no other home. `fee` is a per-fill
                -- commission in trade currency; a CAD<->USD conversion spread is
                -- neither. NOTE 2026-09-05: the author holds USD and CAD sleeves
                -- simultaneously, so there is NO per-trade conversion -- the cost
                -- sits at the funding boundary. This column exists so that can be
                -- shown from the statement rather than assumed either way.
    -- tranche_rung dropped in v14: bet_tranches went with it; 0 rows ever
    reason      TEXT,
    -- at_time dropped in v15: the `@HH:MM` grammar wrote it, nothing read it, 0 of 48
    notes       TEXT,
    CHECK ((kind IN ('open','add') AND shares > 0) OR (kind IN ('trim','close') AND shares < 0)),
    CHECK (fee IS NULL OR fee_source IS NOT NULL),
    CHECK (settled_base IS NULL OR settled_base_source IS NOT NULL)
) STRICT;

CREATE TABLE flows (
    id      INTEGER PRIMARY KEY,
    on_date TEXT NOT NULL CHECK (on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    amount  REAL NOT NULL CHECK (amount <> 0),
    kind    TEXT NOT NULL CHECK (kind IN ('deposit','withdrawal')),
    -- ADDED v11. Net is DERIVED from cash plus positions now, and cash is tracked PER
    -- CURRENCY because one account holds both -- so a deposit with no currency cannot be
    -- put in either pile. The DEFAULT is for the v10->v11 rebuild, where all six existing
    -- flows were CAD; every writer passes it explicitly.
    currency TEXT NOT NULL DEFAULT 'CAD' CHECK (currency IN ('CAD','USD')),
    notes   TEXT, account_id TEXT REFERENCES accounts(id),
    CHECK ((kind = 'deposit' AND amount > 0) OR (kind = 'withdrawal' AND amount < 0))
) STRICT;

-- THE OFFSET. The author, 2026-09-09: "we will have some offsets as we accumulate. probably from
-- dividends and fees. but they are just way too little to track, we will just add a special
-- offset entry into the account balance ... if the diff is small, it's good enough."
--
-- One signed cash correction. It is how dividends, fees and interest reach the derived net
-- without a table each, and a currency conversion is two rows -- negative in the currency
-- sold, positive in the one bought.
--
-- IT IS NOT CONTRIBUTED CAPITAL and must never be summed into it. A deposit changes what
-- The author put in; an offset corrects what the book failed to notice. Mixing them would move the
-- denominator of every return figure on the screen.
CREATE TABLE adjustments (
    id       INTEGER PRIMARY KEY,
    on_date  TEXT NOT NULL CHECK (on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    currency TEXT NOT NULL CHECK (currency IN ('CAD','USD')),
    amount   REAL NOT NULL CHECK (amount <> 0),
    notes    TEXT
) STRICT;

CREATE TRIGGER adjustments_account_must_exist BEFORE INSERT ON adjustments
WHEN NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id)
BEGIN SELECT RAISE(ABORT,'adjustments.account_id names no account'); END;

CREATE TABLE fx_conversions (
            id          INTEGER PRIMARY KEY,
            on_date     TEXT CHECK (on_date IS NULL OR
                          on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
                        -- NULL = NOT RECORDED. The author remembers the amount, not the day.
            account_id  TEXT NOT NULL REFERENCES accounts(id),
            from_ccy    TEXT NOT NULL,
            from_amount REAL NOT NULL CHECK (from_amount > 0),
            to_ccy      TEXT NOT NULL,
            to_amount   REAL CHECK (to_amount IS NULL OR to_amount > 0),
            to_amount_source TEXT CHECK (to_amount_source IS NULL OR
                          to_amount_source IN ('statement','derived')),
                        -- 'derived' means it was BACKED OUT of the sleeve, which
                        -- makes the P&L residual stop being an independent check on
                        -- this row. Only a statement restores that.
            notes       TEXT,
            CHECK (from_ccy <> to_ccy),
            CHECK (to_amount IS NULL OR to_amount_source IS NOT NULL)
        ) STRICT;

CREATE TABLE income (
            id          INTEGER PRIMARY KEY,
            on_date     TEXT NOT NULL
                        CHECK (on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
            account_id  TEXT NOT NULL REFERENCES accounts(id),
            ticker      TEXT REFERENCES instruments(ticker),  -- NULL for account-level
            kind        TEXT NOT NULL CHECK (kind IN
                          ('dividend','interest','stock-lending','other')),
            amount      REAL NOT NULL CHECK (amount > 0),
            currency    TEXT NOT NULL,
            source      TEXT NOT NULL CHECK (source IN ('statement','estimate')),
            notes       TEXT
        ) STRICT;

CREATE TABLE instruments (
    ticker            TEXT PRIMARY KEY NOT NULL,
    name              TEXT,
    currency          TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN
                        ('common','etf','leveraged-etf','money-market','option','future')),
    leverage_factor   REAL CHECK (leverage_factor IS NULL OR leverage_factor <> 0),
    is_daily_reset    INTEGER NOT NULL DEFAULT 0 CHECK (is_daily_reset IN (0,1)),
    risk_free         INTEGER NOT NULL DEFAULT 0 CHECK (risk_free IN (0,1)),
    notes             TEXT, quote_symbol TEXT,
    CHECK (kind <> 'leveraged-etf' OR leverage_factor IS NOT NULL)
) STRICT;






CREATE TABLE prices (
    on_date TEXT NOT NULL CHECK (on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    base    TEXT NOT NULL,
    quote   TEXT NOT NULL,
    amount  REAL NOT NULL CHECK (amount > 0),
    kind    TEXT NOT NULL CHECK (kind IN ('mark','fx')),
    source  TEXT NOT NULL,
    notes   TEXT,
    PRIMARY KEY (base, quote, kind, on_date)   -- leads on base: the lookup shape
) STRICT;




CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY NOT NULL,
    applied_at TEXT NOT NULL,
    filename   TEXT NOT NULL,
    notes      TEXT
) STRICT;

CREATE TABLE session (
    only_row INTEGER PRIMARY KEY NOT NULL CHECK (only_row = 1),
    actor    TEXT NOT NULL
) STRICT;

CREATE TABLE trades (
    id               TEXT PRIMARY KEY NOT NULL,
    -- NULLABLE since v10. A position does not have to belong to a bet: the author,
    -- 2026-09-08, buying SGOV/CBIL -- "im basically parking my principal without letting
    -- them eat dust ... this has nothing to do with any bet." The NOT NULL is what forced
    -- the 'cash-parking' pseudo-bet into existence, and that bet's own note says so:
    -- "NOT A BET ... It exists only because trades.bet_id is NOT NULL."
    bet_id           TEXT REFERENCES bets(id),
    ticker           TEXT NOT NULL REFERENCES instruments(ticker),
    currency         TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('long','protective-put')),
                     -- Rule 3 is a wall. But `side` is a LABEL; the arithmetic
                     -- guard is the no_net_short trigger below, because the
                     -- derivation is about a position that GROWS against you and
                     -- a fat-fingered oversell creates one without touching this.
    status           TEXT NOT NULL CHECK (status IN ('planned','open','closed','not-taken')),

    why_this_vehicle TEXT,

    opened           TEXT CHECK (opened IS NULL OR opened GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    closed           TEXT CHECK (closed IS NULL OR closed GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    -- ruleset_as_of dropped in v14: no rules to pin a version to. The author: "no rules."
                     -- WHICH RULES JUDGE THIS TRADE. Normally = opened. Stored
                     -- rather than derived because for a backfilled trade "when
                     -- it happened" and "which rules were actually applied to it"
                     -- came apart -- which is true of all eight existing trades.
    horizon_as_written TEXT,                   -- 'quarters', 'days-to-weeks' -- the
                                               -- record's actual content. Dropping
                                               -- it silently retires rule 4's check.
    horizon_end      TEXT CHECK (horizon_end IS NULL OR horizon_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    horizon_end_source TEXT CHECK (horizon_end_source IS NULL OR
                         horizon_end_source IN ('stated','inferred')),

    stop             REAL CHECK (stop IS NULL OR stop > 0),
    target           REAL CHECK (target IS NULL OR target > 0),
    dollar_cap       REAL CHECK (dollar_cap IS NULL OR dollar_cap > 0),
    dollar_cap_ccy   TEXT,                     -- 500 is C$693 or C$500: a 38.7%
                                               -- swing in the number 7d/8/2a
                                               -- divide by
    -- Options. Without a multiplier a protective put is wrong by 100x: one put at
    -- a 5.00 premium records $5 of cost against an actual $500, and that error
    -- flows straight into the 20% loss limit and the 50% cap.
    strike           REAL CHECK (strike IS NULL OR strike > 0),
    expiry           TEXT CHECK (expiry IS NULL OR expiry GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    contract_multiplier REAL CHECK (contract_multiplier IS NULL OR contract_multiplier > 0),

    -- Rule 1, settled 2026-09-05: leveraged ETF max 10% weight AND a 20% loss
    -- limit on the position. 10% x 20% = 2% of portfolio worst case.
    -- leveraged_factor dropped in v14: nothing wrote it and nothing read it; The author: "we dont enforce rules on leveraged positions as well"
    -- THE LOSS LIMIT LIVES ON THE TRADE, NOT THE BET. The author, 2026-09-05: "my
    -- preference is to trades bcs once you increase the size for the bets your sl
    -- gets changed then that's hard to operate."
    --
    -- This is also SUFFICIENT, not merely convenient: a bet's loss is the
    -- weighted average of its legs' losses, and a weighted average of numbers
    -- each >= -L is >= -L. So per-trade stops at L bound the bet at L exactly.
    -- The only leak is a stop that does not FILL at its level, which the ledger
    -- already documents ("the $90 stop was not a floor given three stacked
    -- mechanical sellers", 2026-08-16-soxl).
    loss_limit_pct   REAL CHECK (loss_limit_pct IS NULL OR
                                 (loss_limit_pct > 0 AND loss_limit_pct <= 100)),
    loss_limit_basis TEXT CHECK (loss_limit_basis IS NULL OR
                       loss_limit_basis IN ('gross-deployed','current-cost')),
                     -- 20% of WHAT. A $60 loss on a $1,000 position is 6% of gross
                     -- deployed, but 15% of cost once a trim has taken the position
                     -- down to $400 -- so on one reading a trim silently resets the
                     -- limit. Prefer 'gross-deployed': it does not move when you trim.
    loss_limit_source TEXT CHECK (loss_limit_source IS NULL OR loss_limit_source IN
                       ('curve-at-bet-max','hand-set','rule-1-leveraged')),
                     -- 'curve-at-bet-max' is the operational answer to the author's
                     -- concern. The curve is read at the bet's DECLARED
                     -- max_weight_pct, never its current weight -- so building
                     -- 5%->15%->30%->50% does NOT walk the limit down
                     -- 50%->22%->18%->15% and force every existing stop to move.
                     -- It changes only when max_weight_pct changes, which is a
                     -- recorded deviation, i.e. a decision rather than drift.
    stop_anchor_price REAL CHECK (stop_anchor_price IS NULL OR stop_anchor_price > 0),
    stop_set_on      TEXT CHECK (stop_set_on IS NULL OR
                       stop_set_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
                     -- The stop is a PRICE, computed once from the anchor. It is
                     -- recomputed only when the author adds to THIS trade -- a moment they
                     -- is already at the keyboard deciding. Nothing elsewhere in
                     -- the book can move it.

    -- exit_trigger AND exit_falsifier_id DROPPED IN v15, with the CHECK that tied them
    -- ("you may not claim a falsifier exit without naming the falsifier") and the two
    -- triggers that verified the reference. close_trade wrote exit_trigger='none-written'
    -- on every close, so 7 of 11 closed trades held a value meaning "empty"; the other 4
    -- said 'portfolio', hand-written in SQL because nothing on the desk could set it and
    -- nothing on the desk could show it. exit_falsifier_id was never written at all: 0 of
    -- 23 trades. Its only reader was v_scoreable_closes, which graded whether a close
    -- COULD be scored -- the same grading the curve and the rules engine went for in v14.
    --
    -- A close that needs a reason gets one in `notes`, which is read. The four 'portfolio'
    -- values were folded into those trades' notes by the migration, not discarded.
    notes            TEXT, account_id TEXT REFERENCES accounts(id),

    -- NOT A CHECK, DELIBERATELY. "A leveraged position must carry a loss limit"
    -- is POLICY (rule 1, settled 2026-09-05), not structure. Enforcing it here
    -- made both SOXL trades uninsertable, because they opened before the rule
    -- existed. It now lives in `rule_versions` and is evaluated against
    -- ruleset_as_of, so history is judged by the rules it was taken under. The
    -- earlier dated-amnesty clause is gone: it would have needed one new dated
    -- clause per rule change, forever, in a database with no DROP CONSTRAINT.

    CHECK (status <> 'closed' OR closed IS NOT NULL),
    CHECK (closed IS NULL OR opened IS NULL OR closed >= opened),
    CHECK (side <> 'protective-put' OR (strike IS NOT NULL AND expiry IS NOT NULL
                                        AND contract_multiplier IS NOT NULL)),
    CHECK (dollar_cap IS NULL OR dollar_cap_ccy IS NOT NULL),
    CHECK (loss_limit_pct IS NULL OR loss_limit_basis IS NOT NULL)
) STRICT;


-- ==========================================================================
-- INDEXS (12)
-- ==========================================================================

CREATE INDEX ix_events_table_row   ON events(table_name, row_key);

CREATE INDEX ix_falsifiers_bet     ON falsifiers(bet_id, role);

CREATE INDEX ix_falsifiers_trade   ON falsifiers(trade_id);

CREATE INDEX ix_fills_trade        ON fills(trade_id);


CREATE INDEX ix_prices_lookup      ON prices(base, kind, on_date);

CREATE INDEX ix_trades_bet         ON trades(bet_id, status);

CREATE UNIQUE INDEX one_default_account ON accounts(is_default) WHERE is_default=1;


CREATE UNIQUE INDEX ux_one_main_support_bet
  ON falsifiers(bet_id)   WHERE role = 'main-support' AND bet_id   IS NOT NULL;

CREATE UNIQUE INDEX ux_one_main_support_trade
  ON falsifiers(trade_id) WHERE role = 'main-support' AND trade_id IS NOT NULL;


-- ==========================================================================
-- TRIGGERS (32)
-- ==========================================================================

-- bet_live_needs_falsifier_ins AND _upd WERE HERE AND WERE DROPPED IN v13. They refused
-- any bet entering `live` without a main-support falsifier, and book.set_bet_status added
-- a thesis and main_support to the same demand.
--
-- The author, 2026-09-24: "i cant put semis-and-big-techs to live bc of this. i think we are
-- still enforcing rules which shouldnt be at this stage. let me explore the best shape of
-- those rules before committing too early."
--
-- It was the last enforcement left in a book whose stated rule is that it enforces
-- NOTHING, and it failed the same way the loss-limit curve and the `i` key did: the tool
-- deciding when they may record what has already happened. A status is an OBSERVATION. A bet
-- they are running is live whether or not the prose has caught up, and refusing the record
-- does not produce a thesis -- it produces a book that disagrees with reality. The nudge
-- survives in the honest form: an empty thesis renders as empty.
--
-- WHAT WOULD BRING THEM BACK: a shape they have chosen, after running the book without them.
-- Note that these triggers could never have worked as intended anyway -- nothing in the
-- desk can write a falsifier, so the condition they demanded was unsatisfiable through
-- the tool. That made them a wall with no door, which is the pattern this project has now
-- removed three times.

CREATE TRIGGER closed_trade_holds_nothing AFTER UPDATE OF status ON trades
WHEN NEW.status = 'closed'
 AND abs(COALESCE((SELECT SUM(shares) FROM fills WHERE trade_id = NEW.id), 0)) > 1e-9
BEGIN SELECT RAISE(ABORT,'cannot close a trade that still holds shares'); END;


CREATE TRIGGER ev_bets_ins AFTER INSERT ON bets BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'insert', 'bets', NEW.id, NULL,
          json_object('status',NEW.status,'allocation_base',NEW.allocation_base));
END;

CREATE TRIGGER ev_bets_upd AFTER UPDATE ON bets BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'update', 'bets', NEW.id,
          json_object('status',OLD.status,'thesis',OLD.thesis,'main_support',OLD.main_support,
                      'allocation_base',OLD.allocation_base,'max_weight_pct',OLD.max_weight_pct),
          json_object('status',NEW.status,'thesis',NEW.thesis,'main_support',NEW.main_support,
                      'allocation_base',NEW.allocation_base,'max_weight_pct',NEW.max_weight_pct));
END;

CREATE TRIGGER ev_falsifiers_ins AFTER INSERT ON falsifiers BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'insert', 'falsifiers',
          CAST(NEW.id AS TEXT), NULL,
          json_object('bet_id',NEW.bet_id,'trade_id',NEW.trade_id,'role',NEW.role,
                      'wrong_if',NEW.wrong_if,'known_by',NEW.known_by,
                      'provenance',NEW.provenance,'written_on',NEW.written_on));
END;

CREATE TRIGGER ev_falsifiers_upd AFTER UPDATE ON falsifiers BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'update', 'falsifiers',
          CAST(NEW.id AS TEXT),
          json_object('wrong_if',OLD.wrong_if,'known_by',OLD.known_by,
                      'outcome',OLD.outcome,'revoked_on',OLD.revoked_on),
          json_object('wrong_if',NEW.wrong_if,'known_by',NEW.known_by,
                      'outcome',NEW.outcome,'revoked_on',NEW.revoked_on));
END;

CREATE TRIGGER ev_fills_ins AFTER INSERT ON fills BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'insert', 'fills',
          CAST(NEW.id AS TEXT), NULL,
          json_object('trade_id',NEW.trade_id,'shares',NEW.shares,'price',NEW.price));
END;

CREATE TRIGGER ev_fills_upd AFTER UPDATE ON fills BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'update', 'fills',
          CAST(NEW.id AS TEXT),
          json_object('shares',OLD.shares,'price',OLD.price,'fee',OLD.fee),
          json_object('shares',NEW.shares,'price',NEW.price,'fee',NEW.fee));
END;

CREATE TRIGGER ev_trades_ins AFTER INSERT ON trades BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'insert', 'trades', NEW.id, NULL,
          json_object('bet_id',NEW.bet_id,'ticker',NEW.ticker,'status',NEW.status,
                      'stop',NEW.stop,'loss_limit_pct',NEW.loss_limit_pct));
END;

CREATE TRIGGER ev_trades_upd AFTER UPDATE ON trades BEGIN
  INSERT INTO events(at,actor,action,table_name,row_key,before,after)
  VALUES (datetime('now'), (SELECT actor FROM session), 'update', 'trades', NEW.id,
          json_object('status',OLD.status,'stop',OLD.stop,'target',OLD.target,
                      'loss_limit_pct',OLD.loss_limit_pct),
          json_object('status',NEW.status,'stop',NEW.stop,'target',NEW.target,
                      'loss_limit_pct',NEW.loss_limit_pct));
END;

CREATE TRIGGER events_are_append_only_del BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT,'events is append-only'); END;

CREATE TRIGGER events_are_append_only_upd BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT,'events is append-only'); END;

-- THE TWO exit_falsifier_must_exist_and_belong TRIGGERS WERE HERE. They verified that a
-- cited falsifier belonged to the trade or its bet -- a real integrity guard, on a column
-- dropped in v15 that no writer ever set.

CREATE TRIGGER falsifier_delete_would_unmoor_a_live_bet BEFORE DELETE ON falsifiers
WHEN OLD.role = 'main-support' AND OLD.bet_id IS NOT NULL
 AND (SELECT status FROM bets WHERE id = OLD.bet_id) = 'live'
 AND NOT EXISTS (SELECT 1 FROM falsifiers
                 WHERE bet_id = OLD.bet_id AND role = 'main-support' AND id <> OLD.id)
BEGIN SELECT RAISE(ABORT,'cannot remove the only main-support falsifier of a live bet'); END;

CREATE TRIGGER flows_account_must_exist
            BEFORE INSERT ON flows
            WHEN NEW.account_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id)
            BEGIN SELECT RAISE(ABORT,'flows.account_id names no account'); END;

CREATE TRIGGER fx_account_must_exist BEFORE INSERT ON fx_conversions
        WHEN NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id)
        BEGIN SELECT RAISE(ABORT,'fx_conversions.account_id names no account'); END;

CREATE TRIGGER income_account_must_exist BEFORE INSERT ON income
        WHEN NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id)
        BEGIN SELECT RAISE(ABORT,'income.account_id names no account'); END;


CREATE TRIGGER no_fills_on_a_planned_trade BEFORE INSERT ON fills
WHEN (SELECT status FROM trades WHERE id = NEW.trade_id) IN ('planned','not-taken')
BEGIN SELECT RAISE(ABORT,'a planned or not-taken trade cannot have fills'); END;

CREATE TRIGGER no_net_short AFTER INSERT ON fills
WHEN (SELECT SUM(shares) FROM fills WHERE trade_id = NEW.trade_id) < -1e-9
BEGIN SELECT RAISE(ABORT,'this fill would create a net short position (rule 3)'); END;




CREATE TRIGGER trades_account_must_exist
            BEFORE INSERT ON trades
            WHEN NEW.account_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id)
            BEGIN SELECT RAISE(ABORT,'trades.account_id names no account'); END;

-- `NEW.bet_id IS NOT NULL AND` is not decoration: NOT EXISTS(SELECT 1 ... WHERE
-- id = NULL) is TRUE, so without it a bet-less trade is still refused and the nullable
-- column above would do nothing. trades_account_must_exist already used this pattern.
CREATE TRIGGER trades_bet_must_exist_ins BEFORE INSERT ON trades
WHEN NEW.bet_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM bets WHERE id = NEW.bet_id)
BEGIN SELECT RAISE(ABORT,'trades.bet_id names no bet in the registry'); END;

CREATE TRIGGER trades_bet_must_exist_upd BEFORE UPDATE OF bet_id ON trades
WHEN NEW.bet_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM bets WHERE id = NEW.bet_id)
BEGIN SELECT RAISE(ABORT,'trades.bet_id names no bet in the registry'); END;

CREATE TRIGGER trades_instrument_must_exist BEFORE INSERT ON trades
WHEN NOT EXISTS (SELECT 1 FROM instruments WHERE ticker = NEW.ticker)
BEGIN SELECT RAISE(ABORT,'trades.ticker names no instrument in the registry'); END;


-- ==========================================================================
-- VIEWS (9)
-- ==========================================================================
-- v_bet_deployment WAS HERE AND IS GONE IN v16, for the third of the four reasons the
-- v_bet_exposure tombstone below already gives: it summed each leg's cost in that leg's
-- NATIVE currency and check.py divided the result by a HOME-currency allocation_base. On
-- one live book that printed a fourth 'deployed' percentage -- 43.8% where the desk said
-- 62.1% for the same bet at the same moment -- and the over-allocation warning it fed
-- could not fire until a USD-legged bet was about 1.38x past its CAD plan. `WHERE
-- average_price IS NOT NULL` added a second silent understatement by dropping unpriced
-- legs instead of withholding the row, which is the thing SQL cannot do.
--
-- The warning moved to check.py over book.bets(), where Bet.cost is already in the home
-- currency and is already None when any leg cannot be translated. The author, 2026-09-29: "we
-- should be more consistent in using the user's home currency."

-- v_bet_exposure WAS HERE AND WAS DROPPED IN v12. It computed each bet's weight and had
-- four defects, only the first of which was obvious:
--
--   * its denominator was the pooled `account_snapshots`, and nothing writes that table
--     any more, so every weight was measured against a statement dated 2026-09-07 and
--     drifted further from the truth every day;
--   * `COALESCE((SELECT amount FROM prices WHERE kind='fx' ...), 1.0)` -- A USD LEG WITH
--     NO RATE WAS VALUED AT PAR, wrong by the whole factor, about 38% for CAD/USD. That
--     is the one hazard this project has fixed in four other places, and it survived here
--     because SQL has no way to say "withhold this figure";
--   * `quote='CAD'` was hardcoded, so a USD home currency found no row and fell into the
--     par branch above;
--   * it measured at COST, which is what was committed rather than what is at risk.
--
-- desk/book.py:bets() computes it now, where Position.value_base already returns None for
-- an unpriced leg and the home currency comes from the profile. The lesson is not about
-- this view: a figure that must be WITHHELD when its inputs are missing does not belong in
-- a view, because every SQL tool for absent values -- COALESCE, IFNULL, a LEFT JOIN
-- default -- substitutes a number instead.
--
-- `account_snapshots` itself is KEPT, with its two rows. It is the author's real statement history
-- and desk/check.py still reconciles against it. Nothing in the desk reads or writes it.



CREATE VIEW v_position AS
SELECT t.id AS trade_id, t.bet_id, t.ticker, t.currency, t.status,
       COALESCE(SUM(f.shares), 0) AS shares_held,
       CASE WHEN SUM(CASE WHEN f.shares > 0 AND f.price IS NULL THEN 1 ELSE 0 END) > 0
            THEN NULL
            ELSE SUM(CASE WHEN f.shares > 0 THEN f.shares * f.price END)
                 / NULLIF(SUM(CASE WHEN f.shares > 0 THEN f.shares END), 0)
       END AS average_price,
       SUM(CASE WHEN f.on_date IS NULL OR (f.shares > 0 AND f.price IS NULL)
                THEN 1 ELSE 0 END) AS unconfirmed_fills
FROM trades t LEFT JOIN fills f ON f.trade_id = t.id   -- LEFT: planned and
GROUP BY t.id;


-- v_scoreable_closes WAS HERE. It answered "could this close be scored" -- whether the
-- falsifier it cited was dated, written before the close, and not reconstructed after the
-- fact. Every column it produced was built on exit_trigger and exit_falsifier_id, dropped
-- in v15, and its subject was GRADING their reasoning, which is what the curve and the rules
-- engine were deleted for in v14. The falsifiers table itself is untouched: all 14 stay.

-- The matching COMMIT for the BEGIN at the top of this file. It sat immediately after
-- v_trades_without_a_ruleset, the last view dropped in v14, and went with it -- leaving a
-- schema that applied cleanly, reported all 52 objects, and wrote ZERO BYTES to disk,
-- because the stray BEGIN made the whole script one uncommitted transaction that close()
-- rolled back. `migrate.create()` printed success either way.
COMMIT;
