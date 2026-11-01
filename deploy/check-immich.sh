#!/usr/bin/env bash
# deploy/check-immich.sh — READ-ONLY look at the Immich install, for the
# phone app's sync error
#   "UNIQUE constraint failed: remote_asset_entity.owner_id, remote_asset_entity.checksum"
# (the server sent a photo whose owner + checksum the phone already has
# under another asset id).
#
#   bash /opt/vb/deploy/check-immich.sh [CHECKSUM | ID ...]
#
# CHECKSUM: the base64 value from the app's error (e.g. LP8ruMn...=), to
# look up that photo. ID: a photo or album id (uuid) from an error, e.g.
# "FOREIGN KEY constraint failed ... remote_album_asset_entity". Changes nothing: docker ps/logs/inspect, an HTTP GET
# for the version, and SELECTs in a read-only database transaction.
# The database password is never printed.
set -u
DIR="${IMMICH_DIR:-/opt/immich}"
ARGS=("$@")
h() { printf '\n===== %s =====\n' "$*"; }

h "Containers"
docker ps -a --filter name=immich --format '{{.Names}}\t{{.Image}}\t{{.Status}}'

h "Server version"
curl -s -m 10 http://127.0.0.1:2283/api/server/version; echo
grep -E '^\s*(IMMICH_VERSION|UPLOAD_LOCATION|DB_DATA_LOCATION|DB_USERNAME|DB_DATABASE_NAME)=' "$DIR/.env" 2>/dev/null \
  || echo "(no $DIR/.env; set IMMICH_DIR=/path/to/immich)"

h "Disk space"
df -h / "$(sed -n 's/^\s*UPLOAD_LOCATION=//p' "$DIR/.env" 2>/dev/null | head -1)" 2>/dev/null | uniq

h "Server errors, last 48 h (newest 40)"
docker logs --since 48h immich_server 2>&1 | grep -iE 'error|exception|fail' | tail -40

# ---- database (read-only transaction)
DBU="$(sed -n 's/^\s*DB_USERNAME=//p' "$DIR/.env" 2>/dev/null | head -1)"; DBU="${DBU:-postgres}"
DBN="$(sed -n 's/^\s*DB_DATABASE_NAME=//p' "$DIR/.env" 2>/dev/null | head -1)"; DBN="${DBN:-immich}"
PG="$(docker ps --format '{{.Names}}' | grep -E '^immich_postgres$|immich.*(postgres|database)' | head -1)"
if [ -z "$PG" ]; then
  h "Database"; echo "No running Immich database container found."; exit 0
fi
sql() {  # one read-only query, aligned output
  docker exec -i "$PG" psql -U "$DBU" -d "$DBN" -X -q -P pager=off -v ON_ERROR_STOP=1 \
    -c "BEGIN TRANSACTION READ ONLY;" -c "$1" -c "ROLLBACK;" 2>&1
}
# Immich renamed "assets" to "asset" in 2025; use whichever exists.
T="$(docker exec -i "$PG" psql -U "$DBU" -d "$DBN" -X -tA \
     -c "SELECT coalesce(to_regclass('public.asset')::text, to_regclass('public.assets')::text)" 2>/dev/null | tr -d '[:space:]')"
[ -n "$T" ] || { h "Database"; echo "Couldn't find the asset table (container $PG, user $DBU, db $DBN)."; exit 0; }

# to_jsonb(a)->>'col' keeps this working across Immich versions (missing columns read as empty)
COLS="a.id, to_jsonb(a)->>'originalFileName' AS file, to_jsonb(a)->>'status' AS status,
      to_jsonb(a)->>'deletedAt' AS deleted, to_jsonb(a)->>'libraryId' AS library,
      to_jsonb(a)->>'visibility' AS visibility, to_jsonb(a)->>'updatedAt' AS updated"

h "Database: assets per user (table $T)"
sql "SELECT u.email, count(*) AS assets,
            count(*) FILTER (WHERE to_jsonb(a)->>'deletedAt' IS NOT NULL) AS in_trash
     FROM $T a JOIN \"$( [ "$T" = asset ] && echo user || echo users )\" u ON u.id = a.\"ownerId\"
     GROUP BY u.email ORDER BY 2 DESC;" | sed -E 's/^( *)([^@ ]{2})[^@ ]*@/\1\2…@/'

