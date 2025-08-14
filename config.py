# Telegram Bot 配置
TOKEN = "你的Telegram Bot Token"  # 通过 @BotFather 获取
CHAT_ID = "你的Telegram Chat ID"  # 个人/群组聊天ID (可通过 @userinfobot 获取)

# Binance API 配置 (用于自动交易)
BINANCE_API_KEY = "你的Binance API Key"
BINANCE_API_SECRET = "你的Binance API Secret"

# 交易参数默认值 (可通过机器人菜单修改)
DEFAULT_LEVERAGE = 5              # 默认杠杆倍数 (1-125)
DEFAULT_ORDER_AMOUNT = 50        # 默认每单金额 (USDT)

# 高级配置 (一般无需修改)
REQUEST_TIMEOUT = 30               # API请求超时时间(秒)
MAX_RETRIES = 3                    # API失败重试次数
