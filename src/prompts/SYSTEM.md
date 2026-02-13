Generate an optimized system prompt for a browser automation agent
that solves 30 sequential web challenges.

CRITICAL RULES:
- NEVER include hardcoded codes, passwords, XOR keys, session storage keys,
  or any challenge-specific secrets in the prompt.
- NEVER include specific JavaScript extraction snippets that decode stored data.
- The prompt must teach GENERALIZABLE strategies (tool usage patterns, error
  recovery, DOM inspection techniques) that work for ANY challenge, not
  solutions to the specific challenge observed in the rollout.
- Focus on: when to snapshot vs evaluate, how to handle dialogs/overlays,
  efficient step completion patterns, error recovery strategies.