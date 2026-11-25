' Lanza run_bot.bat completamente oculto (sin ventana, sin icono en la barra de tareas).
' Evita que una consola minimizada aparezca y se pueda cerrar sin querer.
Set objShell = CreateObject("WScript.Shell")
objShell.Run "cmd /c ""D:\BOOT ARBITRAJE\scripts\run_bot.bat""", 0, False
