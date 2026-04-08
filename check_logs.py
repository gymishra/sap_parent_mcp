import boto3
logs = boto3.client('logs', region_name='us-east-1')
streams = logs.describe_log_streams(
    logGroupName='/aws/bedrock-agentcore/runtimes/sap_credit_memo_agent-eYb9d43l2E-DEFAULT',
    orderBy='LastEventTime', descending=True, limit=2)
for s in streams['logStreams'][:1]:
    print('Stream:', s['logStreamName'])
    events = logs.get_log_events(
        logGroupName='/aws/bedrock-agentcore/runtimes/sap_credit_memo_agent-eYb9d43l2E-DEFAULT',
        logStreamName=s['logStreamName'], limit=20, startFromHead=False)
    for e in events['events'][-15:]:
        print(e['message'].strip()[:200])
