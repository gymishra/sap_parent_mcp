import boto3, time
c = boto3.client('logs', region_name='us-east-1')
r = c.filter_log_events(
    logGroupName='/aws/bedrock-agentcore/runtimes/sap_odata_mcp_server-5YgVMZ7INB-DEFAULT',
    filterPattern='get_sales OR token_len OR bearer OR SAP OR error',
    limit=20,
    startTime=int(time.time()*1000) - 1800000,  # last 30 min
)
events = r.get('events', [])
if not events:
    print("No matching logs in last 30 minutes")
else:
    for e in events:
        msg = e['message']
        if not msg.startswith('{'):
            print(msg[:300])
