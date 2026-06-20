# Claude Instructions

## Deployment rules

Server-side code (anything under `server/`) runs exclusively on tank2, never locally on the Mac Mini. Every server-side change must be committed, pushed to GitHub, and deployed to tank2 before it can be tested. There is no local server to test against.

iOS app changes that have no server-side component do not need a commit/deploy cycle — they are tested directly from Xcode on the Mac Mini.

## After every server-side commit

Always push every commit immediately after creating it.

Then deploy to tank2 by SSHing in and running:

```
ssh tank2 "pipx install --force git+https://github.com/priestc/radioserver.git && ~/.local/bin/radioserver migrate && sudo systemctl restart radioserver"
```

`sudo systemctl restart radioserver` does not require a password on tank2 — it is configured in sudoers to allow this without prompting.

## Creating new iOS apps

Always use **xcodegen** to create Xcode projects. Never manually edit `project.pbxproj` or click through the Xcode new-project UI.

Workflow:
1. Create the directory structure and all Swift source files
2. Write a `project.yml` in the project root
3. Run `xcodegen generate` to produce the `.xcodeproj`
4. Tell the user to open the `.xcodeproj` in Xcode

`xcodegen` is installed at `/opt/homebrew/bin/xcodegen`. A typical `project.yml` for a SwiftUI iOS app:

```yaml
name: MyApp
options:
  bundleIdPrefix: com.chrispriest
  deploymentTarget:
    iOS: "17.0"
targets:
  MyApp:
    type: application
    platform: iOS
    sources: [MyApp]
    settings:
      base:
        SWIFT_VERSION: "5.0"
        PRODUCT_BUNDLE_IDENTIFIER: com.chrispriest.MyApp
    info:
      path: MyApp/Info.plist
      properties:
        UILaunchScreen: {}
```

Add `entitlements`, `dependencies`, extra `info` properties, and build settings as needed for the specific app.

## Error handling principle

Never silently swallow errors. Whenever something goes wrong — a failed network request, an unexpected API response, a caught exception — always surface it visibly in the UI so the user knows what's happening. This applies to:

- **AJAX / fetch calls**: show an error banner or message on the page if the request fails or returns a non-2xx status. Do not let `.catch()` or `try/catch` blocks silently do nothing.
- **Backend errors**: return meaningful error responses; don't swallow exceptions and return empty or stale data.
- **UI state**: if data can't be loaded, show an error state rather than leaving the UI blank or in a loading spinner forever.
