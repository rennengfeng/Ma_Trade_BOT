#file: ma_bot.py

import asyncio
import json
import os
import aiohttp
import hmac
import hashlib
import urllib.parse
import time
from datetime import datetime
from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    Application
)
from config import TOKEN, CHAT_ID, BINANCE_API_KEY, BINANCE_API_SECRET

# 文件路径
DATA_FILE = "symbols.json"
TRADE_SETTINGS_FILE = "trade_settings.json"
EXISTING_POSITIONS_FILE = "existing_positions.json"

# K线参数
INTERVAL = "15m"

# 主菜单
main_menu = [
    ["1. 添加币种", "2. 删除币种"],
    ["3. 开启监控", "4. 停止监控"],
    ["5. 开启自动交易", "6. 关闭自动交易"],
    ["7. 查看状态", "8. 帮助"]
]
reply_markup_main = ReplyKeyboardMarkup(main_menu, resize_keyboard=True)

# --- 时间同步模块 ---
class TimeSync:
    _instance = None
    _time_diff = 0
    _last_sync = 0
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    async def sync_time(self):
        try:
            # 使用合约API进行时间同步
            url = "https://fapi.binance.com/fapi/v1/time"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        server_time = data["serverTime"]
                        local_time = int(time.time() * 1000)
                        self._time_diff = server_time - local_time
                        self._last_sync = time.time()
                        print(f"时间同步成功，时间差: {self._time_diff}ms")
                    else:
                        error = await resp.text()
                        print(f"时间同步失败 ({resp.status}): {error}")
        except Exception as e:
            print(f"时间同步异常: {e}")
    
    def get_corrected_time(self):
        # 如果超过10分钟未同步，则重新同步
        if time.time() - self._last_sync > 600:
            asyncio.create_task(self.sync_time())
        return int(time.time() * 1000) + self._time_diff

time_sync = TimeSync()

# --- 初始化 ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return {"symbols": [], "monitor": False}
    return {"symbols": [], "monitor": False}

def load_trade_settings():
    default_settings = {
        "auto_trade": False, 
        "setting_mode": "global",  # global or individual
        "global_leverage": 10,
        "global_order_amount": 100,
        "individual_settings": {},
        "take_profit": 0,
        "stop_loss": 0
    }
    if os.path.exists(TRADE_SETTINGS_FILE):
        with open(TRADE_SETTINGS_FILE, "r") as f:
            try:
                loaded = json.load(f)
                for key in default_settings:
                    if key not in loaded:
                        loaded[key] = default_settings[key]
                return loaded
            except:
                return default_settings
    return default_settings

