#!/bin/bash
cd /tmp
su git -c "/usr/local/bin/forgejo admin user create --username verifyagent --email va@local --password ut4JWOrZJlu_ --config /home/git/forgejo/custom/conf/app.ini" 2>&1 | grep -v "^\s*$" | head -2
rm -f cjs.txt
curl -s -c cjs.txt http://127.0.0.1:3000/user/login -o l.html
curl -s -b cjs.txt -c cjs.txt -X POST http://127.0.0.1:3000/user/login \
  --data-urlencode "user_name=verifyagent" \
  --data-urlencode "password=ut4JWOrZJlu_" \
  -o post.html -w "post: %{http_code} -> %{redirect_url}\n"
echo "cookies: $(grep -c forgejo cjs.txt || true)"
# если не залогинились — показать ошибку
grep -o 'class="ui negative[^"]*message[^"]*">[^<]*' post.html | head -2
grep -o 'Invalid[^<]*\|CSRF[^<]*\|error[^<]*' post.html | head -3
curl -s -b cjs.txt http://127.0.0.1:3000/user/settings -o s.html -w "settings: %{http_code}\n"
echo "agent link in settings: $(grep -c 'user/settings#agent' s.html)"
echo "agent js link in settings: $(grep -c 'assets/agent-settings/agent-settings.js' s.html)"
echo "agent css link in settings: $(grep -c 'assets/agent-settings/agent-settings.css' s.html)"
