# Accessibility audit tooling

A repeatable WCAG 2.2 AA audit of every captured state of demo.mercury.com's Admin view. One
command surface runs every stage:

```sh
python3 tools/a11y/audit.py --help          # every stage, in the order the a11y-audit skill runs them
python3 tools/a11y/audit.py status          # progress from files; run it first after any interruption
npm run a11y:test                           # unit and fixture tests
```

File formats live in [schemas.md](schemas.md). Committed audit data lives in `a11y/`.
