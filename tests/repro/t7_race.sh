# ⑦ 관리자 업로드 동시 요청: (a) 같은 이름 2개 동시 (b) 다른 이름 5개 동시
TS="$(cd "$(dirname "$0")" && pwd)"; R="$(cd "$TS/../.." && pwd)"
A="Authorization: Bearer $(sed -n 's/^RECAT_ADMIN_KEY=//p' $R/.env | tail -1)"
clean() { rm -rf $R/data/repos/race_* $R/data/registry.json; }
clean
echo "(a) 같은 이름(race_same) 동시 2개"
for i in 1 2; do (curl -s -o /tmp/race_a$i.txt -w "%{http_code}" -H "$A" -F file=@$TS/kit/1_shop_test.zip -F repo=race_same localhost:8100/admin/repos/upload > /tmp/race_a${i}c.txt &) ; done
sleep 4; for i in 1 2; do echo "  요청$i: $(cat /tmp/race_a${i}c.txt) $(head -c 120 /tmp/race_a$i.txt)"; done
echo "(b) 다른 이름 5개 동시"
for i in 1 2 3 4 5; do (curl -s -o /dev/null -w "%{http_code} " -H "$A" -F file=@$TS/kit/1_shop_test.zip -F repo=race_$i localhost:8100/admin/repos/upload >> /tmp/race_b.txt &); done
sleep 6; echo "  응답: $(cat /tmp/race_b.txt)"; rm -f /tmp/race_b.txt
echo "  폴더: $(ls -d $R/data/repos/race_* | xargs -n1 basename | tr '\n' ' ')"
echo "  registry.json 등록: $(python3 -c "import json;print(sorted(json.load(open('$R/data/registry.json'))['repos']))")"
clean
