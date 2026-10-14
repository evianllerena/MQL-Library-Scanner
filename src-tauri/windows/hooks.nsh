!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Checking for running MQL Indicator Library processes..."
  nsExec::ExecToLog 'taskkill /F /IM "mql-indicator-library.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-engine.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-preview.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-preview-control.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-backtest-batch.exe"'
  Sleep 1200
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  nsExec::ExecToLog 'taskkill /F /IM "mql-indicator-library.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-engine.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-preview.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-preview-control.exe"'
  nsExec::ExecToLog 'taskkill /F /IM "mql-backtest-batch.exe"'
  Sleep 800
!macroend
