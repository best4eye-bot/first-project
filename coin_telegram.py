import pytz
import pyupbit
#import telegram
import jwt
import hashlib
import os
import requests
import uuid
import asyncio
import aiohttp
import aioschedule as schedule
from aiohttp import ClientSession
from aiogram import Bot
from aiogram import types
from asyncio_throttle import Throttler
from urllib.parse import urlencode
import numpy as np
import pandas as pd
import threading
import base64
import json
import configparser
import hmac
import logging
import warnings

from aiogram.utils.deprecated import deprecated
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import ParseMode
#from throttler import Throttler
from logging import FileHandler

warnings.simplefilter("ignore", deprecated)

# Set up logging
logging.basicConfig(level=logging.WARNING)
error_logger = logging.getLogger("error")
debug_logger = logging.getLogger("debug")
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)  # Adjust the log level as needed

# Set up logger for errors
error_handler = logging.FileHandler("error.log")
error_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
error_handler.setFormatter(error_formatter)
error_handler.setLevel(logging.WARNING)  # Log only warning or error messages
logger.addHandler(error_handler)

# Set up logger for debugs
debug_handler = logging.FileHandler("debug.log")
debug_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
debug_handler.setFormatter(debug_formatter)
debug_handler.setLevel(logging.DEBUG)
logger.addHandler(debug_handler)


# Read the API keys and chat ID from the config file
def read_config():
    config = configparser.ConfigParser()
    config.read('config.ini')
    logger.debug("Configuration file read successfully.")
    return config.get('UPBIT', 'ACCESS_KEY'), config.get('UPBIT', 'SECRET_KEY'), config.get('TELEGRAM', 'BOT_TOKEN'), config.get('TELEGRAM', 'CHAT_ID')

UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = read_config()

# Set the maximum number of positions
MAX_POSITIONS = 3
MAKER_FEE = 0.0005
TAKER_FEE = 0.0005
SLIPPAGE = 0.01


# Function to create JWT token
def create_jwt_token(access_key, secret_key, query):
    payload = {
        'access_key': access_key,
        'nonce': str(uuid.uuid4()),
    }
    
    if query:  # only add these fields if there are query parameters
        query_hash = hashlib.sha512(query.encode()).hexdigest()
        payload.update({
            'query_hash': query_hash,
            'query_hash_alg': 'SHA512',
        })
    
    jwt_token = jwt.encode(payload, secret_key)
    return jwt_token


# Initialize Throttler instances
# For order requests: 8 requests per second and 200 requests per minute
order_throttler = Throttler(rate_limit=8, period=1)  # 8 requests per second
order_throttler_minute = Throttler(rate_limit=200, period=60)  # 200 requests per minute

# For other API requests: 30 requests per second and 900 requests per minute
api_throttler = Throttler(rate_limit=30, period=1)  # 30 requests per second
api_throttler_minute = Throttler(rate_limit=900, period=60)  # 900 requests per minute

# Connect Upbit API with async using aiohttp 'fetch()'
async def fetch(url, access_key, secret_key, query, is_order_request=False):
    jwt_token = create_jwt_token(access_key, secret_key, query)
    authorization_token = f"Bearer {jwt_token}"
    headers = {"Authorization": authorization_token, "Content-Type": "application/json; charset=utf-8"}

    if is_order_request:
        throttler_to_use = order_throttler
        throttler_minute_to_use = order_throttler_minute
    else:
        throttler_to_use = api_throttler
        throttler_minute_to_use = api_throttler_minute

    async with throttler_to_use, throttler_minute_to_use:  # apply throttling
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                # Handle 429 status code
                if response.status == 429:
                    debug_logger.warning("API rate limit reached, sleeping for 1 second")
                    await asyncio.sleep(1)
                    return await fetch(url, access_key, secret_key, query, is_order_request)

                data = await response.json()
                return data


# Set send_telegram_message definition using async for wait 598 secs
async def send_telegram_message(bot, message):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

    except telegram.error.RetryAfter as e:
        error_logger.error(f"Telegram API rate limit exceeded. Retrying in {e.retry_after} seconds.")
        await asyncio.sleep(e.retry_after)
        await send_telegram_message(bot, message)

    except Exception as e:
        error_logger.error(f"Error sending message: {e}")

    finally:
        await bot.close()


# Fetch account balances from Upbit API
async def get_balances(session):
    query = {}
    url = "https://api.upbit.com/v1/accounts"
    return await fetch(session, url, UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, json.dumps(query))


# Get the total KRW balance(numberstring is Upbit) of each asset turn to float for calculation
async def get_balance(session):
    balances = await get_balances(session)
    if not balances:
        error_logger.error("Error fetching balances.")
        return None

    total_krw_balance = 0

    for balance in balances:
        currency = balance.get('currency')
        if not currency:
            error_logger.error("Unexpected balance format.")
            continue

        balance_float = float(balance.get('balance', '0'))  # Convert the balance to a float

        if currency == 'KRW':
            total_krw_balance += balance_float
        else:
            ticker = f"KRW-{currency}"
            current_price = await get_current_price(session, ticker)
            if current_price:
                total_krw_balance += balance_float * current_price
            else:
                error_logger.error(f"Error fetching {ticker} current price.")

    return total_krw_balance


