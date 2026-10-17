// API 客户端：BASE 随 Vite base（生产 /replay/，dev /），401 统一跳登录。
// 与旧版行为一致：错误信息取响应体 detail；fetch 拼 Cookie（同源）。
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

// ---- OSS 签名 URL 会话级缓存（v1.2 行为：同 key 只签一次）----
const ossUrlCache = new Map<string, string>();
export async function getOssUrl(key: string): Promise<string> {
  let url = ossUrlCache.get(key);
  if (!url) {
    const r = await api<{ url: string }>(
      "/api/rooms/" + keyRoom(key) + "/oss-url?key=" + encodeURIComponent(key));
    url = r.url;
    ossUrlCache.set(key, url);
  }
  return url;
}
// key 形如 recordings/<room>/...：取房间号段拼本房间接口
function keyRoom(key: string): string {
  return key.split("/")[1] ?? "0";
}

export async function ossDownload(key: string, filename = ""): Promise<void> {
  const url = await getOssUrl(key + (filename ? "" : ""));
  const a = document.createElement("a");
  a.href = url + (filename
    ? "" // 下载附件头已在签名时固定；文件名参数仅本机直链场景用
    : "");
  a.download = filename;
  a.target = "_blank";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
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

// song-detector 联动：签「附件下载」URL（服务端清洗 filename 后写入
// Content-Disposition；下载落盘名即本地识别结果的命名来源——
// 「<录播日期> <录播名> low.mp4」→ 识别结果 .analysis.json 同名对号）
export async function getOssDownloadUrl(key: string, filename: string): Promise<string> {
  const r = await api<{ url: string }>(
    "/api/rooms/" + keyRoom(key) + "/oss-url?key=" + encodeURIComponent(key) +
    "&as=download&filename=" + encodeURIComponent(filename));
  return r.url;
}

export async function logout(): Promise<void> {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    /* 会话已失效也无妨 */
  }
  location.href = BASE + "login";
}
