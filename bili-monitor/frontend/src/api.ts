// API 客户端：BASE 随 Vite base（生产 /bili-monitor/，dev /），401 统一跳
// 登录。错误信息取响应体 detail；fetch 拼 Cookie（同源）。
export const BASE: string = import.meta.env.BASE_URL;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  // BASE 尾带斜杠 + path 头带斜杠时去重（双斜杠路径不匹配路由 → 404）
  const url = BASE.endsWith("/") && path.startsWith("/")
    ? BASE + path.slice(1) : BASE + path;
  const r = await fetch(url, opts);
  if (r.status === 401) {
    location.href = BASE + "login";
    throw new ApiError(401, "未登录");
  }
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({} as any))).detail;
    throw new ApiError(r.status, String(detail ?? r.status));
  }
  return r.json() as Promise<T>;
}

export function postJson<T = any>(path: string, body: unknown): Promise<T> {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function patchJson<T = any>(path: string, body: unknown): Promise<T> {
  return api<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---- 身份（/api/auth/me：{role, username, me?, room_id?, nickname?}）----
export interface Me {
  role: "admin" | "clipper" | "guest" | null;
  username: string | null;
  me: string | null; // 注释/切片署名（admin=用户名、clipper=昵称）
  room_id?: number;
  nickname?: string;
}
export async function fetchMe(): Promise<Me> {
  return api<Me>("/api/auth/me");
}

// 触发浏览器下载（签名 URL 已带 attachment 头；本机直链用 <a download>）
export function triggerDownload(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export async function logout(): Promise<void> {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    /* 会话已失效也无妨 */
  }
  location.href = BASE + "login";
}
