# 实战：腾讯云整机换服务器的踩坑记录

一次真实换机的完整过程记录。2026-10-17，`120.53.28.29` → `211.159.174.204`（腾讯云，Ubuntu，`ubuntu` 用户），迁智能询价（`/opt/sq`）+ zk-ai 网关（`/opt/zkai`）+ 积分消耗器。

**核心教训：数据搬过去 ≠ 服务能跑。** 这次真正卡住用户的不是数据，是三个"看代码看不出来"的运行时条件：SSH 免密、安全组端口、TLS 证书。

---

## 一、按此顺序做，别跳步

换服务器最容易犯的错是「先搬数据，最后发现连不上」。正确顺序：

```text
1. SSH 免密（进不去就没法做任何事）
2. 清点旧机在跑什么（systemd + cron + 进程，别只看眼前目录）
3. 数据搬迁 + 逐表/逐目录核对（不信任「大小差不多」）
4. systemd 服务启用 + 目录权限补齐
5. 安全组端口放行  ← 最容易漏
6. TLS 证书签发验证  ← 最容易漏
7. 公网端到端实测（curl 真实 URL，不是只看 systemctl active）
8. 观测真实流量，确认没人在用旧机后再停旧机
```

第 5、6、7 步是这次翻车的地方。`systemctl is-active` 全绿、日志零错误、磁盘内存健康，**但用户就是打不开**——因为 TLS 握手在 TCP 层就断了。

---

## 二、SSH 免密：Windows 上 `ssh-copy-id` 不存在

**症状**：`ssh-copy-id : 无法将"ssh-copy-id"项识别为 cmdlet、函数、脚本文件或可运行程序的名称。`

**原因**：Windows 自带 OpenSSH 只有 `ssh`/`scp`/`sftp`，没有 `ssh-copy-id`（那是 Linux 脚本）。用户在 PowerShell 里跑就报这个。如果是 Git Bash 则有。

**方案 1（PowerShell，一条命令顶替 ssh-copy-id）**：

```powershell
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub" | ssh ubuntu@<IP> "mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys"
```

**方案 2（Git Bash / Linux）**：

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub ubuntu@<IP>
```

**方案 3（腾讯云，全程免密码）**：控制台 → SSH 密钥 → 创建密钥 → **「使用已有公钥」**（别选「创建新密钥对」，会多生成一把要额外管）→ 粘贴公钥 → 实例 → 更多 → 密码/密钥 → **加载密钥**（需先关机）。

### 坑 A1：把公钥文本当命令粘贴

用户粘贴 `ssh-ed25519 AAAA... my-laptop` 到 shell，报 `command not found`。公钥是**数据**不是命令，必须放进文件或参数里。用户不懂这个区别，所以给命令时要说清「整行是一个字符串，不要单独执行」。

### 坑 A2：密钥被覆盖后，老服务器静默失效

`ssh-keygen -t ed25519 -C "my-laptop"` 若不慎覆盖同名密钥，**所有只授权过旧 ed25519 的服务器全部失效**，且不会报错——只是回落到密码登录，用户以为"服务器变慢了/要输密码了"。

排查：`stat -c '%y' ~/.ssh/id_*` 看时间戳；`ssh-keygen -lf` 看指纹。覆盖后**没有备份就找不回来**，只能逐台重推。换机前先备份 `~/.ssh`。

### 坑 A3：`identitiesOnly` 必须配

`~/.ssh/config` 里给每个 Host 加 `IdentitiesOnly yes`，否则 agent 里堆积的密钥会轮流拿去试，可能触发 `Too many authentication failures`（服务器默认 `MaxAuthTries 6`）。

```sshconfig
Host zk
    HostName 211.159.174.204
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

### 验证（唯一可信的方式）

```bash
ssh -o BatchMode=yes zk 'echo LOGIN_OK; whoami'
```

`BatchMode=yes` 禁掉一切交互，不读密码、不读 passphrase。**只有这行返回 `LOGIN_OK` 才算真免密**。交互式 `ssh` 成功可能只是 shell 缓存了密码或走了 agent，不能作为证据。

---

## 三、清点旧机：别只看 home 目录

**踩的坑**：一开始只看了 `/home/ubuntu` 和 `/opt`，判断「只有几个 json 没迁」。实际漏了 PostgreSQL 里的 22 万条商品数据。

### 正确清点命令（旧机跑一遍）

