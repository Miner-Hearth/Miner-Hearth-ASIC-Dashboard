#!/bin/sh
# Fill in the placeholders of the Miner Hearth Umbrel / 5tratumOS store before you push it.
#   sh prepare.sh <github-user> <store-repo> <main-repo> <image-digest>
# Example: sh prepare.sh alice miner-hearth-store Miner-Hearth sha256:0123abcd...
# The image digest is printed in the summary of the "docker" GitHub Actions run of the main repo.
set -eu
[ $# -eq 4 ] || { echo "usage: sh prepare.sh <github-user> <store-repo> <main-repo> <image-digest>"; exit 2; }
user=$1; store=$2; repo=$3; digest=${4#sha256:}
lower=$(printf %s "$user" | tr '[:upper:]' '[:lower:]')
case "$digest" in *[!0-9a-f]*|"") echo "the digest must be sha256:<64 hex characters>"; exit 2;; esac
[ ${#digest} -eq 64 ] || { echo "the digest must be sha256:<64 hex characters>"; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
for f in $(find "$here" -name '*.yml' -o -name '*.md' | grep -v '/README'); do
  sed -i.bak -e "s#ghcr.io/<github-user>/#ghcr.io/$lower/#g" -e "s#<github-user>#$user#g" -e "s#<store-repo>#$store#g" \
             -e "s#<repo>#$repo#g" -e "s#<image-digest>#$digest#g" "$f" && rm -f "$f.bak"
done
echo "Done. Check: grep -rn '<' --include='*.yml' $here"
