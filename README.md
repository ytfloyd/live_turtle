# turtle-crypto

Turtle Trading system for crypto, executed against the Coinbase Advanced API
in a walled-off $10,000 portfolio. Scanner → trade sheet → executor, each
independently testable. Live money, contained blast radius.

```
  +----------+      +-------------+      +-----------+      +----------+
  | Coinbase |----->|   Scanner   |----->|   Trade   |----->| Executor |
  |  public  |      | (Donchian,  |      |   sheet   |      | (sanity  |
  |  /market |      |  ATR, 55d)  |      | (sizing,  |      |  checks, |
  +----------+      +-------------+      | classify) |      |  caps,   |
                                         +-----------+      |  audit)  |
                                                            +----------+
                                                                  |
                                                                  v
                                                          data/turtle_audit.db
```

No automatic stop placement. No exit logic. No pyramiding. No web UI. See
"What this system does NOT do (v1)" below.

## Quickstart

```bash
# 1. Install dependencies
uv sync --extra dev

# 2. Configure secrets
cp .env.example .env
# edit .env: set CDP_KEY_FILE_PATH (path to the JSON file downloaded from the
# CDP portal) and ALLOWED_PORTFOLIO_UUID (the $10k portfolio's UUID).

# 3. Run the read-only scanner
uv run python scripts/run_scanner.py

# 4. Dry-run the executor — prints every payload that would be POSTed,
#    logs intents to the audit DB with status='dry_run', and makes ZERO
#    state-changing API calls. Sanity checks still run.
uv run python scripts/execute_dry_run.py

# 5. Live execution with per-order confirmation prompts
uv run python scripts/execute_live.py

# 6. Query the audit DB
sqlite3 data/turtle_audit.db "SELECT id, ts, product_id, status FROM orders ORDER BY id DESC LIMIT 20;"
```

## Architecture

```
src/turtle_crypto/
  config.py         # every threshold, cap, magic number in one place
  coinbase_auth.py  # CDP JWT (ES256) signing — tested against a local keypair
  coinbase_client.py# thin HTTP wrapper, public + private, validates responses
  scanner.py        # universe discovery, candle fetch, Donchian + ATR
  trade_sheet.py    # sizing, classification, TradeOrder dataclass
  audit.py          # SQLite audit log, halt flag, daily counter
  executor.py       # order placement, hard caps, sanity checks

scripts/
  run_scanner.py    # read-only, no auth, no orders
  execute_dry_run.py# scanner + trade sheet + executor in dry-run mode
  execute_live.py   # scanner + trade sheet + live execution, confirm per-order

tests/
  test_auth.py      # JWT generation + CDP key parsing
  test_client.py    # HTTP client, Decimal-to-string, payload shapes
  test_scanner.py   # Donchian/ATR calc against fixture candles
  test_sizing.py    # canonical BTC sizing example + classification priority
  test_audit.py     # SQLite audit store + halt + daily counter
  test_executor.py  # every sanity check, every cap, every halt path

data/
  turtle_audit.db   # gitignored, created on first run
```

## The .env file

Exactly two required keys:

```
CDP_KEY_FILE_PATH=/absolute/path/to/cdp_api_key.json
ALLOWED_PORTFOLIO_UUID=00000000-0000-0000-0000-000000000000
```

The CDP key file is the JSON you download from the CDP portal when you create
an API key. It must contain a `name` field (format
`organizations/{org_id}/apiKeys/{key_id}`) and a `privateKey` field
(PEM-encoded EC private key starting with `-----BEGIN EC PRIVATE KEY-----`).

The portfolio UUID is the unique ID of your walled-off $10k portfolio. The
executor refuses to run if this UUID is not in the list of portfolios visible
to the API key. Every order POST carries this UUID as `retail_portfolio_id`.

## Order of operations

1. **Scanner only** (`run_scanner.py`) — first thing every morning. No auth,
   no risk. Confirms the universe looks sane and the signal list is what you
   expect.