```bash
# 1. systemd 服务（不只是当前进程，还有开机自启的）
systemctl list-units --type=service --state=running
ls /etc/systemd/system/ | grep -iE 'sq|zk|quot|ai'

# 2. cron（换机最容易漏，而且可能造成数据分叉）
ls /etc/cron.d/; sudo cat /etc/cron.d/<每个文件>

# 3. 数据库真实大小（用 postgres 身份，别用普通用户）
sudo -u postgres psql -tAc "SELECT datname, pg_size_pretty(pg_database_size(datname))
  FROM pg_database WHERE NOT datistemplate;"

# 4. 各目录真实占用
sudo du -sh /home /opt /var /srv /data /root
```

### 坑 B1：`du` 读不到就是 12K，误会成"空库"

**这是本次最严重的一次误判。** 用 `ubuntu` 身份跑 `du -sh /var/lib/postgresql` 得到 **12K**，据此判断"数据库是空的，不用迁"。实际：

```bash
pg_lsclusters   # → 16 main 5432 online
sudo du -sh /var/lib/postgresql   # → 320M
```

原因：`/var/lib/postgresql` 属主是 `postgres`，普通用户 `du` 时目录不可读，**du 不报错、只统计到目录条目本身的大小**（4K/12K）。这是静默的假阴性。

**规则：判断数据库有没有数据，永远用 `psql` 查 `pg_database_size`，不要用 `du`。**

### 坑 B2：数据库迁好了，但只核对表名不够

两边都有 `sqdb`、都有同样的 7 张表，容易就此收工。必须**逐表核对行数**：

```bash
for t in audit_events companies quotation_configs quotation_items \
         security_events subscription_orders users; do
  printf "%s %s\n" "$t" "$(sudo -u postgres psql -d sqdb -tAc "SELECT count(*) FROM $t;")"
done
```

本次结果两边逐张吻合（`quotation_items` = 228,375），才敢确认数据完整。库大小 121MB vs 87MB 的差异是 VACUUM/索引膨胀导致的，**不能只看大小**。

---

## 四、systemd 服务：目录缺失会静默拖垮服务

新机 `zkai-burner.service` 起不来，原因不是配置错，是 **`/var/lib/zkai/burner` 目录不存在**——而 service 里 `ReadWritePaths` 和 `--state-file` 都指向它。

```bash
sudo mkdir -p /var/lib/zkai/burner
sudo chown www-data:www-data /var/lib/zkai/burner   # 必须和 service 的 User= 一致
sudo chmod 755 /var/lib/zkai/burner
sudo systemctl enable --now zkai-burner.service
```

**规则**：换机后逐项检查每个 service 里的 `WorkingDirectory`、`ReadWritePaths`、`--state-file`、`--log-file`、`EnvironmentFile` 路径是否在新机存在，属主是否为 `User=` 指定的用户。

### 坑 C1：`EnvironmentFile` 不读 `.env`

`EnvironmentFile=/etc/zkai.env` 指的是**系统路径的 env 文件**，不是项目里的 `/opt/zkai/.env`。新机只搬了项目目录、没搬 `/etc/xxx.env`，服务就会缺环境变量起不来。两个都要查：

```bash
ls -la /etc/<app>.env          # systemd 注入用
ls -la /opt/<app>/.env         # 应用自己 load_dotenv 用
```

### 坑 C2：`Restart=always` 会掩盖崩溃

`zkai-burner` 配了 `Restart=always` + `RestartSec=10`。目录缺失导致它每 10 秒崩一次再重启，`systemctl status` 经常抓到 `active` 的瞬间，看起来"正常"。

**看 `NRestarts`，不是看 `Active`**：

```bash
systemctl show <service> -p NRestarts
```

这个值持续增长就是崩溃循环。稳定运行的服务它应该一直是 0。

---

## 五、安全组：`systemctl active` 全绿也打不开的元凶

**本次最隐蔽的问题。** 新机三个服务全 active、日志零 error、`systemctl --failed` 空、磁盘内存健康，但用户访问 `https://211.159.174.204/admin/index.html` 打不开。

### 症状特征

```bash
curl -sk -o /dev/null -w "%{http_code} %{time_total}s\n" https://<IP>/admin/index.html
# → 000  0.056s      ← 000 + 极短耗时 = TCP/TLS 层就断了，不是应用层问题
```

**`curl` 返回 `000` 且耗时 < 0.1s，说明 TCP 连接或 TLS 握手直接失败，跟后面的应用、数据库、反代配置全都无关。** 别在这个信号下去查业务日志。

### 定位

```bash
# 本机（服务器上）看进程在listen
sudo ss -tlnp | grep -E ':(80|443) '

# 从公网侧测（关键：换个视角）
curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 http://<IP>/
```

服务器上 `ss` 显示 `*:443 LISTEN` 正常，公网却连不上 → **安全组/防火墙没放行**。

