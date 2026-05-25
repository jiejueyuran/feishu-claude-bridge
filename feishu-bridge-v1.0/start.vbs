' Silent launcher for Feishu-Claude Bridge
' Hides the console window on startup
' Requires: Python + requests + config.json

Dim shell, scriptPath
scriptPath = CreateObject("Scripting.FileSystemObject").GetAbsolutePathName(".")

' Launch bridge.py via PowerShell in hidden window (no console flash at all)
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -WindowStyle Hidden -Command ""& { $env:PYTHONIOENCODING=''utf-8''; python '"" & scriptPath & "\bridge.py' }""", 0, False

