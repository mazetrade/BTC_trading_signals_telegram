# COWORK TASK — BTC Signal Engine
# Paste this entire prompt into the Cowork tab to get started

## Task for Claude Cowork

You are managing a Bitcoin trading signal engine on this computer.
The files are in this linked folder. Here is what to do step by step:

### Step 1 — Install dependencies
Run this command:
```
pip install -r requirements.txt
```

### Step 2 — Set Telegram credentials
Open `signal_engine.py` and replace these two lines:
- `TELEGRAM_TOKEN   = "PASTE_YOUR_BOT_TOKEN_HERE"`
- `TELEGRAM_CHAT_ID = "PASTE_YOUR_CHAT_ID_HERE"`

I will give you the values. Wait for me to provide them.

### Step 3 — Run the engine in the background
Once credentials are set, run:
```
nohup python signal_engine.py >> logs/engine.log 2>&1 &
```
This keeps it running even when you are not actively in Cowork.

### Step 4 — Confirm it is working
Check the log file to confirm the engine started:
```
tail -20 logs/engine.log
```
Then tell me what you see.

### Ongoing commands I may give you:
- "Check the engine status" → tail the log file
- "How many trades so far?" → read data/trades.json and count entries
- "Show me the last 5 signals" → read data/trades.json and summarize the last 5
- "Stop the engine" → kill the python process
- "Restart the engine" → kill then relaunch with nohup
- "Change take profit to X%" → edit TAKE_PROFIT_PCT in signal_engine.py and restart
