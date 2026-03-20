import boto3, time
c = boto3.client('logs', region_name='us-east-1')
r = c.filter_log_events(
    logGroupName='/aws/bedrock-agentcore/runtimes/sap_odata_mcp_server-5YgVMZ7INB-DEFAULT',
    filterPattern='STARTING OR ENV OR TOOL CALLED OR token',
    limit=30,
    startTime=int(time.time()*1000) - 3600000,
)
for e in r.get('events', []):
    msg = e['message']
    if not msg.startswith('{'):
        print(msg[:300])
if not r.get('events'):
    print("No matching logs")
