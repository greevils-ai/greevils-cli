"""DEPRECATED -- the single-file `Greevil` / `ExchangeProxy` agent contract is gone.

Agents now run in their own sandboxed process (no trading key) and trade by making plain HTTP
calls to the harness's loopback API at `$GREEVILS_AGENT_URL` -- no SDK to import. See
../greevils-api/workload/ARCHITECTURE.md and the working multi-file example in ./example-agent/
(entry.py + requirements.txt + indicators.py).

Package + submit:

    greevils package ./example-agent -o agent-bundle.enc     # prints AGENT_KEY + AGENT_SHA256
    greevils submit agent-bundle.enc --name my-agent
"""
