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
    ContextTypes
)
from config import TOKEN, CHAT_ID, BINANCE_API_KEY, BINANCE_API_SECRET

# 文件路径
DATA_FILE = "symbols.json"
TRADE_SETTINGS_FILE = "trade_settings.json"

# K线参数
INTERVAL = "15m"
MA5_PERIOD = 9
MA20_PERIOD = 26

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
        """同步Binance服务器时间"""
        try:
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
                        print("获取Binance时间失败，使用本地时间")
        except Exception as e:
            print(f"时间同步异常: {e}")
    
    def get_corrected_time(self):
        """获取校准后的时间戳"""
        if time.time() - self._last_sync > 600:  # 10分钟未同步则强制同步
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
        "leverage": 10, 
        "order_amount": 100,
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

data = load_data()
trade_settings = load_trade_settings()
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
    """创建止盈止损订单"""
    if take_profit <= 0 and stop_loss <= 0:
        return None
    
    # 计算止盈止损价格
    if side == "BUY":  # 开多单时
        take_profit_price = entry_price * (1 + take_profit / 100)
        stop_loss_price = entry_price * (1 - stop_loss / 100)
        side = "SELL"  # 平多单
    else:  # 开空单时
        take_profit_price = entry_price * (1 - take_profit / 100)
        stop_loss_price = entry_price * (1 + stop_loss / 100)
        side = "BUY"  # 平空单
    
    params = {
        "symbol": symbol,
        "side": side,
        "quantity": round(quantity, 3),
        "price": round(take_profit_price, 4),
        "stopPrice": round(stop_loss_price, 4),
        "stopLimitPrice": round(stop_loss_price, 4),
        "stopLimitTimeInForce": "GTC"
    }
    return await binance_request("POST", "/fapi/v1/order/oco", params, True)

async def get_position(symbol):
    positions = await binance_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, True)
    if positions:
        for pos in positions:
            if float(pos["positionAmt"]) != 0:
                return {
                    "side": "LONG" if float(pos["positionAmt"]) > 0 else "SHORT",
                    "qty": abs(float(pos["positionAmt"])),
                    "leverage": int(pos["leverage"]),
                    "entryPrice": float(pos["entryPrice"])
                }
    return None

async def get_klines(symbol, market_type):
    limit = max(MA5_PERIOD, MA20_PERIOD) + 5
    if market_type == "contract":
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.upper()}&interval={INTERVAL}&limit={limit}"
    else:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={INTERVAL}&limit={limit}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    klines = await resp.json()
                    if len(klines) >= max(MA5_PERIOD, MA20_PERIOD):
                        return klines
                    return None
                return None
        except Exception as e:
            print(f"获取K线数据失败: {e}")
            return None

def calculate_ma(klines):
    closes = [float(k[4]) for k in klines]
    ma9 = sum(closes[-MA5_PERIOD:]) / MA5_PERIOD
    ma26 = sum(closes[-MA20_PERIOD:]) / MA20_PERIOD
    current_price = closes[-1]
    return ma9, ma26, current_price

