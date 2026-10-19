#!/bin/sh
# Download the AliRTC web SDK the stage page loads from /static/vendor (not redistributed in this repo).
set -e
cd "$(dirname "$0")/.."
mkdir -p static/vendor
curl -fsSL -o static/vendor/aliyun-rtc-sdk-7.1.9.js https://g.alicdn.com/apsara-media-box/imp-web-rtc/7.1.9/aliyun-rtc-sdk.js
echo "8100338351ce622a1d25e26997247e55b09135f458b90e748995b357ce26e376  static/vendor/aliyun-rtc-sdk-7.1.9.js" | shasum -a 256 -c -
