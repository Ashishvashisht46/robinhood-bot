# Keeping the bot up

The dangerous state is not a crash. It is **holding tokens while nothing is
watching them** — no stop-loss, no take-profit, no timeout. That happened on
2026-09-12: the terminal closed at 22:39, the bot stopped, and a position sat
unsold overnight.

Restarting is always safe. `main.py` reconciles every saved position against its
real on-chain balance before resuming, so nothing is double-bought or lost.

## Two separate problems

| Problem | Fix |
|---|---|
| Bot exits (crash, RPC failure, unhandled error) | `run_bot.bat` — restarts it |
| Console window closed / logout | Task Scheduler — see below |

`run_bot.bat` alone does **not** survive the window closing. Windows kills child
processes when their console goes. Only Task Scheduler (or a service) avoids that.

## Level 1 — restart on crash

```
run_bot.bat
```

Runs the bot, restarts it 15s after any non-zero exit, and gives up after 10
restarts because a bot that cannot stay up is misconfigured, not unlucky. A clean
Ctrl+C exit is respected and not restarted. On exit it runs `status.py` so you
find out immediately if anything is left unmanaged.

## Level 2 — survive logout and window closure

Windows Task Scheduler already does everything a hand-written daemon would, so use
it rather than adding one.

**Register it (run this yourself — it arms automated trading):**

```
schtasks /Create ^
  /TN "RobinhoodBot" ^
  /TR "\"C:\Users\Ashish\Projects\robinhood-bot\.venv\Scripts\python.exe\" \"C:\Users\Ashish\Projects\robinhood-bot\main.py\"" ^
  /SC ONLOGON ^
  /RL HIGHEST ^
  /F
```

Then in Task Scheduler (`taskschd.msc`), open **RobinhoodBot** and set:

- **General → Run whether user is logged on or not** — survives logout
- **Settings → If the task fails, restart every: 1 minute, up to 3 times**
- **Settings → If the task is already running: Do not start a new instance**
  — this one matters. Two instances fight over the Telegram session file and
  *neither* receives calls.
- **Settings → Stop the task if it runs longer than: (uncheck)** — it is meant to
  run indefinitely
- **Conditions → Start the task only if the computer is on AC power: (uncheck)**
  on a laptop, or it will stop on battery

**Start / stop / remove:**

```
schtasks /Run    /TN "RobinhoodBot"
schtasks /End    /TN "RobinhoodBot"
schtasks /Delete /TN "RobinhoodBot" /F
```

## Before enabling any of this

Task Scheduler will start the bot **automatically on every logon**, with whatever
`DRY_RUN` is set to at that moment. Check it:

```
.venv\Scripts\python.exe -c "from config import Config; print('DRY_RUN =', Config().DRY_RUN)"
```

`DRY_RUN=false` plus auto-start means the bot trades real money every time you log
in, without you doing anything. That is the point of a supervisor, but it should be
a deliberate choice.

## Checking on it

```
.venv\Scripts\python.exe status.py
```

Reports whether the process is running, what the saved state believes, and whether
the wallet holds anything the bot is not tracking. Read-only, sells nothing.

Exit code 1 means something is unmanaged — usable in a scheduled check of its own.

## What a supervisor does not fix

- A position is still unmanaged during the gap between exit and restart (15s with
  `run_bot.bat`, up to a minute with Task Scheduler).
- If the machine is off or asleep, nothing is watching. Positions persist and
  resume on the next start, but the market does not wait.
- Crash-looping is caught and stopped, not diagnosed. Read `logs\bot.log`.
