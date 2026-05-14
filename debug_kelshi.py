#!/usr/bin/env python3
"""
Kalshi API Test - CORRECT Authentication Method
Using Kalshi's signature-based auth (not JWT!)
"""

import requests
import datetime
import base64
from dotenv import load_dotenv
import os


from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

# =============================================================================
# PASTE YOUR CREDENTIALS
# =============================================================================

load_dotenv()

API_KEY_ID = os.getenv('API_KEY_ID')


PRIVATE_KEY_PEM = "$(cat )"
# Production API
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# =============================================================================

def load_private_key(pem_string):
    """Load private key from PEM string"""
    return serialization.load_pem_private_key(
        pem_string.encode(),
        password=None,
        backend=default_backend()
    )

def create_signature(private_key, timestamp, method, path):
    """
    Create Kalshi request signature
    Format: timestamp + method + path (no query params)
    """
    # Strip query parameters
    path_without_query = path.split('?')[0]
    
    # Create message to sign
    message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
    
    # Sign with RSA-PSS
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    
    # Return base64 encoded
    return base64.b64encode(signature).decode('utf-8')

def kalshi_request(private_key, api_key_id, method, path, base_url=BASE_URL, data=None):
    """Make authenticated request to Kalshi API"""
    
    # Timestamp in milliseconds
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    
    # IMPORTANT: Sign the FULL path from root including /trade-api/v2
    # If path doesn't start with /trade-api, prepend it
    if not path.startswith('/trade-api'):
        sign_path = '/trade-api/v2' + path
    else:
        sign_path = path
    
    # Create signature with full path
    signature = create_signature(private_key, timestamp, method, sign_path)
    
    # Set headers
    headers = {
        'KALSHI-ACCESS-KEY': api_key_id,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json'
    }
    
    # Make request
    url = base_url + path
    
    if method == 'GET':
        return requests.get(url, headers=headers)
    elif method == 'POST':
        return requests.post(url, headers=headers, json=data)
    else:
        raise ValueError(f"Unsupported method: {method}")

def test_kalshi_api():
    """Test Kalshi API connection"""
    
    print("=" * 60)
    print("Testing Kalshi API (Correct Authentication)")
    print("=" * 60)
    
    # Load private key
    print("\n[1/4] Loading private key...")
    try:
        private_key = load_private_key(PRIVATE_KEY_PEM)
        print("✅ Private key loaded")
    except Exception as e:
        print(f"❌ Failed to load private key: {e}")
        return False
    
    # Test 1: Get balance
    print("\n[2/4] Testing authentication (get balance)...")
    try:
        response = kalshi_request(private_key, API_KEY_ID, 'GET', '/portfolio/balance')
        
        if response.status_code == 200:
            data = response.json()
            balance = data['balance'] / 100  # Convert cents to dollars
            print(f"✅ Authentication successful!")
            print(f"✅ Account Balance: ${balance:.2f}")
        else:
            print(f"❌ Authentication failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False
    
    # Test 2: Get events
    print("\n[3/4] Fetching active markets...")
    try:
        response = kalshi_request(private_key, API_KEY_ID, 'GET', '/events?status=open&limit=10')
        
        if response.status_code == 200:
            data = response.json()
            events = data.get('events', [])
            print(f"✅ Found {len(events)} active events")
            
            if events:
                print("\n   Sample events:")
                for event in events[:5]:
                    print(f"   • {event['title']}")
        else:
            print(f"❌ Failed: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False
    
    # Test 3: Get market details
    print("\n[4/4] Testing market data access...")
    try:
        if events:
            first_event = events[0]
            event_ticker = first_event['event_ticker']
            
            response = kalshi_request(
                private_key, 
                API_KEY_ID, 
                'GET', 
                f'/events/{event_ticker}/markets'
            )
            
            if response.status_code == 200:
                markets = response.json().get('markets', [])
                print(f"✅ Market data accessible ({len(markets)} markets)")
                
                # Check for arbitrage
                if markets:
                    sample = markets[0]
                    yes_bid = sample.get('yes_bid', 0) / 100
                    no_bid = sample.get('no_bid', 0) / 100
                    combined = yes_bid + no_bid
                    
                    print(f"\n   Sample Market: {sample['ticker']}")
                    print(f"   YES: ${yes_bid:.4f} | NO: ${no_bid:.4f}")
                    print(f"   Combined: ${combined:.4f}")
                    
                    if combined < 0.99:
                        profit = 1.0 - combined
                        print(f"   🎯 ARBITRAGE! Profit: ${profit:.4f} ({profit*100:.2f}%)")
                    else:
                        print(f"   No arbitrage (combined >= $0.99)")
            else:
                print(f"❌ Failed: {response.status_code}")
                return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False
    
    # Success!
    print("\n" + "=" * 60)
    print("🎉 ALL TESTS PASSED!")
    print("=" * 60)
    print("\n✅ Kalshi API working correctly")
    print("✅ Ready to deploy to AWS!")
    print("\nNext: Run deploy_aws_kalshi.sh")
    print()
    
    return True

if __name__ == '__main__':
    import sys
    success = test_kalshi_api()
    sys.exit(0 if success else 1)