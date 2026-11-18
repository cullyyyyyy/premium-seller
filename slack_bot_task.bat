@echo off
echo. >> "C:\Users\andre\premium_seller\slack_bot_log.txt"
echo ==== %DATE% %TIME% (bot start) ==== >> "C:\Users\andre\premium_seller\slack_bot_log.txt"
cd /d "C:\Users\andre\premium_seller"
"C:\Users\andre\AppData\Local\Programs\Python\Python312\python.exe" -u "slack_bot.py" >> "C:\Users\andre\premium_seller\slack_bot_log.txt" 2>&1