### 修复

腾讯云控制台 → CVM → 实例 → 安全组 → 编辑规则 → **入站规则**：

| 协议 | 端口 | 来源 |
|---|---|---|
| TCP | 80 | 0.0.0.0/0 |
| TCP | 443 | 0.0.0.0/0 |

**坑：80 端口不能省。** 很多人只开 443 觉得 HTTPS 就够了。但 ACME 的 http-01 验证必须走 80，不开 80 就签不出证书，443 也就没东西可用——**两个端口是捆绑的**。

---

## 六、TLS 证书：IP 直连也能上 HTTPS，但要特殊配置

用户问："为什么之前 `https://120.53.28.29` 能访问，新的不行？" —— 因为老机器签出了 IP 证书，新机器没有。

### 现状：Let's Encrypt 支持裸 IP 证书

2026 年起 Let's Encrypt 支持给 IP 地址签证书，但需要 Caddy 的 `shortlived` profile：

```caddy
{
    cert_issuer acme {
        profile shortlived              # 必须！否则普通 ACME 不给 IP 签
        disable_tlsalpn_challenge       # IP 证书只支持 http-01，关掉 tls-alpn
    }
    default_sni 211.159.174.204
}
```

**坑 D1：证书有效期只有 6 天**，靠 Caddy 自动续期。所以 **80 端口必须长期放行**，收回就会续期失败，6 天后再次打不开。这个"定时炸弹"特性必须提前告知用户。

**坑 D2：staging 环境不报错，只是默默拿不到证书**

失败时日志里的关键信号：

```text
CA: acme-staging-v02.api.letsencrypt.org      ← 用了测试环境
error: Fetching http://<IP>/.well-known/acme-challenge/xxx:
       Timeout during connect (likely firewall problem)
attempt: 21, retrying_in: 7200                 ← 退避到每 2 小时
```

Caddy 未拿到正式证书时会退化到 staging 反复重试，退避时间涨到小时级。**你修好端口后如果不等它自己重试，看起来就像"改了没用"**。

**修复后必须主动重启触发立即重试**，别等退避周期：

```bash
sudo systemctl restart caddy
```

### 成功时该看到的日志

```text
served key authentication  status=200          ← LE 验证服务器来取到了
authorization finalized    authz_status=valid
certificate obtained successfully
issuer=acme-v02.api.letsencrypt.org-directory  ← 正式环境（不是 staging）
```

### 端到端验证（最重要的一步）

```bash
# 跳过证书校验（排除内容问题）
curl -sk -o /dev/null -w "%{http_code}\n" https://<IP>/admin/index.html

# 严格校验证书链（确认浏览器不会报红）
curl -s -o /dev/null -w "%{http_code}\n" https://<IP>/admin/index.html
```

**两个都要 200。** 只用 `-k` 会漏掉证书不受信任的问题；只看 `systemctl status` 什么都发现不了。

---

## 七、停旧机前：先看真实流量，别信"没人用了"

**差点出事的地方。** 准备停旧机时查了访问日志，发现旧机网关**每分钟都有请求**：

```text
POST /zkai/v1/messages?beta=true  host=120.53.28.29
User-Agent: claude-cli/2.1.301
X-Zkai-Model: kimi-k3   X-Zkai-Credential: sensenova-09
status=200
```

如果直接 `systemctl stop`，那个正在用的客户端立刻全断。

### 停旧机前的检查清单

```bash
# 1. 旧机访问日志的最近记录（有没有活跃请求）
sudo tail -20 /var/log/caddy/<access>.log

# 2. 旧机应用的请求量（近 2 小时）
sudo journalctl -u <app> --since "2 hours ago" --no-pager | wc -l

# 3. 旧机数据库最新写入时间
sudo -u postgres psql -d <db> -tAc "SELECT max(created_at) FROM <表>;"

# 4. 客户端的公网 IP 是谁（对比自己本机 IP，判断是否有第三方在用）
curl -s https://api.ipify.org
```

第 4 步很关键：本次发现请求方 IP `122.4.121.30` **不等于**自己本机公网 IP `123.235.233.227`，说明还有别的设备（或别人的客户端）在用旧机。这种依赖关系不查出来就停服务，等于给用户制造事故。

### 坑 E1：DNS 域名可能是摆设，全靠 IP 直连

Caddyfile 里配了 `zk.abrdns.com`、`hermes.ip-ddns.com`，看起来有域名可以平滑切换。实际一查：

```bash
nslookup zk.abrdns.com        # → 不存在
nslookup hermes.ip-ddns.com   # → 188.165.11.93（既不是旧机也不是新机）
```