async def send_position_notification(app, symbol, action, position_type, quantity, entry_price, current_price=None, leverage=None, profit=None, profit_percent=None):
    """发送仓位通知"""
    position_type_chinese = {
        "LONG": "多单",
        "SHORT": "空单",
        "多单": "多单",
        "空单": "空单"
    }.get(position_type, position_type)
    
    if action == "open":
        msg = f"📦 开仓: {symbol} {position_type_chinese}\n"
        msg += f"  杠杆: x{leverage}\n"
        msg += f"  数量: {quantity:.4f}\n"
        msg += f"  开仓价: {entry_price:.4f}"
        
        if trade_settings["take_profit"] > 0 or trade_settings["stop_loss"] > 0:
            if position_type == "LONG" or position_type == "多单":
                take_profit_price = entry_price * (1 + trade_settings["take_profit"] / 100)
                stop_loss_price = entry_price * (1 - trade_settings["stop_loss"] / 100)
            else:
                take_profit_price = entry_price * (1 - trade_settings["take_profit"] / 100)
                stop_loss_price = entry_price * (1 + trade_settings["stop_loss"] / 100)
            
            if trade_settings["take_profit"] > 0:
                msg += f"\n  止盈价: {take_profit_price:.4f} ({trade_settings['take_profit']}%)"
            if trade_settings["stop_loss"] > 0:
                msg += f"\n  止损价: {stop_loss_price:.4f} ({trade_settings['stop_loss']}%)"
    
    elif action == "close":
        msg = f"📦 平仓: {symbol} {position_type_chinese}\n"
        msg += f"  数量: {quantity:.4f}\n"
        msg += f"  开仓价: {entry_price:.4f}"
        
        if current_price is not None and profit is not None and profit_percent is not None:
            profit_sign = "+" if profit > 0 else ""
            msg += f"\n  平仓价: {current_price:.4f}"
            msg += f"\n  盈亏: {profit_sign}{profit:.4f} ({profit_sign}{profit_percent:.2f}%)"
    
    elif action == "stop_loss":
        msg = f"📉 止损触发: {symbol} {position_type_chinese}\n"
        msg += f"  数量: {quantity:.4f}\n"
        msg += f"  开仓价: {entry_price:.4f}\n"
        msg += f"  平仓价: {current_price:.4f}\n"
        profit_sign = "+" if profit > 0 else ""
        msg += f"  盈亏: {profit_sign}{profit:.4f} ({profit_sign}{profit_percent:.2f}%)"
    
    elif action == "take_profit":
        msg = f"📈 止盈触发: {symbol} {position_type_chinese}\n"
        msg += f"  数量: {quantity:.4f}\n"
        msg += f"  开仓价: {entry_price:.4f}\n"
        msg += f"  平仓价: {current_price:.4f}\n"
        profit_sign = "+" if profit > 0 else ""
        msg += f"  盈亏: {profit_sign}{profit:.4f} ({profit_sign}{profit_percent:.2f}%)"
    
    for uid in user_states.keys():
        await app.bot.send_message(chat_id=uid, text=msg)

async def execute_trade(app, symbol, signal_type):
    if not trade_settings["auto_trade"]:
        return False
    
    try:
        leverage_resp = await set_leverage(symbol, trade_settings["leverage"])
        if leverage_resp is None:
            print(f"设置杠杆失败: {symbol}")
            return False
        
        klines = await get_klines(symbol, "contract")
        if not klines:
            print(f"无法获取K线数据: {symbol}")
            return False
            
        price = float(klines[-1][4])
        quantity = trade_settings["order_amount"] / price
        
        if signal_type == "BUY":
            pos = await get_position(symbol)
            if pos and pos["side"] == "SHORT":
                for uid in user_states.keys():
                    await app.bot.send_message(chat_id=uid, text=f"⚠️ 检测到买入信号，正在清空{symbol}空单仓位...")
                await close_position(app, symbol)
            
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"🚀 正在为{symbol}开多单...")
                
            order = await place_market_order(symbol, "BUY", quantity)
            if order:
                print(f"开多单成功: {symbol}, 订单: {json.dumps(order, indent=2)}")
                entry_price = float(order.get('price', price))
                
                positions[symbol] = {"side": "LONG", "qty": quantity, "entry_price": entry_price}
                
                await send_position_notification(
                    app, symbol, "open", "多单", quantity, entry_price, 
                    leverage=trade_settings["leverage"]
                )
                
                if trade_settings["take_profit"] > 0 or trade_settings["stop_loss"] > 0:
                    oco_order = await place_oco_order(
                        symbol, "BUY", quantity, entry_price, 
                        trade_settings["take_profit"], trade_settings["stop_loss"]
                    )
                    if oco_order:
                        print(f"止盈止损订单设置成功: {symbol}, 订单: {json.dumps(oco_order, indent=2)}")
                        oco_orders[symbol] = oco_order
                    else:
                        print(f"止盈止损订单设置失败: {symbol}")
                
                return True
            else:
                for uid in user_states.keys():
                    await app.bot.send_message(chat_id=uid, text=f"❌ {symbol}开多单失败，请检查日志")
                return False
            
        elif signal_type == "SELL":
            pos = await get_position(symbol)
            if pos and pos["side"] == "LONG":
                for uid in user_states.keys():
                    await app.bot.send_message(chat_id=uid, text=f"⚠️ 检测到卖出信号，正在清空{symbol}多单仓位...")
                await close_position(app, symbol)
            
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"🚀 正在为{symbol}开空单...")
                
            order = await place_market_order(symbol, "SELL", quantity)
            if order:
                print(f"开空单成功: {symbol}, 订单: {json.dumps(order, indent=2)}")
                entry_price = float(order.get('price', price))
                
                positions[symbol] = {"side": "SHORT", "qty": quantity, "entry_price": entry_price}
                
                await send_position_notification(
                    app, symbol, "open", "空单", quantity, entry_price, 
                    leverage=trade_settings["leverage"]
                )
                
                if trade_settings["take_profit"] > 0 or trade_settings["stop_loss"] > 0:
                    oco_order = await place_oco_order(
                        symbol, "SELL", quantity, entry_price, 
                        trade_settings["take_profit"], trade_settings["stop_loss"]
                    )
                    if oco_order:
                        print(f"止盈止损订单设置成功: {symbol}, 订单: {json.dumps(oco_order, indent=2)}")
                        oco_orders[symbol] = oco_order
                    else:
                        print(f"止盈止损订单设置失败: {symbol}")
                
                return True
            else:
                for uid in user_states.keys():
                    await app.bot.send_message(chat_id=uid, text=f"❌ {symbol}开空单失败，请检查日志")
                return False
        
    except Exception as e:
        print(f"自动交易出错: {e}")
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=f"❌ {symbol}交易失败: {str(e)}")
        return False

