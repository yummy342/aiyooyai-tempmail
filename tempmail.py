#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时收件箱（只收不发）
   收信：IMAP 拉 hMailServer 的 catch-all 邮箱，按收件人分箱
   存储：SQLite；邮件 1 小时过期；日志 24 小时
   页面：/ 与 /i/<inbox>
"""
import json
import os
import random
import re
import sqlite3
import string
import threading
import time
import imaplib
import email
from email.header import decode_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('TM_DB', os.path.join(BASE_DIR, 'data', 'tempmail.db'))
PORT = int(os.environ.get('TM_PORT', '8899'))
DOMAIN = os.environ.get('TM_DOMAIN', 'example.com')
TTL = int(os.environ.get('TM_TTL', '3600'))
LOG_TTL = int(os.environ.get('TM_LOG_TTL', '86400'))
IMAP_HOST = os.environ.get('TM_IMAP_HOST', '')       # 例如 mail.example.com
IMAP_PORT = int(os.environ.get('TM_IMAP_PORT', '993'))
IMAP_USER = os.environ.get('TM_IMAP_USER', '')
IMAP_PASS = os.environ.get('TM_IMAP_PASS', '')
BASE = os.environ.get('TM_BASE', '/') or '/'
if not BASE.startswith('/'): BASE = '/' + BASE
if not BASE.endswith('/'): BASE += '/'

ALPHABET = string.ascii_lowercase + string.digits
MAX_MSG_PER_INBOX = int(os.environ.get('TM_MAX_MSG', '50'))
MAX_MSG_BYTES = int(os.environ.get('TM_MAX_BYTES', '600000'))
RESERVED = {'postmaster', 'webmaster', 'abuse', 'admin', 'root', 'catchall',
            'mailer-daemon', 'noreply', 'no-reply', 'security', 'support', 'hostmaster'}
# 建箱限速：每个 IP 每小时 30 个
CREATE_WINDOW = 3600
CREATE_LIMIT = int(os.environ.get('TM_CREATE_LIMIT', '30'))
_create_hits = {}

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=5000')
    return c

def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS inboxes(
      id TEXT PRIMARY KEY, address TEXT UNIQUE NOT NULL, alias TEXT NOT NULL,
      created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages(
      id TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, mail_from TEXT, subject TEXT,
      body_text TEXT, body_html TEXT, received_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
      msgid TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_msg_inbox ON messages(inbox_id, received_at);
    CREATE TABLE IF NOT EXISTS logs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT, detail TEXT
    );
    ''')
    try:
        cols = [r[1] for r in c.execute('PRAGMA table_info(messages)').fetchall()]
        if 'msgid' not in cols:
            c.execute('ALTER TABLE messages ADD COLUMN msgid TEXT')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_dedup ON messages(inbox_id, msgid) WHERE msgid IS NOT NULL')
    except sqlite3.Error:
        pass
    c.commit(); c.close()

def log(kind, detail=''):
    try:
        c = db(); c.execute('INSERT INTO logs(at, kind, detail) VALUES(?,?,?)', (int(time.time()), kind, str(detail)[:300])); c.commit(); c.close()
    except Exception:
        pass

def rand_id(n=10):
    return ''.join(random.choice(ALPHABET) for _ in range(n))

def create_inbox(addr=None):
    c = db()
    if addr:
        local = addr.split('@')[0].lower()
        local = re.sub(r'[^a-z0-9._-]', '', local).strip('.-_')[:32]
        if not local or '..' in local or local in RESERVED:
            c.close(); return None
        address = local + '@' + DOMAIN
    else:
        address = rand_id(10) + '@' + DOMAIN
    inbox = rand_id(12)
    try:
        c.execute('INSERT INTO inboxes(id, address, alias, created_at) VALUES(?,?,?,?)',
                  (inbox, address, rand_id(8), int(time.time())))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        return None
    c.close()
    return {'inbox': inbox, 'address': address}

def rotate_address(inbox):
    c = db()
    try:
        for _ in range(5):
            addr = rand_id(10) + '@' + DOMAIN
            try:
                c.execute('UPDATE inboxes SET address=? WHERE id=?', (addr, inbox))
                c.commit(); c.close(); return addr
            except sqlite3.IntegrityError:
                continue
    finally:
        try: c.close()
        except Exception: pass
    return ''


def inbox_by_id(inbox):
    c = db(); r = c.execute('SELECT * FROM inboxes WHERE id=?', (inbox,)).fetchone(); c.close()
    return r

def messages_of(inbox):
    c = db()
    now = int(time.time())
    rows = c.execute('SELECT id, mail_from, subject, received_at, expires_at FROM messages WHERE inbox_id=? AND expires_at>? ORDER BY received_at DESC',
                     (inbox, now)).fetchall()
    c.close()
    return [dict(r, remain=max(0, r['expires_at'] - now)) for r in rows]

def message_of(inbox, mid):
    c = db()
    r = c.execute('SELECT * FROM messages WHERE id=? AND inbox_id=?', (mid, inbox)).fetchone()
    c.close()
    return dict(r) if r else None

def delete_message(inbox, mid):
    c = db(); c.execute('DELETE FROM messages WHERE id=? AND inbox_id=?', (mid, inbox)); c.commit(); c.close()

def _h(v):
    if not v:
        return ''
    parts = decode_header(v)
    out = []
    for s, enc in parts:
        out.append(s.decode(enc or 'utf-8', 'replace') if isinstance(s, bytes) else s)
    return ''.join(out)

def _recipient_local(msg):
    for hdr in ('To', 'Delivered-To', 'X-Original-To', 'Envelope-To', 'Cc'):
        v = msg.get(hdr)
        if not v:
            continue
        m = re.search(r'([A-Za-z0-9._-]+)@' + re.escape(DOMAIN), _h(v), re.I)
        if m:
            return m.group(1).lower()
    return ''

def _body(msg):
    text = html = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if part.get_content_maintype() == 'multipart':
                continue
            try:
                payload = part.get_payload(decode=True) or b''
                charset = part.get_content_charset() or 'utf-8'
                body = payload.decode(charset, 'replace')
            except Exception:
                continue
            if ct == 'text/plain' and not text:
                text = body
            elif ct == 'text/html' and not html:
                html = body
    else:
        try:
            body = (msg.get_payload(decode=True) or b'').decode(msg.get_content_charset() or 'utf-8', 'replace')
        except Exception:
            body = ''
        if msg.get_content_type() == 'text/html':
            html = body
        else:
            text = body
    return text, html

def store_message(local, msg):
    c = db()
    row = c.execute('SELECT id FROM inboxes WHERE lower(address)=?', (local + '@' + DOMAIN,)).fetchone()
    if not row:
        c.close(); return False
    cnt = c.execute('SELECT COUNT(*) FROM messages WHERE inbox_id=?', (row['id'],)).fetchone()[0]
    if cnt >= MAX_MSG_PER_INBOX:
        c.close(); return False
    mid = rand_id(14)
    now = int(time.time())
    text, html = _body(msg)
    body = (text or '') + (html or '')
    if len(body) > MAX_MSG_BYTES:
        text, html = text[:200000], (html or '')[:400000]
    msgid = (_h(msg.get('Message-ID')) or '').strip()[:200] or None
    try:
        c.execute('INSERT INTO messages(id, inbox_id, mail_from, subject, body_text, body_html, received_at, expires_at, msgid) VALUES(?,?,?,?,?,?,?,?,?)',
                  (mid, row['id'], _h(msg.get('From'))[:300], _h(msg.get('Subject'))[:500], text[:200000], html[:400000], now, now + TTL, msgid))
    except sqlite3.IntegrityError:
        c.close(); return True  # 重复信：当作已处理，删掉远端副本
    c.commit(); c.close()
    log('store', local + ' ' + _h(msg.get('Subject'))[:60])
    return True

def imap_loop():
    if not (IMAP_HOST and IMAP_USER and IMAP_PASS):
        log('imap', '未配置 IMAP，收信停用')
        return
    while True:
        try:
            conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            conn.login(IMAP_USER, IMAP_PASS)
            conn.select('INBOX')
            typ, data = conn.search(None, 'UNSEEN')
            ids = (data[0] or b'').split()
            for seq in ids:
                typ, msgdata = conn.fetch(seq, '(RFC822)')
                raw = None
                for part in msgdata:
                    if isinstance(part, tuple):
                        raw = part[1]; break
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                local = _recipient_local(msg)
                stored = store_message(local, msg) if local else False
                if stored:
                    conn.store(seq, '+FLAGS', '\\Deleted')
            if ids:
                conn.expunge()
            conn.logout()
        except Exception as e:
            log('imap_err', e)
        time.sleep(10)

def cleanup_loop():
    while True:
        try:
            now = int(time.time())
            c = db()
            c.execute('DELETE FROM messages WHERE expires_at<=?', (now,))
            c.execute('DELETE FROM logs WHERE at<?', (now - LOG_TTL,))
            c.commit(); c.close()
        except Exception as e:
            log('clean_err', e)
        time.sleep(60)

PAGE = r'''<!doctype html><html lang="zh-CN"><head><base href="__BASE__"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>临时邮箱</title>
<style>
:root{--bg:#0f1216;--card:#171b21;--line:#262c35;--ink:#e7ebf1;--muted:#8b95a5;--accent:#3b82f6}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;min-height:100vh}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin-bottom:4px}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.addr{flex:1;min-width:240px;font-family:ui-monospace,Consolas,monospace;font-size:15px;background:#0b0e12;border:1px dashed var(--line);border-radius:8px;padding:10px 12px;word-break:break-all}
button{background:#20262f;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:9px 14px;cursor:pointer;font:inherit}
button.pri{background:var(--accent);border-color:var(--accent);color:#fff}
button:hover{filter:brightness(1.1)}
.list{margin-top:16px;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--card)}
.row{display:flex;gap:12px;padding:12px 14px;border-bottom:1px solid var(--line);cursor:pointer;align-items:baseline}
.row:hover{background:#1b2027}
.row:last-child{border-bottom:none}
.row .s{flex:1;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .f{color:var(--muted);font-size:12px;white-space:nowrap}
.row .t{color:var(--accent);font-size:12px;font-family:ui-monospace,Consolas,monospace;white-space:nowrap}
.empty{padding:36px 16px;text-align:center;color:var(--muted)}
.view{margin-top:16px;border:1px solid var(--line);border-radius:12px;background:#fff;color:#111;overflow:hidden}
.view .vh{padding:14px 16px;border-bottom:1px solid #e5e7eb;background:#f8fafc}
.view .vh b{display:block;font-size:15px;margin-bottom:2px}
.view .vh span{color:#6b7280;font-size:12px}
.view .vb{padding:14px 16px;max-height:60vh;overflow:auto}
.view iframe{width:100%;height:60vh;border:0}
.top{display:flex;gap:8px;justify-content:flex-end;margin-top:8px}
.err{color:#f87171;font-size:12px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>临时邮箱</h1>
<div class="sub">只收不发 · 邮件 1 小时后自动删除 · 地址不过期</div>
<div class="bar">
  <div class="addr" id="addr">（生成中…）</div>
  <button id="copy">复制</button>
  <button id="newAddr" class="pri">换一个</button>
  <button id="alias">换别名</button>
  <input id="custom" placeholder="自选地址，例如 hello"
    style="flex:1;min-width:160px;background:#0b0e12;border:1px solid var(--line);border-radius:8px;padding:9px 12px;color:var(--ink);font:inherit">
  <button id="useCustom">用这个</button>
</div>
<div class="list" id="list"><div class="empty">等待收信…</div></div>
<div class="view" id="view" style="display:none"></div>
<div class="top"><button id="refresh">刷新</button></div>
<div class="err" id="err"></div>
</div>
<script>
const DOM = DOMAIN_PLACEHOLDER;
let inbox=null, address='', timer=null;
const $=s=>document.querySelector(s);
async function api(p,opt){const r=await fetch(p,opt);return r.json()}
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmt(t){const s=Math.max(0,t);const m=Math.floor(s/60);return m>0?m+' 分':'<1 分'}
async function newInbox(){
  const r=await api('api/inbox',{method:'POST'});
  inbox=r.inbox; address=r.address; $('#addr').textContent=address; $('#view').style.display='none';
  history.replaceState(null,'','?i='+inbox);
  await poll();
}
async function poll(){
  if(!inbox) return;
  try{
    const r=await api('api/inbox/'+inbox);
    renderList(r.messages||[]);
  }catch(e){ $('#err').textContent=String(e) }
  clearTimeout(timer); timer=setTimeout(poll,4000);
}
function renderList(ms){
  const el=$('#list');
  if(!ms.length){el.innerHTML='<div class="empty">等待收信…</div>';return}
  el.innerHTML=ms.map(m=>`<div class="row" data-id="${m.id}">
     <span class="s">${esc(m.subject||'(无主题)')}</span>
     <span class="f">${esc(m.mail_from||'')}</span>
     <span class="t">${fmt(m.remain)}</span></div>`).join('');
  [...el.querySelectorAll('.row')].forEach(r=>r.onclick=()=>openMsg(r.dataset.id));
}
async function openMsg(id){
  const r=await api('api/inbox/'+inbox+'/msg/'+id);
  const v=$('#view'); v.style.display='';
  v.innerHTML=`<div class="vh"><b>${esc(r.subject||'(无主题)')}</b><span>${esc(r.mail_from||'')}</span></div>
    <div class="vb" id="vbody"></div>
    <div class="top" style="padding:10px 16px;background:#f8fafc;border-top:1px solid #e5e7eb">
      <button onclick="delMsg('${id}')">删除</button><button onclick="closeMsg()">关闭</button></div>`;
  fillBody(r);
}
function fillBody(r){
  const box=document.getElementById('vbody'); if(!box) return;
  if(r.body_html){
    const f=document.createElement('iframe');
    f.setAttribute('sandbox','allow-popups allow-popups-to-escape-sandbox');
    f.style.cssText='width:100%;height:60vh;border:0;background:#fff';
    f.srcdoc=String(r.body_html); box.appendChild(f);
  } else {
    const pre=document.createElement('pre'); pre.style.cssText='white-space:pre-wrap;font:inherit';
    pre.textContent=r.body_text||''; box.appendChild(pre);
  }
}
function closeMsg(){ $('#view').style.display='none'; $('#view').innerHTML='' }
async function delMsg(id){ await api('api/inbox/'+inbox+'/msg/'+id,{method:'DELETE'}); closeMsg(); await poll() }
$('#copy').onclick=async()=>{try{await navigator.clipboard.writeText(address);$('#err').textContent='已复制'}catch(e){$('#err').textContent='复制失败，请手动选择'}};
$('#newAddr').onclick=newInbox;
$('#alias').onclick=async()=>{ if(!inbox) return; const r=await api('api/inbox/'+inbox+'/alias',{method:'POST'}); if(r.address){address=r.address;$('#addr').textContent=address;$('#err').textContent='已换别名'} };
$('#useCustom').onclick=async()=>{ const v=($('#custom').value||'').trim(); if(!v){$('#err').textContent='先写地址前缀';return} const r=await api('api/inbox',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address:v})}); if(r.inbox){inbox=r.inbox;address=r.address;$('#addr').textContent=address;history.replaceState(null,'','?i='+inbox);await poll()} else {$('#err').textContent=r.error||'地址不可用'} };
$('#refresh').onclick=poll;
(async()=>{
  const m=new URLSearchParams(location.search).get('i');
  if(m){ inbox=m; const r=await api('api/inbox/'+inbox); address=r.address||''; $('#addr').textContent=address||'（已失效）'; await poll(); }
  else await newInbox();
})();
</script></body></html>'''

class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers(); self.wfile.write(b)

    def _page(self, html):
        b = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers(); self.wfile.write(b)

    def log_message(self, fmt, *a):
        pass

    def _path(self):
        p = urlparse(self.path).path
        if BASE != '/' and p.startswith(BASE):
            p = '/' + p[len(BASE):]
        return p

    def do_GET(self):
        p = self._path()
        if p == '/' or p.startswith('/i/'):
            self._page(PAGE.replace('DOMAIN_PLACEHOLDER', json.dumps(DOMAIN)).replace('__BASE__', BASE))
            return
        m = re.match(r'^/api/inbox/([A-Za-z0-9]+)$', p)
        if m:
            row = inbox_by_id(m.group(1))
            if not row:
                return self._json(404, {'error': 'inbox 不存在'})
            return self._json(200, {'address': row['address'], 'messages': messages_of(row['id'])})
        m = re.match(r'^/api/inbox/([A-Za-z0-9]+)/msg/([A-Za-z0-9]+)$', p)
        if m:
            msg = message_of(m.group(1), m.group(2))
            if not msg:
                return self._json(404, {'error': '邮件不存在或已过期'})
            return self._json(200, msg)
        self._json(404, {'error': 'not found'})

    def _origin_ok(self):
        o = self.headers.get('Origin') or ''
        if not o:
            return True
        h = (self.headers.get('Host') or '').strip()
        return urlparse(o).netloc == h

    def do_POST(self):
        if not self._origin_ok():
            return self._json(403, {'error': 'cross-origin'})
        p = self._path()
        m = re.match(r'^/api/inbox/([A-Za-z0-9]+)/alias$', p)
        if m:
            addr = rotate_address(m.group(1))
            if not addr:
                return self._json(409, {'error': '生成别名失败'})
            return self._json(200, {'address': addr})
        if p == '/api/inbox':
            n = int(self.headers.get('Content-Length') or 0)
            body = {}
            if n:
                try: body = json.loads(self.rfile.read(n).decode() or '{}')
                except Exception: body = {}
            ip = (self.client_address[0] if self.client_address else '') or ''
            now = time.time()
            bucket = _create_hits.setdefault(ip, [])
            bucket[:] = [t for t in bucket if now - t < CREATE_WINDOW]
            if len(bucket) >= CREATE_LIMIT:
                return self._json(429, {'error': '太频繁，稍后再试'})
            bucket.append(now)
            addr = (body or {}).get('address') or None
            if addr and '@' in addr and not addr.lower().endswith('@' + DOMAIN):
                return self._json(400, {'error': '地址必须在本域名下'})
            r = create_inbox(addr)
            if not r:
                return self._json(409, {'error': '地址已被占用，换一个'})
            return self._json(200, r)
        self._json(404, {'error': 'not found'})

    def do_DELETE(self):
        p = self._path()
        m = re.match(r'^/api/inbox/([A-Za-z0-9]+)/msg/([A-Za-z0-9]+)$', p)
        if m:
            delete_message(m.group(1), m.group(2))
            return self._json(200, {'ok': True})
        self._json(404, {'error': 'not found'})

def main():
    init_db()
    threading.Thread(target=imap_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    log('boot', 'port=%d domain=%s' % (PORT, DOMAIN))
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()

if __name__ == '__main__':
    main()