def load_existing_positions():
    if os.path.exists(EXISTING_POSITIONS_FILE):
        with open(EXISTING_POSITIONS_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_existing_positions(positions):
    with open(EXISTING_POSITIONS_FILE, "w") as f:
        json.dump(positions, f)

def save_trade_settings(settings):
    with open(TRADE_SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

data = load_data()
trade_settings = load_trade_settings()
existing_positions = load_existing_positions()
monitoring_task = None
user_states = {}
prev_klines = {}
positions = {}
oco_orders = {}

# --- Binance API 增强版 ---
async def binance_request(method, endpoint, params=None, signed=False, retry=3):
    url = f"https://fapi.binance.com{endpoint}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY} if signed else {}
    
    for attempt in range(retry):
        try:
            if signed:
                params = params or {}
                params["timestamp"] = time_sync.get_corrected_time()
                params["recvWindow"] = 5000
                params["signature"] = generate_signature(params)
            
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error = await resp.text()
                        print(f"Binance API 错误 ({resp.status}): {error}")
                        if "timestamp" in error.lower() and attempt < retry - 1:
                            await time_sync.sync_time()  # 时间不同步时立即重试
                            continue
                        return None
        except Exception as e:
            print(f"请求异常: {e}")
            if attempt == retry - 1:
                return None
            await asyncio.sleep(1)
    
    return None

def generate_signature(params):
    query = urllib.parse.urlencode(params)
    return hmac.new(
        BINANCE_API_SECRET.encode('utf-8'),
        query.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

# --- 获取K线数据函数 ---
async def get_klines(symbol, market_type, interval=INTERVAL, limit=100):
    """获取K线数据"""
    if market_type == "contract":
        endpoint = "/fapi/v1/klines"
    else:
        # 现货使用不同端点
        endpoint = "/api/v3/klines"
    
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    return await binance_request("GET", endpoint, params)

# --- 获取持仓信息函数 ---
async def get_position(symbol):
    """获取指定币种的持仓信息"""
    positions_data = await binance_request("GET", "/fapi/v2/positionRisk", None, True)
    if positions_data:
        for pos in positions_data:
            if pos["symbol"] == symbol and float(pos["positionAmt"]) != 0:
                return {
                    "symbol": symbol,
                    "side": "LONG" if float(pos["positionAmt"]) > 0 else "SHORT",
                    "qty": abs(float(pos["positionAmt"])),
                    "entry_price": float(pos["entryPrice"]),
                    "leverage": int(pos["leverage"]),
                    "unrealized_profit": float(pos["unRealizedProfit"]),
                    "mark_price": float(pos["markPrice"])
                }
    return None

# --- 交易功能 ---
async def set_leverage(symbol, leverage):
    return await binance_request("POST", "/fapi/v1/leverage", 
                               {"symbol": symbol, "leverage": leverage}, True)

async def place_market_order(symbol, side, quantity):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": round(quantity, 3)
    }
    return await binance_request("POST", "/fapi/v1/order", params, True)

async def place_oco_order(symbol, side, quantity, entry_price, take_profit, stop_loss):
    if take_profit <= 0 and stop_loss <= 0:
        return None
    
    if side == "BUY":
        take_profit_price = entry_price * (1 + take_profit / 100)
        stop_loss_price = entry_price * (1 - stop_loss / 100)
        oco_side = "SELL"
    else:
        take_profit_price = entry_price * (1 - take_profit / 100)
        stop_loss_price = entry_price * (1 + stop_loss / 100)
        oco_side = "BUY"
    
    params = {
        "symbol": symbol,
        "side": oco_side,
        "quantity": round(quantity, 3),
        "price": round(take_profit_price, 4),
        "stopPrice": round(stop_loss_price, 4),
        "stopLimitPrice": round(stop_loss_price, 4),
        "stopLimitTimeInForce": "GTC"
    }
    return await binance_request("POST", "/fapi/v1/order/oco", params, True)

# --- MA计算 ---
def calculate_ma(klines):
    """计算MA9和MA26指标"""
    if not klines or len(klines) < 26:
        return 0, 0, 0
        
    closes = [float(k[4]) for k in klines]
    ma9 = sum(closes[-9:]) / 9
    ma26 = sum(closes[-26:]) / 26
    return ma9, ma26, closes[-1]

# --- 监控任务 ---
async def monitor_task(app):
    print("监控任务启动")
    await time_sync.sync_time()
    prev_states = {}
    
    try:
        while data["monitor"]:
            print(f"监控循环开始 - 监控币种数量: {len(data['symbols'])}")
            for item in data["symbols"]:
                symbol = item["symbol"]
                symbol_key = f"{symbol}_{item['type']}"
                try:
                    klines = await get_klines(symbol, item["type"])
                    if not klines or len(klines) < 26:
                        print(f"获取K线失败或数据不足: {symbol}")
                        continue
                    
                    if symbol_key in prev_klines and klines[-1][0] == prev_klines[symbol_key][-1][0]:
                        continue
                    
                    prev_klines[symbol_key] = klines
                    ma9, ma26, price = calculate_ma(klines)
                    
                    if symbol_key in prev_states:
                        prev_ma9, prev_ma26 = prev_states[symbol_key]
                        
                        # 信号检测
                        if prev_ma9 <= prev_ma26 and ma9 > ma26:
                            signal_msg = f"📈 检测到买入信号 {symbol}\n价格: {price:.4f}"
                            print(signal_msg)
                            for uid in user_states.keys():
                                await app.bot.send_message(chat_id=uid, text=signal_msg)
                            
                            if item["type"] == "contract" and trade_settings["auto_trade"]:
                                if await execute_trade(app, symbol, "BUY"):
                                    pass
                        
                        elif prev_ma9 >= prev_ma26 and ma9 < ma26:
                            signal_msg = f"📉 检测到卖出信号 {symbol}\n价格: {price:.4f}"
                            print(signal_msg)
                            for uid in user_states.keys():
                                await app.bot.send_message(chat_id=uid, text=signal_msg)
                            
                            if item["type"] == "contract" and trade_settings["auto_trade"]:
                                if await execute_trade(app, symbol, "SELL"):
                                    pass
                    
                    prev_states[symbol_key] = (ma9, ma26)
                    
                    # 止盈止损检查（包括已有持仓）
                    if item["type"] == "contract":
                        # 检查本系统新开的持仓
                        if symbol in positions:
                            pos = positions[symbol]
                            await check_tp_sl(app, symbol, pos, price)
                        
                        # 检查已有持仓（开启自动交易时保留的）
                        if symbol in existing_positions and existing_positions[symbol]["active"]:
                            pos = existing_positions[symbol]
                            await check_tp_sl(app, symbol, pos, price)
                
                except Exception as e:
                    print(f"监控 {symbol} 出错: {e}")
            
            print("监控循环完成，等待60秒...")
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        print("监控任务被取消")
    except Exception as e:
        print(f"监控任务异常: {e}")

# 检查止盈止损通用函数
async def check_tp_sl(app, symbol, pos, price):
    """检查止盈止损并执行平仓"""
    entry_price = pos["entry_price"]
    
    if pos["side"] == "LONG":
        take_profit_price = entry_price * (1 + trade_settings["take_profit"] / 100)
        stop_loss_price = entry_price * (1 - trade_settings["stop_loss"] / 100)
    else:
        take_profit_price = entry_price * (1 - trade_settings["take_profit"] / 100)
        stop_loss_price = entry_price * (1 + trade_settings["stop_loss"] / 100)
    
    if trade_settings["take_profit"] > 0:
        if (pos["side"] == "LONG" and price >= take_profit_price) or \
           (pos["side"] == "SHORT" and price <= take_profit_price):
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"📈 检测到{symbol}止盈触发")
            await close_position(app, symbol, "take_profit", price, is_existing=(symbol in existing_positions))
    
    if trade_settings["stop_loss"] > 0:
        if (pos["side"] == "LONG" and price <= stop_loss_price) or \
           (pos["side"] == "SHORT" and price >= stop_loss_price):
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"📉 检测到{symbol}止损触发")
            await close_position(app, symbol, "stop_loss", price, is_existing=(symbol in existing_positions))