**教训：不要把「配置文件里写了域名」当成「域名在用」。** 换机前必须实际解析验证。纯 IP 直连意味着**换 IP 就必须逐个改客户端配置**，没有 DNS 能帮你平滑切换——这比域名方案成本高得多，值得在架构上纠正。

### 坑 E2：cron 会在两台机器上同时跑

`/etc/cron.d/sq-backup`、`sq-keepwarm`、`sq-cloud` 在两台机器上内容完全一致。旧机不停，就是两个备份任务、两个保活任务并行。

尤其 `sq-cloud-push`（每天 4:40 推云）——**旧库停了服务但 cron 还在跑，会用旧数据覆盖云端新数据**。停了服务不等于停了 cron：

```bash
sudo systemctl stop cron        # 或者逐个注释 /etc/cron.d/sq-*
```

---

## 八、数据搬迁的核对方法

### 目录：大小要精确到 KB 吻合

```bash
du -sh /opt/* | sort -h      # 两台机器各跑一遍，逐项对比
```

本次 `/opt` 六个目录逐一吻合（`google 434M`、`sq 156M`、`zkai 71M`…），即使不是 rsync 也能高置信度确认完整。**"差不多大"不算吻合，"121MB vs 87MB"这种要看里面是什么**（数据库有膨胀，静态目录不该有差异）。

### 配置文件 vs 运行时缓存：要分清

`/var/www` 34M 看起来像网站文件，进去看只有 `.cache/` 和 `.gitconfig`——这是运行时缓存，不是业务数据，新机重启会自己重建。**别因为"看着像数据"就盲目搬**，先 `ls` 一层看清楚。

### home 目录下的点文件：要判断是不是项目用的

`~/.rebuild_cfg.json`、`~/.rebuild_rows.json`、`parent_company_id` 这些看名字完全猜不出用途。办法是**去项目代码里 grep 引用**：

```bash
grep -rn --include=*.py -e "rebuild_cfg" -e "parent_company_id" /opt/<项目>/ | head
```

本次查到它们确实是 `/opt/sq` 的配置和商品样例（`app_title: 智能询价系统` 直接写在 json 里）。但**真正的 22 万条数据在 PostgreSQL**，这些 json 只是配置和 6 条样例——所以它们缺失不影响运行。**搞清楚"配置文件"和"数据源"的区别，才不会把小事当大事、或把大事当小事。**

---

## 九、换机后验收清单

按这个清单走一遍，全绿才算完成：

```bash
# 1. 免密（唯一可信验证）
ssh -o BatchMode=yes <host> 'echo LOGIN_OK'

# 2. 服务状态 + 崩溃次数
for s in <svc1> <svc2>; do
  echo "$s: $(systemctl is-active $s) / $(systemctl is-enabled $s)"
  systemctl show $s -p NRestarts
done

# 3. 系统级失败单元（应该为空）
systemctl --failed

# 4. 日志错误（近 1 小时）
sudo journalctl -u <svc> --since "1 hour ago" | grep -iE 'error|exception|traceback|failed'

# 5. 数据完整性（逐表行数）
sudo -u postgres psql -d <db> -tAc "SELECT count(*) FROM <核心表>;"

# 6. 资源
free -h; df -h /; uptime

# 7. cron 是否到位
ls /etc/cron.d/ | grep -E '<app>'

# 8. 公网端到端（最关键，别省）
curl -s -o /dev/null -w "%{http_code}\n" https://<IP>/<真实路径>
```

第 8 步是这次踩坑后加的。前 7 步全绿也可能第 8 步失败——**只有真实 URL 返回 200，才算换机完成。**

---

## 十、一句话总结

| 层 | 检查什么 | 失败时的典型错觉 |
|---|---|---|
| SSH | `BatchMode=yes` 能登 | "能 ssh 进去就是配好了"（其实走的密码/agent） |
| 进程 | `is-active` + `NRestarts` | "active 就是正常"（其实在崩溃循环） |
| 数据 | 逐表行数 + 逐目录 KB 级大小 | "du 显示 12K 就是空库"（其实是权限问题） |
| 网络 | 安全组 80+443 都放行 | "开了 443 就够"（证书签不出来） |
| TLS | 严格校验 curl 返回 200 | "systemctl 全绿就是好了"（TLS 层早断了） |
| 切流 | 查旧机真实访问日志 | "应该没人用了吧"（每分钟都有请求） |

**最贵的一课：`systemctl status` 全绿、日志零报错、磁盘内存健康，用户照样打不开。** 验收必须从用户的视角做——`curl` 那个真实 URL。
