---
name: Bug report
about: Something doesn't work the way the docs say it should
labels: [bug]
---

## What happened

<!-- A clear, concise description of the bug. -->

## What did you expect to happen?

<!-- e.g. "advisor plan should print the ranking" -->

## Reproduction

```bash
# paste the exact command(s)
advisor <subcommand> <args>
```

## Environment

- advisor version: `<output of advisor --version>`
- Python: `<output of python --version>`
- OS: macOS / Linux / Windows (specify distro/version)
- Terminal: (iTerm2 / Terminal.app / Windows Terminal / VS Code / etc.)

## Relevant logs

<details>
<summary>Click to expand</summary>

```
<paste stdout + stderr here>
```

</details>

## Have you checked?

- [ ] The bug is reproducible with `NO_COLOR=1` set (rules out an
      ANSI-styling issue)
- [ ] The bug reproduces after `pip install -U advisor-agent`
- [ ] I searched existing issues for duplicates
