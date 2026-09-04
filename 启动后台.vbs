' Douyin streak background starter (hidden, detached from any terminal)
' Launches venv pythonw.exe app.py with window style 0 = no window at all
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
sh.Run """" & base & "\venv\Scripts\pythonw.exe"" app.py", 0, False
