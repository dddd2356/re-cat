# ⑦-b 같은 이름에 서로 다른 zip(v1: 함수 7개, v2: discount 추가 8개)을 동시에 — 시도마다 새 이름
TS="$(cd "$(dirname "$0")" && pwd)"; R="$(cd "$TS/../.." && pwd)"
A="Authorization: Bearer $(sed -n 's/^RECAT_ADMIN_KEY=//p' $R/.env | tail -1)"
for n in 1 2 3 4 5 6; do
  name=race_${n}_$RANDOM
  (curl -s -o /tmp/r1.txt -w "%{http_code}" -H "$A" -F file=@$TS/kit/1_shop_test.zip -F repo=$name localhost:8100/admin/repos/upload > /tmp/c1.txt &)
  (curl -s -o /tmp/r2.txt -w "%{http_code}" -H "$A" -F file=@$TS/kit/2_shop_test_v2.zip -F repo=$name localhost:8100/admin/repos/upload > /tmp/c2.txt &)
  sleep 3
  c1=$(cat /tmp/c1.txt); c2=$(cat /tmp/c2.txt)
  win=$( [ "$c1" = 200 ] && echo v1 || ([ "$c2" = 200 ] && echo v2 || echo 없음) )
  disk=$(grep -q "def discount" $R/data/repos/$name/billing/invoice.py 2>/dev/null && echo v2 || echo v1)
  fc=$(curl -s -H "$A" localhost:8100/admin/repos | python3 -c "import sys,json;print([r.get('function_count') for r in json.load(sys.stdin)['repos'] if r['repo']=='$name'])")
  flag=$( [ "$win" != "$disk" ] && echo "  ← 불일치" )
  echo "  시도$n: v1요청=$c1 v2요청=$c2 | 성공 응답=$win, 디스크 파일=$disk, 분석 함수 수=$fc$flag"
  rm -rf $R/data/repos/$name
done
rm -f $R/data/registry.json
