"""
Start all AI Factory sub-agents + parent MCP server.
Usage: python start_all.py

Then point Kiro's kiro_bridge.py at http://localhost:8100/mcp
"""
import subprocess, sys, time, os

BASE   = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

agents = [
    ("ADT Agent",            "adt_agent.py",          8101),
    ("OData Agent",          "odata_agent.py",        8102),
    ("Cloud ALM Agent",      "calm_agent.py",         8103),
    ("SuccessFactors Agent", "sf_agent.py",           8104),
    ("Generator Agent",      "generator_agent.py",    8105),
    ("Parent MCP Server",    "parent_mcp_server.py",  8100),
]

procs = []
for name, script, port in agents:
    p = subprocess.Popen([PYTHON, os.path.join(BASE, script)], env={**os.environ})
    procs.append((name, p, port))
    print(f"Started {name} (pid {p.pid}) on port {port}")
    time.sleep(1)  # stagger startup

print("\nAll agents starting. Parent MCP Server on http://localhost:8100/mcp")
print("Point kiro_bridge LOCAL_MCP_URL=http://localhost:8100/mcp\n")

try:
    for name, p, _ in procs:
        p.wait()
except KeyboardInterrupt:
    print("\nShutting down...")
    for name, p, _ in procs:
        p.terminate()