2. **Dry run** (`execute_dry_run.py`) — performs all sanity checks against
   the live API (auth, portfolio binding, balance drift, audit DB) and prints
   the exact payload for every order. Nothing is sent.
3. **Live** (`execute_live.py`) — interactive per-order confirmation. Type
   `EXECUTE <ASSET>` exactly (e.g. `EXECUTE BTC`) to confirm, `SKIP`, or
   `ABORT`.

Never run step 3 without step 2 passing first.

## How to halt the system

The system has several overlapping kill switches. From gentlest to most total:

1. **Abort the current run**: at any confirmation prompt in `execute_live.py`,
   type `ABORT`. The run stops immediately, all subsequent orders are
   cancelled. Orders already filled remain filled.
2. **Ctrl-C**: every network call has a 20-second timeout and the executor
   never catches `KeyboardInterrupt`, so a Ctrl-C propagates up and stops the
   Python process.
3. **Automatic halt on error**: any failure during a live order (HTTP error,
   rejection, malformed response) sets the persistent `halt_flag` in the audit
   DB. Subsequent runs refuse to execute any orders until the halt is cleared.
4. **Manual halt via audit DB**: set the halt directly.
   ```bash
   sqlite3 data/turtle_audit.db \
     "INSERT INTO state(key, value) VALUES('halt_flag', '1') \
      ON CONFLICT(key) DO UPDATE SET value='1'; \
      INSERT INTO state(key, value) VALUES('halt_reason', 'manual halt') \
      ON CONFLICT(key) DO UPDATE SET value='manual halt';"
   ```
5. **Clear the halt** (only when you've investigated and decided it's safe):
   ```bash
   uv run python scripts/execute_live.py --reset-halt
   ```
   This prompts for `CLEAR HALT` as explicit confirmation.
6. **Revoke the API key**: the nuclear option. Log into the CDP portal and
   delete the key. Any JWT signed by it will be rejected immediately.

## Hard caps (enforced in `executor.py` even in dry-run)

| cap                    | value    | reference in config.py       |
|------------------------|----------|------------------------------|
| Max notional per order | $1,500   | `MAX_ORDER_NOTIONAL_USD`     |
| Max daily orders       | 15       | `MAX_DAILY_ORDERS`           |
| Max portfolio heat     | $1,200   | `MAX_PORTFOLIO_HEAT_USD`     |
| Balance tolerance      | ±20%     | `ACCOUNT_BALANCE_TOLERANCE`  |

The executor ALSO refuses to run if the live portfolio USD balance differs
from `ACCOUNT_SIZE` ($10,000) by more than 20%.

## Sizing formula

```
1 Unit (base) = (ACCOUNT_SIZE * RISK_PER_UNIT) / (STOP_LOSS_ATR_MULTIPLE * ATR)
              = ($10,000  *  0.01)            / (2                     * ATR_$)

Hand check (BTC at $72,000, ATR $2,400):
    = ($100) / ($4,800) = 0.02083... BTC
    ≈ $1,500 notional
    ≈ $100 at risk to a 2N stop   (= 1% of account)
```

Quantities are rounded DOWN to the pair's `base_increment`. Orders whose
rounded quantity falls below `base_min_size` are refused.

## Tests

All unit tests run offline, no network.

```bash
uv run pytest          # 77 tests across 6 files
```

Integration tests (hit the live API) live in `tests/integration/` and are
gated by a `--integration` flag (not included in v1 — add as needed).

## What this system does NOT do (v1)

- **No automatic stop-loss orders.** The 2N stops are computed and displayed,
  but never placed. Stop placement is Phase 2.
- **No exit logic.** Closing positions on 10-day low breaks is Phase 3.
- **No pyramiding.** First-unit entries only.
- **No Slack / Telegram / email notifications.** Terminal only.
- **No multi-account support.** Single portfolio, UUID from .env.
- **No backtesting framework.** Execution infrastructure only.
