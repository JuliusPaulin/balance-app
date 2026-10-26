# Balance on Windows

The Windows release supports x64 PCs. It keeps the app and its data in separate
locations, so reinstalling Balance or changing between the installer and
portable build leaves the database in place.

## Install

The simplest option is `Balance-Windows-x64-Setup.exe` from the
[latest release](https://github.com/JuliusPaulin/balance-app/releases/latest). Run setup, then open Balance from the
Start menu. Setup checks for Microsoft Edge WebView2 Runtime and the Microsoft
Visual C++ Redistributable (x64), and installs either one if it is missing.

For the portable build, download `Balance-Windows-x64.zip`, choose **Extract
All**, and keep the extracted folder together. Start `Balance.exe` from that
folder. The portable build requires WebView2 and the Visual C++ Redistributable
(x64) to be installed already.

Windows may show a Microsoft Defender SmartScreen warning because the release
is unsigned. Continue only if the file came from the project's GitHub releases.
Choose **More info**, then **Run anyway**.

## Your data

Balance stores its Windows database here:

```text
%LOCALAPPDATA%\Balance\expenses.db
```

The database stays outside the installation and extracted folders. The
installer and portable build use the same file. Balance does not sync it between
computers.

### Move your data from macOS

The macOS database is `~/Library/Application Support/Balance/expenses.db`.
To transfer it to Windows:

1. Quit Balance on both computers.
2. If `%LOCALAPPDATA%\Balance\expenses.db` already exists, copy it somewhere
   safe before replacing it.
3. Copy the macOS `expenses.db` file to `%LOCALAPPDATA%\Balance\expenses.db` on
   Windows. Create the `Balance` folder if needed.
4. Start Balance on Windows and confirm that your figures appear.

Keep a separate copy of the original database until you have confirmed the
transfer. Do not copy the database while Balance is open or edit the database
file by hand.

## Balance AI

The Windows build includes the pinned llama.cpp CPU runtime. The model is
downloaded on first use (about 2.7 GB) and stored beside the database. It stays
on this computer and can answer offline after the download. This runtime does
not use the graphics chip, so answers may take longer than on a GPU. For Windows,
16 GB of RAM and about 5 GB of free disk space are recommended.

## Build from source

Use 64-bit Python 3.11 and PowerShell. Install Inno Setup 6 to build the
installer, then run these commands from the repository folder:

```powershell
.\scripts\build_windows.ps1
```

The script fetches and verifies the pinned Windows runtime, builds the app,
smoke-checks it, and writes the portable ZIP and Setup EXE to `dist/`.
