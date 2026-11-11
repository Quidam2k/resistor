' Hidden launcher for daily_refresh.bat (#5863), used by the Task Scheduler entry.
' Window style 0 = hidden; the bat and its children share that hidden console.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
bat = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "daily_refresh.bat")
sh.Run "cmd.exe /c """ & bat & """", 0, True
