<img src="static/icon.png" width="96" alt="Balance icon">

# Balance

A money app for your Mac. Track what you spend, what you earn, and what you own
— all of it stored on your own computer.

**Nothing leaves your Mac.** There is no account to make and no server to sign
in to. Your figures live in one file on your own disk.

## Install

You need a Mac with Apple silicon (an M1, M2, M3 or M4 chip). To check, open the
Apple menu, then **About This Mac** — the Chip line should say Apple, not Intel.

**1.** Download `Balance-macOS-arm64.zip` from the
[latest release](../../releases/latest) and double-click to unzip.

**2.** Drag `Balance.app` into your Applications folder.

**3.** Open **Terminal** (press Cmd+Space, type Terminal, press Return), paste
this line and press Return:

```
xattr -cr /Applications/Balance.app
```

Nothing will print. That is normal. Now open Balance from your Applications
folder. You only need this once — the app is not signed with a paid Apple
developer certificate, so macOS blocks it until you clear the flag.

## Getting later versions

Quit Balance, then paste this into Terminal:

```
curl -fsSL https://raw.githubusercontent.com/JuliusPaulin/balance-app/main/update.sh | bash
```

It fetches the newest release, installs it and clears the macOS block. Your
figures are untouched — they live outside the app.

## What it does

- **Transactions** — add, edit, delete and filter expenses and income
- **CSV import** — Finnish bank statements, Nordea account and Platinum exports,
  Finnair credit card
- **Auto-categories** — merchant rules (exact, contains, fuzzy) sort rows for
  you, with a live preview while you write a rule and one-click re-apply to past
  transactions
- **Dashboard** — month by month spend against income, spending and income
  broken down by category, and annual reports. Click a month to see everything
  in it; the period picker follows you down the page
- **Spending heatmap** — a year calendar you can drill into by day
- **Net worth** — accounts, loans and investment holdings over time
- **Split costs** — halve every imported amount in one click, for shared spending
- **Month notes** — attach context to any month
- **Balance AI** — ask about your money in plain English, answered by a model
  running on your own Mac (see below)
- **Auto-backup** — a safe snapshot before every import and on quit

## Balance AI

Ask it things — *what did I spend on groceries last month*, *did I spend more in
July than in June*, *analyse my latest month and tell me what stands out* — and
it answers from your own figures.

It runs on your Mac. Not a service, not an account, no API key: the app carries
its own model and never sends a word of your spending anywhere. It works with
the wi-fi off.

**The first time you open it, it downloads the model — about 2.7 GB, once.**
There is a button and a progress bar, the rest of the app keeps working while it
runs, and it picks up where it left off if it is interrupted. After that it
starts with the app. You need about 3 GB free.

Under every answer it tells you which screen it read and which months, because a
figure you cannot check is a figure you have to take on trust. It can only read:
it cannot add, edit or delete anything.

## Your data

Everything is written to one SQLite file:

```
~/Library/Application Support/Balance/expenses.db
```

Rebuilding or reinstalling the app never touches it. To move Balance to another
Mac, copy that file across. To start again, delete it.

## Build it yourself

You need Python 3.11 and an Apple silicon Mac.

```
git clone https://github.com/JuliusPaulin/balance-app.git
cd balance-app
pip3 install -r requirements.txt
python3 -m PyInstaller Balance.spec --noconfirm --clean
```

The app lands in `dist/Balance.app`. To install what you just built:

```
rm -rf /Applications/Balance.app && ditto dist/Balance.app /Applications/Balance.app
```

Run it from source instead, without packaging:

```
python3 main.py
```

Both read the same database, so your figures carry over either way.

### Tests

```
python3 -m pytest tests/
```

All 456 pass. They run against a throwaway database in a temp folder — your own
figures are never opened.

### Where things live

```
main.py, app.py             the launcher and the wiring
core.py, config.py          the Flask object and the settings
data/                       the database: db, engine, schema
services/                   net worth, recurring, imports, banking
ai/                         the assistant: tools, loop, backends, runtime
routes/                     the API, one module per area
templates/, static/         the interface
tests/                      pytest suite
evals/                      the questions the assistant is judged on
scripts/                    build script and one-off tools
docs/                       plans/, research/, history/, mockups/
Balance.spec                the PyInstaller build recipe
update.sh                   the one-line updater
```

## Built with

Python, Flask and pywebview, packaged with PyInstaller.
