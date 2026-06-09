# Launcher for the AI Momentum Trader, intended for an unattended ~03:55 ET
# start via Windows Task Scheduler. Captures ALL output to a dated log file so
# the overnight / pre-market run can be reviewed when you reach the desk.
#
# Register the scheduled task (weekdays 03:55), once, from an elevated PowerShell:
#   schtasks /Create /TN "AI Momentum Trader" `
#     /TR "powershell.exe -ExecutionPolicy Bypass -File D:\dev\claude\ai_trader\run_trader.ps1" `
#     /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 03:55 /F
#
# Run it by hand the same way the task does (good for testing):
#   powershell -ExecutionPolicy Bypass -File D:\dev\claude\ai_trader\run_trader.ps1
#
# Stop the bot: Ctrl-C if interactive, else end the python process / Task Scheduler task.

Set-Location 'D:\dev\claude\ai_trader'
New-Item -ItemType Directory -Force -Path logs | Out-Null
$log = "logs\trader_$(Get-Date -Format yyyy-MM-dd).log"

# Make Python emit UTF-8 (the console blocks use ━ / 🚨 etc.).
$env:PYTHONUTF8 = "1"

"===== START $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $log

# Redirect via cmd.exe, NOT PowerShell's `*>>`: cmd appends the process's raw
# UTF-8 bytes, so the log stays clean UTF-8. PowerShell's redirect would re-encode
# to UTF-16 and garble it. --dry-run = live data, NO orders; change to --allow-live
# only when you have deliberately decided to place real orders.
cmd /c "py run_trader.py --allow-live >> ""$log"" 2>&1"

"===== EXIT $LASTEXITCODE  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $log
