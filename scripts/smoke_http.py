"""真实 HTTP 冒烟：/docs、提交、错误码、流式下载与摘要。"""

import base64
import hashlib
import json
import os
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8011")
BLOCK = 4096


def http(method, path, body=None, stream=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode()), None
    if stream:
        return resp.status, dict(resp.headers), resp
    return resp.status, dict(resp.headers), json.loads(resp.read().decode())


# 1. /docs 与 openapi 可访问
docs = urllib.request.urlopen(BASE + "/docs").status
openapi = json.loads(urllib.request.urlopen(BASE + "/openapi.json").read())
assert docs == 200
assert "/recover/commit" in openapi["paths"] and "/image" in openapi["paths"]
print("1) /docs 与 /openapi.json OK，接口:", sorted(openapi["paths"].keys()))

# 2. 当前版本与基线摘要
_, _, version = http("GET", "/version")
print("2) 当前版本:", version)
assert version["version"] == 1

# 3. 下载基线，核对摘要与长度
_, headers, resp = http("GET", "/image", stream=True)
lower_headers = {k.lower(): v for k, v in headers.items()}
baseline = resp.read()
assert hashlib.sha256(baseline).hexdigest() == version["sha256"]
assert lower_headers["x-image-version"] == "1"
print("3) 基线下载 OK，sha256 =", hashlib.sha256(baseline).hexdigest()[:16], "...")

# 4. 构造 v2：覆盖块 5、丢弃块 10
image = bytearray(baseline)
new_block = bytes((i * 13 + 7) & 0xFF for i in range(BLOCK))
image[5 * BLOCK : 6 * BLOCK] = new_block
image[10 * BLOCK : 11 * BLOCK] = b"\x00" * BLOCK
target_sha = hashlib.sha256(bytes(image)).hexdigest()
code, _, body = http(
    "POST",
    "/recover/commit",
    {
        "parent_version": 1,
        "target_sha256": target_sha,
        "changes": [
            {"index": 5, "op": "write", "data_base64": base64.b64encode(new_block).decode()},
            {"index": 10, "op": "discard"},
        ],
    },
)
assert code == 201 and body["version"] == 2, body
print("4) 提交 v2 成功:", body)

# 5. 错误摘要 → 400，且版本不变
code, body, _ = http(
    "POST",
    "/recover/commit",
    {
        "parent_version": 2,
        "target_sha256": "0" * 64,
        "changes": [{"index": 0, "op": "discard"}],
    },
)
assert code == 400 and "摘要不吻合" in body["detail"]
print("5) 错误摘要被拒绝(400):", body["detail"])

# 6. 过期父版本 → 409
code, body, _ = http(
    "POST",
    "/recover/commit",
    {"parent_version": 1, "target_sha256": "0" * 64, "changes": []},
)
assert code == 409 and "不是当前版本" in body["detail"]
print("6) 过期父版本被拒绝(409):", body["detail"])

# 7. 下载 v2，逐字节与期望镜像核对
_, headers, resp = http("GET", "/image", stream=True)
lower_headers = {k.lower(): v for k, v in headers.items()}
downloaded = resp.read()
assert lower_headers["x-image-version"] == "2"
assert downloaded == bytes(image)
assert hashlib.sha256(downloaded).hexdigest() == target_sha
print("7) v2 流式下载 OK，内容与摘要完全吻合")

# 8. 再做 v3（空变动批次也允许，发布同内容新版本）
code, _, body = http(
    "POST",
    "/recover/commit",
    {"parent_version": 2, "target_sha256": target_sha, "changes": []},
)
assert code == 201 and body["version"] == 3
print("8) 空变动批次发布 v3:", body)

print("\n全部冒烟断言通过")
