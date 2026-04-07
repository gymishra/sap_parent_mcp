"""
Streamlit UI to test AI Factory and AP Month-End MCP servers.
Run: streamlit run sap-smart-agent/test_streamlit.py
"""
import streamlit as st
import httpx, json, base64, time, os

st.set_page_config(page_title="AI Factory MCP Tester", layout="wide")
st.title("AI Factory MCP Server Tester")

# ── Token Management ──────────────────────────────────────────────────────────
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".okta_token_cache.json")

def load_token():
    try:
        with open(CACHE) as f:
            token = json.load(f).get("access_token", "")
        if token:
            p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
            exp = json.loads(base64.b64decode(p)).get("exp", 0)
            if exp > time.time() + 60:
                return token, int(exp - time.time())
    except Exception:
        pass
    return "", 0

def get_agent_url(arn):
    enc = arn.replace(":", "%3A").replace("/", "%2F")
    return f"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"

def call_mcp(url, method, params, token):
    r = httpx.post(url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json",
                 "authorization": f"Bearer {token}",
                 "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken": token},
        timeout=180)
    for line in r.text.split("\n"):
        if line.startswith("data: "):
            return json.loads(line[6:])
    return {"raw": r.text, "status": r.status_code}

# ── Sidebar: Agent Selection ──────────────────────────────────────────────────
AGENTS = {
    "AI Factory (Research)": "arn:aws:bedrock-agentcore:us-east-1:953841955037:runtime/sap_smart_agent-9fYEiV4cnV",
    "AP Month-End Agent": "arn:aws:bedrock-agentcore:us-east-1:953841955037:runtime/sap_ap_monthend_agent-eREAig5iJD",
}

with st.sidebar:
    st.header("Configuration")
    token, ttl = load_token()
    if token:
        st.success(f"Okta Token: Valid ({ttl//60}m remaining)")
    else:
        st.error("Okta Token: Expired or missing")
        st.info("Run `python sap-smart-agent/check_token.py` to refresh")
        token = st.text_input("Paste token manually:", type="password")

    agent_name = st.selectbox("Select Agent", list(AGENTS.keys()))
    agent_arn = AGENTS[agent_name]
    agent_url = get_agent_url(agent_arn)
    st.caption(f"Runtime: `{agent_arn.split('/')[-1]}`")

# ── List Tools ────────────────────────────────────────────────────────────────
st.subheader(f"Tools: {agent_name}")

if token:
    with st.spinner("Fetching tools..."):
        resp = call_mcp(agent_url, "tools/list", {}, token)
        tools = resp.get("result", {}).get("tools", [])

    if tools:
        cols = st.columns(min(len(tools), 3))
        for i, tool in enumerate(tools):
            with cols[i % 3]:
                st.markdown(f"**{tool['name']}**")
                desc = tool.get("description", "")[:120]
                st.caption(desc)
    else:
        st.warning("No tools returned. Check agent status.")
else:
    st.warning("No token available. Cannot list tools.")
    tools = []

# ── Tool Execution ────────────────────────────────────────────────────────────
st.divider()
st.subheader("Execute Tool")

if tools and token:
    tool_names = [t["name"] for t in tools]
    selected_tool = st.selectbox("Select Tool", tool_names)

    # Get the selected tool's schema
    tool_def = next((t for t in tools if t["name"] == selected_tool), {})
    input_schema = tool_def.get("inputSchema", {})
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])

    # Build input form
    args = {}
    if properties:
        st.markdown("**Parameters:**")
        for prop_name, prop_def in properties.items():
            prop_type = prop_def.get("type", "string")
            prop_desc = prop_def.get("description", "")
            default = prop_def.get("default", "")
            is_required = prop_name in required

            label = f"{prop_name}{'*' if is_required else ''}"
            if prop_type == "integer" or prop_type == "number":
                args[prop_name] = st.number_input(label, value=int(default) if default else 0,
                                                   help=prop_desc)
            elif prop_type == "boolean":
                args[prop_name] = st.checkbox(label, value=bool(default), help=prop_desc)
            else:
                val = st.text_input(label, value=str(default) if default else "",
                                    help=prop_desc)
                if val:
                    args[prop_name] = val

    # For tools that take a "question" param, show a text area
    if "question" in properties:
        args["question"] = st.text_area("Question", value=args.get("question", ""),
                                         height=100,
                                         placeholder="e.g. Show me 2 purchase orders")

    if st.button("Execute", type="primary"):
        # Clean empty args
        clean_args = {k: v for k, v in args.items() if v != "" and v != 0}
        with st.spinner(f"Calling {selected_tool}..."):
            result = call_mcp(agent_url, "tools/call",
                              {"name": selected_tool, "arguments": clean_args}, token)

        # Display result
        content = result.get("result", {}).get("content", [])
        if content:
            for item in content:
                text = item.get("text", "")
                # Try to parse as JSON for pretty display
                try:
                    parsed = json.loads(text)
                    st.json(parsed)
                except (json.JSONDecodeError, TypeError):
                    st.markdown(text)
        elif "error" in result:
            st.error(f"Error: {json.dumps(result['error'], indent=2)}")
        else:
            st.json(result)

# ── Quick Prompts ─────────────────────────────────────────────────────────────
st.divider()
st.subheader("Quick Prompts")

quick_prompts = {
    "AI Factory (Research)": [
        ("2 Purchase Orders", "odata_agent_tool", "Show me 2 purchase orders"),
        ("Blocked Invoices", "odata_agent_tool", "Show me invoices blocked for payment"),
        ("PR → RFQ → Quotation", "odata_agent_tool", "Show me 2 purchase requisitions, their RFQs and quotations"),
        ("GR/IR Mismatch", "odata_agent_tool", "Show POs where GR qty doesn't match invoice qty"),
        ("Search Z Programs", "adt_agent_tool", "Find custom Z programs related to invoice verification"),
        ("Month-End Close", "odata_agent_tool", "Show all past-due vendor invoices grouped by company code with aging buckets"),
    ],
    "AP Month-End Agent": [
        ("Overdue Invoices", "get_overdue_vendor_invoices", None),
        ("AP Aging Summary", "get_ap_aging_summary", None),
        ("Blocked Invoices", "get_payment_blocked_invoices", None),
        ("Vendor Exposure", "get_vendor_open_exposure", None),
        ("GR/IR Mismatches", "get_grir_mismatch_invoices", None),
    ],
}

prompts = quick_prompts.get(agent_name, [])
if prompts and token:
    cols = st.columns(min(len(prompts), 3))
    for i, (label, tool, question) in enumerate(prompts):
        with cols[i % 3]:
            if st.button(label, key=f"quick_{i}"):
                args = {"question": question} if question else {}
                with st.spinner(f"Running {tool}..."):
                    result = call_mcp(agent_url, "tools/call",
                                      {"name": tool, "arguments": args}, token)
                content = result.get("result", {}).get("content", [])
                if content:
                    for item in content:
                        text = item.get("text", "")
                        try:
                            st.json(json.loads(text))
                        except (json.JSONDecodeError, TypeError):
                            st.markdown(text)
                elif "error" in result:
                    st.error(json.dumps(result.get("error", {}), indent=2))
                else:
                    st.json(result)
