"""
Kalshi Arbitrage Bot - AWS Lambda Function
Uses Kalshi's signature-based authentication
"""

import json
import os
import boto3
import datetime
import base64
from typing import Dict, List, Optional
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

# AWS clients
dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
secretsmanager = boto3.client('secretsmanager')

# Configuration
TABLE_NAME = os.environ.get('TRADES_TABLE', 'arbitrage-trades')
SNS_TOPIC = os.environ.get('SNS_TOPIC_ARN', '')
PROFIT_THRESHOLD = float(os.environ.get('PROFIT_THRESHOLD', '0.01'))
MAX_PRICE_THRESHOLD = float(os.environ.get('MAX_PRICE_THRESHOLD', '0.99'))
MAX_POSITION_SIZE = float(os.environ.get('MAX_POSITION_SIZE', '50.0'))
FEE_RATE = float(os.environ.get('FEE_RATE', '0.01'))

# Kalshi API
BASE_URL = "https://api.elections.kalshi.com"
API_PATH = "/trade-api/v2"

try:
    import urllib3
    http = urllib3.PoolManager()
    USE_URLLIB3 = True
except ImportError:
    USE_URLLIB3 = False


def get_kalshi_credentials():
    """Get credentials from Secrets Manager"""
    try:
        secret_name = os.environ.get('SECRET_NAME', 'arbitrage-bot/credentials')
        response = secretsmanager.get_secret_value(SecretId=secret_name)
        secrets = json.loads(response['SecretString'])
        return secrets.get('API_KEY_ID'), secrets.get('PRIVATE_KEY')
    except Exception as e:
        print(f"Error getting secrets: {e}")
        return os.environ.get('API_KEY_ID'), os.environ.get('PRIVATE_KEY')


def load_private_key(pem_string):
    """Load private key from PEM string"""
    return serialization.load_pem_private_key(
        pem_string.encode(),
        password=None,
        backend=default_backend()
    )


def create_signature(private_key, timestamp, method, path):
    """Create Kalshi request signature"""
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
    """Make authenticated request to Kalshi"""
    
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    
    # Full path for signing
    sign_path = API_PATH + endpoint
    signature = create_signature(private_key, timestamp, method, sign_path)
    
    headers = {
        'KALSHI-ACCESS-KEY': api_key_id,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }
    
    url = BASE_URL + API_PATH + endpoint
    
    if USE_URLLIB3:
        if method == 'GET':
            response = http.request('GET', url, headers=headers)
        else:
            body = json.dumps(data).encode('utf-8') if data else None
            response = http.request('POST', url, body=body, headers=headers)
        
        return response.status, json.loads(response.data.decode('utf-8'))
    else:
        import urllib.request
        import urllib.error
        
        req = urllib.request.Request(url, headers=headers, method=method)
        if data:
            req.data = json.dumps(data).encode('utf-8')
        
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))


def fetch_markets(private_key, api_key_id):
    """Fetch active markets"""
    try:
        # Get events
        status, data = kalshi_request(
            private_key, 
            api_key_id, 
            'GET', 
            '/events?status=open&limit=50'
        )
        
        if status != 200:
            print(f"Error fetching events: {status}")
            return []
        
        events = data.get('events', [])
        
        # Get markets for first 20 events
        all_markets = []
        for event in events[:20]:
            event_ticker = event['event_ticker']
            
            status, data = kalshi_request(
                private_key,
                api_key_id,
                'GET',
                f'/events/{event_ticker}/markets'
            )
            
            if status == 200:
                markets = data.get('markets', [])
                all_markets.extend(markets)
        
        return all_markets
        
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []


def check_arbitrage(market):
    """Check if market has arbitrage opportunity"""
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
            'contracts': contracts,
            'close_time': market.get('close_time', 'unknown')
        }
        
    except Exception as e:
        print(f"Error checking arbitrage: {e}")
        return None


