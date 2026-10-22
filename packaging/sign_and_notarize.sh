#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Signs dist/ActionCheese Sync.app with Developer ID, notarizes it and staples the ticket,
# then writes dist/ActionCheese-Sync-<version>-mac.zip ready to attach to a GitHub release.
# Needs the "Developer ID Application: IMGXMEDIA SRL" certificate in the keychain and a
# notarytool profile: xcrun notarytool store-credentials actioncheese-notary --apple-id ... --team-id ZD4V8G24JK
set -eu
cd "$(dirname "$0")/.."
APP="dist/ActionCheese Sync.app"
ID="${SIGN_ID:-Developer ID Application: IMGXMEDIA SRL (ZD4V8G24JK)}"
PROFILE="${NOTARY_PROFILE:-actioncheese-notary}"
ENT="packaging/entitlements.plist"
VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
ZIP="dist/ActionCheese-Sync-$VERSION-mac.zip"

# Inside-out: every Mach-O in the bundle (deepest paths first), then the bundle itself.
find "$APP/Contents" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then printf '%s\n' "$f"; fi
done | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2- | while IFS= read -r f; do
  codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$ID" "$f"
done
codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$ID" "$APP"
codesign --verify --deep --strict "$APP"

rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
xcrun stapler staple "$APP"
spctl -a -vv "$APP"
# Re-zip so the download carries the stapled ticket.
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
shasum -a 256 "$ZIP"
