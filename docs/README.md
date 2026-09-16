# Documentation

| | |
|---|---|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | How the system is put together. Components, the `TickSource` seam, connection lifecycle, the decision pipeline, concurrency and failure models, deployment topology, module map, testing strategy. |
| **[API.md](API.md)** | Every REST endpoint, the WebSocket protocol, every message field, error codes, and the full configuration reference. |
| **[ENGINEERING.md](ENGINEERING.md)** | Why the numbers are what they are. The three decisions worth explaining, backtest validation, confidence bounds, fuel correction, and an honest account of what has and has not been verified. |

Start with [ARCHITECTURE.md](ARCHITECTURE.md) to understand the shape of the
system, then [API.md](API.md) to use it.

## Where to look for a specific thing

| question | |
|---|---|
| What messages does the stream send? | [API.md § Message protocol](API.md#message-protocol) |
| Why an interface with one implementation? | [ARCHITECTURE.md § The TickSource seam](ARCHITECTURE.md#the-ticksource-seam) |
| What happens when I connect? | [ARCHITECTURE.md § Lifecycle of a connection](ARCHITECTURE.md#lifecycle-of-a-connection) |
| How is the pit/stay decision computed? | [ARCHITECTURE.md § The decision pipeline](ARCHITECTURE.md#the-decision-pipeline) |
| Does the model actually work? | [ENGINEERING.md § Backtest validation](ENGINEERING.md#backtest-validation) |
| Why does it say "no measurable degradation"? | [ENGINEERING.md § Fuel-burn correction](ENGINEERING.md#fuel-burn-correction) |
| What breaks the stream and what doesn't? | [ARCHITECTURE.md § Failure model](ARCHITECTURE.md#failure-model) |
| How do I configure it? | [API.md § Configuration](API.md#configuration) |
| What has actually been proven? | [ENGINEERING.md § Verification status](ENGINEERING.md#verification-status) |