async def close_position(app, symbol, close_type="signal", close_price=None):
    try:
        pos = await get_position(symbol)
        if not pos:
            print(f"没有找到仓位: {symbol}")
            return False
        
        position_type = "多单" if pos["side"] == "LONG" else "空单"
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=f"⚠️ 正在平仓{symbol}{position_type}...")
        
        side = "SELL" if pos["side"] == "LONG" else "BUY"
        result = await place_market_order(symbol, side, pos["qty"])
        
        if not result:
            print(f"平仓失败: {symbol}")
            for uid in user_states.keys():
                await app.bot.send_message(chat_id=uid, text=f"❌ {symbol}平仓失败")
            return False
        
        print(f"平仓成功: {symbol}, 订单: {json.dumps(result, indent=2)}")
        
        if close_price is None:
            klines = await get_klines(symbol, "contract")
            if klines:
                close_price = float(klines[-1][4])
            else:
                close_price = pos["entryPrice"]
        
        entry = pos["entryPrice"]
        qty = pos["qty"]
        if pos["side"] == "LONG":
            profit = (close_price - entry) * qty
            profit_percent = (close_price - entry) / entry * 100
        else:
            profit = (entry - close_price) * qty
            profit_percent = (entry - close_price) / entry * 100
        
        if close_type == "signal":
            await send_position_notification(
                app, symbol, "close", position_type, qty, entry, 
                close_price, profit=profit, profit_percent=profit_percent
            )
        elif close_type == "stop_loss":
            await send_position_notification(
                app, symbol, "stop_loss", position_type, qty, entry, 
                close_price, profit=profit, profit_percent=profit_percent
            )
        elif close_type == "take_profit":
            await send_position_notification(
                app, symbol, "take_profit", position_type, qty, entry, 
                close_price, profit=profit, profit_percent=profit_percent
            )
        elif close_type == "manual":
            await send_position_notification(
                app, symbol, "close", position_type, qty, entry, 
                close_price, profit=profit, profit_percent=profit_percent
            )
        
        if symbol in oco_orders:
            try:
                await binance_request("DELETE", f"/fapi/v1/orderList?symbol={symbol}&orderListId={oco_orders[symbol]['orderListId']}", {}, True)
                del oco_orders[symbol]
            except:
                pass
        
        if symbol in positions:
            del positions[symbol]
        
        return True
    except Exception as e:
        print(f"平仓出错: {e}")
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=f"❌ {symbol}平仓失败: {str(e)}")
        return False

