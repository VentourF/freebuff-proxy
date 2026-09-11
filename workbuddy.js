/**
 * workbuddy.js — WorkBuddy（www.workbuddy.ai）上游 provider
 *
 * 与 freebuff(www.codebuff.com) 是**两条独立上游**，协议差异：
 *   | 维度       | freebuff                      | WorkBuddy                      |
 *   |-----------|-------------------------------|--------------------------------|
 *   | 聊天路径   | /api/v1/chat/completions      | /v2/chat/completions           |
 *   | session   | create/delete 生命周期         | 无，直接发                       |
 *   | 流式      | 可选                          | **强制流式**（非流式报 11101）    |
 *   | 首条消息   | 随意                          | **必须 system**（否则 11128）    |
 *   | developer | —                             | 不支持，需改写为 system           |
 *   | UA        | ai-sdk/.../codebuff           | WorkBuddyAI/<version>          |
 *
 * 凭据来源：credentials/workbuddy_credentials.json（由 tools/sync_workbuddy_creds.py 生成）
 * 鉴权：Authorization: Bearer <accessToken>（accessToken 为 Keycloak JWT，有效期约 365 天）
 * 刷新：POST /auth/realms/copilot/protocol/openid-connect/token
 *        grant_type=refresh_token & client_id=console & refresh_token=<RT>
 */

import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const CRED_DIR = resolve(dirname(fileURLToPath(import.meta.url)), 'credentials');

export const WB_BASE = 'https://www.workbuddy.ai';
export const WB_CHAT_PATH = '/v2/chat/completions';
export const WB_TOKEN_URL = `${WB_BASE}/auth/realms/copilot/protocol/openid-connect/token`;
export const WB_UA = 'WorkBuddyAI/5.5.2';

// 用户指定：对外只暴露这一个模型
export const WB_MODELS = ['deepseek-v4.1-flash'];

// ---------------------------------------------------------------------------
// 凭据加载（热加载：文件 mtime 签名变化才重读）
// ---------------------------------------------------------------------------

let credCache = null;
let credSig = '';

export function loadWorkbuddyAccounts() {
  const files = existsSync(CRED_DIR)
    ? readdirSync(CRED_DIR).filter((f) => f.startsWith('workbuddy') && f.endsWith('.json')).sort()
    : [];
  const sig = files
    .map((f) => {
      try {
        const st = statSync(resolve(CRED_DIR, f));
        return `${f}:${st.mtimeMs}`;
      } catch {
        return `${f}:?`;
      }
    })
    .join(';');
  if (sig === credSig && credCache) return credCache;

  const out = [];
  for (const f of files) {
    try {
      const obj = JSON.parse(readFileSync(resolve(CRED_DIR, f), 'utf-8'));
      const accts = obj.accounts && typeof obj.accounts === 'object' ? obj.accounts : {};
      for (const [uid, a] of Object.entries(accts)) {
        if (a && a.accessToken) {
          out.push({
            uid,
            email: a.email || '',
            accessToken: a.accessToken,
            refreshToken: a.refreshToken || '',
            expiresAt: a.expiresAt || 0,
            baseUrl: obj.baseUrl || WB_BASE,
            chatPath: obj.chatPath || WB_CHAT_PATH,
          });
        }
      }
    } catch {
      /* 坏文件跳过 */
    }
  }
  credCache = out;
  credSig = sig;
  return out;
}

// ---------------------------------------------------------------------------
// token 刷新：Keycloak refresh_token grant
// 同一个 refreshToken 在同一 sid 下可反复换新 AT，不必落盘也能用；
// 这里刷新后回写内存缓存（下次请求生效），落盘交给 sync_workbuddy_creds.py --refresh。
// ---------------------------------------------------------------------------

const refreshInflight = new Map(); // uid -> Promise

export async function refreshAccessToken(acct) {
  if (!acct.refreshToken) return null;
  if (refreshInflight.has(acct.uid)) return refreshInflight.get(acct.uid);

  const task = (async () => {
    try {
      const body = new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: 'console',
        refresh_token: acct.refreshToken,
      });
      const resp = await fetch(WB_TOKEN_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
        signal: AbortSignal.timeout(30000),
      });
      if (!resp.ok) return null;
      const d = await resp.json();
      if (!d.access_token) return null;
      acct.accessToken = d.access_token;
      if (d.refresh_token) acct.refreshToken = d.refresh_token;
      try {
        acct.expiresAt = JSON.parse(
          Buffer.from(d.access_token.split('.')[1], 'base64url').toString('utf-8'),
        ).exp;
      } catch {}
      return acct.accessToken;
    } catch {
      return null;
    } finally {
      refreshInflight.delete(acct.uid);
    }
  })();

  refreshInflight.set(acct.uid, task);
  return task;
}

// ---------------------------------------------------------------------------
// 账号选择：优先挑未过期的；全部过期则尝试刷新
// ---------------------------------------------------------------------------

let wbIdx = 0;
const wbCooldown = new Map(); // uid -> until ms

function isCooling(uid) {
  const until = wbCooldown.get(uid) || 0;
  return Date.now() < until;
}

export function coolDown(uid, ms) {
  wbCooldown.set(uid, Date.now() + ms);
}

