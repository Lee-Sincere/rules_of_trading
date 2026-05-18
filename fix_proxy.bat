@echo off
chcp 65001 >nul
echo ========================================
echo   清理系统代理设置
echo ========================================
echo.

echo 当前代理状态:
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer
echo.

echo 正在清理代理设置...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer /f
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f
echo.

echo 清除DNS缓存...
ipconfig /flushdns
echo.

echo 验证清理结果:
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer 2>nul
if errorlevel 1 echo ProxyServer: (已删除)
echo.

echo ========================================
echo 测试网络连接...
echo ========================================
echo.
echo Ping 百度:
ping -n 2 www.baidu.com | findstr "TTL"
echo.
echo DNS解析:
nslookup www.baidu.com | findstr "Address"
echo.

echo 完成！网络应该已经恢复正常。
pause