# --- 监控任务 ---
async def monitor_task(app):
    await time_sync.sync_time()  # 启动时先同步时间
    prev_states = {}
    
    while data["monitor"]:
        for item in data["symbols"]:
            symbol = item["symbol"]
            symbol_key = f"{symbol}_{item['type']}"
            try:
                klines = await get_klines(symbol, item["type"])
                if not klines or len(klines) < max(MA5_PERIOD, MA20_PERIOD):
                    continue
                
                if symbol_key in prev_klines and klines[-1][0] == prev_klines[symbol_key][-1][0]:
                    continue
                
                prev_klines[symbol_key] = klines
                ma9, ma26, price = calculate_ma(klines)
                
                if symbol_key in prev_states:
                    prev_ma9, prev_ma26 = prev_states[symbol_key]
                    
                    if prev_ma9 <= prev_ma26 and ma9 > ma26:
                        signal_msg = f"📈 检测到买入信号 {symbol}\n价格: {price:.4f}"
                        for uid in user_states.keys():
                            await app.bot.send_message(chat_id=uid, text=signal_msg)
                        
                        if item["type"] == "contract" and trade_settings["auto_trade"]:
                            if await execute_trade(app, symbol, "BUY"):
                                pass
                    
                    elif prev_ma9 >= prev_ma26 and ma9 < ma26:
                        signal_msg = f"📉 检测到卖出信号 {symbol}\n价格: {price:.4f}"
                        for uid in user_states.keys():
                            await app.bot.send_message(chat_id=uid, text=signal_msg)
                        
                        if item["type"] == "contract" and trade_settings["auto_trade"]:
                            if await execute_trade(app, symbol, "SELL"):
                                pass
                
                prev_states[symbol_key] = (ma9, ma26)
                
                if item["type"] == "contract" and symbol in positions:
                    pos = positions[symbol]
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
                            await close_position(app, symbol, "take_profit", price)
                    
                    if trade_settings["stop_loss"] > 0:
                        if (pos["side"] == "LONG" and price <= stop_loss_price) or \
                           (pos["side"] == "SHORT" and price >= stop_loss_price):
                            for uid in user_states.keys():
                                await app.bot.send_message(chat_id=uid, text=f"📉 检测到{symbol}止损触发")
                            await close_position(app, symbol, "stop_loss", price)
                
            except Exception as e:
                print(f"监控 {symbol} 出错: {e}")
        
        await asyncio.sleep(60)

# --- 处理自动交易设置 ---
async def handle_auto_trade(update, context, enable):
    app = context.application
    if enable:
        if not await check_api_available():
            await update.message.reply_text(
                "❌ Binance API不可用，请检查API密钥和网络",
                reply_markup=reply_markup_main
            )
            return
        
        trade_settings["auto_trade"] = True
        with open(TRADE_SETTINGS_FILE, "w") as f:
            json.dump(trade_settings, f)
        
        user_states[update.effective_chat.id] = {"step": "set_leverage"}
        await update.message.reply_text(
            "✅ API验证成功\n请输入杠杆倍数 (1-125):\n\n输入'取消'可中断设置",
            reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True)
        )
    else:
        trade_settings["auto_trade"] = False
        with open(TRADE_SETTINGS_FILE, "w") as f:
            json.dump(trade_settings, f)
        
        has_position = False
        msg = "自动交易已关闭\n"
        for symbol in list(positions.keys()):
            pos = await get_position(symbol)
            if pos:
                has_position = True
                position_type = "多单" if pos["side"] == "LONG" else "空单"
                msg += f"当前持仓: {symbol} {position_type} x{pos['leverage']} 开仓价:{pos['entryPrice']:.2f}\n"
        
        if has_position:
            keyboard = [
                [InlineKeyboardButton("是", callback_data="close_all:yes")],
                [InlineKeyboardButton("否", callback_data="close_all:no")]
            ]
            reply_markup_inline = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                msg + "是否清空所有持仓?",
                reply_markup=reply_markup_inline
            )
        else:
            await update.message.reply_text(msg + "无持仓", reply_markup=reply_markup_main)

