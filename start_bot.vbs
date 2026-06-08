Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c cd /d C:\Users\ryan.li\personal-assistant && python main_ws.py >> bot.log 2>&1", 0, False