# Get current price of a specific ticker
async def get_current_price(session, ticker):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    async with session.get(f"https://api.upbit.com/v1/ticker?markets={ticker}", headers=headers) as response:
        data = await response.json()
        if 'error' in data:
            error_logger.error(f"Error fetching {ticker} current price: {data['error']}")
            return None
        return data[0]['trade_price'] if data else None

# Fetch current prices of multiple tickers
async def fetch_current_prices(session, tickers):
    tasks = [get_current_price(session, ticker) for ticker in tickers]
    prices = await asyncio.gather(*tasks)
    return dict(zip(tickers, prices))


# Report current status of accounts on BTC, ETH, DOGE and fetching price one by one
async def get_trading_pair_price(session, pair):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    async with session.get(f"https://api.upbit.com/v1/ticker?markets={pair}", headers=headers) as response:
        data = await response.json()
        if 'error' in data:
            error_logger.error(f"Error fetching {pair} data: {data['error']}")
            return None
        return data[0]['trade_price'] if data else None


# Fetching trading pair prices
async def fetch_trading_pair_prices(session, trading_pairs):
    tasks = [get_trading_pair_price(session, pair) for pair in trading_pairs]
    prices = await asyncio.gather(*tasks)
    trading_pair_prices = dict(zip(trading_pairs, prices))

    if not trading_pair_prices:
        error_logger.error("No trading pair prices were fetched successfully.")
    else:
        for pair, price in trading_pair_prices.items():
            if price is None:
                error_logger.error(f"Failed to fetch price for {pair}")
            else:
                debug_logger.info(f"{pair}: {price}")

    return trading_pair_prices


# Buy market order one by one
async def buy_market_order(session, ticker, volume):
    url = "https://api.upbit.com/v1/orders"
    query = {
        "market": ticker,
        "side": "bid",
        "volume": str(volume),  # Convert volume to string
        "ord_type": "market",
    }

    response_data = await fetch(url, UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, json.dumps(query))

    if 'uuid' in response_data:
        debug_logger.info(f"Successfully placed buy market order for {ticker} with volume {volume}")
    else:
        error_logger.error(f"Error placing buy market order for {ticker}: {response_data}")


# Buy order async function
async def process_buy_orders(session, orders):
    tasks = [buy_market_order(session, ticker, volume) for ticker, volume in orders]
    await asyncio.gather(*tasks)
   

# Send the initial message for account and Bot function
async def get_krw_balance(balances):
    for balance in balances:
        if balance['currency'] == 'KRW':
            return balance['balance']
    return None


# Telegram initial notification message
async def send_initial_notification(bot, session, bot_instances):
    try:
        # Send a message to inform that the bot has started
        await send_telegram_message(bot, "Bot has started.")
        
        # Schedule the execution of get_balances and get_orders coroutines using ensure_future
        balances_future = asyncio.ensure_future(get_balances(session))
        orders_future = asyncio.ensure_future(get_orders(session))

        # Get moving averages for all trading pairs
        moving_averages = []
        for bot_instance in bot_instances:
            short_ma, long_ma = bot_instance.get_ma()
            moving_averages.append((bot_instance.ticker, short_ma, long_ma))

        # Format the moving averages
        formatted_moving_averages = "\n".join([f"{ticker}: {short_ma:.2f} (short), {long_ma:.2f} (long)" for ticker, short_ma, long_ma in moving_averages])

        # Wait for the scheduled get_balances and get_orders coroutines to complete using asyncio.gather
        balances, orders = await asyncio.gather(balances_future, orders_future)

        # Format the balances and orders information
        formatted_balances = format_balances(balances)
        formatted_orders = format_orders(orders)

        # Send the formatted information as a message
        await send_telegram_message(bot, f"Initial balances:\n{formatted_balances}")
        await send_telegram_message(bot, f"Moving averages:\n{formatted_moving_averages}")
        await send_telegram_message(bot, f"Current orders:\n{formatted_orders}")

    except Exception as e:
        error_logger.exception(f"Error in sending initial notification: {e}")
        await send_telegram_message(bot, f"Error in sending initial notification: {e}")



# Telegram daily notification message
async def send_daily_notification(bot, session):
    try:
        balances = await get_balances(session)

        if balances is None:
            error_logger.error("Error fetching balances")
            await send_telegram_message(bot, "Error fetching balances")
            return

        balance_message = "Autotrading is still running.\n\nAccount balances:\n"

        for balance in balances:
            coin = balance['currency']
            amount = balance['balance']
            balance_message += f"{coin}: {amount}\n"

        await send_telegram_message(bot, balance_message)

    except Exception as e:
        error_logger.exception(f"Error in sending daily notification: {e}")
        await send_telegram_message(bot, f"Error in sending daily notification: {e}")


