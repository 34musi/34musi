# quant-monitor 标准启动说明

> 日常按顺序操作即可。下面带色块的是**必做步骤**。

---

<div style="background:linear-gradient(135deg,#0d47a1,#1565c0);color:#ffffff;padding:18px 22px;border-radius:10px;border:2px solid #42a5f5;margin:16px 0;box-shadow:0 4px 14px rgba(21,101,192,0.35);">

<strong style="font-size:1.15em;">① 进入项目并激活虚拟环境（Windows PowerShell）</strong>

<pre style="background:rgba(0,0,0,0.25);padding:12px;border-radius:6px;margin:10px 0 0;overflow-x:auto;color:#e3f2fd;"><code>cd "d:\Program Files\project_musi\Project\34musi\quant-monitor"
.\.venv\Scripts\Activate.ps1</code></pre>

</div>

<div style="background:linear-gradient(135deg,#1b5e20,#2e7d32);color:#ffffff;padding:18px 22px;border-radius:10px;border:2px solid #66bb6a;margin:16px 0;box-shadow:0 4px 14px rgba(46,125,50,0.35);">

<strong style="font-size:1.15em;">② 启动 API 服务（窗口 1 — 保持运行，不要关）</strong>

<pre style="background:rgba(0,0,0,0.25);padding:12px;border-radius:6px;margin:10px 0 0;overflow-x:auto;color:#e8f5e9;"><code>uvicorn app.main:app --reload --host 0.0.0.0 --port 8000</code></pre>

<p style="margin:12px 0 0;opacity:0.95;">仅本机 + 穿透访问时，可改为：</p>
<pre style="background:rgba(0,0,0,0.2);padding:10px;border-radius:6px;margin:8px 0 0;overflow-x:auto;color:#e8f5e9;"><code>uvicorn app.main:app --host 127.0.0.1 --port 8000</code></pre>

</div>

<div style="background:linear-gradient(135deg,#e65100,#ef6c00);color:#ffffff;padding:18px 22px;border-radius:10px;border:2px solid #ffb74d;margin:16px 0;box-shadow:0 4px 14px rgba(239,108,0,0.35);">

<strong style="font-size:1.15em;">③ 本机 / 家里 WiFi 访问地址</strong>

<table style="width:100%;margin-top:10px;border-collapse:collapse;color:#fff3e0;">
<tr><td style="padding:6px 8px;"><strong>图形控制台</strong></td><td style="padding:6px 8px;"><a href="http://127.0.0.1:8000/ui" style="color:#ffe082;">http://127.0.0.1:8000/ui</a></td></tr>
<tr><td style="padding:6px 8px;"><strong>API 文档</strong></td><td style="padding:6px 8px;"><a href="http://127.0.0.1:8000/docs" style="color:#ffe082;">http://127.0.0.1:8000/docs</a></td></tr>
<tr><td style="padding:6px 8px;"><strong>健康检查</strong></td><td style="padding:6px 8px;"><a href="http://127.0.0.1:8000/health" style="color:#ffe082;">http://127.0.0.1:8000/health</a></td></tr>
</table>

<p style="margin:12px 0 0;opacity:0.95;">同一 WiFi 下其它设备：把 <code style="background:rgba(0,0,0,0.2);padding:2px 6px;border-radius:4px;">127.0.0.1</code> 换成电脑局域网 IP（<code>ipconfig</code> 里的 IPv4）。</p>

</div>

---

## 可选：API 密钥（对外 ngrok 时强烈建议）

在项目根目录创建 `.env`：

```env
API_KEY=请改成一长串随机密码
```

修改后**重启 uvicorn**。在控制台 **① 入门必读** 填入同一串并点「保存到本机浏览器」。

---

<div style="background:linear-gradient(135deg,#4a148c,#6a1b9a);color:#ffffff;padding:18px 22px;border-radius:10px;border:2px solid #ba68c8;margin:16px 0;box-shadow:0 4px 14px rgba(106,27,154,0.35);">

<strong style="font-size:1.15em;">④ 人在外面访问家里电脑 — ngrok（窗口 2 — 保持运行）</strong>

<p style="margin:10px 0 0;opacity:0.95;">先完成上面 ①②；ngrok 只需配置一次 authtoken（见 <a href="https://dashboard.ngrok.com/get-started/your-authtoken" style="color:#e1bee7;">dashboard.ngrok.com</a>）。</p>

<pre style="background:rgba(0,0,0,0.25);padding:12px;border-radius:6px;margin:10px 0 0;overflow-x:auto;color:#f3e5f5;"><code>cd C:\ngrok
.\ngrok.exe config add-authtoken 你的authtoken
.\ngrok.exe http 8000</code></pre>

<p style="margin:12px 0 0;"><strong>外网地址</strong>：看 ngrok 窗口里 <code>Forwarding</code> 那一行，例如：</p>
<pre style="background:rgba(0,0,0,0.25);padding:12px;border-radius:6px;margin:8px 0 0;overflow-x:auto;color:#f3e5f5;"><code>https://你的子域.ngrok-free.dev/ui</code></pre>

<p style="margin:10px 0 0;opacity:0.9;">免费版每次重启 ngrok，域名可能会变，以终端显示为准。调试页：<a href="http://127.0.0.1:4040" style="color:#e1bee7;">http://127.0.0.1:4040</a></p>

</div>

---

<div style="background:#263238;color:#eceff1;padding:14px 18px;border-radius:8px;border-left:6px solid #ff5252;margin:16px 0;">

<strong>注意</strong>

<ul style="margin:8px 0 0;padding-left:1.2em;">
<li>关掉 uvicorn 或 ngrok 窗口 → 外网立刻无法访问。</li>
<li>电脑休眠/关机 → 外网无法访问。</li>
<li>不要把 ngrok 链接和 API_KEY 发到公开场合。</li>
</ul>

</div>

---

## 一键自检

| 检查项 | 做法 |
|--------|------|
| 服务是否起来 | 浏览器打开 `/health` 应返回正常 JSON |
| ngrok 是否在线 | 终端显示 <code>Session Status: online</code> |
| 外网能否打开 | 用手机 4G（不用家里 WiFi）访问 <code>https://xxx.ngrok-free.dev/ui</code> |

---

## 云服务器部署

见项目内 `deploy/` 目录（systemd + nginx + `install-on-ubuntu.sh`）。
