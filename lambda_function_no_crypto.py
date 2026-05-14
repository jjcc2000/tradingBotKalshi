import json
import os
import boto3
import datetime
import urllib.request
import urllib.parse
import base64
import hashlib

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

BASE_URL = "https://demo-api.kalshi.co"
API_PATH = "/trade-api/v2"


def get_kalshi_credentials():
    """Get credentials from Secrets Manager"""
    try:
        secret_name = os.environ.get('SECRET_NAME', 'arbitrage-bot/credentials')
        response = secretsmanager.get_secret_value(SecretId=secret_name)
        secrets = json.loads(response['SecretString'])
        return secrets.get('API_KEY_ID'), secrets.get('PRIVATE_KEY')
    except Exception as e:
        print(f"Error getting secrets: {e}")
        return None, None


def kalshi_request_simple(api_key_id, method, endpoint, data=None):
    """Make request to Kalshi without signature (for testing)"""
    
    url = BASE_URL + API_PATH + endpoint
    
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        if data:
            req.data = json.dumps(data).encode('utf-8')
        
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            error_body = json.loads(e.read().decode('utf-8'))
            return e.code, error_body
        except:
            return e.code, {"error": str(e)}
    except Exception as e:
        print(f"Request error: {e}")
        return None, {"error": str(e)}


def fetch_markets():
    """Fetch active markets (public endpoint, no auth needed)"""
    try:
        # Try public markets endpoint first
        status, data = kalshi_request_simple(None, 'GET', '/markets?status=open&limit=50')
        
        if status == 200:
            markets = data.get('markets', [])
            print(f"Fetched {len(markets)} markets from public endpoint")
            return markets
        
        print(f"Error fetching markets: {status} - {data}")
        return []
        
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []


def check_arbitrage(market):
    """Check if market has arbitrage opportunity"""
    try:
        # Get best bid prices
        yes_bid = market.get('yes_bid', 0) / 100.0 if market.get('yes_bid') else 0
        no_bid = market.get('no_bid', 0) / 100.0 if market.get('no_bid') else 0
        
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
            'ticker': market.get('ticker', 'UNKNOWN'),
            'title': market.get('title', market.get('ticker', 'Unknown')),
            'yes_price': yes_bid,
            'no_price': no_bid,
            'combined_price': combined,
            'expected_profit': profit,
            'profit_percent': profit * 100,
            'contracts': contracts,
            'close_time': market.get('close_time', 'unknown'),
            'volume': volume
        }
        
    except Exception as e:
        print(f"Error checking arbitrage: {e}")
        return None


def send_notification(opportunity, status):
    """Send SNS notification"""
    try:
        if not SNS_TOPIC:
            print("No SNS topic configured")
            return
        
        contracts = opportunity.get('contracts', 0)
        expected_profit = opportunity['expected_profit'] * contracts
        
        message = f"""
Kalshi Arbitrage Alert

Status: {status.upper()}
Market: {opportunity['title']}
Ticker: {opportunity['ticker']}

Prices:
  YES: ${opportunity['yes_price']:.4f}
  NO: ${opportunity['no_price']:.4f}
  Combined: ${opportunity['combined_price']:.4f}

Trade:
  Contracts: {contracts}
  Expected Profit: ${expected_profit:.2f}
  Profit %: {opportunity['profit_percent']:.2f}%
  
Volume: {opportunity['volume']}

NOTE: Bot needs funds deposited to execute trades!

Timestamp: {datetime.datetime.now().isoformat()}
        """
        
        sns.publish(
            TopicArn=SNS_TOPIC,
            Subject=f'Kalshi Arbitrage - {status.upper()}',
            Message=message
        )
        
        print(f"Notification sent via SNS")
        
    except Exception as e:
        print(f"Error sending notification: {e}")


def lambda_handler(event, context):
    """Main Lambda handler"""
    
    print(f"=== Kalshi Arbitrage Bot ===")
    print(f"Lambda invoked at {datetime.datetime.now().isoformat()}")
    
    # Get credentials (for future use when trading)
    api_key_id, private_key = get_kalshi_credentials()
    
    if not api_key_id or not private_key:
        print("WARNING: Kalshi credentials not found (needed for trading)")
    else:
        print(f"Credentials loaded for API Key: {api_key_id[:8]}...")
    
    # Fetch markets (public, no auth needed)
    print("Fetching markets...")
    markets = fetch_markets()
    print(f"Fetched {len(markets)} markets")
    
    if not markets:
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'No markets available',
                'note': 'This may be due to API endpoint changes or connectivity issues'
            })
        }
    
    # Check for arbitrage opportunities
    print("Scanning for arbitrage opportunities...")
    opportunities = []
    for market in markets:
        opp = check_arbitrage(market)
        if opp:
            opportunities.append(opp)
    
    print(f"Found {len(opportunities)} arbitrage opportunities")
    
    # Report on best opportunities
    if opportunities:
        opportunities.sort(key=lambda x: x['expected_profit'], reverse=True)
        
        print("\n=== Top Opportunities ===")
        for i, opp in enumerate(opportunities[:3], 1):
            print(f"{i}. {opp['ticker']}: ${opp['expected_profit']:.4f} profit ({opp['profit_percent']:.2f}%)")
        
        best = opportunities[0]
        
        # Send notification for best opportunity
        send_notification(best, 'found')
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Arbitrage opportunities found!',
                'total_opportunities': len(opportunities),
                'markets_scanned': len(markets),
                'best_opportunity': {
                    'ticker': best['ticker'],
                    'title': best['title'],
                    'profit_percent': round(best['profit_percent'], 2),
                    'expected_profit_dollars': round(best['expected_profit'] * best['contracts'], 2)
                },
                'note': 'Deposit funds to Kalshi to enable automatic trading'
            })
        }
    
    print("No arbitrage opportunities found")
    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'No arbitrage opportunities found',
            'markets_scanned': len(markets),
            'threshold': f'{PROFIT_THRESHOLD*100}%',
            'note': 'Bot is scanning successfully - waiting for opportunities'
        })
    }