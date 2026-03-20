"""Add JWT authorizer to API Gateway for Okta token validation."""
import boto3

apigw = boto3.client("apigatewayv2", region_name="us-east-1")
api_id = "e5kf8uwtaf"

# Create JWT authorizer using Okta OIDC
auth = apigw.create_authorizer(
    ApiId=api_id,
    AuthorizerType="JWT",
    Name="okta-jwt-authorizer",
    IdentitySource=["$request.header.Authorization"],
    JwtConfiguration={
        "Audience": ["0oa10vth79kZAuXGt698"],
        "Issuer": "https://trial-1053860.okta.com/oauth2/default",
    },
)
authorizer_id = auth["AuthorizerId"]
print(f"Created authorizer: {authorizer_id}")

# Attach to the POST /mcp route
routes = apigw.get_routes(ApiId=api_id).get("Items", [])
for route in routes:
    if route.get("RouteKey") == "POST /mcp":
        apigw.update_route(
            ApiId=api_id,
            RouteId=route["RouteId"],
            AuthorizationType="JWT",
            AuthorizerId=authorizer_id,
        )
        print(f"Attached authorizer to route: {route['RouteId']}")
        break

print("Done - API Gateway now rejects requests without a valid Okta JWT")