# --- execute_trade 函数（已修复名义价值问题）---
async def execute_trade(app, symbol, signal_type):
    if not trade_settings["auto_trade"]:
        return False
    
    try:
        # 获取设置
        leverage = trade_settings["global_leverage"]
        notional_value = trade_settings["global_order_amount"]  # 用户设置的名义价值
        
        # 检查是否有币种特定设置
        if trade_settings["setting_mode"] == "individual":
            symbol_settings = trade_settings["individual_settings"].get(symbol)
            if symbol_settings:
                leverage = symbol_settings.get("leverage", leverage)
                notional_value = symbol_settings.get("order_amount", notional_value)
        
        # 确保名义价值至少为20 USDT
        if notional_value < 20:
            notional_value = 20
        
        # 设置杠杆
        leverage_resp = await set_leverage(symbol, leverage)
        if leverage_resp is None:
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"❌ {symbol} 设置杠杆失败")
            return False
        
        # 获取K线数据
        klines = await get_klines(symbol, "contract")
        if not klines:
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"❌ {symbol} 获取K线失败")
            return False
            
        price = float(klines[-1][4])
        
        # 计算保证金
        margin = notional_value / leverage
        
        # 计算数量
        quantity = notional_value / price
        quantity = round(quantity, 3)  # 四舍五入到3位小数
        
        # 处理反向持仓
        if signal_type == "BUY":
            pos = await get_position(symbol)
            if pos and pos["side"] == "SHORT":
                for uid in user_states.keys():
                    await app.bot.send_message(chat_id=uid, text=f"⚠️ 检测到买入信号，正在平空仓 {symbol}")
                await close_position(app, symbol)
            
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"🚀 正在开多仓 {symbol}...")
                
            order = await place_market_order(symbol, "BUY", quantity)
            
        elif signal_type == "SELL":
            pos = await get_position(symbol)
            if pos and pos["side"] == "LONG":
                for uid in user_states.keys():
                    await app.bot.send_message(chat_id=uid, text=f"⚠️ 检测到卖出信号，正在平多仓 {symbol}")
                await close_position(app, symbol)
            
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"🚀 正在开空仓 {symbol}...")
                
            order = await place_market_order(symbol, "SELL", quantity)
        
        # 处理订单结果
        if order:
            entry_price = float(order.get('price', price))
            positions[symbol] = {
                "side": "LONG" if signal_type == "BUY" else "SHORT",
                "qty": quantity,
                "entry_price": entry_price,
                "system_order": True  # 标记为本系统订单
            }
            
            # 发送通知
            pos_type = "多仓" if signal_type == "BUY" else "空仓"
            msg = f"✅ 开仓成功 {symbol} {pos_type}\n" \
                  f"价格: {entry_price:.4f}\n" \
                  f"数量: {quantity:.4f}\n" \
                  f"杠杆: {leverage}x\n" \
                  f"名义价值: {notional_value:.2f} USDT\n" \
                  f"保证金: {margin:.2f} USDT"
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=msg)
            
            # 设置止盈止损
            if trade_settings["take_profit"] > 0 or trade_settings["stop_loss"] > 0:
                oco_order = await place_oco_order(
                    symbol, 
                    signal_type, 
                    quantity, 
                    entry_price, 
                    trade_settings["take_profit"], 
                    trade_settings["stop_loss"]
                )
                if oco_order:
                    oco_orders[symbol] = oco_order
            return True
        else:
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"❌ {symbol} 下单失败")
            return False
            
    except Exception as e:
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=f"❌ {symbol} 交易出错: {str(e)}")
        return False

# --- close_position 函数 ---
async def close_position(app, symbol, close_type="signal", close_price=None, is_existing=False):
    try:
        pos = await get_position(symbol)
        if not pos:
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"⚠️ {symbol} 无持仓可平")
            return False
        
        # 发送平仓通知
        pos_type = "多仓" if pos["side"] == "LONG" else "空仓"
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=f"🛑 正在平{symbol}{pos_type}...")
        
        # 执行平仓
        side = "SELL" if pos["side"] == "LONG" else "BUY"
        result = await place_market_order(symbol, side, pos["qty"])
        
        if not result:
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"❌ {symbol} 平仓失败")
            return False
        
        # 计算盈亏
        if close_price is None:
            klines = await get_klines(symbol, "contract")
            close_price = float(klines[-1][4]) if klines else pos["entry_price"]
        
        entry = pos["entry_price"]
        profit = (close_price - entry) * pos["qty"] if pos["side"] == "LONG" else (entry - close_price) * pos["qty"]
        profit_percent = abs(profit / (entry * pos["qty"])) * 100
        profit_sign = "+" if profit > 0 else ""
        
        # 发送平仓结果
        close_reason = {
            "take_profit": "止盈",
            "stop_loss": "止损",
            "signal": "信号"
        }.get(close_type, "手动")
        
        msg = f"📌 {symbol} 平仓完成 ({close_reason})\n" \
              f"类型: {pos_type}\n" \
              f"数量: {pos['qty']:.4f}\n" \
              f"开仓价: {entry:.4f}\n" \
              f"平仓价: {close_price:.4f}\n" \
              f"盈亏: {profit_sign}{profit:.2f} ({profit_sign}{profit_percent:.2f}%)"
        
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=msg)
        
        # 清理记录
        if symbol in positions:
            del positions[symbol]
        if symbol in oco_orders:
            del oco_orders[symbol]
        if is_existing and symbol in existing_positions:
            existing_positions[symbol]["active"] = False
            save_existing_positions(existing_positions)
            
        return True
        
    except Exception as e:
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=f"❌ {symbol} 平仓出错: {str(e)}")
        return False

# --- 计算持仓盈亏 ---
async def calculate_position_profit(symbol, entry_price, side, qty):
    try:
        klines = await get_klines(symbol, "contract")
        if not klines:
            return 0, 0
        
        current_price = float(klines[-1][4])
        if side == "LONG":
            profit = (current_price - entry_price) * qty
            profit_percent = (current_price - entry_price) / entry_price * 100
        else:
            profit = (entry_price - current_price) * qty
            profit_percent = (entry_price - current_price) / entry_price * 100
        
        return profit, profit_percent
    except:
        return 0, 0

