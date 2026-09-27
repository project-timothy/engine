"""The auditor's own external clients: deliberate, minimal duplicates.

Independence principle 1 forbids deciding truth with the engine's code, so
the auditor carries its own tiny QBO fetch and Graph listing. Both are
strictly read-only by construction — the only POST either ever issues is
the OAuth token exchange — and both take injectable transports so evals
never open a socket.
"""
