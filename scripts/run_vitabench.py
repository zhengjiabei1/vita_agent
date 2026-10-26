"""Opt-in guarded upstream CLI, with no edits to the official checkout.

Usage: python scripts/run_vitabench.py run --domain personalization ...
"""
import vita.run as upstream_run
from vita.cli import main
from vita_agent.vitabench import make_guarded_agent_class


if __name__ == "__main__":
    upstream_run.PersonalizationAgent = make_guarded_agent_class()
    main()
