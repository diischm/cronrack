#!/usr/bin/env bash
# Set the image tag in a Helm values file. Used by the "promote" job in
# .github/workflows/image.yml:
#
#   scripts/ci/promote-tag.sh <values file> <tag>
#
# It changes only the value of `tag:` directly under the top-level `image:` block and
# keeps everything else byte for byte: indentation, quotes, a trailing comment, the line
# ending, and any other `tag:` key (deeper in `image:` or in another block).
#
# The file must be tracked by git and have no uncommitted changes: after the edit,
# `git diff --numstat` must show exactly one line removed and one added, otherwise the
# file is put back and the script fails.
#
# Prints "already set" (nothing changed, exit 0) or "updated <old> -> <new>".

set -euo pipefail

die() {
  echo "promote-tag: $*" >&2
  exit 1
}

[[ $# -eq 2 ]] || die "usage: promote-tag.sh <values file> <tag>"
file=$1
tag=$2

[[ $tag =~ ^sha-[0-9a-f]{7,12}$ ]] || die "invalid tag: must match ^sha-[0-9a-f]{7,12}\$"
[[ -f $file && ! -L $file ]] || die "$file: not a regular file"

dir=$(dirname -- "$file")
name=$(basename -- "$file")
git -C "$dir" ls-files --error-unmatch -- "$name" >/dev/null 2>&1 ||
  die "$file: not tracked by git"
git -C "$dir" diff --quiet -- "$name" || die "$file: has uncommitted changes"

# Find the line of image.tag and its current value. Prints "<line number> <value>".
# Exit codes: 3 no image.tag, 4 more than one, 5 a tag line this script doesn't handle.
status=0
found=$(awk '
  { line = $0; sub(/\r$/, "", line) }
  state == 0 {
    if (line ~ /^image:[ ]*(#.*)?$/) state = 1
    next
  }
  state == 1 {
    if (line ~ /^[ ]*(#.*)?$/) next          # blank or comment line
    if (line !~ /^ /) { state = 2; next }   # the next top-level key: block ended
    match(line, /^ */)
    if (child == 0) child = RLENGTH          # indentation of image.* keys
    if (RLENGTH != child) next               # deeper: a nested key, not image.tag
    if (line ~ /^ *tag:( |$)/) { n++; nr = NR; val = line }
  }
  END {
    if (n == 0) exit 3
    if (n > 1) exit 4
    r = val
    sub(/^ *tag: */, "", r)
    q = substr(r, 1, 1)
    if (q == "\"" || q == "\047") {
      r = substr(r, 2)
      i = index(r, q)
      if (i == 0) exit 5
      old = substr(r, 1, i - 1)
      tail = substr(r, i + 1)
    } else {
      match(r, /^[A-Za-z0-9._-]*/)
      old = substr(r, 1, RLENGTH)
      tail = substr(r, RLENGTH + 1)
      if (old == "") exit 5
    }
    if (old !~ /^[A-Za-z0-9._-]*$/) exit 5
    if (tail !~ /^( +#.*)? *$/) exit 5
    print nr, old
  }
' "$file") || status=$?

case $status in
  0) ;;
  3) die "$file: no tag: key directly under a top-level image: block" ;;
  4) die "$file: more than one image.tag key" ;;
  5) die "$file: image.tag line has an unsupported format (expected tag: <value>)" ;;
  *) die "$file: could not read the file (awk exit $status)" ;;
esac

line_no=${found%% *}
old=${found#* }

if [[ $old == "$tag" ]]; then
  echo "already set"
  exit 0
fi

backup=$(mktemp)
trap 'rm -f -- "$backup"' EXIT
cp -p -- "$file" "$backup"

# Replace only the value on that one line; the prefix (indent, key, opening quote) and
# everything after the value stay. The tag was validated above, so it is safe in sed.
sed -i -E "${line_no}s/^( *tag: +[\"']?)[A-Za-z0-9._-]*/\\1${tag}/" -- "$file"

numstat=$(git -C "$dir" diff --numstat -- "$name")
if [[ $numstat != $'1\t1\t'* || $numstat == *$'\n'* ]]; then
  cp -p -- "$backup" "$file"
  die "$file: expected exactly one changed line, got: ${numstat:-no change}; file restored"
fi

echo "updated $old -> $tag"
