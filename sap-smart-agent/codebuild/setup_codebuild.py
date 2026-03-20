"""
One-time setup script for the MCP Server Generator pipeline.

Creates:
  - S3 bucket for staging generated code
  - IAM role for CodeBuild
  - CodeBuild project
  - Adds required permissions to the smart agent execution role
  - Stores config in SSM
"""
import json, boto3, time
from boto3.session import Session

session = Session()
region = session.region_name
account_id = boto3.client("sts").get_caller_identity()["Account"]

CODEBUILD_PROJECT = "sap-mcp-generator"
STAGING_BUCKET = f"sap-mcp-generator-{account_id}-{region}"
CODEBUILD_ROLE_NAME = "sap-mcp-generator-codebuild-role"
SMART_AGENT_SSM_KEY = "/sap_smart_agent/agent_arn"
OKTA_DOMAIN = "trial-1053860.okta.com"
OKTA_CLIENT_ID = "0oa10vth79kZAuXGt698"

iam = boto3.client("iam")
s3 = boto3.client("s3", region_name=region)
cb = boto3.client("codebuild", region_name=region)
ssm = boto3.client("ssm", region_name=region)


def create_staging_bucket():
    print(f"Creating S3 bucket: {STAGING_BUCKET}")
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=STAGING_BUCKET)
        else:
            s3.create_bucket(Bucket=STAGING_BUCKET,
                             CreateBucketConfiguration={"LocationConstraint": region})
        s3.put_bucket_versioning(Bucket=STAGING_BUCKET,
                                 VersioningConfiguration={"Status": "Enabled"})
        print(f"  Created: {STAGING_BUCKET}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print(f"  Already exists: {STAGING_BUCKET}")


def create_codebuild_role():
    print(f"Creating IAM role: {CODEBUILD_ROLE_NAME}")
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "codebuild.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }
    try:
        role = iam.create_role(
            RoleName=CODEBUILD_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="CodeBuild role for SAP MCP server generator"
        )
        role_arn = role["Role"]["Arn"]
        print(f"  Created: {role_arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=CODEBUILD_ROLE_NAME)["Role"]["Arn"]
        print(f"  Already exists: {role_arn}")

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws/codebuild/{CODEBUILD_PROJECT}*"
            },
            {
                "Sid": "S3Staging",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{STAGING_BUCKET}", f"arn:aws:s3:::{STAGING_BUCKET}/*"]
            },
            {
                "Sid": "SSM",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter", "ssm:PutParameter"],
                "Resource": f"arn:aws:ssm:{region}:{account_id}:parameter/sap_*"
            },
            {
                "Sid": "ECR",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:CreateRepository",
                    "ecr:DescribeRepositories"
                ],
                "Resource": "*"
            },
            {
                "Sid": "AgentCore",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateAgentRuntime",
                    "bedrock-agentcore:UpdateAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:ListAgentRuntimes"
                ],
                "Resource": "*"
            },
            {
                "Sid": "IAMForExecutionRole",
                "Effect": "Allow",
                "Action": [
                    "iam:CreateRole",
                    "iam:AttachRolePolicy",
                    "iam:PassRole",
                    "iam:GetRole"
                ],
                "Resource": f"arn:aws:iam::{account_id}:role/sap-generated-*"
            }
        ]
    }
    iam.put_role_policy(
        RoleName=CODEBUILD_ROLE_NAME,
        PolicyName="sap-mcp-generator-policy",
        PolicyDocument=json.dumps(policy)
    )
    print("  Policy attached")
    return role_arn


def create_codebuild_project(role_arn):
    print(f"Creating CodeBuild project: {CODEBUILD_PROJECT}")
    # Upload buildspec to S3
    import os
    buildspec_path = os.path.join(os.path.dirname(__file__), "buildspec.yml")
    with open(buildspec_path) as f:
        buildspec_content = f.read()

    try:
        cb.create_project(
            name=CODEBUILD_PROJECT,
            description="Generates and deploys SAP MCP servers to AgentCore",
            source={
                "type": "NO_SOURCE",
                "buildspec": buildspec_content,
            },
            artifacts={"type": "NO_ARTIFACTS"},
            environment={
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_SMALL",
                "environmentVariables": [
                    {"name": "STAGING_BUCKET", "value": STAGING_BUCKET, "type": "PLAINTEXT"},
                ],
                "privilegedMode": True,  # needed for docker
            },
            serviceRole=role_arn,
            timeoutInMinutes=30,
        )
        print(f"  Created: {CODEBUILD_PROJECT}")
    except cb.exceptions.ResourceAlreadyExistsException:
        print(f"  Already exists: {CODEBUILD_PROJECT}")


def add_permissions_to_smart_agent_role():
    """Add CodeBuild + S3 permissions to the smart agent execution role so it can trigger builds."""
    print("Adding permissions to smart agent execution role...")
    # Find the execution role — it's named after the agent
    try:
        roles = iam.list_roles()["Roles"]
        agent_role = next((r for r in roles if "sap_smart_agent" in r["RoleName"]), None)
        if not agent_role:
            print("  WARNING: Could not find smart agent execution role — add manually")
            return
        role_name = agent_role["RoleName"]
        print(f"  Found role: {role_name}")

        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "TriggerCodeBuild",
                    "Effect": "Allow",
                    "Action": ["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
                    "Resource": f"arn:aws:codebuild:{region}:{account_id}:project/{CODEBUILD_PROJECT}"
                },
                {
                    "Sid": "StagingS3",
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:GetObject"],
                    "Resource": f"arn:aws:s3:::{STAGING_BUCKET}/*"
                },
                {
                    "Sid": "BedrockInvoke",
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel"],
                    "Resource": "*"
                },
                {
                    "Sid": "SSMRead",
                    "Effect": "Allow",
                    "Action": ["ssm:GetParameter", "ssm:PutParameter"],
                    "Resource": f"arn:aws:ssm:{region}:{account_id}:parameter/sap_*"
                }
            ]
        }
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="sap-mcp-generator-invoke",
            PolicyDocument=json.dumps(policy)
        )
        print("  Permissions added")
    except Exception as e:
        print(f"  ERROR: {e}")


def store_ssm_config():
    print("Storing config in SSM...")
    params = {
        "/sap_smart_agent/staging_bucket": STAGING_BUCKET,
        "/sap_smart_agent/codebuild_project": CODEBUILD_PROJECT,
        "/sap_smart_agent/okta_domain": OKTA_DOMAIN,
        "/sap_smart_agent/okta_client_id": OKTA_CLIENT_ID,
    }
    for key, value in params.items():
        ssm.put_parameter(Name=key, Value=value, Type="String", Overwrite=True)
        print(f"  {key} = {value}")


if __name__ == "__main__":
    print(f"=== SAP MCP Generator Setup ===")
    print(f"Region: {region}, Account: {account_id}\n")

    create_staging_bucket()
    role_arn = create_codebuild_role()
    time.sleep(10)  # IAM propagation
    create_codebuild_project(role_arn)
    add_permissions_to_smart_agent_role()
    store_ssm_config()

    print(f"\n=== Setup Complete ===")
    print(f"Staging bucket: {STAGING_BUCKET}")
    print(f"CodeBuild project: {CODEBUILD_PROJECT}")
    print(f"\nNext: redeploy sap_smart_mcp_server.py to pick up the new tool")
