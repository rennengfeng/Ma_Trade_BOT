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
    await time_sync.sync_time()
    prev_states = {}
    
    while data["monitor"]:
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
                
                # 止盈止损检查
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

# --- execute_trade 函数 ---
async def execute_trade(app, symbol, signal_type):
    if not trade_settings["auto_trade"]:
        return False
    
    try:
        # 设置杠杆
        leverage_resp = await set_leverage(symbol, trade_settings["leverage"])
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
        quantity = trade_settings["order_amount"] / price
        
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
                "entry_price": entry_price
            }
            
            # 发送通知
            pos_type = "多仓" if signal_type == "BUY" else "空仓"
            msg = f"✅ 开仓成功 {symbol} {pos_type}\n" \
                  f"价格: {entry_price:.4f}\n" \
                  f"数量: {quantity:.4f}"
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
async def close_position(app, symbol, close_type="signal", close_price=None):
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
        msg = f"📌 {symbol} 平仓完成\n" \
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
            
        return True
        
    except Exception as e:
        for uid in user_states.keys():
            await app.bot.send_message(chat_id=uid, text=f"❌ {symbol} 平仓出错: {str(e)}")
        return False

# --- handle_auto_trade 函数 ---
async def handle_auto_trade(update, context, enable):
    app = context.application
    if enable:
        # 使用简单的API调用验证连接
        ping_response = await binance_request("GET", "/fapi/v1/ping")
        if ping_response is None:
            await update.message.reply_text(
                "❌ 无法连接币安API，请检查API密钥和网络连接",
                reply_markup=reply_markup_main
            )
            return
        
        trade_settings["auto_trade"] = True
        with open(TRADE_SETTINGS_FILE, "w") as f:
            json.dump(trade_settings, f)
        
        user_states[update.effective_chat.id] = {"step": "set_leverage"}
        await update.message.reply_text(
            "✅ API验证成功\n请输入杠杆倍数 (1-125):",
            reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True)
        )
    else:
        trade_settings["auto_trade"] = False
        with open(TRADE_SETTINGS_FILE, "w") as f:
            json.dump(trade_settings, f)
        
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

# --- button_callback 函数 ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data_parts = query.data.split(":")
    app = context.application

    if data_parts[0] == "close_all":
        if data_parts[1] == "yes":
            positions_data = await binance_request("GET", "/fapi/v2/positionRisk", None, True)
            if positions_data:
                for pos in positions_data:
                    if float(pos["positionAmt"]) != 0:
                        symbol = pos["symbol"]
                        side = "SELL" if float(pos["positionAmt"]) > 0 else "BUY"
                        await place_market_order(symbol, side, abs(float(pos["positionAmt"])))
            await query.edit_message_text("所有持仓已清空")
        else:
            await query.edit_message_text("保留当前持仓")
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)

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
                except Exception as e:
                    print(f"获取价格失败: {e}")
                    msg += f"{s['symbol']} ({s['type']}): 获取价格失败\n"
            
            await query.edit_message_text(msg)
        else:
            await query.edit_message_text("您可以在菜单中手动开启监控")
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)
    
    elif data_parts[0] == "confirm_trade":
        if data_parts[1] == "yes":
            trade_settings["auto_trade"] = True
            with open(TRADE_SETTINGS_FILE, "w") as f:
                json.dump(trade_settings, f)
            
            user_states[user_id] = {}
            await query.edit_message_text(
                "✅ 自动交易已开启！\n" +
                f"杠杆: {trade_settings['leverage']}x\n" +
                f"每单金额: {trade_settings['order_amount']} USDT"
            )
        else:
            user_states[user_id] = {}
            trade_settings["auto_trade"] = False
            await query.edit_message_text("自动交易设置已取消")
        await app.bot.send_message(user_id, "请使用下方菜单继续操作：", reply_markup=reply_markup_main)