async def check_api_available():
    try:
        resp = await binance_request("GET", "/fapi/v1/ping")
        return resp == {}
    except:
        return False

# --- 按钮回调 ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data_parts = query.data.split(":")
    app = context.application

    if data_parts[0] == "close_all":
        if data_parts[1] == "yes":
            for symbol in list(positions.keys()):
                await close_position(app, symbol, "manual")
            await query.edit_message_text("所有持仓已清空", reply_markup=None)
        else:
            await query.edit_message_text("保持当前持仓", reply_markup=None)
        positions.clear()
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)

    elif data_parts[0] == "select_type":
        symbol = data_parts[1]
        market_type = data_parts[2]
        data["symbols"].append({"symbol": symbol, "type": market_type})
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
        await query.edit_message_text(f"已添加 {symbol} ({market_type})", reply_markup=None)
        
        keyboard = [
            [InlineKeyboardButton("是", callback_data="continue_add:yes")],
            [InlineKeyboardButton("否", callback_data="continue_add:no")]
        ]
        reply_markup_inline = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            "是否继续添加币种？",
            reply_markup=reply_markup_inline
        )
    
    elif data_parts[0] == "continue_add":
        if data_parts[1] == "yes":
            user_states[user_id] = {"step": "add_symbol"}
            await query.message.reply_text("请输入币种（如 BTCUSDT）：输入'取消'可中断", reply_markup=reply_markup_main)
        else:
            user_states[user_id] = {}
            keyboard = [
                [InlineKeyboardButton("是", callback_data="start_monitor:yes")],
                [InlineKeyboardButton("否", callback_data="start_monitor:no")]
            ]
            reply_markup_inline = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text(
                "是否立即开启监控？",
                reply_markup=reply_markup_inline
            )
    
    elif data_parts[0] == "start_monitor":
        if data_parts[1] == "yes":
            data["monitor"] = True
            with open(DATA_FILE, "w") as f:
                json.dump(data, f)
            global monitoring_task
            if not monitoring_task:
                monitoring_task = asyncio.create_task(monitor_task(context.application))
            
            msg = "监控已开启\n当前监控列表：\n"
            for s in data["symbols"]:
                try:
                    klines = await get_klines(s["symbol"], s["type"])
                    if klines:
                        _, _, price = calculate_ma(klines)
                        msg += f"{s['symbol']} ({s['type']}): {price:.4f}\n"
                    else:
                        msg += f"{s['symbol']} ({s['type']}): 获取数据失败\n"
                except:
                    msg += f"{s['symbol']} ({s['type']}): 获取价格失败\n"
            
            await query.edit_message_text(msg, reply_markup=None)
            await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
        else:
            await query.edit_message_text("您可以在菜单中手动开启监控", reply_markup=None)
            await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
    
    elif data_parts[0] == "confirm_trade":
        if data_parts[1] == "yes":
            trade_settings["auto_trade"] = True
            with open(TRADE_SETTINGS_FILE, "w") as f:
                json.dump(trade_settings, f)
            
            user_states[user_id] = {}
            await query.edit_message_text(
                "✅ 自动交易已开启！\n"
                f"杠杆: {trade_settings['leverage']}x\n"
                f"每单金额: {trade_settings['order_amount']} USDT\n"
                f"止盈点: {trade_settings['take_profit']}%\n"
                f"止损点: {trade_settings['stop_loss']}%",
                reply_markup=None
            )
            await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
        else:
            user_states[user_id] = {}
            trade_settings["auto_trade"] = False
            await query.edit_message_text("自动交易设置已取消", reply_markup=None)
            await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)

