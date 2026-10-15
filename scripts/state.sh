#!/usr/bin/env bash
# Keeps the SQLite database between GitHub Actions runs on a single-commit branch of this repository.
#   scripts/state.sh restore   download bd.sqlite from the state branch (a missing branch = first run)
#   scripts/state.sh save      force-push bd.sqlite as the only commit on the state branch
# The branch holds your whole target list, signals and scores: keep the repository PRIVATE.
# Needs GITHUB_TOKEN (contents: write) and GITHUB_REPOSITORY; BD_DB_PATH and STATE_BRANCH optional.
set -euo pipefail
cmd="${1:?usage: state.sh restore|save}"
db="${BD_DB_PATH:-bd.sqlite}"
branch="${STATE_BRANCH:-bd-state}"
url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY}.git"
auth="$(printf 'x-access-token:%s' "${GITHUB_TOKEN:?set GITHUB_TOKEN}" | base64 -w0)"
echo "::add-mask::${auth}"
git_auth=(-c "http.extraheader=AUTHORIZATION: basic ${auth}")
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

case "$cmd" in
  restore)
    set +e; git "${git_auth[@]}" ls-remote --exit-code -q "$url" "refs/heads/${branch}" >/dev/null; rc=$?; set -e
    if [ "$rc" -eq 2 ]; then
      echo "No '${branch}' branch yet: first run, the database will be created from accounts.csv."
    elif [ "$rc" -ne 0 ]; then
      echo "Could not reach the repository (git exit ${rc}); refusing to continue with an empty database." >&2
      exit 1
    else
      git -C "$tmp" init -q
      git -C "$tmp" "${git_auth[@]}" fetch -q --depth 1 "$url" "$branch"
      git -C "$tmp" show FETCH_HEAD:bd.sqlite > "$db"
      echo "Restored ${db} from ${branch} ($(wc -c < "$db") bytes)."
    fi ;;
  save)
    [ -f "$db" ] || { echo "No ${db} to save." >&2; exit 1; }
    cp "$db" "$tmp/bd.sqlite"
    git -C "$tmp" init -q -b "$branch"
    git -C "$tmp" add bd.sqlite
    git -C "$tmp" -c user.name=bd-agent -c user.email=bd-agent@users.noreply.github.com \
        commit -q -m "bd state $(date -u +%FT%TZ)"
    git -C "$tmp" "${git_auth[@]}" push -q --force "$url" "$branch"
    echo "Saved ${db} to ${branch}." ;;
  *) echo "usage: state.sh restore|save" >&2; exit 2 ;;
esac
