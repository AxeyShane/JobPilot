' Launch JobPilot Dashboard as a standalone app - fully hidden, no console flash.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\msys64\home\aksha\projects\JobPilot\scripts\open_dashboard.ps1""", 0, False
