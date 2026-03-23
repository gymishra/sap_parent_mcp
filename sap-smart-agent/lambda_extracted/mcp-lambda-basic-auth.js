const https = require('https');

// SAP credentials - in production, these should be from environment variables
const SAP_BASE_URL = process.env.SAP_BASE_URL || 'https://vhcals4hci.awspoc.club';
const SAP_USERNAME = 'gyanmis';
const SAP_PASSWORD = 'Pass2025$';

// Create Basic Auth header
const basicAuth = Buffer.from(`${SAP_USERNAME}:${SAP_PASSWORD}`).toString('base64');

// MCP Server Implementation
class MCPServer {
    constructor() {
        this.tools = [
            {
                name: "get_business_partners",
                description: "Get business partners from SAP using Basic Authentication",
                inputSchema: {
                    type: "object",
                    properties: {
                        top: {
                            type: "number",
                            description: "Number of records to return (default: 10)"
                        }
                    }
                }
            },
            {
                name: "get_business_partner_by_id",
                description: "Get a specific business partner by ID",
                inputSchema: {
                    type: "object",
                    properties: {
                        id: {
                            type: "string",
                            description: "Business Partner ID"
                        }
                    },
                    required: ["id"]
                }
            }
        ];
    }

    async handleRequest(request) {
        const { method, params } = request;

        switch (method) {
            case 'initialize':
                return {
                    protocolVersion: "2024-11-05",
                    capabilities: {
                        tools: {}
                    },
                    serverInfo: {
                        name: "sap-mcp-server",
                        version: "1.0.0"
                    }
                };

            case 'tools/list':
                return { tools: this.tools };

            case 'tools/call':
                return await this.handleToolCall(params);

            default:
                throw new Error(`Unknown method: ${method}`);
        }
    }

    async handleToolCall(params) {
        const { name, arguments: args } = params;

        switch (name) {
            case 'get_business_partners':
                return await this.getBusinessPartners(args.top || 10);
            
            case 'get_business_partner_by_id':
                return await this.getBusinessPartnerById(args.id);

            default:
                throw new Error(`Unknown tool: ${name}`);
        }
    }

    async getBusinessPartners(top = 10) {
        const url = `${SAP_BASE_URL}/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner?$top=${top}`;
        
        try {
            const data = await this.makeHttpRequest(url);
            const partners = data.d.results.map(partner => ({
                BusinessPartner: partner.BusinessPartner,
                BusinessPartnerFullName: partner.BusinessPartnerFullName,
                BusinessPartnerCategory: partner.BusinessPartnerCategory,
                Customer: partner.Customer,
                Supplier: partner.Supplier
            }));

            return {
                content: [
                    {
                        type: "text",
                        text: `Found ${partners.length} business partners:\n\n${JSON.stringify(partners, null, 2)}`
                    }
                ]
            };
        } catch (error) {
            return {
                content: [
                    {
                        type: "text",
                        text: `Error fetching business partners: ${error.message}`
                    }
                ],
                isError: true
            };
        }
    }

    async getBusinessPartnerById(id) {
        const url = `${SAP_BASE_URL}/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner('${id}')`;
        
        try {
            const data = await this.makeHttpRequest(url);
            const partner = {
                BusinessPartner: data.d.BusinessPartner,
                BusinessPartnerFullName: data.d.BusinessPartnerFullName,
                BusinessPartnerCategory: data.d.BusinessPartnerCategory,
                Customer: data.d.Customer,
                Supplier: data.d.Supplier,
                CreatedByUser: data.d.CreatedByUser,
                CreationDate: data.d.CreationDate
            };

            return {
                content: [
                    {
                        type: "text",
                        text: `Business Partner Details:\n\n${JSON.stringify(partner, null, 2)}`
                    }
                ]
            };
        } catch (error) {
            return {
                content: [
                    {
                        type: "text",
                        text: `Error fetching business partner ${id}: ${error.message}`
                    }
                ],
                isError: true
            };
        }
    }

    makeHttpRequest(url) {
        return new Promise((resolve, reject) => {
            const options = {
                headers: {
                    'Authorization': `Basic ${basicAuth}`,
                    'Accept': 'application/json',
                    'X-Requested-With': 'X'
                },
                rejectUnauthorized: false // For self-signed certificates
            };

            https.get(url, options, (res) => {
                let data = '';
                
                res.on('data', (chunk) => {
                    data += chunk;
                });
                
                res.on('end', () => {
                    try {
                        const jsonData = JSON.parse(data);
                        if (res.statusCode >= 200 && res.statusCode < 300) {
                            resolve(jsonData);
                        } else {
                            reject(new Error(`HTTP ${res.statusCode}: ${jsonData.error?.message || 'Unknown error'}`));
                        }
                    } catch (error) {
                        reject(new Error(`Failed to parse JSON: ${error.message}`));
                    }
                });
            }).on('error', (error) => {
                reject(error);
            });
        });
    }
}

// Lambda handler
exports.handler = async (event) => {
    const server = new MCPServer();
    
    try {
        // Handle both API Gateway and direct invocation
        let body;
        if (event.body) {
            body = typeof event.body === 'string' ? JSON.parse(event.body) : event.body;
        } else {
            body = event;
        }

        console.log('Received request:', JSON.stringify(body, null, 2));

        const response = await server.handleRequest(body);
        
        const result = {
            jsonrpc: "2.0",
            id: body.id,
            result: response
        };

        // Return appropriate format based on invocation type
        if (event.httpMethod) {
            // API Gateway format
            return {
                statusCode: 200,
                headers: {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type'
                },
                body: JSON.stringify(result)
            };
        } else {
            // Direct invocation format
            return result;
        }

    } catch (error) {
        console.error('Error:', error);
        
        const errorResponse = {
            jsonrpc: "2.0",
            id: event.id || null,
            error: {
                code: -32603,
                message: error.message
            }
        };

        if (event.httpMethod) {
            return {
                statusCode: 500,
                headers: {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                body: JSON.stringify(errorResponse)
            };
        } else {
            return errorResponse;
        }
    }
};
