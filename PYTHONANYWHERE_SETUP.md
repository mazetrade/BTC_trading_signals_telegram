# Deploying the BTC Signal Engine on PythonAnywhere

This guide explains how to run `signal_engine.py` on [PythonAnywhere](https://www.pythonanywhere.com) so it runs 24/7 and sends you a full 4-hour analysis on Telegram on every BTC 4H candle close.

---

## 1. Compatibility Check — Read This First

### What the script uses
- **Libraries:** only `requests` plus the Python standard library (`json`, `math`, `time`, `logging`, `datetime`, `pathlib`).
- **Network endpoints:**
  - `https://api.binance.com/api/v3/klines` — public market data, no API key.
  - `https://api.telegram.org/bot<token>/sendMessage` — your bot.
- **No GUI, no local file pickers, no database, no browser automation.** Everything runs headless.

### How that maps to PythonAnywhere tiers

| Requirement | Free tier | Paid tier (Hacker $5/mo+) |
|---|---|---|
| `api.binance.com` reachable | ✅ on the allowlist | ✅ unrestricted internet |
| `api.telegram.org` reachable | ✅ on the allowlist | ✅ unrestricted internet |
| `requests` library | ✅ installable | ✅ installable |
| **Always-on task (24/7 loop)** | ❌ **NOT available** | ✅ **one or more included** |
| Scheduled tasks | ⚠️ one per day, daily only (and gated to existing free accounts after 15 Jan 2026) | ✅ up to 20, hourly or daily |
| CPU seconds | 100/day (plenty for a 4H loop) | higher allowances |

### Verdict
The script's **libraries, network endpoints, and headless design are fully compatible** with PythonAnywhere's free tier. **But the engine's 24/7 `while True:` loop with a 4-hour sleep requires an Always-on Task, which is a paid-plan feature.** PythonAnywhere kills free-tier console processes and does not provide always-on workers.

You therefore have two real options:

- **Option A — Paid "Hacker" plan ($5/month) with an Always-on Task.** This matches the engine's design exactly: one long-running process, one analysis every 4 hours, 24/7. This is the recommended path and the guide below assumes it.
- **Option B — Free tier, reduced to one analysis per day.** Refactor the engine to run once and exit, then trigger it via the single daily scheduled task. You lose the 4H cadence — you get one analysis per day at a fixed UTC time. Details in §7.

Important: neither option requires you to whitelist IPs on the Binance side, because the script uses only **public** klines (no API key, no authentication, no signing).

---

## 2. What you need before starting

- A PythonAnywhere account (paid Hacker plan recommended — https://www.pythonanywhere.com/pricing/).
- The three files from your `Trading_BTC` folder:
  - `signal_engine.py`
  - `requirements.txt`
  - (`dashboard.html` is not used on the server — it's a local viewer only.)
- Your Telegram bot token and chat ID — these are already embedded in `signal_engine.py`, so no additional environment variables are needed.

---

## 3. Option A — Recommended: Always-on Task on a paid plan

### Step 1 — Upload the files

1. Log in to PythonAnywhere and open the **Files** tab.
2. Create a folder, for example `btc_engine/`.
3. Upload `signal_engine.py` and `requirements.txt` into that folder.

> Tip: if you prefer command-line, open a **Bash console** (Consoles tab) and use `git clone` or `wget` / `scp`.

### Step 2 — Create a virtualenv and install dependencies

Open a Bash console and run:

```bash
cd ~/btc_engine
mkvirtualenv --python=python3.10 btc-engine-venv
pip install -r requirements.txt
```

`mkvirtualenv` both creates the virtualenv and activates it. The name `btc-engine-venv` is used again later, so keep it consistent.

Sanity check — this should print the version without any errors:

```bash
python -c "import requests; print('requests', requests.__version__)"
```

### Step 3 — Do a one-time test run

Still in the console, with the venv active:

```bash
cd ~/btc_engine
python signal_engine.py
```

You should see:
- A `BTC Signal Engine (4H) is live!` message arrive in your Telegram chat.
- A log line in the console like `BTC=$... | HOLD | score=... | ...`.
- The engine logging `Sleeping Ns until next 4H cycle...`.

Once you've confirmed the Telegram message arrived, press **Ctrl+C** in the console to stop the test. The engine will be started properly as an always-on task in the next step.

### Step 4 — Create the Always-on Task

1. Open the **Tasks** tab → **Always-on tasks** section.
2. Click **Create a new always-on task**.
3. In the command field, enter (replace `YOUR_USERNAME` with your PythonAnywhere username):

   ```
   /home/YOUR_USERNAME/.virtualenvs/btc-engine-venv/bin/python /home/YOUR_USERNAME/btc_engine/signal_engine.py
   ```

4. Set the working directory if the option is offered: `/home/YOUR_USERNAME/btc_engine`.
5. Save.

The task will start within a minute. PythonAnywhere monitors it and **automatically restarts it if it crashes**, which covers your "if the engine ever crashes or stops, restart it" requirement with no extra effort.

### Step 5 — Verify it's running

- **Telegram:** you should see the startup message within ~30 seconds.
- **Task log:** on the Always-on task row click **View log** to see stdout/stderr live.
- **File log:** the engine also writes to `~/btc_engine/logs/engine.log`. Tail it from a Bash console:

  ```bash
  tail -f ~/btc_engine/logs/engine.log
  ```

- **Every 4 hours** (UTC 00:00 / 04:00 / 08:00 / 12:00 / 16:00 / 20:00 + 30s buffer) a new analysis message should arrive on Telegram, containing BUY/SELL/HOLD, entry/TP/SL, support/resistance, indicators, written analysis, and confidence score.

### Step 6 — Persisting state across restarts

The engine writes `data/state.json` (trade counter + last action) and `data/trades.json` (trade log) into `~/btc_engine/data/`. That folder lives on your PythonAnywhere home directory, which is persistent, so the 66-trade counter survives restarts and task stops.

---

## 4. Updating the engine later

When you edit `signal_engine.py`:

1. Upload the new version (Files tab, or `git pull`).
2. On the **Tasks** tab, stop the task, then start it again — PythonAnywhere re-spawns it with the new file.

You don't have to touch the virtualenv unless `requirements.txt` changes, in which case:

```bash
workon btc-engine-venv
cd ~/btc_engine
pip install -r requirements.txt
```

Then restart the task.

---

## 5. Monitoring and alerts

- **Is it alive?** The engine posts every 4 hours regardless of signal (BUY / SELL / HOLD). If you don't see a Telegram message for more than ~5 hours, something is wrong.
- **Where to look when something is wrong:**
  1. Always-on task log on the Tasks tab — catches import errors and early crashes.
  2. `~/btc_engine/logs/engine.log` — catches network and indicator-level errors.
  3. A manual run from a Bash console (`python signal_engine.py`) reproduces most issues fastest.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ProxyError: 403 Forbidden` when calling Binance/Telegram | You're on free tier and a sub-domain isn't allowlisted, or your account was created before the domain was added | Upgrade to any paid plan (unrestricted internet) or contact PythonAnywhere support to refresh your allowlist |
| `ModuleNotFoundError: requests` in the task log | Task command isn't using the virtualenv Python | Re-check the full path in the Always-on task command starts with `/home/YOUR_USERNAME/.virtualenvs/btc-engine-venv/bin/python` |
| Task keeps restarting in a crash loop | Code error on startup | Open the task log and the file log; the traceback will point to the line. Fix, re-upload, restart the task |
| Telegram startup message never arrives | Wrong token or chat ID | Confirm the values at the top of `signal_engine.py` match what BotFather gave you and what `getUpdates` shows for your chat |
| No message arrives at a 4H boundary | Process was killed or paused | On paid tier this should not happen; check the task log. On free tier (Option B) this is expected |
| Binance returns HTTP 451 or geo-block | PythonAnywhere's outbound IP is in a region Binance blocks | Switch data source to `data-api.binance.vision` (same API shape) — edit `fetch_candles()` to use that hostname |

---

## 7. Option B — Free tier, once-a-day analysis (compromised cadence)

This path does not meet the "every 4 hours" requirement. Use it only if you cannot upgrade and want *something* running.

1. Upload `signal_engine.py` and `requirements.txt` (as in §3).
2. Create the venv and install (as in §3).
3. **Refactor:** replace the infinite `main()` loop with a single-cycle call. The minimal change — at the bottom of `signal_engine.py` replace:

   ```python
   if __name__ == "__main__":
       main()
   ```

   with:

   ```python
   if __name__ == "__main__":
       state = load_state()
       try:
           run_cycle(state)
       except Exception as e:
           log.error(f"Run failed: {e}", exc_info=True)
   ```

   This makes the script fetch the latest 4H candles, send one Telegram message, and exit.

4. In the **Tasks** tab → **Scheduled tasks**, add one daily task at a UTC time that matches a 4H candle close (e.g. `00:01` UTC so it runs just after the 00:00 UTC close). Command:

   ```
   /home/YOUR_USERNAME/.virtualenvs/btc-engine-venv/bin/python /home/YOUR_USERNAME/btc_engine/signal_engine.py
   ```

5. You will now get one Telegram message per day. The 66-trade counter and state still persist via `data/state.json`.

If you later upgrade to a paid plan, revert the `__main__` block to `main()` and switch to an Always-on task (§3, Step 4).

---

## 8. File inventory

The two files that belong on the server:

- `signal_engine.py` — the engine.
- `requirements.txt` — one line: `requests==2.31.0`.

Files that do **not** need to be uploaded:

- `dashboard.html` — local viewer only; not used by the engine.
- `COWORK_TASK.md` — local notes.
- `data/`, `logs/` — created automatically by the engine on first run on the server.

---

## 9. Quick reference

Start (always-on task, paid tier):
```
Tasks → Always-on tasks → Create →
  /home/YOUR_USERNAME/.virtualenvs/btc-engine-venv/bin/python /home/YOUR_USERNAME/btc_engine/signal_engine.py
```

Manual test run (either tier):
```
workon btc-engine-venv
cd ~/btc_engine
python signal_engine.py
```

Tail logs:
```
tail -f ~/btc_engine/logs/engine.log
```

Reinstall deps after editing `requirements.txt`:
```
workon btc-engine-venv
pip install -r ~/btc_engine/requirements.txt
```