h "Database: same owner + same checksum more than once (what the phone refuses)"
sql "SELECT a.\"ownerId\" AS owner, encode(a.checksum, 'base64') AS checksum, count(*) AS copies,
            string_agg(a.id::text || ' ' || coalesce(to_jsonb(a)->>'originalFileName','?')
                       || CASE WHEN to_jsonb(a)->>'deletedAt' IS NOT NULL THEN ' (trash)' ELSE '' END
                       || CASE WHEN to_jsonb(a)->>'libraryId' IS NOT NULL THEN ' (external lib)' ELSE '' END, ' | ') AS assets
     FROM $T a GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY 3 DESC LIMIT 30;"

h "Database: count of such duplicates"
sql "SELECT count(*) AS duplicate_groups FROM (SELECT 1 FROM $T GROUP BY \"ownerId\", checksum HAVING count(*) > 1) d;"

# album tables and their link columns (names changed across versions)
AL="$(docker exec -i "$PG" psql -U "$DBU" -d "$DBN" -X -tA \
      -c "SELECT coalesce(to_regclass('public.album')::text, to_regclass('public.albums')::text)" 2>/dev/null | tr -d '[:space:]')"
AA="$(docker exec -i "$PG" psql -U "$DBU" -d "$DBN" -X -tA \
      -c "SELECT coalesce(to_regclass('public.album_asset')::text, to_regclass('public.albums_assets_assets')::text)" 2>/dev/null | tr -d '[:space:]')"
col() {  # column of table $1 whose name contains $2 and ends in "Id"
  docker exec -i "$PG" psql -U "$DBU" -d "$DBN" -X -tA -c "SELECT column_name FROM information_schema.columns
    WHERE table_name = '$1' AND column_name ILIKE '%$2%id' ORDER BY length(column_name) LIMIT 1" 2>/dev/null | tr -d '[:space:]'
}
if [ -n "$AL" ] && [ -n "$AA" ]; then
  AA_AS="$(col "$AA" asset)"; AA_AL="$(col "$AA" album)"
  h "Database: photos in albums, by kind"
  sql "SELECT coalesce(to_jsonb(a)->>'visibility','?') AS visibility, coalesce(to_jsonb(a)->>'status','?') AS status,
              (to_jsonb(a)->>'deletedAt' IS NOT NULL) AS in_trash, (a.\"ownerId\" <> al.\"ownerId\") AS other_users_photo,
              count(*) AS album_links
       FROM $AA x JOIN $T a ON a.id = x.\"$AA_AS\" JOIN $AL al ON al.id = x.\"$AA_AL\"
       GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC;"
fi

for ARG in "${ARGS[@]}"; do
  if [[ "$ARG" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    h "Database: $ARG as a photo"
    sql "SELECT $COLS, a.\"ownerId\" AS owner, to_jsonb(a)->>'livePhotoVideoId' AS live_video,
                to_jsonb(a)->>'stackId' AS stack FROM $T a WHERE a.id = '$ARG';"
    sql "SELECT id AS is_live_photo_video_of FROM $T WHERE to_jsonb($T)->>'livePhotoVideoId' = '$ARG';"
    if [ -n "$AL" ]; then
      h "Database: $ARG as an album"
      sql "SELECT al.id, to_jsonb(al)->>'albumName' AS name, al.\"ownerId\" AS owner,
                  to_jsonb(al)->>'deletedAt' AS deleted, to_jsonb(al)->>'updatedAt' AS updated
           FROM $AL al WHERE al.id = '$ARG';"
    fi
    if [ -n "$AA" ]; then
      h "Database: album links of $ARG"
      sql "SELECT x.\"$AA_AL\" AS album, x.\"$AA_AS\" AS photo FROM $AA x
           WHERE x.\"$AA_AL\" = '$ARG' OR x.\"$AA_AS\" = '$ARG' LIMIT 5;"
      sql "SELECT count(*) AS links FROM $AA x WHERE x.\"$AA_AL\" = '$ARG' OR x.\"$AA_AS\" = '$ARG';"
    fi
  elif [[ "$ARG" =~ ^[A-Za-z0-9+/]+=*$ ]]; then
    h "Database: assets with checksum $ARG"
    sql "SELECT $COLS, a.\"ownerId\" AS owner FROM $T a WHERE a.checksum = decode('$ARG', 'base64');"
  else
    echo "Skipped, neither an id nor a checksum: $ARG"
  fi
done

echo
echo "Nothing was changed. Paste everything above to Claude."