export function pickWorkbuddyAccount() {
  const pool = loadWorkbuddyAccounts();
  if (pool.length === 0) return null;
  const now = Date.now() / 1000;
  // 先找未过期的可用号
  for (let i = 0; i < pool.length; i++) {
    const a = pool[(wbIdx + i) % pool.length];
    if (!isCooling(a.uid) && (a.expiresAt === 0 || a.expiresAt > now + 60)) {
      wbIdx = (wbIdx + i + 1) % pool.length;
      return a;
    }
  }
  // 都过期了，返回一个尝试刷新
  const a = pool[wbIdx % pool.length];
  wbIdx = (wbIdx + 1) % pool.length;
  return a;
}

// ---------------------------------------------------------------------------
// 请求体规整：WorkBuddy 上游的硬性约束
//  1. stream 必须 true（11101）
//  2. 首条必须是 system（11128）
//  3. developer 角色不支持 → 改写为 system
// ---------------------------------------------------------------------------

export function normalizeWorkbuddyPayload(params, model) {
  const payload = { ...params, model, stream: true };
  const msgs = Array.isArray(payload.messages) ? [...payload.messages] : [];
  for (const m of msgs) {
    if (m && typeof m === 'object' && m.role === 'developer') m.role = 'system';
  }
  if (msgs.length === 0 || msgs[0].role !== 'system') {
    msgs.unshift({ role: 'system', content: 'You are a helpful assistant.' });
  }
  payload.messages = msgs;
  return payload;
}

export function workbuddyHeaders(acct) {
  return {
    Authorization: `Bearer ${acct.accessToken}`,
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
    'User-Agent': WB_UA,
  };
}

// ---------------------------------------------------------------------------
// 核心调用：**始终以上游流式发起**，再按调用方需求（流式/非流式）回吐
// ---------------------------------------------------------------------------

const WB_TIMEOUT_MS = 600000;

/**
 * @param {object} acct   账号对象（loadWorkbuddyAccounts 的元素）
 * @param {object} payload 已规整的请求体
 * @returns {Promise<Response>} 上游原始 Response（stream=true）
 */
export async function callWorkbuddy(acct, payload) {
  const url = (acct.baseUrl || WB_BASE) + (acct.chatPath || WB_CHAT_PATH);
  return fetch(url, {
    method: 'POST',
    headers: workbuddyHeaders(acct),
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(WB_TIMEOUT_MS),
  });
}

/** 把上游 SSE 流原样转发给客户端（流式模式）。 */
export function passthroughStream(upstream) {
  return new Response(upstream.body, {
    status: 200,
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      'Access-Control-Allow-Origin': '*',
    },
  });
}

/** 把上游 SSE 流聚合成一次性 chat.completion 响应（非流式模式）。 */
export async function aggregateStream(upstream, model) {
  const reader = upstream.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let content = '';
  let reasoning = '';
  let id = `chatcmpl-wb-${Date.now()}`;
  let created = Math.floor(Date.now() / 1000);
  let finishReason = 'stop';
  const toolCalls = [];

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const raw of lines) {
        const line = raw.trim();
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') continue;
        let chunk;
        try {
          chunk = JSON.parse(data);
        } catch {
          continue;
        }
        if (chunk.id) id = chunk.id;
        if (chunk.created) created = chunk.created;
        if (chunk.model) model = chunk.model;
        const choice = chunk.choices && chunk.choices[0];
        if (!choice) continue;
        if (choice.finish_reason) finishReason = choice.finish_reason;
        const delta = choice.delta || {};
        if (typeof delta.content === 'string') content += delta.content;
        if (typeof delta.reasoning_content === 'string') reasoning += delta.reasoning_content;
        if (Array.isArray(delta.tool_calls)) {
          for (const tc of delta.tool_calls) {
            const idx = tc.index || 0;
            toolCalls[idx] = toolCalls[idx] || { id: '', type: 'function', function: { name: '', arguments: '' } };
            if (tc.id) toolCalls[idx].id = tc.id;
            if (tc.function?.name) toolCalls[idx].function.name += tc.function.name;
            if (tc.function?.arguments) toolCalls[idx].function.arguments += tc.function.arguments;
          }
        }
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {}
  }

  const message = { role: 'assistant', content };
  if (reasoning) message.reasoning_content = reasoning;
  const tcs = toolCalls.filter(Boolean);
  if (tcs.length) {
    message.tool_calls = tcs.map((t, i) => ({ ...t, index: i }));
    if (!content) message.content = null;
  }

  return {
    id,
    object: 'chat.completion',
    created,
    model,
    choices: [{ index: 0, message, finish_reason: finishReason }],
    usage: null,
  };
}

/** 把上游错误体翻译成 OpenAI 风格错误响应。 */
export function workbuddyError(status, text) {
  let msg = text;
  let code = null;
  try {
    const d = JSON.parse(text);
    msg = d.msg || d.message || text;
    code = d.code;
  } catch {}
  return {
    status: status || 502,
    body: {
      error: {
        message: msg || 'upstream error',
        type: 'upstream_error',
        code,
      },
    },
  };
}

/** 健康摘要：不主动打上游，只读本地凭据状态。 */
export function workbuddyHealth() {
  const pool = loadWorkbuddyAccounts();
  const now = Date.now() / 1000;
  return {
    workbuddy_accounts: pool.length,
    workbuddy_detail: pool.map((a) => ({
      uid: a.uid,
      email: a.email,
      expires_in_days: a.expiresAt ? Math.round((a.expiresAt - now) / 86400) : null,
      cooling: isCooling(a.uid),
      has_refresh_token: !!a.refreshToken,
    })),
  };
}