# Making Trading pair bots
async def create_trading_bot(session, pair, bot):
    try:
        bot_instance = await TradingBot.create(session, pair, bot)
        if bot_instance is None:
            error_logger.error(f"Error creating trading bot for {pair}")
            return None
        return bot_instance
    except Exception as e:
        error_logger.exception(f"Error creating trading bot for {pair}: {e}")
        await send_telegram_message(bot, f"Error creating trading bot for {pair}: {e}")
        return None


# TradingBot initialize for trading and get parameters
async def initialize_trading_bots(session, trading_pairs, bot):
    tasks = [create_trading_bot(session, pair, bot) for pair in trading_pairs]
    trading_bots = await asyncio.gather(*tasks)
    trading_bots = [bot for bot in trading_bots if bot is not None]  # remove any None values
    return trading_bots



# Trading Bot class part    
class TradingBot:
    class CurrentPriceFetchError(Exception):
        """Raised when there is an error fetching the current price."""
    
    def __init__(self, session, ticker, bot, slippage=0.01, trading_fee=0.0005):       
        
        self.balances = None
        self.bot = bot
        self.buy_signal = False
        self.buy_prices = []
        self.bollinger_bands = None
        self.max_positions = MAX_POSITIONS
        self.macd = None
        self.position_size = None
        self.rsi = None
        self.risk_per_trade = 0.01        
        self.sell_signal = False
        self.session = session        
        self.sell_prices = []        
        self.stop_loss = None
        self.stop_loss_price = None            
        self.signal = None
        self.slippage = slippage
        self.twap = None
        self.ticker = ticker
        self.trading_fee = trading_fee
        self.take_profit = 0.05
        self.ticker = ticker
        self.trailing_stop_loss = 0.02
        self.previous_macd_histogram = None        
        self.ma_5 = None
        self.ma_10 = None
        self.ma_20 = None
        self.cache = {}  # Cache attribute
        self.terminate_event = asyncio.Event()  # Terminate event
        self.scheduled_tasks = []  # List to hold scheduled tasks
        self.trailing_stop = None
        self.trailing_stop_pct = 0.02  # 2% trailing stop, adjust as needed

    @classmethod
    async def create(cls, session, ticker, bot, slippage=0.01, trading_fee=0.0005):
        try:
            bot_instance = cls(session, ticker, bot, slippage, trading_fee)
            bot_instance.base_currency = 'KRW'  # Set the base currency here
            bot_instance.position_size = await bot_instance.get_position_size(session)
            await bot_instance.fetch_balances()  # Fetch balances when creating a new instance
            return bot_instance
        except Exception as e:
            error_logger.error(f"Error creating trading bot for {ticker}: {e}")
            return None

    # Session closer
    async def close(self):
        try:
            await self.session.close()
        except Exception as e:
            error_logger.error(f"Error closing session for {self.ticker}: {e}")

    # Authentication on Upbit API header function 
    def get_authentication_headers(self, query):
        query_string = urlencode(query).encode()
        query_hash = hashlib.sha512(query_string).hexdigest()
        payload = {
            'access_key': UPBIT_ACCESS_KEY,
            'nonce': str(uuid.uuid4()),
            'query_hash': query_hash,
            'query_hash_alg': 'SHA512',
        }

        jwt_token = jwt.encode(payload, UPBIT_SECRET_KEY, algorithm="HS256")
        authorize_token = 'Bearer {}'.format(jwt_token)
        headers = {"Authorization": authorize_token, "Content-Type": "application/json"}

        return headers
    
    # Fetch balances
    async def fetch_balances(self):
        url = "https://api.upbit.com/v1/accounts"
        query = {}
        headers = self.get_authentication_headers(query)

        try:
            async with self.session.get(url, headers=headers) as response:
                data = await response.json()
                if 'error' in data:
                    error_logger.error(f"Error fetching balances for {self.ticker}: {data['error']}")
                    self.balances = None
                else:
                    self.balances = {item['currency']: float(item["balance"]) for item in data}
        except Exception as e:
            error_logger.error(f"Error fetching balances for {self.ticker}: {e}")
            self.balances = None


    async def get_account_balance(self, currency):
        try:
            if self.balances is None:
                await self.fetch_balances()

            if self.balances is None:
                return 0.0

            balance = self.balances.get(currency)
            if balance is None:
                error_logger.error(f"No balance found for currency: {currency}")
            return balance
        except Exception as e:
            error_logger.error(f"Error getting account balance for {currency}: {e}")
            return 0.0
    

    async def get_volume(self, current_price, allocated_amount):
        try:
            if allocated_amount is None:
                error_logger.error(f"Error fetching allocated amount for {self.ticker}")
                return None

            volume = allocated_amount / current_price
            return volume
        except Exception as e:
            error_logger.error(f"Error getting volume for {self.ticker}: {e}")
            return None


    async def get_current_price(self, ticker):
        url = f"https://api.upbit.com/v1/ticker"
        query = {"markets": ticker}
        headers = self.get_authentication_headers(query)

        try:
            async with self.session.get(url, params=query, headers=headers) as response:
                data = await response.json()
                if 'error' in data:
                    raise CurrentPriceFetchError(f"Error fetching {ticker} current price: {data['error']}")
                return data[0]['trade_price']
        except Exception as e:
            error_logger.error(f"Error fetching current price for {ticker}: {e}")
            return None


    async def async_sell_market_order(self, ticker, volume):
        url = "https://api.upbit.com/v1/orders"
        query = {
            "market": ticker,
            "side": "ask",
            "volume": str(volume),
            "ord_type": "market",
        }

        headers = self.get_authentication_headers(query)

        try:
            async with self.session.post(url, json=query, headers=headers) as response:
                response_data = await response.json()
                if response.status == 201:
                    logger.info(f"Successfully placed sell market order for {ticker} with volume {volume}")
                else:
                    error_logger.error(f"Error placing sell market order for {ticker}: {response_data}")
                    if 'error' in response_data and 'rate limit' in response_data['error']:
                        logger.warning("Rate limit exceeded. Sleeping for 60 seconds.")
                        await asyncio.sleep(60)
        except Exception as e:
            error_logger.error(f"Error placing sell market order for {ticker}: {e}")
            await asyncio.sleep(1)


    # error handling for excess API call
    async def handle_error(self, e, action):
        error_logger.error(f"Error in {action} for {self.ticker}: {e}", exc_info=True)
        await send_telegram_message(self.bot, f"Error in {action} for {self.ticker}: {e}")



    #사용가능한 KRW잔고기반 position size계산 및 trade risk and fee정산후 정수화
    async def get_position_size(self, session):
        try:
            current_price = await self.get_current_price(self.ticker)

            if current_price is None:
                error_logger.error(f"Error fetching current price for {self.ticker}")
                return None

            allocated_amount = await self.get_allocated_amount(current_price)
            volume = await self.get_volume(current_price, allocated_amount)

            account = await get_balances(session)
            krw_balance = float([wallet['balance'] for wallet in account if wallet['currency'] == 'KRW'][0])
            risk_amount = krw_balance * self.risk_per_trade
            volume = (risk_amount / current_price) * (1 - 0.0025) * (1 - 0.01)

            return int(volume)

        except Exception as e:
            error_logger.error(f"Error in getting position size for {self.ticker}: {e}", exc_info=True)
            await send_telegram_message(self.bot, f"Error in getting position size for {self.ticker}: {e}")


    # get MA on simplyfing 
    async def get_moving_average(self, window):
        try:
            candles = await self.get_async_ohlcv(self.ticker, interval='day', count=window)
            if candles is None:
                await self.handle_error("Error fetching data for moving average calculation", "getting moving average")
                return None
        
            debug_logger.debug("Candles: %s", candles)  # Add this line to print the candles data

            close_prices_series = candles['trade_price']
            moving_average = close_prices_series.rolling(window=window).mean().values[-1]
            return moving_average
        except Exception as e:
            logger.error(f"Error in getting moving average for {self.ticker}: {e}", exc_info=True)
            await self.handle_error(e, "getting moving average")
            return None



    # Get ohlcv, using sigle session for multiple requests not everytime multi sessions
    # This function must controlled in main() with 'await bot_instance.close()'
    async def get_async_ohlcv(self, ticker, interval='60', count=200):
        if interval == 'day':
            url = "https://api.upbit.com/v1/candles/days"
        elif interval in ['1', '3', '5', '10', '15', '30', '60', '240']:
            url = f"https://api.upbit.com/v1/candles/minutes/{interval}"
        else:
            logger.error(f"Invalid interval: {interval}")
            return None

        params = {"market": ticker, "count": count}

        try:
            async with self.session.get(url, params=params) as response:
                data = await response.json()
                if 'error' in data:
                    logger.error(f"Error fetching {ticker} OHLCV data: {data['error']}")
                    return None
                ohlcv_dataframe = pd.DataFrame(data)
                ohlcv_dataframe = ohlcv_dataframe[['candle_date_time_utc', 'opening_price', 'high_price', 'low_price', 'trade_price', 'candle_acc_trade_volume', 'unit']]
                ohlcv_dataframe.columns = ['datetime', 'open', 'high', 'low', 'close', 'volume', 'unit']
                try:
                    close_prices = ohlcv_dataframe['close']
                except KeyError:
                    print("Error: 'close' Key not found in the DataFrame")
                    # Handle the error or use an alternative key to get the closing price
                    return None
                return close_prices
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                logger.warning(f"Rate limit exceeded while fetching {ticker} OHLCV data. Retrying in {e.headers.get('Retry-After', 60)} seconds.")
                await asyncio.sleep(int(e.headers.get('Retry-After', 60)))
                return await self.get_async_ohlcv(ticker, interval, count)
            else:
                logger.error(f"Error fetching {ticker} OHLCV data: {e}", exc_info=True)
                return None
        except Exception as e:
            logger.error(f"Error fetching {ticker} OHLCV data: {e}", exc_info=True)
            return None



    # Asynchronous call for get_orderbook() functions
    async def get_async_orderbook(self, ticker):
        url = "https://api.upbit.com/v1/orderbook"
        params = {"markets": ticker}

        try:
            async with self.session.get(url, params=params) as response:
                data = await response.json()
                return data[0] if data else None
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                logger.warning(f"Rate limit exceeded while fetching {ticker} orderbook data. Retrying in {e.headers.get('Retry-After', 60)} seconds.")
                await asyncio.sleep(int(e.headers.get('Retry-After', 60)))
                return await self.get_async_orderbook(ticker)
            else:
                error_logger.error(f"Error fetching {ticker} orderbook data: {e}", exc_info=True)
                return None
        except Exception as e:
            error_logger.error(f"Error fetching {ticker} orderbook data: {e}", exc_info=True)
            return None


    # Get Moving Average 
    async def get_ma(self):
        try:
            candles_df = await self.get_ohlcv(self.ticker, interval='day', count=20)

            if candles_df is None:
                error_logger.error(f"Error fetching {self.ticker} data for moving averages calculation")
                return None

            close = candles_df['trade_price']
            self.ma_5 = close.rolling(window=5).mean().values[-1]
            self.ma_10 = close.rolling(window=10).mean().values[-1]
            self.ma_20 = close.rolling(window=20).mean().values[-1]

            # Debug log for moving averages
            debug_logger.debug(f"Moving averages for {self.ticker}: MA5={self.ma_5}, MA10={self.ma_10}, MA20={self.ma_20}")

        except Exception as e:
            error_logger.error(f"Error in getting moving averages for {self.ticker}: {e}", exc_info=True)
            await send_telegram_message(self.bot, f"Error in getting moving averages for {self.ticker}: {e}")


    def add_buy_price(self, price, volume):
        self.buy_prices.append((price, volume))


    def add_sell_price(self, price, volume):
        self.sell_prices.append((price, volume))


    def clear_prices(self):
        self.buy_prices = []
        self.sell_prices = []


    #현시가, 구매가, 수수료, 슬리피지를 감안한 stop loss시행 및 문자전송
    async def calculate_stop_loss(self):
        try:
            current_price = await self.get_current_price(self.ticker)

            if current_price is None:
                error_logger.error(f"Error fetching current price for {self.ticker}")
                return None

            if self.position_size > 0:
                # If position exists, calculate stop loss based on moving averages
                if current_price < self.ma_5:
                    stop_loss_price = self.ma_5
                elif current_price < self.ma_10:
                    stop_loss_price = self.ma_10
                else:
                    stop_loss_price = None  # No stop loss if price is above both MAs
            else:
                stop_loss_price = None  # No stop loss if no position exists

            if stop_loss_price is not None:
                # Take into account the cost of selling at the stop loss price
                cost = self.calculate_transaction_cost(stop_loss_price, self.position_size, False, MAKER_FEE, SLIPPAGE)  # Assume a taker order
                stop_loss_price -= cost

            return stop_loss_price

        except Exception as e:
            error_logger.error(f"Error in calculating stop loss price for {self.ticker}: {e}", exc_info=True)
            await send_telegram_message(self.bot, f"Error in calculating stop loss for {self.ticker}: {e}")
            return None

    # allocated balance investment strategy
    async def get_allocated_amount(self, current_price):
        try:
            # Get your account balance for the desired asset
            account_balance = await self.get_account_balance(self.base_currency)

            # Calculate the moving averages
            ma_5 = await self.get_moving_average(5)
            ma_10 = await self.get_moving_average(10)

            if ma_5 is None or ma_10 is None:
                error_logger.error(f"Error fetching moving averages for {self.ticker}")
                return None

            # Initialize allocated amount to 0
            allocated_amount = 0

            if current_price > ma_5:
                allocated_amount += account_balance * 0.5

                # Update the account balance
                account_balance *= 0.5

                if current_price > ma_10:
                    allocated_amount += account_balance

            return allocated_amount
        except Exception as e:
            error_logger.error(f"Error in getting allocated amount for {self.ticker}: {e}", exc_info=True)
            await self.handle_error(e, "getting allocated amount")
            return None

    # Excute on my own strategy
    # Buying Strategy:
    #If the ticker price is above the 5-day MA and at least two of the indicators (RSI, MACD, Bollinger Bands, TWAP) show a positive sign, buy the ticker using 50% of your current balance.
    #If the ticker price is above the 10-day MA, buy more of the ticker using 50% of your remaining balance.
    # Selling Strategy:
    #If the ticker price drops below the 5-day MA, sell half of your holding of that ticker.
    #If the ticker price drops below the 10-day MA, sell all of your holding of that ticker.
    

    # Strategy of market analysis for trading
    async def strategy(self):
        try:
            await self.get_rsi()
            await self.get_macd()
            await self.get_bollinger_band()
            await self.get_moving_averages()

            # Buy signal: RSI < 30, MACD histogram > 0, and price > lower Bollinger Band
            if self.rsi < 30 and self.macd_histogram > 0 and self.current_price > self.lower_band:
                self.buy_signal = True
            else:
                self.buy_signal = False

            # Sell signal: RSI > 70, MACD histogram < 0, and price < upper Bollinger Band
            if self.rsi > 70 and self.macd_histogram < 0 and self.current_price < self.upper_band:
                self.sell_signal = True
            else:
                self.sell_signal = False

            # Debug logs for strategy signals and indicators
            debug_logger.debug(f"Buy signal for {self.ticker}: {self.buy_signal}")
            debug_logger.debug(f"Sell signal for {self.ticker}: {self.sell_signal}")
            debug_logger.debug(f"RSI for {self.ticker}: {self.rsi}")
            debug_logger.debug(f"MACD histogram for {self.ticker}: {self.macd_histogram}")
            debug_logger.debug(f"Current price for {self.ticker}: {self.current_price}")
            debug_logger.debug(f"Lower Bollinger Band for {self.ticker}: {self.lower_band}")
            debug_logger.debug(f"Upper Bollinger Band for {self.ticker}: {self.upper_band}")

        except Exception as e:
            error_logger.error(f"Error in executing strategy for {self.ticker}: {e}", exc_info=True)
            await send_telegram_message(self.bot, f"Error in executing strategy for {self.ticker}: {e}")


    # run strategy function for all bot
    async def run_strategy_for_all_bots(bot_instances):
        while True:
            tasks = [bot.strategy() for bot in bot_instances]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(60) #with the desired interval (in seconds) between running the strategy for all bots.


    async def get_buy_signals(self):
        ma_5 = await self.get_moving_average(5)
        ma_10 = await self.get_moving_average(10)

        await self.get_rsi()
        rsi_buy_signal = self.rsi < 30

        await self.get_macd()
        macd_buy_signal = self.macd_histogram > 0 and self.previous_macd_histogram < 0

        bollinger_bands = await self.get_bollinger_bands()
        bb_buy_signal = bollinger_bands['lower_band'].iloc[-1] > self.get_current_price(self.ticker)

        return [rsi_buy_signal, macd_buy_signal, bb_buy_signal]


    async def get_sell_signals(self):
        ma_5 = await self.get_moving_average(5)
        ma_10 = await self.get_moving_average(10)

        await self.get_rsi()
        rsi_sell_signal = self.rsi > 70

        await self.get_macd()
        macd_sell_signal = self.macd_histogram < 0 and self.previous_macd_histogram > 0

        bollinger_bands = await self.get_bollinger_bands()
        bb_sell_signal = bollinger_bands['upper_band'].iloc[-1] < self.get_current_price(self.ticker)

        return [rsi_sell_signal, macd_sell_signal, bb_sell_signal]
           

    async def update_trailing_stop(self):
        current_price = await self.get_current_price(self.ticker)

        if self.trailing_stop is None:
            self.trailing_stop = current_price * (1 - self.trailing_stop_pct)

        if current_price > self.trailing_stop / (1 - self.trailing_stop_pct):
            self.trailing_stop = current_price * (1 - self.trailing_stop_pct)

        if current_price <= self.trailing_stop:
            sell_amount = self.position_size
            await self.async_sell_market_order(self.ticker, sell_amount)


    # Buy along the allocated method on MA of 5 days and 10 days each 50% of remained balance
    async def execute_buy(self, data):
        buy_amount = self.balance * 0.5
        if not await self.validate_order_amount(buy_amount):
            return

        trade_cost = self.calculate_transaction_cost(data["current_price"], buy_amount / data["current_price"], True)

        if data["current_balance"] < buy_amount + trade_cost:
            await self.handle_error(f"Insufficient balance to cover the cost of the trade. Current balance: {data['current_balance']}, Trade cost: {trade_cost}, Buy amount: {buy_amount}")
            return

        await self.async_buy_market_order(self.ticker, (buy_amount - trade_cost) / data["current_price"])

        if self.verify_order(self.ticker):
            self.balance -= buy_amount
            self.position_size += (buy_amount - trade_cost) / data["current_price"]
            await send_telegram_message(self.bot, f"Bought {self.ticker} at {data['current_price']} with {buy_amount} KRW")


    # Trade to meet minimum unit
    async def validate_order_amount(self, allocated_amount):
        market_type = self.ticker.split('-')[0]
        if market_type == "KRW" and allocated_amount < 5000:
            error_logger.error(f"Order amount {allocated_amount} is below the minimum for KRW market (5000 KRW)")
            return False
        elif market_type in ["BTC", "ETH", "DOGE"] and allocated_amount < 10000:
            error_logger.error(f"Order amount {allocated_amount} is below the minimum for {market_type} market (10000 KRW equivalent)")
            return False
        return True


    # Sell half when the price broken 5MA of Trailing Stop
    async def execute_sell_half(self, data):
        sell_amount = self.position_size * 0.5
        sell_value = sell_amount * await self.get_current_price(self.ticker)
        if not await self.validate_order_amount(sell_value):
            return

        trade_cost = self.calculate_transaction_cost(data["current_price"], sell_amount, False)

        if data["current_balance"] < sell_amount * data["current_price"] + trade_cost:
            await self.handle_error(f"Insufficient balance to cover the cost of the trade. Current balance: {data['current_balance']}, Trade cost: {trade_cost}, Sell amount: {sell_amount}")
            return

        await self.async_sell_market_order(self.ticker, sell_amount - trade_cost)

        if self.verify_order(self.ticker):
            self.balance += sell_amount * data["current_price"] - trade_cost
            self.position_size -= sell_amount
            await send_telegram_message(self.bot, f"Sold half {self.ticker} at {data['current_price']}")


    # Sell all when the price broken 10MA of Trailing Stop
    async def execute_sell_all(self, data):
        sell_amount = self.position_size
        sell_value = sell_amount * await self.get_current_price(self.ticker)
        if not await self.validate_order_amount(sell_value):
            return

        trade_cost = self.calculate_transaction_cost(data["current_price"], sell_amount, False)

        if data["current_balance"] < sell_amount * data["current_price"] + trade_cost:
            await self.handle_error(f"Insufficient balance to cover the cost of the trade. Current balance: {data['current_balance']}, Trade cost: {trade_cost}, Sell amount: {sell_amount}")
            return

        await self.async_sell_market_order(self.ticker, sell_amount - trade_cost)

        if self.verify_order(self.ticker):
            self.balance += sell_amount * data["current_price"] - trade_cost
            self.position_size = 0
            await send_telegram_message(self.bot, f"Sold all {self.ticker} at {data['current_price']}")


    # Relative Strength Index - 높을수록 상승추세크고 낮을수록 하락추세크다
    async def get_rsi(self, time_frame=14):
        try:
            candles = await self.get_async_ohlcv(self.ticker, 60)
            if candles is None:
                error_logger.error(f"Error fetching {self.ticker} data for RSI calculation")
                await send_telegram_message(self.bot, f"Error fetching {self.ticker} data for RSI calculation")
                return None

            close = candles['close']
            delta = close.diff()

            up = delta.where(delta > 0, 0)
            down = -delta.where(delta < 0, 0)

            ema_up = up.ewm(com=time_frame - 1, adjust=False).mean()
            ema_down = down.ewm(com=time_frame - 1, adjust=False).mean()

            rs = ema_up / ema_down
            self.rsi = 100 - (100 / (1 + rs.values[-1]))

            debug_logger.debug(f"RSI for {self.ticker}: {self.rsi}")

        except Exception as e:
            error_logger.exception(f"Error in getting RSI for {self.ticker}: {e}")
            await send_telegram_message(self.bot, f"Error in getting RSI for {self.ticker}: {e}")

    # Moving Average Convergence Divergence-추세의 방향과 주가움직임 분석지표
    async def get_macd(self, fast=12, slow=26, signal=9):
        try:
            candles = await self.get_async_ohlcv(self.ticker, 60)
            if candles is None:
                error_logger.error(f"Error fetching {self.ticker} data for MACD calculation")
                await send_telegram_message(self.bot, f"Error fetching {self.ticker} data for MACD calculation")
                return None

            close = candles['close']
            exp1 = close.ewm(span=fast, adjust=False).mean()
            exp2 = close.ewm(span=slow, adjust=False).mean()
            macd = exp1 - exp2
            signal_line = macd.ewm(span=signal, adjust=False).mean()
            self.macd = macd.values[-1]
            self.signal_line = signal_line.values[-1]
            self.macd_histogram = self.macd - self.signal_line

            debug_logger.debug(f"MACD for {self.ticker}: {self.macd}")
            debug_logger.debug(f"Signal line for {self.ticker}: {self.signal_line}")
            debug_logger.debug(f"MACD histogram for {self.ticker}: {self.macd_histogram}")

        except Exception as e:
            error_logger.exception(f"Error in getting MACD for {self.ticker}: {e}")
            await send_telegram_message(self.bot, f"Error in getting MACD for {self.ticker}: {e}")


    # buy_price length check해서 sel.max_position보다크면 sell_all()진행
    async def add_buy_price(self, price, volume):
        self.buy_prices.append((price, volume))
        if len(self.buy_prices) > self.max_positions:
            await self.sell_all(price)


    # Use TWAP indicator - Time Weighted Averaged Price
    async def get_twap(self, volume):
        try:
            candles = await self.get_async_ohlcv(self.ticker, 1)
            if candles is None:
                logger.error(f"Error fetching {self.ticker} data for TWAP calculation")
                await send_telegram_message(self.bot, f"Error fetching {self.ticker} data for TWAP calculation")
                return None

            # Define current_price here
            current_price = candles[-1]['close']

            orderbook = await self.get_async_orderbook(self.ticker)
            if orderbook is None:
                logger.error(f"Error fetching {self.ticker} orderbook data for TWAP calculation")
                await send_telegram_message(self.bot, f"Error fetching {self.ticker} orderbook data for TWAP calculation")
                return None

            bids = [order['bid_price'] for order in orderbook]
            asks = [order['ask_price'] for order in orderbook]
            bid_amount = sum([order['bid_size'] for order in orderbook if order['bid_price'] >= current_price])
            ask_amount = sum([order['ask_size'] for order in orderbook if order['ask_price'] <= current_price])
            twap = (sum(bids) * bid_amount + sum(asks) * ask_amount) / (bid_amount + ask_amount)
            self.twap = twap
            if self.position_size is not None and abs(self.twap - current_price) / current_price > 0.01:
                await self.sell_all(current_price)

        except Exception as e:
            logger.exception(f"Error in getting TWAP for {self.ticker}: {e}")
            await send_telegram_message(self.bot, f"Error in getting TWAP for {self.ticker}: {e}")


    # Bollinger band indicator -가격의 상대적인 높낮이와 변동성추세정보
    async def get_bollinger_bands(self, window=20, k=2):
        try:
            candles = await self.get_async_ohlcv(self.ticker, 1)
            if candles is None:
                error_logger.error(f"Error fetching {self.ticker} data for Bollinger Bands calculation")
                await send_telegram_message(self.bot, f"Error fetching {self.ticker} data for Bollinger Bands calculation")
                return None

            # Convert the candle data into a DataFrame
            df = pd.DataFrame(candles)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df.sort_index(inplace=True)

            df["MA"] = df["trade_price"].rolling(window=window).mean()
            df["STD"] = df["trade_price"].rolling(window=window).std()
            df["upper"] = df["MA"] + (df["STD"] * k)
            df["lower"] = df["MA"] - (df["STD"] * k)

            self.bollinger_bands = df.iloc[-1][["upper", "MA", "lower"]].to_dict()

            debug_logger.debug(f"Bollinger Bands for {self.ticker}: {self.bollinger_bands}")

        except Exception as e:
            error_logger.exception(f"Error in getting Bollinger Bands for {self.ticker}: {e}")
            await send_telegram_message(self.bot, f"Error in getting Bollinger Bands for {self.ticker}: {e}")


    async def execute_sell_all(self, data):
        try:
            sell_amount = self.position_size
            sell_value = sell_amount * await self.get_current_price(self.ticker)
            if not await self.validate_order_amount(sell_value):
                return

            trade_cost = self.calculate_transaction_cost(data["current_price"], sell_amount, False)

            if data["current_balance"] < sell_amount * data["current_price"] + trade_cost:
                await self.handle_error(f"Insufficient balance to cover the cost of the trade. Current balance: {data['current_balance']}, Trade cost: {trade_cost}, Sell amount: {sell_amount}")
                return

            await self.async_sell_market_order(self.ticker, sell_amount - trade_cost)

            if self.verify_order(self.ticker):
                self.balance += sell_amount * data["current_price"] - trade_cost
                self.position_size = 0
                await send_telegram_message(self.bot, f"Sold all {self.ticker} at {data['current_price']}")

        except Exception as e:
            error_logger.exception(f"Error executing sell all for {self.ticker}: {e}")
            await send_telegram_message(self.bot, f"Error executing sell all for {self.ticker}: {e}")


