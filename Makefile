# make check          : every test + the offline demo (no API keys, no spend)
# make demo           : the offline demo, outputs kept in runs/demo
# make install-skills : link skills/* into .claude/skills/ so Claude Code finds them
PY ?= python3
SKILL = skills/founder-email-finder

.PHONY: check test demo install-skills

check: test
	$(PY) $(SKILL)/examples/demo.py

test: export FEF_NO_DOTENV = 1
test:
	cd $(SKILL) && $(PY) -m pytest -q -p no:cacheprovider tests

demo:
	$(PY) $(SKILL)/examples/demo.py --keep

install-skills:
	mkdir -p .claude/skills
	for d in skills/*/; do n=$$(basename "$$d"); ln -sfn "../../skills/$$n" ".claude/skills/$$n"; done
	@ls .claude/skills/*/SKILL.md
