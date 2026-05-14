#!/bin/bash
# Cleanup script to remove all AWS resources

set -e

REGION="us-east-1"
FUNCTION_NAME="arbitrage-bot"

echo "=========================================="
echo "AWS Arbitrage Bot Cleanup"
echo "=========================================="

read -p "This will DELETE all bot resources. Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

echo "Deleting EventBridge rule targets..."
aws events remove-targets \
    --rule arbitrage-bot-trigger \
    --ids "1" \
    --region $REGION 2>/dev/null || true

echo "Deleting EventBridge rule..."
aws events delete-rule \
    --name arbitrage-bot-trigger \
    --region $REGION 2>/dev/null || true

echo "Deleting Lambda function..."
aws lambda delete-function \
    --function-name $FUNCTION_NAME \
    --region $REGION 2>/dev/null || true

echo "Deleting IAM role policies..."
aws iam delete-role-policy \
    --role-name arbitrage-bot-lambda-role \
    --policy-name arbitrage-bot-policy 2>/dev/null || true

echo "Detaching IAM policies..."
aws iam detach-role-policy \
    --role-name arbitrage-bot-lambda-role \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true

echo "Deleting IAM role..."
aws iam delete-role \
    --role-name arbitrage-bot-lambda-role 2>/dev/null || true

echo "Deleting SNS topic..."
SNS_TOPIC_ARN=$(aws sns list-topics --region $REGION --query "Topics[?contains(TopicArn, 'arbitrage-bot-alerts')].TopicArn" --output text)
if [ ! -z "$SNS_TOPIC_ARN" ]; then
    aws sns delete-topic --topic-arn $SNS_TOPIC_ARN --region $REGION
fi

echo "Deleting Secrets Manager secret..."
aws secretsmanager delete-secret \
    --secret-id arbitrage-bot/credentials \
    --force-delete-without-recovery \
    --region $REGION 2>/dev/null || true

read -p "Delete DynamoDB table (this will delete all trade history)? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Deleting DynamoDB table..."
    aws dynamodb delete-table \
        --table-name arbitrage-trades \
        --region $REGION 2>/dev/null || true
fi

echo ""
echo "Cleanup complete!"
echo "All resources have been deleted."
