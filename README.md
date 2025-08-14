# 通过TG引导设置检测币种，ma9/ma26金叉、死叉自动执行买入卖出
**安装依赖**
```bash
pip install python-telegram-bot==13.7 aiohttp
```
**确保服务器时间同步**
```bash
sudo apt install ntpdate
sudo ntpdate pool.ntp.org
```
**配置文件修改**
TOKEN = "你的Telegram Bot Token"
CHAT_ID = "你的Telegram Chat ID"
BINANCE_API_KEY = "你的Binance API Key"
BINANCE_API_SECRET = "你的Binance API Secret"
