# Nightly retrospective launcher — intended for ~21:00 ET via Task Scheduler.
# Scores the day's AI decisions, writes a report under reports/, and proposes
# lessons into playbook_pending.md (review-gated; approve with --approve).
#
# Register the scheduled task (weekdays 21:00), once, from an elevated PowerShell:
#   schtasks /Create /TN "AI Trader Retrospective" `
#     /TR "powershell.exe -ExecutionPolicy Bypass -File D:\dev\claude\ai_trader\retrospective.ps1" `
#     /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 21:00 /F
#
# Run by hand the same way:
#   powershell -ExecutionPolicy Bypass -File D:\dev\claude\ai_trader\retrospective.ps1
#
# After reviewing playbook_pending.md, approve lessons into the live playbook:
#   py retrospective.py --approve

Set-Location 'D:\dev\claude\ai_trader'
New-Item -ItemType Directory -Force -Path logs | Out-Null
$log = "logs\retro_$(Get-Date -Format yyyy-MM-dd).log"

$env:PYTHONUTF8 = "1"

"===== RETRO START $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $log
# cmd redirect keeps the log clean UTF-8 (see run_trader.ps1 for why).
cmd /c "py retrospective.py >> ""$log"" 2>&1"
"===== RETRO EXIT $LASTEXITCODE  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Out-File -Append -Encoding utf8 $log
