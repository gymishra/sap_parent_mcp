const https = require('https');
const { URL } = require('url');

// AgentCore config
const AGENTCORE_REGION = process.env.AWS_REGION || 'us-east-1';
const AGENTCORE_HOST = `bedrock-agentcore.${AGENTCORE_REGION}.amazonaws.com`;
const AGENT_ARN = process.env.AGENTCORE_ARN || 'arn:aws:bedrock-agentcore:us-east-1:953841955037:runtime/sap_smart_agent-9fYEiV4cnV';
const AGENT_ARN_ENCODED = encodeURIComponent(AGENT_ARN);
const AGENTCORE_PATH = `/runtimes/${AGENT_ARN_ENCODED}/invocations?qualifier=DEFAULT`;

// Okta config
const OKTA_DOMAIN = process.env.OKTA_DOMAIN || 'trial-1053860.okta.com';
const OKTA_CLIENT_ID = process.env.OKTA_CLIENT_ID || '0oa10vth79kZAuXGt698';
const OKTA_CLIENT_SECRET = process.env.OKTA_CLIENT_SECRET;

// Token cache
let cachedToken = null;
let tokenExpiry = 0;

async function getOktaToken() {
    if (cachedToken && Date.now() < tokenExpiry - 60000) {
        return cachedToken;
    }
    const creds = Buffer.from(`${OKTA_CLIENT_ID}:${OKTA_CLIENT_SECRET}`).toString('base64');
    const postData = 'grant_type=client_credentials&scope=openid';

    return new Promise((resolve, reject) => {
        const opts = {
            hostname: OKTA_DOMAIN,
            path: '/oauth2/default/v1/token',
            method: 'POST',
            headers: {
                'Authorization': `Basic ${creds}`,
                'Content-Type': 'application/x-www-form-urlencoded',
                'Content-Length': Buffer.byteLength(postData)
            }
        };
        const req = https.request(opts, (res) => {
            let data = '';
            res.on('data', c => data += c);
            res.on('end', () => {
                try {
                    const json = JSON.parse(data);
                    if (json.access_token) {
                        cachedToken = json.access_token;
                        tokenExpiry = Date.now() + (json.expires_in || 3600) * 1000;
                        resolve(cachedToken);
                    } else {
                        reject(new Error(`Okta error: ${data}`));
                    }
                } catch (e) { reject(e); }
            });
        });
        req.on('error', reject);
        req.write(postData);
        req.end();
    });
}

// Forward MCP JSON-RPC to AgentCore streamable-http
async function forwardToAgentCore(mcpRequest, token) {
    const body = JSON.stringify(mcpRequest);
    return new Promise((resolve, reject) => {
        const opts = {
            hostname: AGENTCORE_HOST,
            path: AGENTCORE_PATH,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream',
                'Authorization': `Bearer ${token}`,
                'Content-Length': Buffer.byteLength(body)
            }
        };
        const req = https.request(opts, (res) => {
            let data = '';
            res.on('data', c => data += c);
            res.on('end', () => {
                console.log(`AgentCore ${res.statusCode}: ${data.substring(0, 500)}`);
                // Handle SSE (text/event-stream) response
                if (res.headers['content-type'] && res.headers['content-type'].includes('text/event-stream')) {
                    const lines = data.split('\n');
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const parsed = JSON.parse(line.substring(6));
                                resolve(parsed);
                                return;
                            } catch (e) { /* continue */ }
                        }
                    }
                    resolve({ raw: data });
                } else {
                    try { resolve(JSON.parse(data)); }
                    catch (e) { resolve({ raw: data, status: res.statusCode }); }
                }
            });
        });
        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

exports.handler = async (event) => {
    try {
        // CORS preflight
        if (event.httpMethod === 'OPTIONS') {
            return {
                statusCode: 200,
                headers: {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, Authorization'
                },
                body: ''
            };
        }

        let body;
        if (event.body) {
            body = typeof event.body === 'string' ? JSON.parse(event.body) : event.body;
        } else {
            body = event;
        }

        console.log('MCP Request:', body.method, body.params?.name || '');

        // Get token from QuickSuite Authorization header, body, or Okta
        let token;
        const authHeader = event.headers && (event.headers['Authorization'] || event.headers['authorization']);
        if (authHeader && authHeader.startsWith('Bearer ')) {
            token = authHeader.substring(7);
        } else if (body._auth_token) {
            token = body._auth_token;
            delete body._auth_token;
        } else {
            try { token = await getOktaToken(); } catch(e) { token = ''; }
        }

        // Forward entire MCP request to AgentCore
        const agentResponse = await forwardToAgentCore(body, token);

        // Build JSON-RPC response
        const response = agentResponse.jsonrpc
            ? agentResponse  // Already a JSON-RPC response
            : { jsonrpc: "2.0", id: body.id, result: agentResponse.result || agentResponse };

        if (event.httpMethod) {
            return {
                statusCode: 200,
                headers: {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, Authorization'
                },
                body: JSON.stringify(response)
            };
        }
        return response;

    } catch (error) {
        console.error('Lambda Error:', error);
        const errResp = { jsonrpc: "2.0", id: null, error: { code: -32603, message: error.message } };
        if (event.httpMethod) {
            return {
                statusCode: 500,
                headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
                body: JSON.stringify(errResp)
            };
        }
        return errResp;
    }
};
