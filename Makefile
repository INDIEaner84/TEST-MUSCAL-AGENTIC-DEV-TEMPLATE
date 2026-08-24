.PHONY: ai-validate ai-test ai-status

ai-validate:
	python3 scripts/ai/protocol.py validate

ai-test:
	python3 -m unittest discover -s scripts/ai/tests -v

ai-status:
	python3 scripts/ai/protocol.py status
