#!/bin/bash
# Самодостаточное приложение для архитектуры Mac, на котором выполняется сборка.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked --group build
mkdir -p .cache
.venv/bin/pyinstaller --noconfirm --clean --onedir --name bax-codex-light \
  --distpath dist/runtime --workpath .cache/pyinstaller --specpath .cache \
  --copy-metadata mcp --copy-metadata openai-codex macos/agent_entry.py >.cache/build-macos.log 2>&1
BAX_APP="$PWD/dist/Bax Codex.app"
rm -rf "$BAX_APP"
mkdir -p "$BAX_APP/Contents/MacOS" "$BAX_APP/Contents/Resources"
cp -R dist/runtime/bax-codex-light "$BAX_APP/Contents/Resources/agent"
cp -R plugins/bax-codex "$BAX_APP/Contents/Resources/plugin"
swiftc -parse-as-library -O -target "$(uname -m)-apple-macosx13.0" \
  macos/BaxCodex.swift -o "$BAX_APP/Contents/MacOS/BaxCodex"
cat > "$BAX_APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.baxassist.codex</string>
<key>CFBundleName</key><string>Бакс для Codex</string>
<key>CFBundleExecutable</key><string>BaxCodex</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.2.0</string>
<key>CFBundleVersion</key><string>2</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
# Developer ID можно передать при выпуске. Без него получается локальная тестовая сборка.
BAX_SIGN_OPTIONS=()
if [ "${BAX_MAC_SIGN_IDENTITY:--}" != "-" ]; then
  BAX_SIGN_OPTIONS=(--options runtime --timestamp)
fi
codesign --force --deep "${BAX_SIGN_OPTIONS[@]}" --sign "${BAX_MAC_SIGN_IDENTITY:--}" "$BAX_APP"
codesign --verify --deep --strict "$BAX_APP"
ditto -c -k --keepParent "$BAX_APP" "dist/bax-codex-mac-$(uname -m).zip"
if [ -n "${BAX_MAC_NOTARY_PROFILE:-}" ]; then
  xcrun notarytool submit "dist/bax-codex-mac-$(uname -m).zip" \
    --keychain-profile "$BAX_MAC_NOTARY_PROFILE" --wait
  xcrun stapler staple "$BAX_APP"
  ditto -c -k --keepParent "$BAX_APP" "dist/bax-codex-mac-$(uname -m).zip"
else
  echo "Тестовая сборка: notarization не выполнена. Для распространения нужен Developer ID."
fi
shasum -a 256 "dist/bax-codex-mac-$(uname -m).zip"
