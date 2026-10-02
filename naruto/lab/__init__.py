"""The prompt lab: the self-learning loop's engine and interface.

An external agent (or the owner) runs scenarios against the bot's
configured model under candidate configurations, in sandboxes that never
touch the live bot, compares the results and, when authorized, activates a
candidate. See docs/LAB.md.

Keep this package's __init__ free of imports: the CLI client
(``python3 -m naruto.lab``) must run with only the standard library.
"""
