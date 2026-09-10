from urllib.parse import quote

from app.config import BANGUMI_BASE
from app.errors import AppError
from app.http import APIClient, response_json


class Bangumi(APIClient):
    def __init__(self, token, **kwargs):
        super().__init__(BANGUMI_BASE, token, **kwargs)

    async def me(self):
        user = response_json(await self.request("GET", "/v0/me"))
        if not isinstance(user.get("id"), int) or not user.get("username"):
            raise AppError("Bangumi 没有返回有效的账号信息。")
        return {k: user[k] for k in ("id", "username", "nickname") if k in user}

    async def pages(self, username):
        offset = 0
        seen = set()
        while True:
            page = response_json(
                await self.request(
                    "GET",
                    f"/v0/users/{quote(username, safe='')}/collections",
                    params={"limit": 50, "offset": offset},
                )
            )
            rows = page.get("data")
            if not isinstance(rows, list):
                raise AppError("Bangumi 收藏分页格式不正确。")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("subject_id"), int):
                    raise AppError("Bangumi 返回了无效的收藏条目。")
                if row["subject_id"] in seen:
                    raise AppError("扫描期间收藏分页发生变化，请重新扫描。")
                seen.add(row["subject_id"])
            yield page
            offset += len(rows)
            if len(rows) < 50:
                if isinstance(page.get("total"), int) and offset < page["total"]:
                    raise AppError("Bangumi 返回的收藏不完整，请重新扫描。")
                break

    async def subject(self, subject_id):
        if type(subject_id) is not int or subject_id <= 0:
            raise AppError("Bangumi 条目编号无效。")
        response = await self.request(
            "GET", f"/v0/subjects/{subject_id}", allowed=(200, 404), retry=False
        )
        if response.status_code == 404:
            return None
        detail = response_json(response)
        if detail.get("id") != subject_id:
            raise AppError("Bangumi 条目信息与请求编号不一致。")
        return detail
