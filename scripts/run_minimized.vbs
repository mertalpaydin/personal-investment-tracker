If WScript.Arguments.Count = 0 Then WScript.Quit 1
Dim cmd, i
cmd = "cmd.exe /c " & Chr(34) & Chr(34) & WScript.Arguments(0) & Chr(34)
For i = 1 To WScript.Arguments.Count - 1
    cmd = cmd & " " & Chr(34) & WScript.Arguments(i) & Chr(34)
Next
cmd = cmd & Chr(34)

Set WshShell = CreateObject("WScript.Shell")
WshShell.Run cmd, 7, False
