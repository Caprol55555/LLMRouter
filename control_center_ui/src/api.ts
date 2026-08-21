export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function translateApiMessage(message: string): string {
  const replacements: Array<[string, string]> = [
    ["Invalid administrator credentials", "管理员密码错误"],
    ["Administrator session is required", "需要管理员登录"],
    ["Current password is incorrect", "当前密码不正确"],
    ["New password must be at least 8 characters", "新密码至少需要 8 位"],
    ["Configuration storage is unavailable", "配置存储不可用"],
    ["Telemetry is unavailable", "遥测数据不可用"],
    ["Request failed", "请求失败"],
  ];
  return replacements.reduce((result, [from, to]) => result.replace(from, to), message);
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...init });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(
      response.status,
      translateApiMessage(body?.error?.message || `Request failed (${response.status})`),
    );
  }
  return body as T;
}
