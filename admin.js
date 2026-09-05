// freebuff2api 账号管理路由（/accounts 页面 + /admin/api/*）
// 依赖: 宿主机的 register_service.py (FastAPI, 端口 8899)
import { readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getAccountStatus, banToken, unbanToken, isBannedTokenPublic } from './worker.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CRED_FILE = resolve(__dirname, 'credentials', 'freebuff_credentials.json');
const PAGE_FILE = resolve(__dirname, 'static', 'accounts.html');

// 容器内访问宿主机 Python 服务: host.docker.internal; 本机直接跑: 127.0.0.1
const REG_SVC = process.env.REGISTER_SERVICE_URL || 'http://host.docker.internal:8899';

// 与 server.js 相同的凭据读取逻辑: 收集 credentials 目录下全部 authToken
// （排除 banned_tokens.json: 它没有 authToken/accounts 字段，自然被跳过）
function collectTokens() {
  const tokens = [];
  const dir = resolve(__dirname, 'credentials');
  if (existsSync(dir)) {
    for (const f of readdirSync(dir)) {
      if (!f.endsWith('.json') || f === 'banned_tokens.json') continue;
      try {
        const obj = JSON.parse(readFileSync(resolve(dir, f), 'utf-8'));
        if (obj.authToken) tokens.push(obj.authToken.trim());
        if (obj.accounts && typeof obj.accounts === 'object') {
          for (const acct of Object.values(obj.accounts)) {
            if (acct && acct.authToken) tokens.push(acct.authToken.trim());
          }
        }
      } catch {}
    }
  }
  return tokens;
}

// 读取持久化 banned 名单（credentials/banned_tokens.json，由 worker 维护）
function readBannedTokens() {
  try {
    const f = resolve(__dirname, 'credentials', 'banned_tokens.json');
    if (!existsSync(f)) return [];
    const obj = JSON.parse(readFileSync(f, 'utf-8'));
    return Array.isArray(obj?.tokens) ? obj.tokens : [];
  } catch { return []; }
}

// banned 账号移出账号列表（v1.9.2）：从 freebuff_credentials.json 中清除已 banned
// 的账号。banned 是官方终态（不可恢复），token 已持久化在 banned_tokens.json 中，
// 调度层（worker.pickToken）按该名单永久跳过 —— 凭据里留着只会让账号列表出现
// 永远不可用的死号。无变化时不写文件（避免 mtime 抖动触发无谓热加载）。
function purgeBannedAccounts(creds) {
  const accounts = creds.accounts || {};
  const bannedIds = Object.entries(accounts)
    .filter(([, a]) => a && a.authToken && isBannedTokenPublic(a.authToken))
    .map(([id]) => id);
  if (bannedIds.length === 0) return 0;
  for (const id of bannedIds) delete accounts[id];
  creds.accounts = accounts;
  writeCreds(creds);
  console.log(`[admin] purged ${bannedIds.length} banned account(s) from list: ${bannedIds.join(', ')}`);
  return bannedIds.length;
}

// 取 worker 运行时账号状态快照 (健康/冷却/使用中)
function accountUsageSnapshot() {
  try {
    return getAccountStatus({ FREEBUFF_TOKEN: collectTokens().join(',') });
  } catch {
    return { now: Date.now(), accounts: [] };
  }
}

function readCreds() {
  if (!existsSync(CRED_FILE)) return { accounts: {} };
  try { return JSON.parse(readFileSync(CRED_FILE, 'utf-8')); }
  catch { return { accounts: {} }; }
}

function writeCreds(creds) {
  writeFileSync(CRED_FILE, JSON.stringify(creds, null, 2), 'utf-8');
}

function json(res, status, obj) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(obj));
}

async function proxyJson(url, { method = 'GET', body } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 240000);
  try {
    const r = await fetch(url, {
      method,
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: r.status, data };
  } catch (err) {
    return { status: 502, data: { error: err?.message || '注册服务不可达' } };
  } finally {
    clearTimeout(timer);
  }
}

