#!/bin/bash
set -e

echo "=========================================="
echo "Simple AWS Deployment"
echo "=========================================="

REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "Account ID: $ACCOUNT_ID"

# Get inputs
read -p "Enter your email: " USER_EMAIL
read -p "Enter Kalshi API Key ID: " API_KEY_ID

echo "Enter your private key file path (e.g., kalshi_private.key):"
read -p "File path: " KEY_FILE

if [ ! -f "$KEY_FILE" ]; then
    echo "Error: File not found: $KEY_FILE"
    exit 1
fi

PRIVATE_KEY=$(cat "$KEY_FILE")

echo ""
echo "[1/8] Creating DynamoDB table..."
aws dynamodb create-table \
    --table-name arbitrage-trades \
    --attribute-definitions \
        AttributeName=trade_id,AttributeType=S \
        AttributeName=timestamp,AttributeType=S \
    --key-schema \
        AttributeName=trade_id,KeyType=HASH \
        AttributeName=timestamp,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --region $REGION \
    2>/dev/null || echo "Table exists"

echo "[2/8] Creating SNS topic..."
SNS_TOPIC_ARN=$(aws sns create-topic \
    --name arbitrage-bot-alerts \
    --region $REGION \
    --query TopicArn \
    --output text)

echo "SNS Topic: $SNS_TOPIC_ARN"

aws sns subscribe \
    --topic-arn $SNS_TOPIC_ARN \
    --protocol email \
    --notification-endpoint $USER_EMAIL \
    --region $REGION

echo "[3/8] Storing credentials..."
# Use AWS CLI with inline JSON
aws secretsmanager create-secret \
    --name arbitrage-bot/credentials \
    --description "Kalshi credentials" \
    --secret-string "{\"API_KEY_ID\":\"$API_KEY_ID\",\"PRIVATE_KEY\":$(echo "$PRIVATE_KEY" | jq -Rs .)}" \
    --region $REGION \
    2>/dev/null || \
aws secretsmanager update-secret \
    --secret-id arbitrage-bot/credentials \
    --secret-string "{\"API_KEY_ID\":\"$API_KEY_ID\",\"PRIVATE_KEY\":$(echo "$PRIVATE_KEY" | jq -Rs .)}" \
    --region $REGION

echo "[4/8] Creating IAM role..."
aws iam create-role \
    --role-name arbitrage-bot-lambda-role \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    2>/dev/null || echo "Role exists"

aws iam attach-role-policy \
    --role-name arbitrage-bot-lambda-role \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam put-role-policy \
    --role-name arbitrage-bot-lambda-role \
    --policy-name arbitrage-bot-policy \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"dynamodb:*\"],\"Resource\":\"arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/arbitrage-trades\"},{\"Effect\":\"Allow\",\"Action\":[\"sns:Publish\"],\"Resource\":\"$SNS_TOPIC_ARN\"},{\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"arn:aws:secretsmanager:$REGION:$ACCOUNT_ID:secret:arbitrage-bot/credentials-*\"}]}"

echo "Waiting for IAM..."
sleep 10

echo "[5/8] Creating Lambda layer..."
mkdir -p /tmp/python
pip install --target /tmp/python cryptography -q
cd /tmp
zip -r pyjwt-layer.zip python -q
cd - > /dev/null

LAYER_ARN=$(aws lambda publish-layer-version \
    --layer-name pyjwt-layer \
    --zip-file fileb:///tmp/pyjwt-layer.zip \
    --compatible-runtimes python3.11 \
    --region $REGION \
    --query LayerVersionArn \
    --output text)

echo "[6/8] Packaging Lambda..."
mkdir -p /tmp/lambda-pkg
cp lambda_function.py /tmp/lambda-pkg/
cd /tmp/lambda-pkg
zip -r /tmp/lambda.zip . -q
cd - > /dev/null

echo "[7/8] Creating Lambda function..."
aws lambda create-function \
    --function-name arbitrage-bot \
    --runtime python3.11 \
    --role arn:aws:iam::$ACCOUNT_ID:role/arbitrage-bot-lambda-role \
    --handler lambda_function.lambda_handler \
    --zip-file fileb:///tmp/lambda.zip \
    --timeout 30 \
    --memory-size 256 \
    --layers $LAYER_ARN \
    --region $REGION \
    --environment "Variables={TRADES_TABLE=arbitrage-trades,SNS_TOPIC_ARN=$SNS_TOPIC_ARN,SECRET_NAME=arbitrage-bot/credentials,PROFIT_THRESHOLD=0.01,MAX_PRICE_THRESHOLD=0.99,MAX_POSITION_SIZE=50.0,FEE_RATE=0.01}" \
    2>/dev/null || \
aws lambda update-function-code \
    --function-name arbitrage-bot \
    --zip-file fileb:///tmp/lambda.zip \
    --region $REGION

echo "[8/8] Creating EventBridge rule..."
aws events put-rule \
    --name arbitrage-bot-trigger \
    --schedule-expression "rate(1 minute)" \
    --region $REGION

aws lambda add-permission \
    --function-name arbitrage-bot \
    --statement-id eventbridge \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn arn:aws:events:$REGION:$ACCOUNT_ID:rule/arbitrage-bot-trigger \
    --region $REGION \
    2>/dev/null || true

aws events put-targets \
    --rule arbitrage-bot-trigger \
    --targets "Id"="1","Arn"="arn:aws:lambda:$REGION:$ACCOUNT_ID:function:arbitrage-bot" \
    --region $REGION

echo ""
echo "í¾‰ Deployment Complete!"
echo ""
echo "Check your email and confirm SNS subscription!"
echo ""
echo "View logs:"
echo "  aws logs tail /aws/lambda/arbitrage-bot --follow --region $REGION"
echo ""
