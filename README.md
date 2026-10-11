# EU 热点日报（自动版）

本地运行：
  pip install -r requirements.txt
  cp .env.example .env   # 填入 163 授权码
  python main.py --send

服务器定时（按 Europe/Brussels）：
  0 8 * * 1-5 cd /opt/eu_daily && /usr/bin/python3 main.py --send >> run.log 2>&1

GitHub Pages 自动发布：
  1. 建仓库 eu-daily，放入本目录文件（.env 不进仓库）；
  2. Settings → Secrets → 新建 MAIL_PASS（163 授权码）；
  3. 开启 Settings → Pages，Source 选 gh-pages 分支；
  4. Actions 采用 .github/workflows/daily.yml，工作日 UTC 06:00 触发，
     脚本二次校验 Brussels 08:00 才执行；生成 public/index.html 推送 Pages。

注意：163 使用 SMTP 授权码（非登录密码），端口 465。RSS 源在 FEEDS 中按国家维护。