# --- 检测非系统订单 ---
async def check_existing_positions(app, user_id):
    """检测非系统订单并返回处理结果"""
    positions_data = await binance_request("GET", "/fapi/v2/positionRisk", None, True)
    existing_pos = {}
    if positions_data:
        for pos in positions_data:
            position_amt = float(pos["positionAmt"])
            if position_amt != 0:
                symbol = pos["symbol"]
                existing_pos[symbol] = {
                    "side": "LONG" if position_amt > 0 else "SHORT",
                    "qty": abs(position_amt),
                    "entry_price": float(pos["entryPrice"]),
                    "active": False,
                    "system_order": False
                }
    
    if not existing_pos:
        return None
    
    # 保存已有持仓信息
    existing_positions.clear()
    existing_positions.update(existing_pos)
    save_existing_positions(existing_positions)
    
    # 显示持仓详情并引导用户选择
    msg = "⚠️ 检测到非本系统持仓:\n"
    for symbol, pos in existing_positions.items():
        # 计算盈亏
        profit, profit_percent = await calculate_position_profit(
            symbol, 
            pos["entry_price"], 
            pos["side"], 
            pos["qty"]
        )
        profit_sign = "+" if profit > 0 else ""
        pos_type = "多仓" if pos["side"] == "LONG" else "空仓"
        msg += (f"\n📊 {symbol} {pos_type}\n"
                f"数量: {pos['qty']:.4f}\n"
                f"开仓价: {pos['entry_price']:.4f}\n"
                f"盈亏: {profit_sign}{profit:.2f} ({profit_sign}{profit_percent:.2f}%)\n")
    
    msg += "\n是否将这些持仓纳入本系统管理？"
    
    keyboard = [
        [
            InlineKeyboardButton("是，全部纳入", callback_data="integrate_existing:all"),
            InlineKeyboardButton("否，保留原状", callback_data="integrate_existing:none")
        ],
        [InlineKeyboardButton("选择部分纳入", callback_data="integrate_existing:select")]
    ]
    
    return {"message": msg, "keyboard": keyboard}

# --- 显示自动交易设置 ---
async def show_auto_trade_settings(app, user_id):
    setting_info = "✅ 自动交易已开启！\n"
    
    if trade_settings["setting_mode"] == "global":
        setting_info += f"全局设置:\n" \
                       f"杠杆: {trade_settings['global_leverage']}x\n" \
                       f"下单金额: {trade_settings['global_order_amount']} USDT\n"
    else:
        setting_info += "逐一设置:\n"
        for symbol, settings in trade_settings["individual_settings"].items():
            leverage = settings.get("leverage", trade_settings["global_leverage"])
            order_amount = settings.get("order_amount", trade_settings["global_order_amount"])
            setting_info += f"{symbol} 杠杆: {leverage}x  下单金额: {order_amount}USDT\n"
    
    setting_info += f"止盈: {trade_settings['take_profit']}%\n" \
                   f"止损: {trade_settings['stop_loss']}%"
    
    # 如果有纳入的非本系统持仓，也显示出来
    if existing_positions:
        setting_info += "\n\n已纳入的非本系统持仓：\n"
        for symbol, pos in existing_positions.items():
            if pos.get("system_order", False) and pos.get("active", False):
                pos_type = "多仓" if pos["side"] == "LONG" else "空仓"
                setting_info += f"{symbol} {pos_type} 数量: {pos['qty']:.4f}\n"
    
    await app.bot.send_message(user_id, setting_info)
    await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
    
    # 确保清除用户状态
    if user_id in user_states:
        del user_states[user_id]

# --- handle_auto_trade 函数（优化流程）---
async def handle_auto_trade(update, context, enable):
    app = context.application
    user_id = update.effective_chat.id
    
    if enable:
        # 使用简单的API调用验证连接
        ping_response = await binance_request("GET", "/fapi/v1/ping")
        if ping_response is None:
            await update.message.reply_text(
                "❌ 无法连接币安API，请检查API密钥和网络连接",
                reply_markup=reply_markup_main
            )
            return
        
        # 直接显示设置选项，不再询问
        keyboard = [
            [
                InlineKeyboardButton("全局设置", callback_data="auto_trade_setting:global"),
                InlineKeyboardButton("逐一设置", callback_data="auto_trade_setting:individual")
            ]
        ]
        await update.message.reply_text(
            "✅ API验证成功",
            reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        trade_settings["auto_trade"] = False
        save_trade_settings(trade_settings)
        
        # 获取详细的持仓信息
        positions_data = await binance_request("GET", "/fapi/v2/positionRisk", None, True)
        msg = "自动交易已关闭\n"
        
        if positions_data:
            for pos in positions_data:
                position_amt = float(pos["positionAmt"])
                if position_amt != 0:
                    symbol = pos["symbol"]
                    pos_type = "多仓" if position_amt > 0 else "空仓"
                    entry_price = float(pos["entryPrice"])
                    mark_price = float(pos["markPrice"])
                    leverage = pos["leverage"]
                    unrealized_profit = float(pos["unRealizedProfit"])
                    
                    msg += (f"\n持仓: {symbol} {pos_type} x{leverage}\n"
                            f"数量: {abs(position_amt)}\n"
                            f"开仓价: {entry_price:.4f}\n"
                            f"当前标记价: {mark_price:.4f}\n"
                            f"未实现盈亏: {unrealized_profit:.4f} USDT\n")
        
        if "持仓:" in msg:
            keyboard = [
                [InlineKeyboardButton("清仓所有持仓", callback_data="close_all:yes")],
                [InlineKeyboardButton("保留持仓", callback_data="close_all:no")]
            ]
            await update.message.reply_text(
                msg + "\n是否清空所有持仓?",
                reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(msg + "无持仓", reply_markup=reply_markup_main)

# --- 显示持仓选择界面 ---
async def show_position_selection(query, context, positions):
    user_id = query.from_user.id
    user_states[user_id] = {"step": "select_positions", "positions": positions}
    
    msg = "请选择要纳入本系统管理的持仓:\n"
    keyboard = []
    
    for idx, (symbol, pos) in enumerate(positions.items(), 1):
        pos_type = "多仓" if pos["side"] == "LONG" else "空仓"
        msg += f"{idx}. {symbol} {pos_type} 数量: {pos['qty']:.4f}\n"
        keyboard.append([InlineKeyboardButton(f"{idx}. {symbol}", callback_data=f"select_position:{symbol}")])
    
    keyboard.append([InlineKeyboardButton("确认选择", callback_data="confirm_selection")])
    keyboard.append([InlineKeyboardButton("取消", callback_data="cancel_selection")])
    
    await query.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard))
    await query.answer()

