!macro customCheckAppRunning
  DetailPrint "Closing installed Codex Session Transfer processes..."
  nsExec::ExecToLog `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$names = @('Codex Session Transfer.exe', 'codex-session-transfer-server.exe'); Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $$_.ExecutablePath -and ($$names -contains [IO.Path]::GetFileName($$_.ExecutablePath)) } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"`
  Pop $0
  Sleep 1200
!macroend
