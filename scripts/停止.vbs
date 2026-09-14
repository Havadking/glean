Set objShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptDir & "\stop-vsum.ps1"""
objShell.Run cmd, 0, True
MsgBox "已停止视频总结服务", 64, "视频总结"
