# Claude Instructions

## After every commit

Always push every commit immediately after creating it.

Then deploy to tank2 with:

```
pipx install --force git+https://github.com/priestc/radioserver.git && ~/.local/bin/radioserver migrate && sudo systemctl restart radioserver
```

## Error handling principle

Never silently swallow errors. Whenever something goes wrong — a failed network request, an unexpected API response, a caught exception — always surface it visibly in the UI so the user knows what's happening. This applies to:

- **AJAX / fetch calls**: show an error banner or message on the page if the request fails or returns a non-2xx status. Do not let `.catch()` or `try/catch` blocks silently do nothing.
- **Backend errors**: return meaningful error responses; don't swallow exceptions and return empty or stale data.
- **UI state**: if data can't be loaded, show an error state rather than leaving the UI blank or in a loading spinner forever.