export async function handleAdmin(req, nodeRes) {
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname;

  // ---------------- 页面 ----------------
  if (p === '/accounts' || p === '/accounts/') {
    if (!existsSync(PAGE_FILE)) return json(nodeRes, 404, { error: 'page not found' });
    nodeRes.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    nodeRes.end(readFileSync(PAGE_FILE, 'utf-8'));
    return;
  }

  if (p === '/admin/api/accounts') {
    const creds = readCreds();
    // banned 账号移出账号列表：自动从凭据文件清除（token 留在 banned_tokens.json，
    // 调度层永不回选）。页面每 15s 轮询本接口，运行期新 ban 的号也会被及时清掉。
    purgeBannedAccounts(creds);
    const usage = accountUsageSnapshot();
    const byToken = new Map((usage.accounts || []).map((a) => [a.token, a]));
    const now = usage.now || Date.now();
    const IN_USE_WINDOW_MS = 2 * 60 * 1000; // 最近 2 分钟被选用视为"使用中"
    const list = Object.entries(creds.accounts || {})
      // 双保险：即使凭据写回失败（只读卷等），banned 账号也不进列表
      .filter(([, a]) => !(a && a.authToken && isBannedTokenPublic(a.authToken)))
      .map(([id, a]) => {
      const st = byToken.get(a.authToken) || {};
      const lastUsedAt = st.lastUsedAt || null;
      const cooldownRemainingMs = st.cooldownRemainingMs || 0;
      return {
        id,
        name: a.name || '',
        email: a.email || '',
        token: a.authToken ? a.authToken.slice(0, 8) + '...' : '',
        password: a.password || '',
        registeredAt: a.registeredAt || null,
        alive: st.alive ?? null,
        state: st.state || 'unknown',
        staleFail: st.staleFail || false, // 失效观测已过期，重新参与调度等待自愈
        removed: st.removed || isBannedTokenPublic(a.authToken), // 已移出可用池（banned）
        checkedAt: st.checkedAt || null, // 快照时间戳（ms），页面显示"快照于 X 分钟前"
        inUse: Boolean(lastUsedAt && now - lastUsedAt < IN_USE_WINDOW_MS),
        lastUsedAt,
        cooldownUntil: st.cooldownUntil || null,
        cooldownRemainingMs,
        inCooldown: cooldownRemainingMs > 0,
      };
    });
    return json(nodeRes, 200, { accounts: list });
  }

  if (p === '/admin/api/register' && req.method === 'POST') {
    const body = await req.json().catch(() => ({}));
    const r = await proxyJson(`${REG_SVC}/register`, { method: 'POST', body: { auto_mail: true, ...body } });
    return json(nodeRes, r.status, r.data);
  }

  if (p === '/admin/api/test-proxy' && req.method === 'POST') {
    const body = await req.json().catch(() => ({}));
    const r = await proxyJson(`${REG_SVC}/test_proxy`, { method: 'POST', body });
    return json(nodeRes, r.status, r.data);
  }

  if (p === '/admin/api/cf-config') {
    if (req.method === 'GET') {
      const r = await proxyJson(`${REG_SVC}/cf_config`);
      return json(nodeRes, r.status, r.data);
    }
    if (req.method === 'POST') {
      const body = await req.json().catch(() => ({}));
      const r = await proxyJson(`${REG_SVC}/cf_config`, { method: 'POST', body });
      return json(nodeRes, r.status, r.data);
    }
  }

  if (p === '/admin/api/cf-fetch-domains' && req.method === 'POST') {
    const body = await req.json().catch(() => ({}));
    const r = await proxyJson(`${REG_SVC}/cf/fetch_domains`, { method: 'POST', body });
    return json(nodeRes, r.status, r.data);
  }

  if (p.startsWith('/admin/api/task/')) {
    const taskId = decodeURIComponent(p.split('/').pop());
    const r = await proxyJson(`${REG_SVC}/status/${taskId}`);
    return json(nodeRes, r.status, r.data);
  }

  if (p.startsWith('/admin/api/complete/') && req.method === 'POST') {
    const taskId = decodeURIComponent(p.split('/').pop());
    const r = await proxyJson(`${REG_SVC}/complete/${taskId}`, { method: 'POST' });
    return json(nodeRes, r.status, r.data);
  }

  if (p.startsWith('/admin/api/submit-code/') && req.method === 'POST') {
    const taskId = decodeURIComponent(p.split('/').pop());
    const body = await req.json().catch(() => ({}));
    const r = await proxyJson(`${REG_SVC}/submit_code/${taskId}`, { method: 'POST', body });
    return json(nodeRes, r.status, r.data);
  }

  if (p === '/admin/api/accounts/add' && req.method === 'POST') {
    const body = await req.json().catch(() => ({}));
    if (!body.authToken || !body.userId) return json(nodeRes, 400, { error: 'authToken and userId required' });
    const creds = readCreds();
    creds.accounts = creds.accounts || {};
    creds.accounts[body.userId] = {
      email: body.email || '',
      name: body.name || '',
      authToken: body.authToken,
      registeredAt: body.registeredAt || Date.now(),
    };
    writeCreds(creds);
    console.log(`[admin] account added: ${body.userId} (${body.name || body.email})`);
    // 不需要重启：worker 的 loadTokenList 带文件签名检测，下次请求自动热加载新凭据。
    return json(nodeRes, 200, { ok: true, message: 'account added, hot reloaded' });
  }

  if (p === '/admin/api/accounts/remove' && req.method === 'POST') {
    const body = await req.json().catch(() => ({}));
    // 前端列表字段叫 id（= creds key），兼容 userId 两种叫法
    const acctId = body.id || body.userId;
    if (!acctId) return json(nodeRes, 400, { error: 'id required' });
    const creds = readCreds();
    if (creds.accounts && creds.accounts[acctId]) {
      // 从凭据删除 + 加入持久化 banned 名单（双保险：即使凭据文件被重新写回也不会再用）
      const token = creds.accounts[acctId].authToken;
      delete creds.accounts[acctId];
      writeCreds(creds);
      if (token) banToken(token);
      // 不重启容器：worker 靠文件签名热加载，移除立即生效且服务不中断
      // （banned_tokens.json 持久化保证重启后也跳过该 token）。
      return json(nodeRes, 200, { ok: true, message: 'account removed, hot reloaded' });
    }
    return json(nodeRes, 404, { error: 'account not found' });
  }

  // GET /admin/api/banned → 列出持久化 banned 的 token 列表
  // v1.9.2: banned 账号已从凭据文件清除，直接读 banned_tokens.json（更准确，
  // 也能看到历史所有被移出/被 ban 的 token）。
  if (p === '/admin/api/banned' && req.method === 'GET') {
    const bannedList = readBannedTokens()
      .map((t) => ({ token: t.slice(0, 8) + '...' }));
    return json(nodeRes, 200, { banned: bannedList });
  }

  // POST /admin/api/accounts/restore → 解除 banned 暂停，账号恢复参与调度（凭据保留）
  if (p === '/admin/api/accounts/restore' && req.method === 'POST') {
    const body = await req.json().catch(() => ({}));
    const acctId = body.id || body.userId;
    if (!acctId) return json(nodeRes, 400, { error: 'id required' });
    const creds = readCreds();
    if (creds.accounts && creds.accounts[acctId]) {
      const token = creds.accounts[acctId].authToken;
      if (token) unbanToken(token);
      return json(nodeRes, 200, { ok: true, message: 'account restored, hot reloaded' });
    }
    return json(nodeRes, 404, { error: 'account not found' });
  }

  // POST /admin/api/banned  payload: { token: "..." } 或 { action: "clear" }
  // 用于手动管理 banned 名单（目前仅 "clear" 清空，其他修改在界面上操作）
  // 当前不需要，有需要再加

  if (p === '/admin/api/health') {
    const r = await proxyJson(`${REG_SVC}/`).catch(() => ({ status: 502, data: null }));
    // 账号池汇总
    const usage = accountUsageSnapshot();
    const accs = usage.accounts || [];
    const pool = {
      accounts: accs.length,
      alive: accs.filter((a) => a.alive === true).length,
      unhealthy: accs.filter((a) => a.alive === false).length,
      unknown: accs.filter((a) => a.alive === null).length,
      inCooldown: accs.filter((a) => (a.cooldownRemainingMs || 0) > 0).length,
      inUse: accs.filter((a) => a.lastUsedAt && usage.now - a.lastUsedAt < 2 * 60 * 1000).length,
      bannedRemoved: accs.filter((a) => a.removed).length, // 池内仍有 banned 残留（清凭据前的过渡态）
      bannedTotal: readBannedTokens().length,              // 累计移出（含自动 ban + 手动移除）
    };
    return json(nodeRes, r.status === 200 ? 200 : 502, {
      registerService: r.status === 200 ? 'ok' : 'unreachable',
      credFile: CRED_FILE,
      pool,
    });
  }

  return json(nodeRes, 404, { error: 'not found' });
}
