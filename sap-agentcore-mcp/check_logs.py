import boto3, time
c = boto3.client('logs', region_name='us-east-1')
r = c.filter_log_events(
    logGroupName='/aws/bedrock-agentcore/runtimes/sap_odata_mcp_server-5YgVMZ7INB-DEFAULT',
    filterPattern='get_sales OR CallTool OR ListTool OR error OR token_len',
    limit=10,
    startTime=int(time.time()*1000) - 600000,
)
events = r.get('events', [])
if not events:
    print("No matching logs in last 10 minutes")
else:
    for e in events:
        print(e['message'][:300])
