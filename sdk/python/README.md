# unimem-client

Thin, zero-dependency Python HTTP client for connecting agents, LLMs, and robot fleets to a hosted **UniMem Hub** or compiled local **UniMem Gateway** over HTTP/HTTPS.

Licensed under `Apache-2.0`. Contains no local runtime or proprietary engine code.

```python
from unimem_client import RemoteMemoryClient

client = RemoteMemoryClient("https://api.unimem.example", token="your-api-key", agent_id="agent-1")
ctx = client.reflect("How do we safely recover from actuator fault E42?")
client.learning.outcome(ctx["occasion_id"], succeeded=True)
```
