#!/bin/bash
# Build the RadioClient iOS app and upload it to TestFlight.
#
#   bash ios/testflight.sh
#
# Signing is automatic (team 6V798W6LMZ). Uploading uses an App Store Connect API
# key: AuthKey_<KEY_ID>.p8 in ~/.appstoreconnect/private_keys/ plus an env file
# containing
#     ASC_KEY_ID=<KEY_ID>
#     ASC_ISSUER_ID=<issuer UUID>
# The env file is ~/.appstoreconnect/radioserver.env if it exists, otherwise the
# smart-home one (API keys are team-wide, so either works). With neither, the
# Apple account signed in to Xcode is used.
#
# Signing needs the login keychain unlocked. Over SSH it's locked, so this script
# unlocks it first (it asks for your Mac login password).
#
# The build number is the current date and time, so every upload is newer than
# the last. The version comes from MARKETING_VERSION in the Xcode project.
set -euo pipefail

cd "$(dirname "$0")/RadioClient"
OUT="${TMPDIR:-/tmp}/radioclient-testflight"
BUILD="$(date +%Y%m%d.%H%M)"
TEAM="6V798W6LMZ"

AUTH=()
ENV_FILE="$HOME/.appstoreconnect/radioserver.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$HOME/.appstoreconnect/smart-home.env"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    KEY="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8"
    if [[ ! -f "$KEY" ]]; then
        echo "error: $ENV_FILE names key $ASC_KEY_ID but $KEY doesn't exist" >&2
        exit 1
    fi
    AUTH=(-authenticationKeyPath "$KEY" -authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER_ID")
    echo "Using App Store Connect API key $ASC_KEY_ID (from $ENV_FILE)"
else
    echo "No App Store Connect env file; using the Apple account signed in to Xcode"
fi

# codesign can't read the signing key from a locked keychain (the normal state
# in an SSH session) and fails with errSecInteractionNotAllowed.
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
if ! security show-keychain-info "$KEYCHAIN" >/dev/null 2>&1; then
    echo "Login keychain is locked; unlocking it for code signing."
    security unlock-keychain "$KEYCHAIN"
fi

rm -rf "$OUT"
mkdir -p "$OUT"
cat > "$OUT/ExportOptions.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key><string>app-store-connect</string>
    <key>destination</key><string>upload</string>
    <key>teamID</key><string>$TEAM</string>
    <key>signingStyle</key><string>automatic</string>
    <key>uploadSymbols</key><true/>
    <key>manageAppVersionAndBuildNumber</key><false/>
</dict>
</plist>
EOF

echo "Archiving build $BUILD..."
xcodebuild -project RadioClient.xcodeproj -scheme RadioClient -configuration Release \
    -destination 'generic/platform=iOS' -archivePath "$OUT/RadioClient.xcarchive" \
    -allowProvisioningUpdates ${AUTH[@]+"${AUTH[@]}"} \
    CURRENT_PROJECT_VERSION="$BUILD" \
    archive > "$OUT/archive.log" 2>&1 || true
grep -E "error:|warning: .*(sign|provision)|ARCHIVE (SUCCEEDED|FAILED)" "$OUT/archive.log" || true
[[ -d "$OUT/RadioClient.xcarchive" ]] || { echo "error: archive failed — full log: $OUT/archive.log" >&2; exit 1; }

echo "Uploading to App Store Connect..."
xcodebuild -exportArchive -archivePath "$OUT/RadioClient.xcarchive" \
    -exportOptionsPlist "$OUT/ExportOptions.plist" -exportPath "$OUT/export" \
    -allowProvisioningUpdates ${AUTH[@]+"${AUTH[@]}"}

echo "Uploaded build $BUILD. It shows up in TestFlight after Apple finishes processing it (usually 5-15 minutes)."