# --- button_callback 函数（优化流程）---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data_parts = query.data.split(":")
    app = context.application

    # 自动交易设置方式选择
    if data_parts[0] == "auto_trade_setting":
        trade_settings["setting_mode"] = data_parts[1]
        save_trade_settings(trade_settings)
        
        if data_parts[1] == "global":
            user_states[user_id] = {"step": "set_global_leverage"}
            await query.edit_message_text("已选择全局设置")
            await query.message.reply_text(
                "请输入全局杠杆倍数 (1-125):",
                reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
        elif data_parts[1] == "individual":
            user_states[user_id] = {
                "step": "set_individual_leverage",
                "symbols": [s["symbol"] for s in data["symbols"]],
                "current_index": 0,
                "settings": {}
            }
            symbol = user_states[user_id]["symbols"][0]
            await query.edit_message_text("已选择逐一设置")
            await query.message.reply_text(
                f"请设置 {symbol} 的杠杆倍数 (1-125):",
                reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))

    # 非系统订单处理
    elif data_parts[0] == "integrate_existing":
        if data_parts[1] == "all":
            # 直接使用模块级变量
            for symbol, pos in existing_positions.items():
                existing_positions[symbol]["system_order"] = True
                existing_positions[symbol]["active"] = True
                positions[symbol] = {
                    "side": pos["side"],
                    "qty": pos["qty"],
                    "entry_price": pos["entry_price"],
                    "system_order": True
                }
                       
            save_existing_positions(existing_positions)
            await query.edit_message_text("所有非本系统持仓已纳入本系统管理")
            
            # 开启自动交易并显示设置
            trade_settings["auto_trade"] = True
            save_trade_settings(trade_settings)
            await show_auto_trade_settings(app, user_id)
        
        elif data_parts[1] == "none":
            # 用户选择不纳入本系统
            await query.edit_message_text("非本系统持仓将保持原状，不会自动设置止盈止损")
            
            # 开启自动交易并显示设置
            trade_settings["auto_trade"] = True
            save_trade_settings(trade_settings)
            await show_auto_trade_settings(app, user_id)
        
        elif data_parts[1] == "select":
            # 用户选择部分纳入
            await show_position_selection(query, context, existing_positions)

    # 选择具体持仓
    elif data_parts[0] == "select_position":
        symbol = data_parts[1]
        state = user_states.get(user_id, {})
        if "selected_positions" not in state:
            state["selected_positions"] = {}
        
        # 切换选择状态
        if symbol in state["selected_positions"]:
            del state["selected_positions"][symbol]
        else:
            state["selected_positions"][symbol] = existing_positions[symbol]
        
        user_states[user_id] = state
        
        # 更新消息显示选择状态
        msg = "已选择持仓:\n"
        for sym in state["selected_positions"]:
            pos = existing_positions[sym]
            pos_type = "多仓" if pos["side"] == "LONG" else "空仓"
            msg += f"- {sym} {pos_type} 数量: {pos['qty']:.4f}\n"
        
        await query.edit_message_text(
            text=msg,
            reply_markup=query.message.reply_markup
        )
        await query.answer()
    
    # 确认选择
    elif data_parts[0] == "confirm_selection":
        state = user_states.get(user_id, {})
        if "selected_positions" in state:
            for symbol, pos in state["selected_positions"].items():
                # 标记为本系统订单
                existing_positions[symbol]["system_order"] = True
                existing_positions[symbol]["active"] = True
                
                # 添加到positions字典
                positions[symbol] = {
                    "side": pos["side"],
                    "qty": pos["qty"],
                    "entry_price": pos["entry_price"],
                    "system_order": True
                }
            
            save_existing_positions(existing_positions)
            await query.edit_message_text("已选择的持仓已纳入本系统管理")
            
            # 开启自动交易并显示设置
            trade_settings["auto_trade"] = True
            save_trade_settings(trade_settings)
            await show_auto_trade_settings(app, user_id)
    
    # 取消选择
    elif data_parts[0] == "cancel_selection":
        if user_id in user_states:
            del user_states[user_id]
        await query.edit_message_text("持仓选择已取消")
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)

    # 选择币种类型
    elif data_parts[0] == "select_type":
        symbol = data_parts[1]
        market_type = data_parts[2]
        data["symbols"].append({"symbol": symbol, "type": market_type})
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
        await query.edit_message_text(f"已添加 {symbol} ({market_type})")
        
        keyboard = [
            [InlineKeyboardButton("继续添加", callback_data="continue_add:yes")],
            [InlineKeyboardButton("返回菜单", callback_data="continue_add:no")]
        ]
        await query.message.reply_text(
            "是否继续添加币种？",
            reply_markup=InlineKeyboardMarkup(keyboard))
        await query.answer()
    
    # 继续添加币种
    elif data_parts[0] == "continue_add":
        if data_parts[1] == "yes":
            user_states[user_id] = {"step": "add_symbol"}
            await query.message.reply_text("请输入币种（如 BTCUSDT）：输入'取消'可中断")
        else:
            user_states[user_id] = {}
            keyboard = [
                [InlineKeyboardButton("立即开启监控", callback_data="start_monitor:yes")],
                [InlineKeyboardButton("稍后手动开启", callback_data="start_monitor:no")]
            ]
            await query.message.reply_text(
                "是否立即开启监控？",
                reply_markup=InlineKeyboardMarkup(keyboard))
        await query.answer()
    
    # 开启监控
    elif data_parts[0] == "start_monitor":
        if data_parts[1] == "yes":
            data["monitor"] = True
            with open(DATA_FILE, "w") as f:
                json.dump(data, f)
            global monitoring_task
            if not monitoring_task or monitoring_task.done():
                monitoring_task = asyncio.create_task(monitor_task(context.application))
            
            msg = "监控已开启\n当前监控列表：\n"
            for s in data["symbols"]:
                try:
                    klines = await get_klines(s["symbol"], s["type"])
                    if klines:
                        _, _, price = calculate_ma(klines)
                        msg += f"{s['symbol']} ({s['type']}): {price:.4f}\n"
                except Exception as e:
                    print(f"获取价格失败: {e}")
                    msg += f"{s['symbol']} ({s['type']}): 获取价格失败\n"
            
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text("您可以在菜单中手动开启监控")
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
        await query.answer()
    
    # 确认交易设置
    elif data_parts[0] == "confirm_trade":
        if data_parts[1] == "yes":
            # 先检查非系统订单
            existing_positions_info = await check_existing_positions(app, user_id)
            
            if existing_positions_info:
                await query.edit_message_text("检测到非本系统持仓，正在处理...")
                await query.message.reply_text(
                    existing_positions_info["message"],
                    reply_markup=InlineKeyboardMarkup(existing_positions_info["keyboard"]))
            else:
                # 没有非系统订单，直接开启自动交易
                trade_settings["auto_trade"] = True
                save_trade_settings(trade_settings)
                await show_auto_trade_settings(app, user_id)
        else:
            user_states[user_id] = {}
            trade_settings["auto_trade"] = False
            save_trade_settings(trade_settings)
            await query.edit_message_text("自动交易设置已取消")
            await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
        await query.answer()

    # 清空所有持仓
    elif data_parts[0] == "close_all":
        if data_parts[1] == "yes":
            try:
                positions_data = await binance_request("GET", "/fapi/v2/positionRisk", None, True)
                if positions_data:
                    for pos in positions_data:
                        if float(pos["positionAmt"]) != 0:
                            symbol = pos["symbol"]
                            side = "SELL" if float(pos["positionAmt"]) > 0 else "BUY"
                            await place_market_order(symbol, side, abs(float(pos["positionAmt"])))
                
                await query.edit_message_text("所有持仓已清空")
            except Exception as e:
                await query.edit_message_text(f"清仓失败: {str(e)}")
        else:
            await query.edit_message_text("已保留持仓")
        
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)