# --- 查看状态 ---
async def show_status(update):
    msg = f"监控状态: {'开启' if data['monitor'] else '关闭'}\n"
    msg += f"自动交易: {'开启' if trade_settings['auto_trade'] else '关闭'}"
    
    if trade_settings["auto_trade"]:
        msg += f"\n杠杆: {trade_settings['leverage']}x"
        msg += f"\n每单金额: {trade_settings['order_amount']} USDT"
        msg += f"\n止盈点: {trade_settings['take_profit']}%"
        msg += f"\n止损点: {trade_settings['stop_loss']}%"
    
    if data["symbols"]:
        msg += "\n\n📊 监控列表:"
        for s in data["symbols"]:
            try:
                klines = await get_klines(s["symbol"], s["type"])
                if klines:
                    _, _, price = calculate_ma(klines)
                    msg += f"\n- {s['symbol']} ({s['type']}): {price:.4f}"
                else:
                    msg += f"\n- {s['symbol']} ({s['type']}): 获取数据失败"
            except:
                msg += f"\n- {s['symbol']} ({s['type']}): 获取价格失败"
    else:
        msg += "\n\n📊 监控列表: 无"
    
    has_position = False
    for symbol in data["symbols"]:
        if symbol["type"] == "contract":
            pos = await get_position(symbol["symbol"])
            if pos:
                has_position = True
                position_type = "多单" if pos["side"] == "LONG" else "空单"
                msg += f"\n\n📦 持仓: {symbol['symbol']} {position_type}"
                msg += f"\n  杠杆: x{pos['leverage']}"
                msg += f"\n  数量: {pos['qty']:.4f}"
                msg += f"\n  开仓价: {pos['entryPrice']:.4f}"
                try:
                    klines = await get_klines(symbol["symbol"], "contract")
                    if klines:
                        _, _, current_price = calculate_ma(klines)
                        entry = pos["entryPrice"]
                        qty = pos["qty"]
                        if pos["side"] == "LONG":
                            profit = (current_price - entry) * qty
                            profit_percent = (current_price - entry) / entry * 100
                        else:
                            profit = (entry - current_price) * qty
                            profit_percent = (entry - current_price) / entry * 100
                        profit_sign = "+" if profit > 0 else ""
                        msg += f"\n  当前价: {current_price:.4f}"
                        msg += f"\n  盈亏: {profit_sign}{profit:.4f} ({profit_sign}{profit_percent:.2f}%)"
                        
                        if trade_settings["take_profit"] > 0 or trade_settings["stop_loss"] > 0:
                            if pos["side"] == "LONG":
                                take_profit_price = entry * (1 + trade_settings["take_profit"] / 100)
                                stop_loss_price = entry * (1 - trade_settings["stop_loss"] / 100)
                            else:
                                take_profit_price = entry * (1 - trade_settings["take_profit"] / 100)
                                stop_loss_price = entry * (1 + trade_settings["stop_loss"] / 100)
                            
                            if trade_settings["take_profit"] > 0:
                                msg += f"\n  止盈价: {take_profit_price:.4f} ({trade_settings['take_profit']}%)"
                            if trade_settings["stop_loss"] > 0:
                                msg += f"\n  止损价: {stop_loss_price:.4f} ({trade_settings['stop_loss']}%)"
                except Exception as e:
                    print(f"获取持仓信息出错: {e}")
                    msg += f"\n  当前价: 获取失败"
    
    if not has_position:
        msg += "\n\n📦 持仓: 无"
    
    await update.message.reply_text(msg, reply_markup=reply_markup_main)

# --- 帮助信息 ---
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
        "📊 自动交易说明：\n"
        "- 买入信号: 平空单并开多单\n"
        "- 卖出信号: 平多单并开空单\n"
        "- 金额设置: 输入USDT成本金额\n"
        "- 杠杆设置: 1-125倍\n"
        "- 止盈止损: 设置百分比，输入0表示不设置\n\n"
        "输入 '取消' 可中断当前操作"
    )
    await update.message.reply_text(help_text, reply_markup=reply_markup_main)

