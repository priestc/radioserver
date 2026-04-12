# Claude Instructions

## After every commit

Always push every commit immediately after creating it.

Then deploy to tank2 with:

```
pipx install --force git+https://github.com/priestc/radioserver.git && ~/.local/bin/radioserver migrate && sudo systemctl restart radioserver
```