# --- 异步任务：关闭所有持仓 ---
async def close_all_positions(query, context):
    user_id = query.from_user.id
    app = context.application
    
    try:
        positions_data = await binance_request("GET", "/fapi/v2/positionRisk", None, True)
        if positions_data:
            for pos in positions_data:
                if float(pos["positionAmt"]) != 0:
                    symbol = pos["symbol"]
                    side = "SELL" if float(pos["positionAmt"]) > 0 else "BUY"
                    await place_market_order(symbol, side, abs(float(pos["positionAmt"])))
        
        await query.edit_message_text("所有持仓已清空")
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
    except Exception as e:
        await query.edit_message_text(f"清仓失败: {str(e)}")
        await app.bot.send_message(user_id, "请重试或手动操作", reply_markup=reply_markup_main)
 
# --- show_status 函数（优化显示格式）---
async def show_status(update):
    msg = f"监控状态: {'开启' if data['monitor'] else '关闭'}\n"
    msg += f"自动交易: {'开启' if trade_settings['auto_trade'] else '关闭'}\n"
    
    if trade_settings["auto_trade"]:
        msg += f"设置方式: {'全局设置' if trade_settings['setting_mode'] == 'global' else '逐一设置'}\n"
        msg += f"止盈: {trade_settings['take_profit']}%\n"
        msg += f"止损: {trade_settings['stop_loss']}%\n"
        
        if trade_settings["setting_mode"] == "global":
            msg += f"全局杠杆: {trade_settings['global_leverage']}x\n"
            msg += f"全局每单金额: {trade_settings['global_order_amount']} USDT\n"
        else:
            msg += "币种特定设置:\n"
            for symbol, settings in trade_settings["individual_settings"].items():
                leverage = settings.get("leverage", trade_settings["global_leverage"])
                order_amount = settings.get("order_amount", trade_settings["global_order_amount"])
                msg += f"- {symbol}: {leverage}x, {order_amount} USDT\n"
    
    if data["symbols"]:
        msg += "\n监控列表:\n"
        for s in data["symbols"]:
            try:
                klines = await get_klines(s["symbol"], s["type"])
                if klines:
                    _, _, price = calculate_ma(klines)
                    msg += f"{s['symbol']} ({s['type']}): {price:.4f}\n"
            except:
                msg += f"{s['symbol']} ({s['type']}): 获取失败\n"
    
    # 本系统持仓（无论自动交易是否开启都显示）
    if positions:
        msg += "\n📊 本系统持仓:\n"
        for symbol, pos in positions.items():
            pos_type = "多仓" if pos["side"] == "LONG" else "空仓"
            # 计算盈亏
            profit, profit_percent = await calculate_position_profit(
                symbol, 
                pos["entry_price"], 
                pos["side"], 
                pos["qty"]
            )
            profit_sign = "+" if profit > 0 else ""
            
            msg += (f"{symbol} {pos_type}\n"
                    f"数量: {pos['qty']:.4f}\n"
                    f"开仓价: {pos['entry_price']:.4f}\n"
                    f"盈亏: {profit_sign}{profit:.2f} ({profit_sign}{profit_percent:.2f}%)\n")
    
    # 非本系统持仓（无论自动交易是否开启都显示）
    non_system_positions = False
    if existing_positions:
        for symbol, pos in existing_positions.items():
            if float(pos["qty"]) > 0 and not pos.get("system_order", False):
                non_system_positions = True
                break
        
        if non_system_positions:
            msg += "\n📊 非本系统持仓:\n"
            for symbol, pos in existing_positions.items():
                # 只显示未转入本系统的持仓
                if float(pos["qty"]) > 0 and not pos.get("system_order", False):
                    # 获取实时数据
                    realtime_pos = await get_position(symbol)
                    if realtime_pos:
                        # 使用实时数据计算盈亏
                        profit = realtime_pos["unrealized_profit"]
                        if realtime_pos["entry_price"] > 0:
                            profit_percent = (profit / (realtime_pos["entry_price"] * realtime_pos["qty"])) * 100
                        else:
                            profit_percent = 0
                        profit_sign = "+" if profit > 0 else ""
                        
                        msg += (f"{symbol} {realtime_pos['side']} x{realtime_pos['leverage']}\n"
                                f"数量: {realtime_pos['qty']:.4f}\n"
                                f"开仓价: {realtime_pos['entry_price']:.4f}\n"
                                f"标记价: {realtime_pos['mark_price']:.4f}\n"
                                f"未实现盈亏: {profit_sign}{profit:.4f} USDT\n"
                                f"盈亏率: {profit_sign}{profit_percent:.2f}%\n")
    
    if not positions and not non_system_positions:
        msg += "\n当前无持仓"
    
    await update.message.reply_text(msg, reply_markup=reply_markup_main)

