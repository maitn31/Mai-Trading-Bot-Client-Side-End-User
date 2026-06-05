import json
import os
import sys
import time
import MetaTrader5 as mt5
import requests
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
import time
import threading


def get_app_folder():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_FOLDER = get_app_folder()
load_dotenv(os.path.join(APP_FOLDER, ".env"))

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "").rstrip("/")
CHANNEL_NAME = os.getenv("CHANNEL_USERNAME")

LOT_SIZE = float(os.getenv("LOT_SIZE", "0.01"))
GOOGLE_CLIENT_FILE = os.path.join(APP_FOLDER, "google-oauth-client.json")
SESSION_FILE = os.path.join(APP_FOLDER, "firebase_session.json")

SYMBOL_MAP = {
    "XAUUSD": os.getenv("MT5_SYMBOL_XAUUSD", "XAUUSDm")
}

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]

firebase_token = None
firebase_refresh_token = None

seen_ids = set()
initial_load_done = False


def save_firebase_session(refresh_token):
    with open(SESSION_FILE, "w", encoding="utf-8") as file:
        json.dump({"refresh_token": refresh_token}, file)


def load_firebase_session():
    if not os.path.exists(SESSION_FILE):
        return None

    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data.get("refresh_token")
    except Exception:
        return None


def refresh_firebase_login(refresh_token):
    global firebase_token, firebase_refresh_token

    url = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }

    response = requests.post(url, data=payload, timeout=20)
    data = response.json()

    if not response.ok:
        print("Saved login expired or invalid.")
        return False

    firebase_token = data["id_token"]
    firebase_refresh_token = data["refresh_token"]

    save_firebase_session(firebase_refresh_token)

    print("Firebase login restored from saved session.")
    return True


def initialize_firebase_login():
    global firebase_token, firebase_refresh_token

    if not FIREBASE_API_KEY:
        print("FIREBASE_API_KEY is missing in .env")
        return False

    if not DATABASE_URL:
        print("DATABASE_URL is missing in .env")
        return False

    if not CHANNEL_NAME:
        print("CHANNEL_USERNAME is missing in .env")
        return False

    if not os.path.exists(GOOGLE_CLIENT_FILE):
        print("google-oauth-client.json not found")
        return False

    saved_refresh_token = load_firebase_session()

    if saved_refresh_token:
        if refresh_firebase_login(saved_refresh_token):
            return True

    print("Opening Google login...")

    flow = InstalledAppFlow.from_client_secrets_file(
        GOOGLE_CLIENT_FILE,
        scopes=SCOPES
    )

    credentials = flow.run_local_server(
        port=0,
        prompt="select_account"
    )

    google_id_token = credentials.id_token

    print("Logging in to Firebase...")

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key={FIREBASE_API_KEY}"

    payload = {
        "postBody": f"id_token={google_id_token}&providerId=google.com",
        "requestUri": "http://localhost",
        "returnIdpCredential": True,
        "returnSecureToken": True
    }

    response = requests.post(url, json=payload, timeout=20)
    data = response.json()

    if not response.ok:
        print("Firebase login failed:")
        print(data)
        return False

    firebase_token = data["idToken"]
    firebase_refresh_token = data["refreshToken"]

    save_firebase_session(firebase_refresh_token)

    print("Firebase login successful")
    print("UID:", data["localId"])
    print("Email:", data.get("email"))

    return True


def initialize_mt5():
    if not mt5.initialize():
        print("MT5 initialize failed:", mt5.last_error())
        return False

    print("MT5 connected")
    return True


def get_mt5_symbol(signal_pair):
    return SYMBOL_MAP.get(signal_pair, signal_pair)


def get_pending_order_type(signal_type, entry_price, tick):
    signal_type = signal_type.upper()

    if signal_type == "BUY":
        if entry_price < tick.ask:
            return mt5.ORDER_TYPE_BUY_LIMIT
        return mt5.ORDER_TYPE_BUY_STOP

    if signal_type == "SELL":
        if entry_price > tick.bid:
            return mt5.ORDER_TYPE_SELL_LIMIT
        return mt5.ORDER_TYPE_SELL_STOP

    return None


def validate_stops(signal_type, entry_price, tp, sl):
    signal_type = signal_type.upper()

    if signal_type == "BUY":
        return sl < entry_price < tp

    if signal_type == "SELL":
        return tp < entry_price < sl

    return False


def place_order(signal_id, signal_data):
    signal_pair = signal_data.get("pair")
    signal_type = signal_data.get("type")

    if not signal_pair or not signal_type:
        print("Invalid signal data:", signal_data)
        return False

    symbol = get_mt5_symbol(signal_pair)

    entry_price = float(signal_data["entry_2"])
    tp = float(signal_data["tp1"])
    print("USING TP1 AS ORDER TP:", tp)
    print("TP2 IGNORED:", signal_data.get("tp2"))
    sl = float(signal_data["sl"])

    if not validate_stops(signal_type, entry_price, tp, sl):
        print("Invalid stops before sending order.")
        print("Type:", signal_type)
        print("Entry:", entry_price)
        print("TP:", tp)
        print("SL:", sl)
        return False

    symbol_info = mt5.symbol_info(symbol)

    if symbol_info is None:
        print("Symbol not found:", symbol)
        return False

    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            print("Failed to select symbol:", symbol)
            return False

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        print("Failed to get tick for:", symbol)
        return False

    order_type = get_pending_order_type(signal_type, entry_price, tick)

    if order_type is None:
        print("Invalid order type:", signal_type)
        return False

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": LOT_SIZE,
        "type": order_type,
        "price": entry_price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 20260529,
        "comment": f"Firebase signal {signal_id}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    print("Sending order request:")
    print(request)

    result = mt5.order_send(request)

    print("Order result:")
    print(result)

    if result is None:
        print("Order send returned None:", mt5.last_error())
        return False

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print("Order failed:", result.retcode, result.comment)
        return None

    print("Order placed successfully")
    print("Order ticket:", result.order)
    return result.order