def execute_trade(opportunity, private_key, api_key_id):
    """Execute arbitrage trade"""
    try:
        ticker = opportunity['ticker']
        contracts = opportunity['contracts']
        
        print(f"Executing: {ticker}")
        print(f"  YES: ${opportunity['yes_price']:.4f}")
        print(f"  NO: ${opportunity['no_price']:.4f}")
        print(f"  Contracts: {contracts}")
        
        # Place YES order
        yes_order = {
            'ticker': ticker,
            'action': 'buy',
            'side': 'yes',
            'count': contracts,
            'type': 'market'
        }
        
        status, yes_result = kalshi_request(
            private_key,
            api_key_id,
            'POST',
            '/portfolio/orders',
            yes_order
        )
        
        if status != 200:
            print(f"YES order failed: {status}")
            return False
        
        # Place NO order
        no_order = {
            'ticker': ticker,
            'action': 'buy',
            'side': 'no',
            'count': contracts,
            'type': 'market'
        }
        
        status, no_result = kalshi_request(
            private_key,
            api_key_id,
            'POST',
            '/portfolio/orders',
            no_order
        )
        
        if status != 200:
            print(f"NO order failed: {status}")
            return False
        
        # Log trade
        log_trade(opportunity, {
            'yes_order': yes_result,
            'no_order': no_result
        }, 'success')
        
        send_notification(opportunity, 'success')
        
        return True
        
    except Exception as e:
        print(f"Error executing trade: {e}")
        log_trade(opportunity, {'error': str(e)}, 'failed')
        return False


def log_trade(opportunity, result, status):
    """Log trade to DynamoDB"""
    try:
        table = dynamodb.Table(TABLE_NAME)
        
        contracts = opportunity.get('contracts', 0)
        expected_profit_dollars = opportunity['expected_profit'] * contracts
        
        item = {
            'trade_id': f"{opportunity['ticker']}-{int(datetime.datetime.now().timestamp())}",
            'timestamp': datetime.datetime.now().isoformat(),
            'ticker': opportunity['ticker'],
            'title': opportunity['title'][:100],
            'yes_price': str(opportunity['yes_price']),
            'no_price': str(opportunity['no_price']),
            'combined_price': str(opportunity['combined_price']),
            'contracts': str(contracts),
            'expected_profit': str(expected_profit_dollars),
            'profit_percent': str(opportunity['profit_percent']),
            'status': status,
            'result': json.dumps(result)[:1000]
        }
        
        table.put_item(Item=item)
        print(f"Trade logged: {status}")
        
    except Exception as e:
        print(f"Error logging trade: {e}")


def send_notification(opportunity, status):
    """Send SNS notification"""
    try:
        if not SNS_TOPIC:
            return
        
        contracts = opportunity.get('contracts', 0)
        expected_profit = opportunity['expected_profit'] * contracts
        
        message = f"""
🎯 Kalshi Arbitrage Alert

Status: {status.upper()}
Market: {opportunity['title']}

Prices:
  YES: ${opportunity['yes_price']:.4f}
  NO: ${opportunity['no_price']:.4f}
  Combined: ${opportunity['combined_price']:.4f}

Trade:
  Contracts: {contracts}
  Expected Profit: ${expected_profit:.2f}
  Profit %: {opportunity['profit_percent']:.2f}%

Timestamp: {datetime.datetime.now().isoformat()}
        """
        
        sns.publish(
            TopicArn=SNS_TOPIC,
            Subject=f'Kalshi Arbitrage - {status.upper()}',
            Message=message
        )
        
    except Exception as e:
        print(f"Error sending notification: {e}")


def lambda_handler(event, context):
    """Main Lambda handler"""
    
    print(f"Lambda invoked at {datetime.datetime.now().isoformat()}")
    
    # Get credentials
    api_key_id, private_key_pem = get_kalshi_credentials()
    
    if not api_key_id or not private_key_pem:
        return {
            'statusCode': 400,
            'body': json.dumps('Missing Kalshi credentials')
        }
    
    # Load private key
    try:
        private_key = load_private_key(private_key_pem)
    except Exception as e:
        print(f"Error loading private key: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'Failed to load private key: {str(e)}')
        }
    
    # Fetch markets
    markets = fetch_markets(private_key, api_key_id)
    print(f"Fetched {len(markets)} markets")
    
    if not markets:
        return {
            'statusCode': 200,
            'body': json.dumps('No markets available')
        }
    
    # Check for arbitrage
    opportunities = []
    for market in markets:
        opp = check_arbitrage(market)
        if opp:
            opportunities.append(opp)
    
    print(f"Found {len(opportunities)} opportunities")
    
    # Execute best opportunity
    if opportunities:
        opportunities.sort(key=lambda x: x['expected_profit'], reverse=True)
        best = opportunities[0]
        
        print(f"Executing: {best['title']}")
        success = execute_trade(best, private_key, api_key_id)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Trade executed' if success else 'Trade failed',
                'opportunity': best,
                'total_opportunities': len(opportunities)
            })
        }
    
    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'No trades executed',
            'opportunities_found': len(opportunities),
            'markets_scanned': len(markets)
        })
    }