# --- show_help 函数 ---
async def show_help(update):
    help_text = (
        "📌 功能说明：\n"
        "1. 添加币种 - 添加监控的币种\n"
        "2. 删除币种 - 从监控列表中移除币种\n"
        "3. 开启监控 - 开始MA9/MA26监控\n"
        "4. 停止监控 - 停止监控\n"
        "5. 开启自动交易 - 设置杠杆和金额后自动交易\n"
        "6. 关闭自动交易 - 停止自动交易并可选择清仓\n"
        "7. 查看状态 - 显示当前监控和持仓状态\n"
        "8. 帮助 - 显示此帮助信息\n\n"
        "📊 信号规则：\n"
        "- 买入信号: MA9上穿MA26\n"
        "- 卖出信号: MA9下穿MA26\n\n"
        "💡 自动交易设置：\n"
        "- 全局设置：所有币种使用相同的杠杆和开仓金额\n"
        "- 逐一设置：为每个币种单独设置杠杆和开仓金额\n"
        "- 金额设置：设置的是名义价值（订单总价值）\n\n"
        "💡 非本系统持仓处理：\n"
        "开启自动交易时，如发现非本系统持仓，系统会引导您选择是否将这些持仓纳入本系统管理"
    )
    await update.message.reply_text(help_text, reply_markup=reply_markup_main)

# --- refresh_delete_list 函数（修复状态问题）---
async def refresh_delete_list(update, user_id):
    if not data["symbols"]:
        await update.message.reply_text("已无更多币种可删除", reply_markup=reply_markup_main)
        user_states[user_id] = {}
        return
    
    msg = "请选择要删除的币种：\n"
    for idx, s in enumerate(data["symbols"], 1):
        msg += f"{idx}. {s['symbol']} ({s['type']})\n"
    
    # 使用临时键盘，避免误触主菜单
    temp_keyboard = ReplyKeyboardMarkup([["取消"]], resize_keyboard=True)
    user_states[user_id] = {"step": "delete_symbol"}
    await update.message.reply_text(
        msg + "\n请输入编号继续删除，或输入'取消'返回", 
        reply_markup=temp_keyboard)

# --- start 函数 ---
async def start(update, context):
    user_id = update.effective_chat.id
    user_states[user_id] = {}
    print(f"用户 {user_id} 启动了机器人")
    await update.message.reply_text(
        "🚀 MA交易机器人已启动\n请使用下方菜单操作:",
        reply_markup=reply_markup_main)

