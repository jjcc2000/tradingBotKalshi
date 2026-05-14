import json
import os
import boto3
import datetime
import base64
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

# AWS clients
dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
secretsmanager = boto3.client('secretsmanager')

# Config
TABLE_NAME = os.environ.get('TRADES_TABLE', 'arbitrage-trades')
SNS_TOPIC = os.environ.get('SNS_TOPIC_ARN', '')
PROFIT_THRESHOLD = float(os.environ.get('PROFIT_THRESHOLD', '0.01'))
MAX_PRICE_THRESHOLD = float(os.environ.get('MAX_PRICE_THRESHOLD', '0.99'))
MAX_POSITION_SIZE = float(os.environ.get('MAX_POSITION_SIZE', '50.0'))
FEE_RATE = float(os.environ.get('FEE_RATE', '0.01'))

BASE_URL = "https://api.elections.kalshi.com"
API_PATH = "/trade-api/v2"


def get_credentials():
    try:
        secret_name = os.environ.get('SECRET_NAME', 'arbitrage-bot/credentials')
        response = secretsmanager.get_secret_value(SecretId=secret_name)
        secrets = json.loads(response['SecretString'])
        return secrets.get('API_KEY_ID'), secrets.get('PRIVATE_KEY')
    except Exception as e:
        print(f"Error getting secrets: {e}")
        return None, None


def load_private_key(pem_string):
    return serialization.load_pem_private_key(
        pem_string.encode(),
        password=None,
        backend=default_backend()
    )


def create_signature(private_key, timestamp, method, path):
    path_without_query = path.split('?')[0]
    message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
    
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    
    return base64.b64encode(signature).decode('utf-8')


def kalshi_request(private_key, api_key_id, method, endpoint, data=None):
    import urllib.request
    
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    sign_path = API_PATH + endpoint
    signature = create_signature(private_key, timestamp, method, sign_path)
    
    headers = {
        'KALSHI-ACCESS-KEY': api_key_id,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }
    
    url = BASE_URL + API_PATH + endpoint
    body = None
    
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        if data:
            req.data = json.dumps(data).encode('utf-8')
        
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read().decode('utf-8')
            print(f"[{method} {endpoint}] Status: {response.status}")
            print(f"Raw body (first 300 chars): {body[:300]}")
            
            parsed = json.loads(body)
            return response.status, parsed
            
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        print(f"HTTP Error {e.code}: {body[:300]}")
        try:
            return e.code, json.loads(body)
        except:
            return e.code, {"error": body}
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}")
        print(f"Failed to parse: {body[:500] if body else 'No body'}")
        return None, {"error": "Invalid JSON", "raw": body[:200] if body else ""}
    except Exception as e:
        print(f"Request exception: {type(e).__name__}: {e}")
        return None, {"error": str(e)}


def fetch_markets(private_key, api_key_id):
    try:
        # Usar el endpoint de markets directamente
        status, data = kalshi_request(private_key, api_key_id, 'GET', '/markets?status=open&limit=100')
        
        print(f"Markets endpoint - Status: {status}")
        
        if status != 200:
            print(f"Error fetching markets: {status} - {data}")
            return []
        
        if not isinstance(data, dict):
            print(f"ERROR: Response is not a dict, it's {type(data)}")
            return []
        
        markets = data.get('markets', [])
        print(f"Found {len(markets)} markets")
        
        return markets
        
    except Exception as e:
        print(f"Exception in fetch_markets: {e}")
        import traceback
        traceback.print_exc()
        return []

def check_arbitrage(market):
    try:
        yes_bid = market.get('yes_bid', 0) / 100.0
        no_bid = market.get('no_bid', 0) / 100.0
        
        if yes_bid <= 0 or no_bid <= 0:
            return None
        
        combined = yes_bid + no_bid
        effective_price = combined + (FEE_RATE * 2)
        
        if effective_price >= MAX_PRICE_THRESHOLD:
            return None
        
        profit = 1.0 - effective_price
        
        if profit < PROFIT_THRESHOLD:
            return None
        
        volume = market.get('volume', 0)
        if volume < 100:
            return None
        
        contracts = min(int(MAX_POSITION_SIZE), volume // 10)
        
        return {
            'ticker': market['ticker'],
            'title': market.get('title', market['ticker']),
            'yes_price': yes_bid,
            'no_price': no_bid,
            'combined_price': combined,
            'expected_profit': profit,
            'profit_percent': profit * 100,
            'contracts': contracts
        }
        
    except Exception as e:
        print(f"Error checking arbitrage: {e}")
        return None


def send_notification(opportunity):
    try:
        if not SNS_TOPIC:
            return
        
        contracts = opportunity.get('contracts', 0)
        expected_profit = opportunity['expected_profit'] * contracts
        
        message = f"""
Kalshi Arbitrage Alert!

Market: {opportunity['title']}
Ticker: {opportunity['ticker']}

Prices:
  YES: ${opportunity['yes_price']:.4f}
  NO: ${opportunity['no_price']:.4f}
  Combined: ${opportunity['combined_price']:.4f}

Expected Profit: ${expected_profit:.2f} ({opportunity['profit_percent']:.2f}%)
Contracts: {contracts}

Timestamp: {datetime.datetime.now().isoformat()}
        """
        
        sns.publish(
            TopicArn=SNS_TOPIC,
            Subject='Kalshi Arbitrage Opportunity!',
            Message=message
        )
        
    except Exception as e:
        print(f"Error sending notification: {e}")


def lambda_handler(event, context):
    print(f"=== Kalshi Arbitrage Bot ===")
    print(f"Lambda invoked at {datetime.datetime.now().isoformat()}")
    
    api_key_id, private_key_pem = get_credentials()
    
    if not api_key_id or not private_key_pem:
        return {'statusCode': 400, 'body': json.dumps('Missing credentials')}
    
    print(f"API Key: {api_key_id[:8]}...")
    
    try:
        private_key = load_private_key(private_key_pem)
        print("Private key loaded successfully")
    except Exception as e:
        print(f"Error loading private key: {e}")
        return {'statusCode': 500, 'body': json.dumps(f'Failed to load key: {str(e)}')}
    
    markets = fetch_markets(private_key, api_key_id)
    print(f"Fetched {len(markets)} markets")
    
    if not markets:
        return {'statusCode': 200, 'body': json.dumps('No markets available')}
    
    opportunities = []
    for market in markets:
        opp = check_arbitrage(market)
        if opp:
            opportunities.append(opp)
    
    print(f"Found {len(opportunities)} opportunities")
    
    if opportunities:
        opportunities.sort(key=lambda x: x['expected_profit'], reverse=True)
        best = opportunities[0]
        
        print(f"Best: {best['title']} - ${best['expected_profit']:.4f} profit")
        
        send_notification(best)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Opportunities found!',
                'best_opportunity': best,
                'total_opportunities': len(opportunities),
                'markets_scanned': len(markets)
            })
        }
    
    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'No opportunities found',
            'markets_scanned': len(markets)
        })
    }