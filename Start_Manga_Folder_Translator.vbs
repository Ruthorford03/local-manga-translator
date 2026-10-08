Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strPath = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = strPath
pythonPath = strPath & "\tools\BallonsTranslator\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pythonPath) Then pythonPath = strPath & "\tools\BallonsTranslator\ballontrans_pylibs_win\pythonw.exe"
If Not fso.FileExists(pythonPath) Then
  MsgBox "Python environment is missing. Follow docs\SETUP.zh-TW.md first.", 16, "Local Manga Translator"
  WScript.Quit 1
End If
strCmd = """" & pythonPath & """ -B -X utf8 """ & strPath & "\tools\local-manga-translation\folder_translator.py"""
ws.Run strCmd, 0, False