# --- handle_message 函数（修复删除状态问题）---
async def handle_message(update, context):
    user_id = update.effective_chat.id
    text = update.message.text.strip()
    app = context.application
    
    print(f"收到来自用户 {user_id} 的消息: {text}")
 
    if text.lower() == "取消":
        if user_id in user_states:
            del user_states[user_id]
        await update.message.reply_text("操作已取消", reply_markup=reply_markup_main)
        return
 
    state = user_states.get(user_id, {})
    
    # 全局杠杆设置
    if state.get("step") == "set_global_leverage":
        try:
            leverage = int(text)
            if 1 <= leverage <= 125:
                trade_settings["global_leverage"] = leverage
                save_trade_settings(trade_settings)
                user_states[user_id] = {"step": "set_global_amount"}
                await update.message.reply_text(
                    f"全局杠杆设置完成 {leverage}x\n请输入全局每单金额(USDT):",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("杠杆需在1-125之间")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    # 全局金额设置
    elif state.get("step") == "set_global_amount":
        try:
            amount = float(text)
            if amount > 0:
                trade_settings["global_order_amount"] = amount
                save_trade_settings(trade_settings)
                user_states[user_id] = {"step": "set_take_profit"}
                await update.message.reply_text(
                    f"全局金额设置完成 {amount} USDT\n请输入止盈百分比(0表示不设置):",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("金额必须大于0")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    # 逐一设置杠杆
    elif state.get("step") == "set_individual_leverage":
        try:
            leverage = int(text)
            if 1 <= leverage <= 125:
                current_index = state["current_index"]
                symbol = state["symbols"][current_index]
                
                # 保存该币种的杠杆设置
                if symbol not in state["settings"]:
                    state["settings"][symbol] = {}
                state["settings"][symbol]["leverage"] = leverage
                
                # 更新状态为设置金额
                state["step"] = "set_individual_amount"
                user_states[user_id] = state
                
                await update.message.reply_text(
                    f"{symbol} 杠杆设置完成 {leverage}x\n请输入 {symbol} 的开仓金额(USDT):",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("杠杆需在1-125之间")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    # 逐一设置金额
    elif state.get("step") == "set_individual_amount":
        try:
            amount = float(text)
            if amount > 0:
                current_index = state["current_index"]
                symbol = state["symbols"][current_index]
                
                # 保存该币种的开仓金额
                state["settings"][symbol]["order_amount"] = amount
                
                # 移动到下一个币种
                current_index += 1
                state["current_index"] = current_index
                
                if current_index < len(state["symbols"]):
                    next_symbol = state["symbols"][current_index]
                    # 将状态改为设置下一个币种的杠杆
                    state["step"] = "set_individual_leverage"
                    user_states[user_id] = state
                    await update.message.reply_text(
                        f"{symbol} 开仓金额设置完成 {amount} USDT\n请设置 {next_symbol} 的杠杆倍数 (1-125):",
                        reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
                else:
                    # 所有币种设置完成，保存设置
                    trade_settings["individual_settings"] = state["settings"]
                    save_trade_settings(trade_settings)
                    
                    # 进入止盈止损设置
                    user_states[user_id] = {"step": "set_take_profit"}
                    await update.message.reply_text(
                        "所有币种设置完成！\n请输入止盈百分比(0表示不设置):",
                        reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("金额必须大于0")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    # 止盈设置
    elif state.get("step") == "set_take_profit":
        try:
            take_profit = float(text)
            if 0 <= take_profit <= 100:
                trade_settings["take_profit"] = take_profit
                save_trade_settings(trade_settings)
                user_states[user_id] = {"step": "set_stop_loss"}
                await update.message.reply_text(
                    f"止盈设置完成 {take_profit}%\n请输入止损百分比(0表示不设置):",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("止盈需在0-100%之间")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    # 止损设置
    elif state.get("step") == "set_stop_loss":
        try:
            stop_loss = float(text)
            if 0 <= stop_loss <= 100:
                trade_settings["stop_loss"] = stop_loss
                save_trade_settings(trade_settings)
                
                # 构建设置信息
                setting_info = "✅ 自动交易设置完成:\n"
                
                if trade_settings["setting_mode"] == "global":
                    setting_info += f"全局设置:\n" \
                                   f"杠杆: {trade_settings['global_leverage']}x\n" \
                                   f"下单金额: {trade_settings['global_order_amount']} USDT\n"
                else:
                    setting_info += "逐一设置:\n"
                    for symbol, settings in trade_settings["individual_settings"].items():
                        leverage = settings.get("leverage", trade_settings["global_leverage"])
                        order_amount = settings.get("order_amount", trade_settings["global_order_amount"])
                        setting_info += f"{symbol} 杠杆: {leverage}x  下单金额: {order_amount}USDT\n"
                
                setting_info += f"止盈: {trade_settings['take_profit']}%\n" \
                               f"止损: {trade_settings['stop_loss']}%\n\n" \
                               "是否开启自动交易？"
                
                keyboard = [
                    [InlineKeyboardButton("是，开启交易", callback_data="confirm_trade:yes")],
                    [InlineKeyboardButton("否，取消设置", callback_data="confirm_trade:no")]
                ]
                
                await update.message.reply_text(
                    setting_info,
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await update.message.reply_text("止损需在0-100%之间")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    # 删除币种状态（修复问题）
    elif state.get("step") == "delete_symbol":
        try:
            idx = int(text) - 1
            if 0 <= idx < len(data["symbols"]):
                removed = data["symbols"].pop(idx)
                with open(DATA_FILE, "w") as f:
                    json.dump(data, f)
                await update.message.reply_text(f"已删除 {removed['symbol']}")
                # 刷新列表并保持删除状态
                await refresh_delete_list(update, user_id)
            else:
                await update.message.reply_text("编号无效")
        except ValueError:
            await update.message.reply_text("请输入数字编号")
    
    elif state.get("step") == "add_symbol":
        keyboard = [
            [InlineKeyboardButton("现货", callback_data=f"select_type:{text.upper()}:spot")],
            [InlineKeyboardButton("合约", callback_data=f"select_type:{text.upper()}:contract")]
        ]
        await update.message.reply_text(
            f"请选择 {text.upper()} 类型:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    
    # 主菜单命令处理
    elif text == "1" or "添加币种" in text:
        user_states[user_id] = {"step": "add_symbol"}
        await update.message.reply_text("请输入币种（如 BTCUSDT）：输入'取消'可中断")
    
    elif text == "2" or "删除币种" in text:
        if not data["symbols"]:
            await update.message.reply_text("当前无已添加币种", reply_markup=reply_markup_main)
        else:
            await refresh_delete_list(update, user_id)
    
    elif text == "3" or "开启监控" in text:
        data["monitor"] = True
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
        global monitoring_task
        if not monitoring_task or monitoring_task.done():
            monitoring_task = asyncio.create_task(monitor_task(app))
        
        msg = "监控已开启\n当前监控:\n"
        for s in data["symbols"]:
            try:
                klines = await get_klines(s["symbol"], s["type"])
                if klines:
                    _, _, price = calculate_ma(klines)
                    msg += f"- {s['symbol']} ({s['type']}): {price:.4f}\n"
            except Exception as e:
                print(f"获取价格失败: {e}")
                msg += f"- {s['symbol']} ({s['type']}): 获取失败\n"
        await update.message.reply_text(msg, reply_markup=reply_markup_main)
    
    elif text == "4" or "停止监控" in text:
        data["monitor"] = False
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
        await update.message.reply_text("监控已停止", reply_markup=reply_markup_main)
    
    elif text == "5" or "开启自动交易" in text:
        await handle_auto_trade(update, context, True)
    
    elif text == "6" or "关闭自动交易" in text:
        await handle_auto_trade(update, context, False)
    
    elif text == "7" or "查看状态" in text:
        if user_id in user_states:
            del user_states[user_id]
        await show_status(update)

    elif text == "8" or "帮助" in text:
        if user_id in user_states:
            del user_states[user_id]
        await show_help(update)

# --- 主程序 ---
if __name__ == "__main__":
    # 创建应用
    print("正在创建应用...")
    app = ApplicationBuilder().token(TOKEN).build()
    
    # 添加处理器
    print("添加命令处理器...")
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # 初始化时间同步
    print("初始化时间同步...")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(time_sync.sync_time())
    
    # 启动机器人
    print("MA9/MA26交易机器人已启动")
    app.run_polling()