# --- 刷新删除列表 ---
async def refresh_delete_list(update, user_id):
    if not data["symbols"]:
        await update.message.reply_text("已无更多币种可删除", reply_markup=reply_markup_main)
        user_states[user_id] = {}
        return
    
    msg = "请选择要删除的币种：\n"
    for idx, s in enumerate(data["symbols"], 1):
        msg += f"{idx}. {s['symbol']} ({s['type']})\n"
    
    user_states[user_id] = {"step": "delete_symbol"}
    await update.message.reply_text(msg + "\n请输入编号继续删除，或输入'取消'返回", reply_markup=reply_markup_main)

# --- 启动命令 ---
async def start(update, context):
    user_states[update.effective_chat.id] = {}
    await update.message.reply_text(
        "🚀 MA交易机器人已启动\n请使用下方菜单操作:\n\n输入'取消'可中断当前操作",
        reply_markup=reply_markup_main
    )

# --- 消息处理 ---
async def handle_message(update, context):
    user_id = update.effective_chat.id
    text = update.message.text.strip()
    app = context.application

    if text.lower() == "取消":
        user_states[user_id] = {}
        await update.message.reply_text("操作已取消", reply_markup=reply_markup_main)
        return

    state = user_states.get(user_id, {})
    
    if state.get("step") == "set_leverage":
        if text == "0":
            await update.message.reply_text("杠杆倍数不能为0，请输入1-125之间的数字")
            return
            
        try:
            leverage = int(text)
            if 1 <= leverage <= 125:
                trade_settings["leverage"] = leverage
                user_states[user_id] = {"step": "set_amount"}
                await update.message.reply_text(
                    f"杠杆设置完成 {leverage}x\n请输入每单成本金额(USDT):\n\n输入'取消'可中断设置",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True)
                )
                return
            else:
                await update.message.reply_text("杠杆倍数需在1-125之间，请重新输入")
                return
        except ValueError:
            await update.message.reply_text("请输入有效数字")
            return

    elif state.get("step") == "set_amount":
        if text == "0":
            await update.message.reply_text("金额不能为0，请输入大于0的数字")
            return
            
        try:
            amount = float(text)
            if amount > 0:
                trade_settings["order_amount"] = amount
                user_states[user_id] = {"step": "set_take_profit"}
                await update.message.reply_text(
                    f"金额设置完成 {amount} USDT\n请输入止盈点(百分比，0表示不设置):\n\n输入'取消'可中断设置",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True)
                )
                return
            else:
                await update.message.reply_text("金额必须大于0")
                return
        except ValueError:
            await update.message.reply_text("请输入有效数字")
            return
    
    elif state.get("step") == "set_take_profit":
        try:
            take_profit = float(text)
            if 0 <= take_profit <= 100:
                trade_settings["take_profit"] = take_profit
                user_states[user_id] = {"step": "set_stop_loss"}
                await update.message.reply_text(
                    f"止盈点设置完成 {take_profit}%\n请输入止损点(百分比，0表示不设置):\n\n输入'取消'可中断设置",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True)
                )
                return
            else:
                await update.message.reply_text("止盈点需在0-100%之间")
                return
        except ValueError:
            await update.message.reply_text("请输入有效数字")
            return
    
    elif state.get("step") == "set_stop_loss":
        try:
            stop_loss = float(text)
            if 0 <= stop_loss <= 100:
                trade_settings["stop_loss"] = stop_loss
                
                msg = (
                    f"自动交易设置完成：\n"
                    f"杠杆: {trade_settings['leverage']}x\n"
                    f"每单金额: {trade_settings['order_amount']} USDT\n"
                    f"止盈点: {trade_settings['take_profit']}%\n"
                    f"止损点: {trade_settings['stop_loss']}%\n\n"
                    "是否确认开启自动交易？"
                )
                
                keyboard = [
                    [InlineKeyboardButton("确认开启", callback_data="confirm_trade:yes")],
                    [InlineKeyboardButton("取消设置", callback_data="confirm_trade:no")]
                ]
                reply_markup_inline = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    msg,
                    reply_markup=reply_markup_inline
                )
                return
            else:
                await update.message.reply_text("止损点需在0-100%之间")
                return
        except ValueError:
            await update.message.reply_text("请输入有效数字")
            return
    
    elif state.get("step") == "delete_symbol":
        if text.lower() == "取消":
            user_states[user_id] = {}
            await update.message.reply_text("操作已取消", reply_markup=reply_markup_main)
            return
            
        try:
            idx = int(text) - 1
            if 0 <= idx < len(data["symbols"]):
                removed = data["symbols"].pop(idx)
                with open(DATA_FILE, "w") as f:
                    json.dump(data, f)
                await update.message.reply_text(f"已删除 {removed['symbol']}")
                await refresh_delete_list(update, user_id)
            else:
                await update.message.reply_text("编号无效，请重新输入", reply_markup=reply_markup_main)
        except ValueError:
            await update.message.reply_text("请输入数字编号", reply_markup=reply_markup_main)
        return

    if state.get("step") == "add_symbol":
        if text.lower() == "取消":
            user_states[user_id] = {}
            await update.message.reply_text("操作已取消", reply_markup=reply_markup_main)
            return
            
        keyboard = [
            [InlineKeyboardButton("现货", callback_data=f"select_type:{text.upper()}:spot")],
            [InlineKeyboardButton("合约", callback_data=f"select_type:{text.upper()}:contract")]
        ]
        reply_markup_inline = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"请选择 {text.upper()} 的类型：",
            reply_markup=reply_markup_inline
        )
        return

    if text == "1" or "添加币种" in text:
        user_states[user_id] = {"step": "add_symbol"}
        await update.message.reply_text("请输入币种（如 BTCUSDT）：输入'取消'可中断", reply_markup=reply_markup_main)
        return
    
    elif text == "2" or "删除币种" in text:
        if not data["symbols"]:
            await update.message.reply_text("当前无已添加币种", reply_markup=reply_markup_main)
            return
        
        msg = "请选择要删除的币种：\n"
        for idx, s in enumerate(data["symbols"], 1):
            msg += f"{idx}. {s['symbol']} ({s['type']})\n"
        
        user_states[user_id] = {"step": "delete_symbol"}
        await update.message.reply_text(msg + "\n请输入编号删除，或输入'取消'中断", reply_markup=reply_markup_main)
        return
    
    elif text == "3" or "开启监控" in text:
        data["monitor"] = True
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
        global monitoring_task
        if not monitoring_task:
            monitoring_task = asyncio.create_task(monitor_task(context.application))
        
        msg = "监控已开启\n当前监控列表：\n"
        for s in data["symbols"]:
            try:
                klines = await get_klines(s["symbol"], s["type"])
                if klines:
                    _, _, price = calculate_ma(klines)
                    msg += f"{s['symbol']} ({s['type']}): {price:.4f}\n"
                else:
                    msg += f"{s['symbol']} ({s['type']}): 获取数据失败\n"
            except:
                msg += f"{s['symbol']} ({s['type']}): 获取价格失败\n"
        
        await update.message.reply_text(msg, reply_markup=reply_markup_main)
        return
    
    elif text == "4" or "停止监控" in text:
        data["monitor"] = False
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
        await update.message.reply_text("监控已停止", reply_markup=reply_markup_main)
        return
    
    elif text == "5" or "开启自动交易" in text:
        await handle_auto_trade(update, context, True)
        return
    
    elif text == "6" or "关闭自动交易" in text:
        await handle_auto_trade(update, context, False)
        return
    
    elif text == "7" or "查看状态" in text:
        await show_status(update)
        return
    
    elif text == "8" or "帮助" in text:
        await show_help(update)
        return

# --- 初始化同步 ---
async def initialize():
    await time_sync.sync_time()

# --- 主程序 ---
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(initialize())
    
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("MA交易机器人已启动（带时间同步）")
    app.run_polling()