#Clean up Bot functions
async def cleanup(bot_instances):
    for bot_instance in bot_instances:
        await bot_instance.close()


#The main function sets up and manages the trading bot, initializing all required components, 
# fetching required data, and managing the execution of the various tasks. 
# It also handles scheduling and sending notifications via Telegram.
async def main():
    # Initialize bot_instances as an empty list
    bot_instances = []

    try:
        # Initialize bot and dispatcher
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        dp = Dispatcher(bot)

        # Define the tickers for the trading pairs
        tickers = ["KRW-BTC", "KRW-ETH", "KRW-DOGE"]

        # Create an aiohttp ClientSession
        async with aiohttp.ClientSession() as session:

            # Initialize trading bots for each trading pair
            bot_instances = [await TradingBot.create(session, ticker, bot) for ticker in tickers]

            # Send an initial notification to the Telegram group
            await send_initial_notification(bot, session)

            # Create a list of tasks to be run concurrently
            tasks = []

            # Add the strategy task for all bot instances
            tasks.append(run_strategy_for_all_bots(bot_instances))

            # Schedule daily notifications
            async def schedule_notifications():
                schedule.every().day.at("09:00").do(lambda: asyncio.create_task(send_daily_notification(bot)))
                while True:
                    await asyncio.get_running_loop().run_in_executor(None, schedule.run_pending)
                    await asyncio.sleep(5)  # Add a sleep to prevent rate limit issues

            # Add the scheduled notifications task
            tasks.append(schedule_notifications())

            # Gather all tasks and run them concurrently
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        error_logger.exception("An error occurred in main(): %s", e)

    finally:
        # Clean up the bot instances
        await cleanup(bot_instances)

    # Close the bot
    await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
















