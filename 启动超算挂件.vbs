Option Explicit
Dim files, shell, folder, runtime, launcher
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = files.GetParentFolderName(WScript.ScriptFullName)
runtime = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")
If Not files.FileExists(runtime) Then runtime = "pythonw.exe"
launcher = files.BuildPath(folder, "launch-widget.pyw")
shell.CurrentDirectory = folder
shell.Run Chr(34) & runtime & Chr(34) & " " & Chr(34) & launcher & Chr(34), 1, False