def handle_signal(signal_id, signal_data):
    if signal_id in seen_ids:
        print("Skipped duplicate signal:", signal_id)
        return

    seen_ids.add(signal_id)

    print("New signal received:")
    print("Channel:", CHANNEL_NAME)
    print("Signal ID:", signal_id)
    print(signal_data)

    order_ticket = place_order(signal_id, signal_data)

    if order_ticket:
        symbol = get_mt5_symbol(signal_data["pair"])
        signal_type = signal_data["type"]
        tp1 = float(signal_data["tp1"])

        monitor_thread = threading.Thread(
            target=monitor_cancel_if_tp1_hit,
            args=(symbol, order_ticket, signal_type, tp1),
            daemon=True
        )

        monitor_thread.start()


def firebase_listener_event(event_data):
    global initial_load_done

    path = event_data.get("path")
    data = event_data.get("data")

    if path == "/":
        if not initial_load_done:
            print("Initial Firebase data loaded. Ignoring old signals.")

            if isinstance(data, dict):
                for signal_id in data.keys():
                    seen_ids.add(str(signal_id))

            initial_load_done = True
            print("Ready. New signals after this point will be traded.")
            return

        print("Firebase reconnected. Checking snapshot for unseen signals.")

        if isinstance(data, dict):
            for signal_id, signal_data in data.items():
                signal_id = str(signal_id)

                if signal_id not in seen_ids and isinstance(signal_data, dict):
                    handle_signal(signal_id, signal_data)

        return

    if not initial_load_done:
        return

    if data is None:
        return

    signal_id = path.strip("/")

    if "/" in signal_id:
        return

    if not isinstance(data, dict):
        return

    handle_signal(signal_id, data)


def listen_to_firebase():
    url = f"{DATABASE_URL}/signals/{CHANNEL_NAME}.json?auth={firebase_token}"

    headers = {
        "Accept": "text/event-stream"
    }

    print(f"Listening to Firebase signals/{CHANNEL_NAME}")

    response = requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=(10, None)
    )

    if response.status_code in [401, 403]:
        print("No Firebase read permission.")
        print(response.text)
        return

    if response.status_code != 200:
        print("Firebase listen failed:", response.status_code)
        print(response.text)
        return

    current_event = None

    for raw_line in response.iter_lines(chunk_size=1, decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("event:"):
            current_event = line.replace("event:", "", 1).strip()
            continue

        if line.startswith("data:"):
            data_text = line.replace("data:", "", 1).strip()

            if not data_text:
                continue

            try:
                event_data = json.loads(data_text)
            except json.JSONDecodeError:
                continue

            if current_event in ["put", "patch"]:
                firebase_listener_event(event_data)

def cancel_order(order_ticket):
    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": order_ticket,
    }

    result = mt5.order_send(request)

    print("Cancel order result:")
    print(result)

    if result is None:
        print("Cancel failed:", mt5.last_error())
        return False

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print("Cancel failed:", result.retcode, result.comment)
        return False

    print("Pending order cancelled:", order_ticket)
    return True


def monitor_cancel_if_tp1_hit(symbol, order_ticket, signal_type, tp1):
    print("Started TP1 cancel monitor for order:", order_ticket)

    signal_type = signal_type.upper()

    while True:
        time.sleep(0.5)

        orders = mt5.orders_get(ticket=order_ticket)

        if not orders:
            print("Order is no longer pending. Stop monitor:", order_ticket)
            return

        tick = mt5.symbol_info_tick(symbol)

        if tick is None:
            continue

        if signal_type == "BUY":
            current_price = tick.ask

            if current_price >= tp1:
                print("BUY pending not filled, but TP1 was reached. Cancelling order.")
                cancel_order(order_ticket)
                return

        if signal_type == "SELL":
            current_price = tick.bid

            if current_price <= tp1:
                print("SELL pending not filled, but TP1 was reached. Cancelling order.")
                cancel_order(order_ticket)
                return

def main():
    if not initialize_firebase_login():
        return

    if not initialize_mt5():
        return

    while True:
        try:
            listen_to_firebase()

            print("Firebase listener stopped. Refreshing token and reconnecting...")

            if firebase_refresh_token:
                refresh_firebase_login(firebase_refresh_token)
            else:
                initialize_firebase_login()

            time.sleep(2)

        except KeyboardInterrupt:
            print("Stopping...")
            mt5.shutdown()
            print("Stopped.")
            return

        except Exception as error:
            print("Error:", repr(error))
            print("Refreshing token and reconnecting...")

            if firebase_refresh_token:
                refresh_firebase_login(firebase_refresh_token)
            else:
                initialize_firebase_login()

            time.sleep(2)


if __name__ == "__main__":
    main()