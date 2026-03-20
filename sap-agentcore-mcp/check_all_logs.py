import boto3, time
c = boto3.client('logs', region_name='us-east-1')
# Get ALL recent logs, not filtered
r = c.filter_log_events(
    logGroupName='/aws/bedrock-agentcore/runtimes/sap_odata_mcp_server-5YgVMZ7INB-DEFAULT',
    limit=20,
    startTime=int(time.time()*1000) - 600000,
    interleaved=True,
)
events = r.get('events', [])
if not events:
    print("No logs at all in last 10 minutes")
else:
    for e in events:
        msg = e['message']
        # Skip verbose otel JSON, show readable logs
        if '"body":"Processing request of type' in msg:
            import json
            try:
                d = json.loads(msg)
                print(f"  {d['body']}")
            except:
                print(msg[:200])
        elif '"body":' in msg and 'Terminating' in msg:
            pass  # skip session termination noise
        else:
            print(msg[:300])
