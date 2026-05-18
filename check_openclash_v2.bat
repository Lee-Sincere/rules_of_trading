@echo off
chcp 65001 >nul
echo ========================================
echo   OpenWrt + OpenClash 网络诊断工具
echo ========================================
echo.

set ROUTER_IP=192.168.168.1

:: 生成日志文件名
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
set "YYYY=%dt:~0,4%"
set "MM=%dt:~4,2%"
set "DD=%dt:~6,2%"
set "HH=%dt:~8,2%"
set "MN=%dt:~10,2%"
set "SS=%dt:~12,2%"
set LOG_FILE=openclash_diagnosis_%YYYY%%MM%%DD%_%HH%%MN%%SS%.log

echo 日志文件: %LOG_FILE%
echo.

echo ========================================
echo 诊断时间: %date% %time%
echo ========================================
echo.

echo [1/8] 检查本地网络连接...
echo --- IP配置 ---
ipconfig | findstr "IPv4" | findstr "默认网关" | findstr "DNS"
echo.

echo [2/8] 测试路由器连通性...
ping -n 2 %ROUTER_IP%
echo.

echo [3/8] 检查DNS解析问题...
echo --- DNS解析测试 ---
nslookup www.baidu.com
echo.

echo [4/8] 测试外网连接...
echo --- Ping 百度 ---
ping -n 2 www.baidu.com
echo.
echo --- Ping 114.114.114.114 ---
ping -n 2 114.114.114.114
echo.

echo [5/8] 路由追踪测试...
tracert -d -h 5 www.baidu.com
echo.

echo [6/8] 检查端口连接...
echo --- 路由器SSH端口(22) ---
powershell -Command "Test-NetConnection %ROUTER_IP% -Port 22 -InformationLevel Quiet"
echo --- 路由器HTTP端口(80) ---
powershell -Command "Test-NetConnection %ROUTER_IP% -Port 80 -InformationLevel Quiet"
echo --- OpenClash常见端口 ---
powershell -Command "Test-NetConnection %ROUTER_IP% -Port 7890 -InformationLevel Quiet"
powershell -Command "Test-NetConnection %ROUTER_IP% -Port 7891 -InformationLevel Quiet"
powershell -Command "Test-NetConnection %ROUTER_IP% -Port 7874 -InformationLevel Quiet"
echo.

echo [7/8] 检查代理设置...
echo --- 系统代理 ---
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer
echo.

echo ========================================
echo 诊断完成！
echo ========================================
echo.
echo 快速修复建议:
echo.
echo 1. 临时禁用OpenClash测试:
echo    浏览器访问 http://%ROUTER_IP%
echo    服务 -^> OpenClash -^> 点击"停止"
echo    然后运行: ipconfig /flushdns
echo.
echo 2. 如果禁用后网络恢复，检查OpenClash配置:
echo    - DNS设置: 关闭"DNS劫持"或修改为正确DNS
echo    - 运行模式: 尝试切换为"直连模式"
echo    - Fake-IP: 如启用，改为Redir-Host模式
echo.
echo 3. 重置DNS配置（SSH执行）:
echo    uci set dhcp.@dnsmasq[0].noresolv=0
echo    uci commit dhcp
echo    /etc/init.d/dnsmasq restart
echo.
echo 4. 清除DNS缓存:
echo    ipconfig /flushdns
echo.
echo 完整日志已保存到: %LOG_FILE%
echo.
pause
