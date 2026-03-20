import boto3
client = boto3.client('bedrock-agentcore-control', region_name='us-east-1')
resp = client.list_gateways(maxResults=100)
items = resp.get('items', [])
if not items:
    print("No gateways found")
else:
    for gw in items:
        print(f"Name: {gw['name']}, ID: {gw['gatewayId']}, Status: {gw['status']}")