# --- show_status 函数（增强持仓显示）---
async def show_status(update):
    msg = f"监控状态: {'开启' if data['monitor'] else '关闭'}\n"
    msg += f"自动交易: {'开启' if trade_settings['auto_trade'] else '关闭'}\n"
    
    if trade_settings["auto_trade"]:
        msg += f"杠杆: {trade_settings['leverage']}x\n"
        msg += f"每单金额: {trade_settings['order_amount']} USDT\n"
        msg += f"止盈: {trade_settings['take_profit']}%\n"
        msg += f"止损: {trade_settings['stop_loss']}%\n"
    
    if data["symbols"]:
        msg += "\n监控列表:\n"
        for s in data["symbols"]:
            try:
                klines = await get_klines(s["symbol"], s["type"])
                if klines:
                    _, _, price = calculate_ma(klines)
                    msg += f"- {s['symbol']} ({s['type']}): {price:.4f}\n"
            except:
                msg += f"- {s['symbol']} ({s['type']}): 获取失败\n"
    
    # 直接从API获取持仓信息（详细版）
    positions_data = await binance_request("GET", "/fapi/v2/positionRisk", None, True)
    has_position = False
    
    if positions_data:
        for pos in positions_data:
            position_amt = float(pos["positionAmt"])
            if position_amt != 0:
                has_position = True
                symbol = pos["symbol"]
                pos_type = "多仓" if position_amt > 0 else "空仓"
                entry_price = float(pos["entryPrice"])
                mark_price = float(pos["markPrice"])
                leverage = pos["leverage"]
                unrealized_profit = float(pos["unRealizedProfit"])
                
                # 计算盈亏百分比
                if entry_price > 0:
                    profit_percent = ((mark_price - entry_price) / entry_price * 100 
                                      if pos_type == "多仓" else 
                                      (entry_price - mark_price) / entry_price * 100)
                    profit_sign = "+" if profit_percent > 0 else ""
                else:
                    profit_percent = 0
                    profit_sign = ""
                
                msg += (f"\n📊 持仓: {symbol} {pos_type} x{leverage}\n"
                        f"数量: {abs(position_amt)}\n"
                        f"开仓价: {entry_price:.4f}\n"
                        f"标记价: {mark_price:.4f}\n"
                        f"未实现盈亏: {unrealized_profit:.4f} USDT\n"
                        f"盈亏率: {profit_sign}{profit_percent:.2f}%")
    
    if not has_position:
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
        "- 卖出信号: MA9下穿MA26"
    )
    await update.message.reply_text(help_text, reply_markup=reply_markup_main)

# --- refresh_delete_list 函数 ---
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

# --- start 函数 ---
async def start(update, context):
    user_states[update.effective_chat.id] = {}
    await update.message.reply_text(
        "🚀 MA交易机器人已启动\n请使用下方菜单操作:",
        reply_markup=reply_markup_main)

# --- handle_message 函数 ---
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
        try:
            leverage = int(text)
            if 1 <= leverage <= 125:
                trade_settings["leverage"] = leverage
                user_states[user_id] = {"step": "set_amount"}
                await update.message.reply_text(
                    f"杠杆设置完成 {leverage}x\n请输入每单金额(USDT):",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("杠杆需在1-125之间")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    elif state.get("step") == "set_amount":
        try:
            amount = float(text)
            if amount > 0:
                trade_settings["order_amount"] = amount
                user_states[user_id] = {"step": "set_take_profit"}
                await update.message.reply_text(
                    f"金额设置完成 {amount} USDT\n请输入止盈百分比(0表示不设置):",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("金额必须大于0")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    elif state.get("step") == "set_take_profit":
        try:
            take_profit = float(text)
            if 0 <= take_profit <= 100:
                trade_settings["take_profit"] = take_profit
                user_states[user_id] = {"step": "set_stop_loss"}
                await update.message.reply_text(
                    f"止盈设置完成 {take_profit}%\n请输入止损百分比(0表示不设置):",
                    reply_markup=ReplyKeyboardMarkup([["取消"]], resize_keyboard=True))
            else:
                await update.message.reply_text("止盈需在0-100%之间")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    elif state.get("step") == "set_stop_loss":
        try:
            stop_loss = float(text)
            if 0 <= stop_loss <= 100:
                trade_settings["stop_loss"] = stop_loss
                
                keyboard = [
                    [InlineKeyboardButton("确认设置", callback_data="confirm_trade:yes")],
                    [InlineKeyboardButton("取消设置", callback_data="confirm_trade:no")]
                ]
                await update.message.reply_text(
                    f"✅ 自动交易设置完成:\n"
                    f"杠杆: {trade_settings['leverage']}x\n"
                    f"每单金额: {trade_settings['order_amount']} USDT\n"
                    f"止盈: {trade_settings['take_profit']}%\n"
                    f"止损: {trade_settings['stop_loss']}%",
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await update.message.reply_text("止损需在0-100%之间")
        except ValueError:
            await update.message.reply_text("请输入有效数字")
    
    elif state.get("step") == "delete_symbol":
        try:
            idx = int(text) - 1
            if 0 <= idx < len(data["symbols"]):
                removed = data["symbols"].pop(idx)
                with open(DATA_FILE, "w") as f:
                    json.dump(data, f)
                await update.message.reply_text(f"已删除 {removed['symbol']}")
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
        if not monitoring_task:
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
        await show_status(update)
    
    elif text == "8" or "帮助" in text:
        await show_help(update)

# --- 主程序 ---
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(time_sync.sync_time())
    
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("MA9/MA26交易机器人已启动")
    app.run_polling()
