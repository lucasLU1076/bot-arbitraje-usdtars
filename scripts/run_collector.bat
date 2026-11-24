@echo off
cd /d "D:\BOOT ARBITRAJE"
D:\Python311\python.exe -u scripts\collect_data.py >> logs\collector.log 2>&1
