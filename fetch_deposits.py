import asyncio
import aiohttp
import os
import sys
from datetime import datetime
import time
import dotenv
from dotenv import load_dotenv

load_dotenv()

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def get_usdt_rate():
    # For simplicity, assuming 1 BNB = X USDT. Fetch BNB price in USD.
    # CoinGecko API for BNB price in USD
    url = "https://api.coingecko.com/api/v3/simple/price?ids=binancecoin&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            # Assuming 1 USDT = 1 USD for conversion
            return data['binancecoin']['usd'] if 'binancecoin' in data and 'usd' in data['binancecoin'] else 1.0


async def get_token_usdt_rate(token_symbol):
    # Fetch token price in USD from CoinGecko API
    # Note: token_symbol needs to be converted to CoinGecko's ID if different
    # For common tokens like CAKE, it might be 'pancakeswap-token'
    # This is a simplified example and might need a mapping for various token symbols
    coingecko_id_map = {
        'CAKE': 'pancakeswap-token',
        'BNB': 'binancecoin',
        'ETH': 'ethereum',
        # Add more mappings as needed
    }
    coingecko_id = coingecko_id_map.get(token_symbol.upper(), token_symbol.lower())  # Default to lower case symbol

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return data[coingecko_id]['usd'] if coingecko_id in data and 'usd' in data[coingecko_id] else 1.0


async def check_new_deposits(CHECK_AMOUNT, API_KEY, ADDRESS, bnb_usdt_rate, session):
    BASE_URL = "https://api.etherscan.io/v2/api"
    CHAIN_ID = 56
    params_base = {
        "chainid": CHAIN_ID,
        "module": "account",
        "address": ADDRESS,
        "startblock": 0,
        "endblock": 99999999,
        "sort": "desc",
        "apikey": API_KEY
    }
    now = datetime.utcnow().timestamp()
    one_hour_ago = now - 3600
    # Normal txs
    normal_params = params_base.copy()
    normal_params["action"] = "txlist"
    async with session.get(BASE_URL, params=normal_params) as resp:
        data = await resp.json()
        if data.get("status") == "1":
            normal_txs = data["result"]
        else:
            print("Error fetching normal txs:", data.get("message"))
            return False
    incoming_normal = [tx for tx in normal_txs if
                       tx['to'].lower() == ADDRESS.lower() and int(tx['timeStamp']) >= one_hour_ago]
    for tx in incoming_normal:
        value_eth = int(tx['value']) / 10 ** 18  # Convert from wei to ETH
        value_usdt = value_eth * bnb_usdt_rate
        if abs(value_usdt - CHECK_AMOUNT) < 1e-6:
            print(True)
            return True
    # Token txs
    token_params = params_base.copy()
    token_params["action"] = "tokentx"
    async with session.get(BASE_URL, params=token_params) as resp:
        data = await resp.json()
        if data.get("status") == "1":
            token_txs = data["result"]
        else:
            print("Error fetching token txs:", data.get("message"))
            return False
    incoming_tokens = [tx for tx in token_txs if
                       tx['to'].lower() == ADDRESS.lower() and int(tx['timeStamp']) >= one_hour_ago]
    print("\nIncoming BEP20 Token Transfers:")
    for tx in incoming_tokens:
        if tx['tokenSymbol'].upper() == 'USDT':
            value_usdt = int(tx['value']) / (10 ** int(tx['tokenDecimal']))  # Use tokenDecimal for USDT
            print(
                f"From: {tx['from']}, Token: {tx['tokenName']}, Value: {value_usdt:.2f} USDT, Hash: {tx['hash']}, Time: {datetime.fromtimestamp(int(tx['timeStamp'])).strftime('%Y-%m-%d %H:%M:%S')}")
            # Write transaction data to file

            if abs(value_usdt - CHECK_AMOUNT) < 1e-6:
                with open('usdt_deposits.txt', 'a', encoding='utf-8') as f:
                    f.write(f"{tx['hash']};{tx['from']};{tx['to']};{value_usdt}\n")
                print(True)
                return True
        else:
            value_token = int(tx['value']) / (10 ** int(tx['tokenDecimal']))
            token_usdt_rate = await get_token_usdt_rate(tx['tokenSymbol'])
            value_usdt = value_token * token_usdt_rate
            print(
                f"From: {tx['from']}, Token: {tx['tokenName']}, Value: {value_token:.6f} {tx['tokenSymbol']} (~{value_usdt:.2f} USDT), Hash: {tx['hash']}, Time: {datetime.fromtimestamp(int(tx['timeStamp'])).strftime('%Y-%m-%d %H:%M:%S')}")
            if abs(value_usdt - CHECK_AMOUNT) < 1e-6:
                with open('usdt_deposits.txt', 'a', encoding='utf-8') as f:
                    f.write(f"{tx['hash']};{tx['from']};{tx['to']};{value_usdt}\n")
                print(True)
                return True
    return False


async def main(check_amount, address):
    API_KEY = os.getenv('API_ETHER_SCAN')
    if not API_KEY:
        print("Please set ETHERSCAN_API_KEY environment variable.")
        return False
    bnb_usdt_rate = await get_usdt_rate()
    async with aiohttp.ClientSession() as session:
        start_time = time.time()
        while time.time() - start_time < 3600:
            print(f"\nChecking at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            found = await check_new_deposits(check_amount, API_KEY, address, bnb_usdt_rate, session)
            if found:
                print("Exiting script: matching transaction found.")
                return True
            await asyncio.sleep(60)
        return False


def sync_main(amount, wallet_address):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(main(amount, wallet_address))
    finally:
        loop.close()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
